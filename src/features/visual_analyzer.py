"""
Visual brand consistency analyzer.

Takes screenshots of brand websites using Firecrawl, then analyzes them
with AI vision for:
- Color palette consistency
- Logo presence and placement
- Typography style signals
- Layout and visual identity coherence

Returns visual consistency scores with confidence levels.
"""

import json
import os
import struct
import tempfile
import urllib.request
import urllib.error
import urllib.parse
import base64
import zlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from src.api_key_pool import ApiKeySource, shared_api_key_pool
from src.config import BRAND3_LLM_API_KEY, FIRECRAWL_API_KEYS, LLM_BASE_URL, VISION_MODEL


@dataclass
class VisualAnalysisResult:
    """Result of visual brand analysis from screenshot."""
    screenshot_url: str = ""
    screenshot_path: str = ""
    color_palette_score: float = 0.0
    logo_detected: bool = False
    logo_score: float = 0.0
    typography_score: float = 0.0
    layout_consistency_score: float = 0.0
    overall_score: float = 0.0
    confidence: float = 0.0
    details: dict = field(default_factory=dict)
    error: str = ""


class VisualAnalyzer:
    """Analyzes brand website screenshots for visual consistency."""

    def __init__(self, api_key: ApiKeySource = None, vision_api_key: str = None,
                 vision_base_url: str = None, vision_model: str = None):
        configured_keys = api_key if api_key is not None else FIRECRAWL_API_KEYS
        self._firecrawl_api_keys = shared_api_key_pool("firecrawl", configured_keys)
        self.firecrawl_api_key = api_key
        self.vision_api_key = vision_api_key or BRAND3_LLM_API_KEY
        self.vision_base_url = vision_base_url or LLM_BASE_URL
        self.vision_model = vision_model or VISION_MODEL

    def take_screenshot(self, url: str) -> dict:
        """
        Take a screenshot via Firecrawl SDK. Returns {screenshot_url, metadata, error}.
        """
        if not self._firecrawl_api_keys:
            return {"error": "FIRECRAWL_API_KEY not set"}
        last_error = ""
        for _attempt in range(self._firecrawl_api_keys.size):
            try:
                from firecrawl import Firecrawl

                doc = Firecrawl(api_key=self._firecrawl_api_keys.next_key()).scrape(
                    url, formats=["screenshot"], max_age=0, timeout=60000
                )
                break
            except Exception as exc:
                last_error = str(exc)
        else:
            return {"error": f"Screenshot failed: {last_error}"}

        screenshot_url = doc.screenshot or ""
        if not screenshot_url:
            return {"error": "No screenshot URL in response"}

        metadata = doc.metadata
        if hasattr(metadata, "model_dump"):
            metadata = metadata.model_dump()
        return {"screenshot_url": screenshot_url, "metadata": metadata or {}}

    def _download_image(self, url: str) -> Optional[str]:
        """Resolve a screenshot URL to a local image path."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            path = urllib.request.url2pathname(parsed.path)
            return path if os.path.exists(path) else None
        if parsed.scheme == "" and os.path.exists(url):
            return url

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()

            fd, path = tempfile.mkstemp(suffix=".png")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return path
        except Exception as e:
            print(f"  Failed to download screenshot: {e}")
            return None

    def _is_temp_image_path(self, screenshot_url: str, image_path: str) -> bool:
        parsed = urllib.parse.urlparse(screenshot_url)
        return bool(image_path) and parsed.scheme in {"http", "https"}

    def _encode_image_base64(self, image_path: str) -> str:
        """Read and base64-encode an image file."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _read_png_pixels(self, image_path: str) -> tuple[int, int, list[tuple[int, int, int]]]:
        """Read 8-bit non-interlaced RGB/RGBA PNG pixels using only stdlib."""
        with open(image_path, "rb") as f:
            data = f.read()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("unsupported_image_format")

        offset = 8
        width = height = bit_depth = color_type = interlace = None
        idat = bytearray()
        while offset + 8 <= len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            chunk_type = data[offset + 4:offset + 8]
            chunk_data = data[offset + 8:offset + 8 + length]
            offset += 12 + length
            if chunk_type == b"IHDR":
                width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(
                    ">IIBBBBB", chunk_data
                )
            elif chunk_type == b"IDAT":
                idat.extend(chunk_data)
            elif chunk_type == b"IEND":
                break

        if not width or not height or bit_depth != 8 or color_type not in {2, 6} or interlace != 0:
            raise ValueError("unsupported_png_encoding")

        channels = 4 if color_type == 6 else 3
        row_bytes = width * channels
        raw = zlib.decompress(bytes(idat))
        rows: list[bytearray] = []
        pos = 0
        prev = bytearray(row_bytes)
        for _row_index in range(height):
            filter_type = raw[pos]
            pos += 1
            row = bytearray(raw[pos:pos + row_bytes])
            pos += row_bytes
            for i in range(row_bytes):
                left = row[i - channels] if i >= channels else 0
                up = prev[i]
                upper_left = prev[i - channels] if i >= channels else 0
                if filter_type == 1:
                    row[i] = (row[i] + left) & 0xFF
                elif filter_type == 2:
                    row[i] = (row[i] + up) & 0xFF
                elif filter_type == 3:
                    row[i] = (row[i] + ((left + up) // 2)) & 0xFF
                elif filter_type == 4:
                    predictor = self._png_paeth(left, up, upper_left)
                    row[i] = (row[i] + predictor) & 0xFF
                elif filter_type != 0:
                    raise ValueError("unsupported_png_filter")
            rows.append(row)
            prev = row

        step_x = max(1, width // 180)
        step_y = max(1, height // 180)
        pixels: list[tuple[int, int, int]] = []
        for y in range(0, height, step_y):
            row = rows[y]
            for x in range(0, width, step_x):
                idx = x * channels
                if channels == 4 and row[idx + 3] < 16:
                    continue
                pixels.append((row[idx], row[idx + 1], row[idx + 2]))
        return width, height, pixels

    def _png_paeth(self, left: int, up: int, upper_left: int) -> int:
        p = left + up - upper_left
        pa = abs(p - left)
        pb = abs(p - up)
        pc = abs(p - upper_left)
        if pa <= pb and pa <= pc:
            return left
        if pb <= pc:
            return up
        return upper_left

    def _local_image_analysis(self, image_path: str) -> dict:
        width, height, pixels = self._read_png_pixels(image_path)
        if not pixels:
            raise ValueError("no_pixels")

        quantized = Counter((r // 32 * 32, g // 32 * 32, b // 32 * 32) for r, g, b in pixels)
        dominant = []
        for (r, g, b), _count in quantized.most_common(8):
            color = "#{:02x}{:02x}{:02x}".format(min(r + 16, 255), min(g + 16, 255), min(b + 16, 255))
            if color not in dominant:
                dominant.append(color)
            if len(dominant) >= 6:
                break

        brightness_values = [(0.2126 * r + 0.7152 * g + 0.0722 * b) for r, g, b in pixels]
        avg_brightness = sum(brightness_values) / len(brightness_values)
        variance = sum((v - avg_brightness) ** 2 for v in brightness_values) / len(brightness_values)
        contrast = min(1.0, (variance ** 0.5) / 96.0)
        very_light = sum(1 for v in brightness_values if v > 238) / len(brightness_values)
        very_dark = sum(1 for v in brightness_values if v < 32) / len(brightness_values)
        whitespace_ratio = very_light
        visual_density = max(0.0, min(1.0, 1.0 - whitespace_ratio))

        palette_count = len(quantized)
        color_palette_score = max(45.0, min(82.0, 62.0 + min(palette_count, 24) * 0.6 - abs(whitespace_ratio - 0.42) * 18))
        layout_score = max(45.0, min(80.0, 66.0 + contrast * 10 - abs(visual_density - 0.55) * 15))
        typography_score = max(45.0, min(74.0, 58.0 + contrast * 12))
        overall_score = round((color_palette_score + layout_score + typography_score) / 3, 1)

        if avg_brightness < 85:
            style = "dark"
        elif whitespace_ratio > 0.55 and contrast < 0.45:
            style = "minimal"
        elif contrast > 0.58 and palette_count > 18:
            style = "modern"
        else:
            style = "clean"

        return {
            "image_dimensions": {"width": width, "height": height},
            "dominant_colors": dominant,
            "visual_density": round(visual_density, 3),
            "whitespace_ratio": round(whitespace_ratio, 3),
            "average_brightness": round(avg_brightness, 1),
            "contrast_signal": round(contrast, 3),
            "very_dark_ratio": round(very_dark, 3),
            "style": style,
            "color_palette_score": round(color_palette_score, 1),
            "layout_consistency_score": round(layout_score, 1),
            "typography_score": round(typography_score, 1),
            "overall_score": overall_score,
            "insights": [
                f"Local image analysis found {len(dominant)} dominant color groups.",
                f"Whitespace ratio {whitespace_ratio:.2f}; contrast signal {contrast:.2f}.",
            ],
        }

    def _result_from_local_analysis(self, screenshot_url: str, image_path: str, local_details: dict) -> VisualAnalysisResult:
        result = VisualAnalysisResult(screenshot_url=screenshot_url, screenshot_path=image_path)
        result.color_palette_score = float(local_details["color_palette_score"])
        result.logo_detected = False
        result.logo_score = 50.0
        result.typography_score = float(local_details["typography_score"])
        result.layout_consistency_score = float(local_details["layout_consistency_score"])
        result.overall_score = float(local_details["overall_score"])
        result.confidence = 0.55
        result.details = {
            "method": "local_image_analysis",
            "dominant_colors": local_details["dominant_colors"],
            "style": local_details["style"],
            "logo_position": "unknown",
            "typography_consistent": None,
            "image_dimensions": local_details["image_dimensions"],
            "visual_density": local_details["visual_density"],
            "whitespace_ratio": local_details["whitespace_ratio"],
            "average_brightness": local_details["average_brightness"],
            "contrast_signal": local_details["contrast_signal"],
            "insights": local_details["insights"],
        }
        return result

    def _merge_vision_and_local_details(self, details: dict, local_details: dict) -> dict:
        merged = dict(details or {})
        if not merged.get("dominant_colors"):
            merged["dominant_colors"] = local_details["dominant_colors"]
        if not merged.get("style") or merged.get("style") == "unknown":
            merged["style"] = local_details["style"]
        if not merged.get("insights") and local_details.get("insights"):
            merged["insights"] = local_details["insights"]
        for key in (
            "image_dimensions",
            "visual_density",
            "whitespace_ratio",
            "average_brightness",
            "contrast_signal",
        ):
            merged.setdefault(key, local_details.get(key))
        return merged

    def _call_vision_api(self, image_base64: str, prompt: str) -> dict:
        """
        Call a vision-capable LLM to analyze the screenshot.
        Uses OpenAI-compatible vision API format.
        """
        if not self.vision_api_key:
            return {}

        payload = json.dumps({
            "model": self.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            # Thinking models (Gemini 2.5+) consume part of the output budget
            # on internal reasoning before emitting content; 1200 silently
            # truncated the JSON and forced the local fallback on every run.
            "max_tokens": 8000,
            "temperature": 0.1,
        }).encode()

        req = urllib.request.Request(
            f"{self.vision_base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.vision_api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
                content = data["choices"][0]["message"].get("content", "")
                return self._extract_json(content)
        except Exception as e:
            print(f"  Vision API call failed: {e}")
            return {}

    def _extract_json(self, text: str) -> dict:
        """Extract JSON from potentially markdown-wrapped response."""
        text = text.strip()
        # Handle markdown code blocks
        if "```" in text:
            # Find first code block
            start = text.find("```")
            if start != -1:
                after_marker = text.find("\n", start)
                if after_marker != -1:
                    end = text.find("```", after_marker + 1)
                    if end != -1:
                        text = text[after_marker + 1:end].strip()
        # Try to find JSON object
        if not text.startswith("{"):
            brace_start = text.find("{")
            if brace_start != -1:
                brace_end = text.rfind("}")
                if brace_end != -1:
                    text = text[brace_start:brace_end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def _analyze_with_heuristics(self, metadata: dict) -> VisualAnalysisResult:
        """
        Fallback heuristic analysis when vision API is unavailable.
        Uses metadata from the page (og:image, favicon, etc.) as signals.
        """
        result = VisualAnalysisResult()
        result.confidence = 0.35  # Low confidence for heuristics only

        # Has favicon = minor brand identity signal
        has_favicon = bool(metadata.get("favicon"))
        # Has og:image = social sharing image
        has_og_image = bool(metadata.get("og:image") or metadata.get("ogImage"))
        # Has a clear title
        has_title = bool(metadata.get("title"))
        # Has description
        has_description = bool(metadata.get("description") or metadata.get("og:description"))

        score = 40.0
        if has_favicon:
            score += 10
        if has_og_image:
            score += 15
        if has_title:
            score += 10
        if has_description:
            score += 5

        result.overall_score = min(score, 100.0)
        result.color_palette_score = 50.0
        result.logo_score = 60.0 if has_favicon else 40.0
        result.typography_score = 50.0
        result.layout_consistency_score = 50.0
        result.details = {
            "method": "heuristic_fallback",
            "has_favicon": has_favicon,
            "has_og_image": has_og_image,
        }

        return result

    def analyze_screenshot(self, screenshot_url: str,
                           brand_name: str = "",
                           page_metadata: dict = None) -> VisualAnalysisResult:
        """
        Analyze a screenshot for visual brand consistency.

        Args:
            screenshot_url: URL of the screenshot image
            brand_name: Name of the brand being analyzed
            page_metadata: Optional metadata from the page scrape
        """
        result = VisualAnalysisResult(screenshot_url=screenshot_url)

        # Download the screenshot for local analysis
        image_path = self._download_image(screenshot_url)
        if image_path:
            result.screenshot_path = image_path

        local_details = None
        if image_path:
            try:
                local_details = self._local_image_analysis(image_path)
            except Exception as exc:
                local_details = {"error": str(exc)[:120]}

        # Try vision analysis
        if image_path and self.vision_api_key:
            image_b64 = self._encode_image_base64(image_path)

            prompt = f"""Analyze this website screenshot for brand quality and authenticity. The brand is "{brand_name}".

Evaluate these aspects and return ONLY valid JSON:

**Visual Consistency:**
1. color_palette_score (0-100): How consistent and intentional is the color palette?
2. logo_detected (true/false): Is there a visible logo?
3. logo_score (0-100): How professional is the logo placement?
4. typography_score (0-100): Typography consistency and hierarchy?
5. layout_consistency_score (0-100): Layout cleanliness?

**Design Authenticity (NEW — judge if this feels custom or template):**
6. design_authenticity (0-100): Does this look custom-designed by a real designer, or does it feel like a template/AI-generated layout?
   - 100 = clearly custom, unique design language, creative layout
   - 70 = professional, some custom elements
   - 50 = clean but generic, could be any SaaS
   - 30 = obviously a template, Tailwind/UI kit, cards-in-a-grid
   - 10 = AI-generated look, cookie-cutter, no visual identity
7. cta_count: How many "Book a Call" / "Get Started" / "Contact" buttons are visible?
8. section_repetition: Does the page repeat the same card/grid layout section after section?

Return JSON:
{{
    "color_palette_score": <0-100>,
    "logo_detected": <true/false>,
    "logo_score": <0-100>,
    "typography_score": <0-100>,
    "layout_consistency_score": <0-100>,
    "overall_score": <0-100>,
    "design_authenticity": <0-100>,
    "cta_count": <number>,
    "section_repetition": <true/false>,
    "details": {{
        "dominant_colors": ["list", "of", "main", "colors"],
        "style": "modern|classic|minimal|playful|corporate|dark-mode-agency",
        "logo_position": "top-left|top-center|top-right|none",
        "design_verdict": "custom|professional-generic|template|ai-generated",
        "authenticity_insights": ["any observations about design authenticity"],
        "observations": "brief description"
    }}
}}"""

            vision_result = self._call_vision_api(image_b64, prompt)

            if vision_result and "overall_score" in vision_result:
                result.color_palette_score = float(vision_result.get("color_palette_score", 50))
                result.logo_detected = bool(vision_result.get("logo_detected", False))
                result.logo_score = float(vision_result.get("logo_score", 50))
                result.typography_score = float(vision_result.get("typography_score", 50))
                result.layout_consistency_score = float(vision_result.get("layout_consistency_score", 50))
                result.overall_score = float(vision_result.get("overall_score", 50))

                # Design authenticity fields (NEW)
                design_auth = float(vision_result.get("design_authenticity", 50))
                cta_count = int(vision_result.get("cta_count", 0))
                section_repetition = bool(vision_result.get("section_repetition", False))

                # Handle both nested details and flat response formats
                details = vision_result.get("details", {})
                if not details:
                    # Some models return fields at top level
                    details = {
                        "dominant_colors": vision_result.get("dominant_colors", []),
                        "style": vision_result.get("style", ""),
                        "logo_position": vision_result.get("logo_position", ""),
                        "design_verdict": vision_result.get("design_verdict", ""),
                        "authenticity_insights": vision_result.get("authenticity_insights", []),
                        "observations": vision_result.get("observations", ""),
                    }

                # Add authenticity data to details
                details["design_authenticity"] = design_auth
                details["cta_count"] = cta_count
                details["section_repetition"] = section_repetition
                if local_details and "error" not in local_details:
                    details = self._merge_vision_and_local_details(details, local_details)
                    details["method"] = "mixed"
                    details["vision_method"] = "vision_llm"
                    details["local_method"] = "local_image_analysis"
                else:
                    details["method"] = "vision_llm"
                result.details = details
                result.confidence = 0.8
            else:
                if local_details and "error" not in local_details:
                    fallback = self._result_from_local_analysis(screenshot_url, image_path, local_details)
                    fallback.details["vision_failed"] = True
                    if local_details.get("error"):
                        fallback.details["local_error"] = local_details["error"]
                    if self._is_temp_image_path(screenshot_url, image_path):
                        try:
                            os.unlink(image_path)
                        except OSError:
                            pass
                    return fallback

                # Vision failed, fall back to metadata heuristics
                fallback = self._analyze_with_heuristics(page_metadata or {})
                fallback.screenshot_url = screenshot_url
                fallback.screenshot_path = image_path or ""
                fallback.details["vision_failed"] = True
                return fallback

            # Clean up temp file
            if self._is_temp_image_path(screenshot_url, image_path):
                try:
                    os.unlink(image_path)
                except OSError:
                    pass
        else:
            if image_path and local_details and "error" not in local_details:
                return self._result_from_local_analysis(screenshot_url, image_path, local_details)

            # No image analysis available
            return self._analyze_with_heuristics(page_metadata or {})

        return result

    def analyze_url(self, url: str, brand_name: str = "") -> VisualAnalysisResult:
        """
        Full pipeline: take screenshot of URL, then analyze it.
        """
        print(f"  [Visual] Taking screenshot of {url}...")
        screenshot_data = self.take_screenshot(url)

        if "error" in screenshot_data:
            result = VisualAnalysisResult(error=screenshot_data["error"])
            result.confidence = 0.1
            result.details = {"error": screenshot_data["error"]}
            return result

        screenshot_url = screenshot_data["screenshot_url"]
        metadata = screenshot_data.get("metadata", {})

        print(f"  [Visual] Analyzing screenshot for {brand_name or url}...")
        return self.analyze_screenshot(screenshot_url, brand_name, metadata)
