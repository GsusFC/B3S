from __future__ import annotations

from contextlib import nullcontext
import json
import os
from types import SimpleNamespace

import pytest

from scripts import migrate_b3s_history_postgres as migration_cli
from scripts.b3s_vault_database_target import (
    B3SVaultTargetError,
    b3s_vault_migration_target,
    require_b3s_vault_connection,
    validate_b3s_vault_dsn,
)
from src.history import repository as repository_module


_TARGET = b3s_vault_migration_target()
_TARGET_DSN = f"postgresql://neondb_owner:do-not-print@{_TARGET.host}/neondb?sslmode=require&channel_binding=require"


class _Connection:
    def __init__(self, row, *, ssl_in_use=True):
        self.row = row
        self.info = SimpleNamespace(host=_TARGET.host)
        self.pgconn = SimpleNamespace(ssl_in_use=ssl_in_use)
        self.statements: list[str] = []

    def execute(self, statement, *_args, **_kwargs):
        normalized = " ".join(str(statement).split())
        self.statements.append(normalized)
        if "current_user AS user_name" in normalized:
            return SimpleNamespace(fetchone=lambda: self.row)
        raise AssertionError(f"unexpected statement after target check: {normalized}")


def _target_row(*, branch_id: str = _TARGET.branch_id, server_port: int = 5432):
    return {
        "database_name": _TARGET.database,
        "user_name": _TARGET.user,
        "project_id": _TARGET.project_id,
        "branch_id": branch_id,
        "server_port": server_port,
    }


def test_b3s_profile_attests_same_connection(monkeypatch, capsys):
    connection = _Connection(_target_row())
    observed: dict[str, object] = {}

    class Repository:
        def __init__(self, dsn):
            observed["dsn"] = dsn

        def migrate(self, *, connection_preflight):
            observed["pgoptions"] = os.environ.get("PGOPTIONS")
            connection_preflight(connection)
            observed["preflight_connection"] = connection
            return ["033_evidence_vault_sv9_judgment_candidate_witness.sql"]

    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _TARGET_DSN)
    monkeypatch.setenv("PGOPTIONS", "-c neon.branch_id=br-wrong")
    monkeypatch.setattr(repository_module, "PostgresHistoryRepository", Repository)
    monkeypatch.setattr(
        repository_module,
        "_migration_manifest",
        lambda: [("033", "033_evidence_vault_sv9_judgment_candidate_witness.sql", "a" * 64, "SELECT 1")],
    )

    assert migration_cli.main(["--target-profile", "b3s-vault"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert observed["dsn"] == _TARGET_DSN
    assert observed["pgoptions"] is None
    assert observed["preflight_connection"] is connection
    assert os.environ["PGOPTIONS"] == "-c neon.branch_id=br-wrong"
    assert "inet_server_port()" in connection.statements[0]
    assert _TARGET_DSN not in json.dumps(payload)


@pytest.mark.parametrize(
    "dsn",
    [
        _TARGET_DSN.replace("?", ":5433?"),
        _TARGET_DSN.replace("sslmode=require", "sslmode=verify-full"),
        _TARGET_DSN.replace("channel_binding=require", "channel_binding=prefer"),
        _TARGET_DSN + "&sslmode=require",
        _TARGET_DSN + "&host=wrong.example",
    ],
)
def test_b3s_vault_migration_url_rejects_target_drift(dsn: str) -> None:
    with pytest.raises(B3SVaultTargetError):
        validate_b3s_vault_dsn(dsn, target=_TARGET)


@pytest.mark.parametrize(
    "row",
    [_target_row(branch_id="br-wrong"), _target_row(server_port=5433)],
)
def test_b3s_vault_connection_rejects_wrong_session_before_schema_work(row) -> None:
    connection = _Connection(row)
    connect = lambda *_args, **_kwargs: nullcontext(connection)
    repository = repository_module.PostgresHistoryRepository(_TARGET_DSN, connect=connect)

    def preflight(conn):
        require_b3s_vault_connection(conn, target=_TARGET)

    with pytest.raises(B3SVaultTargetError):
        repository.migrate(connection_preflight=preflight)

    assert len(connection.statements) == 1
    assert "current_user AS user_name" in connection.statements[0]
    assert not any(
        statement.startswith(("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE "))
        for statement in connection.statements
    )


def test_b3s_vault_profile_forbids_url_argument(monkeypatch, capsys) -> None:
    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _TARGET_DSN)

    assert migration_cli.main(["--target-profile", "b3s-vault", "--database-url", "do-not-print"]) == 2

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "target profiles require B3S_MIGRATION_DATABASE_URL",
    }
