from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.services.evidence_claim_relation_gold_set import (
    DEFAULT_GOLD_ROOT,
    EVIDENCE_CLAIM_RELATION_GOLD_DATASET_VERSION,
    EVIDENCE_CLAIM_RELATION_GOLD_SCHEMA_VERSION,
    EvidenceClaimRelationGoldSetError,
    build_review_template,
    evaluate_claim_relation_gold_set,
    gold_candidate_fingerprint,
    load_gold_candidates,
    load_gold_manifest,
    load_gold_reviews,
    predict_claim_relation_disposition,
    render_claim_relation_gold_set_markdown,
)


def test_committed_human_reviews_pass_relation_metrics_but_not_real_gate() -> None:
    result = evaluate_claim_relation_gold_set(
        load_gold_candidates(),
        load_gold_reviews(DEFAULT_GOLD_ROOT / "reviews.jsonl"),
        manifest=load_gold_manifest(),
    )

    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"]["reviewed_count"] == 13
    assert result["summary"]["pending_count"] == 0
    assert result["summary"]["false_replacement_count"] == 0
    assert result["summary"]["missed_replacement_count"] == 0
    assert result["summary"]["replacement_precision"] == 1.0
    assert result["summary"]["replacement_recall"] == 1.0
    assert result["summary"]["exact_agreement_rate"] == 0.846154
    assert result["promotion_ready"] is False
    assert result["promotion_blockers"] == [
        "insufficient_reviewed_real_history_cases",
        "insufficient_reviewed_real_replacements",
    ]


def test_versioned_candidate_set_covers_relation_risks_and_real_history() -> None:
    manifest = load_gold_manifest()
    candidates = load_gold_candidates()
    case_types = {str(row["case_type"]) for row in candidates}

    assert len(candidates) == 13
    assert set(manifest["required_case_types"]) <= case_types
    assert {str(row["split"]) for row in candidates} == {
        "calibration",
        "test",
    }
    assert manifest["candidate_fingerprint"] == gold_candidate_fingerprint(
        candidates
    )
    assert sum(
        row["provenance"] == "real_history" for row in candidates
    ) == 3
    assert all("proposed_review" in row for row in candidates)
    assert all("reviewer_id" not in row for row in candidates)


def test_predictions_keep_absence_relocation_and_reacquisition_out_of_replacement() -> None:
    candidates = {
        str(row["case_id"]): row for row in load_gold_candidates()
    }

    assert predict_claim_relation_disposition(
        candidates["cal-sequential-replacement"]
    )["decision"] == "replacement"
    assert predict_claim_relation_disposition(
        candidates["cal-simultaneous-coexistence"]
    )["decision"] == "coexistence"
    assert predict_claim_relation_disposition(
        candidates["cal-disappearance"]
    )["decision"] == "no_relation"
    assert predict_claim_relation_disposition(
        candidates["cal-source-relocation"]
    )["decision"] == "no_relation"
    assert predict_claim_relation_disposition(
        candidates["test-reacquisition"]
    )["decision"] == "no_relation"
    assert predict_claim_relation_disposition(
        candidates["test-multi-step-replacement"]
    )["relation_candidate_counts"] == {
        "replacement_candidate": 3
    }


def test_real_history_cases_do_not_invent_replacement() -> None:
    candidates = [
        row
        for row in load_gold_candidates()
        if row["provenance"] == "real_history"
    ]

    assert {
        str(row["case_id"]): predict_claim_relation_disposition(row)[
            "decision"
        ]
        for row in candidates
    } == {
        "real-liminal-current-mission": "no_relation",
        "real-robin-single-mission": "no_relation",
        "real-vercel-not-reacquired": "no_relation",
    }


def test_candidate_hints_cannot_make_unreviewed_set_ready() -> None:
    result = evaluate_claim_relation_gold_set(
        load_gold_candidates(),
        [],
        manifest=load_gold_manifest(),
    )

    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["promotion_ready"] is False
    assert result["summary"]["candidate_count"] == 13
    assert result["summary"]["reviewed_count"] == 0
    assert result["summary"]["pending_count"] == 13
    assert result["summary"]["candidate_prediction_counts"] == {
        "coexistence": 2,
        "no_relation": 8,
        "replacement": 3,
    }
    assert (
        result["summary"]["candidate_predicted_replacement_count"]
        == 3
    )
    assert (
        result["summary"]["reviewed_predicted_replacement_count"]
        == 0
    )
    assert result["promotion_blockers"] == [
        "human_reviews_incomplete",
        "insufficient_reviewed_real_history_cases",
        "insufficient_reviewed_real_replacements",
        "no_human_reviews",
    ]


def test_review_template_is_unsigned() -> None:
    rows = build_review_template(load_gold_candidates())

    assert len(rows) == 13
    assert all(row["decision"] is None for row in rows)
    assert all(row["reviewer_id"] is None for row in rows)
    assert all(row["reviewed_at"] is None for row in rows)


def test_evaluation_does_not_return_raw_claim_text() -> None:
    candidate = load_gold_candidates()[0]
    result = evaluate_claim_relation_gold_set(
        [candidate],
        [
            _review(
                str(candidate["case_id"]),
                predict_claim_relation_disposition(candidate)["decision"],
            )
        ],
        manifest=_single_case_manifest(candidate),
    )

    assert "Our mission is to simplify finance." not in json.dumps(result)


def test_perfect_controlled_agreement_still_cannot_replace_missing_real_changes() -> None:
    candidates = load_gold_candidates()
    reviews = [
        _review(
            str(candidate["case_id"]),
            predict_claim_relation_disposition(candidate)["decision"],
        )
        for candidate in candidates
    ]

    result = evaluate_claim_relation_gold_set(
        candidates,
        reviews,
        manifest=load_gold_manifest(),
    )

    assert result["summary"]["reviewed_count"] == 13
    assert result["summary"]["replacement_precision"] == 1.0
    assert result["summary"]["replacement_recall"] == 1.0
    assert result["summary"]["exact_agreement_rate"] == 1.0
    assert result["summary"]["reviewed_real_history_count"] == 3
    assert result["summary"]["reviewed_real_replacement_count"] == 0
    assert result["promotion_ready"] is False
    assert result["promotion_blockers"] == [
        "insufficient_reviewed_real_history_cases",
        "insufficient_reviewed_real_replacements",
    ]


def test_relaxed_test_thresholds_can_prove_evaluator_gate_logic() -> None:
    candidates = load_gold_candidates()
    reviews = [
        _review(
            str(candidate["case_id"]),
            predict_claim_relation_disposition(candidate)["decision"],
        )
        for candidate in candidates
    ]
    manifest = deepcopy(load_gold_manifest())
    manifest["promotion_thresholds"][
        "minimum_reviewed_real_history_cases"
    ] = 3
    manifest["promotion_thresholds"][
        "minimum_reviewed_real_replacements"
    ] = 0

    result = evaluate_claim_relation_gold_set(
        candidates,
        reviews,
        manifest=manifest,
    )

    assert result["promotion_ready"] is True
    assert result["promotion_blockers"] == []


def test_critical_false_replacement_is_blocking() -> None:
    candidate = deepcopy(
        next(
            row
            for row in load_gold_candidates()
            if row["case_id"] == "cal-sequential-replacement"
        )
    )
    candidate["critical"] = True
    manifest = _single_case_manifest(candidate)

    result = evaluate_claim_relation_gold_set(
        [candidate],
        [_review(str(candidate["case_id"]), "no_relation")],
        manifest=manifest,
    )

    assert result["summary"]["critical_false_replacement_count"] == 1
    assert result["critical_false_replacement_case_ids"] == [
        candidate["case_id"]
    ]
    assert "critical_false_replacements_present" in result[
        "promotion_blockers"
    ]
    assert "replacement_precision_below_threshold" in result[
        "promotion_blockers"
    ]


def test_missed_replacement_is_measured() -> None:
    candidate = deepcopy(
        next(
            row
            for row in load_gold_candidates()
            if row["case_id"] == "cal-disappearance"
        )
    )

    result = evaluate_claim_relation_gold_set(
        [candidate],
        [_review(str(candidate["case_id"]), "replacement")],
        manifest=_single_case_manifest(candidate),
    )

    assert result["summary"]["missed_replacement_count"] == 1
    assert result["missed_replacement_case_ids"] == [candidate["case_id"]]
    assert result["summary"]["replacement_recall"] == 0.0
    assert "replacement_recall_below_threshold" in result[
        "promotion_blockers"
    ]


def test_manifest_fingerprint_rejects_silent_candidate_edits() -> None:
    candidates = load_gold_candidates()
    candidates[0]["reports"][0]["claims"][0]["content"] = (
        "Silently edited claim."
    )

    with pytest.raises(
        EvidenceClaimRelationGoldSetError,
        match="fingerprint",
    ):
        evaluate_claim_relation_gold_set(
            candidates,
            [],
            manifest=load_gold_manifest(),
        )


def test_real_history_source_report_ids_must_match_timeline() -> None:
    candidate = deepcopy(
        next(
            row
            for row in load_gold_candidates()
            if row["provenance"] == "real_history"
        )
    )
    candidate["source_report_ids"] = ["unrelated-report"]

    with pytest.raises(
        EvidenceClaimRelationGoldSetError,
        match="must match candidate reports",
    ):
        predict_claim_relation_disposition(candidate)


def test_duplicate_review_is_rejected() -> None:
    candidate = load_gold_candidates()[0]
    review = _review(str(candidate["case_id"]), "no_relation")

    with pytest.raises(
        EvidenceClaimRelationGoldSetError,
        match="duplicate review",
    ):
        evaluate_claim_relation_gold_set(
            [candidate],
            [review, review],
            manifest=_single_case_manifest(candidate),
        )


def test_markdown_reports_pending_human_and_real_change_work() -> None:
    rendered = render_claim_relation_gold_set_markdown(
        evaluate_claim_relation_gold_set(
            load_gold_candidates(),
            [],
            manifest=load_gold_manifest(),
        )
    )

    assert "# Evidence claim relation gold set" in rendered
    assert "Promotion ready: `false`" in rendered
    assert "Pending: `13`" in rendered
    assert "`no_human_reviews`" in rendered
    assert "`insufficient_reviewed_real_replacements`" in rendered


def _single_case_manifest(candidate: dict) -> dict:
    manifest = deepcopy(load_gold_manifest())
    manifest["candidate_fingerprint"] = gold_candidate_fingerprint(
        [candidate]
    )
    manifest["promotion_thresholds"][
        "minimum_reviewed_real_history_cases"
    ] = 0
    manifest["promotion_thresholds"][
        "minimum_reviewed_real_replacements"
    ] = 0
    return manifest


def _review(case_id: str, decision: str) -> dict:
    return {
        "schema_version": (
            EVIDENCE_CLAIM_RELATION_GOLD_SCHEMA_VERSION
        ),
        "dataset_version": (
            EVIDENCE_CLAIM_RELATION_GOLD_DATASET_VERSION
        ),
        "case_id": case_id,
        "decision": decision,
        "reviewer_id": "gsus",
        "rationale": "Manual test review.",
        "reviewed_at": "2026-07-29T12:00:00+00:00",
    }
