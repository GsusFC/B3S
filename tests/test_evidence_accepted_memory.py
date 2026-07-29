from __future__ import annotations

import json

import pytest

from src.evidence_identity import canonical_evidence_digest
from src.services.evidence_accepted_memory import (
    EvidenceAcceptedMemoryError,
    build_evidence_accepted_memory,
)


OLD_CLAIM = "Our mission is to simplify finance."
NEW_CLAIM = "Our mission is to automate treasury."


def test_accepted_evidence_survives_repeat_and_acquisition_dropout() -> None:
    first = _report("one", "2026-01-01T00:00:00Z", OLD_CLAIM)
    repeat = _report("two", "2026-01-02T00:00:00Z", OLD_CLAIM)
    dropout = _empty_report("three", "2026-01-03T00:00:00Z")
    accepted = _event(
        "accept-old",
        _evidence_id(OLD_CLAIM),
        sequence=1,
        decision="accepted",
    )

    baseline = _memory([first], [accepted])
    repeated = _memory([first, repeat], [accepted])
    missing = _memory([first, repeat, dropout], [accepted])

    assert (
        baseline["accepted_memory_candidate_version"]
        == repeated["accepted_memory_candidate_version"]
        == missing["accepted_memory_candidate_version"]
    )
    assert baseline["summary"]["active_accepted_evidence_count"] == 1
    assert missing["summary"][
        "accepted_not_present_in_latest_count"
    ] == 1
    assert missing["entries"][0]["present_in_latest"] is False
    assert missing["entries"][0]["observation_count"] == 2
    assert missing["runtime_effect"] is False
    assert missing["authority"] is False
    assert missing["automatic_scoring_effect"] is False


def test_unreviewed_change_does_not_replace_accepted_evidence() -> None:
    old = _report("old", "2026-01-01T00:00:00Z", OLD_CLAIM)
    changed = _report(
        "changed",
        "2026-01-02T00:00:00Z",
        NEW_CLAIM,
    )
    accepted = _event(
        "accept-old",
        _evidence_id(OLD_CLAIM),
        sequence=1,
        decision="accepted",
    )

    baseline = _memory([old], [accepted])
    result = _memory([old, changed], [accepted])

    assert (
        result["accepted_memory_candidate_version"]
        == baseline["accepted_memory_candidate_version"]
    )
    assert result["summary"]["observed_evidence_count"] == 2
    assert result["summary"]["active_accepted_evidence_count"] == 1
    assert result["summary"][
        "accepted_document_with_unaccepted_variant_count"
    ] == 1
    assert result["entries"][0]["evidence_id"] == (
        _evidence_id(OLD_CLAIM)
    )
    assert result["entries"][0]["present_in_latest"] is False
    assert OLD_CLAIM not in json.dumps(result)
    assert NEW_CLAIM not in json.dumps(result)


def test_accepting_new_variant_appends_without_removing_old() -> None:
    reports = [
        _report("old", "2026-01-01T00:00:00Z", OLD_CLAIM),
        _report("changed", "2026-01-02T00:00:00Z", NEW_CLAIM),
    ]
    old_only = _memory(
        reports,
        [
            _event(
                "accept-old",
                _evidence_id(OLD_CLAIM),
                sequence=1,
                decision="accepted",
            )
        ],
    )
    both = _memory(
        reports,
        [
            _event(
                "accept-old",
                _evidence_id(OLD_CLAIM),
                sequence=1,
                decision="accepted",
            ),
            _event(
                "accept-new",
                _evidence_id(NEW_CLAIM),
                sequence=1,
                decision="accepted",
            ),
        ],
    )

    assert both["accepted_memory_candidate_version"] != (
        old_only["accepted_memory_candidate_version"]
    )
    assert both["summary"]["active_accepted_evidence_count"] == 2
    assert both["summary"][
        "multi_accepted_variant_document_count"
    ] == 1
    assert {
        entry["evidence_id"] for entry in both["entries"]
    } == {
        _evidence_id(OLD_CLAIM),
        _evidence_id(NEW_CLAIM),
    }


def test_report_order_and_reacceptance_metadata_do_not_change_version() -> None:
    reports = [
        _report("old", "2026-01-01T00:00:00Z", OLD_CLAIM),
        _report("changed", "2026-01-02T00:00:00Z", NEW_CLAIM),
    ]
    first = _event(
        "accept-old",
        _evidence_id(OLD_CLAIM),
        sequence=1,
        decision="accepted",
    )
    replacement = {
        **_event(
            "accept-old-again",
            _evidence_id(OLD_CLAIM),
            sequence=2,
            decision="accepted",
        ),
        "reviewer": "second-reviewer",
        "supersedes_event_id": "accept-old",
    }

    baseline = _memory(reports, [first])
    reordered = _memory(list(reversed(reports)), [first])
    reaccepted = _memory(reports, [first, replacement])

    assert reordered == baseline
    assert reaccepted["accepted_memory_candidate_version"] == (
        baseline["accepted_memory_candidate_version"]
    )
    assert reaccepted["state_fingerprint"] != (
        baseline["state_fingerprint"]
    )
    assert reaccepted["entries"][0]["acceptance"]["event_id"] == (
        "accept-old-again"
    )


def test_explicit_revocation_changes_selection_but_keeps_history() -> None:
    report = _report("one", "2026-01-01T00:00:00Z", OLD_CLAIM)
    accepted = _event(
        "accept-old",
        _evidence_id(OLD_CLAIM),
        sequence=1,
        decision="accepted",
    )
    active = _memory([report], [accepted])
    revoked = _memory(
        [report],
        [
            accepted,
            _event(
                "revoke-old",
                _evidence_id(OLD_CLAIM),
                sequence=2,
                decision="revoked",
            ),
        ],
    )

    assert active["summary"]["active_accepted_evidence_count"] == 1
    assert revoked["summary"]["observed_evidence_count"] == 1
    assert revoked["summary"]["active_accepted_evidence_count"] == 0
    assert revoked["summary"]["adjudication_state_counts"] == {
        "revoked": 1
    }
    assert revoked["entries"] == []
    assert revoked["accepted_memory_candidate_version"] != (
        active["accepted_memory_candidate_version"]
    )
    assert "no_active_accepted_evidence" in revoked[
        "promotion_blockers"
    ]


def test_unknown_current_adjudication_fails_closed() -> None:
    with pytest.raises(
        EvidenceAcceptedMemoryError,
        match="unknown evidence",
    ):
        _memory(
            [_report("one", "2026-01-01T00:00:00Z", OLD_CLAIM)],
            [
                _event(
                    "unknown",
                    "f" * 64,
                    sequence=1,
                    decision="accepted",
                )
            ],
        )


def test_unsigned_or_authoritative_acceptance_fails_closed() -> None:
    event = _event(
        "unsigned",
        _evidence_id(OLD_CLAIM),
        sequence=1,
        decision="accepted",
    )
    event["reviewer"] = ""
    event["authority"] = True

    with pytest.raises(
        EvidenceAcceptedMemoryError,
        match="not attributable and non-authoritative",
    ):
        _memory(
            [_report("one", "2026-01-01T00:00:00Z", OLD_CLAIM)],
            [event],
        )


def test_disabled_mode_has_no_candidate_version() -> None:
    result = build_evidence_accepted_memory(
        [],
        mode="off",
    )

    assert result["mode"] == "disabled"
    assert result["accepted_memory_candidate_version"] is None
    assert result["entries"] == []
    assert len(result["state_fingerprint"]) == 64


def _memory(
    reports: list[dict],
    events: list[dict],
) -> dict:
    return build_evidence_accepted_memory(
        reports,
        evidence_adjudications=events,
    )


def _evidence_id(content: str) -> str:
    return canonical_evidence_digest(
        source_class="owned_copy",
        evidence_type="raw_input",
        url="https://example.com/about",
        content=content,
    )


def _event(
    event_id: str,
    subject_id: str,
    *,
    sequence: int,
    decision: str,
) -> dict:
    return {
        "id": event_id,
        "subject_type": "evidence",
        "subject_id": subject_id,
        "sequence": sequence,
        "decision": decision,
        "reviewer": "gsus",
        "reason_code": "manual_identity_review",
        "rationale": "The evidence identity was reviewed.",
        "evaluator_version": "manual-review-v1",
        "created_at": f"2026-01-0{sequence + 1}T00:00:00Z",
        "runtime_effect": False,
        "authority": False,
    }


def _report(
    report_id: str,
    created_at: str,
    content: str,
) -> dict:
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
                        "evidence": [
                            {
                                "ref": "web.0",
                                "source": "web",
                                "evidence_type": "raw_input",
                                "url": "https://example.com/about",
                                "content": content,
                                "metadata": {
                                    "source_class": "owned_copy"
                                },
                            }
                        ],
                    }
                }
            },
        },
    }


def _empty_report(report_id: str, created_at: str) -> dict:
    report = _report(report_id, created_at, "placeholder")
    report["raw"]["flow"]["candidate"]["evidence_pack"][
        "evidence"
    ] = []
    return report
