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

    assert projection is not None
    assert projection["score"] == 100
    assert projection["magnetism_capped"] is False


def test_legacy_projection_is_best_effort_and_unavailable_does_not_raise() -> None:
    assert _best_effort_legacy_operational_projection({"scoring_projection": {}}) is None


def test_runner_defaults_to_dry_run_and_whitelists_receipt(monkeypatch, capsys) -> None:
    calls: list[dict] = []

    class FakeRepository:
        def __init__(self, dsn: str, *, schema_policy: str) -> None:
            assert dsn == "postgresql://writer:secret@example.test/b3s"
            assert schema_policy == "verify_head"

        def append_evidence_vault_operational_sv9_shadow_assessment(self, domain, **kwargs):
            calls.append({"domain": domain, **kwargs})
            return (
                {
                    "schema_version": "shadow-v1",
                    "assessment_id": "assessment-id",
                    "evaluation_identity": _SHA,
                    "operational_packet_fingerprint": _SHA,
                    "source_candidate_packet_fingerprint": _SHA,
                    "expected_parent_canonical_memory_version": None,
                    "candidate_overlay_version": _SHA,
                    "assessment_status": "assessment_unavailable",
                    "assessment_fingerprint": None,
                    "score_fingerprint": None,
                    "sv9_score": None,
                    "base_average": None,
                    "magnetism_capped": None,
                    "legacy_operational_projection": {"score": 0},
                    "semantic_provenance_fingerprint": None,
                    "authority": False,
                    "production_runtime_effect": False,
                    "scanner_runtime_effect": False,
                    "created_at": None,
                    "candidate_semantic_tiles": [{"secret": "must not print"}],
                    "verification_requirements": {"secret": "must not print"},
                },
                False,
            )

    monkeypatch.setattr(runner, "PostgresHistoryRepository", FakeRepository)
    monkeypatch.setenv("B3S_DATABASE_URL", "postgresql://writer:secret@example.test/b3s")

    assert runner.main(
        [
            "--domain",
            "example.com",
            "--operational-packet-fingerprint",
            _SHA,
            "--expected-parent-canonical-memory-version",
            "none",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["dry_run"] is True
    assert payload["replayed"] is False
    assert payload["receipt"]["legacy_operational_projection"] == {"score": 0}
    assert "candidate_semantic_tiles" not in json.dumps(payload)
    assert "verification_requirements" not in json.dumps(payload)
    assert "secret" not in json.dumps(payload)
    assert calls[0]["dry_run"] is True
    assert calls[0]["expected_parent_canonical_memory_version"] is None


def test_runner_append_is_explicit(monkeypatch, capsys) -> None:
    calls: list[bool] = []

    class FakeRepository:
        def __init__(self, _dsn: str, *, schema_policy: str) -> None:
            assert schema_policy == "verify_head"

        def append_evidence_vault_operational_sv9_shadow_assessment(self, _domain, **kwargs):
            calls.append(kwargs["dry_run"])
            return ({"schema_version": "shadow-v1"}, True)

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
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is False
    assert payload["replayed"] is True
    assert calls == [False]


def test_runner_rejects_non_sha_or_missing_database_without_connecting(monkeypatch, capsys) -> None:
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
        ]
    ) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "invalid fingerprint arguments",
    }
