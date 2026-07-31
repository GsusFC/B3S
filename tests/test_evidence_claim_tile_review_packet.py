from __future__ import annotations

from copy import deepcopy

import pytest

from src.services.evidence_claim_tile_ledger import (
    build_evidence_claim_tile_ledger,
)
from src.services.evidence_claim_tile_review_packet import (
    EvidenceClaimTileReviewPacketError,
    build_evidence_claim_tile_review_packet,
    packet_candidate_for_subject,
    validate_evidence_claim_tile_review_packet,
)
from src.sv9.rubric import RUBRIC_VERSION


CLAIM = "Our mission is to make financial work radically simpler."


def test_packet_binds_complete_private_context_without_leaking_public_text() -> None:
    report = _report("one", "2026-07-01T08:00:00Z")

    packet = build_evidence_claim_tile_review_packet([report])

    manifest = packet["manifest"]
    candidate = packet["candidates"][0]
    assert manifest["candidate_count"] == 1
    assert len(manifest["candidate_fingerprint"]) == 64
    assert len(manifest["review_packet_fingerprint"]) == 64
    assert candidate["subject_id"] == candidate["mapping"]["mapping_id"]
    assert candidate["source"]["url"] == "https://example.com/about"
    assert candidate["source"]["passage"] == CLAIM
    assert candidate["claim"]["content"] == CLAIM
    assert candidate["claim"]["claim_slot_key"] == "mission.primary"
    assert candidate["tile"]["tile_key"] == "mission.M1"
    assert candidate["tile"]["evidence_quote"] == CLAIM
    assert candidate["tile"]["evidence_contract"]["reject"]
    assert packet["review_template"] == [
        {
            "case_id": candidate["case_id"],
            "subject_id": candidate["subject_id"],
            "candidate_fingerprint": manifest[
                "candidate_fingerprint"
            ],
            "review_packet_fingerprint": manifest[
                "review_packet_fingerprint"
            ],
            "decision": None,
            "rationale": None,
            "reviewer_id": None,
            "reviewed_at": None,
            "event_id": None,
        }
    ]

    public_ledger = build_evidence_claim_tile_ledger(
        [report],
        mode="shadow",
    )
    assert CLAIM not in str(public_ledger)


def test_packet_is_order_invariant_and_changes_with_new_evidence() -> None:
    first = _report("one", "2026-07-01T08:00:00Z")
    second = _report("two", "2026-07-02T08:00:00Z")

    baseline = build_evidence_claim_tile_review_packet(
        [first, second]
    )
    reordered = build_evidence_claim_tile_review_packet(
        [deepcopy(second), deepcopy(first)]
    )
    earlier_only = build_evidence_claim_tile_review_packet([first])

    assert reordered == baseline
    assert (
        baseline["manifest"]["review_packet_fingerprint"]
        != earlier_only["manifest"]["review_packet_fingerprint"]
    )
    assert baseline["candidates"][0]["report"]["report_id"] == "two"
    assert baseline["candidates"][0]["mapping"][
        "observation_count"
    ] == 2


def test_packet_validation_rejects_tampering_and_wrong_subject() -> None:
    packet = build_evidence_claim_tile_review_packet(
        [_report("one", "2026-07-01T08:00:00Z")]
    )
    altered = deepcopy(packet)
    altered["candidates"][0]["claim"]["content"] = "Altered claim"

    with pytest.raises(
        EvidenceClaimTileReviewPacketError,
        match="candidate fingerprint",
    ):
        validate_evidence_claim_tile_review_packet(altered)

    assert (
        packet_candidate_for_subject(packet, "0" * 64)
        is None
    )


def test_packet_rejects_a_mapping_series_from_another_rubric() -> None:
    report = _report("one", "2026-07-01T08:00:00Z")
    report["raw"]["sv9"]["result"]["rubric_version"] = "legacy-rubric"

    with pytest.raises(
        EvidenceClaimTileReviewPacketError,
        match="current tile rubric",
    ):
        build_evidence_claim_tile_review_packet([report])


def _report(report_id: str, created_at: str) -> dict:
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
    tile = {
        "id": "M1",
        "estado": "ok",
        "evidencia": CLAIM,
        "evidence_ref": "web.0",
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
                            "tile_profile": [tile],
                        }
                    },
                },
            },
        },
    }
