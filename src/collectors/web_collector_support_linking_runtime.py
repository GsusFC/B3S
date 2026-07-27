"""Internal link extraction and ranking helpers for web collector support."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse


_MAX_OWNED_SUBPAGES = 6
_OWNED_PAGE_ROLE_PRIORITY = (
    "product",
    "solutions",
    "about",
    "culture",
    "customers",
    "case_studies",
    "reviews",
    "testimonials",
    "pricing",
    "trust",
)


class WebCollectorLinkingSupport:
    """Composable helpers for choosing owned links to crawl."""

    def _extract_internal_links(
        self,
        markdown: str,
        base_url: str,
        *,
        html: str = "",
        links: list[str] | None = None,
    ) -> list[str]:
        """Extract absolute internal page links from observed markdown, HTML, and browser links."""
        if not base_url:
            return []

        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc.lower()
        if base_domain.startswith("www."):
            base_domain = base_domain[4:]

        candidates = []
        candidates.extend(re.findall(r"\[[^\]]*\]\(([^)]+)\)", markdown or ""))
        # Only navigation anchors are crawl candidates. Framework preload and
        # resource links also use href and must not consume the page budget.
        candidates.extend(
            re.findall(
                r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"']",
                html or "",
                flags=re.IGNORECASE,
            )
        )
        candidates.extend(links or [])

        internal_links = []
        seen = set()

        for link in candidates:
            link = str(link or "").strip()
            if (
                not link
                or link.startswith("#")
                or link.startswith("javascript:")
                or link.startswith("mailto:")
                or link.startswith("tel:")
            ):
                continue

            absolute_url = self._normalize_request_url(urljoin(base_url, link))

            try:
                parsed_link = urlparse(absolute_url)
                link_domain = parsed_link.netloc.lower()
                if link_domain.startswith("www."):
                    link_domain = link_domain[4:]

                if link_domain == base_domain and self._looks_like_page_link(parsed_link):
                    normalized = parsed_link._replace(fragment="").geturl().rstrip("/")
                    base_normalized = base_url.rstrip("/")
                    if normalized not in seen and normalized != base_normalized:
                        seen.add(normalized)
                        internal_links.append(normalized)
            except Exception:
                continue

        return internal_links

    @staticmethod
    def _looks_like_page_link(parsed_link) -> bool:
        path = str(getattr(parsed_link, "path", "") or "")
        query = str(getattr(parsed_link, "query", "") or "")
        lowered = path.lower()
        if lowered.startswith(("/_next/", "/static/", "/assets/", "/images/", "/img/")):
            return False
        blocked_extensions = (
            ".css",
            ".js",
            ".json",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
            ".ico",
            ".pdf",
            ".zip",
            ".mp4",
            ".mov",
            ".webm",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
        )
        if lowered.endswith(blocked_extensions):
            return False
        # Optimized image URLs can hide the real asset in ?url=... .
        for value in parse_qs(query).get("url", []):
            decoded = unquote(value).lower()
            if decoded.startswith(("/_next/", "/static/", "/assets/")) or decoded.endswith(
                blocked_extensions
            ):
                return False
        return True

    def _score_internal_links(self, links: list[str], base_url: str) -> list[str]:
        """Score and sort internal links based on relevance keywords."""
        del base_url
        high_value = {
            "pricing": 10,
            "precios": 10,
            "feature": 8,
            "product": 8,
            "producto": 8,
            "platform": 8,
            "plataforma": 8,
            "solution": 7,
            "solucion": 7,
            "manifesto": 12,
            "mission": 10,
            "principles": 10,
            "values": 10,
            "valores": 10,
            "culture": 8,
            "cultura": 8,
            "about": 6,
            "nosotros": 6,
            "company": 5,
            "how": 5,
            "service": 5,
            "servicio": 5,
            "technology": 5,
            "tecnologia": 5,
            "customer": 6,
            "customers": 6,
            "client": 6,
            "clientes": 6,
            "case-stud": 6,
            "success-stor": 6,
            "casos": 6,
            "reviews": 6,
            "resenas": 6,
            "reseñas": 6,
            "opiniones": 6,
            "testimonial": 6,
            "testimonio": 6,
        }
        low_value = {
            "blog": -5,
            "news": -5,
            "press": -5,
            "contact": -3,
            "contacto": -3,
            "support": -5,
            "soporte": -5,
            "help": -5,
            "faq": -5,
            "privacy": -10,
            "terms": -10,
            "condiciones": -10,
            "legal": -10,
            "login": -8,
            "signin": -8,
            "signup": -8,
            "register": -8,
        }

        scored_links = []
        for link in links:
            parsed = urlparse(link)
            path = parsed.path.lower()
            query = parsed.query.lower()

            score = 0
            for kw, weight in high_value.items():
                if kw in path or kw in query:
                    score += weight
            for kw, penalty in low_value.items():
                if kw in path or kw in query:
                    score += penalty
            if score >= -2:
                scored_links.append((score, link))

        scored_links.sort(key=lambda x: x[0], reverse=True)
        return [link for _, link in scored_links]

    def _select_internal_links_to_crawl(self, links: list[str], base_url: str) -> list[str]:
        scored_links = self._score_internal_links(links, base_url)
        selected: list[str] = []

        for role in _OWNED_PAGE_ROLE_PRIORITY:
            for link in scored_links:
                if link in selected:
                    continue
                if self._link_role(link) == role:
                    selected.append(link)
                    break
            if len(selected) >= _MAX_OWNED_SUBPAGES:
                return selected

        for link in scored_links:
            if link in selected:
                continue
            selected.append(link)
            if len(selected) >= _MAX_OWNED_SUBPAGES:
                break

        return selected

    @staticmethod
    def _link_role(link: str) -> str:
        path = urlparse(link).path.lower()
        if any(marker in path for marker in ("pricing", "precios", "plans")):
            return "pricing"
        if any(marker in path for marker in ("customer", "client", "clientes")):
            return "customers"
        if any(marker in path for marker in ("case-stud", "success-stor", "stories", "casos")):
            return "case_studies"
        if any(
            marker in path for marker in ("reviews", "resenas", "reseñas", "opiniones")
        ):
            return "reviews"
        if any(marker in path for marker in ("testimonial", "testimonio")):
            return "testimonials"
        if any(marker in path for marker in ("security", "trust", "privacy", "compliance")):
            return "trust"
        if any(
            marker in path
            for marker in ("values", "valores", "culture", "cultura", "careers", "jobs", "empleo")
        ):
            # Values and culture usually live on careers pages, which feed the
            # values/vision evidence the strategy blocks starve without.
            return "culture"
        if any(marker in path for marker in ("about", "company", "nosotros", "manifesto")):
            return "about"
        if any(marker in path for marker in ("solution", "solucion", "use-case", "industry")):
            return "solutions"
        if any(
            marker in path
            for marker in ("feature", "product", "producto", "platform", "plataforma")
        ):
            return "product"
        return "other"
