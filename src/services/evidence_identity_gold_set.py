"""Versioned single-reviewer gold-set contract for evidence identity."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)


EVIDENCE_IDENTITY_GOLD_SCHEMA_VERSION = "evidence-identity-gold-v1"
EVIDENCE_IDENTITY_GOLD_DATASET_VERSION = "evidence-identity-gold-2026-07-v1"
REVIEW_DECISIONS = frozenset({"accepted", "disputed", "rejected"})
PREDICTION_BY_IDENTITY_STATUS = {
    "eligible": "accepted",
    "disputed": "disputed",
    "unverified": "disputed",
    "mismatch": "rejected",
}
DEFAULT_GOLD_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "evidence_memory_gold"
    / "v1"
)


class EvidenceIdentityGoldSetError(ValueError):
    """The candidate or review dataset violates its immutable contract."""


def load_gold_manifest(
    path: Path | None = None,
) -> dict[str, Any]:
    target = path or DEFAULT_GOLD_ROOT / "manifest.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceIdentityGoldSetError(
            f"cannot load gold-set manifest: {target}"
        ) from exc
    if not isinstance(payload, dict):
        raise EvidenceIdentityGoldSetError("gold-set manifest must be an object")
    if payload.get("schema_version") != EVIDENCE_IDENTITY_GOLD_SCHEMA_VERSION:
        raise EvidenceIdentityGoldSetError("unsupported gold-set schema version")
    if payload.get("dataset_version") != EVIDENCE_IDENTITY_GOLD_DATASET_VERSION:
        raise EvidenceIdentityGoldSetError("unsupported gold-set dataset version")
    return payload


def load_gold_candidates(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    target = path or DEFAULT_GOLD_ROOT / "candidates.jsonl"
    rows = _load_jsonl(target, label="candidate")
    seen: set[str] = set()
    for row in rows:
        _validate_candidate(row)
        case_id = str(row["case_id"])
        if case_id in seen:
            raise EvidenceIdentityGoldSetError(
                f"duplicate candidate case_id: {case_id}"
            )
        seen.add(case_id)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def load_gold_reviews(
    path: Path,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = _load_jsonl(path, label="review")
    seen: set[str] = set()
    for row in rows:
        _validate_review(row)
        case_id = str(row["case_id"])
        if case_id in seen:
            raise EvidenceIdentityGoldSetError(
                f"duplicate review case_id: {case_id}"
            )
        seen.add(case_id)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def build_review_template(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return intentionally unsigned rows; these are not gold decisions."""

    return [
        {
            "schema_version": EVIDENCE_IDENTITY_GOLD_SCHEMA_VERSION,
            "dataset_version": EVIDENCE_IDENTITY_GOLD_DATASET_VERSION,
            "case_id": str(candidate["case_id"]),
            "decision": None,
            "reviewer_id": None,
            "rationale": None,
            "reviewed_at": None,
        }
        for candidate in sorted(
            (dict(row) for row in candidates),
            key=lambda row: str(row["case_id"]),
        )
    ]


def evaluate_identity_gold_set(
    candidates: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]],
    *,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate only human-reviewed rows; pending candidates cannot pass."""

    candidate_rows = [dict(row) for row in candidates]
    review_rows = [dict(row) for row in reviews]
    for row in candidate_rows:
        _validate_candidate(row)
    for row in review_rows:
        _validate_review(row)
    candidates_by_id = _unique_by_case_id(candidate_rows, label="candidate")
    reviews_by_id = _unique_by_case_id(review_rows, label="review")
    candidate_fingerprint = gold_candidate_fingerprint(candidate_rows)
    if str(manifest.get("candidate_fingerprint") or "") != candidate_fingerprint:
        raise EvidenceIdentityGoldSetError(
            "candidate fingerprint does not match the versioned manifest"
        )
    unknown_reviews = sorted(set(reviews_by_id) - set(candidates_by_id))
    if unknown_reviews:
        raise EvidenceIdentityGoldSetError(
            f"reviews reference unknown cases: {', '.join(unknown_reviews)}"
        )

    evaluated: list[dict[str, Any]] = []
    pending_case_ids: list[str] = []
    confusion: Counter[str] = Counter()
    slice_counts: Counter[str] = Counter()
    critical_false_accepts: list[str] = []
    predicted_accept_count = 0
    reviewed_accept_count = 0
    correct_accept_count = 0
    exact_agreement_count = 0

    for case_id in sorted(candidates_by_id):
        candidate = candidates_by_id[case_id]
        prediction = predict_identity_disposition(candidate)
        review = reviews_by_id.get(case_id)
        if review is None:
            pending_case_ids.append(case_id)
            continue
        decision = str(review["decision"])
        predicted = str(prediction["decision"])
        confusion[f"{decision}->{predicted}"] += 1
        slice_counts[str(candidate["case_type"])] += 1
        predicted_accept_count += int(predicted == "accepted")
        reviewed_accept_count += int(decision == "accepted")
        correct_accept_count += int(
            predicted == "accepted" and decision == "accepted"
        )
        exact_agreement_count += int(predicted == decision)
        if (
            bool(candidate.get("critical"))
            and decision != "accepted"
            and predicted == "accepted"
        ):
            critical_false_accepts.append(case_id)
        evaluated.append(
            {
                "case_id": case_id,
                "case_type": str(candidate["case_type"]),
                "critical": bool(candidate.get("critical")),
                "review_decision": decision,
                "predicted_decision": predicted,
                "identity_status": prediction["identity_status"],
                "exact_agreement": predicted == decision,
                "safe_non_accept": (
                    decision != "accepted" and predicted != "accepted"
                ),
                "reviewer_id": str(review["reviewer_id"]),
            }
        )

    reviewed_count = len(evaluated)
    candidate_count = len(candidates_by_id)
    accepted_precision = _ratio(
        correct_accept_count,
        predicted_accept_count,
    )
    accepted_recall = _ratio(
        correct_accept_count,
        reviewed_accept_count,
    )
    exact_agreement_rate = _ratio(
        exact_agreement_count,
        reviewed_count,
    )
    thresholds = (
        manifest.get("promotion_thresholds")
        if isinstance(manifest.get("promotion_thresholds"), dict)
        else {}
    )
    blockers: list[str] = []
    if reviewed_count != candidate_count:
        blockers.append("human_reviews_incomplete")
    if critical_false_accepts:
        blockers.append("critical_false_accepts_present")
    if (
        accepted_precision is not None
        and accepted_precision < float(thresholds.get("accepted_precision", 1.0))
    ):
        blockers.append("accepted_precision_below_threshold")
    if (
        accepted_recall is not None
        and accepted_recall < float(thresholds.get("accepted_recall", 0.8))
    ):
        blockers.append("accepted_recall_below_threshold")
    if (
        exact_agreement_rate is not None
        and exact_agreement_rate
        < float(thresholds.get("exact_agreement", 0.8))
    ):
        blockers.append("exact_agreement_below_threshold")
    if reviewed_count == 0:
        blockers.append("no_human_reviews")

    return {
        "schema_version": EVIDENCE_IDENTITY_GOLD_SCHEMA_VERSION,
        "dataset_version": EVIDENCE_IDENTITY_GOLD_DATASET_VERSION,
        "dataset_fingerprint": candidate_fingerprint,
        "runtime_effect": False,
        "authority": False,
        "promotion_ready": not blockers,
        "promotion_blockers": sorted(set(blockers)),
        "summary": {
            "candidate_count": candidate_count,
            "reviewed_count": reviewed_count,
            "pending_count": len(pending_case_ids),
            "critical_false_accept_count": len(critical_false_accepts),
            "predicted_accept_count": predicted_accept_count,
            "reviewed_accept_count": reviewed_accept_count,
            "correct_accept_count": correct_accept_count,
            "accepted_precision": accepted_precision,
            "accepted_recall": accepted_recall,
            "exact_agreement_rate": exact_agreement_rate,
            "reviewed_slice_counts": dict(sorted(slice_counts.items())),
            "confusion": dict(sorted(confusion.items())),
        },
        "pending_case_ids": pending_case_ids,
        "critical_false_accept_case_ids": critical_false_accepts,
        "evaluated": evaluated,
    }


def predict_identity_disposition(
    candidate: dict[str, Any],
) -> dict[str, str]:
    """Run Identity v2 over one frozen candidate without adjudicating it."""

    _validate_candidate(candidate)
    reports = [
        _candidate_report(candidate, index)
        for index in range(int(candidate.get("repeat_count") or 1))
    ]
    projection = build_evidence_memory_identity_v2(reports)
    entries = projection.get("entries") or []
    if len(entries) != 1:
        raise EvidenceIdentityGoldSetError(
            f"candidate {candidate['case_id']} must project exactly one evidence entry"
        )
    identity_status = str(entries[0].get("identity_status") or "unverified")
    return {
        "identity_status": identity_status,
        "decision": PREDICTION_BY_IDENTITY_STATUS.get(
            identity_status,
            "disputed",
        ),
    }


def gold_candidate_fingerprint(
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


def render_identity_gold_set_markdown(result: dict[str, Any]) -> str:
    summary = result.get("summary") or {}
    lines = [
        "# Evidence identity gold set",
        "",
        f"- Dataset: `{result.get('dataset_version', 'unknown')}`",
        f"- Fingerprint: `{result.get('dataset_fingerprint', '')}`",
        f"- Runtime effect: `{str(bool(result.get('runtime_effect'))).lower()}`",
        f"- Authority: `{str(bool(result.get('authority'))).lower()}`",
        f"- Promotion ready: `{str(bool(result.get('promotion_ready'))).lower()}`",
        f"- Candidates: `{summary.get('candidate_count', 0)}`",
        f"- Reviewed: `{summary.get('reviewed_count', 0)}`",
        f"- Pending: `{summary.get('pending_count', 0)}`",
        f"- Critical false accepts: `{summary.get('critical_false_accept_count', 0)}`",
        f"- Accepted precision: `{summary.get('accepted_precision')}`",
        f"- Accepted recall: `{summary.get('accepted_recall')}`",
        f"- Exact agreement: `{summary.get('exact_agreement_rate')}`",
        "",
        "## Promotion blockers",
        "",
    ]
    blockers = result.get("promotion_blockers") or []
    lines.extend(
        f"- `{blocker}`" for blocker in blockers
    )
    if not blockers:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _candidate_report(
    candidate: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    brand = candidate["brand"]
    evidence = candidate["evidence"]
    return {
        "id": f"{candidate['case_id']}-{index + 1}",
        "brand_name": str(brand["name"]),
        "url": f"https://{brand['domain']}",
        "created_at": f"2026-01-{index + 1:02d}T00:00:00+00:00",
        "reliability_status": "usable",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [dict(evidence)],
                    }
                }
            }
        },
    }


def _validate_candidate(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "dataset_version",
        "case_id",
        "split",
        "case_type",
        "critical",
        "brand",
        "repeat_count",
        "evidence",
        "review_prompt",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceIdentityGoldSetError(
            f"candidate missing fields: {', '.join(missing)}"
        )
    if row["schema_version"] != EVIDENCE_IDENTITY_GOLD_SCHEMA_VERSION:
        raise EvidenceIdentityGoldSetError("candidate schema version mismatch")
    if row["dataset_version"] != EVIDENCE_IDENTITY_GOLD_DATASET_VERSION:
        raise EvidenceIdentityGoldSetError("candidate dataset version mismatch")
    if row["split"] not in {"calibration", "test"}:
        raise EvidenceIdentityGoldSetError("candidate split must be calibration or test")
    if not isinstance(row["critical"], bool):
        raise EvidenceIdentityGoldSetError("candidate critical must be boolean")
    if not isinstance(row["repeat_count"], int) or not 1 <= row["repeat_count"] <= 5:
        raise EvidenceIdentityGoldSetError("candidate repeat_count must be 1-5")
    brand = row["brand"]
    evidence = row["evidence"]
    if not isinstance(brand, dict) or not brand.get("name") or not brand.get("domain"):
        raise EvidenceIdentityGoldSetError("candidate brand is incomplete")
    if (
        not isinstance(evidence, dict)
        or not evidence.get("ref")
        or not evidence.get("content")
        or not isinstance(evidence.get("metadata"), dict)
    ):
        raise EvidenceIdentityGoldSetError("candidate evidence is incomplete")


def _validate_review(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "dataset_version",
        "case_id",
        "decision",
        "reviewer_id",
        "rationale",
        "reviewed_at",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceIdentityGoldSetError(
            f"review missing fields: {', '.join(missing)}"
        )
    if row["schema_version"] != EVIDENCE_IDENTITY_GOLD_SCHEMA_VERSION:
        raise EvidenceIdentityGoldSetError("review schema version mismatch")
    if row["dataset_version"] != EVIDENCE_IDENTITY_GOLD_DATASET_VERSION:
        raise EvidenceIdentityGoldSetError("review dataset version mismatch")
    if row["decision"] not in REVIEW_DECISIONS:
        raise EvidenceIdentityGoldSetError("review decision is invalid")
    for field in ("case_id", "reviewer_id", "rationale", "reviewed_at"):
        if not str(row.get(field) or "").strip():
            raise EvidenceIdentityGoldSetError(f"review {field} is required")
    try:
        datetime.fromisoformat(str(row["reviewed_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceIdentityGoldSetError(
            "review reviewed_at must be ISO-8601"
        ) from exc


def _unique_by_case_id(
    rows: Iterable[dict[str, Any]],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        if case_id in result:
            raise EvidenceIdentityGoldSetError(
                f"duplicate {label} case_id: {case_id}"
            )
        result[case_id] = row
    return result


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceIdentityGoldSetError(
            f"cannot load {label} JSONL: {path}"
        ) from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceIdentityGoldSetError(
                f"invalid {label} JSONL at line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise EvidenceIdentityGoldSetError(
                f"{label} line {line_number} must be an object"
            )
        rows.append(row)
    return rows


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)
