from copy import deepcopy

import pytest

from src.services import evidence_vault_sv9_first_light as first_light
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory
from src.services.evidence_vault_canonical_core import canonical_fingerprint
from tests.test_sv9_judgment_memory import _hash, _origin, _series


def _evidence(ref: str) -> dict[str, str]:
    return {
        "evidence_ref": ref,
        "evidence_fingerprint": memory.canonical_fingerprint("test-evidence", ref),
    }


def _fingerprint(ref: str) -> str:
    return _evidence(ref)["evidence_fingerprint"]


def _tile(tile_id: str, state: str, evidence=(), *, authority: str) -> dict:
    return memory.build_tile_judgment(
        tile_id=tile_id,
        component_key=dict(planner._REGISTRY)[tile_id],
        assessment_state=state,
        supporting_evidence=list(evidence),
        capture_origin=_origin("capture", 1),
        operation_origin=_origin("operation", 2),
        series_contract=_series(),
        authority_state=authority,
        review_state="resolved" if authority == "accepted" else "none",
        lifecycle_state="active",
        lifecycle_reason="",
    )


def _snapshot(*, candidate: bool, a_state="sin_evidencia", b_state="ok") -> dict:
    authority = "pending" if candidate else "accepted"
    review = "none" if candidate else "resolved"
    rows = []
    for tile_id, _component in planner._REGISTRY:
        if tile_id == "M1":
            rows.append(_tile(tile_id, a_state, [_evidence("evidence:a")] if candidate and a_state == "ok" else [], authority=authority))
        elif tile_id == "M2":
            rows.append(_tile(tile_id, b_state, [_evidence("evidence:b")] if b_state in {"ok", "no"} else [], authority=authority))
        else:
            rows.append(_tile(tile_id, "sin_evidencia", [], authority=authority))
    for row in rows:
        row["review_state"] = review
        row["canonical_judgment_fingerprint"] = memory.canonical_fingerprint(
            "sv9-tile-judgment-fingerprint-v1", {key: value for key, value in row.items() if key != "canonical_judgment_fingerprint"}
        )
    evidence = []
    for row in rows:
        for binding in row["supporting_evidence"]:
            evidence.append(
                {
                    **binding,
                    "capture_origin": deepcopy(row["capture_origin"]),
                    "operation_origin": deepcopy(row["operation_origin"]),
                    "provenance": "evaluated",
                }
            )
    value = {"tiles": rows, "evidence": evidence}
    value["snapshot_fingerprint"] = canonical_fingerprint(
        "sv9-first-light-snapshot-v1", value
    )
    return value


def _resign_snapshot(snapshot: dict) -> None:
    for row in snapshot["tiles"]:
        row["canonical_judgment_fingerprint"] = memory.canonical_fingerprint(
            "sv9-tile-judgment-fingerprint-v1",
            {key: value for key, value in row.items() if key != "canonical_judgment_fingerprint"},
        )
    snapshot["snapshot_fingerprint"] = canonical_fingerprint(
        "sv9-first-light-snapshot-v1",
        {"tiles": snapshot["tiles"], "evidence": snapshot["evidence"]},
    )


def _frozen(*rows, provenance="evaluated"):
    return [
        {
            **_evidence(ref),
            "capture_origin": _origin("capture", 1),
            "operation_origin": _origin("operation", 2),
            "provenance": provenance,
        }
        for ref in rows
    ]


def test_first_light_builds_non_authoritative_candidate_from_frozen_rescan():
    accepted = _snapshot(candidate=False, b_state="ok")
    candidate = _snapshot(candidate=True, a_state="ok", b_state="no")
    before = deepcopy(accepted)

    result = first_light.evaluate_first_light_rescan(
        accepted_snapshot=accepted,
        rescan_candidate=candidate,
        frozen_evidence=_frozen("evidence:a", "evidence:b"),
    )

    assert result["status"] == "candidate_ready"
    assert result["eligible_tile_ids"] == ["M1"]
    assert result["review_tile_ids"] == ["M2"]
    assert accepted == before
    assert result["accepted_snapshot"] == before
    effective = {row["tile_id"]: row for row in result["candidate_report"]["tiles"]}
    assert effective["M1"]["assessment_state"] == "ok"
    assert effective["M2"]["assessment_state"] == "ok"
    assert result["candidate_report"]["assessment"]["sv9_score"] == 2
    assert result["candidate_report"]["authority"] is False
    assert result["candidate_report"]["publication"] == "not_published"
    assert result["evidence_bindings"] == [
        {
            "tile_id": "M1",
            "evidence_ref": "evidence:a",
            "evidence_fingerprint": _fingerprint("evidence:a"),
        }
    ]


def test_first_light_rejects_frozen_evidence_origin_mismatch():
    frozen = _frozen("evidence:a")
    frozen[0]["capture_origin"] = _origin("capture", 3)

    with pytest.raises(first_light.FirstLightEvaluationError, match="origin mismatch"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=_snapshot(candidate=False),
            rescan_candidate=_snapshot(candidate=True, a_state="ok"),
            frozen_evidence=frozen,
        )


def test_first_light_rejects_snapshot_and_frozen_binding_mismatches():
    accepted = _snapshot(candidate=False)
    candidate = _snapshot(candidate=True, a_state="ok")
    candidate["tiles"][0]["assessment_state"] = "no"
    with pytest.raises(first_light.FirstLightEvaluationError, match="snapshot fingerprint mismatch"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=accepted, rescan_candidate=candidate,
            frozen_evidence=_frozen("evidence:a"),
        )

    frozen = _frozen("evidence:a")
    frozen[0]["evidence_fingerprint"] = _fingerprint("other")
    with pytest.raises(first_light.FirstLightEvaluationError, match="evidence binding"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=accepted,
            rescan_candidate=_snapshot(candidate=True, a_state="ok"),
            frozen_evidence=frozen,
        )


def test_first_light_rejects_mixed_sv9_series_before_reconciliation():
    accepted = _snapshot(candidate=False)
    candidate = _snapshot(candidate=True, a_state="ok")
    changed_series = _series(prompt_version="prompt-v2")
    for row in candidate["tiles"]:
        row["series_contract"] = deepcopy(changed_series)
        row["series_fingerprint"] = memory.canonical_fingerprint(
            "sv9-judgment-series-fingerprint-v1", changed_series
        )
    _resign_snapshot(candidate)

    with pytest.raises(
        first_light.FirstLightEvaluationError,
        match="SV9 series contract/fingerprint mismatch; candidate is non-authoritative and must not mix series",
    ):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=accepted,
            rescan_candidate=candidate,
            frozen_evidence=_frozen("evidence:a", "evidence:b"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("review_state", "required"), ("review_state", "in_review"),
     ("lifecycle_state", "reopened"), ("authority_state", "pending")],
)
def test_first_light_rejects_non_active_accepted_snapshot_rows(field, value):
    accepted = _snapshot(candidate=False)
    accepted["tiles"][0][field] = value
    if field == "lifecycle_state":
        accepted["tiles"][0]["lifecycle_reason"] = "reopened for review"
    _resign_snapshot(accepted)

    with pytest.raises(first_light.FirstLightEvaluationError, match="accepted snapshot"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=accepted,
            rescan_candidate=_snapshot(candidate=True, a_state="ok"),
            frozen_evidence=_frozen("evidence:a"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("review_state", state) for state in ("required", "in_review", "resolved")]
    + [("lifecycle_state", state) for state in ("reopened", "superseded")],
)
def test_first_light_rejects_reviewed_or_inactive_pending_candidate(field, value):
    candidate = _snapshot(candidate=True, a_state="ok", b_state="sin_evidencia")
    candidate["tiles"][0][field] = value
    if field == "lifecycle_state":
        candidate["tiles"][0]["lifecycle_reason"] = "pending human review"
    _resign_snapshot(candidate)

    with pytest.raises(first_light.FirstLightEvaluationError, match="candidate.*review|candidate.*active"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=_snapshot(candidate=False, b_state="sin_evidencia"),
            rescan_candidate=candidate,
            frozen_evidence=_frozen("evidence:a"),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda evidence: evidence.clear(),
        lambda evidence: evidence.extend(_frozen("evidence:extra")),
        lambda evidence: evidence.append(deepcopy(evidence[0])),
        lambda evidence: evidence[0].update(provenance="shadow"),
        lambda evidence: evidence[0].update(provenance="preview"),
        lambda evidence: evidence[0].update(authority=False),
        lambda evidence: evidence[0].update(lifecycle_state="reopened"),
        lambda evidence: evidence[0].update(state="shadow"),
        lambda evidence: evidence[0].update(evidence_fingerprint=_fingerprint("other")),
        lambda evidence: evidence[0].update(capture_origin=_origin("capture", 3)),
    ],
)
def test_first_light_rejects_candidate_inventory_not_identical_to_frozen_support(mutation):
    candidate = _snapshot(candidate=True, a_state="ok", b_state="sin_evidencia")
    mutation(candidate["evidence"])
    _resign_snapshot(candidate)

    with pytest.raises(first_light.FirstLightEvaluationError, match="candidate.*evidence"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=_snapshot(candidate=False, b_state="sin_evidencia"),
            rescan_candidate=candidate,
            frozen_evidence=_frozen("evidence:a"),
        )


def test_first_light_rejects_accepted_snapshot_with_pending_review_overlay():
    accepted = _snapshot(candidate=False)
    accepted["reopen_review_overlay"] = {"status": "pending"}

    with pytest.raises(first_light.FirstLightEvaluationError, match="snapshot fields"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=accepted,
            rescan_candidate=_snapshot(candidate=True, a_state="ok"),
            frozen_evidence=_frozen("evidence:a"),
        )


def test_first_light_rejects_unbound_accepted_snapshot_evidence_before_scoring():
    accepted = _snapshot(candidate=False, b_state="ok")
    accepted["evidence"] = []
    _resign_snapshot(accepted)

    with pytest.raises(first_light.FirstLightEvaluationError, match="accepted snapshot evidence"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=accepted,
            rescan_candidate=_snapshot(candidate=True, a_state="ok"),
            frozen_evidence=_frozen("evidence:a"),
        )


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(evidence_fingerprint="not-a-fingerprint"),
    lambda row: row.update(operation_origin=_origin("operation", 99)),
])
def test_first_light_rejects_invalid_accepted_snapshot_evidence_inventory(mutation):
    accepted = _snapshot(candidate=False, b_state="ok")
    mutation(accepted["evidence"][0])
    _resign_snapshot(accepted)

    with pytest.raises(first_light.FirstLightEvaluationError, match="evidence"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=accepted,
            rescan_candidate=_snapshot(candidate=True, a_state="ok"),
            frozen_evidence=_frozen("evidence:a"),
        )


def test_first_light_rejects_non_string_frozen_evidence_ref_without_coercion():
    accepted = _snapshot(candidate=False)
    candidate = _snapshot(candidate=True, a_state="ok")
    candidate["tiles"][0]["supporting_evidence"][0]["evidence_ref"] = "123"
    _resign_snapshot(candidate)
    frozen = _frozen("evidence:a")
    frozen[0]["evidence_ref"] = 123

    with pytest.raises(first_light.FirstLightEvaluationError, match="frozen evidence binding"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=accepted,
            rescan_candidate=candidate,
            frozen_evidence=frozen,
        )


def test_first_light_rejects_candidate_supplied_score_instead_of_overriding_kernel():
    candidate = _snapshot(candidate=True, a_state="ok")
    candidate["assessment"] = {"sv9_score": 100}
    with pytest.raises(first_light.FirstLightEvaluationError, match="snapshot fields"):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=_snapshot(candidate=False),
            rescan_candidate=candidate,
            frozen_evidence=_frozen("evidence:a"),
        )


@pytest.mark.parametrize(
    ("frozen", "candidate_mutation", "message"),
    [
        (_frozen("evidence:a", provenance="shadow"), {}, "shadow"),
        (_frozen("evidence:missing"), {}, "missing"),
        (_frozen("evidence:a", "evidence:a"), {}, "ambiguous"),
        (_frozen("evidence:a"), {"evidence_fingerprint": _fingerprint("wrong")}, "binding"),
    ],
)
def test_first_light_fails_closed_for_invalid_evidence(
    frozen, candidate_mutation, message
):
    accepted = _snapshot(candidate=False)
    candidate = _snapshot(candidate=True, a_state="ok")
    if candidate_mutation:
        candidate["tiles"][0]["supporting_evidence"][0].update(candidate_mutation)
        candidate["tiles"][0]["canonical_judgment_fingerprint"] = memory.canonical_fingerprint(
            "sv9-tile-judgment-fingerprint-v1",
            {key: value for key, value in candidate["tiles"][0].items() if key != "canonical_judgment_fingerprint"},
        )
        candidate["snapshot_fingerprint"] = canonical_fingerprint(
            "sv9-first-light-snapshot-v1",
            {"tiles": candidate["tiles"], "evidence": candidate["evidence"]},
        )

    with pytest.raises(first_light.FirstLightEvaluationError, match=message):
        first_light.evaluate_first_light_rescan(
            accepted_snapshot=accepted,
            rescan_candidate=candidate,
            frozen_evidence=frozen,
        )
