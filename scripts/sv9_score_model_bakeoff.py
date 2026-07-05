#!/usr/bin/env python3
"""Compare full SV9 scores across LLM models over one fixed evidence pack."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sv9_flow_model_bakeoff import (
    DEFAULT_MODELS,
    load_env_file,
    load_evidence_pack,
    source_counts,
    usage_summary,
)
from src.features.llm_analyzer import LLMAnalyzer
from src.sv9.service import run_sv9_from_audit_snapshot
from src.sv9_flow.block_evidence_worker import build_block_evidence_shortlists
from src.sv9_flow.contracts import BrandEvidencePack, Sv9FlowCandidate
from src.sv9_flow.interpretation_llm_worker import build_brand_interpretation_with_llm
from src.sv9_flow.tile_signal_worker import build_tile_signals_from_interpretation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report-json", help="Web report JSON with raw.flow.candidate.evidence_pack.")
    source.add_argument("--candidate-json", help="SV9 Flow candidate JSON or envelope with candidate.evidence_pack.")
    source.add_argument("--evidence-pack-json", help="BrandEvidencePack JSON.")
    source.add_argument("--run-id", type=int, help="Brand audit run id to load from SQLite.")
    parser.add_argument("--db-path", default="")
    parser.add_argument("--model", action="append", dest="models", help="Model to test. Repeatable.")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--adjudicator-model",
        default="same",
        help="Adjudicator model. Use 'same' to reuse each tested model.",
    )
    parser.add_argument(
        "--reasoning-model",
        default="same",
        help="Reasoning/evaluator model for Magnetism and Coherencia. Use 'same' to reuse each tested model.",
    )
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
    payload = run_score_bakeoff(
        evidence_pack,
        models=tuple(args.models or DEFAULT_MODELS),
        repeat=max(1, args.repeat),
        adjudicator_model=args.adjudicator_model,
        reasoning_model=args.reasoning_model,
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


def run_score_bakeoff(
    evidence_pack: BrandEvidencePack,
    *,
    models: tuple[str, ...],
    repeat: int = 1,
    adjudicator_model: str = "same",
    reasoning_model: str = "same",
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
                    reasoning_model=model if reasoning_model == "same" else reasoning_model,
                    gate_authority=gate_authority,
                )
            )
    return {
        "schema_version": "sv9-score-model-bakeoff-v1",
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
    reasoning_model: str,
    gate_authority: str = "veto_only",
) -> dict[str, Any]:
    interpretation_llm = LLMAnalyzer(model=model)
    adjudicator_llm = LLMAnalyzer(model=adjudicator_model) if adjudicator_model else None
    shortlists = build_block_evidence_shortlists(evidence_pack)
    interpretation, debug = build_brand_interpretation_with_llm(
        evidence_pack,
        llm=interpretation_llm,
        adjudicator_llm=adjudicator_llm,
        block_evidence_shortlists=shortlists,
        gate_authority=gate_authority,
    )
    candidate = Sv9FlowCandidate(
        evidence_pack=evidence_pack,
        interpretation=interpretation,
        tile_signals=build_tile_signals_from_interpretation(interpretation),
        limitations=list(evidence_pack.limitations) + list(interpretation.limitations),
    )
    evaluator_llm = LLMAnalyzer(model=model)
    reasoning_llm = evaluator_llm if reasoning_model == model else LLMAnalyzer(model=reasoning_model)
    result = run_sv9_from_audit_snapshot(
        _snapshot_for_pack(evidence_pack),
        llm=evaluator_llm,
        reasoning_llm=reasoning_llm,
        sv9_flow_candidate=candidate,
    )
    result_payload = result.to_dict()
    return {
        "model": model,
        "adjudicator_model": adjudicator_model,
        "reasoning_model": reasoning_model,
        "repeat_index": repeat_index,
        "gate_authority": debug.get("gate_authority"),
        "interpretation_status": debug.get("status"),
        "detected_blocks": sorted(
            key
            for key, block in interpretation.blocks.items()
            if isinstance(block, dict) and block.get("detected") and str(block.get("content") or "").strip()
        ),
        "score": result_payload.get("brand3_score"),
        "base_average": result_payload.get("base_average"),
        "reliability_status": result_payload.get("reliability_status"),
        "not_detected": result_payload.get("not_detected") or [],
        "not_evaluated": result_payload.get("not_evaluated") or [],
        "components": summarize_components(result_payload.get("components") or {}),
        "debug": {
            "failed_blocks": debug.get("failed_blocks") or [],
            "block_failures": debug.get("block_failures") or [],
            "detection_provenance": debug.get("detection_provenance") or {},
            "gate_disagreements": debug.get("gate_disagreements") or [],
        },
        "usage": {
            "interpretation": usage_summary(interpretation_llm),
            "adjudicator": usage_summary(adjudicator_llm) if adjudicator_llm is not None else None,
            "evaluator": usage_summary(evaluator_llm),
            "reasoning": usage_summary(reasoning_llm) if reasoning_llm is not evaluator_llm else None,
        },
    }


def summarize_components(components: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, component in sorted(components.items()):
        if not isinstance(component, dict):
            continue
        out[key] = {
            "status": component.get("status"),
            "score": component.get("score"),
            "scale": component.get("scale"),
            "points": component.get("points"),
            "confidence": component.get("confidence"),
            "lit_tiles": component.get("lit_tiles") or [],
            "off_tiles": component.get("off_tiles") or [],
            "blind_spot_tiles": component.get("blind_spot_tiles") or [],
            "veredicto": component.get("veredicto") or "",
            "evaluation_model": component.get("evaluation_model") or "",
        }
    return out


def compare_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_model = {run["model"]: run for run in runs if run.get("repeat_index") == 1}
    if len(by_model) < 2:
        return {}
    models = sorted(by_model)
    left = by_model[models[0]]
    right = by_model[models[1]]
    components = sorted(set((left.get("components") or {})) | set((right.get("components") or {})))
    component_deltas = {}
    changed = []
    for key in components:
        left_component = (left.get("components") or {}).get(key) or {}
        right_component = (right.get("components") or {}).get(key) or {}
        delta = component_delta(left_component, right_component)
        component_deltas[key] = delta
        if delta["changed"]:
            changed.append(key)
    return {
        "baseline_model": left["model"],
        "candidate_model": right["model"],
        "score_delta": number_delta(left.get("score"), right.get("score")),
        "base_average_delta": number_delta(left.get("base_average"), right.get("base_average")),
        "detected_added": sorted(set(right.get("detected_blocks") or []) - set(left.get("detected_blocks") or [])),
        "detected_removed": sorted(set(left.get("detected_blocks") or []) - set(right.get("detected_blocks") or [])),
        "changed_components": changed,
        "components": component_deltas,
    }


def component_delta(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    lit_left = set(left.get("lit_tiles") or [])
    lit_right = set(right.get("lit_tiles") or [])
    off_left = set(left.get("off_tiles") or [])
    off_right = set(right.get("off_tiles") or [])
    blind_left = set(left.get("blind_spot_tiles") or [])
    blind_right = set(right.get("blind_spot_tiles") or [])
    changes = {
        "status_changed": left.get("status") != right.get("status"),
        "score_delta": number_delta(left.get("score"), right.get("score")),
        "lit_added": sorted(lit_right - lit_left),
        "lit_removed": sorted(lit_left - lit_right),
        "off_added": sorted(off_right - off_left),
        "off_removed": sorted(off_left - off_right),
        "blind_spot_added": sorted(blind_right - blind_left),
        "blind_spot_removed": sorted(blind_left - blind_right),
    }
    return {"changed": any(bool(value) for value in changes.values()), **changes}


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# SV9 score model bakeoff",
        "",
        f"- Marca: {payload['brand_name']}",
        f"- URL: {payload['url']}",
        f"- Evidencias: {payload['evidence']['record_count']} ({payload['evidence']['source_counts']})",
        f"- Modelos: {', '.join(payload['models'])}",
        f"- Gates: {payload.get('gate_authority') or 'n/a'}",
        "",
        "## Resumen",
        "",
        "| Modelo | Score | Base avg | Fiabilidad | Detectados | No detectados |",
        "| --- | ---: | ---: | --- | --- | --- |",
    ]
    for run in payload["runs"]:
        lines.append(
            "| {model} | {score} | {base} | {reliability} | {detected} | {missing} |".format(
                model=run["model"],
                score=run.get("score"),
                base=run.get("base_average"),
                reliability=run.get("reliability_status") or "-",
                detected=", ".join(run.get("detected_blocks") or []) or "-",
                missing=", ".join(run.get("not_detected") or []) or "-",
            )
        )
    comparison = payload.get("comparison") or {}
    if comparison:
        lines.extend(
            [
                "",
                "## Diferencias",
                "",
                f"- Delta score ({comparison.get('candidate_model')} - {comparison.get('baseline_model')}): {comparison.get('score_delta')}",
                f"- Añadidos por {comparison.get('candidate_model')}: {', '.join(comparison.get('detected_added') or []) or '-'}",
                f"- Perdidos por {comparison.get('candidate_model')}: {', '.join(comparison.get('detected_removed') or []) or '-'}",
                f"- Componentes cambiados: {', '.join(comparison.get('changed_components') or []) or '-'}",
            ]
        )
    for run in payload["runs"]:
        lines.extend(["", f"## {run['model']}", ""])
        lines.append("| Componente | Estado | Score | Puntos | Lit | Off | Blind |")
        lines.append("| --- | --- | ---: | ---: | --- | --- | --- |")
        for key, component in (run.get("components") or {}).items():
            lines.append(
                "| {key} | {status} | {score}/{scale} | {points} | {lit} | {off} | {blind} |".format(
                    key=key,
                    status=component.get("status") or "-",
                    score=component.get("score"),
                    scale=component.get("scale"),
                    points=component.get("points"),
                    lit=", ".join(component.get("lit_tiles") or []) or "-",
                    off=", ".join(component.get("off_tiles") or []) or "-",
                    blind=", ".join(component.get("blind_spot_tiles") or []) or "-",
                )
            )
    return "\n".join(lines)


def number_delta(left: Any, right: Any) -> float | int | None:
    if left is None or right is None:
        return None
    try:
        delta = float(right) - float(left)
    except (TypeError, ValueError):
        return None
    return int(delta) if delta.is_integer() else round(delta, 3)


def _snapshot_for_pack(evidence_pack: BrandEvidencePack) -> dict[str, Any]:
    return {
        "run": {
            "brand_name": evidence_pack.brand_name,
            "url": evidence_pack.url,
            "id": None,
        },
        "raw_inputs": [],
        "features": [],
    }


if __name__ == "__main__":
    raise SystemExit(main())
