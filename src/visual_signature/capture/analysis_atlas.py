"""Build a bounded, labeled image atlas for whole-page semantic vision."""

from __future__ import annotations

from pathlib import Path
from typing import Any


ANALYSIS_ATLAS_VERSION = "visual-analysis-atlas-v1"
ATLAS_WIDTH = 1280
ATLAS_MARGIN = 16
ATLAS_LABEL_HEIGHT = 42
ATLAS_SECTION_COLUMNS = 2
ATLAS_MAX_SECTION_PANELS = 16


def build_analysis_atlas(
    *,
    viewport_screenshot_path: str | Path,
    section_manifest: dict[str, Any],
    output_path: str | Path,
) -> dict[str, Any]:
    """Compose first-fold context and readable section panels into one image."""

    from PIL import Image, ImageDraw, ImageFont

    source_panels: list[dict[str, Any]] = [
        {
            "role": "first_viewport",
            "section_id": None,
            "label": "FIRST VIEWPORT",
            "kind": "viewport",
            "source_path": str(viewport_screenshot_path),
        }
    ]
    captured_page_segments = [
        row
        for row in section_manifest.get("page_segments") or []
        if isinstance(row, dict) and str(row.get("capture_path") or "").strip()
    ]
    content_rows = captured_page_segments or [
        row
        for row in section_manifest.get("sections") or []
        if isinstance(row, dict) and str(row.get("capture_path") or "").strip()
    ]
    content_role = "page_segment" if captured_page_segments else "page_section"
    for section in content_rows[:ATLAS_MAX_SECTION_PANELS]:
        if not isinstance(section, dict) or not str(section.get("capture_path") or "").strip():
            continue
        source_panels.append(
            {
                "role": content_role,
                "section_id": str(section.get("id") or ""),
                "label": str(section.get("label") or section.get("id") or "SECTION"),
                "kind": str(section.get("kind") or "section"),
                "source_path": str(section.get("capture_path")),
            }
        )

    prepared: list[dict[str, Any]] = []
    for index, panel in enumerate(source_panels):
        path = Path(panel["source_path"])
        if not path.is_file():
            continue
        with Image.open(path) as source:
            image = source.convert("RGB")
            original_size = {"width": image.width, "height": image.height}
            if panel["role"] == "first_viewport":
                max_width = ATLAS_WIDTH - ATLAS_MARGIN * 2
                max_height = 780
            else:
                max_width = (ATLAS_WIDTH - ATLAS_MARGIN * (ATLAS_SECTION_COLUMNS + 1)) // ATLAS_SECTION_COLUMNS
                max_height = 760
            image.thumbnail(
                (max_width, max_height),
                resample=Image.Resampling.LANCZOS,
            )
            prepared.append(
                {
                    **panel,
                    "source_index": index,
                    "original_size": original_size,
                    "image": image.copy(),
                }
            )

    if not prepared:
        raise ValueError("analysis_atlas_has_no_readable_source_panels")

    viewport_panels = [panel for panel in prepared if panel["role"] == "first_viewport"]
    section_panels = [panel for panel in prepared if panel["role"] != "first_viewport"]
    rows: list[list[dict[str, Any]]] = []
    rows.extend([[panel] for panel in viewport_panels])
    for offset in range(0, len(section_panels), ATLAS_SECTION_COLUMNS):
        rows.append(section_panels[offset : offset + ATLAS_SECTION_COLUMNS])

    row_heights = [ATLAS_LABEL_HEIGHT + max(panel["image"].height for panel in row) for row in rows]
    atlas_height = ATLAS_MARGIN + sum(height + ATLAS_MARGIN for height in row_heights)
    atlas = Image.new("RGB", (ATLAS_WIDTH, atlas_height), color=(241, 242, 244))
    draw = ImageDraw.Draw(atlas)
    try:
        label_font = ImageFont.truetype("DejaVuSans.ttf", 16)
    except OSError:
        label_font = ImageFont.load_default()
    panel_manifest: list[dict[str, Any]] = []
    y = ATLAS_MARGIN
    section_number = 0
    for row, row_height in zip(rows, row_heights):
        full_width = len(row) == 1 and row[0]["role"] == "first_viewport"
        cell_width = (
            ATLAS_WIDTH - ATLAS_MARGIN * 2
            if full_width
            else (ATLAS_WIDTH - ATLAS_MARGIN * (ATLAS_SECTION_COLUMNS + 1)) // ATLAS_SECTION_COLUMNS
        )
        for column, panel in enumerate(row):
            x = ATLAS_MARGIN if full_width else ATLAS_MARGIN + column * (cell_width + ATLAS_MARGIN)
            if panel["role"] == "first_viewport":
                label = "FIRST VIEWPORT - use this panel for first impression"
            elif panel["role"] == "page_segment":
                section_number += 1
                label = f"PAGE SEGMENT {section_number:02d} - {panel['label']} [{panel['kind']}]"
            else:
                section_number += 1
                label = f"SECTION {section_number:02d} - {panel['label']} [{panel['kind']}]"
            label = " ".join(label.split())[:110]
            draw.rectangle(
                (x, y, x + cell_width, y + ATLAS_LABEL_HEIGHT),
                fill=(24, 27, 32),
            )
            draw.text(
                (x + 10, y + 12),
                label,
                fill=(255, 255, 255),
                font=label_font,
            )
            image = panel["image"]
            image_x = x + max(0, (cell_width - image.width) // 2)
            image_y = y + ATLAS_LABEL_HEIGHT
            atlas.paste(image, (image_x, image_y))
            panel_manifest.append(
                {
                    "panel_index": len(panel_manifest),
                    "role": panel["role"],
                    "section_id": panel["section_id"],
                    "label": label,
                    "kind": panel["kind"],
                    "source_path": panel["source_path"],
                    "source_size": panel["original_size"],
                    "atlas_bbox": {
                        "left": image_x,
                        "top": image_y,
                        "width": image.width,
                        "height": image.height,
                    },
                }
            )
        y += row_height + ATLAS_MARGIN

    output = Path(output_path)
    atlas.save(
        output,
        format="JPEG",
        quality=84,
        optimize=True,
        progressive=True,
    )
    return {
        "schema_version": ANALYSIS_ATLAS_VERSION,
        "path": str(output),
        "width": atlas.width,
        "height": atlas.height,
        "format": "jpeg",
        "file_size_bytes": output.stat().st_size,
        "panel_count": len(panel_manifest),
        "section_panel_count": len(section_panels),
        "page_segment_panel_count": sum(1 for panel in panel_manifest if panel["role"] == "page_segment"),
        "page_capture_variant": str(section_manifest.get("capture_variant") or ""),
        "layout": "first_viewport_full_width_then_two_column_sections",
        "panels": panel_manifest,
        "limitations": (["section_panel_limit_reached"] if len(content_rows) > ATLAS_MAX_SECTION_PANELS else []),
    }
