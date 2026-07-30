from __future__ import annotations

from copy import deepcopy

import pytest

from src.services.evidence_claim_tile_review_set import (
    EVIDENCE_CLAIM_TILE_REVIEW_DATASET_VERSION,
    EVIDENCE_CLAIM_TILE_REVIEW_SCHEMA_VERSION,
    EvidenceClaimTileReviewSetError,
    build_review_template,
    evaluate_claim_tile_review_set,
    load_review_candidates,
    load_review_manifest,
    load_reviews,
    render_claim_tile_review_set_markdown,
    review_candidate_fingerprint,
)


def test_frozen_set_contains_all_eight_real_mapping_candidates() -> None:
    manifest = load_review_manifest()
    candidates = load_review_candidates()
    reviews = load_reviews()

    assert len(candidates) == 8
    assert (
        manifest["candidate_fingerprint"]
        == review_candidate_fingerprint(candidates)
    )
    assert len(reviews) == 8
    assert (
        manifest["review_fingerprint"]
        == review_candidate_fingerprint(reviews)
    )
    assert {
        str(row["brand"]["domain"]) for row in candidates
    } == {"robincap.com", "vercel.com"}
    assert sum(
        row["brand"]["domain"] == "vercel.com" for row in candidates
    ) == 7
    assert len(
        {str(row["claim"]["claim_variant_id"]) for row in candidates}
    ) == 2
    assert {
        str(row["tile"]["polarity"]) for row in candidates
    } == {"supports"}
    assert len(
        {str(row["mapping"]["mapping_id"]) for row in candidates}
    ) == 8
    assert all(row["runtime_effect"] is False for row in candidates)
    assert all(row["authority"] is False for row in candidates)


def test_unreviewed_real_candidates_fail_closed() -> None:
    result = evaluate_claim_tile_review_set(
        load_review_candidates(),
        [],
        manifest=_unfrozen_manifest(),
    )

    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["review_gate_ready"] is False
    assert result["promotion_ready"] is False
    assert result["summary"]["candidate_count"] == 8
    assert result["summary"]["reviewed_count"] == 0
    assert result["summary"]["pending_count"] == 8
    assert result["summary"]["candidate_brand_count"] == 2
    assert result["summary"]["candidate_claim_variant_count"] == 2
    assert result["summary"]["candidate_polarity_counts"] == {
        "supports": 8
    }
    assert result["review_blockers"] == [
        "human_reviews_incomplete",
        "no_human_reviews",
    ]
    assert result["promotion_blockers"] == [
        "human_reviews_incomplete",
        "insufficient_reviewed_claim_variants",
        "insufficient_reviewed_real_brands",
        "no_human_reviews",
        "promotion_policy_not_adopted",
        "required_polarity_coverage_missing",
    ]


def test_perfect_review_of_current_set_cannot_fake_generalization() -> None:
    candidates = load_review_candidates()
    reviews = [
        _review(str(candidate["case_id"]), "accepted")
        for candidate in candidates
    ]

    result = evaluate_claim_tile_review_set(
        candidates,
        reviews,
        manifest=_unfrozen_manifest(),
    )

    assert result["review_gate_ready"] is True
    assert result["review_blockers"] == []
    assert result["summary"]["confirmed_mapping_precision"] == 1.0
    assert result["summary"]["reviewed_real_brand_count"] == 2
    assert result["summary"]["reviewed_claim_variant_count"] == 2
    assert result["summary"]["reviewed_polarity_counts"] == {
        "supports": 8
    }
    assert result["promotion_ready"] is False
    assert result["promotion_blockers"] == [
        "insufficient_reviewed_claim_variants",
        "insufficient_reviewed_real_brands",
        "promotion_policy_not_adopted",
        "required_polarity_coverage_missing",
    ]
    assert result["missing_required_polarities"] == [
        "insufficient_evidence",
        "weakens",
    ]


def test_critical_rejection_measures_a_false_mapping() -> None:
    candidates = load_review_candidates()
    reviews = [
        _review(
            str(candidate["case_id"]),
            (
                "rejected"
                if candidate["case_id"]
                == "real-vercel-core-purpose-pr1-7c2aa8ce"
                else "accepted"
            ),
        )
        for candidate in candidates
    ]

    result = evaluate_claim_tile_review_set(
        candidates,
        reviews,
        manifest=_unfrozen_manifest(),
    )

    assert result["summary"]["rejected_count"] == 1
    assert result["summary"]["critical_non_accept_count"] == 1
    assert result["summary"]["confirmed_mapping_precision"] == 0.875
    assert result["critical_non_accept_case_ids"] == [
        "real-vercel-core-purpose-pr1-7c2aa8ce"
    ]
    assert result["review_blockers"] == [
        "critical_non_accepts_present",
        "mapping_precision_below_threshold",
    ]


def test_candidates_expose_tile_contract_not_only_literal_quote() -> None:
    candidates = {
        str(row["case_id"]): row for row in load_review_candidates()
    }
    purpose = candidates[
        "real-vercel-core-purpose-pr1-7c2aa8ce"
    ]
    coherence = candidates["real-vercel-mission-m4-f271a022"]

    assert purpose["claim"]["claim_type"] == "mission"
    assert purpose["tile"]["component_key"] == "core_purpose"
    assert "misión funcional" in purpose["tile"]["evidence_contract"][
        "reject"
    ]
    assert "faltan los bloques" in coherence["tile"][
        "evidence_contract"
    ]["sin_evidencia"]


def test_frozen_human_reviews_capture_the_real_false_mapping() -> None:
    result = evaluate_claim_tile_review_set(
        load_review_candidates(),
        load_reviews(),
        manifest=load_review_manifest(),
    )

    assert result["summary"]["reviewed_count"] == 8
    assert result["summary"]["pending_count"] == 0
    assert result["summary"]["accepted_count"] == 7
    assert result["summary"]["rejected_count"] == 1
    assert result["summary"]["confirmed_mapping_precision"] == 0.875
    assert result["critical_non_accept_case_ids"] == [
        "real-vercel-mission-m2-44c34065"
    ]
    assert result["review_gate_ready"] is False
    assert result["promotion_ready"] is False


def test_review_template_is_unsigned() -> None:
    manifest = load_review_manifest()
    rows = build_review_template(
        load_review_candidates(),
        manifest=manifest,
    )

    assert len(rows) == 8
    assert {
        row["candidate_fingerprint"] for row in rows
    } == {manifest["candidate_fingerprint"]}
    assert {
        row["review_packet_fingerprint"] for row in rows
    } == {manifest["review_packet_fingerprint"]}
    assert all(row["decision"] is None for row in rows)
    assert all(row["reviewer_id"] is None for row in rows)
    assert all(row["reviewed_at"] is None for row in rows)


def test_manifest_fingerprint_rejects_silent_candidate_edit() -> None:
    candidates = load_review_candidates()
    candidates[0]["tile"]["condition"] = "Silently changed."

    with pytest.raises(
        EvidenceClaimTileReviewSetError,
        match="fingerprint",
    ):
        evaluate_claim_tile_review_set(
            candidates,
            [],
            manifest=load_review_manifest(),
        )


def test_packet_fingerprint_rejects_manifest_contract_drift() -> None:
    manifest = deepcopy(load_review_manifest())
    manifest["decision_semantics"]["accepted"] = "Changed after review."

    with pytest.raises(
        EvidenceClaimTileReviewSetError,
        match="review packet fingerprint mismatch",
    ):
        evaluate_claim_tile_review_set(
            load_review_candidates(),
            [],
            manifest=manifest,
        )


def test_duplicate_review_is_rejected() -> None:
    candidate = load_review_candidates()[0]
    review = _review(str(candidate["case_id"]), "accepted")
    manifest = deepcopy(load_review_manifest())
    manifest["candidate_fingerprint"] = review_candidate_fingerprint(
        [candidate]
    )

    with pytest.raises(
        EvidenceClaimTileReviewSetError,
        match="duplicate",
    ):
        evaluate_claim_tile_review_set(
            [candidate],
            [review, review],
            manifest=manifest,
        )


def test_review_fingerprint_must_match_candidates_and_be_uniform() -> None:
    candidates = load_review_candidates()
    manifest = _unfrozen_manifest()
    mismatched = [_review(candidates[0]["case_id"], "accepted")]
    mismatched[0]["candidate_fingerprint"] = "0" * 64

    with pytest.raises(
        EvidenceClaimTileReviewSetError,
        match="candidate fingerprint mismatch",
    ):
        evaluate_claim_tile_review_set(
            candidates,
            mismatched,
            manifest=manifest,
        )

    mixed = [
        _review(candidates[0]["case_id"], "accepted"),
        _review(candidates[1]["case_id"], "accepted"),
    ]
    mixed[1]["candidate_fingerprint"] = "0" * 64
    with pytest.raises(
        EvidenceClaimTileReviewSetError,
        match="mix candidate fingerprints",
    ):
        evaluate_claim_tile_review_set(
            candidates,
            mixed,
            manifest=manifest,
        )

    mismatched_packet = [
        _review(candidates[0]["case_id"], "accepted")
    ]
    mismatched_packet[0]["review_packet_fingerprint"] = "0" * 64
    with pytest.raises(
        EvidenceClaimTileReviewSetError,
        match="review packet fingerprint mismatch",
    ):
        evaluate_claim_tile_review_set(
            candidates,
            mismatched_packet,
            manifest=manifest,
        )

    mixed_packets = [
        _review(candidates[0]["case_id"], "accepted"),
        _review(candidates[1]["case_id"], "accepted"),
    ]
    mixed_packets[1]["review_packet_fingerprint"] = "0" * 64
    with pytest.raises(
        EvidenceClaimTileReviewSetError,
        match="mix review packet fingerprints",
    ):
        evaluate_claim_tile_review_set(
            candidates,
            mixed_packets,
            manifest=manifest,
        )


def test_markdown_reports_pending_review_and_coverage() -> None:
    rendered = render_claim_tile_review_set_markdown(
        evaluate_claim_tile_review_set(
            load_review_candidates(),
            [],
            manifest=_unfrozen_manifest(),
        )
    )

    assert "# Evidence claim-to-tile review set" in rendered
    assert "Review gate ready: `false`" in rendered
    assert "Candidates: `8`" in rendered
    assert "Candidate brands: `2`" in rendered
    assert "`promotion_policy_not_adopted`" in rendered


def _review(case_id: str, decision: str) -> dict:
    return {
        "schema_version": EVIDENCE_CLAIM_TILE_REVIEW_SCHEMA_VERSION,
        "dataset_version": EVIDENCE_CLAIM_TILE_REVIEW_DATASET_VERSION,
        "case_id": case_id,
        "candidate_fingerprint": load_review_manifest()[
            "candidate_fingerprint"
        ],
        "review_packet_fingerprint": load_review_manifest()[
            "review_packet_fingerprint"
        ],
        "decision": decision,
        "reviewer_id": "gsus",
        "rationale": "Manual mapping review.",
        "reviewed_at": "2026-07-29T13:00:00+02:00",
    }


def _unfrozen_manifest() -> dict:
    manifest = deepcopy(load_review_manifest())
    manifest["review_fingerprint"] = None
    return manifest
