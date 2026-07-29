from __future__ import annotations

from copy import deepcopy

import pytest

from src.services.evidence_source_review_set import (
    EVIDENCE_SOURCE_REVIEW_DATASET_VERSION,
    EVIDENCE_SOURCE_REVIEW_EVENT_SCHEMA_VERSION,
    EvidenceSourceReviewSetError,
    build_review_template,
    evaluate_source_review_set,
    load_review_candidates,
    load_review_events,
    load_review_manifest,
    render_source_review_set_markdown,
    source_review_fingerprint,
)


def test_committed_reviews_separate_publisher_and_corroboration_gates() -> None:
    result = evaluate_source_review_set(
        load_review_candidates(),
        load_review_events(),
        manifest=load_review_manifest(),
    )

    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["identity_gate_ready"] is True
    assert result["publisher_independence_gate_ready"] is True
    assert result["claim_corroboration_gate_ready"] is False
    assert result["promotion_ready"] is False
    assert result["promotion_blockers"] == [
        "claim_level_reviews_incomplete",
        "semantic_paraphrase_recall_unmeasured",
    ]
    assert result["summary"] == {
        "candidate_count": 6,
        "review_event_count": 6,
        "active_review_count": 6,
        "pending_count": 0,
        "revoked_case_count": 0,
        "claim_scoped_review_count": 0,
        "claim_level_review_required_count": 5,
        "identity_decision_counts": {"accepted": 6},
        "publisher_independence_decision_counts": {
            "confirmed_independent": 5,
            "excluded": 1,
        },
        "claim_corroboration_decision_counts": {
            "disputed": 1,
            "excluded": 1,
            "mixed": 4,
        },
    }


def test_manifest_freezes_candidates_and_review_event_log() -> None:
    manifest = load_review_manifest()
    candidates = load_review_candidates()
    events = load_review_events()

    assert manifest["candidate_fingerprint"] == source_review_fingerprint(
        candidates
    )
    assert (
        manifest["review_event_fingerprint"]
        == source_review_fingerprint(events)
    )
    assert all(candidate["claim_id"] is None for candidate in candidates)
    assert all(candidate["runtime_effect"] is False for candidate in candidates)
    assert all(candidate["authority"] is False for candidate in candidates)


def test_publisher_independence_never_implies_global_corroboration() -> None:
    result = evaluate_source_review_set(
        load_review_candidates(),
        load_review_events(),
        manifest=load_review_manifest(),
    )
    independent_rows = [
        row
        for row in result["evaluated"]
        if row["publisher_independence_decision"]
        == "confirmed_independent"
    ]

    assert len(independent_rows) == 5
    assert all(row["claim_id"] is None for row in independent_rows)
    assert all(
        row["claim_corroboration_decision"] in {"mixed", "disputed"}
        for row in independent_rows
    )
    assert all(
        row["requires_claim_level_review"] is True
        for row in independent_rows
    )


def test_independent_corroboration_requires_explicit_claim_id() -> None:
    candidates = load_review_candidates()
    events = load_review_events()
    events[0]["claim_corroboration_decision"] = (
        "independently_corroborated"
    )
    events[0]["requires_claim_level_review"] = False

    with pytest.raises(
        EvidenceSourceReviewSetError,
        match="requires an explicit claim_id",
    ):
        evaluate_source_review_set(
            candidates,
            events,
            manifest=_manifest_for(candidates, events),
        )


def test_wire_service_cannot_be_confirmed_as_independent_editorial() -> None:
    candidates = load_review_candidates()
    events = load_review_events()
    wire_event = next(
        event
        for event in events
        if event["case_id"] == "vercel-v6-pr-newswire"
    )
    wire_event["publisher_independence_decision"] = (
        "confirmed_independent"
    )

    with pytest.raises(
        EvidenceSourceReviewSetError,
        match="wire service cannot be confirmed independent",
    ):
        evaluate_source_review_set(
            candidates,
            events,
            manifest=_manifest_for(candidates, events),
        )


def test_revocation_is_append_only_and_removes_active_decision() -> None:
    candidates = load_review_candidates()
    events = load_review_events()
    original = next(
        event
        for event in events
        if event["case_id"] == "vercel-v1-infoq"
    )
    events.append(
        {
            "schema_version": (
                EVIDENCE_SOURCE_REVIEW_EVENT_SCHEMA_VERSION
            ),
            "dataset_version": EVIDENCE_SOURCE_REVIEW_DATASET_VERSION,
            "event_id": "vercel-v1-infoq-revocation-002",
            "case_id": "vercel-v1-infoq",
            "sequence": 2,
            "event_type": "revocation",
            "previous_event_id": original["event_id"],
            "revoked_event_id": original["event_id"],
            "reviewer_id": "gsus",
            "rationale": "Controlled revocation test.",
            "reviewed_at": "2026-07-29T12:30:00+02:00",
            "runtime_effect": False,
            "authority": False,
        }
    )

    result = evaluate_source_review_set(
        candidates,
        events,
        manifest=_manifest_for(candidates, events),
    )

    assert result["summary"]["active_review_count"] == 5
    assert result["summary"]["pending_count"] == 1
    assert result["summary"]["revoked_case_count"] == 1
    assert result["revoked_case_ids"] == ["vercel-v1-infoq"]
    assert "human_reviews_incomplete" in result["promotion_blockers"]


def test_review_template_is_unsigned_and_non_authoritative() -> None:
    rows = build_review_template(load_review_candidates())

    assert len(rows) == 6
    assert all(row["event_id"] is None for row in rows)
    assert all(row["identity_decision"] is None for row in rows)
    assert all(row["publisher_independence_decision"] is None for row in rows)
    assert all(row["claim_corroboration_decision"] is None for row in rows)
    assert all(row["runtime_effect"] is False for row in rows)
    assert all(row["authority"] is False for row in rows)


def test_manifest_fingerprint_rejects_silent_review_edits() -> None:
    events = load_review_events()
    events[0]["claim_corroboration_decision"] = "excluded"

    with pytest.raises(
        EvidenceSourceReviewSetError,
        match="event fingerprint",
    ):
        evaluate_source_review_set(
            load_review_candidates(),
            events,
            manifest=load_review_manifest(),
        )


def test_markdown_exposes_partial_gate_result() -> None:
    rendered = render_source_review_set_markdown(
        evaluate_source_review_set(
            load_review_candidates(),
            load_review_events(),
            manifest=load_review_manifest(),
        )
    )

    assert "# Evidence source review set" in rendered
    assert "Publisher gate ready: `true`" in rendered
    assert "Claim corroboration gate ready: `false`" in rendered
    assert "Sources requiring claim review: `5`" in rendered


def _manifest_for(
    candidates: list[dict],
    events: list[dict],
) -> dict:
    manifest = deepcopy(load_review_manifest())
    manifest["candidate_fingerprint"] = source_review_fingerprint(
        candidates
    )
    manifest["review_event_fingerprint"] = source_review_fingerprint(events)
    return manifest
