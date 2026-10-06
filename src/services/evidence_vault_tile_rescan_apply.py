"""Turn one Vault re-scan's per-tile decisions into an adoptable SV9 candidate of schema v3.

Every row is copied from a durable fact, never fabricated: the accepted candidate's
own row, or the row Core gave in one of the re-scan's evaluation checkpoints. The SV9
kernel scores the selected vector. Pure: no I/O and no LLM.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_tile_rescan_rule as rule
from src.services.evidence_vault_evidence_ledger import EVIDENCE_LEDGER_POLICY_VERSION
from src.services.evidence_vault_sv9_authoritative_relations import validate_evidence_vault_sv9_authoritative_relation_witness
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory


HELD_FOR_AUTHORITATIVE_RELATION = "held_for_authoritative_relation"
_FROM_CHECKPOINT = frozenset({rule.LIGHT, rule.TURN_OFF_PROVEN})
_CANDIDATE_VERSION, _PLAN_VERSION = "evidence-vault-sv9-judgment-candidate-v3", "evidence-vault-sv9-tile-rescan-plan-v1"
_LEDGER_ROWS_FINGERPRINT = "evidence-vault-sv9-tile-rescan-ledger-rows-fingerprint-v1"
_TELEMETRY = ("call_count", "calls_avoided", "reused_tile_count", "evaluated_tile_count")


def build_tile_rescan_candidate(
    *,
    accepted_candidate: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
    current_judgments: Sequence[Mapping[str, Any]],
    witness: Mapping[str, Any],
    guard: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the v3 candidate for one re-scan, or say why there is none.

    ``accepted_candidate`` is the accepted authority's raw candidate payload with its
    ``id``. ``current_judgments`` are the tile judgments in the re-scan's checkpoints
    and ``witness`` its current authoritative relation witness. ``guard`` is recorded
    as is in ``tile_rescan.guard``. The status is ``unavailable`` or ``candidate``.
    """

    witness = validate_evidence_vault_sv9_authoritative_relation_witness(witness)
    prior_rows, prior_sentinels = (accepted_candidate[key] for key in ("candidate_tile_judgments", "candidate_component_sentinels"))
    projection = rule.project_rescan(prior_rows, prior_sentinels, ledger_rows, current_judgments)
    signal, tiles = projection["change_signal"], projection["tile_decisions"]["tiles"]
    if not tiles:
        return _result("unavailable", projection["tile_decisions"]["reason_codes"], signal)
    accepted = {row["tile_id"]: row for row in prior_rows}
    doubted = {row["tile_id"] for row in split_reviewed_relations(accepted_candidate)[1]}
    # Keyed as the rule keys them, so a tile gets the very row its decision saw.
    verdicts = {str(row["tile_id"]): row for row in current_judgments}
    relations: dict[str, list[dict[str, Any]]] = {}
    for relation in witness["authoritative_relations"]:
        relations.setdefault(relation["tile_id"], []).append(relation)
    rows, decisions = {}, []
    for tile in tiles:
        tile_id, decision, codes = tile["tile_id"], tile["decision"], list(tile["reason_codes"])
        # A doubt is keep_lit without a new quote: it keeps the accepted row, never Core's "no".
        fresh = decision in _FROM_CHECKPOINT or (decision == rule.KEEP_LIT and "new_quote" in codes)
        if fresh and decision in {rule.KEEP_LIT, rule.TURN_OFF_PROVEN} and not keeps_reviewed_relations(accepted_candidate, verdicts, relations.get(tile_id, [])):
            # Rule 8: the tile keeps the accepted row that cites its reviewed relation, so it stays lit.
            # A proven turn-off becomes a doubt for a person instead of failing the re-scan (Q4 = A).
            fresh = False
            codes = [*codes, HELD_FOR_AUTHORITATIVE_RELATION] if decision == rule.KEEP_LIT else [rule.DOUBT, rule.TURN_OFF_PROVEN, HELD_FOR_AUTHORITATIVE_RELATION]
            decision = rule.KEEP_LIT
        if tile_id in doubted:
            # The accepted row judged this tile unlit over reviewed evidence it never cited: a person decides.
            codes = [*codes, rule.DOUBT, HELD_FOR_AUTHORITATIVE_RELATION]
        row = verdicts[tile_id] if fresh else accepted.get(tile_id)
        if row is not None:
            rows[tile_id] = row
        decisions.append({"tile_id": tile_id, "decision": decision, "reason_codes": codes})
    if not keeps_reviewed_relations(accepted_candidate, rows, witness["authoritative_relations"]):
        return _result("unavailable", ["authoritative_relation_dropped"], signal)
    # A tile of a not-detected component has no accepted row; its sentinel stands for it until one lights.
    components = {row["component_key"] for row in rows.values()}
    sentinels = {row["component_key"]: row for row in prior_sentinels if row["component_key"] not in components}
    try:
        candidate = _candidate(str(accepted_candidate["id"]), rows, sentinels, witness, ledger_rows, decisions, signal, guard)
    except evaluation.IncrementalEvaluationError:
        return _result("unavailable", ["candidate_partition_invalid"], signal)
    return _result("candidate", [], signal, candidate)


def split_reviewed_relations(candidate: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a candidate's reviewed relations by its own rows into those that bind and its doubts, in witness order.

    A relation binds when its row cites it, or when its row leaves it uncited on a lit tile, as
    the authorities adopted before the 2026-09-11 citation check hold them. One left uncited on
    an unlit tile is a doubt: the candidate's rebuild leaves it out and its next tile re-scan reports it.
    """

    rows = {row["tile_id"]: row for row in candidate["candidate_tile_judgments"]}
    relations = candidate["authoritative_relation_witness"]["authoritative_relations"]
    reviewed = _relation_pairs(relations)
    binding: list[dict[str, Any]] = []
    doubts: list[dict[str, Any]] = []
    for relation in relations:
        row = rows.get(relation["tile_id"])
        (binding if _keeps(row, reviewed[relation["tile_id"]], row, _pair(relation)) else doubts).append(relation)
    return binding, doubts


def keeps_reviewed_relations(accepted_candidate: Mapping[str, Any], rows: Mapping[str, Mapping[str, Any]], relations: Iterable[Mapping[str, Any]]) -> bool:
    """Rule 8, the append guard's test too (repository ``_sv9_tile_rescan_continuity``): whether
    ``rows``, by tile id, keep every reviewed relation in ``relations`` over the accepted candidate.

    A row keeps a pair it cites. Where the accepted row leaves one of its tile's reviewed relations
    uncited (reviewed before the 2026-09-11 citation check), a pair it does not cite either binds
    only while the tile stays lit.
    """

    accepted = {row["tile_id"]: row for row in accepted_candidate["candidate_tile_judgments"]}
    reviewed = _relation_pairs(accepted_candidate["authoritative_relation_witness"]["authoritative_relations"])
    return all(_keeps(accepted.get(row["tile_id"]), reviewed.get(row["tile_id"], set()), rows.get(row["tile_id"]), _pair(row)) for row in relations)


def _keeps(accepted_row: Mapping[str, Any] | None, reviewed: set[tuple[str, str]], row: Mapping[str, Any] | None, pair: tuple[str, str]) -> bool:
    cited = _supports(accepted_row)
    return pair in _supports(row) or (pair not in cited and not reviewed <= cited and _lit(accepted_row) and _lit(row))


def _relation_pairs(relations: Iterable[Mapping[str, Any]]) -> dict[str, set[tuple[str, str]]]:
    pairs: dict[str, set[tuple[str, str]]] = {}
    for relation in relations:
        pairs.setdefault(relation["tile_id"], set()).add(_pair(relation))
    return pairs


def _pair(item: Mapping[str, Any]) -> tuple[str, str]:
    return item["evidence_ref"], item["evidence_fingerprint"]


def _supports(row: Mapping[str, Any] | None) -> set[tuple[str, str]]:
    return {_pair(item) for item in (row or {}).get("supporting_evidence", [])}


def _lit(row: Mapping[str, Any] | None) -> bool:
    return row is not None and row.get("assessment_state") == rule.OK


def _candidate(
    prior_id: str, rows: Mapping[str, Any], sentinels: Mapping[str, Any], witness: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]], decisions: list[dict[str, Any]], signal: Mapping[str, Any],
    guard: Mapping[str, Any],
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
            "guard": dict(guard),
        },
    }
    candidate["complete_record_fingerprint"] = authority_event.candidate_complete_record_fingerprint(candidate)
    return candidate


def _result(
    status: str, reasons: list[str], signal: Mapping[str, Any] | None = None, candidate: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"status": status, "reason_codes": reasons, "change_signal": signal, "candidate": candidate}
