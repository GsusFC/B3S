"""Pure canonical SV9 judgment-authority wrapper contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_sv9_judgment_delta as judgment_delta

# fmt: off
_FIELDS = frozenset("authority authority_scope production_runtime_effect scanner_runtime_effect accepted_candidate accepted_partition assessment score current_head active_authority_event event reopen_review_overlay".split())
_PARTITION_FIELDS = ("candidate_tile_judgments", "candidate_component_sentinels")
_ACTIVE_BINDINGS = (
    ("candidate_id", "id"), ("candidate_complete_record_fingerprint", "complete_record_fingerprint"),
    ("evaluation_bundle_fingerprint", "evaluation_bundle_fingerprint"), ("canonical_plan_fingerprint", "canonical_plan_fingerprint"),
    ("current_series_fingerprint", "current_series_fingerprint"), ("candidate_series_fingerprint", "candidate_series_fingerprint"),
    ("assessment_fingerprint", "assessment_fingerprint"), ("score_fingerprint", "score_fingerprint"),
)
_OVERLAY_FIELDS = frozenset({"review_state", "signed_delta", "delta_fingerprint"})
# fmt: on


class EvidenceVaultSv9AuthorityProjectionError(ValueError):
    """The authority wrapper is not a canonical replayable projection."""


def _fail(label: str) -> None:
    raise EvidenceVaultSv9AuthorityProjectionError(f"SV9 authority projection {label} is invalid.")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        _fail(label)
    try:
        result = json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceVaultSv9AuthorityProjectionError(f"SV9 authority projection {label} is invalid.") from exc
    if type(result) is not dict:
        _fail(label)
    return result


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, allow_nan=False, sort_keys=True) == json.dumps(
        right, ensure_ascii=False, allow_nan=False, sort_keys=True
    )


def _candidate(value: Any) -> dict[str, Any]:
    candidate = _mapping(value, "accepted candidate")
    try:
        complete = authority_event.candidate_complete_record_fingerprint(candidate)
    except authority_event.EvidenceVaultSv9AuthorityEventError as exc:
        raise EvidenceVaultSv9AuthorityProjectionError(
            "SV9 authority projection accepted candidate is invalid."
        ) from exc
    if (
        not {"id", "source_scan_id", "created_at"} <= set(candidate)
        or candidate.get("complete_record_fingerprint") != complete
    ):
        _fail("accepted candidate")
    return candidate


def _event(value: Any, label: str) -> dict[str, Any]:
    try:
        return authority_event.validate_evidence_vault_sv9_authority_event(value)
    except (authority_event.EvidenceVaultSv9AuthorityEventError, TypeError) as exc:
        raise EvidenceVaultSv9AuthorityProjectionError(f"SV9 authority projection {label} is invalid.") from exc


def _active_binding(active: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    if active["event_type"] not in {"adopt", "supersede"} or any(
        active[name] != candidate[field] for name, field in _ACTIVE_BINDINGS
    ):
        _fail("active authority event")
    if active["request"]["source_scan_id"] != candidate["source_scan_id"]:
        _fail("active authority source")


def _overlay(value: Any, head: Mapping[str, Any], active: Mapping[str, Any]) -> Any:
    if value is None:
        if head != active:
            _fail("stable head")
        return None
    overlay = _mapping(value, "review overlay")
    if set(overlay) != _OVERLAY_FIELDS or overlay["review_state"] != "pending":
        _fail("review overlay")
    try:
        signed = judgment_delta.validate_evidence_vault_sv9_judgment_delta(overlay["signed_delta"])
    except judgment_delta.EvidenceVaultSV9JudgmentDeltaError as exc:
        raise EvidenceVaultSv9AuthorityProjectionError("SV9 authority projection review overlay is invalid.") from exc
    fingerprint = signed["canonical_delta_fingerprint"]
    # Repository replay authenticates the omitted prior head; this wrapper binds active-parent state only.
    if (
        not _same(overlay["signed_delta"], signed)
        or overlay["delta_fingerprint"] != fingerprint
        or head["event_type"] != "reopen"
        or head["delta_fingerprint"] != fingerprint
        or head["request"]["delta_fingerprint"] != fingerprint
        or head["active_parent_event_id"] != active["event_id"]
        or head["active_parent_event_fingerprint"] != active["event_fingerprint"]
        or head["current_series_fingerprint"] != active["current_series_fingerprint"]
        or signed["plan"]["current_series_fingerprint"] != active["current_series_fingerprint"]
    ):
        _fail("review overlay binding")
    return overlay


def validate_evidence_vault_sv9_authority_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one complete canonical authority wrapper without mutating it."""
    result = _mapping(value, "wrapper")
    if (
        set(result) != _FIELDS
        or result["authority"] is not True
        or result["authority_scope"] != "b3s-vault"
        or result["production_runtime_effect"] is not False
        or result["scanner_runtime_effect"] is not False
    ):
        _fail("wrapper fields")
    candidate = _candidate(result["accepted_candidate"])
    partition = _mapping(result["accepted_partition"], "accepted partition")
    if not _same(partition, {name: candidate[name] for name in _PARTITION_FIELDS}):
        _fail("accepted partition")
    assessment = _mapping(result["assessment"], "assessment")
    if (
        not _same(assessment, candidate["assessment"])
        or type(result["score"]) not in {int, float}
        or type(assessment.get("sv9_score")) not in {int, float}
        or not _same(result["score"], assessment["sv9_score"])
    ):
        _fail("assessment score")
    active = _event(result["active_authority_event"], "active authority event")
    head = _event(result["current_head"], "current head")
    selected = _event(result["event"], "selected event")
    _active_binding(active, candidate)
    overlay = _overlay(result["reopen_review_overlay"], head, active)
    result.update(
        accepted_candidate=candidate,
        accepted_partition=partition,
        assessment=assessment,
        active_authority_event=active,
        current_head=head,
        event=selected,
        reopen_review_overlay=overlay,
    )
    return result


def validate_persisted_evidence_vault_sv9_authority_projection(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a repository-loaded authority wrapper with persisted audit metadata."""
    result = validate_evidence_vault_sv9_authority_projection(value)
    if any(result[name]["created_at"] is None for name in ("active_authority_event", "current_head", "event")):
        _fail("persisted event audit metadata")
    return result


def build_evidence_vault_sv9_authority_projection(
    *,
    accepted_candidate: Mapping[str, Any],
    current_head: Mapping[str, Any],
    active_authority_event: Mapping[str, Any],
    event: Mapping[str, Any],
    reopen_review_overlay: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the exact repository authority wrapper from canonical state facts."""
    candidate = _mapping(accepted_candidate, "accepted candidate")
    try:
        result = {
            "authority": True,
            "authority_scope": "b3s-vault",
            "production_runtime_effect": False,
            "scanner_runtime_effect": False,
            "accepted_candidate": candidate,
            "accepted_partition": {name: candidate[name] for name in _PARTITION_FIELDS},
            "assessment": candidate["assessment"],
            "score": candidate["assessment"]["sv9_score"],
            "current_head": current_head,
            "active_authority_event": active_authority_event,
            "event": event,
            "reopen_review_overlay": reopen_review_overlay,
        }
    except (KeyError, TypeError) as exc:
        raise EvidenceVaultSv9AuthorityProjectionError(
            "SV9 authority projection accepted candidate is invalid."
        ) from exc
    return validate_evidence_vault_sv9_authority_projection(result)
