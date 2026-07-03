"""Readiness gate for broader Visual Signature calibration corpus use."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.visual_signature._internal.utils import unique as _unique
from src.visual_signature.calibration.calibration_export import validate_calibration_output_root
from src.visual_signature.calibration.calibration_readiness_render import calibration_readiness_markdown
from src.visual_signature.calibration.calibration_readiness_support import (
    category_coverage as _category_coverage,
    confidence_bucket_coverage as _confidence_bucket_coverage,
    load_bundle as _load_bundle,
    load_corpus_manifest as _load_corpus_manifest,
    pct as _pct,
    readiness_notes as _notes,
    reviewed_claims as _reviewed_claims,
    thresholds_for_scope as _thresholds_for_scope,
)
from src.visual_signature.calibration.readiness_models import (
    CALIBRATION_READINESS_SCHEMA_VERSION,
    ReadinessResult,
    ReadinessScope,
    ReadinessStatus,
    ReadinessThresholds,
    validate_readiness_result,
)
from src.visual_signature.phase_zero.models import PHASE_ZERO_TAXONOMY_VERSION


DEFAULT_READINESS_THRESHOLDS = ReadinessThresholds(
    minimum_total_claims=15,
    minimum_reviewed_claims=15,
    minimum_categories=3,
    minimum_claims_per_category=3,
    minimum_confidence_buckets=3,
    maximum_contradiction_rate=0.25,
    maximum_high_confidence_contradictions=1,
    maximum_unresolved_rate=0.25,
)

STANDARD_CONFIDENCE_BUCKETS = ("low", "medium", "high", "unknown")
DEFAULT_READINESS_SCOPE: ReadinessScope = "broader_corpus_use"


def build_calibration_readiness(
    bundle_root: str | Path,
    *,
    corpus_manifest_path: str | Path | None = None,
    thresholds: ReadinessThresholds | None = None,
    readiness_scope: ReadinessScope = DEFAULT_READINESS_SCOPE,
) -> ReadinessResult:
    root = Path(bundle_root)
    threshold_model = thresholds or DEFAULT_READINESS_THRESHOLDS
    scope_thresholds = _thresholds_for_scope(readiness_scope, threshold_model)
    validation_errors = validate_calibration_output_root(root)
    manifest, records_file, summary = _load_bundle(root)
    corpus_manifest, corpus_manifest_ref = _load_corpus_manifest(corpus_manifest_path)

    record_count = summary.record_count if summary is not None else records_file.record_count if records_file is not None else 0
    reviewed_claims = summary.reviewed_claims if summary is not None else _reviewed_claims(records_file)
    contradiction_rate = summary.contradicted_rate if summary is not None else 0.0
    unresolved_rate = summary.unresolved_rate if summary is not None else 0.0
    overconfidence_rate = summary.overconfidence_rate if summary is not None else 0.0
    category_coverage = _category_coverage(summary, records_file, scope_thresholds.minimum_claims_per_category)
    confidence_bucket_coverage = _confidence_bucket_coverage(records_file, scope_thresholds.minimum_confidence_buckets)
    category_count = sum(1 for row in category_coverage.values() if row.count > 0)
    confidence_bucket_count = sum(1 for row in confidence_bucket_coverage.values() if row.count > 0)
    high_confidence_contradictions = summary.high_confidence_contradiction_count if summary is not None else 0

    block_reasons: list[str] = []
    warning_reasons: list[str] = []

    if readiness_scope != DEFAULT_READINESS_SCOPE:
        warning_reasons.append(f"unsupported_scope:{readiness_scope}")
        warning_reasons.append("unsupported_scopes_do_not_reuse_broader_corpus_use_thresholds")

    if validation_errors:
        block_reasons.append("bundle_validation_failed")
    if summary is not None and summary.summary_count_consistency is not True:
        block_reasons.append("summary_count_inconsistent")

    if record_count < threshold_model.minimum_total_claims:
        block_reasons.append("small_sample_size")
    if reviewed_claims < threshold_model.minimum_reviewed_claims:
        block_reasons.append("insufficient_reviewed_claims")
    if category_count < threshold_model.minimum_categories:
        block_reasons.append("insufficient_category_depth")
    if any(row.count < threshold_model.minimum_claims_per_category for row in category_coverage.values() if row.count > 0):
        block_reasons.append("insufficient_category_depth")
    if confidence_bucket_count < threshold_model.minimum_confidence_buckets:
        block_reasons.append("insufficient_confidence_spread")
    if contradiction_rate > threshold_model.maximum_contradiction_rate:
        block_reasons.append("contradiction_rate_too_high")
    if high_confidence_contradictions > threshold_model.maximum_high_confidence_contradictions:
        block_reasons.append("high_confidence_contradictions_too_high")
    if unresolved_rate > threshold_model.maximum_unresolved_rate:
        block_reasons.append("unresolved_rate_too_high")

    if corpus_manifest_ref is None:
        warning_reasons.append("corpus_manifest_missing")
    elif corpus_manifest is not None:
        corpus_categories = corpus_manifest.get("categories") if isinstance(corpus_manifest, dict) else []
        if isinstance(corpus_categories, list) and corpus_categories:
            corpus_category_count = len(corpus_categories)
            if category_count < corpus_category_count:
                warning_reasons.append(
                    f"corpus_category_coverage_limited:{category_count}/{corpus_category_count}"
                )
        corpus_minimums = corpus_manifest.get("minimums") if isinstance(corpus_manifest, dict) else {}
        if isinstance(corpus_minimums, dict) and corpus_minimums:
            warning_reasons.append("corpus_manifest_loaded")

    status: ReadinessStatus = "ready" if not block_reasons else "not_ready"
    recommendation = (
        "Proceed with broader corpus use under the current calibration thresholds."
        if status == "ready"
        else "Hold broader corpus use until sample size, category depth, and confidence spread improve."
    )
    if validation_errors:
        recommendation = "Fix bundle validation errors before re-evaluating readiness."

    result = ReadinessResult(
        schema_version=CALIBRATION_READINESS_SCHEMA_VERSION,
        taxonomy_version=PHASE_ZERO_TAXONOMY_VERSION,
        record_type="calibration_readiness",
        readiness_scope=readiness_scope,
        calibration_run_id=str(summary.calibration_run_id if summary is not None else manifest.calibration_run_id if manifest is not None else "unknown"),
        checked_at=datetime.now(timezone.utc),
        status=status,
        block_reasons=_unique(block_reasons),
        warning_reasons=_unique(warning_reasons),
        bundle_valid=not validation_errors,
        validation_errors=list(validation_errors),
        source_corpus_manifest_path=corpus_manifest_ref,
        summary_count_consistency=bool(summary.summary_count_consistency if summary is not None else False),
        record_count=record_count,
        reviewed_claims=reviewed_claims,
        category_coverage=category_coverage,
        confidence_bucket_coverage=confidence_bucket_coverage,
        contradiction_rate=contradiction_rate,
        unresolved_rate=unresolved_rate,
        overconfidence_rate=overconfidence_rate,
        minimum_thresholds_used=threshold_model,
        recommendation=recommendation,
        notes=_notes(validation_errors, corpus_manifest, category_count, confidence_bucket_count, readiness_scope),
    )
    return result


def write_calibration_readiness(
    bundle_root: str | Path,
    *,
    corpus_manifest_path: str | Path | None = None,
    output_json_path: str | Path | None = None,
    output_md_path: str | Path | None = None,
    thresholds: ReadinessThresholds | None = None,
    readiness_scope: ReadinessScope = DEFAULT_READINESS_SCOPE,
) -> dict[str, str]:
    root = Path(bundle_root)
    readiness = build_calibration_readiness(
        root,
        corpus_manifest_path=corpus_manifest_path,
        thresholds=thresholds,
        readiness_scope=readiness_scope,
    )
    output_json = Path(output_json_path) if output_json_path is not None else root / "calibration_readiness.json"
    output_md = Path(output_md_path) if output_md_path is not None else root / "calibration_readiness.md"
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(readiness.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_md.write_text(calibration_readiness_markdown(readiness) + "\n", encoding="utf-8")
    return {"calibration_readiness_json": str(output_json), "calibration_readiness_md": str(output_md)}
def validate_calibration_readiness_result(payload: dict[str, Any]) -> list[str]:
    return validate_readiness_result(payload)
