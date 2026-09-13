"""SV9 application service.

Shadow-phase entrypoints (design doc section 16, step 0): SV9 runs over
persisted Brand Audit snapshots via replay. It never collects, never touches
V5 scoring, and never renders anywhere public.

Acquisition stays owned by Brand Audit; detection (Pass 1) stays owned by the
existing TLDR extraction. SV9 inserts the tile engine between detection and
the editorial layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.config import (
    BRAND3_DB_PATH,
    SV9_BASE_MODEL,
    SV9_EDITORIAL_MODEL,
    SV9_REASONING_MODEL,
)
from src.features.llm_analyzer import LLMAnalyzer
from src.features.magnetism.extractor import MagnetismExtractor
from src.services.magnetism_service import load_brand_audit_snapshot
from src.sv9.aggregator import aggregate
from src.sv9.editorial import build_editorial
from src.sv9.evaluator import evaluate_snapshot_components
from src.sv9.flow_ingress import (
    detection_blocks_from_flow_candidate,
    flow_candidate_extra_signals,
)
from src.sv9.models import Sv9ScanResult
from src.sv9.signals import (
    audit_visual_method,
    collect_signals,
    compute_vision_observations,
    merge_signals,
    visual_signature_evidence_from_snapshot,
    visual_signature_shadow_signals,
    vision_signals,
)
from src.sv9.store import Sv9Store
from src.sv9_flow.contracts import Sv9FlowCandidate


def run_sv9_from_audit_run(
    run_id: int,
    *,
    llm: LLMAnalyzer | None = None,
    db_path: str = BRAND3_DB_PATH,
) -> Sv9ScanResult:
    """Run SV9 from an existing Brand Audit run snapshot, without re-collecting."""
    snapshot = load_brand_audit_snapshot(run_id, db_path=db_path)
    return run_sv9_from_audit_snapshot(snapshot, llm=llm)


def materialize_sv9_scan(
    run_id: int,
    *,
    db_path: str = BRAND3_DB_PATH,
    llm: LLMAnalyzer | None = None,
    editorial: bool = True,
) -> tuple[int, Sv9ScanResult]:
    """Full shadow pipeline for one audit run — the single source of truth for
    the replay script, the web materialization button, and retries.

    Includes everything a bare run_sv9_from_audit_run lacks: pinned Pass 1
    detection (re-evaluations stay comparable), the vision pass over the
    persisted screenshot when audit-time visual analysis fell back to local
    pixels (Idea de marca and Coherencia starve without it), and the editorial
    voice. Returns (scan_id, result).
    """
    snapshot = load_brand_audit_snapshot(run_id, db_path=db_path)
    store = Sv9Store(db_path)
    try:
        detection = store.get_detection(run_id)
        if detection is None:
            detection = detect_for_snapshot(snapshot, llm=llm)
            store.save_detection(run_id, detection)

        extra_signals = None
        vision_payload = store.get_visual_evidence(run_id)
        if vision_payload is None and audit_visual_method(snapshot) != "vision":
            vision_payload = compute_vision_observations(snapshot)
            if vision_payload is not None:
                store.save_visual_evidence(run_id, vision_payload)
        if vision_payload is not None:
            extra_signals = vision_signals(vision_payload)
        visual_signature_evidence = visual_signature_evidence_from_snapshot(snapshot)
        visual_signature_signals = visual_signature_shadow_signals(visual_signature_evidence)
        if visual_signature_signals:
            extra_signals = merge_signals(extra_signals or {}, visual_signature_signals)

        result = run_sv9_from_audit_snapshot(
            snapshot, llm=llm, magnetism_result=detection, extra_signals=extra_signals
        )
        scan_id = store.save_scan(result)

        if editorial:
            editorial_llm = llm if llm is not None else LLMAnalyzer(model=SV9_EDITORIAL_MODEL)
            if getattr(editorial_llm, "api_key", None):
                payload = build_editorial(result.to_dict(), llm=editorial_llm)
                if payload["component_messages"] or payload["executive_reading"]:
                    store.save_editorial(
                        scan_id,
                        component_messages=payload["component_messages"],
                        executive_reading=payload["executive_reading"],
                    )
        return scan_id, result
    finally:
        store.close()


def run_sv9_from_audit_snapshot(
    snapshot: dict[str, Any],
    *,
    llm: LLMAnalyzer | None = None,
    reasoning_llm: LLMAnalyzer | None = None,
    magnetism_result: dict[str, Any] | None = None,
    sv9_flow_candidate: Sv9FlowCandidate | None = None,
    source_run_id: int | None = None,
    extra_signals: dict[str, Any] | None = None,
) -> Sv9ScanResult:
    """Run SV9 over a loaded Brand Audit snapshot.

    Pass `magnetism_result` to reuse an already-computed Pass 1 extraction
    (e.g. a persisted Magnetism scan payload) and skip re-detection.
    Pass `sv9_flow_candidate` to score an SV9 Flow candidate natively instead:
    its interpretation blocks become the detection input and its tile signals
    merge into the evaluator signals. The two detection inputs are exclusive.
    An explicit `source_run_id` wins over anything derivable from the inputs —
    shadow envelopes carry the run id outside the snapshot, so the caller is
    the only reliable source.
    `extra_signals` ({component: [signal, ...]}) merge on top of the snapshot
    signals — e.g. the SV9-time vision pass over the persisted screenshot.

    Model routing (deploy brief section 2.6): detection and the 8 base
    components run on the Flash tier (`llm`); Magnetism and Coherencia run on
    the reasoning tier (`reasoning_llm`). When a caller passes an explicit
    `llm`, it serves both tiers unless `reasoning_llm` is also given.
    """
    if magnetism_result is not None and sv9_flow_candidate is not None:
        raise ValueError(
            "run_sv9_from_audit_snapshot received both magnetism_result and "
            "sv9_flow_candidate; pass exactly one detection input."
        )
    base_llm = _effective_llm(llm)
    if reasoning_llm is not None:
        reasoning = reasoning_llm
    elif llm is not None:
        reasoning = llm  # caller-supplied single client serves both tiers
    else:
        reasoning = _reasoning_llm()

    tldr, signals, brand_name, url, source_run_id = _prepare_sv9_analysis_inputs(
        snapshot,
        llm=base_llm,
        magnetism_result=magnetism_result,
        sv9_flow_candidate=sv9_flow_candidate,
        source_run_id=source_run_id,
        extra_signals=extra_signals,
    )
    components = evaluate_snapshot_components(
        tldr=tldr,
        signals=signals,
        brand_name=brand_name,
        url=url,
        llm=base_llm,
        reasoning_llm=reasoning,
    )
    return _aggregate_sv9_analysis(
        components,
        brand_name=brand_name,
        url=url,
        source_run_id=source_run_id,
        evaluator_llm=base_llm,
    )


def _prepare_sv9_analysis_inputs(
    snapshot: Mapping[str, Any],
    *,
    llm: Any,
    magnetism_result: Mapping[str, Any] | None = None,
    sv9_flow_candidate: Sv9FlowCandidate | None = None,
    source_run_id: Any = None,
    extra_signals: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], str, str, int | None]:
    """Prepare the canonical SV9 evaluator inputs for legacy or Flow ingress.

    This private seam is intentionally shared with Vault's selective adapter so
    both paths use the same detection projection and signal merge. It does not
    evaluate, aggregate, persist, or change authority.
    """

    if magnetism_result is not None and sv9_flow_candidate is not None:
        raise ValueError(
            "SV9 analysis inputs received both magnetism_result and "
            "sv9_flow_candidate; pass exactly one detection input."
        )
    snapshot_dict = dict(snapshot)
    run_meta = (
        snapshot_dict.get("run")
        if isinstance(snapshot_dict.get("run"), dict)
        else {}
    )
    if sv9_flow_candidate is not None:
        tldr = detection_blocks_from_flow_candidate(sv9_flow_candidate)
        brand_name = str(
            sv9_flow_candidate.evidence_pack.brand_name
            or run_meta.get("brand_name")
            or snapshot_dict.get("brand_name")
            or ""
        )
        url = str(
            sv9_flow_candidate.evidence_pack.url
            or run_meta.get("url")
            or snapshot_dict.get("url")
            or ""
        )
        resolved_source_run_id = _to_int(
            source_run_id
            if source_run_id is not None
            else run_meta.get("id") or snapshot_dict.get("id")
        )
        merged_extra = merge_signals(
            flow_candidate_extra_signals(sv9_flow_candidate), extra_signals
        )
    else:
        if magnetism_result is None:
            magnetism_result = MagnetismExtractor(
                llm=llm
            ).extract_from_audit_snapshot(snapshot_dict)
        tldr = magnetism_result.get("tldr_brand3") or {}
        brand_name = str(
            magnetism_result.get("brand_name")
            or run_meta.get("brand_name")
            or snapshot_dict.get("brand_name")
            or ""
        )
        url = str(
            magnetism_result.get("source_url")
            or magnetism_result.get("url")
            or run_meta.get("url")
            or snapshot_dict.get("url")
            or ""
        )
        resolved_source_run_id = _to_int(
            source_run_id
            if source_run_id is not None
            else magnetism_result.get("source_run_id")
            or run_meta.get("id")
            or snapshot_dict.get("id")
        )
        merged_extra = extra_signals

    signals = merge_signals(collect_signals(snapshot_dict), merged_extra)
    return dict(tldr), signals, brand_name, url, resolved_source_run_id


def _aggregate_sv9_analysis(
    components: Mapping[str, Any],
    *,
    brand_name: str,
    url: str,
    source_run_id: Any,
    evaluator_llm: Any,
) -> Sv9ScanResult:
    """Apply the canonical deterministic SV9 aggregation and model label."""

    result = aggregate(
        components,
        brand_name=brand_name,
        url=url,
        source_run_id=source_run_id,
    )
    # Scan-level label records the base tier; per-component evaluation_model
    # captures the Flash/reasoning routing for regression measurement.
    result.evaluator_model = getattr(evaluator_llm, "model", None)
    return result


def detect_for_snapshot(
    snapshot: dict[str, Any],
    *,
    llm: LLMAnalyzer | None = None,
) -> dict[str, Any]:
    """Run Pass 1 (TLDR detection) over a snapshot and return its payload.

    Detection is not deterministic across runs; callers that re-evaluate the
    same run should persist this payload (Sv9Store.save_detection) and reuse it
    so the evaluator is the only moving part.
    """
    return MagnetismExtractor(llm=_effective_llm(llm)).extract_from_audit_snapshot(snapshot)


def _effective_llm(llm: LLMAnalyzer | None) -> LLMAnalyzer | None:
    """Base (Flash) tier: detection and the 8 base components."""
    if llm is not None:
        return llm
    candidate = LLMAnalyzer(model=SV9_BASE_MODEL)
    if getattr(candidate, "api_key", None):
        return candidate
    return None


def _reasoning_llm() -> LLMAnalyzer | None:
    """Reasoning tier: Magnetism and Coherencia, the two fine judgments."""
    candidate = LLMAnalyzer(model=SV9_REASONING_MODEL)
    if getattr(candidate, "api_key", None):
        return candidate
    return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
