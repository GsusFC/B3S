"""Read-boundary validation and projection for persisted Scanner SV9 reports.

The scan runner produces a report with several compatibility projections of one
SV9 assessment.  This module is the single consumer boundary for those
projections: assessment-bearing reports are validated against the SV9 kernel
and every public reader receives the values stored in the validated envelope.
Legacy reports remain readable, but their score is explicitly diagnostic and
never receives a fabricated assessment identity.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping


class ScannerReportAssessmentError(RuntimeError):
    """A report cannot prove one immutable SV9 assessment projection."""


_SCANNER_ASSESSMENT_FIELDS = frozenset(
    {
        "schema_version",
        "assessment_schema_version",
        "rubric_version",
        "assessment_vector_version",
        "scoring_policy_version",
        "tile_contract_registry_fingerprint",
        "tile_count",
        "expected_tile_count",
        "tiles",
        "availability",
        "reason_codes",
        "sv9_score",
        "component_breakdown",
        "base_average",
        "magnetism_capped",
        "assessment_fingerprint",
        "score_fingerprint",
    }
)
_SCANNER_ASSESSMENT_V2_FIELDS = _SCANNER_ASSESSMENT_FIELDS | {"component_sentinels"}
_ASSESSMENT_MARKER_KEYS = frozenset(
    {
        "assessment",
        "assessment_output",
        "assessment_fingerprint",
        "score_fingerprint",
        "assessment_schema_version",
        "assessment_vector_version",
        "scoring_policy_version",
        "tile_contract_registry_fingerprint",
        "expected_tile_count",
        "component_breakdown",
    }
)
_ASSESSMENT_BREAKDOWN_INTEGER_FIELDS = (
    "tile_count",
    "ok_count",
    "no_count",
    "sin_evidencia_count",
    "raw_score",
    "effective_score",
    "multiplier",
    "points",
    "max_points",
)


def _is_strict_int(value: Any) -> bool:
    """Return whether a value is an integer, excluding JSON booleans."""

    return isinstance(value, int) and not isinstance(value, bool)


def _is_numeric(value: Any) -> bool:
    """Return whether a score-like value is numeric, excluding booleans."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _integer_projection_equal(actual: Any, expected: Any) -> bool:
    """Compare integer aliases without Python's bool-is-int coercion."""

    return _is_strict_int(actual) and _is_strict_int(expected) and actual == expected


def _numeric_projection_equal(actual: Any, expected: Any) -> bool:
    """Compare score/base aliases while rejecting boolean coercion."""

    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if _is_numeric(expected):
        return _is_numeric(actual) and actual == expected
    return actual == expected


def assessment_projection_from_report(
    report: Mapping[str, Any],
    *,
    required: bool = False,
) -> dict[str, Any]:
    """Validate and project a persisted report's SV9 assessment.

    ``available`` reports use only the stored kernel output.  ``unavailable``
    reports retain a legacy aggregate under ``raw_score`` for diagnostics but
    expose no score identity.  Reports written before the assessment envelope
    existed are returned as ``legacy`` and retain their historical score/base
    values without inventing fingerprints.
    """

    if not isinstance(report, Mapping):
        raise ScannerReportAssessmentError("report must be a mapping")
    assessment_present = "sv9_assessment" in report
    assessment = report.get("sv9_assessment")
    if assessment_present and not isinstance(assessment, Mapping):
        raise ScannerReportAssessmentError("sv9_assessment_envelope_invalid")
    if not assessment_present:
        if _has_assessment_markers(report):
            raise ScannerReportAssessmentError("sv9_assessment_envelope_missing")
        if required:
            raise ScannerReportAssessmentError(
                "vault_canonical_assessment_unavailable"
            )
        return _legacy_projection(report)

    output = _validated_scanner_assessment_output(assessment)
    raw = report.get("raw") if isinstance(report.get("raw"), Mapping) else {}
    sv9 = raw.get("sv9") if isinstance(raw.get("sv9"), Mapping) else {}
    result = sv9.get("result") if isinstance(sv9.get("result"), Mapping) else {}

    assessment_identity = _strict_json_identity(assessment)
    for duplicate in (sv9.get("assessment"), result.get("assessment")):
        if _strict_json_identity(duplicate) != assessment_identity:
            raise ScannerReportAssessmentError("sv9_assessment_duplicate_mismatch")
    for container in (report, sv9, result):
        if container.get("assessment_fingerprint") != assessment.get(
            "assessment_fingerprint"
        ):
            raise ScannerReportAssessmentError("sv9_assessment_fingerprint_mismatch")
        if container.get("score_fingerprint") != assessment.get("score_fingerprint"):
            raise ScannerReportAssessmentError("sv9_score_fingerprint_mismatch")

    if output is None:
        _validate_unavailable_projection(report, sv9, result, assessment)
        return {
            "availability": "unavailable",
            "sv9_score": None,
            "base_average": None,
            "magnetism_capped": None,
            "assessment_fingerprint": None,
            "score_fingerprint": None,
            "assessment": copy.deepcopy(dict(assessment)),
            "raw_score": _legacy_score(report, sv9, result),
            "raw_base_average": _legacy_base_average(report, sv9, result),
            "legacy_score": _legacy_score(report, sv9, result),
            "legacy_base_average": _legacy_base_average(report, sv9, result),
            "component_projections": {},
            "is_canonical": False,
        }

    _validate_available_projection(report, sv9, result, output)
    return {
        "availability": "available",
        "sv9_score": output["sv9_score"],
        "base_average": output["base_average"],
        "magnetism_capped": output["magnetism_capped"],
        "assessment_fingerprint": output["assessment_fingerprint"],
        "score_fingerprint": output["score_fingerprint"],
        "assessment": copy.deepcopy(dict(assessment)),
        "component_projections": _canonical_component_projections(output),
        "is_canonical": True,
    }


# These descriptive aliases keep the source-layer contract discoverable while
# allowing callers that used the design terminology to use the same service.
report_assessment_projection = assessment_projection_from_report
project_report_assessment = assessment_projection_from_report
report_assessment_from_report = assessment_projection_from_report
scanner_report_assessment_from_report = assessment_projection_from_report


def validate_report_sv9_assessment(
    report: Mapping[str, Any],
    *,
    required: bool = False,
) -> dict[str, Any]:
    """Compatibility entry point used by the scan runner and read boundaries."""

    return assessment_projection_from_report(report, required=required)


def assessment_projection_from_envelope(
    assessment: Mapping[str, Any],
    *,
    raw_score: Any = None,
    raw_base_average: Any = None,
    raw_magnetism_capped: Any = None,
) -> dict[str, Any]:
    """Project an already-bound assessment envelope for export adapters.

    Markdown/API adapters sometimes receive the nested SV9 result rather than
    the full persisted report.  They do not have enough context to re-check
    report-level duplicates, so they validate the kernel envelope here and
    retain the same canonical values without doing any arithmetic.
    """

    if not isinstance(assessment, Mapping):
        raise ScannerReportAssessmentError("sv9_assessment_envelope_invalid")
    output = _validated_scanner_assessment_output(assessment)
    if output is None:
        return {
            "availability": "unavailable",
            "sv9_score": None,
            "base_average": None,
            "magnetism_capped": None,
            "assessment_fingerprint": None,
            "score_fingerprint": None,
            "assessment": copy.deepcopy(dict(assessment)),
            "raw_score": raw_score,
            "raw_base_average": raw_base_average,
            "raw_magnetism_capped": raw_magnetism_capped,
            "legacy_score": raw_score,
            "legacy_base_average": raw_base_average,
            "legacy_magnetism_capped": raw_magnetism_capped,
            "component_projections": {},
            "is_canonical": False,
        }
    return {
        "availability": "available",
        "sv9_score": output["sv9_score"],
        "base_average": output["base_average"],
        "magnetism_capped": output["magnetism_capped"],
        "assessment_fingerprint": output["assessment_fingerprint"],
        "score_fingerprint": output["score_fingerprint"],
        "assessment": copy.deepcopy(dict(assessment)),
        "component_projections": _canonical_component_projections(output),
        "is_canonical": True,
    }


def _validated_scanner_assessment_output(
    assessment: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate the aggregate adapter without reimplementing SV9 arithmetic."""

    from src.sv9.assessment_kernel import (
        SV9_ASSESSMENT_OUTPUT_VERSION,
        SV9_ASSESSMENT_OUTPUT_V2_VERSION,
        SV9_SCANNER_ASSESSMENT_VERSION,
        SV9_SCANNER_ASSESSMENT_V2_VERSION,
        Sv9AssessmentError,
        validate_sv9_assessment_output,
    )

    schema_version = assessment.get("schema_version")
    fields = (
        _SCANNER_ASSESSMENT_V2_FIELDS
        if schema_version == SV9_SCANNER_ASSESSMENT_V2_VERSION
        else _SCANNER_ASSESSMENT_FIELDS
    )
    if set(assessment) != fields:
        raise ScannerReportAssessmentError("sv9_assessment_envelope_fields_mismatch")
    expected_output_version = (
        SV9_ASSESSMENT_OUTPUT_V2_VERSION
        if schema_version == SV9_SCANNER_ASSESSMENT_V2_VERSION
        else SV9_ASSESSMENT_OUTPUT_VERSION
    )
    if schema_version not in {SV9_SCANNER_ASSESSMENT_VERSION, SV9_SCANNER_ASSESSMENT_V2_VERSION}:
        raise ScannerReportAssessmentError("sv9_assessment_envelope_schema_invalid")
    if assessment.get("assessment_schema_version") != expected_output_version:
        raise ScannerReportAssessmentError("sv9_assessment_output_schema_invalid")
    if not _is_strict_int(assessment.get("expected_tile_count")) or assessment.get(
        "expected_tile_count"
    ) != 80:
        raise ScannerReportAssessmentError(
            "sv9_assessment_expected_tile_count_mismatch"
        )

    availability = assessment.get("availability")
    _validate_assessment_numeric_types(assessment, availability=availability)
    if availability == "unavailable":
        if schema_version == SV9_SCANNER_ASSESSMENT_V2_VERSION:
            raise ScannerReportAssessmentError("sv9_v2_unavailable_assessment_invalid")
        if (
            not _is_strict_int(assessment.get("tile_count"))
            or assessment.get("tile_count") != 0
            or assessment.get("tiles") is not None
            or assessment.get("sv9_score") is not None
            or assessment.get("component_breakdown") is not None
            or assessment.get("base_average") is not None
            or assessment.get("magnetism_capped") is not None
            or assessment.get("assessment_fingerprint") is not None
            or assessment.get("score_fingerprint") is not None
        ):
            raise ScannerReportAssessmentError(
                "sv9_unavailable_assessment_projection_invalid"
            )
        reason_codes = assessment.get("reason_codes")
        if (
            not isinstance(reason_codes, list)
            or not reason_codes
            or any(not isinstance(code, str) or not code for code in reason_codes)
        ):
            raise ScannerReportAssessmentError(
                "sv9_unavailable_assessment_reasons_invalid"
            )
        return None
    if availability != "available" or assessment.get("reason_codes") != []:
        raise ScannerReportAssessmentError("sv9_assessment_availability_invalid")

    output = {
        "schema_version": assessment.get("assessment_schema_version"),
        "rubric_version": assessment.get("rubric_version"),
        "assessment_vector_version": assessment.get("assessment_vector_version"),
        "scoring_policy_version": assessment.get("scoring_policy_version"),
        "tile_contract_registry_fingerprint": assessment.get(
            "tile_contract_registry_fingerprint"
        ),
        "tile_count": assessment.get("tile_count"),
        "tiles": assessment.get("tiles"),
        "sv9_score": assessment.get("sv9_score"),
        "component_breakdown": assessment.get("component_breakdown"),
        "base_average": assessment.get("base_average"),
        "magnetism_capped": assessment.get("magnetism_capped"),
        "assessment_fingerprint": assessment.get("assessment_fingerprint"),
        "score_fingerprint": assessment.get("score_fingerprint"),
    }
    if schema_version == SV9_SCANNER_ASSESSMENT_V2_VERSION:
        output["component_sentinels"] = assessment.get("component_sentinels")
    try:
        validate_sv9_assessment_output(output)
    except Sv9AssessmentError as exc:
        raise ScannerReportAssessmentError("sv9_assessment_output_invalid") from exc
    return output


def _has_assessment_markers(report: Mapping[str, Any]) -> bool:
    """Detect stripped scanner-assessment metadata before legacy fallback."""

    if report.get("assessment") is not None:
        return True
    if any(
        report.get(key) is not None
        for key in ("assessment_fingerprint", "score_fingerprint")
    ):
        return True
    raw = report.get("raw")
    if not isinstance(raw, Mapping):
        return False
    if _has_structural_assessment_markers(raw):
        return True
    sv9 = raw.get("sv9")
    if _has_structural_assessment_markers(sv9):
        return True
    if isinstance(sv9, Mapping) and any(
        _has_structural_assessment_markers(sv9.get(container))
        for container in ("result", "summary")
    ):
        return True
    for container in ("result", "legacy_sv9"):
        if _has_structural_assessment_markers(raw.get(container)):
            return True
    flow = raw.get("flow")
    if _has_structural_assessment_markers(flow):
        return True
    if isinstance(flow, Mapping):
        for container in ("sv9", "result", "summary"):
            if _has_structural_assessment_markers(flow.get(container)):
                return True
    return False


def _has_structural_assessment_markers(value: Any) -> bool:
    """Inspect one documented assessment summary slot, never arbitrary data."""

    if not isinstance(value, Mapping):
        return False
    return any(key in value for key in _ASSESSMENT_MARKER_KEYS)


def _strict_json_identity(value: Any) -> str:
    """Return deterministic JSON identity without Python scalar coercion."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ScannerReportAssessmentError(
            "sv9_assessment_duplicate_invalid"
        ) from exc


def _validate_assessment_numeric_types(
    assessment: Mapping[str, Any],
    *,
    availability: Any,
) -> None:
    """Reject bool aliases before kernel equality can treat them as integers."""

    if not _is_strict_int(assessment.get("tile_count")):
        raise ScannerReportAssessmentError(
            "sv9_assessment_numeric_type_invalid:tile_count"
        )
    if availability == "available":
        for field in ("sv9_score",):
            if not _is_strict_int(assessment.get(field)):
                raise ScannerReportAssessmentError(
                    f"sv9_assessment_numeric_type_invalid:{field}"
                )
        if not _is_numeric(assessment.get("base_average")):
            raise ScannerReportAssessmentError(
                "sv9_assessment_numeric_type_invalid:base_average"
            )
        breakdown = assessment.get("component_breakdown")
        if isinstance(breakdown, list):
            for row in breakdown:
                if not isinstance(row, Mapping):
                    continue
                for field in _ASSESSMENT_BREAKDOWN_INTEGER_FIELDS:
                    if not _is_strict_int(row.get(field)):
                        raise ScannerReportAssessmentError(
                            "sv9_assessment_numeric_type_invalid:"
                            f"component_breakdown.{field}"
                        )


def _component_rows_by_key(
    rows: Any,
    *,
    location: str,
) -> dict[str, Mapping[str, Any]]:
    """Index a report component list without silently collapsing duplicates."""

    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_component_projection_invalid:{location}"
        )
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ScannerReportAssessmentError(
                f"sv9_assessment_component_projection_invalid:{location}"
            )
        key = str(row.get("key") or row.get("component") or "").strip()
        if not key:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_component_key_invalid:{location}"
            )
        if key in indexed:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_component_duplicate:{location}:{key}"
            )
        indexed[key] = row
    return indexed


def _component_mapping_by_key(
    value: Any,
    *,
    location: str,
) -> dict[str, Mapping[str, Any]]:
    """Normalize a keyed component mapping while checking declared keys."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_component_projection_invalid:{location}"
        )
    indexed: dict[str, Mapping[str, Any]] = {}
    for raw_key, row in value.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(row, Mapping):
            raise ScannerReportAssessmentError(
                f"sv9_assessment_component_projection_invalid:{location}"
            )
        declared_key = str(row.get("key") or row.get("component") or key).strip()
        if declared_key != key:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_component_key_mismatch:{location}:{key}"
            )
        indexed[key] = row
    return indexed


def _canonical_component_projections(
    output: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Expose component identity fields directly from the validated kernel output."""

    profiles: dict[str, list[dict[str, str]]] = {}
    for tile in output["tiles"]:
        component_key = str(tile["component_key"])
        profiles.setdefault(component_key, []).append(
            {
                "id": str(tile["tile_id"]),
                "estado": str(tile["assessment_state"]),
            }
        )
    sentinels = {
        row["component_key"]: row for row in output.get("component_sentinels", [])
    }
    projections: dict[str, dict[str, Any]] = {}
    for row in output["component_breakdown"]:
        component_key = str(row["component_key"])
        if component_key in projections:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_component_duplicate:assessment:{component_key}"
            )
        sentinel = sentinels.get(component_key)
        projections[component_key] = {
            "status": sentinel["status"] if sentinel else "scored",
            "score": row["effective_score"],
            "points": row["points"],
            "scale": sentinel["scale"] if sentinel else row["tile_count"],
            "lit": row["ok_count"],
            "off": row["no_count"],
            "blind": row["sin_evidencia_count"],
            "tile_count": row["tile_count"],
            "tile_profile": profiles.get(component_key, []),
        }
    return projections


def _validate_component_counts(
    component: Mapping[str, Any],
    *,
    component_key: str,
    expected_profile: Mapping[str, str],
    breakdown: Mapping[str, Any],
    expected_scale: int,
    location: str,
) -> None:
    """Validate all public count/scale aliases against kernel breakdown data."""

    expected_counts = {
        "lit": breakdown["ok_count"],
        "off": breakdown["no_count"],
        "blind": breakdown["sin_evidencia_count"],
    }
    if not _integer_projection_equal(component.get("scale"), expected_scale):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_component_scale_mismatch:{location}:{component_key}"
        )

    if location == "report":
        for field, expected in expected_counts.items():
            if field not in component or not _integer_projection_equal(
                component.get(field), expected
            ):
                raise ScannerReportAssessmentError(
                    f"sv9_assessment_component_{field}_mismatch:{location}:{component_key}"
                )
        return

    expected_tiles = {
        "lit_tiles": sorted(
            tile_id for tile_id, state in expected_profile.items() if state == "ok"
        ),
        "off_tiles": sorted(
            tile_id for tile_id, state in expected_profile.items() if state == "no"
        ),
        "blind_spot_tiles": sorted(
            tile_id
            for tile_id, state in expected_profile.items()
            if state == "sin_evidencia"
        ),
    }
    for field, expected in expected_tiles.items():
        actual = component.get(field)
        if not isinstance(actual, list) or sorted(str(item) for item in actual) != expected:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_component_{field}_mismatch:{location}:{component_key}"
            )
    if "blind_spot_count" not in component or not _integer_projection_equal(
        component.get("blind_spot_count"), expected_counts["blind"]
    ):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_component_blind_mismatch:{location}:{component_key}"
        )
    for field, expected in expected_counts.items():
        if field in component and not _integer_projection_equal(
            component.get(field), expected
        ):
            raise ScannerReportAssessmentError(
                f"sv9_assessment_component_{field}_mismatch:{location}:{component_key}"
            )


def _validate_unavailable_projection(
    report: Mapping[str, Any],
    sv9: Mapping[str, Any],
    result: Mapping[str, Any],
    assessment: Mapping[str, Any],
) -> None:
    for container in (report, sv9, result):
        projected_score = (
            container.get("score")
            if container is report
            else container.get("brand3_score")
        )
        if not _numeric_projection_equal(
            projected_score, sv9.get("brand3_score")
        ):
            raise ScannerReportAssessmentError(
                "sv9_assessment_score_projection_mismatch"
            )
        if not _numeric_projection_equal(
            container.get("base_average"), sv9.get("base_average")
        ):
            raise ScannerReportAssessmentError(
                "sv9_assessment_base_average_projection_mismatch"
            )
        if not _numeric_projection_equal(
            container.get("magnetism_capped"), sv9.get("magnetism_capped")
        ):
            raise ScannerReportAssessmentError(
                "sv9_assessment_magnetism_cap_projection_mismatch"
            )

    summary_components = _component_mapping_by_key(
        sv9.get("components"),
        location="summary",
    )
    result_components = _component_mapping_by_key(
        result.get("components"),
        location="result",
    )
    report_components = _component_rows_by_key(
        report.get("components"),
        location="report",
    )
    if (
        set(summary_components) != set(result_components)
        or set(summary_components) != set(report_components)
    ):
        raise ScannerReportAssessmentError(
            "sv9_assessment_component_projection_invalid"
        )
    for component_key, summary in summary_components.items():
        if not isinstance(summary, Mapping):
            raise ScannerReportAssessmentError(
                "sv9_assessment_component_projection_invalid"
            )
        for location, component in (
            ("result", result_components.get(component_key)),
            ("report", report_components.get(component_key)),
        ):
            if not isinstance(component, Mapping):
                raise ScannerReportAssessmentError(
                    f"sv9_assessment_component_missing:{location}:{component_key}"
                )
            for field in ("status", "score", "points"):
                matches = (
                    (
                        field not in component
                        and field not in summary
                    )
                    or _integer_projection_equal(
                        component.get(field), summary.get(field)
                    )
                    if field in {"score", "points"}
                    else component.get(field) == summary.get(field)
                )
                if not matches:
                    raise ScannerReportAssessmentError(
                        "sv9_assessment_component_"
                        f"{field}_mismatch:{location}:{component_key}"
                    )


def _validate_available_projection(
    report: Mapping[str, Any],
    sv9: Mapping[str, Any],
    result: Mapping[str, Any],
    output: Mapping[str, Any],
) -> None:
    sentinels = {
        row["component_key"]: row for row in output.get("component_sentinels", [])
    }
    expected_not_detected = list(sentinels)
    for container in (report, sv9, result):
        if container.get("not_detected") != expected_not_detected:
            raise ScannerReportAssessmentError("sv9_assessment_not_detected_mismatch")
    for container in (report, sv9, result):
        projected_score = (
            container.get("score")
            if container is report
            else container.get("brand3_score")
        )
        if not _integer_projection_equal(
            projected_score, output["sv9_score"]
        ):
            raise ScannerReportAssessmentError(
                "sv9_assessment_score_projection_mismatch"
            )
        if not _numeric_projection_equal(
            container.get("base_average"), output["base_average"]
        ):
            raise ScannerReportAssessmentError(
                "sv9_assessment_base_average_projection_mismatch"
            )
    if report.get("magnetism_capped") is not output["magnetism_capped"]:
        raise ScannerReportAssessmentError(
            "sv9_assessment_magnetism_cap_projection_mismatch"
        )
    if sv9.get("magnetism_capped") is not output["magnetism_capped"]:
        raise ScannerReportAssessmentError(
            "sv9_assessment_magnetism_cap_projection_mismatch"
        )
    if result.get("magnetism_capped") is not output["magnetism_capped"]:
        raise ScannerReportAssessmentError(
            "sv9_assessment_magnetism_cap_projection_mismatch"
        )

    breakdown: dict[str, Mapping[str, Any]] = {}
    for row in output["component_breakdown"]:
        if not isinstance(row, Mapping):
            raise ScannerReportAssessmentError(
                "sv9_assessment_component_breakdown_invalid"
            )
        component_key = str(row.get("component_key") or "").strip()
        if not component_key or component_key in breakdown:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_component_duplicate:assessment:{component_key}"
            )
        breakdown[component_key] = row
    expected_profiles: dict[str, dict[str, str]] = {
        component_key: {} for component_key in breakdown
    }
    for tile in output["tiles"]:
        component_key = str(tile["component_key"])
        tile_id = str(tile["tile_id"])
        profile = expected_profiles.setdefault(component_key, {})
        if tile_id in profile:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_tile_duplicate:assessment:{component_key}:{tile_id}"
            )
        profile[tile_id] = str(tile["assessment_state"])
    report_components = _component_rows_by_key(
        report.get("components"),
        location="report",
    )
    result_components = _component_mapping_by_key(
        result.get("components"),
        location="result",
    )
    summary_components = _component_mapping_by_key(
        sv9.get("components"),
        location="summary",
    )
    if set(breakdown) != set(expected_profiles):
        raise ScannerReportAssessmentError(
            "sv9_assessment_component_breakdown_invalid"
        )
    if (
        set(report_components) != set(expected_profiles)
        or set(result_components) != set(expected_profiles)
        or set(summary_components) != set(expected_profiles)
    ):
        raise ScannerReportAssessmentError(
            "sv9_assessment_component_projection_invalid"
        )
    for component_key, component_score in breakdown.items():
        expected_score = component_score["effective_score"]
        expected_points = component_score["points"]
        expected_profile = expected_profiles[component_key]
        sentinel = sentinels.get(component_key)
        expected_status = sentinel["status"] if sentinel else "scored"
        for location, component in (
            ("report", report_components.get(component_key)),
            ("result", result_components.get(component_key)),
            ("summary", summary_components.get(component_key)),
        ):
            if not isinstance(component, Mapping):
                raise ScannerReportAssessmentError(
                    f"sv9_assessment_component_missing:{location}:{component_key}"
                )
            if not _integer_projection_equal(component.get("score"), expected_score):
                raise ScannerReportAssessmentError(
                    f"sv9_assessment_component_score_mismatch:{location}:{component_key}"
                )
            if not _integer_projection_equal(component.get("points"), expected_points):
                raise ScannerReportAssessmentError(
                    f"sv9_assessment_component_points_mismatch:{location}:{component_key}"
                )
            if component.get("status") != expected_status:
                raise ScannerReportAssessmentError(
                    f"sv9_assessment_component_status_mismatch:{location}:{component_key}"
                )
            _validate_component_counts(
                component,
                component_key=component_key,
                expected_profile=expected_profile,
                breakdown=component_score,
                expected_scale=sentinel["scale"] if sentinel else component_score["tile_count"],
                location=location,
            )
        _validate_tile_profile_projection(
            report_components[component_key].get("tile_profile"),
            expected_states=expected_profile,
            component_key=component_key,
            location="report",
        )
        _validate_tile_alias_projection(
            report_components[component_key],
            expected_states=expected_profile,
            component_key=component_key,
        )
        _validate_tile_profile_projection(
            result_components[component_key].get("tile_profile"),
            expected_states=expected_profile,
            component_key=component_key,
            location="result",
        )
        _validate_tile_profile_projection(
            summary_components[component_key].get("tile_profile"),
            expected_states=expected_profile,
            component_key=component_key,
            location="summary",
        )


def _validate_tile_profile_projection(
    profile: Any,
    *,
    expected_states: Mapping[str, str],
    component_key: str,
    location: str,
) -> None:
    if not isinstance(profile, list):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_{location}_profile_invalid"
        )
    actual_states: dict[str, str] = {}
    for row in profile:
        if not isinstance(row, Mapping):
            raise ScannerReportAssessmentError(
                f"sv9_assessment_{location}_profile_invalid"
            )
        tile_id = str(row.get("id") or row.get("tile_id") or "")
        state = str(row.get("estado") or "")
        if not tile_id or tile_id in actual_states:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_{location}_profile_invalid"
            )
        actual_states[tile_id] = state
    if actual_states != dict(expected_states):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_{location}_profile_mismatch:{component_key}"
        )


def _validate_tile_alias_projection(
    component: Mapping[str, Any],
    *,
    expected_states: Mapping[str, str],
    component_key: str,
) -> None:
    """Validate the report's failing-tile alias against the canonical profile.

    ``component["tiles"]`` is a presentation-friendly subset of the complete
    ``tile_profile`` (only ``no`` and ``sin_evidencia`` rows).  It remains an
    input projection, not an independent source of tile identity.  When it is
    persisted, every visible field must agree with the already fingerprint-
    bound profile; an omitted alias remains compatible with older reports.
    """

    if "tiles" not in component:
        return
    alias = component.get("tiles")
    if not isinstance(alias, list):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_report_tiles_alias_invalid:{component_key}"
        )

    profile = component.get("tile_profile")
    if not isinstance(profile, list):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_report_tiles_alias_invalid:{component_key}"
        )
    profile_by_id: dict[str, Mapping[str, Any]] = {}
    for row in profile:
        if not isinstance(row, Mapping):
            raise ScannerReportAssessmentError(
                f"sv9_assessment_report_tiles_alias_invalid:{component_key}"
            )
        tile_id = str(row.get("id") or row.get("tile_id") or "")
        if not tile_id or tile_id in profile_by_id:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_report_tiles_alias_invalid:{component_key}"
            )
        profile_by_id[tile_id] = row

    expected_alias_states = {
        tile_id: state for tile_id, state in expected_states.items() if state != "ok"
    }
    if len(alias) != len(expected_alias_states):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_report_tiles_alias_mismatch:{component_key}"
        )

    from src.sv9.rubric import COMPONENTS

    rubric_tiles = {
        str(tile.get("id") or ""): str(tile.get("name") or "")
        for tile in (COMPONENTS.get(component_key) or {}).get("tiles") or []
        if isinstance(tile, Mapping)
    }
    seen: set[str] = set()
    visible_fields = ("evidencia", "motivo", "contexto_requerido")
    for row in alias:
        if not isinstance(row, Mapping):
            raise ScannerReportAssessmentError(
                f"sv9_assessment_report_tiles_alias_invalid:{component_key}"
            )
        tile_id = str(row.get("id") or row.get("tile_id") or "")
        if not tile_id or tile_id in seen:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_report_tiles_alias_duplicate:{component_key}"
            )
        seen.add(tile_id)
        if tile_id not in expected_alias_states:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_report_tiles_alias_mismatch:{component_key}:{tile_id}"
            )
        if str(row.get("estado") or "") != expected_alias_states[tile_id]:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_report_tiles_alias_state_mismatch:{component_key}:{tile_id}"
            )
        profile_row = profile_by_id.get(tile_id)
        if profile_row is None:
            raise ScannerReportAssessmentError(
                f"sv9_assessment_report_tiles_alias_mismatch:{component_key}:{tile_id}"
            )
        if "name" not in row or str(row.get("name") or "") != rubric_tiles.get(tile_id, ""):
            raise ScannerReportAssessmentError(
                f"sv9_assessment_report_tiles_alias_name_mismatch:{component_key}:{tile_id}"
            )
        for field in visible_fields:
            if field not in row or str(row.get(field) or "") != str(
                profile_row.get(field) or ""
            ):
                raise ScannerReportAssessmentError(
                    "sv9_assessment_report_tiles_alias_"
                    f"{field}_mismatch:{component_key}:{tile_id}"
                )
    if seen != set(expected_alias_states):
        raise ScannerReportAssessmentError(
            f"sv9_assessment_report_tiles_alias_mismatch:{component_key}"
        )


def _legacy_projection(report: Mapping[str, Any]) -> dict[str, Any]:
    raw = report.get("raw") if isinstance(report.get("raw"), Mapping) else {}
    sv9 = raw.get("sv9") if isinstance(raw.get("sv9"), Mapping) else {}
    result = sv9.get("result") if isinstance(sv9.get("result"), Mapping) else {}
    return {
        "availability": "legacy",
        "sv9_score": None,
        "base_average": None,
        "magnetism_capped": None,
        "assessment_fingerprint": None,
        "score_fingerprint": None,
        "assessment": None,
        "raw_score": _legacy_score(report, sv9, result),
        "raw_base_average": _legacy_base_average(report, sv9, result),
        "raw_magnetism_capped": _legacy_magnetism_capped(report, sv9, result),
        "legacy_score": _legacy_score(report, sv9, result),
        "legacy_base_average": _legacy_base_average(report, sv9, result),
        "legacy_magnetism_capped": _legacy_magnetism_capped(report, sv9, result),
        "component_projections": {},
        "is_canonical": False,
    }


def _legacy_score(
    report: Mapping[str, Any],
    sv9: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Any:
    if "score" in report:
        return report.get("score")
    if "brand3_score" in sv9:
        return sv9.get("brand3_score")
    return result.get("brand3_score")


def _legacy_base_average(
    report: Mapping[str, Any],
    sv9: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Any:
    if "base_average" in report:
        return report.get("base_average")
    if "base_average" in sv9:
        return sv9.get("base_average")
    return result.get("base_average")


def _legacy_magnetism_capped(
    report: Mapping[str, Any],
    sv9: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Any:
    if "magnetism_capped" in report:
        return report.get("magnetism_capped")
    if "magnetism_capped" in sv9:
        return sv9.get("magnetism_capped")
    return result.get("magnetism_capped")


__all__ = [
    "ScannerReportAssessmentError",
    "assessment_projection_from_report",
    "assessment_projection_from_envelope",
    "project_report_assessment",
    "report_assessment_from_report",
    "report_assessment_projection",
    "scanner_report_assessment_from_report",
    "validate_report_sv9_assessment",
]
