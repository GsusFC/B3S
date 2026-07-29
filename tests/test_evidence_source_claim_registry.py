from __future__ import annotations

from copy import deepcopy

import pytest

import src.services.evidence_source_claim_registry as registry_module
from src.services.evidence_claim_corroboration_review_set import (
    EvidenceClaimCorroborationReviewSetError,
    build_review_template as build_claim_review_template,
    load_review_candidates as load_claim_review_candidates,
    load_review_manifest as load_claim_review_manifest,
    review_set_fingerprint,
)
from src.services.evidence_source_claim_registry import (
    EVIDENCE_SOURCE_CLAIM_REGISTRY_POLICY_VERSION,
    EVIDENCE_SOURCE_CLAIM_REGISTRY_SCHEMA_VERSION,
    EvidenceSourceClaimRegistryError,
    build_evidence_source_claim_registry,
    render_evidence_source_claim_registry_markdown,
)
from src.services.evidence_source_review_set import (
    load_review_candidates as load_source_review_candidates,
    load_review_events as load_source_review_events,
    load_review_manifest as load_source_review_manifest,
    source_review_fingerprint,
)


def test_default_registry_preserves_both_axes_without_authority() -> None:
    registry = build_evidence_source_claim_registry()

    assert (
        registry["schema_version"]
        == EVIDENCE_SOURCE_CLAIM_REGISTRY_SCHEMA_VERSION
    )
    assert (
        registry["policy_version"]
        == EVIDENCE_SOURCE_CLAIM_REGISTRY_POLICY_VERSION
    )
    assert registry["runtime_effect"] is False
    assert registry["authority"] is False
    assert registry["shadow_contract_ready"] is True
    assert registry["operational_adoption_ready"] is False
    assert registry["promotion_ready"] is False
    assert registry["summary"] == {
        "source_count": 6,
        "publisher_reviewed_count": 6,
        "publisher_decision_counts": {
            "confirmed_independent": 5,
            "excluded": 1,
        },
        "source_global_claim_corroboration_decision_count": 0,
        "claim_record_count": 10,
        "claim_source_count": 5,
        "claim_id_count": 10,
        "claim_reviewed_count": 0,
        "claim_pending_count": 10,
        "claim_decision_counts": {},
    }
    assert registry["policy"][
        "publisher_independence_implies_claim_corroboration"
    ] is False
    assert registry["policy"][
        "source_global_claim_corroboration_allowed"
    ] is False
    assert "operational_two_axis_contract_not_adopted" not in registry[
        "promotion_blockers"
    ]
    assert "shadow_contract_not_operationally_adopted" in registry[
        "promotion_blockers"
    ]


def test_pending_claim_rows_require_claim_id_and_never_inherit_decision() -> None:
    registry = build_evidence_source_claim_registry()

    assert all(row["claim_id"] for row in registry["claims"])
    assert all(
        row["claim_corroboration_review_status"] == "pending"
        for row in registry["claims"]
    )
    assert all(
        row["claim_corroboration_decision"] is None
        for row in registry["claims"]
    )
    assert all(
        row["source_global_claim_corroboration_decision"] is None
        for row in registry["sources"]
    )
    assert {
        row["publisher_independence_decision"]
        for row in registry["claims"]
    } == {"confirmed_independent"}
    assert all(
        row["runtime_effect"] is False and row["authority"] is False
        for row in [*registry["sources"], *registry["claims"]]
    )


def test_registry_fingerprint_is_invariant_to_input_order() -> None:
    source_candidates = load_source_review_candidates()
    source_events = load_source_review_events()
    claim_candidates = load_claim_review_candidates()

    forward = build_evidence_source_claim_registry(
        source_candidates=source_candidates,
        source_review_events=source_events,
        source_manifest=load_source_review_manifest(),
        claim_candidates=claim_candidates,
        claim_review_events=[],
        claim_manifest=load_claim_review_manifest(),
    )
    reverse = build_evidence_source_claim_registry(
        source_candidates=reversed(source_candidates),
        source_review_events=reversed(source_events),
        source_manifest=load_source_review_manifest(),
        claim_candidates=reversed(claim_candidates),
        claim_review_events=[],
        claim_manifest=load_claim_review_manifest(),
    )

    assert forward["registry_fingerprint"] == reverse["registry_fingerprint"]
    assert forward["sources"] == reverse["sources"]
    assert forward["claims"] == reverse["claims"]


def test_unknown_parent_source_fails_closed() -> None:
    candidates = load_claim_review_candidates()
    tampered = deepcopy(candidates)
    tampered[0]["parent_source_case_id"] = "missing-source-case"
    manifest = deepcopy(load_claim_review_manifest())
    manifest["candidate_fingerprint"] = review_set_fingerprint(tampered)

    with pytest.raises(
        EvidenceSourceClaimRegistryError,
        match="unknown parent source",
    ):
        build_evidence_source_claim_registry(
            claim_candidates=tampered,
            claim_review_events=[],
            claim_manifest=manifest,
        )


def test_tampered_dataset_fingerprint_fails_closed() -> None:
    candidates = load_claim_review_candidates()
    tampered = deepcopy(candidates)
    tampered[0]["claim"]["statement"] = "Tampered claim"

    with pytest.raises(
        EvidenceSourceClaimRegistryError,
        match="input dataset is invalid",
    ):
        build_evidence_source_claim_registry(
            claim_candidates=tampered,
        )


def test_missing_input_fixture_is_wrapped_as_registry_failure(
    monkeypatch,
) -> None:
    def _missing_candidates() -> list[dict]:
        raise EvidenceClaimCorroborationReviewSetError("missing fixture")

    monkeypatch.setattr(
        registry_module,
        "load_claim_review_candidates",
        _missing_candidates,
    )

    with pytest.raises(
        EvidenceSourceClaimRegistryError,
        match="input dataset is invalid",
    ):
        build_evidence_source_claim_registry()


def test_independent_claim_cannot_attach_to_excluded_publisher() -> None:
    source_events = load_source_review_events()
    source_events[0]["publisher_independence_decision"] = "excluded"
    source_events[0]["claim_corroboration_decision"] = "excluded"
    source_events[0]["requires_claim_level_review"] = False
    source_manifest = deepcopy(load_source_review_manifest())
    source_manifest["review_event_fingerprint"] = (
        source_review_fingerprint(source_events)
    )

    claim_candidates = load_claim_review_candidates()
    claim_event = build_claim_review_template(claim_candidates)[0]
    claim_event.update(
        {
            "event_id": "incompatible-claim-review-001",
            "decision": "independently_corroborated",
            "corroboration_bases": ["independent_testing"],
            "reviewer_id": "gsus",
            "rationale": "Synthetic compatibility test.",
            "reviewed_at": "2026-07-29T15:00:00+02:00",
        }
    )

    with pytest.raises(
        EvidenceSourceClaimRegistryError,
        match="independent claim requires independent publisher",
    ):
        build_evidence_source_claim_registry(
            source_review_events=source_events,
            source_manifest=source_manifest,
            claim_candidates=claim_candidates,
            claim_review_events=[claim_event],
            claim_manifest=load_claim_review_manifest(),
        )


def test_markdown_reports_the_shadow_adoption_boundary() -> None:
    rendered = render_evidence_source_claim_registry_markdown(
        build_evidence_source_claim_registry()
    )

    assert "# Evidence source/claim registry v2" in rendered
    assert "Shadow contract ready: `true`" in rendered
    assert "Operational adoption ready: `false`" in rendered
    assert "Claim records: `10`" in rendered
    assert "Claims pending: `10`" in rendered
    assert "Source-global corroboration decisions: `0`" in rendered
