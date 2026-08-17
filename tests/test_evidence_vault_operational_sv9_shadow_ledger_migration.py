"""Static contract for migrations 025–027's trusted append-only SV9 shadow ledger."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest


_MIGRATION = Path(
    "src/history/migrations/025_evidence_vault_operational_sv9_shadow_assessments.sql"
)
_HARDENING_MIGRATION = Path(
    "src/history/migrations/026_evidence_vault_operational_sv9_shadow_hardening.sql"
)
_WRITER_MIGRATION = Path(
    "src/history/migrations/027_evidence_vault_operational_sv9_shadow_writer.sql"
)
_DIAGNOSTIC_MIGRATION = Path(
    "src/history/migrations/028_evidence_vault_operational_sv9_shadow_diagnostics.sql"
)


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _hardening_sql() -> str:
    return _HARDENING_MIGRATION.read_text(encoding="utf-8")


def test_sv9_shadow_writer_precedes_the_diagnostic_only_head() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql_text in _migration_files()]

    writer_position = filenames.index(_WRITER_MIGRATION.name)
    diagnostic_position = filenames.index(_DIAGNOSTIC_MIGRATION.name)
    assert diagnostic_position == writer_position + 1
    assert filenames.count(_MIGRATION.name) == 1
    assert filenames.count(_HARDENING_MIGRATION.name) == 1
    assert filenames.count(_WRITER_MIGRATION.name) == 1


def test_sv9_shadow_ledger_binds_exact_operational_source_packets() -> None:
    sql = _sql()

    assert (
        "FOREIGN KEY (\n        brand_id, operational_packet_id, "
        "operational_packet_fingerprint," in sql
    )
    assert (
        "FOREIGN KEY (\n        brand_id, source_packet_id, "
        "source_candidate_packet_fingerprint," in sql
    )
    assert (
        "REFERENCES b3s_history.evidence_vault_canonical_memory_packets (\n"
        "        brand_id, id, packet_fingerprint, packet_kind" in sql
    )
    assert "operational_source_v2', 'operational_reviewed_v2" in sql
    assert "canonical_v1` is never a valid source" in sql
    assert "source_payload ->> 'candidate_packet_fingerprint'" in sql
    assert "operational_payload ->> 'source_candidate_packet_fingerprint'" in sql
    assert "operational_payload ->> 'candidate_overlay_version'" in sql
    assert "expected_parent_canonical_memory_version" in sql


def test_sv9_shadow_ledger_is_strict_conservative_and_append_only() -> None:
    sql = _sql()

    assert "jsonb_array_length(tiles) = 80" in sql
    assert "jsonb_array_length(requirements -> 'tiles') = 80" in sql
    assert "'ok', 'no', 'sin_evidencia', 'contradiction'" in sql
    assert "'pending', 'disputed', 'stale', 'unverifiable'" in sql
    assert "'verified'" not in sql
    assert "'available'," in sql
    assert "'stale_candidate_parent'," in sql
    assert "'contradiction_requires_semantic_reassessment'" in sql
    assert "BEFORE INSERT ON b3s_history.evidence_vault_operational_sv9_shadow_assessments" in sql
    assert "BEFORE UPDATE OR DELETE ON b3s_history.evidence_vault_operational_sv9_shadow_assessments" in sql
    assert "BEFORE TRUNCATE ON b3s_history.evidence_vault_operational_sv9_shadow_assessments" in sql
    assert "SECURITY DEFINER" not in sql
    assert (
        "REVOKE ALL ON b3s_history.evidence_vault_operational_sv9_shadow_assessments\n"
        "FROM PUBLIC" in sql
    )
    assert "b3s_pr71_scanner_ingest" in sql
    assert "has_table_privilege" in sql
    assert "has_any_column_privilege" in sql


def test_adr_limits_persistence_to_the_non_authoritative_ledger() -> None:
    adr = Path("docs/evidence_vault_sv9_assessment_adr_v3.md").read_text(
        encoding="utf-8"
    )

    assert "migración 025 solo define un ledger append-only no autoritativo" in adr
    assert "La migración 027 añade únicamente el writer interno" in adr
    assert "no añade read path, API pública, worker ni cutover" in adr


def test_sv9_shadow_hardening_uses_exact_null_identity_and_private_functions() -> None:
    sql = _hardening_sql()

    assert "COALESCE(expected_parent_canonical_memory_version, '')" in sql
    assert "NULLS NOT DISTINCT" not in sql
    assert "expected_parent_canonical_memory_version" in sql
    for function in (
        "evidence_vault_sv9_shadow_semantic_tiles_are_valid(jsonb)",
        "evidence_vault_sv9_shadow_verification_is_valid(jsonb)",
        "evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(jsonb)",
        "validate_vault_operational_sv9_shadow_assessment_insert()",
        "reject_vault_operational_sv9_shadow_assessment_mutation()",
    ):
        assert function in sql
        assert f"{function}\nFROM PUBLIC" in sql


def test_sv9_shadow_hardening_derives_exact_requirements_and_validates_output_shape() -> None:
    sql = _hardening_sql()

    assert "WHEN 'C7' THEN 'owned_web_plus_external_social'" in sql
    assert "WHEN 'C8' THEN 'human_required'" in sql
    assert "IS NOT DISTINCT FROM 'contradiction' THEN 'disputed'" in sql
    assert "IS NOT DISTINCT FROM 'superseded' THEN 'stale'" in sql
    assert "IS NOT DISTINCT FROM 'rejected' THEN 'unverifiable'" in sql
    assert "ELSE 'pending'" in sql
    assert "ARRAY['pending', 'verified', 'disputed', 'stale', 'unverifiable']" in sql
    assert "IS NOT DISTINCT FROM 'verified'" not in sql
    assert "counts' ->> 'verified') IS NOT DISTINCT FROM '0'" in sql
    assert "evidence_vault_sv9_shadow_assessment_output_is_structurally_valid" in sql
    assert "component_breakdown" in sql
    assert "NEW.assessment_output -> 'tiles') IS DISTINCT FROM NEW.candidate_semantic_tiles" in sql
    assert "Reassert every persisted/output fingerprint at the admission boundary" in sql
    assert "validate_sv9_assessment_output()" in sql


def test_sv9_shadow_writer_capability_is_narrow_and_owned_by_provenance() -> None:
    sql = _WRITER_MIGRATION.read_text(encoding="utf-8")

    assert "CREATE ROLE b3s_history_vault_sv9_shadow_writer" in sql
    assert "NOLOGIN NOINHERIT" in sql
    assert "ALTER TABLE b3s_history.evidence_vault_operational_sv9_shadow_assessments" in sql
    assert "OWNER TO b3s_history_vault_provenance_owner" in sql
    for function in (
        "evidence_vault_sv9_shadow_semantic_tiles_are_valid(jsonb)",
        "evidence_vault_sv9_shadow_verification_is_valid(jsonb)",
        "evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(jsonb)",
        "validate_vault_operational_sv9_shadow_assessment_insert()",
        "reject_vault_operational_sv9_shadow_assessment_mutation()",
    ):
        assert function in sql
    assert "GRANT INSERT ON b3s_history.evidence_vault_operational_sv9_shadow_assessments" in sql
    assert "b3s_history.schema_migrations" in sql
    assert "b3s_history.workspaces" in sql
    assert "FROM b3s_history_vault_runtime_read" in sql
    assert "FROM b3s_pr71_scanner_ingest" in sql
    assert "FROM pg_catalog.pg_auth_members AS memberships" in sql
    assert "memberships.roleid = writer_oid" in sql
    assert "a role administrator must revoke it before migration" in sql
    assert "REVOKE b3s_history_vault_sv9_shadow_writer FROM" not in sql
    assert "UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER" in sql


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1",
    reason="destructive PostgreSQL integration requires B3S_TEST_DATABASE_URL and B3S_ALLOW_SCHEMA_DROP=1",
)
def test_postgres_sv9_shadow_hardening_revokes_public_execute_and_rejects_bad_output() -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository
    from src.sv9.assessment_kernel import build_sv9_assessment
    from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        for role in (
            "b3s_history_vault_runtime_read",
            "b3s_history_vault_provenance_owner",
            "b3s_history_vault_sv9_shadow_writer",
        ):
            if conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (role,),
            ).fetchone()[0]:
                conn.execute(f"REASSIGN OWNED BY {role} TO CURRENT_USER")
                conn.execute(f"DROP OWNED BY {role}")
                conn.execute(f"DROP ROLE {role}")
    try:
        applied = PostgresHistoryRepository(dsn).migrate()
        assert _WRITER_MIGRATION.name in applied
        assert applied[-1] == _DIAGNOSTIC_MIGRATION.name
        rows = [
            {
                "component_key": component_key,
                "tile_id": str(tile["id"]),
                "tile_key": f"{component_key}.{tile['id']}",
                "assessment_state": "ok",
            }
            for component_key in PRESENTATION_ORDER
            for tile in COMPONENTS[component_key]["tiles"]
        ]
        output = build_sv9_assessment(rows)
        requirements = {
            "schema_version": "evidence-vault-operational-verification-requirements-v1",
            "tile_count": 80,
            "tiles": [
                {
                    "tile_id": row["tile_id"],
                    "verification_requirement": (
                        "owned_web_plus_external_social"
                        if row["tile_id"] == "C7"
                        else "human_required" if row["tile_id"] == "C8" else "ordinary"
                    ),
                    "verification_state": "pending",
                }
                for row in rows
            ],
            "counts": {
                "pending": 80,
                "verified": 0,
                "disputed": 0,
                "stale": 0,
                "unverifiable": 0,
            },
        }
        malformed_score = deepcopy(output)
        malformed_score["sv9_score"] = 1.5
        malformed_tile = deepcopy(output)
        malformed_tile["tiles"][0]["tile_id"] = 7
        malformed_counts = deepcopy(requirements)
        malformed_counts["counts"]["pending"] = 79
        forged_verified = deepcopy(requirements)
        forged_verified["tiles"][0]["verification_state"] = "verified"
        forged_verified["counts"]["pending"] = 79
        forged_verified["counts"]["verified"] = 1
        with psycopg.connect(dsn) as conn:
            writer = "b3s_history_vault_sv9_shadow_writer"
            attributes = conn.execute(
                """
                SELECT rolcanlogin, rolinherit, rolsuper, rolcreaterole,
                       rolcreatedb, rolreplication, rolbypassrls
                FROM pg_roles WHERE rolname = %s
                """,
                (writer,),
            ).fetchone()
            assert attributes == (False, False, False, False, False, False, False)
            assert conn.execute(
                """
                SELECT roles.rolname
                FROM pg_class AS relations
                JOIN pg_roles AS roles ON roles.oid = relations.relowner
                WHERE relations.oid =
                    'b3s_history.evidence_vault_operational_sv9_shadow_assessments'::regclass
                """
            ).fetchone()[0] == "b3s_history_vault_provenance_owner"
            assert conn.execute(
                """
                SELECT has_table_privilege(%s,
                    'b3s_history.evidence_vault_operational_sv9_shadow_assessments',
                    'SELECT,INSERT')
                   AND NOT has_table_privilege(%s,
                    'b3s_history.evidence_vault_operational_sv9_shadow_assessments',
                    'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                """,
                (writer, writer),
            ).fetchone()[0]
            for forbidden_role in (
                "b3s_history_vault_runtime_read",
                "b3s_pr71_scanner_ingest",
            ):
                exists = conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                    (forbidden_role,),
                ).fetchone()[0]
                if exists:
                    assert not conn.execute(
                        """
                        SELECT has_table_privilege(%s,
                            'b3s_history.evidence_vault_operational_sv9_shadow_assessments',
                            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                        """,
                        (forbidden_role,),
                    ).fetchone()[0]
            assert conn.execute(
                """
                SELECT NOT EXISTS (
                    SELECT 1 FROM pg_roles AS principals
                    WHERE principals.rolname IN (
                        'b3s_history_vault_runtime_read',
                        'b3s_pr71_scanner_ingest'
                    ) AND pg_has_role(
                        principals.oid,
                        'b3s_history_vault_sv9_shadow_writer'::regrole,
                        'MEMBER'
                    )
                )
                """
            ).fetchone()[0]
            for signature in (
                "b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(jsonb)",
                "b3s_history.evidence_vault_sv9_shadow_verification_is_valid(jsonb)",
                "b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(jsonb)",
                "b3s_history.validate_vault_operational_sv9_shadow_assessment_insert()",
                "b3s_history.reject_vault_operational_sv9_shadow_assessment_mutation()",
            ):
                assert not conn.execute(
                    "SELECT has_function_privilege('public', %s::regprocedure, 'EXECUTE')",
                    (signature,),
                ).fetchone()[0]
            assert conn.execute(
                """
                SELECT b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(
                    %s::jsonb
                )
                """,
                (json.dumps(output),),
            ).fetchone()[0]
            assert not conn.execute(
                """
                SELECT b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(
                    %s::jsonb
                )
                """,
                (json.dumps(malformed_score),),
            ).fetchone()[0]
            assert not conn.execute(
                """
                SELECT b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(
                    %s::jsonb
                )
                """,
                (json.dumps(malformed_tile),),
            ).fetchone()[0]
            assert conn.execute(
                """
                SELECT b3s_history.evidence_vault_sv9_shadow_verification_is_valid(
                    %s::jsonb
                )
                """,
                (json.dumps(requirements),),
            ).fetchone()[0]
            assert not conn.execute(
                """
                SELECT b3s_history.evidence_vault_sv9_shadow_verification_is_valid(
                    %s::jsonb
                )
                """,
                (json.dumps(malformed_counts),),
            ).fetchone()[0]
            assert not conn.execute(
                """
                SELECT b3s_history.evidence_vault_sv9_shadow_verification_is_valid(
                    %s::jsonb
                )
                """,
                (json.dumps(forged_verified),),
            ).fetchone()[0]
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
