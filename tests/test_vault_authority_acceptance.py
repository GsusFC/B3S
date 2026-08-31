"""VA5 integration acceptance for the Vault SV9 authority publication."""

from __future__ import annotations

from copy import deepcopy
import re

from fastapi.testclient import TestClient

from src.services.evidence_vault_sv9_authority_report import (
    project_vault_authority_publication,
)
from tests.test_evidence_vault_sv9_authority_report import _adopt
from web import scan_runner
from web.api_v1 import service as api_service
from web.app import app


API_TOKEN = "va5-authority-acceptance-token"
AUTHORIZATION = {"Authorization": f"Bearer {API_TOKEN}"}
SCAN_ID = "scan"


def _authority_report() -> tuple[dict, dict]:
    """Build a report from the production authority application and projection."""

    _repository, application_result = _adopt()
    publication = project_vault_authority_publication(application_result, SCAN_ID)
    assert publication["action"] == "publish_current"
    payload = publication["scanner_payload"]
    report = scan_runner._compose_report(
        SCAN_ID,
        "https://example.test",
        "Example",
        payload,
    )
    return publication, report


def _api_tile_state(result: dict) -> dict[tuple[str, str], str]:
    return {
        (component["key"], tile["id"]): tile["estado"]
        for component in result["components"]
        for tile in component["tiles"]
    }


def _projected_tile_state(publication: dict) -> dict[tuple[str, str], str]:
    assessment = publication["scanner_payload"]["sv9"]["assessment"]
    return {
        (tile["component_key"], tile["tile_id"]): tile["assessment_state"]
        for tile in assessment["tiles"]
    }


def _running_scan(scan_id: str) -> dict:
    return {
        "id": scan_id,
        "url": "https://example.test",
        "brand_name": "Example",
        "state": "running",
        "phase": "capture",
        "phases": [
            {"key": key, "state": "pending"}
            for key in ("capture", "interpret", "score", "report")
        ],
        "acquisition": [],
        "acquisition_gate": {"state": "pending"},
        "error": None,
        "error_code": None,
        "started_at": "2026-08-31T00:00:00+00:00",
        "completed_at": None,
    }


def test_authority_projection_survives_scanner_api_and_html_routes(monkeypatch) -> None:
    publication, report = _authority_report()
    projected_assessment = publication["scanner_payload"]["sv9"]["assessment"]
    projected_identity = publication["scanner_payload"]["sv9"]["assessment_fingerprint"]
    projected_score_identity = publication["scanner_payload"]["sv9"]["score_fingerprint"]
    projected_score = publication["scanner_payload"]["sv9"]["brand3_score"]

    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", API_TOKEN)
    load_report = lambda scan_id: deepcopy(report) if scan_id == SCAN_ID else None
    monkeypatch.setattr(api_service, "load_report", load_report)
    monkeypatch.setattr("web.app.load_report", load_report)

    captured_view_models: list[dict] = []
    real_view_model_builder = __import__(
        "web.app", fromlist=["build_report_view_model"]
    ).build_report_view_model

    def capture_view_model(value: dict) -> dict:
        view_model = real_view_model_builder(value)
        captured_view_models.append(view_model)
        return view_model

    monkeypatch.setattr("web.app.build_report_view_model", capture_view_model)
    client = TestClient(app)

    api_response = client.get(
        f"/api/v1/scans/{SCAN_ID}/result",
        headers=AUTHORIZATION,
    )
    html_response = client.get(f"/report/{SCAN_ID}")

    assert api_response.status_code == 200
    assert html_response.status_code == 200
    api_result = api_response.json()
    assert api_result["id"] == SCAN_ID
    assert api_result["metadata"]["assessment_availability"] == "available"
    assert api_result["metadata"]["assessment_fingerprint"] == projected_identity
    assert api_result["metadata"]["score_fingerprint"] == projected_score_identity
    assert api_result["score"]["value"] == projected_score
    assert _api_tile_state(api_result) == _projected_tile_state(publication)
    assert len(_api_tile_state(api_result)) == projected_assessment["tile_count"] == 80

    assert captured_view_models
    view_model = captured_view_models[-1]
    assert view_model["id"] == SCAN_ID
    assert view_model["assessment_availability"] == "available"
    assert view_model["assessment_fingerprint"] == projected_identity
    assert view_model["score_fingerprint"] == projected_score_identity
    assert view_model["score"] == api_result["score"]["value"] == projected_score

    component_summaries = {
        component["key"]: component["drawer"]["tile_summary"]
        for component in view_model["components"]
    }
    assert sum(summary["lit"] + summary["off"] + summary["blind"] for summary in component_summaries.values()) == 80
    api_summaries = {
        component["key"]: component["tile_summary"]
        for component in api_result["components"]
    }
    assert {
        key: {
            "passed": summary["lit"],
            "failed": summary["off"],
            "insufficient_evidence": summary["blind"],
            "total": summary["lit"] + summary["off"] + summary["blind"],
        }
        for key, summary in component_summaries.items()
    } == api_summaries

    non_lit_ids = {
        tile_id
        for (_component, tile_id), state in _api_tile_state(api_result).items()
        if state != "ok"
    }
    rendered_tile_ids = set(re.findall(r'class="drawer-tile-id">([^<]+)', html_response.text))
    assert non_lit_ids <= rendered_tile_ids
    assert f"{projected_score}<span>/100</span>" in html_response.text
    assert "Score diagnóstico" not in html_response.text
    # The legacy wire token is retained for compatibility; its assessment and
    # identity must still be the authority projection consumed above.
    assert report["raw"]["schema_version"] == "sv9-flow-sv9-shadow-eval-v1"
    assert report["sv9_assessment"] == projected_assessment


def test_core_runtime_does_not_enter_authority_runner_with_authority_flags(monkeypatch) -> None:
    scan_id = "core-authority-bypass"
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "production")
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")
    monkeypatch.setenv("BRAND3_VAULT_SV9_AUTHORITY_SCANNER_ENABLED", "true")

    authority_calls: list[bool] = []
    legacy_calls: list[bool] = []
    persisted_statuses: list[dict] = []

    monkeypatch.setattr(
        scan_runner,
        "_run_vault_sv9_authority_scanner",
        lambda **_kwargs: authority_calls.append(True),
    )
    monkeypatch.setattr(
        scan_runner,
        "_capture_snapshot",
        lambda *_args, **_kwargs: {
            "run": {"id": 1},
            "raw_inputs": [],
            "acquisition_steps": {},
        },
    )
    monkeypatch.setattr(
        scan_runner,
        "_build_acquisition_gate",
        lambda *_args, **_kwargs: {
            "state": "pass",
            "issues": [],
            "warnings": [],
            "fallbacks": [],
        },
    )
    monkeypatch.setattr(scan_runner, "_persist_scan_status", persisted_statuses.append)
    monkeypatch.setattr(scan_runner.traceback, "print_exc", lambda: None)

    from scripts import sv9_flow_sv9_shadow_eval as legacy

    def stop_legacy_branch(*_args, **_kwargs):
        legacy_calls.append(True)
        raise RuntimeError("legacy-core-branch-reached")

    monkeypatch.setattr(legacy, "build_flow_sv9_shadow_eval", stop_legacy_branch)
    scan_runner._SCANS[scan_id] = _running_scan(scan_id)
    scan_runner._SCAN_EVENTS[scan_id] = scan_runner.threading.Event()
    try:
        assert scan_runner._vault_operational_pipeline_enabled() is False
        assert scan_runner._vault_sv9_authority_scanner_enabled() is False
        scan_runner._run(scan_id, "https://example.test", "Example", False)
        assert legacy_calls == [True]
        assert authority_calls == []
        assert scan_runner._SCANS[scan_id]["state"] == "error"
        assert persisted_statuses[-1]["error"] == "RuntimeError: legacy-core-branch-reached"
    finally:
        scan_runner._SCANS.pop(scan_id, None)
        scan_runner._SCAN_EVENTS.pop(scan_id, None)
        scan_runner._VAULT_ACTIVATIONS.discard(scan_id)
