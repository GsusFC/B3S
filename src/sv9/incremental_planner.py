from __future__ import annotations

from src.sv9 import assessment_kernel as _kernel
from src.sv9 import judgment_memory as jm

PLAN_VERSION = "sv9-incremental-plan-v3"
PLAN_FINGERPRINT_NAMESPACE = "sv9-incremental-plan-fingerprint-v3"
LEGACY_PLAN_VERSION = "sv9-incremental-plan-v2"
LEGACY_PLAN_FINGERPRINT_NAMESPACE = "sv9-incremental-plan-fingerprint-v2"
CANDIDATE_NAMESPACE = "sv9-candidate-series-fingerprint-v1"
COMPONENT_SENTINEL_VERSION = "sv9-component-not-detected-sentinel-v1"
COMPONENT_SENTINEL_FINGERPRINT_NAMESPACE = "sv9-component-not-detected-sentinel-fingerprint-v1"
_REVIEW_ACTIONS = frozenset({"human_review_required", "reopen_contradiction"})
_ELIGIBLE_REVIEW = frozenset({"none", "resolved"})
_PLAN_FIELDS = frozenset("schema_version current_series_contract current_series_fingerprint candidate_series_fingerprint predecessor_series_fingerprints registry_tile_ids prior_judgments prior_component_sentinels delta_projections items tile_workset component_workset reuse_set unaffected_set frozen_set review_set expected_calls calls_avoided canonical_plan_fingerprint".split())  # fmt: skip
_SENTINEL_FIELDS = frozenset("schema_version component_key status supporting_evidence capture_origin operation_origin series_contract series_fingerprint authority_state review_state lifecycle_state lifecycle_reason canonical_component_sentinel_fingerprint".split())  # fmt: skip
_SENTINEL_INPUT = _SENTINEL_FIELDS - {
    "schema_version",
    "series_fingerprint",
    "canonical_component_sentinel_fingerprint",
}


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
_COMPONENT_TILES = {
    component: tuple(tile for tile, owner in _REGISTRY if owner == component)
    for component in dict.fromkeys(owner for _tile, owner in _REGISTRY)
}


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


def build_component_not_detected_sentinel(*, _raw=None, _signed=False, **value):
    raw = _raw if _raw is not None else value
    try:
        _json_types(raw)
    except RecursionError as exc:
        raise IncrementalPlannerError("component sentinel is not canonical JSON") from exc
    if type(raw) is not dict or set(raw) != (_SENTINEL_FIELDS if _signed else _SENTINEL_INPUT):
        _fail("component sentinel fields do not match schema")
    if _signed and raw["schema_version"] != COMPONENT_SENTINEL_VERSION:
        _fail("unsupported component sentinel")
    component = raw["component_key"]
    if type(component) is not str or component not in _COMPONENT_TILES or raw["status"] != "not_detected":
        _fail("component sentinel identity is invalid")
    reference = jm.build_tile_judgment(tile_id=_COMPONENT_TILES[component][0], component_key=component, assessment_state="sin_evidencia", **{key: raw[key] for key in _SENTINEL_INPUT - {"component_key", "status"}})  # fmt: skip
    result = {"schema_version": COMPONENT_SENTINEL_VERSION, "component_key": component, "status": "not_detected", "series_fingerprint": reference["series_fingerprint"], **{key: reference[key] for key in _SENTINEL_INPUT - {"component_key", "status"}}}  # fmt: skip
    result["canonical_component_sentinel_fingerprint"] = jm.canonical_fingerprint(COMPONENT_SENTINEL_FINGERPRINT_NAMESPACE, result)  # fmt: skip
    if _signed and raw != result:
        _fail("component sentinel replay fingerprint or canonical value mismatch")
    return result


def validate_component_not_detected_sentinel(value):
    return build_component_not_detected_sentinel(_raw=value, _signed=True)


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


def _validate_sentinels(values, selected, prior):
    rows = [validate_component_not_detected_sentinel(value) for value in _list(values, "prior component sentinels")]
    indexed = {row["component_key"]: row for row in rows}
    if len(indexed) != len(rows):
        _fail("prior component sentinels contain duplicate components")
    for component in indexed:
        tiles = _COMPONENT_TILES[component]
        if not set(tiles) <= selected:
            _fail("component sentinel requires a full component registry")
        if any(tile in prior for tile in tiles):
            _fail("component sentinel cannot mix with tile judgments")
    return indexed


def _eligible(judgment):
    return _accepted_active(judgment) and judgment["review_state"] in _ELIGIBLE_REVIEW


def _accepted_active(judgment):
    return bool(judgment) and judgment["authority_state"] == "accepted" and judgment["lifecycle_state"] == "active"


def _sentinel_action(sentinel, dispositions, current_fp):
    if "contradiction" in dispositions and _eligible(sentinel):
        return "reopen_contradiction", "component_sentinel_contradiction", False
    if "contradiction" in dispositions or "human_review_required" in dispositions or not _eligible(sentinel):
        return "human_review_required", "component_sentinel_review_required", False
    if sentinel["series_fingerprint"] != current_fp:
        return "reopen_contract_change", "component_sentinel_series_changed", True
    if "relevant" in dispositions:
        return "evaluate_delta", "component_sentinel_relevant_evidence", True
    if any(disposition != "irrelevant" for disposition in dispositions):
        return "human_review_required", "component_sentinel_delta_mismatch", False
    return "reuse_canonical", "same_series_component_sentinel", False


def _apply_sentinels(items, sentinels, delta, current_fp):
    for component, sentinel in sentinels.items():
        dispositions = [delta[tile]["disposition"] for tile in _COMPONENT_TILES[component] if tile in delta]
        action, reason, expected = _sentinel_action(sentinel, dispositions, current_fp)
        reused = sentinel["canonical_component_sentinel_fingerprint"] if action == "reuse_canonical" else None
        for row in items:
            if row["component_key"] == component:
                row["action"], row["reason"], row["expected_call_contribution"], row["reused_judgment_fingerprint"] = action, reason, expected, reused  # fmt: skip


def _invalidate_coherencia(items):
    upstream = [row for row in items if row["component_key"] != "coherencia" and row["expected_call_contribution"]]
    if not upstream:
        return
    action, reason = (
        ("reopen_contract_change", "upstream_contract_change_dependency")
        if any(row["action"] == "reopen_contract_change" for row in upstream)
        else ("evaluate_delta", "upstream_evidence_dependency")
    )
    for row in items:
        if row["component_key"] == "coherencia" and row["action"] in {"reuse_canonical", "unaffected"}:
            row["action"], row["reason"], row["expected_call_contribution"], row["reused_judgment_fingerprint"] = action, reason, True, None  # fmt: skip


def _item(tile, prior, delta, current_fp, rollover=False):
    prior_fp = prior["canonical_judgment_fingerprint"] if prior else None
    delta_fp = delta["projection_fingerprint"] if delta else None
    evidence = delta["evidence"] if delta else (prior["supporting_evidence"] if prior else [])
    same_series = bool(prior and prior["series_fingerprint"] == current_fp)
    eligible = _eligible(prior)
    disposition = delta["disposition"] if delta else None
    action, reason, reused, expected = None, None, None, False
    if rollover:
        action, reason, expected = (
            "reopen_contract_change",
            "upstream_contract_change_dependency"
            if same_series and _COMPONENT_BY_TILE[tile] == "coherencia"
            else "series_contract_changed",
            True,
        )
    elif disposition == "human_review_required":
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


def _build_incremental_plan(
    prior_judgments,
    delta_projections,
    current_series_contract,
    *,
    version,
    registry_tile_ids=None,
    prior_component_sentinels=None,
):
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
    sentinels = _validate_sentinels(
        [] if prior_component_sentinels is None else prior_component_sentinels, selected, prior
    )
    current_fp = jm.canonical_fingerprint("sv9-judgment-series-fingerprint-v1", current)
    predecessors = sorted({row["series_fingerprint"] for row in [*prior.values(), *sentinels.values()]})
    rollover = version == PLAN_VERSION and any(
        _accepted_active(row) and row["series_fingerprint"] != current_fp
        for row in [*prior.values(), *sentinels.values()]
    )
    items = [_item(tile, prior.get(tile), delta.get(tile), current_fp, rollover) for tile in registry_ids]
    if not rollover:
        _apply_sentinels(items, sentinels, delta, current_fp)
    _invalidate_coherencia(items)
    workset = [row["tile_id"] for row in items if row["expected_call_contribution"]]
    components = []
    for row in items:
        if row["expected_call_contribution"] and row["component_key"] not in components:
            components.append(row["component_key"])
    reuse = [row["tile_id"] for row in items if row["action"] == "reuse_canonical"]
    unaffected = [row["tile_id"] for row in items if row["action"] == "unaffected"]
    review = [row["tile_id"] for row in items if row["action"] in _REVIEW_ACTIONS]
    plan = {
        "schema_version": version,
        "current_series_contract": current,
        "current_series_fingerprint": current_fp,
        "candidate_series_fingerprint": _candidate(current_fp, predecessors),
        "predecessor_series_fingerprints": predecessors,
        "registry_tile_ids": registry_ids,
        "prior_judgments": [prior[tile] for tile in registry_ids if tile in prior],
        "prior_component_sentinels": [sentinels[key] for key in _COMPONENT_TILES if key in sentinels],
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
    plan["canonical_plan_fingerprint"] = jm.canonical_fingerprint(
        PLAN_FINGERPRINT_NAMESPACE if version == PLAN_VERSION else LEGACY_PLAN_FINGERPRINT_NAMESPACE, plan
    )
    return plan


def build_incremental_plan(
    prior_judgments,
    delta_projections,
    current_series_contract,
    *,
    registry_tile_ids=None,
    prior_component_sentinels=None,
):
    return _build_incremental_plan(
        prior_judgments,
        delta_projections,
        current_series_contract,
        version=PLAN_VERSION,
        registry_tile_ids=registry_tile_ids,
        prior_component_sentinels=prior_component_sentinels,
    )


def build_incremental_plan_v2(
    prior_judgments,
    delta_projections,
    current_series_contract,
    *,
    registry_tile_ids=None,
    prior_component_sentinels=None,
):
    return _build_incremental_plan(
        prior_judgments,
        delta_projections,
        current_series_contract,
        version=LEGACY_PLAN_VERSION,
        registry_tile_ids=registry_tile_ids,
        prior_component_sentinels=prior_component_sentinels,
    )


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
        builders = {LEGACY_PLAN_VERSION: build_incremental_plan_v2, PLAN_VERSION: build_incremental_plan}
        builder = builders.get(value["schema_version"])
        if builder is None:
            _fail("unsupported incremental plan")
        registry_ids = _ordered_ids(value["registry_tile_ids"], "registry_tile_ids")
        rebuilt = builder(
            value["prior_judgments"],
            value["delta_projections"],
            value["current_series_contract"],
            registry_tile_ids=registry_ids,
            prior_component_sentinels=value["prior_component_sentinels"],
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
