"""Stable identities and prompt aliases for SV9 Flow evidence.

Collector refs describe where a record happened to appear in one acquisition
run (for example ``raw_inputs.2.exa.mentions.3``).  They are useful provenance,
but they are not evidence identity: provider ordering can change while the
observed URL and content stay the same.

This module keeps the original refs for reports and resolves a content-addressed
alias only at semantic/LLM boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.evidence_identity import (
    EVIDENCE_ALIAS_PREFIX,
    EVIDENCE_IDENTITY_VERSION,
    canonical_evidence_alias,
    canonical_evidence_digest,
    canonical_evidence_material,
    normalize_evidence_text,
    normalize_evidence_url,
    stable_artifact_digest,
)
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_source import source_class_for_record


def canonical_evidence_payload(record: EvidenceRecord) -> dict[str, str]:
    """Return the material fields that define one observed evidence item."""

    return canonical_evidence_material(
        source_class=source_class_for_record(record),
        evidence_type=record.evidence_type,
        url=record.url,
        content=record.content,
    )


def canonical_evidence_id(record: EvidenceRecord) -> str:
    return canonical_evidence_digest(
        source_class=source_class_for_record(record),
        evidence_type=record.evidence_type,
        url=record.url,
        content=record.content,
    )


def canonical_evidence_ref(record: EvidenceRecord) -> str:
    """Return the ref exposed to semantic models.

    A full digest avoids collision handling and remains opaque to the model.
    """

    return canonical_evidence_alias(
        source_class=source_class_for_record(record),
        evidence_type=record.evidence_type,
        url=record.url,
        content=record.content,
    )


def canonical_evidence_records(records: Iterable[EvidenceRecord]) -> list[EvidenceRecord]:
    """Deduplicate and order evidence independently from collector positions."""

    first_by_id: dict[str, EvidenceRecord] = {}
    for record in records:
        identity = canonical_evidence_id(record)
        current = first_by_id.get(identity)
        if current is None or str(record.ref) < str(current.ref):
            first_by_id[identity] = record
    return [first_by_id[identity] for identity in sorted(first_by_id)]


def canonical_evidence_set_digest(evidence_pack: BrandEvidencePack) -> str:
    return stable_artifact_digest(
        "sv9-flow-evidence-set",
        {
            "version": EVIDENCE_IDENTITY_VERSION,
            "brand": {
                "name": normalize_evidence_text(evidence_pack.brand_name).casefold(),
                "url": normalize_evidence_url(evidence_pack.url),
            },
            "records": [
                canonical_evidence_payload(record)
                for record in canonical_evidence_records(evidence_pack.evidence)
            ],
        },
    )


def canonical_ref_for_original(ref: str, evidence_pack: BrandEvidencePack) -> str:
    record = evidence_record_for_ref(ref, evidence_pack)
    if record is None:
        return str(ref or "").strip()
    return canonical_evidence_ref(record)


def original_ref_for_canonical(ref: str, evidence_pack: BrandEvidencePack) -> str:
    candidate = str(ref or "").strip()
    if not candidate.startswith(EVIDENCE_ALIAS_PREFIX):
        return candidate
    records = canonical_evidence_records(evidence_pack.evidence)
    for record in records:
        if canonical_evidence_ref(record) == candidate:
            return record.ref
    return candidate


def canonicalize_evidence_refs(
    refs: Iterable[Any],
    evidence_pack: BrandEvidencePack,
) -> list[str]:
    out: list[str] = []
    for value in refs:
        ref = str(value or "").strip()
        if not ref:
            continue
        canonical = canonical_ref_for_original(ref, evidence_pack)
        if canonical not in out:
            out.append(canonical)
    return out


def restore_evidence_refs(
    refs: Iterable[Any],
    evidence_pack: BrandEvidencePack,
) -> list[str]:
    out: list[str] = []
    for value in refs:
        ref = original_ref_for_canonical(str(value or ""), evidence_pack)
        if ref and ref not in out:
            out.append(ref)
    return out


def evidence_record_for_ref(
    ref: str,
    evidence_pack: BrandEvidencePack,
) -> EvidenceRecord | None:
    candidate = str(ref or "").strip()
    for record in evidence_pack.evidence:
        if record.ref == candidate or canonical_evidence_ref(record) == candidate:
            return record
    return None
