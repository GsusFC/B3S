"""Repository-backed, pending-only SV9 authority evaluation."""

from __future__ import annotations
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID
from src.services import evidence_vault_sv9_judgment_delta as delta
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory


# fmt: off
class EvidenceVaultSv9AuthorityEvaluationRepository(Protocol):
    def get_evidence_vault_sv9_judgment_authority(self, domain_or_url: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def load_evidence_vault_sv9_judgment_context(self, source_scan_id: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def resolve_evidence_vault_sv9_judgment_evidence(self, source_scan_id: str, advisory_evidence_refs: list[str], **kwargs: Any) -> dict[str, Any]: ...
    def get_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, *, canonical_plan_fingerprint: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def append_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, candidate: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]: ...
class EvidenceVaultSv9AuthorityEvaluationError(ValueError): pass
def run_evidence_vault_sv9_authority_evaluation(*, repository: EvidenceVaultSv9AuthorityEvaluationRepository, flow: evaluation.Sv9StrictComponentFlowPort, domain_or_url: str, source_scan_id: str, current_evidence: list[Mapping[str, Any]], authoritative_relations: list[Mapping[str, Any]], current_series_contract: Mapping[str, Any], workspace_slug: str = "b3s", trusted_irrelevant_evidence: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Evaluate exact trusted deltas without adopting, reopening, or superseding authority."""
    try:
        _text(domain_or_url); _text(source_scan_id); _text(workspace_slug)
        current = delta.build_evidence_identity_set(current_evidence); trusted = _trusted(trusted_irrelevant_evidence, current)
        authority = repository.get_evidence_vault_sv9_judgment_authority(domain_or_url, workspace_slug=workspace_slug)
        prior, sentinels, authority_ids, overlay = _authority(authority)
        context, records = _source(repository, source_scan_id, current, workspace_slug); relations = _relations(authoritative_relations, context, trusted)
        signed = delta.build_evidence_vault_sv9_judgment_delta(current_evidence=current, prior_judgments=prior, prior_component_sentinels=sentinels, authoritative_relations=relations, current_series_contract=dict(current_series_contract))
    except EvidenceVaultSv9AuthorityEvaluationError: return _outcome("no_new_score", reasons=["invalid_input"])
    except Exception: return _outcome("no_new_score", reasons=["repository_failure"])
    plan = signed["plan"]; review, ignored, unmapped = _review(signed, bool(authority), overlay, trusted)
    if unmapped: return _outcome("review_required", plan, authority_ids, review, ignored, unmapped)
    if _unresolved(plan, prior, sentinels): return _outcome("review_required", plan, authority_ids, review + ["incomplete_review_partition"], ignored, unmapped)
    if not plan["component_workset"]: return _outcome("review_required" if review else "no_new_score", plan, authority_ids, review or ["exact_reuse"], ignored, unmapped)
    try: existing = repository.get_evidence_vault_sv9_judgment_candidate(source_scan_id, canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], workspace_slug=workspace_slug)
    except Exception: return _outcome("no_new_score", plan, authority_ids, ["repository_failure"], ignored, unmapped)
    if existing is not None: return _outcome("review_required" if review else "no_new_score", plan, authority_ids, review or ["candidate_already_present"], ignored, unmapped)
    try: packets, bindings = _packets(plan, records, context)
    except EvidenceVaultSv9AuthorityEvaluationError: return _outcome("no_new_score", plan, authority_ids, ["invalid_input"], ignored, unmapped)
    result = evaluation.execute_incremental_evaluation(plan, packets, flow)
    if result["status"] != "available": return _outcome("no_new_score", plan, authority_ids, [str(result.get("reason_code") or "evaluation_incomplete")], ignored, unmapped, result)
    if not _complete(result, plan): return _outcome("no_new_score", plan, authority_ids, ["incomplete_candidate"], ignored, unmapped, result)
    candidate = _candidate(plan, result, bindings)
    if not _replays(candidate, candidate, candidate, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, result)
    try:
        stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(source_scan_id, candidate, workspace_slug=workspace_slug)
        reloaded = repository.get_evidence_vault_sv9_judgment_candidate(source_scan_id, canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], workspace_slug=workspace_slug)
    except Exception: return _outcome("no_new_score", plan, authority_ids, ["repository_failure"], ignored, unmapped, result)
    if not inserted: return _outcome("review_required" if review else "no_new_score", plan, authority_ids, review or ["candidate_already_present"], ignored, unmapped, result)
    if not _replays(stored, reloaded, candidate, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, result)
    return _outcome("review_required" if review else "candidate_available", plan, authority_ids, review, ignored, unmapped, result, reloaded)
def _text(value: Any) -> str:
    if type(value) is not str or not value.strip(): raise EvidenceVaultSv9AuthorityEvaluationError("text is invalid")
    return value.strip()
def _accepted(value: Mapping[str, Any], sentinel=False) -> dict[str, Any]:
    omit = {"schema_version", "series_fingerprint", "canonical_component_sentinel_fingerprint" if sentinel else "canonical_judgment_fingerprint", "authority_state"}
    return (planner.build_component_not_detected_sentinel if sentinel else memory.build_tile_judgment)(**({key: row for key, row in value.items() if key not in omit} | {"authority_state": "accepted"}))
def _capacity(tiles: Sequence[Mapping[str, Any]], sentinels: Sequence[Mapping[str, Any]]) -> int: return len(tiles) + sum(len(planner._COMPONENT_TILES[row["component_key"]]) for row in sentinels)
def _authority(value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str] | None, bool]:
    if value is None: return [], [], None, False
    try:
        if type(value) is not dict or value["authority"] is not True or set(value["accepted_partition"]) != {"candidate_tile_judgments", "candidate_component_sentinels"}: raise ValueError
        part, candidate = value["accepted_partition"], value["accepted_candidate"]
        tiles, sentinels = [_accepted(row) for row in part["candidate_tile_judgments"]], [_accepted(row, True) for row in part["candidate_component_sentinels"]]
        if _capacity(tiles, sentinels) != len(planner._REGISTRY): raise ValueError
        return tiles, sentinels, {"accepted_candidate_id": str(UUID(candidate["id"])), "active_event_id": str(UUID(value["active_authority_event"]["event_id"])), "current_head_event_fingerprint": evaluation._sha(value["current_head"]["event_fingerprint"])}, value.get("reopen_review_overlay") is not None
    except (AttributeError, KeyError, TypeError, ValueError, memory.JudgmentMemoryContractError, planner.IncrementalPlannerError) as exc: raise EvidenceVaultSv9AuthorityEvaluationError("authority is invalid") from exc
def _trusted(value: Sequence[Mapping[str, Any]], current: Mapping[str, Any]) -> set[tuple[str, str]]:
    if type(value) not in {list, tuple}: raise EvidenceVaultSv9AuthorityEvaluationError("trusted irrelevant evidence is invalid")
    pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in delta.build_evidence_identity_set(list(value))["evidence"]}
    if not pairs <= {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}: raise EvidenceVaultSv9AuthorityEvaluationError("trusted irrelevant evidence is not current")
    return pairs
def _source(repository: EvidenceVaultSv9AuthorityEvaluationRepository, scan: str, current: Mapping[str, Any], workspace: str) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    try:
        loaded = repository.load_evidence_vault_sv9_judgment_context(scan, workspace_slug=workspace)
        context = {"capture_origin": {"capture_id": loaded["capture_id"], "capture_fingerprint": loaded["capture_fingerprint"]}, "operation_origin": {"operation_id": loaded["operation_plan_id"], "operation_fingerprint": loaded["operation_fingerprint"]}}
        evaluation.build_evidence_packet(component_key=next(iter(planner._COMPONENT_TILES)), tiles=[], **context, series_fingerprint="0" * 64)
        if not current["evidence"]: return context, {}
        resolved = repository.resolve_evidence_vault_sv9_judgment_evidence(scan, [row["evidence_ref"] for row in current["evidence"]], workspace_slug=workspace)
        if {key: resolved[key] for key in context} != context: raise ValueError
        records = {}
        for raw in resolved["evidence"]:
            if set(raw) != {"evidence_record_id", "evidence_ref", "evidence_fingerprint", "content"}: raise ValueError
            identifier = str(UUID(raw["evidence_record_id"])); evidence = evaluation._evidence([{key: raw[key] for key in ("evidence_ref", "evidence_fingerprint", "content")}], True)[0]; pair = evidence["evidence_ref"], evidence["evidence_fingerprint"]
            if identifier != raw["evidence_record_id"] or pair in records: raise ValueError
            records[pair] = {"evidence_record_id": identifier, **evidence}
        if set(records) != {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}: raise ValueError
        return context, records
    except (AttributeError, KeyError, TypeError, ValueError, evaluation.IncrementalEvaluationError) as exc: raise EvidenceVaultSv9AuthorityEvaluationError("persisted evidence is invalid") from exc
def _relations(value: Any, context: Mapping[str, Any], trusted: set[tuple[str, str]]) -> list[dict[str, Any]]:
    try:
        if type(value) is not list: raise ValueError
        rows = [delta.build_authoritative_evidence_tile_relation(_raw=row, _signed=True) for row in value]
        if any(row["capture_origin"] != context["capture_origin"] or row["operation_origin"] != context["operation_origin"] or (row["evidence_ref"], row["evidence_fingerprint"]) in trusted for row in rows): raise ValueError
        return rows
    except (AttributeError, TypeError, ValueError, delta.EvidenceVaultSV9JudgmentDeltaError) as exc: raise EvidenceVaultSv9AuthorityEvaluationError("relations are invalid") from exc
def _review(signed: Mapping[str, Any], has_authority: bool, overlay: bool, trusted: set[tuple[str, str]]) -> tuple[list[str], int, int]:
    plan = signed["plan"]; unmapped = [row for row in signed["unmapped_evidence"] if (row["evidence_ref"], row["evidence_fingerprint"]) not in trusted]; codes = ["review_set"] if plan["review_set"] else []
    if has_authority and signed["coverage_loss"]: codes.append("coverage_loss")
    if unmapped: codes.append("unmapped_evidence")
    if has_authority and any(row["action"] == "reopen_contract_change" for row in plan["items"]): codes.append("series_rollover")
    if overlay: codes.append("active_review_overlay")
    return codes, len(signed["unmapped_evidence"]) - len(unmapped), len(unmapped)
def _unresolved(plan: Mapping[str, Any], prior: Sequence[Mapping[str, Any]], sentinels: Sequence[Mapping[str, Any]]) -> bool:
    covered = {row["tile_id"] for row in prior}
    for row in sentinels: covered.update(planner._COMPONENT_TILES[row["component_key"]])
    return any(tile not in covered for tile in plan["review_set"])
def _packets(plan: Mapping[str, Any], records: Mapping[tuple[str, str], Mapping[str, Any]], context: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    projections = {row["tile_id"]: row for row in plan["delta_projections"]}; packets, bindings = [], []
    for component in plan["component_workset"]:
        tiles = []
        for tile in [tile for tile in plan["tile_workset"] if evaluation._BY_TILE[tile][1] == component]:
            evidence = []
            for identity in projections.get(tile, {}).get("evidence", []):
                record = records.get((identity["evidence_ref"], identity["evidence_fingerprint"]))
                if record is None: raise EvidenceVaultSv9AuthorityEvaluationError("packet evidence is unavailable")
                evidence.append({key: record[key] for key in ("evidence_ref", "evidence_fingerprint", "content")}); bindings.append({"tile_id": tile, "evidence_record_id": record["evidence_record_id"], **{key: record[key] for key in ("evidence_ref", "evidence_fingerprint")}})
            tiles.append({"tile_id": tile, "evidence": evidence})
        packets.append(evaluation.build_evidence_packet(component_key=component, tiles=tiles, **context, series_fingerprint=plan["current_series_fingerprint"]))
    return packets, bindings

def _complete(result: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    try:
        tiles, sentinels = [memory.validate_tile_judgment(row) for row in result["candidate_tile_judgments"]], [planner.validate_component_not_detected_sentinel(row) for row in result["candidate_component_sentinels"]]
        ids = {row["tile_id"] for row in tiles}
        return isinstance(result["assessment"], Mapping) and _capacity(tiles, sentinels) == len(planner._REGISTRY) and len(ids) == len(tiles) and not any(ids & set(planner._COMPONENT_TILES[row["component_key"]]) for row in sentinels) and {row["series_fingerprint"] for row in [*tiles, *sentinels]} == {plan["current_series_fingerprint"]}
    except (AttributeError, KeyError, TypeError, ValueError, memory.JudgmentMemoryContractError, planner.IncrementalPlannerError): return False

def _candidate(plan: Mapping[str, Any], result: Mapping[str, Any], bindings: list[dict[str, str]]) -> dict[str, Any]:
    candidate = {"schema_version": "evidence-vault-sv9-judgment-candidate-v1", "plan": plan, "canonical_plan_fingerprint": plan["canonical_plan_fingerprint"], "current_series_fingerprint": plan["current_series_fingerprint"], "candidate_series_fingerprint": plan["candidate_series_fingerprint"], "component_evaluations": [row["evaluation"] for row in result["captured_calls"]], "evidence_bindings": bindings, "candidate_tile_judgments": result["candidate_tile_judgments"], "candidate_component_sentinels": result["candidate_component_sentinels"], "assessment": result["assessment"], "telemetry": {key: result[key] for key in ("call_count", "calls_avoided", "reused_tile_count", "evaluated_tile_count")}}
    candidate["evaluation_bundle_fingerprint"] = memory.canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": plan["canonical_plan_fingerprint"], "evaluations": candidate["component_evaluations"]}); candidate["assessment_fingerprint"], candidate["score_fingerprint"] = result["assessment"]["assessment_fingerprint"], result["assessment"]["score_fingerprint"]; candidate["complete_record_fingerprint"] = memory.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v1", candidate)
    return candidate
def _replays(stored: Any, reloaded: Any, candidate: Mapping[str, Any], packets: list[dict[str, Any]]) -> bool:
    try:
        keys = "plan canonical_plan_fingerprint complete_record_fingerprint evidence_bindings candidate_tile_judgments candidate_component_sentinels assessment".split()
        if type(stored) is not dict or type(reloaded) is not dict or any(stored[key] != candidate[key] or reloaded[key] != candidate[key] for key in keys): return False
        replay = evaluation.replay_incremental_evaluations(reloaded["plan"], packets, reloaded["component_evaluations"])
        return replay["status"] == "available" and all(reloaded[key] == replay[key] for key in ("candidate_tile_judgments", "candidate_component_sentinels", "assessment"))
    except (AttributeError, KeyError, TypeError, ValueError): return False
def _outcome(status: str, plan: Mapping[str, Any] | None = None, authority: Mapping[str, str] | None = None, reasons: list[str] | None = None, ignored: int = 0, unmapped: int = 0, result: Mapping[str, Any] | None = None, candidate: Mapping[str, Any] | None = None) -> dict[str, Any]:
    values, bound = result or {}, plan or {}; output = {"status": status, "reason_codes": list(dict.fromkeys(reasons or [])), "calls_issued": int(values.get("call_count", 0)), "calls_avoided": int(values.get("calls_avoided", bound.get("calls_avoided", 0))), "reused_tiles": int(values.get("reused_tile_count", len(planner._REGISTRY) - len(bound.get("tile_workset", [])))), "evaluated_tiles": int(values.get("evaluated_tile_count", 0)), "review_tile_count": len(bound.get("review_set", [])), "trusted_irrelevant_evidence_count": ignored, "unmapped_evidence_count": unmapped, "accepted_authority": dict(authority) if authority else None, "candidate": None}
    if candidate is not None: output["candidate"] = {key: candidate[key] for key in ("id", "canonical_plan_fingerprint", "complete_record_fingerprint")}
    return output
# fmt: on
