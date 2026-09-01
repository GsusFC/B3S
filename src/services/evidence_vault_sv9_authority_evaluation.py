"""Repository-backed, pending-only SV9 authority evaluation."""

from __future__ import annotations
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID
from src.history.report_parser import normalize_domain
from src.services.evidence_vault_sv9_authoritative_relations import (
    EvidenceVaultSv9AuthoritativeRelationStaleWitnessError,
    EvidenceVaultSv9AuthoritativeRelationWitnessError,
    build_evidence_vault_sv9_authoritative_relation_witness,
    project_evidence_vault_sv9_capture_current,
    project_evidence_vault_sv9_authoritative_relations,
    validate_evidence_vault_sv9_authoritative_relation_witness,
)
from src.services.evidence_vault_sv9_authority_projection import (
    validate_persisted_evidence_vault_sv9_authority_projection,
)
from src.services import evidence_vault_sv9_judgment_delta as delta
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory


# fmt: off
class EvidenceVaultSv9AuthorityEvaluationRepository(Protocol):
    def get_evidence_vault_sv9_judgment_authority(self, domain_or_url: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def load_evidence_vault_sv9_judgment_context(self, source_scan_id: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def load_evidence_vault_sv9_authoritative_relation_facts(self, source_scan_id: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def resolve_evidence_vault_sv9_judgment_evidence(self, source_scan_id: str, advisory_evidence_refs: list[str], **kwargs: Any) -> dict[str, Any]: ...
    def get_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, *, canonical_plan_fingerprint: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def append_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, candidate: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]: ...
class EvidenceVaultSv9AuthorityEvaluationError(ValueError): pass
class EvidenceVaultSv9AuthoritySourceIdentityError(EvidenceVaultSv9AuthorityEvaluationError): pass
def run_evidence_vault_sv9_authority_evaluation(*, repository: EvidenceVaultSv9AuthorityEvaluationRepository, flow: evaluation.Sv9StrictComponentFlowPort, domain_or_url: str, source_scan_id: str, current_evidence: list[Mapping[str, Any]], authoritative_relations: list[Mapping[str, Any]], current_series_contract: Mapping[str, Any], workspace_slug: str = "b3s", trusted_irrelevant_evidence: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Evaluate exact trusted deltas without adopting, reopening, or superseding authority."""
    try:
        domain = normalize_domain(_text(domain_or_url)); _text(source_scan_id); _text(workspace_slug)
        if not domain: raise EvidenceVaultSv9AuthorityEvaluationError("domain is invalid")
        current = delta.build_evidence_identity_set(current_evidence); trusted = _trusted(trusted_irrelevant_evidence, current)
        authority = repository.get_evidence_vault_sv9_judgment_authority(domain, workspace_slug=workspace_slug)
        prior, sentinels, authority_ids, overlay = _authority(authority)
        try:
            persisted_current = project_evidence_vault_sv9_capture_current(repository=repository, source_scan_id=source_scan_id, workspace_slug=workspace_slug)
            if type(persisted_current) is not dict or persisted_current.get("status") != "available": return _outcome("review_required", authority=authority_ids, reasons=list(persisted_current.get("reason_codes") or ["capture_current_unavailable"]) if type(persisted_current) is dict else ["capture_current_unavailable"])
            persisted_identity = delta.build_evidence_identity_set(persisted_current["current_evidence"])
            if persisted_identity != current: return _outcome("review_required", authority=authority_ids, reasons=["current_evidence_mismatch"])
        except Exception: return _outcome("review_required", authority=authority_ids, reasons=["capture_current_unavailable"])
        try: projection = project_evidence_vault_sv9_authoritative_relations(repository=repository, source_scan_id=source_scan_id, workspace_slug=workspace_slug)
        except Exception: return _outcome("review_required", authority=authority_ids, reasons=["authoritative_relations_unavailable"])
        if type(projection) is not dict or projection.get("status") != "available": return _outcome("review_required", authority=authority_ids, reasons=list(projection.get("reason_codes") or ["authoritative_relations_unavailable"]) if type(projection) is dict else ["authoritative_relations_unavailable"])
        try:
            pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in projection["authoritative_relations"]}
            if pairs & trusted: raise EvidenceVaultSv9AuthorityEvaluationError("trusted evidence conflicts with operational authority")
            current_pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}
            if type(authoritative_relations) is not list or authoritative_relations != projection["authoritative_relations"] or not pairs <= current_pairs: return _outcome("review_required", authority=authority_ids, reasons=["authoritative_relation_mismatch"])
        except EvidenceVaultSv9AuthorityEvaluationError: raise
        except Exception: return _outcome("review_required", authority=authority_ids, reasons=["invalid_authoritative_relation_witness"])
        context, records = _source(repository, source_scan_id, current, workspace_slug, domain)
        relations = projection["authoritative_relations"]
        signed = delta.build_evidence_vault_sv9_judgment_delta(current_evidence=current, prior_judgments=prior, prior_component_sentinels=sentinels, authoritative_relations=relations, current_series_contract=dict(current_series_contract))
    except EvidenceVaultSv9AuthoritySourceIdentityError: return _outcome("no_new_score", reasons=["invalid_source_identity"])
    except EvidenceVaultSv9AuthorityEvaluationError: return _outcome("no_new_score", reasons=["invalid_input"])
    except Exception: return _outcome("no_new_score", reasons=["repository_failure"])
    plan = signed["plan"]; review, ignored, unmapped = _review(signed, bool(authority), overlay, trusted)
    if unmapped: return _outcome("review_required", plan, authority_ids, review, ignored, unmapped, signed_delta=signed)
    if _unresolved(plan, prior, sentinels): return _outcome("review_required", plan, authority_ids, review + ["incomplete_review_partition"], ignored, unmapped, signed_delta=signed)
    if not plan["component_workset"]: return _outcome("review_required" if review else "no_new_score", plan, authority_ids, review or ["exact_reuse"], ignored, unmapped, signed_delta=signed if review else None)
    try: witness = build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id=source_scan_id, projection=projection)
    except EvidenceVaultSv9AuthorityEvaluationError: raise
    except Exception: return _outcome("review_required", plan, authority_ids, ["invalid_authoritative_relation_witness"], ignored, unmapped)
    try: existing = repository.get_evidence_vault_sv9_judgment_candidate(source_scan_id, canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], workspace_slug=workspace_slug)
    except EvidenceVaultSv9AuthoritativeRelationWitnessError: return _outcome("review_required", plan, authority_ids, ["invalid_authoritative_relation_witness"], ignored, unmapped)
    except Exception: return _outcome("no_new_score", plan, authority_ids, ["repository_failure"], ignored, unmapped)
    try: packets, bindings = _packets(plan, records, context)
    except EvidenceVaultSv9AuthorityEvaluationError: return _outcome("no_new_score", plan, authority_ids, ["invalid_input"], ignored, unmapped)
    if existing is not None:
        try:
            if existing.get("schema_version") == "evidence-vault-sv9-judgment-candidate-v2": validate_evidence_vault_sv9_authoritative_relation_witness(existing["authoritative_relation_witness"])
        except (AttributeError, KeyError, TypeError, ValueError): return _outcome("review_required", plan, authority_ids, ["invalid_authoritative_relation_witness"], ignored, unmapped)
        if not _replays(existing, existing, existing, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped)
        if existing["schema_version"].endswith("v1"): return _outcome("review_required", plan, authority_ids, review + ["unwitnessed_legacy_candidate"], ignored, unmapped, candidate=existing, signed_delta=signed if review else None, source_scan_id=source_scan_id)
        if existing["authoritative_relation_witness"] != witness: return _outcome("review_required", plan, authority_ids, review + ["stale_authoritative_relation_witness"], ignored, unmapped, candidate=existing, signed_delta=signed if review else None, source_scan_id=source_scan_id)
        if authority_ids and existing.get("id") == authority_ids["accepted_candidate_id"]: return _outcome("no_new_score", plan, authority_ids, ["exact_reuse"], ignored, unmapped, candidate=existing, source_scan_id=source_scan_id)
        return _outcome("review_required" if review else "candidate_available", plan, authority_ids, review or ["candidate_already_present"], ignored, unmapped, candidate=existing, signed_delta=signed if review else None, source_scan_id=source_scan_id)
    result = evaluation.execute_incremental_evaluation(plan, packets, flow)
    if result["status"] != "available": return _outcome("no_new_score", plan, authority_ids, [str(result.get("reason_code") or "evaluation_incomplete")], ignored, unmapped, result)
    if not _complete(result, plan): return _outcome("no_new_score", plan, authority_ids, ["incomplete_candidate"], ignored, unmapped, result)
    candidate = _candidate(plan, result, bindings, witness)
    if not _replays(candidate, candidate, candidate, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, result)
    try:
        stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(source_scan_id, candidate, workspace_slug=workspace_slug)
        reloaded = repository.get_evidence_vault_sv9_judgment_candidate(source_scan_id, canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], workspace_slug=workspace_slug)
    except EvidenceVaultSv9AuthoritativeRelationStaleWitnessError: return _outcome("review_required", plan, authority_ids, ["stale_authoritative_relation_witness"], ignored, unmapped, result)
    except EvidenceVaultSv9AuthoritativeRelationWitnessError: return _outcome("review_required", plan, authority_ids, ["invalid_authoritative_relation_witness"], ignored, unmapped, result)
    except Exception: return _outcome("no_new_score", plan, authority_ids, ["repository_failure"], ignored, unmapped, result)
    try:
        for value in (stored, reloaded):
            if value.get("schema_version") == "evidence-vault-sv9-judgment-candidate-v2": validate_evidence_vault_sv9_authoritative_relation_witness(value["authoritative_relation_witness"])
    except (AttributeError, KeyError, TypeError, ValueError): return _outcome("review_required", plan, authority_ids, ["invalid_authoritative_relation_witness"], ignored, unmapped, result)
    if not _replays(stored, reloaded, stored, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, result)
    if stored["schema_version"].endswith("v1"): return _outcome("review_required", plan, authority_ids, review + ["unwitnessed_legacy_candidate"], ignored, unmapped, result, stored, signed_delta=signed if review else None, source_scan_id=source_scan_id)
    if stored["authoritative_relation_witness"] != witness: return _outcome("review_required", plan, authority_ids, review + ["stale_authoritative_relation_witness"], ignored, unmapped, result, stored, signed_delta=signed if review else None, source_scan_id=source_scan_id)
    if not _replays(stored, reloaded, candidate, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, result)
    return _outcome("review_required" if review else "candidate_available", plan, authority_ids, review if inserted else review or ["candidate_already_present"], ignored, unmapped, result, reloaded, signed_delta=signed if review else None, source_scan_id=source_scan_id)
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
        value = validate_persisted_evidence_vault_sv9_authority_projection(value)
        part, candidate = value["accepted_partition"], value["accepted_candidate"]
        tiles, sentinels = [_accepted(row) for row in part["candidate_tile_judgments"]], [_accepted(row, True) for row in part["candidate_component_sentinels"]]
        if _capacity(tiles, sentinels) != len(planner._REGISTRY): raise ValueError
        return tiles, sentinels, {"accepted_candidate_id": str(UUID(candidate["id"])), "active_event_id": str(UUID(value["active_authority_event"]["event_id"])), "current_head_event_fingerprint": value["current_head"]["event_fingerprint"]}, value["reopen_review_overlay"] is not None
    except (AttributeError, KeyError, TypeError, ValueError, memory.JudgmentMemoryContractError, planner.IncrementalPlannerError) as exc: raise EvidenceVaultSv9AuthorityEvaluationError("authority is invalid") from exc
def _trusted(value: Sequence[Mapping[str, Any]], current: Mapping[str, Any]) -> set[tuple[str, str]]:
    if type(value) not in {list, tuple}: raise EvidenceVaultSv9AuthorityEvaluationError("trusted irrelevant evidence is invalid")
    pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in delta.build_evidence_identity_set(list(value))["evidence"]}
    if not pairs <= {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}: raise EvidenceVaultSv9AuthorityEvaluationError("trusted irrelevant evidence is not current")
    return pairs
def _source(repository: EvidenceVaultSv9AuthorityEvaluationRepository, scan: str, current: Mapping[str, Any], workspace: str, domain: str) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    try:
        loaded = repository.load_evidence_vault_sv9_judgment_context(scan, workspace_slug=workspace)
        if loaded["canonical_domain"] != domain: raise EvidenceVaultSv9AuthoritySourceIdentityError("source domain is invalid")
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
    except EvidenceVaultSv9AuthoritySourceIdentityError: raise
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

def _candidate(plan: Mapping[str, Any], result: Mapping[str, Any], bindings: list[dict[str, str]], witness: Mapping[str, Any]) -> dict[str, Any]:
    candidate = {"schema_version": "evidence-vault-sv9-judgment-candidate-v2", "plan": plan, "canonical_plan_fingerprint": plan["canonical_plan_fingerprint"], "current_series_fingerprint": plan["current_series_fingerprint"], "candidate_series_fingerprint": plan["candidate_series_fingerprint"], "component_evaluations": [row["evaluation"] for row in result["captured_calls"]], "evidence_bindings": bindings, "candidate_tile_judgments": result["candidate_tile_judgments"], "candidate_component_sentinels": result["candidate_component_sentinels"], "assessment": result["assessment"], "telemetry": {key: result[key] for key in ("call_count", "calls_avoided", "reused_tile_count", "evaluated_tile_count")}, "authoritative_relation_witness": dict(witness)}
    candidate["evaluation_bundle_fingerprint"] = memory.canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": plan["canonical_plan_fingerprint"], "evaluations": candidate["component_evaluations"]}); candidate["assessment_fingerprint"], candidate["score_fingerprint"] = result["assessment"]["assessment_fingerprint"], result["assessment"]["score_fingerprint"]; candidate["complete_record_fingerprint"] = memory.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v2", candidate)
    return candidate
def _replays(stored: Any, reloaded: Any, candidate: Mapping[str, Any], packets: list[dict[str, Any]]) -> bool:
    try:
        keys = "schema_version plan canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint component_evaluations evidence_bindings candidate_tile_judgments candidate_component_sentinels assessment telemetry evaluation_bundle_fingerprint assessment_fingerprint score_fingerprint complete_record_fingerprint".split()
        if candidate.get("schema_version") == "evidence-vault-sv9-judgment-candidate-v2": keys.append("authoritative_relation_witness"); validate_evidence_vault_sv9_authoritative_relation_witness(candidate["authoritative_relation_witness"])
        elif candidate.get("schema_version") != "evidence-vault-sv9-judgment-candidate-v1": return False
        if type(stored) is not dict or type(reloaded) is not dict or any(stored[key] != candidate[key] or reloaded[key] != candidate[key] for key in keys): return False
        replay = evaluation.replay_incremental_evaluations(reloaded["plan"], packets, reloaded["component_evaluations"])
        record = {key: reloaded[key] for key in reloaded if key not in {"id", "source_scan_id", "created_at", "complete_record_fingerprint"}}
        return replay["status"] == "available" and all(reloaded[key] == replay[key] for key in ("candidate_tile_judgments", "candidate_component_sentinels", "assessment")) and reloaded["assessment_fingerprint"] == replay["assessment"]["assessment_fingerprint"] and reloaded["score_fingerprint"] == replay["assessment"]["score_fingerprint"] and reloaded["evaluation_bundle_fingerprint"] == memory.canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": reloaded["canonical_plan_fingerprint"], "evaluations": reloaded["component_evaluations"]}) and reloaded["complete_record_fingerprint"] == memory.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v2" if reloaded["schema_version"].endswith("v2") else "evidence-vault-sv9-judgment-candidate-record-v1", record)
    except (AttributeError, KeyError, TypeError, ValueError): return False
def _outcome(status: str, plan: Mapping[str, Any] | None = None, authority: Mapping[str, str] | None = None, reasons: list[str] | None = None, ignored: int = 0, unmapped: int = 0, result: Mapping[str, Any] | None = None, candidate: Mapping[str, Any] | None = None, signed_delta: Mapping[str, Any] | None = None, source_scan_id: str | None = None) -> dict[str, Any]:
    values, bound = result or {}, plan or {}; output = {"status": status, "reason_codes": list(dict.fromkeys(reasons or [])), "calls_issued": int(values.get("call_count", 0)), "calls_avoided": int(values.get("calls_avoided", bound.get("calls_avoided", 0))), "reused_tiles": int(values.get("reused_tile_count", len(planner._REGISTRY) - len(bound.get("tile_workset", [])))), "evaluated_tiles": int(values.get("evaluated_tile_count", 0)), "review_tile_count": len(bound.get("review_set", [])), "trusted_irrelevant_evidence_count": ignored, "unmapped_evidence_count": unmapped, "accepted_authority": dict(authority) if authority else None, "candidate": None, "signed_delta": dict(signed_delta) if signed_delta is not None else None}
    if candidate is not None: output["candidate"] = {key: candidate[key] for key in ("id", "canonical_plan_fingerprint", "complete_record_fingerprint", "assessment_fingerprint", "score_fingerprint")} | {"source_scan_id": str(candidate.get("source_scan_id") or source_scan_id or ""), "schema_version": str(candidate["schema_version"])} | ({"authoritative_relation_witness_fingerprint": candidate["authoritative_relation_witness"]["witness_fingerprint"]} if candidate["schema_version"].endswith("v2") else {})
    return output
# fmt: on
