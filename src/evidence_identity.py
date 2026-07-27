"""Content-addressed identity primitives shared by evidence pipelines."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

EVIDENCE_IDENTITY_VERSION = "sv9-flow-evidence-identity-v1"
EVIDENCE_ALIAS_PREFIX = "evidence:"
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}
_WHITESPACE_RE = re.compile(r"\s+")


def canonical_evidence_material(
    *,
    source_class: Any,
    evidence_type: Any,
    url: Any,
    content: Any,
) -> dict[str, str]:
    return {
        "version": EVIDENCE_IDENTITY_VERSION,
        "source_class": str(source_class or "").strip().lower(),
        "evidence_type": str(evidence_type or "").strip().lower(),
        "url": normalize_evidence_url(url),
        "content": normalize_evidence_text(content).casefold(),
    }


def canonical_evidence_digest(
    *,
    source_class: Any,
    evidence_type: Any,
    url: Any,
    content: Any,
) -> str:
    payload = canonical_evidence_material(
        source_class=source_class,
        evidence_type=evidence_type,
        url=url,
        content=content,
    )
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def canonical_evidence_alias(
    *,
    source_class: Any,
    evidence_type: Any,
    url: Any,
    content: Any,
) -> str:
    digest = canonical_evidence_digest(
        source_class=source_class,
        evidence_type=evidence_type,
        url=url,
        content=content,
    )
    return f"{EVIDENCE_ALIAS_PREFIX}{digest}"


def normalize_evidence_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_evidence_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return text.rstrip("/").lower()
    if not parsed.netloc:
        return text.rstrip("/").lower()
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    return urlunparse((scheme, netloc, path, "", urlencode(sorted(query)), ""))


def stable_artifact_digest(namespace: str, payload: dict[str, Any]) -> str:
    envelope = {
        "namespace": str(namespace),
        "payload": payload,
    }
    rendered = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
