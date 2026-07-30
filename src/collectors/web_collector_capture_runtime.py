"""Low-level capture fallbacks for web collector."""

from __future__ import annotations

import gzip
from urllib.error import URLError
from urllib.request import Request, urlopen

_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
_MAX_DISCOVERY_RESOURCE_BYTES = 2_000_000
_DISCOVERY_RESOURCE_TIMEOUT_SECONDS = 5


class WebCollectorCaptureSupport:
    """Fallback HTML/JS rendering capture helpers for web collector."""

    def _fetch_text_resource(self, url: str) -> tuple[str, str]:
        """Fetch a small public discovery resource such as robots or sitemap."""

        url = self._normalize_request_url(url)
        request = Request(
            url,
            headers={
                "User-Agent": _DESKTOP_USER_AGENT,
                "Accept": "text/plain, application/xml, text/xml;q=0.9, */*;q=0.1",
            },
        )
        try:
            with urlopen(
                request,
                timeout=_DISCOVERY_RESOURCE_TIMEOUT_SECONDS,
            ) as response:
                raw = response.read(_MAX_DISCOVERY_RESOURCE_BYTES + 1)
                if len(raw) > _MAX_DISCOVERY_RESOURCE_BYTES:
                    return "", "discovery_resource_too_large"
                if raw.startswith(b"\x1f\x8b"):
                    raw = gzip.decompress(raw)
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace"), ""
        except (OSError, URLError, TimeoutError, ValueError) as exc:
            return "", str(exc)

    def _fetch_html_fallback(self, url: str) -> tuple[str, str]:
        """Fetch raw HTML directly when Firecrawl returns no useful markdown."""
        url = self._normalize_request_url(url)
        request = Request(
            url,
            headers={
                "User-Agent": _DESKTOP_USER_AGENT
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(charset, errors="replace")
                return html, ""
        except (URLError, TimeoutError, ValueError) as exc:
            return "", str(exc)

    def _fetch_browser_fallback(self, url: str) -> tuple[dict, str]:
        """Render a page in Chromium when static fetches cannot see useful text."""
        url = self._normalize_request_url(url)
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            return {}, f"playwright unavailable: {exc}"

        browser = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=_DESKTOP_USER_AGENT,
                    viewport={"width": 1440, "height": 1200},
                )
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except PlaywrightTimeoutError:
                    pass

                self._dismiss_cookie_banners(page)

                html = page.content()
                title = page.title() or ""
                body_text = page.locator("body").inner_text(timeout=5000)
                meta_description = page.evaluate(
                    """() => {
                        const el = document.querySelector("meta[name='description'], meta[property='og:description']");
                        return el ? (el.getAttribute("content") || "") : "";
                    }"""
                )
                canonical_url = page.evaluate(
                    """() => {
                        const canonical = document.querySelector("link[rel='canonical']");
                        if (canonical) return canonical.href || canonical.getAttribute("href") || "";
                        const og = document.querySelector("meta[property='og:url']");
                        return og ? (og.getAttribute("content") || "") : "";
                    }"""
                )
                links = page.eval_on_selector_all(
                    "a[href]",
                    """els => els.map(a => a.href).filter(Boolean).slice(0, 80)""",
                )
                status = response.status if response else None
                context.close()
                browser.close()
                browser = None
                return {
                    "status": status,
                    "title": title,
                    "meta_description": meta_description,
                    "canonical_url": canonical_url,
                    "body_text": body_text,
                    "html": html,
                    "links": links,
                }, ""
        except Exception as exc:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            return {}, str(exc)

    def _dismiss_cookie_banners(self, page) -> None:
        """Attempt to click typical cookie consent buttons with short timeouts."""
        combined_selector = (
            "button:has-text('Aceptar'), button:has-text('Accept'), button:has-text('Rechazar'), "
            "button:has-text('Cerrar'), button:has-text('Agree'), button:has-text('Allow all'), "
            "button:has-text('Accept all'), button:has-text('Close'), button:has-text('OK'), "
            "a:has-text('Aceptar'), a:has-text('Accept'), a:has-text('Cerrar'), a:has-text('Close'), "
            "#cookie-accept, #accept-cookies, .cookie-accept, .accept-cookies"
        )
        try:
            page.locator(combined_selector).first.click(timeout=500)
        except Exception:
            pass
