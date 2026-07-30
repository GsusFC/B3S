"""Deterministic block evidence shortlist worker for SV9 Flow."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.external_identity_provenance import (
    normalized_domain,
    verify_external_identity_provenance,
)
from src.sv9_flow.calibration_terms import block_evidence_policy
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_identity import canonical_evidence_id
from src.sv9_flow.evidence_source import (
    SOURCE_CLASS_ACQUISITION_METADATA,
    SOURCE_CLASS_DERIVED_STRATEGY,
    SOURCE_CLASS_EXTERNAL_PROOF,
    SOURCE_CLASS_OWNED_COPY,
    SOURCE_CLASS_VISUAL_SIGNAL,
    is_acquisition_noise,
    source_class_for_record,
)

_POLICY = block_evidence_policy()

BLOCK_EVIDENCE_SHORTLIST_VERSION = str(_POLICY["version"])
BLOCK_EVIDENCE_IDENTITY_GATE_VERSION = "sv9-flow-block-evidence-identity-gate-v1"

_DEFAULT_LIMIT = 5
_DEFAULT_EVALUATION_LIMIT = 8
EVALUATION_EVIDENCE_REFS_VERSION = "sv9-flow-evaluation-evidence-refs-v1"
_CHUNK_REF_RE = re.compile(r"^(?P<prefix>.+\.chunk\.)(?P<index>[1-9][0-9]*)$")

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
_EXPLICIT_PRIMARY_COPY_BONUS = 12

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
        block: _shortlist_for_block(block, evidence_pack, limit=limit)
        for block in block_names
    }


def build_block_evidence_identity_quarantine(
    evidence_pack: BrandEvidencePack,
) -> list[dict[str, object]]:
    """List external records retained in the pack but barred from shortlists."""

    rows: list[dict[str, object]] = []
    for record in evidence_pack.evidence:
        eligible, reasons = _identity_eligibility(evidence_pack, record)
        if eligible:
            continue
        rows.append(
            {
                "ref": record.ref,
                "url": record.url,
                "reason_codes": list(reasons),
            }
        )
    rows.sort(key=lambda row: str(row["ref"]))
    return rows


def build_evaluation_evidence_refs(
    evidence_pack: BrandEvidencePack,
    shortlists: dict[str, list[str]],
    *,
    limit: int = _DEFAULT_EVALUATION_LIMIT,
) -> dict[str, list[str]]:
    """Freeze evaluator inputs and recover local context around the main chunk.

    Interpretation refs remain the model's exact citations. This separate lane
    gives the tile evaluator the same bounded evidence on every run, including
    the adjacent chunks when a meaningful page section crosses a chunk boundary.
    """

    existing_refs = {record.ref for record in evidence_pack.evidence}
    output: dict[str, list[str]] = {}
    for block, shortlist in sorted(shortlists.items()):
        valid_shortlist = [
            ref for ref in shortlist if ref in existing_refs
        ]
        anchor = next(
            (
                ref
                for ref in valid_shortlist
                if _CHUNK_REF_RE.match(ref)
            ),
            "",
        )
        refs: list[str] = []
        for ref in valid_shortlist:
            _append_ref(refs, ref, existing_refs)
            if ref == anchor:
                for adjacent in _adjacent_chunk_refs(ref):
                    _append_ref(refs, adjacent, existing_refs)
            if len(refs) >= limit:
                break
        output[block] = refs[:limit]
    return output


def _shortlist_for_block(
    block: str,
    evidence_pack: BrandEvidencePack,
    *,
    limit: int,
) -> list[str]:
    scored: list[tuple[int, str, str]] = []
    for record in evidence_pack.evidence:
        eligible, _ = _identity_eligibility(evidence_pack, record)
        if not eligible:
            continue
        if _semantically_incidental_external_noise(record):
            continue
        score = _score_record(block, record)
        if score <= 0:
            continue
        scored.append((-score, canonical_evidence_id(record), record.ref))
    scored.sort()
    refs: list[str] = []
    for _, _, ref in scored:
        if ref not in refs:
            refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def _identity_eligibility(
    evidence_pack: BrandEvidencePack,
    record: EvidenceRecord,
) -> tuple[bool, tuple[str, ...]]:
    """Fail closed on unresolved external identity without deleting evidence."""

    if source_class_for_record(record) != SOURCE_CLASS_EXTERNAL_PROOF:
        return True, ()

    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    result_group = str(metadata.get("result_group") or "").strip().lower()
    intent = str(metadata.get("intent") or "").strip().lower()
    if result_group == "profiles" or intent == "external_profiles":
        return False, ("external_profile_not_strategic_evidence",)

    provenance = metadata.get("external_identity_provenance")
    if isinstance(provenance, dict):
        verified, reasons = verify_external_identity_provenance(
            provenance,
            brand_domain=normalized_domain(evidence_pack.url),
            source_domain=normalized_domain(record.url or ""),
            content=record.content,
        )
        if not verified:
            return False, tuple(reasons)

    if _explicit_subject_domain_anchor(evidence_pack, record):
        return True, ()

    deterministic_identity = str(
        metadata.get("identity_match") or ""
    ).strip().lower()
    if deterministic_identity == "none":
        return False, ("deterministic_identity_mismatch",)

    llm_identity = str(
        metadata.get("identity_match_llm") or ""
    ).strip().lower()
    if llm_identity == "none":
        return False, ("semantic_identity_mismatch",)

    if _legacy_same_name_different_root(evidence_pack, record):
        return False, ("legacy_same_name_different_root_unresolved",)
    return True, ()


def _explicit_subject_domain_anchor(
    evidence_pack: BrandEvidencePack,
    record: EvidenceRecord,
) -> bool:
    brand_domain = normalized_domain(evidence_pack.url)
    if not brand_domain:
        return False
    source_domain = normalized_domain(record.url or "")
    if (
        source_domain == brand_domain
        or source_domain.endswith(f".{brand_domain}")
    ):
        return True
    return brand_domain in str(record.content or "").casefold()


def _semantically_incidental_external_noise(
    record: EvidenceRecord,
) -> bool:
    """Keep an advisory neutral label from ranking via generic keywords."""

    if source_class_for_record(record) != SOURCE_CLASS_EXTERNAL_PROOF:
        return False
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    labeling_version = str(
        metadata.get("semantic_labeling_version") or ""
    ).strip()
    return bool(
        labeling_version.startswith("sv9-flow-evidence-labeling-")
        and metadata.get("relevant_blocks") == []
        and metadata.get("stance") == "neutral"
        and metadata.get("specificity") == "incidental"
    )


def _legacy_same_name_different_root(
    evidence_pack: BrandEvidencePack,
    record: EvidenceRecord,
) -> bool:
    """Recognize pre-provenance branded domains that cannot prove identity."""

    brand_domain = normalized_domain(evidence_pack.url)
    source_domain = normalized_domain(record.url or "")
    if not brand_domain or not source_domain:
        return False
    if source_domain == brand_domain or source_domain.endswith(f".{brand_domain}"):
        return False

    aliases = {
        re.sub(r"[^a-z0-9]+", "", evidence_pack.brand_name.casefold()),
        re.sub(
            r"[^a-z0-9]+",
            "",
            brand_domain.split(".", 1)[0].casefold(),
        ),
    }
    compact_source = re.sub(r"[^a-z0-9]+", "", source_domain.casefold())
    return any(
        len(alias) >= 4 and alias in compact_source
        for alias in aliases
        if alias
    )


def _adjacent_chunk_refs(ref: str) -> list[str]:
    match = _CHUNK_REF_RE.match(ref)
    if not match:
        return []
    index = int(match.group("index"))
    prefix = match.group("prefix")
    adjacent = []
    if index > 1:
        adjacent.append(f"{prefix}{index - 1}")
    adjacent.append(f"{prefix}{index + 1}")
    return adjacent


def _append_ref(
    refs: list[str],
    ref: str,
    existing_refs: set[str],
) -> None:
    if ref in existing_refs and ref not in refs:
        refs.append(ref)


def _score_record(block: str, record: EvidenceRecord) -> int:
    terms = _BLOCK_TERMS.get(block, ())
    source_class = source_class_for_record(record)
    stable_metadata = _stable_scoring_metadata(record)
    haystack = " ".join(
        (
            record.evidence_type,
            source_class,
            record.content,
            stable_metadata,
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
        score += _explicit_primary_copy_bonus(block, record)
        if record.ref.startswith("features."):
            score -= 4
        if source_class == SOURCE_CLASS_DERIVED_STRATEGY:
            score -= 6
    if block == "magnetism" and source_class == SOURCE_CLASS_VISUAL_SIGNAL:
        score -= 20
    if record.ref.startswith("features."):
        score += sum(1 for term in _FEATURE_BONUS_TERMS if term in haystack)
    return score


def _explicit_primary_copy_bonus(
    block: str,
    record: EvidenceRecord,
) -> int:
    """Keep explicit owned strategic statements above generic product copy."""

    if source_class_for_record(record) != SOURCE_CLASS_OWNED_COPY:
        return 0
    opening = " ".join(str(record.content or "").split())[:280].casefold()
    patterns = {
        "mission": r"\b(?:our|on a|the)\s+mission\b",
        "vision": r"\b(?:our|the)\s+vision\b",
        "core_purpose": r"\b(?:our|core)\s+purpose\b",
    }
    pattern = patterns.get(block)
    return (
        _EXPLICIT_PRIMARY_COPY_BONUS
        if pattern and re.search(pattern, opening)
        else 0
    )


def _stable_scoring_metadata(record: EvidenceRecord) -> str:
    """Keep shortlist scoring independent from provider rank and diagnostics."""

    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    values: list[str] = []
    for key in (
        "source_class",
        "intent",
        "identity_match",
        "relevant_blocks",
        "stance",
        "specificity",
    ):
        value = metadata.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in sorted(value, key=str))
        elif value not in (None, ""):
            values.append(str(value))
    return " ".join(values)


def _semantic_relevance_supports_block(record: EvidenceRecord, block: str) -> bool:
    relevant_blocks = record.metadata.get("relevant_blocks")
    if not isinstance(relevant_blocks, list) or block not in relevant_blocks:
        return False
    if record.metadata.get("specificity") == "incidental":
        return False
    return record.metadata.get("stance") in {"supports", "contradicts"}
