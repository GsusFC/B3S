from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.history.repository import PostgresHistoryRepository
from src.services.evidence_vault_operational_assessment_shadow import (
    EvidenceVaultOperationalAssessmentShadowError,
)
from web import report_store

_SHA = "a" * 64


def _row(**overrides):
    row = {
        "schema_version": "evidence-vault-operational-semantic-assessment-shadow-v1",
        "evaluation_identity": _SHA,
        "operational_packet_fingerprint": "b" * 64,
        "assessment_status": "available",
        "sv9_score": 73,
        "base_average": Decimal("7.3"),
        "magnetism_capped": False,
        "assessment_fingerprint": "c" * 64,
        "score_fingerprint": "d" * 64,
        "semantic_provenance_fingerprint": "e" * 64,
        "verification_pending_count": 80,
        "verification_verified_count": 0,
        "verification_disputed_count": 0,
        "verification_stale_count": 0,
        "verification_unverifiable_count": 0,
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": datetime(2026, 8, 14, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


class _Result:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        rendered = str(statement)
        self.statements.append((rendered, params))
        return _Result(self.rows if "shadow_diagnostics_v1" in rendered else [])


def _repository(rows):
    connection = _Connection(rows)
    repository = PostgresHistoryRepository(
        "postgresql://unused",
        connect=lambda *_args, **_kwargs: connection,
        schema_policy="verify_head",
    )
    repository._migrated = True
    return repository, connection


def test_reads_only_bounded_scalar_view_and_omits_private_material() -> None:
    repository, connection = _repository([_row(), _row(evaluation_identity="f" * 64)])

    result = repository.list_evidence_vault_operational_sv9_shadow_diagnostics(
        "https://Example.com/path",
        limit=1,
    )

    assert result["domain"] == "example.com"
    assert result["count"] == 1
    assert result["has_more"] is True
    assert result["items"][0]["sv9_score"] == 73
    assert result["items"][0]["base_average"] == 7.3
    assert set(result["items"][0]) == {
        "schema_version",
        "evaluation_identity",
        "operational_packet_fingerprint",
        "assessment_status",
        "sv9_score",
        "base_average",
        "magnetism_capped",
        "assessment_fingerprint",
        "score_fingerprint",
        "semantic_provenance_fingerprint",
        "verification_counts",
        "authority",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "created_at",
    }
    query, params = next(
        (statement, params)
        for statement, params in connection.statements
        if "shadow_diagnostics_v1" in statement
    )
    assert "evidence_vault_operational_sv9_shadow_assessments" not in query
    assert "candidate_semantic_tiles" not in query
    assert "verification_requirements" not in query
    assert "assessment_output" not in query
    assert "SELECT diagnostics.*" not in query
    assert params == ("b3s", "example.com", 2)
    assert connection.statements[0][0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )


@pytest.mark.parametrize("limit", [0, 21, True, 1.5])
def test_rejects_non_exact_or_out_of_bounds_limit(limit) -> None:
    repository, _connection = _repository([])
    with pytest.raises(
        EvidenceVaultOperationalAssessmentShadowError,
        match="diagnostic limit",
    ):
        repository.list_evidence_vault_operational_sv9_shadow_diagnostics(
            "example.com",
            limit=limit,
        )


@pytest.mark.parametrize(
    "status",
    [
        "stale_candidate_parent",
        "contradiction_requires_semantic_reassessment",
    ],
)
def test_unavailable_row_must_not_contain_score_material(status: str) -> None:
    repository, _connection = _repository(
        [_row(assessment_status=status, sv9_score=0)]
    )
    with pytest.raises(
        EvidenceVaultOperationalAssessmentShadowError,
        match="unavailable diagnostic contains score material",
    ):
        repository.list_evidence_vault_operational_sv9_shadow_diagnostics(
            "example.com"
        )


def test_exact_persisted_unavailable_status_is_returned_without_score() -> None:
    repository, _connection = _repository(
        [
            _row(
                assessment_status="stale_candidate_parent",
                sv9_score=None,
                base_average=None,
                magnetism_capped=None,
                assessment_fingerprint=None,
                score_fingerprint=None,
                semantic_provenance_fingerprint=None,
            )
        ]
    )
    result = repository.list_evidence_vault_operational_sv9_shadow_diagnostics(
        "example.com"
    )
    assert result["items"][0]["assessment_status"] == "stale_candidate_parent"
    assert result["items"][0]["sv9_score"] is None


def test_authority_and_verification_counts_fail_closed() -> None:
    repository, _connection = _repository([_row(authority=True)])
    with pytest.raises(
        EvidenceVaultOperationalAssessmentShadowError,
        match="authority boundary",
    ):
        repository.list_evidence_vault_operational_sv9_shadow_diagnostics(
            "example.com"
        )

    repository, _connection = _repository(
        [_row(verification_pending_count=79)]
    )
    with pytest.raises(
        EvidenceVaultOperationalAssessmentShadowError,
        match="verification counts",
    ):
        repository.list_evidence_vault_operational_sv9_shadow_diagnostics(
            "example.com"
        )


def test_web_gate_is_vault_only_default_off_and_opens_no_database(monkeypatch) -> None:
    monkeypatch.delenv("B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED", raising=False)
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(
        report_store,
        "_postgres_repository",
        lambda: (_ for _ in ()).throw(AssertionError("database opened")),
    )
    assert report_store.vault_sv9_shadow_diagnostics_for_domain("example.com") == {
        "enabled": False,
        "available": False,
        "items": [],
    }
    monkeypatch.setenv("B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED", "true")
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "production")
    assert report_store.vault_sv9_shadow_diagnostics_enabled() is False


def test_web_boundary_returns_generic_unavailable_without_fallback(monkeypatch, caplog) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED", "true")

    class Repository:
        def list_evidence_vault_operational_sv9_shadow_diagnostics(self, *_args, **_kwargs):
            raise RuntimeError("postgresql://user:secret@private-host/database")

    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())
    result = report_store.vault_sv9_shadow_diagnostics_for_domain("example.com")
    assert result["available"] is False
    assert "secret" not in result["message"]
    assert "private-host" not in result["message"]
    assert "fallback" not in result
    assert "secret" not in caplog.text
    assert "private-host" not in caplog.text
