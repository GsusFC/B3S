"""Versioned single-reviewer gold set for Evidence Claim Memory relations."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from src.services.evidence_claim_memory import build_evidence_claim_memory


EVIDENCE_CLAIM_RELATION_GOLD_SCHEMA_VERSION = (
    "evidence-claim-relation-gold-v1"
)
EVIDENCE_CLAIM_RELATION_GOLD_DATASET_VERSION = (
    "evidence-claim-relation-gold-2026-07-v1"
)
REVIEW_DECISIONS = frozenset(
    {"replacement", "coexistence", "no_relation", "disputed"}
)
DEFAULT_GOLD_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "evidence_claim_relation_gold"
    / "v1"
)


class EvidenceClaimRelationGoldSetError(ValueError):
    """The candidate or review dataset violates its immutable contract."""


def load_gold_manifest(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_GOLD_ROOT / "manifest.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceClaimRelationGoldSetError(
            f"cannot load claim relation gold-set manifest: {target}"
        ) from exc
    if not isinstance(payload, dict):
        raise EvidenceClaimRelationGoldSetError(
            "claim relation gold-set manifest must be an object"
        )
    if (
        payload.get("schema_version")
        != EVIDENCE_CLAIM_RELATION_GOLD_SCHEMA_VERSION
    ):
        raise EvidenceClaimRelationGoldSetError(
            "unsupported claim relation gold-set schema version"
        )
    if (
        payload.get("dataset_version")
        != EVIDENCE_CLAIM_RELATION_GOLD_DATASET_VERSION
    ):
        raise EvidenceClaimRelationGoldSetError(
            "unsupported claim relation gold-set dataset version"
        )
    return payload


def load_gold_candidates(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or DEFAULT_GOLD_ROOT / "candidates.jsonl"
    rows = _load_jsonl(target, label="candidate")
    seen: set[str] = set()
    for row in rows:
        _validate_candidate(row)
        case_id = str(row["case_id"])
        if case_id in seen:
            raise EvidenceClaimRelationGoldSetError(
                f"duplicate candidate case_id: {case_id}"
            )
        seen.add(case_id)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def load_gold_reviews(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = _load_jsonl(path, label="review")
    seen: set[str] = set()
    for row in rows:
        _validate_review(row)
        case_id = str(row["case_id"])
        if case_id in seen:
            raise EvidenceClaimRelationGoldSetError(
                f"duplicate review case_id: {case_id}"
            )
        seen.add(case_id)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def build_review_template(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return unsigned review rows; candidate hints are never decisions."""

    return [
        {
            "schema_version": (
                EVIDENCE_CLAIM_RELATION_GOLD_SCHEMA_VERSION
            ),
            "dataset_version": (
                EVIDENCE_CLAIM_RELATION_GOLD_DATASET_VERSION
            ),
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


def evaluate_claim_relation_gold_set(
    candidates: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]],
    *,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Measure relation proposals against human review without authority."""

    candidate_rows = [dict(row) for row in candidates]
    review_rows = [dict(row) for row in reviews]
    for row in candidate_rows:
        _validate_candidate(row)
    for row in review_rows:
        _validate_review(row)
    candidates_by_id = _unique_by_case_id(
        candidate_rows,
        label="candidate",
    )
    reviews_by_id = _unique_by_case_id(review_rows, label="review")
    candidate_fingerprint = gold_candidate_fingerprint(candidate_rows)
    if (
        str(manifest.get("candidate_fingerprint") or "")
        != candidate_fingerprint
    ):
        raise EvidenceClaimRelationGoldSetError(
            "candidate fingerprint does not match the versioned manifest"
        )
    unknown_reviews = sorted(set(reviews_by_id) - set(candidates_by_id))
    if unknown_reviews:
        raise EvidenceClaimRelationGoldSetError(
            f"reviews reference unknown cases: {', '.join(unknown_reviews)}"
        )

    evaluated: list[dict[str, Any]] = []
    pending_case_ids: list[str] = []
    critical_false_replacements: list[str] = []
    false_replacements: list[str] = []
    missed_replacements: list[str] = []
    confusion: Counter[str] = Counter()
    reviewed_case_type_counts: Counter[str] = Counter()
    reviewed_provenance_counts: Counter[str] = Counter()
    candidate_prediction_counts: Counter[str] = Counter()
    reviewed_predicted_replacement_count = 0
    reviewed_replacement_count = 0
    correct_replacement_count = 0
    exact_agreement_count = 0
    reviewed_real_history_count = 0
    reviewed_real_replacement_count = 0

    for case_id in sorted(candidates_by_id):
        candidate = candidates_by_id[case_id]
        prediction = predict_claim_relation_disposition(candidate)
        candidate_prediction_counts[str(prediction["decision"])] += 1
        review = reviews_by_id.get(case_id)
        if review is None:
            pending_case_ids.append(case_id)
            continue
        decision = str(review["decision"])
        predicted = str(prediction["decision"])
        provenance = str(candidate["provenance"])
        confusion[f"{decision}->{predicted}"] += 1
        reviewed_case_type_counts[str(candidate["case_type"])] += 1
        reviewed_provenance_counts[provenance] += 1
        reviewed_predicted_replacement_count += int(
            predicted == "replacement"
        )
        reviewed_replacement_count += int(decision == "replacement")
        correct_replacement_count += int(
            predicted == "replacement" and decision == "replacement"
        )
        exact_agreement_count += int(predicted == decision)
        if predicted == "replacement" and decision != "replacement":
            false_replacements.append(case_id)
            if bool(candidate.get("critical")):
                critical_false_replacements.append(case_id)
        if decision == "replacement" and predicted != "replacement":
            missed_replacements.append(case_id)
        if provenance == "real_history":
            reviewed_real_history_count += 1
            reviewed_real_replacement_count += int(
                decision == "replacement"
            )
        evaluated.append(
            {
                "case_id": case_id,
                "case_type": str(candidate["case_type"]),
                "provenance": provenance,
                "critical": bool(candidate.get("critical")),
                "review_decision": decision,
                "predicted_decision": predicted,
                "relation_candidate_counts": prediction[
                    "relation_candidate_counts"
                ],
                "exact_agreement": predicted == decision,
                "reviewer_id": str(review["reviewer_id"]),
            }
        )

    candidate_count = len(candidates_by_id)
    reviewed_count = len(evaluated)
    replacement_precision = _ratio(
        correct_replacement_count,
        reviewed_predicted_replacement_count,
    )
    replacement_recall = _ratio(
        correct_replacement_count,
        reviewed_replacement_count,
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
    if reviewed_count == 0:
        blockers.append("no_human_reviews")
    if critical_false_replacements:
        blockers.append("critical_false_replacements_present")
    if (
        replacement_precision is not None
        and replacement_precision
        < float(thresholds.get("replacement_precision", 1.0))
    ):
        blockers.append("replacement_precision_below_threshold")
    if (
        replacement_recall is not None
        and replacement_recall
        < float(thresholds.get("replacement_recall", 0.8))
    ):
        blockers.append("replacement_recall_below_threshold")
    if (
        exact_agreement_rate is not None
        and exact_agreement_rate
        < float(thresholds.get("exact_agreement", 0.8))
    ):
        blockers.append("exact_agreement_below_threshold")
    if reviewed_real_history_count < int(
        thresholds.get("minimum_reviewed_real_history_cases", 5)
    ):
        blockers.append("insufficient_reviewed_real_history_cases")
    if reviewed_real_replacement_count < int(
        thresholds.get("minimum_reviewed_real_replacements", 3)
    ):
        blockers.append("insufficient_reviewed_real_replacements")

    return {
        "schema_version": EVIDENCE_CLAIM_RELATION_GOLD_SCHEMA_VERSION,
        "dataset_version": EVIDENCE_CLAIM_RELATION_GOLD_DATASET_VERSION,
        "dataset_fingerprint": candidate_fingerprint,
        "runtime_effect": False,
        "authority": False,
        "promotion_ready": not blockers,
        "promotion_blockers": sorted(set(blockers)),
        "summary": {
            "candidate_count": candidate_count,
            "reviewed_count": reviewed_count,
            "pending_count": len(pending_case_ids),
            "false_replacement_count": len(false_replacements),
            "critical_false_replacement_count": len(
                critical_false_replacements
            ),
            "missed_replacement_count": len(missed_replacements),
            "candidate_prediction_counts": dict(
                sorted(candidate_prediction_counts.items())
            ),
            "candidate_predicted_replacement_count": (
                candidate_prediction_counts.get("replacement", 0)
            ),
            "reviewed_predicted_replacement_count": (
                reviewed_predicted_replacement_count
            ),
            "reviewed_replacement_count": reviewed_replacement_count,
            "correct_replacement_count": correct_replacement_count,
            "replacement_precision": replacement_precision,
            "replacement_recall": replacement_recall,
            "exact_agreement_rate": exact_agreement_rate,
            "reviewed_real_history_count": reviewed_real_history_count,
            "reviewed_real_replacement_count": (
                reviewed_real_replacement_count
            ),
            "reviewed_case_type_counts": dict(
                sorted(reviewed_case_type_counts.items())
            ),
            "reviewed_provenance_counts": dict(
                sorted(reviewed_provenance_counts.items())
            ),
            "confusion": dict(sorted(confusion.items())),
        },
        "pending_case_ids": pending_case_ids,
        "false_replacement_case_ids": false_replacements,
        "critical_false_replacement_case_ids": (
            critical_false_replacements
        ),
        "missed_replacement_case_ids": missed_replacements,
        "evaluated": evaluated,
    }


def predict_claim_relation_disposition(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Project one frozen timeline through Claim Memory v1."""

    _validate_candidate(candidate)
    projection = build_evidence_claim_memory(
        _candidate_reports(candidate)
    )
    counts = Counter()
    for slot in projection.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        for relation in slot.get("relation_candidates") or []:
            if isinstance(relation, dict):
                counts[str(relation.get("relation") or "unknown")] += 1
    relation_types = set(counts)
    if not relation_types:
        decision = "no_relation"
    elif relation_types == {"replacement_candidate"}:
        decision = "replacement"
    elif relation_types == {"coexistence_candidate"}:
        decision = "coexistence"
    else:
        decision = "disputed"
    return {
        "decision": decision,
        "relation_candidate_counts": dict(sorted(counts.items())),
        "claim_slot_count": int(
            projection.get("summary", {}).get("claim_slot_count") or 0
        ),
        "claim_variant_count": int(
            projection.get("summary", {}).get("claim_variant_count") or 0
        ),
        "runtime_effect": False,
        "authority": False,
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


def render_claim_relation_gold_set_markdown(
    result: dict[str, Any],
) -> str:
    summary = result.get("summary") or {}
    lines = [
        "# Evidence claim relation gold set",
        "",
        f"- Dataset: `{result.get('dataset_version', 'unknown')}`",
        f"- Fingerprint: `{result.get('dataset_fingerprint', '')}`",
        f"- Runtime effect: `{str(bool(result.get('runtime_effect'))).lower()}`",
        f"- Authority: `{str(bool(result.get('authority'))).lower()}`",
        f"- Promotion ready: `{str(bool(result.get('promotion_ready'))).lower()}`",
        f"- Candidates: `{summary.get('candidate_count', 0)}`",
        f"- Reviewed: `{summary.get('reviewed_count', 0)}`",
        f"- Pending: `{summary.get('pending_count', 0)}`",
        f"- Candidate predictions: `{summary.get('candidate_prediction_counts', {})}`",
        f"- False replacements: `{summary.get('false_replacement_count', 0)}`",
        f"- Missed replacements: `{summary.get('missed_replacement_count', 0)}`",
        f"- Replacement precision: `{summary.get('replacement_precision')}`",
        f"- Replacement recall: `{summary.get('replacement_recall')}`",
        f"- Reviewed real-history cases: `{summary.get('reviewed_real_history_count', 0)}`",
        f"- Reviewed real replacements: `{summary.get('reviewed_real_replacement_count', 0)}`",
        "",
        "## Promotion blockers",
        "",
    ]
    blockers = result.get("promotion_blockers") or []
    lines.extend(f"- `{blocker}`" for blocker in blockers)
    if not blockers:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _candidate_reports(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    brand = candidate["brand"]
    reports: list[dict[str, Any]] = []
    for report in candidate["reports"]:
        evidence = [
            _claim_evidence_row(candidate, claim, index)
            for index, claim in enumerate(report["claims"], start=1)
        ]
        reports.append(
            {
                "id": str(report["report_id"]),
                "brand_name": str(brand["name"]),
                "url": f"https://{brand['domain']}",
                "created_at": str(report["created_at"]),
                "reliability_status": "usable",
                "acquisition_gate": {"state": "pass"},
                "components": [],
                "raw": {
                    "flow": {
                        "candidate": {
                            "schema_version": (
                                "claim-relation-gold-candidate-v1"
                            ),
                            "evidence_pack": {"evidence": evidence},
                        }
                    }
                },
            }
        )
    return reports


def _claim_evidence_row(
    candidate: dict[str, Any],
    claim: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    return {
        "ref": str(claim.get("ref") or f"gold.claim.{index}"),
        "source": str(claim.get("source") or "review_fixture"),
        "evidence_type": (
            f"semantic_claim.{str(claim['claim_type']).strip().lower()}"
        ),
        "content": str(claim["content"]),
        "url": str(
            claim.get("url")
            or f"https://{candidate['brand']['domain']}/about"
        ),
        "confidence": str(claim.get("confidence") or "high"),
        "metadata": {
            "source_class": str(
                claim.get("source_class") or "owned_copy"
            ),
            "claim_slot_key": str(claim["claim_slot_key"]),
            "claim_type": str(claim["claim_type"]),
            "claim_slot_derivation_mode": "gold_set_fixture",
            "claim_slot_producer_version": "review_fixture_v1",
            "runtime_effect": False,
            "authority": False,
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
        "provenance",
        "brand",
        "reports",
        "review_prompt",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceClaimRelationGoldSetError(
            f"candidate missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_CLAIM_RELATION_GOLD_SCHEMA_VERSION
    ):
        raise EvidenceClaimRelationGoldSetError(
            "candidate schema version mismatch"
        )
    if (
        row["dataset_version"]
        != EVIDENCE_CLAIM_RELATION_GOLD_DATASET_VERSION
    ):
        raise EvidenceClaimRelationGoldSetError(
            "candidate dataset version mismatch"
        )
    if row["split"] not in {"calibration", "test"}:
        raise EvidenceClaimRelationGoldSetError(
            "candidate split must be calibration or test"
        )
    if row["provenance"] not in {"controlled", "real_history"}:
        raise EvidenceClaimRelationGoldSetError(
            "candidate provenance must be controlled or real_history"
        )
    if not isinstance(row["critical"], bool):
        raise EvidenceClaimRelationGoldSetError(
            "candidate critical must be boolean"
        )
    brand = row["brand"]
    if (
        not isinstance(brand, dict)
        or not brand.get("name")
        or not brand.get("domain")
    ):
        raise EvidenceClaimRelationGoldSetError(
            "candidate brand is incomplete"
        )
    reports = row["reports"]
    if not isinstance(reports, list) or not reports:
        raise EvidenceClaimRelationGoldSetError(
            "candidate reports must be a non-empty list"
        )
    seen_report_ids: set[str] = set()
    for report in reports:
        _validate_report(report)
        report_id = str(report["report_id"])
        if report_id in seen_report_ids:
            raise EvidenceClaimRelationGoldSetError(
                f"duplicate report_id in candidate: {report_id}"
            )
        seen_report_ids.add(report_id)
    if (
        row["provenance"] == "real_history"
        and not isinstance(row.get("source_report_ids"), list)
    ):
        raise EvidenceClaimRelationGoldSetError(
            "real-history candidate requires source_report_ids"
        )
    if row["provenance"] == "real_history":
        source_report_ids = [
            str(value) for value in row["source_report_ids"]
        ]
        if (
            len(source_report_ids) != len(set(source_report_ids))
            or set(source_report_ids) != seen_report_ids
        ):
            raise EvidenceClaimRelationGoldSetError(
                "real-history source_report_ids must match candidate reports"
            )


def _validate_report(report: Any) -> None:
    if not isinstance(report, dict):
        raise EvidenceClaimRelationGoldSetError(
            "candidate report must be an object"
        )
    for field in ("report_id", "created_at", "claims"):
        if field not in report:
            raise EvidenceClaimRelationGoldSetError(
                f"candidate report {field} is required"
            )
    if not str(report["report_id"]).strip():
        raise EvidenceClaimRelationGoldSetError(
            "candidate report_id is required"
        )
    try:
        datetime.fromisoformat(
            str(report["created_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceClaimRelationGoldSetError(
            "candidate report created_at must be ISO-8601"
        ) from exc
    if not isinstance(report["claims"], list):
        raise EvidenceClaimRelationGoldSetError(
            "candidate report claims must be a list"
        )
    for claim in report["claims"]:
        if (
            not isinstance(claim, dict)
            or not str(claim.get("content") or "").strip()
            or not str(claim.get("claim_slot_key") or "").strip()
            or not str(claim.get("claim_type") or "").strip()
        ):
            raise EvidenceClaimRelationGoldSetError(
                "candidate claim is incomplete"
            )


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
        raise EvidenceClaimRelationGoldSetError(
            f"review missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_CLAIM_RELATION_GOLD_SCHEMA_VERSION
    ):
        raise EvidenceClaimRelationGoldSetError(
            "review schema version mismatch"
        )
    if (
        row["dataset_version"]
        != EVIDENCE_CLAIM_RELATION_GOLD_DATASET_VERSION
    ):
        raise EvidenceClaimRelationGoldSetError(
            "review dataset version mismatch"
        )
    if row["decision"] not in REVIEW_DECISIONS:
        raise EvidenceClaimRelationGoldSetError(
            "review decision is invalid"
        )
    for field in ("case_id", "reviewer_id", "rationale", "reviewed_at"):
        if not str(row.get(field) or "").strip():
            raise EvidenceClaimRelationGoldSetError(
                f"review {field} is required"
            )
    try:
        datetime.fromisoformat(
            str(row["reviewed_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceClaimRelationGoldSetError(
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
            raise EvidenceClaimRelationGoldSetError(
                f"duplicate {label} case_id: {case_id}"
            )
        result[case_id] = row
    return result


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceClaimRelationGoldSetError(
            f"cannot load {label} JSONL: {path}"
        ) from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceClaimRelationGoldSetError(
                f"invalid {label} JSONL at line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise EvidenceClaimRelationGoldSetError(
                f"{label} line {line_number} must be an object"
            )
        rows.append(row)
    return rows


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)
