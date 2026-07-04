from __future__ import annotations

import json

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
    assert 'href="/report/abc123"' in response.text
    assert "Vercel" in response.text
    assert "88" in response.text


def test_scan_submission_redirects_to_scan_view(monkeypatch):
    from web.app import app

    calls = []

    def fake_start_scan(url: str, brand_name: str = "") -> str:
        calls.append((url, brand_name))
        return "scan123"

    monkeypatch.setattr("web.app.start_scan", fake_start_scan)

    response = TestClient(app).post(
        "/scan",
        data={"url": "https://vercel.com", "brand_name": "Vercel"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/scan/scan123"
    assert calls == [("https://vercel.com", "Vercel")]


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
