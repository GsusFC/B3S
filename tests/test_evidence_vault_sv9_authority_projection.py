from copy import deepcopy
import json

import pytest

from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_sv9_authority_projection as authority_projection
from src.services import evidence_vault_sv9_judgment_delta as judgment_delta
from src.sv9.judgment_memory import canonical_fingerprint
from tests.test_sv9_judgment_memory import _series

# fmt: off
_H = lambda number: f"{number:064x}"
_U = lambda number: f"00000000-0000-0000-0000-{number:012d}"
_IDENTITY = (
    "complete_record_fingerprint", "evaluation_bundle_fingerprint", "canonical_plan_fingerprint",
    "current_series_fingerprint", "candidate_series_fingerprint", "assessment_fingerprint", "score_fingerprint",
)

def _delta(number):
    current = judgment_delta.build_evidence_identity_set([{"evidence_ref": f"evidence-{number}", "evidence_fingerprint": _H(600 + number)}])
    return judgment_delta.build_evidence_vault_sv9_judgment_delta(
        current_evidence=current, prior_judgments=[], authoritative_relations=[], current_series_contract=_series(),
    )

def _candidate(number, source, current, score=2):
    value = {
        "schema_version": "evidence-vault-sv9-judgment-candidate-v2", "plan": {},
        "canonical_plan_fingerprint": _H(number + 10), "current_series_fingerprint": current,
        "candidate_series_fingerprint": _H(number + 20), "component_evaluations": [], "evidence_bindings": [],
        "candidate_tile_judgments": [], "candidate_component_sentinels": [], "assessment": {"sv9_score": score},
        "telemetry": {"call_count": 0, "calls_avoided": 0, "reused_tile_count": 0, "evaluated_tile_count": 0},
        "assessment_fingerprint": _H(number + 30), "score_fingerprint": _H(number + 40), "authoritative_relation_witness": {},
    }
    value["evaluation_bundle_fingerprint"] = canonical_fingerprint(
        "sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": value["canonical_plan_fingerprint"], "evaluations": []},
    )
    value["complete_record_fingerprint"] = authority_event.candidate_complete_record_fingerprint(value)
    return value | {"id": _U(number), "source_scan_id": source, "created_at": "2026-08-30T00:00:00+00:00"}

def _identity(candidate):
    return {"id": candidate["id"], "source_scan_id": candidate["source_scan_id"], **{name: candidate[name] for name in _IDENTITY}}

def _event(kind, number, *, candidate, current, source, predecessor=None, active=None, delta=None):
    predecessor_fingerprint = None if predecessor is None else predecessor["event_fingerprint"]
    request = authority_event.build_evidence_vault_sv9_authority_request(
        action="reopen_authority" if kind == "reopen" else "adopt_candidate", candidate_id=None if candidate is None else candidate["id"],
        expected_predecessor_event_fingerprint=predecessor_fingerprint, delta_fingerprint=delta if kind == "reopen" else None, source_scan_id=source,
    )
    return authority_event.build_evidence_vault_sv9_authority_event(
        event_id=_U(number), event_type=kind, sequence=1 if predecessor is None else predecessor["sequence"] + 1,
        predecessor_event_fingerprint=predecessor_fingerprint, active_parent_event_fingerprint=None if active is None else active["event_fingerprint"],
        candidate_identity=None if candidate is None else _identity(candidate), current_series_fingerprint=current, delta_fingerprint=request["delta_fingerprint"],
        request=request, idempotency_key_hash=authority_event.authority_application_idempotency_fingerprint(request),
        predecessor_event_id=None if predecessor is None else predecessor["event_id"], active_parent_event_id=None if active is None else active["event_id"], created_at="2026-08-30T00:00:00+00:00",
    )

def _facts(*, pending=False, older=False, adopted=False, consecutive=False):
    first_delta = _delta(1); current = first_delta["plan"]["current_series_fingerprint"]
    first = _candidate(1, "scan-1", current, 1); adopt = _event("adopt", 11, candidate=first, current=current, source="scan-1")
    if adopted: return first, adopt, adopt, adopt, None, None
    accepted = _candidate(2, "scan-2", current)
    active = _event("supersede", 12, candidate=accepted, current=current, source="scan-2", predecessor=adopt, active=adopt)
    if not pending: return accepted, active, active, adopt if older else active, None, None
    first_head = _event("reopen", 13, candidate=None, current=current, source="review-3", predecessor=active, active=active, delta=first_delta["canonical_delta_fingerprint"])
    first_overlay = {"review_state": "pending", "signed_delta": first_delta, "delta_fingerprint": first_delta["canonical_delta_fingerprint"]}
    if not consecutive: return accepted, first_head, active, first_head, first_overlay, None
    newest_delta = _delta(2); assert newest_delta["plan"]["current_series_fingerprint"] == current
    head = _event("reopen", 14, candidate=None, current=current, source="review-4", predecessor=first_head, active=active, delta=newest_delta["canonical_delta_fingerprint"])
    overlay = {"review_state": "pending", "signed_delta": newest_delta, "delta_fingerprint": newest_delta["canonical_delta_fingerprint"]}
    return accepted, head, active, head, overlay, first_overlay

def _projection(**kwargs):
    candidate, head, active, event, overlay, _ = _facts(**kwargs)
    return authority_projection.build_evidence_vault_sv9_authority_projection(accepted_candidate=candidate, current_head=head, active_authority_event=active, event=event, reopen_review_overlay=overlay)

@pytest.mark.parametrize("kwargs", ({"adopted": True}, {}, {"pending": True}))
def test_builder_validates_stable_and_pending_reopen_json_roundtrips(kwargs):
    facts = _facts(**kwargs); before = deepcopy(facts)
    result = authority_projection.build_evidence_vault_sv9_authority_projection(accepted_candidate=facts[0], current_head=facts[1], active_authority_event=facts[2], event=facts[3], reopen_review_overlay=facts[4])
    assert facts == before
    wire = json.loads(json.dumps(result)); before = deepcopy(wire)
    assert authority_projection.validate_evidence_vault_sv9_authority_projection(wire) == result
    assert wire == before

def test_selected_older_idempotent_occurrence_is_not_required_to_be_head():
    result = _projection(older=True)
    assert result["event"] != result["current_head"]
    assert authority_projection.validate_evidence_vault_sv9_authority_projection(result) == result

@pytest.mark.parametrize("change", (
    lambda value: value.pop("authority"), lambda value: value.__setitem__("unknown", True), lambda value: value.__setitem__("authority_scope", "foreign"),
    lambda value: value.__setitem__("production_runtime_effect", True), lambda value: value.__setitem__("scanner_runtime_effect", True),
    lambda value: value["accepted_partition"]["candidate_tile_judgments"].append({"foreign": True}), lambda value: value["accepted_candidate"].__setitem__("source_scan_id", "foreign"),
    lambda value: value["assessment"].__setitem__("sv9_score", True), lambda value: value["assessment"].__setitem__("sv9_score", 2.0),
    lambda value: value.__setitem__("score", True), lambda value: value.__setitem__("score", 2.0), lambda value: value["active_authority_event"].__setitem__("event_fingerprint", _H(99)),
))
def test_wrapper_schema_and_duplicate_facts_fail_closed(change):
    result = _projection(); change(result)
    with pytest.raises(authority_projection.EvidenceVaultSv9AuthorityProjectionError): authority_projection.validate_evidence_vault_sv9_authority_projection(result)

def test_valid_foreign_active_event_cannot_rebind_the_accepted_candidate():
    result = _projection(); foreign = _candidate(99, "foreign", result["accepted_candidate"]["current_series_fingerprint"])
    event = _event("supersede", 99, candidate=foreign, current=foreign["current_series_fingerprint"], source="foreign", predecessor=result["active_authority_event"], active=result["active_authority_event"])
    result["active_authority_event"] = result["current_head"] = event
    with pytest.raises(authority_projection.EvidenceVaultSv9AuthorityProjectionError): authority_projection.validate_evidence_vault_sv9_authority_projection(result)

def test_consecutive_reopen_keeps_active_parent_and_newest_overlay():
    result = _projection(pending=True, consecutive=True); head, active = result["current_head"], result["active_authority_event"]
    assert head["predecessor_event_id"] != active["event_id"] and head["predecessor_event_fingerprint"] != active["event_fingerprint"] and head["request"]["expected_predecessor_event_fingerprint"] == head["predecessor_event_fingerprint"] and head["active_parent_event_id"] == active["event_id"] and head["active_parent_event_fingerprint"] == active["event_fingerprint"] and head["request"]["source_scan_id"] == "review-4"
    first = _facts(pending=True, consecutive=True)[5]
    assert first["delta_fingerprint"] != result["reopen_review_overlay"]["delta_fingerprint"]
    result["reopen_review_overlay"] = first
    with pytest.raises(authority_projection.EvidenceVaultSv9AuthorityProjectionError): authority_projection.validate_evidence_vault_sv9_authority_projection(result)

@pytest.mark.parametrize("change", (
    lambda value: value["reopen_review_overlay"].pop("review_state"), lambda value: value["reopen_review_overlay"].__setitem__("unknown", True),
    lambda value: value["reopen_review_overlay"].__setitem__("review_state", "resolved"), lambda value: value["reopen_review_overlay"].__setitem__("delta_fingerprint", _H(99)),
    lambda value: value["current_head"]["request"].__setitem__("source_scan_id", "foreign"), lambda value: value["current_head"]["request"].__setitem__("delta_fingerprint", _H(99)),
    lambda value: value["current_head"].__setitem__("active_parent_event_id", _U(99)), lambda value: value["current_head"].__setitem__("active_parent_event_fingerprint", _H(99)),
))
def test_pending_reopen_bindings_fail_closed(change):
    result = _projection(pending=True); change(result)
    with pytest.raises(authority_projection.EvidenceVaultSv9AuthorityProjectionError): authority_projection.validate_evidence_vault_sv9_authority_projection(result)

def test_stable_wrapper_rejects_a_valid_nonactive_head():
    result = _projection(older=True); result["current_head"] = result["event"]
    with pytest.raises(authority_projection.EvidenceVaultSv9AuthorityProjectionError): authority_projection.validate_evidence_vault_sv9_authority_projection(result)

@pytest.mark.parametrize("kwargs", ({}, {"pending": True}))
def test_generic_validator_allows_null_event_timestamps_before_persistence(kwargs):
    result = _projection(**kwargs)
    for name in ("active_authority_event", "current_head", "event"): result[name]["created_at"] = None
    assert authority_projection.validate_evidence_vault_sv9_authority_projection(result) == result

@pytest.mark.parametrize("kwargs", ({}, {"pending": True, "consecutive": True}, {"older": True}))
def test_persisted_validator_accepts_stable_pending_and_older_selected_occurrences(kwargs):
    result = _projection(**kwargs)
    if kwargs.get("older"): assert result["event"] != result["current_head"]
    assert authority_projection.validate_persisted_evidence_vault_sv9_authority_projection(result) == json.loads(json.dumps(result))

@pytest.mark.parametrize("name", ("active_authority_event", "current_head", "event"))
def test_persisted_validator_rejects_each_null_event_timestamp_independently(name):
    result = _projection(pending=True); result[name]["created_at"] = None
    assert authority_projection.validate_evidence_vault_sv9_authority_projection(result) == result
    with pytest.raises(authority_projection.EvidenceVaultSv9AuthorityProjectionError): authority_projection.validate_persisted_evidence_vault_sv9_authority_projection(result)

def test_generic_validator_rejects_missing_event_timestamp_field():
    result = _projection(pending=True); result["event"].pop("created_at")
    with pytest.raises(authority_projection.EvidenceVaultSv9AuthorityProjectionError): authority_projection.validate_evidence_vault_sv9_authority_projection(result)
