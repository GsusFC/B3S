import pytest

from src.sv9 import judgment_memory as jm


def _hash(number):
    return f"{number:064x}"


def _origin(prefix, number):
    return {f"{prefix}_id": f"{prefix}-{number}", f"{prefix}_fingerprint": _hash(number)}


def _evidence(*numbers):
    return [{"evidence_ref": f"evidence:{number}", "evidence_fingerprint": _hash(number)} for number in numbers]


def _series(**changes):
    values = {
        "evaluator_version": "evaluator-v1",
        "prompt_version": "prompt-v1",
        "model_version": "model-v1",
        "flow_version": "flow-v1",
        "normalization_version": "normalization-v1",
    }
    return jm.build_judgment_series_contract(**(values | changes))


def _judgment(series=None, evidence=None, metadata=True, **changes):
    value = {
        "tile_id": "M1",
        "component_key": "mission",
        "assessment_state": "ok",
        "supporting_evidence": _evidence(3) if evidence is None else evidence,
        "capture_origin": _origin("capture", 4),
        "operation_origin": _origin("operation", 5),
        "series_contract": _series() if series is None else series,
    }
    if metadata:
        value |= {
            "authority_state": "accepted",
            "review_state": "none",
            "lifecycle_state": "active",
            "lifecycle_reason": "",
        }
    return jm.build_tile_judgment(**(value | changes))


def _delta(evidence=None, **changes):
    value = {
        "tile_id": "M1",
        "component_key": "mission",
        "disposition": "relevant",
        "evidence": _evidence(3) if evidence is None else evidence,
        "capture_origin": _origin("capture", 9),
        "operation_origin": _origin("operation", 10),
    }
    return jm.build_tile_evidence_delta_projection(**(value | changes))


@pytest.mark.parametrize(
    ("build", "validate", "key"),
    [
        (_judgment, jm.validate_tile_judgment, "supporting_evidence"),
        (_delta, jm.validate_tile_evidence_delta_projection, "evidence"),
    ],
)
def test_evidence_refs_are_canonical_and_replayable(build, validate, key):
    first, second = build(evidence=_evidence(2, 1)), build(evidence=_evidence(1, 2))
    assert first[key] == _evidence(1, 2)
    assert jm.canonical_json(first) == jm.canonical_json(second)
    assert validate(first) == first


@pytest.mark.parametrize(
    ("field", "value"),
    [("rubric_version", "rubric-v0"), ("tile_registry_fingerprint", _hash(7)), ("scoring_policy_version", "policy-v0")],
)
def test_series_constants_and_changes_define_judgment_fingerprints(field, value):
    original, changed = _series(), _series(prompt_version="prompt-v2")
    assert jm.validate_judgment_series_contract(original) == original
    assert _judgment(series=original)["series_fingerprint"] != _judgment(series=changed)["series_fingerprint"]
    with pytest.raises(jm.JudgmentMemoryContractError):
        _series(**{field: value})


def test_replay_and_invalid_values_fail_closed():
    series = {**_series(), "prompt_version": " prompt-v1 "}
    judgment = _judgment(evidence=_evidence(1, 2))
    judgment["supporting_evidence"].reverse()
    delta = _delta(evidence=_evidence(10))
    delta["evidence"][0]["evidence_fingerprint"] = delta["evidence"][0]["evidence_fingerprint"].upper()
    duplicate_ref = _judgment(evidence=_evidence(1, 2))
    duplicate_ref["supporting_evidence"][1]["evidence_ref"] = duplicate_ref["supporting_evidence"][0]["evidence_ref"]
    duplicate_fingerprint = _judgment(evidence=_evidence(1, 2))
    duplicate_fingerprint["supporting_evidence"][1]["evidence_fingerprint"] = duplicate_fingerprint[
        "supporting_evidence"
    ][0]["evidence_fingerprint"]
    for validate, value in (
        (jm.validate_judgment_series_contract, series),
        (jm.validate_tile_judgment, judgment),
        (jm.validate_tile_evidence_delta_projection, delta),
        (jm.validate_tile_judgment, {**_judgment(), "supporting_evidence": tuple(_evidence(3))}),
        (jm.validate_tile_judgment, duplicate_ref),
        (jm.validate_tile_judgment, duplicate_fingerprint),
        (jm.validate_tile_evidence_delta_projection, {**_delta(), "tile_id": "BAD"}),
        (jm.validate_tile_judgment, {**_judgment(), "canonical_judgment_fingerprint": _hash(99)}),
        (jm.validate_tile_evidence_delta_projection, {**_delta(), "projection_fingerprint": _hash(99)}),
    ):
        with pytest.raises(jm.JudgmentMemoryContractError):
            validate(value)


def test_metadata_axes_are_explicit_and_independent():
    with pytest.raises(jm.JudgmentMemoryContractError):
        _judgment(metadata=False)
    superseded = _judgment(lifecycle_state="superseded", lifecycle_reason="later adoption")
    pending = _judgment(authority_state="pending")
    assert superseded["review_state"] == "none" and superseded["lifecycle_state"] == "superseded"
    assert pending["authority_state"] == "pending" and pending["lifecycle_state"] == "active"


@pytest.mark.parametrize(
    ("build", "changes"),
    [
        (_judgment, {"assessment_state": "unknown"}),
        (_judgment, {"authority_state": "unknown"}),
        (_judgment, {"review_state": "unknown"}),
        (_judgment, {"lifecycle_state": "unknown"}),
        (_delta, {"disposition": "unknown"}),
    ],
)
def test_unknown_enums_fail_closed(build, changes):
    with pytest.raises(jm.JudgmentMemoryContractError):
        build(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"lifecycle_state": "active", "lifecycle_reason": "reason"},
        {"lifecycle_state": "reopened", "lifecycle_reason": ""},
        {"lifecycle_state": "superseded", "lifecycle_reason": ""},
    ],
)
def test_lifecycle_reason_invariant_fails_closed(changes):
    with pytest.raises(jm.JudgmentMemoryContractError):
        _judgment(**changes)


def test_assessment_state_requires_matching_evidence():
    for state in ("ok", "no"):
        assert _judgment(assessment_state=state, evidence=_evidence(1))["assessment_state"] == state
    assert _judgment(assessment_state="sin_evidencia", evidence=[])["assessment_state"] == "sin_evidencia"
    for state, evidence in (("ok", []), ("no", []), ("sin_evidencia", _evidence(1))):
        with pytest.raises(jm.JudgmentMemoryContractError):
            _judgment(assessment_state=state, evidence=evidence)
