"""Strict scoreless routing overlay for signed SV9 evaluation inputs."""
from __future__ import annotations
from typing import Any, Mapping
from src.services.evidence_vault_canonical_core import build_tile_contract_registry, canonical_fingerprint, canonical_json
from src.services.evidence_vault_sv9_authoritative_relations import validate_evidence_vault_sv9_evaluation_input
from src.services.evidence_vault_sv9_judgment_delta import (
    JUDGMENT_DELTA_VERSION,
    validate_evidence_vault_sv9_judgment_delta,
)
from src.sv9 import incremental_planner as planner
EVIDENCE_VAULT_SV9_WORKSET_PARTITION_VERSION = "evidence-vault-sv9-workset-partition-v1"
_FINGERPRINT = "evidence-vault-sv9-workset-partition-fingerprint-v1"
_FIELDS = frozenset(
    "schema_version authority runtime_effect score_state evaluation_input judgment_delta input_binding healthy_workset "
    "allowed_reuse_tile_ids review_partition trusted_irrelevant_evidence pending_evidence coherencia_dependency "
    "partition_fingerprint".split()
)
_BINDING_FIELDS = frozenset("evaluation_input_fingerprint canonical_delta_fingerprint canonical_plan_fingerprint".split())
_HEALTHY_FIELDS = frozenset("state tiles".split())
_REVIEW_FIELDS = frozenset(
    "state tile_ids evaluation_input_reopened_tile_ids operational_authority_coverage_loss_tile_ids "
    "judgment_delta_coverage_loss_tile_ids planner_review_tile_ids coherencia_blocked_tile_ids".split()
)
_DEPENDENCY_FIELDS = frozenset("state reasons upstream_tile_ids".split())
_REGISTRY = build_tile_contract_registry()["tiles"]
_TILE_IDS = tuple(str(row["tile_id"]) for row in _REGISTRY)
_ORDER = {tile: index for index, tile in enumerate(_TILE_IDS)}
_COMPONENT = {str(row["tile_id"]): str(row["component_key"]) for row in _REGISTRY}
_COHERENCIA = "coherencia"
_COHERENCIA_TILES = tuple(tile for tile in _TILE_IDS if _COMPONENT[tile] == _COHERENCIA)
_UPSTREAM_TILES = tuple(tile for tile in _TILE_IDS if _COMPONENT[tile] != _COHERENCIA)
class EvidenceVaultSv9WorksetPartitionError(ValueError):
    """A signed routing-only partition cannot be replayed safely."""
def build_evidence_vault_sv9_workset_partition(
    *, evaluation_input: Any, judgment_delta: Any, trusted_irrelevant_evidence: Any = None
) -> dict[str, Any]:
    """Overlay scoreless healthy/review/reuse routing without changing canonical authority inputs."""
    try:
        source, delta, plan = _inputs(evaluation_input, judgment_delta)
        bindings = source["current_identity_bindings"]
        by_record = {row["evidence_record_id"]: row for row in bindings}
        unmapped = _unmapped(delta, bindings)
        hinted_tiles, hinted_records = _hints(source, delta, by_record, unmapped)
        trusted = _trusted(trusted_irrelevant_evidence, bindings, unmapped, hinted_records)
        trusted_records = {row["evidence_record_id"] for row in trusted}
        pending = [
            row for row in bindings if row["evidence_record_id"] in unmapped - hinted_records - trusted_records
        ]
        review = _review_causes(source, delta, plan)
        planned = set(plan["tile_workset"])
        sentinel_components = _reusable_sentinel_components(plan)
        expanded = {tile for tile in hinted_tiles if _COMPONENT[tile] in sentinel_components}
        expanded_components = {_COMPONENT[tile] for tile in expanded}
        expanded_tiles = {tile for tile in _TILE_IDS if _COMPONENT[tile] in expanded_components}
        healthy_ids = (planned | set(hinted_tiles) | expanded_tiles) - set(review["tile_ids"])
        dependency = _coherencia_dependency(healthy_ids, review["tile_ids"], pending)
        if dependency["state"] == "deferred":
            healthy_ids |= set(_COHERENCIA_TILES) - set(review["tile_ids"])
        elif dependency["state"] == "blocked":
            blocked = list(_COHERENCIA_TILES)
            review["coherencia_blocked_tile_ids"] = blocked
            review["tile_ids"] = _ordered(set(review["tile_ids"]) | set(blocked), "review tiles")
            review["state"] = "review_required"
            healthy_ids -= set(blocked)
        frozen = set(plan["frozen_set"])
        reusable = frozen - set(review["tile_ids"]) - healthy_ids
        _partition(healthy_ids, review["tile_ids"], reusable)
        rows = _healthy_rows(
            healthy_ids, plan, bindings, hinted_tiles, expanded_tiles, by_record, dependency["state"] == "deferred"
        )
        result = {
            "schema_version": EVIDENCE_VAULT_SV9_WORKSET_PARTITION_VERSION,
            "authority": False,
            "runtime_effect": "evaluation_routing_only",
            "score_state": "unavailable",
            "evaluation_input": source,
            "judgment_delta": delta,
            "input_binding": {
                "evaluation_input_fingerprint": source["evaluation_input_fingerprint"],
                "canonical_delta_fingerprint": delta["canonical_delta_fingerprint"],
                "canonical_plan_fingerprint": plan["canonical_plan_fingerprint"],
            },
            "healthy_workset": {"state": "evaluable" if rows else "empty", "tiles": rows},
            "allowed_reuse_tile_ids": _ordered(reusable, "allowed reuse tiles"),
            "review_partition": review,
            "trusted_irrelevant_evidence": trusted,
            "pending_evidence": pending,
            "coherencia_dependency": dependency,
        }
        result["partition_fingerprint"] = canonical_fingerprint(_FINGERPRINT, result)
        return result
    except EvidenceVaultSv9WorksetPartitionError:
        raise
    except Exception as exc:
        raise EvidenceVaultSv9WorksetPartitionError("SV9 workset partition is invalid") from exc
def validate_evidence_vault_sv9_workset_partition(value: Any) -> dict[str, Any]:
    """Replay a partition from its two signed immutable input contracts."""
    try:
        if type(value) is not dict or set(value) != _FIELDS:
            _fail("partition fields")
        if (
            value["schema_version"] != EVIDENCE_VAULT_SV9_WORKSET_PARTITION_VERSION
            or value["authority"] is not False
            or value["runtime_effect"] != "evaluation_routing_only"
            or value["score_state"] != "unavailable"
        ):
            _fail("partition envelope")
        _exact(value["input_binding"], _BINDING_FIELDS, "input binding")
        _exact(value["healthy_workset"], _HEALTHY_FIELDS, "healthy workset")
        _exact(value["review_partition"], _REVIEW_FIELDS, "review partition")
        _exact(value["coherencia_dependency"], _DEPENDENCY_FIELDS, "coherencia dependency")
        result = build_evidence_vault_sv9_workset_partition(
            evaluation_input=value["evaluation_input"],
            judgment_delta=value["judgment_delta"],
            trusted_irrelevant_evidence=value["trusted_irrelevant_evidence"],
        )
        if canonical_json(value) != canonical_json(result):
            _fail("partition replay mismatch")
        return result
    except EvidenceVaultSv9WorksetPartitionError:
        raise
    except Exception as exc:
        raise EvidenceVaultSv9WorksetPartitionError("SV9 workset partition is invalid") from exc
def _inputs(evaluation_input: Any, judgment_delta: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if type(evaluation_input) is not dict or type(evaluation_input.get("source_identity")) is not dict:
        _fail("evaluation input")
    try:
        source_id = evaluation_input["source_identity"]
        source = validate_evidence_vault_sv9_evaluation_input(
            evaluation_input, source_scan_id=source_id["source_scan_id"], workspace_slug=source_id["workspace_slug"]
        )
        delta = validate_evidence_vault_sv9_judgment_delta(judgment_delta)
        plan = planner.validate_incremental_plan(delta["plan"])
    except Exception as exc:
        raise EvidenceVaultSv9WorksetPartitionError("signed input is invalid") from exc
    if not all(canonical_json(raw) == canonical_json(rebuilt) for raw, rebuilt in ((evaluation_input, source), (judgment_delta, delta), (delta["plan"], plan))):
        _fail("signed input replay")
    if delta["schema_version"] != JUDGMENT_DELTA_VERSION or plan["schema_version"] != planner.PLAN_VERSION:
        _fail("signed input schema versions")
    if not _same(source["current_evidence"], delta["current_evidence"]["evidence"]) or not _same(source["authoritative_relations"], delta["authoritative_relations"]):
        _fail("input evidence or relations do not match delta")
    if plan["registry_tile_ids"] != list(_TILE_IDS) or plan["canonical_plan_fingerprint"] != delta["plan"]["canonical_plan_fingerprint"]:
        _fail("canonical plan registry")
    items = {row["tile_id"]: row for row in plan["items"]}
    if len(items) != len(_TILE_IDS) or set(items) != set(_TILE_IDS) or any(row["component_key"] != _COMPONENT[tile] for tile, row in items.items()):
        _fail("canonical plan items")
    frozen = set(plan["frozen_set"])
    if frozen != set(plan["reuse_set"]) | set(plan["unaffected_set"]):
        _fail("canonical reuse sets")
    _partition(set(plan["tile_workset"]), plan["review_set"], frozen)
    return source, delta, plan
def _unmapped(delta: Mapping[str, Any], bindings: list[dict[str, Any]]) -> set[str]:
    pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in delta["unmapped_evidence"]}
    by_pair = {(row["evidence_ref"], row["evidence_fingerprint"]): row["evidence_record_id"] for row in bindings}
    if len(pairs) != len(delta["unmapped_evidence"]) or not pairs <= set(by_pair):
        _fail("unmapped evidence")
    return {by_pair[pair] for pair in pairs}
def _hints(source: Mapping[str, Any], delta: Mapping[str, Any], by_record: Mapping[str, dict[str, Any]], unmapped: set[str]):
    mapped = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in delta["authoritative_relations"]}
    hinted_tiles: dict[str, set[str]] = {}
    records = set()
    for hint in source["non_authoritative_hints"]:
        record = by_record.get(hint["evidence_record_id"])
        if record is None or hint["evidence_record_id"] not in unmapped or (record["evidence_ref"], record["evidence_fingerprint"]) in mapped:
            _fail("hint is not delta-unmapped")
        hinted_tiles.setdefault(hint["tile_id"], set()).add(hint["evidence_record_id"])
        records.add(hint["evidence_record_id"])
    return hinted_tiles, records
def _trusted(value: Any, bindings: list[dict[str, Any]], unmapped: set[str], hinted: set[str]) -> list[dict[str, Any]]:
    values = [] if value is None else value
    if type(values) is not list:
        _fail("trusted irrelevant evidence")
    by_record = {row["evidence_record_id"]: row for row in bindings}
    selected = set()
    for raw in values:
        if type(raw) is not dict or type(raw.get("evidence_record_id")) is not str:
            _fail("trusted irrelevant evidence")
        record = by_record.get(raw["evidence_record_id"])
        if record is None or raw["evidence_record_id"] in selected | hinted | (set(by_record) - unmapped) or not _same(raw, record):
            _fail("trusted irrelevant evidence")
        selected.add(raw["evidence_record_id"])
    return [row for row in bindings if row["evidence_record_id"] in selected]
def _review_causes(source: Mapping[str, Any], delta: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    reopened = _ordered(source["reopen_tile_ids"], "reopened tiles")
    operational = _source_tiles(source["authority_coverage_loss"], "operational coverage loss")
    delta_loss = _source_tiles(delta["coverage_loss"], "delta coverage loss")
    planner_review = _ordered(plan["review_set"], "planner review tiles")
    tiles = _ordered(set(reopened) | set(operational) | set(delta_loss) | set(planner_review), "review tiles")
    return {
        "state": "review_required" if tiles else "clear", "tile_ids": tiles,
        "evaluation_input_reopened_tile_ids": reopened,
        "operational_authority_coverage_loss_tile_ids": operational,
        "judgment_delta_coverage_loss_tile_ids": delta_loss, "planner_review_tile_ids": planner_review,
        "coherencia_blocked_tile_ids": [],
    }
def _source_tiles(rows: Any, label: str) -> list[str]:
    if type(rows) is not list or any(type(row) is not dict or row.get("component_key") != _COMPONENT.get(row.get("tile_id")) for row in rows):
        _fail(label)
    return _ordered([row["tile_id"] for row in rows], label)
def _reusable_sentinel_components(plan: Mapping[str, Any]) -> set[str]:
    sentinels = {row["component_key"] for row in plan["prior_component_sentinels"]}
    reusable = set(plan["reuse_set"])
    return {component for component in sentinels if set(planner._COMPONENT_TILES[component]) <= reusable}
def _coherencia_dependency(healthy: set[str], review: list[str], pending: list[dict[str, Any]]) -> dict[str, Any]:
    dirty = sorted(healthy & set(_UPSTREAM_TILES), key=_ORDER.__getitem__)
    unresolved = sorted(set(review) & set(_UPSTREAM_TILES), key=_ORDER.__getitem__)
    reasons = (["upstream_healthy_work"] if dirty else []) + (["upstream_review_required"] if unresolved else [])
    upstream = set(dirty) | set(unresolved)
    if pending:
        reasons.append("pending_evidence_unresolved")
        upstream |= set(_UPSTREAM_TILES)
    state = "blocked" if unresolved or pending else "deferred" if dirty else "evaluable" if healthy & set(_COHERENCIA_TILES) else "clean"
    return {"state": state, "reasons": reasons, "upstream_tile_ids": _ordered(upstream, "coherencia upstream tiles")}
def _healthy_rows(healthy: set[str], plan: Mapping[str, Any], bindings: list[dict[str, Any]], hints: Mapping[str, set[str]], expanded: set[str], by_record: Mapping[str, dict[str, Any]], deferred: bool) -> list[dict[str, Any]]:
    items = {row["tile_id"]: row for row in plan["items"]}
    planned = set(plan["tile_workset"])
    rows = []
    for tile in _ordered(healthy, "healthy tiles"):
        pairs = (
            {(row["evidence_ref"], row["evidence_fingerprint"]) for row in items[tile]["evidence"]}
            if tile in planned else set()
        )
        records = hints.get(tile, set())
        if not pairs <= {(row["evidence_ref"], row["evidence_fingerprint"]) for row in bindings} or not records <= set(by_record):
            _fail("healthy evidence bindings")
        route = (
            "coherencia_deferred_dependency" if deferred and tile in _COHERENCIA_TILES else
            "signed_hint_component_sentinel" if tile in expanded and records else
            "signed_hint_component_expansion" if tile in expanded else
            "canonical_plan_and_signed_hint" if tile in planned and records else
            "canonical_plan" if tile in planned else "signed_hint"
        )
        current = [row for row in bindings if (row["evidence_ref"], row["evidence_fingerprint"]) in pairs or row["evidence_record_id"] in records]
        rows.append({"tile_id": tile, "component_key": _COMPONENT[tile], "route_source": route, "current_evidence_bindings": current})
    return rows
def _partition(healthy: set[str], review: Any, reusable: set[str]) -> None:
    reviewed = set(_ordered(review, "partition review tiles"))
    if healthy | reviewed | reusable != set(_TILE_IDS) or healthy & reviewed or healthy & reusable or reviewed & reusable:
        _fail("tile partition")
def _ordered(values: Any, label: str) -> list[str]:
    if type(values) not in {list, set, tuple}:
        _fail(label)
    rows = list(values)
    if any(type(tile) is not str or tile not in _ORDER for tile in rows) or len(rows) != len(set(rows)):
        _fail(label)
    return sorted(rows, key=_ORDER.__getitem__)
def _same(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)
def _exact(value: Any, fields: frozenset[str], label: str) -> None:
    if type(value) is not dict or set(value) != fields:
        _fail(label)
def _fail(label: str) -> None:
    raise EvidenceVaultSv9WorksetPartitionError(f"SV9 workset partition {label} is invalid")
