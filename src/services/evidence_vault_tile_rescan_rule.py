"""Shadow per-tile re-scan rule: what each accepted tile would become after one re-scan.

A lit tile follows what B3S can see against the prior accepted authority. Core's
current ``ok`` keeps it lit. Core's ``no`` or ``sin_evidencia`` turns it off only
when an accepted support was seen in the capture and shown to Core. A ledger that proves every
accepted support gone turns it off with or without a verdict. Any other lit case
is a ``b3s_failure`` that keeps the tile's state. An unlit tile lights only on
Core's ``ok``. Pure: no I/O, and the projection never changes a score.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.services import evidence_vault_evidence_ledger as ledger
from src.sv9 import assessment_kernel as kernel


KEEP_LIT, TURN_OFF_CORE_NO, TURN_OFF_PROVEN = "keep_lit", "turn_off_core_no", "turn_off_proven"
B3S_FAILURE, LIGHT, KEEP_UNLIT = "b3s_failure", "light", "keep_unlit"
OK, SIN_EVIDENCIA, NOT_DETECTED = "ok", "sin_evidencia", "not_detected"
# The owner's acceptance criterion: a re-scan of an unchanged brand stays within 4 SV9 points.
SCORE_TOLERANCE = 4
# Reviewed and reused tiles get no verdict in a re-scan, so Core's "no" can only reach evaluated tiles.
NO_VERDICT = "component_not_evaluated"
_VERDICT_STATES = frozenset({OK, "no", SIN_EVIDENCIA})
_COMPONENTS = kernel.build_sv9_tile_contract_registry()["components"]
_TILES = tuple((tile["tile_id"], row["component_key"]) for row in _COMPONENTS for tile in row["tiles"])
_SCALE = {row["component_key"]: row["scale"] for row in _COMPONENTS}
_Shown = Mapping[str, Mapping[str, Any]]


def decide_tile(
    accepted_state: str | None, supports: Sequence[Mapping[str, Any]], verdict: Mapping[str, Any] | None, shown: _Shown
) -> dict[str, Any]:
    """Decide one tile from its accepted state, its supports' ledger states and Core's current verdict.

    ``supports`` are one tile's ``summarize_tile_support`` entries. ``shown`` maps a
    support's evidence ref to its ``shown_to_core`` entry for the tile's component;
    a ref without an entry was not shown. ``verdict`` is the current scan's tile
    judgment, or None when the tile was not evaluated.
    """

    state = _verdict_state(verdict)
    missing = [NO_VERDICT] if state is None else []
    if accepted_state != OK:
        return _decision(LIGHT, []) if state == OK else _decision(KEEP_UNLIT, missing)
    if state == OK:
        same = _pairs(verdict.get("supporting_evidence") or []) == _pairs(supports)
        return _decision(KEEP_LIT, ["same_quote" if same else "new_quote"])
    seen = [item for item in supports if item["state"] == ledger.SEEN]
    if state is not None and any(_shown(item, shown)["status"] == ledger.SHOWN for item in seen):
        return _decision(TURN_OFF_CORE_NO, [])
    if supports and all(item["state"] == ledger.VERIFIED_ABSENT for item in supports):
        return _decision(TURN_OFF_PROVEN, missing)
    if seen and state is None:
        return _decision(KEEP_LIT, [NO_VERDICT, "proof_seen_not_reevaluated"])
    if seen:
        return _decision(B3S_FAILURE, _unique(code for item in seen for code in _not_shown(_shown(item, shown))))
    return _decision(B3S_FAILURE, _unique([*missing, *(code for item in supports for code in item["reason_codes"])]))


def project_rescan(
    prior_judgments: Sequence[Mapping[str, Any]],
    prior_sentinels: Sequence[Mapping[str, Any]],
    ledger_rows: Sequence[Mapping[str, Any]],
    current_judgments: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Decide all 80 tiles and score the would-be vector against the accepted one.

    ``prior_judgments`` and ``prior_sentinels`` are the accepted candidate's tile
    judgments and not-detected component sentinels; ``current_judgments`` are the
    tile judgments Core gave in the re-scan. An accepted vector the SV9 kernel
    rejects yields no decisions and no scores.
    """

    sentinels = {str(row["component_key"]) for row in prior_sentinels}
    try:
        accepted_score = _score(prior_judgments, sentinels)
    except (kernel.Sv9AssessmentError, KeyError, TypeError):
        return _unavailable("accepted_vector_unavailable")
    accepted = {tile_id: NOT_DETECTED for tile_id, component in _TILES if component in sentinels}
    accepted |= {row["tile_id"]: row["assessment_state"] for row in prior_judgments}
    verdicts = {str(row["tile_id"]): row for row in current_judgments}
    summaries = ledger.summarize_tile_support(ledger_rows, prior_judgments)
    supports = {row["tile_id"]: row["supporting_evidence"] for row in summaries}
    by_pair = {(row["evidence_ref"], row["evidence_fingerprint"]): row for row in ledger_rows}
    tiles = []
    for tile_id, component in _TILES:
        items, verdict = supports.get(tile_id, []), verdicts.get(tile_id)
        decision = decide_tile(accepted[tile_id], items, verdict, _shown_entries(items, component, by_pair))
        would_be = _would_be_state(accepted[tile_id], decision["decision"], _verdict_state(verdict))
        row = {"tile_id": tile_id, "component_key": component, "accepted_state": accepted[tile_id], **decision}
        tiles.append({**row, "would_be_state": would_be})
    _rescore_lit_sentinels(tiles, verdicts)
    scored = [{**tile, "assessment_state": tile["would_be_state"]} for tile in tiles]
    would_be_score = _score(
        [tile for tile in scored if tile["assessment_state"] != NOT_DETECTED],
        {tile["component_key"] for tile in scored if tile["assessment_state"] == NOT_DETECTED},
    )
    delta = would_be_score - accepted_score
    counts = dict(sorted(Counter(tile["decision"] for tile in tiles).items()))
    return {
        "tile_decisions": {"reason_codes": [], "counts": counts, "tiles": tiles},
        "would_be_score": would_be_score,
        "accepted_score": accepted_score,
        "delta": delta,
        "within_tolerance": abs(delta) <= SCORE_TOLERANCE,
    }


def _shown_entries(supports: Sequence[Mapping[str, Any]], component: str, by_pair: Mapping[Any, Any]) -> _Shown:
    pairs = ((item["evidence_ref"], (item["evidence_ref"], item["evidence_fingerprint"])) for item in supports)
    return {ref: by_pair[pair]["shown_to_core"][component] for ref, pair in pairs if pair in by_pair}


def _would_be_state(accepted_state: str, decision: str, verdict_state: str | None) -> str:
    if decision in {KEEP_LIT, LIGHT}:
        return OK
    if decision in {TURN_OFF_CORE_NO, TURN_OFF_PROVEN}:
        return verdict_state or SIN_EVIDENCIA
    return accepted_state


def _rescore_lit_sentinels(tiles: list[dict[str, Any]], verdicts: Mapping[str, Mapping[str, Any]]) -> None:
    """A light inside a not-detected component scores that whole component again."""

    lit = {tile["component_key"] for tile in tiles if tile["decision"] == LIGHT}
    for tile in tiles:
        if tile["component_key"] in lit and tile["would_be_state"] == NOT_DETECTED:
            tile["would_be_state"] = _verdict_state(verdicts.get(tile["tile_id"])) or SIN_EVIDENCIA


def _score(rows: Iterable[Mapping[str, Any]], sentinels: Iterable[str]) -> int:
    """The SV9 kernel score; the kernel rejects unknown, duplicate, missing or mismatched tiles."""

    fields = ("component_key", "tile_id", "assessment_state")
    tiles = [
        {**{key: row[key] for key in fields}, "tile_key": f"{row['component_key']}.{row['tile_id']}"} for row in rows
    ]
    zeros = dict.fromkeys(("score", "raw_score", "effective_score", "points"), 0)
    blocks = [
        {"component_key": key, "status": NOT_DETECTED, **zeros, "tile_profile": [], "scale": _SCALE[key]}
        for key in sorted(sentinels)
    ]
    return kernel.build_sv9_assessment(tiles, blocks or None)["sv9_score"]


def _unavailable(reason: str) -> dict[str, Any]:
    scores = dict.fromkeys(("would_be_score", "accepted_score", "delta", "within_tolerance"))
    return {"tile_decisions": {"reason_codes": [reason], "counts": {}, "tiles": []}, **scores}


def _verdict_state(verdict: Mapping[str, Any] | None) -> str | None:
    state = verdict.get("assessment_state") if isinstance(verdict, Mapping) else None
    return state if state in _VERDICT_STATES else None


def _shown(item: Mapping[str, Any], shown: _Shown) -> Mapping[str, Any]:
    missing = {"status": ledger.NOT_SHOWN, "reason_codes": ["supporting_evidence_not_in_ledger"]}
    return shown.get(item["evidence_ref"]) or missing


def _not_shown(entry: Mapping[str, Any]) -> list[str]:
    # A tile signal carries the evidence ref, not its text, so it never counts as showing the proof.
    if entry["status"] == ledger.SHOWN_AS_SIGNAL:
        return ["not_shown_signal_only"]
    return [f"not_shown_{code}" for code in entry["reason_codes"]]


def _pairs(items: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    return {(str(item["evidence_ref"]), str(item["evidence_fingerprint"])) for item in items}


def _unique(codes: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(codes))


def _decision(decision: str, reason_codes: list[str]) -> dict[str, Any]:
    return {"decision": decision, "reason_codes": reason_codes}
