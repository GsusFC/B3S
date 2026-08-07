from __future__ import annotations

import os

import pytest

from src.history.models import CaptureConflictError
from src.history.report_parser import canonical_json_hash


pytestmark = pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)


def test_capture_only_persistence_is_idempotent_and_creates_no_evaluation() -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        pytest.fail("B3S_TEST_ALLOW_SCHEMA_DROP=1 is required")
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    repository = PostgresHistoryRepository(dsn)
    repository.migrate()
    payload = _observation()

    first = repository.persist_capture_observation(payload)
    second = repository.persist_capture_observation(payload)

    assert first.status == "imported"
    assert second.status == "unchanged"
    assert second.capture_id == first.capture_id
    assert repository.get_current_brand_state("example.com") is None

    with psycopg.connect(dsn) as conn:
        counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM b3s_history.scan_runs) AS scans,
              (SELECT count(*) FROM b3s_history.captures) AS captures,
              (SELECT count(*) FROM b3s_history.evidence_records) AS evidence,
              (SELECT count(*) FROM b3s_history.acquisition_attempts) AS attempts,
              (SELECT count(*) FROM b3s_history.evaluation_runs) AS evaluations,
              (SELECT count(*) FROM b3s_history.report_snapshots) AS reports
            """
        ).fetchone()
    assert tuple(counts) == (1, 1, 1, 1, 0, 0)

    with psycopg.connect(dsn) as conn:
        stored = conn.execute(
            """
            SELECT scan_runs.request_payload,
                   scan_runs.metadata ->> 'observation_hash' AS observation_hash,
                   captures.acquisition_summary
            FROM b3s_history.scan_runs
            JOIN b3s_history.captures ON captures.scan_run_id = scan_runs.id
            """
        ).fetchone()
    assert stored[0] == payload
    assert len(stored[1]) == 64
    assert stored[2]["state"] == "complete"

    upgraded = repository.import_report(_report_for_observation(payload))
    assert upgraded.status == "imported"
    assert upgraded.capture_id == first.capture_id
    with psycopg.connect(dsn) as conn:
        upgraded_counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM b3s_history.scan_runs) AS scans,
              (SELECT count(*) FROM b3s_history.captures) AS captures,
              (SELECT count(*) FROM b3s_history.evidence_records) AS evidence,
              (SELECT count(*) FROM b3s_history.evaluation_runs) AS evaluations,
              (SELECT count(*) FROM b3s_history.report_snapshots) AS reports
            """
        ).fetchone()
    assert tuple(upgraded_counts) == (1, 1, 1, 1, 1)

    changed = _observation()
    changed["capture_payload"]["raw_inputs"][0]["payload"]["text"] = "Changed"
    with pytest.raises(CaptureConflictError, match="different content"):
        repository.persist_capture_observation(changed)


def _report_for_observation(observation: dict) -> dict:
    evidence = observation["evidence_records"][0]
    tile = {
        "id": "P1",
        "estado": "ok",
        "evidencia": evidence["content"],
        "motivo": "",
        "contexto_requerido": "",
    }
    return {
        "id": observation["source_scan_id"],
        "brand_name": observation["brand_name"],
        "url": observation["url"],
        "created_at": observation["recorded_at"],
        "score": 1,
        "base_average": 1.0,
        "reliability_status": "usable",
        "not_detected": [],
        "limitations": observation["limitations"],
        "acquisition_gate": {"state": observation["acquisition_state"]},
        "attempts": observation["acquisition_attempts"],
        "acquisition_artifacts": observation["artifacts"],
        "blocks": [],
        "components": [
            {
                "key": "value_proposition",
                "label": "Propuesta de valor",
                "status": "scored",
                "score": 1,
                "scale": 10,
                "points": 1,
                "confidence": "high",
                "tile_profile": [tile],
            }
        ],
        "raw": {
            "source_run_id": observation["source_run_id"],
            "source_capture": {
                "observation_hash": canonical_json_hash(observation),
                "capture_hash": canonical_json_hash(
                    observation["capture_payload"]
                ),
            },
            "flow": {
                "candidate": {
                    "evidence_pack": {"evidence": observation["evidence_records"]},
                    "interpretation": {"blocks": {}},
                },
                "interpretation_debug": {
                    "prompt_version": "test-prompt-v1",
                    "gate_authority": "veto_only",
                },
            },
            "sv9": {
                "brand3_score": 1,
                "base_average": 1.0,
                "reliability_status": "usable",
                "result": {
                    "rubric_version": "baldosas-v3-1",
                    "model": "v3.1",
                    "evaluator_model": "test-model",
                    "components": {
                        "value_proposition": {
                            "component": "value_proposition",
                            "status": "scored",
                            "score": 1,
                            "scale": 10,
                            "points": 1,
                            "tile_profile": [tile],
                        }
                    },
                },
            },
        },
    }


def _observation() -> dict:
    return {
        "schema_version": "b3s-capture-observation-v1",
        "source_scan_id": "capture-only-postgres-1",
        "source_run_id": "provider-run-1",
        "brand_name": "Example",
        "url": "https://example.com",
        "observed_at": "2026-08-06T10:00:00Z",
        "recorded_at": "2026-08-06T10:00:01Z",
        "pipeline_version": "vault-incremental-capture-v1",
        "acquisition_state": "complete",
        "acquisition_summary": {"owned_pages": 1, "state": "spoofed"},
        "limitations": [],
        "capture_payload": {
            "raw_inputs": [{"source": "web", "payload": {"text": "Evidence"}}]
        },
        "evidence_records": [
            {
                "ref": "raw_inputs.0.chunk.0",
                "source": "web",
                "evidence_type": "owned_copy",
                "url": "https://example.com",
                "content": "Evidence",
                "confidence": "high",
                "metadata": {"source_class": "owned_copy"},
            }
        ],
        "acquisition_attempts": [
            {"provider": "web", "intent": "owned", "status": "success"}
        ],
        "artifacts": [],
        "metadata": {"mode": "incremental_refresh"},
    }
