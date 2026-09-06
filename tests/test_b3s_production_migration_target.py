from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

from scripts import migrate_b3s_history_postgres as migration_cli
from scripts import run_b3s_production_migration as production_controller
from scripts import verify_b3s_history_postgres as verification_cli
from scripts.b3s_production_database_target import (
    B3SProductionTargetError,
    b3s_production_migration_target,
    require_b3s_production_connection,
    validate_b3s_production_dsn,
)
from src.history import repository as repository_module


_TARGET_ENVIRONMENT = {
    "B3S_PRODUCTION_DATABASE": "b3s_production",
    "B3S_PRODUCTION_HOST": "ep-production.example.test",
    "B3S_PRODUCTION_NEON_PROJECT_ID": "b3s-production-project",
    "B3S_PRODUCTION_NEON_BRANCH_ID": "br-production",
    "B3S_PRODUCTION_MIGRATION_ROLE": "b3s_production_migrator",
}
_TARGET = b3s_production_migration_target(environ=_TARGET_ENVIRONMENT)
_BASE_TARGET_DSN = (
    "postgresql://b3s_production_migrator:do-not-print@"
    "ep-production.example.test/b3s_production?sslmode=verify-full&channel_binding=require"
)
_TARGET_DSN = f"{_BASE_TARGET_DSN}&sslrootcert=/tmp/b3s-production-test-root.pem"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Connection:
    def __init__(self, row, *, ssl_in_use=True, expose_pgconn=True):
        self.row = row
        self.info = SimpleNamespace(host=_TARGET.host)
        if expose_pgconn:
            self.pgconn = SimpleNamespace(ssl_in_use=ssl_in_use)
        self.statements: list[str] = []

    def execute(self, statement, *_args, **_kwargs):
        normalized = " ".join(str(statement).split())
        self.statements.append(normalized)
        if "current_user AS user_name" in normalized:
            return SimpleNamespace(fetchone=lambda: self.row)
        raise AssertionError(f"unexpected statement after target check: {normalized}")


def _target_row(*, branch_id: str = _TARGET.branch_id):
    return {
        "database_name": _TARGET.database,
        "user_name": _TARGET.user,
        "project_id": _TARGET.project_id,
        "branch_id": branch_id,
    }


def _set_production_target_environment(monkeypatch) -> None:
    for key, value in _TARGET_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("missing", sorted(_TARGET_ENVIRONMENT))
def test_production_target_requires_each_protected_identity_value(missing: str) -> None:
    environment = dict(_TARGET_ENVIRONMENT)
    environment.pop(missing)

    with pytest.raises(B3SProductionTargetError, match="incomplete"):
        b3s_production_migration_target(environ=environment)


def test_production_profile_attests_same_connection_before_migrate(monkeypatch, capsys):
    connection = _Connection(_target_row())
    observed: dict[str, object] = {}

    class Repository:
        def __init__(self, dsn):
            observed["dsn"] = dsn

        def migrate(self, *, connection_preflight):
            observed["libpq"] = {
                name: os.environ.get(name)
                for name in (
                    "PGHOST",
                    "PGOPTIONS",
                    "PGSERVICE",
                    "PGSSLMODE",
                    "PGSSLROOTCERT",
                )
            }
            connection_preflight(connection)
            observed["preflight_connection"] = connection
            return ["035_evidence_vault_sv9_empty_relation_witness.sql"]

    _set_production_target_environment(monkeypatch)
    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _TARGET_DSN)
    monkeypatch.setenv("PGHOST", "wrong.example.test")
    monkeypatch.setenv("PGOPTIONS", "-c neon.branch_id=br-wrong")
    monkeypatch.setenv("PGSERVICE", "wrong-service")
    monkeypatch.setenv("PGSSLMODE", "disable")
    monkeypatch.setenv("PGSSLROOTCERT", "/wrong/root.pem")
    monkeypatch.setattr(repository_module, "PostgresHistoryRepository", Repository)
    monkeypatch.setattr(
        repository_module,
        "_migration_manifest",
        lambda: [
            (
                "035",
                "035_evidence_vault_sv9_empty_relation_witness.sql",
                "a" * 64,
                "SELECT 1",
            )
        ],
    )

    assert migration_cli.main(["--target-profile", "b3s-production"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert observed["dsn"] == _TARGET_DSN
    assert observed["libpq"] == {
        "PGHOST": None,
        "PGOPTIONS": None,
        "PGSERVICE": None,
        "PGSSLMODE": None,
        "PGSSLROOTCERT": None,
    }
    assert observed["preflight_connection"] is connection
    assert os.environ["PGHOST"] == "wrong.example.test"
    assert os.environ["PGOPTIONS"] == "-c neon.branch_id=br-wrong"
    assert os.environ["PGSERVICE"] == "wrong-service"
    assert os.environ["PGSSLMODE"] == "disable"
    assert os.environ["PGSSLROOTCERT"] == "/wrong/root.pem"
    assert "current_user AS user_name" in connection.statements[0]
    assert _TARGET_DSN not in json.dumps(payload)


@pytest.mark.parametrize(
    "dsn",
    [
        _TARGET_DSN.replace("ep-production.example.test", "wrong.example.test"),
        _TARGET_DSN.replace("/b3s_production?", "/wrong?"),
        _TARGET_DSN.replace("b3s_production_migrator:", "wrong_user:"),
        _TARGET_DSN.replace("sslmode=verify-full", "sslmode=require"),
        _TARGET_DSN + "&host=wrong.example.test",
        _TARGET_DSN + "&sslmode=verify-full",
    ],
)
def test_production_profile_rejects_effective_target_drift(dsn: str) -> None:
    with pytest.raises(B3SProductionTargetError):
        validate_b3s_production_dsn(dsn, target=_TARGET)


def test_production_profile_requires_an_explicit_dsn_trust_root() -> None:
    with pytest.raises(B3SProductionTargetError, match="TLS contract"):
        validate_b3s_production_dsn(_BASE_TARGET_DSN, target=_TARGET)


@pytest.mark.parametrize(
    ("expose_pgconn", "ssl_in_use"),
    [(False, True), (True, False), (True, None)],
)
def test_production_profile_rejects_missing_or_inactive_tls(
    expose_pgconn: bool, ssl_in_use: bool | None
) -> None:
    connection = _Connection(
        _target_row(),
        expose_pgconn=expose_pgconn,
        ssl_in_use=ssl_in_use,
    )

    with pytest.raises(B3SProductionTargetError, match="not using TLS"):
        require_b3s_production_connection(connection, target=_TARGET)

    assert connection.statements == []


def test_wrong_production_session_has_no_advisory_lock_or_ddl() -> None:
    connection = _Connection(_target_row(branch_id="br-wrong"))
    repository = repository_module.PostgresHistoryRepository(
        _TARGET_DSN,
        connect=lambda *_args, **_kwargs: nullcontext(connection),
    )

    with pytest.raises(B3SProductionTargetError):
        repository.migrate(
            connection_preflight=lambda conn: require_b3s_production_connection(
                conn,
                target=_TARGET,
            )
        )

    assert len(connection.statements) == 1
    assert "current_user AS user_name" in connection.statements[0]
    assert not any(
        statement.startswith(
            (
                "SELECT pg_advisory_xact_lock",
                "CREATE ",
                "ALTER ",
                "DROP ",
                "INSERT ",
                "UPDATE ",
                "DELETE ",
            )
        )
        for statement in connection.statements
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_name", "wrong-database"),
        ("user_name", "wrong-user"),
        ("project_id", "wrong-project"),
        ("branch_id", "wrong-branch"),
    ],
)
def test_production_profile_rejects_each_live_identity_mismatch(field: str, value: str) -> None:
    row = _target_row()
    row[field] = value
    connection = _Connection(row)

    with pytest.raises(B3SProductionTargetError, match="session does not match"):
        require_b3s_production_connection(connection, target=_TARGET)

    assert len(connection.statements) == 1
    assert "current_user AS user_name" in connection.statements[0]


def test_postflight_preflight_runs_after_the_read_only_boundary(monkeypatch) -> None:
    class VerificationConnection:
        def __init__(self):
            self.info = SimpleNamespace(host=_TARGET.host)
            self.pgconn = SimpleNamespace(ssl_in_use=True)
            self.statements: list[str] = []

        def execute(self, statement, *_args, **_kwargs):
            normalized = " ".join(str(statement).split())
            self.statements.append(normalized)
            if normalized.startswith("SET TRANSACTION"):
                return SimpleNamespace()
            if "current_user AS user_name" in normalized:
                return SimpleNamespace(fetchone=lambda: _target_row())
            if "FROM b3s_history.schema_migrations" in normalized:
                return SimpleNamespace(
                    fetchall=lambda: [
                        {
                            "version": "035",
                            "filename": "035_evidence_vault_sv9_empty_relation_witness.sql",
                            "checksum": "a" * 64,
                        }
                    ]
                )
            raise AssertionError(f"unexpected verification statement: {normalized}")

    connection = VerificationConnection()
    repository = repository_module.PostgresHistoryRepository(
        _TARGET_DSN,
        schema_policy="verify_head",
        connect=lambda *_args, **_kwargs: nullcontext(connection),
    )
    monkeypatch.setattr(
        repository_module,
        "_migration_manifest",
        lambda: [
            (
                "035",
                "035_evidence_vault_sv9_empty_relation_witness.sql",
                "a" * 64,
                "SELECT 1",
            )
        ],
    )

    repository.verify_migration_head(
        connection_preflight=lambda conn: require_b3s_production_connection(
            conn,
            target=_TARGET,
        )
    )

    assert connection.statements[0].startswith("SET TRANSACTION")
    assert "current_user AS user_name" in connection.statements[1]
    assert not any(
        statement.startswith(("SELECT pg_advisory_xact_lock", "CREATE ", "ALTER "))
        for statement in connection.statements
    )


def test_production_profile_forbids_generic_url_arguments(monkeypatch, capsys) -> None:
    _set_production_target_environment(monkeypatch)
    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _BASE_TARGET_DSN)

    assert (
        migration_cli.main(
            [
                "--target-profile",
                "b3s-production",
                "--database-url",
                "postgresql://do-not-print",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "target profiles require B3S_MIGRATION_DATABASE_URL",
    }

    assert (
        verification_cli.main(
            [
                "--target-profile",
                "b3s-production",
                "--database-url",
                "postgresql://do-not-print",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "target profiles require B3S_MIGRATION_DATABASE_URL",
    }


def test_production_postflight_attests_same_connection(monkeypatch, capsys) -> None:
    connection = _Connection(_target_row())
    observed: dict[str, object] = {}

    class Repository:
        def __init__(self, dsn, *, schema_policy):
            observed["dsn"] = dsn
            observed["schema_policy"] = schema_policy

        def verify_migration_head(self, *, connection_preflight):
            observed["pgoptions"] = os.environ.get("PGOPTIONS")
            connection_preflight(connection)
            observed["preflight_connection"] = connection

    _set_production_target_environment(monkeypatch)
    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _TARGET_DSN)
    monkeypatch.setenv("PGOPTIONS", "-c neon.branch_id=br-wrong")
    monkeypatch.setattr(repository_module, "PostgresHistoryRepository", Repository)
    monkeypatch.setattr(
        repository_module,
        "_migration_manifest",
        lambda: [
            (
                "035",
                "035_evidence_vault_sv9_empty_relation_witness.sql",
                "a" * 64,
                "SELECT 1",
            )
        ],
    )

    assert verification_cli.main(["--target-profile", "b3s-production"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "count": 1,
        "head": "035_evidence_vault_sv9_empty_relation_witness.sql",
        "status": "ok",
    }
    assert observed == {
        "dsn": _TARGET_DSN,
        "schema_policy": "verify_head",
        "pgoptions": None,
        "preflight_connection": connection,
    }
    assert os.environ["PGOPTIONS"] == "-c neon.branch_id=br-wrong"


def test_production_controller_emits_a_bounded_sanitized_receipt(monkeypatch, capsys) -> None:
    observed: list[list[str]] = []
    root_paths: list[Path] = []
    root_certificate = "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"

    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _BASE_TARGET_DSN)
    monkeypatch.setenv("B3S_PRODUCTION_TLS_ROOT_CERT", root_certificate)

    def migrate(argv):
        observed.append(argv)
        root_path = Path(dict(parse_qsl(urlsplit(os.environ["B3S_MIGRATION_DATABASE_URL"]).query))["sslrootcert"])
        root_paths.append(root_path)
        assert root_path.read_text(encoding="utf-8") == root_certificate
        assert root_path.stat().st_mode & 0o777 == 0o600
        print(
            json.dumps(
                {
                    "status": "ok",
                    "head": "035_evidence_vault_sv9_empty_relation_witness.sql",
                    "count": 35,
                    "applied": ["035_evidence_vault_sv9_empty_relation_witness.sql"],
                }
            )
        )
        return 0

    def verify(argv):
        observed.append(argv)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "head": "035_evidence_vault_sv9_empty_relation_witness.sql",
                    "count": 35,
                }
            )
        )
        return 0

    monkeypatch.setattr(production_controller.migration_cli, "main", migrate)
    monkeypatch.setattr(production_controller.verification_cli, "main", verify)

    assert production_controller.main([]) == 0

    assert observed == [
        ["--target-profile", "b3s-production"],
        ["--target-profile", "b3s-production"],
    ]
    assert json.loads(capsys.readouterr().out) == {
        "applied_count": 1,
        "count": 35,
        "head": "035_evidence_vault_sv9_empty_relation_witness.sql",
        "status": "ok",
    }
    assert not root_paths[0].exists()
    assert os.environ["B3S_MIGRATION_DATABASE_URL"] == _BASE_TARGET_DSN


def test_production_controller_does_not_echo_underlying_failure(monkeypatch, capsys) -> None:
    root_certificate = "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"

    def migrate(_argv):
        print(json.dumps({"status": "error", "error": "postgresql://do-not-print@wrong"}))
        return 1

    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _BASE_TARGET_DSN)
    monkeypatch.setenv("B3S_PRODUCTION_TLS_ROOT_CERT", root_certificate)
    monkeypatch.setattr(production_controller.migration_cli, "main", migrate)

    assert production_controller.main([]) == 1

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "error",
        "error": "production migration failed",
    }
    assert "do-not-print" not in output


def test_production_controller_fails_closed_when_trust_root_is_missing(monkeypatch, capsys) -> None:
    called = False

    def migrate(_argv):
        nonlocal called
        called = True
        return 0

    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _BASE_TARGET_DSN)
    monkeypatch.delenv("B3S_PRODUCTION_TLS_ROOT_CERT", raising=False)
    monkeypatch.setattr(production_controller.migration_cli, "main", migrate)

    assert production_controller.main([]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "production migration trust root unavailable",
    }
    assert not called


@pytest.mark.parametrize(
    "certificate",
    [
        "unexpected-prefix\n-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----",
        "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\nunexpected-suffix",
    ],
)
def test_production_controller_fails_closed_when_trust_root_is_not_a_bounded_pem(
    monkeypatch, capsys, certificate: str
) -> None:
    called = False

    def migrate(_argv):
        nonlocal called
        called = True
        return 0

    monkeypatch.setenv("B3S_MIGRATION_DATABASE_URL", _BASE_TARGET_DSN)
    monkeypatch.setenv("B3S_PRODUCTION_TLS_ROOT_CERT", certificate)
    monkeypatch.setattr(production_controller.migration_cli, "main", migrate)

    assert production_controller.main([]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "production migration trust root unavailable",
    }
    assert not called


def test_production_controller_rejects_arguments_without_echoing_them(capsys) -> None:
    assert production_controller.main(["--database-url", "postgresql://do-not-print"]) == 2

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "error",
        "error": "production migration controller accepts no arguments",
    }
    assert "do-not-print" not in output


def test_production_controller_entrypoint_is_importable_without_pythonpath() -> None:
    environment = dict(os.environ)
    for name in (
        "B3S_MIGRATION_DATABASE_URL",
        *_TARGET_ENVIRONMENT,
    ):
        environment.pop(name, None)

    result = subprocess.run(
        [sys.executable, "scripts/run_b3s_production_migration.py"],
        cwd=_PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "status": "error",
        "error": "production migration failed",
    }
    assert result.stderr == ""


def test_production_controller_entrypoint_rejects_unexpected_real_argv() -> None:
    environment = dict(os.environ)
    for name in (
        "B3S_MIGRATION_DATABASE_URL",
        "B3S_PRODUCTION_TLS_ROOT_CERT",
        *_TARGET_ENVIRONMENT,
    ):
        environment.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_b3s_production_migration.py",
            "--database-url",
            "postgresql://do-not-print",
        ],
        cwd=_PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "status": "error",
        "error": "production migration controller accepts no arguments",
    }
    assert "do-not-print" not in result.stdout
    assert result.stderr == ""
