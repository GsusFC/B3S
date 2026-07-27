"""Deterministic vision analysis for rendered page sections."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from typing import Any

from src.visual_signature.vision.composition import analyze_composition
from src.visual_signature.vision.confidence import calculate_vision_confidence
from src.visual_signature.vision.palette_from_screenshot import extract_palette_from_screenshot
from src.visual_signature.vision.screenshot_quality import screenshot_evidence_for_path


SECTION_ANALYSIS_VERSION = "visual-section-analysis-v1"


def analyze_page_sections(
    section_manifest: dict[str, Any] | None,
    *,
    page_url: str = "",
) -> dict[str, Any]:
    """Analyze every persisted section crop and aggregate sections equally."""

    manifest = section_manifest if isinstance(section_manifest, dict) else {}
    raw_sections = manifest.get("sections") if isinstance(manifest.get("sections"), list) else []
    analyzed_rows: list[dict[str, Any]] = []
    usable_rows: list[dict[str, Any]] = []

    for raw in raw_sections:
        if not isinstance(raw, dict):
            continue
        capture_path = str(raw.get("capture_path") or "").strip()
        screenshot, image = screenshot_evidence_for_path(
            capture_path or None,
            screenshot_payload={
                "capture_type": "full_page",
                "page_url": page_url or manifest.get("page_url"),
                "viewport_width": (raw.get("bbox") or {}).get("width")
                if isinstance(raw.get("bbox"), dict)
                else None,
                "viewport_height": (raw.get("bbox") or {}).get("height")
                if isinstance(raw.get("bbox"), dict)
                else None,
            },
        )
        palette = extract_palette_from_screenshot(image)
        composition = analyze_composition(image)
        confidence = calculate_vision_confidence(
            screenshot=screenshot,
            palette=palette,
            composition=composition,
        )
        row = {
            "id": str(raw.get("id") or ""),
            "index": raw.get("index"),
            "label": str(raw.get("label") or ""),
            "kind": str(raw.get("kind") or "section"),
            "heading": str(raw.get("heading") or ""),
            "source": str(raw.get("source") or ""),
            "bbox": dict(raw.get("bbox") or {}) if isinstance(raw.get("bbox"), dict) else {},
            "capture_path": capture_path or None,
            "capture_variant": str(raw.get("capture_variant") or manifest.get("capture_variant") or ""),
            "screenshot": asdict(screenshot),
            "palette": asdict(palette),
            "composition": asdict(composition),
            "confidence": asdict(confidence),
        }
        analyzed_rows.append(row)
        if screenshot.available and screenshot.quality in {"usable", "low_detail"}:
            usable_rows.append(row)

    limitations = [str(item) for item in manifest.get("limitations") or []]
    missing_count = max(0, len(raw_sections) - len(usable_rows))
    if not raw_sections:
        limitations.append("section_manifest_missing_or_empty")
    elif missing_count:
        limitations.append("one_or_more_sections_not_usable_for_vision")
    if manifest.get("truncated") is True:
        limitations.append("section_manifest_truncated")

    if not raw_sections:
        status = "missing"
    elif usable_rows and len(usable_rows) == len(raw_sections):
        status = "available"
    elif usable_rows:
        status = "partial"
    else:
        status = "unavailable"

    return {
        "schema_version": SECTION_ANALYSIS_VERSION,
        "status": status,
        "manifest_version": str(manifest.get("schema_version") or ""),
        "capture_variant": str(manifest.get("capture_variant") or ""),
        "detection_strategy": str(manifest.get("detection_strategy") or ""),
        "full_page_screenshot_path": manifest.get("full_page_screenshot_path"),
        "document": dict(manifest.get("document") or {}) if isinstance(manifest.get("document"), dict) else {},
        "coverage_ratio": manifest.get("coverage_ratio"),
        "section_count": len(raw_sections),
        "captured_section_count": sum(1 for row in analyzed_rows if row["screenshot"].get("available")),
        "analyzed_section_count": len(usable_rows),
        "sections": analyzed_rows,
        "aggregate": _aggregate_sections(usable_rows),
        "limitations": list(dict.fromkeys(limitations)),
    }


def _aggregate_sections(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "aggregation_method": "equal_weight_per_section",
            "dominant_colors": [],
            "visual_density_distribution": {},
            "composition_distribution": {},
            "quality_distribution": {},
            "mean_whitespace_ratio": None,
            "mean_edge_density": None,
            "mean_color_variance": None,
            "mean_confidence": None,
            "cross_section_consistency_proxy": None,
        }

    palette_ratio_sums: dict[str, float] = defaultdict(float)
    palette_presence: Counter[str] = Counter()
    density: Counter[str] = Counter()
    composition_classes: Counter[str] = Counter()
    quality: Counter[str] = Counter()
    whitespace: list[float] = []
    edges: list[float] = []
    variances: list[float] = []
    confidence_scores: list[float] = []

    for row in rows:
        palette = row.get("palette") if isinstance(row.get("palette"), dict) else {}
        seen_colors: set[str] = set()
        for color in palette.get("dominant_colors") or []:
            if not isinstance(color, dict):
                continue
            hex_color = str(color.get("hex") or "").lower()
            if not hex_color:
                continue
            palette_ratio_sums[hex_color] += _float(color.get("ratio"))
            seen_colors.add(hex_color)
        palette_presence.update(seen_colors)

        composition = row.get("composition") if isinstance(row.get("composition"), dict) else {}
        density.update([str(composition.get("visual_density") or "unknown")])
        composition_classes.update([str(composition.get("composition_classification") or "unknown")])
        screenshot = row.get("screenshot") if isinstance(row.get("screenshot"), dict) else {}
        quality.update([str(screenshot.get("quality") or "unknown")])
        _append_number(whitespace, composition.get("whitespace_ratio"))
        _append_number(edges, composition.get("edge_density"))
        _append_number(variances, composition.get("color_variance"))
        confidence = row.get("confidence") if isinstance(row.get("confidence"), dict) else {}
        _append_number(confidence_scores, confidence.get("score"))

    count = len(rows)
    dominant_colors = [
        {
            "hex": color,
            "mean_ratio": round(total / count, 4),
            "section_presence_ratio": round(palette_presence[color] / count, 4),
        }
        for color, total in sorted(
            palette_ratio_sums.items(),
            key=lambda item: (-item[1], item[0]),
        )[:8]
    ]
    global_colors = {item["hex"] for item in dominant_colors}
    palette_reuse_scores: list[float] = []
    for row in rows:
        palette = row.get("palette") if isinstance(row.get("palette"), dict) else {}
        local_colors = {
            str(item.get("hex") or "").lower()
            for item in (palette.get("dominant_colors") or [])[:4]
            if isinstance(item, dict) and item.get("hex")
        }
        if local_colors:
            palette_reuse_scores.append(len(local_colors & global_colors) / len(local_colors))
    palette_reuse = _mean(palette_reuse_scores)
    density_consistency = max(density.values()) / count if density else 0.0
    consistency = round(palette_reuse * 0.6 + density_consistency * 0.4, 4)

    return {
        "aggregation_method": "equal_weight_per_section",
        "dominant_colors": dominant_colors,
        "visual_density_distribution": dict(sorted(density.items())),
        "composition_distribution": dict(sorted(composition_classes.items())),
        "quality_distribution": dict(sorted(quality.items())),
        "mean_whitespace_ratio": _rounded_mean(whitespace),
        "mean_edge_density": _rounded_mean(edges),
        "mean_color_variance": _rounded_mean(variances),
        "mean_confidence": _rounded_mean(confidence_scores),
        "cross_section_consistency_proxy": {
            "score": consistency,
            "palette_reuse": round(palette_reuse, 4),
            "density_consistency": round(density_consistency, 4),
            "method": "heuristic_equal_weight_palette_reuse_and_density_consistency",
        },
    }


def _append_number(target: list[float], value: Any) -> None:
    try:
        if value is not None:
            target.append(float(value))
    except (TypeError, ValueError):
        return


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _rounded_mean(values: list[float]) -> float | None:
    return round(_mean(values), 4) if values else None
