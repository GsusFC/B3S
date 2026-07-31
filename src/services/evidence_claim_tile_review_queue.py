"""Derived human-review queue for exact claim-to-tile packets.

The durable packet registry and append-only review journal remain the sources
of truth. This projection only decides whether a current packet candidate can
reuse an earlier semantic review or must return to human review.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable

from src.evidence_identity import stable_artifact_digest
from src.services.evidence_claim_tile_review_packet import (
    EvidenceClaimTileReviewPacketError,
    validate_evidence_claim_tile_review_packet,
)


EVIDENCE_CLAIM_TILE_REVIEW_QUEUE_VERSION = (
    "evidence-claim-tile-review-queue-v1"
)
EVIDENCE_CLAIM_TILE_REVIEW_QUEUE_POLICY_VERSION = (
    "evidence-claim-tile-review-queue-policy-v1"
)
EVIDENCE_CLAIM_TILE_REVIEW_TARGET_VERSION = (
    "evidence-claim-tile-review-target-v1"
)

_REVIEW_DECISIONS = frozenset(
    {"accepted", "disputed", "rejected", "revoked"}
)
_MAPPING_IDENTITY_FIELDS = (
    "mapping_id",
    "mapping_series_id",
    "source_evidence_id",
    "claim_variant_id",
    "component_key",
    "tile_id",
    "tile_key",
    "polarity",
)
_NON_AUTHORITATIVE_FIELDS = (
    "runtime_effect",
    "authority",
    "automatic_tile_effect",
    "automatic_scoring_effect",
)


class EvidenceClaimTileReviewQueueError(ValueError):
    """The review queue cannot be derived without weakening its contract."""


def build_evidence_claim_tile_review_queue(
    current_packet: dict[str, Any],
    reviews: Iterable[dict[str, Any]],
    *,
    registered_packets: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Classify exact packet candidates as reviewed or pending review.

    Repeated observations do not invalidate a decision. A changed source
    passage, claim, tile contract, evidence quote, or matching method does.
    """

    current = _validated_packet(current_packet)
    current_fingerprint = _packet_fingerprint(current)
    packets_by_fingerprint = _registered_packets_by_fingerprint(
        registered_packets
    )
    if current_fingerprint not in packets_by_fingerprint:
        raise EvidenceClaimTileReviewQueueError(
            "the current review packet is not in the registered packet set"
        )

    current_candidates = {
        str(candidate["subject_id"]): dict(candidate)
        for candidate in current["candidates"]
    }
    reviews_by_subject = _validated_reviews(
        reviews,
        current_candidates=current_candidates,
        packets_by_fingerprint=packets_by_fingerprint,
    )

    items: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    observation_states: Counter[str] = Counter()
    for candidate in current["candidates"]:
        subject_id = str(candidate["subject_id"])
        mapping = dict(candidate["mapping"])
        observation_state = str(mapping.get("state") or "unknown")
        observation_states[observation_state] += 1
        target_fingerprint = review_target_fingerprint(candidate)
        review = reviews_by_subject.get(subject_id)
        reviewed_target_fingerprint: str | None = None
        decision: str | None = None
        event_id: str | None = None

        if review is None:
            status = "pending_review"
            reason = "unreviewed_mapping"
        else:
            decision = str(review["decision"])
            event_id = str(review["event_id"])
            review_packet = packets_by_fingerprint[
                _review_packet_fingerprint(review)
            ]
            reviewed_candidate = _candidate_for_subject(
                review_packet,
                subject_id,
            )
            reviewed_target_fingerprint = review_target_fingerprint(
                reviewed_candidate
            )
            if decision == "revoked":
                status = "pending_review"
                reason = "review_revoked"
            elif reviewed_target_fingerprint != target_fingerprint:
                status = "pending_review"
                reason = "review_target_changed"
            else:
                status = "reviewed"
                reason = "review_target_unchanged"
                decisions[decision] += 1

        reasons[reason] += 1
        items.append(
            {
                "case_id": str(candidate["case_id"]),
                "subject_id": subject_id,
                "review_status": status,
                "review_reason": reason,
                "review_target_fingerprint": target_fingerprint,
                "reviewed_target_fingerprint": (
                    reviewed_target_fingerprint
                ),
                "current_review_event_id": event_id,
                "expected_current_event_id": event_id,
                "current_decision": decision,
                "observation_state": observation_state,
                "present_in_latest_report": bool(
                    mapping.get("present_in_latest_report")
                ),
                "present_in_series_latest": bool(
                    mapping.get("present_in_series_latest")
                ),
                "candidate": dict(candidate),
            }
        )

    pending_subject_ids = [
        str(item["subject_id"])
        for item in items
        if item["review_status"] == "pending_review"
    ]
    reviewed_subject_ids = [
        str(item["subject_id"])
        for item in items
        if item["review_status"] == "reviewed"
    ]
    result = {
        "schema_version": EVIDENCE_CLAIM_TILE_REVIEW_QUEUE_VERSION,
        "policy_version": (
            EVIDENCE_CLAIM_TILE_REVIEW_QUEUE_POLICY_VERSION
        ),
        "queue_kind": "claim_tile_human_review",
        "packet_fingerprint": current_fingerprint,
        "candidate_fingerprint": str(
            current["manifest"]["candidate_fingerprint"]
        ),
        "mapping_series_id": str(
            current["manifest"]["mapping_series_id"]
        ),
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
        "review_complete": not pending_subject_ids and bool(items),
        "summary": {
            "candidate_count": len(items),
            "reviewed_count": len(reviewed_subject_ids),
            "pending_review_count": len(pending_subject_ids),
            "unreviewed_mapping_count": int(
                reasons["unreviewed_mapping"]
            ),
            "changed_review_target_count": int(
                reasons["review_target_changed"]
            ),
            "revoked_review_count": int(reasons["review_revoked"]),
            "accepted_count": int(decisions["accepted"]),
            "disputed_count": int(decisions["disputed"]),
            "rejected_count": int(decisions["rejected"]),
            "observation_state_counts": dict(
                sorted(observation_states.items())
            ),
        },
        "pending_subject_ids": pending_subject_ids,
        "reviewed_subject_ids": reviewed_subject_ids,
        "items": items,
        "warnings": [
            "queue_is_derived_from_registered_packets_and_current_reviews",
            "observation_repetition_alone_does_not_reopen_review",
            "changed_semantic_review_target_requires_human_review",
            "no_runtime_tile_or_scoring_effect",
        ],
    }
    result["queue_fingerprint"] = stable_artifact_digest(
        EVIDENCE_CLAIM_TILE_REVIEW_QUEUE_VERSION,
        result,
    )
    return result


def review_target_fingerprint(candidate: dict[str, Any]) -> str:
    """Hash only the semantic context that the human actually reviews."""

    if not isinstance(candidate, dict):
        raise EvidenceClaimTileReviewQueueError(
            "review target candidate must be an object"
        )
    try:
        mapping = dict(candidate["mapping"])
        source = dict(candidate["source"])
        claim = dict(candidate["claim"])
        tile = dict(candidate["tile"])
        brand = dict(candidate["brand"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceClaimTileReviewQueueError(
            "review target candidate context is incomplete"
        ) from exc
    material = {
        "case_type": str(candidate.get("case_type") or ""),
        "brand_domain": str(brand.get("domain") or ""),
        "source": source,
        "claim": claim,
        "tile": tile,
        "mapping": {
            field: str(mapping.get(field) or "")
            for field in _MAPPING_IDENTITY_FIELDS
        }
        | {"match_method": str(mapping.get("match_method") or "")},
        "review_prompt": str(candidate.get("review_prompt") or ""),
    }
    if any(
        not str(material["mapping"].get(field) or "")
        for field in (*_MAPPING_IDENTITY_FIELDS, "match_method")
    ):
        raise EvidenceClaimTileReviewQueueError(
            "review target mapping identity is incomplete"
        )
    return stable_artifact_digest(
        EVIDENCE_CLAIM_TILE_REVIEW_TARGET_VERSION,
        material,
    )


def _validated_packet(packet: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(packet, dict):
        raise EvidenceClaimTileReviewQueueError(
            "registered review packet must be an object"
        )
    normalized = dict(packet)
    try:
        validate_evidence_claim_tile_review_packet(normalized)
    except EvidenceClaimTileReviewPacketError as exc:
        raise EvidenceClaimTileReviewQueueError(
            "registered review packet failed canonical validation"
        ) from exc
    fingerprint = _packet_fingerprint(normalized)
    if (
        normalized.get("packet_fingerprint") is not None
        and str(normalized["packet_fingerprint"]) != fingerprint
    ):
        raise EvidenceClaimTileReviewQueueError(
            "registered review packet fingerprint mismatch"
        )
    for field in _NON_AUTHORITATIVE_FIELDS:
        if (
            field in normalized
            and normalized.get(field) is not False
        ):
            raise EvidenceClaimTileReviewQueueError(
                f"registered review packet {field} must remain false"
            )
    return normalized


def _registered_packets_by_fingerprint(
    packets: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    brand_domain: str | None = None
    for raw in packets:
        packet = _validated_packet(raw)
        fingerprint = _packet_fingerprint(packet)
        if fingerprint in result:
            raise EvidenceClaimTileReviewQueueError(
                "registered packet set contains duplicate fingerprints"
            )
        packet_domain = str(
            (packet.get("manifest", {}).get("brand") or {}).get("domain")
            or ""
        )
        if brand_domain is None:
            brand_domain = packet_domain
        elif packet_domain != brand_domain:
            raise EvidenceClaimTileReviewQueueError(
                "registered packet set mixes brand domains"
            )
        result[fingerprint] = packet
    return result


def _validated_reviews(
    rows: Iterable[dict[str, Any]],
    *,
    current_candidates: dict[str, dict[str, Any]],
    packets_by_fingerprint: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise EvidenceClaimTileReviewQueueError(
                "current review event must be an object"
            )
        review = dict(raw)
        subject_id = str(
            review.get("subject_id")
            or review.get("mapping_id")
            or ""
        )
        if subject_id not in current_candidates:
            raise EvidenceClaimTileReviewQueueError(
                "current review references a subject outside the packet"
            )
        if subject_id in result:
            raise EvidenceClaimTileReviewQueueError(
                "multiple current reviews exist for one packet subject"
            )
        decision = str(review.get("decision") or "")
        event_id = str(review.get("event_id") or review.get("id") or "")
        if (
            decision not in _REVIEW_DECISIONS
            or not event_id
            or not str(review.get("reviewer_id") or "").strip()
            or not str(review.get("rationale") or "").strip()
            or not _valid_timestamp(review.get("reviewed_at"))
        ):
            raise EvidenceClaimTileReviewQueueError(
                "current claim-to-tile review attribution is incomplete"
            )
        for field in _NON_AUTHORITATIVE_FIELDS:
            if review.get(field) is not False:
                raise EvidenceClaimTileReviewQueueError(
                    "current claim-to-tile review is authoritative"
                )
        packet_fingerprint = _review_packet_fingerprint(review)
        packet = packets_by_fingerprint.get(packet_fingerprint)
        if packet is None:
            raise EvidenceClaimTileReviewQueueError(
                "current review references an unavailable registered packet"
            )
        reviewed_candidate = _candidate_for_subject(packet, subject_id)
        reviewed_mapping = dict(reviewed_candidate["mapping"])
        for field in _MAPPING_IDENTITY_FIELDS:
            if str(review.get(field) or "") != str(
                reviewed_mapping.get(field) or ""
            ):
                raise EvidenceClaimTileReviewQueueError(
                    f"current review {field} does not match its packet"
                )
        result[subject_id] = review
    return result


def _candidate_for_subject(
    packet: dict[str, Any],
    subject_id: str,
) -> dict[str, Any]:
    matches = [
        dict(candidate)
        for candidate in packet["candidates"]
        if str(candidate.get("subject_id") or "") == subject_id
    ]
    if len(matches) != 1:
        raise EvidenceClaimTileReviewQueueError(
            "review subject is missing or ambiguous in its registered packet"
        )
    return matches[0]


def _packet_fingerprint(packet: dict[str, Any]) -> str:
    fingerprint = str(
        (packet.get("manifest") or {}).get(
            "review_packet_fingerprint"
        )
        or ""
    )
    if not _is_sha256(fingerprint):
        raise EvidenceClaimTileReviewQueueError(
            "registered review packet fingerprint must be sha256"
        )
    return fingerprint


def _review_packet_fingerprint(review: dict[str, Any]) -> str:
    fingerprint = str(
        review.get("review_packet_fingerprint") or ""
    )
    if not _is_sha256(fingerprint):
        raise EvidenceClaimTileReviewQueueError(
            "current review packet fingerprint must be sha256"
        )
    return fingerprint


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _valid_timestamp(value: Any) -> bool:
    try:
        datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
