"""Semantic evidence labeling pass for SV9 Flow.

This worker preserves the BrandEvidencePack shape and enriches its records.
Block relevance remains advisory; an explicit external-identity mismatch is a
fail-closed shortlist signal. Unavailable labeling still falls back to the
deterministic identity gate.
"""

from __future__ import annotations

import json
from typing import Any

from src.sv9_flow.calibration_terms import block_evidence_policy
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_identity import (
    canonical_evidence_payload,
    canonical_evidence_records,
    canonical_evidence_ref,
    evidence_record_for_ref,
    normalize_evidence_text,
    normalize_evidence_url,
    stable_artifact_digest,
)
from src.sv9_flow.evidence_source import (
    SOURCE_CLASS_ACQUISITION_METADATA,
    SOURCE_CLASS_OWNED_COPY,
    source_class_for_record,
)

EVIDENCE_LABELING_VERSION = "sv9-flow-evidence-labeling-v3"

_BLOCKS = tuple(block_evidence_policy()["block_terms"].keys())
_STANCES = {"supports", "contradicts", "neutral"}
_IDENTITY_MATCHES = {"domain", "brand_name", "none", "unverified"}
_SPECIFICITIES = {"explicit", "implied", "incidental"}
_MAX_RECORDS = 80
_CONTENT_CHARS = 900
_IDENTITY_CONTEXT_RECORDS = 4
_IDENTITY_CONTEXT_CHARS = 500

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
        "artifact_cache_hits": 0,
        "artifact_cache_misses": 0,
        "provider_records": 0,
        "identity_divergences": [],
    }
    if not _llm_available(llm):
        debug["reason"] = "missing_llm_api_key"
        return debug
    records = canonical_evidence_records(_candidate_records(evidence_pack))[:max_records]
    debug["records_considered"] = len(records)
    if not records:
        debug["reason"] = "no_candidate_records"
        return debug
    try:
        labels, cache_debug = _labels_with_artifact_cache(
            evidence_pack=evidence_pack,
            records=records,
            llm=llm,
        )
    except Exception as exc:
        debug["status"] = "failed"
        debug["reason"] = str(exc)[:300]
        return debug
    debug.update(cache_debug)
    applied = 0
    divergences: list[dict[str, str]] = []
    for label in labels:
        record = evidence_record_for_ref(label.get("ref") or "", evidence_pack)
        if record is None:
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


def _labels_with_artifact_cache(
    *,
    evidence_pack: BrandEvidencePack,
    records: list[EvidenceRecord],
    llm: Any,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    labels: list[dict[str, Any]] = []
    missing: list[EvidenceRecord] = []
    hits = 0
    for record in records:
        key = _record_label_cache_key(evidence_pack=evidence_pack, record=record, llm=llm)
        cached = _artifact_cache_get(llm, key)
        if isinstance(cached, dict) and isinstance(cached.get("label"), dict):
            label = _normalize_label(cached["label"])
            label["ref"] = record.ref
            labels.append(label)
            hits += 1
        else:
            missing.append(record)

    if missing:
        returned = _call_labeler(evidence_pack=evidence_pack, records=missing, llm=llm)
        failure_reason = str(getattr(llm, "last_failure_reason", None) or "").strip()
        if failure_reason:
            raise RuntimeError(f"evidence_labeling_provider_failed:{failure_reason}")
        returned_by_id: dict[str, dict[str, Any]] = {}
        for label in returned:
            record = evidence_record_for_ref(label.get("ref") or "", evidence_pack)
            if record is not None:
                returned_by_id[canonical_evidence_ref(record)] = label
        for record in missing:
            canonical_ref = canonical_evidence_ref(record)
            label = returned_by_id.get(canonical_ref) or _neutral_label(canonical_ref)
            normalized = _normalize_label(label)
            _artifact_cache_save(
                llm,
                _record_label_cache_key(evidence_pack=evidence_pack, record=record, llm=llm),
                {"label": {**normalized, "ref": canonical_ref}},
            )
            normalized["ref"] = record.ref
            labels.append(normalized)

    labels.sort(
        key=lambda label: canonical_evidence_ref(
            evidence_record_for_ref(label.get("ref") or "", evidence_pack)
        )
        if evidence_record_for_ref(label.get("ref") or "", evidence_pack) is not None
        else str(label.get("ref") or "")
    )
    return labels, {
        "artifact_cache_hits": hits,
        "artifact_cache_misses": len(missing),
        "provider_records": len(missing),
    }


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
        "Use contradicts only when the record works against the brand's own claim. "
        "Resolve identity against the supplied owned identity context, not the "
        "brand name alone: same-name companies are `none`; use `unverified` "
        "when the record lacks enough entity anchors to decide."
    )


def _user_prompt(*, evidence_pack: BrandEvidencePack, records: list[EvidenceRecord]) -> str:
    rows = []
    for record in canonical_evidence_records(records):
        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        rows.append(
            {
                "ref": canonical_evidence_ref(record),
                "source_class": metadata.get("source_class") or source_class_for_record(record),
                "evidence_type": record.evidence_type,
                "url": normalize_evidence_url(record.url),
                "content": normalize_evidence_text(record.content)[:_CONTENT_CHARS],
                "deterministic_identity_match": metadata.get("identity_match") or "",
            }
        )
    return json.dumps(
        {
            "prompt_version": EVIDENCE_LABELING_VERSION,
            "task": "Label evidence records for semantic relevance to canonical Brand3 blocks.",
            "brand": {"name": evidence_pack.brand_name, "url": evidence_pack.url},
            "owned_identity_context": _identity_context(evidence_pack),
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


def _identity_context(
    evidence_pack: BrandEvidencePack,
) -> list[dict[str, str]]:
    owned = [
        record
        for record in canonical_evidence_records(evidence_pack.evidence)
        if source_class_for_record(record) == SOURCE_CLASS_OWNED_COPY
        and record.content.strip()
    ]
    return [
        {
            "url": normalize_evidence_url(record.url),
            "content": normalize_evidence_text(record.content)[
                :_IDENTITY_CONTEXT_CHARS
            ],
        }
        for record in owned[:_IDENTITY_CONTEXT_RECORDS]
    ]


def _normalize_label(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": str(item.get("ref") or "").strip(),
        "relevant_blocks": _valid_blocks(item.get("relevant_blocks")),
        "stance": _valid_enum(item.get("stance"), _STANCES, default="neutral"),
        "identity_match": _valid_enum(item.get("identity_match"), _IDENTITY_MATCHES, default="unverified"),
        "specificity": _valid_enum(item.get("specificity"), _SPECIFICITIES, default="incidental"),
    }


def _neutral_label(ref: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "relevant_blocks": [],
        "stance": "neutral",
        "identity_match": "unverified",
        "specificity": "incidental",
    }


def _record_label_cache_key(
    *,
    evidence_pack: BrandEvidencePack,
    record: EvidenceRecord,
    llm: Any,
) -> str:
    return stable_artifact_digest(
        "sv9-flow-evidence-label",
        {
            "version": EVIDENCE_LABELING_VERSION,
            "model": str(getattr(llm, "model", "") or ""),
            "base_url": str(getattr(llm, "base_url", "") or ""),
            "system_prompt": _system_prompt(),
            "response_schema": _LABEL_SCHEMA,
            "brand": {
                "name": normalize_evidence_text(evidence_pack.brand_name).casefold(),
                "url": normalize_evidence_url(evidence_pack.url),
            },
            "owned_identity_context": _identity_context(evidence_pack),
            "record": canonical_evidence_payload(record),
            "deterministic_identity_match": str(
                (record.metadata if isinstance(record.metadata, dict) else {}).get("identity_match")
                or ""
            ),
        },
    )


def _artifact_cache_get(llm: Any, cache_key: str) -> dict[str, Any] | None:
    getter = getattr(llm, "_cache_get", None)
    if not callable(getter):
        return None
    cached = getter(cache_key, "json")
    return cached if isinstance(cached, dict) else None


def _artifact_cache_save(llm: Any, cache_key: str, value: dict[str, Any]) -> None:
    saver = getattr(llm, "_cache_save", None)
    if callable(saver):
        saver(cache_key, "json", value)


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
