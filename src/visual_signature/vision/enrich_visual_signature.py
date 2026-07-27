"""Add local screenshot-derived evidence to a Visual Signature payload."""

from __future__ import annotations

from typing import Any

from src.visual_signature.vision.composition import analyze_composition
from src.visual_signature.vision.confidence import calculate_vision_confidence
from src.visual_signature.vision.palette_from_screenshot import extract_palette_from_screenshot
from src.visual_signature.vision.screenshot_quality import (
    resolve_screenshot_path,
    resolve_screenshot_metadata,
    screenshot_evidence_for_path,
)
from src.visual_signature.vision.section_analysis import analyze_page_sections
from src.visual_signature.vision.agreement import compare_dom_and_viewport
from src.visual_signature.vision.types import RasterImage, VisionEvidence
from src.visual_signature._internal.utils import (
    int_or_none as _int_or_none,
    normalize_capture_type as _normalize_capture_type,
)
from src.visual_signature.vision.viewport_obstruction import analyze_viewport_obstruction


def enrich_visual_signature_with_vision(
    *,
    visual_signature_payload: dict[str, Any],
    screenshot_path: str | None = None,
    screenshot_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a Visual Signature payload with additive local vision evidence.

    This function does not call multimodal models, does not influence scoring,
    and does not mutate the input payload.
    """
    payload = dict(visual_signature_payload)
    metadata = resolve_screenshot_metadata(screenshot_payload=screenshot_payload)
    resolved_path = resolve_screenshot_path(
        screenshot_path=screenshot_path,
        screenshot_payload=screenshot_payload,
        visual_signature_payload=payload,
    )
    screenshot, image = screenshot_evidence_for_path(resolved_path, screenshot_payload=metadata)
    if metadata.get("capture_type") and screenshot.available:
        screenshot.capture_type = _normalize_capture_type(metadata.get("capture_type"))
    if metadata.get("page_url") and screenshot.available:
        screenshot.page_url = str(metadata.get("page_url"))
    if metadata.get("width") and screenshot.available:
        screenshot.viewport_width = _int_or_none(metadata.get("viewport_width") or metadata.get("width"))
    if metadata.get("height") and screenshot.available:
        screenshot.viewport_height = _int_or_none(metadata.get("viewport_height") or metadata.get("height"))
    if screenshot.available and screenshot.capture_type == "unknown":
        screenshot.capture_type = "full_page" if resolved_path else "unknown"
    palette = extract_palette_from_screenshot(image)
    composition = analyze_composition(image)
    confidence = calculate_vision_confidence(
        screenshot=screenshot,
        palette=palette,
        composition=composition,
    )
    viewport_image = _viewport_image(image, screenshot)
    viewport_palette = extract_palette_from_screenshot(viewport_image)
    viewport_composition = analyze_composition(viewport_image)
    viewport_confidence = calculate_vision_confidence(
        screenshot=screenshot,
        palette=viewport_palette,
        composition=viewport_composition,
    )
    agreement = compare_dom_and_viewport(payload, composition, viewport_composition, palette, viewport_palette)
    acquisition = payload.get("acquisition") if isinstance(payload.get("acquisition"), dict) else {}
    acquisition_obstruction = acquisition.get("viewport_obstruction") if isinstance(acquisition, dict) and isinstance(acquisition.get("viewport_obstruction"), dict) else None
    payload_obstruction = metadata.get("viewport_obstruction") if isinstance(metadata.get("viewport_obstruction"), dict) else None
    selected_variant = str(metadata.get("selected_capture_variant") or "")
    payload_override = (
        payload_obstruction
        if _should_use_payload_obstruction(
            payload_obstruction,
            selected_variant=selected_variant,
            same_capture_observation=metadata.get("obstruction_observed_same_capture") is True,
        )
        else None
    )
    existing_obstruction = payload_override or acquisition_obstruction
    dom_html = "" if payload_override else (
        str(acquisition.get("rendered_html") or acquisition.get("raw_html") or "") if isinstance(acquisition, dict) else ""
    )
    viewport_obstruction = analyze_viewport_obstruction(
        dom_html=dom_html,
        viewport_image=viewport_image,
        existing_obstruction=existing_obstruction if isinstance(existing_obstruction, dict) else None,
    )
    section_analysis = analyze_page_sections(
        metadata.get("section_manifest") if isinstance(metadata.get("section_manifest"), dict) else None,
        page_url=str(metadata.get("page_url") or ""),
    )
    section_analysis["semantic_analysis_input"] = {
        "type": "labeled_section_atlas"
        if str(metadata.get("analysis_atlas_path") or "").strip()
        else "primary_screenshot",
        "path": metadata.get("analysis_atlas_path") or resolved_path,
        "status": str(metadata.get("analysis_atlas_status") or ""),
        "manifest": metadata.get("analysis_atlas_manifest")
        if isinstance(metadata.get("analysis_atlas_manifest"), dict)
        else {},
    }
    payload["vision"] = VisionEvidence(
        screenshot=screenshot,
        screenshot_palette=palette,
        composition=composition,
        vision_confidence=confidence,
        agreement=agreement,
        viewport_palette=viewport_palette,
        viewport_whitespace_ratio=viewport_composition.whitespace_ratio,
        viewport_visual_density=viewport_composition.visual_density,
        viewport_composition=viewport_composition,
        viewport_confidence=viewport_confidence,
        viewport_obstruction=viewport_obstruction.to_dict(),
        section_analysis=section_analysis,
    ).to_dict()
    return payload


def _should_use_payload_obstruction(
    payload_obstruction: dict[str, Any] | None,
    *,
    selected_variant: str,
    same_capture_observation: bool = False,
) -> bool:
    if not isinstance(payload_obstruction, dict):
        return False
    if same_capture_observation and "present" in payload_obstruction:
        return True
    if selected_variant == "clean_attempt":
        return True
    if payload_obstruction.get("present") is True:
        return True
    if payload_obstruction.get("signals"):
        return True
    if payload_obstruction.get("visual_signals"):
        return True
    if payload_obstruction.get("overlay_level_signals"):
        return True
    if payload_obstruction.get("page_level_signals"):
        return True
    return False


def _viewport_image(image: RasterImage | None, screenshot: Any) -> RasterImage | None:
    if image is None:
        return None
    if screenshot is None or not getattr(screenshot, "available", False):
        return image

    viewport_width = _int_or_none(getattr(screenshot, "viewport_width", None)) or image.width
    viewport_height = _int_or_none(getattr(screenshot, "viewport_height", None))
    capture_type = _normalize_capture_type(getattr(screenshot, "capture_type", "unknown"))
    if capture_type == "viewport" and viewport_height is None:
        viewport_height = image.height
    if viewport_height is None:
        viewport_height = min(image.height, 900)
    viewport_height = min(viewport_height, 900)
    viewport_width = max(1, min(image.width, viewport_width))
    viewport_height = max(1, min(image.height, viewport_height))
    if viewport_width == image.width and viewport_height == image.height:
        return image
    return image.crop(width=viewport_width, height=viewport_height)
