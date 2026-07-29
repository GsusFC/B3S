"""Versioned human review set for source identity and independence axes."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


EVIDENCE_SOURCE_REVIEW_SCHEMA_VERSION = "evidence-source-review-v1"
EVIDENCE_SOURCE_REVIEW_EVENT_SCHEMA_VERSION = (
    "evidence-source-review-event-v1"
)
EVIDENCE_SOURCE_REVIEW_DATASET_VERSION = (
    "evidence-source-review-2026-07-v1"
)
IDENTITY_DECISIONS = frozenset({"accepted", "disputed", "rejected"})
PUBLISHER_INDEPENDENCE_DECISIONS = frozenset(
    {"confirmed_independent", "disputed", "excluded"}
)
CLAIM_CORROBORATION_DECISIONS = frozenset(
    {
        "independently_corroborated",
        "mixed",
        "disputed",
        "excluded",
    }
)
DEFAULT_REVIEW_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "evidence_source_review"
    / "v1"
)


class EvidenceSourceReviewSetError(ValueError):
    """The source-review dataset violates its immutable contract."""


def load_review_manifest(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_REVIEW_ROOT / "manifest.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceSourceReviewSetError(
            f"cannot load source-review manifest: {target}"
        ) from exc
    if not isinstance(payload, dict):
        raise EvidenceSourceReviewSetError(
            "source-review manifest must be an object"
        )
    if payload.get("schema_version") != EVIDENCE_SOURCE_REVIEW_SCHEMA_VERSION:
        raise EvidenceSourceReviewSetError(
            "unsupported source-review schema version"
        )
    if (
        payload.get("review_event_schema_version")
        != EVIDENCE_SOURCE_REVIEW_EVENT_SCHEMA_VERSION
    ):
        raise EvidenceSourceReviewSetError(
            "unsupported source-review event schema version"
        )
    if (
        payload.get("dataset_version")
        != EVIDENCE_SOURCE_REVIEW_DATASET_VERSION
    ):
        raise EvidenceSourceReviewSetError(
            "unsupported source-review dataset version"
        )
    return payload


def load_review_candidates(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    target = path or DEFAULT_REVIEW_ROOT / "candidates.jsonl"
    rows = _load_jsonl(target, label="source-review candidate")
    seen: set[str] = set()
    for row in rows:
        _validate_candidate(row)
        case_id = str(row["case_id"])
        if case_id in seen:
            raise EvidenceSourceReviewSetError(
                f"duplicate source-review candidate case_id: {case_id}"
            )
        seen.add(case_id)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def load_review_events(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    target = path or DEFAULT_REVIEW_ROOT / "review_events.jsonl"
    rows = _load_jsonl(target, label="source-review event")
    seen: set[str] = set()
    for row in rows:
        _validate_event_shape(row)
        event_id = str(row["event_id"])
        if event_id in seen:
            raise EvidenceSourceReviewSetError(
                f"duplicate source-review event_id: {event_id}"
            )
        seen.add(event_id)
    return sorted(
        rows,
        key=lambda row: (str(row["case_id"]), int(row["sequence"])),
    )


def build_review_template(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return unsigned decision events that cannot pass validation."""

    return [
        {
            "schema_version": EVIDENCE_SOURCE_REVIEW_EVENT_SCHEMA_VERSION,
            "dataset_version": EVIDENCE_SOURCE_REVIEW_DATASET_VERSION,
            "event_id": None,
            "case_id": str(candidate["case_id"]),
            "sequence": 1,
            "event_type": "decision",
            "previous_event_id": None,
            "identity_decision": None,
            "publisher_independence_decision": None,
            "claim_corroboration_decision": None,
            "claim_id": candidate.get("claim_id"),
            "requires_claim_level_review": None,
            "reviewer_id": None,
            "rationale": {
                "identity": None,
                "publisher_independence": None,
                "claim_corroboration": None,
            },
            "reviewed_at": None,
            "runtime_effect": False,
            "authority": False,
        }
        for candidate in sorted(
            (dict(row) for row in candidates),
            key=lambda row: str(row["case_id"]),
        )
    ]


def evaluate_source_review_set(
    candidates: Iterable[dict[str, Any]],
    review_events: Iterable[dict[str, Any]],
    *,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate split review axes without granting corroboration authority."""

    candidate_rows = [dict(row) for row in candidates]
    event_rows = [dict(row) for row in review_events]
    for row in candidate_rows:
        _validate_candidate(row)
    for row in event_rows:
        _validate_event_shape(row)
    candidates_by_id = _unique_candidates(candidate_rows)
    candidate_fingerprint = source_review_fingerprint(candidate_rows)
    event_fingerprint = source_review_fingerprint(event_rows)
    if (
        str(manifest.get("candidate_fingerprint") or "")
        != candidate_fingerprint
    ):
        raise EvidenceSourceReviewSetError(
            "source-review candidate fingerprint does not match manifest"
        )
    if (
        str(manifest.get("review_event_fingerprint") or "")
        != event_fingerprint
    ):
        raise EvidenceSourceReviewSetError(
            "source-review event fingerprint does not match manifest"
        )

    active_reviews, revoked_case_ids = resolve_active_reviews(
        event_rows,
        known_case_ids=set(candidates_by_id),
    )
    pending_case_ids = sorted(set(candidates_by_id) - set(active_reviews))
    identity_counts: Counter[str] = Counter()
    publisher_counts: Counter[str] = Counter()
    corroboration_counts: Counter[str] = Counter()
    claim_level_required: list[str] = []
    claim_scoped_review_count = 0
    evaluated: list[dict[str, Any]] = []

    for case_id in sorted(active_reviews):
        review = active_reviews[case_id]
        candidate_claim_id = (
            str(candidates_by_id[case_id].get("claim_id") or "").strip()
            or None
        )
        review_claim_id = (
            str(review.get("claim_id") or "").strip() or None
        )
        if review_claim_id != candidate_claim_id:
            raise EvidenceSourceReviewSetError(
                f"source-review claim_id mismatch for case: {case_id}"
            )
        identity = str(review["identity_decision"])
        publisher = str(review["publisher_independence_decision"])
        corroboration = str(review["claim_corroboration_decision"])
        claim_id = review_claim_id
        requires_claim_review = bool(
            review["requires_claim_level_review"]
        )
        publisher_kind = str(
            candidates_by_id[case_id]["source"]["publisher_kind"]
        )
        if (
            publisher_kind == "wire_service"
            and publisher == "confirmed_independent"
        ):
            raise EvidenceSourceReviewSetError(
                f"wire service cannot be confirmed independent: {case_id}"
            )
        if publisher == "excluded" and corroboration != "excluded":
            raise EvidenceSourceReviewSetError(
                f"excluded publisher requires excluded corroboration: {case_id}"
            )
        identity_counts[identity] += 1
        publisher_counts[publisher] += 1
        corroboration_counts[corroboration] += 1
        claim_scoped_review_count += int(claim_id is not None)
        if requires_claim_review:
            claim_level_required.append(case_id)
        evaluated.append(
            {
                "case_id": case_id,
                "source_url": str(
                    candidates_by_id[case_id]["source"]["url"]
                ),
                "identity_decision": identity,
                "publisher_independence_decision": publisher,
                "claim_corroboration_decision": corroboration,
                "claim_id": claim_id,
                "requires_claim_level_review": requires_claim_review,
                "reviewer_id": str(review["reviewer_id"]),
                "event_id": str(review["event_id"]),
            }
        )

    review_complete = len(active_reviews) == len(candidates_by_id)
    identity_gate_ready = review_complete and all(
        row["identity_decision"] == "accepted" for row in evaluated
    )
    publisher_gate_ready = review_complete and all(
        row["publisher_independence_decision"]
        in {"confirmed_independent", "excluded"}
        for row in evaluated
    )
    claim_corroboration_gate_ready = (
        review_complete
        and not claim_level_required
        and any(
            row["claim_corroboration_decision"]
            == "independently_corroborated"
            for row in evaluated
        )
    )

    blockers: list[str] = []
    if not review_complete:
        blockers.append("human_reviews_incomplete")
    if not identity_gate_ready:
        blockers.append("identity_gate_incomplete")
    if not publisher_gate_ready:
        blockers.append("publisher_independence_gate_incomplete")
    if claim_level_required:
        blockers.append("claim_level_reviews_incomplete")
    paraphrase_evaluation = manifest.get(
        "semantic_paraphrase_evaluation"
    )
    paraphrase_status = (
        str(paraphrase_evaluation.get("status") or "")
        if isinstance(paraphrase_evaluation, dict)
        else ""
    )
    if paraphrase_status != "measured":
        blockers.append("semantic_paraphrase_recall_unmeasured")
    if not claim_corroboration_gate_ready and not claim_level_required:
        blockers.append("claim_corroboration_gate_incomplete")

    return {
        "schema_version": EVIDENCE_SOURCE_REVIEW_SCHEMA_VERSION,
        "review_event_schema_version": (
            EVIDENCE_SOURCE_REVIEW_EVENT_SCHEMA_VERSION
        ),
        "dataset_version": EVIDENCE_SOURCE_REVIEW_DATASET_VERSION,
        "dataset_fingerprint": candidate_fingerprint,
        "review_event_fingerprint": event_fingerprint,
        "runtime_effect": False,
        "authority": False,
        "promotion_ready": not blockers,
        "identity_gate_ready": identity_gate_ready,
        "publisher_independence_gate_ready": publisher_gate_ready,
        "claim_corroboration_gate_ready": claim_corroboration_gate_ready,
        "promotion_blockers": sorted(set(blockers)),
        "summary": {
            "candidate_count": len(candidates_by_id),
            "review_event_count": len(event_rows),
            "active_review_count": len(active_reviews),
            "pending_count": len(pending_case_ids),
            "revoked_case_count": len(revoked_case_ids),
            "claim_scoped_review_count": claim_scoped_review_count,
            "claim_level_review_required_count": len(
                claim_level_required
            ),
            "identity_decision_counts": dict(
                sorted(identity_counts.items())
            ),
            "publisher_independence_decision_counts": dict(
                sorted(publisher_counts.items())
            ),
            "claim_corroboration_decision_counts": dict(
                sorted(corroboration_counts.items())
            ),
        },
        "pending_case_ids": pending_case_ids,
        "revoked_case_ids": sorted(revoked_case_ids),
        "claim_level_review_required_case_ids": sorted(
            claim_level_required
        ),
        "evaluated": evaluated,
    }


def resolve_active_reviews(
    review_events: Iterable[dict[str, Any]],
    *,
    known_case_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Resolve append-only decision/revocation chains per source case."""

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_event_ids: set[str] = set()
    for raw in review_events:
        event = dict(raw)
        _validate_event_shape(event)
        event_id = str(event["event_id"])
        if event_id in seen_event_ids:
            raise EvidenceSourceReviewSetError(
                f"duplicate source-review event_id: {event_id}"
            )
        seen_event_ids.add(event_id)
        case_id = str(event["case_id"])
        if case_id not in known_case_ids:
            raise EvidenceSourceReviewSetError(
                f"source-review event references unknown case: {case_id}"
            )
        grouped[case_id].append(event)

    active_reviews: dict[str, dict[str, Any]] = {}
    revoked_case_ids: set[str] = set()
    for case_id, events in sorted(grouped.items()):
        ordered = sorted(events, key=lambda row: int(row["sequence"]))
        active: dict[str, Any] | None = None
        previous: dict[str, Any] | None = None
        for expected_sequence, event in enumerate(ordered, start=1):
            if int(event["sequence"]) != expected_sequence:
                raise EvidenceSourceReviewSetError(
                    f"source-review event sequence gap for case: {case_id}"
                )
            expected_previous = (
                None if previous is None else str(previous["event_id"])
            )
            if event.get("previous_event_id") != expected_previous:
                raise EvidenceSourceReviewSetError(
                    f"source-review event chain mismatch for case: {case_id}"
                )
            if event["event_type"] == "decision":
                _validate_decision_event(event)
                active = event
                revoked_case_ids.discard(case_id)
            else:
                _validate_revocation_event(event)
                if active is None:
                    raise EvidenceSourceReviewSetError(
                        f"cannot revoke inactive source review: {case_id}"
                    )
                if event["revoked_event_id"] != active["event_id"]:
                    raise EvidenceSourceReviewSetError(
                        f"revocation target is not active for case: {case_id}"
                    )
                active = None
                revoked_case_ids.add(case_id)
            previous = event
        if active is not None:
            active_reviews[case_id] = active
    return active_reviews, revoked_case_ids


def source_review_fingerprint(rows: Iterable[dict[str, Any]]) -> str:
    canonical = json.dumps(
        sorted(
            (dict(row) for row in rows),
            key=lambda row: (
                str(row.get("case_id") or ""),
                int(row.get("sequence") or 0),
                str(row.get("event_id") or ""),
            ),
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_source_review_set_markdown(result: dict[str, Any]) -> str:
    summary = result.get("summary") or {}
    lines = [
        "# Evidence source review set",
        "",
        f"- Dataset: `{result.get('dataset_version', 'unknown')}`",
        f"- Runtime effect: `{str(bool(result.get('runtime_effect'))).lower()}`",
        f"- Authority: `{str(bool(result.get('authority'))).lower()}`",
        f"- Promotion ready: `{str(bool(result.get('promotion_ready'))).lower()}`",
        f"- Identity gate ready: `{str(bool(result.get('identity_gate_ready'))).lower()}`",
        f"- Publisher gate ready: `{str(bool(result.get('publisher_independence_gate_ready'))).lower()}`",
        f"- Claim corroboration gate ready: `{str(bool(result.get('claim_corroboration_gate_ready'))).lower()}`",
        f"- Candidates: `{summary.get('candidate_count', 0)}`",
        f"- Active reviews: `{summary.get('active_review_count', 0)}`",
        f"- Pending: `{summary.get('pending_count', 0)}`",
        f"- Claim-scoped reviews: `{summary.get('claim_scoped_review_count', 0)}`",
        f"- Sources requiring claim review: `{summary.get('claim_level_review_required_count', 0)}`",
        "",
        "## Promotion blockers",
        "",
    ]
    blockers = result.get("promotion_blockers") or []
    lines.extend(f"- `{blocker}`" for blocker in blockers)
    if not blockers:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _unique_candidates(
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        case_id = str(row["case_id"])
        if case_id in result:
            raise EvidenceSourceReviewSetError(
                f"duplicate source-review candidate case_id: {case_id}"
            )
        result[case_id] = row
    return result


def _validate_candidate(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "dataset_version",
        "case_id",
        "brand",
        "source",
        "claim_id",
        "review_prompt",
        "runtime_effect",
        "authority",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceSourceReviewSetError(
            f"source-review candidate missing fields: {', '.join(missing)}"
        )
    if row["schema_version"] != EVIDENCE_SOURCE_REVIEW_SCHEMA_VERSION:
        raise EvidenceSourceReviewSetError(
            "source-review candidate schema version mismatch"
        )
    if (
        row["dataset_version"]
        != EVIDENCE_SOURCE_REVIEW_DATASET_VERSION
    ):
        raise EvidenceSourceReviewSetError(
            "source-review candidate dataset version mismatch"
        )
    if not str(row["case_id"]).strip():
        raise EvidenceSourceReviewSetError(
            "source-review candidate case_id is required"
        )
    brand = row["brand"]
    if (
        not isinstance(brand, dict)
        or not str(brand.get("name") or "").strip()
        or not str(brand.get("domain") or "").strip()
    ):
        raise EvidenceSourceReviewSetError(
            "source-review candidate brand is incomplete"
        )
    source = row["source"]
    if not isinstance(source, dict):
        raise EvidenceSourceReviewSetError(
            "source-review candidate source must be an object"
        )
    for field in (
        "url",
        "publisher_name",
        "publisher_domain",
        "publisher_kind",
        "article_title",
    ):
        if not str(source.get(field) or "").strip():
            raise EvidenceSourceReviewSetError(
                f"source-review candidate source {field} is required"
            )
    parsed = urlsplit(str(source["url"]))
    if parsed.scheme != "https" or not parsed.netloc:
        raise EvidenceSourceReviewSetError(
            "source-review candidate URL must be absolute HTTPS"
        )
    if source["publisher_kind"] not in {"editorial", "wire_service"}:
        raise EvidenceSourceReviewSetError(
            "source-review candidate publisher kind is invalid"
        )
    if row["claim_id"] is not None and not str(row["claim_id"]).strip():
        raise EvidenceSourceReviewSetError(
            "source-review candidate claim_id cannot be blank"
        )
    review_prompt = row["review_prompt"]
    if not isinstance(review_prompt, dict) or any(
        not str(review_prompt.get(axis) or "").strip()
        for axis in (
            "identity",
            "publisher_independence",
            "claim_corroboration",
        )
    ):
        raise EvidenceSourceReviewSetError(
            "source-review candidate review prompt is incomplete"
        )
    if row["runtime_effect"] is not False or row["authority"] is not False:
        raise EvidenceSourceReviewSetError(
            "source-review candidates must remain non-authoritative"
        )


def _validate_event_shape(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "dataset_version",
        "event_id",
        "case_id",
        "sequence",
        "event_type",
        "previous_event_id",
        "reviewer_id",
        "rationale",
        "reviewed_at",
        "runtime_effect",
        "authority",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceSourceReviewSetError(
            f"source-review event missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_SOURCE_REVIEW_EVENT_SCHEMA_VERSION
    ):
        raise EvidenceSourceReviewSetError(
            "source-review event schema version mismatch"
        )
    if (
        row["dataset_version"]
        != EVIDENCE_SOURCE_REVIEW_DATASET_VERSION
    ):
        raise EvidenceSourceReviewSetError(
            "source-review event dataset version mismatch"
        )
    for field in ("event_id", "case_id", "reviewer_id", "reviewed_at"):
        if not str(row.get(field) or "").strip():
            raise EvidenceSourceReviewSetError(
                f"source-review event {field} is required"
            )
    if (
        not isinstance(row["sequence"], int)
        or int(row["sequence"]) < 1
    ):
        raise EvidenceSourceReviewSetError(
            "source-review event sequence must be a positive integer"
        )
    if row["event_type"] not in {"decision", "revocation"}:
        raise EvidenceSourceReviewSetError(
            "source-review event type is invalid"
        )
    if row["previous_event_id"] is not None and not str(
        row["previous_event_id"]
    ).strip():
        raise EvidenceSourceReviewSetError(
            "source-review previous_event_id cannot be blank"
        )
    try:
        datetime.fromisoformat(
            str(row["reviewed_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceSourceReviewSetError(
            "source-review event reviewed_at must be ISO-8601"
        ) from exc
    if row["runtime_effect"] is not False or row["authority"] is not False:
        raise EvidenceSourceReviewSetError(
            "source-review events must remain non-authoritative"
        )


def _validate_decision_event(row: dict[str, Any]) -> None:
    required = {
        "identity_decision",
        "publisher_independence_decision",
        "claim_corroboration_decision",
        "claim_id",
        "requires_claim_level_review",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceSourceReviewSetError(
            f"source-review decision missing fields: {', '.join(missing)}"
        )
    if row["identity_decision"] not in IDENTITY_DECISIONS:
        raise EvidenceSourceReviewSetError(
            "source-review identity decision is invalid"
        )
    if (
        row["publisher_independence_decision"]
        not in PUBLISHER_INDEPENDENCE_DECISIONS
    ):
        raise EvidenceSourceReviewSetError(
            "source-review publisher independence decision is invalid"
        )
    if (
        row["claim_corroboration_decision"]
        not in CLAIM_CORROBORATION_DECISIONS
    ):
        raise EvidenceSourceReviewSetError(
            "source-review claim corroboration decision is invalid"
        )
    if not isinstance(row["requires_claim_level_review"], bool):
        raise EvidenceSourceReviewSetError(
            "requires_claim_level_review must be boolean"
        )
    claim_id = str(row.get("claim_id") or "").strip() or None
    if (
        row["claim_corroboration_decision"]
        == "independently_corroborated"
        and claim_id is None
    ):
        raise EvidenceSourceReviewSetError(
            "independent corroboration requires an explicit claim_id"
        )
    if (
        row["claim_corroboration_decision"] in {"mixed", "disputed"}
        and claim_id is None
        and row["requires_claim_level_review"] is not True
    ):
        raise EvidenceSourceReviewSetError(
            "aggregate mixed or disputed review requires claim-level review"
        )
    rationale = row["rationale"]
    if not isinstance(rationale, dict) or any(
        not str(rationale.get(axis) or "").strip()
        for axis in (
            "identity",
            "publisher_independence",
            "claim_corroboration",
        )
    ):
        raise EvidenceSourceReviewSetError(
            "source-review decision rationale is incomplete"
        )


def _validate_revocation_event(row: dict[str, Any]) -> None:
    if not str(row.get("revoked_event_id") or "").strip():
        raise EvidenceSourceReviewSetError(
            "source-review revocation revoked_event_id is required"
        )
    if not str(row.get("rationale") or "").strip():
        raise EvidenceSourceReviewSetError(
            "source-review revocation rationale is required"
        )


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceSourceReviewSetError(
            f"cannot load {label} JSONL: {path}"
        ) from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceSourceReviewSetError(
                f"invalid {label} JSONL at line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise EvidenceSourceReviewSetError(
                f"{label} line {line_number} must be an object"
            )
        rows.append(row)
    return rows
