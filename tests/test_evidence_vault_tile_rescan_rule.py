import pytest

from src.services import evidence_vault_tile_rescan_rule as rule
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER
from tests.test_evidence_vault_evidence_ledger import (
    ABOUT,
    _PROMISE,
    _SHIPPING,
    _about,
    _about_pair,
    _fingerprint,
    _ledger,
    _promise_row,
    _snapshot,
    _web,
    _words,
)


_SEEN_REF, _NEW_REF = "raw_inputs.0.chunk.1", "raw_inputs.0.chunk.2"
_PROMISE_PAIR = {"evidence_ref": _promise_row()["ref"], "evidence_fingerprint": _fingerprint(_PROMISE)}


def _pair(ref):
    return {"evidence_ref": ref, "evidence_fingerprint": _fingerprint(ref)}


def _support(ref, state, *reasons):
    return {**_pair(ref), "state": state, "reason_codes": list(reasons)}


def _shown(status, *reasons):
    return {"status": status, "reason_codes": list(reasons), "evidence_refs": []}


def _judgment(tile_id, state, *pairs, component="mission"):
    return {"tile_id": tile_id, "component_key": component, "assessment_state": state, "supporting_evidence": [*pairs]}


def _accepted_vector(lit=(), sentinels=("values",)):
    """A complete accepted vector: lit tiles cite the promise, the others are sin_evidencia."""

    judgments = [
        _judgment(tile["id"], "ok", _PROMISE_PAIR, component=key)
        if tile["id"] in lit
        else _judgment(tile["id"], "sin_evidencia", component=key)
        for key in PRESENTATION_ORDER
        if key not in sentinels
        for tile in COMPONENTS[key]["tiles"]
    ]
    return judgments, [{"component_key": key} for key in sentinels]


def _decide(accepted_state, supports, verdict=None, shown=None):
    return rule.decide_tile(accepted_state, supports, verdict, shown or {})


def _tiles(result):
    return {tile["tile_id"]: tile for tile in result["tile_decisions"]["tiles"]}


def _scores(result):
    return result["accepted_score"], result["would_be_score"], result["delta"], result["within_tolerance"]


@pytest.mark.parametrize(
    ("support", "cited", "reason"),
    [
        (_support(_SEEN_REF, "verified_absent"), _NEW_REF, "new_quote"),
        (_support(_SEEN_REF, "seen"), _SEEN_REF, "same_quote"),
    ],
)
def test_ok_verdict_keeps_a_lit_tile_whichever_quote_it_cites(support, cited, reason):
    decision = _decide("ok", [support], _judgment("M1", "ok", _pair(cited)))

    assert decision == {"decision": "keep_lit", "reason_codes": [reason]}


@pytest.mark.parametrize("state", ["no", "sin_evidencia"])
def test_core_no_over_seen_proof_it_was_shown_turns_a_lit_tile_off(state):
    verdict = _judgment("M1", state, _pair(_NEW_REF))

    decision = _decide("ok", [_support(_SEEN_REF, "seen")], verdict, {_SEEN_REF: _shown("shown")})

    assert decision == {"decision": "turn_off_core_no", "reason_codes": []}


@pytest.mark.parametrize(
    ("shown", "reason"),
    [
        (_shown("not_shown", "fragment_after_snippet_limit"), "not_shown_fragment_after_snippet_limit"),
        (_shown("shown_as_signal"), "not_shown_signal_only"),
    ],
)
def test_core_no_over_seen_proof_it_was_not_shown_is_a_b3s_failure(shown, reason):
    decision = _decide("ok", [_support(_SEEN_REF, "seen")], _judgment("M1", "no", _pair(_NEW_REF)), {_SEEN_REF: shown})

    assert decision == {"decision": "b3s_failure", "reason_codes": [reason]}


def test_lit_tile_whose_supports_are_all_verified_absent_turns_off_without_a_verdict():
    supports = [_support(_SEEN_REF, "verified_absent"), _support(_NEW_REF, "verified_absent")]

    assert _decide("ok", supports) == {"decision": "turn_off_proven", "reason_codes": ["component_not_evaluated"]}


def test_lit_tile_mixing_absent_and_unverified_supports_is_a_b3s_failure():
    supports = [_support(_SEEN_REF, "verified_absent"), _support(_NEW_REF, "not_verified", "page_not_visited")]

    assert _decide("ok", supports) == {
        "decision": "b3s_failure",
        "reason_codes": ["component_not_evaluated", "page_not_visited"],
    }


def test_lit_tile_with_seen_proof_and_no_verdict_stays_lit():
    supports = [_support(_SEEN_REF, "seen"), _support(_NEW_REF, "not_verified", "page_not_visited")]

    assert _decide("ok", supports) == {
        "decision": "keep_lit",
        "reason_codes": ["component_not_evaluated", "proof_seen_not_reevaluated"],
    }


def test_ok_verdict_lights_an_unlit_tile():
    decision = _decide("sin_evidencia", [], _judgment("M1", "ok", _pair(_NEW_REF)))

    assert decision == {"decision": "light", "reason_codes": []}


def test_unlit_tile_without_a_verdict_stays_unlit():
    decision = _decide("no", [_support(_SEEN_REF, "seen")])

    assert decision == {"decision": "keep_unlit", "reason_codes": ["component_not_evaluated"]}


def test_projection_that_keeps_every_tile_reproduces_the_accepted_score():
    snapshot = _snapshot(_web(_words(120, "h"), (ABOUT, _about())))
    rows = _ledger(snapshot, [_promise_row()], snapshot, [_promise_row()])["rows"]

    result = rule.project_rescan(*_accepted_vector({"M1", "MG1"}), rows, [])

    assert result["tile_decisions"]["counts"] == {"keep_lit": 2, "keep_unlit": 78}
    assert _scores(result) == (3, 3, 0, True)
    assert {tile["would_be_state"] for tile in _tiles(result).values() if tile["component_key"] == "values"} == {
        "not_detected"
    }


@pytest.mark.parametrize(("lit", "delta", "within"), [(4, -4, True), (5, -5, False)])
def test_proven_absences_move_the_score_against_a_four_point_tolerance(lit, delta, within):
    prior, current = _about_pair(_about(_SHIPPING))
    rows = _ledger(prior, [_promise_row()], current)["rows"]

    result = rule.project_rescan(*_accepted_vector({f"P{index}" for index in range(1, lit + 1)}), rows, [])

    assert result["tile_decisions"]["counts"]["turn_off_proven"] == lit
    assert _scores(result) == (lit, 0, delta, within)


def test_light_inside_a_not_detected_component_scores_that_component_again():
    verdict = _judgment("VA1", "ok", _PROMISE_PAIR, component="values")

    result = rule.project_rescan(*_accepted_vector(), [], [verdict])

    tiles = _tiles(result)
    assert [tiles[tile]["would_be_state"] for tile in ("VA1", "VA2")] == ["ok", "sin_evidencia"]
    assert (tiles["VA1"]["decision"], result["accepted_score"], result["would_be_score"]) == ("light", 0, 1)


def test_projection_is_unavailable_instead_of_scoring_an_incomplete_accepted_vector():
    judgments, sentinels = _accepted_vector({"M1"})

    result = rule.project_rescan(judgments[1:], sentinels, [], [])

    assert result == {
        "tile_decisions": {"reason_codes": ["accepted_vector_unavailable"], "counts": {}, "tiles": []},
        "would_be_score": None,
        "accepted_score": None,
        "delta": None,
        "within_tolerance": None,
    }
