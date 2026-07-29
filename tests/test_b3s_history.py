from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import pytest

from scripts.import_b3s_reports_postgres import (
    _run_evidence_ledger_shadow_backfill,
    dry_run_summary,
)
from src.history.models import ReportConflictError, ReportImportError
from src.history.report_parser import canonical_json_hash, normalize_domain, parse_report
from src.services.evidence_memory_adjudication import (
    EvidenceMemoryAdjudicationCommand,
    EvidenceMemoryAdjudicationConflictError,
)
from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)


def test_report_parser_preserves_observed_and_evaluation_history() -> None:
    payload = _report("scan-old", "2026-07-01T10:00:00+02:00", score=61)

    parsed = parse_report(payload)

    assert parsed.source_report_id == "scan-old"
    assert parsed.canonical_domain == "example.com"
    assert parsed.observed_at.isoformat() == "2026-07-01T08:00:00+00:00"
    assert parsed.pipeline_version == "sv9-flow-sv9-shadow-eval-v1"
    assert parsed.rubric_version == "baldosas-v3-1"
    assert parsed.prompt_version == "sv9-flow-brand-interpretation-v1"
    assert parsed.score == 61
    assert parsed.report_hash == canonical_json_hash(payload)
    assert len(parsed.evidence_records) == 1
    assert len(parsed.block_interpretations) == 1
    assert len(parsed.components) == 1


def test_report_parser_rejects_duplicate_or_unknown_evidence_refs() -> None:
    duplicate = _report("scan-duplicate", "2026-07-01T08:00:00Z")
    evidence = duplicate["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"]
    evidence.append(deepcopy(evidence[0]))
    with pytest.raises(ReportImportError, match="duplicate evidence ref"):
        parse_report(duplicate)

    unknown = _report("scan-unknown", "2026-07-01T08:00:00Z")
    unknown["blocks"][0]["refs"][0]["ref"] = "web.missing"
    with pytest.raises(ReportImportError, match="references unknown evidence"):
        parse_report(unknown)


def test_report_hash_is_stable_across_mapping_order() -> None:
    assert canonical_json_hash({"a": 1, "b": {"c": 2}}) == canonical_json_hash(
        {"b": {"c": 2}, "a": 1}
    )


def test_dry_run_summary_counts_historical_units() -> None:
    reports = [
        parse_report(_report("scan-1", "2026-07-01T08:00:00Z")),
        parse_report(_report("scan-2", "2026-07-02T08:00:00Z", score=74)),
    ]

    summary = dry_run_summary(reports)

    assert summary["status"] == "valid"
    assert summary["reports"] == 2
    assert summary["brands"] == 1
    assert summary["evidence_records"] == 2
    assert summary["component_evaluations"] == 2
    assert summary["tile_verdicts"] == 2


def test_normalize_domain_accepts_url_or_domain() -> None:
    assert normalize_domain("https://www.Example.com/path") == "example.com"
    assert normalize_domain("example.com") == "example.com"


def test_repository_connections_use_a_bounded_timeout() -> None:
    from src.history.repository import PostgresHistoryRepository

    captured = {}

    def connect(dsn, **kwargs):
        captured.update({"dsn": dsn, **kwargs})
        return object()

    repository = PostgresHistoryRepository("postgresql://example.test/b3s", connect=connect)

    repository._connect()

    assert captured["connect_timeout"] == 5
    assert captured["dsn"] == "postgresql://example.test/b3s"


def test_evidence_ledger_backfill_is_a_noop_when_shadow_is_disabled(monkeypatch) -> None:
    from src.history.repository import PostgresHistoryRepository

    def connect(*_args, **_kwargs):
        raise AssertionError("disabled shadow must not connect")

    repository = PostgresHistoryRepository(
        "postgresql://example.test/b3s",
        connect=connect,
    )
    monkeypatch.setenv("B3S_EVIDENCE_LEDGER_MODE", "disabled")

    result = repository.rebuild_evidence_ledger_shadows()

    assert result == {
        "mode": "disabled",
        "runtime_effect": False,
        "workspace_slug": "b3s",
        "brands_discovered": 0,
        "rebuilt": 0,
        "failed": 0,
        "failed_domains": [],
    }


def test_evidence_ledger_backfill_failure_cannot_fail_a_release() -> None:
    class Repository:
        @staticmethod
        def rebuild_evidence_ledger_shadows(*, workspace_slug):
            raise RuntimeError(f"unavailable:{workspace_slug}")

    result = _run_evidence_ledger_shadow_backfill(
        Repository(),
        workspace_slug="b3s",
    )

    assert result == {
        "mode": "shadow",
        "runtime_effect": False,
        "workspace_slug": "b3s",
        "status": "failed",
        "error": "RuntimeError: unavailable:b3s",
    }


def test_shadow_ledger_failure_isolated_from_authoritative_import(monkeypatch) -> None:
    from src.history.repository import PostgresHistoryRepository

    class Connection:
        savepoint_count = 0

        @contextmanager
        def transaction(self):
            self.savepoint_count += 1
            yield

    repository = PostgresHistoryRepository("postgresql://example.test/b3s")
    connection = Connection()
    monkeypatch.setenv("B3S_EVIDENCE_LEDGER_MODE", "shadow")

    def fail(*_args):
        raise RuntimeError("experimental projection failed")

    monkeypatch.setattr(repository, "_rebuild_evidence_ledger_shadow", fail)

    rebuilt = repository._rebuild_evidence_ledger_shadow_safely(
        connection, uuid4(), uuid4()
    )

    assert connection.savepoint_count == 1
    assert rebuilt is False


def test_schema_drop_requires_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("B3S_ALLOW_SCHEMA_DROP", raising=False)

    with pytest.raises(RuntimeError, match="B3S_ALLOW_SCHEMA_DROP=1"):
        _require_schema_drop_opt_in()


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_postgres_history_import_is_idempotent_and_selects_latest_capture(
    monkeypatch,
) -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    _require_schema_drop_opt_in()
    monkeypatch.setenv("B3S_EVIDENCE_LEDGER_MODE", "shadow")
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    repository = PostgresHistoryRepository(dsn)
    try:
        assert repository.migrate() == [
            "001_history_v1.sql",
            "002_evidence_stability.sql",
            "003_evidence_ledger_shadow.sql",
            "004_evidence_memory_adjudications.sql",
        ]
        assert repository.migrate() == []

        older = _report("scan-older", "2026-07-01T08:00:00Z", score=61)
        newer = _report("scan-newer", "2026-07-02T08:00:00Z", score=77)
        old_outcome = repository.import_report(older)
        assert old_outcome.status == "imported"
        assert repository.import_report(older).status == "unchanged"
        assert repository.import_report(newer).status == "imported"

        current = repository.get_current_brand_state("example.com")
        assert current is not None
        assert current["source_report_id"] == "scan-newer"
        assert current["score"] == 77

        history = repository.list_brand_history("https://example.com")
        assert [row["source_report_id"] for row in history] == ["scan-newer", "scan-older"]
        assert repository.get_current_report("example.com")["id"] == "scan-newer"
        assert repository.get_report_payload("scan-older") == older
        assert [row["id"] for row in repository.list_report_summaries()] == [
            "scan-newer",
            "scan-older",
        ]
        assert [row["id"] for row in repository.list_report_payloads_for_domain("example.com")] == [
            "scan-newer",
            "scan-older",
        ]
        assert len(repository.list_evaluation_revisions(old_outcome.capture_id)) == 1
        ledger = repository.get_evidence_ledger_shadow("example.com")
        assert ledger is not None
        assert ledger["runtime_effect"] is False
        assert ledger["summary"]["state_counts"] == {"validation_candidate": 1}
        assert ledger["entries"][0]["observation_count"] == 2
        assert repository.storage_counts() == {
            "workspaces": 1,
            "brands": 1,
            "scan_runs": 2,
            "captures": 2,
            "evidence_records": 2,
            "evaluation_runs": 2,
            "block_interpretations": 2,
            "component_evaluations": 2,
            "tile_verdicts": 2,
            "report_snapshots": 2,
            "capture_fingerprints": 2,
            "evaluation_comparisons": 2,
            "brand_canonical_selections": 1,
            "evidence_ledger_shadow_states": 1,
            "evidence_ledger_shadow_entries": 1,
            "evidence_ledger_shadow_observations": 2,
            "evidence_memory_adjudication_events": 0,
        }

        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM b3s_history.evidence_ledger_shadow_entries")
            conn.execute("DELETE FROM b3s_history.evidence_ledger_shadow_states")
        assert repository.get_evidence_ledger_shadow("example.com") is None

        backfill = repository.rebuild_evidence_ledger_shadows()

        assert backfill == {
            "mode": "shadow",
            "runtime_effect": False,
            "workspace_slug": "b3s",
            "brands_discovered": 1,
            "rebuilt": 1,
            "failed": 0,
            "failed_domains": [],
        }
        rebuilt_ledger = repository.get_evidence_ledger_shadow("example.com")
        assert rebuilt_ledger is not None
        assert rebuilt_ledger["state_fingerprint"] == ledger["state_fingerprint"]

        concurrent = _report("scan-concurrent", "2026-07-03T08:00:00Z", score=79)
        barrier = Barrier(2)

        def import_concurrently() -> str:
            barrier.wait()
            return repository.import_report(concurrent).status

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(import_concurrently) for _ in range(2)]
            statuses = sorted(future.result() for future in futures)

        assert statuses == ["imported", "unchanged"]

        nul_payload = _report("scan-nul", "2026-06-30T08:00:00Z", score=55)
        nul_content = "wOF2\x00binary-font-data"
        nul_payload["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"][0]["content"] = nul_content
        repository.import_report(nul_payload)
        with psycopg.connect(dsn) as conn:
            stored = conn.execute(
                """
                SELECT evidence_records.content, evidence_records.content_raw,
                       report_snapshots.payload_raw
                FROM b3s_history.evidence_records
                JOIN b3s_history.captures ON captures.id = evidence_records.capture_id
                JOIN b3s_history.evaluation_runs ON evaluation_runs.capture_id = captures.id
                JOIN b3s_history.report_snapshots
                    ON report_snapshots.evaluation_run_id = evaluation_runs.id
                WHERE report_snapshots.source_report_id = 'scan-nul'
                """
            ).fetchone()
        assert stored[0] == "wOF2\ufffdbinary-font-data"
        assert bytes(stored[1]) == nul_content.encode("utf-8")
        assert b"\\u0000" in bytes(stored[2])

        conflicting = deepcopy(older)
        conflicting["score"] = 99
        with pytest.raises(ReportConflictError, match="different content"):
            repository.import_report(conflicting)

        subject_id = build_evidence_memory_identity_v2(
            [older, newer]
        )["entries"][0]["evidence_id"]
        accepted_command = _adjudication_command(
            subject_id,
            decision="accepted",
            expected_current_event_id=None,
            key_hash="1" * 64,
            fingerprint="2" * 64,
        )
        accepted, replayed = repository.append_evidence_memory_adjudication(
            "example.com",
            accepted_command,
        )
        assert replayed is False
        assert accepted["decision"] == "accepted"
        assert accepted["runtime_effect"] is False
        assert accepted["authority"] is False
        replay, replayed = repository.append_evidence_memory_adjudication(
            "example.com",
            accepted_command,
        )
        assert replayed is True
        assert replay["id"] == accepted["id"]

        stale_command = _adjudication_command(
            subject_id,
            decision="disputed",
            expected_current_event_id=None,
            key_hash="3" * 64,
            fingerprint="4" * 64,
        )
        with pytest.raises(
            EvidenceMemoryAdjudicationConflictError,
            match="changed after it was read",
        ):
            repository.append_evidence_memory_adjudication(
                "example.com",
                stale_command,
            )

        revoked, replayed = repository.append_evidence_memory_adjudication(
            "example.com",
            _adjudication_command(
                subject_id,
                decision="revoked",
                expected_current_event_id=accepted["id"],
                key_hash="5" * 64,
                fingerprint="6" * 64,
            ),
        )
        assert replayed is False
        assert revoked["supersedes_event_id"] == accepted["id"]
        old_replay, replayed = repository.append_evidence_memory_adjudication(
            "example.com",
            accepted_command,
        )
        assert replayed is True
        assert old_replay["effective_state"] == "superseded"
        journal = repository.list_evidence_memory_adjudications(
            "example.com"
        )
        assert journal["total"] == 2
        assert [event["effective_state"] for event in journal["events"]] == [
            "revoked",
            "superseded",
        ]
        assert journal["current"] == [revoked]

        adjudication_barrier = Barrier(2)
        competing_commands = (
            _adjudication_command(
                subject_id,
                decision="accepted",
                expected_current_event_id=revoked["id"],
                key_hash="7" * 64,
                fingerprint="8" * 64,
            ),
            _adjudication_command(
                subject_id,
                decision="disputed",
                expected_current_event_id=revoked["id"],
                key_hash="9" * 64,
                fingerprint="a" * 64,
            ),
        )

        def adjudicate_concurrently(command) -> str:
            adjudication_barrier.wait()
            try:
                repository.append_evidence_memory_adjudication(
                    "example.com",
                    command,
                )
            except EvidenceMemoryAdjudicationConflictError:
                return "conflict"
            return "created"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(adjudicate_concurrently, command)
                for command in competing_commands
            ]
            adjudication_outcomes = sorted(
                future.result() for future in futures
            )

        assert adjudication_outcomes == ["conflict", "created"]
        assert repository.list_evidence_memory_adjudications(
            "example.com"
        )["total"] == 3
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")


def _require_schema_drop_opt_in() -> None:
    if os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1":
        raise RuntimeError(
            "PostgreSQL integration tests drop b3s_history; set B3S_ALLOW_SCHEMA_DROP=1 only for a disposable database"
        )


def _adjudication_command(
    subject_id: str,
    *,
    decision: str,
    expected_current_event_id: str | None,
    key_hash: str,
    fingerprint: str,
) -> EvidenceMemoryAdjudicationCommand:
    return EvidenceMemoryAdjudicationCommand(
        subject_id=subject_id,
        decision=decision,
        expected_current_event_id=expected_current_event_id,
        reviewer="reviewer@example.com",
        reason_code="identity_reviewed",
        rationale="The reviewer checked the evidence identity.",
        evaluator_version="manual-review-v1",
        actor_id="test-client",
        idempotency_key_hash=key_hash,
        request_fingerprint=fingerprint,
    )


def _report(
    report_id: str,
    created_at: str,
    *,
    score: int = 61,
) -> dict:
    evidence_record = {
        "ref": "web.home.0",
        "source": "web",
        "evidence_type": "owned_copy.homepage",
        "content": "We help finance teams close their books in one day.",
        "url": "https://example.com",
        "confidence": "high",
        "metadata": {"source_class": "owned_copy"},
    }
    tile = {
        "id": "VP1",
        "estado": "ok",
        "evidencia": "We help finance teams close their books in one day.",
        "motivo": "",
        "contexto_requerido": "",
    }
    raw_component = {
        "component": "value_proposition",
        "status": "scored",
        "score": 1,
        "scale": 10,
        "points": 1,
        "confidence": "high",
        "detected_content": "Finance teams close their books in one day.",
        "detection_mode": "sv9_flow",
        "detection_limitations": [],
        "evidence_source_summary": {"owned_copy": 1, "total": 1},
        "tile_profile": [tile],
    }
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://www.example.com",
        "created_at": created_at,
        "score": score,
        "base_average": 6.1,
        "reliability_status": "usable",
        "not_detected": [],
        "limitations": [],
        "acquisition_gate": {"state": "pass"},
        "attempts": [],
        "acquisition_artifacts": [],
        "blocks": [
            {
                "name": "value_proposition",
                "detected": True,
                "content": "Finance teams close their books in one day.",
                "confidence": "high",
                "coverage_status": "evidence",
                "provenance_source": "llm_and_gate",
                "refs": [
                    {
                        "ref": "web.home.0",
                        "url": "https://example.com",
                        "snippet": evidence_record["content"],
                    }
                ],
            }
        ],
        "components": [
            {
                "key": "value_proposition",
                "label": "Propuesta de valor",
                "status": "scored",
                "score": 1,
                "scale": 10,
                "points": 1,
                "confidence": "high",
                "resumen": "Finance teams close their books in one day.",
                "veredicto": "La propuesta es concreta.",
                "message": "La propuesta identifica un resultado operativo.",
                "tile_profile": [tile],
            }
        ],
        "raw": {
            "schema_version": "sv9-flow-sv9-shadow-eval-v1",
            "source_run_id": 123,
            "flow": {
                "candidate": {
                    "evidence_pack": {"evidence": [evidence_record]},
                    "interpretation": {"blocks": {}},
                },
                "interpretation_debug": {
                    "prompt_version": "sv9-flow-brand-interpretation-v1",
                    "gate_authority": "veto_only",
                },
            },
            "sv9": {
                "brand3_score": score,
                "base_average": 6.1,
                "reliability_status": "usable",
                "result": {
                    "rubric_version": "baldosas-v3-1",
                    "model": "v3.1",
                    "evaluator_model": "test-model",
                    "components": {"value_proposition": raw_component},
                },
            },
        },
    }
