"""Non-public Scanner semantic assessment shadow.

This adapter is deliberately unreachable from aggregate, persistence, APIs, and
runtime code.  It scores only the original evaluator vector: any direct legacy
source-policy cap that would alter a component requires tile reassessment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.sv9.assessment_kernel import build_scanner_sv9_assessment
from src.sv9.models import ComponentResult
from src.sv9.source_policy import SourcePolicyAction, build_source_policy_plan

SV9_SCANNER_SEMANTIC_ASSESSMENT_SHADOW_VERSION = "sv9-scanner-semantic-assessment-shadow-v2"
SOURCE_POLICY_REASSESSMENT_REASON = "source_policy_requires_tile_reassessment"


def build_scanner_semantic_assessment(
    original_components: Mapping[str, ComponentResult],
) -> dict[str, Any]:
    """Return a non-authoritative kernel assessment of the original vector.

    The strict Scanner adapter validates the original vector first. Only a
    valid complete vector can be blocked by a direct legacy source-policy cap.
    """

    assessment = build_scanner_sv9_assessment(original_components)
    if assessment["availability"] != "available":
        return _shadow_output(assessment)
    try:
        plan = build_source_policy_plan(original_components)
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        return _reassessment_unavailable()
    if any(_action_would_cap(original_components, action) for action in plan.actions):
        return _reassessment_unavailable()
    return _shadow_output(assessment)


def _shadow_output(assessment: Mapping[str, Any]) -> dict[str, Any]:
    available = assessment["availability"] == "available"
    return {
        "schema_version": SV9_SCANNER_SEMANTIC_ASSESSMENT_SHADOW_VERSION,
        "availability": assessment["availability"],
        "reason_codes": assessment["reason_codes"],
        "assessment_output": dict(assessment) if available else None,
    }


def _reassessment_unavailable() -> dict[str, Any]:
    return {
        "schema_version": SV9_SCANNER_SEMANTIC_ASSESSMENT_SHADOW_VERSION,
        "availability": "unavailable",
        "reason_codes": [SOURCE_POLICY_REASSESSMENT_REASON],
        "assessment_output": None,
    }


def _action_would_cap(components: Mapping[str, ComponentResult], action: SourcePolicyAction) -> bool:
    """Keep malformed values for the strict adapter instead of coercing them."""

    score = getattr(components.get(action.component), "score", None)
    return type(score) is int and score > action.cap


__all__ = [
    "SOURCE_POLICY_REASSESSMENT_REASON",
    "SV9_SCANNER_SEMANTIC_ASSESSMENT_SHADOW_VERSION",
    "build_scanner_semantic_assessment",
]
