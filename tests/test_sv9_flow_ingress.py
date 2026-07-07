import pytest

from src.sv9.flow_ingress import (
    SV9_FLOW_INGRESS_VERSION,
    detection_blocks_from_flow_candidate,
    flow_candidate_extra_signals,
)
from src.sv9.rubric import COMPONENTS, tile_ids
from src.sv9.service import run_sv9_from_audit_snapshot
from src.sv9_flow.contracts import (
    BrandEvidencePack,
    BrandInterpretation,
    EvidenceRecord,
    Sv9FlowCandidate,
    TileSignal,
)


def _candidate(tile_signals: list[TileSignal] | None = None) -> Sv9FlowCandidate:
    return Sv9FlowCandidate(
        evidence_pack=BrandEvidencePack(
            brand_name="Acme",
            url="https://acme.example",
            evidence=[
                EvidenceRecord(
                    ref="raw_inputs.0.text",
                    source="homepage",
                    evidence_type="raw_input_text",
                    content="Acme helps finance teams close the books faster.",
                ),
                EvidenceRecord(
                    ref="features.0",
                    source="features",
                    evidence_type="feature_signal",
                    content="Tone is operational and precise.",
                ),
            ],
        ),
        interpretation=BrandInterpretation(
            brand_name="Acme",
            url="https://acme.example",
            blocks={
                "mission": {
                    "detected": True,
                    "content": "Help finance teams close faster.",
                    "confidence": "high",
                    "rationale": "The homepage states the operating outcome.",
                },
                "vision": {
                    "detected": False,
                    "content": "",
                    "confidence": "low",
                    "rationale": "No future-state evidence.",
                },
            },
            evidence_refs={
                "mission": ["raw_inputs.0.text", "features.0"],
                "vision": [],
            },
        ),
        tile_signals=list(tile_signals or []),
        limitations=["No direct community metrics were available."],
    )


class _TileLLM:
    api_key = "test-key"
    model = "tile-fake"

    def _call_json(self, system, user, **kwargs):
        ids = []
        schema_name = kwargs.get("schema_name")
        for key in COMPONENTS:
            if schema_name == f"baldosas_{key}":
                ids = tile_ids(key)
                break
        payload = {
            "baldosas": [
                {"id": tile_id, "estado": "ok", "evidencia": f"quote {tile_id}"}
                for tile_id in ids
            ]
        }
        if schema_name == "baldosas_coherencia":
            payload["veredicto"] = "La marca cuenta una historia única."
        return payload


class _ForbiddenExtractor:
    def __init__(self, *args, **kwargs):
        raise AssertionError("MagnetismExtractor must not run in the native flow path")


def test_detection_blocks_resolve_refs_to_citable_snippets() -> None:
    blocks = detection_blocks_from_flow_candidate(_candidate())

    assert blocks["mission"] == {
        "detected": True,
        "content": "Help finance teams close faster.",
        "confidence": "high",
        "mode": "sv9_flow",
        "rationale": "The homepage states the operating outcome.",
        "limitations": [],
        "evidence": [
            "Acme helps finance teams close the books faster.",
            "Tone is operational and precise.",
        ],
        "evidence_refs": ["raw_inputs.0.text", "features.0"],
        "evidence_source_summary": {
            "owned_copy": 1,
            "external_proof": 0,
            "visual_signal": 0,
            "derived_strategy": 0,
            "acquisition_metadata": 0,
            "other": 1,
            "total": 2,
        },
        "source": "sv9_flow",
        "ingress_version": SV9_FLOW_INGRESS_VERSION,
    }
    assert blocks["vision"]["detected"] is False
    assert blocks["vision"]["evidence"] == []


def test_flow_candidate_extra_signals_group_by_component() -> None:
    signals = flow_candidate_extra_signals(
        _candidate(
            tile_signals=[
                TileSignal(
                    component="mission",
                    tile="mission.M1",
                    effect="supports",
                    confidence="high",
                    source="brand_interpretation",
                    evidence_refs=["raw_inputs.0.text"],
                    rationale="mission is detected in brand interpretation.",
                )
            ]
        )
    )

    assert signals["mission"][0]["feature"] == "sv9_flow_tile_signal"
    assert signals["mission"][0]["tile"] == "mission.M1"
    assert signals["mission"][0]["effect"] == "supports"
    assert signals["mission"][0]["evidence_refs"] == ["raw_inputs.0.text"]


def test_ingress_demotes_detected_blocks_without_evidence_refs() -> None:
    candidate = _candidate()
    candidate.interpretation.blocks["values"] = {
        "detected": True,
        "content": "Implied values without citations.",
        "confidence": "medium",
        "rationale": "No refs were kept for this block.",
    }
    candidate.interpretation.evidence_refs.pop("values", None)

    blocks = detection_blocks_from_flow_candidate(candidate)

    assert blocks["values"]["detected"] is False
    assert "contract_violation:detected_without_evidence_refs" in blocks["values"]["limitations"]
    assert blocks["mission"]["detected"] is True


def test_run_sv9_rejects_two_detection_inputs() -> None:
    with pytest.raises(ValueError, match="exactly one detection input"):
        run_sv9_from_audit_snapshot(
            {},
            magnetism_result={"tldr_brand3": {}},
            sv9_flow_candidate=_candidate(),
        )


def test_native_path_uses_explicit_source_run_id(monkeypatch) -> None:
    monkeypatch.setattr("src.sv9.service.MagnetismExtractor", _ForbiddenExtractor)

    result = run_sv9_from_audit_snapshot(
        {"raw_inputs": [], "features": []},
        llm=_TileLLM(),
        sv9_flow_candidate=_candidate(),
        source_run_id=349,
    )

    assert result.source_run_id == 349


def test_native_candidate_path_scores_without_magnetism_payload(monkeypatch) -> None:
    monkeypatch.setattr("src.sv9.service.MagnetismExtractor", _ForbiddenExtractor)
    candidate = _candidate(
        tile_signals=[
            TileSignal(
                component="mission",
                tile="mission.M2",
                effect="insufficient_evidence",
                confidence="high",
                source="brand_interpretation",
                evidence_refs=["raw_inputs.0.text"],
                rationale="No direct evidence for this tile.",
            )
        ]
    )
    snapshot = {
        "run": {"id": 44, "brand_name": "Snapshot Name", "url": "https://snapshot.example"},
        "raw_inputs": [],
        "features": [],
    }

    result = run_sv9_from_audit_snapshot(snapshot, llm=_TileLLM(), sv9_flow_candidate=candidate)

    assert result.brand_name == "Acme"
    assert result.url == "https://acme.example"
    assert result.source_run_id == 44
    data = result.to_dict()
    mission = data["components"]["mission"]
    assert mission["status"] == "scored"
    assert "M2" in mission["blind_spot_tiles"]
    assert data["components"]["vision"]["status"] == "not_detected"
