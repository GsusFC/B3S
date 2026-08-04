from __future__ import annotations

from copy import deepcopy
import math

import pytest

from src.services.evidence_vault_canonical_core import (
    AuthorityState,
    EvidenceVaultCanonicalCoreError,
    TileState,
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_reducer_policy,
    build_tile_contract_registry,
    canonical_fingerprint,
    canonical_json,
    reduce_tile_basis,
    reducer_policy_fingerprint,
    tile_contract_registry_fingerprint,
    validate_candidate_packet,
    validate_incremental_candidate_tiles,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def test_canonical_json_is_labeled_deterministic_and_fail_closed() -> None:
    first = {"z": 1, "a": {"two": 2, "one": [True, None, -0.0]}}
    reordered = {"a": {"one": [True, None, 0.0], "two": 2}, "z": 1}

    assert canonical_json(first) == canonical_json(reordered)
    assert canonical_json(first) == ('{"a":{"one":[true,null,0.0],"two":2},"z":1}')
    assert canonical_fingerprint("one", {"a": "bc"}) != (canonical_fingerprint("one", {"ab": "c"}))
    assert canonical_fingerprint("one", {"a": "bc"}) != (canonical_fingerprint("two", {"a": "bc"}))

    for invalid in (
        {"bad": math.nan},
        {"bad": math.inf},
        {1: "non-string-key"},
        {"bad": ("tuple",)},
        {"bad": {"set"}},
    ):
        with pytest.raises(EvidenceVaultCanonicalCoreError):
            canonical_json(invalid)


def test_tile_registry_and_reducer_policy_have_stable_content_identities() -> None:
    registry = build_tile_contract_registry()
    policy = build_reducer_policy()

    assert registry["rubric_version"] == "baldosas-v3-1"
    assert registry["tile_count"] == 80
    assert len(registry["tiles"]) == 80
    assert len({row["tile_id"] for row in registry["tiles"]}) == 80
    assert registry["tiles"][0]["tile_id"] == "M1"
    assert registry["tiles"][0]["tile_key"] == "mission.M1"
    assert registry["tiles"][-1]["tile_key"] == "coherencia.C10"
    assert all(isinstance(row["absence_test_contract_ids"], list) for row in registry["tiles"])
    assert len(tile_contract_registry_fingerprint()) == 64
    assert tile_contract_registry_fingerprint() == (tile_contract_registry_fingerprint())

    assert policy["legacy_weakens_semantics"] == "review_required"
    assert policy["not_reacquired_semantics"] == "no_state_change"
    assert len(reducer_policy_fingerprint()) == 64


@pytest.mark.parametrize(
    ("polarities", "expected"),
    [
        ([], TileState.SIN_EVIDENCIA.value),
        (["supports"], TileState.OK.value),
        (["contradicts"], TileState.NO.value),
        (["supports", "contradicts"], TileState.CONTRADICTION.value),
        (["invalidates_candidate"], TileState.SIN_EVIDENCIA.value),
        (["irrelevant"], TileState.SIN_EVIDENCIA.value),
    ],
)
def test_reducer_truth_table(
    polarities: list[str],
    expected: str,
) -> None:
    result = reduce_tile_basis([_basis(index + 1, polarity) for index, polarity in enumerate(polarities)])

    assert result["state"] == expected
    assert result["reducer_policy_fingerprint"] == (reducer_policy_fingerprint())


def test_reducer_is_order_invariant_and_rejects_duplicate_relations() -> None:
    support = _basis(1, "supports")
    contradiction = _basis(2, "contradicts")

    forward = reduce_tile_basis([support, contradiction])
    reverse = reduce_tile_basis([deepcopy(contradiction), deepcopy(support)])

    assert reverse == forward
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="duplicate relation ids",
    ):
        reduce_tile_basis([support, deepcopy(support)])


def test_legacy_weakens_never_becomes_no() -> None:
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="unsupported evidence polarity: weakens",
    ):
        reduce_tile_basis([_basis(1, "weakens")])


def test_demonstrates_absence_requires_declared_contract_and_coverage() -> None:
    relation = _basis(
        1,
        "demonstrates_absence",
        absence_test_contract_id="mission-public-surface-absence-v1",
        coverage_assessment_id="coverage-1",
        coverage_status="sufficient",
        tested_scope="all declared first-party strategic surfaces",
        observed_result="no qualifying mission statement or mechanism",
    )

    result = reduce_tile_basis(
        [relation],
        allowed_absence_contract_ids=["mission-public-surface-absence-v1"],
    )

    assert result["state"] == TileState.NO.value

    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="declared tile absence contract",
    ):
        reduce_tile_basis([relation])
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="declared tile absence contract",
    ):
        build_candidate_tile(tile_id="M1", basis=[relation])

    insufficient = deepcopy(relation)
    insufficient["coverage_status"] = "insufficient"
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="sufficient coverage",
    ):
        reduce_tile_basis(
            [insufficient],
            allowed_absence_contract_ids=["mission-public-surface-absence-v1"],
        )

    incomplete = deepcopy(relation)
    incomplete["observed_result"] = None
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="metadata is incomplete",
    ):
        reduce_tile_basis(
            [incomplete],
            allowed_absence_contract_ids=["mission-public-surface-absence-v1"],
        )


def test_candidate_tile_keeps_tile_id_and_tile_key_distinct() -> None:
    direct_evidence = _basis(1, "supports", claim_id=None)

    candidate = build_candidate_tile(tile_id="M1", basis=[direct_evidence])

    assert candidate["tile_id"] == "M1"
    assert candidate["tile_key"] == "mission.M1"
    assert candidate["component_key"] == "mission"
    assert candidate["candidate_state"] == TileState.OK.value
    assert candidate["authority_state"] == AuthorityState.PENDING_REVIEW.value
    assert candidate["provenance_status"] == ("scanner_derived_unreviewed")
    assert candidate["basis"][0]["claim_id"] is None
    assert candidate["single_source_dependency"] is True
    assert candidate["changes_tile_state"] is False
    assert candidate["changes_score"] is False

    second_source = build_candidate_tile(
        tile_id="M1",
        basis=[direct_evidence, _basis(2, "supports")],
    )
    same_source = build_candidate_tile(
        tile_id="M1",
        basis=[
            direct_evidence,
            _basis(
                2,
                "supports",
                source_identity_id=direct_evidence["source_identity_id"],
            ),
        ],
    )
    assert second_source["single_source_dependency"] is False
    assert same_source["single_source_dependency"] is True


def test_accepted_basis_requires_an_event_and_is_not_packet_authority() -> None:
    accepted = _basis(
        1,
        "supports",
        review_status="accepted",
        decision_event_id="review-event-1",
    )
    candidate = build_candidate_tile(tile_id="M1", basis=[accepted])

    assert candidate["provenance_status"] == "accepted_basis"
    assert candidate["authority_state"] == "pending_review"

    missing_event = deepcopy(accepted)
    missing_event["decision_event_id"] = None
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="accepted basis requires decision_event_id",
    ):
        build_candidate_tile(tile_id="M1", basis=[missing_event])


def test_canonical_predecessor_requires_an_explicit_delta() -> None:
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="delta_kind is required",
    ):
        build_candidate_tile(
            tile_id="M1",
            previous_canonical_state="ok",
        )

    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="must preserve the previous canonical state",
    ):
        build_candidate_tile(
            tile_id="M1",
            previous_canonical_state="ok",
            delta_kind="coverage_loss",
        )


def test_verified_deprecation_can_propose_ok_to_sin_evidencia_but_is_not_automatic() -> None:
    previous = _all_empty_candidate_tiles()
    previous[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_accepted_basis(1, "supports")],
    )
    invalidation = _accepted_basis(2, "invalidates_candidate")

    candidates = build_incremental_candidate_tiles(
        previous_candidate_tiles=previous,
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "verified_deprecation",
                "basis": [invalidation],
            }
        ],
    )

    assert previous[0]["candidate_state"] == "ok"
    assert candidates[0]["candidate_state"] == "sin_evidencia"
    assert candidates[0]["delta_kind"] == "verified_deprecation"
    assert candidates[0]["changes_tile_state"] is True
    assert candidates[0]["changes_score"] is True


def test_verified_deprecation_can_change_provenance_without_changing_score() -> None:
    previous = _all_empty_candidate_tiles()
    previous[0] = build_candidate_tile(
        tile_id="M1",
        basis=[
            _accepted_basis(1, "supports"),
            _accepted_basis(2, "supports"),
        ],
    )

    candidates = build_incremental_candidate_tiles(
        previous_candidate_tiles=previous,
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "verified_deprecation",
                "basis": [
                    _accepted_basis(1, "supports"),
                    _accepted_basis(3, "invalidates_candidate"),
                ],
            }
        ],
    )

    assert candidates[0]["candidate_state"] == "ok"
    assert candidates[0]["delta_kind"] == "verified_deprecation"
    assert candidates[0]["changes_tile_state"] is False
    assert candidates[0]["changes_score"] is False


def test_contradiction_candidate_requires_an_explicit_unresolved_reference() -> None:
    basis = [_basis(1, "supports"), _basis(2, "contradicts")]

    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="require an unresolved reference",
    ):
        build_candidate_tile(tile_id="M1", basis=basis)

    candidate = build_candidate_tile(
        tile_id="M1",
        basis=basis,
        previous_canonical_state="ok",
        delta_kind="contradiction",
        unresolved_refs=["contradiction-m1"],
    )

    assert candidate["candidate_state"] == "contradiction"
    assert candidate["provenance_status"] == "contradiction"
    assert candidate["changes_score"] is True


def test_packet_is_complete_pending_review_and_order_invariant() -> None:
    tiles = _all_empty_candidate_tiles()

    forward = _packet(tiles)
    reverse = _packet(list(reversed(deepcopy(tiles))))

    assert reverse == forward
    assert forward["manifest"]["candidate_count"] == 80
    assert forward["manifest"]["authority_state"] == "pending_review"
    assert forward["manifest"]["delta_summary"]["by_kind"]["baseline"] == 80
    assert forward["manifest"]["delta_summary"]["tile_state_change_count"] == 0
    assert len(forward["candidate_packet_fingerprint"]) == 64
    assert len(forward["manifest"]["derived_tile_state_fingerprint"]) == 64
    assert forward["candidate_tiles"][0]["tile_key"] == "mission.M1"
    assert forward["candidate_tiles"][-1]["tile_key"] == "coherencia.C10"
    assert all(tile["authority_state"] == "pending_review" for tile in forward["candidate_tiles"])


def test_packet_ancestry_separates_baseline_from_incremental_memory() -> None:
    baseline = _all_empty_candidate_tiles()
    incremental = build_incremental_candidate_tiles(previous_candidate_tiles=baseline)

    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="baseline candidate packet cannot declare a canonical parent",
    ):
        _packet(baseline, parent_canonical_memory_version=HASH_F)

    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="incremental candidate packet requires a canonical parent",
    ):
        _packet(incremental)

    packet = _packet(incremental, parent_canonical_memory_version=HASH_F)
    assert packet["manifest"]["parent_canonical_memory_version"] == HASH_F
    assert packet["manifest"]["delta_summary"]["by_kind"]["no_change"] == 80


def test_incremental_generation_reuses_all_unaffected_tiles() -> None:
    previous = _all_empty_candidate_tiles()
    previous[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_accepted_basis(1, "supports")],
    )
    previous_snapshot = deepcopy(previous)

    candidates = build_incremental_candidate_tiles(
        previous_candidate_tiles=previous,
    )

    assert previous == previous_snapshot
    assert len(candidates) == 80
    assert all(row["delta_kind"] == "no_change" for row in candidates)
    assert [row["candidate_state"] for row in candidates] == [row["candidate_state"] for row in previous]
    assert [row["basis"] for row in candidates] == [row["basis"] for row in previous]
    assert all(row["changes_tile_state"] is False for row in candidates)
    assert all(row["changes_score"] is False for row in candidates)


def test_incremental_parent_validation_rejects_forged_no_change_basis() -> None:
    previous = _all_empty_candidate_tiles()
    previous[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_accepted_basis(1, "supports")],
    )
    forged = build_incremental_candidate_tiles(previous_candidate_tiles=previous)
    forged[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_accepted_basis(2, "supports")],
        previous_canonical_state="ok",
        delta_kind="no_change",
    )

    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="no_change must reuse canonical basis",
    ):
        validate_incremental_candidate_tiles(
            previous_candidate_tiles=previous,
            candidate_tiles=forged,
        )


def test_coverage_loss_keeps_previous_state_and_basis() -> None:
    previous = _all_empty_candidate_tiles()
    previous[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_accepted_basis(1, "supports")],
        coverage_refs=["coverage-baseline"],
    )

    candidates = build_incremental_candidate_tiles(
        previous_candidate_tiles=previous,
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "coverage_loss",
                "coverage_refs": ["coverage-scan-2"],
            }
        ],
    )

    assert candidates[0]["candidate_state"] == "ok"
    assert candidates[0]["basis"] == previous[0]["basis"]
    assert candidates[0]["delta_kind"] == "coverage_loss"
    assert candidates[0]["coverage_refs"] == ["coverage-baseline", "coverage-scan-2"]
    assert candidates[0]["changes_tile_state"] is False
    assert candidates[0]["changes_score"] is False


def test_incremental_update_changes_only_the_target_tile() -> None:
    previous = _all_empty_candidate_tiles()

    candidates = build_incremental_candidate_tiles(
        previous_candidate_tiles=previous,
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "candidate_update",
                "basis": [_basis(1, "supports")],
            }
        ],
    )

    assert candidates[0]["candidate_state"] == "ok"
    assert candidates[0]["delta_kind"] == "candidate_update"
    assert candidates[0]["changes_tile_state"] is True
    assert candidates[0]["changes_score"] is True
    assert all(row["delta_kind"] == "no_change" for row in candidates[1:])
    assert [row["candidate_state"] for row in candidates[1:]] == [row["candidate_state"] for row in previous[1:]]


def test_incremental_contradiction_blocks_candidate_without_mutating_predecessor() -> None:
    previous = _all_empty_candidate_tiles()
    previous[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_accepted_basis(1, "supports")],
    )
    previous_snapshot = deepcopy(previous)

    candidates = build_incremental_candidate_tiles(
        previous_candidate_tiles=previous,
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "contradiction",
                "basis": [
                    _accepted_basis(1, "supports"),
                    _basis(2, "contradicts"),
                ],
                "unresolved_refs": ["contradiction-m1"],
            }
        ],
    )

    assert previous == previous_snapshot
    assert previous[0]["candidate_state"] == "ok"
    assert candidates[0]["candidate_state"] == "contradiction"
    assert candidates[0]["authority_state"] == "pending_review"
    assert candidates[0]["delta_kind"] == "contradiction"


def test_new_provenance_changes_packet_but_not_derived_tile_state() -> None:
    first_tiles = _all_empty_candidate_tiles()
    first_tiles[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_accepted_basis(1, "supports")],
    )
    strengthened_tiles = build_incremental_candidate_tiles(
        previous_candidate_tiles=first_tiles,
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "strengthened",
                "basis": [
                    _accepted_basis(1, "supports"),
                    _accepted_basis(2, "supports"),
                ],
            }
        ],
    )

    first = _packet(first_tiles)
    strengthened = _packet(
        strengthened_tiles,
        candidate_memory_version=HASH_F,
        parent_canonical_memory_version=HASH_A,
    )

    assert (
        strengthened["manifest"]["derived_tile_state_fingerprint"]
        == first["manifest"]["derived_tile_state_fingerprint"]
    )
    assert strengthened["candidate_packet_fingerprint"] != (first["candidate_packet_fingerprint"])
    assert strengthened["manifest"]["delta_summary"]["by_kind"]["strengthened"] == 1
    assert strengthened["manifest"]["delta_summary"]["by_kind"]["no_change"] == 79
    assert strengthened["manifest"]["delta_summary"]["score_affecting_count"] == 0


def test_packet_binds_blocking_contradiction_without_granting_authority() -> None:
    tiles = _all_empty_candidate_tiles()
    tiles[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_basis(1, "supports"), _basis(2, "contradicts")],
        unresolved_refs=["contradiction-m1"],
    )
    packet = _packet(
        tiles,
        unresolved_items=[
            {
                "unresolved_id": "contradiction-m1",
                "kind": "contradiction",
                "blocking": True,
                "details": {"tile_key": "mission.M1"},
            }
        ],
    )

    assert packet["candidate_tiles"][0]["candidate_state"] == ("contradiction")
    assert packet["manifest"]["authority_state"] == "pending_review"
    assert packet["manifest"]["unresolved_items"][0]["blocking"] is True


def test_packet_validation_rejects_tampering_and_incomplete_coverage() -> None:
    packet = _packet(_all_empty_candidate_tiles())

    authoritative = deepcopy(packet)
    authoritative["manifest"]["authority_state"] = "accepted"
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="must remain pending_review",
    ):
        validate_candidate_packet(authoritative)

    altered_state = deepcopy(packet)
    altered_state["candidate_tiles"][0]["candidate_state"] = "ok"
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="does not match reducer output",
    ):
        validate_candidate_packet(altered_state)

    altered_fingerprint = deepcopy(packet)
    altered_fingerprint["candidate_packet_fingerprint"] = HASH_F
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="candidate packet fingerprint mismatch",
    ):
        validate_candidate_packet(altered_fingerprint)

    missing_tile = _all_empty_candidate_tiles()[:-1]
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="tile coverage mismatch",
    ):
        _packet(missing_tile)


def test_packet_fails_closed_on_mixed_versions_and_unknown_references() -> None:
    packet = _packet(_all_empty_candidate_tiles())

    wrong_registry = deepcopy(packet)
    wrong_registry["manifest"]["tile_contract_registry_fingerprint"] = HASH_F
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="tile contract registry fingerprint mismatch",
    ):
        validate_candidate_packet(wrong_registry)

    wrong_schema = deepcopy(packet)
    wrong_schema["manifest"]["schema_version"] = "future-packet"
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="unsupported candidate packet schema version",
    ):
        validate_candidate_packet(wrong_schema)

    tiles = _all_empty_candidate_tiles()
    tiles[0] = build_candidate_tile(
        tile_id="M1",
        unresolved_refs=["missing-unresolved-item"],
    )
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="unknown unresolved items",
    ):
        _packet(tiles)


def test_schema_rejects_coerced_text_and_derived_field_tampering() -> None:
    invalid_review = _basis(1, "supports")
    invalid_review["review_status"] = 1
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="review_status must be text",
    ):
        build_candidate_tile(tile_id="M1", basis=[invalid_review])

    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="brand_identity must be text",
    ):
        build_candidate_packet(
            brand_identity=1,  # type: ignore[arg-type]
            parent_canonical_memory_version=None,
            candidate_memory_version=HASH_A,
            accepted_memory_candidate_version=HASH_B,
            reviewed_memory_candidate_version=HASH_C,
            review_packet_set_fingerprint=HASH_D,
            aggregation_policy_fingerprint=HASH_E,
            candidate_tiles=_all_empty_candidate_tiles(),
        )

    packet = _packet(_all_empty_candidate_tiles())
    tampered = deepcopy(packet)
    tampered["candidate_tiles"][0]["single_source_dependency"] = True
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="does not match reducer output",
    ):
        validate_candidate_packet(tampered)

    uppercase_hash = deepcopy(packet)
    uppercase_hash["manifest"]["candidate_memory_version"] = HASH_A.upper()
    uppercase_hash["candidate_packet_fingerprint"] = canonical_fingerprint(
        "evidence-vault-candidate-packet-fingerprint-v1",
        {
            "manifest": uppercase_hash["manifest"],
            "candidate_tiles": uppercase_hash["candidate_tiles"],
        },
    )
    with pytest.raises(
        EvidenceVaultCanonicalCoreError,
        match="candidate_memory_version is not canonically normalized",
    ):
        validate_candidate_packet(uppercase_hash)


def _basis(
    seed: int,
    polarity: str,
    *,
    claim_id: str | None = HASH_C,
    source_identity_id: str | None = None,
    review_status: str = "unreviewed",
    decision_event_id: str | None = None,
    absence_test_contract_id: str | None = None,
    coverage_assessment_id: str | None = None,
    coverage_status: str | None = None,
    tested_scope: str | None = None,
    observed_result: str | None = None,
) -> dict:
    character = format(seed, "x")[-1]
    source_character = format(seed + 4, "x")[-1]
    return {
        "relation_id": character * 64,
        "evidence_id": format(seed + 8, "x")[-1] * 64,
        "source_identity_id": (source_identity_id or source_character * 64),
        "claim_id": claim_id,
        "polarity": polarity,
        "review_status": review_status,
        "decision_event_id": decision_event_id,
        "absence_test_contract_id": absence_test_contract_id,
        "coverage_assessment_id": coverage_assessment_id,
        "coverage_status": coverage_status,
        "tested_scope": tested_scope,
        "observed_result": observed_result,
    }


def _accepted_basis(seed: int, polarity: str) -> dict:
    return _basis(
        seed,
        polarity,
        review_status="accepted",
        decision_event_id=f"review-{seed}",
    )


def _all_empty_candidate_tiles() -> list[dict]:
    return [build_candidate_tile(tile_id=row["tile_id"]) for row in build_tile_contract_registry()["tiles"]]


def _packet(
    tiles: list[dict],
    *,
    unresolved_items: list[dict] | None = None,
    candidate_memory_version: str = HASH_A,
    parent_canonical_memory_version: str | None = None,
) -> dict:
    return build_candidate_packet(
        brand_identity="Example.COM",
        parent_canonical_memory_version=parent_canonical_memory_version,
        candidate_memory_version=candidate_memory_version,
        accepted_memory_candidate_version=HASH_B,
        reviewed_memory_candidate_version=HASH_C,
        review_packet_set_fingerprint=HASH_D,
        aggregation_policy_fingerprint=HASH_E,
        candidate_tiles=tiles,
        coverage_summary={"assessment_count": 0},
        unresolved_items=unresolved_items or [],
    )
