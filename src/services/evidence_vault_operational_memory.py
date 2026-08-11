"""Pure operational-memory projection for the incremental Evidence Vault.

This v2 boundary keeps accepted canonical memory separate from the candidate
overlay.  It consumes one complete 80-tile candidate projection plus optional
accepted parent state and produces content-addressed, non-persistent artifacts.
It performs no LLM, database, scanner, or production work.
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
from src.services.evidence_vault_authority_profiles import (
    EvidenceVaultAuthorityProfileError,
    validate_authority_decision,
)
from src.sv9.rubric import COMPONENTS


EVIDENCE_VAULT_OPERATIONAL_PACKET_VERSION = (
    "evidence-vault-operational-memory-packet-v2"
)
EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION = (
    "evidence-vault-accepted-operational-memory-v2"
)
EVIDENCE_VAULT_CANDIDATE_OVERLAY_VERSION = (
    "evidence-vault-candidate-overlay-v2"
)
EVIDENCE_VAULT_SCORING_PROJECTION_VERSION = (
    "evidence-vault-operational-scoring-projection-v2"
)
EVIDENCE_VAULT_CANDIDATE_PREVIEW_VERSION = (
    "evidence-vault-candidate-preview-v2"
)
EVIDENCE_VAULT_OPERATIONAL_DERIVED_TILE_STATE_VERSION = (
    "evidence-vault-operational-derived-tile-state-v2"
)


class EvidenceVaultOperationalMemoryError(ValueError):
    """The operational-memory projection is incomplete or inconsistent."""


class AuthorityState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ReviewState(StrEnum):
    NONE = "none"
    REQUIRED = "required"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"


class LifecycleState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


def build_operational_memory_packet(
    *,
    brand_identity: str,
    source_candidate_packet_fingerprint: str,
    aggregation_policy_fingerprint: str,
    candidate_tiles: Iterable[Mapping[str, Any]],
    dispositions: Mapping[str, Mapping[str, Any]] | None = None,
    current_accepted_tiles: Iterable[Mapping[str, Any]] = (),
    parent_canonical_memory_version: str | None = None,
    reopened_tile_ids: Iterable[str] = (),
    reopen_policy_fingerprint: str | None = None,
    reopened_group_contexts: Iterable[Mapping[str, Any]] = (),
    current_pending_reassessments: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build accepted memory, candidate overlay and a complete score projection.

    Missing dispositions default to ``pending``.  A pending or rejected
    candidate never replaces an accepted parent tile.  An accepted candidate
    replaces the parent regardless of whether that change raises or lowers the
    score; direction is deliberately absent from the authority decision.
    """

    registry = build_tile_contract_registry()
    registry_rows = list(registry["tiles"])
    registry_by_id = {str(row["tile_id"]): row for row in registry_rows}
    tile_order = {
        str(row["tile_id"]): position
        for position, row in enumerate(registry_rows)
    }
    candidates = _normalize_candidates(candidate_tiles, registry_by_id, tile_order)
    parents = _normalize_accepted_tiles(
        current_accepted_tiles,
        registry_by_id=registry_by_id,
        tile_order=tile_order,
    )
    decisions = _normalize_dispositions(dispositions or {}, registry_by_id)
    reopened = _normalize_reopened_tile_ids(reopened_tile_ids, registry_by_id)
    reopen_contexts = _normalize_reopened_group_contexts(
        reopened_group_contexts,
        registry_by_id,
    )
    if set(reopen_contexts) != reopened:
        raise EvidenceVaultOperationalMemoryError(
            "reopened tile ids and group contexts must match"
        )
    pending_reassessments = _normalize_pending_reassessments(
        current_pending_reassessments,
        registry_by_id,
    )
    parent_version = _optional_fingerprint(
        parent_canonical_memory_version,
        field="parent_canonical_memory_version",
    )
    source_packet_fingerprint = _fingerprint(
        source_candidate_packet_fingerprint,
        field="source_candidate_packet_fingerprint",
    )
    if bool(parents) != bool(parent_version):
        raise EvidenceVaultOperationalMemoryError(
            "parent canonical memory and accepted parent tiles must be supplied together"
        )
    if reopened and not parent_version:
        raise EvidenceVaultOperationalMemoryError(
            "reopening accepted tiles requires a canonical parent"
        )
    reopen_policy = _optional_fingerprint(
        reopen_policy_fingerprint,
        field="reopen_policy_fingerprint",
    )
    if bool(reopened) != bool(reopen_policy):
        raise EvidenceVaultOperationalMemoryError(
            "reopened tiles and reopen policy fingerprint must be supplied together"
        )

    accepted_by_id = {row["tile_id"]: row for row in parents}
    if set(pending_reassessments) & set(accepted_by_id):
        raise EvidenceVaultOperationalMemoryError(
            "pending reassessment cannot retain accepted tile authority"
        )
    overlay: list[dict[str, Any]] = []
    projection: list[dict[str, Any]] = []

    for candidate in candidates:
        tile_id = candidate["tile_id"]
        previous = accepted_by_id.get(tile_id)
        disposition = decisions.get(tile_id) or _default_disposition(candidate)
        semantic_state = TileState(candidate["candidate_state"])
        authority_state = AuthorityState(disposition["authority_state"])
        review_state = ReviewState(disposition["review_state"])
        _validate_state_axes(
            tile_id=tile_id,
            semantic_state=semantic_state,
            authority_state=authority_state,
            review_state=review_state,
        )
        is_reopened = tile_id in reopened
        is_pending_reassessment = (
            is_reopened or tile_id in pending_reassessments
        )
        if is_reopened:
            if previous is None:
                raise EvidenceVaultOperationalMemoryError(
                    f"reopened tile {tile_id} has no accepted parent"
                )
            if (
                candidate.get("delta_kind") != "verified_deprecation"
                or authority_state is not AuthorityState.PENDING
                or review_state not in {ReviewState.REQUIRED, ReviewState.IN_REVIEW}
            ):
                raise EvidenceVaultOperationalMemoryError(
                    f"reopened tile {tile_id} must be a pending verified deprecation"
                )
            accepted_by_id.pop(tile_id)
            pending_reassessments[tile_id] = {
                "tile_id": tile_id,
                "lifecycle_state": "pending_reassessment",
                "reopen_policy_fingerprint": reopen_policy,
                "prior_group_id": reopen_contexts[tile_id]["prior_group_id"],
                "trigger_fingerprint": reopen_contexts[tile_id][
                    "trigger_fingerprint"
                ],
                "superseded_member_evidence_fingerprints": (
                    reopen_contexts[tile_id][
                        "superseded_member_evidence_fingerprints"
                    ]
                ),
            }
        if authority_state is AuthorityState.ACCEPTED:
            if is_reopened:
                raise EvidenceVaultOperationalMemoryError(
                    f"reopened tile {tile_id} cannot retain accepted authority"
                )
            if disposition["authority_source"] not in {"policy", "human"}:
                raise EvidenceVaultOperationalMemoryError(
                    f"accepted tile {tile_id} requires policy or human authority"
                )
            if disposition["authority_profile_id"] == "unassigned":
                raise EvidenceVaultOperationalMemoryError(
                    f"accepted tile {tile_id} requires an authority profile"
                )
            if disposition["authority_source"] == "human":
                if not disposition["decision_event_id"]:
                    raise EvidenceVaultOperationalMemoryError(
                        f"human-accepted tile {tile_id} requires a decision event"
                    )
            else:
                policy_decision = disposition.get("policy_decision")
                if not isinstance(policy_decision, Mapping):
                    raise EvidenceVaultOperationalMemoryError(
                        f"policy-accepted tile {tile_id} requires an authority decision"
                    )
                try:
                    validate_authority_decision(
                        policy_decision,
                        candidate_tile=candidate,
                    )
                except EvidenceVaultAuthorityProfileError as exc:
                    raise EvidenceVaultOperationalMemoryError(
                        f"invalid policy authority for {tile_id}: {exc}"
                    ) from exc
                if policy_decision.get("authority_profile_id") != disposition[
                    "authority_profile_id"
                ]:
                    raise EvidenceVaultOperationalMemoryError(
                        f"policy authority profile mismatch for {tile_id}"
                    )

        accepted_candidate = None
        if authority_state is AuthorityState.ACCEPTED:
            if previous is not None and _same_accepted_content(
                previous,
                candidate,
            ):
                # A new whole-packet fingerprint or no-change delta is lineage,
                # not a canonical-memory mutation. Preserve the original
                # authority-bearing row exactly.
                accepted_candidate = dict(previous)
            else:
                _validate_authoritative_candidate(candidate, previous=previous)
                accepted_candidate = _accepted_tile(
                    candidate,
                    disposition,
                    source_candidate_packet_fingerprint=source_packet_fingerprint,
                )
            accepted_by_id[tile_id] = accepted_candidate
            pending_reassessments.pop(tile_id, None)

        is_pending_reassessment = tile_id in pending_reassessments
        material_change = previous is None or not _same_accepted_content(
            previous,
            candidate,
        )
        if authority_state is not AuthorityState.ACCEPTED and (
            material_change or is_pending_reassessment
        ):
            overlay.append(
                _overlay_tile(
                    candidate,
                    disposition,
                    previous=previous,
                )
            )

        effective = accepted_by_id.get(tile_id)
        effective_state = (
            str(effective["semantic_state"])
            if effective is not None
            else TileState.SIN_EVIDENCIA.value
        )
        multiplier = int(COMPONENTS[candidate["component_key"]]["multiplier"])
        preview_points = (
            None
            if semantic_state is TileState.CONTRADICTION
            else multiplier if semantic_state is TileState.OK else 0
        )
        projection.append(
            {
                "component_key": candidate["component_key"],
                "tile_id": tile_id,
                "tile_key": candidate["tile_key"],
                "canonical_semantic_state": (
                    str(effective["semantic_state"])
                    if effective is not None
                    else None
                ),
                "effective_scoring_state": effective_state,
                "canonical_effective_points": (
                    multiplier if effective_state == TileState.OK.value else 0
                ),
                "candidate_semantic_state": semantic_state.value,
                "candidate_preview_points": preview_points,
                "authority_state": authority_state.value,
                "review_state": review_state.value,
                "lifecycle_state": (
                    LifecycleState.SUPERSEDED.value
                    if is_pending_reassessment
                    else LifecycleState.ACTIVE.value
                ),
                "score_eligible": (
                    not is_pending_reassessment
                    and effective is not None
                    and effective_state == TileState.OK.value
                ),
                "has_candidate_overlay": (
                    authority_state is not AuthorityState.ACCEPTED
                    and (material_change or is_pending_reassessment)
                ),
            }
        )

    accepted_tiles = sorted(
        accepted_by_id.values(),
        key=lambda row: tile_order[row["tile_id"]],
    )
    overlay.sort(key=lambda row: tile_order[row["tile_id"]])
    metrics = _coverage_metrics(
        accepted_tiles=accepted_tiles,
        overlay=overlay,
        registry_by_id=registry_by_id,
        reopened_tile_ids=set(pending_reassessments),
    )
    policies = {
        "tile_contract_registry_fingerprint": tile_contract_registry_fingerprint(),
        "reducer_policy_fingerprint": reducer_policy_fingerprint(),
        "aggregation_policy_fingerprint": _fingerprint(
            aggregation_policy_fingerprint,
            field="aggregation_policy_fingerprint",
        ),
    }
    accepted_content = {
        "schema_version": EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION,
        "brand_identity": _brand_identity(brand_identity),
        "parent_canonical_memory_version": parent_version,
        **policies,
        "accepted_tiles": accepted_tiles,
    }
    if pending_reassessments:
        accepted_content["pending_reassessments"] = sorted(
            pending_reassessments.values(),
            key=lambda row: tile_order[row["tile_id"]],
        )
    accepted_memory_candidate_version = canonical_fingerprint(
        EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION,
        accepted_content,
    )
    has_accepted_change = (
        bool(accepted_tiles)
        if parent_version is None
        else not _same_accepted_tile_set(parents, accepted_tiles)
    )
    proposed_canonical_memory_version = (
        accepted_memory_candidate_version if has_accepted_change else parent_version
    )
    overlay_content = {
        "schema_version": EVIDENCE_VAULT_CANDIDATE_OVERLAY_VERSION,
        "brand_identity": accepted_content["brand_identity"],
        "parent_canonical_memory_version": parent_version,
        "candidate_tiles": overlay,
    }
    candidate_overlay_version = canonical_fingerprint(
        EVIDENCE_VAULT_CANDIDATE_OVERLAY_VERSION,
        overlay_content,
    )
    scoring_projection = {
        "schema_version": EVIDENCE_VAULT_SCORING_PROJECTION_VERSION,
        "accepted_memory_candidate_version": accepted_memory_candidate_version,
        "proposed_canonical_memory_version": proposed_canonical_memory_version,
        **policies,
        "tiles": projection,
        "coverage": metrics,
    }
    derived_tile_state_fingerprint = canonical_fingerprint(
        EVIDENCE_VAULT_OPERATIONAL_DERIVED_TILE_STATE_VERSION,
        [
            {
                "tile_id": row["tile_id"],
                "state": row["effective_scoring_state"],
            }
            for row in projection
        ],
    )
    candidate_preview_identity = (
        canonical_fingerprint(
            EVIDENCE_VAULT_CANDIDATE_PREVIEW_VERSION,
            {
                "current_canonical_memory_version": parent_version,
                "accepted_memory_candidate_version": (
                    accepted_memory_candidate_version
                ),
                "candidate_overlay_version": candidate_overlay_version,
                "preview": [
                    {
                        "tile_id": row["tile_id"],
                        "candidate_preview_points": row["candidate_preview_points"],
                    }
                    for row in projection
                    if row["has_candidate_overlay"]
                ],
            },
        )
        if overlay
        else None
    )
    unsigned_packet = {
        "schema_version": EVIDENCE_VAULT_OPERATIONAL_PACKET_VERSION,
        "authority": False,
        "runtime_effect": False,
        "brand_identity": accepted_content["brand_identity"],
        "source_candidate_packet_fingerprint": source_packet_fingerprint,
        "current_canonical_memory_version": parent_version,
        "accepted_memory_candidate_version": accepted_memory_candidate_version,
        "proposed_canonical_memory_version": proposed_canonical_memory_version,
        "has_accepted_change": has_accepted_change,
        "candidate_overlay_version": candidate_overlay_version,
        "candidate_preview_identity": candidate_preview_identity,
        "derived_tile_state_fingerprint": derived_tile_state_fingerprint,
        "accepted_memory": accepted_content,
        "candidate_overlay": overlay_content,
        "scoring_projection": scoring_projection,
    }
    if pending_reassessments:
        unsigned_packet["pending_reassessment_tile_ids"] = sorted(
            pending_reassessments
        )
    if reopened:
        unsigned_packet["reopened_tile_ids"] = sorted(reopened)
        unsigned_packet["reopen_policy_fingerprint"] = reopen_policy
    return {
        **unsigned_packet,
        "candidate_packet_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_OPERATIONAL_PACKET_VERSION,
            unsigned_packet,
        ),
    }


def _normalize_candidates(
    values: Iterable[Mapping[str, Any]],
    registry_by_id: Mapping[str, Mapping[str, Any]],
    tile_order: Mapping[str, int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, Mapping):
            raise EvidenceVaultOperationalMemoryError("candidate tile must be an object")
        tile_id = str(raw.get("tile_id") or "").strip()
        registry = registry_by_id.get(tile_id)
        if registry is None:
            raise EvidenceVaultOperationalMemoryError(f"unknown candidate tile: {tile_id}")
        if tile_id in seen:
            raise EvidenceVaultOperationalMemoryError(f"duplicate candidate tile: {tile_id}")
        seen.add(tile_id)
        try:
            state = TileState(str(raw.get("candidate_state") or ""))
        except ValueError as exc:
            raise EvidenceVaultOperationalMemoryError(
                f"invalid candidate state for {tile_id}"
            ) from exc
        rows.append(
            {
                "component_key": str(registry["component_key"]),
                "tile_id": tile_id,
                "tile_key": str(registry["tile_key"]),
                "candidate_state": state.value,
                "basis": _json_list(raw.get("basis") or [], field=f"{tile_id}.basis"),
                "coverage_refs": _text_list(
                    raw.get("coverage_refs") or [],
                    field=f"{tile_id}.coverage_refs",
                ),
                "unresolved_refs": _text_list(
                    raw.get("unresolved_refs") or [],
                    field=f"{tile_id}.unresolved_refs",
                ),
                "delta_kind": str(raw.get("delta_kind") or "baseline"),
                "previous_canonical_state": (
                    str(raw.get("previous_canonical_state"))
                    if raw.get("previous_canonical_state") is not None
                    else None
                ),
            }
        )
    if seen != set(registry_by_id):
        missing = sorted(set(registry_by_id) - seen)
        raise EvidenceVaultOperationalMemoryError(
            f"candidate projection must contain all 80 tiles; missing: {missing}"
        )
    return sorted(rows, key=lambda row: tile_order[row["tile_id"]])


def _normalize_accepted_tiles(
    values: Iterable[Mapping[str, Any]],
    *,
    registry_by_id: Mapping[str, Mapping[str, Any]],
    tile_order: Mapping[str, int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, Mapping):
            raise EvidenceVaultOperationalMemoryError("accepted tile must be an object")
        tile_id = str(raw.get("tile_id") or "").strip()
        registry = registry_by_id.get(tile_id)
        if registry is None or tile_id in seen:
            raise EvidenceVaultOperationalMemoryError(
                f"invalid or duplicate accepted tile: {tile_id}"
            )
        seen.add(tile_id)
        try:
            state = TileState(str(raw.get("semantic_state") or raw.get("state") or ""))
        except ValueError as exc:
            raise EvidenceVaultOperationalMemoryError(
                f"invalid accepted state for {tile_id}"
            ) from exc
        if state is TileState.CONTRADICTION:
            raise EvidenceVaultOperationalMemoryError(
                "contradiction cannot enter accepted memory"
            )
        rows.append(
            {
                "component_key": str(registry["component_key"]),
                "tile_id": tile_id,
                "tile_key": str(registry["tile_key"]),
                "semantic_state": state.value,
                "basis": _json_list(raw.get("basis") or [], field=f"{tile_id}.basis"),
                "coverage_refs": _text_list(
                    raw.get("coverage_refs") or [],
                    field=f"{tile_id}.coverage_refs",
                ),
                "unresolved_refs": _text_list(
                    raw.get("unresolved_refs") or [],
                    field=f"{tile_id}.unresolved_refs",
                ),
                "source_delta_kind": str(
                    raw.get("source_delta_kind") or "baseline"
                ),
                "source_candidate_packet_fingerprint": _fingerprint(
                    raw.get("source_candidate_packet_fingerprint"),
                    field=f"{tile_id}.source_candidate_packet_fingerprint",
                ),
                "authority_profile_id": str(
                    raw.get("authority_profile_id") or "legacy-accepted"
                ),
                "authority_source": str(raw.get("authority_source") or "human"),
                "decision_event_id": (
                    str(raw.get("decision_event_id"))
                    if raw.get("decision_event_id") is not None
                    else None
                ),
                "authority_matrix_fingerprint": (
                    str(raw.get("authority_matrix_fingerprint"))
                    if raw.get("authority_matrix_fingerprint") is not None
                    else None
                ),
                "authority_decision_fingerprint": (
                    str(raw.get("authority_decision_fingerprint"))
                    if raw.get("authority_decision_fingerprint") is not None
                    else None
                ),
            }
        )
    return sorted(rows, key=lambda row: tile_order[row["tile_id"]])


def _normalize_reopened_tile_ids(
    values: Iterable[str],
    registry_by_id: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    reopened = {str(value or "").strip() for value in values}
    if "" in reopened or not reopened.issubset(registry_by_id):
        raise EvidenceVaultOperationalMemoryError(
            "reopened tile ids contain an unknown tile"
        )
    return reopened


def _normalize_reopened_group_contexts(
    values: Iterable[Mapping[str, Any]],
    registry_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, Mapping) or set(raw) != {
            "tile_id",
            "prior_group_id",
            "trigger_fingerprint",
            "superseded_member_evidence_fingerprints",
        }:
            raise EvidenceVaultOperationalMemoryError(
                "reopened group context fields mismatch"
            )
        tile_id = str(raw.get("tile_id") or "").strip()
        if tile_id not in registry_by_id or tile_id in contexts:
            raise EvidenceVaultOperationalMemoryError(
                "reopened group context tile is invalid or duplicated"
            )
        superseded = sorted(
            {
                _fingerprint(
                    value,
                    field=f"{tile_id}.superseded_member_evidence_fingerprint",
                )
                for value in raw.get(
                    "superseded_member_evidence_fingerprints"
                )
                or []
            }
        )
        if not superseded:
            raise EvidenceVaultOperationalMemoryError(
                "reopened group context requires superseded member evidence"
            )
        contexts[tile_id] = {
            "tile_id": tile_id,
            "prior_group_id": _fingerprint(
                raw.get("prior_group_id"),
                field=f"{tile_id}.prior_group_id",
            ),
            "trigger_fingerprint": _fingerprint(
                raw.get("trigger_fingerprint"),
                field=f"{tile_id}.trigger_fingerprint",
            ),
            "superseded_member_evidence_fingerprints": superseded,
        }
    return contexts


def _normalize_pending_reassessments(
    values: Iterable[Mapping[str, Any]],
    registry_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, Mapping) or set(raw) != {
            "tile_id",
            "lifecycle_state",
            "reopen_policy_fingerprint",
            "prior_group_id",
            "trigger_fingerprint",
            "superseded_member_evidence_fingerprints",
        }:
            raise EvidenceVaultOperationalMemoryError(
                "pending reassessment fields mismatch"
            )
        tile_id = str(raw.get("tile_id") or "").strip()
        if tile_id not in registry_by_id or tile_id in pending:
            raise EvidenceVaultOperationalMemoryError(
                "pending reassessment tile is invalid or duplicated"
            )
        if raw.get("lifecycle_state") != "pending_reassessment":
            raise EvidenceVaultOperationalMemoryError(
                "pending reassessment lifecycle state is invalid"
            )
        superseded = sorted(
            {
                _fingerprint(
                    value,
                    field=f"{tile_id}.superseded_member_evidence_fingerprint",
                )
                for value in raw.get(
                    "superseded_member_evidence_fingerprints"
                )
                or []
            }
        )
        if not superseded:
            raise EvidenceVaultOperationalMemoryError(
                "pending reassessment requires superseded member evidence"
            )
        pending[tile_id] = {
            "tile_id": tile_id,
            "lifecycle_state": "pending_reassessment",
            "reopen_policy_fingerprint": _fingerprint(
                raw.get("reopen_policy_fingerprint"),
                field=f"{tile_id}.reopen_policy_fingerprint",
            ),
            "prior_group_id": _fingerprint(
                raw.get("prior_group_id"),
                field=f"{tile_id}.prior_group_id",
            ),
            "trigger_fingerprint": _fingerprint(
                raw.get("trigger_fingerprint"),
                field=f"{tile_id}.trigger_fingerprint",
            ),
            "superseded_member_evidence_fingerprints": superseded,
        }
    return pending


def _normalize_dispositions(
    values: Mapping[str, Mapping[str, Any]],
    registry_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for tile_id, raw in values.items():
        if tile_id not in registry_by_id or not isinstance(raw, Mapping):
            raise EvidenceVaultOperationalMemoryError(
                f"invalid disposition tile: {tile_id}"
            )
        try:
            authority = AuthorityState(str(raw.get("authority_state") or "pending"))
            review = ReviewState(str(raw.get("review_state") or "none"))
        except ValueError as exc:
            raise EvidenceVaultOperationalMemoryError(
                f"invalid disposition state for {tile_id}"
            ) from exc
        rows[tile_id] = {
            "authority_state": authority.value,
            "review_state": review.value,
            "authority_profile_id": str(
                raw.get("authority_profile_id") or "unassigned"
            ),
            "authority_source": str(raw.get("authority_source") or "none"),
            "decision_event_id": (
                str(raw.get("decision_event_id"))
                if raw.get("decision_event_id") is not None
                else None
            ),
            "policy_decision": (
                dict(raw["policy_decision"])
                if isinstance(raw.get("policy_decision"), Mapping)
                else None
            ),
        }
    return rows


def _default_disposition(candidate: Mapping[str, Any]) -> dict[str, Any]:
    contradiction = candidate["candidate_state"] == TileState.CONTRADICTION.value
    return {
        "authority_state": AuthorityState.PENDING.value,
        "review_state": (
            ReviewState.REQUIRED.value if contradiction else ReviewState.NONE.value
        ),
        "authority_profile_id": "unassigned",
        "authority_source": "none",
        "decision_event_id": None,
        "policy_decision": None,
    }


def _validate_state_axes(
    *,
    tile_id: str,
    semantic_state: TileState,
    authority_state: AuthorityState,
    review_state: ReviewState,
) -> None:
    if semantic_state is TileState.CONTRADICTION:
        if authority_state is not AuthorityState.PENDING:
            raise EvidenceVaultOperationalMemoryError(
                f"contradiction {tile_id} must remain pending"
            )
        if review_state not in {ReviewState.REQUIRED, ReviewState.IN_REVIEW}:
            raise EvidenceVaultOperationalMemoryError(
                f"contradiction {tile_id} requires review"
            )
    if authority_state in {AuthorityState.ACCEPTED, AuthorityState.REJECTED}:
        if review_state in {ReviewState.REQUIRED, ReviewState.IN_REVIEW}:
            raise EvidenceVaultOperationalMemoryError(
                f"resolved authority for {tile_id} cannot remain under review"
            )
    if authority_state is AuthorityState.PENDING and review_state is ReviewState.RESOLVED:
        raise EvidenceVaultOperationalMemoryError(
            f"pending authority for {tile_id} cannot have resolved review"
        )


def _validate_authoritative_candidate(
    candidate: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None,
) -> None:
    """Re-run the canonical reducer before any proposal receives authority."""

    declared_previous = candidate.get("previous_canonical_state")
    if (
        previous is not None
        and declared_previous is not None
        and declared_previous != previous.get("semantic_state")
    ):
        raise EvidenceVaultOperationalMemoryError(
            f"accepted tile {candidate['tile_id']} does not evolve its canonical parent"
        )
    try:
        rebuilt = build_candidate_tile(
            tile_id=str(candidate["tile_id"]),
            basis=candidate.get("basis") or [],
            previous_canonical_state=declared_previous,
            delta_kind=str(candidate.get("delta_kind") or "baseline"),
            coverage_refs=candidate.get("coverage_refs") or [],
            unresolved_refs=candidate.get("unresolved_refs") or [],
        )
    except EvidenceVaultCanonicalCoreError as exc:
        raise EvidenceVaultOperationalMemoryError(
            f"accepted tile {candidate['tile_id']} failed canonical reduction"
        ) from exc
    expected_fields = (
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
    if any(candidate.get(field) != rebuilt.get(field) for field in expected_fields):
        raise EvidenceVaultOperationalMemoryError(
            f"accepted tile {candidate['tile_id']} does not match canonical reducer output"
        )


def _accepted_tile(
    candidate: Mapping[str, Any],
    disposition: Mapping[str, Any],
    *,
    source_candidate_packet_fingerprint: str,
) -> dict[str, Any]:
    policy_decision = disposition.get("policy_decision")
    return {
        "component_key": candidate["component_key"],
        "tile_id": candidate["tile_id"],
        "tile_key": candidate["tile_key"],
        "semantic_state": candidate["candidate_state"],
        "basis": candidate["basis"],
        "coverage_refs": candidate["coverage_refs"],
        "unresolved_refs": candidate["unresolved_refs"],
        "source_delta_kind": candidate["delta_kind"],
        "source_candidate_packet_fingerprint": source_candidate_packet_fingerprint,
        "authority_profile_id": disposition["authority_profile_id"],
        "authority_source": disposition["authority_source"],
        "decision_event_id": disposition["decision_event_id"],
        "authority_matrix_fingerprint": (
            str(policy_decision.get("authority_matrix_fingerprint"))
            if isinstance(policy_decision, Mapping)
            else None
        ),
        "authority_decision_fingerprint": (
            str(policy_decision.get("authority_decision_fingerprint"))
            if isinstance(policy_decision, Mapping)
            else None
        ),
    }


def _overlay_tile(
    candidate: Mapping[str, Any],
    disposition: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "component_key": candidate["component_key"],
        "tile_id": candidate["tile_id"],
        "tile_key": candidate["tile_key"],
        "semantic_state": candidate["candidate_state"],
        "authority_state": disposition["authority_state"],
        "review_state": disposition["review_state"],
        "lifecycle_state": LifecycleState.ACTIVE.value,
        "authority_profile_id": disposition["authority_profile_id"],
        "authority_source": disposition["authority_source"],
        "decision_event_id": disposition["decision_event_id"],
        "basis": candidate["basis"],
        "coverage_refs": candidate["coverage_refs"],
        "unresolved_refs": candidate["unresolved_refs"],
        "delta_kind": candidate["delta_kind"],
        "previous_canonical_state": (
            previous["semantic_state"] if previous is not None else None
        ),
    }



def _same_accepted_content(
    previous: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    return (
        previous["semantic_state"] == candidate["candidate_state"]
        and previous["basis"] == candidate["basis"]
        and previous.get("coverage_refs", []) == candidate["coverage_refs"]
        and previous.get("unresolved_refs", []) == candidate["unresolved_refs"]
    )


def _same_accepted_tile_set(
    previous: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
) -> bool:
    """Compare authority-bearing content, excluding lineage-only metadata."""

    fields = (
        "component_key",
        "tile_id",
        "tile_key",
        "semantic_state",
        "basis",
        "coverage_refs",
        "unresolved_refs",
        "authority_profile_id",
        "authority_source",
        "decision_event_id",
        "authority_matrix_fingerprint",
        "authority_decision_fingerprint",
    )
    return [
        {field: row.get(field) for field in fields}
        for row in previous
    ] == [
        {field: row.get(field) for field in fields}
        for row in candidate
    ]


def _coverage_metrics(
    *,
    accepted_tiles: list[dict[str, Any]],
    overlay: list[dict[str, Any]],
    registry_by_id: Mapping[str, Mapping[str, Any]],
    reopened_tile_ids: set[str] | None = None,
) -> dict[str, Any]:
    accepted_ids = {row["tile_id"] for row in accepted_tiles}
    reopened_ids = set(reopened_tile_ids or ())
    accepted_count = len(accepted_ids)
    pending_initial = [
        row
        for row in overlay
        if row["authority_state"] == AuthorityState.PENDING.value
        and row["delta_kind"] not in {"no_change", "coverage_loss"}
        and row["tile_id"] not in accepted_ids
        and row["tile_id"] not in reopened_ids
    ]
    pending_change = [
        row
        for row in overlay
        if row["authority_state"] == AuthorityState.PENDING.value
        and row["delta_kind"] not in {"no_change", "coverage_loss"}
        and (
            row["tile_id"] in accepted_ids
            or row["tile_id"] in reopened_ids
        )
    ]
    contradiction_on_accepted = [
        row
        for row in overlay
        if row["semantic_state"] == TileState.CONTRADICTION.value
        and row["tile_id"] in accepted_ids
    ]
    contradiction_on_unresolved = [
        row
        for row in overlay
        if row["semantic_state"] == TileState.CONTRADICTION.value
        and row["tile_id"] not in accepted_ids
    ]
    accepted_state_counts = {
        state.value: sum(
            row["semantic_state"] == state.value for row in accepted_tiles
        )
        for state in (TileState.OK, TileState.NO, TileState.SIN_EVIDENCIA)
    }
    accepted_weight = sum(
        int(COMPONENTS[str(registry_by_id[row["tile_id"]]["component_key"])]["multiplier"])
        for row in accepted_tiles
    )
    total_weight = sum(
        int(COMPONENTS[str(row["component_key"])]["multiplier"])
        for row in registry_by_id.values()
    )
    return {
        "tile_count": len(registry_by_id),
        "accepted_tile_count": accepted_count,
        "accepted_ok_count": accepted_state_counts[TileState.OK.value],
        "accepted_no_count": accepted_state_counts[TileState.NO.value],
        "accepted_sin_evidencia_count": accepted_state_counts[
            TileState.SIN_EVIDENCIA.value
        ],
        "unresolved_tile_count": len(registry_by_id) - accepted_count,
        "pending_initial_tile_count": len(pending_initial),
        "pending_change_tile_count": len(pending_change),
        "contradiction_on_accepted_count": len(contradiction_on_accepted),
        "contradiction_on_unresolved_count": len(contradiction_on_unresolved),
        "contradiction_count": (
            len(contradiction_on_accepted) + len(contradiction_on_unresolved)
        ),
        "tile_authority_coverage_ratio": round(
            accepted_count / len(registry_by_id),
            6,
        ),
        "score_weight_authority_coverage_ratio": round(
            accepted_weight / total_weight,
            6,
        ),
        "score_completeness": "complete" if accepted_count == len(registry_by_id) else "partial",
        "canonical_score_status": (
            "pending_reassessment"
            if pending_change or contradiction_on_accepted
            else "current"
        ),
    }


def _brand_identity(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if not normalized or len(normalized) > 300:
        raise EvidenceVaultOperationalMemoryError("invalid brand identity")
    return normalized


def _fingerprint(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise EvidenceVaultOperationalMemoryError(f"{field} must be a sha256")
    return normalized


def _optional_fingerprint(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, field=field)


def _json_list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise EvidenceVaultOperationalMemoryError(f"{field} must be a list")
    return list(value)


def _text_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise EvidenceVaultOperationalMemoryError(f"{field} must be a list")
    rows = sorted({str(item).strip() for item in value if str(item).strip()})
    return rows


__all__ = [
    "AuthorityState",
    "EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION",
    "EVIDENCE_VAULT_CANDIDATE_OVERLAY_VERSION",
    "EVIDENCE_VAULT_CANDIDATE_PREVIEW_VERSION",
    "EVIDENCE_VAULT_OPERATIONAL_DERIVED_TILE_STATE_VERSION",
    "EVIDENCE_VAULT_OPERATIONAL_PACKET_VERSION",
    "EVIDENCE_VAULT_SCORING_PROJECTION_VERSION",
    "EvidenceVaultOperationalMemoryError",
    "LifecycleState",
    "ReviewState",
    "build_operational_memory_packet",
]
