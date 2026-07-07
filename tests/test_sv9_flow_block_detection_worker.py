from src.sv9_flow.block_detection_worker import resolve_block_detection
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord


def test_values_detection_requires_explicit_values_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="tone",
                content="Direct, pragmatic, human-centric tone with clarity and efficiency.",
            )
        ],
    )

    decision = resolve_block_detection("values", pack, evidence_refs=["features.0"])

    assert decision.outcome == "insufficient_evidence"
    assert decision.limitation_code == "values_structural_gate_rejected"


def test_values_detection_accepts_explicit_values_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Our values are transparency, accountability, and care for customers.",
            )
        ],
    )

    decision = resolve_block_detection("values", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["our values", "values are"]
    assert decision.limitation_code == ""


def test_values_detection_rejects_financial_value_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme translates intangible assets into clear financial value for investors.",
            )
        ],
    )

    decision = resolve_block_detection("values", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "insufficient_evidence"
    assert decision.support_terms == []


def test_vision_detection_accepts_explicit_future_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Our vision is to become the next generation operating system for finance teams.",
            )
        ],
    )

    decision = resolve_block_detection("vision", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["our vision", "vision is", "next generation", "operating system for"]


def test_vision_detection_accepts_json_escaped_section_heading_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="exa",
                evidence_type="raw_input",
                content=r"## One spending solution\n\nOur vision\n\nGo beyond the books.",
            )
        ],
    )

    decision = resolve_block_detection("vision", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["our vision"]


def test_vision_detection_accepts_category_operating_system_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Mafer",
        url="https://mafer.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="coherencia.messaging_consistency",
                content="Third-party category: AI operating system for Formulation R&D.",
            )
        ],
    )

    decision = resolve_block_detection("vision", pack, evidence_refs=["features.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["operating system for"]


def test_vision_detection_accepts_manifesto_goal_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Prosper",
        url="https://prosper.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.chunk.3",
                source="web",
                evidence_type="raw_input",
                content=(
                    "At Prosper AI, we believe patient-facing assistants are necessary but not sufficient. "
                    "Our goal is to build an orchestration platform for the entire patient journey."
                ),
                metadata={"source_class": "owned_copy"},
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.chunk.5",
                source="web",
                evidence_type="raw_input",
                content="If we succeed, we do not just cut wait time; we remove friction from the entire patient journey.",
                metadata={"source_class": "owned_copy"},
            ),
        ],
    )

    decision = resolve_block_detection(
        "vision",
        pack,
        evidence_refs=["raw_inputs.0.subpage.1.chunk.3", "raw_inputs.0.subpage.1.chunk.5"],
    )

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["our goal is", "if we succeed"]


def test_vision_detection_rejects_generic_future_of_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="coherencia.messaging_consistency",
                content="Third parties say Acme aims to shape the future of artificial intelligence.",
            )
        ],
    )

    decision = resolve_block_detection("vision", pack, evidence_refs=["features.0"])

    assert decision.outcome == "insufficient_evidence"
    assert decision.support_terms == []


def test_vision_detection_rejects_positioning_inference() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme helps financial teams translate intangible assets into defensible arguments.",
            )
        ],
    )

    decision = resolve_block_detection("vision", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "insufficient_evidence"
    assert decision.limitation_code == "vision_structural_gate_rejected"


def test_magnetism_detection_rejects_visual_polish_only() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="visual_signature.tile.0",
                source="visual_signature",
                evidence_type="visual_tile_signal",
                content="Visual polish, distinctive copy, and confident first impression.",
            )
        ],
    )

    decision = resolve_block_detection("magnetism", pack, evidence_refs=["visual_signature.tile.0"])

    assert decision.outcome == "insufficient_evidence"
    assert decision.limitation_code == "magnetism_structural_gate_rejected"


def test_magnetism_detection_accepts_negative_engagement_as_evaluable_evidence() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="report_narrative",
                evidence_type="raw_input",
                content=(
                    "The brand suffers from stagnation in digital activity and a lack of active engagement "
                    "across public social channels."
                ),
            )
        ],
    )

    decision = resolve_block_detection("magnetism", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.limitation_code == ""
    assert decision.weaken_terms == ["lack of active engagement", "stagnation"]


def test_magnetism_detection_does_not_treat_acquisition_failure_as_negative_evidence() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="social",
                evidence_type="raw_input",
                content="Failed to scrape social channels; followers_count: 0; avg_engagement_rate: 0.0.",
            )
        ],
    )

    decision = resolve_block_detection("magnetism", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "insufficient_evidence"
    assert decision.limitation_code == "magnetism_structural_gate_rejected"
    assert decision.weaken_terms == []


def test_magnetism_detection_ignores_acquisition_noise_even_with_support_terms() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="social",
                evidence_type="raw_input",
                content=(
                    "Failed to scrape social channels; community engagement unavailable; "
                    "followers_count: 0; avg_engagement_rate: 0.0."
                ),
            )
        ],
    )

    decision = resolve_block_detection("magnetism", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "insufficient_evidence"
    assert decision.support_terms == []


def test_values_detection_accepts_operating_culture_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="about",
                evidence_type="raw_input",
                content="What unites us is relentless focus, fast execution, and deep care for craftsmanship.",
            )
        ],
    )

    decision = resolve_block_detection("values", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == [
        "what unites us",
        "relentless focus",
        "fast execution",
        "deep care",
        "craftsmanship",
    ]


def test_magnetism_detection_accepts_structural_momentum_evidence() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Revenue growth, community engagement, and press momentum are visible in the evidence.",
            )
        ],
    )

    decision = resolve_block_detection("magnetism", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["momentum", "community engagement", "community", "revenue growth", "press"]


def test_magnetism_detection_accepts_owned_product_hook_evidence() -> None:
    pack = BrandEvidencePack(
        brand_name="Darwin Biomedical",
        url="https://darwinbiomedical.com",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content=(
                    "MICHELANGELO. Descubre el primer andador inteligente con prevención activa de caídas. "
                    "Seguridad & libertad."
                ),
            )
        ],
    )

    decision = resolve_block_detection("magnetism", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == [
        "prevención activa de caídas",
        "seguridad & libertad",
        "primer andador inteligente",
    ]


def test_mission_detection_accepts_action_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="We help support teams automate high-stakes calls.",
            )
        ],
    )

    decision = resolve_block_detection("mission", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["we help", "automate"]


def test_mission_detection_accepts_spanish_operating_mission_language() -> None:
    pack = BrandEvidencePack(
        brand_name="COFI",
        url="https://cofi.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content=(
                    "Consultora boutique especialista en valoración de activos intangibles. "
                    "Convertimos tus intangibles en valor medible y defendible, en euros."
                ),
            )
        ],
    )

    decision = resolve_block_detection("mission", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["convertimos"]


def test_mission_detection_accepts_operational_outcome_language_without_mission_heading() -> None:
    pack = BrandEvidencePack(
        brand_name="Prosper",
        url="https://prosper.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content=(
                    "Prosper AI: Voice Agents for Patient Access & Prior Auth Automation. "
                    "Tailor-made to handle patient, provider, and payor calls, so your team can focus on care."
                ),
            )
        ],
    )

    decision = resolve_block_detection("mission", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["so your team can"]


def test_mission_detection_accepts_security_fix_operating_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Staris",
        url="https://staris.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content=(
                    "Staris proves which vulnerability candidates are exploitable in your running app "
                    "and ships the fix at your release cadence. Staris cuts noise by 99% before findings reach your team."
                ),
            )
        ],
    )

    decision = resolve_block_detection("mission", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == ["proves which", "ships the fix", "cuts noise"]


def test_mission_detection_accepts_agent_operating_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Hermes",
        url="https://hermes.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content=(
                    "Hermes Agent — the open-source agent that grows with you. "
                    "The self-improving AI agent built by Nous Research. Focused Automation for reports and backups."
                ),
            )
        ],
    )

    decision = resolve_block_detection("mission", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "supports_detection"
    assert decision.support_terms == [
        "open-source agent that",
        "self-improving ai agent",
        "focused automation",
    ]


def test_mission_detection_rejects_metadata_only_mission_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="entity_research_packet",
                evidence_type="raw_input",
                content='{"block_source_guidance": {"mission": ["mission_about"]}}',
            )
        ],
    )

    decision = resolve_block_detection("mission", pack, evidence_refs=["raw_inputs.0"])

    assert decision.outcome == "insufficient_evidence"


def test_block_detection_decision_serializes_for_debug_payloads() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Revenue growth and press momentum are visible.",
            )
        ],
    )

    decision = resolve_block_detection("magnetism", pack, evidence_refs=["raw_inputs.0"])

    assert decision.to_dict() == {
        "version": "sv9-flow-block-detection-policy-v6",
        "block": "magnetism",
        "outcome": "supports_detection",
        "evidence_refs": ["raw_inputs.0"],
        "support_terms": ["momentum", "revenue growth", "press"],
        "weaken_terms": [],
        "limitation_code": "",
    }


def test_magnetism_gate_accepts_repository_proof_as_gravity() -> None:
    from src.sv9_flow.calibration_terms import magnetism_families

    pack = BrandEvidencePack(
        brand_name="Vercel",
        url="https://vercel.com",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.3.github.repos.0",
                source="github",
                evidence_type="external_proof.repository",
                content="GitHub repository vercel/next.js. The React Framework. 140318 stars. 31297 forks. language: JavaScript.",
                confidence="high",
                metadata={"source_class": "external_proof"},
            )
        ],
    )

    decision = resolve_block_detection("magnetism", pack, evidence_refs=["raw_inputs.3.github.repos.0"])

    assert decision.outcome == "supports_detection"
    assert {"github repository", "stars", "forks"} <= set(decision.support_terms)
    assert set(decision.support_terms) & magnetism_families()["gravity"]
