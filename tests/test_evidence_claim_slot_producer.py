from __future__ import annotations

from copy import deepcopy

from src.services.evidence_claim_memory import build_evidence_claim_memory
from src.sv9_flow.claim_slot_producer import (
    CLAIM_SLOT_PRODUCER_VERSION,
    HISTORICAL_CLAIM_BACKFILL_VERSION,
    build_claim_memory_evidence,
)
from src.sv9_flow.contracts import (
    BrandEvidencePack,
    BrandInterpretation,
    EvidenceRecord,
    Sv9FlowCandidate,
)
from src.sv9_flow.orchestrator import build_flow_candidate


def test_producer_emits_only_explicit_owned_mission_and_vision_slots() -> None:
    pack = _pack(
        _owned(
            "# Our Mission\nHelp finance teams close with confidence.",
            ref="web.mission",
        ),
        _owned(
            "Nuestra visión es convertir cada cierre en una decisión clara.",
            ref="web.vision",
        ),
        _owned(
            "# About\nOur mission is to keep every decision traceable.",
            ref="web.about-inline",
            url="https://example.com/es/about",
        ),
        _owned(
            "# Our Values\nCraft, clarity, and care.",
            ref="web.values",
        ),
        _owned(
            "Acme helps operators move faster.",
            ref="web.generic",
        ),
        EvidenceRecord(
            ref="exa.mission",
            source="exa",
            evidence_type="external_proof.news",
            content="Our mission is to transform finance.",
            url="https://press.example/acme",
            metadata={"source_class": "external_proof"},
        ),
    )

    records = build_claim_memory_evidence(pack)

    assert [record.metadata["claim_slot_key"] for record in records] == [
        "mission.primary",
        "vision.primary",
        "mission.primary",
    ]
    assert [record.content for record in records] == [
        "Help finance teams close with confidence.",
        "Nuestra visión es convertir cada cierre en una decisión clara.",
        "Our mission is to keep every decision traceable.",
    ]
    assert all(
        record.metadata["claim_slot_producer_version"]
        == CLAIM_SLOT_PRODUCER_VERSION
        for record in records
    )
    assert all(record.metadata["runtime_effect"] is False for record in records)
    assert all(record.metadata["authority"] is False for record in records)


def test_producer_slot_identity_does_not_depend_on_text_url_or_ordinal() -> None:
    first = build_claim_memory_evidence(
        _pack(
            _owned(
                "Our mission is to simplify finance.",
                ref="web.about",
                url="https://example.com/about",
            )
        )
    )[0]
    second = build_claim_memory_evidence(
        _pack(
            _owned(
                "Our mission is to make operations legible.",
                ref="web.company.7",
                url="https://example.com/company",
            )
        )
    )[0]

    assert first.metadata["claim_slot_key"] == "mission.primary"
    assert second.metadata["claim_slot_key"] == "mission.primary"
    assert first.ref != second.ref
    assert first.content != second.content
    assert first.metadata["record_ref_semantics"] == "provenance_only"
    assert second.metadata["record_ref_semantics"] == "provenance_only"


def test_producer_preserves_explicit_entity_scope_but_does_not_infer_one() -> None:
    scoped = _owned(
        "# Mission\nKeep treasury teams ahead of cash risk.",
        ref="web.product",
        entity_scope="product:treasury",
    )
    unscoped = _owned(
        "# Vision\nMake business finance understandable.",
        ref="web.brand",
    )

    records = build_claim_memory_evidence(_pack(scoped, unscoped))
    by_type = {
        record.metadata["claim_type"]: record.metadata for record in records
    }

    assert by_type["mission"]["entity_scope"] == "product:treasury"
    assert "entity_scope" not in by_type["vision"]


def test_unscoped_product_and_foreign_pages_fail_closed() -> None:
    product = _owned(
        "# Our Mission\nMake treasury operations effortless.",
        ref="web.product",
        url="https://example.com/products/treasury",
    )
    scoped_product = _owned(
        "# Our Mission\nMake treasury operations effortless.",
        ref="web.scoped-product",
        url="https://example.com/products/treasury",
        entity_scope="product:treasury",
    )
    foreign = _owned(
        "# Our Vision\nMake every business legible.",
        ref="web.foreign",
        url="https://unrelated.example/about",
    )
    relative_product = _owned(
        "# Our Vision\nMake treasury effortless.",
        ref="web.relative-product",
        url="/products/treasury",
    )

    records = build_claim_memory_evidence(
        _pack(product, scoped_product, foreign, relative_product)
    )

    assert [record.metadata["source_evidence_ref"] for record in records] == [
        "web.scoped-product"
    ]
    assert records[0].metadata["entity_scope"] == "product:treasury"


def test_producer_to_claim_memory_proposes_replacement_without_authority() -> None:
    reports = [
        _report(
            "old",
            "2026-01-01T00:00:00Z",
            _owned(
                "# Our Mission\nHelp finance teams close with confidence.",
                ref="web.about",
            ),
        ),
        _report(
            "new",
            "2026-02-01T00:00:00Z",
            _owned(
                "# Our Mission\nHelp every operator make confident decisions.",
                ref="web.about",
            ),
        ),
    ]

    memory = build_evidence_claim_memory(reports)

    assert memory["policy_version"] == "evidence-claim-memory-policy-v4"
    assert memory["summary"]["claim_slot_count"] == 1
    assert memory["summary"]["claim_variant_count"] == 2
    assert memory["summary"]["relation_candidate_counts"] == {
        "replacement_candidate": 1
    }
    assert memory["slots"][0]["claim_slot_key"] == "mission.primary"
    assert memory["slots"][0]["runtime_effect"] is False
    assert memory["slots"][0]["authority"] is False
    assert memory["policy"]["shadow_producer_lane_affects_runtime"] is False


def test_explicit_historical_phrasings_are_recoverable_and_deduplicated() -> None:
    robin_web = _owned(
        (
            "# As OnE\nAs founders ourselves, we are deeply committed to "
            "our mission of supporting you. We are here for the long haul."
        ),
        ref="web.robin",
        url="https://example.com/about",
    )
    robin_exa = EvidenceRecord(
        ref="exa.robin",
        source="exa",
        evidence_type="external_proof.owned_confirmation",
        content=(
            "As founders ourselves, we are deeply committed to our mission "
            "of supporting you. We are here for the long haul."
        ),
        url="https://example.com/about",
        confidence="low",
        metadata={
            "source_class": "owned_copy",
            "intent": "owned_confirmation",
            "identity_match": "domain",
        },
    )
    pack = _pack(
        _owned(
            (
                "# About Us\nAt Example, our mission is to enable developers "
                "to publish wonderful apps."
            ),
            ref="web.vercel-shape",
            url="https://example.com/company",
        ),
        _owned(
            "# Our Mission to Democratise Finance",
            ref="web.liminal-shape",
            url="https://example.com/mission",
        ),
        robin_web,
        robin_exa,
    )

    records = build_claim_memory_evidence(pack)

    assert [record.content for record in records] == [
        "our mission is to enable developers to publish wonderful apps.",
        "Our Mission to Democratise Finance",
        "our mission of supporting you.",
    ]
    assert [
        record.metadata["source_evidence_ref"] for record in records
    ] == [
        "web.vercel-shape",
        "web.liminal-shape",
        "web.robin",
    ]


def test_legacy_candidate_is_backfilled_without_mutating_report() -> None:
    report = _legacy_report(
        "legacy",
        "2026-01-01T00:00:00Z",
        _owned(
            (
                "# About\nAt Example, our mission is to make every "
                "decision traceable."
            ),
            ref="web.about",
        ),
    )
    before = deepcopy(report)

    memory = build_evidence_claim_memory([report])

    assert report == before
    assert memory["summary"]["claim_slot_count"] == 1
    assert memory["summary"]["historical_backfill_report_count"] == 1
    assert (
        memory["summary"]["historical_backfill_report_with_claims_count"]
        == 1
    )
    assert memory["summary"]["historical_backfill_claim_record_count"] == 1
    assert memory["summary"]["claim_slot_derivation_mode_counts"] == {
        "historical_backfill": 1
    }
    assert memory["claim_slot_producer"]["historical_backfill_version"] == (
        HISTORICAL_CLAIM_BACKFILL_VERSION
    )
    assert memory["claim_slot_producer"]["resolution_mode_counts"] == {
        "historical_backfill": 1
    }
    assert memory["occurrences"][0]["claim_slot_derivation_mode"] == (
        "historical_backfill"
    )
    assert memory["runtime_effect"] is False
    assert memory["authority"] is False


def test_persisted_empty_lane_prevents_legacy_reinterpretation() -> None:
    report = _legacy_report(
        "legacy-with-empty-lane",
        "2026-01-01T00:00:00Z",
        _owned(
            "Our mission is to make every decision traceable.",
            ref="web.about",
        ),
    )
    candidate = report["raw"]["flow"]["candidate"]
    candidate["claim_memory_evidence"] = []

    memory = build_evidence_claim_memory([report])

    assert memory["summary"]["claim_slot_count"] == 0
    assert memory["summary"]["historical_backfill_report_count"] == 0
    assert memory["claim_slot_producer"]["resolution_mode_counts"] == {
        "persisted_shadow_lane": 1
    }


def test_malformed_persisted_lane_fails_closed_without_backfill() -> None:
    report = _legacy_report(
        "legacy-with-malformed-lane",
        "2026-01-01T00:00:00Z",
        _owned(
            "Our mission is to make every decision traceable.",
            ref="web.about",
        ),
    )
    report["raw"]["flow"]["candidate"]["claim_memory_evidence"] = {
        "unexpected": "object"
    }

    memory = build_evidence_claim_memory([report])

    assert memory["summary"]["claim_slot_count"] == 0
    assert memory["summary"]["historical_backfill_report_count"] == 0
    assert memory["claim_slot_producer"]["resolution_mode_counts"] == {
        "invalid_persisted_shadow_lane": 1
    }


def test_v2_candidate_missing_lane_fails_closed() -> None:
    report = _legacy_report(
        "v2-missing",
        "2026-01-01T00:00:00Z",
        _owned(
            "Our mission is to make every decision traceable.",
            ref="web.about",
        ),
    )
    report["raw"]["flow"]["candidate"]["schema_version"] = (
        "sv9-flow-candidate-v2"
    )

    memory = build_evidence_claim_memory([report])

    assert memory["summary"]["claim_slot_count"] == 0
    assert memory["summary"]["historical_backfill_report_count"] == 0
    assert memory["claim_slot_producer"]["resolution_mode_counts"] == {
        "ineligible_candidate_schema": 1
    }


def test_two_explicit_missions_in_one_report_propose_coexistence() -> None:
    report = _report(
        "one",
        "2026-01-01T00:00:00Z",
        _owned(
            "Our mission is to simplify finance.",
            ref="web.about",
            url="https://example.com/about",
        ),
        _owned(
            "Nuestra misión es hacer visible cada decisión.",
            ref="web.company",
            url="https://example.com/company",
        ),
    )

    memory = build_evidence_claim_memory([report])

    assert memory["summary"]["claim_slot_count"] == 1
    assert memory["summary"]["claim_variant_count"] == 2
    assert memory["summary"]["relation_candidate_counts"] == {
        "coexistence_candidate": 1
    }
    assert memory["slots"][0]["requires_human_review"] is True


def test_orchestrator_persists_claims_outside_runtime_evidence_pack() -> None:
    class EmptyFlowLlm:
        api_key = "test-key"
        model = "test-model"
        last_failure_reason = None
        call_failures: list[str] = []

        def _call_json(self, _system, _user, **_kwargs):
            return {
                "detected": False,
                "content": "",
                "confidence": "low",
                "evidence_refs": [],
                "rationale": "No interpretation needed for this test.",
                "limitations": [],
            }

    candidate, debug = build_flow_candidate(
        snapshot={
            "run": {
                "brand_name": "Example",
                "url": "https://example.com",
            },
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {
                        "url": "https://example.com",
                        "markdown_content": (
                            "# Our Mission\n"
                            "Help finance teams close with confidence."
                        ),
                    },
                }
            ],
        },
        llm=EmptyFlowLlm(),
    )

    assert all(
        not record.evidence_type.startswith("semantic_claim.")
        for record in candidate.evidence_pack.evidence
    )
    assert [
        record.evidence_type for record in candidate.claim_memory_evidence
    ] == ["semantic_claim.mission"]
    assert debug["claim_slot_producer"] == {
        "schema_version": CLAIM_SLOT_PRODUCER_VERSION,
        "mode": "shadow",
        "runtime_effect": False,
        "authority": False,
        "record_count": 1,
        "claim_type_counts": {"mission": 1},
    }


def _owned(
    content: str,
    *,
    ref: str,
    url: str = "https://example.com/about",
    **metadata: str,
) -> EvidenceRecord:
    return EvidenceRecord(
        ref=ref,
        source="web",
        evidence_type="raw_input",
        content=content,
        url=url,
        confidence="high",
        metadata={"source_class": "owned_copy", **metadata},
    )


def _pack(*records: EvidenceRecord) -> BrandEvidencePack:
    return BrandEvidencePack(
        brand_name="Example",
        url="https://example.com",
        evidence=list(records),
    )


def _report(
    report_id: str,
    created_at: str,
    *records: EvidenceRecord,
) -> dict:
    pack = _pack(*records)
    candidate = Sv9FlowCandidate(
        evidence_pack=pack,
        interpretation=BrandInterpretation(
            brand_name=pack.brand_name,
            url=pack.url,
        ),
        claim_memory_evidence=build_claim_memory_evidence(pack),
    )
    return {
        "id": report_id,
        "brand_name": pack.brand_name,
        "url": pack.url,
        "created_at": created_at,
        "reliability_status": "shadow",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "blocks": [],
        "raw": {"flow": {"candidate": candidate.to_dict()}},
    }


def _legacy_report(
    report_id: str,
    created_at: str,
    *records: EvidenceRecord,
) -> dict:
    report = _report(report_id, created_at, *records)
    candidate = report["raw"]["flow"]["candidate"]
    candidate["schema_version"] = "sv9-flow-candidate-v1"
    candidate.pop("claim_memory_evidence")
    return report
