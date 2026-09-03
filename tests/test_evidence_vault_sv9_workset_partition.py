from copy import deepcopy
import json
import pytest
from src.services import evidence_vault_sv9_judgment_delta as delta
from src.services.evidence_vault_canonical_core import canonical_fingerprint, canonical_json
from src.services.evidence_vault_sv9_authoritative_relations import (
    project_evidence_vault_sv9_evaluation_input,
)
from src.services import evidence_vault_sv9_workset_partition as partition
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory
from tests.test_evidence_vault_sv9_authoritative_relations import _Repository, _facts, _id, _sha
from tests.test_sv9_judgment_memory import _series
def _input(*, extras=(), hints=(), sin_evidencia=False, missing=()):
    facts = _facts(count=max(1, len(missing)))
    if sin_evidencia:
        facts["authority"]["accepted"][0].update(assessment_state="sin_evidencia", basis=[])
    for tile in missing:
        next(row for row in facts["authority"]["accepted"] if row["tile_id"] == tile)["basis"][0].update(
            evidence_id=_sha(f"missing-{tile}"), source_identity_id=_sha(f"missing-source-{tile}")
        )
    for label in extras:
        facts["evidence"].append(
            dict(
                facts["evidence"][0], evidence_record_id=_id(label), evidence_ref=f"evidence-{label}",
                evidence_fingerprint=_sha(label), evidence_id=_sha(f"{label}-evidence"),
                source_identity_id=_sha(f"{label}-source"),
            )
        )
    components = dict(planner._REGISTRY)
    facts["evaluation_hint_seeds"] = [
        {
            "hint_id": _id(label), "tile_id": tile, "component_key": components[tile],
            "evidence_record_id": next(row["evidence_record_id"] for row in facts["evidence"] if row["evidence_ref"] == f"evidence-{evidence}"),
            "provenance_fingerprint": _sha(f"{label}-provenance"),
        }
        for label, tile, evidence in hints
    ]
    result = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    assert result["status"] == "available"
    return result
def _prior(value, *, without=()):
    source, current = value["source_identity"], value["current_evidence"][0]
    origins = {
        "capture_origin": {key: source[key] for key in ("capture_id", "capture_fingerprint")},
        "operation_origin": {"operation_id": source["operation_plan_id"], "operation_fingerprint": source["operation_fingerprint"]},
    }
    return [
        memory.build_tile_judgment(
            tile_id=tile, component_key=component, assessment_state="ok", supporting_evidence=[current],
            series_contract=_series(), authority_state="accepted", review_state="none", lifecycle_state="active", lifecycle_reason="", **origins,
        )
        for tile, component in planner._REGISTRY if component not in without
    ]
def _sentinel(value, component="mission"):
    source = value["source_identity"]
    return planner.build_component_not_detected_sentinel(
        component_key=component, status="not_detected", supporting_evidence=[],
        capture_origin={key: source[key] for key in ("capture_id", "capture_fingerprint")},
        operation_origin={"operation_id": source["operation_plan_id"], "operation_fingerprint": source["operation_fingerprint"]},
        series_contract=_series(), authority_state="accepted", review_state="none", lifecycle_state="active", lifecycle_reason="",
    )
def _delta(value, *, prior=None, sentinels=(), relations=None):
    return delta.build_evidence_vault_sv9_judgment_delta(
        current_evidence=delta.build_evidence_identity_set(value["current_evidence"]),
        prior_judgments=_prior(value) if prior is None else prior,
        prior_component_sentinels=list(sentinels), authoritative_relations=value["authoritative_relations"] if relations is None else relations,
        current_series_contract=_series(),
    )
def _build(value, signed_delta, trusted=None):
    return partition.build_evidence_vault_sv9_workset_partition(
        evaluation_input=value, judgment_delta=signed_delta, trusted_irrelevant_evidence=trusted,
    )
def _ids(rows):
    return [row["tile_id"] if type(row) is dict else row for row in rows]
def test_scoreless_partition_replays_exact_80_tiles_and_preserves_signed_inputs():
    source, signed_delta = _input(), None
    signed_delta = _delta(source)
    before = canonical_json(signed_delta)
    value = _build(source, signed_delta)
    all_tiles = [tile for tile, _component in planner._REGISTRY]
    groups = (_ids(value["healthy_workset"]["tiles"]), value["review_partition"]["tile_ids"], value["allowed_reuse_tile_ids"])
    assert value["authority"] is False and value["runtime_effect"] == "evaluation_routing_only"
    assert value["score_state"] == "unavailable" and not ({"assessment", "candidate", "adoptable", "publishable"} & set(value))
    assert sorted([tile for group in groups for tile in group], key=all_tiles.index) == all_tiles
    assert not (set(groups[0]) & set(groups[1]) or set(groups[0]) & set(groups[2]) or set(groups[1]) & set(groups[2]))
    assert value["evaluation_input"] == source and canonical_json(value["judgment_delta"]) == before
    assert value["input_binding"]["canonical_plan_fingerprint"] == signed_delta["plan"]["canonical_plan_fingerprint"]
    assert partition.validate_evidence_vault_sv9_workset_partition(json.loads(json.dumps(value))) == value
    all_new = _build(source, _delta(source, prior=[]))
    assert len(all_new["healthy_workset"]["tiles"]) == 80 and all_new["review_partition"]["tile_ids"] == [] and all_new["coherencia_dependency"]["state"] == "deferred" and all_new["coherencia_dependency"]["reasons"] == ["upstream_healthy_work"]
    assert {row["route_source"] for row in all_new["healthy_workset"]["tiles"] if row["component_key"] == "coherencia"} == {"coherencia_deferred_dependency"}
def test_hints_route_only_unmapped_evidence_and_partition_pending_and_trusted_rows():
    source = _input(extras=("hinted", "trusted", "trusted-two", "pending"), hints=(("hint", "M1", "hinted"),))
    trusted = [row for row in source["current_identity_bindings"] if row["evidence_ref"] in {"evidence-trusted", "evidence-trusted-two"}]
    value = _build(source, _delta(source), trusted)
    healthy = {row["tile_id"]: row for row in value["healthy_workset"]["tiles"]}
    assert healthy["M1"]["route_source"] == "signed_hint"
    assert healthy["M1"]["current_evidence_bindings"] == [next(row for row in source["current_identity_bindings"] if row["evidence_ref"] == "evidence-hinted")]
    assert {row["evidence_ref"] for row in value["trusted_irrelevant_evidence"]} == {"evidence-trusted", "evidence-trusted-two"}
    assert [row["evidence_ref"] for row in value["pending_evidence"]] == ["evidence-pending"]
    assert value["coherencia_dependency"]["state"] == "blocked"
    assert set(planner._COMPONENT_TILES["coherencia"]) <= set(value["review_partition"]["coherencia_blocked_tile_ids"])
    assert not set(planner._COMPONENT_TILES["coherencia"]) & set(healthy) and not set(planner._COMPONENT_TILES["coherencia"]) & set(value["allowed_reuse_tile_ids"])
    assert value == _build(source, _delta(source), list(reversed(trusted)))
    with pytest.raises(partition.EvidenceVaultSv9WorksetPartitionError):
        _build(source, _delta(source), trusted + [source["current_identity_bindings"][0]])


def test_authoritative_evidence_can_feed_a_hint_for_another_tile():
    source = _input(hints=(("cross-tile-hint", "M2", "1"),))
    signed_delta = _delta(source, prior=[])

    value = _build(source, signed_delta)

    m2 = next(row for row in value["healthy_workset"]["tiles"] if row["tile_id"] == "M2")
    assert m2["current_evidence_bindings"] == [source["current_identity_bindings"][0]]


def test_hint_reusing_the_same_authoritative_tile_relation_fails_closed():
    source = _input(hints=(("same-tile-hint", "M1", "1"),))
    signed_delta = _delta(source, prior=[])

    with pytest.raises(partition.EvidenceVaultSv9WorksetPartitionError):
        _build(source, signed_delta)


def test_reusable_component_sentinel_expands_from_signed_hint_and_blocks_coherencia():
    source = _input(extras=("hinted",), hints=(("hint", "M1", "hinted"),), sin_evidencia=True)
    signed_delta = _delta(source, prior=_prior(source, without=("mission",)), sentinels=[_sentinel(source)])
    value = _build(source, signed_delta)
    healthy = {row["tile_id"]: row for row in value["healthy_workset"]["tiles"]}
    mission = set(planner._COMPONENT_TILES["mission"])
    coherencia = set(planner._COMPONENT_TILES["coherencia"])
    assert mission <= set(healthy) and healthy["M1"]["route_source"] == "signed_hint_component_sentinel"
    assert all(healthy[tile]["route_source"] == "signed_hint_component_expansion" for tile in mission - {"M1"})
    assert coherencia <= set(healthy) and value["coherencia_dependency"]["state"] == "deferred"
    assert not mission & set(value["allowed_reuse_tile_ids"]) and not coherencia & set(value["allowed_reuse_tile_ids"])
def test_review_causes_tampering_and_ordering_fail_closed():
    source = _input(missing=("M1", "M2"))
    value = _build(source, _delta(source))
    review = value["review_partition"]
    assert review["evaluation_input_reopened_tile_ids"] == ["M1", "M2"]
    assert review["operational_authority_coverage_loss_tile_ids"] == ["M1", "M2"]
    assert set(planner._COMPONENT_TILES["coherencia"]) <= set(review["coherencia_blocked_tile_ids"])
    tampered = deepcopy(value)
    tampered["review_partition"]["tile_ids"] = []
    tampered["partition_fingerprint"] = canonical_fingerprint(partition._FINGERPRINT, {key: raw for key, raw in tampered.items() if key != "partition_fingerprint"})
    with pytest.raises(partition.EvidenceVaultSv9WorksetPartitionError):
        partition.validate_evidence_vault_sv9_workset_partition(tampered)
    single = _input(missing=("M1",))
    assert _build(single, _delta(single))["review_partition"]["evaluation_input_reopened_tile_ids"] == ["M1"]
    source = _input()
    prior = _prior(source)
    index = next(index for index, row in enumerate(prior) if row["tile_id"] == "M3")
    raw = {key: item for key, item in prior[index].items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint"}}
    prior[index] = memory.build_tile_judgment(**(raw | {"supporting_evidence": [{"evidence_ref": "missing", "evidence_fingerprint": _sha("delta-missing")}]}))
    assert _build(source, _delta(source, prior=prior))["review_partition"]["judgment_delta_coverage_loss_tile_ids"] == ["M3"]
    review_prior = _prior(source)
    index = next(index for index, row in enumerate(review_prior) if row["tile_id"] == "M4")
    raw = {key: item for key, item in review_prior[index].items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint"}}
    review_prior[index] = memory.build_tile_judgment(**(raw | {"review_state": "required"}))
    assert _build(source, _delta(source, prior=review_prior))["review_partition"]["planner_review_tile_ids"] == ["M4"]
    coherent = _input(extras=("coherent",), hints=(("coherent-hint", "C1", "coherent"),))
    assert _build(coherent, _delta(coherent))["coherencia_dependency"]["state"] == "evaluable"
    facts = _facts()
    facts["evidence"].append(dict(facts["evidence"][0], evidence_record_id=_id("duplicate-record")))
    duplicate = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    assert duplicate["status"] == "review_required" and duplicate["reason_codes"] == ["ambiguous_current_evidence"]
    with pytest.raises(partition.EvidenceVaultSv9WorksetPartitionError):
        _build(source, _delta(source, relations=[]))


def test_legacy_compatible_delta_and_plan_are_rejected_at_workset_boundary():
    source = _input()
    legacy = deepcopy(_delta(source))
    legacy["schema_version"] = delta.LEGACY_JUDGMENT_DELTA_VERSION
    legacy.pop("prior_component_sentinels")
    current_plan = legacy["plan"]
    legacy["plan"] = planner.build_incremental_plan_v2(
        current_plan["prior_judgments"],
        current_plan["delta_projections"],
        current_plan["current_series_contract"],
        registry_tile_ids=current_plan["registry_tile_ids"],
        prior_component_sentinels=current_plan["prior_component_sentinels"],
    )
    legacy["canonical_delta_fingerprint"] = delta.jm.canonical_fingerprint(
        delta._LEGACY_DELTA_FINGERPRINT,
        {key: raw for key, raw in legacy.items() if key != "canonical_delta_fingerprint"},
    )

    assert delta.validate_evidence_vault_sv9_judgment_delta(legacy) == legacy
    assert planner.validate_incremental_plan(legacy["plan"]) == legacy["plan"]
    with pytest.raises(partition.EvidenceVaultSv9WorksetPartitionError):
        _build(source, legacy)
