"""Pure, fail-closed SV9 assessment arithmetic for baldosas-v3-1.

The kernel accepts one complete semantic assessment vector and performs no I/O,
model calls, verification, authority resolution, persistence, or mutation.  Both
the Scanner and Evidence Vault adapt their own records to this boundary so a
given 80-tile vector has one numeric interpretation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from src.sv9.rubric import (
    BASE_COMPONENTS,
    COMPONENTS,
    ESTADO_NO,
    ESTADO_OK,
    ESTADO_SIN_EVIDENCIA,
    MAGNETISM_CAP_BASE_THRESHOLD,
    MAGNETISM_CAP_VALUE,
    PRESENTATION_ORDER,
    RUBRIC_VERSION,
    STATUS_SCORED,
)


EXPECTED_RUBRIC_VERSION = "baldosas-v3-1"
SV9_ASSESSMENT_OUTPUT_VERSION = "sv9-assessment-output-v1"
SV9_ASSESSMENT_VECTOR_VERSION = "sv9-assessment-vector-v1"
SV9_SCORING_POLICY_VERSION = "sv9-scoring-policy-baldosas-v3-1-v1"
SV9_ASSESSMENT_FINGERPRINT_VERSION = "sv9-assessment-fingerprint-v1"
SV9_SCORE_FINGERPRINT_VERSION = "sv9-score-fingerprint-v1"
SV9_TILE_CONTRACT_REGISTRY_VERSION = "sv9-tile-contract-registry-v1"
SV9_SCANNER_ASSESSMENT_VERSION = "sv9-scanner-assessment-v1"

_ASSESSMENT_FIELDS = frozenset(
    {"component_key", "tile_id", "tile_key", "assessment_state"}
)
_ASSESSMENT_OUTPUT_FIELDS = frozenset(
    {
        "schema_version",
        "rubric_version",
        "assessment_vector_version",
        "scoring_policy_version",
        "tile_contract_registry_fingerprint",
        "tile_count",
        "tiles",
        "sv9_score",
        "component_breakdown",
        "base_average",
        "magnetism_capped",
        "assessment_fingerprint",
        "score_fingerprint",
    }
)
_BREAKDOWN_FIELDS = frozenset(
    {
        "component_key",
        "tile_count",
        "ok_count",
        "no_count",
        "sin_evidencia_count",
        "raw_score",
        "effective_score",
        "multiplier",
        "points",
        "max_points",
    }
)
_ASSESSMENT_STATES = frozenset(
    {ESTADO_OK, ESTADO_NO, ESTADO_SIN_EVIDENCIA}
)


def _fingerprint(namespace: str, payload: Any) -> str:
    rendered = json.dumps(
        {
            "namespace": namespace,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _registry_rows() -> tuple[dict[str, Any], ...]:
    if RUBRIC_VERSION != EXPECTED_RUBRIC_VERSION:
        raise RuntimeError(
            "SV9 assessment kernel only supports rubric baldosas-v3-1"
        )
    rows: list[dict[str, Any]] = []
    for component_key in PRESENTATION_ORDER:
        component = COMPONENTS[component_key]
        for tile in component["tiles"]:
            tile_id = str(tile["id"])
            rows.append(
                {
                    "component_key": component_key,
                    "tile_id": tile_id,
                    "tile_key": f"{component_key}.{tile_id}",
                }
            )
    if len(rows) != 80:
        raise RuntimeError("SV9 assessment registry must contain exactly 80 tiles")
    if len({row["tile_id"] for row in rows}) != 80:
        raise RuntimeError("SV9 assessment registry tile ids must be unique")
    return tuple(rows)


_REGISTRY = _registry_rows()
_REGISTRY_BY_ID = {row["tile_id"]: row for row in _REGISTRY}
_REGISTRY_ORDER = {
    row["tile_id"]: position for position, row in enumerate(_REGISTRY)
}


def _build_tile_contract_registry() -> dict[str, Any]:
    """Build a neutral identity for the full executable baldosas contract."""

    components: list[dict[str, Any]] = []
    for component_position, component_key in enumerate(PRESENTATION_ORDER):
        component = COMPONENTS[component_key]
        tiles: list[dict[str, Any]] = []
        for tile_position, tile in enumerate(component["tiles"]):
            tile_id = str(tile["id"])
            # JSON round-tripping freezes tuples and nested mappings into the
            # same neutral value model used by fingerprinting.
            definition = json.loads(
                json.dumps(
                    tile,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
            )
            tiles.append(
                {
                    "tile_id": tile_id,
                    "tile_key": f"{component_key}.{tile_id}",
                    "tile_position": tile_position,
                    "definition": definition,
                }
            )
        components.append(
            {
                "component_key": component_key,
                "component_position": component_position,
                "scale": int(component["scale"]),
                "multiplier": int(component["multiplier"]),
                "tile_count": len(tiles),
                "tiles": tiles,
            }
        )
    return {
        "schema_version": SV9_TILE_CONTRACT_REGISTRY_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "component_count": len(components),
        "tile_count": sum(row["tile_count"] for row in components),
        "components": components,
    }


_TILE_CONTRACT_REGISTRY = _build_tile_contract_registry()
SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT = _fingerprint(
    SV9_TILE_CONTRACT_REGISTRY_VERSION,
    _TILE_CONTRACT_REGISTRY,
)


class Sv9AssessmentError(ValueError):
    """The semantic assessment vector cannot be scored safely."""


def build_sv9_tile_contract_registry() -> dict[str, Any]:
    """Return a detached copy of the neutral executable tile registry."""

    return json.loads(
        json.dumps(
            _TILE_CONTRACT_REGISTRY,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    )


def build_sv9_assessment(
    tile_assessments: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate and score one complete 80-tile SV9 assessment vector.

    Input order is non-semantic.  Rows are normalized to the canonical
    baldosas-v3-1 registry order before calculation and fingerprinting.
    Unknown, duplicate, missing, mismatched, or invalid rows raise instead of
    being coerced to a zero-valued brand judgment.
    """

    normalized = _normalize_assessments(tile_assessments)
    calculation = _calculate(normalized)
    assessment_fingerprint = _fingerprint(
        SV9_ASSESSMENT_FINGERPRINT_VERSION,
        {
            "assessment_vector_version": SV9_ASSESSMENT_VECTOR_VERSION,
            "scoring_policy_version": SV9_SCORING_POLICY_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "tile_contract_registry_fingerprint": (
                SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT
            ),
            "tiles": normalized,
        },
    )
    score_fingerprint = _fingerprint(
        SV9_SCORE_FINGERPRINT_VERSION,
        {
            "assessment_fingerprint": assessment_fingerprint,
            "scoring_policy_version": SV9_SCORING_POLICY_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "tile_contract_registry_fingerprint": (
                SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT
            ),
            "calculation": calculation,
        },
    )
    return {
        "schema_version": SV9_ASSESSMENT_OUTPUT_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "assessment_vector_version": SV9_ASSESSMENT_VECTOR_VERSION,
        "scoring_policy_version": SV9_SCORING_POLICY_VERSION,
        "tile_contract_registry_fingerprint": (
            SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT
        ),
        "tile_count": len(normalized),
        "tiles": normalized,
        **calculation,
        "assessment_fingerprint": assessment_fingerprint,
        "score_fingerprint": score_fingerprint,
    }


def validate_sv9_assessment_output(output: Mapping[str, Any]) -> None:
    """Recompose and strictly verify one persisted kernel output snapshot."""

    if not isinstance(output, Mapping) or set(output) != _ASSESSMENT_OUTPUT_FIELDS:
        raise Sv9AssessmentError("SV9 assessment output fields mismatch")
    if output.get("schema_version") != SV9_ASSESSMENT_OUTPUT_VERSION:
        raise Sv9AssessmentError("unsupported SV9 assessment output schema")
    if output.get("rubric_version") != RUBRIC_VERSION:
        raise Sv9AssessmentError("SV9 assessment output rubric mismatch")
    if output.get("assessment_vector_version") != SV9_ASSESSMENT_VECTOR_VERSION:
        raise Sv9AssessmentError("SV9 assessment vector version mismatch")
    if output.get("scoring_policy_version") != SV9_SCORING_POLICY_VERSION:
        raise Sv9AssessmentError("SV9 scoring policy version mismatch")
    if (
        output.get("tile_contract_registry_fingerprint")
        != SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT
    ):
        raise Sv9AssessmentError("SV9 tile contract registry mismatch")
    if (
        not isinstance(output.get("tile_count"), int)
        or isinstance(output.get("tile_count"), bool)
        or output.get("tile_count") != 80
    ):
        raise Sv9AssessmentError("SV9 assessment output tile count mismatch")
    tiles = output.get("tiles")
    if not isinstance(tiles, list):
        raise Sv9AssessmentError("SV9 assessment output tiles must be an array")

    expected = build_sv9_assessment(tiles)
    try:
        actual_json = json.dumps(
            dict(output),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        expected_json = json.dumps(
            expected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise Sv9AssessmentError(
            "SV9 assessment output is not strict JSON"
        ) from exc
    if actual_json != expected_json:
        raise Sv9AssessmentError(
            "SV9 assessment output does not match its tile snapshot"
        )


def build_scanner_sv9_assessment(
    components: Mapping[str, Any],
) -> dict[str, Any]:
    """Adapt complete Scanner ``ComponentResult`` objects to the pure kernel.

    This is a shadow contract: legacy aggregate/store behavior is unchanged for
    incomplete scans.  Any technical status or malformed/incomplete tile
    profile returns an explicit unavailable object with a null score; it never
    fabricates an 80-tile zero vector.
    """

    if not isinstance(components, Mapping):
        return _scanner_unavailable(["components_not_mapping"])

    actual_keys = set(components)
    expected_keys = set(PRESENTATION_ORDER)
    reasons: list[str] = []
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    if missing:
        reasons.append(f"missing_components:{','.join(missing)}")
    if unexpected:
        reasons.append(f"unexpected_components:{','.join(unexpected)}")
    if reasons:
        return _scanner_unavailable(reasons)

    rows: list[dict[str, str]] = []
    for component_key in PRESENTATION_ORDER:
        component = components[component_key]
        if getattr(component, "component", None) != component_key:
            reasons.append(f"component_identity_mismatch:{component_key}")
            continue
        status = getattr(component, "status", None)
        if status != STATUS_SCORED:
            reasons.append(f"component_not_scored:{component_key}:{status or 'unknown'}")
            continue
        tile_profile = getattr(component, "tile_profile", None)
        if not isinstance(tile_profile, list):
            reasons.append(f"tile_profile_not_array:{component_key}")
            continue
        for verdict in tile_profile:
            rows.append(
                {
                    "component_key": component_key,
                    "tile_id": str(getattr(verdict, "tile_id", "") or ""),
                    "tile_key": (
                        f"{component_key}."
                        f"{str(getattr(verdict, 'tile_id', '') or '')}"
                    ),
                    "assessment_state": str(
                        getattr(verdict, "estado", "") or ""
                    ),
                }
            )
    if reasons:
        return _scanner_unavailable(reasons)

    try:
        assessment = build_sv9_assessment(rows)
    except Sv9AssessmentError as exc:
        return _scanner_unavailable([f"invalid_tile_profile:{exc}"])

    breakdown = {
        row["component_key"]: row
        for row in assessment["component_breakdown"]
    }
    for component_key in PRESENTATION_ORDER:
        score = getattr(components[component_key], "score", None)
        row = breakdown[component_key]
        allowed_scores = {row["raw_score"]}
        # ``aggregate`` historically mutates the capped Magnetism component to
        # its effective score while retaining the complete raw tile profile.
        if component_key == "magnetism" and assessment["magnetism_capped"]:
            allowed_scores.add(row["effective_score"])
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or score not in allowed_scores
        ):
            return _scanner_unavailable(
                [f"component_score_mismatch:{component_key}"]
            )

    return {
        **assessment,
        # Preserve the adapter schema at the outer boundary while exposing the
        # kernel schema explicitly.
        "schema_version": SV9_SCANNER_ASSESSMENT_VERSION,
        "assessment_schema_version": assessment["schema_version"],
        "expected_tile_count": 80,
        "availability": "available",
        "reason_codes": [],
    }


def validate_sv9_calculation(
    *,
    sv9_score: Any,
    component_breakdown: Any,
    base_average: Any,
    magnetism_capped: Any,
) -> None:
    """Validate score arithmetic by re-deriving it through this kernel.

    Persisted envelopes may not retain the original assessment vector.  State
    counts in their breakdown are sufficient to reconstruct a semantically
    equivalent vector for arithmetic validation because tile placement inside a
    component does not affect SV9 points.
    """

    if not isinstance(component_breakdown, list) or len(component_breakdown) != len(
        PRESENTATION_ORDER
    ):
        raise Sv9AssessmentError("component breakdown is incomplete")

    reconstructed: list[dict[str, str]] = []
    for position, component_key in enumerate(PRESENTATION_ORDER):
        raw = component_breakdown[position]
        if not isinstance(raw, Mapping) or set(raw) != _BREAKDOWN_FIELDS:
            raise Sv9AssessmentError("component breakdown fields mismatch")
        expected_tiles = [
            row for row in _REGISTRY if row["component_key"] == component_key
        ]
        tile_count = len(expected_tiles)
        multiplier = int(COMPONENTS[component_key]["multiplier"])
        integer_fields = (
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
        if any(
            not isinstance(raw.get(field), int)
            or isinstance(raw.get(field), bool)
            for field in integer_fields
        ):
            raise Sv9AssessmentError("component numeric fields must be exact integers")
        if (
            raw["component_key"] != component_key
            or raw["tile_count"] != tile_count
            or raw["multiplier"] != multiplier
            or raw["max_points"] != tile_count * multiplier
        ):
            raise Sv9AssessmentError("component breakdown metadata mismatch")
        counts = (
            raw["ok_count"],
            raw["no_count"],
            raw["sin_evidencia_count"],
        )
        if any(count < 0 for count in counts) or sum(counts) != tile_count:
            raise Sv9AssessmentError("component state counts are inconsistent")
        states = (
            [ESTADO_OK] * raw["ok_count"]
            + [ESTADO_NO] * raw["no_count"]
            + [ESTADO_SIN_EVIDENCIA] * raw["sin_evidencia_count"]
        )
        reconstructed.extend(
            {
                **contract,
                "assessment_state": state,
            }
            for contract, state in zip(expected_tiles, states)
        )

    expected = _calculate(_normalize_assessments(reconstructed))
    actual = {
        "sv9_score": sv9_score,
        "component_breakdown": component_breakdown,
        "base_average": base_average,
        "magnetism_capped": magnetism_capped,
    }
    try:
        actual_json = json.dumps(
            actual,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        expected_json = json.dumps(
            expected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise Sv9AssessmentError("SV9 calculation is not strict JSON") from exc
    if actual_json != expected_json:
        raise Sv9AssessmentError("SV9 calculation is inconsistent")


def _normalize_assessments(
    tile_assessments: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if not isinstance(tile_assessments, list) or len(tile_assessments) != len(
        _REGISTRY
    ):
        raise Sv9AssessmentError(
            "SV9 assessment requires exactly 80 tile assessments"
        )

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in tile_assessments:
        if not isinstance(raw, Mapping):
            raise Sv9AssessmentError("tile assessment must be an object")
        if set(raw) != _ASSESSMENT_FIELDS:
            raise Sv9AssessmentError("tile assessment fields mismatch")
        tile_id = raw.get("tile_id")
        if not isinstance(tile_id, str) or not tile_id:
            raise Sv9AssessmentError("tile_id must be non-empty text")
        contract = _REGISTRY_BY_ID.get(tile_id)
        if contract is None:
            raise Sv9AssessmentError(f"unknown assessment tile: {tile_id}")
        if tile_id in seen:
            raise Sv9AssessmentError(f"duplicate assessment tile: {tile_id}")
        seen.add(tile_id)

        component_key = raw.get("component_key")
        tile_key = raw.get("tile_key")
        if (
            component_key != contract["component_key"]
            or tile_key != contract["tile_key"]
        ):
            raise Sv9AssessmentError(
                f"assessment tile contract mismatch for {tile_id}"
            )
        state = raw.get("assessment_state")
        if state not in _ASSESSMENT_STATES:
            raise Sv9AssessmentError(
                f"invalid assessment state for {tile_id}"
            )
        normalized.append(
            {
                "component_key": contract["component_key"],
                "tile_id": tile_id,
                "tile_key": contract["tile_key"],
                "assessment_state": state,
            }
        )

    if seen != set(_REGISTRY_BY_ID):
        raise Sv9AssessmentError("SV9 assessment tile coverage is incomplete")
    normalized.sort(key=lambda row: _REGISTRY_ORDER[row["tile_id"]])
    return normalized


def _calculate(normalized: list[dict[str, str]]) -> dict[str, Any]:
    states_by_component: dict[str, list[str]] = {
        component_key: [] for component_key in PRESENTATION_ORDER
    }
    for tile in normalized:
        states_by_component[tile["component_key"]].append(
            tile["assessment_state"]
        )

    raw_scores = {
        component_key: states_by_component[component_key].count(ESTADO_OK)
        for component_key in PRESENTATION_ORDER
    }
    base_average = round(
        sum(
            raw_scores[component_key]
            * (10 / int(COMPONENTS[component_key]["scale"]))
            for component_key in BASE_COMPONENTS
        )
        / len(BASE_COMPONENTS),
        2,
    )
    magnetism_capped = (
        base_average < MAGNETISM_CAP_BASE_THRESHOLD
        and raw_scores["magnetism"] > MAGNETISM_CAP_VALUE
    )

    breakdown: list[dict[str, Any]] = []
    for component_key in PRESENTATION_ORDER:
        states = states_by_component[component_key]
        raw_score = raw_scores[component_key]
        effective_score = (
            MAGNETISM_CAP_VALUE
            if component_key == "magnetism" and magnetism_capped
            else raw_score
        )
        multiplier = int(COMPONENTS[component_key]["multiplier"])
        breakdown.append(
            {
                "component_key": component_key,
                "tile_count": len(states),
                "ok_count": states.count(ESTADO_OK),
                "no_count": states.count(ESTADO_NO),
                "sin_evidencia_count": states.count(ESTADO_SIN_EVIDENCIA),
                "raw_score": raw_score,
                "effective_score": effective_score,
                "multiplier": multiplier,
                "points": effective_score * multiplier,
                "max_points": len(states) * multiplier,
            }
        )
    return {
        "sv9_score": sum(row["points"] for row in breakdown),
        "component_breakdown": breakdown,
        "base_average": base_average,
        "magnetism_capped": magnetism_capped,
    }


def _scanner_unavailable(reason_codes: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SV9_SCANNER_ASSESSMENT_VERSION,
        "assessment_schema_version": SV9_ASSESSMENT_OUTPUT_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "assessment_vector_version": SV9_ASSESSMENT_VECTOR_VERSION,
        "scoring_policy_version": SV9_SCORING_POLICY_VERSION,
        "tile_contract_registry_fingerprint": (
            SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT
        ),
        "tile_count": 0,
        "expected_tile_count": 80,
        "tiles": None,
        "availability": "unavailable",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "sv9_score": None,
        "component_breakdown": None,
        "base_average": None,
        "magnetism_capped": None,
        "assessment_fingerprint": None,
        "score_fingerprint": None,
    }


__all__ = [
    "EXPECTED_RUBRIC_VERSION",
    "SV9_ASSESSMENT_FINGERPRINT_VERSION",
    "SV9_ASSESSMENT_OUTPUT_VERSION",
    "SV9_ASSESSMENT_VECTOR_VERSION",
    "SV9_SCANNER_ASSESSMENT_VERSION",
    "SV9_SCORE_FINGERPRINT_VERSION",
    "SV9_SCORING_POLICY_VERSION",
    "SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT",
    "SV9_TILE_CONTRACT_REGISTRY_VERSION",
    "Sv9AssessmentError",
    "build_scanner_sv9_assessment",
    "build_sv9_assessment",
    "build_sv9_tile_contract_registry",
    "validate_sv9_assessment_output",
    "validate_sv9_calculation",
]
