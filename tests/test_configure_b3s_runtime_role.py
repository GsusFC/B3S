from __future__ import annotations

import json
from contextlib import contextmanager
import os

import pytest

from scripts import configure_b3s_runtime_role as runtime_role


class _Result:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = list(rows or [])

    def fetchone(self):
        if self.row is not None:
            return self.row
        return self.rows[0] if self.rows else None

    def fetchall(self):
        if self.rows:
            return list(self.rows)
        return [self.row] if self.row is not None else []


class _Connection:
    def __init__(self, *, unsafe_role: bool = False):
        self.unsafe_role = unsafe_role
        self.statements: list[tuple[str, object]] = []
        self.transaction_count = 0
        self.rolled_back = False
        self.relations = _relations()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @contextmanager
    def transaction(self):
        self.transaction_count += 1
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise

    def execute(self, statement, params=None, **_kwargs):
        rendered = statement.as_string() if hasattr(statement, "as_string") else str(statement)
        normalized = " ".join(rendered.split())
        self.statements.append((normalized, params))

        if "SELECT version, filename, checksum" in normalized:
            return _Result(
                rows=[
                    {
                        "version": "022",
                        "filename": "022_evidence_vault_raw_incremental_planning.sql",
                        "checksum": "a" * 64,
                    }
                ]
            )
        if "FROM pg_catalog.pg_roles" in normalized:
            return _Result(
                row={
                    "oid": 9001,
                    "rolcanlogin": True,
                    "rolsuper": self.unsafe_role,
                    "rolcreaterole": False,
                    "rolcreatedb": False,
                    "rolreplication": False,
                    "rolbypassrls": False,
                }
            )
        if "FROM pg_catalog.pg_auth_members" in normalized:
            return _Result()
        if "FROM pg_catalog.pg_shdepend" in normalized:
            return _Result()
        if normalized == "SELECT current_database() AS database_name":
            return _Result(row={"database_name": "b3s"})
        if "array_agg(attributes.attname" in normalized:
            return _Result(rows=self.relations)
        if "JOIN pg_catalog.pg_depend AS dependencies" in normalized:
            return _Result(
                rows=[
                    {
                        "sequence_name": "capture_sequence_owned_seq",
                        "table_name": runtime_role.WATERMARK_TABLE,
                    },
                    {
                        "sequence_name": "raw_owned_seq",
                        "table_name": "evidence_vault_raw_acquisition_receipts",
                    },
                ]
            )
        if "has_database_privilege" in normalized:
            return _Result(
                row={
                    "can_connect": True,
                    "can_create_schema": False,
                    "can_use_schema": True,
                    "can_create_in_schema": False,
                }
            )
        if "FROM pg_catalog.pg_namespace AS schemas" in normalized and "AS can_create" in normalized:
            return _Result(row={"can_create": False})
        if "has_table_privilege" in normalized:
            return _Result(rows=_effective_relation_privileges(self.relations))
        if "has_sequence_privilege" in normalized:
            return _Result(
                rows=[
                    {
                        "relname": "capture_sequence_owned_seq",
                        "can_use": True,
                        "can_select": True,
                        "can_update": False,
                    },
                    {
                        "relname": "raw_owned_seq",
                        "can_use": False,
                        "can_select": False,
                        "can_update": False,
                    },
                ]
            )
        if "functions.prosecdef" in normalized:
            return _Result(row={"can_execute_security_definer": False})
        return _Result()


def _relations():
    names_and_kinds = [
        (runtime_role.MIGRATION_JOURNAL, "r"),
        *((name, "r") for name in sorted(runtime_role.EXPECTED_APPLICATION_TABLES)),
        *((name, "v") for name in sorted(runtime_role.EXPECTED_READ_ONLY_VIEWS)),
        *((name, "r") for name in sorted(runtime_role.RAW_MIGRATION_019_RELATIONS)),
    ]
    return [
        {"oid": index, "relname": name, "relkind": kind, "columns": ["id", "payload"]}
        for index, (name, kind) in enumerate(names_and_kinds, start=1)
    ]


def _effective_relation_privileges(relations):
    rows = []
    for relation in relations:
        name = relation["relname"]
        app_dml = name in runtime_role.EXPECTED_APPLICATION_TABLES
        read_only = name in (
            runtime_role.EXPECTED_READ_ONLY_VIEWS
            | {runtime_role.MIGRATION_JOURNAL}
        )
        can_select = app_dml or read_only
        rows.append(
            {
                "relname": name,
                "relkind": relation["relkind"],
                "can_select": can_select,
                "can_insert": app_dml,
                "can_update": app_dml,
                "can_delete": app_dml,
                "can_truncate": False,
                "can_reference": False,
                "can_trigger": False,
                "column_select": can_select,
                "column_insert": app_dml,
                "column_update": app_dml,
                "column_reference": False,
            }
        )
    return rows


def _install_contract(monkeypatch):
    manifest = [
        (
            "022",
            "022_evidence_vault_raw_incremental_planning.sql",
            "a" * 64,
            "SELECT 1",
        )
    ]

    def verify(expected, actual):
        assert expected == manifest
        assert actual == [
            {
                "version": "022",
                "filename": "022_evidence_vault_raw_incremental_planning.sql",
                "checksum": "a" * 64,
            }
        ]

    monkeypatch.setattr(runtime_role, "_migration_contract", lambda: (manifest, verify, lambda *_parts: 71))


def test_configures_exact_runtime_contract_in_one_transaction(monkeypatch, capsys) -> None:
    dsn = "postgresql://migrator:do-not-print@example.test/b3s"
    connection = _Connection()
    connected = []
    _install_contract(monkeypatch)
    monkeypatch.setattr(
        runtime_role.psycopg,
        "connect",
        lambda value, **kwargs: connected.append((value, kwargs)) or connection,
    )
    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", dsn)
    monkeypatch.setenv("DATABASE_URL", "postgresql://wrong:also-secret@example.test/wrong")

    assert runtime_role.main(["--role", 'runtime "web"', "--database-name", "b3s"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "ok",
        "head": "022_evidence_vault_raw_incremental_planning.sql",
    }
    assert dsn not in json.dumps(payload)
    assert connected[0][0] == dsn
    assert connection.transaction_count == 1
    assert not connection.rolled_back

    statements = [statement for statement, _params in connection.statements]
    assert sum("SELECT version, filename, checksum" in statement for statement in statements) == 2
    assert statements[0] == "SELECT pg_catalog.pg_advisory_xact_lock(%s)"
    assert any(
        statement == 'REVOKE ALL PRIVILEGES ON DATABASE "b3s" FROM "runtime ""web"""'
        for statement in statements
    )
    assert any(
        statement == 'REVOKE ALL PRIVILEGES ON SCHEMA "b3s_history" FROM "runtime ""web"""'
        for statement in statements
    )
    assert any(
        statement == 'REVOKE CREATE ON SCHEMA "b3s_history" FROM "runtime ""web"""'
        for statement in statements
    )
    assert sum(statement.startswith("REVOKE ALL PRIVILEGES (") for statement in statements) == len(_relations())
    assert any(
        statement
        == 'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA "b3s_history" FROM "runtime ""web"""'
        for statement in statements
    )

    dml = next(statement for statement in statements if statement.startswith("GRANT SELECT, INSERT, UPDATE, DELETE"))
    assert '"b3s_history"."evidence_vault_capture_watermark_events"' in dml
    assert '"b3s_history"."brands"' in dml
    assert runtime_role.MIGRATION_JOURNAL not in dml
    assert not any(name in dml for name in runtime_role.RAW_MIGRATION_019_RELATIONS)

    journal_and_views = next(statement for statement in statements if statement.startswith("GRANT SELECT ON TABLE"))
    assert '"b3s_history"."schema_migrations"' in journal_and_views
    assert '"b3s_history"."brand_history"' in journal_and_views
    assert "INSERT" not in journal_and_views
    assert any(
        statement
        == 'REVOKE INSERT, UPDATE, DELETE ON TABLE "b3s_history"."schema_migrations" FROM "runtime ""web"""'
        for statement in statements
    )
    assert any(
        statement.startswith("GRANT USAGE, SELECT ON SEQUENCE")
        and '"b3s_history"."capture_sequence_owned_seq"' in statement
        and "raw_owned_seq" not in statement
        for statement in statements
    )

    raw_revoke = next(
        statement
        for statement in statements
        if statement.startswith("REVOKE ALL PRIVILEGES ON TABLE")
    )
    assert all(name in raw_revoke for name in runtime_role.RAW_MIGRATION_019_RELATIONS)


def test_rejects_unsafe_role_and_never_prints_driver_or_dsn(monkeypatch, capsys) -> None:
    dsn = "postgresql://migrator:do-not-print@example.test/b3s"
    connection = _Connection(unsafe_role=True)
    _install_contract(monkeypatch)
    monkeypatch.setattr(runtime_role.psycopg, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", dsn)

    assert runtime_role.main(["--role", "unsafe_runtime"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "error", "error": "runtime role configuration failed"}
    assert dsn not in json.dumps(payload)
    assert connection.rolled_back
    assert not any(statement.startswith("GRANT ") for statement, _params in connection.statements)


def test_requires_only_migration_database_url(monkeypatch, capsys) -> None:
    monkeypatch.delenv("B3S_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("B3S_DATABASE_URL", "postgresql://runtime:secret@example.test/b3s")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fallback:secret@example.test/b3s")

    assert runtime_role.main(["--role", "b3s_runtime"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "error",
        "error": "B3S_MIGRATION_DATABASE_URL is required",
    }
    assert "secret" not in json.dumps(payload)


def test_raw_relation_and_journal_contract_is_pinned() -> None:
    assert runtime_role.EXPECTED_HEAD_VERSION == "022"
    assert runtime_role.MIGRATION_JOURNAL == "schema_migrations"
    assert runtime_role.WATERMARK_TABLE not in runtime_role.RAW_MIGRATION_019_RELATIONS
    assert runtime_role.RAW_MIGRATION_019_RELATIONS == {
        "evidence_vault_raw_acquisition_receipts",
        "evidence_vault_raw_evidence_bindings",
        "evidence_vault_verified_c7_lineage_bindings",
        "evidence_vault_verified_c7_lineage_members",
        "evidence_vault_raw_provenance_disposition_events",
    }


def test_unexpected_relation_fails_before_any_privilege_change(monkeypatch) -> None:
    connection = _Connection()
    connection.relations.append(
        {
            "oid": 99999,
            "relname": "unjournaled_raw_leak",
            "relkind": "v",
            "columns": ["raw_payload"],
        }
    )
    _install_contract(monkeypatch)
    monkeypatch.setattr(
        runtime_role.psycopg,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    try:
        runtime_role.configure_runtime_role(
            "postgresql://migrator:secret@example.test/b3s",
            role="runtime",
            database_name="b3s",
        )
    except runtime_role.RuntimeRoleConfigurationError as exc:
        assert str(exc) == "migration-head relation set is not exact"
    else:
        raise AssertionError("unexpected view must fail closed")

    assert connection.rolled_back
    assert not any(
        statement.startswith(("GRANT ", "REVOKE "))
        for statement, _params in connection.statements
    )


def test_packaged_head_relation_allowlists_are_exact() -> None:
    assert runtime_role.EXPECTED_READ_ONLY_VIEWS == {
        "brand_current_state",
        "brand_history",
    }
    assert runtime_role.WATERMARK_TABLE in runtime_role.EXPECTED_APPLICATION_TABLES
    assert runtime_role.EXPECTED_HEAD_RELATIONS == (
        runtime_role.EXPECTED_APPLICATION_TABLES
        | runtime_role.EXPECTED_READ_ONLY_VIEWS
        | runtime_role.RAW_MIGRATION_019_RELATIONS
        | {runtime_role.MIGRATION_JOURNAL}
    )


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_postgres_runtime_configuration_rejects_unjournaled_escape_surfaces() -> None:
    import psycopg
    from psycopg import sql

    from src.history.repository import PostgresHistoryRepository

    admin_dsn = os.environ["B3S_TEST_DATABASE_URL"]
    role = "b3s_runtime_config_integration"
    public_schema_create_was_granted = False
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        database_name = conn.execute("SELECT current_database()").fetchone()[0]
        public_schema_create_was_granted = bool(
            conn.execute(
                "SELECT has_schema_privilege('public', 'public', 'CREATE')"
            ).fetchone()[0]
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("DROP SCHEMA IF EXISTS outside_runtime_test CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        conn.execute(
            """DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles
                    WHERE rolname = 'b3s_history_vault_provenance_owner'
                ) THEN
                    CREATE ROLE b3s_history_vault_provenance_owner NOLOGIN;
                END IF;
            END $$"""
        )
        if conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            (role,),
        ).fetchone():
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role))
        )
    PostgresHistoryRepository(admin_dsn).migrate()

    try:
        runtime_role.configure_runtime_role(
            admin_dsn,
            role=role,
            database_name=database_name,
        )
        runtime_role.configure_runtime_role(
            admin_dsn,
            role=role,
            database_name=database_name,
        )

        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "CREATE VIEW b3s_history.unjournaled_raw_leak AS "
                "SELECT claims FROM "
                "b3s_history.evidence_vault_raw_acquisition_receipts"
            )
        with pytest.raises(runtime_role.RuntimeRoleConfigurationError):
            runtime_role.configure_runtime_role(
                admin_dsn,
                role=role,
                database_name=database_name,
            )
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute("DROP VIEW b3s_history.unjournaled_raw_leak")
            conn.execute("CREATE SCHEMA outside_runtime_test")
            conn.execute(
                "GRANT CREATE ON SCHEMA outside_runtime_test TO PUBLIC"
            )
        with pytest.raises(runtime_role.RuntimeRoleConfigurationError):
            runtime_role.configure_runtime_role(
                admin_dsn,
                role=role,
                database_name=database_name,
            )
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "REVOKE CREATE ON SCHEMA outside_runtime_test FROM PUBLIC"
            )
            conn.execute(
                "CREATE FUNCTION outside_runtime_test.leaked_sd() "
                "RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'"
            )
        with pytest.raises(runtime_role.RuntimeRoleConfigurationError):
            runtime_role.configure_runtime_role(
                admin_dsn,
                role=role,
                database_name=database_name,
            )
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "REVOKE EXECUTE ON FUNCTION "
                "outside_runtime_test.leaked_sd() FROM PUBLIC"
            )
            conn.execute(
                "GRANT REFERENCES(id) ON b3s_history.brands TO PUBLIC"
            )
        with pytest.raises(runtime_role.RuntimeRoleConfigurationError):
            runtime_role.configure_runtime_role(
                admin_dsn,
                role=role,
                database_name=database_name,
            )
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "REVOKE REFERENCES(id) ON b3s_history.brands FROM PUBLIC"
            )
            conn.execute("DROP SCHEMA outside_runtime_test CASCADE")
        runtime_role.configure_runtime_role(
            admin_dsn,
            role=role,
            database_name=database_name,
        )
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS outside_runtime_test CASCADE")
            if conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone():
                conn.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role))
                )
                conn.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                )
            if public_schema_create_was_granted:
                conn.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")


def test_role_safety_rejects_only_target_outgoing_membership() -> None:
    connection = _Connection()

    runtime_role._require_safe_target_role(connection, "runtime")

    membership_sql = next(
        statement
        for statement, _params in connection.statements
        if "FROM pg_catalog.pg_auth_members" in statement
    )
    assert "WHERE member = %s" in membership_sql
    assert "roleid = %s" not in membership_sql
