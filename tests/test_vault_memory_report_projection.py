from __future__ import annotations

import hashlib
import sqlite3
from uuid import uuid4

import pytest

from src import config
from src.history.capture_observation import parse_capture_observation
from src.history.models import ReportImportError
from src.history.report_parser import parse_report
from src.sv9.export_md import build_scan_markdown
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
from web import scoring_store
from web.app import _scan_payload_for_markdown
from web.report_view_model import build_report_view_model


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


def _all_tile_ids() -> set[str]:
    return {
        str(row["tile_id"])
        for row in build_tile_contract_registry()["tiles"]
    }


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
        "acquisition_gate": {"state": "pass"},
    }


def _observation(
    scan_id: str = "scan-tile",
    snapshot: dict | None = None,
    *,
    artifacts: tuple[dict, ...] = (),
) -> dict:
    return evidence_vault_scan_orchestration.build_capture_observation_from_snapshot(
        snapshot=snapshot or _snapshot(scan_id),
        scan_id=scan_id,
        url="https://example.com",
        brand_name="Example",
        mode="incremental",
        artifacts=artifacts,
        observed_at="2026-08-11T05:00:00Z",
    )


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
    all_tile_ids = _all_tile_ids()
    memory, score = _memory_and_score(all_tile_ids, all_tile_ids)

    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-report",
        url="https://example.com",
        brand_name="Example",
        capture_observation=_observation("scan-report"),
        memory=memory,
        score=score,
    )

    assert report["raw"]["vault_memory_projection"] is True
    assert report["raw"]["schema_version"] == "b3s-vault-memory-report-v1"
    assert (
        report["raw"]["flow"]["candidate"]["evidence_pack"]["schema_version"]
        == "brand-evidence-pack-v1"
    )
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
    assert report["most_painful_gap"] is None
    assert report["most_painful_gap_label"] == ""
    assert report["immediate_margin"] == 0
    assert report["detected_count"] == report["block_count"] == 0
    components = {row["key"]: row for row in report["components"]}
    assert components["magnetism"]["score"] == 10
    assert components["magnetism"]["scale"] == 10
    assert components["magnetism"]["points"] == 20
    m1 = next(
        row for row in components["mission"]["tile_profile"]
        if row["tile_id"] == "M1"
    )
    assert m1["evidencia"] == ""
    assert m1["vault_authority_state"] == "accepted"
    assert m1["vault_basis_relation_count"] == 1
    assert len(m1["vault_basis"][0]["relation_id"]) == 64
    assert len(m1["vault_basis"][0]["evidence_id"]) == 64
    view_components = {
        row["key"]: row for row in build_report_view_model(report)["components"]
    }
    assert view_components["mission"]["card"]["brand_quote"] == {}
    assert report["raw"]["sv9"]["result"]["evaluator_model"] == (
        "vault-deterministic-memory"
    )
    assert "score_projected_from_persisted_vault_memory" in report["limitations"]
    # The capture evidence pack stays bound to this scan's durable snapshot.
    assert len(report["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"]) == 1


def test_vault_report_binds_exact_persisted_observation_without_snapshot_hint() -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    snapshot = _snapshot("scan-exact-binding")
    snapshot.pop("source_capture")
    artifacts = (
        {
            "source": "screenshot_capture",
            "kind": "screenshot",
            "status": "success",
            "screenshot_path": "/tmp/example.png",
        },
    )
    observation = _observation(
        "scan-exact-binding",
        snapshot,
        artifacts=artifacts,
    )
    parsed_observation = parse_capture_observation(observation)

    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-exact-binding",
        url="https://example.com",
        brand_name="Example",
        capture_observation=observation,
        memory=memory,
        score=score,
    )

    assert report["raw"]["source_capture"] == {
        "source_scan_id": "scan-exact-binding",
        "observation_hash": parsed_observation.observation_hash,
        "capture_hash": parsed_observation.capture_hash,
    }
    assert report["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"] == list(
        parsed_observation.evidence_records
    )
    assert report["acquisition_artifacts"] == list(artifacts)
    assert report["attempts"] == list(parsed_observation.acquisition_attempts)
    assert report["coverage_acquisition"] == parsed_observation.acquisition_summary
    assert report["acquisition_gate"] == {"state": "pass"}


def test_trusted_vault_report_rebuilds_gate_from_persisted_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    observation = _observation("scan-trusted-attempts")
    observation["pipeline_version"] = "evidence-vault-trusted-acquisition-v1"
    observation["capture_payload"]["acquisition_gate"] = {"state": "pass"}
    observation["limitations"] = [
        "verified_external_document_unavailable",
        "external_acquisition:provider_result_ineligible",
    ]
    observation["acquisition_attempts"] = [
        {
            "provider": "web",
            "intent": "owned_web",
            "status": "success",
            "detail": "verified_raw_document_persisted",
        },
        {
            "provider": "exa",
            "intent": "external_social_profile",
            "status": "error",
            "detail": "provider_result_ineligible",
        },
    ]
    monkeypatch.setattr(
        scan_runner,
        "trusted_acquisition_report_metadata",
        lambda _payload: (
            list(observation["limitations"]),
            list(observation["acquisition_attempts"]),
        ),
    )

    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-trusted-attempts",
        url="https://example.com",
        brand_name="Example",
        capture_observation=observation,
        memory=memory,
        score=score,
    )

    assert report["attempts"] == observation["acquisition_attempts"]
    assert report["acquisition_gate"]["state"] == "warning"
    assert report["acquisition_gate"]["limitations"] == [
        "acquisition_gate:exa_failed"
    ]
    assert report["acquisition_gate"]["warnings"][0]["detail"] == (
        "provider_result_ineligible"
    )
    assert report["acquisition_gate"]["evaluated_at"] == (
        "2026-08-11T05:00:00+00:00"
    )
    assert {
        "score_projected_from_persisted_vault_memory",
        "verified_external_document_unavailable",
        "external_acquisition:provider_result_ineligible",
        "acquisition_gate:exa_failed",
    }.issubset(report["limitations"])


def test_trusted_not_discovered_gate_does_not_invent_exa_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    observation = _observation("scan-trusted-not-discovered")
    observation["pipeline_version"] = "evidence-vault-trusted-acquisition-v1"
    observation["capture_payload"]["acquisition_gate"] = {"state": "pass"}
    observation["limitations"] = [
        "verified_external_document_unavailable",
        "external_acquisition:not_discovered",
    ]
    observation["acquisition_attempts"] = [
        {
            "provider": "web",
            "intent": "owned_web",
            "status": "success",
            "detail": "verified_raw_document_persisted",
        }
    ]
    monkeypatch.setattr(
        scan_runner,
        "trusted_acquisition_report_metadata",
        lambda _payload: (
            list(observation["limitations"]),
            list(observation["acquisition_attempts"]),
        ),
    )

    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-trusted-not-discovered",
        url="https://example.com",
        brand_name="Example",
        capture_observation=observation,
        memory=memory,
        score=score,
    )

    warning = report["acquisition_gate"]["warnings"][0]
    assert warning["code"] == "external_identity_not_discovered"
    assert warning["status"] == "not_discovered"
    assert "exa_failed" not in report["acquisition_gate"]["limitations"]
    assert report["acquisition_gate"]["limitations"] == [
        "acquisition_gate:external_identity_not_discovered"
    ]


def test_partial_vault_memory_report_stays_shadow_and_counts_unresolved_tiles() -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})

    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-partial",
        url="https://example.com",
        brand_name="Example",
        capture_observation=_observation("scan-partial"),
        memory=memory,
        score=score,
    )

    assert score["authority_coverage"]["score_completeness"] == "partial"
    assert score["authority_coverage"]["unresolved_tile_count"] == 78
    assert report["reliability_status"] == "shadow"
    assert "vault_authority_coverage_partial" in report[
        "reliability_reason_codes"
    ]
    assert "scan_not_complete" not in report["reliability_reason_codes"]
    assert report["total_blind_spots"] == 78
    assert report["most_painful_gap"] == "magnetism"
    assert report["most_painful_gap_label"] == "Magnetism"
    assert report["immediate_margin"] == 12
    mission = next(row for row in report["components"] if row["key"] == "mission")
    unresolved_m3 = next(
        row for row in mission["tile_profile"] if row["tile_id"] == "M3"
    )
    assert unresolved_m3["vault_authority_state"] == "unresolved"
    assert unresolved_m3["vault_basis"] == []
    assert report["raw"]["sv9"]["result"]["total_blind_spots"] == 78


def test_vault_markdown_preserves_shadow_confidence_and_magnetism_cap() -> None:
    partial_memory, partial_score = _memory_and_score(
        {"M1", "M2"},
        {"M1", "M2"},
    )
    partial_report = scan_runner._compose_vault_memory_report(
        scan_id="scan-markdown-partial",
        url="https://example.com",
        brand_name="Example",
        capture_observation=_observation("scan-markdown-partial"),
        memory=partial_memory,
        score=partial_score,
    )
    partial_payload = _scan_payload_for_markdown(partial_report)
    partial_markdown = build_scan_markdown(partial_payload)

    assert partial_payload["reliability_status"] == "shadow"
    assert partial_payload["components"]["mission"]["confidence"] == "baja"
    assert "Confiabilidad: **sombra**" in partial_markdown
    assert "cobertura de autoridad Vault parcial" in partial_markdown
    assert "scan incompleto" not in partial_markdown
    assert "confianza alta" not in partial_markdown

    all_tile_ids = _all_tile_ids()
    magnetism_tiles = {
        tile_id for tile_id in all_tile_ids if tile_id.startswith("MG")
    }
    capped_memory, capped_score = _memory_and_score(
        {"M1", *magnetism_tiles},
        all_tile_ids,
    )
    assert capped_score["magnetism_capped"] is True
    capped_report = scan_runner._compose_vault_memory_report(
        scan_id="scan-markdown-capped",
        url="https://example.com",
        brand_name="Example",
        capture_observation=_observation("scan-markdown-capped"),
        memory=capped_memory,
        score=capped_score,
    )
    capped_payload = _scan_payload_for_markdown(capped_report)
    capped_markdown = build_scan_markdown(capped_payload)

    assert capped_payload["magnetism_capped"] is True
    assert "Tope de Magnetism aplicado: sí" in capped_markdown


def test_compose_vault_memory_report_tracks_accepted_absences_as_shadow() -> None:
    all_tile_ids = _all_tile_ids()
    memory, score = _memory_and_score(all_tile_ids - {"M3"}, all_tile_ids)

    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-shadow",
        url="https://example.com",
        brand_name="Example",
        capture_observation=_observation("scan-shadow"),
        memory=memory,
        score=score,
    )

    assert report["reliability_status"] == "shadow"
    assert report["total_blind_spots"] == 1
    assert report["most_painful_gap"] == "mission"
    assert report["most_painful_gap_label"] == "Misión"
    assert report["immediate_margin"] == 1
    tiles = {
        (tile["tile_id"], tile["estado"])
        for component in report["components"]
        for tile in component["tile_profile"]
    }
    assert ("M1", "ok") in tiles
    assert ("M3", "sin_evidencia") in tiles
    m3 = next(
        tile
        for component in report["components"]
        for tile in component["tile_profile"]
        if tile["tile_id"] == "M3"
    )
    assert m3["vault_authority_state"] == "accepted"
    assert m3["vault_authority_profile_id"] == "human-reviewed-test-v1"
    assert m3["vault_basis"] == []
    assert all(
        component["status"] == "scored" for component in report["components"]
    )


def test_vault_memory_report_passes_report_import_contract() -> None:
    all_tile_ids = _all_tile_ids()
    memory, score = _memory_and_score(all_tile_ids, all_tile_ids)
    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-import",
        url="https://example.com",
        brand_name="Example",
        capture_observation=_observation("scan-import"),
        memory=memory,
        score=score,
    )

    parsed = parse_report(report)
    assert parsed.score == float(score["score"])
    assert parsed.base_average == float(score["base_average"])
    assert parsed.evaluator_model == "vault-deterministic-memory"
    assert parsed.pipeline_version == "b3s-vault-memory-report-v1"
    assert parsed.evaluated_at.isoformat() == "2026-08-06T11:00:00+00:00"
    assert parsed.evaluated_at < parsed.recorded_at
    assert "score_projected_from_persisted_vault_memory" in parsed.limitations
    assert len(parsed.components) == 10
    parsed_components = {row["key"]: row for row in parsed.components}
    assert parsed_components["magnetism"]["score"] == 10
    assert parsed_components["magnetism"]["scale"] == 10
    assert parsed_components["magnetism"]["points"] == 20
    assert len(parsed.evidence_records) == 1
    assert {row["provider"] for row in parsed.acquisition_attempts} == {
        "exa",
        "web",
    }


def test_vault_memory_report_records_all_tiles_in_sqlite_mirror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    all_tile_ids = _all_tile_ids()
    memory, score = _memory_and_score(all_tile_ids, all_tile_ids)
    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-sqlite-mirror",
        url="https://example.com",
        brand_name="Example",
        capture_observation=_observation("scan-sqlite-mirror"),
        memory=memory,
        score=score,
    )
    database_path = tmp_path / "scoring.sqlite3"
    monkeypatch.setenv("B3S_SCORING_DB_PATH", str(database_path))

    scoring_store.record_report(report)

    with sqlite3.connect(database_path) as connection:
        component_count = connection.execute(
            "SELECT COUNT(*) FROM components WHERE run_id = ?",
            ("scan-sqlite-mirror",),
        ).fetchone()[0]
        tile_count = connection.execute(
            "SELECT COUNT(*) FROM tiles WHERE run_id = ?",
            ("scan-sqlite-mirror",),
        ).fetchone()[0]
    assert component_count == 10
    assert tile_count == 80


def test_vault_memory_report_preserves_current_acquisition_limitations() -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    snapshot = _snapshot()
    snapshot["acquisition_gate"] = {
        "state": "warning",
        "limitations": ["acquisition_warning:exa_failed"],
    }

    report = scan_runner._compose_vault_memory_report(
        scan_id="scan-warning",
        url="https://example.com",
        brand_name="Example",
        capture_observation=_observation("scan-warning", snapshot),
        memory=memory,
        score=score,
    )

    assert report["limitations"] == [
        "score_projected_from_persisted_vault_memory",
        "acquisition_warning:exa_failed",
    ]


def test_vault_memory_report_rejects_cross_brand_memory() -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    memory["brand_identity"] = "other.example"

    with pytest.raises(RuntimeError, match="vault_report_memory_brand_mismatch"):
        scan_runner._compose_vault_memory_report(
            scan_id="scan-cross-brand",
            url="https://example.com",
            brand_name="Example",
            capture_observation=_observation("scan-cross-brand"),
            memory=memory,
            score=score,
        )


def test_vault_memory_report_rejects_mismatched_memory_and_score_versions() -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    mismatched_score = dict(score)
    mismatched_score["canonical_memory_version"] = "f" * 64

    with pytest.raises(RuntimeError, match="vault_memory_score_version_mismatch"):
        scan_runner._compose_vault_memory_report(
            scan_id="scan-version-mismatch",
            url="https://example.com",
            brand_name="Example",
            capture_observation=_observation("scan-version-mismatch"),
            memory=memory,
            score=mismatched_score,
        )


def test_vault_memory_report_fails_closed_when_capture_evidence_is_invalid() -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    observation = _observation("scan-evidence-failure")
    observation["evidence_records"] = [{"source": "web"}]

    with pytest.raises(ReportImportError, match=r"evidence_records\[0\].ref"):
        scan_runner._compose_vault_memory_report(
            scan_id="scan-evidence-failure",
            url="https://example.com",
            brand_name="Example",
            capture_observation=observation,
            memory=memory,
            score=score,
        )


def test_vault_resume_run_branches_to_memory_projection_without_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval

    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    scan_id = "scan-vault-resume"
    status = _status(scan_id)
    scan_runner._SCANS[scan_id] = status
    snapshot = _snapshot(scan_id)
    snapshot["raw_inputs"].append(
        {
            "source": "screenshot_capture",
            "payload": {
                "capture": {
                    "status": "success",
                    "success": True,
                    "source": "playwright",
                    "screenshot_path": "/tmp/example-first-fold.png",
                }
            },
        }
    )

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
    def prepare_with_artifacts(**kwargs):
        artifacts = tuple(kwargs.get("artifacts") or ())
        return {
            "mode": "incremental",
            "operation_plan": None,
            "report_observation": _observation(
                scan_id,
                snapshot,
                artifacts=artifacts,
            ),
        }

    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        prepare_with_artifacts,
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
    assert report["acquisition_artifacts"][0]["screenshot_path"] == (
        "/tmp/example-first-fold.png"
    )


def test_vault_projection_cancellation_does_not_advance_report_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    scan_id = "scan-vault-cancelled-during-projection"
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

    published: list[dict] = []

    def cancel_during_stability(report: dict) -> dict:
        status["state"] = "cancelled"
        status["phase"] = "cancelled"
        return report

    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        False,
    )
    monkeypatch.setattr(
        scan_runner,
        "_capture_snapshot",
        lambda *_a, **_k: _snapshot(scan_id),
    )
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda _value: None)
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: FakeRepo())
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: {
            "mode": "incremental",
            "operation_plan": None,
            "report_observation": _observation(scan_id, snapshot),
        },
    )
    monkeypatch.setattr(
        scan_runner,
        "_attach_evidence_stability",
        cancel_during_stability,
    )
    monkeypatch.setattr(
        scan_runner,
        "_publish_completed_report",
        lambda _scan_id, report: published.append(report) or True,
    )
    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
        assert published == []
        assert status["state"] == "cancelled"
        assert status["phase"] == "cancelled"
        scan_runner._set_phase(scan_id, "report", "running")
        assert status["phase"] == "cancelled"
        assert next(
            row for row in status["phases"] if row["key"] == "report"
        )["state"] == "pending"
    finally:
        scan_runner._SCANS.pop(scan_id, None)


def test_vault_activation_boundary_rejects_late_cancellation() -> None:
    scan_id = "scan-vault-activation-boundary"
    status = _status(scan_id)
    scan_runner._SCANS[scan_id] = status
    cancellation_results: list[dict] = []

    class FakeRepo:
        def activate_evidence_vault_operational_scanner_result(
            self,
            *_a,
            **_k,
        ):
            cancellation_results.append(scan_runner.cancel_scan(scan_id) or {})
            return {"memory": {}, "score": {}}

    try:
        result = scan_runner._activate_vault_result_unless_cancelled(
            scan_id,
            FakeRepo(),
            "https://example.com",
            operation_plan_fingerprint="a" * 64,
        )
        assert result == {"memory": {}, "score": {}}
        assert cancellation_results == [
            {
                "state": "running",
                "cancelled": False,
                "reason": "vault_activation_in_progress",
                "acquisition_gate": {},
            }
        ]
        assert status["state"] == "running"
    finally:
        scan_runner._SCANS.pop(scan_id, None)


def test_phase_persistence_reasserts_terminal_state_after_cancel_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_id = "scan-phase-cancel-race"
    status = _status(scan_id)
    scan_runner._SCANS[scan_id] = status
    persisted_states: list[str] = []

    def persist_and_race(value: dict) -> None:
        persisted_states.append(str(value.get("state") or ""))
        if len(persisted_states) == 1:
            status["state"] = "cancelled"
            status["phase"] = "cancelled"

    monkeypatch.setattr(scan_runner, "_persist_scan_status", persist_and_race)
    try:
        scan_runner._set_phase(scan_id, "interpret", "running")
        assert persisted_states == ["running", "cancelled"]
    finally:
        scan_runner._SCANS.pop(scan_id, None)


def test_fresh_vault_activation_projects_memory_without_second_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval
    from src.features import llm_analyzer
    from src.services import evidence_vault_incremental_executor

    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    scan_id = "scan-vault-fresh-activation"
    scan_runner._SCANS[scan_id] = _status(scan_id)
    snapshot = _snapshot(scan_id)
    operation_plan = {
        "operation_plan_fingerprint": "a" * 64,
        "operations": {"llm_required": True},
    }

    class FakeRepo:
        def activate_evidence_vault_operational_scanner_result(
            self, *_a, **_k
        ):
            return {
                "created": True,
                "memory": memory,
                "score": score,
                "score_replayed": False,
            }

    captured: list[dict] = []

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
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: FakeRepo())
    monkeypatch.setattr(
        scan_runner,
        "_publish_completed_report",
        lambda _scan_id, report: captured.append(report) or True,
    )
    monkeypatch.setattr(scan_runner, "_attach_evidence_stability", lambda value: value)
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: {
            "mode": "incremental",
            "operation_plan": operation_plan,
            "report_observation": _observation(scan_id, snapshot),
        },
    )
    operation_llm = object()
    monkeypatch.setattr(
        llm_analyzer,
        "LLMAnalyzer",
        lambda **_k: operation_llm,
    )

    def execute_operation(**kwargs):
        assert kwargs["llm"] is operation_llm
        return {"execution_status": "completed", "work_performed": True}

    monkeypatch.setattr(
        evidence_vault_incremental_executor,
        "execute_vault_operation_plan",
        execute_operation,
    )

    def forbidden_interpreter(*_a, **_k):
        raise AssertionError("fresh Vault activation must not run a second interpreter")

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
    assert captured[0]["raw"]["vault_memory_projection"] is True
    assert captured[0]["vault_memory_version"] == memory["canonical_memory_version"]


def test_result_persisted_resume_materializes_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval
    from src.features import llm_analyzer
    from src.services import evidence_vault_incremental_executor

    memory, score = _memory_and_score({"M1", "M2"}, {"M1", "M2"})
    scan_id = "scan-vault-result-persisted"
    status = _status(scan_id)
    scan_runner._SCANS[scan_id] = status
    snapshot = _snapshot(scan_id)
    operation_plan = {
        "operation_plan_fingerprint": "e" * 64,
        "operations": {"llm_required": True},
    }
    observed: list[str] = []
    published: list[dict] = []

    class FakeRepo:
        def activate_evidence_vault_operational_scanner_result(
            self,
            *_a,
            **_k,
        ):
            observed.append("activated_materialized_result")
            return {"memory": memory, "score": score}

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
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: FakeRepo())
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: {
            "mode": "incremental",
            "operation_plan": operation_plan,
            "report_observation": _observation(scan_id, snapshot),
            "resume": {
                "analysis_status": "result_persisted",
                "semantic_work_completed": True,
                "materialization_required": True,
            },
        },
    )
    monkeypatch.setattr(
        llm_analyzer,
        "LLMAnalyzer",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("persisted result must not invoke the LLM")
        ),
    )

    def materialize_result(**kwargs):
        assert kwargs["llm"] is None
        observed.append("materialized")
        return {"execution_status": "completed", "work_performed": True}

    monkeypatch.setattr(
        evidence_vault_incremental_executor,
        "execute_vault_operation_plan",
        materialize_result,
    )
    monkeypatch.setattr(
        flow_eval,
        "build_flow_sv9_shadow_eval",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("materialized result must not use the legacy interpreter")
        ),
    )
    monkeypatch.setattr(
        scan_runner,
        "_publish_completed_report",
        lambda _scan_id, report: published.append(report) or True,
    )
    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
        assert observed == ["materialized", "activated_materialized_result"]
        assert len(published) == 1
        assert published[0]["raw"]["vault_memory_projection"] is True
    finally:
        scan_runner._SCANS.pop(scan_id, None)


def test_cancellation_after_vault_execution_skips_memory_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features import llm_analyzer
    from src.services import evidence_vault_incremental_executor

    scan_id = "scan-vault-cancel-before-activation"
    status = _status(scan_id)
    scan_runner._SCANS[scan_id] = status
    snapshot = _snapshot(scan_id)
    operation_plan = {
        "operation_plan_fingerprint": "c" * 64,
        "operations": {"llm_required": True},
    }

    class FakeRepo:
        activation_calls = 0

        def activate_evidence_vault_operational_scanner_result(
            self, *_a, **_k
        ):
            self.activation_calls += 1
            raise AssertionError("cancelled execution must remain resumable")

    repository = FakeRepo()
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
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: {
            "mode": "incremental",
            "operation_plan": operation_plan,
            "report_observation": _observation(scan_id, snapshot),
        },
    )
    monkeypatch.setattr(llm_analyzer, "LLMAnalyzer", lambda **_k: object())

    def complete_then_cancel(**_kwargs):
        scan_runner.cancel_scan(scan_id)
        return {"execution_status": "completed", "work_performed": True}

    monkeypatch.setattr(
        evidence_vault_incremental_executor,
        "execute_vault_operation_plan",
        complete_then_cancel,
    )
    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
        assert status["state"] == "cancelled"
        assert repository.activation_calls == 0
    finally:
        scan_runner._SCANS.pop(scan_id, None)


def test_llm_vault_operation_without_memory_score_fails_instead_of_reinterpreting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval
    from src.features import llm_analyzer
    from src.services import evidence_vault_incremental_executor

    scan_id = "scan-vault-llm-missing-memory"
    status = _status(scan_id)
    scan_runner._SCANS[scan_id] = status
    snapshot = _snapshot(scan_id)
    operation_plan = {
        "operation_plan_fingerprint": "b" * 64,
        "operations": {"llm_required": True},
    }

    class FakeRepo:
        def activate_evidence_vault_operational_scanner_result(
            self, *_a, **_k
        ):
            return {"created": False, "memory": None, "score": None}

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
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: FakeRepo())
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: {
            "mode": "incremental",
            "operation_plan": operation_plan,
            "report_observation": _observation(scan_id, snapshot),
        },
    )
    operation_llm = object()
    monkeypatch.setattr(llm_analyzer, "LLMAnalyzer", lambda **_k: operation_llm)
    def replay_operation(**kwargs):
        assert kwargs["llm"] is operation_llm
        return {"execution_status": "completed", "work_performed": False}

    monkeypatch.setattr(
        evidence_vault_incremental_executor,
        "execute_vault_operation_plan",
        replay_operation,
    )

    def forbidden_interpreter(*_a, **_k):
        raise AssertionError("completed Vault interpretation must not run again")

    monkeypatch.setattr(
        flow_eval,
        "build_flow_sv9_shadow_eval",
        forbidden_interpreter,
    )
    monkeypatch.setattr(
        scan_runner,
        "_publish_completed_report",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("missing Vault memory cannot publish a report")
        ),
    )
    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
        assert status["state"] == "error"
        assert status["phase"] == "error"
        assert "vault_interpretation_missing_memory_score" in status["error"]
    finally:
        scan_runner._SCANS.pop(scan_id, None)


def test_completed_llm_resume_reactivates_and_never_uses_legacy_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval

    scan_id = "scan-vault-completed-resume-missing-memory"
    status = _status(scan_id)
    scan_runner._SCANS[scan_id] = status
    snapshot = _snapshot(scan_id)
    observed: list[str] = []
    plan_fingerprint = "d" * 64

    class FakeRepo:
        def activate_evidence_vault_operational_scanner_result(
            self,
            *_a,
            **kwargs,
        ):
            assert kwargs["operation_plan_fingerprint"] == plan_fingerprint
            observed.append("activation_retried")
            return {"memory": None, "score": None}

    repository = FakeRepo()
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
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: {
            "mode": "incremental",
            "operation_plan": None,
            "report_observation": _observation(scan_id, snapshot),
            "resume": {
                "analysis_status": "completed",
                "semantic_work_completed": True,
                "operation_plan_fingerprint": plan_fingerprint,
            },
        },
    )

    def forbidden_interpreter(*_a, **_k):
        observed.append("legacy_interpreter")
        raise AssertionError("completed Vault semantic work must not be repeated")

    monkeypatch.setattr(
        flow_eval,
        "build_flow_sv9_shadow_eval",
        forbidden_interpreter,
    )
    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
        assert status["state"] == "error"
        assert "vault_interpretation_missing_memory_score" in status["error"]
        assert observed == ["activation_retried"]
    finally:
        scan_runner._SCANS.pop(scan_id, None)


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
