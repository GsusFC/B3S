from __future__ import annotations

import json

import pytest

from scripts import verify_pr71_vault_target as target


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self.row = row
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement):
        normalized = " ".join(str(statement).split())
        self.statements.append(normalized)
        if normalized.startswith("SELECT current_database()"):
            return _Cursor(self.row)
        return _Cursor()


def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in target._EXPECTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("B3S_DATABASE_URL", "postgresql://runtime:secret@db.invalid/neondb")


def test_exact_isolated_target_passes_read_only(monkeypatch, capsys):
    _environment(monkeypatch)
    connection = _Connection(
        (
            "neondb",
            "jolly-river-32467750",
            "br-divine-star-aspobuer",
        )
    )
    monkeypatch.setattr(target.psycopg, "connect", lambda *_a, **_kw: connection)

    assert target.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["app"] == "b3s-pr71-vault"
    assert connection.statements[0] == "SET TRANSACTION READ ONLY"
    assert all(
        not statement.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
        for statement in connection.statements
    )


def test_wrong_app_fails_before_database_connection(monkeypatch):
    _environment(monkeypatch)
    monkeypatch.setenv("FLY_APP_NAME", "b3s-vault")
    monkeypatch.setattr(
        target.psycopg,
        "connect",
        lambda *_a, **_kw: pytest.fail("database must not be contacted"),
    )

    with pytest.raises(SystemExit, match="target verification failed"):
        target.main()


def test_wrong_branch_fails_without_leaking_dsn(monkeypatch):
    _environment(monkeypatch)
    connection = _Connection(("neondb", "jolly-river-32467750", "br-wrong"))
    monkeypatch.setattr(target.psycopg, "connect", lambda *_a, **_kw: connection)

    with pytest.raises(SystemExit) as raised:
        target.main()

    assert "secret" not in str(raised.value)
