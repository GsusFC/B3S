"""Append-only Vault authority for operational-memory v2 packets.

The functions are pure and Vault-scoped.  They validate an immutable packet,
build an attributable adoption event and project the accepted subset.  Storage
and concurrency control remain repository responsibilities.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_operational_memory import (
    EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION,
    EVIDENCE_VAULT_CANDIDATE_OVERLAY_VERSION,
    EVIDENCE_VAULT_OPERATIONAL_PACKET_VERSION,
)


EVIDENCE_VAULT_OPERATIONAL_ADOPTION_EVENT_VERSION = (
    "evidence-vault-operational-adoption-event-v2"
)
EVIDENCE_VAULT_OPERATIONAL_ADOPTION_REQUEST_VERSION = (
    "evidence-vault-operational-adoption-request-v2"
)
EVIDENCE_VAULT_OPERATIONAL_MEMORY_PROJECTION_VERSION = (
    "evidence-vault-operational-memory-projection-v2"
)
_ADOPTION_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_type",
        "event_id",
        "sequence",
        "previous_event_id",
        "brand_identity",
        "candidate_packet_fingerprint",
        "parent_canonical_memory_version",
        "promoted_canonical_memory_version",
        "adopted_by",
        "actor_id",
        "policy_fingerprint",
        "decision",
        "created_at",
        "idempotency_key_hash",
        "request_fingerprint",
        "authority",
        "authority_scope",
        "production_runtime_effect",
        "scanner_runtime_effect",
    }
)


class EvidenceVaultOperationalAuthorityError(RuntimeError):
    """The packet or adoption request cannot receive Vault authority."""


class EvidenceVaultOperationalAdoptionConflictError(
    EvidenceVaultOperationalAuthorityError
):
    """The requested parent is no longer the current canonical memory."""


class EvidenceVaultOperationalAdoptionBlockedError(
    EvidenceVaultOperationalAuthorityError
):
    """The packet contains no accepted change that can create a new version."""


def adoption_request_fingerprint(
    *,
    candidate_packet_fingerprint: str,
    parent_canonical_memory_version: str | None,
    adopted_by: str,
    actor_id: str,
    policy_fingerprint: str,
) -> str:
    return canonical_fingerprint(
        EVIDENCE_VAULT_OPERATIONAL_ADOPTION_REQUEST_VERSION,
        {
            "candidate_packet_fingerprint": _sha256(
                candidate_packet_fingerprint,
                field="candidate_packet_fingerprint",
            ),
            "parent_canonical_memory_version": _optional_sha256(
                parent_canonical_memory_version,
                field="parent_canonical_memory_version",
            ),
            "adopted_by": _adopted_by(adopted_by),
            "actor_id": _text(actor_id, field="actor_id", maximum=200),
            "policy_fingerprint": _sha256(
                policy_fingerprint,
                field="policy_fingerprint",
            ),
        },
    )


def build_operational_adoption_event(
    packet: Mapping[str, Any],
    *,
    event_id: str,
    sequence: int,
    previous_event_id: str | None,
    adopted_by: str,
    actor_id: str,
    policy_fingerprint: str,
    created_at: str,
    idempotency_key_hash: str,
    expected_current_canonical_memory_version: str | None,
) -> dict[str, Any]:
    """Build one authoritative event without mutating the candidate packet."""

    validate_operational_memory_packet(packet)
    if packet.get("has_accepted_change") is not True:
        raise EvidenceVaultOperationalAdoptionBlockedError(
            "packet has no accepted change to adopt"
        )
    parent = _optional_sha256(
        packet.get("current_canonical_memory_version"),
        field="current_canonical_memory_version",
    )
    expected_parent = _optional_sha256(
        expected_current_canonical_memory_version,
        field="expected_current_canonical_memory_version",
    )
    if parent != expected_parent:
        raise EvidenceVaultOperationalAdoptionConflictError(
            "candidate parent is not the current canonical memory"
        )
    normalized_sequence = int(sequence)
    if normalized_sequence < 1:
        raise EvidenceVaultOperationalAuthorityError("sequence must be positive")
    previous = _optional_uuid(previous_event_id, field="previous_event_id")
    if normalized_sequence == 1:
        if previous is not None or parent is not None:
            raise EvidenceVaultOperationalAuthorityError(
                "first adoption cannot declare a previous event or parent"
            )
    elif previous is None or parent is None:
        raise EvidenceVaultOperationalAuthorityError(
            "incremental adoption requires previous event and parent"
        )
    actor_kind = _adopted_by(adopted_by)
    actor = _text(actor_id, field="actor_id", maximum=200)
    policy = _sha256(policy_fingerprint, field="policy_fingerprint")
    request_fingerprint = adoption_request_fingerprint(
        candidate_packet_fingerprint=str(packet["candidate_packet_fingerprint"]),
        parent_canonical_memory_version=parent,
        adopted_by=actor_kind,
        actor_id=actor,
        policy_fingerprint=policy,
    )
    return {
        "schema_version": EVIDENCE_VAULT_OPERATIONAL_ADOPTION_EVENT_VERSION,
        "event_type": "canonical_memory_promotion",
        "event_id": _uuid(event_id, field="event_id"),
        "sequence": normalized_sequence,
        "previous_event_id": previous,
        "brand_identity": str(packet["brand_identity"]),
        "candidate_packet_fingerprint": str(
            packet["candidate_packet_fingerprint"]
        ),
        "parent_canonical_memory_version": parent,
        "promoted_canonical_memory_version": str(
            packet["proposed_canonical_memory_version"]
        ),
        "adopted_by": actor_kind,
        "actor_id": actor,
        "policy_fingerprint": policy,
        "decision": "adopt",
        "created_at": _timestamp(created_at),
        "idempotency_key_hash": _sha256(
            idempotency_key_hash,
            field="idempotency_key_hash",
        ),
        "request_fingerprint": request_fingerprint,
        "authority": True,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


def project_adopted_operational_memory(
    packet: Mapping[str, Any],
    event: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one active canonical memory without editing older versions."""

    validate_operational_memory_packet(packet)
    validate_operational_adoption_event(event)
    if event["candidate_packet_fingerprint"] != packet["candidate_packet_fingerprint"]:
        raise EvidenceVaultOperationalAuthorityError(
            "adoption event does not reference the packet"
        )
    if event["brand_identity"] != packet["brand_identity"]:
        raise EvidenceVaultOperationalAuthorityError(
            "adoption event belongs to another brand"
        )
    if event["parent_canonical_memory_version"] != packet[
        "current_canonical_memory_version"
    ]:
        raise EvidenceVaultOperationalAdoptionConflictError(
            "adoption event parent does not match the packet"
        )
    if event["promoted_canonical_memory_version"] != packet[
        "proposed_canonical_memory_version"
    ]:
        raise EvidenceVaultOperationalAuthorityError(
            "adoption event does not activate the proposed memory"
        )
    return {
        "schema_version": EVIDENCE_VAULT_OPERATIONAL_MEMORY_PROJECTION_VERSION,
        "brand_identity": packet["brand_identity"],
        "canonical_memory_version": packet["proposed_canonical_memory_version"],
        "parent_canonical_memory_version": packet[
            "current_canonical_memory_version"
        ],
        "adoption_event_id": event["event_id"],
        "adoption_sequence": event["sequence"],
        "lifecycle_state": "active",
        "authority": True,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "content": packet["accepted_memory"],
        "scoring_projection": packet["scoring_projection"],
    }


def project_memory_lifecycle(
    memories: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate one brand's contiguous adoption chain and derive lifecycle."""

    if not isinstance(memories, list):
        raise EvidenceVaultOperationalAuthorityError("memories must be an array")
    ordered = sorted(
        memories,
        key=lambda row: (
            row.get("adoption_sequence")
            if isinstance(row.get("adoption_sequence"), int)
            and not isinstance(row.get("adoption_sequence"), bool)
            else 0
        ),
    )
    if not ordered:
        return []
    brand_identity: str | None = None
    previous_version: str | None = None
    seen_versions: set[str] = set()
    seen_events: set[str] = set()
    projected: list[dict[str, Any]] = []
    for expected_sequence, memory in enumerate(ordered, start=1):
        if not isinstance(memory, Mapping):
            raise EvidenceVaultOperationalAuthorityError("memory must be an object")
        sequence = memory.get("adoption_sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence != expected_sequence
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "operational memory sequence is not contiguous"
            )
        current_brand = _text(
            memory.get("brand_identity"),
            field="brand_identity",
            maximum=300,
        )
        if brand_identity is None:
            brand_identity = current_brand
        elif current_brand != brand_identity:
            raise EvidenceVaultOperationalAuthorityError(
                "operational lifecycle mixes multiple brands"
            )
        version = _sha256(
            memory.get("canonical_memory_version"),
            field="canonical_memory_version",
        )
        parent = _optional_sha256(
            memory.get("parent_canonical_memory_version"),
            field="parent_canonical_memory_version",
        )
        if parent != previous_version:
            raise EvidenceVaultOperationalAuthorityError(
                "operational lifecycle parent chain is invalid"
            )
        event_id = _uuid(memory.get("adoption_event_id"), field="adoption_event_id")
        if version in seen_versions or event_id in seen_events:
            raise EvidenceVaultOperationalAuthorityError(
                "operational lifecycle repeats a memory or event"
            )
        content = memory.get("content")
        if not isinstance(content, Mapping):
            raise EvidenceVaultOperationalAuthorityError(
                "operational memory content is missing"
            )
        expected_version = canonical_fingerprint(
            EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION,
            content,
        )
        if version != expected_version:
            raise EvidenceVaultOperationalAuthorityError(
                "operational memory content fingerprint mismatch"
            )
        seen_versions.add(version)
        seen_events.add(event_id)
        previous_version = version
        projected.append(dict(memory))
    for index, memory in enumerate(projected):
        memory["lifecycle_state"] = (
            "active" if index == len(projected) - 1 else "superseded"
        )
    return projected


def validate_operational_memory_packet(packet: Mapping[str, Any]) -> None:
    if not isinstance(packet, Mapping):
        raise EvidenceVaultOperationalAuthorityError("packet must be an object")
    if packet.get("schema_version") != EVIDENCE_VAULT_OPERATIONAL_PACKET_VERSION:
        raise EvidenceVaultOperationalAuthorityError("unsupported packet schema")
    if packet.get("authority") is not False or packet.get("runtime_effect") is not False:
        raise EvidenceVaultOperationalAuthorityError(
            "candidate packet cannot carry authority or runtime effect"
        )
    unsigned = {
        key: value
        for key, value in packet.items()
        if key != "candidate_packet_fingerprint"
    }
    expected_packet = canonical_fingerprint(
        EVIDENCE_VAULT_OPERATIONAL_PACKET_VERSION,
        unsigned,
    )
    if packet.get("candidate_packet_fingerprint") != expected_packet:
        raise EvidenceVaultOperationalAuthorityError("candidate packet fingerprint mismatch")
    accepted = packet.get("accepted_memory")
    overlay = packet.get("candidate_overlay")
    if not isinstance(accepted, Mapping) or not isinstance(overlay, Mapping):
        raise EvidenceVaultOperationalAuthorityError(
            "packet requires accepted memory and candidate overlay"
        )
    if accepted.get("schema_version") != EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION:
        raise EvidenceVaultOperationalAuthorityError("accepted memory schema mismatch")
    accepted_version = canonical_fingerprint(
        EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION,
        accepted,
    )
    if packet.get("accepted_memory_candidate_version") != accepted_version:
        raise EvidenceVaultOperationalAuthorityError(
            "accepted memory candidate fingerprint mismatch"
        )
    if overlay.get("schema_version") != EVIDENCE_VAULT_CANDIDATE_OVERLAY_VERSION:
        raise EvidenceVaultOperationalAuthorityError("candidate overlay schema mismatch")
    overlay_version = canonical_fingerprint(
        EVIDENCE_VAULT_CANDIDATE_OVERLAY_VERSION,
        overlay,
    )
    if packet.get("candidate_overlay_version") != overlay_version:
        raise EvidenceVaultOperationalAuthorityError(
            "candidate overlay fingerprint mismatch"
        )
    proposed = _optional_sha256(
        packet.get("proposed_canonical_memory_version"),
        field="proposed_canonical_memory_version",
    )
    parent = _optional_sha256(
        packet.get("current_canonical_memory_version"),
        field="current_canonical_memory_version",
    )
    if packet.get("has_accepted_change") is True:
        if proposed != accepted_version:
            raise EvidenceVaultOperationalAuthorityError(
                "changed accepted memory must propose its content fingerprint"
            )
    elif proposed != parent:
        raise EvidenceVaultOperationalAuthorityError(
            "unchanged accepted memory must preserve the current version"
        )
    projection = packet.get("scoring_projection")
    tiles = projection.get("tiles") if isinstance(projection, Mapping) else None
    coverage = projection.get("coverage") if isinstance(projection, Mapping) else None
    if not isinstance(tiles, list) or len(tiles) != 80 or not isinstance(coverage, Mapping):
        raise EvidenceVaultOperationalAuthorityError(
            "scoring projection must contain 80 tiles and coverage"
        )
    accepted_count = int(coverage.get("accepted_tile_count") or 0)
    unresolved_count = int(coverage.get("unresolved_tile_count") or 0)
    if accepted_count + unresolved_count != 80:
        raise EvidenceVaultOperationalAuthorityError(
            "authority coverage cardinality mismatch"
        )
    contradiction_total = int(coverage.get("contradiction_count") or 0)
    contradiction_parts = int(
        coverage.get("contradiction_on_accepted_count") or 0
    ) + int(coverage.get("contradiction_on_unresolved_count") or 0)
    if contradiction_total != contradiction_parts:
        raise EvidenceVaultOperationalAuthorityError(
            "contradiction cardinality mismatch"
        )


def validate_operational_adoption_event(event: Mapping[str, Any]) -> None:
    if not isinstance(event, Mapping):
        raise EvidenceVaultOperationalAuthorityError("event must be an object")
    if set(event) != _ADOPTION_EVENT_FIELDS:
        raise EvidenceVaultOperationalAuthorityError("adoption event fields mismatch")
    if event.get("schema_version") != EVIDENCE_VAULT_OPERATIONAL_ADOPTION_EVENT_VERSION:
        raise EvidenceVaultOperationalAuthorityError("unsupported adoption event schema")
    if event.get("event_type") != "canonical_memory_promotion":
        raise EvidenceVaultOperationalAuthorityError("invalid event type")
    if event.get("decision") != "adopt":
        raise EvidenceVaultOperationalAuthorityError("invalid adoption decision")
    if event.get("authority") is not True or event.get("authority_scope") != "b3s-vault":
        raise EvidenceVaultOperationalAuthorityError("invalid adoption authority")
    if event.get("production_runtime_effect") is not False:
        raise EvidenceVaultOperationalAuthorityError("production effect is forbidden")
    if event.get("scanner_runtime_effect") is not False:
        raise EvidenceVaultOperationalAuthorityError("scanner effect is forbidden")
    _uuid(event.get("event_id"), field="event_id")
    sequence = event.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise EvidenceVaultOperationalAuthorityError("sequence must be positive")
    previous = _optional_uuid(event.get("previous_event_id"), field="previous_event_id")
    parent = _optional_sha256(
        event.get("parent_canonical_memory_version"),
        field="parent_canonical_memory_version",
    )
    if sequence == 1:
        if previous is not None or parent is not None:
            raise EvidenceVaultOperationalAuthorityError(
                "first adoption cannot declare previous event or parent"
            )
    elif previous is None or parent is None:
        raise EvidenceVaultOperationalAuthorityError(
            "incremental adoption requires previous event and parent"
        )
    _text(event.get("brand_identity"), field="brand_identity", maximum=300)
    _sha256(event.get("candidate_packet_fingerprint"), field="candidate_packet_fingerprint")
    _sha256(
        event.get("promoted_canonical_memory_version"),
        field="promoted_canonical_memory_version",
    )
    _adopted_by(event.get("adopted_by"))
    _text(event.get("actor_id"), field="actor_id", maximum=200)
    _sha256(event.get("policy_fingerprint"), field="policy_fingerprint")
    _sha256(event.get("idempotency_key_hash"), field="idempotency_key_hash")
    expected_request = adoption_request_fingerprint(
        candidate_packet_fingerprint=str(event["candidate_packet_fingerprint"]),
        parent_canonical_memory_version=parent,
        adopted_by=str(event["adopted_by"]),
        actor_id=str(event["actor_id"]),
        policy_fingerprint=str(event["policy_fingerprint"]),
    )
    if event.get("request_fingerprint") != expected_request:
        raise EvidenceVaultOperationalAuthorityError("adoption request fingerprint mismatch")
    _timestamp(event.get("created_at"))


def _adopted_by(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"policy", "human"}:
        raise EvidenceVaultOperationalAuthorityError(
            "adopted_by must be policy or human"
        )
    return normalized


def _timestamp(value: Any) -> str:
    normalized = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceVaultOperationalAuthorityError("invalid created_at") from exc
    if parsed.tzinfo is None:
        raise EvidenceVaultOperationalAuthorityError("created_at requires timezone")
    return parsed.isoformat()


def _uuid(value: Any, *, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceVaultOperationalAuthorityError(f"{field} must be a UUID") from exc


def _optional_uuid(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _uuid(value, field=field)


def _sha256(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise EvidenceVaultOperationalAuthorityError(f"{field} must be a sha256")
    return normalized


def _optional_sha256(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, field=field)


def _text(value: Any, *, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise EvidenceVaultOperationalAuthorityError(f"invalid {field}")
    return normalized


__all__ = [
    "EVIDENCE_VAULT_OPERATIONAL_ADOPTION_EVENT_VERSION",
    "EVIDENCE_VAULT_OPERATIONAL_ADOPTION_REQUEST_VERSION",
    "EVIDENCE_VAULT_OPERATIONAL_MEMORY_PROJECTION_VERSION",
    "EvidenceVaultOperationalAdoptionBlockedError",
    "EvidenceVaultOperationalAdoptionConflictError",
    "EvidenceVaultOperationalAuthorityError",
    "adoption_request_fingerprint",
    "build_operational_adoption_event",
    "project_adopted_operational_memory",
    "project_memory_lifecycle",
    "validate_operational_adoption_event",
    "validate_operational_memory_packet",
]
