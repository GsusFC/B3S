#!/usr/bin/env python3
"""Compare SV9 Flow interpretation output across LLM models without scoring."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import BRAND3_DB_PATH
from src.features.llm_analyzer import LLMAnalyzer
from src.sv9_flow.block_evidence_worker import build_block_evidence_shortlists
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot
from src.sv9_flow.interpretation_llm_worker import build_brand_interpretation_with_llm
from src.sv9_flow.tile_signal_worker import build_tile_signals_from_interpretation


DEFAULT_MODELS = ("gemini-3.1-flash-lite",)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report-json", help="Web report JSON with raw.flow.candidate.evidence_pack.")
    source.add_argument("--candidate-json", help="SV9 Flow candidate JSON or envelope with candidate.evidence_pack.")
    source.add_argument("--evidence-pack-json", help="BrandEvidencePack JSON.")
    source.add_argument("--run-id", type=int, help="Brand audit run id to load from SQLite.")
    parser.add_argument("--db-path", default=BRAND3_DB_PATH)
    parser.add_argument("--model", action="append", dest="models", help="Model to test. Repeatable.")
    parser.add_argument(
        "--adjudicator-model",
        default="same",
        help="Adjudicator model. Use 'same' to reuse each tested model.",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--no-gates",
        action="store_true",
        help="Trust LLM detections with valid classified refs; skip deterministic detection gates.",
    )
    parser.add_argument(
        "--gate-authority",
        choices=("veto_only", "warn", "disabled"),
        default="",
        help="Detection gate mode. Overrides --no-gates when provided.",
    )
    parser.add_argument("--output-json", default="")
    parser.add_argument("--output-md", default="")
    parser.add_argument("--env-file", default="")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    evidence_pack = load_evidence_pack(args)
    models = tuple(args.models or DEFAULT_MODELS)
    payload = run_bakeoff(
        evidence_pack,
        models=models,
        repeat=max(1, args.repeat),
        adjudicator_model=args.adjudicator_model,
        gate_authority=args.gate_authority or ("disabled" if args.no_gates else "veto_only"),
    )
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    md_text = render_markdown(payload)

    if args.output_json:
        path = Path(args.output_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json_text + "\n", encoding="utf-8")
    if args.output_md:
        path = Path(args.output_md)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md_text + "\n", encoding="utf-8")
    if not args.output_json and not args.output_md:
        print(md_text)
    else:
        if args.output_json:
            print(f"JSON: {args.output_json}")
        if args.output_md:
            print(f"Markdown: {args.output_md}")
    return 0


def run_bakeoff(
    evidence_pack: BrandEvidencePack,
    *,
    models: tuple[str, ...],
    repeat: int = 1,
    adjudicator_model: str = "same",
    gate_authority: str = "veto_only",
) -> dict[str, Any]:
    runs = []
    for model in models:
        for index in range(repeat):
            runs.append(
                run_model_once(
                    evidence_pack,
                    model=model,
                    repeat_index=index + 1,
                    adjudicator_model=model if adjudicator_model == "same" else adjudicator_model,
                    gate_authority=gate_authority,
                )
            )
    return {
        "schema_version": "sv9-flow-model-bakeoff-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "brand_name": evidence_pack.brand_name,
        "url": evidence_pack.url,
        "evidence": {
            "record_count": len(evidence_pack.evidence),
            "limitations": list(evidence_pack.limitations),
            "source_counts": source_counts(evidence_pack),
        },
        "models": list(models),
        "repeat": repeat,
        "gate_authority": gate_authority,
        "runs": runs,
        "comparison": compare_runs(runs),
    }


def run_model_once(
    evidence_pack: BrandEvidencePack,
    *,
    model: str,
    repeat_index: int,
    adjudicator_model: str,
    gate_authority: str = "veto_only",
) -> dict[str, Any]:
    llm = LLMAnalyzer(model=model)
    adjudicator_llm = LLMAnalyzer(model=adjudicator_model) if adjudicator_model else None
    shortlists = build_block_evidence_shortlists(evidence_pack)
    interpretation, debug = build_brand_interpretation_with_llm(
        evidence_pack,
        llm=llm,
        adjudicator_llm=adjudicator_llm,
        block_evidence_shortlists=shortlists,
        gate_authority=gate_authority,
    )
    tile_signals = build_tile_signals_from_interpretation(interpretation)
    return {
        "model": model,
        "adjudicator_model": adjudicator_model,
        "repeat_index": repeat_index,
        "status": debug.get("status"),
        "gate_authority": debug.get("gate_authority"),
        "detected_blocks": detected_blocks(interpretation.blocks),
        "detected_count": len(detected_blocks(interpretation.blocks)),
        "blocks": summarize_blocks(interpretation.blocks, interpretation.evidence_refs),
        "tile_signals": summarize_tile_signals(tile_signals),
        "limitations": list(interpretation.limitations),
        "debug": {
            "failed_blocks": debug.get("failed_blocks") or [],
            "block_failures": debug.get("block_failures") or [],
            "detection_provenance": debug.get("detection_provenance") or {},
            "gate_disagreements": debug.get("gate_disagreements") or [],
            "failure_reason": debug.get("failure_reason"),
            "failure": debug.get("failure"),
        },
        "usage": {
            "interpretation": usage_summary(llm),
            "adjudicator": usage_summary(adjudicator_llm) if adjudicator_llm is not None else None,
        },
    }


def load_evidence_pack(args: argparse.Namespace) -> BrandEvidencePack:
    if args.report_json:
        payload = load_json(args.report_json)
        candidate = (((payload.get("raw") or {}).get("flow") or {}).get("candidate") or {})
        pack = candidate.get("evidence_pack") if isinstance(candidate, dict) else None
        if not isinstance(pack, dict):
            raise RuntimeError(f"No raw.flow.candidate.evidence_pack found in {args.report_json}")
        return evidence_pack_from_dict(pack)
    if args.candidate_json:
        payload = load_json(args.candidate_json)
        candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else payload
        pack = candidate.get("evidence_pack") if isinstance(candidate, dict) else None
        if not isinstance(pack, dict):
            raise RuntimeError(f"No candidate.evidence_pack found in {args.candidate_json}")
        return evidence_pack_from_dict(pack)
    if args.evidence_pack_json:
        return evidence_pack_from_dict(load_json(args.evidence_pack_json))
    if args.run_id:
        from src.storage.sqlite_store import SQLiteStore

        store = SQLiteStore(args.db_path)
        try:
            snapshot = store.get_run_snapshot(args.run_id)
        finally:
            store.close()
        if not snapshot:
            raise RuntimeError(f"No snapshot found for run id {args.run_id}")
        return build_evidence_pack_from_snapshot(snapshot)
    raise RuntimeError("No evidence source provided.")


def evidence_pack_from_dict(payload: dict[str, Any]) -> BrandEvidencePack:
    evidence = []
    for item in payload.get("evidence") or []:
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
        brand_name=str(payload.get("brand_name") or ""),
        url=str(payload.get("url") or ""),
        evidence=evidence,
        limitations=[str(item) for item in payload.get("limitations") or []],
    )


def summarize_blocks(blocks: dict[str, dict[str, Any]], evidence_refs: dict[str, list[str]]) -> dict[str, Any]:
    out = {}
    for key, block in sorted(blocks.items()):
        if not isinstance(block, dict):
            continue
        provenance = block.get("detection_provenance") if isinstance(block.get("detection_provenance"), dict) else {}
        out[key] = {
            "detected": block.get("detected") is True,
            "confidence": block.get("confidence") or "",
            "content": block.get("content") or "",
            "rejected_content": block.get("rejected_content") or "",
            "refs": evidence_refs.get(key) or [],
            "ref_count": len(evidence_refs.get(key) or []),
            "final_source": provenance.get("final_source") or "",
            "gate_reason": provenance.get("gate_reason") or "",
        }
    return out


def summarize_tile_signals(tile_signals: list[Any]) -> dict[str, Any]:
    by_component: dict[str, dict[str, int]] = {}
    rows = []
    for signal in tile_signals:
        row = signal.to_dict()
        rows.append(row)
        component = str(row.get("component") or "")
        effect = str(row.get("effect") or "")
        if component and effect:
            by_component.setdefault(component, {})
            by_component[component][effect] = by_component[component].get(effect, 0) + 1
    return {"count": len(rows), "by_component_effect": by_component, "items": rows}


def compare_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_model = {run["model"]: run for run in runs if run.get("repeat_index") == 1}
    if len(by_model) < 2:
        return {}
    models = sorted(by_model)
    left = by_model[models[0]]
    right = by_model[models[1]]
    left_detected = set(left.get("detected_blocks") or [])
    right_detected = set(right.get("detected_blocks") or [])
    changed_blocks = []
    for block in sorted(set((left.get("blocks") or {}).keys()) | set((right.get("blocks") or {}).keys())):
        l_block = (left.get("blocks") or {}).get(block) or {}
        r_block = (right.get("blocks") or {}).get(block) or {}
        if (
            l_block.get("detected") != r_block.get("detected")
            or l_block.get("final_source") != r_block.get("final_source")
            or l_block.get("refs") != r_block.get("refs")
        ):
            changed_blocks.append(block)
    return {
        "baseline_model": left["model"],
        "candidate_model": right["model"],
        "detected_added": sorted(right_detected - left_detected),
        "detected_removed": sorted(left_detected - right_detected),
        "changed_blocks": changed_blocks,
    }


def detected_blocks(blocks: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        key
        for key, block in blocks.items()
        if isinstance(block, dict) and block.get("detected") is True and str(block.get("content") or "").strip()
    )


def source_counts(evidence_pack: BrandEvidencePack) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in evidence_pack.evidence:
        counts[record.source] = counts.get(record.source, 0) + 1
    return dict(sorted(counts.items()))


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# SV9 Flow model bakeoff",
        "",
        f"- Marca: {payload['brand_name']}",
        f"- URL: {payload['url']}",
        f"- Evidencias: {payload['evidence']['record_count']} ({payload['evidence']['source_counts']})",
        f"- Modelos: {', '.join(payload['models'])}",
        f"- Gates: {payload.get('gate_authority') or 'n/a'}",
        "",
        "## Resumen",
        "",
        "| Modelo | Estado | Bloques detectados | Tile signals | Bloques no aceptados |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for run in payload["runs"]:
        lines.append(
            "| {model} | {status} | {blocks} | {tiles} | {failures} |".format(
                model=run["model"],
                status=run.get("status") or "n/a",
                blocks=", ".join(run.get("detected_blocks") or []) or "-",
                tiles=(run.get("tile_signals") or {}).get("count", 0),
                failures=", ".join(item.get("block") or "?" for item in run.get("debug", {}).get("block_failures") or []) or "-",
            )
        )
    comparison = payload.get("comparison") or {}
    if comparison:
        lines.extend(
            [
                "",
                "## Diferencias",
                "",
                f"- Añadidos por {comparison.get('candidate_model')}: {', '.join(comparison.get('detected_added') or []) or '-'}",
                f"- Perdidos por {comparison.get('candidate_model')}: {', '.join(comparison.get('detected_removed') or []) or '-'}",
                f"- Bloques cambiados: {', '.join(comparison.get('changed_blocks') or []) or '-'}",
            ]
        )
    for run in payload["runs"]:
        lines.extend(["", f"## {run['model']}", ""])
        blocks = run.get("blocks") or {}
        for key, block in blocks.items():
            status = "on" if block.get("detected") else "off"
            lines.append(
                f"- `{key}` {status} · {block.get('confidence') or 'n/a'} · {block.get('final_source') or 'n/a'} · refs {block.get('ref_count')}"
            )
            content = str(block.get("content") or block.get("rejected_content") or "").strip()
            if content:
                lines.append(f"  {compact(content, 220)}")
    return "\n".join(lines)


def usage_summary(llm: Any) -> dict[str, Any]:
    if llm is None:
        return {}
    summary_fn = getattr(llm, "usage_observation_summary", None)
    if callable(summary_fn):
        return summary_fn()
    return {
        "model": getattr(llm, "model", ""),
        "cache_hits": int(getattr(llm, "cache_hits", 0) or 0),
        "cache_misses": int(getattr(llm, "cache_misses", 0) or 0),
        "cache_writes": int(getattr(llm, "cache_writes", 0) or 0),
    }


def compact(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def load_json(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return payload


def load_env_file(path: str) -> None:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
