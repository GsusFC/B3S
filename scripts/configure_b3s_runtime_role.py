#!/usr/bin/env python3
"""Configure one existing least-privilege B3S web/report runtime role."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "b3s_history"
MIGRATION_JOURNAL = "schema_migrations"
EXPECTED_HEAD_VERSION = "021"
WATERMARK_TABLE = "evidence_vault_capture_watermark_events"
RAW_MIGRATION_019_RELATIONS = frozenset(
    {
        "evidence_vault_raw_acquisition_receipts",
        "evidence_vault_raw_evidence_bindings",
        "evidence_vault_verified_c7_lineage_bindings",
        "evidence_vault_verified_c7_lineage_members",
        "evidence_vault_raw_provenance_disposition_events",
    }
)
EXPECTED_APPLICATION_TABLES = frozenset(
    {
        "acquisition_attempts",
        "artifacts",
        "block_evidence_links",
        "block_interpretations",
        "brand_canonical_selections",
        "brands",
        "capture_fingerprints",
        "captures",
        "component_evaluations",
        "evaluation_comparisons",
        "evaluation_runs",
        "evidence_claim_reconciliation_events",
        "evidence_claim_tile_ledger_states",
        "evidence_claim_tile_mapping_observations",
        "evidence_claim_tile_mapping_series",
        "evidence_claim_tile_mappings",
        "evidence_claim_tile_review_events",
        "evidence_claim_tile_review_packets",
        "evidence_ledger_shadow_entries",
        "evidence_ledger_shadow_observations",
        "evidence_ledger_shadow_states",
        "evidence_memory_adjudication_events",
        "evidence_records",
        "evidence_scoring_recovery_review_events",
        "evidence_scoring_recovery_supplement_packets",
        "evidence_vault_canonical_memory_packets",
        "evidence_vault_canonical_memory_promotion_events",
        "evidence_vault_canonical_score_evaluations",
        "evidence_vault_capture_watermark_events",
        "evidence_vault_operation_plans",
        "evidence_vault_operational_relation_reviews",
        "evidence_vault_operational_source_capture_lineage_bindings",
        "evidence_vault_operational_source_capture_lineage_members",
        "report_snapshots",
        "scan_runs",
        "tile_verdicts",
        "workspaces",
    }
)
EXPECTED_READ_ONLY_VIEWS = frozenset({"brand_current_state", "brand_history"})
EXPECTED_HEAD_RELATIONS = (
    EXPECTED_APPLICATION_TABLES
    | EXPECTED_READ_ONLY_VIEWS
    | RAW_MIGRATION_019_RELATIONS
    | {MIGRATION_JOURNAL}
)

class RuntimeRoleConfigurationError(RuntimeError):
    """The requested role cannot safely receive the runtime privilege contract."""


@dataclass(frozen=True)
class Relation:
    oid: int
    name: str
    kind: str
    columns: tuple[str, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        required=True,
        help="Existing PostgreSQL LOGIN role to configure (never a DSN or password).",
    )
    parser.add_argument(
        "--database-name",
        help="Database receiving CONNECT; must be the database selected by the migration URL.",
    )
    return parser.parse_args(argv)


def _migration_contract() -> tuple[
    list[tuple[str, str, str, str]],
    Callable[[Iterable[tuple[str, str, str, str]], Iterable[Mapping[str, Any]]], None],
    Callable[..., int],
]:
    from src.history.repository import (
        _advisory_lock_key,
        _migration_manifest,
        _require_exact_migration_manifest,
    )

    return _migration_manifest(), _require_exact_migration_manifest, _advisory_lock_key


def _verify_exact_head(
    conn: Any,
    manifest: list[tuple[str, str, str, str]],
    verifier: Callable[[Iterable[tuple[str, str, str, str]], Iterable[Mapping[str, Any]]], None],
) -> str:
    if not manifest or manifest[-1][0] != EXPECTED_HEAD_VERSION:
        raise RuntimeRoleConfigurationError("this runtime grant tool requires packaged migration head 021")
    rows = conn.execute(
        sql.SQL(
            """
            SELECT version, filename, checksum
            FROM {}.{}
            ORDER BY version
            """
        ).format(sql.Identifier(SCHEMA), sql.Identifier(MIGRATION_JOURNAL))
    ).fetchall()
    verifier(manifest, rows)
    return manifest[-1][1]


def _require_safe_target_role(conn: Any, role: str) -> int:
    row = conn.execute(
        """
        SELECT oid, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = %s
        """,
        (role,),
    ).fetchone()
    if row is None:
        raise RuntimeRoleConfigurationError("target role does not exist")
    if not bool(row["rolcanlogin"]):
        raise RuntimeRoleConfigurationError("target role must be LOGIN")
    unsafe_flags = (
        "rolsuper",
        "rolcreaterole",
        "rolcreatedb",
        "rolreplication",
        "rolbypassrls",
    )
    if any(bool(row[name]) for name in unsafe_flags):
        raise RuntimeRoleConfigurationError("target role has forbidden role attributes")

    role_oid = int(row["oid"])
    membership = conn.execute(
        """
        SELECT 1
        FROM pg_catalog.pg_auth_members
        WHERE member = %s OR roleid = %s
        LIMIT 1
        """,
        (role_oid, role_oid),
    ).fetchone()
    if membership is not None:
        raise RuntimeRoleConfigurationError("target role must not participate in role memberships")

    owned = conn.execute(
        """
        SELECT 1
        FROM pg_catalog.pg_shdepend
        WHERE refclassid = 'pg_catalog.pg_authid'::regclass
          AND refobjid = %s
          AND deptype = 'o'
        LIMIT 1
        """,
        (role_oid,),
    ).fetchone()
    if owned is not None:
        raise RuntimeRoleConfigurationError("target role must not own database objects")
    return role_oid


def _resolve_database_name(conn: Any, requested: str | None) -> str:
    row = conn.execute("SELECT current_database() AS database_name").fetchone()
    current = str(row["database_name"])
    requested_name = str(requested or "").strip()
    if requested_name and requested_name != current:
        raise RuntimeRoleConfigurationError(
            "--database-name must match the database selected by B3S_MIGRATION_DATABASE_URL"
        )
    return requested_name or current


def _load_relations(conn: Any) -> list[Relation]:
    rows = conn.execute(
        """
        SELECT relations.oid,
               relations.relname,
               relations.relkind,
               COALESCE(
                   array_agg(attributes.attname ORDER BY attributes.attnum)
                       FILTER (
                           WHERE attributes.attnum > 0
                             AND NOT attributes.attisdropped
                       ),
                   ARRAY[]::name[]
               ) AS columns
        FROM pg_catalog.pg_class AS relations
        JOIN pg_catalog.pg_namespace AS schemas
          ON schemas.oid = relations.relnamespace
        LEFT JOIN pg_catalog.pg_attribute AS attributes
          ON attributes.attrelid = relations.oid
        WHERE schemas.nspname = %s
          AND relations.relkind IN ('r', 'p', 'v', 'm', 'f')
        GROUP BY relations.oid, relations.relname, relations.relkind
        ORDER BY relations.relname
        """,
        (SCHEMA,),
    ).fetchall()
    relations = [
        Relation(
            oid=int(row["oid"]),
            name=str(row["relname"]),
            kind=str(row["relkind"]),
            columns=tuple(str(column) for column in (row["columns"] or ())),
        )
        for row in rows
    ]
    by_name = {relation.name: relation for relation in relations}
    actual_names = set(by_name)
    if actual_names != EXPECTED_HEAD_RELATIONS:
        raise RuntimeRoleConfigurationError(
            "migration-head relation set is not exact"
        )
    expected_tables = (
        EXPECTED_APPLICATION_TABLES
        | RAW_MIGRATION_019_RELATIONS
        | {MIGRATION_JOURNAL}
    )
    if any(by_name[name].kind != "r" for name in expected_tables) or any(
        by_name[name].kind != "v" for name in EXPECTED_READ_ONLY_VIEWS
    ):
        raise RuntimeRoleConfigurationError(
            "migration-head relation kinds are not exact"
        )
    return relations


def _load_owned_sequences(conn: Any, app_table_names: set[str]) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT sequences.relname AS sequence_name,
                        tables.relname AS table_name
        FROM pg_catalog.pg_class AS sequences
        JOIN pg_catalog.pg_namespace AS sequence_schemas
          ON sequence_schemas.oid = sequences.relnamespace
        JOIN pg_catalog.pg_depend AS dependencies
          ON dependencies.classid = 'pg_catalog.pg_class'::regclass
         AND dependencies.objid = sequences.oid
         AND dependencies.refclassid = 'pg_catalog.pg_class'::regclass
         AND dependencies.deptype IN ('a', 'i')
        JOIN pg_catalog.pg_class AS tables
          ON tables.oid = dependencies.refobjid
        JOIN pg_catalog.pg_namespace AS table_schemas
          ON table_schemas.oid = tables.relnamespace
        WHERE sequence_schemas.nspname = %s
          AND table_schemas.nspname = %s
          AND sequences.relkind = 'S'
        ORDER BY sequences.relname, tables.relname
        """,
        (SCHEMA, SCHEMA),
    ).fetchall()
    return sorted(
        {
            str(row["sequence_name"])
            for row in rows
            if str(row["table_name"]) in app_table_names
        }
    )


def _relation_list(names: Iterable[str]) -> sql.Composed:
    return sql.SQL(", ").join(
        sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(name))
        for name in sorted(names)
    )


def _reset_and_grant(
    conn: Any,
    *,
    role: str,
    database_name: str,
    relations: list[Relation],
    app_table_names: set[str],
    view_names: set[str],
    sequence_names: list[str],
) -> None:
    role_identifier = sql.Identifier(role)
    schema_identifier = sql.Identifier(SCHEMA)
    database_identifier = sql.Identifier(database_name)

    # Remove direct grants first. Table-level REVOKE does not remove column ACLs,
    # so every relation column is reset explicitly as part of the same transaction.
    conn.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
            database_identifier,
            role_identifier,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(
            schema_identifier,
            role_identifier,
        )
    )
    conn.execute(
        sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
            schema_identifier,
            role_identifier,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}").format(
            schema_identifier,
            role_identifier,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(
            schema_identifier,
            role_identifier,
        )
    )
    # A stale EXECUTE grant on a raw SECURITY DEFINER reader would bypass the
    # relation ACL boundary. Web/report runtimes receive no direct function ACLs.
    conn.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA {} FROM {}").format(
            schema_identifier,
            role_identifier,
        )
    )
    for relation in relations:
        if not relation.columns:
            continue
        conn.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ({}) ON TABLE {}.{} FROM {}").format(
                sql.SQL(", ").join(sql.Identifier(column) for column in relation.columns),
                schema_identifier,
                sql.Identifier(relation.name),
                role_identifier,
            )
        )

    conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database_identifier, role_identifier)
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_identifier, role_identifier)
    )
    if app_table_names:
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}").format(
                _relation_list(app_table_names),
                role_identifier,
            )
        )
    if sequence_names:
        conn.execute(
            sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(
                _relation_list(sequence_names),
                role_identifier,
            )
        )

    read_only_names = set(view_names) | {MIGRATION_JOURNAL}
    conn.execute(
        sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
            _relation_list(read_only_names),
            role_identifier,
        )
    )
    conn.execute(
        sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE {}.{} FROM {}").format(
            schema_identifier,
            sql.Identifier(MIGRATION_JOURNAL),
            role_identifier,
        )
    )
    # Keep the five raw migration-019 relations visibly denied even after the
    # schema-wide reset, so future edits cannot accidentally fold them into DML.
    conn.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {} FROM {}").format(
            _relation_list(RAW_MIGRATION_019_RELATIONS),
            role_identifier,
        )
    )


def _verify_effective_privileges(
    conn: Any,
    *,
    role: str,
    database_name: str,
    relations: list[Relation],
    app_table_names: set[str],
    view_names: set[str],
    sequence_names: list[str],
) -> None:
    boundary = conn.execute(
        """
        SELECT pg_catalog.has_database_privilege(%s, %s, 'CONNECT') AS can_connect,
               pg_catalog.has_database_privilege(%s, %s, 'CREATE') AS can_create_schema,
               pg_catalog.has_schema_privilege(%s, %s, 'USAGE') AS can_use_schema,
               pg_catalog.has_schema_privilege(%s, %s, 'CREATE') AS can_create_in_schema
        """,
        (role, database_name, role, database_name, role, SCHEMA, role, SCHEMA),
    ).fetchone()
    if not bool(boundary["can_connect"]) or not bool(boundary["can_use_schema"]):
        raise RuntimeRoleConfigurationError("runtime connection boundary was not granted")
    if bool(boundary["can_create_schema"]) or bool(boundary["can_create_in_schema"]):
        raise RuntimeRoleConfigurationError(
            "runtime role retains effective DDL privileges"
        )
    other_schema_create = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS schemas
            WHERE schemas.nspname !~ '^pg_'
              AND schemas.nspname <> 'information_schema'
              AND pg_catalog.has_schema_privilege(%s, schemas.oid, 'CREATE')
        ) AS can_create
        """,
        (role,),
    ).fetchone()
    if bool(other_schema_create["can_create"]):
        raise RuntimeRoleConfigurationError(
            "runtime role retains effective DDL privileges"
        )

    rows = conn.execute(
        """
        SELECT relations.relname,
               relations.relkind,
               pg_catalog.has_table_privilege(%s, relations.oid, 'SELECT') AS can_select,
               pg_catalog.has_table_privilege(%s, relations.oid, 'INSERT') AS can_insert,
               pg_catalog.has_table_privilege(%s, relations.oid, 'UPDATE') AS can_update,
               pg_catalog.has_table_privilege(%s, relations.oid, 'DELETE') AS can_delete,
               pg_catalog.has_table_privilege(%s, relations.oid, 'TRUNCATE') AS can_truncate,
               pg_catalog.has_table_privilege(%s, relations.oid, 'REFERENCES') AS can_reference,
               pg_catalog.has_table_privilege(%s, relations.oid, 'TRIGGER') AS can_trigger,
               pg_catalog.has_any_column_privilege(%s, relations.oid, 'SELECT') AS column_select,
               pg_catalog.has_any_column_privilege(%s, relations.oid, 'INSERT') AS column_insert,
               pg_catalog.has_any_column_privilege(%s, relations.oid, 'UPDATE') AS column_update,
               pg_catalog.has_any_column_privilege(%s, relations.oid, 'REFERENCES') AS column_reference
        FROM pg_catalog.pg_class AS relations
        JOIN pg_catalog.pg_namespace AS schemas
          ON schemas.oid = relations.relnamespace
        WHERE schemas.nspname = %s
          AND relations.relkind IN ('r', 'p', 'v', 'm', 'f')
        ORDER BY relations.relname
        """,
        (role,) * 11 + (SCHEMA,),
    ).fetchall()
    actual = {str(row["relname"]): row for row in rows}
    if set(actual) != {relation.name for relation in relations}:
        raise RuntimeRoleConfigurationError("relation set changed while configuring runtime privileges")

    table_privileges = (
        "can_select",
        "can_insert",
        "can_update",
        "can_delete",
        "can_truncate",
        "can_reference",
        "can_trigger",
    )
    write_or_reference_columns = ("column_insert", "column_update", "column_reference")
    read_only_names = set(view_names) | {MIGRATION_JOURNAL}
    for name, row in actual.items():
        if name in RAW_MIGRATION_019_RELATIONS:
            if any(bool(row[key]) for key in table_privileges) or any(
                bool(row[key])
                for key in ("column_select",) + write_or_reference_columns
            ):
                raise RuntimeRoleConfigurationError("runtime role retains effective raw-table access")
            continue
        if name in app_table_names:
            if not all(bool(row[key]) for key in ("can_select", "can_insert", "can_update", "can_delete")):
                raise RuntimeRoleConfigurationError("application table DML grant is incomplete")
            if any(
                bool(row[key])
                for key in (
                    "can_truncate",
                    "can_reference",
                    "can_trigger",
                    "column_reference",
                )
            ):
                raise RuntimeRoleConfigurationError(
                    "application table grant exceeds the DML contract"
                )
            continue
        if name in read_only_names:
            if not bool(row["can_select"]) or any(
                bool(row[key])
                for key in (
                    "can_insert",
                    "can_update",
                    "can_delete",
                    "can_truncate",
                    "can_reference",
                    "can_trigger",
                )
                + write_or_reference_columns
            ):
                raise RuntimeRoleConfigurationError("migration journal/view is not read-only")
            continue
        if any(bool(row[key]) for key in table_privileges) or any(
            bool(row[key]) for key in ("column_select",) + write_or_reference_columns
        ):
            raise RuntimeRoleConfigurationError("unexpected relation privilege remains")

    watermark = actual.get(WATERMARK_TABLE)
    if watermark is None or not all(
        bool(watermark[key]) for key in ("can_select", "can_insert", "can_update", "can_delete")
    ):
        raise RuntimeRoleConfigurationError("capture watermark table DML grant is incomplete")

    sequence_rows = conn.execute(
        """
        SELECT sequences.relname,
               pg_catalog.has_sequence_privilege(%s, sequences.oid, 'USAGE') AS can_use,
               pg_catalog.has_sequence_privilege(%s, sequences.oid, 'SELECT') AS can_select,
               pg_catalog.has_sequence_privilege(%s, sequences.oid, 'UPDATE') AS can_update
        FROM pg_catalog.pg_class AS sequences
        JOIN pg_catalog.pg_namespace AS schemas
          ON schemas.oid = sequences.relnamespace
        WHERE schemas.nspname = %s
          AND sequences.relkind = 'S'
        ORDER BY sequences.relname
        """,
        (role, role, role, SCHEMA),
    ).fetchall()
    expected_sequences = set(sequence_names)
    for row in sequence_rows:
        name = str(row["relname"])
        if name in expected_sequences:
            if not bool(row["can_use"]) or not bool(row["can_select"]) or bool(row["can_update"]):
                raise RuntimeRoleConfigurationError("owned application sequence grant is incorrect")
        elif bool(row["can_use"]) or bool(row["can_select"]) or bool(row["can_update"]):
            raise RuntimeRoleConfigurationError("runtime role retains an unexpected sequence privilege")
    if {str(row["relname"]) for row in sequence_rows} & expected_sequences != expected_sequences:
        raise RuntimeRoleConfigurationError("owned application sequence set changed during configuration")

    security_definer = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS functions
            WHERE functions.prosecdef
              AND pg_catalog.has_function_privilege(
                  %s, functions.oid, 'EXECUTE'
              )
        ) AS can_execute_security_definer
        """,
        (role,),
    ).fetchone()
    if bool(security_definer["can_execute_security_definer"]):
        raise RuntimeRoleConfigurationError("runtime role retains raw SECURITY DEFINER access")


def configure_runtime_role(
    database_url: str,
    *,
    role: str,
    database_name: str | None = None,
) -> str:
    role_name = str(role or "").strip()
    if not role_name or "\x00" in role_name:
        raise RuntimeRoleConfigurationError("--role must be a valid existing identifier")

    manifest, verifier, advisory_lock_key = _migration_contract()
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(
                "SELECT pg_catalog.pg_advisory_xact_lock(%s)",
                (advisory_lock_key(SCHEMA, "schema-migrations-v1"),),
            )
            head = _verify_exact_head(conn, manifest, verifier)
            _require_safe_target_role(conn, role_name)
            resolved_database_name = _resolve_database_name(conn, database_name)
            relations = _load_relations(conn)
            app_table_names = set(EXPECTED_APPLICATION_TABLES)
            if WATERMARK_TABLE not in app_table_names:
                raise RuntimeRoleConfigurationError(
                    "capture watermark table is not an application table"
                )
            view_names = set(EXPECTED_READ_ONLY_VIEWS)
            sequence_names = _load_owned_sequences(conn, app_table_names)
            _reset_and_grant(
                conn,
                role=role_name,
                database_name=resolved_database_name,
                relations=relations,
                app_table_names=app_table_names,
                view_names=view_names,
                sequence_names=sequence_names,
            )
            _verify_exact_head(conn, manifest, verifier)
            _require_safe_target_role(conn, role_name)
            _verify_effective_privileges(
                conn,
                role=role_name,
                database_name=resolved_database_name,
                relations=relations,
                app_table_names=app_table_names,
                view_names=view_names,
                sequence_names=sequence_names,
            )
    return head


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    database_url = str(os.environ.get("B3S_MIGRATION_DATABASE_URL", "")).strip()
    if not database_url:
        print(json.dumps({"status": "error", "error": "B3S_MIGRATION_DATABASE_URL is required"}))
        return 2
    try:
        head = configure_runtime_role(
            database_url,
            role=args.role,
            database_name=args.database_name,
        )
    except Exception:
        # This boundary must never echo driver errors: they can contain the DSN,
        # host, username, password, or server-provided connection details.
        print(json.dumps({"status": "error", "error": "runtime role configuration failed"}))
        return 1
    print(json.dumps({"status": "ok", "head": head}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
