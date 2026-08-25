from __future__ import annotations

from src.sv9 import assessment_kernel as _kernel
from src.sv9 import judgment_memory as jm

PLAN_VERSION = "sv9-incremental-plan-v1"
PLAN_FINGERPRINT_NAMESPACE = "sv9-incremental-plan-fingerprint-v1"
CANDIDATE_NAMESPACE = "sv9-candidate-series-fingerprint-v1"
_REVIEW_ACTIONS = frozenset({"human_review_required", "reopen_contradiction"})
_ELIGIBLE_REVIEW = frozenset({"none", "resolved"})
_PLAN_FIELDS = frozenset(
    "schema_version current_series_contract current_series_fingerprint candidate_series_fingerprint predecessor_series_fingerprints registry_tile_ids prior_judgments delta_projections items tile_workset component_workset reuse_set unaffected_set frozen_set review_set expected_calls calls_avoided canonical_plan_fingerprint".split()
)


IncrementalPlannerError = jm.JudgmentMemoryContractError


def _fail(message):
    raise IncrementalPlannerError(message)


_REGISTRY = tuple(
    (tile["tile_id"], component["component_key"])
    for component in _kernel.build_sv9_tile_contract_registry()["components"]
    for tile in component["tiles"]
)
_COMPONENT_BY_TILE = dict(_REGISTRY)
_ORDER = {tile: position for position, (tile, _component) in enumerate(_REGISTRY)}


def _list(value, label):
    if type(value) is not list:
        _fail(f"{label} must be a JSON array")
    return value


def _ordered_ids(value, label, *, require_order=True):
    values = _list(value, label)
    if any(type(tile) is not str or not tile or tile != tile.strip() for tile in values):
        _fail(f"{label} contains noncanonical tile ids")
    if len(set(values)) != len(values):
        _fail(f"{label} contains duplicate tiles")
    if any(tile not in _ORDER for tile in values):
        _fail(f"{label} contains an unknown tile")
    ordered = sorted(values, key=_ORDER.__getitem__)
    if require_order and values != ordered:
        _fail(f"{label} is not in executable registry order")
    return ordered


def _validate_collections(prior_judgments, delta_projections, selected):
    def checked(values, validator, label):
        rows = []
        for value in _list(values, label):
            try:
                rows.append(validator(value))
            except jm.JudgmentMemoryContractError as exc:
                raise IncrementalPlannerError(str(exc)) from exc
        ids = [row["tile_id"] for row in rows]
        if len(set(ids)) != len(ids) or any(tile not in selected for tile in ids):
            _fail(f"{label} contains duplicate or out-of-scope tiles")
        return rows

    prior = checked(prior_judgments, jm.validate_tile_judgment, "prior judgments")
    delta = checked(delta_projections, jm.validate_tile_evidence_delta_projection, "delta projections")
    return ({row["tile_id"]: row for row in prior}, {row["tile_id"]: row for row in delta})


def _eligible(judgment):
    return (
        bool(judgment)
        and judgment["authority_state"] == "accepted"
        and judgment["lifecycle_state"] == "active"
        and judgment["review_state"] in _ELIGIBLE_REVIEW
    )


def _item(tile, prior, delta, current_fp):
    prior_fp = prior["canonical_judgment_fingerprint"] if prior else None
    delta_fp = delta["projection_fingerprint"] if delta else None
    evidence = delta["evidence"] if delta else (prior["supporting_evidence"] if prior else [])
    same_series = bool(prior and prior["series_fingerprint"] == current_fp)
    eligible = _eligible(prior)
    disposition = delta["disposition"] if delta else None
    action, reason, reused, expected = None, None, None, False
    if disposition == "human_review_required":
        action, reason = "human_review_required", "explicit_human_review_disposition"
    elif disposition == "contradiction":
        if _eligible(prior):
            action, reason = "reopen_contradiction", "contradiction_against_canonical_prior"
        else:
            action, reason = "human_review_required", "contradiction_without_canonical_prior"
    elif prior and not eligible:
        action, reason = "human_review_required", "prior_not_accepted_active"
    elif prior and not same_series:
        action, reason, expected = "reopen_contract_change", "series_contract_changed", True
    elif not prior:
        if disposition == "unchanged":
            action, reason = "human_review_required", "unchanged_without_prior"
        else:
            action, reason, expected = "evaluate_new", "no_prior_judgment", True
    elif not delta:
        action, reason, reused = "reuse_canonical", "same_series_no_delta", prior_fp
    elif disposition == "irrelevant":
        action, reason, reused = "unaffected", "irrelevant_evidence", prior_fp
    elif disposition == "unchanged":
        if delta["evidence"] == prior["supporting_evidence"]:
            action, reason, reused = "reuse_canonical", "unchanged_evidence_matches", prior_fp
        else:
            action, reason = "human_review_required", "unchanged_evidence_mismatch"
    elif disposition == "relevant":
        if delta["evidence"] == prior["supporting_evidence"]:
            action, reason = "human_review_required", "relevant_evidence_identical"
        else:
            action, reason, expected = "evaluate_delta", "relevant_evidence_changed", True
    else:
        _fail("invalid planner disposition")
    return {
        "tile_id": tile,
        "component_key": _COMPONENT_BY_TILE[tile],
        "action": action,
        "reason": reason,
        "evidence": evidence,
        "prior_judgment_fingerprint": prior_fp,
        "delta_projection_fingerprint": delta_fp,
        "reused_judgment_fingerprint": reused,
        "expected_call_contribution": expected,
    }


def _candidate(current_fp, predecessors):
    return jm.canonical_fingerprint(
        CANDIDATE_NAMESPACE, {"series_fingerprint": current_fp, "predecessor_series_fingerprints": predecessors}
    )


def build_incremental_plan(prior_judgments, delta_projections, current_series_contract, *, registry_tile_ids=None):
    try:
        current = jm.validate_judgment_series_contract(current_series_contract)
    except jm.JudgmentMemoryContractError as exc:
        raise IncrementalPlannerError(str(exc)) from exc
    registry_ids = (
        list(_ORDER)
        if registry_tile_ids is None
        else _ordered_ids(registry_tile_ids, "registry_tile_ids", require_order=False)
    )
    selected = set(registry_ids)
    prior, delta = _validate_collections(prior_judgments, delta_projections, selected)
    current_fp = jm.canonical_fingerprint("sv9-judgment-series-fingerprint-v1", current)
    predecessors = sorted({row["series_fingerprint"] for row in prior.values()})
    items = [_item(tile, prior.get(tile), delta.get(tile), current_fp) for tile in registry_ids]
    workset = [row["tile_id"] for row in items if row["expected_call_contribution"]]
    components = []
    for row in items:
        if row["expected_call_contribution"] and row["component_key"] not in components:
            components.append(row["component_key"])
    reuse = [row["tile_id"] for row in items if row["action"] == "reuse_canonical"]
    unaffected = [row["tile_id"] for row in items if row["action"] == "unaffected"]
    review = [row["tile_id"] for row in items if row["action"] in _REVIEW_ACTIONS]
    plan = {
        "schema_version": PLAN_VERSION,
        "current_series_contract": current,
        "current_series_fingerprint": current_fp,
        "candidate_series_fingerprint": _candidate(current_fp, predecessors),
        "predecessor_series_fingerprints": predecessors,
        "registry_tile_ids": registry_ids,
        "prior_judgments": [prior[tile] for tile in registry_ids if tile in prior],
        "delta_projections": [delta[tile] for tile in registry_ids if tile in delta],
        "items": items,
        "tile_workset": workset,
        "component_workset": components,
        "reuse_set": reuse,
        "unaffected_set": unaffected,
        "frozen_set": [row["tile_id"] for row in items if row["action"] in {"reuse_canonical", "unaffected"}],
        "review_set": review,
        "expected_calls": len(components),
        "calls_avoided": len({_COMPONENT_BY_TILE[tile] for tile in registry_ids}) - len(components),
    }
    plan["canonical_plan_fingerprint"] = jm.canonical_fingerprint(PLAN_FINGERPRINT_NAMESPACE, plan)
    return plan


def _json_types(value, active=None):
    active = set() if active is None else active
    if type(value) in {dict, list}:
        marker = id(value)
        if marker in active:
            _fail("plan JSON contains a cycle")
        active.add(marker)
        if type(value) is dict and any(type(key) is not str for key in value):
            _fail("plan object keys must be strings")
        for child in value.values() if type(value) is dict else value:
            _json_types(child, active)
        active.remove(marker)
    elif type(value) not in {str, int, bool} and value is not None:
        _fail("plan JSON values must use built-in types")


def validate_incremental_plan(value):
    try:
        _json_types(value)
        if set(value) != _PLAN_FIELDS:
            _fail("plan fields do not match schema")
        if value["schema_version"] != PLAN_VERSION:
            _fail("unsupported incremental plan")
        registry_ids = _ordered_ids(value["registry_tile_ids"], "registry_tile_ids")
        rebuilt = build_incremental_plan(
            value["prior_judgments"],
            value["delta_projections"],
            value["current_series_contract"],
            registry_tile_ids=registry_ids,
        )
        if jm.canonical_json(value) != jm.canonical_json(rebuilt):
            _fail("plan does not match its bound canonical inputs")
        return rebuilt
    except IncrementalPlannerError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError) as exc:
        raise IncrementalPlannerError("invalid signed incremental plan") from exc


def canonical_plan_json(value):
    return jm.canonical_json(validate_incremental_plan(value))
