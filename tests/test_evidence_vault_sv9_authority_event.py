# fmt: off
from copy import deepcopy
import json

import pytest

from src.services import evidence_vault_sv9_authority_event as authority
from src.sv9.judgment_memory import canonical_fingerprint

_REQUEST = "evidence-vault-sv9-judgment-authority-request-v1"
_IDEMPOTENCY = "evidence-vault-sv9-authority-application-idempotency-v1"
_EVENT = "evidence-vault-sv9-judgment-authority-event-v1"
_IDENTITY = "event_id event_type sequence predecessor_event_fingerprint active_parent_event_fingerprint candidate_id candidate_complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint delta_fingerprint request_fingerprint idempotency_key_hash".split()
_H = lambda number: f"{number:064x}"
_U = lambda number: f"00000000-0000-0000-0000-{number:012d}"


def _candidate(version="v2"):
    value = {"schema_version": f"evidence-vault-sv9-judgment-candidate-{version}", "plan": {}, "canonical_plan_fingerprint": _H(1), "current_series_fingerprint": _H(2), "candidate_series_fingerprint": _H(3), "component_evaluations": [], "evidence_bindings": [], "candidate_tile_judgments": [], "candidate_component_sentinels": [], "assessment": {}, "telemetry": {"call_count": 0, "calls_avoided": 0, "reused_tile_count": 0, "evaluated_tile_count": 0}, "assessment_fingerprint": _H(4), "score_fingerprint": _H(5)}
    value["evaluation_bundle_fingerprint"] = canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": value["canonical_plan_fingerprint"], "evaluations": []})
    if version == "v2": value["authoritative_relation_witness"] = {}
    value["complete_record_fingerprint"] = authority.candidate_complete_record_fingerprint(value)
    return value


def _identity(candidate, source="scan"):
    return {"id": _U(1), "source_scan_id": source, **{key: candidate[key] for key in "complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint".split()}}


def _event(kind="adopt", candidate=None, source="scan"):
    candidate = None if kind == "reopen" else candidate or _candidate(); predecessor = None if kind == "adopt" else _H(80); active = None if kind == "adopt" else _H(81); delta = _H(82) if kind == "reopen" else None
    request = authority.build_evidence_vault_sv9_authority_request(action="reopen_authority" if kind == "reopen" else "adopt_candidate", candidate_id=None if candidate is None else _U(1), expected_predecessor_event_fingerprint=predecessor, delta_fingerprint=delta, source_scan_id=source)
    return authority.build_evidence_vault_sv9_authority_event(event_id=_U({"adopt": 11, "supersede": 12, "reopen": 13}[kind]), event_type=kind, sequence=1 if kind == "adopt" else 2, predecessor_event_fingerprint=predecessor, active_parent_event_fingerprint=active, candidate_identity=None if candidate is None else _identity(candidate, source), current_series_fingerprint=_H(2) if candidate is None else candidate["current_series_fingerprint"], delta_fingerprint=delta, request=request, idempotency_key_hash=authority.authority_application_idempotency_fingerprint(request), predecessor_event_id=None if predecessor is None else _U(80), active_parent_event_id=None if active is None else _U(81))


def _old_idempotency(request):
    return canonical_fingerprint(_IDEMPOTENCY, {"action": request["action"], "source_scan_id": request["source_scan_id"], "candidate_id": request["candidate_id"], "canonical_delta_fingerprint": request["delta_fingerprint"], "expected_predecessor_event_fingerprint": request["expected_predecessor_event_fingerprint"]})


def _old_event(event):
    return canonical_fingerprint(_EVENT, {key: event[key] for key in _IDENTITY})


def _rehash(event):
    event["event_fingerprint"] = _old_event(event)
    return event


def test_fingerprints_match_independent_legacy_formulas_and_json_roundtrip():
    for version in ("v1", "v2"):
        candidate = _candidate(version); namespace = f"evidence-vault-sv9-judgment-candidate-record-{version}"
        assert authority.candidate_complete_record_fingerprint(candidate) == canonical_fingerprint(namespace, {key: value for key, value in candidate.items() if key != "complete_record_fingerprint"})
        projection = candidate | {"id": _U(1), "source_scan_id": "scan", "created_at": "2026-08-30T00:00:00+00:00"}
        assert authority.candidate_complete_record_fingerprint(projection) == candidate["complete_record_fingerprint"]
    request = authority.build_evidence_vault_sv9_authority_request(action="adopt_candidate", candidate_id=_U(1), expected_predecessor_event_fingerprint=None, delta_fingerprint=None, source_scan_id="scan")
    assert request == {"schema_version": _REQUEST, "action": "adopt_candidate", "candidate_id": _U(1), "source_scan_id": "scan", "expected_predecessor_event_fingerprint": None, "delta_fingerprint": None}
    assert authority.authority_application_idempotency_fingerprint(request) == _old_idempotency(request)
    event = _event(); assert event["event_fingerprint"] == _old_event(event); assert authority.validate_evidence_vault_sv9_authority_event(json.loads(json.dumps(event))) == event
    audit = deepcopy(event); audit["created_at"] = "2026-08-30T00:00:00+00:00"; assert authority.validate_evidence_vault_sv9_authority_event(audit)["event_fingerprint"] == event["event_fingerprint"]


def test_adopt_supersede_reopen_are_pure_and_do_not_mutate_inputs():
    candidate = _candidate(); before = deepcopy(candidate); assert authority.candidate_complete_record_fingerprint(candidate) == candidate["complete_record_fingerprint"] and candidate == before
    for kind in ("adopt", "supersede", "reopen"):
        event = _event(kind); before = deepcopy(event); assert authority.validate_evidence_vault_sv9_authority_event(event) == event and event == before


def test_every_identity_and_request_binding_tamper_fails_closed():
    event = _event("supersede")
    for key in [*_IDENTITY, "event_fingerprint"]:
        bad = deepcopy(event); bad[key] = "reopen" if key == "event_type" else 3 if key == "sequence" else _U(999) if key.endswith("_id") else _H(999)
        with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError): authority.validate_evidence_vault_sv9_authority_event(bad)
    for key, value in (("action", "reopen_authority"), ("source_scan_id", "other"), ("candidate_id", _U(99)), ("delta_fingerprint", _H(99)), ("expected_predecessor_event_fingerprint", _H(99))):
        bad = deepcopy(event); bad["request"][key] = value
        with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError): authority.validate_evidence_vault_sv9_authority_event(bad)


def test_strict_schema_types_and_event_kind_shapes_fail_closed():
    request = _event()["request"]
    for bad in ({key: value for key, value in request.items() if key != "action"}, request | {"unknown": 1}, request | {"candidate_id": True}, request | {"source_scan_id": " scan"}):
        with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError): authority.authority_application_idempotency_fingerprint(bad)
    candidate = _candidate()
    for bad in ({key: value for key, value in candidate.items() if key != "assessment"}, candidate | {"unknown": 1}, candidate | {"authoritative_relation_witness": []}, candidate | {"complete_record_fingerprint": _H(99)}):
        with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError): authority.candidate_complete_record_fingerprint(bad)
    event = _event("supersede")
    for key, value in (("event_id", "not-a-uuid"), ("current_series_fingerprint", "A" * 64), ("sequence", True), ("sequence", 0)):
        bad = deepcopy(event); bad[key] = value
        with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError): authority.validate_evidence_vault_sv9_authority_event(bad)
    for bad in ({key: value for key, value in event.items() if key != "request"}, event | {"unknown": 1}):
        with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError): authority.validate_evidence_vault_sv9_authority_event(bad)
    adopt = _event(); adopt["candidate_id"] = None; _rehash(adopt)
    reopen = _event("reopen"); reopen["candidate_id"] = _U(7); _rehash(reopen)
    for bad in (adopt, reopen):
        with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError): authority.validate_evidence_vault_sv9_authority_event(bad)


def test_coordinated_changes_with_stale_outer_fingerprint_are_rejected():
    stale, candidate = _event("supersede"), _candidate()
    candidate["current_series_fingerprint"], candidate["assessment_fingerprint"], candidate["score_fingerprint"] = _H(70), _H(71), _H(72); candidate.pop("complete_record_fingerprint"); candidate["complete_record_fingerprint"] = authority.candidate_complete_record_fingerprint(candidate)
    changed = _event("supersede", candidate, "next"); changed["event_fingerprint"] = stale["event_fingerprint"]
    with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError): authority.validate_evidence_vault_sv9_authority_event(changed)
    reopened = _event("reopen"); reopened["request"]["source_scan_id"] = "next"; reopened["request_fingerprint"] = canonical_fingerprint(_REQUEST, reopened["request"]); reopened["idempotency_key_hash"] = _old_idempotency(reopened["request"])
    with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError): authority.validate_evidence_vault_sv9_authority_event(reopened)


def test_v2_reopen_binds_partition_request_idempotency_and_event_fingerprints():
    request = authority.build_evidence_vault_sv9_authority_request(
        action="reopen_authority",
        candidate_id=None,
        expected_predecessor_event_fingerprint=_H(80),
        delta_fingerprint=_H(82),
        source_scan_id="scan",
        workset_partition_fingerprint=_H(83),
    )
    event = authority.build_evidence_vault_sv9_authority_event(
        event_id=_U(13),
        event_type="reopen",
        sequence=2,
        predecessor_event_fingerprint=_H(80),
        active_parent_event_fingerprint=_H(81),
        candidate_identity=None,
        current_series_fingerprint=_H(2),
        delta_fingerprint=_H(82),
        request=request,
        idempotency_key_hash=authority.authority_application_idempotency_fingerprint(request),
        predecessor_event_id=_U(80),
        active_parent_event_id=_U(81),
        workset_partition_fingerprint=_H(83),
    )
    identity = (*_IDENTITY[:14], "workset_partition_fingerprint", *_IDENTITY[14:])
    assert request["schema_version"] == "evidence-vault-sv9-judgment-authority-request-v2"
    assert event["schema_version"] == "evidence-vault-sv9-judgment-authority-event-v2"
    assert authority.validate_evidence_vault_sv9_authority_event(json.loads(json.dumps(event))) == event
    assert authority.authority_application_idempotency_fingerprint(request) != _old_idempotency(
        {key: value for key, value in request.items() if key != "workset_partition_fingerprint"} | {"schema_version": _REQUEST}
    )
    missing = {key: value for key, value in request.items() if key != "workset_partition_fingerprint"}
    with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError):
        authority.authority_application_idempotency_fingerprint(missing)
    outer = deepcopy(event)
    outer["workset_partition_fingerprint"] = _H(99)
    outer["event_fingerprint"] = canonical_fingerprint(
        "evidence-vault-sv9-judgment-authority-event-v2", {key: outer[key] for key in identity}
    )
    with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError):
        authority.validate_evidence_vault_sv9_authority_event(outer)
    request_tamper = deepcopy(event)
    request_tamper["request"]["workset_partition_fingerprint"] = _H(99)
    request_tamper["request_fingerprint"] = canonical_fingerprint(
        "evidence-vault-sv9-judgment-authority-request-v2", request_tamper["request"]
    )
    request_tamper["idempotency_key_hash"] = authority.authority_application_idempotency_fingerprint(
        request_tamper["request"]
    )
    request_tamper["event_fingerprint"] = canonical_fingerprint(
        "evidence-vault-sv9-judgment-authority-event-v2",
        {key: request_tamper[key] for key in identity},
    )
    with pytest.raises(authority.EvidenceVaultSv9AuthorityEventError):
        authority.validate_evidence_vault_sv9_authority_event(request_tamper)
# fmt: on
