"""Deterministic legacy source policy for SV9 scores.

The evaluator decides tile states from cited evidence. This module freezes the
existing demotion behavior while classifying it as assessment work that must be
replaced by tile-local evidence binding. It does not grant verification or
source-authority transition semantics.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from src.sv9.models import ComponentResult, ESTADO_OK, ESTADO_SIN_EVIDENCIA, STATUS_SCORED, TileVerdict
from src.sv9.rubric import COMPONENTS

SOURCE_POLICY_AXIS_CLASSIFICATION_VERSION = "sv9-source-policy-axis-classification-v1"
LEGACY_SOURCE_POLICY_PROJECTION_VERSION = "sv9-legacy-source-policy-projection-v1"
ASSESSMENT_TILE_REASSESSMENT_REQUIRED = "assessment_tile_reassessment_required"
LEGACY_COMPATIBILITY_ONLY = "legacy_compatibility_only"

# Kept in tile motives solely to preserve the current serialized output.
LEGACY_SOURCE_POLICY_VERSION = "sv9-source-authority-v1"
SOURCE_POLICY_VERSION = LEGACY_SOURCE_POLICY_VERSION

SOURCE_POLICY_REASON_CLASSIFICATIONS = MappingProxyType(
    {
        "source_policy:{component}_owned_expression_verified_absent": ASSESSMENT_TILE_REASSESSMENT_REQUIRED,
        "source_policy:{component}_implied_not_explicit": ASSESSMENT_TILE_REASSESSMENT_REQUIRED,
        "source_policy:{component}_requires_owned_expression": ASSESSMENT_TILE_REASSESSMENT_REQUIRED,
        "source_policy:{component}_external_proof_dominates_owned_expression": ASSESSMENT_TILE_REASSESSMENT_REQUIRED,
        "source_policy:{component}_derived_strategy_is_not_primary_evidence": ASSESSMENT_TILE_REASSESSMENT_REQUIRED,
        "source_policy:{component}_external_proof_without_owned_surface": ASSESSMENT_TILE_REASSESSMENT_REQUIRED,
        "source_policy:coherencia_capped_after_component_authority_caps": LEGACY_COMPATIBILITY_ONLY,
    }
)


@dataclass(frozen=True)
class SourcePolicyAction:
    component: str
    cap: int
    reason: str
    axis_classification: str


@dataclass(frozen=True)
class SourcePolicyPlan:
    axis_classification_version: str
    legacy_policy_version: str
    actions: tuple[SourcePolicyAction, ...]


@dataclass(frozen=True)
class LegacySourcePolicyProjection:
    """Detached result of applying the frozen legacy policy."""

    schema_version: str
    components: dict[str, ComponentResult]
    changed: bool


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


def _iter_source_policy_actions(
    components: Mapping[str, ComponentResult],
) -> Iterator[SourcePolicyAction]:
    for key, component in components.items():
        if key == "coherencia":
            continue
        cap, reason, reason_template = _cap_for_component(key, component)
        if cap is not None:
            yield SourcePolicyAction(
                component=key,
                cap=cap,
                reason=reason,
                axis_classification=SOURCE_POLICY_REASON_CLASSIFICATIONS[reason_template],
            )


def build_source_policy_plan(components: Mapping[str, ComponentResult]) -> SourcePolicyPlan:
    """Build an immutable description of matching legacy caps without mutating inputs."""

    return SourcePolicyPlan(
        axis_classification_version=SOURCE_POLICY_AXIS_CLASSIFICATION_VERSION,
        legacy_policy_version=LEGACY_SOURCE_POLICY_VERSION,
        actions=tuple(_iter_source_policy_actions(components)),
    )


def project_legacy_source_policy(
    components: Mapping[str, ComponentResult],
) -> LegacySourcePolicyProjection:
    """Apply the legacy policy to a detached component graph.

    The legacy policy deliberately remains mutable for compatibility callers.
    This projection is the typed pure boundary used by aggregation instead.
    """

    memo: dict[int, object] = {}
    projected_components = {key: deepcopy(component, memo) for key, component in components.items()}
    return LegacySourcePolicyProjection(
        schema_version=LEGACY_SOURCE_POLICY_PROJECTION_VERSION,
        components=projected_components,
        changed=apply_source_policy(projected_components),
    )


def apply_source_policy(components: Mapping[str, ComponentResult]) -> bool:
    """Apply the frozen legacy plan in place and report whether it changed."""

    changed = False
    capped_components = 0
    for action in _iter_source_policy_actions(components):
        if _cap_component(components[action.component], action.cap, action.reason):
            changed = True
            capped_components += 1

    if capped_components:
        reason = "source_policy:coherencia_capped_after_component_authority_caps"
        coherence_cap = 6 if capped_components < 3 else 5
        if _cap_component(components["coherencia"], coherence_cap, reason):
            changed = True
    return changed


def _cap_for_component(key: str, component: ComponentResult) -> tuple[int | None, str, str]:
    if component.status != STATUS_SCORED:
        return None, "", ""
    summary = component.evidence_source_summary or {}
    limitations = [item.lower() for item in component.detection_limitations or []]
    owned = int(summary.get("owned_copy") or 0)
    external = int(summary.get("external_proof") or 0)
    visual = int(summary.get("visual_signal") or 0)
    derived = int(summary.get("derived_strategy") or 0)
    total = int(summary.get("total") or 0)

    if key in _OWNED_EXPRESSION_CAPS:
        if _has_coverage_limitation(limitations, key, "verified_absent"):
            template = "source_policy:{component}_owned_expression_verified_absent"
            return _OWNED_EXPRESSION_CAPS[key], template.format(component=key), template
        if _has_coverage_limitation(limitations, key, "implied_not_explicit"):
            template = "source_policy:{component}_implied_not_explicit"
            return _IMPLIED_EXPRESSION_CAPS[key], template.format(component=key), template
        if total and owned == 0 and visual == 0:
            template = "source_policy:{component}_requires_owned_expression"
            return _OWNED_EXPRESSION_CAPS[key], template.format(component=key), template
        if external > owned + visual and key != "brand_idea":
            template = "source_policy:{component}_external_proof_dominates_owned_expression"
            return _IMPLIED_EXPRESSION_CAPS[key], template.format(component=key), template
        if derived and owned == 0:
            template = "source_policy:{component}_derived_strategy_is_not_primary_evidence"
            return _OWNED_EXPRESSION_CAPS[key], template.format(component=key), template

    if key in _EXTERNAL_ONLY_PROOF_CAPS and total and owned == 0:
        template = "source_policy:{component}_external_proof_without_owned_surface"
        return _EXTERNAL_ONLY_PROOF_CAPS[key], template.format(component=key), template

    return None, "", ""


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
