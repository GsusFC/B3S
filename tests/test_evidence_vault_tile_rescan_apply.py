from collections import Counter

import pytest

from src.history import repository as history
from src.services import evidence_vault_sv9_authoritative_relations as relations
from src.services import evidence_vault_tile_rescan_apply as apply
from src.services import evidence_vault_tile_rescan_rule as rule
from src.sv9 import incremental_evaluation as ie
from src.sv9 import incremental_planner as ip
from src.sv9 import judgment_memory as jm
from src.sv9.rubric import PRESENTATION_ORDER
from tests.test_evidence_vault_sv9_authoritative_relations import _facts, _id, _project, _sha
from tests.test_sv9_judgment_memory import _origin, _series


_ID = "00000000-0000-0000-0000-000000000009"
# PR1's fixture witness: one authoritative relation, on M1.
_WITNESS = relations.build_evidence_vault_sv9_authoritative_relation_witness(
    source_scan_id="scan-1", projection=_project(_facts())[0]
)
# A re-scan's witness once the accepted rebuild leaves M1's relation out as a doubt.
_NO_RELATIONS = relations.build_evidence_vault_sv9_authoritative_relation_witness(
    source_scan_id="scan-1", projection=_project(_facts(count=0))[0],
    capture_origin={"capture_id": _id("capture"), "capture_fingerprint": _sha("capture")},
    operation_origin={"operation_id": _id("operation"), "operation_fingerprint": _sha("operation")},
)
_RELATION = {key: _WITNESS["authoritative_relations"][0][key] for key in ("evidence_ref", "evidence_fingerprint")}
_NOT_SHOWN = {"status": "not_shown", "reason_codes": ["component_not_evaluated"], "evidence_refs": []}


def _pair(name):
    return {"evidence_ref": f"e:{name}", "evidence_fingerprint": jm.canonical_fingerprint("test-pair", name)}


def _judgment(tile, state="ok", *pairs, capture=1):
    """A signed tile judgment: capture 1 is the accepted scan, capture 2 the re-scan's checkpoint."""

    if state != "sin_evidencia" and not pairs:
        pairs = (_RELATION if tile == "M1" else _pair(tile),)
    return jm.build_tile_judgment(
        tile_id=tile, component_key=dict(ip._REGISTRY)[tile], assessment_state=state, supporting_evidence=list(pairs),
        capture_origin=_origin("capture", capture), operation_origin=_origin("operation", capture),
        series_contract=_series(), authority_state="pending", review_state="none", lifecycle_state="active", lifecycle_reason="",
    )


def _accepted(unlit=(), not_detected=()):
    rows = [_judgment(tile, "sin_evidencia" if tile in unlit else "ok") for tile, component in ip._REGISTRY if component not in not_detected]
    sentinels = [
        ip.build_component_not_detected_sentinel(
            component_key=component, status="not_detected", supporting_evidence=[], capture_origin=_origin("capture", 1),
            operation_origin=_origin("operation", 1), series_contract=_series(), authority_state="pending",
            review_state="none", lifecycle_state="active", lifecycle_reason="",
        )
        for component in not_detected
    ]
    return {"id": _ID, "candidate_tile_judgments": rows, "candidate_component_sentinels": sentinels, "authoritative_relation_witness": _WITNESS}


def _uncited(state="ok", *pairs):
    """An accepted candidate whose M1 row leaves its reviewed relation uncited, as the authorities adopted before the 2026-09-11 citation check do."""

    accepted = _accepted()
    m1 = _judgment("M1", state, *(pairs or ((_pair("M1"),) if state != "sin_evidencia" else ())))
    accepted["candidate_tile_judgments"] = [m1 if row["tile_id"] == "M1" else row for row in accepted["candidate_tile_judgments"]]
    return accepted


def _ledger(accepted, states=None, shown=()):
    """One ledger row per accepted support: seen unless ``states`` names its tile, shown to Core for ``shown`` tiles."""

    rows = {}
    for row in accepted["candidate_tile_judgments"]:
        for item in row["supporting_evidence"]:
            entries = dict.fromkeys(PRESENTATION_ORDER, _NOT_SHOWN)
            if row["tile_id"] in shown:
                entries[row["component_key"]] = {"status": "shown", "reason_codes": [], "evidence_refs": [item["evidence_ref"]]}
            state = (states or {}).get(row["tile_id"], "seen")
            rows[item["evidence_ref"]] = {**item, "state": state, "reason_codes": [], "shown_to_core": entries}
    return list(rows.values())


_GUARD = {"core_plan": {"proof_gone_components": [], "would_call": [], "actual_calls": []}, "failed_components": []}


def _build(accepted, verdicts=(), *, ledger=None, witness=_WITNESS):
    """Build, and hold every candidate to PR1's v3 envelope, the append guard and rows copied from durable facts."""

    result = apply.build_tile_rescan_candidate(
        accepted_candidate=accepted, ledger_rows=_ledger(accepted) if ledger is None else ledger,
        current_judgments=list(verdicts), witness=witness, guard=_GUARD,
    )
    if result["status"] == "candidate":
        candidate = result["candidate"]
        assert history._sv9_judgment_candidate_envelope(candidate) == candidate
        history._sv9_tile_rescan_continuity({"candidate": accepted}, candidate)
        sources = {jm.canonical_json(row) for row in [*accepted["candidate_tile_judgments"], *accepted["candidate_component_sentinels"], *verdicts]}
        assert all(jm.canonical_json(row) in sources for row in [*candidate["candidate_tile_judgments"], *candidate["candidate_component_sentinels"]])
    return result


def _tile(rows, tile_id):
    [row] = [row for row in rows if row["tile_id"] == tile_id]
    return row


def _score(rows):
    plan = {"current_series_fingerprint": rows[0]["series_fingerprint"], "prior_judgments": [], "prior_component_sentinels": []}
    return ie._assessment(plan, set(), {row["tile_id"]: row for row in rows}, {})[0]["sv9_score"]


def test_unverified_or_absent_proof_never_blocks_and_keeps_the_accepted_rows():
    accepted = _accepted()
    # No ledger row: every lit tile's proof is unverified. All verified absent: proof gone without a Core call.
    absent = _ledger(accepted, dict.fromkeys((row["tile_id"] for row in accepted["candidate_tile_judgments"]), "verified_absent"))

    for result, share in ((_build(accepted, ledger=[]), "unverified_share"), (_build(accepted, ledger=absent), "absent_share")):
        assert (result["status"], result["change_signal"][share]) == ("candidate", 1.0)
        assert result["candidate"]["candidate_tile_judgments"] == accepted["candidate_tile_judgments"]
        assert {row["decision"] for row in result["candidate"]["tile_rescan"]["decisions"]} == {"b3s_failure"}
        assert result["candidate"]["tile_rescan"]["guard"] == _GUARD


_NEW = _pair("new")


@pytest.mark.parametrize(
    ("accepted_state", "ledger_state", "shown", "verdict", "decision", "source"),
    [
        pytest.param("ok", "seen", (), ("ok", _pair("V1")), "keep_lit", "accepted", id="keep_lit_same_quote"),
        pytest.param("ok", "seen", (), ("ok", _NEW), "keep_lit", "checkpoint", id="keep_lit_new_quote"),
        pytest.param("ok", "seen", (), None, "keep_lit", "accepted", id="keep_lit_not_reevaluated"),
        pytest.param("sin_evidencia", "seen", (), ("sin_evidencia",), "keep_unlit", "accepted", id="keep_unlit"),
        pytest.param("ok", "not_verified", (), None, "b3s_failure", "accepted", id="b3s_failure"),
        pytest.param("sin_evidencia", "seen", (), ("ok", _NEW), "light", "checkpoint", id="light"),
        pytest.param("ok", "seen", ("V1",), ("no", _NEW), "keep_lit", "accepted", id="keep_lit_doubt"),
        pytest.param("ok", "verified_absent", (), ("no", _NEW), "turn_off_proven", "checkpoint", id="turn_off_proven_core_verdict"),
        pytest.param("ok", "verified_absent", (), None, "b3s_failure", "accepted", id="b3s_failure_core_not_called"),
    ],
)
def test_each_decision_copies_the_row_its_table_names(accepted_state, ledger_state, shown, verdict, decision, source):
    accepted = _accepted(unlit={"V1"} if accepted_state == "sin_evidencia" else ())
    checkpoint = [] if verdict is None else [_judgment("V1", *verdict, capture=2)]

    result = _build(accepted, checkpoint, ledger=_ledger(accepted, {"V1": ledger_state}, shown))

    made = _tile(result["candidate"]["tile_rescan"]["decisions"], "V1")
    expected = checkpoint[0] if source == "checkpoint" else _tile(accepted["candidate_tile_judgments"], "V1")
    assert made["decision"] == decision and _tile(result["candidate"]["candidate_tile_judgments"], "V1") == expected


@pytest.mark.parametrize(("pairs", "source"), [((_NEW,), "accepted"), ((_RELATION, _NEW), "checkpoint")], ids=["drops_relation", "keeps_relation"])
def test_a_new_quote_keeps_the_accepted_row_only_when_it_drops_the_authoritative_relation(pairs, source):
    accepted = _accepted()
    checkpoint = _judgment("M1", "ok", *pairs, capture=2)

    result = _build(accepted, [checkpoint])

    made = _tile(result["candidate"]["tile_rescan"]["decisions"], "M1")
    expected = checkpoint if source == "checkpoint" else _tile(accepted["candidate_tile_judgments"], "M1")
    assert made["decision"] == "keep_lit" and made["reason_codes"][0] == "new_quote"
    assert ("held_for_authoritative_relation" in made["reason_codes"]) == (source == "accepted")
    assert _tile(result["candidate"]["candidate_tile_judgments"], "M1") == expected
    assert result["candidate"]["assessment"]["sv9_score"] == _score(accepted["candidate_tile_judgments"])


def test_a_turn_off_that_drops_the_authoritative_relation_stays_lit_as_a_doubt():
    accepted = _accepted()

    result = _build(accepted, [_judgment("M1", "no", _NEW, capture=2)], ledger=_ledger(accepted, {"M1": "verified_absent"}))

    made = _tile(result["candidate"]["tile_rescan"]["decisions"], "M1")
    assert (made["decision"], made["reason_codes"]) == ("keep_lit", ["doubt", "turn_off_proven", "held_for_authoritative_relation"])
    assert _tile(result["candidate"]["candidate_tile_judgments"], "M1") == _tile(accepted["candidate_tile_judgments"], "M1")
    assert result["candidate"]["assessment"]["sv9_score"] == _score(accepted["candidate_tile_judgments"])


def test_a_turn_off_of_a_tile_without_a_reviewed_relation_takes_cores_row():
    accepted = _accepted()
    verdict = _judgment("V1", "no", _NEW, capture=2)

    result = _build(accepted, [verdict], ledger=_ledger(accepted, {"V1": "verified_absent"}))

    assert _tile(result["candidate"]["tile_rescan"]["decisions"], "V1")["decision"] == "turn_off_proven"
    assert _tile(result["candidate"]["candidate_tile_judgments"], "V1") == verdict


@pytest.mark.parametrize(
    ("verdict", "ledger_state", "codes", "source"),
    [
        pytest.param(None, "seen", ["component_not_evaluated", "proof_seen_not_reevaluated"], "accepted", id="not_reevaluated"),
        pytest.param(("ok", _NEW), "seen", ["new_quote"], "checkpoint", id="new_quote"),
        pytest.param(("no", _NEW), "verified_absent", ["doubt", "turn_off_proven", "held_for_authoritative_relation"], "accepted", id="turn_off"),
    ],
)
def test_a_reviewed_relation_its_accepted_row_never_cited_binds_while_the_tile_stays_lit(verdict, ledger_state, codes, source):
    accepted = _uncited()
    checkpoint = [] if verdict is None else [_judgment("M1", *verdict, capture=2)]

    result = _build(accepted, checkpoint, ledger=_ledger(accepted, {"M1": ledger_state}))

    made = _tile(result["candidate"]["tile_rescan"]["decisions"], "M1")
    expected = checkpoint[0] if source == "checkpoint" else _tile(accepted["candidate_tile_judgments"], "M1")
    assert (made["decision"], made["reason_codes"]) == ("keep_lit", codes)
    assert _tile(result["candidate"]["candidate_tile_judgments"], "M1") == expected


@pytest.mark.parametrize("state", ["no", "sin_evidencia"])
def test_a_reviewed_relation_its_accepted_row_never_cited_on_an_unlit_tile_is_a_doubt(state):
    accepted = _uncited(state)

    # The accepted rebuild leaves the doubt out, so the re-scan's witness holds no relation.
    result = _build(accepted, witness=_NO_RELATIONS)

    assert apply.split_reviewed_relations(accepted) == ([], _WITNESS["authoritative_relations"])
    made = _tile(result["candidate"]["tile_rescan"]["decisions"], "M1")
    assert (made["decision"], made["reason_codes"]) == ("keep_unlit", ["component_not_evaluated", "doubt", "held_for_authoritative_relation"])
    assert _tile(result["candidate"]["candidate_tile_judgments"], "M1") == _tile(accepted["candidate_tile_judgments"], "M1")


@pytest.mark.parametrize(("accepted", "kept"), [(_accepted(), False), (_uncited(), True)], ids=["cited_pair", "uncited_pair"])
def test_the_append_guard_never_lets_a_cited_reviewed_pair_go(accepted, kept):
    # Core's new quote for M1 leaves the reviewed pair out, and the v3 takes Core's row anyway.
    rows = [_judgment("M1", "ok", _NEW, capture=2) if row["tile_id"] == "M1" else row for row in accepted["candidate_tile_judgments"]]
    candidate = {"authoritative_relation_witness": _WITNESS, "candidate_tile_judgments": rows}

    if kept:
        history._sv9_tile_rescan_continuity({"candidate": accepted}, candidate)
        return
    with pytest.raises(history.EvidenceVaultSv9JudgmentCandidateError, match="drops a reviewed relation"):
        history._sv9_tile_rescan_continuity({"candidate": accepted}, candidate)


def test_a_not_detected_component_keeps_its_sentinel_unless_one_of_its_tiles_lights():
    accepted = _accepted(not_detected=("values",))

    kept, lit = _build(accepted), _build(accepted, [_judgment("VA1", "ok", _NEW, capture=2)])

    assert kept["candidate"]["candidate_component_sentinels"] == accepted["candidate_component_sentinels"]
    assert (lit["status"], lit["reason_codes"]) == ("unavailable", ["candidate_partition_invalid"])


def test_a_primary_like_rescan_keeps_failed_components_and_takes_new_quotes_at_the_same_score():
    unlit = {"M5", "V4", "V5", "VA5", "A4", "A5", "P8", "P9", "P10", "PE7", "PE8", "PE9", "PE10", "MG8", "MG9", "MG10", "I9", "I10", "PR9", "PR10", "C9", "C10"}
    new_quotes = {"MG1", "MG3", "MG5", "MG6", "PE1"}
    accepted = _accepted(unlit)
    # Attributes and value_proposition failed binding: no checkpoint, and four of their supports went unchecked.
    ledger = _ledger(accepted, dict.fromkeys(("A1", "A2", "A3", "P7"), "not_verified"))
    checkpoint = {
        tile: _judgment(tile, "sin_evidencia" if tile in unlit else "ok", *([_pair(f"{tile}:new")] if tile in new_quotes else []), capture=2)
        for tile in (*ip._COMPONENT_TILES["magnetism"], *ip._COMPONENT_TILES["personality"])
    }

    result = _build(accepted, list(checkpoint.values()), ledger=ledger)

    rows, prior = (
        {row["tile_id"]: row for row in rows} for rows in (result["candidate"]["candidate_tile_judgments"], accepted["candidate_tile_judgments"])
    )
    assert Counter(row["decision"] for row in result["candidate"]["tile_rescan"]["decisions"]) == {"keep_lit": 54, "keep_unlit": 22, "b3s_failure": 4}
    assert {tile for tile in rows if rows[tile] != prior[tile]} == new_quotes and all(rows[tile] == checkpoint[tile] for tile in new_quotes)
    assert result["candidate"]["assessment"]["sv9_score"] == _score(accepted["candidate_tile_judgments"])
