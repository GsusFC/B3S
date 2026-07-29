"""Reproducible negative-identity and entity-relation provenance.

Identity policy v4 distinguishes proof that a passage is about another entity
from the absence of proof that it is about the scanned entity. Persisted LLM
labels are never authoritative here; a negative decision requires inputs that
can be reproduced deterministically.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from src.external_identity_provenance import normalized_domain


ENTITY_CONFLICT_PROVENANCE_VERSION = "entity-conflict-provenance-v1"
ENTITY_CONFLICT_POLICY_VERSION = "entity-conflict-policy-v1"
ENTITY_RELATION_PROVENANCE_VERSION = "entity-relation-provenance-v1"
ENTITY_RELATION_POLICY_VERSION = "entity-relation-policy-v1"

ENTITY_CONFLICT_TYPES = frozenset(
    {
        "explicit_other_entity",
        "legal_entity_mismatch",
        "verified_domain_mismatch",
        "resolved_homonym",
        "subject_role_mismatch",
        "temporal_ownership_mismatch",
    }
)
ENTITY_RELATIONS = frozenset(
    {
        "same_entity",
        "parent",
        "subsidiary",
        "product",
        "related_party",
        "unrelated",
        "unknown",
    }
)
ENTITY_RELATION_STATUSES = frozenset(
    {"verified", "candidate", "none", "unknown"}
)
ENTITY_SUBJECT_ROLES = frozenset(
    {"scanned_entity", "related_entity", "unresolved"}
)

_AUTHORITATIVE_CONFLICT_STATUSES = {"explicit", "verified"}
_TEXT_METHODS = {"deterministic_text"}
_LOCATOR_METHODS = {"verified_domain", "verified_registry"}
_RELATED_RELATIONS = {
    "parent",
    "subsidiary",
    "product",
    "related_party",
}


def resolve_entity_conflict_provenance(
    value: Any,
    *,
    brand_name: str,
    brand_domain: str,
    source_domain: str,
    content: str,
) -> dict[str, Any]:
    """Resolve persisted or locally reproducible evidence of another entity."""

    persisted = _verify_persisted_conflict(
        value,
        brand_name=brand_name,
        brand_domain=brand_domain,
        source_domain=source_domain,
        content=content,
    )
    derived = _derive_text_conflict(
        brand_name=brand_name,
        content=content,
    )
    if persisted["authoritative"]:
        return persisted
    if derived["authoritative"]:
        return {
            **derived,
            "reasons": [
                *persisted["reasons"],
                *derived["reasons"],
            ],
        }
    return persisted


def resolve_entity_relation_provenance(
    value: Any,
    *,
    brand_name: str,
    brand_domain: str,
    source_domain: str,
    content: str,
) -> dict[str, Any]:
    """Resolve the relation and subject role without treating kinship as identity."""

    persisted = _verify_persisted_relation(
        value,
        brand_name=brand_name,
        brand_domain=brand_domain,
        source_domain=source_domain,
        content=content,
    )
    if persisted["status"] != "unknown":
        return persisted
    derived = _derive_relation_candidate(
        brand_name=brand_name,
        brand_domain=brand_domain,
        source_domain=source_domain,
        content=content,
    )
    if derived["status"] != "unknown":
        return {
            **derived,
            "reasons": [
                *persisted["reasons"],
                *derived["reasons"],
            ],
        }
    return persisted


def project_entity_conflict_provenance(value: Any) -> dict[str, Any]:
    """Expose audit fields without returning raw evidence spans."""

    if not isinstance(value, dict):
        return {}
    spans = _evidence_spans(value)
    projected = {
        field: value.get(field)
        for field in (
            "schema_version",
            "policy_version",
            "status",
            "conflict_type",
            "detection_method",
            "alternative_entity_locator",
            "reproducible",
        )
        if field in value
    }
    if spans:
        projected["evidence_span_count"] = len(spans)
        projected["evidence_span_digests"] = [
            _span_digest(span) for span in spans
        ]
    return projected


def project_entity_relation_provenance(value: Any) -> dict[str, Any]:
    """Expose relation audit fields without returning raw evidence spans."""

    if not isinstance(value, dict):
        return {}
    spans = _evidence_spans(value)
    projected = {
        field: value.get(field)
        for field in (
            "schema_version",
            "policy_version",
            "status",
            "relation",
            "subject_role",
            "source_domain",
            "subject_domain",
            "verification_method",
            "evidence_locator",
            "reproducible",
        )
        if field in value
    }
    if spans:
        projected["evidence_span_count"] = len(spans)
        projected["evidence_span_digests"] = [
            _span_digest(span) for span in spans
        ]
    return projected


def _verify_persisted_conflict(
    value: Any,
    *,
    brand_name: str,
    brand_domain: str,
    source_domain: str,
    content: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _conflict_result(
            reasons=["entity_conflict_provenance_missing"],
        )

    reasons: list[str] = []
    if value.get("schema_version") != ENTITY_CONFLICT_PROVENANCE_VERSION:
        reasons.append("entity_conflict_provenance_version_invalid")
    if value.get("policy_version") != ENTITY_CONFLICT_POLICY_VERSION:
        reasons.append("entity_conflict_policy_version_invalid")
    status = str(value.get("status") or "").strip().lower()
    if status not in _AUTHORITATIVE_CONFLICT_STATUSES:
        reasons.append("entity_conflict_status_not_authoritative")
    conflict_type = str(value.get("conflict_type") or "").strip().lower()
    if conflict_type not in ENTITY_CONFLICT_TYPES:
        reasons.append("entity_conflict_type_invalid")
    if value.get("reproducible") is not True:
        reasons.append("entity_conflict_not_marked_reproducible")

    method = str(value.get("detection_method") or "").strip().lower()
    if method.startswith("llm") or method in {"model", "classifier"}:
        reasons.append("llm_entity_conflict_not_authoritative")
    elif method in _TEXT_METHODS:
        reasons.extend(_verify_spans(value, content=content))
        derived = _derive_text_conflict(
            brand_name=brand_name,
            content=content,
        )
        if not derived["authoritative"]:
            reasons.append("entity_conflict_text_rule_not_reproduced")
        elif derived["conflict_type"] != conflict_type:
            reasons.append("entity_conflict_type_not_reproduced")
    elif method in _LOCATOR_METHODS:
        if not str(value.get("alternative_entity_locator") or "").strip():
            reasons.append("entity_conflict_alternative_locator_missing")
        reasons.append("entity_conflict_external_verifier_unavailable")
    else:
        reasons.append("entity_conflict_detection_method_not_reproducible")

    if reasons:
        return _conflict_result(
            conflict_type=conflict_type or "unknown",
            provenance=project_entity_conflict_provenance(value),
            reasons=reasons,
        )
    return _conflict_result(
        authoritative=True,
        conflict_type=conflict_type,
        provenance=project_entity_conflict_provenance(value),
        reasons=["entity_conflict_provenance_reproduced"],
    )


def _derive_text_conflict(
    *,
    brand_name: str,
    content: str,
) -> dict[str, Any]:
    patterns: list[tuple[str, str]] = [
        (
            "temporal_ownership_mismatch",
            (
                r"\b(?:the\s+)?previous\s+(?:operator|owner)\s+"
                r"no\s+longer\s+(?:controls?|owns?)\b"
            ),
        ),
        (
            "temporal_ownership_mismatch",
            (
                r"\b(?:el\s+)?(?:operador|propietario)\s+anterior\s+"
                r"ya\s+no\s+(?:controla|posee)\b"
            ),
        ),
    ]
    compact_brand = str(brand_name or "").strip()
    if compact_brand:
        escaped_brand = re.escape(compact_brand)
        patterns.extend(
            [
                (
                    "resolved_homonym",
                    (
                        r"\b(?:a|the)\s+local\s+"
                        r"(?:company|business|entity|organisation|organization)\s+"
                        rf"(?:called|named)\s+{escaped_brand}\b"
                        r".{0,80}\b(?:physical\s+shop|physical\s+store)\b"
                    ),
                ),
                (
                    "resolved_homonym",
                    (
                        r"\b(?:una|la)\s+(?:empresa|entidad)\s+local\s+"
                        rf"(?:llamada|denominada)\s+{escaped_brand}\b"
                        r".{0,80}\b(?:tienda|local)\s+fisic[oa]\b"
                    ),
                ),
            ]
        )
    patterns.extend(
        [
            (
                "explicit_other_entity",
                (
                    r"\b(?:a|an|the)\s+(?:different|unrelated)\s+"
                    r"(?:company|business|entity|organisation|organization)\b"
                ),
            ),
            (
                "explicit_other_entity",
                (
                    r"\b(?:otra\s+(?:empresa|entidad|organizacion|organización)"
                    r"|una\s+(?:empresa|entidad|organizacion|organización)"
                    r"\s+(?:diferente|distinta|no\s+relacionada))\b"
                ),
            ),
            (
                "explicit_other_entity",
                r"\bdiscusses?\s+(?:an?\s+)?different\b.{0,40}\bentity\b",
            ),
        ]
    )
    for conflict_type, pattern in patterns:
        match = re.search(pattern, str(content or ""), flags=re.IGNORECASE)
        if match is None:
            continue
        provenance = {
            "schema_version": ENTITY_CONFLICT_PROVENANCE_VERSION,
            "policy_version": ENTITY_CONFLICT_POLICY_VERSION,
            "status": "explicit",
            "conflict_type": conflict_type,
            "detection_method": "deterministic_text",
            "evidence_spans": [match.group(0)],
            "alternative_entity_locator": None,
            "reproducible": True,
        }
        return _conflict_result(
            authoritative=True,
            conflict_type=conflict_type,
            provenance=project_entity_conflict_provenance(provenance),
            reasons=["entity_conflict_derived_from_deterministic_text"],
        )
    return _conflict_result(
        reasons=["deterministic_entity_conflict_not_found"],
    )


def _verify_persisted_relation(
    value: Any,
    *,
    brand_name: str,
    brand_domain: str,
    source_domain: str,
    content: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _relation_result(
            reasons=["entity_relation_provenance_missing"],
        )

    reasons: list[str] = []
    if value.get("schema_version") != ENTITY_RELATION_PROVENANCE_VERSION:
        reasons.append("entity_relation_provenance_version_invalid")
    if value.get("policy_version") != ENTITY_RELATION_POLICY_VERSION:
        reasons.append("entity_relation_policy_version_invalid")
    status = str(value.get("status") or "").strip().lower()
    if status not in ENTITY_RELATION_STATUSES:
        reasons.append("entity_relation_status_invalid")
        status = "unknown"
    relation = str(value.get("relation") or "").strip().lower()
    if relation not in ENTITY_RELATIONS:
        reasons.append("entity_relation_invalid")
        relation = "unknown"
    role = str(value.get("subject_role") or "").strip().lower()
    if role not in ENTITY_SUBJECT_ROLES:
        reasons.append("entity_subject_role_invalid")
        role = "unresolved"

    persisted_source = normalized_domain(str(value.get("source_domain") or ""))
    expected_source = normalized_domain(source_domain)
    if not persisted_source or persisted_source != expected_source:
        reasons.append("entity_relation_source_domain_mismatch")
    persisted_subject = normalized_domain(str(value.get("subject_domain") or ""))
    expected_subject = normalized_domain(brand_domain)
    if not persisted_subject or persisted_subject != expected_subject:
        reasons.append("entity_relation_subject_domain_mismatch")

    authoritative = False
    if status == "verified":
        if value.get("reproducible") is not True:
            reasons.append("entity_relation_not_marked_reproducible")
        method = str(value.get("verification_method") or "").strip().lower()
        if method in _TEXT_METHODS:
            reasons.extend(_verify_spans(value, content=content))
            derived = _derive_relation_candidate(
                brand_name=brand_name,
                brand_domain=brand_domain,
                source_domain=source_domain,
                content=content,
            )
            if derived["status"] != "candidate":
                reasons.append("entity_relation_text_rule_not_reproduced")
            elif derived["relation"] != relation:
                reasons.append("entity_relation_type_not_reproduced")
            elif derived["subject_role"] != role:
                reasons.append("entity_relation_subject_role_not_reproduced")
        elif method in _LOCATOR_METHODS:
            locator = str(value.get("evidence_locator") or "").strip()
            if not locator:
                reasons.append("entity_relation_evidence_locator_missing")
            reasons.append("entity_relation_external_verifier_unavailable")
        else:
            reasons.append("entity_relation_verification_method_not_reproducible")
        authoritative = not reasons

    if reasons:
        return _relation_result(
            status="unknown",
            relation="unknown",
            subject_role="unresolved",
            provenance=project_entity_relation_provenance(value),
            reasons=reasons,
        )
    if status == "candidate":
        return _relation_result(
            status=status,
            relation=relation,
            subject_role=role,
            provenance=project_entity_relation_provenance(value),
            reasons=["entity_relation_candidate_persisted"],
        )
    if status in {"none", "unknown"}:
        return _relation_result(
            status=status,
            relation=relation,
            subject_role=role,
            provenance=project_entity_relation_provenance(value),
            reasons=["entity_relation_not_resolved"],
        )
    return _relation_result(
        authoritative=authoritative,
        status=status,
        relation=relation,
        subject_role=role,
        provenance=project_entity_relation_provenance(value),
        reasons=["entity_relation_provenance_reproduced"],
    )


def _derive_relation_candidate(
    *,
    brand_name: str,
    brand_domain: str,
    source_domain: str,
    content: str,
) -> dict[str, Any]:
    text = str(content or "")
    folded = text.casefold()
    brand = str(brand_name or "").strip().casefold()
    if not brand:
        subject_label = normalized_domain(brand_domain).split(".", 1)[0]
        brand = subject_label.replace("-", " ")
    has_brand = bool(brand and brand in folded)
    parent_language = bool(
        re.search(
            r"\b(parent|matrix|matriz|holding|portfolio|cartera)\b",
            folded,
        )
    )
    product_language = bool(
        re.search(r"\b(product|producto|suite)\b", folded)
    )
    source_is_external = (
        normalized_domain(source_domain)
        and normalized_domain(source_domain) != normalized_domain(brand_domain)
    )
    relation = "unknown"
    role = "unresolved"
    if source_is_external and has_brand:
        escaped_brand = re.escape(brand)
        if re.search(
            rf"\b(?:not\s+affiliated\s+with|unrelated\s+to)\s+{escaped_brand}\b",
            folded,
        ):
            relation = "unrelated"
            role = "related_entity"
        elif product_language and re.search(
            rf"\b{escaped_brand}\s+is\s+(?:the|a|one)\s+product\b",
            folded,
        ):
            relation = "product"
            role = "scanned_entity"
        elif parent_language and re.search(
            rf"\b(?:owns?|acquired|controls?)\s+{escaped_brand}\b",
            folded,
        ):
            relation = "parent"
        elif parent_language and product_language:
            relation = "product"
    if relation != "unknown":
        provenance = {
            "schema_version": ENTITY_RELATION_PROVENANCE_VERSION,
            "policy_version": ENTITY_RELATION_POLICY_VERSION,
            "status": "candidate",
            "relation": relation,
            "subject_role": role,
            "source_domain": normalized_domain(source_domain),
            "subject_domain": normalized_domain(brand_domain),
            "reproducible": False,
        }
        return _relation_result(
            status="candidate",
            relation=relation,
            subject_role=role,
            provenance=project_entity_relation_provenance(provenance),
            reasons=["entity_relation_candidate_derived_from_text"],
        )
    return _relation_result(
        reasons=["entity_relation_not_established"],
    )


def _verify_spans(value: dict[str, Any], *, content: str) -> list[str]:
    spans = _evidence_spans(value)
    if not spans:
        return ["entity_provenance_evidence_spans_missing"]
    folded_content = str(content or "").casefold()
    if any(span.casefold() not in folded_content for span in spans):
        return ["entity_provenance_evidence_span_not_reproduced"]
    return []


def _evidence_spans(value: dict[str, Any]) -> list[str]:
    raw = value.get("evidence_spans")
    if not isinstance(raw, list):
        return []
    return [
        str(span).strip()
        for span in raw
        if str(span).strip()
    ]


def _span_digest(span: str) -> str:
    return hashlib.sha256(span.encode("utf-8")).hexdigest()


def _conflict_result(
    *,
    authoritative: bool = False,
    conflict_type: str = "unknown",
    provenance: dict[str, Any] | None = None,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "authoritative": authoritative,
        "conflict_type": conflict_type,
        "provenance": provenance or {},
        "reasons": reasons,
    }


def _relation_result(
    *,
    authoritative: bool = False,
    status: str = "unknown",
    relation: str = "unknown",
    subject_role: str = "unresolved",
    provenance: dict[str, Any] | None = None,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "authoritative": authoritative,
        "status": status,
        "relation": relation,
        "subject_role": subject_role,
        "provenance": provenance or {},
        "reasons": reasons,
    }


def related_entity_relation(relation: str) -> bool:
    """Return whether a verified relation denotes corporate/product kinship."""

    return str(relation or "").strip().lower() in _RELATED_RELATIONS
