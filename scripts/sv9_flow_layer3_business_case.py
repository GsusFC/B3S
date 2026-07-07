"""Offline business-case measurement for SV9 Flow Layer 3.

This script does not change reports, scoring, prompts, or runtime behavior. It
measures the gate described in docs/brand3_evidence_layer3_labeling_design.md:
how many blocks remain `insufficient_acquisition`, and optionally how many of
those blocks have semantically relevant evidence in the saved evidence pack.
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.config import LLM_CHEAP_MODEL
from src.features.llm_analyzer import LLMAnalyzer
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_source import SOURCE_CLASS_ACQUISITION_METADATA, source_class_for_record

LAYER3_BUSINESS_CASE_VERSION = "sv9-flow-layer3-business-case-v1"

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
                    "relevant_blocks": {"type": "array", "items": {"type": "string"}},
                    "stance": {"type": "string", "enum": ["supports", "contradicts", "neutral"]},
                    "identity_match": {"type": "string", "enum": ["domain", "brand_name", "none", "unverified"]},
                    "specificity": {"type": "string", "enum": ["explicit", "implied", "incidental"]},
                },
                "required": ["ref", "relevant_blocks", "stance", "identity_match", "specificity"],
            },
        }
    },
    "required": ["labels"],
}


@dataclass
class Layer3Case:
    report_id: str
    brand_name: str
    url: str
    block: str
    status: str
    candidate_refs_checked: int
    relevant_refs: list[dict[str, Any]] = field(default_factory=list)


def measure_reports(
    report_paths: list[Path],
    *,
    llm: Any | None = None,
    max_records_per_report: int = 80,
) -> dict[str, Any]:
    cases: list[Layer3Case] = []
    reports_scanned = 0
    reports_with_flow = 0
    insufficient_total = 0
    label_errors: list[dict[str, str]] = []
    by_block: Counter[str] = Counter()
    relevant_by_block: Counter[str] = Counter()

    for path in report_paths:
        reports_scanned += 1
        payload = _load_json(path)
        if not payload:
            continue
        pack = _evidence_pack_from_report(payload)
        coverage_blocks = _coverage_blocks_from_report(payload)
        if pack is None or not coverage_blocks:
            continue
        reports_with_flow += 1
        insufficient_blocks = sorted(
            block for block, block_payload in coverage_blocks.items() if block_payload.get("status") == "insufficient_acquisition"
        )
        if not insufficient_blocks:
            continue
        insufficient_total += len(insufficient_blocks)
        by_block.update(insufficient_blocks)
        candidate_records = _candidate_records(pack)[:max_records_per_report]
        labels: dict[str, dict[str, Any]] = {}
        if llm is not None and _llm_available(llm):
            try:
                labels = _labels_by_ref(
                    label_records_for_blocks(
                        pack=pack,
                        blocks=insufficient_blocks,
                        records=candidate_records,
                        llm=llm,
                    )
                )
            except Exception as exc:  # measurement must not stop the batch
                label_errors.append({"report": path.name, "error": str(exc)[:300]})
        for block in insufficient_blocks:
            relevant_refs = [
                _case_ref_payload(record, labels.get(record.ref) or {})
                for record in candidate_records
                if _label_counts_for_block(labels.get(record.ref) or {}, block)
            ]
            if relevant_refs:
                relevant_by_block[block] += 1
            cases.append(
                Layer3Case(
                    report_id=str(payload.get("id") or path.stem),
                    brand_name=str(payload.get("brand_name") or pack.brand_name),
                    url=str(payload.get("url") or pack.url),
                    block=block,
                    status="latent_relevant_evidence" if relevant_refs else "no_labeled_relevant_evidence",
                    candidate_refs_checked=len(candidate_records),
                    relevant_refs=relevant_refs,
                )
            )

    semantic_measured = bool(llm is not None and _llm_available(llm))
    latent_cases = [case for case in cases if case.relevant_refs]
    return {
        "schema_version": LAYER3_BUSINESS_CASE_VERSION,
        "semantic_measured": semantic_measured,
        "llm_status": "available" if semantic_measured else "not_run",
        "reports_scanned": reports_scanned,
        "reports_with_flow": reports_with_flow,
        "insufficient_acquisition_blocks": insufficient_total,
        "latent_relevant_blocks": len(latent_cases) if semantic_measured else None,
        "reports_with_latent_relevance": len({case.report_id for case in latent_cases}) if semantic_measured else None,
        "by_block": dict(sorted(by_block.items())),
        "latent_by_block": dict(sorted(relevant_by_block.items())) if semantic_measured else None,
        "label_errors": label_errors,
        "cases": [asdict(case) for case in cases],
    }


def label_records_for_blocks(
    *,
    pack: BrandEvidencePack,
    blocks: list[str],
    records: list[EvidenceRecord],
    llm: Any,
) -> list[dict[str, Any]]:
    if not blocks or not records or not _llm_available(llm):
        return []
    raw = llm._call_json(
        _labeling_system_prompt(),
        _labeling_user_prompt(pack=pack, blocks=blocks, records=records),
        max_tokens=8000,
        json_schema=_LABEL_SCHEMA,
        schema_name="sv9_flow_layer3_business_case_labels",
    )
    if not isinstance(raw, dict):
        return []
    labels = raw.get("labels")
    if not isinstance(labels, list):
        return []
    return [_normalize_label(item) for item in labels if isinstance(item, dict)]


def _labeling_system_prompt() -> str:
    return (
        "You label Brand3 evidence records for a measurement-only pass. "
        "Return strict JSON. Do not infer facts not present in the record. "
        "Only mark a block relevant when the record actually speaks to that strategic block."
    )


def _labeling_user_prompt(*, pack: BrandEvidencePack, blocks: list[str], records: list[EvidenceRecord]) -> str:
    rows = []
    for record in records:
        rows.append(
            {
                "ref": record.ref,
                "source": record.source,
                "source_class": source_class_for_record(record),
                "url": record.url or "",
                "content": record.content[:900],
                "identity_match": record.metadata.get("identity_match") or "",
            }
        )
    return json.dumps(
        {
            "brand_name": pack.brand_name,
            "url": pack.url,
            "insufficient_blocks_to_check": blocks,
            "labels_required": {
                "relevant_blocks": "subset of insufficient_blocks_to_check",
                "stance": "supports | contradicts | neutral",
                "identity_match": "domain | brand_name | none | unverified",
                "specificity": "explicit | implied | incidental",
            },
            "records": rows,
        },
        ensure_ascii=False,
        indent=2,
    )


def _normalize_label(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": str(item.get("ref") or "").strip(),
        "relevant_blocks": [str(block) for block in item.get("relevant_blocks") or [] if str(block).strip()],
        "stance": str(item.get("stance") or "neutral"),
        "identity_match": str(item.get("identity_match") or "unverified"),
        "specificity": str(item.get("specificity") or "incidental"),
    }


def _labels_by_ref(labels: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {label["ref"]: label for label in labels if label.get("ref")}


def _label_counts_for_block(label: dict[str, Any], block: str) -> bool:
    if block not in (label.get("relevant_blocks") or []):
        return False
    if label.get("specificity") == "incidental":
        return False
    return label.get("stance") in {"supports", "contradicts"}


def _case_ref_payload(record: EvidenceRecord, label: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": record.ref,
        "source": record.source,
        "source_class": source_class_for_record(record),
        "url": record.url or "",
        "content_excerpt": record.content[:220],
        "label": label,
    }


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


def _evidence_pack_from_report(payload: dict[str, Any]) -> BrandEvidencePack | None:
    candidate = (((payload.get("raw") or {}).get("flow") or {}).get("candidate") or {})
    pack_payload = candidate.get("evidence_pack") if isinstance(candidate, dict) else None
    if not isinstance(pack_payload, dict):
        return None
    evidence = []
    for item in pack_payload.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        evidence.append(
            EvidenceRecord(
                ref=str(item.get("ref") or ""),
                source=str(item.get("source") or ""),
                evidence_type=str(item.get("evidence_type") or ""),
                content=str(item.get("content") or ""),
                url=item.get("url"),
                confidence=item.get("confidence") if item.get("confidence") in {"low", "medium", "high"} else "medium",
                metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
            )
        )
    return BrandEvidencePack(
        brand_name=str(pack_payload.get("brand_name") or payload.get("brand_name") or ""),
        url=str(pack_payload.get("url") or payload.get("url") or ""),
        evidence=evidence,
        limitations=[str(item) for item in pack_payload.get("limitations") or []],
    )


def _coverage_blocks_from_report(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    debug = (((payload.get("raw") or {}).get("flow") or {}).get("interpretation_debug") or {})
    coverage = debug.get("evidence_coverage") if isinstance(debug, dict) else {}
    blocks = coverage.get("blocks") if isinstance(coverage, dict) else {}
    return blocks if isinstance(blocks, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _llm_available(llm: Any) -> bool:
    return bool(llm is not None and getattr(llm, "api_key", None))


def _report_paths(patterns: list[str], *, limit: int | None = None) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        matched = sorted(Path(match) for match in glob.glob(pattern))
        if matched:
            paths.extend(matched)
            continue
        path = Path(pattern)
        if path.exists():
            paths.append(path)
    unique = sorted(dict.fromkeys(paths), key=lambda path: path.stat().st_mtime, reverse=True)
    return unique[:limit] if limit else unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure SV9 Flow Layer 3 business-case gate.")
    parser.add_argument("reports", nargs="*", default=["data/reports/*.json"], help="Report JSON path or glob.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum reports to scan, newest first.")
    parser.add_argument("--llm", action="store_true", help="Run semantic labeling with LLMAnalyzer.")
    parser.add_argument("--model", default="", help="Override labeling model.")
    parser.add_argument("--max-records-per-report", type=int, default=80)
    parser.add_argument("--output-json", default="", help="Optional output JSON path.")
    args = parser.parse_args(argv)

    paths = _report_paths(args.reports, limit=args.limit or None)
    llm = LLMAnalyzer(model=args.model or LLM_CHEAP_MODEL) if args.llm else None
    result = measure_reports(paths, llm=llm, max_records_per_report=args.max_records_per_report)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_json:
        Path(args.output_json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
