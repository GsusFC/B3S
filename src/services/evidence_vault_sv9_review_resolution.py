"""Pure SV9 human-review resolution value contract.

This module only builds and validates the row-shaped, append-only resolution
record. It does not insert rows, create authority events, mutate accepted
authority, publish a score/report, or wire a runtime endpoint. Migration 038
is the database authority for candidate provenance, the concrete reopen CAS,
and the event-first, same-transaction authority-plus-resolution boundary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from src.sv9 import judgment_memory as memory
from src.services import evidence_vault_sv9_authority_event as authority_event


RESOLUTION_SCHEMA_VERSION = "evidence-vault-sv9-judgment-review-resolution-v1"
RESOLUTION_KEY_NAMESPACE = "evidence-vault-sv9-judgment-review-resolution-key-v1"
REOPEN_REVIEW_OVERLAY_NAMESPACE = "evidence-vault-sv9-review-overlay-v1"
CANDIDATE_SCHEMA_VERSIONS = frozenset(
    {
        "evidence-vault-sv9-judgment-candidate-v1",
        "evidence-vault-sv9-judgment-candidate-v2",
    }
)
DECISIONS = frozenset({"approve", "reject"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CANDIDATE_BASE_FIELDS = frozenset(
    "schema_version plan canonical_plan_fingerprint current_series_fingerprint "
    "candidate_series_fingerprint component_evaluations evidence_bindings "
    "candidate_tile_judgments candidate_component_sentinels assessment telemetry "
    "evaluation_bundle_fingerprint assessment_fingerprint score_fingerprint "
    "complete_record_fingerprint".split()
)
_CANDIDATE_PAYLOAD_FIELDS = {
    "evidence-vault-sv9-judgment-candidate-v1": _CANDIDATE_BASE_FIELDS,
    "evidence-vault-sv9-judgment-candidate-v2": _CANDIDATE_BASE_FIELDS
    | {"authoritative_relation_witness"},
}
_AUTHORITATIVE_WITNESS_FIELDS = frozenset(
    "canonical_memory_version adoption_event_id adoption_sequence "
    "candidate_packet_fingerprint request_fingerprint".split()
)

_FIELDS = frozenset(
    """
    schema_version id workspace_id brand_id candidate_id scan_run_id capture_id
    operation_plan_id source_scan_id candidate_schema_version
    candidate_complete_record_fingerprint canonical_plan_fingerprint
    current_series_fingerprint candidate_series_fingerprint
    evaluation_bundle_fingerprint assessment_fingerprint score_fingerprint
    decision expected_authority_event_id expected_authority_event_fingerprint
    expected_authority_event_sequence active_authority_event_id
    active_authority_event_fingerprint active_authority_event_sequence
    evaluated_authority_event_id evaluated_authority_event_fingerprint
    evaluated_authority_sequence
    reopen_authority_event_id reopen_authority_event_fingerprint
    reopen_authority_event_sequence reopen_active_parent_event_id
    reopen_active_parent_event_fingerprint reopen_active_parent_event_sequence
    reopen_delta_fingerprint reopen_review_overlay_fingerprint
    successor_authority_event_id successor_authority_event_fingerprint
    successor_authority_event_sequence resolution_key_hash created_at
    """.split()
)
_KEY_FIELDS = (
    "schema_version",
    "workspace_id",
    "brand_id",
    "candidate_id",
    "scan_run_id",
    "capture_id",
    "operation_plan_id",
    "source_scan_id",
    "candidate_schema_version",
    "candidate_complete_record_fingerprint",
    "canonical_plan_fingerprint",
    "current_series_fingerprint",
    "candidate_series_fingerprint",
    "evaluation_bundle_fingerprint",
    "assessment_fingerprint",
    "score_fingerprint",
    "decision",
    "expected_authority_event_id",
    "expected_authority_event_fingerprint",
    "expected_authority_event_sequence",
    "active_authority_event_id",
    "active_authority_event_fingerprint",
    "active_authority_event_sequence",
    "evaluated_authority_event_id",
    "evaluated_authority_event_fingerprint",
    "evaluated_authority_sequence",
    "reopen_authority_event_id",
    "reopen_authority_event_fingerprint",
    "reopen_authority_event_sequence",
    "reopen_active_parent_event_id",
    "reopen_active_parent_event_fingerprint",
    "reopen_active_parent_event_sequence",
    "reopen_delta_fingerprint",
    "reopen_review_overlay_fingerprint",
    "successor_authority_event_id",
    "successor_authority_event_fingerprint",
    "successor_authority_event_sequence",
)
_FINGERPRINT_FIELDS = (
    "candidate_complete_record_fingerprint",
    "canonical_plan_fingerprint",
    "current_series_fingerprint",
    "candidate_series_fingerprint",
    "evaluation_bundle_fingerprint",
    "assessment_fingerprint",
    "score_fingerprint",
    "expected_authority_event_fingerprint",
    "active_authority_event_fingerprint",
    "evaluated_authority_event_fingerprint",
    "reopen_authority_event_fingerprint",
    "reopen_active_parent_event_fingerprint",
    "reopen_delta_fingerprint",
    "reopen_review_overlay_fingerprint",
)


class EvidenceVaultSv9ReviewResolutionError(ValueError):
    """Raised when a review-resolution record is not canonical or safe."""


def _fail(label: str) -> None:
    raise EvidenceVaultSv9ReviewResolutionError(
        f"SV9 review resolution {label} is invalid."
    )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(label)
    try:
        encoded = memory.canonical_json(dict(value))
        encoded.encode("utf-8")
        result = json.loads(encoded)
    except (TypeError, UnicodeError, ValueError, memory.JudgmentMemoryContractError):
        _fail(label)
    if type(result) is not dict:
        _fail(label)
    return result


def _review_overlay(value: Any) -> dict[str, Any]:
    overlay = _mapping(value, "reopen_review_overlay")
    schema_version = overlay.get("schema_version")
    if schema_version == "evidence-vault-sv9-judgment-authority-event-v1":
        expected_fields = {"schema_version", "request", "review_state", "signed_delta"}
        request_version = "evidence-vault-sv9-judgment-authority-request-v1"
    elif schema_version == "evidence-vault-sv9-judgment-authority-event-v2":
        expected_fields = {
            "schema_version",
            "request",
            "review_state",
            "signed_delta",
            "workset_partition",
            "workset_partition_fingerprint",
        }
        request_version = "evidence-vault-sv9-judgment-authority-request-v2"
    else:
        _fail("reopen_review_overlay schema_version")
    if set(overlay) != expected_fields or overlay["review_state"] != "pending":
        _fail("reopen_review_overlay fields")

    request = _mapping(overlay["request"], "reopen_review_overlay request")
    request_fields = {
        "schema_version",
        "action",
        "candidate_id",
        "source_scan_id",
        "expected_predecessor_event_fingerprint",
        "delta_fingerprint",
    }
    if request_version.endswith("v2"):
        request_fields.add("workset_partition_fingerprint")
    if set(request) != request_fields or request["schema_version"] != request_version:
        _fail("reopen_review_overlay request")
    if request["action"] != "reopen_authority" or request["candidate_id"] is not None:
        _fail("reopen_review_overlay request action")
    _text(request["source_scan_id"], "reopen_review_overlay source_scan_id")
    _sha(request["delta_fingerprint"], "reopen_review_overlay delta_fingerprint")
    if request["expected_predecessor_event_fingerprint"] is not None:
        _sha(
            request["expected_predecessor_event_fingerprint"],
            "reopen_review_overlay predecessor",
        )
    signed_delta = _mapping(overlay["signed_delta"], "reopen_review_overlay signed_delta")
    if signed_delta.get("canonical_delta_fingerprint") != request["delta_fingerprint"]:
        _fail("reopen_review_overlay delta binding")
    if request_version.endswith("v2"):
        _sha(
            overlay["workset_partition_fingerprint"],
            "reopen_review_overlay workset_partition_fingerprint",
        )
        if overlay["workset_partition_fingerprint"] != request[
            "workset_partition_fingerprint"
        ]:
            _fail("reopen_review_overlay workset binding")
    return overlay


def _text(value: Any, label: str, *, maximum: int | None = None) -> str:
    if type(value) is not str or not value or value != value.strip():
        _fail(label)
    if maximum is not None and len(value) > maximum:
        _fail(label)
    return value


def _audit_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.strip():
        _fail(label)
    return value


def _uuid(value: Any, label: str) -> str:
    if type(value) is not str:
        _fail(label)
    try:
        valid = str(UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        _fail(label)
    return value


def _sha(value: Any, label: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        _fail(label)
    return value


def _sequence(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        _fail(label)
    return value


def _key_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {name: value[name] for name in _KEY_FIELDS}


def _validate_candidate_payload(
    candidate_payload: Mapping[str, Any] | None,
    resolution: Mapping[str, Any],
) -> None:
    """Bind a supplied candidate payload to the authority head it evaluated.

    Candidate v1 predates the persisted authority witness and remains accepted
    without one. Candidate v2 has the witness as part of its existing payload
    contract, so an omitted or malformed witness is rejected rather than being
    treated as an unbound candidate.
    """

    schema_version = resolution["candidate_schema_version"]
    if candidate_payload is None:
        if schema_version.endswith("v2"):
            _fail("candidate payload witness")
        return

    candidate = _mapping(candidate_payload, "candidate payload")
    expected_fields = _CANDIDATE_PAYLOAD_FIELDS.get(candidate.get("schema_version"))
    if expected_fields is None or set(candidate) != expected_fields:
        _fail("candidate payload fields")
    if candidate["schema_version"] != schema_version:
        _fail("candidate payload schema_version")
    try:
        complete = authority_event.candidate_complete_record_fingerprint(candidate)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise EvidenceVaultSv9ReviewResolutionError(
            "SV9 review resolution candidate payload is invalid."
        ) from exc
    if candidate["complete_record_fingerprint"] != complete:
        _fail("candidate payload complete_record_fingerprint")
    for name in (
        "canonical_plan_fingerprint",
        "current_series_fingerprint",
        "candidate_series_fingerprint",
        "evaluation_bundle_fingerprint",
        "assessment_fingerprint",
        "score_fingerprint",
    ):
        if candidate[name] != resolution[name]:
            _fail(f"candidate payload {name}")
    if candidate["complete_record_fingerprint"] != resolution[
        "candidate_complete_record_fingerprint"
    ]:
        _fail("candidate payload candidate_complete_record_fingerprint")

    if not schema_version.endswith("v2"):
        return
    witness = candidate["authoritative_relation_witness"]
    if type(witness) is not dict:
        _fail("candidate payload authoritative_relation_witness")
    operational = witness.get("operational_witness")
    if type(operational) is not dict or set(operational) != _AUTHORITATIVE_WITNESS_FIELDS:
        _fail("candidate payload operational_witness")
    if witness.get("source_scan_id") != resolution["source_scan_id"]:
        _fail("candidate payload witness source_scan_id")
    adoption_event_id = _uuid(
        operational.get("adoption_event_id"),
        "candidate payload adoption_event_id",
    )
    adoption_sequence = _sequence(
        operational.get("adoption_sequence"),
        "candidate payload adoption_sequence",
    )
    if (
        adoption_event_id != resolution["evaluated_authority_event_id"]
        or adoption_sequence != resolution["evaluated_authority_sequence"]
    ):
        _fail("candidate payload evaluated authority binding")


def _validate(
    value: Mapping[str, Any],
    *,
    check_key: bool,
    candidate_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolution = _mapping(value, "resolution")
    if set(resolution) != _FIELDS:
        _fail("fields")
    if resolution["schema_version"] != RESOLUTION_SCHEMA_VERSION:
        _fail("schema_version")

    for name in "id workspace_id brand_id candidate_id scan_run_id capture_id operation_plan_id".split():
        resolution[name] = _uuid(resolution[name], name)
    resolution["source_scan_id"] = _text(
        resolution["source_scan_id"], "source_scan_id", maximum=300
    )
    if resolution["candidate_schema_version"] not in CANDIDATE_SCHEMA_VERSIONS:
        _fail("candidate_schema_version")
    for name in _FINGERPRINT_FIELDS:
        resolution[name] = _sha(resolution[name], name)

    decision = resolution["decision"]
    if type(decision) is not str or decision not in DECISIONS:
        _fail("decision")

    for name in (
        "expected_authority_event_id",
        "active_authority_event_id",
        "evaluated_authority_event_id",
        "reopen_authority_event_id",
        "reopen_active_parent_event_id",
    ):
        resolution[name] = _uuid(resolution[name], name)
    for name in (
        "expected_authority_event_fingerprint",
        "active_authority_event_fingerprint",
        "evaluated_authority_event_fingerprint",
        "reopen_authority_event_fingerprint",
        "reopen_active_parent_event_fingerprint",
        "reopen_delta_fingerprint",
        "reopen_review_overlay_fingerprint",
    ):
        resolution[name] = _sha(resolution[name], name)
    for name in (
        "expected_authority_event_sequence",
        "active_authority_event_sequence",
        "evaluated_authority_sequence",
        "reopen_authority_event_sequence",
        "reopen_active_parent_event_sequence",
    ):
        resolution[name] = _sequence(resolution[name], name)

    if (
        resolution["reopen_authority_event_id"]
        != resolution["expected_authority_event_id"]
        or resolution["reopen_authority_event_fingerprint"]
        != resolution["expected_authority_event_fingerprint"]
        or resolution["reopen_authority_event_sequence"]
        != resolution["expected_authority_event_sequence"]
    ):
        _fail("reopen authority binding")
    if (
        resolution["reopen_active_parent_event_id"]
        != resolution["active_authority_event_id"]
        or resolution["reopen_active_parent_event_fingerprint"]
        != resolution["active_authority_event_fingerprint"]
        or resolution["reopen_active_parent_event_sequence"]
        != resolution["active_authority_event_sequence"]
        or resolution["reopen_active_parent_event_sequence"]
        >= resolution["reopen_authority_event_sequence"]
    ):
        _fail("reopen active parent binding")
    if (
        resolution["evaluated_authority_event_id"]
        != resolution["active_authority_event_id"]
        or resolution["evaluated_authority_event_fingerprint"]
        != resolution["active_authority_event_fingerprint"]
        or resolution["evaluated_authority_sequence"]
        != resolution["active_authority_event_sequence"]
        or resolution["evaluated_authority_event_id"]
        != resolution["reopen_active_parent_event_id"]
        or resolution["evaluated_authority_event_fingerprint"]
        != resolution["reopen_active_parent_event_fingerprint"]
        or resolution["evaluated_authority_sequence"]
        != resolution["reopen_active_parent_event_sequence"]
    ):
        _fail("evaluated authority binding")

    successor = (
        resolution["successor_authority_event_id"],
        resolution["successor_authority_event_fingerprint"],
        resolution["successor_authority_event_sequence"],
    )
    if all(item is None for item in successor):
        has_successor = False
    elif all(item is not None for item in successor):
        has_successor = True
        resolution["successor_authority_event_id"] = _uuid(
            resolution["successor_authority_event_id"],
            "successor_authority_event_id",
        )
        resolution["successor_authority_event_fingerprint"] = _sha(
            resolution["successor_authority_event_fingerprint"],
            "successor_authority_event_fingerprint",
        )
        resolution["successor_authority_event_sequence"] = _sequence(
            resolution["successor_authority_event_sequence"],
            "successor_authority_event_sequence",
        )
    else:
        _fail("successor authority binding")

    if resolution["active_authority_event_sequence"] > resolution[
        "expected_authority_event_sequence"
    ]:
        _fail("active authority binding")
    if resolution["active_authority_event_id"] == resolution[
        "expected_authority_event_id"
    ]:
        if (
            resolution["active_authority_event_fingerprint"]
            != resolution["expected_authority_event_fingerprint"]
            or resolution["active_authority_event_sequence"]
            != resolution["expected_authority_event_sequence"]
        ):
            _fail("active authority binding")
    elif resolution["active_authority_event_sequence"] >= resolution[
        "expected_authority_event_sequence"
    ]:
        _fail("active authority lineage")

    if decision == "approve":
        if not has_successor:
            _fail("approve successor")
        if resolution["successor_authority_event_id"] in {
            resolution["expected_authority_event_id"],
            resolution["active_authority_event_id"],
        }:
            _fail("approve successor lineage")
        if resolution["successor_authority_event_sequence"] != resolution[
            "expected_authority_event_sequence"
        ] + 1:
            _fail("approve successor sequence")
    elif has_successor:
        _fail("reject successor")

    resolution["resolution_key_hash"] = _sha(
        resolution["resolution_key_hash"], "resolution_key_hash"
    )
    resolution["created_at"] = _audit_text(resolution["created_at"], "created_at")
    _validate_candidate_payload(candidate_payload, resolution)
    if check_key and resolution["resolution_key_hash"] != _key_hash(resolution):
        _fail("resolution_key_hash")
    return resolution


def _key_hash(value: Mapping[str, Any]) -> str:
    return memory.canonical_fingerprint(
        RESOLUTION_KEY_NAMESPACE,
        _key_payload(value),
    )


def review_resolution_idempotency_fingerprint(
    value: Mapping[str, Any],
    *,
    candidate_payload: Mapping[str, Any] | None = None,
) -> str:
    """Return the deterministic hash used by the migration's unique key.

    The hash covers every semantic request binding but excludes the generated
    row id, audit timestamp, and the hash itself. It is not an authority event
    fingerprint and it does not perform persistence or idempotent replay.
    """

    normalized = _validate(
        value, check_key=False, candidate_payload=candidate_payload
    )
    return _key_hash(normalized)


def build_evidence_vault_sv9_review_resolution(
    *,
    id: str,
    workspace_id: str,
    brand_id: str,
    candidate_id: str,
    scan_run_id: str,
    capture_id: str,
    operation_plan_id: str,
    source_scan_id: str,
    candidate_schema_version: str,
    candidate_complete_record_fingerprint: str,
    canonical_plan_fingerprint: str,
    current_series_fingerprint: str,
    candidate_series_fingerprint: str,
    evaluation_bundle_fingerprint: str,
    assessment_fingerprint: str,
    score_fingerprint: str,
    decision: str,
    expected_authority_event_id: str,
    expected_authority_event_fingerprint: str,
    expected_authority_event_sequence: int,
    active_authority_event_id: str,
    active_authority_event_fingerprint: str,
    active_authority_event_sequence: int,
    evaluated_authority_event_id: str,
    evaluated_authority_event_fingerprint: str,
    evaluated_authority_sequence: int,
    reopen_authority_event_id: str,
    reopen_authority_event_fingerprint: str,
    reopen_authority_event_sequence: int,
    reopen_active_parent_event_id: str,
    reopen_active_parent_event_fingerprint: str,
    reopen_active_parent_event_sequence: int,
    reopen_delta_fingerprint: str,
    reopen_review_overlay: Mapping[str, Any],
    candidate_payload: Mapping[str, Any] | None = None,
    successor_authority_event_id: str | None = None,
    successor_authority_event_fingerprint: str | None = None,
    successor_authority_event_sequence: int | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one canonical resolution row without writing or publishing it."""

    value: dict[str, Any] = {
        "schema_version": RESOLUTION_SCHEMA_VERSION,
        "id": id,
        "workspace_id": workspace_id,
        "brand_id": brand_id,
        "candidate_id": candidate_id,
        "scan_run_id": scan_run_id,
        "capture_id": capture_id,
        "operation_plan_id": operation_plan_id,
        "source_scan_id": source_scan_id,
        "candidate_schema_version": candidate_schema_version,
        "candidate_complete_record_fingerprint": candidate_complete_record_fingerprint,
        "canonical_plan_fingerprint": canonical_plan_fingerprint,
        "current_series_fingerprint": current_series_fingerprint,
        "candidate_series_fingerprint": candidate_series_fingerprint,
        "evaluation_bundle_fingerprint": evaluation_bundle_fingerprint,
        "assessment_fingerprint": assessment_fingerprint,
        "score_fingerprint": score_fingerprint,
        "decision": decision,
        "expected_authority_event_id": expected_authority_event_id,
        "expected_authority_event_fingerprint": expected_authority_event_fingerprint,
        "expected_authority_event_sequence": expected_authority_event_sequence,
        "active_authority_event_id": active_authority_event_id,
        "active_authority_event_fingerprint": active_authority_event_fingerprint,
        "active_authority_event_sequence": active_authority_event_sequence,
        "evaluated_authority_event_id": evaluated_authority_event_id,
        "evaluated_authority_event_fingerprint": evaluated_authority_event_fingerprint,
        "evaluated_authority_sequence": evaluated_authority_sequence,
        "reopen_authority_event_id": reopen_authority_event_id,
        "reopen_authority_event_fingerprint": reopen_authority_event_fingerprint,
        "reopen_authority_event_sequence": reopen_authority_event_sequence,
        "reopen_active_parent_event_id": reopen_active_parent_event_id,
        "reopen_active_parent_event_fingerprint": reopen_active_parent_event_fingerprint,
        "reopen_active_parent_event_sequence": reopen_active_parent_event_sequence,
        "reopen_delta_fingerprint": reopen_delta_fingerprint,
        "reopen_review_overlay_fingerprint": review_overlay_fingerprint(
            reopen_review_overlay
        ),
        "successor_authority_event_id": successor_authority_event_id,
        "successor_authority_event_fingerprint": successor_authority_event_fingerprint,
        "successor_authority_event_sequence": successor_authority_event_sequence,
        "resolution_key_hash": _HOLDER_HASH,
        "created_at": created_at,
    }
    normalized = _validate(
        value, check_key=False, candidate_payload=candidate_payload
    )
    normalized["resolution_key_hash"] = _key_hash(normalized)
    return _validate(
        normalized, check_key=True, candidate_payload=candidate_payload
    )


_HOLDER_HASH = "0" * 64


def review_overlay_fingerprint(value: Mapping[str, Any]) -> str:
    """Return the canonical identity of the exact reopen event payload."""

    return memory.canonical_fingerprint(
        REOPEN_REVIEW_OVERLAY_NAMESPACE,
        _review_overlay(value),
    )


def validate_evidence_vault_sv9_review_resolution(
    value: Mapping[str, Any],
    *,
    candidate_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a persisted resolution record without authority side effects.

    Database existence, candidate provenance joins, active-head CAS, and
    successor event lineage remain migration-038 checks; this pure validator
    only proves the canonical shape and internal relationships.
    """

    return _validate(value, check_key=True, candidate_payload=candidate_payload)
