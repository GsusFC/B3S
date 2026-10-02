"""Turn one Vault re-scan's per-tile decisions into an adoptable SV9 candidate of schema v3.

Every row is copied from a durable fact, never fabricated: the accepted candidate's
own row, or the row Core gave in one of the re-scan's evaluation checkpoints. The SV9
kernel scores the selected vector. Pure: no I/O and no LLM.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_tile_rescan_rule as rule
from src.services.evidence_vault_evidence_ledger import EVIDENCE_LEDGER_POLICY_VERSION
from src.services.evidence_vault_sv9_authoritative_relations import validate_evidence_vault_sv9_authoritative_relation_witness
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory


ELIGIBLE_REVIEW_REASONS = frozenset(
    "coverage_loss unmapped_evidence incomplete_review_partition review_set provider_failure "
    "incomplete_candidate evaluation_incomplete".split()
)
# Guard D1: a re-scan that proved this much accepted proof gone, or could not check it, keeps its review.
MAX_ABSENT_SHARE, MAX_UNVERIFIED_SHARE = 0.30, 0.50
HELD_WITHOUT_CORE_VERDICT = "held_without_core_verdict"
HELD_FOR_AUTHORITATIVE_RELATION = "held_for_authoritative_relation"
_FROM_CHECKPOINT = frozenset({rule.LIGHT, rule.TURN_OFF_CORE_NO, rule.TURN_OFF_PROVEN})
_CANDIDATE_VERSION, _PLAN_VERSION = "evidence-vault-sv9-judgment-candidate-v3", "evidence-vault-sv9-tile-rescan-plan-v1"
_LEDGER_ROWS_FINGERPRINT = "evidence-vault-sv9-tile-rescan-ledger-rows-fingerprint-v1"
_TELEMETRY = ("call_count", "calls_avoided", "reused_tile_count", "evaluated_tile_count")


def build_tile_rescan_candidate(
    *,
    outcome: Mapping[str, Any],
    accepted_candidate: Mapping[str, Any] | None,
    ledger_rows: Sequence[Mapping[str, Any]],
    current_judgments: Sequence[Mapping[str, Any]],
    witness: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the v3 candidate for one eligible re-scan, or say why there is none.

    ``outcome`` is the SV9 authority evaluation outcome and ``accepted_candidate`` the
    accepted authority's raw candidate payload with its ``id``. ``current_judgments``
    are the tile judgments in the re-scan's checkpoints and ``witness`` its current
    authoritative relation witness. The status is ``ineligible``, ``guarded`` (D1, with
    the shares in ``change_signal``), ``unavailable`` or ``candidate``.
    """

    reason = _ineligible(outcome, accepted_candidate)
    if reason:
        return _result("ineligible", [reason])
    witness = validate_evidence_vault_sv9_authoritative_relation_witness(witness)
    prior_rows, prior_sentinels = (accepted_candidate[key] for key in ("candidate_tile_judgments", "candidate_component_sentinels"))
    projection = rule.project_rescan(prior_rows, prior_sentinels, ledger_rows, current_judgments)
    signal, tiles = projection["change_signal"], projection["tile_decisions"]["tiles"]
    if not tiles:
        return _result("unavailable", projection["tile_decisions"]["reason_codes"], signal)
    if not _within_guard(signal):
        return _result("guarded", ["change_signal_guard"], signal)
    accepted = {row["tile_id"]: row for row in prior_rows}
    # Keyed as the rule keys them, so a tile gets the very row its decision saw.
    verdicts = {str(row["tile_id"]): row for row in current_judgments}
    relations: dict[str, set[tuple[str, str]]] = {}
    for relation in witness["authoritative_relations"]:
        relations.setdefault(relation["tile_id"], set()).add((relation["evidence_ref"], relation["evidence_fingerprint"]))
    rows, decisions, held = {}, [], []
    for tile in tiles:
        tile_id, codes = tile["tile_id"], list(tile["reason_codes"])
        fresh = tile["decision"] in _FROM_CHECKPOINT or (tile["decision"] == rule.KEEP_LIT and "new_quote" in codes)
        if tile["decision"] == rule.TURN_OFF_PROVEN and rule.NO_VERDICT in codes:
            # D2: proven absence with no Core verdict is ambiguous, so the accepted row holds.
            fresh = False
            codes.append(HELD_WITHOUT_CORE_VERDICT)
            held.append(tile_id)
        elif tile["decision"] == rule.KEEP_LIT and fresh and _drops_relation(verdicts[tile_id], relations.get(tile_id)):
            # Owner decision A: the tile stays lit either way, so it keeps the row that cites its relation.
            fresh = False
            codes.append(HELD_FOR_AUTHORITATIVE_RELATION)
        row = verdicts[tile_id] if fresh else accepted.get(tile_id)
        if row is not None:
            rows[tile_id] = row
        decisions.append({"tile_id": tile_id, "decision": tile["decision"], "reason_codes": codes})
    if any(_drops_relation(rows.get(tile_id), pairs) for tile_id, pairs in relations.items()):
        return _result("unavailable", ["authoritative_relation_dropped"], signal)
    # A tile of a not-detected component has no accepted row; its sentinel stands for it until one lights.
    components = {row["component_key"] for row in rows.values()}
    sentinels = {row["component_key"]: row for row in prior_sentinels if row["component_key"] not in components}
    try:
        candidate = _candidate(str(accepted_candidate["id"]), rows, sentinels, witness, ledger_rows, decisions, signal)
    except evaluation.IncrementalEvaluationError:
        return _result("unavailable", ["candidate_partition_invalid"], signal)
    return _result("candidate", [], signal, candidate, held)


def _ineligible(outcome: Mapping[str, Any], accepted: Mapping[str, Any] | None) -> str | None:
    authority, reasons = outcome.get("accepted_authority"), outcome.get("reason_codes")
    accepted_id = authority.get("accepted_candidate_id") if isinstance(authority, Mapping) else None
    if accepted_id is None or not isinstance(accepted, Mapping) or accepted_id != accepted.get("id"):
        return "no_accepted_authority"
    if type(reasons) is not list or not reasons or not set(reasons) <= ELIGIBLE_REVIEW_REASONS:
        return "outcome_not_eligible"
    status = outcome.get("status")
    eligible = status == "review_required" or (status == "no_new_score" and "provider_failure" in reasons)
    return None if eligible else "outcome_not_eligible"


def _drops_relation(row: Mapping[str, Any] | None, pairs: set[tuple[str, str]] | None) -> bool:
    """The append guard's test (repository ``_sv9_judgment_accepted_result_authority``): a tile's
    authoritative relation pairs must all stay in its row's supporting evidence."""

    supports = {(item["evidence_ref"], item["evidence_fingerprint"]) for item in (row or {}).get("supporting_evidence", [])}
    return bool(pairs) and not pairs <= supports


def _within_guard(signal: Mapping[str, Any]) -> bool:
    absent, unverified = signal["absent_share"], signal["unverified_share"]
    # Without an accepted lit tile there is no share to measure, so the guard cannot clear the re-scan.
    return absent is not None and absent <= MAX_ABSENT_SHARE and unverified <= MAX_UNVERIFIED_SHARE


def _candidate(
    prior_id: str, rows: Mapping[str, Any], sentinels: Mapping[str, Any], witness: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]], decisions: list[dict[str, Any]], signal: Mapping[str, Any],
) -> dict[str, Any]:
    """The v3 payload exactly as the repository's tile re-scan envelope rebuilds it from its rows."""

    first = next(iter([*rows.values(), *sentinels.values()]))
    series, current = first["series_contract"], first["series_fingerprint"]
    plan_input = {"current_series_fingerprint": current, "prior_judgments": [], "prior_component_sentinels": []}
    assessment, tiles, blocks = evaluation._assessment(plan_input, set(), rows, sentinels)
    plan = {"schema_version": _PLAN_VERSION, "kind": "apply", "prior_candidate_id": prior_id, "current_series_contract": series}
    identity = memory.canonical_fingerprint("evidence-vault-sv9-tile-rescan-plan-fingerprint-v1", plan)
    ledger = sorted(ledger_rows, key=lambda row: (row["evidence_ref"], row["evidence_fingerprint"]))
    judgments = {"candidate_tile_judgments": tiles, "candidate_component_sentinels": blocks}
    bundle = {"canonical_plan_fingerprint": identity, "evaluations": []}
    candidate = {
        "schema_version": _CANDIDATE_VERSION,
        "plan": plan,
        "canonical_plan_fingerprint": identity,
        "current_series_fingerprint": current,
        "candidate_series_fingerprint": planner._candidate(current, [current]),
        "component_evaluations": [],
        "evidence_bindings": [],
        **judgments,
        "assessment": assessment,
        "telemetry": dict.fromkeys(_TELEMETRY, 0),
        "evaluation_bundle_fingerprint": memory.canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", bundle),
        "assessment_fingerprint": assessment["assessment_fingerprint"],
        "score_fingerprint": assessment["score_fingerprint"],
        "authoritative_relation_witness": witness,
        "tile_rescan": {
            "kind": "apply",
            "rule_version": rule.RULE_VERSION,
            "ledger_policy_version": EVIDENCE_LEDGER_POLICY_VERSION,
            "prior_candidate_id": prior_id,
            "ledger_rows_fingerprint": memory.canonical_fingerprint(_LEDGER_ROWS_FINGERPRINT, ledger),
            "judgments_fingerprint": memory.canonical_fingerprint("evidence-vault-sv9-tile-rescan-judgments-fingerprint-v1", judgments),
            "decisions": decisions,
            "change_signal": dict(signal),
            "guard": {"max_absent_share": MAX_ABSENT_SHARE, "max_unverified_share": MAX_UNVERIFIED_SHARE},
        },
    }
    candidate["complete_record_fingerprint"] = authority_event.candidate_complete_record_fingerprint(candidate)
    return candidate


def _result(
    status: str, reasons: list[str], signal: Mapping[str, Any] | None = None, candidate: dict[str, Any] | None = None,
    held: Sequence[str] = (),
) -> dict[str, Any]:
    result = {"status": status, "reason_codes": reasons, "change_signal": signal, "candidate": candidate}
    return result | {"ambiguous_tile_ids": list(held)}
