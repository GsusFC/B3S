"""Reversible, non-authoritative decisions over claim-relation candidates."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable


EVIDENCE_CLAIM_RECONCILIATION_VERSION = "evidence-claim-reconciliation-v1"
EVIDENCE_CLAIM_RECONCILIATION_POLICY_VERSION = (
    "evidence-claim-reconciliation-policy-v1"
)
CLAIM_RECONCILIATION_DECISIONS = frozenset(
    {"accepted", "disputed", "rejected", "revoked"}
)
CLAIM_RECONCILIATION_RELATION_TYPES = frozenset(
    {"coexistence_candidate", "replacement_candidate"}
)
CLAIM_RECONCILIATION_SUBJECT_TYPE = "claim_relation"


class EvidenceClaimReconciliationError(RuntimeError):
    """Base error for the durable claim-reconciliation boundary."""


class EvidenceClaimReconciliationUnavailableError(
    EvidenceClaimReconciliationError
):
    """The durable reconciliation journal is unavailable."""


class EvidenceClaimReconciliationNotFoundError(
    EvidenceClaimReconciliationError
):
    """The brand or relation subject does not exist."""


class EvidenceClaimReconciliationInvalidTransitionError(
    EvidenceClaimReconciliationError
):
    """The requested decision cannot follow the current event."""


class EvidenceClaimReconciliationConflictError(
    EvidenceClaimReconciliationError
):
    """An optimistic-concurrency or idempotency precondition failed."""

    def __init__(
        self,
        message: str,
        *,
        current_event_id: str | None = None,
        existing_event_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.current_event_id = current_event_id
        self.existing_event_id = existing_event_id


@dataclass(frozen=True, slots=True)
class EvidenceClaimReconciliationCommand:
    """Validated command passed to the append-only PostgreSQL journal."""

    subject_id: str
    decision: str
    expected_current_event_id: str | None
    reviewer: str
    reason_code: str
    rationale: str
    evaluator_version: str
    actor_id: str
    idempotency_key_hash: str
    request_fingerprint: str


def apply_evidence_claim_reconciliations(
    projection: dict[str, Any],
    events: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Overlay current relation decisions without choosing a canonical claim."""

    result = deepcopy(projection)
    ordered_events = sorted(
        (
            dict(event)
            for event in events
            if isinstance(event, dict)
            and str(event.get("subject_type") or "")
            == CLAIM_RECONCILIATION_SUBJECT_TYPE
        ),
        key=_event_order,
    )
    current_by_subject: dict[str, dict[str, Any]] = {}
    for event in ordered_events:
        subject_id = str(event.get("subject_id") or "")
        decision = str(event.get("decision") or "")
        if subject_id and decision in CLAIM_RECONCILIATION_DECISIONS:
            current_by_subject[subject_id] = event

    state_counts: Counter[str] = Counter()
    adjudicated_count = 0
    relation_type_mismatch_count = 0
    for relation in _relation_candidates(result):
        event = current_by_subject.get(
            str(relation.get("relation_candidate_id") or "")
        )
        if event is not None and str(event.get("relation_type") or "") != str(
            relation.get("relation") or ""
        ):
            event = None
            relation_type_mismatch_count += 1
        if event is None:
            relation["adjudication_state"] = "proposed"
            relation.pop("adjudication", None)
        else:
            relation["adjudication_state"] = str(event["decision"])
            relation["adjudication"] = _public_event(event)
            adjudicated_count += 1
        relation["runtime_effect"] = False
        relation["authority"] = False
        state_counts[str(relation["adjudication_state"])] += 1

    summary = result.setdefault("summary", {})
    summary["adjudicated_relation_count"] = adjudicated_count
    summary["relation_type_mismatch_count"] = (
        relation_type_mismatch_count
    )
    summary["relation_adjudication_state_counts"] = dict(
        sorted(state_counts.items())
    )
    pending_relation_review_count = 0
    for slot in result.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        relations = [
            relation
            for relation in slot.get("relation_candidates") or []
            if isinstance(relation, dict)
        ]
        pending_relations = [
            relation
            for relation in relations
            if str(relation.get("adjudication_state") or "")
            in {"proposed", "disputed", "revoked"}
        ]
        pending_relation_review_count += len(pending_relations)
        slot["requires_human_review"] = bool(
            slot.get("claim_type_conflict")
            or pending_relations
        )
    summary["pending_relation_review_count"] = (
        pending_relation_review_count
    )
    result["claim_reconciliation"] = {
        "schema_version": EVIDENCE_CLAIM_RECONCILIATION_VERSION,
        "policy_version": EVIDENCE_CLAIM_RECONCILIATION_POLICY_VERSION,
        "runtime_effect": False,
        "authority": False,
        "automatic_canonical_selection": False,
        "event_count": len(ordered_events),
        "current_decision_count": len(current_by_subject),
        "applied_current_decision_count": adjudicated_count,
    }
    result["runtime_effect"] = False
    result["authority"] = False
    return result


def claim_relation_subject(
    projection: dict[str, Any],
    subject_id: str,
) -> dict[str, Any] | None:
    """Return one projected relation subject by its stable digest."""

    normalized = str(subject_id or "").strip().lower()
    return next(
        (
            relation
            for relation in _relation_candidates(projection)
            if str(relation.get("relation_candidate_id") or "")
            .strip()
            .lower()
            == normalized
        ),
        None,
    )


def current_claim_reconciliations(
    events: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the latest event per claim-relation subject."""

    current: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        key = (
            str(event.get("subject_type") or ""),
            str(event.get("subject_id") or ""),
        )
        candidate = dict(event)
        previous = current.get(key)
        if previous is None or _event_order(candidate) > _event_order(previous):
            current[key] = candidate
    return [
        current[key]
        for key in sorted(current)
        if key[0] == CLAIM_RECONCILIATION_SUBJECT_TYPE and key[1]
    ]


def _relation_candidates(
    projection: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        relation
        for slot in projection.get("slots") or []
        if isinstance(slot, dict)
        for relation in slot.get("relation_candidates") or []
        if isinstance(relation, dict)
    ]


def _event_order(event: dict[str, Any]) -> tuple[int, str, str]:
    return (
        int(event.get("sequence") or 0),
        str(event.get("created_at") or ""),
        str(event.get("id") or ""),
    )


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(event.get("id") or ""),
        "subject_type": CLAIM_RECONCILIATION_SUBJECT_TYPE,
        "subject_id": str(event.get("subject_id") or ""),
        "relation_type": str(event.get("relation_type") or ""),
        "sequence": int(event.get("sequence") or 0),
        "decision": str(event.get("decision") or ""),
        "effective_state": str(
            event.get("effective_state") or event.get("decision") or ""
        ),
        "supersedes_event_id": (
            str(event["supersedes_event_id"])
            if event.get("supersedes_event_id")
            else None
        ),
        "schema_version": str(event.get("schema_version") or ""),
        "policy_version": str(event.get("policy_version") or ""),
        "evaluator_version": str(event.get("evaluator_version") or ""),
        "reviewer": str(event.get("reviewer") or ""),
        "actor_id": str(event.get("actor_id") or ""),
        "reason_code": str(event.get("reason_code") or ""),
        "rationale": str(event.get("rationale") or ""),
        "runtime_effect": False,
        "authority": False,
        "created_at": str(event.get("created_at") or ""),
    }
