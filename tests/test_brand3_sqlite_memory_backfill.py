from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from src.history.report_parser import parse_report
from src.services.brand3_sqlite_memory_backfill import (
    Brand3SQLiteBackfillError,
    build_brand3_sqlite_memory_validation,
    load_brand3_sqlite_sv9_reports,
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
    }
    domain = validation["domains"][0]
    assert domain["all_scan_preview"]["score_delta"] == 2
    assert domain["capture_preview"]["score_delta"] == 0
    assert validation["recovery_review_candidates"][0][
        "lane"
    ] == "all_scan"
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
        candidate["lane"]
        for candidate in validation["recovery_review_candidates"]
    } == {"all_scan", "capture"}


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
