from __future__ import annotations

from copy import deepcopy
import random

import pytest

from src.services.evidence_vault_canonical_scoring import (
    EvidenceVaultCanonicalScoringError,
    build_vault_sv9_assessment_from_tile_states,
    calculate_score_from_tile_states,
)
from src.sv9.aggregator import aggregate
from src.sv9.assessment_kernel import (
    SV9_ASSESSMENT_OUTPUT_VERSION,
    SV9_ASSESSMENT_VECTOR_VERSION,
    SV9_SCORING_POLICY_VERSION,
    SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT,
    Sv9AssessmentError,
    build_scanner_sv9_assessment,
    build_sv9_assessment,
    validate_sv9_assessment_output,
    validate_sv9_calculation,
)
from src.sv9.models import (
    ComponentResult,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    TileVerdict,
)
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER, RUBRIC_VERSION


def _rows(default: str = "no") -> list[dict[str, str]]:
    return [
        {
            "component_key": component_key,
            "tile_id": str(tile["id"]),
            "tile_key": f"{component_key}.{tile['id']}",
            "assessment_state": default,
        }
        for component_key in PRESENTATION_ORDER
        for tile in COMPONENTS[component_key]["tiles"]
    ]


def _components(rows: list[dict[str, str]]) -> dict[str, ComponentResult]:
    by_component: dict[str, list[TileVerdict]] = {
        component_key: [] for component_key in PRESENTATION_ORDER
    }
    for row in rows:
        by_component[row["component_key"]].append(
            TileVerdict(
                tile_id=row["tile_id"],
                estado=row["assessment_state"],
                evidencia="literal" if row["assessment_state"] == "ok" else "",
                motivo="reason" if row["assessment_state"] != "ok" else "",
            )
        )
    return {
        component_key: ComponentResult(
            component=component_key,
            status=STATUS_SCORED,
            score=sum(
                verdict.estado == "ok"
                for verdict in by_component[component_key]
            ),
            tile_profile=by_component[component_key],
        )
        for component_key in PRESENTATION_ORDER
    }


def test_kernel_golden_perfect_assessment() -> None:
    result = build_sv9_assessment(_rows("ok"))

    assert result["schema_version"] == SV9_ASSESSMENT_OUTPUT_VERSION
    assert result["rubric_version"] == RUBRIC_VERSION == "baldosas-v3-1"
    assert result["assessment_vector_version"] == SV9_ASSESSMENT_VECTOR_VERSION
    assert result["scoring_policy_version"] == SV9_SCORING_POLICY_VERSION
    assert result["tile_contract_registry_fingerprint"] == (
        SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT
    )
    assert SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT == (
        "572e9cf0222f6c8bc1d600eb0db66449bcf634ab7aa59b142ce55e2cbfa65abd"
    )
    assert result["tile_count"] == 80
    assert result["tiles"] == _rows("ok")
    assert result["sv9_score"] == 100
    assert result["base_average"] == 10
    assert result["magnetism_capped"] is False
    assert result["assessment_fingerprint"] == (
        "281fc64e2217be23ed0f75d092a95a98961b5a10cc901c8a904fc0671890e097"
    )
    assert result["score_fingerprint"] == (
        "a7542fd484fea47a00d4812b8844732b3c47098a49874771bd7b63a2f76cff80"
    )


def test_assessment_output_roundtrip_and_tampering_validation() -> None:
    result = build_sv9_assessment(_rows("ok"))
    validate_sv9_assessment_output(result)

    tampered_outputs = []

    changed_tile = deepcopy(result)
    changed_tile["tiles"][0]["assessment_state"] = "no"
    tampered_outputs.append(changed_tile)

    changed_score = deepcopy(result)
    changed_score["sv9_score"] -= 1
    tampered_outputs.append(changed_score)

    changed_breakdown = deepcopy(result)
    changed_breakdown["component_breakdown"][0]["points"] -= 1
    tampered_outputs.append(changed_breakdown)

    changed_assessment_fingerprint = deepcopy(result)
    changed_assessment_fingerprint["assessment_fingerprint"] = "0" * 64
    tampered_outputs.append(changed_assessment_fingerprint)

    changed_score_fingerprint = deepcopy(result)
    changed_score_fingerprint["score_fingerprint"] = "f" * 64
    tampered_outputs.append(changed_score_fingerprint)

    verification_mixed_into_assessment = deepcopy(result)
    verification_mixed_into_assessment["verification_state"] = "verified"
    tampered_outputs.append(verification_mixed_into_assessment)

    for tampered in tampered_outputs:
        with pytest.raises(Sv9AssessmentError):
            validate_sv9_assessment_output(tampered)


def test_c7_and_c8_are_ordinary_two_point_coherencia_tiles() -> None:
    for tile_id in ("C7", "C8"):
        rows = _rows("no")
        next(row for row in rows if row["tile_id"] == tile_id)[
            "assessment_state"
        ] = "ok"

        result = build_sv9_assessment(rows)
        coherencia = next(
            row
            for row in result["component_breakdown"]
            if row["component_key"] == "coherencia"
        )

        assert result["sv9_score"] == 2
        assert coherencia["raw_score"] == 1
        assert coherencia["points"] == 2
        assert all("verification" not in key for key in result)


def test_kernel_uses_canonical_order_for_both_fingerprints() -> None:
    rows = _rows()
    for index, row in enumerate(rows):
        row["assessment_state"] = ("ok", "no", "sin_evidencia")[index % 3]
    shuffled = deepcopy(rows)
    random.Random(20260813).shuffle(shuffled)

    first = build_sv9_assessment(rows)
    second = build_sv9_assessment(shuffled)

    assert second == first


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda rows: rows.pop(), "exactly 80"),
        (
            lambda rows: rows.__setitem__(-1, deepcopy(rows[0])),
            "duplicate assessment tile",
        ),
        (
            lambda rows: rows[0].update(
                {"tile_id": "UNKNOWN", "tile_key": "mission.UNKNOWN"}
            ),
            "unknown assessment tile",
        ),
        (
            lambda rows: rows[0].update({"component_key": "vision"}),
            "contract mismatch",
        ),
        (
            lambda rows: rows[0].update({"tile_key": "mission.wrong"}),
            "contract mismatch",
        ),
        (
            lambda rows: rows[0].update({"assessment_state": "contradiction"}),
            "invalid assessment state",
        ),
        (
            lambda rows: rows[0].update({"verification_state": "verified"}),
            "fields mismatch",
        ),
    ],
)
def test_kernel_rejects_incomplete_or_non_contract_inputs(mutate, match: str) -> None:
    rows = _rows()
    mutate(rows)

    with pytest.raises(Sv9AssessmentError, match=match):
        build_sv9_assessment(rows)


def test_no_and_sin_evidencia_are_numeric_zeros_but_semantically_distinct() -> None:
    no_result = build_sv9_assessment(_rows("no"))
    blind_result = build_sv9_assessment(_rows("sin_evidencia"))

    assert no_result["sv9_score"] == blind_result["sv9_score"] == 0
    assert no_result["assessment_fingerprint"] != blind_result["assessment_fingerprint"]
    assert no_result["score_fingerprint"] != blind_result["score_fingerprint"]
    assert no_result["component_breakdown"][0]["no_count"] == 5
    assert blind_result["component_breakdown"][0]["sin_evidencia_count"] == 5


def test_magnetism_cap_is_part_of_the_versioned_calculation() -> None:
    rows = _rows("no")
    for row in rows:
        if row["component_key"] in {"magnetism", "coherencia"}:
            row["assessment_state"] = "ok"

    result = build_sv9_assessment(rows)

    assert result["sv9_score"] == 30
    assert result["base_average"] == 0
    assert result["magnetism_capped"] is True
    magnetism = next(
        row
        for row in result["component_breakdown"]
        if row["component_key"] == "magnetism"
    )
    assert (magnetism["raw_score"], magnetism["effective_score"]) == (10, 5)


def test_scanner_adapter_is_available_only_for_exact_scored_profiles() -> None:
    components = _components(_rows("ok"))
    available = build_scanner_sv9_assessment(components)

    assert available["availability"] == "available"
    assert available["sv9_score"] == 100

    components["vision"] = ComponentResult(
        component="vision",
        status=STATUS_NOT_EVALUATED,
        error="timeout",
    )
    unavailable = build_scanner_sv9_assessment(components)

    assert unavailable["availability"] == "unavailable"
    assert unavailable["tile_count"] == 0
    assert unavailable["expected_tile_count"] == 80
    assert unavailable["tiles"] is None
    assert unavailable["sv9_score"] is None
    assert unavailable["assessment_fingerprint"] is None
    assert unavailable["score_fingerprint"] is None
    assert unavailable["component_breakdown"] is None
    assert unavailable["reason_codes"] == [
        "component_not_scored:vision:not_evaluated"
    ]


def test_scanner_adapter_fails_closed_on_missing_or_duplicate_profile_tiles() -> None:
    components = _components(_rows("ok"))
    components["mission"].tile_profile[-1] = deepcopy(
        components["mission"].tile_profile[0]
    )

    result = build_scanner_sv9_assessment(components)

    assert result["availability"] == "unavailable"
    assert result["sv9_score"] is None
    assert result["reason_codes"][0].startswith("invalid_tile_profile:")


def test_complete_scanner_aggregate_and_vault_have_kernel_parity() -> None:
    rows = _rows("no")
    for index, row in enumerate(rows):
        if index % 3 == 0 or row["component_key"] == "magnetism":
            row["assessment_state"] = "ok"
    expected = build_sv9_assessment(rows)
    components = _components(rows)

    scan = aggregate(components, brand_name="Acme", url="https://acme.test")
    scanner = build_scanner_sv9_assessment(scan.components)
    vault_rows = [
        {
            "component_key": row["component_key"],
            "tile_id": row["tile_id"],
            "tile_key": row["tile_key"],
            "state": row["assessment_state"],
        }
        for row in reversed(rows)
    ]
    vault = build_vault_sv9_assessment_from_tile_states(vault_rows)
    legacy_vault = calculate_score_from_tile_states(vault_rows)

    assert scanner["availability"] == "available"
    assert scan.brand3_score == scanner["sv9_score"] == expected["sv9_score"]
    assert scan.base_average == scanner["base_average"] == expected["base_average"]
    assert scan.magnetism_capped is scanner["magnetism_capped"]
    assert vault == expected
    for field in (
        "tiles",
        "assessment_fingerprint",
        "score_fingerprint",
        "tile_contract_registry_fingerprint",
        "component_breakdown",
        "base_average",
        "magnetism_capped",
        "sv9_score",
    ):
        assert scanner[field] == vault[field]
    assert legacy_vault == {
        "score": expected["sv9_score"],
        "component_breakdown": expected["component_breakdown"],
        "base_average": expected["base_average"],
        "magnetism_capped": expected["magnetism_capped"],
    }


def test_random_vectors_preserve_scanner_vault_score_parity() -> None:
    rng = random.Random(9917)
    for _ in range(64):
        rows = _rows()
        for row in rows:
            row["assessment_state"] = rng.choice(
                ("ok", "no", "sin_evidencia")
            )
        kernel = build_sv9_assessment(rows)
        scanner = build_scanner_sv9_assessment(_components(rows))
        vault = calculate_score_from_tile_states(
            [
                {
                    "component_key": row["component_key"],
                    "tile_id": row["tile_id"],
                    "tile_key": row["tile_key"],
                    "state": row["assessment_state"],
                }
                for row in rows
            ]
        )
        assert scanner["sv9_score"] == kernel["sv9_score"] == vault["score"]
        assert scanner["component_breakdown"] == vault["component_breakdown"]
        assert scanner["base_average"] == vault["base_average"]
        assert scanner["magnetism_capped"] is vault["magnetism_capped"]


def test_calculation_validator_rejects_tampering() -> None:
    result = build_sv9_assessment(_rows("ok"))
    validate_sv9_calculation(
        sv9_score=result["sv9_score"],
        component_breakdown=result["component_breakdown"],
        base_average=result["base_average"],
        magnetism_capped=result["magnetism_capped"],
    )

    with pytest.raises(Sv9AssessmentError, match="inconsistent"):
        validate_sv9_calculation(
            sv9_score=result["sv9_score"] - 1,
            component_breakdown=result["component_breakdown"],
            base_average=result["base_average"],
            magnetism_capped=result["magnetism_capped"],
        )


def test_vault_adapter_keeps_its_fail_closed_error_boundary() -> None:
    rows = _rows("ok")
    vault_rows = [
        {
            "component_key": row["component_key"],
            "tile_id": row["tile_id"],
            "tile_key": row["tile_key"],
            "state": row["assessment_state"],
        }
        for row in rows
    ]
    vault_rows[0]["state"] = "contradiction"

    with pytest.raises(
        EvidenceVaultCanonicalScoringError,
        match="invalid effective scoring state",
    ):
        calculate_score_from_tile_states(vault_rows)
