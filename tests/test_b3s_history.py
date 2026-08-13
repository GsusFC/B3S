from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from importlib import resources
from threading import Barrier
from uuid import uuid4

import pytest

from scripts import import_b3s_reports_postgres
from scripts.import_b3s_reports_postgres import (
    _run_evidence_claim_tile_ledger_backfill,
    _run_evidence_ledger_shadow_backfill,
    dry_run_summary,
)
from src.history.models import ReportConflictError, ReportImportError
from src.history.report_parser import canonical_json_hash, normalize_domain, parse_report
from src.services.evidence_claim_reconciliation import (
    EvidenceClaimReconciliationCommand,
    EvidenceClaimReconciliationConflictError,
)
from src.services.evidence_claim_tile_review import (
    EvidenceClaimTileReviewCommand,
    EvidenceClaimTileReviewPacketNotFoundError,
)
from src.services.evidence_memory_adjudication import (
    EvidenceMemoryAdjudicationCommand,
    EvidenceMemoryAdjudicationConflictError,
)
from src.services.evidence_memory_identity_v2 import (
    build_accepted_evidence_passage_catalog,
    build_evidence_memory_identity_v2,
)
from src.services.evidence_scoring_recovery_review import (
    EvidenceScoringRecoveryReviewCommand,
    EvidenceScoringRecoveryReviewConflictError,
    build_recovery_review_supplement_packet_from_preview,
)
from src.sv9.rubric import COMPONENTS, component_points


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


def test_claim_tile_ledger_backfill_is_a_noop_when_disabled(
    monkeypatch,
) -> None:
    from src.history.repository import PostgresHistoryRepository

    def connect(*_args, **_kwargs):
        raise AssertionError("disabled shadow must not connect")

    repository = PostgresHistoryRepository(
        "postgresql://example.test/b3s",
        connect=connect,
    )
    monkeypatch.setenv(
        "B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE",
        "disabled",
    )

    result = repository.rebuild_evidence_claim_tile_ledgers()

    assert result == {
        "mode": "disabled",
        "runtime_effect": False,
        "authority": False,
        "workspace_slug": "b3s",
        "brands_discovered": 0,
        "rebuilt": 0,
        "failed": 0,
        "failed_domains": [],
    }


def test_claim_tile_ledger_backfill_failure_cannot_fail_a_release() -> None:
    class Repository:
        @staticmethod
        def rebuild_evidence_claim_tile_ledgers(*, workspace_slug):
            raise RuntimeError(f"unavailable:{workspace_slug}")

    result = _run_evidence_claim_tile_ledger_backfill(
        Repository(),
        workspace_slug="b3s",
    )

    assert result == {
        "mode": "shadow",
        "runtime_effect": False,
        "authority": False,
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


def test_claim_tile_ledger_failure_isolated_from_authoritative_import(
    monkeypatch,
) -> None:
    from src.history.repository import PostgresHistoryRepository

    class Connection:
        savepoint_count = 0

        @contextmanager
        def transaction(self):
            self.savepoint_count += 1
            yield

    repository = PostgresHistoryRepository("postgresql://example.test/b3s")
    connection = Connection()
    monkeypatch.setenv(
        "B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE",
        "shadow",
    )

    def fail(*_args):
        raise RuntimeError("experimental mapping failed")

    monkeypatch.setattr(
        repository,
        "_rebuild_evidence_claim_tile_ledger",
        fail,
    )

    rebuilt = repository._rebuild_evidence_claim_tile_ledger_safely(
        connection,
        uuid4(),
        uuid4(),
    )

    assert connection.savepoint_count == 1
    assert rebuilt is False


def test_schema_drop_requires_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("B3S_ALLOW_SCHEMA_DROP", raising=False)

    with pytest.raises(RuntimeError, match="B3S_ALLOW_SCHEMA_DROP=1"):
        _require_schema_drop_opt_in()


def test_claim_reconciliation_migration_is_packaged_and_non_authoritative() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql in _migration_files()]
    sql = (
        resources.files("src.history")
        .joinpath("migrations/005_evidence_claim_reconciliations.sql")
        .read_text(encoding="utf-8")
    )

    assert filenames[4:6] == [
        "005_evidence_claim_reconciliations.sql",
        "006_evidence_claim_tile_ledger.sql",
    ]
    assert "subject_type = 'claim_relation'" in sql
    assert "runtime_effect = false" in sql
    assert "authority = false" in sql
    assert "supersedes_event_id" in sql
    assert "UNIQUE (brand_id, idempotency_key_hash)" in sql


def test_claim_tile_ledger_migration_is_versioned_and_non_authoritative() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql in _migration_files()]
    sql = (
        resources.files("src.history")
        .joinpath("migrations/006_evidence_claim_tile_ledger.sql")
        .read_text(encoding="utf-8")
    )

    assert filenames[5] == "006_evidence_claim_tile_ledger.sql"
    assert "evidence_claim_tile_mapping_series" in sql
    assert "evidence_claim_tile_mapping_observations" in sql
    assert "runtime_effect = false" in sql
    assert "authority = false" in sql
    assert "mapping_series_id" in sql


def test_scoring_recovery_review_migration_is_append_only_and_safe() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql in _migration_files()]
    sql = (
        resources.files("src.history")
        .joinpath(
            "migrations/007_evidence_scoring_recovery_reviews.sql"
        )
        .read_text(encoding="utf-8")
    )

    assert filenames[6] == (
        "007_evidence_scoring_recovery_reviews.sql"
    )
    assert "subject_type = 'scoring_recovery'" in sql
    assert "candidate_fingerprint = subject_id" in sql
    assert "supersedes_event_id" in sql
    assert "UNIQUE (brand_id, idempotency_key_hash)" in sql
    assert "runtime_effect = false" in sql
    assert "authority = false" in sql
    assert "automatic_scoring_effect = false" in sql


def test_claim_tile_review_migration_is_append_only_and_safe() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql in _migration_files()]
    sql = (
        resources.files("src.history")
        .joinpath("migrations/008_evidence_claim_tile_reviews.sql")
        .read_text(encoding="utf-8")
    )

    packet_sql = (
        resources.files("src.history")
        .joinpath(
            "migrations/"
            "009_evidence_claim_tile_review_packet_fingerprint.sql"
        )
        .read_text(encoding="utf-8")
    )

    registry_sql = (
        resources.files("src.history")
        .joinpath(
            "migrations/010_evidence_claim_tile_review_packets.sql"
        )
        .read_text(encoding="utf-8")
    )

    assert filenames[7] == "008_evidence_claim_tile_reviews.sql"
    assert filenames[8] == (
        "009_evidence_claim_tile_review_packet_fingerprint.sql"
    )
    assert filenames[9] == (
        "010_evidence_claim_tile_review_packets.sql"
    )
    assert "subject_type = 'claim_tile_mapping'" in sql
    assert "mapping_id = subject_id" in sql
    assert "evidence_claim_tile_mappings" in sql
    assert "supersedes_event_id" in sql
    assert "UNIQUE (brand_id, idempotency_key_hash)" in sql
    assert "runtime_effect = false" in sql
    assert "authority = false" in sql
    assert "automatic_tile_effect = false" in sql
    assert "automatic_scoring_effect = false" in sql
    assert "ADD COLUMN review_packet_fingerprint text" in packet_sql
    assert "review_packet_fingerprint IS NULL" in packet_sql
    assert "'^[0-9a-f]{64}$'" in packet_sql
    assert (
        "schema_version <> 'evidence-claim-tile-review-event-v2'"
        in packet_sql
    )
    assert "evidence_claim_tile_review_packets" in registry_sql
    assert "BEFORE UPDATE OR DELETE" in registry_sql
    assert "packet_fingerprint ~ '^[0-9a-f]{64}$'" in registry_sql
    assert "runtime_effect = false" in registry_sql
    assert "authority = false" in registry_sql
    assert "automatic_tile_effect = false" in registry_sql
    assert "automatic_scoring_effect = false" in registry_sql
    assert (
        "evidence_claim_tile_review_registered_packet_fk"
        in registry_sql
    )
    assert "NOT VALID" in registry_sql


def test_vault_canonical_memory_migration_is_append_only_and_vault_scoped() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql in _migration_files()]
    sql = (
        resources.files("src.history")
        .joinpath("migrations/011_evidence_vault_canonical_memory.sql")
        .read_text(encoding="utf-8")
    )

    assert filenames[10] == "011_evidence_vault_canonical_memory.sql"
    assert "evidence_vault_canonical_memory_packets" in sql
    assert "evidence_vault_canonical_memory_promotion_events" in sql
    assert "canonical_memory_promotion" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "UNIQUE (brand_id, idempotency_key_hash)" in sql
    assert "UNIQUE (brand_id, sequence)" in sql
    assert "authority_state = 'pending_review'" in sql
    assert "authority = false" in sql
    assert "authority = true" in sql
    assert "authority_scope = 'b3s-vault'" in sql
    assert "production_runtime_effect = false" in sql
    assert "scanner_runtime_effect = false" in sql


def test_vault_canonical_scoring_migration_is_immutable_and_vault_scoped() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql in _migration_files()]
    sql = (
        resources.files("src.history")
        .joinpath("migrations/012_evidence_vault_canonical_scoring.sql")
        .read_text(encoding="utf-8")
    )

    assert filenames[11] == "012_evidence_vault_canonical_scoring.sql"
    assert "evidence_vault_canonical_score_evaluations" in sql
    assert "UNIQUE (brand_id, evaluation_identity)" in sql
    assert "UNIQUE (brand_id, canonical_memory_version)" in sql
    assert "FOREIGN KEY (brand_id, reused_from_evaluation_identity)" in sql
    assert "reused_from_evaluation_identity" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "authority = true" in sql
    assert "authority_scope = 'b3s-vault'" in sql
    assert "production_runtime_effect = false" in sql
    assert "scanner_runtime_effect = false" in sql


def test_recovery_supplement_migration_is_immutable_and_non_authoritative() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql in _migration_files()]
    sql = (
        resources.files("src.history")
        .joinpath(
            "migrations/013_evidence_scoring_recovery_supplements.sql"
        )
        .read_text(encoding="utf-8")
    )

    assert filenames[12] == (
        "013_evidence_scoring_recovery_supplements.sql"
    )
    assert "evidence_scoring_recovery_supplement_packets" in sql
    assert "UNIQUE (brand_id, packet_fingerprint)" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "authority = false" in sql
    assert "runtime_effect = false" in sql
    assert "production_runtime_effect = false" in sql
    assert "scanner_runtime_effect = false" in sql


def test_vault_operational_migrations_are_versioned_and_vault_scoped() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql in _migration_files()]
    operational_memory_sql = (
        resources.files("src.history")
        .joinpath("migrations/014_evidence_vault_operational_memory_v2.sql")
        .read_text(encoding="utf-8")
    )
    operation_execution_sql = (
        resources.files("src.history")
        .joinpath("migrations/015_evidence_vault_operation_plan_execution.sql")
        .read_text(encoding="utf-8")
    )
    source_binding_sql = (
        resources.files("src.history")
        .joinpath("migrations/016_evidence_vault_source_packet_bindings.sql")
        .read_text(encoding="utf-8")
    )
    capture_lineage_sql = (
        resources.files("src.history")
        .joinpath("migrations/017_evidence_vault_capture_lineage.sql")
        .read_text(encoding="utf-8")
    )
    capture_lineage_hardening_sql = (
        resources.files("src.history")
        .joinpath(
            "migrations/018_evidence_vault_capture_lineage_hardening.sql"
        )
        .read_text(encoding="utf-8")
    )
    cumulative_landing_sql = (
        resources.files("src.history")
        .joinpath(
            "migrations/021_evidence_vault_cumulative_landing_hardening.sql"
        )
        .read_text(encoding="utf-8")
    )
    raw_planning_sql = (
        resources.files("src.history")
        .joinpath(
            "migrations/022_evidence_vault_raw_incremental_planning.sql"
        )
        .read_text(encoding="utf-8")
    )
    raw_replay_sql = (
        resources.files("src.history")
        .joinpath(
            "migrations/023_evidence_vault_raw_replay_projection.sql"
        )
        .read_text(encoding="utf-8")
    )
    accepted_relation_projection_sql = (
        resources.files("src.history")
        .joinpath(
            "migrations/024_evidence_vault_raw_accepted_relation_projection.sql"
        )
        .read_text(encoding="utf-8")
    )

    assert filenames[13:] == [
        "014_evidence_vault_operational_memory_v2.sql",
        "015_evidence_vault_operation_plan_execution.sql",
        "016_evidence_vault_source_packet_bindings.sql",
        "017_evidence_vault_capture_lineage.sql",
        "018_evidence_vault_capture_lineage_hardening.sql",
        "019_evidence_vault_verified_raw_provenance.sql",
        "020_evidence_vault_c7_shadow_readiness.sql",
        "021_evidence_vault_cumulative_landing_hardening.sql",
        "022_evidence_vault_raw_incremental_planning.sql",
        "023_evidence_vault_raw_replay_projection.sql",
        "024_evidence_vault_raw_accepted_relation_projection.sql",
        "025_evidence_vault_operational_sv9_shadow_assessments.sql",
    ]
    assert "packet_kind" in operational_memory_sql
    assert "operational_source_v2" in operational_memory_sql
    assert "operational_reviewed_v2" in operational_memory_sql
    assert "operational_v2" in operational_memory_sql
    assert (
        "DROP CONSTRAINT IF EXISTS "
        "evidence_vault_canonical_memory_brand_id_packet_fingerprint_key"
        in source_binding_sql
    )
    assert "production_runtime_effect" in operation_execution_sql
    assert "scanner_runtime_effect" in operation_execution_sql
    assert "CHECK (authority = false)" in operation_execution_sql
    assert "BEFORE UPDATE OR DELETE" in operation_execution_sql
    assert "evidence_vault_capture_watermark_events" in capture_lineage_sql
    assert "capture_sequence" in capture_lineage_sql
    assert "append_origin = 'capture_observation_commit'" in capture_lineage_sql
    assert "report_derived_candidate_capture_replay" not in capture_lineage_sql
    assert "UNIQUE (brand_id, capture_sequence)" in capture_lineage_sql
    assert "evidence_vault_operational_source_capture_lineage_bindings" in (
        capture_lineage_sql
    )
    assert "evidence_vault_operational_source_capture_lineage_members" in (
        capture_lineage_sql
    )
    assert capture_lineage_sql.count("BEFORE INSERT") == 2
    assert "predecessor must be sequence N-1" in capture_lineage_sql
    assert "first lineage checkpoint origin" in capture_lineage_sql
    assert "lineage checkpoints must be contiguous" in capture_lineage_sql
    assert capture_lineage_sql.count("BEFORE UPDATE OR DELETE") == 3
    assert capture_lineage_sql.count("CHECK (authority = false)") == 3
    assert "evidence_vault_brand_lock_key" in capture_lineage_hardening_sql
    assert "evidence_vault_canonical_fingerprint" in (
        capture_lineage_hardening_sql
    )
    assert "DEFERRABLE INITIALLY DEFERRED" in capture_lineage_hardening_sql
    assert "BEFORE TRUNCATE" in capture_lineage_hardening_sql
    assert "scan identity, request, and observation hash" in (
        capture_lineage_hardening_sql
    )
    assert "evidence_vault_relation_review_source_identity_fk" in (
        cumulative_landing_sql
    )
    assert "evidence_vault_all_operational_packet_payload_check" in (
        cumulative_landing_sql
    )
    assert "terminal Evidence Vault operation plans are immutable" in (
        cumulative_landing_sql
    )
    assert "evidence_vault_operation_plans_no_truncate" in cumulative_landing_sql
    assert "read_evidence_vault_raw_planning_context" in raw_planning_sql
    assert "LIMIT 500" in raw_planning_sql
    assert "evidence_count > 5000" in raw_planning_sql
    assert "raw planning replay plan exceeds bound" in raw_planning_sql
    assert "raw planning serialized evidence exceeds bound" in raw_planning_sql
    assert "evidence-vault-canonical-promotion" in raw_planning_sql
    assert "accepted_evidence_tile_relations', '[]'::jsonb" in raw_planning_sql
    assert raw_planning_sql.index("IF existing_plan IS NOT NULL") < raw_planning_sql.index(
        "SELECT * INTO latest_event"
    )
    assert "REVOKE EXECUTE ON FUNCTION" in raw_planning_sql
    assert "GRANT USAGE, CREATE ON SCHEMA b3s_history" in raw_planning_sql
    assert "provenance owner retains forbidden schema CREATE" in raw_planning_sql
    assert "aclexplode" in raw_planning_sql
    assert "raw scanner function % retains an unexpected EXECUTE grant" in raw_planning_sql
    assert "CREATE OR REPLACE FUNCTION" in raw_replay_sql
    assert "scans.metadata - ARRAY[" in raw_replay_sql
    assert "SET LOCAL ROLE b3s_history_vault_provenance_owner" in raw_replay_sql
    assert "RESET ROLE" in raw_replay_sql
    assert "scans.source_run_id" in raw_replay_sql
    assert "scans.status" in raw_replay_sql
    assert "scans.error_summary" in raw_replay_sql
    assert "accepted_evidence_tile_relations', accepted_relations" in (
        accepted_relation_projection_sql
    )
    assert "evidence_vault_verified_c7_lineage_members" in (
        accepted_relation_projection_sql
    )
    assert "Never turn a missing diagnostic mapping" in (
        accepted_relation_projection_sql
    )


def test_claim_tile_review_requires_packet_fingerprint() -> None:
    from dataclasses import replace

    from src.history.repository import (
        _validate_claim_tile_review_command,
    )

    valid = _claim_tile_review_command(
        "a" * 64,
        decision="accepted",
        expected_current_event_id=None,
        key_hash="b" * 64,
        fingerprint="c" * 64,
    )

    _validate_claim_tile_review_command(valid)
    with pytest.raises(
        ValueError,
        match="review_packet_fingerprint",
    ):
        _validate_claim_tile_review_command(
            replace(valid, review_packet_fingerprint="")
        )


def test_claim_reconciliation_repository_validation_rejects_bad_subjects() -> None:
    from src.history.repository import _validate_claim_reconciliation_command

    valid = _claim_reconciliation_command(
        "a" * 64,
        decision="accepted",
        expected_current_event_id=None,
        key_hash="b" * 64,
        fingerprint="c" * 64,
    )

    _validate_claim_reconciliation_command(
        valid,
        relation_type="replacement_candidate",
    )
    with pytest.raises(ValueError, match="relation type"):
        _validate_claim_reconciliation_command(
            valid,
            relation_type="canonical_replacement",
        )
    with pytest.raises(ValueError, match="subject_id"):
        _validate_claim_reconciliation_command(
            _claim_reconciliation_command(
                "not-a-digest",
                decision="accepted",
                expected_current_event_id=None,
                key_hash="b" * 64,
                fingerprint="c" * 64,
            ),
            relation_type="replacement_candidate",
        )


def test_claim_reconciliation_repository_is_idempotent_and_optimistic() -> None:
    from src.history.repository import PostgresHistoryRepository

    connection = _ClaimReconciliationConnection()
    repository = PostgresHistoryRepository(
        "postgresql://example.test/b3s",
        connect=lambda *_args, **_kwargs: connection,
    )
    repository._migrated = True
    subject_id = "a" * 64
    accepted_command = _claim_reconciliation_command(
        subject_id,
        decision="accepted",
        expected_current_event_id=None,
        key_hash="b" * 64,
        fingerprint="c" * 64,
    )

    accepted, replayed = repository.append_evidence_claim_reconciliation(
        "example.com",
        accepted_command,
        relation_type="replacement_candidate",
    )
    replay, was_replayed = repository.append_evidence_claim_reconciliation(
        "example.com",
        accepted_command,
        relation_type="replacement_candidate",
    )

    assert replayed is False
    assert was_replayed is True
    assert replay["id"] == accepted["id"]
    assert len(connection.events) == 1

    with pytest.raises(
        EvidenceClaimReconciliationConflictError,
        match="changed after it was read",
    ):
        repository.append_evidence_claim_reconciliation(
            "example.com",
            _claim_reconciliation_command(
                subject_id,
                decision="disputed",
                expected_current_event_id=None,
                key_hash="d" * 64,
                fingerprint="e" * 64,
            ),
            relation_type="replacement_candidate",
        )

    revoked, replayed = repository.append_evidence_claim_reconciliation(
        "example.com",
        _claim_reconciliation_command(
            subject_id,
            decision="revoked",
            expected_current_event_id=accepted["id"],
            key_hash="f" * 64,
            fingerprint="0" * 64,
        ),
        relation_type="replacement_candidate",
    )

    assert replayed is False
    assert revoked["sequence"] == 2
    assert revoked["supersedes_event_id"] == accepted["id"]
    assert len(connection.events) == 2


def test_scoring_recovery_review_repository_is_idempotent_and_optimistic() -> None:
    from src.history.repository import PostgresHistoryRepository

    connection = _ScoringRecoveryReviewConnection()
    repository = PostgresHistoryRepository(
        "postgresql://example.test/b3s",
        connect=lambda *_args, **_kwargs: connection,
    )
    repository._migrated = True
    subject_id = "a" * 64
    case_id = "scoring-recovery-example-com-magnetism-mg1-aaaaaaaaaaaa"
    accepted_command = _scoring_recovery_review_command(
        subject_id,
        case_id=case_id,
        decision="accepted",
        expected_current_event_id=None,
        key_hash="b" * 64,
        fingerprint="c" * 64,
    )

    accepted, replayed = (
        repository.append_evidence_scoring_recovery_review(
            "example.com",
            accepted_command,
        )
    )
    replay, was_replayed = (
        repository.append_evidence_scoring_recovery_review(
            "example.com",
            accepted_command,
        )
    )

    assert replayed is False
    assert was_replayed is True
    assert replay["id"] == accepted["id"]
    assert accepted["candidate_fingerprint"] == subject_id
    assert accepted["automatic_scoring_effect"] is False
    assert len(connection.events) == 1

    with pytest.raises(
        EvidenceScoringRecoveryReviewConflictError,
        match="changed after it was read",
    ):
        repository.append_evidence_scoring_recovery_review(
            "example.com",
            _scoring_recovery_review_command(
                subject_id,
                case_id=case_id,
                decision="disputed",
                expected_current_event_id=None,
                key_hash="d" * 64,
                fingerprint="e" * 64,
            ),
        )

    revoked, replayed = (
        repository.append_evidence_scoring_recovery_review(
            "example.com",
            _scoring_recovery_review_command(
                subject_id,
                case_id=case_id,
                decision="revoked",
                expected_current_event_id=accepted["id"],
                key_hash="f" * 64,
                fingerprint="0" * 64,
            ),
        )
    )

    assert replayed is False
    assert revoked["sequence"] == 2
    assert revoked["previous_event_id"] == accepted["id"]
    assert revoked["supersedes_event_id"] == accepted["id"]
    assert len(connection.events) == 2


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_concurrent_release_migration_is_database_serialized() -> None:
    import psycopg
    from threading import Barrier

    from src.history.repository import PostgresHistoryRepository

    _require_schema_drop_opt_in()
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
    barrier = Barrier(2)

    def migrate_concurrently() -> list[str]:
        repository = PostgresHistoryRepository(dsn)
        barrier.wait()
        return repository.migrate()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: migrate_concurrently(), range(2)))
        assert sorted(len(result) for result in results) == [0, 24]
        assert sorted({filename for result in results for filename in result}) == [
            f"{index:03d}_" + name
            for index, name in enumerate(
                [
                    "history_v1.sql",
                    "evidence_stability.sql",
                    "evidence_ledger_shadow.sql",
                    "evidence_memory_adjudications.sql",
                    "evidence_claim_reconciliations.sql",
                    "evidence_claim_tile_ledger.sql",
                    "evidence_scoring_recovery_reviews.sql",
                    "evidence_claim_tile_reviews.sql",
                    "evidence_claim_tile_review_packet_fingerprint.sql",
                    "evidence_claim_tile_review_packets.sql",
                    "evidence_vault_canonical_memory.sql",
                    "evidence_vault_canonical_scoring.sql",
                    "evidence_scoring_recovery_supplements.sql",
                    "evidence_vault_operational_memory_v2.sql",
                    "evidence_vault_operation_plan_execution.sql",
                    "evidence_vault_source_packet_bindings.sql",
                    "evidence_vault_capture_lineage.sql",
                    "evidence_vault_capture_lineage_hardening.sql",
                    "evidence_vault_verified_raw_provenance.sql",
                    "evidence_vault_c7_shadow_readiness.sql",
                    "evidence_vault_cumulative_landing_hardening.sql",
                    "evidence_vault_raw_incremental_planning.sql",
                    "evidence_vault_raw_replay_projection.sql",
                    "evidence_vault_raw_accepted_relation_projection.sql",
                ],
                start=1,
            )
        ]
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")


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
    monkeypatch.setenv(
        "B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE",
        "shadow",
    )
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
            "005_evidence_claim_reconciliations.sql",
            "006_evidence_claim_tile_ledger.sql",
            "007_evidence_scoring_recovery_reviews.sql",
            "008_evidence_claim_tile_reviews.sql",
            "009_evidence_claim_tile_review_packet_fingerprint.sql",
            "010_evidence_claim_tile_review_packets.sql",
            "011_evidence_vault_canonical_memory.sql",
            "012_evidence_vault_canonical_scoring.sql",
            "013_evidence_scoring_recovery_supplements.sql",
            "014_evidence_vault_operational_memory_v2.sql",
            "015_evidence_vault_operation_plan_execution.sql",
            "016_evidence_vault_source_packet_bindings.sql",
            "017_evidence_vault_capture_lineage.sql",
            "018_evidence_vault_capture_lineage_hardening.sql",
            "019_evidence_vault_verified_raw_provenance.sql",
            "020_evidence_vault_c7_shadow_readiness.sql",
            "021_evidence_vault_cumulative_landing_hardening.sql",
            "022_evidence_vault_raw_incremental_planning.sql",
            "023_evidence_vault_raw_replay_projection.sql",
            "024_evidence_vault_raw_accepted_relation_projection.sql",
            "025_evidence_vault_operational_sv9_shadow_assessments.sql",
        ]
        assert repository.migrate() == []

        older = _report("scan-older", "2026-07-01T08:00:00Z", score=61)
        newer = _report("scan-newer", "2026-07-02T08:00:00Z", score=77)
        old_outcome = repository.import_report(older)
        assert old_outcome.status == "imported"
        assert repository.import_report(older).status == "unchanged"
        assert repository.import_report(newer).status == "imported"
        assert (
            repository.get_evidence_vault_current_capture_watermark(
                "example.com"
            )
            is None
        )

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
        scoring_memory = (
            repository.get_evidence_scoring_memory_preview(
                "example.com"
            )
        )
        assert scoring_memory is not None
        assert scoring_memory["runtime_effect"] is False
        assert scoring_memory["summary"][
            "accepted_evidence_count"
        ] == 1
        restarted_repository = PostgresHistoryRepository(dsn)
        restarted_scoring_memory = (
            restarted_repository.get_evidence_scoring_memory_preview(
                "example.com"
            )
        )
        assert restarted_scoring_memory is not None
        assert (
            restarted_scoring_memory["memory_version"]
            == scoring_memory["memory_version"]
        )
        assert (
            restarted_scoring_memory["state_fingerprint"]
            == scoring_memory["state_fingerprint"]
        )
        ledger = repository.get_evidence_ledger_shadow("example.com")
        assert ledger is not None
        assert ledger["runtime_effect"] is False
        assert ledger["summary"]["state_counts"] == {"validation_candidate": 1}
        assert ledger["entries"][0]["observation_count"] == 2
        claim_tile_ledger = repository.get_evidence_claim_tile_ledger(
            "example.com"
        )
        assert claim_tile_ledger is not None
        assert claim_tile_ledger["runtime_effect"] is False
        assert claim_tile_ledger["authority"] is False
        assert claim_tile_ledger["summary"]["mapping_count"] == 0
        assert claim_tile_ledger["summary"]["mapping_series_count"] == 1
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
            "evidence_claim_reconciliation_events": 0,
            "evidence_scoring_recovery_review_events": 0,
            "evidence_scoring_recovery_supplement_packets": 0,
            "evidence_claim_tile_review_events": 0,
            "evidence_claim_tile_review_packets": 0,
            "evidence_vault_canonical_memory_packets": 0,
            "evidence_vault_canonical_memory_promotion_events": 0,
            "evidence_vault_canonical_score_evaluations": 0,
            "evidence_claim_tile_ledger_states": 1,
            "evidence_claim_tile_mapping_series": 1,
            "evidence_claim_tile_mappings": 0,
            "evidence_claim_tile_mapping_observations": 0,
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

        with psycopg.connect(dsn) as conn:
            conn.execute(
                "DELETE FROM b3s_history.evidence_claim_tile_ledger_states"
            )
        assert repository.get_evidence_claim_tile_ledger(
            "example.com"
        ) is None

        claim_tile_backfill = (
            repository.rebuild_evidence_claim_tile_ledgers()
        )
        assert claim_tile_backfill == {
            "mode": "shadow",
            "runtime_effect": False,
            "authority": False,
            "workspace_slug": "b3s",
            "brands_discovered": 1,
            "rebuilt": 1,
            "failed": 0,
            "failed_domains": [],
        }
        rebuilt_claim_tile_ledger = (
            repository.get_evidence_claim_tile_ledger("example.com")
        )
        assert rebuilt_claim_tile_ledger is not None
        assert (
            rebuilt_claim_tile_ledger["state_fingerprint"]
            == claim_tile_ledger["state_fingerprint"]
        )

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

        relation_subject_id = "d" * 64
        relation_command = _claim_reconciliation_command(
            relation_subject_id,
            decision="accepted",
            expected_current_event_id=None,
            key_hash="e" * 64,
            fingerprint="f" * 64,
        )
        relation_event, replayed = (
            repository.append_evidence_claim_reconciliation(
                "example.com",
                relation_command,
                relation_type="replacement_candidate",
            )
        )
        assert replayed is False
        assert relation_event["relation_type"] == "replacement_candidate"
        assert relation_event["runtime_effect"] is False
        assert relation_event["authority"] is False
        relation_replay, replayed = (
            repository.append_evidence_claim_reconciliation(
                "example.com",
                relation_command,
                relation_type="replacement_candidate",
            )
        )
        assert replayed is True
        assert relation_replay["id"] == relation_event["id"]

        with pytest.raises(
            EvidenceClaimReconciliationConflictError,
            match="changed after it was read",
        ):
            repository.append_evidence_claim_reconciliation(
                "example.com",
                _claim_reconciliation_command(
                    relation_subject_id,
                    decision="disputed",
                    expected_current_event_id=None,
                    key_hash="0" * 64,
                    fingerprint="1" * 64,
                ),
                relation_type="replacement_candidate",
            )

        revoked_relation, replayed = (
            repository.append_evidence_claim_reconciliation(
                "example.com",
                _claim_reconciliation_command(
                    relation_subject_id,
                    decision="revoked",
                    expected_current_event_id=relation_event["id"],
                    key_hash="2" * 64,
                    fingerprint="3" * 64,
                ),
                relation_type="replacement_candidate",
            )
        )
        assert replayed is False
        assert revoked_relation["supersedes_event_id"] == relation_event["id"]
        relation_journal = repository.list_evidence_claim_reconciliations(
            "example.com"
        )
        assert relation_journal["total"] == 2
        assert [
            event["effective_state"]
            for event in relation_journal["events"]
        ] == ["revoked", "superseded"]
        assert relation_journal["current"] == [revoked_relation]
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_postgres_scoring_recovery_survives_restart_and_revocation() -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    _require_schema_drop_opt_in()
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    repository = PostgresHistoryRepository(dsn)
    try:
        repository.migrate()
        repository.import_report(
            _scoring_recovery_report(
                "recovery-older",
                "2026-07-04T08:00:00Z",
                target_state="ok",
            )
        )
        repository.import_report(
            _scoring_recovery_report(
                "recovery-newer",
                "2026-07-05T08:00:00Z",
                target_state="sin_evidencia",
            )
        )
        recovery_preview = (
            repository.get_evidence_scoring_memory_preview(
                "memory.example"
            )
        )
        assert recovery_preview is not None
        assert recovery_preview["scoring"]["score_delta"] > 0
        assert (
            recovery_preview["reviewed_shadow"]["scoring"][
                "score_delta"
            ]
            == 0
        )
        recovery_candidate = recovery_preview[
            "recovery_review_candidates"
        ][0]
        recovery_event, replayed = (
            repository.append_evidence_scoring_recovery_review(
                "memory.example",
                _scoring_recovery_review_command(
                    recovery_candidate["candidate_fingerprint"],
                    case_id=recovery_candidate["case_id"],
                    decision="accepted",
                    expected_current_event_id=None,
                    key_hash="4" * 64,
                    fingerprint="5" * 64,
                ),
            )
        )
        assert replayed is False
        assert recovery_event["automatic_scoring_effect"] is False

        restarted_repository = PostgresHistoryRepository(dsn)
        restarted_preview = (
            restarted_repository.get_evidence_scoring_memory_preview(
                "memory.example"
            )
        )
        assert restarted_preview is not None
        assert (
            restarted_preview["reviewed_shadow"]["scoring"][
                "score_delta"
            ]
            == recovery_preview["scoring"]["score_delta"]
        )
        assert restarted_preview["recovery_review"]["summary"][
            "accepted_count"
        ] == 1

        revoked_recovery, replayed = (
            restarted_repository.append_evidence_scoring_recovery_review(
                "memory.example",
                _scoring_recovery_review_command(
                    recovery_candidate["candidate_fingerprint"],
                    case_id=recovery_candidate["case_id"],
                    decision="revoked",
                    expected_current_event_id=recovery_event["id"],
                    key_hash="6" * 64,
                    fingerprint="7" * 64,
                ),
            )
        )
        assert replayed is False
        assert revoked_recovery["sequence"] == 2

        revoked_preview = PostgresHistoryRepository(
            dsn
        ).get_evidence_scoring_memory_preview("memory.example")
        assert revoked_preview is not None
        assert (
            revoked_preview["reviewed_shadow"]["scoring"][
                "score_delta"
            ]
            == 0
        )
        assert revoked_preview["recovery_review"]["summary"][
            "revoked_count"
        ] == 1
        recovery_journal = (
            restarted_repository.list_evidence_scoring_recovery_reviews(
                "memory.example"
            )
        )
        assert recovery_journal["total"] == 2
        assert [
            event["effective_state"]
            for event in recovery_journal["events"]
        ] == ["revoked", "superseded"]
        assert recovery_journal["current"] == [revoked_recovery]
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_postgres_recovery_supplement_survives_restart() -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    _require_schema_drop_opt_in()
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    report = _scoring_recovery_report(
        "recovery-supplement",
        "2026-07-05T08:00:00Z",
        target_state="ok",
    )
    repository = PostgresHistoryRepository(dsn)
    try:
        repository.migrate()
        repository.import_report(report)
        subject_id = build_evidence_memory_identity_v2([report])["entries"][
            0
        ]["evidence_id"]
        repository.append_evidence_memory_adjudication(
            "memory.example",
            _adjudication_command(
                subject_id,
                decision="accepted",
                expected_current_event_id=None,
                key_hash="b" * 64,
                fingerprint="c" * 64,
            ),
        )
        preview = repository.get_evidence_scoring_memory_preview(
            "memory.example"
        )
        assert preview is not None
        adjudications = (
            repository.list_current_evidence_memory_adjudications(
                "memory.example"
            )
        )
        source_catalog = build_accepted_evidence_passage_catalog(
            [report],
            adjudications=adjudications,
        )
        packet = build_recovery_review_supplement_packet_from_preview(
            preview,
            source_catalog=source_catalog,
            proposals=[
                {
                    "component_key": "magnetism",
                    "tile_id": "MG2",
                    "passages": [
                        {
                            "evidence_id": subject_id,
                            "quote": (
                                "A distinctive promise that customers "
                                "remember."
                            ),
                        }
                    ],
                }
            ],
        )

        stored, replayed = (
            repository.register_evidence_scoring_recovery_supplement_packet(
                "memory.example",
                packet,
            )
        )
        assert replayed is False
        assert stored["packet"] == packet
        replay, replayed = (
            repository.register_evidence_scoring_recovery_supplement_packet(
                "memory.example",
                packet,
            )
        )
        assert replayed is True
        assert replay == stored

        with psycopg.connect(dsn, autocommit=True) as conn:
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="append-only",
            ):
                conn.execute(
                    """
                    UPDATE b3s_history.evidence_scoring_recovery_supplement_packets
                    SET rubric_version = rubric_version
                    WHERE packet_fingerprint = %s
                    """,
                    (packet["packet_fingerprint"],),
                )

        restarted = PostgresHistoryRepository(
            dsn
        ).get_evidence_scoring_memory_preview("memory.example")
        assert restarted is not None
        assert restarted["recovery_review_supplements"][
            "active_packet_count"
        ] == 1
        supplemented = next(
            candidate
            for candidate in restarted["recovery_review_candidates"]
            if candidate["tile"]["tile_key"] == "magnetism.MG2"
        )
        assert supplemented["contexts"][0]["review_scope"] == (
            "mapper_omission_supplement"
        )
        assert supplemented["authority"] is False
        assert supplemented["runtime_effect"] is False
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_postgres_claim_tile_review_survives_restart_and_revocation(
    monkeypatch,
) -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    _require_schema_drop_opt_in()
    monkeypatch.setenv(
        "B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE",
        "shadow",
    )
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    repository = PostgresHistoryRepository(dsn)
    try:
        repository.migrate()
        repository.import_report(
            _claim_tile_report(
                "claim-tile-review",
                "2026-07-06T08:00:00Z",
            )
        )
        ledger = repository.get_evidence_claim_tile_ledger(
            "memory.example"
        )
        assert ledger is not None
        assert ledger["runtime_effect"] is False
        assert ledger["authority"] is False
        assert ledger["summary"]["mapping_count"] == 1
        mapping = ledger["mappings"][0]
        with pytest.raises(
            EvidenceClaimTileReviewPacketNotFoundError,
            match="registered review packet",
        ):
            repository.append_evidence_claim_tile_review(
                "memory.example",
                _claim_tile_review_command(
                    mapping["mapping_id"],
                    decision="accepted",
                    expected_current_event_id=None,
                    key_hash="7" * 64,
                    fingerprint="6" * 64,
                    review_packet_fingerprint="f" * 64,
                ),
            )
        packet, replayed = (
            repository.register_evidence_claim_tile_review_packet(
                "memory.example"
            )
        )
        assert replayed is False
        assert packet["manifest"]["candidate_count"] == 1
        assert packet["candidates"][0]["subject_id"] == (
            mapping["mapping_id"]
        )
        packet_fingerprint = packet["packet_fingerprint"]
        pending_queue = (
            repository.get_evidence_claim_tile_review_queue(
                "memory.example",
                packet_fingerprint,
            )
        )
        assert pending_queue["review_complete"] is False
        assert pending_queue["summary"]["unreviewed_mapping_count"] == 1
        assert pending_queue["items"][0]["review_reason"] == (
            "unreviewed_mapping"
        )
        replayed_packet, replayed = (
            repository.register_evidence_claim_tile_review_packet(
                "memory.example"
            )
        )
        assert replayed is True
        assert replayed_packet == packet
        with psycopg.connect(dsn, autocommit=True) as conn:
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="append-only",
            ):
                conn.execute(
                    """
                    UPDATE b3s_history.evidence_claim_tile_review_packets
                    SET candidate_count = candidate_count
                    WHERE packet_fingerprint = %s
                    """,
                    (packet_fingerprint,),
                )

        accepted, replayed = (
            repository.append_evidence_claim_tile_review(
                "memory.example",
                _claim_tile_review_command(
                    mapping["mapping_id"],
                    decision="accepted",
                    expected_current_event_id=None,
                    key_hash="8" * 64,
                    fingerprint="9" * 64,
                    review_packet_fingerprint=packet_fingerprint,
                ),
            )
        )
        assert replayed is False
        assert accepted["mapping_id"] == mapping["mapping_id"]
        assert accepted["source_evidence_id"] == (
            mapping["source_evidence_id"]
        )
        assert accepted["claim_variant_id"] == (
            mapping["claim_variant_id"]
        )
        assert accepted["tile_key"] == "mission.M1"
        assert accepted["automatic_tile_effect"] is False
        assert accepted["automatic_scoring_effect"] is False
        reviewed_queue = (
            repository.get_evidence_claim_tile_review_queue(
                "memory.example",
                packet_fingerprint,
            )
        )
        assert reviewed_queue["review_complete"] is True
        assert reviewed_queue["summary"]["accepted_count"] == 1

        restarted = PostgresHistoryRepository(dsn)
        assert (
            restarted.get_evidence_claim_tile_review_packet(
                "memory.example",
                packet_fingerprint,
            )
            == packet
        )
        journal = restarted.list_evidence_claim_tile_reviews(
            "memory.example"
        )
        assert journal["total"] == 1
        assert journal["current"] == [accepted]
        assert (
            restarted.get_evidence_claim_tile_ledger(
                "memory.example"
            )["state_fingerprint"]
            == ledger["state_fingerprint"]
        )
        reviewed_memory = (
            restarted.get_reviewed_claim_tile_memory_shadow(
                "memory.example"
            )
        )
        assert reviewed_memory is not None
        assert reviewed_memory["selection_ready"] is True
        assert reviewed_memory["summary"][
            "accepted_mapping_count"
        ] == 1
        assert reviewed_memory["accepted_mappings"][0][
            "mapping_id"
        ] == mapping["mapping_id"]
        assert reviewed_memory["runtime_effect"] is False
        assert reviewed_memory["automatic_scoring_effect"] is False

        restarted.import_report(
            _claim_tile_report(
                "claim-tile-review-repeated",
                "2026-07-07T08:00:00Z",
            )
        )
        repeated_packet, replayed = (
            restarted.register_evidence_claim_tile_review_packet(
                "memory.example"
            )
        )
        assert replayed is False
        assert repeated_packet["packet_fingerprint"] != packet_fingerprint
        repeated_queue = (
            restarted.get_evidence_claim_tile_review_queue(
                "memory.example",
                repeated_packet["packet_fingerprint"],
            )
        )
        assert repeated_queue["review_complete"] is True
        assert repeated_queue["summary"][
            "unreviewed_mapping_count"
        ] == 0
        assert repeated_queue["summary"][
            "changed_review_target_count"
        ] == 0
        assert repeated_queue["summary"]["observation_state_counts"] == {
            "repeated": 1
        }
        assert repeated_queue["items"][0]["review_reason"] == (
            "review_target_unchanged"
        )

        revoked, replayed = (
            restarted.append_evidence_claim_tile_review(
                "memory.example",
                _claim_tile_review_command(
                    mapping["mapping_id"],
                    decision="revoked",
                    expected_current_event_id=accepted["id"],
                    key_hash="a" * 64,
                    fingerprint="b" * 64,
                    review_packet_fingerprint=repeated_packet[
                        "packet_fingerprint"
                    ],
                ),
            )
        )
        assert replayed is False
        assert revoked["sequence"] == 2
        assert revoked["previous_event_id"] == accepted["id"]

        restarted_again = PostgresHistoryRepository(dsn)
        revoked_journal = (
            restarted_again.list_evidence_claim_tile_reviews(
                "memory.example"
            )
        )
        assert revoked_journal["total"] == 2
        assert [
            event["effective_state"]
            for event in revoked_journal["events"]
        ] == ["revoked", "superseded"]
        assert revoked_journal["current"] == [revoked]
        revoked_memory = (
            restarted_again.get_reviewed_claim_tile_memory_shadow(
                "memory.example"
            )
        )
        assert revoked_memory is not None
        assert revoked_memory["selection_ready"] is False
        assert revoked_memory["summary"][
            "reviewed_mapping_count"
        ] == 0
        assert revoked_memory["summary"]["pending_mapping_count"] == 1
        assert revoked_memory["accepted_mappings"] == []
        revoked_queue = (
            restarted_again.get_evidence_claim_tile_review_queue(
                "memory.example",
                repeated_packet["packet_fingerprint"],
            )
        )
        assert revoked_queue["review_complete"] is False
        assert revoked_queue["summary"]["revoked_review_count"] == 1
        assert revoked_queue["items"][0]["review_reason"] == (
            "review_revoked"
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_release_migrate_only_cli_is_complete_and_idempotent(
    capsys,
    monkeypatch,
    tmp_path,
) -> None:
    import psycopg

    _require_schema_drop_opt_in()
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    monkeypatch.setattr(
        import_b3s_reports_postgres,
        "B3S_DATABASE_URL",
        dsn,
    )
    command = [
        "--reports-dir",
        str(tmp_path),
        "--migrate-only",
    ]
    try:
        assert import_b3s_reports_postgres.main(command) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["status"] == "ok"
        assert first["applied_migrations"] == [
            "001_history_v1.sql",
            "002_evidence_stability.sql",
            "003_evidence_ledger_shadow.sql",
            "004_evidence_memory_adjudications.sql",
            "005_evidence_claim_reconciliations.sql",
            "006_evidence_claim_tile_ledger.sql",
            "007_evidence_scoring_recovery_reviews.sql",
            "008_evidence_claim_tile_reviews.sql",
            "009_evidence_claim_tile_review_packet_fingerprint.sql",
            "010_evidence_claim_tile_review_packets.sql",
            "011_evidence_vault_canonical_memory.sql",
            "012_evidence_vault_canonical_scoring.sql",
            "013_evidence_scoring_recovery_supplements.sql",
            "014_evidence_vault_operational_memory_v2.sql",
            "015_evidence_vault_operation_plan_execution.sql",
            "016_evidence_vault_source_packet_bindings.sql",
            "017_evidence_vault_capture_lineage.sql",
            "018_evidence_vault_capture_lineage_hardening.sql",
            "019_evidence_vault_verified_raw_provenance.sql",
            "020_evidence_vault_c7_shadow_readiness.sql",
            "021_evidence_vault_cumulative_landing_hardening.sql",
            "022_evidence_vault_raw_incremental_planning.sql",
            "023_evidence_vault_raw_replay_projection.sql",
            "024_evidence_vault_raw_accepted_relation_projection.sql",
            "025_evidence_vault_operational_sv9_shadow_assessments.sql",
        ]

        assert import_b3s_reports_postgres.main(command) == 0
        second = json.loads(capsys.readouterr().out)
        assert second["status"] == "ok"
        assert second["applied_migrations"] == []

        with psycopg.connect(dsn) as conn:
            stored = conn.execute(
                """
                SELECT to_regclass(
                    'b3s_history.evidence_scoring_recovery_review_events'
                )::text AS recovery_review_table,
                to_regclass(
                    'b3s_history.evidence_claim_tile_review_events'
                )::text AS claim_tile_review_table,
                to_regclass(
                    'b3s_history.evidence_claim_tile_review_packets'
                )::text AS claim_tile_review_packet_table,
                to_regclass(
                    'b3s_history.evidence_vault_canonical_memory_packets'
                )::text AS vault_canonical_packet_table,
                to_regclass(
                    'b3s_history.evidence_vault_canonical_memory_promotion_events'
                )::text AS vault_canonical_promotion_table,
                to_regclass(
                    'b3s_history.evidence_vault_canonical_score_evaluations'
                )::text AS vault_canonical_score_table,
                to_regclass(
                    'b3s_history.evidence_scoring_recovery_supplement_packets'
                )::text AS recovery_supplement_packet_table,
                to_regclass(
                    'b3s_history.evidence_vault_operation_plans'
                )::text AS vault_operation_plan_table,
                to_regclass(
                    'b3s_history.evidence_vault_operational_relation_reviews'
                )::text AS vault_operational_review_table,
                (
                    SELECT count(*)
                    FROM b3s_history.schema_migrations
                ) AS migration_count
                """
            ).fetchone()
        assert stored[0] == (
            "b3s_history.evidence_scoring_recovery_review_events"
        )
        assert stored[1] == (
            "b3s_history.evidence_claim_tile_review_events"
        )
        assert stored[2] == (
            "b3s_history.evidence_claim_tile_review_packets"
        )
        assert stored[3] == (
            "b3s_history.evidence_vault_canonical_memory_packets"
        )
        assert stored[4] == (
            "b3s_history.evidence_vault_canonical_memory_promotion_events"
        )
        assert stored[5] == (
            "b3s_history.evidence_vault_canonical_score_evaluations"
        )
        assert stored[6] == (
            "b3s_history.evidence_scoring_recovery_supplement_packets"
        )
        assert stored[7] == "b3s_history.evidence_vault_operation_plans"
        assert stored[8] == (
            "b3s_history.evidence_vault_operational_relation_reviews"
        )
        assert stored[9] == 24
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


def _claim_reconciliation_command(
    subject_id: str,
    *,
    decision: str,
    expected_current_event_id: str | None,
    key_hash: str,
    fingerprint: str,
) -> EvidenceClaimReconciliationCommand:
    return EvidenceClaimReconciliationCommand(
        subject_id=subject_id,
        decision=decision,
        expected_current_event_id=expected_current_event_id,
        reviewer="gsus",
        reason_code="relation_reviewed",
        rationale="The reviewer checked the claim relation.",
        evaluator_version="manual-review-v1",
        actor_id="gsus",
        idempotency_key_hash=key_hash,
        request_fingerprint=fingerprint,
    )


def _scoring_recovery_review_command(
    subject_id: str,
    *,
    case_id: str,
    decision: str,
    expected_current_event_id: str | None,
    key_hash: str,
    fingerprint: str,
) -> EvidenceScoringRecoveryReviewCommand:
    return EvidenceScoringRecoveryReviewCommand(
        subject_id=subject_id,
        case_id=case_id,
        decision=decision,
        expected_current_event_id=expected_current_event_id,
        reviewer="gsus",
        reason_code="tile_contract_reviewed",
        rationale="The reviewer checked the semantic tile contract.",
        evaluator_version="manual-review-v1",
        actor_id="gsus",
        idempotency_key_hash=key_hash,
        request_fingerprint=fingerprint,
    )


def _claim_tile_review_command(
    subject_id: str,
    *,
    decision: str,
    expected_current_event_id: str | None,
    key_hash: str,
    fingerprint: str,
    review_packet_fingerprint: str = "e" * 64,
) -> EvidenceClaimTileReviewCommand:
    return EvidenceClaimTileReviewCommand(
        subject_id=subject_id,
        decision=decision,
        expected_current_event_id=expected_current_event_id,
        reviewer="gsus",
        reason_code="tile_contract_reviewed",
        rationale="The reviewer checked the claim-to-tile contract.",
        evaluator_version="manual-review-v1",
        review_packet_fingerprint=review_packet_fingerprint,
        actor_id="gsus",
        idempotency_key_hash=key_hash,
        request_fingerprint=fingerprint,
    )


class _ClaimReconciliationCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _ClaimReconciliationConnection:
    def __init__(self):
        self.brand_id = uuid4()
        self.events: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=(), **_kwargs):
        compact = " ".join(str(sql).split())
        if "SELECT brands.id" in compact:
            return _ClaimReconciliationCursor({"id": self.brand_id})
        if "pg_advisory_xact_lock" in compact:
            return _ClaimReconciliationCursor()
        if (
            "SELECT * FROM b3s_history.evidence_claim_reconciliation_events"
            in compact
            and "idempotency_key_hash = %s" in compact
        ):
            key_hash = params[1]
            row = next(
                (
                    event
                    for event in self.events
                    if event["idempotency_key_hash"] == key_hash
                ),
                None,
            )
            return _ClaimReconciliationCursor(row)
        if (
            "SELECT id FROM b3s_history.evidence_claim_reconciliation_events"
            in compact
        ):
            current = self._current(params[2])
            return _ClaimReconciliationCursor(
                {"id": current["id"]} if current else None
            )
        if (
            "SELECT * FROM b3s_history.evidence_claim_reconciliation_events"
            in compact
            and "ORDER BY sequence DESC" in compact
        ):
            return _ClaimReconciliationCursor(self._current(params[2]))
        if (
            "INSERT INTO b3s_history.evidence_claim_reconciliation_events"
            in compact
        ):
            row = {
                "id": params[0],
                "brand_id": params[1],
                "subject_type": params[2],
                "subject_id": params[3],
                "relation_type": params[4],
                "sequence": params[5],
                "decision": params[6],
                "supersedes_event_id": params[7],
                "schema_version": params[8],
                "policy_version": params[9],
                "evaluator_version": params[10],
                "reviewer": params[11],
                "actor_id": params[12],
                "reason_code": params[13],
                "rationale": params[14],
                "idempotency_key_hash": params[15],
                "request_fingerprint": params[16],
                "runtime_effect": False,
                "authority": False,
                "created_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
            }
            self.events.append(row)
            return _ClaimReconciliationCursor(row)
        raise AssertionError(f"unexpected SQL: {compact}")

    def _current(self, subject_id):
        matching = [
            event
            for event in self.events
            if event["subject_id"] == subject_id
        ]
        return (
            max(matching, key=lambda event: event["sequence"])
            if matching
            else None
        )


class _ScoringRecoveryReviewConnection:
    def __init__(self):
        self.brand_id = uuid4()
        self.events: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=(), **_kwargs):
        compact = " ".join(str(sql).split())
        table = (
            "b3s_history.evidence_scoring_recovery_review_events"
        )
        if "SELECT brands.id" in compact:
            return _ClaimReconciliationCursor({"id": self.brand_id})
        if "pg_advisory_xact_lock" in compact:
            return _ClaimReconciliationCursor()
        if (
            f"SELECT * FROM {table}" in compact
            and "idempotency_key_hash = %s" in compact
        ):
            key_hash = params[1]
            row = next(
                (
                    event
                    for event in self.events
                    if event["idempotency_key_hash"] == key_hash
                ),
                None,
            )
            return _ClaimReconciliationCursor(row)
        if f"SELECT id FROM {table}" in compact:
            current = self._current(params[2])
            return _ClaimReconciliationCursor(
                {"id": current["id"]} if current else None
            )
        if (
            f"SELECT * FROM {table}" in compact
            and "ORDER BY sequence DESC" in compact
        ):
            return _ClaimReconciliationCursor(
                self._current(params[2])
            )
        if f"INSERT INTO {table}" in compact:
            row = {
                "id": params[0],
                "brand_id": params[1],
                "subject_type": params[2],
                "subject_id": params[3],
                "case_id": params[4],
                "candidate_fingerprint": params[5],
                "sequence": params[6],
                "decision": params[7],
                "supersedes_event_id": params[8],
                "schema_version": params[9],
                "policy_version": params[10],
                "evaluator_version": params[11],
                "reviewer": params[12],
                "actor_id": params[13],
                "reason_code": params[14],
                "rationale": params[15],
                "idempotency_key_hash": params[16],
                "request_fingerprint": params[17],
                "runtime_effect": False,
                "authority": False,
                "automatic_scoring_effect": False,
                "created_at": datetime(
                    2026,
                    7,
                    29,
                    tzinfo=timezone.utc,
                ),
            }
            self.events.append(row)
            return _ClaimReconciliationCursor(row)
        raise AssertionError(f"unexpected SQL: {compact}")

    def _current(self, subject_id):
        matching = [
            event
            for event in self.events
            if event["subject_id"] == subject_id
        ]
        return (
            max(matching, key=lambda event: event["sequence"])
            if matching
            else None
        )


def _scoring_recovery_report(
    report_id: str,
    created_at: str,
    *,
    target_state: str,
) -> dict:
    quote = "A distinctive promise that customers remember."
    evidence_record = {
        "ref": "web.home.0",
        "source": "web",
        "evidence_type": "owned_copy.homepage",
        "content": quote,
        "url": "https://memory.example",
        "confidence": "high",
        "metadata": {"source_class": "owned_copy"},
    }
    raw_components: dict[str, dict] = {}
    public_components: list[dict] = []
    current_score = 0
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
        lit = sum(
            1 for verdict in profile if verdict["estado"] == "ok"
        )
        points = component_points(component_key, lit)
        current_score += points
        raw_components[component_key] = {
            "component": component_key,
            "status": "scored",
            "score": lit,
            "scale": int(spec["scale"]),
            "points": points,
            "confidence": "high",
            "detected_content": quote if lit else "",
            "detection_mode": "sv9_flow",
            "detection_limitations": [],
            "evidence_source_summary": {"owned_copy": 1, "total": 1},
            "tile_profile": profile,
        }
        public_components.append(
            {
                "key": component_key,
                "label": str(spec["label"]),
                "status": "scored",
                "score": lit,
                "scale": int(spec["scale"]),
                "points": points,
                "confidence": "high",
                "resumen": quote if lit else "",
                "veredicto": "Synthetic durable-history fixture.",
                "message": "Synthetic durable-history fixture.",
                "tile_profile": profile,
            }
        )
    return {
        "id": report_id,
        "brand_name": "Memory Example",
        "url": "https://memory.example",
        "created_at": created_at,
        "score": current_score,
        "base_average": 0,
        "reliability_status": "usable",
        "not_detected": [],
        "limitations": [],
        "acquisition_gate": {"state": "pass"},
        "attempts": [],
        "acquisition_artifacts": [],
        "blocks": [
            {
                "name": "magnetism",
                "detected": target_state == "ok",
                "content": quote if target_state == "ok" else "",
                "confidence": "high",
                "coverage_status": (
                    "evidence"
                    if target_state == "ok"
                    else "insufficient"
                ),
                "provenance_source": "llm_and_gate",
                "refs": [
                    {
                        "ref": "web.home.0",
                        "url": evidence_record["url"],
                        "snippet": quote,
                    }
                ],
            }
        ],
        "components": public_components,
        "raw": {
            "schema_version": "sv9-flow-sv9-shadow-eval-v1",
            "source_run_id": 456,
            "flow": {
                "candidate": {
                    "evidence_pack": {"evidence": [evidence_record]},
                    "interpretation": {"blocks": {}},
                },
                "interpretation_debug": {
                    "prompt_version": (
                        "sv9-flow-brand-interpretation-v1"
                    ),
                    "gate_authority": "veto_only",
                },
            },
            "sv9": {
                "brand3_score": current_score,
                "base_average": 0,
                "reliability_status": "usable",
                "result": {
                    "rubric_version": "baldosas-v3-1",
                    "model": "v3.1",
                    "evaluator_model": "test-model",
                    "components": raw_components,
                },
            },
        },
    }


def _claim_tile_report(report_id: str, created_at: str) -> dict:
    report = _scoring_recovery_report(
        report_id,
        created_at,
        target_state="no",
    )
    quote = "Our mission is to make financial work radically simpler."
    evidence = report["raw"]["flow"]["candidate"]["evidence_pack"][
        "evidence"
    ][0]
    evidence["content"] = quote
    evidence["metadata"]["claim_slot_key"] = "mission.primary"
    raw_mission = report["raw"]["sv9"]["result"]["components"][
        "mission"
    ]
    raw_mission["score"] = 1
    raw_mission["points"] = component_points("mission", 1)
    raw_mission["detected_content"] = quote
    raw_mission["tile_profile"][0].update(
        estado="ok",
        evidencia=quote,
        motivo="",
    )
    public_mission = next(
        component
        for component in report["components"]
        if component["key"] == "mission"
    )
    public_mission["score"] = 1
    public_mission["points"] = component_points("mission", 1)
    public_mission["resumen"] = quote
    public_mission["tile_profile"][0].update(
        estado="ok",
        evidencia=quote,
        motivo="",
    )
    report["blocks"] = [
        {
            "name": "mission",
            "detected": True,
            "content": quote,
            "confidence": "high",
            "coverage_status": "evidence",
            "provenance_source": "llm_and_gate",
            "refs": [
                {
                    "ref": evidence["ref"],
                    "url": evidence["url"],
                    "snippet": quote,
                }
            ],
        }
    ]
    return report


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
        "id": "P1",
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
