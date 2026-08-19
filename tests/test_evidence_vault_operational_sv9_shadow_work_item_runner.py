from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import run_evidence_vault_operational_sv9_shadow_work_items as runner


_SHA = "a" * 64
_PARENT = "b" * 64
_TARGET = runner.pr71_vault_sv9_shadow_writer_target()
_TARGET_DSN = (
    f"postgresql://{_TARGET.user}:do-not-print@{_TARGET.host}/"
    "neondb?sslmode=require&channel_binding=require"
)


def _work_item(**extra):
    return {
        "domain": "example.com",
        "operational_packet_fingerprint": _SHA,
        "expected_parent_canonical_memory_version": _PARENT,
        **extra,
    }


def _receipt(**extra):
    return {
        "assessment_status": "available",
        "assessment_id": "private-uuid",
        "sv9_score": 99,
        "candidate_semantic_tiles": [{"evidence": "secret"}],
        **extra,
    }


def test_runner_dry_run_discovers_then_processes_with_sanitized_output(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[tuple] = []
    output = tmp_path / "work-items.json"

    class Repository:
        def __init__(self, dsn, *, connect, schema_policy):
            assert dsn == _TARGET_DSN
            assert callable(connect)
            assert schema_policy == "verify_head"

        def discover_evidence_vault_operational_sv9_shadow_work_items(self, *, limit):
            calls.append(("discover", limit))
            return [_work_item(packet_payload={"secret": "never output"})]

        def append_evidence_vault_operational_sv9_shadow_assessment(
            self,
            domain,
            **kwargs,
        ):
            calls.append(("append", domain, kwargs))
            return _receipt(), False

    monkeypatch.setattr(runner, "PostgresHistoryRepository", Repository)
    monkeypatch.setenv(runner.DATABASE_URL_ENV, _TARGET_DSN)

    assert runner.main(["--limit", "3", "--output", str(output)]) == 0

    payload = json.loads(output.read_text())
    assert payload == {
        "schema_version": runner.ADMIN_RUN_SCHEMA_VERSION,
        "mode": "dry_run",
        "limit": 3,
        "discovered_count": 1,
        "processed_count": 1,
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "work_items": [
            {
                "domain": "example.com",
                "operational_packet_fingerprint": _SHA,
                "expected_parent_canonical_memory_version": _PARENT,
                "outcome": "dry_run",
                "assessment_status": "available",
                "replayed": False,
            }
        ],
    }
    assert calls == [
        ("discover", 3),
        (
            "append",
            "example.com",
            {
                "operational_packet_fingerprint": _SHA,
                "expected_parent_canonical_memory_version": _PARENT,
                "dry_run": True,
            },
        ),
    ]
    encoded = output.read_text()
    for forbidden in (
        "do-not-print",
        "private-uuid",
        "packet_payload",
        "candidate_semantic_tiles",
        "sv9_score",
        "secret",
    ):
        assert forbidden not in encoded


def test_runner_append_passes_through_and_reports_discovery_append_replay(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[bool] = []
    output = tmp_path / "append.json"

    class Repository:
        def __init__(self, *_args, **_kwargs):
            pass

        def discover_evidence_vault_operational_sv9_shadow_work_items(self, *, limit):
            assert limit == 1
            return [_work_item()]

        def append_evidence_vault_operational_sv9_shadow_assessment(
            self,
            _domain,
            **kwargs,
        ):
            calls.append(kwargs["dry_run"])
            # Simulate a concurrent append after advisory discovery.
            return _receipt(), True

    monkeypatch.setattr(runner, "PostgresHistoryRepository", Repository)
    monkeypatch.setenv(runner.DATABASE_URL_ENV, _TARGET_DSN)

    assert runner.main(
        ["--limit", "1", "--append", "--output", str(output)]
    ) == 0
    payload = json.loads(output.read_text())
    assert payload["mode"] == "append"
    assert payload["work_items"][0]["outcome"] == "replayed"
    assert payload["work_items"][0]["replayed"] is True
    assert calls == [False]


def test_runner_fails_safely_when_adoption_race_rejects_append(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    output = tmp_path / "race.json"

    class Repository:
        def __init__(self, *_args, **_kwargs):
            pass

        def discover_evidence_vault_operational_sv9_shadow_work_items(self, *, limit):
            return [_work_item()]

        def append_evidence_vault_operational_sv9_shadow_assessment(self, *_args, **_kwargs):
            raise RuntimeError("postgresql://user:secret@private/packet-evidence")

    monkeypatch.setattr(runner, "PostgresHistoryRepository", Repository)
    monkeypatch.setenv(runner.DATABASE_URL_ENV, _TARGET_DSN)

    assert runner.main(["--append", "--output", str(output)]) == 1
    assert json.loads(output.read_text()) == {
        "status": "error",
        "error": "SV9 shadow work-item processing failed",
    }
    assert "secret" not in output.read_text()
    captured = capsys.readouterr()
    assert "SV9 shadow work-item processing failed" in captured.err
    assert "secret" not in captured.err


@pytest.mark.parametrize("limit", ["0", "101", "-1", "+1", "1.0", " 1"])
def test_runner_rejects_invalid_limit_before_repository(
    monkeypatch,
    tmp_path,
    limit,
) -> None:
    output = tmp_path / f"invalid-{len(limit)}.json"
    monkeypatch.setenv(runner.DATABASE_URL_ENV, _TARGET_DSN)
    monkeypatch.setattr(
        runner,
        "PostgresHistoryRepository",
        lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
    )

    assert runner.main(["--limit", limit, "--output", str(output)]) == 2
    assert json.loads(output.read_text()) == {
        "status": "error",
        "error": "invalid writer database target or limit",
    }


def test_runner_rejects_multi_item_append_before_repository(
    monkeypatch,
    tmp_path,
) -> None:
    output = tmp_path / "multi-append.json"
    monkeypatch.setenv(runner.DATABASE_URL_ENV, _TARGET_DSN)
    monkeypatch.setattr(
        runner,
        "PostgresHistoryRepository",
        lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
    )

    assert runner.main(
        ["--append", "--limit", "2", "--output", str(output)]
    ) == 2
    assert json.loads(output.read_text()) == {
        "status": "error",
        "error": "invalid writer database target or limit",
    }


def test_runner_requires_dedicated_writer_url_before_repository(
    monkeypatch,
    tmp_path,
) -> None:
    output = tmp_path / "missing-target.json"
    monkeypatch.delenv(runner.DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv("B3S_DATABASE_URL", _TARGET_DSN)
    monkeypatch.setattr(
        runner,
        "PostgresHistoryRepository",
        lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
    )

    assert runner.main(["--output", str(output)]) == 2
    assert json.loads(output.read_text()) == {
        "status": "error",
        "error": "invalid writer database target or limit",
    }


def test_runner_rejects_wrong_writer_target_and_invalid_output_before_repository(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setattr(
        runner,
        "PostgresHistoryRepository",
        lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
    )
    monkeypatch.setenv(
        runner.DATABASE_URL_ENV,
        (
            f"postgresql://wrong-user:secret@{_TARGET.host}/neondb"
            "?sslmode=verify-full"
        ),
    )
    target_output = tmp_path / "wrong-target.json"
    assert runner.main(["--output", str(target_output)]) == 2
    assert json.loads(target_output.read_text())["error"] == (
        "invalid writer database target or limit"
    )

    assert runner.main(["--output", str(tmp_path / "missing" / "out.json")]) == 2
    assert "invalid output path" in capsys.readouterr().out


def test_attested_connection_checks_live_tls_and_identity_before_return(
    monkeypatch,
) -> None:
    class Result:
        def fetchone(self):
            return {
                "database_name": _TARGET.database,
                "user_name": _TARGET.user,
                "project_id": _TARGET.project_id,
                "branch_id": _TARGET.branch_id,
            }

    class Connection:
        def __init__(self):
            self.info = SimpleNamespace(host=_TARGET.host)
            self.pgconn = SimpleNamespace(ssl_in_use=True)
            self.statements = []
            self.closed = False

        def execute(self, statement):
            self.statements.append(" ".join(str(statement).split()))
            return Result()

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(runner.psycopg, "connect", lambda *_args, **_kwargs: connection)

    assert runner._attested_connect(_TARGET)(_TARGET_DSN) is connection
    assert len(connection.statements) == 1
    assert "current_user AS user_name" in connection.statements[0]
    assert connection.closed is False


def test_attested_connection_closes_failed_live_target(monkeypatch) -> None:
    class Connection:
        def __init__(self):
            self.info = SimpleNamespace(host=_TARGET.host)
            self.pgconn = SimpleNamespace(ssl_in_use=False)
            self.closed = False

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(runner.psycopg, "connect", lambda *_args, **_kwargs: connection)

    with pytest.raises(Exception, match="not using TLS"):
        runner._attested_connect(_TARGET)(_TARGET_DSN)
    assert connection.closed is True
