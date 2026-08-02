from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

from src.visual_signature.capture.page_sections import (
    build_page_segment_plan,
    build_section_manifest,
    capture_structured_page_evidence,
    hydrate_lazy_content,
    select_page_segments_for_capture,
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


def _snapshot(
    *,
    height: int,
    candidates: list[dict],
    width: int = 1440,
    viewport_height: int = 900,
) -> dict:
    return {
        "viewport": {"width": width, "height": viewport_height},
        "document": {"width": width, "height": height},
        "candidates": candidates,
    }


def _png_bytes(width: int, height: int, pixels: list[tuple[int, int, int]]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend(pixels[y * width + x])
        rows.append(bytes(row))
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(b"".join(rows))),
            chunk(b"IEND", b""),
        ]
    )


def _write_png(path: Path, width: int, height: int, pixels: list[tuple[int, int, int]]) -> None:
    path.write_bytes(_png_bytes(width, height, pixels))


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


def test_page_segment_plan_groups_html_sections_and_covers_entire_document():
    manifest = build_section_manifest(
        _snapshot(
            height=5200,
            candidates=[
                _candidate(top=0, height=700, kind="hero", heading="Promise"),
                _candidate(top=700, height=950, heading="Product"),
                _candidate(top=1650, height=1200, heading="Proof"),
                _candidate(top=2850, height=900, heading="Stories"),
                _candidate(top=3750, height=1450, kind="footer", heading="Footer"),
            ],
        )
    )

    segments = build_page_segment_plan(manifest, max_segment_height=1800)

    assert segments[0]["bbox"] == {
        "left": 0,
        "top": 0,
        "width": 1440,
        "height": 1650,
    }
    assert all(segment["bbox"]["height"] <= 1800 for segment in segments)
    assert sum(segment["bbox"]["height"] for segment in segments) == 5200
    assert [segment["bbox"]["top"] for segment in segments] == [
        sum(previous["bbox"]["height"] for previous in segments[:index]) for index, segment in enumerate(segments)
    ]
    assert all(segment["semantic_section_ids"] for segment in segments)
    assert all(segment["source"] == "html_section_boundaries" for segment in segments)


def test_page_without_reliable_structure_falls_back_to_viewport_bands():
    manifest = build_section_manifest(_snapshot(height=2500, candidates=[]))

    assert manifest["detection_strategy"] == "geometric_viewport_fallback"
    assert [row["bbox"]["height"] for row in manifest["sections"]] == [900, 900, 700]
    assert "no_reliable_semantic_or_heading_boundaries_detected" in manifest["limitations"]
    segments = build_page_segment_plan(manifest)
    assert sum(row["bbox"]["height"] for row in segments) == 2500
    assert all(row["source"] == "geometric_height_fallback" for row in segments)


def test_segment_selector_keeps_first_viewport_and_strategic_sections():
    manifest = {
        "viewport": {"height": 900},
        "sections": [
            {"id": "hero", "kind": "hero", "label": "Promise"},
            {"id": "product", "kind": "section", "label": "Product"},
            {"id": "proof", "kind": "section", "label": "Customer proof"},
            {"id": "other", "kind": "section", "label": "Company history"},
            {"id": "footer", "kind": "footer", "label": "Contact"},
        ],
    }
    segments = [
        {
            "id": f"segment-{index}",
            "index": index,
            "bbox": {"top": index * 1800, "height": 1800},
            "semantic_section_ids": [section_id],
        }
        for index, section_id in enumerate(("hero", "product", "proof", "other", "footer"))
    ]

    selected = select_page_segments_for_capture(manifest, segments, max_segments=3)

    assert [segment["index"] for segment in selected] == [0, 2, 4]


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


class _FakeCDPSession:
    def __init__(self, *, fail_on_call: int | None = None):
        self.capture_calls: list[dict] = []
        self.detached = False
        self.fail_on_call = fail_on_call

    def send(self, method: str, params: dict):
        assert method == "Page.captureScreenshot"
        self.capture_calls.append(params)
        if self.fail_on_call == len(self.capture_calls):
            raise RuntimeError("synthetic_segment_failure")
        clip = params["clip"]
        width = int(clip["width"])
        height = int(clip["height"])
        raw = _png_bytes(
            width,
            height,
            _pattern(width, height, (35 + len(self.capture_calls) * 15, 60, 85)),
        )
        return {"data": base64.b64encode(raw).decode("ascii")}

    def detach(self):
        self.detached = True


class _FakeCDPContext:
    def __init__(self, session: _FakeCDPSession):
        self.session = session

    def new_cdp_session(self, _page):
        return self.session


class _SegmentedCapturePage(_CapturePage):
    def __init__(self, snapshot: dict, *, fail_on_call: int | None = None):
        super().__init__(snapshot)
        self.cdp_session = _FakeCDPSession(fail_on_call=fail_on_call)
        self.context = _FakeCDPContext(self.cdp_session)


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
    assert all(row["capture_variant"] == "post_hydration_clean_attempt" for row in manifest["sections"])
    assert result["post_hydration_obstruction_check"]["successful"] is True
    assert result["analysis_atlas_status"] == "complete"
    assert Path(result["analysis_atlas_path"]).is_file()
    assert result["analysis_atlas_manifest"]["panel_count"] == 3
    assert result["analysis_atlas_manifest"]["panels"][0]["role"] == "first_viewport"


def test_structured_capture_uses_html_grouped_segments_and_reconstructs_master(tmp_path):
    page = _SegmentedCapturePage(
        _snapshot(
            height=2300,
            width=12,
            candidates=[
                _candidate(top=0, height=900, kind="hero", heading="Promise"),
                _candidate(top=900, height=900, heading="Proof"),
                _candidate(top=1800, height=500, kind="footer", heading="Footer"),
            ],
        )
    )
    viewport = tmp_path / "raw.png"
    _write_png(viewport, 12, 9, _pattern(12, 9, (20, 30, 40)))

    result = capture_structured_page_evidence(
        page,
        screenshot_path=viewport,
        page_url="https://example.test",
        capture_variant="raw_viewport",
        viewport_screenshot_path=viewport,
    )

    manifest = result["section_manifest"]
    segment_capture = manifest["page_segment_capture"]
    assert result["section_capture_status"] == "complete"
    assert segment_capture["status"] == "complete"
    assert segment_capture["strategy"] == "html_boundaries_grouped_into_bounded_chromium_segments"
    assert segment_capture["coverage_ratio"] == 1.0
    assert segment_capture["segment_count"] == 2
    assert page.screenshots == []
    assert len(page.cdp_session.capture_calls) == 2
    assert page.cdp_session.detached is True
    assert all(Path(row["capture_path"]).is_file() for row in manifest["page_segments"])
    assert all(Path(row["capture_path"]).is_file() for row in manifest["sections"])
    assert result["analysis_atlas_manifest"]["page_segment_panel_count"] == 2
    assert all(panel["role"] == "page_segment" for panel in result["analysis_atlas_manifest"]["panels"][1:])

    from PIL import Image

    with Image.open(result["full_page_screenshot_path"]) as master:
        assert master.size == (12, 2300)


def test_segment_failure_preserves_partial_atlas_without_claiming_full_page(tmp_path):
    page = _SegmentedCapturePage(
        _snapshot(
            height=2300,
            width=12,
            candidates=[
                _candidate(top=0, height=900, kind="hero", heading="Promise"),
                _candidate(top=900, height=900, heading="Proof"),
                _candidate(top=1800, height=500, kind="footer", heading="Footer"),
            ],
        ),
        fail_on_call=2,
    )
    viewport = tmp_path / "raw.png"
    _write_png(viewport, 12, 9, _pattern(12, 9, (20, 30, 40)))

    result = capture_structured_page_evidence(
        page,
        screenshot_path=viewport,
        page_url="https://example.test",
        capture_variant="raw_viewport",
        viewport_screenshot_path=viewport,
    )

    manifest = result["section_manifest"]
    segment_capture = manifest["page_segment_capture"]
    assert result["full_page_screenshot_path"] is None
    assert result["section_capture_status"] == "partial"
    assert result["analysis_atlas_status"] == "partial"
    assert segment_capture["status"] == "partial"
    assert segment_capture["captured_segment_count"] == 1
    assert 0 < segment_capture["coverage_ratio"] < 1
    assert Path(manifest["page_segments"][0]["capture_path"]).is_file()
    assert manifest["page_segments"][1]["capture_path"] is None
    assert manifest["captured_section_count"] == 2
    assert all(
        Path(row["capture_path"]).is_file()
        for row in manifest["sections"][:2]
    )
    assert "synthetic_segment_failure" in " ".join(result["structured_capture_errors"])


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
