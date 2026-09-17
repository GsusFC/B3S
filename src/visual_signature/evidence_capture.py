"""Capture normalization for Visual Signature evidence packets."""

from __future__ import annotations

from typing import Any

from src.visual_signature._internal.utils import dict_or_empty as _dict
from src.visual_signature._internal.utils import float_or_none as _float_or_none

BLOCKED_QUALITIES = {"missing", "blocked", "unreadable", "blank"}
LIMITED_QUALITIES = {"poor", "partial", "low_detail"}

# Phrases the vision model uses when the screenshot shows a gate instead of the
# site: a bot challenge, a verification step or a loading interstitial.
INTERSTITIAL_MARKERS: tuple[tuple[str, str], ...] = (
    ("loading screen", "loading_screen"),
    ("security check", "security_check"),
    ("checking your browser", "checking_your_browser"),
    ("just a moment", "just_a_moment"),
    ("captcha", "captcha"),
    ("cloudflare", "cloudflare"),
    ("verify you are human", "human_verification"),
    ("are you human", "human_verification"),
    ("access denied", "access_denied"),
    ("ddos protection", "ddos_protection"),
)

# Deliberately narrow: only an explicit statement that brand content is absent.
# A false positive here silences real brand signals, while a miss only leaves
# today's behavior in place.
BRAND_ABSENCE_MARKERS: tuple[tuple[str, str], ...] = (
    ("lack of actual brand content", "lack_of_actual_brand_content"),
    ("lack of brand content", "lack_of_brand_content"),
    ("no actual brand content", "no_actual_brand_content"),
    ("no brand content", "no_brand_content"),
)


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
    content_trust, content_trust_reason = capture_content_trust(payload)
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
        "content_trust": content_trust,
        "content_trust_reason": content_trust_reason,
        "viewport": viewport(screenshot),
        "url_requested": str(payload.get("website_url") or ""),
        "url_final": str(screenshot.get("page_url") or payload.get("analyzed_url") or payload.get("website_url") or ""),
        "captured_at": str(screenshot.get("captured_at") or _dict(payload.get("acquisition")).get("acquired_at") or ""),
        "path": str(screenshot.get("path") or screenshot.get("screenshot_url") or ""),
        "obstruction": obstruction,
    }


def capture_content_trust(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Judge whether the captured pixels show the brand or a gate in front of it.

    Two independent signals, held in OR because either can miss on its own.
    The model's enumerated answer cannot be reworded, so it survives the
    rephrasing that defeats phrase matching; the phrase markers still catch
    the case where the model describes the gate accurately but answers the
    direct question wrong, and they are the only signal available for packets
    captured before the field existed. Phrase matching needs two hits, an
    interstitial phrase and an explicit absence of brand content, because
    either one alone appears in ordinary brand copy.
    """

    semantics = _dict(payload.get("semantics"))
    if str(semantics.get("status") or "") != "detected" or bool(semantics.get("fallback_used")):
        return "unknown", None

    data = _dict(semantics.get("data"))
    reasons: list[str] = []
    if str(data.get("page_content_type") or "").strip().lower() == "interstitial":
        reasons.append("page_content_type:interstitial")

    described = " ".join(
        [
            str(data.get("first_impression_summary") or ""),
            *(str(item) for item in data.get("observed_risks") or []),
            *(str(item) for item in data.get("notable_absences") or []),
        ]
    ).lower()

    interstitial = matched_markers(described, INTERSTITIAL_MARKERS)
    absence = matched_markers(described, BRAND_ABSENCE_MARKERS)
    if interstitial and absence:
        reasons.extend(interstitial + absence)

    if not reasons:
        return "trusted", None
    return "untrusted", "+".join(reasons)


def matched_markers(described: str, markers: tuple[tuple[str, str], ...]) -> list[str]:
    tokens: list[str] = []
    for phrase, token in markers:
        if phrase in described and token not in tokens:
            tokens.append(token)
    return tokens


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
