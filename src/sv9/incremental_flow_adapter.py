"""Strict injected LLM boundary for SV9 component judgments."""

from __future__ import annotations

from typing import Any, Mapping

from src.sv9 import incremental_evaluation as ie
from src.sv9.shadow_component_provider import (
    ShadowProviderOutcome,
    request_binding as _request_binding,
    shadow_provider_environment_snapshot,
)

# fmt: off

class FlowSv9StrictComponentAdapterError(ValueError):
    pass


class FlowSv9StrictComponentAdapter:
    """Maps a dedicated raw-free component provider onto the strict Flow port."""

    def __init__(self, provider: Any, *, environ: Mapping[str, object] | None = None, model: object = None) -> None:
        self._provider, self._environ, self._model = provider, shadow_provider_environment_snapshot(environ if environ is not None else {}), model

    def evaluate_component(self, request: Mapping[str, Any]) -> ie.ComponentEvaluationOutcome:
        try:
            request = ie._canon(request)
            bound = _request_binding(request)
            if bound is None:
                return ie.ComponentEvaluationOutcome.provider_failure()
            component, tiles, evidence = bound
            outcome = self._provider.evaluate_component_json(request, environ=self._environ, model=self._model)
            if type(outcome) is not ShadowProviderOutcome or outcome.payload is None:
                return ie.ComponentEvaluationOutcome.provider_failure()
            raw = outcome.payload
            if set(raw) != {"component_key", "series_fingerprint", "request_fingerprint", "status", "tile_results"}: return ie.ComponentEvaluationOutcome.provider_failure()
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

# fmt: on
