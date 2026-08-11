"""Bridge the existing reviewed candidate packet into operational memory v2.

The bridge does not rerun a model and does not grant authority to model-only
relations.  It reuses the complete 80-tile candidate already produced by the
Vault resolver, automatically adopts only deterministic reductions of reviewed
relations, and leaves every other proposal in the candidate overlay.
"""

from __future__ import annotations

from typing import Any, Mapping

from src.services.evidence_vault_authority_profiles import (
    REVIEWED_BASIS_PROFILE_ID,
    SCANNER_SEMANTIC_PROFILE_ID,
    build_initial_authority_profile_matrix,
    evaluate_reviewed_basis_authority,
    evaluate_scanner_semantic_authority,
)
from src.services.evidence_vault_canonical_core import (
    TileState,
    build_tile_contract_registry,
    canonical_fingerprint,
    validate_candidate_packet,
)
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)


def build_operational_packet_from_reviewed_candidate(
    candidate_packet: Mapping[str, Any],
    *,
    current_operational_memory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a partial accepted baseline/update from one exact v1 packet."""

    packet = dict(candidate_packet)
    validate_candidate_packet(packet)
    manifest = packet["manifest"]
    matrix = build_initial_authority_profile_matrix()
    dispositions: dict[str, dict[str, Any]] = {}
    for candidate in packet["candidate_tiles"]:
        tile_id = str(candidate["tile_id"])
        state = TileState(str(candidate["candidate_state"]))
        if candidate.get("delta_kind") == "coverage_loss":
            dispositions[tile_id] = {
                "authority_state": "pending",
                "review_state": "none",
                "authority_profile_id": "coverage-observation-v1",
                "authority_source": "none",
            }
            continue
        decision = evaluate_reviewed_basis_authority(
            candidate_tile=candidate,
            authority_matrix=matrix,
        )
        if decision["eligible"] is True:
            dispositions[tile_id] = {
                "authority_state": "accepted",
                "review_state": "none",
                "authority_profile_id": REVIEWED_BASIS_PROFILE_ID,
                "authority_source": "policy",
                "policy_decision": decision,
            }
            continue
        effective_basis = [
            row
            for row in candidate.get("basis") or []
            if isinstance(row, Mapping)
            and str(row.get("polarity") or "")
            in {"supports", "contradicts", "demonstrates_absence"}
        ]
        review_required = (
            state is TileState.CONTRADICTION
            or bool(effective_basis)
        )
        dispositions[tile_id] = {
            "authority_state": "pending",
            "review_state": "required" if review_required else "none",
            "authority_profile_id": (
                str(
                    next(
                        row["new_semantic_mapping_profile_id"]
                        for row in matrix["tile_profiles"]
                        if row["tile_id"] == tile_id
                    )
                )
                if review_required
                else "unassigned"
            ),
            "authority_source": "none",
        }

    current = dict(current_operational_memory or {})
    return build_operational_memory_packet(
        brand_identity=str(manifest["brand_identity"]),
        source_candidate_packet_fingerprint=str(
            packet["candidate_packet_fingerprint"]
        ),
        aggregation_policy_fingerprint=str(
            manifest["aggregation_policy_fingerprint"]
        ),
        candidate_tiles=packet["candidate_tiles"],
        dispositions=dispositions,
        current_accepted_tiles=(
            current.get("content", {}).get("accepted_tiles") or []
        ),
        parent_canonical_memory_version=(
            str(current["canonical_memory_version"])
            if current.get("canonical_memory_version")
            else None
        ),
        current_pending_reassessments=(
            current.get("content", {}).get("pending_reassessments") or []
        ),
    )


def build_operational_packet_from_scanner_candidate(
    candidate_packet: Mapping[str, Any],
    *,
    current_operational_memory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize the normal Vault brand-memory path from one scan result.

    The candidate remains immutable and its evidence bindings are rechecked by
    the repository.  Non-contradictory scanner states become the Vault policy
    projection; contradictions stay pending and therefore cannot silently
    change a tile or score.  This policy is Vault-only and does not grant the
    disabled operational C7 cutover any authority.
    """

    packet = dict(candidate_packet)
    validate_candidate_packet(packet)
    manifest = packet["manifest"]
    dispositions: dict[str, dict[str, Any]] = {}
    for candidate in packet["candidate_tiles"]:
        tile_id = str(candidate["tile_id"])
        decision = evaluate_scanner_semantic_authority(candidate_tile=candidate)
        if decision["eligible"] is True:
            dispositions[tile_id] = {
                "authority_state": "accepted",
                "review_state": "none",
                "authority_profile_id": SCANNER_SEMANTIC_PROFILE_ID,
                "authority_source": "policy",
                "policy_decision": decision,
            }
        else:
            dispositions[tile_id] = {
                "authority_state": "pending",
                "review_state": "required",
                "authority_profile_id": SCANNER_SEMANTIC_PROFILE_ID,
                "authority_source": "none",
            }

    current = dict(current_operational_memory or {})
    return build_operational_memory_packet(
        brand_identity=str(manifest["brand_identity"]),
        source_candidate_packet_fingerprint=str(
            packet["candidate_packet_fingerprint"]
        ),
        aggregation_policy_fingerprint=str(
            manifest["aggregation_policy_fingerprint"]
        ),
        candidate_tiles=packet["candidate_tiles"],
        dispositions=dispositions,
        current_accepted_tiles=(
            current.get("content", {}).get("accepted_tiles") or []
        ),
        parent_canonical_memory_version=(
            str(current["canonical_memory_version"])
            if current.get("canonical_memory_version")
            else None
        ),
        current_pending_reassessments=(
            current.get("content", {}).get("pending_reassessments") or []
        ),
    )


def build_provisional_operational_packet_from_report(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze one full scanner diagnosis as a zero-authority baseline overlay.

    This preserves the diagnostic tile states without pretending that an LLM
    verdict is an accepted evidence-to-tile relation.  The canonical accepted
    subset is therefore empty until deterministic or reviewed relations exist.
    """

    registry = build_tile_contract_registry()
    registry_by_component = {
        component: [
            row for row in registry["tiles"] if row["component_key"] == component
        ]
        for component in {
            str(row["component_key"]) for row in registry["tiles"]
        }
    }
    component_rows = {
        str(row.get("key") or row.get("component") or ""): row
        for row in report.get("components") or []
        if isinstance(row, Mapping)
    }
    candidates: list[dict[str, Any]] = []
    for component_key, registry_tiles in registry_by_component.items():
        component = component_rows.get(component_key) or {}
        profiles = {
            str(row.get("id") or row.get("tile_id") or ""): row
            for row in component.get("tile_profile") or []
            if isinstance(row, Mapping)
        }
        for tile in registry_tiles:
            tile_id = str(tile["tile_id"])
            profile = profiles.get(tile_id) or {}
            state = str(profile.get("estado") or "sin_evidencia")
            if state not in {
                TileState.OK.value,
                TileState.NO.value,
                TileState.SIN_EVIDENCIA.value,
            }:
                state = TileState.SIN_EVIDENCIA.value
            candidates.append(
                {
                    "tile_id": tile_id,
                    "candidate_state": state,
                    "basis": [],
                    "coverage_refs": [],
                    "unresolved_refs": [],
                    "delta_kind": "baseline",
                }
            )
    diagnostic_payload = {
        "report_id": str(report.get("id") or ""),
        "pipeline_commit_sha": str(report.get("pipeline_commit_sha") or ""),
        "score": report.get("score"),
        "components": [
            {
                "component_key": key,
                "status": str(row.get("status") or ""),
                "tile_profile": list(row.get("tile_profile") or []),
            }
            for key, row in sorted(component_rows.items())
        ],
    }
    return build_operational_memory_packet(
        brand_identity=_report_brand_identity(report),
        source_candidate_packet_fingerprint=canonical_fingerprint(
            "evidence-vault-provisional-report-candidate-v1",
            diagnostic_payload,
        ),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions={},
    )


def _report_brand_identity(report: Mapping[str, Any]) -> str:
    from src.history.report_parser import normalize_domain

    domain = normalize_domain(str(report.get("url") or ""))
    if not domain:
        raise ValueError("report URL has no canonical brand identity")
    return domain


__all__ = [
    "build_operational_packet_from_reviewed_candidate",
    "build_operational_packet_from_scanner_candidate",
    "build_provisional_operational_packet_from_report",
]
