"""Screenshot runtime helpers for Brand3 runs."""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import tempfile
from pathlib import Path

from src.config import BRAND3_SCREENSHOT_DIR, SCREENSHOT_PROVIDER
from src.features.visual_analyzer import VisualAnalyzer

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
    return data


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
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            "error": f"Playwright not available: {exc}",
            "error_type": "missing_dependency",
            "screenshot_provider": "playwright",
        }

    screenshot_dir = Path(BRAND3_SCREENSHOT_DIR)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    fd, screenshot_path = tempfile.mkstemp(prefix="brand3-screenshot-", suffix=".png", dir=str(screenshot_dir))
    os.close(fd)
    browser = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1200},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            cookie_dismissal = _dismiss_cookie_banner_once(page)
            page.screenshot(path=screenshot_path, full_page=False, timeout=60000, animations="disabled")
            title = page.title()
            browser.close()
            browser = None
        return {
            "screenshot_url": Path(screenshot_path).as_uri(),
            "screenshot_path": screenshot_path,
            "metadata": {"title": title, "cookie_banner_dismissal": cookie_dismissal},
            "screenshot_provider": "playwright",
        }
    except PlaywrightTimeoutError as exc:
        return {"error": str(exc), "error_type": "timeout", "screenshot_provider": "playwright"}
    except Exception as exc:
        return {"error": str(exc), "error_type": "browser_error", "screenshot_provider": "playwright"}
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            leftover = Path(screenshot_path)
            if leftover.exists() and leftover.stat().st_size == 0:
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
    timeout_seconds: int = 45,
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
