"""Vault-only capture persistence and operation planning.

This is the seam the web runner can call immediately after acquisition.  It
does not execute an LLM or score.  Existing production behavior is returned as
an explicit bypass unless the Vault feature boundary is enabled.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol

from src.history.capture_observation import (
    CAPTURE_OBSERVATION_SCHEMA_VERSION,
    parse_capture_observation,
)
from src.history.models import ReportImportError
from src.history.report_parser import canonical_json_hash, normalize_domain
from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_incremental_executor import EvidenceVaultIncrementalExecutorError, validate_vault_operation_result
from src.services.evidence_vault_incremental_refresh import EvidenceVaultOperationPlanError, build_vault_scan_plan, resolve_vault_scan_mode, validate_vault_scan_plan
from src.services.evidence_vault_semantic_analysis_contract import (
    current_semantic_analysis_contract,
)
from src.services.scanner_evidence_comparison import canonical_evidence_representatives
from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot


VAULT_INCREMENTAL_CAPTURE_PIPELINE_VERSION = "vault-incremental-capture-v2"


class EvidenceVaultScanOrchestrationError(ValueError):
    """A persisted Vault scan cannot be planned or resumed safely."""


class VaultExactResumeError(EvidenceVaultScanOrchestrationError):
    """A closed, public-safe outcome for the private exact-resume seam."""
    def __init__(self, reason_code: str, *, retryable: bool) -> None:
        self.reason_code, self.retryable = reason_code, retryable
        super().__init__(reason_code)
_EXACT_RESUME_ERRORS = {"busy": ("vault_exact_resume_busy", True), "invalid_action": ("vault_exact_resume_invalid_action", False), "operation_invalid": ("vault_exact_resume_operation_invalid", False), "operation_missing": ("vault_exact_resume_operation_missing", False), "superseded": ("vault_exact_resume_superseded", False), "report_invalid": ("vault_exact_resume_report_invalid", False), "execution_failed": ("vault_exact_resume_execution_failed", True)}
def vault_exact_resume_error(kind: str) -> VaultExactResumeError:
    """Map only a closed exact-resume kind to a public-safe error."""

    reason_code, retryable = _EXACT_RESUME_ERRORS.get(kind, _EXACT_RESUME_ERRORS["execution_failed"])
    return VaultExactResumeError(reason_code, retryable=retryable)


_VAULT_AUTHORITY_STAGE_REASON_CODES = {
    "operational_memory_read": "vault_authority_operational_memory_read_failed",
    "capture_observation_construction": "vault_authority_capture_observation_failed",
    "exact_operation_lookup": "vault_authority_operation_lookup_failed",
    "capture_history_read": "vault_authority_capture_history_read_failed",
    "operation_plan_construction": "vault_authority_operation_plan_failed",
    "capture_persistence": "vault_authority_capture_persist_failed",
}
_VAULT_AUTHORITY_PUBLIC_REASON_CODES = frozenset(
    _VAULT_AUTHORITY_STAGE_REASON_CODES.values()
)


class EvidenceVaultScanOrchestrationStageError(EvidenceVaultScanOrchestrationError):
    """A preparation stage failed with a safe, stable public reason code."""

    def __init__(self, stage: str, *, cause: BaseException | None = None) -> None:
        self.stage = stage if stage in _VAULT_AUTHORITY_STAGE_REASON_CODES else "unknown"
        self.reason_code = _VAULT_AUTHORITY_STAGE_REASON_CODES.get(
            self.stage,
            "vault_authority_preparation_unavailable",
        )
        super().__init__(self.reason_code)
        if cause is not None:
            self.__cause__ = cause


def public_vault_authority_reason_code(error: BaseException) -> str | None:
    """Return a whitelisted stage reason, never the original exception text."""

    if not isinstance(error, EvidenceVaultScanOrchestrationStageError):
        return None
    reason_code = error.reason_code
    return (
        reason_code
        if reason_code in _VAULT_AUTHORITY_PUBLIC_REASON_CODES
        else None
    )


def _run_preparation_stage(stage: str, operation: Any) -> Any:
    try:
        return operation()
    except EvidenceVaultScanOrchestrationStageError:
        raise
    except Exception as exc:
        raise EvidenceVaultScanOrchestrationStageError(stage) from exc


class VaultCaptureRepository(Protocol):
    def get_evidence_vault_operational_memory(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None: ...

    def get_capture_operation_plan(
        self,
        source_scan_id: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None: ...

    def list_capture_observations_for_domain(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
        limit: int = 500,
    ) -> list[dict[str, Any]]: ...

    def persist_capture_observation(
        self,
        observation: dict[str, Any],
        *,
        workspace_slug: str = "b3s",
        workspace_name: str = "B3S",
    ) -> Any: ...

    def mark_capture_analysis_completed(
        self,
        source_scan_id: str,
        *,
        operation_plan_fingerprint: str,
        analysis_result_fingerprint: str,
        workspace_slug: str = "b3s",
    ) -> dict[str, str]: ...


def prepare_vault_scan_after_capture(
    *,
    repository: VaultCaptureRepository,
    snapshot: Mapping[str, Any],
    scan_id: str,
    url: str,
    brand_name: str,
    environment: str,
    incremental_enabled: bool,
    requested_mode: str | None = None,
    workspace_slug: str = "b3s",
    accepted_evidence_tile_relations: Iterable[Mapping[str, Any]] = (),
    artifacts: Iterable[Mapping[str, Any]] = (),
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Persist capture-only history and return a non-authoritative scan plan."""

    if str(environment or "").strip().lower() != "vault" or not incremental_enabled:
        resolved = resolve_vault_scan_mode(
            environment=environment,
            incremental_enabled=incremental_enabled,
            has_canonical_memory=False,
            requested_mode=requested_mode,
        )
        return {
            **resolved,
            "capture_persisted": False,
            "operation_plan": None,
        }
    relation_rows = [
        dict(row)
        for row in accepted_evidence_tile_relations
        if isinstance(row, Mapping)
    ]
    current_memory = _run_preparation_stage(
        "operational_memory_read",
        lambda: repository.get_evidence_vault_operational_memory(
            url,
            workspace_slug=workspace_slug,
        ),
    )
    resolved = resolve_vault_scan_mode(
        environment=environment,
        incremental_enabled=incremental_enabled,
        has_canonical_memory=current_memory is not None,
        requested_mode=requested_mode,
    )
    observation = _run_preparation_stage(
        "capture_observation_construction",
        lambda: build_capture_observation_from_snapshot(
            snapshot=snapshot,
            scan_id=scan_id,
            url=url,
            brand_name=brand_name,
            mode=str(resolved["mode"]),
            artifacts=artifacts,
            observed_at=observed_at,
        ),
    )
    operation_getter = getattr(repository, "get_capture_operation_plan", None)
    persisted_operation = _run_preparation_stage(
        "exact_operation_lookup",
        lambda: (
            operation_getter(scan_id, workspace_slug=workspace_slug)
            if callable(operation_getter)
            else None
        ),
    )
    if persisted_operation is not None:
        return _resume_first_class_operation(
            repository=repository,
            operation=persisted_operation,
            attempted_observation=observation,
            resolved=resolved,
            current_memory=current_memory,
            workspace_slug=workspace_slug,
        )
    history = _run_preparation_stage(
        "capture_history_read",
        lambda: repository.list_capture_observations_for_domain(
            url,
            workspace_slug=workspace_slug,
            limit=500,
        ),
    )
    existing = next(
        (
            capture
            for capture in history
            if str(capture.get("source_scan_id") or "") == str(scan_id)
        ),
        None,
    )
    if existing is not None:
        stored_metadata = (
            dict(existing.get("metadata") or {})
            if isinstance(existing.get("metadata"), Mapping)
            else {}
        )
        observation_metadata = (
            dict(stored_metadata.get("observation") or {})
            if isinstance(stored_metadata.get("observation"), Mapping)
            else {}
        )
        stored_plan = stored_metadata.get("operation_plan") or observation_metadata.get(
            "operation_plan"
        )
        raw_observation = existing.get("raw_observation")
        if not isinstance(raw_observation, dict):
            raise EvidenceVaultScanOrchestrationError(
                "persisted scan has no exact raw observation"
            )
        if (
            canonical_json_hash(observation["capture_payload"])
            != str(existing.get("capture_hash") or "")
            or not _same_exact_capture_observation(
                observation,
                raw_observation,
            )
        ):
            raise EvidenceVaultScanOrchestrationError(
                "scan_id already belongs to a different exact capture"
            )
        if not isinstance(stored_plan, Mapping):
            raise EvidenceVaultScanOrchestrationError(
                "persisted scan has no resumable operation plan"
            )
        outcome = _run_preparation_stage(
            "capture_persistence",
            lambda: repository.persist_capture_observation(
                raw_observation,
                workspace_slug=workspace_slug,
            ),
        )
        observation = dict(raw_observation)
        analysis_status = str(stored_metadata.get("analysis_status") or "")
        if stored_plan.get("mode") != resolved.get("mode"):
            raise EvidenceVaultScanOrchestrationError(
                "persisted scan mode conflicts with this retry"
            )
        current_version = (
            str(current_memory["canonical_memory_version"])
            if current_memory is not None
            else None
        )
        if stored_plan.get("canonical_memory_version") != current_version:
            raise EvidenceVaultScanOrchestrationError(
                "persisted operation plan has a superseded canonical parent"
            )
        if analysis_status == "completed":
            result_fingerprint = str(
                stored_metadata.get("analysis_result_fingerprint") or ""
            )
            if len(result_fingerprint) != 64 or any(
                character not in "0123456789abcdef"
                for character in result_fingerprint
            ):
                raise EvidenceVaultScanOrchestrationError(
                    "completed plan has no valid result fingerprint"
                )
            return {
                **resolved,
                "mode": stored_plan["mode"],
                "capture_persisted": True,
                "capture_import": (
                    outcome.to_dict()
                    if hasattr(outcome, "to_dict")
                    else dict(outcome)
                ),
                "report_observation": dict(raw_observation),
                "operation_plan": None,
                "resume": {
                    "analysis_status": "completed",
                    "operation_plan_fingerprint": stored_plan[
                        "operation_plan_fingerprint"
                    ],
                    "analysis_result_fingerprint": result_fingerprint,
                    "semantic_work_completed": bool(
                        dict(stored_plan.get("operations") or {}).get(
                            "llm_required"
                        )
                    ),
                    "work_required": False,
                },
            }
        plan = dict(stored_plan)
    else:
        def build_plan() -> dict[str, Any]:
            previous_rows = history[0]["evidence_records"] if history else []
            known_rows = [
                row
                for capture in history
                if _capture_analysis_completed(capture)
                for row in capture.get("evidence_records") or []
                if isinstance(row, dict)
            ]
            semantic_claims = _semantic_analysis_claimed_fingerprints(history)
            return build_vault_scan_plan(
                brand_identity=(
                    str(current_memory["brand_identity"])
                    if current_memory is not None
                    else normalize_domain(str(observation["url"]))
                ),
                subject_url=str(observation["url"]),
                mode=str(resolved["mode"]),
                current_evidence_records=observation["evidence_records"],
                previous_capture_evidence_records=previous_rows,
                known_evidence_records=known_rows,
                semantic_analysis_claimed_fingerprints=semantic_claims,
                accepted_evidence_tile_relations=relation_rows,
                accepted_tiles=(
                    list(
                        (current_memory.get("content") or {}).get("accepted_tiles")
                        or []
                    )
                    if current_memory is not None
                    else []
                ),
                canonical_memory_version=(
                    str(current_memory["canonical_memory_version"])
                    if current_memory is not None
                    else None
                ),
            )

        plan = _run_preparation_stage("operation_plan_construction", build_plan)
        work_required = bool(
            plan["operations"]["llm_required"]
            or plan["operations"]["create_candidate_packet"]
            or plan["operations"]["create_diagnostic_report"]
        )
        observation = {
            **observation,
            "metadata": {
                **dict(observation["metadata"]),
                "operation_plan": plan,
                "operation_plan_fingerprint": plan[
                    "operation_plan_fingerprint"
                ],
                "analysis_status": "pending" if work_required else "not_required",
            },
        }
        outcome = _run_preparation_stage(
            "capture_persistence",
            lambda: repository.persist_capture_observation(
                observation,
                workspace_slug=workspace_slug,
            ),
        )
    return {
        **resolved,
        "capture_persisted": True,
        "capture_import": (
            outcome.to_dict() if hasattr(outcome, "to_dict") else dict(outcome)
        ),
        "report_observation": dict(observation),
        "operation_plan": plan,
    }


def prepare_vault_exact_resume(*, repository: VaultCaptureRepository, action: Mapping[str, Any], scan_id: str, workspace_slug: str = "b3s") -> dict[str, Any]:
    """Recover one frozen operation without recapturing or replanning it."""
    scan_id, request = str(scan_id or "").strip(), {"operation": "exact_resume"}
    if not (scan_id and isinstance(action, Mapping) and isinstance(action.get("action_id"), str) and action["action_id"].strip() and action.get("scan_id") == scan_id and action.get("state") == "running" and action.get("request_payload") == request and action.get("status_payload") == {"state": "running", "phase": "running"} and action.get("request_fingerprint") == canonical_json_hash(request)):
        raise vault_exact_resume_error("invalid_action")
    try: operation = repository.get_capture_operation_plan(scan_id, workspace_slug=workspace_slug)
    except (EvidenceVaultScanOrchestrationError, ReportImportError): raise vault_exact_resume_error("operation_invalid") from None
    if not isinstance(operation, Mapping): raise vault_exact_resume_error("operation_missing" if operation is None else "operation_invalid")
    raw, plan = operation.get("raw_observation"), operation.get("plan")
    try: parsed = parse_capture_observation(dict(raw)); validate_vault_scan_plan(plan)
    except (ReportImportError, EvidenceVaultOperationPlanError, TypeError, ValueError): raise vault_exact_resume_error("operation_invalid") from None
    plan = dict(plan)
    if any((operation.get("source_scan_id") != scan_id, parsed.source_scan_id != scan_id, operation.get("observation_hash") != parsed.observation_hash, operation.get("capture_hash") != parsed.capture_hash, operation.get("operation_plan_fingerprint") != plan["operation_plan_fingerprint"], operation.get("canonical_memory_version") != plan["canonical_memory_version"], normalize_domain(str(plan["subject_url"])) != parsed.canonical_domain, plan["brand_identity"] != parsed.canonical_domain, operation.get("brand_identity") != parsed.canonical_domain)):
        raise vault_exact_resume_error("operation_invalid")
    representatives = canonical_evidence_representatives(parsed.evidence_records, subject_url=plan["subject_url"]); planned = sorted(plan["operations"]["classify_evidence_fingerprints"])
    if plan["semantic_context"]["evidence_fingerprints"] != sorted(representatives) or not set(planned).issubset(representatives): raise vault_exact_resume_error("operation_invalid")
    status = str(operation.get("status") or "")
    if status == "superseded": raise vault_exact_resume_error("superseded")
    active = status in {"claimed", "running"}
    if status not in {"pending", "not_required", "claimed", "running", "result_persisted", "completed", "failed_retryable"}: raise vault_exact_resume_error("operation_invalid")
    if active and operation.get("lease_active") is True: raise vault_exact_resume_error("busy")
    if active and operation.get("lease_active") is not False: raise vault_exact_resume_error("operation_invalid")
    if not active and operation.get("lease_active") is not False and operation.get("lease_active") is not None: raise vault_exact_resume_error("operation_invalid")
    has_result = any(operation.get(field) is not None for field in ("result_payload", "result_fingerprint", "candidate_packet_fingerprint"))
    if status in {"result_persisted", "completed"}:
        result = operation.get("result_payload")
        try: validate_vault_operation_result(result); result_fingerprint = canonical_fingerprint("evidence-vault-operation-result-v1", result)
        except (EvidenceVaultIncrementalExecutorError, TypeError, ValueError): raise vault_exact_resume_error("operation_invalid") from None
        if any((operation.get("result_fingerprint") != result_fingerprint, result.get("operation_plan_fingerprint") != plan["operation_plan_fingerprint"], result.get("observation_hash") != parsed.observation_hash, result.get("canonical_memory_version") != plan["canonical_memory_version"], operation.get("candidate_packet_fingerprint") != result.get("candidate_packet_fingerprint"))): raise vault_exact_resume_error("operation_invalid")
        if result.get("output_kind") == "candidate_overlay" and result.get("selected_evidence_fingerprints") != planned: raise vault_exact_resume_error("operation_invalid")
    elif has_result: raise vault_exact_resume_error("operation_invalid")
    return {"url": parsed.canonical_url, "brand_name": parsed.brand_name, "canonical_snapshot": deepcopy(parsed.capture_payload), "report_binding": {"source_scan_id": parsed.source_scan_id, "source_run_id": parsed.source_run_id, "observation_hash": parsed.observation_hash, "capture_hash": parsed.capture_hash, "canonical_domain": parsed.canonical_domain}, "preparation": {"report_observation": deepcopy(raw), "operation_plan": plan if status != "completed" else None, "resume": {"analysis_status": status, "operation_plan_fingerprint": plan["operation_plan_fingerprint"], "analysis_result_fingerprint": operation.get("result_fingerprint"), "semantic_work_completed": status in {"result_persisted", "completed"} and bool(plan["operations"]["llm_required"]), "materialization_required": status == "result_persisted"}}}


def _trusted_capture_binding_matches(
    attempted: Mapping[str, Any],
    persisted: Mapping[str, Any],
) -> bool:
    if str(persisted.get("pipeline_version") or "") != (
        "evidence-vault-trusted-acquisition-v1"
    ):
        return False
    capture_payload = attempted.get("capture_payload")
    if not isinstance(capture_payload, Mapping):
        return False
    source_capture = capture_payload.get("source_capture")
    if not isinstance(source_capture, Mapping):
        return False
    source_scan_id = str(persisted.get("source_scan_id") or "")
    # The acquisition worker owns the raw observation. The web runner receives
    # only these two hashes, so it must resume that observation rather than
    # treating its derived pre-analysis wrapper as a second capture.
    return bool(
        source_scan_id
        and str(attempted.get("source_scan_id") or "") == source_scan_id
        and str(source_capture.get("source_scan_id") or "") == source_scan_id
        and normalize_domain(str(attempted.get("url") or ""))
        == normalize_domain(str(persisted.get("url") or ""))
        and str(source_capture.get("observation_hash") or "")
        == canonical_json_hash(dict(persisted))
        and str(source_capture.get("capture_hash") or "")
        == canonical_json_hash(persisted.get("capture_payload") or {})
    )


def _same_exact_capture_observation(
    attempted: Mapping[str, Any],
    persisted: Mapping[str, Any],
) -> bool:
    if _trusted_capture_binding_matches(attempted, persisted):
        return True
    fields = (
        "schema_version",
        "source_scan_id",
        "source_run_id",
        "brand_name",
        "url",
        "acquisition_state",
        "acquisition_summary",
        "limitations",
        "capture_payload",
        "evidence_records",
        "acquisition_attempts",
    )
    # v1 runner captures did not freeze screenshot/visual artifacts. Preserve
    # their resumability while v2 makes artifacts part of exact capture identity.
    if str(persisted.get("pipeline_version") or "") != (
        "vault-incremental-capture-v1"
    ):
        fields = (*fields, "artifacts")
    return canonical_json_hash(
        {field: attempted.get(field) for field in fields}
    ) == canonical_json_hash(
        {field: persisted.get(field) for field in fields}
    )



def _resume_first_class_operation(
    *,
    repository: VaultCaptureRepository,
    operation: Mapping[str, Any],
    attempted_observation: Mapping[str, Any],
    resolved: Mapping[str, Any],
    current_memory: Mapping[str, Any] | None,
    workspace_slug: str,
) -> dict[str, Any]:
    raw = operation.get("raw_observation")
    plan = operation.get("plan")
    if not isinstance(raw, Mapping) or not isinstance(plan, Mapping):
        raise EvidenceVaultScanOrchestrationError(
            "persisted operation has no exact capture and plan"
        )
    if not _same_exact_capture_observation(
        attempted_observation,
        raw,
    ):
        raise EvidenceVaultScanOrchestrationError(
            "scan_id already belongs to a different exact capture"
        )
    if plan.get("mode") != resolved.get("mode"):
        raise EvidenceVaultScanOrchestrationError(
            "persisted scan mode conflicts with this retry"
        )
    status = str(operation.get("status") or "")
    current_version = (
        str(current_memory["canonical_memory_version"])
        if current_memory is not None
        else None
    )
    if (
        status != "superseded"
        and plan.get("canonical_memory_version") != current_version
    ):
        raise EvidenceVaultScanOrchestrationError(
            "persisted operation plan has a superseded canonical parent"
        )
    outcome = _run_preparation_stage(
        "capture_persistence",
        lambda: repository.persist_capture_observation(
            dict(raw),
            workspace_slug=workspace_slug,
        ),
    )
    lease_reclaimable = bool(
        status in {"claimed", "running"}
        and operation.get("lease_active") is False
    )
    execution_required = status in {
        "pending",
        "not_required",
        "failed_retryable",
    } or lease_reclaimable
    semantic_work_required = bool(
        execution_required
        and dict(plan.get("operations") or {}).get("llm_required")
    )
    semantic_work_completed = bool(
        status in {"completed", "result_persisted"}
        and dict(plan.get("operations") or {}).get("llm_required")
    )
    materialization_required = status == "result_persisted"
    operation_plan = (
        dict(plan)
        if execution_required or materialization_required
        else None
    )
    return {
        **dict(resolved),
        "mode": plan["mode"],
        "capture_persisted": True,
        "capture_import": (
            outcome.to_dict() if hasattr(outcome, "to_dict") else dict(outcome)
        ),
        "report_observation": dict(raw),
        "operation_plan": operation_plan,
        "resume": {
            "analysis_status": status,
            "operation_plan_fingerprint": plan[
                "operation_plan_fingerprint"
            ],
            "analysis_result_fingerprint": operation.get(
                "result_fingerprint"
            ),
            "execution_required": execution_required,
            "semantic_work_required": semantic_work_required,
            "semantic_work_completed": semantic_work_completed,
            "materialization_required": materialization_required,
            "work_required": execution_required
            or materialization_required,
        },
    }


def bind_vault_report_to_capture_observation(
    report: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a later full report to one exact persisted acquisition observation."""

    parsed = parse_capture_observation(dict(observation))
    bound = deepcopy(dict(report))
    raw = (
        dict(bound.get("raw") or {})
        if isinstance(bound.get("raw"), Mapping)
        else {}
    )
    raw["source_capture"] = {
        "source_scan_id": parsed.source_scan_id,
        "observation_hash": parsed.observation_hash,
        "capture_hash": parsed.capture_hash,
    }
    bound["raw"] = raw
    return bound


def build_capture_observation_from_snapshot(
    *,
    snapshot: Mapping[str, Any],
    scan_id: str,
    url: str,
    brand_name: str,
    mode: str,
    artifacts: Iterable[Mapping[str, Any]] = (),
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Freeze exact acquisition plus its deterministic normalized evidence."""

    snapshot_payload = dict(snapshot)
    pack = build_evidence_pack_from_snapshot(snapshot_payload)
    evidence = [row.to_dict() for row in pack.evidence]
    gate = (
        dict(snapshot_payload.get("acquisition_gate") or {})
        if isinstance(snapshot_payload.get("acquisition_gate"), Mapping)
        else {}
    )
    acquisition_steps = (
        dict(snapshot_payload.get("acquisition_steps") or {})
        if isinstance(snapshot_payload.get("acquisition_steps"), Mapping)
        else {}
    )
    attempts = []
    for provider, raw in sorted(acquisition_steps.items()):
        step = dict(raw) if isinstance(raw, Mapping) else {}
        details = dict(step.get("details") or {}) if isinstance(step.get("details"), Mapping) else {}
        attempts.append(
            {
                "provider": str(provider),
                "intent": str(details.get("intent") or ""),
                "status": str(step.get("status") or "unknown"),
                "detail": str(details.get("reason") or step.get("error") or ""),
                "raw": step,
            }
        )
    timestamp = _timestamp(
        observed_at or _snapshot_observed_at(snapshot_payload)
    )
    limitations = list(pack.limitations)
    for item in [*(gate.get("issues") or []), *(gate.get("warnings") or [])]:
        if isinstance(item, Mapping) and str(item.get("code") or "").strip():
            limitations.append(f"acquisition_gate:{item['code']}")
    limitations = list(dict.fromkeys(str(item) for item in limitations if str(item)))
    return {
        "schema_version": CAPTURE_OBSERVATION_SCHEMA_VERSION,
        "source_scan_id": str(scan_id),
        "source_run_id": (
            str((snapshot_payload.get("run") or {}).get("id") or "")
            if isinstance(snapshot_payload.get("run"), Mapping)
            else ""
        ),
        "brand_name": str(brand_name),
        "url": str(url),
        "observed_at": timestamp,
        "recorded_at": timestamp,
        "pipeline_version": VAULT_INCREMENTAL_CAPTURE_PIPELINE_VERSION,
        "acquisition_state": str(gate.get("state") or "unknown"),
        "acquisition_summary": {
            "evidence_record_count": len(evidence),
            "source_count": len(
                {
                    str(row.get("source") or "")
                    for row in evidence
                    if str(row.get("source") or "")
                }
            ),
            "acquisition_step_count": len(attempts),
        },
        "limitations": limitations,
        "capture_payload": snapshot_payload,
        "evidence_records": evidence,
        "acquisition_attempts": attempts,
        "artifacts": [dict(row) for row in artifacts if isinstance(row, Mapping)],
        "metadata": {
            "mode": str(mode),
            "authority": False,
            "runtime_effect": False,
            "strategic_report_created": False,
        },
    }


def _snapshot_observed_at(snapshot: Mapping[str, Any]) -> str | None:
    run = snapshot.get("run")
    if not isinstance(run, Mapping):
        return None
    explicit = run.get("observed_at") or run.get("captured_at")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    run_id = run.get("id")
    if (
        isinstance(run_id, (int, float))
        and not isinstance(run_id, bool)
        and float(run_id) >= 946684800
    ):
        try:
            return datetime.fromtimestamp(
                float(run_id),
                tz=timezone.utc,
            ).isoformat()
        except (OSError, OverflowError, ValueError):
            # Verified-raw snapshots use an opaque positive integer identity,
            # not necessarily a Unix timestamp.
            return None
    return None


def _semantic_analysis_claimed_fingerprints(
    history: Iterable[Mapping[str, Any]],
) -> list[str]:
    current_fingerprint = current_semantic_analysis_contract()[
        "semantic_analysis_contract_fingerprint"
    ]
    claims: set[str] = set()
    for capture in history:
        metadata = (
            dict(capture.get("metadata") or {})
            if isinstance(capture.get("metadata"), Mapping)
            else {}
        )
        if metadata.get("analysis_status") == "superseded":
            continue
        observation_metadata = (
            dict(metadata.get("observation") or {})
            if isinstance(metadata.get("observation"), Mapping)
            else {}
        )
        plan = metadata.get("operation_plan") or observation_metadata.get(
            "operation_plan"
        )
        if not isinstance(plan, Mapping):
            continue
        context = plan.get("semantic_context")
        contract = (
            context.get("semantic_analysis_contract")
            if isinstance(context, Mapping)
            else None
        )
        if (
            not isinstance(contract, Mapping)
            or contract.get("semantic_analysis_contract_fingerprint")
            != current_fingerprint
        ):
            continue
        operations = plan.get("operations")
        workset = (
            operations.get("classify_evidence_fingerprints")
            if isinstance(operations, Mapping)
            else None
        )
        if not isinstance(workset, list):
            continue
        claims.update(
            item
            for item in workset
            if isinstance(item, str)
            and len(item) == 64
            and all(character in "0123456789abcdef" for character in item)
        )
    return sorted(claims)


def _capture_analysis_completed(capture: Mapping[str, Any]) -> bool:
    metadata = (
        dict(capture.get("metadata") or {})
        if isinstance(capture.get("metadata"), Mapping)
        else {}
    )
    if metadata.get("analysis_status") in {"completed", "not_required"}:
        return True
    return bool(
        metadata.get("report_hash")
        or metadata.get("imported_from") == "b3s-report-json"
        or metadata.get("persisted_as") == "capture_with_report"
    )


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


__all__ = [
    "EvidenceVaultScanOrchestrationError",
    "EvidenceVaultScanOrchestrationStageError",
    "VAULT_INCREMENTAL_CAPTURE_PIPELINE_VERSION",
    "VaultExactResumeError",
    "bind_vault_report_to_capture_observation",
    "build_capture_observation_from_snapshot",
    "prepare_vault_exact_resume",
    "prepare_vault_scan_after_capture",
    "public_vault_authority_reason_code",
    "vault_exact_resume_error",
]
