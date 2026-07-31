"""Reviewed claim-to-tile memory projection over the durable shadow ledger."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from typing import Any, Iterable

from src.evidence_identity import stable_artifact_digest


EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_VERSION = (
    "evidence-reviewed-claim-tile-memory-v2"
)
EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_POLICY_VERSION = (
    "evidence-reviewed-claim-tile-memory-policy-v2"
)
EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_SEMANTIC_VERSION = (
    "evidence-reviewed-claim-tile-semantic-version-v1"
)
EVIDENCE_REVIEWED_CLAIM_TILE_EVALUATION_VERSION = (
    "evidence-reviewed-claim-tile-evaluation-v1"
)
EVIDENCE_REVIEWED_CLAIM_TILE_EVALUATOR_VERSION = (
    "evidence-reviewed-claim-tile-evaluator-v1"
)

_REVIEW_DECISIONS = frozenset(
    {"accepted", "disputed", "rejected", "revoked"}
)
_IDENTITY_FIELDS = (
    "mapping_id",
    "mapping_series_id",
    "source_evidence_id",
    "claim_variant_id",
    "component_key",
    "tile_id",
    "tile_key",
    "polarity",
)


class EvidenceReviewedClaimTileMemoryError(ValueError):
    """Reviewed mapping memory cannot satisfy its fail-closed contract."""


def build_reviewed_claim_tile_memory_shadow(
    ledger: dict[str, Any],
    reviews: Iterable[dict[str, Any]],
    *,
    review_packet_fingerprint: str,
    rubric_version: str,
    evaluator_version: str,
    _allowed_review_packet_fingerprints: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Select accepted mappings without granting canonical or score authority."""

    packet_fingerprint = _required_sha256(
        review_packet_fingerprint,
        field="review_packet_fingerprint",
    )
    packet_fingerprints = sorted(
        {
            _required_sha256(
                value,
                field="review_packet_fingerprint",
            )
            for value in (
                _allowed_review_packet_fingerprints
                if _allowed_review_packet_fingerprints is not None
                else (packet_fingerprint,)
            )
        }
    )
    if not packet_fingerprints:
        raise EvidenceReviewedClaimTileMemoryError(
            "at least one review packet fingerprint is required"
        )
    normalized_rubric = _required_text(
        rubric_version,
        field="rubric_version",
    )
    normalized_evaluator = _required_text(
        evaluator_version,
        field="evaluator_version",
    )
    _validate_ledger(ledger)
    mappings = _unique_mappings(ledger.get("mappings") or [])
    reviews_by_mapping = _validated_reviews(
        reviews,
        mappings=mappings,
        packet_fingerprints=set(packet_fingerprints),
    )

    reviewed_mappings: list[dict[str, Any]] = []
    accepted_mappings: list[dict[str, Any]] = []
    decisions: Counter[str] = Counter()
    for mapping_id in sorted(mappings):
        mapping = mappings[mapping_id]
        review = reviews_by_mapping.get(mapping_id)
        if review is None:
            continue
        decision = str(review["decision"])
        decisions[decision] += 1
        reviewed = {
            **_semantic_mapping(mapping),
            "decision": decision,
            "review_event_id": str(review["event_id"]),
            "review_packet_fingerprint": (
                _review_packet_fingerprint(review)
            ),
            "reviewer_id": str(review["reviewer_id"]),
            "reviewed_at": str(review["reviewed_at"]),
        }
        reviewed_mappings.append(reviewed)
        if decision == "accepted":
            accepted_mappings.append(reviewed)

    brand = {
        "name": str((ledger.get("brand") or {}).get("name") or ""),
        "domain": str((ledger.get("brand") or {}).get("domain") or ""),
    }
    semantic_accepted = [
        _semantic_mapping(mapping) for mapping in accepted_mappings
    ]
    memory_version = stable_artifact_digest(
        EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_SEMANTIC_VERSION,
        {
            "brand_domain": brand["domain"].strip().casefold(),
            "accepted_claim_tile_mappings": semantic_accepted,
        },
    )
    shadow_evaluation_identity = stable_artifact_digest(
        EVIDENCE_REVIEWED_CLAIM_TILE_EVALUATION_VERSION,
        {
            "memory_version": memory_version,
            "rubric_version": normalized_rubric,
            "evaluator_version": normalized_evaluator,
        },
    )
    packet_set_fingerprint = stable_artifact_digest(
        "evidence-reviewed-claim-tile-packet-set-v1",
        packet_fingerprints,
    )
    mapping_count = len(mappings)
    reviewed_count = sum(
        1
        for mapping in reviewed_mappings
        if mapping["decision"] != "revoked"
    )
    selection_ready = mapping_count > 0 and reviewed_count == mapping_count
    blockers = [
        "canonical_reviewed_mapping_policy_not_adopted",
        "score_evaluator_and_result_cache_not_implemented",
    ]
    if not selection_ready:
        blockers.append("claim_tile_reviews_incomplete")

    result = {
        "schema_version": EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_VERSION,
        "policy_version": (
            EVIDENCE_REVIEWED_CLAIM_TILE_MEMORY_POLICY_VERSION
        ),
        "memory_kind": "reviewed_claim_tile_shadow_candidate",
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
        "review_packet_fingerprint": (
            packet_fingerprints[0]
            if len(packet_fingerprints) == 1
            else None
        ),
        "review_packet_fingerprints": packet_fingerprints,
        "review_packet_set_fingerprint": packet_set_fingerprint,
        "rubric_version": normalized_rubric,
        "evaluator_version": normalized_evaluator,
        "brand": brand,
        "selection_ready": selection_ready,
        "shadow_evaluation_ready": selection_ready,
        "canonical_memory_available": False,
        "evaluation_ready": False,
        "reviewed_memory_candidate_version": memory_version,
        "canonical_memory_version": None,
        "shadow_evaluation_identity": shadow_evaluation_identity,
        "evaluation_identity": None,
        "score": None,
        "score_delta": None,
        "summary": {
            "candidate_mapping_count": mapping_count,
            "reviewed_mapping_count": reviewed_count,
            "pending_mapping_count": mapping_count - reviewed_count,
            "accepted_mapping_count": int(decisions["accepted"]),
            "disputed_mapping_count": int(decisions["disputed"]),
            "rejected_mapping_count": int(decisions["rejected"]),
            "revoked_mapping_count": int(decisions["revoked"]),
        },
        "reviewed_mappings": reviewed_mappings,
        "accepted_mappings": accepted_mappings,
        "promotion_ready": False,
        "promotion_blockers": sorted(blockers),
        "warnings": [
            "only_explicitly_accepted_mappings_enter_reviewed_memory",
            "reviewed_memory_remains_shadow_only",
            "no_runtime_tile_or_scoring_effect",
        ],
    }
    result["state_fingerprint"] = _state_fingerprint(result)
    return result


def build_reviewed_claim_tile_memory_from_journal_shadow(
    ledger: dict[str, Any],
    reviews: Iterable[dict[str, Any]],
    *,
    evaluator_version: str = (
        EVIDENCE_REVIEWED_CLAIM_TILE_EVALUATOR_VERSION
    ),
) -> dict[str, Any]:
    """Rebuild reviewed memory from a packet-bound durable review journal."""

    _validate_ledger(ledger)
    latest_series_id = str(
        ledger.get("latest_mapping_series_id") or ""
    )
    scoped_ledger = dict(ledger)
    if latest_series_id:
        scoped_ledger["mapping_series"] = [
            dict(series)
            for series in ledger.get("mapping_series") or []
            if isinstance(series, dict)
            and str(series.get("mapping_series_id") or "")
            == latest_series_id
        ]
        scoped_ledger["mappings"] = [
            dict(mapping)
            for mapping in ledger.get("mappings") or []
            if isinstance(mapping, dict)
            and str(mapping.get("mapping_series_id") or "")
            == latest_series_id
        ]
    scoped_mapping_ids = {
        str(mapping.get("mapping_id") or "")
        for mapping in scoped_ledger.get("mappings") or []
        if isinstance(mapping, dict)
    }
    review_rows = [
        dict(review)
        for review in reviews
        if isinstance(review, dict)
        and (
            not latest_series_id
            or str(
                review.get("mapping_id")
                or review.get("subject_id")
                or ""
            )
            in scoped_mapping_ids
        )
    ]
    if not review_rows:
        raise EvidenceReviewedClaimTileMemoryError(
            "packet-bound current claim-tile reviews are required"
        )

    packet_fingerprints = {
        _review_packet_fingerprint(review)
        for review in review_rows
    }
    normalized_packet_fingerprints = sorted(packet_fingerprints)

    series_by_id = {
        str(series.get("mapping_series_id") or ""): series
        for series in scoped_ledger.get("mapping_series") or []
        if isinstance(series, dict)
        and str(series.get("mapping_series_id") or "")
    }
    rubric_versions: set[str] = set()
    for review in review_rows:
        series_id = str(review.get("mapping_series_id") or "")
        series = series_by_id.get(series_id)
        rubric_version = str(
            (series or {}).get("rubric_version") or ""
        ).strip()
        if not rubric_version:
            raise EvidenceReviewedClaimTileMemoryError(
                "reviewed claim-tile mapping rubric version is unavailable"
            )
        rubric_versions.add(rubric_version)
    if len(rubric_versions) != 1:
        raise EvidenceReviewedClaimTileMemoryError(
            "current claim-tile reviews reference mixed rubric versions"
        )

    return build_reviewed_claim_tile_memory_shadow(
        scoped_ledger,
        review_rows,
        review_packet_fingerprint=normalized_packet_fingerprints[0],
        rubric_version=next(iter(rubric_versions)),
        evaluator_version=evaluator_version,
        _allowed_review_packet_fingerprints=(
            normalized_packet_fingerprints
        ),
    )


def _validate_ledger(ledger: dict[str, Any]) -> None:
    if not isinstance(ledger, dict):
        raise EvidenceReviewedClaimTileMemoryError(
            "claim-tile ledger must be an object"
        )
    if (
        ledger.get("mode") != "shadow"
        or ledger.get("runtime_effect") is not False
        or ledger.get("authority") is not False
    ):
        raise EvidenceReviewedClaimTileMemoryError(
            "claim-tile ledger must remain non-authoritative shadow state"
        )
    brand = ledger.get("brand")
    if (
        not isinstance(brand, dict)
        or not str(brand.get("domain") or "").strip()
    ):
        raise EvidenceReviewedClaimTileMemoryError(
            "claim-tile ledger brand domain is required"
        )


def _unique_mappings(rows: Iterable[Any]) -> dict[str, dict[str, Any]]:
    mappings: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise EvidenceReviewedClaimTileMemoryError(
                "claim-tile mapping must be an object"
            )
        mapping = dict(raw)
        mapping_id = str(mapping.get("mapping_id") or "")
        if mapping_id in mappings:
            raise EvidenceReviewedClaimTileMemoryError(
                f"duplicate claim-tile mapping: {mapping_id}"
            )
        if any(not str(mapping.get(field) or "") for field in _IDENTITY_FIELDS):
            raise EvidenceReviewedClaimTileMemoryError(
                f"claim-tile mapping identity is incomplete: {mapping_id}"
            )
        if (
            mapping.get("runtime_effect") is not False
            or mapping.get("authority") is not False
        ):
            raise EvidenceReviewedClaimTileMemoryError(
                f"claim-tile mapping is authoritative: {mapping_id}"
            )
        mappings[mapping_id] = mapping
    return mappings


def _validated_reviews(
    rows: Iterable[dict[str, Any]],
    *,
    mappings: dict[str, dict[str, Any]],
    packet_fingerprints: set[str],
) -> dict[str, dict[str, Any]]:
    reviews: dict[str, dict[str, Any]] = {}
    for raw in rows:
        review = dict(raw)
        mapping_id = str(
            review.get("mapping_id")
            or review.get("subject_id")
            or ""
        )
        if mapping_id not in mappings:
            raise EvidenceReviewedClaimTileMemoryError(
                f"review references unknown mapping: {mapping_id}"
            )
        if mapping_id in reviews:
            raise EvidenceReviewedClaimTileMemoryError(
                f"multiple current reviews for mapping: {mapping_id}"
            )
        _validate_review_shape(
            review,
            mapping=mappings[mapping_id],
            packet_fingerprints=packet_fingerprints,
        )
        reviews[mapping_id] = review
    return reviews


def _validate_review_shape(
    review: dict[str, Any],
    *,
    mapping: dict[str, Any],
    packet_fingerprints: set[str],
) -> None:
    event_id = str(review.get("event_id") or "")
    decision = str(review.get("decision") or "")
    if (
        not event_id
        or decision not in _REVIEW_DECISIONS
        or not str(review.get("reviewer_id") or "").strip()
        or not str(review.get("rationale") or "").strip()
        or not _valid_timestamp(review.get("reviewed_at"))
    ):
        raise EvidenceReviewedClaimTileMemoryError(
            "claim-tile review attribution is incomplete"
        )
    if (
        review.get("runtime_effect") is not False
        or review.get("authority") is not False
        or review.get("automatic_tile_effect") is not False
        or review.get("automatic_scoring_effect") is not False
    ):
        raise EvidenceReviewedClaimTileMemoryError(
            f"claim-tile review is authoritative: {event_id}"
        )
    if _review_packet_fingerprint(review) not in packet_fingerprints:
        raise EvidenceReviewedClaimTileMemoryError(
            f"claim-tile review packet mismatch: {event_id}"
        )
    for field in _IDENTITY_FIELDS:
        if str(review.get(field) or "") != str(mapping.get(field) or ""):
            raise EvidenceReviewedClaimTileMemoryError(
                f"claim-tile review {field} mismatch: {event_id}"
            )


def _semantic_mapping(mapping: dict[str, Any]) -> dict[str, str]:
    return {
        field: str(mapping.get(field) or "")
        for field in _IDENTITY_FIELDS
    }


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EvidenceReviewedClaimTileMemoryError(
            f"{field} is required"
        )
    return text


def _required_sha256(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise EvidenceReviewedClaimTileMemoryError(
            f"{field} must be sha256"
        )
    return text


def _review_packet_fingerprint(review: dict[str, Any]) -> str:
    return _required_sha256(
        review.get("review_packet_fingerprint"),
        field="review_packet_fingerprint",
    )


def _valid_timestamp(value: Any) -> bool:
    try:
        datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _state_fingerprint(payload: dict[str, Any]) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
