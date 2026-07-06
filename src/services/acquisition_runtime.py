"""Acquisition runtime facade for Brand3 runs."""

from __future__ import annotations

from src.services.social_runtime import _collect_social_with_budget, _social_collect_worker
from src.services.screenshot_runtime import (
    _normalized_screenshot_provider,
    _screenshot_capture_worker,
    _screenshot_has_capture,
    _take_firecrawl_screenshot,
    _take_playwright_screenshot,
    _take_playwright_screenshot_with_firecrawl_fallback,
    _take_screenshot_with_budget,
)


def _classify_screenshot_error(error_message: str) -> str:
    normalized = (error_message or "").lower()
    if "timeout" in normalized or "timed out" in normalized:
        return "timeout"
    if "payment required" in normalized or "insufficient credit" in normalized:
        return "payment_required"
    if "api_key" in normalized or "api key" in normalized or "not set" in normalized:
        return "missing_api_key"
    if "no screenshot url" in normalized:
        return "missing_screenshot_url"
    return "browser_error"


def _screenshot_capture_diagnostic(
    *,
    attempted: bool,
    screenshot_data: dict[str, object] | None = None,
    limitation: str | None = None,
    skipped_reason: str | None = None,
) -> dict[str, object]:
    if not attempted:
        return {
            "attempted": False,
            "success": False,
            "status": "skipped",
            "reason": skipped_reason or "not_attempted",
        }

    data = screenshot_data or {}
    screenshot_url = str(data.get("screenshot_url") or "")
    source = str(data.get("screenshot_provider") or "firecrawl_screenshot")
    if screenshot_url:
        payload: dict[str, object] = {
            "attempted": True,
            "success": True,
            "status": "captured",
            "source": source,
            "error_type": None,
            "error_message": None,
            "screenshot_url": screenshot_url,
        }
        screenshot_path = str(data.get("screenshot_path") or "").strip()
        if screenshot_path:
            payload["screenshot_path"] = screenshot_path
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            payload["metadata"] = metadata
        return payload

    error_message = str(data.get("error") or limitation or "screenshot_capture_failed")
    error_type = str(data.get("error_type") or limitation or _classify_screenshot_error(error_message))
    return {
        "attempted": True,
        "success": False,
        "status": "error" if error_type != "timeout" else "timeout",
        "source": source,
        "error_type": error_type,
        "error_message": error_message[:300],
    }
