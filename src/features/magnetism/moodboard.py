"""Deterministic moodboard built from persisted scan acquisition inputs.

The moodboard is a read-only visual companion for a Magnetism scan: it
collects representative images the brand itself published (og/twitter
cards, icons, page imagery) from the stored "web" raw input of the source
Brand Audit run, and pairs them with the strategic blocks already present
in the scan payload. No network calls and no LLM calls happen here, so the
view stays reproducible against the persisted snapshot.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

_LOG = logging.getLogger(__name__)

MAX_MOODBOARD_IMAGES = 14

# Strategic TLDR blocks that frame the imagery on the moodboard.
VISUAL_READING_BLOCKS = (
    "brand_idea",
    "personality",
    "attributes",
    "value_proposition",
)

_ROLE_PRIORITY = {"social_card": 0, "logo": 1, "content": 2}

_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)")

# Hosts/paths that are tracking beacons or chrome, never brand imagery.
_NOISE_MARKERS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook.com/tr",
    "/pixel",
    "1x1",
    "spacer",
    "transparent.gif",
)


def _clean_url(base_url: str, raw: str) -> str | None:
    candidate = (raw or "").strip()
    if not candidate or candidate.startswith("data:"):
        return None
    resolved = urljoin(base_url, candidate) if base_url else candidate
    parsed = urlparse(resolved)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    resolved = resolved.split("#", 1)[0]
    lowered = resolved.lower()
    if lowered.endswith(".ico"):
        return None
    if any(marker in lowered for marker in _NOISE_MARKERS):
        return None
    return resolved


class _ImageTagParser(HTMLParser):
    """Collect image candidates from meta/link/img tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.social_cards: list[str] = []
        self.icons: list[str] = []
        self.images: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name: (value or "") for name, value in attrs}
        if tag == "meta":
            key = (attr_map.get("property") or attr_map.get("name") or "").lower()
            content = attr_map.get("content", "")
            if key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src") and content:
                self.social_cards.append(content)
            elif key == "og:logo" and content:
                self.icons.append(content)
        elif tag == "link":
            rel = (attr_map.get("rel") or "").lower()
            href = attr_map.get("href", "")
            if "icon" in rel and href:
                self.icons.append(href)
        elif tag == "img":
            src = attr_map.get("src") or attr_map.get("data-src") or ""
            if not src:
                return
            if attr_map.get("width") == "1" or attr_map.get("height") == "1":
                return
            self.images.append((src, attr_map.get("alt", "")))


def extract_moodboard_images(web_payload: dict | None) -> list[dict]:
    """Extract representative image candidates from a persisted web raw input."""
    if not isinstance(web_payload, dict):
        return []

    base_url = str(web_payload.get("canonical_url") or web_payload.get("url") or "")
    candidates: list[dict] = []

    html = str(web_payload.get("html") or "")
    if html:
        parser = _ImageTagParser()
        try:
            parser.feed(html)
        except Exception:
            # Malformed markup should never break a scan view; fall back to the
            # markdown/images candidates but keep the signal for debugging.
            _LOG.debug("Moodboard HTML parse failed; using markdown/images fallback", exc_info=True)
        for raw in parser.social_cards:
            candidates.append({"raw": raw, "role": "social_card", "alt": ""})
        for raw in parser.icons:
            candidates.append({"raw": raw, "role": "logo", "alt": ""})
        for raw, alt in parser.images:
            candidates.append({"raw": raw, "role": "content", "alt": alt})

    markdown = str(web_payload.get("markdown_content") or "")
    for match in _MARKDOWN_IMAGE_RE.finditer(markdown):
        candidates.append({"raw": match.group(2), "role": "content", "alt": match.group(1)})

    for raw in web_payload.get("images") or []:
        if isinstance(raw, str):
            candidates.append({"raw": raw, "role": "content", "alt": ""})

    images: list[dict] = []
    seen: set[str] = set()
    candidates.sort(key=lambda item: _ROLE_PRIORITY.get(item["role"], 9))
    for item in candidates:
        url = _clean_url(base_url, item["raw"])
        if not url or url in seen:
            continue
        seen.add(url)
        images.append(
            {
                "url": url,
                "role": item["role"],
                "alt": (item["alt"] or "").strip()[:160],
                "host": urlparse(url).netloc,
            }
        )
        if len(images) >= MAX_MOODBOARD_IMAGES:
            break
    return images


def _block_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, (list, tuple)):
        return " · ".join(str(item) for item in content if str(item).strip())
    return str(content or "").strip()


def build_moodboard_model(
    scan_payload: dict,
    web_payload: dict | None,
    *,
    brand_logo_url: str | None = None,
) -> dict:
    """Assemble the moodboard view model from persisted data only."""
    images = extract_moodboard_images(web_payload)

    logo_url = _clean_url("", brand_logo_url or "")
    if logo_url and all(item["url"] != logo_url for item in images):
        images.insert(
            0,
            {"url": logo_url, "role": "logo", "alt": "", "host": urlparse(logo_url).netloc},
        )
        images = images[:MAX_MOODBOARD_IMAGES]

    tldr = scan_payload.get("tldr_brand3") or {}
    visual_reading = []
    for key in VISUAL_READING_BLOCKS:
        block = tldr.get(key) or {}
        if not block.get("detected"):
            continue
        text = _block_text(block)
        if text:
            visual_reading.append({"key": key, "text": text})

    role_counts: dict[str, int] = {}
    for item in images:
        role_counts[item["role"]] = role_counts.get(item["role"], 0) + 1
    logo_image = next((item for item in images if item["role"] == "logo"), None)

    return {
        "available": bool(images),
        "images": images,
        "logo_image": logo_image,
        "visual_reading": visual_reading,
        "role_counts": role_counts,
        "image_count": len(images),
        "page_url": str((web_payload or {}).get("url") or scan_payload.get("url") or ""),
    }
