"""Strict, non-authoritative SV9 partial-evaluation checkpoint contract."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from src.services.evidence_vault_canonical_core import build_tile_contract_registry, canonical_fingerprint, canonical_json
from src.services.evidence_vault_sv9_authoritative_relations import validate_evidence_vault_sv9_evaluation_input
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory


EVIDENCE_VAULT_SV9_EVALUATION_CHECKPOINT_VERSION = "evidence-vault-sv9-evaluation-checkpoint-v1"
_FINGERPRINT = "evidence-vault-sv9-evaluation-checkpoint-fingerprint-v1"
_SNAPSHOT_FINGERPRINT = "evidence-vault-sv9-evaluation-checkpoint-authority-snapshot-v1"
_FIELDS = frozenset("schema_version authority runtime_effect evaluation_state score_state reason_codes evaluation_input prior_authority_snapshot plan_binding healthy_workset review_partition pending_evidence non_authoritative_hints checkpoint_fingerprint".split())
_PLAN = frozenset("current_series_fingerprint canonical_plan_fingerprint candidate_series_fingerprint".split())
_PENDING = frozenset("evidence_record_id evidence_ref evidence_fingerprint evidence_id source_identity_id reason".split())
_SHA = re.compile(r"^[0-9a-f]{64}$")
_registry = build_tile_contract_registry()["tiles"]
_ORDER = {row["tile_id"]: index for index, row in enumerate(_registry)}
_COMPONENT = {row["tile_id"]: row["component_key"] for row in _registry}
_COMPONENT_ORDER = {key: index for index, key in enumerate(dict.fromkeys(_COMPONENT.values()))}
_COMPONENT_TILES = {
    component: tuple(tile for tile, owner in _COMPONENT.items() if owner == component)
    for component in _COMPONENT_ORDER
}


class EvidenceVaultSv9EvaluationCheckpointError(ValueError): pass


def build_evidence_vault_sv9_evaluation_checkpoint(*, evaluation_input: Any, prior_authority_snapshot: Any, current_series_fingerprint: Any, canonical_plan_fingerprint: Any, candidate_series_fingerprint: Any, healthy_tile_ids: Any = None, review_tile_ids: Any = None, pending_evidence: Any = None, non_authoritative_hints: Any = None, component_evaluations: Any = None, evaluated_tile_judgments: Any = None, evaluated_component_sentinels: Any = None, evaluation_state: Any = "partial") -> dict[str, Any]:
    """Build a capture-bound checkpoint; it can never be an authority or score."""
    try:
        source = _input(evaluation_input)
        plan = _plan(current_series_fingerprint, canonical_plan_fingerprint, candidate_series_fingerprint)
        return _checkpoint(
            source=source,
            prior_authority_snapshot=prior_authority_snapshot,
            plan=plan,
            healthy_tile_ids=healthy_tile_ids,
            review_tile_ids=review_tile_ids,
            pending_evidence=pending_evidence,
            non_authoritative_hints=non_authoritative_hints,
            component_evaluations=component_evaluations,
            evaluated_tile_judgments=evaluated_tile_judgments,
            evaluated_component_sentinels=evaluated_component_sentinels,
            evaluation_state=evaluation_state,
        )
    except EvidenceVaultSv9EvaluationCheckpointError: raise
    except Exception as exc: raise EvidenceVaultSv9EvaluationCheckpointError("evaluation checkpoint is invalid") from exc


def validate_evidence_vault_sv9_evaluation_checkpoint(value: Any) -> dict[str, Any]:
    """Replay the exact checkpoint from its capture-bound canonical inputs."""
    try:
        if type(value) is not dict or set(value) != _FIELDS: _fail("checkpoint fields")
        if value["schema_version"] != EVIDENCE_VAULT_SV9_EVALUATION_CHECKPOINT_VERSION or value["authority"] is not False or value["runtime_effect"] != "checkpoint_only" or value["score_state"] != "unavailable": _fail("checkpoint envelope")
        _exact(value["plan_binding"], _PLAN, "plan binding")
        _exact(value["healthy_workset"], frozenset({"state", "tile_ids", "component_evaluations", "evaluated_tile_judgments", "evaluated_component_sentinels"}), "healthy workset")
        _exact(value["review_partition"], frozenset({"state", "tile_ids", "reopened_tile_ids"}), "review partition")
        result = _checkpoint(
            source=_input(value["evaluation_input"]),
            prior_authority_snapshot=value["prior_authority_snapshot"],
            plan=_plan(**value["plan_binding"]),
            healthy_tile_ids=value["healthy_workset"]["tile_ids"],
            review_tile_ids=value["review_partition"]["tile_ids"],
            pending_evidence=value["pending_evidence"],
            non_authoritative_hints=value["non_authoritative_hints"],
            component_evaluations=value["healthy_workset"]["component_evaluations"],
            evaluated_tile_judgments=value["healthy_workset"]["evaluated_tile_judgments"],
            evaluated_component_sentinels=value["healthy_workset"]["evaluated_component_sentinels"],
            evaluation_state=value["evaluation_state"],
        )
        if canonical_json(value) != canonical_json(result): _fail("checkpoint replay mismatch")
        return result
    except EvidenceVaultSv9EvaluationCheckpointError: raise
    except Exception as exc: raise EvidenceVaultSv9EvaluationCheckpointError("evaluation checkpoint is invalid") from exc


def _checkpoint(*, source: dict[str, Any], prior_authority_snapshot: Any, plan: dict[str, str], healthy_tile_ids: Any, review_tile_ids: Any, pending_evidence: Any, non_authoritative_hints: Any, component_evaluations: Any, evaluated_tile_judgments: Any, evaluated_component_sentinels: Any, evaluation_state: Any) -> dict[str, Any]:
    healthy = _healthy(healthy_tile_ids, component_evaluations, evaluated_tile_judgments, evaluated_component_sentinels, source, plan)
    review = _review(review_tile_ids, source["reopen_tile_ids"])
    if set(healthy["tile_ids"]) & set(review["tile_ids"]): _fail("healthy and review tiles overlap")
    hints = _hints(non_authoritative_hints, source)
    pending = _pending(pending_evidence, source, {row["evidence_record_id"] for row in hints})
    state = _state(evaluation_state)
    result = {
        "schema_version": EVIDENCE_VAULT_SV9_EVALUATION_CHECKPOINT_VERSION, "authority": False,
        "runtime_effect": "checkpoint_only", "evaluation_state": state, "score_state": "unavailable", "reason_codes": _reasons(review["reopened_tile_ids"], pending, state),
        "evaluation_input": source, "prior_authority_snapshot": _snapshot(prior_authority_snapshot), "plan_binding": plan,
        "healthy_workset": healthy, "review_partition": review,
        "pending_evidence": pending, "non_authoritative_hints": hints,
    }
    result["checkpoint_fingerprint"] = canonical_fingerprint(_FINGERPRINT, result)
    return result


def _input(value: Any) -> dict[str, Any]:
    if type(value) is not dict or type(value.get("source_identity")) is not dict: _fail("evaluation input")
    try:
        source = value["source_identity"]
        return validate_evidence_vault_sv9_evaluation_input(value, source_scan_id=_text(source["source_scan_id"], "source scan"), workspace_slug=_text(source["workspace_slug"], "workspace"))
    except Exception as exc: raise EvidenceVaultSv9EvaluationCheckpointError("evaluation input is invalid") from exc


def _snapshot(value: Any) -> dict[str, Any]:
    if type(value) is not dict: _fail("authority snapshot")
    state = value.get("state")
    fields = {"state"} if state == "bootstrap_absent" else {"state", "accepted_candidate_id", "active_event_id", "current_head_event_fingerprint", "candidate_complete_record_fingerprint", "canonical_plan_fingerprint", "current_series_fingerprint"} if state == "accepted_authority" else None
    if fields is None or (set(value) != fields and set(value) != fields | {"snapshot_fingerprint"}): _fail("authority snapshot")
    result = {"state": state}
    if state == "accepted_authority":
        result |= {"accepted_candidate_id": _uuid(value["accepted_candidate_id"], "candidate id"), "active_event_id": _uuid(value["active_event_id"], "event id")}
        result |= {key: _sha(value[key], key) for key in fields - {"state", "accepted_candidate_id", "active_event_id"}}
    result["snapshot_fingerprint"] = canonical_fingerprint(_SNAPSHOT_FINGERPRINT, {key: result[key] for key in result if key != "snapshot_fingerprint"})
    if "snapshot_fingerprint" in value and value != result: _fail("authority snapshot replay")
    return result


def _plan(current_series_fingerprint: Any, canonical_plan_fingerprint: Any, candidate_series_fingerprint: Any) -> dict[str, str]:
    return {
        "current_series_fingerprint": _sha(current_series_fingerprint, "current series"),
        "canonical_plan_fingerprint": _sha(canonical_plan_fingerprint, "plan"),
        "candidate_series_fingerprint": _sha(candidate_series_fingerprint, "candidate series"),
    }


def _healthy(tiles: Any, evaluations: Any, judgments: Any, sentinels: Any, source: dict[str, Any], plan: dict[str, str]) -> dict[str, Any]:
    tile_ids = _tiles(tiles, "healthy tiles")
    evaluations, judgments = [] if evaluations is None else evaluations, [] if judgments is None else judgments
    if type(evaluations) is not list or type(judgments) is not list: _fail("healthy progress")
    if len(evaluations) != 1: _fail("exactly one component evaluation")
    capture = {"capture_id": source["source_identity"]["capture_id"], "capture_fingerprint": source["source_identity"]["capture_fingerprint"]}
    operation = {"operation_id": source["source_identity"]["operation_plan_id"], "operation_fingerprint": source["source_identity"]["operation_fingerprint"]}
    pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in source["current_identity_bindings"]}
    rows = []
    for value in judgments:
        row = memory.validate_tile_judgment(value)
        if row["tile_id"] not in tile_ids or row["authority_state"] != "pending" or row["review_state"] != "none" or row["lifecycle_state"] != "active" or row["capture_origin"] != capture or row["operation_origin"] != operation or row["series_fingerprint"] != plan["current_series_fingerprint"] or not {(item["evidence_ref"], item["evidence_fingerprint"]) for item in row["supporting_evidence"]} <= pairs: _fail("tile judgment")
        rows.append(row)
    if len({row["tile_id"] for row in rows}) != len(rows): _fail("duplicate tile judgment")
    evaluations = [evaluation._evaluation(value, True) for value in evaluations]
    sentinels = _sentinels(sentinels, tile_ids, rows, source, plan)
    sentinel_components = {row["component_key"] for row in sentinels}
    if len({row["component_key"] for row in evaluations}) != len(evaluations): _fail("component evaluation")
    if any(row["series_fingerprint"] != plan["current_series_fingerprint"] for row in evaluations): _fail("component evaluation")
    if any(row["status"] == "not_detected" and row["component_key"] not in sentinel_components for row in evaluations): _fail("component evaluation sentinel")
    if any(row["status"] == "evaluated" and not row["tile_results"] for row in evaluations): _fail("component evaluation")
    if any(row["component_key"] in sentinel_components and row["status"] != "not_detected" for row in evaluations): _fail("component evaluation sentinel")
    if sentinel_components != {row["component_key"] for row in evaluations if row["status"] == "not_detected"}: _fail("component evaluation sentinel")
    expected = {item["tile_id"]: (row["component_key"], item["assessment_state"], item["supporting_evidence"]) for row in evaluations for item in row["tile_results"]}
    if len(expected) != sum(len(row["tile_results"]) for row in evaluations) or set(expected) != {row["tile_id"] for row in rows}: _fail("evaluation partition")
    single_evaluation = evaluations[0]
    expected_tiles = set(expected) if single_evaluation["status"] == "evaluated" else set(_COMPONENT_TILES[single_evaluation["component_key"]])
    if set(tile_ids) != expected_tiles: _fail("evaluation healthy tile partition")
    for row in rows:
        component, state, evidence = expected[row["tile_id"]]
        if (row["component_key"], row["assessment_state"], row["supporting_evidence"]) != (component, state, evidence): _fail("evaluation judgment mismatch")
    return {"state": "evaluable", "tile_ids": tile_ids, "component_evaluations": sorted(evaluations, key=lambda row: _COMPONENT_ORDER[row["component_key"]]), "evaluated_tile_judgments": sorted(rows, key=lambda row: _ORDER[row["tile_id"]]), "evaluated_component_sentinels": sentinels}


def _sentinels(value: Any, tile_ids: list[str], judgments: list[dict[str, Any]], source: dict[str, Any], plan: dict[str, str]) -> list[dict[str, Any]]:
    value = [] if value is None else value
    if type(value) is not list: _fail("component sentinels")
    capture = {"capture_id": source["source_identity"]["capture_id"], "capture_fingerprint": source["source_identity"]["capture_fingerprint"]}
    operation = {"operation_id": source["source_identity"]["operation_plan_id"], "operation_fingerprint": source["source_identity"]["operation_fingerprint"]}
    rows = []
    for raw in value:
        try:
            row = planner.validate_component_not_detected_sentinel(raw)
        except Exception as exc:
            raise EvidenceVaultSv9EvaluationCheckpointError("component sentinel is invalid") from exc
        if row["authority_state"] != "pending" or row["review_state"] != "none" or row["lifecycle_state"] != "active": _fail("component sentinel state")
        if row["series_fingerprint"] != plan["current_series_fingerprint"] or row["capture_origin"] != capture or row["operation_origin"] != operation: _fail("component sentinel binding")
        capacity = set(_COMPONENT_TILES.get(row["component_key"], ()))
        if not capacity <= set(tile_ids) or capacity & {item["tile_id"] for item in judgments}: _fail("component sentinel partition")
        rows.append(row)
    if len({row["component_key"] for row in rows}) != len(rows): _fail("duplicate component sentinel")
    capacities = [set(_COMPONENT_TILES[row["component_key"]]) for row in rows]
    if any(left & right for index, left in enumerate(capacities) for right in capacities[index + 1:]): _fail("component sentinel overlap")
    return sorted(rows, key=lambda row: _COMPONENT_ORDER[row["component_key"]])


def _review(value: Any, reopened: Any) -> dict[str, Any]:
    tiles, reopen = _tiles(value, "review tiles"), _tiles(reopened, "reopened tiles")
    if not set(reopen) <= set(tiles): _fail("reopened tiles missing from review")
    return {"state": "review_required", "tile_ids": tiles, "reopened_tile_ids": reopen}


def _pending(value: Any, source: dict[str, Any], hinted: set[str]) -> list[dict[str, Any]]:
    value = [] if value is None else value
    if type(value) is not list: _fail("pending evidence")
    bindings = {row["evidence_record_id"]: row for row in source["current_identity_bindings"]}
    mapped = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in source["authoritative_relations"]}
    rows = []
    for raw in value:
        _exact(raw, _PENDING, "pending evidence")
        record = bindings.get(_uuid(raw["evidence_record_id"], "pending record"))
        row = {**record, "reason": "unmapped_evidence"} if record is not None else None
        if row is None or raw != row or row["evidence_record_id"] in hinted or (row["evidence_ref"], row["evidence_fingerprint"]) in mapped: _fail("pending evidence")
        rows.append(row)
    if len({row["evidence_record_id"] for row in rows}) != len(rows): _fail("duplicate pending evidence")
    return sorted(rows, key=lambda row: row["evidence_record_id"])


def _hints(value: Any, source: dict[str, Any]) -> list[dict[str, Any]]:
    expected = source["non_authoritative_hints"]
    if value is not None and (type(value) is not list or canonical_json(value) != canonical_json(expected)): _fail("hints")
    return expected


def _state(value: Any) -> str:
    value = _text(value, "evaluation state")
    if value not in {"partial", "provider_failure"}: _fail("evaluation state")
    return value


def _reasons(reopened: list[str], pending: list[dict[str, Any]], state: str) -> list[str]:
    return (["reopened_tiles_pending"] if reopened else []) + (["unmapped_evidence_pending"] if pending else []) + (["provider_failure"] if state == "provider_failure" else [])


def _tiles(value: Any, label: str) -> list[str]:
    value = [] if value is None else value
    if type(value) is not list: _fail(label)
    rows = [_text(item, label) for item in value]
    if len(set(rows)) != len(rows) or any(item not in _ORDER for item in rows): _fail(label)
    return sorted(rows, key=_ORDER.__getitem__)


def _exact(value: Any, fields: frozenset[str], label: str) -> None:
    if type(value) is not dict or set(value) != fields: _fail(label)


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value or value != value.strip(): _fail(label)
    return value


def _sha(value: Any, label: str) -> str:
    value = _text(value, label)
    if _SHA.fullmatch(value) is None: _fail(label)
    return value


def _uuid(value: Any, label: str) -> str:
    try:
        if type(value) is not str or str(UUID(value)) != value: _fail(label)
        return value
    except (TypeError, ValueError): _fail(label)


def _fail(label: str) -> None: raise EvidenceVaultSv9EvaluationCheckpointError(f"SV9 evaluation checkpoint {label} is invalid")
