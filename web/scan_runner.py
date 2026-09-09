"""Live B3S scan runner: capture → envelope → evidence flow → SV9 → report.

Runs in a background thread per scan. The active thread state lives in memory,
while a durable control-plane envelope is mirrored to SQLite. A process restart
cannot resume a thread, so orphaned jobs are surfaced as explicit interrupted
failures instead of disappearing or being silently duplicated. Finished reports
remain immutable records in the report store.
"""

from __future__ import annotations

import dataclasses
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse

from src.build_info import current_build_sha
from src.history.report_parser import canonical_json_hash, normalize_domain
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER
from src.services.evidence_vault_scan_orchestration import VaultExactResumeError, vault_exact_resume_error
from src.services.scanner_report_assessment import (
    validate_report_sv9_assessment as _validate_report_sv9_assessment,
)
from src.services.scanner_report_assessment import ScannerReportAssessmentError
from src.services.evidence_vault_acquisition_outcome import (
    trusted_acquisition_report_metadata,
)
from src.services.scanner_evidence_comparison import (
    EVIDENCE_COMPARISON_VERSION,
    annotate_candidate_report,
    canonical_enforcement_mode,
    selected_report_for_display,
)
from src.url_validator import validate_url
from web.report_store import list_reports_for_domain, load_report, new_scan_id, save_report

_SCANS: dict[str, dict[str, Any]] = {}
_SCAN_EVENTS: dict[str, threading.Event] = {}
_VAULT_ACTIVATIONS: set[str] = set()
_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)

_DIAGNOSTIC_REASON_CODES = frozenset(
    {
        "active_review_overlay",
        "candidate_already_present",
        "canonical_capture_run_identity_unavailable",
        "components_not_mapping",
        "coverage_loss",
        "evaluation_failure",
        "evaluation_incomplete",
        "exact_reuse",
        "incomplete_candidate",
        "incomplete_review_partition",
        "invalid_authoritative_relation_witness",
        "invalid_evaluation_input",
        "invalid_evaluation_outcome",
        "invalid_input",
        "invalid_replay",
        "invalid_source_identity",
        "process_restarted",
        "provider_failure",
        "repository_failure",
        "review_set",
        "scan_execution_failed",
        "scan_start_failed",
        "series_rollover",
        "shared_analysis_failure",
        "stale_authoritative_relation_witness",
        "unmapped_evidence",
        "unwitnessed_legacy_candidate",
        "vault_authority_capture_persist_failed",
        "vault_authority_capture_history_read_failed",
        "vault_authority_capture_observation_failed",
        "vault_authority_exact_operation_lookup_failed",
        "vault_authority_operational_memory_read_failed",
        "vault_authority_operation_plan_failed",
        "vault_authority_preparation_failed",
        "vault_authority_preparation_unavailable",
        "vault_authority_publication_invalid",
        "vault_authority_source_report_unavailable",
        "vault_completed_operation_missing_plan_fingerprint",
        "vault_persisted_capture_readback_failed",
        "vault_persisted_capture_unavailable",
        "vault_persistence_repository_unavailable",
        "vault_sidecar_failed",
    }
)
_DIAGNOSTIC_STAGES = frozenset(
    {"capture", "interpret", "score", "report", "vault_preparation", "vault_authority", "vault_sidecar", "unknown"}
)
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_SQLSTATE = re.compile(r"[0-9A-Z]{5}\Z")
_SAFE_COMPONENT_NOT_SCORED = re.compile(
    r"component_not_scored:([a-z_]+):(not_evaluated|unknown)\Z"
)
_DIAGNOSTIC_ASSESSMENT_UNSET = object()
_DIAGNOSTIC_LEDGER_KEY = "diagnostic_operation_ledger"
_DIAGNOSTIC_LEDGER_MAX_BYTES = 32 * 1024
_DIAGNOSTIC_LEDGER_MAX_EVENTS = 24
_DIAGNOSTIC_CAUSE_DEPTH = 4
_DIAGNOSTIC_TRACE_FRAMES = 12
_DIAGNOSTIC_REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z", re.I)
_DIAGNOSTIC_TILE_IDS = frozenset(
    tile["id"] for component in COMPONENTS.values() for tile in component["tiles"]
)
_DIAGNOSTIC_REASON_STATES = frozenset({
    "accepted", "active", "blocked", "coverage_loss", "invalid", "missing",
    "historical_basis_changed", "historical_basis_missing", "no_new_score",
    "pending", "review_required", "stale", "unavailable",
})
_DIAGNOSTIC_OPERATION_OUTCOMES = frozenset(
    {"started", "completed", "failed", "unavailable", "observed"}
)
_DIAGNOSTIC_SCAN_STATES = frozenset(
    {"accepted", "running", "blocked", "done", "error", "cancelled", "unknown"}
)
_DIAGNOSTIC_AUTHORITY_STATES = frozenset(
    {"no_new_score", "review_required", "published", "unknown"}
)
_DIAGNOSTIC_PATH = re.compile(r"(?:src|web)/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py\Z")
_SAFE_DB_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


def _safe_build_sha(value: Any) -> str:
    return value if isinstance(value, str) and _SHA40.fullmatch(value) else "unknown"


def _safe_timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    return value if parsed.tzinfo is not None else "unknown"


def _safe_scan_state(value: Any) -> str:
    return value if isinstance(value, str) and value in _DIAGNOSTIC_SCAN_STATES else "unknown"


def _safe_reason_codes(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    safe: list[str] = []
    for code in values:
        if not isinstance(code, str):
            continue
        component = _SAFE_COMPONENT_NOT_SCORED.fullmatch(code)
        if code in _DIAGNOSTIC_REASON_CODES or (
            component is not None and component.group(1) in PRESENTATION_ORDER
        ):
            safe.append(code)
    return list(dict.fromkeys(safe))[:24]


def _safe_exception_origin(exc: BaseException) -> dict[str, str] | None:
    """Return the tiny, observed origin contract; never exception text."""

    observed = getattr(exc, "_b3s_diagnostic_origin", None)
    if isinstance(observed, Mapping):
        return _safe_origin_mapping(observed)
    try:
        sqlstate = getattr(exc, "sqlstate", None)
    except Exception:
        sqlstate = None
    return _safe_origin_mapping(
        {"exception_type": type(exc).__name__, "sqlstate": sqlstate}
    )


def _safe_exception_resource(exc: BaseException | None) -> dict[str, Any] | None:
    """Expose only directly supplied DB identifiers, never diagnostic text."""
    if exc is None:
        return None
    try:
        diagnostic = getattr(exc, "diag", None)
    except Exception:
        diagnostic = None
    fields: dict[str, str] = {}
    for source, target in (
        ("schema_name", "schema"),
        ("table_name", "table"),
        ("constraint_name", "constraint"),
        ("column_name", "column"),
    ):
        try:
            value = getattr(diagnostic, source, None)
        except Exception:
            value = None
        if isinstance(value, str) and _SAFE_DB_IDENTIFIER.fullmatch(value):
            fields[target] = value
    return {"availability": "observed", **fields} if fields else {"availability": "unknown"}


def _safe_origin_mapping(value: Mapping[str, Any]) -> dict[str, str] | None:
    origin: dict[str, str] = {}
    try:
        exception_type = value.get("exception_type")
        sqlstate = value.get("sqlstate")
    except Exception:
        return None
    if isinstance(exception_type, str) and exception_type.isascii() and exception_type.isidentifier() and len(exception_type) <= 80:
        origin["exception_type"] = exception_type
    if isinstance(sqlstate, str) and _SAFE_SQLSTATE.fullmatch(sqlstate):
        origin["sqlstate"] = sqlstate
    return origin or None


def _capture_state(status: Mapping[str, Any]) -> str:
    phases = status.get("phases")
    if not isinstance(phases, list):
        return "unknown"
    capture = next((item for item in phases if isinstance(item, Mapping) and item.get("key") == "capture"), None)
    if not isinstance(capture, Mapping):
        return "unknown"
    return "completed" if capture.get("state") == "done" else "not_completed"


def _diagnostic(
    *,
    kind: str,
    stage: Any,
    capture_state: str,
    reason_codes: Any,
    build_sha: Any,
    origin: dict[str, str] | None = None,
    unknowns: list[str] | None = None,
) -> dict[str, Any]:
    safe_stage = stage if isinstance(stage, str) and stage in _DIAGNOSTIC_STAGES else "unknown"
    safe_kind = kind if isinstance(kind, str) and kind in {"execution_failed", "assessment_unavailable", "secondary_failure"} else "execution_failed"
    summary = {
        "execution_failed": "The scan stopped before completion.",
        "assessment_unavailable": "The scan completed, but no authoritative assessment is available.",
        "secondary_failure": "A non-authoritative Vault sidecar failed after scan processing.",
    }[safe_kind]
    result: dict[str, Any] = {
        "kind": safe_kind,
        "stage": safe_stage,
        "capture_state": capture_state if isinstance(capture_state, str) and capture_state in {"completed", "not_completed", "unknown"} else "unknown",
        "reason_codes": _safe_reason_codes(reason_codes),
        "summary": summary,
        "build_sha": _safe_build_sha(build_sha),
    }
    safe_origin = _safe_origin_mapping(origin) if isinstance(origin, Mapping) else None
    if safe_origin:
        result["origin"] = safe_origin
    if isinstance(unknowns, (list, tuple, set, frozenset)):
        safe_unknowns = [
            item
            for item in unknowns
            if isinstance(item, str)
            and item in {"assessment_reason_not_available", "legacy_status_without_diagnostic"}
        ][:4]
        if safe_unknowns:
            result["unknowns"] = safe_unknowns
    return result


def _safe_cause_chain(exc: BaseException | None) -> list[dict[str, str]]:
    """Return exception classes and SQLSTATE only; messages and args never escape."""
    chain: list[dict[str, str]] = []
    seen: set[int] = set()
    current = exc
    while current is not None and len(chain) < _DIAGNOSTIC_CAUSE_DEPTH and id(current) not in seen:
        seen.add(id(current))
        origin = _safe_exception_origin(current)
        if origin:
            chain.append(origin)
        current = current.__cause__ or current.__context__
    return chain


def _safe_trace_frames(exc: BaseException | None) -> list[dict[str, Any]]:
    if exc is None or exc.__traceback__ is None:
        return []
    frames: list[dict[str, Any]] = []
    for frame in traceback.extract_tb(exc.__traceback__)[-_DIAGNOSTIC_TRACE_FRAMES:]:
        # A substring such as /src/ is not a provenance check: an exception can
        # carry an arbitrary filename.  Only expose an actual path under this
        # checkout, and never open source files, locals, or source lines.
        try:
            path = os.path.realpath(frame.filename)
            if os.path.commonpath((_DIAGNOSTIC_REPO_ROOT, path)) != _DIAGNOSTIC_REPO_ROOT:
                continue
            relative = os.path.relpath(path, _DIAGNOSTIC_REPO_ROOT).replace("\\", "/")
        except (TypeError, ValueError, OSError):
            continue
        if not _safe_trace_path(relative):
            continue
        function = frame.name if isinstance(frame.name, str) else "unknown"
        if not function.isidentifier() or len(function) > 100:
            function = "unknown"
        frames.append({"path": relative, "line": max(1, int(frame.lineno)), "function": function})
    return frames


def _safe_trace_path(value: Any) -> bool:
    if not isinstance(value, str) or not _DIAGNOSTIC_PATH.fullmatch(value):
        return False
    return all(segment not in {"", ".", ".."} for segment in value.split("/"))


def _safe_fingerprint(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else None


def _safe_uuid(value: Any) -> str | None:
    return value if isinstance(value, str) and _UUID4.fullmatch(value) else None


def _safe_nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _safe_tile_ids(value: Any) -> tuple[list[str], int]:
    rows = value if isinstance(value, list) else []
    valid = [row for row in rows if isinstance(row, str) and row in _DIAGNOSTIC_TILE_IDS]
    kept = list(dict.fromkeys(valid))[:24]
    return kept, max(0, len(rows) - len(kept))


def _safe_coverage_row(row: Any, *, delta: bool) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    tile_id = row.get("tile_id")
    component_key = row.get("component_key")
    if (
        not isinstance(tile_id, str)
        or tile_id not in _DIAGNOSTIC_TILE_IDS
        or not isinstance(component_key, str)
        or component_key not in PRESENTATION_ORDER
    ):
        return None
    item: dict[str, Any] = {"tile_id": tile_id, "component_key": component_key}
    reason = row.get("reason")
    if isinstance(reason, str) and reason in _DIAGNOSTIC_REASON_STATES:
        item["reason"] = reason
    if delta:
        raw_fingerprints = row.get("evidence_fingerprints") if isinstance(row.get("evidence_fingerprints"), list) else []
        fingerprints = [
            fingerprint for value in raw_fingerprints
            if (fingerprint := _safe_fingerprint(value)) is not None
        ][:24]
        if fingerprints:
            item["evidence_fingerprints"] = fingerprints
        dropped = _safe_nonnegative_int(row.get("evidence_fingerprints_truncated_count")) + max(0, len(raw_fingerprints) - len(fingerprints))
        if dropped:
            item["evidence_fingerprints_truncated_count"] = dropped
    else:
        facts: list[dict[str, Any]] = []
        raw_facts = row.get("basis_facts") if isinstance(row.get("basis_facts"), list) else []
        for fact in raw_facts:
            if not isinstance(fact, Mapping):
                continue
            safe_fact = {
                key: identifier for key in ("relation_id", "evidence_id", "source_identity_id")
                # Authority coverage uses content-addressed identities in the
                # evaluator flow, while persisted relations may use UUIDs.
                # Both are opaque safe identifiers; discarding the former
                # would make a real coverage-loss event uninspectable.
                if (identifier := (_safe_uuid(fact.get(key)) or _safe_fingerprint(fact.get(key)))) is not None
            }
            continuity_state = fact.get("continuity_state")
            if isinstance(continuity_state, str) and continuity_state in {"missing", "changed"}:
                safe_fact["continuity_state"] = continuity_state
            if safe_fact:
                facts.append(safe_fact)
            if len(facts) == 24:
                break
        if facts:
            item["basis_facts"] = facts
        dropped = _safe_nonnegative_int(row.get("basis_facts_truncated_count")) + max(0, len(raw_facts) - len(facts))
        if dropped:
            item["basis_facts_truncated_count"] = dropped
    return item


def _safe_authority_result(value: Any) -> dict[str, Any] | None:
    """Keep the evaluator's business outcome separate from operation success."""
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    if not isinstance(status, str) or status not in _DIAGNOSTIC_AUTHORITY_STATES:
        return None
    result: dict[str, Any] = {"status": status}
    reasons = _safe_reason_codes(value.get("reason_codes"))
    if reasons:
        result["reason_codes"] = reasons
    return result


def _safe_capture_facts(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    facts: dict[str, Any] = {}
    source_scan_id = value.get("source_scan_id")
    if isinstance(source_scan_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source_scan_id):
        facts["source_scan_id"] = source_scan_id
    for key in ("observation_hash", "capture_hash"):
        if (fingerprint := _safe_fingerprint(value.get(key))) is not None:
            facts[key] = fingerprint
    return facts or None


def _safe_operation_coverage(value: Any) -> dict[str, Any] | None:
    """Treat observer/persisted coverage as hostile, bounded identifier data."""
    if not isinstance(value, Mapping):
        return None
    try:
        safe: dict[str, Any] = {}
        plan = value.get("plan")
        if isinstance(plan, Mapping):
            fingerprints = {
                key: fingerprint for key in ("canonical_plan_fingerprint", "current_series_fingerprint")
                if (fingerprint := _safe_fingerprint(plan.get(key))) is not None
            }
            if fingerprints:
                safe["plan"] = fingerprints
        for key in ("canonical_delta_fingerprint", "partition_fingerprint"):
            if (fingerprint := _safe_fingerprint(value.get(key))) is not None:
                safe[key] = fingerprint
        if (capture := _safe_capture_facts(value.get("capture"))) is not None:
            safe["capture"] = capture
        review = value.get("review_partition")
        if isinstance(review, Mapping):
            safe_review: dict[str, Any] = {}
            for key in ("tile_ids", "evaluation_input_reopened_tile_ids", "operational_authority_coverage_loss_tile_ids", "judgment_delta_coverage_loss_tile_ids", "planner_review_tile_ids", "coherencia_blocked_tile_ids"):
                ids, dropped = _safe_tile_ids(review.get(key))
                if ids:
                    safe_review[key] = ids
                total_dropped = dropped + _safe_nonnegative_int(review.get(f"{key}_truncated_count"))
                if total_dropped:
                    safe_review[f"{key}_truncated_count"] = total_dropped
            if safe_review:
                safe["review_partition"] = safe_review
        for key, delta in (("operational_authority_coverage_loss", False), ("judgment_delta_coverage_loss", True)):
            rows = value.get(key)
            raw_rows = rows if isinstance(rows, list) else []
            kept = [item for row in raw_rows if (item := _safe_coverage_row(row, delta=delta)) is not None][:24]
            if kept:
                safe[key] = kept
            upstream_count = _safe_nonnegative_int(value.get(f"{key}_truncated_count"))
            local_count = max(0, len(raw_rows) - len(kept))
            if upstream_count or local_count:
                safe[f"{key}_truncated_count"] = upstream_count + local_count
        for key in (
            "operational_authority_coverage_loss_truncated_count",
            "judgment_delta_coverage_loss_truncated_count",
        ):
            if (count := _safe_nonnegative_int(value.get(key))):
                safe[key] = max(_safe_nonnegative_int(safe.get(key)), count)
        if not safe:
            return {"available": False, "unknown": "coverage_projection_invalid"}
        encoded = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return safe if len(encoded) <= 8192 else {"available": True, "truncated": True}
    except Exception:
        return {"available": False, "unknown": "coverage_projection_invalid"}


def _diagnostic_operation(
    *, operation: str, stage: str, outcome: str, exc: BaseException | None = None,
    coverage: Mapping[str, Any] | None = None, started_monotonic: float | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    authority_result = _safe_authority_result(coverage)
    projected_coverage = _safe_operation_coverage(
        coverage.get("coverage") if isinstance(coverage, Mapping) and "coverage" in coverage else coverage
    )
    item: dict[str, Any] = {
        "operation": operation if isinstance(operation, str) and operation.isidentifier() else "unknown",
        "stage": stage if isinstance(stage, str) and stage in _DIAGNOSTIC_STAGES else "unknown",
        "outcome": outcome if isinstance(outcome, str) and outcome in _DIAGNOSTIC_OPERATION_OUTCOMES else "unavailable",
        "observed_at": now,
        "durability": "observed_best_effort",
    }
    if started_monotonic is not None:
        item["duration_ms"] = max(0, round((time.monotonic() - started_monotonic) * 1000, 3))
    origin = _safe_exception_origin(exc) if exc is not None else None
    if origin:
        item["origin"] = origin
    if (resource := _safe_exception_resource(exc)) is not None:
        item["resource"] = resource
    causes = _safe_cause_chain(exc)
    if causes:
        item["causes"] = causes
    frames = _safe_trace_frames(exc)
    if frames:
        item["trace"] = frames
    if projected_coverage is not None:
        item["coverage"] = projected_coverage
    if authority_result is not None:
        item["authority_result"] = authority_result
    return item


def _append_diagnostic_operation_locked(status: dict[str, Any], item: dict[str, Any]) -> None:
    ledger = status.get(_DIAGNOSTIC_LEDGER_KEY)
    if not isinstance(ledger, dict):
        ledger = {"version": "scan-diagnostic-ledger-v1", "events": [], "dropped_event_count": 0, "truncated_event_count": 0, "additional_status_write_count": 0, "additional_status_bytes": 0}
        status[_DIAGNOSTIC_LEDGER_KEY] = ledger
    events = ledger.get("events") if isinstance(ledger.get("events"), list) else []
    events = [event for event in events if isinstance(event, dict)][-_DIAGNOSTIC_LEDGER_MAX_EVENTS:]
    if len(events) >= _DIAGNOSTIC_LEDGER_MAX_EVENTS:
        events.pop(0)
        ledger["dropped_event_count"] = int(ledger.get("dropped_event_count") or 0) + 1
    candidate = [*events, item]
    try:
        # The cap applies to the complete persisted ledger, not merely the
        # latest event.  Drop oldest history first; preserve the newest exact
        # operation that explains the current outcome.
        while candidate and len(json.dumps({**ledger, "events": candidate}, sort_keys=True, separators=(",", ":")).encode("utf-8")) > _DIAGNOSTIC_LEDGER_MAX_BYTES:
            candidate.pop(0)
            ledger["dropped_event_count"] = int(ledger.get("dropped_event_count") or 0) + 1
        if not candidate:
            candidate = [{key: item[key] for key in ("operation", "stage", "outcome", "observed_at", "durability") if key in item}]
            ledger["truncated_event_count"] = int(ledger.get("truncated_event_count") or 0) + 1
        ledger["events"] = candidate
    except Exception:
        # Diagnostics are sidecar evidence; never let serialization change the
        # scanner's business result or cancellation path.
        ledger["truncated_event_count"] = int(ledger.get("truncated_event_count") or 0) + 1


def _safe_diagnostic_event(value: Any) -> dict[str, Any] | None:
    """Closed read-time projection for persisted/in-memory ledger entries."""
    if not isinstance(value, Mapping):
        return None
    operation = value.get("operation")
    outcome = value.get("outcome")
    stage = value.get("stage")
    observed_at = value.get("observed_at")
    if not (isinstance(operation, str) and operation.isidentifier() and len(operation) <= 100):
        return None
    if not isinstance(outcome, str) or outcome not in _DIAGNOSTIC_OPERATION_OUTCOMES:
        return None
    item: dict[str, Any] = {
        "operation": operation,
        "stage": stage if isinstance(stage, str) and stage in _DIAGNOSTIC_STAGES else "unknown",
        "outcome": outcome,
        "observed_at": _safe_timestamp(observed_at),
        "durability": "observed_best_effort",
    }
    duration = value.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration <= 86_400_000:
        item["duration_ms"] = round(float(duration), 3)
    origin = _safe_origin_mapping(value.get("origin")) if isinstance(value.get("origin"), Mapping) else None
    if origin:
        item["origin"] = origin
    if isinstance(value.get("causes"), list):
        causes = [_safe_origin_mapping(cause) for cause in value["causes"] if isinstance(cause, Mapping)]
        causes = [cause for cause in causes if cause][:_DIAGNOSTIC_CAUSE_DEPTH]
        if causes:
            item["causes"] = causes
    if isinstance(value.get("trace"), list):
        trace: list[dict[str, Any]] = []
        for frame in value["trace"][:_DIAGNOSTIC_TRACE_FRAMES]:
            if not isinstance(frame, Mapping):
                continue
            path, line, function = frame.get("path"), frame.get("line"), frame.get("function")
            if not _safe_trace_path(path):
                continue
            if not (isinstance(line, int) and not isinstance(line, bool) and line > 0):
                continue
            if not (isinstance(function, str) and function.isidentifier() and len(function) <= 100):
                continue
            trace.append({"path": path, "line": line, "function": function})
        if trace:
            item["trace"] = trace
    coverage = _safe_operation_coverage(value.get("coverage"))
    if coverage:
        item["coverage"] = coverage
    authority_result = _safe_authority_result(value.get("authority_result"))
    if authority_result:
        item["authority_result"] = authority_result
    resource = value.get("resource")
    if isinstance(resource, Mapping):
        availability = resource.get("availability")
        safe_resource = {
            "availability": (
                availability
                if isinstance(availability, str) and availability in {"observed", "unknown"}
                else "unknown"
            )
        }
        for key in ("schema", "table", "constraint", "column"):
            if isinstance(resource.get(key), str) and _SAFE_DB_IDENTIFIER.fullmatch(resource[key]):
                safe_resource[key] = resource[key]
        item["resource"] = safe_resource
    return item


def _record_diagnostic_operation(
    scan_id: str, *, operation: str, stage: str, outcome: str,
    exc: BaseException | None = None, coverage: Mapping[str, Any] | None = None,
    started_monotonic: float | None = None,
) -> None:
    """Best-effort private diagnostic write; it must never change scan outcome."""
    try:
        with _LOCK:
            status = _SCANS.get(scan_id)
            if status is None:
                return
            _append_diagnostic_operation_locked(status, _diagnostic_operation(operation=operation, stage=stage, outcome=outcome, exc=exc, coverage=coverage, started_monotonic=started_monotonic))
    except Exception:
        _LOG.warning("diagnostic operation was not recorded", extra={"scan_id": scan_id, "operation": operation})


def _safe_operation_readout(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    if not isinstance(status, str) or status not in {"pending", "not_required", "claimed", "running", "result_persisted", "completed", "failed_retryable", "superseded"}:
        return None
    result: dict[str, Any] = {"status": status}
    for key in ("observation_hash", "operation_plan_fingerprint", "result_fingerprint", "candidate_packet_fingerprint"):
        if (fingerprint := _safe_fingerprint(value.get(key))) is not None:
            result[key] = fingerprint
    if isinstance(value.get("plan"), Mapping):
        if (fingerprint := _safe_fingerprint(value["plan"].get("operation_plan_fingerprint"))) is not None:
            result["plan_fingerprint"] = fingerprint
    if isinstance(value.get("mode"), str) and value["mode"] in {"incremental", "full", "not_required", "unknown"}:
        result["mode"] = value["mode"]
    if (attempt_count := _safe_nonnegative_int(value.get("attempt_count"))) or value.get("attempt_count") == 0:
        result["attempt_count"] = attempt_count
    if isinstance(value.get("lease_active"), bool):
        result["lease_active"] = value["lease_active"]
    return result


def _safe_detail_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, Any] = {}
    status_readback = value.get("status_readback")
    if isinstance(status_readback, str) and status_readback in {
        "observed_ledger", "observed_without_ledger", "missing", "unavailable", "divergent",
    }:
        safe["status_readback"] = status_readback
    operation = _safe_operation_readout(value.get("operation"))
    if operation:
        safe["operation"] = operation
    else:
        operation_lookup = value.get("operation_lookup")
        if isinstance(operation_lookup, str) and operation_lookup in {"not_applicable", "missing", "unavailable"}:
            safe["operation_lookup"] = operation_lookup
    return safe


def _coverage_is_partial(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            (isinstance(key, str) and key.endswith("_truncated_count") and _safe_nonnegative_int(item) > 0)
            or _coverage_is_partial(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_coverage_is_partial(item) for item in value)
    return False


def scan_diagnostic_dossier_from_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return the protected rich dossier; compact status deliberately omits it."""
    ledger = status.get(_DIAGNOSTIC_LEDGER_KEY)
    if not isinstance(ledger, Mapping):
        return {"available": False, "reason": "diagnostic_ledger_not_recorded", "exact_resume": {"supported": False, "reason": "exact_resume_action_trace_unsupported"}}
    raw_events = ledger.get("events") if isinstance(ledger.get("events"), list) else []
    events = [event for raw in raw_events if (event := _safe_diagnostic_event(raw)) is not None][-_DIAGNOSTIC_LEDGER_MAX_EVENTS:]
    scan_id = status.get("id")
    safe_scan_id = scan_id if isinstance(scan_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scan_id) else "unknown"
    failed = next((event for event in reversed(events) if event.get("outcome") == "failed"), None)
    context = _safe_detail_context(status.get("_diagnostic_detail_context"))
    dependency: dict[str, Any] = {}
    artifacts: dict[str, Any] = {}
    for event in events:
        coverage = event.get("coverage")
        if not isinstance(coverage, Mapping):
            continue
        if isinstance(coverage.get("plan"), Mapping):
            dependency["authority_plan"] = dict(coverage["plan"])
        for key in ("canonical_delta_fingerprint", "partition_fingerprint"):
            if key in coverage:
                artifacts[key] = coverage[key]
        if isinstance(coverage.get("capture"), Mapping):
            artifacts["capture"] = dict(coverage["capture"])
    operation = context.get("operation")
    if isinstance(operation, Mapping):
        dependency["operation"] = dict(operation)
        artifacts["operation"] = {
            key: value for key, value in operation.items()
            if key.endswith("fingerprint") or key == "observation_hash"
        }
    report_id = status.get("report_id")
    if isinstance(report_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", report_id):
        artifacts["report_id"] = report_id
    safe_state = _safe_scan_state(status.get("state"))
    authority_results = [
        event["authority_result"] for event in events
        if isinstance(event.get("authority_result"), Mapping)
    ]
    latest_authority = authority_results[-1] if authority_results else None
    affected_count = sum(
        len((event.get("coverage") or {}).get("operational_authority_coverage_loss") or [])
        + len((event.get("coverage") or {}).get("judgment_delta_coverage_loss") or [])
        for event in events
        if isinstance(event.get("coverage"), Mapping)
    )
    coverage_partial = any(
        _coverage_is_partial(coverage)
        for event in events
        if isinstance((coverage := event.get("coverage")), Mapping)
    )
    observed = failed.get("outcome") if failed else (events[-1].get("outcome") if events else "unknown")
    conditions: dict[str, Any] = {
        "expected": "operation records a completed, failed, unavailable, or observed outcome",
        "observed": observed,
    }
    if latest_authority:
        conditions["authority_application"] = dict(latest_authority)
        if latest_authority.get("status") == "review_required":
            conditions["review_partition"] = {
                "expected_blocked_tile_count": 0,
                "observed_affected_tile_count": affected_count,
            }
    if isinstance(operation, Mapping):
        conditions["repository_operation"] = {
            "expected": "completed",
            "observed": operation.get("status", "unknown"),
        }
    result = {
        "available": True,
        "state": safe_state,
        "scan_id": safe_scan_id,
        "identity": {"scan_id": safe_scan_id, "build_sha": _safe_build_sha(status.get("scan_build_sha"))},
        "conditions": conditions,
        "events": events,
        "dropped_event_count": _safe_nonnegative_int(ledger.get("dropped_event_count")) + max(0, len(raw_events) - len(events)),
        "truncated_event_count": _safe_nonnegative_int(ledger.get("truncated_event_count")),
        "additional_status_write_count": _safe_nonnegative_int(ledger.get("additional_status_write_count")),
        "additional_status_bytes": _safe_nonnegative_int(ledger.get("additional_status_bytes")),
        "durability": "observed_best_effort",
        "persistence": {"ledger": "observed_best_effort", "readback": context.get("status_readback", "unknown"), "restart": "unknown"},
        "dependency": dependency or {"observed": False},
        "artifacts": artifacts or {"observed": False},
        "impact": {"scan_state": safe_state, "score_or_publication_changed": "unknown", "coverage_partial": coverage_partial},
        "recovery": {"retry_advice": "not_provided", "exact_resume": "unsupported"},
        "exact_resume": {"supported": False, "reason": "exact_resume_action_trace_unsupported"},
    }
    while events and len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")) > _DIAGNOSTIC_LEDGER_MAX_BYTES:
        events.pop(0)
        result["dropped_event_count"] += 1
    # Persisted inputs may be malformed.  Fail closed to a small valid dossier,
    # never an endpoint 500 and never raw persisted content.
    if len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")) > _DIAGNOSTIC_LEDGER_MAX_BYTES:
        result["events"] = []
        result["truncated_event_count"] += 1
    return result


def scan_diagnostic_from_status(status: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project persisted scanner state into the closed public diagnostic shape."""

    current = status.get("diagnostic")
    is_current = isinstance(current, Mapping)
    state = status.get("state")
    if state == "error":
        code = status.get("error_code")
        if code == "process_restarted":
            return _diagnostic(
                kind="execution_failed",
                stage="unknown",
                capture_state=_capture_state(status),
                reason_codes="process_restarted",
                build_sha=status.get("scan_build_sha"),
                unknowns=["legacy_status_without_diagnostic"],
            )
        if is_current and current.get("kind") == "execution_failed":
            origin = current.get("origin") if isinstance(current.get("origin"), Mapping) else None
            return _diagnostic(
                kind="execution_failed",
                stage=current.get("stage"),
                capture_state=current.get("capture_state"),
                reason_codes=current.get("reason_codes"),
                build_sha=current.get("build_sha"),
                origin=origin,
                unknowns=current.get("unknowns"),
            )
        safe_codes = _safe_reason_codes(code)
        return _diagnostic(
            kind="execution_failed",
            stage=status.get("execution_stage"),
            capture_state=_capture_state(status),
            reason_codes=safe_codes or "scan_execution_failed",
            build_sha=status.get("scan_build_sha"),
            unknowns=["legacy_status_without_diagnostic"],
        )
    if isinstance(current, Mapping):
        kind = current.get("kind")
        if isinstance(kind, str) and kind in {"execution_failed", "assessment_unavailable", "secondary_failure"}:
            origin = current.get("origin") if isinstance(current.get("origin"), Mapping) else None
            return _diagnostic(
                kind=kind,
                stage=current.get("stage"),
                capture_state=current.get("capture_state"),
                reason_codes=current.get("reason_codes"),
                build_sha=current.get("build_sha"),
                origin=origin,
                unknowns=current.get("unknowns"),
            )
    vault = status.get("vault")
    if isinstance(vault, Mapping) and vault.get("state") == "failed":
        return _diagnostic(
            kind="secondary_failure", stage="vault_sidecar", capture_state=_capture_state(status),
            reason_codes="vault_sidecar_failed", build_sha=status.get("scan_build_sha"),
        )
    return None


def scan_diagnostic_from_report(
    report: Mapping[str, Any],
    *,
    assessment: Any = _DIAGNOSTIC_ASSESSMENT_UNSET,
) -> dict[str, Any] | None:
    """Expose the authoritative no-score outcome without inventing a failure."""

    if assessment is _DIAGNOSTIC_ASSESSMENT_UNSET:
        try:
            assessment = _validate_report_sv9_assessment(report, required=False)
        except ScannerReportAssessmentError:
            return None
    if not isinstance(assessment, Mapping):
        return None
    if assessment.get("availability") != "unavailable":
        return None
    raw = report.get("raw") if isinstance(report.get("raw"), Mapping) else {}
    source_capture = raw.get("source_capture")
    capture_state = "completed" if isinstance(source_capture, Mapping) else "unknown"
    envelope = assessment.get("assessment") if isinstance(assessment.get("assessment"), Mapping) else {}
    reason_codes = _safe_reason_codes(envelope.get("reason_codes"))
    return _diagnostic(
        kind="assessment_unavailable", stage="vault_authority", capture_state=capture_state,
        reason_codes=reason_codes, build_sha=report.get("pipeline_commit_sha"),
        unknowns=[] if reason_codes else ["assessment_reason_not_available"],
    )


@dataclasses.dataclass(frozen=True)
class _ScanOwner: scan_id: str; kind: str; token: object
@dataclasses.dataclass(frozen=True)
class _ExactResumePublication: action: str; report_id: str
class _ExactResumeFailure(RuntimeError):
    def __init__(self, kind: str) -> None:
        if kind not in _EXACT_RESUME_FAILURE_KINDS: raise ValueError("exact resume failure kind is invalid")
        self.kind = kind; super().__init__(kind)
_SCAN_OWNERS: dict[str, _ScanOwner] = {}
_EXACT_RESUME_FAILURE_KINDS = frozenset({"busy", "operation_invalid", "report_invalid", "superseded"})
def _acquire_scan_owner_locked(scan_id: str, kind: str) -> _ScanOwner | None:
    if kind not in {"ordinary", "exact_resume"}: raise ValueError("scan owner kind is invalid")
    if scan_id in _SCAN_OWNERS: return None
    owner = _ScanOwner(scan_id, kind, object())
    _SCAN_OWNERS[scan_id] = owner
    return owner
def _acquire_scan_owner(scan_id: str, kind: str) -> _ScanOwner | None:
    with _LOCK: return _acquire_scan_owner_locked(scan_id, kind)
def _current_scan_owner(owner: _ScanOwner) -> bool:
    with _LOCK: return _SCAN_OWNERS.get(owner.scan_id) == owner
def _release_scan_owner(owner: _ScanOwner) -> bool:
    with _LOCK:
        if _SCAN_OWNERS.get(owner.scan_id) != owner: return False
        del _SCAN_OWNERS[owner.scan_id]
        return True

_PHASES = (
    ("capture", "Capture: owned pages, Exa, GitHub proof, SearchAPI fallback, visual evidence"),
    ("interpret", "Evidence pack → shortlists → gated LLM interpretation"),
    ("score", "Tile signals → SV9 components → score"),
    ("report", "Coverage + report assembly"),
)


def normalize_url(raw: str) -> str:
    valid, normalized_or_error = validate_url(raw)
    if not valid:
        raise ValueError(normalized_or_error)
    return normalized_or_error


def default_brand_name(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    label = host.split(".")[0] if host else "brand"
    return label.capitalize()


def start_scan(
    url: str,
    brand_name: str = "",
    *,
    allow_degraded_fallback: bool = False,
    scan_id: str | None = None,
    client_id: str = "",
) -> str:
    url = normalize_url(url)
    brand_name = (brand_name or "").strip() or default_brand_name(url)
    started_at = datetime.now(timezone.utc).isoformat()
    explicit = bool(scan_id)
    owner: _ScanOwner | None = None
    created_status: dict[str, Any] | None = None
    for _attempt in range(8):
        candidate = str(scan_id if explicit else new_scan_id())
        with _LOCK:
            owner = None if candidate in _SCANS else _acquire_scan_owner_locked(candidate, "ordinary")
            if owner is not None:
                created_status = {"id": candidate, "url": url, "brand_name": brand_name, "state": "running", "phase": "capture", "execution_stage": "capture", "scan_build_sha": _safe_build_sha(current_build_sha()), "phases": [{"key": key, "label": label, "state": "pending"} for key, label in _PHASES], "acquisition": [], "acquisition_gate": {"state": "pending", "issues": [], "warnings": [], "fallbacks": []}, "allow_degraded_fallback": bool(allow_degraded_fallback), "error": None, "error_code": None, "started_at": started_at, "completed_at": None}
                _append_diagnostic_operation_locked(created_status, _diagnostic_operation(operation="scan_created", stage="capture", outcome="started"))
                _SCANS[candidate] = created_status
                _SCAN_EVENTS[candidate] = threading.Event()
                persisted_status = _status_copy_locked(created_status)
                scan_id = candidate
                break
        if explicit:
            break
    if owner is None or created_status is None: raise ValueError("scan_id is already active")

    def rollback() -> None:
        with _LOCK:
            if _SCAN_OWNERS.get(owner.scan_id) != owner: return
            if _SCANS.get(owner.scan_id) is created_status: _SCANS.pop(owner.scan_id, None); _SCAN_EVENTS.pop(owner.scan_id, None)
            _SCAN_OWNERS.pop(owner.scan_id, None)
    try:
        _persist_scan_status(
            persisted_status,
            request_payload={
                "url": url,
                "brand_name": brand_name,
                "allow_degraded_fallback": bool(allow_degraded_fallback),
            },
            client_id=client_id,
        )
    except Exception:
        rollback()
        raise
    try:
        thread = threading.Thread(target=_run, args=(scan_id, url, brand_name, bool(allow_degraded_fallback), owner), daemon=True)
        thread.start()
    except Exception:
        rollback()
        raise
    return str(scan_id)


def scan_status(scan_id: str) -> dict[str, Any] | None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status:
            return _status_copy_locked(status)
    return _load_persisted_scan_status(scan_id)


def recover_interrupted_scans() -> int:
    """Mark process-bound jobs left behind by a restart as interrupted."""

    from src.config import BRAND3_DB_PATH
    from src.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(BRAND3_DB_PATH)
    try:
        return store.interrupt_incomplete_scanner_jobs()
    finally:
        store.close()


def approve_degraded_scan(scan_id: str) -> dict[str, Any] | None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None:
            return None
        gate = status.get("acquisition_gate") if isinstance(status.get("acquisition_gate"), dict) else {}
        if status.get("state") != "blocked" or not gate.get("can_continue"):
            return {"state": str(status.get("state") or "unknown"), "approved": False, "reason": "not_continuable"}
        approved_gate = _approve_acquisition_gate(gate, decision_source="user")
        status["acquisition_gate"] = approved_gate
        status["state"] = "running"
        status["phase"] = "interpret"
        _set_phase_locked(status, "interpret", "pending")
        event = _SCAN_EVENTS.get(scan_id)
        persisted_status = _status_copy_locked(status)
    _persist_scan_status(persisted_status)
    if event:
        event.set()
    return {"state": "running", "approved": True, "acquisition_gate": approved_gate}


def cancel_scan(scan_id: str) -> dict[str, Any] | None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None:
            return None
        if scan_id in _VAULT_ACTIVATIONS:
            return {
                "state": str(status.get("state") or "running"),
                "cancelled": False,
                "reason": "vault_activation_in_progress",
                "acquisition_gate": status.get("acquisition_gate") or {},
            }
        if status.get("state") in {"cancelled", "error", "done"}:
            return {
                "state": str(status.get("state") or ""),
                "cancelled": status.get("state") == "cancelled",
                "acquisition_gate": status.get("acquisition_gate") or {},
            }
        gate = status.get("acquisition_gate") if isinstance(status.get("acquisition_gate"), dict) else {}
        if isinstance(gate, dict):
            gate = dict(gate)
            gate["user_decision"] = "cancelled"
            gate["state"] = "cancelled"
            status["acquisition_gate"] = gate
        status["state"] = "cancelled"
        status["phase"] = "capture"
        status["completed_at"] = datetime.now(timezone.utc).isoformat()
        _mark_pending_phases_locked(status, "cancelled")
        event = _SCAN_EVENTS.pop(scan_id, None)
        persisted_status = _status_copy_locked(status)
    _persist_scan_status(persisted_status)
    if event:
        event.set()
    return {"state": "cancelled", "cancelled": True, "acquisition_gate": gate}


def _set_phase(scan_id: str, key: str, state: str) -> None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if not status or status.get("state") in {"cancelled", "error", "done"}:
            return
        if state == "running":
            status["phase"] = key
            status["execution_stage"] = key
        _set_phase_locked(status, key, state)
        persisted_status = _status_copy_locked(status)
    _persist_scan_status(persisted_status)
    with _LOCK:
        current = _SCANS.get(scan_id)
        terminal_status = (
            _status_copy_locked(current)
            if current is not None
            and current.get("state") in {"cancelled", "error", "done"}
            else None
        )
    if terminal_status is not None:
        _persist_scan_status(terminal_status)


def _set_phase_locked(status: dict[str, Any], key: str, state: str) -> None:
    for phase in status["phases"]:
        if phase["key"] == key:
            phase["state"] = state


def _set_execution_stage(scan_id: str, stage: str) -> None:
    """Track the active internal stage; terminal persistence captures it once."""

    if stage not in _DIAGNOSTIC_STAGES:
        return
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is not None and status.get("state") not in {"cancelled", "error", "done"}:
            status["execution_stage"] = stage


def _scan_build_sha(scan_id: str) -> str:
    with _LOCK:
        status = _SCANS.get(scan_id)
        return _safe_build_sha((status or {}).get("scan_build_sha"))


def _mark_pending_phases_locked(status: dict[str, Any], state: str) -> None:
    for phase in status.get("phases") or []:
        if phase.get("state") in {"pending", "running"}:
            phase["state"] = state


def _status_copy_locked(status: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(status)


def _persist_scan_status(
    status: dict[str, Any],
    *,
    request_payload: dict[str, Any] | None = None,
    client_id: str = "",
) -> None:
    from src.config import BRAND3_DB_PATH
    from src.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(BRAND3_DB_PATH)
    try:
        store.save_scanner_job_status(
            status,
            request_payload=request_payload,
            client_id=client_id,
        )
    finally:
        store.close()


def _activate_vault_result_unless_cancelled(
    scan_id: str,
    repository: Any,
    url: str,
    *,
    operation_plan_fingerprint: str,
) -> dict[str, Any] | None:
    """Guard cancellation from activation entry through report publication."""

    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None or status.get("state") in {
            "cancelled",
            "error",
            "done",
        }:
            return None
        _VAULT_ACTIVATIONS.add(scan_id)
    # Activation can commit more than once before raising.  Keep the guard on
    # both success and failure; only _run may publish a terminal state and
    # remove it while holding the same lock used by cancellation.
    return repository.activate_evidence_vault_operational_scanner_result(
        url,
        source_scan_id=scan_id,
        operation_plan_fingerprint=operation_plan_fingerprint,
        workspace_slug="b3s",
    )


def _vault_published_report_id(url: str, candidate: dict[str, Any]) -> str:
    """Keep the selected brand analysis unless this candidate replaces it."""

    candidate_id = str(candidate.get("id") or "")
    selected, _classified, _state = selected_report_for_display(
        [*list_reports_for_domain(url), candidate]
    )
    selected_id = str((selected or {}).get("id") or "")
    return selected_id or candidate_id


def _finish_scan_without_new_score(scan_id: str, report_id: str) -> bool:
    """Close a recapture that did not replace the published brand score."""

    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None or status.get("state") == "cancelled":
            return False
        _set_phase_locked(status, "report", "done")
        status["state"] = "done"
        status["phase"] = "done"
        status["report_id"] = report_id
        status["score_unchanged"] = True
        status["completed_at"] = datetime.now(timezone.utc).isoformat()
        _SCAN_EVENTS.pop(scan_id, None)
        _VAULT_ACTIVATIONS.discard(scan_id)
        persisted_status = _status_copy_locked(status)
    _persist_scan_status(persisted_status)
    return True


def _publish_completed_report(
    scan_id: str,
    report: dict[str, Any],
    *,
    assessment: Any = _DIAGNOSTIC_ASSESSMENT_UNSET,
) -> bool:
    """Publish one immutable report with the same cancellation boundary."""

    diagnostic = scan_diagnostic_from_report(report, assessment=assessment)
    operation_started = time.monotonic()
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None or status.get("state") == "cancelled":
            return False
        # Keep diagnostic timeline entries in the same existing final status
        # snapshot; a post-persist side write could race cancellation.
        _append_diagnostic_operation_locked(
            status,
            _diagnostic_operation(
                operation="report_save", stage="report", outcome="started",
            ),
        )
        try:
            save_report(report)
        except Exception as exc:
            _append_diagnostic_operation_locked(
                status,
                _diagnostic_operation(
                    operation="report_save", stage="report", outcome="failed",
                    exc=exc, started_monotonic=operation_started,
                ),
            )
            raise
        _append_diagnostic_operation_locked(
            status,
            _diagnostic_operation(
                operation="report_save", stage="report", outcome="completed",
                started_monotonic=operation_started,
            ),
        )
        _set_phase_locked(status, "report", "done")
        status["state"] = "done"
        status["phase"] = "done"
        status["report_id"] = str(report.get("id") or scan_id)
        if diagnostic is not None:
            status["diagnostic"] = diagnostic
        else:
            status.pop("diagnostic", None)
        status["completed_at"] = datetime.now(timezone.utc).isoformat()
        _SCAN_EVENTS.pop(scan_id, None)
        _VAULT_ACTIVATIONS.discard(scan_id)
        persisted_status = _status_copy_locked(status)
    _persist_scan_status(persisted_status)
    return True


def _load_persisted_scan_status(scan_id: str) -> dict[str, Any] | None:
    from src.config import BRAND3_DB_PATH
    from src.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(BRAND3_DB_PATH)
    try:
        return store.get_scanner_job_status(scan_id)
    finally:
        store.close()


def _scan_cancelled(scan_id: str) -> bool:
    with _LOCK:
        status = _SCANS.get(scan_id)
        return status is None or status.get("state") == "cancelled"


def _exact_owner_current(scan_id: str, token: object) -> bool:
    with _LOCK: owner = _SCAN_OWNERS.get(scan_id); return bool(owner and owner.kind == "exact_resume" and owner.token is token)
def _load_report_for_exact_owner(owner_scan_id: str, token: object, report_id: str) -> dict[str, Any] | None:
    with _LOCK:
        owner = _SCAN_OWNERS.get(owner_scan_id)
        if owner is None or owner.kind != "exact_resume" or owner.token is not token: raise _ExactResumeFailure("busy")
        return load_report(report_id)
def _exact_successor_report_id(action: Mapping[str, Any]) -> str | None:
    action_id = action.get("action_id")
    if not isinstance(action_id, str) or not action_id.strip():
        return None
    return f"exact-resume-{canonical_json_hash(action_id)}"
def _validate_exact_report(report: Any, *, scan_id: str, binding: Mapping[str, Any], report_id: str) -> dict[str, Any]:
    if not isinstance(report, Mapping): raise _ExactResumeFailure("report_invalid")
    raw = report.get("raw"); source_capture = raw.get("source_capture") if isinstance(raw, Mapping) else None
    valid = report.get("id") == report_id and scan_id == binding.get("source_scan_id") and normalize_domain(str(report.get("url") or "")) == binding.get("canonical_domain") and isinstance(raw, Mapping) and raw.get("source_run_id") == scan_id and source_capture == {key: binding.get(key) for key in ("source_scan_id", "observation_hash", "capture_hash")}
    if not valid: raise _ExactResumeFailure("report_invalid")
    try: projection = _validate_report_sv9_assessment(report, required=True)
    except ScannerReportAssessmentError as exc: raise _ExactResumeFailure("report_invalid") from exc
    return projection
def _validate_exact_current_report(report: Any, *, scan_id: str, binding: Mapping[str, Any]) -> dict[str, Any]:
    return _validate_exact_report(report, scan_id=scan_id, binding=binding, report_id=scan_id)
def _validate_exact_successor_report(report: Any, *, scan_id: str, binding: Mapping[str, Any], report_id: str) -> dict[str, Any]:
    projection = _validate_exact_report(report, scan_id=scan_id, binding=binding, report_id=report_id)
    if projection.get("availability") != "available": raise _ExactResumeFailure("report_invalid")
    return projection
def _load_exact_successor_publication(scan_id: str, token: object, action: Mapping[str, Any], binding: Mapping[str, Any]) -> _ExactResumePublication | None:
    successor_id = _exact_successor_report_id(action)
    if successor_id is None: return None
    successor = _load_report_for_exact_owner(scan_id, token, successor_id)
    if successor is None: return None
    _validate_exact_successor_report(successor, scan_id=scan_id, binding=binding, report_id=successor_id)
    return _ExactResumePublication("publish_current", successor_id)
def _publish_exact_report(scan_id: str, owner: _ScanOwner, report: Mapping[str, Any], binding: Mapping[str, Any], action: str, *, action_identity: Mapping[str, Any] | None = None, initial_source: Mapping[str, Any] | None = None) -> _ExactResumePublication:
    if not _current_scan_owner(owner): raise _ExactResumeFailure("busy")
    if action_identity is None:
        save_report(dict(report)); saved = _load_report_for_exact_owner(scan_id, owner.token, scan_id)
        _validate_exact_current_report(saved, scan_id=scan_id, binding=binding)
        return _ExactResumePublication(action, scan_id)
    successor_id = _exact_successor_report_id(action_identity)
    if successor_id is None: raise _ExactResumeFailure("operation_invalid")
    expected = dict(report)
    expected["id"] = successor_id if action != "record_no_score" else scan_id
    current = _load_report_for_exact_owner(scan_id, owner.token, scan_id)
    successor = _load_report_for_exact_owner(scan_id, owner.token, successor_id)
    if action == "record_no_score":
        if _validate_exact_current_report(report, scan_id=scan_id, binding=binding).get("availability") != "unavailable":
            raise _ExactResumeFailure("report_invalid")
        if initial_source is not None:
            if current != initial_source: raise _ExactResumeFailure("report_invalid")
        elif current is not None and current != report:
            raise _ExactResumeFailure("report_invalid")
        if current is not None and _validate_exact_current_report(current, scan_id=scan_id, binding=binding).get("availability") != "unavailable":
            raise _ExactResumeFailure("report_invalid")
        if successor is not None: raise _ExactResumeFailure("report_invalid")
        if current is None:
            save_report(dict(report)); current = _load_report_for_exact_owner(scan_id, owner.token, scan_id)
            if current != report: raise _ExactResumeFailure("report_invalid")
            if _validate_exact_current_report(current, scan_id=scan_id, binding=binding).get("availability") != "unavailable":
                raise _ExactResumeFailure("report_invalid")
        return _ExactResumePublication(action, scan_id)
    if initial_source is not None:
        if current != initial_source: raise _ExactResumeFailure("report_invalid")
    elif current is not None:
        raise _ExactResumeFailure("report_invalid")
    _validate_exact_successor_report(expected, scan_id=scan_id, binding=binding, report_id=successor_id)
    if successor is not None:
        _validate_exact_successor_report(successor, scan_id=scan_id, binding=binding, report_id=successor_id)
        if successor != expected: raise _ExactResumeFailure("report_invalid")
        return _ExactResumePublication(action, successor_id)
    save_report(expected); successor = _load_report_for_exact_owner(scan_id, owner.token, successor_id)
    if successor != expected: raise _ExactResumeFailure("report_invalid")
    _validate_exact_successor_report(successor, scan_id=scan_id, binding=binding, report_id=successor_id)
    return _ExactResumePublication(action, successor_id)
def _run_vault_exact_resume(*, scan_id: str, action: Mapping[str, Any], repository: Any) -> _ExactResumePublication:
    """Private VR2 seam; it is intentionally not wired to an API route yet."""
    owner: _ScanOwner | None = None; prepared: Mapping[str, Any] | None = None
    try:
        from src.services import evidence_vault_scan_orchestration as orchestration

        owner = _acquire_scan_owner(scan_id, "exact_resume")
        if owner is None: raise vault_exact_resume_error("busy")
        prepared = orchestration.prepare_vault_exact_resume(repository=repository, action=action, scan_id=scan_id)
        binding = prepared["report_binding"]
        successor = _load_exact_successor_publication(scan_id, owner.token, action, binding)
        if successor is not None: return successor
        current = _load_report_for_exact_owner(scan_id, owner.token, scan_id)
        if current is not None:
            projection = _validate_exact_current_report(current, scan_id=scan_id, binding=binding)
            if projection.get("availability") == "available":
                return _ExactResumePublication("publish_current", scan_id)
        return _run_vault_sv9_authority_scanner(scan_id=scan_id, url=str(prepared["url"]), brand_name=str(prepared["brand_name"]), repository=repository, preparation=prepared["preparation"], canonical_snapshot=prepared["canonical_snapshot"], canonical_source_capture={key: str(binding[key]) for key in ("source_scan_id", "observation_hash", "capture_hash")}, gate={}, exact_owner=owner, exact_report_binding=binding, exact_action=action, exact_initial_source=current)
    except VaultExactResumeError: raise
    except _ExactResumeFailure as exc: raise vault_exact_resume_error(exc.kind) from None
    except Exception:
        if owner is None: raise vault_exact_resume_error("execution_failed") from None
        if not _exact_owner_current(scan_id, owner.token): raise vault_exact_resume_error("busy") from None
        try: orchestration.prepare_vault_exact_resume(repository=repository, action=action, scan_id=scan_id)
        except VaultExactResumeError: raise
        except Exception: pass
        if not _exact_owner_current(scan_id, owner.token): raise vault_exact_resume_error("busy")
        raise vault_exact_resume_error("execution_failed") from None
    finally:
        if owner is not None: _release_scan_owner(owner)


def _vault_operational_pipeline_enabled() -> bool:
    return (
        os.environ.get("BRAND3_ENVIRONMENT", "").strip().lower() == "vault"
        and os.environ.get(
            "BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED",
            "false",
        ).strip().lower()
        == "true"
    )


def _vault_sv9_authority_scanner_enabled() -> bool:
    return _vault_operational_pipeline_enabled() and os.environ.get(
        "BRAND3_VAULT_SV9_AUTHORITY_SCANNER_ENABLED"
    ) == "true"


def _vault_sv9_judgment_shadow_enabled() -> bool:
    return _vault_operational_pipeline_enabled() and os.environ.get(
        "BRAND3_VAULT_SV9_JUDGMENT_SHADOW_ENABLED"
    ) == "true"


def _run_vault_sv9_judgment_shadow_after_publication(
    *, scan_id: str, repository: Any, payload: Mapping[str, Any], report: Mapping[str, Any]
) -> None:
    if not _vault_sv9_judgment_shadow_enabled():
        return
    try:
        from src.config import SV9_FLOW_MODEL
        from src.services.evidence_vault_sv9_judgment_shadow import run_evidence_vault_sv9_judgment_shadow
        from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapter
        from src.sv9.judgment_memory import build_judgment_series_contract
        from src.sv9.shadow_component_provider import FlowSv9ShadowJsonProvider, shadow_provider_environment_snapshot

        flow = (payload.get("flow") or {}) if isinstance(payload, Mapping) else {}
        candidate = flow.get("candidate") if isinstance(flow, Mapping) else {}
        pack = candidate.get("evidence_pack") if isinstance(candidate, Mapping) else {}
        refs = sorted({str(row.get("ref") or "").strip() for row in (pack.get("evidence") or []) if isinstance(row, Mapping) and str(row.get("ref") or "").strip()}) if isinstance(pack, Mapping) else []
        model = os.environ.get("BRAND3_FLOW_INTERPRETATION_MODEL") or SV9_FLOW_MODEL
        result = run_evidence_vault_sv9_judgment_shadow(repository=repository, flow=FlowSv9StrictComponentAdapter(FlowSv9ShadowJsonProvider(), environ=shadow_provider_environment_snapshot(os.environ), model=model), source_scan_id=scan_id, current_series_contract=build_judgment_series_contract(evaluator_version="evidence-vault-sv9-judgment-shadow-v1", prompt_version="sv9-strict-component-v1", model_version=model, flow_version="sv9-flow-strict-component-v1", normalization_version="vault-capture-v1"), advisory_evidence_refs=refs, current_public_score=report.get("score") if isinstance(report, Mapping) else None)
        _LOG.info("vault SV9 judgment shadow completed", extra={"scan_id": scan_id, **{key: result.get(key) for key in ("status", "reason_code", "exception_class", "calls_issued", "calls_avoided", "reused_tiles", "reopened_tiles", "evaluated_tiles", "tile_diffs", "current_score", "candidate_score", "candidate_fingerprint", "divergence_reasons")}})
    except Exception as exc:
        _LOG.warning("vault SV9 judgment shadow failed", extra={"scan_id": scan_id, "reason_code": "shadow_exception", "exception_class": type(exc).__name__})


def _canonical_snapshot_from_persisted_vault_capture(
    *,
    scan_id: str,
    url: str,
    expected_snapshot: Mapping[str, Any],
    report_observation: Any,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Read the canonical Flow input from the validated persisted capture.

    The operational pipeline can derive memory or diagnostic scores from the
    same capture, but neither is an input to Flow/SV9.  Parsing here performs
    the JSON normalization and recomputes the content-addressed identities
    used in the immutable report binding.
    """

    from src.history.capture_observation import parse_capture_observation
    from src.history.report_parser import canonical_json_hash, normalize_domain

    if not isinstance(report_observation, dict):
        raise RuntimeError("vault_persisted_capture_unavailable")
    try:
        parsed = parse_capture_observation(report_observation)
    except Exception as exc:
        raise RuntimeError("vault_persisted_capture_invalid") from exc
    if parsed.source_scan_id != str(scan_id):
        raise RuntimeError("vault_persisted_capture_scan_mismatch")
    if normalize_domain(parsed.canonical_url) != normalize_domain(url):
        raise RuntimeError("vault_persisted_capture_domain_mismatch")
    if parsed.pipeline_version == "evidence-vault-trusted-acquisition-v1":
        source_capture = expected_snapshot.get("source_capture")
        if not (
            isinstance(source_capture, Mapping)
            and str(source_capture.get("source_scan_id") or "")
            == parsed.source_scan_id
            and str(source_capture.get("observation_hash") or "")
            == parsed.observation_hash
            and str(source_capture.get("capture_hash") or "")
            == parsed.capture_hash
        ):
            raise RuntimeError("vault_persisted_capture_snapshot_mismatch")
        raw_inputs = expected_snapshot.get("raw_inputs")
        if not isinstance(raw_inputs, list) or not all(
            isinstance(row, Mapping) and isinstance(row.get("payload"), Mapping)
            for row in raw_inputs
        ):
            raise RuntimeError("vault_persisted_capture_evidence_mismatch")
        projected_raw_inputs = copy.deepcopy(raw_inputs)
        projected_raw_inputs.sort(
            key=lambda row: (
                str(row["payload"].get("role") or ""),
                str(row["payload"].get("receipt_fingerprint") or ""),
            )
        )
        canonical_snapshot = {
            "run": {
                "brand_name": parsed.brand_name,
                "id": _stable_verified_source_run_id(scan_id),
                "url": parsed.canonical_url,
            },
            "raw_inputs": projected_raw_inputs,
            "features": [],
        }
        from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot

        rebuilt_evidence = build_evidence_pack_from_snapshot(
            canonical_snapshot,
            include_acquisition_steps=False,
        ).to_dict()["evidence"]
        persisted_evidence = [dict(row) for row in parsed.evidence_records]
        if canonical_json_hash(rebuilt_evidence) != canonical_json_hash(
            persisted_evidence
        ):
            raise RuntimeError("vault_persisted_capture_evidence_mismatch")
    else:
        try:
            expected_capture_hash = canonical_json_hash(dict(expected_snapshot))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("vault_persisted_capture_invalid") from exc
        if parsed.capture_hash != expected_capture_hash:
            raise RuntimeError("vault_persisted_capture_snapshot_mismatch")
    return (
        canonical_snapshot
        if parsed.pipeline_version == "evidence-vault-trusted-acquisition-v1"
        else copy.deepcopy(parsed.capture_payload),
        {
            "source_scan_id": parsed.source_scan_id,
            "observation_hash": parsed.observation_hash,
            "capture_hash": parsed.capture_hash,
        },
    )


def _read_back_persisted_vault_capture(
    *,
    repository: Any,
    preparation: Mapping[str, Any],
    scan_id: str,
    url: str,
) -> dict[str, Any]:
    """Return the repository readback when the Vault repository exposes it."""

    candidate = preparation.get("report_observation")
    readback = getattr(repository, "list_capture_observations_for_domain", None)
    if callable(readback):
        try:
            captures = readback(url, workspace_slug="b3s", limit=500)
        except Exception as exc:
            raise RuntimeError("vault_persisted_capture_readback_failed") from exc
        candidate = next(
            (
                row.get("raw_observation")
                for row in captures
                if isinstance(row, Mapping)
                and str(row.get("source_scan_id") or "") == str(scan_id)
            ),
            None,
        )
    if not isinstance(candidate, dict):
        raise RuntimeError("vault_persisted_capture_unavailable")
    return candidate


def _record_vault_sidecar_status(scan_id: str, detail: dict[str, Any]) -> None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None:
            return
        status["vault"] = detail
        persisted_status = _status_copy_locked(status)
    try:
        _persist_scan_status(persisted_status)
    except Exception:
        _LOG.exception(
            "failed to persist Vault diagnostic sidecar status",
            extra={"scan_id": scan_id},
        )


def _execute_vault_operational_preparation(
    *,
    scan_id: str,
    repository: Any,
    preparation: Mapping[str, Any],
) -> str:
    """Execute or resume one operational plan and return its activation binding."""

    operation_plan = preparation.get("operation_plan")
    resume = (
        dict(preparation.get("resume") or {})
        if isinstance(preparation.get("resume"), Mapping)
        else {}
    )
    if isinstance(operation_plan, Mapping):
        from src.services.evidence_vault_incremental_executor import (
            execute_vault_operation_plan,
        )

        operation_requires_llm = bool(
            (operation_plan.get("operations") or {}).get("llm_required")
            and resume.get("materialization_required") is not True
        )
        operation_llm = None
        if operation_requires_llm:
            from src.config import SV9_FLOW_MODEL
            from src.features.llm_analyzer import LLMAnalyzer

            operation_llm = LLMAnalyzer(
                model=os.environ.get("BRAND3_FLOW_INTERPRETATION_MODEL")
                or SV9_FLOW_MODEL
            )
        execution = execute_vault_operation_plan(
            repository=repository,
            source_scan_id=scan_id,
            worker_id=f"vault-scan-{scan_id}",
            llm=operation_llm,
            workspace_slug="b3s",
        )
        execution_status = str(execution.get("execution_status") or "unknown")
        if execution_status != "completed":
            raise RuntimeError("vault_operation_not_completed:" + execution_status)
        fingerprint = str(operation_plan.get("operation_plan_fingerprint") or "")
        if not fingerprint:
            raise RuntimeError("vault_operation_plan_fingerprint_unavailable")
        return fingerprint
    if resume.get("analysis_status") == "completed":
        fingerprint = str(resume.get("operation_plan_fingerprint") or "")
        if not fingerprint:
            raise RuntimeError("vault_completed_operation_missing_plan_fingerprint")
        return fingerprint
    raise RuntimeError("vault_operation_resume_incomplete")


def _run_vault_operational_sidecar(
    *,
    scan_id: str,
    url: str,
    repository: Any,
    preparation: Mapping[str, Any],
) -> bool:
    """Run Vault operational work as a non-authoritative diagnostic sidecar.

    ``False`` only communicates user cancellation.  Operational planning,
    execution, activation, memory, and score failures are retained in scan
    diagnostics but must never replace or block the already-built canonical
    Flow/SV9 result.
    """

    if _scan_cancelled(scan_id):
        return False
    operation_plan = preparation.get("operation_plan")
    resume = (
        dict(preparation.get("resume") or {})
        if isinstance(preparation.get("resume"), Mapping)
        else {}
    )
    detail: dict[str, Any] = {
        "mode": str(preparation.get("mode") or "unknown"),
        "role": "diagnostic_sidecar",
        "state": "not_required",
    }
    try:
        activation: dict[str, Any] | None = None
        if isinstance(operation_plan, Mapping):
            from src.services.evidence_vault_incremental_executor import (
                execute_vault_operation_plan,
            )

            operation_requires_llm = bool(
                (operation_plan.get("operations") or {}).get("llm_required")
                and resume.get("materialization_required") is not True
            )
            operation_llm = None
            if operation_requires_llm:
                from src.config import SV9_FLOW_MODEL
                from src.features.llm_analyzer import LLMAnalyzer

                operation_llm = LLMAnalyzer(
                    model=(
                        os.environ.get("BRAND3_FLOW_INTERPRETATION_MODEL")
                        or SV9_FLOW_MODEL
                    )
                )
            execution = execute_vault_operation_plan(
                repository=repository,
                source_scan_id=scan_id,
                worker_id=f"vault-scan-{scan_id}",
                llm=operation_llm,
                workspace_slug="b3s",
            )
            execution_status = str(execution.get("execution_status") or "unknown")
            detail["execution_status"] = execution_status
            if execution_status != "completed":
                raise RuntimeError(
                    "vault_operation_not_completed:" + execution_status
                )
            if _scan_cancelled(scan_id):
                return False
            activation = _activate_vault_result_unless_cancelled(
                scan_id,
                repository,
                url,
                operation_plan_fingerprint=str(
                    operation_plan.get("operation_plan_fingerprint") or ""
                ),
            )
            if activation is None:
                return False
            detail["state"] = "completed"
        elif (
            resume.get("analysis_status") == "completed"
            and resume.get("semantic_work_completed") is True
        ):
            operation_plan_fingerprint = str(
                resume.get("operation_plan_fingerprint") or ""
            )
            if not operation_plan_fingerprint:
                raise RuntimeError(
                    "vault_completed_operation_missing_plan_fingerprint"
                )
            if _scan_cancelled(scan_id):
                return False
            activation = _activate_vault_result_unless_cancelled(
                scan_id,
                repository,
                url,
                operation_plan_fingerprint=operation_plan_fingerprint,
            )
            if activation is None:
                return False
            detail["state"] = "completed"

        if isinstance(activation, Mapping):
            memory = activation.get("memory")
            if isinstance(memory, Mapping):
                detail["memory_version"] = str(
                    memory.get("canonical_memory_version") or ""
                )
            detail["activation_created"] = activation.get("created") is True
    except Exception as exc:
        _LOG.exception(
            "vault operational sidecar failed after canonical SV9 assessment",
            extra={"scan_id": scan_id},
        )
        with _LOCK:
            _VAULT_ACTIVATIONS.discard(scan_id)
        detail.update(
            {
                "state": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "diagnostic": _diagnostic(
                    kind="secondary_failure",
                    stage="vault_sidecar",
                    capture_state="completed",
                    reason_codes="vault_sidecar_failed",
                    build_sha=_scan_build_sha(scan_id),
                ),
            }
        )
    _record_vault_sidecar_status(scan_id, detail)
    return True


def _enter_vault_authority_boundary(scan_id: str) -> bool:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None or status.get("state") in {"cancelled", "error", "done"}:
            return False
        _VAULT_ACTIVATIONS.add(scan_id)
        return True


def _accepted_authority_source_report(application_result: Mapping[str, Any], scan_id: str, exact_owner: _ScanOwner | None = None, *, domain_or_url: str | None = None) -> dict[str, Any] | None:
    authority = application_result.get("authority")
    candidate = authority.get("accepted_candidate") if isinstance(authority, Mapping) else None
    source_scan_id = candidate.get("source_scan_id") if isinstance(candidate, Mapping) else None
    if not isinstance(source_scan_id, str) or not source_scan_id:
        if exact_owner is not None and isinstance(candidate, Mapping):
            raise _ExactResumeFailure("report_invalid")
        return None
    if source_scan_id == scan_id:
        return None
    source = _load_report_for_exact_owner(scan_id, exact_owner.token, source_scan_id) if exact_owner else load_report(source_scan_id)
    # An immutable no-score original can have a completed resume successor.
    # Search only this brand and accept only a persisted, exactly matching
    # authority projection; never substitute a merely newer report or score.
    if domain_or_url and (source is None or isinstance(source.get("sv9_assessment"), Mapping) and source["sv9_assessment"].get("availability") == "unavailable"):
        from src.services.evidence_vault_sv9_authority_report import project_vault_authority_publication

        candidates = list_reports_for_domain(domain_or_url)
        for candidate_report in sorted(candidates, key=lambda row: str(row.get("id") or "")):
            candidate_id = candidate_report.get("id")
            if not isinstance(candidate_id, str) or not candidate_id.startswith("exact-resume-"): continue
            if source is not None and (candidate_report.get("raw") or {}).get("source_capture") != (source.get("raw") or {}).get("source_capture"): continue
            if project_vault_authority_publication(application_result, scan_id, candidate_report)["action"] != "retain_source": continue
            loaded = _load_report_for_exact_owner(scan_id, exact_owner.token, candidate_id) if exact_owner else load_report(candidate_id)
            if loaded != candidate_report: raise _ExactResumeFailure("report_invalid") if exact_owner else RuntimeError("vault_authority_source_report_changed")
            source = loaded
            break
    if exact_owner and source is None: raise _ExactResumeFailure("report_invalid")
    return source


def _authority_scanner_payload(
    *,
    publication: Mapping[str, Any],
    canonical_snapshot: Mapping[str, Any],
    canonical_source_capture: Mapping[str, str] | None,
    gate: Mapping[str, Any],
    exact_report_binding: Mapping[str, Any] | None = None,
    report_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = copy.deepcopy(publication.get("scanner_payload"))
    if not isinstance(payload, dict):
        raise RuntimeError("vault_authority_payload_invalid")
    source_capture = canonical_source_capture or canonical_snapshot.get("source_capture")
    if isinstance(source_capture, Mapping):
        payload["source_capture"] = dict(source_capture)
    if report_observation is not None:
        from src.history.capture_observation import parse_capture_observation

        capture = parse_capture_observation(dict(report_observation))
        # Preserve the frozen evidence, not a reconstruction from raw inputs.
        payload["flow"] = {
            "candidate": {
                "evidence_pack": {
                    "schema_version": "brand-evidence-pack-v1",
                    "brand_name": capture.brand_name,
                    "url": capture.canonical_url,
                    "evidence": [dict(row) for row in capture.evidence_records],
                    "limitations": list(capture.limitations),
                }
            }
        }
    payload["acquisition_gate"] = dict(canonical_snapshot.get("acquisition_gate") or gate)
    payload["acquisition_artifacts"] = _acquisition_artifacts_from_snapshot(
        dict(canonical_snapshot)
    )
    return payload


def _run_vault_sv9_authority_scanner(
    *,
    scan_id: str,
    url: str,
    brand_name: str,
    repository: Any,
    preparation: Mapping[str, Any],
    canonical_snapshot: Mapping[str, Any],
    canonical_source_capture: Mapping[str, str] | None,
    gate: Mapping[str, Any],
    exact_owner: _ScanOwner | None = None,
    exact_report_binding: Mapping[str, Any] | None = None,
    exact_action: Mapping[str, Any] | None = None,
    exact_initial_source: Mapping[str, Any] | None = None,
) -> bool | _ExactResumePublication:
    """Run the authoritative path without evaluating the legacy Flow/SV9 lane."""

    exact = exact_owner is not None
    if exact and not _current_scan_owner(exact_owner):
        raise _ExactResumeFailure("busy")
    if not exact and _scan_cancelled(scan_id):
        return False
    authority_boundary_entered = False
    release_guard = True
    active_operation = "vault_authority_boundary"
    active_stage = "vault_authority"
    active_started: float | None = None
    capture_facts = _safe_capture_facts(
        canonical_source_capture or canonical_snapshot.get("source_capture")
    )
    operation_coverage = {"capture": capture_facts} if capture_facts else None
    try:
        preparation_started = time.monotonic()
        active_operation, active_stage, active_started = "vault_preparation_execution", "vault_preparation", preparation_started
        if not exact:
            _record_diagnostic_operation(scan_id, operation=active_operation, stage="vault_preparation", outcome="started", coverage=operation_coverage)
        fingerprint = _execute_vault_operational_preparation(
            scan_id=scan_id,
            repository=repository,
            preparation=preparation,
        )
        if not exact:
            _record_diagnostic_operation(scan_id, operation="vault_preparation_execution", stage="vault_preparation", outcome="completed", coverage=operation_coverage, started_monotonic=preparation_started)
        if exact and not _current_scan_owner(exact_owner): raise _ExactResumeFailure("busy")
        if not exact and _scan_cancelled(scan_id):
            return False
        if exact:
            activation = repository.activate_evidence_vault_operational_scanner_result(url, source_scan_id=scan_id, operation_plan_fingerprint=fingerprint, workspace_slug="b3s")
            operation = repository.get_capture_operation_plan(scan_id, workspace_slug="b3s")
            if not isinstance(activation, Mapping) or not isinstance(operation, Mapping): raise _ExactResumeFailure("operation_invalid")
            if operation.get("status") == "superseded": raise _ExactResumeFailure("superseded")
            if operation.get("status") != "completed": raise _ExactResumeFailure("operation_invalid")
        else:
            authority_boundary_entered = _enter_vault_authority_boundary(scan_id)
            if not authority_boundary_entered:
                return False
            if _activate_vault_result_unless_cancelled(
                scan_id, repository, url, operation_plan_fingerprint=fingerprint
            ) is None:
                return False
        from src.config import SV9_FLOW_MODEL
        from src.services.evidence_vault_sv9_authority_application import (
            run_evidence_vault_sv9_authority_application,
        )
        from src.services.evidence_vault_sv9_authority_report import (
            project_vault_authority_publication,
        )
        from src.services.evidence_vault_sv9_authority_evaluation import (
            observe_evidence_vault_sv9_authority_evaluation_diagnostics,
        )
        from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapter
        from src.sv9.judgment_memory import build_judgment_series_contract
        from src.sv9.shadow_component_provider import (
            FlowSv9ShadowJsonProvider,
            shadow_provider_environment_snapshot,
        )

        model = os.environ.get("BRAND3_FLOW_INTERPRETATION_MODEL") or SV9_FLOW_MODEL
        application_started = time.monotonic()
        active_operation, active_stage, active_started = "vault_authority_application", "vault_authority", application_started
        if not exact:
            _record_diagnostic_operation(scan_id, operation=active_operation, stage="vault_authority", outcome="started", coverage=operation_coverage)

        def observe_authority(event: dict[str, Any], exc: BaseException | None) -> None:
            if not exact:
                authority_result = _safe_authority_result(event)
                observed_outcome = (
                    "failed" if exc is not None else
                    "observed" if authority_result is not None else "completed"
                )
                _record_diagnostic_operation(
                    scan_id, operation="vault_authority_evaluation",
                    stage="vault_authority", outcome=observed_outcome, exc=exc,
                    coverage=event, started_monotonic=application_started,
                )

        with observe_evidence_vault_sv9_authority_evaluation_diagnostics(observe_authority):
            application_result = run_evidence_vault_sv9_authority_application(
                repository=repository,
                flow=FlowSv9StrictComponentAdapter(
                    FlowSv9ShadowJsonProvider(),
                    environ=shadow_provider_environment_snapshot(os.environ),
                    model=model,
                ),
                domain_or_url=url,
                source_scan_id=scan_id,
                current_series_contract=build_judgment_series_contract(
                    evaluator_version="evidence-vault-sv9-judgment-authority-v1",
                    prompt_version="sv9-strict-component-v1",
                    model_version=model,
                    flow_version="sv9-flow-strict-component-v1",
                    normalization_version="vault-capture-v1",
                ),
                workspace_slug="b3s",
                trusted_irrelevant_evidence=[],
            )
        if not exact:
            _record_diagnostic_operation(scan_id, operation="vault_authority_application", stage="vault_authority", outcome="completed", coverage=operation_coverage, started_monotonic=application_started)
        source_report = _accepted_authority_source_report(application_result, scan_id, exact_owner, domain_or_url=url)
        projection_started = time.monotonic()
        active_operation, active_stage, active_started = "vault_authority_projection", "vault_authority", projection_started
        if not exact:
            _record_diagnostic_operation(scan_id, operation=active_operation, stage="vault_authority", outcome="started", coverage=operation_coverage)
        publication = project_vault_authority_publication(
            application_result,
            scan_id,
            source_report,
        )
        if not exact:
            _record_diagnostic_operation(scan_id, operation="vault_authority_projection", stage="vault_authority", outcome="completed", coverage=operation_coverage, started_monotonic=projection_started)
        action = publication.get("action") if isinstance(publication, Mapping) else None
        if exact and source_report is not None and action != "retain_source": raise _ExactResumeFailure("report_invalid")
        if not exact:
            # Report availability is not proof that this scan produced a score.
            reasons = application_result.get("reason_codes")
            reused = (
                action == "retain_source"
                and application_result.get("status") == "authority_retained"
                and application_result.get("evaluation_status") == "no_new_score"
                and reasons == ["exact_reuse"]
            )
            # Only actual review producers qualify; review_required also wraps
            # invalid inputs. Unknown or mixed failure reasons remain errors.
            review = (
                application_result.get("status") in {"first_run_unresolved", "review_required"}
                and application_result.get("evaluation_status") == "review_required"
                and isinstance(reasons, list) and bool(reasons)
                and all(isinstance(reason, str) and reason in {
                    "review_set", "coverage_loss", "unmapped_evidence",
                    "series_rollover", "active_review_overlay", "incomplete_review_partition",
                } for reason in reasons)
            )
            scored = action == "publish_current" or reused
            _set_phase(scan_id, "interpret", "done" if scored else "blocked" if review else "error")
            _set_phase(scan_id, "score", "done" if scored else "blocked")
            _set_phase(scan_id, "report", "running")
        if action == "retain_source":
            source_report_id = publication.get("source_report_id")
            if not isinstance(source_report_id, str) or not source_report_id:
                raise _ExactResumeFailure("report_invalid") if exact else RuntimeError("vault_authority_source_report_unavailable")
            if exact:
                if not isinstance(source_report, Mapping) or source_report.get("id") != source_report_id: raise _ExactResumeFailure("report_invalid")
                return _ExactResumePublication("retain_source", source_report_id)
            return _finish_scan_without_new_score(scan_id, source_report_id)
        if action not in {"publish_current", "record_no_score"}:
            raise RuntimeError("vault_authority_publication_invalid")
        report = _compose_report(
            scan_id,
            url,
            brand_name,
            _authority_scanner_payload(
                publication=publication,
                canonical_snapshot=canonical_snapshot,
                canonical_source_capture=canonical_source_capture,
                gate=gate,
                exact_report_binding=exact_report_binding,
                report_observation=preparation.get("report_observation"),
            ),
        )
        assessment = _validate_report_sv9_assessment(report, required=True)
        if exact: return _publish_exact_report(scan_id, exact_owner, report, exact_report_binding or {}, str(action), action_identity=exact_action, initial_source=exact_initial_source)
        return _publish_completed_report(scan_id, report, assessment=assessment)
    except _ExactResumeFailure:
        raise
    except Exception as exc:
        if exact:
            raise
        _record_diagnostic_operation(
            scan_id, operation=active_operation, stage=active_stage,
            outcome="failed", exc=exc, coverage=operation_coverage, started_monotonic=active_started,
        )
        # The outer runner marks the error terminal and releases this guard
        # under the same lock; releasing here opens a cancellation window.
        release_guard = not authority_boundary_entered
        raise RuntimeError("vault_authority_preparation_failed") from None
    finally:
        if not exact and release_guard:
            with _LOCK:
                _VAULT_ACTIVATIONS.discard(scan_id)


def _run(scan_id: str, url: str, brand_name: str, allow_degraded_fallback: bool, owner: _ScanOwner | None = None) -> None:
    execution_started = time.monotonic()
    _record_diagnostic_operation(scan_id, operation="scan_execution", stage="capture", outcome="started", started_monotonic=execution_started)
    try:
        vault_repository = None
        vault_preparation: dict[str, Any] | None = None
        vault_enabled = False
        _set_phase(scan_id, "capture", "running")
        from src.config import (
            BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED,
            BRAND3_VAULT_VERIFIED_RAW_ALLOW_OWNED_ONLY_ANALYSIS,
        )

        if BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED:
            snapshot = _capture_verified_raw_shadow(
                scan_id=scan_id,
                url=url,
                brand_name=brand_name,
            )
        else:
            # Normal Vault memory uses the unchanged B3S acquisition contract:
            # owned web plus independent Exa acquisition.
            snapshot = _capture_snapshot(scan_id, url, brand_name)
        if _scan_cancelled(scan_id):
            return
        _set_phase(scan_id, "capture", "done")
        gate = _build_acquisition_gate(
            snapshot.get("acquisition_steps") if isinstance(snapshot, dict) else {},
            allow_degraded_fallback=allow_degraded_fallback,
            allow_owned_only_analysis=(
                BRAND3_VAULT_VERIFIED_RAW_ALLOW_OWNED_ONLY_ANALYSIS
            ),
        )
        if gate["state"] == "blocked" and allow_degraded_fallback and gate.get("can_continue"):
            gate = _approve_acquisition_gate(gate, decision_source="preapproved")
        snapshot["acquisition_gate"] = gate
        _set_acquisition_gate(scan_id, gate)
        if gate["state"] == "blocked":
            if not _wait_for_acquisition_decision(scan_id):
                return
            with _LOCK:
                status = _SCANS.get(scan_id) or {}
                current_gate = status.get("acquisition_gate") if isinstance(status.get("acquisition_gate"), dict) else gate
                if status.get("state") == "cancelled":
                    return
            snapshot["acquisition_gate"] = current_gate

        if _scan_cancelled(scan_id):
            return

        vault_enabled = _vault_operational_pipeline_enabled()
        authority_scanner_enabled = _vault_sv9_authority_scanner_enabled()
        canonical_snapshot = snapshot
        canonical_source_capture: dict[str, str] | None = None
        if vault_enabled:
            _set_execution_stage(scan_id, "vault_preparation")
            from src.services.evidence_vault_scan_orchestration import (
                public_vault_authority_reason_code,
                prepare_vault_scan_after_capture,
            )
            from web.report_store import _postgres_repository

            vault_repository = _postgres_repository()
            if vault_repository is None:
                raise RuntimeError(
                    "vault_authority_preparation_unavailable"
                    if authority_scanner_enabled
                    else "vault_persistence_repository_unavailable"
                )
            preparation_started: float | None = None
            try:
                preparation_started = time.monotonic()
                _record_diagnostic_operation(
                    scan_id, operation="vault_capture_preparation",
                    stage="vault_preparation", outcome="started",
                )
                vault_preparation = prepare_vault_scan_after_capture(
                    repository=vault_repository,
                    snapshot=snapshot,
                    scan_id=scan_id,
                    url=url,
                    brand_name=brand_name,
                    environment="vault",
                    incremental_enabled=True,
                    workspace_slug="b3s",
                    artifacts=_acquisition_artifacts_from_snapshot(snapshot),
                )
                if vault_preparation.get("capture_persisted") is not True:
                    raise RuntimeError("vault_persisted_capture_unavailable")
                persisted_observation = _read_back_persisted_vault_capture(
                    repository=vault_repository,
                    preparation=vault_preparation,
                    scan_id=scan_id,
                    url=url,
                )
            except Exception as exc:
                if authority_scanner_enabled:
                    raise RuntimeError(
                        public_vault_authority_reason_code(exc)
                        or "vault_authority_preparation_unavailable"
                    ) from None
                # The operation planner is a sidecar.  If its work failed only
                # after the capture committed, an exact repository readback is
                # still enough to publish the canonical Flow/SV9 report.
                try:
                    persisted_observation = _read_back_persisted_vault_capture(
                        repository=vault_repository,
                        preparation={},
                        scan_id=scan_id,
                        url=url,
                    )
                    canonical_snapshot, canonical_source_capture = (
                        _canonical_snapshot_from_persisted_vault_capture(
                            scan_id=scan_id,
                            url=url,
                            expected_snapshot=snapshot,
                            report_observation=persisted_observation,
                        )
                    )
                except Exception as readback_exc:
                    raise RuntimeError(
                        "vault_persisted_capture_unavailable"
                    ) from readback_exc
                with _LOCK:
                    status_for_diagnostic = _status_copy_locked(_SCANS.get(scan_id) or {})
                _record_vault_sidecar_status(
                    scan_id,
                    {
                        "role": "diagnostic_sidecar",
                        "state": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "diagnostic": _diagnostic(
                            kind="secondary_failure",
                            stage="vault_sidecar",
                            capture_state=_capture_state(status_for_diagnostic),
                            reason_codes="vault_sidecar_failed",
                            build_sha=status_for_diagnostic.get("scan_build_sha"),
                        ),
                    },
                )
                vault_preparation = None
            else:
                try:
                    canonical_snapshot, canonical_source_capture = (
                        _canonical_snapshot_from_persisted_vault_capture(
                            scan_id=scan_id,
                            url=url,
                            expected_snapshot=snapshot,
                            report_observation=persisted_observation,
                        )
                    )
                except Exception:
                    if authority_scanner_enabled:
                        raise RuntimeError("vault_authority_preparation_unavailable") from None
                    raise

        _set_phase(scan_id, "interpret", "running")
        if authority_scanner_enabled:
            if vault_repository is None or not isinstance(vault_preparation, Mapping):
                raise RuntimeError("vault_authority_preparation_unavailable")
            _set_execution_stage(scan_id, "vault_authority")
            _run_vault_sv9_authority_scanner(
                scan_id=scan_id,
                url=url,
                brand_name=brand_name,
                repository=vault_repository,
                preparation=vault_preparation,
                canonical_snapshot=canonical_snapshot,
                canonical_source_capture=canonical_source_capture,
                gate=gate,
            )
            return
        from scripts.sv9_flow_sv9_shadow_eval import build_flow_sv9_shadow_eval

        canonical_run = (
            canonical_snapshot.get("run")
            if isinstance(canonical_snapshot, dict)
            else None
        )
        if not isinstance(canonical_run, Mapping) or canonical_run.get("id") is None:
            raise RuntimeError("canonical_capture_run_identity_unavailable")
        envelope = {
            "snapshot": canonical_snapshot,
            "source_run_id": canonical_run["id"],
        }
        payload = build_flow_sv9_shadow_eval(envelope, include_full=True)
        payload = _attach_sv9_editorial(payload)
        if BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED:
            flow_payload = payload.get("flow") if isinstance(payload.get("flow"), dict) else {}
            candidate_payload = (
                flow_payload.get("candidate")
                if isinstance(flow_payload.get("candidate"), dict)
                else None
            )
            if candidate_payload is not None:
                # Labeling enriches the in-memory evidence pack for analysis.
                # The report must persist the exact worker evidence rows, so
                # strip those advisory annotations at this trust boundary.
                from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot

                candidate_payload["evidence_pack"] = (
                    build_evidence_pack_from_snapshot(
                        canonical_snapshot,
                        # Raw capture persistence owns the exact evidence set;
                        # acquisition warnings are report metadata, not extra
                        # evidence rows that could break the capture binding.
                        include_acquisition_steps=False,
                    ).to_dict()
                )
        if canonical_source_capture is not None:
            payload["source_capture"] = dict(canonical_source_capture)
        else:
            source_capture = snapshot.get("source_capture")
            if isinstance(source_capture, dict):
                payload["source_capture"] = dict(source_capture)
        payload["acquisition_gate"] = canonical_snapshot.get("acquisition_gate") or gate
        payload["acquisition_artifacts"] = _acquisition_artifacts_from_snapshot(
            canonical_snapshot
        )
        if _scan_cancelled(scan_id):
            return
        _set_phase(scan_id, "interpret", "done")
        _set_phase(scan_id, "score", "done")
        report = _compose_report(scan_id, url, brand_name, payload)
        report = _attach_evidence_stability(report)
        assessment = _validate_report_sv9_assessment(
            report,
            required=vault_enabled,
        )
        if vault_enabled:
            if vault_repository is None:
                raise RuntimeError("vault_persisted_capture_unavailable")
            if vault_preparation is not None:
                if not _run_vault_operational_sidecar(
                    scan_id=scan_id,
                    url=url,
                    repository=vault_repository,
                    preparation=vault_preparation,
                ):
                    return
            assessment = _validate_report_sv9_assessment(report, required=True)
        if _scan_cancelled(scan_id):
            return
        _set_phase(scan_id, "report", "running")
        published = _publish_completed_report(scan_id, report, assessment=assessment)
        if not published:
            return
        _run_vault_sv9_judgment_shadow_after_publication(
            scan_id=scan_id, repository=vault_repository, payload=payload, report=report
        )
    except Exception as exc:  # surface the failure to the UI, never die silently
        traceback.print_exc()
        persisted_status = None
        with _LOCK:
            status = _SCANS.get(scan_id)
            if status and status.get("state") != "cancelled":
                status["state"] = "error"
                status["phase"] = "error"
                status["error"] = f"{type(exc).__name__}: {exc}"
                status["error_code"] = "scan_execution_failed"
                status["diagnostic"] = _diagnostic(
                    kind="execution_failed",
                    stage=status.get("execution_stage"),
                    capture_state=_capture_state(status),
                    reason_codes=(
                        str(exc)
                        if isinstance(exc, RuntimeError)
                        and str(exc) in _DIAGNOSTIC_REASON_CODES
                        else "scan_execution_failed"
                    ),
                    build_sha=status.get("scan_build_sha"),
                    origin=_safe_exception_origin(exc),
                )
                status["completed_at"] = datetime.now(timezone.utc).isoformat()
                _mark_pending_phases_locked(status, "error")
                persisted_status = _status_copy_locked(status)
            _SCAN_EVENTS.pop(scan_id, None)
            _VAULT_ACTIVATIONS.discard(scan_id)
        if persisted_status is not None:
            try:
                _persist_scan_status(persisted_status)
            except Exception:
                _LOG.exception("failed to persist terminal scanner error", extra={"scan_id": scan_id})
        _record_diagnostic_operation(scan_id, operation="scan_execution", stage="unknown", outcome="failed", exc=exc, started_monotonic=execution_started)
    finally:
        if owner is not None:
            _release_scan_owner(owner)


def _capture_verified_raw_shadow(
    *,
    scan_id: str,
    url: str,
    brand_name: str,
) -> dict[str, Any]:
    """Persist and project the exact verified documents before interpretation."""

    from src.config import (
        BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED,
        BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH,
    )

    if not BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED:
        raise RuntimeError("verified_raw_acquisition_disabled")
    if os.environ.get("BRAND3_ENVIRONMENT", "").strip().lower() != "vault":
        raise RuntimeError("verified_raw_acquisition_invalid_environment")
    if not BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH:
        raise RuntimeError("verified_raw_acquisition_unconfigured")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    canonical_url = f"https://{host}"
    try:
        from src.services.evidence_vault_acquisition_contract import (
            TrustedAcquisitionCommand,
        )
        from src.services.evidence_vault_acquisition_ipc import (
            UnixTrustedAcquisitionClient,
        )

        result = UnixTrustedAcquisitionClient(
            BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH,
        ).capture(
            TrustedAcquisitionCommand(
                workspace_slug="b3s",
                source_scan_id=scan_id,
                brand_url=canonical_url,
            )
        )
        documents = list(result.documents)
        valid_result = bool(
            documents
            and any(document.role == "owned_web" for document in documents)
        )
    except Exception:
        result = None
        documents = []
        valid_result = False
    if result is None or not valid_result:
        raise RuntimeError("verified_raw_acquisition_failed")

    safe_status = {
        "state": "persisted_shadow",
        "capture_id": result.capture_id,
        "capture_content_hash": result.capture_content_hash,
        "capture_observation_hash": result.capture_observation_hash,
        "receipt_set_fingerprint": result.receipt_set_fingerprint,
        "receipt_count": len(result.receipt_rows),
    }
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None or status.get("state") == "cancelled":
            raise RuntimeError("verified_raw_acquisition_cancelled")
        status["verified_raw_acquisition"] = safe_status
        persisted_status = _status_copy_locked(status)
    _persist_scan_status(persisted_status)
    return _verified_raw_pre_analysis_snapshot(
        scan_id=scan_id,
        brand_name=brand_name,
        canonical_url=canonical_url,
        capture_content_hash=result.capture_content_hash,
        capture_observation_hash=result.capture_observation_hash,
        documents=documents,
    )


def _verified_raw_pre_analysis_snapshot(
    *,
    scan_id: str,
    brand_name: str,
    canonical_url: str,
    capture_content_hash: str,
    capture_observation_hash: str,
    documents: list[Any],
) -> dict[str, Any]:
    source_by_role = {
        "owned_web": "web",
        "external_social_profile": "exa",
    }
    raw_inputs: list[dict[str, Any]] = []
    acquisition_steps: dict[str, Any] = {}
    acquisition_rows: list[dict[str, str]] = []
    for document in documents:
        source = source_by_role.get(document.role)
        if source is None:
            raise RuntimeError("verified_raw_acquisition_failed")
        raw_inputs.append(
            {
                "source": "verified_raw_document",
                "payload": {
                    "role": document.role,
                    "url": document.source_url,
                    "content": document.extracted_document,
                    "extracted_document_sha256": (
                        document.extracted_document_sha256
                    ),
                    "extractor_version": str(
                        getattr(
                            document,
                            "extractor_version",
                            "evidence-vault-deterministic-extractor-v1",
                        )
                    ),
                    "receipt_fingerprint": document.receipt_fingerprint,
                },
            }
        )
        acquisition_steps[source] = {
            "source": source,
            "status": "success",
            "details": {
                "reason": "verified_raw_document_persisted",
                "verified_raw": True,
            },
        }
        acquisition_rows.append(
            {
                "source": source,
                "status": "success",
                "detail": "verified_raw_document_persisted",
            }
        )
    if "exa" not in acquisition_steps:
        acquisition_steps["exa"] = {
            "source": "exa",
            "status": "error",
            "error": "verified_external_document_unavailable",
            "details": {
                "reason": "verified_external_document_unavailable",
                "verified_raw": True,
            },
        }
        acquisition_rows.append(
            {
                "source": "exa",
                "status": "error",
                "detail": "verified_external_document_unavailable",
            }
        )
    for source in ("searchapi", "github", "context", "visual_acquisition"):
        acquisition_steps[source] = {
            "source": source,
            "status": "disabled",
            "details": {
                "reason": "not_executed_in_verified_raw_mode",
                "verified_raw": True,
            },
        }
        acquisition_rows.append(
            {
                "source": source,
                "status": "disabled",
                "detail": "not_executed_in_verified_raw_mode",
            }
        )
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is not None:
            status["acquisition"] = sorted(
                acquisition_rows,
                key=lambda row: row["source"],
            )
            persisted_status = _status_copy_locked(status)
        else:
            persisted_status = None
    if persisted_status is not None:
        _persist_scan_status(persisted_status)
    return {
        "run": {
            "brand_name": brand_name,
            "id": _stable_verified_source_run_id(scan_id),
            "url": canonical_url,
        },
        # Hash-only binding metadata lets the later report prove it evaluates
        # the exact worker-persisted capture without exposing raw payloads to
        # FastAPI or reconstructing the worker's private observation.
        "source_capture": {
            "source_scan_id": scan_id,
            "observation_hash": capture_observation_hash,
            "capture_hash": capture_content_hash,
        },
        "raw_inputs": raw_inputs,
        "acquisition_steps": acquisition_steps,
        "features": [],
    }


def _stable_verified_source_run_id(scan_id: str) -> int:
    value = int.from_bytes(
        hashlib.sha256(scan_id.encode("utf-8")).digest()[:8],
        "big",
    ) & ((1 << 63) - 1)
    return value or 1


def _attach_evidence_stability(report: dict[str, Any]) -> dict[str, Any]:
    """Classify a candidate without allowing comparison failures to erase it."""

    try:
        prior_reports = list_reports_for_domain(str(report.get("url") or ""))
        return annotate_candidate_report(report, prior_reports)
    except Exception as exc:
        _LOG.exception(
            "failed to classify report evidence stability",
            extra={"scan_id": str(report.get("id") or "")},
        )
        fallback = dict(report)
        fallback["canonical_status"] = "non_canonical"
        fallback["stability"] = {
            "schema_version": EVIDENCE_COMPARISON_VERSION,
            "classification": "comparison_error",
            "canonical_status": "non_canonical",
            "reason_codes": ["evidence_comparison_failed"],
            "error_type": type(exc).__name__,
        }
        fallback["canonical_selection"] = {
            "enforcement_mode": canonical_enforcement_mode(),
            "selected_report_id": None,
            "canonical_report_id": None,
            "provisional_report_id": None,
        }
        return fallback


def _set_acquisition_gate(scan_id: str, gate: dict[str, Any]) -> None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if not status:
            return
        status["acquisition_gate"] = gate
        if gate.get("state") == "blocked":
            status["state"] = "blocked"
            status["phase"] = "capture"
        persisted_status = _status_copy_locked(status)
    _persist_scan_status(persisted_status)


def _wait_for_acquisition_decision(scan_id: str) -> bool:
    with _LOCK:
        event = _SCAN_EVENTS.get(scan_id)
    if event is None:
        return False
    event.wait()
    with _LOCK:
        status = _SCANS.get(scan_id)
        return bool(status and status.get("state") != "cancelled")


def _build_acquisition_gate(
    acquisition_steps: Any,
    *,
    allow_degraded_fallback: bool = False,
    allow_owned_only_analysis: bool = False,
) -> dict[str, Any]:
    steps = acquisition_steps if isinstance(acquisition_steps, dict) else {}
    normalized = {str(source): _step_payload(step) for source, step in steps.items()}
    _ensure_configured_step_markers(normalized)

    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []

    web_step = normalized.get("web")
    if web_step is None or _is_failure_status(_step_status(web_step)):
        issues.append(
            _issue(
                source="web",
                code="web_capture_failed",
                severity="blocker",
                message="Owned web capture failed; scoring would lack the primary evidence base.",
                step=web_step,
            )
        )
    else:
        web_details = web_step.get("details") if isinstance(web_step.get("details"), dict) else {}
        if web_details.get("cookie_banner_suspected") is True:
            warnings.append(
                _issue(
                    source="web",
                    code="web_cookie_banner_suspected",
                    severity="warning",
                    message="Owned web capture may contain unresolved cookie-banner text.",
                    step=web_step,
                )
            )

    exa_step = normalized.get("exa")
    exa_status = _step_status(exa_step)
    exa_failed = exa_step is None or _is_failure_status(exa_status)
    searchapi_step = normalized.get("searchapi")
    searchapi_status = _step_status(searchapi_step)
    searchapi_available = _searchapi_available_for_fallback(searchapi_step)
    if exa_failed:
        fallback = {
            "source": "searchapi",
            "for_source": "exa",
            "available": searchapi_available,
            "approved": False,
            "status": searchapi_status or "missing",
            "reason": "vertical external-proof fallback for Exa failure",
        }
        fallbacks.append(fallback)
        exa_issue = _issue(
            source="exa",
            code="exa_failed",
            severity="warning" if allow_owned_only_analysis else "blocker",
            message=(
                "Exa failed; continuing with owned-web analysis only. "
                "C7 remains sin_evidencia until its second channel is captured and reviewed."
                if allow_owned_only_analysis
                else "Exa failed; external proof acquisition is incomplete."
            ),
            step=exa_step,
            fallback="searchapi" if searchapi_available else "",
            can_fallback=searchapi_available,
        )
        (warnings if allow_owned_only_analysis else issues).append(exa_issue)
    elif exa_status in {"empty", "partial"}:
        warnings.append(
            _issue(
                source="exa",
                code=f"exa_{exa_status}",
                severity="warning",
                message="Exa returned limited external proof; score should declare reduced acquisition coverage.",
                step=exa_step,
            )
        )

    diversity = _vault_external_diversity_status(normalized)
    if diversity and diversity["observed"] < diversity["minimum"]:
        diversity_step = searchapi_step or exa_step
        warning = _issue(
            source="external_sources",
            code="external_domain_diversity_low",
            severity="warning",
            message=(
                "External acquisition found too few distinct candidate domains; "
                "corroboration coverage may be incomplete."
            ),
            step=diversity_step,
        )
        warning.update(
            {
                "detail": (
                    f"distinct_candidate_domains: {diversity['observed']}; "
                    f"minimum: {diversity['minimum']}"
                ),
                "observed_external_domain_count": diversity["observed"],
                "minimum_external_domain_count": diversity["minimum"],
                "metric": "distinct_candidate_domains",
            }
        )
        warnings.append(warning)

    if _is_failure_status(searchapi_status):
        target = issues if exa_failed else warnings
        target.append(
            _issue(
                source="searchapi",
                code="searchapi_failed",
                severity="blocker" if exa_failed else "warning",
                message="SearchAPI fallback failed.",
                step=searchapi_step,
            )
        )

    github_status = _step_status(normalized.get("github"))
    if _is_failure_status(github_status):
        warnings.append(
            _issue(
                source="github",
                code="github_proof_failed",
                severity="warning",
                message="GitHub proof acquisition failed; continuing without repository proof.",
                step=normalized.get("github"),
            )
        )

    visual_step = normalized.get("visual_acquisition")
    visual_status = _step_status(visual_step)
    visual_evidence_status = _visual_evidence_status(visual_step)
    visual_first_fold_evaluable = _visual_first_fold_evaluable(visual_step)
    visual_state_missing = visual_status == "completed" and not visual_evidence_status
    if (
        _is_failure_status(visual_status)
        or visual_evidence_status in {"blocked", "limited", "missing", "not_interpretable", "unavailable"}
        or visual_first_fold_evaluable is False
        or visual_state_missing
    ):
        warnings.append(
            _issue(
                source="visual_acquisition",
                code="visual_acquisition_limited",
                severity="warning",
                message="Visual acquisition was unavailable or obstructed; visual evidence remains limited.",
                step=visual_step,
            )
        )

    blocking = [item for item in issues if item.get("severity") == "blocker"]
    can_continue = not blocking or all(bool(item.get("can_fallback")) for item in blocking)
    state = "blocked" if blocking else ("warning" if warnings else "pass")
    return {
        "version": "b3s-acquisition-gate-v2",
        "state": state,
        "can_continue": can_continue,
        "allow_degraded_fallback": bool(allow_degraded_fallback),
        "issues": issues,
        "warnings": warnings,
        "fallbacks": fallbacks,
        "limitations": _gate_limitations(issues, warnings),
        "user_decision": None,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def _approve_acquisition_gate(gate: dict[str, Any], *, decision_source: str) -> dict[str, Any]:
    approved = dict(gate)
    approved["state"] = "degraded_approved"
    approved["user_decision"] = "continue_degraded"
    approved["decision_source"] = decision_source
    approved["decided_at"] = datetime.now(timezone.utc).isoformat()
    approved["fallbacks"] = [
        {**fallback, "approved": bool(fallback.get("available"))}
        for fallback in gate.get("fallbacks") or []
        if isinstance(fallback, dict)
    ]
    return approved


def _step_payload(step: Any) -> dict[str, Any]:
    if step is None:
        return {}
    if isinstance(step, dict):
        return step
    if hasattr(step, "to_payload"):
        return dict(step.to_payload())
    if hasattr(step, "to_dict"):
        return dict(step.to_dict())
    if dataclasses.is_dataclass(step):
        return dataclasses.asdict(step)
    return dict(vars(step))


def _ensure_configured_step_markers(steps: dict[str, dict[str, Any]]) -> None:
    from src.config import EXA_API_KEY, SEARCHAPI_API_KEY

    if not EXA_API_KEY and "exa" not in steps:
        steps["exa"] = {
            "source": "exa",
            "status": "missing_key",
            "cache_status": "missing",
            "eligible": True,
            "error": "EXA_API_KEY not set",
            "details": {"reason": "EXA_API_KEY not set"},
        }
    if not SEARCHAPI_API_KEY and "searchapi" not in steps:
        steps["searchapi"] = {
            "source": "searchapi",
            "status": "missing_key",
            "cache_status": "missing",
            "eligible": False,
            "error": "SEARCHAPI_API_KEY not set",
            "details": {"reason": "SEARCHAPI_API_KEY not set"},
        }


def _step_status(step: Any) -> str:
    if not isinstance(step, dict):
        return ""
    return str(step.get("status") or "").strip().lower()


def _visual_evidence_status(step: Any) -> str:
    if not isinstance(step, dict):
        return ""
    return str(step.get("evidence_status") or "").strip().lower()


def _visual_first_fold_evaluable(step: Any) -> bool | None:
    if not isinstance(step, dict):
        return None
    value = step.get("first_fold_evaluable")
    return value if isinstance(value, bool) else None


def _step_detail(step: Any) -> str:
    if not isinstance(step, dict):
        return ""
    details = step.get("details") if isinstance(step.get("details"), dict) else {}
    reason = str(details.get("reason") or step.get("error") or "").strip()
    parts = [reason] if reason else []
    for key in ("blocked_reason", "cookie_banner_snippet"):
        value = str(details.get(key) or "").strip()
        if value:
            parts.append(f"{key}: {value}")
    if parts:
        return "; ".join(parts)
    for key in ("failed_intents", "no_result_intents"):
        values = details.get(key)
        if isinstance(values, list) and values:
            return f"{key}: {', '.join(str(value) for value in values)}"
    return ""


def _is_failure_status(status: str) -> bool:
    return status in {
        "error",
        "failed",
        "failure",
        "missing",
        "missing_key",
        "blocked",
        "timeout",
        "acquisition_failed",
    }


def _searchapi_available_for_fallback(step: Any) -> bool:
    from src.config import SEARCHAPI_API_KEY

    if not SEARCHAPI_API_KEY:
        return False
    status = _step_status(step)
    if not status:
        return True
    return status not in {"disabled", "missing_key", "error", "failed", "failure", "blocked", "timeout"}


def _vault_external_diversity_status(steps: dict[str, dict[str, Any]]) -> dict[str, int] | None:
    if os.environ.get("BRAND3_ENVIRONMENT", "").strip().lower() != "vault":
        return None
    exa_details = _step_details(steps.get("exa"))
    searchapi_details = _step_details(steps.get("searchapi"))
    observed = searchapi_details.get("combined_external_domain_count")
    if not _is_plain_int(observed):
        observed = exa_details.get("external_domain_count")
    if not _is_plain_int(observed):
        return None
    minimum = searchapi_details.get("minimum_external_domain_count")
    if not _is_plain_int(minimum):
        minimum = exa_details.get("minimum_external_domain_count")
    if not _is_plain_int(minimum):
        from src.config import BRAND3_VAULT_MIN_EXTERNAL_SOURCE_DOMAINS

        minimum = BRAND3_VAULT_MIN_EXTERNAL_SOURCE_DOMAINS
    return {"observed": observed, "minimum": minimum}


def _step_details(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict) or not isinstance(step.get("details"), dict):
        return {}
    return step["details"]


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _issue(
    *,
    source: str,
    code: str,
    severity: str,
    message: str,
    step: dict[str, Any] | None,
    fallback: str = "",
    can_fallback: bool = False,
) -> dict[str, Any]:
    payload = {
        "source": source,
        "code": code,
        "severity": severity,
        "message": message,
        "status": _step_status(step) or "missing",
        "detail": _step_detail(step),
        "can_fallback": bool(can_fallback),
    }
    if isinstance(step, dict):
        for key in ("evidence_status", "screenshot_status", "first_fold_evaluable", "obstruction"):
            value = step.get(key)
            if value not in (None, "", {}):
                payload[key] = value
    if fallback:
        payload["fallback"] = fallback
    return payload


def _gate_limitations(issues: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> list[str]:
    codes = []
    for item in issues + warnings:
        code = str(item.get("code") or "").strip()
        if code and code not in codes:
            codes.append(f"acquisition_gate:{code}")
    return codes


def _capture_snapshot(scan_id: str, url: str, brand_name: str) -> dict[str, Any]:
    from src.config import BRAND3_VISUAL_SIGNATURE_SCAN_ENABLED
    from src.services import brand_service as service

    raw = service.collect_raw_inputs(
        store=None,
        run_id=None,
        brand_name=brand_name,
        url=url,
        refresh=False,
        use_social=False,
        use_competitors=False,
        effective_brand_url_builder=service._effective_brand_url,
        context_evidence_builder=service._context_evidence_items,
        run_input_sources={"context", "web", "exa", "github", "searchapi"},
        social_collector=service._collect_social_with_budget,
        context_collector_cls=service.ContextCollector,
        web_collector_cls=service.WebCollector,
        exa_collector_cls=service.ExaCollector,
    )

    raw_inputs: list[dict[str, Any]] = []
    for source, data in (
        ("context", raw.context_data),
        ("web", raw.web_data),
        ("exa", raw.exa_data),
        ("github", raw.github_data),
        ("searchapi", raw.searchapi_data),
    ):
        if data is None:
            continue
        if str(getattr(data, "status", "") or "") in {"skipped", "error"}:
            continue
        raw_inputs.append({"source": source, "payload": _to_payload(data)})

    visual_rows, visual_step = _capture_visual_evidence(
        service=service,
        enabled=BRAND3_VISUAL_SIGNATURE_SCAN_ENABLED,
        url=url,
        brand_name=brand_name,
        web_data=raw.web_data,
        content_web=raw.web_data,
    )
    raw_inputs.extend(visual_rows)

    acquisition_steps: dict[str, Any] = {}
    acquisition_rows: list[dict[str, str]] = []
    for source, step in (raw.acquisition_steps or {}).items():
        payload = _to_payload(step)
        if str(source) == "web":
            _annotate_web_cookie_banner_signal(raw.web_data, payload)
        acquisition_steps[source] = payload
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        acquisition_rows.append(
            {
                "source": str(source),
                "status": str(payload.get("status") or ""),
                "detail": str(details.get("reason") or payload.get("error") or ""),
            }
        )
    if visual_step:
        acquisition_steps["visual_acquisition"] = visual_step
        acquisition_rows.append(
            {
                "source": "visual_acquisition",
                "status": str(visual_step.get("status") or ""),
                "detail": str((visual_step.get("details") or {}).get("reason") or ""),
                "evidence_status": str(visual_step.get("evidence_status") or ""),
                "screenshot_status": str(visual_step.get("screenshot_status") or ""),
                "first_fold_evaluable": visual_step.get("first_fold_evaluable"),
            }
        )
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status:
            status["acquisition"] = sorted(acquisition_rows, key=lambda row: row["source"])
            persisted_status = _status_copy_locked(status)
        else:
            persisted_status = None
    if persisted_status is not None:
        _persist_scan_status(persisted_status)

    return {
        "run": {"brand_name": brand_name, "id": int(datetime.now(timezone.utc).timestamp()), "url": url},
        "raw_inputs": raw_inputs,
        "acquisition_steps": acquisition_steps,
        "features": [],
    }


def _annotate_web_cookie_banner_signal(web_data: Any | None, step_payload: dict[str, Any]) -> None:
    snippet = _cookie_banner_snippet_from_web_data(web_data)
    if not snippet:
        return
    details = step_payload.get("details") if isinstance(step_payload.get("details"), dict) else {}
    details["cookie_banner_suspected"] = True
    details["cookie_banner_snippet"] = snippet
    step_payload["details"] = details


def _cookie_banner_snippet_from_web_data(web_data: Any | None) -> str:
    if web_data is None:
        return ""
    try:
        payload = _to_payload(web_data)
    except Exception:
        return ""
    if str(payload.get("capture_obstruction") or "").strip().lower() == "cookie_banner":
        return "cookie_banner"
    markdown = str(
        payload.get("markdown")
        or payload.get("markdown_content")
        or payload.get("content")
        or payload.get("text")
        or ""
    )
    # WebData appends crawled subpages to the homepage markdown. Cookie/privacy
    # policy pages and harmless controls near the footer must not make an
    # otherwise usable homepage look obstructed.
    homepage_markdown = markdown.partition("\n\n---\n## Subpage:")[0]
    candidates = [
        str(payload.get("title") or ""),
        str(payload.get("body_text") or "")[:800],
        homepage_markdown[:800],
    ]
    text = " ".join(part for part in candidates if part).strip()
    if not text:
        return ""
    lowered = text.lower()
    exact_banner_markers = (
        "valoramos tu privacidad",
        "we value your privacy",
        "we use cookies",
        "usamos cookies",
        "utilizamos cookies",
    )
    consent_markers = ("cookie", "cookies", "consent", "privacidad", "privacy")
    action_markers = ("aceptar", "accept", "rechazar", "reject", "preferencias", "preferences", "consent")
    if any(marker in lowered for marker in exact_banner_markers) or (
        any(marker in lowered for marker in consent_markers) and any(marker in lowered for marker in action_markers)
    ):
        return " ".join(text.split())[:220]
    return ""


def _capture_visual_evidence(
    *,
    service: Any,
    enabled: bool,
    url: str,
    brand_name: str,
    web_data: Any | None,
    content_web: Any | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not enabled:
        return [], {
            "status": "skipped",
            "evidence_status": "skipped",
            "screenshot_status": "skipped",
            "first_fold_evaluable": None,
            "obstruction": {},
            "details": {"reason": "visual_acquisition_disabled"},
        }

    screenshot_capture = _capture_screenshot(service=service, url=url)
    try:
        result = service._run_visual_signature_shadow(
            enabled=True,
            store=None,
            run_id=None,
            brand_name=brand_name,
            url=url,
            web_data=web_data,
            content_web=content_web,
            screenshot_capture=screenshot_capture,
        )
    except Exception as exc:  # visual acquisition enriches the run; it must not own the run
        return [
            {
                "source": "screenshot_capture",
                "payload": {
                    "version": "screenshot_capture_v1",
                    "url": url,
                    "content_source": "b3s_live_scan",
                    "skip_visual_analysis": False,
                    "capture": screenshot_capture,
                },
            }
        ], {
            "status": "error",
            "evidence_status": "unavailable",
            "screenshot_status": str(screenshot_capture.get("status") or "unknown"),
            "first_fold_evaluable": None,
            "obstruction": {},
            "details": {"reason": str(exc)},
        }
    visual_evidence = result.get("visual_evidence_packet")
    rows: list[dict[str, Any]] = [
        {
            "source": "screenshot_capture",
            "payload": {
                "version": "screenshot_capture_v1",
                "url": url,
                "content_source": "b3s_live_scan",
                "skip_visual_analysis": False,
                "capture": screenshot_capture,
            },
        }
    ]
    if isinstance(visual_evidence, dict):
        rows.append(
            {
                "source": "visual_acquisition",
                "payload": {
                    "schema_version": "visual-signature-persistence-1",
                    "run_id": None,
                    "brand_name": brand_name,
                    "website_url": url,
                    "run_metadata": {
                        "source": "b3s_live_scan",
                        "visual_signature_scan_status": result.get("visual_signature_scan_status"),
                        "visual_signature_score": result.get("visual_signature_score"),
                        "interpretation_status": result.get("interpretation_status"),
                        "agreement_level": result.get("agreement_level"),
                    },
                    "visual_signature_scan": result.get("visual_signature_scan"),
                    "visual_evidence_packet": visual_evidence,
                    "visual_signature_evidence": result.get("visual_signature_evidence"),
                    "raw_visual_signature_payload": result.get("payload"),
                    "vision_payload": result.get("vision"),
                },
            }
        )

    status = str(result.get("status") or "unknown")
    visual_state = _structured_visual_state(
        result=result,
        screenshot_capture=screenshot_capture,
    )
    details = {
        "reason": _visual_acquisition_detail(result=result, screenshot_capture=screenshot_capture),
    }
    blocked_reason = _visual_obstruction_detail(result)
    if blocked_reason:
        details["blocked_reason"] = blocked_reason
    cookie_snippet = _visual_cookie_banner_snippet(result=result, screenshot_capture=screenshot_capture)
    if cookie_snippet:
        details["cookie_banner_suspected"] = True
        details["cookie_banner_snippet"] = cookie_snippet
    return rows, {
        "status": status,
        **visual_state,
        "details": details,
    }


def _capture_screenshot(*, service: Any, url: str) -> dict[str, Any]:
    try:
        screenshot_data, limitation = service._take_screenshot_with_budget(
            url,
            timeout_seconds=int(os.environ.get("BRAND3_VISUAL_SCREENSHOT_TIMEOUT_SECONDS", "60")),
        )
        return service._screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data=screenshot_data,
            limitation=limitation,
        )
    except Exception as exc:  # visual evidence must never block text scoring
        return service._screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data={"error": str(exc)},
            limitation="error",
        )


def _visual_acquisition_detail(*, result: dict[str, Any], screenshot_capture: dict[str, Any]) -> str:
    evidence = result.get("visual_evidence_packet")
    if isinstance(evidence, dict):
        capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
        capture_status = str(capture.get("status") or "").strip()
        if capture_status:
            return f"visual_evidence_packet:{capture_status}"
        return "visual_evidence_packet"
    if not screenshot_capture.get("success"):
        return str(screenshot_capture.get("error_type") or screenshot_capture.get("error") or "screenshot_unavailable")
    return str(result.get("interpretation_status") or result.get("status") or "visual_acquisition_unavailable")


def _structured_visual_state(
    *,
    result: dict[str, Any],
    screenshot_capture: dict[str, Any],
) -> dict[str, Any]:
    evidence = result.get("visual_evidence_packet")
    capture = evidence.get("capture") if isinstance(evidence, dict) and isinstance(evidence.get("capture"), dict) else {}
    obstruction = capture.get("obstruction") if isinstance(capture.get("obstruction"), dict) else {}
    evidence_status = str(capture.get("status") or "").strip().lower()
    if not evidence_status:
        evidence_status = "captured" if screenshot_capture.get("success") is True else "unavailable"
    structured_obstruction = {
        "present": obstruction.get("present") is True,
        "type": str(obstruction.get("type") or ""),
        "severity": str(obstruction.get("severity") or ""),
        "confidence": obstruction.get("confidence"),
        "coverage_ratio": obstruction.get("coverage_ratio"),
        "signals": [str(item) for item in obstruction.get("signals") or []][:8],
    }
    return {
        "evidence_status": evidence_status,
        "screenshot_status": str(screenshot_capture.get("status") or "unknown").strip().lower(),
        "first_fold_evaluable": (
            capture.get("first_fold_evaluable")
            if isinstance(capture.get("first_fold_evaluable"), bool)
            else None
        ),
        "obstruction": structured_obstruction if obstruction else {},
    }


def _visual_obstruction_detail(result: dict[str, Any]) -> str:
    evidence = result.get("visual_evidence_packet")
    if not isinstance(evidence, dict):
        return ""
    capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
    obstruction = capture.get("obstruction") if isinstance(capture.get("obstruction"), dict) else {}
    if not obstruction.get("present"):
        return ""
    obstruction_type = str(obstruction.get("type") or "unknown")
    severity = str(obstruction.get("severity") or "unknown")
    signals = [str(signal) for signal in obstruction.get("signals") or [] if str(signal).strip()]
    suffix = f"; signals: {', '.join(signals[:4])}" if signals else ""
    return f"obstruction:{obstruction_type}; severity:{severity}{suffix}"


def _visual_cookie_banner_snippet(*, result: dict[str, Any], screenshot_capture: dict[str, Any]) -> str:
    evidence = result.get("visual_evidence_packet")
    if isinstance(evidence, dict):
        capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
        obstruction = capture.get("obstruction") if isinstance(capture.get("obstruction"), dict) else {}
        signals = " ".join(str(signal) for signal in obstruction.get("signals") or [])
        obstruction_type = str(obstruction.get("type") or "")
        if "cookie" in f"{obstruction_type} {signals}".lower() or "privacy" in f"{obstruction_type} {signals}".lower():
            return " ".join(f"{obstruction_type} {signals}".split())[:220]
    metadata = screenshot_capture.get("metadata") if isinstance(screenshot_capture.get("metadata"), dict) else {}
    dismissal = metadata.get("cookie_banner_dismissal") if isinstance(metadata.get("cookie_banner_dismissal"), dict) else {}
    if dismissal.get("success") is True:
        return "cookie_banner_dismissal:success"
    return ""


def _acquisition_artifacts_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for row in snapshot.get("raw_inputs") or []:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if source == "screenshot_capture":
            capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
            artifacts.extend(_screenshot_artifacts(capture))
        elif source == "visual_acquisition":
            evidence = payload.get("visual_evidence_packet") if isinstance(payload.get("visual_evidence_packet"), dict) else {}
            capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
            obstruction = capture.get("obstruction") if isinstance(capture.get("obstruction"), dict) else {}
            artifacts.append(
                {
                    "source": "visual_acquisition",
                    "kind": "visual_evidence_packet",
                    "status": str(capture.get("status") or ""),
                    "first_fold_evaluable": capture.get("first_fold_evaluable"),
                    "obstruction": {
                        "present": obstruction.get("present"),
                        "type": str(obstruction.get("type") or ""),
                        "severity": str(obstruction.get("severity") or ""),
                        "signals": [str(item) for item in obstruction.get("signals") or []][:8],
                    },
                }
            )
    return [artifact for artifact in artifacts if artifact]


def _screenshot_artifact(capture: dict[str, Any]) -> dict[str, Any]:
    screenshot_path = str(capture.get("screenshot_path") or "").strip()
    screenshot_url = str(capture.get("screenshot_url") or "").strip()
    metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
    artifact = {
        "source": "screenshot_capture",
        "kind": "screenshot",
        "status": str(capture.get("status") or ""),
        "success": capture.get("success") is True,
        "provider": str(capture.get("source") or ""),
        "label": "Primer viewport",
        "screenshot_url": screenshot_url,
        "screenshot_path": screenshot_path,
        "public_url": _public_screenshot_url(screenshot_path=screenshot_path, screenshot_url=screenshot_url),
        "metadata": metadata,
    }
    return artifact if screenshot_path or screenshot_url or artifact["status"] else {}


def _screenshot_artifacts(capture: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = [_screenshot_artifact(capture)]
    metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
    full_page_path = str(metadata.get("full_page_screenshot_path") or "").strip()
    if full_page_path:
        artifacts.append(
            {
                "source": "screenshot_capture",
                "kind": "full_page_screenshot",
                "status": str(metadata.get("section_capture_status") or capture.get("status") or ""),
                "success": True,
                "provider": str(capture.get("source") or ""),
                "label": "Página completa",
                "screenshot_path": full_page_path,
                "public_url": _public_screenshot_url(screenshot_path=full_page_path, screenshot_url=""),
                "capture_variant": str(
                    (metadata.get("section_manifest") or {}).get("capture_variant")
                    if isinstance(metadata.get("section_manifest"), dict)
                    else ""
                ),
            }
        )

    atlas_path = str(metadata.get("analysis_atlas_path") or "").strip()
    if atlas_path:
        atlas_manifest = (
            metadata.get("analysis_atlas_manifest")
            if isinstance(metadata.get("analysis_atlas_manifest"), dict)
            else {}
        )
        artifacts.append(
            {
                "source": "screenshot_capture",
                "kind": "visual_analysis_atlas",
                "status": str(metadata.get("analysis_atlas_status") or ""),
                "success": True,
                "provider": str(capture.get("source") or ""),
                "label": "Atlas usado por el análisis semántico",
                "screenshot_path": atlas_path,
                "public_url": _public_screenshot_url(
                    screenshot_path=atlas_path,
                    screenshot_url="",
                ),
                "panel_count": atlas_manifest.get("panel_count"),
                "section_panel_count": atlas_manifest.get("section_panel_count"),
            }
        )

    manifest = metadata.get("section_manifest") if isinstance(metadata.get("section_manifest"), dict) else {}
    sections = manifest.get("sections") if isinstance(manifest.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_path = str(section.get("capture_path") or "").strip()
        if not section_path:
            continue
        artifacts.append(
            {
                "source": "screenshot_capture",
                "kind": "section_screenshot",
                "status": "captured",
                "success": True,
                "provider": str(capture.get("source") or ""),
                "section_id": str(section.get("id") or ""),
                "label": str(section.get("label") or ""),
                "section_kind": str(section.get("kind") or "section"),
                "bbox": dict(section.get("bbox") or {}) if isinstance(section.get("bbox"), dict) else {},
                "capture_variant": str(section.get("capture_variant") or manifest.get("capture_variant") or ""),
                "screenshot_path": section_path,
                "public_url": _public_screenshot_url(screenshot_path=section_path, screenshot_url=""),
            }
        )
    return [artifact for artifact in artifacts if artifact]


def _public_screenshot_url(*, screenshot_path: str, screenshot_url: str) -> str:
    from pathlib import Path
    from urllib.parse import urlparse
    from src.config import BRAND3_SCREENSHOT_DIR

    candidate = screenshot_path
    if not candidate and screenshot_url.startswith("file://"):
        candidate = urlparse(screenshot_url).path
    if not candidate:
        return ""
    try:
        path = Path(candidate).resolve()
        root = Path(BRAND3_SCREENSHOT_DIR).resolve()
        path.relative_to(root)
    except Exception:
        return ""
    return f"/artifacts/screenshots/{path.name}"


def _to_payload(data: Any) -> dict[str, Any]:
    if hasattr(data, "to_dict"):
        return data.to_dict()
    if dataclasses.is_dataclass(data):
        return dataclasses.asdict(data)
    return dict(vars(data))


_COMPONENT_ORDER = (
    "core_purpose",
    "magnetism",
    "value_proposition",
    "personality",
    "brand_idea",
    "attributes",
    "values",
    "mission",
    "vision",
    "coherencia",
)


def _resolve_scan_tile_profile(
    detail: dict[str, Any],
    *,
    lit: int,
    off: int,
    blind: int,
    scale_hint: int,
) -> Any:
    tile_profile = detail.get("tile_profile")
    if tile_profile is not None:
        if isinstance(tile_profile, list):
            if not tile_profile:
                return {
                    "lit": lit,
                    "off": off,
                    "blind": blind,
                    "scale": scale_hint,
                }
            if lit or off or blind:
                lit_tiles = sum(
                    1
                    for tile in tile_profile
                    if str((tile or {}).get("estado") or "") == "ok"
                )
                off_tiles = sum(
                    1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "no"
                )
                blind_tiles = sum(
                    1
                    for tile in tile_profile
                    if str((tile or {}).get("estado") or "") == "sin_evidencia"
                )
                if (lit_tiles, off_tiles, blind_tiles) != (lit, off, blind):
                    return {
                        "lit": lit,
                        "off": off,
                        "blind": blind,
                        "scale": scale_hint,
                    }
        return tile_profile

    fallback_tiles = detail.get("tiles")
    if isinstance(fallback_tiles, list):
        if not fallback_tiles:
            return {
                "lit": lit,
                "off": off,
                "blind": blind,
                "scale": scale_hint,
            }
        if lit or off or blind:
            lit_tiles = sum(
                1 for tile in fallback_tiles if str((tile or {}).get("estado") or "") == "ok"
            )
            off_tiles = sum(
                1 for tile in fallback_tiles if str((tile or {}).get("estado") or "") == "no"
            )
            blind_tiles = sum(
                1
                for tile in fallback_tiles
                if str((tile or {}).get("estado") or "") == "sin_evidencia"
            )
            if (lit_tiles, off_tiles, blind_tiles) != (lit, off, blind):
                return {
                    "lit": lit,
                    "off": off,
                    "blind": blind,
                    "scale": scale_hint,
                }
        return fallback_tiles

    return {
        "lit": lit,
        "off": off,
        "blind": blind,
        "scale": scale_hint,
    }


def _tile_name_map(component_key: str) -> dict[str, str]:
    from src.sv9.rubric import COMPONENTS as RUBRIC_COMPONENTS

    meta = RUBRIC_COMPONENTS.get(component_key) or {}
    return {str(tile.get("id") or ""): str(tile.get("name") or "") for tile in meta.get("tiles") or []}


def _tile_counts_from_profile(tile_profile: list[dict[str, Any]]) -> tuple[int, int, int]:
    lit = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "ok")
    off = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "no")
    blind = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "sin_evidencia")
    return lit, off, blind


def _failing_tiles_from_profile(component_key: str, tile_profile: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tile_names = _tile_name_map(component_key)
    failing_tiles: list[dict[str, Any]] = []
    for tile in tile_profile:
        if not isinstance(tile, dict) or tile.get("estado") == "ok":
            continue
        failing_tiles.append(
            {
                "id": str(tile.get("id") or tile.get("tile_id") or ""),
                "name": str(tile_names.get(str(tile.get("id") or tile.get("tile_id") or "")) or ""),
                "estado": str(tile.get("estado") or ""),
                "motivo": str(tile.get("motivo") or ""),
                "contexto_requerido": str(tile.get("contexto_requerido") or ""),
                "evidencia": str(tile.get("evidencia") or ""),
            }
        )
    return failing_tiles


def _sv9_editorial_enabled() -> bool:
    return os.environ.get("B3S_SV9_EDITORIAL_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _component_needs_editorial_message(component_key: str, detail: dict[str, Any]) -> bool:
    from src.sv9.language_guard import spanish_component_verdict

    if str(detail.get("message") or "").strip():
        return False
    tile_profile = detail.get("tile_profile") if isinstance(detail.get("tile_profile"), list) else []
    verdict = spanish_component_verdict(component_key, detail.get("veredicto"), tile_profile)
    return not verdict or verdict.startswith("Síntesis automática")


def _attach_sv9_editorial(
    payload: dict[str, Any],
    *,
    llm: Any | None = None,
    build_editorial_fn: Any | None = None,
) -> dict[str, Any]:
    if not _sv9_editorial_enabled():
        return payload
    sv9 = payload.get("sv9") if isinstance(payload.get("sv9"), dict) else {}
    result = sv9.get("result") if isinstance(sv9.get("result"), dict) else {}
    components = result.get("components") if isinstance(result.get("components"), dict) else {}
    if not components:
        return payload
    structured = result.get("editorial_v3_1") if isinstance(result.get("editorial_v3_1"), dict) else {}
    structured_components = (
        structured.get("components") if isinstance(structured.get("components"), dict) else {}
    )
    ordered_keys = [
        key for key in _COMPONENT_ORDER
        if key in components and isinstance(components.get(key), dict)
    ]
    ordered_keys.extend(
        key
        for key, detail in components.items()
        if key not in ordered_keys and isinstance(detail, dict)
    )
    needed = [
        key
        for key in ordered_keys
        for detail in [components.get(key)]
        if isinstance(detail, dict) and _component_needs_editorial_message(str(key), detail)
    ]
    if not structured_components:
        needed = ordered_keys
    else:
        for key in ordered_keys:
            if key not in structured_components and key not in needed:
                needed.append(key)
    if not needed:
        return payload
    try:
        if llm is None:
            from src.config import SV9_EDITORIAL_MODEL
            from src.features.llm_analyzer import LLMAnalyzer

            llm = LLMAnalyzer(model=SV9_EDITORIAL_MODEL)
        if not getattr(llm, "api_key", None):
            return payload
        if build_editorial_fn is None:
            from src.sv9.editorial import build_editorial as build_editorial_fn

        editorial = build_editorial_fn(
            result,
            llm=llm,
            component_keys=needed,
            include_executive_reading=True,
        )
    except Exception as exc:
        sv9["editorial"] = {"status": "failed", "error": str(exc)}
        return payload

    messages = editorial.get("component_messages") if isinstance(editorial, dict) else {}
    for key, message in (messages or {}).items():
        detail = components.get(key)
        if isinstance(detail, dict) and str(message or "").strip():
            detail["message"] = str(message).strip()
    reading = editorial.get("executive_reading") if isinstance(editorial, dict) else None
    if str(reading or "").strip():
        result["executive_reading"] = str(reading).strip()
    structured = editorial.get("structured") if isinstance(editorial, dict) else None
    structured_components = {}
    if isinstance(structured, dict):
        result["editorial_v3_1"] = structured
        structured_components = (
            structured.get("components") if isinstance(structured.get("components"), dict) else {}
        )
    sv9["editorial"] = {
        "status": "attached",
        "mode": "v3_1_structured",
        "requested_components": needed,
        "message_components": sorted(str(key) for key in (messages or {}).keys()),
        "executive_reading": bool(str(reading or "").strip()),
        "structured_schema_version": str((structured or {}).get("schema_version") or ""),
        "structured_components": sorted(str(key) for key in structured_components.keys()),
    }
    return payload


def _trusted_persisted_acquisition_gate(
    *,
    acquisition_attempts: list[dict[str, Any]],
    persisted_limitations: list[str],
    observed_at: str,
) -> dict[str, Any]:
    attempts: dict[str, dict[str, Any]] = {}
    for raw_attempt in acquisition_attempts:
        attempt = dict(raw_attempt)
        provider = str(attempt.get("provider") or "")
        if provider not in {"web", "exa"} or provider in attempts:
            raise RuntimeError("vault_trusted_acquisition_attempts_invalid")
        attempts[provider] = attempt
    web_attempt = attempts.get("web")
    if attempts and (
        web_attempt is None
        or str(web_attempt.get("intent") or "") != "owned_web"
        or str(web_attempt.get("status") or "") != "success"
        or str(web_attempt.get("detail") or "")
        != "verified_raw_document_persisted"
    ):
        raise RuntimeError("vault_trusted_web_attempt_invalid")

    warning: dict[str, Any] | None = None
    fallbacks: list[dict[str, Any]] = []
    gate_limitation = ""
    exa_attempt = attempts.get("exa")
    limitation_set = set(persisted_limitations)
    if exa_attempt is not None:
        if str(exa_attempt.get("intent") or "") != "external_social_profile":
            raise RuntimeError("vault_trusted_external_attempt_invalid")
        status = str(exa_attempt.get("status") or "")
        detail = str(exa_attempt.get("detail") or "")
        if status == "success":
            if detail != "verified_raw_document_persisted" or any(
                item.startswith("external_acquisition:")
                for item in limitation_set
            ):
                raise RuntimeError("vault_trusted_external_attempt_invalid")
        elif status == "not_discovered":
            if (
                detail != "independent_discovery_unassociated"
                or "external_acquisition:not_discovered" not in limitation_set
            ):
                raise RuntimeError("vault_trusted_external_attempt_invalid")
            warning = {
                "source": "exa",
                "code": "external_identity_not_discovered",
                "severity": "warning",
                "message": (
                    "Exa searched and found no associated external identity; "
                    "continuing with owned-web analysis only. C7 remains "
                    "sin_evidencia until a second channel is captured and reviewed."
                ),
                "status": "not_discovered",
                "detail": "external_acquisition:not_discovered",
                "can_fallback": False,
            }
            gate_limitation = "acquisition_gate:external_identity_not_discovered"
        elif status == "error":
            if detail not in {
                "provider_not_configured",
                "provider_unavailable",
                "provider_result_ineligible",
            } or f"external_acquisition:{detail}" not in limitation_set:
                raise RuntimeError("vault_trusted_external_attempt_invalid")
            warning = {
                "source": "exa",
                "code": "exa_failed",
                "severity": "warning",
                "message": (
                    "Exa failed; continuing with owned-web analysis only. "
                    "C7 remains sin_evidencia until its second channel is captured and reviewed."
                ),
                "status": "error",
                "detail": detail,
                "can_fallback": False,
            }
            fallbacks = [
                {
                    "source": "searchapi",
                    "for_source": "exa",
                    "available": False,
                    "approved": False,
                    "status": "disabled",
                    "reason": "vertical external-proof fallback for Exa failure",
                }
            ]
            gate_limitation = "acquisition_gate:exa_failed"
        else:
            raise RuntimeError("vault_trusted_external_attempt_invalid")
    elif "external_acquisition:not_discovered" in limitation_set:
        warning = {
            "source": "external_identity",
            "code": "external_identity_not_discovered",
            "severity": "warning",
            "message": (
                "No independent external identity was discovered; continuing "
                "with owned-web analysis only. C7 remains sin_evidencia until its second channel is captured and reviewed."
            ),
            "status": "not_discovered",
            "detail": "external_acquisition:not_discovered",
            "can_fallback": False,
        }
        gate_limitation = "acquisition_gate:external_identity_not_discovered"
    else:
        warning = {
            "source": "trusted_acquisition",
            "code": "trusted_acquisition_metadata_unavailable",
            "severity": "warning",
            "message": "Persisted external acquisition metadata is unavailable.",
            "status": "unknown",
            "detail": "legacy_trusted_capture_without_external_outcome",
            "can_fallback": False,
        }
        gate_limitation = "acquisition_gate:trusted_metadata_unavailable"

    warnings = [warning] if warning is not None else []
    return {
        "version": "b3s-acquisition-gate-v2",
        "state": "warning" if warnings else "pass",
        "can_continue": True,
        "allow_degraded_fallback": False,
        "issues": [],
        "warnings": warnings,
        "fallbacks": fallbacks,
        "limitations": [gate_limitation] if gate_limitation else [],
        "user_decision": None,
        "evaluated_at": observed_at,
    }


def _vault_semantic_contract_from_observation(
    observation: Mapping[str, Any],
) -> dict[str, str]:
    from src.services.evidence_vault_incremental_refresh import (
        semantic_analysis_contract_from_plan,
    )

    metadata = (
        dict(observation.get("metadata") or {})
        if isinstance(observation.get("metadata"), Mapping)
        else {}
    )
    nested = (
        dict(metadata.get("observation") or {})
        if isinstance(metadata.get("observation"), Mapping)
        else {}
    )
    plan = metadata.get("operation_plan") or nested.get("operation_plan")
    if not isinstance(plan, Mapping):
        return {}
    contract = semantic_analysis_contract_from_plan(plan)
    return dict(contract or {})



def _compose_vault_memory_report(
    *,
    scan_id: str,
    url: str,
    brand_name: str,
    capture_observation: dict[str, Any],
    memory: dict[str, Any],
    promotion_event: dict[str, Any],
    semantic_scoring_v3: dict[str, Any],
    authority_coverage: dict[str, Any],
    verification_requirements: dict[str, Any],
    legacy_operational_v2: dict[str, Any],
) -> dict[str, Any]:
    """Build a legacy diagnostic projection from persisted Vault memory.

    This remains available to historical diagnostics, but scanner publication
    never calls it: the immutable scanner report is always composed from the
    persisted capture's fresh Flow/SV9 assessment in ``_run``.
    """

    from src.history.capture_observation import parse_capture_observation
    from src.history.report_parser import normalize_domain
    from src.services.evidence_vault_canonical_core import build_tile_contract_registry
    from src.services.evidence_vault_operational_scoring import (
        validate_operational_score_authority_witness,
    )
    from src.sv9.assessment_kernel import (
        Sv9AssessmentError,
        validate_sv9_assessment_output,
    )
    from src.sv9.rubric import (
        COMPONENTS as RUBRIC_COMPONENTS,
        PRESENTATION_ORDER,
        confidence_from_blind_spots,
    )

    legacy_score = legacy_operational_v2.get("score_evaluation")
    legacy_witness = legacy_operational_v2.get("score_authority_witness")
    if not isinstance(legacy_score, dict) or not isinstance(legacy_witness, dict):
        raise RuntimeError("vault_legacy_operational_v2_witness_invalid")
    validate_operational_score_authority_witness(
        legacy_witness,
        canonical_memory=memory,
        promotion_event=promotion_event,
        evaluation=legacy_score,
    )
    memory_version = str(memory.get("canonical_memory_version") or "")
    if not memory_version:
        raise RuntimeError("vault_memory_version_unavailable")
    if semantic_scoring_v3.get("availability") != "available":
        reasons = semantic_scoring_v3.get("reason_codes") or ["unknown"]
        raise RuntimeError(f"vault_semantic_scoring_v3_unavailable:{','.join(reasons)}")
    score = semantic_scoring_v3.get("assessment_output")
    if not isinstance(score, dict):
        raise RuntimeError("vault_semantic_scoring_v3_output_invalid")
    try:
        validate_sv9_assessment_output(score)
    except Sv9AssessmentError as exc:
        raise RuntimeError("vault_semantic_scoring_v3_output_invalid") from exc
    parsed_capture = parse_capture_observation(capture_observation)
    semantic_analysis_contract = _vault_semantic_contract_from_observation(
        capture_observation
    )
    if parsed_capture.source_scan_id != scan_id:
        raise RuntimeError("vault_report_capture_scan_mismatch")
    if (
        urlparse(parsed_capture.canonical_url).hostname
        != urlparse(url).hostname
    ):
        raise RuntimeError("vault_report_capture_domain_mismatch")
    memory_content = (
        dict(memory.get("content") or {})
        if isinstance(memory.get("content"), dict)
        else {}
    )
    memory_brand_identity = str(
        memory.get("brand_identity")
        or memory_content.get("brand_identity")
        or ""
    )
    if normalize_domain(memory_brand_identity) != parsed_capture.canonical_domain:
        raise RuntimeError("vault_report_memory_brand_mismatch")

    registry = build_tile_contract_registry()
    accepted = {
        str(row.get("tile_id") or ""): row
        for row in (memory.get("content", {}).get("accepted_tiles") or [])
        if isinstance(row, dict)
    }
    breakdown = {
        str(row.get("component_key") or ""): row
        for row in score.get("component_breakdown") or []
        if isinstance(row, dict)
    }
    semantic_tiles = {
        str(row.get("tile_id") or ""): row
        for row in score.get("tiles") or []
        if isinstance(row, dict)
    }
    coverage = authority_coverage
    total_blind_spots = sum(
        int(row.get("sin_evidencia_count") or 0)
        for row in breakdown.values()
    )
    reliability_status = (
        "reliable"
        if coverage.get("score_completeness") == "complete"
        and coverage.get("canonical_score_status") == "current"
        and total_blind_spots == 0
        else "shadow"
    )
    reliability_reason_codes: list[str] = []
    if coverage.get("score_completeness") != "complete":
        reliability_reason_codes.append("vault_authority_coverage_partial")
    if coverage.get("canonical_score_status") != "current":
        reliability_reason_codes.append("vault_canonical_score_not_current")
    if total_blind_spots > 2:
        reliability_reason_codes.append("blind_spots_above_usable_threshold")
    elif total_blind_spots > 0:
        reliability_reason_codes.append("blind_spots_present")
    profiles_by_component: dict[str, list[dict[str, Any]]] = {}
    for tile in registry["tiles"]:
        tile_id = str(tile["tile_id"])
        component_key = str(tile["component_key"])
        accepted_row = accepted.get(tile_id) or {}
        state = str((semantic_tiles.get(tile_id) or {}).get("assessment_state") or "")
        if state not in {"ok", "no", "sin_evidencia"}:
            raise RuntimeError("vault_semantic_scoring_v3_tile_invalid")
        basis_rows = [
            dict(row)
            for row in accepted_row.get("basis") or []
            if isinstance(row, dict)
        ]
        profiles_by_component.setdefault(component_key, []).append(
            {
                "id": tile_id,
                "tile_id": tile_id,
                "estado": state,
                "evidencia": "",
                "vault_authority_state": (
                    "accepted" if tile_id in accepted else "unresolved"
                ),
                "vault_authority_profile_id": str(
                    accepted_row.get("authority_profile_id") or ""
                ),
                "vault_decision_event_id": str(
                    accepted_row.get("decision_event_id") or ""
                ),
                "vault_basis_relation_count": len(basis_rows),
                "vault_basis": basis_rows,
                "motivo": "Sin evidencia persistida" if state == "sin_evidencia" else "",
            }
        )
    components: list[dict[str, Any]] = []
    result_components: dict[str, dict[str, Any]] = {}
    for component_key, profiles in profiles_by_component.items():
        component_score = breakdown.get(component_key) or {}
        component_meta = RUBRIC_COMPONENTS.get(component_key) or {}
        lit_tiles = [row["tile_id"] for row in profiles if row["estado"] == "ok"]
        off_tiles = [row["tile_id"] for row in profiles if row["estado"] == "no"]
        blind_spot_tiles = [
            row["tile_id"]
            for row in profiles
            if row["estado"] == "sin_evidencia"
        ]
        detail = {
            "key": component_key,
            "component": component_key,
            "label": str(component_meta.get("label") or component_key),
            "question": str(component_meta.get("question") or ""),
            "level_zero": str(component_meta.get("level_zero") or ""),
            "status": "scored",
            "score": component_score.get("effective_score", 0),
            "scale": component_score.get(
                "tile_count",
                component_meta.get("scale", 0),
            ),
            "points": component_score.get("points", 0),
            "lit": component_score.get("ok_count", 0),
            "off": component_score.get("no_count", 0),
            "blind": component_score.get("sin_evidencia_count", 0),
            "blind_spot_count": len(blind_spot_tiles),
            "confidence": confidence_from_blind_spots(len(blind_spot_tiles)),
            "lit_tiles": lit_tiles,
            "off_tiles": off_tiles,
            "blind_spot_tiles": blind_spot_tiles,
            "tile_profile": profiles,
            "veredicto": "Proyección de memoria Vault",
        }
        components.append(detail)
        result_components[component_key] = dict(detail)
    gap_key: str | None = None
    gap_rank: tuple[int, int, int] | None = None
    immediate_margin = 0
    for position, component_key in enumerate(PRESENTATION_ORDER):
        component_score = breakdown.get(component_key) or {}
        max_points = int(component_score.get("max_points") or 0)
        points = int(component_score.get("points") or 0)
        gap = max_points - points
        rank = (-gap, -max_points, position)
        if gap > 0 and (gap_rank is None or rank < gap_rank):
            gap_rank = rank
            gap_key = component_key
        if (
            int(component_score.get("effective_score") or 0)
            < int(component_score.get("tile_count") or 0)
            and not (
                component_key == "magnetism"
                and score.get("magnetism_capped") is True
            )
        ):
            immediate_margin += int(component_score.get("multiplier") or 0)
    gap_label = str(
        (RUBRIC_COMPONENTS.get(str(gap_key)) or {}).get("label") or gap_key or ""
    )
    evidence_pack = {
        "schema_version": "brand-evidence-pack-v1",
        "brand_name": parsed_capture.brand_name,
        "url": parsed_capture.canonical_url,
        "evidence": [dict(row) for row in parsed_capture.evidence_records],
        "limitations": list(parsed_capture.limitations),
    }
    capture_payload = parsed_capture.capture_payload
    report_attempts = [dict(row) for row in parsed_capture.acquisition_attempts]
    report_capture_limitations = list(parsed_capture.limitations)
    if (
        parsed_capture.pipeline_version
        == "evidence-vault-trusted-acquisition-v1"
    ):
        try:
            (
                report_capture_limitations,
                report_attempts,
            ) = trusted_acquisition_report_metadata(capture_payload)
        except ValueError:
            raise RuntimeError("vault_trusted_acquisition_outcome_invalid") from None
        acquisition_gate = _trusted_persisted_acquisition_gate(
            acquisition_attempts=report_attempts,
            persisted_limitations=report_capture_limitations,
            observed_at=parsed_capture.observed_at.isoformat(),
        )
    else:
        acquisition_gate = (
            dict(capture_payload.get("acquisition_gate") or {})
            if isinstance(capture_payload.get("acquisition_gate"), dict)
            else {}
        )
    evidence_pack["limitations"] = list(report_capture_limitations)
    limitations = ["score_projected_from_evidence_vault_semantic_scoring_v3"]
    for item in [
        *report_capture_limitations,
        *(acquisition_gate.get("limitations") or []),
    ]:
        value = str(item)
        if value and value not in limitations:
            limitations.append(value)
    attempts = list(report_attempts)
    absences = [
        {
            "block": str(row.get("evidence_type") or "").rsplit(".", 1)[-1],
            "url": str(row.get("url") or ""),
            "content": str(row.get("content") or ""),
        }
        for row in parsed_capture.evidence_records
        if str(row.get("evidence_type") or "").startswith(
            "acquisition.absence."
        )
    ]
    acquisition_artifacts = [dict(row) for row in parsed_capture.artifacts]
    created_at = datetime.now(timezone.utc).isoformat()
    observed_at = parsed_capture.observed_at.isoformat()
    evaluated_at = parsed_capture.observed_at.isoformat()
    raw = {
        "schema_version": "b3s-vault-semantic-report-v2",
        "source_run_id": parsed_capture.source_run_id,
        "vault_memory_projection": True,
        "source_capture": {
            "source_scan_id": parsed_capture.source_scan_id,
            "observation_hash": parsed_capture.observation_hash,
            "capture_hash": parsed_capture.capture_hash,
        },
        "flow": {
            "candidate": {"evidence_pack": evidence_pack, "interpretation": {}},
            "interpretation_debug": {
                "mode": "evidence_vault_semantic_scoring_v3",
                "semantic_analysis_contract": semantic_analysis_contract,
            },
        },
        "sv9": {
            "brand3_score": score.get("sv9_score"),
            "base_average": score.get("base_average"),
            "reliability_status": reliability_status,
            "components": result_components,
            "result": {
                "components": result_components,
                "brand3_score": score.get("sv9_score"),
                "base_average": score.get("base_average"),
                "rubric_version": score.get("rubric_version"),
                "evaluator_model": "evidence-vault-semantic-scoring-v3",
                "magnetism_capped": score.get("magnetism_capped") is True,
                "most_painful_gap": gap_key,
                "immediate_margin": immediate_margin,
                "total_blind_spots": total_blind_spots,
                "reliability_status": reliability_status,
                "reliability_reason_codes": reliability_reason_codes,
            },
        },
        "vault": {
            "memory_version": memory_version,
            "semantic_scoring_v3": semantic_scoring_v3,
            "authority_coverage": authority_coverage,
            "verification_requirements": verification_requirements,
            "legacy_operational_v2": legacy_operational_v2,
        },
    }
    return {
        "id": scan_id,
        "brand_name": brand_name,
        "url": url,
        "created_at": created_at,
        "observed_at": observed_at,
        "recorded_at": created_at,
        "evaluated_at": evaluated_at,
        "pipeline_commit_sha": current_build_sha(),
        "score": score.get("sv9_score"),
        "base_average": score.get("base_average"),
        "reliability_status": raw["sv9"]["reliability_status"],
        "reliability_reason_codes": reliability_reason_codes,
        "magnetism_capped": score.get("magnetism_capped") is True,
        "not_detected": [],
        "most_painful_gap": gap_key,
        "most_painful_gap_label": gap_label,
        "immediate_margin": immediate_margin,
        "total_blind_spots": total_blind_spots,
        "executive_reading": "Assessment semántico reconstruido desde Vault.",
        "editorial": {},
        "detected_count": 0,
        "block_count": 0,
        "components": components,
        "blocks": [],
        "acquisition_gate": acquisition_gate,
        "acquisition_artifacts": acquisition_artifacts,
        "coverage_acquisition": dict(parsed_capture.acquisition_summary),
        "absences": absences,
        "attempts": attempts,
        "limitations": limitations,
        "vault_memory_version": memory_version,
        "vault_semantic_observation_identity": semantic_scoring_v3.get("observation_identity"),
        "raw": raw,
    }


def _compose_report(scan_id: str, url: str, brand_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    from src.sv9.language_guard import (
        spanish_component_summary,
        spanish_component_verdict,
        spanish_generated_text,
    )

    flow = payload.get("flow") or {}
    candidate = flow.get("candidate") or {}
    interpretation = candidate.get("interpretation") or {}
    pack = candidate.get("evidence_pack") or {}
    records = {r.get("ref"): r for r in pack.get("evidence") or [] if isinstance(r, dict)}
    debug = flow.get("interpretation_debug") or {}
    coverage = debug.get("evidence_coverage") or {}
    coverage_blocks = coverage.get("blocks") or {}
    coverage_hierarchy = coverage.get("component_hierarchy") or {}
    sv9 = payload.get("sv9") or {}
    acquisition_gate = payload.get("acquisition_gate") if isinstance(payload.get("acquisition_gate"), dict) else {}
    acquisition_artifacts = payload.get("acquisition_artifacts") if isinstance(payload.get("acquisition_artifacts"), list) else []

    blocks = []
    for name, block in sorted((interpretation.get("blocks") or {}).items()):
        if not isinstance(block, dict):
            continue
        refs = [str(r) for r in (interpretation.get("evidence_refs") or {}).get(name) or []]
        provenance = block.get("detection_provenance") if isinstance(block.get("detection_provenance"), dict) else {}
        blocks.append(
            {
                "name": name,
                "detected": block.get("detected") is True,
                "confidence": str(block.get("confidence") or ""),
                "content": str(block.get("content") or ""),
                "rejected_content": str(block.get("rejected_content") or ""),
                "provenance_source": str(provenance.get("final_source") or ""),
                "coverage_status": str((coverage_blocks.get(name) or {}).get("status") or ""),
                "refs": [
                    {
                        "ref": ref,
                        "url": str((records.get(ref) or {}).get("url") or ""),
                        "snippet": " ".join(str((records.get(ref) or {}).get("content") or "").split())[:280],
                    }
                    for ref in refs
                ],
            }
        )

    absences = []
    attempts = []
    for record in pack.get("evidence") or []:
        if not isinstance(record, dict):
            continue
        evidence_type = str(record.get("evidence_type") or "")
        if evidence_type.startswith("acquisition.absence."):
            absences.append(
                {
                    "block": evidence_type.rsplit(".", 1)[-1],
                    "url": str(record.get("url") or ""),
                    "content": str(record.get("content") or ""),
                }
            )
        elif evidence_type.startswith("acquisition.attempt."):
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            attempts.append(
                {
                    "provider": str(metadata.get("provider") or record.get("source") or ""),
                    "intent": str(metadata.get("intent") or ""),
                    "status": str(metadata.get("status") or ""),
                    "detail": str(metadata.get("query") or metadata.get("error") or ""),
                }
            )

    blocks_by_name = {block["name"]: block for block in blocks}
    result = sv9.get("result") if isinstance(sv9.get("result"), dict) else {}
    summary_assessment = (
        sv9.get("assessment") if isinstance(sv9.get("assessment"), dict) else None
    )
    result_assessment = (
        result.get("assessment")
        if isinstance(result.get("assessment"), dict)
        else None
    )
    if (
        summary_assessment is not None
        and result_assessment is not None
        and summary_assessment != result_assessment
    ):
        raise RuntimeError("sv9_assessment_duplicate_mismatch")
    assessment = copy.deepcopy(summary_assessment or result_assessment)
    result_components = result.get("components") if isinstance(result.get("components"), dict) else {}
    structured_editorial = (
        result.get("editorial_v3_1") if isinstance(result.get("editorial_v3_1"), dict) else {}
    )
    structured_editorial_components = (
        structured_editorial.get("components")
        if isinstance(structured_editorial.get("components"), dict)
        else {}
    )
    from src.sv9.rubric import COMPONENTS as RUBRIC_COMPONENTS

    components = []
    known = tuple(dict.fromkeys(_COMPONENT_ORDER + tuple((sv9.get("components") or {}).keys())))
    for name in known:
        component = (sv9.get("components") or {}).get(name)
        if not isinstance(component, dict):
            continue
        detail = result_components.get(name) if isinstance(result_components.get(name), dict) else {}
        meta = RUBRIC_COMPONENTS.get(name) or {}
        scale = int(detail.get("scale") or meta.get("scale") or 0)
        raw_tile_profile = detail.get("tile_profile")
        tile_profile = (
            [tile for tile in raw_tile_profile if isinstance(tile, dict)]
            if isinstance(raw_tile_profile, list)
            else []
        )
        if tile_profile:
            lit_count, off_count, blind_count = _tile_counts_from_profile(tile_profile)
        else:
            lit_tiles = set(component.get("lit_tiles") or detail.get("lit_tiles") or [])
            off_tiles = set(component.get("off_tiles") or detail.get("off_tiles") or [])
            blind_tiles = set(component.get("blind_spot_tiles") or detail.get("blind_spot_tiles") or [])
            lit_count = len(lit_tiles)
            off_count = len(off_tiles)
            blind_count = len(blind_tiles)
            resolved = _resolve_scan_tile_profile(
                detail,
                lit=lit_count,
                off=off_count,
                blind=blind_count,
                scale_hint=scale,
            )
            tile_profile = list(resolved) if isinstance(resolved, list) else []

        tile_states = []
        tile_names = _tile_name_map(name)
        if tile_profile:
            for tile in tile_profile:
                tile_id = str(tile.get("id") or tile.get("tile_id") or "")
                estado = str(tile.get("estado") or "")
                state = {"ok": "on", "no": "off", "sin_evidencia": "blind"}.get(estado)
                if state:
                    tile_states.append(
                        {"id": tile_id, "name": str(tile_names.get(tile_id) or ""), "state": state}
                    )
        else:
            lit_tiles = set(component.get("lit_tiles") or detail.get("lit_tiles") or [])
            off_tiles = set(component.get("off_tiles") or detail.get("off_tiles") or [])
            blind_tiles = set(component.get("blind_spot_tiles") or detail.get("blind_spot_tiles") or [])
            for tile in meta.get("tiles") or []:
                tile_id = str(tile.get("id") or "")
                if tile_id in lit_tiles:
                    state = "on"
                elif tile_id in off_tiles:
                    state = "off"
                elif tile_id in blind_tiles:
                    state = "blind"
                else:
                    continue
                tile_states.append({"id": tile_id, "name": str(tile.get("name") or ""), "state": state})

        failing_tiles = _failing_tiles_from_profile(name, tile_profile)
        detected_content = str(detail.get("detected_content") or "").strip()
        message = spanish_generated_text(detail.get("message"))
        editorial_component = (
            structured_editorial_components.get(name)
            if isinstance(structured_editorial_components.get(name), dict)
            else {}
        )
        components.append(
            {
                "key": name,
                "component": name,
                "label": str(meta.get("label") or name),
                "question": str(meta.get("question") or ""),
                "level_zero": str(meta.get("level_zero") or ""),
                "score": component.get("score"),
                "scale": scale,
                "status": str(component.get("status") or ""),
                "confidence": str(detail.get("confidence") or ""),
                "tile_profile": list(tile_profile) if isinstance(tile_profile, list) else [],
                "resumen": spanish_component_summary(
                    name,
                    detected_content,
                    tile_profile or {"lit": lit_count, "off": off_count, "blind": blind_count, "scale": scale},
                ),
                "detected_content": detected_content,
                "veredicto": spanish_component_verdict(name, detail.get("veredicto"), tile_profile),
                "message": message,
                "editorial": editorial_component,
                "points": detail.get("points"),
                "evaluation_model": detail.get("evaluation_model"),
                "evidence": list(detail.get("evidence") or []),
                "source_policy_notes": list(detail.get("source_policy_notes") or []),
                "lit": lit_count,
                "off": off_count,
                "blind": blind_count,
                "tile_states": tile_states,
                "tiles": failing_tiles,
                "block": blocks_by_name.get(name),
                "surface_hierarchy": (
                    coverage_hierarchy.get(name)
                    if isinstance(coverage_hierarchy.get(name), dict)
                    else {}
                ),
            }
        )

    gap_key = str(result.get("most_painful_gap") or "")
    detected_count = sum(1 for block in blocks if block["detected"])
    limitations = [str(item) for item in candidate.get("limitations") or []]
    for item in acquisition_gate.get("limitations") or []:
        value = str(item)
        if value and value not in limitations:
            limitations.append(value)

    report = {
        "id": scan_id,
        "brand_name": brand_name,
        "url": url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_commit_sha": current_build_sha(),
        "score": sv9.get("brand3_score"),
        "base_average": sv9.get("base_average"),
        "magnetism_capped": sv9.get("magnetism_capped"),
        "sv9_assessment": assessment,
        "assessment_fingerprint": (
            assessment.get("assessment_fingerprint")
            if isinstance(assessment, dict)
            else None
        ),
        "score_fingerprint": (
            assessment.get("score_fingerprint")
            if isinstance(assessment, dict)
            else None
        ),
        "reliability_status": str(sv9.get("reliability_status") or "shadow"),
        "not_detected": [str(item) for item in sv9.get("not_detected") or []],
        "most_painful_gap": gap_key,
        "most_painful_gap_label": str((RUBRIC_COMPONENTS.get(gap_key) or {}).get("label") or gap_key),
        "immediate_margin": result.get("immediate_margin"),
        "total_blind_spots": result.get("total_blind_spots"),
        "executive_reading": structured_editorial.get("executive_reading") or result.get("executive_reading"),
        "editorial": structured_editorial,
        "detected_count": detected_count,
        "block_count": len(blocks),
        "components": components,
        "blocks": blocks,
        "acquisition_gate": acquisition_gate,
        "acquisition_artifacts": acquisition_artifacts,
        "coverage_acquisition": coverage.get("acquisition") or {},
        "absences": absences,
        "attempts": attempts,
        "limitations": limitations,
        "raw": payload,
    }
    _validate_report_sv9_assessment(report, required=False)
    return report
