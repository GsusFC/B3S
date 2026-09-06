from __future__ import annotations
from dataclasses import dataclass
import json
import re
from typing import Literal, Mapping, Protocol
from src.sv9 import assessment_kernel as kernel
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory
from src.services.evidence_vault_sv9_workset_partition import validate_evidence_vault_sv9_workset_partition

# fmt: off
EVIDENCE_PACKET_VERSION = "sv9-component-evidence-packet-v1"
COMPONENT_REQUEST_VERSION = "sv9-strict-component-request-v1"
COMPONENT_EVALUATION_VERSION = "sv9-strict-component-evaluation-v1"
_PACKET_FINGERPRINT = "sv9-component-evidence-packet-fingerprint-v1"
_REQUEST_FINGERPRINT = "sv9-strict-component-request-fingerprint-v1"
_EVALUATION_FINGERPRINT = "sv9-strict-component-evaluation-fingerprint-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
class IncrementalEvaluationError(ValueError): pass
@dataclass(frozen=True, slots=True)
class ComponentEvaluationOutcome:
    evaluation: Mapping[str, object] | None = None
    reason_code: Literal["provider_failure"] | None = None

    @classmethod
    def success(cls, evaluation: Mapping[str, object]) -> "ComponentEvaluationOutcome":
        return cls(evaluation=evaluation)

    @classmethod
    def provider_failure(cls) -> "ComponentEvaluationOutcome":
        return cls(reason_code="provider_failure")

class Sv9StrictComponentFlowPort(Protocol):
    def evaluate_component(self, request) -> ComponentEvaluationOutcome: ...
_registry = kernel.build_sv9_tile_contract_registry()
_COMPONENTS = tuple(row["component_key"] for row in _registry["components"])
_TILES = tuple((tile["tile_id"], row["component_key"], tile["tile_key"], tile["definition"]) for row in _registry["components"] for tile in row["tiles"])
_BY_TILE = {row[0]: row for row in _TILES}
_ORDER = {row[0]: index for index, row in enumerate(_TILES)}
_COMPONENT_TILES = {component: tuple(row[0] for row in _TILES if row[1] == component) for component in _COMPONENTS}
_SCALE = {row["component_key"]: row["scale"] for row in _registry["components"]}
_PACKET_FIELDS = frozenset("schema_version component_key capture_origin operation_origin series_fingerprint tiles canonical_evidence_packet_fingerprint".split())
_REQUEST_FIELDS = frozenset("schema_version plan_fingerprint candidate_series_fingerprint component_key current_series_contract current_series_fingerprint evidence_packet_fingerprint capture_origin operation_origin requested_tiles upstream_candidate_state canonical_request_fingerprint".split())
_EVALUATION_FIELDS = frozenset("schema_version component_key series_fingerprint request_fingerprint status tile_results canonical_component_evaluation_fingerprint".split())
def _fail(message):
    raise IncrementalEvaluationError(message)
def _json(value, depth=0, active=None):
    if depth > 100: _fail("value is too deeply nested")
    if type(value) in {dict, list}:
        active = set() if active is None else active
        marker = id(value)
        if marker in active: _fail("value contains a cycle")
        active.add(marker)
        if type(value) is dict:
            if any(type(key) is not str for key in value): _fail("object keys must be builtin strings")
            for child in value.values(): _json(child, depth + 1, active)
        else:
            for child in value: _json(child, depth + 1, active)
        active.remove(marker)
    elif type(value) not in {str, int, bool} and value is not None: _fail("value is not builtin JSON")
def _fields(value, expected, label):
    if type(value) is not dict or set(value) != expected: _fail(f"{label} fields do not match schema")
def _text(value, label="value", empty=False):
    if type(value) is not str or value != value.strip() or (not empty and not value): _fail(f"{label} is not canonical text")
    return value
def _sha(value):
    value = _text(value, "fingerprint")
    if not _SHA256.fullmatch(value): _fail("fingerprint is not lowercase sha256")
    return value
def _canon(value):
    _json(value)
    return json.loads(memory.canonical_json(value))
def _origin(value, prefix):
    _fields(value, frozenset({f"{prefix}_id", f"{prefix}_fingerprint"}), f"{prefix} origin")
    return {f"{prefix}_id": _text(value[f"{prefix}_id"]), f"{prefix}_fingerprint": _sha(value[f"{prefix}_fingerprint"])}
def _evidence(values, content=False):
    if type(values) is not list: _fail("evidence must be an array")
    expected = frozenset({"evidence_ref", "evidence_fingerprint"} | ({"content"} if content else set()))
    rows = []
    for raw in values:
        _fields(raw, expected, "evidence")
        row = {"evidence_ref": _text(raw["evidence_ref"]), "evidence_fingerprint": _sha(raw["evidence_fingerprint"])}
        if content: row["content"] = _canon(raw["content"])
        rows.append(row)
    if any(len({row[key] for row in rows}) != len(rows) for key in ("evidence_ref", "evidence_fingerprint")): _fail("evidence contains duplicate identities")
    rows.sort(key=lambda row: (row["evidence_ref"], row["evidence_fingerprint"]))
    return rows
def _packet(raw, signed=False):
    _json(raw)
    expected = _PACKET_FIELDS if signed else _PACKET_FIELDS - {"schema_version", "canonical_evidence_packet_fingerprint"}
    _fields(raw, expected, "evidence packet")
    if signed and raw["schema_version"] != EVIDENCE_PACKET_VERSION: _fail("unsupported evidence packet")
    component = _text(raw["component_key"])
    if component not in _COMPONENT_TILES: _fail("unknown packet component")
    tiles = []
    for row in raw["tiles"] if type(raw["tiles"]) is list else _fail("packet tiles must be an array"):
        _fields(row, frozenset({"tile_id", "evidence"}), "packet tile")
        tile_id = _text(row["tile_id"])
        if tile_id not in _BY_TILE or _BY_TILE[tile_id][1] != component: _fail("packet tile is outside component")
        tiles.append({"tile_id": tile_id, "evidence": _evidence(row["evidence"], True)})
    ids = [row["tile_id"] for row in tiles]
    if len(ids) != len(set(ids)) or ids != sorted(ids, key=_ORDER.__getitem__): _fail("packet tiles are not unique registry order")
    result = {"schema_version": EVIDENCE_PACKET_VERSION, "component_key": component, "capture_origin": _origin(raw["capture_origin"], "capture"), "operation_origin": _origin(raw["operation_origin"], "operation"), "series_fingerprint": _sha(raw["series_fingerprint"]), "tiles": tiles}
    result["canonical_evidence_packet_fingerprint"] = memory.canonical_fingerprint(_PACKET_FINGERPRINT, result)
    if signed and raw != result: _fail("evidence packet is not canonical")
    return result
def build_evidence_packet(*, component_key, tiles, capture_origin, operation_origin, series_fingerprint):
    return _packet({"component_key": component_key, "tiles": tiles, "capture_origin": capture_origin, "operation_origin": operation_origin, "series_fingerprint": series_fingerprint})
def _evaluation(raw, signed=False):
    _json(raw)
    expected = _EVALUATION_FIELDS if signed else _EVALUATION_FIELDS - {"schema_version", "canonical_component_evaluation_fingerprint"}
    _fields(raw, expected, "component evaluation")
    if signed and raw["schema_version"] != COMPONENT_EVALUATION_VERSION: _fail("unsupported component evaluation")
    component, status = _text(raw["component_key"]), _text(raw["status"])
    if component not in _COMPONENT_TILES or status not in {"evaluated", "not_detected"}: _fail("component evaluation identity is invalid")
    if type(raw["tile_results"]) is not list: _fail("tile results must be an array")
    rows = []
    for raw_row in raw["tile_results"]:
        _fields(raw_row, frozenset({"tile_id", "assessment_state", "supporting_evidence"}), "tile result")
        tile_id, state = _text(raw_row["tile_id"]), _text(raw_row["assessment_state"])
        if tile_id not in _BY_TILE or _BY_TILE[tile_id][1] != component or state not in {"ok", "no", "sin_evidencia"}: _fail("tile result is invalid")
        evidence = _evidence(raw_row["supporting_evidence"])
        if (state in {"ok", "no"}) != bool(evidence): _fail("tile state and evidence conflict")
        rows.append({"tile_id": tile_id, "assessment_state": state, "supporting_evidence": evidence})
    ids = [row["tile_id"] for row in rows]
    if len(ids) != len(set(ids)) or ids != sorted(ids, key=_ORDER.__getitem__) or (status == "not_detected" and rows): _fail("component evaluation results are invalid")
    result = {"schema_version": COMPONENT_EVALUATION_VERSION, "component_key": component, "series_fingerprint": _sha(raw["series_fingerprint"]), "request_fingerprint": _sha(raw["request_fingerprint"]), "status": status, "tile_results": rows}
    result["canonical_component_evaluation_fingerprint"] = memory.canonical_fingerprint(_EVALUATION_FINGERPRINT, result)
    if signed and raw != result: _fail("component evaluation is not canonical")
    return result
def build_component_evaluation(*, component_key, series_fingerprint, request_fingerprint, status, tile_results):
    return _evaluation({"component_key": component_key, "series_fingerprint": series_fingerprint, "request_fingerprint": request_fingerprint, "status": status, "tile_results": tile_results})
def _outcome(value) -> ComponentEvaluationOutcome | None:
    if type(value) is not ComponentEvaluationOutcome:
        return None
    if value.evaluation is None:
        return value if value.reason_code == "provider_failure" else None
    if value.reason_code is not None:
        return None
    try:
        return ComponentEvaluationOutcome.success(_evaluation(value.evaluation, True))
    except Exception:
        return None
def _plan(value):
    _json(value)
    try: plan = planner.validate_incremental_plan(value)
    except Exception as exc: raise IncrementalEvaluationError("invalid incremental plan") from exc
    if plan["registry_tile_ids"] != [row[0] for row in _TILES]: _fail("plan is not a complete executable registry")
    if plan["component_workset"] != [component for component in _COMPONENTS if component in plan["component_workset"]]: _fail("plan components are not canonical")
    _frozen(plan, set(plan["tile_workset"]))
    return plan
def _packets(plan, values):
    _json(values)
    if type(values) is not list or len(values) != len(plan["component_workset"]): _fail("evidence packets do not match workset")
    delta, output = {row["tile_id"]: row for row in plan["delta_projections"]}, []
    for component, raw in zip(plan["component_workset"], values):
        packet = _packet(raw, True)
        expected = [tile for tile in plan["tile_workset"] if _BY_TILE[tile][1] == component]
        if packet["component_key"] != component or [row["tile_id"] for row in packet["tiles"]] != expected or packet["series_fingerprint"] != plan["current_series_fingerprint"]: _fail("packet does not bind component workset")
        for tile in packet["tiles"]:
            bound = delta.get(tile["tile_id"])
            if bound and (packet["capture_origin"] != bound["capture_origin"] or packet["operation_origin"] != bound["operation_origin"] or [{key: row[key] for key in ("evidence_ref", "evidence_fingerprint")} for row in tile["evidence"]] != bound["evidence"]): _fail("packet does not bind delta evidence")
        output.append(packet)
    return output
def _request(plan, packet, upstream):
    component = packet["component_key"]
    tiles = [{"tile_id": row["tile_id"], "tile_key": _BY_TILE[row["tile_id"]][2], "definition": _canon(_BY_TILE[row["tile_id"]][3]), "evidence": row["evidence"]} for row in packet["tiles"]]
    raw = {"schema_version": COMPONENT_REQUEST_VERSION, "plan_fingerprint": plan["canonical_plan_fingerprint"], "candidate_series_fingerprint": plan["candidate_series_fingerprint"], "component_key": component, "current_series_contract": plan["current_series_contract"], "current_series_fingerprint": plan["current_series_fingerprint"], "evidence_packet_fingerprint": packet["canonical_evidence_packet_fingerprint"], "capture_origin": packet["capture_origin"], "operation_origin": packet["operation_origin"], "requested_tiles": tiles, "upstream_candidate_state": upstream}
    raw["canonical_request_fingerprint"] = memory.canonical_fingerprint(_REQUEST_FINGERPRINT, raw)
    return raw
def _frozen(plan, workset):
    current, judgments, sentinels = plan["current_series_fingerprint"], {}, {}
    for raw in plan["prior_judgments"]:
        try: row = memory.validate_tile_judgment(raw)
        except Exception as exc: raise IncrementalEvaluationError("frozen tile judgment is invalid") from exc
        if row["tile_id"] in workset: continue
        if row["tile_id"] in judgments or row["component_key"] != _BY_TILE[row["tile_id"]][1] or row["authority_state"] != "accepted" or row["lifecycle_state"] != "active" or row["series_fingerprint"] != current: _fail("frozen tile judgment is not current accepted authority")
        judgments[row["tile_id"]] = row
    for raw in plan["prior_component_sentinels"]:
        try: row = planner.validate_component_not_detected_sentinel(raw)
        except Exception as exc: raise IncrementalEvaluationError("frozen component sentinel is invalid") from exc
        component = row["component_key"]
        if any(tile in workset for tile in _COMPONENT_TILES[component]): continue
        if component in sentinels or row["authority_state"] != "accepted" or row["lifecycle_state"] != "active" or row["series_fingerprint"] != current: _fail("frozen component sentinel is not current accepted authority")
        sentinels[component] = row
    return judgments, sentinels
def _upstream(plan, workset, judgments, sentinels):
    frozen_judgments, frozen_sentinels, rows = *_frozen(plan, workset), []
    for component in _COMPONENTS[:-1]:
        sentinel = sentinels.get(component) or frozen_sentinels.get(component)
        if sentinel:
            rows.append({"component_key": component, "status": "not_detected", "canonical_component_sentinel_fingerprint": sentinel["canonical_component_sentinel_fingerprint"]})
            continue
        for tile in _COMPONENT_TILES[component]:
            judgment = judgments.get(tile) or frozen_judgments.get(tile)
            if not judgment: _fail("upstream candidate partition is incomplete")
            rows.append({"tile_id": tile, "assessment_state": judgment["assessment_state"], "canonical_judgment_fingerprint": judgment["canonical_judgment_fingerprint"]})
    return rows
def _accept(plan, request, raw, workset, judgments, sentinels):
    result = _evaluation(raw, True)
    if result["component_key"] != request["component_key"] or result["series_fingerprint"] != request["current_series_fingerprint"] or result["request_fingerprint"] != request["canonical_request_fingerprint"]: _fail("component evaluation does not echo request")
    requested = [row["tile_id"] for row in request["requested_tiles"]]
    if result["status"] == "not_detected":
        if requested != list(_COMPONENT_TILES[result["component_key"]]): _fail("not detected requires a complete component")
        sentinels[result["component_key"]] = planner.build_component_not_detected_sentinel(component_key=result["component_key"], status="not_detected", supporting_evidence=[], capture_origin=request["capture_origin"], operation_origin=request["operation_origin"], series_contract=request["current_series_contract"], authority_state="pending", review_state="none", lifecycle_state="active", lifecycle_reason="")
        return
    if [row["tile_id"] for row in result["tile_results"]] != requested: _fail("component evaluation does not cover request")
    allowed = {row["tile_id"]: [{key: evidence[key] for key in ("evidence_ref", "evidence_fingerprint")} for evidence in row["evidence"]] for row in request["requested_tiles"]}
    for row in result["tile_results"]:
        if any(evidence not in allowed[row["tile_id"]] for evidence in row["supporting_evidence"]): _fail("tile result cites unrequested evidence")
        judgments[row["tile_id"]] = memory.build_tile_judgment(tile_id=row["tile_id"], component_key=result["component_key"], assessment_state=row["assessment_state"], supporting_evidence=row["supporting_evidence"], capture_origin=request["capture_origin"], operation_origin=request["operation_origin"], series_contract=request["current_series_contract"], authority_state="pending", review_state="none", lifecycle_state="active", lifecycle_reason="")
def _assessment(plan, workset, judgments, sentinels):
    frozen_judgments, frozen_sentinels, tiles, kernel_sentinels = *_frozen(plan, workset), [], []
    if set(judgments) & set(frozen_judgments) or set(sentinels) & set(frozen_sentinels) or not (set(judgments) | set(frozen_judgments)) <= set(_BY_TILE) or not (set(sentinels) | set(frozen_sentinels)) <= set(_COMPONENTS): _fail("candidate partition is invalid")
    candidate_judgments, candidate_sentinels = [], []
    for component in _COMPONENTS:
        sentinel = sentinels.get(component) or frozen_sentinels.get(component)
        if sentinel:
            if any(tile in judgments or tile in frozen_judgments for tile in _COMPONENT_TILES[component]): _fail("component sentinel mixes with tile judgments")
            candidate_sentinels.append(sentinel)
            kernel_sentinels.append({"component_key": component, "status": "not_detected", "score": 0, "raw_score": 0, "effective_score": 0, "points": 0, "tile_profile": [], "scale": _SCALE[component]})
            continue
        for tile in _COMPONENT_TILES[component]:
            judgment = judgments.get(tile) or frozen_judgments.get(tile)
            if not judgment: _fail("candidate partition is incomplete")
            candidate_judgments.append(judgment)
            tile_id, component_key, tile_key, _definition = _BY_TILE[tile]
            tiles.append({"component_key": component_key, "tile_id": tile_id, "tile_key": tile_key, "assessment_state": judgment["assessment_state"]})
    if len(candidate_judgments) + sum(len(_COMPONENT_TILES[row["component_key"]]) for row in candidate_sentinels) != len(_TILES): _fail("candidate partition has invalid capacity")
    if {row["series_fingerprint"] for row in [*candidate_judgments, *candidate_sentinels]} != {plan["current_series_fingerprint"]}: _fail("candidate partition spans judgment series")
    result = kernel.build_sv9_assessment(tiles, kernel_sentinels or None)
    kernel.validate_sv9_assessment_output(result)
    return result, candidate_judgments, candidate_sentinels
def _pending(reason, calls=0, avoided=0, reused=0):
    return {"status": "pending", "reason_code": reason, "assessment": None, "candidate_tile_judgments": [], "candidate_component_sentinels": [], "captured_calls": [], "call_count": calls, "calls_avoided": avoided, "reused_tile_count": reused, "evaluated_tile_count": 0}
def _run(plan, packets, responder, lookup_evaluation=None, persist_evaluation=None):
    calls = [0]
    def call(request, index):
        calls[0] += 1
        return responder(request, index)
    progress = _run_components(plan, packets, plan["tile_workset"], call, calls, "ready", lookup_evaluation, persist_evaluation)
    if progress["status"] == "invalid_input": _fail("invalid component request")
    if progress["status"] != "partial": return None
    judgments = {row["tile_id"]: row for row in progress["evaluated_tile_judgments"]}
    sentinels = {row["component_key"]: row for row in progress["evaluated_component_sentinels"]}
    assessment, candidate_judgments, candidate_sentinels = _assessment(plan, set(plan["tile_workset"]), judgments, sentinels)
    return {"status": "available", "reason_code": None, "assessment": assessment, "candidate_tile_judgments": candidate_judgments, "candidate_component_sentinels": candidate_sentinels, "captured_calls": progress["captured_calls"], "call_count": calls[0], "calls_avoided": plan["calls_avoided"], "reused_tile_count": 80 - len(plan["tile_workset"]), "evaluated_tile_count": len(plan["tile_workset"])}


def _partial_workset(_plan, healthy_tile_ids):
    _json(healthy_tile_ids)
    if type(healthy_tile_ids) is not list: _fail("healthy tile ids must be an array")
    if any(type(tile) is not str or not tile or tile != tile.strip() for tile in healthy_tile_ids): _fail("healthy tile ids are not canonical")
    if len(healthy_tile_ids) != len(set(healthy_tile_ids)): _fail("healthy tile ids contain duplicates")
    if any(tile not in _BY_TILE for tile in healthy_tile_ids): _fail("healthy tile ids contain unknown tiles")
    if healthy_tile_ids != sorted(healthy_tile_ids, key=_ORDER.__getitem__): _fail("healthy tile ids are not registry ordered")
    return list(healthy_tile_ids)


def _partial_evidence(rows):
    pairs = {}
    for row in rows:
        pair = row["evidence_ref"], row["evidence_fingerprint"]
        if pair in pairs and pairs[pair] != row: _fail("resolved evidence conflicts")
        pairs[pair] = row
    return [{key: row[key] for key in ("evidence_ref", "evidence_fingerprint", "content")} for _pair, row in sorted(pairs.items())]


def _partial_packets(partition, values):
    plan, rows = _plan(partition["judgment_delta"]["plan"]), partition["healthy_workset"]["tiles"]
    workset = _partial_workset(plan, [row["tile_id"] for row in rows])
    expected = {}
    for tile in rows:
        for binding in tile["current_evidence_bindings"]:
            identity = {key: binding[key] for key in ("evidence_record_id", "evidence_ref", "evidence_fingerprint")}
            if expected.setdefault(identity["evidence_record_id"], identity) != identity: _fail("signed evidence conflicts")
    if type(values) is not list: _fail("resolved evidence must be an array")
    resolved, pairs = {}, set()
    for raw in values:
        _fields(raw, frozenset({"evidence_record_id", "evidence_ref", "evidence_fingerprint", "content"}), "resolved evidence")
        identity = {key: raw[key] for key in ("evidence_record_id", "evidence_ref", "evidence_fingerprint")}
        record = {**identity, "content": _canon(raw["content"])}
        pair = identity["evidence_ref"], identity["evidence_fingerprint"]
        if expected.get(identity["evidence_record_id"]) != identity or identity["evidence_record_id"] in resolved or pair in pairs: _fail("resolved evidence does not match bindings")
        resolved[identity["evidence_record_id"]], pairs = record, pairs | {pair}
    if set(resolved) != set(expected): _fail("resolved evidence does not match bindings")
    hints = {}
    for hint in partition["evaluation_input"]["non_authoritative_hints"]:
        hints.setdefault(hint["component_key"], []).append(hint["evidence_record_id"])
    source = partition["evaluation_input"]["source_identity"]
    capture = {key: source[key] for key in ("capture_id", "capture_fingerprint")}
    operation = {"operation_id": source["operation_plan_id"], "operation_fingerprint": source["operation_fingerprint"]}
    packets = []
    for component in _COMPONENTS:
        tiles = [tile for tile in rows if tile["component_key"] == component]
        if tiles:
            materialized = []
            for tile in tiles:
                records = [resolved[row["evidence_record_id"]] for row in tile["current_evidence_bindings"]]
                if tile["route_source"] == "signed_hint_component_expansion" and not records:
                    records = [resolved[record] for record in hints.get(component, ()) if record in resolved]
                    if not records: _fail("signed hint expansion has no component evidence")
                materialized.append({"tile_id": tile["tile_id"], "evidence": _partial_evidence(records)})
            packets.append(build_evidence_packet(component_key=component, tiles=materialized, capture_origin=capture, operation_origin=operation, series_fingerprint=plan["current_series_fingerprint"]))
    return plan, workset, packets


def _partial_progress(status, reason, workset, judgments, sentinels, calls, call_count):
    return {
        "status": status, "reason_code": reason,
        "evaluated_tile_judgments": [judgments[tile] for tile in sorted(judgments, key=_ORDER.__getitem__)],
        "evaluated_component_sentinels": [sentinels[component] for component in _COMPONENTS if component in sentinels],
        "captured_calls": calls, "call_count": call_count,
        "evaluated_tile_count": len(judgments) + sum(len(_COMPONENT_TILES[component]) for component in sentinels),
    }


def _run_components(plan, packets, workset, responder, call_count, coherencia_state, lookup_evaluation=None, persist_evaluation=None, *, structural_status="invalid_input", structural_reason="invalid_input", failure_status="provider_failure", failure_reason="provider_failure"):
    workset, judgments, sentinels, calls = set(workset), {}, {}, []
    packets = [packet for packet in packets if packet["component_key"] != "coherencia"] + [packet for packet in packets if packet["component_key"] == "coherencia"]
    for index, packet in enumerate(packets):
        try:
            if packet["component_key"] == "coherencia" and coherencia_state == "blocked": _fail("blocked coherencia")
            upstream = _upstream(plan, workset, judgments, sentinels) if packet["component_key"] == "coherencia" else []
            request = _request(plan, packet, upstream)
        except Exception:
            return _partial_progress(structural_status, structural_reason, workset, judgments, sentinels, calls, call_count[0])
        recovered = lookup_evaluation(_canon(request)) if lookup_evaluation is not None else None
        try:
            raw = recovered
            if raw is None:
                outcome = _outcome(responder(_canon(request), index))
                if outcome is None or outcome.evaluation is None: _fail("component provider failure")
                raw = outcome.evaluation
            next_judgments, next_sentinels = dict(judgments), dict(sentinels)
            _accept(plan, request, raw, workset, next_judgments, next_sentinels)
            evaluation = _evaluation(raw, True)
        except Exception:
            return _partial_progress(failure_status, failure_reason, workset, judgments, sentinels, calls, call_count[0])
        produced_judgments = [next_judgments[tile] for tile in sorted(set(next_judgments) - set(judgments), key=_ORDER.__getitem__)]
        produced_sentinel = next((next_sentinels[component] for component in _COMPONENTS if component not in sentinels and component in next_sentinels), None)
        if recovered is None and persist_evaluation is not None:
            persist_evaluation(_canon(request), _canon(evaluation), _canon(produced_judgments), _canon(produced_sentinel))
        judgments, sentinels = next_judgments, next_sentinels
        calls.append({"request": request, "evaluation": evaluation})
    return _partial_progress("partial", None, workset, judgments, sentinels, calls, call_count[0])


def execute_partial_incremental_evaluation(workset_partition, resolved_evidence, flow, *, lookup_evaluation=None, persist_evaluation=None):
    """Evaluate the validated partition's scoreless healthy workset."""
    calls, workset = [0], []
    try:
        partition = validate_evidence_vault_sv9_workset_partition(workset_partition)
        plan, workset, packets = _partial_packets(partition, resolved_evidence)
        if lookup_evaluation is not None and not callable(lookup_evaluation): _fail("evaluation lookup is not callable")
        if persist_evaluation is not None and not callable(persist_evaluation): _fail("evaluation persistence is not callable")
    except Exception:
        return _partial_progress("invalid_input", "invalid_input", workset, {}, {}, [], calls[0])
    def call(request, _index):
        calls[0] += 1
        try: return flow.evaluate_component(request)
        except Exception: return ComponentEvaluationOutcome.provider_failure()
    return _run_components(plan, packets, workset, call, calls, partition["coherencia_dependency"]["state"], lookup_evaluation, persist_evaluation)


def replay_partial_incremental_evaluation(workset_partition, resolved_evidence, captured_calls, evaluation_state="partial"):
    """Replay exact strict partial calls without invoking Flow."""
    calls, workset = [0], []
    try:
        partition = validate_evidence_vault_sv9_workset_partition(workset_partition)
        plan, workset, packets = _partial_packets(partition, resolved_evidence)
        if type(evaluation_state) is not str or evaluation_state not in {"partial", "provider_failure"}: _fail("invalid replay state")
        _json(captured_calls)
        if type(captured_calls) is not list: _fail("captured calls do not match healthy workset")
        if evaluation_state == "partial":
            if len(captured_calls) != len(packets): _fail("captured calls do not match healthy workset")
            replay_packets = packets
        else:
            if not packets or len(captured_calls) >= len(packets): _fail("captured calls do not match failed prefix")
            replay_packets = packets[:len(captured_calls)]
        def call(request, index):
            calls[0] += 1
            raw = captured_calls[index]
            _fields(raw, frozenset({"request", "evaluation"}), "captured call")
            if raw["request"] != request: _fail("captured request does not match replay")
            return ComponentEvaluationOutcome.success(raw["evaluation"])
        result = _run_components(plan, replay_packets, workset, call, calls, partition["coherencia_dependency"]["state"], structural_status="invalid_replay", structural_reason="invalid_replay", failure_status="invalid_replay", failure_reason="invalid_replay")
        if result["status"] == "invalid_replay": return _partial_progress("invalid_replay", "invalid_replay", workset, {}, {}, [], calls[0])
        if evaluation_state == "provider_failure" and result["status"] == "partial":
            try:
                packet = packets[len(captured_calls)]
                if packet["component_key"] == "coherencia" and partition["coherencia_dependency"]["state"] == "blocked": _fail("blocked coherencia")
                upstream = _upstream(plan, set(workset), {row["tile_id"]: row for row in result["evaluated_tile_judgments"]}, {row["component_key"]: row for row in result["evaluated_component_sentinels"]}) if packet["component_key"] == "coherencia" else []
                _request(plan, packet, upstream)
            except Exception:
                return _partial_progress("invalid_replay", "invalid_replay", workset, {}, {}, [], calls[0])
            result["status"], result["reason_code"], result["call_count"] = "provider_failure", "provider_failure", len(captured_calls) + 1
        return result
    except Exception:
        return _partial_progress("invalid_replay", "invalid_replay", workset, {}, {}, [], calls[0])

def execute_incremental_evaluation(plan, evidence_packets, flow, *, lookup_evaluation=None, persist_evaluation=None):
    calls, avoided, reused = [0], [0], [0]
    try:
        bound = _plan(plan); avoided[0] = bound["calls_avoided"]; reused[0] = 80 - len(bound["tile_workset"]); packets = _packets(bound, evidence_packets)
        if lookup_evaluation is not None and not callable(lookup_evaluation): _fail("evaluation lookup is not callable")
        if persist_evaluation is not None and not callable(persist_evaluation): _fail("evaluation persistence is not callable")
        def call(request, _index):
            calls[0] += 1
            try:
                return flow.evaluate_component(request)
            except Exception:
                return ComponentEvaluationOutcome.provider_failure()
        result = _run(bound, packets, call, lookup_evaluation, persist_evaluation)
        return result or _pending("provider_failure", calls[0], avoided[0], reused[0])
    except Exception:
        return _pending("invalid_input", calls[0], avoided[0], reused[0])
def replay_incremental_evaluation(plan, evidence_packets, captured_calls):
    try:
        bound, packets = _plan(plan), _packets(_plan(plan), evidence_packets)
        _json(captured_calls)
        if type(captured_calls) is not list or len(captured_calls) != len(packets): _fail("captured calls do not match workset")
        def call(request, index):
            raw = captured_calls[index]
            _fields(raw, frozenset({"request", "evaluation"}), "captured call")
            if raw["request"] != request: _fail("captured request does not match replay")
            return ComponentEvaluationOutcome.success(raw["evaluation"])
        result = _run(bound, packets, call)
        return result or _pending("invalid_replay")
    except Exception:
        return _pending("invalid_replay")
def replay_incremental_evaluations(plan, evidence_packets, evaluations):
    """Replay stored signed evaluations after deterministically rebuilding requests."""
    try:
        bound, packets = _plan(plan), _packets(_plan(plan), evidence_packets)
        _json(evaluations)
        if type(evaluations) is not list or len(evaluations) != len(packets): _fail("evaluations do not match workset")
        result = _run(
            bound, packets,
            lambda _request, index: ComponentEvaluationOutcome.success(evaluations[index]),
        )
        return result or _pending("invalid_replay")
    except Exception:
        return _pending("invalid_replay")
# fmt: on
