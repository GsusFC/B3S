"""Reusable service layer for Brand3 Scoring operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

from src.collectors.competitor_collector import (
    ComparisonResult,
    CompetitorCollector,
    CompetitorData,
    CompetitorInfo,
)
from src.collectors.context_collector import ContextCollector, ContextData
from src.collectors.exa_collector import ExaCollector, ExaData, ExaResult
from src.collectors.social_collector import PlatformMetrics, SocialData
from src.collectors.web_collector import WebCollector, WebData
from src.config import (
    AUDIT_ANALYST_MODEL,
    BRAND3_CACHE_TTL_HOURS,
    BRAND3_DB_PATH,
    BRAND3_NICHE_AUTO_APPLY_MIN_CONFIDENCE,
    BRAND3_PROMOTION_MAX_COMPOSITE_DROP,
    BRAND3_PROMOTION_MAX_DIMENSION_DROPS,
    BRAND3_VISUAL_SCREENSHOT_TIMEOUT_SECONDS,
    LLM_CHEAP_MODEL,
)
from src.discovery.enrichment import build_discovery_enrichment
from src.discovery.evidence_preview import build_discovery_evidence_preview
from src.discovery.calibration import apply_discovery_calibration_hint, build_discovery_calibration_hint
from src.discovery.entity_discovery import discover_entity
from src.discovery.summary import format_discovery_summary
from src.discovery.trust_basis import build_discovery_trust_basis
from src.services.context_snapshot import _context_readiness_from_snapshot as _context_readiness_from_snapshot_impl
from src.niche import classify_brand_niche, list_calibration_profiles as _list_calibration_profiles_niche, select_calibration_profile
from src.features.llm_analyzer import LLMAnalyzer
from src.features.percepcion import PercepcionExtractor
from src.features.coherencia import CoherenciaExtractor
from src.features.diferenciacion import DiferenciacionExtractor
from src.features.presencia import PresenciaExtractor
from src.features.vitalidad import VitalidadExtractor
from src.quality.dimension_confidence import dimension_confidence_from_features
from src.quality.evidence_summary import summarize_evidence_from_features
from src.scoring.engine import ScoringEngine
from src.services.brand_reporting import brand_report as _brand_report_impl
from src.services.analysis_exceptions import AnalysisJobCancelled
from src.services.acquisition_runtime import (
    _classify_screenshot_error as _classify_screenshot_error_impl,
    _collect_social_with_budget as _collect_social_with_budget_impl,
    _normalized_screenshot_provider as _normalized_screenshot_provider_impl,
    _screenshot_capture_diagnostic as _screenshot_capture_diagnostic_impl,
    _screenshot_capture_worker as _screenshot_capture_worker_impl,
    _screenshot_has_capture as _screenshot_has_capture_impl,
    _social_collect_worker as _social_collect_worker_impl,
    _take_firecrawl_screenshot as _take_firecrawl_screenshot_impl,
    _take_playwright_screenshot as _take_playwright_screenshot_impl,
    _take_playwright_screenshot_with_firecrawl_fallback as _take_playwright_screenshot_with_firecrawl_fallback_impl,
    _take_screenshot_with_budget as _take_screenshot_with_budget_impl,
)
from src.services.context_enrichment import (
    _context_effective_readiness as _context_effective_readiness_impl,
    _context_enrichment_summary as _context_enrichment_summary_impl,
)
from src.services.feedback_public_api import add_feedback as _add_feedback_impl
from src.services.calibration_public_api import (  # noqa: F401
    _build_run_audit_context,
    _default_gate_config,
    _evaluate_promotion_gate,
    _load_gate_config,
    _read_calibration_state,
    _restore_calibration_state,
    apply_candidates,
    compare_version,
    get_gate_config,
    list_baselines,
    list_candidates,
    list_experiments,
    list_versions,
    promote_baseline,
    propose_calibration,
    review_candidate,
    rollback_version,
    run_experiment,
    set_gate_config,
)
from src.services.acquisition_audit import (
    _acquisition_audit_payload,
    _acquisition_provenance_summary,
    _context_evidence_items,
)
from src.services.brand_profiles import _build_brand_profile, _slugify
from src.services.content_web import (
    _aggregate_exa_content,
    _build_content_web,
    _effective_brand_url,
    _has_usable_web_content,
    _recover_owned_web_content,
    _web_content_changed,
)
from src.services.diagnostics import _log_timing, _print_feature_details
from src.services.discovery_payloads import (
    _annotate_content_source,
    _discovery_search_plan_payload as _discovery_search_plan_payload_impl,
)
from src.services.feature_pipeline import run_feature_pipeline
from src.services.input_collection import collect_raw_inputs, start_analysis_run, store_safely as _store_safely
from src.services.job_public_api import (  # noqa: F401
    cancel_analysis_job,
    claim_next_job,
    enqueue_analysis_job,
    execute_analysis_job,
    get_analysis_job,
    list_analysis_jobs,
    retry_analysis_job,
    run_claimed_job,
)
from src.services.llm_policy import (
    _audit_analyst_llm as _build_audit_analyst_llm,
    _cost_policy_summary,
    _infer_llm_provider,
    _llm_cache_summary,
    _llm_provider_payload,
)
from src.services.output_files import _save_result
from src.services.public_presence import _public_presence_inventory_summary
from src.services.report_readiness import _persist_report_readiness as _persist_report_readiness_impl
from src.services.report_summaries import _context_confidence_summary, _dimension_confidence_summary, _llm_model_roles_payload, _trust_summary_payload
from src.services.run_support import _build_research_pack_for_feature_prompts as _build_research_pack_for_feature_prompts_impl, _check_cancel, _emit_progress
from src.reports.entity_research_packet import build_entity_research_packet
from src.reports.brand_audit_analyst import run_brand_audit_analyst_pass
from src.research.research_pack_facade import build_recommended_research_pack
from src.services.reporting_queries import (
    benchmark_profiles as _benchmark_profiles_impl,
    compare_benchmarks as _compare_benchmarks_impl,
    learn as _learn_impl,
    list_brands as _list_brands_impl,
    list_feedback as _list_feedback_impl,
    list_profiles as _list_profiles_impl,
    list_runs as _list_runs_impl,
    run_dimension_confidence as _run_dimension_confidence_impl,
    run_evidence_summary as _run_evidence_summary_impl,
    run_trust_summary as _run_trust_summary_impl,
    show_run as _show_run_impl,
)
from src.services.runtime_helpers import (
    _compute_data_quality as _compute_data_quality_impl,
    _should_skip_llm_for_low_context as _should_skip_llm_for_low_context_impl,
)
from src.services.run_workflow import run as _run_workflow_impl
from src.services.visual_signature_runtime import _content_web_from_snapshot as _content_web_from_snapshot_impl, _run_visual_signature_shadow as _run_visual_signature_shadow_impl, _screenshot_capture_from_snapshot as _screenshot_capture_from_snapshot_impl, _snapshot_has_visual_signature_scan as _snapshot_has_visual_signature_scan_impl, _visual_signature_shadow_failure_payload as _visual_signature_shadow_failure_payload_impl, _visual_signature_shadow_screenshot_payload as _visual_signature_shadow_screenshot_payload_impl, _web_data_from_snapshot as _web_data_from_snapshot_impl, ensure_visual_signature_for_existing_run as _ensure_visual_signature_for_existing_run_impl, run_visual_signature_for_existing_run as _run_visual_signature_for_existing_run_impl
from src.services.run_payloads import _build_run_audit_payload, _build_run_data_sources_payload
from src.services.run_preparation import plan_content, select_niche_profile, setup_llm
from src.services.serialization import _to_jsonable
from src.services.scoring_pipeline import score_features
from src.storage.sqlite_store import SQLiteStore
from src.visual_signature import build_visual_signature_scan, extract_visual_signature
from src.visual_signature.persistence import (
    build_visual_signature_persistence_bundle,
    persist_visual_signature_bundle,
    persist_visual_signature_result,
)
from src.visual_signature.vision import enrich_visual_signature_with_vision

logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIMENSIONS_PATH = (PROJECT_ROOT / "src" / "dimensions.py").resolve()
ENGINE_PATH = (PROJECT_ROOT / "src" / "scoring" / "engine.py").resolve()
_PARTIAL_DIMENSIONS = ("coherencia", "diferenciacion")

_discovery_search_plan_payload = _discovery_search_plan_payload_impl
_compute_data_quality = _compute_data_quality_impl
_should_skip_llm_for_low_context = _should_skip_llm_for_low_context_impl
_social_collect_worker = _social_collect_worker_impl
_collect_social_with_budget = _collect_social_with_budget_impl
_normalized_screenshot_provider = _normalized_screenshot_provider_impl
_take_firecrawl_screenshot = _take_firecrawl_screenshot_impl
_screenshot_has_capture = _screenshot_has_capture_impl
_screenshot_capture_diagnostic = _screenshot_capture_diagnostic_impl
_classify_screenshot_error = _classify_screenshot_error_impl
_visual_signature_shadow_screenshot_payload = _visual_signature_shadow_screenshot_payload_impl
_visual_signature_shadow_failure_payload = _visual_signature_shadow_failure_payload_impl
_run_visual_signature_shadow = _run_visual_signature_shadow_impl
_snapshot_has_visual_signature_scan = _snapshot_has_visual_signature_scan_impl
_web_data_from_snapshot = _web_data_from_snapshot_impl
_content_web_from_snapshot = _content_web_from_snapshot_impl
_screenshot_capture_from_snapshot = _screenshot_capture_from_snapshot_impl
_context_readiness_from_snapshot = _context_readiness_from_snapshot_impl
_persist_report_readiness = _persist_report_readiness_impl


def _take_playwright_screenshot_with_firecrawl_fallback(
    url: str,
    *,
    take_playwright_screenshot=None,
    take_firecrawl_screenshot=None,
    screenshot_has_capture=None,
) -> dict[str, object]:
    if take_playwright_screenshot is None:
        take_playwright_screenshot = _take_playwright_screenshot
    if take_firecrawl_screenshot is None:
        take_firecrawl_screenshot = _take_firecrawl_screenshot
    if screenshot_has_capture is None:
        screenshot_has_capture = _screenshot_has_capture
    return _take_playwright_screenshot_with_firecrawl_fallback_impl(
        url,
        take_playwright_screenshot=take_playwright_screenshot,
        take_firecrawl_screenshot=take_firecrawl_screenshot,
        screenshot_has_capture=screenshot_has_capture,
    )


def _entity_discovery_payload(
    *,
    brand_name: str,
    url: str,
    web_data: WebData | None,
    exa_data: ExaData | None,
    context_data: ContextData | None,
) -> dict[str, object]:
    try:
        return _to_jsonable(
            discover_entity(
                brand_name=brand_name,
                url=url,
                web_data=web_data,
                exa_data=exa_data,
                context_data=context_data,
            )
        )
    except Exception:
        return {
            "entity_type": "unknown",
            "analysis_scope": "url_only",
            "confidence": 0.0,
            "warnings": ["entity_discovery_failed"],
        }


def _build_research_pack_for_feature_prompts(
    *,
    store: SQLiteStore | None,
    run_id: int | None,
):
    return _build_research_pack_for_feature_prompts_impl(
        store=store,
        run_id=run_id,
        build_recommended_research_pack_fn=build_recommended_research_pack,
    )


def _audit_analyst_llm(feature_llm: LLMAnalyzer | None) -> LLMAnalyzer | None:
    return _build_audit_analyst_llm(
        feature_llm,
        analyzer_cls=LLMAnalyzer,
        audit_analyst_model=AUDIT_ANALYST_MODEL,
    )


def _screenshot_capture_worker(
    output_queue,
    url: str,
    provider: str,
    *,
    take_playwright_screenshot_with_firecrawl_fallback=None,
    take_playwright_screenshot=None,
    take_firecrawl_screenshot=None,
    screenshot_has_capture=None,
) -> None:
    if take_playwright_screenshot_with_firecrawl_fallback is None:
        take_playwright_screenshot_with_firecrawl_fallback = _take_playwright_screenshot_with_firecrawl_fallback
    if take_playwright_screenshot is None:
        take_playwright_screenshot = _take_playwright_screenshot
    if take_firecrawl_screenshot is None:
        take_firecrawl_screenshot = _take_firecrawl_screenshot
    if screenshot_has_capture is None:
        screenshot_has_capture = _screenshot_has_capture
    return _screenshot_capture_worker_impl(
        output_queue,
        url,
        provider,
        take_playwright_screenshot_with_firecrawl_fallback=take_playwright_screenshot_with_firecrawl_fallback,
        take_playwright_screenshot=take_playwright_screenshot,
        take_firecrawl_screenshot=take_firecrawl_screenshot,
        screenshot_has_capture=screenshot_has_capture,
    )


_take_playwright_screenshot = _take_playwright_screenshot_impl


def _take_screenshot_with_budget(
    url: str,
    *,
    timeout_seconds: int = BRAND3_VISUAL_SCREENSHOT_TIMEOUT_SECONDS,
    provider: str | None = None,
    normalized_screenshot_provider=None,
    take_playwright_screenshot=None,
    take_playwright_screenshot_with_firecrawl_fallback=None,
    take_firecrawl_screenshot=None,
    screenshot_capture_worker=None,
) -> tuple[dict[str, object], str | None]:
    if normalized_screenshot_provider is None:
        normalized_screenshot_provider = _normalized_screenshot_provider
    if take_playwright_screenshot is None:
        take_playwright_screenshot = _take_playwright_screenshot
    if take_playwright_screenshot_with_firecrawl_fallback is None:
        take_playwright_screenshot_with_firecrawl_fallback = _take_playwright_screenshot_with_firecrawl_fallback
    if take_firecrawl_screenshot is None:
        take_firecrawl_screenshot = _take_firecrawl_screenshot
    if screenshot_capture_worker is None:
        screenshot_capture_worker = _screenshot_capture_worker
    return _take_screenshot_with_budget_impl(
        url,
        timeout_seconds=timeout_seconds,
        provider=provider,
        normalized_screenshot_provider=normalized_screenshot_provider,
        take_playwright_screenshot=take_playwright_screenshot,
        take_playwright_screenshot_with_firecrawl_fallback=take_playwright_screenshot_with_firecrawl_fallback,
        take_firecrawl_screenshot=take_firecrawl_screenshot,
        screenshot_capture_worker=screenshot_capture_worker,
    )


def run_visual_signature_for_existing_run(
    *,
    store: SQLiteStore,
    run_id: int,
    extractor=extract_visual_signature,
    vision_enricher=enrich_visual_signature_with_vision,
    persistence_fn=persist_visual_signature_bundle,
) -> dict[str, object]:
    return _run_visual_signature_for_existing_run_impl(
        store=store,
        run_id=run_id,
        extractor=extractor,
        vision_enricher=vision_enricher,
        persistence_fn=persistence_fn,
    )


def ensure_visual_signature_for_existing_run(
    *,
    store: SQLiteStore,
    run_id: int,
    extractor=extract_visual_signature,
    vision_enricher=enrich_visual_signature_with_vision,
    persistence_fn=persist_visual_signature_bundle,
) -> dict[str, object]:
    return _ensure_visual_signature_for_existing_run_impl(
        store=store,
        run_id=run_id,
        extractor=extractor,
        vision_enricher=vision_enricher,
        persistence_fn=persistence_fn,
    )



def run(
    url: str,
    brand_name: str = None,
    use_llm: bool = True,
    use_social: bool = True,
    use_competitors: bool = True,
    calibration_profile_override: str | None = None,
    skip_visual_analysis: bool = False,
    enable_visual_signature_shadow_run: bool = False,
    refresh: bool = False,
    run_input_sources: set[str] | None = None,
    progress_cb=None,
    cancel_check=None,
) -> dict:
    return _run_workflow_impl(
        url,
        brand_name=brand_name,
        use_llm=use_llm,
        use_social=use_social,
        use_competitors=use_competitors,
        calibration_profile_override=calibration_profile_override,
        skip_visual_analysis=skip_visual_analysis,
        enable_visual_signature_shadow_run=enable_visual_signature_shadow_run,
        refresh=refresh,
        run_input_sources=run_input_sources,
        progress_cb=progress_cb,
        cancel_check=cancel_check,
    )


def add_feedback(
    note: str,
    run_id: int | None = None,
    brand_name: str | None = None,
    url: str | None = None,
    dimension_name: str | None = None,
    feature_name: str | None = None,
    expected_score: float | None = None,
    actual_score: float | None = None,
) -> int:
    return _add_feedback_impl(
        note,
        run_id=run_id,
        brand_name=brand_name,
        url=url,
        dimension_name=dimension_name,
        feature_name=feature_name,
        expected_score=expected_score,
        actual_score=actual_score,
    )


def learn(run_id: int | None = None, brand_name: str | None = None, url: str | None = None) -> list[dict]:
    return _learn_impl(run_id=run_id, brand_name=brand_name, url=url, db_path=BRAND3_DB_PATH)


def list_runs(brand_name: str | None = None, url: str | None = None, limit: int = 20) -> list[dict]:
    return _list_runs_impl(brand_name=brand_name, url=url, limit=limit, db_path=BRAND3_DB_PATH)


def list_brands(limit: int = 50) -> list[dict]:
    return _list_brands_impl(limit=limit, db_path=BRAND3_DB_PATH)


def list_profiles() -> list[dict]:
    return _list_profiles_impl()


def list_calibration_profiles() -> list[dict]:
    return _list_calibration_profiles_niche()


def benchmark_profiles(
    spec_path: str,
    *,
    profiles: list[str] | None = None,
    include_auto: bool = True,
    use_llm: bool = True,
    use_social: bool = True,
    use_competitors: bool = True,
) -> dict:
    return _benchmark_profiles_impl(
        spec_path,
        profiles=profiles,
        include_auto=include_auto,
        use_llm=use_llm,
        use_social=use_social,
        use_competitors=use_competitors,
    )


def compare_benchmarks(before_path: str, after_path: str) -> dict:
    return _compare_benchmarks_impl(before_path, after_path)


def list_feedback(brand_name: str | None = None) -> list[dict]:
    return _list_feedback_impl(brand_name=brand_name, db_path=BRAND3_DB_PATH)


def show_run(run_id: int) -> dict:
    return _show_run_impl(run_id, db_path=BRAND3_DB_PATH)


def run_evidence_summary(run_id: int) -> dict:
    return _run_evidence_summary_impl(run_id, db_path=BRAND3_DB_PATH)


def run_dimension_confidence(run_id: int) -> dict:
    return _run_dimension_confidence_impl(run_id, db_path=BRAND3_DB_PATH)


def run_trust_summary(run_id: int) -> dict:
    return _run_trust_summary_impl(run_id, db_path=BRAND3_DB_PATH)


def _context_readiness_from_snapshot(snapshot: dict) -> dict:
    return _context_readiness_from_snapshot_impl(snapshot)


def _context_enrichment_summary(
    *,
    public_presence_inventory: dict[str, object] | None,
    context_summary: dict[str, object] | None,
) -> dict[str, object]:
    return _context_enrichment_summary_impl(
        public_presence_inventory=public_presence_inventory,
        context_summary=context_summary,
    )


def _context_effective_readiness(
    *,
    public_presence_inventory: dict[str, object] | None,
    context_summary: dict[str, object] | None,
) -> dict[str, object]:
    return _context_effective_readiness_impl(
        public_presence_inventory=public_presence_inventory,
        context_summary=context_summary,
    )


def brand_report(brand_name: str, limit: int = 10) -> dict:
    return _brand_report_impl(brand_name, limit=limit, db_path=BRAND3_DB_PATH)
