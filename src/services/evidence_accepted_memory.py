"""Current accepted-evidence memory candidate over immutable history.

The projection makes one narrow guarantee explicit: a later acquisition miss
or an unreviewed content variant cannot silently replace evidence that has an
active human ``accepted`` adjudication. Only a later attributable review event
can change the accepted set.

This is still a shadow candidate. It has no runtime, canonical-selection, or
scoring authority.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from typing import Any, Iterable

from src.evidence_identity import stable_artifact_digest
from src.services.evidence_memory_adjudication import (
    ADJUDICATION_DECISIONS,
    current_adjudications,
)
from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)


EVIDENCE_ACCEPTED_MEMORY_VERSION = "evidence-accepted-memory-v1"
EVIDENCE_ACCEPTED_MEMORY_POLICY_VERSION = (
    "evidence-accepted-memory-policy-v1"
)
EVIDENCE_ACCEPTED_MEMORY_SEMANTIC_VERSION = (
    "evidence-accepted-memory-semantic-version-v1"
)


class EvidenceAcceptedMemoryError(ValueError):
    """Accepted-memory input cannot satisfy the fail-closed contract."""


def build_evidence_accepted_memory(
    reports: Iterable[dict[str, Any]],
    *,
    mode: str = "shadow",
    evidence_adjudications: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Select only evidence with an active explicit acceptance.

    The semantic version excludes observation counts, latest-presence state,
    timestamps, and review-event metadata. Those diagnostics may change while
    the accepted evidence set remains identical.
    """

    report_rows = [
        dict(report)
        for report in reports
        if isinstance(report, dict)
    ]
    adjudication_rows = [
        dict(event)
        for event in evidence_adjudications
        if isinstance(event, dict)
    ]
    effective_mode = (
        "shadow"
        if str(mode or "").strip().lower() == "shadow"
        else "disabled"
    )
    result: dict[str, Any] = {
        "schema_version": EVIDENCE_ACCEPTED_MEMORY_VERSION,
        "policy_version": EVIDENCE_ACCEPTED_MEMORY_POLICY_VERSION,
        "mode": effective_mode,
        "memory_kind": "accepted_evidence_candidate",
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "canonical_evidence_available": False,
        "selection_basis": (
            "active_explicit_human_acceptance_only"
        ),
        "brand": {"name": "", "domain": ""},
        "report_count": len(report_rows),
        "accepted_memory_candidate_version": None,
        "selection_ready": False,
        "promotion_ready": False,
        "summary": _empty_summary(),
        "entries": [],
        "promotion_blockers": [
            "canonical_evidence_policy_not_adopted",
            "accepted_evidence_candidate_has_no_runtime_authority",
        ],
        "warnings": [
            "unreviewed_evidence_is_excluded",
            "acquisition_absence_does_not_remove_accepted_evidence",
            "new_content_variant_does_not_replace_accepted_evidence",
            "explicit_revocation_or_rejection_can_change_current_selection",
            "raw_evidence_content_is_not_returned",
        ],
    }
    if effective_mode != "shadow":
        result["state_fingerprint"] = _state_fingerprint(result)
        return result

    identity = build_evidence_memory_identity_v2(
        report_rows,
        mode="shadow",
        adjudications=adjudication_rows,
    )
    identity_entries = [
        entry
        for entry in identity.get("entries") or []
        if isinstance(entry, dict)
        and str(entry.get("evidence_id") or "")
    ]
    evidence_ids = {
        str(entry["evidence_id"]) for entry in identity_entries
    }
    active_events = current_adjudications(adjudication_rows)
    _validate_current_events(active_events, evidence_ids=evidence_ids)

    accepted_entries = sorted(
        (
            _accepted_entry(entry)
            for entry in identity_entries
            if str(entry.get("adjudication_state") or "")
            == "accepted"
        ),
        key=lambda entry: entry["evidence_id"],
    )
    material = [_semantic_entry(entry) for entry in accepted_entries]
    brand = {
        "name": str((identity.get("brand") or {}).get("name") or ""),
        "domain": str(
            (identity.get("brand") or {}).get("domain") or ""
        ),
    }
    candidate_version = stable_artifact_digest(
        EVIDENCE_ACCEPTED_MEMORY_SEMANTIC_VERSION,
        {
            "brand_domain": brand["domain"].strip().casefold(),
            "accepted_evidence": material,
        },
    )
    result.update(
        {
            "brand": brand,
            "accepted_memory_candidate_version": candidate_version,
            "selection_ready": bool(accepted_entries),
            "summary": _summary(
                identity_entries,
                accepted_entries,
            ),
            "entries": accepted_entries,
        }
    )
    if not accepted_entries:
        result["promotion_blockers"].append(
            "no_active_accepted_evidence"
        )
    result["promotion_blockers"] = sorted(
        set(result["promotion_blockers"])
    )
    result["state_fingerprint"] = _state_fingerprint(result)
    return result


def _validate_current_events(
    events: Iterable[dict[str, Any]],
    *,
    evidence_ids: set[str],
) -> None:
    invalid_events: list[str] = []
    for event in events:
        event_id = str(event.get("id") or "").strip()
        missing = [
            field
            for field in (
                "reviewer",
                "reason_code",
                "rationale",
                "evaluator_version",
                "created_at",
            )
            if not str(event.get(field) or "").strip()
        ]
        if not event_id:
            missing.append("id")
        try:
            sequence = int(event.get("sequence") or 0)
        except (TypeError, ValueError):
            sequence = 0
        if sequence < 1:
            missing.append("sequence")
        if not _valid_timestamp(event.get("created_at")):
            missing.append("valid_created_at")
        if event.get("runtime_effect") is not False:
            missing.append("runtime_effect=false")
        if event.get("authority") is not False:
            missing.append("authority=false")
        if missing:
            invalid_events.append(
                f"{event_id or '<missing-id>'}:"
                + ",".join(sorted(set(missing)))
            )
    if invalid_events:
        raise EvidenceAcceptedMemoryError(
            "current adjudications are not attributable and "
            "non-authoritative: "
            + "; ".join(invalid_events)
        )
    invalid_decisions = sorted(
        {
            str(event.get("decision") or "")
            for event in events
            if str(event.get("decision") or "")
            not in ADJUDICATION_DECISIONS
        }
    )
    if invalid_decisions:
        raise EvidenceAcceptedMemoryError(
            "unsupported current evidence decisions: "
            + ", ".join(invalid_decisions)
        )
    unknown_subjects = sorted(
        {
            str(event.get("subject_id") or "")
            for event in events
            if str(event.get("subject_id") or "")
            and str(event.get("subject_id") or "")
            not in evidence_ids
        }
    )
    if unknown_subjects:
        raise EvidenceAcceptedMemoryError(
            "current adjudications reference unknown evidence: "
            + ", ".join(unknown_subjects)
        )


def _valid_timestamp(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _accepted_entry(entry: dict[str, Any]) -> dict[str, Any]:
    adjudication = (
        entry.get("adjudication")
        if isinstance(entry.get("adjudication"), dict)
        else {}
    )
    return {
        **_semantic_entry(entry),
        "present_in_latest": bool(entry.get("present_in_latest")),
        "first_seen_at": str(entry.get("first_seen_at") or ""),
        "last_seen_at": str(entry.get("last_seen_at") or ""),
        "observation_count": int(
            entry.get("observation_count") or 0
        ),
        "acceptance": {
            "event_id": str(adjudication.get("id") or ""),
            "sequence": int(adjudication.get("sequence") or 0),
            "reviewer": str(adjudication.get("reviewer") or ""),
            "evaluator_version": str(
                adjudication.get("evaluator_version") or ""
            ),
            "accepted_at": str(
                adjudication.get("created_at") or ""
            ),
        },
    }


def _semantic_entry(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "evidence_id": str(entry.get("evidence_id") or ""),
        "document_id": str(entry.get("document_id") or ""),
        "passage_id": str(entry.get("passage_id") or ""),
        "claim_slot_id": str(entry.get("claim_slot_id") or ""),
        "source_class": str(entry.get("source_class") or ""),
        "evidence_type": str(entry.get("evidence_type") or ""),
        "content_hash": str(entry.get("content_hash") or ""),
    }


def _summary(
    identity_entries: list[dict[str, Any]],
    accepted_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    decision_counts = Counter(
        str(entry.get("adjudication_state") or "proposed")
        for entry in identity_entries
    )
    accepted_by_document: dict[str, list[dict[str, Any]]] = (
        defaultdict(list)
    )
    all_by_document: dict[str, list[dict[str, Any]]] = defaultdict(
        list
    )
    for entry in identity_entries:
        all_by_document[str(entry.get("document_id") or "")].append(
            entry
        )
    for entry in accepted_entries:
        accepted_by_document[str(entry["document_id"])].append(entry)

    accepted_documents_with_unaccepted_variants = sum(
        1
        for document_id, accepted in accepted_by_document.items()
        if len(all_by_document[document_id]) > len(accepted)
    )
    return {
        "observed_evidence_count": len(identity_entries),
        "active_accepted_evidence_count": len(accepted_entries),
        "active_accepted_document_count": len(accepted_by_document),
        "accepted_not_present_in_latest_count": sum(
            1
            for entry in accepted_entries
            if not bool(entry["present_in_latest"])
        ),
        "multi_accepted_variant_document_count": sum(
            1
            for entries in accepted_by_document.values()
            if len(entries) > 1
        ),
        "accepted_document_with_unaccepted_variant_count": (
            accepted_documents_with_unaccepted_variants
        ),
        "adjudication_state_counts": dict(
            sorted(decision_counts.items())
        ),
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "observed_evidence_count": 0,
        "active_accepted_evidence_count": 0,
        "active_accepted_document_count": 0,
        "accepted_not_present_in_latest_count": 0,
        "multi_accepted_variant_document_count": 0,
        "accepted_document_with_unaccepted_variant_count": 0,
        "adjudication_state_counts": {},
    }


def _state_fingerprint(payload: dict[str, Any]) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
