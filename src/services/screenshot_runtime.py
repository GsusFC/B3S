"""Screenshot runtime helpers for Brand3 runs."""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from src.config import BRAND3_SCREENSHOT_DIR, SCREENSHOT_PROVIDER
from src.features.visual_analyzer import VisualAnalyzer

_MAX_REMOTE_SCREENSHOT_BYTES = 20 * 1024 * 1024
_COOKIE_BANNER_DISMISS_SELECTOR = (
    "button:has-text('Aceptar'), button:has-text('Accept'), button:has-text('Rechazar'), "
    "button:has-text('Cerrar'), button:has-text('Agree'), button:has-text('Allow all'), "
    "button:has-text('Accept all'), button:has-text('Close'), button:has-text('OK'), "
    "a:has-text('Aceptar'), a:has-text('Accept'), a:has-text('Cerrar'), a:has-text('Close'), "
    "#cookie-accept, #accept-cookies, .cookie-accept, .accept-cookies"
)


def _normalized_screenshot_provider(provider: str | None = None) -> str:
    value = (provider or SCREENSHOT_PROVIDER or "firecrawl").strip().lower()
    return value if value in {"firecrawl", "playwright"} else "firecrawl"


def _take_firecrawl_screenshot(url: str) -> dict[str, object]:
    data = VisualAnalyzer().take_screenshot(url)
    data.setdefault("screenshot_provider", "firecrawl_screenshot")
    if str(data.get("screenshot_url") or "").strip():
        return _persist_remote_screenshot(data)
    return data


def _download_remote_screenshot(
    screenshot_url: str,
    *,
    timeout_seconds: int = 20,
    max_bytes: int = _MAX_REMOTE_SCREENSHOT_BYTES,
) -> tuple[bytes, str]:
    parsed = urlparse(screenshot_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("remote_screenshot_url_must_use_https")
    request = urllib.request.Request(
        screenshot_url,
        headers={"User-Agent": "B3S visual evidence capture/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        content_type = str(response.headers.get_content_type() or "").lower()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("remote_screenshot_exceeds_size_limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("remote_screenshot_exceeds_size_limit")
            chunks.append(chunk)
    return b"".join(chunks), content_type


def _persist_remote_screenshot(
    data: dict[str, object],
    *,
    download_remote_screenshot=_download_remote_screenshot,
) -> dict[str, object]:
    screenshot_url = str(data.get("screenshot_url") or "").strip()
    parsed = urlparse(screenshot_url)
    if parsed.scheme not in {"http", "https"}:
        return data

    screenshot_dir = Path(BRAND3_SCREENSHOT_DIR).resolve()
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = ""
    try:
        image_bytes, content_type = download_remote_screenshot(screenshot_url)
        if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"remote_screenshot_not_png:{content_type or 'unknown_content_type'}")
        fd, screenshot_path = tempfile.mkstemp(
            prefix="brand3-screenshot-firecrawl-",
            suffix=".png",
            dir=str(screenshot_dir),
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(image_bytes)

        from src.visual_signature.vision.screenshot_quality import load_raster_image

        image = load_raster_image(screenshot_path)
        persisted = dict(data)
        persisted["remote_screenshot_url"] = screenshot_url
        persisted["screenshot_url"] = Path(screenshot_path).as_uri()
        persisted["screenshot_path"] = screenshot_path
        metadata = dict(data.get("metadata") or {}) if isinstance(data.get("metadata"), dict) else {}
        metadata.update(
            {
                "persisted_locally": True,
                "remote_content_type": content_type,
                "file_size_bytes": len(image_bytes),
                "width": image.width,
                "height": image.height,
            }
        )
        persisted["metadata"] = metadata
        return persisted
    except Exception as exc:
        if screenshot_path:
            try:
                Path(screenshot_path).unlink(missing_ok=True)
            except OSError:
                pass
        failed = {key: value for key, value in data.items() if key != "screenshot_url"}
        failed["error"] = f"remote_screenshot_persistence_failed:{exc}"
        failed["error_type"] = "persistence_error"
        return failed


def _screenshot_has_capture(data: dict[str, object] | None) -> bool:
    return bool(isinstance(data, dict) and str(data.get("screenshot_url") or "").strip())


def _dismiss_cookie_banner_once(page) -> dict[str, object]:
    """Reuse the text-capture cookie dismissal affordances before visual capture."""
    try:
        page.locator(_COOKIE_BANNER_DISMISS_SELECTOR).first.click(timeout=500)
        try:
            page.wait_for_timeout(700)
        except Exception:
            pass
        return {"attempted": True, "success": True, "selector": _COOKIE_BANNER_DISMISS_SELECTOR}
    except Exception as exc:
        return {"attempted": True, "success": False, "error": str(exc)[:160], "selector": _COOKIE_BANNER_DISMISS_SELECTOR}


def _take_playwright_screenshot(url: str, *, timeout_ms: int = 30000) -> dict[str, object]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from src.visual_signature.capture.playwright_capture_runtime import capture_with_playwright
    except Exception as exc:
        return {
            "error": f"Playwright not available: {exc}",
            "error_type": "missing_dependency",
            "screenshot_provider": "playwright",
        }

    screenshot_dir = Path(BRAND3_SCREENSHOT_DIR).resolve()
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    fd, screenshot_path = tempfile.mkstemp(prefix="brand3-screenshot-", suffix=".png", dir=str(screenshot_dir))
    os.close(fd)
    capture_succeeded = False
    try:
        capture = capture_with_playwright(
            (urlparse(url).hostname or "brand").removeprefix("www."),
            url,
            screenshot_path,
            "viewport",
            attempt_dismiss_obstructions=True,
            navigation_timeout_ms=timeout_ms,
            network_idle_timeout_ms=min(8000, max(1000, timeout_ms // 4)),
        )
        dismissal_successful = capture.get("dismissal_successful") is True
        selected_path = str(
            capture.get("clean_attempt_screenshot_path")
            if dismissal_successful
            else capture.get("raw_screenshot_path") or screenshot_path
        )
        selected_obstruction = (
            capture.get("after_obstruction")
            if dismissal_successful
            else capture.get("before_obstruction")
        )
        selected_variant = "clean_attempt" if dismissal_successful else "raw_viewport"
        metadata = {
            "title": str(capture.get("title") or ""),
            "capture_type": "viewport",
            "page_url": str(capture.get("page_url") or url),
            "viewport_width": capture.get("viewport_width"),
            "viewport_height": capture.get("viewport_height"),
            "selected_capture_variant": selected_variant,
            "viewport_obstruction": selected_obstruction if isinstance(selected_obstruction, dict) else {},
            "obstruction_observed_same_capture": True,
            "dismissal_attempted": capture.get("dismissal_attempted") is True,
            "dismissal_successful": dismissal_successful,
            "dismissal_method": capture.get("dismissal_method"),
            "dismissal_eligibility": capture.get("dismissal_eligibility"),
            "dismissal_block_reason": capture.get("dismissal_block_reason"),
            "evidence_integrity_notes": list(capture.get("evidence_integrity_notes") or []),
            "raw_screenshot_path": str(capture.get("raw_screenshot_path") or screenshot_path),
            "full_page_screenshot_path": capture.get("full_page_screenshot_path"),
            "section_capture_status": capture.get("section_capture_status"),
            "section_manifest": capture.get("section_manifest")
            if isinstance(capture.get("section_manifest"), dict)
            else {},
            "section_captures": list(capture.get("section_captures") or []),
            "lazy_content_hydration": capture.get("lazy_content_hydration")
            if isinstance(capture.get("lazy_content_hydration"), dict)
            else {},
            "post_hydration_obstruction_check": capture.get(
                "post_hydration_obstruction_check"
            )
            if isinstance(capture.get("post_hydration_obstruction_check"), dict)
            else {},
            "analysis_atlas_path": capture.get("analysis_atlas_path"),
            "analysis_atlas_status": capture.get("analysis_atlas_status"),
            "analysis_atlas_manifest": capture.get("analysis_atlas_manifest")
            if isinstance(capture.get("analysis_atlas_manifest"), dict)
            else {},
            "structured_capture_errors": list(capture.get("structured_capture_errors") or []),
            "cookie_banner_dismissal": {
                "attempted": capture.get("dismissal_attempted") is True,
                "success": dismissal_successful,
                "method": capture.get("dismissal_method"),
            },
        }
        capture_succeeded = True
        return {
            "screenshot_url": Path(selected_path).as_uri(),
            "screenshot_path": selected_path,
            "metadata": metadata,
            "screenshot_provider": "playwright",
        }
    except PlaywrightTimeoutError as exc:
        return {"error": str(exc), "error_type": "timeout", "screenshot_provider": "playwright"}
    except Exception as exc:
        return {"error": str(exc), "error_type": "browser_error", "screenshot_provider": "playwright"}
    finally:
        leftovers = [Path(screenshot_path)]
        if not capture_succeeded:
            leftovers.append(Path(screenshot_path).with_name(f"{Path(screenshot_path).stem}.clean-attempt.png"))
        for leftover in leftovers:
            try:
                if leftover.exists() and (not capture_succeeded or leftover.stat().st_size == 0):
                    leftover.unlink()
            except OSError:
                pass


def _take_playwright_screenshot_with_firecrawl_fallback(
    url: str,
    *,
    take_playwright_screenshot=_take_playwright_screenshot,
    take_firecrawl_screenshot=_take_firecrawl_screenshot,
    screenshot_has_capture=_screenshot_has_capture,
) -> dict[str, object]:
    primary = take_playwright_screenshot(url)
    if screenshot_has_capture(primary):
        return primary

    fallback_reason = str(primary.get("error_type") or primary.get("error") or "missing_screenshot_url")
    try:
        fallback = take_firecrawl_screenshot(url)
    except Exception as exc:
        primary["fallback_attempted"] = True
        primary["fallback_provider"] = "firecrawl_screenshot"
        primary["fallback_error"] = str(exc)
        return primary

    fallback["fallback_from_provider"] = "playwright"
    fallback["fallback_reason"] = fallback_reason
    if primary.get("error"):
        fallback.setdefault("primary_error", primary.get("error"))
        fallback.setdefault("primary_error_type", primary.get("error_type"))
    return fallback


def _screenshot_capture_worker(
    output_queue,
    url: str,
    provider: str,
    *,
    take_playwright_screenshot_with_firecrawl_fallback=_take_playwright_screenshot_with_firecrawl_fallback,
    take_playwright_screenshot=_take_playwright_screenshot,
    take_firecrawl_screenshot=_take_firecrawl_screenshot,
    screenshot_has_capture=_screenshot_has_capture,
) -> None:
    try:
        if provider == "playwright":
            output_queue.put(
                (
                    "ok",
                    take_playwright_screenshot_with_firecrawl_fallback(
                        url,
                        take_playwright_screenshot=take_playwright_screenshot,
                        take_firecrawl_screenshot=take_firecrawl_screenshot,
                        screenshot_has_capture=screenshot_has_capture,
                    ),
                )
            )
            return
        output_queue.put(("ok", take_firecrawl_screenshot(url)))
    except Exception as exc:
        output_queue.put(("error", str(exc)))


def _take_screenshot_with_budget(
    url: str,
    *,
    timeout_seconds: int = 60,
    provider: str | None = None,
    normalized_screenshot_provider=_normalized_screenshot_provider,
    take_playwright_screenshot=_take_playwright_screenshot,
    take_playwright_screenshot_with_firecrawl_fallback=_take_playwright_screenshot_with_firecrawl_fallback,
    take_firecrawl_screenshot=_take_firecrawl_screenshot,
    screenshot_capture_worker=_screenshot_capture_worker,
) -> tuple[dict[str, object], str | None]:
    provider_name = normalized_screenshot_provider(provider)
    if timeout_seconds <= 0:
        if provider_name == "playwright":
            return (
                take_playwright_screenshot_with_firecrawl_fallback(
                    url,
                    take_playwright_screenshot=take_playwright_screenshot,
                    take_firecrawl_screenshot=take_firecrawl_screenshot,
                    screenshot_has_capture=_screenshot_has_capture,
                ),
                None,
            )
        try:
            return take_firecrawl_screenshot(url), None
        except Exception as exc:
            return {"error": str(exc), "screenshot_provider": "firecrawl_screenshot"}, "error"

    import sys

    method = "spawn" if sys.platform == "darwin" else ("fork" if "fork" in mp.get_all_start_methods() else "spawn")
    ctx = mp.get_context(method)
    output_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=screenshot_capture_worker,
        args=(output_queue, url, provider_name),
        kwargs={
            "take_playwright_screenshot_with_firecrawl_fallback": take_playwright_screenshot_with_firecrawl_fallback,
            "take_playwright_screenshot": take_playwright_screenshot,
            "take_firecrawl_screenshot": take_firecrawl_screenshot,
            "screenshot_has_capture": _screenshot_has_capture,
        },
    )
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)
        return {
            "error": f"visual_screenshot_timeout_after_{timeout_seconds}s",
            "error_type": "timeout",
            "screenshot_provider": provider_name if provider_name == "playwright" else "firecrawl_screenshot",
        }, "timeout"

    try:
        status, payload = output_queue.get_nowait()
    except queue.Empty:
        return {
            "error": "visual_screenshot_no_result",
            "error_type": "unknown",
            "screenshot_provider": provider_name if provider_name == "playwright" else "firecrawl_screenshot",
        }, "error"

    if status == "ok" and isinstance(payload, dict):
        return payload, None
    return {
        "error": str(payload or "visual_screenshot_error"),
        "error_type": "browser_error" if provider_name == "playwright" else "capture_error",
        "screenshot_provider": provider_name if provider_name == "playwright" else "firecrawl_screenshot",
    }, "error"
