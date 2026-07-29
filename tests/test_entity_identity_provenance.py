from __future__ import annotations

from src.entity_identity_provenance import (
    resolve_entity_conflict_provenance,
    resolve_entity_relation_provenance,
)


def test_deterministic_conflict_requires_a_reproduced_span() -> None:
    value = {
        "schema_version": "entity-conflict-provenance-v1",
        "policy_version": "entity-conflict-policy-v1",
        "status": "explicit",
        "conflict_type": "explicit_other_entity",
        "detection_method": "deterministic_text",
        "evidence_spans": ["A different company"],
        "alternative_entity_locator": None,
        "reproducible": True,
    }

    resolved = resolve_entity_conflict_provenance(
        value,
        brand_name="Example",
        brand_domain="example.com",
        source_domain="news.test",
        content="A different company named Example announced a launch.",
    )
    tampered = resolve_entity_conflict_provenance(
        value,
        brand_name="Example",
        brand_domain="example.com",
        source_domain="news.test",
        content="Example announced a launch.",
    )
    forged = resolve_entity_conflict_provenance(
        {
            **value,
            "evidence_spans": ["Example announced a launch"],
        },
        brand_name="Example",
        brand_domain="example.com",
        source_domain="news.test",
        content="Example announced a launch.",
    )

    assert resolved["authoritative"] is True
    assert resolved["conflict_type"] == "explicit_other_entity"
    assert "evidence_spans" not in resolved["provenance"]
    assert resolved["provenance"]["evidence_span_count"] == 1
    assert tampered["authoritative"] is False
    assert "entity_provenance_evidence_span_not_reproduced" in tampered[
        "reasons"
    ]
    assert forged["authoritative"] is False
    assert "entity_conflict_text_rule_not_reproduced" in forged["reasons"]


def test_llm_conflict_is_not_authoritative_even_when_marked_reproducible() -> None:
    resolved = resolve_entity_conflict_provenance(
        {
            "schema_version": "entity-conflict-provenance-v1",
            "policy_version": "entity-conflict-policy-v1",
            "status": "explicit",
            "conflict_type": "resolved_homonym",
            "detection_method": "llm_classification",
            "evidence_spans": ["Example announced a launch."],
            "reproducible": True,
        },
        brand_name="Example",
        brand_domain="example.com",
        source_domain="news.test",
        content="Example announced a launch.",
    )

    assert resolved["authoritative"] is False
    assert "llm_entity_conflict_not_authoritative" in resolved["reasons"]


def test_sector_difference_alone_is_not_negative_identity_proof() -> None:
    resolved = resolve_entity_conflict_provenance(
        None,
        brand_name="Example",
        brand_domain="example.com",
        source_domain="industry.test",
        content="Example appears in a different sector from the scanned business.",
    )

    assert resolved["authoritative"] is False
    assert resolved["conflict_type"] == "unknown"


def test_external_domain_locator_alone_cannot_prove_unrelated_identity() -> None:
    resolved = resolve_entity_conflict_provenance(
        {
            "schema_version": "entity-conflict-provenance-v1",
            "policy_version": "entity-conflict-policy-v1",
            "status": "verified",
            "conflict_type": "verified_domain_mismatch",
            "detection_method": "verified_domain",
            "alternative_entity_locator": "parent.test",
            "reproducible": True,
        },
        brand_name="Example",
        brand_domain="example.com",
        source_domain="parent.test",
        content="Parent Holdings describes Example as a product.",
    )

    assert resolved["authoritative"] is False
    assert "entity_conflict_external_verifier_unavailable" in resolved[
        "reasons"
    ]


def test_verified_relation_requires_reproducible_inputs() -> None:
    relation = {
        "schema_version": "entity-relation-provenance-v1",
        "policy_version": "entity-relation-policy-v1",
        "status": "verified",
        "relation": "product",
        "subject_role": "scanned_entity",
        "source_domain": "parent.test",
        "subject_domain": "example.com",
        "verification_method": "deterministic_text",
        "evidence_spans": ["Example is the product"],
        "reproducible": True,
    }

    resolved = resolve_entity_relation_provenance(
        relation,
        brand_name="Example",
        brand_domain="example.com",
        source_domain="parent.test",
        content="Parent Holdings confirms Example is the product.",
    )
    wrong_domain = resolve_entity_relation_provenance(
        relation,
        brand_name="Example",
        brand_domain="example.com",
        source_domain="other-parent.test",
        content="Parent Holdings confirms Example is the product.",
    )
    forged = resolve_entity_relation_provenance(
        {
            **relation,
            "evidence_spans": ["Parent Holdings mentions Example"],
        },
        brand_name="Example",
        brand_domain="example.com",
        source_domain="parent.test",
        content="Parent Holdings mentions Example.",
    )

    assert resolved["authoritative"] is True
    assert resolved["status"] == "verified"
    assert resolved["relation"] == "product"
    assert resolved["subject_role"] == "scanned_entity"
    assert "evidence_spans" not in resolved["provenance"]
    assert wrong_domain["authoritative"] is False
    assert wrong_domain["status"] == "candidate"
    assert wrong_domain["subject_role"] == "scanned_entity"
    assert forged["authoritative"] is False
    assert forged["status"] == "unknown"
