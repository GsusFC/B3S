from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.services.evidence_memory_snapshot import (
    EvidenceMemorySnapshotError,
    build_evidence_memory_snapshot,
)


RUBRIC_VERSION = "memory-rubric-v1"
EVALUATOR_VERSION = "memory-evaluator-v1"


def test_snapshot_is_explicitly_shadow_only_and_has_no_score() -> None:
    claim = "Our mission is to simplify finance."
    result = _snapshot([_report("one", "2026-01-01T00:00:00Z", claim)])

    assert result["schema_version"] == "evidence-memory-snapshot-v1"
    assert result["memory_kind"] == "shadow_candidate"
    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["automatic_scoring_effect"] is False
    assert result["canonical_memory_available"] is False
    assert result["evaluation_ready"] is False
    assert len(result["candidate_memory_version"]) == 64
    assert len(result["shadow_evaluation_identity"]) == 64
    assert result["canonical_memory_version"] is None
    assert result["evaluation_identity"] is None
    assert result["score"] is None
    assert result["score_delta"] is None
    assert result["summary"]["evidence_count"] == 2
    assert result["summary"]["claim_slot_count"] == 1
    assert result["summary"]["claim_variant_count"] == 1
    assert result["summary"]["claim_tile_mapping_count"] == 1
    assert claim not in json.dumps(result)


def test_repeat_and_acquisition_dropout_preserve_candidate_memory_version() -> None:
    claim = "Our mission is to simplify finance."
    first = _report("one", "2026-01-01T00:00:00Z", claim)
    repeat = _report("two", "2026-01-02T00:00:00Z", claim)
    dropout = _empty_report("dropout", "2026-01-03T00:00:00Z")
    dropout["acquisition_gate"] = {
        "state": "warning",
        "warnings": [{"code": "provider_empty_result"}],
    }

    baseline = _snapshot([first])
    repeated = _snapshot([first, repeat])
    dropped_out = _snapshot([first, repeat, dropout])

    assert repeated["report_count"] == 2
    assert dropped_out["report_count"] == 3
    assert repeated["state_fingerprint"] != baseline["state_fingerprint"]
    assert dropped_out["state_fingerprint"] != repeated["state_fingerprint"]
    assert (
        baseline["candidate_memory_version"]
        == repeated["candidate_memory_version"]
        == dropped_out["candidate_memory_version"]
    )
    assert (
        baseline["shadow_evaluation_identity"]
        == repeated["shadow_evaluation_identity"]
        == dropped_out["shadow_evaluation_identity"]
    )
    assert repeated["summary"]["claim_tile_mapping_count"] == 1
    assert dropped_out["summary"]["claim_tile_mapping_count"] == 1


def test_evaluator_mapping_series_drift_does_not_change_candidate_memory() -> None:
    claim = "Our mission is to simplify finance."
    old = _report(
        "old",
        "2026-01-01T00:00:00Z",
        claim,
        report_evaluator="tile-evaluator-a",
    )
    new = _report(
        "new",
        "2026-01-02T00:00:00Z",
        claim,
        report_evaluator="tile-evaluator-b",
    )

    baseline = _snapshot([old])
    drifted = _snapshot([old, new])

    assert (
        drifted["candidate_memory_version"]
        == baseline["candidate_memory_version"]
    )
    assert drifted["summary"]["claim_tile_mapping_count"] == 1


def test_declared_rubric_and_evaluator_versions_change_only_shadow_identity() -> None:
    report = _report(
        "one",
        "2026-01-01T00:00:00Z",
        "Our mission is to simplify finance.",
    )
    baseline = _snapshot([report])
    new_rubric = build_evidence_memory_snapshot(
        [report],
        rubric_version="memory-rubric-v2",
        evaluator_version=EVALUATOR_VERSION,
    )
    new_evaluator = build_evidence_memory_snapshot(
        [report],
        rubric_version=RUBRIC_VERSION,
        evaluator_version="memory-evaluator-v2",
    )

    assert (
        baseline["candidate_memory_version"]
        == new_rubric["candidate_memory_version"]
        == new_evaluator["candidate_memory_version"]
    )
    assert len(
        {
            baseline["shadow_evaluation_identity"],
            new_rubric["shadow_evaluation_identity"],
            new_evaluator["shadow_evaluation_identity"],
        }
    ) == 3


def test_real_claim_change_changes_candidate_memory_version() -> None:
    old = _report(
        "old",
        "2026-01-01T00:00:00Z",
        "Our mission is to simplify finance.",
    )
    new = _report(
        "new",
        "2026-01-02T00:00:00Z",
        "Our mission is to automate treasury.",
    )

    baseline = _snapshot([old])
    changed = _snapshot([old, new])

    assert (
        changed["candidate_memory_version"]
        != baseline["candidate_memory_version"]
    )
    assert changed["summary"]["claim_variant_count"] == 2
    assert changed["summary"]["claim_relation_count"] == 1
    assert changed["summary"]["claim_tile_mapping_count"] == 2


def test_report_order_does_not_change_snapshot_identity() -> None:
    old = _report(
        "old",
        "2026-01-01T00:00:00Z",
        "Our mission is to simplify finance.",
    )
    new = _report(
        "new",
        "2026-01-02T00:00:00Z",
        "Our mission is to automate treasury.",
    )

    chronological = _snapshot([old, new])
    reversed_result = _snapshot(list(reversed(deepcopy([old, new]))))

    assert reversed_result == chronological


def test_manual_acceptance_changes_candidate_version_but_not_authority() -> None:
    report = _report(
        "one",
        "2026-01-01T00:00:00Z",
        "Our mission is to simplify finance.",
    )
    baseline = _snapshot([report])
    source_evidence_id = next(
        item["evidence_id"]
        for item in baseline["semantic_state"]["evidence"]
        if item["source_class"] == "owned_copy"
    )
    accepted = build_evidence_memory_snapshot(
        [report],
        rubric_version=RUBRIC_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        evidence_adjudications=iter(
            [
                {
                    "id": "review-1",
                    "subject_type": "evidence",
                    "subject_id": source_evidence_id,
                    "sequence": 1,
                    "decision": "accepted",
                    "created_at": "2026-01-02T00:00:00Z",
                }
            ]
        ),
    )

    assert (
        accepted["candidate_memory_version"]
        != baseline["candidate_memory_version"]
    )
    assert accepted["summary"]["accepted_evidence_decision_count"] == 1
    assert accepted["canonical_memory_version"] is None
    assert accepted["evaluation_identity"] is None
    assert accepted["authority"] is False


def test_disabled_mode_and_invalid_versions_fail_closed() -> None:
    disabled = build_evidence_memory_snapshot(
        [],
        rubric_version=RUBRIC_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        mode="off",
    )

    assert disabled["mode"] == "disabled"
    assert disabled["candidate_memory_version"] is None
    assert disabled["shadow_evaluation_identity"] is None
    assert disabled["evaluation_identity"] is None
    assert len(disabled["state_fingerprint"]) == 64

    with pytest.raises(
        EvidenceMemorySnapshotError,
        match="rubric_version is required",
    ):
        build_evidence_memory_snapshot(
            [],
            rubric_version="",
            evaluator_version=EVALUATOR_VERSION,
        )


def _snapshot(reports: list[dict]) -> dict:
    return build_evidence_memory_snapshot(
        reports,
        rubric_version=RUBRIC_VERSION,
        evaluator_version=EVALUATOR_VERSION,
    )


def _report(
    report_id: str,
    created_at: str,
    claim: str,
    *,
    report_evaluator: str = "tile-evaluator-a",
) -> dict:
    source = {
        "ref": "web.0",
        "source": "web",
        "evidence_type": "raw_input",
        "url": "https://example.com/about",
        "content": claim,
        "metadata": {"source_class": "owned_copy"},
    }
    interpreted_claim = {
        "ref": "claim.0",
        "source": "derived_strategy",
        "evidence_type": "interpreted_claim",
        "url": "https://example.com/about",
        "content": claim,
        "metadata": {
            "source_class": "derived_strategy",
            "source_evidence_ref": "web.0",
            "claim_slot_key": "mission.primary",
            "claim_type": "mission",
        },
    }
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "reliability_status": "shadow",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "blocks": [],
        "raw": {
            "schema_version": "report-v1",
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "brand_name": "Example",
                        "url": "https://example.com",
                        "evidence": [source, interpreted_claim],
                    }
                }
            },
            "sv9": {
                "evaluator_model": report_evaluator,
                "result": {
                    "rubric_version": "sv9-rubric-v1",
                    "components": {
                        "mission": {
                            "tile_profile": [
                                {
                                    "id": "M1",
                                    "estado": "ok",
                                    "evidencia": claim,
                                    "evidence_ref": "",
                                }
                            ]
                        }
                    },
                },
            },
        },
    }


def _empty_report(report_id: str, created_at: str) -> dict:
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "reliability_status": "shadow",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "blocks": [],
        "raw": {
            "schema_version": "report-v1",
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "brand_name": "Example",
                        "url": "https://example.com",
                        "evidence": [],
                    }
                }
            },
        },
    }
