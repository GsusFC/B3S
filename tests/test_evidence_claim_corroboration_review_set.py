from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from src.services.evidence_claim_corroboration_review_set import (
    DEFAULT_CAPTURE_PATH,
    EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION,
    EVIDENCE_CLAIM_CORROBORATION_REVIEW_EVENT_SCHEMA_VERSION,
    EvidenceClaimCorroborationReviewSetError,
    build_review_candidates,
    build_review_candidates_from_files,
    build_review_template,
    evaluate_claim_corroboration_review_set,
    load_review_candidates,
    load_review_events,
    load_review_manifest,
    load_review_selections,
    load_source_candidates,
    render_claim_corroboration_review_set_markdown,
    review_set_fingerprint,
    verify_frozen_candidate_provenance,
)
from src.services.evidence_source_review_set import (
    load_review_events as load_source_review_events,
)


def test_frozen_candidates_reproduce_from_real_capture() -> None:
    candidates = load_review_candidates()
    selections = load_review_selections()
    rebuilt = build_review_candidates_from_files()

    assert len(candidates) == 10
    assert len(selections) == 10
    assert rebuilt == candidates
    assert verify_frozen_candidate_provenance(candidates) is True
    assert len({row["source"]["url"] for row in candidates}) == 5
    assert len({row["claim"]["claim_id"] for row in candidates}) == 10
    assert {row["brand"]["domain"] for row in candidates} == {
        "vercel.com"
    }
    assert {
        row["parent_source_case_id"] for row in candidates
    } == {
        "vercel-v1-infoq",
        "vercel-v2-venturebeat",
        "vercel-v3-the-register",
        "vercel-v4-siliconangle",
        "vercel-v5-techinformed",
    }
    assert all(row["claim"]["canonical"] is False for row in candidates)
    assert all(row["runtime_effect"] is False for row in candidates)
    assert all(row["authority"] is False for row in candidates)


def test_empty_review_set_exposes_pending_work_without_authority() -> None:
    result = evaluate_claim_corroboration_review_set(
        load_review_candidates(),
        load_review_events(),
        manifest=load_review_manifest(),
    )

    assert result["review_gate_ready"] is False
    assert result["promotion_ready"] is False
    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"] == {
        "candidate_count": 10,
        "candidate_source_count": 5,
        "candidate_claim_count": 10,
        "candidate_brand_count": 1,
        "review_event_count": 0,
        "reviewed_count": 0,
        "pending_count": 10,
        "revoked_case_count": 0,
        "reviewed_source_count": 0,
        "reviewed_claim_count": 0,
        "reviewed_brand_count": 0,
        "decision_counts": {},
        "corroboration_basis_counts": {},
    }
    assert "human_reviews_incomplete" in result["promotion_blockers"]
    assert "no_human_reviews" in result["promotion_blockers"]
    assert (
        "operational_two_axis_contract_not_adopted"
        in result["promotion_blockers"]
    )
    assert (
        "semantic_paraphrase_recall_unmeasured"
        in result["promotion_blockers"]
    )


def test_review_template_contains_no_automatic_decisions() -> None:
    manifest = load_review_manifest()
    template = build_review_template(
        load_review_candidates(),
        manifest=manifest,
    )

    assert len(template) == 10
    assert {
        row["candidate_fingerprint"] for row in template
    } == {manifest["candidate_fingerprint"]}
    assert all(row["event_id"] is None for row in template)
    assert all(row["decision"] is None for row in template)
    assert all(row["corroboration_bases"] == [] for row in template)
    assert all(row["reviewer_id"] is None for row in template)
    assert all(row["runtime_effect"] is False for row in template)
    assert all(row["authority"] is False for row in template)


def test_manifest_exposes_stable_upstream_exclusion_for_v6() -> None:
    coverage = load_review_manifest()["upstream_coverage"]
    source_event = next(
        row
        for row in load_source_review_events()
        if row["event_id"] == "vercel-v6-pr-newswire-review-001"
    )

    assert coverage == [
        {
            "source_case_id": source_event["case_id"],
            "upstream_dataset": "evidence_source_review",
            "upstream_dataset_version": source_event[
                "dataset_version"
            ],
            "upstream_event_schema_version": source_event[
                "schema_version"
            ],
            "upstream_event_id": source_event["event_id"],
            "publisher_independence_decision": source_event[
                "publisher_independence_decision"
            ],
            "claim_corroboration_decision": source_event[
                "claim_corroboration_decision"
            ],
            "requires_claim_level_review": False,
            "candidate_generation": "skipped",
        }
    ]


def test_complete_reviews_close_review_gate_but_not_promotion() -> None:
    candidates = load_review_candidates()
    events = []
    for index, candidate in enumerate(candidates, start=1):
        if candidate["case_id"] == "vercel-v5-no-audited-impact":
            decision = "independently_corroborated"
            bases = ["absence_check"]
        elif candidate["case_id"] == "vercel-v3-eve-platform-portability":
            decision = "mixed"
            bases = ["mixed_provenance"]
        else:
            decision = "disputed"
            bases = ["first_party_material"]
        events.append(
            _decision(
                candidate["case_id"],
                event_id=f"review-{index}",
                decision=decision,
                bases=bases,
            )
        )
    result = evaluate_claim_corroboration_review_set(
        candidates,
        events,
        manifest=load_review_manifest(),
    )

    assert result["review_gate_ready"] is True
    assert result["promotion_ready"] is False
    assert result["summary"]["reviewed_count"] == 10
    assert result["summary"]["pending_count"] == 0
    assert result["summary"]["reviewed_source_count"] == 5
    assert result["summary"]["reviewed_claim_count"] == 10
    assert result["summary"]["decision_counts"] == {
        "disputed": 8,
        "independently_corroborated": 1,
        "mixed": 1,
    }
    assert result["promotion_blockers"] == [
        "insufficient_reviewed_real_brands",
        "operational_two_axis_contract_not_adopted",
        "semantic_paraphrase_recall_unmeasured",
    ]
    assert all(row["runtime_effect"] is False for row in result["evaluated"])
    assert all(row["authority"] is False for row in result["evaluated"])


def test_decision_can_be_revoked_and_replaced_append_only() -> None:
    candidates = load_review_candidates()
    case_id = candidates[0]["case_id"]
    first = _decision(
        case_id,
        event_id="decision-1",
        decision="disputed",
        bases=["first_party_statement"],
    )
    revocation = _revocation(
        case_id,
        event_id="revocation-2",
        sequence=2,
        previous_event_id="decision-1",
        revoked_event_id="decision-1",
    )
    replacement = _decision(
        case_id,
        event_id="decision-3",
        sequence=3,
        previous_event_id="revocation-2",
        decision="mixed",
        bases=["mixed_provenance"],
    )

    revoked_result = evaluate_claim_corroboration_review_set(
        candidates,
        [first, revocation],
        manifest=_manifest_for_events([first, revocation]),
    )
    active_result = evaluate_claim_corroboration_review_set(
        candidates,
        [first, revocation, replacement],
        manifest=_manifest_for_events(
            [first, revocation, replacement]
        ),
    )

    assert revoked_result["summary"]["reviewed_count"] == 0
    assert revoked_result["summary"]["revoked_case_count"] == 1
    assert case_id in revoked_result["pending_case_ids"]
    assert active_result["summary"]["reviewed_count"] == 1
    assert active_result["summary"]["revoked_case_count"] == 0
    assert active_result["evaluated"][0]["decision"] == "mixed"
    assert active_result["evaluated"][0]["event_id"] == "decision-3"


def test_invalid_event_chain_and_basis_fail_closed() -> None:
    candidates = load_review_candidates()
    case_id = candidates[0]["case_id"]
    invalid_basis = _decision(
        case_id,
        event_id="invalid-basis",
        decision="disputed",
        bases=["llm_guess"],
    )

    with pytest.raises(
        EvidenceClaimCorroborationReviewSetError,
        match="bases are invalid",
    ):
        evaluate_claim_corroboration_review_set(
            candidates,
            [invalid_basis],
            manifest=_manifest_for_events([invalid_basis]),
        )

    false_independent = _decision(
        case_id,
        event_id="false-independent",
        decision="independently_corroborated",
        bases=["first_party_statement"],
    )
    with pytest.raises(
        EvidenceClaimCorroborationReviewSetError,
        match="requires an independent basis",
    ):
        evaluate_claim_corroboration_review_set(
            candidates,
            [false_independent],
            manifest=_manifest_for_events([false_independent]),
        )

    first = _decision(
        case_id,
        event_id="first",
        decision="disputed",
        bases=["unresolved"],
    )
    gap = _decision(
        case_id,
        event_id="gap",
        sequence=3,
        previous_event_id="first",
        decision="mixed",
        bases=["mixed_provenance"],
    )
    with pytest.raises(
        EvidenceClaimCorroborationReviewSetError,
        match="sequence gap",
    ):
        evaluate_claim_corroboration_review_set(
            candidates,
            [first, gap],
            manifest=_manifest_for_events([first, gap]),
        )


def test_fingerprint_and_capture_tampering_fail_closed() -> None:
    candidates = load_review_candidates()
    tampered = deepcopy(candidates)
    tampered[0]["claim"]["statement"] = "A different claim."

    assert verify_frozen_candidate_provenance(tampered) is False
    with pytest.raises(
        EvidenceClaimCorroborationReviewSetError,
        match="candidate fingerprint mismatch",
    ):
        evaluate_claim_corroboration_review_set(
            tampered,
            [],
            manifest=load_review_manifest(),
        )

    event = _decision(
        candidates[0]["case_id"],
        event_id="review-event",
        decision="disputed",
        bases=["unresolved"],
    )
    pinned_manifest = deepcopy(load_review_manifest())
    pinned_manifest["review_event_fingerprint"] = "0" * 64
    with pytest.raises(
        EvidenceClaimCorroborationReviewSetError,
        match="event fingerprint mismatch",
    ):
        evaluate_claim_corroboration_review_set(
            candidates,
            [event],
            manifest=pinned_manifest,
        )

    selections = load_review_selections()
    broken_selection = deepcopy(selections)
    broken_selection[0]["evidence_span"] = (
        "This sentence does not exist in the captured article text."
    )
    capture_bytes = DEFAULT_CAPTURE_PATH.read_bytes()
    with pytest.raises(
        EvidenceClaimCorroborationReviewSetError,
        match="must occur exactly once",
    ):
        build_review_candidates(
            broken_selection,
            load_source_candidates(),
            json.loads(capture_bytes),
            capture_artifact_path=(
                "fixtures/vercel/vercel_fresh_capture_envelope.json"
            ),
            capture_artifact_sha256=hashlib.sha256(
                capture_bytes
            ).hexdigest(),
        )


def test_review_fingerprint_must_match_candidates_and_be_uniform() -> None:
    candidates = load_review_candidates()
    manifest = load_review_manifest()
    mismatched = _decision(
        candidates[0]["case_id"],
        event_id="mismatched",
        decision="disputed",
        bases=["unresolved"],
    )
    mismatched["candidate_fingerprint"] = "0" * 64

    with pytest.raises(
        EvidenceClaimCorroborationReviewSetError,
        match="candidate fingerprint mismatch",
    ):
        evaluate_claim_corroboration_review_set(
            candidates,
            [mismatched],
            manifest=manifest,
        )

    mixed = [
        _decision(
            candidates[0]["case_id"],
            event_id="first-candidate-set",
            decision="disputed",
            bases=["unresolved"],
        ),
        _decision(
            candidates[1]["case_id"],
            event_id="second-candidate-set",
            decision="disputed",
            bases=["unresolved"],
        ),
    ]
    mixed[1]["candidate_fingerprint"] = "0" * 64
    with pytest.raises(
        EvidenceClaimCorroborationReviewSetError,
        match="mix candidate fingerprints",
    ):
        evaluate_claim_corroboration_review_set(
            candidates,
            mixed,
            manifest=manifest,
        )


def test_markdown_reports_claim_scoped_queue() -> None:
    rendered = render_claim_corroboration_review_set_markdown(
        evaluate_claim_corroboration_review_set(
            load_review_candidates(),
            [],
            manifest=load_review_manifest(),
        )
    )

    assert "# Evidence claim-corroboration review set" in rendered
    assert "Candidates: `10`" in rendered
    assert "Sources: `5`" in rendered
    assert "Claims: `10`" in rendered
    assert "Pending: `10`" in rendered
    assert "human_reviews_incomplete" in rendered


def _manifest_for_events(events: list[dict]) -> dict:
    manifest = deepcopy(load_review_manifest())
    manifest["review_event_fingerprint"] = review_set_fingerprint(events)
    return manifest


def _decision(
    case_id: str,
    *,
    event_id: str,
    decision: str,
    bases: list[str],
    sequence: int = 1,
    previous_event_id: str | None = None,
) -> dict:
    return {
        "schema_version": (
            EVIDENCE_CLAIM_CORROBORATION_REVIEW_EVENT_SCHEMA_VERSION
        ),
        "dataset_version": (
            EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION
        ),
        "candidate_fingerprint": load_review_manifest()[
            "candidate_fingerprint"
        ],
        "event_id": event_id,
        "case_id": case_id,
        "sequence": sequence,
        "event_type": "decision",
        "previous_event_id": previous_event_id,
        "decision": decision,
        "corroboration_bases": bases,
        "revoked_event_id": None,
        "reviewer_id": "gsus",
        "rationale": "Human claim-scoped source review.",
        "reviewed_at": "2026-07-29T12:00:00Z",
        "runtime_effect": False,
        "authority": False,
    }


def _revocation(
    case_id: str,
    *,
    event_id: str,
    sequence: int,
    previous_event_id: str,
    revoked_event_id: str,
) -> dict:
    return {
        "schema_version": (
            EVIDENCE_CLAIM_CORROBORATION_REVIEW_EVENT_SCHEMA_VERSION
        ),
        "dataset_version": (
            EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION
        ),
        "candidate_fingerprint": load_review_manifest()[
            "candidate_fingerprint"
        ],
        "event_id": event_id,
        "case_id": case_id,
        "sequence": sequence,
        "event_type": "revocation",
        "previous_event_id": previous_event_id,
        "decision": None,
        "corroboration_bases": [],
        "revoked_event_id": revoked_event_id,
        "reviewer_id": "gsus",
        "rationale": "Revoke the prior decision.",
        "reviewed_at": "2026-07-29T12:05:00Z",
        "runtime_effect": False,
        "authority": False,
    }
