"""Defensive Gemini Vision semantics for Visual Signature."""

from __future__ import annotations

import base64
import json
import logging
import time
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

ERROR_DETAIL_MESSAGE_LIMIT = 300

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
    payload = json.dumps(body).encode("utf-8")
    image_bytes = _file_size(path)
    started_at = time.perf_counter()

    def unavailable(error_type: str, message: str, http_status: int | None = None) -> dict[str, Any]:
        return _failure_semantics(
            error_type,
            brand_name=brand_name,
            analysis_scope=analysis_scope,
            message=message,
            http_status=http_status,
            image_bytes=image_bytes,
            payload_bytes=len(payload),
            started_at=started_at,
        )

    try:
        status, content = _run_llm_http_call(
            url=f"{LLM_BASE_URL}/chat/completions",
            payload=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BRAND3_LLM_API_KEY}",
            },
            timeout_seconds=_multimodal_effective_timeout(),
        )
    except Exception as exc:
        return unavailable("llm_error", str(exc))

    if status != "ok":
        error_type = "llm_timeout" if status == "timeout" else "llm_error"
        return unavailable(error_type, content, http_status=_http_status_from_message(content))

    if not content:
        return unavailable("empty_response", "")

    try:
        parsed = json.loads(_strip_json_fence(content))
    except json.JSONDecodeError:
        return unavailable("json_parse_error", content)

    if not isinstance(parsed, dict):
        return unavailable("invalid_response", content)

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
    error_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Neutral contract; ``error_detail`` is attached only for failures observed after the request was built."""
    semantics: dict[str, Any] = {
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
    if error_detail is not None:
        semantics["error_detail"] = error_detail
    return semantics


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


def _failure_semantics(
    error_type: str,
    *,
    brand_name: str,
    analysis_scope: str,
    message: str,
    http_status: int | None,
    image_bytes: int | None,
    payload_bytes: int,
    started_at: float,
) -> dict[str, Any]:
    """Neutral fallback for a failure after the request was built, plus the detail needed to explain it."""
    error_detail = {
        "http_status": http_status,
        "message": _excerpt(str(message or "")),
        "image_bytes": image_bytes,
        "payload_bytes": payload_bytes,
        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
    }
    logger.warning(
        "visual_semantics unavailable (brand=%s model=%s error_type=%s http_status=%s image_bytes=%s "
        "payload_bytes=%s elapsed_ms=%s): %s",
        brand_name,
        BRAND3_VISUAL_SIGNATURE_MODEL,
        error_type,
        http_status,
        image_bytes,
        payload_bytes,
        error_detail["elapsed_ms"],
        error_detail["message"],
    )
    return fallback_semantics(error_type, analysis_scope=analysis_scope, error_detail=error_detail)


def _excerpt(message: str) -> str:
    """Keep both ends of a long message: the tail is what shows a truncated response."""
    if len(message) <= ERROR_DETAIL_MESSAGE_LIMIT:
        return message
    separator = " … "
    head = ERROR_DETAIL_MESSAGE_LIMIT * 2 // 3
    tail = ERROR_DETAIL_MESSAGE_LIMIT - head - len(separator)
    return f"{message[:head]}{separator}{message[-tail:]}"


def _http_status_from_message(message: str) -> int | None:
    """Parse the ``HTTP <code>: <body>`` prefix the shared transport emits on HTTPError."""
    if not isinstance(message, str) or not message.startswith("HTTP "):
        return None
    code = message[5:].partition(":")[0].strip()
    return int(code) if code.isdigit() else None


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


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
