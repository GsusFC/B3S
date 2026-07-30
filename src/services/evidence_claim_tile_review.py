"""Durable human-review contract for evidence-to-claim-to-tile mappings.

The claim-to-tile ledger proves literal provenance. It does not prove that a
claim satisfies the semantic contract of a tile. This module defines the
append-only, non-authoritative review boundary for that final semantic step.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


EVIDENCE_CLAIM_TILE_REVIEW_EVENT_VERSION = (
    "evidence-claim-tile-review-event-v2"
)
EVIDENCE_CLAIM_TILE_REVIEW_POLICY_VERSION = (
    "evidence-claim-tile-review-policy-v1"
)
CLAIM_TILE_REVIEW_SUBJECT_TYPE = "claim_tile_mapping"
CLAIM_TILE_REVIEW_DECISIONS = frozenset(
    {"accepted", "disputed", "rejected", "revoked"}
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class EvidenceClaimTileReviewJournalError(RuntimeError):
    """Base error for the durable claim-to-tile review boundary."""


class EvidenceClaimTileReviewUnavailableError(
    EvidenceClaimTileReviewJournalError
):
    """The durable claim-to-tile review journal is unavailable."""


class EvidenceClaimTileReviewNotFoundError(
    EvidenceClaimTileReviewJournalError
):
    """The brand or immutable claim-to-tile mapping does not exist."""


class EvidenceClaimTileReviewInvalidTransitionError(
    EvidenceClaimTileReviewJournalError
):
    """The requested decision cannot follow the current review event."""


class EvidenceClaimTileReviewConflictError(
    EvidenceClaimTileReviewJournalError
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
class EvidenceClaimTileReviewCommand:
    """Validated command passed to the append-only PostgreSQL journal."""

    subject_id: str
    decision: str
    expected_current_event_id: str | None
    reviewer: str
    reason_code: str
    rationale: str
    evaluator_version: str
    review_packet_fingerprint: str
    actor_id: str
    idempotency_key_hash: str
    request_fingerprint: str


def claim_tile_review_case_id(
    domain: str,
    mapping: dict[str, Any],
) -> str:
    """Build a stable human-readable locator for one immutable mapping."""

    mapping_id = str(mapping.get("mapping_id") or "").strip().lower()
    component_key = _slug(
        str(mapping.get("component_key") or "component"),
        maximum=60,
    )
    tile_id = _slug(
        str(mapping.get("tile_id") or "tile"),
        maximum=60,
    )
    domain_slug = _slug(domain, maximum=80)
    return (
        f"claim-tile-{domain_slug}-{component_key}-{tile_id}-"
        f"{mapping_id[:12]}"
    )


def _slug(value: str, *, maximum: int) -> str:
    normalized = _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")
    bounded = normalized[:maximum].strip("-")
    return bounded or "unknown"
