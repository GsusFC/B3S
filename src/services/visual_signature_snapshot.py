"""Snapshot reconstruction helpers for Visual Signature runs."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse

from src.collectors.web_collector import WebData
from src.visual_signature.acquisition_contract import (
    is_visual_acquisition_source,
    visual_evidence_packet_from_payload,
)


def _visual_signature_shadow_screenshot_payload(
    screenshot_capture: dict[str, object] | None,
    *,
    page_url: str,
) -> dict[str, object] | None:
    if not isinstance(screenshot_capture, dict):
        return None
    screenshot_url = str(screenshot_capture.get("screenshot_url") or "").strip()
    if not screenshot_url:
        return None
    parsed = urlparse(screenshot_url)
    metadata = screenshot_capture.get("metadata") if isinstance(screenshot_capture.get("metadata"), dict) else {}
    payload: dict[str, object] = {
        "screenshot_url": screenshot_url,
        "page_url": page_url,
        "source": screenshot_capture.get("source") or "existing_brand3_screenshot",
    }
    screenshot_path = str(screenshot_capture.get("screenshot_path") or "").strip()
    if screenshot_path:
        payload["path"] = screenshot_path
    elif parsed.scheme == "file":
        payload["path"] = unquote(parsed.path)
    if payload.get("path"):
        payload["capture_type"] = str(metadata.get("capture_type") or "viewport")
        payload["viewport_width"] = metadata.get("viewport_width") or 1440
        payload["viewport_height"] = metadata.get("viewport_height") or 1200
    for key in (
        "selected_capture_variant",
        "viewport_obstruction",
        "obstruction_observed_same_capture",
        "dismissal_attempted",
        "dismissal_successful",
        "dismissal_method",
        "dismissal_eligibility",
        "dismissal_block_reason",
        "evidence_integrity_notes",
        "full_page_screenshot_path",
        "section_capture_status",
        "section_manifest",
        "section_captures",
        "lazy_content_hydration",
        "post_hydration_obstruction_check",
        "analysis_atlas_path",
        "analysis_atlas_status",
        "analysis_atlas_manifest",
        "structured_capture_errors",
        "width",
        "height",
        "file_size_bytes",
    ):
        if metadata.get(key) not in (None, "", [], {}):
            payload[key] = metadata.get(key)
    return payload


def _snapshot_has_visual_signature_scan(snapshot: dict[str, Any]) -> bool:
    for item in snapshot.get("raw_inputs") or []:
        if not is_visual_acquisition_source(item.get("source")) or not isinstance(item.get("payload"), dict):
            continue
        scan = item["payload"].get("visual_signature_scan")
        if isinstance(scan, dict) and scan.get("schema_version") == "visual-signature-scan-v1":
            return True
    return False


def _snapshot_has_visual_signature_evidence(snapshot: dict[str, Any]) -> bool:
    for item in snapshot.get("raw_inputs") or []:
        if not is_visual_acquisition_source(item.get("source")) or not isinstance(item.get("payload"), dict):
            continue
        evidence = visual_evidence_packet_from_payload(item["payload"])
        if evidence is not None:
            return True
    return False


def _web_data_from_snapshot(snapshot: dict[str, Any], *, fallback_url: str):
    selected: WebData | None = None
    for item in snapshot.get("raw_inputs") or []:
        if item.get("source") != "web" or not isinstance(item.get("payload"), dict):
            continue
        try:
            selected = WebData(**item["payload"])
        except TypeError:
            continue
    return selected or WebData(url=fallback_url)


def _content_web_from_snapshot(snapshot: dict[str, Any], *, fallback):
    selected: WebData | None = None
    for item in snapshot.get("raw_inputs") or []:
        payload = item.get("payload")
        if item.get("source") != "web" or not isinstance(payload, dict):
            continue
        if payload.get("derived") != "discovery_enrichment":
            continue
        try:
            selected = WebData(**{key: value for key, value in payload.items() if key != "derived"})
        except TypeError:
            continue
    return selected or fallback


def _screenshot_capture_from_snapshot(snapshot: dict[str, Any]) -> dict[str, object] | None:
    selected: dict[str, object] | None = None
    for item in snapshot.get("raw_inputs") or []:
        if item.get("source") != "screenshot_capture" or not isinstance(item.get("payload"), dict):
            continue
        capture = item["payload"].get("capture")
        if isinstance(capture, dict):
            selected = capture
    return selected
