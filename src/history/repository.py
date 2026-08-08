"""Synchronous PostgreSQL repository for immutable B3S history."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from importlib import resources
import logging
import re
from threading import Lock
from typing import Any, Callable, Iterable, Mapping
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.history.capture_observation import parse_capture_observation
from src.history.models import (
    CaptureConflictError,
    CaptureImportOutcome,
    CaptureObservation,
    HistoricalReport,
    ImportOutcome,
    OperationalAdoptionCommand,
    ReportConflictError,
)
from src.history.report_parser import (
    canonical_json_bytes,
    canonical_json_hash,
    normalize_domain,
    parse_report,
)
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
from src.services.evidence_claim_tile_review import (
    CLAIM_TILE_REVIEW_DECISIONS,
    CLAIM_TILE_REVIEW_SUBJECT_TYPE,
    EVIDENCE_CLAIM_TILE_REVIEW_EVENT_VERSION,
    EVIDENCE_CLAIM_TILE_REVIEW_POLICY_VERSION,
    EvidenceClaimTileReviewCommand,
    EvidenceClaimTileReviewConflictError,
    EvidenceClaimTileReviewInvalidTransitionError,
    EvidenceClaimTileReviewNotFoundError,
    EvidenceClaimTileReviewPacketNotFoundError,
    EvidenceClaimTileReviewUnavailableError,
    claim_tile_review_case_id,
)
from src.services.evidence_claim_tile_review_packet import (
    EvidenceClaimTileReviewPacketError,
    build_evidence_claim_tile_review_packet,
    build_review_template,
    packet_candidate_for_subject,
    validate_evidence_claim_tile_review_packet,
)
from src.services.evidence_claim_tile_review_queue import (
    build_evidence_claim_tile_review_queue,
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
from src.services.evidence_memory_identity_v2 import (
    build_accepted_evidence_passage_catalog,
    project_evidence_memory_row_identity,
)
from src.services.evidence_reviewed_claim_tile_memory import (
    build_reviewed_claim_tile_memory_from_journal_shadow,
)
from src.services.evidence_scoring_recovery_review import (
    EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION,
    EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION,
    REVIEW_DECISIONS as SCORING_RECOVERY_REVIEW_DECISIONS,
    SCORING_RECOVERY_REVIEW_SUBJECT_TYPE,
    EvidenceScoringRecoveryReviewCommand,
    EvidenceScoringRecoveryReviewConflictError,
    EvidenceScoringRecoveryReviewError,
    EvidenceScoringRecoveryReviewInvalidTransitionError,
    EvidenceScoringRecoveryReviewNotFoundError,
    build_reviewed_scoring_memory_shadow,
    merge_recovery_review_supplements,
    validate_recovery_review_supplement_against_preview,
    validate_recovery_review_supplement_packet,
)
from src.services.evidence_ledger_shadow import (
    build_evidence_ledger_shadow,
    evidence_ledger_mode,
)
from src.services.evidence_vault_canonical_authority import (
    CanonicalMemoryPromotionCommand,
    EvidenceVaultCanonicalAuthorityError,
    EvidenceVaultCanonicalPacketNotFoundError,
    EvidenceVaultCanonicalPromotionConflictError,
    EvidenceVaultCanonicalUnavailableError,
    build_promotion_event,
    build_reference_resolution,
    plan_canonical_memory_promotion,
    promotion_request_fingerprint,
    project_promoted_canonical_memory,
    validate_promotion_event,
)
from src.services.evidence_vault_canonical_core import (
    EvidenceVaultCanonicalCoreError,
    build_candidate_tile,
    build_tile_contract_registry,
    canonical_fingerprint,
    validate_candidate_packet,
    validate_incremental_candidate_tiles,
)
from src.services.evidence_vault_candidate_resolver import (
    EvidenceVaultCandidateResolverError,
    build_resolved_canonical_memory_candidate,
)
from src.services.evidence_vault_coverage_supplement import (
    EvidenceVaultCoverageSupplementError,
    build_coverage_supplement_source_candidate,
    build_coverage_supplement_source_resolution,
    validate_coverage_supplement_artifact,
)
from src.services.evidence_vault_canonical_scoring import (
    EvidenceVaultCanonicalScoringError,
    build_canonical_score_evaluation,
    validate_canonical_score_evaluation,
)
from src.services.evidence_vault_composite_group_lifecycle import (
    COMPOSITE_GROUP_REOPEN_POLICY_ACTOR,
    EvidenceVaultCompositeGroupLifecycleError,
    attest_active_composite_group,
    build_composite_group_reopen_artifact,
    build_composite_group_reopen_operational_packet,
    build_composite_group_reopen_source_candidate,
    build_composite_group_reopen_source_resolution,
    validate_composite_group_reopen_source_resolution,
)
from src.services.evidence_vault_exact_relation_supplement import (
    EvidenceVaultExactRelationSupplementError,
    build_exact_relation_source_candidate,
    build_exact_relation_source_resolution,
    validate_exact_relation_source_decisions,
    validate_exact_relation_supplement_artifact,
    validate_exact_relation_supplement_structure,
)
from src.services.evidence_vault_incremental_refresh import (
    EvidenceVaultOperationPlanError,
    validate_vault_scan_plan,
)
from src.services.evidence_vault_operational_authority import (
    EvidenceVaultOperationalAdoptionConflictError,
    EvidenceVaultOperationalAuthorityError,
    adoption_request_fingerprint,
    build_operational_adoption_event,
    project_adopted_operational_memory,
    validate_operational_adoption_event,
    validate_operational_memory_packet,
)
from src.services.evidence_vault_authority_profiles import (
    build_initial_authority_profile_matrix,
    evaluate_reviewed_basis_authority,
    validate_authority_decision,
)
from src.services.evidence_vault_operational_candidate import (
    build_operational_packet_from_reviewed_candidate,
    build_provisional_operational_packet_from_report,
)
from src.services.evidence_vault_operational_review import (
    EvidenceVaultOperationalReviewError,
    build_reviewed_operational_source,
)
from src.services.evidence_vault_operational_scoring import (
    EvidenceVaultOperationalScoringError,
    build_operational_score_evaluation,
    validate_operational_score_evaluation,
)
from src.services.scanner_evidence_comparison import (
    CANONICAL_POLICY_VERSION,
    annotate_report_history,
    build_evidence_snapshot,
    canonical_evidence_representatives,
)
from src.sv9_flow.contracts import EvidenceRecord
from src.sv9_flow.evidence_labeling_worker import is_evidence_record_labelable

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
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(_SCHEMA, "schema-migrations-v1"),),
            )
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

            capture_only = conn.execute(
                f"""
                SELECT scan_runs.id AS scan_run_id,
                       scan_runs.brand_id,
                       scan_runs.request_payload,
                       scan_runs.metadata AS scan_metadata,
                       captures.id AS capture_id,
                       captures.content_hash,
                       captures.source_url,
                       captures.observed_at,
                       brands.canonical_domain
                FROM {_SCHEMA}.scan_runs
                JOIN {_SCHEMA}.captures ON captures.scan_run_id = scan_runs.id
                JOIN {_SCHEMA}.brands ON brands.id = scan_runs.brand_id
                WHERE scan_runs.workspace_id = %s
                  AND scan_runs.source_scan_id = %s
                """,
                (workspace_id, parsed.source_report_id),
            ).fetchone()
            brand_id = self._upsert_brand(conn, workspace_id, parsed)
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(workspace_id, "brand", brand_id),),
            )
            if capture_only is None:
                scan_run_id = _stable_uuid(
                    workspace_id,
                    "scan",
                    parsed.source_report_id,
                )
                capture_id = _stable_uuid(scan_run_id, "capture")
                self._insert_scan_run(conn, scan_run_id, workspace_id, brand_id, parsed)
                self._insert_capture(conn, capture_id, scan_run_id, brand_id, parsed)
                evidence_ids = self._insert_evidence(conn, capture_id, parsed)
            else:
                if str(capture_only["brand_id"]) != str(brand_id):
                    raise ReportConflictError(
                        "capture-only scan belongs to another brand"
                    )
                scan_run_id = capture_only["scan_run_id"]
                capture_id = capture_only["capture_id"]
                evidence_ids = _validate_capture_to_report_upgrade(
                    conn,
                    capture_only,
                    parsed,
                )
                conn.execute(
                    f"""
                    UPDATE {_SCHEMA}.scan_runs
                    SET source_run_id = %s,
                        status = 'completed',
                        pipeline_version = %s,
                        acquisition_state = %s,
                        completed_at = %s,
                        recorded_at = %s,
                        metadata = metadata || %s
                    WHERE id = %s
                    """,
                    (
                        parsed.source_run_id,
                        parsed.pipeline_version,
                        parsed.acquisition_state,
                        parsed.recorded_at,
                        parsed.recorded_at,
                        _jsonb(
                            {
                                "persisted_as": "capture_with_report",
                                "report_hash": parsed.report_hash,
                            }
                        ),
                        scan_run_id,
                    ),
                )
            evaluation_run_id = _stable_uuid(
                capture_id,
                "evaluation",
                parsed.source_report_id,
            )
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
            if capture_only is None:
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

    def persist_capture_observation(
        self,
        observation: dict[str, Any],
        *,
        workspace_slug: str = "b3s",
        workspace_name: str = "B3S",
    ) -> CaptureImportOutcome:
        """Persist one validated raw acquisition without inventing an evaluation."""

        # Hashes are always recomputed from caller data at this trust boundary.
        # Preconstructed dataclasses are deliberately rejected so a caller
        # cannot forge observation_hash/capture_hash or mutate nested payloads.
        parsed = parse_capture_observation(observation)
        operation_plan = parsed.metadata.get("operation_plan")
        if operation_plan is not None:
            try:
                validate_vault_scan_plan(operation_plan)
            except EvidenceVaultOperationPlanError as exc:
                raise CaptureConflictError(
                    "capture operation plan is invalid"
                ) from exc
            if parsed.metadata.get("operation_plan_fingerprint") != operation_plan.get(
                "operation_plan_fingerprint"
            ):
                raise CaptureConflictError(
                    "capture operation plan fingerprint is inconsistent"
                )
            operations = operation_plan["operations"]
            work_required = bool(
                operations["llm_required"]
                or operations["create_candidate_packet"]
                or operations["create_diagnostic_report"]
            )
            expected_status = "pending" if work_required else "not_required"
            if parsed.metadata.get("analysis_status") != expected_status:
                raise CaptureConflictError(
                    "capture analysis status does not match its operation plan"
                )
        self._ensure_migrated()
        with self._connect() as conn:
            workspace_id = self._ensure_workspace(
                conn,
                workspace_slug,
                workspace_name,
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(workspace_id, parsed.source_scan_id),),
            )
            existing = conn.execute(
                f"""
                SELECT scan_runs.metadata ->> 'observation_hash' AS observation_hash,
                       brands.id AS brand_id,
                       captures.id AS capture_id
                FROM {_SCHEMA}.scan_runs
                JOIN {_SCHEMA}.captures ON captures.scan_run_id = scan_runs.id
                JOIN {_SCHEMA}.brands ON brands.id = captures.brand_id
                WHERE scan_runs.workspace_id = %s
                  AND scan_runs.source_scan_id = %s
                """,
                (workspace_id, parsed.source_scan_id),
            ).fetchone()
            if existing:
                if str(existing.get("observation_hash") or "") != parsed.observation_hash:
                    raise CaptureConflictError(
                        f"capture {parsed.source_scan_id} already exists with different content"
                    )
                if operation_plan is not None:
                    plan_row = _vault_operation_row(
                        conn,
                        workspace_slug=workspace_slug,
                        source_scan_id=parsed.source_scan_id,
                        for_update=False,
                    )
                    if plan_row is None:
                        raise CaptureConflictError(
                            "persisted capture is missing its operation plan"
                        )
                    plan_record = _vault_operation_plan_record(plan_row)
                    if (
                        plan_record["operation_plan_fingerprint"]
                        != operation_plan["operation_plan_fingerprint"]
                        or plan_record["observation_hash"]
                        != parsed.observation_hash
                    ):
                        raise CaptureConflictError(
                            "persisted capture operation identity mismatch"
                        )
                return CaptureImportOutcome(
                    source_scan_id=parsed.source_scan_id,
                    status="unchanged",
                    brand_id=str(existing["brand_id"]),
                    capture_id=str(existing["capture_id"]),
                    observation_hash=parsed.observation_hash,
                )

            brand_id = self._upsert_brand(conn, workspace_id, parsed)
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(workspace_id, "brand", brand_id),),
            )
            scan_run_id = _stable_uuid(
                workspace_id,
                "scan",
                parsed.source_scan_id,
            )
            capture_id = _stable_uuid(scan_run_id, "capture")
            self._insert_capture_only_scan_run(
                conn,
                scan_run_id,
                workspace_id,
                brand_id,
                parsed,
            )
            if operation_plan is not None:
                self._insert_vault_operation_plan(
                    conn,
                    workspace_id=workspace_id,
                    brand_id=brand_id,
                    scan_run_id=scan_run_id,
                    observation_hash=parsed.observation_hash,
                    plan=operation_plan,
                    initial_status=str(parsed.metadata["analysis_status"]),
                )
            self._insert_capture_only(
                conn,
                capture_id,
                scan_run_id,
                brand_id,
                parsed,
            )
            self._insert_evidence(conn, capture_id, parsed)
            self._insert_attempts(conn, capture_id, parsed)
            self._insert_artifacts(conn, capture_id, parsed)
            _append_evidence_vault_capture_watermark_event(
                conn,
                brand_id=brand_id,
                capture_id=capture_id,
                capture_content_hash=parsed.capture_hash,
                capture_observation_hash=parsed.observation_hash,
                capture_metadata=parsed.metadata,
            )
            return CaptureImportOutcome(
                source_scan_id=parsed.source_scan_id,
                status="imported",
                brand_id=str(brand_id),
                capture_id=str(capture_id),
                observation_hash=parsed.observation_hash,
            )

    def get_evidence_vault_current_capture_watermark(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Return the exact brand-serialized capture head, if one exists.

        The head is commit ordered by the append-only database sequence.  Caller
        supplied observation timestamps never participate in this selection and
        captures written before migration 017 are deliberately not backfilled.
        """

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT events.*, brands.canonical_domain
                FROM {_SCHEMA}.evidence_vault_capture_watermark_events AS events
                JOIN {_SCHEMA}.brands ON brands.id = events.brand_id
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                ORDER BY events.capture_sequence DESC
                LIMIT 1
                """,
                (workspace_slug, domain),
            ).fetchone()
            return _evidence_vault_capture_watermark_record(row) if row else None

    # Kept as a narrow semantic alias while the lineage/cutover integration
    # settles on its final public name.
    get_current_exact_capture_watermark = (
        get_evidence_vault_current_capture_watermark
    )

    def get_capture_operation_plan(
        self,
        source_scan_id: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Load one exact persisted plan directly by scan identity."""

        scan_id = str(source_scan_id or "").strip()
        if not scan_id:
            raise CaptureConflictError("source_scan_id is required")
        self._ensure_migrated()
        with self._connect() as conn:
            row = _vault_operation_row(
                conn,
                workspace_slug=workspace_slug,
                source_scan_id=scan_id,
                for_update=False,
            )
            if row is None:
                return None
            evidence_rows = conn.execute(
                f"""
                SELECT evidence_ref, source, source_class, evidence_type,
                       url, content, content_raw, confidence, metadata
                FROM {_SCHEMA}.evidence_records
                WHERE capture_id = %s
                ORDER BY evidence_ref, id
                """,
                (row["capture_id"],),
            ).fetchall()
            evidence = _capture_evidence_rows(evidence_rows)
            record = _vault_operation_plan_record(row)
            record["lease_token"] = None
            record.update(
                {
                    "source_scan_id": scan_id,
                    "brand_identity": str(row["canonical_domain"]),
                    "capture_id": str(row["capture_id"]),
                    "capture_hash": str(row["capture_hash"]),
                    "raw_observation": dict(row["request_payload"] or {}),
                    "evidence_records": evidence,
                }
            )
            return record

    def claim_capture_operation_plan(
        self,
        source_scan_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        """Lease one plan with a monotonic generation that fences old workers."""

        scan_id = str(source_scan_id or "").strip()
        worker = _bounded_text(worker_id, field="worker_id", maximum=200)
        if not scan_id:
            raise CaptureConflictError("source_scan_id is required")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
        ):
            raise CaptureConflictError("lease_seconds must be between 1 and 3600")
        self._ensure_migrated()
        with self._connect() as conn:
            row = _vault_operation_row(
                conn,
                workspace_slug=workspace_slug,
                source_scan_id=scan_id,
                for_update=True,
            )
            if row is None:
                raise CaptureConflictError("capture operation plan does not exist")
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(row["brand_id"], "evidence-vault-canonical-promotion"),),
            )
            record = _vault_operation_plan_record(row)
            if record["status"] in {"completed", "result_persisted", "superseded"}:
                return {**record, "claim_status": record["status"], "claimed": False}
            current = _project_vault_operational_memory(conn, row["brand_id"])
            current_version = (
                current["canonical_memory_version"] if current is not None else None
            )
            if (
                record["mode"] != "diagnostic_full"
                and current_version != record["canonical_memory_version"]
            ):
                superseded = conn.execute(
                    f"""
                    UPDATE {_SCHEMA}.evidence_vault_operation_plans
                    SET status = 'superseded', superseded_at = now(),
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error = 'canonical_parent_superseded'
                    WHERE id = %s
                    RETURNING *
                    """,
                    (row["id"],),
                ).fetchone()
                return {
                    **_vault_operation_plan_record(superseded),
                    "claim_status": "superseded",
                    "claimed": False,
                }
            lease_active = (
                record["status"] in {"claimed", "running"}
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > row["db_now"]
            )
            if lease_active:
                same_owner = str(row["lease_owner"] or "") == worker
                return {
                    **record,
                    "lease_token": None,
                    "claim_status": "owned_active" if same_owner else "busy",
                    "claimed": False,
                }
            token = uuid4()
            claimed = conn.execute(
                f"""
                UPDATE {_SCHEMA}.evidence_vault_operation_plans
                SET status = 'claimed',
                    attempt_count = attempt_count + 1,
                    lease_owner = %s,
                    lease_token = %s,
                    lease_generation = lease_generation + 1,
                    lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                    claimed_at = now(), heartbeat_at = now(),
                    started_at = NULL, last_error = ''
                WHERE id = %s
                RETURNING *
                """,
                (worker, token, lease_seconds, row["id"]),
            ).fetchone()
            return {
                **_vault_operation_plan_record(claimed),
                "claim_status": "acquired",
                "claimed": True,
            }

    def mark_capture_operation_running(
        self,
        source_scan_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        lease_seconds: int = 300,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        return self._advance_capture_operation_lease(
            source_scan_id,
            worker_id=worker_id,
            lease_token=lease_token,
            lease_generation=lease_generation,
            lease_seconds=lease_seconds,
            workspace_slug=workspace_slug,
            start=True,
        )

    def heartbeat_capture_operation_plan(
        self,
        source_scan_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        lease_seconds: int = 300,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        return self._advance_capture_operation_lease(
            source_scan_id,
            worker_id=worker_id,
            lease_token=lease_token,
            lease_generation=lease_generation,
            lease_seconds=lease_seconds,
            workspace_slug=workspace_slug,
            start=False,
        )

    def _advance_capture_operation_lease(
        self,
        source_scan_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        lease_seconds: int,
        workspace_slug: str,
        start: bool,
    ) -> dict[str, Any]:
        scan_id = str(source_scan_id or "").strip()
        worker = _bounded_text(worker_id, field="worker_id", maximum=200)
        token = _uuid_text(lease_token, field="lease_token")
        generation = _positive_int(lease_generation, field="lease_generation")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
        ):
            raise CaptureConflictError("lease_seconds must be between 1 and 3600")
        self._ensure_migrated()
        with self._connect() as conn:
            row = _vault_operation_row(
                conn,
                workspace_slug=workspace_slug,
                source_scan_id=scan_id,
                for_update=True,
            )
            if row is None:
                raise CaptureConflictError("capture operation plan does not exist")
            allowed = {"claimed"} if start else {"claimed", "running"}
            if str(row["status"]) not in allowed or not _lease_matches(
                row,
                worker_id=worker,
                lease_token=token,
                lease_generation=generation,
            ):
                raise CaptureConflictError("operation lease is stale or owned by another worker")
            if row["lease_expires_at"] <= row["db_now"]:
                raise CaptureConflictError("operation lease has expired")
            updated = conn.execute(
                f"""
                UPDATE {_SCHEMA}.evidence_vault_operation_plans
                SET status = %s,
                    started_at = CASE WHEN %s THEN COALESCE(started_at, now()) ELSE started_at END,
                    heartbeat_at = now(),
                    lease_expires_at = clock_timestamp() + make_interval(secs => %s)
                WHERE id = %s
                RETURNING *
                """,
                ("running" if start else str(row["status"]), start, lease_seconds, row["id"]),
            ).fetchone()
            return _vault_operation_plan_record(updated)

    def persist_capture_operation_result(
        self,
        source_scan_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        result_payload: Mapping[str, Any],
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        """Persist exact analyzer output once; late fenced workers cannot write."""

        scan_id = str(source_scan_id or "").strip()
        worker = _bounded_text(worker_id, field="worker_id", maximum=200)
        token = _uuid_text(lease_token, field="lease_token")
        generation = _positive_int(lease_generation, field="lease_generation")
        detached = _strict_json_object(result_payload, field="result_payload")
        result_fingerprint = canonical_fingerprint(
            "evidence-vault-operation-result-v1",
            detached,
        )
        self._ensure_migrated()
        with self._connect() as conn:
            row = _vault_operation_row(
                conn,
                workspace_slug=workspace_slug,
                source_scan_id=scan_id,
                for_update=True,
            )
            if row is None:
                raise CaptureConflictError("capture operation plan does not exist")
            _validate_vault_operation_result_for_plan(
                conn,
                detached,
                operation=row,
            )
            existing_result = str(row["result_fingerprint"] or "")
            if existing_result:
                if existing_result != result_fingerprint or dict(
                    row["result_payload"] or {}
                ) != detached:
                    raise CaptureConflictError(
                        "operation already has a different immutable result"
                    )
                return {**_vault_operation_plan_record(row), "result_replayed": True}
            if str(row["status"]) != "running" or not _lease_matches(
                row,
                worker_id=worker,
                lease_token=token,
                lease_generation=generation,
            ):
                raise CaptureConflictError("operation result writer has a stale lease")
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(row["brand_id"], "evidence-vault-canonical-promotion"),),
            )
            wall_clock = conn.execute(
                "SELECT clock_timestamp() AS value"
            ).fetchone()["value"]
            if row["lease_expires_at"] <= wall_clock:
                raise CaptureConflictError("operation result lease has expired")
            current = _project_vault_operational_memory(conn, row["brand_id"])
            current_version = (
                current["canonical_memory_version"] if current is not None else None
            )
            stale_parent = (
                str(row["mode"]) != "diagnostic_full"
                and current_version != row["canonical_memory_version"]
            )
            updated = conn.execute(
                f"""
                UPDATE {_SCHEMA}.evidence_vault_operation_plans
                SET status = %s,
                    result_fingerprint = %s,
                    result_payload = %s,
                    result_persisted_at = now(),
                    superseded_at = CASE WHEN %s THEN now() ELSE NULL END,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = CASE WHEN %s
                        THEN 'canonical_parent_superseded' ELSE '' END
                WHERE id = %s
                RETURNING *
                """,
                (
                    "superseded" if stale_parent else "result_persisted",
                    result_fingerprint,
                    _jsonb(detached),
                    stale_parent,
                    stale_parent,
                    row["id"],
                ),
            ).fetchone()
            return {
                **_vault_operation_plan_record(updated),
                "result_replayed": False,
            }

    def finalize_capture_operation_plan(
        self,
        source_scan_id: str,
        *,
        operation_plan_fingerprint: str,
        result_fingerprint: str,
        candidate_packet_fingerprint: str | None = None,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        """Finalize only a stored result and an idempotently persisted output."""

        scan_id = str(source_scan_id or "").strip()
        plan_fingerprint = _require_sha256_text(
            operation_plan_fingerprint,
            field="operation_plan_fingerprint",
        )
        expected_result = _require_sha256_text(
            result_fingerprint,
            field="result_fingerprint",
        )
        candidate_fingerprint = (
            _require_sha256_text(
                candidate_packet_fingerprint,
                field="candidate_packet_fingerprint",
            )
            if candidate_packet_fingerprint is not None
            else None
        )
        self._ensure_migrated()
        with self._connect() as conn:
            row = _vault_operation_row(
                conn,
                workspace_slug=workspace_slug,
                source_scan_id=scan_id,
                for_update=True,
            )
            if row is None:
                raise CaptureConflictError("capture operation plan does not exist")
            record = _vault_operation_plan_record(row)
            if record["operation_plan_fingerprint"] != plan_fingerprint:
                raise CaptureConflictError("operation plan fingerprint mismatch")
            if record.get("result_fingerprint") != expected_result:
                raise CaptureConflictError("operation result fingerprint mismatch")
            if record["status"] == "completed":
                if record.get("candidate_packet_fingerprint") != candidate_fingerprint:
                    raise CaptureConflictError(
                        "completed operation references another output packet"
                    )
                return {**record, "finalization_replayed": True}
            if record["status"] == "superseded":
                return {**record, "finalization_replayed": True}
            if record["status"] != "result_persisted":
                raise CaptureConflictError(
                    "operation must persist its result before finalization"
                )
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(row["brand_id"], "evidence-vault-canonical-promotion"),),
            )
            current = _project_vault_operational_memory(conn, row["brand_id"])
            current_version = (
                current["canonical_memory_version"] if current is not None else None
            )
            if (
                record["mode"] != "diagnostic_full"
                and current_version != record["canonical_memory_version"]
            ):
                superseded = conn.execute(
                    f"""
                    UPDATE {_SCHEMA}.evidence_vault_operation_plans
                    SET status = 'superseded', superseded_at = now(),
                        last_error = 'canonical_parent_superseded'
                    WHERE id = %s
                    RETURNING *
                    """,
                    (row["id"],),
                ).fetchone()
                return {
                    **_vault_operation_plan_record(superseded),
                    "finalization_replayed": False,
                }
            result_payload = dict(row["result_payload"] or {})
            output_kind = str(result_payload.get("output_kind") or "")
            if output_kind == "candidate_overlay":
                if candidate_fingerprint is None or result_payload.get(
                    "candidate_packet_fingerprint"
                ) != candidate_fingerprint:
                    raise CaptureConflictError(
                        "candidate result is not bound to its stored packet"
                    )
                source_fingerprint = str(
                    result_payload.get(
                        "source_candidate_packet_fingerprint"
                    )
                    or ""
                )
                expected_source_resolution = {
                    "schema_version": "evidence-vault-operational-source-resolution-v1",
                    "operation_plan_fingerprint": plan_fingerprint,
                    "observation_hash": str(row["observation_hash"]),
                    "result_fingerprint": expected_result,
                    "source_candidate_packet_fingerprint": source_fingerprint,
                }
                source_resolution_fingerprint = canonical_fingerprint(
                    "evidence-vault-operational-source-resolution-v1",
                    expected_source_resolution,
                )
                source_row = conn.execute(
                    f"""
                    SELECT *
                    FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                    WHERE brand_id = %s
                      AND packet_fingerprint = %s
                      AND reference_resolution_fingerprint = %s
                      AND packet_kind = 'operational_source_v2'
                    """,
                    (
                        row["brand_id"],
                        source_fingerprint,
                        source_resolution_fingerprint,
                    ),
                ).fetchone()
                if source_row is None:
                    raise CaptureConflictError(
                        "executor source packet is not durably registered"
                    )
                source_record = _vault_operational_source_packet_record(
                    source_row
                )
                if (
                    source_record["packet"]
                    != result_payload.get("source_candidate_packet")
                    or source_record["reference_resolution"]
                    != expected_source_resolution
                ):
                    raise CaptureConflictError(
                        "executor source packet is not bound to its immutable result"
                    )
                operational_candidate = result_payload.get(
                    "operational_candidate_packet"
                )
                if not isinstance(operational_candidate, Mapping):
                    raise CaptureConflictError(
                        "stored executor candidate packet is missing"
                    )
                operational_resolution = _operational_storage_resolution(
                    dict(operational_candidate)
                )
                packet_row = conn.execute(
                    f"""
                    SELECT *
                    FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                    WHERE brand_id = %s
                      AND packet_fingerprint = %s
                      AND reference_resolution_fingerprint = %s
                      AND packet_kind = 'operational_v2'
                    """,
                    (
                        row["brand_id"],
                        candidate_fingerprint,
                        operational_resolution[
                            "reference_resolution_fingerprint"
                        ],
                    ),
                ).fetchone()
                if packet_row is None:
                    raise CaptureConflictError(
                        "candidate result packet is not durably registered"
                    )
                packet = _vault_operational_packet_record(packet_row)["packet"]
                if result_payload.get("operational_candidate_packet") != packet:
                    raise CaptureConflictError(
                        "stored executor packet differs from the immutable result"
                    )
                if (
                    packet["has_accepted_change"] is not False
                    or packet["current_canonical_memory_version"]
                    != record["canonical_memory_version"]
                ):
                    raise CaptureConflictError(
                        "executor candidate packet crosses the authority boundary"
                    )
            elif output_kind == "no_delta":
                if candidate_fingerprint is not None:
                    raise CaptureConflictError("no-delta result cannot reference a packet")
                operations = record["plan"]["operations"]
                if (
                    operations["llm_required"]
                    or operations["create_candidate_packet"]
                    or operations["create_diagnostic_report"]
                ):
                    raise CaptureConflictError(
                        "no-delta result conflicts with the stored work contract"
                    )
            else:
                raise CaptureConflictError(
                    f"unsupported executor output kind: {output_kind or 'missing'}"
                )
            completed = conn.execute(
                f"""
                UPDATE {_SCHEMA}.evidence_vault_operation_plans
                SET status = 'completed',
                    candidate_packet_fingerprint = %s,
                    completed_at = now(), last_error = ''
                WHERE id = %s
                RETURNING *
                """,
                (candidate_fingerprint, row["id"]),
            ).fetchone()
            conn.execute(
                f"""
                UPDATE {_SCHEMA}.scan_runs
                SET metadata = metadata || %s
                WHERE id = %s
                """,
                (
                    _jsonb(
                        {
                            "analysis_status": "completed",
                            "analysis_result_fingerprint": expected_result,
                            "candidate_packet_fingerprint": candidate_fingerprint,
                        }
                    ),
                    row["scan_run_id"],
                ),
            )
            return {
                **_vault_operation_plan_record(completed),
                "finalization_replayed": False,
            }

    def fail_capture_operation_plan(
        self,
        source_scan_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        error: str,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        scan_id = str(source_scan_id or "").strip()
        worker = _bounded_text(worker_id, field="worker_id", maximum=200)
        token = _uuid_text(lease_token, field="lease_token")
        generation = _positive_int(lease_generation, field="lease_generation")
        message = _bounded_text(error, field="error", maximum=4000)
        self._ensure_migrated()
        with self._connect() as conn:
            row = _vault_operation_row(
                conn,
                workspace_slug=workspace_slug,
                source_scan_id=scan_id,
                for_update=True,
            )
            if row is None or str(row["status"]) not in {"claimed", "running"}:
                raise CaptureConflictError("capture operation has no active lease")
            if not _lease_matches(
                row,
                worker_id=worker,
                lease_token=token,
                lease_generation=generation,
            ):
                raise CaptureConflictError("operation failure writer has a stale lease")
            if row["lease_expires_at"] <= row["db_now"]:
                raise CaptureConflictError("operation failure lease has expired")
            failed = conn.execute(
                f"""
                UPDATE {_SCHEMA}.evidence_vault_operation_plans
                SET status = 'failed_retryable', last_error = %s,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL
                WHERE id = %s
                RETURNING *
                """,
                (message, row["id"]),
            ).fetchone()
            return _vault_operation_plan_record(failed)

    def mark_capture_analysis_completed(
        self,
        source_scan_id: str,
        *,
        operation_plan_fingerprint: str,
        analysis_result_fingerprint: str,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        """Compatibility finalizer; it cannot create or forge a result."""

        operation = self.get_capture_operation_plan(
            source_scan_id,
            workspace_slug=workspace_slug,
        )
        if operation is None or operation.get("result_payload") is None:
            raise CaptureConflictError(
                "analysis result must be durably persisted before completion"
            )
        candidate = operation["result_payload"].get(
            "candidate_packet_fingerprint"
        )
        return self.finalize_capture_operation_plan(
            source_scan_id,
            operation_plan_fingerprint=operation_plan_fingerprint,
            result_fingerprint=analysis_result_fingerprint,
            candidate_packet_fingerprint=(
                str(candidate) if candidate is not None else None
            ),
            workspace_slug=workspace_slug,
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

    def list_capture_observations_for_domain(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Read acquisition history without requiring evaluations or reports."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        bounded_limit = max(1, min(int(limit), 500))
        if not domain:
            return []
        with self._connect() as conn:
            capture_rows = conn.execute(
                f"""
                SELECT captures.id, captures.observed_at, captures.recorded_at,
                       captures.source_url, captures.content_hash,
                       captures.acquisition_summary, captures.limitations,
                       captures.raw_payload, scan_runs.source_scan_id,
                       scan_runs.source_run_id, scan_runs.pipeline_version,
                       scan_runs.acquisition_state, scan_runs.request_payload,
                       scan_runs.metadata,
                       operation_plans.status AS operation_status,
                       operation_plans.operation_plan_fingerprint AS stored_plan_fingerprint,
                       operation_plans.result_fingerprint AS stored_result_fingerprint,
                       operation_plans.candidate_packet_fingerprint AS stored_candidate_packet_fingerprint
                FROM {_SCHEMA}.captures
                JOIN {_SCHEMA}.scan_runs ON scan_runs.id = captures.scan_run_id
                LEFT JOIN {_SCHEMA}.evidence_vault_operation_plans AS operation_plans
                  ON operation_plans.scan_run_id = scan_runs.id
                JOIN {_SCHEMA}.brands ON brands.id = captures.brand_id
                JOIN {_SCHEMA}.workspaces ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                ORDER BY captures.observed_at DESC,
                         captures.recorded_at DESC,
                         captures.id DESC
                LIMIT %s
                """,
                (workspace_slug, domain, bounded_limit),
            ).fetchall()
            observations: list[dict[str, Any]] = []
            for capture in capture_rows:
                evidence_rows = conn.execute(
                    f"""
                    SELECT evidence_ref, source, source_class, evidence_type,
                           url, content, content_raw, confidence, metadata
                    FROM {_SCHEMA}.evidence_records
                    WHERE capture_id = %s
                    ORDER BY evidence_ref, id
                    """,
                    (capture["id"],),
                ).fetchall()
                evidence: list[dict[str, Any]] = []
                for row in evidence_rows:
                    metadata = dict(row["metadata"] or {})
                    metadata.setdefault("source_class", str(row["source_class"]))
                    raw_content = row["content_raw"]
                    content = (
                        bytes(raw_content).decode("utf-8")
                        if raw_content is not None
                        else str(row["content"])
                    )
                    evidence.append(
                        {
                            "ref": str(row["evidence_ref"]),
                            "source": str(row["source"]),
                            "evidence_type": str(row["evidence_type"]),
                            "url": str(row["url"]),
                            "content": content,
                            "confidence": str(row["confidence"]),
                            "metadata": metadata,
                        }
                    )
                raw_observation = dict(capture["request_payload"] or {})
                metadata_payload = dict(capture["metadata"] or {})
                if capture.get("operation_status") is not None:
                    metadata_payload["analysis_status"] = str(
                        capture["operation_status"]
                    )
                    metadata_payload["operation_plan_fingerprint"] = str(
                        capture["stored_plan_fingerprint"]
                    )
                    metadata_payload["analysis_result_fingerprint"] = (
                        str(capture["stored_result_fingerprint"])
                        if capture.get("stored_result_fingerprint") is not None
                        else None
                    )
                    metadata_payload["candidate_packet_fingerprint"] = (
                        str(capture["stored_candidate_packet_fingerprint"])
                        if capture.get("stored_candidate_packet_fingerprint") is not None
                        else None
                    )
                stored_observation_hash = str(
                    metadata_payload.get("observation_hash") or ""
                )
                if metadata_payload.get("persisted_as") in {
                    "capture_only",
                    "capture_with_report",
                }:
                    if (
                        not raw_observation
                        or canonical_json_hash(raw_observation)
                        != stored_observation_hash
                    ):
                        raise CaptureConflictError(
                            f"capture {capture['source_scan_id']} failed observation integrity"
                        )
                observed_at = capture["observed_at"]
                recorded_at = capture["recorded_at"]
                observations.append(
                    {
                        "source_scan_id": str(capture["source_scan_id"]),
                        "source_run_id": str(capture["source_run_id"]),
                        "source_url": str(capture["source_url"]),
                        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
                        "recorded_at": recorded_at.astimezone(timezone.utc).isoformat(),
                        "pipeline_version": str(capture["pipeline_version"]),
                        "acquisition_state": str(capture["acquisition_state"]),
                        "capture_hash": str(capture["content_hash"]),
                        "acquisition_summary": dict(
                            capture["acquisition_summary"] or {}
                        ),
                        "limitations": list(capture["limitations"] or []),
                        "capture_payload": dict(capture["raw_payload"] or {}),
                        "metadata": metadata_payload,
                        "raw_observation": raw_observation,
                        "observation_hash": stored_observation_hash,
                        "evidence_records": evidence,
                    }
                )
        return observations

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
                    COALESCE(report_snapshots.payload -> 'not_detected', '[]'::jsonb) AS not_detected,
                    COALESCE(report_snapshots.payload ->> 'canonical_status', '') AS canonical_status,
                    COALESCE(report_snapshots.payload -> 'stability', '{{}}'::jsonb) AS stability
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

    def get_reviewed_claim_tile_memory_shadow(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Rebuild packet-bound reviewed mappings from durable state."""

        ledger = self.get_evidence_claim_tile_ledger(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        if not isinstance(ledger, dict):
            return None
        reviews = self.list_current_evidence_claim_tile_reviews(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        if not reviews:
            return None
        return build_reviewed_claim_tile_memory_from_journal_shadow(
            ledger,
            reviews,
        )

    def register_evidence_claim_tile_review_packet(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Append the exact current private reviewer packet, idempotently."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceClaimTileReviewPacketNotFoundError(
                "The brand domain does not exist in durable history."
            )
        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id,
                       workspaces.id AS workspace_id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                raise EvidenceClaimTileReviewPacketNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand["workspace_id"],
                        "brand",
                        brand_id,
                    ),
                ),
            )
            report_rows = conn.execute(
                f"""
                SELECT report_snapshots.payload
                FROM {_SCHEMA}.report_snapshots
                JOIN {_SCHEMA}.evaluation_runs
                  ON evaluation_runs.id =
                     report_snapshots.evaluation_run_id
                JOIN {_SCHEMA}.captures
                  ON captures.id = evaluation_runs.capture_id
                WHERE report_snapshots.workspace_id = %s
                  AND captures.brand_id = %s
                ORDER BY report_snapshots.created_at,
                         report_snapshots.source_report_id
                """,
                (brand["workspace_id"], brand_id),
            ).fetchall()
            reports = [
                dict(row["payload"])
                for row in report_rows
                if isinstance(row["payload"], dict)
            ]
            packet = build_evidence_claim_tile_review_packet(reports)
            manifest = packet["manifest"]
            ledger_state = conn.execute(
                f"""
                SELECT state_fingerprint
                FROM {_SCHEMA}.evidence_claim_tile_ledger_states
                WHERE brand_id = %s
                """,
                (brand_id,),
            ).fetchone()
            if (
                ledger_state is None
                or str(ledger_state["state_fingerprint"])
                != str(manifest["ledger_state_fingerprint"])
            ):
                raise EvidenceClaimTileReviewUnavailableError(
                    "The persisted claim-to-tile ledger does not match "
                    "the immutable snapshots."
                )
            candidate_subject_ids = [
                str(candidate["subject_id"])
                for candidate in packet["candidates"]
            ]
            mapping_rows = (
                conn.execute(
                    f"""
                    SELECT *
                    FROM {_SCHEMA}.evidence_claim_tile_mappings
                    WHERE brand_id = %s
                      AND mapping_id = ANY(%s::text[])
                    """,
                    (brand_id, candidate_subject_ids),
                ).fetchall()
                if candidate_subject_ids
                else []
            )
            mappings_by_id = {
                str(mapping["mapping_id"]): mapping
                for mapping in mapping_rows
            }
            for candidate in packet["candidates"]:
                mapping = mappings_by_id.get(
                    str(candidate["subject_id"])
                )
                if mapping is None or not _packet_mapping_matches(
                    candidate,
                    mapping,
                ):
                    raise EvidenceClaimTileReviewUnavailableError(
                        "A packet candidate does not match the durable "
                        "claim-to-tile mapping."
                    )
            packet_fingerprint = str(
                manifest["review_packet_fingerprint"]
            )
            packet_id = _stable_uuid(
                brand_id,
                "evidence-claim-tile-review-packet",
                packet_fingerprint,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_claim_tile_review_packets (
                    id, brand_id, packet_kind, packet_fingerprint,
                    candidate_fingerprint, schema_version,
                    packet_schema_version, candidate_schema_version,
                    ledger_state_fingerprint, mapping_series_id,
                    rubric_version, candidate_count, manifest, candidates,
                    runtime_effect, authority, automatic_tile_effect,
                    automatic_scoring_effect
                ) VALUES (
                    %s, %s, 'claim_tile', %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, false, false, false, false
                )
                ON CONFLICT (brand_id, packet_fingerprint) DO NOTHING
                RETURNING *
                """,
                (
                    packet_id,
                    brand_id,
                    packet_fingerprint,
                    str(manifest["candidate_fingerprint"]),
                    str(manifest["schema_version"]),
                    str(manifest["review_packet_schema_version"]),
                    str(manifest["candidate_schema_version"]),
                    str(manifest["ledger_state_fingerprint"]),
                    str(manifest["mapping_series_id"]),
                    str(manifest["rubric_version"]),
                    int(manifest["candidate_count"]),
                    _jsonb(manifest),
                    _jsonb(packet["candidates"]),
                ),
            ).fetchone()
            replayed = inserted is None
            row = inserted
            if row is None:
                row = conn.execute(
                    f"""
                    SELECT *
                    FROM {_SCHEMA}.evidence_claim_tile_review_packets
                    WHERE brand_id = %s
                      AND packet_fingerprint = %s
                    """,
                    (brand_id, packet_fingerprint),
                ).fetchone()
            if row is None:
                raise EvidenceClaimTileReviewUnavailableError(
                    "The review packet could not be registered."
                )
            stored = _claim_tile_review_packet_record(row)
        return stored, replayed

    def get_evidence_claim_tile_review_packet(
        self,
        domain_or_url: str,
        packet_fingerprint: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        """Read and revalidate one exact private reviewer packet."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        normalized_fingerprint = str(
            packet_fingerprint or ""
        ).strip().lower()
        if not domain or not _is_sha256(normalized_fingerprint):
            raise EvidenceClaimTileReviewPacketNotFoundError(
                "The registered claim-to-tile review packet does not exist."
            )
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT packets.*
                FROM {_SCHEMA}.evidence_claim_tile_review_packets
                     AS packets
                JOIN {_SCHEMA}.brands
                  ON brands.id = packets.brand_id
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                  AND packets.packet_fingerprint = %s
                """,
                (
                    workspace_slug,
                    domain,
                    normalized_fingerprint,
                ),
            ).fetchone()
        if row is None:
            raise EvidenceClaimTileReviewPacketNotFoundError(
                "The registered claim-to-tile review packet does not exist."
            )
        return _claim_tile_review_packet_record(row)

    def get_evidence_claim_tile_review_queue(
        self,
        domain_or_url: str,
        packet_fingerprint: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        """Derive review readiness from exact packets and current events."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        normalized_fingerprint = str(
            packet_fingerprint or ""
        ).strip().lower()
        if not domain or not _is_sha256(normalized_fingerprint):
            raise EvidenceClaimTileReviewPacketNotFoundError(
                "The registered claim-to-tile review packet does not exist."
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
                raise EvidenceClaimTileReviewPacketNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            packet_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_claim_tile_review_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                """,
                (brand_id, normalized_fingerprint),
            ).fetchone()
            if packet_row is None:
                raise EvidenceClaimTileReviewPacketNotFoundError(
                    "The registered claim-to-tile review packet does not "
                    "exist."
                )
            current_packet = _claim_tile_review_packet_record(packet_row)
            subject_ids = [
                str(candidate["subject_id"])
                for candidate in current_packet["candidates"]
            ]
            review_rows = (
                conn.execute(
                    f"""
                    SELECT DISTINCT ON (
                               events.subject_type,
                               events.subject_id
                           )
                           events.*
                    FROM {_SCHEMA}.evidence_claim_tile_review_events
                         AS events
                    JOIN {_SCHEMA}.evidence_claim_tile_review_packets
                         AS packets
                      ON packets.brand_id = events.brand_id
                     AND packets.packet_fingerprint =
                         events.review_packet_fingerprint
                    WHERE events.brand_id = %s
                      AND events.subject_type = %s
                      AND events.subject_id = ANY(%s::text[])
                    ORDER BY events.subject_type,
                             events.subject_id,
                             events.sequence DESC
                    """,
                    (
                        brand_id,
                        CLAIM_TILE_REVIEW_SUBJECT_TYPE,
                        subject_ids,
                    ),
                ).fetchall()
                if subject_ids
                else []
            )
            reviews = [
                _claim_tile_review_event(row) for row in review_rows
            ]
            source_fingerprints = sorted(
                {
                    normalized_fingerprint,
                    *(
                        str(review["review_packet_fingerprint"])
                        for review in reviews
                    ),
                }
            )
            source_packet_rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_claim_tile_review_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = ANY(%s::text[])
                ORDER BY packet_fingerprint
                """,
                (brand_id, source_fingerprints),
            ).fetchall()
            registered_packets = [
                _claim_tile_review_packet_record(row)
                for row in source_packet_rows
            ]
        return build_evidence_claim_tile_review_queue(
            current_packet,
            reviews,
            registered_packets=registered_packets,
        )

    def build_and_register_evidence_vault_canonical_memory_packet(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Resolve and register one packet exclusively from durable history."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceVaultCanonicalPacketNotFoundError(
                "The brand domain does not exist in durable history."
            )
        reports: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = self.list_report_payloads_for_domain(
                domain,
                workspace_slug=workspace_slug,
                limit=500,
                offset=offset,
            )
            reports.extend(batch)
            if len(batch) < 500:
                break
            offset += len(batch)
        if not reports:
            raise EvidenceVaultCanonicalPacketNotFoundError(
                "The brand has no immutable report history."
            )
        ledger = self.get_evidence_claim_tile_ledger(
            domain,
            workspace_slug=workspace_slug,
        )
        if not isinstance(ledger, dict):
            raise EvidenceVaultCanonicalUnavailableError(
                "The durable claim-to-tile ledger is unavailable."
            )
        adjudications = self.list_current_evidence_memory_adjudications(
            domain,
            workspace_slug=workspace_slug,
        )
        reconciliations = self.list_current_evidence_claim_reconciliations(
            domain,
            workspace_slug=workspace_slug,
        )
        reviews = self.list_current_evidence_claim_tile_reviews(
            domain,
            workspace_slug=workspace_slug,
        )
        recovery_reviews = (
            self.list_current_evidence_scoring_recovery_reviews(
                domain,
                workspace_slug=workspace_slug,
            )
        )
        supplemental_packets = [
            row["packet"]
            for row in self.list_evidence_scoring_recovery_supplement_packets(
                domain,
                workspace_slug=workspace_slug,
            )
        ]
        registered_packet_fingerprints = {
            str(review.get("review_packet_fingerprint") or "")
            for review in reviews
        }
        if "" in registered_packet_fingerprints:
            raise EvidenceVaultCanonicalUnavailableError(
                "A current claim-to-tile review has no packet fingerprint."
            )
        for fingerprint in sorted(registered_packet_fingerprints):
            self.get_evidence_claim_tile_review_packet(
                domain,
                fingerprint,
                workspace_slug=workspace_slug,
            )
        current_memory = self.get_evidence_vault_canonical_memory(
            domain,
            workspace_slug=workspace_slug,
        )
        try:
            resolved = build_resolved_canonical_memory_candidate(
                brand_identity=domain,
                reports=reports,
                evidence_adjudications=adjudications,
                claim_reconciliations=reconciliations,
                claim_tile_ledger=ledger,
                claim_tile_reviews=reviews,
                scoring_recovery_reviews=recovery_reviews,
                supplemental_recovery_candidate_packets=(
                    supplemental_packets
                ),
                registered_review_packet_fingerprints=(
                    registered_packet_fingerprints
                ),
                current_canonical_memory=current_memory,
            )
        except EvidenceVaultCandidateResolverError as exc:
            raise EvidenceVaultCanonicalUnavailableError(
                "Durable history cannot resolve one canonical-memory candidate."
            ) from exc
        return self.register_evidence_vault_canonical_memory_packet(
            domain,
            resolved["packet"],
            resolved_references=resolved["resolved_references"],
            workspace_slug=workspace_slug,
        )

    def register_evidence_vault_canonical_memory_packet(
        self,
        domain_or_url: str,
        packet: dict[str, Any],
        *,
        resolved_references: dict[str, str],
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Register one immutable packet after trusted reference resolution.

        The caller must be the Vault candidate generator/resolver. This
        storage boundary binds its exact resolution but does not infer
        upstream ledger identities from caller-provided strings.
        """

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceVaultCanonicalPacketNotFoundError(
                "The brand domain does not exist in durable history."
            )
        try:
            validate_candidate_packet(packet)
            resolution = build_reference_resolution(packet, resolved_references)
        except EvidenceVaultCanonicalCoreError as exc:
            raise EvidenceVaultCanonicalPacketNotFoundError(
                "The canonical-memory candidate packet is invalid."
            ) from exc
        manifest = packet["manifest"]
        if manifest["brand_identity"] != domain:
            raise EvidenceVaultCanonicalPromotionConflictError(
                "The candidate packet belongs to a different brand."
            )

        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id,
                       workspaces.id AS workspace_id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                raise EvidenceVaultCanonicalPacketNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-vault-canonical-packet",
                        packet["candidate_packet_fingerprint"],
                    ),
                ),
            )
            packet_id = _stable_uuid(
                brand_id,
                "evidence-vault-canonical-memory-packet",
                packet["candidate_packet_fingerprint"],
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint,
                    reference_resolution, manifest, candidate_tiles,
                    authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'pending_review', false, false, false
                )
                ON CONFLICT (
                    brand_id, packet_fingerprint,
                    reference_resolution_fingerprint
                ) DO NOTHING
                RETURNING *
                """,
                (
                    packet_id,
                    brand_id,
                    packet["candidate_packet_fingerprint"],
                    manifest["schema_version"],
                    manifest["brand_identity"],
                    manifest["parent_canonical_memory_version"],
                    resolution["reference_resolution_fingerprint"],
                    _jsonb(resolution),
                    _jsonb(manifest),
                    _jsonb(packet["candidate_tiles"]),
                ),
            ).fetchone()
            replayed = inserted is None
            row = inserted
            if row is None:
                row = conn.execute(
                    f"""
                    SELECT *
                    FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                    WHERE brand_id = %s
                      AND packet_fingerprint = %s
                      AND reference_resolution_fingerprint = %s
                      AND packet_kind = 'canonical_v1'
                    """,
                    (
                        brand_id,
                        packet["candidate_packet_fingerprint"],
                        resolution["reference_resolution_fingerprint"],
                    ),
                ).fetchone()
            if row is None:
                raise EvidenceVaultCanonicalPacketNotFoundError(
                    "The canonical-memory candidate packet could not be registered."
                )
            stored = _vault_canonical_packet_record(row)
            if (
                stored["packet"] != packet
                or stored["reference_resolution"] != resolution
            ):
                raise EvidenceVaultCanonicalPromotionConflictError(
                    "The registered packet fingerprint resolves to different content."
                )
        return stored, replayed

    def get_evidence_vault_canonical_memory_packet(
        self,
        domain_or_url: str,
        packet_fingerprint: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        """Read and revalidate one exact canonical-memory candidate packet."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        normalized_fingerprint = str(packet_fingerprint or "").strip().lower()
        if not domain or not _is_sha256(normalized_fingerprint):
            raise EvidenceVaultCanonicalPacketNotFoundError(
                "The registered canonical-memory packet does not exist."
            )
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT packets.*
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets AS packets
                JOIN {_SCHEMA}.brands
                  ON brands.id = packets.brand_id
                JOIN {_SCHEMA}.workspaces
                  ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                  AND packets.packet_fingerprint = %s
                  AND packets.packet_kind = 'canonical_v1'
                """,
                (workspace_slug, domain, normalized_fingerprint),
            ).fetchone()
        if row is None:
            raise EvidenceVaultCanonicalPacketNotFoundError(
                "The registered canonical-memory packet does not exist."
            )
        return _vault_canonical_packet_record(row)

    def append_evidence_vault_canonical_memory_promotion(
        self,
        domain_or_url: str,
        command: CanonicalMemoryPromotionCommand,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Append one human promotion with serialized parent comparison."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceVaultCanonicalPacketNotFoundError(
                "The brand domain does not exist in durable history."
            )
        packet_fingerprint = str(
            command.candidate_packet_fingerprint or ""
        ).strip().lower()
        idempotency_hash = str(
            command.idempotency_key_hash or ""
        ).strip().lower()
        request_fingerprint = str(
            command.request_fingerprint or ""
        ).strip().lower()
        if not all(
            _is_sha256(value)
            for value in (
                packet_fingerprint,
                idempotency_hash,
                request_fingerprint,
            )
        ):
            raise EvidenceVaultCanonicalPromotionConflictError(
                "The promotion command fingerprints are invalid."
            )
        try:
            expected_request_fingerprint = promotion_request_fingerprint(
                candidate_packet_fingerprint=packet_fingerprint,
                parent_canonical_memory_version=(
                    command.parent_canonical_memory_version
                ),
                reviewer_id=command.reviewer_id,
                reviewed_at=command.reviewed_at,
                rationale=command.rationale,
            )
        except EvidenceVaultCanonicalAuthorityError as exc:
            raise EvidenceVaultCanonicalPromotionConflictError(
                "The promotion command is invalid."
            ) from exc
        if request_fingerprint != expected_request_fingerprint:
            raise EvidenceVaultCanonicalPromotionConflictError(
                "The promotion request fingerprint does not match the command."
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
                raise EvidenceVaultCanonicalPacketNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-vault-canonical-promotion",
                    ),
                ),
            )

            existing = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_promotion_events
                WHERE brand_id = %s
                  AND idempotency_key_hash = %s
                  AND adoption_kind = 'human_promotion_v1'
                """,
                (brand_id, idempotency_hash),
            ).fetchone()
            if existing is not None:
                event = _vault_canonical_promotion_event(existing)
                if event["request_fingerprint"] != request_fingerprint:
                    raise EvidenceVaultCanonicalPromotionConflictError(
                        "The Idempotency-Key was used for a different promotion.",
                        existing_event_id=event["event_id"],
                    )
                return event, True

            existing_packet_event = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_promotion_events
                WHERE brand_id = %s
                  AND candidate_packet_fingerprint = %s
                  AND adoption_kind = 'human_promotion_v1'
                """,
                (brand_id, packet_fingerprint),
            ).fetchone()
            if existing_packet_event is not None:
                event = _vault_canonical_promotion_event(
                    existing_packet_event
                )
                if event["request_fingerprint"] == request_fingerprint:
                    return event, True
                raise EvidenceVaultCanonicalPromotionConflictError(
                    "The candidate packet was already promoted by a different request.",
                    existing_event_id=event["event_id"],
                )

            packet_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND packet_kind = 'canonical_v1'
                """,
                (brand_id, packet_fingerprint),
            ).fetchone()
            if packet_row is None:
                raise EvidenceVaultCanonicalPacketNotFoundError(
                    "The exact registered canonical-memory packet does not exist."
                )
            stored_packet = _vault_canonical_packet_record(packet_row)
            current_memory = _project_vault_canonical_memory(
                conn,
                brand_id,
            )
            current_version = (
                current_memory["canonical_memory_version"]
                if current_memory is not None
                else None
            )
            if command.parent_canonical_memory_version != current_version:
                raise EvidenceVaultCanonicalPromotionConflictError(
                    "The canonical memory changed after the candidate was built.",
                    current_canonical_memory_version=current_version,
                )
            plan = plan_canonical_memory_promotion(
                stored_packet["packet"],
                resolved_references=(
                    stored_packet["reference_resolution"]["references"]
                ),
                current_memory=current_memory,
            )
            previous_event_id = (
                current_memory["promotion_event_id"]
                if current_memory is not None
                else None
            )
            sequence = (
                int(current_memory["promotion_sequence"]) + 1
                if current_memory is not None
                else 1
            )
            event_id = _stable_uuid(
                brand_id,
                "evidence-vault-canonical-memory-promotion",
                idempotency_hash,
            )
            event = build_promotion_event(
                plan,
                command,
                event_id=str(event_id),
                sequence=sequence,
                previous_event_id=previous_event_id,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_promotion_events (
                    id, brand_id, event_type, sequence, previous_event_id,
                    brand_identity, candidate_packet_fingerprint,
                    reference_resolution_fingerprint,
                    promotion_policy_fingerprint,
                    parent_canonical_memory_version,
                    promoted_canonical_memory_version, decision,
                    reviewer_id, reviewed_at, rationale, schema_version,
                    idempotency_key_hash, request_fingerprint, authority,
                    authority_scope, production_runtime_effect,
                    scanner_runtime_effect
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, true, %s,
                    false, false
                )
                RETURNING *
                """,
                (
                    event["event_id"],
                    brand_id,
                    event["event_type"],
                    event["sequence"],
                    event["previous_event_id"],
                    event["brand_identity"],
                    event["candidate_packet_fingerprint"],
                    event["reference_resolution_fingerprint"],
                    event["promotion_policy_fingerprint"],
                    event["parent_canonical_memory_version"],
                    event["promoted_canonical_memory_version"],
                    event["decision"],
                    event["reviewer_id"],
                    event["reviewed_at"],
                    event["rationale"],
                    event["schema_version"],
                    event["idempotency_key_hash"],
                    event["request_fingerprint"],
                    event["authority_scope"],
                ),
            ).fetchone()
        return _vault_canonical_promotion_event(inserted), False

    def get_evidence_vault_canonical_memory(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Rebuild the current canonical memory from packets and promotions."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
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
                return None
            return _project_vault_canonical_memory(conn, brand["id"])

    def get_or_create_evidence_vault_canonical_score_evaluation(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any] | None, bool]:
        """Persist the deterministic score for the current promoted memory.

        Returns ``(None, False)`` while no canonical baseline exists.  The
        promotion advisory lock makes selection of the current memory and
        insertion of its immutable evaluation one serialized operation.
        """

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None, False
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
                return None, False
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-vault-canonical-promotion",
                    ),
                ),
            )
            current_memory = _project_vault_canonical_memory(conn, brand_id)
            if current_memory is None:
                return None, False

            existing = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_score_evaluations
                WHERE brand_id = %s
                  AND canonical_memory_version = %s
                """,
                (brand_id, current_memory["canonical_memory_version"]),
            ).fetchone()
            if existing is not None:
                return _vault_canonical_score_evaluation_record(existing), True

            try:
                evaluation = build_canonical_score_evaluation(
                    current_memory,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            except EvidenceVaultCanonicalScoringError as exc:
                raise EvidenceVaultCanonicalUnavailableError(
                    "The promoted canonical memory cannot be scored safely."
                ) from exc

            reusable_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_score_evaluations
                WHERE brand_id = %s
                  AND score_input_fingerprint = %s
                ORDER BY created_at, id
                LIMIT 1
                """,
                (brand_id, evaluation["score_input_fingerprint"]),
            ).fetchone()
            if reusable_row is not None:
                reusable = _vault_canonical_score_evaluation_record(
                    reusable_row
                )
                try:
                    evaluation = build_canonical_score_evaluation(
                        current_memory,
                        created_at=evaluation["created_at"],
                        reusable_evaluation=reusable,
                    )
                except EvidenceVaultCanonicalScoringError as exc:
                    raise EvidenceVaultCanonicalUnavailableError(
                        "The canonical score calculation cache is invalid."
                    ) from exc

            evaluation_id = _stable_uuid(
                brand_id,
                "evidence-vault-canonical-score-evaluation",
                evaluation["evaluation_identity"],
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_score_evaluations (
                    id, brand_id, promotion_event_id,
                    canonical_memory_version, evaluation_identity,
                    score_input_fingerprint,
                    derived_tile_state_fingerprint, rubric_version,
                    tile_contract_registry_fingerprint,
                    reducer_policy_fingerprint,
                    aggregation_policy_fingerprint, schema_version,
                    score, component_breakdown, base_average,
                    magnetism_capped,
                    reused_from_evaluation_identity, authority,
                    authority_scope, production_runtime_effect,
                    scanner_runtime_effect, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, true, %s, false,
                    false, %s
                )
                ON CONFLICT (brand_id, canonical_memory_version) DO NOTHING
                RETURNING *
                """,
                (
                    evaluation_id,
                    brand_id,
                    evaluation["promotion_event_id"],
                    evaluation["canonical_memory_version"],
                    evaluation["evaluation_identity"],
                    evaluation["score_input_fingerprint"],
                    evaluation["derived_tile_state_fingerprint"],
                    evaluation["rubric_version"],
                    evaluation["tile_contract_registry_fingerprint"],
                    evaluation["reducer_policy_fingerprint"],
                    evaluation["aggregation_policy_fingerprint"],
                    evaluation["schema_version"],
                    evaluation["score"],
                    _jsonb(evaluation["component_breakdown"]),
                    evaluation["base_average"],
                    evaluation["magnetism_capped"],
                    evaluation["reused_from_evaluation_identity"],
                    evaluation["authority_scope"],
                    evaluation["created_at"],
                ),
            ).fetchone()
            replayed = inserted is None
            row = inserted
            if row is None:
                row = conn.execute(
                    f"""
                    SELECT *
                    FROM {_SCHEMA}.evidence_vault_canonical_score_evaluations
                    WHERE brand_id = %s
                      AND canonical_memory_version = %s
                    """,
                    (brand_id, evaluation["canonical_memory_version"]),
                ).fetchone()
            if row is None:
                raise EvidenceVaultCanonicalUnavailableError(
                    "The canonical score evaluation could not be persisted."
                )
            stored = _vault_canonical_score_evaluation_record(row)
            if stored != evaluation:
                mismatched_fields = sorted(
                    field
                    for field in evaluation
                    if stored.get(field) != evaluation.get(field)
                )
                raise EvidenceVaultCanonicalUnavailableError(
                    "The canonical evaluation identity resolves to different "
                    f"content fields: {', '.join(mismatched_fields)}."
                )
            return stored, replayed

    def get_evidence_vault_canonical_score_evaluation(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Return the persisted evaluation for the current canonical memory."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
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
                return None
            memory = _project_vault_canonical_memory(conn, brand["id"])
            if memory is None:
                return None
            row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_score_evaluations
                WHERE brand_id = %s
                  AND canonical_memory_version = %s
                """,
                (brand["id"], memory["canonical_memory_version"]),
            ).fetchone()
        return (
            _vault_canonical_score_evaluation_record(row)
            if row is not None
            else None
        )

    def activate_provisional_evidence_vault_operational_baseline(
        self,
        report: dict[str, Any],
        *,
        workspace_slug: str = "b3s",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Create N1 from a diagnostic report without adopting its LLM states."""

        domain = normalize_domain(str(report.get("url") or ""))
        current = self.get_evidence_vault_operational_memory(
            domain,
            workspace_slug=workspace_slug,
        )
        if current is not None:
            return {
                "created": False,
                "reason": "canonical_memory_already_exists",
                "memory": current,
                "score": self.get_or_create_evidence_vault_operational_score_evaluation(
                    domain,
                    workspace_slug=workspace_slug,
                )[0],
            }
        packet = build_provisional_operational_packet_from_report(report)
        stored, packet_replayed = self.register_evidence_vault_operational_memory_packet(
            domain,
            packet,
            workspace_slug=workspace_slug,
        )
        policy_fingerprint = canonical_fingerprint(
            "evidence-vault-provisional-baseline-policy-v1",
            {
                "accepted_subset": "empty",
                "scanner_states": "candidate_overlay_only",
                "authority_scope": "b3s-vault",
            },
        )
        actor_id = "automatic-provisional-baseline-v1"
        request_fingerprint = adoption_request_fingerprint(
            candidate_packet_fingerprint=packet[
                "candidate_packet_fingerprint"
            ],
            parent_canonical_memory_version=None,
            adopted_by="policy",
            actor_id=actor_id,
            policy_fingerprint=policy_fingerprint,
        )
        command = OperationalAdoptionCommand(
            candidate_packet_fingerprint=packet[
                "candidate_packet_fingerprint"
            ],
            parent_canonical_memory_version=None,
            adopted_by="policy",
            actor_id=actor_id,
            policy_fingerprint=policy_fingerprint,
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
            idempotency_key_hash=canonical_fingerprint(
                "evidence-vault-provisional-baseline-idempotency-v1",
                packet["candidate_packet_fingerprint"],
            ),
            request_fingerprint=request_fingerprint,
        )
        _, adoption_replayed = self.append_evidence_vault_operational_adoption(
            domain,
            command,
            workspace_slug=workspace_slug,
        )
        memory = self.get_evidence_vault_operational_memory(
            domain,
            workspace_slug=workspace_slug,
        )
        score, score_replayed = (
            self.get_or_create_evidence_vault_operational_score_evaluation(
                domain,
                workspace_slug=workspace_slug,
            )
        )
        return {
            "created": True,
            "packet_replayed": packet_replayed,
            "adoption_replayed": adoption_replayed,
            "score_replayed": score_replayed,
            "memory": memory,
            "score": score,
            "diagnostic_score": report.get("score"),
        }

    def build_and_register_evidence_vault_operational_baseline_packet(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Bridge current reviewed history into one partial v2 baseline packet."""

        if self.get_evidence_vault_operational_memory(
            domain_or_url,
            workspace_slug=workspace_slug,
        ) is not None:
            raise EvidenceVaultOperationalAuthorityError(
                "Operational canonical memory already exists; use incremental refresh."
            )
        # A legacy v1 memory remains immutable and authoritative in its own
        # ledger.  The v2 baseline is an explicit, separately adopted projection
        # of only the currently policy-eligible reviewed subset.
        source, _ = self.build_and_register_evidence_vault_canonical_memory_packet(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        operational = build_operational_packet_from_reviewed_candidate(
            source["packet"]
        )
        return self.register_evidence_vault_operational_memory_packet(
            domain_or_url,
            operational,
            workspace_slug=workspace_slug,
        )

    def build_and_register_evidence_vault_operational_incremental_packet(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Bridge latest reviewed evidence onto the exact active v2 parent."""

        current = self.get_evidence_vault_operational_memory(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        if current is None:
            raise EvidenceVaultOperationalAuthorityError(
                "Incremental operational memory requires an active v2 parent."
            )
        source, _ = self.build_and_register_evidence_vault_canonical_memory_packet(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        operational = build_operational_packet_from_reviewed_candidate(
            source["packet"],
            current_operational_memory=current,
        )
        return self.register_evidence_vault_operational_memory_packet(
            domain_or_url,
            operational,
            workspace_slug=workspace_slug,
        )

    def activate_evidence_vault_operational_incremental_refresh(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Adopt newly policy-eligible reviewed relations onto active v2 memory."""

        current = self.get_evidence_vault_operational_memory(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        if current is None:
            raise EvidenceVaultOperationalAuthorityError(
                "Incremental operational refresh requires active memory."
            )
        stored, packet_replayed = (
            self.build_and_register_evidence_vault_operational_incremental_packet(
                domain_or_url,
                workspace_slug=workspace_slug,
            )
        )
        packet = stored["packet"]
        if packet["has_accepted_change"] is not True:
            score, score_replayed = (
                self.get_or_create_evidence_vault_operational_score_evaluation(
                    domain_or_url,
                    workspace_slug=workspace_slug,
                )
            )
            return {
                "memory": current,
                "score": score,
                "packet_replayed": packet_replayed,
                "adoption_replayed": True,
                "score_replayed": score_replayed,
                "reason": "no_accepted_change",
            }
        policy_fingerprint = str(
            build_initial_authority_profile_matrix()[
                "authority_matrix_fingerprint"
            ]
        )
        actor_id = "automatic-reviewed-basis-incremental-v1"
        request_fingerprint = adoption_request_fingerprint(
            candidate_packet_fingerprint=packet[
                "candidate_packet_fingerprint"
            ],
            parent_canonical_memory_version=current[
                "canonical_memory_version"
            ],
            adopted_by="policy",
            actor_id=actor_id,
            policy_fingerprint=policy_fingerprint,
        )
        command = OperationalAdoptionCommand(
            candidate_packet_fingerprint=packet[
                "candidate_packet_fingerprint"
            ],
            parent_canonical_memory_version=current[
                "canonical_memory_version"
            ],
            adopted_by="policy",
            actor_id=actor_id,
            policy_fingerprint=policy_fingerprint,
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
            idempotency_key_hash=canonical_fingerprint(
                "evidence-vault-operational-incremental-idempotency-v1",
                {
                    "candidate_packet_fingerprint": packet[
                        "candidate_packet_fingerprint"
                    ],
                    "parent_canonical_memory_version": current[
                        "canonical_memory_version"
                    ],
                    "policy_fingerprint": policy_fingerprint,
                },
            ),
            request_fingerprint=request_fingerprint,
        )
        _, adoption_replayed = self.append_evidence_vault_operational_adoption(
            domain_or_url,
            command,
            workspace_slug=workspace_slug,
        )
        memory = self.get_evidence_vault_operational_memory(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        score, score_replayed = (
            self.get_or_create_evidence_vault_operational_score_evaluation(
                domain_or_url,
                workspace_slug=workspace_slug,
            )
        )
        return {
            "memory": memory,
            "score": score,
            "packet_replayed": packet_replayed,
            "adoption_replayed": adoption_replayed,
            "score_replayed": score_replayed,
        }

    def activate_evidence_vault_operational_baseline(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Activate the deterministic reviewed subset and persist its score."""

        current = self.get_evidence_vault_operational_memory(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        if current is not None:
            return self.activate_evidence_vault_operational_incremental_refresh(
                domain_or_url,
                workspace_slug=workspace_slug,
                created_at=created_at,
            )
        stored, packet_replayed = (
            self.build_and_register_evidence_vault_operational_baseline_packet(
                domain_or_url,
                workspace_slug=workspace_slug,
            )
        )
        packet = stored["packet"]
        policy_fingerprint = str(
            build_initial_authority_profile_matrix()[
                "authority_matrix_fingerprint"
            ]
        )
        actor_id = "automatic-reviewed-basis-baseline-v1"
        request_fingerprint = adoption_request_fingerprint(
            candidate_packet_fingerprint=packet[
                "candidate_packet_fingerprint"
            ],
            parent_canonical_memory_version=None,
            adopted_by="policy",
            actor_id=actor_id,
            policy_fingerprint=policy_fingerprint,
        )
        idempotency_key_hash = canonical_fingerprint(
            "evidence-vault-operational-baseline-idempotency-v1",
            {
                "candidate_packet_fingerprint": packet[
                    "candidate_packet_fingerprint"
                ],
                "policy_fingerprint": policy_fingerprint,
            },
        )
        command = OperationalAdoptionCommand(
            candidate_packet_fingerprint=packet[
                "candidate_packet_fingerprint"
            ],
            parent_canonical_memory_version=None,
            adopted_by="policy",
            actor_id=actor_id,
            policy_fingerprint=policy_fingerprint,
            created_at=(
                created_at or datetime.now(timezone.utc).isoformat()
            ),
            idempotency_key_hash=idempotency_key_hash,
            request_fingerprint=request_fingerprint,
        )
        _, adoption_replayed = self.append_evidence_vault_operational_adoption(
            domain_or_url,
            command,
            workspace_slug=workspace_slug,
        )
        memory = self.get_evidence_vault_operational_memory(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        score, score_replayed = (
            self.get_or_create_evidence_vault_operational_score_evaluation(
                domain_or_url,
                workspace_slug=workspace_slug,
            )
        )
        return {
            "memory": memory,
            "score": score,
            "packet_replayed": packet_replayed,
            "adoption_replayed": adoption_replayed,
            "score_replayed": score_replayed,
        }

    def register_evidence_vault_exact_relation_supplement_source_packet(
        self,
        domain_or_url: str,
        artifact: Mapping[str, Any],
        *,
        assessment_artifact: Mapping[str, Any],
        assessment_review_rows: list[Mapping[str, Any]],
        assessment_worksheet_sha256: str,
        evidence_pack: Mapping[str, Any],
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Persist exact assessment-derived relations as pending-only source."""

        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceVaultOperationalAuthorityError(
                "Exact relation supplement brand is required."
            )
        try:
            validate_exact_relation_supplement_artifact(
                artifact,
                assessment_artifact=assessment_artifact,
                assessment_review_rows=assessment_review_rows,
                assessment_worksheet_sha256=assessment_worksheet_sha256,
                evidence_pack=evidence_pack,
            )
        except EvidenceVaultExactRelationSupplementError as exc:
            raise EvidenceVaultOperationalAuthorityError(
                "The exact relation supplement artifact is invalid."
            ) from exc
        if str(artifact["brand_identity"]) != domain:
            raise EvidenceVaultOperationalAuthorityError(
                "The exact relation supplement belongs to another brand."
            )
        self._ensure_migrated()
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
                raise EvidenceVaultOperationalAuthorityError(
                    "The exact relation supplement brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-vault-canonical-promotion",
                    ),
                ),
            )
            existing_rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_kind = 'operational_source_v2'
                  AND reference_resolution ->> 'source_kind' = 'exact_relation_supplement'
                  AND reference_resolution ->> 'artifact_fingerprint' = %s
                FOR UPDATE
                """,
                (brand_id, artifact["artifact_fingerprint"]),
            ).fetchall()
            if len(existing_rows) > 1:
                raise EvidenceVaultOperationalAuthorityError(
                    "Exact relation supplement resolves to multiple sources."
                )
            if existing_rows:
                stored = _vault_operational_source_packet_record(existing_rows[0])
                expected_resolution = build_exact_relation_source_resolution(
                    artifact,
                    source_candidate_packet=stored["packet"],
                )
                if stored["reference_resolution"] != expected_resolution:
                    raise EvidenceVaultOperationalAuthorityError(
                        "Exact relation replay differs from its immutable source."
                    )
                return stored, True
            current = _project_vault_operational_memory(conn, brand_id)
            current_version = (
                current["canonical_memory_version"]
                if current is not None
                else None
            )
            if current_version != artifact["parent_canonical_memory_version"]:
                raise EvidenceVaultOperationalAdoptionConflictError(
                    "Exact relation supplement has a stale canonical parent."
                )
            try:
                source = build_exact_relation_source_candidate(
                    artifact,
                    current_operational_memory=current,
                )
                resolution = build_exact_relation_source_resolution(
                    artifact,
                    source_candidate_packet=source,
                )
            except EvidenceVaultExactRelationSupplementError as exc:
                raise EvidenceVaultOperationalAuthorityError(
                    "The exact relation source projection is invalid."
                ) from exc
            resolution_fingerprint = canonical_fingerprint(
                "evidence-vault-operational-source-resolution-v1",
                resolution,
            )
            packet_id = _stable_uuid(
                brand_id,
                "evidence-vault-exact-relation-supplement-source-packet",
                source["candidate_packet_fingerprint"],
                resolution_fingerprint,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'pending_review', false, false, false,
                    'operational_source_v2', %s
                )
                ON CONFLICT (
                    brand_id, packet_fingerprint,
                    reference_resolution_fingerprint
                ) DO NOTHING
                RETURNING *
                """,
                (
                    packet_id,
                    brand_id,
                    source["candidate_packet_fingerprint"],
                    source["manifest"]["schema_version"],
                    source["manifest"]["brand_identity"],
                    source["manifest"]["parent_canonical_memory_version"],
                    resolution_fingerprint,
                    _jsonb(resolution),
                    _jsonb(source["manifest"]),
                    _jsonb(source["candidate_tiles"]),
                    _jsonb(source),
                ),
            ).fetchone()
            replayed = inserted is None
            row = inserted or conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND reference_resolution_fingerprint = %s
                  AND packet_kind = 'operational_source_v2'
                """,
                (
                    brand_id,
                    source["candidate_packet_fingerprint"],
                    resolution_fingerprint,
                ),
            ).fetchone()
            if row is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The exact relation source could not be persisted."
                )
            stored = _vault_operational_source_packet_record(row)
            expected = {
                "packet": source,
                "reference_resolution": resolution,
                "authority": False,
                "authority_scope": "b3s-vault",
                "production_runtime_effect": False,
                "scanner_runtime_effect": False,
            }
            if stored != expected:
                raise EvidenceVaultOperationalAuthorityError(
                    "Exact relation source identity resolves to different content."
                )
            return stored, replayed

    def bind_evidence_vault_exact_source_capture_lineage(
        self,
        domain_or_url: str,
        lineage_seed_export: Mapping[str, Any],
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Bind an exact C7 source to the current persisted capture.

        Integration contract: ``lineage_seed_export`` is the strict
        ``evidence-vault-lineage-seed-export-v2`` object.  Its nested exact
        supplement and capture observation are treated only as claims: this
        method resolves the source packet by stored artifact fingerprint,
        reparses the stored scan ``request_payload``, and rederives every C7
        evidence identity from durable evidence rows before inserting anything.
        The isolated parser below is the sole adapter point for the pure export
        module.
        """

        domain = normalize_domain(domain_or_url)
        prepared = _prepare_evidence_vault_lineage_seed_export(
            lineage_seed_export,
            expected_brand=domain,
            workspace_slug=workspace_slug,
        )
        self._ensure_migrated()
        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id, brands.workspace_id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The lineage binding brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(brand["workspace_id"], "brand", brand_id),),
            )
            source_rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_kind = 'operational_source_v2'
                  AND reference_resolution ->> 'source_kind' =
                      'exact_relation_supplement'
                  AND reference_resolution ->> 'artifact_fingerprint' = %s
                ORDER BY created_at, id
                """,
                (brand_id, prepared["source_relation_artifact_fingerprint"]),
            ).fetchall()
            if len(source_rows) != 1:
                raise EvidenceVaultOperationalAuthorityError(
                    "The lineage export does not resolve one exact source."
                )
            source_row = source_rows[0]
            source_resolution = dict(source_row["reference_resolution"] or {})
            if source_resolution.get("artifact") != prepared["exact_artifact"]:
                raise EvidenceVaultOperationalAuthorityError(
                    "The lineage export exact source differs from durable storage."
                )
            capture = conn.execute(
                f"""
                SELECT captures.id AS capture_id,
                       captures.content_hash AS capture_content_hash,
                       scan_runs.request_payload, scan_runs.metadata,
                       scan_runs.source_scan_id
                FROM {_SCHEMA}.scan_runs
                JOIN {_SCHEMA}.captures ON captures.scan_run_id = scan_runs.id
                WHERE scan_runs.brand_id = %s
                  AND scan_runs.source_scan_id = %s
                """,
                (brand_id, prepared["capture"].source_scan_id),
            ).fetchone()
            if capture is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The lineage export capture does not exist."
                )
            try:
                stored_capture = parse_capture_observation(
                    dict(capture["request_payload"] or {})
                )
            except Exception as exc:
                raise EvidenceVaultOperationalAuthorityError(
                    "The stored capture is not an exact capture observation."
                ) from exc
            if (
                stored_capture.raw_observation
                != prepared["capture"].raw_observation
                or stored_capture.observation_hash
                != prepared["capture"].observation_hash
                or stored_capture.capture_hash != prepared["capture"].capture_hash
                or str(capture["capture_content_hash"])
                != prepared["capture"].capture_hash
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    "The lineage export capture differs from durable storage."
                )
            evidence_rows = conn.execute(
                f"""
                SELECT id, evidence_ref, source, source_class, evidence_type,
                       url, content, content_raw, confidence, metadata
                FROM {_SCHEMA}.evidence_records
                WHERE capture_id = %s
                ORDER BY evidence_ref, id
                """,
                (capture["capture_id"],),
            ).fetchall()
            members = _rederive_evidence_vault_c7_lineage_members(
                evidence_rows,
                brand_identity=domain,
                subject_url=str(prepared["exact_artifact"]["subject_url"]),
                exact_artifact=prepared["exact_artifact"],
                declared_bindings=prepared["evidence_bindings"],
            )
            watermark = conn.execute(
                f"""
                SELECT events.*, brands.canonical_domain
                FROM {_SCHEMA}.evidence_vault_capture_watermark_events AS events
                JOIN {_SCHEMA}.brands ON brands.id = events.brand_id
                WHERE events.brand_id = %s
                ORDER BY events.capture_sequence DESC
                LIMIT 1
                """,
                (brand_id,),
            ).fetchone()
            if watermark is None or str(watermark["capture_id"]) != str(
                capture["capture_id"]
            ):
                raise EvidenceVaultOperationalAdoptionConflictError(
                    "The lineage export is not bound to the current capture watermark."
                )
            capture_sequence = int(watermark["capture_sequence"])
            expected_event_origin = (
                "report_derived_candidate_capture_replay"
                if prepared["provenance"] == "report_derived_candidate_capture"
                else "live_capture_observation"
            )
            if (
                prepared["capture_sequence"] != capture_sequence
                or prepared["expected_predecessor_event_fingerprint"]
                != watermark.get("previous_event_fingerprint")
                or str(watermark["append_origin"]) != expected_event_origin
            ):
                raise EvidenceVaultOperationalAdoptionConflictError(
                    "The lineage export capture sequence is not current."
                )
            member_set_fingerprint = canonical_fingerprint(
                "evidence-vault-source-capture-lineage-member-set-v1",
                [row["content"] for row in members],
            )
            binding_content = {
                "schema_version": "evidence-vault-source-capture-lineage-binding-v1",
                "brand_identity": domain,
                "source_candidate_packet_fingerprint": str(
                    source_row["packet_fingerprint"]
                ),
                "watermark_event_fingerprint": str(
                    watermark["event_fingerprint"]
                ),
                "capture_id": str(capture["capture_id"]),
                "capture_sequence": capture_sequence,
                "provenance": prepared["provenance"],
                "lineage_artifact_fingerprint": prepared["artifact_fingerprint"],
                "lineage_export_identity": prepared["lineage_export_identity"],
                "lineage_export_fingerprint": prepared[
                    "lineage_export_fingerprint"
                ],
                "replay_origin_sequence": prepared["replay_origin_sequence"],
                "member_set_fingerprint": member_set_fingerprint,
            }
            binding_fingerprint = canonical_fingerprint(
                "evidence-vault-source-capture-lineage-binding-v1",
                binding_content,
            )
            binding_id = _stable_uuid(
                brand_id,
                "evidence-vault-source-capture-lineage-binding-v1",
                binding_fingerprint,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_operational_source_capture_lineage_bindings (
                    id, brand_id, operational_source_packet_id,
                    watermark_event_id, capture_id, capture_sequence,
                    provenance, lineage_artifact_schema_version,
                    lineage_artifact_fingerprint, lineage_export_identity,
                    lineage_export_fingerprint, replay_origin_sequence,
                    member_set_fingerprint, binding_fingerprint,
                    authority, production_runtime_effect, scanner_runtime_effect
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    'evidence-vault-lineage-seed-export-v2',
                    %s, %s, %s, %s, %s, %s, false, false, false
                )
                ON CONFLICT (
                    brand_id, operational_source_packet_id, capture_sequence
                ) DO NOTHING
                RETURNING *
                """,
                (
                    binding_id,
                    brand_id,
                    source_row["id"],
                    watermark["id"],
                    capture["capture_id"],
                    capture_sequence,
                    prepared["provenance"],
                    prepared["artifact_fingerprint"],
                    prepared["lineage_export_identity"],
                    prepared["lineage_export_fingerprint"],
                    prepared["replay_origin_sequence"],
                    member_set_fingerprint,
                    binding_fingerprint,
                ),
            ).fetchone()
            replayed = inserted is None
            binding_row = inserted or conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_operational_source_capture_lineage_bindings
                WHERE brand_id = %s
                  AND operational_source_packet_id = %s
                  AND capture_sequence = %s
                """,
                (brand_id, source_row["id"], capture_sequence),
            ).fetchone()
            if (
                binding_row is None
                or str(binding_row["binding_fingerprint"])
                != binding_fingerprint
                or str(binding_row["lineage_artifact_fingerprint"])
                != prepared["artifact_fingerprint"]
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    "The lineage checkpoint conflicts with immutable content."
                )
            if not replayed:
                for member in members:
                    content = member["content"]
                    member_fingerprint = canonical_fingerprint(
                        "evidence-vault-source-capture-lineage-member-v1",
                        content,
                    )
                    member_id = _stable_uuid(
                        binding_id,
                        "evidence-vault-source-capture-lineage-member-v1",
                        member_fingerprint,
                    )
                    conn.execute(
                        f"""
                        INSERT INTO {_SCHEMA}.evidence_vault_operational_source_capture_lineage_members (
                            id, brand_id, binding_id,
                            operational_source_packet_id, capture_id,
                            evidence_record_id, composite_group_id, relation_id,
                            evidence_id, source_identity_id,
                            evidence_fingerprint, channel_role, evidence_ref,
                            source_ref, evidence_quote, member_fingerprint,
                            authority, production_runtime_effect,
                            scanner_runtime_effect
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, false, false, false
                        )
                        """,
                        (
                            member_id,
                            brand_id,
                            binding_id,
                            source_row["id"],
                            capture["capture_id"],
                            member["evidence_record_id"],
                            content["group_id"],
                            content["relation_id"],
                            content["evidence_id"],
                            content["source_identity_id"],
                            content["evidence_fingerprint"],
                            content["channel_role"],
                            content["evidence_ref"],
                            content["source_ref"],
                            content["evidence_quote"],
                            member_fingerprint,
                        ),
                    )
            stored_members = conn.execute(
                f"""
                SELECT relation_id, evidence_id, source_identity_id,
                       evidence_fingerprint, channel_role, evidence_ref,
                       source_ref, evidence_quote, composite_group_id
                FROM {_SCHEMA}.evidence_vault_operational_source_capture_lineage_members
                WHERE binding_id = %s
                ORDER BY relation_id
                """,
                (binding_row["id"],),
            ).fetchall()
            expected_member_content = sorted(
                (row["content"] for row in members),
                key=lambda row: row["relation_id"],
            )
            if [
                {
                    "group_id": str(row["composite_group_id"]),
                    "relation_id": str(row["relation_id"]),
                    "evidence_id": str(row["evidence_id"]),
                    "source_identity_id": str(row["source_identity_id"]),
                    "evidence_fingerprint": str(row["evidence_fingerprint"]),
                    "channel_role": str(row["channel_role"]),
                    "evidence_ref": str(row["evidence_ref"]),
                    "source_ref": str(row["source_ref"]),
                    "evidence_quote": str(row["evidence_quote"]),
                }
                for row in stored_members
            ] != expected_member_content:
                raise EvidenceVaultOperationalAuthorityError(
                    "The lineage checkpoint members conflict with immutable content."
                )
            return {
                **binding_content,
                "binding_id": str(binding_row["id"]),
                "binding_fingerprint": binding_fingerprint,
                "member_count": 2,
                "authority": False,
                "production_runtime_effect": False,
                "scanner_runtime_effect": False,
            }, replayed

    def bind_evidence_vault_exact_source_current_live_capture(
        self,
        domain_or_url: str,
        *,
        exact_source_candidate_packet_fingerprint: str,
        source_scan_id: str,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Checkpoint the current original/live capture without caller member IDs."""

        domain = normalize_domain(domain_or_url)
        source_fingerprint = _require_sha256_text(
            exact_source_candidate_packet_fingerprint,
            field="exact_source_candidate_packet_fingerprint",
        )
        scan_id = _bounded_text(
            source_scan_id, field="source_scan_id", maximum=500
        )
        self._ensure_migrated()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT packets.reference_resolution,
                       scan_runs.request_payload, scan_runs.metadata,
                       captures.id AS capture_id,
                       events.capture_sequence,
                       events.previous_event_fingerprint
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces ON workspaces.id = brands.workspace_id
                JOIN {_SCHEMA}.evidence_vault_canonical_memory_packets AS packets
                  ON packets.brand_id = brands.id
                JOIN {_SCHEMA}.scan_runs ON scan_runs.brand_id = brands.id
                JOIN {_SCHEMA}.captures ON captures.scan_run_id = scan_runs.id
                JOIN {_SCHEMA}.evidence_vault_capture_watermark_events AS events
                  ON events.brand_id = brands.id
                 AND events.capture_id = captures.id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                  AND packets.packet_fingerprint = %s
                  AND packets.packet_kind = 'operational_source_v2'
                  AND packets.reference_resolution ->> 'source_kind' =
                      'exact_relation_supplement'
                  AND scan_runs.source_scan_id = %s
                  AND events.append_origin = 'live_capture_observation'
                  AND events.capture_sequence = (
                      SELECT max(head.capture_sequence)
                      FROM {_SCHEMA}.evidence_vault_capture_watermark_events AS head
                      WHERE head.brand_id = brands.id
                  )
                """,
                (workspace_slug, domain, source_fingerprint, scan_id),
            ).fetchall()
            if len(rows) != 1:
                raise EvidenceVaultOperationalAdoptionConflictError(
                    "The exact source and live capture do not resolve at the current head."
                )
            row = rows[0]
            metadata = dict(row["metadata"] or {})
            if metadata.get("acquisition_classification") == (
                "report_derived_candidate_capture"
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    "A report-derived capture cannot be a live runtime checkpoint."
                )
            try:
                capture = parse_capture_observation(
                    dict(row["request_payload"] or {})
                )
            except Exception as exc:
                raise EvidenceVaultOperationalAuthorityError(
                    "The current live capture observation is invalid."
                ) from exc
            exact_artifact = dict(row["reference_resolution"] or {}).get(
                "artifact"
            )
            if not isinstance(exact_artifact, Mapping):
                raise EvidenceVaultOperationalAuthorityError(
                    "The exact source artifact is unavailable."
                )
            c7 = [
                dict(group)
                for group in exact_artifact.get("groups") or []
                if isinstance(group, Mapping) and group.get("tile_id") == "C7"
            ]
            if len(c7) != 1 or len(c7[0].get("relations") or []) != 2:
                raise EvidenceVaultOperationalAuthorityError(
                    "The exact source C7 group is invalid."
                )
            ref_indices = {
                str(value.get("ref") or ""): index
                for index, value in enumerate(capture.evidence_records)
            }
            bindings = [
                {
                    "group_id": c7[0]["group_id"],
                    "relation_id": relation["relation_id"],
                    "tile_id": "C7",
                    "decision_rule": "all_of",
                    "evidence_fingerprint": relation["evidence_fingerprint"],
                    "evidence_id": relation["evidence_id"],
                    "source_identity_id": relation["source_identity_id"],
                    "source_ref": relation["ref"],
                    "source_url": relation["url"],
                    "source_class": relation["source_class"],
                    "channel_role": relation["channel_role"],
                    "evidence_quote": relation["literal_quote"],
                    "capture_evidence_index": ref_indices.get(relation["ref"], -1),
                }
                for relation in c7[0]["relations"]
            ]
            filler_hash = canonical_fingerprint(
                "evidence-vault-live-capture-lineage-filler-v1",
                {
                    "source_scan_id": scan_id,
                    "observation_hash": capture.observation_hash,
                },
            )
            payload = {
                "schema_version": "evidence-vault-lineage-seed-export-v2",
                "workspace_slug": workspace_slug,
                "seed_id": f"live:{scan_id}",
                "lineage_export_identity": f"live:{scan_id}",
                "lineage_export_ordinal": int(row["capture_sequence"]),
                "capture_sequence": int(row["capture_sequence"]),
                "replay_origin_sequence": int(row["capture_sequence"]),
                "expected_predecessor_event_fingerprint": row[
                    "previous_event_fingerprint"
                ],
                "lineage_kind": "live_capture_observation",
                "acquisition_classification": "live_capture_observation",
                "provenance_policy_versions": {
                    "lineage_binding": "evidence-vault-lineage-seed-export-v2"
                },
                "brand_identity": domain,
                "subject_url": str(exact_artifact["subject_url"]),
                "source_artifact_name": scan_id,
                "source_raw_bytes_sha256": filler_hash,
                "source_raw_hash_verification": "durable_capture_observation",
                "source_report_canonical_sha256": filler_hash,
                "source_relation_artifact_fingerprint": exact_artifact[
                    "artifact_fingerprint"
                ],
                "capture_observation_hash": capture.observation_hash,
                "capture_hash": capture.capture_hash,
                "raw_evidence_pack_canonical_sha256": filler_hash,
                "normalized_evidence_pack_canonical_sha256": exact_artifact[
                    "source_evidence_pack_canonical_sha256"
                ],
                "normalization_policy": {},
                "source_historical_report": {},
                "source_normalized_evidence_pack": {},
                "source_exact_relation_supplement": dict(exact_artifact),
                "capture_observation": capture.raw_observation,
                "evidence_bindings": bindings,
                "group_count": 1,
                "relation_count": 2,
                "adoption_eligible": False,
                "authority": False,
                "runtime_effect": False,
                "production_runtime_effect": False,
                "scanner_runtime_effect": False,
                "cutover_authorized": False,
            }
            payload["lineage_export_fingerprint"] = canonical_fingerprint(
                "evidence-vault-live-capture-lineage-manifest-v1", payload
            )
            payload["artifact_fingerprint"] = canonical_fingerprint(
                "evidence-vault-lineage-seed-export-v2", payload
            )
        return self.bind_evidence_vault_exact_source_capture_lineage(
            domain,
            payload,
            workspace_slug=workspace_slug,
        )

    def reopen_evidence_vault_composite_group(
        self,
        domain_or_url: str,
        *,
        exact_source_candidate_packet_fingerprint: str,
        source_scan_id: str | None = None,
        accepted_contradiction_review_event_id: str | None = None,
        workspace_slug: str = "b3s",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Atomically reopen one accepted C7 ``all_of`` group.

        The trigger must already be durable: either a completed incremental
        operation plan whose delta materially supersedes a group member, or an
        accepted contradictory relation-review event.  The source transition,
        score-lowering operational packet, and CAS adoption are inserted in one
        transaction under the brand promotion lock.
        """

        domain = normalize_domain(domain_or_url)
        exact_fingerprint = _require_sha256_text(
            exact_source_candidate_packet_fingerprint,
            field="exact_source_candidate_packet_fingerprint",
        )
        scan_id = str(source_scan_id or "").strip()
        review_event_text = str(
            accepted_contradiction_review_event_id or ""
        ).strip()
        if not domain or bool(scan_id) == bool(review_event_text):
            raise EvidenceVaultOperationalAuthorityError(
                "Exactly one durable composite-group reopen trigger is required."
            )
        timestamp = _normalized_event_timestamp(
            created_at or datetime.now(timezone.utc).isoformat(),
            field="created_at",
        )
        self._ensure_migrated()
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
                raise EvidenceVaultOperationalAuthorityError(
                    "The composite-group brand does not exist in durable history."
                )
            brand_id = brand["id"]
            operation = None
            if scan_id:
                operation_row = _vault_operation_row(
                    conn,
                    workspace_slug=workspace_slug,
                    source_scan_id=scan_id,
                    for_update=True,
                )
                if (
                    operation_row is None
                    or str(operation_row["canonical_domain"]) != domain
                ):
                    raise EvidenceVaultOperationalAuthorityError(
                        "The material-change operation plan does not exist."
                    )
                operation = _vault_operation_plan_record(operation_row)
                if operation["status"] != "completed":
                    raise EvidenceVaultOperationalAuthorityError(
                        "The material-change operation is not complete."
                    )
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-vault-canonical-promotion",
                    ),
                ),
            )
            exact_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND packet_kind = 'operational_source_v2'
                  AND reference_resolution ->> 'source_kind' =
                      'exact_relation_supplement'
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE
                """,
                (brand_id, exact_fingerprint),
            ).fetchone()
            if exact_row is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The exact composite-group source does not exist."
                )
            exact_record = _vault_operational_source_packet_record(exact_row)

            evidence_delta = None
            accepted_contradiction = None
            if scan_id:
                if operation is None:
                    raise EvidenceVaultOperationalAuthorityError(
                        "The material-change operation plan does not exist."
                    )
                evidence_delta = dict(operation["plan"].get("delta") or {})
                durable_fingerprint = _require_sha256_text(
                    evidence_delta.get("delta_fingerprint"),
                    field="delta_fingerprint",
                )
                durable_trigger = {
                    "kind": "operation_plan",
                    "id": operation["operation_plan_id"],
                    "fingerprint": durable_fingerprint,
                }
                expected_parent = operation["canonical_memory_version"]
            else:
                try:
                    review_event_uuid = UUID(review_event_text)
                except (TypeError, ValueError, AttributeError) as exc:
                    raise EvidenceVaultOperationalAuthorityError(
                        "The contradiction review event id must be a UUID."
                    ) from exc
                review_row = conn.execute(
                    f"""
                    SELECT reviews.*, packets.packet_payload
                    FROM {_SCHEMA}.evidence_vault_operational_relation_reviews
                         AS reviews
                    JOIN {_SCHEMA}.evidence_vault_canonical_memory_packets
                         AS packets
                      ON packets.id = reviews.source_packet_id
                    WHERE reviews.brand_id = %s
                      AND reviews.id = %s
                      AND packets.packet_kind = 'operational_source_v2'
                    FOR UPDATE OF reviews
                    """,
                    (brand_id, review_event_uuid),
                ).fetchone()
                if review_row is None or str(review_row["decision"]) != "accept":
                    raise EvidenceVaultOperationalAuthorityError(
                        "The accepted contradiction review does not exist."
                    )
                contradiction_source = dict(review_row["packet_payload"] or {})
                try:
                    validate_candidate_packet(contradiction_source)
                except EvidenceVaultCanonicalCoreError as exc:
                    raise EvidenceVaultOperationalAuthorityError(
                        "The contradiction source packet is invalid."
                    ) from exc
                matches = [
                    (str(tile["tile_id"]), dict(relation))
                    for tile in contradiction_source["candidate_tiles"]
                    for relation in tile.get("basis") or []
                    if str(relation.get("relation_id") or "")
                    == str(review_row["relation_id"])
                ]
                if len(matches) != 1:
                    raise EvidenceVaultOperationalAuthorityError(
                        "The contradiction review relation cannot be resolved."
                    )
                tile_id, relation = matches[0]
                accepted_contradiction = {
                    "kind": "accepted_contradiction",
                    "source_candidate_packet_fingerprint": str(
                        review_row["source_packet_fingerprint"]
                    ),
                    "relation_id": str(review_row["relation_id"]),
                    "tile_id": tile_id,
                    "evidence_id": str(relation["evidence_id"]),
                    "source_identity_id": str(relation["source_identity_id"]),
                    "decision_event_id": str(review_row["id"]),
                    "review_request_fingerprint": str(
                        review_row["review_request_fingerprint"]
                    ),
                    "decision": "accept",
                    "polarity": str(relation["polarity"]),
                }
                durable_fingerprint = canonical_fingerprint(
                    "evidence-vault-composite-group-contradiction-review-trigger-v1",
                    accepted_contradiction,
                )
                durable_trigger = {
                    "kind": "relation_review",
                    "id": str(review_row["id"]),
                    "fingerprint": durable_fingerprint,
                }
                expected_parent = contradiction_source["manifest"][
                    "parent_canonical_memory_version"
                ]

            replay_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_kind = 'operational_source_v2'
                  AND reference_resolution ->> 'source_kind' =
                      'composite_group_reopen'
                  AND reference_resolution ->>
                      'prior_group_source_candidate_packet_fingerprint' = %s
                  AND reference_resolution #>>
                      '{{durable_trigger,fingerprint}}' = %s
                ORDER BY created_at, id
                LIMIT 1
                """,
                (brand_id, exact_fingerprint, durable_fingerprint),
            ).fetchone()
            if replay_row is not None:
                return _composite_group_reopen_repository_result(
                    conn,
                    brand_id=brand_id,
                    source_row=replay_row,
                    replayed=True,
                )

            current = _project_vault_operational_memory(conn, brand_id)
            current_version = (
                current["canonical_memory_version"]
                if current is not None
                else None
            )
            if current is None or expected_parent != current_version:
                raise EvidenceVaultOperationalAdoptionConflictError(
                    "The durable reopen trigger has a stale canonical parent."
                )
            reopen_event_id = _stable_uuid(
                brand_id,
                "evidence-vault-composite-group-reopen",
                exact_fingerprint,
                durable_fingerprint,
                current_version,
            )
            try:
                artifact = build_composite_group_reopen_artifact(
                    current_operational_memory=current,
                    exact_source_record=exact_record,
                    reopen_event_id=str(reopen_event_id),
                    evidence_delta=evidence_delta,
                    accepted_contradiction=accepted_contradiction,
                )
                source = build_composite_group_reopen_source_candidate(
                    artifact,
                    current_operational_memory=current,
                )
                resolution = build_composite_group_reopen_source_resolution(
                    artifact,
                    source_candidate_packet=source,
                    durable_trigger=durable_trigger,
                )
                validate_composite_group_reopen_source_resolution(
                    resolution,
                    source_candidate_packet=source,
                )
                operational = build_composite_group_reopen_operational_packet(
                    artifact,
                    source_candidate_packet=source,
                    current_operational_memory=current,
                )
            except EvidenceVaultCompositeGroupLifecycleError as exc:
                raise EvidenceVaultOperationalAuthorityError(
                    "The composite-group reopen transition is invalid."
                ) from exc
            _validate_operational_packet_lineage_for_storage(
                operational,
                current_memory=current,
                source_candidate_packet=source,
                source_reference_resolution=resolution,
            )

            resolution_fingerprint = canonical_fingerprint(
                "evidence-vault-operational-source-resolution-v1",
                resolution,
            )
            inserted_source = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'pending_review', false, false, false,
                    'operational_source_v2', %s
                )
                ON CONFLICT (
                    brand_id, packet_fingerprint,
                    reference_resolution_fingerprint
                ) DO NOTHING
                RETURNING *
                """,
                (
                    reopen_event_id,
                    brand_id,
                    source["candidate_packet_fingerprint"],
                    source["manifest"]["schema_version"],
                    source["manifest"]["brand_identity"],
                    source["manifest"]["parent_canonical_memory_version"],
                    resolution_fingerprint,
                    _jsonb(resolution),
                    _jsonb(source["manifest"]),
                    _jsonb(source["candidate_tiles"]),
                    _jsonb(source),
                ),
            ).fetchone()
            source_row = inserted_source or conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND reference_resolution_fingerprint = %s
                  AND packet_kind = 'operational_source_v2'
                """,
                (
                    brand_id,
                    source["candidate_packet_fingerprint"],
                    resolution_fingerprint,
                ),
            ).fetchone()
            if source_row is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The composite-group reopen source could not be persisted."
                )
            stored_source = _vault_operational_source_packet_record(source_row)
            if (
                stored_source["packet"] != source
                or stored_source["reference_resolution"] != resolution
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    "The composite-group reopen source replay differs."
                )

            storage_resolution = _operational_storage_resolution(operational)
            operational_id = _stable_uuid(
                brand_id,
                "evidence-vault-operational-memory-packet",
                operational["candidate_packet_fingerprint"],
            )
            operational_row = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint,
                    reference_resolution, manifest, candidate_tiles,
                    authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload,
                    accepted_memory_candidate_version,
                    candidate_overlay_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'pending_review', false, false, false,
                    'operational_v2', %s, %s, %s
                )
                RETURNING *
                """,
                (
                    operational_id,
                    brand_id,
                    operational["candidate_packet_fingerprint"],
                    operational["schema_version"],
                    domain,
                    operational["current_canonical_memory_version"],
                    storage_resolution["reference_resolution_fingerprint"],
                    _jsonb(storage_resolution),
                    _jsonb(
                        {
                            "schema_version": operational["schema_version"],
                            "packet_kind": "operational_v2",
                            "brand_identity": domain,
                            "current_canonical_memory_version": operational[
                                "current_canonical_memory_version"
                            ],
                            "proposed_canonical_memory_version": operational[
                                "proposed_canonical_memory_version"
                            ],
                            "accepted_memory_candidate_version": operational[
                                "accepted_memory_candidate_version"
                            ],
                            "candidate_overlay_version": operational[
                                "candidate_overlay_version"
                            ],
                            "authority": False,
                            "runtime_effect": False,
                        }
                    ),
                    _jsonb(operational["scoring_projection"]["tiles"]),
                    _jsonb(operational),
                    operational["accepted_memory_candidate_version"],
                    operational["candidate_overlay_version"],
                ),
            ).fetchone()
            stored_operational = _vault_operational_packet_record(
                operational_row
            )
            if stored_operational["packet"] != operational:
                raise EvidenceVaultOperationalAuthorityError(
                    "The composite-group operational packet replay differs."
                )

            previous = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_promotion_events
                WHERE brand_id = %s AND adoption_kind = 'operational_v2'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (brand_id,),
            ).fetchone()
            if previous is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "Composite-group reopen requires an operational parent event."
                )
            sequence = int(previous["sequence"]) + 1
            previous_event_id = str(previous["id"])
            idempotency_key_hash = canonical_fingerprint(
                "evidence-vault-composite-group-reopen-adoption-v1",
                {
                    "brand_identity": domain,
                    "old_group_id": artifact["group_id"],
                    "trigger_fingerprint": artifact["trigger"][
                        "trigger_fingerprint"
                    ],
                    "parent_canonical_memory_version": current_version,
                },
            )
            adoption_event_id = _stable_uuid(
                brand_id,
                "evidence-vault-operational-adoption",
                idempotency_key_hash,
            )
            adoption = build_operational_adoption_event(
                operational,
                event_id=str(adoption_event_id),
                sequence=sequence,
                previous_event_id=previous_event_id,
                adopted_by="policy",
                actor_id=COMPOSITE_GROUP_REOPEN_POLICY_ACTOR,
                policy_fingerprint=artifact["artifact_fingerprint"],
                created_at=timestamp,
                idempotency_key_hash=idempotency_key_hash,
                expected_current_canonical_memory_version=current_version,
            )
            _validate_operational_adoption_attribution(
                operational,
                current_memory=current,
                adopted_by="policy",
                policy_fingerprint=artifact["artifact_fingerprint"],
            )
            adoption_row = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_promotion_events (
                    id, brand_id, event_type, sequence, previous_event_id,
                    brand_identity, candidate_packet_fingerprint,
                    reference_resolution_fingerprint,
                    promotion_policy_fingerprint,
                    parent_canonical_memory_version,
                    promoted_canonical_memory_version, decision,
                    reviewer_id, reviewed_at, rationale, schema_version,
                    idempotency_key_hash, request_fingerprint, authority,
                    authority_scope, production_runtime_effect,
                    scanner_runtime_effect, adoption_kind, adopted_by,
                    actor_id, event_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, 'promote', %s, %s, %s, %s, %s, %s, true,
                    'b3s-vault', false, false, 'operational_v2', %s, %s, %s
                )
                RETURNING *
                """,
                (
                    adoption["event_id"],
                    brand_id,
                    adoption["event_type"],
                    adoption["sequence"],
                    adoption["previous_event_id"],
                    adoption["brand_identity"],
                    adoption["candidate_packet_fingerprint"],
                    operational_row["reference_resolution_fingerprint"],
                    adoption["policy_fingerprint"],
                    adoption["parent_canonical_memory_version"],
                    adoption["promoted_canonical_memory_version"],
                    adoption["actor_id"],
                    adoption["created_at"],
                    "Whole C7 all-of group reopened after durable material change.",
                    adoption["schema_version"],
                    adoption["idempotency_key_hash"],
                    adoption["request_fingerprint"],
                    adoption["adopted_by"],
                    adoption["actor_id"],
                    _jsonb(adoption),
                ),
            ).fetchone()
            _vault_operational_adoption_event_record(adoption_row)
            return _composite_group_reopen_repository_result(
                conn,
                brand_id=brand_id,
                source_row=source_row,
                replayed=False,
            )

    def register_evidence_vault_coverage_supplement_source_packet(
        self,
        domain_or_url: str,
        artifact: Mapping[str, Any],
        *,
        evidence_pack: Mapping[str, Any],
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Persist one exact coverage supplement as pending-only source."""

        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceVaultOperationalAuthorityError(
                "Coverage supplement brand is required."
            )
        try:
            validate_coverage_supplement_artifact(
                artifact,
                evidence_pack=evidence_pack,
            )
        except EvidenceVaultCoverageSupplementError as exc:
            raise EvidenceVaultOperationalAuthorityError(
                "The coverage supplement artifact is invalid."
            ) from exc
        request = artifact["request"]
        result = artifact["result"]
        if str(request["brand_identity"]) != domain:
            raise EvidenceVaultOperationalAuthorityError(
                "The coverage supplement belongs to another brand."
            )
        self._ensure_migrated()
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
                raise EvidenceVaultOperationalAuthorityError(
                    "The coverage supplement brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-vault-canonical-promotion",
                    ),
                ),
            )
            existing_rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_kind = 'operational_source_v2'
                  AND reference_resolution ->> 'source_kind' = 'coverage_supplement'
                  AND reference_resolution ->> 'result_fingerprint' = %s
                FOR UPDATE
                """,
                (brand_id, result["result_fingerprint"]),
            ).fetchall()
            if len(existing_rows) > 1:
                raise EvidenceVaultOperationalAuthorityError(
                    "Coverage supplement result resolves to multiple sources."
                )
            if existing_rows:
                stored = _vault_operational_source_packet_record(
                    existing_rows[0]
                )
                expected_resolution = (
                    build_coverage_supplement_source_resolution(
                        artifact,
                        source_candidate_packet=stored["packet"],
                    )
                )
                if stored["reference_resolution"] != expected_resolution:
                    raise EvidenceVaultOperationalAuthorityError(
                        "Coverage supplement replay differs from its immutable source."
                    )
                return stored, True
            current = _project_vault_operational_memory(conn, brand_id)
            current_version = (
                current["canonical_memory_version"]
                if current is not None
                else None
            )
            if current_version != request[
                "parent_canonical_memory_version"
            ]:
                raise EvidenceVaultOperationalAdoptionConflictError(
                    "Coverage supplement has a stale canonical parent."
                )
            try:
                source = build_coverage_supplement_source_candidate(
                    artifact,
                    evidence_pack=evidence_pack,
                    current_operational_memory=current,
                )
                resolution = build_coverage_supplement_source_resolution(
                    artifact,
                    source_candidate_packet=source,
                )
            except EvidenceVaultCoverageSupplementError as exc:
                raise EvidenceVaultOperationalAuthorityError(
                    "The coverage supplement source projection is invalid."
                ) from exc
            resolution_fingerprint = canonical_fingerprint(
                "evidence-vault-operational-source-resolution-v1",
                resolution,
            )
            packet_id = _stable_uuid(
                brand_id,
                "evidence-vault-coverage-supplement-source-packet",
                source["candidate_packet_fingerprint"],
                resolution_fingerprint,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'pending_review', false, false, false,
                    'operational_source_v2', %s
                )
                ON CONFLICT (
                    brand_id, packet_fingerprint,
                    reference_resolution_fingerprint
                ) DO NOTHING
                RETURNING *
                """,
                (
                    packet_id,
                    brand_id,
                    source["candidate_packet_fingerprint"],
                    source["manifest"]["schema_version"],
                    source["manifest"]["brand_identity"],
                    source["manifest"][
                        "parent_canonical_memory_version"
                    ],
                    resolution_fingerprint,
                    _jsonb(resolution),
                    _jsonb(source["manifest"]),
                    _jsonb(source["candidate_tiles"]),
                    _jsonb(source),
                ),
            ).fetchone()
            replayed = inserted is None
            row = inserted or conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND reference_resolution_fingerprint = %s
                  AND packet_kind = 'operational_source_v2'
                """,
                (
                    brand_id,
                    source["candidate_packet_fingerprint"],
                    resolution_fingerprint,
                ),
            ).fetchone()
            if row is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The coverage supplement source could not be persisted."
                )
            stored = _vault_operational_source_packet_record(row)
            expected = {
                "packet": source,
                "reference_resolution": resolution,
                "authority": False,
                "authority_scope": "b3s-vault",
                "production_runtime_effect": False,
                "scanner_runtime_effect": False,
            }
            if stored != expected:
                raise EvidenceVaultOperationalAuthorityError(
                    "Coverage supplement source identity resolves to different content."
                )
            return stored, replayed

    def register_evidence_vault_operational_source_packet(
        self,
        domain_or_url: str,
        packet: Mapping[str, Any],
        *,
        source_scan_id: str,
        operation_plan_fingerprint: str,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Persist one model-only source packet bound to a frozen plan result."""

        candidate = dict(packet)
        try:
            validate_candidate_packet(candidate)
        except EvidenceVaultCanonicalCoreError as exc:
            raise EvidenceVaultOperationalAuthorityError(
                "The operational source candidate packet is invalid."
            ) from exc
        domain = normalize_domain(domain_or_url)
        plan_fingerprint = _require_sha256_text(
            operation_plan_fingerprint,
            field="operation_plan_fingerprint",
        )
        scan_id = str(source_scan_id or "").strip()
        if not domain or not scan_id:
            raise EvidenceVaultOperationalAuthorityError(
                "Operational source brand and scan are required."
            )
        if candidate["manifest"]["brand_identity"] != domain:
            raise EvidenceVaultOperationalAuthorityError(
                "The operational source packet belongs to another brand."
            )
        self._ensure_migrated()
        with self._connect() as conn:
            operation = _vault_operation_row(
                conn,
                workspace_slug=workspace_slug,
                source_scan_id=scan_id,
                for_update=True,
            )
            if operation is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The source operation plan does not exist."
                )
            if (
                str(operation["operation_plan_fingerprint"]) != plan_fingerprint
                or str(operation["status"])
                not in {"result_persisted", "completed"}
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    "The source operation has no exact persisted result."
                )
            operation_plan = dict(operation.get("plan_payload") or {})
            if (
                domain != str(operation["canonical_domain"])
                or domain != str(operation_plan.get("brand_identity") or "")
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    "The source packet brand is not bound to the capture operation."
                )
            brand_id = operation["brand_id"]
            resolution = {
                "schema_version": "evidence-vault-operational-source-resolution-v1",
                "operation_plan_fingerprint": plan_fingerprint,
                "observation_hash": str(operation["observation_hash"]),
                "result_fingerprint": str(operation["result_fingerprint"]),
                "source_candidate_packet_fingerprint": candidate[
                    "candidate_packet_fingerprint"
                ],
            }
            resolution_fingerprint = canonical_fingerprint(
                "evidence-vault-operational-source-resolution-v1",
                resolution,
            )
            if str(operation["status"]) == "completed":
                replay_row = conn.execute(
                    f"""
                    SELECT *
                    FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                    WHERE brand_id = %s
                      AND packet_fingerprint = %s
                      AND reference_resolution_fingerprint = %s
                      AND packet_kind = 'operational_source_v2'
                    """,
                    (
                        brand_id,
                        candidate["candidate_packet_fingerprint"],
                        resolution_fingerprint,
                    ),
                ).fetchone()
                if replay_row is None:
                    raise EvidenceVaultOperationalAuthorityError(
                        "Completed operation has no exact source packet."
                    )
                replay_record = _vault_operational_source_packet_record(
                    replay_row
                )
                replay_resolution = replay_record["reference_resolution"]
                if (
                    replay_record["packet"] != candidate
                    or replay_resolution.get(
                        "operation_plan_fingerprint"
                    )
                    != plan_fingerprint
                    or replay_resolution.get("result_fingerprint")
                    != str(operation["result_fingerprint"])
                ):
                    raise EvidenceVaultOperationalAuthorityError(
                        "Completed source replay differs from its immutable result."
                    )
                return replay_record, True
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(brand_id, "evidence-vault-canonical-promotion"),),
            )
            current = _project_vault_operational_memory(conn, brand_id)
            _validate_operational_source_packet_for_storage(
                candidate,
                operation=operation,
                current_memory=current,
            )
            packet_id = _stable_uuid(
                brand_id,
                "evidence-vault-operational-source-packet",
                candidate["candidate_packet_fingerprint"],
                resolution_fingerprint,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'pending_review', false, false, false,
                    'operational_source_v2', %s
                )
                ON CONFLICT (
                    brand_id, packet_fingerprint,
                    reference_resolution_fingerprint
                ) DO NOTHING
                RETURNING *
                """,
                (
                    packet_id,
                    brand_id,
                    candidate["candidate_packet_fingerprint"],
                    candidate["manifest"]["schema_version"],
                    candidate["manifest"]["brand_identity"],
                    candidate["manifest"]["parent_canonical_memory_version"],
                    resolution_fingerprint,
                    _jsonb(resolution),
                    _jsonb(candidate["manifest"]),
                    _jsonb(candidate["candidate_tiles"]),
                    _jsonb(candidate),
                ),
            ).fetchone()
            replayed = inserted is None
            row = inserted or conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND reference_resolution_fingerprint = %s
                  AND packet_kind = 'operational_source_v2'
                """,
                (
                    brand_id,
                    candidate["candidate_packet_fingerprint"],
                    resolution_fingerprint,
                ),
            ).fetchone()
            if row is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The operational source packet could not be persisted."
                )
            stored = _vault_operational_source_packet_record(row)
            expected = {
                "packet": candidate,
                "reference_resolution": resolution,
                "authority": False,
                "authority_scope": "b3s-vault",
                "production_runtime_effect": False,
                "scanner_runtime_effect": False,
            }
            if stored != expected:
                raise EvidenceVaultOperationalAuthorityError(
                    "The source packet identity resolves to different content."
                )
            return stored, replayed

    def review_and_adopt_evidence_vault_operational_source(
        self,
        domain_or_url: str,
        *,
        source_candidate_packet_fingerprint: str,
        decisions: Iterable[Mapping[str, Any]],
        reviewer_id: str,
        workspace_slug: str = "b3s",
        reviewed_at: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Record exact human relation reviews, then CAS-adopt eligible tiles."""

        domain = normalize_domain(domain_or_url)
        source_fingerprint = _require_sha256_text(
            source_candidate_packet_fingerprint,
            field="source_candidate_packet_fingerprint",
        )
        reviewer = _bounded_text(
            reviewer_id,
            field="reviewer_id",
            maximum=200,
        )
        timestamp_was_explicit = reviewed_at is not None or created_at is not None
        reviewed_timestamp = _normalized_event_timestamp(
            reviewed_at or created_at or datetime.now(timezone.utc).isoformat(),
            field="reviewed_at",
        )
        normalized: dict[str, dict[str, str]] = {}
        for raw in decisions:
            if not isinstance(raw, Mapping) or set(raw) != {
                "relation_id",
                "decision",
                "rationale",
            }:
                raise EvidenceVaultOperationalAuthorityError(
                    "Operational review decision fields mismatch."
                )
            relation_id = _require_sha256_text(
                raw.get("relation_id"),
                field="relation_id",
            )
            decision = str(raw.get("decision") or "").strip()
            if decision not in {"accept", "reject"}:
                raise EvidenceVaultOperationalAuthorityError(
                    "Operational relation review decision is invalid."
                )
            rationale = _bounded_text(
                raw.get("rationale"),
                field="rationale",
                maximum=2000,
            )
            if relation_id in normalized:
                raise EvidenceVaultOperationalAuthorityError(
                    "Operational relation review contains duplicates."
                )
            normalized[relation_id] = {
                "decision": decision,
                "rationale": rationale,
            }
        if not domain or not normalized:
            raise EvidenceVaultOperationalAuthorityError(
                "Operational relation review is incomplete."
            )
        self._ensure_migrated()
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
                raise EvidenceVaultOperationalAuthorityError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(brand_id, "evidence-vault-canonical-promotion"),),
            )
            source_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND packet_kind = 'operational_source_v2'
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE
                """,
                (brand_id, source_fingerprint),
            ).fetchone()
            if source_row is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The pending operational source packet does not exist."
                )
            source_packet = _vault_operational_source_packet_record(source_row)[
                "packet"
            ]
            pending_ids = {
                str(basis["relation_id"])
                for candidate in source_packet["candidate_tiles"]
                for basis in candidate.get("basis") or []
                if basis.get("review_status") == "unreviewed"
            }
            if set(normalized) != pending_ids:
                raise EvidenceVaultOperationalAuthorityError(
                    "Human decisions must resolve every pending source relation."
                )
            try:
                validate_exact_relation_source_decisions(
                    source_packet,
                    normalized,
                )
            except EvidenceVaultExactRelationSupplementError as exc:
                raise EvidenceVaultOperationalAuthorityError(
                    "The exact relation decision group is invalid."
                ) from exc
            existing_rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_operational_relation_reviews
                WHERE brand_id = %s AND source_packet_id = %s
                ORDER BY relation_id
                """,
                (brand_id, source_row["id"]),
            ).fetchall()
            existing_by_relation = {
                str(row["relation_id"]): row for row in existing_rows
            }
            if existing_by_relation and set(existing_by_relation) != pending_ids:
                raise EvidenceVaultOperationalAuthorityError(
                    "The source has a partial conflicting review set."
                )
            if existing_rows and not timestamp_was_explicit:
                existing_timestamps = {
                    row["created_at"].astimezone(timezone.utc).isoformat()
                    for row in existing_rows
                }
                if len(existing_timestamps) != 1:
                    raise EvidenceVaultOperationalAuthorityError(
                        "The source review set has inconsistent timestamps."
                    )
                reviewed_timestamp = next(iter(existing_timestamps))
            request_fingerprint = canonical_fingerprint(
                "evidence-vault-operational-relation-review-request-v1",
                {
                    "source_candidate_packet_fingerprint": source_fingerprint,
                    "reviewer_id": reviewer,
                    "reviewed_at": reviewed_timestamp,
                    "decisions": normalized,
                },
            )
            review_events: list[dict[str, Any]] = []
            for relation_id, decision in sorted(normalized.items()):
                event_id = _stable_uuid(
                    brand_id,
                    "evidence-vault-operational-relation-review",
                    source_fingerprint,
                    relation_id,
                    request_fingerprint,
                )
                existing = existing_by_relation.get(relation_id)
                if existing is None:
                    existing = conn.execute(
                        f"""
                        INSERT INTO {_SCHEMA}.evidence_vault_operational_relation_reviews (
                            id, brand_id, source_packet_id,
                            source_packet_fingerprint, relation_id, decision,
                            reviewer_id, rationale, review_request_fingerprint,
                            created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *
                        """,
                        (
                            event_id,
                            brand_id,
                            source_row["id"],
                            source_fingerprint,
                            relation_id,
                            decision["decision"],
                            reviewer,
                            decision["rationale"],
                            request_fingerprint,
                            reviewed_timestamp,
                        ),
                    ).fetchone()
                if (
                    str(existing["decision"]) != decision["decision"]
                    or str(existing["reviewer_id"]) != reviewer
                    or str(existing["rationale"]) != decision["rationale"]
                    or str(existing["review_request_fingerprint"])
                    != request_fingerprint
                    or existing["created_at"].astimezone(timezone.utc).isoformat()
                    != reviewed_timestamp
                ):
                    raise EvidenceVaultOperationalAuthorityError(
                        "The source relation already has another human decision."
                    )
                review_events.append(_vault_operational_relation_review(existing))
            if existing_rows:
                reviewed_existing = conn.execute(
                    f"""
                    SELECT *
                    FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                    WHERE brand_id = %s
                      AND packet_kind = 'operational_reviewed_v2'
                      AND reference_resolution ->> 'review_request_fingerprint' = %s
                      AND reference_resolution ->> 'source_candidate_packet_fingerprint' = %s
                    """,
                    (brand_id, request_fingerprint, source_fingerprint),
                ).fetchone()
                if reviewed_existing is not None:
                    reviewed_record = _vault_operational_source_packet_record(
                        reviewed_existing
                    )
                    operational_existing = conn.execute(
                        f"""
                        SELECT *
                        FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                        WHERE brand_id = %s
                          AND packet_kind = 'operational_v2'
                          AND packet_payload ->> 'source_candidate_packet_fingerprint' = %s
                        """,
                        (
                            brand_id,
                            reviewed_record["packet"][
                                "candidate_packet_fingerprint"
                            ],
                        ),
                    ).fetchone()
                    if operational_existing is not None:
                        operational_record = _vault_operational_packet_record(
                            operational_existing
                        )
                        adoption_existing = conn.execute(
                            f"""
                            SELECT *
                            FROM {_SCHEMA}.evidence_vault_canonical_memory_promotion_events
                            WHERE brand_id = %s
                              AND adoption_kind = 'operational_v2'
                              AND candidate_packet_fingerprint = %s
                            """,
                            (
                                brand_id,
                                operational_record["packet"][
                                    "candidate_packet_fingerprint"
                                ],
                            ),
                        ).fetchone()
                        if (
                            operational_record["packet"]["has_accepted_change"]
                            is False
                            or adoption_existing is not None
                        ):
                            return {
                                "review_events": review_events,
                                "review_request_fingerprint": request_fingerprint,
                                "reviewed_source_packet": reviewed_record,
                                "operational_packet": operational_record,
                                "packet_replayed": True,
                                "adoption": (
                                    _vault_operational_adoption_event_record(
                                        adoption_existing
                                    )
                                    if adoption_existing is not None
                                    else None
                                ),
                                "adoption_replayed": True,
                                "memory": _project_vault_operational_memory(
                                    conn, brand_id
                                ),
                                "score": None,
                            }
            current = _project_vault_operational_memory(conn, brand_id)
            try:
                reviewed_source, operational = build_reviewed_operational_source(
                    source_packet,
                    decisions={
                        event["relation_id"]: {
                            "decision": event["decision"],
                            "decision_event_id": event["decision_event_id"],
                        }
                        for event in review_events
                    },
                    current_operational_memory=current,
                )
            except EvidenceVaultOperationalReviewError as exc:
                raise EvidenceVaultOperationalAuthorityError(
                    "The reviewed operational source is invalid."
                ) from exc
            resolution = {
                "schema_version": "evidence-vault-operational-reviewed-resolution-v1",
                "source_candidate_packet_fingerprint": source_fingerprint,
                "review_request_fingerprint": request_fingerprint,
                "decision_event_ids": sorted(
                    event["decision_event_id"] for event in review_events
                ),
            }
            resolution_fingerprint = canonical_fingerprint(
                "evidence-vault-operational-reviewed-resolution-v1",
                resolution,
            )
            packet_id = _stable_uuid(
                brand_id,
                "evidence-vault-operational-reviewed-packet",
                reviewed_source["candidate_packet_fingerprint"],
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'pending_review', false, false, false,
                    'operational_reviewed_v2', %s
                )
                ON CONFLICT (
                    brand_id, packet_fingerprint,
                    reference_resolution_fingerprint
                ) DO NOTHING
                RETURNING *
                """,
                (
                    packet_id,
                    brand_id,
                    reviewed_source["candidate_packet_fingerprint"],
                    reviewed_source["manifest"]["schema_version"],
                    reviewed_source["manifest"]["brand_identity"],
                    reviewed_source["manifest"][
                        "parent_canonical_memory_version"
                    ],
                    resolution_fingerprint,
                    _jsonb(resolution),
                    _jsonb(reviewed_source["manifest"]),
                    _jsonb(reviewed_source["candidate_tiles"]),
                    _jsonb(reviewed_source),
                ),
            ).fetchone()
            stored_row = inserted or conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND reference_resolution_fingerprint = %s
                  AND packet_kind = 'operational_reviewed_v2'
                """,
                (
                    brand_id,
                    reviewed_source["candidate_packet_fingerprint"],
                    resolution_fingerprint,
                ),
            ).fetchone()
            if stored_row is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The reviewed operational source could not be persisted."
                )
            stored_reviewed = _vault_operational_source_packet_record(stored_row)
            if (
                stored_reviewed["packet"] != reviewed_source
                or stored_reviewed["reference_resolution"] != resolution
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    "The reviewed source identity resolves to different content."
                )

        stored_operational, packet_replayed = (
            self.register_evidence_vault_operational_memory_packet(
                domain,
                operational,
                workspace_slug=workspace_slug,
            )
        )
        packet = stored_operational["packet"]
        adoption = None
        adoption_replayed = True
        if packet["has_accepted_change"] is True:
            policy_fingerprint = request_fingerprint
            actor_id = reviewer
            adoption_request = adoption_request_fingerprint(
                candidate_packet_fingerprint=packet[
                    "candidate_packet_fingerprint"
                ],
                parent_canonical_memory_version=packet[
                    "current_canonical_memory_version"
                ],
                adopted_by="human",
                actor_id=actor_id,
                policy_fingerprint=policy_fingerprint,
            )
            command = OperationalAdoptionCommand(
                candidate_packet_fingerprint=packet[
                    "candidate_packet_fingerprint"
                ],
                parent_canonical_memory_version=packet[
                    "current_canonical_memory_version"
                ],
                adopted_by="human",
                actor_id=actor_id,
                policy_fingerprint=policy_fingerprint,
                created_at=created_at or reviewed_timestamp,
                idempotency_key_hash=canonical_fingerprint(
                    "evidence-vault-operational-human-review-adoption-v1",
                    {
                        "review_request_fingerprint": request_fingerprint,
                        "candidate_packet_fingerprint": packet[
                            "candidate_packet_fingerprint"
                        ],
                    },
                ),
                request_fingerprint=adoption_request,
            )
            adoption, adoption_replayed = (
                self.append_evidence_vault_operational_adoption(
                    domain,
                    command,
                    workspace_slug=workspace_slug,
                )
            )
        return {
            "review_events": review_events,
            "review_request_fingerprint": request_fingerprint,
            "reviewed_source_packet": stored_reviewed,
            "operational_packet": stored_operational,
            "packet_replayed": packet_replayed,
            "adoption": adoption,
            "adoption_replayed": adoption_replayed,
            "memory": self.get_evidence_vault_operational_memory(
                domain,
                workspace_slug=workspace_slug,
            ),
            "score": None,
        }

    def register_evidence_vault_operational_memory_packet(
        self,
        domain_or_url: str,
        packet: dict[str, Any],
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Store one immutable v2 packet in the existing canonical packet store."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceVaultOperationalAuthorityError(
                "The brand domain does not exist in durable history."
            )
        validate_operational_memory_packet(packet)
        if str(packet.get("brand_identity") or "") != domain:
            raise EvidenceVaultOperationalAuthorityError(
                "The operational packet belongs to a different brand."
            )
        resolution = _operational_storage_resolution(packet)
        manifest = {
            "schema_version": str(packet["schema_version"]),
            "packet_kind": "operational_v2",
            "brand_identity": domain,
            "current_canonical_memory_version": packet[
                "current_canonical_memory_version"
            ],
            "proposed_canonical_memory_version": packet[
                "proposed_canonical_memory_version"
            ],
            "accepted_memory_candidate_version": packet[
                "accepted_memory_candidate_version"
            ],
            "candidate_overlay_version": packet["candidate_overlay_version"],
            "authority": False,
            "runtime_effect": False,
        }
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
                raise EvidenceVaultOperationalAuthorityError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(brand_id, "evidence-vault-canonical-promotion"),),
            )
            current_memory = _project_vault_operational_memory(conn, brand_id)
            source_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND packet_kind IN (
                      'canonical_v1',
                      'operational_source_v2',
                      'operational_reviewed_v2'
                  )
                """,
                (brand_id, packet["source_candidate_packet_fingerprint"]),
            ).fetchone()
            source_packet = None
            source_resolution = None
            if source_row is not None:
                if str(source_row["packet_kind"]) == "canonical_v1":
                    source_packet = _vault_canonical_packet_record(source_row)[
                        "packet"
                    ]
                else:
                    source_record = _vault_operational_source_packet_record(
                        source_row
                    )
                    source_packet = source_record["packet"]
                    source_resolution = source_record["reference_resolution"]
            resolved_group_source_record = None
            if (
                source_row is not None
                and str(source_row["packet_kind"]) == "operational_reviewed_v2"
            ):
                _validate_operational_reviewed_source_events(
                    conn,
                    brand_id=brand_id,
                    source_row=source_row,
                    source_packet=source_packet,
                )
                original_source_fingerprint = str(
                    (source_resolution or {}).get(
                        "source_candidate_packet_fingerprint"
                    )
                    or ""
                )
                if original_source_fingerprint:
                    original_source_row = conn.execute(
                        f"""
                        SELECT *
                        FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                        WHERE brand_id = %s
                          AND packet_fingerprint = %s
                          AND packet_kind = 'operational_source_v2'
                          AND reference_resolution ->> 'source_kind' =
                              'exact_relation_supplement'
                        ORDER BY created_at, id
                        LIMIT 1
                        """,
                        (brand_id, original_source_fingerprint),
                    ).fetchone()
                    if original_source_row is not None:
                        resolved_group_source_record = (
                            _vault_operational_source_packet_record(
                                original_source_row
                            )
                        )
            _validate_operational_packet_lineage_for_storage(
                packet,
                current_memory=current_memory,
                source_candidate_packet=source_packet,
                source_reference_resolution=source_resolution,
                resolved_group_source_record=resolved_group_source_record,
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-vault-operational-packet",
                        packet["candidate_packet_fingerprint"],
                    ),
                ),
            )
            packet_id = _stable_uuid(
                brand_id,
                "evidence-vault-operational-memory-packet",
                packet["candidate_packet_fingerprint"],
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint,
                    reference_resolution, manifest, candidate_tiles,
                    authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload,
                    accepted_memory_candidate_version,
                    candidate_overlay_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'pending_review', false, false, false,
                    'operational_v2', %s, %s, %s
                )
                ON CONFLICT (
                    brand_id, packet_fingerprint,
                    reference_resolution_fingerprint
                ) DO NOTHING
                RETURNING *
                """,
                (
                    packet_id,
                    brand_id,
                    packet["candidate_packet_fingerprint"],
                    packet["schema_version"],
                    domain,
                    packet["current_canonical_memory_version"],
                    resolution["reference_resolution_fingerprint"],
                    _jsonb(resolution),
                    _jsonb(manifest),
                    _jsonb(packet["scoring_projection"]["tiles"]),
                    _jsonb(packet),
                    packet["accepted_memory_candidate_version"],
                    packet["candidate_overlay_version"],
                ),
            ).fetchone()
            replayed = inserted is None
            row = inserted or conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND reference_resolution_fingerprint = %s
                  AND packet_kind = 'operational_v2'
                """,
                (
                    brand_id,
                    packet["candidate_packet_fingerprint"],
                    resolution["reference_resolution_fingerprint"],
                ),
            ).fetchone()
            if row is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The operational packet could not be registered."
                )
            stored = _vault_operational_packet_record(row)
            if stored["packet"] != packet:
                raise EvidenceVaultOperationalAuthorityError(
                    "The operational packet fingerprint resolves to different content."
                )
        return stored, replayed

    def get_evidence_vault_operational_memory_packet(
        self,
        domain_or_url: str,
        packet_fingerprint: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]:
        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        fingerprint = str(packet_fingerprint or "").strip().lower()
        if not domain or not _is_sha256(fingerprint):
            raise EvidenceVaultOperationalAuthorityError(
                "The operational packet does not exist."
            )
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT packets.*
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets AS packets
                JOIN {_SCHEMA}.brands ON brands.id = packets.brand_id
                JOIN {_SCHEMA}.workspaces ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                  AND packets.packet_fingerprint = %s
                  AND packets.packet_kind = 'operational_v2'
                """,
                (workspace_slug, domain, fingerprint),
            ).fetchone()
        if row is None:
            raise EvidenceVaultOperationalAuthorityError(
                "The operational packet does not exist."
            )
        return _vault_operational_packet_record(row)

    def append_evidence_vault_operational_adoption(
        self,
        domain_or_url: str,
        command: OperationalAdoptionCommand,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Append one policy or human adoption with parent compare-and-swap."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceVaultOperationalAuthorityError(
                "The brand domain does not exist in durable history."
            )
        expected_request = adoption_request_fingerprint(
            candidate_packet_fingerprint=command.candidate_packet_fingerprint,
            parent_canonical_memory_version=command.parent_canonical_memory_version,
            adopted_by=command.adopted_by,
            actor_id=command.actor_id,
            policy_fingerprint=command.policy_fingerprint,
        )
        if command.request_fingerprint != expected_request:
            raise EvidenceVaultOperationalAuthorityError(
                "The operational adoption request fingerprint is invalid."
            )
        if not _is_sha256(command.idempotency_key_hash):
            raise EvidenceVaultOperationalAuthorityError(
                "The operational adoption idempotency hash is invalid."
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
                raise EvidenceVaultOperationalAuthorityError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(brand_id, "evidence-vault-canonical-promotion"),),
            )
            existing = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_promotion_events
                WHERE brand_id = %s AND idempotency_key_hash = %s
                """,
                (brand_id, command.idempotency_key_hash),
            ).fetchone()
            if existing is not None:
                if str(existing["adoption_kind"]) != "operational_v2":
                    raise EvidenceVaultOperationalAuthorityError(
                        "The idempotency key belongs to a legacy promotion."
                    )
                event = _vault_operational_adoption_event_record(existing)
                if event["request_fingerprint"] != command.request_fingerprint:
                    raise EvidenceVaultOperationalAdoptionConflictError(
                        "The idempotency key was used for another adoption."
                    )
                return event, True

            packet_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND packet_kind = 'operational_v2'
                """,
                (brand_id, command.candidate_packet_fingerprint),
            ).fetchone()
            if packet_row is None:
                raise EvidenceVaultOperationalAuthorityError(
                    "The exact operational packet does not exist."
                )
            stored_packet = _vault_operational_packet_record(packet_row)
            current = _project_vault_operational_memory(conn, brand_id)
            current_version = (
                current["canonical_memory_version"] if current is not None else None
            )
            if command.parent_canonical_memory_version != current_version:
                raise EvidenceVaultOperationalAdoptionConflictError(
                    "The canonical memory changed after the packet was built."
                )
            _validate_operational_adoption_attribution(
                stored_packet["packet"],
                current_memory=current,
                adopted_by=command.adopted_by,
                policy_fingerprint=command.policy_fingerprint,
            )
            previous = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_promotion_events
                WHERE brand_id = %s AND adoption_kind = 'operational_v2'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (brand_id,),
            ).fetchone()
            sequence = int(previous["sequence"]) + 1 if previous is not None else 1
            previous_event_id = str(previous["id"]) if previous is not None else None
            event_id = _stable_uuid(
                brand_id,
                "evidence-vault-operational-adoption",
                command.idempotency_key_hash,
            )
            event = build_operational_adoption_event(
                stored_packet["packet"],
                event_id=str(event_id),
                sequence=sequence,
                previous_event_id=previous_event_id,
                adopted_by=command.adopted_by,
                actor_id=command.actor_id,
                policy_fingerprint=command.policy_fingerprint,
                created_at=command.created_at,
                idempotency_key_hash=command.idempotency_key_hash,
                expected_current_canonical_memory_version=current_version,
            )
            if event["request_fingerprint"] != command.request_fingerprint:
                raise EvidenceVaultOperationalAuthorityError(
                    "The stored adoption request does not match the command."
                )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_memory_promotion_events (
                    id, brand_id, event_type, sequence, previous_event_id,
                    brand_identity, candidate_packet_fingerprint,
                    reference_resolution_fingerprint,
                    promotion_policy_fingerprint,
                    parent_canonical_memory_version,
                    promoted_canonical_memory_version, decision,
                    reviewer_id, reviewed_at, rationale, schema_version,
                    idempotency_key_hash, request_fingerprint, authority,
                    authority_scope, production_runtime_effect,
                    scanner_runtime_effect, adoption_kind, adopted_by,
                    actor_id, event_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, 'promote', %s, %s, %s, %s, %s, %s, true,
                    'b3s-vault', false, false, 'operational_v2', %s, %s, %s
                )
                RETURNING *
                """,
                (
                    event["event_id"],
                    brand_id,
                    event["event_type"],
                    event["sequence"],
                    event["previous_event_id"],
                    event["brand_identity"],
                    event["candidate_packet_fingerprint"],
                    packet_row["reference_resolution_fingerprint"],
                    event["policy_fingerprint"],
                    event["parent_canonical_memory_version"],
                    event["promoted_canonical_memory_version"],
                    event["actor_id"],
                    event["created_at"],
                    f"Operational adoption by {event['adopted_by']} policy.",
                    event["schema_version"],
                    event["idempotency_key_hash"],
                    event["request_fingerprint"],
                    event["adopted_by"],
                    event["actor_id"],
                    _jsonb(event),
                ),
            ).fetchone()
        return _vault_operational_adoption_event_record(inserted), False

    def get_evidence_vault_operational_memory(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                return None
            return _project_vault_operational_memory(conn, brand["id"])

    def get_evidence_vault_active_c7_group_attestation(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Rederive current C7 authority from its exact immutable source.

        The accepted tile points at a reviewed packet.  Its immutable resolution
        points at the original exact-relation source.  Both links are validated
        before the group attestation is built; no caller-provided fingerprint is
        trusted and no unbounded packet scan is required.
        """

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                return None
            brand_id = brand["id"]
            current = _project_vault_operational_memory(conn, brand_id)
            if current is None:
                return None
            accepted_c7 = [
                dict(row)
                for row in current["content"].get("accepted_tiles") or []
                if isinstance(row, Mapping) and row.get("tile_id") == "C7"
            ]
            if len(accepted_c7) != 1:
                return None
            reviewed_fingerprint = str(
                accepted_c7[0].get("source_candidate_packet_fingerprint") or ""
            )
            reviewed_rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND packet_kind = 'operational_reviewed_v2'
                ORDER BY created_at, id
                """,
                (brand_id, reviewed_fingerprint),
            ).fetchall()
            if len(reviewed_rows) != 1:
                return None
            reviewed_record = _vault_operational_source_packet_record(
                reviewed_rows[0]
            )
            reviewed_resolution = reviewed_record["reference_resolution"]
            current_basis = {
                str(row.get("relation_id") or ""): dict(row)
                for row in accepted_c7[0].get("basis") or []
                if isinstance(row, Mapping)
            }
            reviewed_c7 = [
                dict(row)
                for row in reviewed_record["packet"].get("candidate_tiles") or []
                if isinstance(row, Mapping) and row.get("tile_id") == "C7"
            ]
            reviewed_basis = (
                {
                    str(row.get("relation_id") or ""): dict(row)
                    for row in reviewed_c7[0].get("basis") or []
                    if isinstance(row, Mapping)
                }
                if len(reviewed_c7) == 1
                else {}
            )
            accepted_decision_ids = sorted(
                str(row.get("decision_event_id") or "")
                for row in current_basis.values()
            )
            resolution_decision_ids = reviewed_resolution.get(
                "decision_event_ids"
            )
            if (
                reviewed_resolution.get("schema_version")
                != "evidence-vault-operational-reviewed-resolution-v1"
                or set(current_basis) != set(reviewed_basis)
                or any(
                    reviewed_basis[relation_id] != current_row
                    for relation_id, current_row in current_basis.items()
                )
                or not isinstance(resolution_decision_ids, list)
                or resolution_decision_ids
                != sorted(set(str(value) for value in resolution_decision_ids))
                or not set(accepted_decision_ids).issubset(
                    set(resolution_decision_ids)
                )
            ):
                return None
            exact_fingerprint = str(
                reviewed_resolution.get("source_candidate_packet_fingerprint")
                or ""
            )
            exact_rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND packet_kind = 'operational_source_v2'
                  AND reference_resolution ->> 'source_kind' =
                      'exact_relation_supplement'
                ORDER BY created_at, id
                """,
                (brand_id, exact_fingerprint),
            ).fetchall()
            if len(exact_rows) != 1:
                return None
            try:
                accepted_review_ids = [
                    UUID(value) for value in accepted_decision_ids
                ]
            except (TypeError, ValueError, AttributeError):
                return None
            review_rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_operational_relation_reviews
                WHERE brand_id = %s
                  AND source_packet_id = %s
                  AND id = ANY(%s)
                ORDER BY id
                """,
                (brand_id, exact_rows[0]["id"], accepted_review_ids),
            ).fetchall()
            reviews_by_event = {
                str(row["decision_event_id"]): row
                for row in (
                    _vault_operational_relation_review(value)
                    for value in review_rows
                )
            }
            if (
                set(reviews_by_event) != set(accepted_decision_ids)
                or any(
                    reviews_by_event[current_row["decision_event_id"]][
                        "relation_id"
                    ]
                    != relation_id
                    or reviews_by_event[current_row["decision_event_id"]][
                        "decision"
                    ]
                    != "accept"
                    or reviews_by_event[current_row["decision_event_id"]][
                        "source_candidate_packet_fingerprint"
                    ]
                    != exact_fingerprint
                    or reviews_by_event[current_row["decision_event_id"]][
                        "review_request_fingerprint"
                    ]
                    != reviewed_resolution.get("review_request_fingerprint")
                    or reviews_by_event[current_row["decision_event_id"]][
                        "authority"
                    ]
                    is not True
                    or reviews_by_event[current_row["decision_event_id"]][
                        "authority_scope"
                    ]
                    != "relation_review"
                    or reviews_by_event[current_row["decision_event_id"]][
                        "production_runtime_effect"
                    ]
                    is not False
                    or reviews_by_event[current_row["decision_event_id"]][
                        "scanner_runtime_effect"
                    ]
                    is not False
                    for relation_id, current_row in current_basis.items()
                )
            ):
                return None
            try:
                return attest_active_composite_group(
                    current_operational_memory=current,
                    exact_source_record=(
                        _vault_operational_source_packet_record(exact_rows[0])
                    ),
                )
            except EvidenceVaultCompositeGroupLifecycleError:
                return None

    def get_evidence_vault_runtime_ready_c7_group_attestation(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None:
        """Return accepted C7 authority only when current raw lineage is exact.

        Report-derived candidate captures are useful replay evidence but cannot
        satisfy this runtime freshness gate.  Coverage loss therefore fails this
        method closed without mutating accepted memory or its score.
        """

        authority_attestation = self.get_evidence_vault_active_c7_group_attestation(
            domain_or_url,
            workspace_slug=workspace_slug,
        )
        if authority_attestation is None:
            return None
        domain = normalize_domain(domain_or_url)
        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                return None
            source_rows = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                  AND packet_kind = 'operational_source_v2'
                  AND reference_resolution ->> 'source_kind' =
                      'exact_relation_supplement'
                ORDER BY created_at, id
                """,
                (
                    brand["id"],
                    authority_attestation[
                        "exact_source_candidate_packet_fingerprint"
                    ],
                ),
            ).fetchall()
            if len(source_rows) != 1:
                return None
            rows = conn.execute(
                f"""
                SELECT bindings.*, events.event_fingerprint,
                       events.capture_observation_hash
                FROM {_SCHEMA}.evidence_vault_capture_watermark_events AS events
                JOIN {_SCHEMA}.evidence_vault_operational_source_capture_lineage_bindings
                     AS bindings
                  ON bindings.brand_id = events.brand_id
                 AND bindings.watermark_event_id = events.id
                 AND bindings.capture_id = events.capture_id
                 AND bindings.capture_sequence = events.capture_sequence
                WHERE events.brand_id = %s
                  AND bindings.operational_source_packet_id = %s
                  AND bindings.provenance IN (
                      'live_capture_observation',
                      'verified_raw_acquisition_replay'
                  )
                  AND events.capture_sequence = (
                      SELECT max(current_events.capture_sequence)
                      FROM {_SCHEMA}.evidence_vault_capture_watermark_events
                           AS current_events
                      WHERE current_events.brand_id = events.brand_id
                  )
                """,
                (brand["id"], source_rows[0]["id"]),
            ).fetchall()
            if len(rows) != 1:
                return None
            binding = rows[0]
            member_rows = conn.execute(
                f"""
                SELECT members.*, evidence_records.evidence_ref AS durable_ref,
                       evidence_records.source,
                       evidence_records.source_class,
                       evidence_records.evidence_type,
                       evidence_records.url, evidence_records.content,
                       evidence_records.content_raw, evidence_records.confidence,
                       evidence_records.metadata
                FROM {_SCHEMA}.evidence_vault_operational_source_capture_lineage_members
                     AS members
                JOIN {_SCHEMA}.evidence_records
                  ON evidence_records.capture_id = members.capture_id
                 AND evidence_records.id = members.evidence_record_id
                WHERE members.binding_id = %s
                ORDER BY members.relation_id
                """,
                (binding["id"],),
            ).fetchall()
            if len(member_rows) != 2:
                return None
            exact_artifact = dict(
                source_rows[0]["reference_resolution"] or {}
            ).get("artifact")
            if not isinstance(exact_artifact, Mapping):
                return None
            c7_groups = [
                dict(group)
                for group in exact_artifact.get("groups") or []
                if isinstance(group, Mapping) and group.get("tile_id") == "C7"
            ]
            if len(c7_groups) != 1:
                return None
            declared = [
                {
                    "group_id": c7_groups[0]["group_id"],
                    "relation_id": relation["relation_id"],
                    "tile_id": "C7",
                    "decision_rule": "all_of",
                    "evidence_fingerprint": relation["evidence_fingerprint"],
                    "evidence_id": relation["evidence_id"],
                    "source_identity_id": relation["source_identity_id"],
                    "source_ref": relation["ref"],
                    "source_url": relation["url"],
                    "source_class": relation["source_class"],
                    "channel_role": relation["channel_role"],
                    "evidence_quote": relation["literal_quote"],
                    "capture_evidence_index": index,
                }
                for index, relation in enumerate(c7_groups[0].get("relations") or [])
            ]
            try:
                rederived = _rederive_evidence_vault_c7_lineage_members(
                    member_rows,
                    brand_identity=domain,
                    subject_url=str(exact_artifact["subject_url"]),
                    exact_artifact=exact_artifact,
                    declared_bindings=declared,
                )
            except EvidenceVaultOperationalAuthorityError:
                return None
            stored = [
                {
                    "group_id": str(row["composite_group_id"]),
                    "relation_id": str(row["relation_id"]),
                    "evidence_id": str(row["evidence_id"]),
                    "source_identity_id": str(row["source_identity_id"]),
                    "evidence_fingerprint": str(row["evidence_fingerprint"]),
                    "channel_role": str(row["channel_role"]),
                    "evidence_ref": str(row["evidence_ref"]),
                    "source_ref": str(row["source_ref"]),
                    "evidence_quote": str(row["evidence_quote"]),
                }
                for row in member_rows
            ]
            expected = [row["content"] for row in rederived]
            if stored != expected:
                return None
            return authority_attestation

    def get_or_create_evidence_vault_operational_score_evaluation(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any] | None, bool]:
        """Persist or reuse the deterministic v2 score for current memory."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None, False
        with self._connect() as conn:
            brand = conn.execute(
                f"""
                SELECT brands.id
                FROM {_SCHEMA}.brands
                JOIN {_SCHEMA}.workspaces ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s AND brands.canonical_domain = %s
                """,
                (workspace_slug, domain),
            ).fetchone()
            if brand is None:
                return None, False
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key(brand_id, "evidence-vault-canonical-promotion"),),
            )
            memory = _project_vault_operational_memory(conn, brand_id)
            if memory is None:
                return None, False
            existing = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_score_evaluations
                WHERE brand_id = %s
                  AND canonical_memory_version = %s
                  AND evaluation_kind = 'operational_v2'
                """,
                (brand_id, memory["canonical_memory_version"]),
            ).fetchone()
            if existing is not None:
                return _vault_operational_score_record(existing), True

            evaluation = build_operational_score_evaluation(
                memory,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            reusable_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_score_evaluations
                WHERE brand_id = %s
                  AND score_input_fingerprint = %s
                  AND evaluation_kind = 'operational_v2'
                ORDER BY created_at, id
                LIMIT 1
                """,
                (brand_id, evaluation["score_input_fingerprint"]),
            ).fetchone()
            if reusable_row is not None:
                evaluation = build_operational_score_evaluation(
                    memory,
                    created_at=evaluation["created_at"],
                    reusable_evaluation=_vault_operational_score_record(reusable_row),
                )
            evaluation_id = _stable_uuid(
                brand_id,
                "evidence-vault-operational-score",
                evaluation["evaluation_identity"],
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_vault_canonical_score_evaluations (
                    id, brand_id, promotion_event_id,
                    canonical_memory_version, evaluation_identity,
                    score_input_fingerprint, derived_tile_state_fingerprint,
                    rubric_version, tile_contract_registry_fingerprint,
                    reducer_policy_fingerprint,
                    aggregation_policy_fingerprint, schema_version,
                    score, component_breakdown, base_average,
                    magnetism_capped, reused_from_evaluation_identity,
                    authority, authority_scope, production_runtime_effect,
                    scanner_runtime_effect, created_at, evaluation_kind,
                    authority_coverage, evaluation_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, true, 'b3s-vault',
                    false, false, %s, 'operational_v2', %s, %s
                )
                ON CONFLICT (brand_id, canonical_memory_version) DO NOTHING
                RETURNING *
                """,
                (
                    evaluation_id,
                    brand_id,
                    evaluation["adoption_event_id"],
                    evaluation["canonical_memory_version"],
                    evaluation["evaluation_identity"],
                    evaluation["score_input_fingerprint"],
                    evaluation["derived_tile_state_fingerprint"],
                    evaluation["rubric_version"],
                    evaluation["tile_contract_registry_fingerprint"],
                    evaluation["reducer_policy_fingerprint"],
                    evaluation["aggregation_policy_fingerprint"],
                    evaluation["schema_version"],
                    evaluation["score"],
                    _jsonb(evaluation["component_breakdown"]),
                    evaluation["base_average"],
                    evaluation["magnetism_capped"],
                    evaluation["reused_from_evaluation_identity"],
                    evaluation["created_at"],
                    _jsonb(evaluation["authority_coverage"]),
                    _jsonb(evaluation),
                ),
            ).fetchone()
            replayed = inserted is None
            row = inserted or conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_vault_canonical_score_evaluations
                WHERE brand_id = %s
                  AND canonical_memory_version = %s
                  AND evaluation_kind = 'operational_v2'
                """,
                (brand_id, evaluation["canonical_memory_version"]),
            ).fetchone()
            if row is None:
                raise EvidenceVaultOperationalScoringError(
                    "The operational score evaluation could not be persisted."
                )
            stored = _vault_operational_score_record(row)
            if stored != evaluation:
                raise EvidenceVaultOperationalScoringError(
                    "The operational evaluation identity resolves to different content."
                )
            return stored, replayed

    def append_evidence_claim_tile_review(
        self,
        domain_or_url: str,
        command: EvidenceClaimTileReviewCommand,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Append one semantic mapping decision with optimistic locking."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceClaimTileReviewNotFoundError(
                "The brand domain does not exist in durable history."
            )
        _validate_claim_tile_review_command(command)

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
                raise EvidenceClaimTileReviewNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-claim-tile-review-idempotency",
                        command.idempotency_key_hash,
                    ),
                ),
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        CLAIM_TILE_REVIEW_SUBJECT_TYPE,
                        command.subject_id,
                    ),
                ),
            )
            packet_row = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_claim_tile_review_packets
                WHERE brand_id = %s
                  AND packet_fingerprint = %s
                """,
                (brand_id, command.review_packet_fingerprint),
            ).fetchone()
            if packet_row is None:
                raise EvidenceClaimTileReviewPacketNotFoundError(
                    "The exact registered review packet does not exist "
                    "for this brand."
                )
            try:
                packet = _claim_tile_review_packet_record(packet_row)
                packet_candidate = packet_candidate_for_subject(
                    packet,
                    command.subject_id,
                )
            except EvidenceClaimTileReviewPacketError as exc:
                raise EvidenceClaimTileReviewUnavailableError(
                    "The registered claim-to-tile review packet failed "
                    "canonical validation."
                ) from exc
            if packet_candidate is None:
                raise EvidenceClaimTileReviewConflictError(
                    "The mapping subject is not part of the exact "
                    "registered review packet."
                )
            existing = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_claim_tile_review_events
                WHERE brand_id = %s
                  AND idempotency_key_hash = %s
                """,
                (brand_id, command.idempotency_key_hash),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["request_fingerprint"])
                    != command.request_fingerprint
                ):
                    raise EvidenceClaimTileReviewConflictError(
                        "The Idempotency-Key was already used for a different claim-to-tile review.",
                        existing_event_id=str(existing["id"]),
                    )
                latest = conn.execute(
                    f"""
                    SELECT id
                    FROM {_SCHEMA}.evidence_claim_tile_review_events
                    WHERE brand_id = %s
                      AND subject_type = %s
                      AND subject_id = %s
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (
                        brand_id,
                        CLAIM_TILE_REVIEW_SUBJECT_TYPE,
                        command.subject_id,
                    ),
                ).fetchone()
                effective_state = (
                    str(existing["decision"])
                    if latest is not None
                    and latest["id"] == existing["id"]
                    else "superseded"
                )
                return _claim_tile_review_event(
                    existing,
                    effective_state=effective_state,
                ), True

            mapping = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_claim_tile_mappings
                WHERE brand_id = %s
                  AND mapping_id = %s
                """,
                (brand_id, command.subject_id),
            ).fetchone()
            if mapping is None:
                raise EvidenceClaimTileReviewNotFoundError(
                    "The claim-to-tile mapping does not exist in this "
                    "brand's immutable history."
                )
            if not _packet_mapping_matches(
                packet_candidate,
                mapping,
            ):
                raise EvidenceClaimTileReviewConflictError(
                    "The registered packet mapping does not match the "
                    "durable claim-to-tile mapping."
                )

            current = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_claim_tile_review_events
                WHERE brand_id = %s
                  AND subject_type = %s
                  AND subject_id = %s
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (
                    brand_id,
                    CLAIM_TILE_REVIEW_SUBJECT_TYPE,
                    command.subject_id,
                ),
            ).fetchone()
            current_id = (
                str(current["id"]) if current is not None else None
            )
            if command.expected_current_event_id != current_id:
                raise EvidenceClaimTileReviewConflictError(
                    "The claim-to-tile review changed after it was read.",
                    current_event_id=current_id,
                )
            if command.decision == "revoked" and (
                current is None
                or str(current["decision"]) == "revoked"
            ):
                raise EvidenceClaimTileReviewInvalidTransitionError(
                    "Only a current accepted, disputed, or rejected "
                    "claim-to-tile review can be revoked."
                )

            sequence = (
                int(current["sequence"]) + 1
                if current is not None
                else 1
            )
            event_id = _stable_uuid(
                brand_id,
                "evidence-claim-tile-review",
                command.idempotency_key_hash,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_claim_tile_review_events (
                    id, brand_id, subject_type, subject_id, case_id,
                    mapping_id, mapping_series_id, source_evidence_id,
                    claim_variant_id, component_key, tile_id, tile_key,
                    polarity, sequence, decision, supersedes_event_id,
                    schema_version, policy_version, evaluator_version,
                    review_packet_fingerprint,
                    reviewer, actor_id, reason_code, rationale,
                    idempotency_key_hash, request_fingerprint,
                    runtime_effect, authority, automatic_tile_effect,
                    automatic_scoring_effect
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, false, false, false, false
                )
                RETURNING *
                """,
                (
                    event_id,
                    brand_id,
                    CLAIM_TILE_REVIEW_SUBJECT_TYPE,
                    command.subject_id,
                    claim_tile_review_case_id(domain, dict(mapping)),
                    command.subject_id,
                    str(mapping["mapping_series_id"]),
                    str(mapping["source_evidence_id"]),
                    str(mapping["claim_variant_id"]),
                    str(mapping["component_key"]),
                    str(mapping["tile_id"]),
                    str(mapping["tile_key"]),
                    str(mapping["polarity"]),
                    sequence,
                    command.decision,
                    current["id"] if current is not None else None,
                    EVIDENCE_CLAIM_TILE_REVIEW_EVENT_VERSION,
                    EVIDENCE_CLAIM_TILE_REVIEW_POLICY_VERSION,
                    command.evaluator_version,
                    command.review_packet_fingerprint,
                    command.reviewer,
                    command.actor_id,
                    command.reason_code,
                    command.rationale,
                    command.idempotency_key_hash,
                    command.request_fingerprint,
                ),
            ).fetchone()
        return _claim_tile_review_event(inserted), False

    def list_current_evidence_claim_tile_reviews(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> list[dict[str, Any]]:
        """Return the latest semantic decision for every mapping subject."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ON (events.subject_type, events.subject_id)
                       events.*
                FROM {_SCHEMA}.evidence_claim_tile_review_events AS events
                JOIN {_SCHEMA}.evidence_claim_tile_review_packets
                     AS packets
                  ON packets.brand_id = events.brand_id
                 AND packets.packet_fingerprint =
                     events.review_packet_fingerprint
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
        return [_claim_tile_review_event(row) for row in rows]

    def list_evidence_claim_tile_reviews(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        subject_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one page of the durable claim-to-tile review journal."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceClaimTileReviewNotFoundError(
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
                raise EvidenceClaimTileReviewNotFoundError(
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
                    FROM {_SCHEMA}.evidence_claim_tile_review_events
                         AS events
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
                FROM {_SCHEMA}.evidence_claim_tile_review_events
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
                FROM {_SCHEMA}.evidence_claim_tile_review_events
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
            _claim_tile_review_event(
                row,
                effective_state=(
                    str(row["decision"])
                    if int(row["sequence"])
                    == int(row["current_sequence"])
                    else "superseded"
                ),
            )
            for row in rows
        ]
        return {
            "events": events,
            "current": [
                _claim_tile_review_event(row)
                for row in current_rows
            ],
            "total": int(total_row["count"]),
            "limit": limit,
            "offset": offset,
        }

    def register_evidence_scoring_recovery_supplement_packet(
        self,
        domain_or_url: str,
        packet: dict[str, Any],
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Register one immutable mapper-omission supplement fail-closed."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceScoringRecoveryReviewNotFoundError(
                "The brand domain does not exist in durable history."
            )
        validate_recovery_review_supplement_packet(packet)
        manifest = packet["manifest"]
        if manifest["brand_identity"] != domain:
            raise EvidenceScoringRecoveryReviewNotFoundError(
                "The supplement packet belongs to a different brand."
            )

        reports: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = self.list_report_payloads_for_domain(
                domain,
                workspace_slug=workspace_slug,
                limit=500,
                offset=offset,
            )
            reports.extend(batch)
            if len(batch) < 500:
                break
            offset += len(batch)
        if not reports:
            raise EvidenceScoringRecoveryReviewNotFoundError(
                "The brand has no immutable report history."
            )
        adjudications = self.list_current_evidence_memory_adjudications(
            domain,
            workspace_slug=workspace_slug,
        )
        claim_tile_ledger = build_evidence_claim_tile_ledger(
            reports,
            mode="shadow",
        )
        base_preview = build_reviewed_scoring_memory_shadow(
            reports,
            evidence_adjudications=adjudications,
            reviewed_claim_tile_memory=(
                self.get_reviewed_claim_tile_memory_shadow(
                    domain,
                    workspace_slug=workspace_slug,
                )
            ),
            claim_tile_ledger=claim_tile_ledger,
        )
        source_catalog = build_accepted_evidence_passage_catalog(
            reports,
            adjudications=adjudications,
        )
        validate_recovery_review_supplement_against_preview(
            packet,
            base_preview,
            source_catalog,
        )
        registered_packets = [
            row["packet"]
            for row in self.list_evidence_scoring_recovery_supplement_packets(
                domain,
                workspace_slug=workspace_slug,
            )
            if row["packet"]["packet_fingerprint"]
            != packet["packet_fingerprint"]
        ]
        supplement_validation = merge_recovery_review_supplements(
            base_preview["recovery_review_candidates"],
            [*registered_packets, packet],
            brand_domain=domain,
            rubric_version=str(base_preview.get("rubric_version") or ""),
            evidence_identity_state_fingerprint=str(
                source_catalog.get("identity_state_fingerprint") or ""
            ),
            accepted_evidence_ids={
                str(entry.get("evidence_id") or "")
                for entry in source_catalog.get("entries") or []
                if isinstance(entry, dict)
                and str(entry.get("evidence_id") or "")
            },
        )
        supplement_summary = supplement_validation["summary"]
        packet_fingerprint = packet["packet_fingerprint"]
        if (
            packet_fingerprint
            not in supplement_summary["active_packet_fingerprints"]
            or packet_fingerprint
            in supplement_summary["stale_packet_fingerprints"]
            or packet_fingerprint
            in supplement_summary["blocked_packet_fingerprints"]
            or packet_fingerprint
            in supplement_summary[
                "generation_context_drift_fingerprints"
            ]
        ):
            raise EvidenceScoringRecoveryReviewError(
                "supplement packet does not match current durable history"
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
                raise EvidenceScoringRecoveryReviewNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            packet_id = _stable_uuid(
                brand_id,
                "evidence-scoring-recovery-supplement-packet",
                packet["packet_fingerprint"],
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_scoring_recovery_supplement_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, base_candidate_set_fingerprint,
                    evidence_identity_state_fingerprint, rubric_version,
                    manifest, candidates, authority, runtime_effect,
                    automatic_scoring_effect, production_runtime_effect,
                    scanner_runtime_effect
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    false, false, false, false, false
                )
                ON CONFLICT (brand_id, packet_fingerprint) DO NOTHING
                RETURNING *
                """,
                (
                    packet_id,
                    brand_id,
                    packet["packet_fingerprint"],
                    packet["schema_version"],
                    manifest["brand_identity"],
                    manifest["base_candidate_set_fingerprint"],
                    manifest["evidence_identity_state_fingerprint"],
                    manifest["rubric_version"],
                    _jsonb(manifest),
                    _jsonb(packet["candidates"]),
                ),
            ).fetchone()
            replayed = inserted is None
            row = inserted
            if row is None:
                row = conn.execute(
                    f"""
                    SELECT *
                    FROM {_SCHEMA}.evidence_scoring_recovery_supplement_packets
                    WHERE brand_id = %s AND packet_fingerprint = %s
                    """,
                    (brand_id, packet["packet_fingerprint"]),
                ).fetchone()
            if row is None:
                raise EvidenceScoringRecoveryReviewNotFoundError(
                    "The supplement packet could not be registered."
                )
            stored = _scoring_recovery_supplement_packet_record(row)
            if stored["packet"] != packet:
                raise EvidenceScoringRecoveryReviewConflictError(
                    "The supplement fingerprint resolves to different content."
                )
        return stored, replayed

    def list_evidence_scoring_recovery_supplement_packets(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> list[dict[str, Any]]:
        """List immutable supplements; active selection is content-bound."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT packets.*
                FROM {_SCHEMA}.evidence_scoring_recovery_supplement_packets AS packets
                JOIN {_SCHEMA}.brands ON brands.id = packets.brand_id
                JOIN {_SCHEMA}.workspaces ON workspaces.id = brands.workspace_id
                WHERE workspaces.slug = %s
                  AND brands.canonical_domain = %s
                ORDER BY packets.created_at, packets.id
                """,
                (workspace_slug, domain),
            ).fetchall()
        return [
            _scoring_recovery_supplement_packet_record(row)
            for row in rows
        ]

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
        recovery_reviews = (
            self.list_current_evidence_scoring_recovery_reviews(
                domain_or_url,
                workspace_slug=workspace_slug,
            )
        )
        supplemental_packets = [
            row["packet"]
            for row in self.list_evidence_scoring_recovery_supplement_packets(
                domain_or_url,
                workspace_slug=workspace_slug,
            )
        ]
        reviewed_claim_tile_memory = (
            self.get_reviewed_claim_tile_memory_shadow(
                domain_or_url,
                workspace_slug=workspace_slug,
            )
        )
        claim_tile_ledger = build_evidence_claim_tile_ledger(
            reports,
            mode="shadow",
        )
        return build_reviewed_scoring_memory_shadow(
            reports,
            evidence_adjudications=adjudications,
            recovery_review_events=recovery_reviews,
            reviewed_claim_tile_memory=reviewed_claim_tile_memory,
            claim_tile_ledger=claim_tile_ledger,
            supplemental_candidate_packets=supplemental_packets,
            ignore_stale_review_events=True,
            review_events_are_current=True,
        )

    def append_evidence_scoring_recovery_review(
        self,
        domain_or_url: str,
        command: EvidenceScoringRecoveryReviewCommand,
        *,
        workspace_slug: str = "b3s",
    ) -> tuple[dict[str, Any], bool]:
        """Append one semantic recovery decision with optimistic locking."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceScoringRecoveryReviewNotFoundError(
                "The brand domain does not exist in durable history."
            )
        _validate_scoring_recovery_review_command(command)

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
                raise EvidenceScoringRecoveryReviewNotFoundError(
                    "The brand does not exist in durable history."
                )
            brand_id = brand["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        "evidence-scoring-recovery-review-idempotency",
                        command.idempotency_key_hash,
                    ),
                ),
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (
                    _advisory_lock_key(
                        brand_id,
                        SCORING_RECOVERY_REVIEW_SUBJECT_TYPE,
                        command.subject_id,
                    ),
                ),
            )
            existing = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_scoring_recovery_review_events
                WHERE brand_id = %s
                  AND idempotency_key_hash = %s
                """,
                (brand_id, command.idempotency_key_hash),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["request_fingerprint"])
                    != command.request_fingerprint
                ):
                    raise EvidenceScoringRecoveryReviewConflictError(
                        "The Idempotency-Key was already used for a different recovery review.",
                        existing_event_id=str(existing["id"]),
                    )
                latest = conn.execute(
                    f"""
                    SELECT id
                    FROM {_SCHEMA}.evidence_scoring_recovery_review_events
                    WHERE brand_id = %s
                      AND subject_type = %s
                      AND subject_id = %s
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (
                        brand_id,
                        SCORING_RECOVERY_REVIEW_SUBJECT_TYPE,
                        command.subject_id,
                    ),
                ).fetchone()
                effective_state = (
                    str(existing["decision"])
                    if latest is not None
                    and latest["id"] == existing["id"]
                    else "superseded"
                )
                return _scoring_recovery_review_event(
                    existing,
                    effective_state=effective_state,
                ), True

            current = conn.execute(
                f"""
                SELECT *
                FROM {_SCHEMA}.evidence_scoring_recovery_review_events
                WHERE brand_id = %s
                  AND subject_type = %s
                  AND subject_id = %s
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (
                    brand_id,
                    SCORING_RECOVERY_REVIEW_SUBJECT_TYPE,
                    command.subject_id,
                ),
            ).fetchone()
            current_id = (
                str(current["id"]) if current is not None else None
            )
            if command.expected_current_event_id != current_id:
                raise EvidenceScoringRecoveryReviewConflictError(
                    "The scoring recovery review changed after it was read.",
                    current_event_id=current_id,
                )
            if (
                current is not None
                and str(current["case_id"]) != command.case_id
            ):
                raise EvidenceScoringRecoveryReviewConflictError(
                    "The projected scoring recovery case changed unexpectedly.",
                    current_event_id=current_id,
                )
            if command.decision == "revoked" and (
                current is None
                or str(current["decision"]) == "revoked"
            ):
                raise (
                    EvidenceScoringRecoveryReviewInvalidTransitionError(
                        "Only a current accepted, disputed, or rejected recovery review can be revoked."
                    )
                )

            sequence = (
                int(current["sequence"]) + 1
                if current is not None
                else 1
            )
            event_id = _stable_uuid(
                brand_id,
                "evidence-scoring-recovery-review",
                command.idempotency_key_hash,
            )
            inserted = conn.execute(
                f"""
                INSERT INTO {_SCHEMA}.evidence_scoring_recovery_review_events (
                    id, brand_id, subject_type, subject_id, case_id,
                    candidate_fingerprint, sequence, decision,
                    supersedes_event_id, schema_version, policy_version,
                    evaluator_version, reviewer, actor_id, reason_code,
                    rationale, idempotency_key_hash, request_fingerprint,
                    runtime_effect, authority, automatic_scoring_effect
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, false, false, false
                )
                RETURNING *
                """,
                (
                    event_id,
                    brand_id,
                    SCORING_RECOVERY_REVIEW_SUBJECT_TYPE,
                    command.subject_id,
                    command.case_id,
                    command.subject_id,
                    sequence,
                    command.decision,
                    current["id"] if current is not None else None,
                    EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION,
                    EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION,
                    command.evaluator_version,
                    command.reviewer,
                    command.actor_id,
                    command.reason_code,
                    command.rationale,
                    command.idempotency_key_hash,
                    command.request_fingerprint,
                ),
            ).fetchone()
        return _scoring_recovery_review_event(inserted), False

    def list_current_evidence_scoring_recovery_reviews(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> list[dict[str, Any]]:
        """Return the latest semantic decision for each recovery subject."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ON (events.subject_type, events.subject_id)
                       events.*
                FROM {_SCHEMA}.evidence_scoring_recovery_review_events AS events
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
        return [
            _scoring_recovery_review_event(row)
            for row in rows
        ]

    def list_evidence_scoring_recovery_reviews(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        subject_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a reviewable semantic-recovery journal page."""

        self._ensure_migrated()
        domain = normalize_domain(domain_or_url)
        if not domain:
            raise EvidenceScoringRecoveryReviewNotFoundError(
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
                raise EvidenceScoringRecoveryReviewNotFoundError(
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
                    FROM {_SCHEMA}.evidence_scoring_recovery_review_events
                         AS events
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
                FROM {_SCHEMA}.evidence_scoring_recovery_review_events
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
                FROM {_SCHEMA}.evidence_scoring_recovery_review_events
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
            _scoring_recovery_review_event(
                row,
                effective_state=(
                    str(row["decision"])
                    if int(row["sequence"])
                    == int(row["current_sequence"])
                    else "superseded"
                ),
            )
            for row in rows
        ]
        total = int(total_row["count"])
        return {
            "events": events,
            "current": [
                _scoring_recovery_review_event(row)
                for row in current_rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

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
            "evidence_scoring_recovery_review_events",
            "evidence_scoring_recovery_supplement_packets",
            "evidence_claim_tile_review_events",
            "evidence_claim_tile_review_packets",
            "evidence_vault_canonical_memory_packets",
            "evidence_vault_canonical_memory_promotion_events",
            "evidence_vault_canonical_score_evaluations",
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
    def _upsert_brand(
        conn,
        workspace_id: UUID,
        report: HistoricalReport | CaptureObservation,
    ) -> UUID:
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
    def _insert_capture_only_scan_run(
        conn,
        scan_run_id: UUID,
        workspace_id: UUID,
        brand_id: UUID,
        observation: CaptureObservation,
    ) -> None:
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.scan_runs (
                id, workspace_id, brand_id, source_scan_id, source_run_id,
                status, pipeline_version, acquisition_state, requested_at,
                started_at, completed_at, recorded_at, request_payload, metadata
            ) VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s, %s, %s, %s,
                      %s, %s, %s)
            """,
            (
                scan_run_id,
                workspace_id,
                brand_id,
                observation.source_scan_id,
                observation.source_run_id,
                observation.pipeline_version,
                observation.acquisition_state,
                observation.observed_at,
                observation.observed_at,
                observation.recorded_at,
                observation.recorded_at,
                _jsonb(observation.raw_observation),
                _jsonb(
                    {
                        "persisted_as": "capture_only",
                        "observation_hash": observation.observation_hash,
                        "operation_plan": observation.metadata.get("operation_plan"),
                        "operation_plan_fingerprint": observation.metadata.get(
                            "operation_plan_fingerprint"
                        ),
                        "analysis_status": observation.metadata.get(
                            "analysis_status",
                            "pending",
                        ),
                        "observation": observation.metadata,
                    }
                ),
            ),
        )

    @staticmethod
    def _insert_vault_operation_plan(
        conn: Any,
        *,
        workspace_id: UUID,
        brand_id: UUID,
        scan_run_id: UUID,
        observation_hash: str,
        plan: Mapping[str, Any],
        initial_status: str,
    ) -> None:
        validate_vault_scan_plan(plan)
        if initial_status not in {"pending", "not_required"}:
            raise CaptureConflictError(
                "initial Vault operation status must be pending or not_required"
            )
        plan_id = _stable_uuid(
            scan_run_id,
            "evidence-vault-operation-plan",
            plan["operation_plan_fingerprint"],
        )
        conn.execute(
            f"""
            INSERT INTO {_SCHEMA}.evidence_vault_operation_plans (
                id, workspace_id, brand_id, scan_run_id,
                observation_hash, operation_plan_fingerprint,
                canonical_memory_version,
                mode, status, plan_payload,
                authority, authority_scope,
                production_runtime_effect, scanner_runtime_effect
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                false, 'b3s-vault', false, false
            )
            """,
            (
                plan_id,
                workspace_id,
                brand_id,
                scan_run_id,
                observation_hash,
                plan["operation_plan_fingerprint"],
                plan["canonical_memory_version"],
                plan["mode"],
                initial_status,
                _jsonb(dict(plan)),
            ),
        )

    @staticmethod
    def _insert_capture_only(
        conn,
        capture_id: UUID,
        scan_run_id: UUID,
        brand_id: UUID,
        observation: CaptureObservation,
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
                observation.observed_at,
                observation.recorded_at,
                observation.canonical_url,
                observation.capture_hash,
                _jsonb(
                    {
                        **observation.acquisition_summary,
                        "state": observation.acquisition_state,
                    }
                ),
                _jsonb(list(observation.limitations)),
                _jsonb(observation.capture_payload),
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
        report: HistoricalReport | CaptureObservation,
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
    def _insert_attempts(
        conn,
        capture_id: UUID,
        report: HistoricalReport | CaptureObservation,
    ) -> None:
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
    def _insert_artifacts(
        conn,
        capture_id: UUID,
        report: HistoricalReport | CaptureObservation,
    ) -> None:
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


def _validate_composite_group_resolution(
    *,
    resolved_tile_ids: set[str],
    pending_reassessments: Mapping[str, Mapping[str, Any]],
    accepted_by_id: Mapping[str, Mapping[str, Any]],
    reviewed_source_packet: Mapping[str, Any],
    reviewed_source_resolution: Mapping[str, Any] | None,
    exact_source_record: Mapping[str, Any] | None,
    expected_parent: str | None,
) -> None:
    if (
        not isinstance(reviewed_source_resolution, Mapping)
        or reviewed_source_resolution.get("schema_version")
        != "evidence-vault-operational-reviewed-resolution-v1"
        or not isinstance(exact_source_record, Mapping)
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "Pending composite group requires an exact reviewed resolution."
        )
    exact_packet = dict(exact_source_record.get("packet") or {})
    exact_resolution = dict(
        exact_source_record.get("reference_resolution") or {}
    )
    artifact = exact_resolution.get("artifact")
    try:
        validate_candidate_packet(exact_packet)
        if (
            exact_resolution.get("source_kind")
            != "exact_relation_supplement"
            or exact_resolution.get("source_candidate_packet_fingerprint")
            != exact_packet.get("candidate_packet_fingerprint")
            or reviewed_source_resolution.get(
                "source_candidate_packet_fingerprint"
            )
            != exact_packet.get("candidate_packet_fingerprint")
            or not isinstance(artifact, Mapping)
        ):
            raise EvidenceVaultExactRelationSupplementError(
                "exact resolution lineage mismatch"
            )
        validate_exact_relation_supplement_structure(artifact)
    except (
        EvidenceVaultCanonicalCoreError,
        EvidenceVaultExactRelationSupplementError,
    ) as exc:
        raise EvidenceVaultOperationalAuthorityError(
            "The replacement composite group source is invalid."
        ) from exc
    if artifact.get("parent_canonical_memory_version") != expected_parent:
        raise EvidenceVaultOperationalAdoptionConflictError(
            "The replacement composite group has a stale parent."
        )
    reviewed_manifest = reviewed_source_packet.get("manifest") or {}
    if reviewed_manifest.get("parent_canonical_memory_version") != expected_parent:
        raise EvidenceVaultOperationalAdoptionConflictError(
            "The reviewed replacement group has a stale parent."
        )
    groups_by_tile = {
        str(group.get("tile_id") or ""): group
        for group in artifact.get("groups") or []
        if isinstance(group, Mapping) and group.get("decision_rule") == "all_of"
    }
    reviewed_by_tile = {
        str(row.get("tile_id") or ""): row
        for row in reviewed_source_packet.get("candidate_tiles") or []
        if isinstance(row, Mapping)
    }
    for tile_id in resolved_tile_ids:
        pending = pending_reassessments[tile_id]
        group = groups_by_tile.get(tile_id)
        accepted = accepted_by_id.get(tile_id)
        reviewed = reviewed_by_tile.get(tile_id)
        if (
            not isinstance(group, Mapping)
            or not isinstance(accepted, Mapping)
            or not isinstance(reviewed, Mapping)
            or group.get("group_id") == pending.get("prior_group_id")
            or group.get("group_contract", {}).get(
                "member_change_requires_review"
            )
            is not True
            or group.get("group_contract", {}).get(
                "member_decisions_must_match"
            )
            is not True
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "Pending composite group was not replaced by a new all-of group."
            )
        member_relations = [
            row
            for row in group.get("relations") or []
            if isinstance(row, Mapping)
        ]
        member_ids = {str(row["relation_id"]) for row in member_relations}
        superseded_evidence = set(
            pending.get("superseded_member_evidence_fingerprints") or []
        )
        if (
            not superseded_evidence
            or superseded_evidence
            & {
                str(row.get("evidence_fingerprint") or "")
                for row in member_relations
            }
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "Replacement all-of group reuses superseded member evidence."
            )
        accepted_members = [
            row
            for row in accepted.get("basis") or []
            if isinstance(row, Mapping)
            and row.get("claim_id") == group["group_id"]
        ]
        reviewed_members = [
            row
            for row in reviewed.get("basis") or []
            if isinstance(row, Mapping)
            and row.get("claim_id") == group["group_id"]
        ]
        if (
            not member_ids
            or {
                str(row.get("relation_id") or "")
                for row in accepted.get("basis") or []
                if isinstance(row, Mapping)
            }
            != member_ids
            or {str(row.get("relation_id") or "") for row in accepted_members}
            != member_ids
            or {str(row.get("relation_id") or "") for row in reviewed_members}
            != member_ids
            or any(
                row.get("review_status") != "accepted"
                or not str(row.get("decision_event_id") or "").strip()
                for row in accepted_members
            )
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "Replacement all-of group does not have complete reviewed authority."
            )


def _pending_reassessment_rows(
    values: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(values, list):
        raise EvidenceVaultOperationalAuthorityError(
            "Pending reassessments must be an array."
        )
    rows: dict[str, dict[str, Any]] = {}
    registry_ids = {
        str(row["tile_id"]) for row in build_tile_contract_registry()["tiles"]
    }
    for raw in values:
        if not isinstance(raw, Mapping) or set(raw) != {
            "tile_id",
            "lifecycle_state",
            "reopen_policy_fingerprint",
            "prior_group_id",
            "trigger_fingerprint",
            "superseded_member_evidence_fingerprints",
        }:
            raise EvidenceVaultOperationalAuthorityError(
                "Pending reassessment fields mismatch."
            )
        tile_id = str(raw.get("tile_id") or "")
        policy = str(raw.get("reopen_policy_fingerprint") or "")
        prior_group_id = str(raw.get("prior_group_id") or "")
        trigger_fingerprint = str(raw.get("trigger_fingerprint") or "")
        superseded = raw.get("superseded_member_evidence_fingerprints")
        if (
            tile_id not in registry_ids
            or tile_id in rows
            or raw.get("lifecycle_state") != "pending_reassessment"
            or not _is_sha256(policy)
            or not _is_sha256(prior_group_id)
            or not _is_sha256(trigger_fingerprint)
            or not isinstance(superseded, list)
            or not superseded
            or superseded != sorted(set(superseded))
            or any(not _is_sha256(value) for value in superseded)
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "Pending reassessment is invalid."
            )
        rows[tile_id] = {
            "tile_id": tile_id,
            "lifecycle_state": "pending_reassessment",
            "reopen_policy_fingerprint": policy,
            "prior_group_id": prior_group_id,
            "trigger_fingerprint": trigger_fingerprint,
            "superseded_member_evidence_fingerprints": superseded,
        }
    return rows


def _composite_group_reopen_repository_result(
    conn: Any,
    *,
    brand_id: Any,
    source_row: Mapping[str, Any],
    replayed: bool,
) -> dict[str, Any]:
    source_record = _vault_operational_source_packet_record(source_row)
    try:
        validate_composite_group_reopen_source_resolution(
            source_record["reference_resolution"],
            source_candidate_packet=source_record["packet"],
        )
    except EvidenceVaultCompositeGroupLifecycleError as exc:
        raise EvidenceVaultOperationalAuthorityError(
            "The stored composite-group reopen source is invalid."
        ) from exc
    operational_rows = conn.execute(
        f"""
        SELECT *
        FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
        WHERE brand_id = %s
          AND packet_kind = 'operational_v2'
          AND packet_payload ->> 'source_candidate_packet_fingerprint' = %s
        ORDER BY created_at, id
        """,
        (
            brand_id,
            source_record["packet"]["candidate_packet_fingerprint"],
        ),
    ).fetchall()
    if len(operational_rows) != 1:
        raise EvidenceVaultOperationalAuthorityError(
            "The composite-group reopen has no unique operational packet."
        )
    operational_record = _vault_operational_packet_record(operational_rows[0])
    adoption_rows = conn.execute(
        f"""
        SELECT *
        FROM {_SCHEMA}.evidence_vault_canonical_memory_promotion_events
        WHERE brand_id = %s
          AND adoption_kind = 'operational_v2'
          AND candidate_packet_fingerprint = %s
        ORDER BY sequence, id
        """,
        (
            brand_id,
            operational_record["packet"]["candidate_packet_fingerprint"],
        ),
    ).fetchall()
    if len(adoption_rows) != 1:
        raise EvidenceVaultOperationalAuthorityError(
            "The composite-group reopen has no unique adoption event."
        )
    adoption = _vault_operational_adoption_event_record(adoption_rows[0])
    return {
        "reopen_artifact": dict(
            source_record["reference_resolution"]["artifact"]
        ),
        "source_packet": source_record,
        "operational_packet": operational_record,
        "adoption": adoption,
        "memory": _project_vault_operational_memory(conn, brand_id),
        "score": None,
        "source_replayed": replayed,
        "packet_replayed": replayed,
        "adoption_replayed": replayed,
        "authority": True,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


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


def _evidence_vault_capture_watermark_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": str(row["event_schema_version"]),
        "brand_identity": str(row["canonical_domain"]),
        "watermark_event_id": str(row["id"]),
        "capture_id": str(row["capture_id"]),
        "capture_sequence": int(row["capture_sequence"]),
        "previous_event_id": (
            str(row["previous_event_id"])
            if row.get("previous_event_id") is not None
            else None
        ),
        "previous_event_fingerprint": row.get("previous_event_fingerprint"),
        "capture_content_hash": str(row["capture_content_hash"]),
        "capture_observation_hash": str(row["capture_observation_hash"]),
        "append_origin": str(row["append_origin"]),
        "lineage_export_identity": row.get("lineage_export_identity"),
        "lineage_export_fingerprint": row.get("lineage_export_fingerprint"),
        "lineage_export_ordinal": row.get("lineage_export_ordinal"),
        "watermark_fingerprint": str(row["event_fingerprint"]),
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


def _append_evidence_vault_capture_watermark_event(
    conn: Any,
    *,
    brand_id: UUID,
    capture_id: UUID,
    capture_content_hash: str,
    capture_observation_hash: str,
    capture_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one live commit-order event under the existing brand lock."""

    brand = conn.execute(
        f"SELECT workspace_id, canonical_domain FROM {_SCHEMA}.brands WHERE id = %s",
        (brand_id,),
    ).fetchone()
    if brand is None:
        raise CaptureConflictError("capture watermark brand does not exist")
    metadata = dict(capture_metadata or {})
    if metadata.get("acquisition_classification") == (
        "report_derived_candidate_capture"
    ) and metadata.get("provenance") in {
        "report_derived_candidate_capture",
        "historical_report_embedded_acquisition",
    }:
        append_origin = "report_derived_candidate_capture_replay"
        lineage_export_identity = _bounded_text(
            metadata.get("source_report_id")
            or metadata.get("source_artifact_name"),
            field="source_report_id",
            maximum=300,
        )
        lineage_export_fingerprint = _require_sha256_text(
            metadata.get("source_report_canonical_sha256"),
            field="source_report_canonical_sha256",
        )
    else:
        append_origin = "live_capture_observation"
        lineage_export_identity = None
        lineage_export_fingerprint = None
    # Reacquiring a transaction advisory lock is harmless and prevents future
    # call sites from accidentally appending outside the canonical brand lock.
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (_advisory_lock_key(brand["workspace_id"], "brand", brand_id),),
    )
    existing = conn.execute(
        f"""
        SELECT events.*, brands.canonical_domain
        FROM {_SCHEMA}.evidence_vault_capture_watermark_events AS events
        JOIN {_SCHEMA}.brands ON brands.id = events.brand_id
        WHERE events.brand_id = %s AND events.capture_id = %s
        """,
        (brand_id, capture_id),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["capture_content_hash"]) != capture_content_hash
            or str(existing["capture_observation_hash"])
            != capture_observation_hash
            or str(existing["append_origin"]) != append_origin
            or existing.get("lineage_export_identity")
            != lineage_export_identity
            or existing.get("lineage_export_fingerprint")
            != lineage_export_fingerprint
        ):
            raise CaptureConflictError(
                "capture watermark replay differs from its immutable event"
            )
        return _evidence_vault_capture_watermark_record(existing)
    previous = conn.execute(
        f"""
        SELECT *
        FROM {_SCHEMA}.evidence_vault_capture_watermark_events
        WHERE brand_id = %s
        ORDER BY capture_sequence DESC
        LIMIT 1
        """,
        (brand_id,),
    ).fetchone()
    sequence = int(previous["capture_sequence"]) + 1 if previous else 1
    previous_id = previous["id"] if previous else None
    previous_fingerprint = (
        str(previous["event_fingerprint"]) if previous else None
    )
    event_content = {
        "schema_version": "evidence-vault-capture-watermark-event-v1",
        "brand_id": str(brand_id),
        "capture_id": str(capture_id),
        "capture_sequence": sequence,
        "previous_event_id": str(previous_id) if previous_id else None,
        "previous_event_fingerprint": previous_fingerprint,
        "capture_content_hash": _require_sha256_text(
            capture_content_hash,
            field="capture_content_hash",
        ),
        "capture_observation_hash": _require_sha256_text(
            capture_observation_hash,
            field="capture_observation_hash",
        ),
        "append_origin": append_origin,
        "lineage_export_identity": lineage_export_identity,
        "lineage_export_fingerprint": lineage_export_fingerprint,
        "lineage_export_ordinal": (
            sequence
            if append_origin == "report_derived_candidate_capture_replay"
            else None
        ),
    }
    event_fingerprint = canonical_fingerprint(
        "evidence-vault-capture-watermark-event-v1",
        event_content,
    )
    event_id = _stable_uuid(
        brand_id,
        "evidence-vault-capture-watermark-event-v1",
        event_fingerprint,
    )
    row = conn.execute(
        f"""
        INSERT INTO {_SCHEMA}.evidence_vault_capture_watermark_events (
            id, brand_id, capture_id, capture_sequence,
            previous_event_id, previous_event_fingerprint,
            capture_content_hash, capture_observation_hash, append_origin,
            lineage_export_identity, lineage_export_fingerprint,
            lineage_export_ordinal, event_fingerprint, authority,
            production_runtime_effect, scanner_runtime_effect
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, false, false, false
        )
        RETURNING *, %s::text AS canonical_domain
        """,
        (
            event_id,
            brand_id,
            capture_id,
            sequence,
            previous_id,
            previous_fingerprint,
            event_content["capture_content_hash"],
            event_content["capture_observation_hash"],
            event_content["append_origin"],
            event_content["lineage_export_identity"],
            event_content["lineage_export_fingerprint"],
            event_content["lineage_export_ordinal"],
            event_fingerprint,
            str(brand["canonical_domain"]),
        ),
    ).fetchone()
    return _evidence_vault_capture_watermark_record(row)


def _prepare_evidence_vault_lineage_seed_export(
    value: Mapping[str, Any],
    *,
    expected_brand: str,
    workspace_slug: str,
) -> dict[str, Any]:
    """Isolated adapter for ``evidence-vault-lineage-seed-export-v2``.

    This intentionally names and validates only the integration surface consumed
    by PostgreSQL.  The pure artifact module may add self-contained audit fields
    without teaching the repository how to interpret them.
    """

    if not isinstance(value, Mapping):
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export must be an object."
        )
    try:
        artifact = json.loads(canonical_json_bytes(dict(value)).decode("utf-8"))
    except Exception as exc:
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export must be canonical JSON."
        ) from exc
    try:
        from src.services.evidence_vault_lineage_replay import (
            EvidenceVaultLineageReplayError,
            validate_lineage_seed_export_v2,
        )
    except ImportError:
        # Migration/repository workers are independently cherry-pickable.  The
        # strict local checks below remain fail closed until the pure module is
        # integrated, at which point its full self-contained rebuild runs here.
        validate_lineage_seed_export_v2 = None
        EvidenceVaultLineageReplayError = Exception
    if (
        validate_lineage_seed_export_v2 is not None
        and artifact.get("acquisition_classification")
        == "report_derived_candidate_capture"
    ):
        try:
            validate_lineage_seed_export_v2(artifact)
        except EvidenceVaultLineageReplayError as exc:
            raise EvidenceVaultOperationalAuthorityError(
                "The lineage seed export failed exact replay validation."
            ) from exc
    required = {
        "schema_version", "workspace_slug", "seed_id",
        "lineage_export_identity", "lineage_export_ordinal",
        "capture_sequence", "replay_origin_sequence",
        "expected_predecessor_event_fingerprint", "lineage_kind",
        "acquisition_classification", "provenance_policy_versions",
        "brand_identity", "subject_url", "source_artifact_name",
        "source_raw_bytes_sha256", "source_raw_hash_verification",
        "source_report_canonical_sha256",
        "source_relation_artifact_fingerprint",
        "capture_observation_hash", "capture_hash",
        "raw_evidence_pack_canonical_sha256",
        "normalized_evidence_pack_canonical_sha256",
        "normalization_policy", "source_historical_report",
        "source_normalized_evidence_pack",
        "source_exact_relation_supplement", "capture_observation",
        "evidence_bindings", "group_count", "relation_count",
        "adoption_eligible", "authority", "runtime_effect",
        "production_runtime_effect", "scanner_runtime_effect",
        "cutover_authorized", "lineage_export_fingerprint",
        "artifact_fingerprint",
    }
    if (
        set(artifact) != required
        or artifact.get("schema_version")
        != "evidence-vault-lineage-seed-export-v2"
        or artifact.get("workspace_slug") != workspace_slug
        or normalize_domain(str(artifact.get("brand_identity") or ""))
        != expected_brand
        or any(
            artifact.get(field) is not False
            for field in (
                "adoption_eligible",
                "authority",
                "runtime_effect",
                "production_runtime_effect",
                "scanner_runtime_effect",
                "cutover_authorized",
            )
        )
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export contract is invalid."
        )
    exact_artifact = artifact.get("source_exact_relation_supplement")
    try:
        validate_exact_relation_supplement_structure(exact_artifact)
    except Exception as exc:
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export exact source is invalid."
        ) from exc
    if (
        exact_artifact.get("brand_identity") != expected_brand
        or normalize_domain(str(exact_artifact.get("subject_url") or ""))
        != expected_brand
        or artifact.get("source_relation_artifact_fingerprint")
        != exact_artifact.get("artifact_fingerprint")
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export belongs to another exact source."
        )
    try:
        capture = parse_capture_observation(artifact.get("capture_observation"))
    except Exception as exc:
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export capture observation is invalid."
        ) from exc
    if (
        capture.canonical_domain != expected_brand
        or artifact.get("capture_observation_hash") != capture.observation_hash
        or artifact.get("capture_hash") != capture.capture_hash
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export capture identity changed."
        )
    sequence = artifact.get("capture_sequence")
    ordinal = artifact.get("lineage_export_ordinal")
    origin = artifact.get("replay_origin_sequence")
    predecessor = artifact.get("expected_predecessor_event_fingerprint")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or ordinal != sequence
        or not isinstance(origin, int)
        or isinstance(origin, bool)
        or origin < 1
        or origin > sequence
        or (sequence == 1 and predecessor is not None)
        or (
            sequence > 1
            and (
                not isinstance(predecessor, str)
                or re.fullmatch(r"[0-9a-f]{64}", predecessor) is None
            )
        )
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export sequence contract is invalid."
        )
    # The full artifact fingerprint is independently content addressed.  The
    # export-manifest fingerprint is validated by the pure module and retained
    # as an opaque but strictly formed cross-artifact identity here.
    artifact_fingerprint = _require_sha256_text(
        artifact.get("artifact_fingerprint"), field="artifact_fingerprint"
    )
    unsigned = dict(artifact)
    unsigned.pop("artifact_fingerprint", None)
    if artifact_fingerprint != canonical_fingerprint(
        "evidence-vault-lineage-seed-export-v2", unsigned
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export fingerprint is invalid."
        )
    export_identity = _bounded_text(
        artifact.get("lineage_export_identity"),
        field="lineage_export_identity",
        maximum=300,
    )
    export_fingerprint = _require_sha256_text(
        artifact.get("lineage_export_fingerprint"),
        field="lineage_export_fingerprint",
    )
    bindings = artifact.get("evidence_bindings")
    if (
        not isinstance(bindings, list)
        or len(bindings) != artifact.get("relation_count")
        or any(not isinstance(row, Mapping) for row in bindings)
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export relation bindings are incomplete."
        )
    c7_bindings = [
        dict(row) for row in bindings if row.get("tile_id") == "C7"
    ]
    if len(c7_bindings) != 2:
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export must bind exactly two C7 members."
        )
    lineage_kind = str(artifact.get("lineage_kind") or "")
    acquisition_classification = str(
        artifact.get("acquisition_classification") or ""
    )
    observation_provenance = str(capture.metadata.get("provenance") or "")
    observation_classification = str(
        capture.metadata.get("acquisition_classification") or ""
    )
    if acquisition_classification == "report_derived_candidate_capture":
        if (
            observation_classification != "report_derived_candidate_capture"
            or lineage_kind
            not in {
                "report_derived_candidate_capture",
                "historical_report_embedded_acquisition",
            }
            or observation_provenance
            not in {
                "report_derived_candidate_capture",
                "historical_report_embedded_acquisition",
            }
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "Report-derived lineage provenance is inconsistent."
            )
        provenance = "report_derived_candidate_capture"
    elif lineage_kind == "verified_raw_acquisition_replay":
        if observation_provenance != "verified_raw_acquisition_replay":
            raise EvidenceVaultOperationalAuthorityError(
                "Verified raw replay provenance is inconsistent."
            )
        provenance = "verified_raw_acquisition_replay"
    elif lineage_kind == "live_capture_observation":
        if observation_provenance in {
            "report_derived_candidate_capture",
            "verified_raw_acquisition_replay",
        }:
            raise EvidenceVaultOperationalAuthorityError(
                "Live capture lineage provenance is inconsistent."
            )
        provenance = "live_capture_observation"
    else:
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export provenance is unsupported."
        )
    return {
        "artifact": artifact,
        "artifact_fingerprint": artifact_fingerprint,
        "lineage_export_identity": export_identity,
        "lineage_export_fingerprint": export_fingerprint,
        "capture_sequence": sequence,
        "replay_origin_sequence": origin,
        "expected_predecessor_event_fingerprint": predecessor,
        "source_relation_artifact_fingerprint": _require_sha256_text(
            artifact.get("source_relation_artifact_fingerprint"),
            field="source_relation_artifact_fingerprint",
        ),
        "exact_artifact": dict(exact_artifact),
        "capture": capture,
        "evidence_bindings": c7_bindings,
        "provenance": provenance,
    }


def _rederive_evidence_vault_c7_lineage_members(
    evidence_rows: Any,
    *,
    brand_identity: str,
    subject_url: str,
    exact_artifact: Mapping[str, Any],
    declared_bindings: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    c7_groups = [
        dict(group)
        for group in exact_artifact.get("groups") or []
        if isinstance(group, Mapping) and group.get("tile_id") == "C7"
    ]
    if (
        len(c7_groups) != 1
        or c7_groups[0].get("decision_rule") != "all_of"
        or len(c7_groups[0].get("relations") or []) != 2
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The exact source does not contain one two-member C7 group."
        )
    detached_rows = list(evidence_rows)
    evidence = _capture_evidence_rows(detached_rows)
    representatives = canonical_evidence_representatives(
        evidence,
        subject_url=subject_url,
    )
    durable_by_ref = {
        str(row["evidence_ref"]): row for row in detached_rows
    }
    declared_by_relation = {
        str(row.get("relation_id") or ""): dict(row)
        for row in declared_bindings
    }
    relations = [dict(row) for row in c7_groups[0]["relations"]]
    if set(declared_by_relation) != {
        str(row.get("relation_id") or "") for row in relations
    }:
        raise EvidenceVaultOperationalAuthorityError(
            "The lineage seed export relation set changed."
        )
    derived: list[dict[str, Any]] = []
    for relation in relations:
        fingerprint = str(relation.get("evidence_fingerprint") or "")
        representative = representatives.get(fingerprint)
        identity = (
            project_evidence_memory_row_identity(
                representative,
                brand_domain=brand_identity,
            )
            if representative is not None
            else None
        )
        reference = str(relation.get("ref") or "")
        durable = durable_by_ref.get(reference)
        if (
            representative is None
            or identity is None
            or durable is None
            or identity["evidence_id"] != relation.get("evidence_id")
            or identity["document_id"] != relation.get("source_identity_id")
            or representative.get("ref") != reference
            or (representative.get("url") or "") != relation.get("url")
            or (
                representative.get("metadata", {}).get("source_class") or ""
            )
            != relation.get("source_class")
            or relation.get("literal_quote")
            not in str(representative.get("content") or "")
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "The exact C7 member cannot be rederived from this capture."
            )
        declared = declared_by_relation[str(relation["relation_id"])]
        expected_declared = {
            "group_id": c7_groups[0]["group_id"],
            "relation_id": relation["relation_id"],
            "tile_id": "C7",
            "decision_rule": "all_of",
            "evidence_fingerprint": fingerprint,
            "evidence_id": identity["evidence_id"],
            "source_identity_id": identity["document_id"],
            "source_ref": reference,
            "source_url": str(representative.get("url") or ""),
            "source_class": str(
                representative.get("metadata", {}).get("source_class") or ""
            ),
            "channel_role": relation["channel_role"],
            "evidence_quote": relation["literal_quote"],
        }
        if any(declared.get(key) != expected for key, expected in expected_declared.items()):
            raise EvidenceVaultOperationalAuthorityError(
                "The lineage seed export member binding changed."
            )
        capture_index = declared.get("capture_evidence_index")
        if (
            not isinstance(capture_index, int)
            or isinstance(capture_index, bool)
            or capture_index < 0
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "The lineage seed export capture evidence index is invalid."
            )
        derived.append(
            {
                "evidence_record_id": durable["id"],
                "content": {
                    "group_id": str(c7_groups[0]["group_id"]),
                    "relation_id": str(relation["relation_id"]),
                    "evidence_id": str(identity["evidence_id"]),
                    "source_identity_id": str(identity["document_id"]),
                    "evidence_fingerprint": fingerprint,
                    "channel_role": str(relation["channel_role"]),
                    "evidence_ref": reference,
                    "source_ref": reference,
                    "evidence_quote": str(relation["literal_quote"]),
                },
            }
        )
    roles = {row["content"]["channel_role"] for row in derived}
    if roles != {"owned_web", "external_social_profile"}:
        raise EvidenceVaultOperationalAuthorityError(
            "The exact C7 capture lacks independent channel roles."
        )
    return sorted(derived, key=lambda row: row["content"]["relation_id"])


def _vault_canonical_packet_record(row: Any) -> dict[str, Any]:
    manifest = dict(row["manifest"])
    candidate_tiles = list(row["candidate_tiles"])
    packet = {
        "manifest": manifest,
        "candidate_tiles": candidate_tiles,
        "candidate_packet_fingerprint": str(row["packet_fingerprint"]),
    }
    resolution = dict(row["reference_resolution"])
    try:
        validate_candidate_packet(packet)
        expected_resolution = build_reference_resolution(
            packet,
            resolution.get("references") or {},
        )
    except Exception as exc:
        raise EvidenceVaultCanonicalUnavailableError(
            "The registered canonical-memory packet failed validation."
        ) from exc
    if resolution != expected_resolution:
        raise EvidenceVaultCanonicalUnavailableError(
            "The registered canonical-memory reference resolution is invalid."
        )
    if (
        str(row["schema_version"]) != manifest["schema_version"]
        or str(row["brand_identity"]) != manifest["brand_identity"]
        or (
            str(row["parent_canonical_memory_version"])
            if row["parent_canonical_memory_version"] is not None
            else None
        )
        != manifest["parent_canonical_memory_version"]
        or str(row["reference_resolution_fingerprint"])
        != resolution["reference_resolution_fingerprint"]
        or str(row["authority_state"]) != "pending_review"
        or bool(row["authority"])
        or bool(row["production_runtime_effect"])
        or bool(row["scanner_runtime_effect"])
    ):
        raise EvidenceVaultCanonicalUnavailableError(
            "The registered canonical-memory packet metadata is inconsistent."
        )
    created_at = row["created_at"]
    return {
        "packet": packet,
        "reference_resolution": resolution,
        "authority_state": "pending_review",
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at)
        ),
    }


def _vault_canonical_promotion_event(row: Any) -> dict[str, Any]:
    reviewed_at = row["reviewed_at"]
    event = {
        "schema_version": str(row["schema_version"]),
        "event_type": str(row["event_type"]),
        "event_id": str(row["id"]),
        "sequence": int(row["sequence"]),
        "previous_event_id": (
            str(row["previous_event_id"])
            if row["previous_event_id"] is not None
            else None
        ),
        "brand_identity": str(row["brand_identity"]),
        "candidate_packet_fingerprint": str(
            row["candidate_packet_fingerprint"]
        ),
        "reference_resolution_fingerprint": str(
            row["reference_resolution_fingerprint"]
        ),
        "promotion_policy_fingerprint": str(
            row["promotion_policy_fingerprint"]
        ),
        "parent_canonical_memory_version": (
            str(row["parent_canonical_memory_version"])
            if row["parent_canonical_memory_version"] is not None
            else None
        ),
        "promoted_canonical_memory_version": str(
            row["promoted_canonical_memory_version"]
        ),
        "decision": str(row["decision"]),
        "reviewer_id": str(row["reviewer_id"]),
        "reviewed_at": (
            reviewed_at.isoformat()
            if hasattr(reviewed_at, "isoformat")
            else str(reviewed_at)
        ),
        "rationale": str(row["rationale"]),
        "idempotency_key_hash": str(row["idempotency_key_hash"]),
        "request_fingerprint": str(row["request_fingerprint"]),
        "authority": bool(row["authority"]),
        "authority_scope": str(row["authority_scope"]),
        "production_runtime_effect": bool(
            row["production_runtime_effect"]
        ),
        "scanner_runtime_effect": bool(row["scanner_runtime_effect"]),
    }
    validate_promotion_event(event)
    return event


def _vault_canonical_score_evaluation_record(row: Any) -> dict[str, Any]:
    created_at = row["created_at"]
    base_average = row["base_average"]
    evaluation = {
        "schema_version": str(row["schema_version"]),
        "evaluation_identity": str(row["evaluation_identity"]),
        "canonical_memory_version": str(row["canonical_memory_version"]),
        "promotion_event_id": str(row["promotion_event_id"]),
        "score_input_fingerprint": str(row["score_input_fingerprint"]),
        "derived_tile_state_fingerprint": str(
            row["derived_tile_state_fingerprint"]
        ),
        "rubric_version": str(row["rubric_version"]),
        "tile_contract_registry_fingerprint": str(
            row["tile_contract_registry_fingerprint"]
        ),
        "reducer_policy_fingerprint": str(
            row["reducer_policy_fingerprint"]
        ),
        "aggregation_policy_fingerprint": str(
            row["aggregation_policy_fingerprint"]
        ),
        "score": int(row["score"]),
        "component_breakdown": list(row["component_breakdown"]),
        "base_average": float(base_average),
        "magnetism_capped": bool(row["magnetism_capped"]),
        "reused_from_evaluation_identity": (
            str(row["reused_from_evaluation_identity"])
            if row["reused_from_evaluation_identity"] is not None
            else None
        ),
        "created_at": (
            created_at.astimezone(timezone.utc).isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at)
        ),
        "authority": bool(row["authority"]),
        "authority_scope": str(row["authority_scope"]),
        "production_runtime_effect": bool(
            row["production_runtime_effect"]
        ),
        "scanner_runtime_effect": bool(row["scanner_runtime_effect"]),
    }
    try:
        validate_canonical_score_evaluation(evaluation)
    except EvidenceVaultCanonicalScoringError as exc:
        raise EvidenceVaultCanonicalUnavailableError(
            "The stored canonical score evaluation failed validation."
        ) from exc
    return evaluation


def _operational_storage_resolution(packet: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": "evidence-vault-operational-storage-resolution-v1",
        "candidate_packet_fingerprint": str(
            packet["candidate_packet_fingerprint"]
        ),
        "references": {},
    }
    return {
        **payload,
        "reference_resolution_fingerprint": canonical_fingerprint(
            "evidence-vault-operational-storage-resolution-v1",
            payload,
        ),
    }


def _vault_operation_row(
    conn: Any,
    *,
    workspace_slug: str,
    source_scan_id: str,
    for_update: bool,
) -> Any:
    lock_clause = "FOR UPDATE OF operation_plans" if for_update else ""
    return conn.execute(
        f"""
        SELECT operation_plans.*,
               scan_runs.source_scan_id,
               scan_runs.request_payload,
               scan_runs.metadata AS scan_metadata,
               captures.id AS capture_id,
               captures.content_hash AS capture_hash,
               brands.canonical_domain,
               clock_timestamp() AS db_now
        FROM {_SCHEMA}.evidence_vault_operation_plans AS operation_plans
        JOIN {_SCHEMA}.scan_runs ON scan_runs.id = operation_plans.scan_run_id
        JOIN {_SCHEMA}.captures ON captures.scan_run_id = scan_runs.id
        JOIN {_SCHEMA}.brands ON brands.id = operation_plans.brand_id
        JOIN {_SCHEMA}.workspaces ON workspaces.id = operation_plans.workspace_id
        WHERE workspaces.slug = %s
          AND scan_runs.source_scan_id = %s
        {lock_clause}
        """,
        (workspace_slug, source_scan_id),
    ).fetchone()


def _vault_operation_plan_record(row: Mapping[str, Any]) -> dict[str, Any]:
    plan = dict(row["plan_payload"] or {})
    try:
        validate_vault_scan_plan(plan)
    except EvidenceVaultOperationPlanError as exc:
        raise CaptureConflictError("stored operation plan is invalid") from exc
    expected_pairs = {
        "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
        "canonical_memory_version": plan["canonical_memory_version"],
        "mode": plan["mode"],
    }
    if any(row.get(field) != value for field, value in expected_pairs.items()):
        raise CaptureConflictError("stored operation plan columns are inconsistent")
    observation_hash = _require_sha256_text(
        row.get("observation_hash"),
        field="observation_hash",
    )
    result_payload = (
        dict(row["result_payload"])
        if isinstance(row.get("result_payload"), Mapping)
        else None
    )
    result_fingerprint = (
        str(row["result_fingerprint"])
        if row.get("result_fingerprint") is not None
        else None
    )
    if result_payload is not None:
        expected_result = canonical_fingerprint(
            "evidence-vault-operation-result-v1",
            result_payload,
        )
        if result_fingerprint != expected_result:
            raise CaptureConflictError("stored operation result fingerprint mismatch")
    elif result_fingerprint is not None:
        raise CaptureConflictError("stored operation result payload is missing")
    status = str(row["status"])
    lease_expires_at = row.get("lease_expires_at")
    db_now = row.get("db_now") or datetime.now(timezone.utc)
    lease_active = bool(
        status in {"claimed", "running"}
        and lease_expires_at is not None
        and lease_expires_at > db_now
    )
    return {
        "operation_plan_id": str(row["id"]),
        "operation_plan_fingerprint": str(row["operation_plan_fingerprint"]),
        "observation_hash": observation_hash,
        "canonical_memory_version": row.get("canonical_memory_version"),
        "mode": str(row["mode"]),
        "status": status,
        "plan": plan,
        "attempt_count": int(row["attempt_count"]),
        "lease_owner": (
            str(row["lease_owner"]) if row.get("lease_owner") is not None else None
        ),
        "lease_token": (
            str(row["lease_token"]) if row.get("lease_token") is not None else None
        ),
        "lease_generation": int(row["lease_generation"]),
        "lease_expires_at": (
            lease_expires_at.astimezone(timezone.utc).isoformat()
            if lease_expires_at is not None
            else None
        ),
        "lease_active": lease_active,
        "result_fingerprint": result_fingerprint,
        "result_payload": result_payload,
        "candidate_packet_fingerprint": (
            str(row["candidate_packet_fingerprint"])
            if row.get("candidate_packet_fingerprint") is not None
            else None
        ),
        "last_error": str(row.get("last_error") or ""),
        "authority": False,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


def _capture_evidence_rows(rows: Any) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for row in rows:
        metadata = dict(row["metadata"] or {})
        metadata.setdefault("source_class", str(row["source_class"]))
        raw_content = row["content_raw"]
        content = (
            bytes(raw_content).decode("utf-8")
            if raw_content is not None
            else str(row["content"])
        )
        evidence.append(
            {
                "ref": str(row["evidence_ref"]),
                "source": str(row["source"]),
                "evidence_type": str(row["evidence_type"]),
                "url": str(row["url"]),
                "content": content,
                "confidence": str(row["confidence"]),
                "metadata": metadata,
            }
        )
    return evidence


def _validate_vault_operation_result_for_plan(
    conn: Any,
    result: Mapping[str, Any],
    *,
    operation: Mapping[str, Any],
) -> None:
    from src.services.evidence_vault_incremental_executor import (
        EvidenceVaultIncrementalExecutorError,
        derive_vault_tile_shortlists,
        validate_vault_operation_result,
    )

    try:
        validate_vault_operation_result(result)
    except EvidenceVaultIncrementalExecutorError as exc:
        raise CaptureConflictError("operation result contract is invalid") from exc
    plan = dict(operation.get("plan_payload") or {})
    if (
        result.get("operation_plan_fingerprint")
        != operation.get("operation_plan_fingerprint")
        or result.get("observation_hash") != operation.get("observation_hash")
        or result.get("canonical_memory_version")
        != operation.get("canonical_memory_version")
    ):
        raise CaptureConflictError("operation result is not bound to its frozen plan")
    operations = dict(plan.get("operations") or {})
    expected_kind = (
        "candidate_overlay"
        if operations.get("create_candidate_packet") is True
        else "no_delta"
    )
    if plan.get("mode") == "diagnostic_full":
        raise CaptureConflictError(
            "diagnostic_full has no Vault incremental result contract"
        )
    if result.get("output_kind") != expected_kind:
        raise CaptureConflictError(
            "operation result kind does not match its frozen plan"
        )
    if expected_kind == "no_delta":
        delta = dict(plan.get("delta") or {})
        if (
            result.get("delta_fingerprint") != delta.get("delta_fingerprint")
            or result.get("delta_summary") != dict(delta.get("summary") or {})
        ):
            raise CaptureConflictError(
                "no-delta result differs from the frozen delta"
            )
        return

    evidence_rows = conn.execute(
        f"""
        SELECT evidence_ref, source, source_class, evidence_type,
               url, content, content_raw, confidence, metadata
        FROM {_SCHEMA}.evidence_records
        WHERE capture_id = %s
        ORDER BY evidence_ref, id
        """,
        (operation["capture_id"],),
    ).fetchall()
    evidence = _capture_evidence_rows(evidence_rows)
    representatives = canonical_evidence_representatives(
        evidence,
        subject_url=str(plan.get("subject_url") or ""),
    )
    context_fingerprints = sorted(representatives)
    if plan.get("semantic_context", {}).get(
        "evidence_fingerprints"
    ) != context_fingerprints:
        raise CaptureConflictError(
            "frozen semantic context differs from the exact capture"
        )
    planned_fingerprints = sorted(
        operations.get("classify_evidence_fingerprints") or []
    )
    if (
        result.get("selected_evidence_fingerprints") != planned_fingerprints
        or not set(planned_fingerprints).issubset(representatives)
    ):
        raise CaptureConflictError(
            "executor result evidence workset differs from the frozen plan"
        )
    identities: dict[str, dict[str, Any]] = {}
    expected_dispositions: dict[str, str] = {}
    for fingerprint in planned_fingerprints:
        representative = representatives[fingerprint]
        record = EvidenceRecord(
            ref=str(representative.get("ref") or ""),
            source=str(representative.get("source") or "unknown"),
            evidence_type=str(
                representative.get("evidence_type") or "unknown"
            ),
            content=str(representative.get("content") or ""),
            url=str(representative.get("url") or "") or None,
            confidence=str(representative.get("confidence") or "medium"),
            metadata=dict(representative.get("metadata") or {}),
        )
        if not is_evidence_record_labelable(record):
            expected_dispositions[fingerprint] = "ineligible_label_type"
            continue
        identity = project_evidence_memory_row_identity(
            representative,
            brand_domain=str(operation["canonical_domain"]),
        )
        if identity is None:
            expected_dispositions[fingerprint] = "non_material_identity"
            continue
        deterministic_identity_match = str(
            record.metadata.get("identity_match") or ""
        ).strip().lower()
        if deterministic_identity_match == "none":
            expected_dispositions[fingerprint] = (
                "deterministic_identity_mismatch"
            )
            continue
        expected_dispositions[fingerprint] = "semantic_candidate"
        identities[fingerprint] = identity
    if result.get("evidence_work_dispositions") != expected_dispositions:
        raise CaptureConflictError(
            "executor evidence eligibility dispositions are invalid"
        )
    if result.get("selected_evidence_ids") != sorted(
        {row["evidence_id"] for row in identities.values()}
    ) or result.get("selected_document_ids") != sorted(
        {row["document_id"] for row in identities.values()}
    ):
        raise CaptureConflictError(
            "executor result durable evidence identities are invalid"
        )
    shortlists = result.get("tile_shortlists")
    semantic_fingerprints = {
        fingerprint
        for fingerprint, disposition in expected_dispositions.items()
        if disposition == "semantic_candidate"
    }
    if not isinstance(shortlists, Mapping) or set(shortlists) != semantic_fingerprints:
        raise CaptureConflictError("executor result shortlist workset is invalid")
    derived_shortlists, derived_truncations = derive_vault_tile_shortlists(
        [
            {
                "evidence_fingerprint": fingerprint,
                "labels": labels,
            }
            for fingerprint, labels in result["semantic_labels"].items()
        ],
        forced_tile_ids=list(operations.get("reevaluate_tile_ids") or []),
    )
    derived_shortlist_ids = {
        fingerprint: [row["tile_id"] for row in rows]
        for fingerprint, rows in sorted(derived_shortlists.items())
    }
    if (
        shortlists != derived_shortlist_ids
        or result.get("shortlist_truncations") != derived_truncations
    ):
        raise CaptureConflictError(
            "executor shortlists are not derived from its semantic labels"
        )
    relation_pair_count = sum(len(rows) for rows in shortlists.values())
    if (
        plan.get("mode") != "baseline"
        and relation_pair_count > 120
    ):
        raise CaptureConflictError(
            "incremental relation workset exceeds its single-call bound"
        )
    proposal = result.get("relation_proposal")
    if (
        not isinstance(proposal, Mapping)
        or proposal.get("schema_version")
        != "evidence-tile-relation-proposal-v2"
        or not isinstance(proposal.get("relations"), list)
    ):
        raise CaptureConflictError("executor relation proposal is invalid")
    expected_basis: list[dict[str, Any]] = []
    seen_relations: set[tuple[str, str, str]] = set()
    for raw in proposal["relations"]:
        if not isinstance(raw, Mapping) or set(raw) != {
            "evidence_fingerprint",
            "tile_id",
            "polarity",
            "literal_quote",
            "rationale",
        }:
            raise CaptureConflictError("executor relation fields mismatch")
        fingerprint = str(raw.get("evidence_fingerprint") or "")
        identity = identities.get(fingerprint)
        tile_id = str(raw.get("tile_id") or "")
        polarity = str(raw.get("polarity") or "")
        quote = str(raw.get("literal_quote") or "").strip()
        rationale = str(raw.get("rationale") or "").strip()
        if (
            identity is None
            or tile_id not in shortlists.get(fingerprint, [])
            or polarity not in {"supports", "contradicts"}
            or not quote
            or quote not in str(representatives[fingerprint].get("content") or "")
            or not rationale
            or len(rationale) > 1000
        ):
            raise CaptureConflictError(
                "executor relation crosses its durable evidence bounds"
            )
        relation_key = (fingerprint, tile_id, polarity)
        if relation_key in seen_relations:
            raise CaptureConflictError("executor result has duplicate relations")
        seen_relations.add(relation_key)
        relation_id = canonical_fingerprint(
            "evidence-vault-model-relation-v1",
            {
                "operation_plan_fingerprint": operation[
                    "operation_plan_fingerprint"
                ],
                "evidence_id": identity["evidence_id"],
                "source_identity_id": identity["document_id"],
                "tile_id": tile_id,
                "polarity": polarity,
                "literal_quote": quote,
            },
        )
        expected_basis.append(
            {
                "tile_id": tile_id,
                "relation_id": relation_id,
                "evidence_id": identity["evidence_id"],
                "source_identity_id": identity["document_id"],
                "claim_id": None,
                "polarity": polarity,
                "review_status": "unreviewed",
                "decision_event_id": None,
                "absence_test_contract_id": None,
                "coverage_assessment_id": None,
                "coverage_status": None,
                "tested_scope": None,
                "observed_result": None,
            }
        )
    expected_basis.sort(key=lambda row: (row["tile_id"], row["relation_id"]))
    if result.get("basis_relations") != expected_basis:
        raise CaptureConflictError(
            "executor basis relations are not derived from its literal proposals"
        )
    source = result["source_candidate_packet"]
    operational = result["operational_candidate_packet"]
    if (
        source["manifest"]["brand_identity"]
        != str(operation["canonical_domain"])
        or source["manifest"]["brand_identity"]
        != plan.get("brand_identity")
        or source["manifest"]["parent_canonical_memory_version"]
        != operation.get("canonical_memory_version")
        or operational.get("brand_identity") != str(operation["canonical_domain"])
        or operational.get("current_canonical_memory_version")
        != operation.get("canonical_memory_version")
    ):
        raise CaptureConflictError(
            "executor packets are not bound to the operation brand and parent"
        )


def _lease_matches(
    row: Mapping[str, Any],
    *,
    worker_id: str,
    lease_token: str,
    lease_generation: int,
) -> bool:
    return (
        str(row.get("lease_owner") or "") == worker_id
        and str(row.get("lease_token") or "") == lease_token
        and int(row.get("lease_generation") or 0) == lease_generation
    )


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise CaptureConflictError(f"{field} is invalid")
    return text


def _normalized_event_timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise EvidenceVaultOperationalAuthorityError(
            f"{field} must be an aware ISO-8601 timestamp."
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceVaultOperationalAuthorityError(
            f"{field} must be an aware ISO-8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceVaultOperationalAuthorityError(
            f"{field} must be an aware ISO-8601 timestamp."
        )
    return parsed.astimezone(timezone.utc).isoformat()


def _uuid_text(value: Any, *, field: str) -> str:
    try:
        return str(UUID(str(value or "")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CaptureConflictError(f"{field} must be a UUID") from exc


def _positive_int(value: Any, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CaptureConflictError(f"{field} must be a positive integer")
    return value


def _strict_json_object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CaptureConflictError(f"{field} must be an object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        detached = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise CaptureConflictError(f"{field} must contain strict JSON") from exc
    if "\x00" in encoded:
        raise CaptureConflictError(f"{field} contains a NUL character")
    return detached


def _validate_capture_to_report_upgrade(
    conn: Any,
    capture_row: Mapping[str, Any],
    report: HistoricalReport,
) -> dict[str, UUID]:
    """Verify that a full report evaluates the exact persisted capture."""

    raw_observation = capture_row.get("request_payload")
    if not isinstance(raw_observation, dict):
        raise ReportConflictError("capture-only scan has no exact raw observation")
    try:
        observation = parse_capture_observation(raw_observation)
    except Exception as exc:
        raise ReportConflictError(
            "capture-only scan has an invalid raw observation"
        ) from exc
    report_raw = report.report_payload.get("raw")
    source_capture = (
        report_raw.get("source_capture")
        if isinstance(report_raw, Mapping)
        and isinstance(report_raw.get("source_capture"), Mapping)
        else None
    )
    if (
        observation.source_scan_id != report.source_report_id
        or observation.canonical_domain != report.canonical_domain
        or normalize_domain(str(capture_row.get("source_url") or ""))
        != report.canonical_domain
        or str(capture_row.get("content_hash") or "")
        != observation.capture_hash
        or source_capture is None
        or source_capture.get("observation_hash")
        != observation.observation_hash
        or source_capture.get("capture_hash") != observation.capture_hash
    ):
        raise ReportConflictError(
            "full report does not bind the exact capture-only observation"
        )

    stored_rows = conn.execute(
        f"""
        SELECT id, evidence_ref, source, evidence_type, url, content,
               confidence, metadata
        FROM {_SCHEMA}.evidence_records
        WHERE capture_id = %s
        ORDER BY evidence_ref
        """,
        (capture_row["capture_id"],),
    ).fetchall()
    stored_by_ref = {str(row["evidence_ref"]): row for row in stored_rows}
    report_by_ref = {
        str(row.get("ref") or ""): row
        for row in report.evidence_records
        if isinstance(row, dict)
    }
    if (
        len(report_by_ref) != len(report.evidence_records)
        or set(stored_by_ref) != set(report_by_ref)
    ):
        raise ReportConflictError(
            "full report evidence differs from the capture-only observation"
        )
    for evidence_ref, expected in report_by_ref.items():
        stored = stored_by_ref[evidence_ref]
        expected_metadata = (
            expected.get("metadata")
            if isinstance(expected.get("metadata"), dict)
            else {}
        )
        comparisons = {
            "source": str(expected.get("source") or "unknown"),
            "evidence_type": str(expected.get("evidence_type") or "unknown"),
            "url": str(expected.get("url") or ""),
            "content": _pg_text(str(expected.get("content") or "")),
            "confidence": str(expected.get("confidence") or "medium"),
            "metadata": expected_metadata,
        }
        if any(stored[field] != value for field, value in comparisons.items()):
            raise ReportConflictError(
                f"full report evidence differs for ref {evidence_ref}"
            )
    return {
        evidence_ref: stored_by_ref[evidence_ref]["id"]
        for evidence_ref in stored_by_ref
    }


def _vault_operational_source_packet_record(row: Mapping[str, Any]) -> dict[str, Any]:
    if str(row.get("packet_kind") or "") not in {
        "operational_source_v2",
        "operational_reviewed_v2",
    }:
        raise EvidenceVaultOperationalAuthorityError(
            "The stored packet is not an operational source."
        )
    packet = dict(row.get("packet_payload") or {})
    try:
        validate_candidate_packet(packet)
    except EvidenceVaultCanonicalCoreError as exc:
        raise EvidenceVaultOperationalAuthorityError(
            "The stored operational source packet is invalid."
        ) from exc
    resolution = dict(row.get("reference_resolution") or {})
    expected_resolution = canonical_fingerprint(
        (
            "evidence-vault-operational-reviewed-resolution-v1"
            if str(row.get("packet_kind") or "")
            == "operational_reviewed_v2"
            else "evidence-vault-operational-source-resolution-v1"
        ),
        resolution,
    )
    if (
        str(row.get("packet_fingerprint") or "")
        != packet["candidate_packet_fingerprint"]
        or str(row.get("reference_resolution_fingerprint") or "")
        != expected_resolution
        or bool(row.get("authority"))
        or bool(row.get("production_runtime_effect"))
        or bool(row.get("scanner_runtime_effect"))
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The stored operational source metadata is inconsistent."
        )
    return {
        "packet": packet,
        "reference_resolution": resolution,
        "authority": False,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


def _vault_operational_relation_review(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "decision_event_id": str(row["id"]),
        "source_candidate_packet_fingerprint": str(
            row["source_packet_fingerprint"]
        ),
        "relation_id": str(row["relation_id"]),
        "decision": str(row["decision"]),
        "reviewer_id": str(row["reviewer_id"]),
        "rationale": str(row["rationale"]),
        "review_request_fingerprint": str(
            row["review_request_fingerprint"]
        ),
        "authority": bool(row["authority"]),
        "authority_scope": str(row["authority_scope"]),
        "production_runtime_effect": bool(
            row["production_runtime_effect"]
        ),
        "scanner_runtime_effect": bool(row["scanner_runtime_effect"]),
        "created_at": row["created_at"].astimezone(timezone.utc).isoformat(),
    }


def _validate_operational_source_packet_for_storage(
    packet: Mapping[str, Any],
    *,
    operation: Mapping[str, Any],
    current_memory: Mapping[str, Any] | None,
) -> None:
    result = dict(operation.get("result_payload") or {})
    if (
        result.get("output_kind") != "candidate_overlay"
        or result.get("source_candidate_packet_fingerprint")
        != packet.get("candidate_packet_fingerprint")
        or result.get("source_candidate_packet") != packet
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "Operational source packet is not bound to the persisted result."
        )
    expected_parent = (
        current_memory["canonical_memory_version"]
        if current_memory is not None
        else None
    )
    if (
        packet["manifest"]["parent_canonical_memory_version"]
        != expected_parent
        or operation.get("canonical_memory_version") != expected_parent
    ):
        raise EvidenceVaultOperationalAdoptionConflictError(
            "Operational source packet has a stale canonical parent."
        )
    selected_evidence_ids = {
        _require_sha256_text(value, field="selected_evidence_id")
        for value in result.get("selected_evidence_ids") or []
    }
    selected_document_ids = {
        _require_sha256_text(value, field="selected_document_id")
        for value in result.get("selected_document_ids") or []
    }
    shortlisted_tile_ids = {
        str(value or "").strip()
        for value in result.get("shortlisted_tile_ids") or []
        if str(value or "").strip()
    }
    registry_ids = {
        str(row["tile_id"]) for row in build_tile_contract_registry()["tiles"]
    }
    if not shortlisted_tile_ids.issubset(registry_ids):
        raise EvidenceVaultOperationalAuthorityError(
            "Operational result has an unknown tile shortlist."
        )
    expected_relations = {
        str(row.get("relation_id") or ""): dict(row)
        for row in result.get("basis_relations") or []
        if isinstance(row, Mapping)
    }
    if (
        len(expected_relations) != len(result.get("basis_relations") or [])
        or any(not _is_sha256(relation_id) for relation_id in expected_relations)
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "Operational result relation identities are invalid."
        )

    previous_tiles: list[dict[str, Any]] = []
    current_relations: dict[str, dict[str, Any]] = {}
    accepted_by_id = {
        str(row["tile_id"]): row
        for row in (
            current_memory["content"]["accepted_tiles"]
            if current_memory is not None
            else []
        )
    }
    for contract in build_tile_contract_registry()["tiles"]:
        tile_id = str(contract["tile_id"])
        accepted = accepted_by_id.get(tile_id)
        basis = list(accepted.get("basis") or []) if accepted is not None else []
        for relation in basis:
            current_relations[str(relation["relation_id"])] = dict(relation)
        previous_tiles.append(
            build_candidate_tile(
                tile_id=tile_id,
                basis=basis,
                coverage_refs=(
                    accepted.get("coverage_refs") or []
                    if accepted is not None
                    else []
                ),
                unresolved_refs=(
                    accepted.get("unresolved_refs") or []
                    if accepted is not None
                    else []
                ),
            )
        )
    if current_memory is not None:
        try:
            validate_incremental_candidate_tiles(
                previous_candidate_tiles=previous_tiles,
                candidate_tiles=packet["candidate_tiles"],
            )
        except EvidenceVaultCanonicalCoreError as exc:
            raise EvidenceVaultOperationalAuthorityError(
                "Operational source packet does not evolve its exact parent."
            ) from exc

    observed_new_relations: dict[str, dict[str, Any]] = {}
    for candidate in packet["candidate_tiles"]:
        tile_id = str(candidate["tile_id"])
        for basis in candidate.get("basis") or []:
            relation_id = str(basis.get("relation_id") or "")
            previous = current_relations.get(relation_id)
            if previous is not None:
                if dict(basis) != previous:
                    raise EvidenceVaultOperationalAuthorityError(
                        "Operational source mutates an accepted relation."
                    )
                continue
            if (
                tile_id not in shortlisted_tile_ids
                or basis.get("review_status") != "unreviewed"
                or basis.get("decision_event_id") is not None
                or basis.get("polarity") not in {"supports", "contradicts"}
                or basis.get("evidence_id") not in selected_evidence_ids
                or basis.get("source_identity_id") not in selected_document_ids
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    "Model relation crosses the pending-only source boundary."
                )
            observed_new_relations[relation_id] = {
                "tile_id": tile_id,
                **dict(basis),
            }
    if observed_new_relations != expected_relations:
        raise EvidenceVaultOperationalAuthorityError(
            "Operational source relations differ from the persisted result."
        )


def _validate_operational_reviewed_source_events(
    conn: Any,
    *,
    brand_id: Any,
    source_row: Mapping[str, Any],
    source_packet: Mapping[str, Any],
) -> None:
    resolution = dict(source_row.get("reference_resolution") or {})
    original_fingerprint = str(
        resolution.get("source_candidate_packet_fingerprint") or ""
    )
    request_fingerprint = str(
        resolution.get("review_request_fingerprint") or ""
    )
    event_ids = {
        str(value) for value in resolution.get("decision_event_ids") or []
    }
    rows = conn.execute(
        f"""
        SELECT *
        FROM {_SCHEMA}.evidence_vault_operational_relation_reviews
        WHERE brand_id = %s
          AND source_packet_fingerprint = %s
          AND review_request_fingerprint = %s
        """,
        (brand_id, original_fingerprint, request_fingerprint),
    ).fetchall()
    durable_by_id = {str(row["id"]): row for row in rows}
    if set(durable_by_id) != event_ids or not event_ids:
        raise EvidenceVaultOperationalAuthorityError(
            "Reviewed source is not bound to its complete durable event set."
        )
    accepted_event_ids = {
        str(basis.get("decision_event_id") or "")
        for candidate in source_packet.get("candidate_tiles") or []
        for basis in candidate.get("basis") or []
        if isinstance(basis, Mapping)
        and basis.get("review_status") == "accepted"
        and basis.get("decision_event_id") is not None
        and str(basis.get("decision_event_id")) in event_ids
    }
    if any(
        basis.get("review_status") == "unreviewed"
        for candidate in source_packet.get("candidate_tiles") or []
        for basis in candidate.get("basis") or []
        if isinstance(basis, Mapping)
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "Reviewed source still contains unreviewed relations."
        )
    for event_id in accepted_event_ids:
        row = durable_by_id.get(event_id)
        if row is None or str(row["decision"]) != "accept":
            raise EvidenceVaultOperationalAuthorityError(
                "Reviewed accepted relation has no matching human event."
            )
    durable_accepts = {
        str(row["id"])
        for row in rows
        if str(row["decision"]) == "accept"
    }
    if accepted_event_ids != durable_accepts:
        raise EvidenceVaultOperationalAuthorityError(
            "Reviewed source omits or fabricates accepted decisions."
        )


def _validate_operational_adoption_attribution(
    packet: Mapping[str, Any],
    *,
    current_memory: Mapping[str, Any] | None,
    adopted_by: str,
    policy_fingerprint: str,
) -> None:
    accepted_rows = packet["accepted_memory"]["accepted_tiles"]
    current_rows = (
        current_memory["content"]["accepted_tiles"]
        if current_memory is not None
        else []
    )
    current_by_id = {str(row["tile_id"]): row for row in current_rows}
    changed = [
        row
        for row in accepted_rows
        if current_by_id.get(str(row["tile_id"])) != row
    ]
    reopened_values = packet.get("reopened_tile_ids") or []
    reopened_tile_ids = {
        str(value or "").strip() for value in reopened_values
    }
    if reopened_tile_ids:
        accepted_ids = {str(row["tile_id"]) for row in accepted_rows}
        if (
            adopted_by != "policy"
            or changed
            or reopened_tile_ids - set(current_by_id)
            or reopened_tile_ids & accepted_ids
            or packet.get("reopen_policy_fingerprint") != policy_fingerprint
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "Composite-group reopen adoption is not attributable to its policy."
            )
        return
    if adopted_by == "policy":
        if any(row.get("authority_source") != "policy" for row in changed):
            raise EvidenceVaultOperationalAuthorityError(
                "Policy adoption cannot grant authority to human-asserted tiles."
            )
        if changed:
            matrix_fingerprints = {
                str(row.get("authority_matrix_fingerprint") or "")
                for row in changed
            }
            if matrix_fingerprints != {policy_fingerprint}:
                raise EvidenceVaultOperationalAuthorityError(
                    "Adoption policy does not match the tile authority matrix."
                )
        else:
            provisional_policy = canonical_fingerprint(
                "evidence-vault-provisional-baseline-policy-v1",
                {
                    "accepted_subset": "empty",
                    "scanner_states": "candidate_overlay_only",
                    "authority_scope": "b3s-vault",
                },
            )
            if current_memory is not None or policy_fingerprint != provisional_policy:
                raise EvidenceVaultOperationalAuthorityError(
                    "Policy adoption has no attributable accepted change."
                )
    elif adopted_by == "human":
        if not changed:
            raise EvidenceVaultOperationalAuthorityError(
                "Human adoption has no accepted change."
            )
    else:
        raise EvidenceVaultOperationalAuthorityError(
            "Operational adoption actor is invalid."
        )


def _validate_operational_packet_lineage_for_storage(
    packet: Mapping[str, Any],
    *,
    current_memory: Mapping[str, Any] | None,
    source_candidate_packet: Mapping[str, Any] | None,
    source_reference_resolution: Mapping[str, Any] | None = None,
    resolved_group_source_record: Mapping[str, Any] | None = None,
) -> None:
    """Bind one v2 packet to the durable parent and resolved v1 source packet."""

    expected_parent = (
        str(current_memory["canonical_memory_version"])
        if current_memory is not None
        else None
    )
    if packet.get("current_canonical_memory_version") != expected_parent:
        raise EvidenceVaultOperationalAdoptionConflictError(
            "The operational packet parent is not the durable current memory."
        )
    accepted_content = packet.get("accepted_memory")
    if not isinstance(accepted_content, Mapping):
        raise EvidenceVaultOperationalAuthorityError(
            "The operational packet has no accepted-memory content."
        )
    if accepted_content.get("parent_canonical_memory_version") != expected_parent:
        raise EvidenceVaultOperationalAdoptionConflictError(
            "The accepted memory does not declare the durable parent."
        )
    if accepted_content.get("brand_identity") != packet.get("brand_identity"):
        raise EvidenceVaultOperationalAuthorityError(
            "The accepted memory belongs to another brand."
        )

    current_content = (
        current_memory.get("content") if current_memory is not None else None
    )
    if current_content is not None:
        for field in (
            "brand_identity",
            "tile_contract_registry_fingerprint",
            "reducer_policy_fingerprint",
            "aggregation_policy_fingerprint",
        ):
            if accepted_content.get(field) != current_content.get(field):
                raise EvidenceVaultOperationalAuthorityError(
                    f"The operational packet changes canonical policy: {field}."
                )
    current_rows = (
        current_content.get("accepted_tiles")
        if isinstance(current_content, Mapping)
        else []
    )
    accepted_rows = accepted_content.get("accepted_tiles")
    if not isinstance(current_rows, list) or not isinstance(accepted_rows, list):
        raise EvidenceVaultOperationalAuthorityError(
            "Operational accepted tiles must be arrays."
        )
    current_pending_rows = (
        current_content.get("pending_reassessments") or []
        if isinstance(current_content, Mapping)
        else []
    )
    accepted_pending_rows = accepted_content.get("pending_reassessments") or []
    current_pending = _pending_reassessment_rows(current_pending_rows)
    accepted_pending = _pending_reassessment_rows(accepted_pending_rows)
    current_by_id = {
        str(row.get("tile_id") or ""): dict(row)
        for row in current_rows
        if isinstance(row, Mapping)
    }
    accepted_by_id = {
        str(row.get("tile_id") or ""): dict(row)
        for row in accepted_rows
        if isinstance(row, Mapping)
    }
    if len(accepted_by_id) != len(accepted_rows):
        raise EvidenceVaultOperationalAuthorityError(
            "Operational accepted memory has duplicate or invalid tiles."
        )
    reopened_values = packet.get("reopened_tile_ids") or []
    if not isinstance(reopened_values, list):
        raise EvidenceVaultOperationalAuthorityError(
            "Operational reopened tile ids must be an array."
        )
    reopened_tile_ids = {
        str(value or "").strip() for value in reopened_values
    }
    if (
        "" in reopened_tile_ids
        or len(reopened_tile_ids) != len(reopened_values)
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "Operational reopened tile ids are invalid."
        )
    deleted_tile_ids = set(current_by_id) - set(accepted_by_id)
    if deleted_tile_ids != reopened_tile_ids:
        raise EvidenceVaultOperationalAuthorityError(
            "An operational packet cannot silently delete an accepted tile."
        )
    resolved_pending_tile_ids = set(current_pending) - set(accepted_pending)
    if resolved_pending_tile_ids & reopened_tile_ids:
        raise EvidenceVaultOperationalAuthorityError(
            "A tile cannot be reopened and resolved in one packet."
        )
    if any(tile_id not in accepted_by_id for tile_id in resolved_pending_tile_ids):
        raise EvidenceVaultOperationalAuthorityError(
            "Pending reassessment can only resolve into accepted tile authority."
        )
    expected_pending = {
        tile_id: descriptor
        for tile_id, descriptor in current_pending.items()
        if tile_id not in resolved_pending_tile_ids
    }
    if reopened_tile_ids:
        reopen_policy = str(packet.get("reopen_policy_fingerprint") or "")
        for tile_id in reopened_tile_ids:
            descriptor = accepted_pending.get(tile_id)
            if (
                descriptor is None
                or descriptor["reopen_policy_fingerprint"] != reopen_policy
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    "Operational reopen is missing its pending group descriptor."
                )
            expected_pending[tile_id] = descriptor
    if accepted_pending != expected_pending:
        raise EvidenceVaultOperationalAuthorityError(
            "Operational memory cannot drop or fabricate pending reassessment."
        )
    if set(packet.get("pending_reassessment_tile_ids") or []) != set(
        accepted_pending
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "Operational pending reassessment projection is inconsistent."
        )
    changed = {
        tile_id: row
        for tile_id, row in accepted_by_id.items()
        if current_by_id.get(tile_id) != row
    }
    if reopened_tile_ids and changed:
        raise EvidenceVaultOperationalAuthorityError(
            "A composite-group reopen cannot mix authority removal with other changes."
        )
    expected_change = (
        bool(accepted_rows)
        if current_memory is None
        else bool(changed or reopened_tile_ids)
    )
    if packet.get("has_accepted_change") is not expected_change:
        raise EvidenceVaultOperationalAuthorityError(
            "Operational accepted-change flag does not match its parent."
        )
    if not changed and not reopened_tile_ids:
        return
    if source_candidate_packet is None:
        raise EvidenceVaultOperationalAuthorityError(
            "Accepted operational changes require a registered resolved source packet."
        )
    source_manifest = source_candidate_packet.get("manifest")
    source_rows = source_candidate_packet.get("candidate_tiles")
    if not isinstance(source_manifest, Mapping) or not isinstance(source_rows, list):
        raise EvidenceVaultOperationalAuthorityError(
            "The operational source candidate packet is invalid."
        )
    if source_manifest.get("brand_identity") != packet.get("brand_identity"):
        raise EvidenceVaultOperationalAuthorityError(
            "The operational source packet belongs to another brand."
        )
    for field in (
        "tile_contract_registry_fingerprint",
        "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint",
    ):
        if source_manifest.get(field) != accepted_content.get(field):
            raise EvidenceVaultOperationalAuthorityError(
                f"The source packet changes canonical policy: {field}."
            )
    source_by_id = {
        str(row.get("tile_id") or ""): row
        for row in source_rows
        if isinstance(row, Mapping)
    }
    source_fingerprint = str(
        source_candidate_packet.get("candidate_packet_fingerprint") or ""
    )
    if resolved_pending_tile_ids:
        _validate_composite_group_resolution(
            resolved_tile_ids=resolved_pending_tile_ids,
            pending_reassessments=current_pending,
            accepted_by_id=accepted_by_id,
            reviewed_source_packet=source_candidate_packet,
            reviewed_source_resolution=source_reference_resolution,
            exact_source_record=resolved_group_source_record,
            expected_parent=expected_parent,
        )
    if reopened_tile_ids:
        if not isinstance(source_reference_resolution, Mapping):
            raise EvidenceVaultOperationalAuthorityError(
                "A composite-group reopen requires its immutable source resolution."
            )
        try:
            validate_composite_group_reopen_source_resolution(
                source_reference_resolution,
                source_candidate_packet=source_candidate_packet,
            )
        except EvidenceVaultCompositeGroupLifecycleError as exc:
            raise EvidenceVaultOperationalAuthorityError(
                "The composite-group reopen source resolution is invalid."
            ) from exc
        artifact = source_reference_resolution["artifact"]
        pending_descriptor = accepted_pending.get(str(artifact["tile_id"]))
        if (
            reopened_tile_ids != {str(artifact["tile_id"])}
            or packet.get("reopen_policy_fingerprint")
            != artifact["artifact_fingerprint"]
            or not isinstance(pending_descriptor, Mapping)
            or pending_descriptor.get("prior_group_id")
            != artifact["group_id"]
            or pending_descriptor.get("trigger_fingerprint")
            != artifact["trigger"]["trigger_fingerprint"]
            or pending_descriptor.get(
                "superseded_member_evidence_fingerprints"
            )
            != sorted(
                artifact["trigger"][
                    "affected_member_evidence_fingerprints"
                ]
            )
            or source_manifest.get("parent_canonical_memory_version")
            != expected_parent
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "The composite-group reopen policy does not match its source."
            )
        previous_candidates = [
            build_candidate_tile(
                tile_id=str(contract["tile_id"]),
                basis=(current_by_id.get(str(contract["tile_id"])) or {}).get(
                    "basis"
                )
                or [],
                coverage_refs=(
                    current_by_id.get(str(contract["tile_id"])) or {}
                ).get("coverage_refs")
                or [],
                unresolved_refs=(
                    current_by_id.get(str(contract["tile_id"])) or {}
                ).get("unresolved_refs")
                or [],
            )
            for contract in build_tile_contract_registry()["tiles"]
        ]
        try:
            validate_incremental_candidate_tiles(
                previous_candidate_tiles=previous_candidates,
                candidate_tiles=source_rows,
            )
        except EvidenceVaultCanonicalCoreError as exc:
            raise EvidenceVaultOperationalAuthorityError(
                "The composite-group reopen does not evolve its exact parent."
            ) from exc
        tile_id = str(artifact["tile_id"])
        source_tile = source_by_id.get(tile_id)
        if source_tile is None or source_tile.get("delta_kind") != "verified_deprecation":
            raise EvidenceVaultOperationalAuthorityError(
                "The composite-group reopen tile is missing."
            )
        old_member_ids = {
            str(row["relation_id"])
            for row in artifact.get("member_bindings") or []
            if isinstance(row, Mapping)
        }
        source_basis = [
            row for row in source_tile.get("basis") or [] if isinstance(row, Mapping)
        ]
        source_relation_ids = {
            str(row.get("relation_id") or "") for row in source_basis
        }
        invalidations = [
            row
            for row in source_basis
            if row.get("polarity") == "invalidates_candidate"
            and row.get("claim_id") == artifact["group_id"]
            and row.get("decision_event_id") == artifact["reopen_event_id"]
            and row.get("review_status") == "accepted"
        ]
        if (
            not old_member_ids
            or old_member_ids & source_relation_ids
            or len(invalidations) != len(old_member_ids)
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "The composite-group reopen did not supersede the whole group."
            )
        overlay_rows = packet.get("candidate_overlay", {}).get("candidate_tiles")
        overlay = next(
            (
                row
                for row in overlay_rows or []
                if isinstance(row, Mapping) and row.get("tile_id") == tile_id
            ),
            None,
        )
        if (
            not isinstance(overlay, Mapping)
            or overlay.get("authority_state") != "pending"
            or overlay.get("review_state") not in {"required", "in_review"}
            or overlay.get("delta_kind") != "verified_deprecation"
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "The composite-group reopen is not pending reassessment."
            )
    for tile_id, accepted in changed.items():
        source = source_by_id.get(tile_id)
        if source is None:
            raise EvidenceVaultOperationalAuthorityError(
                f"Accepted tile {tile_id} is absent from the source packet."
            )
        expected_pairs = {
            "semantic_state": source.get("candidate_state"),
            "basis": source.get("basis"),
            "coverage_refs": source.get("coverage_refs"),
            "unresolved_refs": source.get("unresolved_refs"),
            "source_delta_kind": source.get("delta_kind"),
            "source_candidate_packet_fingerprint": source_fingerprint,
        }
        if any(accepted.get(field) != value for field, value in expected_pairs.items()):
            raise EvidenceVaultOperationalAuthorityError(
                f"Accepted tile {tile_id} does not match its resolved source candidate."
            )
        authority_source = str(accepted.get("authority_source") or "")
        if authority_source not in {"policy", "human"}:
            raise EvidenceVaultOperationalAuthorityError(
                f"Accepted tile {tile_id} has no authority source."
            )
        if source.get("candidate_state") == "contradiction":
            raise EvidenceVaultOperationalAuthorityError(
                f"Contradictory tile {tile_id} must remain pending."
            )
        if authority_source == "human":
            decision_event_id = str(
                accepted.get("decision_event_id") or ""
            ).strip()
            durable_review_events = {
                str(basis.get("decision_event_id") or "").strip()
                for basis in source.get("basis") or []
                if isinstance(basis, Mapping)
                and basis.get("review_status") == "accepted"
                and str(basis.get("decision_event_id") or "").strip()
            }
            if decision_event_id not in durable_review_events:
                raise EvidenceVaultOperationalAuthorityError(
                    f"Human-accepted tile {tile_id} is not linked to a durable reviewed relation."
                )
            if (
                accepted.get("authority_matrix_fingerprint") is not None
                or accepted.get("authority_decision_fingerprint") is not None
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    f"Human-accepted tile {tile_id} asserts policy authority."
                )
        else:
            matrix = build_initial_authority_profile_matrix()
            decision = evaluate_reviewed_basis_authority(
                candidate_tile=source,
                authority_matrix=matrix,
            )
            validate_authority_decision(
                decision,
                candidate_tile=source,
            )
            if (
                decision.get("eligible") is not True
                or decision.get("decision") != "accept"
                or accepted.get("authority_profile_id")
                != decision.get("authority_profile_id")
                or accepted.get("authority_matrix_fingerprint")
                != decision.get("authority_matrix_fingerprint")
                or accepted.get("authority_decision_fingerprint")
                != decision.get("authority_decision_fingerprint")
                or accepted.get("decision_event_id") is not None
            ):
                raise EvidenceVaultOperationalAuthorityError(
                    f"Policy-accepted tile {tile_id} does not match the current deterministic authority matrix."
                )


def _vault_operational_packet_record(row: Any) -> dict[str, Any]:
    if str(row["packet_kind"]) != "operational_v2":
        raise EvidenceVaultOperationalAuthorityError(
            "The stored packet is not operational memory v2."
        )
    packet = dict(row["packet_payload"])
    validate_operational_memory_packet(packet)
    resolution = dict(row["reference_resolution"])
    expected_resolution = _operational_storage_resolution(packet)
    if resolution != expected_resolution:
        raise EvidenceVaultOperationalAuthorityError(
            "The operational storage resolution is invalid."
        )
    if (
        str(row["packet_fingerprint"])
        != packet["candidate_packet_fingerprint"]
        or str(row["schema_version"]) != packet["schema_version"]
        or str(row["brand_identity"]) != packet["brand_identity"]
        or (
            str(row["parent_canonical_memory_version"])
            if row["parent_canonical_memory_version"] is not None
            else None
        )
        != packet["current_canonical_memory_version"]
        or str(row["accepted_memory_candidate_version"])
        != packet["accepted_memory_candidate_version"]
        or str(row["candidate_overlay_version"])
        != packet["candidate_overlay_version"]
        or str(row["reference_resolution_fingerprint"])
        != resolution["reference_resolution_fingerprint"]
        or list(row["candidate_tiles"])
        != packet["scoring_projection"]["tiles"]
        or str(row["authority_state"]) != "pending_review"
        or bool(row["authority"])
        or bool(row["production_runtime_effect"])
        or bool(row["scanner_runtime_effect"])
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The stored operational packet metadata is inconsistent."
        )
    created_at = row["created_at"]
    return {
        "packet": packet,
        "reference_resolution": resolution,
        "authority": False,
        "runtime_effect": False,
        "created_at": (
            created_at.astimezone(timezone.utc).isoformat()
            if hasattr(created_at, "astimezone")
            else str(created_at)
        ),
    }


def _vault_operational_adoption_event_record(row: Any) -> dict[str, Any]:
    if str(row["adoption_kind"]) != "operational_v2":
        raise EvidenceVaultOperationalAuthorityError(
            "The stored event is not operational adoption v2."
        )
    event = dict(row["event_payload"])
    validate_operational_adoption_event(event)
    if (
        str(row["id"]) != event["event_id"]
        or str(row["event_type"]) != event["event_type"]
        or int(row["sequence"]) != event["sequence"]
        or (
            str(row["previous_event_id"])
            if row["previous_event_id"] is not None
            else None
        )
        != event["previous_event_id"]
        or str(row["brand_identity"]) != event["brand_identity"]
        or str(row["candidate_packet_fingerprint"])
        != event["candidate_packet_fingerprint"]
        or str(row["promotion_policy_fingerprint"])
        != event["policy_fingerprint"]
        or (
            str(row["parent_canonical_memory_version"])
            if row["parent_canonical_memory_version"] is not None
            else None
        )
        != event["parent_canonical_memory_version"]
        or str(row["promoted_canonical_memory_version"])
        != event["promoted_canonical_memory_version"]
        or str(row["adopted_by"]) != event["adopted_by"]
        or str(row["actor_id"]) != event["actor_id"]
        or str(row["idempotency_key_hash"])
        != event["idempotency_key_hash"]
        or str(row["request_fingerprint"]) != event["request_fingerprint"]
        or not bool(row["authority"])
        or str(row["authority_scope"]) != "b3s-vault"
        or bool(row["production_runtime_effect"])
        or bool(row["scanner_runtime_effect"])
    ):
        raise EvidenceVaultOperationalAuthorityError(
            "The stored operational adoption metadata is inconsistent."
        )
    return event


def _vault_operational_score_record(row: Any) -> dict[str, Any]:
    if str(row["evaluation_kind"]) != "operational_v2":
        raise EvidenceVaultOperationalScoringError(
            "The stored evaluation is not operational scoring v2."
        )
    evaluation = dict(row["evaluation_payload"])
    validate_operational_score_evaluation(evaluation)
    if (
        str(row["evaluation_identity"]) != evaluation["evaluation_identity"]
        or str(row["canonical_memory_version"])
        != evaluation["canonical_memory_version"]
        or str(row["promotion_event_id"])
        != evaluation["adoption_event_id"]
        or str(row["score_input_fingerprint"])
        != evaluation["score_input_fingerprint"]
        or int(row["score"]) != evaluation["score"]
        or list(row["component_breakdown"])
        != evaluation["component_breakdown"]
        or dict(row["authority_coverage"])
        != evaluation["authority_coverage"]
        or not bool(row["authority"])
        or str(row["authority_scope"]) != "b3s-vault"
        or bool(row["production_runtime_effect"])
        or bool(row["scanner_runtime_effect"])
    ):
        raise EvidenceVaultOperationalScoringError(
            "The stored operational score metadata is inconsistent."
        )
    return evaluation


def _project_vault_operational_memory(
    conn: Any,
    brand_id: Any,
) -> dict[str, Any] | None:
    rows = conn.execute(
        f"""
        SELECT *
        FROM {_SCHEMA}.evidence_vault_canonical_memory_promotion_events
        WHERE brand_id = %s AND adoption_kind = 'operational_v2'
        ORDER BY sequence
        """,
        (brand_id,),
    ).fetchall()
    current: dict[str, Any] | None = None
    previous_event_id: str | None = None
    for expected_sequence, row in enumerate(rows, start=1):
        event = _vault_operational_adoption_event_record(row)
        if (
            event["sequence"] != expected_sequence
            or event["previous_event_id"] != previous_event_id
        ):
            raise EvidenceVaultOperationalAuthorityError(
                "The operational adoption chain is not contiguous."
            )
        packet_row = conn.execute(
            f"""
            SELECT *
            FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
            WHERE brand_id = %s
              AND packet_fingerprint = %s
              AND packet_kind = 'operational_v2'
            """,
            (brand_id, event["candidate_packet_fingerprint"]),
        ).fetchone()
        if packet_row is None:
            raise EvidenceVaultOperationalAuthorityError(
                "An adopted operational packet is unavailable."
            )
        packet = _vault_operational_packet_record(packet_row)["packet"]
        expected_parent = (
            current["canonical_memory_version"] if current is not None else None
        )
        if packet["current_canonical_memory_version"] != expected_parent:
            raise EvidenceVaultOperationalAuthorityError(
                "The operational packet chain has a parent gap."
            )
        current = project_adopted_operational_memory(packet, event)
        previous_event_id = event["event_id"]
    return current


def _project_vault_canonical_memory(
    conn: Any,
    brand_id: Any,
) -> dict[str, Any] | None:
    rows = conn.execute(
        f"""
        SELECT *
        FROM {_SCHEMA}.evidence_vault_canonical_memory_promotion_events
        WHERE brand_id = %s
          AND adoption_kind = 'human_promotion_v1'
        ORDER BY sequence
        """,
        (brand_id,),
    ).fetchall()
    current: dict[str, Any] | None = None
    previous_event_id: str | None = None
    for expected_sequence, row in enumerate(rows, start=1):
        event = _vault_canonical_promotion_event(row)
        if (
            event["sequence"] != expected_sequence
            or event["previous_event_id"] != previous_event_id
        ):
            raise EvidenceVaultCanonicalUnavailableError(
                "The canonical-memory promotion chain is not contiguous."
            )
        packet_row = conn.execute(
            f"""
            SELECT *
            FROM {_SCHEMA}.evidence_vault_canonical_memory_packets
            WHERE brand_id = %s
              AND packet_fingerprint = %s
              AND packet_kind = 'canonical_v1'
            """,
            (brand_id, event["candidate_packet_fingerprint"]),
        ).fetchone()
        if packet_row is None:
            raise EvidenceVaultCanonicalUnavailableError(
                "A promoted canonical-memory packet is unavailable."
            )
        stored_packet = _vault_canonical_packet_record(packet_row)
        plan = plan_canonical_memory_promotion(
            stored_packet["packet"],
            resolved_references=(
                stored_packet["reference_resolution"]["references"]
            ),
            current_memory=current,
        )
        current = project_promoted_canonical_memory(plan, event)
        previous_event_id = event["event_id"]
    return current


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


def _validate_scoring_recovery_review_command(
    command: EvidenceScoringRecoveryReviewCommand,
) -> None:
    if command.decision not in SCORING_RECOVERY_REVIEW_DECISIONS:
        raise ValueError("unsupported scoring recovery review decision")
    if (
        len(command.subject_id) != 64
        or any(
            character not in "0123456789abcdef"
            for character in command.subject_id
        )
    ):
        raise ValueError(
            "scoring recovery subject_id must be a lowercase SHA-256 digest"
        )
    if (
        not command.case_id
        or len(command.case_id) > 300
        or "\x00" in command.case_id
    ):
        raise ValueError("invalid scoring recovery case_id")
    for field, value, maximum in (
        ("reviewer", command.reviewer, 200),
        ("actor_id", command.actor_id, 200),
        ("evaluator_version", command.evaluator_version, 200),
        ("rationale", command.rationale, 2000),
    ):
        if not value or len(value) > maximum or "\x00" in value:
            raise ValueError(f"invalid scoring recovery review {field}")
    if (
        not command.reason_code
        or len(command.reason_code) > 100
        or not command.reason_code[0].isalnum()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in command.reason_code
        )
    ):
        raise ValueError("invalid scoring recovery review reason_code")
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
            raise ValueError(f"invalid scoring recovery review {field}")


def _validate_claim_tile_review_command(
    command: EvidenceClaimTileReviewCommand,
) -> None:
    if command.decision not in CLAIM_TILE_REVIEW_DECISIONS:
        raise ValueError("unsupported claim-to-tile review decision")
    if (
        len(command.subject_id) != 64
        or any(
            character not in "0123456789abcdef"
            for character in command.subject_id
        )
    ):
        raise ValueError(
            "claim-to-tile subject_id must be a lowercase SHA-256 digest"
        )
    for field, value, maximum in (
        ("reviewer", command.reviewer, 200),
        ("actor_id", command.actor_id, 200),
        ("evaluator_version", command.evaluator_version, 200),
        ("rationale", command.rationale, 2000),
    ):
        if not value or len(value) > maximum or "\x00" in value:
            raise ValueError(f"invalid claim-to-tile review {field}")
    if (
        not command.reason_code
        or len(command.reason_code) > 100
        or not command.reason_code[0].isalnum()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in command.reason_code
        )
    ):
        raise ValueError("invalid claim-to-tile review reason_code")
    for field, value in (
        (
            "review_packet_fingerprint",
            command.review_packet_fingerprint,
        ),
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
            raise ValueError(f"invalid claim-to-tile review {field}")


def _claim_tile_review_event(
    row: Any,
    *,
    effective_state: str | None = None,
) -> dict[str, Any]:
    created_at = row["created_at"]
    timestamp = (
        created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else str(created_at)
    )
    event_id = str(row["id"])
    supersedes_event_id = (
        str(row["supersedes_event_id"])
        if row["supersedes_event_id"] is not None
        else None
    )
    return {
        "id": event_id,
        "event_id": event_id,
        "subject_type": str(row["subject_type"]),
        "subject_id": str(row["subject_id"]),
        "case_id": str(row["case_id"]),
        "mapping_id": str(row["mapping_id"]),
        "mapping_series_id": str(row["mapping_series_id"]),
        "source_evidence_id": str(row["source_evidence_id"]),
        "claim_variant_id": str(row["claim_variant_id"]),
        "component_key": str(row["component_key"]),
        "tile_id": str(row["tile_id"]),
        "tile_key": str(row["tile_key"]),
        "polarity": str(row["polarity"]),
        "sequence": int(row["sequence"]),
        "decision": str(row["decision"]),
        "effective_state": effective_state or str(row["decision"]),
        "supersedes_event_id": supersedes_event_id,
        "previous_event_id": supersedes_event_id,
        "schema_version": str(row["schema_version"]),
        "policy_version": str(row["policy_version"]),
        "evaluator_version": str(row["evaluator_version"]),
        "review_packet_fingerprint": (
            str(row["review_packet_fingerprint"])
            if row["review_packet_fingerprint"] is not None
            else None
        ),
        "reviewer": str(row["reviewer"]),
        "reviewer_id": str(row["reviewer"]),
        "actor_id": str(row["actor_id"]),
        "reason_code": str(row["reason_code"]),
        "rationale": str(row["rationale"]),
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
        "created_at": timestamp,
        "reviewed_at": timestamp,
    }


def _claim_tile_review_packet_record(row: Any) -> dict[str, Any]:
    manifest = (
        dict(row["manifest"])
        if isinstance(row["manifest"], dict)
        else {}
    )
    candidates = [
        dict(candidate)
        for candidate in row["candidates"] or []
        if isinstance(candidate, dict)
    ]
    packet = {
        "manifest": manifest,
        "candidates": candidates,
    }
    validate_evidence_claim_tile_review_packet(packet)
    column_bindings = {
        "packet_fingerprint": "review_packet_fingerprint",
        "candidate_fingerprint": "candidate_fingerprint",
        "schema_version": "schema_version",
        "packet_schema_version": "review_packet_schema_version",
        "candidate_schema_version": "candidate_schema_version",
        "ledger_state_fingerprint": "ledger_state_fingerprint",
        "mapping_series_id": "mapping_series_id",
        "rubric_version": "rubric_version",
    }
    for column, manifest_field in column_bindings.items():
        if str(row[column]) != str(manifest.get(manifest_field) or ""):
            raise EvidenceClaimTileReviewPacketError(
                f"registered review packet {column} mismatch"
            )
    if (
        str(row["packet_kind"]) != "claim_tile"
        or int(row["candidate_count"]) != len(candidates)
        or row["runtime_effect"] is not False
        or row["authority"] is not False
        or row["automatic_tile_effect"] is not False
        or row["automatic_scoring_effect"] is not False
    ):
        raise EvidenceClaimTileReviewPacketError(
            "registered review packet metadata mismatch"
        )
    created_at = row["created_at"]
    timestamp = (
        created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else str(created_at)
    )
    return {
        "id": str(row["id"]),
        "packet_kind": "claim_tile",
        "packet_fingerprint": str(row["packet_fingerprint"]),
        "candidate_fingerprint": str(row["candidate_fingerprint"]),
        "manifest": manifest,
        "candidates": candidates,
        "review_template": build_review_template(
            manifest,
            candidates,
        ),
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
        "created_at": timestamp,
    }


def _packet_mapping_matches(
    candidate: dict[str, Any],
    mapping: Any,
) -> bool:
    candidate_mapping = candidate.get("mapping")
    if not isinstance(candidate_mapping, dict):
        return False
    return all(
        str(candidate_mapping.get(field) or "")
        == str(mapping[field])
        for field in (
            "mapping_id",
            "mapping_series_id",
            "source_evidence_id",
            "claim_variant_id",
            "component_key",
            "tile_id",
            "tile_key",
            "polarity",
        )
    )


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _require_sha256_text(value: Any, *, field: str) -> str:
    text = str(value or "")
    if not _is_sha256(text):
        raise CaptureConflictError(f"{field} must be a lowercase SHA-256")
    return text


def _scoring_recovery_supplement_packet_record(
    row: Any,
) -> dict[str, Any]:
    manifest = dict(row["manifest"])
    candidates = [dict(candidate) for candidate in row["candidates"]]
    packet = {
        "schema_version": str(row["schema_version"]),
        "packet_fingerprint": str(row["packet_fingerprint"]),
        "manifest": manifest,
        "candidates": candidates,
        "authority": False,
        "runtime_effect": False,
        "automatic_scoring_effect": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    validate_recovery_review_supplement_packet(packet)
    expected_columns = {
        "brand_identity": manifest["brand_identity"],
        "base_candidate_set_fingerprint": manifest[
            "base_candidate_set_fingerprint"
        ],
        "evidence_identity_state_fingerprint": manifest[
            "evidence_identity_state_fingerprint"
        ],
        "rubric_version": manifest["rubric_version"],
    }
    if any(
        str(row[column]) != str(value)
        for column, value in expected_columns.items()
    ) or any(
        row[field] is not False
        for field in (
            "authority",
            "runtime_effect",
            "automatic_scoring_effect",
            "production_runtime_effect",
            "scanner_runtime_effect",
        )
    ):
        raise EvidenceScoringRecoveryReviewError(
            "registered supplement packet metadata mismatch"
        )
    created_at = row["created_at"]
    return {
        "id": str(row["id"]),
        "packet": packet,
        "created_at": (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at)
        ),
    }


def _scoring_recovery_review_event(
    row: Any,
    *,
    effective_state: str | None = None,
) -> dict[str, Any]:
    created_at = row["created_at"]
    timestamp = (
        created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else str(created_at)
    )
    event_id = str(row["id"])
    supersedes_event_id = (
        str(row["supersedes_event_id"])
        if row["supersedes_event_id"] is not None
        else None
    )
    return {
        "id": event_id,
        "event_id": event_id,
        "subject_type": str(row["subject_type"]),
        "subject_id": str(row["subject_id"]),
        "case_id": str(row["case_id"]),
        "candidate_fingerprint": str(
            row["candidate_fingerprint"]
        ),
        "sequence": int(row["sequence"]),
        "decision": str(row["decision"]),
        "effective_state": effective_state or str(row["decision"]),
        "supersedes_event_id": supersedes_event_id,
        "previous_event_id": supersedes_event_id,
        "schema_version": str(row["schema_version"]),
        "policy_version": str(row["policy_version"]),
        "evaluator_version": str(row["evaluator_version"]),
        "reviewer": str(row["reviewer"]),
        "reviewer_id": str(row["reviewer"]),
        "actor_id": str(row["actor_id"]),
        "reason_code": str(row["reason_code"]),
        "rationale": str(row["rationale"]),
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "created_at": timestamp,
        "reviewed_at": timestamp,
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
