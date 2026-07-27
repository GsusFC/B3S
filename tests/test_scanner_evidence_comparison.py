from __future__ import annotations

from copy import deepcopy

from src.services.scanner_evidence_comparison import (
    annotate_report_history,
    build_evidence_snapshot,
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
