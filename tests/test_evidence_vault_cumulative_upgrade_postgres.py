from __future__ import annotations

import hashlib
import os
from typing import Any

import pytest

from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_authority import (
    CanonicalMemoryPromotionCommand,
    build_promotion_event,
    build_reference_resolution,
    plan_canonical_memory_promotion,
    project_promoted_canonical_memory,
    promotion_request_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_tile_contract_registry,
)
from src.services.evidence_vault_canonical_scoring import (
    build_canonical_score_evaluation,
)


_HEAD_MIGRATION = "022_evidence_vault_raw_incremental_planning.sql"
_CLUSTER_ROLES = (
    "b3s_history_vault_runtime_read",
    "b3s_history_vault_provenance_owner",
)
_V1_TABLE_COLUMNS = {
    "workspaces": (
        "id", "slug", "name", "created_at",
    ),
    "brands": (
        "id", "workspace_id", "canonical_domain", "display_name",
        "canonical_url", "first_observed_at", "latest_observed_at",
        "created_at", "updated_at",
    ),
    "evidence_vault_canonical_memory_packets": (
        "id", "brand_id", "packet_fingerprint", "schema_version",
        "brand_identity", "parent_canonical_memory_version",
        "reference_resolution_fingerprint", "reference_resolution",
        "manifest", "candidate_tiles", "authority_state", "authority",
        "production_runtime_effect", "scanner_runtime_effect", "created_at",
    ),
    "evidence_vault_canonical_memory_promotion_events": (
        "id", "brand_id", "event_type", "sequence", "previous_event_id",
        "brand_identity", "candidate_packet_fingerprint",
        "reference_resolution_fingerprint", "promotion_policy_fingerprint",
        "parent_canonical_memory_version", "promoted_canonical_memory_version",
        "decision", "reviewer_id", "reviewed_at", "rationale",
        "schema_version", "idempotency_key_hash", "request_fingerprint",
        "authority", "authority_scope", "production_runtime_effect",
        "scanner_runtime_effect", "created_at",
    ),
    "evidence_vault_canonical_score_evaluations": (
        "id", "brand_id", "promotion_event_id", "canonical_memory_version",
        "evaluation_identity", "score_input_fingerprint",
        "derived_tile_state_fingerprint", "rubric_version",
        "tile_contract_registry_fingerprint", "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint", "schema_version", "score",
        "component_breakdown", "base_average", "magnetism_capped",
        "reused_from_evaluation_identity", "authority", "authority_scope",
        "production_runtime_effect", "scanner_runtime_effect", "created_at",
    ),
}


pytestmark = pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1",
    reason=(
        "destructive PostgreSQL integration requires B3S_TEST_DATABASE_URL "
        "and B3S_ALLOW_SCHEMA_DROP=1"
    ),
)


def test_populated_v1_upgrade_through_cumulative_head_is_lossless() -> None:
    import psycopg
    from psycopg.rows import dict_row

    from src.history.repository import PostgresHistoryRepository, _migration_files

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    migration_files = _migration_files()
    filenames = [filename for filename, _sql_text in migration_files]
    assert filenames[-1] == _HEAD_MIGRATION
    assert [filename.split("_", 1)[0] for filename in filenames] == [
        f"{version:03d}" for version in range(1, 23)
    ]

    fixture = _v1_fixture()
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as admin:
        roles_preexisting = {
            role: admin.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (role,),
            ).fetchone()["exists"]
            for role in _CLUSTER_ROLES
        }
        admin.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            _apply_and_journal_v1_migrations(conn, migration_files)
            _insert_v1_fixture(conn, fixture)
            before = _v1_snapshot(conn)
            journal = conn.execute(
                """
                SELECT version, filename, checksum
                FROM b3s_history.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            assert [row["filename"] for row in journal] == filenames[:13]
            assert len(journal) == 13

        repository = PostgresHistoryRepository(dsn)
        assert repository.migrate() == filenames[13:]

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            after = _v1_snapshot(conn)
            assert after == before
            assert conn.execute(
                "SELECT count(*) AS count FROM b3s_history.schema_migrations"
            ).fetchone()["count"] == 22
            assert [
                row["filename"]
                for row in conn.execute(
                    """
                    SELECT filename
                    FROM b3s_history.schema_migrations
                    ORDER BY version
                    """
                ).fetchall()
            ] == filenames
            assert conn.execute(
                """
                SELECT packet_kind, packet_payload,
                       accepted_memory_candidate_version,
                       candidate_overlay_version
                FROM b3s_history.evidence_vault_canonical_memory_packets
                """
            ).fetchone() == {
                "packet_kind": "canonical_v1",
                "packet_payload": None,
                "accepted_memory_candidate_version": None,
                "candidate_overlay_version": None,
            }
            assert conn.execute(
                """
                SELECT adoption_kind, adopted_by, actor_id, event_payload
                FROM b3s_history.evidence_vault_canonical_memory_promotion_events
                """
            ).fetchone() == {
                "adoption_kind": "human_promotion_v1",
                "adopted_by": None,
                "actor_id": None,
                "event_payload": None,
            }
            assert conn.execute(
                """
                SELECT evaluation_kind, authority_coverage, evaluation_payload
                FROM b3s_history.evidence_vault_canonical_score_evaluations
                """
            ).fetchone() == {
                "evaluation_kind": "canonical_v1",
                "authority_coverage": None,
                "evaluation_payload": None,
            }

        stored_packet = repository.get_evidence_vault_canonical_memory_packet(
            "example.com", fixture["packet"]["candidate_packet_fingerprint"]
        )
        assert stored_packet["packet"] == fixture["packet"]
        assert stored_packet["reference_resolution"] == fixture["resolution"]
        assert stored_packet["authority_state"] == "pending_review"
        assert stored_packet["authority"] is False
        assert repository.get_evidence_vault_canonical_memory(
            "example.com"
        ) == fixture["canonical_memory"]
        assert repository.get_evidence_vault_canonical_score_evaluation(
            "example.com"
        ) == fixture["evaluation"]

        # A fresh repository instance proves the durable journal, not an
        # instance-local migration flag, makes the cumulative rerun idempotent.
        assert PostgresHistoryRepository(dsn).migrate() == []
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            assert _v1_snapshot(conn) == before
            assert conn.execute(
                "SELECT count(*) AS count FROM b3s_history.schema_migrations"
            ).fetchone()["count"] == 22

        _assert_v1_destructive_mutations_are_denied(dsn)
    finally:
        _cleanup_database(dsn, roles_preexisting)


@pytest.mark.parametrize(
    ("contaminant", "expected_message"),
    (
        (
            "wrong_source_kind",
            "relation-review source identity preflight failed",
        ),
        (
            "impossible_plan_lifecycle",
            "operation plan lifecycle preflight failed",
        ),
    ),
)
def test_upgrade_rejects_legacy_integrity_contamination(
    contaminant: str,
    expected_message: str,
) -> None:
    import psycopg
    from psycopg.rows import dict_row

    from src.history.repository import PostgresHistoryRepository, _migration_files

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    migration_files = _migration_files()
    fixture = _v1_fixture()
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as admin:
        roles_preexisting = {
            role: admin.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (role,),
            ).fetchone()["exists"]
            for role in _CLUSTER_ROLES
        }
        admin.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            _apply_and_journal_v1_migrations(conn, migration_files)
            _insert_v1_fixture(conn, fixture)
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            _apply_and_journal_migrations(conn, migration_files[13:20])
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            if contaminant == "wrong_source_kind":
                packet = conn.execute(
                    """
                    SELECT id, brand_id, packet_fingerprint, packet_kind
                    FROM b3s_history.evidence_vault_canonical_memory_packets
                    """
                ).fetchone()
                assert packet["packet_kind"] == "canonical_v1"
                conn.execute(
                    """
                    INSERT INTO
                        b3s_history.evidence_vault_operational_relation_reviews (
                            id, brand_id, source_packet_id,
                            source_packet_fingerprint, relation_id, decision,
                            reviewer_id, rationale, review_request_fingerprint
                        ) VALUES (
                            'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'::uuid,
                            %s, %s, %s, repeat('e', 64), 'accept',
                            'legacy-wrong-kind-reviewer',
                            'must fail the cumulative migration preflight',
                            repeat('f', 64)
                        )
                    """,
                    (
                        packet["brand_id"],
                        packet["id"],
                        packet["packet_fingerprint"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO b3s_history.scan_runs (
                        id, workspace_id, brand_id, source_scan_id, status,
                        pipeline_version, requested_at
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000106'::uuid,
                        %s, %s, 'legacy-impossible-plan', 'captured',
                        'legacy-test-v1', now()
                    )
                    """,
                    (fixture["workspace_id"], fixture["brand_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO b3s_history.evidence_vault_operation_plans (
                        id, workspace_id, brand_id, scan_run_id,
                        observation_hash, operation_plan_fingerprint,
                        mode, status, plan_payload, completed_at
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000107'::uuid,
                        %s, %s,
                        '00000000-0000-0000-0000-000000000106'::uuid,
                        repeat('a', 64), repeat('b', 64), 'baseline',
                        'pending', '{}'::jsonb, now()
                    )
                    """,
                    (fixture["workspace_id"], fixture["brand_id"]),
                )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match=expected_message,
        ):
            PostgresHistoryRepository(dsn).migrate()
    finally:
        _cleanup_database(dsn, roles_preexisting)


def _apply_and_journal_v1_migrations(
    conn: Any,
    migration_files: list[tuple[str, str]],
) -> None:
    conn.execute("CREATE SCHEMA b3s_history")
    conn.execute(
        """
        CREATE TABLE b3s_history.schema_migrations (
            version text PRIMARY KEY,
            filename text NOT NULL,
            checksum text NOT NULL CHECK (length(checksum) = 64),
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    _apply_and_journal_migrations(conn, migration_files[:13])


def _apply_and_journal_migrations(
    conn: Any,
    migration_files: list[tuple[str, str]],
) -> None:
    for filename, sql_text in migration_files:
        conn.execute(sql_text, prepare=False)
        conn.execute(
            """
            INSERT INTO b3s_history.schema_migrations
                (version, filename, checksum)
            VALUES (%s, %s, %s)
            """,
            (
                filename.split("_", 1)[0],
                filename,
                hashlib.sha256(sql_text.encode("utf-8")).hexdigest(),
            ),
        )


def _v1_fixture() -> dict[str, Any]:
    hash_a = "a" * 64
    hash_b = "b" * 64
    hash_c = "c" * 64
    hash_d = "d" * 64
    tiles = [
        build_candidate_tile(tile_id=row["tile_id"])
        for row in build_tile_contract_registry()["tiles"]
    ]
    tiles[0] = build_candidate_tile(
        tile_id="M1",
        basis=[{
            "relation_id": "1" * 64,
            "evidence_id": "9" * 64,
            "source_identity_id": "5" * 64,
            "claim_id": hash_c,
            "polarity": "supports",
            "review_status": "accepted",
            "decision_event_id": "review-cumulative-upgrade",
            "absence_test_contract_id": None,
            "coverage_assessment_id": None,
            "coverage_status": None,
            "tested_scope": None,
            "observed_result": None,
        }],
    )
    packet = build_candidate_packet(
        brand_identity="example.com",
        parent_canonical_memory_version=None,
        candidate_memory_version=hash_a,
        accepted_memory_candidate_version=hash_b,
        reviewed_memory_candidate_version=hash_c,
        review_packet_set_fingerprint=hash_d,
        aggregation_policy_fingerprint=(
            canonical_aggregation_policy_fingerprint()
        ),
        candidate_tiles=tiles,
        coverage_summary={"fixture": "populated-v1-cumulative-upgrade"},
    )
    references = {
        field: packet["manifest"][field]
        for field in (
            "candidate_memory_version",
            "accepted_memory_candidate_version",
            "reviewed_memory_candidate_version",
            "review_packet_set_fingerprint",
            "rubric_version",
            "tile_contract_registry_fingerprint",
            "reducer_policy_fingerprint",
            "aggregation_policy_fingerprint",
        )
    }
    resolution = build_reference_resolution(packet, references)
    plan = plan_canonical_memory_promotion(
        packet,
        resolved_references=references,
        current_memory=None,
    )
    reviewed_at = "2026-08-09T06:00:00+00:00"
    rationale = "Reproducible populated v1 cumulative-upgrade fixture."
    command = CanonicalMemoryPromotionCommand(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=None,
        reviewer_id="cumulative-upgrade-reviewer",
        reviewed_at=reviewed_at,
        rationale=rationale,
        idempotency_key_hash="e" * 64,
        request_fingerprint=promotion_request_fingerprint(
            candidate_packet_fingerprint=packet[
                "candidate_packet_fingerprint"
            ],
            parent_canonical_memory_version=None,
            reviewer_id="cumulative-upgrade-reviewer",
            reviewed_at=reviewed_at,
            rationale=rationale,
        ),
    )
    event = build_promotion_event(
        plan,
        command,
        event_id="00000000-0000-0000-0000-000000000104",
        sequence=1,
        previous_event_id=None,
    )
    canonical_memory = project_promoted_canonical_memory(plan, event)
    evaluation = build_canonical_score_evaluation(
        canonical_memory,
        created_at="2026-08-09T06:01:00+00:00",
    )
    return {
        "workspace_id": "00000000-0000-0000-0000-000000000101",
        "brand_id": "00000000-0000-0000-0000-000000000102",
        "packet_id": "00000000-0000-0000-0000-000000000103",
        "score_id": "00000000-0000-0000-0000-000000000105",
        "packet": packet,
        "resolution": resolution,
        "event": event,
        "canonical_memory": canonical_memory,
        "evaluation": evaluation,
    }


def _insert_v1_fixture(conn: Any, fixture: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    packet = fixture["packet"]
    manifest = packet["manifest"]
    resolution = fixture["resolution"]
    event = fixture["event"]
    evaluation = fixture["evaluation"]
    conn.execute(
        """
        INSERT INTO b3s_history.workspaces (id, slug, name, created_at)
        VALUES (%s, 'b3s', 'B3S cumulative upgrade', %s)
        """,
        (fixture["workspace_id"], "2026-08-09T05:59:00+00:00"),
    )
    conn.execute(
        """
        INSERT INTO b3s_history.brands (
            id, workspace_id, canonical_domain, display_name, canonical_url,
            first_observed_at, latest_observed_at, created_at, updated_at
        ) VALUES (%s, %s, 'example.com', 'Example', 'https://example.com',
                  %s, %s, %s, %s)
        """,
        (
            fixture["brand_id"],
            fixture["workspace_id"],
            "2026-08-09T05:59:10+00:00",
            "2026-08-09T05:59:20+00:00",
            "2026-08-09T05:59:30+00:00",
            "2026-08-09T05:59:40+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO b3s_history.evidence_vault_canonical_memory_packets (
            id, brand_id, packet_fingerprint, schema_version, brand_identity,
            parent_canonical_memory_version, reference_resolution_fingerprint,
            reference_resolution, manifest, candidate_tiles, authority_state,
            authority, production_runtime_effect, scanner_runtime_effect,
            created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  'pending_review', false, false, false, %s)
        """,
        (
            fixture["packet_id"], fixture["brand_id"],
            packet["candidate_packet_fingerprint"], manifest["schema_version"],
            manifest["brand_identity"],
            manifest["parent_canonical_memory_version"],
            resolution["reference_resolution_fingerprint"], Jsonb(resolution),
            Jsonb(manifest), Jsonb(packet["candidate_tiles"]),
            "2026-08-09T06:00:10+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO b3s_history.evidence_vault_canonical_memory_promotion_events (
            id, brand_id, event_type, sequence, previous_event_id,
            brand_identity, candidate_packet_fingerprint,
            reference_resolution_fingerprint, promotion_policy_fingerprint,
            parent_canonical_memory_version, promoted_canonical_memory_version,
            decision, reviewer_id, reviewed_at, rationale, schema_version,
            idempotency_key_hash, request_fingerprint, authority,
            authority_scope, production_runtime_effect, scanner_runtime_effect,
            created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, true, %s, false, false, %s)
        """,
        (
            event["event_id"], fixture["brand_id"], event["event_type"],
            event["sequence"], event["previous_event_id"],
            event["brand_identity"], event["candidate_packet_fingerprint"],
            event["reference_resolution_fingerprint"],
            event["promotion_policy_fingerprint"],
            event["parent_canonical_memory_version"],
            event["promoted_canonical_memory_version"], event["decision"],
            event["reviewer_id"], event["reviewed_at"], event["rationale"],
            event["schema_version"], event["idempotency_key_hash"],
            event["request_fingerprint"], event["authority_scope"],
            "2026-08-09T06:00:20+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO b3s_history.evidence_vault_canonical_score_evaluations (
            id, brand_id, promotion_event_id, canonical_memory_version,
            evaluation_identity, score_input_fingerprint,
            derived_tile_state_fingerprint, rubric_version,
            tile_contract_registry_fingerprint, reducer_policy_fingerprint,
            aggregation_policy_fingerprint, schema_version, score,
            component_breakdown, base_average, magnetism_capped,
            reused_from_evaluation_identity, authority, authority_scope,
            production_runtime_effect, scanner_runtime_effect, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, true, %s, false, false, %s)
        """,
        (
            fixture["score_id"], fixture["brand_id"],
            evaluation["promotion_event_id"],
            evaluation["canonical_memory_version"],
            evaluation["evaluation_identity"],
            evaluation["score_input_fingerprint"],
            evaluation["derived_tile_state_fingerprint"],
            evaluation["rubric_version"],
            evaluation["tile_contract_registry_fingerprint"],
            evaluation["reducer_policy_fingerprint"],
            evaluation["aggregation_policy_fingerprint"],
            evaluation["schema_version"], evaluation["score"],
            Jsonb(evaluation["component_breakdown"]),
            evaluation["base_average"], evaluation["magnetism_capped"],
            evaluation["reused_from_evaluation_identity"],
            evaluation["authority_scope"], evaluation["created_at"],
        ),
    )


def _v1_snapshot(conn: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"rows": {}, "counts": {}}
    for table, columns in _V1_TABLE_COLUMNS.items():
        column_list = ", ".join(columns)
        rows = conn.execute(
            f"SELECT {column_list} FROM b3s_history.{table} ORDER BY id"
        ).fetchall()
        snapshot["rows"][table] = rows
        snapshot["counts"][table] = len(rows)
    assert snapshot["counts"] == {
        table: 1 for table in _V1_TABLE_COLUMNS
    }
    return snapshot


def _assert_v1_destructive_mutations_are_denied(dsn: str) -> None:
    import psycopg

    statements = (
        (
            "UPDATE b3s_history.evidence_vault_canonical_memory_packets "
            "SET manifest = manifest",
            "canonical-memory packets are append-only",
        ),
        (
            "DELETE FROM "
            "b3s_history.evidence_vault_canonical_memory_promotion_events",
            "canonical-memory promotions are append-only",
        ),
        (
            "UPDATE b3s_history.evidence_vault_canonical_score_evaluations "
            "SET score = score",
            "canonical score evaluations are append-only",
        ),
    )
    for statement, message in statements:
        with psycopg.connect(dsn) as conn:
            with pytest.raises(psycopg.errors.RaiseException, match=message):
                conn.execute(statement)


def _cleanup_database(
    dsn: str,
    roles_preexisting: dict[str, bool],
) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        # 019/020 use cluster-global fixed roles. Preserve roles that existed
        # before this test; remove only roles this disposable run created.
        for role in _CLUSTER_ROLES:
            exists = admin.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (role,),
            ).fetchone()[0]
            if exists and not roles_preexisting[role]:
                admin.execute(
                    sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(
                        sql.Identifier(role)
                    )
                )
                admin.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role))
                )
                admin.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                )
