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
from src.services.evidence_vault_operational_memory import (
    EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION,
    EVIDENCE_VAULT_CANDIDATE_OVERLAY_VERSION,
    EVIDENCE_VAULT_SCORING_PROJECTION_VERSION,
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
_EXPECTED_PARENT_UNSET = object()


class EvidenceVaultOperationalAssessmentShadowError(ValueError):
    """An operational packet cannot produce a safely bound shadow assessment."""


def build_operational_semantic_shadow_assessment(
    *,
    operational_packet: Mapping[str, Any],
    source_candidate_packet: Mapping[str, Any],
    expected_parent_canonical_memory_version: str | None | object = _EXPECTED_PARENT_UNSET,
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
    coverage = dict(projection["coverage"])
    requirements = _verification_requirements(projection["tiles"])
    parent = packet["current_canonical_memory_version"]
    if expected_parent_canonical_memory_version is _EXPECTED_PARENT_UNSET:
        return _unavailable_output(
            packet=packet,
            coverage=coverage,
            requirements=requirements,
            reason="expected_parent_required",
        )
    if expected_parent_canonical_memory_version != parent:
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
            # This identity deliberately excludes packet, basis, review, and
            # authority lineage.  Those are separately bound by the source
            # packet fingerprint; this fingerprint names only the semantics
            # consumed by the kernel and its static semantic contracts.
            "kernel_assessment_fingerprint": assessment_output["assessment_fingerprint"],
            "assessment_vector_version": assessment_output["assessment_vector_version"],
            "scoring_policy_version": assessment_output["scoring_policy_version"],
            "rubric_version": assessment_output["rubric_version"],
            "kernel_tile_contract_registry_fingerprint": assessment_output[
                "tile_contract_registry_fingerprint"
            ],
            "vault_tile_contract_registry_fingerprint": projection[
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
    expected_parent_canonical_memory_version: str | None | object = _EXPECTED_PARENT_UNSET,
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
    """Validate and independently rederive every operational projection axis.

    ``scoring_projection`` is a convenience projection, never an authority
    source.  Its canonical/authority/review/lifecycle axes must be reproduced
    from accepted memory plus the candidate overlay (the only disposition
    records retained in this packet), or the adapter refuses the packet.
    """

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

    source_by_id = {str(row["tile_id"]): row for row in source["candidate_tiles"]}
    if len(source_by_id) != 80:
        raise EvidenceVaultOperationalAssessmentShadowError(
            "source candidate must contain exactly 80 unique tiles"
        )
    expected_projection = _rederive_projection(
        packet=packet,
        source_by_id=source_by_id,
        source_manifest=source_manifest,
    )
    if projection != expected_projection:
        raise EvidenceVaultOperationalAssessmentShadowError(
            "scoring projection does not match accepted memory and candidate overlay"
        )
    return packet, source, expected_projection, source_by_id


def _rederive_projection(
    *,
    packet: Mapping[str, Any],
    source_by_id: Mapping[str, Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    accepted = packet["accepted_memory"]
    overlay = packet["candidate_overlay"]
    if (
        not isinstance(accepted, Mapping)
        or accepted.get("schema_version") != EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION
        or not isinstance(overlay, Mapping)
        or overlay.get("schema_version") != EVIDENCE_VAULT_CANDIDATE_OVERLAY_VERSION
    ):
        raise EvidenceVaultOperationalAssessmentShadowError(
            "accepted memory or candidate overlay schema mismatch"
        )
    parent = packet["current_canonical_memory_version"]
    if (
        accepted.get("brand_identity") != packet["brand_identity"]
        or overlay.get("brand_identity") != packet["brand_identity"]
        or accepted.get("parent_canonical_memory_version") != parent
        or overlay.get("parent_canonical_memory_version") != parent
    ):
        raise EvidenceVaultOperationalAssessmentShadowError(
            "operational memory artifacts do not bind the packet parent"
        )
    for field in (
        "tile_contract_registry_fingerprint",
        "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint",
    ):
        if accepted.get(field) != source_manifest[field]:
            raise EvidenceVaultOperationalAssessmentShadowError(
                f"accepted memory {field} differs from source candidate"
            )

    accepted_by_id = _accepted_tiles_by_id(accepted, source_by_id)
    overlay_by_id = _overlay_tiles_by_id(overlay, source_by_id, accepted_by_id)
    pending_reassessment_ids = _pending_reassessment_ids(
        accepted,
        source_by_id=source_by_id,
        accepted_by_id=accepted_by_id,
        overlay_by_id=overlay_by_id,
    )
    for tile_id, disposition in overlay_by_id.items():
        previous = accepted_by_id.get(tile_id)
        material_change = previous is None or not _same_accepted_content(
            previous, source_by_id[tile_id]
        )
        if not material_change and tile_id not in pending_reassessment_ids:
            raise EvidenceVaultOperationalAssessmentShadowError(
                f"overlay disposition has no candidate change for {tile_id}"
            )

    rows: list[dict[str, Any]] = []
    for tile_id, candidate in source_by_id.items():
        previous = accepted_by_id.get(tile_id)
        disposition = overlay_by_id.get(tile_id)
        pending_reassessment = tile_id in pending_reassessment_ids
        if disposition is None:
            # Without an overlay, the only non-ambiguous authority source is an
            # accepted record created from this exact source packet.  A prior
            # accepted tile plus a later no-change candidate otherwise loses
            # the current disposition and must not be guessed from projection.
            if previous is None or pending_reassessment:
                raise EvidenceVaultOperationalAssessmentShadowError(
                    f"missing disposition source for {tile_id}"
                )
            if not _same_accepted_candidate(previous, candidate) or (
                previous["source_candidate_packet_fingerprint"]
                != packet["source_candidate_packet_fingerprint"]
            ):
                raise EvidenceVaultOperationalAssessmentShadowError(
                    f"ambiguous disposition source for {tile_id}"
                )
            authority_state = "accepted"
            review_state = "none"
        else:
            authority_state = disposition["authority_state"]
            review_state = disposition["review_state"]

        canonical_state = previous["semantic_state"] if previous is not None else None
        effective_state = canonical_state if canonical_state is not None else "sin_evidencia"
        multiplier = int(COMPONENTS[candidate["component_key"]]["multiplier"])
        candidate_state = candidate["candidate_state"]
        rows.append(
            {
                "component_key": candidate["component_key"],
                "tile_id": tile_id,
                "tile_key": candidate["tile_key"],
                "canonical_semantic_state": canonical_state,
                "effective_scoring_state": effective_state,
                "canonical_effective_points": (
                    multiplier if effective_state == "ok" else 0
                ),
                "candidate_semantic_state": candidate_state,
                "candidate_preview_points": (
                    None if candidate_state == "contradiction"
                    else multiplier if candidate_state == "ok" else 0
                ),
                "authority_state": authority_state,
                "review_state": review_state,
                "lifecycle_state": (
                    "superseded" if pending_reassessment else "active"
                ),
                "score_eligible": (
                    not pending_reassessment
                    and previous is not None
                    and effective_state == "ok"
                ),
                "has_candidate_overlay": disposition is not None,
            }
        )
    coverage = _authority_coverage_from_rows(rows, source_by_id)
    return {
        "schema_version": EVIDENCE_VAULT_SCORING_PROJECTION_VERSION,
        "accepted_memory_candidate_version": packet[
            "accepted_memory_candidate_version"
        ],
        "proposed_canonical_memory_version": packet[
            "proposed_canonical_memory_version"
        ],
        "tile_contract_registry_fingerprint": source_manifest[
            "tile_contract_registry_fingerprint"
        ],
        "reducer_policy_fingerprint": source_manifest["reducer_policy_fingerprint"],
        "aggregation_policy_fingerprint": source_manifest[
            "aggregation_policy_fingerprint"
        ],
        "tiles": rows,
        "coverage": coverage,
    }


def _accepted_tiles_by_id(
    accepted: Mapping[str, Any],
    source_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    rows = accepted.get("accepted_tiles")
    if not isinstance(rows, list):
        raise EvidenceVaultOperationalAssessmentShadowError("accepted tiles must be an array")
    fields = {
        "component_key", "tile_id", "tile_key", "semantic_state", "basis",
        "coverage_refs", "unresolved_refs", "source_delta_kind",
        "source_candidate_packet_fingerprint", "authority_profile_id",
        "authority_source", "decision_event_id", "authority_matrix_fingerprint",
        "authority_decision_fingerprint",
    }
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != fields:
            raise EvidenceVaultOperationalAssessmentShadowError("accepted tile fields mismatch")
        tile_id = str(row.get("tile_id") or "")
        source = source_by_id.get(tile_id)
        if tile_id in result or source is None:
            raise EvidenceVaultOperationalAssessmentShadowError("accepted tile is unknown or duplicated")
        if (
            row.get("component_key") != source["component_key"]
            or row.get("tile_key") != source["tile_key"]
            or row.get("semantic_state") not in {"ok", "no", "sin_evidencia"}
            or row.get("authority_source") not in {"policy", "human"}
            or not isinstance(row.get("authority_profile_id"), str)
            or not row["authority_profile_id"]
            or not _is_sha256(row.get("source_candidate_packet_fingerprint"))
        ):
            raise EvidenceVaultOperationalAssessmentShadowError("accepted tile is invalid")
        result[tile_id] = row
    return result


def _overlay_tiles_by_id(
    overlay: Mapping[str, Any],
    source_by_id: Mapping[str, Mapping[str, Any]],
    accepted_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    rows = overlay.get("candidate_tiles")
    if not isinstance(rows, list):
        raise EvidenceVaultOperationalAssessmentShadowError("candidate overlay tiles must be an array")
    fields = {
        "component_key", "tile_id", "tile_key", "semantic_state", "authority_state",
        "review_state", "lifecycle_state", "authority_profile_id", "authority_source",
        "decision_event_id", "basis", "coverage_refs", "unresolved_refs", "delta_kind",
        "previous_canonical_state",
    }
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != fields:
            raise EvidenceVaultOperationalAssessmentShadowError("candidate overlay tile fields mismatch")
        tile_id = str(row.get("tile_id") or "")
        source = source_by_id.get(tile_id)
        if tile_id in result or source is None:
            raise EvidenceVaultOperationalAssessmentShadowError("overlay tile is unknown or duplicated")
        previous = accepted_by_id.get(tile_id)
        if (
            row.get("component_key") != source["component_key"]
            or row.get("tile_key") != source["tile_key"]
            or row.get("semantic_state") != source["candidate_state"]
            or row.get("basis") != source["basis"]
            or row.get("coverage_refs") != source["coverage_refs"]
            or row.get("unresolved_refs") != source["unresolved_refs"]
            or row.get("delta_kind") != source["delta_kind"]
            or row.get("previous_canonical_state") != (
                previous["semantic_state"] if previous is not None else None
            )
            or row.get("authority_state") not in {"pending", "rejected"}
            or row.get("review_state") not in {"none", "required", "in_review", "resolved"}
            or row.get("lifecycle_state") != "active"
        ):
            raise EvidenceVaultOperationalAssessmentShadowError("candidate overlay tile is invalid")
        result[tile_id] = row
    return result


def _pending_reassessment_ids(
    accepted: Mapping[str, Any],
    *,
    source_by_id: Mapping[str, Mapping[str, Any]],
    accepted_by_id: Mapping[str, Mapping[str, Any]],
    overlay_by_id: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    rows = accepted.get("pending_reassessments", [])
    if not isinstance(rows, list):
        raise EvidenceVaultOperationalAssessmentShadowError("pending reassessments must be an array")
    fields = {
        "tile_id", "lifecycle_state", "reopen_policy_fingerprint", "prior_group_id",
        "trigger_fingerprint", "superseded_member_evidence_fingerprints",
    }
    result: set[str] = set()
    for row in rows:
        tile_id = str(row.get("tile_id") or "") if isinstance(row, Mapping) else ""
        if (
            not isinstance(row, Mapping)
            or set(row) != fields
            or tile_id in result
            or tile_id not in source_by_id
            or tile_id in accepted_by_id
            or tile_id not in overlay_by_id
            or row.get("lifecycle_state") != "pending_reassessment"
        ):
            raise EvidenceVaultOperationalAssessmentShadowError("pending reassessment is invalid")
        result.add(tile_id)
    return result


def _same_accepted_content(
    accepted: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    return all(
        accepted.get(accepted_field) == candidate.get(candidate_field)
        for accepted_field, candidate_field in (
            ("semantic_state", "candidate_state"),
            ("basis", "basis"),
            ("coverage_refs", "coverage_refs"),
            ("unresolved_refs", "unresolved_refs"),
        )
    )


def _same_accepted_candidate(
    accepted: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    return all(
        accepted.get(accepted_field) == candidate.get(candidate_field)
        for accepted_field, candidate_field in (
            ("semantic_state", "candidate_state"),
            ("basis", "basis"),
            ("coverage_refs", "coverage_refs"),
            ("unresolved_refs", "unresolved_refs"),
            ("source_delta_kind", "delta_kind"),
        )
    )


def _authority_coverage_from_rows(
    projection_tiles: list[Mapping[str, Any]],
    source_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    accepted = [row for row in projection_tiles if row["canonical_semantic_state"] is not None]
    accepted_ids = {str(row["tile_id"]) for row in accepted}
    reopened_ids = {
        str(row["tile_id"])
        for row in projection_tiles
        if row["lifecycle_state"] == "superseded"
    }
    pending_initial = []
    pending_change = []
    contradiction_accepted = []
    contradiction_unresolved = []
    for row in projection_tiles:
        tile_id = str(row["tile_id"])
        delta_kind = source_by_id[tile_id]["delta_kind"]
        if row["authority_state"] == "pending" and delta_kind not in {
            "no_change", "coverage_loss",
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
    return {
        "tile_count": 80,
        "accepted_tile_count": len(accepted),
        "accepted_ok_count": sum(row["canonical_semantic_state"] == "ok" for row in accepted),
        "accepted_no_count": sum(row["canonical_semantic_state"] == "no" for row in accepted),
        "accepted_sin_evidencia_count": sum(
            row["canonical_semantic_state"] == "sin_evidencia" for row in accepted
        ),
        "unresolved_tile_count": 80 - len(accepted),
        "pending_initial_tile_count": len(pending_initial),
        "pending_change_tile_count": len(pending_change),
        "contradiction_on_accepted_count": len(contradiction_accepted),
        "contradiction_on_unresolved_count": len(contradiction_unresolved),
        "contradiction_count": len(contradiction_accepted) + len(contradiction_unresolved),
        "tile_authority_coverage_ratio": round(len(accepted) / 80, 6),
        "score_weight_authority_coverage_ratio": round(accepted_weight / total_weight, 6),
        "score_completeness": "complete" if len(accepted) == 80 else "partial",
        "canonical_score_status": (
            "pending_reassessment" if pending_change or contradiction_accepted else "current"
        ),
    }


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )

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
