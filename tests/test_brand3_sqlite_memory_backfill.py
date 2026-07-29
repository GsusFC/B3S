from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from scripts import import_brand3_sqlite_postgres
from src.history.report_parser import parse_report
from src.services.brand3_sqlite_history_import import (
    BRAND3_ARCHIVE_WORKSPACE_SLUG,
    load_brand3_archive_history_reports,
)
from src.services.brand3_sqlite_memory_backfill import (
    Brand3SQLiteBackfillError,
    build_brand3_sqlite_memory_validation,
    load_brand3_sqlite_capture_reports,
    load_brand3_sqlite_sv9_reports,
)
from src.services.evidence_scoring_recovery_review import (
    EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION,
)
from src.sv9.rubric import COMPONENTS, RUBRIC_VERSION, component_points


def test_adapter_is_read_only_and_emits_importable_reports(
    tmp_path: Path,
) -> None:
    database = tmp_path / "brand3.sqlite3"
    with sqlite3.connect(database) as conn:
        _create_schema(conn)
        _insert_capture(
            conn,
            source_run_id=10,
            scan_id=20,
            created_at="2026-06-01T08:00:00+00:00",
            target_state="ok",
        )
    before = _sha256(database)

    reports = load_brand3_sqlite_sv9_reports(database)

    after = _sha256(database)
    assert after == before
    assert len(reports) == 1
    assert reports[0]["id"] == "brand3-sqlite-sv9-20"
    assert reports[0]["raw"]["legacy_source"]["read_only"] is True
    assert (
        reports[0]["raw"]["flow"]["candidate"]["evidence_pack"][
            "evidence"
        ]
    )
    parsed = parse_report(reports[0])
    assert parsed.source_report_id == "brand3-sqlite-sv9-20"
    assert parsed.rubric_version == RUBRIC_VERSION
    assert len(parsed.components) == len(COMPONENTS)


def test_archive_import_plan_keeps_latest_evaluation_per_capture(
    tmp_path: Path,
) -> None:
    database = tmp_path / "brand3.sqlite3"
    with sqlite3.connect(database) as conn:
        _create_schema(conn)
        _insert_capture(
            conn,
            source_run_id=10,
            scan_id=20,
            created_at="2026-06-01T08:00:00+00:00",
            target_state="ok",
        )
        _insert_scan(
            conn,
            source_run_id=10,
            scan_id=21,
            created_at="2026-06-01T09:00:00+00:00",
            target_state="sin_evidencia",
        )
        _insert_capture(
            conn,
            source_run_id=11,
            scan_id=22,
            created_at="2026-06-02T08:00:00+00:00",
            target_state="ok",
        )
    before = _sha256(database)

    latest = load_brand3_sqlite_capture_reports(database)
    reports, plan = load_brand3_archive_history_reports(database)

    assert _sha256(database) == before
    assert [row["id"] for row in latest] == [
        "brand3-sqlite-sv9-21",
        "brand3-sqlite-sv9-22",
    ]
    assert [report.source_report_id for report in reports] == [
        "brand3-sqlite-capture-10",
        "brand3-sqlite-capture-11",
    ]
    assert reports[0].observed_at.isoformat() == (
        "2026-06-01T08:00:00+00:00"
    )
    assert reports[0].evaluated_at.isoformat() == (
        "2026-06-01T09:00:00+00:00"
    )
    assert plan["runtime_effect"] is False
    assert plan["authority"] is False
    assert plan["automatic_scoring_effect"] is False
    assert plan["mutates_source_archive"] is False
    assert plan["workspace"] == {
        "slug": "b3s-archive",
        "name": "B3S Legacy Archive",
        "operational_workspace": False,
    }
    assert plan["summary"]["archive_evaluation_count"] == 3
    assert plan["summary"]["capture_report_count"] == 2
    assert plan["summary"]["excluded_evaluator_revision_count"] == 1
    assert plan["summary"]["brand_count"] == 1
    assert len(plan["source_database"]["manifest_fingerprint"]) == 64
    assert len(plan["state_fingerprint"]) == 64


def test_archive_import_cli_defaults_to_dry_run(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "brand3.sqlite3"
    with sqlite3.connect(database) as conn:
        _create_schema(conn)
        _insert_capture(
            conn,
            source_run_id=10,
            scan_id=20,
            created_at="2026-06-01T08:00:00+00:00",
            target_state="ok",
        )

    assert import_brand3_sqlite_postgres.main([str(database)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "valid"
    assert payload["action"] == "dry_run"
    assert payload["applied"] is False
    assert payload["requires_explicit_apply"] is True
    assert payload["summary"]["capture_report_count"] == 1


def test_validation_separates_evaluator_repeats_from_captures(
    tmp_path: Path,
) -> None:
    database = tmp_path / "brand3.sqlite3"
    with sqlite3.connect(database) as conn:
        _create_schema(conn)
        _insert_capture(
            conn,
            source_run_id=10,
            scan_id=20,
            created_at="2026-06-01T08:00:00+00:00",
            target_state="ok",
        )
        _insert_scan(
            conn,
            source_run_id=10,
            scan_id=21,
            created_at="2026-06-01T09:00:00+00:00",
            target_state="sin_evidencia",
        )

    validation = build_brand3_sqlite_memory_validation(database)

    assert validation["runtime_effect"] is False
    assert validation["mutates_archive"] is False
    assert validation["promotion_ready"] is False
    assert validation["summary"] == {
        "archive_scan_count": 2,
        "archive_domain_count": 1,
        "archive_capture_count": 1,
        "evaluator_repeat_count": 1,
        "archive_accepted_evidence_count": 1,
        "evaluator_repeat_recovery_count": 1,
        "capture_recovery_count": 0,
        "capture_explicit_negative_conflict_count": 0,
        "bridge_domain_count": 0,
        "bridge_recovery_count": 0,
        "unmatched_current_domain_count": 0,
        "recovery_review_candidate_count": 1,
        "recovery_review_accepted_count": 0,
    }
    domain = validation["domains"][0]
    assert domain["all_scan_preview"]["score_delta"] == 2
    assert domain["all_scan_preview"]["reviewed_score_delta"] == 0
    assert domain["capture_preview"]["score_delta"] == 0
    candidate = validation["recovery_review_candidates"][0]
    assert {
        context["lane"] for context in candidate["contexts"]
    } == {"all_scan"}
    assert candidate["tile"]["tile_key"] == "magnetism.MG1"
    assert (
        validation["recovery_review"]["summary"]["pending_count"]
        == 1
    )
    assert (
        "recovered_tile_semantics_pending_review"
        in validation["promotion_blockers"]
    )


def test_distinct_captures_can_recover_a_later_blind_spot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "brand3.sqlite3"
    with sqlite3.connect(database) as conn:
        _create_schema(conn)
        _insert_capture(
            conn,
            source_run_id=10,
            scan_id=20,
            created_at="2026-06-01T08:00:00+00:00",
            target_state="ok",
        )
        _insert_capture(
            conn,
            source_run_id=11,
            scan_id=21,
            created_at="2026-06-02T08:00:00+00:00",
            target_state="sin_evidencia",
        )

    validation = build_brand3_sqlite_memory_validation(database)

    assert validation["summary"]["archive_capture_count"] == 2
    assert validation["summary"]["evaluator_repeat_count"] == 0
    assert validation["summary"]["capture_recovery_count"] == 1
    domain = validation["domains"][0]
    assert domain["capture_preview"]["score_delta"] == 2
    assert {
        context["lane"]
        for candidate in validation["recovery_review_candidates"]
        for context in candidate["contexts"]
    } == {"all_scan", "capture"}
    assert len(validation["recovery_review_candidates"]) == 1


def test_only_accepted_semantic_mapping_changes_reviewed_shadow_score(
    tmp_path: Path,
) -> None:
    database = tmp_path / "brand3.sqlite3"
    with sqlite3.connect(database) as conn:
        _create_schema(conn)
        _insert_capture(
            conn,
            source_run_id=10,
            scan_id=20,
            created_at="2026-06-01T08:00:00+00:00",
            target_state="ok",
        )
        _insert_scan(
            conn,
            source_run_id=10,
            scan_id=21,
            created_at="2026-06-01T09:00:00+00:00",
            target_state="sin_evidencia",
        )
    pending = build_brand3_sqlite_memory_validation(database)
    candidate = pending["recovery_review_candidates"][0]

    accepted = build_brand3_sqlite_memory_validation(
        database,
        recovery_review_events=[
            _review_event(candidate, decision="accepted")
        ],
    )
    disputed = build_brand3_sqlite_memory_validation(
        database,
        recovery_review_events=[
            _review_event(candidate, decision="disputed")
        ],
    )

    assert accepted["domains"][0]["all_scan_preview"][
        "reviewed_score_delta"
    ] == 2
    assert accepted["summary"]["recovery_review_accepted_count"] == 1
    assert accepted["domains"][0]["all_scan_preview"][
        "reviewed_accepted_recovery_count"
    ] == 1
    assert disputed["domains"][0]["all_scan_preview"][
        "reviewed_score_delta"
    ] == 0
    assert (
        "recovered_tile_semantics_not_accepted"
        in disputed["promotion_blockers"]
    )


def test_non_matching_legacy_rubric_is_not_converted(
    tmp_path: Path,
) -> None:
    database = tmp_path / "brand3.sqlite3"
    with sqlite3.connect(database) as conn:
        _create_schema(conn)
        _insert_capture(
            conn,
            source_run_id=10,
            scan_id=20,
            created_at="2026-06-01T08:00:00+00:00",
            target_state="ok",
            rubric_version="sv9-rubric-v1",
        )

    reports = load_brand3_sqlite_sv9_reports(database)

    assert reports == []


def test_missing_archive_tables_are_rejected(tmp_path: Path) -> None:
    database = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")

    with pytest.raises(
        Brand3SQLiteBackfillError,
        match="missing tables",
    ):
        load_brand3_sqlite_sv9_reports(database)


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_archive_import_cli_is_idempotent_and_workspace_isolated(
    tmp_path: Path,
    capsys,
) -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1":
        raise RuntimeError(
            "B3S_ALLOW_SCHEMA_DROP=1 is required for a disposable database"
        )
    database = tmp_path / "brand3.sqlite3"
    with sqlite3.connect(database) as conn:
        _create_schema(conn)
        _insert_capture(
            conn,
            source_run_id=10,
            scan_id=20,
            created_at="2026-06-01T08:00:00+00:00",
            target_state="ok",
        )
        _insert_scan(
            conn,
            source_run_id=10,
            scan_id=21,
            created_at="2026-06-01T09:00:00+00:00",
            target_state="sin_evidencia",
        )
        _insert_capture(
            conn,
            source_run_id=11,
            scan_id=22,
            created_at="2026-06-02T08:00:00+00:00",
            target_state="ok",
        )
    before = _sha256(database)
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    command = [
        str(database),
        "--database-url",
        dsn,
        "--apply",
    ]
    try:
        assert import_brand3_sqlite_postgres.main(command) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["status"] == "ok"
        assert first["workspace"]["slug"] == (
            BRAND3_ARCHIVE_WORKSPACE_SLUG
        )
        assert first["import"]["discovered"] == 2
        assert first["import"]["imported"] == 2
        assert first["import"]["unchanged"] == 0
        assert first["import"]["failed"] == 0

        assert import_brand3_sqlite_postgres.main(command) == 0
        second = json.loads(capsys.readouterr().out)
        assert second["status"] == "ok"
        assert second["import"]["imported"] == 0
        assert second["import"]["unchanged"] == 2
        assert second["import"]["failed"] == 0
        assert _sha256(database) == before

        repository = PostgresHistoryRepository(dsn)
        assert repository.get_current_brand_state("example.com") is None
        archived = repository.get_current_brand_state(
            "example.com",
            workspace_slug=BRAND3_ARCHIVE_WORKSPACE_SLUG,
        )
        assert archived is not None
        assert archived["source_report_id"] == (
            "brand3-sqlite-capture-11"
        )
        with sqlite3.connect(database) as conn:
            _insert_scan(
                conn,
                source_run_id=10,
                scan_id=23,
                created_at="2026-06-03T09:00:00+00:00",
                target_state="no",
            )
        assert import_brand3_sqlite_postgres.main(command) == 1
        changed = json.loads(capsys.readouterr().out)
        assert changed["status"] == "partial"
        assert changed["import"]["imported"] == 0
        assert changed["import"]["unchanged"] == 1
        assert changed["import"]["failed"] == 1
        assert changed["import"]["failures"][0][
            "source_report_id"
        ] == "brand3-sqlite-capture-10"
        with psycopg.connect(dsn) as conn:
            counts = conn.execute(
                """
                SELECT
                    (SELECT count(*) FROM b3s_history.captures)
                        AS captures,
                    (SELECT count(*) FROM b3s_history.evaluation_runs)
                        AS evaluations,
                    (SELECT count(*) FROM b3s_history.report_snapshots)
                        AS reports
                """
            ).fetchone()
        assert counts == (2, 2, 2)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY,
            brand_name TEXT,
            url TEXT,
            started_at TEXT
        );
        CREATE TABLE raw_inputs (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE features (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            dimension_name TEXT,
            feature_name TEXT,
            value REAL,
            raw_value TEXT,
            confidence REAL,
            source TEXT
        );
        CREATE TABLE evidence_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            url TEXT,
            quote TEXT,
            feature_name TEXT,
            dimension_name TEXT,
            confidence REAL,
            freshness_days REAL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sv9_scans (
            id INTEGER PRIMARY KEY,
            brand_name TEXT NOT NULL,
            url TEXT NOT NULL,
            source_run_id INTEGER,
            rubric_version TEXT NOT NULL,
            brand3_score INTEGER NOT NULL,
            base_average REAL,
            magnetism_capped INTEGER NOT NULL,
            evaluator_model TEXT,
            created_at TEXT NOT NULL,
            model TEXT,
            reliability_status TEXT
        );
        CREATE TABLE sv9_component_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL,
            component TEXT NOT NULL,
            status TEXT NOT NULL,
            score INTEGER NOT NULL,
            scale INTEGER NOT NULL,
            points INTEGER NOT NULL,
            detected_content TEXT,
            detection_mode TEXT,
            detection_confidence TEXT,
            evidence_json TEXT,
            message TEXT,
            error TEXT,
            tile_profile_json TEXT,
            veredicto TEXT,
            evaluation_model TEXT
        );
        """
    )


def _insert_capture(
    conn: sqlite3.Connection,
    *,
    source_run_id: int,
    scan_id: int,
    created_at: str,
    target_state: str,
    rubric_version: str = RUBRIC_VERSION,
) -> None:
    quote = "The archived evidence remains reproducible."
    conn.execute(
        """
        INSERT INTO runs (id, brand_name, url, started_at)
        VALUES (?, 'Example', 'https://example.com', ?)
        """,
        (source_run_id, created_at),
    )
    conn.execute(
        """
        INSERT INTO raw_inputs (
            run_id, source, payload_json, created_at
        ) VALUES (?, 'web', ?, ?)
        """,
        (
            source_run_id,
            json.dumps(
                {
                    "url": "https://example.com",
                    "content": quote,
                }
            ),
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO evidence_items (
            run_id, source, url, quote, feature_name,
            dimension_name, confidence, freshness_days, created_at
        ) VALUES (
            ?, 'web', 'https://example.com', ?, 'message',
            'coherencia', 0.95, 0, ?
        )
        """,
        (source_run_id, quote, created_at),
    )
    _insert_scan(
        conn,
        source_run_id=source_run_id,
        scan_id=scan_id,
        created_at=created_at,
        target_state=target_state,
        rubric_version=rubric_version,
    )


def _insert_scan(
    conn: sqlite3.Connection,
    *,
    source_run_id: int,
    scan_id: int,
    created_at: str,
    target_state: str,
    rubric_version: str = RUBRIC_VERSION,
) -> None:
    quote = "The archived evidence remains reproducible."
    component_rows = []
    total = 0
    for component_key, spec in COMPONENTS.items():
        profile = []
        for tile in spec["tiles"]:
            state = (
                target_state
                if component_key == "magnetism"
                and tile["id"] == "MG1"
                else "no"
            )
            profile.append(
                {
                    "id": tile["id"],
                    "estado": state,
                    "evidencia": quote if state == "ok" else "",
                    "motivo": (
                        "" if state == "ok" else "not observed"
                    ),
                    "contexto_requerido": "",
                }
            )
        score = sum(
            1 for row in profile if row["estado"] == "ok"
        )
        points = component_points(component_key, score)
        total += points
        component_rows.append(
            (
                scan_id,
                component_key,
                "scored",
                score,
                int(spec["scale"]),
                points,
                "",
                "sv9",
                "high",
                "[]",
                "",
                None,
                json.dumps(profile),
                "",
                "test-model",
            )
        )
    conn.execute(
        """
        INSERT INTO sv9_scans (
            id, brand_name, url, source_run_id, rubric_version,
            brand3_score, base_average, magnetism_capped,
            evaluator_model, created_at, model, reliability_status
        ) VALUES (
            ?, 'Example', 'https://example.com', ?, ?, ?, 0, 0,
            'test-model', ?, 'v3.1', 'shadow'
        )
        """,
        (
            scan_id,
            source_run_id,
            rubric_version,
            total,
            created_at,
        ),
    )
    conn.executemany(
        """
        INSERT INTO sv9_component_scores (
            scan_id, component, status, score, scale, points,
            detected_content, detection_mode, detection_confidence,
            evidence_json, message, error, tile_profile_json,
            veredicto, evaluation_model
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        component_rows,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_event(
    candidate: dict,
    *,
    decision: str,
) -> dict:
    return {
        "schema_version": (
            EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION
        ),
        "case_id": candidate["case_id"],
        "candidate_fingerprint": candidate[
            "candidate_fingerprint"
        ],
        "event_id": f"{candidate['case_id']}-{decision}-1",
        "sequence": 1,
        "previous_event_id": None,
        "decision": decision,
        "reviewer_id": "gsus",
        "rationale": "Manual semantic mapping review.",
        "reviewed_at": "2026-07-29T17:00:00+02:00",
        "runtime_effect": False,
        "authority": False,
    }
