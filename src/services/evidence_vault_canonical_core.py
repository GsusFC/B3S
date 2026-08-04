"""Pure, Vault-only contracts for candidate memory packages.

This module implements the executable core fixed by
``docs/evidence_vault_canonical_memory_adr_v1.md``:

- canonical JSON and content-addressed fingerprints;
- the current 80-tile contract-registry identity;
- a fail-closed polarity-to-state reducer; and
- an immutable, pending-review candidate-packet schema.

It deliberately does not read ledgers, persist promotions, project canonical
memory, calculate a score, or affect the scanner/runtime.
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Any, Iterable

from src.sv9.rubric import (
    COMPONENTS,
    PRESENTATION_ORDER,
    RUBRIC_VERSION,
    TILE_EVIDENCE_CONTRACT_VERSION,
)


EVIDENCE_VAULT_CANONICAL_JSON_VERSION = "evidence-vault-canonical-json-v1"
EVIDENCE_VAULT_TILE_CONTRACT_REGISTRY_VERSION = "evidence-vault-tile-contract-registry-v1"
EVIDENCE_VAULT_REDUCER_POLICY_VERSION = "evidence-vault-tile-reducer-policy-v1"
EVIDENCE_VAULT_TILE_REDUCTION_VERSION = "evidence-vault-tile-reduction-v1"
EVIDENCE_VAULT_CANDIDATE_TILE_VERSION = "evidence-vault-candidate-tile-v1"
EVIDENCE_VAULT_CANDIDATE_PACKET_VERSION = "evidence-vault-candidate-packet-v1"
EVIDENCE_VAULT_DERIVED_TILE_STATE_VERSION = "evidence-vault-derived-tile-state-v1"
EVIDENCE_VAULT_CANDIDATE_PACKET_FINGERPRINT_VERSION = "evidence-vault-candidate-packet-fingerprint-v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceVaultCanonicalCoreError(ValueError):
    """The candidate contract cannot be built or validated safely."""


class TileState(StrEnum):
    OK = "ok"
    NO = "no"
    SIN_EVIDENCIA = "sin_evidencia"
    CONTRADICTION = "contradiction"


class AuthorityState(StrEnum):
    UNREVIEWED = "unreviewed"
    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


class EvidencePolarity(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DEMONSTRATES_ABSENCE = "demonstrates_absence"
    INVALIDATES_CANDIDATE = "invalidates_candidate"
    IRRELEVANT = "irrelevant"


class BasisReviewStatus(StrEnum):
    ACCEPTED = "accepted"
    UNREVIEWED = "unreviewed"


class ProvenanceStatus(StrEnum):
    ACCEPTED_BASIS = "accepted_basis"
    SCANNER_DERIVED_UNREVIEWED = "scanner_derived_unreviewed"
    NO_ACCEPTED_BASIS = "no_accepted_basis"
    CONTRADICTION = "contradiction"
    COVERAGE_UNRESOLVED = "coverage_unresolved"


class EvidenceDeltaKind(StrEnum):
    BASELINE = "baseline"
    NO_CHANGE = "no_change"
    STRENGTHENED = "strengthened"
    CANDIDATE_UPDATE = "candidate_update"
    CONTRADICTION = "contradiction"
    COVERAGE_LOSS = "coverage_loss"
    VERIFIED_DEPRECATION = "verified_deprecation"


_BASIS_FIELDS = frozenset(
    {
        "relation_id",
        "evidence_id",
        "source_identity_id",
        "claim_id",
        "polarity",
        "review_status",
        "decision_event_id",
        "absence_test_contract_id",
        "coverage_assessment_id",
        "coverage_status",
        "tested_scope",
        "observed_result",
    }
)
_CANDIDATE_TILE_FIELDS = frozenset(
    {
        "schema_version",
        "component_key",
        "tile_id",
        "tile_key",
        "candidate_state",
        "delta_kind",
        "authority_state",
        "basis",
        "provenance_status",
        "previous_canonical_state",
        "single_source_dependency",
        "changes_tile_state",
        "changes_score",
        "coverage_refs",
        "unresolved_refs",
    }
)
_PACKET_FIELDS = frozenset(
    {
        "manifest",
        "candidate_tiles",
        "candidate_packet_fingerprint",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "brand_identity",
        "parent_canonical_memory_version",
        "authority_state",
        "candidate_memory_version",
        "accepted_memory_candidate_version",
        "reviewed_memory_candidate_version",
        "review_packet_set_fingerprint",
        "rubric_version",
        "tile_contract_registry_fingerprint",
        "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint",
        "coverage_summary",
        "unresolved_items",
        "candidate_count",
        "delta_summary",
        "derived_tile_state_fingerprint",
    }
)
_UNRESOLVED_ITEM_FIELDS = frozenset({"unresolved_id", "kind", "blocking", "details"})
_INCREMENTAL_UPDATE_FIELDS = frozenset(
    {
        "tile_id",
        "delta_kind",
        "basis",
        "coverage_refs",
        "unresolved_refs",
    }
)


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON or reject non-JSON material.

    Array order is preserved because it may be semantic. Builders in this
    module sort arrays whose order is explicitly non-semantic before hashing.
    """

    normalized = _canonical_json_value(value, path="$")
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EvidenceVaultCanonicalCoreError("value cannot be represented as canonical JSON") from exc


def canonical_fingerprint(namespace: str, value: Any) -> str:
    """Hash a tagged canonical object to avoid cross-contract collisions."""

    normalized_namespace = _required_text(namespace, field="namespace")
    rendered = canonical_json(
        {
            "canonical_json_version": (EVIDENCE_VAULT_CANONICAL_JSON_VERSION),
            "namespace": normalized_namespace,
            "payload": value,
        }
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def build_tile_contract_registry() -> dict[str, Any]:
    """Freeze the executable identity of the current 80 tile contracts."""

    tiles: list[dict[str, Any]] = []
    for component_position, component_key in enumerate(PRESENTATION_ORDER):
        component = COMPONENTS[component_key]
        for tile_position, tile in enumerate(component["tiles"]):
            absence_contract_ids = _normalized_text_list(
                tile.get("absence_test_contract_ids") or (),
                field=(f"{component_key}.{tile['id']}.absence_test_contract_ids"),
            )
            tiles.append(
                {
                    "component_key": component_key,
                    "component_position": component_position,
                    "tile_id": str(tile["id"]),
                    "tile_key": f"{component_key}.{tile['id']}",
                    "tile_position": tile_position,
                    "name": str(tile["name"]),
                    "condition": str(tile["condition"]),
                    "evidence_contract": dict(tile["evidence_contract"]),
                    "absence_test_contract_ids": absence_contract_ids,
                }
            )
    if len(tiles) != 80:
        raise EvidenceVaultCanonicalCoreError("tile contract registry must contain exactly 80 tiles")
    return {
        "schema_version": EVIDENCE_VAULT_TILE_CONTRACT_REGISTRY_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "tile_evidence_contract_version": (TILE_EVIDENCE_CONTRACT_VERSION),
        "tile_count": len(tiles),
        "tiles": tiles,
    }


def tile_contract_registry_fingerprint() -> str:
    return canonical_fingerprint(
        EVIDENCE_VAULT_TILE_CONTRACT_REGISTRY_VERSION,
        build_tile_contract_registry(),
    )


def build_reducer_policy() -> dict[str, Any]:
    """Return the content-addressed truth table implemented below."""

    return {
        "schema_version": EVIDENCE_VAULT_REDUCER_POLICY_VERSION,
        "states": [state.value for state in TileState],
        "polarities": [polarity.value for polarity in EvidencePolarity],
        "truth_table": [
            {
                "support": False,
                "counterevidence": False,
                "state": TileState.SIN_EVIDENCIA.value,
            },
            {
                "support": True,
                "counterevidence": False,
                "state": TileState.OK.value,
            },
            {
                "support": False,
                "counterevidence": True,
                "state": TileState.NO.value,
            },
            {
                "support": True,
                "counterevidence": True,
                "state": TileState.CONTRADICTION.value,
            },
        ],
        "counterevidence_polarities": [
            EvidencePolarity.CONTRADICTS.value,
            EvidencePolarity.DEMONSTRATES_ABSENCE.value,
        ],
        "ignored_for_state": [
            EvidencePolarity.INVALIDATES_CANDIDATE.value,
            EvidencePolarity.IRRELEVANT.value,
        ],
        "legacy_weakens_semantics": "review_required",
        "demonstrates_absence_requirements": [
            "declared_tile_absence_contract",
            "coverage_status_sufficient",
            "coverage_assessment_id",
            "tested_scope",
            "observed_result",
        ],
        "not_reacquired_semantics": "no_state_change",
    }


def reducer_policy_fingerprint() -> str:
    return canonical_fingerprint(
        EVIDENCE_VAULT_REDUCER_POLICY_VERSION,
        build_reducer_policy(),
    )


def reduce_tile_basis(
    basis: Iterable[dict[str, Any]],
    *,
    allowed_absence_contract_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Reduce proposed basis rows to one state without any side effect."""

    allowed_absence = frozenset(
        _normalized_text_list(
            allowed_absence_contract_ids,
            field="allowed_absence_contract_ids",
        )
    )
    rows = _normalized_basis_rows(
        basis,
        allowed_absence_contract_ids=allowed_absence,
    )
    support_ids = [row["relation_id"] for row in rows if row["polarity"] == EvidencePolarity.SUPPORTS.value]
    counterevidence_ids = [
        row["relation_id"]
        for row in rows
        if row["polarity"]
        in {
            EvidencePolarity.CONTRADICTS.value,
            EvidencePolarity.DEMONSTRATES_ABSENCE.value,
        }
    ]
    ignored_ids = [
        row["relation_id"]
        for row in rows
        if row["polarity"]
        in {
            EvidencePolarity.INVALIDATES_CANDIDATE.value,
            EvidencePolarity.IRRELEVANT.value,
        }
    ]
    state = _state_from_presence(
        support=bool(support_ids),
        counterevidence=bool(counterevidence_ids),
    )
    return {
        "schema_version": EVIDENCE_VAULT_TILE_REDUCTION_VERSION,
        "reducer_policy_fingerprint": reducer_policy_fingerprint(),
        "state": state.value,
        "relation_ids": [row["relation_id"] for row in rows],
        "support_relation_ids": support_ids,
        "counterevidence_relation_ids": counterevidence_ids,
        "ignored_relation_ids": ignored_ids,
    }


def build_candidate_tile(
    *,
    tile_id: str,
    basis: Iterable[dict[str, Any]] = (),
    previous_canonical_state: str | None = None,
    delta_kind: str | None = None,
    coverage_refs: Iterable[str] = (),
    unresolved_refs: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one deterministic pending-review tile candidate.

    ``basis`` is always the complete effective basis for the candidate, not
    merely the evidence reacquired by the latest scan. Once a canonical
    predecessor exists, callers must state the delta explicitly; this prevents
    an empty scan result from being interpreted as loss of canonical support.
    """

    registry_entry = _registry_by_tile_id().get(_required_text(tile_id, field="tile_id"))
    if registry_entry is None:
        raise EvidenceVaultCanonicalCoreError(f"unknown tile_id: {tile_id}")
    absence_contract_ids = registry_entry["absence_test_contract_ids"]
    normalized_basis = _normalized_basis_rows(
        basis,
        allowed_absence_contract_ids=frozenset(absence_contract_ids),
    )
    reduction = reduce_tile_basis(
        normalized_basis,
        allowed_absence_contract_ids=absence_contract_ids,
    )
    state = TileState(reduction["state"])
    previous_state = _optional_previous_state(previous_canonical_state)
    normalized_delta_kind = _candidate_delta_kind(
        delta_kind,
        previous_state=previous_state,
    )
    normalized_coverage_refs = set(_normalized_text_list(coverage_refs, field="coverage_refs"))
    normalized_coverage_refs.update(
        str(row["coverage_assessment_id"])
        for row in normalized_basis
        if row["polarity"] == EvidencePolarity.DEMONSTRATES_ABSENCE.value
    )
    normalized_unresolved_refs = _normalized_text_list(
        unresolved_refs,
        field="unresolved_refs",
    )
    if state is TileState.CONTRADICTION and not normalized_unresolved_refs:
        raise EvidenceVaultCanonicalCoreError("contradiction candidates require an unresolved reference")
    _validate_delta_state(
        delta_kind=normalized_delta_kind,
        previous_state=previous_state,
        candidate_state=state,
    )
    provenance = _provenance_status(
        state=state,
        basis=normalized_basis,
        unresolved_refs=normalized_unresolved_refs,
    )
    changes_tile_state, changes_score = _change_flags(
        previous=previous_state,
        candidate=state,
    )
    effective_source_ids = {
        row["source_identity_id"]
        for row in normalized_basis
        if row["polarity"]
        in {
            EvidencePolarity.SUPPORTS.value,
            EvidencePolarity.CONTRADICTS.value,
            EvidencePolarity.DEMONSTRATES_ABSENCE.value,
        }
    }
    return {
        "schema_version": EVIDENCE_VAULT_CANDIDATE_TILE_VERSION,
        "component_key": registry_entry["component_key"],
        "tile_id": registry_entry["tile_id"],
        "tile_key": registry_entry["tile_key"],
        "candidate_state": state.value,
        "delta_kind": normalized_delta_kind.value,
        "authority_state": AuthorityState.PENDING_REVIEW.value,
        "basis": normalized_basis,
        "provenance_status": provenance.value,
        "previous_canonical_state": (previous_state.value if previous_state is not None else None),
        "single_source_dependency": len(effective_source_ids) == 1,
        "changes_tile_state": changes_tile_state,
        "changes_score": changes_score,
        "coverage_refs": sorted(normalized_coverage_refs),
        "unresolved_refs": normalized_unresolved_refs,
    }


def build_incremental_candidate_tiles(
    *,
    previous_candidate_tiles: Iterable[dict[str, Any]],
    tile_updates: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Derive all 80 candidates from the previous promoted tile snapshot.

    Tiles without an update are copied semantically as ``no_change``. A
    ``coverage_loss`` also preserves the previous basis and state. Updates
    that may alter canonical content must supply their complete effective
    basis, so this function never substitutes "what the latest scan saw" for
    the accumulated memory.

    Authority is intentionally out of scope: the returned rows remain
    ``pending_review`` and the input snapshot is never mutated.
    """

    previous_tiles = _normalized_candidate_tiles(previous_candidate_tiles)
    if any(row["candidate_state"] == TileState.CONTRADICTION.value for row in previous_tiles):
        raise EvidenceVaultCanonicalCoreError("a canonical predecessor cannot contain contradiction")
    updates = _normalized_incremental_updates(tile_updates)
    unknown_tile_ids = sorted(set(updates) - {row["tile_id"] for row in previous_tiles})
    if unknown_tile_ids:
        raise EvidenceVaultCanonicalCoreError(
            "incremental updates contain unknown tile ids: " + ", ".join(unknown_tile_ids)
        )

    candidates: list[dict[str, Any]] = []
    for previous in previous_tiles:
        tile_id = previous["tile_id"]
        update = updates.get(tile_id)
        if update is None or update["delta_kind"] == EvidenceDeltaKind.NO_CHANGE.value:
            candidates.append(
                build_candidate_tile(
                    tile_id=tile_id,
                    basis=previous["basis"],
                    previous_canonical_state=previous["candidate_state"],
                    delta_kind=EvidenceDeltaKind.NO_CHANGE.value,
                    coverage_refs=previous["coverage_refs"],
                    unresolved_refs=previous["unresolved_refs"],
                )
            )
            continue

        delta_kind = EvidenceDeltaKind(update["delta_kind"])
        previous_basis = previous["basis"]
        if delta_kind is EvidenceDeltaKind.COVERAGE_LOSS:
            new_coverage_refs = sorted(set(previous["coverage_refs"]) | set(update["coverage_refs"]))
            if new_coverage_refs == previous["coverage_refs"]:
                raise EvidenceVaultCanonicalCoreError("coverage_loss requires a new coverage reference")
            candidates.append(
                build_candidate_tile(
                    tile_id=tile_id,
                    basis=previous_basis,
                    previous_canonical_state=previous["candidate_state"],
                    delta_kind=delta_kind.value,
                    coverage_refs=new_coverage_refs,
                    unresolved_refs=(set(previous["unresolved_refs"]) | set(update["unresolved_refs"])),
                )
            )
            continue

        effective_basis = update["basis"]
        candidate = build_candidate_tile(
            tile_id=tile_id,
            basis=effective_basis,
            previous_canonical_state=previous["candidate_state"],
            delta_kind=delta_kind.value,
            coverage_refs=(set(previous["coverage_refs"]) | set(update["coverage_refs"])),
            unresolved_refs=(
                update["unresolved_refs"] if update["replaces_unresolved_refs"] else previous["unresolved_refs"]
            ),
        )
        _validate_incremental_basis_transition(
            delta_kind=delta_kind,
            previous_basis=previous_basis,
            candidate_basis=candidate["basis"],
            previous_state=TileState(previous["candidate_state"]),
        )
        candidates.append(candidate)

    validate_incremental_candidate_tiles(
        previous_candidate_tiles=previous_tiles,
        candidate_tiles=candidates,
    )
    return candidates


def validate_incremental_candidate_tiles(
    *,
    previous_candidate_tiles: Iterable[dict[str, Any]],
    candidate_tiles: Iterable[dict[str, Any]],
) -> None:
    """Validate an incremental tile set against its resolved canonical parent.

    The packet fingerprint binds the declared parent version, but only the
    authority boundary can resolve that version to content. Promotion must use
    this validator after loading the parent so a forged ``no_change`` or
    ``coverage_loss`` cannot replace accumulated basis.
    """

    previous_tiles = _normalized_candidate_tiles(previous_candidate_tiles)
    candidates = _normalized_candidate_tiles(candidate_tiles)
    if any(row["candidate_state"] == TileState.CONTRADICTION.value for row in previous_tiles):
        raise EvidenceVaultCanonicalCoreError("a canonical predecessor cannot contain contradiction")
    for previous, candidate in zip(previous_tiles, candidates, strict=True):
        if previous["tile_id"] != candidate["tile_id"]:
            raise EvidenceVaultCanonicalCoreError("incremental tile order mismatch")
        if candidate["previous_canonical_state"] != previous["candidate_state"]:
            raise EvidenceVaultCanonicalCoreError(
                f"incremental tile {candidate['tile_id']} does not match its canonical predecessor"
            )
        delta_kind = EvidenceDeltaKind(candidate["delta_kind"])
        if delta_kind is EvidenceDeltaKind.BASELINE:
            raise EvidenceVaultCanonicalCoreError("incremental tile cannot declare baseline")
        if delta_kind is EvidenceDeltaKind.NO_CHANGE:
            for field in ("candidate_state", "basis", "coverage_refs", "unresolved_refs"):
                previous_field = "candidate_state" if field == "candidate_state" else field
                if candidate[field] != previous[previous_field]:
                    raise EvidenceVaultCanonicalCoreError(
                        f"no_change must reuse canonical {field} for tile {candidate['tile_id']}"
                    )
            continue
        if delta_kind is EvidenceDeltaKind.COVERAGE_LOSS:
            if candidate["basis"] != previous["basis"]:
                raise EvidenceVaultCanonicalCoreError(
                    f"coverage_loss cannot replace canonical basis for tile {candidate['tile_id']}"
                )
            previous_coverage = set(previous["coverage_refs"])
            candidate_coverage = set(candidate["coverage_refs"])
            if not previous_coverage < candidate_coverage:
                raise EvidenceVaultCanonicalCoreError(
                    f"coverage_loss requires a new coverage reference for tile {candidate['tile_id']}"
                )
            if not set(previous["unresolved_refs"]).issubset(candidate["unresolved_refs"]):
                raise EvidenceVaultCanonicalCoreError(
                    f"coverage_loss cannot clear unresolved references for tile {candidate['tile_id']}"
                )
            continue
        if not set(previous["coverage_refs"]).issubset(candidate["coverage_refs"]):
            raise EvidenceVaultCanonicalCoreError(
                f"incremental update cannot discard coverage history for tile {candidate['tile_id']}"
            )
        _validate_incremental_basis_transition(
            delta_kind=delta_kind,
            previous_basis=previous["basis"],
            candidate_basis=candidate["basis"],
            previous_state=TileState(previous["candidate_state"]),
        )


def build_candidate_packet(
    *,
    brand_identity: str,
    parent_canonical_memory_version: str | None,
    candidate_memory_version: str,
    accepted_memory_candidate_version: str,
    reviewed_memory_candidate_version: str,
    review_packet_set_fingerprint: str,
    aggregation_policy_fingerprint: str,
    candidate_tiles: Iterable[dict[str, Any]],
    coverage_summary: dict[str, Any] | None = None,
    unresolved_items: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build and self-validate one atomic pending-review packet."""

    tiles = _normalized_candidate_tiles(candidate_tiles)
    unresolved = _normalized_unresolved_items(unresolved_items)
    _validate_tile_references(tiles, unresolved_items=unresolved)
    derived_fingerprint = _derived_tile_state_fingerprint(tiles)
    delta_summary = _delta_summary(tiles)
    normalized_parent = _optional_sha256(
        parent_canonical_memory_version,
        field="parent_canonical_memory_version",
    )
    _validate_packet_ancestry(tiles, parent_canonical_memory_version=normalized_parent)
    manifest = {
        "schema_version": EVIDENCE_VAULT_CANDIDATE_PACKET_VERSION,
        "brand_identity": _required_text(
            brand_identity,
            field="brand_identity",
        ).casefold(),
        "parent_canonical_memory_version": normalized_parent,
        "authority_state": AuthorityState.PENDING_REVIEW.value,
        "candidate_memory_version": _required_sha256(
            candidate_memory_version,
            field="candidate_memory_version",
        ),
        "accepted_memory_candidate_version": _required_sha256(
            accepted_memory_candidate_version,
            field="accepted_memory_candidate_version",
        ),
        "reviewed_memory_candidate_version": _required_sha256(
            reviewed_memory_candidate_version,
            field="reviewed_memory_candidate_version",
        ),
        "review_packet_set_fingerprint": _required_sha256(
            review_packet_set_fingerprint,
            field="review_packet_set_fingerprint",
        ),
        "rubric_version": RUBRIC_VERSION,
        "tile_contract_registry_fingerprint": (tile_contract_registry_fingerprint()),
        "reducer_policy_fingerprint": reducer_policy_fingerprint(),
        "aggregation_policy_fingerprint": _required_sha256(
            aggregation_policy_fingerprint,
            field="aggregation_policy_fingerprint",
        ),
        "coverage_summary": _canonical_json_object(
            coverage_summary or {},
            field="coverage_summary",
        ),
        "unresolved_items": unresolved,
        "candidate_count": len(tiles),
        "delta_summary": delta_summary,
        "derived_tile_state_fingerprint": derived_fingerprint,
    }
    unsigned_packet = {
        "manifest": manifest,
        "candidate_tiles": tiles,
    }
    packet = {
        **unsigned_packet,
        "candidate_packet_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_CANDIDATE_PACKET_FINGERPRINT_VERSION,
            unsigned_packet,
        ),
    }
    validate_candidate_packet(packet)
    return packet


def validate_candidate_packet(packet: dict[str, Any]) -> None:
    """Recompute all identities and reject altered or mixed packets."""

    if not isinstance(packet, dict):
        raise EvidenceVaultCanonicalCoreError("candidate packet must be an object")
    _require_exact_fields(packet, _PACKET_FIELDS, label="candidate packet")
    manifest = packet.get("manifest")
    tiles = packet.get("candidate_tiles")
    if not isinstance(manifest, dict) or not isinstance(tiles, list):
        raise EvidenceVaultCanonicalCoreError("candidate packet manifest and candidate_tiles are required")
    _require_exact_fields(manifest, _MANIFEST_FIELDS, label="manifest")
    if manifest.get("schema_version") != EVIDENCE_VAULT_CANDIDATE_PACKET_VERSION:
        raise EvidenceVaultCanonicalCoreError("unsupported candidate packet schema version")
    if manifest.get("authority_state") != AuthorityState.PENDING_REVIEW.value:
        raise EvidenceVaultCanonicalCoreError("candidate packet authority_state must remain pending_review")
    if manifest.get("rubric_version") != RUBRIC_VERSION:
        raise EvidenceVaultCanonicalCoreError("candidate packet rubric version mismatch")
    normalized_brand_identity = _required_text(manifest.get("brand_identity"), field="brand_identity").casefold()
    if manifest.get("brand_identity") != normalized_brand_identity:
        raise EvidenceVaultCanonicalCoreError("brand_identity is not canonically normalized")
    normalized_parent = _optional_sha256(
        manifest.get("parent_canonical_memory_version"),
        field="parent_canonical_memory_version",
    )
    if manifest.get("parent_canonical_memory_version") != normalized_parent:
        raise EvidenceVaultCanonicalCoreError("parent_canonical_memory_version is not canonically normalized")
    for field in (
        "candidate_memory_version",
        "accepted_memory_candidate_version",
        "reviewed_memory_candidate_version",
        "review_packet_set_fingerprint",
        "aggregation_policy_fingerprint",
        "derived_tile_state_fingerprint",
    ):
        normalized_fingerprint = _required_sha256(manifest.get(field), field=field)
        if manifest.get(field) != normalized_fingerprint:
            raise EvidenceVaultCanonicalCoreError(f"{field} is not canonically normalized")
    if manifest.get("tile_contract_registry_fingerprint") != tile_contract_registry_fingerprint():
        raise EvidenceVaultCanonicalCoreError("tile contract registry fingerprint mismatch")
    if manifest.get("reducer_policy_fingerprint") != reducer_policy_fingerprint():
        raise EvidenceVaultCanonicalCoreError("reducer policy fingerprint mismatch")
    coverage_summary = manifest.get("coverage_summary")
    _canonical_json_object(coverage_summary, field="coverage_summary")
    unresolved = _normalized_unresolved_items(manifest.get("unresolved_items") or [])
    if manifest.get("unresolved_items") != unresolved:
        raise EvidenceVaultCanonicalCoreError("unresolved items are not canonically normalized")
    normalized_tiles = _normalized_candidate_tiles(tiles)
    if tiles != normalized_tiles:
        raise EvidenceVaultCanonicalCoreError("candidate tiles are not canonically normalized")
    if manifest.get("candidate_count") != len(normalized_tiles):
        raise EvidenceVaultCanonicalCoreError("candidate packet count does not match candidate tiles")
    _validate_packet_ancestry(
        normalized_tiles,
        parent_canonical_memory_version=normalized_parent,
    )
    expected_delta_summary = _delta_summary(normalized_tiles)
    if manifest.get("delta_summary") != expected_delta_summary:
        raise EvidenceVaultCanonicalCoreError("candidate packet delta summary mismatch")
    _validate_tile_references(normalized_tiles, unresolved_items=unresolved)
    expected_derived = _derived_tile_state_fingerprint(normalized_tiles)
    if manifest.get("derived_tile_state_fingerprint") != expected_derived:
        raise EvidenceVaultCanonicalCoreError("derived tile state fingerprint mismatch")
    expected_packet = canonical_fingerprint(
        EVIDENCE_VAULT_CANDIDATE_PACKET_FINGERPRINT_VERSION,
        {"manifest": manifest, "candidate_tiles": normalized_tiles},
    )
    if packet.get("candidate_packet_fingerprint") != expected_packet:
        raise EvidenceVaultCanonicalCoreError("candidate packet fingerprint mismatch")


def _canonical_json_value(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvidenceVaultCanonicalCoreError(f"{path} contains a non-finite number")
        return 0.0 if value == 0.0 else value
    if isinstance(value, list):
        return [_canonical_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise EvidenceVaultCanonicalCoreError(f"{path} contains a non-string object key")
        return {key: _canonical_json_value(value[key], path=f"{path}.{key}") for key in sorted(value)}
    raise EvidenceVaultCanonicalCoreError(f"{path} contains unsupported type {type(value).__name__}")


def _canonical_json_object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceVaultCanonicalCoreError(f"{field} must be an object")
    normalized = _canonical_json_value(value, path=f"$.{field}")
    canonical_json(normalized)
    return normalized


def _registry_by_tile_id() -> dict[str, dict[str, Any]]:
    return {str(row["tile_id"]): row for row in build_tile_contract_registry()["tiles"]}


def _tile_order() -> dict[str, int]:
    return {str(row["tile_id"]): index for index, row in enumerate(build_tile_contract_registry()["tiles"])}


def _normalized_basis_rows(
    basis: Iterable[dict[str, Any]],
    *,
    allowed_absence_contract_ids: frozenset[str],
) -> list[dict[str, Any]]:
    if isinstance(basis, (str, bytes, dict)):
        raise EvidenceVaultCanonicalCoreError("basis must be an array")
    rows: list[dict[str, Any]] = []
    try:
        raw_rows = list(basis)
    except TypeError as exc:
        raise EvidenceVaultCanonicalCoreError("basis must be an array") from exc
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise EvidenceVaultCanonicalCoreError(f"basis[{index}] must be an object")
        rows.append(
            _normalized_basis_row(
                raw,
                allowed_absence_contract_ids=allowed_absence_contract_ids,
                index=index,
            )
        )
    rows.sort(key=lambda row: row["relation_id"])
    relation_ids = [row["relation_id"] for row in rows]
    if len(relation_ids) != len(set(relation_ids)):
        raise EvidenceVaultCanonicalCoreError("basis contains duplicate relation ids")
    return rows


def _normalized_basis_row(
    raw: dict[str, Any],
    *,
    allowed_absence_contract_ids: frozenset[str],
    index: int,
) -> dict[str, Any]:
    unknown = set(raw) - _BASIS_FIELDS
    if unknown:
        raise EvidenceVaultCanonicalCoreError(
            f"basis[{index}] contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    relation_id = _required_sha256(
        raw.get("relation_id"),
        field=f"basis[{index}].relation_id",
    )
    evidence_id = _required_sha256(
        raw.get("evidence_id"),
        field=f"basis[{index}].evidence_id",
    )
    source_identity_id = _required_sha256(
        raw.get("source_identity_id"),
        field=f"basis[{index}].source_identity_id",
    )
    claim_id = _optional_sha256(
        raw.get("claim_id"),
        field=f"basis[{index}].claim_id",
    )
    polarity_text = _required_text(
        raw.get("polarity"),
        field=f"basis[{index}].polarity",
    )
    try:
        polarity = EvidencePolarity(polarity_text)
    except ValueError as exc:
        raise EvidenceVaultCanonicalCoreError(f"unsupported evidence polarity: {polarity_text}") from exc
    raw_review_status = raw.get("review_status")
    review_text = (
        BasisReviewStatus.UNREVIEWED.value
        if raw_review_status is None
        else _required_text(
            raw_review_status,
            field=f"basis[{index}].review_status",
        )
    )
    try:
        review_status = BasisReviewStatus(review_text)
    except ValueError as exc:
        raise EvidenceVaultCanonicalCoreError(f"unsupported basis review status: {review_text}") from exc
    decision_event_id = _optional_text(
        raw.get("decision_event_id"),
        field=f"basis[{index}].decision_event_id",
    )
    if review_status is BasisReviewStatus.ACCEPTED and decision_event_id is None:
        raise EvidenceVaultCanonicalCoreError("accepted basis requires decision_event_id")
    if review_status is BasisReviewStatus.UNREVIEWED and decision_event_id is not None:
        raise EvidenceVaultCanonicalCoreError("unreviewed basis cannot declare decision_event_id")
    absence_test_contract_id = _optional_text(
        raw.get("absence_test_contract_id"),
        field=f"basis[{index}].absence_test_contract_id",
    )
    coverage_assessment_id = _optional_text(
        raw.get("coverage_assessment_id"),
        field=f"basis[{index}].coverage_assessment_id",
    )
    coverage_status = _optional_text(
        raw.get("coverage_status"),
        field=f"basis[{index}].coverage_status",
    )
    tested_scope = _optional_text(
        raw.get("tested_scope"),
        field=f"basis[{index}].tested_scope",
    )
    observed_result = _optional_text(
        raw.get("observed_result"),
        field=f"basis[{index}].observed_result",
    )
    absence_fields = (
        absence_test_contract_id,
        coverage_assessment_id,
        coverage_status,
        tested_scope,
        observed_result,
    )
    if polarity is EvidencePolarity.DEMONSTRATES_ABSENCE:
        if absence_test_contract_id not in allowed_absence_contract_ids:
            raise EvidenceVaultCanonicalCoreError("demonstrates_absence requires a declared tile absence contract")
        if coverage_status != "sufficient":
            raise EvidenceVaultCanonicalCoreError("demonstrates_absence requires sufficient coverage")
        if any(value is None for value in absence_fields):
            raise EvidenceVaultCanonicalCoreError("demonstrates_absence metadata is incomplete")
    elif any(value is not None for value in absence_fields):
        raise EvidenceVaultCanonicalCoreError("absence metadata is only valid for demonstrates_absence")
    return {
        "relation_id": relation_id,
        "evidence_id": evidence_id,
        "source_identity_id": source_identity_id,
        "claim_id": claim_id,
        "polarity": polarity.value,
        "review_status": review_status.value,
        "decision_event_id": decision_event_id,
        "absence_test_contract_id": absence_test_contract_id,
        "coverage_assessment_id": coverage_assessment_id,
        "coverage_status": coverage_status,
        "tested_scope": tested_scope,
        "observed_result": observed_result,
    }


def _state_from_presence(
    *,
    support: bool,
    counterevidence: bool,
) -> TileState:
    if support and counterevidence:
        return TileState.CONTRADICTION
    if support:
        return TileState.OK
    if counterevidence:
        return TileState.NO
    return TileState.SIN_EVIDENCIA


def _candidate_delta_kind(
    value: Any,
    *,
    previous_state: TileState | None,
) -> EvidenceDeltaKind:
    if value is None:
        if previous_state is None:
            return EvidenceDeltaKind.BASELINE
        raise EvidenceVaultCanonicalCoreError("delta_kind is required when a canonical predecessor exists")
    text = _required_text(value, field="delta_kind")
    try:
        return EvidenceDeltaKind(text)
    except ValueError as exc:
        raise EvidenceVaultCanonicalCoreError(f"unsupported evidence delta kind: {text}") from exc


def _validate_delta_state(
    *,
    delta_kind: EvidenceDeltaKind,
    previous_state: TileState | None,
    candidate_state: TileState,
) -> None:
    if delta_kind is EvidenceDeltaKind.BASELINE:
        if previous_state is not None:
            raise EvidenceVaultCanonicalCoreError("baseline cannot declare a canonical predecessor")
        return
    if previous_state is None:
        raise EvidenceVaultCanonicalCoreError(f"{delta_kind.value} requires a canonical predecessor")
    if delta_kind in {
        EvidenceDeltaKind.NO_CHANGE,
        EvidenceDeltaKind.STRENGTHENED,
        EvidenceDeltaKind.COVERAGE_LOSS,
    }:
        if candidate_state is not previous_state:
            raise EvidenceVaultCanonicalCoreError(f"{delta_kind.value} must preserve the previous canonical state")
        return
    if delta_kind is EvidenceDeltaKind.CONTRADICTION:
        if candidate_state is not TileState.CONTRADICTION:
            raise EvidenceVaultCanonicalCoreError("contradiction delta must reduce to contradiction")
        return
    if delta_kind is EvidenceDeltaKind.VERIFIED_DEPRECATION:
        if candidate_state is TileState.CONTRADICTION:
            raise EvidenceVaultCanonicalCoreError("verified_deprecation cannot encode a contradiction")
        return
    if candidate_state is TileState.CONTRADICTION:
        raise EvidenceVaultCanonicalCoreError(f"{delta_kind.value} cannot encode a contradiction")
    if candidate_state is previous_state:
        raise EvidenceVaultCanonicalCoreError(f"{delta_kind.value} must change the previous canonical state")


def _normalized_incremental_updates(
    tile_updates: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if isinstance(tile_updates, (str, bytes, dict)):
        raise EvidenceVaultCanonicalCoreError("tile_updates must be an array")
    try:
        raw_updates = list(tile_updates)
    except TypeError as exc:
        raise EvidenceVaultCanonicalCoreError("tile_updates must be an array") from exc
    normalized: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_updates):
        if not isinstance(raw, dict):
            raise EvidenceVaultCanonicalCoreError(f"tile_updates[{index}] must be an object")
        unknown = set(raw) - _INCREMENTAL_UPDATE_FIELDS
        if unknown:
            raise EvidenceVaultCanonicalCoreError(
                f"tile_updates[{index}] contains unsupported fields: " + ", ".join(sorted(unknown))
            )
        tile_id = _required_text(raw.get("tile_id"), field=f"tile_updates[{index}].tile_id")
        if tile_id in normalized:
            raise EvidenceVaultCanonicalCoreError("tile_updates contains duplicate tile ids")
        delta_text = _required_text(
            raw.get("delta_kind"),
            field=f"tile_updates[{index}].delta_kind",
        )
        try:
            delta_kind = EvidenceDeltaKind(delta_text)
        except ValueError as exc:
            raise EvidenceVaultCanonicalCoreError(f"unsupported evidence delta kind: {delta_text}") from exc
        if delta_kind is EvidenceDeltaKind.BASELINE:
            raise EvidenceVaultCanonicalCoreError("incremental updates cannot declare baseline")
        if delta_kind is EvidenceDeltaKind.NO_CHANGE:
            unexpected = set(raw) - {"tile_id", "delta_kind"}
            if unexpected:
                raise EvidenceVaultCanonicalCoreError("no_change cannot carry update payload")
            basis: list[dict[str, Any]] | None = None
            coverage_refs: list[str] = []
            unresolved_refs: list[str] = []
            replaces_unresolved_refs = False
        elif delta_kind is EvidenceDeltaKind.COVERAGE_LOSS:
            if "basis" in raw:
                raise EvidenceVaultCanonicalCoreError("coverage_loss cannot replace the canonical basis")
            coverage_refs = _normalized_text_list(
                raw.get("coverage_refs") or (),
                field=f"tile_updates[{index}].coverage_refs",
            )
            if not coverage_refs:
                raise EvidenceVaultCanonicalCoreError("coverage_loss requires a coverage reference")
            unresolved_refs = _normalized_text_list(
                raw.get("unresolved_refs") or (),
                field=f"tile_updates[{index}].unresolved_refs",
            )
            basis = None
            replaces_unresolved_refs = False
        else:
            if "basis" not in raw:
                raise EvidenceVaultCanonicalCoreError(f"{delta_kind.value} requires the complete effective basis")
            raw_basis = raw.get("basis")
            if not isinstance(raw_basis, list):
                raise EvidenceVaultCanonicalCoreError(f"tile_updates[{index}].basis must be an array")
            basis = raw_basis
            coverage_refs = _normalized_text_list(
                raw.get("coverage_refs") or (),
                field=f"tile_updates[{index}].coverage_refs",
            )
            unresolved_refs = _normalized_text_list(
                raw.get("unresolved_refs") or (),
                field=f"tile_updates[{index}].unresolved_refs",
            )
            replaces_unresolved_refs = "unresolved_refs" in raw
            if delta_kind is EvidenceDeltaKind.CONTRADICTION and not unresolved_refs:
                raise EvidenceVaultCanonicalCoreError("contradiction update requires an unresolved reference")
        normalized[tile_id] = {
            "tile_id": tile_id,
            "delta_kind": delta_kind.value,
            "basis": basis,
            "coverage_refs": coverage_refs,
            "unresolved_refs": unresolved_refs,
            "replaces_unresolved_refs": replaces_unresolved_refs,
        }
    return normalized


def _validate_incremental_basis_transition(
    *,
    delta_kind: EvidenceDeltaKind,
    previous_basis: list[dict[str, Any]],
    candidate_basis: list[dict[str, Any]],
    previous_state: TileState,
) -> None:
    previous_relation_ids = {row["relation_id"] for row in previous_basis}
    candidate_relation_ids = {row["relation_id"] for row in candidate_basis}
    if delta_kind is EvidenceDeltaKind.STRENGTHENED:
        if not previous_relation_ids.issubset(candidate_relation_ids):
            raise EvidenceVaultCanonicalCoreError("strengthened cannot remove canonical basis relations")
        new_effective_rows = [
            row
            for row in candidate_basis
            if row["relation_id"] not in previous_relation_ids
            and _polarity_supports_state(row["polarity"], previous_state)
        ]
        if not new_effective_rows:
            raise EvidenceVaultCanonicalCoreError("strengthened requires new effective support for the existing state")
    if delta_kind is EvidenceDeltaKind.VERIFIED_DEPRECATION:
        removed_effective = {
            row["relation_id"] for row in previous_basis if _polarity_supports_state(row["polarity"], previous_state)
        } - candidate_relation_ids
        has_invalidation = any(
            row["polarity"] == EvidencePolarity.INVALIDATES_CANDIDATE.value for row in candidate_basis
        )
        if not removed_effective or not has_invalidation:
            raise EvidenceVaultCanonicalCoreError(
                "verified_deprecation requires a removed effective relation and an invalidation basis row"
            )


def _polarity_supports_state(polarity: str, state: TileState) -> bool:
    if state is TileState.OK:
        return polarity == EvidencePolarity.SUPPORTS.value
    if state is TileState.NO:
        return polarity in {
            EvidencePolarity.CONTRADICTS.value,
            EvidencePolarity.DEMONSTRATES_ABSENCE.value,
        }
    return False


def _delta_summary(tiles: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "by_kind": {kind.value: sum(row["delta_kind"] == kind.value for row in tiles) for kind in EvidenceDeltaKind},
        "tile_state_change_count": sum(bool(row["changes_tile_state"]) for row in tiles),
        "score_affecting_count": sum(bool(row["changes_score"]) for row in tiles),
    }


def _validate_packet_ancestry(
    tiles: list[dict[str, Any]],
    *,
    parent_canonical_memory_version: str | None,
) -> None:
    delta_kinds = {row["delta_kind"] for row in tiles}
    if EvidenceDeltaKind.BASELINE.value in delta_kinds:
        if delta_kinds != {EvidenceDeltaKind.BASELINE.value}:
            raise EvidenceVaultCanonicalCoreError("candidate packet cannot mix baseline and incremental tiles")
        if parent_canonical_memory_version is not None:
            raise EvidenceVaultCanonicalCoreError("baseline candidate packet cannot declare a canonical parent")
        return
    if parent_canonical_memory_version is None:
        raise EvidenceVaultCanonicalCoreError("incremental candidate packet requires a canonical parent")


def _optional_previous_state(value: Any) -> TileState | None:
    if value is None:
        return None
    try:
        state = TileState(str(value).strip())
    except ValueError as exc:
        raise EvidenceVaultCanonicalCoreError(f"unsupported previous canonical state: {value}") from exc
    if state is TileState.CONTRADICTION:
        raise EvidenceVaultCanonicalCoreError("a canonical predecessor cannot be contradiction")
    return state


def _provenance_status(
    *,
    state: TileState,
    basis: list[dict[str, Any]],
    unresolved_refs: list[str],
) -> ProvenanceStatus:
    if state is TileState.CONTRADICTION:
        return ProvenanceStatus.CONTRADICTION
    if unresolved_refs:
        return ProvenanceStatus.COVERAGE_UNRESOLVED
    effective = [
        row
        for row in basis
        if row["polarity"]
        in {
            EvidencePolarity.SUPPORTS.value,
            EvidencePolarity.CONTRADICTS.value,
            EvidencePolarity.DEMONSTRATES_ABSENCE.value,
        }
    ]
    if not effective:
        return ProvenanceStatus.NO_ACCEPTED_BASIS
    if any(row["review_status"] == BasisReviewStatus.UNREVIEWED.value for row in effective):
        return ProvenanceStatus.SCANNER_DERIVED_UNREVIEWED
    return ProvenanceStatus.ACCEPTED_BASIS


def _change_flags(
    *,
    previous: TileState | None,
    candidate: TileState,
) -> tuple[bool, bool]:
    if previous is None:
        return False, False
    changes_tile_state = candidate is not previous
    if candidate is TileState.CONTRADICTION:
        return changes_tile_state, True
    previous_points = 1 if previous is TileState.OK else 0
    candidate_points = 1 if candidate is TileState.OK else 0
    return changes_tile_state, previous_points != candidate_points


def _normalized_candidate_tiles(
    candidate_tiles: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(candidate_tiles, (str, bytes, dict)):
        raise EvidenceVaultCanonicalCoreError("candidate_tiles must be an array")
    try:
        raw_tiles = list(candidate_tiles)
    except TypeError as exc:
        raise EvidenceVaultCanonicalCoreError("candidate_tiles must be an array") from exc
    order = _tile_order()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_tiles):
        if not isinstance(raw, dict):
            raise EvidenceVaultCanonicalCoreError(f"candidate_tiles[{index}] must be an object")
        _require_exact_fields(
            raw,
            _CANDIDATE_TILE_FIELDS,
            label=f"candidate_tiles[{index}]",
        )
        expected = build_candidate_tile(
            tile_id=str(raw.get("tile_id") or ""),
            basis=raw.get("basis") or [],
            previous_canonical_state=raw.get("previous_canonical_state"),
            delta_kind=raw.get("delta_kind"),
            coverage_refs=raw.get("coverage_refs") or [],
            unresolved_refs=raw.get("unresolved_refs") or [],
        )
        if raw != expected:
            raise EvidenceVaultCanonicalCoreError(
                f"candidate tile does not match reducer output: {raw.get('tile_id') or '<missing>'}"
            )
        normalized.append(expected)
    tile_ids = [row["tile_id"] for row in normalized]
    if len(tile_ids) != len(set(tile_ids)):
        raise EvidenceVaultCanonicalCoreError("candidate packet contains duplicate tile ids")
    expected_ids = set(order)
    if set(tile_ids) != expected_ids:
        missing = sorted(expected_ids - set(tile_ids))
        extra = sorted(set(tile_ids) - expected_ids)
        raise EvidenceVaultCanonicalCoreError(
            f"candidate packet tile coverage mismatch; missing={missing}; extra={extra}"
        )
    normalized.sort(key=lambda row: order[row["tile_id"]])
    return normalized


def _normalized_unresolved_items(
    unresolved_items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(unresolved_items, (str, bytes, dict)):
        raise EvidenceVaultCanonicalCoreError("unresolved_items must be an array")
    try:
        raw_items = list(unresolved_items)
    except TypeError as exc:
        raise EvidenceVaultCanonicalCoreError("unresolved_items must be an array") from exc
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise EvidenceVaultCanonicalCoreError(f"unresolved_items[{index}] must be an object")
        unknown = set(raw) - _UNRESOLVED_ITEM_FIELDS
        if unknown:
            raise EvidenceVaultCanonicalCoreError(
                f"unresolved_items[{index}] contains unsupported fields: " + ", ".join(sorted(unknown))
            )
        blocking = raw.get("blocking")
        if not isinstance(blocking, bool):
            raise EvidenceVaultCanonicalCoreError(f"unresolved_items[{index}].blocking must be boolean")
        normalized.append(
            {
                "unresolved_id": _required_text(
                    raw.get("unresolved_id"),
                    field=f"unresolved_items[{index}].unresolved_id",
                ),
                "kind": _required_text(
                    raw.get("kind"),
                    field=f"unresolved_items[{index}].kind",
                ),
                "blocking": blocking,
                "details": _canonical_json_object(
                    raw.get("details") or {},
                    field=f"unresolved_items[{index}].details",
                ),
            }
        )
    normalized.sort(key=lambda item: item["unresolved_id"])
    unresolved_ids = [item["unresolved_id"] for item in normalized]
    if len(unresolved_ids) != len(set(unresolved_ids)):
        raise EvidenceVaultCanonicalCoreError("unresolved_items contains duplicate ids")
    return normalized


def _validate_tile_references(
    tiles: list[dict[str, Any]],
    *,
    unresolved_items: list[dict[str, Any]],
) -> None:
    unresolved_by_id = {item["unresolved_id"]: item for item in unresolved_items}
    unknown_refs = sorted({ref for tile in tiles for ref in tile["unresolved_refs"] if ref not in unresolved_by_id})
    if unknown_refs:
        raise EvidenceVaultCanonicalCoreError(
            "candidate tiles reference unknown unresolved items: " + ", ".join(unknown_refs)
        )
    for tile in tiles:
        if tile["candidate_state"] != TileState.CONTRADICTION.value:
            continue
        referenced = [unresolved_by_id[ref] for ref in tile["unresolved_refs"]]
        if not any(item["blocking"] and item["kind"] == "contradiction" for item in referenced):
            raise EvidenceVaultCanonicalCoreError(
                f"contradiction candidate must reference a blocking contradiction item: {tile['tile_key']}"
            )


def _derived_tile_state_fingerprint(
    tiles: list[dict[str, Any]],
) -> str:
    return canonical_fingerprint(
        EVIDENCE_VAULT_DERIVED_TILE_STATE_VERSION,
        {
            "rubric_version": RUBRIC_VERSION,
            "tiles": [
                {
                    "component_key": tile["component_key"],
                    "tile_id": tile["tile_id"],
                    "tile_key": tile["tile_key"],
                    "state": tile["candidate_state"],
                }
                for tile in tiles
            ],
        },
    )


def _require_exact_fields(
    value: dict[str, Any],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    fields = set(value)
    if fields == expected:
        return
    missing = sorted(expected - fields)
    extra = sorted(fields - expected)
    raise EvidenceVaultCanonicalCoreError(f"{label} fields do not match schema; missing={missing}; extra={extra}")


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise EvidenceVaultCanonicalCoreError(f"{field} must be text")
    text = value.strip()
    if not text:
        raise EvidenceVaultCanonicalCoreError(f"{field} is required")
    return text


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field=field)


def _required_sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise EvidenceVaultCanonicalCoreError(f"{field} must be a sha256 fingerprint")
    text = value.strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise EvidenceVaultCanonicalCoreError(f"{field} must be a sha256 fingerprint")
    return text


def _optional_sha256(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_sha256(value, field=field)


def _normalized_text_list(
    values: Iterable[Any],
    *,
    field: str,
) -> list[str]:
    if isinstance(values, (str, bytes, dict)):
        raise EvidenceVaultCanonicalCoreError(f"{field} must be an array")
    try:
        normalized = sorted({_required_text(value, field=field) for value in values})
    except TypeError as exc:
        raise EvidenceVaultCanonicalCoreError(f"{field} must be an array") from exc
    return normalized


__all__ = [
    "AuthorityState",
    "BasisReviewStatus",
    "EVIDENCE_VAULT_CANDIDATE_PACKET_VERSION",
    "EVIDENCE_VAULT_CANDIDATE_TILE_VERSION",
    "EVIDENCE_VAULT_REDUCER_POLICY_VERSION",
    "EvidenceDeltaKind",
    "EvidencePolarity",
    "EvidenceVaultCanonicalCoreError",
    "ProvenanceStatus",
    "TileState",
    "build_candidate_packet",
    "build_candidate_tile",
    "build_incremental_candidate_tiles",
    "build_reducer_policy",
    "build_tile_contract_registry",
    "canonical_fingerprint",
    "canonical_json",
    "reduce_tile_basis",
    "reducer_policy_fingerprint",
    "tile_contract_registry_fingerprint",
    "validate_candidate_packet",
    "validate_incremental_candidate_tiles",
]
