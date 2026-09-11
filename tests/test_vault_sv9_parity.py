from __future__ import annotations

from copy import deepcopy

import pytest

from src.history.capture_observation import parse_capture_observation
from src.services import evidence_vault_scan_orchestration
from src.services.scanner_report_assessment import (
    ScannerReportAssessmentError,
    assessment_projection_from_report,
)
from src.sv9.aggregator import aggregate
from src.sv9.assessment_kernel import validate_sv9_assessment_output
from src.sv9.models import (
    ComponentResult,
    ESTADO_NO,
    ESTADO_OK,
    STATUS_NOT_DETECTED,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    TileVerdict,
)
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER
from web import scan_runner


def _component(key: str, states: list[str]) -> ComponentResult:
    return ComponentResult(
        component=key,
        status=STATUS_SCORED,
        score=states.count(ESTADO_OK),
        tile_profile=[
            TileVerdict(
                tile_id=str(tile["id"]),
                estado=state,
                evidencia="quoted evidence" if state == ESTADO_OK else "",
                motivo="not demonstrated" if state != ESTADO_OK else "",
            )
            for tile, state in zip(COMPONENTS[key]["tiles"], states)
        ],
    )


def _components() -> dict[str, ComponentResult]:
    rows: dict[str, ComponentResult] = {}
    for component_key in PRESENTATION_ORDER:
        scale = int(COMPONENTS[component_key]["scale"])
        states = [ESTADO_NO] * scale
        if component_key == "mission":
            states[:3] = [ESTADO_OK] * 3
        elif component_key == "magnetism":
            states[:7] = [ESTADO_OK] * 7
        elif component_key == "coherencia":
            states[:4] = [ESTADO_OK] * 4
        rows[component_key] = _component(component_key, states)
    return rows


def _vision_sentinel_result() -> dict:
    components = _components()
    components["vision"] = ComponentResult(component="vision", status=STATUS_NOT_DETECTED)
    for key, score in {
        "mission": 5, "values": 5, "attributes": 5, "value_proposition": 10,
        "personality": 10, "brand_idea": 10, "core_purpose": 10,
        "magnetism": 8, "coherencia": 1,
    }.items():
        states = [ESTADO_OK] * score + [ESTADO_NO] * (int(COMPONENTS[key]["scale"]) - score)
        components[key] = _component(key, states)
    return aggregate(components, brand_name="Example", url="https://example.com").to_dict()


def _vision_sentinel_report(scan_id: str) -> dict:
    return scan_runner._compose_report(
        scan_id, "https://example.com", "Example", _flow_payload(_vision_sentinel_result())
    )


def _snapshot(scan_id: str) -> dict:
    return {
        "run": {
            "brand_name": "Example",
            "id": 123,
            "url": "https://example.com",
        },
        "raw_inputs": [
            {
                "source": "web",
                "payload": {
                    "url": "https://example.com",
                    "content": "Exact persisted capture text.",
                },
            }
        ],
        "acquisition_steps": {
            "web": {"source": "web", "status": "success"},
            "exa": {"source": "exa", "status": "success"},
        },
        "features": [],
        "acquisition_gate": {"state": "pass"},
        "source_capture": {
            "source_scan_id": scan_id,
            "observation_hash": "1" * 64,
            "capture_hash": "2" * 64,
        },
    }


def _flow_payload(result: dict) -> dict:
    return {
        "schema_version": "sv9-flow-sv9-shadow-eval-v1",
        "source_run_id": 123,
        "brand_name": "Example",
        "url": "https://example.com",
        "flow": {
            "candidate": {
                "interpretation": {"blocks": {}, "evidence_refs": {}},
                "evidence_pack": {"evidence": []},
                "limitations": [],
            },
            "interpretation_debug": {"evidence_coverage": {}},
        },
        "sv9": {
            "brand3_score": result["brand3_score"],
            "base_average": result["base_average"],
            "magnetism_capped": result["magnetism_capped"],
            "reliability_status": result["reliability_status"],
            "not_detected": result["not_detected"],
            "not_evaluated": result["not_evaluated"],
            "components": result["components"],
            "assessment": result["assessment"],
            "assessment_fingerprint": result["assessment_fingerprint"],
            "score_fingerprint": result["score_fingerprint"],
            "result": result,
        },
    }


def _strict_output(assessment: dict) -> dict:
    return {
        "schema_version": assessment["assessment_schema_version"],
        "rubric_version": assessment["rubric_version"],
        "assessment_vector_version": assessment["assessment_vector_version"],
        "scoring_policy_version": assessment["scoring_policy_version"],
        "tile_contract_registry_fingerprint": assessment[
            "tile_contract_registry_fingerprint"
        ],
        "tile_count": assessment["tile_count"],
        "tiles": assessment["tiles"],
        "sv9_score": assessment["sv9_score"],
        "component_breakdown": assessment["component_breakdown"],
        "base_average": assessment["base_average"],
        "magnetism_capped": assessment["magnetism_capped"],
        "assessment_fingerprint": assessment["assessment_fingerprint"],
        "score_fingerprint": assessment["score_fingerprint"],
    }


def _status(scan_id: str) -> dict:
    return {
        "id": scan_id,
        "url": "https://example.com",
        "brand_name": "Example",
        "state": "running",
        "phase": "capture",
        "phases": [
            {"key": key, "state": "pending"}
            for key in ("capture", "interpret", "score", "report")
        ],
        "acquisition": [],
        "acquisition_gate": {},
        "allow_degraded_fallback": False,
        "error": None,
        "error_code": None,
        "started_at": "2026-08-21T00:00:00+00:00",
        "completed_at": None,
    }


def test_aggregate_serializes_exact_available_assessment() -> None:
    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()

    assessment = result["assessment"]

    assert assessment["availability"] == "available"
    validate_sv9_assessment_output(_strict_output(assessment))
    assert result["brand3_score"] == assessment["sv9_score"]
    assert result["base_average"] == assessment["base_average"]
    assert result["magnetism_capped"] is assessment["magnetism_capped"]
    assert result["assessment_fingerprint"] == assessment["assessment_fingerprint"]
    assert result["score_fingerprint"] == assessment["score_fingerprint"]


def test_flow_summary_adapter_publishes_real_available_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production Flow adapter must retain the complete assessment projection."""

    from scripts import sv9_flow_sv9_shadow_eval as flow_eval
    from src.services.scanner_report_assessment import validate_report_sv9_assessment
    from src.sv9_flow.contracts import (
        BrandEvidencePack,
        BrandInterpretation,
        Sv9FlowCandidate,
    )

    scan_result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    )
    result = scan_result.to_dict()
    candidate = Sv9FlowCandidate(
        evidence_pack=BrandEvidencePack(
            brand_name="Example",
            url="https://example.com",
        ),
        interpretation=BrandInterpretation(
            brand_name="Example",
            url="https://example.com",
        ),
    )
    monkeypatch.setattr(
        flow_eval,
        "build_flow_candidate",
        lambda **_kwargs: (candidate, {"evidence_coverage": {}}),
    )

    payload = flow_eval.build_flow_sv9_shadow_eval(
        {"run": {"id": 123, "brand_name": "Example", "url": "https://example.com"}},
        include_full=True,
        interpretation_llm=object(),
        adjudicator_llm=object(),
        labeling_llm=object(),
        evaluator_llm=object(),
        reasoning_llm=object(),
        sv9_runner=lambda *_args, **_kwargs: scan_result,
        visual_evidence_fn=lambda _snapshot: None,
    )

    report = scan_runner._compose_report(
        "flow-summary-adapter",
        "https://example.com",
        "Example",
        payload,
    )
    projection = validate_report_sv9_assessment(report, required=True)

    assert projection["availability"] == "available"
    assert report["score"] == result["brand3_score"]
    assert report["base_average"] == result["base_average"]
    assert report["magnetism_capped"] is result["magnetism_capped"]
    assert report["assessment_fingerprint"] == result["assessment_fingerprint"]
    assert report["score_fingerprint"] == result["score_fingerprint"]
    assert report["sv9_assessment"] == result["assessment"]
    for component_key in PRESENTATION_ORDER:
        summary = payload["sv9"]["components"][component_key]
        component = result["components"][component_key]
        assert summary["scale"] == component["scale"]
        assert summary["blind_spot_count"] == component["blind_spot_count"]
        assert summary["tile_profile"] == component["tile_profile"]


@pytest.mark.parametrize(
    ("classification", "publishable"),
    [("candidate", True), ("evaluation_drift", True),
     ("acquisition_regression", False), ("comparison_error", False),
     ("contract_mismatch", False), ("invalid", False)],
)
def test_v2_sentinel_survives_report_store_api_view_and_history_diagnostics(
    tmp_path, monkeypatch: pytest.MonkeyPatch, classification: str, publishable: bool,
) -> None:
    from src.services.scanner_score_publication import score_publication_from_report
    from web import report_store
    from web.api_v1.presenters import result_payload
    from web.report_view_model import build_report_view_model

    result = _vision_sentinel_result()
    report = _vision_sentinel_report("sentinel-73")
    report["stability"] = {"classification": classification}
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.save_report(report)
    loaded = report_store.load_report("sentinel-73")

    assert loaded is not None
    projection = assessment_projection_from_report(loaded)
    publication = score_publication_from_report(loaded)
    assert result["brand3_score"] == projection["sv9_score"] == 73
    assert projection["component_projections"]["vision"]["status"] == "not_detected"
    expected = 73 if publishable else None
    assert publication["publishable"] is publishable
    assert publication["classification"] == classification
    assert publication["value"] == result_payload(loaded)["score"]["value"] == expected
    assert build_report_view_model(loaded)["score_publication"]["value"] == expected
    components = dict(reversed(_components().items()))
    components.update({key: ComponentResult(component=key, status=STATUS_NOT_DETECTED) for key in ("mission", "vision")})
    multi = aggregate(components, brand_name="Example", url="https://example.com").to_dict()
    assert multi["not_detected"] == ["mission", "vision"]
    assert assessment_projection_from_report(scan_runner._compose_report("multi", "https://example.com", "Example", _flow_payload(multi)))["availability"] == "available"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.pop("component_sentinels"),
        lambda a: a["component_sentinels"].append(deepcopy(a["component_sentinels"][0])),
        lambda a: a["component_sentinels"][0].update(component_key="unknown"),
        lambda a: a["component_sentinels"].append({
            **deepcopy(a["component_sentinels"][0]), "component_key": "mission",
        }),
        lambda a: a["component_sentinels"][0].update(status="scored"),
        lambda a: a["component_sentinels"][0].update(scale=6),
        lambda a: a["component_sentinels"][0].update(score=1),
        lambda a: a["component_sentinels"][0]["tile_profile"].append({}),
        lambda a: a["component_breakdown"][1].update(points=1),
        lambda a: a.update(assessment_fingerprint="0" * 64),
        lambda a: a["component_sentinels"][0].update(score=False),
        lambda a: a.update(availability="unavailable"),
    ],
)
def test_v2_sentinel_tampering_fails_closed(mutate) -> None:
    report = _vision_sentinel_report("sentinel-tamper")
    assessment = deepcopy(report["sv9_assessment"])
    mutate(assessment)
    report["sv9_assessment"] = deepcopy(assessment)
    report["raw"]["sv9"]["assessment"] = deepcopy(assessment)
    report["raw"]["sv9"]["result"]["assessment"] = deepcopy(assessment)
    with pytest.raises(ScannerReportAssessmentError):
        assessment_projection_from_report(report)


def test_v2_sentinel_divergent_duplicate_fails_closed() -> None:
    report = _vision_sentinel_report("sentinel-duplicate")
    report["raw"]["sv9"]["assessment"]["component_sentinels"][0]["scale"] = 6
    with pytest.raises(ScannerReportAssessmentError, match="duplicate_mismatch"):
        assessment_projection_from_report(report)


@pytest.mark.parametrize("tamper", ["component_status", "not_detected_alias"])
def test_v2_report_alias_tampering_fails_closed(tamper: str) -> None:
    report = _vision_sentinel_report("sentinel-alias")
    if tamper == "component_status":
        next(row for row in report["components"] if row["key"] == "vision")["status"] = "scored"
    else:
        report["raw"]["sv9"]["not_detected"] = []
    with pytest.raises(ScannerReportAssessmentError):
        assessment_projection_from_report(report)


def test_incomplete_aggregate_exposes_unavailable_assessment_without_zero_tiles() -> None:
    components = _components()
    components["vision"] = ComponentResult(
        component="vision",
        status=STATUS_NOT_EVALUATED,
        error="timeout",
    )

    result = aggregate(
        components,
        brand_name="Example",
        url="https://example.com",
    ).to_dict()

    assert result["assessment"] == {
        **result["assessment"],
        "availability": "unavailable",
        "tile_count": 0,
        "tiles": None,
        "sv9_score": None,
        "assessment_fingerprint": None,
        "score_fingerprint": None,
    }
    assert "component_not_scored:vision:not_evaluated" in result["assessment"][
        "reason_codes"
    ]
    assert result["brand3_score"] > 0

    report = scan_runner._compose_report(
        "incomplete-assessment",
        "https://example.com",
        "Example",
        _flow_payload(result),
    )
    assert report["score"] == result["brand3_score"]
    assert report["base_average"] == result["base_average"]
    assert report["sv9_assessment"] == result["assessment"]


def test_report_rejects_score_projection_that_disagrees_with_assessment() -> None:
    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    payload = _flow_payload(result)
    payload["sv9"]["brand3_score"] = int(payload["sv9"]["brand3_score"]) + 1

    with pytest.raises(
        RuntimeError,
        match="sv9_assessment_score_projection_mismatch",
    ):
        scan_runner._compose_report(
            "assessment-mismatch",
            "https://example.com",
            "Example",
            payload,
        )


def test_unavailable_assessment_still_rejects_legacy_score_projection_drift() -> None:
    components = _components()
    components["vision"] = ComponentResult(
        component="vision",
        status=STATUS_NOT_EVALUATED,
        error="timeout",
    )
    result = aggregate(
        components,
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    payload = _flow_payload(result)
    payload["sv9"]["result"]["brand3_score"] = (
        int(payload["sv9"]["result"]["brand3_score"]) + 1
    )

    with pytest.raises(
        RuntimeError,
        match="sv9_assessment_score_projection_mismatch",
    ):
        scan_runner._compose_report(
            "unavailable-assessment-mismatch",
            "https://example.com",
            "Example",
            payload,
        )


def _available_report(scan_id: str = "assessment-downgrade") -> dict:
    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    return scan_runner._compose_report(
        scan_id,
        "https://example.com",
        "Example",
        _flow_payload(result),
    )


@pytest.mark.parametrize("envelope", [None, [], "invalid", 7])
def test_present_non_mapping_assessment_envelope_fails_projection_and_publication(
    envelope: object,
) -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )
    from src.services.scanner_score_publication import score_publication_from_report

    report = _available_report("present-invalid-assessment")
    report["sv9_assessment"] = envelope

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_envelope_invalid",
    ):
        assessment_projection_from_report(report)

    publication = score_publication_from_report(report)
    assert publication["publishable"] is False
    assert publication["value"] is None
    assert publication["availability"] == "invalid"


def test_removed_assessment_envelope_with_top_level_fingerprints_fails_closed() -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )
    from src.services.scanner_score_publication import score_publication_from_report

    report = _available_report("missing-top-level-assessment")
    report.pop("sv9_assessment")
    report["raw"] = {}

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_envelope_missing",
    ):
        assessment_projection_from_report(report)

    publication = score_publication_from_report(report)
    assert publication["publishable"] is False
    assert publication["value"] is None
    assert publication["availability"] == "invalid"


@pytest.mark.parametrize(
    "raw",
    [
        {"sv9": {"assessment": {"availability": "available"}}},
        {"sv9": {"score_fingerprint": "stale"}},
        {"sv9": {"result": {"assessment": {"availability": "available"}}}},
        {"flow": {"assessment_fingerprint": "stale"}},
    ],
)
def test_removed_assessment_envelope_with_nested_flow_markers_fails_closed(
    raw: dict,
) -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )
    from src.services.scanner_score_publication import score_publication_from_report

    report = _available_report("missing-nested-assessment")
    report.pop("sv9_assessment")
    report.pop("assessment_fingerprint")
    report.pop("score_fingerprint")
    report["raw"] = raw

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_envelope_missing",
    ):
        assessment_projection_from_report(report)

    publication = score_publication_from_report(report)
    assert publication["publishable"] is False
    assert publication["value"] is None
    assert publication["availability"] == "invalid"


def test_true_legacy_report_remains_compatible_without_assessment_markers() -> None:
    from src.services.scanner_report_assessment import assessment_projection_from_report
    from src.services.scanner_score_publication import score_publication_from_report

    report = {
        "id": "true-legacy",
        "brand_name": "Example",
        "url": "https://example.com",
        "score": 99,
        "base_average": 9.9,
        "magnetism_capped": False,
        "components": [],
        "raw": {"sv9": {"result": {"brand3_score": 99}}},
    }

    projection = assessment_projection_from_report(report)
    publication = score_publication_from_report(report)
    assert projection["availability"] == "legacy"
    assert publication["publishable"] is True
    assert publication["value"] == 99
    assert publication["assessment_fingerprint"] is None
    assert publication["score_fingerprint"] is None


def test_explicit_unavailable_assessment_remains_nonpublishable() -> None:
    from src.services.scanner_score_publication import score_publication_from_report

    components = _components()
    components["vision"] = ComponentResult(
        component="vision",
        status=STATUS_NOT_EVALUATED,
        error="timeout",
    )
    result = aggregate(
        components,
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    report = scan_runner._compose_report(
        "unavailable-publication",
        "https://example.com",
        "Example",
        _flow_payload(result),
    )

    publication = score_publication_from_report(report)
    assert publication["availability"] == "unavailable"
    assert publication["publishable"] is False
    assert publication["value"] is None
    assert publication["raw_value"] == report["score"]
    assert publication["assessment_fingerprint"] is None
    assert publication["score_fingerprint"] is None


def test_available_report_exposes_canonical_component_projection() -> None:
    from src.services.scanner_report_assessment import assessment_projection_from_report

    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    report = scan_runner._compose_report(
        "component-projection",
        "https://example.com",
        "Example",
        _flow_payload(result),
    )

    projection = assessment_projection_from_report(report)
    mission = projection["component_projections"]["mission"]
    breakdown = next(
        row
        for row in result["assessment"]["component_breakdown"]
        if row["component_key"] == "mission"
    )

    assert mission == {
        "status": "scored",
        "score": breakdown["effective_score"],
        "points": breakdown["points"],
        "scale": breakdown["tile_count"],
        "lit": breakdown["ok_count"],
        "off": breakdown["no_count"],
        "blind": breakdown["sin_evidencia_count"],
        "tile_count": breakdown["tile_count"],
        "tile_profile": [
            {"id": tile["tile_id"], "estado": tile["assessment_state"]}
            for tile in result["assessment"]["tiles"]
            if tile["component_key"] == "mission"
        ],
    }


def test_available_report_rejects_duplicate_component_keys() -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )

    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    report = scan_runner._compose_report(
        "duplicate-component",
        "https://example.com",
        "Example",
        _flow_payload(result),
    )
    report["components"].append(deepcopy(report["components"][0]))

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_component_duplicate:report",
    ):
        assessment_projection_from_report(report)


@pytest.mark.parametrize("field", ["scale", "lit", "off", "blind"])
def test_available_report_rejects_tampered_component_identity_field(field: str) -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )

    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    report = scan_runner._compose_report(
        f"tampered-component-{field}",
        "https://example.com",
        "Example",
        _flow_payload(result),
    )
    report["components"][0][field] += 1

    with pytest.raises(
        ScannerReportAssessmentError,
        match=f"sv9_assessment_component_{field}_mismatch:report",
    ):
        assessment_projection_from_report(report)


@pytest.mark.parametrize(
    "field",
    [
        "sin_evidencia_count",
        "ok_count",
        "no_count",
        "raw_score",
        "effective_score",
        "multiplier",
        "points",
        "max_points",
    ],
)
def test_available_report_rejects_boolean_assessment_breakdown_alias(field: str) -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )

    report = _available_report(f"boolean-breakdown-{field}")
    report["sv9_assessment"]["component_breakdown"][0][field] = False

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_numeric_type_invalid",
    ):
        assessment_projection_from_report(report)


@pytest.mark.parametrize(
    "field", ["expected_tile_count", "tile_count", "sv9_score", "base_average"]
)
def test_available_report_rejects_boolean_envelope_numeric_alias(field: str) -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )

    report = _available_report(f"boolean-envelope-{field}")
    report["sv9_assessment"][field] = False

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_numeric_type_invalid|sv9_assessment_.*mismatch",
    ):
        assessment_projection_from_report(report)


@pytest.mark.parametrize("field", ["score", "points", "scale", "lit", "off", "blind"])
def test_available_report_rejects_boolean_component_alias(field: str) -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )

    report = _available_report(f"boolean-component-{field}")
    report["components"][0][field] = False

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_component_.*_mismatch:report",
    ):
        assessment_projection_from_report(report)


def test_available_report_rejects_type_drift_in_duplicate_assessment_mapping() -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )
    from src.services.scanner_score_publication import score_publication_from_report

    report = _available_report("duplicate-assessment-type-drift")
    report["raw"]["sv9"]["assessment"]["component_breakdown"][0][
        "sin_evidencia_count"
    ] = False

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_duplicate_mismatch",
    ):
        assessment_projection_from_report(report)

    publication = score_publication_from_report(report)
    assert publication["availability"] == "invalid"
    assert publication["publishable"] is False
    assert publication["value"] is None


def test_available_report_accepts_canonical_numeric_zero_and_one_aliases() -> None:
    from src.services.scanner_report_assessment import assessment_projection_from_report

    report = _available_report("numeric-aliases")
    component = report["components"][0]
    for field in ("score", "points", "scale", "lit", "off", "blind"):
        component[field] = int(component[field])

    projection = assessment_projection_from_report(report)
    assert projection["availability"] == "available"


def test_report_publication_rejects_boolean_component_alias() -> None:
    from src.services.scanner_score_publication import score_publication_from_report

    report = _available_report("boolean-publication")
    report["components"][0]["lit"] = False

    publication = score_publication_from_report(report)
    assert publication["availability"] == "invalid"
    assert publication["publishable"] is False
    assert publication["value"] is None


def test_legacy_evidence_metadata_named_assessment_remains_legacy() -> None:
    from src.services.scanner_report_assessment import assessment_projection_from_report
    from src.services.scanner_score_publication import score_publication_from_report

    report = {
        "id": "legacy-evidence-assessment-metadata",
        "brand_name": "Example",
        "url": "https://example.com",
        "score": 99,
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [
                            {
                                "metadata": {
                                    "assessment": "claim-level annotation",
                                    "assessment_fingerprint": "not-an-sv9-id",
                                }
                            }
                        ]
                    }
                }
            }
        },
    }

    projection = assessment_projection_from_report(report)
    publication = score_publication_from_report(report)
    assert projection["availability"] == "legacy"
    assert publication["publishable"] is True
    assert publication["value"] == 99


def test_available_report_accepts_identical_tile_alias_and_alias_absence() -> None:
    from src.services.scanner_report_assessment import assessment_projection_from_report

    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    report = scan_runner._compose_report(
        "tile-alias-compatible",
        "https://example.com",
        "Example",
        _flow_payload(result),
    )

    projection = assessment_projection_from_report(report)
    assert projection["availability"] == "available"

    without_alias = deepcopy(report)
    for component in without_alias["components"]:
        component.pop("tiles", None)
    without_alias_projection = assessment_projection_from_report(without_alias)
    assert without_alias_projection["assessment_fingerprint"] == projection[
        "assessment_fingerprint"
    ]
    assert without_alias_projection["score_fingerprint"] == projection[
        "score_fingerprint"
    ]


def test_available_report_rejects_tampered_tile_alias_state() -> None:
    from src.services.scanner_report_assessment import (
        ScannerReportAssessmentError,
        assessment_projection_from_report,
    )

    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    report = scan_runner._compose_report(
        "tile-alias-tampered",
        "https://example.com",
        "Example",
        _flow_payload(result),
    )
    mission = next(component for component in report["components"] if component["key"] == "mission")
    m4 = next(tile for tile in mission["tiles"] if tile["id"] == "M4")
    m4["estado"] = "sin_evidencia"

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_report_tiles_alias_state_mismatch:mission:M4",
    ):
        assessment_projection_from_report(report)


def test_available_report_preserves_raw_english_tile_text_through_persistence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.scanner_report_assessment import assessment_projection_from_report
    from web import report_store
    from web.app import _sanitize_report_language

    english_motivo = (
        "The snapshot does not provide access to the full product interface "
        "and customer evidence"
    )
    components = _components()
    components["mission"].tile_profile[3].motivo = english_motivo
    result = aggregate(
        components,
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    report = scan_runner._compose_report(
        "english-tile-text",
        "https://example.com",
        "Example",
        _flow_payload(result),
    )

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.save_report(report)
    loaded = report_store.load_report("english-tile-text")
    assert loaded is not None
    assessment_projection_from_report(loaded)

    raw_mission = next(row for row in loaded["components"] if row["key"] == "mission")
    raw_profile_m4 = next(row for row in raw_mission["tile_profile"] if row["id"] == "M4")
    raw_alias_m4 = next(row for row in raw_mission["tiles"] if row["id"] == "M4")
    assert raw_profile_m4["motivo"] == english_motivo
    assert raw_alias_m4["motivo"] == english_motivo

    presentation = _sanitize_report_language(loaded)
    presentation_mission = next(
        row for row in presentation["components"] if row["key"] == "mission"
    )
    presentation_profile_m4 = next(
        row for row in presentation_mission["tile_profile"] if row["id"] == "M4"
    )
    presentation_alias_m4 = next(
        row for row in presentation_mission["tiles"] if row["id"] == "M4"
    )
    assert raw_profile_m4["motivo"] == english_motivo
    assert raw_alias_m4["motivo"] == english_motivo
    assert presentation_profile_m4["motivo"] == presentation_alias_m4["motivo"]
    assert presentation_profile_m4["motivo"] != english_motivo


def test_vault_report_uses_fresh_persisted_capture_not_public_history_or_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval
    from src import config
    from src.services import evidence_vault_incremental_executor
    from web import report_store

    scan_id = "vault-parity-fresh-capture"
    snapshot = _snapshot(scan_id)
    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    payload = _flow_payload(result)
    prior_report = {
        "id": "public-history",
        "url": "https://example.com",
        "score": 99,
        "components": [
            {
                "key": "mission",
                "score": 5,
                "tile_profile": [
                    {"id": "M1", "estado": "ok"},
                    {"id": "M2", "estado": "no"},
                ],
            }
        ],
    }
    published: list[dict] = []
    flow_envelopes: list[dict] = []
    history_reads = 0

    class SidecarRepository:
        def get_evidence_vault_operational_memory(self, *_a, **_k):
            raise AssertionError("operational memory must not supply scanner output")

        def get_or_create_evidence_vault_operational_score_evaluation(
            self,
            *_a,
            **_k,
        ):
            raise AssertionError("operational score must not supply scanner output")

        def activate_evidence_vault_operational_scanner_result(self, *_a, **_k):
            raise RuntimeError("diagnostic sidecar unavailable")

    scan_runner._SCANS[scan_id] = _status(scan_id)
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        False,
    )
    monkeypatch.setattr(scan_runner, "_capture_snapshot", lambda *_a: snapshot)
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda _value: None)
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: SidecarRepository())
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **kwargs: {
            "capture_persisted": True,
            "mode": "incremental",
            "operation_plan": {
                "operation_plan_fingerprint": "a" * 64,
                "operations": {"llm_required": False},
            },
            "report_observation": (
                evidence_vault_scan_orchestration.build_capture_observation_from_snapshot(
                    snapshot=kwargs["snapshot"],
                    scan_id=scan_id,
                    url="https://example.com",
                    brand_name="Example",
                    mode="incremental",
                    observed_at="2026-08-21T00:00:00+00:00",
                )
            ),
        },
    )
    monkeypatch.setattr(
        evidence_vault_incremental_executor,
        "execute_vault_operation_plan",
        lambda **_k: {"execution_status": "completed"},
    )

    def canonical_flow(envelope: dict, **_k) -> dict:
        flow_envelopes.append(deepcopy(envelope))
        return deepcopy(payload)

    monkeypatch.setattr(
        flow_eval,
        "build_flow_sv9_shadow_eval",
        canonical_flow,
    )
    monkeypatch.setattr(
        scan_runner,
        "_publish_completed_report",
        lambda _scan_id, report, **_kwargs: published.append(deepcopy(report)) or True,
    )
    def prior_history(_url: str) -> list[dict]:
        nonlocal history_reads
        history_reads += 1
        return [prior_report]

    monkeypatch.setattr(scan_runner, "list_reports_for_domain", prior_history)

    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
        sidecar = deepcopy(scan_runner._SCANS[scan_id]["vault"])
    finally:
        scan_runner._SCANS.pop(scan_id, None)
        scan_runner._SCAN_EVENTS.pop(scan_id, None)
        scan_runner._VAULT_ACTIVATIONS.discard(scan_id)

    assert len(published) == 1
    # History is still a post-composition stability sidecar.  It may choose a
    # display identity, but it cannot rewrite this fresh report's tile vector.
    assert history_reads == 1
    assert sidecar["state"] == "failed"
    assert sidecar["role"] == "diagnostic_sidecar"
    report = published[0]
    assessment = result["assessment"]
    assert report["score"] == result["brand3_score"] == assessment["sv9_score"]
    assert report["base_average"] == assessment["base_average"]
    assert report["sv9_assessment"] == assessment
    assert report["raw"]["sv9"]["assessment"] == assessment
    assert report["raw"]["sv9"]["result"]["assessment"] == assessment
    parsed = parse_capture_observation(
        evidence_vault_scan_orchestration.build_capture_observation_from_snapshot(
            snapshot=snapshot,
            scan_id=scan_id,
            url="https://example.com",
            brand_name="Example",
            mode="incremental",
            observed_at="2026-08-21T00:00:00+00:00",
        )
    )
    assert report["raw"]["source_capture"] == {
        "source_scan_id": scan_id,
        "observation_hash": parsed.observation_hash,
        "capture_hash": parsed.capture_hash,
    }
    assert flow_envelopes == [
        {
            "snapshot": parsed.capture_payload,
            "source_run_id": parsed.capture_payload["run"]["id"],
        }
    ]
    assert report["magnetism_capped"] is assessment["magnetism_capped"]
    assert report["assessment_fingerprint"] == assessment["assessment_fingerprint"]
    assert report["score_fingerprint"] == assessment["score_fingerprint"]
    by_component = {row["key"]: row for row in report["components"]}
    for breakdown in assessment["component_breakdown"]:
        component = by_component[breakdown["component_key"]]
        assert component["score"] == breakdown["effective_score"]
        assert component["points"] == breakdown["points"]
    assert report["raw"]["sv9"]["result"]["components"] == result["components"]
    assert scan_runner._SCANS.get(scan_id) is None


def test_vault_operational_planning_failure_does_not_gate_persisted_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval
    from src import config
    from web import report_store

    scan_id = "vault-parity-planning-failure"
    snapshot = _snapshot(scan_id)
    result = aggregate(
        _components(),
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    published: list[dict] = []

    class Repository:
        def list_capture_observations_for_domain(self, *_a, **_k):
            return [
                {
                    "source_scan_id": scan_id,
                    "raw_observation": (
                        evidence_vault_scan_orchestration.build_capture_observation_from_snapshot(
                            snapshot=snapshot,
                            scan_id=scan_id,
                            url="https://example.com",
                            brand_name="Example",
                            mode="incremental",
                            observed_at="2026-08-21T00:00:00+00:00",
                        )
                    ),
                }
            ]

    scan_runner._SCANS[scan_id] = _status(scan_id)
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        False,
    )
    monkeypatch.setattr(scan_runner, "_capture_snapshot", lambda *_a: snapshot)
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda _value: None)
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("planner unavailable")),
    )
    monkeypatch.setattr(
        flow_eval,
        "build_flow_sv9_shadow_eval",
        lambda *_a, **_k: _flow_payload(deepcopy(result)),
    )
    monkeypatch.setattr(scan_runner, "_attach_evidence_stability", lambda report: report)
    monkeypatch.setattr(
        scan_runner,
        "_publish_completed_report",
        lambda _scan_id, report, **_kwargs: published.append(deepcopy(report)) or True,
    )

    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
        diagnostic = deepcopy(scan_runner._SCANS[scan_id]["vault"])
    finally:
        scan_runner._SCANS.pop(scan_id, None)
        scan_runner._SCAN_EVENTS.pop(scan_id, None)
        scan_runner._VAULT_ACTIVATIONS.discard(scan_id)

    assert len(published) == 1
    assert published[0]["score"] == result["brand3_score"]
    assert diagnostic == {
        "role": "diagnostic_sidecar",
        "state": "failed",
        "error_type": "RuntimeError",
        "error": "planner unavailable",
        "diagnostic": {
            "kind": "secondary_failure",
            "stage": "vault_sidecar",
            "capture_state": "completed",
            "reason_codes": ["vault_sidecar_failed"],
            "summary": "A non-authoritative Vault sidecar failed after scan processing.",
            "build_sha": "unknown",
        },
    }


def test_vault_capture_provenance_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval
    from src import config
    from web import report_store

    scan_id = "vault-parity-provenance-mismatch"
    snapshot = _snapshot(scan_id)
    stale_snapshot = deepcopy(snapshot)
    stale_snapshot["raw_inputs"][0]["payload"]["content"] = "Stale capture text."
    stale_observation = (
        evidence_vault_scan_orchestration.build_capture_observation_from_snapshot(
            snapshot=stale_snapshot,
            scan_id=scan_id,
            url="https://example.com",
            brand_name="Example",
            mode="incremental",
            observed_at="2026-08-21T00:00:00+00:00",
        )
    )
    flow_calls: list[dict] = []

    class Repository:
        def list_capture_observations_for_domain(self, *_a, **_k):
            return [
                {
                    "source_scan_id": scan_id,
                    "raw_observation": stale_observation,
                }
            ]

    scan_runner._SCANS[scan_id] = _status(scan_id)
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        False,
    )
    monkeypatch.setattr(scan_runner, "_capture_snapshot", lambda *_a: snapshot)
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda _value: None)
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())
    monkeypatch.setattr(
        evidence_vault_scan_orchestration,
        "prepare_vault_scan_after_capture",
        lambda **_k: {
            "capture_persisted": True,
            "mode": "incremental",
            "operation_plan": None,
            "report_observation": stale_observation,
        },
    )
    monkeypatch.setattr(
        flow_eval,
        "build_flow_sv9_shadow_eval",
        lambda envelope, **_k: flow_calls.append(deepcopy(envelope)) or {},
    )

    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
        status = deepcopy(scan_runner._SCANS[scan_id])
    finally:
        scan_runner._SCANS.pop(scan_id, None)
        scan_runner._SCAN_EVENTS.pop(scan_id, None)

    assert flow_calls == []
    assert status["state"] == "error"
    assert "vault_persisted_capture_snapshot_mismatch" in status["error"]


def test_vault_trusted_capture_binding_authenticates_exact_worker_capture() -> None:
    scan_id = "vault-parity-trusted-capture"
    worker_snapshot = _snapshot(scan_id)
    worker_snapshot.pop("source_capture")
    worker_snapshot["raw_inputs"][0]["payload"]["content"] = (
        "Exact worker-persisted evidence."
    )
    raw_observation = (
        evidence_vault_scan_orchestration.build_capture_observation_from_snapshot(
            snapshot=worker_snapshot,
            scan_id=scan_id,
            url="https://example.com",
            brand_name="Example",
            mode="incremental",
            observed_at="2026-08-21T00:00:00+00:00",
        )
    )
    raw_observation["pipeline_version"] = (
        "evidence-vault-trusted-acquisition-v1"
    )
    parsed = parse_capture_observation(raw_observation)
    wrapper_snapshot = _snapshot(scan_id)
    wrapper_snapshot["run"]["id"] = 2**63 - 1
    wrapper_snapshot["run"]["brand_name"] = "Untrusted wrapper brand"
    wrapper_snapshot["raw_inputs"] = deepcopy(worker_snapshot["raw_inputs"])
    wrapper_snapshot["source_capture"] = {
        "source_scan_id": scan_id,
        "observation_hash": parsed.observation_hash,
        "capture_hash": parsed.capture_hash,
    }

    canonical_snapshot, source_capture = (
        scan_runner._canonical_snapshot_from_persisted_vault_capture(
            scan_id=scan_id,
            url="https://example.com",
            expected_snapshot=wrapper_snapshot,
            report_observation=raw_observation,
        )
    )

    assert source_capture == wrapper_snapshot["source_capture"]

    wrapper_snapshot["source_capture"]["capture_hash"] = "0" * 64
    with pytest.raises(
        RuntimeError,
        match="vault_persisted_capture_snapshot_mismatch",
    ):
        scan_runner._canonical_snapshot_from_persisted_vault_capture(
            scan_id=scan_id,
            url="https://example.com",
            expected_snapshot=wrapper_snapshot,
            report_observation=raw_observation,
        )


def test_vault_trusted_capture_projects_verified_documents_into_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from scripts import sv9_flow_sv9_shadow_eval as flow_eval
    from src import config
    from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot
    from tests.evidence_vault_c7_shadow_fixture import build_verified_raw_ready_fixture
    from web import report_store

    proof = build_verified_raw_ready_fixture().snapshot["proofs"][0]["acquisition"]
    raw_observation = deepcopy(proof["scan_run"]["request_payload"])
    scan_id = raw_observation["source_scan_id"]
    evidence_by_source = {
        row["source"]: row for row in raw_observation["evidence_records"]
    }
    documents = [
            SimpleNamespace(
                role=binding["channel_role"],
                source_url=binding["source_url"],
                extracted_document=binding["extracted_document"],
                extracted_document_sha256=binding["extracted_document_sha256"],
                extractor_version=binding["extractor_version"],
                receipt_fingerprint=evidence_by_source[binding["channel_role"]][
                    "metadata"
                ]["receipt_fingerprint"],
            )
            for binding in proof["evidence_bindings"]
        ]

    scan_runner._SCANS[scan_id] = _status(scan_id)
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda _value: None)
    provisional = parse_capture_observation(raw_observation)
    wrapper_snapshot = scan_runner._verified_raw_pre_analysis_snapshot(
        scan_id=scan_id,
        brand_name=provisional.brand_name,
        canonical_url=provisional.canonical_url,
        capture_content_hash=provisional.capture_hash,
        capture_observation_hash=provisional.observation_hash,
        documents=documents,
    )
    normalized_evidence = sorted(
        build_evidence_pack_from_snapshot(
            wrapper_snapshot,
            include_acquisition_steps=False,
        ).to_dict()["evidence"],
        key=lambda row: row["ref"],
    )
    raw_observation["evidence_records"] = deepcopy(normalized_evidence)
    parsed = parse_capture_observation(raw_observation)
    wrapper_snapshot["source_capture"] = {
        "source_scan_id": scan_id,
        "observation_hash": parsed.observation_hash,
        "capture_hash": parsed.capture_hash,
    }
    assert set(parsed.capture_payload) == {"evidence_vault_raw_provenance", "sources"}
    result = aggregate(
        _components(),
        brand_name=parsed.brand_name,
        url=parsed.canonical_url,
    ).to_dict()
    flow_calls: list[dict] = []
    published: list[dict] = []

    class Repository:
        def list_capture_observations_for_domain(self, *_a, **_k):
            return [
                {
                    "source_scan_id": scan_id,
                    "raw_observation": deepcopy(raw_observation),
                }
            ]

    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")
    monkeypatch.setattr(config, "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED", True)
    monkeypatch.setattr(scan_runner, "_capture_verified_raw_shadow", lambda **_kwargs: deepcopy(wrapper_snapshot))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())
    monkeypatch.setattr(
        evidence_vault_scan_orchestration, "prepare_vault_scan_after_capture",
        lambda **_kwargs: {
            "capture_persisted": True,
            "mode": "incremental",
            "operation_plan": None,
            "report_observation": deepcopy(raw_observation),
        },
    )

    def canonical_flow(envelope: dict, **_kwargs) -> dict:
        flow_calls.append(deepcopy(envelope))
        payload = _flow_payload(deepcopy(result))
        payload["source_run_id"] = envelope["source_run_id"]
        return payload

    monkeypatch.setattr(flow_eval, "build_flow_sv9_shadow_eval", canonical_flow)
    monkeypatch.setattr(scan_runner, "_attach_evidence_stability", lambda report: report)
    monkeypatch.setattr(scan_runner, "_publish_completed_report", lambda _scan_id, report, **_kwargs: published.append(deepcopy(report)) or True)

    try:
        scan_runner._run(scan_id, parsed.canonical_url, parsed.brand_name, False)
    finally:
        scan_runner._SCANS.pop(scan_id, None)
        scan_runner._SCAN_EVENTS.pop(scan_id, None)
        scan_runner._VAULT_ACTIVATIONS.discard(scan_id)

    assert len(flow_calls) == 1
    envelope = flow_calls[0]
    assert envelope["source_run_id"] == scan_runner._stable_verified_source_run_id(
        scan_id
    )
    assert envelope["snapshot"]["run"]["brand_name"] == parsed.brand_name
    assert envelope["snapshot"]["run"]["url"] == parsed.canonical_url
    assert "acquisition_steps" not in envelope["snapshot"]
    assert build_evidence_pack_from_snapshot(
        envelope["snapshot"],
        include_acquisition_steps=False,
    ).to_dict()["evidence"] == list(parsed.evidence_records)
    assert len(published) == 1
    assert published[0]["raw"]["source_run_id"] == envelope["source_run_id"]
    assert published[0]["raw"]["flow"]["candidate"]["evidence_pack"][
        "evidence"
    ] == list(parsed.evidence_records)

    tampered_wrapper = deepcopy(wrapper_snapshot)
    tampered_wrapper["raw_inputs"][0]["payload"]["content"] += " tampered"
    with pytest.raises(RuntimeError, match="vault_persisted_capture_evidence_mismatch"):
        scan_runner._canonical_snapshot_from_persisted_vault_capture(
            scan_id=scan_id,
            url=parsed.canonical_url,
            expected_snapshot=tampered_wrapper,
            report_observation=raw_observation,
        )
