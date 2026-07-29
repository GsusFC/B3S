"""Versioned human review set for real evidence-to-claim-to-tile mappings."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


EVIDENCE_CLAIM_TILE_REVIEW_SCHEMA_VERSION = (
    "evidence-claim-tile-review-v1"
)
EVIDENCE_CLAIM_TILE_REVIEW_DATASET_VERSION = (
    "evidence-claim-tile-review-2026-07-v1"
)
REVIEW_DECISIONS = frozenset({"accepted", "disputed", "rejected"})
DEFAULT_REVIEW_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "evidence_claim_tile_review"
    / "v1"
)


class EvidenceClaimTileReviewSetError(ValueError):
    """The mapping-review dataset violates its immutable contract."""


def load_review_manifest(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_REVIEW_ROOT / "manifest.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceClaimTileReviewSetError(
            f"cannot load claim-tile review manifest: {target}"
        ) from exc
    if not isinstance(payload, dict):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile review manifest must be an object"
        )
    if (
        payload.get("schema_version")
        != EVIDENCE_CLAIM_TILE_REVIEW_SCHEMA_VERSION
    ):
        raise EvidenceClaimTileReviewSetError(
            "unsupported claim-tile review schema version"
        )
    if (
        payload.get("dataset_version")
        != EVIDENCE_CLAIM_TILE_REVIEW_DATASET_VERSION
    ):
        raise EvidenceClaimTileReviewSetError(
            "unsupported claim-tile review dataset version"
        )
    return payload


def load_review_candidates(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    target = path or DEFAULT_REVIEW_ROOT / "candidates.jsonl"
    rows = _load_jsonl(target, label="claim-tile candidate")
    seen: set[str] = set()
    for row in rows:
        _validate_candidate(row)
        case_id = str(row["case_id"])
        if case_id in seen:
            raise EvidenceClaimTileReviewSetError(
                f"duplicate claim-tile candidate case_id: {case_id}"
            )
        seen.add(case_id)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def load_reviews(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or DEFAULT_REVIEW_ROOT / "reviews.jsonl"
    if not target.is_file():
        return []
    rows = _load_jsonl(target, label="claim-tile review")
    seen: set[str] = set()
    for row in rows:
        _validate_review(row)
        case_id = str(row["case_id"])
        if case_id in seen:
            raise EvidenceClaimTileReviewSetError(
                f"duplicate claim-tile review case_id: {case_id}"
            )
        seen.add(case_id)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def build_review_template(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return unsigned rows; deterministic mapping proposals are not labels."""

    return [
        {
            "schema_version": (
                EVIDENCE_CLAIM_TILE_REVIEW_SCHEMA_VERSION
            ),
            "dataset_version": (
                EVIDENCE_CLAIM_TILE_REVIEW_DATASET_VERSION
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


def evaluate_claim_tile_review_set(
    candidates: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]],
    *,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Measure real mapping correctness without granting tile authority."""

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
    fingerprint = review_candidate_fingerprint(candidate_rows)
    if str(manifest.get("candidate_fingerprint") or "") != fingerprint:
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate fingerprint does not match manifest"
        )
    unknown_reviews = sorted(set(reviews_by_id) - set(candidates_by_id))
    if unknown_reviews:
        raise EvidenceClaimTileReviewSetError(
            f"reviews reference unknown cases: {', '.join(unknown_reviews)}"
        )

    decision_counts: Counter[str] = Counter()
    reviewed_brand_counts: Counter[str] = Counter()
    reviewed_variant_ids: set[str] = set()
    reviewed_polarity_counts: Counter[str] = Counter()
    case_type_counts: Counter[str] = Counter()
    pending_case_ids: list[str] = []
    critical_non_accepts: list[str] = []
    evaluated: list[dict[str, Any]] = []

    for case_id in sorted(candidates_by_id):
        candidate = candidates_by_id[case_id]
        review = reviews_by_id.get(case_id)
        if review is None:
            pending_case_ids.append(case_id)
            continue
        decision = str(review["decision"])
        decision_counts[decision] += 1
        domain = str(candidate["brand"]["domain"])
        variant_id = str(candidate["claim"]["claim_variant_id"])
        polarity = str(candidate["tile"]["polarity"])
        reviewed_brand_counts[domain] += 1
        reviewed_variant_ids.add(variant_id)
        reviewed_polarity_counts[polarity] += 1
        case_type_counts[str(candidate["case_type"])] += 1
        if bool(candidate["critical"]) and decision != "accepted":
            critical_non_accepts.append(case_id)
        evaluated.append(
            {
                "case_id": case_id,
                "mapping_id": str(candidate["mapping"]["mapping_id"]),
                "brand_domain": domain,
                "claim_variant_id": variant_id,
                "tile_key": str(candidate["tile"]["tile_key"]),
                "polarity": polarity,
                "review_decision": decision,
                "proposed_decision": str(
                    candidate["proposed_review"]
                ),
                "exact_agreement": decision == "accepted",
                "critical": bool(candidate["critical"]),
                "reviewer_id": str(review["reviewer_id"]),
            }
        )

    candidate_count = len(candidates_by_id)
    reviewed_count = len(evaluated)
    accepted_count = int(decision_counts["accepted"])
    precision = _ratio(accepted_count, reviewed_count)
    thresholds = (
        manifest.get("promotion_thresholds")
        if isinstance(manifest.get("promotion_thresholds"), dict)
        else {}
    )
    required_polarities = {
        str(value)
        for value in thresholds.get("required_reviewed_polarities") or []
        if str(value)
    }
    missing_polarities = sorted(
        required_polarities - set(reviewed_polarity_counts)
    )

    review_blockers: list[str] = []
    if reviewed_count != candidate_count:
        review_blockers.append("human_reviews_incomplete")
    if reviewed_count == 0:
        review_blockers.append("no_human_reviews")
    if critical_non_accepts:
        review_blockers.append("critical_non_accepts_present")
    if (
        precision is not None
        and precision
        < float(thresholds.get("confirmed_mapping_precision", 1.0))
    ):
        review_blockers.append("mapping_precision_below_threshold")
    review_gate_ready = not review_blockers

    promotion_blockers = list(review_blockers)
    if len(reviewed_brand_counts) < int(
        thresholds.get("minimum_reviewed_real_brands", 5)
    ):
        promotion_blockers.append("insufficient_reviewed_real_brands")
    if len(reviewed_variant_ids) < int(
        thresholds.get("minimum_reviewed_claim_variants", 10)
    ):
        promotion_blockers.append(
            "insufficient_reviewed_claim_variants"
        )
    if missing_polarities:
        promotion_blockers.append("required_polarity_coverage_missing")
    promotion_policy = manifest.get("promotion_policy")
    promotion_policy_status = (
        str(promotion_policy.get("status") or "")
        if isinstance(promotion_policy, dict)
        else ""
    )
    if promotion_policy_status != "adopted":
        promotion_blockers.append("promotion_policy_not_adopted")

    return {
        "schema_version": EVIDENCE_CLAIM_TILE_REVIEW_SCHEMA_VERSION,
        "dataset_version": EVIDENCE_CLAIM_TILE_REVIEW_DATASET_VERSION,
        "dataset_fingerprint": fingerprint,
        "runtime_effect": False,
        "authority": False,
        "review_gate_ready": review_gate_ready,
        "promotion_ready": not promotion_blockers,
        "review_blockers": sorted(set(review_blockers)),
        "promotion_blockers": sorted(set(promotion_blockers)),
        "summary": {
            "candidate_count": candidate_count,
            "reviewed_count": reviewed_count,
            "pending_count": len(pending_case_ids),
            "accepted_count": accepted_count,
            "disputed_count": int(decision_counts["disputed"]),
            "rejected_count": int(decision_counts["rejected"]),
            "critical_non_accept_count": len(critical_non_accepts),
            "confirmed_mapping_precision": precision,
            "candidate_brand_count": len(
                {
                    str(row["brand"]["domain"])
                    for row in candidate_rows
                }
            ),
            "candidate_claim_variant_count": len(
                {
                    str(row["claim"]["claim_variant_id"])
                    for row in candidate_rows
                }
            ),
            "candidate_polarity_counts": dict(
                sorted(
                    Counter(
                        str(row["tile"]["polarity"])
                        for row in candidate_rows
                    ).items()
                )
            ),
            "reviewed_real_brand_count": len(reviewed_brand_counts),
            "reviewed_claim_variant_count": len(reviewed_variant_ids),
            "reviewed_polarity_counts": dict(
                sorted(reviewed_polarity_counts.items())
            ),
            "reviewed_case_type_counts": dict(
                sorted(case_type_counts.items())
            ),
        },
        "pending_case_ids": pending_case_ids,
        "critical_non_accept_case_ids": critical_non_accepts,
        "missing_required_polarities": missing_polarities,
        "evaluated": evaluated,
    }


def review_candidate_fingerprint(
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


def render_claim_tile_review_set_markdown(
    result: dict[str, Any],
) -> str:
    summary = result.get("summary") or {}
    lines = [
        "# Evidence claim-to-tile review set",
        "",
        f"- Dataset: `{result.get('dataset_version', 'unknown')}`",
        f"- Runtime effect: `{str(bool(result.get('runtime_effect'))).lower()}`",
        f"- Authority: `{str(bool(result.get('authority'))).lower()}`",
        f"- Review gate ready: `{str(bool(result.get('review_gate_ready'))).lower()}`",
        f"- Promotion ready: `{str(bool(result.get('promotion_ready'))).lower()}`",
        f"- Candidates: `{summary.get('candidate_count', 0)}`",
        f"- Reviewed: `{summary.get('reviewed_count', 0)}`",
        f"- Pending: `{summary.get('pending_count', 0)}`",
        f"- Candidate brands: `{summary.get('candidate_brand_count', 0)}`",
        f"- Candidate claim variants: `{summary.get('candidate_claim_variant_count', 0)}`",
        f"- Candidate polarities: `{summary.get('candidate_polarity_counts', {})}`",
        f"- Confirmed mapping precision: `{summary.get('confirmed_mapping_precision')}`",
        "",
        "## Promotion blockers",
        "",
    ]
    blockers = result.get("promotion_blockers") or []
    lines.extend(f"- `{blocker}`" for blocker in blockers)
    if not blockers:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _validate_candidate(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "dataset_version",
        "case_id",
        "case_type",
        "critical",
        "provenance",
        "brand",
        "report",
        "source",
        "claim",
        "tile",
        "mapping",
        "proposed_review",
        "review_prompt",
        "runtime_effect",
        "authority",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceClaimTileReviewSetError(
            f"claim-tile candidate missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_CLAIM_TILE_REVIEW_SCHEMA_VERSION
    ):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate schema version mismatch"
        )
    if (
        row["dataset_version"]
        != EVIDENCE_CLAIM_TILE_REVIEW_DATASET_VERSION
    ):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate dataset version mismatch"
        )
    if row["provenance"] != "real_history":
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate must come from real history"
        )
    if not isinstance(row["critical"], bool):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate critical must be boolean"
        )
    if row["proposed_review"] != "accepted":
        raise EvidenceClaimTileReviewSetError(
            "frozen mapping candidate must be an accepted proposal"
        )
    brand = row["brand"]
    if (
        not isinstance(brand, dict)
        or not str(brand.get("name") or "").strip()
        or not str(brand.get("domain") or "").strip()
    ):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate brand is incomplete"
        )
    report = row["report"]
    if not isinstance(report, dict):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate report must be an object"
        )
    for field in ("report_id", "observed_at"):
        if not str(report.get(field) or "").strip():
            raise EvidenceClaimTileReviewSetError(
                f"claim-tile candidate report {field} is required"
            )
    try:
        datetime.fromisoformat(
            str(report["observed_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate observed_at must be ISO-8601"
        ) from exc
    source = row["source"]
    if not isinstance(source, dict):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate source must be an object"
        )
    for field in (
        "url",
        "source_evidence_id",
        "source_evidence_ref",
        "source_class",
        "identity_status",
        "source_independence_status",
    ):
        if not str(source.get(field) or "").strip():
            raise EvidenceClaimTileReviewSetError(
                f"claim-tile candidate source {field} is required"
            )
    parsed = urlsplit(str(source["url"]))
    if parsed.scheme != "https" or not parsed.netloc:
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate source URL must be absolute HTTPS"
        )
    claim = row["claim"]
    if not isinstance(claim, dict):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate claim must be an object"
        )
    for field in (
        "claim_slot_id",
        "claim_slot_key",
        "claim_variant_id",
        "claim_type",
        "content",
        "derivation_mode",
    ):
        if not str(claim.get(field) or "").strip():
            raise EvidenceClaimTileReviewSetError(
                f"claim-tile candidate claim {field} is required"
            )
    tile = row["tile"]
    if not isinstance(tile, dict):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate tile must be an object"
        )
    for field in (
        "component_key",
        "tile_id",
        "tile_key",
        "name",
        "condition",
        "state",
        "polarity",
        "evidence_quote",
        "evidence_contract",
    ):
        if not tile.get(field):
            raise EvidenceClaimTileReviewSetError(
                f"claim-tile candidate tile {field} is required"
            )
    if tile["polarity"] not in {
        "supports",
        "weakens",
        "insufficient_evidence",
    }:
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate polarity is invalid"
        )
    contract = tile["evidence_contract"]
    if not isinstance(contract, dict) or any(
        not str(contract.get(field) or "").strip()
        for field in ("ok", "no", "sin_evidencia", "reject")
    ):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate evidence contract is incomplete"
        )
    mapping = row["mapping"]
    if not isinstance(mapping, dict):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate mapping must be an object"
        )
    for field in (
        "mapping_id",
        "mapping_series_id",
        "match_method",
        "observation_count",
    ):
        if mapping.get(field) is None or mapping.get(field) == "":
            raise EvidenceClaimTileReviewSetError(
                f"claim-tile candidate mapping {field} is required"
            )
    if (
        not isinstance(mapping["observation_count"], int)
        or mapping["observation_count"] < 1
    ):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile mapping observation_count must be positive"
        )
    if not str(row["review_prompt"]).strip():
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidate review_prompt is required"
        )
    if row["runtime_effect"] is not False or row["authority"] is not False:
        raise EvidenceClaimTileReviewSetError(
            "claim-tile candidates must remain non-authoritative"
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
        raise EvidenceClaimTileReviewSetError(
            f"claim-tile review missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_CLAIM_TILE_REVIEW_SCHEMA_VERSION
    ):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile review schema version mismatch"
        )
    if (
        row["dataset_version"]
        != EVIDENCE_CLAIM_TILE_REVIEW_DATASET_VERSION
    ):
        raise EvidenceClaimTileReviewSetError(
            "claim-tile review dataset version mismatch"
        )
    if row["decision"] not in REVIEW_DECISIONS:
        raise EvidenceClaimTileReviewSetError(
            "claim-tile review decision is invalid"
        )
    for field in ("case_id", "reviewer_id", "rationale", "reviewed_at"):
        if not str(row.get(field) or "").strip():
            raise EvidenceClaimTileReviewSetError(
                f"claim-tile review {field} is required"
            )
    try:
        datetime.fromisoformat(
            str(row["reviewed_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceClaimTileReviewSetError(
            "claim-tile review reviewed_at must be ISO-8601"
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
            raise EvidenceClaimTileReviewSetError(
                f"duplicate claim-tile {label} case_id: {case_id}"
            )
        result[case_id] = row
    return result


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceClaimTileReviewSetError(
            f"cannot load {label} JSONL: {path}"
        ) from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceClaimTileReviewSetError(
                f"invalid {label} JSONL at line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise EvidenceClaimTileReviewSetError(
                f"{label} line {line_number} must be an object"
            )
        rows.append(row)
    return rows


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)
