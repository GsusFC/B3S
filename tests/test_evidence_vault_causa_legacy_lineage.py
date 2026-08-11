from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from src.services.evidence_vault_authority_profiles import (
    evaluate_reviewed_basis_authority,
)
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
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
    EvidenceVaultOperationalMemoryError,
    build_operational_memory_packet,
)
from src.services.evidence_vault_operational_scoring import (
    build_operational_score_evaluation,
)


_PREVIEW = (
    Path(__file__).parents[1]
    / "fixtures/evidence_vault_field_validation_v1"
    / "causa_prima_downstream_reference/preview.json"
)


def test_causa_legacy_preview_cannot_be_a_v2_genesis_parent() -> None:
    preview = json.loads(_PREVIEW.read_text(encoding="utf-8"))
    legacy_memory_version = preview["memory_version"]
    assert preview["authority"] is False
    assert preview["runtime_effect"] is False

    with pytest.raises(
        EvidenceVaultOperationalMemoryError,
        match="parent canonical memory and accepted parent tiles",
    ):
        _packet(parent=legacy_memory_version)


def test_causa_v2_genesis_has_new_identity_and_recomputed_score() -> None:
    preview = json.loads(_PREVIEW.read_text(encoding="utf-8"))
    packet = _packet(parent=None)
    event = _event(packet, expected_current=None)
    memory = project_adopted_operational_memory(packet, event)
    evaluation = build_operational_score_evaluation(
        memory,
        created_at="2026-08-07T12:00:00+02:00",
    )

    assert memory["parent_canonical_memory_version"] is None
    assert memory["canonical_memory_version"] != preview["memory_version"]
    assert evaluation["canonical_memory_version"] == memory[
        "canonical_memory_version"
    ]
    assert len(evaluation["evaluation_identity"]) == 64
    assert evaluation["schema_version"].endswith("-v2")
    assert evaluation["score"] != preview["scoring"]["current_score"]
    assert evaluation["authority"] is True
    assert evaluation["production_runtime_effect"] is False
    assert evaluation["scanner_runtime_effect"] is False


def _packet(*, parent: str | None) -> dict:
    candidates = [
        build_candidate_tile(
            tile_id=str(row["tile_id"]),
            basis=[_basis("M1")] if row["tile_id"] == "M1" else [],
        )
        for row in build_tile_contract_registry()["tiles"]
    ]
    m1 = next(row for row in candidates if row["tile_id"] == "M1")
    dispositions = {
        "M1": {
            "authority_state": "accepted",
            "review_state": "none",
            "authority_profile_id": "reviewed-basis-reducer-v1",
            "authority_source": "human",
            "decision_event_id": "review-causa-m1",
            "policy_decision": evaluate_reviewed_basis_authority(
                candidate_tile=m1
            ),
        }
    }
    return build_operational_memory_packet(
        brand_identity="causaprima.ai",
        source_candidate_packet_fingerprint=_digest("reviewed-v2-seed"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions=dispositions,
        current_accepted_tiles=(),
        parent_canonical_memory_version=parent,
    )


def _event(packet: dict, *, expected_current: str | None) -> dict:
    return build_operational_adoption_event(
        packet,
        event_id=str(uuid4()),
        sequence=1,
        previous_event_id=None,
        adopted_by="human",
        actor_id="migration-reviewer",
        policy_fingerprint=_digest("policy"),
        created_at="2026-08-07T12:00:00+02:00",
        idempotency_key_hash=_digest("causa-v2-genesis"),
        expected_current_canonical_memory_version=expected_current,
    )


def _basis(seed: str) -> dict:
    return {
        "relation_id": _digest(f"{seed}-relation"),
        "evidence_id": _digest(f"{seed}-evidence"),
        "source_identity_id": _digest(f"{seed}-source"),
        "claim_id": None,
        "polarity": "supports",
        "review_status": "accepted",
        "decision_event_id": "review-causa-m1",
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
