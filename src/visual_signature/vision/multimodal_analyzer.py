"""Defensive Gemini Vision semantics for Visual Signature."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from src.config import (
    BRAND3_LLM_API_KEY,
    BRAND3_VISUAL_SIGNATURE_MODEL,
    BRAND3_VISUAL_SIGNATURE_SKIP_MULTIMODAL,
    LLM_BASE_URL,
)
from src.visual_signature._internal.multimodal_client import (
    build_cache_key as _build_cache_key,
    build_multimodal_payload as _build_multimodal_payload,
    effective_timeout as _multimodal_effective_timeout,
    mime_type_for_path as _mime_type_for_path,
    run_multimodal_http_call as _run_multimodal_http_call,
)
from src.visual_signature._internal.multimodal_normalizer import normalize_semantics_data
from src.visual_signature.versions import MULTIMODAL_PROMPT_VERSION as PROMPT_VERSION

logger = logging.getLogger(__name__)

SYSTEM_PREAMBLE = (
    "You are a senior design director auditing a rendered brand website. "
    "Evaluate only visible visual evidence. "
)

PROMPT_TEMPLATE = """Analyze the visual signature of the brand "{brand_name}" from its website screenshot.

Return ONLY valid JSON with this exact shape:
{{
  "aesthetic_style": "primary design style, or not_detected",
  "visual_mood": "visual mood / emotional tone, or not_detected",
  "visual_polish_score": 1-10,
  "visual_polish_rationale": "one short justification",
  "visual_coherence": "how imagery/layout supports brand promises, or not_detected",
  "brand_distinctiveness": "high, medium, low, or not_detected",
  "category_fit": "how well the visible execution fits its likely category, or not_detected",
  "copy_visual_alignment": "whether visible copy and design appear aligned, or not_detected",
  "logo_prominence": "clear, partial, weak, or not_detected",
  "hierarchy_clarity": "clear, mixed, weak, or not_detected",
  "cta_salience": "clear, partial, weak, or not_detected",
  "trust_signal_presence": "clear, partial, weak, or not_detected",
  "first_impression_summary": "one short first-impression summary, or not_detected",
  "observed_strengths": ["short visible strengths"],
  "observed_risks": ["short visible risks"],
  "notable_absences": ["important missing visual signals"]
}}

Use not_detected for fields where the screenshot does not provide enough evidence.
Use [] for list fields when there is insufficient evidence.
Do not infer facts that are not visible in the image."""

ATLAS_INSTRUCTIONS = """

This input is a labeled analysis atlas, not one continuous screenshot.
- The panel labeled FIRST VIEWPORT is the only basis for first impression,
  above-the-fold hierarchy, initial CTA salience, and logo prominence.
- The ordered SECTION panels are additional whole-page evidence for visual
  coherence, consistency, trust signals, strengths, risks, and absences.
- Atlas labels and gray gutters are audit scaffolding, not website design.
- Do not count content repeated between FIRST VIEWPORT and SECTION panels twice.
"""


def analyze_visual_semantics(screenshot_path: str | None, brand_name: str) -> dict[str, Any]:
    """Analyze a local screenshot with Gemini Vision and always return a stable contract."""
    analysis_scope = (
        "labeled_section_atlas"
        if screenshot_path
        and "analysis-atlas" in Path(screenshot_path).name.lower()
        else "single_capture"
    )
    if BRAND3_VISUAL_SIGNATURE_SKIP_MULTIMODAL:
        return fallback_semantics(
            "multimodal_disabled",
            analysis_scope=analysis_scope,
        )

    if not screenshot_path:
        return fallback_semantics("screenshot_path_missing")

    path = Path(screenshot_path)
    if not path.exists() or not path.is_file():
        return fallback_semantics(
            "screenshot_file_not_found",
            analysis_scope=analysis_scope,
        )

    if not BRAND3_LLM_API_KEY:
        return fallback_semantics(
            "api_key_missing",
            analysis_scope=analysis_scope,
        )

    try:
        encoded_image = encode_image_base64(path)
    except Exception:
        return fallback_semantics(
            "screenshot_unreadable",
            analysis_scope=analysis_scope,
        )

    if not encoded_image:
        return fallback_semantics(
            "screenshot_empty",
            analysis_scope=analysis_scope,
        )

    body = build_multimodal_payload(
        encoded_image=encoded_image,
        mime_type=_mime_type_for_path(path),
        brand_name=brand_name,
        analysis_scope=analysis_scope,
    )

    try:
        status, content = _run_llm_http_call(
            url=f"{LLM_BASE_URL}/chat/completions",
            payload=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BRAND3_LLM_API_KEY}",
            },
            timeout_seconds=_multimodal_effective_timeout(),
        )
    except Exception:
        return fallback_semantics("llm_error", analysis_scope=analysis_scope)

    if status != "ok":
        error_type = "llm_timeout" if status == "timeout" else "llm_error"
        return fallback_semantics(error_type, analysis_scope=analysis_scope)

    if not content:
        return fallback_semantics("empty_response", analysis_scope=analysis_scope)

    try:
        parsed = json.loads(_strip_json_fence(content))
    except json.JSONDecodeError:
        return fallback_semantics("json_parse_error", analysis_scope=analysis_scope)

    if not isinstance(parsed, dict):
        return fallback_semantics("invalid_response", analysis_scope=analysis_scope)

    data = normalize_semantics_data(parsed)
    return {
        "status": "detected",
        "model": BRAND3_VISUAL_SIGNATURE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "fallback_used": False,
        "error_type": None,
        "audit": {
            "analysis_scope": analysis_scope,
            "capture_count": 1,
            "input_kind": "labeled_atlas"
            if analysis_scope == "labeled_section_atlas"
            else "screenshot",
            "response_normalized": True,
        },
        "data": data,
    }


def fallback_semantics(
    error_type: str | None,
    *,
    analysis_scope: str = "single_capture",
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "model": BRAND3_VISUAL_SIGNATURE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "fallback_used": True,
        "error_type": error_type,
        "audit": {
            "analysis_scope": analysis_scope,
            "capture_count": 1,
            "input_kind": "labeled_atlas"
            if analysis_scope == "labeled_section_atlas"
            else "screenshot",
            "response_normalized": True,
        },
        "data": {
            "aesthetic_style": "not_detected",
            "visual_mood": "not_detected",
            "visual_polish_score": None,
            "visual_polish_rationale": "",
            "visual_coherence": "not_detected",
            "brand_distinctiveness": "not_detected",
            "category_fit": "not_detected",
            "copy_visual_alignment": "not_detected",
            "logo_prominence": "not_detected",
            "hierarchy_clarity": "not_detected",
            "cta_salience": "not_detected",
            "trust_signal_presence": "not_detected",
            "first_impression_summary": "not_detected",
            "observed_strengths": [],
            "observed_risks": [],
            "notable_absences": [],
        },
    }


def encode_image_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def build_multimodal_payload(
    *,
    encoded_image: str,
    mime_type: str,
    brand_name: str,
    analysis_scope: str = "single_capture",
) -> dict[str, Any]:
    return _build_multimodal_payload(
        encoded_image=encoded_image,
        mime_type=mime_type,
        brand_name=brand_name,
        prompt_template=(
            PROMPT_TEMPLATE + ATLAS_INSTRUCTIONS
            if analysis_scope == "labeled_section_atlas"
            else PROMPT_TEMPLATE
        ),
        system_preamble=SYSTEM_PREAMBLE,
    )


def build_cache_key(*, brand_name: str, screenshot_bytes: bytes) -> str:
    """Stable cache key for multimodal calls. Bumps when PROMPT_VERSION changes."""
    return _build_cache_key(
        model=BRAND3_VISUAL_SIGNATURE_MODEL,
        prompt_version=PROMPT_VERSION,
        brand_name=brand_name,
        screenshot_bytes=screenshot_bytes,
    )


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _run_llm_http_call(*, url: str, payload: bytes, headers: dict[str, str], timeout_seconds: int) -> tuple[str, str]:
    return _run_multimodal_http_call(
        url=url,
        payload=payload,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
