"""Visual Signature extraction pipeline.

The module returns structured evidence about a brand's observable visual
behavior. It is not a scoring dimension and does not change rubric weights.
Firecrawl is an acquisition layer; Brand3 owns normalization, taxonomy,
interpretation, and extraction-confidence logic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.visual_signature.types import (
    VisualSignature,
    VisualSignatureInput,
)
from src.visual_signature.vision.multimodal_analyzer import analyze_visual_semantics, fallback_semantics
from src.visual_signature.vision.screenshot_quality import resolve_screenshot_path

logger = logging.getLogger(__name__)


def extract_visual_signature(
    *,
    brand_name: str,
    website_url: str,
    web_data: Any | None = None,
    content_web: Any | None = None,
    screenshot_payload: dict[str, Any] | None = None,
    adapter: Any | None = None,
) -> dict[str, Any]:
    """Extract structured visual behavior signals as a JSON-serializable dict.

    Existing Brand3 `content_web`/`web_data` is preferred to avoid duplicate
    Firecrawl calls during the main analysis pipeline. The adapter is used only
    when no existing web payload is provided.
    """
    from src.visual_signature.adapters.firecrawl_adapter import acquisition_from_web_data
    from src.visual_signature.normalizers.assets import normalize_asset_signals
    from src.visual_signature.normalizers.colors import normalize_colors
    from src.visual_signature.normalizers.components import normalize_component_signals
    from src.visual_signature.normalizers.consistency import normalize_consistency_signals
    from src.visual_signature.normalizers.layout import normalize_layout_signals
    from src.visual_signature.normalizers.logo import normalize_logo_signals
    from src.visual_signature.normalizers.typography import normalize_typography
    from src.visual_signature.scoring.extraction_confidence import calculate_extraction_confidence
    from src.visual_signature.vision.viewport_obstruction import analyze_viewport_obstruction

    input_data = VisualSignatureInput(brand_name=brand_name, website_url=website_url)
    _validate_input(input_data)
    source_web = content_web or web_data
    if source_web is not None:
        acquisition = acquisition_from_web_data(
            source_web,
            adapter="existing_web_data",
            screenshot_payload=screenshot_payload,
        )
        if not acquisition.requested_url:
            acquisition.requested_url = website_url
        if not acquisition.final_url:
            acquisition.final_url = website_url
    else:
        from src.visual_signature.adapters.firecrawl_adapter import FirecrawlVisualSignatureAdapter

        acquisition_adapter = adapter or FirecrawlVisualSignatureAdapter()
        acquisition = acquisition_adapter.acquire(input_data)

    colors = normalize_colors(acquisition)
    typography = normalize_typography(acquisition)
    logo = normalize_logo_signals(acquisition, brand_name)
    layout = normalize_layout_signals(acquisition)
    components = normalize_component_signals(acquisition)
    assets = normalize_asset_signals(acquisition)
    consistency = normalize_consistency_signals(
        colors=colors,
        typography=typography,
        components=components,
        assets=assets,
    )
    extraction_confidence = calculate_extraction_confidence(
        acquisition=acquisition,
        colors=colors,
        typography=typography,
        logo=logo,
        layout=layout,
        components=components,
        assets=assets,
        consistency=consistency,
    )
    screenshot_path = resolve_screenshot_path(screenshot_payload=screenshot_payload)
    analysis_atlas_path = str(
        (screenshot_payload or {}).get("analysis_atlas_path") or ""
    ).strip()
    screenshot_for_semantics = next(
        (
            candidate
            for candidate in (analysis_atlas_path, screenshot_path)
            if candidate and Path(candidate).exists()
        ),
        None,
    )
    try:
        semantics = analyze_visual_semantics(
            screenshot_path=screenshot_for_semantics,
            brand_name=brand_name,
        )
    except Exception as exc:
        logger.warning("visual_semantics failed (brand=%s): %s", brand_name, exc, exc_info=True)
        semantics = fallback_semantics("vision_analysis_exception")

    viewport_obstruction = _viewport_obstruction_for_selected_capture(
        acquisition=acquisition,
        screenshot_payload=screenshot_payload,
    )
    signature = VisualSignature(
        brand_name=brand_name,
        website_url=website_url,
        analyzed_url=acquisition.final_url or acquisition.requested_url or website_url,
        interpretation_status=_interpretation_status(acquisition),
        acquisition={
            "adapter": acquisition.adapter,
            "status_code": acquisition.status_code,
            "acquired_at": acquisition.acquired_at,
            "warnings": acquisition.warnings,
            "errors": acquisition.errors,
            "viewport_obstruction": viewport_obstruction,
        },
        colors=colors,
        typography=typography,
        logo=logo,
        layout=layout,
        components=components,
        assets=assets,
        consistency=consistency,
        extraction_confidence=extraction_confidence,
        semantics=semantics,
    )
    return signature.to_dict()


def _interpretation_status(acquisition: Any) -> str:
    if acquisition.errors:
        return "not_interpretable"
    return "interpretable"


def _viewport_obstruction_for_selected_capture(
    *,
    acquisition: Any,
    screenshot_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    from src.visual_signature.vision.viewport_obstruction import analyze_viewport_obstruction

    payload_obstruction = (
        screenshot_payload.get("viewport_obstruction")
        if isinstance(screenshot_payload, dict) and isinstance(screenshot_payload.get("viewport_obstruction"), dict)
        else None
    )
    selected_variant = str((screenshot_payload or {}).get("selected_capture_variant") or "")
    same_capture_observation = (screenshot_payload or {}).get("obstruction_observed_same_capture") is True
    if payload_obstruction and (selected_variant == "clean_attempt" or same_capture_observation):
        return analyze_viewport_obstruction(
            dom_html="",
            existing_obstruction=payload_obstruction,
        ).to_dict()
    return analyze_viewport_obstruction(
        dom_html="\n".join([acquisition.rendered_html or "", acquisition.raw_html or ""]),
        existing_obstruction=payload_obstruction if isinstance(payload_obstruction, dict) else None,
    ).to_dict()


def _validate_input(input_data: VisualSignatureInput) -> None:
    if not (input_data.brand_name or "").strip():
        raise ValueError("brand_name is required")
    if not (input_data.website_url or "").strip():
        raise ValueError("website_url is required")
    parsed = urlparse(input_data.website_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("website_url must be a valid http(s) URL")
