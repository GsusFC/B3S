"""Versioned authority profiles for automatic operational-memory adoption.

The initial matrix makes a narrow distinction:

* reducing already reviewed evidence-to-tile relations is deterministic and
  may be automated;
* proposing a new semantic relation is not calibrated with the two-brand
  corpus and remains shadow-only;
* Coherencia C8 is explicitly human-required for new relations.

This is authority for a relation/reduction path, not a confidence threshold for
an LLM.  Upgrade and downgrade direction is intentionally absent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Iterable, Mapping

from src.services.evidence_vault_canonical_core import (
    EvidenceVaultCanonicalCoreError,
    TileState,
    build_candidate_tile,
    build_tile_contract_registry,
    canonical_fingerprint,
    reducer_policy_fingerprint,
    tile_contract_registry_fingerprint,
)


EVIDENCE_VAULT_AUTHORITY_MATRIX_VERSION = "evidence-vault-authority-matrix-v1"
EVIDENCE_VAULT_AUTHORITY_DECISION_VERSION = "evidence-vault-authority-decision-v1"
AUTHORITY_CORPUS_VERSION = "evidence-vault-authority-corpus-v1"
AUTHORITY_SPLIT_VERSION = "leave-one-brand-out-v1"
AUTHORITY_LABEL_POLICY_VERSION = "evidence-tile-human-label-policy-v1"
REVIEWED_BASIS_PROFILE_ID = "reviewed-basis-reducer-v1"
SEMANTIC_MAPPING_PROFILE_ID = "semantic-mapping-shadow-v1"
C8_PROFILE_ID = "coherencia-c8-human-v1"


class EvidenceVaultAuthorityProfileError(ValueError):
    """An authority matrix or policy decision is invalid."""


class CalibrationStatus(StrEnum):
    DETERMINISTIC = "deterministic"
    CALIBRATED_POLICY_GUARDED = "calibrated_policy_guarded"
    SHADOW_INSUFFICIENT_DATA = "shadow_insufficient_data"
    KNOWN_FALSE_POSITIVE = "known_false_positive"
    HUMAN_REQUIRED = "human_required"


def build_initial_authority_profile_matrix() -> dict[str, Any]:
    """Build the conservative v1 matrix without claiming false calibration."""

    registry = build_tile_contract_registry()
    profiles = [
        {
            "profile_id": REVIEWED_BASIS_PROFILE_ID,
            "profile_version": "v1",
            "auto_adoption_mode": "deterministic",
            "calibration_status": CalibrationStatus.DETERMINISTIC.value,
            "automatic_authority_enabled": True,
            "scope": "reduction_of_previously_reviewed_relations",
            "predicates": [
                "effective_basis_non_empty",
                "every_effective_relation_reviewed_accepted",
                "every_effective_relation_has_decision_event",
                "candidate_state_matches_versioned_reducer",
                "absence_evidence_passes_registered_tile_contract",
                "candidate_is_not_contradiction",
            ],
            "statistical_calibration_required": False,
            "direction_policy": "neutral",
            "known_false_positive_count": 0,
        },
        {
            "profile_id": SEMANTIC_MAPPING_PROFILE_ID,
            "profile_version": "v1",
            "auto_adoption_mode": "policy_guarded",
            "calibration_status": CalibrationStatus.SHADOW_INSUFFICIENT_DATA.value,
            "automatic_authority_enabled": False,
            "scope": "new_model_proposed_evidence_to_tile_relation",
            "predicates": [
                "source_class_represented_in_calibration",
                "tile_contract_semantically_satisfied",
                "no_tile_specific_false_positive",
                "independent_validation_passed",
                "shadow_replay_passed",
            ],
            "statistical_calibration_required": True,
            "direction_policy": "neutral",
            "known_false_positive_count": 0,
            "reason_disabled": "only_two_reviewed_brands_not_generalizable",
        },
        {
            "profile_id": C8_PROFILE_ID,
            "profile_version": "v1",
            "auto_adoption_mode": "human_required",
            "calibration_status": CalibrationStatus.HUMAN_REQUIRED.value,
            "automatic_authority_enabled": False,
            "scope": "new_relation_for_coherencia_c8",
            "predicates": [
                "experience_or_product_proof_satisfies_c8_contract",
                "semantic_relevance_reviewed",
            ],
            "statistical_calibration_required": True,
            "direction_policy": "neutral",
            "known_false_positive_count": 1,
            "known_false_positive_refs": [
                "soccersolver-c8-literal-substring-drift-2026-08-06"
            ],
            "reason_disabled": "known_literal_but_semantically_invalid_citation",
        },
    ]
    tile_profiles = [
        {
            "component_key": str(tile["component_key"]),
            "tile_id": str(tile["tile_id"]),
            "tile_key": str(tile["tile_key"]),
            "accepted_basis_reduction_profile_id": REVIEWED_BASIS_PROFILE_ID,
            "new_semantic_mapping_profile_id": (
                C8_PROFILE_ID
                if str(tile["tile_key"]) == "coherencia.C8"
                else SEMANTIC_MAPPING_PROFILE_ID
            ),
            "automatic_new_mapping_enabled": False,
            "tile_guardrails": (
                ["human_review_for_every_new_relation"]
                if str(tile["tile_key"]) == "coherencia.C8"
                else []
            ),
        }
        for tile in registry["tiles"]
    ]
    unsigned = {
        "schema_version": EVIDENCE_VAULT_AUTHORITY_MATRIX_VERSION,
        "runtime_effect": False,
        "authority_scope": "b3s-vault",
        "corpus": {
            "corpus_version": AUTHORITY_CORPUS_VERSION,
            "split_version": AUTHORITY_SPLIT_VERSION,
            "label_policy_version": AUTHORITY_LABEL_POLICY_VERSION,
            "brand_ids": ["causa-prima", "soccersolver"],
            "folds": [
                {
                    "design_brand_ids": ["soccersolver"],
                    "validation_brand_ids": ["causa-prima"],
                },
                {
                    "design_brand_ids": ["causa-prima"],
                    "validation_brand_ids": ["soccersolver"],
                },
            ],
            "generalization_claim": False,
            "semantic_calibration_status": (
                CalibrationStatus.SHADOW_INSUFFICIENT_DATA.value
            ),
            "results_by_brand_source_tile": [],
        },
        "performance_criterion": {
            "known_human_rejection_accepted": "fail_immediately",
            "zero_known_rejections_with_insufficient_data": "remain_shadow",
            "requires_diverse_brand_and_source_coverage": True,
            "requires_deterministic_replay": True,
            "model_confidence_sufficient": False,
        },
        "tile_contract_registry_fingerprint": tile_contract_registry_fingerprint(),
        "reducer_policy_fingerprint": reducer_policy_fingerprint(),
        "profiles": profiles,
        "tile_profiles": tile_profiles,
        "summary": {
            "tile_count": len(tile_profiles),
            "automatic_new_mapping_tile_count": 0,
            "shadow_insufficient_data_tile_count": 79,
            "human_required_tile_count": 1,
            "deterministic_reviewed_basis_tile_count": 80,
        },
    }
    return {
        **unsigned,
        "authority_matrix_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_AUTHORITY_MATRIX_VERSION,
            unsigned,
        ),
    }


def evaluate_reviewed_basis_authority(
    *,
    candidate_tile: Mapping[str, Any],
    authority_matrix: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate deterministic adoption from relations already human-reviewed."""

    matrix = dict(authority_matrix or build_initial_authority_profile_matrix())
    validate_authority_profile_matrix(matrix)
    _validate_candidate_reduction(candidate_tile)
    registry_by_id = {
        str(row["tile_id"]): row for row in build_tile_contract_registry()["tiles"]
    }
    tile_id = str(candidate_tile.get("tile_id") or "").strip()
    registry_tile = registry_by_id.get(tile_id)
    if registry_tile is None:
        raise EvidenceVaultAuthorityProfileError(f"unknown candidate tile: {tile_id}")
    candidate_state = str(candidate_tile.get("candidate_state") or "").strip()
    try:
        state = TileState(candidate_state)
    except ValueError as exc:
        raise EvidenceVaultAuthorityProfileError(
            f"invalid candidate state for {tile_id}"
        ) from exc
    basis = [dict(row) for row in candidate_tile.get("basis") or [] if isinstance(row, Mapping)]
    effective = [
        row
        for row in basis
        if str(row.get("polarity") or "")
        in {"supports", "contradicts", "demonstrates_absence"}
    ]
    failures: list[str] = []
    if not effective:
        failures.append("effective_basis_empty")
    if any(str(row.get("review_status") or "") != "accepted" for row in effective):
        failures.append("unreviewed_effective_relation")
    if any(not str(row.get("decision_event_id") or "").strip() for row in effective):
        failures.append("effective_relation_missing_decision_event")
    if state is TileState.CONTRADICTION:
        failures.append("contradiction_requires_review")
    if state is TileState.SIN_EVIDENCIA:
        failures.append("absence_of_effective_basis_is_not_adoptable")
    eligible = not failures
    unsigned = {
        "schema_version": EVIDENCE_VAULT_AUTHORITY_DECISION_VERSION,
        "authority_scope": "b3s-vault",
        "authority_matrix_fingerprint": str(
            matrix["authority_matrix_fingerprint"]
        ),
        "authority_profile_id": REVIEWED_BASIS_PROFILE_ID,
        "tile_id": tile_id,
        "tile_key": str(registry_tile["tile_key"]),
        "candidate_state": state.value,
        "eligible": eligible,
        "decision": "accept" if eligible else "remain_pending",
        "reason_codes": sorted(set(failures)) or [
            "reviewed_basis_reduced_deterministically"
        ],
        "direction_policy": "neutral",
        "effective_relation_ids": sorted(
            str(row.get("relation_id") or "") for row in effective
        ),
    }
    return {
        **unsigned,
        "authority_decision_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_AUTHORITY_DECISION_VERSION,
            unsigned,
        ),
    }


def validate_authority_profile_matrix(matrix: Mapping[str, Any]) -> None:
    if matrix.get("schema_version") != EVIDENCE_VAULT_AUTHORITY_MATRIX_VERSION:
        raise EvidenceVaultAuthorityProfileError("authority matrix schema mismatch")
    fingerprint = str(matrix.get("authority_matrix_fingerprint") or "")
    unsigned = {
        key: matrix[key]
        for key in matrix
        if key != "authority_matrix_fingerprint"
    }
    if fingerprint != canonical_fingerprint(
        EVIDENCE_VAULT_AUTHORITY_MATRIX_VERSION,
        unsigned,
    ):
        raise EvidenceVaultAuthorityProfileError("authority matrix fingerprint mismatch")
    if matrix.get("authority_scope") != "b3s-vault":
        raise EvidenceVaultAuthorityProfileError("authority matrix is not vault-only")
    rows = matrix.get("tile_profiles")
    if not isinstance(rows, list) or len(rows) != 80:
        raise EvidenceVaultAuthorityProfileError(
            "authority matrix must contain exactly 80 tile profiles"
        )
    expected = {
        str(row["tile_id"]) for row in build_tile_contract_registry()["tiles"]
    }
    found = {str(row.get("tile_id") or "") for row in rows if isinstance(row, Mapping)}
    if found != expected:
        raise EvidenceVaultAuthorityProfileError("authority matrix tile set mismatch")
    corpus = matrix.get("corpus")
    if not isinstance(corpus, Mapping):
        raise EvidenceVaultAuthorityProfileError("authority corpus manifest missing")
    for fold in corpus.get("folds") or []:
        design = set(fold.get("design_brand_ids") or [])
        validation = set(fold.get("validation_brand_ids") or [])
        if design & validation:
            raise EvidenceVaultAuthorityProfileError(
                "authority corpus split leaks brands between design and validation"
            )


def validate_authority_decision(
    decision: Mapping[str, Any],
    *,
    candidate_tile: Mapping[str, Any],
) -> None:
    if decision.get("schema_version") != EVIDENCE_VAULT_AUTHORITY_DECISION_VERSION:
        raise EvidenceVaultAuthorityProfileError("authority decision schema mismatch")
    fingerprint = str(decision.get("authority_decision_fingerprint") or "")
    unsigned = {
        key: decision[key]
        for key in decision
        if key != "authority_decision_fingerprint"
    }
    if fingerprint != canonical_fingerprint(
        EVIDENCE_VAULT_AUTHORITY_DECISION_VERSION,
        unsigned,
    ):
        raise EvidenceVaultAuthorityProfileError("authority decision fingerprint mismatch")
    expected = evaluate_reviewed_basis_authority(candidate_tile=candidate_tile)
    if dict(decision) != expected:
        raise EvidenceVaultAuthorityProfileError(
            "authority decision does not match the current deterministic policy"
        )
    if decision.get("eligible") is not True or decision.get("decision") != "accept":
        raise EvidenceVaultAuthorityProfileError("authority decision is not eligible")
    if decision.get("authority_profile_id") != REVIEWED_BASIS_PROFILE_ID:
        raise EvidenceVaultAuthorityProfileError("unsupported automatic authority profile")
    if str(decision.get("tile_id") or "") != str(candidate_tile.get("tile_id") or ""):
        raise EvidenceVaultAuthorityProfileError("authority decision tile mismatch")
    if str(decision.get("candidate_state") or "") != str(
        candidate_tile.get("candidate_state") or ""
    ):
        raise EvidenceVaultAuthorityProfileError("authority decision state mismatch")
    relation_ids = sorted(
        str(row.get("relation_id") or "")
        for row in candidate_tile.get("basis") or []
        if isinstance(row, Mapping)
        and str(row.get("polarity") or "")
        in {"supports", "contradicts", "demonstrates_absence"}
    )
    if decision.get("effective_relation_ids") != relation_ids:
        raise EvidenceVaultAuthorityProfileError("authority decision basis mismatch")


def _validate_candidate_reduction(candidate_tile: Mapping[str, Any]) -> None:
    """Reject policy inputs that do not match the canonical tile reducer."""

    try:
        rebuilt = build_candidate_tile(
            tile_id=str(candidate_tile.get("tile_id") or ""),
            basis=candidate_tile.get("basis") or [],
            previous_canonical_state=candidate_tile.get("previous_canonical_state"),
            delta_kind=str(candidate_tile.get("delta_kind") or "baseline"),
            coverage_refs=candidate_tile.get("coverage_refs") or [],
            unresolved_refs=candidate_tile.get("unresolved_refs") or [],
        )
    except EvidenceVaultCanonicalCoreError as exc:
        raise EvidenceVaultAuthorityProfileError(
            "candidate does not satisfy the canonical reducer"
        ) from exc
    fields = (
        "component_key",
        "tile_id",
        "tile_key",
        "candidate_state",
        "basis",
        "coverage_refs",
        "unresolved_refs",
        "delta_kind",
        "previous_canonical_state",
    )
    if any(candidate_tile.get(field) != rebuilt.get(field) for field in fields):
        raise EvidenceVaultAuthorityProfileError(
            "candidate does not match canonical reducer output"
        )


__all__ = [
    "AUTHORITY_CORPUS_VERSION",
    "AUTHORITY_LABEL_POLICY_VERSION",
    "AUTHORITY_SPLIT_VERSION",
    "C8_PROFILE_ID",
    "CalibrationStatus",
    "EVIDENCE_VAULT_AUTHORITY_DECISION_VERSION",
    "EVIDENCE_VAULT_AUTHORITY_MATRIX_VERSION",
    "EvidenceVaultAuthorityProfileError",
    "REVIEWED_BASIS_PROFILE_ID",
    "SEMANTIC_MAPPING_PROFILE_ID",
    "build_initial_authority_profile_matrix",
    "evaluate_reviewed_basis_authority",
    "validate_authority_decision",
    "validate_authority_profile_matrix",
]
