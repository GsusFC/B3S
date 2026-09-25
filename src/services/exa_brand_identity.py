"""Brand identity for Exa's external searches, read from the owned website."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.config import LLM_CHEAP_MODEL
from src.features.llm_analyzer import LLMAnalyzer, llm_failure_reason

if TYPE_CHECKING:
    # Imported for typing only: src.collectors imports the Exa collector,
    # which imports this module.
    from src.collectors.web_collector import WebData

_TITLE_SEPARATORS = re.compile(r" \| | - | – | — |: ")
_DOMAIN_LIKE = re.compile(r"^(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/?$")
_DESCRIPTION_MAX_CHARS = 200
_HEAD_SCAN_CHARS = 200_000
_OG_KEYS = ("og:site_name", "og:description")

_IDENTITY_CHECK_TIMEOUT_SECONDS = 20
_IDENTITY_CHECK_MAX_TOKENS = 2000
_IDENTITY_CHECK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["same_company"],
    "properties": {"same_company": {"type": "array", "items": {"type": "integer"}}},
}
_IDENTITY_CHECK_SYSTEM = "You check whether web search results are about one specific company. Answer only with JSON."


@dataclass(frozen=True)
class BrandIdentity:
    name: str
    domain: str
    description: str
    strong_aliases: tuple[str, ...]


@dataclass(frozen=True)
class IdentityCandidate:
    url: str
    title: str
    text: str


class IdentityCheckError(RuntimeError):
    """The identity check gave no usable verdict; `reason` is a short code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def brand_domain(brand_url: str | None) -> str:
    if not brand_url:
        return ""
    parsed = urlparse(brand_url)
    hostname = (parsed.netloc or parsed.path or "").lower()
    hostname = hostname.replace("www.", "").strip("/")
    return hostname


def derive_brand_identity(
    *,
    brand_name: str,
    brand_url: str | None,
    web_data: WebData | None,
) -> BrandIdentity:
    domain = brand_domain(brand_url)
    label = domain.split(".", 1)[0]
    og = _og_meta(_text_attr(web_data, "html"))
    segments = [
        segment.strip()
        for segment in _TITLE_SEPARATORS.split(_collapse(_text_attr(web_data, "title")))
        if segment.strip()
    ]
    tokens = [token for token in (_brand_token(brand_name), _compact(label)) if len(token) >= 3]
    branded_segments = [segment for segment in segments if any(token in _compact(segment) for token in tokens)]
    name = (
        og.get("og:site_name")
        or max(branded_segments, key=len, default="")
        or label.replace("-", " ").title()
        or _collapse(brand_name)
    )
    description = _trim(
        _text_attr(web_data, "meta_description")
        or og.get("og:description")
        or " · ".join(segment for segment in segments if _compact(segment) != _compact(name))
    )
    aliases = {_compact(domain)}
    aliases.update(_compact(candidate) for candidate in (name, *branded_segments) if _is_multi_word(candidate))
    if "-" in label:
        aliases.add(_compact(label))
    return BrandIdentity(
        name=name,
        domain=domain,
        description=description,
        strong_aliases=tuple(sorted(alias for alias in aliases if alias)),
    )


def llm_identity_check(
    identity: BrandIdentity,
    candidates: Sequence[IdentityCandidate],
    *,
    llm: Any | None = None,
) -> set[int]:
    """Return the indices of the candidates that are about the identity's company."""

    analyzer = llm if llm is not None else LLMAnalyzer(model=LLM_CHEAP_MODEL)
    if not getattr(analyzer, "api_key", None):
        raise IdentityCheckError("llm_unavailable")
    response = analyzer._call_json(
        _IDENTITY_CHECK_SYSTEM,
        _identity_check_prompt(identity, candidates),
        max_tokens=_IDENTITY_CHECK_MAX_TOKENS,
        json_schema=_IDENTITY_CHECK_SCHEMA,
        schema_name="brand3_exa_identity_check",
        timeout_seconds=_IDENTITY_CHECK_TIMEOUT_SECONDS,
        temperature=0.0,
    )
    indices = response.get("same_company") if isinstance(response, dict) else None
    if not isinstance(indices, list) or not all(
        isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(candidates)
        for index in indices
    ):
        raise IdentityCheckError(llm_failure_reason(analyzer, "invalid_output"))
    return set(indices)


def _identity_check_prompt(identity: BrandIdentity, candidates: Sequence[IdentityCandidate]) -> str:
    company = f"Company: {identity.name}"
    if identity.domain:
        company += f" (website {identity.domain})"
    company += "."
    if identity.description:
        company += f" Its website describes it as: {identity.description}."
    results = [
        {"index": index, "url": candidate.url, "title": candidate.title, "text": candidate.text}
        for index, candidate in enumerate(candidates)
    ]
    return "\n".join(
        [
            company,
            "For each result, decide whether the page is about THIS company, not another organisation, "
            "product, person or thing with a similar name.",
            "Content published by the company itself (its own website or its own social media accounts) "
            "does not count.",
            'Return {"same_company": [indices of the results about this company]}.',
            f"Results: {json.dumps(results, ensure_ascii=False)}",
        ]
    )


class _OgMetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attributes = {key: value or "" for key, value in attrs}
        key = (attributes.get("property") or attributes.get("name") or "").strip().lower()
        content = _collapse(attributes.get("content", ""))
        if key in _OG_KEYS and content and key not in self.values:
            self.values[key] = content


def _og_meta(html: str) -> dict[str, str]:
    head = html[:_HEAD_SCAN_CHARS]
    head_end = head.lower().find("</head>")
    if head_end >= 0:
        head = head[:head_end]
    parser = _OgMetaParser()
    try:
        parser.feed(head)
    except Exception:
        # Owned HTML is arbitrary; a parse failure only loses the og hints.
        pass
    return parser.values


def _text_attr(web_data: Any, name: str) -> str:
    value = getattr(web_data, name, "") if web_data is not None else ""
    return value if isinstance(value, str) else ""


def _brand_token(brand_name: str) -> str:
    value = (brand_name or "").strip().lower()
    if _DOMAIN_LIKE.match(value):
        value = brand_domain(value).split(".", 1)[0]
    return _compact(value)


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _collapse(value: str) -> str:
    return " ".join((value or "").split())


def _is_multi_word(value: str) -> bool:
    return len(re.findall(r"[a-z0-9]+", (value or "").lower())) >= 2


def _trim(value: str) -> str:
    text = _collapse(value)
    if len(text) <= _DESCRIPTION_MAX_CHARS:
        return text
    cut = text[: _DESCRIPTION_MAX_CHARS + 1]
    boundary = cut.rfind(" ")
    return cut[:boundary] if boundary > 0 else cut[:_DESCRIPTION_MAX_CHARS]
