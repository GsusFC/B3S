"""Deterministic evidence coverage summaries for SV9 Flow.

Coverage is acquisition/accountability metadata. It does not interpret brand
strategy and it does not change the BrandInterpretation contract.
"""

from __future__ import annotations

from typing import Any, Literal

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


def acquisition_coverage(pack: BrandEvidencePack) -> dict[str, Any]:
    """Summarize what acquisition observed, attempted, and failed."""

    owned_urls: list[str] = []
    external_attempts: list[dict[str, Any]] = []
    external_proof_refs: list[str] = []
    absence_refs: list[dict[str, str]] = []
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
        absence_refs = [
            record.ref
            for record in pack.evidence
            if record.evidence_type == f"acquisition.absence.{block}"
        ]
        absence_surface_count = _distinct_absence_surface_count(pack, block)
        if positive_refs:
            status: BlockCoverageStatus = "positive_evidence"
        elif _is_implied_not_explicit(block_payload, block, absence_blocks_by_surface):
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
            "absence_refs": absence_refs,
            "cited_refs": cited_refs,
            "absence_surface_count": absence_surface_count,
        }
    return coverage


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
    return source_class_for_record(record) != SOURCE_CLASS_ACQUISITION_METADATA


def _attempt_summary(record: EvidenceRecord) -> dict[str, Any]:
    return {
        "ref": record.ref,
        "provider": str(record.metadata.get("provider") or record.source),
        "intent": str(record.metadata.get("intent") or ""),
        "status": str(record.metadata.get("status") or ""),
        "query": str(record.metadata.get("query") or ""),
        "error": str(record.metadata.get("error") or ""),
    }


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
