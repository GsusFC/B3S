import pytest

from src.sv9.flow_ingress import (
    SV9_FLOW_INGRESS_VERSION,
    detection_blocks_from_flow_candidate,
    flow_candidate_extra_signals,
)
from src.sv9.rubric import COMPONENTS, tile_ids
from src.sv9.evaluator import _build_component_prompt
from src.sv9.service import run_sv9_from_audit_snapshot
from src.sv9_flow.contracts import (
    BrandEvidencePack,
    BrandInterpretation,
    EvidenceRecord,
    Sv9FlowCandidate,
    TileSignal,
)
from src.sv9_flow.evidence_identity import canonical_evidence_ref


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
        if schema_name == "baldosas_coherencia":
            literal_quote = "Tone is operational and precise."
        elif "Acme now helps legal teams review contracts." in user:
            literal_quote = "Acme now helps legal teams review contracts."
        else:
            literal_quote = "Acme helps finance teams close the books faster."
        payload = {
            "baldosas": [
                {
                    "id": tile_id,
                    "estado": "ok",
                    "evidencia": literal_quote,
                }
                for tile_id in ids
            ]
        }
        if schema_name == "baldosas_coherencia":
            payload["veredicto"] = "La marca cuenta una historia única."
        return payload


class _PromptCachingTileLLM(_TileLLM):
    def __init__(self):
        self.cache = {}
        self.provider_calls = 0

    def _call_json(self, system, user, **kwargs):
        key = (
            system,
            user,
            kwargs.get("schema_name"),
            kwargs.get("temperature"),
        )
        if key in self.cache:
            return self.cache[key]
        self.provider_calls += 1
        payload = super()._call_json(system, user, **kwargs)
        self.cache[key] = payload
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
        "evaluation_evidence_refs": ["raw_inputs.0.text", "features.0"],
        "evaluation_evidence_version": "",
        "evidence_source_summary": {
            "owned_copy": 1,
            "external_proof": 0,
            "visual_signal": 0,
            "derived_strategy": 0,
            "acquisition_metadata": 0,
            "other": 1,
            "total": 2,
        },
        "citation_source_summary": {
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
    assert signals["mission"][0]["evidence_refs"] == [
        canonical_evidence_ref(_candidate().evidence_pack.evidence[0])
    ]
    assert signals["mission"][0]["source_evidence_refs"] == ["raw_inputs.0.text"]


def test_evaluator_prompt_reuses_semantically_identical_component_evidence() -> None:
    first = _candidate(
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
    second = _candidate(
        tile_signals=[
            TileSignal(
                component="mission",
                tile="mission.M1",
                effect="supports",
                confidence="high",
                source="brand_interpretation",
                evidence_refs=["raw_inputs.9.text"],
                rationale="mission is detected in brand interpretation.",
            )
        ]
    )
    second.evidence_pack.evidence[0].ref = "raw_inputs.9.text"
    second.evidence_pack.evidence.reverse()
    second.interpretation.evidence_refs["mission"] = ["raw_inputs.9.text", "features.0"]

    first_tldr = detection_blocks_from_flow_candidate(first)
    second_tldr = detection_blocks_from_flow_candidate(second)
    first_signals = flow_candidate_extra_signals(first)["mission"]
    second_signals = flow_candidate_extra_signals(second)["mission"]

    first_prompt = _build_component_prompt(
        "mission",
        block=first_tldr["mission"],
        signals=first_signals,
        tldr=first_tldr,
        brand_name="Acme",
        url="https://acme.example",
    )
    second_prompt = _build_component_prompt(
        "mission",
        block=second_tldr["mission"],
        signals=second_signals,
        tldr=second_tldr,
        brand_name="Acme",
        url="https://acme.example",
    )

    assert first_prompt == second_prompt

    second.evidence_pack.evidence[1].content = "Acme now helps legal teams review contracts."
    changed_tldr = detection_blocks_from_flow_candidate(second)
    changed_prompt = _build_component_prompt(
        "mission",
        block=changed_tldr["mission"],
        signals=second_signals,
        tldr=changed_tldr,
        brand_name="Acme",
        url="https://acme.example",
    )

    assert changed_prompt != first_prompt


def test_values_prompt_uses_frozen_evaluator_refs_not_variable_interpretation_refs() -> None:
    evidence = [
        EvidenceRecord(
            ref="raw_inputs.1.subpage.3.chunk.4",
            source="web",
            evidence_type="raw_input",
            content="Our 11 pillars. Be Service means serving before selling.",
        ),
        EvidenceRecord(
            ref="raw_inputs.1.subpage.3.chunk.5",
            source="web",
            evidence_type="raw_input",
            content="Be Ownership. Be Happy. Be WYSIWYG.",
        ),
        EvidenceRecord(
            ref="raw_inputs.1.subpage.3.chunk.6",
            source="web",
            evidence_type="raw_input",
            content="Why we exist. How we work. What we build.",
        ),
    ]

    def candidate(citation_ref: str) -> Sv9FlowCandidate:
        return Sv9FlowCandidate(
            evidence_pack=BrandEvidencePack(
                brand_name="SoccerSolver",
                url="https://soccersolver.com",
                evidence=evidence,
            ),
            interpretation=BrandInterpretation(
                brand_name="SoccerSolver",
                url="https://soccersolver.com",
                blocks={
                    "values": {
                        "detected": True,
                        "content": "Service, ownership, happiness, and transparency.",
                        "confidence": "high",
                        "rationale": "The about page states operational principles.",
                    }
                },
                evidence_refs={"values": [citation_ref]},
            ),
            evaluation_evidence_refs={
                "values": [
                    "raw_inputs.1.subpage.3.chunk.5",
                    "raw_inputs.1.subpage.3.chunk.4",
                    "raw_inputs.1.subpage.3.chunk.6",
                ]
            },
            evaluation_evidence_version="sv9-flow-evaluation-evidence-refs-v1",
        )

    first_blocks = detection_blocks_from_flow_candidate(
        candidate("raw_inputs.1.subpage.3.chunk.5")
    )
    second_blocks = detection_blocks_from_flow_candidate(
        candidate("raw_inputs.1.subpage.3.chunk.4")
    )

    assert first_blocks["values"]["evidence_refs"] != second_blocks["values"]["evidence_refs"]
    assert (
        first_blocks["values"]["evaluation_evidence_refs"]
        == second_blocks["values"]["evaluation_evidence_refs"]
    )
    assert first_blocks["values"]["evidence"] == second_blocks["values"]["evidence"]
    assert _build_component_prompt(
        "values",
        block=first_blocks["values"],
        signals=[],
        tldr=first_blocks,
        brand_name="SoccerSolver",
        url="https://soccersolver.com",
    ) == _build_component_prompt(
        "values",
        block=second_blocks["values"],
        signals=[],
        tldr=second_blocks,
        brand_name="SoccerSolver",
        url="https://soccersolver.com",
    )


def test_native_evaluator_reuses_components_when_only_provenance_refs_change() -> None:
    first = _candidate()
    second = _candidate()
    second.evidence_pack.evidence[0].ref = "raw_inputs.9.text"
    second.evidence_pack.evidence.reverse()
    second.interpretation.evidence_refs["mission"] = ["raw_inputs.9.text", "features.0"]
    llm = _PromptCachingTileLLM()
    snapshot = {"raw_inputs": [], "features": []}

    first_result = run_sv9_from_audit_snapshot(snapshot, llm=llm, sv9_flow_candidate=first)
    provider_calls_after_first = llm.provider_calls
    second_result = run_sv9_from_audit_snapshot(snapshot, llm=llm, sv9_flow_candidate=second)

    assert provider_calls_after_first == 2  # mission + coherencia
    assert llm.provider_calls == provider_calls_after_first
    assert first_result.to_dict()["components"] == second_result.to_dict()["components"]

    second.evidence_pack.evidence[1].content = "Acme now helps legal teams review contracts."
    run_sv9_from_audit_snapshot(snapshot, llm=llm, sv9_flow_candidate=second)

    # A material quote change invalidates both the affected component and
    # Coherencia, whose prompt now exposes the literal sources it may cite.
    assert llm.provider_calls == provider_calls_after_first + 2


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
