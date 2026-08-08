import pytest

from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_tile_contract_registry,
    canonical_fingerprint,
)
from src.services.evidence_vault_operational_authority import (
    build_operational_adoption_event,
    project_adopted_operational_memory,
)
from src.services.evidence_vault_operational_review import (
    EvidenceVaultOperationalReviewError,
    build_reviewed_operational_source,
)


def _source(tile_id: str = "M1"):
    relation = {
        "relation_id": "1" * 64,
        "evidence_id": "2" * 64,
        "source_identity_id": "3" * 64,
        "claim_id": None,
        "polarity": "supports",
        "review_status": "unreviewed",
        "decision_event_id": None,
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }
    tiles = [
        build_candidate_tile(
            tile_id=row["tile_id"],
            basis=[relation] if row["tile_id"] == tile_id else [],
        )
        for row in build_tile_contract_registry()["tiles"]
    ]
    return build_candidate_packet(
        brand_identity="example.com",
        parent_canonical_memory_version=None,
        candidate_memory_version="4" * 64,
        accepted_memory_candidate_version="5" * 64,
        reviewed_memory_candidate_version="6" * 64,
        review_packet_set_fingerprint="7" * 64,
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=tiles,
    )


def test_human_acceptance_creates_reviewed_relation_and_authoritative_tile() -> None:
    reviewed, operational = build_reviewed_operational_source(
        _source(),
        decisions={
            "1" * 64: {
                "decision": "accept",
                "decision_event_id": "human-event-1",
            }
        },
        current_operational_memory=None,
    )

    m1 = next(row for row in reviewed["candidate_tiles"] if row["tile_id"] == "M1")
    assert m1["basis"][0]["review_status"] == "accepted"
    assert m1["basis"][0]["decision_event_id"] == "human-event-1"
    accepted = operational["accepted_memory"]["accepted_tiles"]
    assert len(accepted) == 1
    assert accepted[0]["tile_id"] == "M1"
    assert accepted[0]["authority_source"] == "human"
    assert accepted[0]["decision_event_id"] == "human-event-1"
    assert operational["has_accepted_change"] is True


def test_generic_single_relation_cannot_create_c7_authority() -> None:
    with pytest.raises(
        EvidenceVaultOperationalReviewError,
        match="C7 authority requires an exact two-member relation source",
    ):
        build_reviewed_operational_source(
            _source("C7"),
            decisions={
                "1" * 64: {
                    "decision": "accept",
                    "decision_event_id": "human-event-c7",
                }
            },
            current_operational_memory=None,
        )

    _, rejected = build_reviewed_operational_source(
        _source("C7"),
        decisions={
            "1" * 64: {
                "decision": "reject",
                "decision_event_id": "human-event-c7-reject",
            }
        },
        current_operational_memory=None,
    )
    assert rejected["accepted_memory"]["accepted_tiles"] == []


def test_human_rejection_removes_relation_without_creating_memory() -> None:
    reviewed, operational = build_reviewed_operational_source(
        _source(),
        decisions={
            "1" * 64: {
                "decision": "reject",
                "decision_event_id": "human-event-2",
            }
        },
        current_operational_memory=None,
    )

    m1 = next(row for row in reviewed["candidate_tiles"] if row["tile_id"] == "M1")
    assert m1["basis"] == []
    assert operational["accepted_memory"]["accepted_tiles"] == []
    assert operational["has_accepted_change"] is False
    assert len(operational["candidate_overlay"]["candidate_tiles"]) == 80


def test_human_review_can_adopt_an_incremental_n_plus_one_relation() -> None:
    _, baseline_packet = build_reviewed_operational_source(
        _source(),
        decisions={
            "1" * 64: {
                "decision": "accept",
                "decision_event_id": "human-event-1",
            }
        },
        current_operational_memory=None,
    )
    event = build_operational_adoption_event(
        baseline_packet,
        event_id="11111111-1111-4111-8111-111111111111",
        sequence=1,
        previous_event_id=None,
        adopted_by="human",
        actor_id="reviewer",
        policy_fingerprint="8" * 64,
        created_at="2026-08-07T10:00:00+02:00",
        idempotency_key_hash="9" * 64,
        expected_current_canonical_memory_version=None,
    )
    current = project_adopted_operational_memory(baseline_packet, event)
    previous = [
        build_candidate_tile(
            tile_id=row["tile_id"],
            basis=(
                current["content"]["accepted_tiles"][0]["basis"]
                if row["tile_id"] == "M1"
                else []
            ),
        )
        for row in build_tile_contract_registry()["tiles"]
    ]
    relation = {
        "relation_id": "a" * 64,
        "evidence_id": "b" * 64,
        "source_identity_id": "c" * 64,
        "claim_id": None,
        "polarity": "supports",
        "review_status": "unreviewed",
        "decision_event_id": None,
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }
    incremental_tiles = build_incremental_candidate_tiles(
        previous_candidate_tiles=previous,
        tile_updates=[
            {
                "tile_id": "M2",
                "delta_kind": "candidate_update",
                "basis": [relation],
                "coverage_refs": [],
                "unresolved_refs": [],
            }
        ],
    )
    source = build_candidate_packet(
        brand_identity="example.com",
        parent_canonical_memory_version=current["canonical_memory_version"],
        candidate_memory_version="d" * 64,
        accepted_memory_candidate_version="e" * 64,
        reviewed_memory_candidate_version="f" * 64,
        review_packet_set_fingerprint="0" * 64,
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=incremental_tiles,
    )

    reviewed, operational = build_reviewed_operational_source(
        source,
        decisions={
            "a" * 64: {
                "decision": "accept",
                "decision_event_id": "human-event-2",
            }
        },
        current_operational_memory=current,
    )

    reviewed_by_id = {
        row["tile_id"]: row for row in reviewed["candidate_tiles"]
    }
    assert reviewed_by_id["M1"]["basis"][0]["review_status"] == "accepted"
    assert reviewed_by_id["M2"]["basis"][0]["review_status"] == "accepted"
    assert {
        row["tile_id"]
        for row in operational["accepted_memory"]["accepted_tiles"]
    } == {"M1", "M2"}
    assert operational["current_canonical_memory_version"] == current[
        "canonical_memory_version"
    ]
    assert operational["has_accepted_change"] is True
