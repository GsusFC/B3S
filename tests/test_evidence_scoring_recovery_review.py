from __future__ import annotations

from copy import deepcopy

import pytest

from src.services.evidence_scoring_recovery_review import (
    EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION,
    EvidenceScoringRecoveryReviewError,
    build_recovery_review_supplement_packet,
    build_recovery_review_supplement_packet_from_preview,
    build_recovery_review_candidates,
    build_recovery_review_template,
    evaluate_recovery_reviews,
    merge_recovery_review_supplements,
    validate_recovery_review_supplement_against_preview,
    validate_recovery_review_supplement_packet,
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


def test_current_rubric_unknown_tile_fails_closed() -> None:
    preview = _preview()
    preview["accepted_evidence"][0].update(
        {
            "present_in_latest": True,
            "component_key": "value_proposition",
            "tile_id": "VP1",
        }
    )
    preview["recoveries"] = []

    with pytest.raises(
        EvidenceScoringRecoveryReviewError,
        match=r"unknown recovery tile: value_proposition\.VP1",
    ):
        build_recovery_review_candidates(
            [{"lane": "history", "preview": preview}]
        )


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


def test_supplement_packet_merges_exact_non_authoritative_candidates() -> None:
    base = build_recovery_review_candidates(
        [{"lane": "history", "preview": _preview()}]
    )
    supplemental = _supplement_candidates()
    packet = build_recovery_review_supplement_packet(
        brand_identity="example.com",
        rubric_version="baldosas-v3-1",
        base_candidates=base,
        evidence_identity_state_fingerprint="d" * 64,
        candidates=supplemental,
    )

    validate_recovery_review_supplement_packet(packet)
    merged = merge_recovery_review_supplements(
        base,
        [packet],
        brand_domain="example.com",
        rubric_version="baldosas-v3-1",
        evidence_identity_state_fingerprint="d" * 64,
        accepted_evidence_ids=["c" * 64],
    )

    assert len(merged["candidates"]) == 2
    assert merged["summary"]["active_packet_count"] == 1
    assert merged["summary"]["blocked_packet_count"] == 0
    assert merged["summary"]["authority"] is False
    assert packet["authority"] is False
    assert packet["runtime_effect"] is False


def test_unrelated_generation_context_drift_does_not_expire_supplement() -> None:
    base = build_recovery_review_candidates(
        [{"lane": "history", "preview": _preview()}]
    )
    supplemental = _supplement_candidates()
    packet = build_recovery_review_supplement_packet(
        brand_identity="example.com",
        rubric_version="baldosas-v3-1",
        base_candidates=base,
        evidence_identity_state_fingerprint="d" * 64,
        candidates=supplemental,
    )

    merged = merge_recovery_review_supplements(
        [],
        [packet],
        brand_domain="example.com",
        rubric_version="baldosas-v3-1",
        evidence_identity_state_fingerprint="e" * 64,
        accepted_evidence_ids=["c" * 64],
    )

    assert merged["candidates"] == supplemental
    assert merged["summary"]["active_packet_count"] == 1
    assert merged["summary"]["generation_context_drift_count"] == 1


def test_supplement_is_blocked_when_cited_identity_is_not_accepted() -> None:
    supplemental = _supplement_candidates()
    packet = build_recovery_review_supplement_packet(
        brand_identity="example.com",
        rubric_version="baldosas-v3-1",
        base_candidates=[],
        evidence_identity_state_fingerprint="d" * 64,
        candidates=supplemental,
    )

    merged = merge_recovery_review_supplements(
        [],
        [packet],
        brand_domain="example.com",
        rubric_version="baldosas-v3-1",
        evidence_identity_state_fingerprint="d" * 64,
        accepted_evidence_ids=[],
    )

    assert merged["candidates"] == []
    assert merged["summary"]["active_packet_count"] == 0
    assert merged["summary"]["blocked_packet_count"] == 1


def test_mapper_learning_same_relation_deduplicates_supplement() -> None:
    supplemental = _supplement_candidates()
    packet = build_recovery_review_supplement_packet(
        brand_identity="example.com",
        rubric_version="baldosas-v3-1",
        base_candidates=[],
        evidence_identity_state_fingerprint="d" * 64,
        candidates=supplemental,
    )

    merged = merge_recovery_review_supplements(
        supplemental,
        [packet],
        brand_domain="example.com",
        rubric_version="baldosas-v3-1",
        evidence_identity_state_fingerprint="d" * 64,
        accepted_evidence_ids=["c" * 64],
    )

    assert merged["candidates"] == supplemental
    assert merged["summary"]["redundant_case_ids"] == [
        supplemental[0]["case_id"]
    ]


def test_supplement_tampering_fails_closed() -> None:
    packet = build_recovery_review_supplement_packet(
        brand_identity="example.com",
        rubric_version="baldosas-v3-1",
        base_candidates=[],
        evidence_identity_state_fingerprint="d" * 64,
        candidates=_supplement_candidates(),
    )
    packet["candidates"][0]["evidence"]["quote"] = "Changed silently."

    with pytest.raises(
        EvidenceScoringRecoveryReviewError,
        match="candidate fingerprint",
    ):
        validate_recovery_review_supplement_packet(packet)


def test_preview_based_supplement_reuses_exact_accepted_evidence() -> None:
    preview = _supplement_source_preview()
    source_catalog = _supplement_source_catalog()
    packet = build_recovery_review_supplement_packet_from_preview(
        preview,
        source_catalog=source_catalog,
        proposals=[
            {
                "component_key": "magnetism",
                "tile_id": "MG2",
                "passages": [
                    {
                        "evidence_id": "c" * 64,
                        "quote": "End the chase between companies.",
                    }
                ],
            },
            {
                "component_key": "magnetism",
                "tile_id": "MG4",
                "passages": [
                    {
                        "evidence_id": "c" * 64,
                        "quote": "End the chase between companies.",
                    },
                    {
                        "evidence_id": "e" * 64,
                        "quote": "Chasing became the second job.",
                    },
                ],
            },
        ],
    )

    validate_recovery_review_supplement_against_preview(
        packet,
        preview,
        source_catalog,
    )
    by_tile = {
        candidate["tile"]["tile_id"]: candidate
        for candidate in packet["candidates"]
    }
    assert by_tile["MG2"]["evidence"]["quote"] == (
        "End the chase between companies."
    )
    assert by_tile["MG4"]["evidence"]["quote"] == (
        "End the chase between companies.\n\n"
        "Chasing became the second job."
    )
    assert by_tile["MG4"]["evidence"][
        "supplement_source_passages"
    ] == [
        {
            "evidence_id": "c" * 64,
            "quote": "End the chase between companies.",
        },
        {
            "evidence_id": "e" * 64,
            "quote": "Chasing became the second job.",
        },
    ]
    assert all(
        candidate["contexts"][0]["review_scope"]
        == "mapper_omission_supplement"
        for candidate in packet["candidates"]
    )


def test_preview_evidence_drift_invalidates_supplement_projection() -> None:
    preview = _supplement_source_preview()
    source_catalog = _supplement_source_catalog()
    packet = build_recovery_review_supplement_packet_from_preview(
        preview,
        source_catalog=source_catalog,
        proposals=[
            {
                "component_key": "magnetism",
                "tile_id": "MG2",
                "passages": [
                    {
                        "evidence_id": "c" * 64,
                        "quote": "End the chase between companies.",
                    }
                ],
            }
        ],
    )
    changed = deepcopy(source_catalog)
    changed["entries"][0]["content"] = "Changed silently."

    with pytest.raises(
        EvidenceScoringRecoveryReviewError,
        match="not literal in accepted evidence",
    ):
        validate_recovery_review_supplement_against_preview(
            packet,
            preview,
            changed,
        )


def test_supplement_quote_must_preserve_literal_case() -> None:
    with pytest.raises(
        EvidenceScoringRecoveryReviewError,
        match="not literal in accepted evidence",
    ):
        build_recovery_review_supplement_packet_from_preview(
            _supplement_source_preview(),
            source_catalog=_supplement_source_catalog(),
            proposals=[
                {
                    "component_key": "magnetism",
                    "tile_id": "MG2",
                    "passages": [
                        {
                            "evidence_id": "c" * 64,
                            "quote": "end the chase between companies.",
                        }
                    ],
                }
            ],
        )


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


def _supplement_candidates() -> list[dict]:
    preview = _preview()
    preview["accepted_evidence"][0].update(
        {
            "tile_evidence_id": "b" * 64,
            "source_evidence_ids": ["c" * 64],
            "component_key": "magnetism",
            "tile_id": "MG2",
            "present_in_latest": True,
        }
    )
    preview["recoveries"] = []
    return build_recovery_review_candidates(
        [{"lane": "supplement", "preview": preview}]
    )


def _supplement_source_preview() -> dict:
    return _preview()


def _supplement_source_catalog() -> dict:
    common = {
        "document_id": "f" * 64,
        "url": "https://example.com",
        "source_class": "owned_copy",
        "evidence_type": "raw_input",
        "first_seen_at": "2026-07-01T08:00:00+00:00",
        "last_seen_at": "2026-07-01T08:00:00+00:00",
        "observation_count": 1,
        "present_in_latest": True,
        "adjudication_state": "accepted",
    }
    return {
        "schema_version": "evidence-accepted-passage-catalog-v1",
        "runtime_effect": False,
        "authority": False,
        "brand": {"name": "Example", "domain": "example.com"},
        "identity_state_fingerprint": "d" * 64,
        "entries": [
            {
                **common,
                "evidence_id": "c" * 64,
                "content": "End the chase between companies.",
            },
            {
                **common,
                "evidence_id": "e" * 64,
                "content": "Chasing became the second job.",
            },
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
