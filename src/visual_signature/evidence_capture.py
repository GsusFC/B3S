"""Capture normalization for Visual Signature evidence packets."""

from __future__ import annotations

from typing import Any

from src.visual_signature._internal.utils import dict_or_empty as _dict
from src.visual_signature._internal.utils import float_or_none as _float_or_none

BLOCKED_QUALITIES = {"missing", "blocked", "unreadable", "blank"}
LIMITED_QUALITIES = {"poor", "partial", "low_detail"}


def screenshot_payload(payload: dict[str, Any], screenshot_payload: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(screenshot_payload, dict):
        return dict(screenshot_payload)
    vision = _dict(payload.get("vision"))
    screenshot = _dict(vision.get("screenshot"))
    if screenshot:
        return screenshot
    capture = _dict(payload.get("capture"))
    return capture


def capture_contract(
    payload: dict[str, Any],
    *,
    screenshot: dict[str, Any],
    obstruction: dict[str, Any],
) -> dict[str, Any]:
    available = bool(
        screenshot.get("available")
        or screenshot.get("path")
        or screenshot.get("screenshot_url")
        or _dict(payload.get("assets")).get("screenshot_available")
    )
    quality = str(screenshot.get("quality") or ("usable" if available else "missing"))
    variant = capture_variant(screenshot)
    structured_errors = screenshot.get("structured_capture_errors")
    if not isinstance(structured_errors, list):
        structured_errors = []
    first_fold_evaluable = first_fold_evaluable_for_capture(obstruction, available=available, quality=quality)
    status = capture_status(
        available=available,
        quality=quality,
        obstruction=obstruction,
        first_fold_evaluable=first_fold_evaluable,
    )
    return {
        "status": status,
        "available": available,
        "quality": quality,
        "capture_variant": variant,
        "capture_recovery": str(screenshot.get("capture_recovery") or "") or None,
        "section_capture_status": str(screenshot.get("section_capture_status") or "") or None,
        "structured_capture_errors": [str(item) for item in structured_errors[:8]],
        "first_fold_evaluable": first_fold_evaluable,
        "viewport": viewport(screenshot),
        "url_requested": str(payload.get("website_url") or ""),
        "url_final": str(screenshot.get("page_url") or payload.get("analyzed_url") or payload.get("website_url") or ""),
        "captured_at": str(screenshot.get("captured_at") or _dict(payload.get("acquisition")).get("acquired_at") or ""),
        "path": str(screenshot.get("path") or screenshot.get("screenshot_url") or ""),
        "obstruction": obstruction,
    }


def capture_variant(screenshot: dict[str, Any]) -> str:
    selected = str(screenshot.get("selected_capture_variant") or screenshot.get("capture_variant") or screenshot.get("capture_type") or "")
    if selected in {"viewport", "full_page", "clean_attempt", "blocked"}:
        return selected
    if str(screenshot.get("quality") or "") == "blocked":
        return "blocked"
    return "unknown"


def viewport(screenshot: dict[str, Any]) -> dict[str, int | None]:
    viewport_payload = _dict(screenshot.get("viewport"))
    return {
        "width": int_or_none(screenshot.get("width") or viewport_payload.get("width")),
        "height": int_or_none(screenshot.get("height") or viewport_payload.get("height")),
    }


def capture_obstruction(payload: dict[str, Any]) -> dict[str, Any]:
    vision = _dict(payload.get("vision"))
    acquisition = _dict(payload.get("acquisition"))
    raw = _dict(vision.get("viewport_obstruction")) or _dict(acquisition.get("viewport_obstruction"))
    if not raw:
        return {"present": False, "type": "none", "severity": "none", "first_impression_valid": True}
    return {
        "present": bool(raw.get("present")),
        "type": str(raw.get("type") or "unknown"),
        "severity": str(raw.get("severity") or "unknown"),
        "coverage_ratio": _float_or_none(raw.get("coverage_ratio"), digits=3),
        "first_impression_valid": bool(raw.get("first_impression_valid", True)),
        "confidence": _float_or_none(raw.get("confidence"), digits=3),
        "signals": [str(item) for item in raw.get("signals") or []][:12],
    }


def first_fold_evaluable_for_capture(obstruction: dict[str, Any], *, available: bool, quality: str) -> bool:
    if not available or quality in BLOCKED_QUALITIES or quality in LIMITED_QUALITIES:
        return False
    if obstruction.get("present") and obstruction.get("first_impression_valid") is False:
        return False
    if obstruction.get("severity") == "blocking":
        return False
    return True


def capture_status(
    *,
    available: bool,
    quality: str,
    obstruction: dict[str, Any],
    first_fold_evaluable: bool,
) -> str:
    if not available:
        return "missing"
    if obstruction.get("present") and (obstruction.get("severity") == "blocking" or not first_fold_evaluable):
        return "blocked"
    if quality in BLOCKED_QUALITIES:
        return "blocked"
    if quality in LIMITED_QUALITIES or not first_fold_evaluable:
        return "limited"
    return "usable"


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
