from copy import deepcopy

import pytest

from src.sv9 import incremental_planner as ip
from src.sv9 import judgment_memory as jm
from tests.test_sv9_judgment_memory import _delta, _judgment, _series


def judgment(tile="M1", **changes):
    return (
        _judgment(**changes)
        if tile == "M1"
        else _judgment(tile_id=tile, component_key=dict(ip._REGISTRY)[tile], **changes)
    )


def delta(tile="M1", disposition="relevant", *numbers):
    return _delta(
        tile_id=tile,
        component_key=dict(ip._REGISTRY)[tile],
        disposition=disposition,
        evidence=[{"evidence_ref": f"evidence:{n}", "evidence_fingerprint": f"{n:064x}"} for n in (numbers or (1,))],
    )


def plan(prior=(), deltas=(), *, ids=("M1",), current=None):
    return ip.build_incremental_plan(list(prior), list(deltas), current or _series(), registry_tile_ids=list(ids))


def resign(value):
    unsigned = dict(value)
    del unsigned["canonical_plan_fingerprint"]
    value["canonical_plan_fingerprint"] = jm.canonical_fingerprint(ip.PLAN_FINGERPRINT_NAMESPACE, unsigned)
    return value


def test_reuse_and_irrelevant_are_frozen_and_replay_stable():
    first = plan([judgment()], current=_series())
    unchanged = plan([judgment()], [delta("M1", "irrelevant", 9)])
    assert first["items"][0]["action"] == "reuse_canonical" and first["expected_calls"] == 0
    assert unchanged["items"][0]["action"] == "unaffected" and unchanged["frozen_set"] == ["M1"]
    assert ip.validate_incremental_plan(first) == first
    assert ip.canonical_plan_json(first) == ip.canonical_plan_json(plan([judgment()], current=_series()))


def test_resolved_review_is_reusable_when_active_and_accepted():
    result = plan([judgment(review_state="resolved")])
    assert result["items"][0]["action"] == "reuse_canonical" and result["expected_calls"] == 0


def test_frozen_set_uses_global_registry_order():
    result = plan([judgment("M1"), judgment("M2")], [delta("M1", "irrelevant", 9)], ids=("M1", "M2"))
    assert result["reuse_set"] == ["M2"] and result["unaffected_set"] == ["M1"]
    assert result["frozen_set"] == ["M1", "M2"]


def test_relevant_deltas_count_tiles_once_per_component():
    priors = [judgment("M1"), judgment("M2"), judgment("A1")]
    deltas = [delta("A1", "relevant", 8), delta("M2", "relevant", 7), delta("M1", "relevant", 6)]
    result = plan(priors, deltas, ids=("A1", "M2", "M1"))
    assert result["tile_workset"] == ["M1", "M2", "A1"]
    assert result["component_workset"] == ["mission", "attributes"] and result["expected_calls"] == 2


def test_contradiction_preserves_prior_and_never_calls():
    prior = judgment()
    snapshot = deepcopy(prior)
    result = plan([prior], [delta("M1", "contradiction", 8)])
    item = result["items"][0]
    assert item["action"] == "reopen_contradiction" and item["expected_call_contribution"] is False
    assert item["prior_judgment_fingerprint"] == prior["canonical_judgment_fingerprint"] and prior == snapshot


def test_contract_change_is_a_new_candidate_series():
    result = plan([judgment()], current=_series(prompt_version="prompt-2"))
    item = result["items"][0]
    assert item["action"] == "reopen_contract_change" and result["expected_calls"] == 1
    assert result["predecessor_series_fingerprints"] == [judgment()["series_fingerprint"]]


def test_first_execution_uses_all_tiles_and_unique_component_calls():
    result = ip.build_incremental_plan([], [], _series())
    assert len(result["tile_workset"]) == 80 and result["expected_calls"] == 10 and result["calls_avoided"] == 0


def test_order_invariant_and_fail_closed_inputs():
    left = plan([judgment("M1"), judgment("A1")], [delta("A1", "relevant", 8)], ids=("A1", "M1"))
    right = plan([judgment("A1"), judgment("M1")], [delta("A1", "relevant", 8)], ids=("M1", "A1"))
    assert left == right
    with pytest.raises(ip.IncrementalPlannerError):
        plan([judgment(), judgment()])
    with pytest.raises(ip.IncrementalPlannerError):
        plan(ids=("BAD",))

    class TileId(str):
        pass

    with pytest.raises(ip.IncrementalPlannerError):
        plan(ids=(TileId("M1"),))
    tampered = deepcopy(left)
    tampered["items"][0]["reason"] = "tampered"
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(tampered)

    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(type("PlanDict", (dict,), {})(left))


def test_signed_replay_rejects_resigned_semantic_tampering():
    changed = resign(deepcopy(plan([judgment()])))
    changed["items"][0]["action"] = "unaffected"
    changed["items"][0]["reason"] = "irrelevant_evidence"
    resign(changed)
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(changed)
    changed = resign(deepcopy(plan([judgment()])))
    changed["items"][0]["reused_judgment_fingerprint"] = "0" * 64
    resign(changed)
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(changed)


def test_signed_replay_rejects_container_subclasses_and_malformed_rows():
    changed = plan([judgment()])
    changed["items"] = type("Rows", (list,), {})(changed["items"])
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(changed)


def test_signed_replay_rejects_deep_json_without_recursion_error():
    changed = plan([judgment()])
    nested = []
    for _ in range(1101):
        nested = [nested]
    changed["predecessor_series_fingerprints"] = nested
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(changed)
    changed = plan([judgment()])
    changed["predecessor_series_fingerprints"] = ["0" * 64, []]
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(changed)


def test_signed_replay_rejects_bound_memory_not_matching_items():
    changed = plan([judgment()], [delta("M1", "relevant", 8)])
    changed["prior_judgments"] = []
    resign(changed)
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(changed)
    changed = plan([judgment()], [delta("M1", "relevant", 8)])
    changed["delta_projections"] = []
    resign(changed)
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(changed)
    changed = plan([judgment()])
    changed["items"][0] = []
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(changed)
    changed = plan([judgment()])
    changed["prior_judgments"][0] = type("Judgment", (dict,), {})(changed["prior_judgments"][0])
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(changed)


@pytest.mark.parametrize(
    ("prior", "projection", "expected"),
    [
        ([judgment()], delta("M1", "unchanged", 9), "human_review_required"),
        ([judgment()], delta("M1", "relevant", 3), "human_review_required"),
        ([], delta("M1", "contradiction", 9), "human_review_required"),
    ],
)
def test_inconsistent_projections_require_review(prior, projection, expected):
    assert plan(prior, [projection])["items"][0]["action"] == expected
