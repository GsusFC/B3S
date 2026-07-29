"""Human review gate for evidence-scoring memory recoveries.

The scoring-memory preview proves that an old quote is reproducible and still
belongs to the scanned brand.  It does not prove that the quote satisfies the
semantic contract of the SV9 tile it would recover.  This module freezes that
last mapping as a reviewable subject and resolves append-only review events
without granting runtime or scoring authority.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from src.evidence_identity import stable_artifact_digest
from src.services.evidence_scoring_memory_preview import (
    apply_recovery_review_gate,
    build_evidence_scoring_memory_preview,
)
from src.sv9.rubric import COMPONENTS


EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION = (
    "evidence-scoring-recovery-review-v1"
)
EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION = (
    "evidence-scoring-recovery-review-event-v1"
)
EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION = (
    "evidence-scoring-recovery-review-policy-v1"
)
REVIEW_DECISIONS = frozenset(
    {"accepted", "disputed", "rejected", "revoked"}
)
EVIDENCE_SCORING_REVIEWED_SHADOW_VERSION = (
    "evidence-scoring-reviewed-memory-shadow-v1"
)
SCORING_RECOVERY_REVIEW_SUBJECT_TYPE = "scoring_recovery"


class EvidenceScoringRecoveryReviewError(ValueError):
    """Recovery candidates or review events violate the review contract."""


class EvidenceScoringRecoveryJournalError(RuntimeError):
    """Base error for the durable semantic-recovery review boundary."""


class EvidenceScoringRecoveryReviewUnavailableError(
    EvidenceScoringRecoveryJournalError
):
    """The durable semantic-recovery review journal is unavailable."""


class EvidenceScoringRecoveryReviewNotFoundError(
    EvidenceScoringRecoveryJournalError
):
    """The brand or semantic recovery subject does not exist."""


class EvidenceScoringRecoveryReviewInvalidTransitionError(
    EvidenceScoringRecoveryJournalError
):
    """The requested decision cannot follow the current journal event."""


class EvidenceScoringRecoveryReviewConflictError(
    EvidenceScoringRecoveryJournalError
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
class EvidenceScoringRecoveryReviewCommand:
    """Validated command passed to the append-only PostgreSQL journal."""

    subject_id: str
    case_id: str
    decision: str
    expected_current_event_id: str | None
    reviewer: str
    reason_code: str
    rationale: str
    evaluator_version: str
    actor_id: str
    idempotency_key_hash: str
    request_fingerprint: str


def build_reviewed_scoring_memory_shadow(
    reports: Iterable[dict[str, Any]],
    *,
    lane: str = "history",
    evidence_adjudications: Iterable[dict[str, Any]] = (),
    recovery_review_events: Iterable[dict[str, Any]] = (),
    ignore_stale_review_events: bool = False,
    review_events_are_current: bool = False,
) -> dict[str, Any]:
    """Build candidate and fail-closed reviewed scores from one report history."""

    rows = [
        dict(report)
        for report in reports
        if isinstance(report, dict)
    ]
    preview = build_evidence_scoring_memory_preview(
        rows,
        mode="shadow",
        evidence_adjudications=evidence_adjudications,
    )
    candidates = build_recovery_review_candidates(
        [{"lane": lane, "preview": preview}]
    )
    stored_events = [
        dict(event)
        for event in recovery_review_events
        if isinstance(event, dict)
    ]
    candidate_fingerprints = {
        str(candidate["case_id"]): str(
            candidate["candidate_fingerprint"]
        )
        for candidate in candidates
    }
    stale_events: list[dict[str, Any]] = []
    applicable_events: list[dict[str, Any]] = []
    for event in stored_events:
        case_id = str(event.get("case_id") or "")
        expected = candidate_fingerprints.get(case_id)
        if (
            ignore_stale_review_events
            and (
                expected is None
                or str(event.get("candidate_fingerprint") or "")
                != expected
            )
        ):
            stale_events.append(event)
        else:
            applicable_events.append(event)
    review = evaluate_recovery_reviews(
        candidates,
        applicable_events,
        events_are_current=review_events_are_current,
    )
    review["journal"] = {
        "stored_event_count": len(stored_events),
        "applicable_event_count": len(applicable_events),
        "stale_event_count": len(stale_events),
        "stale_event_ids": sorted(
            str(
                event.get("event_id")
                or event.get("id")
                or ""
            )
            for event in stale_events
            if str(
                event.get("event_id")
                or event.get("id")
                or ""
            )
        ),
    }
    latest_report_id = str(preview.get("latest_report_id") or "")
    latest_report = next(
        (
            report
            for report in rows
            if str(report.get("id") or "") == latest_report_id
        ),
        None,
    )
    result = (
        apply_recovery_review_gate(
            preview,
            latest_report,
            accepted_tile_evidence_ids=review[
                "accepted_tile_evidence_ids"
            ],
        )
        if isinstance(latest_report, dict)
        else dict(preview)
    )
    result["recovery_review_candidates"] = candidates
    result["recovery_review"] = review
    result["reviewed_shadow_version"] = (
        EVIDENCE_SCORING_REVIEWED_SHADOW_VERSION
    )
    result["state_fingerprint"] = stable_artifact_digest(
        EVIDENCE_SCORING_REVIEWED_SHADOW_VERSION,
        {
            key: value
            for key, value in result.items()
            if key != "state_fingerprint"
        },
    )
    return result


def build_recovery_review_candidates(
    preview_lanes: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Freeze unique evidence-to-tile recovery mappings across preview lanes."""

    accumulators: dict[str, dict[str, Any]] = {}
    for lane_row in preview_lanes:
        if not isinstance(lane_row, dict):
            continue
        lane = str(lane_row.get("lane") or "").strip()
        preview = lane_row.get("preview")
        if not lane or not isinstance(preview, dict):
            continue
        if (
            preview.get("runtime_effect") is not False
            or preview.get("authority") is not False
        ):
            raise EvidenceScoringRecoveryReviewError(
                "recovery review requires a non-authoritative preview"
            )
        brand = (
            preview.get("brand")
            if isinstance(preview.get("brand"), dict)
            else {}
        )
        domain = str(brand.get("domain") or "").strip().lower()
        if not domain:
            continue
        evidence_by_id = {
            str(row.get("tile_evidence_id") or ""): row
            for row in preview.get("accepted_evidence") or []
            if isinstance(row, dict)
            and str(row.get("tile_evidence_id") or "")
        }
        for recovery in preview.get("recoveries") or []:
            if not isinstance(recovery, dict):
                continue
            component_key = str(
                recovery.get("component_key") or ""
            ).strip()
            tile_id = str(recovery.get("tile_id") or "").strip()
            tile_spec = _tile_spec(component_key, tile_id)
            for tile_evidence_id in sorted(
                {
                    str(value)
                    for value in recovery.get("tile_evidence_ids") or []
                    if str(value)
                }
            ):
                evidence = evidence_by_id.get(tile_evidence_id)
                if evidence is None:
                    raise EvidenceScoringRecoveryReviewError(
                        "recovery references evidence absent from preview"
                    )
                subject = {
                    "brand_domain": domain,
                    "rubric_version": str(
                        preview.get("rubric_version") or ""
                    ),
                    "component_key": component_key,
                    "tile_id": tile_id,
                    "tile_name": str(tile_spec["name"]),
                    "tile_condition": str(tile_spec["condition"]),
                    "tile_evidence_contract": dict(
                        tile_spec["evidence_contract"]
                    ),
                    "tile_evidence_id": tile_evidence_id,
                    "quote": str(evidence.get("quote") or ""),
                    "source_urls": sorted(
                        str(value)
                        for value in evidence.get("source_urls") or []
                        if str(value)
                    ),
                    "source_evidence_ids": sorted(
                        str(value)
                        for value in evidence.get(
                            "source_evidence_ids"
                        )
                        or []
                        if str(value)
                    ),
                }
                candidate_fingerprint = stable_artifact_digest(
                    EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION,
                    subject,
                )
                case_id = (
                    "scoring-recovery-"
                    f"{domain.replace('.', '-')}-"
                    f"{component_key}-{tile_id.lower()}-"
                    f"{candidate_fingerprint[:12]}"
                )
                accumulator = accumulators.setdefault(
                    case_id,
                    {
                        "schema_version": (
                            EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION
                        ),
                        "policy_version": (
                            EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION
                        ),
                        "case_id": case_id,
                        "candidate_fingerprint": candidate_fingerprint,
                        "brand": {
                            "name": str(brand.get("name") or ""),
                            "domain": domain,
                        },
                        "rubric_version": str(
                            preview.get("rubric_version") or ""
                        ),
                        "tile": {
                            "component_key": component_key,
                            "tile_id": tile_id,
                            "tile_key": f"{component_key}.{tile_id}",
                            "name": str(tile_spec["name"]),
                            "condition": str(tile_spec["condition"]),
                            "evidence_contract": dict(
                                tile_spec["evidence_contract"]
                            ),
                            "latest_state": str(
                                recovery.get("latest_state") or ""
                            ),
                            "proposed_state": str(
                                recovery.get("preview_state") or ""
                            ),
                        },
                        "evidence": {
                            "tile_evidence_id": tile_evidence_id,
                            "quote": str(evidence.get("quote") or ""),
                            "source_urls": list(subject["source_urls"]),
                            "source_classes": sorted(
                                str(value)
                                for value in evidence.get(
                                    "source_classes"
                                )
                                or []
                                if str(value)
                            ),
                            "source_evidence_ids": list(
                                subject["source_evidence_ids"]
                            ),
                            "acceptance_basis": sorted(
                                str(value)
                                for value in evidence.get(
                                    "acceptance_basis"
                                )
                                or []
                                if str(value)
                            ),
                            "first_seen_at": str(
                                evidence.get("first_seen_at") or ""
                            ),
                            "last_seen_at": str(
                                evidence.get("last_seen_at") or ""
                            ),
                            "observation_count": int(
                                evidence.get("observation_count") or 0
                            ),
                        },
                        "contexts": [],
                        "review_prompt": (
                            "¿La evidencia citada satisface específicamente "
                            "el contrato semántico del tile y puede "
                            "recuperarlo cuando el escaneo actual queda "
                            "sin evidencia?"
                        ),
                        "runtime_effect": False,
                        "authority": False,
                    },
                )
                context = {
                    "lane": lane,
                    "latest_report_id": (
                        str(preview.get("latest_report_id") or "")
                        or None
                    ),
                    "latest_state": str(
                        recovery.get("latest_state") or ""
                    ),
                    "proposed_state": str(
                        recovery.get("preview_state") or ""
                    ),
                }
                if context not in accumulator["contexts"]:
                    accumulator["contexts"].append(context)

    candidates = list(accumulators.values())
    for candidate in candidates:
        candidate["contexts"].sort(
            key=lambda row: (
                str(row["lane"]),
                str(row.get("latest_report_id") or ""),
            )
        )
        _validate_candidate(candidate)
    return sorted(candidates, key=lambda row: str(row["case_id"]))


def recovery_review_subject(
    candidates: Iterable[dict[str, Any]],
    subject_id: str,
) -> dict[str, Any] | None:
    """Return one currently projected semantic mapping by its fingerprint."""

    normalized = str(subject_id or "").strip().lower()
    return next(
        (
            dict(candidate)
            for candidate in candidates
            if isinstance(candidate, dict)
            and str(
                candidate.get("candidate_fingerprint") or ""
            ).strip().lower()
            == normalized
        ),
        None,
    )


def build_recovery_review_template(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return unsigned first events; generated proposals are not decisions."""

    rows = [dict(candidate) for candidate in candidates]
    for row in rows:
        _validate_candidate(row)
    return [
        {
            "schema_version": (
                EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION
            ),
            "case_id": str(candidate["case_id"]),
            "candidate_fingerprint": str(
                candidate["candidate_fingerprint"]
            ),
            "event_id": None,
            "sequence": 1,
            "previous_event_id": None,
            "decision": None,
            "reviewer_id": None,
            "rationale": None,
            "reviewed_at": None,
            "runtime_effect": False,
            "authority": False,
        }
        for candidate in sorted(
            rows,
            key=lambda row: str(row["case_id"]),
        )
    ]


def load_recovery_review_events(
    path: str | Path | None,
) -> list[dict[str, Any]]:
    """Load append-only review events; a missing optional path means no review."""

    if path is None:
        return []
    target = Path(path)
    if not target.is_file():
        raise EvidenceScoringRecoveryReviewError(
            f"recovery review JSONL does not exist: {target}"
        )
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        target.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceScoringRecoveryReviewError(
                f"invalid recovery review JSONL at line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise EvidenceScoringRecoveryReviewError(
                f"recovery review line {line_number} must be an object"
            )
        rows.append(row)
    return rows


def evaluate_recovery_reviews(
    candidates: Iterable[dict[str, Any]],
    review_events: Iterable[dict[str, Any]],
    *,
    events_are_current: bool = False,
) -> dict[str, Any]:
    """Resolve full chains or durable current projections fail-closed."""

    candidate_rows = [dict(row) for row in candidates]
    event_rows = [dict(row) for row in review_events]
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for candidate in candidate_rows:
        _validate_candidate(candidate)
        case_id = str(candidate["case_id"])
        if case_id in candidates_by_id:
            raise EvidenceScoringRecoveryReviewError(
                f"duplicate recovery candidate case_id: {case_id}"
            )
        candidates_by_id[case_id] = candidate

    events_by_case: dict[str, list[dict[str, Any]]] = {}
    seen_event_ids: set[str] = set()
    for event in event_rows:
        _validate_event(event)
        event_id = str(event["event_id"])
        if event_id in seen_event_ids:
            raise EvidenceScoringRecoveryReviewError(
                f"duplicate recovery review event_id: {event_id}"
            )
        seen_event_ids.add(event_id)
        case_id = str(event["case_id"])
        candidate = candidates_by_id.get(case_id)
        if candidate is None:
            raise EvidenceScoringRecoveryReviewError(
                f"recovery review references unknown case: {case_id}"
            )
        if (
            str(event["candidate_fingerprint"])
            != str(candidate["candidate_fingerprint"])
        ):
            raise EvidenceScoringRecoveryReviewError(
                f"recovery review candidate fingerprint mismatch: {case_id}"
            )
        events_by_case.setdefault(case_id, []).append(event)

    current_by_case: dict[str, dict[str, Any]] = {}
    for case_id, events in events_by_case.items():
        if events_are_current:
            if len(events) != 1:
                raise EvidenceScoringRecoveryReviewError(
                    "durable recovery review projection contains "
                    f"multiple current events for case: {case_id}"
                )
            event = events[0]
            sequence = int(event["sequence"])
            previous_event_id = (
                str(event["previous_event_id"])
                if event.get("previous_event_id")
                else None
            )
            if (
                (sequence == 1 and previous_event_id is not None)
                or (sequence > 1 and previous_event_id is None)
                or (
                    str(event["decision"]) == "revoked"
                    and sequence == 1
                )
            ):
                raise EvidenceScoringRecoveryReviewError(
                    "durable recovery review projection has invalid "
                    f"sequence metadata for case: {case_id}"
                )
            current_by_case[case_id] = event
            continue
        ordered = sorted(
            events,
            key=lambda row: (
                int(row["sequence"]),
                str(row["reviewed_at"]),
                str(row["event_id"]),
            ),
        )
        previous: dict[str, Any] | None = None
        for expected_sequence, event in enumerate(ordered, start=1):
            if int(event["sequence"]) != expected_sequence:
                raise EvidenceScoringRecoveryReviewError(
                    f"recovery review sequence gap for case: {case_id}"
                )
            expected_previous = (
                str(previous["event_id"]) if previous else None
            )
            actual_previous = (
                str(event["previous_event_id"])
                if event.get("previous_event_id")
                else None
            )
            if actual_previous != expected_previous:
                raise EvidenceScoringRecoveryReviewError(
                    f"recovery review predecessor mismatch: {case_id}"
                )
            if (
                str(event["decision"]) == "revoked"
                and (
                    previous is None
                    or str(previous["decision"]) == "revoked"
                )
            ):
                raise EvidenceScoringRecoveryReviewError(
                    f"invalid recovery review revocation: {case_id}"
                )
            previous = event
        if previous is not None:
            current_by_case[case_id] = previous

    decision_counts: Counter[str] = Counter()
    pending_case_ids: list[str] = []
    evaluated: list[dict[str, Any]] = []
    accepted_evidence_ids: set[str] = set()
    for case_id in sorted(candidates_by_id):
        candidate = candidates_by_id[case_id]
        event = current_by_case.get(case_id)
        decision = (
            str(event["decision"]) if event is not None else "pending"
        )
        effective_decision = (
            "pending" if decision == "revoked" else decision
        )
        decision_counts[decision] += 1
        if effective_decision == "pending":
            pending_case_ids.append(case_id)
        if effective_decision == "accepted":
            accepted_evidence_ids.add(
                str(candidate["evidence"]["tile_evidence_id"])
            )
        evaluated.append(
            {
                "case_id": case_id,
                "candidate_fingerprint": str(
                    candidate["candidate_fingerprint"]
                ),
                "brand_domain": str(candidate["brand"]["domain"]),
                "tile_key": str(candidate["tile"]["tile_key"]),
                "tile_evidence_id": str(
                    candidate["evidence"]["tile_evidence_id"]
                ),
                "decision": effective_decision,
                "current_event_decision": decision,
                "event_id": (
                    str(event["event_id"]) if event is not None else None
                ),
                "reviewer_id": (
                    str(event["reviewer_id"])
                    if event is not None
                    else None
                ),
            }
        )

    reviewed_count = sum(
        count
        for decision, count in decision_counts.items()
        if decision not in {"pending", "revoked"}
    )
    non_accept_count = int(
        decision_counts["disputed"] + decision_counts["rejected"]
    )
    review_gate_ready = not pending_case_ids
    blockers: list[str] = []
    if pending_case_ids:
        blockers.append("recovery_semantic_reviews_incomplete")
    if non_accept_count:
        blockers.append("recovery_semantic_non_accepts_present")
    blockers.extend(
        [
            "runtime_scoring_wiring_disabled",
            "promotion_policy_not_adopted",
        ]
    )
    return {
        "schema_version": EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION,
        "event_schema_version": (
            EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION
        ),
        "policy_version": (
            EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION
        ),
        "candidate_set_fingerprint": recovery_candidate_fingerprint(
            candidate_rows
        ),
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "review_event_mode": (
            "current_projection"
            if events_are_current
            else "full_chain"
        ),
        "review_gate_ready": review_gate_ready,
        "promotion_ready": False,
        "promotion_blockers": sorted(set(blockers)),
        "summary": {
            "candidate_count": len(candidates_by_id),
            "event_count": len(event_rows),
            "reviewed_count": reviewed_count,
            "pending_count": len(pending_case_ids),
            "accepted_count": int(decision_counts["accepted"]),
            "disputed_count": int(decision_counts["disputed"]),
            "rejected_count": int(decision_counts["rejected"]),
            "revoked_count": int(decision_counts["revoked"]),
            "accepted_evidence_count": len(accepted_evidence_ids),
        },
        "accepted_tile_evidence_ids": sorted(accepted_evidence_ids),
        "pending_case_ids": pending_case_ids,
        "evaluated": evaluated,
    }


def recovery_candidate_fingerprint(
    candidates: Iterable[dict[str, Any]],
) -> str:
    canonical = json.dumps(
        sorted(
            (dict(row) for row in candidates),
            key=lambda row: str(row.get("case_id") or ""),
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tile_spec(component_key: str, tile_id: str) -> dict[str, Any]:
    component = COMPONENTS.get(component_key)
    if not isinstance(component, dict):
        raise EvidenceScoringRecoveryReviewError(
            f"unknown recovery component: {component_key}"
        )
    for tile in component.get("tiles") or []:
        if isinstance(tile, dict) and str(tile.get("id") or "") == tile_id:
            contract = tile.get("evidence_contract")
            if not isinstance(contract, dict):
                break
            return tile
    raise EvidenceScoringRecoveryReviewError(
        f"unknown recovery tile: {component_key}.{tile_id}"
    )


def _validate_candidate(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "policy_version",
        "case_id",
        "candidate_fingerprint",
        "brand",
        "rubric_version",
        "tile",
        "evidence",
        "contexts",
        "review_prompt",
        "runtime_effect",
        "authority",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceScoringRecoveryReviewError(
            f"recovery candidate missing fields: {', '.join(missing)}"
        )
    if row["schema_version"] != EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION:
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate schema version mismatch"
        )
    if (
        row["policy_version"]
        != EVIDENCE_SCORING_RECOVERY_REVIEW_POLICY_VERSION
    ):
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate policy version mismatch"
        )
    if row["runtime_effect"] is not False or row["authority"] is not False:
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidates must remain non-authoritative"
        )
    brand = row.get("brand")
    tile = row.get("tile")
    evidence = row.get("evidence")
    if not isinstance(brand, dict) or not str(
        brand.get("domain") or ""
    ).strip():
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate brand domain is required"
        )
    if not isinstance(tile, dict):
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate tile is required"
        )
    for field in (
        "component_key",
        "tile_id",
        "tile_key",
        "name",
        "condition",
        "evidence_contract",
        "latest_state",
        "proposed_state",
    ):
        if not tile.get(field):
            raise EvidenceScoringRecoveryReviewError(
                f"recovery candidate tile {field} is required"
            )
    _tile_spec(str(tile["component_key"]), str(tile["tile_id"]))
    if not isinstance(evidence, dict):
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate evidence is required"
        )
    for field in ("tile_evidence_id", "quote", "source_evidence_ids"):
        if not evidence.get(field):
            raise EvidenceScoringRecoveryReviewError(
                f"recovery candidate evidence {field} is required"
            )
    if not isinstance(row.get("contexts"), list) or not row["contexts"]:
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate context is required"
        )
    expected_fingerprint = stable_artifact_digest(
        EVIDENCE_SCORING_RECOVERY_REVIEW_VERSION,
        {
            "brand_domain": str(brand["domain"]).strip().lower(),
            "rubric_version": str(row["rubric_version"]),
            "component_key": str(tile["component_key"]),
            "tile_id": str(tile["tile_id"]),
            "tile_name": str(tile["name"]),
            "tile_condition": str(tile["condition"]),
            "tile_evidence_contract": dict(
                tile["evidence_contract"]
            ),
            "tile_evidence_id": str(evidence["tile_evidence_id"]),
            "quote": str(evidence["quote"]),
            "source_urls": sorted(
                str(value)
                for value in evidence.get("source_urls") or []
                if str(value)
            ),
            "source_evidence_ids": sorted(
                str(value)
                for value in evidence.get("source_evidence_ids") or []
                if str(value)
            ),
        },
    )
    if str(row["candidate_fingerprint"]) != expected_fingerprint:
        raise EvidenceScoringRecoveryReviewError(
            "recovery candidate fingerprint mismatch"
        )


def _validate_event(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "case_id",
        "candidate_fingerprint",
        "event_id",
        "sequence",
        "previous_event_id",
        "decision",
        "reviewer_id",
        "rationale",
        "reviewed_at",
        "runtime_effect",
        "authority",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceScoringRecoveryReviewError(
            f"recovery review event missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION
    ):
        raise EvidenceScoringRecoveryReviewError(
            "recovery review event schema version mismatch"
        )
    if row["decision"] not in REVIEW_DECISIONS:
        raise EvidenceScoringRecoveryReviewError(
            "recovery review decision is invalid"
        )
    if (
        not isinstance(row["sequence"], int)
        or int(row["sequence"]) < 1
    ):
        raise EvidenceScoringRecoveryReviewError(
            "recovery review sequence must be positive"
        )
    for field in (
        "case_id",
        "candidate_fingerprint",
        "event_id",
        "reviewer_id",
        "rationale",
        "reviewed_at",
    ):
        if not str(row.get(field) or "").strip():
            raise EvidenceScoringRecoveryReviewError(
                f"recovery review {field} is required"
            )
    if row["runtime_effect"] is not False or row["authority"] is not False:
        raise EvidenceScoringRecoveryReviewError(
            "recovery reviews must remain non-authoritative"
        )
    try:
        datetime.fromisoformat(
            str(row["reviewed_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceScoringRecoveryReviewError(
            "recovery review reviewed_at must be ISO-8601"
        ) from exc
