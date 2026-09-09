from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
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


def _assessment_report(scan_id: str, *, unavailable: bool = False) -> dict:
    """Build a report through the production SV9 composition boundary."""

    from src.sv9.aggregator import aggregate
    from src.sv9.models import ComponentResult, STATUS_NOT_EVALUATED
    from tests.test_vault_sv9_parity import _components, _flow_payload

    components = _components()
    if unavailable:
        components["vision"] = ComponentResult(
            component="vision",
            status=STATUS_NOT_EVALUATED,
            error="timeout",
        )
    result = aggregate(
        components,
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    return scan_runner._compose_report(
        scan_id,
        "https://example.com",
        "Example",
        _flow_payload(result),
    )


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


def test_completed_result_rejects_tampered_canonical_component_summary():
    from src.services.scanner_report_assessment import ScannerReportAssessmentError

    report = _assessment_report("tampered-api-summary")
    report["components"][0]["lit"] += 1

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_component_lit_mismatch:report",
    ):
        result_payload(report)


def test_completed_result_rejects_boolean_component_summary_alias():
    from src.services.scanner_report_assessment import ScannerReportAssessmentError

    report = _assessment_report("boolean-api-summary")
    report["components"][0]["lit"] = False

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_component_lit_mismatch:report",
    ):
        result_payload(report)


def test_completed_result_rejects_type_drift_in_duplicate_assessment():
    from src.services.scanner_report_assessment import ScannerReportAssessmentError

    report = _assessment_report("duplicate-assessment-api")
    report["raw"]["sv9"]["assessment"]["component_breakdown"][0][
        "sin_evidencia_count"
    ] = False

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_duplicate_mismatch",
    ):
        result_payload(report)


def test_completed_result_rejects_stripped_assessment_envelope():
    from src.services.scanner_report_assessment import ScannerReportAssessmentError

    report = _assessment_report("stripped-api-assessment")
    report.pop("sv9_assessment")
    report.pop("assessment_fingerprint")
    report.pop("score_fingerprint")

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_envelope_missing",
    ):
        result_payload(report)


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


def test_brand_history_identity_matches_available_result_projection(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    report = _assessment_report("available-history")
    monkeypatch.setattr(
        "web.api_v1.router.list_reports_for_domain",
        lambda _domain: [report],
    )

    history_response = TestClient(app).get(
        "/api/v1/brands/example.com/scans",
        headers=AUTH,
    )

    assert history_response.status_code == 200
    history_item = history_response.json()["items"][0]
    result = result_payload(report)

    assert history_item["assessment_availability"] == "available"
    assert history_item["score"] == result["score"]["value"]
    assert history_item["assessment_fingerprint"] == result["metadata"][
        "assessment_fingerprint"
    ]
    assert history_item["score_fingerprint"] == result["metadata"]["score_fingerprint"]


def test_brand_history_legacy_and_unavailable_have_no_assessment_identity(monkeypatch):
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", TOKEN)
    legacy = _report("legacy-history")
    unavailable = _assessment_report("unavailable-history", unavailable=True)
    monkeypatch.setattr(
        "web.api_v1.router.list_reports_for_domain",
        lambda _domain: [legacy, unavailable],
    )

    response = TestClient(app).get(
        "/api/v1/brands/example.com/scans",
        headers=AUTH,
    )

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}

    assert items["legacy-history"]["assessment_availability"] == "legacy"
    assert items["legacy-history"]["assessment_fingerprint"] is None
    assert items["legacy-history"]["score_fingerprint"] is None

    assert items["unavailable-history"]["assessment_availability"] == "unavailable"
    assert items["unavailable-history"]["score"] is None
    assert items["unavailable-history"]["raw_score"] == unavailable["score"]
    assert items["unavailable-history"]["assessment_fingerprint"] is None
    assert items["unavailable-history"]["score_fingerprint"] is None


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


@pytest.mark.parametrize("flag,environment,pipeline,visible", [("false", "vault", "true", False), ("true", "production", "true", False), ("true", "vault", "false", False), ("true", "vault", "true", True)])
def test_openapi_resume_paths_follow_runtime_gate(monkeypatch, flag, environment, pipeline, visible):
    monkeypatch.setenv("B3S_VAULT_EXACT_RESUME_API_ENABLED", flag)
    monkeypatch.setenv("BRAND3_ENVIRONMENT", environment)
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", pipeline)
    document = TestClient(app).get("/api/v1/openapi.json").json()
    paths = document["paths"]
    schemas = document["components"]["schemas"]
    resume_paths = ("/api/v1/scans/{scan_id}/resume", "/api/v1/scans/{scan_id}/resume-actions/{action_id}")
    resume_schemas = ("ResumeActionFailure", "ResumeActionLinks", "ResumeActionResult", "ScanResumeActionResponse")
    assert all((path in paths) is visible for path in resume_paths)
    assert all((schema in schemas) is visible for schema in resume_schemas)


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


def _resume_status(state: str) -> dict:
    if state == "completed":
        return {"state": "completed", "publication_action": "publish_current", "report_id": "vaultresume-report"}
    if state == "failed":
        return {"state": "failed", "reason_code": "vault_exact_resume_execution_failed", "retryable": True}
    if state == "interrupted":
        return {"state": "interrupted", "reason_code": "vault_exact_resume_interrupted", "retryable": True}
    return {"state": state, "phase": state}


def _reserve_resume_action(store, scan: str, key: str, fingerprint: str, request=None, status=None):
    return store.reserve_scanner_resume_action(
        scan_id=scan,
        request_payload={"operation": "exact_resume"} if request is None else request,
        status_payload=_resume_status("accepted") if status is None else status,
        client_id="scanner-api",
        idempotency_key_hash=key,
        request_fingerprint=fingerprint,
    )


def test_scanner_resume_action_lifecycle_replay_conflict_and_retry():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SQLiteStore(str(Path(tmpdir) / "scanner-resume.sqlite3"))
        try:
            created = _reserve_resume_action(store, "vaultresume1", "a" * 64, "b" * 64)
            replay = _reserve_resume_action(store, "vaultresume1", "a" * 64, "b" * 64)
            key_conflict = _reserve_resume_action(store, "vaultresume1", "a" * 64, "c" * 64)
            binding_conflict = _reserve_resume_action(store, "vaultresume8", "a" * 64, "b" * 64)
            active_conflict = _reserve_resume_action(store, "vaultresume1", "d" * 64, "e" * 64)
            started = store.start_scanner_resume_action(action_id=created["action_id"])
            start_stale = store.start_scanner_resume_action(action_id=created["action_id"])
            finalized = store.finalize_scanner_resume_action(
                action_id=created["action_id"], state="failed", status_payload=_resume_status("failed")
            )
            terminal_stale = store.finalize_scanner_resume_action(
                action_id=created["action_id"], state="completed", status_payload=_resume_status("completed")
            )
            retry = _reserve_resume_action(store, "vaultresume1", "f" * 64, "a" * 64)
            accepted_terminal = store.finalize_scanner_resume_action(
                action_id=retry["action_id"], state="completed", status_payload=_resume_status("completed")
            )
            retry_started = store.start_scanner_resume_action(action_id=retry["action_id"])
            retry_finalized = store.finalize_scanner_resume_action(
                action_id=retry["action_id"], state="completed", status_payload=_resume_status("completed")
            )
            completed_replay = _reserve_resume_action(store, "vaultresume1", "f" * 64, "a" * 64)
            readback = store.get_scanner_resume_action(action_id=created["action_id"])
        finally:
            store.close()

    assert created["outcome"] == "created" and replay == {**created, "outcome": "replay"}
    assert key_conflict == active_conflict == {"outcome": "conflict", "scan_id": "vaultresume1", "action_id": None, "state": None}
    assert binding_conflict["outcome"] == "conflict" and binding_conflict["action_id"] is None
    assert started["outcome"] == "started" and start_stale["outcome"] == "stale"
    assert finalized["outcome"] == "finalized" and terminal_stale["action"]["state"] == "failed"
    assert accepted_terminal["outcome"] == "stale"
    assert retry_started["outcome"] == "started" and retry_finalized["outcome"] == "finalized"
    assert retry["outcome"] == "created" and completed_replay == {**retry, "outcome": "replay", "state": "completed"}
    assert readback is not None and readback["state"] == "failed"


def test_scanner_resume_action_rejects_unbounded_envelopes_before_writes():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SQLiteStore(str(Path(tmpdir) / "scanner-resume-inputs.sqlite3"))
        try:
            invalid = [
                ({"operation": ("exact_resume",)}, None),
                ({"operation": "exact_resume", "control": {"Idempotency-Key": "redacted"}}, None),
                ({"operation": float("nan")}, None),
                (None, {"state": "accepted", "phase": "accepted", "provider": "x"}),
            ]
            for request, status in invalid:
                with pytest.raises(ValueError):
                    _reserve_resume_action(store, "vaultresume2", "1" * 64, "2" * 64, request, status)
            count = store.conn.execute("SELECT COUNT(*) FROM b3s_scanner_resume_actions").fetchone()[0]
        finally:
            store.close()

    assert count == 0


def test_scanner_resume_action_restart_recovery_and_cas_race():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "scanner-resume-recovery.sqlite3")
        writer = SQLiteStore(db_path)
        action = _reserve_resume_action(writer, "vaultresume3", "3" * 64, "4" * 64)
        writer.start_scanner_resume_action(action_id=action["action_id"])
        writer.close()
        SQLiteStore.reset_schema_init_metrics()
        restart = SQLiteStore(db_path)
        try:
            assert restart.interrupt_incomplete_scanner_resume_actions() == 1
            interrupted = restart.get_scanner_resume_action(action_id=action["action_id"])
            stale_terminal = restart.finalize_scanner_resume_action(action_id=action["action_id"], state="completed", status_payload=_resume_status("completed"))
            replay = _reserve_resume_action(restart, "vaultresume3", "3" * 64, "4" * 64)
            retry = _reserve_resume_action(restart, "vaultresume3", "5" * 64, "6" * 64)
            restart.start_scanner_resume_action(action_id=retry["action_id"])
            restart.finalize_scanner_resume_action(
                action_id=retry["action_id"], state="failed", status_payload=_resume_status("failed")
            )
            race = _reserve_resume_action(restart, "vaultresume4", "7" * 64, "8" * 64)
            terminal = SQLiteStore(db_path)

            class RaceBeforeRecoveryUpdate:
                def __init__(self, connection):
                    self.connection, self.raced = connection, False

                def execute(self, statement, parameters=()):
                    if not self.raced and "UPDATE b3s_scanner_resume_actions" in statement:
                        self.raced = True
                        terminal.start_scanner_resume_action(action_id=race["action_id"])
                        terminal.finalize_scanner_resume_action(
                            action_id=race["action_id"], state="completed", status_payload=_resume_status("completed")
                        )
                    return self.connection.execute(statement, parameters)

                def __getattr__(self, name):
                    return getattr(self.connection, name)

            raced = RaceBeforeRecoveryUpdate(restart.conn)
            restart.conn = raced
            assert restart.interrupt_incomplete_scanner_resume_actions() == 0 and raced.raced
            persisted = terminal.get_scanner_resume_action(action_id=race["action_id"])
        finally:
            terminal.close()
            restart.close()

    assert interrupted is not None and interrupted["state"] == "interrupted"
    assert stale_terminal["outcome"] == "stale" and stale_terminal["action"]["state"] == "interrupted"
    assert replay == {**action, "outcome": "replay", "state": "interrupted"}
    assert retry["outcome"] == "created"
    assert persisted is not None and persisted["state"] == "completed"


def test_scanner_resume_action_corruption_fails_closed_before_finalization():
    from src.storage.scanner_api_jobs import ScannerResumeActionCorruptionError

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SQLiteStore(str(Path(tmpdir) / "scanner-resume-corrupt.sqlite3"))
        try:
            active = _reserve_resume_action(store, "vaultresume5", "9" * 64, "a" * 64)
            store.conn.execute("UPDATE b3s_scanner_resume_actions SET status_json = '{' WHERE action_id = ?", (active["action_id"],))
            store.conn.commit()
            with pytest.raises(ScannerResumeActionCorruptionError):
                store.finalize_scanner_resume_action(action_id=active["action_id"], state="failed", status_payload=_resume_status("failed"))
            active_state = store.conn.execute("SELECT state FROM b3s_scanner_resume_actions WHERE action_id = ?", (active["action_id"],)).fetchone()[0]
            terminal = _reserve_resume_action(store, "vaultresume6", "b" * 64, "c" * 64)
            store.start_scanner_resume_action(action_id=terminal["action_id"])
            store.finalize_scanner_resume_action(action_id=terminal["action_id"], state="completed", status_payload=_resume_status("completed"))
            store.conn.execute("UPDATE b3s_scanner_resume_actions SET request_json = '[' WHERE action_id = ?", (terminal["action_id"],))
            store.conn.commit()
            with pytest.raises(ScannerResumeActionCorruptionError):
                store.get_scanner_resume_action(action_id=terminal["action_id"])
            with pytest.raises(ScannerResumeActionCorruptionError):
                store.finalize_scanner_resume_action(action_id=terminal["action_id"], state="failed", status_payload=_resume_status("failed"))
            terminal_state = store.conn.execute("SELECT state FROM b3s_scanner_resume_actions WHERE action_id = ?", (terminal["action_id"],)).fetchone()[0]
        finally:
            store.close()

    assert active_state == "accepted" and terminal_state == "completed"


def test_scanner_resume_action_concurrent_reservation_has_one_active_winner():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, gate = str(Path(tmpdir) / "scanner-resume-race.sqlite3"), Barrier(2)
        SQLiteStore(db_path).close()

        def reserve(key, fingerprint):
            store = SQLiteStore(db_path)
            try:
                gate.wait()
                return _reserve_resume_action(store, "vaultresume7", key, fingerprint)
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, ["d" * 64, "e" * 64], ["f" * 64, "1" * 64]))

    assert sorted(result["outcome"] for result in results) == ["conflict", "created"]


class _InlineThread:
    def __init__(self, *, target, daemon):
        self.target = target

    def start(self):
        self.target()


class _DeferredThread:
    targets = []

    def __init__(self, *, target, daemon):
        self.target = target

    def start(self):
        self.targets.append(self.target)


class _FailingThread:
    def __init__(self, *, target, daemon):
        self.target = target

    def start(self):
        raise RuntimeError("thread construction detail")


def test_vault_exact_resume_controller_has_one_two_connection_start_winner_and_ignores_replay(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from web import exact_resume_controller as controller

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, gate = str(Path(tmpdir) / "resume-controller-race.sqlite3"), Barrier(2)
        store = SQLiteStore(db_path)
        try:
            created = _reserve_resume_action(store, "vaultcontroller1", "a" * 64, "b" * 64)
            replay = _reserve_resume_action(store, "vaultcontroller1", "a" * 64, "b" * 64)
        finally:
            store.close()
        calls = []

        def run_exact_resume(*, action, **kwargs):
            calls.append((action["action_id"], set(kwargs)))
            return SimpleNamespace(action="publish_current", report_id="vaultcontroller1")

        monkeypatch.setattr(controller, "_run_vault_exact_resume", run_exact_resume)
        ignored = controller.launch_vault_exact_resume_action(reservation=replay, repository=object(), database_path=db_path, thread_factory=_InlineThread)

        def launch(_index):
            gate.wait()
            return controller.launch_vault_exact_resume_action(reservation=created, repository=object(), database_path=db_path, thread_factory=_InlineThread)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(launch, range(2)))
        store = SQLiteStore(db_path)
        try:
            persisted = store.get_scanner_resume_action(action_id=created["action_id"])
        finally:
            store.close()

    assert ignored.outcome == "ignored"
    assert sorted(outcome.outcome for outcome in outcomes) == ["stale", "started"]
    assert calls == [(created["action_id"], {"scan_id", "repository"})]
    assert persisted is not None and persisted["state"] == "completed"


def test_vault_exact_resume_controller_persists_publications_and_safe_failures(monkeypatch):
    from src.services.evidence_vault_scan_orchestration import VaultExactResumeError
    from web import exact_resume_controller as controller

    publications = ["publish_current", "record_no_score", "retain_source"]
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "resume-controller-projections.sqlite3")
        store = SQLiteStore(db_path)
        try:
            reservations = [_reserve_resume_action(store, f"vaultcontroller{index}", str(index) * 64, "f" * 64) for index in range(1, 4)]
        finally:
            store.close()
        for reservation, publication in zip(reservations, publications, strict=True):
            report_id = f"report-{publication}"
            monkeypatch.setattr(controller, "_run_vault_exact_resume", lambda **_kwargs: SimpleNamespace(action=publication, report_id=report_id))
            controller.launch_vault_exact_resume_action(reservation=reservation, repository=object(), database_path=db_path, thread_factory=_InlineThread)
        store = SQLiteStore(db_path)
        try:
            typed = _reserve_resume_action(store, "vaultcontrollertyped", "4" * 64, "e" * 64)
            arbitrary = _reserve_resume_action(store, "vaultcontrollerarbitrary", "5" * 64, "d" * 64)
        finally:
            store.close()

        def typed_error(**_kwargs):
            raise VaultExactResumeError("vault_exact_resume_superseded", retryable=False)

        def arbitrary_error(**_kwargs):
            raise RuntimeError("private execution detail")

        monkeypatch.setattr(controller, "_run_vault_exact_resume", typed_error)
        controller.launch_vault_exact_resume_action(reservation=typed, repository=object(), database_path=db_path, thread_factory=_InlineThread)
        monkeypatch.setattr(controller, "_run_vault_exact_resume", arbitrary_error)
        controller.launch_vault_exact_resume_action(reservation=arbitrary, repository=object(), database_path=db_path, thread_factory=_InlineThread)
        store = SQLiteStore(db_path)
        try:
            actions = [store.get_scanner_resume_action(action_id=reservation["action_id"]) for reservation in [*reservations, typed, arbitrary]]
        finally:
            store.close()

    assert [action["status_payload"] for action in actions[:3]] == [
        {"state": "completed", "publication_action": publication, "report_id": f"report-{publication}"}
        for publication in publications
    ]
    assert actions[3]["status_payload"] == {
        "state": "failed", "reason_code": "vault_exact_resume_superseded", "retryable": False
    }
    assert actions[4]["status_payload"] == {
        "state": "failed", "reason_code": "vault_exact_resume_execution_failed", "retryable": True
    }
    assert "private execution detail" not in str(actions[4])


def test_vault_exact_resume_controller_fences_stale_corrupt_non_running_and_failed_threads(monkeypatch):
    from web import exact_resume_controller as controller

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "resume-controller-fencing.sqlite3")
        store = SQLiteStore(db_path)
        try:
            stale = _reserve_resume_action(store, "vaultcontrollerstale", "6" * 64, "c" * 64)
        finally:
            store.close()
        calls, _DeferredThread.targets = [], []
        monkeypatch.setattr(controller, "_run_vault_exact_resume", lambda **kwargs: calls.append(kwargs))
        started = controller.launch_vault_exact_resume_action(reservation=stale, repository=object(), database_path=db_path, thread_factory=_DeferredThread)
        assert controller.recover_interrupted_vault_exact_resume_actions(database_path=db_path) == 1
        _DeferredThread.targets.pop()()
        store = SQLiteStore(db_path)
        try:
            failed = _reserve_resume_action(store, "vaultcontrollerstart", "7" * 64, "b" * 64)
            corrupt = _reserve_resume_action(store, "vaultcontrollercorrupt", "8" * 64, "a" * 64)
            running = _reserve_resume_action(store, "vaultcontrollerrunning", "9" * 64, "9" * 64)
            store.conn.execute("UPDATE b3s_scanner_resume_actions SET status_json='{' WHERE action_id=?", (corrupt["action_id"],))
            store.conn.commit()
            store.start_scanner_resume_action(action_id=running["action_id"])
        finally:
            store.close()
        start_failed = controller.launch_vault_exact_resume_action(reservation=failed, repository=object(), database_path=db_path, thread_factory=_FailingThread)
        corrupt_result = controller.launch_vault_exact_resume_action(reservation=corrupt, repository=object(), database_path=db_path, thread_factory=_InlineThread)
        running_result = controller.launch_vault_exact_resume_action(reservation=running, repository=object(), database_path=db_path, thread_factory=_InlineThread)
        store = SQLiteStore(db_path)
        try:
            stale_action = store.get_scanner_resume_action(action_id=stale["action_id"])
            failed_action = store.get_scanner_resume_action(action_id=failed["action_id"])
            states = [
                store.conn.execute("SELECT state FROM b3s_scanner_resume_actions WHERE action_id=?", (reservation["action_id"],)).fetchone()[0]
                for reservation in [corrupt, running]
            ]
        finally:
            store.close()

    assert [started.outcome, start_failed.outcome, corrupt_result.outcome, running_result.outcome] == ["started", "thread_start_failed", "invalid", "stale"]
    assert calls == [] and states == ["accepted", "running"]
    assert stale_action["status_payload"] == {"state": "interrupted", "reason_code": "vault_exact_resume_interrupted", "retryable": True}
    assert failed_action["status_payload"] == {"state": "failed", "reason_code": "vault_exact_resume_execution_failed", "retryable": True}


def test_app_runtime_recovers_orphaned_vault_resume_actions(monkeypatch):
    from web import app as web_app

    class MissingEnv:
        def is_file(self):
            return False

    calls = []
    monkeypatch.setattr(web_app, "Path", lambda _path: MissingEnv())
    monkeypatch.setattr(web_app, "google_oidc_enabled", lambda _env: False)
    monkeypatch.setattr(web_app, "verify_postgres_runtime_ready", lambda: calls.append("postgres"))
    monkeypatch.setattr(web_app, "recover_interrupted_scans", lambda: calls.append("scan"))
    monkeypatch.setattr(web_app, "recover_interrupted_vault_exact_resume_actions", lambda: calls.append("resume"))
    web_app._initialize_runtime()

    assert calls == ["postgres", "scan", "resume"]


def _enable_resume_api(monkeypatch, database_path, repository=None):
    for name, value in (("B3S_SCANNER_API_TOKEN", TOKEN), ("B3S_VAULT_EXACT_RESUME_API_ENABLED", "true"),
                        ("BRAND3_ENVIRONMENT", "vault"), ("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("web.api_v1.service.BRAND3_DB_PATH", str(database_path))
    monkeypatch.setattr("web.api_v1.service._postgres_repository", lambda: repository or object())


@pytest.mark.parametrize("flag,environment,pipeline", [("false", "vault", "true"), ("true", "production", "true"), ("true", "vault", "false")])
def test_vault_resume_api_gate_precedes_auth_and_side_effects(monkeypatch, flag, environment, pipeline):
    monkeypatch.setenv("B3S_VAULT_EXACT_RESUME_API_ENABLED", flag)
    monkeypatch.setenv("BRAND3_ENVIRONMENT", environment)
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", pipeline)
    calls = []
    monkeypatch.setattr("web.api_v1.service.SQLiteStore", lambda *_: calls.append("store"))
    monkeypatch.setattr("web.api_v1.service.launch_vault_exact_resume_action", lambda **_: calls.append("launch"))
    client = TestClient(app)
    post = client.post("/api/v1/scans/hidden-scan/resume")
    get = client.get("/api/v1/scans/hidden-scan/resume-actions/hidden-action")
    assert post.status_code == get.status_code == 404 and calls == [] and post.headers["cache-control"] == get.headers["cache-control"] == "no-store"


def test_vault_resume_api_contract_guards_and_replay(monkeypatch, tmp_path):
    database_path, repository = tmp_path / "resume-idempotency.sqlite3", object()
    _enable_resume_api(monkeypatch, database_path, repository)
    _configure_evidence_reviewer(monkeypatch)
    calls = []
    monkeypatch.setattr("web.api_v1.service.launch_vault_exact_resume_action", lambda **kwargs: calls.append(kwargs))
    client = TestClient(app)
    denied = client.post("/api/v1/scans/auth-scan/resume", headers={**REVIEW_AUTH, "Idempotency-Key": "reviewer-read-only"})
    assert denied.status_code == 403 and denied.json()["error"]["details"]["required_scope"] == "scans:write"
    monkeypatch.setattr("web.api_v1.service._postgres_repository", lambda: None)
    failed = client.post("/api/v1/scans/guard-scan/resume", headers={**AUTH, "Idempotency-Key": "repo-failure"})
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "vault_resume_repository_unavailable" and not database_path.exists()
    monkeypatch.setattr("web.api_v1.service._postgres_repository", lambda: repository)
    touches = []
    monkeypatch.setattr("web.api_v1.service.SQLiteStore", lambda *_: touches.append("store"))
    for body in (b"{}", b"null"):
        response = client.post("/api/v1/scans/body-scan/resume", content=body, headers={**AUTH, "Content-Type": "application/json", "Idempotency-Key": body.decode()})
        assert response.status_code == 422 and response.json()["error"]["code"] == "request_validation_failed"
    monkeypatch.setattr("web.api_v1.service.SQLiteStore", SQLiteStore)
    assert touches == [] and calls == []
    headers = {**AUTH, "Idempotency-Key": "resume-once"}
    first = client.post("/api/v1/scans/exact-scan/resume", headers=headers)
    replay = client.post("/api/v1/scans/exact-scan/resume", headers=headers)
    assert first.status_code == replay.status_code == 202
    assert first.json()["action_id"] == replay.json()["action_id"] and first.headers["location"] == first.json()["links"]["self"]
    assert first.headers["retry-after"] == "5" and "idempotent-replayed" not in first.headers
    assert replay.headers["idempotent-replayed"] == "true" and len(calls) == 1
    assert calls[0]["repository"] is repository and calls[0]["reservation"]["outcome"] == "created"
    conflict = client.post("/api/v1/scans/exact-scan/resume", headers={**AUTH, "Idempotency-Key": "resume-conflict"})
    assert conflict.status_code == 409 and "request_payload" not in conflict.text
    action_id = first.json()["action_id"]
    assert client.get(f"/api/v1/scans/exact-scan/resume-actions/{action_id}", headers=REVIEW_AUTH).status_code == 200


@pytest.mark.parametrize("mutation,injected", [({"status_payload": {"publication_action": "retain_source", "report_id": "../secret"}}, "../secret"), ({"created_at": "timestamp-secret"}, "timestamp"), ({"state": "failed", "status_payload": {"reason_code": "vault_exact_resume_superseded", "retryable": "retry-secret"}}, "retryable")])
def test_vault_resume_api_binds_action_to_scan_and_projects_all_states(monkeypatch, tmp_path, mutation, injected):
    database_path = tmp_path / "resume-projection.sqlite3"
    _enable_resume_api(monkeypatch, database_path)
    states, terminal_status = {}, {"completed": {"state": "completed", "publication_action": "retain_source", "report_id": "safe-report"}, "failed": {"state": "failed", "reason_code": "vault_exact_resume_superseded", "retryable": False}, "interrupted": {"state": "interrupted", "reason_code": "vault_exact_resume_interrupted", "retryable": True}}
    store = SQLiteStore(str(database_path))
    try:
        for index, state in enumerate(("accepted", "running", "completed", "failed", "interrupted")):
            scan_id = f"resume-scan-{index}"
            reservation = _reserve_resume_action(store, scan_id, f"{index + 1}" * 64, f"{index + 2}" * 64)
            action_id = reservation["action_id"]
            if state != "accepted":
                store.start_scanner_resume_action(action_id=action_id)
            if state in terminal_status:
                store.finalize_scanner_resume_action(action_id=action_id, state=state, status_payload=terminal_status[state])
            states[state] = (scan_id, action_id)
    finally:
        store.close()
    client = TestClient(app)
    for state, (scan_id, action_id) in states.items():
        response = client.get(f"/api/v1/scans/{scan_id}/resume-actions/{action_id}", headers=AUTH)
        payload = response.json()
        assert response.status_code == 200 and payload["state"] == state and all(isinstance(payload[field], str) for field in ("created_at", "updated_at"))
        assert set(payload) == {
            "object", "api_version", "action_id", "scan_id", "state",
            "created_at", "updated_at", "completed_at", "result", "failure", "links",
        }
        assert all(field not in response.text for field in ("request_payload", "request_fingerprint"))
        if state == "completed":
            assert payload["result"] == {"publication_action": "retain_source", "report_id": "safe-report"} and payload["failure"] is None
        elif state in {"failed", "interrupted"}:
            assert payload["result"] is None and payload["failure"]["reason_code"].startswith("vault_exact_resume_") and isinstance(payload["failure"]["retryable"], bool)
        else:
            assert payload["result"] is None and payload["failure"] is None
    mismatch = client.get(f"/api/v1/scans/wrong-scan/resume-actions/{states['completed'][1]}", headers=AUTH)
    missing = client.get("/api/v1/scans/missing-scan/resume-actions/missing-action", headers=AUTH)
    assert mismatch.status_code == missing.status_code == 404 and mismatch.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found" and mismatch.headers["cache-control"] == missing.headers["cache-control"] == "no-store"
    monkeypatch.setattr("web.api_v1.service._read_resume_action", lambda _: {"action_id": "bad-action", "scan_id": "bad-scan", "state": "completed", "created_at": "2026-07-20T10:00:00+00:00", "updated_at": "2026-07-20T10:00:00+00:00", "completed_at": None, "status_payload": {"publication_action": "retain_source", "report_id": "safe-report"}, **mutation})
    malformed = client.get("/api/v1/scans/bad-scan/resume-actions/bad-action", headers=AUTH)
    assert malformed.status_code == 503 and malformed.json()["error"]["code"] == "scanner_resume_action_store_unavailable" and injected not in malformed.text


def test_diagnostic_detail_requires_existing_read_scope_and_keeps_ledger_private(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)
    status = _running_scan()
    status["diagnostic_operation_ledger"] = {
        "version": "scan-diagnostic-ledger-v1",
        "events": [{"operation": "scan_execution", "stage": "capture", "outcome": "failed", "observed_at": "2026-07-20T10:00:01+00:00", "durability": "observed_best_effort"}],
        "dropped_event_count": 1,
        "truncated_event_count": 0,
        "additional_status_write_count": 1,
        "additional_status_bytes": 123,
    }
    monkeypatch.setattr("web.api_v1.router.get_scan_diagnostic_status", lambda _scan_id: status)
    client = TestClient(app)

    assert client.get("/api/v1/scans/scan123/diagnostic-detail").status_code == 401
    response = client.get("/api/v1/scans/scan123/diagnostic-detail", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["events"][0]["operation"] == "scan_execution"
    assert response.json()["exact_resume"] == {"supported": False, "reason": "exact_resume_action_trace_unsupported"}


def test_diagnostic_detail_returns_not_found_for_unknown_scan(monkeypatch):
    _configure_evidence_reviewer(monkeypatch)
    monkeypatch.setattr("web.api_v1.router.get_scan_diagnostic_status", lambda _scan_id: None)
    response = TestClient(app).get("/api/v1/scans/missing/diagnostic-detail", headers=AUTH)
    assert response.status_code == 404


def test_diagnostic_operation_ledger_bounds_and_redacts_exception_text():
    status = {"id": "scan123", "state": "running"}
    for _ in range(26):
        scan_runner._append_diagnostic_operation_locked(
            status,
            scan_runner._diagnostic_operation(
                operation="scan_execution",
                stage="capture",
                outcome="failed",
                exc=RuntimeError("secret-token-must-not-appear"),
                coverage={"payload": "x" * 10000},
            ),
        )
    dossier = scan_runner.scan_diagnostic_dossier_from_status(status)
    assert len(dossier["events"]) == 24 and dossier["dropped_event_count"] == 2
    assert "secret-token-must-not-appear" not in str(dossier)
    assert all("payload" not in str(event.get("coverage") or {}) for event in dossier["events"])


def test_diagnostic_dossier_treats_persisted_events_as_hostile_and_stays_bounded():
    status = {
        "id": "scan123",
        "state": "error",
        "scan_build_sha": "a" * 40,
        "diagnostic_operation_ledger": {
            "events": [
                {
                    "operation": "vault_authority_application",
                    "stage": "vault_authority",
                    "outcome": "failed",
                    "observed_at": "2026-07-20T10:00:01+00:00",
                    "durability": "persisted-definitely-not",
                    "trace": [{"path": "/private/secret.py", "line": 1, "function": "leak"}],
                    "coverage": {"payload": "secret-token-must-not-appear"},
                }
                for _ in range(200)
            ],
            "dropped_event_count": 0,
            "truncated_event_count": 0,
        },
    }
    dossier = scan_runner.scan_diagnostic_dossier_from_status(status)
    encoded = __import__("json").dumps(dossier, sort_keys=True).encode("utf-8")
    assert len(encoded) <= 32 * 1024
    assert len(dossier["events"]) == 24
    assert "secret-token-must-not-appear" not in str(dossier)
    assert all(event["durability"] == "observed_best_effort" for event in dossier["events"])
    assert all("trace" not in event for event in dossier["events"])


def test_diagnostic_dossier_keeps_only_closed_authority_identifiers():
    relation_id = "a" * 64
    fingerprint = "b" * 64
    status = {"id": "scan123", "state": "running"}
    event = scan_runner._diagnostic_operation(
        operation="vault_authority_evaluation",
        stage="vault_authority",
        outcome="failed",
        coverage={
            "plan": {"canonical_plan_fingerprint": fingerprint, "private": "no"},
            "review_partition": {"tile_ids": ["M1", "not-a-real-tile"]},
            "operational_authority_coverage_loss": [
                {
                    "tile_id": "M1",
                    "component_key": "mission",
                    "reason": "historical_basis_missing",
                    "basis_facts": [{
                        "relation_id": relation_id,
                        "continuity_state": "missing",
                        "provider_text": "do-not-copy",
                    }],
                }
            ],
        },
    )
    scan_runner._append_diagnostic_operation_locked(status, event)
    coverage = scan_runner.scan_diagnostic_dossier_from_status(status)["events"][0]["coverage"]
    assert coverage["plan"] == {"canonical_plan_fingerprint": fingerprint}
    assert coverage["review_partition"]["tile_ids"] == ["M1"]
    loss = coverage["operational_authority_coverage_loss"][0]
    assert loss["reason"] == "historical_basis_missing"
    assert loss["basis_facts"] == [{"relation_id": relation_id, "continuity_state": "missing"}]
    assert "provider_text" not in str(coverage)


def test_diagnostic_detail_context_is_closed_for_malformed_and_divergent_readback():
    assert scan_runner._safe_detail_context({"status_readback": [], "operation_lookup": []}) == {}
    dossier = scan_runner.scan_diagnostic_dossier_from_status({
        "id": "scan123",
        "state": "running",
        "diagnostic_operation_ledger": {"events": []},
        "_diagnostic_detail_context": {"status_readback": "divergent"},
    })
    assert dossier["persistence"]["readback"] == "divergent"


def test_diagnostic_detail_keeps_safe_db_identity_not_exception_text():
    class Diagnostic:
        schema_name = "public"
        table_name = "capture_operations"
        constraint_name = "capture_operations_source_scan_id_key"
        column_name = "source_scan_id"

    class DatabaseFailure(RuntimeError):
        sqlstate = "23505"
        diag = Diagnostic()

    status = {"id": "scan123", "state": "error"}
    event = scan_runner._diagnostic_operation(
        operation="vault_capture_preparation",
        stage="vault_preparation",
        outcome="failed",
        exc=DatabaseFailure("postgres://secret message must never escape"),
    )
    scan_runner._append_diagnostic_operation_locked(status, event)
    detail = scan_runner.scan_diagnostic_dossier_from_status(status)["events"][0]
    assert detail["origin"] == {"exception_type": "DatabaseFailure", "sqlstate": "23505"}
    assert detail["resource"] == {
        "availability": "observed",
        "schema": "public",
        "table": "capture_operations",
        "constraint": "capture_operations_source_scan_id_key",
        "column": "source_scan_id",
    }
    assert "secret" not in str(detail)


def test_diagnostic_coverage_preserves_upstream_and_local_truncation_counts():
    fingerprint = "c" * 64
    relation_ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(30)]
    projection = scan_runner._safe_operation_coverage(
        {
            "review_partition": {
                "tile_ids": ["M1"] * 30,
                "tile_ids_truncated_count": 5,
            },
            "operational_authority_coverage_loss": [
                {
                    "tile_id": "M1",
                    "component_key": "mission",
                    "basis_facts": (
                        [{"relation_id": value} for value in relation_ids]
                        if index == 0
                        else []
                    ),
                    "basis_facts_truncated_count": 7 if index == 0 else 0,
                }
                for index in range(30)
            ],
            "operational_authority_coverage_loss_truncated_count": 11,
            "judgment_delta_coverage_loss": [
                {
                    "tile_id": "M1",
                    "component_key": "mission",
                    "evidence_fingerprints": [fingerprint] * 30,
                    "evidence_fingerprints_truncated_count": 3,
                }
            ],
        }
    )
    assert projection["review_partition"]["tile_ids_truncated_count"] == 34
    assert projection["operational_authority_coverage_loss_truncated_count"] == 17
    assert projection["operational_authority_coverage_loss"][0]["basis_facts_truncated_count"] == 13
    assert projection["judgment_delta_coverage_loss"][0]["evidence_fingerprints_truncated_count"] == 9


def test_diagnostic_detail_rejects_malformed_resource_and_trace_traversal():
    detail = scan_runner.scan_diagnostic_dossier_from_status(
        {
            "id": "scan123",
            "state": "done",
            "diagnostic_operation_ledger": {
                "events": [
                    {
                        "operation": "vault_authority_evaluation",
                        "outcome": "failed",
                        "resource": {"availability": []},
                        "trace": [
                            {
                                "path": "src/../../secret.py",
                                "line": 1,
                                "function": "leak",
                            }
                        ],
                    }
                ]
            },
        }
    )
    event = detail["events"][0]
    assert event["resource"] == {"availability": "unknown"}
    assert "trace" not in event


def test_diagnostic_detail_actual_response_model_stays_under_wire_cap():
    from web.api_v1.models import ScanDiagnosticDetailResponse
    from web.api_v1.presenters import diagnostic_detail_payload

    status = {
        "id": "scan123",
        "state": "done",
        "diagnostic_operation_ledger": {
            "events": [
                {
                    "operation": "x",
                    "outcome": "failed",
                    "trace": [{"path": "src/a.py", "line": 1, "function": "f"}],
                }
            ]
        },
    }
    base = diagnostic_detail_payload(status)
    size = len(__import__("json").dumps(base, separators=(",", ":")).encode())
    status["diagnostic_operation_ledger"]["events"][0]["trace"][0]["path"] = "src/" + "a" * (32760 - size + 1) + ".py"
    payload = diagnostic_detail_payload(status)
    app = __import__("fastapi").FastAPI()

    @app.get("/", response_model=ScanDiagnosticDetailResponse)
    def get_detail():
        return payload

    assert len(TestClient(app).get("/").content) <= 32 * 1024


def test_report_save_event_is_in_existing_final_status_snapshot(monkeypatch):
    scan_id = "diagnostic-report-save"
    status = _running_scan(scan_id)
    status["state"] = "running"
    persisted = []
    with scan_runner._LOCK:
        scan_runner._SCANS[scan_id] = status
        scan_runner._SCAN_EVENTS[scan_id] = __import__("threading").Event()
    monkeypatch.setattr(scan_runner, "save_report", lambda _report: None)
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda snapshot: persisted.append(snapshot))
    try:
        assert scan_runner._publish_completed_report(scan_id, {"id": scan_id}) is True
    finally:
        with scan_runner._LOCK:
            scan_runner._SCANS.pop(scan_id, None)
            scan_runner._SCAN_EVENTS.pop(scan_id, None)
    assert persisted
    events = persisted[0]["diagnostic_operation_ledger"]["events"]
    assert [event["outcome"] for event in events if event["operation"] == "report_save"] == ["started", "completed"]


def test_status_diagnostic_is_closed_and_preserves_scan_time_context():
    status = _running_scan()
    status.update(
        state="error",
        execution_stage="vault_preparation",
        scan_build_sha="b" * 40,
        phases=[{"key": "capture", "label": "Capture", "state": "done"}],
        diagnostic={
            "kind": "execution_failed",
            "stage": "vault_preparation",
            "capture_state": "completed",
            "reason_codes": ["vault_authority_capture_persist_failed", "postgres://secret"],
            "summary": "private exception text",
            "build_sha": "b" * 40,
            "origin": {"exception_type": "RuntimeError", "sqlstate": "23505", "message": "secret"},
        },
    )

    public = status_payload(status)

    assert public["diagnostic"] == {
        "kind": "execution_failed",
        "stage": "vault_preparation",
        "capture_state": "completed",
        "reason_codes": ["vault_authority_capture_persist_failed"],
        "summary": "The scan stopped before completion.",
        "build_sha": "b" * 40,
        "origin": {"exception_type": "RuntimeError", "sqlstate": "23505"},
    }
    assert "secret" not in str(public)


def test_restarted_legacy_status_derives_safe_diagnostic_without_error_text():
    status = _running_scan()
    status.update(
        state="error",
        phase="error",
        error_code="process_restarted",
        error="worker restart at /private/token",
    )

    public = status_payload(status)

    assert public["diagnostic"] == {
        "kind": "execution_failed",
        "stage": "unknown",
        "capture_state": "not_completed",
        "reason_codes": ["process_restarted"],
        "summary": "The scan stopped before completion.",
        "build_sha": "unknown",
        "unknowns": ["legacy_status_without_diagnostic"],
    }
    assert "private" not in str(public)


def test_completed_unavailable_result_has_no_score_and_safe_diagnostic():
    report = _assessment_report("no-score", unavailable=True)
    original_score = report["score"]

    public = result_payload(report)

    assert public["score"]["value"] is None
    assert report["score"] == original_score
    assert public["diagnostic"] == {
        "kind": "assessment_unavailable",
        "stage": "vault_authority",
        "capture_state": "unknown",
        "reason_codes": ["component_not_scored:vision:not_evaluated"],
        "summary": "The scan completed, but no authoritative assessment is available.",
        "build_sha": "unknown",
    }


def test_assessment_diagnostic_keeps_only_closed_kernel_reason_codes():
    diagnostic = scan_runner.scan_diagnostic_from_report(
        {"pipeline_commit_sha": "d" * 40, "raw": {"source_capture": {}}},
        assessment={
            "availability": "unavailable",
            "assessment": {
                "reason_codes": [
                    "components_not_mapping",
                    "component_not_scored:vision:not_evaluated",
                    "component_not_scored:vision:private:payload",
                    "component_not_scored:private_component:not_evaluated",
                ]
            },
        },
    )

    assert diagnostic == {
        "kind": "assessment_unavailable",
        "stage": "vault_authority",
        "capture_state": "completed",
        "reason_codes": [
            "components_not_mapping",
            "component_not_scored:vision:not_evaluated",
        ],
        "summary": "The scan completed, but no authoritative assessment is available.",
        "build_sha": "d" * 40,
    }


def test_malformed_persisted_diagnostic_never_breaks_status_projection():
    status = _running_scan()
    status.update(
        state="error",
        error_code="scan_execution_failed",
        diagnostic={
            "kind": [],
            "capture_state": {},
            "unknowns": [{}, {}, {}, {}, {}],
        },
    )

    public = status_payload(status)

    assert public["diagnostic"] == {
        "kind": "execution_failed",
        "stage": "unknown",
        "capture_state": "not_completed",
        "reason_codes": ["scan_execution_failed"],
        "summary": "The scan stopped before completion.",
        "build_sha": "unknown",
        "unknowns": ["legacy_status_without_diagnostic"],
    }

    status.update(
        state="done",
        diagnostic={
            "kind": "assessment_unavailable",
            "stage": "vault_authority",
            "capture_state": {},
            "reason_codes": ["components_not_mapping"],
            "build_sha": "a" * 40,
            "unknowns": 5,
        },
    )
    public = status_payload(status)

    assert public["diagnostic"] == {
        "kind": "assessment_unavailable",
        "stage": "vault_authority",
        "capture_state": "unknown",
        "reason_codes": ["components_not_mapping"],
        "summary": "The scan completed, but no authoritative assessment is available.",
        "build_sha": "a" * 40,
    }


def test_primary_restart_outweighs_stale_secondary_diagnostic():
    status = _running_scan()
    status.update(
        state="error",
        error_code="process_restarted",
        execution_stage="capture",
        diagnostic={
            "kind": "assessment_unavailable",
            "stage": "vault_authority",
            "capture_state": "completed",
            "reason_codes": ["components_not_mapping"],
            "build_sha": "d" * 40,
        },
        vault={"state": "failed", "error": "private"},
    )

    public = status_payload(status)

    assert public["diagnostic"] == {
        "kind": "execution_failed",
        "stage": "unknown",
        "capture_state": "not_completed",
        "reason_codes": ["process_restarted"],
        "summary": "The scan stopped before completion.",
        "build_sha": "unknown",
        "unknowns": ["legacy_status_without_diagnostic"],
    }
