from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from src.storage.sqlite_store import SQLiteStore
from web import scan_runner
from web.api_v1.presenters import result_payload, status_payload
from web.app import app


TOKEN = "test-b3s-scanner-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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
            }
        ],
        "raw": {
            "schema_version": "sv9-flow-v1",
            "flow": {"interpretation_debug": {"prompt_version": "prompt-v1"}},
            "sv9": {"result": {"rubric_version": "rubric-v1"}},
        },
    }


def test_api_health_is_public():
    response = TestClient(app).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["service"] == "b3s-scanner-api"
    assert response.headers["x-b3s-api-version"] == "v1"


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


def test_api_reports_unconfigured_auth(monkeypatch):
    monkeypatch.delenv("B3S_SCANNER_API_TOKEN", raising=False)
    monkeypatch.delenv("BRAND3_SCANNER_API_TOKEN", raising=False)

    response = TestClient(app).get("/api/v1/capabilities", headers={"Authorization": "Bearer anything"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "api_auth_not_configured"


def test_create_scan_returns_async_contract_and_idempotency_header(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    captured = {}

    def fake_create(payload, *, client_id, idempotency_key):
        captured.update(payload=payload, client_id=client_id, idempotency_key=idempotency_key)
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
    assert response.json()["metadata"]["prompt_version"] == "prompt-v1"
    assert response.json()["components"][0]["tile_summary"] == {
        "passed": 1,
        "failed": 0,
        "insufficient_evidence": 1,
        "total": 2,
    }
    assert response.json()["components"][0]["evidence_refs"][0]["ref"] == "raw_inputs.1"
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
