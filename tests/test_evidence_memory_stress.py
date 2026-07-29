from __future__ import annotations

from copy import deepcopy

import src.services.evidence_memory_stress as stress_module
from src.services.evidence_claim_relation_gold_set import (
    EvidenceClaimRelationGoldSetError,
)
from src.services.evidence_identity_gold_set import (
    EvidenceIdentityGoldSetError,
)
from src.services.evidence_memory_stress import (
    render_evidence_memory_stress_markdown,
    run_evidence_memory_stress,
)


def test_controlled_stress_supports_foundation_but_blocks_promotion() -> None:
    report = run_evidence_memory_stress({})
    probes = {probe["id"]: probe for probe in report["controlled_probes"]}

    assert report["mutates_state"] is False
    assert report["verdict"] == "foundation_supported_promotion_blocked"
    assert report["promotion_ready"] is False
    assert report["executable_failures"] == []
    assert report["schema_version"] == "evidence-memory-stress-v2"
    assert report["policy_version"] == "evidence-memory-stress-policy-v8"
    assert report["summary"]["executable_invariant_count"] == 24
    assert report["summary"]["promotion_blocker_count"] == 4
    assert report["summary"]["claim_relation_gold_candidate_count"] == 13
    assert report["summary"]["claim_relation_gold_reviewed_count"] == 13
    assert report["summary"]["claim_relation_gold_pending_count"] == 0
    assert report["summary"]["identity_gold_candidate_count"] == 14
    assert report["summary"]["identity_gold_reviewed_count"] == 14
    assert report["summary"]["identity_gold_pending_count"] == 0
    assert (
        report["summary"]["identity_gold_exact_agreement_rate"]
        == 1.0
    )
    assert (
        report["summary"]["identity_gold_critical_false_accept_count"]
        == 0
    )
    assert (
        report["summary"][
            "claim_relation_gold_reviewed_real_replacement_count"
        ]
        == 0
    )
    assert all(
        probe["status"] == "pass"
        for probe in probes.values()
        if probe["kind"] == "executable_invariant"
    )


def test_stress_exposes_poisoning_and_syndication_instead_of_false_green() -> None:
    report = run_evidence_memory_stress({})
    probes = {probe["id"]: probe for probe in report["controlled_probes"]}

    identity_v4 = probes["identity_v4_matches_frozen_human_reviews"]
    source_independence = probes[
        "source_independence_pending_production_review"
    ]
    assert identity_v4["status"] == "pass"
    assert "14 unchanged human decisions" in identity_v4["observation"]
    assert source_independence["status"] == "blocked"
    assert "no production-reviewed source" in source_independence["observation"]
    assert probes["identity_v4_explicit_other_entity_is_rejected"][
        "status"
    ] == "pass"
    assert probes["accepted_identity_never_grants_runtime_authority"][
        "status"
    ] == "pass"
    assert probes["identity_v3_exact_syndication_is_one_source_cluster"][
        "status"
    ] == "pass"
    assert probes[
        "identity_v3_paraphrased_syndication_is_not_independent"
    ]["status"] == "pass"
    assert probes[
        "identity_v3_unknown_ownership_never_counts_as_independent"
    ]["status"] == "pass"
    assert probes["identity_v2_stable_claim_slot_surfaces_revision"][
        "status"
    ] == "pass"
    assert probes[
        "identity_v4_upstream_domain_label_cannot_override_conflict"
    ]["status"] == "pass"
    assert probes[
        "identity_v2_reproduced_external_identity_is_eligible"
    ]["status"] == "pass"
    assert probes[
        "claim_memory_rejects_bare_content_derived_claim_id"
    ]["status"] == "pass"
    assert probes[
        "claim_memory_separates_slot_variant_and_occurrence"
    ]["status"] == "pass"
    assert probes[
        "claim_memory_simultaneous_variants_are_coexistence_candidates"
    ]["status"] == "pass"
    assert probes["claim_memory_report_order_invariance"]["status"] == "pass"
    assert probes[
        "accepted_claim_relation_never_grants_canonical_authority"
    ]["status"] == "pass"
    assert probes[
        "claim_slot_producer_is_explicit_scoped_and_shadow_only"
    ]["status"] == "pass"
    assert probes[
        "historical_claim_backfill_reuses_v1_evidence_without_mutation"
    ]["status"] == "pass"
    assert probes[
        "claim_tile_mapping_is_literal_versioned_and_shadow_only"
    ]["status"] == "pass"
    tile_mapping = probes[
        "tile_mapping_pending_reviewed_promotion_policy"
    ]
    assert tile_mapping["status"] == "blocked"
    assert "persistent versioned" in tile_mapping["observation"]
    assert "review real mapping coverage" in tile_mapping[
        "required_capability"
    ]
    claim_relation = probes["changed_claim_has_no_canonical_resolution"]
    assert "13 cases" in claim_relation["observation"]
    assert "0 pending reviews" in claim_relation["observation"]
    assert "real replacements" in claim_relation["required_capability"]


def test_missing_claim_relation_fixture_fails_closed(monkeypatch) -> None:
    def _missing_candidates() -> list[dict]:
        raise EvidenceClaimRelationGoldSetError("missing fixture")

    monkeypatch.setattr(
        stress_module,
        "load_claim_relation_gold_candidates",
        _missing_candidates,
    )

    report = run_evidence_memory_stress({})

    assert report["summary"]["claim_relation_gold_candidate_count"] == 0
    assert report["claim_relation_gold_set"]["promotion_ready"] is False
    assert report["claim_relation_gold_set"]["promotion_blockers"] == [
        "claim_relation_gold_set_unavailable"
    ]


def test_missing_identity_fixture_fails_closed(monkeypatch) -> None:
    def _missing_candidates() -> list[dict]:
        raise EvidenceIdentityGoldSetError("missing fixture")

    monkeypatch.setattr(
        stress_module,
        "load_identity_gold_candidates",
        _missing_candidates,
    )

    report = run_evidence_memory_stress({})

    assert report["summary"]["identity_gold_candidate_count"] == 0
    assert report["identity_gold_set"]["promotion_ready"] is False
    assert report["identity_gold_set"]["promotion_blockers"] == [
        "identity_gold_set_unavailable"
    ]


def test_real_history_replay_checks_invariants_without_mutating_reports() -> None:
    row = _evidence("web.0", "A stable owned claim.")
    first = _report("one", "2026-01-01T00:00:00Z", 53, [row])
    second = _report(
        "two",
        "2026-01-02T00:00:00Z",
        34,
        [{**deepcopy(row), "ref": "raw_inputs.9"}],
    )
    histories = {"example.com": [second, first]}
    before = deepcopy(histories)

    report = run_evidence_memory_stress(histories)
    replay = report["real_history_replay"]

    assert histories == before
    assert replay["history_count"] == 1
    assert replay["history_with_material_evidence_count"] == 1
    assert all(check["status"] == "pass" for check in replay["checks"])
    assert all(check["rate"] == 1.0 for check in replay["checks"])
    assert replay["histories"][0]["score_range"] == 19.0
    assert replay["histories"][0]["evidence_identity_count"] == 1
    assert report["identity_v2_replay"]["summary"]["history_count"] == 1
    assert report["identity_v2_replay"]["summary"][
        "v2_revision_candidate_count"
    ] == 0
    assert report["claim_memory_replay"]["summary"]["history_count"] == 1
    assert report["claim_memory_replay"]["summary"]["claim_slot_count"] == 0
    assert report["claim_tile_ledger_replay"]["summary"][
        "history_count"
    ] == 1
    assert report["claim_tile_ledger_replay"]["authority"] is False
    assert (
        report["claim_tile_ledger_replay"]["runtime_effect"] is False
    )


def test_histories_without_material_evidence_are_reported_but_not_used_as_proof() -> None:
    empty = _report("empty", "2026-01-01T00:00:00Z", 50, [])

    report = run_evidence_memory_stress({"empty.test": [empty]})
    replay = report["real_history_replay"]

    assert replay["history_count"] == 1
    assert replay["history_with_material_evidence_count"] == 0
    assert all(check["total"] == 0 for check in replay["checks"])


def test_diagnostic_exposes_multiple_claims_sharing_one_coarse_locator() -> None:
    report = _report(
        "one",
        "2026-01-01T00:00:00Z",
        50,
        [
            _evidence("web.0", "We serve finance teams."),
            _evidence("web.1", "We automate monthly reporting."),
        ],
    )

    result = run_evidence_memory_stress({"example.com": [report]})
    diagnostic = result["real_history_replay"]["histories"][0]

    assert diagnostic["evidence_identity_count"] == 2
    assert diagnostic["locator_count"] == 1
    assert diagnostic["multi_variant_locator_count"] == 1
    assert diagnostic["entries_in_multi_variant_locators"] == 2
    assert diagnostic["multi_variant_entry_rate"] == 1.0
    assert "## Locator pressure" in render_evidence_memory_stress_markdown(result)


def test_identity_v2_replay_removes_url_only_change_pressure() -> None:
    first = _report(
        "one",
        "2026-01-01T00:00:00Z",
        50,
        [_evidence("web.0", "We serve finance teams.")],
    )
    second = _report(
        "two",
        "2026-01-02T00:00:00Z",
        50,
        [_evidence("web.0", "We serve operations teams.")],
    )

    result = run_evidence_memory_stress({"example.com": [first, second]})
    comparison = result["identity_v2_replay"]["histories"][0]

    assert comparison["v1_changed_candidate_count"] == 1
    assert comparison["v2_revision_candidate_count"] == 0
    assert comparison["removed_url_only_change_pressure_count"] == 1
    assert comparison["retained_explicit_revision_count"] == 0
    assert comparison["v2_claim_slot_count"] == 0


def test_identity_v2_replay_only_retains_change_with_stable_slot() -> None:
    old = _evidence("web.0", "We serve finance teams.")
    old["metadata"]["claim_id"] = "audience-primary"
    new = _evidence("web.0", "We serve operations teams.")
    new["metadata"]["claim_id"] = "audience-primary"

    result = run_evidence_memory_stress(
        {
            "example.com": [
                _report("one", "2026-01-01T00:00:00Z", 50, [old]),
                _report("two", "2026-01-02T00:00:00Z", 50, [new]),
            ]
        }
    )
    comparison = result["identity_v2_replay"]["histories"][0]

    assert comparison["v1_changed_candidate_count"] == 1
    assert comparison["v2_revision_candidate_count"] == 1
    assert comparison["removed_url_only_change_pressure_count"] == 0
    assert comparison["retained_explicit_revision_count"] == 1
    assert comparison["v2_claim_slot_method_counts"] == {
        "explicit_claim_id": 1
    }


def test_claim_memory_replay_requires_semantic_slot_metadata() -> None:
    old = _evidence("web.0", "We serve finance teams.")
    old["metadata"]["claim_slot_key"] = "audience.primary"
    old["metadata"]["claim_type"] = "audience"
    new = _evidence("web.0", "We serve operations teams.")
    new["metadata"]["claim_slot_key"] = "audience.primary"
    new["metadata"]["claim_type"] = "audience"

    result = run_evidence_memory_stress(
        {
            "example.com": [
                _report("one", "2026-01-01T00:00:00Z", 50, [old]),
                _report("two", "2026-01-02T00:00:00Z", 50, [new]),
            ]
        }
    )
    replay = result["claim_memory_replay"]
    history = replay["histories"][0]

    assert replay["authority"] is False
    assert replay["runtime_effect"] is False
    assert replay["summary"]["claim_slot_count"] == 1
    assert replay["summary"]["claim_variant_count"] == 2
    assert replay["summary"]["claim_occurrence_count"] == 2
    assert replay["summary"]["relation_candidate_counts"] == {
        "replacement_candidate": 1
    }
    assert history["claim_slot_method_counts"] == {
        "explicit_claim_slot_key": 1
    }


def test_markdown_distinguishes_passes_from_promotion_blockers() -> None:
    rendered = render_evidence_memory_stress_markdown(
        run_evidence_memory_stress({})
    )

    assert "# Evidence memory stress report" in rendered
    assert "foundation_supported_promotion_blocked" in rendered
    assert "## Controlled probes" in rendered
    assert "## Identity v2 comparison" in rendered
    assert "## Claim Memory v1 replay" in rendered
    assert "## Evidence → claim → tile ledger replay" in rendered
    assert "## Identity review set" in rendered
    assert "## Claim relation review set" in rendered
    assert "Candidates: `13`" in rendered
    assert "Pending: `0`" in rendered
    assert "## Locator pressure" not in rendered
    assert "## Promotion blockers" in rendered
    assert "no_versioned_memory_evaluator" in rendered


def _evidence(ref: str, content: str) -> dict:
    return {
        "ref": ref,
        "source": "web",
        "evidence_type": "raw_input",
        "url": "https://example.com/about",
        "content": content,
        "metadata": {"source_class": "owned_copy"},
    }


def _report(
    report_id: str,
    created_at: str,
    score: int,
    evidence: list[dict],
) -> dict:
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "score": score,
        "reliability_status": "shadow",
        "acquisition_gate": {"state": "pass"},
        "components": [
            {
                "key": "mission",
                "status": "scored",
                "score": 5,
                "tile_profile": [{"id": "M1", "estado": "ok"}],
            }
        ],
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
