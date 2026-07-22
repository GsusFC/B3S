"""Primary-source raw input collection helpers."""

from __future__ import annotations

import logging

from src.collectors.context_collector import ContextCollector, ContextData
from src.collectors.web_collector import WebCollector, WebData
from src.config import BRAND3_CACHE_TTL_HOURS, FIRECRAWL_API_KEYS
from src.services.input_collection_payloads import from_context_payload, from_exa_payload, from_web_payload
from src.services.input_collection_state import (
    AcquisitionResult,
    _save_raw_input_safely,
    _set_acquisition_state,
    _use_cached_input,
    store_safely,
)
from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def _collect_context_input(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    brand_name: str,
    url: str,
    cache_read,
    raw_input_cache: dict[str, str],
    acquisition_steps: dict[str, AcquisitionResult] | None = None,
    context_evidence_builder,
    context_collector_cls=ContextCollector,
) -> ContextData:
    context_data = cache_read("context", 24, from_context_payload)
    if context_data:
        _use_cached_input(
            store=store,
            run_id=run_id,
            source="context",
            payload=context_data,
            raw_input_cache=raw_input_cache,
            acquisition_steps=acquisition_steps,
            details={
                "score": context_data.context_score,
                "confidence": context_data.confidence,
            },
            action="context cache save",
            message=(
                "  Context: cache hit"
                f" (score={context_data.context_score:.0f}, confidence={context_data.confidence:.2f})"
            ),
        )
        if run_id:
            store_safely(
                store,
                "context cache evidence save",
                lambda: store.save_evidence_items(run_id, context_evidence_builder(context_data)),
                acquisition_steps=acquisition_steps,
                source="context",
            )
        return context_data

    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="context",
        raw_cache_status="miss",
        status="fetched",
        cache_status="miss",
        eligible=True,
    )
    context_data = context_collector_cls().scan(url)
    logger.info(
        "context input collected",
        extra={
            "source": "context",
            "score": round(context_data.context_score, 2),
            "coverage": round(context_data.coverage, 2),
            "confidence": round(context_data.confidence, 2),
        },
    )
    if run_id:
        _save_raw_input_safely(
            store,
            run_id,
            "context",
            context_data,
            action="context save",
            acquisition_steps=acquisition_steps,
        )

        store_safely(
            store,
            "context evidence save",
            lambda: store.save_evidence_items(run_id, context_evidence_builder(context_data)),
            acquisition_steps=acquisition_steps,
            source="context",
        )
    return context_data


def _collect_web_input(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
    url: str,
    cache_read,
    raw_input_cache: dict[str, str],
    acquisition_steps: dict[str, AcquisitionResult] | None = None,
    web_collector_cls=WebCollector,
) -> tuple[WebData, WebCollector]:
    web_collector = web_collector_cls(api_key=FIRECRAWL_API_KEYS)
    web_data = cache_read("web", BRAND3_CACHE_TTL_HOURS, from_web_payload)
    if web_data:
        _use_cached_input(
            store=store,
            run_id=run_id,
            source="web",
            payload=web_data,
            raw_input_cache=raw_input_cache,
            acquisition_steps=acquisition_steps,
            details={"chars": len(web_data.markdown_content)},
            action="web cache save",
            message=f"  Web: cache hit ({len(web_data.markdown_content)} chars)",
        )
        return web_data, web_collector

    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="web",
        raw_cache_status="miss",
        status="fetched",
        cache_status="miss",
        eligible=True,
    )
    web_data = web_collector.scrape(url)
    logger.info("web input collected", extra={"source": "web", "chars": len(web_data.markdown_content)})
    if getattr(web_data, "capture_obstruction", ""):
        _set_acquisition_state(
            raw_input_cache,
            acquisition_steps,
            source="web",
            raw_cache_status="obstructed",
            status="obstructed",
            cache_status="miss",
            eligible=False,
            details={"capture_obstruction": web_data.capture_obstruction},
        )
        logger.warning(
            "web input obstructed",
            extra={"source": "web", "capture_obstruction": web_data.capture_obstruction},
        )
    if run_id:
        _save_raw_input_safely(
            store,
            run_id,
            "web",
            web_data,
            action="web save",
            acquisition_steps=acquisition_steps,
        )
    return web_data, web_collector
