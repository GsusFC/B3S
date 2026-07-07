"""Deterministic source-authority policy for SV9 scores.

The evaluator decides tile states from cited evidence. This module applies the
second contract: which sources are authoritative enough to carry each strategic
component. External proof may validate traction or a product claim, but it must
not fully create owned brand expression such as mission, values, purpose, voice
or vision.
"""

from __future__ import annotations

from src.sv9.models import ComponentResult, ESTADO_OK, ESTADO_SIN_EVIDENCIA, STATUS_SCORED, TileVerdict
from src.sv9.rubric import COMPONENTS

SOURCE_POLICY_VERSION = "sv9-source-authority-v1"

_OWNED_EXPRESSION_CAPS = {
    "mission": 3,
    "vision": 3,
    "values": 3,
    "core_purpose": 5,
    "personality": 4,
    "brand_idea": 6,
}

_IMPLIED_EXPRESSION_CAPS = {
    "mission": 3,
    "vision": 3,
    "values": 3,
    "core_purpose": 6,
    "personality": 5,
    "brand_idea": 7,
}

_EXTERNAL_ONLY_PROOF_CAPS = {
    "value_proposition": 8,
    "attributes": 4,
    "magnetism": 7,
}


def apply_source_policy(components: dict[str, ComponentResult]) -> bool:
    """Apply source-authority caps in place.

    Returns True when any component was changed. The policy is intentionally
    conservative: it only lowers scores when the evidence authority is weaker
    than the component requires.
    """

    changed = False
    capped_components: list[str] = []
    for key, component in components.items():
        if key == "coherencia":
            continue
        cap, reason = _cap_for_component(key, component)
        if cap is None:
            continue
        if _cap_component(component, cap, reason):
            changed = True
            capped_components.append(key)

    if capped_components:
        coherence_cap = 6 if len(capped_components) < 3 else 5
        reason = "source_policy:coherencia_capped_after_component_authority_caps"
        if _cap_component(components["coherencia"], coherence_cap, reason):
            changed = True
    return changed


def _cap_for_component(key: str, component: ComponentResult) -> tuple[int | None, str]:
    if component.status != STATUS_SCORED:
        return None, ""
    summary = component.evidence_source_summary or {}
    limitations = [item.lower() for item in component.detection_limitations or []]
    owned = int(summary.get("owned_copy") or 0)
    external = int(summary.get("external_proof") or 0)
    visual = int(summary.get("visual_signal") or 0)
    derived = int(summary.get("derived_strategy") or 0)
    total = int(summary.get("total") or 0)

    if key in _OWNED_EXPRESSION_CAPS:
        if _has_coverage_limitation(limitations, key, "verified_absent"):
            return _OWNED_EXPRESSION_CAPS[key], f"source_policy:{key}_owned_expression_verified_absent"
        if _has_coverage_limitation(limitations, key, "implied_not_explicit"):
            return _IMPLIED_EXPRESSION_CAPS[key], f"source_policy:{key}_implied_not_explicit"
        if total and owned == 0 and visual == 0:
            return _OWNED_EXPRESSION_CAPS[key], f"source_policy:{key}_requires_owned_expression"
        if external > owned + visual and key != "brand_idea":
            return _IMPLIED_EXPRESSION_CAPS[key], f"source_policy:{key}_external_proof_dominates_owned_expression"
        if derived and owned == 0:
            return _OWNED_EXPRESSION_CAPS[key], f"source_policy:{key}_derived_strategy_is_not_primary_evidence"

    if key in _EXTERNAL_ONLY_PROOF_CAPS and total and owned == 0:
        return _EXTERNAL_ONLY_PROOF_CAPS[key], f"source_policy:{key}_external_proof_without_owned_surface"

    return None, ""


def _has_coverage_limitation(limitations: list[str], key: str, status: str) -> bool:
    needle = f"coverage:{key}_{status}"
    return any(needle in item for item in limitations)


def _cap_component(component: ComponentResult, cap: int, reason: str) -> bool:
    if component.status != STATUS_SCORED:
        return False
    scale = int(COMPONENTS[component.component]["scale"])
    cap = max(0, min(cap, scale))
    if component.score <= cap:
        return False

    demoted = component.score - cap
    component.tile_profile = _demote_lit_tiles(component.tile_profile, demoted, reason)
    component.score = sum(1 for verdict in component.tile_profile if verdict.estado == ESTADO_OK)
    if reason not in component.source_policy_notes:
        component.source_policy_notes.append(reason)
    return True


def _demote_lit_tiles(
    tile_profile: list[TileVerdict],
    demoted_count: int,
    reason: str,
) -> list[TileVerdict]:
    remaining = demoted_count
    normalized: list[TileVerdict] = []
    for verdict in reversed(tile_profile):
        if remaining > 0 and verdict.estado == ESTADO_OK:
            normalized.append(
                TileVerdict(
                    tile_id=verdict.tile_id,
                    estado=ESTADO_SIN_EVIDENCIA,
                    motivo=f"{SOURCE_POLICY_VERSION}: la fuente citada no tiene autoridad suficiente para esta baldosa ({reason}).",
                    contexto_requerido="Aporta evidencia propia o de primera mano para validar esta baldosa.",
                )
            )
            remaining -= 1
            continue
        normalized.append(verdict)
    normalized.reverse()
    return normalized
