"""External-source raw input collection helpers."""

from __future__ import annotations

import logging
import os

from src.collectors.github_proof_collector import (
    GitHubProofCollector,
    GitHubProofData,
    observed_github_urls_from_web_data,
)
from src.collectors.hyperbrowser_collector import HyperbrowserCollector, HyperbrowserFetchData
from src.collectors.parallel_shadow_collector import ParallelShadowCollector, ParallelShadowData
from src.collectors.searchapi_collector import SearchApiCollector, SearchApiData
from src.config import (
    BRAND3_CACHE_TTL_HOURS,
    BRAND3_GITHUB_PROOF_ENABLED,
    BRAND3_HYPERBROWSER_ENABLED,
    BRAND3_SEARCHAPI_FALLBACK_INTENTS,
    BRAND3_SEARCHAPI_VERTICAL_FALLBACK_ENABLED,
    SEARCHAPI_API_KEY,
)
from src.services.input_collection_payloads import (
    from_github_payload,
    from_hyperbrowser_payload,
    from_parallel_shadow_payload,
    from_searchapi_payload,
)
from src.services.exa_diagnostics import exa_external_proof_empty
from src.services.input_collection_state import (
    AcquisitionResult,
    _save_raw_input_safely,
    _set_acquisition_state,
    _use_cached_input,
)
from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def _parallel_shadow_enabled() -> bool:
    return os.environ.get("BRAND3_PARALLEL_SHADOW_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _hyperbrowser_enabled(run_input_sources: set[str] | None = None) -> bool:
    if run_input_sources is not None:
        return "hyperbrowser" in run_input_sources
    return BRAND3_HYPERBROWSER_ENABLED


def _github_proof_enabled(run_input_sources: set[str] | None = None) -> bool:
    if run_input_sources is not None:
        return "github" in run_input_sources or "github_proof" in run_input_sources
    return BRAND3_GITHUB_PROOF_ENABLED


def _collect_github_proof_input(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    brand_name: str,
    effective_brand_url: str,
    web_data,
    cache_read,
    raw_input_cache: dict[str, str],
    acquisition_steps: dict[str, AcquisitionResult] | None = None,
    run_input_sources: set[str] | None = None,
    github_collector_cls=GitHubProofCollector,
) -> GitHubProofData | None:
    if not _github_proof_enabled(run_input_sources):
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="github",
            raw_cache_status="disabled",
            status="disabled",
            cache_status="disabled",
            eligible=False,
        )
        return None

    observed_urls = observed_github_urls_from_web_data(web_data)
    if not observed_urls:
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="github",
            raw_cache_status="skipped",
            status="skipped",
            cache_status="skipped",
            eligible=False,
            details={"reason": "no GitHub repository links observed on owned capture"},
        )
        return None

    cached = cache_read("github", BRAND3_CACHE_TTL_HOURS, from_github_payload)
    if cached:
        _use_cached_input(
            store=store,
            run_id=run_id,
            source="github",
            payload=cached,
            raw_input_cache=raw_input_cache,
            acquisition_steps=acquisition_steps,
            details={"repos": len(cached.repos), "status": cached.status},
            action="github proof cache save",
        )
        return cached

    collector = github_collector_cls()
    data = collector.collect(brand_name, effective_brand_url, observed_urls)
    raw_status = "miss" if data.status in {"ok", "partial"} else data.status
    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="github",
        raw_cache_status=raw_status,
        status=data.status,
        cache_status="miss",
        eligible=data.status not in {"skipped", "error"},
        details={
            "repos": len(data.repos),
            "observed_repo_count": data.diagnostics.get("observed_repo_count", 0),
            "errors": data.diagnostics.get("errors", []),
        },
    )
    logger.info(
        "github proof input collected",
        extra={"source": "github", "status": data.status, "repos": len(data.repos)},
    )
    if run_id and data.status not in {"skipped", "error"}:
        _save_raw_input_safely(
            store,
            run_id,
            "github",
            data,
            action="github proof save",
            acquisition_steps=acquisition_steps,
        )
    return data


def _searchapi_fallback_enabled(run_input_sources: set[str] | None = None) -> bool:
    if run_input_sources is not None:
        return "searchapi" in run_input_sources or "searchapi_fallback" in run_input_sources
    return BRAND3_SEARCHAPI_VERTICAL_FALLBACK_ENABLED


def _collect_searchapi_fallback_input(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    brand_name: str,
    effective_brand_url: str,
    exa_data,
    cache_read,
    raw_input_cache: dict[str, str],
    acquisition_steps: dict[str, AcquisitionResult] | None = None,
    run_input_sources: set[str] | None = None,
    searchapi_collector_cls=SearchApiCollector,
) -> SearchApiData | None:
    if not _searchapi_fallback_enabled(run_input_sources):
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="searchapi",
            raw_cache_status="disabled",
            status="disabled",
            cache_status="disabled",
            eligible=False,
            details={"reason": "vertical fallback disabled"},
        )
        return None

    intents = _searchapi_candidate_intents(exa_data)
    if not intents:
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="searchapi",
            raw_cache_status="skipped",
            status="skipped",
            cache_status="skipped",
            eligible=False,
            details={"reason": "no failed or empty Exa intents eligible for SearchAPI fallback"},
        )
        return None

    cached = cache_read("searchapi", BRAND3_CACHE_TTL_HOURS, from_searchapi_payload)
    if cached:
        _use_cached_input(
            store=store,
            run_id=run_id,
            source="searchapi",
            payload=cached,
            raw_input_cache=raw_input_cache,
            acquisition_steps=acquisition_steps,
            details={"intents": list(cached.intents), "status": cached.status},
            action="searchapi cache save",
        )
        return cached

    collector = searchapi_collector_cls(api_key=SEARCHAPI_API_KEY)
    queries_by_intent = _searchapi_queries_from_exa_diagnostics(exa_data)
    data = collector.collect(
        brand_name,
        effective_brand_url,
        intents=intents,
        queries_by_intent=queries_by_intent,
    )
    raw_status = "miss" if data.status in {"ok", "partial"} else data.status
    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="searchapi",
        raw_cache_status=raw_status,
        status=data.status,
        cache_status="miss",
        eligible=data.status not in {"skipped", "error"},
        details={
            "intents": list(intents),
            "result_total": data.diagnostics.get("result_total", 0),
            "failed_intents": data.diagnostics.get("failed_intents", []),
            "no_result_intents": data.diagnostics.get("no_result_intents", []),
        },
    )
    logger.info(
        "searchapi fallback input collected",
        extra={"source": "searchapi", "status": data.status, "intents": list(intents)},
    )
    if run_id and data.status not in {"skipped", "error"}:
        _save_raw_input_safely(
            store,
            run_id,
            "searchapi",
            data,
            action="searchapi save",
            acquisition_steps=acquisition_steps,
        )
    return data


def _searchapi_candidate_intents(exa_data) -> tuple[str, ...]:
    diagnostics = getattr(exa_data, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        return ()
    failed_or_empty = set(diagnostics.get("failed_intents") or []) | set(diagnostics.get("no_result_intents") or [])
    allowed = set(BRAND3_SEARCHAPI_FALLBACK_INTENTS)
    fallback_intents = ("news", "external_mentions", "ai_visibility")
    explicit = tuple(intent for intent in fallback_intents if intent in failed_or_empty and intent in allowed)
    if explicit:
        return explicit
    if exa_external_proof_empty(diagnostics):
        return tuple(intent for intent in fallback_intents if intent in allowed)
    return ()


def _searchapi_queries_from_exa_diagnostics(exa_data) -> dict[str, str]:
    diagnostics = getattr(exa_data, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        return {}
    intent_results = diagnostics.get("intent_results") if isinstance(diagnostics.get("intent_results"), dict) else {}
    queries: dict[str, str] = {}
    for intent, payload in intent_results.items():
        if isinstance(payload, dict) and str(payload.get("query") or "").strip():
            queries[str(intent)] = str(payload.get("query") or "").strip()
    return queries


def _collect_parallel_shadow_input(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    brand_name: str,
    effective_brand_url: str,
    cache_read,
    raw_input_cache: dict[str, str],
    acquisition_steps: dict[str, AcquisitionResult] | None = None,
    parallel_shadow_collector_cls=ParallelShadowCollector,
) -> ParallelShadowData | None:
    if not _parallel_shadow_enabled():
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="parallel_shadow",
            raw_cache_status="disabled",
            status="disabled",
            cache_status="disabled",
            eligible=False,
        )
        return None

    cached = cache_read("parallel_shadow", BRAND3_CACHE_TTL_HOURS, from_parallel_shadow_payload)
    if cached:
        _use_cached_input(
            store=store,
            run_id=run_id,
            source="parallel_shadow",
            payload=cached,
            raw_input_cache=raw_input_cache,
            acquisition_steps=acquisition_steps,
            action="parallel shadow cache save",
        )
        return ParallelShadowData.from_dict(
            {
                **cached,
                "brand_name": cached.get("brand_name") or brand_name,
                "brand_url": cached.get("brand_url") or effective_brand_url,
            }
        )

    collector = parallel_shadow_collector_cls()
    shadow_data = collector.collect(brand_name, effective_brand_url)
    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="parallel_shadow",
        raw_cache_status=shadow_data.status,
        status=shadow_data.status,
        cache_status="miss",
        eligible=shadow_data.status not in {"error", "failed"},
        details=shadow_data.summary(),
    )
    summary = shadow_data.summary()
    logger.info(
        "parallel shadow input collected",
        extra={
            "source": "parallel_shadow",
            "status": shadow_data.status,
            "results": summary["result_total"],
            "domains": summary["unique_domain_count"],
        },
    )
    if run_id:
        _save_raw_input_safely(
            store,
            run_id,
            "parallel_shadow",
            shadow_data.to_dict(),
            action="parallel shadow save",
            acquisition_steps=acquisition_steps,
        )
    return shadow_data


def _collect_hyperbrowser_input(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    url: str,
    cache_read,
    raw_input_cache: dict[str, str],
    acquisition_steps: dict[str, AcquisitionResult] | None = None,
    run_input_sources: set[str] | None = None,
    hyperbrowser_collector_cls=HyperbrowserCollector,
) -> HyperbrowserFetchData | None:
    if not _hyperbrowser_enabled(run_input_sources):
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="hyperbrowser",
            raw_cache_status="disabled",
            status="disabled",
            cache_status="disabled",
            eligible=False,
            details={
                "provider": "hyperbrowser",
                "channel": "web_shadow",
                "evidence_eligibility": "ineligible",
            },
        )
        return None

    cached = cache_read("hyperbrowser", BRAND3_CACHE_TTL_HOURS, from_hyperbrowser_payload)
    if cached:
        _use_cached_input(
            store=store,
            run_id=run_id,
            source="hyperbrowser",
            payload=cached,
            raw_input_cache=raw_input_cache,
            acquisition_steps=acquisition_steps,
            details={
                "provider": "hyperbrowser",
                "channel": "web_shadow",
                "evidence_eligibility": "eligible",
                "source_url": cached.final_url,
                "content_hash": cached.metadata.get("contentHash"),
                "confidence": cached.metadata.get("confidence"),
                "chars": cached.text_chars,
            },
            action="hyperbrowser cache save",
            message=f"  Hyperbrowser: cache hit ({cached.text_chars} chars)",
        )
        return cached

    collector = hyperbrowser_collector_cls()
    data = collector.fetch(
        url,
        include_html=True,
        include_links=True,
        include_branding=True,
        include_screenshot=False,
    )
    status = "ok" if not data.error else "error"
    eligible = not bool(data.error)
    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="hyperbrowser",
        raw_cache_status="miss" if eligible else "error",
        status=status,
        cache_status="miss",
        eligible=eligible,
        error=data.error or None,
        details={
            "provider": "hyperbrowser",
            "channel": "web_shadow",
            "evidence_eligibility": "eligible" if eligible else "ineligible",
            "source_url": data.final_url,
            "content_hash": data.metadata.get("contentHash"),
            "confidence": data.metadata.get("confidence"),
            "chars": data.text_chars,
        },
    )
    logger.info(
        "hyperbrowser input collected",
        extra={"source": "hyperbrowser", "status": status, "chars": data.text_chars, "links": len(data.links)},
    )
    if run_id:
        _save_raw_input_safely(
            store,
            run_id,
            "hyperbrowser",
            data,
            action="hyperbrowser save",
            acquisition_steps=acquisition_steps,
        )
    return data
