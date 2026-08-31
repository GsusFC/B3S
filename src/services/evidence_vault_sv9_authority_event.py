"""Pure replayable SV9 authority request, candidate, and event contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from src.sv9 import judgment_memory as memory

_REQUEST_VERSION = "evidence-vault-sv9-judgment-authority-request-v1"
_IDEMPOTENCY_VERSION = "evidence-vault-sv9-authority-application-idempotency-v1"
_EVENT_VERSION = "evidence-vault-sv9-judgment-authority-event-v1"
_CANDIDATE_RECORDS = {
    "evidence-vault-sv9-judgment-candidate-v1": "evidence-vault-sv9-judgment-candidate-record-v1",
    "evidence-vault-sv9-judgment-candidate-v2": "evidence-vault-sv9-judgment-candidate-record-v2",
}
# fmt: off
_REQUEST_FIELDS = frozenset("schema_version action candidate_id source_scan_id expected_predecessor_event_fingerprint delta_fingerprint".split())
_CANDIDATE_BASE = tuple("schema_version plan canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint component_evaluations evidence_bindings candidate_tile_judgments candidate_component_sentinels assessment telemetry evaluation_bundle_fingerprint assessment_fingerprint score_fingerprint".split())
_CANDIDATE_CONTEXT = frozenset({"id", "source_scan_id", "created_at"})
_CANDIDATE_IDENTITY = frozenset("id complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint source_scan_id".split())
_EVENT_IDENTITY = tuple("event_id event_type sequence predecessor_event_fingerprint active_parent_event_fingerprint candidate_id candidate_complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint delta_fingerprint request_fingerprint idempotency_key_hash".split())
_EVENT_FIELDS = frozenset({"schema_version", *_EVENT_IDENTITY, "request", "event_fingerprint", "predecessor_event_id", "active_parent_event_id", "created_at"})
_CANDIDATE_EVENT_FINGERPRINTS = tuple("candidate_complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint".split())
# fmt: on
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceVaultSv9AuthorityEventError(ValueError):
    pass


def _fail(label: str) -> None:
    raise EvidenceVaultSv9AuthorityEventError(f"SV9 authority {label} is invalid.")


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


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
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


def _optional_sha(value: Any, label: str) -> str | None:
    return None if value is None else _sha(value, label)


def _request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = _mapping(value, "request")
    if set(request) != _REQUEST_FIELDS or request.get("schema_version") != _REQUEST_VERSION:
        _fail("request")
    if request.get("action") not in {"adopt_candidate", "reopen_authority"}:
        _fail("request action")
    request["source_scan_id"] = _text(request["source_scan_id"], "request source_scan_id")
    request["expected_predecessor_event_fingerprint"] = _optional_sha(
        request["expected_predecessor_event_fingerprint"], "request predecessor"
    )
    if request["action"] == "adopt_candidate":
        request["candidate_id"] = _uuid(request["candidate_id"], "request candidate_id")
        if request["delta_fingerprint"] is not None:
            _fail("request")
    elif request["candidate_id"] is not None:
        _fail("request")
    else:
        request["delta_fingerprint"] = _sha(request["delta_fingerprint"], "request delta_fingerprint")
    return request


# fmt: off
def build_evidence_vault_sv9_authority_request(*, action: str, candidate_id: str | None, expected_predecessor_event_fingerprint: str | None, delta_fingerprint: str | None, source_scan_id: str) -> dict[str, Any]:
    return _request({"schema_version": _REQUEST_VERSION, "action": action, "candidate_id": candidate_id, "source_scan_id": source_scan_id, "expected_predecessor_event_fingerprint": expected_predecessor_event_fingerprint, "delta_fingerprint": delta_fingerprint})

def authority_application_idempotency_fingerprint(request: Mapping[str, Any]) -> str:
    request = _request(request)
    return memory.canonical_fingerprint(_IDEMPOTENCY_VERSION, {"action": request["action"], "source_scan_id": request["source_scan_id"], "candidate_id": request["candidate_id"], "canonical_delta_fingerprint": request["delta_fingerprint"], "expected_predecessor_event_fingerprint": request["expected_predecessor_event_fingerprint"]})
# fmt: on


def _candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    candidate, expected = _mapping(value, "candidate"), set(_CANDIDATE_BASE)
    version = candidate.get("schema_version")
    if version not in _CANDIDATE_RECORDS:
        _fail("candidate schema_version")
    if version.endswith("v2"):
        expected.add("authoritative_relation_witness")
    context, allowed = (
        set(candidate) & _CANDIDATE_CONTEXT,
        expected | {"complete_record_fingerprint"} | _CANDIDATE_CONTEXT,
    )
    if set(candidate) - allowed or not expected <= set(candidate) or (context and context != _CANDIDATE_CONTEXT):
        _fail("candidate fields")
    if not isinstance(candidate["plan"], dict) or not isinstance(candidate["assessment"], dict):
        _fail("candidate shape")
    lists = "component_evaluations evidence_bindings candidate_tile_judgments candidate_component_sentinels".split()
    if any(type(candidate[name]) is not list for name in lists):
        _fail("candidate shape")
    telemetry = candidate["telemetry"]
    if (
        type(telemetry) is not dict
        or set(telemetry) != {"call_count", "calls_avoided", "reused_tile_count", "evaluated_tile_count"}
        or any(type(item) is not int or item < 0 for item in telemetry.values())
    ):
        _fail("candidate telemetry")
    if version.endswith("v2") and not isinstance(candidate["authoritative_relation_witness"], dict):
        _fail("candidate witness")
    for name in "canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint evaluation_bundle_fingerprint assessment_fingerprint score_fingerprint".split():
        _sha(candidate[name], f"candidate {name}")
    for row in candidate["evidence_bindings"]:
        if type(row) is not dict or set(row) != {
            "tile_id",
            "evidence_record_id",
            "evidence_ref",
            "evidence_fingerprint",
        }:
            _fail("candidate evidence_bindings")
        _text(row["tile_id"], "candidate tile_id")
        _uuid(row["evidence_record_id"], "candidate evidence_record_id")
        _text(row["evidence_ref"], "candidate evidence_ref")
        _sha(row["evidence_fingerprint"], "candidate evidence_fingerprint")
    bundle = memory.canonical_fingerprint(
        "sv9-judgment-evaluation-bundle-v1",
        {
            "canonical_plan_fingerprint": candidate["canonical_plan_fingerprint"],
            "evaluations": candidate["component_evaluations"],
        },
    )
    if candidate["evaluation_bundle_fingerprint"] != bundle:
        _fail("candidate evaluation bundle")
    if "complete_record_fingerprint" in candidate:
        _sha(candidate["complete_record_fingerprint"], "candidate complete_record_fingerprint")
    if context:
        _uuid(candidate["id"], "candidate id")
        _text(candidate["source_scan_id"], "candidate source_scan_id")
        _audit_text(candidate["created_at"], "candidate created_at")
    return candidate


def candidate_complete_record_fingerprint(candidate: Mapping[str, Any]) -> str:
    candidate = _candidate(candidate)
    preimage = {name: candidate[name] for name in _CANDIDATE_BASE}
    if candidate["schema_version"].endswith("v2"):
        preimage["authoritative_relation_witness"] = candidate["authoritative_relation_witness"]
    result = memory.canonical_fingerprint(_CANDIDATE_RECORDS[candidate["schema_version"]], preimage)
    if candidate.get("complete_record_fingerprint") not in {None, result}:
        _fail("candidate complete_record_fingerprint")
    return result


def _candidate_identity(value: Mapping[str, Any]) -> dict[str, str]:
    candidate = _mapping(value, "candidate identity")
    if set(candidate) != _CANDIDATE_IDENTITY:
        _fail("candidate identity fields")
    candidate["id"] = _uuid(candidate["id"], "candidate identity id")
    candidate["source_scan_id"] = _text(candidate["source_scan_id"], "candidate identity source_scan_id")
    for name in _CANDIDATE_IDENTITY - {"id", "source_scan_id"}:
        candidate[name] = _sha(candidate[name], f"candidate identity {name}")
    return candidate


def _identity(event: Mapping[str, Any]) -> dict[str, Any]:
    return {name: event[name] for name in _EVENT_IDENTITY}


def _semantics(event: Mapping[str, Any]) -> None:
    kind, request = event["event_type"], event["request"]
    if kind not in {"adopt", "supersede", "reopen"}:
        _fail("event type")
    root = kind == "adopt"
    if (root and event["sequence"] != 1) or (not root and event["sequence"] < 2):
        _fail("event sequence")
    parents = "predecessor_event_fingerprint active_parent_event_fingerprint predecessor_event_id active_parent_event_id".split()
    present = [event[name] is not None for name in parents]
    if (root and any(present)) or (not root and not all(present)):
        _fail("event parent facts")
    if request["expected_predecessor_event_fingerprint"] != event["predecessor_event_fingerprint"]:
        _fail("event request predecessor")
    candidate = [event["candidate_id"], *(event[name] for name in _CANDIDATE_EVENT_FINGERPRINTS)]
    if kind in {"adopt", "supersede"}:
        if any(item is None for item in candidate) or event["delta_fingerprint"] is not None:
            _fail("candidate event")
        if (
            request["action"] != "adopt_candidate"
            or request["candidate_id"] != event["candidate_id"]
            or request["delta_fingerprint"] is not None
        ):
            _fail("candidate event request")
    elif (
        any(item is not None for item in candidate)
        or request["action"] != "reopen_authority"
        or event["delta_fingerprint"] != request["delta_fingerprint"]
    ):
        _fail("reopen event")


def validate_evidence_vault_sv9_authority_event(value: Mapping[str, Any]) -> dict[str, Any]:
    event = _mapping(value, "event")
    if set(event) != _EVENT_FIELDS or event.get("schema_version") != _EVENT_VERSION:
        _fail("event fields")
    event["event_id"] = _uuid(event["event_id"], "event_id")
    if type(event["sequence"]) is not int or event["sequence"] < 1:
        _fail("event sequence")
    for name in "predecessor_event_fingerprint active_parent_event_fingerprint delta_fingerprint".split():
        event[name] = _optional_sha(event[name], f"event {name}")
    for name in "predecessor_event_id active_parent_event_id".split():
        event[name] = None if event[name] is None else _uuid(event[name], f"event {name}")
    event["created_at"] = _audit_text(event["created_at"], "event created_at")
    event["candidate_id"] = (
        None if event["candidate_id"] is None else _uuid(event["candidate_id"], "event candidate_id")
    )
    for name in _CANDIDATE_EVENT_FINGERPRINTS:
        event[name] = _optional_sha(event[name], f"event {name}")
    event["current_series_fingerprint"] = _sha(event["current_series_fingerprint"], "event current_series_fingerprint")
    event["request"] = _request(event["request"])
    for name in "request_fingerprint idempotency_key_hash event_fingerprint".split():
        event[name] = _sha(event[name], f"event {name}")
    if event["request_fingerprint"] != memory.canonical_fingerprint(_REQUEST_VERSION, event["request"]):
        _fail("event request_fingerprint")
    if event["idempotency_key_hash"] != authority_application_idempotency_fingerprint(event["request"]):
        _fail("event idempotency_key_hash")
    _semantics(event)
    if event["event_fingerprint"] != memory.canonical_fingerprint(_EVENT_VERSION, _identity(event)):
        _fail("event event_fingerprint")
    return event


# fmt: off
def build_evidence_vault_sv9_authority_event(*, event_id: str, event_type: str, sequence: int, predecessor_event_fingerprint: str | None, active_parent_event_fingerprint: str | None, candidate_identity: Mapping[str, Any] | None, current_series_fingerprint: str, delta_fingerprint: str | None, request: Mapping[str, Any], idempotency_key_hash: str, predecessor_event_id: str | None = None, active_parent_event_id: str | None = None, created_at: str | None = None) -> dict[str, Any]:
    candidate, request = (None if candidate_identity is None else _candidate_identity(candidate_identity)), _request(request)
    if candidate is not None and (request["source_scan_id"] != candidate["source_scan_id"] or current_series_fingerprint != candidate["current_series_fingerprint"]): _fail("candidate identity binding")
    event = {
        "schema_version": _EVENT_VERSION, "event_id": event_id, "event_type": event_type, "sequence": sequence, "predecessor_event_fingerprint": predecessor_event_fingerprint, "active_parent_event_fingerprint": active_parent_event_fingerprint,
        "candidate_id": None if candidate is None else candidate["id"], "candidate_complete_record_fingerprint": None if candidate is None else candidate["complete_record_fingerprint"], "evaluation_bundle_fingerprint": None if candidate is None else candidate["evaluation_bundle_fingerprint"], "canonical_plan_fingerprint": None if candidate is None else candidate["canonical_plan_fingerprint"],
        "current_series_fingerprint": current_series_fingerprint, "candidate_series_fingerprint": None if candidate is None else candidate["candidate_series_fingerprint"], "assessment_fingerprint": None if candidate is None else candidate["assessment_fingerprint"], "score_fingerprint": None if candidate is None else candidate["score_fingerprint"], "delta_fingerprint": delta_fingerprint,
        "request_fingerprint": memory.canonical_fingerprint(_REQUEST_VERSION, request), "idempotency_key_hash": idempotency_key_hash, "request": request, "predecessor_event_id": predecessor_event_id, "active_parent_event_id": active_parent_event_id, "created_at": created_at,
    }
    event["event_fingerprint"] = memory.canonical_fingerprint(_EVENT_VERSION, _identity(event))
    return validate_evidence_vault_sv9_authority_event(event)
# fmt: on
