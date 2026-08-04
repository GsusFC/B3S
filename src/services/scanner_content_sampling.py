"""Deterministic disclosure of owned-page content sampling.

Page acquisition and evidence retention are different coverage layers. A URL
may have been fetched successfully while only a strategic sample of its text
was retained in the evidence pack. This module exposes that distinction
without changing interpretation or scoring.
"""

from __future__ import annotations

from typing import Any, Iterable


CONTENT_SAMPLING_VERSION = "content-sampling-disclosure-v1"


def content_sampling_from_evidence(rows: Iterable[Any] | None) -> dict[str, Any]:
    surfaces: list[dict[str, Any]] = []
    for row in rows or []:
        if _value(row, "evidence_type") != "acquisition.evidence_sampling":
            continue
        metadata = _value(row, "metadata")
        if not isinstance(metadata, dict):
            continue
        total = _non_negative_int(metadata.get("total_chunks"))
        retained = _non_negative_int(metadata.get("selected_chunks"))
        omitted = max(total - retained, 0)
        if not omitted:
            continue
        surfaces.append(
            {
                "ref": str(_value(row, "ref") or "").strip(),
                "url": str(_value(row, "url") or "").strip(),
                "available_chunk_count": total,
                "retained_chunk_count": retained,
                "omitted_chunk_count": omitted,
                "strategic_prioritization": metadata.get(
                    "strategic_prioritization"
                )
                is True,
            }
        )

    affected_urls = {surface["url"] for surface in surfaces if surface["url"]}
    return {
        "schema_version": CONTENT_SAMPLING_VERSION,
        "sampling_record_count": len(surfaces),
        "affected_url_count": len(affected_urls),
        "available_chunk_count": sum(
            surface["available_chunk_count"] for surface in surfaces
        ),
        "retained_chunk_count": sum(
            surface["retained_chunk_count"] for surface in surfaces
        ),
        "omitted_chunk_count": sum(
            surface["omitted_chunk_count"] for surface in surfaces
        ),
        "surfaces": surfaces,
    }


def content_sampling_from_report(report: dict[str, Any]) -> dict[str, Any]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    candidate = (
        flow.get("candidate")
        if isinstance(flow.get("candidate"), dict)
        else {}
    )
    pack = (
        candidate.get("evidence_pack")
        if isinstance(candidate.get("evidence_pack"), dict)
        else {}
    )
    evidence = pack.get("evidence") if isinstance(pack.get("evidence"), list) else []
    return content_sampling_from_evidence(evidence)


def _value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _non_negative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
