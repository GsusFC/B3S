from __future__ import annotations

from copy import deepcopy

import pytest

from src.services.evidence_vault_incremental_refresh import (
    EvidenceVaultOperationPlanError,
    build_incremental_evidence_delta,
    build_vault_scan_plan,
    resolve_vault_scan_mode,
    validate_vault_scan_plan,
)
from src.services.scanner_evidence_comparison import canonical_evidence_rows


MEMORY_VERSION = "a" * 64
SUBJECT_URL = "https://example.com"


def test_mode_resolution_leaves_existing_scanner_untouched_until_vault_enabled() -> None:
    result = resolve_vault_scan_mode(
        environment="production",
        incremental_enabled=True,
        has_canonical_memory=True,
    )

    assert result == {
        "execution_path": "existing_scanner",
        "mode": None,
        "vault_only": False,
        "reason": "vault_incremental_not_enabled",
    }
    with pytest.raises(EvidenceVaultOperationPlanError, match="enabled vault"):
        resolve_vault_scan_mode(
            environment="production",
            incremental_enabled=True,
            has_canonical_memory=True,
            requested_mode="incremental_refresh",
        )


def test_mode_resolution_uses_baseline_then_incremental_and_explicit_diagnostic() -> None:
    baseline = resolve_vault_scan_mode(
        environment="vault",
        incremental_enabled=True,
        has_canonical_memory=False,
    )
    incremental = resolve_vault_scan_mode(
        environment="vault",
        incremental_enabled=True,
        has_canonical_memory=True,
    )
    diagnostic = resolve_vault_scan_mode(
        environment="vault",
        incremental_enabled=True,
        has_canonical_memory=True,
        requested_mode="diagnostic_full",
    )

    assert baseline["mode"] == "baseline"
    assert incremental["mode"] == "incremental_refresh"
    assert diagnostic["mode"] == "diagnostic_full"


def test_identical_incremental_refresh_requires_zero_llm_and_no_report() -> None:
    rows = [_row("home", "Stable evidence")]

    plan = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="incremental_refresh",
        current_evidence_records=rows,
        previous_capture_evidence_records=rows,
        known_evidence_records=rows,
        semantic_analysis_claimed_fingerprints=_fingerprints(rows),
        canonical_memory_version=MEMORY_VERSION,
    )

    assert plan["canonical_impact"] == "none"
    assert plan["delta"]["summary"]["unchanged_count"] == 1
    assert plan["operations"] == {
        "persist_capture_only": True,
        "classify_evidence_fingerprints": [],
        "propose_tile_relations_for_fingerprints": [],
        "reevaluate_tile_ids": [],
        "llm_required": False,
        "recalculate_canonical_score": False,
        "create_candidate_packet": False,
        "create_canonical_report": False,
        "create_diagnostic_report": False,
    }
    assert plan["exit_contract"]["canonical_memory_changes"] is False


def test_unlit_accepted_tiles_reanalyze_current_evidence() -> None:
    rows = [_row("home", "Stable evidence")]
    accepted_tiles = [
        {"tile_id": "M1", "basis": []},
        {
            "tile_id": "P1",
            "basis": [
                {
                    "relation_id": "r1",
                    "evidence_id": "e1",
                    "source_identity_id": "s1",
                    "polarity": "supports",
                }
            ],
        },
    ]

    plan = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="incremental_refresh",
        current_evidence_records=rows,
        previous_capture_evidence_records=rows,
        known_evidence_records=rows,
        semantic_analysis_claimed_fingerprints=_fingerprints(rows),
        accepted_tiles=accepted_tiles,
        canonical_memory_version=MEMORY_VERSION,
    )

    assert plan["operations"]["llm_required"] is True
    assert plan["operations"]["create_candidate_packet"] is True
    assert "M1" in plan["operations"]["reevaluate_tile_ids"]
    assert "P1" not in plan["operations"]["reevaluate_tile_ids"]
    assert plan["operations"]["classify_evidence_fingerprints"]


def test_unchanged_but_unanalysed_capture_still_requires_analysis() -> None:
    rows = [_row("home", "Persisted before analysis completed")]

    plan = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="incremental_refresh",
        current_evidence_records=rows,
        previous_capture_evidence_records=rows,
        known_evidence_records=[],
        canonical_memory_version=MEMORY_VERSION,
    )

    assert plan["delta"]["summary"]["unanalysed_unchanged_count"] == 1
    assert plan["operations"]["llm_required"] is True
    assert len(plan["operations"]["classify_evidence_fingerprints"]) == 1



def test_not_reacquired_is_coverage_loss_not_canonical_change() -> None:
    previous = [_row("home", "Still canonical")]

    plan = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="incremental_refresh",
        current_evidence_records=[],
        previous_capture_evidence_records=previous,
        known_evidence_records=previous,
        canonical_memory_version=MEMORY_VERSION,
    )

    assert plan["delta"]["coverage_loss_only"] is True
    assert plan["delta"]["summary"]["not_reacquired_count"] == 1
    assert plan["canonical_impact"] == "none"
    assert plan["operations"]["llm_required"] is False
    assert plan["operations"]["recalculate_canonical_score"] is False


def test_modified_known_locator_analyses_only_delta_and_related_tile() -> None:
    previous = [_row("home", "Old proposition")]
    current = [_row("home", "New proposition")]
    old_record = canonical_evidence_rows(previous, subject_url=SUBJECT_URL)[0]

    plan = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="incremental_refresh",
        current_evidence_records=current,
        previous_capture_evidence_records=previous,
        known_evidence_records=previous,
        accepted_evidence_tile_relations=[
            {
                "evidence_fingerprint": old_record.fingerprint,
                "tile_id": "P1",
            }
        ],
        canonical_memory_version=MEMORY_VERSION,
    )

    current_record = canonical_evidence_rows(current, subject_url=SUBJECT_URL)[0]
    assert plan["canonical_impact"] == "review_required"
    assert plan["delta"]["modified_evidence_fingerprints"] == [
        current_record.fingerprint
    ]
    assert plan["delta"]["superseded_evidence_fingerprints"] == [
        old_record.fingerprint
    ]
    assert plan["delta"]["not_reacquired_evidence_fingerprints"] == []
    assert plan["operations"]["classify_evidence_fingerprints"] == [
        current_record.fingerprint
    ]
    assert plan["operations"]["reevaluate_tile_ids"] == [
        "P1"
    ]
    assert plan["operations"]["create_canonical_report"] is False
    assert plan["operations"]["recalculate_canonical_score"] is False


def test_modified_multi_chunk_locator_only_supersedes_missing_fingerprint() -> None:
    shared_url = "https://example.com/home"
    previous = [
        {
            **_row("home-a", "Chunk A"),
            "url": shared_url,
        },
        {
            **_row("home-b", "Chunk B"),
            "url": shared_url,
        },
    ]
    current = [
        previous[0],
        {
            **_row("home-c", "Chunk C"),
            "url": shared_url,
        },
    ]
    previous_records = canonical_evidence_rows(
        previous,
        subject_url=SUBJECT_URL,
    )
    current_records = canonical_evidence_rows(
        current,
        subject_url=SUBJECT_URL,
    )
    fingerprint_a = next(
        row.fingerprint
        for row in previous_records
        if row.normalized_content == "chunk a"
    )
    fingerprint_b = next(
        row.fingerprint
        for row in previous_records
        if row.normalized_content == "chunk b"
    )
    fingerprint_c = next(
        row.fingerprint
        for row in current_records
        if row.normalized_content == "chunk c"
    )

    delta = build_incremental_evidence_delta(
        subject_url=SUBJECT_URL,
        current_evidence_records=current,
        previous_capture_evidence_records=previous,
        known_evidence_records=previous,
        semantic_analysis_claimed_fingerprints=_fingerprints(previous),
    )

    assert fingerprint_a in delta["unchanged_evidence_fingerprints"]
    assert delta["modified_evidence_fingerprints"] == [fingerprint_c]
    assert delta["superseded_evidence_fingerprints"] == [fingerprint_b]
    assert fingerprint_a not in delta["superseded_evidence_fingerprints"]



def test_changed_reacquisition_supersedes_accepted_historical_member() -> None:
    historical = [_row("home", "Accepted historical C7 member")]
    changed = [_row("home", "Materially changed C7 member")]
    historical_record = canonical_evidence_rows(
        historical, subject_url=SUBJECT_URL
    )[0]
    changed_record = canonical_evidence_rows(changed, subject_url=SUBJECT_URL)[0]

    delta = build_incremental_evidence_delta(
        subject_url=SUBJECT_URL,
        current_evidence_records=changed,
        previous_capture_evidence_records=[],
        known_evidence_records=historical,
        accepted_evidence_tile_relations=[
            {
                "evidence_fingerprint": historical_record.fingerprint,
                "tile_id": "C7",
            }
        ],
    )

    assert delta["modified_evidence_fingerprints"] == [changed_record.fingerprint]
    assert delta["superseded_evidence_fingerprints"] == [
        historical_record.fingerprint
    ]
    assert delta["affected_tile_ids"] == ["C7"]



def test_reacquired_historical_evidence_needs_no_llm() -> None:
    historical = [_row("proof", "Known proof")]

    delta = build_incremental_evidence_delta(
        subject_url=SUBJECT_URL,
        current_evidence_records=historical,
        previous_capture_evidence_records=[],
        known_evidence_records=historical,
        semantic_analysis_claimed_fingerprints=_fingerprints(historical),
    )

    assert delta["summary"]["reacquired_count"] == 1
    assert delta["requires_incremental_analysis"] is False


def test_new_evidence_is_classified_without_reopening_all_tiles() -> None:
    previous = [_row("home", "Stable")]
    current = [*previous, _row("news", "A new launch")]

    plan = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="incremental_refresh",
        current_evidence_records=current,
        previous_capture_evidence_records=previous,
        known_evidence_records=previous,
        semantic_analysis_claimed_fingerprints=_fingerprints(previous),
        canonical_memory_version=MEMORY_VERSION,
    )

    assert plan["delta"]["summary"]["added_count"] == 1
    assert len(plan["operations"]["classify_evidence_fingerprints"]) == 1
    assert plan["operations"]["reevaluate_tile_ids"] == []
    assert plan["operations"]["llm_required"] is True


def test_baseline_and_diagnostic_full_have_distinct_authority_contracts() -> None:
    rows = [_row("home", "Initial evidence")]

    baseline = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="baseline",
        current_evidence_records=rows,
    )
    diagnostic = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="diagnostic_full",
        current_evidence_records=rows,
        canonical_memory_version=MEMORY_VERSION,
    )

    assert len(baseline["operations"]["reevaluate_tile_ids"]) == 80
    assert baseline["operations"]["create_candidate_packet"] is True
    assert baseline["operations"]["create_canonical_report"] is False
    assert diagnostic["canonical_impact"] == "diagnostic_only"
    assert diagnostic["operations"]["create_diagnostic_report"] is True
    assert diagnostic["operations"]["create_candidate_packet"] is False
    assert diagnostic["authority"] is False


def test_operation_plan_validator_rejects_tampered_delta() -> None:
    plan = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="baseline",
        current_evidence_records=[_row("home", "Initial evidence")],
    )
    tampered = deepcopy(plan)
    tampered["delta"]["evidence_count"] = 999

    with pytest.raises(EvidenceVaultOperationPlanError, match="fingerprint"):
        validate_vault_scan_plan(tampered)



def test_operation_plan_is_reproducible_and_order_insensitive() -> None:
    first = [_row("home", "One"), _row("about", "Two")]

    left = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="baseline",
        current_evidence_records=first,
    )
    right = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="baseline",
        current_evidence_records=list(reversed(first)),
    )

    assert left == right


def test_exact_duplicate_representative_is_order_insensitive() -> None:
    left_row = _row("home", "Same canonical evidence")
    left_row["source"] = "z-provider"
    right_row = _row("home", "Same canonical evidence")
    right_row["source"] = "a-provider"

    first = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="baseline",
        current_evidence_records=[left_row, right_row],
    )
    second = build_vault_scan_plan(
        brand_identity="Example",
        subject_url=SUBJECT_URL,
        mode="baseline",
        current_evidence_records=[right_row, left_row],
    )

    assert first == second



def _fingerprints(rows: list[dict[str, object]]) -> list[str]:
    return [
        record.fingerprint
        for record in canonical_evidence_rows(rows, subject_url=SUBJECT_URL)
    ]


def _row(slug: str, content: str) -> dict:
    return {
        "ref": f"web.{slug}",
        "source": "web",
        "evidence_type": "owned_copy",
        "url": f"https://example.com/{slug}",
        "content": content,
        "metadata": {"source_class": "owned_copy"},
    }


def test_operation_plan_freezes_full_capture_semantic_context() -> None:
    rows = [
        _row("selected", "Selected delta"),
        _row("identity-context", "Stable owned identity context"),
    ]

    plan = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url=SUBJECT_URL,
        mode="baseline",
        current_evidence_records=rows,
    )

    expected = sorted(
        row.fingerprint
        for row in canonical_evidence_rows(rows, subject_url=SUBJECT_URL)
    )
    assert plan["semantic_context"]["scope"] == (
        "exact_capture_owned_identity_context"
    )
    assert plan["semantic_context"]["evidence_fingerprints"] == expected
    validate_vault_scan_plan(plan)
