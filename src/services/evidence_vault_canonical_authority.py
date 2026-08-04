"""Vault-only authority boundary for promoted canonical evidence memory.

This module is deliberately pure. It validates one immutable candidate packet
against its resolved references and canonical parent, derives the exact next
memory content, and builds the separate append-only promotion event. Database
serialization lives in the history repository; scanner and production runtime
do not import this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from src.services.evidence_vault_canonical_core import (
    EvidenceVaultCanonicalCoreError,
    build_candidate_tile,
    canonical_fingerprint,
    validate_candidate_packet,
    validate_incremental_candidate_tiles,
)


EVIDENCE_VAULT_PROMOTION_EVENT_VERSION = "evidence-vault-canonical-memory-promotion-event-v1"
EVIDENCE_VAULT_PROMOTION_POLICY_VERSION = "evidence-vault-canonical-memory-promotion-policy-v1"
EVIDENCE_VAULT_REFERENCE_RESOLUTION_VERSION = "evidence-vault-candidate-reference-resolution-v1"
EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION = "evidence-vault-canonical-memory-content-v1"
EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION = "evidence-vault-canonical-memory-projection-v1"
EVIDENCE_VAULT_PROMOTION_REQUEST_VERSION = "evidence-vault-canonical-memory-promotion-request-v1"

AUTHORITY_SCOPE = "b3s-vault"
PROMOTABLE_DELTA_KINDS = frozenset({"strengthened", "candidate_update", "verified_deprecation"})
NON_PROMOTABLE_DELTA_KINDS = frozenset({"no_change", "coverage_loss"})

_REFERENCE_FIELDS = (
    "candidate_memory_version",
    "accepted_memory_candidate_version",
    "reviewed_memory_candidate_version",
    "review_packet_set_fingerprint",
    "rubric_version",
    "tile_contract_registry_fingerprint",
    "reducer_policy_fingerprint",
    "aggregation_policy_fingerprint",
)
_STABLE_INCREMENTAL_POLICY_FIELDS = (
    "rubric_version",
    "tile_contract_registry_fingerprint",
    "reducer_policy_fingerprint",
    "aggregation_policy_fingerprint",
)
_PROMOTION_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_type",
        "event_id",
        "sequence",
        "previous_event_id",
        "brand_identity",
        "candidate_packet_fingerprint",
        "reference_resolution_fingerprint",
        "promotion_policy_fingerprint",
        "parent_canonical_memory_version",
        "promoted_canonical_memory_version",
        "decision",
        "reviewer_id",
        "reviewed_at",
        "rationale",
        "idempotency_key_hash",
        "request_fingerprint",
        "authority",
        "authority_scope",
        "production_runtime_effect",
        "scanner_runtime_effect",
    }
)


class EvidenceVaultCanonicalAuthorityError(RuntimeError):
    """Base error for canonical-memory promotion."""


class EvidenceVaultCanonicalPromotionBlockedError(EvidenceVaultCanonicalAuthorityError):
    """The exact candidate is valid shadow data but cannot be promoted."""


class EvidenceVaultCanonicalUnavailableError(EvidenceVaultCanonicalAuthorityError):
    """The durable canonical-memory boundary cannot be used safely."""


class EvidenceVaultCanonicalPacketNotFoundError(EvidenceVaultCanonicalAuthorityError):
    """The exact registered candidate packet does not exist."""


class EvidenceVaultCanonicalPromotionConflictError(EvidenceVaultCanonicalAuthorityError):
    """The requested parent, event chain, or idempotent request conflicts."""

    def __init__(
        self,
        message: str,
        *,
        current_canonical_memory_version: str | None = None,
        existing_event_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.current_canonical_memory_version = current_canonical_memory_version
        self.existing_event_id = existing_event_id


class EvidenceVaultCanonicalReferenceError(EvidenceVaultCanonicalAuthorityError):
    """A packet dependency was not resolved to its exact content identity."""


@dataclass(frozen=True, slots=True)
class CanonicalMemoryPromotionCommand:
    """Human promotion command passed to the durable append-only journal."""

    candidate_packet_fingerprint: str
    parent_canonical_memory_version: str | None
    reviewer_id: str
    reviewed_at: str
    rationale: str
    idempotency_key_hash: str
    request_fingerprint: str


def build_promotion_policy() -> dict[str, Any]:
    """Return the exact immutable rules that grant Vault-only authority."""

    return {
        "schema_version": EVIDENCE_VAULT_PROMOTION_POLICY_VERSION,
        "authority_scope": AUTHORITY_SCOPE,
        "reviewer_count": 1,
        "packet_authority_state": "pending_review",
        "blocking_conditions": [
            "candidate_packet_missing_or_invalid",
            "unresolved_reference",
            "blocking_unresolved_item",
            "contradiction",
            "canonical_parent_mismatch",
            "incremental_policy_change",
            "no_canonical_change",
        ],
        "promotable_delta_kinds": sorted(PROMOTABLE_DELTA_KINDS),
        "non_promotable_delta_kinds": sorted(NON_PROMOTABLE_DELTA_KINDS),
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


def promotion_policy_fingerprint() -> str:
    return canonical_fingerprint(EVIDENCE_VAULT_PROMOTION_POLICY_VERSION, build_promotion_policy())


def build_reference_resolution(
    packet: dict[str, Any],
    resolved_references: Mapping[str, str],
) -> dict[str, Any]:
    """Bind independently resolved artifacts to the packet's exact manifest."""

    _validate_packet(packet)
    if not isinstance(resolved_references, Mapping):
        raise EvidenceVaultCanonicalReferenceError("resolved references must be an object")
    keys = set(resolved_references)
    expected_keys = set(_REFERENCE_FIELDS)
    if keys != expected_keys:
        missing = sorted(expected_keys - keys)
        extra = sorted(keys - expected_keys)
        raise EvidenceVaultCanonicalReferenceError(
            f"resolved reference coverage mismatch; missing={missing}; extra={extra}"
        )
    manifest = packet["manifest"]
    references: dict[str, str] = {}
    for field in _REFERENCE_FIELDS:
        value = _required_text(resolved_references.get(field), field=field)
        if value != manifest[field]:
            raise EvidenceVaultCanonicalReferenceError(f"resolved reference mismatch: {field}")
        references[field] = value
    payload = {
        "schema_version": EVIDENCE_VAULT_REFERENCE_RESOLUTION_VERSION,
        "brand_identity": manifest["brand_identity"],
        "candidate_packet_fingerprint": packet["candidate_packet_fingerprint"],
        "references": references,
    }
    return {
        **payload,
        "reference_resolution_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_REFERENCE_RESOLUTION_VERSION,
            payload,
        ),
    }


def plan_canonical_memory_promotion(
    packet: dict[str, Any],
    *,
    resolved_references: Mapping[str, str],
    current_memory: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate and derive the exact semantic memory a promotion would adopt."""

    _validate_packet(packet)
    manifest = packet["manifest"]
    resolution = build_reference_resolution(packet, resolved_references)
    _validate_blockers(packet)
    _validate_current_memory(current_memory)

    parent_version = manifest["parent_canonical_memory_version"]
    if current_memory is None:
        if parent_version is not None:
            raise EvidenceVaultCanonicalPromotionConflictError("the first canonical memory cannot declare a parent")
        canonical_tiles = [
            _canonical_tile(tile, packet["candidate_packet_fingerprint"]) for tile in packet["candidate_tiles"]
        ]
    else:
        current_version = current_memory["canonical_memory_version"]
        if parent_version != current_version:
            raise EvidenceVaultCanonicalPromotionConflictError(
                "the candidate parent is not the current canonical memory"
            )
        if manifest["brand_identity"] != current_memory["content"]["brand_identity"]:
            raise EvidenceVaultCanonicalPromotionConflictError(
                "the candidate brand does not match the canonical parent"
            )
        _validate_incremental_policy(manifest, current_memory["content"])
        previous_candidate_tiles = [
            build_candidate_tile(
                tile_id=tile["tile_id"],
                basis=tile["basis"],
                coverage_refs=tile["coverage_refs"],
                unresolved_refs=tile["unresolved_refs"],
            )
            for tile in current_memory["content"]["tiles"]
        ]
        try:
            validate_incremental_candidate_tiles(
                previous_candidate_tiles=previous_candidate_tiles,
                candidate_tiles=packet["candidate_tiles"],
            )
        except EvidenceVaultCanonicalCoreError as exc:
            raise EvidenceVaultCanonicalPromotionConflictError(
                "the candidate does not evolve the resolved canonical parent"
            ) from exc
        if not any(tile["delta_kind"] in PROMOTABLE_DELTA_KINDS for tile in packet["candidate_tiles"]):
            raise EvidenceVaultCanonicalPromotionBlockedError("no_change and coverage_loss remain shadow-only")
        previous_by_id = {tile["tile_id"]: tile for tile in current_memory["content"]["tiles"]}
        canonical_tiles = []
        for tile in packet["candidate_tiles"]:
            if tile["delta_kind"] in NON_PROMOTABLE_DELTA_KINDS:
                canonical_tiles.append(dict(previous_by_id[tile["tile_id"]]))
            else:
                canonical_tiles.append(_canonical_tile(tile, packet["candidate_packet_fingerprint"]))

    content = {
        "schema_version": EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION,
        "brand_identity": manifest["brand_identity"],
        "parent_canonical_memory_version": parent_version,
        "candidate_memory_version": manifest["candidate_memory_version"],
        "accepted_memory_candidate_version": manifest["accepted_memory_candidate_version"],
        "reviewed_memory_candidate_version": manifest["reviewed_memory_candidate_version"],
        "review_packet_set_fingerprint": manifest["review_packet_set_fingerprint"],
        "rubric_version": manifest["rubric_version"],
        "tile_contract_registry_fingerprint": manifest["tile_contract_registry_fingerprint"],
        "reducer_policy_fingerprint": manifest["reducer_policy_fingerprint"],
        "aggregation_policy_fingerprint": manifest["aggregation_policy_fingerprint"],
        "tile_count": len(canonical_tiles),
        "tiles": canonical_tiles,
    }
    promoted_version = canonical_fingerprint(
        EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION,
        content,
    )
    return {
        "schema_version": EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION,
        "candidate_packet_fingerprint": packet["candidate_packet_fingerprint"],
        "reference_resolution_fingerprint": resolution["reference_resolution_fingerprint"],
        "promotion_policy_fingerprint": promotion_policy_fingerprint(),
        "promoted_canonical_memory_version": promoted_version,
        "content": content,
    }


def promotion_request_fingerprint(
    *,
    candidate_packet_fingerprint: str,
    parent_canonical_memory_version: str | None,
    reviewer_id: str,
    reviewed_at: str,
    rationale: str,
) -> str:
    payload = {
        "candidate_packet_fingerprint": _required_sha256(
            candidate_packet_fingerprint,
            field="candidate_packet_fingerprint",
        ),
        "parent_canonical_memory_version": _optional_sha256(
            parent_canonical_memory_version,
            field="parent_canonical_memory_version",
        ),
        "reviewer_id": _required_text(reviewer_id, field="reviewer_id"),
        "reviewed_at": _reviewed_at(reviewed_at),
        "rationale": _required_text(rationale, field="rationale", maximum=4000),
    }
    return canonical_fingerprint(EVIDENCE_VAULT_PROMOTION_REQUEST_VERSION, payload)


def build_promotion_event(
    plan: dict[str, Any],
    command: CanonicalMemoryPromotionCommand,
    *,
    event_id: str,
    sequence: int,
    previous_event_id: str | None,
) -> dict[str, Any]:
    """Build one immutable event after the repository wins concurrency."""

    _validate_plan(plan)
    normalized_event_id = str(UUID(_required_text(event_id, field="event_id")))
    normalized_previous_event_id = (
        str(UUID(_required_text(previous_event_id, field="previous_event_id")))
        if previous_event_id is not None
        else None
    )
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise EvidenceVaultCanonicalAuthorityError("sequence must be a positive integer")
    if (sequence == 1) != (normalized_previous_event_id is None):
        raise EvidenceVaultCanonicalAuthorityError("promotion event sequence and previous_event_id are inconsistent")
    command_packet_fingerprint = _required_sha256(
        command.candidate_packet_fingerprint,
        field="candidate_packet_fingerprint",
    )
    command_parent = _optional_sha256(
        command.parent_canonical_memory_version,
        field="parent_canonical_memory_version",
    )
    expected_request = promotion_request_fingerprint(
        candidate_packet_fingerprint=command_packet_fingerprint,
        parent_canonical_memory_version=command_parent,
        reviewer_id=command.reviewer_id,
        reviewed_at=command.reviewed_at,
        rationale=command.rationale,
    )
    if _required_sha256(command.request_fingerprint, field="request_fingerprint") != expected_request:
        raise EvidenceVaultCanonicalAuthorityError("promotion request fingerprint mismatch")
    if command_packet_fingerprint != plan["candidate_packet_fingerprint"]:
        raise EvidenceVaultCanonicalPromotionConflictError("promotion command packet mismatch")
    if command_parent != plan["content"]["parent_canonical_memory_version"]:
        raise EvidenceVaultCanonicalPromotionConflictError("promotion command parent mismatch")
    return {
        "schema_version": EVIDENCE_VAULT_PROMOTION_EVENT_VERSION,
        "event_type": "canonical_memory_promotion",
        "event_id": normalized_event_id,
        "sequence": sequence,
        "previous_event_id": normalized_previous_event_id,
        "brand_identity": plan["content"]["brand_identity"],
        "candidate_packet_fingerprint": plan["candidate_packet_fingerprint"],
        "reference_resolution_fingerprint": plan["reference_resolution_fingerprint"],
        "promotion_policy_fingerprint": plan["promotion_policy_fingerprint"],
        "parent_canonical_memory_version": plan["content"]["parent_canonical_memory_version"],
        "promoted_canonical_memory_version": plan["promoted_canonical_memory_version"],
        "decision": "promote",
        "reviewer_id": _required_text(command.reviewer_id, field="reviewer_id"),
        "reviewed_at": _reviewed_at(command.reviewed_at),
        "rationale": _required_text(command.rationale, field="rationale", maximum=4000),
        "idempotency_key_hash": _required_sha256(
            command.idempotency_key_hash,
            field="idempotency_key_hash",
        ),
        "request_fingerprint": expected_request,
        "authority": True,
        "authority_scope": AUTHORITY_SCOPE,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


def project_promoted_canonical_memory(
    plan: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    """Attach authority derived from an exact promotion event to its content."""

    _validate_plan(plan)
    validate_promotion_event(event)
    expected_pairs = {
        "candidate_packet_fingerprint": plan["candidate_packet_fingerprint"],
        "reference_resolution_fingerprint": plan["reference_resolution_fingerprint"],
        "promotion_policy_fingerprint": plan["promotion_policy_fingerprint"],
        "promoted_canonical_memory_version": plan["promoted_canonical_memory_version"],
        "parent_canonical_memory_version": plan["content"]["parent_canonical_memory_version"],
        "brand_identity": plan["content"]["brand_identity"],
    }
    if any(event.get(field) != value for field, value in expected_pairs.items()):
        raise EvidenceVaultCanonicalPromotionConflictError(
            "promotion event does not match the derived canonical memory"
        )
    return {
        "schema_version": EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION,
        "canonical_memory_version": plan["promoted_canonical_memory_version"],
        "promotion_event_id": event["event_id"],
        "promotion_sequence": event["sequence"],
        "authority": True,
        "authority_scope": AUTHORITY_SCOPE,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "content": plan["content"],
    }


def validate_promotion_event(event: dict[str, Any]) -> None:
    """Revalidate a stored event before deriving any canonical authority."""

    if not isinstance(event, dict) or set(event) != _PROMOTION_EVENT_FIELDS:
        raise EvidenceVaultCanonicalAuthorityError("promotion event schema fields mismatch")
    if event["schema_version"] != EVIDENCE_VAULT_PROMOTION_EVENT_VERSION:
        raise EvidenceVaultCanonicalAuthorityError("unsupported promotion event schema")
    if (
        event["event_type"] != "canonical_memory_promotion"
        or event["decision"] != "promote"
        or event["authority"] is not True
        or event["authority_scope"] != AUTHORITY_SCOPE
        or event["production_runtime_effect"] is not False
        or event["scanner_runtime_effect"] is not False
    ):
        raise EvidenceVaultCanonicalAuthorityError("promotion event has invalid authority scope")
    str(UUID(_required_text(event["event_id"], field="event_id")))
    sequence = event["sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise EvidenceVaultCanonicalAuthorityError("sequence must be a positive integer")
    previous_event_id = event["previous_event_id"]
    if previous_event_id is not None:
        str(UUID(_required_text(previous_event_id, field="previous_event_id")))
    if (sequence == 1) != (previous_event_id is None):
        raise EvidenceVaultCanonicalAuthorityError("promotion event sequence and previous_event_id are inconsistent")
    _required_text(event["brand_identity"], field="brand_identity")
    candidate_packet_fingerprint = _required_sha256(
        event["candidate_packet_fingerprint"],
        field="candidate_packet_fingerprint",
    )
    _required_sha256(
        event["reference_resolution_fingerprint"],
        field="reference_resolution_fingerprint",
    )
    if (
        _required_sha256(
            event["promotion_policy_fingerprint"],
            field="promotion_policy_fingerprint",
        )
        != promotion_policy_fingerprint()
    ):
        raise EvidenceVaultCanonicalAuthorityError("promotion policy fingerprint mismatch")
    parent = _optional_sha256(
        event["parent_canonical_memory_version"],
        field="parent_canonical_memory_version",
    )
    _required_sha256(
        event["promoted_canonical_memory_version"],
        field="promoted_canonical_memory_version",
    )
    reviewer_id = _required_text(event["reviewer_id"], field="reviewer_id")
    reviewed_at = _reviewed_at(event["reviewed_at"])
    rationale = _required_text(event["rationale"], field="rationale", maximum=4000)
    _required_sha256(event["idempotency_key_hash"], field="idempotency_key_hash")
    expected_request = promotion_request_fingerprint(
        candidate_packet_fingerprint=candidate_packet_fingerprint,
        parent_canonical_memory_version=parent,
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        rationale=rationale,
    )
    if _required_sha256(event["request_fingerprint"], field="request_fingerprint") != expected_request:
        raise EvidenceVaultCanonicalAuthorityError("promotion request fingerprint mismatch")


def _canonical_tile(tile: dict[str, Any], packet_fingerprint: str) -> dict[str, Any]:
    return {
        "component_key": tile["component_key"],
        "tile_id": tile["tile_id"],
        "tile_key": tile["tile_key"],
        "state": tile["candidate_state"],
        "basis": tile["basis"],
        "provenance_status": tile["provenance_status"],
        "single_source_dependency": tile["single_source_dependency"],
        "coverage_refs": tile["coverage_refs"],
        "unresolved_refs": tile["unresolved_refs"],
        "source_candidate_packet_fingerprint": packet_fingerprint,
        "source_delta_kind": tile["delta_kind"],
    }


def _validate_packet(packet: dict[str, Any]) -> None:
    try:
        validate_candidate_packet(packet)
    except EvidenceVaultCanonicalCoreError as exc:
        raise EvidenceVaultCanonicalPromotionBlockedError("candidate packet failed canonical validation") from exc


def _validate_blockers(packet: dict[str, Any]) -> None:
    if any(tile["candidate_state"] == "contradiction" for tile in packet["candidate_tiles"]):
        raise EvidenceVaultCanonicalPromotionBlockedError("contradiction blocks promotion")
    if any(item["blocking"] is True for item in packet["manifest"]["unresolved_items"]):
        raise EvidenceVaultCanonicalPromotionBlockedError("blocking unresolved item prevents promotion")


def _validate_incremental_policy(manifest: dict[str, Any], content: dict[str, Any]) -> None:
    for field in _STABLE_INCREMENTAL_POLICY_FIELDS:
        if manifest[field] != content[field]:
            raise EvidenceVaultCanonicalPromotionBlockedError(f"incremental packet changes canonical policy: {field}")


def _validate_current_memory(current_memory: dict[str, Any] | None) -> None:
    if current_memory is None:
        return
    if not isinstance(current_memory, dict):
        raise EvidenceVaultCanonicalPromotionConflictError("current canonical memory must be an object")
    if (
        current_memory.get("schema_version") != EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION
        or current_memory.get("authority") is not True
        or current_memory.get("authority_scope") != AUTHORITY_SCOPE
        or current_memory.get("production_runtime_effect") is not False
        or current_memory.get("scanner_runtime_effect") is not False
        or not isinstance(current_memory.get("content"), dict)
    ):
        raise EvidenceVaultCanonicalPromotionConflictError("current canonical memory is not authoritative")
    expected_version = canonical_fingerprint(
        EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION,
        current_memory["content"],
    )
    if current_memory.get("canonical_memory_version") != expected_version:
        raise EvidenceVaultCanonicalPromotionConflictError("current canonical memory fingerprint mismatch")


def _validate_plan(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict) or plan.get("schema_version") != EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION:
        raise EvidenceVaultCanonicalAuthorityError("invalid canonical memory promotion plan")
    content = plan.get("content")
    if (
        not isinstance(content, dict)
        or content.get("schema_version") != EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION
    ):
        raise EvidenceVaultCanonicalAuthorityError("invalid canonical memory content")
    expected = canonical_fingerprint(EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION, content)
    if plan.get("promoted_canonical_memory_version") != expected:
        raise EvidenceVaultCanonicalAuthorityError("canonical memory fingerprint mismatch")
    if plan.get("promotion_policy_fingerprint") != promotion_policy_fingerprint():
        raise EvidenceVaultCanonicalAuthorityError("promotion policy fingerprint mismatch")


def _reviewed_at(value: Any) -> str:
    text = _required_text(value, field="reviewed_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceVaultCanonicalAuthorityError("reviewed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceVaultCanonicalAuthorityError("reviewed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _required_sha256(value: Any, *, field: str) -> str:
    text = _required_text(value, field=field).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise EvidenceVaultCanonicalAuthorityError(f"{field} must be a sha256 fingerprint")
    return text


def _optional_sha256(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_sha256(value, field=field)


def _required_text(value: Any, *, field: str, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise EvidenceVaultCanonicalAuthorityError(f"{field} must be text")
    text = value.strip()
    if not text or len(text) > maximum:
        raise EvidenceVaultCanonicalAuthorityError(f"{field} is invalid")
    return text


__all__ = [
    "AUTHORITY_SCOPE",
    "CanonicalMemoryPromotionCommand",
    "EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION",
    "EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION",
    "EVIDENCE_VAULT_PROMOTION_EVENT_VERSION",
    "EVIDENCE_VAULT_PROMOTION_POLICY_VERSION",
    "EvidenceVaultCanonicalAuthorityError",
    "EvidenceVaultCanonicalPromotionBlockedError",
    "EvidenceVaultCanonicalPromotionConflictError",
    "EvidenceVaultCanonicalPacketNotFoundError",
    "EvidenceVaultCanonicalReferenceError",
    "EvidenceVaultCanonicalUnavailableError",
    "build_promotion_event",
    "build_promotion_policy",
    "build_reference_resolution",
    "plan_canonical_memory_promotion",
    "project_promoted_canonical_memory",
    "promotion_policy_fingerprint",
    "promotion_request_fingerprint",
    "validate_promotion_event",
]
