from __future__ import annotations

from copy import deepcopy

from src.external_identity_provenance import (
    build_external_identity_provenance,
    verify_external_identity_provenance,
)


def test_strong_external_identity_provenance_is_reproducible() -> None:
    provenance = _provenance()

    verified, reasons = verify_external_identity_provenance(
        provenance,
        brand_domain="example.com",
        source_domain="press.test",
        content="Example launches a new product.",
    )

    assert verified is True
    assert reasons == ["external_identity_provenance_reproduced"]
    assert provenance["candidate_strength"] == "strong"
    assert provenance["subject_domain"] == "example.com"
    assert provenance["source_domain"] == "press.test"


def test_provenance_cannot_be_replayed_against_different_evidence() -> None:
    provenance = _provenance()

    verified, reasons = verify_external_identity_provenance(
        provenance,
        brand_domain="example.com",
        source_domain="different.test",
        content="A different company launches a product.",
    )

    assert verified is False
    assert "external_identity_source_domain_mismatch" in reasons
    assert "external_identity_alias_not_reproducible_in_content" in reasons


def test_review_gated_or_tampered_provenance_is_not_strong() -> None:
    provenance = _provenance()
    provenance["requires_human_review"] = True
    provenance["collector_source_class"] = "related_unresolved"

    verified, reasons = verify_external_identity_provenance(
        provenance,
        brand_domain="example.com",
        source_domain="press.test",
        content="Example launches a new product.",
    )

    assert verified is False
    assert "external_identity_requires_human_review" in reasons
    assert "external_identity_source_class_not_independent" in reasons


def test_non_finite_or_out_of_range_scores_never_verify() -> None:
    for score in ("nan", "inf", 1.1):
        provenance = deepcopy(_provenance())
        provenance["match_score"] = score

        verified, reasons = verify_external_identity_provenance(
            provenance,
            brand_domain="example.com",
            source_domain="press.test",
            content="Example launches a new product.",
        )

        assert verified is False
        assert any(
            reason
            in {
                "external_identity_match_score_invalid",
                "external_identity_match_score_too_low",
            }
            for reason in reasons
        )


def _provenance() -> dict:
    return build_external_identity_provenance(
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
