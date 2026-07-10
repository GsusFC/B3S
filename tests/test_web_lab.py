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
    assert 'href="/brand/vercel.com?lang=es"' in response.text
    assert 'href="/report/abc123/moodboard?lang=es"' not in response.text
    assert "Vercel" in response.text
    assert "88" in response.text
    assert "status-tag status-tag--ok status-tag--filled" in response.text


def test_visual_signature_logo_url_falls_back_to_real_logo_candidate():
    from web.app import _visual_signature_logo_url

    visual_payload = {
        "schema_version": "visual-signature-persistence-1",
        "visual_signature_evidence": {
            "schema_version": "visual-signature-evidence-v1",
            "identity": {
                "candidates": [
                    {
                        "url": "https://stabolut.com/assets/brand-mark.svg",
                        "role": "real_logo",
                        "location": "header",
                        "confidence": 0.91,
                    }
                ]
            },
        },
    }
    report = {
        "id": "scan123",
        "brand_name": "Stabolut",
        "url": "https://stabolut.com",
        "score": 79,
        "blocks": [],
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [
                            {
                                "source": "visual_acquisition",
                                "evidence_type": "raw_input",
                                "content": json.dumps(visual_payload),
                            }
                        ]
                    }
                }
            }
        },
    }

    assert _visual_signature_logo_url(report) == "https://stabolut.com/assets/brand-mark.svg"


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


def test_brand_view_prefers_component_editorial_message(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.list_reports_for_domain",
        lambda domain: [
            {
                "id": "report123",
                "brand_name": "Optiak",
                "url": "https://optiak.com",
                "created_at": "2026-07-07T12:00:00+00:00",
                "score": 65,
                "components": [
                    {
                        "key": "core_purpose",
                        "status": "scored",
                        "resumen": "Componente Propósito detectado: 7/10 baldosas encendidas.",
                        "resumen_is_fallback": True,
                        "veredicto": "Propósito técnico sólido.",
                        "message": "Lectura editorial de propósito.",
                        "block": None,
                    },
                    {
                        "key": "value_proposition",
                        "status": "scored",
                        "resumen": "Componente Propuesta de valor detectado.",
                        "resumen_is_fallback": True,
                        "veredicto": "Propuesta aún demasiado técnica.",
                        "message": "Lectura editorial de propuesta.",
                        "block": None,
                    },
                ],
                "blocks": [],
                "raw": {},
            }
        ],
    )

    response = TestClient(app).get("/brand/optiak.com?lang=es")

    assert response.status_code == 200
    assert "Lectura editorial de propósito." in response.text
    assert "Lectura editorial de propuesta." in response.text
    assert "Componente Propósito detectado" not in response.text


def test_brand_view_embeds_visual_module_from_latest_report(monkeypatch):
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
                "components": [],
                "blocks": [{"name": "brand_idea", "detected": True, "content": "Beyond digital dollars."}],
                "raw": {
                    "flow": {
                        "candidate": {
                            "evidence_pack": {
                                "evidence": [
                                    {
                                        "source": "web",
                                        "evidence_type": "raw_input",
                                        "content": "![Layered cube](https://stabolut.com/assets/card-overcollateralized.png)",
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ],
    )

    response = TestClient(app).get("/brand/stabolut.com?lang=es")

    assert response.status_code == 200
    assert "módulo_visual" in response.text
    assert "moodboard-stage--brand" in response.text
    assert "data-moodboard-stage" in response.text
    assert "/static/brand3_cloud.js" in response.text
    assert "/static/moodboard.js" in response.text
    assert '<div class="moodboard-strip"' not in response.text
    assert "https://stabolut.com/assets/card-overcollateralized.png" in response.text
    assert 'href="#modulo-visual"' in response.text


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


def test_acquisition_artifacts_include_screenshot_and_visual_obstruction(tmp_path, monkeypatch):
    from web.scan_runner import _acquisition_artifacts_from_snapshot

    screenshot_dir = tmp_path / "data" / "screenshots"
    screenshot_dir.mkdir(parents=True)
    screenshot = screenshot_dir / "brand3-screenshot-test.png"
    screenshot.write_bytes(b"png")
    monkeypatch.chdir(tmp_path)

    artifacts = _acquisition_artifacts_from_snapshot(
        {
            "raw_inputs": [
                {
                    "source": "screenshot_capture",
                    "payload": {
                        "capture": {
                            "status": "captured",
                            "success": True,
                            "source": "playwright",
                            "screenshot_url": screenshot.as_uri(),
                            "screenshot_path": str(screenshot),
                            "metadata": {"title": "Stabolut"},
                        }
                    },
                },
                {
                    "source": "visual_acquisition",
                    "payload": {
                        "visual_evidence_packet": {
                            "capture": {
                                "status": "blocked",
                                "first_fold_evaluable": False,
                                "obstruction": {
                                    "present": True,
                                    "type": "cookie_modal",
                                    "severity": "major",
                                    "signals": ["dom_keyword:privacy"],
                                },
                            }
                        }
                    },
                },
            ]
        }
    )

    assert artifacts[0]["public_url"] == "/artifacts/screenshots/brand3-screenshot-test.png"
    assert artifacts[0]["metadata"]["title"] == "Stabolut"
    assert artifacts[1]["status"] == "blocked"
    assert artifacts[1]["obstruction"]["type"] == "cookie_modal"


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


def test_scan_view_uses_structured_layout(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.scan_status",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Mercury",
            "url": "https://mercury.com",
            "state": "running",
            "phases": [
                {"key": "capture", "label": "Capture evidence", "state": "running"},
                {"key": "interpret", "label": "Interpret SV9", "state": "pending"},
            ],
        },
    )

    response = TestClient(app).get("/scan/scan123")

    assert response.status_code == 200
    assert "scan-shell" in response.text
    assert "scan-card" in response.text
    assert "scan-meter" in response.text
    assert "scan-steps" in response.text
    assert "scan-gate-actions" in response.text
    assert "scan-acquisition-table" in response.text
    assert "Detalle técnico" in response.text
    assert "https://mercury.com" in response.text
    assert 'style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px"' not in response.text


def test_scan_preview_renders_without_live_scan():
    from web.app import app

    response = TestClient(app).get("/dev/scan-preview?variant=blocked")

    assert response.status_code == 200
    assert "preview-blocked" in response.text
    assert "scan-shell" in response.text
    assert "visual_acquisition" in response.text
    assert "visual_evidence_packet:blocked" in response.text
    assert "status-tag status-tag--warn status-tag--filled" in response.text
    assert ">limited</span>" in response.text
    assert "const scanPreview = true" in response.text


def test_scan_view_marks_blocked_visual_packet_as_limited_warning(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.scan_status",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "state": "running",
            "phases": [
                {"key": "capture", "label": "Capture evidence", "state": "running"},
            ],
            "acquisition": [
                {
                    "source": "visual_acquisition",
                    "status": "completed",
                    "detail": "visual_evidence_packet:blocked; blocked_reason: obstruction:cookie_modal",
                }
            ],
            "acquisition_gate": {
                "state": "warning",
                "issues": [],
                "warnings": [
                    {
                        "source": "visual_acquisition",
                        "code": "visual_acquisition_limited",
                        "status": "completed",
                        "message": "Visual acquisition was obstructed.",
                    }
                ],
            },
        },
    )

    response = TestClient(app).get("/scan/scan123")

    assert response.status_code == 200
    assert "visual_evidence_packet:blocked" in response.text
    assert "status-tag status-tag--warn status-tag--filled" in response.text
    assert ">limited</span>" in response.text


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


def test_report_view_hides_automatic_verdict_from_card_without_tile_profile(monkeypatch):
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
                    "key": "magnetism",
                    "label": "Magnetism",
                    "score": 3,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "The snapshot does not provide access to the full product interface.",
                    "veredicto": "The snapshot does not provide access to the full product interface.",
                    "lit": 3,
                    "off": 1,
                    "blind": 6,
                    "tile_states": [{"id": "MX9", "name": "Resilience", "state": "off"}],
                    "tiles": [],
                    "block": None,
                }
            ],
            "blocks": [],
            "absences": [],
            "attempts": [],
            "limitations": [],
            "raw": {},
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert "Síntesis automática: 3/10 baldosas encendidas, 1 apagada, 6 puntos ciegos." not in response.text
    assert "The snapshot does not provide access" not in response.text
    assert "0/10 baldosas encendidas" not in response.text


def test_compose_report_preserves_canonical_sv9_tile_profile():
    from src.sv9.rubric import tile_ids
    from web.scan_runner import _compose_report

    ids = tile_ids("magnetism")
    tile_profile = [
        {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
        {"id": ids[1], "estado": "ok", "evidencia": "dolor visible"},
        {"id": ids[2], "estado": "ok", "evidencia": "deseo visible"},
        {"id": ids[3], "estado": "no", "motivo": "No hay contraste narrativo."},
        *[
            {
                "id": tile_id,
                "estado": "sin_evidencia",
                "motivo": "El snapshot no aporta prueba externa.",
                "contexto_requerido": "Aporta entrevistas o métricas de adopción.",
            }
            for tile_id in ids[4:]
        ],
    ]
    payload = {
        "schema_version": "test",
        "source_run_id": 1,
        "flow": {
            "candidate": {"interpretation": {"blocks": {}}, "evidence_pack": {"evidence": []}},
            "interpretation_debug": {},
        },
        "sv9": {
            "brand3_score": 61,
            "base_average": 6.1,
            "components": {
                "magnetism": {
                    "status": "scored",
                    "score": 3,
                    "lit_tiles": ids[:3],
                    "off_tiles": [ids[3]],
                    "blind_spot_tiles": ids[4:],
                }
            },
            "result": {
                "brand3_score": 61,
                "components": {
                    "magnetism": {
                        "component": "magnetism",
                        "status": "scored",
                        "score": 3,
                        "scale": 10,
                        "points": 6,
                        "confidence": "baja",
                        "detected_content": "Promesa de soberanía tecnológica.",
                        "veredicto": "La marca tiene utilidad técnica, pero necesita más tensión narrativa.",
                        "tile_profile": tile_profile,
                    }
                },
                "most_painful_gap": "magnetism",
                "immediate_margin": 8,
                "total_blind_spots": 6,
            },
        },
    }

    report = _compose_report("scan123", "https://optiak.com", "Optiak", payload)
    magnetism = next(component for component in report["components"] if component["key"] == "magnetism")

    assert len(magnetism["tile_profile"]) == 10
    assert magnetism["lit"] == 3
    assert magnetism["off"] == 1
    assert magnetism["blind"] == 6
    assert magnetism["resumen"] == "Promesa de soberanía tecnológica."
    assert "tensión narrativa" in magnetism["veredicto"]
    assert magnetism["tiles"][0]["id"] == ids[3]
    assert magnetism["tiles"][1]["contexto_requerido"] == "Aporta entrevistas o métricas de adopción."


def test_attach_sv9_editorial_only_requests_components_with_unusable_prose():
    from src.sv9.rubric import tile_ids
    from web.scan_runner import _attach_sv9_editorial

    ids = tile_ids("magnetism")
    payload = {
        "sv9": {
            "result": {
                "brand_name": "Optiak",
                "url": "https://optiak.com",
                "brand3_score": 61,
                "editorial_v3_1": {
                    "schema_version": "sv9_editorial_v3_1",
                    "components": {
                        "magnetism": {"diagnosis": "Diagnóstico estructurado."},
                        "mission": {"diagnosis": "Diagnóstico estructurado."},
                    },
                },
                "components": {
                    "magnetism": {
                        "component": "magnetism",
                        "status": "scored",
                        "score": 3,
                        "scale": 10,
                        "veredicto": "The snapshot does not provide enough narrative evidence.",
                        "tile_profile": [
                            {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
                            {"id": ids[1], "estado": "no", "motivo": "falta"},
                        ],
                    },
                    "mission": {
                        "component": "mission",
                        "status": "scored",
                        "score": 5,
                        "scale": 5,
                        "veredicto": "La misión está formulada con claridad.",
                        "tile_profile": [],
                    },
                },
            }
        }
    }
    calls = []

    class FakeLLM:
        api_key = "test-key"

    def fake_build_editorial(scan, *, llm, component_keys, include_executive_reading):
        calls.append(
            {
                "component_keys": list(component_keys),
                "include_executive_reading": include_executive_reading,
            }
        )
        return {
            "component_messages": {
                "magnetism": "La marca necesita convertir utilidad técnica en tensión narrativa."
            },
            "executive_reading": "Lectura ejecutiva del scan.",
            "structured": {
                "schema_version": "sv9_editorial_v3_1",
                "executive_reading": "Lectura ejecutiva del scan.",
                "components": {
                    "magnetism": {
                        "diagnosis": "La marca necesita convertir utilidad técnica en tensión narrativa.",
                        "detected_basis": "Base detectada.",
                        "next_artifact": "Narrativa de tensión.",
                        "terms": [],
                    }
                },
            },
        }

    result = _attach_sv9_editorial(
        payload,
        llm=FakeLLM(),
        build_editorial_fn=fake_build_editorial,
    )

    assert calls == [{"component_keys": ["magnetism"], "include_executive_reading": True}]
    components = result["sv9"]["result"]["components"]
    assert components["magnetism"]["message"] == "La marca necesita convertir utilidad técnica en tensión narrativa."
    assert "message" not in components["mission"]
    assert result["sv9"]["result"]["executive_reading"] == "Lectura ejecutiva del scan."
    assert result["sv9"]["result"]["editorial_v3_1"]["schema_version"] == "sv9_editorial_v3_1"


def test_attach_sv9_editorial_requests_all_components_when_structured_contract_is_missing():
    from src.sv9.rubric import tile_ids
    from web.scan_runner import _attach_sv9_editorial

    ids = tile_ids("magnetism")
    payload = {
        "sv9": {
            "result": {
                "brand_name": "Optiak",
                "url": "https://optiak.com",
                "brand3_score": 61,
                "components": {
                    "magnetism": {
                        "component": "magnetism",
                        "status": "scored",
                        "score": 3,
                        "scale": 10,
                        "message": "La marca necesita convertir utilidad técnica en tensión narrativa.",
                        "veredicto": "Síntesis automática: 3/10 baldosas encendidas.",
                        "tile_profile": [
                            {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
                            {"id": ids[1], "estado": "no", "motivo": "falta"},
                        ],
                    },
                    "mission": {
                        "component": "mission",
                        "status": "scored",
                        "score": 5,
                        "scale": 5,
                        "message": "La misión está formulada con claridad.",
                        "veredicto": "La misión está formulada con claridad.",
                        "tile_profile": [],
                    },
                },
            }
        }
    }
    calls = []

    class FakeLLM:
        api_key = "test-key"

    def fake_build_editorial(scan, *, llm, component_keys, include_executive_reading):
        calls.append(
            {
                "component_keys": list(component_keys),
                "include_executive_reading": include_executive_reading,
            }
        )
        return {"component_messages": {}, "executive_reading": None}

    _attach_sv9_editorial(
        payload,
        llm=FakeLLM(),
        build_editorial_fn=fake_build_editorial,
    )

    assert calls == [{"component_keys": ["magnetism", "mission"], "include_executive_reading": True}]


def test_attach_sv9_editorial_skips_when_evaluator_messages_exist():
    from src.sv9.rubric import tile_ids
    from web.scan_runner import _attach_sv9_editorial

    ids = tile_ids("magnetism")
    payload = {
        "sv9": {
            "result": {
                "brand_name": "Optiak",
                "url": "https://optiak.com",
                "brand3_score": 61,
                "components": {
                    "magnetism": {
                        "component": "magnetism",
                        "status": "scored",
                        "score": 3,
                        "scale": 10,
                        "message": "La marca necesita convertir utilidad técnica en tensión narrativa.",
                        "veredicto": "Síntesis automática: 3/10 baldosas encendidas.",
                        "tile_profile": [
                            {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
                            {"id": ids[1], "estado": "no", "motivo": "falta"},
                        ],
                    },
                    "mission": {
                        "component": "mission",
                        "status": "scored",
                        "score": 5,
                        "scale": 5,
                        "message": "La misión está formulada con claridad.",
                        "veredicto": "La misión está formulada con claridad.",
                        "tile_profile": [],
                    },
                },
            }
        }
    }

    def fail_build_editorial(*_args, **_kwargs):
        raise AssertionError("editorial fallback should not run")

    result = _attach_sv9_editorial(
        payload,
        llm=object(),
        build_editorial_fn=fail_build_editorial,
    )

    assert "editorial" not in result["sv9"]
    assert result["sv9"]["result"]["components"]["magnetism"]["message"].startswith("La marca")


def test_report_view_rehydrates_reduced_projection_from_raw_sv9_result(monkeypatch):
    from src.sv9.rubric import tile_ids
    from web.app import app

    ids = tile_ids("magnetism")
    tile_profile = [
        {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
        {"id": ids[1], "estado": "ok", "evidencia": "dolor visible"},
        {"id": ids[2], "estado": "ok", "evidencia": "deseo visible"},
        {"id": ids[3], "estado": "no", "motivo": "No hay contraste narrativo."},
        *[
            {
                "id": tile_id,
                "estado": "sin_evidencia",
                "motivo": "El snapshot no aporta prueba externa.",
                "contexto_requerido": "Aporta entrevistas o métricas de adopción.",
            }
            for tile_id in ids[4:]
        ],
    ]
    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 61,
            "base_average": 61,
            "reliability_status": "shadow",
            "detected_count": 1,
            "block_count": 1,
            "not_detected": [],
            "most_painful_gap_label": "Magnetism",
            "immediate_margin": 8,
            "total_blind_spots": 6,
            "coverage_acquisition": {
                "owned_url_count": 1,
                "external_source_count": 0,
                "absence_record_count": 0,
                "attempt_record_count": 0,
            },
            "components": [
                {
                    "key": "magnetism",
                    "label": "Magnetism",
                    "score": 3,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "Componente Magnetism detectado: 3/10 baldosas encendidas.",
                    "veredicto": "Síntesis automática: 3/10 baldosas encendidas, 1 apagada, 6 puntos ciegos.",
                    "lit": 3,
                    "off": 1,
                    "blind": 6,
                    "tiles": [],
                    "block": None,
                }
            ],
            "blocks": [],
            "absences": [],
            "attempts": [],
            "limitations": [],
            "raw": {
                "sv9": {
                    "result": {
                        "brand3_score": 61,
                        "model": "v3.1",
                        "components": {
                            "magnetism": {
                                "component": "magnetism",
                                "status": "scored",
                                "score": 3,
                                "scale": 10,
                                "points": 6,
                                "confidence": "baja",
                                "detected_content": "Promesa de soberanía tecnológica.",
                                "veredicto": "La marca tiene utilidad técnica, pero necesita más tensión narrativa.",
                                "tile_profile": tile_profile,
                            }
                        },
                    }
                }
            },
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert "Promesa de soberanía tecnológica." in response.text
    assert "La marca tiene utilidad técnica" in response.text
    assert "Síntesis automática" not in response.text
    assert ids[3] in response.text
    assert "aporta contexto: Aporta entrevistas o métricas de adopción." in response.text


def test_report_markdown_exports_raw_sv9_contract(monkeypatch):
    from src.sv9.rubric import tile_ids
    from web.app import app

    ids = tile_ids("magnetism")
    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 61,
            "components": [],
            "raw": {
                "sv9": {
                    "result": {
                        "brand_name": "Optiak",
                        "url": "https://optiak.com",
                        "brand3_score": 61,
                        "model": "v3.1",
                        "components": {
                            "magnetism": {
                                "component": "magnetism",
                                "status": "scored",
                                "score": 3,
                                "scale": 10,
                                "points": 6,
                                "confidence": "baja",
                                "detected_content": "Promesa de soberanía tecnológica.",
                                "message": "Lectura editorial de Magnetism.",
                                "veredicto": "La marca necesita más tensión narrativa.",
                                "tile_profile": [
                                    {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
                                    {"id": ids[1], "estado": "ok", "evidencia": "dolor visible"},
                                    {"id": ids[2], "estado": "ok", "evidencia": "deseo visible"},
                                    {"id": ids[3], "estado": "no", "motivo": "No hay contraste narrativo."},
                                ],
                            }
                        },
                    }
                }
            },
        },
    )

    response = TestClient(app).get("/report/report123.md")

    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]
    assert "# Brand3 Scanner — Optiak" in response.text
    assert "Brand3 Score: **61/100**" in response.text
    assert "## Magnetism" in response.text
    assert "Lectura editorial de Magnetism." in response.text


def test_report_view_renders_coherencia_once_and_prioritizes_editorial_message(monkeypatch):
    from web.app import app

    base_component = {
        "score": 5,
        "scale": 10,
        "status": "scored",
        "resumen": "Texto detectado.",
        "veredicto": "Veredicto estratégico.",
        "tile_states": [],
        "tiles": [],
        "block": None,
    }
    components = [
        {**base_component, "key": "core_purpose", "label": "Propósito"},
        {
            **base_component,
            "key": "coherencia",
            "label": "Coherencia",
            "score": 6,
            "resumen": "Componente Coherencia detectado: 6/10 baldosas encendidas, 0 apagadas, 4 puntos ciegos. Revisa las fuentes para validar el matiz exacto.",
            "veredicto": "Optiak construye un discurso técnico sólido.",
            "message": "Lectura editorial de coherencia.",
        },
    ]
    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 65,
            "base_average": 65,
            "reliability_status": "shadow",
            "detected_count": 2,
            "block_count": 2,
            "not_detected": [],
            "most_painful_gap_label": "Magnetism",
            "immediate_margin": 8,
            "total_blind_spots": 4,
            "coverage_acquisition": {
                "owned_url_count": 1,
                "external_source_count": 0,
                "absence_record_count": 0,
                "attempt_record_count": 0,
            },
            "components": components,
            "blocks": [],
            "absences": [],
            "attempts": [],
            "limitations": [],
            "raw": {},
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert response.text.count('id="coherencia"') == 1
    assert "Lectura editorial de coherencia." in response.text
    assert "Optiak construye un discurso técnico sólido." in response.text
    assert "Componente Coherencia detectado" not in response.text


def test_report_view_does_not_instantiate_llm_analyzer(monkeypatch):
    from web.app import app

    def fail_llm(*_args, **_kwargs):
        raise AssertionError("report view must not instantiate LLMAnalyzer")

    monkeypatch.setattr("src.features.llm_analyzer.LLMAnalyzer", fail_llm)
    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 65,
            "components": [
                {
                    "key": "mission",
                    "label": "Misión",
                    "score": 5,
                    "scale": 5,
                    "status": "scored",
                    "message": "Lectura persistida.",
                    "detected_content": "Misión detectada.",
                }
            ],
            "raw": {},
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert "Lectura persistida." in response.text
    assert "Base detectada" in response.text
    assert "Misión detectada." in response.text
    assert '<p class="card-verdict">Misión detectada.</p>' in response.text
