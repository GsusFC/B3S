from __future__ import annotations

from copy import deepcopy
import json

from src.services.evidence_claim_tile_ledger import (
    build_evidence_claim_tile_ledger,
)


def test_literal_claim_quote_maps_without_returning_raw_text() -> None:
    claim = "Our mission is to simplify finance."
    result = build_evidence_claim_tile_ledger(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [
                    _owned(claim),
                    _derived_claim(
                        claim,
                        source_evidence_ref="web.0",
                        claim_slot_key="mission.primary",
                    ),
                ],
                [_tile("M1", claim)],
            )
        ],
        mode="shadow",
    )

    assert result["schema_version"] == "evidence-claim-tile-ledger-v1"
    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"]["mapping_count"] == 1
    mapping = result["mappings"][0]
    assert mapping["tile_key"] == "mission.M1"
    assert mapping["polarity"] == "supports"
    assert mapping["state"] == "observed"
    assert mapping["source_independence_status"] == "owned_source"
    assert mapping["runtime_effect"] is False
    assert mapping["authority"] is False
    assert claim not in json.dumps(result)


def test_same_page_quote_without_claim_anchor_is_rejected() -> None:
    source = (
        "Our mission is to simplify finance. "
        "We also publish a weekly newsletter."
    )
    result = build_evidence_claim_tile_ledger(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [
                    _owned(source),
                    _derived_claim(
                        "Our mission is to simplify finance.",
                        source_evidence_ref="web.0",
                        claim_slot_key="mission.primary",
                    ),
                ],
                [_tile("M1", "We also publish a weekly newsletter.")],
            )
        ],
        mode="shadow",
    )

    assert result["mappings"] == []
    assert result["summary"]["diagnostic_counts"][
        "tile_quote_does_not_anchor_claim"
    ] == 1


def test_non_literal_quote_is_rejected() -> None:
    result = build_evidence_claim_tile_ledger(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [
                    _owned(
                        "Our mission is to simplify finance.",
                        claim_slot_key="mission.primary",
                    )
                ],
                [_tile("M1", "Our mission is to transform finance.")],
            )
        ],
        mode="shadow",
    )

    assert result["mappings"] == []
    assert result["summary"]["diagnostic_counts"][
        "tile_quote_not_literal_in_claim_source"
    ] == 1


def test_ordered_ellipsis_fragments_are_literal_and_claim_anchored() -> None:
    source = (
        "Our mission is to simplify finance for every small business "
        "without adding operational overhead."
    )
    result = build_evidence_claim_tile_ledger(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [_owned(source, claim_slot_key="mission.primary")],
                [
                    _tile(
                        "M1",
                        "Our mission is to simplify finance..."
                        "without adding operational overhead.",
                    )
                ],
            )
        ],
        mode="shadow",
    )

    assert result["summary"]["mapping_count"] == 1


def test_explicit_evidence_ref_must_match_claim_source() -> None:
    result = build_evidence_claim_tile_ledger(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [
                    _owned(
                        "Our mission is to simplify finance.",
                        claim_slot_key="mission.primary",
                    )
                ],
                [
                    _tile(
                        "M1",
                        "Our mission is to simplify finance.",
                        evidence_ref="web.99",
                    )
                ],
            )
        ],
        mode="shadow",
    )

    assert result["mappings"] == []
    assert result["summary"]["diagnostic_counts"][
        "explicit_evidence_ref_does_not_match_claim_source"
    ] == 1


def test_ambiguous_literal_claim_mapping_fails_closed() -> None:
    claim = "Our mission is to simplify finance."
    first = _owned(claim, claim_slot_key="mission.primary")
    second = _owned(
        claim,
        ref="web.1",
        url="https://example.com/company",
        claim_slot_key="mission.primary",
    )
    result = build_evidence_claim_tile_ledger(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [first, second],
                [_tile("M1", claim)],
            )
        ],
        mode="shadow",
    )

    assert result["mappings"] == []
    assert result["summary"]["diagnostic_counts"][
        "tile_claim_mapping_is_ambiguous"
    ] == 1


def test_repeat_is_persistence_not_breadth_or_scoring_authority() -> None:
    claim = "Our mission is to simplify finance."
    first = _report(
        "one",
        "2026-01-01T00:00:00Z",
        [_owned(claim, claim_slot_key="mission.primary")],
        [_tile("M1", claim)],
    )
    second = _report(
        "two",
        "2026-01-02T00:00:00Z",
        [_owned(claim, claim_slot_key="mission.primary")],
        [_tile("M1", claim)],
    )
    result = build_evidence_claim_tile_ledger(
        [first, second],
        mode="shadow",
    )

    assert result["summary"]["mapping_count"] == 1
    assert result["summary"]["mapping_observation_count"] == 2
    mapping = result["mappings"][0]
    assert mapping["state"] == "repeated"
    assert mapping["observation_count"] == 2
    assert result["policy"]["exact_repeat_increases_breadth"] is False
    assert result["policy"]["automatic_scoring_effect"] is False


def test_new_evaluator_creates_a_new_mapping_series() -> None:
    claim = "Our mission is to simplify finance."
    old = _report(
        "old",
        "2026-01-01T00:00:00Z",
        [_owned(claim, claim_slot_key="mission.primary")],
        [_tile("M1", claim)],
        evaluator_model="evaluator-a",
    )
    new = _report(
        "new",
        "2026-01-02T00:00:00Z",
        [_owned(claim, claim_slot_key="mission.primary")],
        [_tile("M1", claim)],
        evaluator_model="evaluator-b",
    )
    result = build_evidence_claim_tile_ledger(
        [old, new],
        mode="shadow",
    )

    assert result["summary"]["mapping_series_count"] == 2
    assert result["summary"]["mapping_count"] == 2
    assert {mapping["state"] for mapping in result["mappings"]} == {
        "observed"
    }
    assert all(
        mapping["present_in_series_latest"]
        for mapping in result["mappings"]
    )
    assert sum(
        mapping["present_in_latest_report"]
        for mapping in result["mappings"]
    ) == 1


def test_report_order_does_not_change_projection() -> None:
    claim = "Our mission is to simplify finance."
    reports = [
        _report(
            "one",
            "2026-01-01T00:00:00Z",
            [_owned(claim, claim_slot_key="mission.primary")],
            [_tile("M1", claim)],
        ),
        _report(
            "two",
            "2026-01-02T00:00:00Z",
            [_owned(claim, claim_slot_key="mission.primary")],
            [_tile("M1", claim)],
        ),
    ]

    baseline = build_evidence_claim_tile_ledger(
        reports,
        mode="shadow",
    )
    reordered = build_evidence_claim_tile_ledger(
        list(reversed(deepcopy(reports))),
        mode="shadow",
    )

    assert reordered == baseline
    assert reordered["state_fingerprint"] == baseline["state_fingerprint"]


def test_disabled_mode_returns_deterministic_empty_projection() -> None:
    disabled = build_evidence_claim_tile_ledger(
        [
            _report(
                "one",
                "2026-01-01T00:00:00Z",
                [],
                [],
            )
        ],
        mode="off",
    )

    assert disabled["mode"] == "disabled"
    assert disabled["mappings"] == []
    assert disabled["mapping_series"] == []
    assert len(disabled["state_fingerprint"]) == 64


def _owned(
    content: str,
    *,
    ref: str = "web.0",
    url: str = "https://example.com/about",
    **metadata: str,
) -> dict:
    return {
        "ref": ref,
        "source": "web",
        "evidence_type": "raw_input",
        "url": url,
        "content": content,
        "metadata": {
            "source_class": "owned_copy",
            **metadata,
        },
    }


def _tile(
    tile_id: str,
    quote: str,
    *,
    state: str = "ok",
    evidence_ref: str = "",
) -> dict:
    return {
        "id": tile_id,
        "estado": state,
        "evidencia": quote,
        "evidence_ref": evidence_ref,
    }


def _derived_claim(
    content: str,
    *,
    ref: str = "claim.0",
    source_evidence_ref: str,
    **metadata: str,
) -> dict:
    return {
        "ref": ref,
        "source": "derived_strategy",
        "evidence_type": "interpreted_claim",
        "url": "https://example.com/about",
        "content": content,
        "metadata": {
            "source_class": "derived_strategy",
            "source_evidence_ref": source_evidence_ref,
            **metadata,
        },
    }


def _report(
    report_id: str,
    created_at: str,
    evidence: list[dict],
    tiles: list[dict],
    *,
    evaluator_model: str = "evaluator-a",
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
            "schema_version": "report-v1",
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "brand_name": "Example",
                        "url": "https://example.com",
                        "evidence": deepcopy(evidence),
                    }
                }
            },
            "sv9": {
                "evaluator_model": evaluator_model,
                "result": {
                    "rubric_version": "sv9-rubric-v1",
                    "components": {
                        "mission": {
                            "tile_profile": deepcopy(tiles),
                        }
                    },
                },
            },
        },
    }
