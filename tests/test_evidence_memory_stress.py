from __future__ import annotations

from copy import deepcopy

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
    assert report["summary"]["executable_invariant_count"] == 12
    assert report["summary"]["promotion_blocker_count"] == 5
    assert all(
        probe["status"] == "pass"
        for probe in probes.values()
        if probe["kind"] == "executable_invariant"
    )


def test_stress_exposes_poisoning_and_syndication_instead_of_false_green() -> None:
    report = run_evidence_memory_stress({})
    probes = {probe["id"]: probe for probe in report["controlled_probes"]}

    poison = probes["repeated_false_identity_can_become_validation_candidate"]
    syndication = probes["syndication_is_not_clustered"]

    assert poison["status"] == "blocked"
    assert "`validation_candidate`" in poison["observation"]
    assert "rejected/revoked" in poison["required_capability"]
    assert syndication["status"] == "blocked"
    assert "2 validation candidates" in syndication["observation"]
    assert probes["identity_v2_weak_external_identity_is_not_eligible"][
        "status"
    ] == "pass"
    assert probes["identity_v2_exact_syndication_is_one_independent_cluster"][
        "status"
    ] == "pass"
    assert probes["identity_v2_stable_claim_slot_surfaces_revision"][
        "status"
    ] == "pass"
    assert probes[
        "identity_v2_bare_upstream_domain_label_is_not_eligible"
    ]["status"] == "pass"
    assert probes[
        "identity_v2_reproduced_external_identity_is_eligible"
    ]["status"] == "pass"


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


def test_markdown_distinguishes_passes_from_promotion_blockers() -> None:
    rendered = render_evidence_memory_stress_markdown(
        run_evidence_memory_stress({})
    )

    assert "# Evidence memory stress report" in rendered
    assert "foundation_supported_promotion_blocked" in rendered
    assert "## Controlled probes" in rendered
    assert "## Identity v2 comparison" in rendered
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
