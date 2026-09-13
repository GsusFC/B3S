"""Application service boundary for Scanner API job creation and lookup."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from src.config import BRAND3_DB_PATH
from src.services.evidence_claim_reconciliation import (
    EvidenceClaimReconciliationCommand,
    EvidenceClaimReconciliationConflictError,
    EvidenceClaimReconciliationInvalidTransitionError,
    EvidenceClaimReconciliationNotFoundError,
    EvidenceClaimReconciliationUnavailableError,
)
from src.services.evidence_claim_tile_review import (
    EvidenceClaimTileReviewCommand,
    EvidenceClaimTileReviewConflictError,
    EvidenceClaimTileReviewInvalidTransitionError,
    EvidenceClaimTileReviewNotFoundError,
    EvidenceClaimTileReviewPacketNotFoundError,
    EvidenceClaimTileReviewUnavailableError,
)
from src.services.evidence_memory_adjudication import (
    EvidenceMemoryAdjudicationCommand,
    EvidenceMemoryAdjudicationConflictError,
    EvidenceMemoryAdjudicationInvalidTransitionError,
    EvidenceMemoryAdjudicationNotFoundError,
    EvidenceMemoryAdjudicationUnavailableError,
)
from src.services.evidence_scoring_recovery_review import (
    EvidenceScoringRecoveryReviewCommand,
    EvidenceScoringRecoveryReviewConflictError,
    EvidenceScoringRecoveryReviewInvalidTransitionError,
    EvidenceScoringRecoveryReviewNotFoundError,
    EvidenceScoringRecoveryReviewUnavailableError,
)
from src.storage.sqlite_store import SQLiteStore
from web.exact_resume_controller import launch_vault_exact_resume_action
from web.report_store import (
    append_evidence_claim_reconciliation_for_domain,
    append_evidence_claim_tile_review_for_domain,
    append_evidence_memory_adjudication_for_domain,
    append_evidence_scoring_recovery_review_for_domain,
    get_evidence_claim_tile_review_packet_for_domain,
    get_evidence_claim_tile_review_queue_for_domain,
    list_evidence_claim_reconciliations_for_domain,
    list_evidence_claim_tile_reviews_for_domain,
    list_evidence_memory_adjudications_for_domain,
    list_evidence_scoring_recovery_reviews_for_domain,
    _postgres_repository,
    load_report,
    new_scan_id,
    register_evidence_claim_tile_review_packet_for_domain,
)
from web.scan_runner import (
    _vault_operational_pipeline_enabled,
    _load_persisted_scan_status,
    default_brand_name,
    normalize_url,
    scan_diagnostic_dossier_from_status,
    scan_status,
    start_scan,
)

from .errors import ApiError
from .models import ScanResumeActionResponse
from .presenters import completed_status_from_report


_IDEMPOTENCY_KEY_RE = re.compile(r"^[\x21-\x7E]{1,200}$")
_RESUME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EXACT_RESUME_REQUEST = {"operation": "exact_resume"}
_RESUME_NOT_FOUND = (404, "not_found", "Resource not found.")


def vault_exact_resume_api_enabled() -> bool:
    """Return the fail-closed, Vault-only API gate without touching storage."""

    if (
        os.environ.get("B3S_VAULT_EXACT_RESUME_API_ENABLED", "false")
        .strip()
        .lower()
        != "true"
    ):
        return False
    return _vault_operational_pipeline_enabled()


def create_vault_exact_resume_action(
    scan_id: str,
    *,
    client_id: str,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:

    _require_resume_api()
    _validate_resume_id(scan_id, "scan_id")
    key_hash = _idempotency_key_hash(idempotency_key)
    if key_hash is None:
        raise ApiError(
            400,
            "idempotency_key_required",
            "Idempotency-Key is required for exact resume actions.",
        )
    fingerprint = _request_fingerprint(_EXACT_RESUME_REQUEST)
    try:
        repository = _postgres_repository()
    except Exception as exc:
        raise ApiError(503, "vault_resume_repository_unavailable", "The Vault resume repository is temporarily unavailable.") from exc
    if repository is None:
        raise ApiError(503, "vault_resume_repository_unavailable", "The Vault resume repository is temporarily unavailable.")
    try:
        store = SQLiteStore(BRAND3_DB_PATH)
        try:
            reservation = store.reserve_scanner_resume_action(
                scan_id=scan_id,
                request_payload=dict(_EXACT_RESUME_REQUEST),
                status_payload={"state": "accepted", "phase": "accepted"},
                client_id=client_id,
                idempotency_key_hash=key_hash,
                request_fingerprint=fingerprint,
            )
        finally:
            store.close()
    except Exception as exc:
        _resume_store_unavailable(exc)

    outcome = reservation.get("outcome")
    if outcome == "conflict":
        raise ApiError(
            409,
            "resume_action_conflict",
            "An exact-resume action conflicts with the requested scan or key.",
        )
    if outcome not in {"created", "replay"}:
        _resume_store_unavailable()
    action_id = reservation.get("action_id")
    if not isinstance(action_id, str):
        _resume_store_unavailable()
    if outcome == "created":
        try:
            launch_vault_exact_resume_action(
                reservation=reservation,
                repository=repository,
                database_path=BRAND3_DB_PATH,
            )
        except Exception as exc:
            raise ApiError(503, "vault_resume_action_unavailable", "The Vault exact-resume action is temporarily unavailable.") from exc
    action = _read_resume_action(action_id)
    if action is None:
        _resume_store_unavailable()
    public = _public_resume_action(action)
    if public["scan_id"] != scan_id:
        _resume_store_unavailable()
    return public, outcome == "replay"


def get_vault_exact_resume_action(
    scan_id: str,
    action_id: str,
) -> dict[str, Any] | None:

    _require_resume_api()
    _validate_resume_id(scan_id, "scan_id")
    _validate_resume_id(action_id, "action_id")
    action = _read_resume_action(action_id)
    if action is None:
        return None
    public = _public_resume_action(action)
    if public["scan_id"] != scan_id:
        return None
    return public


def _require_resume_api() -> None:
    if not vault_exact_resume_api_enabled():
        raise ApiError(*_RESUME_NOT_FOUND)


def _validate_resume_id(value: str, field: str) -> None:
    if not isinstance(value, str) or _RESUME_ID_RE.fullmatch(value) is None:
        raise ApiError(400, f"invalid_{field}", f"The {field} is invalid.")


def _read_resume_action(action_id: str) -> dict[str, Any] | None:
    try:
        store = SQLiteStore(BRAND3_DB_PATH)
        try:
            return store.get_scanner_resume_action(action_id=action_id)
        finally:
            store.close()
    except Exception as exc:
        _resume_store_unavailable(exc)


def _resume_store_unavailable(exc: BaseException | None = None) -> None:
    error = ApiError(
        503,
        "scanner_resume_action_store_unavailable",
        "The exact-resume action store is temporarily unavailable.",
    )
    if exc is None:
        raise error
    raise error from exc


def _public_resume_action(action: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(action, dict):
        _resume_store_unavailable()
    state = action.get("state")
    if state not in {"accepted", "running", "completed", "failed", "interrupted"}:
        _resume_store_unavailable()
    action_id, scan_id = action.get("action_id"), action.get("scan_id")
    if not all(isinstance(value, str) for value in (action_id, scan_id)):
        _resume_store_unavailable()
    public: dict[str, Any] = {
        "object": "scan_resume_action",
        "api_version": "v1",
        "action_id": action_id,
        "scan_id": scan_id,
        "state": state,
        "created_at": action.get("created_at"),
        "updated_at": action.get("updated_at"),
        "completed_at": action.get("completed_at"),
        "result": None,
        "failure": None,
        "links": {
            "self": f"/api/v1/scans/{scan_id}/resume-actions/{action_id}",
            "scan": f"/api/v1/scans/{scan_id}",
        },
    }
    status_payload = action.get("status_payload")
    if not isinstance(status_payload, dict):
        _resume_store_unavailable()
    if state == "completed":
        public["result"] = {
            "publication_action": status_payload.get("publication_action"),
            "report_id": status_payload.get("report_id"),
        }
    elif state in {"failed", "interrupted"}:
        public["failure"] = {
            "reason_code": status_payload.get("reason_code"),
            "retryable": status_payload.get("retryable"),
        }
    try:
        return ScanResumeActionResponse.model_validate(public).model_dump(mode="json")
    except Exception as exc:
        _resume_store_unavailable(exc)


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


def get_scan_diagnostic_status(scan_id: str) -> dict[str, Any] | None:
    """Detail-only bounded enrichment; compact polling never calls this."""
    normalized_id = str(scan_id)
    status = scan_status(normalized_id)
    if status is None or status.get("id") != normalized_id:
        return None
    enriched = dict(status)
    context: dict[str, Any] = {"operation_lookup": "not_applicable"}
    try:
        persisted = _load_persisted_scan_status(normalized_id)
    except Exception:
        context["status_readback"] = "unavailable"
    else:
        if isinstance(persisted, dict) and persisted.get("id") == normalized_id:
            if isinstance(persisted.get("diagnostic_operation_ledger"), dict):
                active_detail = scan_diagnostic_dossier_from_status(enriched)
                persisted_detail = scan_diagnostic_dossier_from_status(persisted)
                same_ledger = (
                    active_detail.get("events") == persisted_detail.get("events")
                    and active_detail.get("dropped_event_count") == persisted_detail.get("dropped_event_count")
                    and active_detail.get("truncated_event_count") == persisted_detail.get("truncated_event_count")
                )
                context["status_readback"] = (
                    "observed_ledger" if same_ledger else "divergent"
                )
            else:
                context["status_readback"] = "observed_without_ledger"
        else:
            context["status_readback"] = "missing"
    if _known_vault_scan_for_detail(enriched):
        try:
            repository = _postgres_repository()
            operation = (
                repository.get_capture_operation_plan(normalized_id, workspace_slug="b3s")
                if repository is not None
                else None
            )
        except Exception:
            context["operation_lookup"] = "unavailable"
        else:
            if isinstance(operation, dict) and operation.get("source_scan_id") == normalized_id:
                context["operation"] = operation
            else:
                context["operation_lookup"] = "missing"
    enriched["_diagnostic_detail_context"] = context
    return enriched


def _known_vault_scan_for_detail(status: dict[str, Any]) -> bool:
    """Require an existing Vault binding before the on-demand repository read."""
    if not _vault_operational_pipeline_enabled():
        return False
    stage = status.get("execution_stage")
    if isinstance(stage, str) and stage.startswith("vault_"):
        return True
    ledger = status.get("diagnostic_operation_ledger")
    events = ledger.get("events") if isinstance(ledger, dict) else []
    if not isinstance(events, list):
        return False
    return any(
        isinstance(event, dict)
        and isinstance(event.get("operation"), str)
        and event["operation"].startswith("vault_")
        for event in events
    )


def get_completed_report(scan_id: str) -> dict[str, Any]:
    report = load_report(str(scan_id))
    if report is not None:
        return report
    status = get_scan(scan_id)
    if status is None:
        raise ApiError(404, "scan_not_found", f"Scan {scan_id} was not found.")
    try:
        persisted_status = _load_persisted_scan_status(str(scan_id))
    except Exception:
        persisted_status = None
    source_report_id = (
        persisted_status.get("report_id") if isinstance(persisted_status, dict) else None
    )
    if persisted_status and persisted_status.get("state") == "done" and isinstance(source_report_id, str):
        source_report = load_report(source_report_id)
        if source_report is not None:
            return source_report
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
    reviewer_id: str,
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
        "reviewer": str(reviewer_id or "").strip(),
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


def create_evidence_claim_reconciliation(
    domain: str,
    request_payload: dict[str, Any],
    *,
    client_id: str,
    reviewer_id: str,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Append a versioned relation decision without selecting a claim."""

    key_hash = _idempotency_key_hash(idempotency_key)
    if key_hash is None:
        raise ApiError(
            400,
            "idempotency_key_required",
            "Idempotency-Key is required for claim reconciliations.",
        )
    normalized = {
        "domain": str(domain).strip().lower(),
        "subject_id": str(
            request_payload.get("subject_id") or ""
        ).strip().lower(),
        "decision": str(
            request_payload.get("decision") or ""
        ).strip().lower(),
        "expected_current_event_id": (
            str(request_payload["expected_current_event_id"]).strip().lower()
            if request_payload.get("expected_current_event_id")
            else None
        ),
        "reviewer": str(reviewer_id or "").strip(),
        "reason_code": str(
            request_payload.get("reason_code") or ""
        ).strip().lower(),
        "rationale": str(
            request_payload.get("rationale") or ""
        ).strip(),
        "evaluator_version": str(
            request_payload.get("evaluator_version") or ""
        ).strip(),
        "actor_id": str(client_id or "").strip(),
    }
    command = EvidenceClaimReconciliationCommand(
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
        return append_evidence_claim_reconciliation_for_domain(
            domain,
            command,
        )
    except EvidenceClaimReconciliationNotFoundError as exc:
        raise ApiError(
            404,
            "claim_relation_not_found",
            str(exc),
            details={"subject_id": command.subject_id},
        ) from exc
    except EvidenceClaimReconciliationConflictError as exc:
        if exc.existing_event_id:
            raise ApiError(
                409,
                "idempotency_key_reused",
                str(exc),
                details={"existing_event_id": exc.existing_event_id},
            ) from exc
        raise ApiError(
            409,
            "claim_reconciliation_precondition_failed",
            str(exc),
            details={"current_event_id": exc.current_event_id},
        ) from exc
    except EvidenceClaimReconciliationInvalidTransitionError as exc:
        raise ApiError(
            409,
            "invalid_claim_reconciliation_transition",
            str(exc),
        ) from exc
    except EvidenceClaimReconciliationUnavailableError as exc:
        raise ApiError(
            503,
            "claim_reconciliation_store_unavailable",
            "The durable claim reconciliation journal is temporarily unavailable.",
        ) from exc


def get_evidence_claim_reconciliations(
    domain: str,
    *,
    subject_id: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Read a page from the durable claim-reconciliation journal."""

    try:
        return list_evidence_claim_reconciliations_for_domain(
            domain,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    except EvidenceClaimReconciliationNotFoundError as exc:
        raise ApiError(404, "brand_not_found", str(exc)) from exc
    except EvidenceClaimReconciliationUnavailableError as exc:
        raise ApiError(
            503,
            "claim_reconciliation_store_unavailable",
            "The durable claim reconciliation journal is temporarily unavailable.",
        ) from exc


def create_evidence_claim_tile_review(
    domain: str,
    request_payload: dict[str, Any],
    *,
    client_id: str,
    reviewer_id: str,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Append a semantic mapping decision without tile or scoring authority."""

    key_hash = _idempotency_key_hash(idempotency_key)
    if key_hash is None:
        raise ApiError(
            400,
            "idempotency_key_required",
            "Idempotency-Key is required for claim-to-tile reviews.",
        )
    normalized = {
        "domain": str(domain).strip().lower(),
        "subject_id": str(
            request_payload.get("subject_id") or ""
        ).strip().lower(),
        "decision": str(
            request_payload.get("decision") or ""
        ).strip().lower(),
        "expected_current_event_id": (
            str(
                request_payload["expected_current_event_id"]
            ).strip().lower()
            if request_payload.get("expected_current_event_id")
            else None
        ),
        "reviewer": str(reviewer_id or "").strip(),
        "reason_code": str(
            request_payload.get("reason_code") or ""
        ).strip().lower(),
        "rationale": str(
            request_payload.get("rationale") or ""
        ).strip(),
        "evaluator_version": str(
            request_payload.get("evaluator_version") or ""
        ).strip(),
        "review_packet_fingerprint": str(
            request_payload.get("review_packet_fingerprint") or ""
        ).strip().lower(),
        "actor_id": str(client_id or "").strip(),
    }
    command = EvidenceClaimTileReviewCommand(
        subject_id=normalized["subject_id"],
        decision=normalized["decision"],
        expected_current_event_id=normalized[
            "expected_current_event_id"
        ],
        reviewer=normalized["reviewer"],
        reason_code=normalized["reason_code"],
        rationale=normalized["rationale"],
        evaluator_version=normalized["evaluator_version"],
        review_packet_fingerprint=normalized[
            "review_packet_fingerprint"
        ],
        actor_id=normalized["actor_id"],
        idempotency_key_hash=key_hash,
        request_fingerprint=_request_fingerprint(normalized),
    )
    try:
        return append_evidence_claim_tile_review_for_domain(
            domain,
            command,
        )
    except EvidenceClaimTileReviewPacketNotFoundError as exc:
        raise ApiError(
            404,
            "claim_tile_review_packet_not_found",
            str(exc),
            details={
                "review_packet_fingerprint": (
                    command.review_packet_fingerprint
                )
            },
        ) from exc
    except EvidenceClaimTileReviewNotFoundError as exc:
        raise ApiError(
            404,
            "claim_tile_mapping_not_found",
            str(exc),
            details={"subject_id": command.subject_id},
        ) from exc
    except EvidenceClaimTileReviewConflictError as exc:
        if exc.existing_event_id:
            raise ApiError(
                409,
                "idempotency_key_reused",
                str(exc),
                details={
                    "existing_event_id": exc.existing_event_id
                },
            ) from exc
        raise ApiError(
            409,
            "claim_tile_review_precondition_failed",
            str(exc),
            details={"current_event_id": exc.current_event_id},
        ) from exc
    except EvidenceClaimTileReviewInvalidTransitionError as exc:
        raise ApiError(
            409,
            "invalid_claim_tile_review_transition",
            str(exc),
        ) from exc
    except EvidenceClaimTileReviewUnavailableError as exc:
        raise ApiError(
            503,
            "claim_tile_review_store_unavailable",
            "The durable claim-to-tile review journal is temporarily unavailable.",
        ) from exc


def register_evidence_claim_tile_review_packet(
    domain: str,
) -> tuple[dict[str, Any], bool]:
    """Build and append one exact, private, non-authoritative packet."""

    try:
        return register_evidence_claim_tile_review_packet_for_domain(
            domain
        )
    except EvidenceClaimTileReviewPacketNotFoundError as exc:
        raise ApiError(
            404,
            "claim_tile_review_packet_source_not_found",
            str(exc),
        ) from exc
    except EvidenceClaimTileReviewUnavailableError as exc:
        raise ApiError(
            503,
            "claim_tile_review_packet_store_unavailable",
            "The durable claim-to-tile review packet registry is "
            "temporarily unavailable.",
        ) from exc


def get_evidence_claim_tile_review_packet(
    domain: str,
    packet_fingerprint: str,
) -> dict[str, Any]:
    """Read one exact registered private reviewer packet."""

    try:
        return get_evidence_claim_tile_review_packet_for_domain(
            domain,
            packet_fingerprint,
        )
    except EvidenceClaimTileReviewPacketNotFoundError as exc:
        raise ApiError(
            404,
            "claim_tile_review_packet_not_found",
            str(exc),
            details={
                "review_packet_fingerprint": packet_fingerprint,
            },
        ) from exc
    except EvidenceClaimTileReviewUnavailableError as exc:
        raise ApiError(
            503,
            "claim_tile_review_packet_store_unavailable",
            "The durable claim-to-tile review packet registry is "
            "temporarily unavailable.",
        ) from exc


def get_evidence_claim_tile_review_queue(
    domain: str,
    packet_fingerprint: str,
) -> dict[str, Any]:
    """Read the mutable review status derived for one immutable packet."""

    try:
        return get_evidence_claim_tile_review_queue_for_domain(
            domain,
            packet_fingerprint,
        )
    except EvidenceClaimTileReviewPacketNotFoundError as exc:
        raise ApiError(
            404,
            "claim_tile_review_packet_not_found",
            str(exc),
            details={
                "review_packet_fingerprint": packet_fingerprint,
            },
        ) from exc
    except EvidenceClaimTileReviewUnavailableError as exc:
        raise ApiError(
            503,
            "claim_tile_review_queue_unavailable",
            "The claim-to-tile review queue is temporarily unavailable.",
        ) from exc


def get_evidence_claim_tile_reviews(
    domain: str,
    *,
    subject_id: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Read a page from the durable semantic claim-to-tile journal."""

    try:
        return list_evidence_claim_tile_reviews_for_domain(
            domain,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    except EvidenceClaimTileReviewNotFoundError as exc:
        raise ApiError(404, "brand_not_found", str(exc)) from exc
    except EvidenceClaimTileReviewUnavailableError as exc:
        raise ApiError(
            503,
            "claim_tile_review_store_unavailable",
            "The durable claim-to-tile review journal is temporarily unavailable.",
        ) from exc


def create_evidence_scoring_recovery_review(
    domain: str,
    request_payload: dict[str, Any],
    *,
    client_id: str,
    reviewer_id: str,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Append a semantic recovery decision without runtime scoring authority."""

    key_hash = _idempotency_key_hash(idempotency_key)
    if key_hash is None:
        raise ApiError(
            400,
            "idempotency_key_required",
            "Idempotency-Key is required for scoring recovery reviews.",
        )
    normalized = {
        "domain": str(domain).strip().lower(),
        "subject_id": str(
            request_payload.get("subject_id") or ""
        ).strip().lower(),
        "case_id": str(
            request_payload.get("case_id") or ""
        ).strip(),
        "decision": str(
            request_payload.get("decision") or ""
        ).strip().lower(),
        "expected_current_event_id": (
            str(
                request_payload["expected_current_event_id"]
            ).strip().lower()
            if request_payload.get("expected_current_event_id")
            else None
        ),
        "reviewer": str(reviewer_id or "").strip(),
        "reason_code": str(
            request_payload.get("reason_code") or ""
        ).strip().lower(),
        "rationale": str(
            request_payload.get("rationale") or ""
        ).strip(),
        "evaluator_version": str(
            request_payload.get("evaluator_version") or ""
        ).strip(),
        "actor_id": str(client_id or "").strip(),
    }
    command = EvidenceScoringRecoveryReviewCommand(
        subject_id=normalized["subject_id"],
        case_id=normalized["case_id"],
        decision=normalized["decision"],
        expected_current_event_id=normalized[
            "expected_current_event_id"
        ],
        reviewer=normalized["reviewer"],
        reason_code=normalized["reason_code"],
        rationale=normalized["rationale"],
        evaluator_version=normalized["evaluator_version"],
        actor_id=normalized["actor_id"],
        idempotency_key_hash=key_hash,
        request_fingerprint=_request_fingerprint(normalized),
    )
    try:
        return append_evidence_scoring_recovery_review_for_domain(
            domain,
            command,
        )
    except EvidenceScoringRecoveryReviewNotFoundError as exc:
        raise ApiError(
            404,
            "scoring_recovery_not_found",
            str(exc),
            details={"subject_id": command.subject_id},
        ) from exc
    except EvidenceScoringRecoveryReviewConflictError as exc:
        if exc.existing_event_id:
            raise ApiError(
                409,
                "idempotency_key_reused",
                str(exc),
                details={
                    "existing_event_id": exc.existing_event_id
                },
            ) from exc
        raise ApiError(
            409,
            "scoring_recovery_review_precondition_failed",
            str(exc),
            details={"current_event_id": exc.current_event_id},
        ) from exc
    except EvidenceScoringRecoveryReviewInvalidTransitionError as exc:
        raise ApiError(
            409,
            "invalid_scoring_recovery_review_transition",
            str(exc),
        ) from exc
    except EvidenceScoringRecoveryReviewUnavailableError as exc:
        raise ApiError(
            503,
            "scoring_recovery_review_store_unavailable",
            "The durable scoring recovery review journal is temporarily unavailable.",
        ) from exc


def get_evidence_scoring_recovery_reviews(
    domain: str,
    *,
    subject_id: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Read a page from the durable semantic-recovery review journal."""

    try:
        return list_evidence_scoring_recovery_reviews_for_domain(
            domain,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    except EvidenceScoringRecoveryReviewNotFoundError as exc:
        raise ApiError(404, "brand_not_found", str(exc)) from exc
    except EvidenceScoringRecoveryReviewUnavailableError as exc:
        raise ApiError(
            503,
            "scoring_recovery_review_store_unavailable",
            "The durable scoring recovery review journal is temporarily unavailable.",
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
