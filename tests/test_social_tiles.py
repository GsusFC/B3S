from __future__ import annotations

import dataclasses
import json

import pytest

from src.research.social_tiles import (
    CATALOG,
    CATALOG_VERSION,
    COMPONENT_IDS,
    TILE_IDS,
    CatalogIntegrityError,
    ComponentDefinition,
    TileDefinition,
    TileState,
    TileVerdict,
    assert_catalog_integrity,
)


def test_catalog_is_the_ordered_v1_six_component_twelve_tile_contract() -> None:
    assert CATALOG_VERSION == "b3s-social-tiles-v1"
    assert tuple(component.component_id for component in CATALOG.components) == COMPONENT_IDS
    assert tuple(tile.tile_id for component in CATALOG.components for tile in component.tiles) == TILE_IDS
    assert len(COMPONENT_IDS) == 6
    assert len(TILE_IDS) == 12
    assert len(set(TILE_IDS)) == len(TILE_IDS)
    assert CATALOG.components[0].tiles[0].evidence_requirement == ">=2 official posts"
    assert CATALOG.components[-1].tiles[-1].evidence_requirement == (
        "tension-bearing response + linked brand reply"
    )


def test_catalog_integrity_rejects_unknown_duplicate_and_missing_values() -> None:
    baseline = CATALOG.components
    unknown = dataclasses.replace(
        baseline[0],
        tiles=(
            dataclasses.replace(baseline[0].tiles[0], tile_id="ST-XX-99"),
            baseline[0].tiles[1],
        ),
    )
    duplicate = dataclasses.replace(
        baseline[0],
        tiles=(baseline[0].tiles[0], baseline[0].tiles[0]),
    )
    missing = dataclasses.replace(baseline[0], tiles=(baseline[0].tiles[0],))

    for component in (unknown, duplicate, missing):
        with pytest.raises(CatalogIntegrityError):
            assert_catalog_integrity(dataclasses.replace(CATALOG, components=(component, *baseline[1:])))


@pytest.mark.parametrize("state", tuple(TileState))
def test_verdict_is_immutable_and_serializes_deterministically(state: TileState) -> None:
    if state is TileState.DEMONSTRATED:
        verdict = TileVerdict(tile_id="ST-VI-01", state=state, citations=("post-1",))
    elif state is TileState.CONTRADICTED:
        verdict = TileVerdict(tile_id="ST-VI-01", state=state, citations=("post-1",))
    elif state is TileState.NOT_OBSERVED:
        verdict = TileVerdict(tile_id="ST-VI-01", state=state)
    else:
        verdict = TileVerdict(
            tile_id="ST-VI-01",
            state=state,
            reason_code="insufficient_capture",
            failure_stage="acquisition",
        )

    assert dataclasses.is_dataclass(verdict)
    assert verdict.citations == tuple(verdict.citations)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.tile_id = "ST-CC-01"  # type: ignore[misc]
    payload = verdict.to_dict()
    assert tuple(payload) == ("tile_id", "state", "citations", "reason_code", "failure_stage")
    assert json.dumps(payload, sort_keys=True, separators=(",", ":")) == verdict.to_json()


def test_verdict_state_invariants_fail_closed() -> None:
    with pytest.raises(ValueError):
        TileVerdict(tile_id="ST-XX-99", state="not_observed")
    with pytest.raises(ValueError):
        TileVerdict(tile_id="ST-VI-01", state="demonstrated")
    with pytest.raises(ValueError):
        TileVerdict(tile_id="ST-VI-01", state="demonstrated", citations=("post-1",), reason_code="bad")
    with pytest.raises(ValueError):
        TileVerdict(tile_id="ST-VI-01", state="not_observed", citations=("post-1",))
    with pytest.raises(ValueError):
        TileVerdict(tile_id="ST-VI-01", state="not_acquired", reason_code="missing")


def test_catalog_records_are_immutable_and_serialization_contains_no_score_fields() -> None:
    assert dataclasses.is_dataclass(CATALOG)
    assert dataclasses.is_dataclass(CATALOG.components[0])
    assert dataclasses.is_dataclass(CATALOG.components[0].tiles[0])
    with pytest.raises(dataclasses.FrozenInstanceError):
        CATALOG.components = ()  # type: ignore[misc]
    serialized = CATALOG.to_json()
    assert "score" not in serialized
    assert "confidence" not in serialized
    assert json.loads(serialized)["version"] == CATALOG_VERSION


def test_component_and_tile_records_are_typed_and_linked() -> None:
    component = ComponentDefinition(
        component_id="example",
        tiles=(
            TileDefinition(
                tile_id="EX-01",
                component_id="example",
                description="Example",
                evidence_requirement="one citation",
            ),
        ),
    )
    assert component.tile_ids == ("EX-01",)
    assert component.tiles[0].component_id == component.component_id
