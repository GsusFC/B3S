"""Internal link extraction and ranking helpers for web collector support."""

from __future__ import annotations

import re
from urllib import robotparser
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from xml.etree import ElementTree


MAX_OWNED_SUBPAGES = 8
VAULT_MAX_OWNED_SUBPAGES = 24
_MAX_STRATEGIC_ROLE_PAGES = 6
_MAX_SITEMAP_EXPLORATION_PAGES = 2
VAULT_MAX_STRATEGIC_ROLE_PAGES = 14
VAULT_MAX_SITEMAP_EXPLORATION_PAGES = 12
_MAX_SITEMAP_FILES = 4
_MAX_SITEMAP_CANDIDATES = 200
OWNED_PAGE_SELECTION_VERSION = "owned-page-selection-v3"
VAULT_OWNED_PAGE_SELECTION_VERSION = "owned-page-selection-v4-vault-adaptive"
_OWNED_PAGE_ROLE_PRIORITY = (
    "product",
    "solutions",
    "about",
    "culture",
    "method",
    "research",
    "proof",
    "customers",
    "case_studies",
    "reviews",
    "testimonials",
    "pricing",
    "trust",
)


class WebCollectorLinkingSupport:
    """Composable helpers for choosing owned links to crawl."""

    def _discover_sitemap_links(
        self,
        base_url: str,
    ) -> tuple[list[str], dict[str, object]]:
        """Discover same-domain page candidates from robots and sitemaps."""

        parsed_base = urlparse(base_url)
        if not parsed_base.scheme or not parsed_base.netloc:
            return [], {
                "status": "unavailable",
                "reason": "invalid_base_url",
                "robots_url": "",
                "sitemaps_read": [],
                "candidate_count": 0,
                "disallowed_count": 0,
            }

        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        robots_url = urljoin(origin, "/robots.txt")
        robots_text, robots_error = self._fetch_text_resource(robots_url)
        sitemap_urls = self._sitemap_urls_from_robots(robots_text, origin)
        if not sitemap_urls:
            sitemap_urls = [urljoin(origin, "/sitemap.xml")]

        rules = None
        if robots_text:
            rules = robotparser.RobotFileParser()
            rules.set_url(robots_url)
            rules.parse(robots_text.splitlines())

        pending = [
            url
            for url in sitemap_urls
            if self._same_owned_domain(url, base_url)
        ][:_MAX_SITEMAP_FILES]
        visited: set[str] = set()
        sitemaps_read: list[str] = []
        candidates: list[str] = []
        seen_candidates: set[str] = set()
        known_pages: list[dict[str, str]] = []
        known_page_keys: set[str] = set()
        excluded_pages: list[dict[str, str]] = []
        errors: list[str] = []
        disallowed_count = 0

        if robots_error:
            errors.append(f"robots:{robots_error[:160]}")

        while pending and len(visited) < _MAX_SITEMAP_FILES:
            sitemap_url = pending.pop(0)
            if sitemap_url in visited:
                continue
            visited.add(sitemap_url)
            sitemap_text, sitemap_error = self._fetch_text_resource(sitemap_url)
            if sitemap_error:
                errors.append(f"sitemap:{sitemap_url}:{sitemap_error[:160]}")
                continue
            page_entries, nested_sitemaps = self._parse_sitemap(sitemap_text)
            sitemaps_read.append(sitemap_url)
            for nested_url in nested_sitemaps:
                absolute = urljoin(sitemap_url, nested_url)
                if (
                    absolute not in visited
                    and absolute not in pending
                    and self._same_owned_domain(absolute, base_url)
                    and len(visited) + len(pending) < _MAX_SITEMAP_FILES
                ):
                    pending.append(absolute)
            for entry in page_entries:
                candidate = str(entry.get("url") or "")
                lastmod = str(entry.get("lastmod") or "")
                absolute = self._normalize_request_url(
                    urljoin(sitemap_url, candidate)
                )
                parsed_candidate = urlparse(absolute)
                if not self._same_owned_domain(absolute, base_url):
                    continue
                normalized = parsed_candidate._replace(
                    fragment="",
                    query="",
                ).geturl().rstrip("/")
                normalized_key = normalized.rstrip("/")
                if not self._looks_like_page_link(parsed_candidate):
                    excluded_pages.append(
                        {
                            "url": normalized,
                            "reason": "resource_filtered",
                            "lastmod": lastmod,
                        }
                    )
                    continue
                if rules is not None and not rules.can_fetch("*", normalized):
                    disallowed_count += 1
                    excluded_pages.append(
                        {
                            "url": normalized,
                            "reason": "robots_disallowed",
                            "lastmod": lastmod,
                        }
                    )
                    continue
                if normalized_key not in known_page_keys:
                    known_page_keys.add(normalized_key)
                    known_pages.append(
                        {
                            "url": normalized,
                            "lastmod": lastmod,
                        }
                    )
                if normalized_key == base_url.rstrip("/"):
                    continue
                if normalized in seen_candidates:
                    continue
                seen_candidates.add(normalized)
                candidates.append(normalized)
                if len(candidates) >= _MAX_SITEMAP_CANDIDATES:
                    pending.clear()
                    break

        status = "discovered" if candidates else "empty"
        if not sitemaps_read and errors:
            status = "unavailable"
        return candidates, {
            "status": status,
            "reason": "",
            "robots_url": robots_url,
            "robots_status": "read" if robots_text else "unavailable",
            "sitemaps_read": sitemaps_read,
            "candidate_count": len(candidates),
            "known_page_count": len(known_pages),
            "known_pages": known_pages,
            "excluded_pages": excluded_pages[:50],
            "latest_lastmod": self._latest_lastmod(known_pages),
            "disallowed_count": disallowed_count,
            "errors": errors[:5],
        }

    @staticmethod
    def _sitemap_urls_from_robots(
        robots_text: str,
        origin: str,
    ) -> list[str]:
        urls: list[str] = []
        for raw_line in str(robots_text or "").splitlines():
            match = re.match(r"^\s*sitemap\s*:\s*(\S+)", raw_line, re.IGNORECASE)
            if not match:
                continue
            candidate = urljoin(origin, match.group(1).strip())
            if candidate not in urls:
                urls.append(candidate)
        return urls

    @staticmethod
    def _parse_sitemap(
        sitemap_text: str,
    ) -> tuple[list[dict[str, str]], list[str]]:
        try:
            root = ElementTree.fromstring(str(sitemap_text or "").strip())
        except ElementTree.ParseError:
            return [], []

        root_name = root.tag.rsplit("}", 1)[-1].lower()
        if root_name == "sitemapindex":
            locations = [
                str(node.text or "").strip()
                for node in root.iter()
                if node.tag.rsplit("}", 1)[-1].lower() == "loc"
                and str(node.text or "").strip()
            ]
            return [], locations

        entries: list[dict[str, str]] = []
        for node in root:
            if node.tag.rsplit("}", 1)[-1].lower() != "url":
                continue
            values = {
                child.tag.rsplit("}", 1)[-1].lower(): str(child.text or "").strip()
                for child in node
            }
            location = values.get("loc", "")
            if location:
                entries.append(
                    {
                        "url": location,
                        "lastmod": values.get("lastmod", ""),
                    }
                )
        return entries, []

    @staticmethod
    def _latest_lastmod(pages: list[dict[str, str]]) -> str:
        values = [
            str(page.get("lastmod") or "").strip()
            for page in pages
            if str(page.get("lastmod") or "").strip()
        ]
        return max(values, default="")

    @staticmethod
    def _same_owned_domain(candidate_url: str, base_url: str) -> bool:
        def normalized_host(value: str) -> str:
            host = urlparse(value).netloc.lower().split("@")[-1]
            if host.startswith("www."):
                host = host[4:]
            return host

        candidate_host = normalized_host(candidate_url)
        base_host = normalized_host(base_url)
        return bool(candidate_host and base_host and candidate_host == base_host)

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

    def _score_internal_links(
        self,
        links: list[str],
        base_url: str,
        *,
        evidence_expansion: bool = False,
    ) -> list[str]:
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
            "software": 8,
            "algorithm": 10,
            "algoritmo": 10,
            "method": 9,
            "metodo": 9,
            "método": 9,
            "science": 8,
            "ciencia": 8,
            "research": 9,
            "investigacion": 9,
            "investigación": 9,
            "validation": 8,
            "validacion": 8,
            "validación": 8,
            "evidence": 7,
            "result": 6,
            "impact": 6,
            "partner": 6,
            "detected": 7,
            "simulation": 7,
            "simulacion": 7,
            "simulación": 7,
            "signing": 6,
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
            "/signin": -8,
            "signup": -8,
            "register": -8,
            "/404": -20,
            "/qr": -5,
        }
        if evidence_expansion:
            # Vault needs editorial and announcement surfaces as discovery
            # paths. They are candidates for evidence extraction, not proof by
            # themselves; provenance validation remains downstream.
            low_value.pop("blog", None)
            low_value.pop("news", None)
            low_value.pop("press", None)
            evidence_route_weights = {
                "media": 7,
                "press": 6,
                "news": 5,
                "newsroom": 6,
                "insight": 5,
                "insights": 5,
                "investment": 6,
                "funding": 6,
                "round": 5,
                "partnership": 6,
                "partnerships": 6,
                "agreement": 6,
                "agreements": 6,
                "agree": 6,
                "acuerdo": 6,
                "acuerdos": 6,
                "alianza": 6,
                "alianzas": 6,
            }
        else:
            evidence_route_weights = {}

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
            route_tokens = self._route_tokens(link)
            score += sum(
                weight
                for marker, weight in evidence_route_weights.items()
                if marker in route_tokens
            )
            if score >= -2:
                scored_links.append((score, link))

        scored_links.sort(key=lambda x: x[0], reverse=True)
        return [link for _, link in scored_links]

    def _select_internal_links_to_crawl(
        self,
        links: list[str],
        base_url: str,
        *,
        sitemap_only_links: list[str] | None = None,
        sitemap_lastmod: dict[str, str] | None = None,
        maximum_budget: int = MAX_OWNED_SUBPAGES,
        strategic_role_page_limit: int = _MAX_STRATEGIC_ROLE_PAGES,
        sitemap_exploration_limit: int = _MAX_SITEMAP_EXPLORATION_PAGES,
        evidence_expansion: bool = False,
    ) -> list[str]:
        if maximum_budget <= 0:
            return []
        scored_links = self._score_internal_links(
            links,
            base_url,
            evidence_expansion=evidence_expansion,
        )
        selected: list[str] = []

        role_priority = list(_OWNED_PAGE_ROLE_PRIORITY)
        if evidence_expansion:
            proof_index = role_priority.index("proof") + 1
            role_priority.insert(proof_index, "editorial_hub")

        for role in role_priority:
            for link in scored_links:
                if link in selected:
                    continue
                if (
                    self._selection_link_role(
                        link,
                        evidence_expansion=evidence_expansion,
                    )
                    == role
                ):
                    selected.append(link)
                    break
            if len(selected) >= strategic_role_page_limit:
                break

        # A recognized strategic page is stronger evidence than an untyped
        # sitemap-only discovery. Keep role diversity first, then use any
        # remaining budget for additional strategic candidates before
        # exploring pages whose role is still unknown.
        for link in scored_links:
            if (
                link in selected
                or self._selection_link_role(
                    link,
                    evidence_expansion=evidence_expansion,
                )
                == "other"
            ):
                continue
            selected.append(link)
            if len(selected) >= maximum_budget:
                return selected

        exploration_added = 0
        lastmod_by_url = {
            str(url).rstrip("/"): str(lastmod or "")
            for url, lastmod in (sitemap_lastmod or {}).items()
        }
        sitemap_exploration = [
            link
            for link in sitemap_only_links or []
            if self._selection_link_role(
                link,
                evidence_expansion=evidence_expansion,
            )
            == "other"
            and self._is_sitemap_exploration_candidate(link)
        ]
        sitemap_exploration.sort(
            key=lambda link: lastmod_by_url.get(link.rstrip("/"), ""),
            reverse=True,
        )
        for link in sitemap_exploration:
            if link in selected:
                continue
            selected.append(link)
            exploration_added += 1
            if (
                len(selected) >= maximum_budget
                or exploration_added >= sitemap_exploration_limit
            ):
                break

        if len(selected) >= maximum_budget:
            return selected

        for link in scored_links:
            if link in selected:
                continue
            selected.append(link)
            if len(selected) >= maximum_budget:
                break

        if len(selected) >= maximum_budget:
            return selected

        # A site with few recognized strategic routes should not leave the
        # capture budget unused while recent, public sitemap pages remain.
        for link in sitemap_exploration:
            if link in selected:
                continue
            selected.append(link)
            if len(selected) >= maximum_budget:
                break

        return selected

    @classmethod
    def _selection_link_role(
        cls,
        link: str,
        *,
        evidence_expansion: bool = False,
    ) -> str:
        role = cls._link_role(link)
        if role != "other" or not evidence_expansion:
            return role

        path = urlparse(link).path.casefold().rstrip("/")
        last_segment = path.rsplit("/", 1)[-1]
        if last_segment in {
            "blog",
            "news",
            "newsroom",
            "press",
            "press-room",
            "media",
            "media-and-press",
            "insight",
            "insights",
        }:
            return "editorial_hub"
        route_tokens = cls._route_tokens(link)
        if any(
            marker in route_tokens
            for marker in (
                "investment",
                "funding",
                "round",
                "partnership",
                "partnerships",
                "agreement",
                "agreements",
                "agree",
                "acuerdo",
                "acuerdos",
                "alianza",
                "alianzas",
            )
        ):
            return "proof"
        return role

    @staticmethod
    def _route_tokens(link: str) -> set[str]:
        parsed = urlparse(link)
        route = f"{parsed.path} {parsed.query}".casefold()
        return {
            token
            for token in re.split(r"[^a-z0-9áéíóúüñ]+", route)
            if token
        }

    @staticmethod
    def _is_sitemap_exploration_candidate(link: str) -> bool:
        path = urlparse(link).path.casefold()
        excluded_markers = (
            "/legal",
            "privacy",
            "cookie",
            "terms",
            "/login",
            "/signin",
            "/signup",
            "/register",
            "/404",
            "/qr",
        )
        return not any(marker in path for marker in excluded_markers)

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
        if any(
            marker in path
            for marker in (
                "algorithm",
                "algoritmo",
                "method",
                "metodo",
                "método",
                "science",
                "ciencia",
                "how-it-work",
                "how-we",
            )
        ):
            return "method"
        if any(
            marker in path
            for marker in ("research", "investigacion", "investigación")
        ):
            return "research"
        if any(
            marker in path
            for marker in (
                "validation",
                "validacion",
                "validación",
                "evidence",
                "result",
                "impact",
                "partner",
                "detected",
            )
        ):
            return "proof"
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
            for marker in (
                "feature",
                "product",
                "producto",
                "platform",
                "plataforma",
                "software",
                "service",
                "servicio",
            )
        ):
            return "product"
        if any(
            marker in path
            for marker in (
                "simulation",
                "simulacion",
                "simulación",
                "signing",
            )
        ):
            return "solutions"
        return "other"
