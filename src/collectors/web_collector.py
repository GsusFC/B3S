"""
Web collector using Firecrawl.

Scrapes the brand's website and extracts:
- HTML structure, meta tags, content
- Visual assets (logo, colors — via screenshots)
- Tech stack detection
- Page speed signals
"""

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

from src.api_key_pool import ApiKeySource, shared_api_key_pool
from src.collectors.page_language import (
    detect_page_language,
    summarize_page_languages,
)
from src.collectors.web_collector_capture_runtime import WebCollectorCaptureSupport
from src.collectors.web_collector_content_runtime import WebCollectorContentSupport
from src.collectors.web_collector_support_linking_runtime import (
    MAX_OWNED_SUBPAGES,
    OWNED_PAGE_SELECTION_VERSION,
    VAULT_MAX_OWNED_SUBPAGES,
    VAULT_MAX_SITEMAP_EXPLORATION_PAGES,
    VAULT_MAX_STRATEGIC_ROLE_PAGES,
    VAULT_OWNED_PAGE_SELECTION_VERSION,
    WebCollectorLinkingSupport,
)
from src.config import FIRECRAWL_API_KEYS

_TRANSIENT_FETCH_ATTEMPTS = 2
_TRANSIENT_FETCH_DELAY_S = 1.5
_FIRECRAWL_MAX_AGE_MS = 0
WEB_CAPTURE_PROVENANCE_VERSION = "web-capture-provenance-v1"


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
    capture_provenance: dict = None
    page_selection: dict = None

    def __post_init__(self):
        self.links = self.links or []
        self.alternate_domains = self.alternate_domains or []
        self.images = self.images or []
        self.tech_stack = self.tech_stack or []
        self.owned_fallback_urls = self.owned_fallback_urls or []
        self.capture_provenance = self.capture_provenance or {}
        self.page_selection = self.page_selection or {}


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

    def __init__(
        self,
        api_key: ApiKeySource = None,
        *,
        sitemap_discovery: bool = True,
    ):
        configured_keys = api_key if api_key is not None else FIRECRAWL_API_KEYS
        self._api_keys = shared_api_key_pool("firecrawl", configured_keys)
        self.api_key = api_key
        self.sitemap_discovery = sitemap_discovery

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
                    max_age=_FIRECRAWL_MAX_AGE_MS,
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
        metadata = self._firecrawl_metadata(getattr(doc, "metadata", None))
        final_url = str(
            metadata.get("sourceURL")
            or metadata.get("source_url")
            or metadata.get("url")
            or url
        ).strip()
        return {
            "content": content,
            "raw": content,
            "html": html,
            "final_url": final_url,
        }

    def scrape(self, url: str, crawl_subpages: bool = True) -> WebData:
        """Scrape a website and return structured data."""
        url = self._normalize_request_url(url)
        data = WebData(url=url)
        capture_provider = ""
        final_url = url

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
            if self._has_usable_markdown_content(data.markdown_content):
                capture_provider = "firecrawl"
                final_url = str(result.get("final_url") or url)
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
                capture_provider = "direct_http"
                final_url = data.canonical_url or url
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
                    capture_provider = "browser_fallback"
                    final_url = data.canonical_url or url
            elif browser_error and not data.error:
                data.error = browser_error

        if crawl_subpages and self._has_usable_markdown_content(data.markdown_content):
            homepage_language = detect_page_language(
                data.markdown_content,
                html=data.html,
            )
            observed_links = self._extract_internal_links(
                data.markdown_content,
                url,
                html=data.html,
                links=data.links,
            )
            sitemap_links: list[str] = []
            discovery: dict[str, object] = {
                "status": "disabled",
                "reason": "sitemap_discovery_disabled",
                "robots_url": "",
                "robots_status": "disabled",
                "sitemaps_read": [],
                "candidate_count": 0,
                "disallowed_count": 0,
                "errors": [],
            }
            if self.sitemap_discovery:
                try:
                    sitemap_links, discovery = self._discover_sitemap_links(url)
                except Exception as exc:
                    discovery = {
                        **discovery,
                        "status": "unavailable",
                        "reason": "discovery_exception",
                        "errors": [str(exc)[:160]],
                    }
            sitemap_page_metadata = {
                str(row.get("url") or "").rstrip("/"): row
                for row in discovery.get("known_pages") or []
                if isinstance(row, dict) and str(row.get("url") or "").strip()
            }
            sitemap_lastmod = {
                page_url: str(row.get("lastmod") or "")
                for page_url, row in sitemap_page_metadata.items()
            }
            all_candidates = list(
                dict.fromkeys([*observed_links, *sitemap_links])
            )
            observed_set = set(observed_links)
            sitemap_set = set(sitemap_links)
            sitemap_only_links = [
                candidate
                for candidate in sitemap_links
                if candidate not in observed_set
            ]
            vault_evidence_expansion = (
                os.environ.get("BRAND3_ENVIRONMENT", "").strip().lower()
                == "vault"
            )
            maximum_page_budget = (
                VAULT_MAX_OWNED_SUBPAGES
                if vault_evidence_expansion
                else MAX_OWNED_SUBPAGES
            )
            subpages_to_crawl = self._select_internal_links_to_crawl(
                all_candidates,
                url,
                sitemap_only_links=sitemap_only_links,
                sitemap_lastmod=sitemap_lastmod,
                maximum_budget=maximum_page_budget,
                strategic_role_page_limit=(
                    VAULT_MAX_STRATEGIC_ROLE_PAGES
                    if vault_evidence_expansion
                    else 6
                ),
                sitemap_exploration_limit=(
                    VAULT_MAX_SITEMAP_EXPLORATION_PAGES
                    if vault_evidence_expansion
                    else 2
                ),
                evidence_expansion=vault_evidence_expansion,
            )[:maximum_page_budget]
            selected_rows = [
                {
                    "url": selected_url,
                    "role": self._selection_link_role(
                        selected_url,
                        evidence_expansion=vault_evidence_expansion,
                    ),
                    "source": (
                        "observed+sitemap"
                        if selected_url in observed_set
                        and selected_url in sitemap_set
                        else "sitemap"
                        if selected_url in sitemap_set
                        else "observed"
                    ),
                    "status": "selected",
                }
                for selected_url in subpages_to_crawl
            ]
            data.page_selection = {
                "version": (
                    VAULT_OWNED_PAGE_SELECTION_VERSION
                    if vault_evidence_expansion
                    else OWNED_PAGE_SELECTION_VERSION
                ),
                "budget": len(subpages_to_crawl),
                "maximum_budget": maximum_page_budget,
                "observed_candidate_count": len(observed_links),
                "sitemap_candidate_count": len(sitemap_links),
                "unique_candidate_count": len(all_candidates),
                "discovery": discovery,
                "selected": selected_rows,
            }
            if vault_evidence_expansion:
                data.page_selection["profile"] = "vault_evidence_expansion"

            subpage_contents = []
            owned_fallback_urls = []
            for subpage_url in subpages_to_crawl:
                subpage_data = self.scrape(subpage_url, crawl_subpages=False)
                if subpage_data.markdown_content:
                    page_language = detect_page_language(
                        subpage_data.markdown_content,
                        html=subpage_data.html,
                    )
                    owned_fallback_urls.append(subpage_url)
                    subpage_contents.append(
                        f"\n\n---\n## Subpage: {subpage_url}\n{subpage_data.markdown_content}"
                    )
                    status = "captured"
                else:
                    status = "not_captured"
                for row in selected_rows:
                    if row["url"] == subpage_url:
                        row["status"] = status
                        if status == "captured":
                            row.update(
                                self._page_language_fields(page_language)
                            )
                        break

            if subpage_contents:
                data.markdown_content += "".join(subpage_contents)
                data.owned_fallback_urls = list(
                    dict.fromkeys(
                        data.owned_fallback_urls + owned_fallback_urls
                    )
                )
            known_urls = list(dict.fromkeys([url, *all_candidates]))
            known_pages = [
                {
                    "url": known_url,
                    "role": (
                        "homepage"
                        if known_url.rstrip("/") == url.rstrip("/")
                        else self._link_role(known_url)
                    ),
                    "source": (
                        "input"
                        if known_url.rstrip("/") == url.rstrip("/")
                        else "observed+sitemap"
                        if known_url in observed_set and known_url in sitemap_set
                        else "sitemap"
                        if known_url in sitemap_set
                        else "observed"
                    ),
                    "navigation_status": (
                        "homepage"
                        if known_url.rstrip("/") == url.rstrip("/")
                        else "linked_from_home"
                        if known_url in observed_set
                        else "sitemap_only"
                    ),
                    "lastmod": str(
                        (
                            sitemap_page_metadata.get(
                                known_url.rstrip("/"),
                                {},
                            )
                        ).get("lastmod")
                        or ""
                    ),
                }
                for known_url in known_urls
            ]
            visited_pages = [
                {
                    "url": url,
                    "role": "homepage",
                    "source": "input",
                    "navigation_status": "homepage",
                    "status": "captured",
                    "lastmod": str(
                        sitemap_page_metadata.get(url.rstrip("/"), {}).get(
                            "lastmod"
                        )
                        or ""
                    ),
                    **self._page_language_fields(homepage_language),
                },
                *[
                    {
                        **row,
                        "navigation_status": (
                            "linked_from_home"
                            if row["url"] in observed_set
                            else "sitemap_only"
                        ),
                        "lastmod": str(
                            sitemap_page_metadata.get(
                                str(row["url"]).rstrip("/"),
                                {},
                            ).get("lastmod")
                            or ""
                        ),
                    }
                    for row in selected_rows
                ],
            ]
            selected_set = {
                str(row.get("url") or "")
                for row in selected_rows
                if str(row.get("url") or "").strip()
            }
            not_visited_pages = []
            for row in known_pages:
                known_url = str(row.get("url") or "")
                if (
                    known_url.rstrip("/") == url.rstrip("/")
                    or known_url in selected_set
                ):
                    continue
                scored_eligible = bool(
                    self._score_internal_links(
                        [known_url],
                        url,
                        evidence_expansion=vault_evidence_expansion,
                    )
                )
                if vault_evidence_expansion:
                    sitemap_exploration_eligible = (
                        known_url in sitemap_only_links
                        and self._selection_link_role(
                            known_url,
                            evidence_expansion=True,
                        )
                        == "other"
                        and self._is_sitemap_exploration_candidate(known_url)
                    )
                else:
                    # Preserve the production v3 reason contract exactly.
                    sitemap_exploration_eligible = (
                        known_url in sitemap_set
                        and self._is_sitemap_exploration_candidate(known_url)
                    )
                eligible = scored_eligible or sitemap_exploration_eligible
                not_visited_pages.append(
                    {
                        **row,
                        "status": "not_visited",
                        "reason": (
                            "page_budget"
                            if eligible
                            else "low_priority"
                        ),
                    }
                )

            captured_page_count = 1 + len(owned_fallback_urls)
            known_page_count = len(known_pages)
            language_detection = summarize_page_languages(visited_pages)
            data.page_selection.update(
                {
                    "known_page_count": known_page_count,
                    "attempted_page_count": 1 + len(selected_rows),
                    "captured_page_count": captured_page_count,
                    "coverage_ratio": (
                        round(captured_page_count / known_page_count, 4)
                        if known_page_count
                        else 0.0
                    ),
                    "known_pages": known_pages,
                    "visited_pages": visited_pages,
                    "not_visited_pages": not_visited_pages,
                    "excluded_pages": [
                        row
                        for row in discovery.get("excluded_pages") or []
                        if isinstance(row, dict)
                    ],
                    "latest_lastmod": str(
                        discovery.get("latest_lastmod") or ""
                    ),
                    "language_detection": language_detection,
                    # Compatibility: before v2 this counted subpages only.
                    "captured_count": len(owned_fallback_urls),
                }
            )
            if vault_evidence_expansion:
                eligible_not_visited_count = sum(
                    1
                    for row in not_visited_pages
                    if row.get("reason") == "page_budget"
                )
                data.page_selection.update(
                    {
                        "eligible_not_visited_count": eligible_not_visited_count,
                        "budget_exhausted": (
                            len(selected_rows) >= maximum_page_budget
                            and eligible_not_visited_count > 0
                        ),
                    }
                )

        data.capture_provenance = self._capture_provenance(
            requested_url=url,
            final_url=final_url,
            provider=capture_provider,
            content=data.markdown_content,
        )
        return data

    @staticmethod
    def _page_language_fields(result: dict[str, object]) -> dict[str, object]:
        return {
            "language_detection_version": str(result.get("version") or ""),
            "observed_language": str(
                result.get("observed_language") or "und"
            ),
            "language_confidence": str(result.get("confidence") or "low"),
            "declared_language": str(result.get("declared_language") or ""),
            "declared_language_mismatch": bool(
                result.get("declared_mismatch")
            ),
            "language_token_count": int(result.get("token_count") or 0),
            "language_scores": dict(result.get("scores") or {}),
        }

    def scrape_multiple(self, urls: list[str]) -> list[WebData]:
        """Scrape multiple URLs."""
        return [self.scrape(url) for url in urls]

    @staticmethod
    def _firecrawl_metadata(value) -> dict:
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            dumped = value.model_dump()
            return dumped if isinstance(dumped, dict) else {}
        if hasattr(value, "dict"):
            dumped = value.dict()
            return dumped if isinstance(dumped, dict) else {}
        return {}

    @staticmethod
    def _capture_provenance(
        *,
        requested_url: str,
        final_url: str,
        provider: str,
        content: str,
    ) -> dict[str, object]:
        content_sha256 = (
            hashlib.sha256(content.encode("utf-8")).hexdigest()
            if content
            else ""
        )
        return {
            "version": WEB_CAPTURE_PROVENANCE_VERSION,
            "requested_url": requested_url,
            "final_url": final_url or requested_url,
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "provider": provider or "none",
            "content_sha256": content_sha256,
            "firecrawl_max_age_ms": (
                _FIRECRAWL_MAX_AGE_MS if provider == "firecrawl" else None
            ),
        }
