from __future__ import annotations

import struct
import zlib
from pathlib import Path

from src.visual_signature.capture.page_sections import (
    build_section_manifest,
    capture_structured_page_evidence,
    hydrate_lazy_content,
)
from src.visual_signature.vision import enrich_visual_signature_with_vision


def _candidate(
    *,
    top: int,
    height: int,
    kind: str = "section",
    heading: str = "",
    source: str = "semantic",
    priority: int = 100,
) -> dict:
    return {
        "top": top,
        "left": 0,
        "width": 1440,
        "height": height,
        "kind": kind,
        "heading": heading,
        "source": source,
        "priority": priority,
        "selector": f"main > section:nth-of-type({top + 1})",
    }


def _snapshot(*, height: int, candidates: list[dict]) -> dict:
    return {
        "viewport": {"width": 1440, "height": 900},
        "document": {"width": 1440, "height": height},
        "candidates": candidates,
    }


def _write_png(path: Path, width: int, height: int, pixels: list[tuple[int, int, int]]) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend(pixels[y * width + x])
        rows.append(bytes(row))
    path.write_bytes(
        b"".join(
            [
                b"\x89PNG\r\n\x1a\n",
                chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
                chunk(b"IDAT", zlib.compress(b"".join(rows))),
                chunk(b"IEND", b""),
            ]
        )
    )


def _pattern(width: int, height: int, base: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    pixels = []
    for y in range(height):
        for x in range(width):
            delta = ((x * 7 + y * 11) % 9) * 8
            pixels.append(tuple(min(255, channel + delta) for channel in base))
    return pixels


def test_section_manifest_uses_semantic_boundaries_and_covers_page():
    manifest = build_section_manifest(
        _snapshot(
            height=2600,
            candidates=[
                _candidate(top=0, height=80, kind="header"),
                _candidate(top=80, height=820, kind="hero", heading="A clear promise"),
                _candidate(top=900, height=1400, heading="Customer proof"),
                _candidate(top=2300, height=300, kind="footer"),
            ],
        ),
        page_url="https://example.test",
    )

    assert manifest["detection_strategy"] == "rendered_dom_semantics_and_headings"
    assert manifest["coverage_ratio"] == 1.0
    assert [row["kind"] for row in manifest["sections"]] == ["header", "hero", "section", "footer"]
    assert manifest["sections"][1]["label"] == "A clear promise"
    assert manifest["sections"][-1]["bbox"]["top"] == 2300


def test_section_manifest_uses_heading_fallback_for_div_soup():
    manifest = build_section_manifest(
        _snapshot(
            height=2100,
            candidates=[
                _candidate(
                    top=0,
                    height=700,
                    kind="hero",
                    heading="The product",
                    source="heading_fallback",
                    priority=75,
                ),
                _candidate(
                    top=700,
                    height=700,
                    heading="How it works",
                    source="heading_fallback",
                    priority=65,
                ),
                _candidate(
                    top=1400,
                    height=700,
                    heading="What customers say",
                    source="heading_fallback",
                    priority=65,
                ),
            ],
        )
    )

    assert manifest["section_count"] == 3
    assert [row["heading"] for row in manifest["sections"]] == [
        "The product",
        "How it works",
        "What customers say",
    ]
    assert all(row["source"] == "heading_fallback" for row in manifest["sections"])


def test_long_section_is_split_into_bounded_analysis_crops():
    manifest = build_section_manifest(
        _snapshot(
            height=7200,
            candidates=[_candidate(top=0, height=7200, heading="Long editorial page")],
        ),
        max_section_height=1800,
    )

    assert manifest["section_count"] == 4
    assert [row["bbox"]["height"] for row in manifest["sections"]] == [1800, 1800, 1800, 1800]
    assert manifest["coverage_ratio"] == 1.0
    assert manifest["sections"][0]["part_count"] == 4


def test_page_without_reliable_structure_falls_back_to_viewport_bands():
    manifest = build_section_manifest(_snapshot(height=2500, candidates=[]))

    assert manifest["detection_strategy"] == "geometric_viewport_fallback"
    assert [row["bbox"]["height"] for row in manifest["sections"]] == [900, 900, 700]
    assert "no_reliable_semantic_or_heading_boundaries_detected" in manifest["limitations"]


class _LazyPage:
    def __init__(self):
        self.document_height = 1800
        self.scroll_positions: list[int] = []

    def evaluate(self, script: str, argument=None):
        if "(y) =>" in script:
            self.scroll_positions.append(int(argument))
            if int(argument) >= 738:
                self.document_height = 2700
            return None
        if "window.scrollTo(0, 0)" in script:
            self.scroll_positions.append(0)
            return None
        return {"document_height": self.document_height, "viewport_height": 900}

    def wait_for_timeout(self, _wait_ms: int):
        return None


def test_lazy_hydration_remeasures_growing_document_and_returns_to_top():
    page = _LazyPage()

    result = hydrate_lazy_content(page, max_steps=8, wait_ms=0)

    assert result["initial_document_height"] == 1800
    assert result["final_document_height"] == 2700
    assert result["reached_end"] is True
    assert page.scroll_positions[-1] == 0


class _CapturePage(_LazyPage):
    def __init__(self, snapshot: dict):
        super().__init__()
        self.document_height = int(snapshot["document"]["height"])
        self.snapshot = snapshot
        self.screenshots: list[dict] = []

    def evaluate(self, script: str, argument=None):
        if "const candidates = []" in script:
            return self.snapshot
        return super().evaluate(script, argument)

    def screenshot(self, **kwargs):
        path = Path(kwargs["path"])
        _write_png(path, 12, 18, _pattern(12, 18, (40, 60, 80)))
        self.screenshots.append(kwargs)


def test_structured_capture_persists_master_and_section_files(tmp_path):
    page = _CapturePage(
        _snapshot(
            height=1800,
            candidates=[
                _candidate(top=0, height=900, kind="hero", heading="Promise"),
                _candidate(top=900, height=900, heading="Proof"),
            ],
        )
    )

    viewport = tmp_path / "raw.png"
    _write_png(viewport, 12, 9, _pattern(12, 9, (20, 30, 40)))
    result = capture_structured_page_evidence(
        page,
        screenshot_path=viewport,
        page_url="https://example.test",
        capture_variant="clean_attempt",
        viewport_screenshot_path=viewport,
        post_hydration_hook=lambda _page: {
            "checked": True,
            "attempted": True,
            "successful": True,
        },
    )

    manifest = result["section_manifest"]
    assert result["section_capture_status"] == "complete"
    assert Path(result["full_page_screenshot_path"]).is_file()
    assert manifest["captured_section_count"] == 2
    assert all(Path(row["capture_path"]).is_file() for row in manifest["sections"])
    assert all(
        row["capture_variant"] == "post_hydration_clean_attempt"
        for row in manifest["sections"]
    )
    assert result["post_hydration_obstruction_check"]["successful"] is True
    assert result["analysis_atlas_status"] == "complete"
    assert Path(result["analysis_atlas_path"]).is_file()
    assert result["analysis_atlas_manifest"]["panel_count"] == 3
    assert result["analysis_atlas_manifest"]["panels"][0]["role"] == "first_viewport"


def test_vision_analyzes_sections_and_aggregates_each_section_equally(tmp_path):
    primary = tmp_path / "primary.png"
    red = tmp_path / "section-red.png"
    blue = tmp_path / "section-blue.png"
    _write_png(primary, 30, 20, _pattern(30, 20, (30, 30, 30)))
    _write_png(red, 30, 20, _pattern(30, 20, (120, 10, 10)))
    _write_png(blue, 30, 80, _pattern(30, 80, (10, 10, 120)))
    manifest = {
        "schema_version": "rendered-page-section-manifest-v1",
        "page_url": "https://example.test",
        "capture_variant": "raw_viewport",
        "detection_strategy": "rendered_dom_semantics_and_headings",
        "document": {"width": 30, "height": 100},
        "coverage_ratio": 1.0,
        "sections": [
            {
                "id": "section-01-red",
                "index": 0,
                "label": "Red",
                "kind": "hero",
                "bbox": {"left": 0, "top": 0, "width": 30, "height": 20},
                "capture_path": str(red),
            },
            {
                "id": "section-02-blue",
                "index": 1,
                "label": "Blue",
                "kind": "section",
                "bbox": {"left": 0, "top": 20, "width": 30, "height": 80},
                "capture_path": str(blue),
            },
        ],
    }

    enriched = enrich_visual_signature_with_vision(
        visual_signature_payload={
            "brand_name": "Sections",
            "website_url": "https://example.test",
        },
        screenshot_path=str(primary),
        screenshot_payload={
            "capture_type": "viewport",
            "viewport_width": 30,
            "viewport_height": 20,
            "page_url": "https://example.test",
            "section_manifest": manifest,
        },
    )

    analysis = enriched["vision"]["section_analysis"]
    assert analysis["status"] == "available"
    assert analysis["analyzed_section_count"] == 2
    assert analysis["aggregate"]["aggregation_method"] == "equal_weight_per_section"
    assert analysis["sections"][0]["screenshot"]["height"] == 20
    assert analysis["sections"][1]["screenshot"]["height"] == 80
    assert analysis["aggregate"]["dominant_colors"]
