"""Input collection orchestration for Brand3 analysis runs."""

from __future__ import annotations

import logging
import os
from time import perf_counter

from src.collectors.competitor_collector import (
    CompetitorCollector,
    CompetitorData,
)
from src.collectors.context_collector import ContextCollector, ContextData
from src.collectors.exa_collector import ExaCollector, ExaData
from src.collectors.github_proof_collector import GitHubProofCollector, GitHubProofData
from src.collectors.hyperbrowser_collector import HyperbrowserCollector, HyperbrowserFetchData
from src.collectors.parallel_shadow_collector import ParallelShadowCollector, ParallelShadowData
from src.collectors.searchapi_collector import SearchApiCollector, SearchApiData
from src.collectors.social_collector import SocialData
from src.collectors.web_collector import WebCollector, WebData
from src.config import (
    BRAND3_CACHE_TTL_HOURS,
    BRAND3_CACHE_TTL_HOURS_BY_SOURCE,
    BRAND3_HYPERBROWSER_ENABLED,
)
from src.services.input_collection_payloads import (
    _competitor_storage_payload as _competitor_storage_payload_impl,
    from_competitor_payload as _from_competitor_payload_impl,
    from_context_payload as _from_context_payload_impl,
    from_exa_payload as _from_exa_payload_impl,
    from_github_payload as _from_github_payload_impl,
    from_hyperbrowser_payload as _from_hyperbrowser_payload_impl,
    from_parallel_shadow_payload as _from_parallel_shadow_payload_impl,
    from_searchapi_payload as _from_searchapi_payload_impl,
    from_social_payload as _from_social_payload_impl,
    from_web_payload as _from_web_payload_impl,
)
from src.services.diagnostics import _log_timing
from src.services.input_collection_sources import (
    _collect_competitor_input,
    _collect_context_input,
    _collect_exa_input,
    _collect_github_proof_input,
    _collect_hyperbrowser_input,
    _collect_parallel_shadow_input,
    _collect_searchapi_fallback_input,
    _collect_social_input,
    _collect_web_input,
    _hyperbrowser_enabled,
    _parallel_shadow_enabled,
)
from src.services.input_collection_state import (
    AcquisitionResult,
    RawInputs,
    RunStorage,
    _merge_acquisition_details,
    _raw_payload_ref,
    _record_acquisition,
    _record_storage_error,
    _save_raw_input_safely,
    _set_acquisition_state,
    _use_cached_input,
    load_cached,
    store_safely,
)
from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


from_web_payload = _from_web_payload_impl
from_exa_payload = _from_exa_payload_impl
from_github_payload = _from_github_payload_impl
from_hyperbrowser_payload = _from_hyperbrowser_payload_impl
from_parallel_shadow_payload = _from_parallel_shadow_payload_impl
from_searchapi_payload = _from_searchapi_payload_impl
from_social_payload = _from_social_payload_impl
from_competitor_payload = _from_competitor_payload_impl
from_context_payload = _from_context_payload_impl
_competitor_storage_payload = _competitor_storage_payload_impl


def start_analysis_run(
    brand_name: str,
    url: str,
    *,
    use_llm: bool,
    use_social: bool,
    db_path: str,
) -> RunStorage:
    try:
        store = SQLiteStore(db_path)
        brand_id = store.upsert_brand(brand_name, url)
        run_id = store.create_run(brand_id, brand_name, url, use_llm, use_social)
        return RunStorage(store=store, run_id=run_id)
    except Exception as e:
        logger.warning("raw input storage disabled", extra={"error": str(e)})
        return RunStorage(store=None, run_id=None)


def _cache_reader(
    *,
    store: SQLiteStore | None,
    brand_name: str,
    url: str,
    refresh: bool,
    acquisition_steps: dict[str, AcquisitionResult] | None = None,
):
    def cache_read(source: str, ttl_hours: int, decoder):
        if refresh:
            return None
        effective_ttl = BRAND3_CACHE_TTL_HOURS_BY_SOURCE.get(source, ttl_hours)
        return load_cached(store, brand_name, url, source, effective_ttl, decoder, acquisition_steps)

    return cache_read


def collect_raw_inputs(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    brand_name: str,
    url: str,
    refresh: bool,
    use_social: bool,
    use_competitors: bool,
    effective_brand_url_builder,
    context_evidence_builder,
    social_collector,
    context_collector_cls=ContextCollector,
    web_collector_cls=WebCollector,
    exa_collector_cls=ExaCollector,
    github_collector_cls=GitHubProofCollector,
    searchapi_collector_cls=SearchApiCollector,
    hyperbrowser_collector_cls=HyperbrowserCollector,
    run_input_sources: set[str] | None = None,
) -> RawInputs:
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, AcquisitionResult] = {}
    step_started = perf_counter()
    cache_read = _cache_reader(
        store=store,
        brand_name=brand_name,
        url=url,
        refresh=refresh,
        acquisition_steps=acquisition_steps,
    )
    context_data = _collect_context_input(
        store=store,
        run_id=run_id,
        brand_name=brand_name,
        url=url,
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        context_evidence_builder=context_evidence_builder,
        context_collector_cls=context_collector_cls,
    )
    step_started = _log_timing("raw input context", step_started)
    web_data, web_collector = _collect_web_input(
        store=store,
        run_id=run_id,
        url=url,
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        web_collector_cls=web_collector_cls,
    )
    step_started = _log_timing("raw input web", step_started)
    hyperbrowser_data = _collect_hyperbrowser_input(
        store=store,
        run_id=run_id,
        url=url,
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        run_input_sources=run_input_sources,
        hyperbrowser_collector_cls=hyperbrowser_collector_cls,
    )
    step_started = _log_timing("raw input hyperbrowser", step_started)
    effective_brand_url = effective_brand_url_builder(url, web_data)
    github_data = _collect_github_proof_input(
        store=store,
        run_id=run_id,
        brand_name=brand_name,
        effective_brand_url=effective_brand_url,
        web_data=web_data,
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        run_input_sources=run_input_sources,
        github_collector_cls=github_collector_cls,
    )
    step_started = _log_timing("raw input github", step_started)
    exa_data, exa_collector = _collect_exa_input(
        store=store,
        run_id=run_id,
        brand_name=brand_name,
        effective_brand_url=effective_brand_url,
        web_data=web_data,
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        exa_collector_cls=exa_collector_cls,
    )
    step_started = _log_timing("raw input exa", step_started)
    searchapi_data = _collect_searchapi_fallback_input(
        store=store,
        run_id=run_id,
        brand_name=brand_name,
        effective_brand_url=effective_brand_url,
        exa_data=exa_data,
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        run_input_sources=run_input_sources,
        searchapi_collector_cls=searchapi_collector_cls,
    )
    step_started = _log_timing("raw input searchapi", step_started)
    parallel_shadow_data = _collect_parallel_shadow_input(
        store=store,
        run_id=run_id,
        brand_name=brand_name,
        effective_brand_url=effective_brand_url,
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
    )
    step_started = _log_timing("raw input parallel shadow", step_started)
    social_data, social_limitation = _collect_social_input(
        store=store,
        run_id=run_id,
        brand_name=brand_name,
        web_data=web_data,
        use_social=use_social,
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        social_collector=social_collector,
    )
    step_started = _log_timing("raw input social", step_started)
    competitor_data = _collect_competitor_input(
        store=store,
        run_id=run_id,
        brand_name=brand_name,
        effective_brand_url=effective_brand_url,
        web_data=web_data,
        exa_data=exa_data,
        exa_collector=exa_collector,
        web_collector=web_collector,
        use_competitors=use_competitors,
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
    )
    _log_timing("raw input competitors", step_started)
    return RawInputs(
        context_data=context_data,
        web_data=web_data,
        hyperbrowser_data=hyperbrowser_data,
        github_data=github_data,
        effective_brand_url=effective_brand_url,
        exa_data=exa_data,
        searchapi_data=searchapi_data,
        parallel_shadow_data=parallel_shadow_data,
        social_data=social_data,
        social_limitation=social_limitation,
        competitor_data=competitor_data,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        web_collector=web_collector,
        exa_collector=exa_collector,
    )
