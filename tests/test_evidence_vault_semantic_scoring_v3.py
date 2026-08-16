from copy import deepcopy
from hashlib import sha256
from types import MappingProxyType

import pytest

from src.services.evidence_vault_candidate_resolver import canonical_aggregation_policy_fingerprint
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_tile_contract_registry,
)
import src.services.evidence_vault_semantic_scoring_v3 as scoring
from src.services.evidence_vault_operational_assessment_shadow import (
    build_operational_semantic_shadow_assessment,
)
from src.services.evidence_vault_operational_memory import build_operational_memory_packet
from src.sv9.assessment_kernel import build_sv9_assessment


_validate = scoring.validate_evidence_vault_semantic_assessment


def test_extracts_only_semantics_and_calls_kernel_once(monkeypatch) -> None:
    source = _packet({"M1": "ok", "C8": "no"})
    real_build = scoring.build_sv9_assessment
    calls = []

    def spy(rows):
        calls.append(rows)
        return real_build(rows)

    monkeypatch.setattr(scoring, "build_sv9_assessment", spy)
    result = scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=source)
    expected = _kernel_rows(source)
    assert calls == [expected]
    _validate(result, source_candidate_packet=source)


def test_contradiction_is_exact_unavailable_and_never_calls_kernel(monkeypatch) -> None:
    source = _packet({"C8": "contradiction"})

    def bomb(_rows):
        raise AssertionError("kernel must not run")

    monkeypatch.setattr(scoring, "build_sv9_assessment", bomb)
    result = scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=source)
    _validate(result, source_candidate_packet=source)

    assert result == {
        "schema_version": scoring.EVIDENCE_VAULT_SEMANTIC_ASSESSMENT_VERSION,
        "observation_identity": scoring.canonical_fingerprint(
            scoring.EVIDENCE_VAULT_SEMANTIC_ASSESSMENT_VERSION,
            {"source_candidate_packet_fingerprint": source["candidate_packet_fingerprint"]},
        ),
        "source_candidate_packet_fingerprint": source["candidate_packet_fingerprint"],
        "availability": "unavailable",
        "reason_codes": ["contradiction_requires_semantic_reassessment"],
        "score_input_fingerprint": None,
        "semantic_provenance_fingerprint": None,
        "assessment_output": None,
    }


def test_metadata_changes_only_source_and_observation_identity() -> None:
    source = _packet({"M1": "ok"}, "a")
    first = scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=source)
    second = scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=_packet({"M1": "ok"}, "b"))

    assert {key for key in first if first[key] != second[key]} == {
        "observation_identity",
        "source_candidate_packet_fingerprint",
    }
    assert scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=MappingProxyType(source)) == first


class _ChangingRows(list):
    def __iter__(self):
        self.iterations = getattr(self, "iterations", 0) + 1
        rows = deepcopy(list(list.__iter__(self)))
        if self.iterations > 1:
            for row in rows:
                row["candidate_state"] = "ok"
        return iter(rows)


class _FacadeDict(dict):
    def __getitem__(self, key):
        return self.facade[key]


def test_boundaries_detach_dynamic_containers_and_wrap_mixed_keys() -> None:
    dynamic_source = _packet({})
    changing = _ChangingRows(dynamic_source["candidate_tiles"])
    dynamic_source["candidate_tiles"] = changing
    snapshot = scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=dynamic_source)
    assert changing.iterations == 1
    assert snapshot["assessment_output"]["sv9_score"] == 0

    stable_source = _packet({})
    exact = scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=stable_source)
    all_ok = {row["tile_id"]: "ok" for row in build_tile_contract_registry()["tiles"]}
    perfect = scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=_packet(all_ok))
    facade = _FacadeDict(perfect["assessment_output"])
    facade.facade = exact["assessment_output"]
    forged = {**exact, "assessment_output": facade}
    bool_field, float_count, surrogate = (_packet({}) for _ in range(3))
    bool_field["candidate_tiles"][0]["single_source_dependency"] = 0
    float_count["manifest"]["candidate_count"] = 80.0
    float_count["candidate_packet_fingerprint"] = scoring.canonical_fingerprint(
        scoring.EVIDENCE_VAULT_CANDIDATE_PACKET_FINGERPRINT_VERSION,
        {"manifest": float_count["manifest"], "candidate_tiles": float_count["candidate_tiles"]},
    )
    surrogate["manifest"]["brand_identity"] = "\ud800"
    missing = dict(exact)
    del missing["reason_codes"]
    altered = [forged, missing, {**exact, "extra": None}]
    for value in (True, float("nan"), object()):
        nested = deepcopy(exact)
        nested["assessment_output"]["sv9_score"] = value
        altered.append(nested)
    for invalid in altered:
        with pytest.raises(scoring.EvidenceVaultSemanticScoringV3Error):
            _validate(invalid, source_candidate_packet=stable_source)
    for invalid in ({**_packet({}), 42: None}, bool_field, float_count, surrogate):
        with pytest.raises(scoring.EvidenceVaultSemanticScoringV3Error):
            scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=invalid)


def test_kernel_and_current_operational_shadow_parity_across_vectors() -> None:
    contracts = build_tile_contract_registry()["tiles"]
    magnetism = [row["tile_id"] for row in contracts if row["component_key"] == "magnetism"]
    vectors = [
        ({}, 0, False),
        (dict.fromkeys(magnetism[:5], "ok"), 10, False),
        (dict.fromkeys(magnetism[:6], "ok"), 10, True),
        ({"C7": "ok", "C8": "ok"}, 4, False),
        ({"M1": "no"}, 0, False),
    ]
    results = []
    for states, expected_score, capped in vectors:
        source = _packet(states)
        result = scoring.build_evidence_vault_semantic_assessment(source_candidate_packet=source)
        direct = build_sv9_assessment(_kernel_rows(source))
        operational = build_operational_memory_packet(
            brand_identity="example.com",
            source_candidate_packet_fingerprint=source["candidate_packet_fingerprint"],
            aggregation_policy_fingerprint=source["manifest"]["aggregation_policy_fingerprint"],
            candidate_tiles=source["candidate_tiles"],
        )
        shadow = build_operational_semantic_shadow_assessment(
            operational_packet=operational,
            source_candidate_packet=source,
            expected_parent_canonical_memory_version=operational["current_canonical_memory_version"],
        )
        assert result["assessment_output"] == direct == shadow["assessment_output"]
        assert (direct["sv9_score"], direct["magnetism_capped"]) == (expected_score, capped)
        results.append(result)
    assert results[0]["score_input_fingerprint"] != results[-1]["score_input_fingerprint"]


def _packet(states: dict[str, str], tag: str = "a") -> dict:
    tiles, unresolved = [], []
    for contract in build_tile_contract_registry()["tiles"]:
        tile_id = contract["tile_id"]
        state = states.get(tile_id, "sin_evidencia")
        polarities = {
            "ok": ["supports"],
            "no": ["contradicts"],
            "sin_evidencia": [],
            "contradiction": ["supports", "contradicts"],
        }[state]
        refs = [f"conflict-{tile_id}"] if state == "contradiction" else []
        tiles.append(
            build_candidate_tile(
                tile_id=tile_id, basis=[_basis(tile_id, polarity, tag) for polarity in polarities], unresolved_refs=refs
            )
        )
        unresolved.extend(
            {"unresolved_id": ref, "kind": "contradiction", "blocking": True, "details": {}} for ref in refs
        )
    return build_candidate_packet(
        brand_identity="example.com",
        parent_canonical_memory_version=None,
        candidate_memory_version=_digest(f"candidate-{tag}"),
        accepted_memory_candidate_version=_digest(f"accepted-{tag}"),
        reviewed_memory_candidate_version=_digest(f"reviewed-{tag}"),
        review_packet_set_fingerprint=_digest(f"review-{tag}"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=tiles,
        unresolved_items=unresolved,
    )


def _basis(tile_id: str, polarity: str, tag: str) -> dict:
    seed = f"{tag}-{tile_id}-{polarity}"
    return {
        "relation_id": _digest(f"relation-{seed}"),
        "evidence_id": _digest(f"evidence-{seed}"),
        "source_identity_id": _digest(f"source-{seed}"),
        "claim_id": None,
        "polarity": polarity,
        "review_status": "accepted",
        "decision_event_id": f"decision-{seed}",
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _kernel_rows(source: dict) -> list[dict]:
    return [
        {
            "component_key": row["component_key"],
            "tile_id": row["tile_id"],
            "tile_key": row["tile_key"],
            "assessment_state": row["candidate_state"],
        }
        for row in source["candidate_tiles"]
    ]


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()
