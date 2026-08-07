from __future__ import annotations

from copy import deepcopy
import hashlib
from uuid import uuid4

import pytest

from src.services.evidence_vault_canonical_core import (
    build_candidate_tile,
    build_tile_contract_registry,
)
from src.services.evidence_vault_authority_profiles import (
    evaluate_reviewed_basis_authority,
)
from src.services.evidence_vault_operational_authority import (
    EvidenceVaultOperationalAdoptionBlockedError,
    EvidenceVaultOperationalAdoptionConflictError,
    EvidenceVaultOperationalAuthorityError,
    build_operational_adoption_event,
    project_adopted_operational_memory,
    project_memory_lifecycle,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)


def test_policy_can_activate_partial_baseline_without_human_signature() -> None:
    packet = _packet(_candidates({"M1"}), accepted={"M1"})
    event = _event(packet, sequence=1)

    memory = project_adopted_operational_memory(packet, event)

    assert event["adopted_by"] == "policy"
    assert event["actor_id"] == "automatic-baseline-v1"
    assert event["authority_scope"] == "b3s-vault"
    assert event["production_runtime_effect"] is False
    assert memory["canonical_memory_version"] == packet[
        "proposed_canonical_memory_version"
    ]
    assert memory["scoring_projection"]["coverage"]["accepted_tile_count"] == 1
    assert memory["scoring_projection"]["coverage"]["score_completeness"] == "partial"


def test_overlay_only_packet_cannot_create_another_canonical_version() -> None:
    baseline_packet = _packet(_candidates({"M1"}), accepted={"M1"})
    baseline_event = _event(baseline_packet, sequence=1)
    baseline = project_adopted_operational_memory(baseline_packet, baseline_event)
    refresh = _packet(
        _candidates({"M1"}),
        current=baseline,
    )

    assert refresh["has_accepted_change"] is False
    assert refresh["proposed_canonical_memory_version"] == baseline[
        "canonical_memory_version"
    ]
    with pytest.raises(EvidenceVaultOperationalAdoptionBlockedError, match="no accepted change"):
        _event(
            refresh,
            sequence=2,
            previous_event_id=baseline_event["event_id"],
            expected_current=baseline["canonical_memory_version"],
        )


def test_incremental_adoption_uses_parent_compare_and_swap() -> None:
    baseline_packet = _packet(_candidates({"M1"}), accepted={"M1"})
    baseline_event = _event(baseline_packet, sequence=1)
    baseline = project_adopted_operational_memory(baseline_packet, baseline_event)
    changed = _packet(
        _candidates({"M1", "M2"}),
        accepted={"M2"},
        current=baseline,
    )

    with pytest.raises(EvidenceVaultOperationalAdoptionConflictError, match="not the current"):
        _event(
            changed,
            sequence=2,
            previous_event_id=baseline_event["event_id"],
            expected_current=_digest("stale"),
        )

    event = _event(
        changed,
        sequence=2,
        previous_event_id=baseline_event["event_id"],
        expected_current=baseline["canonical_memory_version"],
    )
    updated = project_adopted_operational_memory(changed, event)
    lifecycle = project_memory_lifecycle([updated, baseline])

    assert updated["parent_canonical_memory_version"] == baseline[
        "canonical_memory_version"
    ]
    assert lifecycle[0]["lifecycle_state"] == "superseded"
    assert lifecycle[1]["lifecycle_state"] == "active"
    assert baseline["lifecycle_state"] == "active"


def test_tampered_overlay_invalidates_the_whole_packet() -> None:
    packet = _packet(_candidates({"M1"}), accepted={"M1"})
    tampered = deepcopy(packet)
    tampered["candidate_overlay"]["candidate_tiles"][0]["review_state"] = "required"

    with pytest.raises(EvidenceVaultOperationalAuthorityError, match="packet fingerprint"):
        _event(tampered, sequence=1)


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


def _packet(
    candidates: list[dict],
    *,
    accepted: set[str] | None = None,
    current: dict | None = None,
) -> dict:
    by_id = {row["tile_id"]: row for row in candidates}
    dispositions = {
        tile_id: {
            "authority_state": "accepted",
            "review_state": "none",
            "authority_profile_id": "reviewed-basis-reducer-v1",
            "authority_source": "policy",
            "policy_decision": evaluate_reviewed_basis_authority(
                candidate_tile=by_id[tile_id]
            ),
        }
        for tile_id in accepted or set()
    }
    return build_operational_memory_packet(
        brand_identity="example.com",
        source_candidate_packet_fingerprint=_digest("source-packet"),
        aggregation_policy_fingerprint=_digest("aggregation"),
        candidate_tiles=candidates,
        dispositions=dispositions,
        current_accepted_tiles=(
            current["content"]["accepted_tiles"] if current else ()
        ),
        parent_canonical_memory_version=(
            current["canonical_memory_version"] if current else None
        ),
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
