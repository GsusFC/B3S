"""Two-axis shadow registry for publisher and claim-level source reviews.

The projection joins the immutable source-review and claim-corroboration
datasets without turning either dataset into runtime or scoring authority.
Publisher independence is source-scoped. Corroboration is always claim-scoped.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from src.evidence_identity import stable_artifact_digest
from src.services.evidence_claim_corroboration_review_set import (
    EvidenceClaimCorroborationReviewSetError,
    evaluate_claim_corroboration_review_set,
    load_review_candidates as load_claim_review_candidates,
    load_review_events as load_claim_review_events,
    load_review_manifest as load_claim_review_manifest,
)
from src.services.evidence_source_review_set import (
    EvidenceSourceReviewSetError,
    evaluate_source_review_set,
    load_review_candidates as load_source_review_candidates,
    load_review_events as load_source_review_events,
    load_review_manifest as load_source_review_manifest,
)


EVIDENCE_SOURCE_CLAIM_REGISTRY_SCHEMA_VERSION = (
    "evidence-source-claim-registry-v2"
)
EVIDENCE_SOURCE_CLAIM_REGISTRY_POLICY_VERSION = (
    "evidence-source-claim-registry-policy-v1"
)


class EvidenceSourceClaimRegistryError(ValueError):
    """The two-axis shadow registry cannot be reproduced safely."""


def build_evidence_source_claim_registry(
    *,
    source_candidates: Iterable[dict[str, Any]] | None = None,
    source_review_events: Iterable[dict[str, Any]] | None = None,
    source_manifest: dict[str, Any] | None = None,
    claim_candidates: Iterable[dict[str, Any]] | None = None,
    claim_review_events: Iterable[dict[str, Any]] | None = None,
    claim_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, non-authoritative two-axis registry."""

    try:
        source_rows = _rows_or_default(
            source_candidates,
            load_source_review_candidates,
        )
        source_events = _rows_or_default(
            source_review_events,
            load_source_review_events,
        )
        claim_rows = _rows_or_default(
            claim_candidates,
            load_claim_review_candidates,
        )
        claim_events = _rows_or_default(
            claim_review_events,
            load_claim_review_events,
        )
        source_result = evaluate_source_review_set(
            source_rows,
            source_events,
            manifest=dict(
                load_source_review_manifest()
                if source_manifest is None
                else source_manifest
            ),
        )
        claim_result = evaluate_claim_corroboration_review_set(
            claim_rows,
            claim_events,
            manifest=dict(
                load_claim_review_manifest()
                if claim_manifest is None
                else claim_manifest
            ),
        )
    except (
        EvidenceSourceReviewSetError,
        EvidenceClaimCorroborationReviewSetError,
    ) as exc:
        raise EvidenceSourceClaimRegistryError(
            "two-axis registry input dataset is invalid"
        ) from exc

    source_by_case = _unique_rows(source_rows, id_field="case_id")
    source_review_by_case = {
        str(row["case_id"]): dict(row)
        for row in source_result["evaluated"]
    }
    claim_review_by_case = {
        str(row["case_id"]): dict(row)
        for row in claim_result["evaluated"]
    }
    claim_count_by_source: Counter[str] = Counter(
        str(row["parent_source_case_id"]) for row in claim_rows
    )

    sources: list[dict[str, Any]] = []
    for case_id in sorted(source_by_case):
        candidate = source_by_case[case_id]
        review = source_review_by_case.get(case_id)
        source = dict(candidate["source"])
        sources.append(
            {
                "source_case_id": case_id,
                "brand_domain": str(candidate["brand"]["domain"]),
                "source_url": str(source["url"]),
                "publisher_domain": str(source["publisher_domain"]),
                "publisher_kind": str(source["publisher_kind"]),
                "review_status": "reviewed" if review else "pending",
                "identity_decision": (
                    str(review["identity_decision"]) if review else None
                ),
                "publisher_independence_decision": (
                    str(review["publisher_independence_decision"])
                    if review
                    else None
                ),
                "publisher_review_event_id": (
                    str(review["event_id"]) if review else None
                ),
                "publisher_reviewer_id": (
                    str(review["reviewer_id"]) if review else None
                ),
                "claim_record_count": int(
                    claim_count_by_source.get(case_id, 0)
                ),
                "source_global_claim_corroboration_decision": None,
                "runtime_effect": False,
                "authority": False,
            }
        )

    claims: list[dict[str, Any]] = []
    seen_source_claim_pairs: set[tuple[str, str]] = set()
    for candidate in sorted(
        claim_rows,
        key=lambda row: str(row["case_id"]),
    ):
        case_id = str(candidate["case_id"])
        parent_case_id = str(candidate["parent_source_case_id"])
        parent = source_by_case.get(parent_case_id)
        if parent is None:
            raise EvidenceSourceClaimRegistryError(
                f"claim case has unknown parent source: {case_id}"
            )
        source_url = str(candidate["source"]["url"])
        if source_url != str(parent["source"]["url"]):
            raise EvidenceSourceClaimRegistryError(
                f"claim/source URL mismatch: {case_id}"
            )
        if str(candidate["brand"]["domain"]) != str(
            parent["brand"]["domain"]
        ):
            raise EvidenceSourceClaimRegistryError(
                f"claim/source brand mismatch: {case_id}"
            )
        for field in ("publisher_domain", "publisher_kind"):
            if str(candidate["source"][field]) != str(
                parent["source"][field]
            ):
                raise EvidenceSourceClaimRegistryError(
                    f"claim/source publisher mismatch: {case_id}"
                )
        claim_id = str(candidate["claim"]["claim_id"]).strip()
        if not claim_id:
            raise EvidenceSourceClaimRegistryError(
                f"claim-scoped registry row requires claim_id: {case_id}"
            )
        source_claim_pair = (source_url, claim_id)
        if source_claim_pair in seen_source_claim_pairs:
            raise EvidenceSourceClaimRegistryError(
                f"duplicate source/claim registry row: {case_id}"
            )
        seen_source_claim_pairs.add(source_claim_pair)

        parent_review = source_review_by_case.get(parent_case_id)
        claim_review = claim_review_by_case.get(case_id)
        publisher_decision = (
            str(parent_review["publisher_independence_decision"])
            if parent_review
            else None
        )
        corroboration_decision = (
            str(claim_review["decision"]) if claim_review else None
        )
        _validate_axis_compatibility(
            case_id=case_id,
            publisher_decision=publisher_decision,
            corroboration_decision=corroboration_decision,
        )
        claims.append(
            {
                "claim_case_id": case_id,
                "parent_source_case_id": parent_case_id,
                "brand_domain": str(candidate["brand"]["domain"]),
                "source_url": source_url,
                "claim_id": claim_id,
                "canonical_claim": False,
                "publisher_independence_decision": publisher_decision,
                "claim_corroboration_review_status": (
                    "reviewed" if claim_review else "pending"
                ),
                "claim_corroboration_decision": corroboration_decision,
                "corroboration_bases": (
                    list(claim_review["corroboration_bases"])
                    if claim_review
                    else []
                ),
                "claim_review_event_id": (
                    str(claim_review["event_id"])
                    if claim_review
                    else None
                ),
                "claim_reviewer_id": (
                    str(claim_review["reviewer_id"])
                    if claim_review
                    else None
                ),
                "evidence_span_sha256": str(
                    candidate["evidence"]["evidence_span_sha256"]
                ),
                "runtime_effect": False,
                "authority": False,
            }
        )

    publisher_counts = Counter(
        str(row["publisher_independence_decision"])
        for row in sources
        if row["publisher_independence_decision"] is not None
    )
    claim_decision_counts = Counter(
        str(row["claim_corroboration_decision"])
        for row in claims
        if row["claim_corroboration_decision"] is not None
    )
    reviewed_claim_count = sum(
        row["claim_corroboration_review_status"] == "reviewed"
        for row in claims
    )
    promotion_blockers = set(claim_result["promotion_blockers"])
    promotion_blockers.discard(
        "operational_two_axis_contract_not_adopted"
    )
    promotion_blockers.add("shadow_contract_not_operationally_adopted")
    payload = {
        "schema_version": EVIDENCE_SOURCE_CLAIM_REGISTRY_SCHEMA_VERSION,
        "policy_version": EVIDENCE_SOURCE_CLAIM_REGISTRY_POLICY_VERSION,
        "source_review_dataset_version": source_result["dataset_version"],
        "source_review_fingerprint": source_result["dataset_fingerprint"],
        "source_review_event_fingerprint": source_result[
            "review_event_fingerprint"
        ],
        "claim_review_dataset_version": claim_result["dataset_version"],
        "claim_review_fingerprint": claim_result["dataset_fingerprint"],
        "claim_review_event_fingerprint": claim_result[
            "review_event_fingerprint"
        ],
        "runtime_effect": False,
        "authority": False,
        "shadow_contract_ready": True,
        "operational_adoption_ready": False,
        "promotion_ready": False,
        "policy": {
            "publisher_independence_scope": "source_url",
            "claim_corroboration_scope": "source_url_and_claim_id",
            "publisher_independence_implies_claim_corroboration": False,
            "source_global_claim_corroboration_allowed": False,
            "claim_id_required_for_corroboration": True,
            "automatic_decisions": False,
            "canonical_claim_selection": False,
        },
        "summary": {
            "source_count": len(sources),
            "publisher_reviewed_count": sum(
                row["review_status"] == "reviewed" for row in sources
            ),
            "publisher_decision_counts": dict(
                sorted(publisher_counts.items())
            ),
            "source_global_claim_corroboration_decision_count": 0,
            "claim_record_count": len(claims),
            "claim_source_count": len(
                {str(row["source_url"]) for row in claims}
            ),
            "claim_id_count": len(
                {str(row["claim_id"]) for row in claims}
            ),
            "claim_reviewed_count": reviewed_claim_count,
            "claim_pending_count": len(claims) - reviewed_claim_count,
            "claim_decision_counts": dict(
                sorted(claim_decision_counts.items())
            ),
        },
        "promotion_blockers": sorted(promotion_blockers),
        "sources": sources,
        "claims": claims,
    }
    return {
        **payload,
        "registry_fingerprint": stable_artifact_digest(
            EVIDENCE_SOURCE_CLAIM_REGISTRY_SCHEMA_VERSION,
            payload,
        ),
    }


def render_evidence_source_claim_registry_markdown(
    registry: dict[str, Any],
) -> str:
    """Render a concise audit view of the two-axis shadow registry."""

    summary = registry.get("summary") or {}
    lines = [
        "# Evidence source/claim registry v2",
        "",
        f"- Schema: `{registry.get('schema_version', 'unknown')}`",
        f"- Policy: `{registry.get('policy_version', 'unknown')}`",
        f"- Fingerprint: `{registry.get('registry_fingerprint', '')}`",
        f"- Runtime effect: `{str(bool(registry.get('runtime_effect'))).lower()}`",
        f"- Authority: `{str(bool(registry.get('authority'))).lower()}`",
        f"- Shadow contract ready: `{str(bool(registry.get('shadow_contract_ready'))).lower()}`",
        f"- Operational adoption ready: `{str(bool(registry.get('operational_adoption_ready'))).lower()}`",
        f"- Sources: `{summary.get('source_count', 0)}`",
        f"- Publisher reviewed: `{summary.get('publisher_reviewed_count', 0)}`",
        f"- Claim records: `{summary.get('claim_record_count', 0)}`",
        f"- Claim sources: `{summary.get('claim_source_count', 0)}`",
        f"- Claim IDs: `{summary.get('claim_id_count', 0)}`",
        f"- Claims reviewed: `{summary.get('claim_reviewed_count', 0)}`",
        f"- Claims pending: `{summary.get('claim_pending_count', 0)}`",
        f"- Source-global corroboration decisions: `{summary.get('source_global_claim_corroboration_decision_count', 0)}`",
        "",
        "## Promotion blockers",
        "",
    ]
    lines.extend(
        f"- `{blocker}`"
        for blocker in registry.get("promotion_blockers") or []
    )
    return "\n".join(lines) + "\n"


def _rows_or_default(
    rows: Iterable[dict[str, Any]] | None,
    loader: Any,
) -> list[dict[str, Any]]:
    if rows is None:
        return [dict(row) for row in loader()]
    return [dict(row) for row in rows]


def _unique_rows(
    rows: Iterable[dict[str, Any]],
    *,
    id_field: str,
) -> dict[str, dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        row_id = str(row.get(id_field) or "")
        if not row_id or row_id in unique:
            raise EvidenceSourceClaimRegistryError(
                f"registry input requires unique {id_field}"
            )
        unique[row_id] = row
    return unique


def _validate_axis_compatibility(
    *,
    case_id: str,
    publisher_decision: str | None,
    corroboration_decision: str | None,
) -> None:
    if corroboration_decision is None:
        return
    if (
        corroboration_decision == "independently_corroborated"
        and publisher_decision != "confirmed_independent"
    ):
        raise EvidenceSourceClaimRegistryError(
            f"independent claim requires independent publisher: {case_id}"
        )
    if (
        publisher_decision == "excluded"
        and corroboration_decision != "excluded"
    ):
        raise EvidenceSourceClaimRegistryError(
            f"excluded publisher requires excluded claim: {case_id}"
        )
