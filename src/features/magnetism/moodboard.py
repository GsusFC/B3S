"""Deterministic visual-asset selection from persisted scan inputs.

The moodboard is a read-only companion for a brand report. It extracts image
candidates from the immutable web capture, rejects common acquisition noise,
and ranks the remaining assets without network or LLM calls. The filtered
selection therefore cannot alter scoring or report authority and is fully
recomputable from the stored report.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

_LOG = logging.getLogger(__name__)

MAX_MOODBOARD_IMAGES = 8
LEGACY_MAX_MOODBOARD_IMAGES = 14
SELECTION_VERSION = "visual-assets-v2"
DEFAULT_SELECTION_MODE = "filtered"

# Strategic TLDR blocks that frame the imagery on the moodboard.
VISUAL_READING_BLOCKS = (
    "brand_idea",
    "personality",
    "attributes",
    "value_proposition",
)

_ROLE_PRIORITY = {"social_card": 0, "logo": 1, "hero": 2, "content": 3}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_SECTION_TAGS = {"article", "aside", "footer", "header", "main", "nav", "section"}

_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)")
_DIMENSION_RE = re.compile(r"^\s*(\d{1,5})")
_STYLE_DIMENSION_RE = re.compile(r"(?:^|;)\s*(width|height)\s*:\s*(\d{1,5})(?:px)?", re.I)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Hosts/paths that are tracking beacons or browser chrome, never brand imagery.
_TRACKING_MARKERS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook.com/tr",
    "/pixel",
    "tracking-pixel",
    "1x1",
    "spacer",
    "transparent.gif",
)
_PLACEHOLDER_MARKERS = (
    "placeholder",
    "placehold.",
    "placehold/",
    "dummy-image",
    "dummy_image",
    "no-image",
    "no_image",
    "image-not-found",
    "image_not_found",
    "default-image",
    "default_image",
    "blank-image",
    "blank_image",
)
_LANGUAGE_MARKERS = (
    "country-flag",
    "country_flag",
    "flag-icon",
    "flag_icon",
    "/flags/",
    "/flag/",
    "language-selector",
    "language_selector",
    "language-switcher",
    "language_switcher",
    "locale-switcher",
    "locale_switcher",
    "weglot",
    "wpml",
    "polylang",
)
_PARTNER_CONTEXT_MARKERS = (
    "partner",
    "partners",
    "cliente",
    "clientes",
    "client",
    "clients",
    "customer",
    "customers",
    "investor",
    "investors",
    "portfolio",
    "sponsor",
    "sponsors",
    "trusted by",
    "trusted-by",
    "backed by",
    "backed-by",
    "aseguradora",
    "aseguradoras",
    "insurance logos",
    "insurance-logos",
    "companies",
    "company logos",
    "logo cloud",
    "logo-cloud",
    "logo grid",
    "logo-grid",
)
_POSITIVE_CONTEXT_MARKERS = (
    "hero",
    "masthead",
    "campaign",
    "product",
    "producto",
    "editorial",
    "story",
    "about",
    "nosotros",
    "team",
    "equipo",
    "work",
    "case-study",
    "case study",
    "showcase",
)
_UI_ICON_MARKERS = (
    "arrow",
    "chevron",
    "hamburger",
    "menu-icon",
    "menu_icon",
    "close-icon",
    "close_icon",
    "search-icon",
    "search_icon",
    "caret",
    "dropdown",
    "social-icon",
    "social_icon",
    "linkedin-icon",
    "instagram-icon",
    "facebook-icon",
    "x-icon",
)
_GENERIC_BRAND_TOKENS = {
    "app",
    "brand",
    "co",
    "com",
    "company",
    "dev",
    "group",
    "global",
    "home",
    "io",
    "net",
    "official",
    "org",
    "site",
    "the",
    "vc",
    "website",
    "www",
}
_VARIANT_TOKENS = {
    "black",
    "color",
    "colour",
    "dark",
    "light",
    "white",
    "mono",
    "monochrome",
    "negative",
    "positive",
    "rgb",
    "cmyk",
    "1x",
    "2x",
    "3x",
}


def _configured_selection_mode(mode: str | None = None) -> str:
    candidate = str(mode or os.environ.get("B3S_VISUAL_EVIDENCE_FILTER_MODE", DEFAULT_SELECTION_MODE))
    candidate = candidate.strip().lower()
    return candidate if candidate in {"filtered", "legacy"} else DEFAULT_SELECTION_MODE


def _is_qualified_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost":
        return True
    try:
        ipaddress.ip_address(lowered)
    except ValueError:
        return "." in lowered and not lowered.startswith(".")
    return True


def _clean_url(base_url: str, raw: str) -> str | None:
    candidate = (raw or "").strip()
    if not candidate or candidate.startswith(("data:", "blob:", "javascript:")):
        return None
    resolved = urljoin(base_url, candidate) if base_url else candidate
    parsed = urlparse(resolved)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if not _is_qualified_hostname(parsed.hostname):
        return None
    return resolved.split("#", 1)[0]


def _dimension(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    match = _DIMENSION_RE.match(str(value or ""))
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def _style_dimensions(style: str) -> tuple[int | None, int | None]:
    values: dict[str, int] = {}
    for name, raw in _STYLE_DIMENSION_RE.findall(style or ""):
        values[name.lower()] = int(raw)
    return values.get("width"), values.get("height")


def _src_from_attrs(attrs: dict[str, str]) -> str:
    for key in ("src", "data-src", "data-lazy-src", "data-original"):
        if attrs.get(key):
            return attrs[key]
    srcset = attrs.get("srcset") or attrs.get("data-srcset") or ""
    options = [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
    return options[-1] if options else ""


def _frame_context(tag: str, attrs: dict[str, str]) -> str:
    values = [tag]
    for key in ("id", "class", "role", "aria-label", "data-section", "data-testid"):
        value = attrs.get(key, "").strip()
        if value:
            values.append(value)
    return " ".join(values)


def _role_from_context(raw: str, alt: str, context: str, default: str = "content") -> str:
    text = f"{raw} {alt} {context}".lower()
    if any(marker in text for marker in ("hero", "masthead", "banner-image", "banner_image")):
        return "hero"
    if any(marker in text for marker in ("logo", "wordmark", "brandmark")):
        return "logo"
    return default


class _ImageTagParser(HTMLParser):
    """Collect image candidates together with their surrounding DOM context."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[dict] = []
        self._stack: list[dict[str, str]] = []
        self._heading_tag = ""
        self._heading_parts: list[str] = []
        self._last_heading = ""
        self._index = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        if tag == "meta":
            key = (attr_map.get("property") or attr_map.get("name") or "").lower()
            content = attr_map.get("content", "")
            if key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src") and content:
                self._append(content, role="social_card", source="html_meta")
            elif key == "og:logo" and content:
                self._append(content, role="logo", source="html_meta")
        elif tag == "link":
            rel = (attr_map.get("rel") or "").lower()
            href = attr_map.get("href", "")
            if "icon" in rel and href:
                self._append(href, role="logo", source="html_link", context="head icon")
        elif tag == "img":
            src = _src_from_attrs(attr_map)
            if src:
                style_width, style_height = _style_dimensions(attr_map.get("style", ""))
                alt = attr_map.get("alt", "")
                context = self._current_context(extra=_frame_context(tag, attr_map))
                self._append(
                    src,
                    role=_role_from_context(src, alt, context),
                    alt=alt,
                    width=_dimension(attr_map.get("width")) or style_width,
                    height=_dimension(attr_map.get("height")) or style_height,
                    source="html_img",
                    context=context,
                    section=self._current_section(),
                )

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_tag = tag
            self._heading_parts = []
        if tag not in _VOID_TAGS:
            self._stack.append({"tag": tag, "context": _frame_context(tag, attr_map)})

    def handle_data(self, data: str) -> None:
        if self._heading_tag and data.strip():
            self._heading_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == self._heading_tag:
            self._last_heading = " ".join(self._heading_parts).strip()[:160]
            self._heading_tag = ""
            self._heading_parts = []
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] == tag:
                del self._stack[index:]
                break

    def _append(
        self,
        raw: str,
        *,
        role: str,
        source: str,
        alt: str = "",
        width: int | None = None,
        height: int | None = None,
        context: str = "",
        section: str = "",
    ) -> None:
        self.candidates.append(
            {
                "raw": raw,
                "role": role,
                "alt": alt,
                "width": width,
                "height": height,
                "source": source,
                "context": context,
                "section": section,
                "index": self._index,
            }
        )
        self._index += 1

    def _current_context(self, *, extra: str = "") -> str:
        frames = [frame["context"] for frame in self._stack[-6:]]
        return " ".join(part for part in (*frames, self._last_heading, extra) if part).strip()[:600]

    def _current_section(self) -> str:
        for frame in reversed(self._stack):
            if frame["tag"] in _SECTION_TAGS:
                return f"{frame['context']} {self._last_heading}".strip()[:240]
        return self._last_heading[:240]


def _structured_image_candidate(item: object, index: int) -> dict | None:
    if isinstance(item, str):
        return {
            "raw": item,
            "role": _role_from_context(item, "", ""),
            "alt": "",
            "width": None,
            "height": None,
            "source": "images",
            "context": "",
            "section": "",
            "index": index,
        }
    if not isinstance(item, dict):
        return None
    raw = item.get("url") or item.get("src")
    if not raw:
        return None
    alt = str(item.get("alt") or "")
    context = " ".join(
        str(item.get(key) or "")
        for key in ("context", "section", "class", "id", "role_hint")
        if item.get(key)
    ).strip()
    default_role = str(item.get("role") or item.get("role_hint") or "content")
    if default_role not in _ROLE_PRIORITY:
        default_role = "content"
    return {
        "raw": str(raw),
        "role": _role_from_context(str(raw), alt, context, default=default_role),
        "alt": alt,
        "width": _dimension(item.get("width")),
        "height": _dimension(item.get("height")),
        "source": str(item.get("source") or "images"),
        "context": context[:600],
        "section": str(item.get("section") or context)[:240],
        "index": index,
    }


def _raw_candidates(web_payload: dict) -> list[dict]:
    candidates: list[dict] = []
    html = str(web_payload.get("html") or "")
    if html:
        parser = _ImageTagParser()
        try:
            parser.feed(html)
        except Exception:
            _LOG.debug("Moodboard HTML parse failed; using other persisted inputs", exc_info=True)
        candidates.extend(parser.candidates)

    image_items = web_payload.get("images") or []
    for index, item in enumerate(image_items, start=len(candidates)):
        candidate = _structured_image_candidate(item, index)
        if candidate:
            candidates.append(candidate)

    # Markdown remains a lower-authority compatibility source because it can
    # contain images from crawled subpages whose HTML was not retained. When
    # the same URL also exists in HTML, the richer DOM candidate wins.
    markdown = str(web_payload.get("markdown_content") or "")
    start_index = len(candidates)
    for offset, match in enumerate(_MARKDOWN_IMAGE_RE.finditer(markdown)):
        raw, alt = match.group(2), match.group(1)
        candidates.append(
            {
                "raw": raw,
                "role": _role_from_context(raw, alt, ""),
                "alt": alt,
                "width": None,
                "height": None,
                "source": "markdown",
                "context": "",
                "section": "",
                "index": start_index + offset,
            }
        )
    return candidates


def _brand_tokens(web_payload: dict) -> set[str]:
    values = [str(web_payload.get("brand_name") or "")]
    base_url = str(web_payload.get("canonical_url") or web_payload.get("url") or "")
    hostname = (urlparse(base_url).hostname or "").lower()
    if hostname:
        values.extend(hostname.split("."))
    tokens = {
        token
        for value in values
        for token in _TOKEN_RE.findall(value.lower())
        if len(token) >= 3 and token not in _GENERIC_BRAND_TOKENS
    }
    return tokens


def _text(candidate: dict) -> str:
    return " ".join(
        str(candidate.get(key) or "")
        for key in ("url", "raw", "alt", "context", "section")
    ).lower()


def _filename_tokens(url: str) -> list[str]:
    path = unquote(urlparse(url).path)
    stem = PurePosixPath(path).stem.lower()
    return _TOKEN_RE.findall(stem)


def _matches_brand(candidate: dict, brand_tokens: set[str]) -> bool:
    if not brand_tokens:
        return False
    haystack = set(_filename_tokens(str(candidate.get("url") or "")))
    haystack.update(_TOKEN_RE.findall(str(candidate.get("alt") or "").lower()))
    return bool(haystack & brand_tokens)


def _looks_like_logo(candidate: dict) -> bool:
    text = _text(candidate)
    if candidate.get("role") == "logo":
        return True
    if any(marker in text for marker in ("logo", "wordmark", "brandmark")):
        return True
    width, height = candidate.get("width"), candidate.get("height")
    if width and height and height <= 180 and width / height >= 1.8:
        return True
    path = urlparse(str(candidate.get("url") or "")).path.lower()
    return path.endswith(".svg") and any(marker in text for marker in _PARTNER_CONTEXT_MARKERS)


def _has_variant_marker(candidate: dict) -> bool:
    return bool(set(_filename_tokens(str(candidate.get("url") or ""))) & _VARIANT_TOKENS)


def _is_opaque_asset(candidate: dict) -> bool:
    path = unquote(urlparse(str(candidate.get("url") or "")).path)
    name = PurePosixPath(path).name.lower()
    if not name or "." in name:
        return False
    if name.isdigit() and len(name) <= 3:
        return True
    return bool(re.fullmatch(r"[a-f0-9]{6,}", name))


def _pre_rejection_reason(candidate: dict, brand_tokens: set[str]) -> str:
    text = _text(candidate)
    semantic_context = " ".join(
        str(candidate.get(key) or "")
        for key in ("alt", "context", "section")
    ).lower()
    path = urlparse(str(candidate.get("url") or "")).path.lower()
    if path.endswith(".ico"):
        return "ui_icon"
    if any(marker in text for marker in _TRACKING_MARKERS):
        return "tracking_asset"
    if any(marker in text for marker in _PLACEHOLDER_MARKERS):
        return "placeholder"
    if any(marker in text for marker in _LANGUAGE_MARKERS):
        return "language_flag"
    if re.search(r"(?:^|[/_.-])flags?(?:[/_.-]|$)", text):
        return "language_flag"
    if any(marker in text for marker in _UI_ICON_MARKERS):
        return "ui_icon"

    width, height = candidate.get("width"), candidate.get("height")
    if width and height:
        if width <= 2 or height <= 2:
            return "tracking_asset"
        if max(width, height) <= 48 and not candidate.get("verified_brand_logo"):
            return "ui_icon"
        ratio = max(width / height, height / width)
        if ratio >= 6 and not candidate.get("verified_brand_logo") and not _looks_like_logo(candidate):
            return "extreme_aspect"

    partner_context = any(marker in semantic_context for marker in _PARTNER_CONTEXT_MARKERS)
    if partner_context and _looks_like_logo(candidate) and not candidate.get("verified_brand_logo"):
        return "partner_logo"

    in_owned_logo_context = any(
        marker in str(candidate.get("context") or "").lower()
        for marker in ("header", " nav", "navbar", "site-header", "site_header")
    )
    if (
        _looks_like_logo(candidate)
        and not candidate.get("verified_brand_logo")
        and not in_owned_logo_context
        and not _matches_brand(candidate, brand_tokens)
        and ("logo" in text or _has_variant_marker(candidate))
    ):
        return "third_party_logo"

    if (
        _is_opaque_asset(candidate)
        and candidate.get("role") not in {"social_card", "hero"}
        and not any(marker in text for marker in _POSITIVE_CONTEXT_MARKERS)
    ):
        return "opaque_asset"
    return ""


def _score(candidate: dict, brand_tokens: set[str]) -> int:
    role = str(candidate.get("role") or "content")
    score = {
        "social_card": 92,
        "logo": 68,
        "hero": 84,
        "content": 45,
    }.get(role, 40)
    source = str(candidate.get("source") or "")
    if source == "html_img":
        score += 5
    elif source == "images":
        score += 4
    elif source == "markdown":
        score -= 2
    if candidate.get("verified_brand_logo"):
        score = 120

    text = _text(candidate)
    if any(marker in text for marker in _POSITIVE_CONTEXT_MARKERS):
        score += 14
    if _matches_brand(candidate, brand_tokens):
        score += 8
    alt = str(candidate.get("alt") or "").strip().lower()
    if alt and alt not in {"image", "img", "photo", "logo"}:
        score += 5

    width, height = candidate.get("width"), candidate.get("height")
    if width and height:
        area = width * height
        if area >= 720_000:
            score += 14
        elif area >= 180_000:
            score += 8
        elif area < 8_000:
            score -= 12

    if "footer" in text:
        score -= 28
    if any(marker in text for marker in ("feature-icon", "feature_icon", "service-icon", "service_icon")):
        score -= 14
    return score


def _section_key(candidate: dict) -> str:
    value = str(candidate.get("section") or "").strip().lower()
    return re.sub(r"\s+", " ", value)[:200]


def _visual_family_key(candidate: dict) -> str:
    path = unquote(urlparse(str(candidate.get("url") or "")).path)
    stem = PurePosixPath(path).stem.lower()
    tokens = [
        token
        for token in _TOKEN_RE.findall(stem)
        if token not in _VARIANT_TOKENS
        and token not in {"logo", "wordmark", "brandmark", "png", "jpg", "jpeg", "svg", "webp", "avif"}
        and not re.fullmatch(r"\d{2,4}x\d{2,4}", token)
    ]
    return "-".join(tokens) if tokens else stem


def _public_image(candidate: dict, *, selection_reason: str) -> dict:
    image = {
        "url": candidate["url"],
        "role": candidate["role"],
        "alt": str(candidate.get("alt") or "").strip()[:160],
        "host": urlparse(candidate["url"]).netloc,
        "source": str(candidate.get("source") or ""),
        "selection_reason": selection_reason,
        "score": int(candidate.get("score") or 0),
    }
    if candidate.get("width"):
        image["width"] = candidate["width"]
    if candidate.get("height"):
        image["height"] = candidate["height"]
    return image


def _public_rejection(candidate: dict, reason: str) -> dict:
    raw = str(candidate.get("url") or candidate.get("raw") or "")
    return {
        "url": raw,
        "role": str(candidate.get("role") or "content"),
        "source": str(candidate.get("source") or ""),
        "reason": reason,
    }


def _selection_reason(candidate: dict) -> str:
    if candidate.get("verified_brand_logo"):
        return "verified_brand_logo"
    return {
        "social_card": "social_metadata",
        "logo": "owned_logo_candidate",
        "hero": "hero_context",
    }.get(str(candidate.get("role") or ""), "ranked_brand_content")


def _candidate_richness(candidate: dict) -> tuple[int, int, int, int]:
    source = str(candidate.get("source") or "")
    return (
        1 if candidate.get("verified_brand_logo") else 0,
        1 if source != "markdown" else 0,
        1 if candidate.get("context") or candidate.get("section") else 0,
        int(bool(candidate.get("width"))) + int(bool(candidate.get("height"))),
    )


def _merge_same_url_candidates(candidates: list[dict]) -> dict:
    ordered = sorted(
        candidates,
        key=lambda item: (
            _candidate_richness(item),
            -int(item.get("index") or 0),
        ),
        reverse=True,
    )
    merged = dict(ordered[0])
    merged["index"] = min(int(item.get("index") or 0) for item in candidates)
    if any(item.get("verified_brand_logo") for item in candidates):
        merged["verified_brand_logo"] = True
        merged["role"] = "logo"
    else:
        merged["role"] = min(
            (str(item.get("role") or "content") for item in candidates),
            key=lambda role: _ROLE_PRIORITY.get(role, 9),
        )
    for field in ("alt", "context", "section", "width", "height"):
        if not merged.get(field):
            merged[field] = next((item.get(field) for item in ordered if item.get(field)), merged.get(field))
    return merged


def _filtered_selection(web_payload: dict, *, brand_logo_url: str | None = None) -> dict:
    base_url = str(web_payload.get("canonical_url") or web_payload.get("url") or "")
    candidates = _raw_candidates(web_payload)
    if brand_logo_url:
        candidates.append(
            {
                "raw": brand_logo_url,
                "role": "logo",
                "alt": str(web_payload.get("brand_name") or ""),
                "width": None,
                "height": None,
                "source": "visual_signature",
                "context": "verified brand logo header nav",
                "section": "header",
                "index": -1,
                "verified_brand_logo": True,
            }
        )

    brand_tokens = _brand_tokens(web_payload)
    rejected: list[dict] = []
    by_url: dict[str, list[dict]] = defaultdict(list)
    for candidate in candidates:
        url = _clean_url(base_url, str(candidate.get("raw") or ""))
        if not url:
            rejected.append(_public_rejection(candidate, "invalid_url"))
            continue
        normalized = dict(candidate)
        normalized["url"] = url
        by_url[url.split("?", 1)[0]].append(normalized)

    eligible: list[dict] = []
    for same_url_candidates in by_url.values():
        normalized = _merge_same_url_candidates(same_url_candidates)
        reason = _pre_rejection_reason(normalized, brand_tokens)
        if reason:
            rejected.append(_public_rejection(normalized, reason))
            continue
        normalized["score"] = _score(normalized, brand_tokens)
        eligible.append(normalized)

    # A repeated group of small/vector/logo-like assets in one DOM section is
    # a logo cloud or feature-icon grid, not representative moodboard content.
    groups: dict[str, list[dict]] = defaultdict(list)
    for candidate in eligible:
        section = _section_key(candidate)
        if section:
            groups[section].append(candidate)
    grid_ids: set[int] = set()
    for group in groups.values():
        logo_like = [item for item in group if _looks_like_logo(item)]
        if len(group) >= 3 and len(logo_like) >= 3:
            grid_ids.update(id(item) for item in logo_like if not item.get("verified_brand_logo"))
    if grid_ids:
        retained = []
        for candidate in eligible:
            if id(candidate) in grid_ids:
                rejected.append(_public_rejection(candidate, "logo_grid"))
            else:
                retained.append(candidate)
        eligible = retained

    eligible.sort(key=lambda item: (-item["score"], item.get("index", 0), item["url"]))
    selected: list[dict] = []
    family_seen: set[str] = set()
    section_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    for candidate in eligible:
        if candidate["score"] < 48:
            rejected.append(_public_rejection(candidate, "low_relevance"))
            continue
        family = (
            _visual_family_key(candidate)
            if _has_variant_marker(candidate) or _looks_like_logo(candidate)
            else ""
        )
        if family and family in family_seen:
            rejected.append(_public_rejection(candidate, "duplicate_variant"))
            continue
        role = str(candidate.get("role") or "content")
        if role == "logo" and role_counts["logo"] >= 1:
            rejected.append(_public_rejection(candidate, "role_limit"))
            continue
        if role == "social_card" and role_counts["social_card"] >= 1:
            rejected.append(_public_rejection(candidate, "role_limit"))
            continue
        section = _section_key(candidate)
        if section and section_counts[section] >= 2:
            rejected.append(_public_rejection(candidate, "section_limit"))
            continue

        selected.append(_public_image(candidate, selection_reason=_selection_reason(candidate)))
        if family:
            family_seen.add(family)
        role_counts[role] += 1
        if section:
            section_counts[section] += 1
        if len(selected) >= MAX_MOODBOARD_IMAGES:
            break

    selected_urls = {item["url"] for item in selected}
    rejected_urls = {item["url"] for item in rejected}
    for candidate in eligible:
        if candidate["url"] not in selected_urls and candidate["url"] not in rejected_urls:
            rejected.append(_public_rejection(candidate, "selection_limit"))

    return {
        "images": selected,
        "rejected_images": rejected,
        "selection_version": SELECTION_VERSION,
        "selection_mode": "filtered",
    }


def _legacy_extract_moodboard_images(web_payload: dict) -> list[dict]:
    base_url = str(web_payload.get("canonical_url") or web_payload.get("url") or "")
    candidates = _raw_candidates(web_payload)
    images: list[dict] = []
    seen: set[str] = set()
    candidates.sort(key=lambda item: _ROLE_PRIORITY.get(item["role"], 9))
    for item in candidates:
        url = _clean_url(base_url, str(item.get("raw") or ""))
        lowered = str(url or "").lower()
        if (
            not url
            or lowered.endswith(".ico")
            or any(marker in lowered for marker in _TRACKING_MARKERS)
            or url in seen
        ):
            continue
        seen.add(url)
        images.append(
            {
                "url": url,
                "role": item["role"],
                "alt": str(item.get("alt") or "").strip()[:160],
                "host": urlparse(url).netloc,
                "source": str(item.get("source") or ""),
                "selection_reason": "legacy_order",
                "score": 0,
            }
        )
        if len(images) >= LEGACY_MAX_MOODBOARD_IMAGES:
            break
    return images


def select_moodboard_images(
    web_payload: dict | None,
    *,
    brand_logo_url: str | None = None,
    mode: str | None = None,
) -> dict:
    """Select display assets and return auditable rejection diagnostics."""
    payload = web_payload if isinstance(web_payload, dict) else {}
    selection_mode = _configured_selection_mode(mode)
    if selection_mode == "legacy":
        images = _legacy_extract_moodboard_images(payload)
        logo_url = _clean_url("", brand_logo_url or "")
        if logo_url and all(item["url"] != logo_url for item in images):
            images.insert(
                0,
                {
                    "url": logo_url,
                    "role": "logo",
                    "alt": str(payload.get("brand_name") or "")[:160],
                    "host": urlparse(logo_url).netloc,
                    "source": "visual_signature",
                    "selection_reason": "verified_brand_logo",
                    "score": 0,
                },
            )
            images = images[:LEGACY_MAX_MOODBOARD_IMAGES]
        return {
            "images": images,
            "rejected_images": [],
            "selection_version": "legacy",
            "selection_mode": "legacy",
        }
    return _filtered_selection(payload, brand_logo_url=brand_logo_url)


def extract_moodboard_images(web_payload: dict | None) -> list[dict]:
    """Backwards-compatible public helper returning only selected images."""
    return select_moodboard_images(web_payload)["images"]


def _block_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, (list, tuple)):
        return " · ".join(str(item) for item in content if str(item).strip())
    return str(content or "").strip()


def build_moodboard_model(
    scan_payload: dict,
    web_payload: dict | None,
    *,
    brand_logo_url: str | None = None,
    selection_mode: str | None = None,
) -> dict:
    """Assemble the moodboard view model from persisted data only."""
    selection = select_moodboard_images(
        web_payload,
        brand_logo_url=brand_logo_url,
        mode=selection_mode,
    )
    images = selection["images"]
    rejected_images = selection["rejected_images"]

    tldr = scan_payload.get("tldr_brand3") or {}
    visual_reading = []
    for key in VISUAL_READING_BLOCKS:
        block = tldr.get(key) or {}
        if not block.get("detected"):
            continue
        text = _block_text(block)
        if text:
            visual_reading.append({"key": key, "text": text})

    role_counts: dict[str, int] = {}
    for item in images:
        role_counts[item["role"]] = role_counts.get(item["role"], 0) + 1
    rejection_counts = dict(sorted(Counter(item["reason"] for item in rejected_images).items()))
    logo_image = next((item for item in images if item["role"] == "logo"), None)

    return {
        "available": bool(images),
        "images": images,
        "logo_image": logo_image,
        "visual_reading": visual_reading,
        "role_counts": role_counts,
        "image_count": len(images),
        "rejected_count": len(rejected_images),
        "rejection_counts": rejection_counts,
        "selection_version": selection["selection_version"],
        "selection_mode": selection["selection_mode"],
        "runtime_effect": False,
        "page_url": str((web_payload or {}).get("url") or scan_payload.get("url") or ""),
    }
