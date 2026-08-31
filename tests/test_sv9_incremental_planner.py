from copy import deepcopy

import pytest

from src.sv9 import incremental_planner as ip
from src.sv9 import judgment_memory as jm
from tests.test_sv9_judgment_memory import _delta, _judgment, _series


_PLAN_ACTIONS = frozenset(
    {
        "reuse_canonical",
        "evaluate_new",
        "evaluate_delta",
        "reopen_contradiction",
        "reopen_contract_change",
        "human_review_required",
        "unaffected",
    }
)


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


def plan(prior=(), deltas=(), *, ids=("M1",), current=None, sentinels=()):
    return ip.build_incremental_plan(
        list(prior),
        list(deltas),
        current or _series(),
        registry_tile_ids=list(ids),
        prior_component_sentinels=list(sentinels),
    )


def component_ids(*components):
    return tuple(tile for component in components for tile in ip._COMPONENT_TILES[component])


def component_judgments(component):
    return [judgment(tile) for tile in ip._COMPONENT_TILES[component]]


def sentinel(component="mission", **changes):
    reference = judgment(ip._COMPONENT_TILES[component][0])
    value = {
        "component_key": component,
        "status": "not_detected",
        "supporting_evidence": [],
        "capture_origin": reference["capture_origin"],
        "operation_origin": reference["operation_origin"],
        "series_contract": reference["series_contract"],
        "authority_state": "accepted",
        "review_state": "none",
        "lifecycle_state": "active",
        "lifecycle_reason": "",
    }
    return ip.build_component_not_detected_sentinel(**(value | changes))


def resign(value):
    unsigned = dict(value)
    del unsigned["canonical_plan_fingerprint"]
    namespace = (
        ip.LEGACY_PLAN_FINGERPRINT_NAMESPACE
        if value["schema_version"] == ip.LEGACY_PLAN_VERSION
        else ip.PLAN_FINGERPRINT_NAMESPACE
    )
    value["canonical_plan_fingerprint"] = jm.canonical_fingerprint(namespace, unsigned)
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


def test_v2_replays_per_tile_rollover_while_v3_reopens_the_complete_registry():
    current = _series(prompt_version="prompt-2")
    prior = [judgment(tile) for tile, _component in ip._REGISTRY]
    deltas = [delta("M1", "human_review_required", 9), delta("A1", "contradiction", 10)]
    legacy = ip.build_incremental_plan_v2(prior, deltas, current)
    latest = ip.build_incremental_plan(prior, deltas, current)
    assert legacy["schema_version"] == ip.LEGACY_PLAN_VERSION and ip.validate_incremental_plan(legacy) == legacy
    assert legacy["review_set"] == ["M1", "A1"] and len(legacy["tile_workset"]) == 78
    tampered = resign(deepcopy(legacy))
    tampered["items"][0]["action"] = "evaluate_new"
    resign(tampered)
    with pytest.raises(ip.IncrementalPlannerError):
        ip.validate_incremental_plan(tampered)
    assert latest["schema_version"] == ip.PLAN_VERSION and ip.validate_incremental_plan(latest) == latest
    assert latest["tile_workset"] == [tile for tile, _component in ip._REGISTRY]
    assert {row["action"] for row in latest["items"]} == {"reopen_contract_change"}


def test_first_execution_uses_all_tiles_and_unique_component_calls():
    result = ip.build_incremental_plan([], [], _series())
    assert result["registry_tile_ids"] == [tile for tile, _component in ip._REGISTRY]
    assert len(result["tile_workset"]) == 80 and result["component_workset"] == list(ip._COMPONENT_TILES)
    assert result["expected_calls"] == 10 and result["calls_avoided"] == 0


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


@pytest.mark.parametrize("disposition", ["contradiction", "human_review_required"])
def test_review_blocks_coherencia_dependency_and_provider_calls(disposition):
    result = plan(
        [judgment("M1"), judgment("C1")],
        [delta("M1", disposition, 8)],
        ids=("M1", "C1"),
    )
    assert result["items"][0]["action"] == (
        "reopen_contradiction" if disposition == "contradiction" else "human_review_required"
    )
    assert result["items"][1]["action"] == "reuse_canonical"
    assert result["tile_workset"] == []
    assert result["component_workset"] == []
    assert not any(row["expected_call_contribution"] for row in result["items"])
    assert result["expected_calls"] == 0 and result["calls_avoided"] == 2


def test_reviewed_tile_does_not_freeze_an_independent_delta_component():
    result = plan(
        [judgment("M1"), judgment("A1"), judgment("C1")],
        [delta("M1", "human_review_required", 8), delta("A1", "relevant", 9)],
        ids=("M1", "A1", "C1"),
    )
    assert result["items"][0]["action"] == "human_review_required"
    assert result["component_workset"] == ["attributes", "coherencia"]
    assert result["expected_calls"] == 2


def test_same_series_component_sentinel_reuses_all_tiles_without_calls():
    ids = component_ids("mission")
    prior = sentinel()
    result = plan(ids=ids, sentinels=[prior])
    assert ip.validate_component_not_detected_sentinel(prior) == prior
    assert [row["action"] for row in result["items"]] == ["reuse_canonical"] * len(ids)
    assert result["reuse_set"] == list(ids) and result["frozen_set"] == list(ids)
    assert {row["action"] for row in result["items"]} <= _PLAN_ACTIONS
    assert [row["reused_judgment_fingerprint"] for row in result["items"]] == [
        prior["canonical_component_sentinel_fingerprint"]
    ] * len(ids)
    assert result["tile_workset"] == [] and result["component_workset"] == []
    assert result["expected_calls"] == 0 and result["calls_avoided"] == 1
    assert ip.validate_incremental_plan(result) == result
    assert ip.canonical_plan_json(result) == ip.canonical_plan_json(plan(ids=ids, sentinels=[prior]))


def test_relevant_sentinel_delta_reopens_the_component_and_coherencia():
    ids = component_ids("mission", "coherencia")
    result = plan(
        component_judgments("coherencia"),
        [delta("M1", "relevant", 9)],
        ids=ids,
        sentinels=[sentinel()],
    )
    actions = {
        component: [row["action"] for row in result["items"] if row["component_key"] == component]
        for component in ("mission", "coherencia")
    }
    assert actions == {"mission": ["evaluate_delta"] * 5, "coherencia": ["evaluate_delta"] * 10}
    assert {row["reason"] for row in result["items"] if row["component_key"] == "coherencia"} == {
        "upstream_evidence_dependency"
    }
    assert result["tile_workset"] == list(ids)
    assert result["component_workset"] == ["mission", "coherencia"]
    assert result["expected_calls"] == 2 and result["calls_avoided"] == 0


def test_contract_change_reopens_sentinel_component_and_coherencia():
    ids = component_ids("mission", "coherencia")
    result = plan(
        component_judgments("coherencia"),
        ids=ids,
        current=_series(prompt_version="prompt-v2"),
        sentinels=[sentinel()],
    )
    actions = {
        component: [row["action"] for row in result["items"] if row["component_key"] == component]
        for component in ("mission", "coherencia")
    }
    assert actions == {"mission": ["reopen_contract_change"] * 5, "coherencia": ["reopen_contract_change"] * 10}
    assert {row["reason"] for row in result["items"] if row["component_key"] == "coherencia"} == {
        "series_contract_changed"
    }
    assert result["component_workset"] == ["mission", "coherencia"]
    assert result["expected_calls"] == 2 and result["calls_avoided"] == 0


def test_irrelevant_sentinel_delta_does_not_reopen_or_invalidate_coherencia():
    ids = component_ids("mission")
    result = plan([], [delta("M1", "irrelevant", 9)], ids=ids, sentinels=[sentinel()])
    assert [row["action"] for row in result["items"]] == ["reuse_canonical"] * len(ids)
    assert result["expected_calls"] == 0 and result["component_workset"] == []
    assert result["calls_avoided"] == 1


def test_signed_replay_rejects_malformed_duplicate_mixed_partial_wrong_and_noncanonical_sentinels():
    ids = component_ids("mission")
    original = plan(ids=ids, sentinels=[sentinel()])
    assert ip.validate_incremental_plan(original) == original

    malformed = deepcopy(original)
    malformed["prior_component_sentinels"] = [{"component_key": "mission"}]
    duplicate = deepcopy(original)
    duplicate["prior_component_sentinels"].append(deepcopy(duplicate["prior_component_sentinels"][0]))
    mixed = deepcopy(original)
    mixed["prior_judgments"].append(judgment("M1"))
    partial = deepcopy(original)
    partial["registry_tile_ids"] = list(ids[:-1])
    wrong_fingerprint = deepcopy(original)
    wrong_fingerprint["prior_component_sentinels"][0]["canonical_component_sentinel_fingerprint"] = "0" * 64
    noncanonical = deepcopy(original)
    noncanonical["prior_component_sentinels"] = type("Sentinels", (list,), {})(
        noncanonical["prior_component_sentinels"]
    )

    for candidate in (malformed, duplicate, mixed, partial, wrong_fingerprint, noncanonical):
        with pytest.raises(ip.IncrementalPlannerError):
            ip.validate_incremental_plan(resign(candidate))


def test_coherencia_direct_delta_preserves_its_action_reason_and_fingerprint():
    ids = component_ids("mission", "coherencia")
    direct_delta = delta("C1", "relevant", 10)
    result = plan(
        component_judgments("mission") + component_judgments("coherencia"),
        [delta("M1", "relevant", 9), direct_delta],
        ids=ids,
    )
    coherencia = next(row for row in result["items"] if row["tile_id"] == "C1")
    assert coherencia["action"] == "evaluate_delta"
    assert coherencia["reason"] == "relevant_evidence_changed"
    assert coherencia["delta_projection_fingerprint"] == direct_delta["projection_fingerprint"]


def test_component_sentinel_plan_actions_remain_within_closed_taxonomy():
    ids = component_ids("mission", "coherencia")
    result = plan(
        component_judgments("coherencia"),
        [delta("M1", "relevant", 9)],
        ids=ids,
        sentinels=[sentinel()],
    )
    assert {row["action"] for row in result["items"]} <= _PLAN_ACTIONS


def test_contract_change_invalidates_reusable_coherencia_with_contract_action():
    ids = component_ids("mission", "coherencia")
    current = _series(prompt_version="prompt-v2")
    result = plan(
        [judgment(tile, series=current) for tile in ip._COMPONENT_TILES["coherencia"]],
        ids=ids,
        current=current,
        sentinels=[sentinel()],
    )
    coherencia = [row for row in result["items"] if row["component_key"] == "coherencia"]
    assert [row["action"] for row in coherencia] == ["reopen_contract_change"] * len(coherencia)
    assert {row["reason"] for row in coherencia} == {"upstream_contract_change_dependency"}


def test_coherencia_direct_new_evaluation_is_not_overwritten():
    ids = component_ids("mission", "coherencia")
    result = plan(component_judgments("mission"), [delta("M1", "relevant", 9)], ids=ids)
    coherencia = [row for row in result["items"] if row["component_key"] == "coherencia"]
    assert [row["action"] for row in coherencia] == ["evaluate_new"] * len(coherencia)
    assert {row["reason"] for row in coherencia} == {"no_prior_judgment"}
