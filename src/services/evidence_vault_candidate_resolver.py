"""Resolve one canonical-memory candidate from existing durable Vault state.

The resolver is intentionally pure.  It consumes immutable report snapshots
and the current projections of append-only human journals, cross-checks every
accepted evidence-to-tile path, and emits the exact pending-review packet plus
its reference resolution. It grants no authority and emits no canonical score.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable
from urllib.parse import urlparse

from src.evidence_identity import stable_artifact_digest
from src.services.evidence_accepted_memory import (
    EVIDENCE_ACCEPTED_MEMORY_VERSION,
    EvidenceAcceptedMemoryError,
    build_evidence_accepted_memory,
)
from src.services.evidence_claim_tile_ledger import (
    build_evidence_claim_tile_ledger,
)
from src.services.evidence_memory_snapshot import (
    EVIDENCE_MEMORY_SNAPSHOT_VERSION,
    EvidenceMemorySnapshotError,
    build_evidence_memory_snapshot,
)
from src.services.evidence_reviewed_claim_tile_memory import (
    EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_SEMANTIC_VERSION,
    EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_VERSION,
    EvidenceReviewedClaimTileMemoryError,
    build_reviewed_claim_tile_memory_from_journal_shadow,
)
from src.services.evidence_scoring_memory_preview import (
    EvidenceScoringMemoryPreviewError,
)
from src.services.evidence_scoring_recovery_review import (
    EVIDENCE_SCORING_REVIEWED_SHADOW_VERSION,
    EvidenceScoringRecoveryReviewError,
    build_reviewed_scoring_memory_shadow,
)
from src.services.evidence_vault_canonical_authority import (
    EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION,
    EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION,
    build_reference_resolution,
)
from src.services.evidence_vault_canonical_core import (
    EvidenceVaultCanonicalCoreError,
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_tile_contract_registry,
    canonical_fingerprint,
)
from src.sv9.rubric import (
    BASE_COMPONENTS,
    COMPONENTS,
    MAGNETISM_CAP_BASE_THRESHOLD,
    MAGNETISM_CAP_VALUE,
    PRESENTATION_ORDER,
    RUBRIC_VERSION,
)


EVIDENCE_VAULT_CANDIDATE_RESOLVER_VERSION = "evidence-vault-candidate-resolver-v2"
EVIDENCE_VAULT_AGGREGATION_POLICY_VERSION = "evidence-vault-canonical-aggregation-policy-v1"
EVIDENCE_VAULT_CLAIM_TILE_REVIEW_PACKET_SET_VERSION = "evidence-reviewed-claim-tile-packet-set-v1"
EVIDENCE_VAULT_REVIEWED_TILE_RELATION_MEMORY_VERSION = "evidence-vault-reviewed-tile-relation-memory-v1"
EVIDENCE_VAULT_REVIEW_PACKET_SET_VERSION = "evidence-vault-review-packet-set-v1"
EVIDENCE_VAULT_DIRECT_TILE_RELATION_VERSION = "evidence-vault-direct-tile-relation-v1"
EVIDENCE_VAULT_VERIFIED_DEPRECATION_VERSION = "evidence-vault-verified-deprecation-relation-v1"
EVIDENCE_VAULT_UNRESOLVED_VERSION = "evidence-vault-candidate-unresolved-item-v1"


class EvidenceVaultCandidateResolverError(ValueError):
    """Durable inputs cannot produce one reproducible candidate packet."""


def build_canonical_aggregation_policy() -> dict[str, Any]:
    """Freeze the scoring semantics required by a candidate manifest.

    This is only a content-addressed contract.  The canonical scorer that
    executes it is a later boundary and the current scanner never imports it.
    """

    return {
        "schema_version": EVIDENCE_VAULT_AGGREGATION_POLICY_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "tile_points": {
            "ok": 1,
            "no": 0,
            "sin_evidencia": 0,
            "contradiction": None,
        },
        "components": [
            {
                "component_key": component_key,
                "multiplier": int(COMPONENTS[component_key]["multiplier"]),
                "tile_ids": [str(tile["id"]) for tile in COMPONENTS[component_key]["tiles"]],
            }
            for component_key in PRESENTATION_ORDER
        ],
        "base_components": list(BASE_COMPONENTS),
        "magnetism_cap": {
            "base_average_below": MAGNETISM_CAP_BASE_THRESHOLD,
            "maximum_lit_tiles": MAGNETISM_CAP_VALUE,
        },
        "contradiction_result": "score_null",
        "authority_scope": "b3s-vault",
        "scanner_runtime_effect": False,
        "production_runtime_effect": False,
    }


def canonical_aggregation_policy_fingerprint() -> str:
    return canonical_fingerprint(
        EVIDENCE_VAULT_AGGREGATION_POLICY_VERSION,
        build_canonical_aggregation_policy(),
    )


def build_resolved_canonical_memory_candidate(
    *,
    brand_identity: str,
    reports: Iterable[dict[str, Any]],
    evidence_adjudications: Iterable[dict[str, Any]] = (),
    claim_reconciliations: Iterable[dict[str, Any]] = (),
    claim_tile_ledger: dict[str, Any],
    claim_tile_reviews: Iterable[dict[str, Any]] = (),
    scoring_recovery_reviews: Iterable[dict[str, Any]] = (),
    supplemental_recovery_candidate_packets: Iterable[dict[str, Any]] = (),
    registered_review_packet_fingerprints: Iterable[str] = (),
    current_canonical_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one exact 80-tile packet from the current durable projections."""

    domain = _normalize_domain(brand_identity)
    if not domain:
        raise EvidenceVaultCandidateResolverError("brand_identity must resolve to a canonical domain")
    report_rows = _dict_rows(reports, field="reports")
    if not report_rows:
        raise EvidenceVaultCandidateResolverError("at least one immutable report snapshot is required")
    adjudication_rows = _dict_rows(
        evidence_adjudications,
        field="evidence_adjudications",
    )
    reconciliation_rows = _dict_rows(
        claim_reconciliations,
        field="claim_reconciliations",
    )
    review_rows = _dict_rows(
        claim_tile_reviews,
        field="claim_tile_reviews",
    )
    recovery_review_rows = _dict_rows(
        scoring_recovery_reviews,
        field="scoring_recovery_reviews",
    )
    registered_packets = {
        _sha256(value, field="registered_review_packet_fingerprints") for value in registered_review_packet_fingerprints
    }
    review_packet_fingerprints = {
        _sha256(
            review.get("review_packet_fingerprint"),
            field="review_packet_fingerprint",
        )
        for review in review_rows
    }
    missing_packets = sorted(review_packet_fingerprints - registered_packets)
    if missing_packets:
        raise EvidenceVaultCandidateResolverError(
            "claim-to-tile reviews reference unregistered packets: " + ", ".join(missing_packets)
        )

    _validate_ledger(claim_tile_ledger, domain=domain)
    rebuilt_ledger = build_evidence_claim_tile_ledger(
        report_rows,
        mode="shadow",
    )
    if rebuilt_ledger.get("state_fingerprint") != claim_tile_ledger.get("state_fingerprint"):
        raise EvidenceVaultCandidateResolverError("claim-to-tile ledger does not match the immutable reports")
    try:
        memory_snapshot = build_evidence_memory_snapshot(
            report_rows,
            rubric_version=RUBRIC_VERSION,
            evaluator_version=EVIDENCE_VAULT_CANDIDATE_RESOLVER_VERSION,
            evidence_adjudications=adjudication_rows,
            claim_reconciliations=reconciliation_rows,
        )
        accepted_memory = build_evidence_accepted_memory(
            report_rows,
            evidence_adjudications=adjudication_rows,
        )
        reviewed_memory = _reviewed_memory(
            claim_tile_ledger,
            review_rows,
            brand_identity=domain,
        )
    except (
        EvidenceAcceptedMemoryError,
        EvidenceMemorySnapshotError,
        EvidenceReviewedClaimTileMemoryError,
    ) as exc:
        raise EvidenceVaultCandidateResolverError("durable evidence projections are invalid") from exc
    try:
        recovery_memory = build_reviewed_scoring_memory_shadow(
            report_rows,
            evidence_adjudications=adjudication_rows,
            recovery_review_events=recovery_review_rows,
            supplemental_candidate_packets=(
                supplemental_recovery_candidate_packets
            ),
            reviewed_claim_tile_memory=reviewed_memory,
            claim_tile_ledger=claim_tile_ledger,
            ignore_stale_review_events=True,
            review_events_are_current=True,
        )
    except (
        EvidenceScoringMemoryPreviewError,
        EvidenceScoringRecoveryReviewError,
    ) as exc:
        raise EvidenceVaultCandidateResolverError("scoring evidence-to-tile projection is invalid") from exc
    for label, projection in (
        ("memory snapshot", memory_snapshot),
        ("accepted evidence memory", accepted_memory),
        ("reviewed claim-to-tile memory", reviewed_memory),
        ("reviewed scoring memory", recovery_memory),
    ):
        _validate_projection_brand(
            projection,
            domain=domain,
            label=label,
        )

    latest_mappings = _latest_mappings(claim_tile_ledger)
    reviews_by_mapping = {
        str(review.get("mapping_id") or review.get("subject_id") or ""): review
        for review in review_rows
        if str(review.get("mapping_id") or review.get("subject_id") or "")
    }
    accepted_evidence = {
        str(entry["evidence_id"]): entry
        for entry in accepted_memory.get("entries") or []
        if isinstance(entry, dict) and str(entry.get("evidence_id") or "")
    }
    snapshot_evidence_ids = {
        str(entry["evidence_id"])
        for entry in (memory_snapshot.get("semantic_state", {}).get("evidence", []))
        if isinstance(entry, dict) and str(entry.get("evidence_id") or "")
    }
    snapshot_claim_ids = {
        str(entry["claim_variant_id"])
        for entry in (
            memory_snapshot.get("semantic_state", {}).get(
                "claim_variants",
                [],
            )
        )
        if isinstance(entry, dict) and str(entry.get("claim_variant_id") or "")
    }

    basis_by_tile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unresolved_items: list[dict[str, Any]] = []
    unresolved_by_tile: dict[str, list[str]] = defaultdict(list)
    accepted_mapping_ids: set[str] = set()
    for mapping in latest_mappings:
        mapping_id = str(mapping.get("mapping_id") or "")
        tile_id = str(mapping.get("tile_id") or "")
        review = reviews_by_mapping.get(mapping_id)
        decision = str((review or {}).get("decision") or "pending")
        if decision == "accepted":
            accepted_mapping_ids.add(mapping_id)
            _resolve_accepted_mapping(
                mapping,
                review=review or {},
                accepted_evidence=accepted_evidence,
                snapshot_evidence_ids=snapshot_evidence_ids,
                snapshot_claim_ids=snapshot_claim_ids,
                basis_by_tile=basis_by_tile,
                unresolved_items=unresolved_items,
                unresolved_by_tile=unresolved_by_tile,
            )
        elif decision == "pending":
            _add_unresolved(
                unresolved_items,
                unresolved_by_tile,
                tile_id=tile_id,
                kind="claim_tile_review_pending",
                blocking=True,
                details={
                    "mapping_id": mapping_id,
                    "review_state": decision,
                },
            )
            _add_pending_mapping_prerequisites(
                mapping,
                review_state=decision,
                accepted_evidence=accepted_evidence,
                snapshot_evidence_ids=snapshot_evidence_ids,
                snapshot_claim_ids=snapshot_claim_ids,
                unresolved_items=unresolved_items,
                unresolved_by_tile=unresolved_by_tile,
            )
        elif decision == "disputed":
            _add_unresolved(
                unresolved_items,
                unresolved_by_tile,
                tile_id=tile_id,
                kind="claim_tile_review_disputed",
                blocking=True,
                details={
                    "mapping_id": mapping_id,
                    "review_state": decision,
                },
            )
            _add_pending_mapping_prerequisites(
                mapping,
                review_state=decision,
                accepted_evidence=accepted_evidence,
                snapshot_evidence_ids=snapshot_evidence_ids,
                snapshot_claim_ids=snapshot_claim_ids,
                unresolved_items=unresolved_items,
                unresolved_by_tile=unresolved_by_tile,
            )
        elif decision not in {"rejected", "revoked"}:
            raise EvidenceVaultCandidateResolverError(f"unsupported claim-to-tile review decision: {decision}")

    claim_routed_pairs = {
        (
            str(mapping.get("tile_id") or ""),
            str(mapping.get("source_evidence_id") or ""),
        )
        for mapping in latest_mappings
        if str(mapping.get("tile_id") or "") and str(mapping.get("source_evidence_id") or "")
    }
    direct_relations, recovery_reviews_by_relation = _resolve_direct_tile_relations(
        recovery_memory,
        recovery_review_rows=recovery_review_rows,
        accepted_evidence=accepted_evidence,
        snapshot_evidence_ids=snapshot_evidence_ids,
        claim_routed_pairs=claim_routed_pairs,
        basis_by_tile=basis_by_tile,
        unresolved_items=unresolved_items,
        unresolved_by_tile=unresolved_by_tile,
    )
    reviewed_relation_version = canonical_fingerprint(
        EVIDENCE_VAULT_REVIEWED_TILE_RELATION_MEMORY_VERSION,
        {
            "claim_tile_reviewed_memory_candidate_version": reviewed_memory["reviewed_memory_candidate_version"],
            "direct_tile_relations": direct_relations,
        },
    )
    review_packet_set_fingerprint = canonical_fingerprint(
        EVIDENCE_VAULT_REVIEW_PACKET_SET_VERSION,
        {
            "claim_tile_review_packet_set_fingerprint": reviewed_memory["review_packet_set_fingerprint"],
            "scoring_recovery_candidate_set_fingerprint": recovery_memory["recovery_review"][
                "candidate_set_fingerprint"
            ],
        },
    )

    current = _validate_current_memory(
        current_canonical_memory,
        domain=domain,
    )
    coverage_loss_relation_ids = {
        str(mapping.get("mapping_id") or "")
        for mapping in latest_mappings
        if str(mapping.get("state") or "") == "not_reacquired"
    }
    if current is None:
        candidate_tiles = _baseline_tiles(
            basis_by_tile,
            unresolved_by_tile=unresolved_by_tile,
        )
    else:
        candidate_tiles = _incremental_tiles(
            current,
            basis_by_tile=basis_by_tile,
            unresolved_items=unresolved_items,
            unresolved_by_tile=unresolved_by_tile,
            reviews_by_mapping=reviews_by_mapping,
            adjudications_by_evidence={
                str(event.get("subject_id") or ""): event
                for event in adjudication_rows
                if str(event.get("subject_id") or "")
            },
            recovery_reviews_by_relation=recovery_reviews_by_relation,
            coverage_loss_relation_ids=coverage_loss_relation_ids,
            ledger_state_fingerprint=str(claim_tile_ledger.get("state_fingerprint") or ""),
        )

    coverage_summary = {
        "resolver_version": EVIDENCE_VAULT_CANDIDATE_RESOLVER_VERSION,
        "report_count": len(report_rows),
        "observed_evidence_count": int(
            accepted_memory.get("summary", {}).get(
                "observed_evidence_count",
                0,
            )
        ),
        "accepted_evidence_count": len(accepted_evidence),
        "candidate_mapping_count": len(latest_mappings),
        "accepted_mapping_count": len(accepted_mapping_ids),
        "pending_mapping_count": sum(item["kind"] == "claim_tile_review_pending" for item in unresolved_items),
        "claim_tile_ledger_state_fingerprint": str(claim_tile_ledger.get("state_fingerprint") or ""),
        "reviewed_mapping_state_fingerprint": str(reviewed_memory.get("state_fingerprint") or ""),
        "scoring_recovery_state_fingerprint": str(recovery_memory.get("state_fingerprint") or ""),
        "direct_evidence_tile_relation_count": len(direct_relations),
        "accepted_direct_evidence_tile_relation_count": sum(
            row["review_status"] == "accepted" for row in direct_relations
        ),
        "unreviewed_direct_evidence_tile_relation_count": sum(
            row["review_status"] == "unreviewed" for row in direct_relations
        ),
        "pending_scoring_recovery_review_count": int(
            recovery_memory.get("recovery_review", {}).get("summary", {}).get("pending_count", 0)
        ),
        "stale_scoring_recovery_review_count": int(
            recovery_memory.get("recovery_review", {}).get("journal", {}).get("stale_event_count", 0)
        ),
    }
    packet = build_candidate_packet(
        brand_identity=domain,
        parent_canonical_memory_version=(current["canonical_memory_version"] if current is not None else None),
        candidate_memory_version=str(memory_snapshot["candidate_memory_version"]),
        accepted_memory_candidate_version=str(accepted_memory["accepted_memory_candidate_version"]),
        reviewed_memory_candidate_version=reviewed_relation_version,
        review_packet_set_fingerprint=review_packet_set_fingerprint,
        aggregation_policy_fingerprint=(canonical_aggregation_policy_fingerprint()),
        candidate_tiles=candidate_tiles,
        coverage_summary=coverage_summary,
        unresolved_items=unresolved_items,
    )
    resolved_references = {
        field: packet["manifest"][field]
        for field in (
            "candidate_memory_version",
            "accepted_memory_candidate_version",
            "reviewed_memory_candidate_version",
            "review_packet_set_fingerprint",
            "rubric_version",
            "tile_contract_registry_fingerprint",
            "reducer_policy_fingerprint",
            "aggregation_policy_fingerprint",
        )
    }
    resolution = build_reference_resolution(packet, resolved_references)
    return {
        "schema_version": EVIDENCE_VAULT_CANDIDATE_RESOLVER_VERSION,
        "packet": packet,
        "resolved_references": resolved_references,
        "reference_resolution": resolution,
        "runtime_effect": False,
        "authority": False,
        "scanner_runtime_effect": False,
        "production_runtime_effect": False,
    }


def _resolve_accepted_mapping(
    mapping: dict[str, Any],
    *,
    review: dict[str, Any],
    accepted_evidence: dict[str, dict[str, Any]],
    snapshot_evidence_ids: set[str],
    snapshot_claim_ids: set[str],
    basis_by_tile: dict[str, list[dict[str, Any]]],
    unresolved_items: list[dict[str, Any]],
    unresolved_by_tile: dict[str, list[str]],
) -> None:
    mapping_id = _sha256(mapping.get("mapping_id"), field="mapping_id")
    evidence_id = _sha256(
        mapping.get("source_evidence_id"),
        field="source_evidence_id",
    )
    claim_id = _sha256(
        mapping.get("claim_variant_id"),
        field="claim_variant_id",
    )
    tile_id = str(mapping.get("tile_id") or "")
    polarity = str(mapping.get("polarity") or "")
    if polarity == "weakens":
        _add_unresolved(
            unresolved_items,
            unresolved_by_tile,
            tile_id=tile_id,
            kind="ambiguous_legacy_polarity",
            blocking=True,
            details={"mapping_id": mapping_id, "polarity": polarity},
        )
        return
    if polarity == "insufficient_evidence":
        return
    if polarity != "supports":
        raise EvidenceVaultCandidateResolverError(f"unsupported accepted mapping polarity: {polarity}")
    accepted = accepted_evidence.get(evidence_id)
    if accepted is None or evidence_id not in snapshot_evidence_ids:
        _add_unresolved(
            unresolved_items,
            unresolved_by_tile,
            tile_id=tile_id,
            kind="accepted_mapping_evidence_not_accepted",
            blocking=True,
            details={
                "mapping_id": mapping_id,
                "evidence_id": evidence_id,
            },
        )
        return
    if claim_id not in snapshot_claim_ids:
        _add_unresolved(
            unresolved_items,
            unresolved_by_tile,
            tile_id=tile_id,
            kind="mapping_claim_unresolvable",
            blocking=True,
            details={
                "mapping_id": mapping_id,
                "claim_variant_id": claim_id,
            },
        )
        return
    source_identity_id = _sha256(
        accepted.get("document_id"),
        field="accepted_evidence.document_id",
    )
    event_id = str(review.get("event_id") or review.get("id") or "").strip()
    if not event_id:
        raise EvidenceVaultCandidateResolverError("accepted mapping review_event_id is required")
    basis_by_tile[tile_id].append(
        {
            "relation_id": mapping_id,
            "evidence_id": evidence_id,
            "source_identity_id": source_identity_id,
            "claim_id": claim_id,
            "polarity": "supports",
            "review_status": "accepted",
            "decision_event_id": event_id,
            "absence_test_contract_id": None,
            "coverage_assessment_id": None,
            "coverage_status": None,
            "tested_scope": None,
            "observed_result": None,
        }
    )


def _add_pending_mapping_prerequisites(
    mapping: dict[str, Any],
    *,
    review_state: str,
    accepted_evidence: dict[str, dict[str, Any]],
    snapshot_evidence_ids: set[str],
    snapshot_claim_ids: set[str],
    unresolved_items: list[dict[str, Any]],
    unresolved_by_tile: dict[str, list[str]],
) -> None:
    """Expose downstream prerequisites before a mapping decision is accepted."""

    mapping_id = _sha256(mapping.get("mapping_id"), field="mapping_id")
    evidence_id = _sha256(
        mapping.get("source_evidence_id"),
        field="source_evidence_id",
    )
    claim_id = _sha256(
        mapping.get("claim_variant_id"),
        field="claim_variant_id",
    )
    tile_id = str(mapping.get("tile_id") or "")
    if evidence_id not in accepted_evidence or evidence_id not in snapshot_evidence_ids:
        _add_unresolved(
            unresolved_items,
            unresolved_by_tile,
            tile_id=tile_id,
            kind="pending_mapping_evidence_identity_not_accepted",
            blocking=True,
            details={
                "mapping_id": mapping_id,
                "evidence_id": evidence_id,
                "review_state": review_state,
            },
        )
    if claim_id not in snapshot_claim_ids:
        _add_unresolved(
            unresolved_items,
            unresolved_by_tile,
            tile_id=tile_id,
            kind="pending_mapping_claim_unresolvable",
            blocking=True,
            details={
                "mapping_id": mapping_id,
                "claim_variant_id": claim_id,
                "review_state": review_state,
            },
        )


def _resolve_direct_tile_relations(
    recovery_memory: dict[str, Any],
    *,
    recovery_review_rows: list[dict[str, Any]],
    accepted_evidence: dict[str, dict[str, Any]],
    snapshot_evidence_ids: set[str],
    claim_routed_pairs: set[tuple[str, str]],
    basis_by_tile: dict[str, list[dict[str, Any]]],
    unresolved_items: list[dict[str, Any]],
    unresolved_by_tile: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Resolve claim-optional direct relations without granting authority.

    Current scanner evidence and historical recoveries use the same durable
    semantic-review journal.  Only an accepted exact relation can become tile
    basis, and its underlying evidence identity must already be accepted.
    """

    semantic_by_relation: dict[str, dict[str, Any]] = {}
    recovery_reviews_by_relation: dict[str, dict[str, Any]] = {}
    events_by_case = {
        str(event.get("case_id") or ""): event for event in recovery_review_rows if str(event.get("case_id") or "")
    }
    evaluated_by_case = {
        str(row.get("case_id") or ""): row
        for row in recovery_memory.get("recovery_review", {}).get("evaluated") or []
        if isinstance(row, dict) and str(row.get("case_id") or "")
    }
    for candidate in recovery_memory.get("recovery_review_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        case_id = str(candidate.get("case_id") or "")
        tile = candidate.get("tile") if isinstance(candidate.get("tile"), dict) else {}
        tile_id = str(tile.get("tile_id") or "")
        evaluated = evaluated_by_case.get(case_id) or {}
        event = events_by_case.get(case_id)
        if event is not None and str(event.get("candidate_fingerprint") or "") != str(
            candidate.get("candidate_fingerprint") or ""
        ):
            event = None
        evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
        direct_source_evidence_ids = _direct_source_evidence_ids(
            evidence,
            tile_id=tile_id,
            claim_routed_pairs=claim_routed_pairs,
        )
        if not direct_source_evidence_ids:
            continue
        review_scope = (
            "current_direct_relation"
            if any(
                isinstance(context, dict)
                and context.get("review_scope") in {
                    "current_direct_relation",
                    "mapper_omission_supplement",
                }
                for context in candidate.get("contexts") or []
            )
            else "historical_recovery"
        )
        if event is not None:
            tile_evidence_id = _sha256(
                evidence.get("tile_evidence_id"),
                field="tile_evidence_id",
            )
            for evidence_id in direct_source_evidence_ids:
                relation_id = _direct_relation_id(
                    tile_id=tile_id,
                    tile_evidence_id=tile_evidence_id,
                    evidence_id=evidence_id,
                )
                recovery_reviews_by_relation[relation_id] = event
        decision = str(evaluated.get("decision") or "pending")
        current_event_decision = str(evaluated.get("current_event_decision") or "pending")
        if decision == "accepted":
            if event is None:
                raise EvidenceVaultCandidateResolverError(
                    "accepted direct relation has no current review event"
                )
            if str(tile.get("proposed_state") or "") != "ok":
                _add_unresolved(
                    unresolved_items,
                    unresolved_by_tile,
                    tile_id=tile_id,
                    kind="unsupported_direct_relation_state",
                    blocking=True,
                    details={
                        "case_id": case_id,
                        "proposed_state": str(tile.get("proposed_state") or ""),
                    },
                )
                continue
            _resolve_direct_entry(
                evidence,
                tile_id=tile_id,
                review=event,
                accepted_evidence=accepted_evidence,
                snapshot_evidence_ids=snapshot_evidence_ids,
                claim_routed_pairs=claim_routed_pairs,
                basis_by_tile=basis_by_tile,
                semantic_by_relation=semantic_by_relation,
                unresolved_items=unresolved_items,
                unresolved_by_tile=unresolved_by_tile,
            )
        elif decision == "pending" and current_event_decision != "revoked":
            _add_unresolved(
                unresolved_items,
                unresolved_by_tile,
                tile_id=tile_id,
                kind=(
                    "direct_tile_relation_review_pending"
                    if review_scope == "current_direct_relation"
                    else "direct_recovery_review_pending"
                ),
                blocking=True,
                details={
                    "case_id": case_id,
                    "candidate_fingerprint": str(candidate.get("candidate_fingerprint") or ""),
                },
            )
            _add_direct_relation_identity_prerequisite(
                evidence,
                tile_id=tile_id,
                accepted_evidence=accepted_evidence,
                snapshot_evidence_ids=snapshot_evidence_ids,
                claim_routed_pairs=claim_routed_pairs,
                unresolved_items=unresolved_items,
                unresolved_by_tile=unresolved_by_tile,
            )
        elif decision == "disputed":
            _add_unresolved(
                unresolved_items,
                unresolved_by_tile,
                tile_id=tile_id,
                kind=(
                    "direct_tile_relation_review_disputed"
                    if review_scope == "current_direct_relation"
                    else "direct_recovery_review_disputed"
                ),
                blocking=True,
                details={
                    "case_id": case_id,
                    "candidate_fingerprint": str(
                        candidate.get("candidate_fingerprint") or ""
                    ),
                },
            )
            _add_direct_relation_identity_prerequisite(
                evidence,
                tile_id=tile_id,
                accepted_evidence=accepted_evidence,
                snapshot_evidence_ids=snapshot_evidence_ids,
                claim_routed_pairs=claim_routed_pairs,
                unresolved_items=unresolved_items,
                unresolved_by_tile=unresolved_by_tile,
            )

    return (
        [semantic_by_relation[key] for key in sorted(semantic_by_relation)],
        recovery_reviews_by_relation,
    )


def _resolve_direct_entry(
    entry: dict[str, Any],
    *,
    review: dict[str, Any],
    accepted_evidence: dict[str, dict[str, Any]],
    snapshot_evidence_ids: set[str],
    claim_routed_pairs: set[tuple[str, str]],
    basis_by_tile: dict[str, list[dict[str, Any]]],
    semantic_by_relation: dict[str, dict[str, Any]],
    unresolved_items: list[dict[str, Any]],
    unresolved_by_tile: dict[str, list[str]],
    tile_id: str | None = None,
) -> None:
    resolved_tile_id = str(tile_id or entry.get("tile_id") or "")
    tile_evidence_id = _sha256(
        entry.get("tile_evidence_id"),
        field="tile_evidence_id",
    )
    direct_source_evidence_ids = _direct_source_evidence_ids(
        entry,
        tile_id=resolved_tile_id,
        claim_routed_pairs=claim_routed_pairs,
    )
    if not direct_source_evidence_ids:
        return
    unresolved_source_ids = [
        evidence_id
        for evidence_id in direct_source_evidence_ids
        if evidence_id not in accepted_evidence or evidence_id not in snapshot_evidence_ids
    ]
    if unresolved_source_ids:
        _add_unresolved(
            unresolved_items,
            unresolved_by_tile,
            tile_id=resolved_tile_id,
            kind="direct_tile_evidence_identity_not_accepted",
            blocking=True,
            details={
                "tile_evidence_id": tile_evidence_id,
                "source_evidence_ids": unresolved_source_ids,
            },
        )
        return

    review_status = "accepted"
    decision_event_id = str(review.get("event_id") or review.get("id") or "").strip()
    if not decision_event_id:
        raise EvidenceVaultCandidateResolverError("accepted direct relation review_event_id is required")
    for evidence_id in direct_source_evidence_ids:
        source_identity_id = _sha256(
            accepted_evidence[evidence_id].get("document_id"),
            field="accepted_evidence.document_id",
        )
        relation_id = _direct_relation_id(
            tile_id=resolved_tile_id,
            tile_evidence_id=tile_evidence_id,
            evidence_id=evidence_id,
        )
        basis = {
            "relation_id": relation_id,
            "evidence_id": evidence_id,
            "source_identity_id": source_identity_id,
            "claim_id": None,
            "polarity": "supports",
            "review_status": review_status,
            "decision_event_id": decision_event_id,
            "absence_test_contract_id": None,
            "coverage_assessment_id": None,
            "coverage_status": None,
            "tested_scope": None,
            "observed_result": None,
        }
        _upsert_basis(basis_by_tile[resolved_tile_id], basis)
        semantic_by_relation[relation_id] = {
            "relation_id": relation_id,
            "evidence_id": evidence_id,
            "source_identity_id": source_identity_id,
            "tile_id": resolved_tile_id,
            "tile_evidence_id": tile_evidence_id,
            "polarity": "supports",
            "review_status": review_status,
        }


def _direct_source_evidence_ids(
    entry: dict[str, Any],
    *,
    tile_id: str,
    claim_routed_pairs: set[tuple[str, str]],
) -> list[str]:
    source_evidence_ids = sorted(
        {
            _sha256(value, field="source_evidence_id")
            for value in entry.get("source_evidence_ids") or []
        }
    )
    if not source_evidence_ids:
        raise EvidenceVaultCandidateResolverError(
            "direct tile evidence requires source evidence"
        )
    return [
        evidence_id
        for evidence_id in source_evidence_ids
        if (tile_id, evidence_id) not in claim_routed_pairs
    ]


def _add_direct_relation_identity_prerequisite(
    entry: dict[str, Any],
    *,
    tile_id: str,
    accepted_evidence: dict[str, dict[str, Any]],
    snapshot_evidence_ids: set[str],
    claim_routed_pairs: set[tuple[str, str]],
    unresolved_items: list[dict[str, Any]],
    unresolved_by_tile: dict[str, list[str]],
) -> None:
    direct_source_evidence_ids = _direct_source_evidence_ids(
        entry,
        tile_id=tile_id,
        claim_routed_pairs=claim_routed_pairs,
    )
    unresolved_source_ids = [
        evidence_id
        for evidence_id in direct_source_evidence_ids
        if evidence_id not in accepted_evidence
        or evidence_id not in snapshot_evidence_ids
    ]
    if not unresolved_source_ids:
        return
    _add_unresolved(
        unresolved_items,
        unresolved_by_tile,
        tile_id=tile_id,
        kind="direct_tile_evidence_identity_not_accepted",
        blocking=True,
        details={
            "tile_evidence_id": _sha256(
                entry.get("tile_evidence_id"),
                field="tile_evidence_id",
            ),
            "source_evidence_ids": unresolved_source_ids,
        },
    )


def _direct_relation_id(
    *,
    tile_id: str,
    tile_evidence_id: str,
    evidence_id: str,
) -> str:
    return canonical_fingerprint(
        EVIDENCE_VAULT_DIRECT_TILE_RELATION_VERSION,
        {
            "tile_id": tile_id,
            "tile_evidence_id": tile_evidence_id,
            "evidence_id": evidence_id,
        },
    )


def _upsert_basis(rows: list[dict[str, Any]], basis: dict[str, Any]) -> None:
    relation_id = str(basis["relation_id"])
    for index, row in enumerate(rows):
        if str(row.get("relation_id") or "") == relation_id:
            if row.get("review_status") != "accepted" or basis["review_status"] == "accepted":
                rows[index] = basis
            return
    rows.append(basis)


def _baseline_tiles(
    basis_by_tile: dict[str, list[dict[str, Any]]],
    *,
    unresolved_by_tile: dict[str, list[str]],
) -> list[dict[str, Any]]:
    return [
        build_candidate_tile(
            tile_id=str(row["tile_id"]),
            basis=basis_by_tile.get(str(row["tile_id"]), []),
            unresolved_refs=unresolved_by_tile.get(
                str(row["tile_id"]),
                [],
            ),
        )
        for row in build_tile_contract_registry()["tiles"]
    ]


def _incremental_tiles(
    current: dict[str, Any],
    *,
    basis_by_tile: dict[str, list[dict[str, Any]]],
    unresolved_items: list[dict[str, Any]],
    unresolved_by_tile: dict[str, list[str]],
    reviews_by_mapping: dict[str, dict[str, Any]],
    adjudications_by_evidence: dict[str, dict[str, Any]],
    recovery_reviews_by_relation: dict[str, dict[str, Any]],
    coverage_loss_relation_ids: set[str],
    ledger_state_fingerprint: str,
) -> list[dict[str, Any]]:
    previous_tiles = [
        build_candidate_tile(
            tile_id=str(tile["tile_id"]),
            basis=tile.get("basis") or [],
            coverage_refs=tile.get("coverage_refs") or [],
            unresolved_refs=tile.get("unresolved_refs") or [],
        )
        for tile in current["content"]["tiles"]
    ]
    updates: list[dict[str, Any]] = []
    for previous in previous_tiles:
        tile_id = str(previous["tile_id"])
        previous_by_relation = {str(row["relation_id"]): row for row in previous["basis"]}
        desired_by_relation = {str(row["relation_id"]): row for row in basis_by_tile.get(tile_id, [])}
        missing_relation_ids = set(previous_by_relation) - set(desired_by_relation)
        invalidations: list[dict[str, Any]] = []
        unverified_missing: set[str] = set()
        for relation_id in sorted(missing_relation_ids):
            prior = previous_by_relation[relation_id]
            event = _deprecation_event(
                relation_id,
                evidence_id=str(prior["evidence_id"]),
                reviews_by_mapping=reviews_by_mapping,
                adjudications_by_evidence=adjudications_by_evidence,
                recovery_reviews_by_relation=recovery_reviews_by_relation,
            )
            if event is None:
                unverified_missing.add(relation_id)
                desired_by_relation[relation_id] = prior
                continue
            invalidations.append(_invalidation_basis(prior, event=event))

        new_relation_ids = set(desired_by_relation) - set(previous_by_relation)
        effective_basis = [desired_by_relation[key] for key in sorted(desired_by_relation)] + invalidations
        tile_unresolved = list(unresolved_by_tile.get(tile_id, []))
        coverage_unresolved_id: str | None = None
        if unverified_missing:
            coverage_unresolved_id = _add_unresolved(
                unresolved_items,
                unresolved_by_tile,
                tile_id=tile_id,
                kind="canonical_basis_not_reacquired",
                blocking=False,
                details={
                    "relation_ids": sorted(unverified_missing),
                },
            )
            tile_unresolved.append(coverage_unresolved_id)

        blocking_ids = {str(item["unresolved_id"]) for item in unresolved_items if item["blocking"] is True}
        blocking_unresolved = sorted(ref for ref in tile_unresolved if ref in blocking_ids)
        promotable_unresolved = blocking_unresolved
        lost_coverage = bool(set(previous_by_relation) & coverage_loss_relation_ids) or bool(unverified_missing)
        if invalidations:
            updates.append(
                {
                    "tile_id": tile_id,
                    "delta_kind": "verified_deprecation",
                    "basis": effective_basis,
                    "unresolved_refs": promotable_unresolved,
                }
            )
        elif new_relation_ids:
            preview = build_candidate_tile(
                tile_id=tile_id,
                basis=effective_basis,
            )
            if preview["candidate_state"] == "contradiction":
                delta_kind = "contradiction"
            elif preview["candidate_state"] != previous["candidate_state"]:
                delta_kind = "candidate_update"
            elif _adds_independent_source(
                previous_basis=previous["basis"],
                candidate_basis=effective_basis,
                new_relation_ids=new_relation_ids,
            ):
                delta_kind = "strengthened"
            else:
                delta_kind = None
            if delta_kind is not None:
                updates.append(
                    {
                        "tile_id": tile_id,
                        "delta_kind": delta_kind,
                        "basis": effective_basis,
                        "unresolved_refs": promotable_unresolved,
                    }
                )
        elif lost_coverage:
            coverage_ref = canonical_fingerprint(
                EVIDENCE_VAULT_UNRESOLVED_VERSION,
                {
                    "kind": "coverage_loss",
                    "ledger_state_fingerprint": ledger_state_fingerprint,
                    "tile_id": tile_id,
                    "relation_ids": sorted(
                        set(previous_by_relation) & (coverage_loss_relation_ids | unverified_missing)
                    ),
                },
            )
            updates.append(
                {
                    "tile_id": tile_id,
                    "delta_kind": "coverage_loss",
                    "coverage_refs": [coverage_ref],
                    "unresolved_refs": tile_unresolved,
                }
            )
    try:
        return build_incremental_candidate_tiles(
            previous_candidate_tiles=previous_tiles,
            tile_updates=updates,
        )
    except EvidenceVaultCanonicalCoreError as exc:
        raise EvidenceVaultCandidateResolverError("durable inputs cannot evolve the current canonical tiles") from exc


def _adds_independent_source(
    *,
    previous_basis: list[dict[str, Any]],
    candidate_basis: list[dict[str, Any]],
    new_relation_ids: set[str],
) -> bool:
    previous_source_ids = {
        str(row["source_identity_id"])
        for row in previous_basis
        if row["polarity"] in {"supports", "contradicts", "demonstrates_absence"}
    }
    return any(
        str(row["relation_id"]) in new_relation_ids
        and row["polarity"] in {"supports", "contradicts", "demonstrates_absence"}
        and str(row["source_identity_id"]) not in previous_source_ids
        for row in candidate_basis
    )


def _deprecation_event(
    relation_id: str,
    *,
    evidence_id: str,
    reviews_by_mapping: dict[str, dict[str, Any]],
    adjudications_by_evidence: dict[str, dict[str, Any]],
    recovery_reviews_by_relation: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    mapping_event = reviews_by_mapping.get(relation_id)
    if str((mapping_event or {}).get("decision") or "") in {
        "rejected",
        "revoked",
    }:
        return mapping_event
    evidence_event = adjudications_by_evidence.get(evidence_id)
    if str((evidence_event or {}).get("decision") or "") in {
        "rejected",
        "revoked",
    }:
        return evidence_event
    recovery_event = recovery_reviews_by_relation.get(relation_id)
    if str((recovery_event or {}).get("decision") or "") in {
        "rejected",
        "revoked",
    }:
        return recovery_event
    return None


def _invalidation_basis(
    prior: dict[str, Any],
    *,
    event: dict[str, Any],
) -> dict[str, Any]:
    event_id = str(event.get("event_id") or event.get("id") or "").strip()
    if not event_id:
        raise EvidenceVaultCandidateResolverError("verified deprecation event_id is required")
    relation_id = canonical_fingerprint(
        EVIDENCE_VAULT_VERIFIED_DEPRECATION_VERSION,
        {
            "prior_relation_id": prior["relation_id"],
            "decision_event_id": event_id,
            "decision": str(event.get("decision") or ""),
        },
    )
    return {
        **prior,
        "relation_id": relation_id,
        "polarity": "invalidates_candidate",
        "review_status": "accepted",
        "decision_event_id": event_id,
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _reviewed_memory(
    ledger: dict[str, Any],
    reviews: list[dict[str, Any]],
    *,
    brand_identity: str,
) -> dict[str, Any]:
    if reviews:
        result = build_reviewed_claim_tile_memory_from_journal_shadow(
            ledger,
            reviews,
            evaluator_version=EVIDENCE_VAULT_CANDIDATE_RESOLVER_VERSION,
        )
        if result.get("schema_version") != (EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_VERSION):
            raise EvidenceVaultCandidateResolverError("reviewed mapping projection version mismatch")
        return result
    mappings = _latest_mappings(ledger)
    reviewed_version = stable_artifact_digest(
        EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_SEMANTIC_VERSION,
        {
            "brand_domain": brand_identity,
            "accepted_claim_tile_mappings": [],
        },
    )
    packet_set_fingerprint = stable_artifact_digest(
        EVIDENCE_VAULT_CLAIM_TILE_REVIEW_PACKET_SET_VERSION,
        [],
    )
    result = {
        "schema_version": EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_VERSION,
        "brand": {
            "name": str((ledger.get("brand") or {}).get("name") or ""),
            "domain": brand_identity,
        },
        "reviewed_memory_candidate_version": reviewed_version,
        "review_packet_set_fingerprint": packet_set_fingerprint,
        "review_packet_fingerprints": [],
        "reviewed_mappings": [],
        "accepted_mappings": [],
        "selection_ready": not mappings,
        "summary": {
            "candidate_mapping_count": len(mappings),
            "reviewed_mapping_count": 0,
            "pending_mapping_count": len(mappings),
            "accepted_mapping_count": 0,
        },
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
    }
    result["state_fingerprint"] = canonical_fingerprint(
        EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_VERSION,
        result,
    )
    return result


def _validate_ledger(ledger: dict[str, Any], *, domain: str) -> None:
    if not isinstance(ledger, dict):
        raise EvidenceVaultCandidateResolverError("claim_tile_ledger must be an object")
    if (
        ledger.get("mode") != "shadow"
        or ledger.get("runtime_effect") is not False
        or ledger.get("authority") is not False
    ):
        raise EvidenceVaultCandidateResolverError("claim-to-tile ledger must remain non-authoritative shadow state")
    ledger_domain = _normalize_domain(str((ledger.get("brand") or {}).get("domain") or ""))
    if ledger_domain != domain:
        raise EvidenceVaultCandidateResolverError("claim-to-tile ledger brand mismatch")
    latest_series_id = str(ledger.get("latest_mapping_series_id") or "")
    matching_series = [
        series
        for series in ledger.get("mapping_series") or []
        if isinstance(series, dict)
        and (not latest_series_id or str(series.get("mapping_series_id") or "") == latest_series_id)
    ]
    rubric_versions = {
        str(series.get("rubric_version") or "") for series in matching_series if str(series.get("rubric_version") or "")
    }
    if rubric_versions and rubric_versions != {RUBRIC_VERSION}:
        raise EvidenceVaultCandidateResolverError("claim-to-tile ledger rubric version mismatch")


def _latest_mappings(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    latest_series_id = str(ledger.get("latest_mapping_series_id") or "")
    rows = [
        dict(mapping)
        for mapping in ledger.get("mappings") or []
        if isinstance(mapping, dict)
        and (not latest_series_id or str(mapping.get("mapping_series_id") or "") == latest_series_id)
    ]
    rows.sort(key=lambda mapping: str(mapping.get("mapping_id") or ""))
    return rows


def _validate_projection_brand(
    projection: dict[str, Any],
    *,
    domain: str,
    label: str,
) -> None:
    projection_domain = _normalize_domain(str((projection.get("brand") or {}).get("domain") or ""))
    if projection_domain != domain:
        raise EvidenceVaultCandidateResolverError(f"{label} brand mismatch")
    if projection.get("runtime_effect") is not False:
        raise EvidenceVaultCandidateResolverError(f"{label} has runtime effect")
    if projection.get("authority") is not False:
        raise EvidenceVaultCandidateResolverError(f"{label} has authority")
    if label == "memory snapshot" and projection.get("schema_version") != EVIDENCE_MEMORY_SNAPSHOT_VERSION:
        raise EvidenceVaultCandidateResolverError("memory snapshot version mismatch")
    if label == "accepted evidence memory" and projection.get("schema_version") != EVIDENCE_ACCEPTED_MEMORY_VERSION:
        raise EvidenceVaultCandidateResolverError("accepted evidence memory version mismatch")
    if (
        label == "reviewed scoring memory"
        and projection.get("reviewed_shadow_version") != EVIDENCE_SCORING_REVIEWED_SHADOW_VERSION
    ):
        raise EvidenceVaultCandidateResolverError("reviewed scoring memory version mismatch")


def _validate_current_memory(
    current: dict[str, Any] | None,
    *,
    domain: str,
) -> dict[str, Any] | None:
    if current is None:
        return None
    if not isinstance(current, dict):
        raise EvidenceVaultCandidateResolverError("current canonical memory must be an object")
    if (
        current.get("schema_version") != EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION
        or current.get("authority") is not True
        or current.get("authority_scope") != "b3s-vault"
        or current.get("production_runtime_effect") is not False
        or current.get("scanner_runtime_effect") is not False
    ):
        raise EvidenceVaultCandidateResolverError("current canonical memory is not Vault-authoritative")
    content = current.get("content")
    if not isinstance(content, dict):
        raise EvidenceVaultCandidateResolverError("current canonical memory content is unavailable")
    expected_version = canonical_fingerprint(
        EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION,
        content,
    )
    if current.get("canonical_memory_version") != expected_version:
        raise EvidenceVaultCandidateResolverError("current canonical memory fingerprint mismatch")
    current_domain = _normalize_domain(str(content.get("brand_identity") or ""))
    if current_domain != domain:
        raise EvidenceVaultCandidateResolverError("current canonical memory brand mismatch")
    return current


def _add_unresolved(
    unresolved_items: list[dict[str, Any]],
    unresolved_by_tile: dict[str, list[str]],
    *,
    tile_id: str,
    kind: str,
    blocking: bool,
    details: dict[str, Any],
) -> str:
    unresolved_id = canonical_fingerprint(
        EVIDENCE_VAULT_UNRESOLVED_VERSION,
        {
            "tile_id": tile_id,
            "kind": kind,
            "blocking": blocking,
            "details": details,
        },
    )
    item = {
        "unresolved_id": unresolved_id,
        "kind": kind,
        "blocking": blocking,
        "details": details,
    }
    if all(existing["unresolved_id"] != unresolved_id for existing in unresolved_items):
        unresolved_items.append(item)
    if unresolved_id not in unresolved_by_tile[tile_id]:
        unresolved_by_tile[tile_id].append(unresolved_id)
    return unresolved_id


def _dict_rows(
    values: Iterable[dict[str, Any]],
    *,
    field: str,
) -> list[dict[str, Any]]:
    if isinstance(values, (str, bytes, dict)):
        raise EvidenceVaultCandidateResolverError(f"{field} must be an array")
    try:
        rows = list(values)
    except TypeError as exc:
        raise EvidenceVaultCandidateResolverError(f"{field} must be an array") from exc
    if any(not isinstance(row, dict) for row in rows):
        raise EvidenceVaultCandidateResolverError(f"{field} must contain objects")
    return [dict(row) for row in rows]


def _sha256(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise EvidenceVaultCandidateResolverError(f"{field} must be sha256")
    return text


def _normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = (parsed.hostname or "").strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


__all__ = [
    "EVIDENCE_VAULT_AGGREGATION_POLICY_VERSION",
    "EVIDENCE_VAULT_CANDIDATE_RESOLVER_VERSION",
    "EvidenceVaultCandidateResolverError",
    "build_canonical_aggregation_policy",
    "build_resolved_canonical_memory_candidate",
    "canonical_aggregation_policy_fingerprint",
]
