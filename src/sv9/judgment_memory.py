"""Pure, versioned SV9 tile-judgment value contracts."""

import hashlib, json, re

from src.sv9 import assessment_kernel as _kernel
from src.sv9.rubric import RUBRIC_VERSION, TILE_ESTADOS

CANONICAL_JSON_VERSION = "sv9-judgment-memory-canonical-json-v1"
SERIES_VERSION = "sv9-judgment-series-contract-v1"
JUDGMENT_VERSION = "sv9-tile-judgment-v1"
DELTA_VERSION = "sv9-tile-evidence-delta-projection-v1"

_AUTHORITY = frozenset({"accepted", "pending", "rejected"})
_REVIEW = frozenset({"none", "required", "in_review", "resolved"})
_LIFECYCLE = frozenset({"active", "reopened", "superseded"})
_DELTA = frozenset({"unchanged", "irrelevant", "relevant", "contradiction", "human_review_required"})
_SERIES_TEXT = ("evaluator_version", "prompt_version", "model_version", "flow_version", "normalization_version")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_FIELDS = frozenset({"evidence_ref", "evidence_fingerprint"})
_SERIES_FIELDS = frozenset(
    "schema_version rubric_version tile_registry_fingerprint scoring_policy_version evaluator_version prompt_version model_version flow_version normalization_version".split()
)
_JUDGMENT_FIELDS = frozenset(
    "schema_version tile_id component_key assessment_state supporting_evidence capture_origin operation_origin series_contract series_fingerprint authority_state review_state lifecycle_state lifecycle_reason canonical_judgment_fingerprint".split()
)
_DELTA_FIELDS = frozenset(
    "schema_version tile_id component_key disposition evidence capture_origin operation_origin projection_fingerprint".split()
)
_JUDGMENT_INPUT = _JUDGMENT_FIELDS - {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint"}
_DELTA_INPUT = _DELTA_FIELDS - {"schema_version", "projection_fingerprint"}
_SERIES_AUTHORITY = (
    ("rubric_version", RUBRIC_VERSION),
    ("tile_registry_fingerprint", _kernel.SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT),
    ("scoring_policy_version", _kernel.SV9_SCORING_POLICY_VERSION),
)


class JudgmentMemoryContractError(ValueError):
    pass


def canonical_json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise JudgmentMemoryContractError("value is not canonical JSON") from exc


def canonical_fingerprint(namespace, value):
    payload = {"version": CANONICAL_JSON_VERSION, "namespace": _text(namespace), "payload": value}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_judgment_series_contract(*, _raw=None, _signed=False, **versions):
    raw = _raw or {
        "schema_version": SERIES_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "tile_registry_fingerprint": _kernel.SV9_TILE_CONTRACT_REGISTRY_FINGERPRINT,
        "scoring_policy_version": _kernel.SV9_SCORING_POLICY_VERSION,
        **versions,
    }
    _fields(raw, _SERIES_FIELDS, "series contract")
    if raw["schema_version"] != SERIES_VERSION:
        raise JudgmentMemoryContractError("unsupported series contract")
    if any(raw[key] != expected for key, expected in _SERIES_AUTHORITY):
        raise JudgmentMemoryContractError("series constants do not match executable SV9 authority")
    result = {
        "schema_version": SERIES_VERSION,
        "rubric_version": _text(raw["rubric_version"]),
        "tile_registry_fingerprint": _sha(raw["tile_registry_fingerprint"]),
        "scoring_policy_version": _text(raw["scoring_policy_version"]),
        **{key: _text(raw[key]) for key in _SERIES_TEXT},
    }
    if _signed and raw != result:
        raise JudgmentMemoryContractError("series replay is not canonical")
    return result


def validate_judgment_series_contract(value):
    return build_judgment_series_contract(_raw=value, _signed=True)


def build_tile_judgment(*, _raw=None, _signed=False, **value):
    raw = _raw if _raw is not None else value
    _fields(raw, _JUDGMENT_FIELDS if _signed else _JUDGMENT_INPUT, "judgment")
    if _signed and raw["schema_version"] != JUDGMENT_VERSION:
        raise JudgmentMemoryContractError("unsupported judgment schema")
    tile_id, component = _tile(raw["tile_id"], raw["component_key"])
    evidence = _evidence(raw["supporting_evidence"], "supporting_evidence")
    state = _one_of(raw["assessment_state"], TILE_ESTADOS, "assessment_state")
    lifecycle = _one_of(raw["lifecycle_state"], _LIFECYCLE, "lifecycle_state")
    reason = _text(raw["lifecycle_reason"], "lifecycle_reason", allow_empty=True)
    if (state in {"ok", "no"}) != bool(evidence):
        raise JudgmentMemoryContractError("assessment state and supporting evidence conflict")
    if (lifecycle == "active") != (reason == ""):
        raise JudgmentMemoryContractError("only reopened or superseded judgments require a lifecycle reason")
    authority = _one_of(raw["authority_state"], _AUTHORITY, "authority_state")
    review = _one_of(raw["review_state"], _REVIEW, "review_state")
    series = build_judgment_series_contract(_raw=raw["series_contract"])
    result = {
        "schema_version": JUDGMENT_VERSION,
        "tile_id": tile_id,
        "component_key": component,
        "assessment_state": state,
        "supporting_evidence": evidence,
        "capture_origin": _origin(raw["capture_origin"], "capture"),
        "operation_origin": _origin(raw["operation_origin"], "operation"),
        "series_contract": series,
        "series_fingerprint": canonical_fingerprint("sv9-judgment-series-fingerprint-v1", series),
        "authority_state": authority,
        "review_state": review,
        "lifecycle_state": lifecycle,
        "lifecycle_reason": reason,
    }
    result["canonical_judgment_fingerprint"] = canonical_fingerprint("sv9-tile-judgment-fingerprint-v1", result)
    if _signed and raw != result:
        raise JudgmentMemoryContractError("judgment replay fingerprint or canonical value mismatch")
    return result


def validate_tile_judgment(value):
    return build_tile_judgment(_raw=value, _signed=True)


def build_tile_evidence_delta_projection(*, _raw=None, _signed=False, **value):
    raw = _raw if _raw is not None else value
    _fields(raw, _DELTA_FIELDS if _signed else _DELTA_INPUT, "delta projection")
    if _signed and raw["schema_version"] != DELTA_VERSION:
        raise JudgmentMemoryContractError("unsupported delta projection schema")
    tile_id, component = _tile(raw["tile_id"], raw["component_key"])
    evidence = _evidence(raw["evidence"], "evidence")
    if not evidence:
        raise JudgmentMemoryContractError("delta projection requires evidence")
    result = {
        "schema_version": DELTA_VERSION,
        "tile_id": tile_id,
        "component_key": component,
        "disposition": _one_of(raw["disposition"], _DELTA, "disposition"),
        "evidence": evidence,
        "capture_origin": _origin(raw["capture_origin"], "capture"),
        "operation_origin": _origin(raw["operation_origin"], "operation"),
    }
    result["projection_fingerprint"] = canonical_fingerprint("sv9-tile-evidence-delta-fingerprint-v1", result)
    if _signed and raw != result:
        raise JudgmentMemoryContractError("delta replay fingerprint or canonical value mismatch")
    return result


def validate_tile_evidence_delta_projection(value):
    return build_tile_evidence_delta_projection(_raw=value, _signed=True)


def _registry():
    source = _kernel.build_sv9_tile_contract_registry()
    rows = {t["tile_id"]: c["component_key"] for c in source["components"] for t in c["tiles"]}
    if len(rows) != source["tile_count"]:
        raise JudgmentMemoryContractError("SV9 tile registry contains duplicate ids")
    return rows


def _tile(tile_id, component_key):
    component = _registry().get(tile_id := _text(tile_id))
    if component is None or _text(component_key) != component:
        raise JudgmentMemoryContractError("unknown tile or mismatched component")
    return tile_id, component


def _evidence(values, label):
    records = []
    for raw in _rows(values, label):
        _fields(raw, _EVIDENCE_FIELDS, label)
        record = {"evidence_ref": _text(raw["evidence_ref"]), "evidence_fingerprint": _sha(raw["evidence_fingerprint"])}
        records.append(record)
    if any(len({row[key] for row in records}) != len(records) for key in _EVIDENCE_FIELDS):
        raise JudgmentMemoryContractError(f"{label} contains duplicate evidence")
    return sorted(records, key=lambda row: (row["evidence_ref"], row["evidence_fingerprint"]))


def _origin(raw, prefix):
    _fields(raw, frozenset({f"{prefix}_id", f"{prefix}_fingerprint"}), f"{prefix}_origin")
    return {f"{prefix}_id": _text(raw[f"{prefix}_id"]), f"{prefix}_fingerprint": _sha(raw[f"{prefix}_fingerprint"])}


def _fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise JudgmentMemoryContractError(f"{label} fields do not match schema")


def _rows(values, label):
    if not isinstance(values, list):
        raise JudgmentMemoryContractError(f"{label} must be an array")
    return values


def _text(value, label="value", *, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not (value := value.strip())):
        raise JudgmentMemoryContractError(f"{label} must be non-empty text")
    return value.strip()


def _sha(value):
    value = _text(value).lower()
    if not _SHA256.fullmatch(value):
        raise JudgmentMemoryContractError("value must be a sha256 fingerprint")
    return value


def _one_of(value, allowed, label):
    if (value := _text(value)) not in allowed:
        raise JudgmentMemoryContractError(f"{label} is invalid")
    return value
