from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from web.app import app
from web.api_v1.models import VaultSv9ShadowDiagnosticItem

SCANNER_TOKEN = "scanner-token-" + ("s" * 32)
REVIEWER_TOKEN = "reviewer-token-" + ("r" * 32)
REVIEW_AUTH = {"Authorization": f"Bearer {REVIEWER_TOKEN}"}
SCANNER_AUTH = {"Authorization": f"Bearer {SCANNER_TOKEN}"}


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED", "true")
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", SCANNER_TOKEN)
    monkeypatch.setenv("B3S_EVIDENCE_ADJUDICATION_TOKEN", REVIEWER_TOKEN)
    monkeypatch.setenv("B3S_EVIDENCE_REVIEWER_ID", "diagnostic-reviewer")


def _diagnostics():
    return {
        "enabled": True,
        "available": True,
        "domain": "example.com",
        "limit": 10,
        "count": 1,
        "has_more": False,
        "items": [
            {
                "schema_version": "evidence-vault-operational-semantic-assessment-shadow-v1",
                "evaluation_identity": "a" * 64,
                "operational_packet_fingerprint": "b" * 64,
                "assessment_status": "available",
                "sv9_score": 73,
                "base_average": 7.3,
                "magnetism_capped": False,
                "assessment_fingerprint": "c" * 64,
                "score_fingerprint": "d" * 64,
                "semantic_provenance_fingerprint": "e" * 64,
                "verification_counts": {
                    "pending": 80,
                    "verified": 0,
                    "disputed": 0,
                    "stale": 0,
                    "unverifiable": 0,
                },
                "authority": False,
                "production_runtime_effect": False,
                "scanner_runtime_effect": False,
                "created_at": "2026-08-14T00:00:00+00:00",
            }
        ],
    }


def test_flag_off_is_hidden_after_reviewer_auth(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv("B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED", "false")
    response = TestClient(app).get(
        "/api/v1/brands/example.com/vault-sv9-shadow-diagnostics",
        headers=REVIEW_AUTH,
    )
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"


def test_scanner_token_cannot_read_reviewer_diagnostics(monkeypatch) -> None:
    _enable(monkeypatch)
    response = TestClient(app).get(
        "/api/v1/brands/example.com/vault-sv9-shadow-diagnostics",
        headers=SCANNER_AUTH,
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"


def test_reviewer_reads_sanitized_non_authoritative_diagnostics(
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        "web.api_v1.router.vault_sv9_shadow_diagnostics_for_domain",
        lambda _domain, *, limit: {**_diagnostics(), "limit": limit},
    )
    response = TestClient(app).get(
        "/api/v1/brands/example.com/vault-sv9-shadow-diagnostics?limit=10",
        headers=REVIEW_AUTH,
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    payload = response.json()
    assert payload["diagnostic_only"] is True
    assert payload["authority"] is False
    assert payload["public_scoring_effect"] is False
    assert payload["ranking_effect"] is False
    assert payload["items"][0]["sv9_score"] == 73
    encoded = response.text
    for forbidden in (
        "brand_id",
        "candidate_semantic_tiles",
        "assessment_output",
        "verification_requirements",
        "source_packet_id",
        "operational_packet_id",
    ):
        assert forbidden not in encoded


def test_unavailable_response_is_generic(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        "web.api_v1.router.vault_sv9_shadow_diagnostics_for_domain",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "available": False,
            "items": [],
            "message": "driver secret host detail",
        },
    )
    response = TestClient(app).get(
        "/api/v1/brands/example.com/vault-sv9-shadow-diagnostics",
        headers=REVIEW_AUTH,
    )
    assert response.status_code == 503
    assert "driver secret host detail" not in response.text
    assert "temporarily unavailable" in response.text


def test_hidden_route_is_absent_from_openapi(monkeypatch) -> None:
    _enable(monkeypatch)
    response = TestClient(app).get("/api/v1/openapi.json")
    assert response.status_code == 200
    assert not any(
        "vault-sv9-shadow-diagnostics" in path
        for path in response.json()["paths"]
    )


def test_diagnostic_response_model_rejects_bool_as_numeric() -> None:
    item = _diagnostics()["items"][0]
    with pytest.raises(ValidationError):
        VaultSv9ShadowDiagnosticItem.model_validate({**item, "sv9_score": True})
    with pytest.raises(ValidationError):
        VaultSv9ShadowDiagnosticItem.model_validate(
            {
                **item,
                "verification_counts": {
                    **item["verification_counts"],
                    "pending": True,
                },
            }
        )
