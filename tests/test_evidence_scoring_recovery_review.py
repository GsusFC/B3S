from __future__ import annotations

from copy import deepcopy

import pytest

from src.services.evidence_scoring_recovery_review import (
    EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION,
    EvidenceScoringRecoveryReviewError,
    build_recovery_review_candidates,
    build_recovery_review_template,
    evaluate_recovery_reviews,
)


def test_candidate_is_deduplicated_across_shadow_lanes() -> None:
    preview = _preview()

    candidates = build_recovery_review_candidates(
        [
            {"lane": "all_scan", "preview": preview},
            {"lane": "capture", "preview": preview},
        ]
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert {
        context["lane"] for context in candidate["contexts"]
    } == {"all_scan", "capture"}
    assert candidate["tile"]["tile_key"] == "magnetism.MG1"
    assert "detiene el scroll" in candidate["tile"]["condition"]
    assert candidate["runtime_effect"] is False
    assert candidate["authority"] is False


def test_current_direct_relation_is_a_review_candidate() -> None:
    preview = _preview()
    preview["accepted_evidence"][0]["present_in_latest"] = True
    preview["accepted_evidence"][0]["component_key"] = "magnetism"
    preview["accepted_evidence"][0]["tile_id"] = "MG1"
    preview["recoveries"] = []

    candidates = build_recovery_review_candidates(
        [{"lane": "history", "preview": preview}]
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["tile"]["latest_state"] == "ok"
    assert candidate["tile"]["proposed_state"] == "ok"
    assert candidate["contexts"] == [
        {
            "lane": "history",
            "latest_report_id": "report-2",
            "review_scope": "current_direct_relation",
            "latest_state": "ok",
            "proposed_state": "ok",
        }
    ]


def test_claim_routed_current_relation_is_not_duplicated() -> None:
    preview = _preview()
    preview["accepted_evidence"][0]["present_in_latest"] = True
    preview["accepted_evidence"][0]["component_key"] = "magnetism"
    preview["accepted_evidence"][0]["tile_id"] = "MG1"
    preview["recoveries"] = []

    candidates = build_recovery_review_candidates(
        [{"lane": "history", "preview": preview}],
        claim_tile_mappings=[
            {
                "tile_id": "MG1",
                "source_evidence_id": "source-1",
            }
        ],
    )

    assert candidates == []


def test_unsigned_template_and_missing_reviews_fail_closed() -> None:
    candidates = build_recovery_review_candidates(
        [{"lane": "bridge", "preview": _preview()}]
    )

    template = build_recovery_review_template(candidates)
    result = evaluate_recovery_reviews(candidates, [])

    assert template[0]["event_id"] is None
    assert template[0]["decision"] is None
    assert template[0]["reviewer_id"] is None
    assert result["review_gate_ready"] is False
    assert result["promotion_ready"] is False
    assert result["summary"]["reviewed_count"] == 0
    assert result["summary"]["pending_count"] == 1
    assert result["accepted_tile_evidence_ids"] == []


@pytest.mark.parametrize("decision", ["disputed", "rejected"])
def test_only_accepted_review_exposes_recovery_evidence(
    decision: str,
) -> None:
    candidates = build_recovery_review_candidates(
        [{"lane": "bridge", "preview": _preview()}]
    )
    candidate = candidates[0]

    non_accept = evaluate_recovery_reviews(
        candidates,
        [_event(candidate, decision=decision)],
    )
    accepted = evaluate_recovery_reviews(
        candidates,
        [_event(candidate, decision="accepted")],
    )

    assert non_accept["accepted_tile_evidence_ids"] == []
    assert accepted["accepted_tile_evidence_ids"] == ["evidence-1"]
    assert accepted["review_gate_ready"] is True
    assert accepted["runtime_effect"] is False
    assert accepted["authority"] is False


def test_append_only_revocation_returns_mapping_to_pending() -> None:
    candidates = build_recovery_review_candidates(
        [{"lane": "bridge", "preview": _preview()}]
    )
    candidate = candidates[0]
    accepted = _event(candidate, decision="accepted")
    revoked = _event(
        candidate,
        decision="revoked",
        sequence=2,
        previous_event_id=accepted["event_id"],
    )

    result = evaluate_recovery_reviews(
        candidates,
        [accepted, revoked],
    )

    assert result["summary"]["revoked_count"] == 1
    assert result["summary"]["pending_count"] == 1
    assert result["accepted_tile_evidence_ids"] == []
    assert result["evaluated"][0]["current_event_decision"] == "revoked"
    assert result["evaluated"][0]["decision"] == "pending"


def test_candidate_or_event_drift_is_rejected() -> None:
    candidates = build_recovery_review_candidates(
        [{"lane": "bridge", "preview": _preview()}]
    )
    changed = deepcopy(candidates)
    changed[0]["evidence"]["quote"] = "Silently changed."

    with pytest.raises(
        EvidenceScoringRecoveryReviewError,
        match="candidate fingerprint",
    ):
        evaluate_recovery_reviews(changed, [])

    event = _event(candidates[0], decision="accepted")
    event["candidate_fingerprint"] = "wrong"
    with pytest.raises(
        EvidenceScoringRecoveryReviewError,
        match="fingerprint mismatch",
    ):
        evaluate_recovery_reviews(candidates, [event])


def test_event_chain_requires_exact_predecessor() -> None:
    candidates = build_recovery_review_candidates(
        [{"lane": "bridge", "preview": _preview()}]
    )
    candidate = candidates[0]
    first = _event(candidate, decision="disputed")
    second = _event(
        candidate,
        decision="accepted",
        sequence=2,
        previous_event_id="wrong",
    )

    with pytest.raises(
        EvidenceScoringRecoveryReviewError,
        match="predecessor mismatch",
    ):
        evaluate_recovery_reviews(candidates, [first, second])


def _preview() -> dict:
    return {
        "runtime_effect": False,
        "authority": False,
        "brand": {"name": "Example", "domain": "example.com"},
        "rubric_version": "baldosas-v3-1",
        "latest_report_id": "report-2",
        "accepted_evidence": [
            {
                "tile_evidence_id": "evidence-1",
                "component_key": "magnetism",
                "tile_id": "MG1",
                "quote": "A sharp statement that stops the scroll.",
                "source_urls": ["https://example.com"],
                "source_classes": ["owned_copy"],
                "source_evidence_ids": ["source-1"],
                "acceptance_basis": [
                    "scanner_tile_ok",
                    "literal_source_match",
                ],
                "first_seen_at": "2026-07-01T08:00:00+00:00",
                "last_seen_at": "2026-07-01T08:00:00+00:00",
                "observation_count": 1,
            }
        ],
        "recoveries": [
            {
                "component_key": "magnetism",
                "tile_id": "MG1",
                "latest_state": "sin_evidencia",
                "preview_state": "ok",
                "tile_evidence_ids": ["evidence-1"],
            }
        ],
    }


def _event(
    candidate: dict,
    *,
    decision: str,
    sequence: int = 1,
    previous_event_id: str | None = None,
) -> dict:
    return {
        "schema_version": (
            EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION
        ),
        "case_id": candidate["case_id"],
        "candidate_fingerprint": candidate[
            "candidate_fingerprint"
        ],
        "event_id": f"{candidate['case_id']}-{sequence}",
        "sequence": sequence,
        "previous_event_id": previous_event_id,
        "decision": decision,
        "reviewer_id": "gsus",
        "rationale": "Manual semantic mapping review.",
        "reviewed_at": f"2026-07-29T17:0{sequence}:00+02:00",
        "runtime_effect": False,
        "authority": False,
    }
