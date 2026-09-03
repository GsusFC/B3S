from copy import deepcopy
import json

import pytest

from src.services import evidence_vault_sv9_evaluation_checkpoint as checkpoint
from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_sv9_authoritative_relations import (
    project_evidence_vault_sv9_evaluation_input,
)
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory
from tests.test_evidence_vault_sv9_authoritative_relations import _Repository, _facts, _id, _sha
from tests.test_sv9_judgment_memory import _series


def _input(*, reopen=False, extra=False, hint_specs=()):
    facts = _facts()
    if reopen:
        facts["authority"]["accepted"][0]["basis"][0].update(
            evidence_id=_sha("missing"), source_identity_id=_sha("missing-source")
        )
    extra_labels = ("extra",) if extra is True else () if extra is False else tuple(extra)
    for label in extra_labels:
        facts["evidence"].append(
            dict(
                facts["evidence"][0],
                evidence_record_id=_id(label),
                evidence_ref=f"evidence-{label}",
                evidence_fingerprint=_sha(label),
                evidence_id=_sha(f"{label}-evidence"),
                source_identity_id=_sha(f"{label}-source"),
            )
        )
    if hint_specs:
        components = {"M1": "mission", "M2": "mission"}
        facts["evaluation_hint_seeds"] = [
            {
                "hint_id": _id(label),
                "tile_id": tile,
                "component_key": components[tile],
                "evidence_record_id": next(
                    row["evidence_record_id"]
                    for row in facts["evidence"]
                    if row["evidence_ref"] == evidence_ref
                ),
                "provenance_fingerprint": _sha(f"{label}-provenance"),
            }
            for label, evidence_ref, tile in hint_specs
        ]
    value = project_evidence_vault_sv9_evaluation_input(
        repository=_Repository(facts), source_scan_id="scan-1"
    )
    assert value["status"] == "available"
    return value


def _snapshot(existing=True):
    if not existing:
        return {"state": "bootstrap_absent"}
    return {
        "state": "accepted_authority",
        "accepted_candidate_id": _id("candidate"),
        "active_event_id": _id("event"),
        "current_head_event_fingerprint": _sha("head"),
        "candidate_complete_record_fingerprint": _sha("record"),
        "canonical_plan_fingerprint": _sha("prior-plan"),
        "current_series_fingerprint": _sha("prior-series"),
    }


def _progress(value, request="request", component="mission", tile_id=None):
    source, series = value["source_identity"], _series()
    tile_id = planner._COMPONENT_TILES[component][0] if tile_id is None else tile_id
    binding = value["current_identity_bindings"][0]
    evidence_row = {key: binding[key] for key in ("evidence_ref", "evidence_fingerprint")}
    origins = {
        "capture_origin": {key: source[key] for key in ("capture_id", "capture_fingerprint")},
        "operation_origin": {"operation_id": source["operation_plan_id"], "operation_fingerprint": source["operation_fingerprint"]},
    }
    judgment = memory.build_tile_judgment(
        tile_id=tile_id, component_key=component, assessment_state="ok", supporting_evidence=[evidence_row],
        series_contract=series, authority_state="pending", review_state="none", lifecycle_state="active", lifecycle_reason="", **origins
    )
    component = evaluation.build_component_evaluation(
        component_key=component, series_fingerprint=judgment["series_fingerprint"], request_fingerprint=_sha(request),
        status="evaluated", tile_results=[{"tile_id": tile_id, "assessment_state": "ok", "supporting_evidence": [evidence_row]}]
    )
    return {"component_evaluations": [component], "evaluated_tile_judgments": [judgment]}


def _not_detected_progress(value=None, *, component="mission", request="request", **changes):
    value = _input() if value is None else value
    source, series = value["source_identity"], _series()
    sentinel = planner.build_component_not_detected_sentinel(
        component_key=component,
        status="not_detected",
        supporting_evidence=[],
        capture_origin={key: source[key] for key in ("capture_id", "capture_fingerprint")},
        operation_origin={"operation_id": source["operation_plan_id"], "operation_fingerprint": source["operation_fingerprint"]},
        series_contract=series,
        authority_state="pending",
        review_state="none",
        lifecycle_state="active",
        lifecycle_reason="",
        **changes,
    )
    component_evaluation = evaluation.build_component_evaluation(
        component_key=component,
        series_fingerprint=sentinel["series_fingerprint"],
        request_fingerprint=_sha(request),
        status="not_detected",
        tile_results=[],
    )
    return {"component_evaluations": [component_evaluation], "evaluated_component_sentinels": [sentinel], "evaluated_tile_judgments": []}


def _resign_sentinel(value, **changes):
    unsigned = {key: item for key, item in value.items() if key not in {"schema_version", "series_fingerprint", "canonical_component_sentinel_fingerprint"}}
    return planner.build_component_not_detected_sentinel(**(unsigned | changes))


def _hint(label="hint", value=None, record="evidence-1", tile="M1"):
    value = _input(hint_specs=((label, record, tile),)) if value is None else value
    return deepcopy(next(item for item in value["non_authoritative_hints"] if item["hint_id"] == _id(label)))


def _build(value=None, *, snapshot=None, healthy=("M1",), review=(), pending=(), hints=None, state="partial", progress=True, request="request", evaluated_component_sentinels=()):
    value = _input() if value is None else value
    series = _progress(value, request)["evaluated_tile_judgments"][0]["series_fingerprint"]
    return checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(
        evaluation_input=value, prior_authority_snapshot=_snapshot() if snapshot is None else snapshot,
        current_series_fingerprint=series, canonical_plan_fingerprint=_sha("checkpoint-plan"), candidate_series_fingerprint=_sha("candidate-series"),
        healthy_tile_ids=list(healthy), review_tile_ids=list(review), pending_evidence=list(pending),
        non_authoritative_hints=hints, evaluation_state=state, evaluated_component_sentinels=list(evaluated_component_sentinels),
        **(_progress(value, request) if progress else {"component_evaluations": [], "evaluated_tile_judgments": []}),
    )


def _build_not_detected(value=None, *, snapshot=None, healthy=None, state="partial", progress=True, request="request", sentinels=None):
    value = _input() if value is None else value
    progress = _not_detected_progress(value, request=request) if progress else {"component_evaluations": [], "evaluated_component_sentinels": [], "evaluated_tile_judgments": []}
    component = progress["evaluated_component_sentinels"][0]["component_key"] if progress["evaluated_component_sentinels"] else "mission"
    series = progress["component_evaluations"][0]["series_fingerprint"] if progress["component_evaluations"] else _progress(value)["component_evaluations"][0]["series_fingerprint"]
    return checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(
        evaluation_input=value, prior_authority_snapshot=_snapshot() if snapshot is None else snapshot,
        current_series_fingerprint=series, canonical_plan_fingerprint=_sha("checkpoint-plan"), candidate_series_fingerprint=_sha("candidate-series"),
        healthy_tile_ids=list(planner._COMPONENT_TILES[component] if healthy is None else healthy), review_tile_ids=[],
        pending_evidence=[], non_authoritative_hints=None, evaluation_state=state,
        evaluated_component_sentinels=progress["evaluated_component_sentinels"] if sentinels is None else list(sentinels),
        component_evaluations=progress["component_evaluations"], evaluated_tile_judgments=progress["evaluated_tile_judgments"],
    )

def test_checkpoint_v1_rejects_partition_api_and_keeps_v1_plan_binding():
    source = _input()
    progress = _progress(source)
    with pytest.raises(TypeError):
        checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(
            evaluation_input=source,
            prior_authority_snapshot=_snapshot(),
            current_series_fingerprint=progress["evaluated_tile_judgments"][0]["series_fingerprint"],
            canonical_plan_fingerprint=_sha("checkpoint-plan"),
            candidate_series_fingerprint=_sha("candidate-series"),
            workset_partition={},
            healthy_tile_ids=["M1"],
            **progress,
        )
    value = _build(source)
    assert value["schema_version"] == "evidence-vault-sv9-evaluation-checkpoint-v1"
    assert set(value["plan_binding"]) == {"current_series_fingerprint", "canonical_plan_fingerprint", "candidate_series_fingerprint"}
    assert "workset_partition_fingerprint" not in json.dumps(value)


def test_checkpoint_v1_rejects_v2_and_partition_payloads():
    value = _build()
    for malformed in (
        {**value, "schema_version": "evidence-vault-sv9-evaluation-checkpoint-v2"},
        {**value, "plan_binding": {**value["plan_binding"], "workset_partition_fingerprint": "sha256:" + "0" * 64}},
    ):
        with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
            checkpoint.validate_evidence_vault_sv9_evaluation_checkpoint(malformed)


def test_zero_evaluation_checkpoint_fails_closed():
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        _build(progress=False)


def test_multi_evaluation_checkpoint_fails_closed():
    source = _input()
    progress = _progress(source)
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(
            evaluation_input=source,
            prior_authority_snapshot=_snapshot(),
            current_series_fingerprint=progress["evaluated_tile_judgments"][0]["series_fingerprint"],
            canonical_plan_fingerprint=_sha("checkpoint-plan"),
            candidate_series_fingerprint=_sha("candidate-series"),
            healthy_tile_ids=["M1"],
            component_evaluations=progress["component_evaluations"] * 2,
            evaluated_tile_judgments=progress["evaluated_tile_judgments"],
        )


def test_schema_is_checkpoint_only_non_authoritative_and_round_trips():
    value = _build()
    assert value["authority"] is False and value["runtime_effect"] == "checkpoint_only"
    assert value["evaluation_state"] == "partial" and value["score_state"] == "unavailable" and value["reason_codes"] == []
    assert not {"assessment", "score", "adoptable", "publishable", "complete_candidate"} & set(value)
    assert checkpoint.validate_evidence_vault_sv9_evaluation_checkpoint(value) == value


def test_bootstrap_and_existing_authority_snapshots_are_distinct_and_signed():
    bootstrap, existing = _build(snapshot=_snapshot(False)), _build()
    assert bootstrap["prior_authority_snapshot"]["state"] == "bootstrap_absent"
    assert existing["prior_authority_snapshot"]["state"] == "accepted_authority"
    assert bootstrap["prior_authority_snapshot"]["snapshot_fingerprint"] != existing["prior_authority_snapshot"]["snapshot_fingerprint"]
    facts = _facts(); facts["authority"] = None
    unavailable = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    assert unavailable["status"] == "review_required"
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(evaluation_input=unavailable, prior_authority_snapshot=_snapshot(False), current_series_fingerprint=_sha("series"), canonical_plan_fingerprint=_sha("plan"), candidate_series_fingerprint=_sha("candidate"))


def test_reopened_review_and_healthy_tiles_are_disjoint_and_canonical():
    value = _input(reopen=True)
    progress = _progress(value, component="vision")
    checkpointed = checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(
        evaluation_input=value,
        prior_authority_snapshot=_snapshot(),
        current_series_fingerprint=progress["evaluated_tile_judgments"][0]["series_fingerprint"],
        canonical_plan_fingerprint=_sha("checkpoint-plan"),
        candidate_series_fingerprint=_sha("candidate-series"),
        healthy_tile_ids=["V1"],
        review_tile_ids=["M1"],
        **progress,
    )
    assert checkpointed["review_partition"]["reopened_tile_ids"] == ["M1"] and checkpointed["reason_codes"] == ["reopened_tiles_pending"]
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        _build(value, healthy=("M1",), review=("M1",))


def test_capture_bound_hints_are_persisted_exactly_from_signed_evaluation_input():
    source = _input(extra=True, hint_specs=(("hint", "evidence-extra", "M1"),))
    derived = _build(source)
    explicit = _build(source, hints=deepcopy(source["non_authoritative_hints"]))
    assert derived["non_authoritative_hints"] == source["non_authoritative_hints"]
    assert explicit["non_authoritative_hints"] == source["non_authoritative_hints"]
    hint = source["non_authoritative_hints"][0]
    assert hint["authority"] is False and hint["runtime_effect"] == "evaluation_routing_only"
    assert derived["non_authoritative_hints"] == explicit["non_authoritative_hints"]
    assert checkpoint.validate_evidence_vault_sv9_evaluation_checkpoint(derived) == derived


def test_checkpoint_hints_reject_seed_shapes_additions_omissions_mutations_and_reordering():
    source = _input(extra=True, hint_specs=(("one", "evidence-extra", "M1"), ("two", "evidence-extra", "M2")))
    signed = source["non_authoritative_hints"]
    seed = {key: signed[0][key] for key in ("hint_id", "tile_id", "component_key", "evidence_record_id", "provenance_fingerprint")}
    cases = [
        [seed],
        signed[1:],
        signed + [deepcopy(signed[0])],
        [dict(signed[0], runtime_effect="checkpoint_only"), signed[1]],
        list(reversed(signed)),
    ]
    for hints in cases:
        with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
            _build(source, hints=hints)

    tampered = deepcopy(_build(source))
    tampered["non_authoritative_hints"][0]["authority"] = True
    tampered["checkpoint_fingerprint"] = canonical_fingerprint(
        checkpoint._FINGERPRINT,
        {key: value for key, value in tampered.items() if key != "checkpoint_fingerprint"},
    )
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        checkpoint.validate_evidence_vault_sv9_evaluation_checkpoint(tampered)


def test_checkpoint_builder_rejects_type_loose_hint_equality_collisions():
    source = _input(extra=True, hint_specs=(("hint", "evidence-extra", "M1"),))
    colliding = deepcopy(source["non_authoritative_hints"])
    colliding[0]["authority"] = 0
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        _build(source, hints=colliding)


def test_checkpoint_replay_rejects_serialized_type_loose_hint_mutation():
    source = _input(extra=True, hint_specs=(("hint", "evidence-extra", "M1"),))
    checkpointed = _build(source)
    deserialized = json.loads(json.dumps(checkpointed))
    deserialized["non_authoritative_hints"][0]["authority"] = 0
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        checkpoint.validate_evidence_vault_sv9_evaluation_checkpoint(deserialized)


def test_pending_unmapped_evidence_and_provider_failure_preserve_progress_and_authority():
    source = _input(extra=("hinted", "pending"), hint_specs=(("hint", "evidence-hinted", "M1"),))
    row = next(item for item in source["current_identity_bindings"] if item["evidence_ref"] == "evidence-pending")
    pending = [{**row, "reason": "unmapped_evidence"}]
    value = _build(source, pending=pending)
    failed = _build(source, pending=pending, state="provider_failure")
    assert value["healthy_workset"]["tile_ids"] == ["M1"] and value["pending_evidence"] == pending and value["reason_codes"] == ["unmapped_evidence_pending"]
    assert failed["evaluation_state"] == "provider_failure" and failed["reason_codes"] == ["unmapped_evidence_pending", "provider_failure"] and failed["prior_authority_snapshot"] == value["prior_authority_snapshot"]
    hinted_source = _input(extra=True, hint_specs=(("extra-hint", "evidence-extra", "M1"),))
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError): _build(hinted_source, pending=pending)


def test_semantic_reordering_is_invariant_and_bound_changes_change_fingerprint():
    source = _input(extra=("one", "two"), hint_specs=(("two", "evidence-two", "M2"), ("one", "evidence-one", "M1")))
    first = _build(source, healthy=("M1",))
    second = _build(source, healthy=("M1",))
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        _build(source, healthy=("M1", "M2"), hints=list(reversed(source["non_authoritative_hints"])))
    reopened_source = _input(reopen=True)
    reopened_progress = _progress(reopened_source, component="vision")
    reopened = checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(
        evaluation_input=reopened_source, prior_authority_snapshot=_snapshot(),
        current_series_fingerprint=reopened_progress["evaluated_tile_judgments"][0]["series_fingerprint"],
        canonical_plan_fingerprint=_sha("checkpoint-plan"), candidate_series_fingerprint=_sha("candidate-series"),
        healthy_tile_ids=["V1"], review_tile_ids=["M1"], **reopened_progress,
    )
    changed = [_build(), _build(_input(extra=True, hint_specs=(("hint", "evidence-extra", "M1"),))), _build(request="other"), _build(snapshot={**_snapshot(), "active_event_id": _id("other-event")})]
    assert first == second
    assert len({first["checkpoint_fingerprint"], reopened["checkpoint_fingerprint"], *(item["checkpoint_fingerprint"] for item in changed)}) == 6


def test_changing_signed_hint_set_changes_evaluation_input_and_checkpoint_fingerprints():
    baseline_source = _input()
    hinted_source = _input(extra=True, hint_specs=(("hint", "evidence-extra", "M1"),))
    baseline, hinted = _build(baseline_source), _build(hinted_source)
    assert hinted_source["evaluation_input_fingerprint"] != baseline_source["evaluation_input_fingerprint"]
    assert hinted["checkpoint_fingerprint"] != baseline["checkpoint_fingerprint"]


def test_invalid_schema_types_duplicates_partitions_and_tampering_fail_closed():
    value = _build(); malformed = deepcopy(value)
    malformed["evaluation_input"]["operational_witness"]["adoption_sequence"] = True
    malformed_id = deepcopy(value); malformed_id["evaluation_input"]["source_identity"]["capture_id"] = "bad"
    malformed_fingerprint = deepcopy(value); malformed_fingerprint["plan_binding"]["canonical_plan_fingerprint"] = "bad"
    cases = [
        {**value, "assessment": None},
        {**value, "checkpoint_fingerprint": "0" * 64},
        {**value, "reason_codes": ["provider_failure"]},
        malformed,
        malformed_id,
        malformed_fingerprint,
    ]
    for case in cases:
        with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
            checkpoint.validate_evidence_vault_sv9_evaluation_checkpoint(case)
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError): _build(healthy=("M1", "M1"))
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError): _build(healthy=("M1",), review=("M1",))
    source = _input(extra=True); row = next(item for item in source["current_identity_bindings"] if item["evidence_ref"] == "evidence-extra")
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError): _build(source, pending=[{**row, "reason": "unmapped_evidence"}] * 2)


def test_pending_not_detected_progress_is_canonical_and_survives_provider_failure():
    value = _build_not_detected()
    expected = _not_detected_progress()["evaluated_component_sentinels"]
    assert value["healthy_workset"]["tile_ids"] == list(planner._COMPONENT_TILES["mission"])
    assert value["healthy_workset"]["evaluated_component_sentinels"] == expected
    assert value["healthy_workset"]["component_evaluations"][0]["status"] == "not_detected"
    assert value["healthy_workset"]["evaluated_component_sentinels"][0]["authority_state"] == "pending"
    assert checkpoint.validate_evidence_vault_sv9_evaluation_checkpoint(value) == value

    failed = _build_not_detected(state="provider_failure")
    assert failed["evaluation_state"] == "provider_failure"
    assert failed["reason_codes"] == ["provider_failure"]
    assert failed["healthy_workset"] == value["healthy_workset"]
    assert checkpoint.validate_evidence_vault_sv9_evaluation_checkpoint(failed) == failed


@pytest.mark.parametrize("change", [
    {"authority_state": "accepted"},
    {"review_state": "human_review_required"},
    {"lifecycle_state": "retired"},
])
def test_pending_not_detected_sentinels_require_pending_active_none_state(change):
    source = _input()
    valid = _not_detected_progress(source)["evaluated_component_sentinels"][0]
    invalid = deepcopy(valid)
    invalid.update(change)
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        _build_not_detected(source, sentinels=[invalid])


@pytest.mark.parametrize("binding", ["series_contract", "capture_origin", "operation_origin"])
def test_pending_not_detected_sentinels_bind_current_series_and_capture_operation(binding):
    source = _input()
    valid = _not_detected_progress(source)["evaluated_component_sentinels"][0]
    replacements = {
        "series_contract": _series(prompt_version="prompt-v2"),
        "capture_origin": {"capture_id": _id("other-capture"), "capture_fingerprint": _sha("other-capture")},
        "operation_origin": {"operation_id": _id("other-operation"), "operation_fingerprint": _sha("other-operation")},
    }
    invalid = _resign_sentinel(valid, **{binding: replacements[binding]})
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        _build_not_detected(source, sentinels=[invalid])


def test_not_detected_progress_requires_full_non_overlapping_component_capacity():
    source = _input()
    progress = _not_detected_progress(source)
    sentinel = progress["evaluated_component_sentinels"][0]
    judgment_progress = _progress(source)
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        _build_not_detected(source, healthy=planner._COMPONENT_TILES["mission"][:-1])
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(
            evaluation_input=source,
            prior_authority_snapshot=_snapshot(),
            current_series_fingerprint=sentinel["series_fingerprint"],
            canonical_plan_fingerprint=_sha("checkpoint-plan"),
            candidate_series_fingerprint=_sha("candidate-series"),
            healthy_tile_ids=list(planner._COMPONENT_TILES["mission"]),
            evaluated_component_sentinels=[sentinel],
            component_evaluations=progress["component_evaluations"],
            evaluated_tile_judgments=judgment_progress["evaluated_tile_judgments"],
        )


@pytest.mark.parametrize("kind", ["missing_sentinel", "missing_evaluation", "evaluated_status", "wrong_component", "duplicate_sentinel"])
def test_not_detected_progress_requires_one_to_one_signed_evaluation_and_sentinel(kind):
    source = _input()
    progress = _not_detected_progress(source)
    sentinel = progress["evaluated_component_sentinels"][0]
    evaluations = progress["component_evaluations"]
    sentinels = [sentinel]
    if kind == "missing_sentinel":
        sentinels = []
    elif kind == "missing_evaluation":
        evaluations = []
    elif kind == "evaluated_status":
        evaluations = [_progress(source)["component_evaluations"][0]]
    elif kind == "wrong_component":
        vision = _not_detected_progress(source, component="vision")
        evaluations, sentinels = vision["component_evaluations"], [sentinel]
    elif kind == "duplicate_sentinel":
        sentinels = [sentinel, deepcopy(sentinel)]
    with pytest.raises(checkpoint.EvidenceVaultSv9EvaluationCheckpointError):
        checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(
            evaluation_input=source,
            prior_authority_snapshot=_snapshot(),
            current_series_fingerprint=sentinel["series_fingerprint"],
            canonical_plan_fingerprint=_sha("checkpoint-plan"),
            candidate_series_fingerprint=_sha("candidate-series"),
            healthy_tile_ids=list(planner._COMPONENT_TILES["mission"]),
            evaluated_component_sentinels=sentinels,
            component_evaluations=evaluations,
            evaluated_tile_judgments=[],
        )
