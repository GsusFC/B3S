"""Helpers for capture artifacts and obstruction DOM serialization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.visual_signature.capture.screenshot_capture_models import CaptureResult
from src.visual_signature.vision.composition import analyze_composition
from src.visual_signature.vision.palette_from_screenshot import extract_palette_from_screenshot
from src.visual_signature.vision.screenshot_quality import load_raster_image
from src.visual_signature.vision.viewport_obstruction import analyze_viewport_obstruction


def _snapshot_for_path(path: Path, *, dom_html: str | None = None) -> dict[str, Any]:
    image = load_raster_image(str(path))
    palette = extract_palette_from_screenshot(image)
    composition = analyze_composition(image)
    obstruction = analyze_viewport_obstruction(dom_html=dom_html, viewport_image=image).to_dict()
    return {
        "metrics": {
            "viewport_whitespace_ratio": composition.whitespace_ratio,
            "viewport_visual_density": composition.visual_density,
            "viewport_composition": composition.composition_classification,
            "palette_color_count": palette.color_count,
            "palette_confidence": palette.confidence,
            "composition_confidence": composition.confidence,
        },
        "obstruction": obstruction,
    }


def _visible_obstruction_dom_snapshot(page: Any) -> str:
    if not hasattr(page, "evaluate"):
        try:
            return page.content()
        except Exception:
            return ""
    try:
        rows = page.evaluate(
            """
            () => {
              const selectors = [
                '[role="dialog"]',
                '[role="alertdialog"]',
                '[aria-modal="true"]',
                '[aria-label*="cookie" i]',
                '[aria-label*="consent" i]',
                '[aria-label*="privacy" i]',
                '[class*="cookie" i]',
                '[id*="cookie" i]',
                '[class*="consent" i]',
                '[id*="consent" i]',
                '[class*="modal" i]',
                '[id*="modal" i]',
                '[class*="popup" i]',
                '[id*="popup" i]',
                '[class*="newsletter" i]',
                '[id*="newsletter" i]',
                '[class*="banner" i]',
                '[id*="banner" i]'
              ].join(',');
              const nodes = Array.from(document.querySelectorAll(selectors));
              const candidates = [];
              const seen = new Set();
              for (const node of nodes) {
                if (!node || seen.has(node)) continue;
                seen.add(node);
                const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : null;
                const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
                if (!rect || !style) continue;
                const visible = rect.width > 0 && rect.height > 0
                  && rect.bottom > 0 && rect.right > 0
                  && rect.top < (window.innerHeight || 0)
                  && rect.left < (window.innerWidth || 0)
                  && style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0.02;
                if (!visible) continue;
                const position = style.position || '';
                const zIndex = style.zIndex || '';
                const role = node.getAttribute ? (node.getAttribute('role') || '') : '';
                const ariaModal = node.getAttribute ? (node.getAttribute('aria-modal') || '') : '';
                const ariaLabel = node.getAttribute ? (node.getAttribute('aria-label') || '') : '';
                const className = typeof node.className === 'string' ? node.className : '';
                const id = node.id || '';
                const text = (node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 600);
                const overlayish = ['fixed', 'sticky'].includes(position)
                  || role === 'dialog'
                  || role === 'alertdialog'
                  || ariaModal === 'true'
                  || /cookie|consent|privacy|modal|popup|newsletter|banner/i.test(`${id} ${className} ${ariaLabel} ${text}`)
                  || Number.parseInt(zIndex || '0', 10) >= 100;
                if (!overlayish) continue;
                candidates.push({
                  tag: node.tagName ? node.tagName.toLowerCase() : '',
                  id,
                  className,
                  role,
                  ariaModal,
                  ariaLabel,
                  position,
                  zIndex,
                  width: Math.round(rect.width || 0),
                  height: Math.round(rect.height || 0),
                  top: Math.round(rect.top || 0),
                  left: Math.round(rect.left || 0),
                  text
                });
              }
              return candidates.slice(0, 24);
            }
            """
        )
    except Exception:
        return ""
    if not isinstance(rows, list):
        return ""
    parts: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        attrs = " ".join(
            f"{key}={value}"
            for key, value in {
                "tag": row.get("tag"),
                "id": row.get("id"),
                "class": row.get("className"),
                "role": row.get("role"),
                "aria-modal": row.get("ariaModal"),
                "aria-label": row.get("ariaLabel"),
                "position": row.get("position"),
                "z-index": row.get("zIndex"),
                "width": row.get("width"),
                "height": row.get("height"),
                "top": row.get("top"),
                "left": row.get("left"),
            }.items()
            if value not in (None, "")
        )
        text = str(row.get("text") or "")
        parts.append(f"<visible-candidate {attrs}>{text}</visible-candidate>")
    if parts:
        return "\n".join(parts)
    return ""


def _coerce_dict_or_none(value: Any, *, field_name: str) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Field '{field_name}' must be valid JSON if provided as a string") from exc
        if parsed is None:
            return None
        if not isinstance(parsed, dict):
            raise ValueError(f"Field '{field_name}' must decode to an object")
        return parsed
    raise ValueError(f"Field '{field_name}' must be an object or JSON object string")


def _coerce_transition_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _derived_capture_path(path: Path, capture_type: str) -> Path:
    suffix = ".png"
    stem = path.name[:-len(suffix)] if path.name.endswith(suffix) else path.name
    return path.with_name(f"{stem}.{capture_type.replace('_', '-')}{path.suffix or '.png'}")
