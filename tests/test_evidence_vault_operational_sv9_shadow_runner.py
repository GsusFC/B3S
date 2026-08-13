from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import run_evidence_vault_operational_sv9_shadow as runner
from src.history.repository import _best_effort_legacy_operational_projection
from src.services.evidence_vault_canonical_core import build_tile_contract_registry


_SHA = "a" * 64


def _projection_rows(*, state: str = "sin_evidencia") -> list[dict[str, str]]:
    return [
        {
            "component_key": row["component_key"],
            "tile_id": row["tile_id"],
            "tile_key": row["tile_key"],
            "effective_scoring_state": state,
        }
        for row in build_tile_contract_registry()["tiles"]
    ]


def test_legacy_projection_uses_only_effective_scoring_state() -> None:
    packet = {
        "scoring_projection": {
            "tiles": _projection_rows(state="ok"),
            "ignored": "not read",
        },
        "accepted_memory": {"secret": "not read"},
        "candidate_overlay": {"secret": "not read"},
    }

    projection = _best_effort_legacy_operational_projection(packet)

    assert projection == {
        "availability": "available",
        "score": 100,
        "base_average": 10.0,
        "magnetism_capped": False,
    }


def test_legacy_projection_is_best_effort_and_unavailable_does_not_raise() -> None:
    assert _best_effort_legacy_operational_projection({"scoring_projection": {}}) == {
        "availability": "unavailable",
        "reason": "not_safely_derivable",
        "score": None,
    }


def _receipt(*, shadow_score: int | None = 5, legacy_score: int | None = 3) -> dict:
    return {
        "schema_version": "shadow-v1",
        "assessment_id": "assessment-id",
        "evaluation_identity": _SHA,
        "operational_packet_fingerprint": _SHA,
        "source_candidate_packet_fingerprint": _SHA,
        "expected_parent_canonical_memory_version": None,
        "candidate_overlay_version": _SHA,
        "assessment_status": "available" if shadow_score is not None else "assessment_unavailable",
        "assessment_fingerprint": _SHA if shadow_score is not None else None,
        "score_fingerprint": _SHA if shadow_score is not None else None,
        "sv9_score": shadow_score,
        "base_average": 1.0 if shadow_score is not None else None,
        "magnetism_capped": False if shadow_score is not None else None,
        "legacy_operational_projection": (
            {
                "availability": "available",
                "score": legacy_score,
                "base_average": 1.0,
                "magnetism_capped": False,
            }
            if legacy_score is not None
            else {
                "availability": "unavailable",
                "reason": "not_safely_derivable",
                "score": None,
            }
        ),
        "semantic_provenance_fingerprint": _SHA if shadow_score is not None else None,
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": None,
        "candidate_semantic_tiles": [{"secret": "must not print"}],
        "verification_requirements": {"secret": "must not print"},
    }


def test_runner_defaults_to_dry_run_and_emits_exact_sanitized_schema(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []
    output = tmp_path / "admin-run.json"

    class FakeRepository:
        def __init__(self, dsn: str, *, schema_policy: str) -> None:
            assert dsn == "postgresql://writer:secret@example.test/b3s"
            assert schema_policy == "verify_head"

        def append_evidence_vault_operational_sv9_shadow_assessment(self, domain, **kwargs):
            calls.append({"domain": domain, **kwargs})
            return _receipt(), False

    monkeypatch.setattr(runner, "PostgresHistoryRepository", FakeRepository)
    monkeypatch.setenv("B3S_DATABASE_URL", "postgresql://writer:secret@example.test/b3s")

    assert runner.main(
        [
            "--domain",
            "https://www.Example.com",
            "--operational-packet-fingerprint",
            _SHA,
            "--expected-parent-canonical-memory-version",
            "none",
            "--output",
            str(output),
        ]
    ) == 0

    payload = json.loads(output.read_text())
    assert set(payload) == {
        "schema_version",
        "input",
        "mode",
        "authority",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "replayed",
        "shadow_receipt",
        "comparison",
    }
    assert payload["schema_version"] == runner.ADMIN_RUN_SCHEMA_VERSION
    assert payload["input"] == {
        "domain": "example.com",
        "operational_packet_fingerprint": _SHA,
        "expected_parent_canonical_memory_version": None,
    }
    assert payload["mode"] == "dry_run"
    assert payload["authority"] is False
    assert payload["production_runtime_effect"] is False
    assert payload["scanner_runtime_effect"] is False
    assert payload["replayed"] is False
    assert payload["comparison"] == {
        "shadow_score": 5,
        "legacy_operational_projection": {
            "availability": "available",
            "score": 3,
            "base_average": 1.0,
            "magnetism_capped": False,
        },
        "score_delta": 2,
    }
    encoded = output.read_text()
    assert "candidate_semantic_tiles" not in encoded
    assert "verification_requirements" not in encoded
    assert "secret" not in encoded
    assert calls[0]["domain"] == "example.com"
    assert calls[0]["dry_run"] is True


def test_runner_append_is_explicit_and_writes_mode(tmp_path, monkeypatch) -> None:
    calls: list[bool] = []
    output = tmp_path / "append.json"

    class FakeRepository:
        def __init__(self, _dsn: str, *, schema_policy: str) -> None:
            assert schema_policy == "verify_head"

        def append_evidence_vault_operational_sv9_shadow_assessment(self, _domain, **kwargs):
            calls.append(kwargs["dry_run"])
            return _receipt(shadow_score=None, legacy_score=0), True

    monkeypatch.setattr(runner, "PostgresHistoryRepository", FakeRepository)
    assert runner.main(
        [
            "--domain",
            "example.com",
            "--operational-packet-fingerprint",
            _SHA,
            "--expected-parent-canonical-memory-version",
            _SHA,
            "--append",
            "--database-url",
            "postgresql://writer:secret@example.test/b3s",
            "--output",
            str(output),
        ]
    ) == 0

    payload = json.loads(output.read_text())
    assert payload["mode"] == "append"
    assert payload["replayed"] is True
    assert payload["comparison"] == {
        "shadow_score": None,
        "legacy_operational_projection": {
            "availability": "available",
            "score": 0,
            "base_average": 1.0,
            "magnetism_capped": False,
        },
    }
    assert calls == [False]


def test_runner_rejects_non_sha_or_missing_database_without_connecting(tmp_path, monkeypatch) -> None:
    output = tmp_path / "invalid.json"
    monkeypatch.delenv("B3S_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        runner,
        "PostgresHistoryRepository",
        lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
    )

    assert runner.main(
        [
            "--domain",
            "example.com",
            "--operational-packet-fingerprint",
            "not-a-sha",
            "--expected-parent-canonical-memory-version",
            "none",
            "--output",
            str(output),
        ]
    ) == 2
    assert json.loads(output.read_text()) == {
        "status": "error",
        "error": "invalid fingerprint arguments",
    }


def test_runner_validates_domain_dsn_and_output_before_connecting(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        runner,
        "PostgresHistoryRepository",
        lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
    )

    assert runner.main(
        [
            "--domain",
            "https:///missing-host",
            "--operational-packet-fingerprint",
            _SHA,
            "--expected-parent-canonical-memory-version",
            "none",
            "--database-url",
            "postgresql://writer:secret@example.test/b3s",
            "--output",
            str(tmp_path / "domain-invalid.json"),
        ]
    ) == 2
    assert json.loads((tmp_path / "domain-invalid.json").read_text())["error"] == (
        "invalid domain argument"
    )

    assert runner.main(
        [
            "--domain",
            "example.com",
            "--operational-packet-fingerprint",
            _SHA,
            "--expected-parent-canonical-memory-version",
            "none",
            "--database-url",
            "not-a-postgresql-url",
            "--output",
            str(tmp_path / "dsn-invalid.json"),
        ]
    ) == 2
    assert json.loads((tmp_path / "dsn-invalid.json").read_text())["error"] == (
        "invalid PostgreSQL URL"
    )

    for invalid_domain in (
        "https://example.com/path",
        "ftp://example.com",
        "example\\.com",
        "example .com",
        "example%2ecom",
    ):
        output = tmp_path / f"domain-{len(invalid_domain)}.json"
        assert runner.main(
            [
                "--domain",
                invalid_domain,
                "--operational-packet-fingerprint",
                _SHA,
                "--expected-parent-canonical-memory-version",
                "none",
                "--database-url",
                "postgresql://writer:secret@example.test/b3s",
                "--output",
                str(output),
            ]
        ) == 2
        assert json.loads(output.read_text())["error"] == "invalid domain argument"

    for invalid_dsn in (
        "postgresql://writer:secret@bad\\host/b3s",
        "postgresql://writer:secret@bad host/b3s",
        "postgresql://writer:secret@example.test/b3s?x=%ZZ",
        "postgresql://writer:sec%ZZret@example.test/b3s",
        "postgresql://writer:secret@example.test%ZZ/b3s",
        "postgresql://writer:secret@example.test/b%ZZs",
        "postgresql://writer:secret@example.test/b3s\t",
    ):
        output = tmp_path / f"dsn-{len(invalid_dsn)}.json"
        assert runner.main(
            [
                "--domain",
                "example.com",
                "--operational-packet-fingerprint",
                _SHA,
                "--expected-parent-canonical-memory-version",
                "none",
                "--database-url",
                invalid_dsn,
                "--output",
                str(output),
            ]
        ) == 2
        assert json.loads(output.read_text())["error"] == "invalid PostgreSQL URL"
    assert capsys.readouterr().out == ""


def test_output_replace_is_atomic_and_preserves_existing_file_on_failure(tmp_path, monkeypatch) -> None:
    output = tmp_path / "existing.json"
    output.write_text("old-content", encoding="utf-8")

    class FakeRepository:
        def __init__(self, _dsn: str, *, schema_policy: str) -> None:
            assert schema_policy == "verify_head"

        def append_evidence_vault_operational_sv9_shadow_assessment(self, _domain, **_kwargs):
            return _receipt(), False

    monkeypatch.setattr(runner, "PostgresHistoryRepository", FakeRepository)
    monkeypatch.setattr(runner.os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fail")))

    assert runner.main(
        [
            "--domain",
            "example.com",
            "--operational-packet-fingerprint",
            _SHA,
            "--expected-parent-canonical-memory-version",
            "none",
            "--database-url",
            "postgresql://writer:secret@example.test/b3s",
            "--output",
            str(output),
        ]
    ) == 1
    assert output.read_text(encoding="utf-8") == "old-content"
