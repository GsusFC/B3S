"""SV9 result models (baldosas v3.1).

One record per component (not a monolithic blob) so individual components can
be retried and re-evaluated from the persisted snapshot.

The unit of judgement is the tile (baldosa): an independent key characteristic
with three possible states — `ok` (lit), `no` (off: a brand failure) and
`sin_evidencia` (blind spot: a limit of the snapshot, never a brand failure).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.sv9.rubric import (
    COMPONENTS,
    ESTADO_NO,
    ESTADO_OK,
    ESTADO_SIN_EVIDENCIA,
    MODEL_LABEL,
    RUBRIC_VERSION,
    STATUS_NOT_DETECTED,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    TILE_ESTADOS,
    component_points,
    confidence_from_blind_spots,
)


@dataclass
class TileVerdict:
    """One verdict per tile, with the evidence/motive contract of v3.1.

    - `ok` requires a verbatim `evidencia` quote from the snapshot.
    - `no` and `sin_evidencia` require a `motivo`.
    - `sin_evidencia` may carry `contexto_requerido`: what the user could add to
      light up the blind spot (the FLOC* conversation hook).
    """

    tile_id: str
    estado: str  # ESTADO_OK | ESTADO_NO | ESTADO_SIN_EVIDENCIA
    evidencia: str = ""
    motivo: str = ""
    contexto_requerido: str = ""

    @property
    def is_ok(self) -> bool:
        return self.estado == ESTADO_OK

    @property
    def is_blind_spot(self) -> bool:
        return self.estado == ESTADO_SIN_EVIDENCIA

    @property
    def is_off(self) -> bool:
        return self.estado == ESTADO_NO

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.tile_id,
            "estado": self.estado,
            "evidencia": self.evidencia,
            "motivo": self.motivo,
            "contexto_requerido": self.contexto_requerido,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TileVerdict":
        estado = str(payload.get("estado") or "").strip()
        if estado not in TILE_ESTADOS:
            estado = ESTADO_NO
        return cls(
            tile_id=str(payload.get("id") or payload.get("tile_id") or "").strip(),
            estado=estado,
            evidencia=str(payload.get("evidencia") or "").strip(),
            motivo=str(payload.get("motivo") or "").strip(),
            contexto_requerido=str(payload.get("contexto_requerido") or "").strip(),
        )


@dataclass
class ComponentResult:
    """Evaluation outcome for one SV9 component."""

    component: str
    status: str  # STATUS_SCORED | STATUS_NOT_DETECTED | STATUS_NOT_EVALUATED
    score: int = 0
    tile_profile: list[TileVerdict] = field(default_factory=list)
    veredicto: str = ""  # synthesis sentence (mandatory for Coherencia)
    evaluation_model: str | None = None  # which model tier judged this component
    detected_content: str | None = None
    detection_mode: str | None = None
    detection_confidence: str | None = None
    detection_limitations: list[str] = field(default_factory=list)
    evidence_source_summary: dict[str, int] = field(default_factory=dict)
    source_policy_notes: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    error: str | None = None  # populated for STATUS_NOT_EVALUATED

    @property
    def scale(self) -> int:
        return COMPONENTS[self.component]["scale"]

    @property
    def points(self) -> int:
        """Points contributed to the Brand3 Score (multiplier applied)."""
        if self.status != STATUS_SCORED:
            return 0
        return component_points(self.component, self.score)

    @property
    def blind_spot_count(self) -> int:
        return sum(1 for v in self.tile_profile if v.estado == ESTADO_SIN_EVIDENCIA)

    @property
    def confidence(self) -> str | None:
        """Confidence index derived from the count of `sin_evidencia` tiles.

        Only scored components have a confidence; not-detected and not-evaluated
        components return None so the UI/export never report a misleading "alta"
        over an empty tile profile.
        """
        if self.status != STATUS_SCORED:
            return None
        return confidence_from_blind_spots(self.blind_spot_count)

    @property
    def lit_tiles(self) -> list[str]:
        return [v.tile_id for v in self.tile_profile if v.estado == ESTADO_OK]

    @property
    def off_tiles(self) -> list[str]:
        """Tiles the snapshot proves are not met: the work plan."""
        return [v.tile_id for v in self.tile_profile if v.estado == ESTADO_NO]

    @property
    def blind_spot_tiles(self) -> list[str]:
        """Tiles the snapshot structurally cannot prove: the context to gather."""
        return [v.tile_id for v in self.tile_profile if v.estado == ESTADO_SIN_EVIDENCIA]

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status,
            "score": self.score,
            "scale": self.scale,
            "points": self.points,
            "confidence": self.confidence,
            "blind_spot_count": self.blind_spot_count,
            "veredicto": self.veredicto,
            "evaluation_model": self.evaluation_model,
            "tile_profile": [v.to_dict() for v in self.tile_profile],
            "lit_tiles": self.lit_tiles,
            "off_tiles": self.off_tiles,
            "blind_spot_tiles": self.blind_spot_tiles,
            "detected_content": self.detected_content,
            "detection_mode": self.detection_mode,
            "detection_confidence": self.detection_confidence,
            "detection_limitations": list(self.detection_limitations),
            "evidence_source_summary": dict(self.evidence_source_summary),
            "source_policy_notes": list(self.source_policy_notes),
            "evidence": list(self.evidence),
            "error": self.error,
        }


@dataclass
class Sv9ScanResult:
    """Aggregated SV9 scan: 9 components + Coherencia + Brand3 Score."""

    brand_name: str
    url: str
    source_run_id: int | None
    components: dict[str, ComponentResult]
    brand3_score: int = 0
    base_average: float | None = None
    magnetism_capped: bool = False
    immediate_margin: int = 0
    most_painful_gap: str | None = None
    needs_review: bool = False
    rubric_version: str = RUBRIC_VERSION
    model: str = MODEL_LABEL
    evaluator_model: str | None = None

    @property
    def is_complete(self) -> bool:
        """Complete scans have no technical failures; only these enter the ranking."""
        return all(c.status != STATUS_NOT_EVALUATED for c in self.components.values())

    @property
    def not_detected(self) -> list[str]:
        return [k for k, c in self.components.items() if c.status == STATUS_NOT_DETECTED]

    @property
    def not_evaluated(self) -> list[str]:
        return [k for k, c in self.components.items() if c.status == STATUS_NOT_EVALUATED]

    @property
    def total_blind_spots(self) -> int:
        return sum(c.blind_spot_count for c in self.components.values())

    @property
    def reliability_status(self) -> str:
        """Product-facing reliability gate for the scan as a whole."""
        if not self.is_complete:
            return "broken"
        if self.needs_review:
            return "shadow"
        if self.not_detected:
            return "shadow"
        if self.total_blind_spots == 0:
            return "reliable"
        if self.total_blind_spots <= 2:
            return "usable"
        return "shadow"

    @property
    def reliability_reason_codes(self) -> list[str]:
        reasons: list[str] = []
        if not self.is_complete:
            reasons.append("scan_not_complete")
        if self.needs_review:
            reasons.append("coherencia_needs_review")
        if self.not_detected:
            reasons.append("components_not_detected")
        if self.total_blind_spots > 2:
            reasons.append("blind_spots_above_usable_threshold")
        elif self.total_blind_spots > 0:
            reasons.append("blind_spots_present")
        return reasons

    @property
    def is_reliable(self) -> bool:
        return self.reliability_status == "reliable"

    @property
    def canonical_status(self) -> str:
        """Product-facing canonicity gate for downstream reuse."""
        if not self.is_complete:
            return "invalid"
        if self.reliability_status == "reliable" and not self.needs_review:
            return "canonical"
        if self.reliability_status in {"usable", "shadow"}:
            return "non_canonical"
        return "invalid"

    @property
    def canonical_reason_codes(self) -> list[str]:
        if self.canonical_status == "canonical":
            return []
        reasons = list(self.reliability_reason_codes)
        if not self.is_complete:
            reasons.append("scan_not_complete")
        elif self.needs_review:
            reasons.append("needs_review")
        elif self.reliability_status == "usable":
            reasons.append("usable_not_canonical")
        elif self.reliability_status == "shadow":
            reasons.append("shadow_not_canonical")
        else:
            reasons.append("invalid_scan_state")
        return list(dict.fromkeys(reasons))

    @property
    def is_canonical(self) -> bool:
        return self.canonical_status == "canonical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand_name": self.brand_name,
            "url": self.url,
            "source_run_id": self.source_run_id,
            "rubric_version": self.rubric_version,
            "model": self.model,
            "evaluator_model": self.evaluator_model,
            "brand3_score": self.brand3_score,
            "base_average": self.base_average,
            "magnetism_capped": self.magnetism_capped,
            "immediate_margin": self.immediate_margin,
            "most_painful_gap": self.most_painful_gap,
            "needs_review": self.needs_review,
            "is_complete": self.is_complete,
            "total_blind_spots": self.total_blind_spots,
            "reliability_status": self.reliability_status,
            "reliability_reason_codes": self.reliability_reason_codes,
            "is_reliable": self.is_reliable,
            "canonical_status": self.canonical_status,
            "canonical_reason_codes": self.canonical_reason_codes,
            "is_canonical": self.is_canonical,
            "not_detected": self.not_detected,
            "not_evaluated": self.not_evaluated,
            "components": {k: c.to_dict() for k, c in self.components.items()},
        }


__all__ = [
    "ComponentResult",
    "TileVerdict",
    "Sv9ScanResult",
    "STATUS_SCORED",
    "STATUS_NOT_DETECTED",
    "STATUS_NOT_EVALUATED",
    "ESTADO_OK",
    "ESTADO_NO",
    "ESTADO_SIN_EVIDENCIA",
]
