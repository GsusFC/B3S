"""Repository-backed, pending-only SV9 authority evaluation."""

from __future__ import annotations
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID
from src.history.report_parser import normalize_domain
from src.services.evidence_vault_sv9_authoritative_relations import (
    EvidenceVaultSv9AuthoritativeRelationStaleWitnessError,
    EvidenceVaultSv9AuthoritativeRelationWitnessError,
    build_evidence_vault_sv9_authoritative_relation_witness,
    project_evidence_vault_sv9_evaluation_input,
    validate_evidence_vault_sv9_evaluation_input,
    validate_evidence_vault_sv9_authoritative_relation_witness,
)
from src.services.evidence_vault_sv9_authority_projection import (
    validate_persisted_evidence_vault_sv9_authority_projection,
)
from src.services import evidence_vault_sv9_judgment_delta as delta
from src.services import evidence_vault_sv9_evaluation_checkpoint as checkpoint
from src.services import evidence_vault_sv9_workset_partition as partitioning
from src.services.evidence_vault_incremental_refresh import validate_vault_scan_plan
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory


# fmt: off
class EvidenceVaultSv9AuthorityEvaluationRepository(Protocol):
    def get_evidence_vault_sv9_judgment_authority(self, domain_or_url: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def load_evidence_vault_sv9_authoritative_relation_facts(self, source_scan_id: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def resolve_evidence_vault_sv9_judgment_evidence(self, source_scan_id: str, advisory_evidence_refs: list[str], **kwargs: Any) -> dict[str, Any]: ...
    def get_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, *, canonical_plan_fingerprint: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def append_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, candidate: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]: ...
    def get_evidence_vault_sv9_evaluation_checkpoint_component_evaluation(self, source_scan_id: str, *, canonical_plan_fingerprint: str, canonical_request_fingerprint: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def get_evidence_vault_sv9_evaluation_checkpoint_for_request(self, source_scan_id: str, *, canonical_plan_fingerprint: str, canonical_request_fingerprint: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def append_evidence_vault_sv9_evaluation_checkpoint(self, source_scan_id: str, checkpoint: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]: ...
class EvidenceVaultSv9AuthorityEvaluationError(ValueError): pass
class EvidenceVaultSv9AuthoritySourceIdentityError(EvidenceVaultSv9AuthorityEvaluationError): pass
def run_evidence_vault_sv9_authority_evaluation(*, repository: EvidenceVaultSv9AuthorityEvaluationRepository, flow: evaluation.Sv9StrictComponentFlowPort, domain_or_url: str, source_scan_id: str, current_series_contract: Mapping[str, Any], workspace_slug: str = "b3s", trusted_irrelevant_evidence: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Evaluate exact trusted deltas without adopting, reopening, or superseding authority."""
    try:
        domain = normalize_domain(_text(domain_or_url)); _text(source_scan_id); _text(workspace_slug)
        if not domain: raise EvidenceVaultSv9AuthorityEvaluationError("domain is invalid")
        authority = repository.get_evidence_vault_sv9_judgment_authority(domain, workspace_slug=workspace_slug)
        prior, sentinels, authority_ids, overlay, authority_snapshot = _authority(authority)
        try:
            evaluation_input = project_evidence_vault_sv9_evaluation_input(
                repository=repository,
                source_scan_id=source_scan_id,
                workspace_slug=workspace_slug,
            )
            if type(evaluation_input) is not dict or evaluation_input.get("status") != "available":
                return _outcome(
                    "review_required",
                    authority=authority_ids,
                    reasons=(
                        list(evaluation_input.get("reason_codes") or ["invalid_evaluation_input"])
                        if type(evaluation_input) is dict
                        else ["invalid_evaluation_input"]
                    ),
                )
            evaluation_input = validate_evidence_vault_sv9_evaluation_input(
                evaluation_input,
                source_scan_id=source_scan_id,
                workspace_slug=workspace_slug,
            )
            current = delta.build_evidence_identity_set(
                evaluation_input["current_evidence"]
            )
            trusted = _trusted(trusted_irrelevant_evidence, current)
            relations = evaluation_input["authoritative_relations"]
            relation_pairs = {
                (row["evidence_ref"], row["evidence_fingerprint"])
                for row in relations
            }
            if relation_pairs & trusted:
                raise EvidenceVaultSv9AuthorityEvaluationError(
                    "trusted evidence conflicts with operational authority"
                )
            context, records = _source(
                repository,
                source_scan_id,
                evaluation_input,
                workspace_slug,
                domain,
            )
        except EvidenceVaultSv9AuthoritySourceIdentityError:
            raise
        except EvidenceVaultSv9AuthorityEvaluationError:
            raise
        except ValueError as exc:
            if str(exc) == "invalid_source_identity":
                raise EvidenceVaultSv9AuthoritySourceIdentityError from exc
            return _outcome(
                "review_required",
                authority=authority_ids,
                reasons=["invalid_evaluation_input"],
            )
        signed = delta.build_evidence_vault_sv9_judgment_delta(current_evidence=current, prior_judgments=prior, prior_component_sentinels=sentinels, authoritative_relations=relations, current_series_contract=dict(current_series_contract))
    except EvidenceVaultSv9AuthoritySourceIdentityError: return _outcome("no_new_score", reasons=["invalid_source_identity"])
    except EvidenceVaultSv9AuthorityEvaluationError: return _outcome("no_new_score", reasons=["invalid_input"])
    except Exception: return _outcome("no_new_score", reasons=["repository_failure"])
    try:
        initial = authority is None or (
            authority["accepted_candidate"]["source_scan_id"] == source_scan_id
            and _full_capture_candidate(authority["accepted_candidate"], current["evidence"])
        )
        if initial and not evaluation_input["non_authoritative_hints"] and _first_baseline(repository, evaluation_input, workspace_slug):
            if not overlay and not signed["plan"]["review_set"] and not evaluation_input["authority_coverage_loss"] and not evaluation_input["reopen_tile_ids"]:
                return _evaluate_first_baseline(repository, flow, source_scan_id, workspace_slug, evaluation_input, context, records, current_series_contract, authority, authority_ids)
    except EvidenceVaultSv9AuthoritativeRelationStaleWitnessError: return _outcome("review_required", authority=authority_ids, reasons=["stale_authoritative_relation_witness"])
    except EvidenceVaultSv9AuthoritativeRelationWitnessError: return _outcome("review_required", authority=authority_ids, reasons=["invalid_authoritative_relation_witness"])
    except Exception:
        return _outcome("no_new_score", authority=authority_ids, reasons=["invalid_input"])
    plan = signed["plan"]; review, ignored, unmapped = _review(signed, bool(authority), overlay, trusted)
    try:
        trusted_rows = [row for row in evaluation_input["current_identity_bindings"] if (row["evidence_ref"], row["evidence_fingerprint"]) in trusted]
        partition = partitioning.validate_evidence_vault_sv9_workset_partition(
            partitioning.build_evidence_vault_sv9_workset_partition(
                evaluation_input=evaluation_input,
                judgment_delta=signed,
                trusted_irrelevant_evidence=trusted_rows,
            )
        )
        review = _partition_reasons(partition, review)
        resolved = _resolved(partition, records)
    except Exception: return _outcome("no_new_score", plan, authority_ids, ["invalid_input"], ignored, unmapped, signed_delta=signed)
    complete = not review and _complete_partition(partition, plan)
    if not partition["healthy_workset"]["tiles"]: return _outcome("review_required" if review else "no_new_score", plan, authority_ids, review or ["exact_reuse"], ignored, unmapped, signed_delta=signed if review else None, partition=partition)
    if complete:
        try: packets, bindings = _packets(plan, records, context)
        except EvidenceVaultSv9AuthorityEvaluationError: return _outcome("no_new_score", plan, authority_ids, ["invalid_input"], ignored, unmapped, partition=partition)
        try:
            witness = build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id=source_scan_id, projection={"status": "available", "reason_codes": [], "authoritative_relations": relations, "operational_witness": evaluation_input["operational_witness"], "projection_fingerprint": evaluation_input["relation_projection_fingerprint"], **{key: evaluation_input[key] for key in ("current_identity_bindings", "authority_continuity", "authority_coverage_loss", "reopen_tile_ids")}})
            if authority and not trusted and _accepted_input_replays(repository, authority, evaluation_input, plan, witness, records, context, workspace_slug):
                return _outcome("no_new_score", plan, authority_ids, ["exact_reuse"], ignored, unmapped, partition=partition)
            existing = repository.get_evidence_vault_sv9_judgment_candidate(source_scan_id, canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], workspace_slug=workspace_slug)
        except EvidenceVaultSv9AuthoritativeRelationWitnessError: return _outcome("review_required", plan, authority_ids, ["invalid_authoritative_relation_witness"], ignored, unmapped, partition=partition)
        except Exception: return _outcome("no_new_score", plan, authority_ids, ["repository_failure"], ignored, unmapped, partition=partition)
        if existing is not None:
            try:
                if existing.get("schema_version") == "evidence-vault-sv9-judgment-candidate-v2": validate_evidence_vault_sv9_authoritative_relation_witness(existing["authoritative_relation_witness"])
            except (AttributeError, KeyError, TypeError, ValueError): return _outcome("review_required", plan, authority_ids, ["invalid_authoritative_relation_witness"], ignored, unmapped, partition=partition)
            if not _replays(existing, existing, existing, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, partition=partition)
            if existing["schema_version"].endswith("v1"): return _outcome("review_required", plan, authority_ids, ["unwitnessed_legacy_candidate"], ignored, unmapped, candidate=existing, source_scan_id=source_scan_id, partition=partition)
            if existing["authoritative_relation_witness"] != witness: return _outcome("review_required", plan, authority_ids, ["stale_authoritative_relation_witness"], ignored, unmapped, candidate=existing, source_scan_id=source_scan_id, partition=partition)
            if authority_ids and existing.get("id") == authority_ids["accepted_candidate_id"]: return _outcome("no_new_score", plan, authority_ids, ["exact_reuse"], ignored, unmapped, candidate=existing, source_scan_id=source_scan_id, partition=partition)
            return _outcome("candidate_available", plan, authority_ids, ["candidate_already_present"], ignored, unmapped, candidate=existing, source_scan_id=source_scan_id, partition=partition)
    lookup, persist = _checkpoint_callbacks(repository, source_scan_id, workspace_slug, evaluation_input, authority_snapshot, plan, partition["review_partition"]["tile_ids"], partition["pending_evidence"])
    try: result = evaluation.execute_partial_incremental_evaluation(partition, resolved, flow, lookup_evaluation=lookup, persist_evaluation=persist)
    except Exception: return _outcome("no_new_score", plan, authority_ids, ["repository_failure"], ignored, unmapped, signed_delta=signed, partition=partition)
    if result["status"] != "partial": return _outcome("review_required" if review else "no_new_score", plan, authority_ids, review + [str(result.get("reason_code") or "evaluation_incomplete")], ignored, unmapped, result, signed_delta=signed if review else None, partition=partition)
    if not complete: return _outcome("review_required", plan, authority_ids, review or ["incomplete_candidate"], ignored, unmapped, result, signed_delta=signed, partition=partition)
    try: replay = evaluation.replay_incremental_evaluations(plan, packets, [row["evaluation"] for row in result["captured_calls"]])
    except Exception: return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, result, partition=partition)
    if replay["status"] != "available" or not _complete(replay, plan): return _outcome("no_new_score", plan, authority_ids, ["incomplete_candidate"], ignored, unmapped, result, partition=partition)
    candidate = _candidate(plan, replay, bindings, witness)
    if not _replays(candidate, candidate, candidate, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, result, partition=partition)
    try:
        stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(source_scan_id, candidate, workspace_slug=workspace_slug)
        reloaded = repository.get_evidence_vault_sv9_judgment_candidate(source_scan_id, canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], workspace_slug=workspace_slug)
    except EvidenceVaultSv9AuthoritativeRelationStaleWitnessError: return _outcome("review_required", plan, authority_ids, ["stale_authoritative_relation_witness"], ignored, unmapped, result, partition=partition)
    except EvidenceVaultSv9AuthoritativeRelationWitnessError: return _outcome("review_required", plan, authority_ids, ["invalid_authoritative_relation_witness"], ignored, unmapped, result, partition=partition)
    except Exception: return _outcome("no_new_score", plan, authority_ids, ["repository_failure"], ignored, unmapped, result, partition=partition)
    try:
        for value in (stored, reloaded):
            if value.get("schema_version") == "evidence-vault-sv9-judgment-candidate-v2": validate_evidence_vault_sv9_authoritative_relation_witness(value["authoritative_relation_witness"])
    except (AttributeError, KeyError, TypeError, ValueError): return _outcome("review_required", plan, authority_ids, ["invalid_authoritative_relation_witness"], ignored, unmapped, result, partition=partition)
    if not _replays(stored, reloaded, stored, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, result, partition=partition)
    if stored["schema_version"].endswith("v1"): return _outcome("review_required", plan, authority_ids, ["unwitnessed_legacy_candidate"], ignored, unmapped, result, stored, source_scan_id=source_scan_id, partition=partition)
    if stored["authoritative_relation_witness"] != witness: return _outcome("review_required", plan, authority_ids, ["stale_authoritative_relation_witness"], ignored, unmapped, result, stored, source_scan_id=source_scan_id, partition=partition)
    if not _replays(stored, reloaded, candidate, packets): return _outcome("no_new_score", plan, authority_ids, ["invalid_replay"], ignored, unmapped, result, partition=partition)
    return _outcome("candidate_available", plan, authority_ids, [] if inserted else ["candidate_already_present"], ignored, unmapped, result, reloaded, source_scan_id=source_scan_id, partition=partition)


def _checkpoint_callbacks(repository, source_scan_id, workspace_slug, evaluation_input, authority_snapshot, plan, review_tile_ids=(), pending_evidence=()):
    def lookup(request):
        return repository.get_evidence_vault_sv9_evaluation_checkpoint_component_evaluation(source_scan_id, canonical_plan_fingerprint=request["plan_fingerprint"], canonical_request_fingerprint=request["canonical_request_fingerprint"], workspace_slug=workspace_slug)
    def persist(_request, accepted, judgments, sentinel):
        tiles = [row["tile_id"] for row in judgments] if sentinel is None else list(planner._COMPONENT_TILES[sentinel["component_key"]])
        value = checkpoint.build_evidence_vault_sv9_evaluation_checkpoint(evaluation_input=evaluation_input, prior_authority_snapshot=authority_snapshot, current_series_fingerprint=plan["current_series_fingerprint"], canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], candidate_series_fingerprint=plan["candidate_series_fingerprint"], healthy_tile_ids=tiles, review_tile_ids=list(review_tile_ids), pending_evidence=[{**row, "reason": "unmapped_evidence"} for row in pending_evidence], non_authoritative_hints=evaluation_input["non_authoritative_hints"], component_evaluations=[accepted], evaluated_tile_judgments=judgments, evaluated_component_sentinels=[] if sentinel is None else [sentinel])
        repository.append_evidence_vault_sv9_evaluation_checkpoint(source_scan_id, value, workspace_slug=workspace_slug)
    return lookup, persist


def _first_baseline(repository, current, workspace):
    getter = getattr(repository, "get_capture_operation_plan", None)
    if not callable(getter):
        return False
    source = current["source_identity"]
    operation = getter(source["source_scan_id"], workspace_slug=workspace)
    if operation is None:
        return False
    plan = operation["plan"]
    validate_vault_scan_plan(plan)
    if (
        operation["source_scan_id"] != source["source_scan_id"]
        or operation["capture_id"] != source["capture_id"]
        or operation["capture_hash"] != source["capture_fingerprint"]
        or operation["operation_plan_id"] != source["operation_plan_id"]
        or operation["operation_plan_fingerprint"] != source["operation_fingerprint"]
        or plan["operation_plan_fingerprint"] != source["operation_fingerprint"]
        or operation["status"] != source["operation_status"]
        or normalize_domain(plan["subject_url"]) != source["canonical_domain"]
    ):
        raise EvidenceVaultSv9AuthorityEvaluationError("baseline source is invalid")
    return plan["mode"] == "baseline" and plan["canonical_memory_version"] is None


def _full_capture_candidate(candidate, evidence):
    plan = candidate["plan"]
    return (
        plan.get("prior_judgments") == [] and plan.get("prior_component_sentinels") == []
        and plan.get("tile_workset") == list(planner._ORDER)
        and all(row["evidence"] == evidence for row in plan["items"])
    )


def _evaluate_first_baseline(repository, flow, scan, workspace, current, context, records, series, authority, authority_ids):
    # A first assessment receives the complete capture as input. Its selective
    # output citations, not the input workset, establish tile support.
    plan = planner.build_incremental_plan(
        prior_judgments=[],
        delta_projections=[
            memory.build_tile_evidence_delta_projection(
                tile_id=tile, component_key=component, disposition="relevant",
                evidence=current["current_evidence"], **context,
            )
            for tile, component in planner._REGISTRY
        ],
        current_series_contract=dict(series),
    )
    packets, bindings = _packets(plan, records, context)
    witness = build_evidence_vault_sv9_authoritative_relation_witness(
        source_scan_id=scan,
        projection={
            "status": "available", "reason_codes": [],
            "authoritative_relations": current["authoritative_relations"],
            "operational_witness": current["operational_witness"],
            "projection_fingerprint": current["relation_projection_fingerprint"],
            **{key: current[key] for key in ("current_identity_bindings", "authority_continuity", "authority_coverage_loss", "reopen_tile_ids")},
        },
    )
    if authority is not None:
        accepted = authority["accepted_candidate"]
        if not current["non_authoritative_hints"] and accepted["plan"] == plan and accepted["evidence_bindings"] == bindings and accepted.get("authoritative_relation_witness") == witness and _replays(accepted, accepted, accepted, packets):
            return _outcome("no_new_score", plan, authority_ids, ["exact_reuse"])
        return _outcome("no_new_score", plan, authority_ids, ["invalid_input"])
    existing = repository.get_evidence_vault_sv9_judgment_candidate(scan, canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], workspace_slug=workspace)
    if existing is not None:
        if existing["plan"] != plan or existing["evidence_bindings"] != bindings or existing.get("authoritative_relation_witness") != witness or not _replays(existing, existing, existing, packets):
            return _outcome("no_new_score", plan, reasons=["invalid_input"])
        return _outcome("candidate_available", plan, reasons=["candidate_already_present"], candidate=existing, source_scan_id=scan)
    lookup, persist = _checkpoint_callbacks(repository, scan, workspace, current, {"state": "bootstrap_absent"}, plan)
    result = evaluation.execute_incremental_evaluation(plan, packets, flow, lookup_evaluation=lookup, persist_evaluation=persist)
    if result["status"] != "available" or not _complete(result, plan):
        return _outcome("no_new_score", plan, reasons=[result.get("reason_code") or "incomplete_candidate"], result=result)
    candidate = _candidate(plan, evaluation.replay_incremental_evaluations(plan, packets, [row["evaluation"] for row in result["captured_calls"]]), bindings, witness)
    if not _replays(candidate, candidate, candidate, packets):
        return _outcome("no_new_score", plan, reasons=["invalid_replay"], result=result)
    stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(scan, candidate, workspace_slug=workspace)
    reloaded = repository.get_evidence_vault_sv9_judgment_candidate(scan, canonical_plan_fingerprint=plan["canonical_plan_fingerprint"], workspace_slug=workspace)
    if not _replays(stored, reloaded, candidate, packets):
        return _outcome("no_new_score", plan, reasons=["invalid_replay"], result=result)
    return _outcome("candidate_available", plan, reasons=[] if inserted else ["candidate_already_present"], result=result, candidate=reloaded, source_scan_id=scan)


def _text(value: Any) -> str:
    if type(value) is not str or not value.strip(): raise EvidenceVaultSv9AuthorityEvaluationError("text is invalid")
    return value.strip()
def _accepted(value: Mapping[str, Any], sentinel=False) -> dict[str, Any]:
    omit = {"schema_version", "series_fingerprint", "canonical_component_sentinel_fingerprint" if sentinel else "canonical_judgment_fingerprint", "authority_state"}
    return (planner.build_component_not_detected_sentinel if sentinel else memory.build_tile_judgment)(**({key: row for key, row in value.items() if key not in omit} | {"authority_state": "accepted"}))
def _capacity(tiles: Sequence[Mapping[str, Any]], sentinels: Sequence[Mapping[str, Any]]) -> int: return len(tiles) + sum(len(planner._COMPONENT_TILES[row["component_key"]]) for row in sentinels)
def _authority(value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str] | None, bool, dict[str, str]]:
    if value is None: return [], [], None, False, {"state": "bootstrap_absent"}
    try:
        value = validate_persisted_evidence_vault_sv9_authority_projection(value)
        part, candidate = value["accepted_partition"], value["accepted_candidate"]
        tiles, sentinels = [_accepted(row) for row in part["candidate_tile_judgments"]], [_accepted(row, True) for row in part["candidate_component_sentinels"]]
        if _capacity(tiles, sentinels) != len(planner._REGISTRY): raise ValueError
        ids = {"accepted_candidate_id": str(UUID(candidate["id"])), "active_event_id": str(UUID(value["active_authority_event"]["event_id"])), "current_head_event_fingerprint": value["current_head"]["event_fingerprint"]}
        snapshot = {"state": "accepted_authority", **ids, "candidate_complete_record_fingerprint": candidate["complete_record_fingerprint"], "canonical_plan_fingerprint": candidate["canonical_plan_fingerprint"], "current_series_fingerprint": candidate["current_series_fingerprint"]}
        return tiles, sentinels, ids, value["reopen_review_overlay"] is not None, snapshot
    except (AttributeError, KeyError, TypeError, ValueError, memory.JudgmentMemoryContractError, planner.IncrementalPlannerError) as exc: raise EvidenceVaultSv9AuthorityEvaluationError("authority is invalid") from exc
def _trusted(value: Sequence[Mapping[str, Any]], current: Mapping[str, Any]) -> set[tuple[str, str]]:
    if type(value) not in {list, tuple}: raise EvidenceVaultSv9AuthorityEvaluationError("trusted irrelevant evidence is invalid")
    pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in delta.build_evidence_identity_set(list(value))["evidence"]}
    if not pairs <= {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}: raise EvidenceVaultSv9AuthorityEvaluationError("trusted irrelevant evidence is not current")
    return pairs
def _source(repository: EvidenceVaultSv9AuthorityEvaluationRepository, scan: str, current: Mapping[str, Any], workspace: str, domain: str) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    try:
        loaded = current["source_identity"]
        if loaded["canonical_domain"] != domain:
            raise EvidenceVaultSv9AuthoritySourceIdentityError("source domain is invalid")
        context = {
            "capture_origin": {
                "capture_id": loaded["capture_id"],
                "capture_fingerprint": loaded["capture_fingerprint"],
            },
            "operation_origin": {
                "operation_id": loaded["operation_plan_id"],
                "operation_fingerprint": loaded["operation_fingerprint"],
            },
        }
        evaluation.build_evidence_packet(component_key=next(iter(planner._COMPONENT_TILES)), tiles=[], **context, series_fingerprint="0" * 64)
        if not current["current_evidence"]:
            return context, {}
        resolved = repository.resolve_evidence_vault_sv9_judgment_evidence(
            scan,
            [row["evidence_ref"] for row in current["current_evidence"]],
            workspace_slug=workspace,
        )
        if {key: resolved[key] for key in context} != context: raise ValueError
        records = {}
        for raw in resolved["evidence"]:
            if set(raw) != {"evidence_record_id", "evidence_ref", "evidence_fingerprint", "content"}: raise ValueError
            identifier = str(UUID(raw["evidence_record_id"])); evidence = evaluation._evidence([{key: raw[key] for key in ("evidence_ref", "evidence_fingerprint", "content")}], True)[0]; pair = evidence["evidence_ref"], evidence["evidence_fingerprint"]
            if identifier != raw["evidence_record_id"] or pair in records: raise ValueError
            records[pair] = {"evidence_record_id": identifier, **evidence}
        if set(records) != {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["current_evidence"]}: raise ValueError
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
def _partition_reasons(partition: Mapping[str, Any], legacy: list[str]) -> list[str]:
    review = partition["review_partition"]; codes = list(legacy)
    if any(review[key] for key in ("operational_authority_coverage_loss_tile_ids", "judgment_delta_coverage_loss_tile_ids", "evaluation_input_reopened_tile_ids")): codes.append("coverage_loss")
    if review["planner_review_tile_ids"]: codes.append("review_set")
    if review["coherencia_blocked_tile_ids"]: codes.append("incomplete_review_partition")
    if partition["pending_evidence"]: codes.append("unmapped_evidence")
    return list(dict.fromkeys(codes))
def _unresolved(plan: Mapping[str, Any], prior: Sequence[Mapping[str, Any]], sentinels: Sequence[Mapping[str, Any]]) -> bool:
    covered = {row["tile_id"] for row in prior}
    for row in sentinels: covered.update(planner._COMPONENT_TILES[row["component_key"]])
    return any(tile not in covered for tile in plan["review_set"])
def _complete_partition(partition: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    healthy = partition["healthy_workset"]["tiles"]
    return not partition["review_partition"]["tile_ids"] and not partition["pending_evidence"] and {row["tile_id"] for row in healthy} == set(plan["tile_workset"])
def _resolved(partition: Mapping[str, Any], records: Mapping[tuple[str, str], Mapping[str, Any]]) -> list[dict[str, Any]]:
    expected = {row["evidence_record_id"]: (row["evidence_ref"], row["evidence_fingerprint"]) for tile in partition["healthy_workset"]["tiles"] for row in tile["current_evidence_bindings"]}
    rows = []
    for identifier, pair in sorted(expected.items()):
        record = records.get(pair)
        if record is None or record["evidence_record_id"] != identifier: raise EvidenceVaultSv9AuthorityEvaluationError("resolved evidence is invalid")
        rows.append(record)
    return rows
def _packets(plan: Mapping[str, Any], records: Mapping[tuple[str, str], Mapping[str, Any]], context: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    # Canonical packets retain authorized plan support, never routing-only hints.
    items = {row["tile_id"]: row for row in plan["items"]}; packets, bindings = [], []
    for component in plan["component_workset"]:
        tiles = []
        for tile in [tile for tile in plan["tile_workset"] if evaluation._BY_TILE[tile][1] == component]:
            evidence = []
            for identity in items[tile]["evidence"]:
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
def _partition_telemetry(partition: Mapping[str, Any]) -> tuple[int, int, int]:
    healthy_components = {row["component_key"] for row in partition["healthy_workset"]["tiles"]}
    calls_avoided = len(set(planner._COMPONENT_TILES) - healthy_components)
    reused_tiles = len(partition["allowed_reuse_tile_ids"])
    review_tiles = len(partition["review_partition"]["tile_ids"])
    return calls_avoided, reused_tiles, review_tiles
def _outcome(status: str, plan: Mapping[str, Any] | None = None, authority: Mapping[str, str] | None = None, reasons: list[str] | None = None, ignored: int = 0, unmapped: int = 0, result: Mapping[str, Any] | None = None, candidate: Mapping[str, Any] | None = None, signed_delta: Mapping[str, Any] | None = None, source_scan_id: str | None = None, partition: Mapping[str, Any] | None = None) -> dict[str, Any]:
    values, bound = result or {}, plan or {}
    if partition is None:
        calls_avoided = int(values.get("calls_avoided", bound.get("calls_avoided", 0)))
        reused_tiles = int(values.get("reused_tile_count", len(planner._REGISTRY) - len(bound.get("tile_workset", []))))
        review_tile_count = len(bound.get("review_set", []))
    else:
        calls_avoided, reused_tiles, review_tile_count = _partition_telemetry(partition)
    output = {"status": status, "reason_codes": list(dict.fromkeys(reasons or [])), "calls_issued": int(values.get("call_count", 0)), "calls_avoided": calls_avoided, "reused_tiles": reused_tiles, "evaluated_tiles": int(values.get("evaluated_tile_count", 0)), "review_tile_count": review_tile_count, "trusted_irrelevant_evidence_count": ignored, "unmapped_evidence_count": unmapped, "accepted_authority": dict(authority) if authority else None, "candidate": None, "signed_delta": dict(signed_delta) if signed_delta is not None else None}
    if status == "review_required" and signed_delta is not None and partition is not None:
        output["workset_partition"] = dict(partition)
    if candidate is not None: output["candidate"] = {key: candidate[key] for key in ("id", "canonical_plan_fingerprint", "complete_record_fingerprint", "assessment_fingerprint", "score_fingerprint")} | {"source_scan_id": str(candidate.get("source_scan_id") or source_scan_id or ""), "schema_version": str(candidate["schema_version"])} | ({"authoritative_relation_witness_fingerprint": candidate["authoritative_relation_witness"]["witness_fingerprint"]} if candidate["schema_version"].endswith("v2") else {})
    return output
# fmt: on


# fmt: on


def _accepted_input_replays(repository, authority, current, plan, witness, records, context, workspace):
    """Reuse accepted authority only with its original, complete checkpoint input proof."""
    candidate = authority["accepted_candidate"]
    if (
        authority["reopen_review_overlay"] is not None
        or current["non_authoritative_hints"]
        or candidate["source_scan_id"] != current["source_identity"]["source_scan_id"]
        or candidate["current_series_fingerprint"] != plan["current_series_fingerprint"]
        or candidate.get("authoritative_relation_witness") != witness
        or not candidate["component_evaluations"]
    ):
        return False
    try:
        packets, bindings = _packets(candidate["plan"], records, context)
        if bindings != candidate["evidence_bindings"] or not _replays(candidate, candidate, candidate, packets):
            return False
        expected_plan = {
            key: candidate[key]
            for key in ("canonical_plan_fingerprint", "current_series_fingerprint", "candidate_series_fingerprint")
        }
        prior_snapshot = None
        for component in candidate["component_evaluations"]:
            try:
                stored = repository.get_evidence_vault_sv9_evaluation_checkpoint_for_request(
                    candidate["source_scan_id"],
                    canonical_plan_fingerprint=candidate["canonical_plan_fingerprint"],
                    canonical_request_fingerprint=component["request_fingerprint"],
                    workspace_slug=workspace,
                )
            except Exception:
                # This optional read can deny reuse, never authorize it on failure.
                return False
            if not isinstance(stored, Mapping):
                return False
            proof = checkpoint.validate_evidence_vault_sv9_evaluation_checkpoint(
                {key: value for key, value in stored.items() if key not in {"id", "source_scan_id", "created_at"}}
            )
            if (
                proof["evaluation_state"] != "partial"
                or proof["evaluation_input"] != current
                or proof["plan_binding"] != expected_plan
                or proof["healthy_workset"]["component_evaluations"] != [component]
                or proof["review_partition"]["tile_ids"]
                or proof["pending_evidence"]
                or proof["non_authoritative_hints"]
                or (prior_snapshot is not None and proof["prior_authority_snapshot"] != prior_snapshot)
            ):
                return False
            # All components belong to one historical input; adoption must not rewrite it.
            prior_snapshot = proof["prior_authority_snapshot"]
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
