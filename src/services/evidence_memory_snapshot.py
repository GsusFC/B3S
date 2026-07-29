"""Stable shadow snapshot and evaluation identity for evidence memory.

This module prepares Gate 3 without pretending that a canonical memory or a
memory-derived score already exists. It deliberately separates:

- a candidate semantic memory version, which ignores acquisition repetition,
  latest-presence state, and evaluator-specific mapping series; and
- a shadow evaluation identity, which adds explicit rubric and evaluator
  versions to that candidate memory version.

Neither identifier has runtime, canonical-selection, or scoring authority.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any, Iterable

from src.evidence_identity import stable_artifact_digest
from src.services.evidence_claim_memory import (
    EVIDENCE_CLAIM_MEMORY_POLICY_VERSION,
    EVIDENCE_CLAIM_MEMORY_VERSION,
    build_evidence_claim_memory,
)
from src.services.evidence_claim_reconciliation import (
    EVIDENCE_CLAIM_RECONCILIATION_POLICY_VERSION,
    EVIDENCE_CLAIM_RECONCILIATION_VERSION,
)
from src.services.evidence_claim_tile_ledger import (
    EVIDENCE_CLAIM_TILE_LEDGER_POLICY_VERSION,
    EVIDENCE_CLAIM_TILE_LEDGER_VERSION,
    EVIDENCE_CLAIM_TILE_MAPPING_VERSION,
    build_evidence_claim_tile_ledger,
)
from src.services.evidence_memory_adjudication import (
    EVIDENCE_MEMORY_ADJUDICATION_POLICY_VERSION,
    EVIDENCE_MEMORY_ADJUDICATION_VERSION,
)
from src.services.evidence_memory_identity_v2 import (
    EVIDENCE_MEMORY_IDENTITY_V2_POLICY_VERSION,
    EVIDENCE_MEMORY_IDENTITY_V2_VERSION,
    build_evidence_memory_identity_v2,
)


EVIDENCE_MEMORY_SNAPSHOT_VERSION = "evidence-memory-snapshot-v1"
EVIDENCE_MEMORY_SNAPSHOT_POLICY_VERSION = (
    "evidence-memory-snapshot-policy-v1"
)
EVIDENCE_MEMORY_CANDIDATE_VERSION = (
    "evidence-memory-candidate-semantic-version-v1"
)
EVIDENCE_MEMORY_EVALUATION_IDENTITY_VERSION = (
    "evidence-memory-evaluation-identity-v1"
)
EVIDENCE_MEMORY_SEMANTIC_TILE_MAPPING_VERSION = (
    "evidence-memory-semantic-tile-mapping-v1"
)


class EvidenceMemorySnapshotError(ValueError):
    """The requested snapshot cannot satisfy the versioned contract."""


def build_evidence_memory_snapshot(
    reports: Iterable[dict[str, Any]],
    *,
    rubric_version: str,
    evaluator_version: str,
    mode: str = "shadow",
    evidence_adjudications: Iterable[dict[str, Any]] = (),
    claim_reconciliations: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a stable, non-authoritative candidate memory snapshot.

    The returned ``candidate_memory_version`` is a diagnostic precursor to a
    canonical memory version. ``evaluation_identity`` remains ``None`` until a
    canonical-memory policy exists; ``shadow_evaluation_identity`` lets us
    verify the hash contract without granting that authority.
    """

    normalized_rubric_version = _required_version(
        rubric_version,
        field="rubric_version",
    )
    normalized_evaluator_version = _required_version(
        evaluator_version,
        field="evaluator_version",
    )
    report_rows = [
        report for report in reports if isinstance(report, dict)
    ]
    evidence_adjudication_rows = [
        event
        for event in evidence_adjudications
        if isinstance(event, dict)
    ]
    claim_reconciliation_rows = [
        event
        for event in claim_reconciliations
        if isinstance(event, dict)
    ]
    effective_mode = (
        "shadow"
        if str(mode or "").strip().lower() == "shadow"
        else "disabled"
    )
    projection_versions = _projection_versions()
    empty_semantic_state = _empty_semantic_state()
    result: dict[str, Any] = {
        "schema_version": EVIDENCE_MEMORY_SNAPSHOT_VERSION,
        "policy_version": EVIDENCE_MEMORY_SNAPSHOT_POLICY_VERSION,
        "mode": effective_mode,
        "memory_kind": "shadow_candidate",
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "canonical_memory_available": False,
        "evaluation_ready": False,
        "brand": {"name": "", "domain": ""},
        "report_count": len(report_rows),
        "projection_versions": projection_versions,
        "rubric_version": normalized_rubric_version,
        "evaluator_version": normalized_evaluator_version,
        "candidate_memory_version": None,
        "canonical_memory_version": None,
        "shadow_evaluation_identity": None,
        "evaluation_identity": None,
        "score": None,
        "score_delta": None,
        "summary": _summary(empty_semantic_state),
        "semantic_state": empty_semantic_state,
        "promotion_blockers": [
            "canonical_memory_policy_not_adopted",
            "canonical_claim_selection_unavailable",
            "canonical_tile_mapping_selection_unavailable",
            "score_evaluator_and_result_cache_not_implemented",
        ],
        "warnings": [
            "shadow_candidate_version_is_not_canonical_memory",
            "shadow_evaluation_identity_has_no_scoring_authority",
            "acquisition_presence_and_repeat_counts_are_excluded",
            "evaluator_specific_mapping_series_are_deduplicated",
            "raw_evidence_and_claim_text_are_not_returned",
        ],
    }
    if effective_mode != "shadow":
        result["state_fingerprint"] = _state_fingerprint(result)
        return result

    identity_projection = build_evidence_memory_identity_v2(
        report_rows,
        mode="shadow",
        adjudications=evidence_adjudication_rows,
    )
    claim_projection = build_evidence_claim_memory(
        report_rows,
        mode="shadow",
        evidence_adjudications=evidence_adjudication_rows,
        claim_reconciliations=claim_reconciliation_rows,
    )
    tile_projection = build_evidence_claim_tile_ledger(
        report_rows,
        mode="shadow",
    )
    semantic_state = _semantic_state(
        identity_projection=identity_projection,
        claim_projection=claim_projection,
        tile_projection=tile_projection,
    )
    brand = {
        "name": str(
            (identity_projection.get("brand") or {}).get("name") or ""
        ),
        "domain": str(
            (identity_projection.get("brand") or {}).get("domain") or ""
        ),
    }
    candidate_memory_version = stable_artifact_digest(
        EVIDENCE_MEMORY_CANDIDATE_VERSION,
        {
            "brand": {
                "name": brand["name"].strip().casefold(),
                "domain": brand["domain"].strip().casefold(),
            },
            "projection_versions": projection_versions,
            "semantic_state": semantic_state,
        },
    )
    shadow_evaluation_identity = stable_artifact_digest(
        EVIDENCE_MEMORY_EVALUATION_IDENTITY_VERSION,
        {
            "memory_version": candidate_memory_version,
            "rubric_version": normalized_rubric_version,
            "evaluator_version": normalized_evaluator_version,
        },
    )
    result.update(
        {
            "brand": brand,
            "candidate_memory_version": candidate_memory_version,
            "shadow_evaluation_identity": shadow_evaluation_identity,
            "summary": _summary(semantic_state),
            "semantic_state": semantic_state,
        }
    )
    result["state_fingerprint"] = _state_fingerprint(result)
    return result


def _semantic_state(
    *,
    identity_projection: dict[str, Any],
    claim_projection: dict[str, Any],
    tile_projection: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    evidence = sorted(
        (
            {
                "evidence_id": str(entry.get("evidence_id") or ""),
                "document_id": str(entry.get("document_id") or ""),
                "passage_id": str(entry.get("passage_id") or ""),
                "source_class": str(entry.get("source_class") or ""),
                "evidence_type": str(entry.get("evidence_type") or ""),
                "content_hash": str(entry.get("content_hash") or ""),
                "identity_status": str(
                    entry.get("identity_status") or ""
                ),
                "identity_strength": str(
                    entry.get("identity_strength") or ""
                ),
                "adjudication_state": str(
                    entry.get("adjudication_state") or "proposed"
                ),
            }
            for entry in identity_projection.get("entries") or []
            if isinstance(entry, dict)
            and str(entry.get("evidence_id") or "")
        ),
        key=lambda item: item["evidence_id"],
    )
    claim_slots = sorted(
        (
            {
                "claim_slot_id": str(slot.get("claim_slot_id") or ""),
                "entity_scope": str(slot.get("entity_scope") or ""),
                "claim_types": sorted(
                    str(value)
                    for value in slot.get("claim_types") or []
                    if str(value)
                ),
                "claim_variant_ids": sorted(
                    str(value)
                    for value in slot.get("variant_ids") or []
                    if str(value)
                ),
            }
            for slot in claim_projection.get("slots") or []
            if isinstance(slot, dict)
            and str(slot.get("claim_slot_id") or "")
        ),
        key=lambda item: item["claim_slot_id"],
    )
    claim_variants = sorted(
        (
            {
                "claim_variant_id": str(
                    variant.get("claim_variant_id") or ""
                ),
                "claim_slot_id": str(
                    variant.get("claim_slot_id") or ""
                ),
                "content_hash": str(variant.get("content_hash") or ""),
                "evidence_ids": sorted(
                    str(value)
                    for value in variant.get("evidence_ids") or []
                    if str(value)
                ),
                "identity_statuses": sorted(
                    str(value)
                    for value in variant.get("identity_statuses") or []
                    if str(value)
                ),
                "adjudication_states": sorted(
                    str(value)
                    for value in variant.get("adjudication_states") or []
                    if str(value)
                ),
            }
            for variant in claim_projection.get("variants") or []
            if isinstance(variant, dict)
            and str(variant.get("claim_variant_id") or "")
        ),
        key=lambda item: item["claim_variant_id"],
    )
    claim_relations = sorted(
        (
            {
                "relation_candidate_id": str(
                    relation.get("relation_candidate_id") or ""
                ),
                "relation": str(relation.get("relation") or ""),
                "from_claim_variant_id": str(
                    relation.get("from_claim_variant_id") or ""
                ),
                "to_claim_variant_id": str(
                    relation.get("to_claim_variant_id") or ""
                ),
                "adjudication_state": str(
                    relation.get("adjudication_state") or "proposed"
                ),
            }
            for slot in claim_projection.get("slots") or []
            if isinstance(slot, dict)
            for relation in slot.get("relation_candidates") or []
            if isinstance(relation, dict)
            and str(relation.get("relation_candidate_id") or "")
        ),
        key=lambda item: item["relation_candidate_id"],
    )
    semantic_mappings: dict[str, dict[str, Any]] = {}
    for mapping in tile_projection.get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        material = {
            "source_evidence_id": str(
                mapping.get("source_evidence_id") or ""
            ),
            "claim_variant_id": str(
                mapping.get("claim_variant_id") or ""
            ),
            "tile_key": str(mapping.get("tile_key") or ""),
            "polarity": str(mapping.get("polarity") or ""),
        }
        if not all(material.values()):
            continue
        semantic_mapping_id = stable_artifact_digest(
            EVIDENCE_MEMORY_SEMANTIC_TILE_MAPPING_VERSION,
            material,
        )
        semantic_mappings.setdefault(
            semantic_mapping_id,
            {
                "semantic_mapping_id": semantic_mapping_id,
                **material,
            },
        )
    claim_tile_mappings = [
        semantic_mappings[mapping_id]
        for mapping_id in sorted(semantic_mappings)
    ]
    return {
        "evidence": evidence,
        "claim_slots": claim_slots,
        "claim_variants": claim_variants,
        "claim_relations": claim_relations,
        "claim_tile_mappings": claim_tile_mappings,
    }


def _summary(
    semantic_state: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    evidence = semantic_state["evidence"]
    claim_relations = semantic_state["claim_relations"]
    identity_decisions = Counter(
        str(item["adjudication_state"]) for item in evidence
    )
    relation_decisions = Counter(
        str(item["adjudication_state"]) for item in claim_relations
    )
    return {
        "evidence_count": len(evidence),
        "claim_slot_count": len(semantic_state["claim_slots"]),
        "claim_variant_count": len(semantic_state["claim_variants"]),
        "claim_relation_count": len(claim_relations),
        "claim_tile_mapping_count": len(
            semantic_state["claim_tile_mappings"]
        ),
        "accepted_evidence_decision_count": int(
            identity_decisions["accepted"]
        ),
        "accepted_claim_relation_decision_count": int(
            relation_decisions["accepted"]
        ),
        "reviewed_claim_tile_mapping_count": 0,
        "identity_adjudication_state_counts": dict(
            sorted(identity_decisions.items())
        ),
        "claim_relation_adjudication_state_counts": dict(
            sorted(relation_decisions.items())
        ),
    }


def _projection_versions() -> dict[str, str]:
    return {
        "snapshot_policy_version": (
            EVIDENCE_MEMORY_SNAPSHOT_POLICY_VERSION
        ),
        "candidate_semantic_version": (
            EVIDENCE_MEMORY_CANDIDATE_VERSION
        ),
        "identity_schema_version": EVIDENCE_MEMORY_IDENTITY_V2_VERSION,
        "identity_policy_version": (
            EVIDENCE_MEMORY_IDENTITY_V2_POLICY_VERSION
        ),
        "identity_adjudication_schema_version": (
            EVIDENCE_MEMORY_ADJUDICATION_VERSION
        ),
        "identity_adjudication_policy_version": (
            EVIDENCE_MEMORY_ADJUDICATION_POLICY_VERSION
        ),
        "claim_schema_version": EVIDENCE_CLAIM_MEMORY_VERSION,
        "claim_policy_version": EVIDENCE_CLAIM_MEMORY_POLICY_VERSION,
        "claim_reconciliation_schema_version": (
            EVIDENCE_CLAIM_RECONCILIATION_VERSION
        ),
        "claim_reconciliation_policy_version": (
            EVIDENCE_CLAIM_RECONCILIATION_POLICY_VERSION
        ),
        "claim_tile_schema_version": (
            EVIDENCE_CLAIM_TILE_LEDGER_VERSION
        ),
        "claim_tile_policy_version": (
            EVIDENCE_CLAIM_TILE_LEDGER_POLICY_VERSION
        ),
        "claim_tile_mapping_version": (
            EVIDENCE_CLAIM_TILE_MAPPING_VERSION
        ),
        "semantic_tile_mapping_version": (
            EVIDENCE_MEMORY_SEMANTIC_TILE_MAPPING_VERSION
        ),
    }


def _empty_semantic_state() -> dict[str, list[dict[str, Any]]]:
    return {
        "evidence": [],
        "claim_slots": [],
        "claim_variants": [],
        "claim_relations": [],
        "claim_tile_mappings": [],
    }


def _required_version(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise EvidenceMemorySnapshotError(f"{field} is required")
    return normalized


def _state_fingerprint(payload: dict[str, Any]) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
