from __future__ import annotations

from copy import deepcopy
import hashlib
from uuid import uuid4

import pytest

from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_authority import (
    CanonicalMemoryPromotionCommand,
    EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION,
    build_promotion_event,
    plan_canonical_memory_promotion,
    project_promoted_canonical_memory,
    promotion_request_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_tile_contract_registry,
    canonical_fingerprint,
)
from src.services.evidence_vault_canonical_scoring import (
    EvidenceVaultCanonicalScoringError,
    build_canonical_score_evaluation,
    validate_canonical_score_evaluation,
)


CREATED_AT = "2026-08-05T08:00:00+02:00"


def test_promoted_memory_scores_without_reading_scanner_results() -> None:
    memory = _promoted_memory(
        {
            "M1",
            "M2",
            "M3",
            "M4",
            "M5",
            *(f"MG{index}" for index in range(1, 11)),
            *(f"C{index}" for index in range(1, 11)),
        }
    )

    evaluation = build_canonical_score_evaluation(
        memory,
        created_at=CREATED_AT,
    )

    assert evaluation["score"] == 35
    assert evaluation["base_average"] == 1.25
    assert evaluation["magnetism_capped"] is True
    assert _component(evaluation, "mission")["points"] == 5
    assert _component(evaluation, "magnetism") == {
        "component_key": "magnetism",
        "tile_count": 10,
        "ok_count": 10,
        "no_count": 0,
        "sin_evidencia_count": 0,
        "raw_score": 10,
        "effective_score": 5,
        "multiplier": 2,
        "points": 10,
        "max_points": 20,
    }
    assert evaluation["authority_scope"] == "b3s-vault"
    assert evaluation["scanner_runtime_effect"] is False
    assert evaluation["production_runtime_effect"] is False


def test_all_lit_canonical_tiles_score_one_hundred_without_cap() -> None:
    tile_ids = {
        str(tile["tile_id"])
        for tile in build_tile_contract_registry()["tiles"]
    }
    memory = _promoted_memory(tile_ids)

    evaluation = build_canonical_score_evaluation(
        memory,
        created_at=CREATED_AT,
    )

    assert evaluation["score"] == 100
    assert evaluation["base_average"] == 10
    assert evaluation["magnetism_capped"] is False


def test_no_and_sin_evidencia_both_score_zero_but_keep_distinct_identity() -> None:
    blind_spot = _promoted_memory(set())
    demonstrated_failure = deepcopy(blind_spot)
    demonstrated_failure["promotion_event_id"] = str(uuid4())
    demonstrated_failure["content"]["tiles"][0]["state"] = "no"
    _refresh_memory_identity(demonstrated_failure)

    blind_evaluation = build_canonical_score_evaluation(
        blind_spot,
        created_at=CREATED_AT,
    )
    failure_evaluation = build_canonical_score_evaluation(
        demonstrated_failure,
        created_at="2026-08-05T08:00:30+02:00",
    )

    assert blind_evaluation["score"] == failure_evaluation["score"] == 0
    assert blind_evaluation["score_input_fingerprint"] != (
        failure_evaluation["score_input_fingerprint"]
    )
    assert _component(blind_evaluation, "mission")["sin_evidencia_count"] == 5
    assert _component(failure_evaluation, "mission")["no_count"] == 1
    assert _component(failure_evaluation, "mission")["sin_evidencia_count"] == 4


def test_tile_order_is_not_an_effective_scoring_input() -> None:
    original = _promoted_memory({"M1", "P1", "MG1", "C1"})
    reordered = deepcopy(original)
    reordered["content"]["tiles"].reverse()
    _refresh_memory_identity(reordered)

    first = build_canonical_score_evaluation(
        original,
        created_at=CREATED_AT,
    )
    second = build_canonical_score_evaluation(
        reordered,
        created_at="2026-08-05T08:01:00+02:00",
        reusable_evaluation=first,
    )

    assert second["score_input_fingerprint"] == first["score_input_fingerprint"]
    assert second["score"] == first["score"]
    assert second["component_breakdown"] == first["component_breakdown"]
    assert second["evaluation_identity"] != first["evaluation_identity"]
    assert second["reused_from_evaluation_identity"] == first["evaluation_identity"]


def test_strengthened_provenance_creates_new_evaluation_and_reuses_score() -> None:
    baseline = _promoted_memory({"M1", "P1", "C1"})
    strengthened = deepcopy(baseline)
    strengthened["promotion_event_id"] = str(uuid4())
    strengthened["promotion_sequence"] = 2
    strengthened["content"]["parent_canonical_memory_version"] = baseline[
        "canonical_memory_version"
    ]
    strengthened["content"]["tiles"][0]["basis"].append(
        _accepted_basis("M1-independent")
    )
    strengthened["content"]["tiles"][0]["source_delta_kind"] = "strengthened"
    strengthened["content"]["tiles"][0][
        "source_candidate_packet_fingerprint"
    ] = _digest("strengthened-packet")
    _refresh_memory_identity(strengthened)

    first = build_canonical_score_evaluation(
        baseline,
        created_at=CREATED_AT,
    )
    second = build_canonical_score_evaluation(
        strengthened,
        created_at="2026-08-05T08:02:00+02:00",
        reusable_evaluation=first,
    )

    assert second["canonical_memory_version"] != first["canonical_memory_version"]
    assert second["evaluation_identity"] != first["evaluation_identity"]
    assert second["score_input_fingerprint"] == first["score_input_fingerprint"]
    assert second["score"] == first["score"]
    assert second["reused_from_evaluation_identity"] == first["evaluation_identity"]


def test_pending_packet_and_contradiction_cannot_be_scored() -> None:
    packet = _candidate_packet({"M1"})
    with pytest.raises(
        EvidenceVaultCanonicalScoringError,
        match="projection schema",
    ):
        build_canonical_score_evaluation(packet, created_at=CREATED_AT)

    contradictory = _promoted_memory({"M1"})
    contradictory["content"]["tiles"][0]["state"] = "contradiction"
    _refresh_memory_identity(contradictory)
    with pytest.raises(
        EvidenceVaultCanonicalScoringError,
        match="contradiction cannot enter",
    ):
        build_canonical_score_evaluation(
            contradictory,
            created_at=CREATED_AT,
        )


def test_policy_or_evaluation_tampering_fails_closed() -> None:
    memory = _promoted_memory({"M1", "P1"})
    wrong_policy = deepcopy(memory)
    wrong_policy["content"]["aggregation_policy_fingerprint"] = "f" * 64
    _refresh_memory_identity(wrong_policy)
    with pytest.raises(
        EvidenceVaultCanonicalScoringError,
        match="aggregation policy mismatch",
    ):
        build_canonical_score_evaluation(
            wrong_policy,
            created_at=CREATED_AT,
        )

    evaluation = build_canonical_score_evaluation(
        memory,
        created_at=CREATED_AT,
    )
    tampered = deepcopy(evaluation)
    tampered["score"] += 1
    with pytest.raises(
        EvidenceVaultCanonicalScoringError,
        match="total is inconsistent",
    ):
        validate_canonical_score_evaluation(tampered)


def test_reuse_requires_identical_effective_inputs() -> None:
    first = build_canonical_score_evaluation(
        _promoted_memory({"M1"}),
        created_at=CREATED_AT,
    )
    changed = _promoted_memory({"M1", "M2"})

    with pytest.raises(
        EvidenceVaultCanonicalScoringError,
        match="different effective scoring inputs",
    ):
        build_canonical_score_evaluation(
            changed,
            created_at="2026-08-05T08:03:00+02:00",
            reusable_evaluation=first,
        )


def _promoted_memory(ok_tile_ids: set[str]) -> dict:
    packet = _candidate_packet(ok_tile_ids)
    references = _references(packet)
    plan = plan_canonical_memory_promotion(
        packet,
        resolved_references=references,
        current_memory=None,
    )
    reviewer_id = "gsus"
    reviewed_at = "2026-08-05T07:00:00+02:00"
    rationale = "Exact baseline reviewed for Vault-only canonical scoring."
    request_fingerprint = promotion_request_fingerprint(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=None,
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        rationale=rationale,
    )
    command = CanonicalMemoryPromotionCommand(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=None,
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        rationale=rationale,
        idempotency_key_hash=_digest("idempotency"),
        request_fingerprint=request_fingerprint,
    )
    event = build_promotion_event(
        plan,
        command,
        event_id=str(uuid4()),
        sequence=1,
        previous_event_id=None,
    )
    return project_promoted_canonical_memory(plan, event)


def _candidate_packet(ok_tile_ids: set[str]) -> dict:
    tiles = []
    for row in build_tile_contract_registry()["tiles"]:
        tile_id = str(row["tile_id"])
        tiles.append(
            build_candidate_tile(
                tile_id=tile_id,
                basis=(
                    [_accepted_basis(tile_id)]
                    if tile_id in ok_tile_ids
                    else []
                ),
            )
        )
    return build_candidate_packet(
        brand_identity="example.com",
        parent_canonical_memory_version=None,
        candidate_memory_version=_digest("candidate-memory"),
        accepted_memory_candidate_version=_digest("accepted-memory"),
        reviewed_memory_candidate_version=_digest("reviewed-memory"),
        review_packet_set_fingerprint=_digest("review-packets"),
        aggregation_policy_fingerprint=(
            canonical_aggregation_policy_fingerprint()
        ),
        candidate_tiles=tiles,
        coverage_summary={},
    )


def _accepted_basis(seed: str) -> dict:
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


def _references(packet: dict) -> dict[str, str]:
    return {
        field: packet["manifest"][field]
        for field in (
            "candidate_memory_version",
            "accepted_memory_candidate_version",
            "reviewed_memory_candidate_version",
            "review_packet_set_fingerprint",
            "rubric_version",
            "tile_contract_registry_fingerprint",
            "reducer_policy_fingerprint",
            "aggregation_policy_fingerprint",
        )
    }


def _refresh_memory_identity(memory: dict) -> None:
    memory["canonical_memory_version"] = canonical_fingerprint(
        EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION,
        memory["content"],
    )


def _component(evaluation: dict, component_key: str) -> dict:
    return next(
        row
        for row in evaluation["component_breakdown"]
        if row["component_key"] == component_key
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
