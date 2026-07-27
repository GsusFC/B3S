"""Capture input and result models for Visual Signature screenshot runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CaptureBrand:
    brand_name: str
    website_url: str
    screenshot_path: str
    capture_type: str = "viewport"


@dataclass
class CaptureResult:
    brand_name: str
    website_url: str
    screenshot_path: str
    status: str
    error: str | None = None
    source: str = "playwright"
    capture_type: str = "full_page"
    capture_variant: str = "viewport"
    clean_attempt_capture_variant: str | None = None
    raw_screenshot_path: str | None = None
    clean_attempt_screenshot_path: str | None = None
    secondary_screenshot_path: str | None = None
    secondary_capture_type: str | None = None
    full_page_screenshot_path: str | None = None
    section_capture_status: str | None = None
    section_manifest: dict[str, Any] | None = None
    lazy_content_hydration: dict[str, Any] | None = None
    post_hydration_obstruction_check: dict[str, Any] | None = None
    analysis_atlas_path: str | None = None
    analysis_atlas_status: str | None = None
    analysis_atlas_manifest: dict[str, Any] | None = None
    structured_capture_errors: list[str] = field(default_factory=list)
    page_url: str | None = None
    width: int | None = None
    height: int | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    secondary_width: int | None = None
    secondary_height: int | None = None
    file_size_bytes: int | None = None
    secondary_file_size_bytes: int | None = None
    dismissal_attempted: bool = False
    dismissal_successful: bool = False
    dismissal_method: str | None = None
    clicked_text: str | None = None
    dismissal_eligibility: str | None = None
    dismissal_block_reason: str | None = None
    candidate_click_targets: list[dict[str, Any]] = field(default_factory=list)
    rejected_click_targets: list[dict[str, Any]] = field(default_factory=list)
    before_obstruction: dict[str, Any] | None = None
    after_obstruction: dict[str, Any] | None = None
    evidence_integrity_notes: list[str] = field(default_factory=list)
    raw_viewport_metrics: dict[str, Any] | None = None
    clean_attempt_metrics: dict[str, Any] | None = None
    perceptual_state: str | None = None
    perceptual_transitions: list[dict[str, Any]] = field(default_factory=list)
    mutation_audit: dict[str, Any] | None = None
    perceptual_state_data: dict[str, Any] | None = None
    captured_at: str | None = None


def load_capture_brands(path: str | Path) -> list[CaptureBrand]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    rows = payload.get("brands") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Capture input must be a list or an object with a 'brands' list")
    brands: list[CaptureBrand] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Row {index} must be an object")
        brand_name = str(row.get("brand_name") or row.get("brandName") or "").strip()
        website_url = str(row.get("website_url") or row.get("websiteUrl") or "").strip()
        screenshot_path = str(row.get("screenshot_path") or row.get("screenshotPath") or "").strip()
        capture_type = str(row.get("capture_type") or row.get("captureType") or "viewport").strip() or "viewport"
        if not brand_name or not website_url or not screenshot_path:
            raise ValueError(f"Row {index} must include brand_name, website_url, and screenshot_path")
        brands.append(
            CaptureBrand(
                brand_name=brand_name,
                website_url=website_url,
                screenshot_path=screenshot_path,
                capture_type=capture_type,
            )
        )
    return brands
