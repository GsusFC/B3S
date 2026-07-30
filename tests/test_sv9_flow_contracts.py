import json
import subprocess
import sys

from src.sv9_flow import (
    BRAND_EVIDENCE_PACK_VERSION,
    BRAND_INTERPRETATION_VERSION,
    SV9_FLOW_CANDIDATE_VERSION,
    SV9_TILE_SIGNALS_VERSION,
    BrandEvidencePack,
    BrandInterpretation,
    EvidenceRecord,
    Sv9FlowCandidate,
)
from src.sv9_flow.block_evidence_worker import EVALUATION_EVIDENCE_REFS_VERSION
from src.sv9_flow.contracts import interpretation_contract_violations
from src.sv9_flow.evidence_coverage import (
    acquisition_coverage,
    block_coverage,
    component_surface_hierarchy,
    coverage_limitations,
)
from scripts.sv9_flow_legacy_compat import build_flow_candidate_from_current_outputs
from src.sv9_flow.orchestrator import build_flow_candidate
from src.sv9_flow.reporting import SV9_FLOW_REPORT_VERSION, build_flow_report
from src.sv9_flow.tile_signal_worker import build_tile_signals_from_interpretation
from src.sv9.rubric import COMPONENTS, tile_ids
from scripts.sv9_flow_sv9_shadow_eval import build_flow_sv9_shadow_eval, compare_sv9_summaries


def test_flow_candidate_builds_evidence_interpretation_and_tile_signals_without_score() -> None:
    snapshot = {
        "run": {"brand_name": "Acme", "url": "https://acme.example"},
        "raw_inputs": [
            {
                "source": "homepage",
                "payload": {
                    "url": "https://acme.example",
                    "text": "Acme helps finance teams close books faster with automated workflows.",
                },
            }
        ],
        "features": [
            {
                "dimension_name": "coherencia",
                "feature_name": "tone_consistency",
                "value": 0.8,
                "confidence": 0.9,
                "raw_value": "Tone is consistently operational and precise.",
            }
        ],
    }
    tldr = {
        "tldr_brand3": {
            "mission": {"detected": True, "content": "Help finance teams close faster."},
            "value_proposition": {"detected": True, "content": "Automated workflows for finance teams."},
        }
    }

    candidate = build_flow_candidate_from_current_outputs(snapshot=snapshot, tldr_payload=tldr)
    payload = candidate.to_dict()

    assert payload["schema_version"] == SV9_FLOW_CANDIDATE_VERSION
    assert payload["evidence_pack"]["schema_version"] == BRAND_EVIDENCE_PACK_VERSION
    assert payload["interpretation"]["schema_version"] == BRAND_INTERPRETATION_VERSION
    assert payload["evidence_pack"]["brand_name"] == "Acme"
    assert "brand3_score" not in payload
    assert "score" not in payload

    signals = payload["tile_signals"]
    assert signals
    assert all(signal["schema_version"] == SV9_TILE_SIGNALS_VERSION for signal in signals)
    assert {signal["effect"] for signal in signals} == {"supports"}
    assert {signal["source"] for signal in signals} == {"brand_interpretation"}
    by_component = {signal["component"]: signal for signal in signals}
    assert by_component["value_proposition"]["tile"] == "value_proposition.P1"


def test_component_surface_hierarchy_joins_exact_refs_with_navigation() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="acquisition.page_selection",
                source="web",
                evidence_type="acquisition.page_selection",
                content="Page selection manifest.",
                url="https://acme.example",
                metadata={
                    "page_selection": {
                        "known_pages": [
                            {
                                "url": "https://acme.example",
                                "navigation_status": "homepage",
                            },
                            {
                                "url": "https://acme.example/about",
                                "navigation_status": "linked_from_home",
                            },
                            {
                                "url": "https://acme.example/thesis",
                                "navigation_status": "sitemap_only",
                            },
                        ],
                        "visited_pages": [
                            {
                                "url": "https://acme.example",
                                "status": "captured",
                            },
                            {
                                "url": "https://acme.example/about",
                                "status": "captured",
                            },
                            {
                                "url": "https://acme.example/thesis",
                                "status": "captured",
                            },
                        ],
                    }
                },
            ),
            EvidenceRecord(
                ref="owned.home",
                source="web",
                evidence_type="owned.claim",
                content="Acme promise.",
                url="https://acme.example/",
            ),
            EvidenceRecord(
                ref="owned.about",
                source="web",
                evidence_type="owned.claim",
                content="Acme purpose.",
                url="https://acme.example/about",
            ),
            EvidenceRecord(
                ref="owned.thesis",
                source="web",
                evidence_type="owned.claim",
                content="Acme category thesis.",
                url="https://acme.example/thesis",
            ),
            EvidenceRecord(
                ref="external.profile",
                source="exa",
                evidence_type="external.profile",
                content="Acme profile.",
                url="https://example.org/acme",
            ),
        ],
    )
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={
            "brand_idea": {"detected": True, "content": "Acme promise."},
            "mission": {"detected": True, "content": "Acme purpose and thesis."},
            "attributes": {"detected": True, "content": "External profile."},
            "vision": {"detected": False, "content": ""},
        },
        evidence_refs={
            "brand_idea": ["owned.home"],
            "mission": ["owned.about", "owned.thesis"],
            "attributes": ["external.profile"],
        },
    )

    hierarchy = component_surface_hierarchy(pack, interpretation)

    assert hierarchy["brand_idea"]["hierarchy_status"] == "homepage"
    assert hierarchy["mission"]["hierarchy_status"] == "mixed_with_sitemap_only"
    assert hierarchy["mission"]["counts"]["linked_from_home"] == 1
    assert hierarchy["mission"]["counts"]["sitemap_only"] == 1
    assert hierarchy["mission"]["owned_surfaces"][1]["captured"] is True
    assert hierarchy["attributes"]["hierarchy_status"] == "external_only"
    assert hierarchy["vision"]["hierarchy_status"] == "not_detected"


def test_interpretation_tile_signals_use_valid_sv9_tile_ids() -> None:
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={
            component: {"detected": True, "content": f"{component} content", "confidence": "high"}
            for component in COMPONENTS
            if component != "coherencia"
        },
        evidence_refs={},
        limitations=[],
    )

    signals = build_tile_signals_from_interpretation(interpretation)
    valid_by_component = {component: set(tile_ids(component)) for component in COMPONENTS}

    for signal in signals:
        tile = signal.to_dict()["tile"]
        component = signal.to_dict()["component"]
        assert tile.startswith(f"{component}.")
        assert tile.split(".", 1)[1] in valid_by_component[component]


def test_flow_candidate_keeps_medium_negative_visual_signature_as_evidence_only() -> None:
    visual_signature = {
        "schema_version": "visual-signature-evidence-v1",
        "capture": {"status": "usable", "first_fold_evaluable": True},
        "tile_signals": [
            {
                "tile": "coherencia.C6",
                "effect": "weakens",
                "confidence": "medium",
                "source": "heuristic",
                "rationale": "Hero copy and visible visual system point in different directions.",
            }
        ],
    }

    candidate = build_flow_candidate_from_current_outputs(
        snapshot={"run": {"brand_name": "Acme", "url": "https://acme.example"}},
        tldr_payload={"tldr_brand3": {}},
        visual_signature_evidence=visual_signature,
    )
    signals = candidate.to_dict()["tile_signals"]

    assert signals == []
    assert candidate.evidence_pack.evidence[0].ref == "visual_signature.capture"
    assert candidate.evidence_pack.evidence[1].ref == "visual_signature.tile_signals.0"


def test_flow_candidate_does_not_turn_visual_capture_payloads_into_text_evidence() -> None:
    snapshot = {
        "run": {"brand_name": "Acme", "url": "https://acme.example"},
        "raw_inputs": [
            {
                "source": "screenshot_capture",
                "payload": {
                    "capture": {
                        "success": False,
                        "error_message": "Payment Required: screenshot provider has no credits",
                    }
                },
            },
            {
                "source": "visual_acquisition",
                "payload": {
                    "visual_evidence_packet": {
                        "schema_version": "visual-signature-evidence-v1",
                        "capture": {"status": "missing", "first_fold_evaluable": False},
                        "tile_signals": [],
                    }
                },
            },
            {
                "source": "web",
                "payload": {
                    "url": "https://acme.example",
                    "markdown_content": "Acme helps finance teams close books faster.",
                },
            },
        ],
    }

    candidate = build_flow_candidate_from_current_outputs(
        snapshot=snapshot,
        tldr_payload={"tldr_brand3": {}},
    )
    evidence_refs = [record.ref for record in candidate.evidence_pack.evidence]

    assert all("screenshot_capture" not in ref for ref in evidence_refs)
    assert all("visual_acquisition" not in ref for ref in evidence_refs)
    assert any(ref.startswith("raw_inputs.2") for ref in evidence_refs)


def test_flow_candidate_keeps_high_confidence_negative_visual_signature_tile_signal() -> None:
    visual_signature = {
        "schema_version": "visual-signature-evidence-v1",
        "capture": {"status": "usable", "first_fold_evaluable": True},
        "tile_signals": [
            {
                "tile": "coherencia.C6",
                "effect": "weakens",
                "confidence": "high",
                "source": "heuristic",
                "rationale": "Hero copy and visible visual system point in different directions.",
            }
        ],
    }

    candidate = build_flow_candidate_from_current_outputs(
        snapshot={"run": {"brand_name": "Acme", "url": "https://acme.example"}},
        tldr_payload={"tldr_brand3": {}},
        visual_signature_evidence=visual_signature,
    )
    signals = candidate.to_dict()["tile_signals"]

    assert {
        (signal["tile"], signal["effect"], signal["source"])
        for signal in signals
    } == {("coherencia.C6", "weakens", "visual_signature")}


def test_flow_candidate_blocks_positive_visual_signal_when_capture_is_unusable() -> None:
    visual_signature = {
        "schema_version": "visual-signature-evidence-v1",
        "capture": {"status": "blocked", "first_fold_evaluable": False},
        "tile_signals": [
            {
                "tile": "coherencia.C6",
                "effect": "supports",
                "confidence": "high",
                "rationale": "Would be unsafe to trust because capture is blocked.",
            }
        ],
    }

    candidate = build_flow_candidate_from_current_outputs(
        snapshot={"run": {"brand_name": "Acme", "url": "https://acme.example"}},
        tldr_payload={"tldr_brand3": {}},
        visual_signature_evidence=visual_signature,
    )
    signals = candidate.to_dict()["tile_signals"]

    assert signals == []
    assert not [
        record
        for record in candidate.evidence_pack.evidence
        if record.ref.startswith("visual_signature.")
    ]


def test_magnetism_tile_signals_mark_pull_tiles_insufficient_when_direct_evidence_is_missing() -> None:
    interpretation = BrandInterpretation(
        brand_name="Linear",
        url="https://linear.app",
        blocks={
            "magnetism": {
                "detected": True,
                "content": "Linear has professional appeal among engineering teams.",
                "confidence": "low",
                "rationale": "Third-party recognition indicates appeal, but direct pull metrics are missing.",
            }
        },
        evidence_refs={"magnetism": ["features.7", "features.11"]},
        limitations=["No direct community metrics, developer advocacy data, or organic growth statistics were available."],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    assert by_tile["magnetism.MG1"]["effect"] == "supports"
    assert by_tile["magnetism.MG1"]["confidence"] == "low"
    assert by_tile["magnetism.MG9"]["effect"] == "insufficient_evidence"
    assert by_tile["magnetism.MG9"]["confidence"] == "high"
    assert by_tile["magnetism.MG10"]["effect"] == "insufficient_evidence"


def test_magnetism_tile_signals_mark_pull_tiles_insufficient_for_missing_loyalty_metrics() -> None:
    interpretation = BrandInterpretation(
        brand_name="Pleo",
        url="https://www.pleo.io",
        blocks={
            "magnetism": {
                "detected": True,
                "content": "Pleo has momentum evidence, but direct customer loyalty evidence is missing.",
                "confidence": "low",
                "rationale": "Momentum is present, not direct community pull.",
            }
        },
        evidence_refs={"magnetism": ["features.7"]},
        limitations=["No direct metrics or qualitative data regarding brand magnetism or customer loyalty were available."],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    assert by_tile["magnetism.MG9"]["effect"] == "insufficient_evidence"
    assert by_tile["magnetism.MG10"]["effect"] == "insufficient_evidence"


def test_gate_overridden_magnetism_does_not_mark_mechanism_tiles_without_family_limitations() -> None:
    interpretation = BrandInterpretation(
        brand_name="Pleo",
        url="https://www.pleo.io",
        blocks={
            "magnetism": {
                "detected": True,
                "content": "magnetism has enough evidence to evaluate via deterministic gate signals: momentum.",
                "confidence": "low",
                "rationale": "No direct magnetism data was available.",
            }
        },
        evidence_refs={"magnetism": ["features.7"]},
        limitations=["magnetism_llm_detection_overridden_by_gate"],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    assert by_tile["magnetism.MG1"]["effect"] == "supports"
    for tile in ("magnetism.MG3", "magnetism.MG4", "magnetism.MG5", "magnetism.MG6", "magnetism.MG7", "magnetism.MG8"):
        assert tile not in by_tile


def test_market_momentum_only_magnetism_marks_mechanism_tiles_insufficient() -> None:
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={
            "magnetism": {
                "detected": True,
                "content": "Acme has funding and press momentum.",
                "confidence": "medium",
                "rationale": "The evidence is market momentum, not a direct audience pull mechanism.",
            }
        },
        evidence_refs={"magnetism": ["features.7"]},
        limitations=[
            "magnetism_market_momentum_only",
            "magnetism_no_owned_hook_evidence",
            "magnetism_no_preference_evidence",
            "magnetism_no_belonging_status_evidence",
        ],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    for tile in ("magnetism.MG3", "magnetism.MG4", "magnetism.MG5", "magnetism.MG6", "magnetism.MG7", "magnetism.MG8"):
        assert by_tile[tile]["effect"] == "insufficient_evidence"
        assert by_tile[tile]["confidence"] == "high"
    assert by_tile["magnetism.MG9"]["effect"] == "insufficient_evidence"
    assert by_tile["magnetism.MG9"]["confidence"] == "high"


def test_magnetism_owned_hook_limitation_only_marks_hook_tiles_insufficient() -> None:
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={
            "magnetism": {
                "detected": True,
                "content": "Acme has audience preference evidence but no owned hook mechanism.",
                "confidence": "medium",
                "rationale": "The evidence supports magnetism only in preference terms.",
            }
        },
        evidence_refs={"magnetism": ["raw_inputs.2"]},
        limitations=["magnetism_no_owned_hook_evidence"],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    for tile in ("magnetism.MG3", "magnetism.MG4", "magnetism.MG5", "magnetism.MG6"):
        assert by_tile[tile]["effect"] == "insufficient_evidence"
        assert by_tile[tile]["confidence"] == "high"
    assert "magnetism.MG7" not in by_tile
    assert "magnetism.MG8" not in by_tile
    assert "magnetism.MG9" not in by_tile
    assert "magnetism.MG10" not in by_tile


def test_magnetism_preference_limitation_only_marks_preference_tiles_insufficient() -> None:
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={
            "magnetism": {
                "detected": True,
                "content": "Acme has owned hook evidence but no preference evidence.",
                "confidence": "medium",
                "rationale": "The evidence supports magnetism only in hook terms.",
            }
        },
        evidence_refs={"magnetism": ["raw_inputs.2"]},
        limitations=["magnetism_no_preference_evidence"],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    assert "magnetism.MG3" not in by_tile
    assert "magnetism.MG4" not in by_tile
    assert "magnetism.MG5" not in by_tile
    assert "magnetism.MG6" not in by_tile
    for tile in ("magnetism.MG7", "magnetism.MG8"):
        assert by_tile[tile]["effect"] == "insufficient_evidence"
        assert by_tile[tile]["confidence"] == "high"


def test_generic_core_purpose_marks_advanced_purpose_tiles_as_weakening() -> None:
    interpretation = BrandInterpretation(
        brand_name="Pleo",
        url="https://www.pleo.io",
        blocks={
            "core_purpose": {
                "detected": True,
                "content": (
                    "Pleo's mission is to make spend management effective, with a vision "
                    "to automate expense processes for finance teams."
                ),
                "confidence": "high",
                "rationale": "Extracted from official mission and vision statements.",
            }
        },
        evidence_refs={"core_purpose": ["raw_inputs.2", "features.14"]},
        limitations=["The brand's messaging relies heavily on standard SaaS category descriptors."],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    assert by_tile["core_purpose.PR1"]["effect"] == "supports"
    for tile in ("core_purpose.PR3", "core_purpose.PR4", "core_purpose.PR7", "core_purpose.PR8", "core_purpose.PR9"):
        assert by_tile[tile]["effect"] == "weakens"
        assert by_tile[tile]["confidence"] == "high"


def test_derived_core_purpose_marks_advanced_purpose_tiles_as_weakening() -> None:
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={
            "core_purpose": {
                "detected": True,
                "content": "Acme exists to maximize operational efficiency.",
                "confidence": "medium",
                "rationale": "Derived from strategy summaries.",
            }
        },
        evidence_refs={"core_purpose": ["features.0", "raw_inputs.8"]},
        limitations=["core_purpose_derived_strategy_evidence"],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    for tile in ("core_purpose.PR3", "core_purpose.PR4", "core_purpose.PR7", "core_purpose.PR8", "core_purpose.PR9"):
        assert by_tile[tile]["effect"] == "weakens"
        assert by_tile[tile]["confidence"] == "high"


def test_product_bound_core_purpose_marks_advanced_purpose_tiles_as_weakening() -> None:
    interpretation = BrandInterpretation(
        brand_name="Baker",
        url="https://withbaker.com",
        blocks={
            "core_purpose": {
                "detected": True,
                "content": (
                    "Baker is a performance marketing agency that manages paid marketing channels "
                    "for lead-based businesses using autonomous AI agents and engineers."
                ),
                "confidence": "high",
                "rationale": "The homepage describes the service from strategy to execution.",
            }
        },
        evidence_refs={"core_purpose": ["raw_inputs.1", "raw_inputs.2"]},
        limitations=[],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    assert by_tile["core_purpose.PR1"]["effect"] == "supports"
    for tile in ("core_purpose.PR3", "core_purpose.PR4", "core_purpose.PR7", "core_purpose.PR8", "core_purpose.PR9"):
        assert by_tile[tile]["effect"] == "weakens"
        assert by_tile[tile]["confidence"] == "high"


def test_core_purpose_duplicate_of_mission_marks_advanced_purpose_tiles_as_weakening() -> None:
    interpretation = BrandInterpretation(
        brand_name="Staris",
        url="https://staris.tech",
        blocks={
            "core_purpose": {
                "detected": True,
                "content": (
                    "Staris provides continuous AppSec validation by proving which vulnerability "
                    "candidates are exploitable and delivering PR-ready patches to fix them."
                ),
                "confidence": "high",
                "rationale": "The sources define core purpose as proving exploits and shipping fixes.",
            },
            "mission": {
                "detected": True,
                "content": (
                    "To prove which vulnerability candidates are exploitable in running applications "
                    "and ship the fixes at release cadence."
                ),
                "confidence": "high",
                "rationale": "The mission is to prove exploits and ship fixes.",
            },
        },
        evidence_refs={"core_purpose": ["raw_inputs.1"], "mission": ["raw_inputs.1"]},
        limitations=[],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    for tile in ("core_purpose.PR3", "core_purpose.PR4", "core_purpose.PR7", "core_purpose.PR8", "core_purpose.PR9"):
        assert by_tile[tile]["effect"] == "weakens"
        assert by_tile[tile]["confidence"] == "high"


def test_functional_product_core_purpose_marks_advanced_purpose_tiles_as_weakening() -> None:
    interpretation = BrandInterpretation(
        brand_name="Staris",
        url="https://staris.tech",
        blocks={
            "core_purpose": {
                "detected": True,
                "content": (
                    "Staris delivers automated penetration testing and continuous security validation, "
                    "replacing traditional scanners and manual workflows by discovering and proving "
                    "real exploitable vulnerabilities with working exploits and PR-ready patches."
                ),
                "confidence": "high",
                "rationale": "The block describes product functionality and service delivery.",
            },
            "mission": {
                "detected": True,
                "content": (
                    "To build continuous, exploit-proven application security validation that proves "
                    "which vulnerability candidates are exploitable in running apps."
                ),
                "confidence": "high",
                "rationale": "The mission is to prove exploitable vulnerabilities.",
            },
            "value_proposition": {
                "detected": True,
                "content": (
                    "Staris automates security validation volume with proof of exploit and PR-ready "
                    "patches so AppSec teams can eliminate scanner noise."
                ),
                "confidence": "high",
                "rationale": "The value proposition is product utility.",
            },
        },
        evidence_refs={"core_purpose": ["raw_inputs.1"]},
        limitations=[],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    assert by_tile["core_purpose.PR1"]["effect"] == "supports"
    for tile in ("core_purpose.PR3", "core_purpose.PR4", "core_purpose.PR7", "core_purpose.PR8", "core_purpose.PR9"):
        assert by_tile[tile]["effect"] == "weakens"
        assert by_tile[tile]["confidence"] == "high"


def test_agent_feature_core_purpose_marks_advanced_purpose_tiles_as_weakening() -> None:
    interpretation = BrandInterpretation(
        brand_name="Hermes Agent",
        url="https://hermes-agent.example",
        blocks={
            "core_purpose": {
                "detected": True,
                "content": (
                    "Hermes Agent is an open-source, autonomous agent designed to grow more capable "
                    "over time. It features persistent memory to learn projects and auto-generate "
                    "skills, running across multiple platforms and messaging interfaces."
                ),
                "confidence": "high",
                "rationale": "The block describes product capabilities and surfaces.",
            },
            "value_proposition": {
                "detected": True,
                "content": (
                    "Power users and developers get a persistent, local-first AI agent environment "
                    "with focused automation, persistent memory, and isolated subagents."
                ),
                "confidence": "high",
                "rationale": "The value proposition is product utility.",
            },
        },
        evidence_refs={"core_purpose": ["raw_inputs.1"]},
        limitations=[],
    )

    signals = build_tile_signals_from_interpretation(interpretation)

    by_tile = {signal.tile: signal.to_dict() for signal in signals}
    assert by_tile["core_purpose.PR1"]["effect"] == "supports"
    for tile in ("core_purpose.PR3", "core_purpose.PR4", "core_purpose.PR7", "core_purpose.PR8", "core_purpose.PR9"):
        assert by_tile[tile]["effect"] == "weakens"
        assert by_tile[tile]["confidence"] == "high"


def test_flow_report_summarizes_candidate_quality_controls() -> None:
    candidate = build_flow_candidate_from_current_outputs(
        snapshot={"run": {"brand_name": "Acme", "url": "https://acme.example"}},
        tldr_payload={"tldr_brand3": {"mission": {"detected": True, "content": "Help teams ship."}}},
    )

    report = build_flow_report(candidate)

    assert report["schema_version"] == SV9_FLOW_REPORT_VERSION
    assert report["candidate_schema_version"] == SV9_FLOW_CANDIDATE_VERSION
    assert report["brand_name"] == "Acme"
    assert report["counts"]["interpretation_blocks"] == 1
    assert report["tile_signal_effects"] == {"supports": 1}


def test_sv9_flow_candidate_script_reads_json_and_prints_report(tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    tldr_path = tmp_path / "tldr.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "run": {"brand_name": "Acme", "url": "https://acme.example"},
                "raw_inputs": [{"source": "homepage", "payload": {"text": "Acme helps teams ship."}}],
            }
        ),
        encoding="utf-8",
    )
    tldr_path.write_text(
        json.dumps({"tldr_brand3": {"mission": {"detected": True, "content": "Help teams ship."}}}),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/sv9_flow_candidate.py",
            "--snapshot",
            str(snapshot_path),
            "--tldr",
            str(tldr_path),
            "--report",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["schema_version"] == SV9_FLOW_REPORT_VERSION
    assert payload["brand_name"] == "Acme"
    assert payload["counts"]["evidence_records"] == 1
    assert payload["tile_signal_effects"] == {"supports": 1}


def test_interpretation_contract_requires_content_and_refs_when_detected() -> None:
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={
            "mission": {"detected": True, "content": "Help teams ship.", "confidence": "high"},
            "vision": {"detected": True, "content": "", "confidence": "medium"},
            "values": {"detected": True, "content": "Implied values.", "confidence": "low"},
            "personality": {"detected": False, "content": "", "confidence": "low"},
        },
        evidence_refs={"mission": ["raw_inputs.0"], "values": []},
    )

    violations = interpretation_contract_violations(interpretation)

    assert violations == [
        "values_detected_without_evidence_refs",
        "vision_detected_without_content",
        "vision_detected_without_evidence_refs",
    ]


def test_block_coverage_derives_probable_absent_from_one_checked_surface() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Acme helps finance teams automate payments.",
                url="https://acme.example",
                metadata={"source_class": "owned_copy"},
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.absence.values",
                source="web",
                evidence_type="acquisition.absence.values",
                content="Crawled owned page https://acme.example/about; no explicit values section terms were observed.",
                url="https://acme.example/about",
                metadata={"source_class": "acquisition_metadata", "checked_block": "values"},
            ),
        ],
    )
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={
            "mission": {"detected": True, "content": "Acme helps finance teams.", "confidence": "high"},
            "values": {"detected": False, "content": "", "confidence": "low"},
            "vision": {"detected": False, "content": "", "confidence": "low"},
        },
        evidence_refs={
            "mission": ["raw_inputs.0"],
            # Rejected/not-detected candidates may keep refs for review, but
            # they are not accepted positive evidence for coverage.
            "values": ["raw_inputs.0"],
        },
    )

    coverage = block_coverage(pack, interpretation)

    assert coverage["mission"]["status"] == "positive_evidence"
    assert coverage["values"]["status"] == "probable_absent"
    assert coverage["values"]["absence_refs"] == ["raw_inputs.0.subpage.1.absence.values"]
    assert coverage["values"]["absence_surface_count"] == 1
    assert coverage["vision"]["status"] == "insufficient_acquisition"
    assert "coverage:values_probable_absent" in coverage_limitations(coverage)
    assert "coverage:vision_insufficient_acquisition" in coverage_limitations(coverage)


def test_block_coverage_derives_verified_absent_from_two_checked_surfaces() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.absence.values",
                source="web",
                evidence_type="acquisition.absence.values",
                content="Crawled owned page https://acme.example/about; no explicit values section terms were observed.",
                url="https://acme.example/about",
                metadata={"source_class": "acquisition_metadata", "checked_block": "values"},
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.2.absence.values",
                source="web",
                evidence_type="acquisition.absence.values",
                content="Crawled owned page https://acme.example/careers; no explicit values section terms were observed.",
                url="https://acme.example/careers",
                metadata={"source_class": "acquisition_metadata", "checked_block": "values"},
            ),
        ],
    )
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={"values": {"detected": False, "content": "", "confidence": "low"}},
        evidence_refs={},
    )

    coverage = block_coverage(pack, interpretation)

    assert coverage["values"]["status"] == "verified_absent"
    assert coverage["values"]["absence_surface_count"] == 2
    assert "coverage:values_verified_absent" in coverage_limitations(coverage)


def test_block_coverage_surfaces_counter_refs_without_counting_positive_support() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="review",
                evidence_type="external_proof.review",
                content="Customers say Acme claims care but behaves transactionally.",
                metadata={
                    "source_class": "external_proof",
                    "relevant_blocks": ["values"],
                    "stance": "contradicts",
                    "specificity": "explicit",
                },
            )
        ],
    )
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={"values": {"detected": True, "content": "Acme values care.", "confidence": "medium"}},
        evidence_refs={"values": ["raw_inputs.0"]},
    )

    coverage = block_coverage(pack, interpretation)

    assert coverage["values"]["positive_refs"] == []
    assert coverage["values"]["counter_refs"] == ["raw_inputs.0"]
    assert coverage["values"]["status"] == "insufficient_acquisition"


def test_block_coverage_uses_implied_semantic_ref_for_implied_not_explicit() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="We turn every training ride into a reward loop.",
                metadata={
                    "source_class": "owned_copy",
                    "relevant_blocks": ["mission"],
                    "stance": "supports",
                    "specificity": "implied",
                },
            )
        ],
    )
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={"mission": {"detected": False, "content": "", "confidence": "low"}},
        evidence_refs={"mission": ["raw_inputs.0"]},
    )

    coverage = block_coverage(pack, interpretation)

    assert coverage["mission"]["status"] == "implied_not_explicit"
    assert coverage["mission"]["positive_refs"] == []


def test_acquisition_coverage_summarizes_external_attempts_and_absence_refs() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Homepage copy.",
                url="https://acme.example",
                metadata={"source_class": "owned_copy"},
            ),
            EvidenceRecord(
                ref="raw_inputs.1.searchapi.diagnostics.news",
                source="searchapi",
                evidence_type="acquisition.attempt.news",
                content="searchapi acquisition attempt for news ended with status no_results.",
                metadata={
                    "source_class": "acquisition_metadata",
                    "provider": "searchapi",
                    "intent": "news",
                    "status": "no_results",
                    "query": "Acme funding news",
                },
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.absence.values",
                source="web",
                evidence_type="acquisition.absence.values",
                content="No values terms observed.",
                url="https://acme.example/about",
                metadata={"source_class": "acquisition_metadata"},
            ),
            EvidenceRecord(
                ref="raw_inputs.0.diagnostics.page_selection",
                source="web",
                evidence_type="acquisition.page_selection",
                content="Owned page selection captured 2 of 3 known pages.",
                metadata={
                    "source_class": "acquisition_metadata",
                    "page_selection": {
                        "version": "owned-page-selection-v2",
                        "known_page_count": 3,
                        "attempted_page_count": 2,
                        "captured_page_count": 2,
                        "known_pages": [
                            {"url": "https://acme.example"},
                            {"url": "https://acme.example/about"},
                            {"url": "https://acme.example/orphan"},
                        ],
                        "visited_pages": [
                            {
                                "url": "https://acme.example",
                                "status": "captured",
                            },
                            {
                                "url": "https://acme.example/about",
                                "status": "captured",
                            },
                        ],
                        "not_visited_pages": [
                            {
                                "url": "https://acme.example/orphan",
                                "reason": "page_budget",
                            }
                        ],
                        "latest_lastmod": "2026-07-30",
                    },
                },
            ),
        ],
    )

    coverage = acquisition_coverage(pack)

    assert coverage["owned_urls"] == ["https://acme.example"]
    assert coverage["external_attempts"] == [
        {
            "ref": "raw_inputs.1.searchapi.diagnostics.news",
            "provider": "searchapi",
            "intent": "news",
            "status": "no_results",
            "query": "Acme funding news",
            "error": "",
        }
    ]
    assert coverage["absence_refs"] == [
        {
            "ref": "raw_inputs.0.subpage.1.absence.values",
            "block": "values",
            "url": "https://acme.example/about",
        }
    ]
    assert coverage["owned_page_coverage"] == {
        "selection_version": "owned-page-selection-v2",
        "known_page_count": 3,
        "attempted_page_count": 2,
        "captured_page_count": 2,
        "coverage_ratio": 0.6667,
        "known_pages": [
            {"url": "https://acme.example"},
            {"url": "https://acme.example/about"},
            {"url": "https://acme.example/orphan"},
        ],
        "visited_pages": [
            {"url": "https://acme.example", "status": "captured"},
            {"url": "https://acme.example/about", "status": "captured"},
        ],
        "not_visited_pages": [
            {
                "url": "https://acme.example/orphan",
                "reason": "page_budget",
            }
        ],
        "excluded_pages": [],
        "latest_lastmod": "2026-07-30",
        "language_detection": {},
    }


def test_canonical_orchestrator_builds_candidate_from_evidence_and_llm() -> None:
    class FlowLLM:
        api_key = "test-key"
        model = "flow-fake"
        last_failure_reason = None
        call_failures = []

        def _call_json(self, system, user, **kwargs):
            if '"block": "mission"' in user:
                return {
                    "detected": True,
                    "content": "Help finance teams close faster.",
                    "confidence": "high",
                    "evidence_refs": ["raw_inputs.0"],
                    "rationale": "The homepage states it.",
                    "limitations": [],
                }
            return {
                "detected": False,
                "content": "",
                "confidence": "low",
                "evidence_refs": [],
                "rationale": "Not enough evidence.",
                "limitations": [],
            }

    candidate, debug = build_flow_candidate(
        snapshot={
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "homepage",
                    "payload": {"text": "Acme helps finance teams close the books faster."},
                }
            ],
        },
        llm=FlowLLM(),
    )

    assert candidate.evidence_pack.brand_name == "Acme"
    assert candidate.interpretation.blocks["mission"]["detected"] is True
    assert candidate.interpretation.evidence_refs["mission"] == ["raw_inputs.0"]
    assert any(
        signal.component == "mission" and signal.effect == "supports"
        for signal in candidate.tile_signals
    )
    assert debug["gate_authority"] == "veto_only"
    assert "raw_inputs.0" in debug["block_evidence_shortlists"]["mission"]
    assert candidate.evaluation_evidence_refs["mission"]
    assert (
        candidate.evaluation_evidence_version
        == EVALUATION_EVIDENCE_REFS_VERSION
    )
    assert (
        candidate.to_dict()["evaluation_evidence_refs"]
        == candidate.evaluation_evidence_refs
    )
    assert not [item for item in candidate.limitations if item.startswith("contract_violation:")]


def test_canonical_orchestrator_labels_evidence_before_shortlisting() -> None:
    class LabelingLLM:
        api_key = "test-key"

        def _call_json(self, _system, _user, **_kwargs):
            return {
                "labels": [
                    {
                        "ref": "raw_inputs.0",
                        "relevant_blocks": ["mission"],
                        "stance": "supports",
                        "identity_match": "domain",
                        "specificity": "implied",
                    }
                ]
            }

    class FlowLLM:
        api_key = "test-key"
        model = "flow-fake"
        last_failure_reason = None
        call_failures = []

        def _call_json(self, _system, user, **_kwargs):
            if '"block": "mission"' in user:
                assert '"semantic_relevant_blocks": [' in user
                return {
                    "detected": True,
                    "content": "Acme turns training rides into a reward loop.",
                    "confidence": "medium",
                    "evidence_refs": ["raw_inputs.0"],
                    "rationale": "The homepage implies the mission through the product outcome.",
                    "limitations": [],
                }
            return {
                "detected": False,
                "content": "",
                "confidence": "low",
                "evidence_refs": [],
                "rationale": "Not enough evidence.",
                "limitations": [],
            }

    candidate, debug = build_flow_candidate(
        snapshot={
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {"text": "We turn every training ride into a reward loop."},
                }
            ],
        },
        llm=FlowLLM(),
        labeling_llm=LabelingLLM(),
        gate_authority="warn",
    )

    assert debug["evidence_labeling"]["status"] == "labeled"
    assert debug["evidence_labeling"]["records_labeled"] == 1
    assert "raw_inputs.0" in debug["block_evidence_shortlists"]["mission"]
    assert candidate.evidence_pack.evidence[0].metadata["relevant_blocks"] == ["mission"]
    assert candidate.interpretation.blocks["mission"]["detected"] is True


def test_canonical_orchestrator_emits_evidence_coverage_debug_and_limitations() -> None:
    class FlowLLM:
        api_key = "test-key"

        def _call_json(self, _system, user, **_kwargs):
            if '"mission"' in user:
                block = {
                    "detected": True,
                    "content": "Acme helps finance teams automate payments.",
                    "confidence": "high",
                    "evidence_refs": ["raw_inputs.0"],
                    "rationale": "The homepage states this directly.",
                }
            elif '"values"' in user:
                block = {
                    "detected": False,
                    "content": "",
                    "confidence": "low",
                    "evidence_refs": [],
                    "rationale": "No explicit values statement in cited evidence.",
                }
            elif '"vision"' in user:
                block = {
                    "detected": False,
                    "content": "",
                    "confidence": "low",
                    "evidence_refs": [],
                    "rationale": "No explicit future-state statement in cited evidence.",
                }
            else:
                block = {
                    "detected": False,
                    "content": "",
                    "confidence": "low",
                    "evidence_refs": [],
                    "rationale": "No cited evidence.",
                }
            return block

    snapshot = {
        "run": {"brand_name": "Acme", "url": "https://acme.example"},
        "raw_inputs": [
            {
                "source": "web",
                "payload": {
                    "url": "https://acme.example",
                    "markdown_content": (
                        "Acme helps finance teams automate payments.\n"
                        "\n---\n## Subpage: https://acme.example/about\n"
                        "# About Acme\n"
                        "Acme builds operational software for finance teams."
                    ),
                },
            }
        ],
    }

    candidate, debug = build_flow_candidate(snapshot=snapshot, llm=FlowLLM())

    coverage = debug["evidence_coverage"]
    assert coverage["blocks"]["mission"]["status"] == "positive_evidence"
    assert coverage["blocks"]["values"]["status"] == "probable_absent"
    assert coverage["blocks"]["vision"]["status"] == "probable_absent"
    assert coverage["blocks"]["values"]["absence_refs"] == ["raw_inputs.0.subpage.1.absence.values"]
    assert "coverage:values_probable_absent" in candidate.limitations
    assert "coverage:vision_probable_absent" in candidate.limitations
    assert not candidate.interpretation.evidence_refs.get("values")


def test_shadow_eval_model_tiers_read_env_overrides(monkeypatch) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as shadow_eval

    monkeypatch.setenv("BRAND3_FLOW_INTERPRETATION_MODEL", "tier-lite")
    monkeypatch.delenv("BRAND3_FLOW_EVALUATOR_MODEL", raising=False)

    assert shadow_eval._default_interpretation_llm().model == "tier-lite"
    assert shadow_eval._default_evaluator_llm().model != "tier-lite"


def test_flow_sv9_shadow_eval_runs_current_sv9_from_flow_interpretation() -> None:
    class FlowLLM:
        api_key = "test-key"
        model = "flow-fake"

        def _call_json(self, system, user, **kwargs):
            return {
                "detected": True,
                "content": "Acme helps finance teams close faster.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The evidence states the audience and outcome.",
                "limitations": [],
            }

    class TileLLM:
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

    class NoKeyLabelingLLM:
        api_key = None
        model = "labeling-disabled"

    report = build_flow_sv9_shadow_eval(
        {
            "source_run_id": 44,
            "debug": {
                "run": {"id": 44, "brand_name": "Acme", "url": "https://acme.example"},
                "raw_inputs": [
                    {
                        "source": "homepage",
                        "payload": {
                            "url": "https://acme.example",
                            "text": "Acme helps finance teams close faster.",
                        },
                    }
                ],
            },
        },
        include_full=True,
        interpretation_llm=FlowLLM(),
        evaluator_llm=TileLLM(),
        reasoning_llm=TileLLM(),
        labeling_llm=NoKeyLabelingLLM(),
        visual_evidence_fn=lambda _snapshot: {
            "schema_version": "visual-signature-evidence-v1",
            "capture": {"status": "usable", "first_fold_evaluable": True},
            "tile_signals": [
                {
                    "tile": "brand_idea.I1",
                    "effect": "supports",
                    "confidence": "high",
                    "rationale": "Logo is visible and distinctive.",
                },
                {
                    "tile": "coherencia.C6",
                    "effect": "weakens",
                    "confidence": "medium",
                    "rationale": "Medium-confidence visual mismatch should remain evidence only.",
                },
            ],
        },
    )

    assert report["schema_version"] == "sv9-flow-sv9-shadow-eval-v1"
    assert report["source_run_id"] == 44
    assert report["visual_acquisition_present"] is True
    assert report["flow"]["visual_acquisition_present"] is True
    assert report["flow"]["detected_blocks"]
    assert report["llm_usage"]["roles"]["flow_interpretation"]["model"] == "flow-fake"
    assert report["llm_usage"]["roles"]["sv9_evaluator"]["model"] == "tile-fake"
    assert report["llm_usage"]["totals"]["provider_calls"] == 0
    assert report["flow"]["extra_signals"]["mission"][0]["feature"] == "sv9_flow_tile_signal"
    assert report["flow"]["extra_signals"]["mission"][0]["tile"] == "mission.M1"
    assert report["flow"]["extra_signals"]["mission"][0]["effect"] == "supports"
    assert any(
        signal["source"] == "visual_signature" and signal["tile"] == "brand_idea.I1"
        for signal in report["flow"]["extra_signals"]["brand_idea"]
    )
    assert not any(
        signal["source"] == "visual_signature" and signal["tile"] == "coherencia.C6"
        for signal in report["flow"]["extra_signals"].get("coherencia", [])
    )
    evidence_refs = [
        record["ref"]
        for record in report["flow"]["candidate"]["evidence_pack"]["evidence"]
    ]
    assert "visual_signature.tile_signals.0" in evidence_refs
    assert "visual_signature.tile_signals.1" in evidence_refs
    assert report["sv9"]["brand3_score"] is not None
    assert report["sv9"]["components"]["mission"]["status"] == "scored"


def test_flow_sv9_shadow_eval_compares_flow_and_legacy_tile_profiles() -> None:
    comparison = compare_sv9_summaries(
        flow_summary={
            "brand3_score": 72,
            "base_average": 6.5,
            "reliability_status": "shadow",
            "not_detected": ["vision"],
            "components": {
                "mission": {
                    "status": "scored",
                    "score": 4,
                    "lit_tiles": ["M1", "M2"],
                    "off_tiles": [],
                    "blind_spot_tiles": ["M3"],
                },
                "vision": {
                    "status": "not_detected",
                    "score": 0,
                    "lit_tiles": [],
                    "off_tiles": [],
                    "blind_spot_tiles": [],
                },
            },
        },
        legacy_summary={
            "brand3_score": 76,
            "base_average": 7,
            "reliability_status": "usable",
            "not_detected": [],
            "components": {
                "mission": {
                    "status": "scored",
                    "score": 5,
                    "lit_tiles": ["M1", "M4"],
                    "off_tiles": ["M2"],
                    "blind_spot_tiles": [],
                },
                "vision": {
                    "status": "scored",
                    "score": 2,
                    "lit_tiles": ["V1"],
                    "off_tiles": [],
                    "blind_spot_tiles": [],
                },
            },
        },
    )

    assert comparison["brand3_score_delta"] == -4
    assert comparison["base_average_delta"] == -0.5
    assert comparison["reliability_changed"] is True
    assert comparison["not_detected_added"] == ["vision"]
    assert comparison["changed_components"] == ["mission", "vision"]
    assert comparison["components"]["mission"]["score_delta"] == -1
    assert comparison["components"]["mission"]["tile_changes"] == {
        "changed": True,
        "lit_added": ["M2"],
        "lit_removed": ["M4"],
        "off_added": [],
        "off_removed": ["M2"],
        "blind_spot_added": ["M3"],
        "blind_spot_removed": [],
    }


def test_block_coverage_derives_implied_not_explicit_from_gate_veto() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.absence.values",
                source="web",
                evidence_type="acquisition.absence.values",
                content="Crawled owned page https://acme.example/about; no explicit values section terms were observed.",
                url="https://acme.example/about",
                metadata={"source_class": "acquisition_metadata", "checked_block": "values"},
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.2.absence.values",
                source="web",
                evidence_type="acquisition.absence.values",
                content="Crawled owned page https://acme.example/careers; no explicit values section terms were observed.",
                url="https://acme.example/careers",
                metadata={"source_class": "acquisition_metadata", "checked_block": "values"},
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.2.absence.vision",
                source="web",
                evidence_type="acquisition.absence.vision",
                content="Crawled owned page https://acme.example/careers; no explicit vision section terms were observed.",
                url="https://acme.example/careers",
                metadata={"source_class": "acquisition_metadata", "checked_block": "vision"},
            ),
        ],
    )
    interpretation = BrandInterpretation(
        brand_name="Acme",
        url="https://acme.example",
        blocks={
            "vision": {
                "detected": False,
                "content": "",
                "confidence": "low",
                "detection_provenance": {
                    "llm_detected": True,
                    "gate_detected": False,
                    "final_detected": False,
                    "final_source": "gate_rejected",
                    "gate_reason": "vision_structural_gate_rejected",
                },
            },
            "values": {
                "detected": False,
                "content": "",
                "confidence": "low",
                "detection_provenance": {
                    "llm_detected": True,
                    "gate_detected": False,
                    "final_detected": False,
                    "final_source": "gate_rejected",
                    "gate_reason": "values_structural_gate_rejected",
                },
            },
        },
        evidence_refs={},
    )

    coverage = block_coverage(pack, interpretation)

    # /about emitted no vision absence record, so it carried vision terms:
    # the LLM-read, gate-vetoed vision is implicit rather than verified absent.
    assert coverage["vision"]["status"] == "implied_not_explicit"
    # Every checked strategic surface lacked values terms, so the gate veto
    # resolves to verified absence even though the LLM claimed a detection.
    assert coverage["values"]["status"] == "verified_absent"
    limitations = coverage_limitations(coverage)
    assert "coverage:vision_implied_not_explicit" in limitations
    assert "coverage:values_verified_absent" in limitations
