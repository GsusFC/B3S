"""Canonical orchestrator for the SV9 Flow candidate.

The canonical path is evidence_pack -> flow-llm interpretation -> tile
signals. Pass 1/TLDR compatibility wrappers are not part of this package;
they live in scripts/sv9_flow_legacy_compat.py while legacy baselines are
being retired.
"""

from __future__ import annotations

from typing import Any

from src.sv9_flow._utils import unique_strings
from src.sv9_flow.block_evidence_worker import build_block_evidence_shortlists
from src.sv9_flow.contracts import Sv9FlowCandidate, interpretation_contract_violations
from src.sv9_flow.evidence_coverage import acquisition_coverage, block_coverage, coverage_limitations
from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot
from src.sv9_flow.interpretation_llm_worker import build_brand_interpretation_with_llm
from src.sv9_flow.tile_signal_worker import build_tile_signals_from_interpretation


def build_flow_candidate(
    *,
    snapshot: dict[str, Any],
    llm: Any,
    adjudicator_llm: Any | None = None,
    visual_signature_evidence: dict[str, Any] | None = None,
    gate_authority: str | None = None,
) -> tuple[Sv9FlowCandidate, dict[str, Any]]:
    """Build the canonical flow candidate for one audit snapshot.

    Returns the candidate plus the interpretation debug payload (prompt and
    gate provenance, shortlists); the debug payload is for harness comparison
    only and is not a score.
    """

    evidence_pack = build_evidence_pack_from_snapshot(
        snapshot,
        visual_signature_evidence=visual_signature_evidence,
    )
    shortlists = build_block_evidence_shortlists(evidence_pack)
    if gate_authority is None:
        from src.config import SV9_FLOW_GATE_AUTHORITY

        gate_authority = SV9_FLOW_GATE_AUTHORITY
    interpretation, debug = build_brand_interpretation_with_llm(
        evidence_pack,
        llm=llm,
        adjudicator_llm=adjudicator_llm,
        block_evidence_shortlists=shortlists,
        gate_authority=gate_authority,
    )
    debug["block_evidence_shortlists"] = shortlists
    coverage = {
        "acquisition": acquisition_coverage(evidence_pack),
        "blocks": block_coverage(evidence_pack, interpretation),
    }
    debug["evidence_coverage"] = coverage
    tile_signals = build_tile_signals_from_interpretation(
        interpretation,
        visual_signature_evidence=visual_signature_evidence,
    )
    # The normalizer guarantees detected=>content+refs; a violation here means
    # a worker bug, so surface it instead of hiding it.
    contract_violations = [
        f"contract_violation:{code}"
        for code in interpretation_contract_violations(interpretation)
    ]
    coverage_findings = coverage_limitations(coverage["blocks"])
    candidate = Sv9FlowCandidate(
        evidence_pack=evidence_pack,
        interpretation=interpretation,
        tile_signals=tile_signals,
        limitations=unique_strings(
            list(evidence_pack.limitations)
            + list(interpretation.limitations)
            + contract_violations
            + coverage_findings
        ),
    )
    return candidate, debug
