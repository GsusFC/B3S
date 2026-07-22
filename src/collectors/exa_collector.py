"""
Exa collector for semantic search, competitor discovery, and AI visibility.

Uses Exa API for:
- Brand mention search (percepción)
- Competitor discovery (diferenciación)
- AI visibility probe (presencia)
- Content freshness (vitalidad)
"""

import concurrent.futures
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
import os
import re
import time
from urllib.parse import urlparse

from src.api_key_pool import ApiKeySource, shared_api_key_pool
from src.config import EXA_API_KEYS
from src.services.legal_identity import legal_name_aliases

_TRANSIENT_SEARCH_ATTEMPTS = 2
_TRANSIENT_SEARCH_DELAY_S = 1.5
_MAX_BRAND_DATA_WORKERS = 4
EXA_STRATEGY_VERSION = "precision_vnext_v2"


@dataclass
class ExaResult:
    """Single Exa search result."""
    url: str
    title: str
    text: str = ""
    highlights: list = field(default_factory=list)
    summary: str = ""
    score: float = 0.0
    published_date: str = ""
    intent: str = ""
    source_class: str = ""
    relation: str = ""
    classification_reason: str = ""
    requires_human_review: bool = False
    score_is_missing: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class ExaData:
    """Aggregated Exa data for a brand."""
    brand_name: str
    mentions: list[ExaResult] = field(default_factory=list)
    profiles: list[ExaResult] = field(default_factory=list)
    competitors: list[ExaResult] = field(default_factory=list)
    ai_visibility_results: list[ExaResult] = field(default_factory=list)
    news: list[ExaResult] = field(default_factory=list)
    raw_responses: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


class ExaCollector:
    """Collects data via Exa API."""

    _INTENT_PROFILES = {
        "default": {
            "type": "auto",
            "num_results": 10,
            "contents": {
                "highlights": {"max_characters": 4000},
                "text": {"max_characters": 5000},
            },
        },
        "mentions": {
            "type": "auto",
            "num_results": 15,
            "contents": {
                "highlights": {"max_characters": 4000},
                "text": {"max_characters": 5000},
            },
        },
        "owned_confirmation": {
            "type": "auto",
            "num_results": 10,
            "contents": {
                "highlights": {"max_characters": 4000},
                "text": {"max_characters": 5000},
            },
        },
        "external_mentions": {
            "type": "auto",
            "num_results": 10,
            "contents": {
                "highlights": {"max_characters": 4000},
                "text": {"max_characters": 5000},
            },
        },
        "external_profiles": {
            "type": "auto",
            "num_results": 10,
            "category": "company",
            "contents": {
                "highlights": {"max_characters": 3000},
                "text": {"max_characters": 4000},
            },
        },
        "competitors": {
            "type": "deep",
            "num_results": 10,
            "category": "company",
            "contents": {
                "highlights": {"max_characters": 3000},
                "text": {"max_characters": 4000},
            },
        },
        "news": {
            "type": "fast",
            "num_results": 10,
            "category": "news",
            "contents": {
                "highlights": {"max_characters": 3000},
                "text": {"max_characters": 3000},
            },
        },
        "ai_visibility": {
            "type": "deep",
            "num_results": 5,
            "contents": {
                "highlights": {"max_characters": 3000},
                "text": {"max_characters": 3500},
            },
        },
        "enrichment": {
            "type": "fast",
            "num_results": 5,
            "contents": {
                "highlights": {"max_characters": 2500},
                "text": {"max_characters": 3000},
            },
        },
    }

    _TECHNICAL_MARKERS = (
        "robots.txt",
        "sitemap.xml",
        "llms.txt",
        "schema.org",
        "/.well-known",
    )

    _MARKETPLACE_HOST_MARKERS = (
        "producthunt.com",
        "peerpush.net",
        "uneed.best",
        "devhunt.org",
        "sourceforge.net",
    )

    _PROFILE_HOST_MARKERS = (
        "linkedin.com",
        "crunchbase.com",
        "g2.com",
        "capterra.com",
        "producthunt.com",
        "trustpilot.com",
        "angellist.com",
        "angel.co",
        "insurtechcommunityhub.com",
        "startupshub.catalonia.com",
        "einforma.com",
        "datocapital.es",
        "expansion.com",
        "prospeo.io",
    )

    _NOISE_HOST_MARKERS = (
        "pinterest.",
        "youtube.com/watch",
        "amazon.",
    )

    _UNSUPPORTED_FILTERS_BY_CATEGORY = {
        "company": {
            "start_published_date",
            "end_published_date",
            "start_crawl_date",
            "end_crawl_date",
            "exclude_domains",
        },
        "people": {
            "start_published_date",
            "end_published_date",
            "start_crawl_date",
            "end_crawl_date",
            "exclude_domains",
        },
    }

    _FILTER_KEYS = (
        "include_domains",
        "exclude_domains",
        "start_published_date",
        "end_published_date",
        "start_crawl_date",
        "end_crawl_date",
        "category",
    )

    def __init__(self, api_key: ApiKeySource = None):
        configured_keys = api_key if api_key is not None else EXA_API_KEYS
        self._api_keys = shared_api_key_pool("exa", configured_keys)
        self.api_key = api_key
        self._client = None
        self._search_events: list[dict] = []
        self._search_events_lock = threading.Lock()
        self._deep_reasoning_enabled = os.environ.get("BRAND3_EXA_ENABLE_DEEP_REASONING", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._deep_reasoning_intents = {
            part.strip().lower()
            for part in os.environ.get("BRAND3_EXA_DEEP_REASONING_INTENTS", "").split(",")
            if part.strip()
        }
        self._include_competitor_intent = os.environ.get("BRAND3_EXA_INCLUDE_COMPETITOR_INTENT", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._mentions_start_crawl_days = self._env_int("BRAND3_EXA_MENTIONS_START_CRAWL_DAYS")
        self._enrichment_start_crawl_days = self._env_int("BRAND3_EXA_ENRICHMENT_START_CRAWL_DAYS")

    @property
    def client(self):
        if self._client is not None:
            return self._client
        from exa_py import Exa
        api_key = self._api_keys.next_key()
        if not api_key:
            raise ValueError("EXA_API_KEY not set")
        client = Exa(api_key=api_key)
        if self._api_keys.size == 1:
            self._client = client
        return client

    @staticmethod
    def _domain_anchor(brand_url: str | None) -> str:
        if not brand_url:
            return ""
        parsed = urlparse(brand_url)
        hostname = (parsed.netloc or parsed.path or "").lower()
        hostname = hostname.replace("www.", "").strip("/")
        return hostname

    def _brand_query(
        self,
        brand_name: str,
        brand_url: str | None,
        suffix: str,
        *,
        legal_name: str | None = None,
    ) -> str:
        domain_anchor = self._domain_anchor(brand_url)
        parts = [f'"{brand_name}"']
        if domain_anchor:
            parts.append(f'"{domain_anchor}"')
        if legal_name:
            parts.append(f'"{legal_name}"')
        parts.append(suffix)
        return " ".join(part for part in parts if part)

    def _owned_confirmation_query(self, brand_name: str, brand_url: str | None) -> str:
        return self._brand_query(brand_name, brand_url, "official website product company about services")

    def _external_profiles_query(self, brand_name: str, brand_url: str | None, legal_name: str | None = None) -> str:
        return self._brand_query(
            brand_name,
            brand_url,
            "company profile linkedin crunchbase business directory services",
            legal_name=legal_name,
        )

    def _external_mentions_query(self, brand_name: str, brand_url: str | None, legal_name: str | None = None) -> str:
        return self._brand_query(
            brand_name,
            brand_url,
            "press mention media coverage client testimonial case study review",
            legal_name=legal_name,
        )

    def _news_query(self, brand_name: str, brand_url: str | None, legal_name: str | None = None) -> str:
        return self._brand_query(
            brand_name,
            brand_url,
            "press release media coverage announcement featured in",
            legal_name=legal_name,
        )

    def _ai_visibility_query(self, brand_name: str, brand_url: str | None) -> str:
        return self._brand_query(
            brand_name,
            brand_url,
            "what is this company services expertise overview",
        )

    def _intent_params(self, intent: str, num_results: int | None, **kwargs) -> dict:
        profile = dict(self._INTENT_PROFILES.get(intent or "", self._INTENT_PROFILES["default"]))
        params = {**profile, **kwargs}
        if num_results is not None:
            params["num_results"] = num_results
        if intent == "news" and "start_published_date" not in params:
            params["start_published_date"] = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
        if intent == "mentions" and "start_crawl_date" not in params and self._mentions_start_crawl_days:
            params["start_crawl_date"] = (
                datetime.now(timezone.utc) - timedelta(days=self._mentions_start_crawl_days)
            ).date().isoformat()
        if intent == "enrichment" and "start_crawl_date" not in params and self._enrichment_start_crawl_days:
            params["start_crawl_date"] = (
                datetime.now(timezone.utc) - timedelta(days=self._enrichment_start_crawl_days)
            ).date().isoformat()
        return {k: v for k, v in params.items() if v is not None}

    @staticmethod
    def _brand_token(brand_name: str) -> str:
        for token in (brand_name or "").lower().replace("-", " ").replace("_", " ").split():
            token = "".join(ch for ch in token if ch.isalnum())
            if len(token) >= 4:
                return token
        return ""

    @staticmethod
    def _normalize_text(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())

    @classmethod
    def _brand_aliases(cls, brand_name: str, brand_url: str | None, legal_name: str | None = None) -> set[str]:
        aliases: set[str] = set()
        domain_anchor = cls._domain_anchor(brand_url)
        if domain_anchor:
            aliases.add(cls._normalize_text(domain_anchor))
            root_label = domain_anchor.split(".", 1)[0]
            if root_label and root_label not in {"www", "com", "net", "org", "io", "ai", "co", "es"}:
                aliases.add(cls._normalize_text(root_label))
        normalized_name = cls._normalize_text(brand_name)
        if normalized_name and normalized_name not in {"www", "company"}:
            aliases.add(normalized_name)
        aliases.update(legal_name_aliases(legal_name))
        return {alias for alias in aliases if len(alias) >= 4}

    @classmethod
    def _entity_match_score(
        cls,
        *,
        result,
        brand_name: str,
        brand_url: str | None,
        legal_name: str | None = None,
    ) -> tuple[float, str]:
        aliases = cls._brand_aliases(brand_name, brand_url, legal_name=legal_name)
        if not aliases:
            return 0.0, "no_brand_alias"

        host = cls._host(getattr(result, "url", "") or "")
        host_norm = cls._normalize_text(host)
        title_norm = cls._normalize_text(getattr(result, "title", "") or "")
        text_norm = cls._normalize_text(
            ((getattr(result, "text", "") or "") + " " + (getattr(result, "summary", "") or ""))
        )

        for alias in aliases:
            if alias and alias in host_norm:
                return 1.0, "alias_in_host"
        for alias in aliases:
            if alias and alias in title_norm:
                return 0.95, "alias_in_title"
        for alias in aliases:
            if alias and alias in text_norm:
                return 0.7, "alias_in_text"
        return 0.0, "no_alias_match"

    @classmethod
    def _should_accept_result(
        cls,
        *,
        result,
        intent: str,
        brand_name: str,
        brand_url: str | None,
        legal_name: str | None = None,
    ) -> tuple[bool, str, float]:
        source_class, relation, _, _ = cls._classify_source(
            url=getattr(result, "url", ""),
            brand_url=brand_url,
            brand_name=brand_name,
        )
        match_score, match_reason = cls._entity_match_score(
            result=result,
            brand_name=brand_name,
            brand_url=brand_url,
            legal_name=legal_name,
        )

        if intent == "owned_confirmation":
            if source_class == "owned":
                return True, "owned_surface", 1.0
            return False, "owned_confirmation_requires_owned_surface", match_score

        if intent in {"external_profiles", "external_mentions", "news"}:
            if source_class == "owned":
                return False, "owned_surface_excluded_from_external_intent", match_score
            if source_class in {"technical_internal", "noise"}:
                return False, "non_market_source_class", match_score
            if match_score >= 0.95:
                return True, match_reason, match_score
            return False, f"weak_entity_match:{match_reason}", match_score

        if intent == "ai_visibility":
            if source_class == "owned" and match_score >= 0.95:
                return True, "owned_alias_match", match_score
            if source_class != "owned" and match_score >= 0.95:
                return True, match_reason, match_score
            return False, f"weak_entity_match:{match_reason}", match_score

        return True, "default_accept", match_score

    @staticmethod
    def _host(url: str | None) -> str:
        if not url:
            return ""
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.netloc or parsed.path or "").lower().strip("/")
        return host.removeprefix("www.")

    @staticmethod
    def _root(host: str) -> str:
        if not host:
            return ""
        parts = host.split(".")
        if len(parts) <= 2:
            return host
        return ".".join(parts[-2:])

    @classmethod
    def _classify_source(
        cls,
        *,
        url: str,
        brand_url: str | None,
        brand_name: str,
    ) -> tuple[str, str, str, bool]:
        host = cls._host(url)
        root = cls._root(host)
        brand_host = cls._host(brand_url)
        brand_root = cls._root(brand_host)
        brand_token = cls._brand_token(brand_name)
        haystack = f"{host} {url}".lower()

        if host and host == brand_host:
            return ("owned", "audited_surface", "same_host_owned_surface", False)
        if host and brand_root and root == brand_root:
            return ("owned", "same_root_surface", "same_root_owned_surface", False)
        if any(marker in haystack for marker in cls._TECHNICAL_MARKERS):
            return ("technical_internal", "external", "technical_artifact", False)
        if any(marker in haystack for marker in cls._MARKETPLACE_HOST_MARKERS):
            return ("marketplace_listing", "external", "marketplace_listing_review_gated", True)
        if any(marker in haystack for marker in cls._NOISE_HOST_MARKERS):
            return ("noise", "external", "likely_noise_source", True)
        if brand_token and brand_token in host and root and brand_root and root != brand_root:
            return ("related_unresolved", "unresolved", "same_name_different_root_domain", True)
        return ("external", "external", "external_candidate", False)

    def _record_event(self, event: dict) -> None:
        with self._search_events_lock:
            self._search_events.append(event)

    @staticmethod
    def _env_int(name: str) -> int | None:
        value = os.environ.get(name)
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _latency_bucket(elapsed_ms: int) -> str:
        if elapsed_ms < 1000:
            return "sub_1s"
        if elapsed_ms < 5000:
            return "1s_to_5s"
        if elapsed_ms < 20000:
            return "5s_to_20s"
        return "over_20s"

    @staticmethod
    def _linkedin_only_domains(domains: list[str]) -> list[str]:
        allowed: list[str] = []
        for domain in domains:
            host = ExaCollector._host(domain)
            if host == "linkedin.com" or host.endswith(".linkedin.com"):
                allowed.append(domain)
        return allowed

    @classmethod
    def _sanitize_params(cls, params: dict) -> tuple[dict, list[dict]]:
        category = str(params.get("category") or "").lower()
        sanitized = dict(params)
        stripped: list[dict] = []
        unsupported = cls._UNSUPPORTED_FILTERS_BY_CATEGORY.get(category, set())
        for key in tuple(unsupported):
            if key in sanitized:
                stripped.append(
                    {
                        "param": key,
                        "reason": f"unsupported_for_category:{category}",
                        "value": sanitized.pop(key),
                    }
                )
        if category == "people" and "include_domains" in sanitized:
            original = list(sanitized.get("include_domains") or [])
            linkedin_domains = cls._linkedin_only_domains(original)
            if linkedin_domains:
                if linkedin_domains != original:
                    stripped.append(
                        {
                            "param": "include_domains",
                            "reason": "non_linkedin_domains_removed_for_people_category",
                            "value": [d for d in original if d not in linkedin_domains],
                        }
                    )
                sanitized["include_domains"] = linkedin_domains
            else:
                stripped.append(
                    {
                        "param": "include_domains",
                        "reason": "unsupported_for_category:people_without_linkedin_domains",
                        "value": original,
                    }
                )
                sanitized.pop("include_domains", None)
        return sanitized, stripped

    def _resolve_search_type(self, intent: str, configured_type: str) -> tuple[str, bool]:
        if not self._deep_reasoning_enabled:
            return configured_type, False
        if intent not in self._deep_reasoning_intents:
            return configured_type, False
        if configured_type != "deep":
            return configured_type, False
        return "deep-reasoning", True

    def _search_with_retry(self, query: str, params: dict):
        """One retry on transient search failures.

        Each attempt borrows the next configured key. With a single key this
        preserves the previous retry behaviour; with a pool it fails over
        across independent credentials without exposing them in diagnostics.
        """
        if not self._api_keys:
            raise ValueError("EXA_API_KEY not set")
        attempts = max(_TRANSIENT_SEARCH_ATTEMPTS, self._api_keys.size)
        for attempt in range(attempts):
            try:
                return self.client.search(query, **params)
            except Exception:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(_TRANSIENT_SEARCH_DELAY_S)

    @staticmethod
    def _result_has_content(result) -> bool:
        for attr in ("text", "summary"):
            if str(getattr(result, attr, "") or "").strip():
                return True
        highlights = getattr(result, "highlights", None)
        if isinstance(highlights, list) and any(str(item or "").strip() for item in highlights):
            return True
        return False

    def search(
        self,
        query: str,
        num_results: int | None = None,
        *,
        intent: str = "default",
        brand_name: str = "",
        brand_url: str | None = None,
        legal_name: str | None = None,
        **kwargs,
    ) -> list[ExaResult]:
        """Run a search query via Exa."""
        params = self._intent_params(intent, num_results, **kwargs)
        configured_type = str(params.get("type") or "")
        effective_type, deep_reasoning_applied = self._resolve_search_type(intent, configured_type)
        params["type"] = effective_type
        params, stripped_filters = self._sanitize_params(params)
        applied_filters = {k: params.get(k) for k in self._FILTER_KEYS if k in params}
        start = time.perf_counter()
        try:
            response = self._search_with_retry(query, params)
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            print(f"Exa search error: {e}")
            self._record_event(
                {
                    "intent": intent,
                    "query": query,
                    "status": "search_failed",
                    "result_count": 0,
                    "error": str(e),
                    "params": {k: v for k, v in params.items() if k != "contents"},
                    "configured_type": configured_type,
                    "effective_type": effective_type,
                    "deep_reasoning_enabled": self._deep_reasoning_enabled,
                    "deep_reasoning_intents": sorted(self._deep_reasoning_intents),
                    "deep_reasoning_applied": deep_reasoning_applied,
                    "applied_filters": applied_filters,
                    "stripped_filters": stripped_filters,
                    "elapsed_ms": elapsed_ms,
                    "latency_bucket": self._latency_bucket(elapsed_ms),
                    "unresolved_collision_count": 0,
                }
            )
            return []

        results = []
        filtered_empty_content_count = 0
        filtered_irrelevant_count = 0
        for r in response.results:
            if getattr(r, "url", "") and not self._result_has_content(r):
                filtered_empty_content_count += 1
                continue
            source_class, relation, reason, requires_review = self._classify_source(
                url=getattr(r, "url", ""),
                brand_url=brand_url,
                brand_name=brand_name,
            )
            accepted, acceptance_reason, match_score = self._should_accept_result(
                result=r,
                intent=intent,
                brand_name=brand_name,
                brand_url=brand_url,
                legal_name=legal_name,
            )
            if not accepted:
                filtered_irrelevant_count += 1
                continue
            raw_score = getattr(r, "score", None)
            results.append(ExaResult(
                url=r.url,
                title=getattr(r, "title", ""),
                text=getattr(r, "text", ""),
                highlights=getattr(r, "highlights", []) or [],
                summary=getattr(r, "summary", ""),
                score=raw_score if raw_score is not None else 0.0,
                published_date=str(getattr(r, "published_date", "")),
                intent=intent,
                source_class=source_class,
                relation=relation,
                classification_reason=reason,
                requires_human_review=requires_review,
                score_is_missing=raw_score is None,
                metadata={
                    "entity_match_score": match_score,
                    "entity_match_reason": acceptance_reason,
                },
            ))
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        self._record_event(
            {
                "intent": intent,
                "query": query,
                "status": "no_results" if not results else "ok",
                "result_count": len(results),
                "filtered_empty_content_count": filtered_empty_content_count,
                "filtered_irrelevant_count": filtered_irrelevant_count,
                "params": {k: v for k, v in params.items() if k != "contents"},
                "score_missing_count": sum(1 for item in results if item.score_is_missing),
                "configured_type": configured_type,
                "effective_type": effective_type,
                "deep_reasoning_enabled": self._deep_reasoning_enabled,
                "deep_reasoning_intents": sorted(self._deep_reasoning_intents),
                "deep_reasoning_applied": deep_reasoning_applied,
                "applied_filters": applied_filters,
                "stripped_filters": stripped_filters,
                "elapsed_ms": elapsed_ms,
                "latency_bucket": self._latency_bucket(elapsed_ms),
                "unresolved_collision_count": sum(
                    1 for item in results if item.source_class == "related_unresolved"
                ),
            }
        )
        return results

    def collect_brand_data(
        self,
        brand_name: str,
        brand_url: str = None,
        *,
        legal_name: str | None = None,
    ) -> ExaData:
        """Collect all Exa data for a brand."""
        with self._search_events_lock:
            self._search_events = []
        data = ExaData(brand_name=brand_name)
        domain_anchor = self._domain_anchor(brand_url)

        tasks = {
            "owned_confirmation": lambda: self.search(
                self._owned_confirmation_query(brand_name, brand_url),
                intent="owned_confirmation",
                brand_name=brand_name,
                brand_url=brand_url,
                legal_name=legal_name,
                include_domains=[domain_anchor] if domain_anchor else None,
            ),
            "external_profiles": lambda: self.search(
                self._external_profiles_query(brand_name, brand_url, legal_name),
                intent="external_profiles",
                brand_name=brand_name,
                brand_url=brand_url,
                legal_name=legal_name,
            ),
            "external_mentions": lambda: self.search(
                self._external_mentions_query(brand_name, brand_url, legal_name),
                intent="external_mentions",
                brand_name=brand_name,
                brand_url=brand_url,
                legal_name=legal_name,
                exclude_domains=[domain_anchor] if domain_anchor else None,
            ),
            "news": lambda: self.search(
                self._news_query(brand_name, brand_url, legal_name),
                intent="news",
                brand_name=brand_name,
                brand_url=brand_url,
                legal_name=legal_name,
                exclude_domains=[domain_anchor] if domain_anchor else None,
            ),
            "ai_visibility": lambda: self.probe_ai_visibility(
                brand_name,
                brand_url=brand_url,
                legal_name=legal_name,
            ),
        }
        if domain_anchor and self._include_competitor_intent:
            tasks["competitors"] = lambda: self.search(
                f"alternatives competitors similar to {brand_name} {domain_anchor} category",
                intent="competitors",
                brand_name=brand_name,
                brand_url=brand_url,
                exclude_domains=[domain_anchor] if domain_anchor else None,
            )

        results = self._run_brand_data_tasks(tasks)
        external_mentions = results.get("external_mentions") or []
        promoted_profiles = [item for item in external_mentions if self._is_profile_like_result(item)]
        independent_mentions = [item for item in external_mentions if not self._is_profile_like_result(item)]
        data.mentions = (
            (results.get("owned_confirmation") or [])
            + independent_mentions
        )
        data.profiles = (results.get("external_profiles") or []) + promoted_profiles
        data.competitors = results.get("competitors") or []
        data.news = results.get("news") or []
        data.ai_visibility_results = results.get("ai_visibility") or []
        data.diagnostics = self._build_diagnostics()
        if legal_name:
            data.diagnostics["identity_anchors"] = {
                "brand_name": brand_name,
                "brand_url": brand_url or "",
                "legal_name": legal_name,
                "alias_count": len(self._brand_aliases(brand_name, brand_url, legal_name=legal_name)),
            }
        with self._search_events_lock:
            data.raw_responses = {"search_events": list(self._search_events)}

        return data

    @staticmethod
    def _run_brand_data_tasks(tasks: dict[str, Callable[[], list[ExaResult]]]) -> dict[str, list[ExaResult]]:
        if len(tasks) <= 1:
            return {name: task() for name, task in tasks.items()}
        results: dict[str, list[ExaResult]] = {}
        max_workers = min(_MAX_BRAND_DATA_WORKERS, len(tasks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_name = {pool.submit(task): name for name, task in tasks.items()}
            for future in concurrent.futures.as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    results[name] = future.result()
                except Exception:
                    results[name] = []
        return results

    def probe_ai_visibility(
        self,
        brand_name: str,
        brand_url: str | None = None,
        *,
        legal_name: str | None = None,
    ) -> list[ExaResult]:
        """
        Check if the brand appears in AI-related content.
        Proxies 'AI visibility' — do LLMs know this brand?
        """
        return self.search(
            self._ai_visibility_query(brand_name, brand_url),
            intent="ai_visibility",
            brand_name=brand_name,
            brand_url=brand_url,
            legal_name=legal_name,
        )

    def _build_diagnostics(self) -> dict:
        by_intent: dict[str, dict] = {}
        latency_by_intent: dict[str, dict[str, int]] = {}
        unresolved_collision_count = 0
        for event in self._search_events:
            intent = str(event.get("intent") or "default")
            by_intent[intent] = {
                "status": event.get("status"),
                "result_count": int(event.get("result_count") or 0),
                "error": event.get("error") or "",
                "query": event.get("query") or "",
                "params": event.get("params") or {},
                "filtered_empty_content_count": int(event.get("filtered_empty_content_count") or 0),
                "filtered_irrelevant_count": int(event.get("filtered_irrelevant_count") or 0),
                "score_missing_count": int(event.get("score_missing_count") or 0),
                "configured_type": event.get("configured_type") or "",
                "effective_type": event.get("effective_type") or "",
                "deep_reasoning_enabled": bool(event.get("deep_reasoning_enabled")),
                "deep_reasoning_intents": event.get("deep_reasoning_intents") or [],
                "deep_reasoning_applied": bool(event.get("deep_reasoning_applied")),
                "applied_filters": event.get("applied_filters") or {},
                "stripped_filters": event.get("stripped_filters") or [],
                "elapsed_ms": int(event.get("elapsed_ms") or 0),
                "latency_bucket": event.get("latency_bucket") or "",
                "unresolved_collision_count": int(event.get("unresolved_collision_count") or 0),
            }
            unresolved_collision_count += int(event.get("unresolved_collision_count") or 0)
            bucket = str(event.get("latency_bucket") or "")
            if bucket:
                latency_by_intent.setdefault(intent, {})
                latency_by_intent[intent][bucket] = latency_by_intent[intent].get(bucket, 0) + 1

        failed_intents = [intent for intent, payload in by_intent.items() if payload.get("status") == "search_failed"]
        no_result_intents = [intent for intent, payload in by_intent.items() if payload.get("status") == "no_results"]
        return {
            "strategy": EXA_STRATEGY_VERSION,
            "strategy_notes": [
                "owned_confirmation, external_profiles, external_mentions, news, and ai_visibility are default production intents.",
                "competitors intent is opt-in via BRAND3_EXA_INCLUDE_COMPETITOR_INTENT=1.",
                "Use separate CompetitorCollector for full competitive analysis.",
            ],
            "competitor_intent_enabled": self._include_competitor_intent,
            "planned_intents": [
                "owned_confirmation",
                "external_profiles",
                "external_mentions",
                "news",
                "ai_visibility",
            ]
            + (["competitors"] if self._include_competitor_intent else []),
            "status": "degraded" if failed_intents else "ok",
            "failed_intents": failed_intents,
            "no_result_intents": no_result_intents,
            "intent_results": by_intent,
            "latency_buckets_by_intent": latency_by_intent,
            "unresolved_collision_count": unresolved_collision_count,
        }

    @classmethod
    def _is_profile_like_result(cls, result: ExaResult) -> bool:
        source_class = str(getattr(result, "source_class", "") or "").strip().lower()
        if source_class == "marketplace_listing":
            return True
        host = cls._host(getattr(result, "url", "") or "")
        return any(host == marker or host.endswith(f".{marker}") for marker in cls._PROFILE_HOST_MARKERS)
