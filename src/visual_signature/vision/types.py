"""Types for local Visual Signature vision enrichment."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


ScreenshotQuality = Literal["missing", "unreadable", "blank", "low_detail", "usable"]
CaptureType = Literal["viewport", "full_page", "unknown"]
VisualDensity = Literal["sparse", "balanced", "dense", "unknown"]
CompositionClass = Literal["blank", "sparse_single_focus", "balanced_blocks", "dense_grid", "unknown"]
ConfidenceLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class RasterImage:
    width: int
    height: int
    source_path: str
    pixels: list[tuple[int, int, int]] | None = None
    raw_bytes: bytes | None = None
    channels: int = 3

    def sample_pixels(self, limit: int) -> list[tuple[int, int, int]]:
        if limit <= 0:
            return []
        if self.raw_bytes is not None:
            return _sample_pixels_from_bytes(
                self.raw_bytes,
                width=self.width,
                height=self.height,
                channels=self.channels,
                limit=limit,
            )
        if not self.pixels:
            return []
        return _sample_pixels_from_tuples(self.pixels, limit=limit)

    def sample_grid(
        self,
        *,
        max_width: int,
        max_height: int,
        left: int = 0,
        top: int = 0,
        right: int | None = None,
        bottom: int | None = None,
    ) -> list[tuple[int, int, int]]:
        right = self.width if right is None else right
        bottom = self.height if bottom is None else bottom
        left = max(0, min(self.width, left))
        top = max(0, min(self.height, top))
        right = max(left, min(self.width, right))
        bottom = max(top, min(self.height, bottom))
        if left >= right or top >= bottom:
            return []
        x_step = max(1, (right - left) // max_width)
        y_step = max(1, (bottom - top) // max_height)
        if self.raw_bytes is not None:
            return _sample_grid_from_bytes(
                self.raw_bytes,
                width=self.width,
                channels=self.channels,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                x_step=x_step,
                y_step=y_step,
            )
        if not self.pixels:
            return []
        return _sample_grid_from_tuples(
            self.pixels,
            width=self.width,
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            x_step=x_step,
            y_step=y_step,
        )

    def row_pixels(self, y: int, *, left: int = 0, right: int | None = None, step: int = 1) -> list[tuple[int, int, int]]:
        if self.height <= 0:
            return []
        y = max(0, min(self.height - 1, y))
        right = self.width if right is None else right
        left = max(0, min(self.width, left))
        right = max(left, min(self.width, right))
        step = max(1, step)
        if left >= right:
            return []
        if self.raw_bytes is not None:
            return _row_pixels_from_bytes(
                self.raw_bytes,
                width=self.width,
                channels=self.channels,
                y=y,
                left=left,
                right=right,
                step=step,
            )
        if not self.pixels:
            return []
        row = self.pixels[y * self.width:(y + 1) * self.width]
        return row[left:right:step]

    def crop(
        self,
        *,
        left: int = 0,
        top: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> "RasterImage":
        width = self.width - left if width is None else width
        height = self.height - top if height is None else height
        left = max(0, min(self.width, left))
        top = max(0, min(self.height, top))
        width = max(0, min(self.width - left, width))
        height = max(0, min(self.height - top, height))
        if width == self.width and height == self.height and left == 0 and top == 0:
            return self
        if self.raw_bytes is not None:
            return RasterImage(
                width=width,
                height=height,
                source_path=self.source_path,
                raw_bytes=_crop_bytes(
                    self.raw_bytes,
                    width=self.width,
                    channels=self.channels,
                    left=left,
                    top=top,
                    crop_width=width,
                    crop_height=height,
                ),
                channels=self.channels,
            )
        if self.pixels:
            cropped = []
            for y in range(top, top + height):
                offset = y * self.width
                cropped.extend(self.pixels[offset + left:offset + left + width])
            return RasterImage(
                width=width,
                height=height,
                source_path=self.source_path,
                pixels=cropped,
                channels=self.channels,
            )
        return RasterImage(width=width, height=height, source_path=self.source_path, channels=self.channels)


@dataclass
class VisionScreenshotEvidence:
    available: bool
    source: str = "none"
    path: str | None = None
    capture_type: CaptureType = "unknown"
    page_url: str | None = None
    width: int | None = None
    height: int | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    quality: ScreenshotQuality = "missing"
    file_size_bytes: int | None = None
    limitations: list[str] = field(default_factory=list)


@dataclass
class VisionColor:
    hex: str
    occurrences: int
    ratio: float


@dataclass
class VisionPaletteEvidence:
    dominant_colors: list[VisionColor] = field(default_factory=list)
    color_count: int = 0
    confidence: float = 0.0


@dataclass
class VisionCompositionEvidence:
    whitespace_ratio: float | None = None
    visual_density: VisualDensity = "unknown"
    composition_classification: CompositionClass = "unknown"
    edge_density: float | None = None
    color_variance: float | None = None
    confidence: float = 0.0


@dataclass
class VisionConfidence:
    score: float
    level: ConfidenceLevel
    factors: dict[str, float]
    limitations: list[str] = field(default_factory=list)


@dataclass
class VisionEvidence:
    screenshot: VisionScreenshotEvidence
    screenshot_palette: VisionPaletteEvidence
    composition: VisionCompositionEvidence
    vision_confidence: VisionConfidence
    agreement: dict[str, object] | None = None
    viewport_palette: VisionPaletteEvidence | None = None
    viewport_whitespace_ratio: float | None = None
    viewport_visual_density: VisualDensity = "unknown"
    viewport_composition: VisionCompositionEvidence | None = None
    viewport_confidence: VisionConfidence | None = None
    viewport_obstruction: dict[str, object] | None = None
    section_analysis: dict[str, object] | None = None
    version: Literal["vision-enrichment-mvp-1"] = "vision-enrichment-mvp-1"

    def to_dict(self) -> dict:
        return asdict(self)


def _sample_pixels_from_tuples(pixels: list[tuple[int, int, int]], *, limit: int) -> list[tuple[int, int, int]]:
    if len(pixels) <= limit:
        return pixels
    step = max(1, len(pixels) // limit)
    return pixels[::step][:limit]


def _sample_pixels_from_bytes(raw_bytes: bytes, *, width: int, height: int, channels: int, limit: int) -> list[tuple[int, int, int]]:
    total = width * height
    if total <= 0:
        return []
    if total <= limit:
        return [
            _pixel_from_bytes(raw_bytes, index=index, channels=channels)
            for index in range(total)
        ]
    step = max(1, total // limit)
    sampled: list[tuple[int, int, int]] = []
    for index in range(0, total, step):
        sampled.append(_pixel_from_bytes(raw_bytes, index=index, channels=channels))
        if len(sampled) >= limit:
            break
    return sampled


def _sample_grid_from_tuples(
    pixels: list[tuple[int, int, int]],
    *,
    width: int,
    left: int,
    top: int,
    right: int,
    bottom: int,
    x_step: int,
    y_step: int,
) -> list[tuple[int, int, int]]:
    sampled: list[tuple[int, int, int]] = []
    for y in range(top, bottom, y_step):
        row_offset = y * width
        row = pixels[row_offset + left:row_offset + right]
        sampled.extend(row[::x_step])
    return sampled


def _sample_grid_from_bytes(
    raw_bytes: bytes,
    *,
    width: int,
    channels: int,
    left: int,
    top: int,
    right: int,
    bottom: int,
    x_step: int,
    y_step: int,
) -> list[tuple[int, int, int]]:
    sampled: list[tuple[int, int, int]] = []
    for y in range(top, bottom, y_step):
        row_offset = (y * width + left) * channels
        row_width = right - left
        for x in range(0, row_width, x_step):
            sampled.append(_pixel_from_bytes(raw_bytes, offset=row_offset + x * channels, channels=channels))
    return sampled


def _row_pixels_from_bytes(
    raw_bytes: bytes,
    *,
    width: int,
    channels: int,
    y: int,
    left: int,
    right: int,
    step: int,
) -> list[tuple[int, int, int]]:
    row_offset = (y * width + left) * channels
    row_width = right - left
    sampled: list[tuple[int, int, int]] = []
    for x in range(0, row_width, step):
        sampled.append(_pixel_from_bytes(raw_bytes, offset=row_offset + x * channels, channels=channels))
    return sampled


def _pixel_from_bytes(raw_bytes: bytes, *, index: int | None = None, offset: int | None = None, channels: int) -> tuple[int, int, int]:
    if offset is None:
        if index is None:
            raise ValueError("index or offset required")
        offset = index * channels
    return (raw_bytes[offset], raw_bytes[offset + 1], raw_bytes[offset + 2])


def _crop_bytes(
    raw_bytes: bytes,
    *,
    width: int,
    channels: int,
    left: int,
    top: int,
    crop_width: int,
    crop_height: int,
) -> bytes:
    if crop_width <= 0 or crop_height <= 0:
        return b""
    output = bytearray(crop_width * crop_height * channels)
    out_offset = 0
    row_bytes = crop_width * channels
    for y in range(top, top + crop_height):
        in_offset = (y * width + left) * channels
        output[out_offset:out_offset + row_bytes] = raw_bytes[in_offset:in_offset + row_bytes]
        out_offset += row_bytes
    return bytes(output)
