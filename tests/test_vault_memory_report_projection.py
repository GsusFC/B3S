from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest

from src import config
from src.history.report_parser import parse_report
from src.services import evidence_vault_scan_orchestration
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
    build_operational_memory_packet,
)
from src.services.evidence_vault_operational_scoring import (
    build_operational_score_evaluation,
)
from web import report_store
from web import scan_runner


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _packet(ok_tile_ids: set[str], accepted_ids: set[str]) -> dict:
    candidates = _candidates(ok_tile_ids)
    by_id = {row["tile_id"]: row for row in candidates}
    dispositions = {
        tile_id: {
            "authority_state": "accepted",
            "review_state": "none",
            "authority_profile_id": (
                "reviewed-basis-reducer-v1"
                if by_id[tile_id]["basis"]
                else "human-reviewed-test-v1"
            ),
            "authority_source": "policy" if by_id[tile_id]["basis"] else "human",
            "decision_event_id": (
                None if by_id[tile_id]["basis"] else f"human-review-{tile_id}"
            ),
            "policy_decision": (
                evaluate_reviewed_basis_authority(candidate_tile=by_id[tile_id])
                if by_id[tile_id]["basis"]
                else None
            ),
        }
        for tile_id in accepted_ids
    }
    return build_operational_memory_packet(
        brand_identity="example.com",
        source_candidate_packet_fingerprint=_digest("source-packet"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions=dispositions,
    )


def _adopted_memory(ok_tile_ids: set[str], accepted_ids: set[str]) -> dict:
    packet = _packet(ok_tile_ids, accepted_ids)
    event = build_operational_adoption_event(
        packet,
        event_id=str(uuid4()),
        sequence=1,
        previous_event_id=None,
        adopted_by="policy",
        actor_id="automatic-baseline-v1",
        policy_fingerprint=_digest("policy"),
        created_at="2026-08-06T12:00:00+02:00",
        idempotency_key_hash=_digest("idempotency-1"),
        expected_current_canonical_memory_version=None,
    )
    return project_adopted_operational_memory(packet, event)


def _memory_and_score(ok_tile_ids: set[str], accepted_ids: set[str]):
    memory = _adopted_memory(ok_tile_ids, accepted_ids)
    score = build_operational_score_evaluation(
        memory,
        created_at="2026-08-06T13:00:00+02:00",
    )
    return memory, score


def _snapshot(scan_id: str = "scan-tile") -> dict:
    return {
        "run": {"brand_name": "Example", "id": 123, "url": "https://example.com"},
        "brand_name": "Example",
        "raw_inputs": [
            {
                "source": "verified_raw_document",
                "payload": {
                    "role": "owned_web",
                    "url": "https://example.com",
                    "content": "Exact owned verified text.",
                    "extracted_document_sha256": "3" * 64,
                    "extractor_version": "vault-extractor-v1",
                    "receipt_fingerprint": "1" * 64,
                },
            }
        ],
        "acquisition_steps": {
            "web": {"source": "web", "status": "success"},
            "exa": {"source": "exa", "status": "success"},
        },
        "features": [],
        "source_capture": {
            "source_scan_id": scan_id,
            "observation_hash": "5" * 64,
            "capture_hash": "1" * 64,
        },
        "acquisition_gate": {"state": "passed"},
    }


def _status(scan_id: str) -> dict:
    return {
        "id": scan_id,
        "url": "https://example.com",
        "brand_name": "Example",
        "state": "running",
        "phase": "capture",
        "phases": [
            {"key": key, "state": "pending"}
            for key in ("capture", "interpret", "score", "report")
        ],
        "acquisition": [],
        "acquisition_gate": {},
        "allow_degraded_fallback": False,
        "error": None,
        "error_code": None,
        "started_at": "2026-08-09T00:00:00Z",
        "completed_at": None,
    }


def test_compose_vault_memory_report_projects_durable_memory_model_free() -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})

    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-report",
        url="https://example.com",
        brand_name="Example",
        snapshot=_snapshot(),
        memory=memory,
        score=score,
    )

    assert report["raw"]["vault_memory_projection"] is True
    assert report["raw"]["schema_version"] == "b3s-vault-memory-report-v1"
    assert report["raw"]["flow"]["interpretation_debug"]["mode"] == (
        "persisted_vault_memory"
    )
    assert report["raw"]["source_run_id"] == "123"
    assert report["score"] == score["score"]
    assert report["base_average"] == score["base_average"]
    assert report["reliability_status"] == "reliable"
    assert report["vault_memory_version"] == memory["canonical_memory_version"]
    assert report["vault_score_evaluation_identity"] == score["evaluation_identity"]
    assert report["total_blind_spots"] == 0
    assert report["raw"]["sv9"]["result"]["evaluator_model"] == (
        "vault-deterministic-memory"
    )
    assert "score_projected_from_persisted_vault_memory" in report["limitations"]
    # The capture evidence pack stays bound to this scan's durable snapshot.
    assert len(report["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"]) == 1


def test_compose_vault_memory_report_tracks_accepted_absences_as_shadow() -> None:
    memory, score = _memory_and_score({"M1"}, {"M1", "M3"})

    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-shadow",
        url="https://example.com",
        brand_name="Example",
        snapshot=_snapshot(),
        memory=memory,
        score=score,
    )

    assert report["reliability_status"] == "shadow"
    assert report["total_blind_spots"] == 1
    assert report["most_painful_gap"] == "mission"
    tiles = {
        (tile["tile_id"], tile["estado"])
        for component in report["components"]
        for tile in component["tile_profile"]
    }
    assert ("M1", "ok") in tiles
    assert ("M3", "sin_evidencia") in tiles
    assert all(
        component["status"] == "scored" for component in report["components"]
    )


def test_vault_memory_report_passes_report_import_contract() -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-import",
        url="https://example.com",
        brand_name="Example",
        snapshot=_snapshot(),
        memory=memory,
        score=score,
    )

    parsed = parse_report(report)
    assert parsed.score == float(score["score"])
    assert parsed.base_average == float(score["base_average"])
    assert parsed.evaluator_model == "vault-deterministic-memory"
    assert parsed.pipeline_version == "b3s-vault-memory-report-v1"
    assert "score_projected_from_persisted_vault_memory" in parsed.limitations
    assert len(parsed.components) == 10
    assert len(parsed.evidence_records) == 1


def test_vault_resume_run_branches_to_memory_projection_without_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval

    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    scan_id = "scan-vault-resume"
    status = _status(scan_id)
    scan_runner._SCANS[scan_id] = status
    snapshot = _snapshot(scan_id)

    class FakeRepo:
        def get_evidence_vault_operational_memory(self, *_a, **_k):
            return memory

        def get_or_create_evidence_vault_operational_score_evaluation(
            self, *_a, **_k
        ):
            return score, False

    captured: list[dict] = []

    def fake_publish(_scan_id: str, report: dict) -> bool:
        captured.append(report)
        return True

    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        False,
    )
    monkeypatch.setattr(
        scan_runner,
        "_capture_snapshot",
        lambda *_a, **_k: snapshot,
    )
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda _value: None)
    monkeypatch.setattr(
        report_store,
        "_postgres_repository",
        lambda: FakeRepo(),
    )
    monkeypatch.setattr(scan_runner, "_publish_completed_report", fake_publish)
    monkeypatch.setattr(scan_runner, "_attach_evidence_stability", lambda value: value)
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: {"mode": "incremental", "operation_plan": None},
    )

    def forbidden_interpreter(*_a, **_k):
        raise AssertionError("vault memory projection must not re-run the interpreter")

    monkeypatch.setattr(
        flow_eval,
        "build_flow_sv9_shadow_eval",
        forbidden_interpreter,
    )
    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
    finally:
        scan_runner._SCANS.pop(scan_id, None)

    assert len(captured) == 1
    report = captured[0]
    assert report["id"] == scan_id
    assert report["raw"]["vault_memory_projection"] is True
    assert report["raw"]["flow"]["interpretation_debug"]["mode"] == (
        "persisted_vault_memory"
    )
    assert report["score"] == score["score"]


def test_vault_run_without_canonical_memory_falls_back_to_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval

    scan_id = "scan-vault-no-memory"
    status = _status(scan_id)
    scan_runner._SCANS[scan_id] = status
    snapshot = _snapshot(scan_id)

    class FakeRepo:
        def get_evidence_vault_operational_memory(self, *_a, **_k):
            return None

        def get_or_create_evidence_vault_operational_score_evaluation(
            self, *_a, **_k
        ):
            return None, False

    observed = []

    def fake_publish(_scan_id: str, report: dict) -> bool:
        observed.append(report)
        return True

    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        False,
    )
    monkeypatch.setattr(
        scan_runner,
        "_capture_snapshot",
        lambda *_a, **_k: snapshot,
    )
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda _value: None)
    monkeypatch.setattr(
        report_store,
        "_postgres_repository",
        lambda: FakeRepo(),
    )
    monkeypatch.setattr(scan_runner, "_publish_completed_report", fake_publish)
    monkeypatch.setattr(scan_runner, "_attach_evidence_stability", lambda value: value)
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: {"mode": "initial", "operation_plan": None},
    )

    def interpreter_envelope(*_a, **_k):
        observed.append("interpreter_ran")
        return {}

    monkeypatch.setattr(
        flow_eval,
        "build_flow_sv9_shadow_eval",
        interpreter_envelope,
    )
    monkeypatch.setattr(
        scan_runner,
        "_compose_report",
        lambda *_a, **_k: {"id": scan_id},
    )
    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
    finally:
        scan_runner._SCANS.pop(scan_id, None)

    assert "interpreter_ran" in observed
