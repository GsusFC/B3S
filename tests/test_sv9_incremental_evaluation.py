from copy import deepcopy
import json

import pytest

from src.sv9 import assessment_kernel as kernel
from src.sv9 import incremental_evaluation as ie
from src.sv9 import incremental_planner as ip
from src.sv9 import judgment_memory as jm
from tests.test_sv9_judgment_memory import _judgment, _origin, _series


# fmt: off
def _hash(number): return f"{number:064x}"
def _delta(tile="M1", disposition="relevant"):
    return jm.build_tile_evidence_delta_projection(tile_id=tile, component_key=dict(ip._REGISTRY)[tile], disposition=disposition, evidence=[{"evidence_ref": f"delta:{tile}", "evidence_fingerprint": _hash(91)}], capture_origin=_origin("capture", 92), operation_origin=_origin("operation", 93))
def _packets(plan):
    deltas, result = {row["tile_id"]: row for row in plan["delta_projections"]}, []
    for component in plan["component_workset"]:
        ids = [tile for tile in plan["tile_workset"] if ie._BY_TILE[tile][1] == component]; bound = next((deltas[tile] for tile in ids if tile in deltas), None)
        evidence = lambda tile: bound["evidence"] if tile in deltas else [{"evidence_ref": f"e:{tile}", "evidence_fingerprint": _hash(sum(map(ord, tile)))}]
        result.append(ie.build_evidence_packet(component_key=component, tiles=[{"tile_id": tile, "evidence": [{**row, "content": {"tile": tile}} for row in evidence(tile)]} for tile in ids], capture_origin=bound["capture_origin"] if bound else _origin("capture", 1), operation_origin=bound["operation_origin"] if bound else _origin("operation", 2), series_fingerprint=plan["current_series_fingerprint"]))
    return result
class _Flow:
    def __init__(self, mode="ok"): self.mode, self.calls = mode, []
    def evaluate_component(self, request):
        self.calls.append(request)
        if self.mode == "exception": raise RuntimeError("SECRET_REQUEST SECRET_EVIDENCE SECRET_PROVIDER_RESPONSE")
        if self.mode == "failure" or (self.mode == "second_failure" and len(self.calls) == 2): return ie.ComponentEvaluationOutcome.provider_failure()
        if self.mode == "mixed": return ie.ComponentEvaluationOutcome({}, "provider_failure")
        if self.mode == "empty": return ie.ComponentEvaluationOutcome()
        rows = [{"tile_id": row["tile_id"], "assessment_state": "ok", "supporting_evidence": [{key: evidence[key] for key in ("evidence_ref", "evidence_fingerprint")} for evidence in row["evidence"]]} for row in request["requested_tiles"]]
        rows, status = ([], "not_detected") if self.mode == "not_detected" and request["component_key"] == "mission" else (rows, "evaluated")
        if self.mode == "missing": rows = rows[:-1]
        if self.mode == "duplicate": rows.append(deepcopy(rows[0]))
        if self.mode == "extra": rows.append({**deepcopy(rows[0]), "tile_id": next(tile for tile in ie._COMPONENT_TILES[request["component_key"]] if tile not in {row["tile_id"] for row in rows})})
        if self.mode == "evidence": rows[0]["supporting_evidence"][0]["evidence_fingerprint"] = _hash(99)
        raw = [] if self.mode == "malformed" else ie.build_component_evaluation(component_key=request["component_key"], series_fingerprint=request["candidate_series_fingerprint"] if self.mode == "series" else request["current_series_fingerprint"], request_fingerprint=_hash(97) if self.mode == "request" else request["canonical_request_fingerprint"], status=status, tile_results=rows)
        return raw if self.mode == "legacy" else ie.ComponentEvaluationOutcome.success(raw)
def _prior(): return [_judgment(tile_id=tile, component_key=component) for tile, component in ip._REGISTRY]
def _capacity(result): return len(result["candidate_tile_judgments"]) + sum(len(ip._COMPONENT_TILES[row["component_key"]]) for row in result["candidate_component_sentinels"])
def _sentinel(component="mission", **changes):
    reference = next(row for row in _prior() if row["component_key"] == component)
    value = {"component_key": component, "status": "not_detected", "supporting_evidence": [], "capture_origin": reference["capture_origin"], "operation_origin": reference["operation_origin"], "series_contract": reference["series_contract"], "authority_state": "accepted", "review_state": "none", "lifecycle_state": "active", "lifecycle_reason": ""}
    return ip.build_component_not_detected_sentinel(**(value | changes))
def test_first_execution_binds_evidence_calls_coherencia_last_and_replays():
    plan, flow = ip.build_incremental_plan([], [], _series()), _Flow(); packets = _packets(plan)
    assert plan["candidate_series_fingerprint"] != plan["current_series_fingerprint"]
    result = ie.execute_incremental_evaluation(plan, packets, flow)
    assert (result["status"], result["call_count"], result["calls_avoided"], result["reused_tile_count"], result["evaluated_tile_count"]) == ("available", 10, 0, 0, 80)
    assert [request["component_key"] for request in flow.calls] == list(ip._COMPONENT_TILES)
    assert not flow.calls[0]["upstream_candidate_state"] and len(flow.calls[-1]["upstream_candidate_state"]) == 70
    assert result["assessment"]["sv9_score"] == 100 and len(result["candidate_tile_judgments"]) == 80
    replay = ie.replay_incremental_evaluation(plan, packets, result["captured_calls"])
    assert ([row["canonical_judgment_fingerprint"] for row in replay["candidate_tile_judgments"]], replay["assessment"]["assessment_fingerprint"], replay["assessment"]["score_fingerprint"]) == ([row["canonical_judgment_fingerprint"] for row in result["candidate_tile_judgments"]], result["assessment"]["assessment_fingerprint"], result["assessment"]["score_fingerprint"])
    tampered = deepcopy(result["captured_calls"]); tampered[0]["request"]["plan_fingerprint"] = _hash(5)
    assert ie.replay_incremental_evaluation(plan, packets, tampered)["status"] == "pending"
    evidence = deepcopy(packets); evidence[0]["tiles"][0]["evidence"][0]["content"] = {"tampered": True}
    assert ie.replay_incremental_evaluation(plan, evidence, result["captured_calls"])["status"] == "pending"
    assert ie.replay_incremental_evaluation(ip.build_incremental_plan([], [], _series(prompt_version="p2")), packets, result["captured_calls"])["status"] == "pending"
def test_delta_preserves_frozen_memory_and_zero_workset_reuses_kernel():
    plan = ip.build_incremental_plan(_prior(), [_delta()], _series()); frozen, flow = deepcopy(plan), _Flow(); result = ie.execute_incremental_evaluation(plan, _packets(plan), flow)
    assert result["status"] == "available" and [call["component_key"] for call in flow.calls] == ["mission", "coherencia"]
    assert _capacity(result) == 80 and plan == frozen
    reused = ip.build_incremental_plan(_prior(), [], _series()); zero = ie.execute_incremental_evaluation(reused, [], _Flow("exception"))
    expected = kernel.build_sv9_assessment([{"component_key": component, "tile_id": tile, "tile_key": f"{component}.{tile}", "assessment_state": "ok"} for tile, component in ip._REGISTRY])
    assert zero["status"] == "available" and (zero["call_count"], zero["calls_avoided"], zero["reused_tile_count"], zero["evaluated_tile_count"], _capacity(zero)) == (0, 10, 80, 0, 80) and zero["assessment"] == expected and zero["candidate_tile_judgments"] == _prior()
def test_full_sentinel_and_review_compose_while_partial_registry_fails_closed():
    first = ip.build_incremental_plan([], [], _series()); packets = _packets(first); sentinel = ie.execute_incremental_evaluation(first, packets, _Flow("not_detected")); replay = ie.replay_incremental_evaluation(first, packets, sentinel["captured_calls"])
    assert sentinel["status"] == "available" and len(sentinel["candidate_component_sentinels"]) == 1 and sentinel["assessment"]["tile_count"] == 75
    assert ([row["canonical_component_sentinel_fingerprint"] for row in replay["candidate_component_sentinels"]], replay["assessment"]["assessment_fingerprint"], replay["assessment"]["score_fingerprint"]) == ([row["canonical_component_sentinel_fingerprint"] for row in sentinel["candidate_component_sentinels"]], sentinel["assessment"]["assessment_fingerprint"], sentinel["assessment"]["score_fingerprint"])
    partial = ip.build_incremental_plan(_prior(), [_delta()], _series())
    assert ie.execute_incremental_evaluation(partial, _packets(partial), _Flow("not_detected"))["status"] == "pending"
    review = ip.build_incremental_plan(_prior(), [_delta(disposition="human_review_required")], _series()); blocked = _Flow()
    assert ie.execute_incremental_evaluation(review, [], blocked)["status"] == "available" and not blocked.calls
    incomplete = ip.build_incremental_plan([], [], _series(), registry_tile_ids=["M1"]); blocked = _Flow()
    assert ie.execute_incremental_evaluation(incomplete, [], blocked)["status"] == "pending" and not blocked.calls
@pytest.mark.parametrize("mode", "exception failure empty mixed legacy malformed missing duplicate extra series request evidence".split())
def test_provider_and_signed_result_failures_are_atomic_pending(mode):
    plan = ip.build_incremental_plan(_prior(), [_delta()], _series()) if mode == "extra" else ip.build_incremental_plan([], [], _series()); flow = _Flow(mode); result = ie.execute_incremental_evaluation(plan, _packets(plan), flow)
    assert result["status"] == "pending" and result["reason_code"] == "provider_failure" and result["assessment"] is None and not result["candidate_tile_judgments"] and not result["captured_calls"] and result["call_count"] == 1 and result["evaluated_tile_count"] == 0 and "SECRET_" not in json.dumps(result)
def test_second_component_failure_discards_prior_outcome_atomically():
    plan, frozen, flow = ip.build_incremental_plan(_prior(), [_delta()], _series()), None, _Flow("second_failure")
    frozen = deepcopy(plan); result = ie.execute_incremental_evaluation(plan, _packets(plan), flow)
    assert plan == frozen and (result["status"], result["reason_code"], result["call_count"], result["assessment"], result["candidate_tile_judgments"], result["captured_calls"]) == ("pending", "provider_failure", 2, None, [], [])


def test_same_series_review_freezes_authority_while_safe_work_and_rollover_execute():
    prior = _prior()
    review = ip.build_incremental_plan(prior, [_delta("M1", "contradiction"), _delta("A1")], _series()); flow = _Flow()
    result = ie.execute_incremental_evaluation(review, _packets(review), flow)
    assert [row["component_key"] for row in flow.calls] == ["attributes", "coherencia"] and _capacity(result) == 80
    assert next(row for row in result["candidate_tile_judgments"] if row["tile_id"] == "M1") == prior[0]
    rollover = ip.build_incremental_plan(prior, [_delta("M1", "human_review_required")], _series(prompt_version="p2")); flow = _Flow()
    result = ie.execute_incremental_evaluation(rollover, _packets(rollover), flow)
    rows = [*result["candidate_tile_judgments"], *result["candidate_component_sentinels"]]
    assert len(flow.calls) == 10 and _capacity(result) == 80 and not result["candidate_component_sentinels"]
    assert all(row["authority_state"] == "pending" and row["series_fingerprint"] == rollover["current_series_fingerprint"] for row in rows)


def test_sentinel_reuse_and_reopen_compose_complete_candidates():
    prior, sentinel = [row for row in _prior() if row["component_key"] != "mission"], _sentinel()
    reused = ie.execute_incremental_evaluation(ip.build_incremental_plan(prior, [], _series(), prior_component_sentinels=[sentinel]), [], _Flow("exception"))
    assert reused["candidate_component_sentinels"] == [sentinel] and _capacity(reused) == 80 and reused["call_count"] == 0
    plan = ip.build_incremental_plan(prior, [_delta()], _series(), prior_component_sentinels=[sentinel]); flow = _Flow()
    reopened = ie.execute_incremental_evaluation(plan, _packets(plan), flow)
    assert [row["component_key"] for row in flow.calls] == ["mission", "coherencia"] and _capacity(reopened) == 80 and not reopened["candidate_component_sentinels"]


@pytest.mark.parametrize("kind", ["tile", "sentinel"])
def test_invalid_or_foreign_frozen_authority_fails_before_flow_calls(kind):
    if kind == "tile":
        plan = ip.build_incremental_plan(_prior(), [], _series())
        plan["prior_judgments"][0] = _judgment(authority_state="pending")
    else:
        prior = [row for row in _prior() if row["component_key"] != "mission"]
        plan = ip.build_incremental_plan(prior, [], _series(), prior_component_sentinels=[_sentinel()])
        plan["prior_component_sentinels"][0] = _sentinel(series_contract=_series(prompt_version="p2"))
    with pytest.raises(ie.IncrementalEvaluationError): ie._frozen(plan, set())
    flow = _Flow(); result = ie.execute_incremental_evaluation(plan, [], flow)
    assert (result["status"], result["reason_code"], result["assessment"], result["candidate_tile_judgments"], result["candidate_component_sentinels"], flow.calls) == ("pending", "invalid_input", None, [], [], [])


def test_composition_rejects_overlap_mixed_series_and_missing_capacity():
    full, empty = ip.build_incremental_plan(_prior(), [], _series()), ip.build_incremental_plan([], [], _series())
    mixed = {row["tile_id"]: row for row in _prior()}; mixed["M1"] = _judgment(series=_series(prompt_version="p2"))
    for plan, judgments, sentinels in ((full, {"M1": _prior()[0]}, {}), (full, {}, {"mission": _sentinel()}), (empty, {}, {})):
        with pytest.raises(ie.IncrementalEvaluationError): ie._assessment(plan, set(), judgments, sentinels)
    with pytest.raises(ie.IncrementalEvaluationError): ie._assessment(empty, set(empty["tile_workset"]), mixed, {})
# fmt: on
