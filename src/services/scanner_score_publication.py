"""Single fail-closed policy for publishing scanner scores."""

from __future__ import annotations

from typing import Any

from src.services.scanner_report_assessment import (
    ScannerReportAssessmentError,
    assessment_projection_from_envelope,
    assessment_projection_from_report,
)


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
    try:
        if (
            "sv9_assessment" not in report
            and isinstance(report.get("assessment"), dict)
            and "raw" not in report
            and "score" not in report
            and not isinstance(report.get("components"), list)
        ):
            assessment = assessment_projection_from_envelope(
                report["assessment"],
                raw_score=report.get("brand3_score"),
                raw_base_average=report.get("base_average"),
                raw_magnetism_capped=report.get("magnetism_capped"),
            )
        else:
            assessment = assessment_projection_from_report(report)
    except ScannerReportAssessmentError:
        # A persisted assessment-bearing report that no longer validates is
        # never allowed to publish a score.  Keep the raw number only as a
        # diagnostic so callers can explain the failure without trusting it.
        raw_score = report.get("score")
        return {
            "publishable": False,
            "classification": str(
                (report.get("stability") or {}).get("classification") or ""
            ),
            "value": None,
            "raw_value": raw_score,
            "retention_reason": "invalid_assessment",
            "availability": "invalid",
            "sv9_score": None,
            "base_average": None,
            "magnetism_capped": None,
            "assessment_fingerprint": None,
            "score_fingerprint": None,
        }
    stability = (
        report.get("stability")
        if isinstance(report.get("stability"), dict)
        else {}
    )
    classification = str(stability.get("classification") or "").strip()
    availability = str(assessment.get("availability") or "legacy")
    publishable = (
        availability in {"available", "legacy"}
        and classification not in RETAINED_SCORE_CLASSIFICATIONS
    )
    raw_value = (
        report.get("score")
        if availability == "available"
        else assessment.get("raw_score")
    )
    canonical_value = (
        assessment.get("sv9_score") if availability == "available" else None
    )
    retention_reason = "" if publishable else classification
    if availability == "unavailable":
        retention_reason = "assessment_unavailable"
        publishable = False
    elif availability == "legacy":
        # Legacy reports are supported as historical diagnostics.  Their
        # existing score remains publishable for compatibility, but it does
        # not receive a fabricated SV9 identity.
        canonical_value = raw_value
    return {
        "publishable": publishable,
        "classification": classification,
        "value": canonical_value if publishable else None,
        "raw_value": raw_value,
        "retention_reason": retention_reason,
        "availability": availability,
        "sv9_score": (
            assessment.get("sv9_score")
            if availability == "available"
            else None
        ),
        "base_average": (
            assessment.get("base_average")
            if availability == "available"
            else assessment.get("raw_base_average")
        ),
        "magnetism_capped": (
            assessment.get("magnetism_capped")
            if availability == "available"
            else assessment.get("raw_magnetism_capped")
        ),
        "assessment_fingerprint": assessment.get("assessment_fingerprint"),
        "score_fingerprint": assessment.get("score_fingerprint"),
    }
