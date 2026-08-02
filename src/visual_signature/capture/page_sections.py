"""Rendered-page section discovery and structural screenshot capture.

The detector uses the browser's live DOM and layout geometry. It does not
claim to recover a designer's original intent; it records the structure that
the shipped page exposes after rendering.
"""

from __future__ import annotations

import base64
import io
import math
import re
from pathlib import Path
from typing import Any, Callable

from src.visual_signature.capture.analysis_atlas import build_analysis_atlas


SECTION_MANIFEST_VERSION = "rendered-page-section-manifest-v1"
PAGE_SEGMENT_CAPTURE_VERSION = "html-section-segmented-page-v1"
SECTION_ANALYSIS_MAX_CAPTURES = 16
SECTION_MAX_HEIGHT = 1800
SECTION_MIN_HEIGHT = 120
PAGE_SEGMENT_MAX_HEIGHT = 1800
RUNTIME_MAX_PAGE_SEGMENTS = 5

_SEGMENT_PRIORITY_TERMS = (
    "product",
    "how it works",
    "how it work",
    "proof",
    "customer",
    "case",
    "result",
    "testimonial",
    "pricing",
    "cta",
    "contact",
    "producto",
    "cómo funciona",
    "como funciona",
    "prueba",
    "cliente",
    "caso",
    "resultado",
    "precio",
    "contacto",
)


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


def build_page_segment_plan(
    section_manifest: dict[str, Any],
    *,
    max_segment_height: int = PAGE_SEGMENT_MAX_HEIGHT,
) -> list[dict[str, Any]]:
    """Group adjacent HTML sections into bounded, full-width capture segments."""

    document = section_manifest.get("document") if isinstance(section_manifest.get("document"), dict) else {}
    viewport = section_manifest.get("viewport") if isinstance(section_manifest.get("viewport"), dict) else {}
    document_height = max(1, _int(document.get("height"), 1))
    viewport_width = max(1, _int(viewport.get("width"), 1440))
    max_segment_height = max(1, int(max_segment_height))
    raw_sections = [
        row
        for row in section_manifest.get("sections") or []
        if isinstance(row, dict) and isinstance(row.get("bbox"), dict)
    ]
    html_boundaries_available = str(section_manifest.get("detection_strategy") or "") != "geometric_viewport_fallback"

    semantic_boundaries = {0, document_height}
    for section in raw_sections:
        bbox = section["bbox"]
        top = max(0, min(document_height, _int(bbox.get("top"), 0)))
        bottom = max(
            top,
            min(document_height, top + max(1, _int(bbox.get("height"), 1))),
        )
        semantic_boundaries.update((top, bottom))

    ordered_boundaries = sorted(semantic_boundaries)
    segments: list[dict[str, Any]] = []
    cursor = 0
    while cursor < document_height:
        hard_end = min(document_height, cursor + max_segment_height)
        minimum_group_height = max(1, max_segment_height // 3)
        eligible_boundaries = [
            boundary
            for boundary in ordered_boundaries
            if cursor < boundary <= hard_end
            and (boundary == document_height or boundary - cursor >= minimum_group_height)
        ]
        end = max(eligible_boundaries) if eligible_boundaries else hard_end
        if end <= cursor:
            end = hard_end

        included_sections = []
        included_labels = []
        for section in raw_sections:
            bbox = section["bbox"]
            section_top = max(0, _int(bbox.get("top"), 0))
            section_bottom = section_top + max(1, _int(bbox.get("height"), 1))
            if section_top < end and section_bottom > cursor:
                section_id = str(section.get("id") or "")
                label = str(section.get("label") or "").strip()
                if section_id and section_id not in included_sections:
                    included_sections.append(section_id)
                if label and label not in included_labels:
                    included_labels.append(label)

        if included_labels:
            label = included_labels[0]
            if len(included_labels) > 1:
                label = f"{included_labels[0]} → {included_labels[-1]}"
        else:
            label = f"Page segment {len(segments) + 1}"
        segments.append(
            {
                "id": f"page-segment-{len(segments) + 1:02d}",
                "index": len(segments),
                "label": label,
                "kind": (
                    "html_section_group"
                    if included_sections and html_boundaries_available
                    else "geometric_gap_fallback"
                ),
                "source": (
                    "html_section_boundaries"
                    if end in semantic_boundaries and included_sections and html_boundaries_available
                    else "geometric_height_fallback"
                ),
                "bbox": {
                    "left": 0,
                    "top": cursor,
                    "width": viewport_width,
                    "height": end - cursor,
                },
                "semantic_section_ids": included_sections,
                "capture_path": None,
            }
        )
        cursor = end

    return segments


def select_page_segments_for_capture(
    section_manifest: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    max_segments: int | None = None,
) -> list[dict[str, Any]]:
    """Select bounded, high-value segments while preserving the full plan."""

    if max_segments is None or len(segments) <= max_segments:
        return list(segments)
    limit = max(1, int(max_segments))
    viewport = section_manifest.get("viewport") if isinstance(section_manifest.get("viewport"), dict) else {}
    viewport_height = max(1, _int(viewport.get("height"), 900))
    sections_by_id = {
        str(row.get("id") or ""): row
        for row in section_manifest.get("sections") or []
        if isinstance(row, dict) and str(row.get("id") or "")
    }

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, segment in enumerate(segments):
        bbox = segment.get("bbox") if isinstance(segment.get("bbox"), dict) else {}
        top = _int(bbox.get("top"), 0)
        score = 1000 if top == 0 else 800 if top < viewport_height else 0
        for section_id in segment.get("semantic_section_ids") or []:
            section = sections_by_id.get(str(section_id))
            if not section:
                continue
            kind = str(section.get("kind") or "").lower()
            label = " ".join(
                str(section.get(key) or "")
                for key in ("label", "heading", "aria_label")
            ).lower()
            if kind == "hero":
                score += 500
            elif kind in {"header", "navigation"}:
                score += 100
            elif kind == "footer":
                score += 250
            score += sum(40 for term in _SEGMENT_PRIORITY_TERMS if term in label)
        scored.append((-score, index, segment))

    scored.sort(key=lambda item: (item[0], item[1]))
    selected_indexes = {index for _, index, _ in scored[:limit]}
    return [segment for index, segment in enumerate(segments) if index in selected_indexes]


def _capture_page_segments(
    page: Any,
    *,
    base_path: Path,
    section_manifest: dict[str, Any],
    max_segments: int | None = None,
) -> dict[str, Any]:
    """Capture bounded Chromium slices and reconstruct a master when complete."""

    context = getattr(page, "context", None)
    new_cdp_session = getattr(context, "new_cdp_session", None)
    if not callable(new_cdp_session):
        raise RuntimeError("chromium_cdp_session_unavailable")

    from PIL import Image

    document = section_manifest.get("document") if isinstance(section_manifest.get("document"), dict) else {}
    viewport = section_manifest.get("viewport") if isinstance(section_manifest.get("viewport"), dict) else {}
    document_height = max(1, _int(document.get("height"), 1))
    viewport_width = max(1, _int(viewport.get("width"), 1440))
    segments = build_page_segment_plan(section_manifest)
    selected_segments = select_page_segments_for_capture(
        section_manifest,
        segments,
        max_segments=max_segments,
    )
    selected_segment_ids = {str(segment.get("id") or "") for segment in selected_segments}
    for segment in segments:
        segment["selected_for_capture"] = str(segment.get("id") or "") in selected_segment_ids
    session = new_cdp_session(page)
    reconstruct_master = len(selected_segments) == len(segments)
    master = (
        Image.new("RGB", (viewport_width, document_height), color=(255, 255, 255))
        if reconstruct_master
        else None
    )
    captured_height = 0
    errors: list[str] = []
    try:
        for segment in selected_segments:
            bbox = segment["bbox"]
            segment_path = _derived_path(base_path, str(segment["id"]))
            try:
                payload = session.send(
                    "Page.captureScreenshot",
                    {
                        "format": "png",
                        "fromSurface": True,
                        "captureBeyondViewport": True,
                        "optimizeForSpeed": True,
                        "clip": {
                            "x": int(bbox["left"]),
                            "y": int(bbox["top"]),
                            "width": int(bbox["width"]),
                            "height": int(bbox["height"]),
                            "scale": 1,
                        },
                    },
                )
                encoded = str(payload.get("data") or "") if isinstance(payload, dict) else ""
                raw = base64.b64decode(encoded, validate=True)
                with Image.open(io.BytesIO(raw)) as image:
                    rendered = image.convert("RGB")
                    try:
                        expected_size = (int(bbox["width"]), int(bbox["height"]))
                        if rendered.size != expected_size:
                            raise ValueError(
                                "segment_dimensions_mismatch:"
                                f"expected={expected_size[0]}x{expected_size[1]};"
                                f"actual={rendered.width}x{rendered.height}"
                            )
                        segment_path.write_bytes(raw)
                        if master is not None:
                            master.paste(rendered, (int(bbox["left"]), int(bbox["top"])))
                    finally:
                        rendered.close()
                segment["capture_path"] = str(segment_path)
                segment["file_size_bytes"] = segment_path.stat().st_size
                captured_height += int(bbox["height"])
            except Exception as exc:
                segment["capture_path"] = None
                segment["capture_error"] = f"{type(exc).__name__}:{exc}"
                errors.append(f"{segment['id']}_capture_failed:{type(exc).__name__}:{exc}")
                break
    finally:
        detach = getattr(session, "detach", None)
        if callable(detach):
            try:
                detach()
            except Exception:
                pass

    complete = reconstruct_master and captured_height == document_height and not errors
    full_page_path = _derived_path(base_path, "full-page")
    if complete and master is not None:
        master.save(full_page_path, format="PNG", compress_level=1)
    if master is not None:
        master.close()
    return {
        "schema_version": PAGE_SEGMENT_CAPTURE_VERSION,
        "strategy": "html_boundaries_grouped_into_bounded_chromium_segments",
        "status": "complete" if complete else "partial" if captured_height else "failed",
        "max_segment_height": PAGE_SEGMENT_MAX_HEIGHT,
        "segment_count": len(segments),
        "selected_segment_count": len(selected_segments),
        "max_segment_captures": max_segments,
        "selection_strategy": (
            "priority_first_viewport_and_strategic_labels"
            if max_segments is not None and len(selected_segments) < len(segments)
            else "all_planned_segments"
        ),
        "captured_segment_count": sum(1 for segment in segments if str(segment.get("capture_path") or "").strip()),
        "coverage_ratio": round(min(1.0, captured_height / document_height), 4),
        "full_page_screenshot_path": str(full_page_path) if complete else None,
        "segments": segments,
        "errors": errors,
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
    max_page_segments: int | None = None,
) -> dict[str, Any]:
    """Capture bounded page segments and normalized section bands."""

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
    structural_variant = "post_hydration_clean_attempt" if post_hydration.get("successful") is True else capture_variant
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
        segment_capture = _capture_page_segments(
            page,
            base_path=base_path,
            section_manifest=manifest,
            max_segments=max_page_segments,
        )
        manifest["page_segment_capture"] = {key: value for key, value in segment_capture.items() if key != "segments"}
        manifest["page_segments"] = list(segment_capture.get("segments") or [])
        manifest["full_page_screenshot_path"] = segment_capture.get("full_page_screenshot_path")
        errors.extend(str(item) for item in segment_capture.get("errors") or [])
    except Exception as exc:
        # Test doubles and non-Chromium adapters retain the public Playwright
        # path. Production Chromium captures use the segmented strategy above.
        try:
            page.screenshot(
                path=str(full_page_path),
                full_page=True,
                animations="disabled",
                timeout=12000,
            )
            manifest["full_page_screenshot_path"] = str(full_page_path)
            manifest["page_segment_capture"] = {
                "schema_version": PAGE_SEGMENT_CAPTURE_VERSION,
                "strategy": "playwright_full_page_compatibility_fallback",
                "status": "complete",
                "segment_count": 0,
                "captured_segment_count": 0,
                "coverage_ratio": 1.0,
                "fallback_reason": f"{type(exc).__name__}:{exc}",
                "errors": [],
            }
            manifest["page_segments"] = []
        except Exception as fallback_exc:
            manifest["full_page_screenshot_path"] = None
            manifest["page_segment_capture"] = {
                "schema_version": PAGE_SEGMENT_CAPTURE_VERSION,
                "strategy": "playwright_full_page_compatibility_fallback",
                "status": "failed",
                "segment_count": 0,
                "captured_segment_count": 0,
                "coverage_ratio": 0.0,
                "fallback_reason": f"{type(exc).__name__}:{exc}",
                "errors": [f"{type(fallback_exc).__name__}:{fallback_exc}"],
            }
            manifest["page_segments"] = []
            errors.append(f"full_page_capture_failed:{type(fallback_exc).__name__}:{fallback_exc}")

    captured = 0
    full_page_available = bool(manifest.get("full_page_screenshot_path"))
    master_image = None
    if full_page_available:
        try:
            from PIL import Image

            master_image = Image.open(full_page_path)
        except Exception as exc:
            full_page_available = False
            errors.append(f"full_page_master_open_failed:{type(exc).__name__}:{exc}")
    try:
        for section in manifest.get("sections") or []:
            section_path = _derived_path(
                base_path,
                str(section.get("id") or f"section-{captured + 1:02d}"),
            )
            if not full_page_available or master_image is None:
                if _crop_section_from_captured_segment(
                    section=section,
                    section_manifest=manifest,
                    section_path=section_path,
                ):
                    section["capture_path"] = str(section_path)
                    section["file_size_bytes"] = section_path.stat().st_size
                    section["capture_source"] = "page_segment"
                    captured += 1
                else:
                    section["capture_path"] = None
                    section["capture_error"] = "full_page_master_unavailable"
                continue
            try:
                _crop_section_from_master_image(
                    master=master_image,
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
    finally:
        if master_image is not None:
            master_image.close()

    manifest["captured_section_count"] = captured
    captured_segment_count = int((manifest.get("page_segment_capture") or {}).get("captured_segment_count") or 0)
    manifest["capture_status"] = (
        "complete"
        if captured == manifest.get("section_count") and manifest.get("full_page_screenshot_path")
        else "partial"
        if captured or captured_segment_count or manifest.get("full_page_screenshot_path")
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
            and str((manifest.get("page_segment_capture") or {}).get("status") or "complete") == "complete"
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


def _crop_section_from_master_image(
    *,
    master: Any,
    section_path: Path,
    bbox: dict[str, Any],
    viewport: dict[str, Any],
    document: dict[str, Any],
) -> None:
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
    with master.crop((left, top, right, bottom)) as crop:
        crop.save(section_path, format="PNG")


def _crop_section_from_captured_segment(
    *,
    section: dict[str, Any],
    section_manifest: dict[str, Any],
    section_path: Path,
) -> bool:
    """Persist a section crop when only a bounded segment was captured."""

    section_bbox = section.get("bbox") if isinstance(section.get("bbox"), dict) else {}
    section_top = _int(section_bbox.get("top"), 0)
    section_left = _int(section_bbox.get("left"), 0)
    section_width = max(1, _int(section_bbox.get("width"), 1))
    section_height = max(1, _int(section_bbox.get("height"), 1))
    section_bottom = section_top + section_height
    for segment in section_manifest.get("page_segments") or []:
        if not isinstance(segment, dict):
            continue
        capture_path = str(segment.get("capture_path") or "").strip()
        if not capture_path:
            continue
        segment_bbox = segment.get("bbox") if isinstance(segment.get("bbox"), dict) else {}
        segment_top = _int(segment_bbox.get("top"), 0)
        segment_left = _int(segment_bbox.get("left"), 0)
        segment_width = max(1, _int(segment_bbox.get("width"), 1))
        segment_height = max(1, _int(segment_bbox.get("height"), 1))
        segment_bottom = segment_top + segment_height
        if not (
            section_left >= segment_left
            and section_top >= segment_top
            and section_left + section_width <= segment_left + segment_width
            and section_bottom <= segment_bottom
        ):
            continue
        try:
            from PIL import Image

            with Image.open(capture_path) as image:
                left = section_left - segment_left
                top = section_top - segment_top
                right = min(image.width, left + section_width)
                bottom = min(image.height, top + section_height)
                if right <= left or bottom <= top:
                    return False
                with image.crop((left, top, right, bottom)) as crop:
                    crop.save(section_path, format="PNG")
            return True
        except Exception:
            return False
    return False


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

    valid.sort(
        key=lambda item: (_int(item.get("top"), 0), -_int(item.get("priority"), 0), -_int(item.get("height"), 0))
    )
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
            if distance < min_section_height and str(candidate.get("kind") or "") not in {
                "header",
                "footer",
                "navigation",
            }:
                continue
        compact.append(candidate)
    return compact


def _anchor_score(candidate: dict[str, Any]) -> tuple[int, int, int]:
    landmark = 1 if str(candidate.get("kind") or "") in {"header", "footer", "hero", "navigation"} else 0
    named = (
        1
        if any(
            str(candidate.get(key) or "").strip() for key in ("heading", "aria_label", "data_section", "data_component")
        )
        else 0
    )
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
