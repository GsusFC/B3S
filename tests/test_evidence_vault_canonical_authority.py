from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from src.services.evidence_vault_canonical_authority import (
    CanonicalMemoryPromotionCommand,
    EvidenceVaultCanonicalAuthorityError,
    EvidenceVaultCanonicalPromotionBlockedError,
    EvidenceVaultCanonicalPromotionConflictError,
    EvidenceVaultCanonicalReferenceError,
    build_promotion_event,
    build_reference_resolution,
    plan_canonical_memory_promotion,
    project_promoted_canonical_memory,
    promotion_policy_fingerprint,
    promotion_request_fingerprint,
    validate_promotion_event,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_tile_contract_registry,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def test_baseline_promotion_is_deterministic_and_authority_comes_from_event() -> None:
    packet = _baseline_packet()
    plan = plan_canonical_memory_promotion(
        packet,
        resolved_references=_references(packet),
        current_memory=None,
    )
    repeated = plan_canonical_memory_promotion(
        deepcopy(packet),
        resolved_references=dict(reversed(list(_references(packet).items()))),
        current_memory=None,
    )

    assert repeated == plan
    assert plan["content"]["tile_count"] == 80
    assert plan["content"]["tiles"][0]["state"] == "ok"
    assert "authority" not in plan["content"]
    assert len(plan["promoted_canonical_memory_version"]) == 64

    command = _command(packet)
    event = _event(plan, command)
    memory = project_promoted_canonical_memory(plan, event)

    assert event["authority"] is True
    assert event["authority_scope"] == "b3s-vault"
    assert event["production_runtime_effect"] is False
    assert event["scanner_runtime_effect"] is False
    assert memory["canonical_memory_version"] == plan["promoted_canonical_memory_version"]
    assert memory["authority"] is True


def test_reference_resolution_is_complete_exact_and_content_addressed() -> None:
    packet = _baseline_packet()
    resolution = build_reference_resolution(packet, _references(packet))

    assert resolution["candidate_packet_fingerprint"] == packet["candidate_packet_fingerprint"]
    assert len(resolution["reference_resolution_fingerprint"]) == 64

    missing = _references(packet)
    missing.pop("candidate_memory_version")
    with pytest.raises(EvidenceVaultCanonicalReferenceError, match="coverage mismatch"):
        build_reference_resolution(packet, missing)

    mismatched = _references(packet)
    mismatched["review_packet_set_fingerprint"] = HASH_F
    with pytest.raises(EvidenceVaultCanonicalReferenceError, match="review_packet_set_fingerprint"):
        build_reference_resolution(packet, mismatched)


def test_blocking_unresolved_item_prevents_initial_promotion() -> None:
    tiles = _baseline_tiles()
    tiles[1] = build_candidate_tile(
        tile_id="M2",
        unresolved_refs=["coverage-m2"],
    )
    packet = _packet(
        tiles,
        unresolved_items=[
            {
                "unresolved_id": "coverage-m2",
                "kind": "coverage",
                "blocking": True,
                "details": {"tile_key": "mission.M2"},
            }
        ],
    )

    with pytest.raises(EvidenceVaultCanonicalPromotionBlockedError, match="blocking unresolved"):
        plan_canonical_memory_promotion(
            packet,
            resolved_references=_references(packet),
            current_memory=None,
        )


def test_contradiction_blocks_n_plus_one_and_preserves_current_memory() -> None:
    baseline_packet = _baseline_packet()
    current = _promoted_memory(baseline_packet)
    current_before = deepcopy(current)
    tiles = build_incremental_candidate_tiles(
        previous_candidate_tiles=baseline_packet["candidate_tiles"],
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "contradiction",
                "basis": [_accepted_basis(1, "supports"), _basis(2, "contradicts")],
                "unresolved_refs": ["contradiction-m1"],
            }
        ],
    )
    packet = _packet(
        tiles,
        parent=current["canonical_memory_version"],
        candidate_memory_version=HASH_F,
        unresolved_items=[
            {
                "unresolved_id": "contradiction-m1",
                "kind": "contradiction",
                "blocking": True,
                "details": {"tile_key": "mission.M1"},
            }
        ],
    )

    with pytest.raises(EvidenceVaultCanonicalPromotionBlockedError, match="contradiction"):
        plan_canonical_memory_promotion(
            packet,
            resolved_references=_references(packet),
            current_memory=current,
        )

    assert current == current_before
    assert current["content"]["tiles"][0]["state"] == "ok"


def test_no_change_and_coverage_loss_remain_shadow_only() -> None:
    baseline_packet = _baseline_packet()
    current = _promoted_memory(baseline_packet)

    no_change = build_incremental_candidate_tiles(
        previous_candidate_tiles=baseline_packet["candidate_tiles"],
    )
    no_change_packet = _packet(
        no_change,
        parent=current["canonical_memory_version"],
        candidate_memory_version=HASH_F,
    )
    with pytest.raises(EvidenceVaultCanonicalPromotionBlockedError, match="shadow-only"):
        plan_canonical_memory_promotion(
            no_change_packet,
            resolved_references=_references(no_change_packet),
            current_memory=current,
        )

    coverage_loss = build_incremental_candidate_tiles(
        previous_candidate_tiles=baseline_packet["candidate_tiles"],
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "coverage_loss",
                "coverage_refs": ["scan-2-not-reacquired"],
            }
        ],
    )
    coverage_packet = _packet(
        coverage_loss,
        parent=current["canonical_memory_version"],
        candidate_memory_version=HASH_F,
    )
    with pytest.raises(EvidenceVaultCanonicalPromotionBlockedError, match="shadow-only"):
        plan_canonical_memory_promotion(
            coverage_packet,
            resolved_references=_references(coverage_packet),
            current_memory=current,
        )


def test_strengthened_updates_only_provenance_and_ignores_parallel_coverage_loss() -> None:
    baseline_packet = _baseline_packet()
    current = _promoted_memory(baseline_packet)
    tiles = build_incremental_candidate_tiles(
        previous_candidate_tiles=baseline_packet["candidate_tiles"],
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "strengthened",
                "basis": [_accepted_basis(1, "supports"), _accepted_basis(2, "supports")],
            },
            {
                "tile_id": "M2",
                "delta_kind": "coverage_loss",
                "coverage_refs": ["scan-2-not-reacquired"],
            },
        ],
    )
    packet = _packet(
        tiles,
        parent=current["canonical_memory_version"],
        candidate_memory_version=HASH_F,
    )

    plan = plan_canonical_memory_promotion(
        packet,
        resolved_references=_references(packet),
        current_memory=current,
    )

    assert plan["promoted_canonical_memory_version"] != current["canonical_memory_version"]
    assert plan["content"]["tiles"][0]["state"] == "ok"
    assert len(plan["content"]["tiles"][0]["basis"]) == 2
    assert plan["content"]["tiles"][1] == current["content"]["tiles"][1]


def test_incremental_packet_cannot_switch_semantic_policies() -> None:
    baseline_packet = _baseline_packet()
    current = _promoted_memory(baseline_packet)
    tiles = build_incremental_candidate_tiles(
        previous_candidate_tiles=baseline_packet["candidate_tiles"],
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "strengthened",
                "basis": [_accepted_basis(1, "supports"), _accepted_basis(2, "supports")],
            }
        ],
    )
    packet = _packet(
        tiles,
        parent=current["canonical_memory_version"],
        candidate_memory_version=HASH_F,
        aggregation_policy_fingerprint="1" * 64,
    )

    with pytest.raises(EvidenceVaultCanonicalPromotionBlockedError, match="aggregation_policy_fingerprint"):
        plan_canonical_memory_promotion(
            packet,
            resolved_references=_references(packet),
            current_memory=current,
        )


def test_foreign_or_stale_parent_is_rejected() -> None:
    baseline_packet = _baseline_packet()
    current = _promoted_memory(baseline_packet)
    tiles = build_incremental_candidate_tiles(
        previous_candidate_tiles=baseline_packet["candidate_tiles"],
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "strengthened",
                "basis": [_accepted_basis(1, "supports"), _accepted_basis(2, "supports")],
            }
        ],
    )
    packet = _packet(tiles, parent=HASH_F, candidate_memory_version=HASH_F)

    with pytest.raises(EvidenceVaultCanonicalPromotionConflictError, match="not the current"):
        plan_canonical_memory_promotion(
            packet,
            resolved_references=_references(packet),
            current_memory=current,
        )


def test_promotion_event_requires_attributable_exact_request() -> None:
    packet = _baseline_packet()
    plan = plan_canonical_memory_promotion(
        packet,
        resolved_references=_references(packet),
        current_memory=None,
    )
    command = _command(packet)

    event = _event(plan, command)
    assert event["reviewer_id"] == "gsus"
    assert event["reviewed_at"] == "2026-08-04T18:00:00+00:00"
    assert event["rationale"] == "Baseline reviewed as an exact atomic package."
    assert event["promotion_policy_fingerprint"] == promotion_policy_fingerprint()

    wrong = replace(command, request_fingerprint=HASH_F)
    with pytest.raises(EvidenceVaultCanonicalAuthorityError, match="request fingerprint mismatch"):
        _event(plan, wrong)

    with pytest.raises(EvidenceVaultCanonicalAuthorityError, match="include a timezone"):
        promotion_request_fingerprint(
            candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
            parent_canonical_memory_version=None,
            reviewer_id="gsus",
            reviewed_at="2026-08-04T20:00:00",
            rationale="Reviewed.",
        )


def test_tampered_event_cannot_project_authority() -> None:
    packet = _baseline_packet()
    plan = plan_canonical_memory_promotion(
        packet,
        resolved_references=_references(packet),
        current_memory=None,
    )
    event = _event(plan, _command(packet))
    tampered = {**event, "promoted_canonical_memory_version": HASH_F}

    with pytest.raises(EvidenceVaultCanonicalPromotionConflictError, match="does not match"):
        project_promoted_canonical_memory(plan, tampered)

    altered_rationale = {**event, "rationale": "Different human decision."}
    with pytest.raises(EvidenceVaultCanonicalAuthorityError, match="request fingerprint mismatch"):
        validate_promotion_event(altered_rationale)

    broadened_scope = {**event, "authority_scope": "production"}
    with pytest.raises(EvidenceVaultCanonicalAuthorityError, match="invalid authority scope"):
        validate_promotion_event(broadened_scope)


def _baseline_packet() -> dict:
    return _packet(_baseline_tiles())


def _baseline_tiles() -> list[dict]:
    tiles = [build_candidate_tile(tile_id=row["tile_id"]) for row in build_tile_contract_registry()["tiles"]]
    tiles[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_accepted_basis(1, "supports")],
    )
    return tiles


def _packet(
    tiles: list[dict],
    *,
    parent: str | None = None,
    candidate_memory_version: str = HASH_A,
    aggregation_policy_fingerprint: str = HASH_E,
    unresolved_items: list[dict] | None = None,
) -> dict:
    return build_candidate_packet(
        brand_identity="Example.COM",
        parent_canonical_memory_version=parent,
        candidate_memory_version=candidate_memory_version,
        accepted_memory_candidate_version=HASH_B,
        reviewed_memory_candidate_version=HASH_C,
        review_packet_set_fingerprint=HASH_D,
        aggregation_policy_fingerprint=aggregation_policy_fingerprint,
        candidate_tiles=tiles,
        coverage_summary={"assessment_count": 1},
        unresolved_items=unresolved_items or [],
    )


def _references(packet: dict) -> dict[str, str]:
    manifest = packet["manifest"]
    return {
        field: manifest[field]
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


def _promoted_memory(packet: dict) -> dict:
    plan = plan_canonical_memory_promotion(
        packet,
        resolved_references=_references(packet),
        current_memory=None,
    )
    return project_promoted_canonical_memory(plan, _event(plan, _command(packet)))


def _command(packet: dict) -> CanonicalMemoryPromotionCommand:
    reviewer_id = "gsus"
    reviewed_at = "2026-08-04T20:00:00+02:00"
    rationale = "Baseline reviewed as an exact atomic package."
    return CanonicalMemoryPromotionCommand(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=packet["manifest"]["parent_canonical_memory_version"],
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        rationale=rationale,
        idempotency_key_hash="1" * 64,
        request_fingerprint=promotion_request_fingerprint(
            candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
            parent_canonical_memory_version=packet["manifest"]["parent_canonical_memory_version"],
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            rationale=rationale,
        ),
    )


def _event(plan: dict, command: CanonicalMemoryPromotionCommand) -> dict:
    return build_promotion_event(
        plan,
        command,
        event_id=str(uuid4()),
        sequence=1,
        previous_event_id=None,
    )


def _accepted_basis(seed: int, polarity: str) -> dict:
    return _basis(
        seed,
        polarity,
        review_status="accepted",
        decision_event_id=f"review-{seed}",
    )


def _basis(
    seed: int,
    polarity: str,
    *,
    review_status: str = "unreviewed",
    decision_event_id: str | None = None,
) -> dict:
    character = format(seed, "x")[-1]
    return {
        "relation_id": character * 64,
        "evidence_id": format(seed + 8, "x")[-1] * 64,
        "source_identity_id": format(seed + 4, "x")[-1] * 64,
        "claim_id": HASH_C,
        "polarity": polarity,
        "review_status": review_status,
        "decision_event_id": decision_event_id,
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }
