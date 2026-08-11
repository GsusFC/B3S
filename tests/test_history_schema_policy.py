from __future__ import annotations

import json

import pytest

from src.history.repository import (
    PostgresHistoryRepository,
    SchemaHeadMismatchError,
    _migration_manifest,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _ManifestConnection:
    def __init__(self, rows, *, select_error: Exception | None = None):
        self.rows = rows
        self.select_error = select_error
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, *_args, **_kwargs):
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        if sql.upper().startswith("SELECT"):
            if self.select_error is not None:
                raise self.select_error
            return _Result(self.rows)
        return _Result([])


def _packaged_rows() -> list[dict[str, str]]:
    return [
        {"version": version, "filename": filename, "checksum": checksum}
        for version, filename, checksum, _sql_text in _migration_manifest()
    ]


def test_verify_head_is_read_only_and_cached_by_repository_policy() -> None:
    connection = _ManifestConnection(_packaged_rows())
    repository = PostgresHistoryRepository(
        "postgresql://runtime:secret@example.test/b3s",
        connect=lambda *_args, **_kwargs: connection,
        schema_policy="verify_head",
    )

    repository._ensure_migrated()
    repository._ensure_migrated()

    assert len(connection.statements) == 2
    assert connection.statements[0] == ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    assert connection.statements[1].startswith("SELECT version, filename, checksum FROM b3s_history.schema_migrations")
    assert not any(
        statement.upper().startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "TRUNCATE"))
        for statement in connection.statements
    )


def test_verify_head_policy_cannot_call_migrate() -> None:
    repository = PostgresHistoryRepository(
        "postgresql://runtime:secret@example.test/b3s",
        connect=lambda *_args, **_kwargs: pytest.fail("must not connect"),
        schema_policy="verify_head",
    )

    with pytest.raises(RuntimeError, match="does not permit migrations"):
        repository.migrate()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows[:-1], "missing versions"),
        (
            lambda rows: (
                rows
                + [
                    {
                        "version": "999",
                        "filename": "999_future.sql",
                        "checksum": "f" * 64,
                    }
                ]
            ),
            "unexpected/ahead versions",
        ),
        (
            lambda rows: [
                *rows[:-1],
                {**rows[-1], "filename": "021_renamed.sql"},
            ],
            "filename drift",
        ),
        (
            lambda rows: [
                *rows[:-1],
                {**rows[-1], "checksum": "0" * 64},
            ],
            "checksum drift",
        ),
    ],
)
def test_verify_head_rejects_exact_manifest_drift(mutate, message) -> None:
    rows = mutate(_packaged_rows())
    repository = PostgresHistoryRepository(
        "postgresql://runtime:secret@example.test/b3s",
        connect=lambda *_args, **_kwargs: _ManifestConnection(rows),
        schema_policy="verify_head",
    )

    with pytest.raises(SchemaHeadMismatchError, match=message):
        repository.verify_migration_head()


def test_verify_head_hides_connection_details() -> None:
    dsn = "postgresql://runtime:do-not-leak@example.test/b3s"
    connection = _ManifestConnection(
        [],
        select_error=RuntimeError(f"could not use {dsn}"),
    )
    repository = PostgresHistoryRepository(
        dsn,
        connect=lambda *_args, **_kwargs: connection,
        schema_policy="verify_head",
    )

    with pytest.raises(SchemaHeadMismatchError) as captured:
        repository.verify_migration_head()

    assert "do-not-leak" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_web_repository_uses_verify_head_policy(monkeypatch) -> None:
    from src.history import repository as repository_module
    from web import report_store

    created: list[tuple[str, str]] = []

    class Repository:
        def __init__(self, dsn, *, schema_policy):
            created.append((dsn, schema_policy))

    monkeypatch.setattr(repository_module, "PostgresHistoryRepository", Repository)
    report_store._postgres_repository_for_url.cache_clear()
    try:
        report_store._postgres_repository_for_url("postgresql://runtime:secret@example.test/b3s")
    finally:
        report_store._postgres_repository_for_url.cache_clear()

    assert created == [("postgresql://runtime:secret@example.test/b3s", "verify_head")]


def test_required_postgres_readiness_fails_closed_without_dsn(monkeypatch) -> None:
    from web import report_store

    dsn = "postgresql://runtime:do-not-leak@example.test/b3s"

    class Repository:
        def verify_migration_head(self):
            raise RuntimeError(dsn)

    monkeypatch.setenv("B3S_POSTGRES_REQUIRED", "true")
    monkeypatch.setenv("B3S_DATABASE_URL", dsn)
    monkeypatch.setattr(
        report_store,
        "_postgres_repository_for_url",
        lambda _database_url: Repository(),
    )

    with pytest.raises(RuntimeError) as captured:
        report_store.verify_postgres_runtime_ready()

    assert "do-not-leak" not in str(captured.value)



def test_required_postgres_readiness_rejects_invalid_toggle(monkeypatch) -> None:
    from web import report_store

    monkeypatch.setenv("B3S_POSTGRES_REQUIRED", "tru")

    with pytest.raises(RuntimeError, match="must be true or false"):
        report_store.verify_postgres_runtime_ready()

def test_database_identity_clis_do_not_fall_back_or_leak_dsn(
    capsys,
    monkeypatch,
) -> None:
    from scripts import migrate_b3s_history_postgres
    from scripts import verify_b3s_history_postgres

    monkeypatch.delenv("B3S_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("B3S_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://wrong:do-not-leak@example.test/b3s",
    )

    assert migrate_b3s_history_postgres.main([]) == 2
    migrate_payload = json.loads(capsys.readouterr().out)
    assert migrate_payload == {
        "status": "error",
        "error": "B3S_MIGRATION_DATABASE_URL is required",
    }

    assert verify_b3s_history_postgres.main([]) == 2
    verify_payload = json.loads(capsys.readouterr().out)
    assert verify_payload == {
        "status": "error",
        "error": "B3S_DATABASE_URL is required",
    }
    assert "do-not-leak" not in json.dumps([migrate_payload, verify_payload])


def test_privileged_migration_cli_uses_only_migration_identity(
    capsys,
    monkeypatch,
) -> None:
    from scripts import migrate_b3s_history_postgres
    from src.history import repository as repository_module

    migration_dsn = "postgresql://migrator:secret@example.test/b3s"
    created: list[str] = []

    class Repository:
        def __init__(self, dsn):
            created.append(dsn)

        def migrate(self):
            return ["001_history.sql"]

    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", migration_dsn)
    monkeypatch.setenv(
        "B3S_DATABASE_URL",
        "postgresql://runtime:wrong@example.test/b3s",
    )
    monkeypatch.setattr(repository_module, "PostgresHistoryRepository", Repository)
    monkeypatch.setattr(
        repository_module,
        "_migration_manifest",
        lambda: [("001", "001_history.sql", "a" * 64, "SELECT 1")],
    )

    assert migrate_b3s_history_postgres.main([]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert created == [migration_dsn]
    assert payload == {
        "status": "ok",
        "head": "001_history.sql",
        "count": 1,
        "applied": ["001_history.sql"],
    }
    assert migration_dsn not in json.dumps(payload)


def test_verification_cli_uses_runtime_identity_and_verify_policy(
    capsys,
    monkeypatch,
) -> None:
    from scripts import verify_b3s_history_postgres
    from src.history import repository as repository_module

    runtime_dsn = "postgresql://runtime:secret@example.test/b3s"
    created: list[tuple[str, str]] = []

    class Repository:
        def __init__(self, dsn, *, schema_policy):
            created.append((dsn, schema_policy))

        def verify_migration_head(self):
            return None

    monkeypatch.setenv("B3S_DATABASE_URL", runtime_dsn)
    monkeypatch.setattr(repository_module, "PostgresHistoryRepository", Repository)
    monkeypatch.setattr(
        repository_module,
        "_migration_manifest",
        lambda: [("001", "001_history.sql", "a" * 64, "SELECT 1")],
    )

    assert verify_b3s_history_postgres.main([]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert created == [(runtime_dsn, "verify_head")]
    assert payload == {
        "status": "ok",
        "head": "001_history.sql",
        "count": 1,
    }
    assert runtime_dsn not in json.dumps(payload)
