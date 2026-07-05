from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def test_normalize_url_accepts_domains_and_rejects_bad_inputs():
    from web.scan_runner import default_brand_name, normalize_url

    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url(" HTTPS://EXAMPLE.COM/path ") == "https://example.com/path"
    assert default_brand_name("https://www.linear.app") == "Linear"

    with pytest.raises(ValueError, match="URL is required"):
        normalize_url(" ")
    with pytest.raises(ValueError, match="host must include a dot"):
        normalize_url("intranet")
    with pytest.raises(ValueError, match="private"):
        normalize_url("http://127.0.0.1")


def test_report_store_saves_loads_lists_and_ignores_corrupt_json(tmp_path, monkeypatch):
    from web import report_store

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.save_report(
        {
            "id": "older",
            "brand_name": "Older",
            "url": "https://older.test",
            "created_at": "2026-01-01T00:00:00+00:00",
            "score": 55,
            "detected_count": 2,
            "block_count": 9,
            "not_detected": ["vision"],
        }
    )
    report_store.save_report(
        {
            "id": "newer",
            "brand_name": "Newer",
            "url": "https://newer.test",
            "created_at": "2026-01-02T00:00:00+00:00",
            "score": 77,
            "detected_count": 4,
            "block_count": 9,
            "not_detected": [],
        }
    )
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    assert report_store.load_report("older")["brand_name"] == "Older"
    assert report_store.load_report("missing") is None
    assert [row["id"] for row in report_store.list_reports()] == ["newer", "older"]


def test_home_renders_report_list(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.list_reports",
        lambda: [
            {
                "id": "abc123",
                "brand_name": "Vercel",
                "url": "https://vercel.com",
                "score": 88,
                "detected_count": 8,
                "block_count": 9,
                "not_detected": [],
                "created_at": "2026-01-02T12:00:00+00:00",
            }
        ],
    )

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "Scan a brand" in response.text
    assert 'aria-label="B3S home"' in response.text
    assert 'viewBox="0 0 193 63"' in response.text
    assert 'href="/report/abc123"' in response.text
    assert "Vercel" in response.text
    assert "88" in response.text


def test_brand_view_renders_profile_from_matching_reports(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.list_reports_for_domain",
        lambda domain: [
            {
                "id": "report123",
                "brand_name": "Stabolut",
                "url": "https://stabolut.com",
                "created_at": "2026-07-03T12:00:00+00:00",
                "score": 79,
                "immediate_margin": 8,
                "most_painful_gap_label": "Propósito",
                "not_detected": ["values"],
                "components": [
                    {
                        "key": "core_purpose",
                        "status": "scored",
                        "resumen": "Stablecoin platform.",
                        "veredicto": "Purpose needs sharper proof.",
                        "block": {"refs": [{"url": "https://stabolut.com"}]},
                    }
                ],
            }
        ],
    )

    response = TestClient(app).get("/brand/stabolut.com?lang=es")

    assert response.status_code == 200
    assert "Stabolut" in response.text
    assert "Stablecoin platform." in response.text
    assert 'href="/report/report123"' in response.text
    assert "79" in response.text


def test_brand_view_handles_missing_scan(monkeypatch):
    from web.app import app

    monkeypatch.setattr("web.app.list_reports_for_domain", lambda domain: [])

    response = TestClient(app).get("/brand/stabolut.com?lang=es")

    assert response.status_code == 200
    assert "stabolut.com" in response.text
    assert "sin scan local" in response.text
    assert 'name="url" value="https://stabolut.com"' in response.text


def test_api_health_page_renders_config_without_secrets(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app._api_health",
        lambda run_checks=False: {
            "status": "ok",
            "run_checks": run_checks,
            "checked_at": "2026-07-04T00:00:00+00:00",
            "services": [
                {
                    "key": "firecrawl_scrape",
                    "label": "Firecrawl scrape",
                    "env_var": "FIRECRAWL_API_KEY",
                    "configured": True,
                    "enabled": True,
                    "status": "configured",
                    "detail": "",
                    "elapsed_ms": None,
                    "secret_suffix": "507d",
                }
            ],
        },
    )

    response = TestClient(app).get("/health/apis")

    assert response.status_code == 200
    assert "Estado de APIs" in response.text
    assert "Firecrawl scrape" in response.text
    assert "••••507d" in response.text
    assert "run checks" in response.text


def test_api_health_json_can_run_checks(monkeypatch):
    from web.app import app

    calls = []
    monkeypatch.setattr(
        "web.app._api_health",
        lambda run_checks=False: calls.append(run_checks)
        or {
            "status": "ok",
            "run_checks": run_checks,
            "checked_at": "2026-07-04T00:00:00+00:00",
            "services": [],
        },
    )

    response = TestClient(app).get("/api/health/apis?run=1")

    assert response.status_code == 200
    assert response.json()["run_checks"] is True
    assert calls == [True]


def test_disabled_api_health_service_does_not_degrade_without_secret():
    from web.app import _overall_api_status, _service_row

    row = _service_row(
        key="searchapi",
        label="SearchAPI fallback",
        env_var="SEARCHAPI_API_KEY",
        secret="",
        enabled=False,
        run_checks=False,
        check_fn=None,
    )

    assert row["status"] == "disabled"
    assert _overall_api_status([row]) == "ok"


def test_scan_submission_redirects_to_scan_view(monkeypatch):
    from web.app import app

    calls = []

    def fake_start_scan(url: str, brand_name: str = "", *, allow_degraded_fallback: bool = False) -> str:
        calls.append((url, brand_name, allow_degraded_fallback))
        return "scan123"

    monkeypatch.setattr("web.app.start_scan", fake_start_scan)

    response = TestClient(app).post(
        "/scan",
        data={"url": "https://vercel.com", "brand_name": "Vercel"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/scan/scan123"
    assert calls == [("https://vercel.com", "Vercel", False)]


def test_scan_submission_can_preapprove_degraded_fallback(monkeypatch):
    from web.app import app

    calls = []

    def fake_start_scan(url: str, brand_name: str = "", *, allow_degraded_fallback: bool = False) -> str:
        calls.append((url, brand_name, allow_degraded_fallback))
        return "scan123"

    monkeypatch.setattr("web.app.start_scan", fake_start_scan)

    response = TestClient(app).post(
        "/scan",
        data={
            "url": "https://vercel.com",
            "brand_name": "Vercel",
            "allow_degraded_fallback": "true",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert calls == [("https://vercel.com", "Vercel", True)]


def test_acquisition_gate_blocks_web_failure_without_safe_fallback():
    from web.scan_runner import _build_acquisition_gate

    gate = _build_acquisition_gate(
        {
            "web": {"status": "error", "error": "timeout", "details": {"reason": "timeout"}},
            "exa": {"status": "ok"},
        }
    )

    assert gate["state"] == "blocked"
    assert gate["can_continue"] is False
    assert gate["issues"][0]["code"] == "web_capture_failed"


def test_acquisition_gate_allows_exa_fallback_when_searchapi_is_available(monkeypatch):
    from web.scan_runner import _approve_acquisition_gate, _build_acquisition_gate

    monkeypatch.setattr("src.config.SEARCHAPI_API_KEY", "search-key")
    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "missing_key", "error": "EXA_API_KEY not set"},
            "searchapi": {"status": "ok", "details": {"result_total": 4}},
        }
    )

    assert gate["state"] == "blocked"
    assert gate["can_continue"] is True
    assert gate["fallbacks"][0]["source"] == "searchapi"

    approved = _approve_acquisition_gate(gate, decision_source="user")
    assert approved["state"] == "degraded_approved"
    assert approved["fallbacks"][0]["approved"] is True


def test_acquisition_gate_treats_exa_empty_as_warning():
    from web.scan_runner import _build_acquisition_gate

    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "empty", "details": {"no_result_intents": ["news"]}},
            "searchapi": {"status": "skipped"},
        }
    )

    assert gate["state"] == "warning"
    assert gate["issues"] == []
    assert gate["warnings"][0]["code"] == "exa_empty"
    assert gate["warnings"][0]["detail"] == "no_result_intents: news"


def test_acquisition_gate_does_not_warn_when_exa_has_external_proof_with_profile_gap():
    from web.scan_runner import _build_acquisition_gate

    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "ok", "details": {"no_result_intents": ["external_profiles"]}},
            "searchapi": {"status": "skipped"},
        }
    )

    assert gate["state"] == "pass"
    assert gate["warnings"] == []


def test_scan_control_endpoints_delegate_to_runner(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.approve_degraded_scan",
        lambda scan_id: {"state": "running", "approved": True, "id": scan_id},
    )
    monkeypatch.setattr(
        "web.app.cancel_scan",
        lambda scan_id: {"state": "cancelled", "cancelled": True, "id": scan_id},
    )
    client = TestClient(app)

    assert client.post("/api/scan/scan123/continue").json()["approved"] is True
    assert client.post("/api/scan/scan123/cancel").json()["cancelled"] is True


def test_visual_acquisition_rows_include_evidence_packet(monkeypatch):
    from web.scan_runner import _capture_visual_evidence

    calls = {}
    evidence = {
        "schema_version": "visual-signature-evidence-v1",
        "capture": {"status": "usable", "first_fold_evaluable": True},
        "tile_signals": [],
    }

    def fake_shadow(**kwargs):
        calls.update(kwargs)
        return {
            "status": "completed",
            "interpretation_status": "interpretable",
            "agreement_level": "high",
            "visual_signature_score": 74,
            "visual_signature_scan_status": "usable",
            "visual_signature_scan": {"schema_version": "visual-signature-scan-v1"},
            "visual_evidence_packet": evidence,
            "visual_signature_evidence": evidence,
        }

    service = SimpleNamespace(
        _take_screenshot_with_budget=lambda url, timeout_seconds=20: (
            {"screenshot_url": "file:///tmp/example.png", "screenshot_provider": "playwright"},
            None,
        ),
        _screenshot_capture_diagnostic=lambda attempted, screenshot_data=None, limitation=None: {
            "success": bool((screenshot_data or {}).get("screenshot_url")),
            "source": (screenshot_data or {}).get("screenshot_provider"),
            "screenshot_url": (screenshot_data or {}).get("screenshot_url"),
        },
        _run_visual_signature_shadow=fake_shadow,
    )

    rows, step = _capture_visual_evidence(
        service=service,
        enabled=True,
        url="https://example.com",
        brand_name="Example",
        web_data={"owned": True},
        content_web={"owned": True},
    )

    assert step == {"status": "completed", "details": {"reason": "visual_evidence_packet:usable"}}
    assert [row["source"] for row in rows] == ["screenshot_capture", "visual_acquisition"]
    assert rows[1]["payload"]["visual_evidence_packet"] == evidence
    assert rows[1]["payload"]["visual_signature_evidence"] == evidence
    assert calls["store"] is None
    assert calls["run_id"] is None
    assert calls["screenshot_capture"]["screenshot_url"] == "file:///tmp/example.png"


def test_visual_acquisition_persists_blocked_cookie_obstruction_detail(monkeypatch):
    from web.scan_runner import _build_acquisition_gate, _capture_visual_evidence

    evidence = {
        "schema_version": "visual-signature-evidence-v1",
        "capture": {
            "status": "blocked",
            "first_fold_evaluable": False,
            "obstruction": {
                "present": True,
                "type": "cookie_banner",
                "severity": "blocking",
                "signals": ["dom_keyword:cookie", "dom_keyword:privacy"],
            },
        },
        "tile_signals": [],
    }

    service = SimpleNamespace(
        _take_screenshot_with_budget=lambda url, timeout_seconds=20: (
            {"screenshot_url": "file:///tmp/example.png", "screenshot_provider": "playwright"},
            None,
        ),
        _screenshot_capture_diagnostic=lambda attempted, screenshot_data=None, limitation=None: {
            "success": bool((screenshot_data or {}).get("screenshot_url")),
            "source": (screenshot_data or {}).get("screenshot_provider"),
            "screenshot_url": (screenshot_data or {}).get("screenshot_url"),
        },
        _run_visual_signature_shadow=lambda **_kwargs: {
            "status": "completed",
            "visual_evidence_packet": evidence,
            "visual_signature_evidence": evidence,
        },
    )

    _rows, step = _capture_visual_evidence(
        service=service,
        enabled=True,
        url="https://example.com",
        brand_name="Example",
        web_data=None,
        content_web=None,
    )

    assert step["details"]["reason"] == "visual_evidence_packet:blocked"
    assert "obstruction:cookie_banner" in step["details"]["blocked_reason"]
    assert step["details"]["cookie_banner_suspected"] is True

    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "ok"},
            "searchapi": {"status": "ok"},
            "visual_acquisition": step,
        }
    )
    visual_warning = next(item for item in gate["warnings"] if item["source"] == "visual_acquisition")
    assert "blocked_reason: obstruction:cookie_banner" in visual_warning["detail"]


def test_visual_acquisition_failure_does_not_drop_screenshot_row():
    from web.scan_runner import _capture_visual_evidence

    def fake_shadow(**_kwargs):
        raise RuntimeError("visual model unavailable")

    service = SimpleNamespace(
        _take_screenshot_with_budget=lambda url, timeout_seconds=20: (
            {"screenshot_url": "file:///tmp/example.png", "screenshot_provider": "playwright"},
            None,
        ),
        _screenshot_capture_diagnostic=lambda attempted, screenshot_data=None, limitation=None: {
            "success": bool((screenshot_data or {}).get("screenshot_url")),
            "source": (screenshot_data or {}).get("screenshot_provider"),
            "screenshot_url": (screenshot_data or {}).get("screenshot_url"),
        },
        _run_visual_signature_shadow=fake_shadow,
    )

    rows, step = _capture_visual_evidence(
        service=service,
        enabled=True,
        url="https://example.com",
        brand_name="Example",
        web_data=None,
        content_web=None,
    )

    assert step["status"] == "error"
    assert "visual model unavailable" in step["details"]["reason"]
    assert rows == [
        {
            "source": "screenshot_capture",
            "payload": {
                "version": "screenshot_capture_v1",
                "url": "https://example.com",
                "content_source": "b3s_live_scan",
                "skip_visual_analysis": False,
                "capture": {
                    "success": True,
                    "source": "playwright",
                    "screenshot_url": "file:///tmp/example.png",
                },
            },
        }
    ]


def test_acquisition_gate_warns_when_web_capture_looks_like_cookie_banner():
    from web.scan_runner import _build_acquisition_gate

    gate = _build_acquisition_gate(
        {
            "web": {
                "status": "ok",
                "details": {
                    "reason": "captured",
                    "cookie_banner_suspected": True,
                    "cookie_banner_snippet": "Valoramos tu privacidad. Aceptar Rechazar Configurar.",
                },
            },
            "exa": {"status": "ok"},
            "searchapi": {"status": "ok"},
        }
    )

    warning = next(item for item in gate["warnings"] if item["code"] == "web_cookie_banner_suspected")
    assert warning["severity"] == "warning"
    assert "cookie_banner_snippet: Valoramos tu privacidad" in warning["detail"]


def test_capture_snapshot_adds_visual_acquisition_raw_input(monkeypatch):
    from src.services import brand_service
    from web.scan_runner import _capture_snapshot

    evidence = {
        "schema_version": "visual-signature-evidence-v1",
        "capture": {"status": "usable"},
        "tile_signals": [],
    }

    monkeypatch.setattr("src.config.BRAND3_VISUAL_SIGNATURE_SCAN_ENABLED", True)
    monkeypatch.setattr(
        brand_service,
        "collect_raw_inputs",
        lambda **_kwargs: SimpleNamespace(
            context_data=SimpleNamespace(status="ok", to_dict=lambda: {"url": "https://example.com"}),
            web_data=SimpleNamespace(status="ok", to_dict=lambda: {"url": "https://example.com"}),
            exa_data=None,
            github_data=None,
            searchapi_data=None,
            acquisition_steps={
                "web": SimpleNamespace(
                    status="ok",
                    to_dict=lambda: {"status": "ok", "details": {"reason": "captured"}},
                )
            },
        ),
    )
    monkeypatch.setattr(
        brand_service,
        "_take_screenshot_with_budget",
        lambda url, timeout_seconds=20: (
            {"screenshot_url": "file:///tmp/example.png", "screenshot_provider": "playwright"},
            None,
        ),
    )
    monkeypatch.setattr(
        brand_service,
        "_screenshot_capture_diagnostic",
        lambda attempted, screenshot_data=None, limitation=None: {
            "success": True,
            "source": (screenshot_data or {}).get("screenshot_provider"),
            "screenshot_url": (screenshot_data or {}).get("screenshot_url"),
        },
    )
    monkeypatch.setattr(
        brand_service,
        "_run_visual_signature_shadow",
        lambda **_kwargs: {
            "status": "completed",
            "visual_signature_scan": {"schema_version": "visual-signature-scan-v1"},
            "visual_evidence_packet": evidence,
            "visual_signature_evidence": evidence,
        },
    )

    snapshot = _capture_snapshot("scan123", "https://example.com", "Example")

    raw_sources = [row["source"] for row in snapshot["raw_inputs"]]
    assert raw_sources == ["context", "web", "screenshot_capture", "visual_acquisition"]
    assert snapshot["raw_inputs"][-1]["payload"]["visual_evidence_packet"] == evidence
    assert snapshot["acquisition_steps"]["visual_acquisition"]["status"] == "completed"


def test_scan_and_api_fall_back_to_finished_report(monkeypatch):
    from web.app import app

    monkeypatch.setattr("web.app.scan_status", lambda scan_id: None)
    monkeypatch.setattr("web.app.load_report", lambda scan_id: {"id": scan_id})
    client = TestClient(app)

    page = client.get("/scan/done123", follow_redirects=False)
    api = client.get("/api/scan/done123")

    assert page.status_code == 303
    assert page.headers["location"] == "/report/done123"
    assert api.status_code == 200
    assert api.json() == {"state": "done", "id": "done123"}


def test_report_view_renders_report(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Mercury",
            "url": "https://mercury.com",
            "score": 81,
            "base_average": 72,
            "reliability_status": "shadow",
            "detected_count": 1,
            "block_count": 1,
            "not_detected": [],
            "most_painful_gap_label": "",
            "immediate_margin": None,
            "total_blind_spots": 0,
            "coverage_acquisition": {
                "owned_url_count": 1,
                "external_source_count": 0,
                "absence_record_count": 0,
                "attempt_record_count": 0,
            },
            "components": [
                {
                    "key": "core_purpose",
                    "label": "Propósito",
                    "score": 4,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "Purpose text.",
                    "veredicto": "",
                    "tile_states": [{"id": "PX9", "name": "Hidden tile", "state": "off"}],
                    "tiles": [
                        {
                            "id": "P2",
                            "name": "Tensión explícita",
                            "estado": "no",
                            "motivo": "No aparece una tensión propia.",
                            "evidencia": "Texto de apoyo.",
                        }
                    ],
                    "block": None,
                },
                {
                    "key": "brand_idea",
                    "label": "Idea de marca",
                    "score": 3,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "Brand idea text.",
                    "veredicto": "",
                    "tile_states": [],
                    "tiles": [],
                    "block": None,
                },
                {
                    "key": "coherencia",
                    "label": "Coherencia",
                    "score": 7,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "",
                    "veredicto": "Coherence text.",
                    "tile_states": [],
                    "tiles": [],
                    "block": None,
                },
            ],
            "blocks": [],
            "absences": [],
            "attempts": [],
            "limitations": [],
            "raw": {"ok": True},
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert "Mercury" in response.text
    assert "https://mercury.com" in response.text
    assert "report-hero" in response.text
    assert "Brand3 Score" in response.text
    assert "--score-width: 81%;" in response.text
    assert "shadow run" not in response.text
    assert 'id="core_purpose"' in response.text
    assert "component-card--half" in response.text
    assert 'aria-label="Abrir lectura de Propósito"' in response.text
    assert 'data-dialog-target="drawer-core_purpose"' in response.text
    assert 'class="report-drawer" id="drawer-core_purpose"' in response.text
    assert 'class="tiles"' not in response.text
    assert "PX9" not in response.text
    assert "P2" in response.text
    assert 'id="brand_idea"' in response.text
    assert "component-card--third" in response.text
    assert 'id="coherencia"' in response.text
    assert "component-card--full" in response.text
    assert json.dumps({"ok": True}) not in response.text
