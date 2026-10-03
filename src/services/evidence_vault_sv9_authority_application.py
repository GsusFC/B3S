"""Retry-safe application of persisted SV9 judgment authority candidates."""

from __future__ import annotations

from collections import Counter
import json
import logging
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from src.config import BRAND3_VAULT_TILE_RESCAN_APPLY_DOMAINS, BRAND3_VAULT_TILE_RESCAN_APPLY_ENABLED
from src.history.report_parser import normalize_domain
from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_sv9_authority_evaluation as evaluation_service
from src.services import evidence_vault_sv9_judgment_delta as delta
from src.services import evidence_vault_sv9_workset_partition as partitioning
from src.services.evidence_vault_evidence_ledger import build_evidence_ledger, build_shown_index
from src.services.evidence_vault_sv9_authoritative_relations import (
    EvidenceVaultSv9AuthoritativeRelationStaleWitnessError,
    EvidenceVaultSv9AuthoritativeRelationWitnessError,
    build_evidence_vault_sv9_authoritative_relation_witness,
    project_evidence_vault_sv9_evaluation_input,
    validate_evidence_vault_sv9_evaluation_input,
)
from src.services.evidence_vault_sv9_authority_projection import (
    validate_persisted_evidence_vault_sv9_authority_projection,
)
from src.services.evidence_vault_tile_rescan_apply import build_tile_rescan_candidate
from src.sv9 import incremental_evaluation as evaluation

_LOG = logging.getLogger(__name__)


# fmt: off
class EvidenceVaultSv9JudgmentCandidateLegacyAuthorityError(Exception):
    pass


class EvidenceVaultSv9AuthorityApplicationRepository(evaluation_service.EvidenceVaultSv9AuthorityEvaluationRepository, Protocol):
    def adopt_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, candidate_id: str, **kwargs: Any) -> tuple[dict[str, Any], bool]: ...
    def reopen_evidence_vault_sv9_judgment_authority(self, source_scan_id: str, signed_delta: Mapping[str, Any], *, workset_partition: Mapping[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]: ...

def run_evidence_vault_sv9_authority_application(*, repository: EvidenceVaultSv9AuthorityApplicationRepository, flow: evaluation.Sv9StrictComponentFlowPort, domain_or_url: str, source_scan_id: str, current_series_contract: Mapping[str, Any], workspace_slug: str = "b3s", trusted_irrelevant_evidence: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Evaluate first, then append at most one authority event or fail closed."""
    try:
        outcome = evaluation_service.run_evidence_vault_sv9_authority_evaluation(
            repository=repository, flow=flow, domain_or_url=domain_or_url, source_scan_id=source_scan_id,
            current_series_contract=current_series_contract, workspace_slug=workspace_slug,
            trusted_irrelevant_evidence=trusted_irrelevant_evidence,
        )
    except Exception:
        outcome = {"status": "no_new_score", "reason_codes": ["evaluation_failure"]}
    if type(outcome) is not dict:
        return _result("authority_conflict", {"reason_codes": ["evaluation_failure"]})
    status, source_scan_id = outcome.get("status"), source_scan_id.strip() if type(source_scan_id) is str else source_scan_id
    if status == "candidate_available":
        return _apply_candidate(repository, domain_or_url, source_scan_id, workspace_slug, outcome)
    if status == "review_required":
        return _apply_tile_rescan(repository, flow, domain_or_url, source_scan_id, workspace_slug, outcome) or _apply_review(repository, domain_or_url, source_scan_id, workspace_slug, outcome)
    if status == "no_new_score":
        return _result("authority_conflict", outcome) if any(reason in outcome.get("reason_codes", []) for reason in ("invalid_source_identity", "invalid_input")) else _retain(repository, domain_or_url, source_scan_id, workspace_slug, outcome)
    return _result("authority_conflict", outcome)

def _apply_candidate(repository, domain: str, source: str, workspace: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _candidate(outcome.get("candidate"))
    valid, predecessor = _snapshot(outcome)
    if candidate is None or candidate["source_scan_id"] != source or not valid:
        return _result("authority_conflict", outcome)
    state, authority, details = _read(repository, domain, workspace)
    if state == "authority" and details.get("rejected_candidate_id") == candidate["id"]:
        return _result("authority_retained", dict(outcome) | {"reason_codes": ["review_rejected"]}, authority, candidate)
    # The overlay's own scan waits for human review, and re-adopting the accepted
    # candidate cannot clear it. The repository decides whether this scan is newer.
    if state == "conflict" or (state == "authority" and details["overlay"] is not None and (details["review_scan_id"] == source or _matches(details, candidate))):
        return _result("authority_conflict", outcome)
    if state == "authority" and _matches(details, candidate):
        return _success(outcome, authority, details, candidate)
    if not _same_snapshot(state, details, predecessor):
        return _result("authority_conflict", outcome)
    key = _idempotency("adopt_candidate", source, candidate["id"], None, predecessor)
    try:
        repository.adopt_evidence_vault_sv9_judgment_candidate(
            source, candidate["id"], expected_predecessor_event_fingerprint=predecessor,
            idempotency_key_hash=key, workspace_slug=workspace,
        )
    except EvidenceVaultSv9AuthoritativeRelationStaleWitnessError:
        return _candidate_review(outcome, candidate, authority, "stale_authoritative_relation_witness")
    except EvidenceVaultSv9AuthoritativeRelationWitnessError:
        return _candidate_review(outcome, candidate, authority, "invalid_authoritative_relation_witness")
    except EvidenceVaultSv9JudgmentCandidateLegacyAuthorityError:
        return _candidate_review(outcome, candidate, authority, "unwitnessed_legacy_candidate")
    except Exception:
        pass
    return _candidate_readback(repository, domain, workspace, outcome, candidate)

def _candidate_review(outcome: Mapping[str, Any], candidate: Mapping[str, str], authority: Mapping[str, Any] | None, reason: str) -> dict[str, Any]:
    return _result("review_required", dict(outcome) | {"reason_codes": [reason]}, authority, candidate)

def _candidate_readback(repository, domain: str, workspace: str, outcome: Mapping[str, Any], candidate: Mapping[str, str]) -> dict[str, Any]:
    state, authority, details = _read(repository, domain, workspace)
    if state == "authority" and details["overlay"] is None and _matches(details, candidate):
        return _success(outcome, authority, details, candidate)
    return _result("authority_conflict", outcome)

def _apply_review(repository, domain: str, source: str, workspace: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    valid, predecessor = _snapshot(outcome)
    state, authority, details = _read(repository, domain, workspace)
    if state == "conflict" or not valid:
        return _result("authority_conflict", outcome)
    if state == "absent": return _result("first_run_unresolved", outcome) if _same_snapshot(state, details, predecessor) else _result("authority_conflict", outcome)
    signed = _signed_delta(outcome.get("signed_delta"))
    partition = _workset_partition(outcome.get("workset_partition"), signed)
    if signed is None or partition is None:
        return _result("authority_conflict", outcome, authority)
    fingerprint = signed["canonical_delta_fingerprint"]
    partition_fingerprint = partition["partition_fingerprint"]
    if _same_review_overlay(details["overlay"], fingerprint, partition_fingerprint):
        return _result("review_required", outcome, authority, signed_delta=signed)
    if not _same_snapshot(state, details, predecessor):
        return _result("authority_conflict", outcome)
    key = _idempotency(
        "reopen_authority", source, None, fingerprint, predecessor, partition_fingerprint
    )
    try:
        repository.reopen_evidence_vault_sv9_judgment_authority(
            source, signed, expected_predecessor_event_fingerprint=predecessor,
            idempotency_key_hash=key, workspace_slug=workspace,
            workset_partition=partition,
        )
    except Exception:
        pass
    state, authority, details = _read(repository, domain, workspace)
    if state == "authority" and _same_review_overlay(
        details["overlay"], fingerprint, partition_fingerprint
    ):
        return _result("review_required", outcome, authority, signed_delta=signed)
    return _result("authority_conflict", outcome)

def _apply_tile_rescan(repository, flow, domain: str, source: str, workspace: str, outcome: Mapping[str, Any]) -> dict[str, Any] | None:
    """S4a: an eligible re-scan of a listed brand publishes its v3 candidate; None keeps today's review."""
    if not BRAND3_VAULT_TILE_RESCAN_APPLY_ENABLED or normalize_domain(domain) not in BRAND3_VAULT_TILE_RESCAN_APPLY_DOMAINS:
        return None
    summary: dict[str, Any] = {"scan_id": source, "status": "failed", "reason_codes": [], **dict.fromkeys(("change_signal", "counts", "accepted_score", "score", "candidate_id", "step", "error"))}
    try:
        result = _tile_rescan(repository, flow, domain, source, workspace, outcome, summary)
    except Exception as exc:
        result = None; summary.update(status="failed", reason_codes=[type(exc).__name__], error=_error_chain(exc))
    # WARNING with the JSON in the message: without a logging config, INFO and extra fields never reach the log.
    _LOG.warning("vault tile rescan %s %s", "guard" if summary["status"] == "guarded" else "apply", json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str))
    return result

def _tile_rescan(repository, flow, domain: str, source: str, workspace: str, outcome: Mapping[str, Any], summary: dict[str, Any]) -> dict[str, Any] | None:
    bound = outcome.get("accepted_authority")
    if not isinstance(bound, Mapping):
        summary.update(status="ineligible", reason_codes=["no_accepted_authority"]); return None
    summary["step"] = "authority"
    state, authority, details = _read(repository, domain, workspace)
    held = state == "authority" and (details["candidate"]["id"], details["head"]) == (bound.get("accepted_candidate_id"), bound.get("current_head_event_fingerprint"))
    facts = repository.load_evidence_ledger_scan_facts(domain, source_scan_id=source, workspace_slug=workspace) if held else None
    if facts is None or facts["accepted"]["candidate_id"] != details["candidate"]["id"]:
        summary["reason_codes"] = ["accepted_authority_mismatch"]; return None
    summary["step"] = "witness"
    accepted, witness = authority["accepted_candidate"], _rescan_witness(repository, source, workspace)
    summary["accepted_score"], prior = accepted["assessment"]["sv9_score"], accepted["authoritative_relation_witness"]
    # The outcome carries no witness: today's must descend from the accepted one before the builder protects its relations.
    if witness["operational_witness"] != prior["operational_witness"] or [row["tile_id"] for row in witness["authoritative_relations"]] != [row["tile_id"] for row in prior["authoritative_relations"]]:
        summary["reason_codes"] = ["witness_mismatch"]; return None
    current, summary["step"] = facts["current"], "ledger"
    rows = build_evidence_ledger(brand_domain=facts["domain"], prior_snapshot=facts["prior"]["snapshot"], prior_rows=facts["prior"]["evidence_rows"], current_snapshot=current["snapshot"], current_rows=current["evidence_rows"], shown_index=build_shown_index(current["evaluations"]))["rows"]
    summary["step"] = "build"
    built = build_tile_rescan_candidate(outcome=outcome, accepted_candidate=accepted, ledger_rows=rows, current_judgments=current["judgments"], witness=witness)
    candidate = built["candidate"]
    summary.update(status=built["status"], reason_codes=built["reason_codes"], change_signal=built["change_signal"])
    if candidate is None:
        return None
    summary.update(counts=dict(Counter(row["decision"] for row in candidate["tile_rescan"]["decisions"])), score=candidate["assessment"]["sv9_score"])
    kept, chosen = ({row["tile_id"]: row for row in value["candidate_tile_judgments"]} for value in (accepted, candidate))
    # The builder returns decisions, not sources: a tile is current when its row is not the accepted one.
    sources = {tile: "current" if tile in chosen and chosen[tile] != kept.get(tile) else "accepted" for tile in evaluation._ORDER}
    fresh = {evaluation._BY_TILE[tile][1] for tile, origin in sources.items() if origin == "current"}
    components = {row["component_key"]: row["component_result"] for row in current["evaluations"] if row["component_key"] in fresh}
    # Without the re-scan's Flow context (no component evaluated or restored), merging would prepare a new one with model calls.
    if getattr(flow, "_candidate", None) is None:
        summary.update(status="failed", reason_codes=["no_flow_context"]); return None
    # The adapter that holds this re-scan's Flow context; its payload finalizer runs on the merged payload.
    summary["step"] = "snapshot"
    snapshot, _assessment = flow.build_merged_shared_analysis_payload(assessment=candidate["assessment"], current_components=components, tile_source_map=sources)
    summary["step"] = "append"
    stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(source, candidate, workspace_slug=workspace, shared_analysis_payload=snapshot)
    summary["candidate_id"] = stored["id"]
    compact = {key: stored[key] for key in ("id", "source_scan_id", "canonical_plan_fingerprint", "complete_record_fingerprint", "assessment_fingerprint", "score_fingerprint")}
    summary["step"] = "adopt"
    result = _apply_candidate(repository, domain, source, workspace, dict(outcome) | {"status": "candidate_available", "reason_codes": [] if inserted else ["candidate_already_present"], "candidate": compact})
    advanced = result["status"] == "authority_advanced"
    summary.update(status="applied" if advanced else "failed", reason_codes=[result["status"], *result["reason_codes"]])
    return result if advanced else None

def _error_chain(exc: BaseException) -> str:
    """The exception and its causes, bounded: a repository refusal names its failing check in the cause."""
    parts: list[str] = []
    while exc is not None and len(parts) < 3:
        parts.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    return " <- ".join(parts)[:400]

def _rescan_witness(repository, source: str, workspace: str) -> dict[str, Any]:
    """The re-scan's authoritative relation witness, built as the evaluation builds a candidate's."""
    value = validate_evidence_vault_sv9_evaluation_input(project_evidence_vault_sv9_evaluation_input(repository=repository, source_scan_id=source, workspace_slug=workspace), source_scan_id=source, workspace_slug=workspace)
    origin = value["source_identity"]
    projection = {"status": "available", "reason_codes": [], "authoritative_relations": value["authoritative_relations"], "operational_witness": value["operational_witness"], "projection_fingerprint": value["relation_projection_fingerprint"], **{key: value[key] for key in ("current_identity_bindings", "authority_continuity", "authority_coverage_loss", "reopen_tile_ids")}}
    # Operational coverage loss or a reopened tile has no witness: the builder raises, so the review stays.
    return build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id=source, projection=projection, capture_origin={key: origin[key] for key in ("capture_id", "capture_fingerprint")}, operation_origin={"operation_id": origin["operation_plan_id"], "operation_fingerprint": origin["operation_fingerprint"]})

def _retain(repository, domain: str, source: str, workspace: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    state, authority, _details = _read(repository, domain, workspace)
    if state == "authority" and (_details["overlay"] is None or _details.get("review_state") == "rejected" or _details["review_scan_id"] != source):
        return _result("authority_retained", outcome, authority)
    return _result("first_run_unresolved" if state == "absent" else "authority_conflict", outcome)

def _read(repository, domain: str, workspace: str) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    try:
        authority = repository.get_evidence_vault_sv9_judgment_authority(domain, workspace_slug=workspace)
        return ("absent", None, None) if authority is None else ("authority", authority, _authority(authority))
    except Exception:
        return "conflict", None, None

def _authority(value: Any) -> dict[str, Any]:
    value = validate_persisted_evidence_vault_sv9_authority_projection(value)
    candidate = _candidate(value["accepted_candidate"])
    assessment = value["accepted_candidate"]["assessment"]
    required = "id source_scan_id complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint".split()
    if candidate is None or any(name not in candidate for name in required) or value["assessment"] != assessment or value["score"] != assessment["sv9_score"]:
        raise ValueError("authority projection is inconsistent")
    if assessment.get("assessment_fingerprint") != candidate["assessment_fingerprint"] or assessment.get("score_fingerprint") != candidate["score_fingerprint"]:
        raise ValueError("authority assessment is inconsistent")
    active, head = value["active_authority_event"], value["current_head"]
    if active["event_type"] not in {"adopt", "supersede"}:
        raise ValueError("authority event is invalid")
    bindings = {"candidate_id": "id", "candidate_complete_record_fingerprint": "complete_record_fingerprint", "evaluation_bundle_fingerprint": "evaluation_bundle_fingerprint", "canonical_plan_fingerprint": "canonical_plan_fingerprint", "current_series_fingerprint": "current_series_fingerprint", "candidate_series_fingerprint": "candidate_series_fingerprint", "assessment_fingerprint": "assessment_fingerprint", "score_fingerprint": "score_fingerprint"}
    if any(active[name] != candidate[field] for name, field in bindings.items()) or active["request"]["source_scan_id"] != candidate["source_scan_id"] or head["current_series_fingerprint"] != candidate["current_series_fingerprint"]:
        raise ValueError("authority event is not bound to the accepted candidate")
    overlay = value.get("reopen_review_overlay")
    if overlay is None:
        if head != active: raise ValueError("authority head is inconsistent")
    else:
        signed = _signed_delta(overlay.get("signed_delta"))
        if signed is None or overlay.get("delta_fingerprint") != signed["canonical_delta_fingerprint"] or head["event_type"] != "reopen" or head["delta_fingerprint"] != signed["canonical_delta_fingerprint"] or head["active_parent_event_id"] != active["event_id"] or head["active_parent_event_fingerprint"] != active["event_fingerprint"]:
            raise ValueError("authority review overlay is inconsistent")
    return {
        "candidate": candidate,
        "head": head["event_fingerprint"],
        "kind": active["event_type"],
        "overlay": None
        if overlay is None
        else {
            "delta_fingerprint": overlay["delta_fingerprint"],
            "workset_partition_fingerprint": overlay.get("workset_partition_fingerprint"),
        },
        "review_state": None if overlay is None else overlay["review_state"],
        # Kept beside the overlay: _same_review_overlay compares the overlay dict exactly.
        "review_scan_id": None if overlay is None else head["request"]["source_scan_id"],
        "rejected_candidate_id": None if overlay is None else overlay.get("candidate_id"),
    }

def _candidate(value: Any) -> dict[str, str] | None:
    try:
        if type(value) is not dict:
            raise ValueError
        keys = "id source_scan_id canonical_plan_fingerprint complete_record_fingerprint assessment_fingerprint score_fingerprint".split()
        result = {key: value[key] for key in keys}
        if type(result["source_scan_id"]) is not str or str(UUID(result["id"])) != result["id"] or any(type(result[key]) is not str or evaluation._sha(result[key]) != result[key] for key in result if key.endswith("fingerprint")):
            raise ValueError
        full = "evaluation_bundle_fingerprint current_series_fingerprint candidate_series_fingerprint".split()
        if any(key in value for key in full):
            if any(key not in value or type(value[key]) is not str or evaluation._sha(value[key]) != value[key] for key in full) or authority_event.candidate_complete_record_fingerprint(value) != result["complete_record_fingerprint"]: raise ValueError
            result |= {key: value[key] for key in full}
        return result
    except (authority_event.EvidenceVaultSv9AuthorityEventError, AttributeError, KeyError, TypeError, ValueError):
        return None

def _signed_delta(value: Any) -> dict[str, Any] | None:
    try:
        return delta.validate_evidence_vault_sv9_judgment_delta(value)
    except Exception:
        return None


def _workset_partition(value: Any, signed: Mapping[str, Any] | None) -> dict[str, Any] | None:
    try:
        if signed is None:
            raise ValueError
        partition = partitioning.validate_evidence_vault_sv9_workset_partition(value)
        if (
            partition["judgment_delta"] != signed
            or partition["input_binding"]["canonical_delta_fingerprint"]
            != signed["canonical_delta_fingerprint"]
        ):
            raise ValueError
        return partition
    except Exception:
        return None

def _matches(details: Mapping[str, Any], candidate: Mapping[str, str]) -> bool:
    return all(details["candidate"].get(key) == value for key, value in candidate.items())

def _snapshot(outcome: Mapping[str, Any]) -> tuple[bool, str | None]:
    value = outcome.get("accepted_authority")
    try: return (True, None) if value is None else (True, evaluation._sha(value["current_head_event_fingerprint"]))
    except Exception: return False, None

def _same_snapshot(state: str, details: Mapping[str, Any] | None, predecessor: str | None) -> bool:
    return state == "absent" if predecessor is None else state == "authority" and details["head"] == predecessor


def _same_review_overlay(
    overlay: Mapping[str, Any] | None,
    delta_fingerprint: str,
    workset_partition_fingerprint: str,
) -> bool:
    return overlay == {
        "delta_fingerprint": delta_fingerprint,
        "workset_partition_fingerprint": workset_partition_fingerprint,
    }

def _success(outcome: Mapping[str, Any], authority: Mapping[str, Any], details: Mapping[str, Any], candidate: Mapping[str, str]) -> dict[str, Any]:
    status = "authority_established" if details["kind"] == "adopt" else "authority_advanced"
    return _result(status, outcome, authority, candidate)

def _idempotency(action: str, source: str, candidate: str | None, fingerprint: str | None, predecessor: str | None, workset_partition_fingerprint: str | None = None) -> str:
    request = authority_event.build_evidence_vault_sv9_authority_request(action=action, candidate_id=candidate, expected_predecessor_event_fingerprint=predecessor, delta_fingerprint=fingerprint, source_scan_id=source, workset_partition_fingerprint=workset_partition_fingerprint)
    return authority_event.authority_application_idempotency_fingerprint(request)

def _result(status: str, outcome: Mapping[str, Any], authority: Mapping[str, Any] | None = None, candidate: Mapping[str, str] | None = None, signed_delta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    reasons = outcome.get("reason_codes", [])
    return {"status": status, "reason_codes": list(reasons) if type(reasons) is list and all(type(value) is str for value in reasons) else ["invalid_evaluation_outcome"], "evaluation_status": outcome.get("status"), "candidate": dict(candidate) if candidate else None, "signed_delta": dict(signed_delta) if signed_delta else None, "authority": dict(authority) if authority else None}
# fmt: on
