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
from src.history.report_parser import canonical_json_hash, normalize_domain
from src.services.evidence_vault_incremental_refresh import (
    build_vault_scan_plan,
    resolve_vault_scan_mode,
)
from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot


VAULT_INCREMENTAL_CAPTURE_PIPELINE_VERSION = "vault-incremental-capture-v1"


class EvidenceVaultScanOrchestrationError(ValueError):
    """A persisted Vault scan cannot be planned or resumed safely."""


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
    current_memory = repository.get_evidence_vault_operational_memory(
        url,
        workspace_slug=workspace_slug,
    )
    resolved = resolve_vault_scan_mode(
        environment=environment,
        incremental_enabled=incremental_enabled,
        has_canonical_memory=current_memory is not None,
        requested_mode=requested_mode,
    )
    observation = build_capture_observation_from_snapshot(
        snapshot=snapshot,
        scan_id=scan_id,
        url=url,
        brand_name=brand_name,
        mode=str(resolved["mode"]),
        artifacts=artifacts,
        observed_at=observed_at,
    )
    operation_getter = getattr(repository, "get_capture_operation_plan", None)
    persisted_operation = (
        operation_getter(scan_id, workspace_slug=workspace_slug)
        if callable(operation_getter)
        else None
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
    history = repository.list_capture_observations_for_domain(
        url,
        workspace_slug=workspace_slug,
        limit=500,
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
            or canonical_json_hash(observation["evidence_records"])
            != canonical_json_hash(
                raw_observation.get("evidence_records") or []
            )
        ):
            raise EvidenceVaultScanOrchestrationError(
                "scan_id already belongs to a different exact capture"
            )
        if not isinstance(stored_plan, Mapping):
            raise EvidenceVaultScanOrchestrationError(
                "persisted scan has no resumable operation plan"
            )
        outcome = repository.persist_capture_observation(
            raw_observation,
            workspace_slug=workspace_slug,
        )
        analysis_status = str(stored_metadata.get("analysis_status") or "")
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
                "operation_plan": None,
                "resume": {
                    "analysis_status": "completed",
                    "operation_plan_fingerprint": stored_plan[
                        "operation_plan_fingerprint"
                    ],
                    "analysis_result_fingerprint": result_fingerprint,
                    "work_required": False,
                },
            }
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
        plan = dict(stored_plan)
    else:
        previous_rows = history[0]["evidence_records"] if history else []
        known_rows = [
            row
            for capture in history
            if _capture_analysis_completed(capture)
            for row in capture.get("evidence_records") or []
            if isinstance(row, dict)
        ]
        plan = build_vault_scan_plan(
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
            accepted_evidence_tile_relations=accepted_evidence_tile_relations,
            canonical_memory_version=(
                str(current_memory["canonical_memory_version"])
                if current_memory is not None
                else None
            ),
        )
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
        outcome = repository.persist_capture_observation(
            observation,
            workspace_slug=workspace_slug,
        )
    return {
        **resolved,
        "capture_persisted": True,
        "capture_import": (
            outcome.to_dict() if hasattr(outcome, "to_dict") else dict(outcome)
        ),
        "operation_plan": plan,
    }


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
    if (
        canonical_json_hash(attempted_observation["capture_payload"])
        != canonical_json_hash(raw.get("capture_payload") or {})
        or canonical_json_hash(attempted_observation["evidence_records"])
        != canonical_json_hash(raw.get("evidence_records") or [])
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
    outcome = repository.persist_capture_observation(
        dict(raw),
        workspace_slug=workspace_slug,
    )
    execution_required = status in {
        "pending",
        "not_required",
        "failed_retryable",
    }
    semantic_work_required = bool(
        execution_required
        and dict(plan.get("operations") or {}).get("llm_required")
    )
    materialization_required = status == "result_persisted"
    operation_plan = dict(plan) if execution_required else None
    return {
        **dict(resolved),
        "mode": plan["mode"],
        "capture_persisted": True,
        "capture_import": (
            outcome.to_dict() if hasattr(outcome, "to_dict") else dict(outcome)
        ),
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
        return datetime.fromtimestamp(
            float(run_id),
            tz=timezone.utc,
        ).isoformat()
    return None


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
    "VAULT_INCREMENTAL_CAPTURE_PIPELINE_VERSION",
    "bind_vault_report_to_capture_observation",
    "build_capture_observation_from_snapshot",
    "prepare_vault_scan_after_capture",
]
