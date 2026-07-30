"""Canonical orchestrator for the SV9 Flow candidate.

The canonical path is evidence_pack -> flow-llm interpretation -> tile
signals. Pass 1/TLDR compatibility wrappers are not part of this package;
they live in scripts/sv9_flow_legacy_compat.py while legacy baselines are
being retired.
"""

from __future__ import annotations

from typing import Any

from src.sv9_flow._utils import unique_strings
from src.sv9_flow.block_evidence_worker import (
    BLOCK_EVIDENCE_IDENTITY_GATE_VERSION,
    EVALUATION_EVIDENCE_REFS_VERSION,
    build_block_evidence_identity_quarantine,
    build_block_evidence_shortlists,
    build_evaluation_evidence_refs,
)
from src.sv9_flow.claim_slot_producer import (
    build_claim_memory_evidence,
    claim_slot_producer_summary,
)
from src.sv9_flow.contracts import (
    SV9_FLOW_CANDIDATE_VERSION,
    Sv9FlowCandidate,
    interpretation_contract_violations,
)
from src.sv9_flow.evidence_coverage import (
    acquisition_coverage,
    block_coverage,
    component_surface_hierarchy,
    coverage_limitations,
)
from src.sv9_flow.evidence_identity import (
    EVIDENCE_IDENTITY_VERSION,
    canonical_evidence_records,
    canonical_evidence_set_digest,
)
from src.sv9_flow.evidence_labeling_worker import label_evidence_pack
from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot
from src.sv9_flow.interpretation_llm_worker import build_brand_interpretation_with_llm
from src.sv9_flow.tile_signal_worker import build_tile_signals_from_interpretation


def build_flow_candidate(
    *,
    snapshot: dict[str, Any],
    llm: Any,
    adjudicator_llm: Any | None = None,
    labeling_llm: Any | None = None,
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
    labeling_debug = label_evidence_pack(evidence_pack, llm=labeling_llm)
    shortlists = build_block_evidence_shortlists(evidence_pack)
    identity_quarantine = build_block_evidence_identity_quarantine(
        evidence_pack
    )
    evaluation_evidence_refs = build_evaluation_evidence_refs(
        evidence_pack,
        shortlists,
    )
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
    debug["evidence_labeling"] = labeling_debug
    debug["block_evidence_shortlists"] = shortlists
    debug["block_evidence_identity_gate"] = {
        "version": BLOCK_EVIDENCE_IDENTITY_GATE_VERSION,
        "quarantined_count": len(identity_quarantine),
        "records": identity_quarantine,
    }
    debug["evidence_identity"] = {
        "version": EVIDENCE_IDENTITY_VERSION,
        "fingerprint": canonical_evidence_set_digest(evidence_pack),
        "record_count": len(canonical_evidence_records(evidence_pack.evidence)),
    }
    coverage = {
        "acquisition": acquisition_coverage(evidence_pack),
        "blocks": block_coverage(evidence_pack, interpretation),
        "component_hierarchy": component_surface_hierarchy(
            evidence_pack,
            interpretation,
        ),
    }
    debug["evidence_coverage"] = coverage
    tile_signals = build_tile_signals_from_interpretation(
        interpretation,
        visual_signature_evidence=visual_signature_evidence,
    )
    claim_memory_evidence = build_claim_memory_evidence(
        evidence_pack,
        source_candidate_schema_version=SV9_FLOW_CANDIDATE_VERSION,
    )
    debug["claim_slot_producer"] = claim_slot_producer_summary(
        claim_memory_evidence
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
        claim_memory_evidence=claim_memory_evidence,
        evaluation_evidence_refs=evaluation_evidence_refs,
        evaluation_evidence_version=EVALUATION_EVIDENCE_REFS_VERSION,
        limitations=unique_strings(
            list(evidence_pack.limitations)
            + list(interpretation.limitations)
            + contract_violations
            + coverage_findings
        ),
    )
    return candidate, debug
