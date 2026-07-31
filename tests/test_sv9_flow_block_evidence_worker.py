from src.external_identity_provenance import build_external_identity_provenance
from src.sv9_flow.block_evidence_worker import (
    BLOCK_EVIDENCE_IDENTITY_GATE_VERSION,
    BLOCK_EVIDENCE_SHORTLIST_VERSION,
    EVALUATION_EVIDENCE_REFS_VERSION,
    BlockEvidenceShortlist,
    build_block_evidence_identity_quarantine,
    build_block_evidence_shortlists,
    build_evaluation_evidence_refs,
)
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_identity import canonical_evidence_id


def test_block_evidence_shortlists_are_deterministic_and_block_specific() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme helps teams ship faster.",
            ),
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="coherencia.tone_consistency",
                content="The brand voice is direct, pragmatic, professional, and human.",
                confidence="high",
            ),
            EvidenceRecord(
                ref="features.1",
                source="legacy_feature",
                evidence_type="diferenciacion.positioning_clarity",
                content="The brand idea is a distinctive platform concept.",
                confidence="high",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(
        pack,
        blocks=("personality", "brand_idea"),
        limit=2,
    )

    assert shortlists == build_block_evidence_shortlists(pack, blocks=("personality", "brand_idea"), limit=2)
    assert shortlists["personality"][0] == "features.0"
    assert shortlists["brand_idea"][0] == "features.1"


def test_shortlist_is_invariant_to_provider_order_refs_and_volatile_metadata() -> None:
    def record(ref: str, content: str, *, score: float) -> EvidenceRecord:
        return EvidenceRecord(
            ref=ref,
            source="exa",
            evidence_type="external_proof.external_mentions",
            content=content,
            url=f"https://proof.example/{content.split()[0].lower()}",
            confidence="low" if score < 0.5 else "high",
            metadata={
                "source_class": "external_proof",
                "intent": "external_mentions",
                "score": score,
                "published_date": f"2026-07-{int(score * 10) + 10}",
                "result_group": "mentions",
            },
        )

    first_records = [
        record("raw_inputs.2.exa.mentions.0", "Mission reward mechanism", score=0.1),
        record("raw_inputs.2.exa.mentions.1", "Mission purpose for teams", score=0.9),
        record("raw_inputs.2.exa.mentions.2", "Mission enables progress", score=0.3),
    ]
    second_records = [
        record("raw_inputs.8.exa.news.7", "Mission enables progress", score=0.95),
        record("raw_inputs.8.exa.news.3", "Mission reward mechanism", score=0.8),
        record("raw_inputs.8.exa.news.5", "Mission purpose for teams", score=0.05),
    ]
    first = BrandEvidencePack("Acme", "https://acme.example", first_records)
    second = BrandEvidencePack("Acme", "https://acme.example", second_records)

    first_refs = build_block_evidence_shortlists(first, blocks=("mission",), limit=2)["mission"]
    second_refs = build_block_evidence_shortlists(second, blocks=("mission",), limit=2)["mission"]
    first_ids = [canonical_evidence_id(next(item for item in first_records if item.ref == ref)) for ref in first_refs]
    second_ids = [canonical_evidence_id(next(item for item in second_records if item.ref == ref)) for ref in second_refs]

    assert first_ids == second_ids


def test_shortlist_quarantines_external_profiles_without_deleting_them() -> None:
    profile = EvidenceRecord(
        ref="raw_inputs.2.exa.profiles.0",
        source="exa",
        evidence_type="external_proof.external_profiles",
        content=(
            "Altitude is an AI automation company with a mission, values, "
            "vision, and distinctive platform."
        ),
        url="https://getaltitudeai.com",
        metadata={
            "source_class": "external_proof",
            "identity_match": "brand_name",
            "result_group": "profiles",
            "intent": "external_profiles",
        },
    )
    owned = EvidenceRecord(
        ref="raw_inputs.1",
        source="web",
        evidence_type="raw_input",
        content="Our mission is to make business money programmable.",
        url="https://altitude.xyz",
    )
    pack = BrandEvidencePack(
        brand_name="Altitude",
        url="https://altitude.xyz",
        evidence=[profile, owned],
    )

    shortlists = build_block_evidence_shortlists(
        pack,
        blocks=("mission",),
        limit=5,
    )
    quarantine = build_block_evidence_identity_quarantine(pack)

    assert shortlists["mission"] == ["raw_inputs.1"]
    assert profile in pack.evidence
    assert quarantine == [
        {
            "ref": profile.ref,
            "url": profile.url,
            "reason_codes": ["external_profile_not_strategic_evidence"],
        }
    ]
    assert (
        BLOCK_EVIDENCE_IDENTITY_GATE_VERSION
        == "sv9-flow-block-evidence-identity-gate-v1"
    )


def test_shortlist_quarantines_review_gated_external_identity_provenance() -> None:
    provenance = build_external_identity_provenance(
        provider="exa",
        subject_url="https://altitude.xyz",
        source_url="https://altitude.fi",
        matched_alias="Altitude",
        match_method="alias_in_host",
        match_score=1.0,
        collector_source_class="related_unresolved",
        collector_relation="unresolved",
        requires_human_review=True,
    )
    record = EvidenceRecord(
        ref="raw_inputs.2.exa.news.0",
        source="exa",
        evidence_type="external_proof.news",
        content="Altitude announces its mission and platform.",
        url="https://altitude.fi",
        metadata={
            "source_class": "external_proof",
            "identity_match": "brand_name",
            "result_group": "news",
            "external_identity_provenance": provenance,
        },
    )
    pack = BrandEvidencePack(
        brand_name="Altitude",
        url="https://altitude.xyz",
        evidence=[record],
    )

    shortlists = build_block_evidence_shortlists(
        pack,
        blocks=("mission",),
    )
    quarantine = build_block_evidence_identity_quarantine(pack)

    assert shortlists["mission"] == []
    assert "external_identity_requires_human_review" in quarantine[0][
        "reason_codes"
    ]


def test_shortlist_keeps_reproducible_external_press_identity() -> None:
    provenance = build_external_identity_provenance(
        provider="exa",
        subject_url="https://example.com",
        source_url="https://press.test/story",
        matched_alias="Example",
        match_method="alias_in_title",
        match_score=0.95,
        collector_source_class="external",
        collector_relation="external",
        requires_human_review=False,
    )
    record = EvidenceRecord(
        ref="raw_inputs.2.exa.news.0",
        source="exa",
        evidence_type="external_proof.news",
        content="Example launches a platform that advances its mission.",
        url="https://press.test/story",
        metadata={
            "source_class": "external_proof",
            "identity_match": "brand_name",
            "result_group": "news",
            "external_identity_provenance": provenance,
        },
    )
    pack = BrandEvidencePack(
        brand_name="Example",
        url="https://example.com",
        evidence=[record],
    )

    shortlists = build_block_evidence_shortlists(
        pack,
        blocks=("mission",),
    )

    assert shortlists["mission"] == [record.ref]
    assert build_block_evidence_identity_quarantine(pack) == []


def test_shortlist_quarantines_semantic_mismatch_and_legacy_name_collision() -> None:
    semantic_mismatch = EvidenceRecord(
        ref="raw_inputs.2.exa.news.0",
        source="exa",
        evidence_type="external_proof.news",
        content="Liminal launches a GTM intelligence platform.",
        url="https://journal.test/liminal-launch",
        metadata={
            "source_class": "external_proof",
            "identity_match": "brand_name",
            "identity_match_llm": "none",
            "result_group": "news",
        },
    )
    legacy_collision = EvidenceRecord(
        ref="raw_inputs.2.exa.ai_visibility_results.3",
        source="exa",
        evidence_type="external_proof.ai_visibility",
        content="Altitude Consulting Services describes its mission.",
        url="https://altitudeconsultingservices.com",
        metadata={
            "source_class": "external_proof",
            "identity_match": "brand_name",
            "result_group": "ai_visibility_results",
        },
    )
    liminal = BrandEvidencePack(
        brand_name="Liminal",
        url="https://becomeliminal.com",
        evidence=[semantic_mismatch],
    )
    altitude = BrandEvidencePack(
        brand_name="Altitude",
        url="https://altitude.xyz",
        evidence=[legacy_collision],
    )

    assert build_block_evidence_shortlists(
        liminal,
        blocks=("mission",),
    )["mission"] == []
    assert build_block_evidence_shortlists(
        altitude,
        blocks=("mission",),
    )["mission"] == []
    assert build_block_evidence_identity_quarantine(liminal)[0][
        "reason_codes"
    ] == ["semantic_identity_mismatch"]
    assert build_block_evidence_identity_quarantine(altitude)[0][
        "reason_codes"
    ] == ["legacy_same_name_different_root_unresolved"]


def test_shortlist_keeps_unverified_name_only_identity_when_no_mismatch_is_proven() -> None:
    record = EvidenceRecord(
        ref="raw_inputs.2.exa.news.0",
        source="exa",
        evidence_type="external_proof.news",
        content="Liminal announces a new platform and mission.",
        url="https://journal.test/liminal",
        metadata={
            "source_class": "external_proof",
            "identity_match": "brand_name",
            "identity_match_llm": "unverified",
            "result_group": "news",
            "relevant_blocks": ["mission"],
            "stance": "supports",
            "specificity": "explicit",
            "semantic_labeling_version": "sv9-flow-evidence-labeling-v3",
        },
    )
    pack = BrandEvidencePack(
        brand_name="Liminal",
        url="https://becomeliminal.com",
        evidence=[record],
    )

    assert build_block_evidence_shortlists(
        pack,
        blocks=("mission",),
    )["mission"] == [record.ref]
    assert build_block_evidence_identity_quarantine(pack) == []


def test_shortlist_excludes_semantically_incidental_unverified_external_noise() -> None:
    unrelated = EvidenceRecord(
        ref="raw_inputs.2.exa.news.0",
        source="exa",
        evidence_type="external_proof.news",
        content=(
            "Altitude is a trading infrastructure platform with fast execution, "
            "advanced market data, and tools that help retail traders invest."
        ),
        url="https://airdrops.example/altitude",
        metadata={
            "source_class": "external_proof",
            "identity_match": "brand_name",
            "identity_match_llm": "unverified",
            "result_group": "news",
            "relevant_blocks": [],
            "stance": "neutral",
            "specificity": "incidental",
            "semantic_labeling_version": "sv9-flow-evidence-labeling-v3",
        },
    )
    owned = EvidenceRecord(
        ref="raw_inputs.1",
        source="web",
        evidence_type="raw_input",
        content="Run your business on stablecoins with one financial operating system.",
        url="https://altitude.xyz",
        metadata={"source_class": "owned_copy"},
    )
    pack = BrandEvidencePack(
        brand_name="Altitude",
        url="https://altitude.xyz",
        evidence=[unrelated, owned],
    )

    shortlists = build_block_evidence_shortlists(
        pack,
        blocks=("mission", "value_proposition"),
    )

    assert unrelated.ref not in shortlists["mission"]
    assert unrelated.ref not in shortlists["value_proposition"]
    assert owned.ref in shortlists["mission"]


def test_explicit_subject_domain_anchor_overrides_semantic_false_negative() -> None:
    record = EvidenceRecord(
        ref="raw_inputs.2.exa.mentions.10",
        source="exa",
        evidence_type="external_proof.external_mentions",
        content=(
            "Liminal GitHub repository and open source projects. "
            "Official blog: https://becomeliminal.com"
        ),
        url="https://github.com/becomeliminal",
        metadata={
            "source_class": "external_proof",
            "identity_match": "brand_name",
            "identity_match_llm": "none",
            "result_group": "mentions",
        },
    )
    pack = BrandEvidencePack(
        brand_name="Liminal",
        url="https://becomeliminal.com",
        evidence=[record],
    )

    assert build_block_evidence_shortlists(
        pack,
        blocks=("magnetism",),
    )["magnetism"] == [record.ref]
    assert build_block_evidence_identity_quarantine(pack) == []


def test_repository_proof_ranks_into_magnetism_shortlist() -> None:
    filler = [
        EvidenceRecord(
            ref=f"raw_inputs.1.subpage.{index}.chunk.1",
            source="web",
            evidence_type="raw_input",
            content=f"Owned page {index} mentions the developer community and engagement.",
        )
        for index in range(1, 7)
    ]
    repo = EvidenceRecord(
        ref="raw_inputs.3.github.repos.0",
        source="github",
        evidence_type="external_proof.repository",
        content="GitHub repository vercel/next.js. The React Framework. 140318 stars. 31297 forks. language: JavaScript.",
        confidence="high",
        metadata={"source_class": "external_proof"},
    )
    pack = BrandEvidencePack(
        brand_name="Vercel",
        url="https://vercel.com",
        evidence=filler + [repo],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("magnetism",), limit=5)

    assert "raw_inputs.3.github.repos.0" in shortlists["magnetism"]


def test_product_embodied_mission_evidence_ranks_above_product_navigation() -> None:
    pack = BrandEvidencePack(
        brand_name="Toteemi",
        url="https://toteemi.com",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Shop Ciclismo Equipamiento Zapatillas Maillots Chaquetas Mochilas.",
            ),
            EvidenceRecord(
                ref="raw_inputs.1",
                source="exa",
                evidence_type="external_proof.owned_confirmation",
                content="Convierte tu esfuerzo en descuentos. Una app que te paga por entrenar y una tienda donde puedes pagar con tu entrenamiento.",
                confidence="high",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("mission",), limit=2)

    assert shortlists["mission"][0] == "raw_inputs.1"


def test_explicit_owned_mission_heading_ranks_above_generic_product_copy() -> None:
    explicit = EvidenceRecord(
        ref="raw_inputs.1.subpage.2.chunk.1",
        source="web",
        evidence_type="raw_input",
        content="# On a mission to democratise finance\n\nFrom Singapore to London.",
        url="https://example.com/about",
        metadata={"source_class": "owned_copy"},
    )
    generic = [
        EvidenceRecord(
            ref=f"raw_inputs.1.subpage.{index}.chunk.2",
            source="web",
            evidence_type="raw_input",
            content=(
                "Build a global account that helps people invest, "
                "convert currencies, and enable instant transfers."
            ),
            url=f"https://example.com/features/{index}",
            metadata={"source_class": "owned_copy"},
        )
        for index in range(3, 9)
    ]
    pack = BrandEvidencePack(
        brand_name="Example",
        url="https://example.com",
        evidence=[*generic, explicit],
    )

    shortlist = build_block_evidence_shortlists(
        pack,
        blocks=("mission",),
        limit=5,
    )["mission"]

    assert shortlist[0] == explicit.ref


def test_values_shortlist_prefers_operational_principle_evidence_over_product_benefits() -> None:
    product_refs = [
        EvidenceRecord(
            ref=f"raw_inputs.product.{index}",
            source="web",
            evidence_type="raw_input",
            content="AI agents automate invoices, reduce coordination, and improve efficiency for finance teams.",
        )
        for index in range(5)
    ]
    policy_refs = [
        EvidenceRecord(
            ref="raw_inputs.legal.0",
            source="exa",
            evidence_type="external_proof.owned_confirmation",
            content=(
                "Data Processing Agreement. Customers may object to new sub-processors "
                "on reasonable data-protection grounds."
            ),
            confidence="high",
        ),
        EvidenceRecord(
            ref="raw_inputs.docs.0",
            source="exa",
            evidence_type="external_proof.owned_confirmation",
            content=(
                "Scribo is a free, EN 16931-compliant e-invoicing tool. "
                "The /api/v1 namespace is the public contract."
            ),
            confidence="high",
        ),
    ]
    pack = BrandEvidencePack(
        brand_name="Causa Prima",
        url="https://causaprima.ai",
        evidence=product_refs + policy_refs,
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("values",), limit=3)

    assert shortlists["values"][:2] == ["raw_inputs.legal.0", "raw_inputs.docs.0"]


def test_values_evaluation_refs_freeze_shortlist_with_adjacent_page_context() -> None:
    records = [
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
        EvidenceRecord(
            ref="raw_inputs.0.chunk.2",
            source="web",
            evidence_type="raw_input",
            content="A product benefit that also mentions values.",
        ),
    ]
    pack = BrandEvidencePack(
        brand_name="SoccerSolver",
        url="https://soccersolver.com",
        evidence=records,
    )
    shortlists = {
        "values": [
            "raw_inputs.1.subpage.3.chunk.5",
            "raw_inputs.0.chunk.2",
        ]
    }

    evaluation_refs = build_evaluation_evidence_refs(pack, shortlists)
    reordered_pack = BrandEvidencePack(
        brand_name=pack.brand_name,
        url=pack.url,
        evidence=list(reversed(records)),
    )

    assert evaluation_refs["values"] == [
        "raw_inputs.1.subpage.3.chunk.5",
        "raw_inputs.1.subpage.3.chunk.4",
        "raw_inputs.1.subpage.3.chunk.6",
        "raw_inputs.0.chunk.2",
    ]
    assert build_evaluation_evidence_refs(reordered_pack, shortlists) == evaluation_refs
    assert EVALUATION_EVIDENCE_REFS_VERSION == "sv9-flow-evaluation-evidence-refs-v1"


def test_vision_shortlist_promotes_agent_network_target_state() -> None:
    filler = [
        EvidenceRecord(
            ref=f"raw_inputs.product.{index}",
            source="web",
            evidence_type="raw_input",
            content="Feature overview for invoice handling, disputes, discounts, and payment operations.",
        )
        for index in range(5)
    ]
    target_state = EvidenceRecord(
        ref="raw_inputs.home.0",
        source="web",
        evidence_type="raw_input",
        content=(
            "The agent-to-agent network for finance teams. "
            "Where buyer and supplier finally meet. "
            "So money moves on the right terms, at the right time, on its own."
        ),
        confidence="high",
    )
    pack = BrandEvidencePack(
        brand_name="Causa Prima",
        url="https://causaprima.ai",
        evidence=filler + [target_state],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("vision",), limit=2)

    assert shortlists["vision"][0] == "raw_inputs.home.0"


def test_block_evidence_shortlist_serializes_version() -> None:
    item = BlockEvidenceShortlist(block="vision", evidence_refs=["raw_inputs.0"])

    assert item.to_dict() == {
        "version": BLOCK_EVIDENCE_SHORTLIST_VERSION,
        "block": "vision",
        "evidence_refs": ["raw_inputs.0"],
    }


def test_brand_idea_shortlist_prefers_textual_evidence_over_visual_signature_when_available() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="visual_signature.tile_signals.0",
                source="visual_signature",
                evidence_type="visual_tile_signal",
                content="Distinctive visual signature supports a brand idea.",
            ),
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="The core brand idea is an ownable platform methodology.",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("brand_idea",), limit=2)

    assert shortlists["brand_idea"][0] == "raw_inputs.0"


def test_brand_idea_shortlist_prioritizes_uniqueness_and_vocabulary_features() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="visual_signature",
                evidence_type="raw_input",
                content="Visual signature agreement payload with visual signature metrics.",
            ),
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="diferenciacion.uniqueness",
                content="unique_phrases and brand_vocabulary show an ownable differentiator_claimed.",
                confidence="high",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("brand_idea",), limit=2)

    assert shortlists["brand_idea"][0] == "features.0"


def test_magnetism_shortlist_prefers_market_evidence_over_visual_signature() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="visual_signature.tile_signals.0",
                source="visual_signature",
                evidence_type="visual_tile_signal",
                content="Distinctive visual polish for a magnetism tile.",
                metadata={"tile": "magnetism.MG5", "source_class": "visual_signal"},
            ),
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="vitalidad.momentum",
                content="Funding, press momentum, and revenue growth are visible.",
                confidence="high",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("magnetism",), limit=2)

    assert shortlists["magnetism"][0] == "features.0"
    assert "visual_signature.tile_signals.0" not in shortlists["magnetism"]


def test_magnetism_shortlist_prefers_preference_evidence_over_acquisition_metadata() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="entity_research_packet",
                evidence_type="raw_input",
                content='{"block_source_guidance": {"magnetism": ["audited_surface"]}}',
            ),
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="diferenciacion.positioning_clarity",
                content="A clear differentiator and native integration give buyers a reason to choose Acme.",
                confidence="high",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("magnetism",), limit=2)

    assert shortlists["magnetism"][0] == "features.0"
    assert "raw_inputs.0" not in shortlists["magnetism"]


def test_value_proposition_shortlist_prioritizes_financial_value_claims() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="coherencia.visual_consistency",
                content="Visual consistency and corporate style.",
                confidence="high",
            ),
            EvidenceRecord(
                ref="features.1",
                source="legacy_feature",
                evidence_type="diferenciacion.uniqueness",
                content="Traduce a euros el impacto del trabajo and turns intangibles into capital arguments.",
                confidence="high",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("value_proposition",), limit=2)

    assert shortlists["value_proposition"][0] == "features.1"


def test_mission_shortlist_prefers_owned_web_copy_over_derived_summaries() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="diferenciacion.positioning_clarity",
                content="A derived summary says Acme helps teams build faster.",
                confidence="high",
            ),
            EvidenceRecord(
                ref="raw_inputs.0",
                source="report_narrative",
                evidence_type="raw_input",
                content="The report narrative says Acme has momentum and a mission.",
            ),
            EvidenceRecord(
                ref="raw_inputs.1",
                source="web",
                evidence_type="raw_input",
                content="Acme helps support teams automate high-stakes calls.",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("mission",), limit=3)

    assert shortlists["mission"][0] == "raw_inputs.1"


def test_mission_shortlist_does_not_promote_acquisition_noise_over_owned_copy() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="social",
                evidence_type="raw_input",
                content="Failed to scrape social channels. We help teams automate support. followers_count: 0.",
            ),
            EvidenceRecord(
                ref="raw_inputs.1",
                source="web",
                evidence_type="raw_input",
                content="We help support teams automate high-stakes calls.",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("mission",), limit=2)

    assert shortlists["mission"] == ["raw_inputs.1"]


def test_vision_shortlist_prioritizes_owned_manifesto_aspiration() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Homepage product copy for scheduling automation.",
                metadata={"source_class": "owned_copy"},
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.chunk.4",
                source="web",
                evidence_type="raw_input",
                content=(
                    "Manifesto: our goal is to transform the experience underneath. "
                    "If we succeed, people get care when they need it."
                ),
                metadata={"source_class": "owned_copy"},
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("vision",), limit=2)

    assert shortlists["vision"][0] == "raw_inputs.0.subpage.1.chunk.4"


def test_values_shortlist_prioritizes_owned_belief_language() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="coherencia.tone_consistency",
                content="The brand sounds efficient and clear.",
                confidence="high",
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.chunk.3",
                source="web",
                evidence_type="raw_input",
                content="We believe teams should act with conviction, precision, and care.",
                metadata={"source_class": "owned_copy"},
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("values",), limit=2)

    assert shortlists["values"][0] == "raw_inputs.0.subpage.1.chunk.3"


def test_core_purpose_shortlist_penalizes_report_narrative_when_owned_copy_exists() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="report_narrative",
                evidence_type="raw_input",
                content="Acme exists to maximize market momentum and discover growth.",
            ),
            EvidenceRecord(
                ref="raw_inputs.1",
                source="web",
                evidence_type="raw_input",
                content="We help clinics free care teams from manual call queues.",
            ),
        ],
    )

    shortlists = build_block_evidence_shortlists(pack, blocks=("core_purpose",), limit=2)

    assert shortlists["core_purpose"][0] == "raw_inputs.1"


def test_semantic_labeling_rescues_relevant_record_without_keyword_hits() -> None:
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

    shortlists = build_block_evidence_shortlists(pack, blocks=("mission",), limit=2)

    assert shortlists["mission"] == ["raw_inputs.0"]
