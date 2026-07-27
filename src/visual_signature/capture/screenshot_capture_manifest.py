"""Manifest and result helpers for Visual Signature screenshot capture."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.visual_signature.capture.playwright_capture_runtime import (
    _coerce_dict_or_none,
    _coerce_transition_list,
)
from src.visual_signature.capture.screenshot_capture_models import CaptureResult


def build_success_result(
    *,
    brand_name: str,
    website_url: str,
    screenshot_path: Path,
    primary_capture_type: str,
    metadata: dict[str, Any],
    captured_at: str,
    secondary_path: Path | None = None,
    secondary_capture_type: str | None = None,
    secondary_metadata: dict[str, Any] | None = None,
) -> CaptureResult:
    file_size = screenshot_path.stat().st_size if screenshot_path.exists() else None
    secondary_file_size = secondary_path.stat().st_size if secondary_path and secondary_path.exists() else None
    clean_attempt_capture_variant = "clean_attempt" if metadata.get("clean_attempt_screenshot_path") else None
    return CaptureResult(
        brand_name=brand_name,
        website_url=website_url,
        screenshot_path=str(screenshot_path),
        status="ok",
        source=str(metadata.get("source") or "playwright"),
        capture_type=str(metadata.get("capture_type") or primary_capture_type or "viewport"),
        capture_variant=str(
            metadata.get("capture_variant") or ("raw_viewport" if metadata.get("dismissal_attempted") else primary_capture_type or "viewport")
        ),
        clean_attempt_capture_variant=str(metadata.get("clean_attempt_capture_variant") or clean_attempt_capture_variant) or None,
        raw_screenshot_path=str(metadata.get("raw_screenshot_path") or screenshot_path),
        clean_attempt_screenshot_path=str(metadata.get("clean_attempt_screenshot_path") or "") or None,
        secondary_screenshot_path=str(secondary_path) if secondary_path else None,
        secondary_capture_type=secondary_capture_type,
        full_page_screenshot_path=str(metadata.get("full_page_screenshot_path") or "") or None,
        section_capture_status=str(metadata.get("section_capture_status") or "") or None,
        section_manifest=_coerce_dict_or_none(
            metadata.get("section_manifest"),
            field_name="section_manifest",
        ),
        lazy_content_hydration=_coerce_dict_or_none(
            metadata.get("lazy_content_hydration"),
            field_name="lazy_content_hydration",
        ),
        post_hydration_obstruction_check=_coerce_dict_or_none(
            metadata.get("post_hydration_obstruction_check"),
            field_name="post_hydration_obstruction_check",
        ),
        analysis_atlas_path=str(metadata.get("analysis_atlas_path") or "") or None,
        analysis_atlas_status=str(metadata.get("analysis_atlas_status") or "") or None,
        analysis_atlas_manifest=_coerce_dict_or_none(
            metadata.get("analysis_atlas_manifest"),
            field_name="analysis_atlas_manifest",
        ),
        structured_capture_errors=[
            str(item)
            for item in metadata.get("structured_capture_errors") or []
        ],
        page_url=str(metadata.get("page_url") or website_url),
        width=_int_or_none(metadata.get("width")),
        height=_int_or_none(metadata.get("height")),
        viewport_width=_int_or_none(metadata.get("viewport_width")),
        viewport_height=_int_or_none(metadata.get("viewport_height")),
        file_size_bytes=file_size,
        secondary_width=_int_or_none((secondary_metadata or {}).get("width")),
        secondary_height=_int_or_none((secondary_metadata or {}).get("height")),
        secondary_file_size_bytes=secondary_file_size,
        dismissal_attempted=bool(metadata.get("dismissal_attempted")),
        dismissal_successful=bool(metadata.get("dismissal_successful")),
        dismissal_method=str(metadata.get("dismissal_method") or "") or None,
        clicked_text=str(metadata.get("clicked_text") or "") or None,
        dismissal_eligibility=str(metadata.get("dismissal_eligibility") or "") or None,
        dismissal_block_reason=str(metadata.get("dismissal_block_reason") or "") or None,
        candidate_click_targets=[dict(item) for item in metadata.get("candidate_click_targets") or [] if isinstance(item, dict)],
        rejected_click_targets=[dict(item) for item in metadata.get("rejected_click_targets") or [] if isinstance(item, dict)],
        before_obstruction=_coerce_dict_or_none(metadata.get("before_obstruction"), field_name="before_obstruction"),
        after_obstruction=_coerce_dict_or_none(metadata.get("after_obstruction"), field_name="after_obstruction"),
        evidence_integrity_notes=[str(item) for item in metadata.get("evidence_integrity_notes") or []],
        raw_viewport_metrics=_coerce_dict_or_none(metadata.get("raw_viewport_metrics"), field_name="raw_viewport_metrics"),
        clean_attempt_metrics=_coerce_dict_or_none(metadata.get("clean_attempt_metrics"), field_name="clean_attempt_metrics"),
        perceptual_state=str(metadata.get("perceptual_state") or "") or None,
        perceptual_transitions=_coerce_transition_list(metadata.get("perceptual_transitions")),
        mutation_audit=_coerce_dict_or_none(metadata.get("mutation_audit"), field_name="mutation_audit"),
        perceptual_state_data=_coerce_dict_or_none(metadata.get("perceptual_state_data"), field_name="perceptual_state_data"),
        captured_at=captured_at,
    )


def build_error_result(
    *,
    brand_name: str,
    website_url: str,
    screenshot_path: Path,
    capture_type: str,
    error: Exception,
    captured_at: str,
) -> CaptureResult:
    return CaptureResult(
        brand_name=brand_name,
        website_url=website_url,
        screenshot_path=str(screenshot_path),
        status="error",
        error=str(error),
        capture_type=capture_type,
        capture_variant="error",
        page_url=website_url,
        evidence_integrity_notes=[f"capture_error: {error}"],
        captured_at=captured_at,
    )


def build_manifest(
    *,
    results: list[CaptureResult],
    started_at: str,
    completed_at: str,
    output_dir: Path,
    attempt_dismiss_obstructions: bool,
) -> dict[str, Any]:
    return {
        "started_at": started_at,
        "completed_at": completed_at,
        "output_dir": str(output_dir),
        "total": len(results),
        "ok": sum(1 for item in results if item.status == "ok"),
        "error": sum(1 for item in results if item.status == "error"),
        "attempt_dismiss_obstructions": attempt_dismiss_obstructions,
        "results": [capture_result_to_dict(item) for item in results],
    }


def capture_result_to_dict(item: CaptureResult) -> dict[str, Any]:
    payload = asdict(item)
    perceptual_state_data = payload.pop("perceptual_state_data", None)
    has_state_output = bool(
        payload.get("perceptual_state")
        or payload.get("perceptual_transitions")
        or payload.get("mutation_audit") is not None
        or perceptual_state_data
    )
    if not payload.get("perceptual_state") and perceptual_state_data:
        payload["perceptual_state"] = perceptual_state_data.get("current_state")
    if not payload.get("perceptual_transitions") and perceptual_state_data:
        payload["perceptual_transitions"] = perceptual_state_data.get("transitions") or []
    if payload.get("mutation_audit") is None and perceptual_state_data:
        if perceptual_state_data.get("mutation_results"):
            payload["mutation_audit"] = perceptual_state_data.get("mutation_results")[-1].get("mutation_audit")
        else:
            payload["mutation_audit"] = None
    if not has_state_output:
        payload.pop("perceptual_state", None)
        payload.pop("perceptual_transitions", None)
        payload.pop("mutation_audit", None)
    return payload


def invoke_capture_fn(
    capture_fn: Callable[..., dict[str, Any]],
    brand_name: str,
    website_url: str,
    screenshot_path: str,
    capture_type: str,
    *,
    attempt_dismiss_obstructions: bool,
) -> dict[str, Any]:
    try:
        return capture_fn(
            brand_name,
            website_url,
            screenshot_path,
            capture_type,
            attempt_dismiss_obstructions=attempt_dismiss_obstructions,
        )
    except TypeError:
        return capture_fn(brand_name, website_url, screenshot_path, capture_type)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def iso_now(now: Callable[[], datetime]) -> str:
    return now().isoformat()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
