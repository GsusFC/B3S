from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path

from src.services import screenshot_runtime
from src.services.visual_signature_snapshot import _visual_signature_shadow_screenshot_payload


class _FakeLocator:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.clicked = False

    @property
    def first(self):
        return self

    def click(self, *, timeout: int):
        self.clicked = True
        if self.fail:
            raise RuntimeError("not found")


class _FakePage:
    def __init__(self, *, fail: bool = False):
        self.locator_arg = ""
        self.locator_obj = _FakeLocator(fail=fail)
        self.waited = False

    def locator(self, selector: str):
        self.locator_arg = selector
        return self.locator_obj

    def wait_for_timeout(self, _timeout_ms: int):
        self.waited = True


def _slow_screenshot_worker(_output_queue, _url, _provider, **_kwargs):
    time.sleep(3)


def _png_bytes(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    row = b"\x00" + (b"\xff\xff\xff" * width)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(row * height)),
            chunk(b"IEND", b""),
        ]
    )


def test_dismiss_cookie_banner_once_reuses_shared_cookie_controls():
    page = _FakePage()

    result = screenshot_runtime._dismiss_cookie_banner_once(page)

    assert result["attempted"] is True
    assert result["success"] is True
    assert "Aceptar" in page.locator_arg
    assert "Accept all" in page.locator_arg
    assert page.locator_obj.clicked is True
    assert page.waited is True


def test_dismiss_cookie_banner_once_records_failed_attempt():
    page = _FakePage(fail=True)

    result = screenshot_runtime._dismiss_cookie_banner_once(page)

    assert result["attempted"] is True
    assert result["success"] is False
    assert "not found" in result["error"]


def test_remote_firecrawl_screenshot_is_persisted_as_local_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(screenshot_runtime, "BRAND3_SCREENSHOT_DIR", str(tmp_path))

    persisted = screenshot_runtime._persist_remote_screenshot(
        {
            "screenshot_url": "https://cdn.example.test/signed-shot.png",
            "screenshot_provider": "firecrawl_screenshot",
            "metadata": {"provider": "firecrawl"},
        },
        download_remote_screenshot=lambda _url: (_png_bytes(), "image/png"),
    )

    screenshot_path = Path(str(persisted["screenshot_path"]))
    assert screenshot_path.parent == tmp_path
    assert screenshot_path.is_file()
    assert str(persisted["screenshot_url"]).startswith("file://")
    assert persisted["remote_screenshot_url"] == "https://cdn.example.test/signed-shot.png"
    assert persisted["metadata"]["persisted_locally"] is True
    assert persisted["metadata"]["width"] == 2
    assert persisted["metadata"]["height"] == 2


def test_remote_firecrawl_screenshot_persistence_failure_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(screenshot_runtime, "BRAND3_SCREENSHOT_DIR", str(tmp_path))

    failed = screenshot_runtime._persist_remote_screenshot(
        {
            "screenshot_url": "https://cdn.example.test/not-an-image",
            "screenshot_provider": "firecrawl_screenshot",
        },
        download_remote_screenshot=lambda _url: (b"not a png", "text/html"),
    )

    assert "screenshot_url" not in failed
    assert failed["error_type"] == "persistence_error"
    assert "remote_screenshot_not_png" in str(failed["error"])
    assert list(tmp_path.iterdir()) == []


def test_visual_snapshot_propagates_same_capture_obstruction_metadata(tmp_path):
    screenshot = tmp_path / "capture.png"
    full_page = tmp_path / "capture.full-page.png"
    section = tmp_path / "capture.section-01-hero.png"
    atlas = tmp_path / "capture.analysis-atlas.png"
    screenshot.write_bytes(_png_bytes())
    full_page.write_bytes(_png_bytes())
    section.write_bytes(_png_bytes())
    atlas.write_bytes(_png_bytes())
    obstruction = {
        "present": False,
        "type": "none",
        "severity": "none",
        "confidence": 0.8,
    }

    payload = _visual_signature_shadow_screenshot_payload(
        {
            "screenshot_url": screenshot.as_uri(),
            "screenshot_path": str(screenshot),
            "source": "playwright",
            "metadata": {
                "capture_type": "viewport",
                "viewport_width": 1440,
                "viewport_height": 900,
                "selected_capture_variant": "raw_viewport",
                "viewport_obstruction": obstruction,
                "obstruction_observed_same_capture": True,
                "dismissal_attempted": False,
                "full_page_screenshot_path": str(full_page),
                "section_capture_status": "complete",
                "section_manifest": {
                    "schema_version": "rendered-page-section-manifest-v1",
                    "sections": [
                        {
                            "id": "section-01-hero",
                            "capture_path": str(section),
                        }
                    ],
                },
                "analysis_atlas_path": str(atlas),
                "analysis_atlas_status": "complete",
                "analysis_atlas_manifest": {
                    "schema_version": "visual-analysis-atlas-v1",
                    "panel_count": 2,
                },
            },
        },
        page_url="https://example.test",
    )

    assert payload is not None
    assert payload["path"] == str(screenshot)
    assert payload["selected_capture_variant"] == "raw_viewport"
    assert payload["viewport_obstruction"] == obstruction
    assert payload["obstruction_observed_same_capture"] is True
    assert payload["full_page_screenshot_path"] == str(full_page)
    assert payload["section_capture_status"] == "complete"
    assert payload["section_manifest"]["sections"][0]["capture_path"] == str(section)
    assert payload["analysis_atlas_path"] == str(atlas)
    assert payload["analysis_atlas_manifest"]["panel_count"] == 2


def test_playwright_capture_budget_is_enforced_by_worker_process():
    data, limitation = screenshot_runtime._take_screenshot_with_budget(
        "https://example.test",
        provider="playwright",
        timeout_seconds=1,
        screenshot_capture_worker=_slow_screenshot_worker,
    )

    assert limitation == "timeout"
    assert data["error_type"] == "timeout"
    assert data["screenshot_provider"] == "playwright"
