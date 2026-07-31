"""Contract models for the parallel SV9 evidence-first flow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

BRAND_EVIDENCE_PACK_VERSION = "brand-evidence-pack-v1"
BRAND_INTERPRETATION_VERSION = "brand-interpretation-v1"
SV9_TILE_SIGNALS_VERSION = "sv9-tile-signals-v1"
SV9_FLOW_CANDIDATE_VERSION = "sv9-flow-candidate-v2"

EvidenceConfidence = Literal["low", "medium", "high"]
TileEffect = Literal[
    "supports",
    "weakens",
    "insufficient_evidence",
    "blocked",
    "capture_unreliable",
]


@dataclass(slots=True)
class EvidenceRecord:
    """One traceable observed fact, not an evaluation."""

    ref: str
    source: str
    evidence_type: str
    content: str
    url: str | None = None
    confidence: EvidenceConfidence = "medium"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BrandEvidencePack:
    """Raw and normalized evidence available to the candidate flow."""

    brand_name: str
    url: str
    evidence: list[EvidenceRecord] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    schema_version: str = BRAND_EVIDENCE_PACK_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = [record.to_dict() for record in self.evidence]
        return payload


@dataclass(slots=True)
class BrandInterpretation:
    """Structured reading derived from evidence, before SV9 scoring."""

    brand_name: str
    url: str
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_refs: dict[str, list[str]] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    schema_version: str = BRAND_INTERPRETATION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def interpretation_contract_violations(interpretation: BrandInterpretation) -> list[str]:
    """Return violations of the canonical contract: detected requires content and refs."""

    violations: list[str] = []
    for name, block in sorted(interpretation.blocks.items()):
        if not isinstance(block, dict) or block.get("detected") is not True:
            continue
        if not str(block.get("content") or "").strip():
            violations.append(f"{name}_detected_without_content")
        refs = [ref for ref in interpretation.evidence_refs.get(name) or [] if str(ref or "").strip()]
        if not refs:
            violations.append(f"{name}_detected_without_evidence_refs")
    return violations


@dataclass(slots=True)
class TileSignal:
    """One evidence-backed influence on one SV9 tile."""

    component: str
    tile: str
    effect: TileEffect
    confidence: EvidenceConfidence
    source: str
    evidence_refs: list[str] = field(default_factory=list)
    rationale: str = ""
    schema_version: str = SV9_TILE_SIGNALS_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Sv9FlowCandidate:
    """A complete shadow candidate; it is not a score."""

    evidence_pack: BrandEvidencePack
    interpretation: BrandInterpretation
    tile_signals: list[TileSignal] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    schema_version: str = SV9_FLOW_CANDIDATE_VERSION
    claim_memory_evidence: list[EvidenceRecord] = field(default_factory=list)
    evaluation_evidence_refs: dict[str, list[str]] = field(default_factory=dict)
    evaluation_evidence_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_pack": self.evidence_pack.to_dict(),
            "interpretation": self.interpretation.to_dict(),
            "tile_signals": [signal.to_dict() for signal in self.tile_signals],
            "claim_memory_evidence": [
                record.to_dict() for record in self.claim_memory_evidence
            ],
            "evaluation_evidence_refs": {
                block: list(refs)
                for block, refs in sorted(
                    self.evaluation_evidence_refs.items()
                )
            },
            "evaluation_evidence_version": self.evaluation_evidence_version,
            "limitations": list(self.limitations),
        }
