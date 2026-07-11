"""Normalize persisted B3S report JSON into the history import contract."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from src.history.models import HistoricalReport, ReportImportError


def parse_report(report: dict[str, Any]) -> HistoricalReport:
    if not isinstance(report, dict):
        raise ReportImportError("report must be a JSON object")

    source_report_id = _required_text(report.get("id"), "id")
    canonical_url = _required_text(report.get("url"), "url")
    canonical_domain = normalize_domain(canonical_url)
    if not canonical_domain:
        raise ReportImportError("url must contain a canonical domain")

    observed_at = _timestamp(report.get("observed_at") or report.get("created_at"), "created_at")
    recorded_at = _timestamp(report.get("recorded_at") or report.get("created_at"), "created_at")
    evaluated_at = _timestamp(report.get("evaluated_at") or report.get("created_at"), "created_at")
    raw = _mapping(report.get("raw"))
    flow = _mapping(raw.get("flow"))
    candidate = _mapping(flow.get("candidate"))
    evidence_pack = _mapping(candidate.get("evidence_pack"))
    interpretation_debug = _mapping(flow.get("interpretation_debug"))
    raw_sv9 = _mapping(raw.get("sv9"))
    evaluation_result = _mapping(raw_sv9.get("result"))
    components = tuple(_mapping(item) for item in _sequence(report.get("components")) if isinstance(item, dict))
    if not components:
        raise ReportImportError("report must contain component evaluations")

    evidence_records = tuple(
        _mapping(item) for item in _sequence(evidence_pack.get("evidence")) if isinstance(item, dict)
    )
    _validate_evidence_refs(evidence_records)
    block_interpretations = tuple(
        _mapping(item) for item in _sequence(report.get("blocks")) if isinstance(item, dict)
    )
    _validate_block_refs(block_interpretations, evidence_records)

    report_hash = canonical_json_hash(report)
    capture_payload = candidate if candidate else {
        "brand_name": report.get("brand_name"),
        "url": canonical_url,
        "evidence_pack": {"evidence": list(evidence_records)},
    }
    acquisition_gate = _mapping(report.get("acquisition_gate"))
    evaluation_config = {
        "interpretation_debug": interpretation_debug,
        "llm_usage": _mapping(raw.get("llm_usage")),
        "visual_acquisition_present": raw.get("visual_acquisition_present"),
    }

    return HistoricalReport(
        source_report_id=source_report_id,
        source_run_id=str(raw.get("source_run_id") or report.get("source_run_id") or ""),
        brand_name=str(report.get("brand_name") or canonical_domain).strip() or canonical_domain,
        canonical_domain=canonical_domain,
        canonical_url=canonical_url,
        observed_at=observed_at,
        recorded_at=recorded_at,
        evaluated_at=evaluated_at,
        pipeline_version=str(raw.get("schema_version") or "b3s-report-json-v1"),
        rubric_version=str(evaluation_result.get("rubric_version") or evaluation_result.get("model") or "unknown"),
        prompt_version=str(interpretation_debug.get("prompt_version") or "unknown"),
        evaluator_model=str(evaluation_result.get("evaluator_model") or raw_sv9.get("evaluator_model") or "unknown"),
        gate_authority=str(interpretation_debug.get("gate_authority") or "unknown"),
        score=_number(report.get("score", raw_sv9.get("brand3_score"))),
        base_average=_number(report.get("base_average", raw_sv9.get("base_average"))),
        reliability_status=str(report.get("reliability_status") or raw_sv9.get("reliability_status") or "unknown"),
        acquisition_state=str(acquisition_gate.get("state") or "unknown"),
        report_hash=report_hash,
        capture_hash=canonical_json_hash(capture_payload),
        limitations=tuple(_strings(report.get("limitations"))),
        not_detected=tuple(_strings(report.get("not_detected"))),
        evidence_records=evidence_records,
        block_interpretations=block_interpretations,
        components=components,
        acquisition_attempts=tuple(
            _mapping(item) for item in _sequence(report.get("attempts")) if isinstance(item, dict)
        ),
        artifacts=tuple(
            _mapping(item) for item in _sequence(report.get("acquisition_artifacts")) if isinstance(item, dict)
        ),
        capture_payload=capture_payload,
        evaluation_result=evaluation_result,
        evaluation_config=evaluation_config,
        report_payload=report,
    )


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_evidence_refs(records: tuple[dict[str, Any], ...]) -> None:
    refs: set[str] = set()
    for index, record in enumerate(records):
        ref = str(record.get("ref") or "").strip()
        if not ref:
            raise ReportImportError(f"evidence record {index} is missing ref")
        if ref in refs:
            raise ReportImportError(f"duplicate evidence ref: {ref}")
        refs.add(ref)


def _validate_block_refs(
    blocks: tuple[dict[str, Any], ...],
    evidence_records: tuple[dict[str, Any], ...],
) -> None:
    known_refs = {str(record.get("ref") or "") for record in evidence_records}
    for block in blocks:
        name = str(block.get("name") or "unknown")
        for row in _sequence(block.get("refs")):
            ref = str(row.get("ref") or "").strip() if isinstance(row, dict) else str(row or "").strip()
            if ref and ref not in known_refs:
                raise ReportImportError(f"block {name} references unknown evidence: {ref}")


def _timestamp(value: Any, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportImportError(f"{field} must be ISO-8601: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_domain(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return str(parsed.hostname or "").strip(".").lower().removeprefix("www.")


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReportImportError(f"report is missing {field}")
    return text


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _strings(value: Any) -> list[str]:
    return [str(item) for item in _sequence(value) if str(item).strip()]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
