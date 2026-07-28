"""Application service boundary for Scanner API job creation and lookup."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from src.config import BRAND3_DB_PATH
from src.services.evidence_memory_adjudication import (
    EvidenceMemoryAdjudicationCommand,
    EvidenceMemoryAdjudicationConflictError,
    EvidenceMemoryAdjudicationInvalidTransitionError,
    EvidenceMemoryAdjudicationNotFoundError,
    EvidenceMemoryAdjudicationUnavailableError,
)
from src.storage.sqlite_store import SQLiteStore
from web.report_store import (
    append_evidence_memory_adjudication_for_domain,
    list_evidence_memory_adjudications_for_domain,
    load_report,
    new_scan_id,
)
from web.scan_runner import default_brand_name, normalize_url, scan_status, start_scan

from .errors import ApiError
from .presenters import completed_status_from_report


_IDEMPOTENCY_KEY_RE = re.compile(r"^[\x21-\x7E]{1,200}$")


def create_scan_job(
    request_payload: dict[str, Any],
    *,
    client_id: str,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    try:
        normalized_url = normalize_url(str(request_payload.get("url") or ""))
    except ValueError as exc:
        raise ApiError(400, "url_rejected", str(exc)) from exc

    brand_name = str(request_payload.get("brand_name") or "").strip() or default_brand_name(normalized_url)
    normalized_request = {
        "url": normalized_url,
        "brand_name": brand_name,
        "language": str(request_payload.get("language") or "es"),
        "allow_degraded_fallback": bool(request_payload.get("allow_degraded_fallback")),
    }
    key_hash = _idempotency_key_hash(idempotency_key)
    fingerprint = _request_fingerprint(normalized_request)
    proposed_scan_id = new_scan_id()
    initial_status = {
        "id": proposed_scan_id,
        "url": normalized_url,
        "brand_name": brand_name,
        "state": "accepted",
        "phase": "accepted",
        "phases": [],
        "acquisition": [],
        "acquisition_gate": {"state": "pending", "issues": [], "warnings": [], "fallbacks": []},
        "allow_degraded_fallback": normalized_request["allow_degraded_fallback"],
        "error": None,
        "error_code": None,
        "started_at": None,
        "completed_at": None,
    }

    store = SQLiteStore(BRAND3_DB_PATH)
    try:
        reservation = store.reserve_scanner_api_job(
            scan_id=proposed_scan_id,
            request_payload=normalized_request,
            status_payload=initial_status,
            client_id=client_id,
            idempotency_key_hash=key_hash,
            request_fingerprint=fingerprint,
        )
    except Exception as exc:
        raise ApiError(
            503,
            "scanner_control_store_unavailable",
            "The scanner job store is temporarily unavailable.",
        ) from exc
    finally:
        store.close()

    if reservation["outcome"] == "conflict":
        raise ApiError(
            409,
            "idempotency_key_reused",
            "The Idempotency-Key has already been used for a different request.",
            details={"existing_scan_id": reservation["scan_id"]},
        )
    if reservation["outcome"] == "replay":
        existing = get_scan(reservation["scan_id"])
        if existing is None:
            raise ApiError(
                409,
                "idempotency_record_unavailable",
                "The original idempotent scan exists but its status is unavailable.",
                details={"existing_scan_id": reservation["scan_id"]},
            )
        return existing, True

    try:
        start_scan(
            normalized_url,
            brand_name,
            allow_degraded_fallback=normalized_request["allow_degraded_fallback"],
            scan_id=proposed_scan_id,
            client_id=client_id,
        )
    except Exception as exc:
        failed_status = {
            **initial_status,
            "state": "error",
            "phase": "error",
            "error": "The scanner job could not be started.",
            "error_code": "scan_start_failed",
        }
        failed_store = SQLiteStore(BRAND3_DB_PATH)
        try:
            failed_store.save_scanner_job_status(failed_status)
        finally:
            failed_store.close()
        raise ApiError(503, "scan_start_failed", "The scanner job could not be started.") from exc

    created = get_scan(proposed_scan_id)
    if created is None:
        raise ApiError(500, "scan_status_unavailable", "The scanner started but its status is unavailable.")
    return created, False


def get_scan(scan_id: str) -> dict[str, Any] | None:
    report = load_report(str(scan_id))
    if report is not None:
        return completed_status_from_report(report)
    active = scan_status(str(scan_id))
    if active is not None:
        return active
    return None


def get_completed_report(scan_id: str) -> dict[str, Any]:
    report = load_report(str(scan_id))
    if report is not None:
        return report
    status = get_scan(scan_id)
    if status is None:
        raise ApiError(404, "scan_not_found", f"Scan {scan_id} was not found.")
    raise ApiError(
        409,
        "scan_result_not_ready",
        "The scan result is not available yet.",
        details={"scan_id": str(scan_id), "state": str(status.get("state") or "unknown")},
        headers={"Retry-After": "5"},
    )


def create_evidence_memory_adjudication(
    domain: str,
    request_payload: dict[str, Any],
    *,
    client_id: str,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Append a versioned identity decision without changing runtime outputs."""

    key_hash = _idempotency_key_hash(idempotency_key)
    if key_hash is None:
        raise ApiError(
            400,
            "idempotency_key_required",
            "Idempotency-Key is required for evidence adjudications.",
        )
    normalized = {
        "domain": str(domain).strip().lower(),
        "subject_id": str(request_payload.get("subject_id") or "").strip().lower(),
        "decision": str(request_payload.get("decision") or "").strip().lower(),
        "expected_current_event_id": (
            str(request_payload["expected_current_event_id"]).strip().lower()
            if request_payload.get("expected_current_event_id")
            else None
        ),
        "reviewer": str(request_payload.get("reviewer") or "").strip(),
        "reason_code": str(request_payload.get("reason_code") or "").strip().lower(),
        "rationale": str(request_payload.get("rationale") or "").strip(),
        "evaluator_version": str(
            request_payload.get("evaluator_version") or ""
        ).strip(),
        "actor_id": str(client_id or "").strip(),
    }
    command = EvidenceMemoryAdjudicationCommand(
        subject_id=normalized["subject_id"],
        decision=normalized["decision"],
        expected_current_event_id=normalized["expected_current_event_id"],
        reviewer=normalized["reviewer"],
        reason_code=normalized["reason_code"],
        rationale=normalized["rationale"],
        evaluator_version=normalized["evaluator_version"],
        actor_id=normalized["actor_id"],
        idempotency_key_hash=key_hash,
        request_fingerprint=_request_fingerprint(normalized),
    )
    try:
        return append_evidence_memory_adjudication_for_domain(domain, command)
    except EvidenceMemoryAdjudicationNotFoundError as exc:
        raise ApiError(
            404,
            "evidence_subject_not_found",
            str(exc),
            details={"subject_id": command.subject_id},
        ) from exc
    except EvidenceMemoryAdjudicationConflictError as exc:
        if exc.existing_event_id:
            raise ApiError(
                409,
                "idempotency_key_reused",
                str(exc),
                details={"existing_event_id": exc.existing_event_id},
            ) from exc
        raise ApiError(
            409,
            "adjudication_precondition_failed",
            str(exc),
            details={"current_event_id": exc.current_event_id},
        ) from exc
    except EvidenceMemoryAdjudicationInvalidTransitionError as exc:
        raise ApiError(
            409,
            "invalid_adjudication_transition",
            str(exc),
        ) from exc
    except EvidenceMemoryAdjudicationUnavailableError as exc:
        raise ApiError(
            503,
            "evidence_adjudication_store_unavailable",
            "The durable evidence adjudication journal is temporarily unavailable.",
        ) from exc


def get_evidence_memory_adjudications(
    domain: str,
    *,
    subject_id: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Read a page from the durable evidence identity decision journal."""

    try:
        return list_evidence_memory_adjudications_for_domain(
            domain,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    except EvidenceMemoryAdjudicationNotFoundError as exc:
        raise ApiError(404, "brand_not_found", str(exc)) from exc
    except EvidenceMemoryAdjudicationUnavailableError as exc:
        raise ApiError(
            503,
            "evidence_adjudication_store_unavailable",
            "The durable evidence adjudication journal is temporarily unavailable.",
        ) from exc


def _idempotency_key_hash(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ApiError(
            400,
            "invalid_idempotency_key",
            "Idempotency-Key must contain 1-200 visible ASCII characters.",
        )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
