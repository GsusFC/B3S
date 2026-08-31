"""Retry-safe application of persisted SV9 judgment authority candidates."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_sv9_authority_evaluation as evaluation_service
from src.services import evidence_vault_sv9_judgment_delta as delta
from src.services.evidence_vault_sv9_authoritative_relations import (
    EvidenceVaultSv9AuthoritativeRelationStaleWitnessError,
    EvidenceVaultSv9AuthoritativeRelationWitnessError,
)
from src.services.evidence_vault_sv9_authority_projection import (
    validate_persisted_evidence_vault_sv9_authority_projection,
)
from src.sv9 import incremental_evaluation as evaluation


# fmt: off
class EvidenceVaultSv9JudgmentCandidateLegacyAuthorityError(Exception):
    pass


class EvidenceVaultSv9AuthorityApplicationRepository(evaluation_service.EvidenceVaultSv9AuthorityEvaluationRepository, Protocol):
    def adopt_evidence_vault_sv9_judgment_candidate(self, source_scan_id: str, candidate_id: str, **kwargs: Any) -> tuple[dict[str, Any], bool]: ...
    def reopen_evidence_vault_sv9_judgment_authority(self, source_scan_id: str, signed_delta: Mapping[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]: ...

def run_evidence_vault_sv9_authority_application(*, repository: EvidenceVaultSv9AuthorityApplicationRepository, flow: evaluation.Sv9StrictComponentFlowPort, domain_or_url: str, source_scan_id: str, current_evidence: list[Mapping[str, Any]], authoritative_relations: list[Mapping[str, Any]], current_series_contract: Mapping[str, Any], workspace_slug: str = "b3s", trusted_irrelevant_evidence: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Evaluate first, then append at most one authority event or fail closed."""
    try:
        outcome = evaluation_service.run_evidence_vault_sv9_authority_evaluation(
            repository=repository, flow=flow, domain_or_url=domain_or_url, source_scan_id=source_scan_id,
            current_evidence=current_evidence, authoritative_relations=authoritative_relations,
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
        return _apply_review(repository, domain_or_url, source_scan_id, workspace_slug, outcome)
    if status == "no_new_score":
        return _result("authority_conflict", outcome) if any(reason in outcome.get("reason_codes", []) for reason in ("invalid_source_identity", "invalid_input")) else _retain(repository, domain_or_url, workspace_slug, outcome)
    return _result("authority_conflict", outcome)

def _apply_candidate(repository, domain: str, source: str, workspace: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _candidate(outcome.get("candidate"))
    valid, predecessor = _snapshot(outcome)
    if candidate is None or candidate["source_scan_id"] != source or not valid:
        return _result("authority_conflict", outcome)
    state, authority, details = _read(repository, domain, workspace)
    if state == "conflict" or (state == "authority" and details["overlay"] is not None):
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
    if signed is None:
        return _result("authority_conflict", outcome, authority)
    fingerprint = signed["canonical_delta_fingerprint"]
    if details["overlay"] == fingerprint:
        return _result("review_required", outcome, authority, signed_delta=signed)
    if not _same_snapshot(state, details, predecessor):
        return _result("authority_conflict", outcome)
    key = _idempotency("reopen_authority", source, None, fingerprint, predecessor)
    try:
        repository.reopen_evidence_vault_sv9_judgment_authority(
            source, signed, expected_predecessor_event_fingerprint=predecessor,
            idempotency_key_hash=key, workspace_slug=workspace,
        )
    except Exception:
        pass
    state, authority, details = _read(repository, domain, workspace)
    if state == "authority" and details["overlay"] == fingerprint:
        return _result("review_required", outcome, authority, signed_delta=signed)
    return _result("authority_conflict", outcome)

def _retain(repository, domain: str, workspace: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    state, authority, _details = _read(repository, domain, workspace)
    if state == "authority" and _details["overlay"] is None:
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
    return {"candidate": candidate, "head": head["event_fingerprint"], "kind": active["event_type"], "overlay": None if overlay is None else overlay["delta_fingerprint"]}

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

def _matches(details: Mapping[str, Any], candidate: Mapping[str, str]) -> bool:
    return all(details["candidate"].get(key) == value for key, value in candidate.items())

def _snapshot(outcome: Mapping[str, Any]) -> tuple[bool, str | None]:
    value = outcome.get("accepted_authority")
    try: return (True, None) if value is None else (True, evaluation._sha(value["current_head_event_fingerprint"]))
    except Exception: return False, None

def _same_snapshot(state: str, details: Mapping[str, Any] | None, predecessor: str | None) -> bool:
    return state == "absent" if predecessor is None else state == "authority" and details["head"] == predecessor

def _success(outcome: Mapping[str, Any], authority: Mapping[str, Any], details: Mapping[str, Any], candidate: Mapping[str, str]) -> dict[str, Any]:
    status = "authority_established" if details["kind"] == "adopt" else "authority_advanced"
    return _result(status, outcome, authority, candidate)

def _idempotency(action: str, source: str, candidate: str | None, fingerprint: str | None, predecessor: str | None) -> str:
    request = authority_event.build_evidence_vault_sv9_authority_request(action=action, candidate_id=candidate, expected_predecessor_event_fingerprint=predecessor, delta_fingerprint=fingerprint, source_scan_id=source)
    return authority_event.authority_application_idempotency_fingerprint(request)

def _result(status: str, outcome: Mapping[str, Any], authority: Mapping[str, Any] | None = None, candidate: Mapping[str, str] | None = None, signed_delta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    reasons = outcome.get("reason_codes", [])
    return {"status": status, "reason_codes": list(reasons) if type(reasons) is list and all(type(value) is str for value in reasons) else ["invalid_evaluation_outcome"], "evaluation_status": outcome.get("status"), "candidate": dict(candidate) if candidate else None, "signed_delta": dict(signed_delta) if signed_delta else None, "authority": dict(authority) if authority else None}
# fmt: on
