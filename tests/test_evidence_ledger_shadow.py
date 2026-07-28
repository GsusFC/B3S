from __future__ import annotations

from copy import deepcopy

from src.services.evidence_ledger_shadow import build_evidence_ledger_shadow
from src.services.scanner_evidence_comparison import (
    build_evidence_snapshot,
    classify_report_history,
)


def test_ledger_is_disabled_by_default_and_has_no_runtime_effect(monkeypatch) -> None:
    monkeypatch.delenv("B3S_EVIDENCE_LEDGER_MODE", raising=False)

    result = build_evidence_ledger_shadow([_report("one", "2026-01-01T00:00:00Z")])

    assert result["mode"] == "disabled"
    assert result["runtime_effect"] is False
    assert result["entries"] == []


def test_repeated_owned_and_identity_matched_external_evidence_become_candidates() -> None:
    rows = [
        _evidence(
            "web.0",
            "web",
            "owned_copy",
            "raw_input",
            "https://example.com",
            "A stable owned claim.",
        ),
        _evidence(
            "exa.0",
            "exa",
            "external_proof",
            "external_proof.external_mentions",
            "https://proof.test/story",
            "Independent evidence about Example.",
            identity_match="brand_name",
        ),
    ]
    first = _report("one", "2026-01-01T00:00:00Z", evidence=rows)
    second = _report(
        "two",
        "2026-01-02T00:00:00Z",
        evidence=[
            {**deepcopy(rows[1]), "ref": "raw_inputs.8.exa.mentions.4"},
            {**deepcopy(rows[0]), "ref": "raw_inputs.9"},
        ],
    )

    result = build_evidence_ledger_shadow([second, first], mode="shadow")

    assert result["runtime_effect"] is False
    assert result["summary"]["entry_count"] == 2
    assert result["summary"]["state_counts"] == {"validation_candidate": 2}
    assert result["summary"]["repeat_rate"] == 1.0
    assert all(entry["observation_count"] == 2 for entry in result["entries"])
    assert all(entry["state"] == "validation_candidate" for entry in result["entries"])


def test_external_identity_mismatch_can_repeat_but_never_becomes_validation_candidate() -> None:
    row = _evidence(
        "exa.0",
        "exa",
        "external_proof",
        "external_proof.external_mentions",
        "https://proof.test/story",
        "A different company named Example.",
        identity_match="none",
    )

    result = build_evidence_ledger_shadow(
        [
            _report("one", "2026-01-01T00:00:00Z", evidence=[row]),
            _report(
                "two",
                "2026-01-02T00:00:00Z",
                evidence=[{**deepcopy(row), "ref": "exa.changed"}],
            ),
        ],
        mode="shadow",
    )

    assert result["entries"][0]["state"] == "repeated"
    assert result["entries"][0]["qualified_observation_count"] == 0
    assert "validation_eligibility_incomplete" in result["entries"][0]["reason_codes"]


def test_llm_identity_label_drift_does_not_change_evidence_identity_or_validate_it() -> None:
    row = _evidence(
        "exa.0",
        "exa",
        "external_proof",
        "external_proof.external_mentions",
        "https://proof.test/story",
        "Evidence that may refer to Example.",
        identity_match="brand_name",
    )
    relabeled = deepcopy(row)
    relabeled["metadata"]["identity_match_llm"] = "none"
    first = _report("one", "2026-01-01T00:00:00Z", evidence=[row])
    second = _report("two", "2026-01-02T00:00:00Z", evidence=[relabeled])

    result = build_evidence_ledger_shadow([first, second], mode="shadow")

    assert build_evidence_snapshot(first).fingerprint == build_evidence_snapshot(second).fingerprint
    assert result["summary"]["entry_count"] == 1
    assert result["entries"][0]["observation_count"] == 2
    assert result["entries"][0]["qualified_observation_count"] == 1
    assert result["entries"][0]["state"] == "repeated"


def test_changed_content_is_a_change_candidate_not_an_automatic_contradiction() -> None:
    old = _evidence(
        "web.0",
        "web",
        "owned_copy",
        "raw_input",
        "https://example.com/about",
        "We serve finance teams.",
    )
    changed = {**deepcopy(old), "ref": "web.9", "content": "We serve operations teams."}

    result = build_evidence_ledger_shadow(
        [
            _report("old", "2026-01-01T00:00:00Z", evidence=[old]),
            _report("new", "2026-01-02T00:00:00Z", evidence=[changed]),
        ],
        mode="shadow",
    )
    entries = {entry["content_hash"]: entry for entry in result["entries"]}
    old_entry = next(entry for entry in entries.values() if entry["present_in_latest"] is False)
    new_entry = next(entry for entry in entries.values() if entry["present_in_latest"] is True)

    assert old_entry["state"] == "changed_candidate"
    assert "semantic_contradiction_not_assumed" in old_entry["reason_codes"]
    assert new_entry["state"] == "observed"
    assert old_entry["locator_variant_count"] == 2
    assert "semantic_contradiction_detection_not_implemented" in result["warnings"]


def test_one_missing_scan_does_not_retire_evidence_and_ttl_only_proposes_staleness() -> None:
    external = _evidence(
        "exa.0",
        "exa",
        "external_proof",
        "external_proof.external_mentions",
        "https://proof.test/story",
        "Independent evidence.",
        identity_match="domain",
    )
    replacement = _evidence(
        "web.0",
        "web",
        "owned_copy",
        "raw_input",
        "https://example.com",
        "Current owned evidence.",
    )

    recent = build_evidence_ledger_shadow(
        [
            _report("old", "2026-01-01T00:00:00Z", evidence=[external]),
            _report("new", "2026-01-02T00:00:00Z", evidence=[replacement]),
        ],
        mode="shadow",
    )
    aged = build_evidence_ledger_shadow(
        [
            _report("old", "2026-01-01T00:00:00Z", evidence=[external]),
            _report("new", "2026-04-15T00:00:00Z", evidence=[replacement]),
        ],
        mode="shadow",
    )

    recent_external = next(
        entry for entry in recent["entries"] if entry["source_class"] == "external_proof"
    )
    aged_external = next(
        entry for entry in aged["entries"] if entry["source_class"] == "external_proof"
    )
    assert recent_external["state"] == "not_reacquired"
    assert "single_miss_never_retires_evidence" in recent_external["reason_codes"]
    assert aged_external["state"] == "stale_candidate"
    assert "automatic_retirement_disabled" in aged_external["reason_codes"]


def test_building_shadow_ledger_does_not_change_canonical_selection() -> None:
    first = _report("one", "2026-01-01T00:00:00Z", score=56)
    second = _report("two", "2026-01-02T00:00:00Z", score=35)
    before = classify_report_history([first, second])

    ledger = build_evidence_ledger_shadow([first, second], mode="shadow")
    after = classify_report_history([first, second])

    assert ledger["runtime_effect"] is False
    assert before == after
    assert after["selected_report_id"] == "one"


def _evidence(
    ref: str,
    source: str,
    source_class: str,
    evidence_type: str,
    url: str,
    content: str,
    *,
    identity_match: str = "",
) -> dict:
    metadata = {"source_class": source_class}
    if identity_match:
        metadata["identity_match"] = identity_match
    return {
        "ref": ref,
        "source": source,
        "evidence_type": evidence_type,
        "content": content,
        "url": url,
        "confidence": "medium",
        "metadata": metadata,
    }


def _report(
    report_id: str,
    created_at: str,
    *,
    score: int = 50,
    evidence: list[dict] | None = None,
) -> dict:
    rows = deepcopy(evidence) if evidence is not None else [
        _evidence(
            "web.0",
            "web",
            "owned_copy",
            "raw_input",
            "https://example.com",
            "A stable owned claim.",
        )
    ]
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "score": score,
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
                        "evidence": rows,
                    }
                }
            }
        },
    }
