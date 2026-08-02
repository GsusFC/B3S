"""Playwright-backed screenshot capture runtime for Visual Signature."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.visual_signature._internal.utils import float_or_none as _float_or_none
from src.visual_signature._internal.utils import normalize_capture_type as _normalize_capture_type
from src.visual_signature._internal.playwright_capture_dismissal_rules import dismissal_skip_note as _dismissal_skip_note
from src.visual_signature.capture.screenshot_capture_models import CaptureResult
from src.visual_signature.capture.page_sections import (
    RUNTIME_MAX_PAGE_SEGMENTS,
    capture_structured_page_evidence,
)
from src.visual_signature._internal.playwright_capture_helpers import DISMISSAL_TARGET_SELECTOR
from src.visual_signature._internal.playwright_capture_helpers import _attempt_obstruction_dismissal
from src.visual_signature._internal.playwright_capture_helpers import _attempt_obstruction_dismissal_with_discovery
from src.visual_signature._internal.playwright_capture_helpers import _dismissal_successful
from src.visual_signature._internal.playwright_capture_helpers import _discover_dismissal_targets
from src.visual_signature._internal.playwright_capture_helpers import _prepare_perceptual_state_machine
from src.visual_signature._internal.playwright_capture_helpers_capture_runtime import (
    _coerce_dict_or_none,
    _coerce_transition_list,
    _derived_capture_path,
    _snapshot_for_path,
    _visible_obstruction_dom_snapshot,
)
from src.visual_signature.perception import PerceptualStateMachine
from src.visual_signature.vision.viewport_obstruction import analyze_viewport_obstruction


COOKIE_DISMISS_PHRASES = (
    ("accept all", "accept_all"),
    ("allow all", "allow_all"),
    ("reject all", "reject_all"),
    ("decline all", "decline_all"),
    ("i agree", "agree"),
    ("agree", "agree"),
    ("accept", "accept"),
    ("reject", "reject"),
    ("decline", "decline"),
    ("continue", "continue"),
    ("close", "close"),
    ("dismiss", "dismiss"),
    ("got it", "got_it"),
    ("ok", "ok"),
    ("aceptar todas", "accept_all"),
    ("aceptar todo", "accept_all"),
    ("aceptar", "accept"),
    ("permitir todas", "allow_all"),
    ("rechazar todas", "reject_all"),
    ("rechazar todo", "reject_all"),
    ("rechazar", "reject"),
    ("denegar", "decline"),
    ("de acuerdo", "agree"),
    ("continuar", "continue"),
    ("cerrar", "close"),
    ("entendido", "got_it"),
    ("vale", "ok"),
    ("x", "close"),
    ("×", "close"),
    ("✕", "close"),
    ("✖", "close"),
)
NEWSLETTER_DISMISS_PHRASES = (
    ("close", "close"),
    ("dismiss", "dismiss"),
    ("x", "close"),
    ("×", "close"),
    ("✕", "close"),
    ("✖", "close"),
)
COMMON_DISMISS_IGNORED_TERMS = (
    "manage choices",
    "manage preference",
    "manage preferences",
    "preferences",
    "settings",
    "customize",
    "configurar",
    "configuración",
    "configuracion",
    "preferencias",
    "ajustes",
    "personalizar",
    "subscribe",
    "sign up",
    "signup",
    "join",
    "register",
    "learn more",
)
DISMISSAL_TARGET_SELECTOR = "button, [role='button'], input[type='button'], input[type='submit'], a, [aria-label], [title], [tabindex='0']"


def capture_with_playwright(
    brand_name: str,
    website_url: str,
    screenshot_path: str,
    capture_type: str,
    *,
    attempt_dismiss_obstructions: bool = False,
    navigation_timeout_ms: int = 30000,
    network_idle_timeout_ms: int = 8000,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(
            "playwright is not installed. Run: ./.venv/bin/python -m pip install playwright && ./.venv/bin/python -m playwright install chromium"
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        normalized_capture_type = "viewport" if str(capture_type).strip().lower() == "viewport" else "full_page"
        viewport_width, viewport_height = (1440, 900) if normalized_capture_type == "viewport" else (1440, 1200)
        context = browser.new_context(viewport={"width": viewport_width, "height": viewport_height})
        page = context.new_page()
        page.goto(
            website_url,
            wait_until="domcontentloaded",
            timeout=max(1000, int(navigation_timeout_ms)),
        )
        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=max(500, int(network_idle_timeout_ms)),
            )
        except PlaywrightTimeoutError:
            pass

        raw_path = Path(screenshot_path)
        raw_dom_html = _visible_obstruction_dom_snapshot(page)
        page.screenshot(
            path=str(raw_path),
            full_page=normalized_capture_type != "viewport",
            timeout=10000,
        )
        raw_snapshot = _snapshot_for_path(raw_path, dom_html=raw_dom_html)
        width = page.viewport_size["width"] if page.viewport_size else viewport_width
        height = page.viewport_size["height"] if page.viewport_size else viewport_height

        result: dict[str, Any] = {
            "source": "playwright",
            "capture_type": normalized_capture_type,
            "capture_variant": "raw_viewport" if normalized_capture_type == "viewport" else normalized_capture_type,
            "clean_attempt_capture_variant": None,
            "brand_name": brand_name,
            "website_url": website_url,
            "raw_screenshot_path": str(raw_path),
            "width": width,
            "height": height,
            "viewport_width": width,
            "viewport_height": height,
            "page_url": website_url,
            "title": page.title(),
            "before_obstruction": raw_snapshot["obstruction"],
            "raw_viewport_metrics": raw_snapshot["metrics"],
            "evidence_integrity_notes": [
                "raw_viewport_preserved_as_primary_evidence",
            ],
            "dismissal_attempted": False,
            "dismissal_successful": False,
            "dismissal_eligibility": None,
            "dismissal_block_reason": None,
            "candidate_click_targets": [],
            "rejected_click_targets": [],
        }

        if attempt_dismiss_obstructions and normalized_capture_type == "viewport":
            perceptual_context = _prepare_perceptual_state_machine(
                page=page,
                raw_snapshot=raw_snapshot,
                raw_artifact_ref=str(raw_path),
                attempt_dismiss_obstructions=True,
            )
            if perceptual_context is not None:
                machine = perceptual_context["machine"]
                discovery = perceptual_context["discovery"] or {"eligible": False, "candidate_click_targets": [], "rejected_click_targets": [], "block_reason": None, "dismissal_eligibility": None}
                eligibility = perceptual_context["eligibility"]
                result["perceptual_state_data"] = machine.to_dict()
                result["perceptual_state"] = machine.current_state
                result["perceptual_transitions"] = machine.to_dict().get("transitions") or []
                result["mutation_audit"] = None

                if discovery.get("eligible") and discovery.get("selected_candidate") is not None:
                    dismissal = _attempt_obstruction_dismissal_with_discovery(page, raw_snapshot["obstruction"], discovery)
                else:
                    dismissal = {
                        "attempted": False,
                        "successful": False,
                        "method": None,
                        "clicked_text": None,
                        "note": discovery.get("block_reason") or _dismissal_skip_note(raw_snapshot["obstruction"]),
                        "dismissal_eligibility": discovery.get("dismissal_eligibility"),
                        "dismissal_block_reason": discovery.get("block_reason"),
                        "candidate_click_targets": discovery.get("candidate_click_targets") or [],
                        "rejected_click_targets": discovery.get("rejected_click_targets") or [],
                    }

                result["dismissal_attempted"] = bool(dismissal.get("attempted"))
                result["dismissal_successful"] = bool(dismissal.get("successful"))
                result["dismissal_method"] = dismissal.get("method")
                result["clicked_text"] = dismissal.get("clicked_text")
                result["dismissal_eligibility"] = dismissal.get("dismissal_eligibility") or eligibility
                result["dismissal_block_reason"] = dismissal.get("dismissal_block_reason") or dismissal.get("note")
                result["candidate_click_targets"] = dismissal.get("candidate_click_targets") or discovery.get("candidate_click_targets") or []
                result["rejected_click_targets"] = dismissal.get("rejected_click_targets") or discovery.get("rejected_click_targets") or []

                if dismissal.get("attempted") and dismissal.get("successful"):
                    clean_path = _derived_capture_path(raw_path, "clean_attempt")
                    clean_dom_html = _visible_obstruction_dom_snapshot(page)
                    page.screenshot(
                        path=str(clean_path),
                        full_page=False,
                        timeout=10000,
                    )
                    clean_snapshot = _snapshot_for_path(clean_path, dom_html=clean_dom_html)
                    result["clean_attempt_screenshot_path"] = str(clean_path)
                    result["secondary_screenshot_path"] = str(clean_path)
                    result["secondary_capture_type"] = "viewport"
                    result["after_obstruction"] = clean_snapshot["obstruction"]
                    result["clean_attempt_metrics"] = clean_snapshot["metrics"]
                    mutation = machine.classify_mutation(
                        before_state=machine.current_state,
                        attempted=True,
                        successful=_dismissal_successful(
                            raw_snapshot["obstruction"],
                            clean_snapshot["obstruction"],
                        ),
                        reversible=True,
                        evidence_preserved=True,
                        mutation_type="obstruction_dismissal",
                        trigger="safe_mutation_attempted",
                        before_artifact_ref=str(raw_path),
                        after_artifact_ref=str(clean_path),
                        evidence_refs=[str(raw_path), str(clean_path)],
                        confidence=_float_or_none(raw_snapshot["obstruction"].get("confidence")) or 0.5,
                        notes=[
                            "raw_viewport_preserved_as_primary_evidence",
                            "clean_attempt_is_supplemental_only; raw_viewport_remains_primary",
                        ],
                        risk_level="low",
                    )
                    result["perceptual_state_data"] = machine.to_dict()
                    result["perceptual_state"] = machine.current_state
                    result["perceptual_transitions"] = machine.to_dict().get("transitions") or []
                    result["mutation_audit"] = mutation.mutation_audit.to_dict()
                    result["dismissal_successful"] = mutation.state == "MINIMALLY_MUTATED_STATE"
                    result["evidence_integrity_notes"].append(
                        "clean_attempt_is_supplemental_only; raw_viewport_remains_primary"
                    )
                    if result["dismissal_successful"]:
                        result["evidence_integrity_notes"].append("dismissal_reduced_viewport_obstruction")
                    else:
                        result["evidence_integrity_notes"].append("dismissal_did_not_materially_reduce_obstruction")
                else:
                    result["evidence_integrity_notes"].append(dismissal.get("note") or "dismissal_not_attempted")
                    result["after_obstruction"] = raw_snapshot["obstruction"]
                    result["clean_attempt_metrics"] = raw_snapshot["metrics"]
                    result["perceptual_state_data"] = machine.to_dict()
                    result["perceptual_state"] = machine.current_state
                    result["perceptual_transitions"] = machine.to_dict().get("transitions") or []
                    result["mutation_audit"] = None

        selected_variant = "clean_attempt" if result.get("dismissal_successful") is True else result["capture_variant"]
        selected_viewport_path = (
            result.get("clean_attempt_screenshot_path")
            if result.get("dismissal_successful") is True
            else result.get("raw_screenshot_path")
        )
        try:
            result.update(
                capture_structured_page_evidence(
                    page,
                    screenshot_path=raw_path,
                    page_url=website_url,
                    capture_variant=selected_variant,
                    max_page_segments=RUNTIME_MAX_PAGE_SEGMENTS,
                    viewport_screenshot_path=str(selected_viewport_path or raw_path),
                    post_hydration_hook=_dismiss_post_hydration_obstruction
                    if attempt_dismiss_obstructions
                    else None,
                )
            )
            result["evidence_integrity_notes"].append(
                "structural_captures_derived_from_rendered_dom_after_bounded_lazy_load_hydration"
            )
        except Exception as exc:
            result["section_capture_status"] = "failed"
            result["structured_capture_errors"] = [
                f"structural_capture_failed:{type(exc).__name__}:{exc}"
            ]
            result["evidence_integrity_notes"].append(
                "structural_capture_failed; primary_viewport_evidence_remains_available"
            )

        context.close()
        browser.close()
        return result


def _dismiss_post_hydration_obstruction(page: Any) -> dict[str, Any]:
    """Safely handle overlays that appear only after the lazy-load scroll."""

    before = analyze_viewport_obstruction(
        dom_html=_visible_obstruction_dom_snapshot(page),
    ).to_dict()
    if before.get("present") is not True:
        return {
            "checked": True,
            "attempted": False,
            "successful": False,
            "before_obstruction": before,
            "after_obstruction": before,
            "reason": "no_post_hydration_obstruction_detected",
        }

    discovery = _discover_dismissal_targets(page, before)
    if not discovery.get("eligible") or discovery.get("selected_candidate") is None:
        return {
            "checked": True,
            "attempted": False,
            "successful": False,
            "before_obstruction": before,
            "after_obstruction": before,
            "reason": discovery.get("block_reason")
            or "post_hydration_obstruction_not_safe_to_dismiss",
            "candidate_click_targets": discovery.get("candidate_click_targets") or [],
            "rejected_click_targets": discovery.get("rejected_click_targets") or [],
        }

    dismissal = _attempt_obstruction_dismissal_with_discovery(
        page,
        before,
        discovery,
    )
    after = analyze_viewport_obstruction(
        dom_html=_visible_obstruction_dom_snapshot(page),
    ).to_dict()
    successful = bool(
        dismissal.get("attempted")
        and dismissal.get("successful")
        and _dismissal_successful(before, after)
    )
    return {
        "checked": True,
        "attempted": dismissal.get("attempted") is True,
        "successful": successful,
        "method": dismissal.get("method"),
        "clicked_text": dismissal.get("clicked_text"),
        "before_obstruction": before,
        "after_obstruction": after,
        "reason": (
            "post_hydration_obstruction_dismissed"
            if successful
            else dismissal.get("note")
            or "post_hydration_dismissal_did_not_reduce_obstruction"
        ),
        "candidate_click_targets": dismissal.get("candidate_click_targets")
        or discovery.get("candidate_click_targets")
        or [],
        "rejected_click_targets": dismissal.get("rejected_click_targets")
        or discovery.get("rejected_click_targets")
        or [],
    }
