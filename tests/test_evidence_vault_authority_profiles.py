from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from src.services.evidence_vault_authority_profiles import (
    EvidenceVaultAuthorityProfileError,
    REVIEWED_BASIS_PROFILE_ID,
    SCANNER_SEMANTIC_PROFILE_ID,
    build_initial_authority_profile_matrix,
    evaluate_reviewed_basis_authority,
    evaluate_scanner_semantic_authority,
    validate_authority_decision,
    validate_authority_profile_matrix,
)
from src.services.evidence_vault_canonical_core import build_candidate_tile
from src.services.evidence_vault_operational_memory import (
    EvidenceVaultOperationalMemoryError,
    build_operational_memory_packet,
)
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import build_tile_contract_registry


def test_initial_matrix_is_complete_but_does_not_claim_semantic_calibration() -> None:
    matrix = build_initial_authority_profile_matrix()
    c8 = next(row for row in matrix["tile_profiles"] if row["tile_key"] == "coherencia.C8")
    other = next(row for row in matrix["tile_profiles"] if row["tile_key"] == "mission.M1")

    validate_authority_profile_matrix(matrix)
    assert matrix["summary"] == {
        "tile_count": 80,
        "automatic_new_mapping_tile_count": 79,
        "shadow_insufficient_data_tile_count": 0,
        "human_required_tile_count": 1,
        "deterministic_reviewed_basis_tile_count": 80,
    }
    assert matrix["corpus"]["generalization_claim"] is False
    assert other["new_semantic_mapping_profile_id"] == SCANNER_SEMANTIC_PROFILE_ID
    assert c8["new_semantic_mapping_profile_id"] == SCANNER_SEMANTIC_PROFILE_ID
    assert c8["accepted_basis_reduction_profile_id"] == REVIEWED_BASIS_PROFILE_ID
    c7 = next(row for row in matrix["tile_profiles"] if row["tile_key"] == "coherencia.C7")
    assert c7["automatic_new_mapping_enabled"] is False
    assert c7["tile_guardrails"] == [
        "c7_requires_exact_reviewed_two_member_group"
    ]


def test_corpus_folds_are_split_by_brand_without_leakage() -> None:
    folds = build_initial_authority_profile_matrix()["corpus"]["folds"]

    assert len(folds) == 2
    for fold in folds:
        assert not (
            set(fold["design_brand_ids"]) & set(fold["validation_brand_ids"])
        )


@pytest.mark.parametrize(
    ("polarity", "expected_state"),
    [("supports", "ok"), ("contradicts", "no")],
)
def test_reviewed_basis_policy_is_neutral_for_upgrade_and_downgrade(
    polarity: str,
    expected_state: str,
) -> None:
    candidate = build_candidate_tile(
        tile_id="M1",
        basis=[_basis("m1", polarity)],
    )

    decision = evaluate_reviewed_basis_authority(candidate_tile=candidate)

    assert candidate["candidate_state"] == expected_state
    assert decision["eligible"] is True
    assert decision["decision"] == "accept"
    assert decision["direction_policy"] == "neutral"
    validate_authority_decision(decision, candidate_tile=candidate)


def test_unreviewed_relation_cannot_receive_automatic_authority() -> None:
    basis = _basis("m1", "supports")
    basis["review_status"] = "unreviewed"
    basis["decision_event_id"] = None
    candidate = build_candidate_tile(tile_id="M1", basis=[basis])

    decision = evaluate_reviewed_basis_authority(candidate_tile=candidate)

    assert decision["eligible"] is False
    assert decision["decision"] == "remain_pending"
    assert "unreviewed_effective_relation" in decision["reason_codes"]
    with pytest.raises(EvidenceVaultAuthorityProfileError, match="not eligible"):
        validate_authority_decision(decision, candidate_tile=candidate)


def test_scanner_semantic_policy_accepts_persisted_unreviewed_relation() -> None:
    scanner_basis = _basis("scanner", "supports", review_status="unreviewed")
    scanner_basis["decision_event_id"] = None
    candidate = build_candidate_tile(tile_id="M1", basis=[scanner_basis])

    decision = evaluate_scanner_semantic_authority(candidate_tile=candidate)

    assert decision["authority_profile_id"] == SCANNER_SEMANTIC_PROFILE_ID
    assert decision["eligible"] is True
    validate_authority_decision(decision, candidate_tile=candidate)


def test_scanner_semantic_policy_keeps_c7_pending_for_exact_group_review() -> None:
    scanner_basis = _basis("scanner-c7", "supports", review_status="unreviewed")
    scanner_basis["decision_event_id"] = None
    candidate = build_candidate_tile(tile_id="C7", basis=[scanner_basis])

    decision = evaluate_scanner_semantic_authority(candidate_tile=candidate)

    assert decision["eligible"] is False
    assert decision["decision"] == "remain_pending"
    assert "c7_requires_exact_reviewed_two_member_group" in decision["reason_codes"]
    with pytest.raises(EvidenceVaultAuthorityProfileError, match="not eligible"):
        validate_authority_decision(decision, candidate_tile=candidate)


def test_scanner_semantic_policy_keeps_contradiction_pending() -> None:
    scanner_support = _basis("scanner-support", "supports", review_status="unreviewed")
    scanner_counter = _basis("scanner-counter", "contradicts", review_status="unreviewed")
    scanner_support["decision_event_id"] = None
    scanner_counter["decision_event_id"] = None
    candidate = build_candidate_tile(
        tile_id="M1",
        basis=[scanner_support, scanner_counter],
        unresolved_refs=[_digest("contradiction")],
    )

    decision = evaluate_scanner_semantic_authority(candidate_tile=candidate)

    assert decision["eligible"] is False
    assert decision["decision"] == "remain_pending"
    assert "contradiction_requires_review" in decision["reason_codes"]


def test_c8_requires_human_for_new_mapping_but_accepts_a_reviewed_relation() -> None:
    matrix = build_initial_authority_profile_matrix()
    c8_profile = next(
        row for row in matrix["tile_profiles"] if row["tile_key"] == "coherencia.C8"
    )
    reviewed_candidate = build_candidate_tile(
        tile_id="C8",
        basis=[_basis("c8", "supports")],
    )

    decision = evaluate_reviewed_basis_authority(candidate_tile=reviewed_candidate)

    assert c8_profile["automatic_new_mapping_enabled"] is True
    assert c8_profile["new_semantic_mapping_profile_id"] == SCANNER_SEMANTIC_PROFILE_ID
    assert decision["eligible"] is True


def test_operational_memory_rejects_policy_authority_without_exact_decision() -> None:
    candidates = _candidates_with_only_m1_supported()
    dispositions = {
        "M1": {
            "authority_state": "accepted",
            "review_state": "none",
            "authority_profile_id": REVIEWED_BASIS_PROFILE_ID,
            "authority_source": "policy",
        }
    }

    with pytest.raises(EvidenceVaultOperationalMemoryError, match="authority decision"):
        build_operational_memory_packet(
            brand_identity="example.com",
            source_candidate_packet_fingerprint=_digest("source"),
            aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
            candidate_tiles=candidates,
            dispositions=dispositions,
        )


def test_operational_memory_accepts_exact_policy_decision_and_detects_tampering() -> None:
    candidates = _candidates_with_only_m1_supported()
    m1 = next(row for row in candidates if row["tile_id"] == "M1")
    decision = evaluate_reviewed_basis_authority(candidate_tile=m1)
    disposition = {
        "authority_state": "accepted",
        "review_state": "none",
        "authority_profile_id": REVIEWED_BASIS_PROFILE_ID,
        "authority_source": "policy",
        "policy_decision": decision,
    }

    packet = build_operational_memory_packet(
        brand_identity="example.com",
        source_candidate_packet_fingerprint=_digest("source"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions={"M1": disposition},
    )

    accepted = packet["accepted_memory"]["accepted_tiles"][0]
    assert accepted["authority_matrix_fingerprint"] == decision[
        "authority_matrix_fingerprint"
    ]
    assert accepted["authority_decision_fingerprint"] == decision[
        "authority_decision_fingerprint"
    ]

    tampered = deepcopy(decision)
    tampered["candidate_state"] = "no"
    disposition["policy_decision"] = tampered
    with pytest.raises(EvidenceVaultOperationalMemoryError, match="fingerprint mismatch"):
        build_operational_memory_packet(
            brand_identity="example.com",
            source_candidate_packet_fingerprint=_digest("source"),
            aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
            candidate_tiles=candidates,
            dispositions={"M1": disposition},
        )


def test_matrix_tampering_fails_closed() -> None:
    matrix = build_initial_authority_profile_matrix()
    tampered = deepcopy(matrix)
    tampered["tile_profiles"][0]["automatic_new_mapping_enabled"] = False

    with pytest.raises(EvidenceVaultAuthorityProfileError, match="fingerprint mismatch"):
        validate_authority_profile_matrix(tampered)


def _candidates_with_only_m1_supported() -> list[dict]:
    return [
        build_candidate_tile(
            tile_id=str(row["tile_id"]),
            basis=(
                [_basis("m1", "supports")]
                if str(row["tile_id"]) == "M1"
                else []
            ),
        )
        for row in build_tile_contract_registry()["tiles"]
    ]


def _basis(
    seed: str,
    polarity: str,
    *,
    review_status: str = "accepted",
    decision_event_id: str | None = None,
) -> dict:
    return {
        "relation_id": _digest(f"{seed}-relation"),
        "evidence_id": _digest(f"{seed}-evidence"),
        "source_identity_id": _digest(f"{seed}-source"),
        "claim_id": None,
        "polarity": polarity,
        "review_status": review_status,
        "decision_event_id": (
            decision_event_id if decision_event_id is not None else f"review-{seed}"
        ),
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
