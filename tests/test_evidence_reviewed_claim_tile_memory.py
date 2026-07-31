from __future__ import annotations

from copy import deepcopy

import pytest

from src.services.evidence_reviewed_claim_tile_memory import (
    EvidenceReviewedClaimTileMemoryError,
    build_reviewed_claim_tile_memory_from_journal_shadow,
    build_reviewed_claim_tile_memory_shadow,
)


PACKET_FINGERPRINT = "a" * 64


def test_reviewed_memory_selects_only_explicit_acceptances() -> None:
    first = _mapping("1", "mission.M1")
    second = _mapping("2", "mission.M2")
    result = _build(
        _ledger([first, second]),
        [_review(first, "accepted"), _review(second, "rejected")],
    )

    assert result["selection_ready"] is True
    assert result["shadow_evaluation_ready"] is True
    assert result["canonical_memory_available"] is False
    assert result["evaluation_ready"] is False
    assert result["score"] is None
    assert result["summary"] == {
        "candidate_mapping_count": 2,
        "reviewed_mapping_count": 2,
        "pending_mapping_count": 0,
        "accepted_mapping_count": 1,
        "disputed_mapping_count": 0,
        "rejected_mapping_count": 1,
        "revoked_mapping_count": 0,
    }
    assert [
        row["mapping_id"] for row in result["accepted_mappings"]
    ] == [first["mapping_id"]]
    assert all(
        result[field] is False
        for field in (
            "runtime_effect",
            "authority",
            "automatic_tile_effect",
            "automatic_scoring_effect",
        )
    )


def test_reviewed_memory_is_order_invariant() -> None:
    first = _mapping("1", "mission.M1")
    second = _mapping("2", "mission.M2")
    reviews = [_review(first, "accepted"), _review(second, "rejected")]

    forward = _build(_ledger([first, second]), reviews)
    reverse = _build(
        _ledger([second, first]),
        reversed(reviews),
    )

    assert (
        forward["reviewed_memory_candidate_version"]
        == reverse["reviewed_memory_candidate_version"]
    )
    assert (
        forward["shadow_evaluation_identity"]
        == reverse["shadow_evaluation_identity"]
    )
    assert forward["state_fingerprint"] == reverse["state_fingerprint"]


def test_evaluator_version_changes_only_shadow_evaluation_identity() -> None:
    mapping = _mapping("1", "mission.M1")
    baseline = _build(
        _ledger([mapping]),
        [_review(mapping, "accepted")],
        evaluator_version="evaluator-v1",
    )
    changed = _build(
        _ledger([mapping]),
        [_review(mapping, "accepted")],
        evaluator_version="evaluator-v2",
    )

    assert (
        baseline["reviewed_memory_candidate_version"]
        == changed["reviewed_memory_candidate_version"]
    )
    assert (
        baseline["shadow_evaluation_identity"]
        != changed["shadow_evaluation_identity"]
    )
    assert baseline["score"] is changed["score"] is None


def test_unreviewed_mapping_is_excluded_and_blocks_selection() -> None:
    first = _mapping("1", "mission.M1")
    second = _mapping("2", "mission.M2")
    result = _build(
        _ledger([first, second]),
        [_review(first, "accepted")],
    )

    assert result["selection_ready"] is False
    assert result["summary"]["pending_mapping_count"] == 1
    assert "claim_tile_reviews_incomplete" in result[
        "promotion_blockers"
    ]
    assert [
        row["mapping_id"] for row in result["accepted_mappings"]
    ] == [first["mapping_id"]]


def test_revoked_mapping_returns_to_pending() -> None:
    mapping = _mapping("1", "mission.M1")
    result = _build(
        _ledger([mapping]),
        [_review(mapping, "revoked")],
    )

    assert result["selection_ready"] is False
    assert result["shadow_evaluation_ready"] is False
    assert result["summary"] == {
        "candidate_mapping_count": 1,
        "reviewed_mapping_count": 0,
        "pending_mapping_count": 1,
        "accepted_mapping_count": 0,
        "disputed_mapping_count": 0,
        "rejected_mapping_count": 0,
        "revoked_mapping_count": 1,
    }
    assert result["accepted_mappings"] == []


def test_journal_rebuild_derives_packet_and_rubric_fail_closed() -> None:
    mapping = _mapping("1", "mission.M1")
    ledger = _ledger([mapping])
    review = _review(mapping, "accepted")

    result = build_reviewed_claim_tile_memory_from_journal_shadow(
        ledger,
        [review],
    )

    assert result["review_packet_fingerprint"] == PACKET_FINGERPRINT
    assert result["review_packet_fingerprints"] == [
        PACKET_FINGERPRINT
    ]
    assert result["rubric_version"] == "baldosas-v3-1"
    assert result["summary"]["accepted_mapping_count"] == 1

    missing_packet = _review(mapping, "accepted")
    missing_packet.pop("review_packet_fingerprint")
    with pytest.raises(
        EvidenceReviewedClaimTileMemoryError,
        match="review_packet_fingerprint must be sha256",
    ):
        build_reviewed_claim_tile_memory_from_journal_shadow(
            ledger,
            [missing_packet],
        )

    second = _mapping("2", "mission.M2")
    mixed_packet = _review(second, "accepted")
    mixed_packet["review_packet_fingerprint"] = "b" * 64
    mixed_result = (
        build_reviewed_claim_tile_memory_from_journal_shadow(
            _ledger([mapping, second]),
            [review, mixed_packet],
        )
    )
    assert mixed_result["selection_ready"] is True
    assert mixed_result["review_packet_fingerprint"] is None
    assert mixed_result["review_packet_fingerprints"] == [
        PACKET_FINGERPRINT,
        "b" * 64,
    ]
    assert len(mixed_result["review_packet_set_fingerprint"]) == 64
    assert {
        row["review_packet_fingerprint"]
        for row in mixed_result["reviewed_mappings"]
    } == {PACKET_FINGERPRINT, "b" * 64}


def test_journal_rebuild_scopes_memory_to_latest_mapping_series() -> None:
    old = _mapping("1", "mission.M1")
    current = _mapping("2", "mission.M2")
    ledger = _ledger([old, current])
    ledger["latest_mapping_series_id"] = current[
        "mapping_series_id"
    ]
    old_review = _review(old, "accepted")
    current_review = _review(current, "accepted")
    current_review["review_packet_fingerprint"] = "b" * 64

    result = build_reviewed_claim_tile_memory_from_journal_shadow(
        ledger,
        [old_review, current_review],
    )

    assert result["selection_ready"] is True
    assert result["summary"]["candidate_mapping_count"] == 1
    assert result["review_packet_fingerprints"] == ["b" * 64]
    assert [
        row["mapping_id"] for row in result["accepted_mappings"]
    ] == [current["mapping_id"]]


def test_unknown_authoritative_or_wrong_packet_review_fails_closed() -> None:
    mapping = _mapping("1", "mission.M1")
    unknown = _review(mapping, "accepted")
    unknown["mapping_id"] = "9" * 64
    unknown["subject_id"] = "9" * 64
    with pytest.raises(
        EvidenceReviewedClaimTileMemoryError,
        match="unknown mapping",
    ):
        _build(_ledger([mapping]), [unknown])

    authoritative = _review(mapping, "accepted")
    authoritative["automatic_scoring_effect"] = True
    with pytest.raises(
        EvidenceReviewedClaimTileMemoryError,
        match="authoritative",
    ):
        _build(_ledger([mapping]), [authoritative])

    wrong_packet = _review(mapping, "accepted")
    wrong_packet["review_packet_fingerprint"] = "b" * 64
    with pytest.raises(
        EvidenceReviewedClaimTileMemoryError,
        match="packet mismatch",
    ):
        _build(_ledger([mapping]), [wrong_packet])


def _build(
    ledger: dict,
    reviews,
    *,
    evaluator_version: str = "evaluator-v1",
) -> dict:
    return build_reviewed_claim_tile_memory_shadow(
        ledger,
        reviews,
        review_packet_fingerprint=PACKET_FINGERPRINT,
        rubric_version="baldosas-v3-1",
        evaluator_version=evaluator_version,
    )


def _ledger(mappings: list[dict]) -> dict:
    series_ids = sorted(
        {mapping["mapping_series_id"] for mapping in mappings}
    )
    return {
        "schema_version": "evidence-claim-tile-ledger-v1",
        "mode": "shadow",
        "runtime_effect": False,
        "authority": False,
        "brand": {"name": "Example", "domain": "example.com"},
        "mapping_series": [
            {
                "mapping_series_id": series_id,
                "rubric_version": "baldosas-v3-1",
            }
            for series_id in series_ids
        ],
        "mappings": deepcopy(mappings),
    }


def _mapping(seed: str, tile_key: str) -> dict:
    tile_id = tile_key.split(".", 1)[1]
    return {
        "mapping_id": seed * 64,
        "mapping_series_id": (str(int(seed) + 2)) * 64,
        "source_evidence_id": (str(int(seed) + 4)) * 64,
        "claim_variant_id": (str(int(seed) + 6)) * 64,
        "component_key": "mission",
        "tile_id": tile_id,
        "tile_key": tile_key,
        "polarity": "supports",
        "runtime_effect": False,
        "authority": False,
    }


def _review(mapping: dict, decision: str) -> dict:
    return {
        **{
            field: mapping[field]
            for field in (
                "mapping_id",
                "mapping_series_id",
                "source_evidence_id",
                "claim_variant_id",
                "component_key",
                "tile_id",
                "tile_key",
                "polarity",
            )
        },
        "subject_id": mapping["mapping_id"],
        "event_id": f"review-{mapping['mapping_id'][:8]}",
        "sequence": 1,
        "decision": decision,
        "reviewer_id": "gsus",
        "rationale": "Human tile-contract review.",
        "reviewed_at": "2026-07-30T09:49:51+02:00",
        "evaluator_version": "manual-review-v1",
        "review_packet_fingerprint": PACKET_FINGERPRINT,
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
    }
