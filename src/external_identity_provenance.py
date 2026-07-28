"""Reproducible provenance for external brand-identity attribution.

An upstream label such as ``identity_match=domain`` is not proof. This contract
keeps the exact deterministic inputs needed to reproduce why an external
result was attributed to the scanned brand.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any
from urllib.parse import urlparse


EXTERNAL_IDENTITY_PROVENANCE_VERSION = "external-identity-provenance-v1"
EXTERNAL_IDENTITY_POLICY_VERSION = "external-identity-policy-v1"
_STRONG_METHODS = {"alias_in_host", "alias_in_title"}
_MIN_STRONG_SCORE = 0.95


def build_external_identity_provenance(
    *,
    provider: str,
    subject_url: str,
    source_url: str,
    matched_alias: str,
    match_method: str,
    match_score: Any,
    collector_source_class: str,
    collector_relation: str,
    requires_human_review: bool,
) -> dict[str, Any]:
    """Build the serializable attribution contract stored with one result."""

    normalized_score = _score(match_score)
    method = str(match_method or "").strip().lower()
    alias = _compact_alias(matched_alias)
    return {
        "schema_version": EXTERNAL_IDENTITY_PROVENANCE_VERSION,
        "policy_version": EXTERNAL_IDENTITY_POLICY_VERSION,
        "provider": str(provider or "").strip().lower(),
        "subject_domain": normalized_domain(subject_url),
        "source_domain": normalized_domain(source_url),
        "matched_alias": alias,
        "match_method": method,
        "match_score": normalized_score,
        "collector_source_class": str(
            collector_source_class or ""
        ).strip().lower(),
        "collector_relation": str(collector_relation or "").strip().lower(),
        "requires_human_review": bool(requires_human_review),
        "candidate_strength": (
            "strong"
            if method in _STRONG_METHODS
            and _MIN_STRONG_SCORE <= normalized_score <= 1.0
            and bool(alias)
            else "weak"
        ),
    }


def verify_external_identity_provenance(
    value: Any,
    *,
    brand_domain: str,
    source_domain: str,
    content: str,
) -> tuple[bool, list[str]]:
    """Reproduce a strong external attribution from its persisted inputs."""

    if not isinstance(value, dict):
        return False, ["external_identity_provenance_missing"]
    reasons: list[str] = []
    if value.get("schema_version") != EXTERNAL_IDENTITY_PROVENANCE_VERSION:
        reasons.append("external_identity_provenance_version_invalid")
    if value.get("policy_version") != EXTERNAL_IDENTITY_POLICY_VERSION:
        reasons.append("external_identity_policy_version_invalid")
    if not str(value.get("provider") or "").strip():
        reasons.append("external_identity_provider_missing")
    subject = normalized_domain(str(value.get("subject_domain") or ""))
    expected_subject = normalized_domain(brand_domain)
    if not subject or subject != expected_subject:
        reasons.append("external_identity_subject_domain_mismatch")
    persisted_source = normalized_domain(str(value.get("source_domain") or ""))
    expected_source = normalized_domain(source_domain)
    if not persisted_source or persisted_source != expected_source:
        reasons.append("external_identity_source_domain_mismatch")
    if bool(value.get("requires_human_review")):
        reasons.append("external_identity_requires_human_review")
    if str(value.get("collector_relation") or "").strip().lower() != "external":
        reasons.append("external_identity_relation_not_external")
    collector_class = str(
        value.get("collector_source_class") or ""
    ).strip().lower()
    if collector_class not in {"external"}:
        reasons.append("external_identity_source_class_not_independent")

    method = str(value.get("match_method") or "").strip().lower()
    if method not in _STRONG_METHODS:
        reasons.append("external_identity_match_method_not_strong")
    score = _score(value.get("match_score"))
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        reasons.append("external_identity_match_score_invalid")
        score = 0.0
    if score < _MIN_STRONG_SCORE:
        reasons.append("external_identity_match_score_too_low")
    alias = _compact_alias(value.get("matched_alias"))
    if len(alias) < 4:
        reasons.append("external_identity_matched_alias_missing")
    elif method == "alias_in_host":
        if not _contains_alias(expected_source, alias):
            reasons.append("external_identity_alias_not_reproducible_in_host")
    elif method == "alias_in_title":
        if not _contains_alias(content, alias):
            reasons.append("external_identity_alias_not_reproducible_in_content")

    if reasons:
        return False, reasons
    return True, ["external_identity_provenance_reproduced"]


def normalized_domain(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    parsed = urlparse(
        candidate if "://" in candidate else f"https://{candidate}"
    )
    return str(parsed.hostname or "").strip(".").lower().removeprefix("www.")


def _compact_alias(value: Any) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "").casefold())
    ascii_text = "".join(
        char for char in folded if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]+", "", ascii_text)


def _contains_alias(value: Any, alias: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    if alias in tokens:
        return True
    for start in range(len(tokens)):
        compact = ""
        for token in tokens[start:]:
            compact += token
            if compact == alias:
                return True
            if len(compact) >= len(alias):
                break
    return False


def _score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) else 0.0
