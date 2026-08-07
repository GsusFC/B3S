from __future__ import annotations

import hashlib

import pytest

from src.services.evidence_vault_canonical_core import (
    build_candidate_tile,
    build_tile_contract_registry,
)
from src.services.evidence_vault_operational_memory import (
    EvidenceVaultOperationalMemoryError,
    build_operational_memory_packet,
)


def test_partial_baseline_accounts_for_all_tiles_without_authorizing_them() -> None:
    candidates = _candidate_tiles({"M1", "M2"})
    accepted = {row["tile_id"] for row in candidates[:5]}

    packet = _packet(candidates, accepted=accepted)

    coverage = packet["scoring_projection"]["coverage"]
    assert coverage["accepted_tile_count"] == 5
    assert coverage["unresolved_tile_count"] == 75
    assert coverage["accepted_tile_count"] + coverage["unresolved_tile_count"] == 80
    assert coverage["pending_initial_tile_count"] == 75
    assert coverage["pending_change_tile_count"] == 0
    assert coverage["accepted_ok_count"] == 2
    assert coverage["accepted_sin_evidencia_count"] == 3
    assert coverage["tile_authority_coverage_ratio"] == 0.0625
    assert coverage["score_weight_authority_coverage_ratio"] == 0.05
    assert coverage["score_completeness"] == "partial"
    assert len(packet["scoring_projection"]["tiles"]) == 80
    assert len(packet["accepted_memory"]["accepted_tiles"]) == 5
    assert len(packet["candidate_overlay"]["candidate_tiles"]) == 75


def test_overlay_changes_do_not_change_canonical_memory() -> None:
    candidates = _candidate_tiles({"M1"})
    first = _packet(candidates, accepted={"M1"})
    second = _packet(
        candidates,
        accepted={"M1"},
        extra_dispositions={
            "M2": {
                "authority_state": "pending",
                "review_state": "required",
                "authority_profile_id": "human-semantic-v1",
            }
        },
    )

    assert second["accepted_memory_candidate_version"] == first[
        "accepted_memory_candidate_version"
    ]
    assert second["candidate_overlay_version"] != first["candidate_overlay_version"]
    assert second["candidate_packet_fingerprint"] != first["candidate_packet_fingerprint"]
    assert second["candidate_preview_identity"] != first["candidate_preview_identity"]


def test_contradiction_on_unresolved_tile_is_local_and_preview_is_null() -> None:
    candidates = _candidate_tiles(set())
    candidates = _replace_tile(
        candidates,
        build_candidate_tile(
            tile_id="C8",
            basis=[_basis("c8-support", "supports"), _basis("c8-counter", "contradicts")],
            unresolved_refs=["c8-conflict"],
        ),
    )

    packet = _packet(candidates, accepted={"M1"})
    coverage = packet["scoring_projection"]["coverage"]
    c8 = _projection_tile(packet, "C8")

    assert coverage["contradiction_on_unresolved_count"] == 1
    assert coverage["contradiction_on_accepted_count"] == 0
    assert coverage["contradiction_count"] == 1
    assert c8["candidate_semantic_state"] == "contradiction"
    assert c8["candidate_preview_points"] is None
    assert c8["canonical_semantic_state"] is None
    assert c8["effective_scoring_state"] == "sin_evidencia"


def test_new_contradiction_preserves_existing_accepted_state() -> None:
    baseline_candidates = _candidate_tiles({"C8"})
    baseline = _packet(baseline_candidates, accepted={"C8"})
    contradictory = _replace_tile(
        baseline_candidates,
        build_candidate_tile(
            tile_id="C8",
            basis=[_basis("c8-support", "supports"), _basis("c8-counter", "contradicts")],
            unresolved_refs=["c8-conflict"],
        ),
    )

    refresh = _packet(
        contradictory,
        current=baseline,
    )
    coverage = refresh["scoring_projection"]["coverage"]
    c8 = _projection_tile(refresh, "C8")

    assert refresh["proposed_canonical_memory_version"] == baseline[
        "proposed_canonical_memory_version"
    ]
    assert c8["canonical_semantic_state"] == "ok"
    assert c8["canonical_effective_points"] == 2
    assert c8["candidate_preview_points"] is None
    assert coverage["pending_change_tile_count"] == 1
    assert coverage["contradiction_on_accepted_count"] == 1
    assert coverage["canonical_score_status"] == "pending_reassessment"


def test_accepted_deterministic_change_is_neutral_about_direction() -> None:
    baseline_candidates = _candidate_tiles({"C8"})
    baseline = _packet(baseline_candidates, accepted={"C8"})
    downgraded = _replace_tile(
        baseline_candidates,
        build_candidate_tile(
            tile_id="C8",
            basis=[_basis("c8-counter", "contradicts")],
            previous_canonical_state="ok",
            delta_kind="candidate_update",
        ),
    )

    refresh = _packet(
        downgraded,
        accepted={"C8"},
        current=baseline,
    )
    c8 = _projection_tile(refresh, "C8")

    assert c8["canonical_semantic_state"] == "no"
    assert c8["canonical_effective_points"] == 0
    assert refresh["proposed_canonical_memory_version"] != baseline[
        "proposed_canonical_memory_version"
    ]


def test_contradiction_cannot_receive_authority() -> None:
    candidates = _candidate_tiles(set())
    candidates = _replace_tile(
        candidates,
        build_candidate_tile(
            tile_id="C8",
            basis=[_basis("c8-support", "supports"), _basis("c8-counter", "contradicts")],
            unresolved_refs=["c8-conflict"],
        ),
    )

    with pytest.raises(EvidenceVaultOperationalMemoryError, match="must remain pending"):
        _packet(candidates, accepted={"C8"})


def test_accepted_tile_must_match_the_canonical_reducer() -> None:
    candidates = _candidate_tiles(set())
    forged = dict(candidates[0])
    forged["candidate_state"] = "ok"
    candidates[0] = forged

    with pytest.raises(
        EvidenceVaultOperationalMemoryError,
        match="canonical reducer output",
    ):
        _packet(candidates, accepted={forged["tile_id"]})


def test_unrelated_source_packet_does_not_churn_accepted_memory() -> None:
    candidates = _candidate_tiles({"M1"})
    baseline = _packet(
        candidates,
        accepted={"M1"},
        source_seed="source-one",
    )
    refresh = _packet(
        candidates,
        accepted={"M1"},
        current=baseline,
        source_seed="source-two-with-unrelated-pending-change",
    )

    assert refresh["has_accepted_change"] is False
    assert refresh["proposed_canonical_memory_version"] == baseline[
        "proposed_canonical_memory_version"
    ]
    assert refresh["accepted_memory"]["accepted_tiles"] == baseline[
        "accepted_memory"
    ]["accepted_tiles"]



def test_coverage_loss_remains_in_candidate_overlay() -> None:
    baseline_candidates = _candidate_tiles({"M1"})
    baseline = _packet(baseline_candidates, accepted={"M1"})
    previous = next(row for row in baseline_candidates if row["tile_id"] == "M1")
    coverage_loss = build_candidate_tile(
        tile_id="M1",
        basis=previous["basis"],
        previous_canonical_state="ok",
        delta_kind="coverage_loss",
        coverage_refs=["capture:coverage-loss"],
    )
    refresh = _packet(
        _replace_tile(baseline_candidates, coverage_loss),
        current=baseline,
    )

    overlay = refresh["candidate_overlay"]["candidate_tiles"]
    m1 = next(row for row in overlay if row["tile_id"] == "M1")
    assert m1["delta_kind"] == "coverage_loss"
    assert refresh["scoring_projection"]["coverage"]["pending_change_tile_count"] == 0
    assert refresh["scoring_projection"]["coverage"]["canonical_score_status"] == "current"



def test_candidate_projection_must_have_exactly_eighty_unique_tiles() -> None:
    with pytest.raises(EvidenceVaultOperationalMemoryError, match="all 80"):
        _packet(_candidate_tiles(set())[:-1])


def _packet(
    candidates: list[dict],
    *,
    accepted: set[str] | None = None,
    current: dict | None = None,
    extra_dispositions: dict[str, dict] | None = None,
    source_seed: str = "candidate-packet",
) -> dict:
    dispositions = {
        tile_id: {
            "authority_state": "accepted",
            "review_state": "none",
            "authority_profile_id": "human-reviewed-test-v1",
            "authority_source": "human",
            "decision_event_id": f"human-review-{tile_id}",
        }
        for tile_id in accepted or set()
    }
    dispositions.update(extra_dispositions or {})
    return build_operational_memory_packet(
        brand_identity="Example.COM",
        source_candidate_packet_fingerprint=_digest(source_seed),
        aggregation_policy_fingerprint=_digest("aggregation-policy"),
        candidate_tiles=candidates,
        dispositions=dispositions,
        current_accepted_tiles=(
            current["accepted_memory"]["accepted_tiles"] if current else ()
        ),
        parent_canonical_memory_version=(
            current["proposed_canonical_memory_version"] if current else None
        ),
    )


def _candidate_tiles(ok_tile_ids: set[str]) -> list[dict]:
    return [
        build_candidate_tile(
            tile_id=str(row["tile_id"]),
            basis=(
                [_basis(str(row["tile_id"]), "supports")]
                if str(row["tile_id"]) in ok_tile_ids
                else []
            ),
        )
        for row in build_tile_contract_registry()["tiles"]
    ]


def _replace_tile(candidates: list[dict], replacement: dict) -> list[dict]:
    return [
        replacement if row["tile_id"] == replacement["tile_id"] else row
        for row in candidates
    ]


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
    return next(
        row
        for row in packet["scoring_projection"]["tiles"]
        if row["tile_id"] == tile_id
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
