from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_tile,
    build_tile_contract_registry,
    validate_candidate_packet,
)
from src.services.evidence_vault_exact_relation_supplement import (
    EXACT_RELATION_ATOMIC_REVIEW_EFFECT,
    EXACT_RELATION_COMPOSITE_REVIEW_EFFECT,
    EvidenceVaultExactRelationSupplementError,
    build_exact_relation_review_worksheets,
    build_exact_relation_source_candidate,
    build_exact_relation_source_resolution,
    build_exact_relation_supplement_artifact,
    validate_exact_relation_source_decisions,
    validate_exact_relation_supplement_artifact,
)
from src.services.evidence_vault_operational_authority import (
    build_operational_adoption_event,
    project_adopted_operational_memory,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)
from src.services.evidence_vault_operational_review import (
    EvidenceVaultOperationalReviewError,
    build_reviewed_operational_source,
)


_ROOT = Path(__file__).parents[1]
_AUDIT = _ROOT / "audits/evidence_vault_field_validation_v1"
_FIXTURE = _ROOT / "fixtures/evidence_vault_field_validation_v1"
_EXPECTED_ARTIFACT_FINGERPRINT = (
    "13a1677eb3acfd2e51e8dd7be1c455be43e94358ad76cb79af0c88a6ce3ac795"
)


def test_reviewed_assessments_project_to_exact_pending_relations() -> None:
    artifact, assessment, rows, pack, worksheet_sha = _sources()
    validate_exact_relation_supplement_artifact(
        artifact,
        assessment_artifact=assessment,
        assessment_review_rows=rows,
        assessment_worksheet_sha256=worksheet_sha,
        evidence_pack=pack,
    )

    assert artifact["artifact_fingerprint"] == _EXPECTED_ARTIFACT_FINGERPRINT
    assert artifact["parent_canonical_memory_version"] == (
        "b1bafe845ab85a888f71b044681eee69cd0548d0462a76d9288ecbc7074b887a"
    )
    assert artifact["selected_tile_ids"] == ["C7", "MG1", "MG3", "MG5"]
    assert artifact["relation_count"] == 5
    assert artifact["adoption_eligible"] is False
    assert artifact["authority"] is False
    assert artifact["runtime_effect"] is False
    assert artifact["cutover_authorized"] is False

    groups = {row["tile_id"]: row for row in artifact["groups"]}
    assert {key: value["decision_rule"] for key, value in groups.items()} == {
        "C7": "all_of",
        "MG1": "atomic",
        "MG3": "atomic",
        "MG5": "atomic",
    }
    relation_ids = {
        relation["relation_id"]
        for group in groups.values()
        for relation in group["relations"]
    }
    assert len(relation_ids) == 5
    assert all(
        relation["review_status"] == "unreviewed"
        and relation["decision_event_id"] is None
        for group in groups.values()
        for relation in group["relations"]
    )
    assert all(
        relation["claim_id"] is None
        for tile_id in ("MG1", "MG3", "MG5")
        for relation in groups[tile_id]["relations"]
    )


def test_c7_is_one_all_of_decision_over_two_distinct_channels() -> None:
    artifact, *_ = _sources()
    c7 = next(row for row in artifact["groups"] if row["tile_id"] == "C7")

    assert c7["group_contract"] == {
        "decision_rule": "all_of",
        "minimum_member_count": 2,
        "required_distinct_source_identity_count": 2,
        "required_channel_roles": [
            "external_social_profile",
            "owned_web",
        ],
        "member_decisions_must_match": True,
        "member_change_requires_review": True,
    }
    assert {row["channel_role"] for row in c7["relations"]} == {
        "owned_web",
        "external_social_profile",
    }
    assert len({row["source_identity_id"] for row in c7["relations"]}) == 2
    assert {row["claim_id"] for row in c7["relations"]} == {c7["group_id"]}


def test_exact_relation_worksheets_preserve_separate_review_scopes() -> None:
    artifact, *_ = _sources()
    atomic, composite = build_exact_relation_review_worksheets(artifact)

    assert len(atomic) == 3
    assert {row["tile_id"] for row in atomic} == {"MG1", "MG3", "MG5"}
    assert {row["review_effect"] for row in atomic} == {
        EXACT_RELATION_ATOMIC_REVIEW_EFFECT
    }
    assert len(composite) == 1
    assert composite[0]["tile_id"] == "C7"
    assert composite[0]["decision_rule"] == "all_of"
    assert composite[0]["review_effect"] == (
        EXACT_RELATION_COMPOSITE_REVIEW_EFFECT
    )
    assert all(not row["human_decision"] for row in [*atomic, *composite])


def test_exact_relation_source_keeps_groups_pending_and_parent_unchanged() -> None:
    _, assessment, rows, pack, worksheet_sha = _sources()
    current = _current_memory()
    artifact = build_exact_relation_supplement_artifact(
        brand_identity="causaprima.ai",
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version=current["canonical_memory_version"],
        selected_tile_ids=["C7", "MG1", "MG3", "MG5"],
        assessment_artifact=assessment,
        assessment_review_rows=rows,
        assessment_worksheet_sha256=worksheet_sha,
        evidence_pack=pack,
    )

    source = build_exact_relation_source_candidate(
        artifact,
        current_operational_memory=current,
    )
    validate_candidate_packet(source)
    by_tile = {row["tile_id"]: row for row in source["candidate_tiles"]}
    assert by_tile["P1"]["basis"][0]["review_status"] == "accepted"
    assert sum(
        len(
            [
                relation
                for relation in by_tile[tile_id]["basis"]
                if relation["review_status"] == "unreviewed"
            ]
        )
        for tile_id in ("C7", "MG1", "MG3", "MG5")
    ) == 5
    c7_claim_ids = {
        relation["claim_id"] for relation in by_tile["C7"]["basis"]
    }
    assert c7_claim_ids == {
        next(group for group in artifact["groups"] if group["tile_id"] == "C7")[
            "group_id"
        ]
    }
    resolution = build_exact_relation_source_resolution(
        artifact,
        source_candidate_packet=source,
    )
    assert resolution["source_kind"] == "exact_relation_supplement"
    assert resolution["artifact"] == artifact
    assert len(resolution["decision_groups"]) == 4


def test_c7_all_of_decision_cannot_be_split_or_bypassed() -> None:
    _, assessment, rows, pack, worksheet_sha = _sources()
    current = _current_memory()
    artifact = build_exact_relation_supplement_artifact(
        brand_identity="causaprima.ai",
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version=current["canonical_memory_version"],
        selected_tile_ids=["C7", "MG1", "MG3", "MG5"],
        assessment_artifact=assessment,
        assessment_review_rows=rows,
        assessment_worksheet_sha256=worksheet_sha,
        evidence_pack=pack,
    )
    source = build_exact_relation_source_candidate(
        artifact,
        current_operational_memory=current,
    )
    pending = {
        relation["relation_id"]: {
            "decision": "accept",
            "decision_event_id": f"event-{index}",
        }
        for index, tile in enumerate(source["candidate_tiles"])
        for relation in tile["basis"]
        if relation["review_status"] == "unreviewed"
    }
    validate_exact_relation_source_decisions(source, pending)
    reviewed, _ = build_reviewed_operational_source(
        source,
        decisions=pending,
        current_operational_memory=current,
    )
    reviewed_c7 = next(
        row for row in reviewed["candidate_tiles"] if row["tile_id"] == "C7"
    )
    assert {row["review_status"] for row in reviewed_c7["basis"]} == {"accepted"}

    c7 = next(group for group in artifact["groups"] if group["tile_id"] == "C7")
    mixed = deepcopy(pending)
    mixed[c7["relations"][0]["relation_id"]]["decision"] = "reject"
    with pytest.raises(
        EvidenceVaultOperationalReviewError,
        match="all-of exact relation members require one matching decision",
    ):
        build_reviewed_operational_source(
            source,
            decisions=mixed,
            current_operational_memory=current,
        )


def test_completed_exact_scope_reviews_preserve_the_generated_contract() -> None:
    artifact, *_ = _sources()
    expected_atomic, expected_composite = build_exact_relation_review_worksheets(
        artifact
    )
    atomic_path = _AUDIT / "causa-prima-magnetism-strict-relation-review.csv"
    composite_path = _AUDIT / "causa-prima-c7-composite-review.csv"
    with atomic_path.open(encoding="utf-8", newline="") as handle:
        atomic = list(csv.DictReader(handle))
    with composite_path.open(encoding="utf-8", newline="") as handle:
        composite = list(csv.DictReader(handle))

    mutable = {
        "human_decision",
        "human_rationale",
        "reviewer_id",
        "reviewed_at",
    }
    assert [
        {key: value for key, value in row.items() if key not in mutable}
        for row in atomic
    ] == [
        {key: value for key, value in row.items() if key not in mutable}
        for row in expected_atomic
    ]
    assert [
        {key: value for key, value in row.items() if key not in mutable}
        for row in composite
    ] == [
        {key: value for key, value in row.items() if key not in mutable}
        for row in expected_composite
    ]
    assert {row["human_decision"] for row in [*atomic, *composite]} == {
        "accept"
    }
    assert {row["reviewer_id"] for row in [*atomic, *composite]} == {"gsus"}
    assert {row["reviewed_at"] for row in [*atomic, *composite]} == {
        "2026-08-07T13:21:02+02:00"
    }
    assert hashlib.sha256(atomic_path.read_bytes()).hexdigest() == (
        "a1cdc29509ae720e89289c2b4620ceb8031093bea9a0a87f30d7de4112c12c11"
    )
    assert hashlib.sha256(composite_path.read_bytes()).hexdigest() == (
        "ed048d519004fd443007f406c7b2c1b93fc8331615290ace5d8df94e5ca26f68"
    )


def test_exact_scope_field_adoption_is_sparse_v2_not_legacy_magnetism() -> None:
    result = json.loads(
        (
            _AUDIT
            / "causa-prima-exact-relation-human-adoption-result.json"
        ).read_text(encoding="utf-8")
    )
    summary = json.loads(
        (
            _AUDIT
            / "postgres-causa-exact-relation-adoption-summary.json"
        ).read_text(encoding="utf-8")
    )

    assert result["parent_canonical_memory_version"] == (
        "b1bafe845ab85a888f71b044681eee69cd0548d0462a76d9288ecbc7074b887a"
    )
    assert result["canonical_memory_version"] == (
        "7b94b1062aa01b1cb259944b3cd67bef0fac614a130d90e65326b53d169b1a7f"
    )
    assert result["adoption_sequence"] == 3
    assert result["score_before"] == 6
    assert result["score"] == 14
    assert result["accepted_relation_count"] == 5
    assert result["review_event_count"] == 5
    assert result["accepted_group_tile_ids"] == ["C7", "MG1", "MG3", "MG5"]
    assert result["production_runtime_effect"] is False
    assert result["scanner_runtime_effect"] is False
    assert summary["magnetism_accepted_tile_ids"] == [
        "MG1",
        "MG10",
        "MG3",
        "MG5",
    ]
    assert summary["mg7_accepted"] is False
    assert summary["mg8_accepted"] is False
    assert summary["magnetism_breakdown"]["ok_count"] == 4
    assert summary["magnetism_breakdown"]["points"] == 8
    assert summary["c7"]["basis_relation_count"] == 2
    assert summary["c7"]["distinct_source_identity_count"] == 2
    assert summary["c7"]["decision_rule"] == "all_of"


def test_exact_relation_artifact_rejects_quote_or_review_drift() -> None:
    artifact, assessment, rows, pack, worksheet_sha = _sources()
    tampered = deepcopy(artifact)
    tampered["groups"][0]["relations"][0]["literal_quote"] += " changed"
    with pytest.raises(
        EvidenceVaultExactRelationSupplementError,
        match="artifact fingerprint mismatch|differs from its sources",
    ):
        validate_exact_relation_supplement_artifact(
            tampered,
            assessment_artifact=assessment,
            assessment_review_rows=rows,
            assessment_worksheet_sha256=worksheet_sha,
            evidence_pack=pack,
        )

    altered_rows = deepcopy(rows)
    altered_rows[0]["literal_quotes"] = "[]"
    with pytest.raises(
        EvidenceVaultExactRelationSupplementError,
        match="immutable columns changed",
    ):
        validate_exact_relation_supplement_artifact(
            artifact,
            assessment_artifact=assessment,
            assessment_review_rows=altered_rows,
            assessment_worksheet_sha256=worksheet_sha,
            evidence_pack=pack,
        )


def _current_memory() -> dict:
    basis = {
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
    candidates = [
        build_candidate_tile(
            tile_id=row["tile_id"],
            basis=[basis] if row["tile_id"] == "P1" else [],
        )
        for row in build_tile_contract_registry()["tiles"]
    ]
    packet = build_operational_memory_packet(
        brand_identity="causaprima.ai",
        source_candidate_packet_fingerprint=_digest("current-source"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions={
            "P1": {
                "authority_state": "accepted",
                "review_state": "resolved",
                "authority_profile_id": "human-reviewed-relation-v1",
                "authority_source": "human",
                "decision_event_id": "review-current-p1",
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
        policy_fingerprint=_digest("fixture-policy"),
        created_at="2026-08-07T10:00:00+02:00",
        idempotency_key_hash=_digest("fixture-adoption"),
        expected_current_canonical_memory_version=None,
    )
    return project_adopted_operational_memory(packet, event)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sources() -> tuple[dict, dict, list[dict[str, str]], dict, str]:
    artifact = json.loads(
        (_AUDIT / "causa-prima-exact-relation-supplement-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assessment = json.loads(
        (_AUDIT / "causa-prima-missing-tile-independent-audit.json").read_text(
            encoding="utf-8"
        )
    )
    worksheet_path = _AUDIT / "causa-prima-missing-tile-independent-review.csv"
    with worksheet_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    pack = json.loads(
        (
            _FIXTURE
            / "causa_prima-a8ba05137817-normalized-evidence-pack.json"
        ).read_text(encoding="utf-8")
    )
    return (
        artifact,
        assessment,
        rows,
        pack,
        hashlib.sha256(worksheet_path.read_bytes()).hexdigest(),
    )
