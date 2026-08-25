"""Default-off, pending-only Vault execution of strict SV9 judgments."""

from __future__ import annotations

from typing import Any, Mapping, Protocol
from uuid import UUID

from src.sv9 import assessment_kernel as kernel
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory

# fmt: off

class EvidenceVaultSv9JudgmentShadowRepository(Protocol):
    def resolve_evidence_vault_sv9_judgment_evidence(self, source_scan_id: str, advisory_evidence_refs: list[str], **kwargs: Any) -> dict[str, Any]: ...
    def get_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, *, canonical_plan_fingerprint: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def append_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, candidate: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]: ...


class EvidenceVaultSv9JudgmentShadowError(ValueError):
    pass


_REASONS = frozenset(("invalid_input", "repository_failure", "provider_failure", "invalid_replay", "exact_same_capture_plan", "no_accepted_prior"))


def run_evidence_vault_sv9_judgment_shadow(*, repository: EvidenceVaultSv9JudgmentShadowRepository, flow: evaluation.Sv9StrictComponentFlowPort, source_scan_id: str, current_series_contract: Mapping[str, Any], advisory_evidence_refs: list[str], workspace_slug: str = "b3s", current_public_score: Any = None) -> dict[str, Any]:
    """Evaluate one detached capture; pending candidates never become memory."""
    current_score = float(current_public_score) if type(current_public_score) in {int, float} and 0 <= current_public_score <= 100 else None
    try:
        series = memory.validate_judgment_series_contract(dict(current_series_contract))
        context, records = _context(repository.resolve_evidence_vault_sv9_judgment_evidence(source_scan_id, _refs(advisory_evidence_refs), workspace_slug=workspace_slug))
        registry = [(tile["tile_id"], component["component_key"]) for component in kernel.build_sv9_tile_contract_registry()["components"] for tile in component["tiles"]]
        evidence = [{key: row[key] for key in ("evidence_ref", "evidence_fingerprint")} for row in records]
        prior_judgments = []  # Pending PR5 candidates have no accepted cross-scan occurrence authority.
        deltas = [memory.build_tile_evidence_delta_projection(tile_id=tile, component_key=component, disposition="relevant", evidence=evidence, capture_origin=context["capture_origin"], operation_origin=context["operation_origin"]) for tile, component in registry]
        plan = planner.build_incremental_plan(prior_judgments=prior_judgments, delta_projections=deltas, current_series_contract=series)
        existing = repository.get_evidence_vault_sv9_judgment_candidate(source_scan_id, canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], workspace_slug=workspace_slug)
        if existing is not None:
            return _telemetry("replayed", plan, dict(existing.get("telemetry") or {}) | {"calls_avoided": len(plan["component_workset"]), "current_score": current_score}, existing.get("assessment"), existing.get("complete_record_fingerprint"), "exact_same_capture_plan", issued=0)
    except EvidenceVaultSv9JudgmentShadowError:
        return _pending("invalid_input", current_score=current_score, plan=locals().get("plan"))
    except Exception as exc:
        return _pending("repository_failure", exc=exc, current_score=current_score, plan=locals().get("plan"))
    packets = [evaluation.build_evidence_packet(component_key=component, tiles=[{"tile_id": tile, "evidence": [{**row, "content": record["content"]} for row, record in zip(evidence, records)]} for tile in plan["tile_workset"] if evaluation._BY_TILE[tile][1] == component], capture_origin=context["capture_origin"], operation_origin=context["operation_origin"], series_fingerprint=plan["current_series_fingerprint"]) for component in plan["component_workset"]]
    result = evaluation.execute_incremental_evaluation(plan, packets, flow)
    if result["status"] != "available":
        return _pending(str(result["reason_code"] or "invalid_input"), result, current_score=current_score, plan=plan)
    candidate = _candidate(plan, result, records)
    try:
        stored, _inserted = repository.append_evidence_vault_sv9_judgment_candidate(source_scan_id, candidate, workspace_slug=workspace_slug)
    except Exception as exc:
        return _pending("repository_failure", result, exc, current_score, plan)
    return _telemetry("available", plan, dict(stored["telemetry"]) | {"current_score": current_score}, stored["assessment"], stored["complete_record_fingerprint"], "no_accepted_prior")


def _refs(value: list[str]) -> list[str]:
    if type(value) is not list or not value or any(type(item) is not str or not item or item != item.strip() for item in value) or len(set(value)) != len(value):
        raise EvidenceVaultSv9JudgmentShadowError("advisory evidence shortlist is invalid")
    return sorted(value)


def _context(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        context = {key: value[key] for key in ("capture_origin", "operation_origin")}
        evidence, keys = [], []
        for row in value["evidence"]:
            if set(row) != {"evidence_record_id", "evidence_ref", "evidence_fingerprint", "content"}:
                raise ValueError
            record_id = str(UUID(row["evidence_record_id"]))
            if record_id != row["evidence_record_id"]:
                raise ValueError
            bound = evaluation._evidence([{"evidence_ref": row["evidence_ref"], "evidence_fingerprint": row["evidence_fingerprint"], "content": row["content"]}], True)[0]
            evidence.append({"evidence_record_id": record_id, **bound}); keys.append((bound["evidence_ref"], bound["evidence_fingerprint"], record_id))
        if not evidence or keys != sorted(keys) or len({row["evidence_ref"] for row in evidence}) != len(evidence) or len({row["evidence_fingerprint"] for row in evidence}) != len(evidence):
            raise ValueError
        evaluation.build_evidence_packet(component_key="mission", tiles=[], capture_origin=context["capture_origin"], operation_origin=context["operation_origin"], series_fingerprint="0" * 64)
        return context, evidence
    except (AttributeError, KeyError, TypeError, ValueError, evaluation.IncrementalEvaluationError) as exc:
        raise EvidenceVaultSv9JudgmentShadowError("persisted evidence context is invalid") from exc


def _candidate(plan: dict[str, Any], result: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    bindings = [{"tile_id": tile, "evidence_record_id": row["evidence_record_id"], "evidence_ref": row["evidence_ref"], "evidence_fingerprint": row["evidence_fingerprint"]} for tile in plan["tile_workset"] for row in records]
    candidate = {"schema_version": "evidence-vault-sv9-judgment-candidate-v1", "plan": plan, "canonical_plan_fingerprint": plan["canonical_plan_fingerprint"], "current_series_fingerprint": plan["current_series_fingerprint"], "candidate_series_fingerprint": plan["candidate_series_fingerprint"], "component_evaluations": [row["evaluation"] for row in result["captured_calls"]], "evidence_bindings": bindings, "candidate_tile_judgments": result["candidate_tile_judgments"], "candidate_component_sentinels": result["candidate_component_sentinels"], "assessment": result["assessment"], "telemetry": {key: result[key] for key in ("call_count", "calls_avoided", "reused_tile_count", "evaluated_tile_count")}}
    candidate["evaluation_bundle_fingerprint"] = memory.canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": plan["canonical_plan_fingerprint"], "evaluations": candidate["component_evaluations"]})
    candidate["assessment_fingerprint"], candidate["score_fingerprint"] = result["assessment"]["assessment_fingerprint"], result["assessment"]["score_fingerprint"]
    candidate["complete_record_fingerprint"] = memory.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v1", candidate)
    return candidate


def _tile_diffs(plan: Mapping[str, Any] | None) -> list[dict[str, str]]:
    return [{"tile_id": row["tile_id"], "action": row["action"]} for row in plan.get("items", []) if row["action"] != "unaffected"][:80] if isinstance(plan, Mapping) else []


def _telemetry(status: str, plan: dict[str, Any], values: Mapping[str, Any], assessment: Mapping[str, Any] | None, fingerprint: Any, reason: str, issued: int | None = None) -> dict[str, Any]:
    reused, evaluated = int(values.get("reused_tile_count", 0)), int(values.get("evaluated_tile_count", 0)); reopened = sum(row["action"].startswith("reopen") for row in plan["items"])
    return {"status": status, "calls_issued": int(values.get("call_count", 0)) if issued is None else issued, "calls_avoided": int(values.get("calls_avoided", 0)), "reused_tiles": reused, "reopened_tiles": reopened, "evaluated_tiles": evaluated, "tile_diffs": _tile_diffs(plan), "current_score": values.get("current_score"), "candidate_score": assessment["sv9_score"] if assessment else None, "candidate_fingerprint": str(fingerprint), "divergence_reasons": [reason]}


def _pending(reason: str, result: Mapping[str, Any] | None = None, exc: BaseException | None = None, current_score: float | None = None, plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    reason = reason if reason in _REASONS else "invalid_input"
    values = result or {}
    output = {"status": "pending", "reason_code": reason, "calls_issued": int(values.get("call_count", 0)), "calls_avoided": int(values.get("calls_avoided", 0)), "reused_tiles": int(values.get("reused_tile_count", 0)), "reopened_tiles": 0, "evaluated_tiles": 0, "tile_diffs": _tile_diffs(plan), "current_score": current_score, "divergence_reasons": [reason]}
    if exc is not None:
        output["exception_class"] = type(exc).__name__
    return output
# fmt: on
