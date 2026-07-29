from __future__ import annotations

from src.services.evidence_claim_reconciliation import (
    EVIDENCE_CLAIM_RECONCILIATION_POLICY_VERSION,
    apply_evidence_claim_reconciliations,
    claim_relation_subject,
    current_claim_reconciliations,
)


SUBJECT = "a" * 64


def test_current_decision_overlays_relation_without_canonical_authority() -> None:
    projection = _projection()
    accepted = _event(
        "00000000-0000-0000-0000-000000000001",
        1,
        "accepted",
    )

    result = apply_evidence_claim_reconciliations(
        projection,
        [accepted],
    )
    relation = result["slots"][0]["relation_candidates"][0]

    assert projection["slots"][0]["relation_candidates"][0][
        "adjudication_state"
    ] == "proposed"
    assert relation["adjudication_state"] == "accepted"
    assert relation["adjudication"]["relation_type"] == (
        "replacement_candidate"
    )
    assert relation["runtime_effect"] is False
    assert relation["authority"] is False
    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"]["adjudicated_relation_count"] == 1
    assert result["summary"]["relation_adjudication_state_counts"] == {
        "accepted": 1
    }
    assert result["summary"]["pending_relation_review_count"] == 0
    assert result["slots"][0]["requires_human_review"] is False
    assert result["claim_reconciliation"] == {
        "schema_version": "evidence-claim-reconciliation-v1",
        "policy_version": EVIDENCE_CLAIM_RECONCILIATION_POLICY_VERSION,
        "runtime_effect": False,
        "authority": False,
        "automatic_canonical_selection": False,
        "event_count": 1,
        "current_decision_count": 1,
        "applied_current_decision_count": 1,
    }
    assert "canonical_claim_variant_id" not in result


def test_revocation_explicitly_reverses_current_relation_decision() -> None:
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

    result = apply_evidence_claim_reconciliations(
        _projection(),
        [revoked, accepted],
    )
    relation = result["slots"][0]["relation_candidates"][0]

    assert relation["adjudication_state"] == "revoked"
    assert relation["adjudication"]["supersedes_event_id"] == accepted["id"]
    assert current_claim_reconciliations([revoked, accepted]) == [revoked]
    assert result["summary"]["pending_relation_review_count"] == 1
    assert result["slots"][0]["requires_human_review"] is True


def test_unknown_relation_cannot_change_a_projected_subject() -> None:
    unknown = _event(
        "00000000-0000-0000-0000-000000000003",
        1,
        "rejected",
        subject_id="b" * 64,
    )

    result = apply_evidence_claim_reconciliations(
        _projection(),
        [unknown],
    )

    assert result["slots"][0]["relation_candidates"][0][
        "adjudication_state"
    ] == "proposed"
    assert result["summary"]["adjudicated_relation_count"] == 0
    assert claim_relation_subject(result, SUBJECT) is not None
    assert claim_relation_subject(result, "b" * 64) is None


def test_relation_type_mismatch_fails_closed() -> None:
    mismatched = {
        **_event(
            "00000000-0000-0000-0000-000000000004",
            1,
            "accepted",
        ),
        "relation_type": "coexistence_candidate",
    }

    result = apply_evidence_claim_reconciliations(
        _projection(),
        [mismatched],
    )
    relation = result["slots"][0]["relation_candidates"][0]

    assert relation["adjudication_state"] == "proposed"
    assert "adjudication" not in relation
    assert result["summary"]["adjudicated_relation_count"] == 0
    assert result["summary"]["relation_type_mismatch_count"] == 1
    assert result["claim_reconciliation"][
        "applied_current_decision_count"
    ] == 0
    assert result["authority"] is False


def _projection() -> dict:
    return {
        "runtime_effect": False,
        "authority": False,
        "summary": {},
        "slots": [
            {
                "claim_slot_id": "c" * 64,
                "claim_type_conflict": False,
                "requires_human_review": True,
                "relation_candidates": [
                    {
                        "relation_candidate_id": SUBJECT,
                        "relation": "replacement_candidate",
                        "from_claim_variant_id": "d" * 64,
                        "to_claim_variant_id": "e" * 64,
                        "adjudication_state": "proposed",
                        "runtime_effect": False,
                        "authority": False,
                    }
                ],
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
        "subject_type": "claim_relation",
        "subject_id": subject_id,
        "relation_type": "replacement_candidate",
        "sequence": sequence,
        "decision": decision,
        "effective_state": decision,
        "supersedes_event_id": supersedes_event_id,
        "schema_version": "evidence-claim-reconciliation-v1",
        "policy_version": EVIDENCE_CLAIM_RECONCILIATION_POLICY_VERSION,
        "evaluator_version": "manual-review-v1",
        "reviewer": "gsus",
        "actor_id": "gsus",
        "reason_code": "relation_reviewed",
        "rationale": "The relation was manually reviewed.",
        "runtime_effect": False,
        "authority": False,
        "created_at": f"2026-07-29T10:00:0{sequence}+00:00",
    }
