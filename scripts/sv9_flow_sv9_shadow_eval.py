#!/usr/bin/env python3
"""Evaluate current SV9 from an SV9 Flow interpretation over a fixed snapshot."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sv9_flow_shadow_run import _load_env_file
from scripts.sv9_flow_snapshot_eval import snapshot_and_run_id_from_envelope
from src.config import (
    SV9_ADJUDICATOR_MODEL,
    SV9_FLOW_GATE_AUTHORITY,
    SV9_FLOW_LABELING_MODEL,
    SV9_FLOW_MODEL,
    SV9_REASONING_MODEL,
)
from src.features.llm_analyzer import LLMAnalyzer
from src.sv9.flow_ingress import (
    detection_blocks_from_flow_candidate,
    flow_candidate_extra_signals,
)
from src.sv9.service import run_sv9_from_audit_snapshot
from src.sv9_flow.contracts import Sv9FlowCandidate
from src.sv9_flow.orchestrator import build_flow_candidate

SV9_FLOW_SV9_SHADOW_EVAL_VERSION = "sv9-flow-sv9-shadow-eval-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot_json")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--disable-llm-cache", action="store_true")
    parser.add_argument(
        "--compare-legacy",
        action="store_true",
        help="Also run the current legacy SV9 chain and emit tile/score deltas.",
    )
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--output", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.env_file:
        _load_env_file(args.env_file)
    if args.disable_llm_cache:
        os.environ["BRAND3_LLM_CACHE_ENABLED"] = "false"

    envelope = json.loads(Path(args.snapshot_json).read_text(encoding="utf-8"))
    report = build_flow_sv9_shadow_eval(
        envelope,
        include_full=args.full,
        compare_legacy=args.compare_legacy,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


def build_flow_sv9_shadow_eval(
    envelope: dict[str, Any],
    *,
    include_full: bool = False,
    compare_legacy: bool = False,
    interpretation_llm: Any | None = None,
    adjudicator_llm: Any | None = None,
    labeling_llm: Any | None = None,
    evaluator_llm: Any | None = None,
    reasoning_llm: Any | None = None,
    gate_authority: str | None = None,
    sv9_runner: Callable[..., Any] = run_sv9_from_audit_snapshot,
    visual_evidence_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    snapshot, run_id = snapshot_and_run_id_from_envelope(envelope)
    visual_evidence_fn = visual_evidence_fn or _visual_evidence_packet_from_snapshot
    visual_evidence_packet = visual_evidence_fn(snapshot)
    interpretation_llm = interpretation_llm or _default_interpretation_llm()
    adjudicator_llm = adjudicator_llm or _default_adjudicator_llm()
    labeling_llm = labeling_llm or _default_labeling_llm()
    candidate, debug = build_flow_candidate(
        snapshot=snapshot,
        llm=interpretation_llm,
        adjudicator_llm=adjudicator_llm,
        labeling_llm=labeling_llm,
        visual_signature_evidence=visual_evidence_packet,
        gate_authority=gate_authority or SV9_FLOW_GATE_AUTHORITY,
    )
    evaluator_llm = evaluator_llm or _default_evaluator_llm()
    reasoning_llm = reasoning_llm or _default_reasoning_llm()
    result = sv9_runner(
        snapshot,
        llm=evaluator_llm,
        reasoning_llm=reasoning_llm,
        sv9_flow_candidate=candidate,
        source_run_id=run_id,
    )

    payload: dict[str, Any] = {
        "schema_version": SV9_FLOW_SV9_SHADOW_EVAL_VERSION,
        "source_run_id": run_id,
        "brand_name": result.brand_name,
        "url": result.url,
        "visual_acquisition_present": visual_evidence_packet is not None,
        "flow": {
            "detected_blocks": _detected_blocks(candidate),
            "limitations": candidate.limitations,
            "visual_acquisition_present": visual_evidence_packet is not None,
            "visual_acquisition_schema_version": (
                visual_evidence_packet.get("schema_version")
                if isinstance(visual_evidence_packet, dict)
                else None
            ),
            "interpretation_debug": _compact_interpretation_debug(debug),
        },
        "sv9": _result_summary(result.to_dict()),
    }
    if compare_legacy:
        legacy_result = sv9_runner(
            snapshot,
            llm=evaluator_llm,
            reasoning_llm=reasoning_llm,
            source_run_id=run_id,
        )
        legacy_summary = _result_summary(legacy_result.to_dict())
        payload["legacy_sv9"] = legacy_summary
        payload["comparison"] = compare_sv9_summaries(
            flow_summary=payload["sv9"],
            legacy_summary=legacy_summary,
        )
    if include_full:
        payload["flow"]["candidate"] = candidate.to_dict()
        payload["flow"]["detection_blocks"] = detection_blocks_from_flow_candidate(candidate)
        payload["flow"]["extra_signals"] = flow_candidate_extra_signals(candidate)
        payload["sv9"]["result"] = result.to_dict()
        if compare_legacy:
            payload["legacy_sv9"]["result"] = legacy_result.to_dict()
    payload["llm_usage"] = _llm_usage_payload(
        interpretation_llm=interpretation_llm,
        labeling_llm=labeling_llm,
        evaluator_llm=evaluator_llm,
        reasoning_llm=reasoning_llm,
    )
    return payload


def _default_interpretation_llm() -> LLMAnalyzer:
    """Interpretation tier; override per-role for asymmetric model configs."""

    return LLMAnalyzer(model=os.environ.get("BRAND3_FLOW_INTERPRETATION_MODEL") or SV9_FLOW_MODEL)


def _default_evaluator_llm() -> LLMAnalyzer:
    """Tile evaluator tier; the score-sensitive role."""

    return LLMAnalyzer(model=os.environ.get("BRAND3_FLOW_EVALUATOR_MODEL") or SV9_FLOW_MODEL)


def _default_reasoning_llm() -> LLMAnalyzer:
    """Reasoning tier; instantiate here so failures survive in scan telemetry."""

    return LLMAnalyzer(model=SV9_REASONING_MODEL)


def _default_adjudicator_llm() -> LLMAnalyzer:
    """Cheap quote-grounded rescue pass for deterministic gate false negatives."""

    return LLMAnalyzer(model=os.environ.get("BRAND3_FLOW_ADJUDICATOR_MODEL") or SV9_ADJUDICATOR_MODEL)


def _default_labeling_llm() -> LLMAnalyzer:
    """Cheap semantic evidence labeling pass for shortlist recall."""

    return LLMAnalyzer(model=os.environ.get("BRAND3_FLOW_LABELING_MODEL") or SV9_FLOW_LABELING_MODEL)


def _visual_evidence_packet_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    from src.sv9.signals import visual_evidence_packet_from_snapshot

    return visual_evidence_packet_from_snapshot(snapshot)


def _detected_blocks(candidate: Sv9FlowCandidate) -> list[str]:
    return [
        key
        for key, block in sorted(candidate.interpretation.blocks.items())
        if isinstance(block, dict) and block.get("detected") and block.get("content")
    ]


def _compact_interpretation_debug(debug: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in debug.items()
        if key
        in {
            "status",
            "prompt_version",
            "shortlist_version",
            "gate_authority",
            "mode",
            "detected_count",
            "failed_blocks",
            "block_failures",
            "block_detection_decisions",
            "gate_disagreements",
            "detection_provenance",
            "evidence_labeling",
            "block_evidence_identity_gate",
            "evidence_coverage",
            "failure_reason",
            "failure",
        }
    }


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    components = result.get("components") if isinstance(result.get("components"), dict) else {}
    assessment = (
        copy.deepcopy(result["assessment"])
        if isinstance(result.get("assessment"), dict)
        else None
    )
    return {
        "brand3_score": result.get("brand3_score"),
        "base_average": result.get("base_average"),
        "magnetism_capped": result.get("magnetism_capped"),
        "assessment": assessment,
        "assessment_fingerprint": result.get("assessment_fingerprint"),
        "score_fingerprint": result.get("score_fingerprint"),
        "reliability_status": result.get("reliability_status"),
        "not_detected": result.get("not_detected") or [],
        "not_evaluated": result.get("not_evaluated") or [],
        "components": {
            key: {
                "status": value.get("status"),
                "score": value.get("score"),
                "points": value.get("points"),
                "scale": value.get("scale"),
                "lit_tiles": value.get("lit_tiles") or [],
                "off_tiles": value.get("off_tiles") or [],
                "blind_spot_tiles": value.get("blind_spot_tiles") or [],
                "blind_spot_count": value.get("blind_spot_count"),
                "tile_profile": copy.deepcopy(value.get("tile_profile") or []),
            }
            for key, value in sorted(components.items())
            if isinstance(value, dict)
        },
    }


def _llm_usage_payload(
    *,
    interpretation_llm: Any,
    labeling_llm: Any,
    evaluator_llm: Any,
    reasoning_llm: Any | None,
) -> dict[str, Any]:
    roles = {
        "flow_labeling": labeling_llm,
        "flow_interpretation": interpretation_llm,
        "sv9_evaluator": evaluator_llm,
    }
    if reasoning_llm is not None and reasoning_llm is not evaluator_llm:
        roles["sv9_reasoning"] = reasoning_llm
    summaries = {role: _llm_usage_summary(llm) for role, llm in roles.items()}
    return {
        "schema_version": f"{SV9_FLOW_SV9_SHADOW_EVAL_VERSION}.llm_usage",
        "roles": summaries,
        "totals": {
            "cache_hits": sum(item.get("cache_hits", 0) for item in summaries.values()),
            "cache_misses": sum(item.get("cache_misses", 0) for item in summaries.values()),
            "cache_writes": sum(item.get("cache_writes", 0) for item in summaries.values()),
            "provider_calls": sum(item.get("provider_calls", 0) for item in summaries.values()),
            "usage_metadata_available": any(
                bool(item.get("usage_metadata_available")) for item in summaries.values()
            ),
        },
    }


def _llm_usage_summary(llm: Any) -> dict[str, Any]:
    summary_fn = getattr(llm, "usage_observation_summary", None)
    if callable(summary_fn):
        summary = summary_fn()
        summary["call_failures"] = list(getattr(llm, "call_failures", []) or [])
        return summary
    observations = getattr(llm, "usage_observations", [])
    if not isinstance(observations, list):
        observations = []
    return {
        "model": getattr(llm, "model", ""),
        "base_url": getattr(llm, "base_url", ""),
        "cache_hits": int(getattr(llm, "cache_hits", 0) or 0),
        "cache_misses": int(getattr(llm, "cache_misses", 0) or 0),
        "cache_writes": int(getattr(llm, "cache_writes", 0) or 0),
        "provider_calls": sum(1 for item in observations if item.get("event") == "provider_call"),
        "usage_metadata_available": any(
            bool(item.get("usage_metadata_available")) for item in observations
        ),
        "observations": observations,
        "call_failures": list(getattr(llm, "call_failures", []) or []),
    }


def compare_sv9_summaries(
    *,
    flow_summary: dict[str, Any],
    legacy_summary: dict[str, Any],
) -> dict[str, Any]:
    """Compare two compact SV9 summaries without assigning product authority."""

    flow_components = _components(flow_summary)
    legacy_components = _components(legacy_summary)
    component_keys = sorted(set(flow_components) | set(legacy_components))
    component_deltas = {
        key: _component_delta(flow_components.get(key) or {}, legacy_components.get(key) or {})
        for key in component_keys
    }
    changed_components = [
        key
        for key, delta in component_deltas.items()
        if delta["status_changed"] or delta["score_delta"] != 0 or delta["tile_changes"]["changed"]
    ]
    return {
        "brand3_score_delta": _number_delta(
            flow_summary.get("brand3_score"),
            legacy_summary.get("brand3_score"),
        ),
        "base_average_delta": _number_delta(
            flow_summary.get("base_average"),
            legacy_summary.get("base_average"),
        ),
        "reliability_changed": flow_summary.get("reliability_status")
        != legacy_summary.get("reliability_status"),
        "not_detected_added": sorted(
            set(flow_summary.get("not_detected") or []) - set(legacy_summary.get("not_detected") or [])
        ),
        "not_detected_removed": sorted(
            set(legacy_summary.get("not_detected") or []) - set(flow_summary.get("not_detected") or [])
        ),
        "changed_components": changed_components,
        "components": component_deltas,
    }


def _components(summary: dict[str, Any]) -> dict[str, Any]:
    components = summary.get("components")
    return components if isinstance(components, dict) else {}


def _component_delta(flow_component: dict[str, Any], legacy_component: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": {
            "flow": flow_component.get("status"),
            "legacy": legacy_component.get("status"),
        },
        "status_changed": flow_component.get("status") != legacy_component.get("status"),
        "score_delta": _number_delta(flow_component.get("score"), legacy_component.get("score")),
        "tile_changes": _tile_changes(flow_component, legacy_component),
    }


def _tile_changes(flow_component: dict[str, Any], legacy_component: dict[str, Any]) -> dict[str, Any]:
    flow_lit = set(flow_component.get("lit_tiles") or [])
    legacy_lit = set(legacy_component.get("lit_tiles") or [])
    flow_off = set(flow_component.get("off_tiles") or [])
    legacy_off = set(legacy_component.get("off_tiles") or [])
    flow_blind = set(flow_component.get("blind_spot_tiles") or [])
    legacy_blind = set(legacy_component.get("blind_spot_tiles") or [])
    changes = {
        "lit_added": sorted(flow_lit - legacy_lit),
        "lit_removed": sorted(legacy_lit - flow_lit),
        "off_added": sorted(flow_off - legacy_off),
        "off_removed": sorted(legacy_off - flow_off),
        "blind_spot_added": sorted(flow_blind - legacy_blind),
        "blind_spot_removed": sorted(legacy_blind - flow_blind),
    }
    changed = any(changes[key] for key in changes)
    return {"changed": changed, **changes}


def _number_delta(flow_value: Any, legacy_value: Any) -> float | int | None:
    if flow_value is None or legacy_value is None:
        return None
    try:
        delta = float(flow_value) - float(legacy_value)
    except (TypeError, ValueError):
        return None
    return int(delta) if delta.is_integer() else round(delta, 3)


if __name__ == "__main__":
    raise SystemExit(main())
