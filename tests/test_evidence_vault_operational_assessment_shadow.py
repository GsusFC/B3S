from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_tile_contract_registry,
    canonical_fingerprint,
)
from src.services.evidence_vault_operational_assessment_shadow import (
    EvidenceVaultOperationalAssessmentShadowError,
    build_operational_semantic_shadow_assessment,
    validate_operational_semantic_shadow_assessment,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)


def test_complete_candidate_vector_scores_pending_tiles_independently_of_authority() -> None:
    source, packet = _artifacts(ok_tile_ids={"M1", "M2"})

    shadow = _build(packet, source)

    assert shadow["assessment_status"] == "available"
    assert shadow["assessment_output"]["tile_count"] == 80
    assert shadow["assessment_output"]["sv9_score"] == 2
    assert shadow["authority_coverage"]["accepted_tile_count"] == 0
    assert _requirement(shadow, "M1")["verification_state"] == "pending"
    assert _requirement(shadow, "C8")["verification_requirement"] == "human_required"
    validate_operational_semantic_shadow_assessment(
        shadow,
        operational_packet=packet,
        source_candidate_packet=source,
    )


def test_authority_changes_only_verification_not_semantic_assessment_fingerprints() -> None:
    source, pending = _artifacts(ok_tile_ids={"M1"})
    _, accepted = _artifacts(
        source=source,
        ok_tile_ids={"M1"},
        accepted_ids={"M1"},
    )

    pending_shadow = _build(pending, source)
    accepted_shadow = _build(accepted, source)

    assert pending_shadow["assessment_output"] == accepted_shadow["assessment_output"]
    assert (
        pending_shadow["semantic_provenance_fingerprint"]
        == accepted_shadow["semantic_provenance_fingerprint"]
    )
    assert _requirement(pending_shadow, "M1")["verification_state"] == "pending"
    assert _requirement(accepted_shadow, "M1")["verification_state"] == "verified"



def test_pending_rejected_and_accepted_do_not_change_candidate_kernel_score() -> None:
    source, pending = _artifacts(ok_tile_ids={"M1"})
    _, rejected = _artifacts(
        source=source,
        ok_tile_ids={"M1"},
        rejected_ids={"M1"},
    )
    _, accepted = _artifacts(
        source=source,
        ok_tile_ids={"M1"},
        accepted_ids={"M1"},
    )

    shadows = [_build(packet, source) for packet in (pending, rejected, accepted)]

    assert [shadow["assessment_output"]["sv9_score"] for shadow in shadows] == [1, 1, 1]
    assert len({shadow["assessment_output"]["assessment_fingerprint"] for shadow in shadows}) == 1
    assert len({shadow["assessment_output"]["score_fingerprint"] for shadow in shadows}) == 1
    assert len({shadow["semantic_provenance_fingerprint"] for shadow in shadows}) == 1
    assert [_requirement(shadow, "M1")["verification_state"] for shadow in shadows] == [
        "pending",
        "unverifiable",
        "verified",
    ]

def test_c7_and_c8_are_ordinary_semantic_tiles_with_only_c8_human_requirement() -> None:
    source, packet = _artifacts(ok_tile_ids={"C7", "C8"})

    shadow = _build(packet, source)

    assert shadow["assessment_output"]["sv9_score"] == 4
    assert _requirement(shadow, "C7") == {
        "tile_id": "C7",
        "verification_requirement": "owned_web_plus_external_social",
        "verification_state": "pending",
    }
    assert _requirement(shadow, "C8") == {
        "tile_id": "C8",
        "verification_requirement": "human_required",
        "verification_state": "pending",
    }



def test_verification_mapping_table_keeps_c7_c8_pending_without_its_owned_proof() -> None:
    source, packet = _artifacts(
        ok_tile_ids={"M1", "C7", "C8"},
        accepted_ids={"M1", "C7", "C8"},
        rejected_ids={"M2"},
    )

    shadow = _build(packet, source)

    assert _requirement(shadow, "M1")["verification_state"] == "verified"
    assert _requirement(shadow, "M2")["verification_state"] == "unverifiable"
    assert _requirement(shadow, "M3")["verification_state"] == "pending"
    assert _requirement(shadow, "C7") == {
        "tile_id": "C7",
        "verification_requirement": "owned_web_plus_external_social",
        "verification_state": "pending",
    }
    assert _requirement(shadow, "C8") == {
        "tile_id": "C8",
        "verification_requirement": "human_required",
        "verification_state": "pending",
    }

def test_contradiction_is_disputed_but_does_not_make_up_a_kernel_score() -> None:
    source, packet = _artifacts(ok_tile_ids={"M1"}, contradiction_tile_id="C8")

    shadow = _build(packet, source)

    assert shadow["assessment_status"] == "assessment_unavailable"
    assert shadow["reason"] == "contradiction_requires_semantic_reassessment"
    assert shadow["assessment_output"] is None
    assert shadow["semantic_provenance_fingerprint"] is None
    assert _requirement(shadow, "C8")["verification_state"] == "disputed"


def test_stale_reopen_changes_only_verification_not_semantic_vector() -> None:
    source, active = _artifacts(ok_tile_ids={"M1"})
    stale = deepcopy(active)
    tile = _projection_tile(stale, "M1")
    tile["lifecycle_state"] = "superseded"
    tile["score_eligible"] = False
    stale["scoring_projection"]["coverage"]["pending_initial_tile_count"] = 79
    stale["scoring_projection"]["coverage"]["pending_change_tile_count"] = 1
    stale["scoring_projection"]["coverage"]["canonical_score_status"] = "pending_reassessment"
    _rehash_operational_packet(stale)

    active_shadow = _build(active, source)
    stale_shadow = _build(stale, source)

    assert active_shadow["assessment_output"] == stale_shadow["assessment_output"]
    assert (
        active_shadow["semantic_provenance_fingerprint"]
        == stale_shadow["semantic_provenance_fingerprint"]
    )
    assert _requirement(stale_shadow, "M1")["verification_state"] == "stale"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "state_mismatch"])
def test_projection_must_match_exact_complete_source_candidate(mutation: str) -> None:
    source, packet = _artifacts(ok_tile_ids={"M1"})
    tampered = deepcopy(packet)
    tiles = tampered["scoring_projection"]["tiles"]
    if mutation == "missing":
        tiles.pop()
    elif mutation == "duplicate":
        tiles[-1] = deepcopy(tiles[0])
    else:
        _projection_tile(tampered, "M1")["candidate_semantic_state"] = "no"
    _rehash_operational_packet(tampered)

    with pytest.raises(EvidenceVaultOperationalAssessmentShadowError):
        _build(tampered, source)


def test_parent_mismatch_is_an_unavailable_stale_candidate_not_a_score() -> None:
    source, packet = _artifacts(ok_tile_ids={"M1"})

    shadow = build_operational_semantic_shadow_assessment(
        operational_packet=packet,
        source_candidate_packet=source,
        expected_parent_canonical_memory_version=_digest("different-parent"),
    )

    assert shadow["assessment_status"] == "assessment_unavailable"
    assert shadow["reason"] == "stale_candidate_parent"
    assert shadow["assessment_output"] is None
    assert shadow["semantic_provenance_fingerprint"] is None


def test_validation_rejects_tampered_shadow_output() -> None:
    source, packet = _artifacts(ok_tile_ids={"M1"})
    shadow = _build(packet, source)
    shadow["authority_coverage"]["accepted_tile_count"] = 80

    with pytest.raises(EvidenceVaultOperationalAssessmentShadowError):
        validate_operational_semantic_shadow_assessment(
            shadow,
            operational_packet=packet,
            source_candidate_packet=source,
        )


def _build(packet: dict, source: dict) -> dict:
    return build_operational_semantic_shadow_assessment(
        operational_packet=packet,
        source_candidate_packet=source,
    )


def _artifacts(
    *,
    ok_tile_ids: set[str],
    contradiction_tile_id: str | None = None,
    accepted_ids: set[str] | None = None,
    rejected_ids: set[str] | None = None,
    source: dict | None = None,
) -> tuple[dict, dict]:
    candidates = _candidates(ok_tile_ids, contradiction_tile_id=contradiction_tile_id)
    if source is None:
        source = build_candidate_packet(
            brand_identity="example.com",
            parent_canonical_memory_version=None,
            candidate_memory_version=_digest("candidate-memory"),
            accepted_memory_candidate_version=_digest("accepted-memory"),
            reviewed_memory_candidate_version=_digest("reviewed-memory"),
            review_packet_set_fingerprint=_digest("review-set"),
            aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
            candidate_tiles=candidates,
            unresolved_items=(
                [{
                    "unresolved_id": f"{contradiction_tile_id}-conflict",
                    "kind": "contradiction",
                    "blocking": True,
                    "details": {},
                }]
                if contradiction_tile_id is not None
                else []
            ),
        )
    else:
        candidates = source["candidate_tiles"]
    dispositions = {
        tile_id: {
            "authority_state": "accepted",
            "review_state": "none",
            "authority_profile_id": "human-reviewed-test-v1",
            "authority_source": "human",
            "decision_event_id": f"review-{tile_id}",
        }
        for tile_id in accepted_ids or set()
    }
    dispositions.update(
        {
            tile_id: {
                "authority_state": "rejected",
                "review_state": "none",
                "authority_profile_id": "human-reviewed-test-v1",
                "authority_source": "human",
            }
            for tile_id in rejected_ids or set()
        }
    )
    packet = build_operational_memory_packet(
        brand_identity="example.com",
        source_candidate_packet_fingerprint=source["candidate_packet_fingerprint"],
        aggregation_policy_fingerprint=source["manifest"]["aggregation_policy_fingerprint"],
        candidate_tiles=candidates,
        dispositions=dispositions,
    )
    return source, packet


def _candidates(ok_tile_ids: set[str], *, contradiction_tile_id: str | None) -> list[dict]:
    rows = []
    for contract in build_tile_contract_registry()["tiles"]:
        tile_id = str(contract["tile_id"])
        if tile_id == contradiction_tile_id:
            basis = [_basis(tile_id, "supports"), _basis(f"{tile_id}-counter", "contradicts")]
            rows.append(
                build_candidate_tile(
                    tile_id=tile_id,
                    basis=basis,
                    unresolved_refs=[f"{tile_id}-conflict"],
                )
            )
        else:
            rows.append(
                build_candidate_tile(
                    tile_id=tile_id,
                    basis=[_basis(tile_id, "supports")] if tile_id in ok_tile_ids else [],
                )
            )
    return rows


def _basis(seed: str, polarity: str) -> dict:
    return {
        "relation_id": _digest(f"{seed}-relation"),
        "evidence_id": _digest(f"{seed}-evidence"),
        "source_identity_id": _digest(f"{seed}-source"),
        "claim_id": None,
        "polarity": polarity,
        "review_status": "accepted",
        "decision_event_id": f"review-{seed}",
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _projection_tile(packet: dict, tile_id: str) -> dict:
    return next(row for row in packet["scoring_projection"]["tiles"] if row["tile_id"] == tile_id)


def _requirement(shadow: dict, tile_id: str) -> dict:
    return next(row for row in shadow["verification_requirements"]["tiles"] if row["tile_id"] == tile_id)


def _rehash_operational_packet(packet: dict) -> None:
    unsigned = {key: value for key, value in packet.items() if key != "candidate_packet_fingerprint"}
    packet["candidate_packet_fingerprint"] = canonical_fingerprint(
        packet["schema_version"], unsigned
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
