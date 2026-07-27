"""Local screenshot loading and quality heuristics for Vision Enrichment."""

from __future__ import annotations

import os
import struct
import zlib
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from src.visual_signature.vision.types import RasterImage, VisionScreenshotEvidence
from src.visual_signature._internal.utils import int_or_none as _int_or_none


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def resolve_screenshot_path(
    *,
    screenshot_path: str | None = None,
    screenshot_payload: dict[str, Any] | None = None,
    visual_signature_payload: dict[str, Any] | None = None,
) -> str | None:
    """Resolve a local screenshot path without acquiring a new screenshot."""
    candidates = [
        screenshot_path,
        (screenshot_payload or {}).get("path"),
        (screenshot_payload or {}).get("local_path"),
        (screenshot_payload or {}).get("screenshot_path"),
        _local_path_from_file_uri((screenshot_payload or {}).get("screenshot_url")),
        _local_path_from_file_uri((screenshot_payload or {}).get("url")),
        ((visual_signature_payload or {}).get("assets") or {}).get("screenshot_path"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def _local_path_from_file_uri(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    parsed = urlparse(text)
    if parsed.scheme != "file":
        return None
    return unquote(parsed.path)


def resolve_screenshot_metadata(
    *,
    screenshot_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(screenshot_payload, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key in (
        "capture_type",
        "page_url",
        "pageUrl",
        "viewport_width",
        "viewportHeight",
        "viewport_height",
        "viewport_width",
        "source",
        "width",
        "height",
        "file_size_bytes",
        "fileSizeBytes",
        "selected_capture_variant",
        "viewport_obstruction",
        "obstruction_observed_same_capture",
        "dismissal_attempted",
        "dismissal_successful",
        "dismissal_method",
        "dismissal_eligibility",
        "dismissal_block_reason",
        "evidence_integrity_notes",
        "full_page_screenshot_path",
        "section_capture_status",
        "section_manifest",
        "section_captures",
        "lazy_content_hydration",
        "post_hydration_obstruction_check",
        "analysis_atlas_path",
        "analysis_atlas_status",
        "analysis_atlas_manifest",
        "structured_capture_errors",
    ):
        if key in screenshot_payload and screenshot_payload.get(key) not in (None, ""):
            metadata[key] = screenshot_payload.get(key)
    return metadata


def load_raster_image(path: str) -> RasterImage:
    """Load a local screenshot with Pillow when available and stdlib fallback."""
    data = Path(path).read_bytes()
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            rgb = image.convert("RGB")
            return RasterImage(
                width=rgb.width,
                height=rgb.height,
                source_path=path,
                raw_bytes=rgb.tobytes(),
                channels=3,
            )
    except Exception:
        if data.startswith(PNG_SIGNATURE):
            return _load_png(data, source_path=path)
        if data.startswith((b"P6", b"P3")):
            return _load_ppm(data, source_path=path)
        raise ValueError("unsupported image format; expected PNG or PPM")


def screenshot_evidence_for_path(
    path: str | None,
    *,
    screenshot_payload: dict[str, Any] | None = None,
) -> tuple[VisionScreenshotEvidence, RasterImage | None]:
    if not path:
        return (
            VisionScreenshotEvidence(
                available=False,
                quality="missing",
                limitations=["screenshot_path_not_provided"],
            ),
            None,
        )

    if not os.path.exists(path):
        return (
            VisionScreenshotEvidence(
                available=False,
                source="local_file",
                path=path,
                quality="missing",
                limitations=["screenshot_file_not_found"],
            ),
            None,
        )

    file_size = os.path.getsize(path)
    try:
        image = load_raster_image(path)
    except Exception as exc:
        return (
            VisionScreenshotEvidence(
                available=False,
                source="local_file",
                path=path,
                quality="unreadable",
                file_size_bytes=file_size,
                limitations=[f"screenshot_unreadable: {exc}"],
            ),
            None,
        )

    quality, limitations = classify_screenshot_quality(image)
    metadata = resolve_screenshot_metadata(screenshot_payload=screenshot_payload)
    capture_type = str(metadata.get("capture_type") or "unknown").strip() or "unknown"
    viewport_width = _int_or_none(metadata.get("viewport_width") or metadata.get("viewportWidth"))
    viewport_height = _int_or_none(metadata.get("viewport_height") or metadata.get("viewportHeight"))
    page_url = str(metadata.get("page_url") or metadata.get("pageUrl") or "").strip() or None
    return (
        VisionScreenshotEvidence(
            available=True,
            source="local_file",
            path=path,
            capture_type=capture_type if capture_type in {"viewport", "full_page"} else "unknown",
            page_url=page_url,
            width=image.width,
            height=image.height,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            quality=quality,
            file_size_bytes=file_size,
            limitations=limitations,
        ),
        image,
    )


def classify_screenshot_quality(image: RasterImage) -> tuple[str, list[str]]:
    if image.width <= 0 or image.height <= 0 or not image.sample_pixels(1):
        return "unreadable", ["screenshot_has_no_pixels"]

    unique_sample = len(set(image.sample_pixels(5000)))
    if unique_sample <= 1:
        return "blank", ["screenshot_has_single_color"]
    if unique_sample < 8:
        return "low_detail", ["screenshot_has_low_color_detail"]
    return "usable", []


def _load_png(data: bytes, *, source_path: str) -> RasterImage:
    pos = len(PNG_SIGNATURE)
    width = height = None
    color_type = None
    bit_depth = None
    interlace = None
    idat = bytearray()

    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError("truncated png chunk")
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + length]
        pos += 12 + length

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk_data)
        elif chunk_type == b"IDAT":
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    if width is None or height is None or color_type is None or bit_depth is None:
        raise ValueError("png missing ihdr")
    if bit_depth != 8:
        raise ValueError("only 8-bit png screenshots are supported")
    if color_type not in {2, 6}:
        raise ValueError("only rgb/rgba png screenshots are supported")
    if interlace:
        raise ValueError("interlaced png screenshots are not supported")

    channels = 4 if color_type == 6 else 3
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    rows: list[bytearray] = []
    offset = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset:offset + stride])
        offset += stride
        row = _unfilter_png_row(row, previous, filter_type, channels)
        rows.append(row)
        previous = row

    raw = bytearray(width * height * 3)
    out = 0
    for row in rows:
        for idx in range(0, len(row), channels):
            raw[out] = row[idx]
            raw[out + 1] = row[idx + 1]
            raw[out + 2] = row[idx + 2]
            out += 3
    return RasterImage(width=width, height=height, source_path=source_path, raw_bytes=bytes(raw), channels=3)


def _unfilter_png_row(row: bytearray, previous: bytearray, filter_type: int, channels: int) -> bytearray:
    result = bytearray(row)
    for idx, value in enumerate(row):
        left = result[idx - channels] if idx >= channels else 0
        up = previous[idx] if previous else 0
        up_left = previous[idx - channels] if previous and idx >= channels else 0
        if filter_type == 0:
            result[idx] = value
        elif filter_type == 1:
            result[idx] = (value + left) & 0xFF
        elif filter_type == 2:
            result[idx] = (value + up) & 0xFF
        elif filter_type == 3:
            result[idx] = (value + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            result[idx] = (value + _paeth(left, up, up_left)) & 0xFF
        else:
            raise ValueError(f"unsupported png filter {filter_type}")
    return result


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    up_left_distance = abs(estimate - up_left)
    if left_distance <= up_distance and left_distance <= up_left_distance:
        return left
    if up_distance <= up_left_distance:
        return up
    return up_left


def _load_ppm(data: bytes, *, source_path: str) -> RasterImage:
    tokens = _ppm_tokens(data)
    magic = next(tokens)
    width = int(next(tokens))
    height = int(next(tokens))
    max_value = int(next(tokens))
    if max_value <= 0:
        raise ValueError("invalid ppm max value")
    raw = bytearray(width * height * 3)
    out = 0
    if magic == b"P3":
        for _ in range(width * height):
            r = _scale_ppm_value(int(next(tokens)), max_value)
            g = _scale_ppm_value(int(next(tokens)), max_value)
            b = _scale_ppm_value(int(next(tokens)), max_value)
            raw[out] = r
            raw[out + 1] = g
            raw[out + 2] = b
            out += 3
        return RasterImage(width=width, height=height, source_path=source_path, raw_bytes=bytes(raw), channels=3)
    if magic != b"P6":
        raise ValueError("unsupported ppm format")

    header_end = _ppm_binary_header_end(data)
    pixel_data = data[header_end:]
    expected = width * height * 3
    if len(pixel_data) < expected:
        raise ValueError("truncated ppm pixels")
    for idx in range(0, expected, 3):
        raw[out] = _scale_ppm_value(pixel_data[idx], max_value)
        raw[out + 1] = _scale_ppm_value(pixel_data[idx + 1], max_value)
        raw[out + 2] = _scale_ppm_value(pixel_data[idx + 2], max_value)
        out += 3
    return RasterImage(width=width, height=height, source_path=source_path, raw_bytes=bytes(raw), channels=3)


def _ppm_tokens(data: bytes):
    token = bytearray()
    in_comment = False
    for byte in data:
        if in_comment:
            if byte in b"\r\n":
                in_comment = False
            continue
        if byte == ord("#"):
            in_comment = True
            continue
        if byte in b" \t\r\n":
            if token:
                yield bytes(token)
                token.clear()
            continue
        token.append(byte)
    if token:
        yield bytes(token)


def _ppm_binary_header_end(data: bytes) -> int:
    token_count = 0
    in_comment = False
    in_token = False
    for idx, byte in enumerate(data):
        if in_comment:
            if byte in b"\r\n":
                in_comment = False
            continue
        if byte == ord("#"):
            in_comment = True
            continue
        if byte in b" \t\r\n":
            if in_token:
                token_count += 1
                in_token = False
                if token_count == 4:
                    return idx + 1
            continue
        in_token = True
    raise ValueError("ppm header not terminated")


def _scale_ppm_value(value: int, max_value: int) -> int:
    if max_value == 255:
        return value
    return max(0, min(255, round(value * 255 / max_value)))
