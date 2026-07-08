"""SV9 persistence: self-contained store over the shared SQLite database.

Owns only sv9_* tables. The migration is idempotent and also runs via the main
SQLiteStore startup path (migrations/*.sql in filename order), so either entry
point leaves the schema ready.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import BRAND3_DB_PATH
from src.sv9.detection_trace import detection_fingerprint
from src.sv9.models import ComponentResult, Sv9ScanResult, TileVerdict

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_MIGRATION_PATHS = [
    _MIGRATIONS_DIR / "009_sv9.sql",
    _MIGRATIONS_DIR / "010_sv9_ranking.sql",
    _MIGRATIONS_DIR / "011_sv9_editorial_decisions.sql",
    _MIGRATIONS_DIR / "012_baldosas_v31.sql",
    _MIGRATIONS_DIR / "013_sv9_reliability.sql",
    _MIGRATIONS_DIR / "017_sv9_detection_runs.sql",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Sv9Store:
    def __init__(self, db_path: str = BRAND3_DB_PATH):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        for migration_path in _MIGRATION_PATHS:
            self.conn.executescript(migration_path.read_text(encoding="utf-8"))
        self._backfill_columns()
        self.conn.commit()

    def _backfill_columns(self) -> None:
        """Add columns introduced after the table was first created.

        The .sql migration is re-runnable but CREATE IF NOT EXISTS does not
        alter existing tables (same pattern as the main SQLiteStore).
        """
        for statement in (
            "ALTER TABLE sv9_scans ADD COLUMN evaluator_model TEXT",
            "ALTER TABLE sv9_scans ADD COLUMN executive_reading TEXT",
            # Baldosas v3.1 (migration 012): model label and tile-level data.
            "ALTER TABLE sv9_scans ADD COLUMN model TEXT",
            "ALTER TABLE sv9_scans ADD COLUMN reliability_status TEXT",
            "ALTER TABLE sv9_scans ADD COLUMN reliability_reason_codes_json TEXT",
            "ALTER TABLE sv9_component_scores ADD COLUMN tile_profile_json TEXT",
            "ALTER TABLE sv9_component_scores ADD COLUMN confidence TEXT",
            "ALTER TABLE sv9_component_scores ADD COLUMN veredicto TEXT",
            "ALTER TABLE sv9_component_scores ADD COLUMN evaluation_model TEXT",
        ):
            try:
                self.conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # column already exists

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Sv9Store":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # --- scans ---

    def save_scan(self, result: Sv9ScanResult) -> int:
        now = _utcnow()
        cursor = self.conn.execute(
            """
            INSERT INTO sv9_scans (
                brand_name, url, source_run_id, rubric_version, brand3_score,
                base_average, magnetism_capped, immediate_margin,
                most_painful_gap, needs_review, is_complete, evaluator_model,
                model, reliability_status, reliability_reason_codes_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.brand_name,
                result.url,
                result.source_run_id,
                result.rubric_version,
                result.brand3_score,
                result.base_average,
                int(result.magnetism_capped),
                result.immediate_margin,
                result.most_painful_gap,
                int(result.needs_review),
                int(result.is_complete),
                result.evaluator_model,
                result.model,
                result.reliability_status,
                json.dumps(result.reliability_reason_codes, ensure_ascii=False),
                now,
            ),
        )
        scan_id = int(cursor.lastrowid)
        for component in result.components.values():
            self._save_component(scan_id, component, result.rubric_version, now)
        self.conn.commit()
        return scan_id

    def _save_component(
        self,
        scan_id: int,
        component: ComponentResult,
        rubric_version: str,
        created_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO sv9_component_scores (
                scan_id, component, status, score, scale, points,
                tile_profile_json, confidence, veredicto, evaluation_model,
                detected_content, detection_mode,
                detection_confidence, evidence_json, message, error,
                rubric_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                component.component,
                component.status,
                component.score,
                component.scale,
                component.points,
                json.dumps([v.to_dict() for v in component.tile_profile], ensure_ascii=False),
                component.confidence,
                component.veredicto,
                component.evaluation_model,
                component.detected_content,
                component.detection_mode,
                component.detection_confidence,
                json.dumps(component.evidence, ensure_ascii=False),
                component.message,
                component.error,
                rubric_version,
                created_at,
            ),
        )

    def get_scan(self, scan_id: int) -> dict[str, Any] | None:
        scan = self.conn.execute(
            "SELECT * FROM sv9_scans WHERE id = ?", (scan_id,)
        ).fetchone()
        if scan is None:
            return None
        components = self.conn.execute(
            "SELECT * FROM sv9_component_scores WHERE scan_id = ? ORDER BY id ASC",
            (scan_id,),
        ).fetchall()
        payload = self._scan_row_to_dict(scan)
        payload["components"] = [self._component_row_to_dict(row) for row in components]
        return payload

    def get_scan_for_run(
        self,
        source_run_id: int,
        *,
        rubric_version: str | None = None,
    ) -> dict[str, Any] | None:
        where = "source_run_id = ?"
        params: list[Any] = [source_run_id]
        if rubric_version is not None:
            where += " AND rubric_version = ?"
            params.append(rubric_version)
        row = self.conn.execute(
            f"""
            SELECT id FROM sv9_scans
            WHERE {where}
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return self.get_scan(int(row["id"])) if row else None

    def list_scans(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sv9_scans ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._scan_row_to_dict(row) for row in rows]

    @staticmethod
    def _scan_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        raw = payload.get("reliability_reason_codes_json")
        try:
            payload["reliability_reason_codes"] = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            payload["reliability_reason_codes"] = []
        return payload

    @staticmethod
    def _component_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        json_fields = (
            ("tile_profile_json", "tile_profile"),
            ("rung_profile_json", "rung_profile"),  # legacy v2 scans
            ("evidence_json", "evidence"),
        )
        for json_field, target in json_fields:
            raw = payload.pop(json_field, None)
            try:
                payload[target] = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                payload[target] = []
        return payload

    # --- editorial layer (presentation only: never touches scores) ---

    def save_editorial(
        self,
        scan_id: int,
        *,
        component_messages: dict[str, str],
        executive_reading: str | None,
    ) -> None:
        for component, message in component_messages.items():
            self.conn.execute(
                """
                UPDATE sv9_component_scores SET message = ?
                WHERE scan_id = ? AND component = ?
                """,
                (message, scan_id, component),
            )
        if executive_reading is not None:
            self.conn.execute(
                "UPDATE sv9_scans SET executive_reading = ? WHERE id = ?",
                (executive_reading, scan_id),
            )
        self.conn.commit()

    # --- editorial calibration ---

    def save_editorial_decision(
        self,
        *,
        scan_id: int,
        component: str,
        decision: str,
        note: str | None,
        evaluator: str | None,
    ) -> None:
        now = _utcnow()
        self.conn.execute(
            """
            INSERT INTO sv9_editorial_decisions (
                scan_id, component, decision, note, evaluator, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id, component) DO UPDATE SET
                decision = excluded.decision,
                note = excluded.note,
                evaluator = excluded.evaluator,
                updated_at = excluded.updated_at
            """,
            (scan_id, component, decision, note, evaluator, now, now),
        )
        self.conn.commit()

    def list_editorial_decisions(self, scan_id: int) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM sv9_editorial_decisions
            WHERE scan_id = ?
            ORDER BY component ASC
            """,
            (scan_id,),
        ).fetchall()
        return {str(row["component"]): dict(row) for row in rows}

    # --- pinned Pass 1 detection ---

    def save_detection(self, run_id: int, payload: dict[str, Any]) -> None:
        fingerprint = detection_fingerprint(payload)
        self.record_detection_run(
            run_id,
            payload,
            source="cache_pin",
            fingerprint=fingerprint,
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sv9_detection_cache (run_id, payload_json, created_at)
            VALUES (?, ?, ?)
            """,
            (run_id, json.dumps(payload, ensure_ascii=False, default=str), _utcnow()),
        )
        self.conn.commit()

    def get_detection(self, run_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload_json FROM sv9_detection_cache WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

    def record_detection_run(
        self,
        run_id: int,
        payload: dict[str, Any],
        *,
        source: str,
        fingerprint: dict[str, Any] | None = None,
    ) -> int:
        fingerprint = fingerprint or detection_fingerprint(payload)
        cursor = self.conn.execute(
            """
            INSERT INTO sv9_detection_runs (
                run_id, source, tldr_hash, block_hashes_json, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source,
                fingerprint.get("tldr_hash"),
                json.dumps(fingerprint.get("block_hashes") or {}, ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False, default=str),
                _utcnow(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_detection_runs(
        self,
        run_id: int,
        *,
        source: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM sv9_detection_runs
            WHERE run_id = ?
        """
        params: list[Any] = [run_id]
        if source is not None:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        payloads: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            for field in ("block_hashes_json", "payload_json"):
                raw = payload.pop(field, None)
                try:
                    payload[field.removesuffix("_json")] = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload[field.removesuffix("_json")] = {}
            payloads.append(payload)
        return payloads

    # --- brand categories (ranking) ---

    def upsert_brand_category(
        self,
        domain: str,
        *,
        primary_category: str | None,
        secondary: list[str] | None = None,
        source: str = "confirmada",
        evaluador: str | None = None,
        exclude_from_ranking: bool = False,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sv9_brand_categories (
                domain, primary_category, secondary_json, source, evaluador,
                exclude_from_ranking, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain,
                primary_category,
                json.dumps(secondary or [], ensure_ascii=False),
                source,
                evaluador,
                int(exclude_from_ranking),
                _utcnow(),
            ),
        )
        self.conn.commit()

    def get_brand_categories(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM sv9_brand_categories").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = dict(row)
            try:
                payload["secondary"] = json.loads(payload.pop("secondary_json") or "[]")
            except json.JSONDecodeError:
                payload["secondary"] = []
            result[payload["domain"]] = payload
        return result

    # --- vision evidence (computed at SV9 time, cached per run) ---

    def save_visual_evidence(self, run_id: int, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sv9_visual_evidence (run_id, payload_json, created_at)
            VALUES (?, ?, ?)
            """,
            (run_id, json.dumps(payload, ensure_ascii=False, default=str), _utcnow()),
        )
        self.conn.commit()

    def get_visual_evidence(self, run_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload_json FROM sv9_visual_evidence WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

    # --- calibration ---

    def save_calibration_label(
        self,
        *,
        scan_id: int,
        url: str,
        component: str,
        score_ia: int,
        score_humano: int,
        motivo: str | None,
        flag_evidencia: bool,
        evaluador: str,
        rubric_version: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO sv9_calibration_labels (
                scan_id, url, component, score_ia, score_humano, delta,
                motivo, flag_evidencia, evaluador, rubric_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                url,
                component,
                score_ia,
                score_humano,
                score_humano - score_ia,
                motivo,
                int(flag_evidencia),
                evaluador,
                rubric_version,
                _utcnow(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_calibration_labels(
        self,
        *,
        component: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if component:
            rows = self.conn.execute(
                """
                SELECT * FROM sv9_calibration_labels
                WHERE component = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (component, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM sv9_calibration_labels ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- per-tile calibration (baldosas v3.1) ---

    def save_tile_calibration(
        self,
        *,
        scan_id: int,
        url: str,
        component: str,
        baldosa_id: str,
        estado_ia: str,
        estado_humano: str,
        motivo: str | None,
        evaluador: str,
        rubric_version: str,
    ) -> int:
        """One human verdict on one tile: confirms or corrects the IA state.

        Record schema (prompt técnico step 7): scan_id, componente, baldosa_id,
        estado_ia, estado_humano, motivo, evaluador, fecha.
        """
        cursor = self.conn.execute(
            """
            INSERT INTO sv9_tile_calibration (
                scan_id, url, component, baldosa_id, estado_ia, estado_humano,
                motivo, evaluador, rubric_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                url,
                component,
                baldosa_id,
                estado_ia,
                estado_humano,
                motivo,
                evaluador,
                rubric_version,
                _utcnow(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_tile_calibration(
        self,
        *,
        scan_id: int | None = None,
        baldosa_id: str | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if scan_id is not None:
            clauses.append("scan_id = ?")
            params.append(scan_id)
        if baldosa_id is not None:
            clauses.append("baldosa_id = ?")
            params.append(baldosa_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM sv9_tile_calibration {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def load_tile_profile(component_payload: dict[str, Any]) -> list[TileVerdict]:
    """Rebuild typed tile verdicts from a persisted component payload."""
    return [TileVerdict.from_dict(item) for item in component_payload.get("tile_profile") or []]
