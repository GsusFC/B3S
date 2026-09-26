"""Pure VA1 evidence-to-tile projection before Vault judgment authority exists."""

from __future__ import annotations

from src.services import evidence_vault_sv9_support_continuity as continuity
from src.sv9 import incremental_planner as ip
from src.sv9 import judgment_memory as jm


EVIDENCE_IDENTITY_SET_VERSION = "evidence-vault-sv9-evidence-identity-set-v1"
AUTHORITATIVE_RELATION_VERSION = "evidence-vault-sv9-authoritative-tile-relation-v1"
JUDGMENT_DELTA_VERSION = "evidence-vault-sv9-judgment-delta-v2"
LEGACY_JUDGMENT_DELTA_VERSION = "evidence-vault-sv9-judgment-delta-v1"
# v3 is reserved for parked work; continuity-carrying deltas are v4.
SUPPORT_CONTINUITY_JUDGMENT_DELTA_VERSION = "evidence-vault-sv9-judgment-delta-v4"
_IDENTITY_FIELDS = frozenset("schema_version evidence evidence_set_fingerprint".split())
_RELATION_FIELDS = frozenset(
    "schema_version tile_id component_key disposition evidence_ref evidence_fingerprint capture_origin operation_origin relation_fingerprint".split()
)
_DELTA_FIELDS = frozenset(
    "schema_version current_evidence prior_judgments prior_component_sentinels authoritative_relations current_series_contract delta_projections coverage_loss unmapped_evidence plan canonical_delta_fingerprint".split()
)
_LEGACY_DELTA_FIELDS = _DELTA_FIELDS - {"prior_component_sentinels"}
_RELATION_INPUT = _RELATION_FIELDS - {"schema_version", "relation_fingerprint"}
_DELTA_INPUT = _DELTA_FIELDS - frozenset(
    "schema_version delta_projections coverage_loss unmapped_evidence plan canonical_delta_fingerprint".split()
)
_RELATION_DISPOSITIONS = frozenset({"relevant", "contradiction", "human_review_required"})
_DELTA_FINGERPRINT = "evidence-vault-sv9-judgment-delta-fingerprint-v2"
_LEGACY_DELTA_FINGERPRINT = "evidence-vault-sv9-judgment-delta-fingerprint-v1"
_SUPPORT_CONTINUITY_DELTA_FIELDS = _DELTA_FIELDS | {"support_continuity"}
_SUPPORT_CONTINUITY_DELTA_INPUT = _DELTA_INPUT | {"support_continuity"}
_SUPPORT_CONTINUITY_FIELDS = frozenset("rule_version capture_origin operation_origin carried".split())
_CARRIED_FIELDS = frozenset("tile_id source tier targets".split())
_SIMILARITY_FIELDS = frozenset("source_shingle_digest target_shingle_digest intersection union".split())
_SUPPORT_CONTINUITY_RULE_VERSIONS = frozenset({continuity.SUPPORT_CONTINUITY_RULE_VERSION})
_SUPPORT_CONTINUITY_DELTA_FINGERPRINT = "evidence-vault-sv9-judgment-delta-fingerprint-v4"


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


def _prior_sentinels(values):
    if type(values) is not list:
        _fail("prior component sentinels must be a JSON array")
    try:
        rows = [ip.validate_component_not_detected_sentinel(value) for value in values]
    except jm.JudgmentMemoryContractError as exc:
        _fail(str(exc))
    if any(row["authority_state"] != "accepted" or row["lifecycle_state"] != "active" for row in rows):
        _fail("prior component sentinel is not an active accepted value")
    if len({row["component_key"] for row in rows}) != len(rows):
        _fail("prior component sentinels contain duplicate components")
    return sorted(rows, key=lambda row: list(ip._COMPONENT_TILES).index(row["component_key"]))


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


def _support_continuity(raw, prior, current):
    if type(raw) is not dict or set(raw) != _SUPPORT_CONTINUITY_FIELDS:
        _fail("support continuity fields do not match schema")
    if raw["rule_version"] not in _SUPPORT_CONTINUITY_RULE_VERSIONS:
        _fail("unsupported support continuity rule")
    if type(raw["carried"]) is not list:
        _fail("carried support must be a JSON array")
    prior_by_tile = {row["tile_id"]: row for row in prior}
    current_pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}
    try:
        origins = {
            "capture_origin": jm._origin(raw["capture_origin"], "capture"),
            "operation_origin": jm._origin(raw["operation_origin"], "operation"),
        }
        rows = [_carried_entry(value, prior_by_tile, current_pairs) for value in raw["carried"]]
    except jm.JudgmentMemoryContractError as exc:
        _fail(str(exc))
    keys = [(row["tile_id"], row["source"]["evidence_ref"], row["source"]["evidence_fingerprint"]) for row in rows]
    if len(set(keys)) != len(keys):
        _fail("carried support contains duplicate entries")
    rows.sort(
        key=lambda row: (
            ip._ORDER[row["tile_id"]],
            row["source"]["evidence_ref"],
            row["source"]["evidence_fingerprint"],
        )
    )
    return {"rule_version": raw["rule_version"], **origins, "carried": rows}


def _carried_entry(value, prior_by_tile, current_pairs):
    tier = value.get("tier") if type(value) is dict else None
    expected = _CARRIED_FIELDS | ({"similarity"} if tier == continuity.OWNED_PAGE_SIMILARITY_TIER else set())
    if tier not in continuity.SUPPORT_CONTINUITY_TIERS or set(value) != expected:
        _fail("carried support entry fields do not match schema")
    prior_row = prior_by_tile.get(value["tile_id"]) if type(value["tile_id"]) is str else None
    if prior_row is None:
        _fail("carried support tile has no accepted prior judgment")
    source = jm._evidence([value["source"]], "carried source")[0]
    if source not in prior_row["supporting_evidence"]:
        _fail("carried source is not accepted support for the tile")
    if (source["evidence_ref"], source["evidence_fingerprint"]) in current_pairs:
        _fail("carried source is still present exactly")
    targets = jm._evidence(value["targets"], "carried targets")
    if not targets or any((row["evidence_ref"], row["evidence_fingerprint"]) not in current_pairs for row in targets):
        _fail("carried targets are not current evidence")
    entry = {"tile_id": value["tile_id"], "source": source, "tier": tier, "targets": targets}
    if tier == continuity.CANONICAL_TIER:
        if len(targets) != 1:
            _fail("canonical continuity carries exactly one target")
        return entry
    return {**entry, "similarity": _similarity(value["similarity"])}


def _similarity(raw):
    if type(raw) is not dict or set(raw) != _SIMILARITY_FIELDS:
        _fail("owned page similarity fields do not match schema")
    intersection, union = raw["intersection"], raw["union"]
    if (
        type(union) is not int
        or union < continuity.MIN_PAGE_SHINGLES
        or not continuity.meets_similarity_threshold(intersection, union)
    ):
        _fail("owned page similarity does not meet the carry threshold")
    return {
        "source_shingle_digest": jm._sha(raw["source_shingle_digest"]),
        "target_shingle_digest": jm._sha(raw["target_shingle_digest"]),
        "intersection": intersection,
        "union": union,
    }


def _carried_projections(projections, relations, prior, current, support_continuity):
    """Replace a carried tile's projection with its full current support, one row per tile."""
    current_pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}
    prior_by_tile = {row["tile_id"]: row for row in prior}
    by_tile = {row["tile_id"]: row for row in projections}
    relevant, targets = {}, {}
    for row in relations:
        if row["disposition"] == "relevant":
            relevant.setdefault(row["tile_id"], []).append(
                {key: row[key] for key in ("evidence_ref", "evidence_fingerprint")}
            )
    for row in support_continuity["carried"]:
        targets.setdefault(row["tile_id"], []).extend(row["targets"])
    for tile, carried in targets.items():
        existing = by_tile.get(tile)
        if existing is not None and existing["disposition"] != "relevant":
            # A contradiction or explicit review relation keeps the tile in review.
            continue
        present = [
            value
            for value in prior_by_tile[tile]["supporting_evidence"]
            if (value["evidence_ref"], value["evidence_fingerprint"]) in current_pairs
        ]
        evidence = {
            (row["evidence_ref"], row["evidence_fingerprint"]): row
            for row in [*present, *carried, *relevant.get(tile, [])]
        }
        by_tile[tile] = jm.build_tile_evidence_delta_projection(
            tile_id=tile,
            component_key=prior_by_tile[tile]["component_key"],
            disposition="relevant",
            evidence=list(evidence.values()),
            capture_origin=support_continuity["capture_origin"],
            operation_origin=support_continuity["operation_origin"],
        )
    return sorted(by_tile.values(), key=lambda row: ip._ORDER[row["tile_id"]])


def _coverage_loss(prior, current, carried=()):
    current_pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]}
    carried_keys = {
        (row["tile_id"], row["source"]["evidence_ref"], row["source"]["evidence_fingerprint"]) for row in carried
    }
    result = []
    for row in prior:
        missing = [
            value
            for value in row["supporting_evidence"]
            if (value["evidence_ref"], value["evidence_fingerprint"]) not in current_pairs
            and (row["tile_id"], value["evidence_ref"], value["evidence_fingerprint"]) not in carried_keys
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


def build_evidence_vault_sv9_judgment_delta(
    *, _raw=None, _signed=False, prior_component_sentinels=None, support_continuity=None, **value
):
    raw = (
        _raw
        if _raw is not None
        else value
        | {"prior_component_sentinels": [] if prior_component_sentinels is None else prior_component_sentinels}
        | ({} if support_continuity is None else {"support_continuity": support_continuity})
    )
    version = (
        raw.get("schema_version")
        if _signed and type(raw) is dict
        else SUPPORT_CONTINUITY_JUDGMENT_DELTA_VERSION
        if support_continuity is not None
        else JUDGMENT_DELTA_VERSION
    )
    legacy = version == LEGACY_JUDGMENT_DELTA_VERSION
    carrying = version == SUPPORT_CONTINUITY_JUDGMENT_DELTA_VERSION
    if _signed:
        expected = _LEGACY_DELTA_FIELDS if legacy else _SUPPORT_CONTINUITY_DELTA_FIELDS if carrying else _DELTA_FIELDS
    else:
        expected = _SUPPORT_CONTINUITY_DELTA_INPUT if carrying else _DELTA_INPUT
    if type(raw) is not dict or set(raw) != expected:
        _fail("judgment delta fields do not match schema")
    if _signed and version not in (
        LEGACY_JUDGMENT_DELTA_VERSION,
        JUDGMENT_DELTA_VERSION,
        SUPPORT_CONTINUITY_JUDGMENT_DELTA_VERSION,
    ):
        _fail("unsupported judgment delta")
    if _signed:
        _builtin_json(raw)
    current = build_evidence_identity_set(_raw=raw["current_evidence"], _signed=True)
    prior = _prior(raw["prior_judgments"])
    sentinels = [] if legacy else _prior_sentinels(raw["prior_component_sentinels"])
    relations = _relations(raw["authoritative_relations"], current)
    carrying_value = _support_continuity(raw["support_continuity"], prior, current) if carrying else None
    carried = [] if carrying_value is None else carrying_value["carried"]
    try:
        series = jm.validate_judgment_series_contract(raw["current_series_contract"])
        projections = _projections(relations, prior)
        if carried:
            projections = _carried_projections(projections, relations, prior, current, carrying_value)
        plan = (ip.build_incremental_plan_v2 if legacy else ip.build_incremental_plan)(
            prior, projections, series, prior_component_sentinels=sentinels
        )
    except jm.JudgmentMemoryContractError as exc:
        _fail(str(exc))
    known = {
        (value["evidence_ref"], value["evidence_fingerprint"]) for row in prior for value in row["supporting_evidence"]
    }
    mapped = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in relations}
    targets = {(value["evidence_ref"], value["evidence_fingerprint"]) for row in carried for value in row["targets"]}
    unmapped = [
        {**row, "reason": "unmapped_current_evidence"}
        for row in current["evidence"]
        if (row["evidence_ref"], row["evidence_fingerprint"]) not in known | mapped | targets
    ]
    result = {
        "schema_version": version,
        "current_evidence": current,
        "prior_judgments": prior,
        **({"prior_component_sentinels": sentinels} if not legacy else {}),
        "authoritative_relations": relations,
        **({"support_continuity": carrying_value} if carrying else {}),
        "current_series_contract": series,
        "delta_projections": plan["delta_projections"],
        "coverage_loss": _coverage_loss(prior, current, carried),
        "unmapped_evidence": unmapped,
        "plan": plan,
    }
    result["canonical_delta_fingerprint"] = jm.canonical_fingerprint(
        _LEGACY_DELTA_FINGERPRINT
        if legacy
        else _SUPPORT_CONTINUITY_DELTA_FINGERPRINT
        if carrying
        else _DELTA_FINGERPRINT,
        result,
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
