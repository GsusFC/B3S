"""Human review projection for capture-only operational relation proposals."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from src.services.evidence_vault_canonical_core import (
    TileState,
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_tile_contract_registry,
    canonical_fingerprint,
    validate_candidate_packet,
)
from src.services.evidence_vault_exact_relation_supplement import (
    EvidenceVaultExactRelationSupplementError,
    validate_exact_relation_source_decisions,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)


EVIDENCE_VAULT_OPERATIONAL_REVIEW_VERSION = (
    "evidence-vault-operational-relation-review-v1"
)


class EvidenceVaultOperationalReviewError(ValueError):
    """Human relation decisions do not resolve one exact pending source."""


def build_reviewed_operational_source(
    source_packet: Mapping[str, Any],
    *,
    decisions: Mapping[str, Mapping[str, Any]],
    current_operational_memory: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a reviewed source plus a human-authority operational packet."""

    source = deepcopy(dict(source_packet))
    validate_candidate_packet(source)
    manifest = source["manifest"]
    current = dict(current_operational_memory or {})
    parent = current.get("canonical_memory_version")
    if manifest["parent_canonical_memory_version"] != parent:
        raise EvidenceVaultOperationalReviewError(
            "review source has a stale canonical parent"
        )
    pending = {
        str(row["relation_id"]): dict(row)
        for tile in source["candidate_tiles"]
        for row in tile.get("basis") or []
        if row.get("review_status") == "unreviewed"
    }
    normalized = _decisions(decisions)
    if set(normalized) != set(pending):
        raise EvidenceVaultOperationalReviewError(
            "human decisions must resolve every pending source relation exactly"
        )
    pending_c7_relation_ids = {
        str(row["relation_id"])
        for tile in source["candidate_tiles"]
        if tile.get("tile_id") == "C7"
        for row in tile.get("basis") or []
        if row.get("review_status") == "unreviewed"
    }
    coverage = manifest.get("coverage_summary") or {}
    if (
        coverage.get("source") != "exact_relation_supplement"
        and any(
            normalized[relation_id]["decision"] == "accept"
            for relation_id in pending_c7_relation_ids
        )
    ):
        raise EvidenceVaultOperationalReviewError(
            "C7 authority requires an exact two-member relation source"
        )
    try:
        validate_exact_relation_source_decisions(source, normalized)
    except EvidenceVaultExactRelationSupplementError as exc:
        raise EvidenceVaultOperationalReviewError(str(exc)) from exc
    current_by_id = {
        str(row["tile_id"]): dict(row)
        for row in current.get("content", {}).get("accepted_tiles") or []
    }
    reviewed_by_tile: dict[str, list[dict[str, Any]]] = {}
    accepted_events_by_tile: dict[str, list[str]] = {}
    for candidate in source["candidate_tiles"]:
        tile_id = str(candidate["tile_id"])
        basis: list[dict[str, Any]] = []
        for raw in candidate.get("basis") or []:
            relation = dict(raw)
            if relation.get("review_status") != "unreviewed":
                basis.append(relation)
                continue
            decision = normalized[str(relation["relation_id"])]
            if decision["decision"] == "reject":
                continue
            relation["review_status"] = "accepted"
            relation["decision_event_id"] = decision["decision_event_id"]
            basis.append(relation)
            accepted_events_by_tile.setdefault(tile_id, []).append(
                decision["decision_event_id"]
            )
        reviewed_by_tile[tile_id] = sorted(
            basis, key=lambda row: str(row["relation_id"])
        )

    if accepted_events_by_tile.get("C7"):
        _validate_accepted_c7_basis(reviewed_by_tile.get("C7") or [])

    registry = build_tile_contract_registry()["tiles"]
    if parent is None:
        reviewed_tiles = [
            build_candidate_tile(
                tile_id=str(contract["tile_id"]),
                basis=reviewed_by_tile[str(contract["tile_id"])],
                coverage_refs=_source_tile(source, str(contract["tile_id"]))[
                    "coverage_refs"
                ],
                unresolved_refs=_source_tile(source, str(contract["tile_id"]))[
                    "unresolved_refs"
                ],
            )
            for contract in registry
        ]
    else:
        previous = [
            build_candidate_tile(
                tile_id=str(contract["tile_id"]),
                basis=(current_by_id.get(str(contract["tile_id"])) or {}).get(
                    "basis"
                )
                or [],
                coverage_refs=(
                    current_by_id.get(str(contract["tile_id"])) or {}
                ).get("coverage_refs")
                or [],
                unresolved_refs=(
                    current_by_id.get(str(contract["tile_id"])) or {}
                ).get("unresolved_refs")
                or [],
            )
            for contract in registry
        ]
        previous_by_id = {str(row["tile_id"]): row for row in previous}
        updates: list[dict[str, Any]] = []
        for tile_id, basis in sorted(reviewed_by_tile.items()):
            prior = previous_by_id[tile_id]
            source_tile = _source_tile(source, tile_id)
            coverage = source_tile["coverage_refs"]
            unresolved = source_tile["unresolved_refs"]
            probe = build_candidate_tile(
                tile_id=tile_id,
                basis=basis,
                coverage_refs=coverage,
                unresolved_refs=unresolved,
            )
            if (
                probe["basis"] == prior["basis"]
                and probe["coverage_refs"] == prior["coverage_refs"]
                and probe["unresolved_refs"] == prior["unresolved_refs"]
            ):
                continue
            candidate_state = TileState(probe["candidate_state"])
            previous_state = TileState(prior["candidate_state"])
            if candidate_state is TileState.CONTRADICTION:
                delta_kind = "contradiction"
            elif candidate_state is previous_state:
                delta_kind = "strengthened"
            else:
                delta_kind = "candidate_update"
            updates.append(
                {
                    "tile_id": tile_id,
                    "delta_kind": delta_kind,
                    "basis": basis,
                    "coverage_refs": coverage,
                    "unresolved_refs": unresolved,
                }
            )
        reviewed_tiles = build_incremental_candidate_tiles(
            previous_candidate_tiles=previous,
            tile_updates=updates,
        )

    review_identity = {
        relation_id: {
            "decision": row["decision"],
            "decision_event_id": row["decision_event_id"],
        }
        for relation_id, row in sorted(normalized.items())
    }
    reviewed_source = build_candidate_packet(
        brand_identity=str(manifest["brand_identity"]),
        parent_canonical_memory_version=parent,
        candidate_memory_version=str(manifest["candidate_memory_version"]),
        accepted_memory_candidate_version=canonical_fingerprint(
            "evidence-vault-reviewed-accepted-candidate-v1",
            current.get("content") or {},
        ),
        reviewed_memory_candidate_version=canonical_fingerprint(
            EVIDENCE_VAULT_OPERATIONAL_REVIEW_VERSION,
            {"source": source["candidate_packet_fingerprint"], "reviews": review_identity},
        ),
        review_packet_set_fingerprint=canonical_fingerprint(
            "evidence-vault-operational-review-event-set-v1", review_identity
        ),
        aggregation_policy_fingerprint=str(
            manifest["aggregation_policy_fingerprint"]
        ),
        candidate_tiles=reviewed_tiles,
        coverage_summary=dict(manifest.get("coverage_summary") or {}),
        unresolved_items=[
            {
                "unresolved_id": ref,
                "kind": "contradiction",
                "blocking": True,
                "details": {"source": "human_relation_review"},
            }
            for tile in reviewed_tiles
            if tile["candidate_state"] == TileState.CONTRADICTION.value
            for ref in tile.get("unresolved_refs") or []
        ],
    )
    dispositions: dict[str, dict[str, Any]] = {}
    reviewed_by_id = {
        str(row["tile_id"]): row for row in reviewed_source["candidate_tiles"]
    }
    for tile_id, event_ids in accepted_events_by_tile.items():
        candidate = reviewed_by_id[tile_id]
        if candidate["candidate_state"] == TileState.CONTRADICTION.value:
            dispositions[tile_id] = {
                "authority_state": "pending",
                "review_state": "required",
                "authority_profile_id": "human-reviewed-relation-v1",
                "authority_source": "none",
            }
        else:
            dispositions[tile_id] = {
                "authority_state": "accepted",
                "review_state": "resolved",
                "authority_profile_id": "human-reviewed-relation-v1",
                "authority_source": "human",
                "decision_event_id": sorted(event_ids)[0],
            }
    operational = build_operational_memory_packet(
        brand_identity=str(manifest["brand_identity"]),
        source_candidate_packet_fingerprint=reviewed_source[
            "candidate_packet_fingerprint"
        ],
        aggregation_policy_fingerprint=str(
            manifest["aggregation_policy_fingerprint"]
        ),
        candidate_tiles=reviewed_source["candidate_tiles"],
        dispositions=dispositions,
        current_accepted_tiles=current.get("content", {}).get("accepted_tiles")
        or [],
        parent_canonical_memory_version=parent,
        current_pending_reassessments=current.get("content", {}).get(
            "pending_reassessments"
        )
        or [],
    )
    return reviewed_source, operational


def _validate_accepted_c7_basis(values: list[Mapping[str, Any]]) -> None:
    rows = [dict(row) for row in values]
    claim_ids = {row.get("claim_id") for row in rows}
    source_ids = {row.get("source_identity_id") for row in rows}
    if (
        len(rows) != 2
        or len(claim_ids) != 1
        or None in claim_ids
        or len(source_ids) != 2
        or any(
            row.get("polarity") != "supports"
            or row.get("review_status") != "accepted"
            or not row.get("decision_event_id")
            for row in rows
        )
    ):
        raise EvidenceVaultOperationalReviewError(
            "accepted C7 basis must be one complete two-member group"
        )


def _decisions(
    values: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    if not isinstance(values, Mapping):
        raise EvidenceVaultOperationalReviewError("review decisions must be an object")
    result: dict[str, dict[str, str]] = {}
    for relation_id, raw in values.items():
        if not isinstance(raw, Mapping) or set(raw) != {
            "decision",
            "decision_event_id",
        }:
            raise EvidenceVaultOperationalReviewError("review decision fields mismatch")
        relation = _sha256(relation_id, field="relation_id")
        decision = str(raw.get("decision") or "")
        event = str(raw.get("decision_event_id") or "").strip()
        if decision not in {"accept", "reject"} or not event or len(event) > 200:
            raise EvidenceVaultOperationalReviewError("review decision is invalid")
        result[relation] = {
            "decision": decision,
            "decision_event_id": event,
        }
    return result


def _source_tile(packet: Mapping[str, Any], tile_id: str) -> dict[str, Any]:
    return next(
        dict(row)
        for row in packet["candidate_tiles"]
        if str(row["tile_id"]) == tile_id
    )


def _sha256(value: Any, *, field: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise EvidenceVaultOperationalReviewError(f"{field} must be a SHA-256")
    return text


__all__ = [
    "EVIDENCE_VAULT_OPERATIONAL_REVIEW_VERSION",
    "EvidenceVaultOperationalReviewError",
    "build_reviewed_operational_source",
]
