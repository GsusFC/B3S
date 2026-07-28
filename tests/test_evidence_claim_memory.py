from __future__ import annotations

from copy import deepcopy
import json

from src.services.evidence_claim_memory import build_evidence_claim_memory


def test_explicit_slot_separates_slot_variant_and_occurrence_identity() -> None:
    claim = _owned(
        "Our mission is to simplify finance.",
        claim_slot_key="mission.primary",
        claim_type="mission",
    )

    result = build_evidence_claim_memory(
        [
            _report("one", "2026-01-01T00:00:00Z", [claim]),
            _report("two", "2026-01-02T00:00:00Z", [deepcopy(claim)]),
        ]
    )

    assert result["schema_version"] == "evidence-claim-memory-v1"
    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"]["claim_slot_count"] == 1
    assert result["summary"]["claim_variant_count"] == 1
    assert result["summary"]["claim_occurrence_count"] == 2
    slot = result["slots"][0]
    variant = result["variants"][0]
    occurrences = result["occurrences"]
    assert slot["claim_slot_id"] == variant["claim_slot_id"]
    assert {row["claim_slot_id"] for row in occurrences} == {
        slot["claim_slot_id"]
    }
    assert {row["claim_variant_id"] for row in occurrences} == {
        variant["claim_variant_id"]
    }
    assert len({row["claim_occurrence_id"] for row in occurrences}) == 2
    assert variant["state"] == "repeated"
    assert "Our mission is to simplify finance." not in json.dumps(result)


def test_bare_content_derived_claim_id_is_not_treated_as_stable_slot() -> None:
    result = build_evidence_claim_memory(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [_owned("A generated claim.", claim_id="content-derived-id")],
            )
        ]
    )

    assert result["slots"] == []
    assert result["summary"]["ignored_bare_claim_id_count"] == 1
    assert result["summary"]["ignored_claim_reason_counts"] == {
        "bare_claim_id_without_stable_semantics": 1
    }


def test_legacy_claim_id_requires_declared_stable_slot_semantics() -> None:
    result = build_evidence_claim_memory(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [
                    _owned(
                        "A declared legacy slot.",
                        claim_id="mission-primary",
                        claim_id_semantics="stable_slot",
                    )
                ],
            )
        ]
    )

    assert result["summary"]["claim_slot_count"] == 1
    assert result["summary"]["claim_slot_method_counts"] == {
        "declared_stable_claim_id": 1
    }


def test_invalid_explicit_slot_is_reported_instead_of_silently_coerced() -> None:
    result = build_evidence_claim_memory(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [_owned("A claim.", claim_slot_key="mission / primary")],
            )
        ]
    )

    assert result["slots"] == []
    assert result["summary"]["ignored_claim_reason_counts"] == {
        "invalid_explicit_claim_slot_key": 1
    }


def test_sequential_variants_only_propose_replacement() -> None:
    old = _owned(
        "We serve finance teams.",
        claim_slot_key="audience.primary",
        claim_type="audience",
    )
    new = _owned(
        "We serve operations teams.",
        claim_slot_key="audience.primary",
        claim_type="audience",
    )

    result = build_evidence_claim_memory(
        [
            _report("old", "2026-01-01T00:00:00Z", [old]),
            _report("new", "2026-01-02T00:00:00Z", [new]),
        ]
    )

    assert result["summary"]["claim_variant_count"] == 2
    assert result["summary"]["relation_candidate_counts"] == {
        "replacement_candidate": 1
    }
    relation = result["slots"][0]["relation_candidates"][0]
    historical = next(
        variant for variant in result["variants"] if not variant["present_in_latest"]
    )
    current = next(
        variant for variant in result["variants"] if variant["present_in_latest"]
    )
    assert historical["state"] == "not_reacquired"
    assert current["state"] == "observed"
    assert relation["from_claim_variant_id"] == historical["claim_variant_id"]
    assert relation["to_claim_variant_id"] == current["claim_variant_id"]
    assert relation["adjudication_state"] == "proposed"
    assert relation["runtime_effect"] is False
    assert relation["authority"] is False


def test_simultaneous_variants_propose_coexistence_not_replacement() -> None:
    first = _owned(
        "We serve finance teams.",
        claim_slot_key="audience.primary",
    )
    second = _owned(
        "We serve operations teams.",
        claim_slot_key="audience.primary",
    )

    result = build_evidence_claim_memory(
        [_report("one", "2026-01-01T00:00:00Z", [first, second])]
    )

    assert result["slots"][0]["latest_variant_count"] == 2
    assert result["summary"]["relation_candidate_counts"] == {
        "coexistence_candidate": 1
    }
    assert {
        relation["relation"]
        for relation in result["slots"][0]["relation_candidates"]
    } == {"coexistence_candidate"}


def test_claim_type_drift_is_visible_and_requires_review() -> None:
    old = _owned(
        "We serve finance teams.",
        claim_slot_key="audience.primary",
        claim_type="audience",
    )
    drifted = _owned(
        "We serve finance teams.",
        claim_slot_key="audience.primary",
        claim_type="mission",
    )

    result = build_evidence_claim_memory(
        [
            _report("old", "2026-01-01T00:00:00Z", [old]),
            _report("new", "2026-01-02T00:00:00Z", [drifted]),
        ]
    )

    assert result["summary"]["claim_variant_count"] == 1
    assert result["summary"]["claim_type_conflict_count"] == 1
    assert result["slots"][0]["claim_types"] == ["audience", "mission"]
    assert result["slots"][0]["claim_type_conflict"] is True
    assert result["slots"][0]["requires_human_review"] is True
    assert result["slots"][0]["relation_candidates"] == []


def test_stable_slot_and_variant_survive_url_move() -> None:
    old = _owned(
        "We serve finance teams.",
        url="https://example.com/about",
        claim_slot_key="audience.primary",
    )
    moved = _owned(
        "We serve finance teams.",
        url="https://example.com/customers",
        claim_slot_key="audience.primary",
    )

    result = build_evidence_claim_memory(
        [
            _report("old", "2026-01-01T00:00:00Z", [old]),
            _report("new", "2026-01-02T00:00:00Z", [moved]),
        ]
    )

    assert result["summary"]["claim_slot_count"] == 1
    assert result["summary"]["claim_variant_count"] == 1
    assert result["summary"]["claim_occurrence_count"] == 2
    assert result["variants"][0]["observation_count"] == 2
    assert len(result["variants"][0]["evidence_ids"]) == 2


def test_report_and_row_order_do_not_change_projection() -> None:
    first = _owned(
        "We serve finance teams.",
        claim_slot_key="audience.primary",
    )
    second = _owned(
        "We simplify monthly reporting.",
        claim_slot_key="promise.primary",
    )
    reports = [
        _report("one", "2026-01-01T00:00:00Z", [first, second]),
        _report(
            "two",
            "2026-01-02T00:00:00Z",
            [deepcopy(first), deepcopy(second)],
        ),
    ]
    reordered = list(reversed(deepcopy(reports)))
    for report in reordered:
        report["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"].reverse()

    baseline = build_evidence_claim_memory(reports)
    changed_order = build_evidence_claim_memory(reordered)

    assert changed_order["state_fingerprint"] == baseline["state_fingerprint"]
    assert changed_order == baseline


def test_visual_and_structural_metadata_are_not_semantic_slots() -> None:
    visual = _owned("A visual observation.", tile="mission.M1")
    structural = _owned("A block observation.", checked_block="mission")

    result = build_evidence_claim_memory(
        [_report("one", "2026-01-01T00:00:00Z", [visual, structural])]
    )

    assert result["slots"] == []
    assert result["summary"]["ignored_claim_reason_counts"] == {
        "checked_block_is_not_semantic_claim_slot": 1,
        "visual_tile_is_not_semantic_claim_slot": 1,
    }


def test_duplicate_reference_is_one_occurrence_with_a_ref_count() -> None:
    claim = _owned(
        "We serve finance teams.",
        claim_slot_key="audience.primary",
    )

    result = build_evidence_claim_memory(
        [_report("one", "2026-01-01T00:00:00Z", [claim, deepcopy(claim)])]
    )

    assert result["summary"]["claim_occurrence_count"] == 1
    assert result["occurrences"][0]["duplicate_ref_count"] == 2
    assert result["variants"][0]["observation_count"] == 1


def test_evidence_identity_adjudication_is_provenance_not_claim_authority() -> None:
    report = _report(
        "one",
        "2026-01-01T00:00:00Z",
        [_owned("A claim.", claim_slot_key="mission.primary")],
    )
    initial = build_evidence_claim_memory([report])
    evidence_id = initial["occurrences"][0]["evidence_id"]

    result = build_evidence_claim_memory(
        [report],
        evidence_adjudications=[
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "subject_type": "evidence",
                "subject_id": evidence_id,
                "sequence": 1,
                "decision": "accepted",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ],
    )

    assert result["variants"][0]["adjudication_states"] == ["accepted"]
    assert result["variants"][0]["runtime_effect"] is False
    assert result["variants"][0]["authority"] is False
    assert result["slots"][0]["requires_human_review"] is False
    assert result["authority"] is False


def test_disabled_and_empty_modes_return_a_deterministic_empty_projection() -> None:
    disabled = build_evidence_claim_memory(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [_owned("A claim.", claim_slot_key="mission.primary")],
            )
        ],
        mode="off",
    )
    empty = build_evidence_claim_memory([])

    assert disabled["mode"] == "disabled"
    assert disabled["summary"]["claim_slot_count"] == 0
    assert disabled["slots"] == []
    assert len(disabled["state_fingerprint"]) == 64
    assert empty["mode"] == "shadow"
    assert empty["summary"]["claim_slot_count"] == 0
    assert len(empty["state_fingerprint"]) == 64


def _owned(
    content: str,
    *,
    url: str = "https://example.com/about",
    **metadata: str,
) -> dict:
    return {
        "ref": "web.0",
        "source": "web",
        "evidence_type": "raw_input",
        "url": url,
        "content": content,
        "metadata": {
            "source_class": "owned_copy",
            **metadata,
        },
    }


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
