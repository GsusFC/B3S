from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from scripts import verify_pr71_vault_target as target


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row, *, ssl_in_use=True):
        self.row = row
        self.statements: list[str] = []
        self.info = SimpleNamespace(
            host=target._EXPECTED["B3S_EXPECTED_NEON_ENDPOINT_HOST"]
        )
        self.pgconn = SimpleNamespace(ssl_in_use=ssl_in_use)

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
    monkeypatch.setenv(
        "B3S_DATABASE_URL",
        (
            "postgresql://b3s_pr71_app_runtime:secret@"
            f"{target._EXPECTED['B3S_EXPECTED_NEON_ENDPOINT_HOST']}/neondb"
            "?sslmode=require&channel_binding=require"
        ),
    )


def test_exact_isolated_target_passes_read_only(monkeypatch, capsys):
    _environment(monkeypatch)
    connection = _Connection(
        (
            "neondb",
            "b3s_pr71_app_runtime",
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
    connection = _Connection(
        (
            "neondb",
            "b3s_pr71_app_runtime",
            "jolly-river-32467750",
            "br-wrong",
        )
    )
    monkeypatch.setattr(target.psycopg, "connect", lambda *_a, **_kw: connection)

    with pytest.raises(SystemExit) as raised:
        target.main()

    assert "secret" not in str(raised.value)


def test_wrong_dsn_hostname_fails_before_database_connection(monkeypatch) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv(
        "B3S_DATABASE_URL",
        "postgresql://runtime:secret@wrong.example/neondb",
    )
    monkeypatch.setattr(
        target.psycopg,
        "connect",
        lambda *_a, **_kw: pytest.fail("database must not be contacted"),
    )

    with pytest.raises(SystemExit, match="target verification failed"):
        target.main()


@pytest.mark.parametrize(
    "query",
    [
        "host=evil.example&sslmode=require&channel_binding=require",
        "hostaddr=evil.example&sslmode=require&channel_binding=require",
        "options=-c%20neon.project_id%3Djolly-river-32467750&sslmode=require&channel_binding=require",
        "sslmode=disable",
        "sslmode=require&channel_binding=require&sslmode=require",
    ],
)
def test_effective_target_overrides_fail_before_connection(monkeypatch, query):
    _environment(monkeypatch)
    monkeypatch.setenv(
        "B3S_DATABASE_URL",
        (
            "postgresql://b3s_pr71_app_runtime:secret@"
            f"{target._EXPECTED['B3S_EXPECTED_NEON_ENDPOINT_HOST']}/neondb?{query}"
        ),
    )
    monkeypatch.setattr(
        target.psycopg,
        "connect",
        lambda *_a, **_kw: pytest.fail("database must not be contacted"),
    )

    with pytest.raises(SystemExit, match="target verification failed"):
        target.main()


def test_wrong_runtime_role_fails_after_database_connection(monkeypatch):
    _environment(monkeypatch)
    connection = _Connection(
        (
            "neondb",
            "neondb_owner",
            "jolly-river-32467750",
            "br-divine-star-aspobuer",
        )
    )
    monkeypatch.setattr(target.psycopg, "connect", lambda *_a, **_kw: connection)

    with pytest.raises(SystemExit, match="target verification failed"):
        target.main()


def test_ambient_libpq_overrides_are_absent_during_target_connection(monkeypatch):
    _environment(monkeypatch)
    monkeypatch.setenv("PGOPTIONS", "-c neon.project_id=wrong")
    connection = _Connection(
        (
            "neondb",
            "b3s_pr71_app_runtime",
            "jolly-river-32467750",
            "br-divine-star-aspobuer",
        )
    )
    observed = {}

    def connect(*_args, **_kwargs):
        observed["PGOPTIONS"] = os.environ.get("PGOPTIONS")
        return connection

    monkeypatch.setattr(target.psycopg, "connect", connect)
    assert target.main() == 0
    assert observed["PGOPTIONS"] is None
    assert os.environ["PGOPTIONS"] == "-c neon.project_id=wrong"


def test_plaintext_live_session_fails_closed(monkeypatch) -> None:
    _environment(monkeypatch)
    connection = _Connection(
        (
            "neondb",
            "b3s_pr71_app_runtime",
            "jolly-river-32467750",
            "br-divine-star-aspobuer",
        ),
        ssl_in_use=False,
    )
    monkeypatch.setattr(target.psycopg, "connect", lambda *_a, **_kw: connection)

    with pytest.raises(SystemExit, match="target verification failed"):
        target.main()


def test_connected_host_must_be_observable_and_exact(monkeypatch) -> None:
    _environment(monkeypatch)
    connection = _Connection(
        (
            "neondb",
            "b3s_pr71_app_runtime",
            "jolly-river-32467750",
            "br-divine-star-aspobuer",
        )
    )
    connection.info.host = "wrong.example"
    monkeypatch.setattr(target.psycopg, "connect", lambda *_a, **_kw: connection)

    with pytest.raises(SystemExit, match="target verification failed"):
        target.main()
