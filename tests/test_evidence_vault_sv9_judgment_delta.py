from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.services import evidence_vault_sv9_judgment_delta as delta
from src.sv9 import incremental_planner as ip
from tests.test_sv9_judgment_memory import _evidence, _judgment, _origin, _series


def _accepted_memory():
    return [_judgment(tile_id=tile, component_key=dict(ip._REGISTRY)[tile]) for tile, _component in ip._REGISTRY]


def _relation(tile, disposition, number):
    return delta.build_authoritative_evidence_tile_relation(
        tile_id=tile,
        component_key=dict(ip._REGISTRY)[tile],
        disposition=disposition,
        evidence_ref=f"evidence:{number}",
        evidence_fingerprint=f"{number:064x}",
        capture_origin=_origin("capture", 9),
        operation_origin=_origin("operation", 10),
    )


def _sentinel(component="mission", **changes):
    reference = next(row for row in _accepted_memory() if row["component_key"] == component)
    return ip.build_component_not_detected_sentinel(
        component_key=component,
        status="not_detected",
        supporting_evidence=[],
        capture_origin=reference["capture_origin"],
        operation_origin=reference["operation_origin"],
        series_contract=reference["series_contract"],
        authority_state="accepted",
        review_state="none",
        lifecycle_state="active",
        lifecycle_reason="",
        **changes,
    )


def _build(*, evidence=(3,), prior=(), sentinels=(), relations=(), series=None):
    return delta.build_evidence_vault_sv9_judgment_delta(
        current_evidence=delta.build_evidence_identity_set(_evidence(*evidence)),
        prior_judgments=list(prior),
        prior_component_sentinels=list(sentinels),
        authoritative_relations=list(relations),
        current_series_contract=_series() if series is None else series,
    )


def test_same_evidence_replays_to_exact_reuse():
    prior = _accepted_memory()
    first = _build(prior=prior)
    assert first["canonical_delta_fingerprint"] == _build(prior=prior)["canonical_delta_fingerprint"]
    assert first["delta_projections"] == [] and first["plan"]["expected_calls"] == 0
    assert {row["action"] for row in first["plan"]["items"]} == {"reuse_canonical"}
    assert delta.validate_evidence_vault_sv9_judgment_delta(first) == first


def test_current_v2_binds_sentinels_and_base_v1_fixture_replays_unchanged():
    sentinel = _sentinel()
    current = _build(
        prior=[row for row in _accepted_memory() if row["component_key"] != "mission"], sentinels=[sentinel]
    )
    assert current["schema_version"] == delta.JUDGMENT_DELTA_VERSION
    assert current["plan"]["schema_version"] == ip.PLAN_VERSION
    assert current["prior_component_sentinels"] == current["plan"]["prior_component_sentinels"] == [sentinel]
    assert current == _build(
        prior=[row for row in _accepted_memory() if row["component_key"] != "mission"], sentinels=[sentinel]
    )
    assert delta.validate_evidence_vault_sv9_judgment_delta(current) == current
    fixture = json.loads(Path("tests/fixtures/evidence_vault_sv9_judgment_delta_v1_rollover_review.json").read_text())
    legacy = delta.validate_evidence_vault_sv9_judgment_delta(fixture)
    assert legacy == fixture and legacy["schema_version"] == delta.LEGACY_JUDGMENT_DELTA_VERSION
    assert legacy["plan"]["schema_version"] == ip.LEGACY_PLAN_VERSION
    assert legacy["plan"]["review_set"] == ["M1", "A1"] and len(legacy["plan"]["tile_workset"]) == 78


def test_one_evidence_identity_can_target_multiple_tiles_and_coherencia():
    result = _build(
        evidence=(3, 9),
        prior=_accepted_memory(),
        relations=[_relation("M1", "relevant", 9), _relation("A1", "relevant", 9)],
    )
    assert [row["tile_id"] for row in result["delta_projections"]] == ["M1", "A1"]
    assert result["plan"]["tile_workset"] == ["M1", "A1", *ip._COMPONENT_TILES["coherencia"]]
    assert result["plan"]["component_workset"] == ["mission", "attributes", "coherencia"]
    assert result["plan"]["expected_calls"] == 3


def test_unmapped_new_evidence_requires_review_without_automatic_calls():
    result = _build(evidence=(3, 9), prior=_accepted_memory())
    assert result["unmapped_evidence"] == [
        {"evidence_ref": "evidence:9", "evidence_fingerprint": f"{9:064x}", "reason": "unmapped_current_evidence"}
    ]
    assert result["delta_projections"] == [] and result["plan"]["expected_calls"] == 0


def test_missing_support_is_coverage_loss_and_does_not_reopen_a_tile():
    result = _build(evidence=(), prior=_accepted_memory())
    item = next(row for row in result["plan"]["items"] if row["tile_id"] == "M1")
    assert item["action"] == "reuse_canonical" and result["plan"]["expected_calls"] == 0
    assert result["coverage_loss"][0]["reason"] == "supporting_evidence_missing"
    assert result["coverage_loss"][0]["missing_evidence"] == _evidence(3)


def test_contradiction_preserves_the_prior_and_makes_no_replacement_call():
    prior = _accepted_memory()
    snapshot = deepcopy(prior)
    result = _build(evidence=(3, 9), prior=prior, relations=[_relation("M1", "contradiction", 9)])
    item = next(row for row in result["plan"]["items"] if row["tile_id"] == "M1")
    assert prior == snapshot
    assert item["action"] == "reopen_contradiction" and not item["expected_call_contribution"]
    assert item["reused_judgment_fingerprint"] is None and result["plan"]["expected_calls"] == 0


def test_version_mismatch_creates_a_candidate_without_mutating_active_values():
    prior = _accepted_memory()
    snapshot = deepcopy(prior)
    result = _build(prior=prior, series=_series(prompt_version="prompt-v2"))
    assert prior == snapshot
    assert result["plan"]["candidate_series_fingerprint"] != result["plan"]["current_series_fingerprint"]
    assert {row["action"] for row in result["plan"]["items"]} == {"reopen_contract_change"}


def test_first_run_remains_the_complete_existing_evaluation_path():
    result = _build(evidence=(9,))
    assert len(result["plan"]["tile_workset"]) == 80
    assert result["plan"]["expected_calls"] == 10
    assert result["unmapped_evidence"][0]["reason"] == "unmapped_current_evidence"


def test_malformed_or_ambiguous_relation_contracts_fail_closed():
    current = delta.build_evidence_identity_set(_evidence(3, 9))
    first = _relation("M1", "relevant", 9)
    for invalid in (_relation("M1", "relevant", 9), _relation("M1", "human_review_required", 9)):
        with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
            delta.build_evidence_vault_sv9_judgment_delta(
                current_evidence=current,
                prior_judgments=_accepted_memory(),
                authoritative_relations=[first, invalid],
                current_series_contract=_series(),
            )
    with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
        _build(evidence=(3,), prior=_accepted_memory(), relations=[first])
    tampered = _build(prior=_accepted_memory())
    tampered["unmapped_evidence"] = [{"reason": "tampered"}]
    with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
        delta.validate_evidence_vault_sv9_judgment_delta(tampered)


def test_signed_replay_rejects_nested_nonbuiltin_json_values():
    mapping, sequence = type("Mapping", (dict,), {}), type("Sequence", (list,), {})
    identity = delta.build_evidence_identity_set(_evidence(3))
    identity["evidence"] = sequence(identity["evidence"])
    with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
        delta.build_evidence_identity_set(_raw=identity, _signed=True)
    relation = _relation("M1", "relevant", 3)
    relation["capture_origin"] = mapping(relation["capture_origin"])
    with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
        delta.build_authoritative_evidence_tile_relation(_raw=relation, _signed=True)

    def reject(value):
        with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
            delta.validate_evidence_vault_sv9_judgment_delta(value)

    signed = _build(prior=_accepted_memory())
    series = deepcopy(signed)
    series["current_series_contract"] = mapping(series["current_series_contract"])
    reject(series)
    judgment = deepcopy(signed)
    judgment["prior_judgments"][0] = mapping(judgment["prior_judgments"][0])
    reject(judgment)
    evidence = deepcopy(signed)
    evidence["prior_judgments"][0]["supporting_evidence"] = sequence(
        evidence["prior_judgments"][0]["supporting_evidence"]
    )
    reject(evidence)
    reject(mapping(signed))
