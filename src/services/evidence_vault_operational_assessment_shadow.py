"""Pure 80-tile semantic shadow assessment for operational Vault packets.

This is deliberately a non-authoritative adapter.  It binds a complete
operational packet to its exact complete source candidate packet, sends only
the candidate semantic vector to the neutral SV9 kernel, and exposes authority
coverage and verification requirements as separate observations.  It neither
persists nor activates anything.
"""

from __future__ import annotations

from typing import Any, Mapping

from src.services.evidence_vault_canonical_core import (
    EvidenceVaultCanonicalCoreError,
    canonical_fingerprint,
    validate_candidate_packet,
)
from src.services.evidence_vault_operational_authority import (
    EvidenceVaultOperationalAuthorityError,
    validate_operational_memory_packet,
)
from src.sv9.assessment_kernel import (
    Sv9AssessmentError,
    build_sv9_assessment,
    validate_sv9_assessment_output,
)
from src.sv9.rubric import COMPONENTS


EVIDENCE_VAULT_OPERATIONAL_SEMANTIC_ASSESSMENT_SHADOW_VERSION = (
    "evidence-vault-operational-semantic-assessment-shadow-v1"
)
EVIDENCE_VAULT_OPERATIONAL_SEMANTIC_PROVENANCE_VERSION = (
    "evidence-vault-operational-semantic-provenance-v1"
)
EVIDENCE_VAULT_OPERATIONAL_VERIFICATION_REQUIREMENTS_VERSION = (
    "evidence-vault-operational-verification-requirements-v1"
)

_OUTPUT_FIELDS = frozenset(
    {
        "schema_version",
        "authority",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "source_candidate_packet_fingerprint",
        "candidate_overlay_version",
        "assessment_status",
        "reason",
        "assessment_output",
        "semantic_provenance_fingerprint",
        "authority_coverage",
        "verification_requirements",
    }
)
_PROJECTION_FIELDS = frozenset(
    {
        "schema_version",
        "accepted_memory_candidate_version",
        "proposed_canonical_memory_version",
        "tile_contract_registry_fingerprint",
        "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint",
        "tiles",
        "coverage",
    }
)
_PROJECTION_TILE_FIELDS = frozenset(
    {
        "component_key",
        "tile_id",
        "tile_key",
        "canonical_semantic_state",
        "effective_scoring_state",
        "canonical_effective_points",
        "candidate_semantic_state",
        "candidate_preview_points",
        "authority_state",
        "review_state",
        "lifecycle_state",
        "score_eligible",
        "has_candidate_overlay",
    }
)
_COVERAGE_FIELDS = frozenset(
    {
        "tile_count",
        "accepted_tile_count",
        "accepted_ok_count",
        "accepted_no_count",
        "accepted_sin_evidencia_count",
        "unresolved_tile_count",
        "pending_initial_tile_count",
        "pending_change_tile_count",
        "contradiction_on_accepted_count",
        "contradiction_on_unresolved_count",
        "contradiction_count",
        "tile_authority_coverage_ratio",
        "score_weight_authority_coverage_ratio",
        "score_completeness",
        "canonical_score_status",
    }
)
_REQUIREMENTS_FIELDS = frozenset({"schema_version", "tile_count", "tiles", "counts"})
_REQUIREMENT_TILE_FIELDS = frozenset(
    {"tile_id", "verification_requirement", "verification_state"}
)
_VERIFICATION_STATES = frozenset(
    {"pending", "verified", "disputed", "stale", "unverifiable"}
)


class EvidenceVaultOperationalAssessmentShadowError(ValueError):
    """An operational packet cannot produce a safely bound shadow assessment."""


def build_operational_semantic_shadow_assessment(
    *,
    operational_packet: Mapping[str, Any],
    source_candidate_packet: Mapping[str, Any],
    expected_parent_canonical_memory_version: str | None = None,
) -> dict[str, Any]:
    """Build a non-persistent semantic assessment from exact 80-tile inputs.

    The source candidate packet is mandatory because an operational packet's
    projection is a derived storage artifact.  Both artifacts must agree before
    semantic states are allowed through to the shared assessment kernel.
    """

    packet, _source, projection, source_by_id = _validated_inputs(
        operational_packet=operational_packet,
        source_candidate_packet=source_candidate_packet,
    )
    assessment_tiles = [
        {
            "component_key": row["component_key"],
            "tile_id": row["tile_id"],
            "tile_key": row["tile_key"],
            "assessment_state": row["candidate_semantic_state"],
        }
        for row in projection["tiles"]
    ]
    coverage = _validated_authority_coverage(
        projection["coverage"],
        projection_tiles=projection["tiles"],
        source_by_id=source_by_id,
    )
    requirements = _verification_requirements(projection["tiles"])
    parent = packet["current_canonical_memory_version"]
    if (
        expected_parent_canonical_memory_version is not None
        and expected_parent_canonical_memory_version != parent
    ):
        return _unavailable_output(
            packet=packet,
            coverage=coverage,
            requirements=requirements,
            reason="stale_candidate_parent",
        )
    if any(row["assessment_state"] == "contradiction" for row in assessment_tiles):
        return _unavailable_output(
            packet=packet,
            coverage=coverage,
            requirements=requirements,
            reason="contradiction_requires_semantic_reassessment",
        )
    try:
        assessment_output = build_sv9_assessment(assessment_tiles)
    except Sv9AssessmentError as exc:
        raise EvidenceVaultOperationalAssessmentShadowError(
            "candidate semantic vector is not a complete SV9 assessment"
        ) from exc
    semantic_provenance_fingerprint = canonical_fingerprint(
        EVIDENCE_VAULT_OPERATIONAL_SEMANTIC_PROVENANCE_VERSION,
        {
            "source_candidate_packet_fingerprint": packet[
                "source_candidate_packet_fingerprint"
            ],
            "tiles": assessment_output["tiles"],
            "tile_contract_registry_fingerprint": projection[
                "tile_contract_registry_fingerprint"
            ],
            "reducer_policy_fingerprint": projection["reducer_policy_fingerprint"],
            "aggregation_policy_fingerprint": projection[
                "aggregation_policy_fingerprint"
            ],
        },
    )
    return {
        "schema_version": EVIDENCE_VAULT_OPERATIONAL_SEMANTIC_ASSESSMENT_SHADOW_VERSION,
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "source_candidate_packet_fingerprint": packet[
            "source_candidate_packet_fingerprint"
        ],
        "candidate_overlay_version": packet["candidate_overlay_version"],
        "assessment_status": "available",
        "reason": None,
        "assessment_output": assessment_output,
        "semantic_provenance_fingerprint": semantic_provenance_fingerprint,
        "authority_coverage": coverage,
        "verification_requirements": requirements,
    }


def validate_operational_semantic_shadow_assessment(
    assessment: Mapping[str, Any],
    *,
    operational_packet: Mapping[str, Any],
    source_candidate_packet: Mapping[str, Any],
    expected_parent_canonical_memory_version: str | None = None,
) -> None:
    """Fail closed unless a result exactly rederives from both source artifacts."""

    if not isinstance(assessment, Mapping) or set(assessment) != _OUTPUT_FIELDS:
        raise EvidenceVaultOperationalAssessmentShadowError(
            "shadow assessment fields mismatch"
        )
    expected = build_operational_semantic_shadow_assessment(
        operational_packet=operational_packet,
        source_candidate_packet=source_candidate_packet,
        expected_parent_canonical_memory_version=expected_parent_canonical_memory_version,
    )
    if dict(assessment) != expected:
        raise EvidenceVaultOperationalAssessmentShadowError(
            "shadow assessment does not match its exact source artifacts"
        )
    if assessment["assessment_output"] is not None:
        try:
            validate_sv9_assessment_output(assessment["assessment_output"])
        except Sv9AssessmentError as exc:
            raise EvidenceVaultOperationalAssessmentShadowError(
                "shadow assessment kernel output is invalid"
            ) from exc


def _validated_inputs(
    *,
    operational_packet: Mapping[str, Any],
    source_candidate_packet: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    if not isinstance(operational_packet, Mapping) or not isinstance(
        source_candidate_packet, Mapping
    ):
        raise EvidenceVaultOperationalAssessmentShadowError(
            "operational and source candidate packets must be objects"
        )
    packet = dict(operational_packet)
    source = dict(source_candidate_packet)
    try:
        validate_operational_memory_packet(packet)
        validate_candidate_packet(source)
    except (EvidenceVaultOperationalAuthorityError, EvidenceVaultCanonicalCoreError) as exc:
        raise EvidenceVaultOperationalAssessmentShadowError(
            "operational or source candidate packet is invalid"
        ) from exc
    projection = packet.get("scoring_projection")
    if not isinstance(projection, Mapping) or set(projection) != _PROJECTION_FIELDS:
        raise EvidenceVaultOperationalAssessmentShadowError("scoring projection fields mismatch")
    projection = dict(projection)
    source_manifest = source["manifest"]
    if (
        packet["source_candidate_packet_fingerprint"]
        != source["candidate_packet_fingerprint"]
        or packet["brand_identity"] != source_manifest["brand_identity"]
        or packet["current_canonical_memory_version"]
        != source_manifest["parent_canonical_memory_version"]
    ):
        raise EvidenceVaultOperationalAssessmentShadowError(
            "operational packet does not bind its exact source candidate parent"
        )
    for field in (
        "tile_contract_registry_fingerprint",
        "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint",
    ):
        if projection[field] != source_manifest[field]:
            raise EvidenceVaultOperationalAssessmentShadowError(
                f"operational projection {field} differs from source candidate"
            )
    rows = projection.get("tiles")
    if not isinstance(rows, list) or len(rows) != 80:
        raise EvidenceVaultOperationalAssessmentShadowError(
            "scoring projection must contain exactly 80 tiles"
        )
    source_by_id = {str(row["tile_id"]): row for row in source["candidate_tiles"]}
    if len(source_by_id) != 80:
        raise EvidenceVaultOperationalAssessmentShadowError(
            "source candidate must contain exactly 80 unique tiles"
        )
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _PROJECTION_TILE_FIELDS:
            raise EvidenceVaultOperationalAssessmentShadowError(
                "scoring projection tile fields mismatch"
            )
        tile_id = str(row.get("tile_id") or "")
        source_row = source_by_id.get(tile_id)
        if tile_id in seen or source_row is None:
            raise EvidenceVaultOperationalAssessmentShadowError(
                "projection has an unknown or duplicate source tile"
            )
        seen.add(tile_id)
        if (
            row.get("component_key") != source_row["component_key"]
            or row.get("tile_key") != source_row["tile_key"]
            or row.get("candidate_semantic_state") != source_row["candidate_state"]
        ):
            raise EvidenceVaultOperationalAssessmentShadowError(
                f"projection semantic state mismatches source candidate for {tile_id}"
            )
        _validate_projection_axes(row)
    if seen != set(source_by_id):
        raise EvidenceVaultOperationalAssessmentShadowError("projection is missing source tiles")
    return packet, source, projection, source_by_id


def _validate_projection_axes(row: Mapping[str, Any]) -> None:
    if row["candidate_semantic_state"] not in {
        "ok",
        "no",
        "sin_evidencia",
        "contradiction",
    }:
        raise EvidenceVaultOperationalAssessmentShadowError("invalid candidate semantic state")
    if row["authority_state"] not in {"pending", "accepted", "rejected"}:
        raise EvidenceVaultOperationalAssessmentShadowError("invalid authority state")
    if row["review_state"] not in {"none", "required", "in_review", "resolved"}:
        raise EvidenceVaultOperationalAssessmentShadowError("invalid review state")
    if row["lifecycle_state"] not in {"active", "superseded"}:
        raise EvidenceVaultOperationalAssessmentShadowError("invalid lifecycle state")
    if not isinstance(row["has_candidate_overlay"], bool) or not isinstance(
        row["score_eligible"], bool
    ):
        raise EvidenceVaultOperationalAssessmentShadowError("invalid projection booleans")


def _validated_authority_coverage(
    coverage: Any,
    *,
    projection_tiles: list[Mapping[str, Any]],
    source_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(coverage, Mapping) or set(coverage) != _COVERAGE_FIELDS:
        raise EvidenceVaultOperationalAssessmentShadowError("authority coverage fields mismatch")
    accepted = [row for row in projection_tiles if row["canonical_semantic_state"] is not None]
    accepted_ids = {str(row["tile_id"]) for row in accepted}
    reopened_ids = {
        str(row["tile_id"])
        for row in projection_tiles
        if row["lifecycle_state"] == "superseded"
    }
    accepted_count = len(accepted)
    states = ("ok", "no", "sin_evidencia")
    if any(row["canonical_semantic_state"] not in states for row in accepted):
        raise EvidenceVaultOperationalAssessmentShadowError("invalid canonical semantic state")
    pending_initial = []
    pending_change = []
    contradiction_accepted = []
    contradiction_unresolved = []
    for row in projection_tiles:
        tile_id = str(row["tile_id"])
        delta_kind = source_by_id[tile_id]["delta_kind"]
        if row["authority_state"] == "pending" and delta_kind not in {
            "no_change",
            "coverage_loss",
        }:
            if tile_id in accepted_ids or tile_id in reopened_ids:
                pending_change.append(row)
            else:
                pending_initial.append(row)
        if row["candidate_semantic_state"] == "contradiction":
            (contradiction_accepted if tile_id in accepted_ids else contradiction_unresolved).append(row)
    accepted_weight = sum(
        int(COMPONENTS[row["component_key"]]["multiplier"]) for row in accepted
    )
    total_weight = sum(
        int(COMPONENTS[row["component_key"]]["multiplier"])
        for row in projection_tiles
    )
    expected = {
        "tile_count": 80,
        "accepted_tile_count": accepted_count,
        "accepted_ok_count": sum(row["canonical_semantic_state"] == "ok" for row in accepted),
        "accepted_no_count": sum(row["canonical_semantic_state"] == "no" for row in accepted),
        "accepted_sin_evidencia_count": sum(
            row["canonical_semantic_state"] == "sin_evidencia" for row in accepted
        ),
        "unresolved_tile_count": 80 - accepted_count,
        "pending_initial_tile_count": len(pending_initial),
        "pending_change_tile_count": len(pending_change),
        "contradiction_on_accepted_count": len(contradiction_accepted),
        "contradiction_on_unresolved_count": len(contradiction_unresolved),
        "contradiction_count": len(contradiction_accepted) + len(contradiction_unresolved),
        "tile_authority_coverage_ratio": round(accepted_count / 80, 6),
        "score_weight_authority_coverage_ratio": round(accepted_weight / total_weight, 6),
        "score_completeness": "complete" if accepted_count == 80 else "partial",
        "canonical_score_status": (
            "pending_reassessment"
            if pending_change or contradiction_accepted
            else "current"
        ),
    }
    if dict(coverage) != expected:
        raise EvidenceVaultOperationalAssessmentShadowError(
            "authority coverage does not match operational projection"
        )
    return expected


def _verification_requirements(
    projection_tiles: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Expose policy requirements and a non-scoring verification observation."""

    tiles: list[dict[str, str]] = []
    for row in projection_tiles:
        tile_id = str(row["tile_id"])
        requirement = (
            "owned_web_plus_external_social"
            if tile_id == "C7"
            else "human_required" if tile_id == "C8" else "ordinary"
        )
        # C7 and C8 need proof/binding unavailable in this projection.  Their
        # policy authority alone therefore never becomes a verification claim.
        # These observations remain wholly outside assessment arithmetic.
        if row["candidate_semantic_state"] == "contradiction":
            state = "disputed"
        elif row["lifecycle_state"] == "superseded":
            state = "stale"
        elif tile_id in {"C7", "C8"}:
            state = "pending"
        elif row["authority_state"] == "accepted":
            state = "verified"
        elif row["authority_state"] == "rejected":
            state = "unverifiable"
        else:
            state = "pending"
        tiles.append(
            {
                "tile_id": tile_id,
                "verification_requirement": requirement,
                "verification_state": state,
            }
        )
    counts = {
        state: sum(row["verification_state"] == state for row in tiles)
        for state in sorted(_VERIFICATION_STATES)
    }
    return {
        "schema_version": EVIDENCE_VAULT_OPERATIONAL_VERIFICATION_REQUIREMENTS_VERSION,
        "tile_count": 80,
        "tiles": tiles,
        "counts": counts,
    }

def _unavailable_output(
    *,
    packet: Mapping[str, Any],
    coverage: Mapping[str, Any],
    requirements: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_VAULT_OPERATIONAL_SEMANTIC_ASSESSMENT_SHADOW_VERSION,
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "source_candidate_packet_fingerprint": packet[
            "source_candidate_packet_fingerprint"
        ],
        "candidate_overlay_version": packet["candidate_overlay_version"],
        "assessment_status": "assessment_unavailable",
        "reason": reason,
        "assessment_output": None,
        "semantic_provenance_fingerprint": None,
        "authority_coverage": dict(coverage),
        "verification_requirements": dict(requirements),
    }


__all__ = [
    "EVIDENCE_VAULT_OPERATIONAL_SEMANTIC_ASSESSMENT_SHADOW_VERSION",
    "EVIDENCE_VAULT_OPERATIONAL_SEMANTIC_PROVENANCE_VERSION",
    "EVIDENCE_VAULT_OPERATIONAL_VERIFICATION_REQUIREMENTS_VERSION",
    "EvidenceVaultOperationalAssessmentShadowError",
    "build_operational_semantic_shadow_assessment",
    "validate_operational_semantic_shadow_assessment",
]
