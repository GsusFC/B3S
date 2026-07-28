from __future__ import annotations

from src.services.evidence_memory_adjudication import (
    EVIDENCE_MEMORY_ADJUDICATION_POLICY_VERSION,
    apply_evidence_memory_adjudications,
    current_adjudications,
    evidence_subject_exists,
)


SUBJECT = "a" * 64


def test_current_adjudication_overlays_identity_without_runtime_authority() -> None:
    projection = _projection()
    accepted = _event(
        "00000000-0000-0000-0000-000000000001",
        1,
        "accepted",
    )

    result = apply_evidence_memory_adjudications(projection, [accepted])

    assert projection["entries"][0]["adjudication_state"] == "proposed"
    assert result["entries"][0]["adjudication_state"] == "accepted"
    assert result["entries"][0]["state"] == "repeated"
    assert result["entries"][0]["adjudication"]["runtime_effect"] is False
    assert result["entries"][0]["adjudication"]["authority"] is False
    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"]["adjudicated_entry_count"] == 1
    assert result["summary"]["adjudication_state_counts"] == {"accepted": 1}
    assert result["adjudication"] == {
        "schema_version": "evidence-memory-adjudication-v1",
        "policy_version": EVIDENCE_MEMORY_ADJUDICATION_POLICY_VERSION,
        "runtime_effect": False,
        "authority": False,
        "event_count": 1,
        "current_decision_count": 1,
    }


def test_revocation_is_explicit_and_reverses_the_previous_current_decision() -> None:
    accepted = _event(
        "00000000-0000-0000-0000-000000000001",
        1,
        "accepted",
    )
    revoked = _event(
        "00000000-0000-0000-0000-000000000002",
        2,
        "revoked",
        supersedes_event_id=accepted["id"],
    )

    result = apply_evidence_memory_adjudications(
        _projection(),
        [revoked, accepted],
    )

    assert result["entries"][0]["adjudication_state"] == "revoked"
    assert result["entries"][0]["adjudication"]["supersedes_event_id"] == accepted["id"]
    assert current_adjudications([revoked, accepted]) == [revoked]


def test_unknown_subject_cannot_mutate_an_existing_evidence_entry() -> None:
    unknown = _event(
        "00000000-0000-0000-0000-000000000003",
        1,
        "rejected",
        subject_id="b" * 64,
    )

    result = apply_evidence_memory_adjudications(_projection(), [unknown])

    assert result["entries"][0]["adjudication_state"] == "proposed"
    assert result["summary"]["adjudicated_entry_count"] == 0
    assert evidence_subject_exists(result, SUBJECT) is True
    assert evidence_subject_exists(result, "b" * 64) is False


def _projection() -> dict:
    return {
        "runtime_effect": False,
        "authority": False,
        "summary": {},
        "entries": [
            {
                "evidence_id": SUBJECT,
                "state": "repeated",
                "adjudication_state": "proposed",
            }
        ],
    }


def _event(
    event_id: str,
    sequence: int,
    decision: str,
    *,
    subject_id: str = SUBJECT,
    supersedes_event_id: str | None = None,
) -> dict:
    return {
        "id": event_id,
        "subject_type": "evidence",
        "subject_id": subject_id,
        "sequence": sequence,
        "decision": decision,
        "effective_state": decision,
        "supersedes_event_id": supersedes_event_id,
        "schema_version": "evidence-memory-adjudication-v1",
        "policy_version": EVIDENCE_MEMORY_ADJUDICATION_POLICY_VERSION,
        "evaluator_version": "manual-review-v1",
        "reviewer": "reviewer@example.com",
        "actor_id": "environment-token",
        "reason_code": "identity_confirmed",
        "rationale": "The source identifies the scanned brand unambiguously.",
        "runtime_effect": False,
        "authority": False,
        "created_at": f"2026-07-28T10:00:0{sequence}+00:00",
    }
