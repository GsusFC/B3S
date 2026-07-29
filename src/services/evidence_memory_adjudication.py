"""Versioned, reversible identity adjudications for evidence-memory Gate 1."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable


EVIDENCE_MEMORY_ADJUDICATION_VERSION = "evidence-memory-adjudication-v1"
EVIDENCE_MEMORY_ADJUDICATION_POLICY_VERSION = (
    "evidence-memory-identity-adjudication-policy-v1"
)
ADJUDICATION_DECISIONS = frozenset(
    {"accepted", "disputed", "rejected", "revoked"}
)
ADJUDICATION_SUBJECT_TYPE = "evidence"


class EvidenceMemoryAdjudicationError(RuntimeError):
    """Base error for the durable evidence identity adjudication boundary."""


class EvidenceMemoryAdjudicationUnavailableError(
    EvidenceMemoryAdjudicationError
):
    """The durable journal is not configured or cannot be reached."""


class EvidenceMemoryAdjudicationNotFoundError(
    EvidenceMemoryAdjudicationError
):
    """The brand or evidence subject does not exist in durable history."""


class EvidenceMemoryAdjudicationInvalidTransitionError(
    EvidenceMemoryAdjudicationError
):
    """The requested decision cannot follow the current journal state."""


class EvidenceMemoryAdjudicationConflictError(
    EvidenceMemoryAdjudicationError
):
    """An optimistic concurrency or idempotency precondition failed."""

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
class EvidenceMemoryAdjudicationCommand:
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


def apply_evidence_memory_adjudications(
    projection: dict[str, Any],
    events: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Overlay current identity decisions without granting runtime authority."""

    result = deepcopy(projection)
    ordered_events = sorted(
        (
            dict(event)
            for event in events
            if isinstance(event, dict)
            and str(event.get("subject_type") or "") == ADJUDICATION_SUBJECT_TYPE
        ),
        key=lambda event: (
            int(event.get("sequence") or 0),
            str(event.get("created_at") or ""),
            str(event.get("id") or ""),
        ),
    )
    current_by_subject: dict[str, dict[str, Any]] = {}
    for event in ordered_events:
        subject_id = str(event.get("subject_id") or "")
        decision = str(event.get("decision") or "")
        if subject_id and decision in ADJUDICATION_DECISIONS:
            current_by_subject[subject_id] = event

    state_counts: Counter[str] = Counter()
    adjudicated_count = 0
    for entry in result.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        event = current_by_subject.get(str(entry.get("evidence_id") or ""))
        if event is None:
            entry["adjudication_state"] = "proposed"
            entry.pop("adjudication", None)
        else:
            decision = str(event["decision"])
            entry["adjudication_state"] = decision
            entry["adjudication"] = _public_event(event)
            adjudicated_count += 1
        state_counts[str(entry["adjudication_state"])] += 1

    summary = result.setdefault("summary", {})
    summary["adjudicated_entry_count"] = adjudicated_count
    summary["adjudication_state_counts"] = dict(sorted(state_counts.items()))
    result["adjudication"] = {
        "schema_version": EVIDENCE_MEMORY_ADJUDICATION_VERSION,
        "policy_version": EVIDENCE_MEMORY_ADJUDICATION_POLICY_VERSION,
        "runtime_effect": False,
        "authority": False,
        "event_count": len(ordered_events),
        "current_decision_count": len(current_by_subject),
    }
    return result


def evidence_subject_exists(
    projection: dict[str, Any],
    subject_id: str,
) -> bool:
    """Return whether a subject is present in the immutable-history projection."""

    normalized = str(subject_id or "").strip().lower()
    return any(
        isinstance(entry, dict)
        and str(entry.get("evidence_id") or "").strip().lower() == normalized
        for entry in projection.get("entries") or []
    )


def current_adjudications(
    events: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the latest event per evidence subject in deterministic order."""

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
        if key[0] == ADJUDICATION_SUBJECT_TYPE and key[1]
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
        "policy_version": str(event.get("policy_version") or ""),
        "evaluator_version": str(event.get("evaluator_version") or ""),
        "reviewer": str(event.get("reviewer") or ""),
        "reason_code": str(event.get("reason_code") or ""),
        "rationale": str(event.get("rationale") or ""),
        "created_at": str(event.get("created_at") or ""),
        "runtime_effect": False,
        "authority": False,
    }
