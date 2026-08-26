from copy import deepcopy

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
        if self.mode == "exception": raise RuntimeError("provider")
        rows = [{"tile_id": row["tile_id"], "assessment_state": "ok", "supporting_evidence": [{key: evidence[key] for key in ("evidence_ref", "evidence_fingerprint")} for evidence in row["evidence"]]} for row in request["requested_tiles"]]
        rows, status = ([], "not_detected") if self.mode == "not_detected" and request["component_key"] == "mission" else (rows, "evaluated")
        if self.mode == "missing": rows = rows[:-1]
        if self.mode == "duplicate": rows.append(deepcopy(rows[0]))
        if self.mode == "extra": rows.append({**deepcopy(rows[0]), "tile_id": next(tile for tile in ie._COMPONENT_TILES[request["component_key"]] if tile not in {row["tile_id"] for row in rows})})
        if self.mode == "evidence": rows[0]["supporting_evidence"][0]["evidence_fingerprint"] = _hash(99)
        return [] if self.mode == "malformed" else ie.build_component_evaluation(component_key=request["component_key"], series_fingerprint=request["candidate_series_fingerprint"] if self.mode == "series" else request["current_series_fingerprint"], request_fingerprint=_hash(97) if self.mode == "request" else request["canonical_request_fingerprint"], status=status, tile_results=rows)
def _prior(): return [_judgment(tile_id=tile, component_key=component) for tile, component in ip._REGISTRY]
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
    assert len(result["candidate_tile_judgments"]) == 11 and plan == frozen
    reused = ip.build_incremental_plan(_prior(), [], _series()); zero = ie.execute_incremental_evaluation(reused, [], _Flow("exception"))
    expected = kernel.build_sv9_assessment([{"component_key": component, "tile_id": tile, "tile_key": f"{component}.{tile}", "assessment_state": "ok"} for tile, component in ip._REGISTRY])
    assert zero["status"] == "available" and (zero["call_count"], zero["calls_avoided"], zero["reused_tile_count"], zero["evaluated_tile_count"]) == (0, 10, 80, 0) and zero["assessment"] == expected
def test_full_sentinel_and_review_or_partial_registry_fail_closed_before_calls():
    first = ip.build_incremental_plan([], [], _series()); packets = _packets(first); sentinel = ie.execute_incremental_evaluation(first, packets, _Flow("not_detected")); replay = ie.replay_incremental_evaluation(first, packets, sentinel["captured_calls"])
    assert sentinel["status"] == "available" and len(sentinel["candidate_component_sentinels"]) == 1 and sentinel["assessment"]["tile_count"] == 75
    assert ([row["canonical_component_sentinel_fingerprint"] for row in replay["candidate_component_sentinels"]], replay["assessment"]["assessment_fingerprint"], replay["assessment"]["score_fingerprint"]) == ([row["canonical_component_sentinel_fingerprint"] for row in sentinel["candidate_component_sentinels"]], sentinel["assessment"]["assessment_fingerprint"], sentinel["assessment"]["score_fingerprint"])
    partial = ip.build_incremental_plan(_prior(), [_delta()], _series())
    assert ie.execute_incremental_evaluation(partial, _packets(partial), _Flow("not_detected"))["status"] == "pending"
    review = ip.build_incremental_plan(_prior(), [_delta(disposition="human_review_required")], _series()); blocked = _Flow()
    assert ie.execute_incremental_evaluation(review, [], blocked)["status"] == "pending" and not blocked.calls
    incomplete = ip.build_incremental_plan([], [], _series(), registry_tile_ids=["M1"]); blocked = _Flow()
    assert ie.execute_incremental_evaluation(incomplete, [], blocked)["status"] == "pending" and not blocked.calls
@pytest.mark.parametrize("mode", "exception malformed missing duplicate extra series request evidence".split())
def test_provider_and_signed_result_failures_are_atomic_pending(mode):
    plan = ip.build_incremental_plan(_prior(), [_delta()], _series()) if mode == "extra" else ip.build_incremental_plan([], [], _series()); flow = _Flow(mode); result = ie.execute_incremental_evaluation(plan, _packets(plan), flow)
    assert result["status"] == "pending" and result["assessment"] is None and not result["candidate_tile_judgments"] and not result["captured_calls"] and result["call_count"] == 1 and result["evaluated_tile_count"] == 0
# fmt: on
