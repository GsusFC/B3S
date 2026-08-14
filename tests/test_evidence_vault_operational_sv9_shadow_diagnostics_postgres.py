from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)


def test_sanitized_view_is_queryable_without_raw_ledger_access() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.errors import InsufficientPrivilege

    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        pytest.fail("B3S_TEST_ALLOW_SCHEMA_DROP=1 is required")
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    role = f"b3s_sv9_diag_test_{uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        if admin.execute(
            "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'b3s_pr71_app_runtime'"
        ).fetchone():
            admin.execute("DROP OWNED BY b3s_pr71_app_runtime")
            admin.execute("DROP ROLE b3s_pr71_app_runtime")
        admin.execute("CREATE ROLE b3s_pr71_app_runtime NOLOGIN")
        admin.execute(
            "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO b3s_pr71_app_runtime"
        )
    try:
        PostgresHistoryRepository(dsn).migrate()
        with psycopg.connect(dsn, autocommit=True) as admin:
            columns = admin.execute(
                """
                SELECT attributes.attname
                FROM pg_catalog.pg_attribute AS attributes
                WHERE attributes.attrelid =
                    'b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1'::regclass
                  AND attributes.attnum > 0
                  AND NOT attributes.attisdropped
                ORDER BY attributes.attnum
                """
            ).fetchall()
            names = [row[0] for row in columns]
            assert "brand_id" in names
            assert "candidate_semantic_tiles" not in names
            assert "assessment_output" not in names
            assert "verification_requirements" not in names
            assert "operational_packet_id" not in names
            assert "source_packet_id" not in names
            assert admin.execute(
                """
                SELECT owners.rolname
                FROM pg_catalog.pg_class AS relations
                JOIN pg_catalog.pg_roles AS owners ON owners.oid = relations.relowner
                WHERE relations.oid =
                    'b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1'::regclass
                """
            ).fetchone()[0] == "b3s_history_vault_provenance_owner"
            for principal in (
                "b3s_history_vault_runtime_read",
                "b3s_history_vault_sv9_shadow_writer",
                "b3s_pr71_app_runtime",
            ):
                assert admin.execute(
                    "SELECT pg_catalog.has_table_privilege(%s, %s, 'SELECT')",
                    (
                        principal,
                        "b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1",
                    ),
                ).fetchone()[0] is False
            assert admin.execute(
                "SELECT pg_catalog.has_table_privilege(%s, %s, 'SELECT')",
                (
                    "b3s_pr71_app_runtime",
                    "b3s_history.evidence_vault_operational_sv9_shadow_assessments",
                ),
            ).fetchone()[0] is False
            admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            admin.execute(
                sql.SQL("GRANT USAGE ON SCHEMA b3s_history TO {}").format(
                    sql.Identifier(role)
                )
            )
            admin.execute(
                sql.SQL(
                    "GRANT SELECT ON b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1 TO {}"
                ).format(sql.Identifier(role))
            )

        with psycopg.connect(dsn) as reader:
            reader.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            assert reader.execute(
                "SELECT count(*) FROM b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1"
            ).fetchone()[0] == 0

        with psycopg.connect(dsn) as reader:
            reader.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            with pytest.raises(InsufficientPrivilege):
                reader.execute(
                    "SELECT count(*) FROM b3s_history.evidence_vault_operational_sv9_shadow_assessments"
                )
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            if admin.execute(
                "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone():
                admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            if admin.execute(
                "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'b3s_pr71_app_runtime'"
            ).fetchone():
                admin.execute(
                    "ALTER DEFAULT PRIVILEGES REVOKE SELECT ON TABLES FROM b3s_pr71_app_runtime"
                )
                admin.execute("DROP OWNED BY b3s_pr71_app_runtime")
                admin.execute("DROP ROLE b3s_pr71_app_runtime")
