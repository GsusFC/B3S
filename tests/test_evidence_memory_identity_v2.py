from __future__ import annotations

from copy import deepcopy

from src.external_identity_provenance import build_external_identity_provenance
from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)


def test_multiple_passages_from_one_document_are_not_revision_candidates() -> None:
    report = _report(
        "one",
        "2026-01-01T00:00:00Z",
        [
            _owned("web.0.chunk.1", "We serve finance teams."),
            _owned("web.0.chunk.2", "We automate monthly reporting."),
        ],
    )

    result = build_evidence_memory_identity_v2([report])

    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"]["entry_count"] == 2
    assert result["summary"]["document_count"] == 1
    assert result["summary"]["passage_count"] == 2
    assert result["summary"]["multi_passage_document_count"] == 1
    assert result["summary"]["revision_candidate_count"] == 0
    assert {entry["state"] for entry in result["entries"]} == {"observed"}
    assert all(not entry["claim_slot_id"] for entry in result["entries"])


def test_new_passage_on_same_url_is_not_assumed_to_replace_old_passage() -> None:
    old = _owned("web.0.chunk.1", "We serve finance teams.")
    new = _owned("web.0.chunk.1", "We serve operations teams.")

    result = build_evidence_memory_identity_v2(
        [
            _report("old", "2026-01-01T00:00:00Z", [old]),
            _report("new", "2026-01-02T00:00:00Z", [new]),
        ]
    )

    assert result["summary"]["document_count"] == 1
    assert result["summary"]["passage_count"] == 2
    assert result["summary"]["revision_candidate_count"] == 0
    assert {entry["state"] for entry in result["entries"]} == {
        "not_reacquired",
        "observed",
    }
    missing = next(
        entry for entry in result["entries"] if entry["state"] == "not_reacquired"
    )
    assert "same_document_new_passage_is_not_change" in missing["state_reason_codes"]


def test_explicit_claim_slot_can_propose_revision_without_accepting_replacement() -> None:
    old = _owned(
        "web.0",
        "Our mission is to simplify finance.",
        claim_id="mission-primary",
    )
    new = _owned(
        "web.0",
        "Our mission is to automate finance.",
        claim_id="mission-primary",
    )

    result = build_evidence_memory_identity_v2(
        [
            _report("old", "2026-01-01T00:00:00Z", [old]),
            _report("new", "2026-01-02T00:00:00Z", [new]),
        ]
    )
    revision = next(
        entry for entry in result["entries"] if entry["state"] == "revision_candidate"
    )

    assert result["summary"]["claim_slot_count"] == 1
    assert result["summary"]["claim_slot_method_counts"] == {
        "explicit_claim_id": 1
    }
    assert result["summary"]["revision_candidate_count"] == 1
    assert revision["claim_slot_method"] == "explicit_claim_id"
    assert revision["claim_slot_variant_count"] == 2
    assert revision["adjudication_state"] == "proposed"
    assert "semantic_replacement_not_assumed" in revision["state_reason_codes"]


def test_llm_only_external_identity_never_becomes_validation_candidate() -> None:
    row = _external(
        "exa.0",
        "https://unrelated.test/story",
        "A different company called Example launched a product.",
        identity_match_llm="brand_name",
    )

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", [row]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(row)]),
        ]
    )
    entry = result["entries"][0]

    assert entry["identity_status"] == "unverified"
    assert entry["qualified_observation_count"] == 0
    assert entry["state"] == "repeated"
    assert all(
        "llm_only_identity_match_not_eligible"
        in observation["identity_reason_codes"]
        for observation in entry["observations"]
    )


def test_external_brand_name_match_requires_adjudication() -> None:
    row = _external(
        "exa.0",
        "https://press.test/story",
        "Example launched a product.",
        identity_match="brand_name",
    )

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", [row]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(row)]),
        ]
    )

    assert result["entries"][0]["identity_status"] == "unverified"
    assert result["entries"][0]["state"] == "repeated"
    assert result["entries"][0]["qualified_observation_count"] == 0


def test_bare_upstream_domain_label_is_not_validation_eligible() -> None:
    row = _external(
        "exa.0",
        "https://press.test/story",
        "Evidence explicitly tied to https://example.com.",
        identity_match="domain",
        identity_match_llm="domain",
    )

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", [row]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(row)]),
        ]
    )

    assert result["entries"][0]["identity_status"] == "unverified"
    assert result["entries"][0]["qualified_observation_count"] == 0
    assert result["entries"][0]["state"] == "repeated"
    assert all(
        "upstream_domain_label_not_independently_reproduced"
        in observation["identity_reason_codes"]
        for observation in result["entries"][0]["observations"]
    )


def test_reproducible_external_identity_can_repeat_as_candidate() -> None:
    eligible = _external(
        "exa.0",
        "https://press.test/story",
        "Example launches a product.",
        identity_match="brand_name",
        provenance=_provenance(),
    )

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", [eligible]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(eligible)]),
        ]
    )
    entry = result["entries"][0]

    assert entry["identity_status"] == "eligible"
    assert entry["qualified_observation_count"] == 2
    assert entry["state"] == "validation_candidate"
    assert entry["observations"][0]["external_identity_provenance"][0][
        "matched_alias"
    ] == "example"


def test_later_identity_dispute_degrades_validation_candidate() -> None:
    eligible = _external(
        "exa.0",
        "https://press.test/story",
        "Example launches a product.",
        identity_match="brand_name",
        provenance=_provenance(),
    )
    disputed = deepcopy(eligible)
    disputed["metadata"]["identity_match_llm"] = "none"

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", [eligible]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(eligible)]),
            _report("three", "2026-01-03T00:00:00Z", [disputed]),
        ]
    )
    entry = result["entries"][0]

    assert entry["qualified_observation_count"] == 2
    assert entry["identity_status"] == "disputed"
    assert entry["state"] == "repeated"
    assert "validation_eligibility_incomplete" in entry["state_reason_codes"]


def test_exact_syndication_across_publishers_counts_as_one_independent_cluster() -> None:
    rows = [
        _external(
            "exa.1",
            "https://wire-one.test/story",
            "Example announced the same syndicated release.",
            identity_match="domain",
        ),
        _external(
            "exa.2",
            "https://wire-two.test/copy",
            "Example announced the same syndicated release.",
            identity_match="domain",
        ),
    ]

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", rows),
            _report("two", "2026-01-02T00:00:00Z", deepcopy(rows)),
        ]
    )

    assert result["summary"]["current_external_publisher_count"] == 2
    assert result["summary"]["current_external_syndication_cluster_count"] == 1
    assert result["summary"]["current_independent_external_cluster_count"] == 1
    assert {
        entry["independence_cluster_id"] for entry in result["entries"]
    } == {result["entries"][0]["independence_cluster_id"]}
    assert all(
        entry["independence_cluster_member_count"] == 2
        for entry in result["entries"]
    )


def test_different_external_passages_from_one_publisher_are_one_cluster() -> None:
    rows = [
        _external(
            "exa.1",
            "https://press.test/one",
            "First independent-looking article.",
            identity_match="domain",
        ),
        _external(
            "exa.2",
            "https://press.test/two",
            "Second independent-looking article.",
            identity_match="domain",
        ),
    ]

    result = build_evidence_memory_identity_v2(
        [_report("one", "2026-01-01T00:00:00Z", rows)]
    )

    assert result["summary"]["current_external_publisher_count"] == 1
    assert result["summary"]["current_external_syndication_cluster_count"] == 2
    assert result["summary"]["current_independent_external_cluster_count"] == 1


def test_historical_external_passages_do_not_bridge_current_clusters() -> None:
    historical = [
        _external(
            "exa.old.1",
            "https://publisher-one.test/old",
            "The same historical syndicated copy.",
        ),
        _external(
            "exa.old.2",
            "https://publisher-two.test/old",
            "The same historical syndicated copy.",
        ),
    ]
    current = [
        _external(
            "exa.current.1",
            "https://publisher-one.test/current",
            "A current independent article.",
        ),
        _external(
            "exa.current.2",
            "https://publisher-two.test/current",
            "A different current independent article.",
        ),
    ]

    result = build_evidence_memory_identity_v2(
        [
            _report("old", "2026-01-01T00:00:00Z", historical),
            _report("current", "2026-01-02T00:00:00Z", current),
        ]
    )
    current_entries = [
        entry for entry in result["entries"] if entry["present_in_latest"]
    ]
    historical_entries = [
        entry for entry in result["entries"] if not entry["present_in_latest"]
    ]

    assert result["summary"]["current_independent_external_cluster_count"] == 2
    assert len(
        {entry["independence_cluster_id"] for entry in current_entries}
    ) == 2
    assert all(
        entry["independence_cluster_id"] == ""
        and entry["independence_cluster_member_count"] == 0
        for entry in historical_entries
    )


def test_projection_is_order_invariant_and_deduplicates_refs() -> None:
    row = _owned("web.0", "A stable claim.")
    duplicate = {**deepcopy(row), "ref": "raw_inputs.9"}
    first = _report("one", "2026-01-01T00:00:00Z", [row, duplicate])
    second = _report("two", "2026-01-02T00:00:00Z", [deepcopy(row)])

    forward = build_evidence_memory_identity_v2([first, second])
    reverse = build_evidence_memory_identity_v2([second, first])

    assert forward["state_fingerprint"] == reverse["state_fingerprint"]
    assert forward["summary"]["entry_count"] == 1
    assert forward["entries"][0]["observation_count"] == 2
    assert forward["entries"][0]["observations"][0]["duplicate_ref_count"] == 2


def _owned(ref: str, content: str, *, claim_id: str = "") -> dict:
    metadata = {"source_class": "owned_copy"}
    if claim_id:
        metadata["claim_id"] = claim_id
    return {
        "ref": ref,
        "source": "web",
        "evidence_type": "raw_input",
        "url": "https://example.com/about",
        "content": content,
        "metadata": metadata,
    }


def _external(
    ref: str,
    url: str,
    content: str,
    *,
    identity_match: str = "",
    identity_match_llm: str = "",
    provenance: dict | None = None,
) -> dict:
    metadata = {"source_class": "external_proof"}
    if identity_match:
        metadata["identity_match"] = identity_match
    if identity_match_llm:
        metadata["identity_match_llm"] = identity_match_llm
    if provenance:
        metadata["external_identity_provenance"] = deepcopy(provenance)
    return {
        "ref": ref,
        "source": "exa",
        "evidence_type": "external_proof.external_mentions",
        "url": url,
        "content": content,
        "metadata": metadata,
    }


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


def _report(
    report_id: str,
    created_at: str,
    evidence: list[dict],
) -> dict:
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "score": 50,
        "reliability_status": "shadow",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "blocks": [],
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "brand_name": "Example",
                        "url": "https://example.com",
                        "evidence": deepcopy(evidence),
                    }
                }
            }
        },
    }
