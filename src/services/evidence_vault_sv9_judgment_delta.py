"""Pure VA1 evidence-to-tile projection before Vault judgment authority exists."""

from __future__ import annotations

from src.sv9 import incremental_planner as ip
from src.sv9 import judgment_memory as jm


EVIDENCE_IDENTITY_SET_VERSION = "evidence-vault-sv9-evidence-identity-set-v1"
AUTHORITATIVE_RELATION_VERSION = "evidence-vault-sv9-authoritative-tile-relation-v1"
JUDGMENT_DELTA_VERSION = "evidence-vault-sv9-judgment-delta-v1"
_IDENTITY_FIELDS = frozenset("schema_version evidence evidence_set_fingerprint".split())
_RELATION_FIELDS = frozenset(
    "schema_version tile_id component_key disposition evidence_ref evidence_fingerprint capture_origin operation_origin relation_fingerprint".split()
)
_DELTA_FIELDS = frozenset(
    "schema_version current_evidence prior_judgments authoritative_relations current_series_contract delta_projections coverage_loss unmapped_evidence plan canonical_delta_fingerprint".split()
)
_RELATION_INPUT = _RELATION_FIELDS - {"schema_version", "relation_fingerprint"}
_DELTA_INPUT = _DELTA_FIELDS - frozenset(
    "schema_version delta_projections coverage_loss unmapped_evidence plan canonical_delta_fingerprint".split()
)
_RELATION_DISPOSITIONS = frozenset({"relevant", "contradiction", "human_review_required"})


class EvidenceVaultSV9JudgmentDeltaError(jm.JudgmentMemoryContractError):
    """The caller did not provide a replayable, authoritative delta contract."""


def _fail(message):
    raise EvidenceVaultSV9JudgmentDeltaError(message)


def _builtin_json(value):
    try:
        ip._json_types(value)
    except (jm.JudgmentMemoryContractError, RecursionError) as exc:
        _fail(str(exc))


def build_evidence_identity_set(evidence=None, *, _raw=None, _signed=False):
    raw = _raw if _raw is not None else {"evidence": evidence}
    expected = _IDENTITY_FIELDS if _signed else {"evidence"}
    if _signed:
        _builtin_json(raw)
    if type(raw) is not dict or set(raw) != expected:
        _fail("evidence identity set fields do not match schema")
    if _signed and raw["schema_version"] != EVIDENCE_IDENTITY_SET_VERSION:
        _fail("unsupported evidence identity set")
    try:
        evidence = jm._evidence(raw["evidence"], "evidence")
    except jm.JudgmentMemoryContractError as exc:
        _fail(str(exc))
    result = {"schema_version": EVIDENCE_IDENTITY_SET_VERSION, "evidence": evidence}
    result["evidence_set_fingerprint"] = jm.canonical_fingerprint(
        "evidence-vault-sv9-evidence-identity-set-fingerprint-v1", result
    )
    if _signed and raw != result:
        _fail("evidence identity set replay mismatch")
    return result


def build_authoritative_evidence_tile_relation(*, _raw=None, _signed=False, **value):
    raw = _raw if _raw is not None else value
    expected = _RELATION_FIELDS if _signed else _RELATION_INPUT
    if _signed:
        _builtin_json(raw)
    if type(raw) is not dict or set(raw) != expected:
        _fail("authoritative relation fields do not match schema")
    if _signed and raw["schema_version"] != AUTHORITATIVE_RELATION_VERSION:
        _fail("unsupported authoritative relation")
    try:
        projection = jm.build_tile_evidence_delta_projection(
            tile_id=raw["tile_id"],
            component_key=raw["component_key"],
            disposition=raw["disposition"],
            evidence=[{"evidence_ref": raw["evidence_ref"], "evidence_fingerprint": raw["evidence_fingerprint"]}],
            capture_origin=raw["capture_origin"],
            operation_origin=raw["operation_origin"],
        )
    except jm.JudgmentMemoryContractError as exc:
        _fail(str(exc))
    if projection["disposition"] not in _RELATION_DISPOSITIONS:
        _fail("authoritative relation disposition is not safe")
    result = {
        "schema_version": AUTHORITATIVE_RELATION_VERSION,
        "tile_id": projection["tile_id"],
        "component_key": projection["component_key"],
        "disposition": projection["disposition"],
        **projection["evidence"][0],
        "capture_origin": projection["capture_origin"],
        "operation_origin": projection["operation_origin"],
    }
    result["relation_fingerprint"] = jm.canonical_fingerprint(
        "evidence-vault-sv9-authoritative-tile-relation-fingerprint-v1", result
    )
    if _signed and raw != result:
        _fail("authoritative relation replay mismatch")
    return result


def _prior(values):
    if type(values) is not list:
        _fail("prior judgments must be a JSON array")
    try:
        rows = [jm.validate_tile_judgment(value) for value in values]
    except jm.JudgmentMemoryContractError as exc:
        _fail(str(exc))
    if any(row["authority_state"] != "accepted" or row["lifecycle_state"] != "active" for row in rows):
        _fail("prior judgment is not an active accepted value")
    if len({row["tile_id"] for row in rows}) != len(rows):
        _fail("prior judgments contain duplicate tiles")
    return sorted(rows, key=lambda row: ip._ORDER[row["tile_id"]])


def _relations(values, current):
    if type(values) is not list:
        _fail("authoritative relations must be a JSON array")
    current_pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}
    rows, relation_keys, tile_shapes = [], set(), {}
    for value in values:
        row = build_authoritative_evidence_tile_relation(_raw=value, _signed=True)
        pair = row["evidence_ref"], row["evidence_fingerprint"]
        relation_key = row["tile_id"], *pair
        if pair not in current_pairs or relation_key in relation_keys:
            _fail("authoritative relation is stale or duplicate")
        relation_keys.add(relation_key)
        shape = row["disposition"], jm.canonical_json(row["capture_origin"]), jm.canonical_json(row["operation_origin"])
        if tile_shapes.setdefault(row["tile_id"], shape) != shape:
            _fail("authoritative relations conflict for a tile")
        rows.append(row)
    return sorted(rows, key=lambda row: (ip._ORDER[row["tile_id"]], row["evidence_ref"], row["evidence_fingerprint"]))


def _projections(relations, prior):
    prior_by_tile = {row["tile_id"]: row for row in prior}
    grouped = {}
    for row in relations:
        grouped.setdefault(row["tile_id"], []).append(row)
    result = []
    for tile, rows in grouped.items():
        old = {
            (row["evidence_ref"], row["evidence_fingerprint"])
            for row in prior_by_tile.get(tile, {}).get("supporting_evidence", [])
        }
        if rows[0]["disposition"] == "relevant":
            rows = [row for row in rows if (row["evidence_ref"], row["evidence_fingerprint"]) not in old]
        if not rows:
            continue
        first = rows[0]
        try:
            result.append(
                jm.build_tile_evidence_delta_projection(
                    tile_id=tile,
                    component_key=first["component_key"],
                    disposition=first["disposition"],
                    evidence=[{key: row[key] for key in ("evidence_ref", "evidence_fingerprint")} for row in rows],
                    capture_origin=first["capture_origin"],
                    operation_origin=first["operation_origin"],
                )
            )
        except jm.JudgmentMemoryContractError as exc:
            _fail(str(exc))
    return sorted(result, key=lambda row: ip._ORDER[row["tile_id"]])


def _coverage_loss(prior, current):
    current_pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}
    result = []
    for row in prior:
        missing = [
            value
            for value in row["supporting_evidence"]
            if (value["evidence_ref"], value["evidence_fingerprint"]) not in current_pairs
        ]
        if missing:
            result.append(
                {
                    "tile_id": row["tile_id"],
                    "component_key": row["component_key"],
                    "missing_evidence": missing,
                    "reason": "supporting_evidence_missing",
                }
            )
    return result


def build_evidence_vault_sv9_judgment_delta(*, _raw=None, _signed=False, **value):
    raw = _raw if _raw is not None else value
    expected = _DELTA_FIELDS if _signed else _DELTA_INPUT
    if type(raw) is not dict or set(raw) != expected:
        _fail("judgment delta fields do not match schema")
    if _signed and raw["schema_version"] != JUDGMENT_DELTA_VERSION:
        _fail("unsupported judgment delta")
    if _signed:
        _builtin_json(raw)
    current = build_evidence_identity_set(_raw=raw["current_evidence"], _signed=True)
    prior = _prior(raw["prior_judgments"])
    relations = _relations(raw["authoritative_relations"], current)
    try:
        series = jm.validate_judgment_series_contract(raw["current_series_contract"])
        plan = ip.build_incremental_plan(prior, _projections(relations, prior), series)
    except jm.JudgmentMemoryContractError as exc:
        _fail(str(exc))
    known = {
        (value["evidence_ref"], value["evidence_fingerprint"]) for row in prior for value in row["supporting_evidence"]
    }
    mapped = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in relations}
    unmapped = [
        {**row, "reason": "unmapped_current_evidence"}
        for row in current["evidence"]
        if (row["evidence_ref"], row["evidence_fingerprint"]) not in known | mapped
    ]
    result = {
        "schema_version": JUDGMENT_DELTA_VERSION,
        "current_evidence": current,
        "prior_judgments": prior,
        "authoritative_relations": relations,
        "current_series_contract": series,
        "delta_projections": plan["delta_projections"],
        "coverage_loss": _coverage_loss(prior, current),
        "unmapped_evidence": unmapped,
        "plan": plan,
    }
    result["canonical_delta_fingerprint"] = jm.canonical_fingerprint(
        "evidence-vault-sv9-judgment-delta-fingerprint-v1", result
    )
    if _signed and raw != result:
        _fail("judgment delta replay mismatch")
    return result


def validate_evidence_vault_sv9_judgment_delta(value):
    try:
        return build_evidence_vault_sv9_judgment_delta(_raw=value, _signed=True)
    except EvidenceVaultSV9JudgmentDeltaError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError) as exc:
        raise EvidenceVaultSV9JudgmentDeltaError("invalid signed judgment delta") from exc
