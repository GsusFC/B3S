"""Normalize acquisition-only payloads for the historical repository."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from src.history.models import CaptureObservation, ReportImportError
from src.history.report_parser import (
    canonical_json_bytes,
    canonical_json_hash,
    normalize_domain,
)


CAPTURE_OBSERVATION_SCHEMA_VERSION = "b3s-capture-observation-v1"
_CAPTURE_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "source_scan_id",
        "source_run_id",
        "brand_name",
        "url",
        "observed_at",
        "recorded_at",
        "pipeline_version",
        "acquisition_state",
        "acquisition_summary",
        "limitations",
        "capture_payload",
        "evidence_records",
        "acquisition_attempts",
        "artifacts",
        "metadata",
    }
)


def parse_capture_observation(payload: dict[str, Any]) -> CaptureObservation:
    """Validate, detach and content-address one exact capture observation."""

    if not isinstance(payload, dict):
        raise ReportImportError("capture observation must be a JSON object")
    unknown = set(payload) - _CAPTURE_OBSERVATION_FIELDS
    if unknown:
        raise ReportImportError(
            "capture observation contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    if payload.get("schema_version") != CAPTURE_OBSERVATION_SCHEMA_VERSION:
        raise ReportImportError("unsupported capture observation schema")
    _validate_json_value(payload, path="$")
    try:
        # Round-tripping through canonical JSON detaches every mutable nested
        # value from the caller and proves that the retained payload is exactly
        # representable by PostgreSQL jsonb.
        raw_observation = json.loads(canonical_json_bytes(payload).decode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ReportImportError("capture observation must contain canonical JSON") from exc

    source_scan_id = _required_text(
        raw_observation.get("source_scan_id"), "source_scan_id"
    )
    canonical_url = _required_text(raw_observation.get("url"), "url")
    domain = normalize_domain(canonical_url)
    if not domain:
        raise ReportImportError("url must contain a canonical domain")
    observed_at = _timestamp(raw_observation.get("observed_at"), "observed_at")
    recorded_at = _timestamp(
        raw_observation.get("recorded_at") or raw_observation.get("observed_at"),
        "recorded_at",
    )
    capture_payload = _mapping_field(
        raw_observation.get("capture_payload"),
        field="capture_payload",
    )
    if not capture_payload:
        raise ReportImportError("capture_payload must be a non-empty object")
    evidence_records = tuple(
        _object_rows(raw_observation.get("evidence_records"), "evidence_records")
    )
    _validate_evidence(evidence_records)
    acquisition_attempts = tuple(
        _object_rows(
            raw_observation.get("acquisition_attempts"),
            "acquisition_attempts",
        )
    )
    artifacts = tuple(_object_rows(raw_observation.get("artifacts"), "artifacts"))

    source_run_id = _optional_text(raw_observation.get("source_run_id"), "source_run_id")
    brand_name = _optional_text(raw_observation.get("brand_name"), "brand_name") or domain
    pipeline_version = (
        _optional_text(raw_observation.get("pipeline_version"), "pipeline_version")
        or "unknown"
    )
    acquisition_state = (
        _optional_text(raw_observation.get("acquisition_state"), "acquisition_state")
        or "unknown"
    )
    return CaptureObservation(
        source_scan_id=source_scan_id,
        source_run_id=source_run_id,
        brand_name=brand_name,
        canonical_domain=domain,
        canonical_url=canonical_url,
        observed_at=observed_at,
        recorded_at=recorded_at,
        pipeline_version=pipeline_version,
        acquisition_state=acquisition_state,
        observation_hash=canonical_json_hash(raw_observation),
        capture_hash=canonical_json_hash(capture_payload),
        limitations=tuple(
            _text_rows(raw_observation.get("limitations"), "limitations")
        ),
        evidence_records=evidence_records,
        acquisition_attempts=acquisition_attempts,
        artifacts=artifacts,
        capture_payload=capture_payload,
        acquisition_summary=_mapping_field(
            raw_observation.get("acquisition_summary"),
            field="acquisition_summary",
            default_empty=True,
        ),
        metadata=_mapping_field(
            raw_observation.get("metadata"),
            field="metadata",
            default_empty=True,
        ),
        raw_observation=raw_observation,
    )


def _validate_evidence(records: tuple[dict[str, Any], ...]) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records):
        ref = _required_text(record.get("ref"), f"evidence_records[{index}].ref")
        if ref in seen:
            raise ReportImportError(f"duplicate evidence ref: {ref}")
        seen.add(ref)
        _required_text(record.get("source"), f"evidence_records[{index}].source")
        _required_text(
            record.get("evidence_type"),
            f"evidence_records[{index}].evidence_type",
        )
        _required_text(record.get("content"), f"evidence_records[{index}].content")
        if "metadata" in record and not isinstance(record["metadata"], dict):
            raise ReportImportError(
                f"evidence_records[{index}].metadata must be an object"
            )


def _validate_json_value(value: Any, *, path: str) -> None:
    if isinstance(value, str):
        if "\x00" in value:
            raise ReportImportError(f"{path} contains a NUL character")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReportImportError(f"{path} contains a non-finite number")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReportImportError(f"{path} contains a non-string key")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ReportImportError(f"{path} contains unsupported type {type(value).__name__}")


def _timestamp(value: Any, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportImportError(f"{field} must be ISO-8601: {text}") from exc
    if parsed.tzinfo is None:
        raise ReportImportError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ReportImportError(f"capture observation {field} must be text")
    text = value.strip()
    if not text:
        raise ReportImportError(f"capture observation is missing {field}")
    return text


def _optional_text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReportImportError(f"capture observation {field} must be text")
    return value.strip()


def _mapping_field(
    value: Any,
    *,
    field: str,
    default_empty: bool = False,
) -> dict[str, Any]:
    if value is None and default_empty:
        return {}
    if not isinstance(value, dict):
        raise ReportImportError(f"{field} must be an object")
    return dict(value)


def _object_rows(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReportImportError(f"{field} must be an array")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise ReportImportError(f"{field}[{index}] must be an object")
        rows.append(dict(row))
    return rows


def _text_rows(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReportImportError(f"{field} must be an array")
    rows: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ReportImportError(f"{field}[{index}] must be non-empty text")
        rows.append(item)
    return rows


__all__ = [
    "CAPTURE_OBSERVATION_SCHEMA_VERSION",
    "parse_capture_observation",
]
