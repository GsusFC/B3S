from __future__ import annotations

from src.services.evidence_claim_tile_review import (
    claim_tile_review_case_id,
)


def test_case_id_is_stable_and_locates_the_mapping() -> None:
    mapping = {
        "mapping_id": "a" * 64,
        "component_key": "core_purpose",
        "tile_id": "PR1",
    }

    assert claim_tile_review_case_id("Example.COM", mapping) == (
        "claim-tile-example-com-core-purpose-pr1-aaaaaaaaaaaa"
    )


def test_case_id_cannot_exceed_database_limit() -> None:
    mapping = {
        "mapping_id": "a" * 64,
        "component_key": "component-" * 100,
        "tile_id": "tile-" * 100,
    }

    case_id = claim_tile_review_case_id("domain-" * 100, mapping)

    assert len(case_id) <= 300
    assert case_id.endswith("-aaaaaaaaaaaa")
