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
import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse

from src.build_info import current_build_sha
from src.services.scanner_report_assessment import (
    validate_report_sv9_assessment as _validate_report_sv9_assessment,
)
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
from web.report_store import list_reports_for_domain, new_scan_id, save_report

_SCANS: dict[str, dict[str, Any]] = {}
_SCAN_EVENTS: dict[str, threading.Event] = {}
_VAULT_ACTIVATIONS: set[str] = set()
_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)

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
    scan_id = str(scan_id or new_scan_id())
    started_at = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        _SCANS[scan_id] = {
            "id": scan_id,
            "url": url,
            "brand_name": brand_name,
            "state": "running",
            "phase": "capture",
            "phases": [{"key": key, "label": label, "state": "pending"} for key, label in _PHASES],
            "acquisition": [],
            "acquisition_gate": {"state": "pending", "issues": [], "warnings": [], "fallbacks": []},
            "allow_degraded_fallback": bool(allow_degraded_fallback),
            "error": None,
            "error_code": None,
            "started_at": started_at,
            "completed_at": None,
        }
        _SCAN_EVENTS[scan_id] = threading.Event()
        persisted_status = _status_copy_locked(_SCANS[scan_id])
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
        with _LOCK:
            _SCANS.pop(scan_id, None)
            _SCAN_EVENTS.pop(scan_id, None)
        raise
    thread = threading.Thread(
        target=_run,
        args=(scan_id, url, brand_name, bool(allow_degraded_fallback)),
        daemon=True,
    )
    thread.start()
    return scan_id


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


def _publish_completed_report(scan_id: str, report: dict[str, Any]) -> bool:
    """Publish one immutable report with the same cancellation boundary."""

    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None or status.get("state") == "cancelled":
            return False
        save_report(report)
        _set_phase_locked(status, "report", "done")
        status["state"] = "done"
        status["phase"] = "done"
        status["report_id"] = str(report.get("id") or scan_id)
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


def _vault_operational_pipeline_enabled() -> bool:
    return (
        os.environ.get("BRAND3_ENVIRONMENT", "").strip().lower() == "vault"
        and os.environ.get(
            "BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED",
            "false",
        ).strip().lower()
        == "true"
    )


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
        from src.features.llm_analyzer import LLMAnalyzer
        from src.services.evidence_vault_sv9_judgment_shadow import run_evidence_vault_sv9_judgment_shadow
        from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapter
        from src.sv9.judgment_memory import build_judgment_series_contract

        flow = (payload.get("flow") or {}) if isinstance(payload, Mapping) else {}
        candidate = flow.get("candidate") if isinstance(flow, Mapping) else {}
        pack = candidate.get("evidence_pack") if isinstance(candidate, Mapping) else {}
        refs = sorted({str(row.get("ref") or "").strip() for row in (pack.get("evidence") or []) if isinstance(row, Mapping) and str(row.get("ref") or "").strip()}) if isinstance(pack, Mapping) else []
        result = run_evidence_vault_sv9_judgment_shadow(repository=repository, flow=FlowSv9StrictComponentAdapter(LLMAnalyzer(model=os.environ.get("BRAND3_FLOW_INTERPRETATION_MODEL") or SV9_FLOW_MODEL)), source_scan_id=scan_id, current_series_contract=build_judgment_series_contract(evaluator_version="evidence-vault-sv9-judgment-shadow-v1", prompt_version="sv9-strict-component-v1", model_version=os.environ.get("BRAND3_FLOW_INTERPRETATION_MODEL") or SV9_FLOW_MODEL, flow_version="sv9-flow-strict-component-v1", normalization_version="vault-capture-v1"), advisory_evidence_refs=refs, current_public_score=report.get("score") if isinstance(report, Mapping) else None)
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
            }
        )
    _record_vault_sidecar_status(scan_id, detail)
    return True


def _run(scan_id: str, url: str, brand_name: str, allow_degraded_fallback: bool) -> None:
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
        canonical_snapshot = snapshot
        canonical_source_capture: dict[str, str] | None = None
        if vault_enabled:
            from src.services.evidence_vault_scan_orchestration import (
                prepare_vault_scan_after_capture,
            )
            from web.report_store import _postgres_repository

            vault_repository = _postgres_repository()
            if vault_repository is None:
                raise RuntimeError("vault_persistence_repository_unavailable")
            try:
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
                _record_vault_sidecar_status(
                    scan_id,
                    {
                        "role": "diagnostic_sidecar",
                        "state": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                vault_preparation = None
            else:
                canonical_snapshot, canonical_source_capture = (
                    _canonical_snapshot_from_persisted_vault_capture(
                        scan_id=scan_id,
                        url=url,
                        expected_snapshot=snapshot,
                        report_observation=persisted_observation,
                    )
                )

        _set_phase(scan_id, "interpret", "running")
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
        _validate_report_sv9_assessment(
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
            _validate_report_sv9_assessment(report, required=True)
        if _scan_cancelled(scan_id):
            return
        _set_phase(scan_id, "report", "running")
        if not _publish_completed_report(scan_id, report):
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
