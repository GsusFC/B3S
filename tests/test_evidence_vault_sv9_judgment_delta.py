from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.services import evidence_vault_sv9_judgment_delta as delta
from src.services import evidence_vault_sv9_support_continuity as continuity
from src.sv9 import incremental_planner as ip
from tests.test_sv9_judgment_memory import _evidence, _hash, _judgment, _origin, _series


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


def _memory(support=None):
    # Every tile cites the present pair 5 unless the caller moves a tile's support.
    support = {} if support is None else support
    return [
        _judgment(tile_id=tile, component_key=component, evidence=support.get(tile, _evidence(5)))
        for tile, component in ip._REGISTRY
    ]


def _sorted(*numbers):
    return sorted(_evidence(*numbers), key=lambda row: (row["evidence_ref"], row["evidence_fingerprint"]))


def _carried(tile, source, *targets, similarity=None):
    entry = {
        "tile_id": tile,
        "source": _evidence(source)[0],
        "tier": "canonical" if similarity is None else "owned_page_similarity",
        "targets": _evidence(*targets),
    }
    if similarity is not None:
        entry["similarity"] = {
            "source_shingle_digest": _hash(0xA1),
            "target_shingle_digest": _hash(0xB2),
            **similarity,
        }
    return entry


def _continuity(*carried, rule_version=continuity.SUPPORT_CONTINUITY_RULE_VERSION):
    return {
        "rule_version": rule_version,
        "capture_origin": _origin("capture", 9),
        "operation_origin": _origin("operation", 10),
        "carried": list(carried),
    }


def _build_v4(*, evidence=(5, 9), prior=None, relations=(), support_continuity=None, sentinels=()):
    return delta.build_evidence_vault_sv9_judgment_delta(
        current_evidence=delta.build_evidence_identity_set(_evidence(*evidence)),
        prior_judgments=_memory({"M1": _evidence(3)}) if prior is None else prior,
        prior_component_sentinels=list(sentinels),
        authoritative_relations=list(relations),
        current_series_contract=_series(),
        support_continuity=_continuity(_carried("M1", 3, 9)) if support_continuity is None else support_continuity,
    )


def test_frozen_v2_fixture_replays_unchanged_and_builds_without_continuity_stay_v2():
    fixture = json.loads(Path("tests/fixtures/evidence_vault_sv9_judgment_delta_v2_sentinel_relevant.json").read_text())
    replayed = delta.validate_evidence_vault_sv9_judgment_delta(fixture)
    assert replayed == fixture and replayed["schema_version"] == delta.JUDGMENT_DELTA_VERSION == "evidence-vault-sv9-judgment-delta-v2"
    assert "support_continuity" not in replayed and replayed["plan"]["expected_calls"] == 2
    assert _build(prior=_accepted_memory())["schema_version"] == "evidence-vault-sv9-judgment-delta-v2"


def test_v4_carries_moved_support_into_one_relevant_projection_and_validates():
    result = _build_v4()

    assert result["schema_version"] == delta.SUPPORT_CONTINUITY_JUDGMENT_DELTA_VERSION == "evidence-vault-sv9-judgment-delta-v4"
    assert result["support_continuity"] == _continuity(_carried("M1", 3, 9))
    assert result["coverage_loss"] == [] and result["unmapped_evidence"] == []
    assert [row["tile_id"] for row in result["delta_projections"]] == ["M1"]
    projection = result["delta_projections"][0]
    assert projection["disposition"] == "relevant" and projection["evidence"] == _evidence(9)
    assert projection["capture_origin"] == _origin("capture", 9) and projection["operation_origin"] == _origin("operation", 10)
    item = next(row for row in result["plan"]["items"] if row["tile_id"] == "M1")
    assert item["action"] == "evaluate_delta" and item["evidence"] == _evidence(9)
    assert result["plan"]["tile_workset"] == ["M1", *ip._COMPONENT_TILES["coherencia"]]
    assert delta.validate_evidence_vault_sv9_judgment_delta(json.loads(json.dumps(result))) == result
    assert result == _build_v4()
    assert result["canonical_delta_fingerprint"] != _build_v4(support_continuity=_continuity())["canonical_delta_fingerprint"]


def test_v4_without_carry_keeps_coverage_loss_and_unmapped_semantics():
    result = _build_v4(support_continuity=_continuity())
    assert result["schema_version"] == delta.SUPPORT_CONTINUITY_JUDGMENT_DELTA_VERSION
    assert [row["tile_id"] for row in result["coverage_loss"]] == ["M1"]
    assert result["unmapped_evidence"] == [{**_evidence(9)[0], "reason": "unmapped_current_evidence"}]
    assert result["delta_projections"] == [] and result["plan"]["expected_calls"] == 0
    assert delta.validate_evidence_vault_sv9_judgment_delta(result) == result


def test_v4_merges_present_support_carried_targets_and_relevant_relations_per_tile():
    prior = _memory({"M1": _evidence(3, 5)})
    relation = _relation("M1", "relevant", 11)
    result = _build_v4(evidence=(5, 9, 11), prior=prior, relations=[relation])

    assert [row["tile_id"] for row in result["delta_projections"]] == ["M1"]
    assert result["delta_projections"][0]["evidence"] == _sorted(5, 9, 11)
    assert result["coverage_loss"] == [] and result["unmapped_evidence"] == []
    similarity = _build_v4(
        evidence=(5, 9, 12),
        prior=_memory({"M1": _evidence(3), "A1": _evidence(4)}),
        support_continuity=_continuity(
            _carried("M1", 3, 9, 12, similarity={"intersection": 112, "union": 121}),
            _carried("A1", 4, 9, 12, similarity={"intersection": 112, "union": 121}),
        ),
    )
    assert [(row["tile_id"], row["evidence"]) for row in similarity["delta_projections"]] == [("M1", _sorted(9, 12)), ("A1", _sorted(9, 12))]
    assert similarity["coverage_loss"] == [] and similarity["unmapped_evidence"] == []
    assert delta.validate_evidence_vault_sv9_judgment_delta(similarity) == similarity
    reversed_input = _build_v4(
        evidence=(12, 9, 5),
        prior=list(reversed(_memory({"M1": _evidence(3), "A1": _evidence(4)}))),
        support_continuity=_continuity(
            _carried("A1", 4, 12, 9, similarity={"intersection": 112, "union": 121}),
            _carried("M1", 3, 12, 9, similarity={"intersection": 112, "union": 121}),
        ),
    )
    assert reversed_input == similarity


def test_v4_carried_tile_with_contradiction_relation_stays_in_review():
    result = _build_v4(relations=[_relation("M1", "contradiction", 9)])
    item = next(row for row in result["plan"]["items"] if row["tile_id"] == "M1")
    assert item["action"] == "reopen_contradiction" and "M1" in result["plan"]["review_set"]
    assert [row["disposition"] for row in result["delta_projections"]] == ["contradiction"]
    assert result["coverage_loss"] == []


@pytest.mark.parametrize(
    ("label", "support_continuity"),
    [
        ("source outside the tile support", _continuity(_carried("M2", 3, 9))),
        ("source still present exactly", _continuity(_carried("M1", 5, 9))),
        ("target is not a current record", _continuity(_carried("M1", 3, 7))),
        ("integers below threshold", _continuity(_carried("M1", 3, 9, similarity={"intersection": 79, "union": 100}))),
        ("union below the page minimum", _continuity(_carried("M1", 3, 9, similarity={"intersection": 19, "union": 19}))),
        ("intersection above union", _continuity(_carried("M1", 3, 9, similarity={"intersection": 101, "union": 100}))),
        ("boolean integers", _continuity(_carried("M1", 3, 9, similarity={"intersection": True, "union": True}))),
        ("unknown rule version", _continuity(_carried("M1", 3, 9), rule_version="evidence-vault-sv9-support-continuity-rule-v0")),
        ("unknown tier", _continuity({**_carried("M1", 3, 9), "tier": "exact"})),
        ("canonical with two targets", _continuity(_carried("M1", 3, 9, 11))),
        ("canonical carrying similarity", _continuity({**_carried("M1", 3, 9), "similarity": _carried("M1", 3, 9, similarity={"intersection": 1, "union": 1})["similarity"]})),
        ("similarity without integers", _continuity({**_carried("M1", 3, 9, similarity={"intersection": 9, "union": 10}), "similarity": {"source_shingle_digest": _hash(1), "target_shingle_digest": _hash(2)}})),
        ("duplicate entry", _continuity(_carried("M1", 3, 9), _carried("M1", 3, 9))),
        ("malformed origin", {**_continuity(_carried("M1", 3, 9)), "operation_origin": {"operation_id": "operation-10"}}),
        ("extra field", {**_continuity(_carried("M1", 3, 9)), "notes": "forged"}),
        ("empty targets", _continuity({**_carried("M1", 3, 9), "targets": []})),
        ("unknown tile", _continuity(_carried("Z9", 3, 9))),
    ],
)
def test_forged_v4_support_continuity_is_rejected(label, support_continuity):
    prior = _memory({"M1": _evidence(3, 5)})
    with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
        _build_v4(evidence=(5, 9, 11), prior=prior, support_continuity=support_continuity)
    signed = _build_v4(evidence=(5, 9, 11), prior=prior)
    forged = deepcopy(signed)
    forged["support_continuity"] = support_continuity
    forged["canonical_delta_fingerprint"] = delta.jm.canonical_fingerprint(
        delta._SUPPORT_CONTINUITY_DELTA_FINGERPRINT,
        {key: raw for key, raw in forged.items() if key != "canonical_delta_fingerprint"},
    )
    with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
        delta.validate_evidence_vault_sv9_judgment_delta(forged)


def test_v4_signed_replay_rejects_tampered_carried_targets_and_missing_field():
    signed = _build_v4()
    tampered = deepcopy(signed)
    tampered["support_continuity"]["carried"][0]["targets"] = _evidence(5)
    with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
        delta.validate_evidence_vault_sv9_judgment_delta(tampered)
    stripped = deepcopy(signed)
    stripped.pop("support_continuity")
    with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
        delta.validate_evidence_vault_sv9_judgment_delta(stripped)
    downgraded = deepcopy(signed)
    downgraded["schema_version"] = delta.JUDGMENT_DELTA_VERSION
    with pytest.raises(delta.EvidenceVaultSV9JudgmentDeltaError):
        delta.validate_evidence_vault_sv9_judgment_delta(downgraded)
