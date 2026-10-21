"""Content extraction and normalization helpers for the web collector."""

from __future__ import annotations

import re
from bisect import bisect_left
from html import unescape
from urllib.parse import urlparse

_MIN_USABLE_MARKDOWN_CHARS = 200
_FALLBACK_MAX_LINES = 96
_FALLBACK_RICH_TEXT_MIN_CHARS = 2000
_FALLBACK_MAX_CHARS = 12000
_SKIPPED_ELEMENT_NAMES = r"script|style|noscript|svg|iframe"
_TEXT_BLOCK_NAMES = r"h[1-6]|p|li|blockquote"


def _paired_elements(html: str, names: str) -> list[tuple[int, int, int, int]]:
    """Return what ``<(names)[^>]*>(.*?)</\\1>`` finds, in linear time.

    Each item is (start, content start, content end, end). With that regex an
    opening tag without a later closing tag, such as ``<link`` read as ``<li``,
    rescans the rest of the page, so a large page held the GIL for hours.
    """
    gt_positions = [match.start() for match in re.finditer(">", html)]
    closings: dict[str, list[tuple[int, int]]] = {}
    for match in re.finditer(rf"</({names})>", html, flags=re.IGNORECASE):
        closings.setdefault(match.group(1).lower(), []).append((match.start(), match.end()))
    closing_starts = {name: [start for start, _ in spans] for name, spans in closings.items()}

    elements: list[tuple[int, int, int, int]] = []
    cursor = 0
    for opening in re.finditer(rf"<({names})", html, flags=re.IGNORECASE):
        if opening.start() < cursor:
            continue
        gt_index = bisect_left(gt_positions, opening.end())
        if gt_index == len(gt_positions):
            break
        content_start = gt_positions[gt_index] + 1
        name = opening.group(1).lower()
        starts = closing_starts.get(name, [])
        closing_index = bisect_left(starts, content_start)
        if closing_index == len(starts):
            continue
        content_end, end = closings[name][closing_index]
        elements.append((opening.start(), content_start, content_end, end))
        cursor = end
    return elements


class WebCollectorContentSupport:
    """Composable helpers for cleaning, extracting, and normalizing web text."""

    def _looks_like_cookie_banner(self, title: str, content: str) -> bool:
        title_lower = (title or "").lower()
        preview_lower = (content or "")[:200].lower()
        return any(
            keyword in title_lower or keyword in preview_lower
            for keyword in self.COOKIE_BANNER_KEYWORDS
        )

    def _clean_markdown_content(self, content: str) -> str:
        """Remove obvious cookie/consent UI sludge from scraped markdown."""
        if not content:
            return ""

        lowered_content = content.lower()
        if any(
            re.search(pattern, lowered_content) for pattern in self.FIRECRAWL_PROMPT_PATTERNS
        ):
            return ""

        cleaned_lines = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                cleaned_lines.append("")
                continue

            lowered = stripped.lower()
            if any(re.search(pattern, lowered) for pattern in self.COOKIE_PATTERNS):
                continue
            if stripped.startswith("![") and "consent" in lowered:
                continue
            if len(stripped) <= 24 and lowered in {
                "accept all",
                "reject all",
                "customise",
                "customize",
                "close",
                "show more",
            }:
                continue

            cleaned_lines.append(stripped)

        # Collapse excessive blank lines introduced by filtering.
        collapsed = []
        previous_blank = False
        for line in cleaned_lines:
            is_blank = not line
            if is_blank and previous_blank:
                continue
            collapsed.append(line)
            previous_blank = is_blank

        trimmed = self._trim_preamble(collapsed)

        return "\n".join(trimmed).strip()

    def _trim_preamble(self, lines: list[str]) -> list[str]:
        """Drop leading UI/navigation sludge before the first meaningful content block."""
        meaningful_index = None

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if (
                self._is_meaningful_content_line(stripped)
                and not self._is_link_only_line(stripped)
            ):
                meaningful_index = idx
                break

        if meaningful_index is None:
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if stripped and self._is_meaningful_content_line(stripped):
                    meaningful_index = idx
                    break

        if meaningful_index is None or meaningful_index <= 0:
            return lines
        return lines[meaningful_index:]

    def _is_meaningful_content_line(self, line: str) -> bool:
        if line.startswith("# "):
            return True
        if len(line) >= 28:
            return True
        if any(mark in line for mark in [".", ",", ":", "?", "!"]):
            return True
        if line.startswith("[") and "](" in line and len(line) >= 36:
            return True
        return False

    def _is_link_only_line(self, line: str) -> bool:
        return line.startswith("[") and "](" in line

    def _extract_title(self, content: str) -> str:
        """Extract a meaningful title from cleaned markdown."""
        for line in content.split("\n"):
            if line.startswith("# "):
                return line[2:].strip()

        for line in content.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("![") and len(stripped) <= 120:
                return stripped
        return ""

    def _trim_to_title(self, content: str, title: str) -> str:
        """Drop any leading content that appears before the extracted title."""
        if not content or not title:
            return content

        lines = content.splitlines()
        for idx, line in enumerate(lines):
            normalized = line.strip()
            if normalized == title or normalized == f"# {title}":
                if idx > 0:
                    return "\n".join(lines[idx:]).strip()
                return content
        return content

    def _extract_html_title(self, html: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        return self._normalize_html_text(match.group(1))

    def _extract_meta_description(self, html: str) -> str:
        patterns = [
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
            if match:
                return self._normalize_html_text(match.group(1))
        return ""

    def _normalize_html_text(self, text: str) -> str:
        cleaned = unescape(re.sub(r"\s+", " ", text or "")).strip()
        return cleaned

    def _extract_domains_from_urls(self, urls: list[str]) -> list[str]:
        domains = []
        seen = set()
        for value in urls:
            if not value:
                continue
            parsed = urlparse(value if "://" in value else f"https://{value}")
            host = (parsed.netloc or parsed.path or "").strip().lower()
            if host.startswith("www."):
                host = host[4:]
            if not host or "." not in host or host in seen:
                continue
            seen.add(host)
            domains.append(host)
        return domains

    def _extract_canonical_metadata(self, html: str) -> tuple[str, list[str]]:
        if not html:
            return "", []

        urls = []
        patterns = [
            r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](.*?)["\']',
            r'<link[^>]+rel=["\']alternate["\'][^>]+href=["\'](.*?)["\']',
            r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\'](.*?)["\']',
            r'"url"\s*:\s*"(https?://[^"]+)"',
        ]
        for pattern in patterns:
            urls.extend(
                match.strip()
                for match in re.findall(pattern, html, flags=re.IGNORECASE | re.DOTALL)
                if match and isinstance(match, str)
            )

        canonical_url = urls[0] if urls else ""
        alternate_domains = self._extract_domains_from_urls(urls)
        return canonical_url, alternate_domains

    def _html_to_markdown_fallback(self, html: str) -> str:
        """Extract a readable text snapshot from raw HTML."""
        if not html:
            return ""

        title = self._extract_html_title(html)
        meta_description = self._extract_meta_description(html)

        body_parts: list[str] = []
        kept_from = 0
        for start, _, _, end in _paired_elements(html, _SKIPPED_ELEMENT_NAMES):
            body_parts.extend((html[kept_from:start], " "))
            kept_from = end
        body_parts.append(html[kept_from:])
        body = "".join(body_parts)

        block_matches = [
            (None, body[content_start:content_end])
            for _, content_start, content_end, _ in _paired_elements(body, _TEXT_BLOCK_NAMES)
        ]

        lines: list[str] = []
        seen: set[str] = set()
        total_chars = 0
        for _, fragment in block_matches:
            text = re.sub(r"<[^>]+>", " ", fragment)
            text = self._normalize_html_text(text)
            if not text or len(text) < 12:
                continue
            lowered = text.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            lines.append(text)
            total_chars += len(text)
            if len(lines) >= _FALLBACK_MAX_LINES:
                break

        if total_chars < _FALLBACK_RICH_TEXT_MIN_CHARS:
            # SPA/SSR pages keep most copy in <div>/<span>, invisible to the
            # block-tag pass; recover it from whole-body text with breaks at
            # block boundaries so strategy sections (values, culture) survive.
            with_breaks = re.sub(
                r"</(p|div|h[1-6]|li|section|article|blockquote|tr)>|<br\s*/?>",
                "\n",
                body,
                flags=re.IGNORECASE,
            )
            text_only = re.sub(r"<[^>]+>", " ", with_breaks)
            for raw_line in text_only.splitlines():
                text = self._normalize_html_text(raw_line)
                if not text or len(text) < 12:
                    continue
                lowered = text.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                lines.append(text)
                total_chars += len(text)
                if len(lines) >= _FALLBACK_MAX_LINES or total_chars >= _FALLBACK_MAX_CHARS:
                    break

        content_parts = []
        if title:
            content_parts.append(f"# {title}")
        if meta_description and meta_description.lower() != title.lower():
            content_parts.append(meta_description)
        content_parts.extend(lines)

        return "\n\n".join(part for part in content_parts if part).strip()

    def _has_usable_markdown_content(self, content: str) -> bool:
        return len((content or "").strip()) >= _MIN_USABLE_MARKDOWN_CHARS

    def _body_text_to_markdown(
        self,
        body_text: str,
        *,
        title: str = "",
        meta_description: str = "",
    ) -> str:
        lines = []
        seen = set()
        for raw_line in (body_text or "").splitlines():
            line = self._normalize_html_text(raw_line)
            if not line or len(line) < 3:
                continue
            lowered = line.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            lines.append(line)

        content_parts = []
        if title:
            content_parts.append(f"# {title}")
        if meta_description and meta_description.lower() != title.lower():
            content_parts.append(meta_description)
        content_parts.extend(lines)
        return self._clean_markdown_content(
            "\n\n".join(part for part in content_parts if part).strip()
        )
