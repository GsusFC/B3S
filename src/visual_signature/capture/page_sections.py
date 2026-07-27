"""Rendered-page section discovery and structural screenshot capture.

The detector uses the browser's live DOM and layout geometry. It does not
claim to recover a designer's original intent; it records the structure that
the shipped page exposes after rendering.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Callable

from src.visual_signature.capture.analysis_atlas import build_analysis_atlas


SECTION_MANIFEST_VERSION = "rendered-page-section-manifest-v1"
SECTION_ANALYSIS_MAX_CAPTURES = 16
SECTION_MAX_HEIGHT = 1800
SECTION_MIN_HEIGHT = 120


_RENDERED_SECTION_SNAPSHOT_JS = r"""
() => {
  const viewportWidth = Math.max(1, window.innerWidth || document.documentElement.clientWidth || 1);
  const viewportHeight = Math.max(1, window.innerHeight || document.documentElement.clientHeight || 1);
  const documentWidth = Math.max(
    viewportWidth,
    document.documentElement.scrollWidth || 0,
    document.body ? document.body.scrollWidth : 0
  );
  const documentHeight = Math.max(
    viewportHeight,
    document.documentElement.scrollHeight || 0,
    document.body ? document.body.scrollHeight : 0
  );

  const clean = (value, limit = 180) =>
    String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);
  const visible = (element, rect) => {
    const style = window.getComputedStyle(element);
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      Number(style.opacity || 1) > 0.02 &&
      rect.width >= Math.min(320, viewportWidth * 0.45) &&
      rect.height >= 36 &&
      rect.bottom > 0 &&
      rect.top < documentHeight
    );
  };
  const domPath = (element) => {
    if (element.id) return `#${CSS.escape(element.id)}`;
    const parts = [];
    let node = element;
    while (node && node.nodeType === 1 && parts.length < 4) {
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) {
        parts.unshift(tag);
        break;
      }
      const peers = Array.from(parent.children).filter((item) => item.tagName === node.tagName);
      const suffix = peers.length > 1 ? `:nth-of-type(${peers.indexOf(node) + 1})` : "";
      parts.unshift(`${tag}${suffix}`);
      node = parent;
    }
    return parts.join(" > ");
  };
  const inferKind = (element, top) => {
    const tag = element.tagName.toLowerCase();
    const role = clean(element.getAttribute("role"), 60).toLowerCase();
    if (tag === "header" || role === "banner") return "header";
    if (tag === "footer" || role === "contentinfo") return "footer";
    if (tag === "nav" || role === "navigation") return "navigation";
    if (tag === "article") return "article";
    if (top <= viewportHeight * 1.15 && element.querySelector("h1")) return "hero";
    if (role === "region") return "region";
    if (element.hasAttribute("data-component")) return "component";
    return "section";
  };
  const candidates = [];
  const seen = new Set();
  const add = (element, source, priority) => {
    if (!element || seen.has(element)) return;
    const rect = element.getBoundingClientRect();
    if (!visible(element, rect)) return;
    const top = Math.max(0, rect.top + window.scrollY);
    const left = Math.max(0, rect.left + window.scrollX);
    const text = clean(element.innerText || element.textContent, 320);
    const mediaCount = element.querySelectorAll("img, picture, video, svg, canvas").length;
    if (text.length < 12 && mediaCount === 0 && rect.height < viewportHeight * 0.45) return;
    const heading = element.matches("h1, h2, h3")
      ? clean(element.innerText || element.textContent, 180)
      : clean(element.querySelector("h1, h2, h3")?.innerText, 180);
    seen.add(element);
    candidates.push({
      source,
      priority,
      kind: inferKind(element, top),
      tag: element.tagName.toLowerCase(),
      role: clean(element.getAttribute("role"), 60),
      aria_label: clean(element.getAttribute("aria-label"), 180),
      heading,
      id: clean(element.id, 100),
      class_name: clean(typeof element.className === "string" ? element.className : "", 180),
      data_section: clean(element.getAttribute("data-section"), 120),
      data_component: clean(element.getAttribute("data-component"), 120),
      selector: domPath(element),
      text_sample: text,
      media_count: mediaCount,
      left: Math.round(left),
      top: Math.round(top),
      width: Math.round(Math.min(rect.width, documentWidth - left)),
      height: Math.round(Math.min(rect.height, documentHeight - top)),
    });
  };

  document
    .querySelectorAll(
      "header, footer, main > section, main > article, body > section, body > article, " +
      "main [role='region'], body > [role='region'], [data-section], [data-component]"
    )
    .forEach((element) => add(element, "semantic", 100));

  document.querySelectorAll("main section, main article, body > main").forEach((element) =>
    add(element, "semantic_nested", 85)
  );

  document.querySelectorAll("h1, h2").forEach((heading) => {
    let root = heading.parentElement;
    let best = heading;
    for (let depth = 0; root && depth < 5; depth += 1, root = root.parentElement) {
      if (root === document.body || root === document.documentElement) break;
      const rect = root.getBoundingClientRect();
      if (root.matches("section, article, [role='region'], [data-section], [data-component]")) {
        best = root;
        break;
      }
      if (
        rect.width >= viewportWidth * 0.55 &&
        rect.height >= Math.max(160, viewportHeight * 0.2) &&
        rect.height <= viewportHeight * 3.2
      ) {
        best = root;
        break;
      }
    }
    add(best, "heading_fallback", heading.tagName.toLowerCase() === "h1" ? 75 : 65);
  });

  return {
    viewport: {width: viewportWidth, height: viewportHeight},
    document: {width: documentWidth, height: documentHeight},
    candidates,
  };
}
"""


def hydrate_lazy_content(
    page: Any,
    *,
    max_steps: int = 18,
    wait_ms: int = 90,
) -> dict[str, Any]:
    """Scroll through a bounded amount of the page so lazy content can render."""

    initial = _document_metrics(page)
    viewport_height = max(1, int(initial.get("viewport_height") or 900))
    position = 0
    steps = 0
    reached_end = False
    try:
        while steps < max_steps:
            metrics = _document_metrics(page)
            document_height = max(viewport_height, int(metrics.get("document_height") or viewport_height))
            target = min(
                max(0, document_height - viewport_height),
                position + max(480, int(viewport_height * 0.82)),
            )
            if target <= position:
                reached_end = True
                break
            page.evaluate("(y) => window.scrollTo(0, y)", target)
            page.wait_for_timeout(wait_ms)
            position = target
            steps += 1
            if position >= document_height - viewport_height:
                reached_end = True
                page.wait_for_timeout(wait_ms)
                break
    finally:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(min(120, max(0, wait_ms)))

    final = _document_metrics(page)
    return {
        "attempted": True,
        "steps": steps,
        "reached_end": reached_end,
        "truncated": not reached_end,
        "initial_document_height": int(initial.get("document_height") or 0),
        "final_document_height": int(final.get("document_height") or 0),
        "viewport_height": viewport_height,
    }


def rendered_section_snapshot(page: Any) -> dict[str, Any]:
    snapshot = page.evaluate(_RENDERED_SECTION_SNAPSHOT_JS)
    return snapshot if isinstance(snapshot, dict) else {}


def build_section_manifest(
    snapshot: dict[str, Any],
    *,
    page_url: str = "",
    capture_variant: str = "raw_viewport",
    max_captures: int = SECTION_ANALYSIS_MAX_CAPTURES,
    max_section_height: int = SECTION_MAX_HEIGHT,
    min_section_height: int = SECTION_MIN_HEIGHT,
) -> dict[str, Any]:
    """Normalize noisy DOM candidates into full-width vertical page bands."""

    viewport = snapshot.get("viewport") if isinstance(snapshot.get("viewport"), dict) else {}
    document = snapshot.get("document") if isinstance(snapshot.get("document"), dict) else {}
    viewport_width = max(1, _int(viewport.get("width"), 1440))
    viewport_height = max(1, _int(viewport.get("height"), 900))
    document_height = max(viewport_height, _int(document.get("height"), viewport_height))
    document_width = max(viewport_width, _int(document.get("width"), viewport_width))
    max_captures = max(1, int(max_captures))
    max_section_height = max(min_section_height, int(max_section_height))

    anchors = _normalized_anchors(
        snapshot.get("candidates"),
        viewport_width=viewport_width,
        document_height=document_height,
        min_section_height=min_section_height,
    )
    detection_strategy = "rendered_dom_semantics_and_headings"
    if not anchors:
        anchors = [
            {
                "top": top,
                "kind": "viewport_slice",
                "heading": "",
                "aria_label": "",
                "data_section": "",
                "data_component": "",
                "selector": "",
                "source": "geometric_fallback",
                "priority": 0,
            }
            for top in range(0, document_height, viewport_height)
        ]
        detection_strategy = "geometric_viewport_fallback"
    elif int(anchors[0].get("top") or 0) > min_section_height:
        anchors.insert(
            0,
            {
                "top": 0,
                "kind": "first_fold",
                "heading": "",
                "aria_label": "",
                "data_section": "",
                "data_component": "",
                "selector": "",
                "source": "synthetic_page_start",
                "priority": 0,
            },
        )

    bands: list[dict[str, Any]] = []
    truncated = False
    for anchor_index, anchor in enumerate(anchors):
        top = max(0, min(document_height - 1, _int(anchor.get("top"), 0)))
        next_top = (
            max(top + 1, min(document_height, _int(anchors[anchor_index + 1].get("top"), document_height)))
            if anchor_index + 1 < len(anchors)
            else document_height
        )
        band_height = max(1, next_top - top)
        part_count = max(1, math.ceil(band_height / max_section_height))
        for part_index in range(part_count):
            if len(bands) >= max_captures:
                truncated = True
                break
            part_top = top + part_index * max_section_height
            part_height = min(max_section_height, next_top - part_top)
            if part_height <= 0:
                continue
            label = _section_label(anchor, index=len(bands), part_index=part_index, part_count=part_count)
            section_id = f"section-{len(bands) + 1:02d}-{_slug(label)}"
            bands.append(
                {
                    "id": section_id,
                    "index": len(bands),
                    "label": label,
                    "kind": str(anchor.get("kind") or "section"),
                    "source": str(anchor.get("source") or ""),
                    "selector": str(anchor.get("selector") or ""),
                    "heading": str(anchor.get("heading") or ""),
                    "capture_variant": capture_variant,
                    "bbox": {
                        "left": 0,
                        "top": part_top,
                        "width": viewport_width,
                        "height": part_height,
                    },
                    "part_index": part_index + 1,
                    "part_count": part_count,
                    "capture_path": None,
                }
            )
        if truncated:
            break

    covered_pixels = sum(int((section.get("bbox") or {}).get("height") or 0) for section in bands)
    limitations: list[str] = []
    if truncated:
        limitations.append("section_capture_limit_reached")
    if detection_strategy == "geometric_viewport_fallback":
        limitations.append("no_reliable_semantic_or_heading_boundaries_detected")
    return {
        "schema_version": SECTION_MANIFEST_VERSION,
        "page_url": page_url,
        "capture_variant": capture_variant,
        "detection_strategy": detection_strategy,
        "viewport": {"width": viewport_width, "height": viewport_height},
        "document": {"width": document_width, "height": document_height},
        "section_count": len(bands),
        "candidate_count": len(snapshot.get("candidates") or []),
        "coverage_ratio": round(min(1.0, covered_pixels / document_height), 4),
        "truncated": truncated,
        "limitations": limitations,
        "sections": bands,
    }


def capture_structured_page_evidence(
    page: Any,
    *,
    screenshot_path: str | Path,
    page_url: str,
    capture_variant: str,
    viewport_screenshot_path: str | Path | None = None,
    post_hydration_hook: Callable[[Any], dict[str, Any]] | None = None,
    max_captures: int = SECTION_ANALYSIS_MAX_CAPTURES,
) -> dict[str, Any]:
    """Capture one master full-page image and the normalized section bands."""

    base_path = Path(screenshot_path)
    hydration = hydrate_lazy_content(page)
    post_hydration = {}
    if post_hydration_hook is not None:
        try:
            value = post_hydration_hook(page)
            post_hydration = value if isinstance(value, dict) else {}
        except Exception as exc:
            post_hydration = {
                "checked": True,
                "successful": False,
                "error": f"{type(exc).__name__}:{exc}",
            }
    structural_variant = (
        "post_hydration_clean_attempt"
        if post_hydration.get("successful") is True
        else capture_variant
    )
    snapshot = rendered_section_snapshot(page)
    manifest = build_section_manifest(
        snapshot,
        page_url=page_url,
        capture_variant=structural_variant,
        max_captures=max_captures,
    )
    manifest["post_hydration_obstruction_check"] = post_hydration
    errors: list[str] = []

    full_page_path = _derived_path(base_path, "full-page")
    try:
        page.screenshot(
            path=str(full_page_path),
            full_page=True,
            animations="disabled",
            timeout=12000,
        )
        manifest["full_page_screenshot_path"] = str(full_page_path)
    except Exception as exc:
        manifest["full_page_screenshot_path"] = None
        errors.append(f"full_page_capture_failed:{type(exc).__name__}:{exc}")

    captured = 0
    full_page_available = bool(manifest.get("full_page_screenshot_path"))
    for section in manifest.get("sections") or []:
        section_path = _derived_path(
            base_path,
            str(section.get("id") or f"section-{captured + 1:02d}"),
        )
        if not full_page_available:
            section["capture_path"] = None
            section["capture_error"] = "full_page_master_unavailable"
            continue
        try:
            _crop_section_from_master(
                full_page_path=full_page_path,
                section_path=section_path,
                bbox=section.get("bbox") if isinstance(section.get("bbox"), dict) else {},
                viewport=manifest.get("viewport") if isinstance(manifest.get("viewport"), dict) else {},
                document=manifest.get("document") if isinstance(manifest.get("document"), dict) else {},
            )
            section["capture_path"] = str(section_path)
            section["file_size_bytes"] = section_path.stat().st_size
            captured += 1
        except Exception as exc:
            section["capture_path"] = None
            section["capture_error"] = f"{type(exc).__name__}:{exc}"
            errors.append(f"{section.get('id')}_capture_failed:{type(exc).__name__}:{exc}")

    manifest["captured_section_count"] = captured
    manifest["capture_status"] = (
        "complete"
        if captured == manifest.get("section_count") and manifest.get("full_page_screenshot_path")
        else "partial"
        if captured or manifest.get("full_page_screenshot_path")
        else "failed"
    )
    if errors:
        manifest["capture_errors"] = errors
        manifest["limitations"] = [*(manifest.get("limitations") or []), "one_or_more_structural_captures_failed"]
    atlas_manifest: dict[str, Any] = {}
    atlas_path = _derived_path(base_path, "analysis-atlas").with_suffix(".jpg")
    try:
        atlas_manifest = build_analysis_atlas(
            viewport_screenshot_path=viewport_screenshot_path or base_path,
            section_manifest=manifest,
            output_path=atlas_path,
        )
        analysis_atlas_status = (
            "complete"
            if int(atlas_manifest.get("section_panel_count") or 0) > 0
            else "partial"
        )
    except Exception as exc:
        analysis_atlas_status = "failed"
        errors.append(f"analysis_atlas_failed:{type(exc).__name__}:{exc}")
    return {
        "full_page_screenshot_path": manifest.get("full_page_screenshot_path"),
        "section_manifest": manifest,
        "section_captures": list(manifest.get("sections") or []),
        "section_capture_status": manifest.get("capture_status"),
        "lazy_content_hydration": hydration,
        "post_hydration_obstruction_check": post_hydration,
        "analysis_atlas_path": str(atlas_path) if atlas_manifest else None,
        "analysis_atlas_status": analysis_atlas_status,
        "analysis_atlas_manifest": atlas_manifest,
        "structured_capture_errors": errors,
    }


def _crop_section_from_master(
    *,
    full_page_path: Path,
    section_path: Path,
    bbox: dict[str, Any],
    viewport: dict[str, Any],
    document: dict[str, Any],
) -> None:
    from PIL import Image

    with Image.open(full_page_path) as master:
        viewport_width = max(1, _int(viewport.get("width"), master.width))
        document_height = max(1, _int(document.get("height"), master.height))
        scale_x = master.width / viewport_width
        scale_y = master.height / document_height
        left = max(0, min(master.width - 1, math.floor(_int(bbox.get("left"), 0) * scale_x)))
        top = max(0, min(master.height - 1, math.floor(_int(bbox.get("top"), 0) * scale_y)))
        right = max(
            left + 1,
            min(master.width, math.ceil((_int(bbox.get("left"), 0) + _int(bbox.get("width"), 1)) * scale_x)),
        )
        bottom = max(
            top + 1,
            min(master.height, math.ceil((_int(bbox.get("top"), 0) + _int(bbox.get("height"), 1)) * scale_y)),
        )
        master.crop((left, top, right, bottom)).save(section_path, format="PNG")


def _document_metrics(page: Any) -> dict[str, int]:
    value = page.evaluate(
        """() => ({
          document_height: Math.max(
            window.innerHeight || 0,
            document.documentElement.scrollHeight || 0,
            document.body ? document.body.scrollHeight : 0
          ),
          viewport_height: Math.max(1, window.innerHeight || document.documentElement.clientHeight || 1)
        })"""
    )
    return value if isinstance(value, dict) else {}


def _normalized_anchors(
    candidates: Any,
    *,
    viewport_width: int,
    document_height: int,
    min_section_height: int,
) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    for raw in candidates if isinstance(candidates, list) else []:
        if not isinstance(raw, dict):
            continue
        top = max(0, min(document_height - 1, _int(raw.get("top"), 0)))
        width = max(0, _int(raw.get("width"), 0))
        height = max(0, _int(raw.get("height"), 0))
        if width < viewport_width * 0.42 or height < 36:
            continue
        candidate = dict(raw)
        candidate["top"] = top
        candidate["height"] = height
        candidate["priority"] = _int(raw.get("priority"), 0)
        valid.append(candidate)

    valid.sort(key=lambda item: (_int(item.get("top"), 0), -_int(item.get("priority"), 0), -_int(item.get("height"), 0)))
    deduplicated: list[dict[str, Any]] = []
    for candidate in valid:
        if deduplicated and abs(_int(candidate.get("top"), 0) - _int(deduplicated[-1].get("top"), 0)) <= 64:
            if _anchor_score(candidate) > _anchor_score(deduplicated[-1]):
                deduplicated[-1] = candidate
            continue
        deduplicated.append(candidate)

    if len(deduplicated) <= 1:
        return deduplicated
    compact: list[dict[str, Any]] = []
    for index, candidate in enumerate(deduplicated):
        if index + 1 < len(deduplicated):
            distance = _int(deduplicated[index + 1].get("top"), 0) - _int(candidate.get("top"), 0)
            if distance < min_section_height and str(candidate.get("kind") or "") not in {"header", "footer", "navigation"}:
                continue
        compact.append(candidate)
    return compact


def _anchor_score(candidate: dict[str, Any]) -> tuple[int, int, int]:
    landmark = 1 if str(candidate.get("kind") or "") in {"header", "footer", "hero", "navigation"} else 0
    named = 1 if any(str(candidate.get(key) or "").strip() for key in ("heading", "aria_label", "data_section", "data_component")) else 0
    return (_int(candidate.get("priority"), 0), landmark, named)


def _section_label(anchor: dict[str, Any], *, index: int, part_index: int, part_count: int) -> str:
    base = next(
        (
            str(anchor.get(key) or "").strip()
            for key in ("heading", "aria_label", "data_section", "data_component", "kind")
            if str(anchor.get(key) or "").strip()
        ),
        f"section {index + 1}",
    )
    base = re.sub(r"\s+", " ", base)[:80]
    if part_count > 1:
        return f"{base} — part {part_index + 1}"
    return base


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "content")[:54].strip("-")


def _derived_path(path: Path, suffix: str) -> Path:
    safe_suffix = _slug(suffix)
    return path.with_name(f"{path.stem}.{safe_suffix}{path.suffix or '.png'}")


def _int(value: Any, default: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default
