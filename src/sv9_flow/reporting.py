"""Reporting helpers for the parallel SV9 flow."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from src.sv9_flow.contracts import Sv9FlowCandidate

SV9_FLOW_REPORT_VERSION = "sv9-flow-report-v1"


def build_flow_report(candidate: Sv9FlowCandidate) -> dict[str, Any]:
    """Return a compact quality/control report for one flow candidate."""

    signals = candidate.tile_signals
    effects = Counter(signal.effect for signal in signals)
    by_component: dict[str, Counter[str]] = defaultdict(Counter)
    for signal in signals:
        by_component[signal.component][signal.effect] += 1

    return {
        "schema_version": SV9_FLOW_REPORT_VERSION,
        "candidate_schema_version": candidate.schema_version,
        "brand_name": candidate.evidence_pack.brand_name,
        "url": candidate.evidence_pack.url,
        "counts": {
            "evidence_records": len(candidate.evidence_pack.evidence),
            "claim_memory_evidence_records": len(
                candidate.claim_memory_evidence
            ),
            "interpretation_blocks": len(candidate.interpretation.blocks),
            "tile_signals": len(signals),
            "limitations": len(candidate.limitations),
        },
        "tile_signal_effects": dict(sorted(effects.items())),
        "tile_signal_effects_by_component": {
            component: dict(sorted(component_effects.items()))
            for component, component_effects in sorted(by_component.items())
        },
        "limitations": list(candidate.limitations),
    }
