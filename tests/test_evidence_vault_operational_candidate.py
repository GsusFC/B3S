from __future__ import annotations

import hashlib

from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_tile_contract_registry,
)
from src.services.evidence_vault_operational_candidate import (
    build_operational_packet_from_reviewed_candidate,
    build_provisional_operational_packet_from_report,
)


def test_reviewed_relation_is_adopted_without_reviewing_the_tile_again() -> None:
    candidates = _candidates()
    candidates[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_basis("m1", "supports", reviewed=True)],
    )

    operational = build_operational_packet_from_reviewed_candidate(
        _packet(candidates)
    )

    accepted = operational["accepted_memory"]["accepted_tiles"]
    coverage = operational["scoring_projection"]["coverage"]
    assert [row["tile_id"] for row in accepted] == ["M1"]
    assert accepted[0]["semantic_state"] == "ok"
    assert accepted[0]["authority_source"] == "policy"
    assert coverage["accepted_tile_count"] == 1
    assert coverage["unresolved_tile_count"] == 79


def test_model_only_relation_remains_pending_and_requires_review() -> None:
    candidates = _candidates()
    candidates[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_basis("m1", "supports", reviewed=False)],
    )

    operational = build_operational_packet_from_reviewed_candidate(
        _packet(candidates)
    )
    overlay = operational["candidate_overlay"]["candidate_tiles"]
    m1 = next(row for row in overlay if row["tile_id"] == "M1")

    assert operational["accepted_memory"]["accepted_tiles"] == []
    assert m1["semantic_state"] == "ok"
    assert m1["authority_state"] == "pending"
    assert m1["review_state"] == "required"


def test_empty_tiles_are_unresolved_without_generating_eighty_reviews() -> None:
    operational = build_operational_packet_from_reviewed_candidate(
        _packet(_candidates())
    )

    overlay = operational["candidate_overlay"]["candidate_tiles"]
    assert len(overlay) == 80
    assert all(row["review_state"] == "none" for row in overlay)
    assert operational["scoring_projection"]["coverage"][
        "pending_initial_tile_count"
    ] == 80


def test_contradiction_is_local_and_does_not_block_other_reviewed_tiles() -> None:
    candidates = _candidates()
    candidates[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_basis("m1", "supports", reviewed=True)],
    )
    c8_index = next(
        index for index, row in enumerate(candidates) if row["tile_id"] == "C8"
    )
    candidates[c8_index] = build_candidate_tile(
        tile_id="C8",
        basis=[
            _basis("c8-positive", "supports", reviewed=True),
            _basis("c8-negative", "contradicts", reviewed=True),
        ],
        unresolved_refs=["c8-conflict"],
    )

    operational = build_operational_packet_from_reviewed_candidate(
        _packet(candidates, unresolved=["c8-conflict"])
    )
    coverage = operational["scoring_projection"]["coverage"]
    c8 = next(
        row
        for row in operational["scoring_projection"]["tiles"]
        if row["tile_id"] == "C8"
    )

    assert coverage["accepted_tile_count"] == 1
    assert coverage["contradiction_on_unresolved_count"] == 1
    assert c8["candidate_preview_points"] is None
    assert c8["canonical_semantic_state"] is None


def test_scanner_report_becomes_provisional_overlay_not_automatic_truth() -> None:
    report = {
        "id": "diagnostic-scan-1",
        "url": "https://www.example.com",
        "pipeline_commit_sha": "abc123",
        "score": 57,
        "components": [
            {
                "key": "mission",
                "status": "scored",
                "tile_profile": [
                    {"id": "M1", "estado": "ok", "evidencia": "Mission"},
                    {"id": "M2", "estado": "no", "motivo": "Generic"},
                ],
            }
        ],
    }

    operational = build_provisional_operational_packet_from_report(report)
    projection = {
        row["tile_id"]: row for row in operational["scoring_projection"]["tiles"]
    }

    assert operational["brand_identity"] == "example.com"
    assert operational["accepted_memory"]["accepted_tiles"] == []
    assert operational["scoring_projection"]["coverage"]["accepted_tile_count"] == 0
    assert projection["M1"]["candidate_semantic_state"] == "ok"
    assert projection["M2"]["candidate_semantic_state"] == "no"
    assert projection["M3"]["candidate_semantic_state"] == "sin_evidencia"
    assert projection["M1"]["score_eligible"] is False


def _candidates() -> list[dict]:
    return [
        build_candidate_tile(tile_id=str(row["tile_id"]))
        for row in build_tile_contract_registry()["tiles"]
    ]


def _packet(candidates: list[dict], *, unresolved: list[str] | None = None) -> dict:
    return build_candidate_packet(
        brand_identity="example.com",
        parent_canonical_memory_version=None,
        candidate_memory_version=_digest("candidate-memory"),
        accepted_memory_candidate_version=_digest("accepted-memory"),
        reviewed_memory_candidate_version=_digest("reviewed-memory"),
        review_packet_set_fingerprint=_digest("review-packets"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        unresolved_items=[
            {
                "unresolved_id": ref,
                "kind": "contradiction",
                "blocking": True,
                "details": {},
            }
            for ref in unresolved or []
        ],
    )


def _basis(seed: str, polarity: str, *, reviewed: bool) -> dict:
    return {
        "relation_id": _digest(f"{seed}-relation"),
        "evidence_id": _digest(f"{seed}-evidence"),
        "source_identity_id": _digest(f"{seed}-source"),
        "claim_id": None,
        "polarity": polarity,
        "review_status": "accepted" if reviewed else "unreviewed",
        "decision_event_id": f"review-{seed}" if reviewed else None,
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
