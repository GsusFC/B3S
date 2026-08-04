"""Deterministic evidence coverage summaries for SV9 Flow.

Coverage is acquisition/accountability metadata. It does not interpret brand
strategy and it does not change the BrandInterpretation contract.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from src.evidence_identity import normalize_evidence_url
from src.services.scanner_content_sampling import content_sampling_from_evidence
from src.sv9_flow.contracts import BrandEvidencePack, BrandInterpretation, EvidenceRecord
from src.sv9_flow.evidence_source import (
    SOURCE_CLASS_ACQUISITION_METADATA,
    SOURCE_CLASS_EXTERNAL_PROOF,
    SOURCE_CLASS_OWNED_COPY,
    source_class_for_record,
)

BlockCoverageStatus = Literal[
    "positive_evidence",
    "implied_not_explicit",
    "verified_absent",
    "probable_absent",
    "insufficient_acquisition",
]

_CANONICAL_BLOCKS = (
    "brand_idea",
    "mission",
    "vision",
    "values",
    "personality",
    "value_proposition",
    "core_purpose",
    "attributes",
    "magnetism",
)

COMPONENT_SURFACE_HIERARCHY_VERSION = "component-surface-hierarchy-v1"


def acquisition_coverage(pack: BrandEvidencePack) -> dict[str, Any]:
    """Summarize what acquisition observed, attempted, and failed."""

    owned_urls: list[str] = []
    external_attempts: list[dict[str, Any]] = []
    external_proof_refs: list[str] = []
    absence_refs: list[dict[str, str]] = []
    owned_page_coverage: dict[str, Any] = {}
    by_ref = {record.ref: record for record in pack.evidence}

    for record in pack.evidence:
        source_class = source_class_for_record(record)
        if source_class == SOURCE_CLASS_OWNED_COPY and record.url:
            _append_unique(owned_urls, record.url)
        if source_class == SOURCE_CLASS_EXTERNAL_PROOF:
            external_proof_refs.append(record.ref)
        if record.evidence_type.startswith("acquisition.attempt."):
            external_attempts.append(_attempt_summary(record))
        if record.evidence_type.startswith("acquisition.absence."):
            absence_refs.append(
                {
                    "ref": record.ref,
                    "block": record.evidence_type.rsplit(".", 1)[-1],
                    "url": str(record.url or record.metadata.get("checked_url") or ""),
                }
            )
        if record.evidence_type == "acquisition.page_selection":
            selection = record.metadata.get("page_selection")
            if isinstance(selection, dict):
                owned_page_coverage = _owned_page_coverage(selection)

    return {
        "owned_urls": owned_urls,
        "owned_url_count": len(owned_urls),
        "external_proof_refs": external_proof_refs,
        "external_proof_count": len(external_proof_refs),
        "external_attempts": external_attempts,
        "external_attempt_count": len(external_attempts),
        "absence_refs": absence_refs,
        "absence_ref_count": len(absence_refs),
        "evidence_record_count": len(by_ref),
        "owned_page_coverage": owned_page_coverage,
        "content_sampling": content_sampling_from_evidence(pack.evidence),
    }


def block_coverage(pack: BrandEvidencePack, interpretation: BrandInterpretation) -> dict[str, dict[str, Any]]:
    """Join pack evidence with interpretation refs to classify each block."""

    records_by_ref = {record.ref: record for record in pack.evidence}
    absence_blocks_by_surface = _absence_blocks_by_surface(pack)
    blocks = tuple(dict.fromkeys(_CANONICAL_BLOCKS + tuple(interpretation.blocks.keys())))
    coverage: dict[str, dict[str, Any]] = {}
    for block in blocks:
        block_payload = interpretation.blocks.get(block) if isinstance(interpretation.blocks.get(block), dict) else {}
        final_detected = block_payload.get("detected") is True
        cited_refs = [str(ref) for ref in interpretation.evidence_refs.get(block) or [] if str(ref).strip()]
        positive_refs = [
            ref
            for ref in cited_refs
            if final_detected and _is_positive_ref(records_by_ref.get(ref))
        ]
        counter_refs = [
            ref
            for ref in cited_refs
            if _is_counter_ref(records_by_ref.get(ref), block)
        ]
        absence_refs = [
            record.ref
            for record in pack.evidence
            if record.evidence_type == f"acquisition.absence.{block}"
        ]
        absence_surface_count = _distinct_absence_surface_count(pack, block)
        if positive_refs:
            status: BlockCoverageStatus = "positive_evidence"
        elif _is_implied_not_explicit(block_payload, block, absence_blocks_by_surface) or _has_implied_semantic_ref(
            block,
            cited_refs,
            records_by_ref,
        ):
            status = "implied_not_explicit"
        elif absence_surface_count >= 2:
            status = "verified_absent"
        elif absence_surface_count == 1:
            status = "probable_absent"
        else:
            status = "insufficient_acquisition"
        coverage[block] = {
            "status": status,
            "positive_refs": positive_refs,
            "counter_refs": counter_refs,
            "absence_refs": absence_refs,
            "cited_refs": cited_refs,
            "absence_surface_count": absence_surface_count,
        }
    return coverage


def component_surface_hierarchy(
    pack: BrandEvidencePack,
    interpretation: BrandInterpretation,
) -> dict[str, dict[str, Any]]:
    """Locate each detected component's cited evidence within owned navigation.

    This is a deterministic accountability projection, not a score. It joins
    exact interpretation refs with the page-selection manifest so the report
    can distinguish evidence on the homepage, linked pages, sitemap-only
    surfaces, and external sources.
    """

    records_by_ref = {record.ref: record for record in pack.evidence}
    page_selection = _page_selection(pack)
    known_pages = {
        normalize_evidence_url(row.get("url")): dict(row)
        for row in page_selection.get("known_pages") or []
        if isinstance(row, dict) and normalize_evidence_url(row.get("url"))
    }
    visited_urls = {
        normalize_evidence_url(row.get("url"))
        for row in page_selection.get("visited_pages") or []
        if isinstance(row, dict)
        and str(row.get("status") or "") == "captured"
        and normalize_evidence_url(row.get("url"))
    }
    brand_site = _site_key(pack.url)
    blocks = tuple(dict.fromkeys(_CANONICAL_BLOCKS + tuple(interpretation.blocks.keys())))
    hierarchy: dict[str, dict[str, Any]] = {}

    for block in blocks:
        block_payload = (
            interpretation.blocks.get(block)
            if isinstance(interpretation.blocks.get(block), dict)
            else {}
        )
        cited_refs = _unique_strings(interpretation.evidence_refs.get(block) or [])
        surfaces_by_url: dict[str, dict[str, Any]] = {}
        external_refs: list[str] = []
        uncategorized_refs: list[str] = []

        for ref in cited_refs:
            record = records_by_ref.get(ref)
            if record is None:
                uncategorized_refs.append(ref)
                continue
            normalized_url = normalize_evidence_url(record.url)
            if not normalized_url:
                uncategorized_refs.append(ref)
                continue
            page = known_pages.get(normalized_url)
            if page is None and _site_key(normalized_url) != brand_site:
                external_refs.append(ref)
                continue

            navigation_status = str((page or {}).get("navigation_status") or "")
            if not navigation_status and normalized_url == normalize_evidence_url(pack.url):
                navigation_status = "homepage"
            if not navigation_status:
                navigation_status = "owned_unknown"
            surface = surfaces_by_url.setdefault(
                normalized_url,
                {
                    "url": str(record.url or normalized_url),
                    "navigation_status": navigation_status,
                    "captured": normalized_url in visited_urls,
                    "cited_refs": [],
                },
            )
            surface["cited_refs"].append(ref)

        owned_surfaces = list(surfaces_by_url.values())
        counts = {
            "homepage": sum(
                1
                for row in owned_surfaces
                if row["navigation_status"] == "homepage"
            ),
            "linked_from_home": sum(
                1
                for row in owned_surfaces
                if row["navigation_status"] == "linked_from_home"
            ),
            "sitemap_only": sum(
                1
                for row in owned_surfaces
                if row["navigation_status"] == "sitemap_only"
            ),
            "owned_unknown": sum(
                1
                for row in owned_surfaces
                if row["navigation_status"] == "owned_unknown"
            ),
            "external": len(external_refs),
        }
        hierarchy[block] = {
            "schema_version": COMPONENT_SURFACE_HIERARCHY_VERSION,
            "presence_status": (
                "detected"
                if block_payload.get("detected") is True
                else "not_detected"
            ),
            "hierarchy_status": _hierarchy_status(
                detected=block_payload.get("detected") is True,
                counts=counts,
            ),
            "owned_surface_count": len(owned_surfaces),
            "external_ref_count": len(external_refs),
            "uncategorized_ref_count": len(uncategorized_refs),
            "counts": counts,
            "owned_surfaces": owned_surfaces,
            "external_refs": external_refs,
            "uncategorized_refs": uncategorized_refs,
        }
    return hierarchy


def coverage_limitations(blocks: dict[str, dict[str, Any]]) -> list[str]:
    """Expose non-positive block coverage as stable candidate limitations."""

    out: list[str] = []
    for block, payload in sorted(blocks.items()):
        status = str(payload.get("status") or "")
        if status == "verified_absent":
            out.append(f"coverage:{block}_verified_absent")
        elif status == "probable_absent":
            out.append(f"coverage:{block}_probable_absent")
        elif status == "implied_not_explicit":
            out.append(f"coverage:{block}_implied_not_explicit")
        elif status == "insufficient_acquisition":
            out.append(f"coverage:{block}_insufficient_acquisition")
    return out


def _page_selection(pack: BrandEvidencePack) -> dict[str, Any]:
    for record in pack.evidence:
        if record.evidence_type != "acquisition.page_selection":
            continue
        selection = record.metadata.get("page_selection")
        if isinstance(selection, dict):
            return selection
    return {}


def _site_key(value: Any) -> str:
    normalized = normalize_evidence_url(value)
    if not normalized:
        return ""
    host = urlparse(normalized).hostname or ""
    return host.lower().removeprefix("www.")


def _hierarchy_status(*, detected: bool, counts: dict[str, int]) -> str:
    if not detected:
        return "not_detected"
    has_visible_owned = bool(counts["homepage"] or counts["linked_from_home"])
    if counts["sitemap_only"] and has_visible_owned:
        return "mixed_with_sitemap_only"
    if counts["sitemap_only"]:
        return "sitemap_only"
    if counts["homepage"]:
        return "homepage"
    if counts["linked_from_home"]:
        return "linked_from_home"
    if counts["owned_unknown"]:
        return "owned_unknown"
    if counts["external"]:
        return "external_only"
    return "unlocated"


def _unique_strings(values: Any) -> list[str]:
    unique: list[str] = []
    for item in values:
        value = str(item or "").strip()
        if value and value not in unique:
            unique.append(value)
    return unique


def _absence_blocks_by_surface(pack: BrandEvidencePack) -> dict[str, set[str]]:
    """Map each checked strategic surface (absence-record ref prefix) to the blocks it lacked."""

    by_surface: dict[str, set[str]] = {}
    for record in pack.evidence:
        if not record.evidence_type.startswith("acquisition.absence."):
            continue
        block = record.evidence_type.rsplit(".", 1)[-1]
        surface = record.ref.split(".absence.", 1)[0]
        by_surface.setdefault(surface, set()).add(block)
    return by_surface


def _distinct_absence_surface_count(pack: BrandEvidencePack, block: str) -> int:
    urls: set[str] = set()
    for record in pack.evidence:
        if record.evidence_type != f"acquisition.absence.{block}":
            continue
        url = str(record.url or record.metadata.get("checked_url") or "").strip()
        if url:
            urls.add(url)
    return len(urls)


def _is_implied_not_explicit(
    block_payload: dict[str, Any],
    name: str,
    absence_blocks_by_surface: dict[str, set[str]],
) -> bool:
    """The LLM read the block off owned copy but the structural gate vetoed it.

    Only claim "implied" when some checked strategic surface actually carried
    the block's terms — it emitted absence records for other blocks but not for
    this one. Otherwise the gate veto plus absence records fall through to the
    graduated absence rule below.
    """

    provenance = block_payload.get("detection_provenance")
    if not isinstance(provenance, dict):
        return False
    if provenance.get("llm_detected") is not True:
        return False
    if provenance.get("final_source") != "gate_rejected":
        return False
    return any(name not in lacked for lacked in absence_blocks_by_surface.values())


def _is_positive_ref(record: EvidenceRecord | None) -> bool:
    if record is None:
        return False
    if record.evidence_type.startswith("acquisition."):
        return False
    if record.metadata.get("stance") == "contradicts":
        return False
    return source_class_for_record(record) != SOURCE_CLASS_ACQUISITION_METADATA


def _is_counter_ref(record: EvidenceRecord | None, block: str) -> bool:
    if record is None:
        return False
    if record.evidence_type.startswith("acquisition."):
        return False
    relevant_blocks = record.metadata.get("relevant_blocks")
    return (
        isinstance(relevant_blocks, list)
        and block in relevant_blocks
        and record.metadata.get("stance") == "contradicts"
        and source_class_for_record(record) != SOURCE_CLASS_ACQUISITION_METADATA
    )


def _has_implied_semantic_ref(
    block: str,
    cited_refs: list[str],
    records_by_ref: dict[str, EvidenceRecord],
) -> bool:
    for ref in cited_refs:
        record = records_by_ref.get(ref)
        if record is None:
            continue
        relevant_blocks = record.metadata.get("relevant_blocks")
        if not isinstance(relevant_blocks, list) or block not in relevant_blocks:
            continue
        if record.metadata.get("stance") != "supports":
            continue
        if record.metadata.get("specificity") == "implied":
            return True
    return False


def _attempt_summary(record: EvidenceRecord) -> dict[str, Any]:
    return {
        "ref": record.ref,
        "provider": str(record.metadata.get("provider") or record.source),
        "intent": str(record.metadata.get("intent") or ""),
        "status": str(record.metadata.get("status") or ""),
        "query": str(record.metadata.get("query") or ""),
        "error": str(record.metadata.get("error") or ""),
    }


def _owned_page_coverage(selection: dict[str, Any]) -> dict[str, Any]:
    known_pages = [
        dict(row)
        for row in selection.get("known_pages") or []
        if isinstance(row, dict)
    ]
    visited_pages = [
        dict(row)
        for row in selection.get("visited_pages") or []
        if isinstance(row, dict)
    ]
    not_visited_pages = [
        dict(row)
        for row in selection.get("not_visited_pages") or []
        if isinstance(row, dict)
    ]
    excluded_pages = [
        dict(row)
        for row in selection.get("excluded_pages") or []
        if isinstance(row, dict)
    ]
    known_count = int(selection.get("known_page_count") or len(known_pages))
    captured_count = int(
        selection.get("captured_page_count")
        or sum(
            1
            for row in visited_pages
            if str(row.get("status") or "") == "captured"
        )
    )
    attempted_count = max(
        captured_count,
        int(selection.get("attempted_page_count") or len(visited_pages)),
    )
    ratio = (
        round(captured_count / known_count, 4)
        if known_count
        else 0.0
    )
    eligible_not_visited_count = int(
        selection.get("eligible_not_visited_count")
        or sum(
            1
            for row in not_visited_pages
            if str(row.get("reason") or "") == "page_budget"
        )
    )
    eligible_captured_count = int(
        selection.get("eligible_captured_page_count") or captured_count
    )
    eligible_count = int(
        selection.get("eligible_page_count")
        or attempted_count + eligible_not_visited_count
    )
    coverage = {
        "selection_version": str(selection.get("version") or ""),
        "known_page_count": known_count,
        "attempted_page_count": attempted_count,
        "captured_page_count": captured_count,
        "coverage_ratio": ratio,
        "eligible_page_count": eligible_count,
        "eligible_captured_page_count": eligible_captured_count,
        "eligible_not_visited_count": eligible_not_visited_count,
        "eligible_coverage_ratio": (
            round(eligible_captured_count / eligible_count, 4)
            if eligible_count
            else 0.0
        ),
        "known_pages": known_pages,
        "visited_pages": visited_pages,
        "not_visited_pages": not_visited_pages,
        "excluded_pages": excluded_pages,
        "latest_lastmod": str(selection.get("latest_lastmod") or ""),
        "language_detection": (
            dict(selection.get("language_detection"))
            if isinstance(selection.get("language_detection"), dict)
            else {}
        ),
    }
    if str(selection.get("discovery_status") or "").strip():
        coverage.update(
            {
                "discovery_status": str(selection.get("discovery_status") or ""),
                "discovery_sources": [
                    str(source)
                    for source in selection.get("discovery_sources") or []
                    if str(source).strip()
                ],
                "discovery_limitations": [
                    str(limitation)
                    for limitation in selection.get("discovery_limitations") or []
                    if str(limitation).strip()
                ],
            }
        )
    return coverage


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
