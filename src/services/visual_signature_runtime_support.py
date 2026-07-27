"""Support helpers for Visual Signature shadow runtime orchestration."""

from __future__ import annotations

from typing import Any

from src.visual_signature import build_visual_signature_evidence_v1, build_visual_signature_scan
from src.visual_signature.persistence import build_visual_signature_persistence_bundle


def build_failure_payload(*, brand_name: str, url: str, error: str) -> dict[str, object]:
    return {
        "brand_name": brand_name,
        "website_url": url,
        "analyzed_url": url,
        "interpretation_status": "not_interpretable",
        "acquisition": {
            "adapter": "visual_acquisition_shadow_run",
            "status_code": None,
            "warnings": [],
            "errors": [error],
        },
        "extraction_confidence": {
            "score": 0.0,
            "level": "low",
            "factors": {},
            "limitations": ["shadow_run_extraction_failed"],
        },
        "version": "visual-signature-mvp-1",
    }


def enrich_payload_with_vision(
    *,
    payload: dict[str, object],
    screenshot_payload: dict[str, object] | None,
    run_id: int | None,
    vision_enricher,
    logger,
    events: list[str],
) -> dict[str, object]:
    if not screenshot_payload:
        events.append("vision_skipped")
        return payload
    try:
        return vision_enricher(
            visual_signature_payload=payload,
            screenshot_path=str(screenshot_payload.get("path") or "") or None,
            screenshot_payload=screenshot_payload,
        )
    except Exception as exc:
        events.append("vision_skipped")
        logger.warning("visual_signature shadow vision skipped (run_id=%s): %s", run_id, exc, exc_info=True)
        return payload


def build_shadow_bundle(
    *,
    payload: dict[str, object],
    run_id: int | None,
    brand_name: str,
    url: str,
    screenshot_payload: dict[str, object] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    vision = payload.get("vision") if isinstance(payload.get("vision"), dict) else None
    screenshot = (vision or {}).get("screenshot") if isinstance(vision, dict) else {}
    section_manifest = (
        screenshot_payload.get("section_manifest")
        if isinstance(screenshot_payload, dict) and isinstance(screenshot_payload.get("section_manifest"), dict)
        else {}
    )
    section_rows = (
        section_manifest.get("sections")
        if isinstance(section_manifest, dict) and isinstance(section_manifest.get("sections"), list)
        else []
    )
    full_page_path = str(
        (screenshot_payload or {}).get("full_page_screenshot_path")
        or (section_manifest or {}).get("full_page_screenshot_path")
        or ""
    )
    section_paths = [
        str(row.get("capture_path"))
        for row in section_rows
        if isinstance(row, dict) and str(row.get("capture_path") or "").strip()
    ]
    atlas_path = str((screenshot_payload or {}).get("analysis_atlas_path") or "")
    atlas_manifest = (
        screenshot_payload.get("analysis_atlas_manifest")
        if isinstance(screenshot_payload, dict)
        and isinstance(screenshot_payload.get("analysis_atlas_manifest"), dict)
        else {}
    )
    visual_signature_scan = build_visual_signature_scan(payload)
    visual_signature_evidence = build_visual_signature_evidence_v1(
        payload,
        screenshot_payload=screenshot if isinstance(screenshot, dict) and screenshot else screenshot_payload,
    )
    bundle = build_visual_signature_persistence_bundle(
        raw_visual_signature_payload=payload,
        vision_payload=vision,
        agreement_payload=(vision or {}).get("agreement") if isinstance(vision, dict) else None,
        visual_signature_scan=visual_signature_scan,
        run_id=run_id,
        brand_name=brand_name,
        website_url=url,
        screenshot_path=(screenshot or {}).get("path") if isinstance(screenshot, dict) else (screenshot_payload or {}).get("path"),
        secondary_screenshot_path=full_page_path or None,
        full_page_screenshot_path=full_page_path or None,
        section_screenshot_paths=section_paths,
        section_manifest=section_manifest if isinstance(section_manifest, dict) else None,
        analysis_atlas_path=atlas_path or None,
        analysis_atlas_manifest=atlas_manifest,
        capture_type=(screenshot or {}).get("capture_type") if isinstance(screenshot, dict) else (screenshot_payload or {}).get("capture_type"),
        secondary_capture_type="full_page" if full_page_path else None,
        visual_signature_evidence=visual_signature_evidence,
    )
    return vision, screenshot, {
        "visual_signature_scan": visual_signature_scan,
        "visual_evidence_packet": visual_signature_evidence,
        "visual_signature_evidence": visual_signature_evidence,
        "bundle": bundle,
    }


def persist_shadow_bundle(
    *,
    store,
    run_id: int | None,
    bundle: dict[str, Any],
    persistence_fn,
    logger,
    events: list[str],
) -> bool:
    persisted = False
    try:
        persistence_fn(store, run_id, bundle)
        persisted = bool(store and run_id is not None)
        if persisted:
            events.append("persisted")
            logger.info("visual_signature shadow persisted (run_id=%s)", run_id)
    except Exception as exc:
        events.append("persistence_skipped")
        logger.warning("visual_signature shadow persistence skipped (run_id=%s): %s", run_id, exc, exc_info=True)
    return persisted


def shadow_result(
    *,
    status: str,
    events: list[str],
    persisted: bool,
    payload: dict[str, object],
    vision: dict[str, Any] | None,
    visual_signature_scan: dict[str, Any],
    visual_signature_evidence: dict[str, Any],
) -> dict[str, object]:
    return {
        "status": status,
        "events": events,
        "persisted": persisted,
        "interpretation_status": payload.get("interpretation_status"),
        "agreement_level": ((vision or {}).get("agreement") or {}).get("agreement_level") if isinstance(vision, dict) else None,
        "visual_signature_score": visual_signature_scan.get("score"),
        "visual_signature_scan_status": visual_signature_scan.get("status"),
        "visual_evidence_packet": visual_signature_evidence,
        "visual_signature_evidence": visual_signature_evidence,
    }
