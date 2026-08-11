from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from src.services.evidence_memory_identity_v2 import (
    project_evidence_memory_row_identity,
)
from src.services.evidence_vault_authority_profiles import (
    evaluate_reviewed_basis_authority,
)
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_tile,
    build_tile_contract_registry,
    canonical_fingerprint,
    validate_candidate_packet,
)
from src.services.evidence_vault_coverage_supplement import (
    EvidenceVaultCoverageSupplementError,
    build_coverage_supplement_request,
    build_coverage_supplement_source_candidate,
    build_coverage_supplement_source_resolution,
    execute_coverage_supplement,
    validate_coverage_supplement_artifact,
    validate_coverage_supplement_request,
    validate_coverage_supplement_result,
)
from src.services.evidence_vault_operational_authority import (
    build_operational_adoption_event,
    project_adopted_operational_memory,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)
from src.services.scanner_evidence_comparison import (
    canonical_evidence_representatives,
)


_ROOT = Path(__file__).parents[1]
_AUDIT = _ROOT / "audits/evidence_vault_field_validation_v1"
_FIXTURE = _ROOT / "fixtures/evidence_vault_field_validation_v1"
_EXPECTED_AUDIT_FINGERPRINT = (
    "f4709c3522e1bed851fc42a7b4a4f8c9162fa8d5755e5f9938d2fdb557d0ded9"
)


def test_causa_missing_tile_audit_is_bound_to_literal_frozen_evidence() -> None:
    audit = _json(_AUDIT / "causa-prima-missing-tile-independent-audit.json")
    pack = _json(
        _FIXTURE
        / "causa_prima-a8ba05137817-normalized-evidence-pack.json"
    )
    pack_sha = hashlib.sha256(
        json.dumps(
            pack,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert pack_sha == audit["source_evidence_pack_canonical_sha256"]
    assert audit["authority"] is False
    assert audit["runtime_effect"] is False
    assert audit["cutover_authorized"] is False
    assert audit["target_tile_ids"] == [
        "C7", "I9", "MG1", "MG10", "MG3", "MG5", "V3", "VA4"
    ]
    unsigned = {
        key: value for key, value in audit.items() if key != "audit_fingerprint"
    }
    assert audit["audit_fingerprint"] == canonical_fingerprint(
        "evidence-vault-independent-contract-audit-v1",
        unsigned,
    )
    assert audit["audit_fingerprint"] == _EXPECTED_AUDIT_FINGERPRINT

    representatives = canonical_evidence_representatives(
        pack["evidence"],
        subject_url=audit["subject_url"],
    )
    for assessment in audit["tile_assessments"]:
        payload = {
            key: value
            for key, value in assessment.items()
            if key not in {"candidate_id", "review_status", "decision_event_id"}
        }
        assert assessment["candidate_id"] == canonical_fingerprint(
            "evidence-vault-independent-contract-audit-candidate-v1",
            payload,
        )
        assert assessment["review_status"] == "unreviewed"
        assert assessment["decision_event_id"] is None
        assert assessment["adoption_eligible"] is False
        for evidence in assessment["evidence"]:
            row = representatives[evidence["evidence_fingerprint"]]
            identity = project_evidence_memory_row_identity(
                {**row, "evidence_fingerprint": evidence["evidence_fingerprint"]},
                brand_domain=audit["brand_identity"],
            )
            assert identity is not None
            assert evidence["literal_quote"] in row["content"]
            assert evidence["ref"] == row["ref"]
            assert evidence["evidence_id"] == identity["evidence_id"]
            assert evidence["source_identity_id"] == identity["document_id"]

    dispositions = {
        row["tile_id"]: row["proposed_disposition"]
        for row in audit["tile_assessments"]
    }
    assert dispositions == {
        "MG1": "accept",
        "MG3": "accept",
        "MG5": "accept",
        "MG10": "accept",
        "C7": "accept",
        "V3": "sin_evidencia",
        "VA4": "sin_evidencia",
        "I9": "sin_evidencia",
    }


def test_causa_provider_supplement_reviews_preserve_exact_authority_boundaries() -> None:
    replay = _json(_AUDIT / "causa-prima-coverage-supplement-v1.json")
    replay_source = _AUDIT / replay["replay_source_file"]
    assert replay["execution_mode"] == "persisted_relation_replay"
    assert hashlib.sha256(replay_source.read_bytes()).hexdigest() == replay[
        "replay_source_file_sha256"
    ]
    validate_coverage_supplement_request(replay["request"])
    validate_coverage_supplement_result(
        replay["result"], request=replay["request"]
    )

    artifact = _json(_AUDIT / "causa-prima-coverage-supplement-v2.json")
    validate_coverage_supplement_request(artifact["request"])
    validate_coverage_supplement_result(
        artifact["result"],
        request=artifact["request"],
    )
    assert artifact["execution_mode"] == "live_provider"
    assert artifact["authority"] is False
    assert artifact["runtime_effect"] is False
    assert artifact["cutover_authorized"] is False
    assert artifact["worksheet_relation_count"] == 4
    assert {
        row["tile_id"] for row in artifact["result"]["basis_relations"]
    } == {"MG10"}
    assert len({
        row["source_identity_id"]
        for row in artifact["result"]["basis_relations"]
    }) == 4

    with (_AUDIT / "causa-prima-coverage-supplement-review-v2.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        relation_rows = list(csv.DictReader(handle))
    assert len(relation_rows) == 4
    assert {row["human_decision"] for row in relation_rows} == {"accept"}
    assert {row["reviewer_id"] for row in relation_rows} == {"gsus"}
    assert {row["reviewed_at"] for row in relation_rows} == {
        "2026-08-07T11:43:10+02:00"
    }
    assert all(row["human_rationale"].strip() for row in relation_rows)
    assert {row["review_effect"] for row in relation_rows} == {
        "relation_decision_only_no_runtime_effect"
    }
    assert {row["allowed_human_decisions"] for row in relation_rows} == {
        "accept|reject"
    }
    assert {row["relation_id"] for row in relation_rows} == {
        row["relation_id"] for row in artifact["result"]["basis_relations"]
    }

    independent = _json(
        _AUDIT / "causa-prima-missing-tile-independent-audit.json"
    )
    with (_AUDIT / "causa-prima-missing-tile-independent-review.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        review_rows = list(csv.DictReader(handle))
    assert len(review_rows) == 8
    assert {row["human_decision"] for row in review_rows} == {"accept"}
    assert {row["reviewer_id"] for row in review_rows} == {"gsus"}
    assert {row["reviewed_at"] for row in review_rows} == {
        "2026-08-07T11:43:10+02:00"
    }
    assert all(row["human_rationale"].strip() for row in review_rows)
    assert {row["review_effect"] for row in review_rows} == {
        "coverage_assessment_only_no_adoption"
    }
    assert {row["allowed_human_decisions"] for row in review_rows} == {
        "accept|reject"
    }
    assert {row["candidate_id"] for row in review_rows} == {
        row["candidate_id"] for row in independent["tile_assessments"]
    }


def test_causa_coverage_adoption_is_vault_only_and_assessments_stay_out() -> None:
    result = _json(
        _AUDIT / "causa-prima-coverage-human-adoption-result.json"
    )
    summary = _json(
        _AUDIT / "postgres-causa-coverage-adoption-summary.json"
    )
    assert result["reviewer_id"] == "gsus"
    assert result["reviewed_at"] == "2026-08-07T11:43:10+02:00"
    assert result["initial_application"] is True
    assert result["source_replayed_on_first_call"] is False
    assert result["source_replayed_after_adoption"] is True
    assert result["accepted_relation_count"] == 4
    assert result["publisher_source_identity_count"] == 4
    assert result["economic_event_count"] == 1
    assert result["accepted_tile_ids"] == [
        "MG10", "P1", "P3", "P5", "PR3"
    ]
    assert result["accepted_memory_relation_count"] == 11
    assert result["parent_canonical_memory_version"] == (
        "1423ffd3fdce0e7e29d8adce16f91ad1b1f93be29a252aa6f545b809f50f78c4"
    )
    assert result["canonical_memory_version"] == (
        "b1bafe845ab85a888f71b044681eee69cd0548d0462a76d9288ecbc7074b887a"
    )
    assert result["adoption_sequence"] == 2
    assert result["score"] == 6
    assert result["authority_coverage"]["accepted_tile_count"] == 5
    assert result["authority_coverage"]["unresolved_tile_count"] == 75
    assert result["assessment_review"] == {
        "accepted_assessment_count": 8,
        "adoption_eligible_count": 0,
        "persisted_to_vault": False,
        "proposed_dispositions": {
            "C7": "accept",
            "I9": "sin_evidencia",
            "MG1": "accept",
            "MG10": "accept",
            "MG3": "accept",
            "MG5": "accept",
            "V3": "sin_evidencia",
            "VA4": "sin_evidencia",
        },
    }
    assert result["authority"] is True
    assert result["authority_scope"] == "b3s-vault"
    assert result["production_runtime_effect"] is False
    assert result["scanner_runtime_effect"] is False
    assert result["cutover_authorized"] is False
    assert summary["canonical_memory_version"] == result[
        "canonical_memory_version"
    ]
    assert summary["score_evaluation_identity"] == result[
        "score_evaluation_identity"
    ]
    assert summary["coverage_source_authority"] is False
    assert summary["assessment_review_persisted_to_vault"] is False
    assert summary["dump"]["sha256"] == (
        "422f5918b7d2b39250922e1c74717559e9f475c9b72e10813d08bd3fd9c4618b"
    )


class _PersistedRelationLLM:
    api_key = "persisted-test"

    def __init__(self, relations: list[dict]) -> None:
        self._relations = relations

    def _call_json(self, system, user, **kwargs):
        del system, user, kwargs
        return {"relations": self._relations}


def test_coverage_supplement_projects_only_exact_relations_pending() -> None:
    frozen = _json(_AUDIT / "causa-prima-coverage-supplement-v2.json")
    pack = _json(
        _FIXTURE
        / "causa_prima-a8ba05137817-normalized-evidence-pack.json"
    )
    current = _current_memory()
    evidence_rows = frozen["result"]["evidence_snapshot"]
    request = build_coverage_supplement_request(
        brand_identity="causaprima.ai",
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version=current["canonical_memory_version"],
        source_evidence_pack_sha256=frozen["request"][
            "source_evidence_pack_sha256"
        ],
        evidence_rows=evidence_rows,
        tile_shortlists={
            row["evidence_fingerprint"]: row["tile_ids"]
            for row in frozen["request"]["evidence_workset"]
        },
        target_tile_ids=frozen["request"]["target_tile_ids"],
        rationale=frozen["request"]["rationale"],
    )
    result = execute_coverage_supplement(
        request,
        evidence_rows=evidence_rows,
        llm=_PersistedRelationLLM(
            frozen["result"]["relation_proposal"]["relations"]
        ),
    )
    artifact = {
        **frozen,
        "execution_mode": "live_provider",
        "replay_source_result_fingerprint": None,
        "replay_source_file_sha256": None,
        "replay_source_file": None,
        "request": request,
        "result": result,
        "worksheet_relation_count": len(result["basis_relations"]),
    }
    validate_coverage_supplement_artifact(artifact, evidence_pack=pack)

    source = build_coverage_supplement_source_candidate(
        artifact,
        evidence_pack=pack,
        current_operational_memory=current,
    )
    validate_candidate_packet(source)
    by_tile = {row["tile_id"]: row for row in source["candidate_tiles"]}
    assert len(by_tile["MG10"]["basis"]) == 4
    assert {
        row["review_status"] for row in by_tile["MG10"]["basis"]
    } == {"unreviewed"}
    assert by_tile["P1"]["basis"][0]["review_status"] == "accepted"
    resolution = build_coverage_supplement_source_resolution(
        artifact,
        source_candidate_packet=source,
    )
    assert resolution["artifact"] == artifact
    assert resolution["source_kind"] == "coverage_supplement"

    tampered_pack = deepcopy(pack)
    tampered_pack["evidence"][0]["content"] += " tampered"
    with pytest.raises(
        EvidenceVaultCoverageSupplementError,
        match="pack hash mismatch",
    ):
        build_coverage_supplement_source_candidate(
            artifact,
            evidence_pack=tampered_pack,
            current_operational_memory=current,
        )


def _current_memory() -> dict:
    candidates = [
        build_candidate_tile(
            tile_id=row["tile_id"],
            basis=[_accepted_basis()] if row["tile_id"] == "P1" else [],
        )
        for row in build_tile_contract_registry()["tiles"]
    ]
    p1 = next(row for row in candidates if row["tile_id"] == "P1")
    packet = build_operational_memory_packet(
        brand_identity="causaprima.ai",
        source_candidate_packet_fingerprint=_digest("source"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions={
            "P1": {
                "authority_state": "accepted",
                "review_state": "resolved",
                "authority_profile_id": "human-reviewed-relation-v1",
                "authority_source": "human",
                "decision_event_id": "review-current-p1",
                "policy_decision": evaluate_reviewed_basis_authority(
                    candidate_tile=p1
                ),
            }
        },
    )
    event = build_operational_adoption_event(
        packet,
        event_id=str(uuid4()),
        sequence=1,
        previous_event_id=None,
        adopted_by="human",
        actor_id="fixture-reviewer",
        policy_fingerprint=_digest("review-policy"),
        created_at="2026-08-07T10:00:00+02:00",
        idempotency_key_hash=_digest("baseline-adoption"),
        expected_current_canonical_memory_version=None,
    )
    return project_adopted_operational_memory(packet, event)


def _accepted_basis() -> dict:
    return {
        "relation_id": _digest("p1-relation"),
        "evidence_id": _digest("p1-evidence"),
        "source_identity_id": _digest("p1-source"),
        "claim_id": None,
        "polarity": "supports",
        "review_status": "accepted",
        "decision_event_id": "review-current-p1",
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value
