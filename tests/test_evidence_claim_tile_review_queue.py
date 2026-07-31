from __future__ import annotations

from copy import deepcopy

import pytest

from src.services.evidence_claim_tile_review_packet import (
    build_evidence_claim_tile_review_packet,
)
from src.services.evidence_claim_tile_review_queue import (
    EvidenceClaimTileReviewQueueError,
    build_evidence_claim_tile_review_queue,
)
from src.sv9.rubric import RUBRIC_VERSION


CLAIM = (
    "Our mission is to make financial work radically simpler for everyone."
)


def test_unreviewed_mapping_enters_non_authoritative_queue() -> None:
    packet = build_evidence_claim_tile_review_packet(
        [_report("one", "2026-07-01T08:00:00Z")]
    )

    queue = _build(packet, [])

    assert queue["review_complete"] is False
    assert queue["summary"]["pending_review_count"] == 1
    assert queue["summary"]["unreviewed_mapping_count"] == 1
    assert queue["items"][0]["review_reason"] == "unreviewed_mapping"
    assert queue["items"][0]["expected_current_event_id"] is None
    assert all(
        queue[field] is False
        for field in (
            "runtime_effect",
            "authority",
            "automatic_tile_effect",
            "automatic_scoring_effect",
        )
    )


def test_repeated_observation_reuses_exact_semantic_review() -> None:
    first = _report("one", "2026-07-01T08:00:00Z")
    earlier = build_evidence_claim_tile_review_packet([first])
    current = build_evidence_claim_tile_review_packet(
        [first, _report("two", "2026-07-02T08:00:00Z")]
    )
    review = _review(earlier, "accepted")

    queue = _build(current, [review], registered=[earlier, current])

    assert queue["review_complete"] is True
    assert queue["summary"]["reviewed_count"] == 1
    assert queue["summary"]["accepted_count"] == 1
    assert queue["summary"]["observation_state_counts"] == {
        "repeated": 1
    }
    item = queue["items"][0]
    assert item["review_status"] == "reviewed"
    assert item["review_reason"] == "review_target_unchanged"
    assert (
        item["review_target_fingerprint"]
        == item["reviewed_target_fingerprint"]
    )
    assert item["candidate"]["mapping"]["observation_count"] == 2


def test_changed_evidence_quote_reopens_same_mapping_review() -> None:
    first = _report("one", "2026-07-01T08:00:00Z")
    earlier = build_evidence_claim_tile_review_packet([first])
    changed = _report(
        "two",
        "2026-07-02T08:00:00Z",
        quote="make financial work radically simpler",
    )
    current = build_evidence_claim_tile_review_packet([first, changed])
    assert (
        earlier["candidates"][0]["subject_id"]
        == current["candidates"][0]["subject_id"]
    )

    queue = _build(
        current,
        [_review(earlier, "accepted")],
        registered=[earlier, current],
    )

    assert queue["review_complete"] is False
    assert queue["summary"]["changed_review_target_count"] == 1
    assert queue["summary"]["unreviewed_mapping_count"] == 0
    item = queue["items"][0]
    assert item["review_reason"] == "review_target_changed"
    assert item["current_decision"] == "accepted"
    assert item["expected_current_event_id"] == "review-event-1"
    assert (
        item["review_target_fingerprint"]
        != item["reviewed_target_fingerprint"]
    )


def test_revoked_review_returns_mapping_to_queue() -> None:
    packet = build_evidence_claim_tile_review_packet(
        [_report("one", "2026-07-01T08:00:00Z")]
    )

    queue = _build(packet, [_review(packet, "revoked")])

    assert queue["review_complete"] is False
    assert queue["summary"]["revoked_review_count"] == 1
    assert queue["items"][0]["review_reason"] == "review_revoked"
    assert queue["items"][0]["current_decision"] == "revoked"


def test_queue_fails_closed_without_review_source_packet() -> None:
    first = _report("one", "2026-07-01T08:00:00Z")
    reviewed_packet = build_evidence_claim_tile_review_packet([first])
    current_packet = build_evidence_claim_tile_review_packet(
        [first, _report("two", "2026-07-02T08:00:00Z")]
    )
    review = _review(reviewed_packet, "accepted")

    with pytest.raises(
        EvidenceClaimTileReviewQueueError,
        match="unavailable registered packet",
    ):
        _build(
            current_packet,
            [review],
            registered=[current_packet],
        )


def test_queue_fails_closed_on_review_identity_drift() -> None:
    packet = build_evidence_claim_tile_review_packet(
        [_report("one", "2026-07-01T08:00:00Z")]
    )
    review = _review(packet, "accepted")
    review["tile_key"] = "mission.M2"

    with pytest.raises(
        EvidenceClaimTileReviewQueueError,
        match="tile_key does not match",
    ):
        _build(packet, [review])


def test_queue_is_order_invariant() -> None:
    first = _report("one", "2026-07-01T08:00:00Z")
    second = deepcopy(first)
    second["id"] = "two"
    second["created_at"] = "2026-07-02T08:00:00Z"
    second["raw"]["sv9"]["result"]["components"]["mission"][
        "tile_profile"
    ].append(
        {
            "id": "M2",
            "estado": "ok",
            "evidencia": CLAIM,
            "evidence_ref": "web.0",
        }
    )
    packet = build_evidence_claim_tile_review_packet([first, second])
    reviews = [
        _review(packet, "accepted", index=0),
        _review(packet, "rejected", index=1),
    ]

    forward = _build(packet, reviews)
    reverse = _build(packet, reversed(reviews))

    assert forward["queue_fingerprint"] == reverse["queue_fingerprint"]
    assert forward == reverse


def _build(
    packet: dict,
    reviews,
    *,
    registered: list[dict] | None = None,
) -> dict:
    return build_evidence_claim_tile_review_queue(
        packet,
        reviews,
        registered_packets=registered or [packet],
    )


def _review(
    packet: dict,
    decision: str,
    *,
    index: int = 0,
) -> dict:
    candidate = packet["candidates"][index]
    mapping = candidate["mapping"]
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
        "subject_id": candidate["subject_id"],
        "event_id": f"review-event-{index + 1}",
        "decision": decision,
        "reviewer_id": "gsus",
        "rationale": "Human semantic review.",
        "reviewed_at": "2026-07-31T08:00:00+02:00",
        "review_packet_fingerprint": packet["manifest"][
            "review_packet_fingerprint"
        ],
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
    }


def _report(
    report_id: str,
    created_at: str,
    *,
    quote: str = CLAIM,
) -> dict:
    evidence = {
        "ref": "web.0",
        "source": "web",
        "evidence_type": "raw_input",
        "url": "https://example.com/about",
        "content": CLAIM,
        "metadata": {
            "source_class": "owned_copy",
            "claim_slot_key": "mission.primary",
            "claim_type": "mission",
            "claim_slot_derivation_mode": "upstream_explicit",
        },
    }
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "reliability_status": "shadow",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "blocks": [],
        "raw": {
            "schema_version": "report-v1",
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "brand_name": "Example",
                        "url": "https://example.com",
                        "evidence": [evidence],
                    }
                }
            },
            "sv9": {
                "evaluator_model": "evaluator-a",
                "result": {
                    "rubric_version": RUBRIC_VERSION,
                    "components": {
                        "mission": {
                            "tile_profile": [
                                {
                                    "id": "M1",
                                    "estado": "ok",
                                    "evidencia": quote,
                                    "evidence_ref": "web.0",
                                }
                            ],
                        }
                    },
                },
            },
        },
    }
