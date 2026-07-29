from __future__ import annotations

from copy import deepcopy

import pytest

from src.services.evidence_identity_gold_set import (
    DEFAULT_GOLD_ROOT,
    EVIDENCE_IDENTITY_GOLD_DATASET_VERSION,
    EVIDENCE_IDENTITY_GOLD_SCHEMA_VERSION,
    EvidenceIdentityGoldSetError,
    build_review_template,
    evaluate_identity_gold_set,
    gold_candidate_fingerprint,
    load_gold_candidates,
    load_gold_manifest,
    load_gold_reviews,
    predict_identity_disposition,
    render_identity_gold_set_markdown,
)


def test_committed_human_reviews_satisfy_identity_v4_gate() -> None:
    result = evaluate_identity_gold_set(
        load_gold_candidates(),
        load_gold_reviews(DEFAULT_GOLD_ROOT / "reviews.jsonl"),
        manifest=load_gold_manifest(),
    )

    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"]["reviewed_count"] == 14
    assert result["summary"]["pending_count"] == 0
    assert result["summary"]["critical_false_accept_count"] == 0
    assert result["summary"]["accepted_precision"] == 1.0
    assert result["summary"]["accepted_recall"] == 1.0
    assert result["summary"]["exact_agreement_rate"] == 1.0
    assert result["promotion_ready"] is True
    assert result["promotion_blockers"] == []


def test_versioned_candidate_set_covers_required_identity_risks() -> None:
    manifest = load_gold_manifest()
    candidates = load_gold_candidates()
    case_types = {str(row["case_type"]) for row in candidates}

    assert len(candidates) == 14
    assert set(manifest["required_case_types"]) <= case_types
    assert {str(row["split"]) for row in candidates} == {
        "calibration",
        "test",
    }
    assert manifest["candidate_fingerprint"] == gold_candidate_fingerprint(
        candidates
    )
    assert all("proposed_review" in row for row in candidates)
    assert all("reviewer_id" not in row for row in candidates)


def test_candidate_predictions_keep_weak_identity_out_of_acceptance() -> None:
    candidates = {
        str(row["case_id"]): row for row in load_gold_candidates()
    }

    assert predict_identity_disposition(
        candidates["cal-owned-domain"]
    ) == {
        "identity_status": "eligible",
        "decision": "accepted",
    }
    assert predict_identity_disposition(
        candidates["cal-wrong-owned-domain"]
    ) == {
        "identity_status": "mismatch",
        "decision": "rejected",
    }
    assert predict_identity_disposition(
        candidates["cal-homonym-name-only"]
    ) == {
        "identity_status": "mismatch",
        "decision": "rejected",
    }
    assert predict_identity_disposition(
        candidates["test-reproducible-external"]
    ) == {
        "identity_status": "eligible",
        "decision": "accepted",
    }
    assert predict_identity_disposition(
        candidates["test-reproduced-disagreement"]
    ) == {
        "identity_status": "disputed",
        "decision": "disputed",
    }
    assert predict_identity_disposition(
        candidates["test-first-scan-poison"]
    ) == {
        "identity_status": "mismatch",
        "decision": "rejected",
    }
    assert predict_identity_disposition(
        candidates["test-product-on-parent-domain"]
    ) == {
        "identity_status": "unverified",
        "decision": "disputed",
    }


def test_candidate_set_cannot_be_promotion_ready_without_human_reviews() -> None:
    result = evaluate_identity_gold_set(
        load_gold_candidates(),
        [],
        manifest=load_gold_manifest(),
    )

    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["promotion_ready"] is False
    assert result["summary"]["candidate_count"] == 14
    assert result["summary"]["reviewed_count"] == 0
    assert result["summary"]["pending_count"] == 14
    assert result["promotion_blockers"] == [
        "human_reviews_incomplete",
        "no_human_reviews",
    ]


def test_review_template_is_unsigned_and_cannot_masquerade_as_gold() -> None:
    rows = build_review_template(load_gold_candidates())

    assert len(rows) == 14
    assert all(row["decision"] is None for row in rows)
    assert all(row["reviewer_id"] is None for row in rows)
    assert all(row["reviewed_at"] is None for row in rows)


def test_complete_matching_reviews_can_satisfy_candidate_gate() -> None:
    candidates = load_gold_candidates()
    reviews = [
        _review(
            str(candidate["case_id"]),
            predict_identity_disposition(candidate)["decision"],
        )
        for candidate in candidates
    ]

    result = evaluate_identity_gold_set(
        candidates,
        reviews,
        manifest=load_gold_manifest(),
    )

    assert result["promotion_ready"] is True
    assert result["promotion_blockers"] == []
    assert result["summary"]["reviewed_count"] == 14
    assert result["summary"]["accepted_precision"] == 1.0
    assert result["summary"]["accepted_recall"] == 1.0
    assert result["summary"]["exact_agreement_rate"] == 1.0
    assert result["summary"]["critical_false_accept_count"] == 0


def test_critical_human_rejection_blocks_a_predicted_acceptance() -> None:
    candidate = deepcopy(
        next(
            row
            for row in load_gold_candidates()
            if row["case_id"] == "cal-owned-domain"
        )
    )
    candidate["critical"] = True
    manifest = {
        **load_gold_manifest(),
        "candidate_fingerprint": gold_candidate_fingerprint([candidate]),
    }

    result = evaluate_identity_gold_set(
        [candidate],
        [_review(str(candidate["case_id"]), "rejected")],
        manifest=manifest,
    )

    assert result["promotion_ready"] is False
    assert result["summary"]["critical_false_accept_count"] == 1
    assert result["critical_false_accept_case_ids"] == [
        candidate["case_id"]
    ]
    assert "critical_false_accepts_present" in result["promotion_blockers"]


def test_manifest_fingerprint_rejects_silent_candidate_edits() -> None:
    candidates = load_gold_candidates()
    candidates[0]["evidence"]["content"] = "Silently edited content."

    with pytest.raises(
        EvidenceIdentityGoldSetError,
        match="fingerprint",
    ):
        evaluate_identity_gold_set(
            candidates,
            [],
            manifest=load_gold_manifest(),
        )


def test_duplicate_review_is_rejected() -> None:
    candidate = load_gold_candidates()[0]
    review = _review(str(candidate["case_id"]), "accepted")

    with pytest.raises(
        EvidenceIdentityGoldSetError,
        match="duplicate review",
    ):
        evaluate_identity_gold_set(
            [candidate],
            [review, review],
            manifest={
                **load_gold_manifest(),
                "candidate_fingerprint": gold_candidate_fingerprint(
                    [candidate]
                ),
            },
        )


def test_markdown_reports_pending_human_work() -> None:
    rendered = render_identity_gold_set_markdown(
        evaluate_identity_gold_set(
            load_gold_candidates(),
            [],
            manifest=load_gold_manifest(),
        )
    )

    assert "# Evidence identity gold set" in rendered
    assert "Promotion ready: `false`" in rendered
    assert "Pending: `14`" in rendered
    assert "`no_human_reviews`" in rendered


def _review(case_id: str, decision: str) -> dict:
    return {
        "schema_version": EVIDENCE_IDENTITY_GOLD_SCHEMA_VERSION,
        "dataset_version": EVIDENCE_IDENTITY_GOLD_DATASET_VERSION,
        "case_id": case_id,
        "decision": decision,
        "reviewer_id": "gsus",
        "rationale": "Manual test review.",
        "reviewed_at": "2026-07-28T12:00:00+00:00",
    }
