"""Synchronous PostgreSQL repository for immutable B3S history."""

from __future__ import annotations

import hashlib
from importlib import resources
import logging
from threading import Lock
from typing import Any, Callable
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.history.models import HistoricalReport, ImportOutcome, ReportConflictError
from src.history.report_parser import canonical_json_bytes, normalize_domain, parse_report
from src.services.evidence_claim_reconciliation import (
    CLAIM_RECONCILIATION_DECISIONS,
    CLAIM_RECONCILIATION_RELATION_TYPES,
    CLAIM_RECONCILIATION_SUBJECT_TYPE,
    EVIDENCE_CLAIM_RECONCILIATION_POLICY_VERSION,
    EVIDENCE_CLAIM_RECONCILIATION_VERSION,
    EvidenceClaimReconciliationCommand,
    EvidenceClaimReconciliationConflictError,
    EvidenceClaimReconciliationInvalidTransitionError,
    EvidenceClaimReconciliationNotFoundError,
)
from src.services.evidence_claim_tile_ledger import (
    build_evidence_claim_tile_ledger,
    evidence_claim_tile_ledger_mode,
)
from src.services.evidence_memory_adjudication import (
    ADJUDICATION_DECISIONS,
    ADJUDICATION_SUBJECT_TYPE,
    EVIDENCE_MEMORY_ADJUDICATION_POLICY_VERSION,
    EVIDENCE_MEMORY_ADJUDICATION_VERSION,
    EvidenceMemoryAdjudicationCommand,
    EvidenceMemoryAdjudicationConflictError,
    EvidenceMemoryAdjudicationInvalidTransitionError,
    EvidenceMemoryAdjudicationNotFoundError,
)
from src.services.evidence_scoring_memory_preview import (
    build_evidence_scoring_memory_preview,
)
from src.services.evidence_ledger_shadow import (
    build_evidence_ledger_shadow,
    evidence_ledger_mode,
)
from src.services.scanner_evidence_comparison import (
    CANONICAL_POLICY_VERSION,
    annotate_report_history,
    build_evidence_snapshot,
)

_ID_NAMESPACE = UUID("3ef1b80c-e7b7-4fb3-95ad-fb9e03c59d52")
_SCHEMA = "b3s_history"
_CONNECT_TIMEOUT_SECONDS = 5
_LOG = logging.getLogger(__name__)


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
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_advisory_lock_key(workspace_id, "brand", existing["brand_id"]),),
                )
                self._rebuild_brand_stability(conn, workspace_id, existing["brand_id"])
                self._rebuild_evidence_ledger_shadow_safely(
                    conn,
                    workspace_id,
                    existing["brand_id"],
                )
                self._rebuild_evidence_claim_tile_ledger_safely(
                    conn,
                    workspace_id,
                    existing["brand_id"],
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
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(workspace_id, "brand", brand_id),),
            )
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
            self._rebuild_brand_stability(conn, workspace_id, brand_id)
            self._rebuild_evidence_ledger_shadow_safely(
                conn,
                workspace_id,
                brand_id,
            )
            self._rebuild_evidence_claim_tile_ledger_safely(
                conn,
                workspace_id,
                brand_id,
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

    def get_evidence_ledger_shadow(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Return the persisted shadow read model for one brand."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT evidence_ledger_shadow_states.payload
                FROM {_SCHEMA}.evidence_ledger_shadow_states
                JOIN {_SCHEMA}.brands
                  ON brands.id = evidence_ledger_shadow_states.brand_id
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
        return dict(row["payload"]) if row and isinstance(row["payload"], dict) else None

    def get_evidence_claim_tile_ledger(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Return the persisted non-authoritative claim-to-tile ledger."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT evidence_claim_tile_ledger_states.payload
                FROM {_SCHEMA}.evidence_claim_tile_ledger_states
                JOIN {_SCHEMA}.brands
                  ON brands.id = evidence_claim_tile_ledger_states.brand_id
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
        return dict(row["payload"]) if row and isinstance(row["payload"], dict) else None

    def get_evidence_scoring_memory_preview(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Rebuild the additive score preview from durable immutable history.

        The preview is intentionally computed on read. PostgreSQL persists the
        source reports and review events; recreating this repository after a
        process restart therefore reconstructs the same memory version without
        introducing a second authoritative copy.
        """

        reports = self.list_report_payloads_for_domain(
            domain_or_url,
            workspace_slug=workspace_slug,
            limit=500,
        )
        if not reports:
            return None
        adjudications = self.list_current_evidence_memory_adjudications(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        return build_evidence_scoring_memory_preview(
            reports,
            mode="shadow",
            evidence_adjudications=adjudications,
        )

    def append_evidence_memory_adjudication(
        self,
        domain_or_url: str,
        command: EvidenceMemoryAdjudicationCommand,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Append one identity decision with idempotency and optimistic locking."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceMemoryAdjudicationNotFoundError(
                "The brand domain does not exist in durable history."
            )
        _validate_adjudication_command(command)

        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                raise EvidenceMemoryAdjudicationNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-memory-adjudication-idempotency",
                        command.idempotency_key_hash,
                    ),
                ),
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        ADJUDICATION_SUBJECT_TYPE,
                        command.subject_id,
                    ),
                ),
            )
            existing = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_memory_adjudication_events
                WHERE brand_id = %s
                  AND idempotency_key_hash = %s
                """,
                (brand_id, command.idempotency_key_hash),
            ).fetchone()
            if existing is not None:
                if str(existing["request_fingerprint"]) != command.request_fingerprint:
                    raise EvidenceMemoryAdjudicationConflictError(
                        "The Idempotency-Key was already used for a different adjudication.",
                        existing_event_id=str(existing["id"]),
                    )
                latest = conn.execute(
                    f"""
                    SELECT id
                    FROM {_SCHEMA}.evidence_memory_adjudication_events
                    WHERE brand_id = %s
                      AND subject_type = %s
                      AND subject_id = %s
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (
                        brand_id,
                        ADJUDICATION_SUBJECT_TYPE,
                        command.subject_id,
                    ),
                ).fetchone()
                effective_state = (
                    str(existing["decision"])
                    if latest is not None and latest["id"] == existing["id"]
                    else "superseded"
                )
                return _adjudication_event(
                    existing,
                    effective_state=effective_state,
                ), True

            current = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_memory_adjudication_events
                WHERE brand_id = %s
                  AND subject_type = %s
                  AND subject_id = %s
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (brand_id, ADJUDICATION_SUBJECT_TYPE, command.subject_id),
            ).fetchone()
            current_id = str(current["id"]) if current is not None else None
            if command.expected_current_event_id != current_id:
                raise EvidenceMemoryAdjudicationConflictError(
                    "The evidence adjudication changed after it was read.",
                    current_event_id=current_id,
                )
            if command.decision == "revoked" and (
                current is None or str(current["decision"]) == "revoked"
            ):
                raise EvidenceMemoryAdjudicationInvalidTransitionError(
                    "Only a current accepted, disputed, or rejected decision can be revoked."
                )

            sequence = int(current["sequence"]) + 1 if current is not None else 1
            event_id = _stable_uuid(
                brand_id,
                "evidence-memory-adjudication",
                command.idempotency_key_hash,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_memory_adjudication_events (
                    id, brand_id, subject_type, subject_id, sequence, decision,
                    supersedes_event_id, schema_version, policy_version,
                    evaluator_version, reviewer, actor_id, reason_code,
                    rationale, idempotency_key_hash, request_fingerprint,
                    runtime_effect, authority
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, false, false
                )
                RETURNING *
                """,
                (
                    event_id,
                    brand_id,
                    ADJUDICATION_SUBJECT_TYPE,
                    command.subject_id,
                    sequence,
                    command.decision,
                    current["id"] if current is not None else None,
                    EVIDENCE_MEMORY_ADJUDICATION_VERSION,
                    EVIDENCE_MEMORY_ADJUDICATION_POLICY_VERSION,
                    command.evaluator_version,
                    command.reviewer,
                    command.actor_id,
                    command.reason_code,
                    command.rationale,
                    command.idempotency_key_hash,
                    command.request_fingerprint,
                ),
            ).fetchone()
        return _adjudication_event(inserted), False

    def list_current_evidence_memory_adjudications(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> list[dict[str, Any]]:
        """Return the latest identity decision for each evidence subject."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ON (
                    evidence_memory_adjudication_events.subject_type,
                    evidence_memory_adjudication_events.subject_id
                ) evidence_memory_adjudication_events.*
                FROM {_SCHEMA}.evidence_memory_adjudication_events
                JOIN {_SCHEMA}.brands
                  ON brands.id = evidence_memory_adjudication_events.brand_id
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                ORDER BY evidence_memory_adjudication_events.subject_type,
                         evidence_memory_adjudication_events.subject_id,
                         evidence_memory_adjudication_events.sequence DESC
                """,
                (workspace_slug, domain),
            ).fetchall()
        return [_adjudication_event(row) for row in rows]

    def list_evidence_memory_adjudications(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        subject_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a reviewable journal page and the current decision set."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceMemoryAdjudicationNotFoundError(
                "The brand domain does not exist in durable history."
            )
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        normalized_subject_id = (
            str(subject_id or "").strip().lower() or None
        )
        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                raise EvidenceMemoryAdjudicationNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            rows = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT events.*,
                           max(sequence) OVER (
                               PARTITION BY subject_type, subject_id
                           ) AS current_sequence
                    FROM {_SCHEMA}.evidence_memory_adjudication_events AS events
                    WHERE brand_id = %s
                      AND (%s::text IS NULL OR subject_id = %s)
                )
                SELECT *
                FROM ranked
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (
                    brand_id,
                    normalized_subject_id,
                    normalized_subject_id,
                    limit,
                    offset,
                ),
            ).fetchall()
            total_row = conn.execute(
                f"""
                SELECT count(*) AS count
                FROM {_SCHEMA}.evidence_memory_adjudication_events
                WHERE brand_id = %s
                  AND (%s::text IS NULL OR subject_id = %s)
                """,
                (brand_id, normalized_subject_id, normalized_subject_id),
            ).fetchone()
            current_rows = conn.execute(
                f"""
                SELECT DISTINCT ON (subject_type, subject_id) *
                FROM {_SCHEMA}.evidence_memory_adjudication_events
                WHERE brand_id = %s
                  AND (%s::text IS NULL OR subject_id = %s)
                ORDER BY subject_type, subject_id, sequence DESC
                """,
                (brand_id, normalized_subject_id, normalized_subject_id),
            ).fetchall()

        events = [
            _adjudication_event(
                row,
                effective_state=(
                    str(row["decision"])
                    if int(row["sequence"]) == int(row["current_sequence"])
                    else "superseded"
                ),
            )
            for row in rows
        ]
        total = int(total_row["count"])
        return {
            "events": events,
            "current": [_adjudication_event(row) for row in current_rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def append_evidence_claim_reconciliation(
        self,
        domain_or_url: str,
        command: EvidenceClaimReconciliationCommand,
        *,
        relation_type: str,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Append one relation decision with idempotency and optimistic locking."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceClaimReconciliationNotFoundError(
                "The brand domain does not exist in durable history."
            )
        _validate_claim_reconciliation_command(
            command,
            relation_type=relation_type,
        )

        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                raise EvidenceClaimReconciliationNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-claim-reconciliation-idempotency",
                        command.idempotency_key_hash,
                    ),
                ),
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        CLAIM_RECONCILIATION_SUBJECT_TYPE,
                        command.subject_id,
                    ),
                ),
            )
            existing = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_claim_reconciliation_events
                WHERE brand_id = %s
                  AND idempotency_key_hash = %s
                """,
                (brand_id, command.idempotency_key_hash),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["request_fingerprint"])
                    != command.request_fingerprint
                    or str(existing["relation_type"]) != relation_type
                ):
                    raise EvidenceClaimReconciliationConflictError(
                        "The Idempotency-Key was already used for a different reconciliation.",
                        existing_event_id=str(existing["id"]),
                    )
                latest = conn.execute(
                    f"""
                    SELECT id
                    FROM {_SCHEMA}.evidence_claim_reconciliation_events
                    WHERE brand_id = %s
                      AND subject_type = %s
                      AND subject_id = %s
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (
                        brand_id,
                        CLAIM_RECONCILIATION_SUBJECT_TYPE,
                        command.subject_id,
                    ),
                ).fetchone()
                effective_state = (
                    str(existing["decision"])
                    if latest is not None and latest["id"] == existing["id"]
                    else "superseded"
                )
                return _claim_reconciliation_event(
                    existing,
                    effective_state=effective_state,
                ), True

            current = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_claim_reconciliation_events
                WHERE brand_id = %s
                  AND subject_type = %s
                  AND subject_id = %s
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (
                    brand_id,
                    CLAIM_RECONCILIATION_SUBJECT_TYPE,
                    command.subject_id,
                ),
            ).fetchone()
            current_id = str(current["id"]) if current is not None else None
            if command.expected_current_event_id != current_id:
                raise EvidenceClaimReconciliationConflictError(
                    "The claim reconciliation changed after it was read.",
                    current_event_id=current_id,
                )
            if current is not None and str(current["relation_type"]) != relation_type:
                raise EvidenceClaimReconciliationConflictError(
                    "The projected claim relation type changed unexpectedly.",
                    current_event_id=current_id,
                )
            if command.decision == "revoked" and (
                current is None or str(current["decision"]) == "revoked"
            ):
                raise EvidenceClaimReconciliationInvalidTransitionError(
                    "Only a current accepted, disputed, or rejected relation decision can be revoked."
                )

            sequence = int(current["sequence"]) + 1 if current is not None else 1
            event_id = _stable_uuid(
                brand_id,
                "evidence-claim-reconciliation",
                command.idempotency_key_hash,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_claim_reconciliation_events (
                    id, brand_id, subject_type, subject_id, relation_type,
                    sequence, decision, supersedes_event_id, schema_version,
                    policy_version, evaluator_version, reviewer, actor_id,
                    reason_code, rationale, idempotency_key_hash,
                    request_fingerprint, runtime_effect, authority
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, false, false
                )
                RETURNING *
                """,
                (
                    event_id,
                    brand_id,
                    CLAIM_RECONCILIATION_SUBJECT_TYPE,
                    command.subject_id,
                    relation_type,
                    sequence,
                    command.decision,
                    current["id"] if current is not None else None,
                    EVIDENCE_CLAIM_RECONCILIATION_VERSION,
                    EVIDENCE_CLAIM_RECONCILIATION_POLICY_VERSION,
                    command.evaluator_version,
                    command.reviewer,
                    command.actor_id,
                    command.reason_code,
                    command.rationale,
                    command.idempotency_key_hash,
                    command.request_fingerprint,
                ),
            ).fetchone()
        return _claim_reconciliation_event(inserted), False

    def list_current_evidence_claim_reconciliations(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> list[dict[str, Any]]:
        """Return the latest decision for each claim-relation subject."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ON (
                    events.subject_type,
                    events.subject_id
                ) events.*
                FROM {_SCHEMA}.evidence_claim_reconciliation_events AS events
                JOIN {_SCHEMA}.brands
                  ON brands.id = events.brand_id
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                ORDER BY events.subject_type,
                         events.subject_id,
                         events.sequence DESC
                """,
                (workspace_slug, domain),
            ).fetchall()
        return [_claim_reconciliation_event(row) for row in rows]

    def list_evidence_claim_reconciliations(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        subject_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a page of relation decisions and their current states."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceClaimReconciliationNotFoundError(
                "The brand domain does not exist in durable history."
            )
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        normalized_subject_id = (
            str(subject_id or "").strip().lower() or None
        )
        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                raise EvidenceClaimReconciliationNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            rows = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT events.*,
                           max(sequence) OVER (
                               PARTITION BY subject_type, subject_id
                           ) AS current_sequence
                    FROM {_SCHEMA}.evidence_claim_reconciliation_events AS events
                    WHERE brand_id = %s
                      AND (%s::text IS NULL OR subject_id = %s)
                )
                SELECT *
                FROM ranked
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (
                    brand_id,
                    normalized_subject_id,
                    normalized_subject_id,
                    limit,
                    offset,
                ),
            ).fetchall()
            total_row = conn.execute(
                f"""
                SELECT count(*) AS count
                FROM {_SCHEMA}.evidence_claim_reconciliation_events
                WHERE brand_id = %s
                  AND (%s::text IS NULL OR subject_id = %s)
                """,
                (
                    brand_id,
                    normalized_subject_id,
                    normalized_subject_id,
                ),
            ).fetchone()
            current_rows = conn.execute(
                f"""
                SELECT DISTINCT ON (subject_type, subject_id) *
                FROM {_SCHEMA}.evidence_claim_reconciliation_events
                WHERE brand_id = %s
                  AND (%s::text IS NULL OR subject_id = %s)
                ORDER BY subject_type, subject_id, sequence DESC
                """,
                (
                    brand_id,
                    normalized_subject_id,
                    normalized_subject_id,
                ),
            ).fetchall()

        events = [
            _claim_reconciliation_event(
                row,
                effective_state=(
                    str(row["decision"])
                    if int(row["sequence"]) == int(row["current_sequence"])
                    else "superseded"
                ),
            )
            for row in rows
        ]
        total = int(total_row["count"])
        return {
            "events": events,
            "current": [
                _claim_reconciliation_event(row) for row in current_rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def rebuild_evidence_ledger_shadows(
        self,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        """Backfill every existing brand without affecting authoritative state."""

        mode = evidence_ledger_mode()
        if mode != "shadow":
            return {
                "mode": mode,
                "runtime_effect": False,
                "workspace_slug": workspace_slug,
                "brands_discovered": 0,
                "rebuilt": 0,
                "failed": 0,
                "failed_domains": [],
            }

        self._ensure_migrated()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT workspaces.id AS workspace_id,
                                brands.id,
                                brands.canonical_domain
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                JOIN {_SCHEMA}.captures
                  ON captures.brand_id = brands.id
                JOIN {_SCHEMA}.evaluation_runs
                  ON evaluation_runs.capture_id = captures.id
                JOIN {_SCHEMA}.report_snapshots
                  ON report_snapshots.evaluation_run_id = evaluation_runs.id
                WHERE workspaces.slug = %s
                ORDER BY brands.canonical_domain
                """,
                (workspace_slug,),
            ).fetchall()

        rebuilt = 0
        failed_domains: list[str] = []
        for row in rows:
            workspace_id = UUID(str(row["workspace_id"]))
            brand_id = row["id"]
            domain = str(row["canonical_domain"])
            try:
                with self._connect() as conn:
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (_advisory_lock_key(workspace_id, "brand", brand_id),),
                    )
                    if self._rebuild_evidence_ledger_shadow_safely(
                        conn,
                        workspace_id,
                        UUID(str(brand_id)),
                    ):
                        rebuilt += 1
                    else:
                        failed_domains.append(domain)
            except Exception:
                failed_domains.append(domain)
                _LOG.exception(
                    "failed to backfill evidence ledger shadow",
                    extra={"brand_id": str(brand_id), "domain": domain},
                )

        return {
            "mode": mode,
            "runtime_effect": False,
            "workspace_slug": workspace_slug,
            "brands_discovered": len(rows),
            "rebuilt": rebuilt,
            "failed": len(failed_domains),
            "failed_domains": failed_domains,
        }

    def rebuild_evidence_claim_tile_ledgers(
        self,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        """Backfill the versioned shadow mapping without changing evaluation."""

        mode = evidence_claim_tile_ledger_mode()
        if mode != "shadow":
            return {
                "mode": mode,
                "runtime_effect": False,
                "authority": False,
                "workspace_slug": workspace_slug,
                "brands_discovered": 0,
                "rebuilt": 0,
                "failed": 0,
                "failed_domains": [],
            }

        self._ensure_migrated()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT workspaces.id AS workspace_id,
                                brands.id,
                                brands.canonical_domain
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                JOIN {_SCHEMA}.captures
                  ON captures.brand_id = brands.id
                JOIN {_SCHEMA}.evaluation_runs
                  ON evaluation_runs.capture_id = captures.id
                JOIN {_SCHEMA}.report_snapshots
                  ON report_snapshots.evaluation_run_id = evaluation_runs.id
                WHERE workspaces.slug = %s
                ORDER BY brands.canonical_domain
                """,
                (workspace_slug,),
            ).fetchall()

        rebuilt = 0
        failed_domains: list[str] = []
        for row in rows:
            workspace_id = UUID(str(row["workspace_id"]))
            brand_id = UUID(str(row["id"]))
            domain = str(row["canonical_domain"])
            try:
                with self._connect() as conn:
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (
                            _advisory_lock_key(
                                workspace_id,
                                "brand",
                                brand_id,
                            ),
                        ),
                    )
                    if self._rebuild_evidence_claim_tile_ledger_safely(
                        conn,
                        workspace_id,
                        brand_id,
                    ):
                        rebuilt += 1
                    else:
                        failed_domains.append(domain)
            except Exception:
                failed_domains.append(domain)
                _LOG.exception(
                    "failed to backfill evidence claim tile ledger",
                    extra={"brand_id": str(brand_id), "domain": domain},
                )

        return {
            "mode": mode,
            "runtime_effect": False,
            "authority": False,
            "workspace_slug": workspace_slug,
            "brands_discovered": len(rows),
            "rebuilt": rebuilt,
            "failed": len(failed_domains),
            "failed_domains": failed_domains,
        }

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
            "capture_fingerprints",
            "evaluation_comparisons",
            "brand_canonical_selections",
            "evidence_ledger_shadow_states",
            "evidence_ledger_shadow_entries",
            "evidence_ledger_shadow_observations",
            "evidence_memory_adjudication_events",
            "evidence_claim_reconciliation_events",
            "evidence_claim_tile_ledger_states",
            "evidence_claim_tile_mapping_series",
            "evidence_claim_tile_mappings",
            "evidence_claim_tile_mapping_observations",
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

    def _rebuild_evidence_ledger_shadow_safely(
        self,
        conn,
        workspace_id: UUID,
        brand_id: UUID,
    ) -> bool:
        """Keep a shadow failure outside the authoritative import transaction."""

        if evidence_ledger_mode() != "shadow":
            return False
        try:
            # Psycopg maps a nested transaction block to a SAVEPOINT. A broken
            # experimental projection therefore rolls back only its own writes.
            with conn.transaction():
                self._rebuild_evidence_ledger_shadow(
                    conn,
                    workspace_id,
                    brand_id,
                )
            return True
        except Exception:
            _LOG.exception(
                "failed to rebuild evidence ledger shadow",
                extra={"brand_id": str(brand_id)},
            )
            return False

    @staticmethod
    def _rebuild_evidence_ledger_shadow(
        conn,
        workspace_id: UUID,
        brand_id: UUID,
    ) -> None:
        rows = conn.execute(
            f"""
            SELECT report_snapshots.source_report_id,
                   report_snapshots.payload,
                   captures.id AS capture_id
            FROM {_SCHEMA}.report_snapshots
            JOIN {_SCHEMA}.evaluation_runs
              ON evaluation_runs.id = report_snapshots.evaluation_run_id
            JOIN {_SCHEMA}.captures
              ON captures.id = evaluation_runs.capture_id
            WHERE report_snapshots.workspace_id = %s
              AND captures.brand_id = %s
            ORDER BY report_snapshots.created_at, report_snapshots.source_report_id
            """,
            (workspace_id, brand_id),
        ).fetchall()
        reports = [dict(row["payload"]) for row in rows if isinstance(row["payload"], dict)]
        row_by_report_id = {str(row["source_report_id"]): row for row in rows}
        conn.execute(
            f"DELETE FROM {_SCHEMA}.evidence_ledger_shadow_entries WHERE brand_id = %s",
            (brand_id,),
        )
        if not reports:
            conn.execute(
                f"DELETE FROM {_SCHEMA}.evidence_ledger_shadow_states WHERE brand_id = %s",
                (brand_id,),
            )
            return

        payload = build_evidence_ledger_shadow(reports, mode="shadow")
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            fingerprint = str(entry.get("evidence_fingerprint") or "")
            entry_id = _stable_uuid(
                brand_id,
                "evidence-ledger-shadow",
                fingerprint,
            )
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_ledger_shadow_entries (
                    id, brand_id, evidence_fingerprint, locator_hash,
                    source_class, evidence_type, canonical_url, source_domain,
                    content_hash, state, reason_codes, first_seen_at,
                    last_seen_at, age_days, ttl_days, observation_count,
                    qualified_observation_count, present_in_latest,
                    identity_matches, locator_variant_count, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    entry_id,
                    brand_id,
                    fingerprint,
                    str(entry.get("locator_hash") or ""),
                    str(entry.get("source_class") or "other"),
                    str(entry.get("evidence_type") or "unknown"),
                    str(entry.get("url") or ""),
                    str(entry.get("source_domain") or ""),
                    str(entry.get("content_hash") or ""),
                    str(entry.get("state") or "observed"),
                    _jsonb(entry.get("reason_codes") or []),
                    entry.get("first_seen_at"),
                    entry.get("last_seen_at"),
                    int(entry.get("age_days") or 0),
                    int(entry.get("ttl_days") or 1),
                    int(entry.get("observation_count") or 1),
                    int(entry.get("qualified_observation_count") or 0),
                    bool(entry.get("present_in_latest")),
                    _jsonb(entry.get("identity_matches") or []),
                    int(entry.get("locator_variant_count") or 1),
                    _jsonb(
                        {
                            "report_ids": entry.get("report_ids") or [],
                            "shadow_only": True,
                        }
                    ),
                ),
            )
            for observation in entry.get("observations") or []:
                if not isinstance(observation, dict):
                    continue
                report_id = str(observation.get("report_id") or "")
                report_row = row_by_report_id.get(report_id)
                if report_row is None:
                    continue
                conn.execute(
                    f"""
                    INSERT INTO {_SCHEMA}.evidence_ledger_shadow_observations (
                        ledger_entry_id, capture_id, source_report_id,
                        observed_at, identity_match, acquisition_state, invalid
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        entry_id,
                        report_row["capture_id"],
                        report_id,
                        observation.get("observed_at"),
                        str(observation.get("identity_match") or ""),
                        str(observation.get("acquisition_state") or "unknown"),
                        bool(observation.get("invalid")),
                    ),
                )

        latest_report_id = str(payload.get("latest_report_id") or "")
        latest_row = row_by_report_id.get(latest_report_id)
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.evidence_ledger_shadow_states (
                brand_id, latest_capture_id, schema_version, policy_version,
                mode, runtime_effect, state_fingerprint, summary, payload,
                computed_at
            ) VALUES (%s, %s, %s, %s, 'shadow', false, %s, %s, %s, now())
            ON CONFLICT (brand_id) DO UPDATE SET
                latest_capture_id = EXCLUDED.latest_capture_id,
                schema_version = EXCLUDED.schema_version,
                policy_version = EXCLUDED.policy_version,
                mode = EXCLUDED.mode,
                runtime_effect = false,
                state_fingerprint = EXCLUDED.state_fingerprint,
                summary = EXCLUDED.summary,
                payload = EXCLUDED.payload,
                computed_at = now()
            """,
            (
                brand_id,
                latest_row["capture_id"] if latest_row is not None else None,
                str(payload.get("schema_version") or ""),
                str(payload.get("policy_version") or ""),
                str(payload.get("state_fingerprint") or ""),
                _jsonb(payload.get("summary") or {}),
                _jsonb(payload),
            ),
        )

    def _rebuild_evidence_claim_tile_ledger_safely(
        self,
        conn,
        workspace_id: UUID,
        brand_id: UUID,
    ) -> bool:
        """Isolate the experimental mapping from the import transaction."""

        if evidence_claim_tile_ledger_mode() != "shadow":
            return False
        try:
            with conn.transaction():
                self._rebuild_evidence_claim_tile_ledger(
                    conn,
                    workspace_id,
                    brand_id,
                )
            return True
        except Exception:
            _LOG.exception(
                "failed to rebuild evidence claim tile ledger",
                extra={"brand_id": str(brand_id)},
            )
            return False

    @staticmethod
    def _rebuild_evidence_claim_tile_ledger(
        conn,
        workspace_id: UUID,
        brand_id: UUID,
    ) -> None:
        rows = conn.execute(
            f"""
            SELECT report_snapshots.source_report_id,
                   report_snapshots.payload,
                   captures.id AS capture_id
            FROM {_SCHEMA}.report_snapshots
            JOIN {_SCHEMA}.evaluation_runs
              ON evaluation_runs.id = report_snapshots.evaluation_run_id
            JOIN {_SCHEMA}.captures
              ON captures.id = evaluation_runs.capture_id
            WHERE report_snapshots.workspace_id = %s
              AND captures.brand_id = %s
            ORDER BY report_snapshots.created_at,
                     report_snapshots.source_report_id
            """,
            (workspace_id, brand_id),
        ).fetchall()
        reports = [
            dict(row["payload"])
            for row in rows
            if isinstance(row["payload"], dict)
        ]
        if not reports:
            conn.execute(
                f"""
                DELETE FROM {_SCHEMA}.evidence_claim_tile_ledger_states
                WHERE brand_id = %s
                """,
                (brand_id,),
            )
            return

        row_by_report_id = {
            str(row["source_report_id"]): row for row in rows
        }
        payload = build_evidence_claim_tile_ledger(
            reports,
            mode="shadow",
        )

        for series in payload.get("mapping_series") or []:
            if not isinstance(series, dict):
                continue
            series_id = str(series.get("mapping_series_id") or "")
            series_entry_id = _stable_uuid(
                brand_id,
                "evidence-claim-tile-series",
                series_id,
            )
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_claim_tile_mapping_series (
                    id, brand_id, mapping_series_id, mapping_version,
                    mapping_policy_version, identity_policy_version,
                    claim_memory_policy_version,
                    source_registry_fingerprint, pipeline_version,
                    rubric_version, prompt_version, evaluator_model,
                    claim_projection_version, identity_projection_version,
                    first_seen_at, last_seen_at, report_count,
                    runtime_effect, authority, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, false, false, %s
                )
                ON CONFLICT (brand_id, mapping_series_id) DO UPDATE SET
                    mapping_version = EXCLUDED.mapping_version,
                    mapping_policy_version =
                        EXCLUDED.mapping_policy_version,
                    identity_policy_version =
                        EXCLUDED.identity_policy_version,
                    claim_memory_policy_version =
                        EXCLUDED.claim_memory_policy_version,
                    source_registry_fingerprint =
                        EXCLUDED.source_registry_fingerprint,
                    pipeline_version = EXCLUDED.pipeline_version,
                    rubric_version = EXCLUDED.rubric_version,
                    prompt_version = EXCLUDED.prompt_version,
                    evaluator_model = EXCLUDED.evaluator_model,
                    claim_projection_version =
                        EXCLUDED.claim_projection_version,
                    identity_projection_version =
                        EXCLUDED.identity_projection_version,
                    first_seen_at = EXCLUDED.first_seen_at,
                    last_seen_at = EXCLUDED.last_seen_at,
                    report_count = EXCLUDED.report_count,
                    runtime_effect = false,
                    authority = false,
                    metadata = EXCLUDED.metadata
                """,
                (
                    series_entry_id,
                    brand_id,
                    series_id,
                    str(series.get("mapping_version") or ""),
                    str(series.get("mapping_policy_version") or ""),
                    str(series.get("identity_policy_version") or ""),
                    str(series.get("claim_memory_policy_version") or ""),
                    str(series.get("source_registry_fingerprint") or ""),
                    str(series.get("pipeline_version") or ""),
                    str(series.get("rubric_version") or ""),
                    str(series.get("prompt_version") or ""),
                    str(series.get("evaluator_model") or ""),
                    str(series.get("claim_projection_version") or ""),
                    str(series.get("identity_projection_version") or ""),
                    series.get("first_seen_at"),
                    series.get("last_seen_at"),
                    int(series.get("report_count") or 1),
                    _jsonb(
                        {
                            "report_ids": series.get("report_ids") or [],
                            "latest_report_id": series.get(
                                "latest_report_id"
                            ),
                            "shadow_only": True,
                        }
                    ),
                ),
            )

        for mapping in payload.get("mappings") or []:
            if not isinstance(mapping, dict):
                continue
            mapping_id = str(mapping.get("mapping_id") or "")
            mapping_entry_id = _stable_uuid(
                brand_id,
                "evidence-claim-tile-mapping",
                mapping_id,
            )
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_claim_tile_mappings (
                    id, brand_id, mapping_series_id, mapping_id,
                    source_evidence_id, claim_evidence_id, claim_slot_id,
                    claim_variant_id, component_key, tile_id, tile_key,
                    polarity, source_class, identity_status,
                    source_independence_status, state, first_seen_at,
                    last_seen_at, observation_count,
                    present_in_series_latest, present_in_latest_report,
                    source_evidence_refs, match_methods, quote_hashes,
                    runtime_effect, authority, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, false, false, %s
                )
                ON CONFLICT (brand_id, mapping_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    first_seen_at = EXCLUDED.first_seen_at,
                    last_seen_at = EXCLUDED.last_seen_at,
                    observation_count = EXCLUDED.observation_count,
                    present_in_series_latest =
                        EXCLUDED.present_in_series_latest,
                    present_in_latest_report =
                        EXCLUDED.present_in_latest_report,
                    source_evidence_refs = EXCLUDED.source_evidence_refs,
                    match_methods = EXCLUDED.match_methods,
                    quote_hashes = EXCLUDED.quote_hashes,
                    runtime_effect = false,
                    authority = false,
                    metadata = EXCLUDED.metadata
                """,
                (
                    mapping_entry_id,
                    brand_id,
                    str(mapping.get("mapping_series_id") or ""),
                    mapping_id,
                    str(mapping.get("source_evidence_id") or ""),
                    str(mapping.get("claim_evidence_id") or ""),
                    str(mapping.get("claim_slot_id") or ""),
                    str(mapping.get("claim_variant_id") or ""),
                    str(mapping.get("component_key") or ""),
                    str(mapping.get("tile_id") or ""),
                    str(mapping.get("tile_key") or ""),
                    str(mapping.get("polarity") or ""),
                    str(mapping.get("source_class") or ""),
                    str(mapping.get("identity_status") or ""),
                    str(
                        mapping.get("source_independence_status") or ""
                    ),
                    str(mapping.get("state") or "observed"),
                    mapping.get("first_seen_at"),
                    mapping.get("last_seen_at"),
                    int(mapping.get("observation_count") or 1),
                    bool(mapping.get("present_in_series_latest")),
                    bool(mapping.get("present_in_latest_report")),
                    _jsonb(mapping.get("source_evidence_refs") or []),
                    _jsonb(mapping.get("match_methods") or []),
                    _jsonb(mapping.get("quote_hashes") or []),
                    _jsonb(
                        {
                            "report_ids": mapping.get("report_ids") or [],
                            "shadow_only": True,
                        }
                    ),
                ),
            )
            for observation in mapping.get("observations") or []:
                if not isinstance(observation, dict):
                    continue
                report_id = str(observation.get("report_id") or "")
                report_row = row_by_report_id.get(report_id)
                if report_row is None:
                    continue
                conn.execute(
                    f"""
                    INSERT INTO {_SCHEMA}.evidence_claim_tile_mapping_observations (
                            mapping_entry_id, capture_id, source_report_id,
                            claim_occurrence_id, observed_at, tile_state,
                            quote_hash, match_method,
                            duplicate_observation_count
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                    ON CONFLICT (mapping_entry_id, capture_id)
                    DO UPDATE SET
                        source_report_id = EXCLUDED.source_report_id,
                        claim_occurrence_id =
                            EXCLUDED.claim_occurrence_id,
                        observed_at = EXCLUDED.observed_at,
                        tile_state = EXCLUDED.tile_state,
                        quote_hash = EXCLUDED.quote_hash,
                        match_method = EXCLUDED.match_method,
                        duplicate_observation_count =
                            EXCLUDED.duplicate_observation_count
                    """,
                    (
                        mapping_entry_id,
                        report_row["capture_id"],
                        report_id,
                        str(
                            observation.get("claim_occurrence_id") or ""
                        ),
                        observation.get("observed_at"),
                        str(observation.get("tile_state") or ""),
                        str(observation.get("quote_hash") or ""),
                        str(observation.get("match_method") or ""),
                        int(
                            observation.get(
                                "duplicate_observation_count"
                            )
                            or 1
                        ),
                    ),
                )

        latest_report_id = str(payload.get("latest_report_id") or "")
        latest_row = row_by_report_id.get(latest_report_id)
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.evidence_claim_tile_ledger_states (
                brand_id, latest_capture_id, schema_version,
                policy_version, mapping_version, mode, runtime_effect,
                authority, state_fingerprint, summary, payload, computed_at
            ) VALUES (
                %s, %s, %s, %s, %s, 'shadow', false, false,
                %s, %s, %s, now()
            )
            ON CONFLICT (brand_id) DO UPDATE SET
                latest_capture_id = EXCLUDED.latest_capture_id,
                schema_version = EXCLUDED.schema_version,
                policy_version = EXCLUDED.policy_version,
                mapping_version = EXCLUDED.mapping_version,
                mode = 'shadow',
                runtime_effect = false,
                authority = false,
                state_fingerprint = EXCLUDED.state_fingerprint,
                summary = EXCLUDED.summary,
                payload = EXCLUDED.payload,
                computed_at = now()
            """,
            (
                brand_id,
                latest_row["capture_id"] if latest_row else None,
                str(payload.get("schema_version") or ""),
                str(payload.get("policy_version") or ""),
                str(payload.get("mapping_version") or ""),
                str(payload.get("state_fingerprint") or ""),
                _jsonb(payload.get("summary") or {}),
                _jsonb(payload),
            ),
        )

    @staticmethod
    def _rebuild_brand_stability(conn, workspace_id: UUID, brand_id: UUID) -> None:
        """Recompute the derived canonical projection without mutating snapshots."""

        rows = conn.execute(
            f"""
            SELECT report_snapshots.source_report_id,
                   report_snapshots.payload,
                   evaluation_runs.id AS evaluation_run_id,
                   captures.id AS capture_id
            FROM {_SCHEMA}.report_snapshots
            JOIN {_SCHEMA}.evaluation_runs
              ON evaluation_runs.id = report_snapshots.evaluation_run_id
            JOIN {_SCHEMA}.captures
              ON captures.id = evaluation_runs.capture_id
            WHERE report_snapshots.workspace_id = %s
              AND captures.brand_id = %s
            ORDER BY report_snapshots.created_at, report_snapshots.source_report_id
            """,
            (workspace_id, brand_id),
        ).fetchall()
        reports = [dict(row["payload"]) for row in rows if isinstance(row["payload"], dict)]
        if not reports:
            conn.execute(
                f"DELETE FROM {_SCHEMA}.brand_canonical_selections WHERE brand_id = %s",
                (brand_id,),
            )
            return

        _annotated, state = annotate_report_history(reports)
        entry_by_id = {
            str(entry.get("report_id") or ""): entry
            for entry in state.get("entries") or []
            if isinstance(entry, dict)
        }
        row_by_report_id = {str(row["source_report_id"]): row for row in rows}

        for report in reports:
            report_id = str(report.get("id") or "")
            row = row_by_report_id.get(report_id)
            entry = entry_by_id.get(report_id)
            if row is None or entry is None:
                continue
            snapshot = build_evidence_snapshot(report)
            snapshot_payload = snapshot.to_dict()
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.capture_fingerprints (
                    capture_id, schema_version, material_fingerprint,
                    semantic_fingerprint, component_fingerprints,
                    acquisition_profile, computed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (capture_id) DO UPDATE SET
                    schema_version = EXCLUDED.schema_version,
                    material_fingerprint = EXCLUDED.material_fingerprint,
                    semantic_fingerprint = EXCLUDED.semantic_fingerprint,
                    component_fingerprints = EXCLUDED.component_fingerprints,
                    acquisition_profile = EXCLUDED.acquisition_profile,
                    computed_at = now()
                """,
                (
                    row["capture_id"],
                    snapshot_payload["schema_version"],
                    snapshot.fingerprint,
                    snapshot.semantic_fingerprint,
                    _jsonb(snapshot.component_fingerprints),
                    _jsonb(
                        {
                            "counts": snapshot_payload["counts"],
                            "acquisition": snapshot_payload["acquisition"],
                            "reliability_status": snapshot.reliability_status,
                            "invalid": snapshot.invalid,
                        }
                    ),
                ),
            )
            baseline_id = _comparison_report_id(entry.get("baseline_comparison"), "baseline_report_id")
            previous_id = _comparison_report_id(entry.get("previous_comparison"), "baseline_report_id")
            conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evaluation_comparisons (
                    evaluation_run_id, baseline_evaluation_run_id,
                    previous_evaluation_run_id, schema_version, policy_version,
                    classification, canonical_status, reason_codes, assessment,
                    computed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (evaluation_run_id) DO UPDATE SET
                    baseline_evaluation_run_id = EXCLUDED.baseline_evaluation_run_id,
                    previous_evaluation_run_id = EXCLUDED.previous_evaluation_run_id,
                    schema_version = EXCLUDED.schema_version,
                    policy_version = EXCLUDED.policy_version,
                    classification = EXCLUDED.classification,
                    canonical_status = EXCLUDED.canonical_status,
                    reason_codes = EXCLUDED.reason_codes,
                    assessment = EXCLUDED.assessment,
                    computed_at = now()
                """,
                (
                    row["evaluation_run_id"],
                    _evaluation_id_for_report(row_by_report_id, baseline_id),
                    _evaluation_id_for_report(row_by_report_id, previous_id),
                    str(entry.get("schema_version") or snapshot_payload["schema_version"]),
                    str(entry.get("policy_version") or CANONICAL_POLICY_VERSION),
                    str(entry.get("classification") or "unknown"),
                    str(entry.get("canonical_status") or "non_canonical"),
                    _jsonb(entry.get("reason_codes") or []),
                    _jsonb(entry),
                ),
            )

        selected_report_id = str(state.get("selected_report_id") or "")
        selected = row_by_report_id.get(selected_report_id)
        if selected is None:
            conn.execute(
                f"DELETE FROM {_SCHEMA}.brand_canonical_selections WHERE brand_id = %s",
                (brand_id,),
            )
            return
        selection_status = "canonical" if state.get("canonical_report_id") else "provisional"
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.brand_canonical_selections (
                brand_id, evaluation_run_id, status, policy_version, selected_at
            ) VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (brand_id) DO UPDATE SET
                evaluation_run_id = EXCLUDED.evaluation_run_id,
                status = EXCLUDED.status,
                policy_version = EXCLUDED.policy_version,
                selected_at = now()
            """,
            (
                brand_id,
                selected["evaluation_run_id"],
                selection_status,
                CANONICAL_POLICY_VERSION,
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


def _validate_adjudication_command(
    command: EvidenceMemoryAdjudicationCommand,
) -> None:
    if command.decision not in ADJUDICATION_DECISIONS:
        raise ValueError("unsupported evidence memory adjudication decision")
    if (
        len(command.subject_id) != 64
        or any(character not in "0123456789abcdef" for character in command.subject_id)
    ):
        raise ValueError("evidence adjudication subject_id must be a lowercase SHA-256 digest")
    for field, value, maximum in (
        ("reviewer", command.reviewer, 200),
        ("actor_id", command.actor_id, 200),
        ("evaluator_version", command.evaluator_version, 200),
        ("rationale", command.rationale, 2000),
    ):
        if not value or len(value) > maximum or "\x00" in value:
            raise ValueError(f"invalid evidence adjudication {field}")
    if (
        not command.reason_code
        or len(command.reason_code) > 100
        or not command.reason_code[0].isalnum()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in command.reason_code
        )
    ):
        raise ValueError("invalid evidence adjudication reason_code")
    for field, value in (
        ("idempotency_key_hash", command.idempotency_key_hash),
        ("request_fingerprint", command.request_fingerprint),
    ):
        if (
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"invalid evidence adjudication {field}")


def _adjudication_event(
    row: Any,
    *,
    effective_state: str | None = None,
) -> dict[str, Any]:
    created_at = row["created_at"]
    return {
        "id": str(row["id"]),
        "subject_type": str(row["subject_type"]),
        "subject_id": str(row["subject_id"]),
        "sequence": int(row["sequence"]),
        "decision": str(row["decision"]),
        "effective_state": effective_state or str(row["decision"]),
        "supersedes_event_id": (
            str(row["supersedes_event_id"])
            if row["supersedes_event_id"] is not None
            else None
        ),
        "schema_version": str(row["schema_version"]),
        "policy_version": str(row["policy_version"]),
        "evaluator_version": str(row["evaluator_version"]),
        "reviewer": str(row["reviewer"]),
        "actor_id": str(row["actor_id"]),
        "reason_code": str(row["reason_code"]),
        "rationale": str(row["rationale"]),
        "runtime_effect": False,
        "authority": False,
        "created_at": (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at)
        ),
    }


def _validate_claim_reconciliation_command(
    command: EvidenceClaimReconciliationCommand,
    *,
    relation_type: str,
) -> None:
    if command.decision not in CLAIM_RECONCILIATION_DECISIONS:
        raise ValueError("unsupported evidence claim reconciliation decision")
    if relation_type not in CLAIM_RECONCILIATION_RELATION_TYPES:
        raise ValueError("unsupported evidence claim relation type")
    if (
        len(command.subject_id) != 64
        or any(
            character not in "0123456789abcdef"
            for character in command.subject_id
        )
    ):
        raise ValueError(
            "claim reconciliation subject_id must be a lowercase SHA-256 digest"
        )
    for field, value, maximum in (
        ("reviewer", command.reviewer, 200),
        ("actor_id", command.actor_id, 200),
        ("evaluator_version", command.evaluator_version, 200),
        ("rationale", command.rationale, 2000),
    ):
        if not value or len(value) > maximum or "\x00" in value:
            raise ValueError(f"invalid claim reconciliation {field}")
    if (
        not command.reason_code
        or len(command.reason_code) > 100
        or not command.reason_code[0].isalnum()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in command.reason_code
        )
    ):
        raise ValueError("invalid claim reconciliation reason_code")
    for field, value in (
        ("idempotency_key_hash", command.idempotency_key_hash),
        ("request_fingerprint", command.request_fingerprint),
    ):
        if (
            len(value) != 64
            or any(
                character not in "0123456789abcdef"
                for character in value
            )
        ):
            raise ValueError(f"invalid claim reconciliation {field}")


def _claim_reconciliation_event(
    row: Any,
    *,
    effective_state: str | None = None,
) -> dict[str, Any]:
    created_at = row["created_at"]
    return {
        "id": str(row["id"]),
        "subject_type": str(row["subject_type"]),
        "subject_id": str(row["subject_id"]),
        "relation_type": str(row["relation_type"]),
        "sequence": int(row["sequence"]),
        "decision": str(row["decision"]),
        "effective_state": effective_state or str(row["decision"]),
        "supersedes_event_id": (
            str(row["supersedes_event_id"])
            if row["supersedes_event_id"] is not None
            else None
        ),
        "schema_version": str(row["schema_version"]),
        "policy_version": str(row["policy_version"]),
        "evaluator_version": str(row["evaluator_version"]),
        "reviewer": str(row["reviewer"]),
        "actor_id": str(row["actor_id"]),
        "reason_code": str(row["reason_code"]),
        "rationale": str(row["rationale"]),
        "runtime_effect": False,
        "authority": False,
        "created_at": (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at)
        ),
    }


def _comparison_report_id(value: Any, key: str) -> str:
    return str(value.get(key) or "") if isinstance(value, dict) else ""


def _evaluation_id_for_report(rows: dict[str, Any], report_id: str):
    row = rows.get(str(report_id or ""))
    return row["evaluation_run_id"] if row is not None else None


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
