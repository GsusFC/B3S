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
    assert report["summary"]["executable_invariant_count"] == 6
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


def test_markdown_distinguishes_passes_from_promotion_blockers() -> None:
    rendered = render_evidence_memory_stress_markdown(
        run_evidence_memory_stress({})
    )

    assert "# Evidence memory stress report" in rendered
    assert "foundation_supported_promotion_blocked" in rendered
    assert "## Controlled probes" in rendered
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
