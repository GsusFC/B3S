"""Exa raw input collection helpers."""

from __future__ import annotations

import logging

from src.collectors.exa_collector import ExaCollector, ExaData
from src.config import BRAND3_CACHE_TTL_HOURS, EXA_API_KEY
from src.services.legal_identity import derive_legal_name
from src.services.exa_diagnostics import exa_external_proof_empty
from src.services.input_collection_payloads import from_exa_payload
from src.services.input_collection_state import (
    AcquisitionResult,
    _save_raw_input_safely,
    _set_acquisition_state,
    _use_cached_input,
)
from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def _collect_exa_input(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    brand_name: str,
    effective_brand_url: str,
    web_data=None,
    cache_read,
    raw_input_cache: dict[str, str],
    acquisition_steps: dict[str, AcquisitionResult] | None = None,
    exa_collector_cls=ExaCollector,
) -> tuple[ExaData, ExaCollector]:
    exa_collector = exa_collector_cls(api_key=EXA_API_KEY)
    exa_data = cache_read("exa", BRAND3_CACHE_TTL_HOURS, from_exa_payload)
    if exa_data:
        _use_cached_input(
            store=store,
            run_id=run_id,
            source="exa",
            payload=exa_data,
            raw_input_cache=raw_input_cache,
            acquisition_steps=acquisition_steps,
            details={
                "mentions": len(exa_data.mentions),
                "news": len(exa_data.news),
            },
            action="exa cache save",
            message=f"  Exa: cache hit ({len(exa_data.mentions)} mentions, {len(exa_data.news)} news)",
        )
        return exa_data, exa_collector
    if not EXA_API_KEY:
        exa_data = ExaData(
            brand_name=brand_name,
            diagnostics={
                "failed_intents": ["mentions", "news", "competitors"],
                "error": "EXA_API_KEY not set",
            },
        )
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="exa",
            raw_cache_status="missing_key",
            status="missing_key",
            cache_status="missing",
            eligible=True,
            error="EXA_API_KEY not set",
            details={"reason": "EXA_API_KEY not set"},
        )
        logger.warning("exa input skipped: EXA_API_KEY not set", extra={"source": "exa"})
        return exa_data, exa_collector

    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="exa",
        raw_cache_status="miss",
        status="fetched",
        cache_status="miss",
        eligible=True,
    )
    legal_name = derive_legal_name(brand_name=brand_name, web_data=web_data)
    exa_data = exa_collector.collect_brand_data(
        brand_name,
        effective_brand_url,
        legal_name=legal_name,
    )
    diagnostics = dict(exa_data.diagnostics or {})
    failed_intents = diagnostics.get("failed_intents") or []
    no_result_intents = diagnostics.get("no_result_intents") or []
    if failed_intents:
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="exa",
            raw_cache_status="partial",
            status="partial",
            cache_status="miss",
            eligible=True,
            details={"failed_intents": list(failed_intents)},
        )
        logger.warning(
            "exa input partially collected",
            extra={
                "source": "exa",
                "mentions": len(exa_data.mentions),
                "news": len(exa_data.news),
                "failed_intents": list(failed_intents),
            },
        )
    elif no_result_intents:
        status = "empty" if exa_external_proof_empty(diagnostics) else "ok"
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="exa",
            raw_cache_status="miss",
            status=status,
            cache_status="miss",
            eligible=True,
            details={"no_result_intents": list(no_result_intents)},
        )
        logger.info(
            "exa input collected with empty intents",
            extra={
                "source": "exa",
                "mentions": len(exa_data.mentions),
                "news": len(exa_data.news),
                "no_result_intents": list(no_result_intents),
            },
        )
    else:
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="exa",
            raw_cache_status="miss",
            status="ok",
            cache_status="miss",
            eligible=True,
        )
        logger.info(
            "exa input collected",
            extra={"source": "exa", "mentions": len(exa_data.mentions), "news": len(exa_data.news)},
        )
    if run_id:
        _save_raw_input_safely(
            store,
            run_id,
            "exa",
            exa_data,
            action="exa save",
            acquisition_steps=acquisition_steps,
        )
    return exa_data, exa_collector
