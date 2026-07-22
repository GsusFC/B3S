"""Social and competitor raw input collection helpers."""

from __future__ import annotations

import logging

from src.collectors.competitor_collector import CompetitorCollector, CompetitorData
from src.collectors.exa_collector import ExaData
from src.collectors.social_collector import SocialData
from src.collectors.web_collector import WebData, WebCollector
from src.config import BRAND3_CACHE_TTL_HOURS, FIRECRAWL_API_KEYS
from src.services.input_collection_payloads import (
    _competitor_storage_payload,
    from_competitor_payload,
    from_social_payload,
)
from src.services.input_collection_state import (
    AcquisitionResult,
    _save_raw_input_safely,
    _set_acquisition_state,
    _use_cached_input,
)
from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def _collect_social_input(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    brand_name: str,
    web_data: WebData,
    use_social: bool,
    cache_read,
    raw_input_cache: dict[str, str],
    acquisition_steps: dict[str, AcquisitionResult] | None = None,
    social_collector,
) -> tuple[SocialData | None, str | None]:
    if not use_social:
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="social",
            raw_cache_status="skipped",
            status="skipped",
            cache_status="skipped",
            eligible=False,
        )
        return None, None

    social_data = cache_read("social", BRAND3_CACHE_TTL_HOURS, from_social_payload)
    if social_data:
        _use_cached_input(
            store=store,
            run_id=run_id,
            source="social",
            payload=social_data,
            raw_input_cache=raw_input_cache,
            acquisition_steps=acquisition_steps,
            details={"platforms": len(social_data.platforms), "followers": social_data.total_followers},
            action="social cache save",
            message=f"  Social: cache hit ({len(social_data.platforms)} platforms, {social_data.total_followers:,} total followers)",
        )
        return social_data, None

    try:
        social_data, social_limitation = social_collector(
            brand_name,
            web_data.markdown_content,
            api_key=FIRECRAWL_API_KEYS,
        )
        platforms_count = len(social_data.platforms)
        if social_limitation:
            _set_acquisition_state(
                raw_input_cache,
                acquisition_steps,
                source="social",
                raw_cache_status=social_limitation,
                status=social_limitation,
                cache_status="miss",
                eligible=True,
                details={"platforms": len(social_data.platforms)},
            )
            logger.warning(
                "social input limitation",
                extra={"source": "social", "status": social_limitation, "platforms": len(social_data.platforms)},
            )
        else:
            _set_acquisition_state(
                raw_input_cache,
                acquisition_steps,
                source="social",
                raw_cache_status="miss",
                status="ok",
                cache_status="miss",
                eligible=True,
                details={"platforms": len(social_data.platforms), "followers": social_data.total_followers},
            )
            logger.info(
                "social input collected",
                extra={"source": "social", "platforms": platforms_count, "followers": social_data.total_followers},
            )
        if run_id:
            _save_raw_input_safely(
                store,
                run_id,
                "social",
                social_data,
                action="social save",
                acquisition_steps=acquisition_steps,
            )
        return social_data, social_limitation
    except Exception as e:
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="social",
            raw_cache_status="error",
            status="error",
            cache_status="miss",
            eligible=False,
            error=str(e),
        )
        logger.warning("social input failed", extra={"source": "social", "error": str(e)})
        social_data = SocialData(brand_name=brand_name, error=str(e))
        if run_id:
            _save_raw_input_safely(
                store,
                run_id,
                "social",
                social_data,
                action="social error save",
                acquisition_steps=acquisition_steps,
            )
        return social_data, "error"


def _collect_competitor_input(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    brand_name: str,
    effective_brand_url: str,
    web_data: WebData,
    exa_data: ExaData,
    exa_collector,
    web_collector: WebCollector,
    use_competitors: bool,
    cache_read,
    raw_input_cache: dict[str, str],
    acquisition_steps: dict[str, AcquisitionResult] | None,
) -> CompetitorData | None:
    if not use_competitors:
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="competitors",
            raw_cache_status="skipped",
            status="skipped",
            cache_status="skipped",
            eligible=False,
        )
        logger.info("competitor input skipped", extra={"source": "competitors", "reason": "fast_mode"})
        return None

    competitor_collector = CompetitorCollector(
        exa_collector=exa_collector,
        web_collector=web_collector,
        max_competitors=5,
    )
    competitor_data = cache_read("competitors", BRAND3_CACHE_TTL_HOURS, from_competitor_payload)
    if competitor_data:
        _use_cached_input(
            store=store,
            run_id=run_id,
            source="competitors",
            payload=competitor_data,
            raw_input_cache=raw_input_cache,
            acquisition_steps=acquisition_steps,
            details={"competitors": len(competitor_data.competitors)},
            action="competitor cache save",
            message=f"  Competitors: cache hit ({len(competitor_data.competitors)} competitors)",
        )
        return competitor_data

    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="competitors",
        raw_cache_status="miss",
        status="fetched",
        cache_status="miss",
        eligible=True,
    )
    competitor_data = competitor_collector.collect(
        brand_name=brand_name,
        brand_url=effective_brand_url,
        brand_web=web_data,
        exa_data=exa_data,
    )
    if run_id:
        _save_raw_input_safely(
            store,
            run_id,
            "competitors",
            _competitor_storage_payload(competitor_data),
            action="competitor save",
            acquisition_steps=acquisition_steps,
        )
    return competitor_data
