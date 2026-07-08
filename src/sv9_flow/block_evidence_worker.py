"""Deterministic block evidence shortlist worker for SV9 Flow."""

from __future__ import annotations

from dataclasses import dataclass

from src.sv9_flow.calibration_terms import block_evidence_policy
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_source import (
    SOURCE_CLASS_ACQUISITION_METADATA,
    SOURCE_CLASS_DERIVED_STRATEGY,
    SOURCE_CLASS_OWNED_COPY,
    SOURCE_CLASS_VISUAL_SIGNAL,
    is_acquisition_noise,
    source_class_for_record,
)

_POLICY = block_evidence_policy()

BLOCK_EVIDENCE_SHORTLIST_VERSION = str(_POLICY["version"])

_DEFAULT_LIMIT = 5

_BLOCK_TERMS: dict[str, tuple[str, ...]] = {
    key: tuple(values) for key, values in _POLICY["block_terms"].items()
}

_TYPE_BONUS: dict[str, int] = {
    "raw_input": 1,
    "visual_tile_signal": 1,
    "visual_capture": 0,
}

_FEATURE_BONUS_TERMS = tuple(_POLICY["feature_bonus_terms"])
_SEMANTIC_RELEVANCE_BONUS = 3

_TEXT_STRATEGY_BLOCKS = {
    "brand_idea",
    "mission",
    "vision",
    "value_proposition",
    "core_purpose",
    "values",
    "magnetism",
}

_PRIMARY_COPY_BLOCKS = {"mission", "vision", "core_purpose"}


@dataclass(frozen=True, slots=True)
class BlockEvidenceShortlist:
    block: str
    evidence_refs: list[str]
    version: str = BLOCK_EVIDENCE_SHORTLIST_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "block": self.block,
            "evidence_refs": list(self.evidence_refs),
        }


def build_block_evidence_shortlists(
    evidence_pack: BrandEvidencePack,
    *,
    blocks: tuple[str, ...] | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, list[str]]:
    """Select stable evidence refs per block before LLM drafting."""

    block_names = blocks or tuple(_BLOCK_TERMS)
    return {
        block: _shortlist_for_block(block, evidence_pack.evidence, limit=limit)
        for block in block_names
    }


def _shortlist_for_block(block: str, evidence: list[EvidenceRecord], *, limit: int) -> list[str]:
    scored: list[tuple[int, int, str]] = []
    for index, record in enumerate(evidence):
        score = _score_record(block, record)
        if score <= 0:
            continue
        scored.append((-score, index, record.ref))
    scored.sort()
    refs: list[str] = []
    for _, _, ref in scored:
        if ref not in refs:
            refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def _score_record(block: str, record: EvidenceRecord) -> int:
    terms = _BLOCK_TERMS.get(block, ())
    source_class = source_class_for_record(record)
    haystack = " ".join(
        (
            record.evidence_type,
            record.source,
            record.content,
            " ".join(str(value) for value in record.metadata.values()),
        )
    ).lower()
    score = sum(3 for term in terms if term in haystack)
    if _semantic_relevance_supports_block(record, block):
        score += _SEMANTIC_RELEVANCE_BONUS
    score += _TYPE_BONUS.get(record.evidence_type, 0)
    if is_acquisition_noise(record):
        score -= 20
    if block in _TEXT_STRATEGY_BLOCKS:
        if record.ref.startswith(("visual_signature.", "visual_acquisition.")) or record.source in {"visual_signature", "visual_acquisition"}:
            score -= 5
        if source_class == SOURCE_CLASS_ACQUISITION_METADATA:
            score -= 8
        if record.ref.startswith("raw_inputs."):
            score += 2
    if block in _PRIMARY_COPY_BLOCKS:
        if source_class == SOURCE_CLASS_OWNED_COPY and record.evidence_type == "raw_input":
            score += 5
        if record.ref.startswith("features."):
            score -= 4
        if source_class == SOURCE_CLASS_DERIVED_STRATEGY:
            score -= 6
    if block == "magnetism" and source_class == SOURCE_CLASS_VISUAL_SIGNAL:
        score -= 20
    if record.ref.startswith("features."):
        score += sum(1 for term in _FEATURE_BONUS_TERMS if term in haystack)
    if record.confidence == "high":
        score += 1
    return score


def _semantic_relevance_supports_block(record: EvidenceRecord, block: str) -> bool:
    relevant_blocks = record.metadata.get("relevant_blocks")
    if not isinstance(relevant_blocks, list) or block not in relevant_blocks:
        return False
    if record.metadata.get("specificity") == "incidental":
        return False
    return record.metadata.get("stance") in {"supports", "contradicts"}
