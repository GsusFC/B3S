"""Single fail-closed policy for publishing scanner scores."""

from __future__ import annotations

from typing import Any


RETAINED_SCORE_CLASSIFICATIONS = frozenset(
    {
        "acquisition_regression",
        "candidate",
        "comparison_error",
        "contract_mismatch",
        "evaluation_drift",
        "invalid",
    }
)


def score_publication_from_report(report: dict[str, Any]) -> dict[str, Any]:
    stability = (
        report.get("stability")
        if isinstance(report.get("stability"), dict)
        else {}
    )
    classification = str(stability.get("classification") or "").strip()
    publishable = classification not in RETAINED_SCORE_CLASSIFICATIONS
    raw_value = report.get("score")
    return {
        "publishable": publishable,
        "classification": classification,
        "value": raw_value if publishable else None,
        "raw_value": raw_value,
        "retention_reason": "" if publishable else classification,
    }
