from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from scripts import migrate_b3s_history_postgres as migration_cli
from scripts.pr71_vault_database_target import (
    PR71VaultTargetError,
    pr71_vault_migration_target,
    require_pr71_vault_connection,
    validate_pr71_vault_dsn,
)
from src.history import repository as repository_module


_TARGET = pr71_vault_migration_target()
_TARGET_DSN = (
    "postgresql://neondb_owner:do-not-print@"
    f"{_TARGET.host}/neondb?sslmode=require&channel_binding=require"
)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self.row = row
        self.info = SimpleNamespace(host=_TARGET.host)
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, *_args, **_kwargs):
        normalized = " ".join(str(statement).split())
        self.statements.append(normalized)
        if "current_user AS user_name" in normalized:
            return _Result(self.row)
        raise AssertionError(f"unexpected statement after failed target check: {normalized}")


def _target_row(*, branch_id: str = "br-divine-star-aspobuer"):
    return {
        "database_name": "neondb",
        "user_name": "neondb_owner",
        "project_id": "jolly-river-32467750",
        "branch_id": branch_id,
        "tls_in_use": True,
    }


def test_pr71_migration_profile_checks_same_connection_before_migrate(
    monkeypatch,
    capsys,
) -> None:
    connection = _Connection(_target_row())
    observed: dict[str, object] = {}

    class Repository:
        def __init__(self, dsn):
            observed["dsn"] = dsn

        def migrate(self, *, connection_preflight):
            observed["pgoptions"] = os.environ.get("PGOPTIONS")
            connection_preflight(connection)
            observed["preflight_connection"] = connection
            return ["023_evidence_vault_raw_replay_projection.sql"]

    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _TARGET_DSN)
    monkeypatch.setenv("PGOPTIONS", "-c neon.branch_id=br-wrong")
    monkeypatch.setattr(repository_module, "PostgresHistoryRepository", Repository)
    monkeypatch.setattr(
        repository_module,
        "_migration_manifest",
        lambda: [
            (
                "023",
                "023_evidence_vault_raw_replay_projection.sql",
                "a" * 64,
                "SELECT 1",
            )
        ],
    )

    assert migration_cli.main(["--target-profile", "pr71-vault"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert observed == {
        "dsn": _TARGET_DSN,
        "pgoptions": None,
        "preflight_connection": connection,
    }
    assert os.environ["PGOPTIONS"] == "-c neon.branch_id=br-wrong"
    assert _TARGET_DSN not in json.dumps(payload)


@pytest.mark.parametrize(
    "tls_query",
    [
        "sslmode=verify-full",
        "sslmode=require&channel_binding=require",
    ],
)
def test_pr71_target_accepts_only_the_two_authenticated_tls_contracts(
    tls_query,
) -> None:
    dsn = (
        "postgresql://neondb_owner:secret@"
        f"{_TARGET.host}/neondb?{tls_query}"
    )

    validate_pr71_vault_dsn(dsn, target=_TARGET)


def test_wrong_pr71_url_fails_before_migrate(monkeypatch, capsys) -> None:
    wrong_dsn = (
        "postgresql://neondb_owner:do-not-print@wrong.example/neondb"
        "?sslmode=verify-full"
    )
    called = False

    class Repository:
        def __init__(self, _dsn):
            pass

        def migrate(self, **_kwargs):
            nonlocal called
            called = True
            return []

    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", wrong_dsn)
    monkeypatch.setattr(repository_module, "PostgresHistoryRepository", Repository)

    assert migration_cli.main(["--target-profile", "pr71-vault"]) == 1
    assert not called
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "error", "error": "history migration failed"}
    assert "do-not-print" not in json.dumps(payload)


def test_wrong_live_pr71_target_has_no_migration_side_effects() -> None:
    connection = _Connection(_target_row(branch_id="br-wrong"))
    repository = repository_module.PostgresHistoryRepository(
        _TARGET_DSN,
        connect=lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(PR71VaultTargetError):
        repository.migrate(
            connection_preflight=lambda conn: require_pr71_vault_connection(
                conn,
                target=_TARGET,
            )
        )

    assert len(connection.statements) == 1
    assert "current_user AS user_name" in connection.statements[0]
    assert not any(
        statement.startswith(
            ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ")
        )
        for statement in connection.statements
    )


@pytest.mark.parametrize(
    "dsn",
    [
        (
            "postgresql://neondb_owner:secret@"
            f"{_TARGET.host}/wrong?sslmode=verify-full"
        ),
        (
            "postgresql://wrong_user:secret@"
            f"{_TARGET.host}/neondb?sslmode=verify-full"
        ),
        (
            "postgresql://neondb_owner:secret@"
            f"{_TARGET.host}/neondb?sslmode=require"
        ),
        (
            "postgresql://neondb_owner:secret@"
            f"{_TARGET.host}/neondb?sslmode=verify-full&options=-c%20role%3Dneondb_owner"
        ),
    ],
)
def test_pr71_migration_url_contract_rejects_effective_target_drift(
    monkeypatch,
    capsys,
    dsn,
) -> None:
    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", dsn)

    assert migration_cli.main(["--target-profile", "pr71-vault"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "error", "error": "history migration failed"}
    assert "secret" not in json.dumps(payload)
