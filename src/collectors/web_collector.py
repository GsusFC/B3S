"""
Web collector using Firecrawl.

Scrapes the brand's website and extracts:
- HTML structure, meta tags, content
- Visual assets (logo, colors — via screenshots)
- Tech stack detection
- Page speed signals
"""

import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from src.api_key_pool import ApiKeySource, shared_api_key_pool
from src.collectors.web_collector_capture_runtime import WebCollectorCaptureSupport
from src.collectors.web_collector_content_runtime import WebCollectorContentSupport
from src.collectors.web_collector_support_linking_runtime import WebCollectorLinkingSupport
from src.config import FIRECRAWL_API_KEYS

_TRANSIENT_FETCH_ATTEMPTS = 2
_TRANSIENT_FETCH_DELAY_S = 1.5


@dataclass
class WebData:
    """Raw web data from scraping."""
    url: str
    title: str = ""
    meta_description: str = ""
    markdown_content: str = ""
    html: str = ""
    canonical_url: str = ""
    alternate_domains: list[str] = None
    links: list = None
    images: list = None
    screenshot_path: str = ""
    tech_stack: list[str] = None
    load_time_ms: int = 0
    error: str = ""
    # Set when a capture was wiped because it looked like a consent wall —
    # downstream can then tell "obstructed" apart from "empty" or "failed".
    capture_obstruction: str = ""
    content_source: str = ""
    browser_status: int | None = None
    owned_fallback_urls: list[str] = None

    def __post_init__(self):
        self.links = self.links or []
        self.alternate_domains = self.alternate_domains or []
        self.images = self.images or []
        self.tech_stack = self.tech_stack or []
        self.owned_fallback_urls = self.owned_fallback_urls or []


class WebCollector(
    WebCollectorLinkingSupport,
    WebCollectorCaptureSupport,
    WebCollectorContentSupport,
):
    """Collects web data via Firecrawl CLI."""

    COOKIE_BANNER_KEYWORDS = [
        "aceptar",
        "rechazar cookies",
        "cookie preferences",
        "manage cookies",
        "accept cookies",
        "consent",
    ]

    COOKIE_PATTERNS = [
        r"we value your privacy",
        r"cookie",
        r"consent preferences",
        r"accept all",
        r"reject all",
        r"customise",
        r"customize",
        r"necessary always active",
        r"manage preferences",
        r"no cookies to display",
        r"revisit consent",
        r"show more",
        r"necessaryalways active",
        r"strictly necessary",
        r"functional",
        r"analytics",
        r"performance",
        r"advertisement",
    ]

    FIRECRAWL_PROMPT_PATTERNS = [
        r"turn websites into llm-ready data",
        r"authenticate with your firecrawl account",
        r"login with browser",
        r"enter api key manually",
        r"you are not logged in",
    ]

    def __init__(self, api_key: ApiKeySource = None):
        configured_keys = api_key if api_key is not None else FIRECRAWL_API_KEYS
        self._api_keys = shared_api_key_pool("firecrawl", configured_keys)
        self.api_key = api_key

    @staticmethod
    def _normalize_request_url(url: str) -> str:
        """Percent-encode unsafe characters before issuing an HTTP request."""
        raw = str(url or "").strip()
        if not raw:
            return raw
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return raw
        path = quote(parts.path or "", safe="/:%@-._~!$&'()*+,;=")
        query = quote(parts.query or "", safe="=&?/:@-._~!$&'()*+,;=%[]")
        fragment = quote(parts.fragment or "", safe="=&?/:@-._~!$&'()*+,;=%[]")
        return urlunsplit((parts.scheme, parts.netloc, path, query, fragment))

    def _run_firecrawl(self, url: str) -> dict:
        """Scrape URL via Firecrawl Python SDK. Returns legacy {content, raw, error} shape."""
        if not self._api_keys:
            return {"error": "FIRECRAWL_API_KEY not set"}
        last_error = ""
        attempts = max(_TRANSIENT_FETCH_ATTEMPTS, self._api_keys.size)
        for attempt in range(attempts):
            try:
                from firecrawl import Firecrawl

                doc = Firecrawl(api_key=self._api_keys.next_key()).scrape(
                    url,
                    formats=["markdown", "html"],
                    timeout=60000,
                    wait_for=2000,
                    only_main_content=True,
                )
                break
            except Exception as exc:
                # Transient network failures are the common case here; one
                # retry keeps the capture on the best tier instead of
                # degrading to the HTML/browser fallbacks.
                last_error = str(exc)
                if attempt + 1 < attempts:
                    time.sleep(_TRANSIENT_FETCH_DELAY_S)
        else:
            return {"error": last_error}
        content = (doc.markdown or "").strip()
        html = (getattr(doc, "html", None) or "").strip()
        return {"content": content, "raw": content, "html": html}

    def scrape(self, url: str, crawl_subpages: bool = True) -> WebData:
        """Scrape a website and return structured data."""
        url = self._normalize_request_url(url)
        data = WebData(url=url)

        # Basic scrape
        result = self._run_firecrawl(url)
        if "error" not in result:
            data.markdown_content = self._clean_markdown_content(result.get("content", ""))
            data.html = result.get("html", "") or data.html
            data.title = self._extract_title(data.markdown_content)
            data.markdown_content = self._trim_to_title(data.markdown_content, data.title)
            if self._looks_like_cookie_banner(data.title, data.markdown_content):
                print(
                    f"  WARNING: scrape may be cookie banner, not content"
                    f" (title: {data.title[:80]})"
                )
                data.title = ""
                data.markdown_content = ""
                data.capture_obstruction = "cookie_banner"
        else:
            data.error = result["error"]

        if not self._has_usable_markdown_content(data.markdown_content):
            html, html_error = self._fetch_html_fallback(url)
            if html:
                data.html = html
                data.canonical_url, data.alternate_domains = self._extract_canonical_metadata(html)
                data.meta_description = self._extract_meta_description(html)
                data.title = self._extract_html_title(html) or data.title
                data.markdown_content = self._html_to_markdown_fallback(html)
                data.markdown_content = self._trim_to_title(data.markdown_content, data.title)
                data.error = ""
                data.capture_obstruction = ""
            elif html_error and not data.error:
                data.error = html_error

        if not self._has_usable_markdown_content(data.markdown_content):
            payload, browser_error = self._fetch_browser_fallback(url)
            if payload:
                data.html = payload.get("html") or data.html
                data.links = payload.get("links") or data.links
                data.browser_status = payload.get("status")
                data.title = payload.get("title") or data.title
                data.meta_description = payload.get("meta_description") or data.meta_description
                data.canonical_url = payload.get("canonical_url") or data.canonical_url
                data.markdown_content = self._body_text_to_markdown(
                    payload.get("body_text") or "",
                    title=data.title,
                    meta_description=data.meta_description,
                )
                data.markdown_content = self._trim_to_title(data.markdown_content, data.title)
                if self._looks_like_cookie_banner(data.title, data.markdown_content):
                    print(
                        f"  WARNING: browser fallback may be cookie banner, not content"
                        f" (title: {data.title[:80]})"
                    )
                    data.markdown_content = ""
                    data.capture_obstruction = "cookie_banner"
                if self._has_usable_markdown_content(data.markdown_content):
                    data.content_source = "browser_fallback"
                    data.error = ""
                    data.capture_obstruction = ""
            elif browser_error and not data.error:
                data.error = browser_error

        if crawl_subpages and self._has_usable_markdown_content(data.markdown_content):
            internal_links = self._extract_internal_links(
                data.markdown_content,
                url,
                html=data.html,
                links=data.links,
            )
            subpages_to_crawl = self._select_internal_links_to_crawl(internal_links, url)
            
            subpage_contents = []
            owned_fallback_urls = []
            for subpage_url in subpages_to_crawl:
                subpage_data = self.scrape(subpage_url, crawl_subpages=False)
                if subpage_data.markdown_content:
                    owned_fallback_urls.append(subpage_url)
                    subpage_contents.append(
                        f"\n\n---\n## Subpage: {subpage_url}\n{subpage_data.markdown_content}"
                    )
            
            if subpage_contents:
                data.markdown_content += "".join(subpage_contents)
                data.owned_fallback_urls = list(dict.fromkeys(data.owned_fallback_urls + owned_fallback_urls))

        return data


    def scrape_multiple(self, urls: list[str]) -> list[WebData]:
        """Scrape multiple URLs."""
        return [self.scrape(url) for url in urls]
