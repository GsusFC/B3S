"""Synchronous PostgreSQL repository for immutable B3S history."""

from __future__ import annotations

import hashlib
from importlib import resources
from threading import Lock
from typing import Any, Callable
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.history.models import HistoricalReport, ImportOutcome, ReportConflictError
from src.history.report_parser import canonical_json_bytes, normalize_domain, parse_report

_ID_NAMESPACE = UUID("3ef1b80c-e7b7-4fb3-95ad-fb9e03c59d52")
_SCHEMA = "b3s_history"
_CONNECT_TIMEOUT_SECONDS = 5


class PostgresHistoryRepository:
    """Persist and query brand captures without overwriting observed history."""

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[..., Any] = psycopg.connect,
    ) -> None:
        if not str(dsn or "").strip():
            raise ValueError("B3S_DATABASE_URL is required")
        self.dsn = dsn
        self._connect_fn = connect
        self._migrated = False
        self._migration_lock = Lock()

    def migrate(self) -> list[str]:
        """Apply immutable SQL migrations and reject edited applied files."""

        applied: list[str] = []
        with self._connect() as conn:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_SCHEMA}.schema_migrations (
                    version text PRIMARY KEY,
                    filename text NOT NULL,
                    checksum text NOT NULL CHECK (length(checksum) = 64),
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            for filename, sql_text in _migration_files():
                version = filename.split("_", 1)[0]
                checksum = hashlib.sha256(sql_text.encode("utf-8")).hexdigest()
                row = conn.execute(
                    f"SELECT checksum FROM {_SCHEMA}.schema_migrations WHERE version = %s",
                    (version,),
                ).fetchone()
                if row:
                    if str(row["checksum"]) != checksum:
                        raise RuntimeError(f"applied migration {filename} has changed")
                    continue
                conn.execute(sql_text, prepare=False)
                conn.execute(
                    f"""
                    INSERT INTO {_SCHEMA}.schema_migrations (version, filename, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (version, filename, checksum),
                )
                applied.append(filename)
        self._migrated = True
        return applied

    def import_report(
        self,
        report: dict[str, Any] | HistoricalReport,
        *,
        workspace_slug: str = "b3s",
        workspace_name: str = "B3S",
    ) -> ImportOutcome:
        parsed = report if isinstance(report, HistoricalReport) else parse_report(report)
        self._ensure_migrated()

        with self._connect() as conn:
            workspace_id = self._ensure_workspace(conn, workspace_slug, workspace_name)
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(workspace_id, parsed.source_report_id),),
            )
            existing = conn.execute(
                f"""
                SELECT report_snapshots.payload_sha256,
                       brands.id AS brand_id,
                       captures.id AS capture_id,
                       evaluation_runs.id AS evaluation_run_id
                FROM {_SCHEMA}.report_snapshots
                JOIN {_SCHEMA}.evaluation_runs
                    ON evaluation_runs.id = report_snapshots.evaluation_run_id
                JOIN {_SCHEMA}.captures ON captures.id = evaluation_runs.capture_id
                JOIN {_SCHEMA}.brands ON brands.id = captures.brand_id
                WHERE report_snapshots.workspace_id = %s
                  AND report_snapshots.source_report_id = %s
                """,
                (workspace_id, parsed.source_report_id),
            ).fetchone()
            if existing:
                if str(existing["payload_sha256"]) != parsed.report_hash:
                    raise ReportConflictError(
                        f"report {parsed.source_report_id} already exists with different content"
                    )
                return ImportOutcome(
                    source_report_id=parsed.source_report_id,
                    status="unchanged",
                    brand_id=str(existing["brand_id"]),
                    capture_id=str(existing["capture_id"]),
                    evaluation_run_id=str(existing["evaluation_run_id"]),
                    report_hash=parsed.report_hash,
                )

            brand_id = self._upsert_brand(conn, workspace_id, parsed)
            scan_run_id = _stable_uuid(workspace_id, "scan", parsed.source_report_id)
            capture_id = _stable_uuid(scan_run_id, "capture")
            evaluation_run_id = _stable_uuid(capture_id, "evaluation", parsed.source_report_id)
            self._insert_scan_run(conn, scan_run_id, workspace_id, brand_id, parsed)
            self._insert_capture(conn, capture_id, scan_run_id, brand_id, parsed)
            evidence_ids = self._insert_evidence(conn, capture_id, parsed)
            self._insert_evaluation(conn, evaluation_run_id, capture_id, parsed)
            block_ids = self._insert_blocks(
                conn,
                evaluation_run_id,
                parsed,
                evidence_ids,
            )
            self._insert_components(
                conn,
                evaluation_run_id,
                parsed,
                evidence_ids,
                block_ids,
            )
            self._insert_attempts(conn, capture_id, parsed)
            self._insert_artifacts(conn, capture_id, parsed)
            self._insert_report_snapshot(
                conn,
                workspace_id,
                evaluation_run_id,
                parsed,
            )
            return ImportOutcome(
                source_report_id=parsed.source_report_id,
                status="imported",
                brand_id=str(brand_id),
                capture_id=str(capture_id),
                evaluation_run_id=str(evaluation_run_id),
                report_hash=parsed.report_hash,
            )

    def get_current_brand_state(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.brand_current_state
                WHERE workspace_slug = %s AND canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
        return dict(row) if row else None

    def get_current_report(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT report_snapshots.payload
                FROM {_SCHEMA}.brand_current_state
                JOIN {_SCHEMA}.report_snapshots
                    ON report_snapshots.evaluation_run_id = brand_current_state.evaluation_run_id
                WHERE brand_current_state.workspace_slug = %s
                  AND brand_current_state.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
        return dict(row["payload"]) if row and isinstance(row["payload"], dict) else None

    def get_report_payload(
        self,
        source_report_id: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Return one immutable report snapshot without scanning the history."""

        self._ensure_migrated()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT report_snapshots.payload
                FROM {_SCHEMA}.report_snapshots
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = report_snapshots.workspace_id
                WHERE workspaces.slug = %s
                  AND report_snapshots.source_report_id = %s
                """,
                (workspace_slug, str(source_report_id)),
            ).fetchone()
        return dict(row["payload"]) if row and isinstance(row["payload"], dict) else None

    def list_report_summaries(
        self,
        *,
        workspace_slug: str = "b3s",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return lightweight report rows for paginated index reads."""

        self._ensure_migrated()
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    report_snapshots.source_report_id AS id,
                    COALESCE(report_snapshots.payload ->> 'brand_name', '') AS brand_name,
                    COALESCE(report_snapshots.payload ->> 'url', '') AS url,
                    COALESCE(report_snapshots.payload ->> 'created_at', '') AS created_at,
                    report_snapshots.payload -> 'score' AS score,
                    report_snapshots.payload -> 'detected_count' AS detected_count,
                    report_snapshots.payload -> 'block_count' AS block_count,
                    COALESCE(report_snapshots.payload -> 'not_detected', '[]'::jsonb) AS not_detected
                FROM {_SCHEMA}.report_snapshots
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = report_snapshots.workspace_id
                WHERE workspaces.slug = %s
                ORDER BY report_snapshots.created_at DESC
                LIMIT %s OFFSET %s
                """,
                (workspace_slug, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_report_payloads_for_domain(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return paginated report snapshots for one canonical brand domain."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return []
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT report_snapshots.payload
                FROM {_SCHEMA}.report_snapshots
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = report_snapshots.workspace_id
                JOIN {_SCHEMA}.evaluation_runs
                  ON evaluation_runs.id = report_snapshots.evaluation_run_id
                JOIN {_SCHEMA}.captures
                  ON captures.id = evaluation_runs.capture_id
                JOIN {_SCHEMA}.brands
                  ON brands.id = captures.brand_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                ORDER BY report_snapshots.created_at DESC
                LIMIT %s OFFSET %s
                """,
                (workspace_slug, domain, limit, offset),
            ).fetchall()
        return [dict(row["payload"]) for row in rows if isinstance(row["payload"], dict)]

    def list_report_payloads(
        self,
        *,
        workspace_slug: str = "b3s",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return immutable report payloads for the web read model."""
        self._ensure_migrated()
        limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT report_snapshots.payload
                FROM {_SCHEMA}.report_snapshots
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = report_snapshots.workspace_id
                WHERE workspaces.slug = %s
                ORDER BY report_snapshots.created_at DESC
                LIMIT %s
                """,
                (workspace_slug, limit),
            ).fetchall()
        return [dict(row["payload"]) for row in rows if isinstance(row["payload"], dict)]

    def list_brand_history(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.brand_history
                WHERE workspace_slug = %s AND canonical_domain = %s
                ORDER BY observed_at DESC, evaluated_at DESC
                LIMIT %s OFFSET %s
                """,
                (workspace_slug, domain, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_evaluation_revisions(self, capture_id: str) -> list[dict[str, Any]]:
        self._ensure_migrated()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, capture_id, source_evaluation_key, status,
                       pipeline_version, rubric_version, prompt_version,
                       evaluator_model, gate_authority, evaluated_at, score,
                       base_average, reliability_status
                FROM {_SCHEMA}.evaluation_runs
                WHERE capture_id = %s
                ORDER BY evaluated_at DESC, recorded_at DESC
                """,
                (capture_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def storage_counts(self) -> dict[str, int]:
        self._ensure_migrated()
        tables = (
            "workspaces",
            "brands",
            "scan_runs",
            "captures",
            "evidence_records",
            "evaluation_runs",
            "block_interpretations",
            "component_evaluations",
            "tile_verdicts",
            "report_snapshots",
        )
        with self._connect() as conn:
            return {
                table: int(
                    conn.execute(f"SELECT count(*) AS count FROM {_SCHEMA}.{table}").fetchone()["count"]
                )
                for table in tables
            }

    def _ensure_migrated(self) -> None:
        if self._migrated:
            return
        with self._migration_lock:
            if not self._migrated:
                self.migrate()

    def _connect(self):
        return self._connect_fn(
            self.dsn,
            row_factory=dict_row,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        )

    @staticmethod
    def _ensure_workspace(conn, slug: str, name: str) -> UUID:
        normalized_slug = str(slug or "").strip().lower()
        workspace_id = _stable_uuid("workspace", normalized_slug)
        row = conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.workspaces (id, slug, name)
            VALUES (%s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            (workspace_id, normalized_slug, str(name or normalized_slug)),
        ).fetchone()
        return row["id"]

    @staticmethod
    def _upsert_brand(conn, workspace_id: UUID, report: HistoricalReport) -> UUID:
        brand_id = _stable_uuid(workspace_id, "brand", report.canonical_domain)
        row = conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.brands (
                id, workspace_id, canonical_domain, display_name, canonical_url,
                first_observed_at, latest_observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (workspace_id, canonical_domain) DO UPDATE SET
                display_name = CASE
                    WHEN EXCLUDED.latest_observed_at >= brands.latest_observed_at
                    THEN EXCLUDED.display_name ELSE brands.display_name END,
                canonical_url = CASE
                    WHEN EXCLUDED.latest_observed_at >= brands.latest_observed_at
                    THEN EXCLUDED.canonical_url ELSE brands.canonical_url END,
                first_observed_at = LEAST(brands.first_observed_at, EXCLUDED.first_observed_at),
                latest_observed_at = GREATEST(brands.latest_observed_at, EXCLUDED.latest_observed_at),
                updated_at = now()
            RETURNING id
            """,
            (
                brand_id,
                workspace_id,
                report.canonical_domain,
                report.brand_name,
                report.canonical_url,
                report.observed_at,
                report.observed_at,
            ),
        ).fetchone()
        return row["id"]

    @staticmethod
    def _insert_scan_run(
        conn,
        scan_run_id: UUID,
        workspace_id: UUID,
        brand_id: UUID,
        report: HistoricalReport,
    ) -> None:
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.scan_runs (
                id, workspace_id, brand_id, source_scan_id, source_run_id,
                status, pipeline_version, acquisition_state, requested_at,
                started_at, completed_at, recorded_at, request_payload, metadata
            ) VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                scan_run_id,
                workspace_id,
                brand_id,
                report.source_report_id,
                report.source_run_id,
                report.pipeline_version,
                report.acquisition_state,
                report.observed_at,
                report.observed_at,
                report.recorded_at,
                report.recorded_at,
                _jsonb({"brand_name": report.brand_name, "url": report.canonical_url}),
                _jsonb({"imported_from": "b3s-report-json", "report_hash": report.report_hash}),
            ),
        )

    @staticmethod
    def _insert_capture(
        conn,
        capture_id: UUID,
        scan_run_id: UUID,
        brand_id: UUID,
        report: HistoricalReport,
    ) -> None:
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.captures (
                id, scan_run_id, brand_id, observed_at, recorded_at, source_url,
                content_hash, acquisition_summary, limitations, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                capture_id,
                scan_run_id,
                brand_id,
                report.observed_at,
                report.recorded_at,
                report.canonical_url,
                report.capture_hash,
                _jsonb({"state": report.acquisition_state}),
                _jsonb(list(report.limitations)),
                _jsonb(report.capture_payload),
            ),
        )

    @staticmethod
    def _insert_evidence(
        conn,
        capture_id: UUID,
        report: HistoricalReport,
    ) -> dict[str, UUID]:
        evidence_ids: dict[str, UUID] = {}
        for record in report.evidence_records:
            ref = str(record.get("ref") or "").strip()
            record_id = _stable_uuid(capture_id, "evidence", ref)
            content = str(record.get("content") or "")
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            source_class = str(metadata.get("source_class") or "other")
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_records (
                    id, capture_id, evidence_ref, source, source_class,
                    evidence_type, url, content, content_raw, content_hash,
                    confidence, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record_id,
                    capture_id,
                    ref,
                    str(record.get("source") or "unknown"),
                    source_class,
                    str(record.get("evidence_type") or "unknown"),
                    str(record.get("url") or ""),
                    _pg_text(content),
                    content.encode("utf-8") if "\x00" in content else None,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    str(record.get("confidence") or "medium"),
                    _jsonb(metadata),
                ),
            )
            evidence_ids[ref] = record_id
        return evidence_ids

    @staticmethod
    def _insert_evaluation(
        conn,
        evaluation_run_id: UUID,
        capture_id: UUID,
        report: HistoricalReport,
    ) -> None:
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.evaluation_runs (
                id, capture_id, source_evaluation_key, status, pipeline_version,
                rubric_version, prompt_version, evaluator_model, gate_authority,
                evaluated_at, recorded_at, score, base_average,
                reliability_status, limitations, not_detected, config, raw_result
            ) VALUES (%s, %s, %s, 'completed', %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                evaluation_run_id,
                capture_id,
                f"report:{report.source_report_id}:primary",
                report.pipeline_version,
                report.rubric_version,
                report.prompt_version,
                report.evaluator_model,
                report.gate_authority,
                report.evaluated_at,
                report.recorded_at,
                report.score,
                report.base_average,
                report.reliability_status,
                _jsonb(list(report.limitations)),
                _jsonb(list(report.not_detected)),
                _jsonb(report.evaluation_config),
                _jsonb(report.evaluation_result),
            ),
        )

    @staticmethod
    def _insert_blocks(
        conn,
        evaluation_run_id: UUID,
        report: HistoricalReport,
        evidence_ids: dict[str, UUID],
    ) -> dict[str, UUID]:
        block_ids: dict[str, UUID] = {}
        for block in report.block_interpretations:
            key = str(block.get("name") or "").strip()
            if not key:
                continue
            block_id = _stable_uuid(evaluation_run_id, "block", key)
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.block_interpretations (
                    id, evaluation_run_id, component_key, detected, content,
                    rejected_content, confidence, rationale, coverage_status,
                    provenance_source, raw_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    block_id,
                    evaluation_run_id,
                    key,
                    block.get("detected") is True,
                    str(block.get("content") or ""),
                    str(block.get("rejected_content") or ""),
                    str(block.get("confidence") or ""),
                    str(block.get("rationale") or ""),
                    str(block.get("coverage_status") or ""),
                    str(block.get("provenance_source") or ""),
                    _jsonb(block),
                ),
            )
            block_ids[key] = block_id
            for ordinal, ref_row in enumerate(block.get("refs") or []):
                ref = (
                    str(ref_row.get("ref") or "").strip()
                    if isinstance(ref_row, dict)
                    else str(ref_row or "").strip()
                )
                evidence_id = evidence_ids.get(ref)
                if evidence_id is None:
                    continue
                conn.execute(
                    f"""
                    INSERT INTO {_SCHEMA}.block_evidence_links (
                        block_interpretation_id, evidence_record_id, ordinal
                    ) VALUES (%s, %s, %s)
                    """,
                    (block_id, evidence_id, ordinal),
                )
        return block_ids

    @staticmethod
    def _insert_components(
        conn,
        evaluation_run_id: UUID,
        report: HistoricalReport,
        evidence_ids: dict[str, UUID],
        block_ids: dict[str, UUID],
    ) -> None:
        raw_components = report.evaluation_result.get("components")
        raw_components = raw_components if isinstance(raw_components, dict) else {}
        for component in report.components:
            key = str(component.get("key") or component.get("component") or "").strip()
            if not key:
                continue
            raw_component = raw_components.get(key) if isinstance(raw_components.get(key), dict) else {}
            component_id = _stable_uuid(evaluation_run_id, "component", key)
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.component_evaluations (
                    id, evaluation_run_id, block_interpretation_id, component_key,
                    label, status, score, max_score, points, confidence,
                    detected_content, summary, verdict, message, detection_mode,
                    detection_limitations, evidence_source_summary, raw_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s)
                """,
                (
                    component_id,
                    evaluation_run_id,
                    block_ids.get(key),
                    key,
                    str(component.get("label") or key),
                    str(component.get("status") or raw_component.get("status") or "unknown"),
                    _number(raw_component.get("score", component.get("score"))),
                    _number(raw_component.get("scale", component.get("scale"))),
                    _number(raw_component.get("points", component.get("points"))),
                    str(component.get("confidence") or raw_component.get("confidence") or ""),
                    str(component.get("detected_content") or raw_component.get("detected_content") or ""),
                    str(component.get("resumen") or ""),
                    str(component.get("veredicto") or raw_component.get("veredicto") or ""),
                    str(component.get("message") or raw_component.get("message") or ""),
                    str(raw_component.get("detection_mode") or ""),
                    _jsonb(raw_component.get("detection_limitations") or []),
                    _jsonb(raw_component.get("evidence_source_summary") or {}),
                    _jsonb({"report": component, "result": raw_component}),
                ),
            )
            tile_profile = raw_component.get("tile_profile")
            if not isinstance(tile_profile, list):
                tile_profile = component.get("tile_profile") if isinstance(component.get("tile_profile"), list) else []
            for tile in tile_profile:
                if isinstance(tile, dict):
                    PostgresHistoryRepository._insert_tile(
                        conn,
                        component_id,
                        tile,
                        evidence_ids,
                        report,
                    )

    @staticmethod
    def _insert_tile(
        conn,
        component_id: UUID,
        tile: dict[str, Any],
        evidence_ids: dict[str, UUID],
        report: HistoricalReport,
    ) -> None:
        tile_id = str(tile.get("id") or tile.get("tile_id") or "").strip()
        state = str(tile.get("estado") or tile.get("state") or "").strip()
        if not tile_id or state not in {"ok", "no", "sin_evidencia"}:
            return
        evidence_ref = str(tile.get("evidence_ref") or "").strip()
        quote = str(tile.get("evidencia") or tile.get("evidence_quote") or "").strip()
        evidence_id = evidence_ids.get(evidence_ref)
        verified = _literal_quote_verified(evidence_ref, quote, report.evidence_records)
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.tile_verdicts (
                id, component_evaluation_id, evidence_record_id, tile_id, state,
                evidence_ref, evidence_quote, evidence_literal_verified, reason,
                required_context, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                _stable_uuid(component_id, "tile", tile_id),
                component_id,
                evidence_id,
                tile_id,
                state,
                evidence_ref,
                quote,
                verified,
                str(tile.get("motivo") or tile.get("reason") or ""),
                str(tile.get("contexto_requerido") or tile.get("required_context") or ""),
                _jsonb(tile),
            ),
        )

    @staticmethod
    def _insert_attempts(conn, capture_id: UUID, report: HistoricalReport) -> None:
        for index, attempt in enumerate(report.acquisition_attempts):
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.acquisition_attempts (
                    id, capture_id, provider, intent, status, detail, raw_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _stable_uuid(capture_id, "attempt", index),
                    capture_id,
                    str(attempt.get("provider") or "unknown"),
                    str(attempt.get("intent") or ""),
                    str(attempt.get("status") or ""),
                    str(attempt.get("detail") or ""),
                    _jsonb(attempt),
                ),
            )

    @staticmethod
    def _insert_artifacts(conn, capture_id: UUID, report: HistoricalReport) -> None:
        for index, artifact in enumerate(report.artifacts):
            uri = str(
                artifact.get("public_url")
                or artifact.get("screenshot_url")
                or artifact.get("screenshot_path")
                or artifact.get("url")
                or ""
            )
            content_hash = str(artifact.get("sha256") or artifact.get("content_hash") or "") or None
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.artifacts (
                    id, capture_id, kind, source, uri, content_hash, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _stable_uuid(capture_id, "artifact", index),
                    capture_id,
                    str(artifact.get("kind") or "unknown"),
                    str(artifact.get("source") or ""),
                    uri,
                    content_hash,
                    _jsonb(artifact),
                ),
            )

    @staticmethod
    def _insert_report_snapshot(
        conn,
        workspace_id: UUID,
        evaluation_run_id: UUID,
        report: HistoricalReport,
    ) -> None:
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.report_snapshots (
                id, workspace_id, evaluation_run_id, source_report_id, format,
                language, created_at, payload_sha256, payload, payload_raw
            ) VALUES (%s, %s, %s, %s, 'b3s-report-json', 'es', %s, %s, %s, %s)
            """,
            (
                _stable_uuid(workspace_id, "report", report.source_report_id),
                workspace_id,
                evaluation_run_id,
                report.source_report_id,
                report.recorded_at,
                report.report_hash,
                _jsonb(report.report_payload),
                canonical_json_bytes(report.report_payload),
            ),
        )


def _migration_files() -> list[tuple[str, str]]:
    root = resources.files("src.history").joinpath("migrations")
    return [
        (item.name, item.read_text(encoding="utf-8"))
        for item in sorted(root.iterdir(), key=lambda path: path.name)
        if item.name.endswith(".sql")
    ]


def _stable_uuid(*parts: Any) -> UUID:
    key = ":".join(str(part) for part in parts)
    return uuid5(_ID_NAMESPACE, key)


def _advisory_lock_key(*parts: Any) -> int:
    digest = hashlib.sha256(":".join(str(part) for part in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _literal_quote_verified(
    evidence_ref: str,
    quote: str,
    records: tuple[dict[str, Any], ...],
) -> bool | None:
    if not evidence_ref or not quote:
        return None
    record = next((item for item in records if str(item.get("ref") or "") == evidence_ref), None)
    if record is None:
        return False
    normalized_quote = " ".join(quote.split()).casefold()
    normalized_content = " ".join(str(record.get("content") or "").split()).casefold()
    return bool(normalized_quote and normalized_quote in normalized_content)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _jsonb(value: Any) -> Jsonb:
    return Jsonb(_postgres_json_value(value))


def _postgres_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _pg_text(value)
    if isinstance(value, dict):
        return {str(key): _postgres_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_postgres_json_value(item) for item in value]
    return value


def _pg_text(value: Any) -> str:
    return str(value or "").replace("\x00", "\ufffd")
