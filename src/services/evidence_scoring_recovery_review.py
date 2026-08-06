"""Human review gate for direct evidence-to-tile semantic relations.

The scoring-memory preview can prove that a quote is reproducible and belongs
to the scanned brand.  It does not prove that the quote satisfies the semantic
contract of an SV9 tile, whether the quote is present in the current scan or
would recover a historical tile.  This module freezes that last mapping as a
reviewable subject and resolves append-only review events without granting
runtime or scoring authority.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from src.evidence_identity import (
    normalize_evidence_text,
    stable_artifact_digest,
)
from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)
from src.services.evidence_scoring_memory_preview import (
    apply_recovery_review_gate,
    build_evidence_scoring_memory_preview,
)
from src.sv9.rubric import COMPONENTS


EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION = (
    "evidence-scoring-recovery-review-v1"
)
EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION = (
    "evidence-scoring-recovery-review-event-v1"
)
EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION = (
    "evidence-scoring-recovery-review-policy-v2"
)
REVIEW_DECISIONS = frozenset(
    {"accepted", "disputed", "rejected", "revoked"}
)
EVIDENCE_SCORING_REVIEWED_SHADOW_VERSION = (
    "evidence-scoring-reviewed-memory-shadow-v2"
)
EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_PACKET_VERSION = (
    "evidence-scoring-recovery-supplement-packet-v1"
)
EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_MANIFEST_VERSION = (
    "evidence-scoring-recovery-supplement-manifest-v1"
)
EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_EVIDENCE_VERSION = (
    "evidence-scoring-recovery-supplement-evidence-v1"
)
SCORING_RECOVERY_REVIEW_SUBJECT_TYPE = "scoring_recovery"


class EvidenceScoringRecoveryReviewError(ValueError):
    """Recovery candidates or review events violate the review contract."""


class EvidenceScoringRecoveryJournalError(RuntimeError):
    """Base error for the durable semantic-recovery review boundary."""


class EvidenceScoringRecoveryReviewUnavailableError(
    EvidenceScoringRecoveryJournalError
):
    """The durable semantic-recovery review journal is unavailable."""


class EvidenceScoringRecoveryReviewNotFoundError(
    EvidenceScoringRecoveryJournalError
):
    """The brand or semantic recovery subject does not exist."""


class EvidenceScoringRecoveryReviewInvalidTransitionError(
    EvidenceScoringRecoveryJournalError
):
    """The requested decision cannot follow the current journal event."""


class EvidenceScoringRecoveryReviewConflictError(
    EvidenceScoringRecoveryJournalError
):
    """An optimistic-concurrency or idempotency precondition failed."""

    def __init__(
        self,
        message: str,
        *,
        current_event_id: str | None = None,
        existing_event_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.current_event_id = current_event_id
        self.existing_event_id = existing_event_id


@dataclass(frozen=True, slots=True)
class EvidenceScoringRecoveryReviewCommand:
    """Validated command passed to the append-only PostgreSQL journal."""

    subject_id: str
    case_id: str
    decision: str
    expected_current_event_id: str | None
    reviewer: str
    reason_code: str
    rationale: str
    evaluator_version: str
    actor_id: str
    idempotency_key_hash: str
    request_fingerprint: str


def build_reviewed_scoring_memory_shadow(
    reports: Iterable[dict[str, Any]],
    *,
    lane: str = "history",
    evidence_adjudications: Iterable[dict[str, Any]] = (),
    recovery_review_events: Iterable[dict[str, Any]] = (),
    reviewed_claim_tile_memory: dict[str, Any] | None = None,
    claim_tile_ledger: dict[str, Any] | None = None,
    supplemental_candidate_packets: Iterable[dict[str, Any]] = (),
    ignore_stale_review_events: bool = False,
    review_events_are_current: bool = False,
) -> dict[str, Any]:
    """Build candidate and fail-closed reviewed scores from one report history."""

    if reviewed_claim_tile_memory is not None:
        if not isinstance(reviewed_claim_tile_memory, dict) or (
            reviewed_claim_tile_memory.get("runtime_effect") is not False
            or reviewed_claim_tile_memory.get("authority") is not False
            or reviewed_claim_tile_memory.get("automatic_scoring_effect")
            is not False
        ):
            raise EvidenceScoringRecoveryReviewError(
                "reviewed claim-to-tile memory must remain non-authoritative"
            )
    rows = [
        dict(report)
        for report in reports
        if isinstance(report, dict)
    ]
    preview = build_evidence_scoring_memory_preview(
        rows,
        mode="shadow",
        evidence_adjudications=evidence_adjudications,
    )
    base_candidates = build_recovery_review_candidates(
        [{"lane": lane, "preview": preview}],
        claim_tile_mappings=_latest_claim_tile_mappings(
            claim_tile_ledger
        ),
    )
    identity_projection = build_evidence_memory_identity_v2(
        rows,
        mode="shadow",
        adjudications=evidence_adjudications,
    )
    supplements = merge_recovery_review_supplements(
        base_candidates,
        supplemental_candidate_packets,
        brand_domain=str(preview.get("brand", {}).get("domain") or ""),
        rubric_version=str(preview.get("rubric_version") or ""),
        evidence_identity_state_fingerprint=str(
            identity_projection.get("state_fingerprint") or ""
        ),
        accepted_evidence_ids={
            str(entry.get("evidence_id") or "")
            for entry in identity_projection.get("entries") or []
            if isinstance(entry, dict)
            and entry.get("adjudication_state") == "accepted"
            and str(entry.get("evidence_id") or "")
        },
    )
    candidates = supplements["candidates"]
    stored_events = [
        dict(event)
        for event in recovery_review_events
        if isinstance(event, dict)
    ]
    candidate_fingerprints = {
        str(candidate["case_id"]): str(
            candidate["candidate_fingerprint"]
        )
        for candidate in candidates
    }
    stale_events: list[dict[str, Any]] = []
    applicable_events: list[dict[str, Any]] = []
    for event in stored_events:
        case_id = str(event.get("case_id") or "")
        expected = candidate_fingerprints.get(case_id)
        if (
            ignore_stale_review_events
            and (
                expected is None
                or str(event.get("candidate_fingerprint") or "")
                != expected
            )
        ):
            stale_events.append(event)
        else:
            applicable_events.append(event)
    review = evaluate_recovery_reviews(
        candidates,
        applicable_events,
        events_are_current=review_events_are_current,
    )
    review["journal"] = {
        "stored_event_count": len(stored_events),
        "applicable_event_count": len(applicable_events),
        "stale_event_count": len(stale_events),
        "stale_event_ids": sorted(
            str(
                event.get("event_id")
                or event.get("id")
                or ""
            )
            for event in stale_events
            if str(
                event.get("event_id")
                or event.get("id")
                or ""
            )
        ),
    }
    latest_report_id = str(preview.get("latest_report_id") or "")
    latest_report = next(
        (
            report
            for report in rows
            if str(report.get("id") or "") == latest_report_id
        ),
        None,
    )
    result = (
        apply_recovery_review_gate(
            preview,
            latest_report,
            accepted_tile_evidence_ids=review[
                "accepted_tile_evidence_ids"
            ],
            reviewed_claim_tile_mappings=(
                reviewed_claim_tile_memory.get("reviewed_mappings") or []
                if reviewed_claim_tile_memory is not None
                else []
            ),
        )
        if isinstance(latest_report, dict)
        else dict(preview)
    )
    result["recovery_review_candidates"] = candidates
    result["recovery_review"] = review
    result["recovery_review_supplements"] = supplements["summary"]
    result["reviewed_shadow_version"] = (
        EVIDENCE_SCORING_REVIEWED_SHADOW_VERSION
    )
    result["state_fingerprint"] = stable_artifact_digest(
        EVIDENCE_SCORING_REVIEWED_SHADOW_VERSION,
        {
            key: value
            for key, value in result.items()
            if key != "state_fingerprint"
        },
    )
    return result


def build_recovery_review_supplement_packet(
    *,
    brand_identity: str,
    rubric_version: str,
    base_candidates: Iterable[dict[str, Any]],
    evidence_identity_state_fingerprint: str,
    candidates: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Freeze additional direct evidence-to-tile subjects for exact review.

    Supplemental candidates use the existing semantic-review subject contract;
    this packet only makes mapper omissions durable and reproducible. It grants
    no authority and cannot affect scanner or production scoring.
    """

    domain = str(brand_identity or "").strip().lower()
    rubric = str(rubric_version or "").strip()
    identity_fingerprint = _sha256(
        evidence_identity_state_fingerprint,
        field="evidence_identity_state_fingerprint",
    )
    base_rows = [dict(row) for row in base_candidates]
    candidate_rows = [dict(row) for row in candidates]
    if not domain or not rubric or not candidate_rows:
        raise EvidenceScoringRecoveryReviewError(
            "supplement packet requires brand, rubric, and candidates"
        )
    evaluate_recovery_reviews(base_rows, [])
    evaluate_recovery_reviews(candidate_rows, [])
    if any(
        str(row.get("brand", {}).get("domain") or "").strip().lower()
        != domain
        or str(row.get("rubric_version") or "") != rubric
        for row in candidate_rows
    ):
        raise EvidenceScoringRecoveryReviewError(
            "supplement candidates do not match packet brand and rubric"
        )
    base_case_ids = {str(row["case_id"]) for row in base_rows}
    supplemental_case_ids = {str(row["case_id"]) for row in candidate_rows}
    collisions = sorted(base_case_ids & supplemental_case_ids)
    if collisions:
        raise EvidenceScoringRecoveryReviewError(
            "supplement duplicates base candidates: " + ", ".join(collisions)
        )
    ordered = sorted(candidate_rows, key=lambda row: str(row["case_id"]))
    manifest = {
        "schema_version": (
            EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_MANIFEST_VERSION
        ),
        "brand_identity": domain,
        "rubric_version": rubric,
        "base_candidate_set_fingerprint": recovery_candidate_fingerprint(
            base_rows
        ),
        "evidence_identity_state_fingerprint": identity_fingerprint,
        "supplemental_candidate_set_fingerprint": (
            recovery_candidate_fingerprint(ordered)
        ),
        "candidate_count": len(ordered),
        "authority": False,
        "runtime_effect": False,
        "automatic_scoring_effect": False,
        "scanner_runtime_effect": False,
        "production_runtime_effect": False,
    }
    packet_fingerprint = stable_artifact_digest(
        EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_PACKET_VERSION,
        {"manifest": manifest, "candidates": ordered},
    )
    return {
        "schema_version": EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_PACKET_VERSION,
        "packet_fingerprint": packet_fingerprint,
        "manifest": manifest,
        "candidates": ordered,
        "authority": False,
        "runtime_effect": False,
        "automatic_scoring_effect": False,
        "scanner_runtime_effect": False,
        "production_runtime_effect": False,
    }


def build_recovery_review_supplement_packet_from_preview(
    preview: dict[str, Any],
    *,
    source_catalog: dict[str, Any],
    proposals: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build an exact supplement from identity-accepted report passages."""

    base_candidates = _preview_recovery_candidates(preview)
    candidates = build_recovery_review_supplement_candidates(
        preview,
        source_catalog,
        proposals,
    )
    return build_recovery_review_supplement_packet(
        brand_identity=str(preview.get("brand", {}).get("domain") or ""),
        rubric_version=str(preview.get("rubric_version") or ""),
        base_candidates=base_candidates,
        evidence_identity_state_fingerprint=(
            source_catalog.get("identity_state_fingerprint")
        ),
        candidates=candidates,
    )


def build_recovery_review_supplement_candidates(
    preview: dict[str, Any],
    source_catalog: dict[str, Any],
    proposals: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project mapper omissions from exact accepted report passages."""

    if not isinstance(preview, dict) or (
        preview.get("runtime_effect") is not False
        or preview.get("authority") is not False
    ):
        raise EvidenceScoringRecoveryReviewError(
            "supplement generation requires a non-authoritative preview"
        )
    brand = (
        dict(preview.get("brand"))
        if isinstance(preview.get("brand"), dict)
        else {}
    )
    domain = str(brand.get("domain") or "").strip().lower()
    rubric = str(preview.get("rubric_version") or "").strip()
    if not domain or not rubric:
        raise EvidenceScoringRecoveryReviewError(
            "supplement preview brand and rubric are required"
        )
    if not isinstance(source_catalog, dict) or (
        source_catalog.get("runtime_effect") is not False
        or source_catalog.get("authority") is not False
    ):
        raise EvidenceScoringRecoveryReviewError(
            "supplement source catalog must be non-authoritative"
        )
    catalog_domain = str(
        source_catalog.get("brand", {}).get("domain") or ""
    ).strip().lower()
    if catalog_domain != domain:
        raise EvidenceScoringRecoveryReviewError(
            "supplement source catalog belongs to a different brand"
        )
    _sha256(
        source_catalog.get("identity_state_fingerprint"),
        field="evidence_identity_state_fingerprint",
    )
    evidence_by_id = {
        str(row.get("evidence_id") or ""): dict(row)
        for row in source_catalog.get("entries") or []
        if isinstance(row, dict)
        and row.get("adjudication_state") == "accepted"
        and str(row.get("evidence_id") or "")
    }
    proposal_rows = [dict(row) for row in proposals]
    if not proposal_rows:
        raise EvidenceScoringRecoveryReviewError(
            "supplement proposals are required"
        )
    synthetic_entries: list[dict[str, Any]] = []
    seen_proposals: set[str] = set()
    for proposal in sorted(
        proposal_rows,
        key=lambda row: (
            str(row.get("component_key") or ""),
            str(row.get("tile_id") or ""),
            json.dumps(
                row.get("passages") or [],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    ):
        component_key = str(proposal.get("component_key") or "").strip()
        tile_id = str(proposal.get("tile_id") or "").strip()
        _tile_spec(component_key, tile_id)
        passages = proposal.get("passages")
        if not isinstance(passages, list) or not passages or not all(
            isinstance(row, dict) for row in passages
        ):
            raise EvidenceScoringRecoveryReviewError(
                "supplement proposal requires exact passages"
            )
        normalized_passages: list[dict[str, str]] = []
        sources: list[dict[str, Any]] = []
        for passage in passages:
            evidence_id = _sha256(
                passage.get("evidence_id"),
                field="source_evidence_id",
            )
            quote = normalize_evidence_text(passage.get("quote"))
            source = evidence_by_id.get(evidence_id)
            if source is None:
                raise EvidenceScoringRecoveryReviewError(
                    "supplement proposal references unknown accepted evidence: "
                    + evidence_id
                )
            content = normalize_evidence_text(source.get("content"))
            if not quote or quote not in content:
                raise EvidenceScoringRecoveryReviewError(
                    "supplement quote is not literal in accepted evidence: "
                    + evidence_id
                )
            normalized_passages.append(
                {"evidence_id": evidence_id, "quote": quote}
            )
            sources.append(source)
        proposal_identity = stable_artifact_digest(
            EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_EVIDENCE_VERSION,
            {
                "component_key": component_key,
                "tile_id": tile_id,
                "passages": normalized_passages,
            },
        )
        if proposal_identity in seen_proposals:
            raise EvidenceScoringRecoveryReviewError(
                "duplicate supplement proposal: "
                f"{component_key}.{tile_id}"
            )
        seen_proposals.add(proposal_identity)
        combined_quote = "\n\n".join(
            passage["quote"] for passage in normalized_passages
        )
        source_evidence_ids = sorted(
            {
                passage["evidence_id"]
                for passage in normalized_passages
            }
        )
        synthetic_entries.append(
            {
                "tile_evidence_id": stable_artifact_digest(
                    EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_EVIDENCE_VERSION,
                    {
                        "brand_domain": domain,
                        "rubric_version": rubric,
                        "component_key": component_key,
                        "tile_id": tile_id,
                        "passages": normalized_passages,
                    },
                ),
                "component_key": component_key,
                "tile_id": tile_id,
                "quote": combined_quote,
                "source_urls": sorted(
                    {
                        str(source.get("url") or "")
                        for source in sources
                        if str(source.get("url") or "")
                    }
                ),
                "source_classes": sorted(
                    {
                        str(source.get("source_class") or "")
                        for source in sources
                        if str(source.get("source_class") or "")
                    }
                ),
                "source_evidence_ids": source_evidence_ids,
                "supplement_source_passages": normalized_passages,
                "acceptance_basis": [
                    "human_identity_accepted",
                    "literal_source_match",
                    "mapper_omission_supplement",
                ],
                "first_seen_at": min(
                    str(source.get("first_seen_at") or "")
                    for source in sources
                ),
                "last_seen_at": max(
                    str(source.get("last_seen_at") or "")
                    for source in sources
                ),
                "observation_count": sum(
                    int(source.get("observation_count") or 0)
                    for source in sources
                ),
                "present_in_latest": True,
            }
        )
    synthetic_preview = {
        "runtime_effect": False,
        "authority": False,
        "brand": brand,
        "rubric_version": rubric,
        "latest_report_id": preview.get("latest_report_id"),
        "accepted_evidence": synthetic_entries,
        "recoveries": [],
    }
    candidates = build_recovery_review_candidates(
        [{"lane": "supplement", "preview": synthetic_preview}]
    )
    for candidate in candidates:
        candidate["contexts"][0]["review_scope"] = (
            "mapper_omission_supplement"
        )
        source_entry = next(
            entry
            for entry in synthetic_entries
            if entry["tile_evidence_id"]
            == candidate["evidence"]["tile_evidence_id"]
        )
        candidate["evidence"][
            "supplement_source_passages"
        ] = source_entry["supplement_source_passages"]
        _validate_candidate(candidate)
    return candidates


def validate_recovery_review_supplement_against_preview(
    packet: dict[str, Any],
    preview: dict[str, Any],
    source_catalog: dict[str, Any],
) -> None:
    """Prove candidates are exact projections of accepted report passages."""

    validate_recovery_review_supplement_packet(packet)
    proposals = []
    for candidate in packet["candidates"]:
        evidence = candidate.get("evidence") or {}
        passages = evidence.get("supplement_source_passages")
        if not isinstance(passages, list) or not passages:
            raise EvidenceScoringRecoveryReviewError(
                "supplement candidate source projection is required"
            )
        tile = candidate["tile"]
        proposals.append(
            {
                "component_key": tile["component_key"],
                "tile_id": tile["tile_id"],
                "passages": passages,
            }
        )
    rebuilt = build_recovery_review_supplement_candidates(
        preview,
        source_catalog,
        proposals,
    )
    if rebuilt != packet["candidates"]:
        raise EvidenceScoringRecoveryReviewError(
            "supplement candidates do not match accepted report passages"
        )


def _preview_recovery_candidates(
    preview: dict[str, Any],
) -> list[dict[str, Any]]:
    existing = preview.get("recovery_review_candidates")
    if isinstance(existing, list):
        candidates = [dict(row) for row in existing]
        evaluate_recovery_reviews(candidates, [])
        return candidates
    return build_recovery_review_candidates(
        [{"lane": "history", "preview": preview}]
    )


def validate_recovery_review_supplement_packet(
    packet: dict[str, Any],
) -> None:
    if not isinstance(packet, dict):
        raise EvidenceScoringRecoveryReviewError(
            "supplement packet must be an object"
        )
    if packet.get("schema_version") != (
        EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_PACKET_VERSION
    ):
        raise EvidenceScoringRecoveryReviewError(
            "supplement packet schema version mismatch"
        )
    if any(
        packet.get(field) is not False
        for field in (
            "authority",
            "runtime_effect",
            "automatic_scoring_effect",
            "scanner_runtime_effect",
            "production_runtime_effect",
        )
    ):
        raise EvidenceScoringRecoveryReviewError(
            "supplement packet must remain non-authoritative"
        )
    manifest = packet.get("manifest")
    candidates = packet.get("candidates")
    if not isinstance(manifest, dict) or not isinstance(candidates, list):
        raise EvidenceScoringRecoveryReviewError(
            "supplement packet manifest and candidates are required"
        )
    if manifest.get("schema_version") != (
        EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_MANIFEST_VERSION
    ):
        raise EvidenceScoringRecoveryReviewError(
            "supplement manifest schema version mismatch"
        )
    for field in (
        "base_candidate_set_fingerprint",
        "evidence_identity_state_fingerprint",
        "supplemental_candidate_set_fingerprint",
    ):
        _sha256(manifest.get(field), field=field)
    if not str(manifest.get("brand_identity") or "").strip() or not str(
        manifest.get("rubric_version") or ""
    ).strip():
        raise EvidenceScoringRecoveryReviewError(
            "supplement manifest brand and rubric are required"
        )
    if manifest.get("candidate_count") != len(candidates) or not candidates:
        raise EvidenceScoringRecoveryReviewError(
            "supplement candidate count mismatch"
        )
    if any(
        manifest.get(field) is not False
        for field in (
            "authority",
            "runtime_effect",
            "automatic_scoring_effect",
            "scanner_runtime_effect",
            "production_runtime_effect",
        )
    ):
        raise EvidenceScoringRecoveryReviewError(
            "supplement manifest must remain non-authoritative"
        )
    evaluate_recovery_reviews(candidates, [])
    if recovery_candidate_fingerprint(candidates) != manifest.get(
        "supplemental_candidate_set_fingerprint"
    ):
        raise EvidenceScoringRecoveryReviewError(
            "supplement candidate set fingerprint mismatch"
        )
    if any(
        str(row.get("brand", {}).get("domain") or "").strip().lower()
        != str(manifest["brand_identity"]).strip().lower()
        or str(row.get("rubric_version") or "")
        != str(manifest["rubric_version"])
        for row in candidates
    ):
        raise EvidenceScoringRecoveryReviewError(
            "supplement candidate brand or rubric mismatch"
        )
    expected = stable_artifact_digest(
        EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_PACKET_VERSION,
        {"manifest": manifest, "candidates": candidates},
    )
    if packet.get("packet_fingerprint") != expected:
        raise EvidenceScoringRecoveryReviewError(
            "supplement packet fingerprint mismatch"
        )


def merge_recovery_review_supplements(
    base_candidates: Iterable[dict[str, Any]],
    packets: Iterable[dict[str, Any]],
    *,
    brand_domain: str,
    rubric_version: str,
    evidence_identity_state_fingerprint: str,
    accepted_evidence_ids: Iterable[str],
) -> dict[str, Any]:
    """Select exact active packets and merge them fail-closed.

    The global base-candidate and identity fingerprints record the context in
    which a supplement was generated.  They are deliberately not activation
    keys: unrelated evidence discovered later must not invalidate a reviewed
    direct relation.  Activation instead depends on the exact candidate,
    brand/rubric contract, and continued acceptance of every cited source.
    """

    base_rows = [dict(row) for row in base_candidates]
    evaluate_recovery_reviews(base_rows, [])
    base_fingerprint = recovery_candidate_fingerprint(base_rows)
    identity_fingerprint = _sha256(
        evidence_identity_state_fingerprint,
        field="evidence_identity_state_fingerprint",
    )
    domain = str(brand_domain or "").strip().lower()
    rubric = str(rubric_version or "").strip()
    accepted_ids = {str(value) for value in accepted_evidence_ids if str(value)}
    merged = {str(row["case_id"]): row for row in base_rows}
    active: list[str] = []
    stale: list[str] = []
    blocked: list[str] = []
    context_drift: list[str] = []
    redundant_case_ids: list[str] = []
    for packet in packets:
        validate_recovery_review_supplement_packet(packet)
        manifest = packet["manifest"]
        packet_fingerprint = str(packet["packet_fingerprint"])
        if (
            str(manifest["brand_identity"]).strip().lower() != domain
            or str(manifest["rubric_version"]) != rubric
        ):
            stale.append(packet_fingerprint)
            continue
        if (
            manifest["base_candidate_set_fingerprint"] != base_fingerprint
            or manifest["evidence_identity_state_fingerprint"]
            != identity_fingerprint
        ):
            context_drift.append(packet_fingerprint)
        packet_source_ids = {
            str(value)
            for candidate in packet["candidates"]
            for value in candidate["evidence"].get(
                "source_evidence_ids", []
            )
            if str(value)
        }
        if (
            not packet_source_ids
            or not packet_source_ids.issubset(accepted_ids)
        ):
            blocked.append(packet_fingerprint)
            continue
        for candidate in packet["candidates"]:
            case_id = str(candidate["case_id"])
            if case_id in merged:
                if merged[case_id] != candidate:
                    raise EvidenceScoringRecoveryReviewError(
                        "active supplement candidate collision: " + case_id
                    )
                redundant_case_ids.append(case_id)
                continue
            merged[case_id] = dict(candidate)
        active.append(packet_fingerprint)
    candidates = [merged[key] for key in sorted(merged)]
    evaluate_recovery_reviews(candidates, [])
    return {
        "candidates": candidates,
        "summary": {
            "schema_version": (
                EVIDENCE_SCORING_RECOVERY_SUPPLEMENT_PACKET_VERSION
            ),
            "registered_packet_count": (
                len(active) + len(stale) + len(blocked)
            ),
            "active_packet_count": len(active),
            "stale_packet_count": len(stale),
            "blocked_packet_count": len(blocked),
            "generation_context_drift_count": len(context_drift),
            "active_packet_fingerprints": sorted(active),
            "stale_packet_fingerprints": sorted(stale),
            "blocked_packet_fingerprints": sorted(blocked),
            "generation_context_drift_fingerprints": sorted(
                context_drift
            ),
            "redundant_case_ids": sorted(set(redundant_case_ids)),
            "base_candidate_count": len(base_rows),
            "merged_candidate_count": len(candidates),
            "authority": False,
            "runtime_effect": False,
        },
    }


def _sha256(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise EvidenceScoringRecoveryReviewError(
            f"{field} must be a lowercase SHA-256 digest"
        )
    return normalized


def build_recovery_review_candidates(
    preview_lanes: Iterable[dict[str, Any]],
    *,
    claim_tile_mappings: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Freeze unique direct evidence-to-tile mappings across preview lanes.

    A semantic decision is reusable while the exact evidence, tile contract,
    rubric, and source identities remain unchanged.  Current observations and
    historical recoveries therefore share one subject fingerprint.  Relations
    already routed through the claim-to-tile ledger are excluded so reviewers
    never decide the same path twice.
    """

    accumulators: dict[str, dict[str, Any]] = {}
    claim_routed_pairs = {
        (
            str(mapping.get("tile_id") or ""),
            str(mapping.get("source_evidence_id") or ""),
        )
        for mapping in claim_tile_mappings
        if isinstance(mapping, dict)
        and str(mapping.get("tile_id") or "")
        and str(mapping.get("source_evidence_id") or "")
    }

    def accumulate_candidate(
        *,
        lane: str,
        preview: dict[str, Any],
        brand: dict[str, Any],
        domain: str,
        evidence: dict[str, Any],
        component_key: str,
        tile_id: str,
        latest_state: str,
        proposed_state: str,
        review_scope: str,
    ) -> None:
        tile_spec = _tile_spec(component_key, tile_id)
        source_evidence_ids = sorted(
            {
                str(value)
                for value in evidence.get("source_evidence_ids") or []
                if str(value)
                and (tile_id, str(value)) not in claim_routed_pairs
            }
        )
        if not source_evidence_ids:
            return
        tile_evidence_id = str(
            evidence.get("tile_evidence_id") or ""
        )
        if not tile_evidence_id:
            raise EvidenceScoringRecoveryReviewError(
                "direct tile relation is missing tile evidence identity"
            )
        source_urls = sorted(
            str(value)
            for value in evidence.get("source_urls") or []
            if str(value)
        )
        subject = {
            "brand_domain": domain,
            "rubric_version": str(preview.get("rubric_version") or ""),
            "component_key": component_key,
            "tile_id": tile_id,
            "tile_name": str(tile_spec["name"]),
            "tile_condition": str(tile_spec["condition"]),
            "tile_evidence_contract": dict(
                tile_spec["evidence_contract"]
            ),
            "tile_evidence_id": tile_evidence_id,
            "quote": str(evidence.get("quote") or ""),
            "source_urls": source_urls,
            "source_evidence_ids": source_evidence_ids,
        }
        candidate_fingerprint = stable_artifact_digest(
            EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION,
            subject,
        )
        case_id = (
            "scoring-recovery-"
            f"{domain.replace('.', '-')}-"
            f"{component_key}-{tile_id.lower()}-"
            f"{candidate_fingerprint[:12]}"
        )
        accumulator = accumulators.setdefault(
            case_id,
            {
                "schema_version": (
                    EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION
                ),
                "policy_version": (
                    EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION
                ),
                "case_id": case_id,
                "candidate_fingerprint": candidate_fingerprint,
                "brand": {
                    "name": str(brand.get("name") or ""),
                    "domain": domain,
                },
                "rubric_version": str(
                    preview.get("rubric_version") or ""
                ),
                "tile": {
                    "component_key": component_key,
                    "tile_id": tile_id,
                    "tile_key": f"{component_key}.{tile_id}",
                    "name": str(tile_spec["name"]),
                    "condition": str(tile_spec["condition"]),
                    "evidence_contract": dict(
                        tile_spec["evidence_contract"]
                    ),
                    "latest_state": latest_state,
                    "proposed_state": proposed_state,
                },
                "evidence": {
                    "tile_evidence_id": tile_evidence_id,
                    "quote": str(evidence.get("quote") or ""),
                    "source_urls": source_urls,
                    "source_classes": sorted(
                        str(value)
                        for value in evidence.get("source_classes") or []
                        if str(value)
                    ),
                    "source_evidence_ids": source_evidence_ids,
                    "acceptance_basis": sorted(
                        str(value)
                        for value in evidence.get("acceptance_basis") or []
                        if str(value)
                    ),
                    "first_seen_at": str(
                        evidence.get("first_seen_at") or ""
                    ),
                    "last_seen_at": str(
                        evidence.get("last_seen_at") or ""
                    ),
                    "observation_count": int(
                        evidence.get("observation_count") or 0
                    ),
                },
                "contexts": [],
                "review_prompt": (
                    "¿La evidencia citada satisface específicamente "
                    "el contrato semántico de esta baldosa?"
                ),
                "runtime_effect": False,
                "authority": False,
            },
        )
        context = {
            "lane": lane,
            "latest_report_id": (
                str(preview.get("latest_report_id") or "") or None
            ),
            "review_scope": review_scope,
            "latest_state": latest_state,
            "proposed_state": proposed_state,
        }
        if context not in accumulator["contexts"]:
            accumulator["contexts"].append(context)

    for lane_row in preview_lanes:
        if not isinstance(lane_row, dict):
            continue
        lane = str(lane_row.get("lane") or "").strip()
        preview = lane_row.get("preview")
        if not lane or not isinstance(preview, dict):
            continue
        if (
            preview.get("runtime_effect") is not False
            or preview.get("authority") is not False
        ):
            raise EvidenceScoringRecoveryReviewError(
                "recovery review requires a non-authoritative preview"
            )
        brand = (
            preview.get("brand")
            if isinstance(preview.get("brand"), dict)
            else {}
        )
        domain = str(brand.get("domain") or "").strip().lower()
        if not domain:
            continue
        evidence_by_id = {
            str(row.get("tile_evidence_id") or ""): row
            for row in preview.get("accepted_evidence") or []
            if isinstance(row, dict)
            and str(row.get("tile_evidence_id") or "")
        }
        for evidence in evidence_by_id.values():
            if evidence.get("present_in_latest") is not True:
                continue
            accumulate_candidate(
                lane=lane,
                preview=preview,
                brand=brand,
                domain=domain,
                evidence=evidence,
                component_key=str(
                    evidence.get("component_key") or ""
                ).strip(),
                tile_id=str(evidence.get("tile_id") or "").strip(),
                latest_state="ok",
                proposed_state="ok",
                review_scope="current_direct_relation",
            )
        for recovery in preview.get("recoveries") or []:
            if not isinstance(recovery, dict):
                continue
            component_key = str(
                recovery.get("component_key") or ""
            ).strip()
            tile_id = str(recovery.get("tile_id") or "").strip()
            for tile_evidence_id in sorted(
                {
                    str(value)
                    for value in recovery.get("tile_evidence_ids") or []
                    if str(value)
                }
            ):
                evidence = evidence_by_id.get(tile_evidence_id)
                if evidence is None:
                    raise EvidenceScoringRecoveryReviewError(
                        "recovery references evidence absent from preview"
                    )
                accumulate_candidate(
                    lane=lane,
                    preview=preview,
                    brand=brand,
                    domain=domain,
                    evidence=evidence,
                    component_key=component_key,
                    tile_id=tile_id,
                    latest_state=str(
                        recovery.get("latest_state") or ""
                    ),
                    proposed_state=str(
                        recovery.get("preview_state") or ""
                    ),
                    review_scope="historical_recovery",
                )

    candidates = list(accumulators.values())
    for candidate in candidates:
        candidate["contexts"].sort(
            key=lambda row: (
                0
                if row.get("review_scope")
                == "current_direct_relation"
                else 1,
                str(row.get("review_scope") or ""),
                str(row["lane"]),
                str(row.get("latest_report_id") or ""),
            )
        )
        primary_context = candidate["contexts"][0]
        candidate["tile"]["latest_state"] = str(
            primary_context["latest_state"]
        )
        candidate["tile"]["proposed_state"] = str(
            primary_context["proposed_state"]
        )
        _validate_candidate(candidate)
    return sorted(candidates, key=lambda row: str(row["case_id"]))


def _latest_claim_tile_mappings(
    ledger: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if ledger is None:
        return []
    if not isinstance(ledger, dict):
        raise EvidenceScoringRecoveryReviewError(
            "claim-to-tile ledger must be an object"
        )
    if (
        ledger.get("runtime_effect") is not False
        or ledger.get("authority") is not False
    ):
        raise EvidenceScoringRecoveryReviewError(
            "claim-to-tile ledger must remain non-authoritative"
        )
    latest_series_id = str(
        ledger.get("latest_mapping_series_id") or ""
    )
    rows = [
        dict(mapping)
        for mapping in ledger.get("mappings") or []
        if isinstance(mapping, dict)
        and (
            not latest_series_id
            or str(mapping.get("mapping_series_id") or "")
            == latest_series_id
        )
    ]
    rows.sort(key=lambda row: str(row.get("mapping_id") or ""))
    return rows


def recovery_review_subject(
    candidates: Iterable[dict[str, Any]],
    subject_id: str,
) -> dict[str, Any] | None:
    """Return one currently projected semantic mapping by its fingerprint."""

    normalized = str(subject_id or "").strip().lower()
    return next(
        (
            dict(candidate)
            for candidate in candidates
            if isinstance(candidate, dict)
            and str(
                candidate.get("candidate_fingerprint") or ""
            ).strip().lower()
            == normalized
        ),
        None,
    )


def build_recovery_review_template(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return unsigned first events; generated proposals are not decisions."""

    rows = [dict(candidate) for candidate in candidates]
    for row in rows:
        _validate_candidate(row)
    return [
        {
            "schema_version": (
                EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION
            ),
            "case_id": str(candidate["case_id"]),
            "candidate_fingerprint": str(
                candidate["candidate_fingerprint"]
            ),
            "event_id": None,
            "sequence": 1,
            "previous_event_id": None,
            "decision": None,
            "reviewer_id": None,
            "rationale": None,
            "reviewed_at": None,
            "runtime_effect": False,
            "authority": False,
        }
        for candidate in sorted(
            rows,
            key=lambda row: str(row["case_id"]),
        )
    ]


def load_recovery_review_events(
    path: str | Path | None,
) -> list[dict[str, Any]]:
    """Load append-only review events; a missing optional path means no review."""

    if path is None:
        return []
    target = Path(path)
    if not target.is_file():
        raise EvidenceScoringRecoveryReviewError(
            f"recovery review JSONL does not exist: {target}"
        )
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        target.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceScoringRecoveryReviewError(
                f"invalid recovery review JSONL at line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise EvidenceScoringRecoveryReviewError(
                f"recovery review line {line_number} must be an object"
            )
        rows.append(row)
    return rows


def evaluate_recovery_reviews(
    candidates: Iterable[dict[str, Any]],
    review_events: Iterable[dict[str, Any]],
    *,
    events_are_current: bool = False,
) -> dict[str, Any]:
    """Resolve full chains or durable current projections fail-closed."""

    candidate_rows = [dict(row) for row in candidates]
    event_rows = [dict(row) for row in review_events]
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for candidate in candidate_rows:
        _validate_candidate(candidate)
        case_id = str(candidate["case_id"])
        if case_id in candidates_by_id:
            raise EvidenceScoringRecoveryReviewError(
                f"duplicate recovery candidate case_id: {case_id}"
            )
        candidates_by_id[case_id] = candidate

    events_by_case: dict[str, list[dict[str, Any]]] = {}
    seen_event_ids: set[str] = set()
    for event in event_rows:
        _validate_event(event)
        event_id = str(event["event_id"])
        if event_id in seen_event_ids:
            raise EvidenceScoringRecoveryReviewError(
                f"duplicate recovery review event_id: {event_id}"
            )
        seen_event_ids.add(event_id)
        case_id = str(event["case_id"])
        candidate = candidates_by_id.get(case_id)
        if candidate is None:
            raise EvidenceScoringRecoveryReviewError(
                f"recovery review references unknown case: {case_id}"
            )
        if (
            str(event["candidate_fingerprint"])
            != str(candidate["candidate_fingerprint"])
        ):
            raise EvidenceScoringRecoveryReviewError(
                f"recovery review candidate fingerprint mismatch: {case_id}"
            )
        events_by_case.setdefault(case_id, []).append(event)

    current_by_case: dict[str, dict[str, Any]] = {}
    for case_id, events in events_by_case.items():
        if events_are_current:
            if len(events) != 1:
                raise EvidenceScoringRecoveryReviewError(
                    "durable recovery review projection contains "
                    f"multiple current events for case: {case_id}"
                )
            event = events[0]
            sequence = int(event["sequence"])
            previous_event_id = (
                str(event["previous_event_id"])
                if event.get("previous_event_id")
                else None
            )
            if (
                (sequence == 1 and previous_event_id is not None)
                or (sequence > 1 and previous_event_id is None)
                or (
                    str(event["decision"]) == "revoked"
                    and sequence == 1
                )
            ):
                raise EvidenceScoringRecoveryReviewError(
                    "durable recovery review projection has invalid "
                    f"sequence metadata for case: {case_id}"
                )
            current_by_case[case_id] = event
            continue
        ordered = sorted(
            events,
            key=lambda row: (
                int(row["sequence"]),
                str(row["reviewed_at"]),
                str(row["event_id"]),
            ),
        )
        previous: dict[str, Any] | None = None
        for expected_sequence, event in enumerate(ordered, start=1):
            if int(event["sequence"]) != expected_sequence:
                raise EvidenceScoringRecoveryReviewError(
                    f"recovery review sequence gap for case: {case_id}"
                )
            expected_previous = (
                str(previous["event_id"]) if previous else None
            )
            actual_previous = (
                str(event["previous_event_id"])
                if event.get("previous_event_id")
                else None
            )
            if actual_previous != expected_previous:
                raise EvidenceScoringRecoveryReviewError(
                    f"recovery review predecessor mismatch: {case_id}"
                )
            if (
                str(event["decision"]) == "revoked"
                and (
                    previous is None
                    or str(previous["decision"]) == "revoked"
                )
            ):
                raise EvidenceScoringRecoveryReviewError(
                    f"invalid recovery review revocation: {case_id}"
                )
            previous = event
        if previous is not None:
            current_by_case[case_id] = previous

    decision_counts: Counter[str] = Counter()
    pending_case_ids: list[str] = []
    evaluated: list[dict[str, Any]] = []
    accepted_evidence_ids: set[str] = set()
    for case_id in sorted(candidates_by_id):
        candidate = candidates_by_id[case_id]
        event = current_by_case.get(case_id)
        decision = (
            str(event["decision"]) if event is not None else "pending"
        )
        effective_decision = (
            "pending" if decision == "revoked" else decision
        )
        decision_counts[decision] += 1
        if effective_decision == "pending":
            pending_case_ids.append(case_id)
        if effective_decision == "accepted":
            accepted_evidence_ids.add(
                str(candidate["evidence"]["tile_evidence_id"])
            )
        evaluated.append(
            {
                "case_id": case_id,
                "candidate_fingerprint": str(
                    candidate["candidate_fingerprint"]
                ),
                "brand_domain": str(candidate["brand"]["domain"]),
                "tile_key": str(candidate["tile"]["tile_key"]),
                "tile_evidence_id": str(
                    candidate["evidence"]["tile_evidence_id"]
                ),
                "decision": effective_decision,
                "current_event_decision": decision,
                "event_id": (
                    str(event["event_id"]) if event is not None else None
                ),
                "reviewer_id": (
                    str(event["reviewer_id"])
                    if event is not None
                    else None
                ),
            }
        )

    reviewed_count = sum(
        count
        for decision, count in decision_counts.items()
        if decision not in {"pending", "revoked"}
    )
    non_accept_count = int(
        decision_counts["disputed"] + decision_counts["rejected"]
    )
    review_gate_ready = not pending_case_ids
    blockers: list[str] = []
    if pending_case_ids:
        blockers.append("recovery_semantic_reviews_incomplete")
    if non_accept_count:
        blockers.append("recovery_semantic_non_accepts_present")
    blockers.extend(
        [
            "runtime_scoring_wiring_disabled",
            "promotion_policy_not_adopted",
        ]
    )
    return {
        "schema_version": EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION,
        "event_schema_version": (
            EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION
        ),
        "policy_version": (
            EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION
        ),
        "candidate_set_fingerprint": recovery_candidate_fingerprint(
            candidate_rows
        ),
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "review_event_mode": (
            "current_projection"
            if events_are_current
            else "full_chain"
        ),
        "review_gate_ready": review_gate_ready,
        "promotion_ready": False,
        "promotion_blockers": sorted(set(blockers)),
        "summary": {
            "candidate_count": len(candidates_by_id),
            "event_count": len(event_rows),
            "reviewed_count": reviewed_count,
            "pending_count": len(pending_case_ids),
            "accepted_count": int(decision_counts["accepted"]),
            "disputed_count": int(decision_counts["disputed"]),
            "rejected_count": int(decision_counts["rejected"]),
            "revoked_count": int(decision_counts["revoked"]),
            "accepted_evidence_count": len(accepted_evidence_ids),
        },
        "accepted_tile_evidence_ids": sorted(accepted_evidence_ids),
        "pending_case_ids": pending_case_ids,
        "evaluated": evaluated,
    }


def recovery_candidate_fingerprint(
    candidates: Iterable[dict[str, Any]],
) -> str:
    canonical = json.dumps(
        sorted(
            (dict(row) for row in candidates),
            key=lambda row: str(row.get("case_id") or ""),
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tile_spec(component_key: str, tile_id: str) -> dict[str, Any]:
    component = COMPONENTS.get(component_key)
    if not isinstance(component, dict):
        raise EvidenceScoringRecoveryReviewError(
            f"unknown recovery component: {component_key}"
        )
    for tile in component.get("tiles") or []:
        if isinstance(tile, dict) and str(tile.get("id") or "") == tile_id:
            contract = tile.get("evidence_contract")
            if not isinstance(contract, dict):
                break
            return tile
    raise EvidenceScoringRecoveryReviewError(
        f"unknown recovery tile: {component_key}.{tile_id}"
    )


def _validate_candidate(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "policy_version",
        "case_id",
        "candidate_fingerprint",
        "brand",
        "rubric_version",
        "tile",
        "evidence",
        "contexts",
        "review_prompt",
        "runtime_effect",
        "authority",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceScoringRecoveryReviewError(
            f"recovery candidate missing fields: {', '.join(missing)}"
        )
    if row["schema_version"] != EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION:
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate schema version mismatch"
        )
    if (
        row["policy_version"]
        != EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION
    ):
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate policy version mismatch"
        )
    if row["runtime_effect"] is not False or row["authority"] is not False:
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidates must remain non-authoritative"
        )
    brand = row.get("brand")
    tile = row.get("tile")
    evidence = row.get("evidence")
    if not isinstance(brand, dict) or not str(
        brand.get("domain") or ""
    ).strip():
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate brand domain is required"
        )
    if not isinstance(tile, dict):
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate tile is required"
        )
    for field in (
        "component_key",
        "tile_id",
        "tile_key",
        "name",
        "condition",
        "evidence_contract",
        "latest_state",
        "proposed_state",
    ):
        if not tile.get(field):
            raise EvidenceScoringRecoveryReviewError(
                f"recovery candidate tile {field} is required"
            )
    _tile_spec(str(tile["component_key"]), str(tile["tile_id"]))
    if not isinstance(evidence, dict):
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate evidence is required"
        )
    for field in ("tile_evidence_id", "quote", "source_evidence_ids"):
        if not evidence.get(field):
            raise EvidenceScoringRecoveryReviewError(
                f"recovery candidate evidence {field} is required"
            )
    if not isinstance(row.get("contexts"), list) or not row["contexts"]:
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate context is required"
        )
    expected_fingerprint = stable_artifact_digest(
        EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION,
        {
            "brand_domain": str(brand["domain"]).strip().lower(),
            "rubric_version": str(row["rubric_version"]),
            "component_key": str(tile["component_key"]),
            "tile_id": str(tile["tile_id"]),
            "tile_name": str(tile["name"]),
            "tile_condition": str(tile["condition"]),
            "tile_evidence_contract": dict(
                tile["evidence_contract"]
            ),
            "tile_evidence_id": str(evidence["tile_evidence_id"]),
            "quote": str(evidence["quote"]),
            "source_urls": sorted(
                str(value)
                for value in evidence.get("source_urls") or []
                if str(value)
            ),
            "source_evidence_ids": sorted(
                str(value)
                for value in evidence.get("source_evidence_ids") or []
                if str(value)
            ),
        },
    )
    if str(row["candidate_fingerprint"]) != expected_fingerprint:
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate fingerprint mismatch"
        )


def _validate_event(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "case_id",
        "candidate_fingerprint",
        "event_id",
        "sequence",
        "previous_event_id",
        "decision",
        "reviewer_id",
        "rationale",
        "reviewed_at",
        "runtime_effect",
        "authority",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceScoringRecoveryReviewError(
            f"recovery review event missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION
    ):
        raise EvidenceScoringRecoveryReviewError(
            "recovery review event schema version mismatch"
        )
    if row["decision"] not in REVIEW_DECISIONS:
        raise EvidenceScoringRecoveryReviewError(
            "recovery review decision is invalid"
        )
    if (
        not isinstance(row["sequence"], int)
        or int(row["sequence"]) < 1
    ):
        raise EvidenceScoringRecoveryReviewError(
            "recovery review sequence must be positive"
        )
    for field in (
        "case_id",
        "candidate_fingerprint",
        "event_id",
        "reviewer_id",
        "rationale",
        "reviewed_at",
    ):
        if not str(row.get(field) or "").strip():
            raise EvidenceScoringRecoveryReviewError(
                f"recovery review {field} is required"
            )
    if row["runtime_effect"] is not False or row["authority"] is not False:
        raise EvidenceScoringRecoveryReviewError(
            "recovery reviews must remain non-authoritative"
        )
    try:
        datetime.fromisoformat(
            str(row["reviewed_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceScoringRecoveryReviewError(
            "recovery review reviewed_at must be ISO-8601"
        ) from exc
