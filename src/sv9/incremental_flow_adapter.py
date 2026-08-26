"""Strict injected LLM boundary for SV9 component judgments."""

from __future__ import annotations

import json
from typing import Any, Mapping

from src.sv9 import incremental_evaluation as ie
from src.sv9 import judgment_memory as memory

# fmt: off

class FlowSv9StrictComponentAdapterError(ValueError):
    pass


_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["component_key", "series_fingerprint", "request_fingerprint", "status", "tile_results"],
    "properties": {
        "component_key": {"type": "string"}, "series_fingerprint": {"type": "string"},
        "request_fingerprint": {"type": "string"}, "status": {"enum": ["evaluated", "not_detected"]},
        "tile_results": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["tile_id", "assessment_state", "supporting_evidence"], "properties": {
                "tile_id": {"type": "string"}, "assessment_state": {"enum": ["ok", "no", "sin_evidencia"]},
                "supporting_evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "required": ["evidence_ref", "evidence_fingerprint"], "properties": {"evidence_ref": {"type": "string"}, "evidence_fingerprint": {"type": "string"}}}},
            }}},
    },
}


class FlowSv9StrictComponentAdapter:
    """Adapts an injected ``LLMAnalyzer._call_json`` to the strict Flow port."""

    def __init__(self, analyzer: Any) -> None:
        self._analyzer = analyzer

    def evaluate_component(self, request: Mapping[str, Any]) -> ie.ComponentEvaluationOutcome:
        try:
            bound = _request_binding(request)
            if bound is None:
                return ie.ComponentEvaluationOutcome.provider_failure()
            component, tiles, evidence = bound
            raw = self._analyzer._call_json(
                "Evaluate only the requested SV9 tiles. Return no prose or extra fields.",
                json.dumps(dict(request), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                max_tokens=6000, json_schema=_SCHEMA, schema_name="sv9_strict_component_evaluation", strict_schema=True, temperature=0.0, sanitized_diagnostics=True,
            )
            if not isinstance(raw, Mapping) or set(raw) != {"component_key", "series_fingerprint", "request_fingerprint", "status", "tile_results"}:
                return ie.ComponentEvaluationOutcome.provider_failure()
            result = ie.build_component_evaluation(**dict(raw))
            if (result["component_key"], result["series_fingerprint"], result["request_fingerprint"]) != (component, request["current_series_fingerprint"], request["canonical_request_fingerprint"]):
                return ie.ComponentEvaluationOutcome.provider_failure()
            rows = result["tile_results"]
            if result["status"] == "not_detected" and tuple(tiles) != ie._COMPONENT_TILES[component]:
                return ie.ComponentEvaluationOutcome.provider_failure()
            if result["status"] != "not_detected" and [row["tile_id"] for row in rows] != tiles:
                return ie.ComponentEvaluationOutcome.provider_failure()
            if any(item not in evidence[row["tile_id"]] for row in rows for item in row["supporting_evidence"]):
                return ie.ComponentEvaluationOutcome.provider_failure()
            return ie.ComponentEvaluationOutcome.success(result)
        except Exception:
            return ie.ComponentEvaluationOutcome.provider_failure()


def _request_binding(request: Mapping[str, Any]) -> tuple[str, list[str], dict[str, list[dict[str, str]]]] | None:
    try:
        if type(request) is not dict or set(request) != ie._REQUEST_FIELDS:
            raise ValueError
        component = ie._text(request["component_key"])
        if component not in ie._COMPONENT_TILES or ie._sha(request["current_series_fingerprint"]) != request["current_series_fingerprint"] or ie._sha(request["canonical_request_fingerprint"]) != request["canonical_request_fingerprint"]:
            raise ValueError
        unsigned = {key: request[key] for key in request if key != "canonical_request_fingerprint"}
        if memory.canonical_fingerprint(ie._REQUEST_FINGERPRINT, unsigned) != request["canonical_request_fingerprint"]:
            raise ValueError
        tiles, evidence = [], {}
        for row in request["requested_tiles"]:
            if set(row) != {"tile_id", "tile_key", "definition", "evidence"} or row["tile_id"] not in ie._COMPONENT_TILES[component]:
                raise ValueError
            tile = ie._text(row["tile_id"]); expected = ie._BY_TILE[tile]
            if row["tile_key"] != expected[2] or ie._canon(row["definition"]) != ie._canon(expected[3]): raise ValueError
            values = [{key: item[key] for key in ("evidence_ref", "evidence_fingerprint")} for item in ie._evidence(row["evidence"], True)]
            if tile in evidence:
                raise ValueError
            tiles.append(tile); evidence[tile] = values
        if not tiles or tiles != sorted(tiles, key=ie._ORDER.__getitem__):
            raise ValueError
        return component, tiles, evidence
    except Exception:
        return None
# fmt: on
