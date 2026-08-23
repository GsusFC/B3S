from __future__ import annotations

import dataclasses
import json

import pytest

from src.research.social_tiles import (
    CATALOG,
    CATALOG_VERSION,
    COMPONENT_IDS,
    CaptureSet,
    TILE_IDS,
    CatalogIntegrityError,
    ComponentDefinition,
    TileDefinition,
    TileState,
    TileVerdict,
    assert_catalog_integrity,
    build_capture_set,
)
from src.research.social_lab_contracts import MetricContext, Provenance, SocialObservation, build_social_observation


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


def _capture_observation(
    *,
    external_id: str = "post-1",
    platform: str = "x",
    text: str = "A useful launch update.",
    published_at: str = "2026-08-21T12:00:00Z",
    fetched_at: str = "2026-08-22T08:00:00Z",
    response_sha256: str = "a" * 64,
    request_fingerprint: str = "request-fingerprint-1",
    metric_context: MetricContext | None = None,
) -> SocialObservation:
    provenance = Provenance(
        provider="scrapecreators",
        endpoint_key="brand_posts",
        target_fingerprint="target-fingerprint-1",
        manifest_fingerprint="manifest-fingerprint-1",
        request_fingerprint=request_fingerprint,
        response_sha256=response_sha256,
        fetched_at=fetched_at,
        page=1,
        cursor="cursor-1",
        result_ordinal=0,
        target_identity={"account_id": "brand-account-1", "handle": "brandco"},
        linkage_evidence={},
    )
    return build_social_observation(
        platform=platform,
        record_kind="post",
        external_id=external_id,
        canonical_url=f"https://social.example/brandco/status/{external_id}",
        author_account_id="brand-account-1",
        author_handle="brandco",
        parent_external_id=None,
        thread_external_id=None,
        text=text,
        media_kind="text",
        published_at=published_at,
        provenance=provenance,
        bound_brand_identity={"account_id": "brand-account-1", "handle": "brandco"},
        metric_context=metric_context,
    )


def test_capture_set_is_order_independent_and_serializes_deterministically() -> None:
    observations = [_capture_observation(external_id="post-1"), _capture_observation(external_id="post-2", platform="instagram")]
    first = build_capture_set(observations)
    second = build_capture_set(reversed(observations))
    assert first.capture_set_id == second.capture_set_id
    assert first.receipt_integrity_sha256 == second.receipt_integrity_sha256
    assert first.to_json() == second.to_json()
    assert first.observation_count == 2
    assert first.target_fingerprints == ("target-fingerprint-1",)
    assert first.platforms == ("instagram", "x")


def test_metric_only_changes_do_not_change_capture_or_receipt_identity() -> None:
    base = _capture_observation()
    changed = dataclasses.replace(
        base,
        metric_context=MetricContext(metrics={"likes": 4}, observed_at="2026-08-22T09:00:00Z"),
    )
    assert build_capture_set([base]).capture_set_id == build_capture_set([changed]).capture_set_id
    assert build_capture_set([base]).receipt_integrity_sha256 == build_capture_set([changed]).receipt_integrity_sha256


def test_fetch_or_raw_receipt_changes_only_receipt_integrity() -> None:
    base = _capture_observation()
    changed = _capture_observation(fetched_at="2026-08-23T08:00:00Z", response_sha256="b" * 64)
    first, second = build_capture_set([base]), build_capture_set([changed])
    assert first.capture_set_id == second.capture_set_id
    assert first.receipt_integrity_sha256 != second.receipt_integrity_sha256


def test_semantic_or_request_scope_changes_capture_set_id() -> None:
    base = _capture_observation()
    semantic = _capture_observation(text="A materially different update.")
    request = _capture_observation(request_fingerprint="request-fingerprint-2")
    assert build_capture_set([base]).capture_set_id != build_capture_set([semantic]).capture_set_id
    assert build_capture_set([base]).capture_set_id != build_capture_set([request]).capture_set_id


def test_capture_set_derives_bounded_ranges_and_empty_sets_without_claiming_coverage() -> None:
    capture = build_capture_set(
        [
            _capture_observation(external_id="post-1", published_at="2026-08-20T12:00:00Z", fetched_at="2026-08-21T08:00:00Z"),
            _capture_observation(external_id="post-2", published_at="2026-08-22T12:00:00Z", fetched_at="2026-08-23T08:00:00Z"),
        ]
    )
    assert capture.observed_published_from == "2026-08-20T12:00:00Z"
    assert capture.observed_published_through == "2026-08-22T12:00:00Z"
    assert capture.fetched_from == "2026-08-21T08:00:00Z"
    assert capture.fetched_through == "2026-08-23T08:00:00Z"
    empty = build_capture_set([])
    assert empty.observation_count == 0
    assert empty.observed_published_from is None
    assert empty.fetched_through is None
    assert "bounded" in empty.coverage_disclaimer and "non-exhaustive" in empty.coverage_disclaimer
    assert empty.capture_set_id == build_capture_set([]).capture_set_id


def test_capture_set_reconstruction_rejects_tampered_model_hashes_and_unvalidated_inputs() -> None:
    observation = _capture_observation()
    capture = build_capture_set([observation])
    tampered = {**capture.to_dict(), "capture_set_id": "sha256:" + "0" * 64}
    with pytest.raises(ValueError):
        CaptureSet.from_dict(tampered, [observation])
    tampered_receipt = {**capture.to_dict(), "receipt_integrity_sha256": "sha256:" + "0" * 64}
    with pytest.raises(ValueError):
        CaptureSet.from_dict(tampered_receipt, [observation])
    with pytest.raises(ValueError):
        build_capture_set([observation.to_dict()])
