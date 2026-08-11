from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from src.history.repository import _validate_operational_packet_lineage_for_storage
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_tile_contract_registry,
)
from src.services.evidence_vault_operational_authority import (
    EvidenceVaultOperationalAuthorityError,
)
from src.services.evidence_vault_operational_candidate import (
    build_operational_packet_from_reviewed_candidate,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)


def test_storage_recomputes_policy_authority_from_registered_source() -> None:
    source = _source_packet()
    operational = build_operational_packet_from_reviewed_candidate(source)

    _validate_operational_packet_lineage_for_storage(
        operational,
        current_memory=None,
        source_candidate_packet=source,
    )

    forged = deepcopy(operational)
    forged["accepted_memory"]["accepted_tiles"][0][
        "authority_matrix_fingerprint"
    ] = _digest("forged-matrix")
    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="current deterministic authority matrix",
    ):
        _validate_operational_packet_lineage_for_storage(
            forged,
            current_memory=None,
            source_candidate_packet=source,
        )


def test_storage_rejects_generic_or_partial_c7_authority() -> None:
    source = _source_packet("C7")
    operational = build_operational_memory_packet(
        brand_identity="example.com",
        source_candidate_packet_fingerprint=source[
            "candidate_packet_fingerprint"
        ],
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=source["candidate_tiles"],
        dispositions={
            "C7": {
                "authority_state": "accepted",
                "review_state": "none",
                "authority_profile_id": "human-reviewed-v1",
                "authority_source": "human",
                "decision_event_id": "durable-review-m1",
            }
        },
    )

    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="complete exact two-member group",
    ):
        _validate_operational_packet_lineage_for_storage(
            operational,
            current_memory=None,
            source_candidate_packet=source,
        )


def test_storage_rejects_fake_human_decision_event() -> None:
    source = _source_packet()
    candidates = source["candidate_tiles"]
    human = build_operational_memory_packet(
        brand_identity="example.com",
        source_candidate_packet_fingerprint=source[
            "candidate_packet_fingerprint"
        ],
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions={
            "M1": {
                "authority_state": "accepted",
                "review_state": "none",
                "authority_profile_id": "human-reviewed-v1",
                "authority_source": "human",
                "decision_event_id": "invented-human-event",
            }
        },
    )

    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="durable reviewed relation",
    ):
        _validate_operational_packet_lineage_for_storage(
            human,
            current_memory=None,
            source_candidate_packet=source,
        )


def _source_packet(tile_id: str = "M1") -> dict:
    candidates = [
        build_candidate_tile(
            tile_id=str(tile["tile_id"]),
            basis=[_basis()] if tile["tile_id"] == tile_id else [],
        )
        for tile in build_tile_contract_registry()["tiles"]
    ]
    return build_candidate_packet(
        brand_identity="example.com",
        parent_canonical_memory_version=None,
        candidate_memory_version=_digest("candidate-memory"),
        accepted_memory_candidate_version=_digest("accepted-memory"),
        reviewed_memory_candidate_version=_digest("reviewed-memory"),
        review_packet_set_fingerprint=_digest("review-packets"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        coverage_summary={},
    )


def _basis() -> dict:
    return {
        "relation_id": _digest("relation"),
        "evidence_id": _digest("evidence"),
        "source_identity_id": _digest("source"),
        "claim_id": None,
        "polarity": "supports",
        "review_status": "accepted",
        "decision_event_id": "durable-review-m1",
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
