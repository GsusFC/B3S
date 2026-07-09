"""Semantic evidence labeling pass for SV9 Flow.

This worker enriches evidence records with advisory metadata only. It does not
change the BrandEvidencePack shape, scoring, or runtime contracts; unavailable
or failing labeling falls back to the deterministic evidence path.
"""

from __future__ import annotations

import json
from typing import Any

from src.sv9_flow.calibration_terms import block_evidence_policy
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_source import SOURCE_CLASS_ACQUISITION_METADATA, source_class_for_record

EVIDENCE_LABELING_VERSION = "sv9-flow-evidence-labeling-v1"

_BLOCKS = tuple(block_evidence_policy()["block_terms"].keys())
_STANCES = {"supports", "contradicts", "neutral"}
_IDENTITY_MATCHES = {"domain", "brand_name", "none", "unverified"}
_SPECIFICITIES = {"explicit", "implied", "incidental"}
_MAX_RECORDS = 80
_CONTENT_CHARS = 900

_LABEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string"},
                    "relevant_blocks": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(_BLOCKS)},
                    },
                    "stance": {"type": "string", "enum": sorted(_STANCES)},
                    "identity_match": {"type": "string", "enum": sorted(_IDENTITY_MATCHES)},
                    "specificity": {"type": "string", "enum": sorted(_SPECIFICITIES)},
                },
                "required": ["ref", "relevant_blocks", "stance", "identity_match", "specificity"],
            },
        }
    },
    "required": ["labels"],
}


def label_evidence_pack(
    evidence_pack: BrandEvidencePack,
    *,
    llm: Any | None,
    max_records: int = _MAX_RECORDS,
) -> dict[str, Any]:
    """Annotate evidence records with semantic labels, returning debug info."""

    debug: dict[str, Any] = {
        "version": EVIDENCE_LABELING_VERSION,
        "status": "skipped",
        "reason": "",
        "records_considered": 0,
        "records_labeled": 0,
        "identity_divergences": [],
    }
    if not _llm_available(llm):
        debug["reason"] = "missing_llm_api_key"
        return debug
    records = _candidate_records(evidence_pack)[:max_records]
    debug["records_considered"] = len(records)
    if not records:
        debug["reason"] = "no_candidate_records"
        return debug
    try:
        labels = _call_labeler(evidence_pack=evidence_pack, records=records, llm=llm)
    except Exception as exc:
        debug["status"] = "failed"
        debug["reason"] = str(exc)[:300]
        return debug
    by_ref = {record.ref: record for record in records}
    applied = 0
    divergences: list[dict[str, str]] = []
    for label in labels:
        record = by_ref.get(label.get("ref") or "")
        if record is None:
            continue
        if not label["relevant_blocks"] and label["stance"] == "neutral" and label["specificity"] == "incidental":
            continue
        record.metadata["relevant_blocks"] = list(label["relevant_blocks"])
        record.metadata["stance"] = label["stance"]
        record.metadata["identity_match_llm"] = label["identity_match"]
        record.metadata["specificity"] = label["specificity"]
        record.metadata["semantic_labeling_version"] = EVIDENCE_LABELING_VERSION
        applied += 1
        deterministic_identity = str(record.metadata.get("identity_match") or "").strip()
        if deterministic_identity and deterministic_identity != label["identity_match"]:
            divergences.append(
                {
                    "ref": record.ref,
                    "deterministic_identity_match": deterministic_identity,
                    "llm_identity_match": label["identity_match"],
                }
            )
    debug["status"] = "labeled"
    debug["reason"] = ""
    debug["records_labeled"] = applied
    debug["identity_divergences"] = divergences
    return debug


def _call_labeler(*, evidence_pack: BrandEvidencePack, records: list[EvidenceRecord], llm: Any) -> list[dict[str, Any]]:
    raw = llm._call_json(
        _system_prompt(),
        _user_prompt(evidence_pack=evidence_pack, records=records),
        max_tokens=8000,
        json_schema=_LABEL_SCHEMA,
        schema_name="sv9_flow_evidence_labeling",
        temperature=0.0,  # deterministic evidence labeling
    )
    if not isinstance(raw, dict):
        return []
    labels = raw.get("labels")
    if not isinstance(labels, list):
        return []
    return [_normalize_label(item) for item in labels if isinstance(item, dict)]


def _system_prompt() -> str:
    return (
        "You label Brand3 evidence records for an advisory semantic pass. "
        "Return strict JSON only. Do not infer facts not present in the record. "
        "Only mark a block relevant when the record actually speaks to that strategic block. "
        "Use contradicts only when the record works against the brand's own claim."
    )


def _user_prompt(*, evidence_pack: BrandEvidencePack, records: list[EvidenceRecord]) -> str:
    rows = []
    for record in records:
        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        rows.append(
            {
                "ref": record.ref,
                "source": record.source,
                "source_class": metadata.get("source_class") or source_class_for_record(record),
                "evidence_type": record.evidence_type,
                "url": record.url or "",
                "content": record.content[:_CONTENT_CHARS],
                "deterministic_identity_match": metadata.get("identity_match") or "",
            }
        )
    return json.dumps(
        {
            "prompt_version": EVIDENCE_LABELING_VERSION,
            "task": "Label evidence records for semantic relevance to canonical Brand3 blocks.",
            "brand": {"name": evidence_pack.brand_name, "url": evidence_pack.url},
            "canonical_blocks": list(_BLOCKS),
            "labels_required": {
                "relevant_blocks": "subset of canonical_blocks; empty if the record is not useful for any block",
                "stance": "supports | contradicts | neutral",
                "identity_match": "domain | brand_name | none | unverified",
                "specificity": "explicit | implied | incidental",
            },
            "records": rows,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _normalize_label(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": str(item.get("ref") or "").strip(),
        "relevant_blocks": _valid_blocks(item.get("relevant_blocks")),
        "stance": _valid_enum(item.get("stance"), _STANCES, default="neutral"),
        "identity_match": _valid_enum(item.get("identity_match"), _IDENTITY_MATCHES, default="unverified"),
        "specificity": _valid_enum(item.get("specificity"), _SPECIFICITIES, default="incidental"),
    }


def _valid_blocks(raw: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        value = str(item or "").strip()
        if value in _BLOCKS and value not in out:
            out.append(value)
    return out


def _valid_enum(raw: Any, allowed: set[str], *, default: str) -> str:
    value = str(raw or "").strip().lower()
    return value if value in allowed else default


def _candidate_records(pack: BrandEvidencePack) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    for record in pack.evidence:
        if not record.content.strip():
            continue
        if record.evidence_type.startswith("acquisition."):
            continue
        if source_class_for_record(record) == SOURCE_CLASS_ACQUISITION_METADATA:
            continue
        records.append(record)
    return records


def _llm_available(llm: Any | None) -> bool:
    return bool(llm is not None and getattr(llm, "api_key", None))
