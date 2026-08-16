"""SQLite scoring observability store for B3S reports."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


COMPONENT_MAX_SCORE = {
    "mission": 5,
    "vision": 5,
    "values": 5,
    "attributes": 5,
    "core_purpose": 10,
    "value_proposition": 10,
    "personality": 10,
    "brand_idea": 10,
    "magnetism": 10,
    "coherencia": 10,
}


def db_path() -> Path:
    return Path(os.environ.get("B3S_SCORING_DB_PATH", "data/b3s_scoring.sqlite3"))


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    own_conn = conn is None
    conn = conn or _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                brand_name TEXT NOT NULL,
                domain TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                pipeline_version TEXT NOT NULL,
                rubric_version TEXT NOT NULL,
                score REAL,
                detected_count INTEGER,
                block_count INTEGER,
                reliability_status TEXT,
                source_run_id TEXT,
                not_detected_json TEXT NOT NULL,
                limitations_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS components (
                run_id TEXT NOT NULL,
                component_key TEXT NOT NULL,
                label TEXT NOT NULL,
                status TEXT NOT NULL,
                score REAL,
                max_score INTEGER,
                confidence TEXT,
                summary TEXT NOT NULL,
                verdict TEXT NOT NULL,
                ref_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, component_key),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tiles (
                run_id TEXT NOT NULL,
                component_key TEXT NOT NULL,
                tile_id TEXT NOT NULL,
                state TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                evidence_text TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (run_id, component_key, tile_id),
                FOREIGN KEY (run_id, component_key)
                    REFERENCES components(run_id, component_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                component_key TEXT NOT NULL,
                source_type TEXT NOT NULL,
                url TEXT NOT NULL,
                ref TEXT NOT NULL DEFAULT '',
                text_excerpt TEXT NOT NULL DEFAULT '',
                UNIQUE (run_id, component_key, url, ref),
                FOREIGN KEY (run_id, component_key)
                    REFERENCES components(run_id, component_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                component_key TEXT NOT NULL,
                tile_id TEXT NOT NULL,
                ai_state TEXT NOT NULL,
                human_state TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id, component_key, tile_id)
                    REFERENCES tiles(run_id, component_key, tile_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_runs_domain_created ON runs(domain, created_at);
            CREATE INDEX IF NOT EXISTS idx_components_key ON components(component_key);
            CREATE INDEX IF NOT EXISTS idx_tiles_component_state ON tiles(component_key, state);
            CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source_type);
            """
        )
        conn.commit()
    finally:
        if own_conn:
            conn.close()


def record_report(report: dict[str, Any]) -> None:
    if not report.get("id"):
        return
    with _connect() as conn:
        ensure_schema(conn)
        _record_report(conn, report)


def backfill_reports() -> int:
    from web.report_store import load_report, list_reports

    count = 0
    with _connect() as conn:
        ensure_schema(conn)
        for row in list_reports():
            report = load_report(str(row.get("id") or ""))
            if not report:
                continue
            _record_report(conn, report)
            count += 1
    return count


def dashboard() -> dict[str, Any]:
    with _connect() as conn:
        ensure_schema(conn)
        return {
            "db_path": str(db_path()),
            "stats": _one(
                conn,
                """
                SELECT
                    COUNT(*) AS run_count,
                    COUNT(DISTINCT domain) AS brand_count,
                    ROUND(AVG(score), 1) AS avg_score,
                    SUM(CASE WHEN score < 70 THEN 1 ELSE 0 END) AS runs_below_70
                FROM runs
                """,
            ),
            "recent_runs": _all(
                conn,
                """
                SELECT id, brand_name, domain, url, created_at, score, detected_count,
                       block_count, reliability_status
                FROM runs
                ORDER BY created_at DESC
                LIMIT 20
                """,
            ),
            "component_stats": _all(
                conn,
                """
                SELECT component_key, label, COUNT(*) AS samples,
                       ROUND(AVG(score), 1) AS avg_score,
                       SUM(CASE WHEN status != 'scored' THEN 1 ELSE 0 END) AS not_detected,
                       ROUND(AVG(ref_count), 1) AS avg_refs
                FROM components
                GROUP BY component_key, label
                ORDER BY not_detected DESC, avg_score ASC
                """,
            ),
            "tile_stats": _all(
                conn,
                """
                SELECT component_key, tile_id, state, COUNT(*) AS count
                FROM tiles
                GROUP BY component_key, tile_id, state
                ORDER BY count DESC, component_key, tile_id, state
                LIMIT 60
                """,
            ),
            "evidence_stats": _all(
                conn,
                """
                SELECT source_type, COUNT(*) AS count, COUNT(DISTINCT url) AS unique_urls
                FROM evidence
                GROUP BY source_type
                ORDER BY count DESC
                """,
            ),
            "score_gaps": _all(
                conn,
                """
                SELECT r.brand_name, r.id AS run_id, r.score AS run_score,
                       c.component_key, c.label, c.status, c.score, c.max_score,
                       c.ref_count
                FROM components c
                JOIN runs r ON r.id = c.run_id
                WHERE c.status != 'scored'
                   OR c.score IS NULL
                   OR c.score <= (c.max_score * 0.5)
                ORDER BY r.created_at DESC, c.max_score DESC, c.score ASC
                LIMIT 80
                """,
            ),
            "repeat_domains": _all(
                conn,
                """
                SELECT domain, COUNT(*) AS runs, MIN(score) AS min_score,
                       MAX(score) AS max_score, ROUND(AVG(score), 1) AS avg_score
                FROM runs
                GROUP BY domain
                HAVING COUNT(*) > 1
                ORDER BY runs DESC, (MAX(score) - MIN(score)) DESC
                LIMIT 20
                """,
            ),
        }


def _record_report(conn: sqlite3.Connection, report: dict[str, Any]) -> None:
    run_id = str(report["id"])
    url = str(report.get("url") or "")
    domain = _domain_key(url)
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    raw_sv9 = raw.get("sv9") if isinstance(raw.get("sv9"), dict) else {}
    result = raw_sv9.get("result") if isinstance(raw_sv9.get("result"), dict) else {}

    conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    conn.execute(
        """
        INSERT INTO runs (
            id, brand_name, domain, url, created_at, pipeline_version, rubric_version,
            score, detected_count, block_count, reliability_status, source_run_id,
            not_detected_json, limitations_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            str(report.get("brand_name") or ""),
            domain,
            url,
            str(report.get("created_at") or ""),
            str(raw.get("schema_version") or "b3s-report-json"),
            str(result.get("rubric_version") or result.get("model") or raw_sv9.get("model") or "sv9"),
            _num(report.get("score")),
            _int(report.get("detected_count")),
            _int(report.get("block_count")),
            str(report.get("reliability_status") or raw_sv9.get("reliability_status") or ""),
            str(raw.get("source_run_id") or report.get("source_run_id") or ""),
            json.dumps(report.get("not_detected") or [], ensure_ascii=False),
            json.dumps(report.get("limitations") or [], ensure_ascii=False),
        ),
    )

    components = report.get("components") if isinstance(report.get("components"), list) else []
    raw_components = raw_sv9.get("components") if isinstance(raw_sv9.get("components"), dict) else {}
    for component in components:
        key = str(component.get("key") or "")
        if not key:
            continue
        raw_component = raw_components.get(key) if isinstance(raw_components.get(key), dict) else {}
        refs = ((component.get("block") or {}).get("refs") or []) if isinstance(component.get("block"), dict) else []
        conn.execute(
            """
            INSERT INTO components (
                run_id, component_key, label, status, score, max_score, confidence,
                summary, verdict, ref_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                key,
                str(component.get("label") or key),
                str(component.get("status") or raw_component.get("status") or ""),
                _num(raw_component.get("score", component.get("score"))),
                COMPONENT_MAX_SCORE.get(key, 10),
                str(component.get("confidence") or raw_component.get("confidence") or ""),
                str(component.get("resumen") or ""),
                str(component.get("veredicto") or ""),
                len(refs),
            ),
        )
        _insert_tiles(conn, run_id, key, raw_component)
        _insert_evidence(conn, run_id, key, domain, refs)
    conn.commit()


def _insert_tiles(conn: sqlite3.Connection, run_id: str, component_key: str, raw_component: dict[str, Any]) -> None:
    state_map = {
        "ok": raw_component.get("lit_tiles") or [],
        "no": raw_component.get("off_tiles") or [],
        "sin_evidencia": raw_component.get("blind_spot_tiles") or [],
    }
    for state, tile_ids in state_map.items():
        for tile_id in tile_ids:
            conn.execute(
                """
                INSERT INTO tiles (run_id, component_key, tile_id, state)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, component_key, str(tile_id), state),
            )


def _insert_evidence(
    conn: sqlite3.Connection,
    run_id: str,
    component_key: str,
    domain: str,
    refs: list[dict[str, Any]],
) -> None:
    for ref in refs:
        url = str(ref.get("url") or "")
        if not url:
            continue
        ref_id = str(ref.get("ref") or ref.get("id") or "")
        excerpt = str(ref.get("text") or ref.get("excerpt") or ref.get("snippet") or "")[:500]
        source_type = "owned" if _domain_key(url) == domain else "external"
        conn.execute(
            """
            INSERT OR IGNORE INTO evidence (
                run_id, component_key, source_type, url, ref, text_excerpt
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, component_key, source_type, url, ref_id, excerpt),
        )


def _domain_key(value: str) -> str:
    candidate = (value or "").strip().lower()
    if not candidate:
        return ""
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    host = (parsed.hostname or candidate).strip(".")
    return host.removeprefix("www.")


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _one(conn: sqlite3.Connection, sql: str) -> dict[str, Any]:
    row = conn.execute(sql).fetchone()
    return dict(row) if row else {}


def _all(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql).fetchall()]
