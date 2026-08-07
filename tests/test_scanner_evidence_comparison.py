from __future__ import annotations

from copy import deepcopy

from src.services.scanner_evidence_comparison import (
    annotate_report_history,
    build_evidence_snapshot,
    canonical_evidence_records,
    classify_report_history,
    compare_reports,
    render_history_dry_run,
    selected_report_for_display,
)


def test_evidence_fingerprint_ignores_record_order_and_unstable_refs() -> None:
    first = _report(
        "first",
        "2026-07-25T00:00:00Z",
        evidence=[
            _evidence("web.1", "web", "owned_copy", "https://example.com/", "A clear offer."),
            _evidence("exa.1", "exa", "external_proof", "https://press.test/story", "Independent proof."),
        ],
    )
    second = deepcopy(first)
    second["id"] = "second"
    rows = second["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"]
    rows.reverse()
    rows[0]["ref"] = "raw_inputs.9.exa.result.4"
    rows[1]["ref"] = "raw_inputs.3"

    first_snapshot = build_evidence_snapshot(first)
    second_snapshot = build_evidence_snapshot(second)

    assert first_snapshot.fingerprint == second_snapshot.fingerprint
    assert first_snapshot.semantic_fingerprint == second_snapshot.semantic_fingerprint


def test_legacy_scanner_duplicate_representative_remains_last_row() -> None:
    report = _report(
        "legacy-duplicate",
        "2026-07-25T00:00:00Z",
        evidence=[
            _evidence(
                "proof.alpha",
                "alpha",
                "external_proof",
                "https://proof.test/story",
                "The same independent proof.",
            ),
            _evidence(
                "proof.zeta",
                "zeta",
                "external_proof",
                "https://proof.test/story",
                "The same independent proof.",
            ),
        ],
    )

    records = canonical_evidence_records(report)

    assert len(records) == 1
    assert records[0].source == "zeta"


def test_comparator_flags_evaluation_drift_for_equivalent_evidence() -> None:
    shared = [
        _evidence("web.1", "web", "owned_copy", "https://example.com", "A clear offer."),
        _evidence("exa.1", "exa", "external_proof", "https://press.test/one", "Repeated external proof."),
        _evidence("exa.2", "exa", "external_proof", "https://press.test/two", "A second independent source."),
    ]
    baseline = _report(
        "baseline",
        "2026-07-25T00:00:00Z",
        score=47,
        evidence=shared,
        component_score=3,
        component_status="scored",
        tile_states={"P4": "ok", "P5": "ok", "P8": "ok"},
    )
    candidate = _report(
        "candidate",
        "2026-07-26T00:00:00Z",
        score=40,
        evidence=[
            deepcopy(shared[2]),
            {**deepcopy(shared[0]), "ref": "raw_inputs.8"},
            {**deepcopy(shared[1]), "ref": "raw_inputs.5"},
        ],
        component_score=0,
        component_status="scored",
        tile_states={"P4": "no", "P5": "no", "P8": "no"},
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison.classification == "evaluation_drift"
    assert comparison.equivalent_evidence is True
    assert comparison.acquisition_comparable is True
    assert comparison.evaluation_changed is True
    assert comparison.delta["changed_components"][0]["component"] == "value_proposition"


def test_comparator_does_not_call_different_rubrics_evaluation_drift() -> None:
    baseline = _report(
        "baseline",
        "2026-07-25T00:00:00Z",
        component_score=3,
    )
    candidate = _report(
        "candidate",
        "2026-07-26T00:00:00Z",
        component_score=0,
    )
    baseline["raw"]["sv9"] = {
        "result": {"rubric_version": "baldosas-v3-1"}
    }
    candidate["raw"]["sv9"] = {
        "result": {"rubric_version": "baldosas-v3-2"}
    }

    comparison = compare_reports(baseline, candidate)

    assert comparison.classification == "contract_mismatch"
    assert comparison.contract_comparable is False
    assert comparison.reason_codes == ("analysis_contract_changed",)
    assert (
        comparison.delta["baseline_analysis_contract"]["rubric_version"]
        == "baldosas-v3-1"
    )


def test_comparator_detects_legacy_evaluator_model_change_as_contract_mismatch() -> None:
    baseline = _report("baseline", "2026-07-25T00:00:00Z")
    candidate = _report("candidate", "2026-07-26T00:00:00Z")
    baseline["raw"]["sv9"] = {
        "result": {"evaluation_model": "gemini-2.5-flash"}
    }
    candidate["raw"]["sv9"] = {
        "result": {"evaluation_model": "gemini-3-flash"}
    }

    comparison = compare_reports(baseline, candidate)

    assert comparison.classification == "contract_mismatch"
    assert (
        comparison.delta["baseline_analysis_contract"]["evaluator_model"]
        == "gemini-2.5-flash"
    )


def test_history_promotes_repeated_reliable_new_contract_without_locking_old_baseline() -> None:
    old = _report("old", "2026-07-25T00:00:00Z")
    first_new = _report("first-new", "2026-07-26T00:00:00Z")
    repeated_new = _report("repeated-new", "2026-07-27T00:00:00Z")
    for report, rubric in (
        (old, "baldosas-v3-1"),
        (first_new, "baldosas-v3-2"),
        (repeated_new, "baldosas-v3-2"),
    ):
        report["reliability_status"] = "reliable"
        report["acquisition_gate"] = {"state": "pass", "warnings": []}
        report["raw"]["sv9"] = {"result": {"rubric_version": rubric}}

    state = classify_report_history([old, first_new, repeated_new])
    entries = {entry["report_id"]: entry for entry in state["entries"]}

    assert entries["first-new"]["classification"] == "contract_mismatch"
    assert entries["first-new"]["canonical_status"] == "non_canonical"
    assert entries["repeated-new"]["classification"] == "canonical"
    assert entries["repeated-new"]["reason_codes"] == [
        "new_contract_reliable_repeat_promoted"
    ]
    assert entries["old"]["canonical_status"] == "non_canonical"
    assert state["canonical_report_id"] == "repeated-new"


def test_comparator_tolerates_one_minor_content_change_in_same_source_set() -> None:
    baseline_rows = [
        _evidence("web", "web", "owned_copy", "https://example.com", "Owned copy."),
        *[
            _evidence(
                f"exa.{index}",
                "exa",
                "external_proof",
                f"https://proof.test/{index}",
                f"External proof number {index} with enough stable descriptive content.",
            )
            for index in range(10)
        ],
    ]
    candidate_rows = deepcopy(baseline_rows)
    candidate_rows[-1]["content"] = "External proof number 9 changed its page chrome but retained the same URL."
    candidate_rows.append(
        _evidence(
            "exa.owned",
            "exa",
            "external_proof",
            "https://example.com/",
            "Duplicate domain discovery result.",
        )
    )
    baseline = _report("baseline", "2026-07-25T00:00:00Z", evidence=baseline_rows, component_score=3)
    candidate = _report("candidate", "2026-07-26T00:00:00Z", evidence=candidate_rows, component_score=0)

    comparison = compare_reports(baseline, candidate)

    assert comparison.equivalent_evidence is True
    assert comparison.classification == "evaluation_drift"
    assert comparison.delta["url_jaccard"] == 1.0


def test_missing_previous_sources_under_warning_is_acquisition_regression() -> None:
    baseline = _report(
        "baseline",
        "2026-07-26T00:00:00Z",
        evidence=[
            _evidence("web", "web", "owned_copy", "https://example.com", "Owned copy."),
            _evidence("exa.1", "exa", "external_proof", "https://proof.test/one", "Proof one."),
            _evidence("exa.2", "exa", "external_proof", "https://proof.test/two", "Proof two."),
        ],
    )
    candidate = _report(
        "candidate",
        "2026-07-27T00:00:00Z",
        evidence=[
            _evidence("web", "web", "owned_copy", "https://example.com", "Owned copy."),
            _evidence("exa.1", "exa", "external_proof", "https://proof.test/one", "Proof one."),
        ],
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison.classification == "acquisition_regression"
    assert comparison.acquisition_comparable is False
    assert comparison.delta["acquisition_unknown_urls"] == ["https://proof.test/two"]
    assert comparison.delta["verified_removed_urls"] == []


def test_snapshot_excludes_identity_quarantine_from_material_comparison() -> None:
    owned = _evidence(
        "web",
        "web",
        "owned_copy",
        "https://example.com",
        "Owned copy.",
    )
    proof = _evidence(
        "proof",
        "exa",
        "external_proof",
        "https://press.test/story",
        "Independent proof.",
    )
    baseline = _report(
        "baseline",
        "2026-07-26T00:00:00Z",
        evidence=[
            owned,
            proof,
            _evidence(
                "homonym.old",
                "exa",
                "external_proof",
                "https://other-example.test",
                "A different same-name company.",
            ),
        ],
    )
    candidate = _report(
        "candidate",
        "2026-07-27T00:00:00Z",
        evidence=[
            owned,
            proof,
            _evidence(
                "homonym.new",
                "exa",
                "external_proof",
                "https://another-example.test",
                "Another different same-name company.",
            ),
        ],
    )
    _set_identity_quarantine(baseline, "homonym.old")
    _set_identity_quarantine(candidate, "homonym.new")

    baseline_snapshot = build_evidence_snapshot(baseline)
    candidate_snapshot = build_evidence_snapshot(candidate)
    comparison = compare_reports(baseline, candidate)

    assert baseline_snapshot.external_count == 1
    assert candidate_snapshot.external_count == 1
    assert comparison.classification == "stable"
    assert comparison.acquisition_comparable is True


def test_same_domain_exa_result_is_owned_not_external_content() -> None:
    report = _report(
        "candidate",
        "2026-07-27T00:00:00Z",
        evidence=[
            _evidence(
                "web",
                "web",
                "owned_copy",
                "https://example.com",
                "Owned homepage.",
            ),
            _evidence(
                "exa.owned",
                "exa",
                "external_proof",
                "https://www.example.com/about",
                "Official about page discovered by Exa.",
            ),
            _evidence(
                "exa.press",
                "exa",
                "external_proof",
                "https://press.test/story",
                "Independent press coverage.",
            ),
        ],
    )

    snapshot = build_evidence_snapshot(report)
    public_snapshot = snapshot.to_dict()

    assert snapshot.owned_count == 2
    assert snapshot.external_count == 1
    assert snapshot.external_content_cluster_count == 1
    assert public_snapshot["schema_version"] == "evidence-comparison-v5"
    assert public_snapshot["counts"]["external_content_clusters"] == 1
    assert "independent_external_clusters" not in public_snapshot["counts"]


def test_comparator_treats_chunk_boundary_drift_as_equivalent_evidence() -> None:
    baseline = _report(
        "baseline",
        "2026-07-26T00:00:00Z",
        evidence=[
            _evidence(
                "web.1",
                "web",
                "owned_copy",
                "https://example.com/research",
                "Alpha evidence explains the durable strategic direction.",
            ),
            _evidence(
                "web.2",
                "web",
                "owned_copy",
                "https://example.com/research",
                "Strategic direction connects product proof with market impact.",
            ),
        ],
        component_score=3,
    )
    candidate = _report(
        "candidate",
        "2026-07-27T00:00:00Z",
        evidence=[
            _evidence(
                "web.9",
                "web",
                "owned_copy",
                "https://example.com/research",
                "Alpha evidence explains the durable strategic direction and connects product proof.",
            ),
            _evidence(
                "web.10",
                "web",
                "owned_copy",
                "https://example.com/research",
                "Product proof connects the strategic direction with market impact.",
            ),
        ],
        component_score=0,
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison.classification == "evaluation_drift"
    assert comparison.equivalent_evidence is True
    assert comparison.delta["unchanged_record_count"] == 0
    assert comparison.delta["modified_record_count"] == 1
    assert comparison.delta["semantic_locator_ratio"] == 1.0
    assert comparison.delta["semantic_token_jaccard"] >= 0.9


def test_lost_same_domain_exa_result_is_owned_evidence_loss() -> None:
    shared = _evidence(
        "web",
        "web",
        "owned_copy",
        "https://example.com",
        "Owned homepage.",
    )
    baseline = _report(
        "baseline",
        "2026-07-26T00:00:00Z",
        evidence=[
            shared,
            _evidence(
                "exa.owned",
                "exa",
                "external_proof",
                "https://example.com/features/product",
                "Official product page discovered by Exa.",
            ),
        ],
    )
    candidate = _report(
        "candidate",
        "2026-07-27T00:00:00Z",
        evidence=[shared],
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison.classification == "acquisition_regression"
    assert "owned_evidence_lost" in comparison.reason_codes
    assert "external_evidence_lost" not in comparison.reason_codes
    assert "external_content_coverage_lost" not in comparison.reason_codes


def test_history_keeps_first_shadow_scan_provisional_and_quarantines_drift() -> None:
    first = _report(
        "first",
        "2026-07-24T00:00:00Z",
        score=56,
        evidence=[_evidence("web", "web", "owned_copy", "https://example.com", "Owned copy.")],
    )
    richer = _report(
        "richer",
        "2026-07-25T00:00:00Z",
        score=47,
        evidence=[
            _evidence("web", "web", "owned_copy", "https://example.com", "Owned copy."),
            _evidence("exa", "exa", "external_proof", "https://proof.test/one", "Proof."),
        ],
        component_score=3,
    )
    drifted = _report(
        "drifted",
        "2026-07-26T00:00:00Z",
        score=40,
        evidence=[
            _evidence("exa.changed-ref", "exa", "external_proof", "https://proof.test/one", "Proof."),
            _evidence("web.changed-ref", "web", "owned_copy", "https://example.com", "Owned copy."),
        ],
        component_score=0,
    )

    state = classify_report_history([drifted, first, richer])
    entries = {entry["report_id"]: entry for entry in state["entries"]}

    assert state["canonical_report_id"] is None
    assert state["provisional_report_id"] == "first"
    assert state["selected_report_id"] == "first"
    assert entries["first"]["canonical_status"] == "provisional"
    assert entries["richer"]["classification"] == "candidate"
    assert entries["drifted"]["classification"] == "evaluation_drift"


def test_repeated_enforcement_selects_provisional_while_observe_keeps_latest() -> None:
    reports = [
        _report("new", "2026-07-26T00:00:00Z", score=40),
        _report("old", "2026-07-25T00:00:00Z", score=56),
    ]

    observed, _, _ = selected_report_for_display(reports, mode="observe")
    enforced, annotated, _ = selected_report_for_display(reports, mode="repeated")

    assert observed["id"] == "new"
    assert enforced["id"] == "old"
    assert {row["canonical_status"] for row in annotated} == {"provisional", "non_canonical"}


def test_global_dry_run_is_read_only_and_counts_repeated_histories() -> None:
    report = _report("only", "2026-07-25T00:00:00Z")
    result = render_history_dry_run(
        {
            "one.test": [report],
            "two.test": [
                {**deepcopy(report), "id": "older", "created_at": "2026-07-24T00:00:00Z"},
                {**deepcopy(report), "id": "newer", "created_at": "2026-07-25T00:00:00Z"},
            ],
        }
    )

    assert result["mutates_state"] is False
    assert result["brand_count"] == 2
    assert result["repeated_brand_count"] == 1
    assert result["single_scan_brand_count"] == 1
    assert result["repeated_mode_changed_brand_count"] == 1
    repeated = next(brand for brand in result["brands"] if brand["domain"] == "two.test")
    assert repeated["latest_report_id"] == "newer"
    assert repeated["selected_report_id"] == "older"
    assert repeated["selection_changes_visible_report"] is True
    assert repeated["score_delta_latest_minus_selected"] == 0


def test_annotated_history_is_returned_newest_first() -> None:
    older = _report("older", "2026-07-24T00:00:00Z")
    newer = _report("newer", "2026-07-25T00:00:00Z")

    annotated, state = annotate_report_history([older, newer])

    assert [row["id"] for row in annotated] == ["newer", "older"]
    assert state["selected_report_id"] == "older"
    assert annotated[1]["canonical_status"] == "provisional"


def _evidence(
    ref: str,
    source: str,
    source_class: str,
    url: str,
    content: str,
) -> dict:
    return {
        "ref": ref,
        "source": source,
        "evidence_type": "raw_input" if source == "web" else "external_proof.external_mentions",
        "content": content,
        "url": url,
        "confidence": "medium",
        "metadata": {"source_class": source_class},
    }


def _set_identity_quarantine(report: dict, *refs: str) -> None:
    report["raw"]["flow"]["interpretation_debug"] = {
        "block_evidence_identity_gate": {
            "version": "sv9-flow-block-evidence-identity-gate-v1",
            "records": [
                {
                    "ref": ref,
                    "reason_codes": ["semantic_identity_mismatch"],
                }
                for ref in refs
            ],
        }
    }


def _report(
    report_id: str,
    created_at: str,
    *,
    score: int = 50,
    evidence: list[dict] | None = None,
    component_score: int = 3,
    component_status: str = "scored",
    tile_states: dict[str, str] | None = None,
) -> dict:
    rows = deepcopy(evidence) if evidence is not None else [
        _evidence("web", "web", "owned_copy", "https://example.com", "Owned copy.")
    ]
    refs = [{"ref": row["ref"], "url": row["url"]} for row in rows]
    tiles = [
        {"id": tile_id, "estado": state}
        for tile_id, state in (tile_states or {"P4": "ok"}).items()
    ]
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "score": score,
        "reliability_status": "shadow",
        "acquisition_gate": {
            "state": "warning",
            "warnings": [{"code": "exa_partial"}],
        },
        "blocks": [
            {
                "name": "value_proposition",
                "detected": component_status == "scored",
                "content": "A value proposition." if component_status == "scored" else "",
                "coverage_status": "positive_evidence",
                "refs": refs,
            }
        ],
        "components": [
            {
                "key": "value_proposition",
                "status": component_status,
                "score": component_score,
                "tile_profile": tiles,
                "block": {
                    "coverage_status": "positive_evidence",
                    "refs": refs,
                },
            }
        ],
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": rows,
                    }
                }
            }
        },
    }
