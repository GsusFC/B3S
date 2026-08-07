from __future__ import annotations

from copy import deepcopy
import hashlib
from uuid import uuid4

import pytest

from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_authority_profiles import (
    evaluate_reviewed_basis_authority,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_tile,
    build_tile_contract_registry,
)
from src.services.evidence_vault_operational_authority import (
    build_operational_adoption_event,
    project_adopted_operational_memory,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)
from src.services.evidence_vault_operational_scoring import (
    EvidenceVaultOperationalScoringError,
    build_operational_score_evaluation,
    validate_operational_score_evaluation,
)


def test_partial_adopted_memory_scores_only_accepted_ok_tiles() -> None:
    memory = _baseline({"M1"})

    evaluation = build_operational_score_evaluation(
        memory,
        created_at="2026-08-06T13:00:00+02:00",
    )

    assert evaluation["score"] == 1
    assert evaluation["authority_coverage"]["accepted_tile_count"] == 1
    assert evaluation["authority_coverage"]["unresolved_tile_count"] == 79
    assert evaluation["authority_coverage"]["score_completeness"] == "partial"
    assert evaluation["authority_scope"] == "b3s-vault"
    assert evaluation["production_runtime_effect"] is False


def test_new_authorized_provenance_can_reuse_identical_score_inputs() -> None:
    baseline = _baseline({"M1"})
    first = build_operational_score_evaluation(
        baseline,
        created_at="2026-08-06T13:00:00+02:00",
    )
    candidates = _candidates({"M1"})
    packet = _packet(
        candidates,
        accepted={"M2"},
        current=baseline,
    )
    event = _event(
        packet,
        sequence=2,
        previous_event_id=baseline["adoption_event_id"],
        expected_current=baseline["canonical_memory_version"],
    )
    strengthened = project_adopted_operational_memory(packet, event)

    second = build_operational_score_evaluation(
        strengthened,
        created_at="2026-08-06T13:01:00+02:00",
        reusable_evaluation=first,
    )

    assert second["canonical_memory_version"] != first["canonical_memory_version"]
    assert second["evaluation_identity"] != first["evaluation_identity"]
    assert second["score_input_fingerprint"] == first["score_input_fingerprint"]
    assert second["score"] == first["score"] == 1
    assert second["reused_from_evaluation_identity"] == first["evaluation_identity"]
    assert second["authority_coverage"]["accepted_tile_count"] == 2


def test_evaluation_tampering_fails_closed() -> None:
    evaluation = build_operational_score_evaluation(
        _baseline({"M1"}),
        created_at="2026-08-06T13:00:00+02:00",
    )
    tampered = deepcopy(evaluation)
    tampered["score"] = 2

    with pytest.raises(EvidenceVaultOperationalScoringError, match="component total"):
        validate_operational_score_evaluation(tampered)


def test_corrupted_reusable_calculation_is_rejected_even_if_total_matches() -> None:
    baseline = _baseline({"M1"})
    first = build_operational_score_evaluation(
        baseline,
        created_at="2026-08-06T13:00:00+02:00",
    )
    corrupted = deepcopy(first)
    corrupted["component_breakdown"][0]["ok_count"] = 2
    corrupted["component_breakdown"][0]["no_count"] -= 1
    corrupted["component_breakdown"][0]["raw_score"] = 2
    corrupted["component_breakdown"][0]["effective_score"] = 2
    corrupted["component_breakdown"][0]["points"] = 2
    corrupted["score"] = 2

    with pytest.raises(EvidenceVaultOperationalScoringError):
        validate_operational_score_evaluation(corrupted)


def test_scoring_ignores_a_tampered_stored_projection() -> None:
    memory = _baseline({"M1"})
    tampered = deepcopy(memory)
    for row in tampered["scoring_projection"]["tiles"]:
        row["effective_scoring_state"] = "ok"

    evaluation = build_operational_score_evaluation(
        tampered,
        created_at="2026-08-06T13:00:00+02:00",
    )
    assert evaluation["score"] == 1



def _baseline(accepted: set[str]) -> dict:
    packet = _packet(_candidates(accepted), accepted=accepted)
    event = _event(packet, sequence=1)
    return project_adopted_operational_memory(packet, event)


def _packet(
    candidates: list[dict],
    *,
    accepted: set[str],
    current: dict | None = None,
) -> dict:
    by_id = {row["tile_id"]: row for row in candidates}
    dispositions = {
        tile_id: {
            "authority_state": "accepted",
            "review_state": "none",
            "authority_profile_id": (
                "reviewed-basis-reducer-v1"
                if by_id[tile_id]["basis"]
                else "human-reviewed-test-v1"
            ),
            "authority_source": "policy" if by_id[tile_id]["basis"] else "human",
            "decision_event_id": (
                None if by_id[tile_id]["basis"] else f"human-review-{tile_id}"
            ),
            "policy_decision": (
                evaluate_reviewed_basis_authority(candidate_tile=by_id[tile_id])
                if by_id[tile_id]["basis"]
                else None
            ),
        }
        for tile_id in accepted
    }
    return build_operational_memory_packet(
        brand_identity="example.com",
        source_candidate_packet_fingerprint=_digest("source-packet"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions=dispositions,
        current_accepted_tiles=(
            current["content"]["accepted_tiles"] if current else ()
        ),
        parent_canonical_memory_version=(
            current["canonical_memory_version"] if current else None
        ),
    )


def _event(
    packet: dict,
    *,
    sequence: int,
    previous_event_id: str | None = None,
    expected_current: str | None = None,
) -> dict:
    return build_operational_adoption_event(
        packet,
        event_id=str(uuid4()),
        sequence=sequence,
        previous_event_id=previous_event_id,
        adopted_by="policy",
        actor_id="automatic-baseline-v1",
        policy_fingerprint=_digest("policy"),
        created_at="2026-08-06T12:00:00+02:00",
        idempotency_key_hash=_digest(f"idempotency-{sequence}"),
        expected_current_canonical_memory_version=expected_current,
    )


def _candidates(ok_tile_ids: set[str]) -> list[dict]:
    return [
        build_candidate_tile(
            tile_id=str(row["tile_id"]),
            basis=(
                [_basis(str(row["tile_id"]))]
                if str(row["tile_id"]) in ok_tile_ids
                else []
            ),
        )
        for row in build_tile_contract_registry()["tiles"]
    ]


def _basis(seed: str) -> dict:
    return {
        "relation_id": _digest(f"{seed}-relation"),
        "evidence_id": _digest(f"{seed}-evidence"),
        "source_identity_id": _digest(f"{seed}-source"),
        "claim_id": None,
        "polarity": "supports",
        "review_status": "accepted",
        "decision_event_id": f"review-{seed}",
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
