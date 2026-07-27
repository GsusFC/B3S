from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_coverage import block_coverage
from src.sv9_flow.block_evidence_worker import build_block_evidence_shortlists
from src.sv9_flow.evidence_identity import canonical_evidence_ref
from src.sv9_flow.interpretation_llm_worker import (
    FLOW_INTERPRETATION_PROMPT_VERSION,
    block_interpretation_response_schema,
    brand_interpretation_response_schema,
    build_brand_interpretation_with_llm,
    normalize_llm_interpretation_response,
    _block_user_prompt,
    _restore_interpretation_refs,
    _user_prompt,
)


def test_interpretation_prompts_are_invariant_to_ref_order_and_volatile_metadata() -> None:
    def record(ref: str, content: str, url: str, *, score: float) -> EvidenceRecord:
        return EvidenceRecord(
            ref=ref,
            source="exa",
            evidence_type="external_proof.external_mentions",
            content=content,
            url=url,
            confidence="high" if score > 0.5 else "low",
            metadata={
                "source_class": "external_proof",
                "intent": "external_mentions",
                "identity_match": "brand_name",
                "identity_match_llm": "brand_name",
                "relevant_blocks": ["value_proposition", "mission"],
                "stance": "supports",
                "specificity": "explicit",
                "score": score,
                "published_date": f"2026-07-{int(score * 10) + 10}",
                "result_group": "mentions",
            },
        )

    first = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            record(
                "raw_inputs.2.exa.mentions.0",
                "Acme helps teams close faster.",
                "https://proof.example/acme",
                score=0.1,
            ),
            record(
                "raw_inputs.2.exa.mentions.1",
                "Acme turns every close into verified savings.",
                "https://proof.example/savings",
                score=0.9,
            ),
        ],
    )
    second = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            record(
                "raw_inputs.8.exa.news.4",
                "Acme turns every close into verified savings.",
                "https://proof.example/savings",
                score=0.2,
            ),
            record(
                "raw_inputs.8.exa.news.7",
                "Acme helps teams close faster.",
                "https://proof.example/acme",
                score=0.8,
            ),
        ],
    )
    first_shortlists = build_block_evidence_shortlists(first)
    second_shortlists = build_block_evidence_shortlists(second)

    assert _user_prompt(
        first,
        block_evidence_shortlists=first_shortlists,
    ) == _user_prompt(
        second,
        block_evidence_shortlists=second_shortlists,
    )
    assert _block_user_prompt(
        first,
        block="value_proposition",
        evidence_refs=first_shortlists["value_proposition"],
    ) == _block_user_prompt(
        second,
        block="value_proposition",
        evidence_refs=second_shortlists["value_proposition"],
    )


def test_model_facing_aliases_restore_current_provenance_refs() -> None:
    record = EvidenceRecord(
        ref="raw_inputs.2.exa.mentions.4",
        source="exa",
        evidence_type="external_proof.external_mentions",
        content="Acme helps teams close faster.",
        url="https://proof.example/acme",
        metadata={"source_class": "external_proof"},
    )
    pack = BrandEvidencePack("Acme", "https://acme.example", [record])
    raw = {
        "blocks": {
            "mission": {
                "detected": True,
                "content": "Help teams close faster.",
                "confidence": "high",
                "evidence_refs": [canonical_evidence_ref(record)],
                "rationale": "The evidence states the outcome.",
            }
        },
        "limitations": [],
    }

    restored = _restore_interpretation_refs(raw, pack)

    assert restored["blocks"]["mission"]["evidence_refs"] == [record.ref]


def test_normalize_llm_interpretation_requires_valid_evidence_refs() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme helps finance teams close faster.",
            )
        ],
    )
    raw = {
        "prompt_version": FLOW_INTERPRETATION_PROMPT_VERSION,
        "blocks": {
            "mission": {
                "detected": True,
                "content": "Help finance teams close faster.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The homepage states this directly.",
            },
            "vision": {
                "detected": True,
                "content": "Own the finance category.",
                "confidence": "medium",
                "evidence_refs": ["missing.ref"],
                "rationale": "Invalid ref should prevent detection.",
            },
        },
        "limitations": ["limited_sample"],
    }

    interpretation = normalize_llm_interpretation_response(raw, pack)

    assert interpretation.blocks["mission"]["detected"] is True
    assert interpretation.evidence_refs["mission"] == ["raw_inputs.0"]
    assert interpretation.blocks["vision"]["detected"] is False
    assert "vision_dropped_missing_evidence_refs" in interpretation.limitations
    assert "limited_sample" in interpretation.limitations


def test_normalize_llm_interpretation_rejects_refs_outside_block_shortlist() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(ref="raw_inputs.0", source="homepage", evidence_type="raw_input", content="Mission text."),
            EvidenceRecord(ref="features.0", source="feature", evidence_type="tone", content="Personality text."),
        ],
    )
    raw = {
        "blocks": {
            "mission": {
                "detected": True,
                "content": "Mission text.",
                "confidence": "high",
                "evidence_refs": ["features.0"],
                "rationale": "Existing ref, wrong shortlist.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["mission"]["detected"] is False
    assert "mission_dropped_missing_evidence_refs" in interpretation.limitations


def test_normalize_llm_interpretation_can_accept_classified_evidence_without_gates() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme is decentralized, independent, stable, and private.",
                metadata={"source_class": "owned_copy"},
            )
        ],
    )
    raw = {
        "blocks": {
            "values": {
                "detected": True,
                "content": "Decentralization, independence, stability, and privacy.",
                "confidence": "medium",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The listed principles are stated in owned copy.",
            }
        },
        "limitations": [],
    }

    gated = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"values": ["raw_inputs.0"]},
    )
    ungated = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"values": ["raw_inputs.0"]},
        gate_authority="disabled",
    )

    assert gated.blocks["values"]["detected"] is False
    assert gated.blocks["values"]["detection_provenance"]["final_source"] == "gate_rejected"
    assert ungated.blocks["values"]["detected"] is True
    assert ungated.evidence_refs["values"] == ["raw_inputs.0"]
    assert ungated.blocks["values"]["detection_provenance"]["final_source"] == "llm_classified_evidence"


def test_values_detection_requires_explicit_values_evidence() -> None:
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
    raw = {
        "blocks": {
            "values": {
                "detected": True,
                "content": "Clarity and efficiency as values.",
                "confidence": "medium",
                "evidence_refs": ["features.0"],
                "rationale": "The tone implies values.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"values": ["features.0"]},
    )

    assert interpretation.blocks["values"]["detected"] is False
    assert "values_structural_gate_rejected" in interpretation.limitations
    assert interpretation.blocks["values"]["detection_provenance"] == {
        "llm_detected": True,
        "gate_detected": False,
        "final_detected": False,
        "final_source": "gate_rejected",
        "gate_reason": "values_structural_gate_rejected",
    }


def test_values_detection_accepts_explicit_values_evidence() -> None:
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
    raw = {
        "blocks": {
            "values": {
                "detected": True,
                "content": "Transparency, accountability, and customer care.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The page explicitly lists values.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"values": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["values"]["detected"] is True
    assert interpretation.evidence_refs["values"] == ["raw_inputs.0"]


def test_sensitive_block_gate_uses_deterministic_shortlist_not_llm_selected_refs() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="momentum",
                evidence_type="vitalidad.momentum",
                content="Revenue growth, community engagement, and press momentum are visible.",
            ),
            EvidenceRecord(
                ref="raw_inputs.0",
                source="social",
                evidence_type="raw_input",
                content="Failed to scrape social channels; followers_count: 0; avg_engagement_rate: 0.0.",
            ),
        ],
    )
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "Acme shows momentum and community engagement.",
                "confidence": "high",
                "evidence_refs": ["features.0"],
                "rationale": "The evidence mentions momentum.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"magnetism": ["features.0", "raw_inputs.0"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is True
    assert "magnetism_structural_negative_evidence" not in interpretation.limitations


def test_gate_positive_llm_negative_never_grants_detection() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="momentum",
                evidence_type="vitalidad.momentum",
                content="Press momentum and market pull are visible in recent product launches.",
            )
        ],
    )
    raw = {
        "blocks": {
            "magnetism": {
                "detected": False,
                "content": "",
                "confidence": "low",
                "evidence_refs": [],
                "rationale": "Not enough direct community data.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"magnetism": ["features.0"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is False
    assert interpretation.blocks["magnetism"]["content"] == ""
    assert "magnetism" not in interpretation.evidence_refs
    assert "magnetism_gate_positive_llm_negative" in interpretation.limitations
    assert "magnetism_llm_detection_overridden_by_gate" not in interpretation.limitations
    assert interpretation.blocks["magnetism"]["detection_provenance"] == {
        "llm_detected": False,
        "gate_detected": True,
        "final_detected": False,
        "final_source": "llm_rejected_gate_candidate",
        "gate_reason": "support_terms:momentum,press,market pull",
        "review_queue_reason": "gate_positive_llm_negative",
    }


def test_mission_gate_positive_llm_negative_stays_undetected() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="We help support teams automate high-stakes calls.",
            )
        ],
    )
    raw = {
        "blocks": {
            "mission": {
                "detected": False,
                "content": "",
                "confidence": "low",
                "evidence_refs": [],
                "rationale": "Provider failed to extract it.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["mission"]["detected"] is False
    assert interpretation.blocks["mission"]["content"] == ""
    assert "mission_gate_positive_llm_negative" in interpretation.limitations
    provenance = interpretation.blocks["mission"]["detection_provenance"]
    assert provenance["final_source"] == "llm_rejected_gate_candidate"
    assert provenance["review_queue_reason"] == "gate_positive_llm_negative"


def test_mission_detection_accepts_product_embodied_strategy_evidence() -> None:
    pack = BrandEvidencePack(
        brand_name="Toteemi",
        url="https://toteemi.com",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Convierte tu esfuerzo en descuentos. Una app que te paga por entrenar y una tienda donde puedes pagar con tu entrenamiento.",
            )
        ],
    )
    raw = {
        "blocks": {
            "mission": {
                "detected": True,
                "content": "Convert athletic effort into rewards users can spend.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The copy states that training effort is converted into discounts and payment power.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["mission"]["detected"] is True
    assert interpretation.evidence_refs["mission"] == ["raw_inputs.0"]
    assert interpretation.blocks["mission"]["detection_provenance"]["final_source"] == "llm_confirmed_by_gate"


def test_sensitive_detection_requires_llm_cited_refs_even_when_gate_supports() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Our mission is to help support teams automate high-stakes calls.",
            )
        ],
    )
    raw = {
        "blocks": {
            "mission": {
                "detected": True,
                "content": "Automate high-stakes calls for support teams.",
                "confidence": "high",
                "evidence_refs": [],
                "rationale": "Stated on the homepage.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["mission"]["detected"] is False
    assert interpretation.blocks["mission"]["content"] == ""
    assert interpretation.blocks["mission"]["rejected_content"] == "Automate high-stakes calls for support teams."
    assert "mission" not in interpretation.evidence_refs
    assert "mission_dropped_missing_evidence_refs" in interpretation.limitations
    assert interpretation.blocks["mission"]["detection_provenance"]["final_source"] == "llm_missing_evidence_refs"


def test_magnetism_market_momentum_only_is_marked_as_limited() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="vitalidad.momentum",
                content="Funding, press momentum, and revenue growth are visible.",
            )
        ],
    )
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "Acme has market momentum.",
                "confidence": "medium",
                "evidence_refs": ["features.0"],
                "rationale": "The evidence mentions funding and press.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"magnetism": ["features.0"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is True
    assert "magnetism_market_momentum_only" in interpretation.limitations
    assert "magnetism_no_owned_hook_evidence" in interpretation.limitations
    assert "magnetism_no_preference_evidence" in interpretation.limitations
    assert "magnetism_no_belonging_status_evidence" in interpretation.limitations
    assert "magnetism_no_gravity_evidence" not in interpretation.limitations


def test_magnetism_preference_evidence_does_not_mark_preference_as_missing() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="diferenciacion.positioning_clarity",
                content="A clear differentiator and native integration give buyers a reason to choose Acme.",
            )
        ],
    )
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "Acme has preference evidence.",
                "confidence": "medium",
                "evidence_refs": ["features.0"],
                "rationale": "The evidence names a differentiator and reason to choose.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"magnetism": ["features.0"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is True
    assert "magnetism_market_momentum_only" not in interpretation.limitations
    assert "magnetism_no_owned_hook_evidence" in interpretation.limitations
    assert "magnetism_no_preference_evidence" not in interpretation.limitations
    assert "magnetism_no_belonging_status_evidence" in interpretation.limitations
    assert "magnetism_no_gravity_evidence" in interpretation.limitations


def test_magnetism_owned_product_hook_does_not_mark_owned_hook_as_missing() -> None:
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
            ),
            EvidenceRecord(
                ref="raw_inputs.1",
                source="external_profile",
                evidence_type="external_proof",
                content="Darwin Biomed has a funding announcement.",
            ),
        ],
    )
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "Darwin Biomedical combines product pull with external validation.",
                "confidence": "medium",
                "evidence_refs": ["raw_inputs.0", "raw_inputs.1"],
                "rationale": "The owned copy names active fall prevention and the external profile validates momentum.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"magnetism": ["raw_inputs.0", "raw_inputs.1"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is True
    assert "magnetism_market_momentum_only" not in interpretation.limitations
    assert "magnetism_no_owned_hook_evidence" not in interpretation.limitations
    assert "magnetism_no_belonging_status_evidence" in interpretation.limitations


def test_sensitive_blocks_keep_only_llm_cited_refs() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="vitalidad.momentum",
                content="Funding and press momentum are visible.",
            ),
            EvidenceRecord(
                ref="features.1",
                source="legacy_feature",
                evidence_type="diferenciacion.positioning_clarity",
                content="A native integration gives buyers a reason to choose Acme.",
            ),
        ],
    )
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "Acme has market momentum.",
                "confidence": "medium",
                "evidence_refs": ["features.0"],
                "rationale": "The evidence mentions funding and press.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"magnetism": ["features.0", "features.1"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is True
    assert interpretation.evidence_refs["magnetism"] == ["features.0"]
    assert "magnetism_no_preference_evidence" not in interpretation.limitations


def test_core_purpose_based_on_derived_strategy_evidence_is_marked_limited() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="diferenciacion.positioning_clarity",
                content="A derived summary says Acme exists to maximize efficiency.",
            ),
            EvidenceRecord(
                ref="raw_inputs.0",
                source="report_narrative",
                evidence_type="raw_input",
                content="The narrative says Acme has a clear purpose.",
            ),
        ],
    )
    raw = {
        "blocks": {
            "core_purpose": {
                "detected": True,
                "content": "Maximize efficiency for teams.",
                "confidence": "medium",
                "evidence_refs": ["features.0", "raw_inputs.0"],
                "rationale": "The derived evidence says this.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"core_purpose": ["features.0", "raw_inputs.0"]},
    )

    assert "core_purpose_derived_strategy_evidence" in interpretation.limitations


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
    raw = {
        "blocks": {
            "values": {
                "detected": True,
                "content": "Clarity and investor value.",
                "confidence": "medium",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The evidence mentions value.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"values": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["values"]["detected"] is False
    assert "values_structural_gate_rejected" in interpretation.limitations


def test_vision_detection_requires_explicit_future_evidence() -> None:
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
    raw = {
        "blocks": {
            "vision": {
                "detected": True,
                "content": "A future where intangible assets are defensible.",
                "confidence": "medium",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The positioning implies a future direction.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"vision": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["vision"]["detected"] is False
    assert "vision_structural_gate_rejected" in interpretation.limitations


def test_vision_detection_accepts_explicit_future_evidence() -> None:
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
    raw = {
        "blocks": {
            "vision": {
                "detected": True,
                "content": "Become the next generation operating system for finance teams.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The evidence states a vision directly.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"vision": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["vision"]["detected"] is True
    assert interpretation.evidence_refs["vision"] == ["raw_inputs.0"]


def test_magnetism_detection_requires_structural_momentum_evidence() -> None:
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
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "The brand has magnetism through polish.",
                "confidence": "medium",
                "evidence_refs": ["visual_signature.tile.0"],
                "rationale": "The visual system is polished.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"magnetism": ["visual_signature.tile.0"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is False
    assert "magnetism_structural_gate_rejected" in interpretation.limitations


def test_adjudicator_can_rescue_sensitive_gate_false_negative_with_literal_quote() -> None:
    class AdjudicatorLLM:
        api_key = "test"

        def _call_json(self, *args, **kwargs):
            return {
                "state": "ok",
                "quote": "Una app que te paga por entrenar",
                "ref": "raw_inputs.0",
                "reason": "The reward mechanism creates repeat motivation and preference.",
                "confidence": "high",
                "inference_type": "product_embodied_strategy",
            }

    pack = BrandEvidencePack(
        brand_name="Toteemi",
        url="https://toteemi.com",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Convierte tu esfuerzo en descuentos. Una app que te paga por entrenar.",
            )
        ],
    )
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "Toteemi uses rewards to make training repeatable and desirable.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The page frames training as paid/rewarded behavior.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        adjudicator_llm=AdjudicatorLLM(),
        block_evidence_shortlists={"magnetism": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is True
    assert interpretation.evidence_refs["magnetism"] == ["raw_inputs.0"]
    assert "magnetism_adjudicator_rescued_gate_rejection" in interpretation.limitations
    provenance = interpretation.blocks["magnetism"]["detection_provenance"]
    assert provenance["final_source"] == "adjudicator_rescued_gate_rejection"
    assert provenance["adjudicator"]["inference_type"] == "product_embodied_strategy"


def test_adjudicator_rejects_rescue_when_quote_is_not_literal() -> None:
    class AdjudicatorLLM:
        api_key = "test"

        def _call_json(self, *args, **kwargs):
            return {
                "state": "ok",
                "quote": "a global movement that rewards athletes",
                "ref": "raw_inputs.0",
                "reason": "The candidate sounds like magnetism.",
                "confidence": "high",
                "inference_type": "product_embodied_strategy",
            }

    pack = BrandEvidencePack(
        brand_name="Toteemi",
        url="https://toteemi.com",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Convierte tu esfuerzo en descuentos. Una app que te paga por entrenar.",
            )
        ],
    )
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "Toteemi uses rewards to make training repeatable and desirable.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The page frames training as paid/rewarded behavior.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        adjudicator_llm=AdjudicatorLLM(),
        block_evidence_shortlists={"magnetism": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is False
    assert "magnetism_structural_gate_rejected" in interpretation.limitations
    assert "magnetism_adjudicator_rejected_gate_rejection" in interpretation.limitations
    adjudicator = interpretation.blocks["magnetism"]["detection_provenance"]["adjudicator"]
    assert adjudicator["validation_error"] == "quote_not_literal_substring"


def test_warn_gate_disagreement_detects_when_adjudicator_verifies_literal_quote() -> None:
    class AdjudicatorLLM:
        api_key = "test"

        def _call_json(self, *args, **kwargs):
            return {
                "state": "ok",
                "quote": "turns effort into credit",
                "ref": "raw_inputs.0",
                "reason": "The quote supports product-embodied mission.",
                "confidence": "high",
                "inference_type": "product_embodied_strategy",
            }

    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme turns effort into credit for neighborhood athletes.",
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.absence.values",
                source="acquisition",
                evidence_type="acquisition.absence.values",
                content="Checked a strategic surface and found no explicit values.",
            ),
        ],
    )
    raw = {
        "blocks": {
            "mission": {
                "detected": True,
                "content": "Turn effort into credit for athletes.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The product repeatedly converts effort into credit.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        adjudicator_llm=AdjudicatorLLM(),
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
        gate_authority="warn",
    )

    provenance = interpretation.blocks["mission"]["detection_provenance"]
    assert interpretation.blocks["mission"]["detected"] is True
    assert provenance["final_source"] == "adjudicator_rescued_gate_rejection"
    assert "mission_adjudicator_rescued_gate_rejection" in interpretation.limitations


def test_warn_gate_disagreement_rejects_when_adjudicator_quote_is_invalid() -> None:
    class AdjudicatorLLM:
        api_key = "test"

        def _call_json(self, *args, **kwargs):
            return {
                "state": "ok",
                "quote": "a movement to transform every athlete",
                "ref": "raw_inputs.0",
                "reason": "The candidate sounds like a mission.",
                "confidence": "high",
                "inference_type": "product_embodied_strategy",
            }

    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme turns effort into credit for neighborhood athletes.",
            ),
            EvidenceRecord(
                ref="raw_inputs.0.subpage.1.absence.values",
                source="acquisition",
                evidence_type="acquisition.absence.values",
                content="Checked a strategic surface and found no explicit values.",
            ),
        ],
    )
    raw = {
        "blocks": {
            "mission": {
                "detected": True,
                "content": "Turn effort into credit for athletes.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The product repeatedly converts effort into credit.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        adjudicator_llm=AdjudicatorLLM(),
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
        gate_authority="warn",
    )

    provenance = interpretation.blocks["mission"]["detection_provenance"]
    assert interpretation.blocks["mission"]["detected"] is False
    assert provenance["final_source"] == "gate_rejected"
    assert provenance["adjudicator"]["validation_error"] == "quote_not_literal_substring"
    assert "mission_adjudicator_rejected_gate_rejection" in interpretation.limitations
    assert "mission_structural_gate_rejected" in interpretation.limitations
    coverage = block_coverage(pack, interpretation)
    assert coverage["mission"]["status"] in {"implied_not_explicit", "verified_absent", "probable_absent"}


def test_warn_gate_disagreement_without_adjudicator_fails_open_and_flags_limitation() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme turns effort into credit for neighborhood athletes.",
            )
        ],
    )
    raw = {
        "blocks": {
            "mission": {
                "detected": True,
                "content": "Turn effort into credit for athletes.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The product repeatedly converts effort into credit.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        adjudicator_llm=None,
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
        gate_authority="warn",
    )

    provenance = interpretation.blocks["mission"]["detection_provenance"]
    assert interpretation.blocks["mission"]["detected"] is True
    assert provenance["final_source"] == "llm_unadjudicated_gate_disagreement"
    assert "mission_gate_disagreement_unadjudicated" in interpretation.limitations


def test_warn_gate_agreement_uses_confirmed_by_gate_source() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Our mission is to help support teams automate high-stakes calls.",
            )
        ],
    )
    raw = {
        "blocks": {
            "mission": {
                "detected": True,
                "content": "Help support teams automate high-stakes calls.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The page states a mission.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
        gate_authority="warn",
    )

    assert interpretation.blocks["mission"]["detected"] is True
    assert interpretation.blocks["mission"]["detection_provenance"]["final_source"] == "llm_confirmed_by_gate"


def test_magnetism_detection_accepts_negative_engagement_evidence_for_tile_evaluation() -> None:
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
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "The brand has engagement signals.",
                "confidence": "medium",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The evidence mentions engagement.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"magnetism": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is True
    assert "magnetism_structural_negative_evidence" not in interpretation.limitations


def test_magnetism_detection_accepts_growth_or_engagement_evidence() -> None:
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
    raw = {
        "blocks": {
            "magnetism": {
                "detected": True,
                "content": "Momentum through growth, community engagement, and press.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The evidence names momentum and engagement.",
            }
        },
        "limitations": [],
    }

    interpretation = normalize_llm_interpretation_response(
        raw,
        pack,
        block_evidence_shortlists={"magnetism": ["raw_inputs.0"]},
    )

    assert interpretation.blocks["magnetism"]["detected"] is True
    assert interpretation.evidence_refs["magnetism"] == ["raw_inputs.0"]


def test_normalize_llm_interpretation_fills_missing_blocks_as_not_detected() -> None:
    pack = BrandEvidencePack(brand_name="Acme", url="https://acme.example")

    interpretation = normalize_llm_interpretation_response({"blocks": {}}, pack)

    assert interpretation.blocks["mission"]["detected"] is False
    assert interpretation.blocks["magnetism"]["detected"] is False
    assert interpretation.evidence_refs == {}


def test_llm_worker_marks_empty_provider_payload_as_empty() -> None:
    class EmptyLLM:
        api_key = "test"
        last_failure_reason = "llm_error"
        call_failures = [{"reason": "llm_error", "error_type": "empty_response", "response_empty": True}]

        def _call_json(self, *args, **kwargs):
            return {}

    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme helps teams ship.",
            )
        ],
    )

    interpretation, debug = build_brand_interpretation_with_llm(pack, llm=EmptyLLM())

    assert debug["status"] == "empty"
    assert debug["detected_count"] == 0
    assert len(debug["failed_blocks"]) == 9
    assert len(debug["block_failures"]) == 9
    assert debug["block_failures"][0]["reason"] == "empty_or_parse_failed"
    assert debug["failure_reason"] == "llm_error"
    assert debug["failure"] == {
        "reason": "llm_error",
        "error_type": "empty_response",
        "provider_status": None,
        "response_empty": True,
    }
    assert debug["block_call_debug"][0] == {
        "json_mode_empty": True,
        "text_fallback_attempted": False,
        "text_fallback_empty": None,
        "last_failure": {
            "reason": "llm_error",
            "error_type": "empty_response",
            "response_empty": True,
            "json_parse_error": None,
        },
        "block": "mission",
    }
    assert interpretation.blocks["mission"]["detected"] is False


def test_llm_worker_can_build_interpretation_per_block() -> None:
    class PerBlockLLM:
        api_key = "test"
        last_failure_reason = None
        call_failures = []

        def __init__(self):
            self.calls = []

        def _call_json(self, system, user, **kwargs):
            self.calls.append((system, user, kwargs))
            if '"block": "mission"' in user:
                return {
                    "detected": True,
                    "content": "Help teams ship.",
                    "confidence": "high",
                    "evidence_refs": ["raw_inputs.0"],
                    "rationale": "Homepage states it.",
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

    llm = PerBlockLLM()
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(ref="raw_inputs.0", source="homepage", evidence_type="raw_input", content="We help teams ship.")
        ],
    )

    interpretation, debug = build_brand_interpretation_with_llm(
        pack,
        llm=llm,
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
    )

    assert len(llm.calls) == 9
    assert all(call[2]["json_schema"] is None for call in llm.calls)
    assert debug["mode"] == "per_block"
    assert debug["status"] == "ok"
    assert debug["detected_count"] == 1
    assert debug["block_detection_decisions"] == [
        {
            "version": "sv9-flow-block-detection-policy-v7",
            "block": "magnetism",
            "outcome": "insufficient_evidence",
            "evidence_refs": [],
            "support_terms": [],
            "weaken_terms": [],
            "limitation_code": "magnetism_insufficient_evidence_refs",
        },
        {
            "version": "sv9-flow-block-detection-policy-v7",
            "block": "mission",
            "outcome": "supports_detection",
            "evidence_refs": ["raw_inputs.0"],
            "support_terms": ["we help"],
            "weaken_terms": [],
            "limitation_code": "",
        },
        {
            "version": "sv9-flow-block-detection-policy-v7",
            "block": "values",
            "outcome": "insufficient_evidence",
            "evidence_refs": [],
            "support_terms": [],
            "weaken_terms": [],
            "limitation_code": "values_insufficient_evidence_refs",
        },
        {
            "version": "sv9-flow-block-detection-policy-v7",
            "block": "vision",
            "outcome": "insufficient_evidence",
            "evidence_refs": [],
            "support_terms": [],
            "weaken_terms": [],
            "limitation_code": "vision_insufficient_evidence_refs",
        },
    ]
    assert debug["block_call_debug"][0]["json_mode_empty"] is False
    assert debug["block_call_debug"][0]["text_fallback_attempted"] is False
    assert "mission" not in debug["failed_blocks"]
    assert "vision" not in debug["failed_blocks"]
    assert any(item["block"] == "vision" and item["reason"] == "insufficient_evidence" for item in debug["block_failures"])
    assert interpretation.blocks["mission"]["detected"] is True
    assert interpretation.evidence_refs["mission"] == ["raw_inputs.0"]


def test_llm_worker_reports_block_detection_from_shortlists_not_llm_refs() -> None:
    class PerBlockLLM:
        api_key = "test"
        last_failure_reason = None
        call_failures = []

        def _call_json(self, system, user, **kwargs):
            return {
                "detected": False,
                "content": "",
                "confidence": "low",
                "evidence_refs": [],
                "rationale": "Not enough evidence.",
                "limitations": [],
            }

    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="report_narrative",
                evidence_type="raw_input",
                content="The brand shows stagnation and a lack of active engagement across public social channels.",
            )
        ],
    )

    _interpretation, debug = build_brand_interpretation_with_llm(
        pack,
        llm=PerBlockLLM(),
        block_evidence_shortlists={"magnetism": ["raw_inputs.0"]},
    )

    assert debug["block_detection_decisions"][0] == {
        "version": "sv9-flow-block-detection-policy-v7",
        "block": "magnetism",
        "outcome": "supports_detection",
        "evidence_refs": ["raw_inputs.0"],
        "support_terms": [],
        "weaken_terms": ["lack of active engagement", "stagnation"],
        "limitation_code": "",
    }


def test_llm_worker_warn_mode_reports_gate_disagreements() -> None:
    class PerBlockLLM:
        api_key = "test"
        last_failure_reason = None
        call_failures = []

        def _call_json(self, system, user, **kwargs):
            if '"block": "mission"' in user:
                return {
                    "detected": True,
                    "content": "Turn effort into credit for athletes.",
                    "confidence": "high",
                    "evidence_refs": ["raw_inputs.0"],
                    "rationale": "The product repeatedly converts effort into credit.",
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

    class AdjudicatorLLM:
        api_key = "test"

        def _call_json(self, *args, **kwargs):
            return {
                "state": "ok",
                "quote": "turns effort into credit",
                "ref": "raw_inputs.0",
                "reason": "The quote supports product-embodied mission.",
                "confidence": "high",
                "inference_type": "product_embodied_strategy",
            }

    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="homepage",
                evidence_type="raw_input",
                content="Acme turns effort into credit for neighborhood athletes.",
            )
        ],
    )

    interpretation, debug = build_brand_interpretation_with_llm(
        pack,
        llm=PerBlockLLM(),
        adjudicator_llm=AdjudicatorLLM(),
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
        gate_authority="warn",
    )

    assert interpretation.blocks["mission"]["detected"] is True
    assert debug["gate_authority"] == "warn"
    assert debug["gate_disagreements"] == [
        {
            "block": "mission",
            "gate_reason": "mission_structural_gate_rejected",
            "adjudicator_state": "ok",
            "adjudicator_validation_error": "",
            "final_detected": True,
            "final_source": "adjudicator_rescued_gate_rejection",
        }
    ]


def test_llm_worker_falls_back_to_text_call_when_json_mode_is_empty() -> None:
    class TextFallbackLLM:
        api_key = "test"
        last_failure_reason = None
        call_failures = []

        def __init__(self):
            self.json_calls = 0
            self.text_calls = 0

        def _call_json(self, system, user, **kwargs):
            self.json_calls += 1
            return {}

        def _call(self, system, user, max_tokens=900):
            self.text_calls += 1
            if '"block": "mission"' in user:
                return """{
                      "detected": true,
                      "content": "We help teams ship.",
                      "confidence": "high",
                      "evidence_refs": ["raw_inputs.0"],
                      "rationale": "Homepage states it.",
                  "limitations": []
                }"""
            return """{
              "detected": false,
              "content": "",
              "confidence": "low",
              "evidence_refs": [],
              "rationale": "Not enough evidence.",
              "limitations": []
            }"""

    llm = TextFallbackLLM()
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(ref="raw_inputs.0", source="homepage", evidence_type="raw_input", content="We help teams ship.")
        ],
    )

    interpretation, debug = build_brand_interpretation_with_llm(
        pack,
        llm=llm,
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
    )

    assert llm.json_calls == 9
    assert llm.text_calls == 9
    assert debug["detected_count"] == 1
    assert debug["block_call_debug"][0]["json_mode_empty"] is True
    assert debug["block_call_debug"][0]["text_fallback_attempted"] is True
    assert debug["block_call_debug"][0]["text_fallback_empty"] is False
    assert interpretation.blocks["mission"]["detected"] is True


def test_llm_worker_text_fallback_accepts_fenced_json() -> None:
    class FencedTextLLM:
        api_key = "test"
        last_failure_reason = None
        call_failures = []

        def _call_json(self, system, user, **kwargs):
            return {}

        def _call(self, system, user, max_tokens=900):
            if '"block": "mission"' in user:
                return """```json
                    {
                      "detected": true,
                      "content": "We help teams ship.",
                      "confidence": "high",
                      "evidence_refs": ["raw_inputs.0"],
                      "rationale": "Homepage states it.",
                  "limitations": []
                }
                ```"""
            return """No clear evidence.
            {
              "detected": false,
              "content": "",
              "confidence": "low",
              "evidence_refs": [],
              "rationale": "Not enough evidence.",
              "limitations": []
            }"""

    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(ref="raw_inputs.0", source="homepage", evidence_type="raw_input", content="We help teams ship.")
        ],
    )

    interpretation, debug = build_brand_interpretation_with_llm(
        pack,
        llm=FencedTextLLM(),
        block_evidence_shortlists={"mission": ["raw_inputs.0"]},
    )

    assert debug["detected_count"] == 1
    assert interpretation.blocks["mission"]["detected"] is True


def test_user_prompt_contains_complete_output_contract() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(ref="raw_inputs.0", source="homepage", evidence_type="raw_input", content="Mission text.")
        ],
    )

    prompt = _user_prompt(pack, block_evidence_shortlists={"mission": ["raw_inputs.0"]})

    assert '"output_contract"' in prompt
    assert "top-level keys: prompt_version, blocks, limitations" in prompt
    assert '"mission"' in prompt
    assert '"vision"' in prompt
    assert '"magnetism"' in prompt


def test_block_interpretation_schema_requires_single_block_shape() -> None:
    schema = block_interpretation_response_schema()

    assert schema["required"] == ["detected", "content", "confidence", "evidence_refs", "rationale", "limitations"]


def test_brand_interpretation_schema_requires_all_blocks_shape() -> None:
    schema = brand_interpretation_response_schema()

    assert schema["required"] == ["prompt_version", "blocks", "limitations"]
    assert "mission" in schema["properties"]["blocks"]["required"]
    assert "brand_idea" in schema["properties"]["blocks"]["required"]


def test_block_prompt_adds_guidance_for_brand_idea_and_value_proposition() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="features.0",
                source="legacy_feature",
                evidence_type="diferenciacion.uniqueness",
                content="Unique methodology and vocabulary.",
            )
        ],
    )

    brand_idea_prompt = _block_user_prompt(pack, block="brand_idea", evidence_refs=["features.0"])
    value_prompt = _block_user_prompt(pack, block="value_proposition", evidence_refs=["features.0"])

    assert "ownable language" in brand_idea_prompt
    assert "Do not infer brand_idea from visual style alone" in brand_idea_prompt
    assert "positive" in brand_idea_prompt
    assert "negative" in brand_idea_prompt
    assert "Visual polish alone is not a brand idea" in brand_idea_prompt
    assert "concrete value" in value_prompt
    assert "Prefer a concrete offer statement" in value_prompt
    assert "The evidence states who receives what concrete benefit" in value_prompt
    assert "A broad mission claim is not enough" in value_prompt


def test_block_prompt_uses_small_required_json_contract() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(ref="raw_inputs.0", source="homepage", evidence_type="raw_input", content="Mission text.")
        ],
    )

    prompt = _block_user_prompt(pack, block="mission", evidence_refs=["raw_inputs.0"])

    assert '"required_json"' in prompt
    assert '"output_contract"' not in prompt
    assert "Return only one valid JSON object with the required_json keys." in prompt


def test_magnetism_block_prompt_allows_external_proof_grounds() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0.exa.news.0",
                source="exa",
                evidence_type="external_proof.news",
                content="Acme closes Series B and announces an enterprise partnership.",
            )
        ],
    )

    prompt = _block_user_prompt(pack, block="magnetism", evidence_refs=["raw_inputs.0.exa.news.0"])

    assert "External proof is strongest for gravity and market validation" in prompt
    assert "it must not replace owned-copy hook evidence" in prompt
