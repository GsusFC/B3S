from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import tempfile
import time

import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from src.storage.sqlite_store import SQLiteStore
from web import scan_runner
from web.api_v1.presenters import result_payload, status_payload
from web.app import app


TOKEN = "test-b3s-scanner-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REVIEW_TOKEN = "test-b3s-evidence-review-token"
REVIEWER_ID = "gsus"
REVIEW_AUTH = {"Authorization": f"Bearer {REVIEW_TOKEN}"}
REVIEW_PACKET_FINGERPRINT = "9" * 64
LEAD_TOKEN = "test-eclipse-scan-token-0000000000000000"
LEAD_AUTH = {"Authorization": f"Bearer {LEAD_TOKEN}"}


def _configure_lead_client(monkeypatch, *, daily_scan_limit: int = 40) -> None:
    assert daily_scan_limit == 40
    monkeypatch.setenv("B3S_ECLIPSE_SCAN_API_TOKEN", LEAD_TOKEN)


def _configure_evidence_reviewer(monkeypatch) -> None:
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setenv("B3S_EVIDENCE_ADJUDICATION_TOKEN", REVIEW_TOKEN)
    monkeypatch.setenv("B3S_EVIDENCE_REVIEWER_ID", REVIEWER_ID)


def _running_scan(scan_id: str = "scan123") -> dict:
    return {
        "id": scan_id,
        "url": "https://example.com",
        "brand_name": "Example",
        "state": "running",
        "phase": "capture",
        "phases": [
            {"key": "capture", "label": "Capture", "state": "running"},
            {"key": "report", "label": "Report", "state": "pending"},
        ],
        "acquisition": [],
        "acquisition_gate": {"state": "pending"},
        "error": None,
        "started_at": "2026-07-20T10:00:00+00:00",
        "completed_at": None,
    }


def _report(scan_id: str = "scan123") -> dict:
    return {
        "id": scan_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-07-20T10:05:00+00:00",
        "pipeline_commit_sha": "a" * 40,
        "score": 72,
        "base_average": 7.2,
        "reliability_status": "reviewable",
        "detected_count": 1,
        "not_detected": ["vision"],
        "limitations": ["external proof limited"],
        "executive_reading": "A concise evidence-backed reading.",
        "coverage_acquisition": {
            "owned_url_count": 2,
            "external_proof_count": 1,
        },
        "acquisition_gate": {"state": "warning", "warnings": []},
        "absences": [{"ref": "absence.1", "block": "vision"}],
        "attempts": [{"provider": "exa", "status": "partial"}],
        "components": [
            {
                "key": "core_purpose",
                "label": "Propósito",
                "status": "scored",
                "score": 8,
                "scale": 10,
                "confidence": "high",
                "resumen": "Clear purpose.",
                "veredicto": "Strong.",
                "message": "Keep proving it.",
                "detected_content": "Purpose statement.",
                "evaluation_model": "test-model",
                "tile_profile": [
                    {"id": "PR1", "estado": "ok", "evidencia": "Proof"},
                    {"id": "PR2", "estado": "sin_evidencia", "motivo": "Missing"},
                ],
                "lit": 1,
                "off": 0,
                "blind": 1,
                "block": {
                    "coverage_status": "positive_evidence",
                    "refs": [
                        {
                            "ref": "raw_inputs.1",
                            "url": "https://example.com/about",
                            "snippet": "Purpose proof.",
                        }
                    ],
                },
            },
            {
                "key": "coherencia",
                "label": "Coherencia",
                "status": "scored",
                "score": 6,
                "scale": 10,
                "confidence": "high",
                "resumen": "The narrative is coherent across channels.",
                "veredicto": "Coherent.",
                "message": "Keep the cross-channel promise aligned.",
                "detected_content": "Owned and external narratives.",
                "evaluation_model": "test-model",
                "tile_profile": [
                    {
                        "id": "C7",
                        "estado": "ok",
                        "evidencia": "The owned and social narratives match.",
                    }
                ],
                "lit": 1,
                "off": 0,
                "blind": 0,
                "block": {"coverage_status": "positive_evidence", "refs": []},
            },
        ],
        "raw": {
            "schema_version": "sv9-flow-v1",
            "flow": {"interpretation_debug": {"prompt_version": "prompt-v1"}},
            "sv9": {"result": {"rubric_version": "rubric-v1"}},
        },
    }


def test_api_health_is_public(monkeypatch):
    monkeypatch.setenv("B3S_BUILD_SHA", "b" * 40)

    response = TestClient(app).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["service"] == "b3s-scanner-api"
    assert response.json()["commit_sha"] == "b" * 40
    assert response.headers["x-b3s-api-version"] == "v1"


def test_app_health_exposes_current_commit(monkeypatch):
    monkeypatch.setenv("B3S_BUILD_SHA", "c" * 40)

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "commit_sha": "c" * 40}


def test_api_requires_bearer_token(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)

    response = TestClient(app).get("/api/v1/capabilities", headers={"X-Request-ID": "request-123"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["x-request-id"] == "request-123"
    assert response.json() == {
        "error": {
            "code": "invalid_api_token",
            "message": "A valid B3S Scanner API Bearer token is required.",
            "request_id": "request-123",
        }
    }


def test_non_ascii_bearer_token_is_rejected_without_server_error(monkeypatch):
    from fastapi.security import HTTPAuthorizationCredentials
    from web.api_v1.auth import authenticate
    from web.api_v1.errors import ApiError

    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="é")

    with pytest.raises(ApiError) as caught:
        asyncio.run(authenticate(credentials))

    assert caught.value.status_code == 401
    assert caught.value.code == "invalid_api_token"


def test_api_reports_unconfigured_auth(monkeypatch):
    monkeypatch.delenv("B3S_SCANNER_API_TOKEN", raising=False)
    monkeypatch.delenv("BRAND3_SCANNER_API_TOKEN", raising=False)

    response = TestClient(app).get("/api/v1/capabilities", headers={"Authorization": "Bearer anything"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "api_auth_not_configured"


def test_create_scan_returns_async_contract_and_idempotency_header(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    captured = {}

    def fake_create(
        payload,
        *,
        client_id,
        idempotency_key,
        daily_scan_limit=None,
        require_idempotency=False,
        fail_blocked_scan=False,
    ):
        captured.update(
            payload=payload,
            client_id=client_id,
            idempotency_key=idempotency_key,
            daily_scan_limit=daily_scan_limit,
            require_idempotency=require_idempotency,
            fail_blocked_scan=fail_blocked_scan,
        )
        return _running_scan(), True

    monkeypatch.setattr("web.api_v1.router.create_scan_job", fake_create)

    response = TestClient(app).post(
        "/api/v1/scans",
        headers={**AUTH, "Idempotency-Key": "lead-example-2026-07-20"},
        json={"url": "https://example.com", "brand_name": "Example"},
    )

    assert response.status_code == 202
    assert response.headers["location"] == "/api/v1/scans/scan123"
    assert response.headers["retry-after"] == "5"
    assert response.headers["idempotent-replayed"] == "true"
    assert response.json()["status"] == "running"
    assert response.json()["progress"] == 0.25
    assert captured["idempotency_key"] == "lead-example-2026-07-20"
    assert captured["client_id"] == "environment-token"
    assert captured["daily_scan_limit"] is None
    assert captured["require_idempotency"] is False
    assert captured["fail_blocked_scan"] is False


def test_lead_capture_token_is_named_limited_and_quota_aware(monkeypatch):
    _configure_lead_client(monkeypatch)
    captured = {}

    def fake_create(
        payload,
        *,
        client_id,
        idempotency_key,
        daily_scan_limit=None,
        require_idempotency=False,
        fail_blocked_scan=False,
    ):
        captured.update(
            payload=payload,
            client_id=client_id,
            idempotency_key=idempotency_key,
            daily_scan_limit=daily_scan_limit,
            require_idempotency=require_idempotency,
            fail_blocked_scan=fail_blocked_scan,
        )
        return _running_scan(), False

    monkeypatch.setattr("web.api_v1.router.create_scan_job", fake_create)
    monkeypatch.setattr("web.api_v1.router.get_scan", lambda _scan_id: _running_scan())
    monkeypatch.setattr("web.api_v1.router.get_completed_report", lambda _scan_id: _report())
    monkeypatch.setattr("web.api_v1.router.list_reports_for_domain", lambda _domain: [])

    create_response = TestClient(app).post(
        "/api/v1/scans",
        headers={**LEAD_AUTH, "Idempotency-Key": "eclipse-scan:example.com:2026-08-13"},
        json={"url": "https://example.com", "brand_name": "Example"},
    )
    status_response = TestClient(app).get("/api/v1/scans/scan123", headers=LEAD_AUTH)
    result_response = TestClient(app).get("/api/v1/scans/scan123/result", headers=LEAD_AUTH)
    evidence_response = TestClient(app).get("/api/v1/scans/scan123/evidence", headers=LEAD_AUTH)
    history_response = TestClient(app).get(
        "/api/v1/brands/example.com/scans?limit=1",
        headers=LEAD_AUTH,
    )
    denied_cancel = TestClient(app).post("/api/v1/scans/scan123/cancel", headers=LEAD_AUTH)
    denied_shadow = TestClient(app).get(
        "/api/v1/brands/example.com/evidence-ledger-shadow",
        headers=LEAD_AUTH,
    )

    assert create_response.status_code == 202
    assert status_response.status_code == 200
    assert result_response.status_code == 200
    assert evidence_response.status_code == 200
    assert history_response.status_code == 200
    assert captured == {
        "payload": {
            "url": "https://example.com",
            "brand_name": "Example",
            "language": "es",
            "allow_degraded_fallback": False,
        },
        "client_id": "eclipse-scan",
        "idempotency_key": "eclipse-scan:example.com:2026-08-13",
        "daily_scan_limit": 40,
        "require_idempotency": True,
        "fail_blocked_scan": True,
    }
    assert denied_cancel.status_code == 403
    assert denied_shadow.status_code == 403


def test_lead_capture_token_requires_idempotency_key(monkeypatch):
    _configure_lead_client(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/scans",
        headers=LEAD_AUTH,
        json={"url": "https://example.com", "brand_name": "Example"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_weak_eclipse_token_config_fails_closed(monkeypatch):
    monkeypatch.setenv("B3S_ECLIPSE_SCAN_API_TOKEN", "weak-token")

    response = TestClient(app).get("/api/v1/scans/scan123", headers=LEAD_AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "api_token_configuration_invalid"


def test_invalid_or_duplicate_eclipse_token_config_fails_closed(monkeypatch):
    monkeypatch.setenv("B3S_ECLIPSE_SCAN_API_TOKEN", "é" * 32)
    invalid = TestClient(app).get("/api/v1/scans/scan123", headers=LEAD_AUTH)

    _configure_lead_client(monkeypatch)
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", LEAD_TOKEN)
    duplicate = TestClient(app).get("/api/v1/scans/scan123", headers=LEAD_AUTH)

    assert invalid.status_code == 503
    assert invalid.json()["error"]["code"] == "api_token_configuration_invalid"
    assert duplicate.status_code == 503
    assert duplicate.json()["error"]["code"] == "api_token_configuration_conflict"


def test_eclipse_blocked_scan_fails_without_waiting_for_user_decision(monkeypatch, tmp_path):
    from src import config

    db_path = str(tmp_path / "blocked-scan.sqlite3")
    monkeypatch.setattr(config, "BRAND3_DB_PATH", db_path)
    monkeypatch.setattr(
        scan_runner,
        "_capture_snapshot",
        lambda *_args, **_kwargs: {"run": {"id": 1}, "acquisition_steps": {}},
    )
    monkeypatch.setattr(
        scan_runner,
        "_build_acquisition_gate",
        lambda *_args, **_kwargs: {"state": "blocked", "can_continue": True},
    )
    monkeypatch.setattr(
        scan_runner,
        "_wait_for_acquisition_decision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Eclipse scans must not wait for a user decision")
        ),
    )

    scan_id = "eclipse-blocked-regression"
    scan_runner.start_scan(
        "https://example.com",
        allow_degraded_fallback=True,
        scan_id=scan_id,
        client_id="eclipse-scan",
        fail_blocked_scan=True,
    )
    deadline = time.monotonic() + 3
    status = None
    while time.monotonic() < deadline:
        status = scan_runner.scan_status(scan_id)
        if status and status.get("state") == "error":
            break
        time.sleep(0.02)

    assert status is not None
    assert status["state"] == "error"
    assert status["error_code"] == "acquisition_gate_blocked_for_client"
    assert "acquisition_gate_blocked_for_client" in status["error"]


def test_create_scan_rejects_unknown_fields_with_stable_error(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)

    response = TestClient(app).post(
        "/api/v1/scans",
        headers=AUTH,
        json={"url": "https://example.com", "mode": "advanced"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert response.json()["error"]["details"]["fields"][0]["field"] == "mode"
    assert response.json()["error"]["details"]["total_errors"] == 1


def test_unexpected_api_errors_keep_the_stable_envelope_without_leaking_details(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setattr("web.api_v1.router.get_scan", lambda _scan_id: _running_scan())
    monkeypatch.setattr(
        "web.api_v1.router.status_payload",
        lambda _status: (_ for _ in ()).throw(RuntimeError("secret database detail")),
    )

    response = TestClient(app, raise_server_exceptions=False).get("/api/v1/scans/scan123", headers=AUTH)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret database detail" not in response.text


def test_completed_result_has_stable_schema_and_etag(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setattr("web.api_v1.router.get_completed_report", lambda scan_id: _report(scan_id))
    client = TestClient(app)

    response = client.get("/api/v1/scans/scan123/result", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["metadata"]["schema_version"] == "b3s-scanner-result-v1"
    assert response.json()["metadata"]["pipeline_commit_sha"] == "a" * 40
    assert response.json()["metadata"]["prompt_version"] == "prompt-v1"
    assert response.json()["components"][0]["tile_summary"] == {
        "passed": 1,
        "failed": 0,
        "insufficient_evidence": 1,
        "total": 2,
    }
    assert response.json()["components"][0]["evidence_refs"][0]["ref"] == "raw_inputs.1"
    coherencia = next(
        component
        for component in response.json()["components"]
        if component["key"] == "coherencia"
    )
    assert coherencia["tiles"] == [
        {
            "id": "C7",
            "estado": "ok",
            "evidencia": "The owned and social narratives match.",
        }
    ]
    assert coherencia["score"] == 6
    etag = response.headers["etag"]

    cached = client.get("/api/v1/scans/scan123/result", headers={**AUTH, "If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""


def test_completed_result_exposes_insufficient_evidence_separately():
    report = _report("scan-insufficient")
    report["not_detected"] = ["values"]
    report["components"].append(
        {
            "key": "values",
            "label": "Valores",
            "status": "not_detected",
            "score": 0,
            "scale": 5,
            "block": {"coverage_status": "insufficient_acquisition", "refs": []},
            "tile_profile": [],
        }
    )

    payload = result_payload(report)

    assert payload["not_detected"] == ["values"]
    assert payload["insufficient_evidence"] == ["values"]


def test_completed_result_exposes_scan_time_stability_assessment():
    report = _report("scan-stability")
    report["canonical_status"] = "non_canonical"
    report["stability"] = {
        "classification": "evaluation_drift",
        "canonical_status": "non_canonical",
        "reason_codes": ["evaluation_changed_without_material_evidence_delta"],
    }

    payload = result_payload(report)

    assert payload["stability"] == report["stability"]
    assert payload["score"] == {
        "value": None,
        "raw_value": 72,
        "publishable": False,
        "retention_reason": "evaluation_drift",
        "scale": 100,
        "base_average": 7.2,
        "reliability_status": "reviewable",
    }
    assert payload["components"][0]["score"] is None
    assert payload["components"][0]["raw_score"] == 8
    assert payload["components"][0]["score_publishable"] is False


def test_completed_result_retains_score_after_acquisition_regression():
    report = _report("scan-regression")
    report["stability"] = {
        "classification": "acquisition_regression",
        "canonical_status": "non_canonical",
        "reason_codes": ["owned_evidence_lost"],
    }

    payload = result_payload(report)

    assert payload["score"]["value"] is None
    assert payload["score"]["raw_value"] == 72
    assert payload["score"]["retention_reason"] == "acquisition_regression"


def test_completed_result_fails_closed_when_stability_comparison_errors():
    report = _report("scan-comparison-error")
    report["stability"] = {
        "classification": "comparison_error",
        "canonical_status": "non_canonical",
        "reason_codes": ["evidence_comparison_failed"],
    }

    payload = result_payload(report)

    assert payload["score"]["value"] is None
    assert payload["score"]["retention_reason"] == "comparison_error"


def test_evidence_endpoint_separates_evidence_from_result(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setattr("web.api_v1.router.get_completed_report", lambda scan_id: _report(scan_id))

    response = TestClient(app).get("/api/v1/scans/scan123/evidence", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["totals"] == {
        "references": 1,
        "absences": 1,
        "attempts": 1,
        "owned_urls": 2,
        "external_proof": 1,
    }
    assert response.json()["references"][0]["component"] == "core_purpose"


def test_brand_history_is_paginated(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        "web.api_v1.router.list_reports_for_domain",
        lambda domain: [
            {"id": "one", "brand_name": "Example", "url": "https://example.com", "score": 70},
            {"id": "two", "brand_name": "Example", "url": "https://example.com", "score": 60},
        ],
    )

    response = TestClient(app).get("/api/v1/brands/example.com/scans?limit=1", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["domain"] == "example.com"
    assert response.json()["pagination"] == {"limit": 1, "offset": 0, "count": 1, "has_more": True}
    assert response.json()["selected_report_id"] == "one"
    assert response.json()["provisional_report_id"] == "one"
    assert response.json()["canonical_report_id"] is None
    assert response.json()["items"][0]["canonical_status"] == "non_canonical"
    assert response.json()["items"][0]["stability_classification"] == "stable"


def test_brand_history_retains_drifted_score_but_preserves_raw_audit_value(
    monkeypatch,
):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    shared = {
        "brand_name": "SoccerSolver",
        "url": "https://soccersolver.com",
        "reliability_status": "shadow",
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [
                            {
                                "ref": "web",
                                "source": "web",
                                "evidence_type": "raw_input",
                                "content": "Stable owned evidence.",
                                "url": "https://soccersolver.com",
                                "metadata": {"source_class": "owned_copy"},
                            }
                        ]
                    }
                }
            }
        },
    }
    monkeypatch.setattr(
        "web.api_v1.router.list_reports_for_domain",
        lambda _domain: [
            {
                **shared,
                "id": "new",
                "created_at": "2026-08-03T12:00:00Z",
                "score": 64,
                "components": [
                    {"key": "core_purpose", "status": "scored", "score": 4}
                ],
            },
            {
                **shared,
                "id": "old",
                "created_at": "2026-08-03T08:00:00Z",
                "score": 62,
                "components": [
                    {"key": "core_purpose", "status": "scored", "score": 9}
                ],
            },
        ],
    )

    response = TestClient(app).get(
        "/api/v1/brands/soccersolver.com/scans",
        headers=AUTH,
    )

    newest = response.json()["items"][0]
    assert newest["stability_classification"] == "evaluation_drift"
    assert newest["score"] is None
    assert newest["raw_score"] == 64
    assert newest["score_publishable"] is False


def test_evidence_ledger_shadow_endpoint_is_explicitly_non_authoritative(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        "web.api_v1.router.evidence_ledger_shadow_for_domain",
        lambda _domain: {
            "schema_version": "evidence-ledger-shadow-v1",
            "policy_version": "evidence-ledger-shadow-policy-v1",
            "mode": "shadow",
            "runtime_effect": False,
            "state_fingerprint": "a" * 64,
            "brand": {"name": "Example", "domain": "example.com"},
            "report_count": 2,
            "latest_report_id": "two",
            "summary": {
                "entry_count": 1,
                "state_counts": {"validation_candidate": 1},
            },
            "policy": {"automatic_validation": False},
            "warnings": ["validation_candidates_are_not_validated_facts"],
            "entries": [
                {
                    "evidence_fingerprint": "b" * 64,
                    "state": "validation_candidate",
                }
            ],
            "persistence": {"stored": True, "backend": "postgres"},
        },
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/evidence-ledger-shadow",
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["object"] == "evidence_ledger_shadow"
    assert response.json()["domain"] == "example.com"
    assert response.json()["runtime_effect"] is False
    assert response.json()["policy"]["automatic_validation"] is False
    assert response.json()["entries"][0]["state"] == "validation_candidate"
    assert response.json()["persistence"] == {
        "stored": True,
        "backend": "postgres",
    }


def test_evidence_memory_identity_v2_endpoint_is_non_authoritative(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        "web.api_v1.router.evidence_memory_identity_v2_for_domain",
        lambda _domain: {
            "schema_version": "evidence-memory-identity-v2",
            "policy_version": "evidence-memory-identity-policy-v4",
            "mode": "shadow",
            "runtime_effect": False,
            "authority": False,
            "state_fingerprint": "a" * 64,
            "brand": {"name": "Example", "domain": "example.com"},
            "report_count": 2,
            "latest_report_id": "two",
            "summary": {
                "document_count": 1,
                "passage_count": 2,
                "revision_candidate_count": 0,
            },
            "policy": {
                "automatic_validation": False,
                "url_equality_implies_revision": False,
            },
            "source_independence": {
                "schema_version": "evidence-source-independence-v3",
                "policy_version": "evidence-source-independence-policy-v3",
                "runtime_effect": False,
                "authority": False,
            },
            "warnings": ["revision_requires_explicit_claim_slot"],
            "entries": [
                {
                    "evidence_id": "b" * 64,
                    "document_id": "c" * 64,
                    "passage_id": "d" * 64,
                    "state": "not_reacquired",
                    "adjudication_state": "proposed",
                }
            ],
            "persistence": {
                "stored": False,
                "backend": "history_derived",
            },
        },
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/evidence-memory-identity-v2-shadow",
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["object"] == "evidence_memory_identity_v2_shadow"
    assert response.json()["domain"] == "example.com"
    assert response.json()["runtime_effect"] is False
    assert response.json()["authority"] is False
    assert response.json()["policy"]["url_equality_implies_revision"] is False
    assert response.json()["source_independence"]["authority"] is False
    assert response.json()["entries"][0]["adjudication_state"] == "proposed"
    assert response.json()["persistence"] == {
        "stored": False,
        "backend": "history_derived",
    }


def test_evidence_claim_memory_endpoint_is_non_authoritative(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        "web.api_v1.router.evidence_claim_memory_for_domain",
        lambda _domain: {
            "schema_version": "evidence-claim-memory-v1",
            "policy_version": "evidence-claim-memory-policy-v4",
            "mode": "shadow",
            "runtime_effect": False,
            "authority": False,
            "state_fingerprint": "a" * 64,
            "brand": {"name": "Example", "domain": "example.com"},
            "report_count": 2,
            "latest_report_id": "two",
            "claim_slot_producer": {
                "historical_backfill_version": (
                    "evidence-claim-historical-backfill-v1"
                ),
                "resolution_mode_counts": {"persisted_shadow_lane": 2},
                "runtime_effect": False,
                "authority": False,
            },
            "summary": {
                "claim_slot_count": 1,
                "claim_variant_count": 2,
                "relation_candidate_counts": {
                    "replacement_candidate": 1,
                },
            },
            "policy": {
                "automatic_claim_replacement": False,
                "explicit_stable_slot_declaration_required": True,
            },
            "warnings": ["claim_relation_candidates_are_not_adjudications"],
            "slots": [
                {
                    "claim_slot_id": "b" * 64,
                    "claim_slot_key": "promise.primary",
                    "relation_candidates": [
                        {
                            "relation": "replacement_candidate",
                            "adjudication_state": "proposed",
                            "runtime_effect": False,
                            "authority": False,
                        }
                    ],
                }
            ],
            "variants": [
                {
                    "claim_variant_id": "c" * 64,
                    "claim_slot_id": "b" * 64,
                    "state": "not_reacquired",
                },
                {
                    "claim_variant_id": "d" * 64,
                    "claim_slot_id": "b" * 64,
                    "state": "observed",
                },
            ],
            "occurrences": [
                {
                    "claim_occurrence_id": "e" * 64,
                    "claim_variant_id": "d" * 64,
                    "claim_slot_id": "b" * 64,
                }
            ],
            "persistence": {
                "stored": False,
                "backend": "history_derived",
            },
        },
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/evidence-claim-memory-shadow",
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "evidence_claim_memory_shadow"
    assert payload["domain"] == "example.com"
    assert payload["runtime_effect"] is False
    assert payload["authority"] is False
    assert payload["claim_slot_producer"]["resolution_mode_counts"] == {
        "persisted_shadow_lane": 2
    }
    assert payload["policy"]["automatic_claim_replacement"] is False
    assert payload["summary"]["claim_slot_count"] == 1
    assert payload["slots"][0]["relation_candidates"][0]["authority"] is False
    assert payload["persistence"] == {
        "stored": False,
        "backend": "history_derived",
    }


def test_evidence_claim_tile_ledger_endpoint_is_non_authoritative(
    monkeypatch,
):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        "web.api_v1.router.evidence_claim_tile_ledger_for_domain",
        lambda _domain: {
            "schema_version": "evidence-claim-tile-ledger-v1",
            "policy_version": "evidence-claim-tile-ledger-policy-v1",
            "mapping_version": "evidence-claim-tile-mapping-v1",
            "mode": "shadow",
            "runtime_effect": False,
            "authority": False,
            "state_fingerprint": "a" * 64,
            "brand": {"name": "Example", "domain": "example.com"},
            "report_count": 2,
            "latest_report_id": "two",
            "latest_mapping_series_id": "b" * 64,
            "summary": {
                "mapping_series_count": 1,
                "mapping_count": 1,
                "mapping_observation_count": 2,
            },
            "policy": {
                "literal_source_quote_required": True,
                "automatic_scoring_effect": False,
            },
            "warnings": [
                "shadow_only_no_scoring_or_selection_effect"
            ],
            "mapping_series": [
                {
                    "mapping_series_id": "b" * 64,
                    "evaluator_model": "evaluator-a",
                }
            ],
            "mappings": [
                {
                    "mapping_id": "c" * 64,
                    "mapping_series_id": "b" * 64,
                    "source_evidence_id": "d" * 64,
                    "claim_variant_id": "e" * 64,
                    "tile_key": "mission.M1",
                    "polarity": "supports",
                    "state": "repeated",
                    "runtime_effect": False,
                    "authority": False,
                }
            ],
            "persistence": {
                "stored": True,
                "backend": "postgres",
            },
            "reviewed_memory": {
                "available": True,
                "runtime_effect": False,
                "authority": False,
                "automatic_tile_effect": False,
                "automatic_scoring_effect": False,
                "accepted_mappings": [
                    {
                        "mapping_id": "c" * 64,
                        "tile_key": "mission.M1",
                    }
                ],
            },
        },
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/evidence-claim-tile-ledger-shadow",
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "evidence_claim_tile_ledger_shadow"
    assert payload["runtime_effect"] is False
    assert payload["authority"] is False
    assert payload["policy"]["automatic_scoring_effect"] is False
    assert payload["mappings"][0]["state"] == "repeated"
    assert payload["persistence"] == {
        "stored": True,
        "backend": "postgres",
    }
    assert payload["reviewed_memory"]["available"] is True
    assert (
        payload["reviewed_memory"]["automatic_scoring_effect"]
        is False
    )
    assert payload["reviewed_memory"]["accepted_mappings"][0][
        "tile_key"
    ] == "mission.M1"


def test_create_evidence_memory_adjudication_is_idempotent_and_non_authoritative(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)
    captured = {}
    event = _adjudication_event()

    def fake_create(
        domain,
        payload,
        *,
        client_id,
        reviewer_id,
        idempotency_key,
    ):
        captured.update(
            domain=domain,
            payload=payload,
            client_id=client_id,
            reviewer_id=reviewer_id,
            idempotency_key=idempotency_key,
        )
        return event, True

    monkeypatch.setattr(
        "web.api_v1.router.create_evidence_memory_adjudication",
        fake_create,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-memory-adjudications",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "review-example-proof-1",
        },
        json={
            "subject_id": "a" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "identity_confirmed",
            "rationale": "The source identifies the scanned brand.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 201
    assert response.headers["idempotent-replayed"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["runtime_effect"] is False
    assert response.json()["authority"] is False
    assert response.json()["event"]["decision"] == "accepted"
    assert captured["client_id"] == REVIEWER_ID
    assert captured["reviewer_id"] == REVIEWER_ID
    assert "reviewer" not in captured["payload"]
    assert captured["idempotency_key"] == "review-example-proof-1"
    assert captured["payload"]["expected_current_event_id"] is None


def test_scanner_token_cannot_adjudicate_evidence(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-memory-adjudications",
        headers={**AUTH, "Idempotency-Key": "scanner-cannot-review"},
        json={
            "subject_id": "a" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "identity_confirmed",
            "rationale": "The source identifies the scanned brand.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert response.json()["error"]["details"]["required_scope"] == (
        "evidence:adjudicate"
    )


def test_evidence_reviewer_cannot_impersonate_another_reviewer(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-memory-adjudications",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "reviewer-impersonation",
        },
        json={
            "subject_id": "a" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reviewer": "someone-else",
            "reason_code": "identity_confirmed",
            "rationale": "The source identifies the scanned brand.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert response.json()["error"]["details"]["fields"][0]["field"] == (
        "reviewer"
    )


def test_evidence_adjudication_token_requires_server_reviewer_identity(
    monkeypatch,
):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setenv("B3S_EVIDENCE_ADJUDICATION_TOKEN", REVIEW_TOKEN)
    monkeypatch.delenv("B3S_EVIDENCE_REVIEWER_ID", raising=False)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-memory-adjudications",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "reviewer-not-configured",
        },
        json={
            "subject_id": "a" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "identity_confirmed",
            "rationale": "The source identifies the scanned brand.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "evidence_reviewer_not_configured"
    )


def test_scanner_and_reviewer_tokens_must_be_different(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setenv("B3S_EVIDENCE_ADJUDICATION_TOKEN", TOKEN)
    monkeypatch.setenv("B3S_EVIDENCE_REVIEWER_ID", REVIEWER_ID)

    response = TestClient(app).get(
        "/api/v1/capabilities",
        headers=AUTH,
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "api_token_configuration_conflict"
    )


def test_create_evidence_memory_adjudication_requires_explicit_precondition(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-memory-adjudications",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "review-example-proof-2",
        },
        json={
            "subject_id": "a" * 64,
            "decision": "accepted",
            "reason_code": "identity_confirmed",
            "rationale": "The source identifies the scanned brand.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert response.json()["error"]["details"]["fields"][0]["field"] == (
        "expected_current_event_id"
    )


def test_create_evidence_memory_adjudication_requires_idempotency_key(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-memory-adjudications",
        headers=REVIEW_AUTH,
        json={
            "subject_id": "a" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "identity_confirmed",
            "rationale": "The source identifies the scanned brand.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_evidence_memory_adjudication_write_fails_closed_without_durable_store(
    monkeypatch,
):
    from src.services.evidence_memory_adjudication import (
        EvidenceMemoryAdjudicationUnavailableError,
    )

    _configure_evidence_reviewer(monkeypatch)

    def unavailable(*_args, **_kwargs):
        raise EvidenceMemoryAdjudicationUnavailableError("not configured")

    monkeypatch.setattr(
        "web.api_v1.service.append_evidence_memory_adjudication_for_domain",
        unavailable,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-memory-adjudications",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "review-example-proof-3",
        },
        json={
            "subject_id": "a" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "identity_confirmed",
            "rationale": "The source identifies the scanned brand.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "evidence_adjudication_store_unavailable"
    )


def test_evidence_memory_adjudication_rejects_stale_current_event(
    monkeypatch,
):
    from src.services.evidence_memory_adjudication import (
        EvidenceMemoryAdjudicationConflictError,
    )

    _configure_evidence_reviewer(monkeypatch)
    current_event_id = "00000000-0000-0000-0000-000000000009"

    def conflict(*_args, **_kwargs):
        raise EvidenceMemoryAdjudicationConflictError(
            "The evidence adjudication changed after it was read.",
            current_event_id=current_event_id,
        )

    monkeypatch.setattr(
        "web.api_v1.service.append_evidence_memory_adjudication_for_domain",
        conflict,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-memory-adjudications",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "review-example-proof-stale",
        },
        json={
            "subject_id": "a" * 64,
            "decision": "disputed",
            "expected_current_event_id": None,
            "reason_code": "identity_conflict",
            "rationale": "A concurrent review already changed this subject.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == (
        "adjudication_precondition_failed"
    )
    assert response.json()["error"]["details"]["current_event_id"] == (
        current_event_id
    )


def test_adjudication_service_derives_reviewer_from_authenticated_principal(
    monkeypatch,
):
    from web.api_v1.service import create_evidence_memory_adjudication

    captured = {}

    def append(_domain, command):
        captured["command"] = command
        return _adjudication_event(), False

    monkeypatch.setattr(
        "web.api_v1.service.append_evidence_memory_adjudication_for_domain",
        append,
    )

    create_evidence_memory_adjudication(
        "example.com",
        {
            "subject_id": "a" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "identity_confirmed",
            "rationale": "The source identifies the scanned brand.",
            "evaluator_version": "manual-review-v1",
        },
        client_id=REVIEWER_ID,
        reviewer_id=REVIEWER_ID,
        idempotency_key="server-derived-reviewer",
    )

    assert captured["command"].reviewer == REVIEWER_ID
    assert captured["command"].actor_id == REVIEWER_ID


def test_evidence_memory_adjudication_journal_exposes_superseded_events(
    monkeypatch,
):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    current = _adjudication_event()
    superseded = {
        **current,
        "id": "00000000-0000-0000-0000-000000000000",
        "effective_state": "superseded",
    }
    monkeypatch.setattr(
        "web.api_v1.router.get_evidence_memory_adjudications",
        lambda domain, **_kwargs: {
            "events": [current, superseded],
            "current": [current],
            "total": 2,
            "limit": 100,
            "offset": 0,
        },
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/evidence-memory-adjudications",
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["runtime_effect"] is False
    assert response.json()["authority"] is False
    assert [item["effective_state"] for item in response.json()["events"]] == [
        "accepted",
        "superseded",
    ]
    assert response.json()["current"] == [current]
    assert response.json()["pagination"]["has_more"] is False


def test_create_claim_reconciliation_is_idempotent_and_non_authoritative(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)
    captured = {}
    event = _claim_reconciliation_event()

    def fake_create(
        domain,
        payload,
        *,
        client_id,
        reviewer_id,
        idempotency_key,
    ):
        captured.update(
            domain=domain,
            payload=payload,
            client_id=client_id,
            reviewer_id=reviewer_id,
            idempotency_key=idempotency_key,
        )
        return event, True

    monkeypatch.setattr(
        "web.api_v1.router.create_evidence_claim_reconciliation",
        fake_create,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-reconciliations",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "review-claim-relation-1",
        },
        json={
            "subject_id": "f" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "replacement_confirmed",
            "rationale": "The newer variant replaces the earlier statement.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 201
    assert response.headers["idempotent-replayed"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["runtime_effect"] is False
    assert response.json()["authority"] is False
    assert response.json()["event"]["relation_type"] == (
        "replacement_candidate"
    )
    assert captured["client_id"] == REVIEWER_ID
    assert captured["reviewer_id"] == REVIEWER_ID
    assert "reviewer" not in captured["payload"]
    assert captured["idempotency_key"] == "review-claim-relation-1"


def test_scanner_token_cannot_reconcile_claims(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-reconciliations",
        headers={**AUTH, "Idempotency-Key": "scanner-cannot-reconcile"},
        json={
            "subject_id": "f" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "replacement_confirmed",
            "rationale": "The relation was reviewed.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert response.json()["error"]["details"]["required_scope"] == (
        "evidence:adjudicate"
    )


def test_claim_reconciliation_write_fails_closed_without_durable_store(
    monkeypatch,
):
    from src.services.evidence_claim_reconciliation import (
        EvidenceClaimReconciliationUnavailableError,
    )

    _configure_evidence_reviewer(monkeypatch)

    def unavailable(*_args, **_kwargs):
        raise EvidenceClaimReconciliationUnavailableError("not configured")

    monkeypatch.setattr(
        "web.api_v1.service.append_evidence_claim_reconciliation_for_domain",
        unavailable,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-reconciliations",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "claim-journal-unavailable",
        },
        json={
            "subject_id": "f" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "replacement_confirmed",
            "rationale": "The relation was reviewed.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "claim_reconciliation_store_unavailable"
    )


def test_claim_reconciliation_rejects_stale_current_event(monkeypatch):
    from src.services.evidence_claim_reconciliation import (
        EvidenceClaimReconciliationConflictError,
    )

    _configure_evidence_reviewer(monkeypatch)
    current_event_id = "00000000-0000-0000-0000-000000000019"

    def conflict(*_args, **_kwargs):
        raise EvidenceClaimReconciliationConflictError(
            "The claim reconciliation changed after it was read.",
            current_event_id=current_event_id,
        )

    monkeypatch.setattr(
        "web.api_v1.service.append_evidence_claim_reconciliation_for_domain",
        conflict,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-reconciliations",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "claim-reconciliation-stale",
        },
        json={
            "subject_id": "f" * 64,
            "decision": "disputed",
            "expected_current_event_id": None,
            "reason_code": "relation_conflict",
            "rationale": "A concurrent review already changed this relation.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == (
        "claim_reconciliation_precondition_failed"
    )
    assert response.json()["error"]["details"]["current_event_id"] == (
        current_event_id
    )


def test_claim_reconciliation_rejects_unknown_projected_relation(monkeypatch):
    from src.services.evidence_claim_reconciliation import (
        EvidenceClaimReconciliationNotFoundError,
    )

    _configure_evidence_reviewer(monkeypatch)

    def missing(*_args, **_kwargs):
        raise EvidenceClaimReconciliationNotFoundError(
            "The claim relation does not exist."
        )

    monkeypatch.setattr(
        "web.api_v1.service.append_evidence_claim_reconciliation_for_domain",
        missing,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-reconciliations",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "unknown-claim-relation",
        },
        json={
            "subject_id": "f" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "replacement_confirmed",
            "rationale": "The relation was reviewed.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "claim_relation_not_found"
    assert response.json()["error"]["details"]["subject_id"] == "f" * 64


def test_claim_reconciliation_service_binds_authenticated_reviewer(
    monkeypatch,
):
    from web.api_v1.service import create_evidence_claim_reconciliation

    captured = {}

    def append(_domain, command):
        captured["command"] = command
        return _claim_reconciliation_event(), False

    monkeypatch.setattr(
        "web.api_v1.service.append_evidence_claim_reconciliation_for_domain",
        append,
    )

    create_evidence_claim_reconciliation(
        "example.com",
        {
            "subject_id": "f" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "replacement_confirmed",
            "rationale": "The relation was manually reviewed.",
            "evaluator_version": "manual-review-v1",
        },
        client_id=REVIEWER_ID,
        reviewer_id=REVIEWER_ID,
        idempotency_key="server-derived-claim-reviewer",
    )

    assert captured["command"].reviewer == REVIEWER_ID
    assert captured["command"].actor_id == REVIEWER_ID


def test_claim_reconciliation_journal_exposes_superseded_events(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    current = _claim_reconciliation_event()
    superseded = {
        **current,
        "id": "00000000-0000-0000-0000-000000000000",
        "effective_state": "superseded",
    }
    monkeypatch.setattr(
        "web.api_v1.router.get_evidence_claim_reconciliations",
        lambda domain, **_kwargs: {
            "events": [current, superseded],
            "current": [current],
            "total": 2,
            "limit": 100,
            "offset": 0,
        },
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/evidence-claim-reconciliations",
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["runtime_effect"] is False
    assert response.json()["authority"] is False
    assert [item["effective_state"] for item in response.json()["events"]] == [
        "accepted",
        "superseded",
    ]
    assert response.json()["current"] == [current]
    assert response.json()["pagination"]["has_more"] is False


def test_create_claim_tile_review_is_attributable_and_non_authoritative(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)
    captured = {}
    event = _claim_tile_review_event()

    def fake_create(
        domain,
        payload,
        *,
        client_id,
        reviewer_id,
        idempotency_key,
    ):
        captured.update(
            domain=domain,
            payload=payload,
            client_id=client_id,
            reviewer_id=reviewer_id,
            idempotency_key=idempotency_key,
        )
        return event, True

    monkeypatch.setattr(
        "web.api_v1.router.create_evidence_claim_tile_review",
        fake_create,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-tile-reviews",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "claim-tile-review-1",
        },
        json={
            "subject_id": "c" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The claim satisfies this exact tile contract.",
            "evaluator_version": "manual-review-v1",
            "review_packet_fingerprint": REVIEW_PACKET_FINGERPRINT,
        },
    )

    assert response.status_code == 201
    assert response.headers["idempotent-replayed"] == "true"
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["runtime_effect"] is False
    assert payload["authority"] is False
    assert payload["automatic_tile_effect"] is False
    assert payload["automatic_scoring_effect"] is False
    assert payload["event"]["reviewer"] == REVIEWER_ID
    assert captured["reviewer_id"] == REVIEWER_ID
    assert captured["client_id"] == REVIEWER_ID
    assert "reviewer" not in captured["payload"]


def test_reviewer_can_register_and_read_exact_claim_tile_packet(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)
    packet = _claim_tile_review_packet()
    captured = {}

    def fake_register(domain):
        captured["registered_domain"] = domain
        return packet, True

    def fake_get(domain, packet_fingerprint):
        captured["read"] = (domain, packet_fingerprint)
        return packet

    monkeypatch.setattr(
        "web.api_v1.router."
        "register_evidence_claim_tile_review_packet",
        fake_register,
    )
    monkeypatch.setattr(
        "web.api_v1.router.get_evidence_claim_tile_review_packet",
        fake_get,
    )

    created = TestClient(app).post(
        "/api/v1/brands/example.com/"
        "evidence-claim-tile-review-packets",
        headers=REVIEW_AUTH,
    )

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert created.headers["idempotent-replayed"] == "true"
    assert created.json()["candidates"][0]["claim"]["content"] == (
        "Exact private claim"
    )
    assert captured["registered_domain"] == "example.com"

    read = TestClient(app).get(
        "/api/v1/brands/example.com/"
        "evidence-claim-tile-review-packets/"
        f"{REVIEW_PACKET_FINGERPRINT}",
        headers=REVIEW_AUTH,
    )

    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"
    assert read.json()["replayed"] is False
    assert captured["read"] == (
        "example.com",
        REVIEW_PACKET_FINGERPRINT,
    )


def test_reviewer_can_read_derived_claim_tile_review_queue(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)
    queue = _claim_tile_review_queue()
    captured = {}

    def fake_get(domain, packet_fingerprint):
        captured["read"] = (domain, packet_fingerprint)
        return queue

    monkeypatch.setattr(
        "web.api_v1.router.get_evidence_claim_tile_review_queue",
        fake_get,
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/"
        "evidence-claim-tile-review-packets/"
        f"{REVIEW_PACKET_FINGERPRINT}/queue",
        headers=REVIEW_AUTH,
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["object"] == "evidence_claim_tile_review_queue"
    assert payload["review_complete"] is False
    assert payload["summary"]["pending_review_count"] == 1
    assert payload["runtime_effect"] is False
    assert payload["automatic_scoring_effect"] is False
    assert captured["read"] == (
        "example.com",
        REVIEW_PACKET_FINGERPRINT,
    )


def test_scanner_token_cannot_read_claim_tile_review_queue(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).get(
        "/api/v1/brands/example.com/"
        "evidence-claim-tile-review-packets/"
        f"{REVIEW_PACKET_FINGERPRINT}/queue",
        headers=AUTH,
    )

    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == (
        "evidence:adjudicate"
    )


def test_scanner_token_cannot_read_private_claim_tile_packet(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).get(
        "/api/v1/brands/example.com/"
        "evidence-claim-tile-review-packets/"
        f"{REVIEW_PACKET_FINGERPRINT}",
        headers=AUTH,
    )

    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == (
        "evidence:adjudicate"
    )


def test_claim_tile_review_requires_idempotency_key(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-tile-reviews",
        headers=REVIEW_AUTH,
        json={
            "subject_id": "c" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The claim satisfies this exact tile contract.",
            "evaluator_version": "manual-review-v1",
            "review_packet_fingerprint": REVIEW_PACKET_FINGERPRINT,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_claim_tile_review_requires_packet_fingerprint(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-tile-reviews",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "claim-tile-review-without-packet",
        },
        json={
            "subject_id": "c" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The claim satisfies this exact tile contract.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 422
    assert "review_packet_fingerprint" in response.text


def test_scanner_token_cannot_review_claim_tile_mapping(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-tile-reviews",
        headers={
            **AUTH,
            "Idempotency-Key": "scanner-cannot-review-claim-tile",
        },
        json={
            "subject_id": "c" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The claim satisfies this exact tile contract.",
            "evaluator_version": "manual-review-v1",
            "review_packet_fingerprint": REVIEW_PACKET_FINGERPRINT,
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == (
        "evidence:adjudicate"
    )


def test_claim_tile_review_fails_closed_without_durable_store(
    monkeypatch,
):
    from src.services.evidence_claim_tile_review import (
        EvidenceClaimTileReviewUnavailableError,
    )

    _configure_evidence_reviewer(monkeypatch)

    def unavailable(*_args, **_kwargs):
        raise EvidenceClaimTileReviewUnavailableError("not configured")

    monkeypatch.setattr(
        "web.api_v1.service."
        "append_evidence_claim_tile_review_for_domain",
        unavailable,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-tile-reviews",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "claim-tile-journal-unavailable",
        },
        json={
            "subject_id": "c" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The claim satisfies this exact tile contract.",
            "evaluator_version": "manual-review-v1",
            "review_packet_fingerprint": REVIEW_PACKET_FINGERPRINT,
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "claim_tile_review_store_unavailable"
    )


def test_claim_tile_review_rejects_unknown_mapping(monkeypatch):
    from src.services.evidence_claim_tile_review import (
        EvidenceClaimTileReviewNotFoundError,
    )

    _configure_evidence_reviewer(monkeypatch)

    captured = {}

    def missing(_domain, command):
        captured["command"] = command
        raise EvidenceClaimTileReviewNotFoundError(
            "The claim-to-tile mapping does not exist."
        )

    monkeypatch.setattr(
        "web.api_v1.service."
        "append_evidence_claim_tile_review_for_domain",
        missing,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-claim-tile-reviews",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "unknown-claim-tile-mapping",
        },
        json={
            "subject_id": "c" * 64,
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The claim satisfies this exact tile contract.",
            "evaluator_version": "manual-review-v1",
            "review_packet_fingerprint": REVIEW_PACKET_FINGERPRINT,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == (
        "claim_tile_mapping_not_found"
    )
    assert response.json()["error"]["details"]["subject_id"] == "c" * 64
    assert (
        captured["command"].review_packet_fingerprint
        == REVIEW_PACKET_FINGERPRINT
    )
    assert captured["command"].evaluator_version == "manual-review-v1"


def test_claim_tile_review_journal_exposes_superseded_events(
    monkeypatch,
):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    current = _claim_tile_review_event()
    superseded = {
        **current,
        "id": "00000000-0000-0000-0000-000000000030",
        "event_id": "00000000-0000-0000-0000-000000000030",
        "effective_state": "superseded",
    }
    monkeypatch.setattr(
        "web.api_v1.router.get_evidence_claim_tile_reviews",
        lambda domain, **_kwargs: {
            "events": [current, superseded],
            "current": [current],
            "total": 2,
            "limit": 100,
            "offset": 0,
        },
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/evidence-claim-tile-reviews",
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["automatic_tile_effect"] is False
    assert payload["automatic_scoring_effect"] is False
    assert [item["effective_state"] for item in payload["events"]] == [
        "accepted",
        "superseded",
    ]
    assert payload["pagination"]["has_more"] is False


def test_scoring_memory_preview_exposes_candidate_and_reviewed_scores(
    monkeypatch,
):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        "web.api_v1.router.evidence_scoring_memory_preview_for_domain",
        lambda _domain: {
            "schema_version": "evidence-scoring-memory-preview-v1",
            "policy_version": (
                "evidence-scoring-memory-preview-policy-v1"
            ),
            "reviewed_shadow_version": (
                "evidence-scoring-reviewed-memory-shadow-v1"
            ),
            "mode": "shadow",
            "runtime_effect": False,
            "authority": False,
            "automatic_scoring_effect": False,
            "state_fingerprint": "a" * 64,
            "scoring": {"score_delta": 2},
            "reviewed_shadow": {
                "scoring": {"score_delta": 0},
            },
            "recovery_review_candidates": [
                {"candidate_fingerprint": "b" * 64}
            ],
            "recovery_review": {
                "summary": {"pending_count": 1}
            },
            "persistence": {
                "stored": True,
                "backend": "postgres_history_derived",
                "review_journal": "postgres",
            },
        },
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/evidence-scoring-memory-preview",
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scoring"]["score_delta"] == 2
    assert payload["reviewed_shadow"]["scoring"]["score_delta"] == 0
    assert payload["recovery_review"]["summary"]["pending_count"] == 1
    assert payload["runtime_effect"] is False
    assert payload["automatic_scoring_effect"] is False


def test_create_scoring_recovery_review_is_attributable_and_idempotent(
    monkeypatch,
):
    _configure_evidence_reviewer(monkeypatch)
    captured = {}
    event = _scoring_recovery_review_event()

    def fake_create(
        domain,
        payload,
        *,
        client_id,
        reviewer_id,
        idempotency_key,
    ):
        captured.update(
            domain=domain,
            payload=payload,
            client_id=client_id,
            reviewer_id=reviewer_id,
            idempotency_key=idempotency_key,
        )
        return event, True

    monkeypatch.setattr(
        "web.api_v1.router.create_evidence_scoring_recovery_review",
        fake_create,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/"
        "evidence-scoring-recovery-reviews",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "review-scoring-recovery-1",
        },
        json={
            "subject_id": "b" * 64,
            "case_id": "scoring-recovery-example-magnetism-mg1",
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The quote satisfies the tile contract.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 201
    assert response.headers["idempotent-replayed"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["automatic_scoring_effect"] is False
    assert response.json()["event"]["reviewer"] == REVIEWER_ID
    assert captured["reviewer_id"] == REVIEWER_ID
    assert captured["client_id"] == REVIEWER_ID
    assert "reviewer" not in captured["payload"]
    assert captured["idempotency_key"] == "review-scoring-recovery-1"


def test_scanner_token_cannot_review_scoring_recovery(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/"
        "evidence-scoring-recovery-reviews",
        headers={
            **AUTH,
            "Idempotency-Key": "scanner-cannot-review-scoring",
        },
        json={
            "subject_id": "b" * 64,
            "case_id": "scoring-recovery-example-magnetism-mg1",
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The quote satisfies the tile contract.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == (
        "evidence:adjudicate"
    )


def test_scoring_recovery_review_requires_idempotency_key(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/"
        "evidence-scoring-recovery-reviews",
        headers=REVIEW_AUTH,
        json={
            "subject_id": "b" * 64,
            "case_id": "scoring-recovery-example-magnetism-mg1",
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The quote satisfies the tile contract.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_scoring_recovery_review_fails_closed_without_durable_store(
    monkeypatch,
):
    from src.services.evidence_scoring_recovery_review import (
        EvidenceScoringRecoveryReviewUnavailableError,
    )

    _configure_evidence_reviewer(monkeypatch)

    def unavailable(*_args, **_kwargs):
        raise EvidenceScoringRecoveryReviewUnavailableError(
            "not configured"
        )

    monkeypatch.setattr(
        "web.api_v1.service."
        "append_evidence_scoring_recovery_review_for_domain",
        unavailable,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/"
        "evidence-scoring-recovery-reviews",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "scoring-journal-unavailable",
        },
        json={
            "subject_id": "b" * 64,
            "case_id": "scoring-recovery-example-magnetism-mg1",
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The quote satisfies the tile contract.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "scoring_recovery_review_store_unavailable"
    )


def test_scoring_recovery_review_rejects_stale_current_event(
    monkeypatch,
):
    from src.services.evidence_scoring_recovery_review import (
        EvidenceScoringRecoveryReviewConflictError,
    )

    _configure_evidence_reviewer(monkeypatch)
    current_event_id = "00000000-0000-0000-0000-000000000029"

    def conflict(*_args, **_kwargs):
        raise EvidenceScoringRecoveryReviewConflictError(
            "The recovery review changed after it was read.",
            current_event_id=current_event_id,
        )

    monkeypatch.setattr(
        "web.api_v1.service."
        "append_evidence_scoring_recovery_review_for_domain",
        conflict,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/"
        "evidence-scoring-recovery-reviews",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "scoring-recovery-stale",
        },
        json={
            "subject_id": "b" * 64,
            "case_id": "scoring-recovery-example-magnetism-mg1",
            "decision": "disputed",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_conflict",
            "rationale": "A concurrent review changed the recovery.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == (
        "scoring_recovery_review_precondition_failed"
    )
    assert response.json()["error"]["details"]["current_event_id"] == (
        current_event_id
    )


def test_scoring_recovery_review_rejects_unknown_projected_candidate(
    monkeypatch,
):
    from src.services.evidence_scoring_recovery_review import (
        EvidenceScoringRecoveryReviewNotFoundError,
    )

    _configure_evidence_reviewer(monkeypatch)

    def missing(*_args, **_kwargs):
        raise EvidenceScoringRecoveryReviewNotFoundError(
            "The scoring recovery does not exist."
        )

    monkeypatch.setattr(
        "web.api_v1.service."
        "append_evidence_scoring_recovery_review_for_domain",
        missing,
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/"
        "evidence-scoring-recovery-reviews",
        headers={
            **REVIEW_AUTH,
            "Idempotency-Key": "unknown-scoring-recovery",
        },
        json={
            "subject_id": "b" * 64,
            "case_id": "scoring-recovery-example-magnetism-mg1",
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The quote satisfies the tile contract.",
            "evaluator_version": "manual-review-v1",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == (
        "scoring_recovery_not_found"
    )
    assert response.json()["error"]["details"]["subject_id"] == "b" * 64


def test_scoring_recovery_service_binds_authenticated_reviewer(
    monkeypatch,
):
    from web.api_v1.service import (
        create_evidence_scoring_recovery_review,
    )

    captured = {}

    def append(_domain, command):
        captured["command"] = command
        return _scoring_recovery_review_event(), False

    monkeypatch.setattr(
        "web.api_v1.service."
        "append_evidence_scoring_recovery_review_for_domain",
        append,
    )

    create_evidence_scoring_recovery_review(
        "example.com",
        {
            "subject_id": "b" * 64,
            "case_id": "scoring-recovery-example-magnetism-mg1",
            "decision": "accepted",
            "expected_current_event_id": None,
            "reason_code": "tile_contract_satisfied",
            "rationale": "The quote satisfies the tile contract.",
            "evaluator_version": "manual-review-v1",
        },
        client_id=REVIEWER_ID,
        reviewer_id=REVIEWER_ID,
        idempotency_key="server-derived-scoring-reviewer",
    )

    assert captured["command"].reviewer == REVIEWER_ID
    assert captured["command"].actor_id == REVIEWER_ID
    assert captured["command"].case_id == (
        "scoring-recovery-example-magnetism-mg1"
    )


def test_scoring_recovery_review_journal_exposes_superseded_events(
    monkeypatch,
):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    current = _scoring_recovery_review_event()
    superseded = {
        **current,
        "id": "00000000-0000-0000-0000-000000000020",
        "event_id": "00000000-0000-0000-0000-000000000020",
        "effective_state": "superseded",
    }
    monkeypatch.setattr(
        "web.api_v1.router.get_evidence_scoring_recovery_reviews",
        lambda domain, **_kwargs: {
            "events": [current, superseded],
            "current": [current],
            "total": 2,
            "limit": 100,
            "offset": 0,
        },
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/"
        "evidence-scoring-recovery-reviews",
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["automatic_scoring_effect"] is False
    assert [item["effective_state"] for item in response.json()["events"]] == [
        "accepted",
        "superseded",
    ]
    assert response.json()["pagination"]["has_more"] is False


def test_failed_status_never_exposes_internal_exception_text():
    status = _running_scan()
    status.update(
        state="error",
        phase="error",
        error_code="scan_execution_failed",
        error="RuntimeError: bearer top-secret-token at /private/internal/path",
    )

    public = status_payload(status)
    assert public["failure"] == {
        "code": "scan_execution_failed",
        "message": "The scan failed during execution.",
        "retryable": False,
    }
    assert "top-secret-token" not in str(public)


def _adjudication_event() -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "subject_type": "evidence",
        "subject_id": "a" * 64,
        "sequence": 1,
        "decision": "accepted",
        "effective_state": "accepted",
        "supersedes_event_id": None,
        "schema_version": "evidence-memory-adjudication-v1",
        "policy_version": "evidence-memory-identity-adjudication-policy-v1",
        "evaluator_version": "manual-review-v1",
        "reviewer": REVIEWER_ID,
        "actor_id": REVIEWER_ID,
        "reason_code": "identity_confirmed",
        "rationale": "The source identifies the scanned brand.",
        "runtime_effect": False,
        "authority": False,
        "created_at": "2026-07-28T10:00:00+00:00",
    }


def _claim_reconciliation_event() -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000011",
        "subject_type": "claim_relation",
        "subject_id": "f" * 64,
        "relation_type": "replacement_candidate",
        "sequence": 1,
        "decision": "accepted",
        "effective_state": "accepted",
        "supersedes_event_id": None,
        "schema_version": "evidence-claim-reconciliation-v1",
        "policy_version": "evidence-claim-reconciliation-policy-v1",
        "evaluator_version": "manual-review-v1",
        "reviewer": REVIEWER_ID,
        "actor_id": REVIEWER_ID,
        "reason_code": "replacement_confirmed",
        "rationale": "The newer variant replaces the earlier statement.",
        "runtime_effect": False,
        "authority": False,
        "created_at": "2026-07-29T10:00:00+00:00",
    }


def _claim_tile_review_event() -> dict:
    event_id = "00000000-0000-0000-0000-000000000031"
    return {
        "id": event_id,
        "event_id": event_id,
        "subject_type": "claim_tile_mapping",
        "subject_id": "c" * 64,
        "case_id": "claim-tile-example-com-mission-m1-cccccccccccc",
        "mapping_id": "c" * 64,
        "mapping_series_id": "d" * 64,
        "source_evidence_id": "e" * 64,
        "claim_variant_id": "f" * 64,
        "component_key": "mission",
        "tile_id": "M1",
        "tile_key": "mission.M1",
        "polarity": "supports",
        "sequence": 1,
        "decision": "accepted",
        "effective_state": "accepted",
        "supersedes_event_id": None,
        "previous_event_id": None,
        "schema_version": "evidence-claim-tile-review-event-v2",
        "policy_version": "evidence-claim-tile-review-policy-v1",
        "evaluator_version": "manual-review-v1",
        "review_packet_fingerprint": REVIEW_PACKET_FINGERPRINT,
        "reviewer": REVIEWER_ID,
        "reviewer_id": REVIEWER_ID,
        "actor_id": REVIEWER_ID,
        "reason_code": "tile_contract_satisfied",
        "rationale": "The claim satisfies this exact tile contract.",
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
        "created_at": "2026-07-29T16:00:00+00:00",
        "reviewed_at": "2026-07-29T16:00:00+00:00",
    }


def _claim_tile_review_packet() -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000041",
        "packet_kind": "claim_tile",
        "packet_fingerprint": REVIEW_PACKET_FINGERPRINT,
        "candidate_fingerprint": "8" * 64,
        "manifest": {
            "review_packet_fingerprint": (
                REVIEW_PACKET_FINGERPRINT
            ),
            "candidate_fingerprint": "8" * 64,
        },
        "candidates": [
            {
                "case_id": (
                    "claim-tile-example-com-mission-m1-cccccccccccc"
                ),
                "subject_id": "c" * 64,
                "claim": {"content": "Exact private claim"},
            }
        ],
        "review_template": [
            {
                "case_id": (
                    "claim-tile-example-com-mission-m1-cccccccccccc"
                ),
                "subject_id": "c" * 64,
                "review_packet_fingerprint": (
                    REVIEW_PACKET_FINGERPRINT
                ),
                "decision": None,
            }
        ],
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
        "created_at": "2026-07-31T10:00:00+00:00",
    }


def _claim_tile_review_queue() -> dict:
    return {
        "schema_version": "evidence-claim-tile-review-queue-v1",
        "policy_version": "evidence-claim-tile-review-queue-policy-v1",
        "queue_kind": "claim_tile_human_review",
        "packet_fingerprint": REVIEW_PACKET_FINGERPRINT,
        "candidate_fingerprint": "8" * 64,
        "mapping_series_id": "d" * 64,
        "queue_fingerprint": "9" * 64,
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
        "review_complete": False,
        "summary": {
            "candidate_count": 1,
            "reviewed_count": 0,
            "pending_review_count": 1,
        },
        "pending_subject_ids": ["c" * 64],
        "reviewed_subject_ids": [],
        "items": [
            {
                "subject_id": "c" * 64,
                "review_status": "pending_review",
                "review_reason": "unreviewed_mapping",
            }
        ],
        "warnings": ["no_runtime_tile_or_scoring_effect"],
    }


def _scoring_recovery_review_event() -> dict:
    event_id = "00000000-0000-0000-0000-000000000021"
    return {
        "id": event_id,
        "event_id": event_id,
        "subject_type": "scoring_recovery",
        "subject_id": "b" * 64,
        "case_id": "scoring-recovery-example-magnetism-mg1",
        "candidate_fingerprint": "b" * 64,
        "sequence": 1,
        "decision": "accepted",
        "effective_state": "accepted",
        "supersedes_event_id": None,
        "previous_event_id": None,
        "schema_version": (
            "evidence-scoring-recovery-review-event-v1"
        ),
        "policy_version": (
            "evidence-scoring-recovery-review-policy-v1"
        ),
        "evaluator_version": "manual-review-v1",
        "reviewer": REVIEWER_ID,
        "reviewer_id": REVIEWER_ID,
        "actor_id": REVIEWER_ID,
        "reason_code": "tile_contract_satisfied",
        "rationale": "The quote satisfies the tile contract.",
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "created_at": "2026-07-29T15:00:00+00:00",
        "reviewed_at": "2026-07-29T15:00:00+00:00",
    }


def test_cancelled_scan_cannot_publish_or_transition_to_completed(monkeypatch):
    scan_id = "cancel-race"
    status = _running_scan(scan_id)
    status["state"] = "running"
    scan_runner._SCANS[scan_id] = status
    scan_runner._SCAN_EVENTS[scan_id] = scan_runner.threading.Event()

    def capture_and_cancel(_scan_id, _url, _brand_name):
        scan_runner.cancel_scan(scan_id)
        return {"acquisition_steps": {}, "run": {"id": 1}}

    published = []
    monkeypatch.setattr(scan_runner, "_capture_snapshot", capture_and_cancel)
    monkeypatch.setattr(scan_runner, "save_report", lambda report: published.append(report))
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda *_args, **_kwargs: None)

    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)

        assert scan_runner._SCANS[scan_id]["state"] == "cancelled"
        assert published == []
    finally:
        scan_runner._SCANS.pop(scan_id, None)
        scan_runner._SCAN_EVENTS.pop(scan_id, None)


def test_immutable_report_wins_over_stale_interrupted_job_status(monkeypatch):
    monkeypatch.setattr("web.api_v1.service.load_report", lambda _scan_id: _report("finished"))
    monkeypatch.setattr(
        "web.api_v1.service.scan_status",
        lambda _scan_id: {**_running_scan("finished"), "state": "error", "error_code": "process_restarted"},
    )

    from web.api_v1.service import get_scan

    status = get_scan("finished")

    assert status is not None
    assert status["state"] == "done"


def test_openapi_is_dedicated_to_v1_routes():
    response = TestClient(app).get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "B3S Scanner API"
    assert "/api/v1/scans" in response.json()["paths"]
    assert "/api/v1/brands/{domain}/evidence-ledger-shadow" in response.json()["paths"]
    assert (
        "/api/v1/brands/{domain}/evidence-memory-identity-v2-shadow"
        in response.json()["paths"]
    )
    assert (
        "/api/v1/brands/{domain}/evidence-claim-memory-shadow"
        in response.json()["paths"]
    )
    assert (
        "/api/v1/brands/{domain}/evidence-claim-tile-ledger-shadow"
        in response.json()["paths"]
    )
    assert (
        "/api/v1/brands/{domain}/evidence-claim-tile-reviews"
        in response.json()["paths"]
    )
    assert (
        "/api/v1/brands/{domain}/evidence-claim-tile-review-packets"
        in response.json()["paths"]
    )
    assert (
        "/api/v1/brands/{domain}/evidence-claim-tile-review-packets/"
        "{packet_fingerprint}"
        in response.json()["paths"]
    )
    assert (
        "/api/v1/brands/{domain}/evidence-claim-reconciliations"
        in response.json()["paths"]
    )
    assert (
        "/api/v1/brands/{domain}/evidence-scoring-memory-preview"
        in response.json()["paths"]
    )
    assert (
        "/api/v1/brands/{domain}/evidence-scoring-recovery-reviews"
        in response.json()["paths"]
    )
    assert "/scan" not in response.json()["paths"]
    assert "B3SScannerBearer" in response.json()["components"]["securitySchemes"]


def test_scanner_job_store_persists_idempotency_and_marks_restart_interruption():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "scanner.sqlite3")
        initial = _running_scan("durable123")
        initial["state"] = "accepted"
        store = SQLiteStore(db_path)
        created = store.reserve_scanner_api_job(
            scan_id="durable123",
            request_payload={"url": "https://example.com"},
            status_payload=initial,
            client_id="test-client",
            idempotency_key_hash="key-hash",
            request_fingerprint="request-a",
        )
        replay = store.reserve_scanner_api_job(
            scan_id="ignored",
            request_payload={"url": "https://example.com"},
            status_payload=initial,
            client_id="test-client",
            idempotency_key_hash="key-hash",
            request_fingerprint="request-a",
        )
        conflict = store.reserve_scanner_api_job(
            scan_id="ignored-again",
            request_payload={"url": "https://different.example"},
            status_payload=initial,
            client_id="test-client",
            idempotency_key_hash="key-hash",
            request_fingerprint="request-b",
        )
        store.close()

        reopened = SQLiteStore(db_path)
        interrupted_count = reopened.interrupt_incomplete_scanner_jobs()
        persisted = reopened.get_scanner_job_status("durable123")
        reopened.close()

    assert created["outcome"] == "created"
    assert replay == {"outcome": "replay", "scan_id": "durable123", "request_fingerprint": "request-a"}
    assert conflict["outcome"] == "conflict"
    assert interrupted_count == 1
    assert persisted["state"] == "error"
    assert persisted["error_code"] == "process_restarted"


def test_scanner_job_quota_is_atomic_per_client_and_replays_do_not_count():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "scanner-quota.sqlite3")
        store = SQLiteStore(db_path)
        first = store.reserve_scanner_api_job(
            scan_id="quota-first",
            request_payload={"url": "https://example.com"},
            status_payload={**_running_scan("quota-first"), "state": "accepted"},
            client_id="eclipse-scan",
            idempotency_key_hash="quota-key-1",
            request_fingerprint="request-a",
            daily_scan_limit=1,
        )
        replay = store.reserve_scanner_api_job(
            scan_id="quota-replay-ignored",
            request_payload={"url": "https://example.com"},
            status_payload={**_running_scan("quota-replay-ignored"), "state": "accepted"},
            client_id="eclipse-scan",
            idempotency_key_hash="quota-key-1",
            request_fingerprint="request-a",
            daily_scan_limit=1,
        )
        blocked = store.reserve_scanner_api_job(
            scan_id="quota-blocked",
            request_payload={"url": "https://other.example"},
            status_payload={**_running_scan("quota-blocked"), "state": "accepted"},
            client_id="eclipse-scan",
            idempotency_key_hash="quota-key-2",
            request_fingerprint="request-b",
            daily_scan_limit=1,
        )
        other_client = store.reserve_scanner_api_job(
            scan_id="other-client",
            request_payload={"url": "https://other.example"},
            status_payload={**_running_scan("other-client"), "state": "accepted"},
            client_id="b3s-leads",
            idempotency_key_hash="quota-key-3",
            request_fingerprint="request-b",
            daily_scan_limit=1,
        )
        store.close()

    assert first["outcome"] == "created"
    assert replay["outcome"] == "replay"
    assert blocked["outcome"] == "quota_exceeded"
    assert other_client["outcome"] == "created"


def test_scanner_job_quota_is_atomic_under_concurrent_reservations():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "scanner-quota-concurrent.sqlite3")

        open_lock = threading.Lock()
        ready = threading.Barrier(8)

        def reserve(index: int):
            # SQLiteStore negotiates WAL during construction; serialize only
            # that setup, then exercise the reservation transaction together.
            with open_lock:
                store = SQLiteStore(db_path)
            try:
                ready.wait()
                return store.reserve_scanner_api_job(
                    scan_id=f"concurrent-{index}",
                    request_payload={"url": "https://example.com"},
                    status_payload={**_running_scan(f"concurrent-{index}"), "state": "accepted"},
                    client_id="eclipse-scan",
                    idempotency_key_hash=f"concurrent-key-{index}",
                    request_fingerprint=f"request-{index}",
                    daily_scan_limit=3,
                )
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            reservations = list(pool.map(reserve, range(8)))

    assert sum(item["outcome"] == "created" for item in reservations) == 3
    assert sum(item["outcome"] == "quota_exceeded" for item in reservations) == 5


def test_scanner_job_quota_window_is_utc_half_open():
    from src.storage.scanner_api_jobs import _utc_day_window

    start, end = _utc_day_window("2026-08-13T23:59:59.999999+00:00")
    assert start == "2026-08-13T00:00:00+00:00"
    assert end == "2026-08-14T00:00:00+00:00"

    next_start, next_end = _utc_day_window("2026-08-14T00:00:00+00:00")
    assert next_start == end
    assert next_end == "2026-08-15T00:00:00+00:00"


def test_scanner_job_terminal_status_wins_over_stale_nonterminal_upserts():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "scanner-monotonic.sqlite3")
        stale_writer = SQLiteStore(db_path)
        terminal_writer = SQLiteStore(db_path)
        try:
            for terminal_state in ("cancelled", "error", "done"):
                scan_id = f"monotonic-{terminal_state}"
                stale_snapshot = _running_scan(scan_id)
                stale_writer.save_scanner_job_status(stale_snapshot)

                terminal_snapshot = {
                    **stale_snapshot,
                    "state": terminal_state,
                    "phase": terminal_state,
                    "completed_at": "2026-07-20T10:05:00+00:00",
                }
                terminal_writer.save_scanner_job_status(terminal_snapshot)

                stale_snapshot["phase"] = "interpret"
                stale_writer.save_scanner_job_status(stale_snapshot)
                persisted = terminal_writer.get_scanner_job_status(scan_id)
                assert persisted is not None
                assert persisted["state"] == terminal_state
                assert persisted["phase"] == terminal_state
                assert persisted["completed_at"] == terminal_snapshot["completed_at"]

                terminal_reassertion = {
                    **terminal_snapshot,
                    "phase": f"{terminal_state}-reasserted",
                    "terminal_reasserted": True,
                }
                terminal_writer.save_scanner_job_status(terminal_reassertion)
                persisted = stale_writer.get_scanner_job_status(scan_id)
                assert persisted is not None
                assert persisted["state"] == terminal_state
                assert persisted["phase"] == f"{terminal_state}-reasserted"
                assert persisted["terminal_reasserted"] is True
        finally:
            stale_writer.close()
            terminal_writer.close()


def test_restart_interruption_cas_preserves_jobs_terminalized_after_select():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "scanner-restart-cas.sqlite3")
        restart_writer = SQLiteStore(db_path)
        terminal_writer = SQLiteStore(db_path)
        terminal_states = {
            "restart-race-done": "done",
            "restart-race-error": "error",
            "restart-race-cancelled": "cancelled",
        }
        interruptible_scan_id = "restart-still-running"
        try:
            snapshots = {
                scan_id: _running_scan(scan_id) for scan_id in terminal_states
            }
            snapshots[interruptible_scan_id] = _running_scan(interruptible_scan_id)
            for snapshot in snapshots.values():
                restart_writer.save_scanner_job_status(snapshot)

            def terminalize_selected_jobs() -> None:
                for scan_id, terminal_state in terminal_states.items():
                    terminal_writer.save_scanner_job_status(
                        {
                            **snapshots[scan_id],
                            "state": terminal_state,
                            "phase": terminal_state,
                            "completed_at": "2026-07-20T10:05:00+00:00",
                        }
                    )

            class RaceBeforeRestartUpdate:
                def __init__(self, connection):
                    self.connection = connection
                    self.raced = False

                def execute(self, statement, parameters=()):
                    normalized = " ".join(statement.split())
                    if (
                        not self.raced
                        and normalized.startswith("UPDATE b3s_scanner_jobs")
                        and "phase='interrupted'" in normalized
                    ):
                        self.raced = True
                        terminalize_selected_jobs()
                    return self.connection.execute(statement, parameters)

                def __getattr__(self, name):
                    return getattr(self.connection, name)

            raced_connection = RaceBeforeRestartUpdate(restart_writer.conn)
            restart_writer.conn = raced_connection
            interrupted_count = restart_writer.interrupt_incomplete_scanner_jobs()

            assert raced_connection.raced is True
            assert interrupted_count == 1
            for scan_id, terminal_state in terminal_states.items():
                persisted = terminal_writer.get_scanner_job_status(scan_id)
                assert persisted is not None
                assert persisted["state"] == terminal_state
                assert persisted["phase"] == terminal_state
                assert persisted.get("error_code") != "process_restarted"

            interrupted = terminal_writer.get_scanner_job_status(
                interruptible_scan_id
            )
            assert interrupted is not None
            assert interrupted["state"] == "error"
            assert interrupted["phase"] == "interrupted"
            assert interrupted["error_code"] == "process_restarted"
        finally:
            restart_writer.close()
            terminal_writer.close()
