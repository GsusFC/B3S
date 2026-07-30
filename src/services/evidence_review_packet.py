"""Canonical identity for an unsigned human evidence-review packet."""

from __future__ import annotations

from typing import Any, Iterable

from src.evidence_identity import stable_artifact_digest


EVIDENCE_REVIEW_PACKET_SCHEMA_VERSION = "evidence-review-packet-v1"

_MUTABLE_MANIFEST_FIELDS = frozenset(
    {
        "review_event_fingerprint",
        "review_event_status",
        "review_fingerprint",
        "review_status",
        "review_packet_fingerprint",
        "status",
    }
)


def review_packet_fingerprint(
    *,
    packet_kind: str,
    manifest: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
    schema_versions: dict[str, str],
) -> str:
    """Bind reviewer context without creating a reviews/self-hash cycle."""

    normalized_manifest = {
        key: value
        for key, value in manifest.items()
        if key not in _MUTABLE_MANIFEST_FIELDS
    }
    normalized_candidates = sorted(
        (dict(row) for row in candidates),
        key=lambda row: str(row.get("case_id") or ""),
    )
    return stable_artifact_digest(
        EVIDENCE_REVIEW_PACKET_SCHEMA_VERSION,
        {
            "packet_kind": str(packet_kind),
            "manifest": normalized_manifest,
            "candidates": normalized_candidates,
            "schema_versions": dict(sorted(schema_versions.items())),
        },
    )
