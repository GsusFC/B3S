"""Fingerprint helpers for Visual Signature evidence packets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def fingerprint_contract(
    payload: dict[str, Any],
    capture: dict[str, Any],
    screenshot: dict[str, Any],
) -> dict[str, Any]:
    normalized_payload = {
        "brand_name": payload.get("brand_name"),
        "website_url": payload.get("website_url"),
        "analyzed_url": payload.get("analyzed_url"),
        "capture": {
            "status": capture.get("status"),
            "capture_variant": capture.get("capture_variant"),
            "capture_recovery": capture.get("capture_recovery"),
            "section_capture_status": capture.get("section_capture_status"),
            "viewport": capture.get("viewport"),
            "url_final": capture.get("url_final"),
        },
        "identity": payload.get("logo"),
        "visual_system": {
            "colors": payload.get("colors"),
            "typography": payload.get("typography"),
            "layout": payload.get("layout"),
            "components": payload.get("components"),
            "consistency": payload.get("consistency"),
        },
        "semantics": payload.get("semantics"),
    }
    return {
        "screenshot_sha256": screenshot_sha256(screenshot),
        "normalized_payload_sha256": sha256_json(normalized_payload),
        "capture_variant": capture.get("capture_variant"),
        "captured_at": capture.get("captured_at"),
        "viewport": capture.get("viewport"),
        "url_requested": capture.get("url_requested"),
        "url_final": capture.get("url_final"),
    }


def screenshot_sha256(screenshot: dict[str, Any]) -> str | None:
    explicit = screenshot.get("sha256") or screenshot.get("screenshot_sha256")
    if explicit:
        return str(explicit)
    path_value = screenshot.get("path")
    if not path_value:
        return None
    path = Path(str(path_value))
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
