from __future__ import annotations

from collections import ChainMap, UserDict
from copy import deepcopy
from types import MappingProxyType

import pytest

from src.sv9.assessment_kernel import build_scanner_sv9_assessment
from src.sv9.models import (
    ComponentResult,
    ESTADO_NO,
    ESTADO_OK,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    TileVerdict,
)
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER, tile_ids
from src.sv9.scanner_semantic_assessment import (
    SOURCE_POLICY_REASSESSMENT_REASON,
    SV9_SCANNER_SEMANTIC_ASSESSMENT_SHADOW_VERSION,
    build_scanner_semantic_assessment,
)
from src.sv9.source_policy import SOURCE_POLICY_REASON_CLASSIFICATIONS, build_source_policy_plan


def _scored(
    key: str,
    score: int | None = None,
    *,
    summary: dict[str, int] | None = None,
    limitations: list[str] | None = None,
) -> ComponentResult:
    score = int(COMPONENTS[key]["scale"]) if score is None else score
    return ComponentResult(
        component=key,
        status=STATUS_SCORED,
        score=score,
        tile_profile=[
            TileVerdict(
                tile_id=tile_id,
                estado=ESTADO_OK if index < score else ESTADO_NO,
                evidencia="literal" if index < score else "",
                motivo="reason" if index >= score else "",
            )
            for index, tile_id in enumerate(tile_ids(key))
        ],
        evidence_source_summary=summary or {},
        detection_limitations=limitations or [],
    )


def _components(**overrides: ComponentResult) -> dict[str, ComponentResult]:
    components = {key: _scored(key) for key in PRESENTATION_ORDER}
    components.update(overrides)
    return components


@pytest.mark.parametrize(
    ("component", "kwargs", "reason"),
    [
        (
            "mission",
            {"limitations": ["coverage:mission_verified_absent"]},
            "source_policy:mission_owned_expression_verified_absent",
        ),
        (
            "vision",
            {"limitations": ["coverage:vision_implied_not_explicit"]},
            "source_policy:vision_implied_not_explicit",
        ),
        ("values", {"summary": {"external_proof": 1, "total": 1}}, "source_policy:values_requires_owned_expression"),
        (
            "core_purpose",
            {"summary": {"owned_copy": 1, "external_proof": 3, "total": 4}},
            "source_policy:core_purpose_external_proof_dominates_owned_expression",
        ),
        (
            "personality",
            {"summary": {"derived_strategy": 1}},
            "source_policy:personality_derived_strategy_is_not_primary_evidence",
        ),
        (
            "magnetism",
            {"summary": {"external_proof": 1, "total": 1}},
            "source_policy:magnetism_external_proof_without_owned_surface",
        ),
    ],
)
def test_each_direct_legacy_classifier_blocks_semantic_shadow(
    component: str, kwargs: dict[str, object], reason: str
) -> None:
    components = _components(**{component: _scored(component, **kwargs)})
    before = deepcopy(components)

    plan = build_source_policy_plan(components)
    shadow = build_scanner_semantic_assessment(components)

    assert reason in {action.reason for action in plan.actions}
    assert SOURCE_POLICY_REASON_CLASSIFICATIONS[reason.replace(component, "{component}", 1)]
    assert shadow == {
        "schema_version": SV9_SCANNER_SEMANTIC_ASSESSMENT_SHADOW_VERSION,
        "availability": "unavailable",
        "reason_codes": [SOURCE_POLICY_REASSESSMENT_REASON],
        "assessment_output": None,
    }
    assert components == before


def test_boundary_action_uses_original_kernel_vector_without_legacy_cap() -> None:
    components = _components(mission=_scored("mission", 3, summary={"external_proof": 2, "total": 2}))
    kernel = build_scanner_sv9_assessment(components)

    shadow = build_scanner_semantic_assessment(components)

    assert shadow == {
        "schema_version": SV9_SCANNER_SEMANTIC_ASSESSMENT_SHADOW_VERSION,
        "availability": "available",
        "reason_codes": [],
        "assessment_output": kernel,
    }
    assert shadow["assessment_output"]["sv9_score"] == 98


def test_coherencia_legacy_note_is_not_a_semantic_source_policy_trigger() -> None:
    components = _components()
    components["coherencia"].source_policy_notes.append(
        "source_policy:coherencia_capped_after_component_authority_caps"
    )

    shadow = build_scanner_semantic_assessment(components)

    assert shadow["availability"] == "available"
    assert shadow["assessment_output"] == build_scanner_sv9_assessment(components)


@pytest.mark.parametrize("summary", [{"external_proof": 2, "total": 2}, {"total": float("inf")}])
@pytest.mark.parametrize("mapping_factory", [UserDict, ChainMap, MappingProxyType])
def test_mapping_inputs_still_block_effective_direct_caps(mapping_factory, summary) -> None:
    components = _components(mission=_scored("mission", 5, summary=summary))

    shadow = build_scanner_semantic_assessment(mapping_factory(components))

    assert shadow == {
        "schema_version": SV9_SCANNER_SEMANTIC_ASSESSMENT_SHADOW_VERSION,
        "availability": "unavailable",
        "reason_codes": [SOURCE_POLICY_REASSESSMENT_REASON],
        "assessment_output": None,
    }


@pytest.mark.parametrize("invalid", ["not_evaluated", "duplicate_tile"])
def test_incomplete_or_invalid_original_vectors_stay_closed(invalid: str) -> None:
    components = _components()
    if invalid == "not_evaluated":
        components["vision"] = ComponentResult(component="vision", status=STATUS_NOT_EVALUATED, error="timeout")
    else:
        components["mission"].tile_profile[-1] = deepcopy(components["mission"].tile_profile[0])

    adapter = build_scanner_sv9_assessment(components)
    shadow = build_scanner_semantic_assessment(components)

    assert adapter["availability"] == shadow["availability"] == "unavailable"
    assert shadow["reason_codes"] == adapter["reason_codes"]
    assert shadow["assessment_output"] is None


def test_invalid_vector_has_precedence_over_direct_source_policy_action() -> None:
    components = _components(mission=_scored("mission", 5, summary={"external_proof": 1, "total": 1}))
    components["mission"].tile_profile[-1] = deepcopy(components["mission"].tile_profile[0])

    adapter = build_scanner_sv9_assessment(components)
    shadow = build_scanner_semantic_assessment(components)

    assert adapter["availability"] == "unavailable"
    assert adapter["reason_codes"] == ["invalid_tile_profile:duplicate assessment tile: M1"]
    assert shadow["availability"] == "unavailable"
    assert shadow["reason_codes"] == adapter["reason_codes"]
    assert shadow["assessment_output"] is None
