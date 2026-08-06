from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from src.external_identity_provenance import build_external_identity_provenance
from src.services.evidence_memory_identity_v2 import (
    build_accepted_evidence_passage_catalog,
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
    assert result["policy_version"] == "evidence-memory-identity-policy-v4"
    assert result["source_independence"]["schema_version"] == (
        "evidence-source-independence-v3"
    )
    assert result["source_independence"]["production_reviewed_source_count"] == 0
    assert result["source_independence"]["authority"] is False
    assert result["summary"]["entry_count"] == 2
    assert result["summary"]["document_count"] == 1
    assert result["summary"]["passage_count"] == 2
    assert result["summary"]["multi_passage_document_count"] == 1
    assert result["summary"]["revision_candidate_count"] == 0
    assert {entry["state"] for entry in result["entries"]} == {"observed"}
    assert all(not entry["claim_slot_id"] for entry in result["entries"])


def test_accepted_passage_catalog_rehydrates_exact_immutable_content() -> None:
    report = _report(
        "one",
        "2026-01-01T00:00:00Z",
        [_owned("web.0", "End the chase between companies.")],
    )
    identity = build_evidence_memory_identity_v2([report])
    evidence_id = identity["entries"][0]["evidence_id"]
    catalog = build_accepted_evidence_passage_catalog(
        [report],
        adjudications=[
            {
                "id": "review-1",
                "subject_type": "evidence",
                "subject_id": evidence_id,
                "sequence": 1,
                "decision": "accepted",
                "reviewer": "gsus",
                "reason_code": "brand_named_in_passage",
                "rationale": "The owned passage identity was reviewed.",
                "evaluator_version": "manual-review-v1",
                "actor_id": "gsus",
                "created_at": "2026-01-02T00:00:00Z",
                "runtime_effect": False,
                "authority": False,
            }
        ],
    )

    assert catalog["entry_count"] == 1
    assert catalog["entries"][0]["evidence_id"] == evidence_id
    assert catalog["entries"][0]["content"] == (
        "End the chase between companies."
    )
    assert catalog["entries"][0]["adjudication_state"] == "accepted"
    assert catalog["runtime_effect"] is False
    assert catalog["authority"] is False


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


def test_llm_positive_label_cannot_override_reproducible_entity_conflict() -> None:
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

    assert entry["identity_status"] == "mismatch"
    assert entry["identity_strength"] == "negative"
    assert entry["qualified_observation_count"] == 0
    assert entry["state"] == "repeated"
    assert all(
        "reproducible_entity_conflict:explicit_other_entity"
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


def test_identity_v4_boundary_fixture_keeps_unresolved_separate_from_negative() -> None:
    root = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "evidence_identity_policy"
        / "v4"
    )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    case_bytes = (root / "boundary_cases.jsonl").read_bytes()
    cases = [
        json.loads(line)
        for line in case_bytes.decode("utf-8").splitlines()
        if line.strip()
    ]
    decision_by_status = {
        "eligible": "accepted",
        "mismatch": "rejected",
        "disputed": "disputed",
        "unverified": "disputed",
    }

    assert manifest["policy_version"] == "evidence-memory-identity-policy-v4"
    assert manifest["runtime_effect"] is False
    assert manifest["authority"] is False
    assert manifest["case_count"] == len(cases) == 7
    assert manifest["case_file_sha256"] == hashlib.sha256(
        case_bytes
    ).hexdigest()

    for case in cases:
        result = build_evidence_memory_identity_v2(
            [
                _report(
                    str(case["case_id"]),
                    "2026-01-01T00:00:00Z",
                    [case["evidence"]],
                )
            ]
        )
        entry = result["entries"][0]
        assert decision_by_status[entry["identity_status"]] == case[
            "expected_decision"
        ], case["case_id"]
        assert entry["identity_strength"] == case[
            "expected_identity_strength"
        ], case["case_id"]

    llm_only = next(
        case for case in cases if case["case_id"] == "llm-only-conflict"
    )
    llm_result = build_evidence_memory_identity_v2(
        [
            _report(
                "llm-only",
                "2026-01-01T00:00:00Z",
                [llm_only["evidence"]],
            )
        ]
    )
    llm_observation = llm_result["entries"][0]["observations"][0]
    assert llm_observation["identity_status"] == "unverified"
    assert "llm_entity_conflict_not_authoritative" in llm_observation[
        "identity_reason_codes"
    ]
    assert "evidence_spans" not in llm_observation[
        "entity_conflict_provenance"
    ][0]


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
    assert result["summary"]["current_external_cluster_count"] == 1
    assert result["summary"]["current_independent_external_cluster_count"] == 0
    assert result["summary"][
        "current_external_independence_cluster_status_counts"
    ] == {"same_cluster": 1}
    assert {
        entry["independence_cluster_id"] for entry in result["entries"]
    } == {result["entries"][0]["independence_cluster_id"]}
    assert all(
        entry["independence_cluster_member_count"] == 2
        for entry in result["entries"]
    )
    assert {entry["independence_status"] for entry in result["entries"]} == {
        "same_cluster"
    }


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
    assert result["summary"]["current_external_cluster_count"] == 1
    assert result["summary"]["current_independent_external_cluster_count"] == 0
    assert {entry["independence_status"] for entry in result["entries"]} == {
        "same_cluster"
    }


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

    assert result["summary"]["current_external_cluster_count"] == 2
    assert result["summary"]["current_independent_external_cluster_count"] == 0
    assert {entry["independence_status"] for entry in current_entries} == {
        "unknown"
    }
    assert len(
        {entry["independence_cluster_id"] for entry in current_entries}
    ) == 2
    assert all(
        entry["independence_cluster_id"] == ""
        and entry["independence_cluster_member_count"] == 0
        for entry in historical_entries
    )


def test_lightly_paraphrased_external_copy_is_one_cluster() -> None:
    rows = [
        _external(
            "exa.1",
            "https://publisher-one.test/story",
            (
                "Example announced a new platform that helps finance teams close "
                "monthly books faster with automated reporting across every subsidiary."
            ),
        ),
        _external(
            "exa.2",
            "https://publisher-two.test/copy",
            (
                "Example announced a new platform that helps finance teams close "
                "monthly books faster using automated reporting across every subsidiary."
            ),
        ),
    ]

    result = build_evidence_memory_identity_v2(
        [_report("one", "2026-01-01T00:00:00Z", rows)]
    )

    assert result["summary"]["current_external_cluster_count"] == 1
    assert result["summary"]["current_independent_external_cluster_count"] == 0
    assert {entry["independence_status"] for entry in result["entries"]} == {
        "same_cluster"
    }
    assert all(
        "near_duplicate_content" in entry["independence_reason_codes"]
        for entry in result["entries"]
    )


def test_ambiguous_similarity_is_disputed_and_never_counts() -> None:
    rows = [
        _external(
            "exa.1",
            "https://publisher-one.test/story",
            (
                "Example announced a new platform that helps finance teams close "
                "monthly books faster with automated reporting across every subsidiary."
            ),
        ),
        _external(
            "exa.2",
            "https://publisher-two.test/story",
            (
                "Example announced a new platform that helps operations teams close "
                "monthly books faster using automated reporting across every subsidiary."
            ),
        ),
    ]

    result = build_evidence_memory_identity_v2(
        [_report("one", "2026-01-01T00:00:00Z", rows)]
    )

    assert result["summary"]["current_external_cluster_count"] == 2
    assert result["summary"]["current_independent_external_cluster_count"] == 0
    assert {entry["independence_status"] for entry in result["entries"]} == {
        "disputed"
    }
    assert all(
        entry["independence_requires_human_review"] is True
        for entry in result["entries"]
    )


def test_reviewed_publisher_groups_can_confirm_distinct_eligible_sources() -> None:
    rows = [
        _external(
            "exa.1",
            "https://alpha-news.test/story",
            "Alpha independently documented Example's launch.",
            provenance=_provenance("https://alpha-news.test/story"),
        ),
        _external(
            "exa.2",
            "https://gamma-news.test/report",
            "Gamma separately interviewed Example customers.",
            provenance=_provenance("https://gamma-news.test/report"),
        ),
    ]

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", rows),
            _report("two", "2026-01-02T00:00:00Z", deepcopy(rows)),
        ]
    )

    assert result["summary"]["current_external_cluster_count"] == 2
    assert result["summary"]["current_independent_external_cluster_count"] == 2
    assert result["summary"][
        "current_confirmed_independent_external_cluster_count"
    ] == 2
    assert {entry["independence_status"] for entry in result["entries"]} == {
        "confirmed_independent"
    }
    assert all(
        entry["independence_requires_human_review"] is False
        for entry in result["entries"]
    )


def test_same_reviewed_publisher_group_is_one_cluster() -> None:
    rows = [
        _external(
            "exa.1",
            "https://alpha-news.test/story",
            "Alpha documented the product launch.",
            provenance=_provenance("https://alpha-news.test/story"),
        ),
        _external(
            "exa.2",
            "https://beta-news.test/report",
            "Beta interviewed a customer about the product.",
            provenance=_provenance("https://beta-news.test/report"),
        ),
    ]

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", rows),
            _report("two", "2026-01-02T00:00:00Z", deepcopy(rows)),
        ]
    )

    assert result["summary"]["current_external_cluster_count"] == 1
    assert result["summary"]["current_independent_external_cluster_count"] == 0
    assert {entry["publisher_group_id"] for entry in result["entries"]} == {
        "fixture-editorial-group-one"
    }
    assert {entry["independence_status"] for entry in result["entries"]} == {
        "same_cluster"
    }


def test_explicit_original_source_lineage_collapses_republished_copy() -> None:
    origin_url = "https://origin.test/release"
    rows = [
        _external(
            "exa.1",
            origin_url,
            "Original release with complete launch details.",
        ),
        _external(
            "exa.2",
            "https://publisher.test/copy",
            "A shortened copy of the launch announcement.",
            original_source_url=origin_url,
            distribution_type="press_release",
        ),
    ]

    result = build_evidence_memory_identity_v2(
        [_report("one", "2026-01-01T00:00:00Z", rows)]
    )

    assert result["summary"]["current_external_cluster_count"] == 1
    assert {entry["independence_status"] for entry in result["entries"]} == {
        "same_cluster"
    }
    assert all(
        "shared_source_lineage" in entry["independence_reason_codes"]
        for entry in result["entries"]
    )


def test_unreviewed_publisher_is_unknown_even_with_eligible_identity() -> None:
    row = _external(
        "exa.1",
        "https://unreviewed.test/story",
        "An eligible external identity with unknown publisher ownership.",
        provenance=_provenance("https://unreviewed.test/story"),
    )

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", [row]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(row)]),
        ]
    )
    entry = result["entries"][0]

    assert result["summary"]["current_independent_external_cluster_count"] == 0
    assert entry["independence_status"] == "unknown"
    assert entry["independence_requires_human_review"] is True
    assert "unreviewed_publisher_ownership" in entry["independence_reason_codes"]
    assert "_source_independence_shingles" not in entry


def test_reviewed_group_without_source_review_stays_unknown() -> None:
    row = _external(
        "exa.1",
        "https://alpha-news.test/unreviewed",
        "Publisher ownership alone cannot prove original reporting.",
        provenance=_provenance("https://alpha-news.test/unreviewed"),
    )

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", [row]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(row)]),
        ]
    )
    entry = result["entries"][0]

    assert entry["publisher_registry_status"] == "fixture"
    assert entry["source_independence_review_status"] == "unreviewed"
    assert entry["independence_status"] == "unknown"
    assert "source_not_manually_reviewed" in entry["independence_reason_codes"]


def test_canonical_alias_does_not_borrow_another_url_source_review() -> None:
    row = _external(
        "exa.1",
        "https://alpha-news.test/reprint",
        "A reprint cannot inherit the reviewed origin's independence decision.",
        provenance=_provenance("https://alpha-news.test/reprint"),
        canonical_url="https://alpha-news.test/story",
    )

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", [row]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(row)]),
        ]
    )
    entry = result["entries"][0]

    assert entry["canonical_urls"] == [
        "https://alpha-news.test/reprint",
        "https://alpha-news.test/story",
    ]
    assert entry["source_independence_review_status"] == "unreviewed"
    assert entry["independence_status"] == "unknown"


def test_press_release_label_blocks_confirmed_independence() -> None:
    row = _external(
        "exa.1",
        "https://alpha-news.test/story",
        "A reviewed URL explicitly identified as a press release.",
        provenance=_provenance("https://alpha-news.test/story"),
        distribution_type="press_release",
    )

    result = build_evidence_memory_identity_v2(
        [
            _report("one", "2026-01-01T00:00:00Z", [row]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(row)]),
        ]
    )
    entry = result["entries"][0]

    assert entry["source_independence_review_status"] == "fixture"
    assert entry["independence_status"] == "unknown"
    assert "wire_or_press_release_lineage_unresolved" in (
        entry["independence_reason_codes"]
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


def test_source_independence_is_invariant_to_evidence_row_order() -> None:
    rows = [
        _external(
            "exa.1",
            "https://publisher-one.test/story",
            (
                "Example announced a new platform that helps finance teams close "
                "monthly books faster with automated reporting across every subsidiary."
            ),
        ),
        _external(
            "exa.2",
            "https://publisher-two.test/copy",
            (
                "Example announced a new platform that helps finance teams close "
                "monthly books faster using automated reporting across every subsidiary."
            ),
        ),
    ]

    forward = build_evidence_memory_identity_v2(
        [_report("one", "2026-01-01T00:00:00Z", rows)]
    )
    reverse = build_evidence_memory_identity_v2(
        [_report("one", "2026-01-01T00:00:00Z", list(reversed(rows)))]
    )

    assert forward["state_fingerprint"] == reverse["state_fingerprint"]


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
    canonical_url: str = "",
    original_source_url: str = "",
    distribution_type: str = "",
) -> dict:
    metadata = {"source_class": "external_proof"}
    if identity_match:
        metadata["identity_match"] = identity_match
    if identity_match_llm:
        metadata["identity_match_llm"] = identity_match_llm
    if provenance:
        metadata["external_identity_provenance"] = deepcopy(provenance)
    if canonical_url:
        metadata["canonical_url"] = canonical_url
    if original_source_url:
        metadata["original_source_url"] = original_source_url
    if distribution_type:
        metadata["distribution_type"] = distribution_type
    return {
        "ref": ref,
        "source": "exa",
        "evidence_type": "external_proof.external_mentions",
        "url": url,
        "content": content,
        "metadata": metadata,
    }


def _provenance(source_url: str = "https://press.test/story") -> dict:
    return build_external_identity_provenance(
        provider="exa",
        subject_url="https://example.com",
        source_url=source_url,
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
