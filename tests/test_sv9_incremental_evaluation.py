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
        rows = [{"tile_id": row["tile_id"], "assessment_state": "ok" if row["evidence"] else "sin_evidencia", "supporting_evidence": [{key: evidence[key] for key in ("evidence_ref", "evidence_fingerprint")} for evidence in row["evidence"]]} for row in request["requested_tiles"]]
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


from tests.test_evidence_vault_sv9_workset_partition import (
    _build as _build_workset_partition, _delta as _partition_delta, _input as _partition_input,
    _prior as _partition_prior, _sentinel as _partition_sentinel,
)


def _partitioned(kind="canonical"):
    extras, hints, sin_evidencia = (), (), False
    if kind in {"hint", "both", "sentinel"}: extras, hints = ("hinted",), (("hint", "M1", "hinted"),)
    if kind == "blocked": extras, hints = ("hinted", "pending"), (("hint", "M1", "hinted"),)
    if kind == "zero_pending": extras = ("pending",)
    if kind == "sentinel": sin_evidencia = True
    source = _partition_input(extras=extras, hints=hints, sin_evidencia=sin_evidencia)
    prior = _partition_prior(source, without=("mission",)) if kind in {"canonical", "both", "sentinel"} else None
    sentinels = [_partition_sentinel(source)] if kind == "sentinel" else ()
    return _build_workset_partition(source, _partition_delta(source, prior=prior, sentinels=sentinels))


def _resolved(workset):
    ids = {binding["evidence_record_id"] for tile in workset["healthy_workset"]["tiles"] for binding in tile["current_evidence_bindings"]}
    return [{key: row[key] for key in ("evidence_record_id", "evidence_ref", "evidence_fingerprint")} | {"content": {"record": row["evidence_record_id"]}} for row in workset["evaluation_input"]["current_identity_bindings"] if row["evidence_record_id"] in ids]


def _persisted():
    rows = []
    def persist(request, evaluation, judgments, sentinel):
        rows.append((deepcopy(request), deepcopy(evaluation), deepcopy(judgments), deepcopy(sentinel)))
    return rows, persist


def _lookup(rows):
    values = {(request["plan_fingerprint"], request["canonical_request_fingerprint"]): evaluation for request, evaluation, _judgments, _sentinel in rows}
    return lambda request: values.get((request["plan_fingerprint"], request["canonical_request_fingerprint"]))


def test_partial_executor_persists_only_validated_prefix_without_score_authority():
    workset, flow, stored = _partitioned(), _Flow("second_failure"), None
    stored, persist = _persisted()
    result = ie.execute_partial_incremental_evaluation(workset, _resolved(workset), flow, persist_evaluation=persist)
    request, evaluation, judgments, sentinel = stored[0]
    assert result["status"] == "provider_failure" and len(stored) == 1 and [row["component_key"] for row in flow.calls] == ["mission", "coherencia"]
    assert set(request) == ie._REQUEST_FIELDS and request["schema_version"] == ie.COMPONENT_REQUEST_VERSION and "workset_partition_fingerprint" not in request
    assert all(set(call) == ie._REQUEST_FIELDS and call["schema_version"] == ie.COMPONENT_REQUEST_VERSION and "workset_partition_fingerprint" not in call for call in flow.calls)
    assert evaluation == result["captured_calls"][0]["evaluation"] and len(judgments) == 5 and sentinel is None
    assert not {"assessment", "score", "candidate", "adoption", "publication", "accepted_authority", "workset_partition_fingerprint"} & set(result)


def test_partial_executor_recovers_per_request_before_flow_and_rebuilds_coherencia_identically():
    workset, first_flow = _partitioned(), _Flow("second_failure")
    stored, persist = _persisted()
    ie.execute_partial_incremental_evaluation(workset, _resolved(workset), first_flow, persist_evaluation=persist)
    uninterrupted = _Flow(); ie.execute_partial_incremental_evaluation(workset, _resolved(workset), uninterrupted)
    resumed_flow, resumed_stored = _Flow(), _persisted()
    result = ie.execute_partial_incremental_evaluation(workset, _resolved(workset), resumed_flow, lookup_evaluation=_lookup(stored), persist_evaluation=resumed_stored[1])
    assert result["status"] == "partial" and result["call_count"] == 1 and [row["component_key"] for row in resumed_flow.calls] == ["coherencia"]
    assert resumed_flow.calls[0]["upstream_candidate_state"] == uninterrupted.calls[-1]["upstream_candidate_state"]
    assert resumed_flow.calls[0]["canonical_request_fingerprint"] == uninterrupted.calls[-1]["canonical_request_fingerprint"]
    assert len(resumed_stored[0]) == 1 and resumed_stored[0][0][0]["component_key"] == "coherencia"


def test_partial_executor_propagates_persistence_failure_before_next_flow_call():
    workset, flow = _partitioned(), _Flow()
    with pytest.raises(RuntimeError):
        ie.execute_partial_incremental_evaluation(workset, _resolved(workset), flow, persist_evaluation=lambda *_args: (_ for _ in ()).throw(RuntimeError("persist")))
    assert [row["component_key"] for row in flow.calls] == ["mission"]


def test_partial_executor_rejects_tampered_recovered_evaluation_through_acceptance():
    workset, seed_flow = _partitioned(), _Flow("second_failure")
    stored, persist = _persisted(); ie.execute_partial_incremental_evaluation(workset, _resolved(workset), seed_flow, persist_evaluation=persist)
    request, evaluation, _judgments, _sentinel = stored[0]
    tampered = ie.build_component_evaluation(component_key=evaluation["component_key"], series_fingerprint=evaluation["series_fingerprint"], request_fingerprint=_hash(5), status=evaluation["status"], tile_results=evaluation["tile_results"])
    flow = _Flow(); result = ie.execute_partial_incremental_evaluation(workset, _resolved(workset), flow, lookup_evaluation=lambda candidate: tampered if candidate["component_key"] == request["component_key"] else None)
    assert result["status"] == "provider_failure" and result["call_count"] == 0 and not result["captured_calls"] and not flow.calls


def test_partial_executor_detaches_lookup_request_before_recovery():
    workset, stored = _partitioned(), None
    stored, persist = _persisted(); ie.execute_partial_incremental_evaluation(workset, _resolved(workset), _Flow("second_failure"), persist_evaluation=persist)
    expected_flow = _Flow(); expected = ie.execute_partial_incremental_evaluation(workset, _resolved(workset), expected_flow)
    def lookup(request):
        if request["component_key"] == "mission":
            request["canonical_request_fingerprint"] = _hash(1); request["requested_tiles"][0]["evidence"] = []
            return stored[0][1]
        return None
    flow = _Flow(); result = ie.execute_partial_incremental_evaluation(workset, _resolved(workset), flow, lookup_evaluation=lookup)
    assert flow.calls == [expected_flow.calls[-1]]
    assert result["captured_calls"] == expected["captured_calls"] and result["evaluated_tile_judgments"] == expected["evaluated_tile_judgments"]


def test_partial_executor_detaches_persistence_material_before_coherencia():
    workset = _partitioned()
    expected_flow = _Flow("not_detected"); expected = ie.execute_partial_incremental_evaluation(workset, _resolved(workset), expected_flow)
    def persist(request, evaluation, judgments, sentinel):
        request["canonical_request_fingerprint"] = _hash(1); evaluation["request_fingerprint"] = _hash(2)
        if judgments: judgments[0]["assessment_state"] = "no"
        if sentinel: sentinel["canonical_component_sentinel_fingerprint"] = _hash(3)
    flow = _Flow("not_detected"); result = ie.execute_partial_incremental_evaluation(workset, _resolved(workset), flow, persist_evaluation=persist)
    assert flow.calls == expected_flow.calls and result["captured_calls"] == expected["captured_calls"]
    assert result["evaluated_tile_judgments"] == expected["evaluated_tile_judgments"]
    assert result["evaluated_component_sentinels"] == expected["evaluated_component_sentinels"]
