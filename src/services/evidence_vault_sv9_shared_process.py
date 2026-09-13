"""Vault bridge that reuses the Core Flow/SV9 analysis process."""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from src.sv9 import incremental_evaluation as ie
from src.sv9 import judgment_memory
from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapterError

# fmt: off

CORE_SHARED_EVALUATOR_VERSION = "sv9-core-shared-evaluator-v1"
CORE_SHARED_PROMPT_VERSION = (
    "sv9-flow-brand-interpretation-v1.2+"
    "baldosas-v3.1-evaluator-v2+sv9-editorial-v3.1"
)
CORE_SHARED_FLOW_VERSION = "sv9-flow-sv9-shadow-eval-v1"
CORE_SHARED_NORMALIZATION_VERSION = "vault-capture-v1"
_SHARED_CHECKPOINT_PROCESS_VERSION = (
    "evidence-vault-sv9-shared-checkpoint-process-v1"
)
_SHARED_CHECKPOINT_SNAPSHOT_FINGERPRINT_VERSION = (
    "evidence-vault-sv9-shared-checkpoint-snapshot-v1"
)


def _assessment_output_from_scanner_envelope(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Recover the canonical kernel output from Core's scanner envelope."""

    from src.sv9.assessment_kernel import validate_sv9_assessment_output

    if not isinstance(value, Mapping):
        raise FlowSv9StrictComponentAdapterError(
            "Core scanner assessment is invalid"
        )
    output = {
        key: copy.deepcopy(child)
        for key, child in value.items()
        if key
        not in {
            "assessment_schema_version",
            "expected_tile_count",
            "availability",
            "reason_codes",
        }
    }
    output["schema_version"] = value.get("assessment_schema_version")
    try:
        validate_sv9_assessment_output(output)
    except Exception as exc:
        raise FlowSv9StrictComponentAdapterError(
            "Core scanner assessment is invalid"
        ) from exc
    return output


def _shared_json_copy(value: Any) -> Any:
    """Copy full Core JSON without applying the integer-only delta contract."""

    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise FlowSv9StrictComponentAdapterError(
            "shared analysis JSON is invalid"
        ) from exc


def build_core_shared_series_contract(
    *,
    interpretation_model: str,
    labeling_model: str,
    adjudicator_model: str,
    evaluator_model: str,
    reasoning_model: str,
    editorial_model: str,
    gate_authority: str,
    editorial_enabled: bool,
) -> dict[str, Any]:
    """Bind every model and executable version used by shared Core analysis."""

    from scripts.sv9_flow_sv9_shadow_eval import SV9_FLOW_SV9_SHADOW_EVAL_VERSION
    from src.sv9.editorial import SV9_EDITORIAL_PROMPT_VERSION
    from src.sv9.evaluator import SV9_EVALUATOR_PROMPT_VERSION
    from src.sv9.flow_ingress import SV9_FLOW_INGRESS_VERSION
    from src.sv9_flow.block_evidence_worker import (
        BLOCK_EVIDENCE_IDENTITY_GATE_VERSION,
        BLOCK_EVIDENCE_SHORTLIST_VERSION,
        EVALUATION_EVIDENCE_REFS_VERSION,
    )
    from src.sv9_flow.contracts import SV9_FLOW_CANDIDATE_VERSION
    from src.sv9_flow.evidence_labeling_worker import EVIDENCE_LABELING_VERSION
    from src.sv9_flow.interpretation_llm_worker import (
        FLOW_INTERPRETATION_PROMPT_VERSION,
    )

    roles = {
        "interpretation": _series_text(interpretation_model),
        "labeling": _series_text(labeling_model),
        "adjudicator": _series_text(adjudicator_model),
        "evaluator": _series_text(evaluator_model),
        "reasoning": _series_text(reasoning_model),
        "editorial": _series_text(editorial_model),
    }
    model_version = judgment_memory.canonical_json(
        {
            "schema_version": "sv9-core-shared-model-bundle-v1",
            "roles": roles,
            "gate_authority": _series_text(gate_authority),
            "editorial_enabled": bool(editorial_enabled),
            "versions": {
                "flow_candidate": SV9_FLOW_CANDIDATE_VERSION,
                "flow_interpretation_prompt": FLOW_INTERPRETATION_PROMPT_VERSION,
                "flow_evidence_labeling": EVIDENCE_LABELING_VERSION,
                "flow_evidence_shortlist": BLOCK_EVIDENCE_SHORTLIST_VERSION,
                "flow_evidence_identity_gate": BLOCK_EVIDENCE_IDENTITY_GATE_VERSION,
                "flow_evaluation_evidence_refs": EVALUATION_EVIDENCE_REFS_VERSION,
                "flow_ingress": SV9_FLOW_INGRESS_VERSION,
                "sv9_evaluator_prompt": SV9_EVALUATOR_PROMPT_VERSION,
                "sv9_editorial_prompt": SV9_EDITORIAL_PROMPT_VERSION,
                "analysis_envelope": SV9_FLOW_SV9_SHADOW_EVAL_VERSION,
            },
        }
    )
    return judgment_memory.build_judgment_series_contract(
        evaluator_version=CORE_SHARED_EVALUATOR_VERSION,
        prompt_version=CORE_SHARED_PROMPT_VERSION,
        model_version=model_version,
        flow_version=CORE_SHARED_FLOW_VERSION,
        normalization_version=CORE_SHARED_NORMALIZATION_VERSION,
    )


def _series_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise FlowSv9StrictComponentAdapterError(
            "shared series model identity is incomplete"
        )
    return text


def is_core_shared_series_contract(value: Any) -> bool:
    try:
        contract = judgment_memory.validate_judgment_series_contract(value)
    except Exception:
        return False
    return all(
        contract[key] == expected
        for key, expected in {
            "evaluator_version": CORE_SHARED_EVALUATOR_VERSION,
            "prompt_version": CORE_SHARED_PROMPT_VERSION,
            "flow_version": CORE_SHARED_FLOW_VERSION,
            "normalization_version": CORE_SHARED_NORMALIZATION_VERSION,
        }.items()
    )


class CoreFlowSv9StrictComponentAdapter:
    """Run the existing Core Flow/evaluator lazily for a Vault workset.

    The adapter never changes Core prompts or evaluator behavior.  It only
    translates selected Core tile results into the existing request-bound
    incremental port and keeps frozen component results from the previously
    accepted private analysis snapshot.
    """

    def __init__(
        self,
        *,
        snapshot: Mapping[str, Any],
        source_run_id: str,
        interpretation_llm_factory: Any,
        adjudicator_llm_factory: Any,
        labeling_llm_factory: Any,
        evaluator_llm_factory: Any,
        reasoning_llm_factory: Any,
        gate_authority: str,
        prior_shared_analysis: Mapping[str, Any] | None = None,
        payload_context: Mapping[str, Any] | None = None,
        payload_finalizer: Any | None = None,
    ) -> None:
        self._snapshot = _shared_json_copy(dict(snapshot))
        self._source_run_id = str(source_run_id)
        self._llm_factories = {
            "interpretation": interpretation_llm_factory,
            "adjudicator": adjudicator_llm_factory,
            "labeling": labeling_llm_factory,
            "evaluator": evaluator_llm_factory,
            "reasoning": reasoning_llm_factory,
        }
        self._interpretation_llm = None
        self._adjudicator_llm = None
        self._labeling_llm = None
        self._evaluator_llm = None
        self._reasoning_llm = None
        self._gate_authority = str(gate_authority)
        self._payload_context = _shared_json_copy(dict(payload_context or {}))
        self._payload_finalizer = payload_finalizer
        self._candidate = None
        self._debug: dict[str, Any] | None = None
        self._tldr: dict[str, Any] | None = None
        self._signals: dict[str, list[dict[str, Any]]] | None = None
        self._visual_evidence_packet: dict[str, Any] | None = None
        self._components = _components_from_shared_analysis(prior_shared_analysis)
        self._component_provenance_candidates = (
            _component_provenance_candidates_from_shared_analysis(
                prior_shared_analysis,
                components=self._components,
            )
        )
        self._prior_projected_components = self._project_source_policy_graph(
            self._components
        )
        self._restored_checkpoint_components: dict[str, Any] = {}
        self._restored_flow_context: dict[str, Any] | None = None
        self._restored_llm_usage: dict[str, Any] | None = None

    def evaluate_component(self, request: Mapping[str, Any]) -> ie.ComponentEvaluationOutcome:
        component: Any = "unknown"
        suboperation = "request_validation"
        try:
            request = ie._canon(dict(request))
            component = request.get("component_key", "unknown")
            if not is_core_shared_series_contract(request.get("current_series_contract")):
                raise FlowSv9StrictComponentAdapterError("shared series is invalid")
            suboperation = "prepare"
            self._prepare()
            suboperation = "initialize_evaluation_llms"
            self._initialize_evaluation_llms()
            suboperation = "request_workset_validation"
            component = str(request["component_key"])
            requested = [str(row["tile_id"]) for row in request["requested_tiles"]]
            full_tiles = list(ie._COMPONENT_TILES[component])
            if component == "coherencia":
                suboperation = "evaluate_coherencia"
                core = self._evaluate_coherencia()
            else:
                suboperation = "evaluate_base_component"
                core = self._evaluate_base_component(component)
            if core.status == "not_evaluated":
                raise FlowSv9StrictComponentAdapterError("Core component evaluation failed")
            if core.status == "not_detected":
                if requested != full_tiles:
                    raise FlowSv9StrictComponentAdapterError(
                        "partial component cannot become not-detected"
                    )
                suboperation = "build_component_evaluation"
                self._components[component] = core
                self._component_provenance_candidates[component] = self._candidate
                strict = ie.build_component_evaluation(
                    component_key=component,
                    series_fingerprint=request["current_series_fingerprint"],
                    request_fingerprint=request["canonical_request_fingerprint"],
                    status="not_detected",
                    tile_results=[],
                )
                return ie.ComponentEvaluationOutcome.success(strict)
            suboperation = "merge_component"
            core = self._merge_component(component, requested, core)
            self._components[component] = core
            self._component_provenance_candidates[component] = self._candidate
            suboperation = "project_component"
            projected = self._project_component_for_strict(component)
            suboperation = "validate_projected_untouched_tiles"
            self._validate_projected_untouched_tiles(
                component,
                request,
                projected,
            )
            suboperation = "actual_evaluation_evidence"
            actual_sources, admitted_refs = self._actual_evaluation_evidence(
                component
            )
            by_tile = {row.tile_id: row for row in projected.tile_profile}
            suboperation = "strict_tile_results"
            strict_rows = [
                self._strict_tile_result(
                    row,
                    by_tile[row["tile_id"]],
                    actual_sources=actual_sources,
                    admitted_refs=admitted_refs,
                )
                for row in request["requested_tiles"]
            ]
            suboperation = "build_component_evaluation"
            strict = ie.build_component_evaluation(
                component_key=component,
                series_fingerprint=request["current_series_fingerprint"],
                request_fingerprint=request["canonical_request_fingerprint"],
                status="evaluated",
                tile_results=strict_rows,
            )
            return ie.ComponentEvaluationOutcome.success(strict)
        except Exception as exc:
            try:
                from src.services.evidence_vault_sv9_authority_evaluation import (
                    emit_evidence_vault_sv9_component_evaluation_diagnostic,
                )

                emit_evidence_vault_sv9_component_evaluation_diagnostic(
                    component=component,
                    suboperation=suboperation,
                    exception=exc,
                )
            except Exception:
                pass
            return ie.ComponentEvaluationOutcome.provider_failure()

    def build_shared_analysis_payload(self, assessment: Mapping[str, Any]) -> dict[str, Any]:
        """Build the same complete Core payload before candidate persistence."""

        self._prepare()
        from scripts.sv9_flow_sv9_shadow_eval import (
            SV9_FLOW_SV9_SHADOW_EVAL_VERSION,
            _compact_interpretation_debug,
            _detected_blocks,
            _result_summary,
        )
        from src.sv9.rubric import COMPONENTS
        from src.sv9.service import _aggregate_sv9_analysis
        from src.sv9.flow_ingress import (
            detection_blocks_from_flow_candidate,
            flow_candidate_extra_signals,
        )

        if set(self._components) != set(COMPONENTS):
            raise FlowSv9StrictComponentAdapterError(
                "shared analysis components are incomplete"
            )
        result = _aggregate_sv9_analysis(
            copy.deepcopy(self._components),
            brand_name=str(self._candidate.evidence_pack.brand_name),
            url=str(self._candidate.evidence_pack.url),
            source_run_id=self._source_run_id,
            evaluator_llm=self._evaluator_llm,
        )
        if _assessment_output_from_scanner_envelope(result.assessment) != dict(
            assessment
        ):
            raise FlowSv9StrictComponentAdapterError(
                "Core and Vault assessments do not match"
            )
        payload: dict[str, Any] = {
            "schema_version": SV9_FLOW_SV9_SHADOW_EVAL_VERSION,
            "source_run_id": self._source_run_id,
            "brand_name": result.brand_name,
            "url": result.url,
            "visual_acquisition_present": self._visual_evidence_packet is not None,
            "flow": {
                "detected_blocks": _detected_blocks(self._candidate),
                "limitations": list(self._candidate.limitations),
                "visual_acquisition_present": self._visual_evidence_packet is not None,
                "visual_acquisition_schema_version": (
                    self._visual_evidence_packet.get("schema_version")
                    if isinstance(self._visual_evidence_packet, dict)
                    else None
                ),
                "interpretation_debug": _compact_interpretation_debug(self._debug or {}),
                "candidate": self._candidate.to_dict(),
                "detection_blocks": detection_blocks_from_flow_candidate(self._candidate),
                "extra_signals": flow_candidate_extra_signals(self._candidate),
            },
            "sv9": _result_summary(result.to_dict())
            | {"result": result.to_dict()},
            "llm_usage": self._cumulative_llm_usage(),
        }
        payload.update(copy.deepcopy(self._payload_context))
        if callable(self._payload_finalizer):
            payload = self._payload_finalizer(payload)
        return _shared_json_copy(
            {
                "schema_version": "evidence-vault-sv9-shared-analysis-payload-v1",
                "analysis_payload": payload,
                "evaluation_components": {
                    key: value.to_dict()
                    for key, value in sorted(self._components.items())
                },
                "component_provenance": {
                    key: self._component_provenance_candidates[key].to_dict()
                    for key in _shared_provenance_component_keys()
                },
            }
        )

    def get_shared_checkpoint_process(
        self, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return completed Core work and frozen Flow context for one checkpoint."""

        request = ie._canon(dict(request))
        component = str(request.get("component_key") or "")
        value = self._components.get(component)
        if value is None or self._candidate is None or self._debug is None:
            raise FlowSv9StrictComponentAdapterError(
                "shared checkpoint process is unavailable"
            )
        return _shared_json_copy(
            {
                "schema_version": _SHARED_CHECKPOINT_PROCESS_VERSION,
                "binding": self._shared_checkpoint_binding(request),
                "flow_context": {
                    "candidate": self._candidate.to_dict(),
                    "interpretation_debug": self._debug,
                    "visual_evidence_packet": self._visual_evidence_packet,
                },
                "component_result": value.to_dict(),
                "llm_usage": self._cumulative_llm_usage(),
            }
        )

    def restore_shared_checkpoint_process(
        self,
        request: Mapping[str, Any],
        accepted: Mapping[str, Any],
        value: Mapping[str, Any],
    ) -> None:
        """Restore persisted completed Core work without repeating Flow model calls."""

        request = ie._canon(dict(request))
        restored = _validate_shared_checkpoint_process(
            value,
            request=request,
            source_run_id=self._source_run_id,
            snapshot=self._snapshot,
        )
        flow_context = restored["flow_context"]
        if (
            self._restored_flow_context is not None
            and self._restored_flow_context != flow_context
        ):
            raise FlowSv9StrictComponentAdapterError(
                "shared checkpoint Flow context conflicts"
            )
        if self._restored_flow_context is None:
            self._candidate = _flow_candidate_from_shared_checkpoint(
                flow_context["candidate"]
            )
            self._debug = _shared_json_copy(
                flow_context["interpretation_debug"]
            )
            visual = flow_context["visual_evidence_packet"]
            self._visual_evidence_packet = (
                _shared_json_copy(visual) if visual is not None else None
            )
            self._hydrate_analysis_inputs()
            self._restored_flow_context = _shared_json_copy(flow_context)
        component = str(request["component_key"])
        component_result = _component_from_shared_analysis_row(
            component, restored["component_result"]
        )
        previous = self._restored_checkpoint_components.get(component)
        if previous is None:
            self._validate_untouched_checkpoint_tiles(
                component,
                request,
                component_result,
            )
        if previous is not None and _shared_json_copy(
            previous.to_dict()
        ) != _shared_json_copy(component_result.to_dict()):
            raise FlowSv9StrictComponentAdapterError(
                "shared checkpoint component conflicts"
            )
        self._components[component] = component_result
        self._component_provenance_candidates[component] = self._candidate
        projected = self._project_component_for_strict(component)
        self._validate_projected_untouched_tiles(
            component,
            request,
            projected,
        )
        if projected.status == "not_detected":
            strict = ie.build_component_evaluation(
                component_key=component,
                series_fingerprint=request["current_series_fingerprint"],
                request_fingerprint=request["canonical_request_fingerprint"],
                status="not_detected",
                tile_results=[],
            )
        else:
            actual_sources, admitted_refs = self._actual_evaluation_evidence(
                component
            )
            by_tile = {row.tile_id: row for row in projected.tile_profile}
            strict = ie.build_component_evaluation(
                component_key=component,
                series_fingerprint=request["current_series_fingerprint"],
                request_fingerprint=request["canonical_request_fingerprint"],
                status="evaluated",
                tile_results=[
                    self._strict_tile_result(
                        row,
                        by_tile[row["tile_id"]],
                        actual_sources=actual_sources,
                        admitted_refs=admitted_refs,
                    )
                    for row in request["requested_tiles"]
                ],
            )
        if ie._canon(dict(accepted)) != ie._canon(strict):
            raise FlowSv9StrictComponentAdapterError(
                "shared checkpoint component does not match strict evaluation"
            )
        self._restored_checkpoint_components[component] = component_result
        self._restored_llm_usage = _merge_cumulative_llm_usage(
            self._restored_llm_usage,
            restored["llm_usage"],
            additive=False,
        )

    def _validate_untouched_checkpoint_tiles(
        self,
        component: str,
        request: Mapping[str, Any],
        restored: Any,
    ) -> None:
        requested = {str(row["tile_id"]) for row in request["requested_tiles"]}
        untouched = set(ie._COMPONENT_TILES[component]) - requested
        if not untouched:
            return
        prior = self._components.get(component)
        prior_tiles = {
            row.tile_id: _shared_json_copy(row.to_dict())
            for row in getattr(prior, "tile_profile", [])
        }
        restored_tiles = {
            row.tile_id: _shared_json_copy(row.to_dict())
            for row in restored.tile_profile
        }
        if (
            getattr(prior, "status", None) != "scored"
            or set(prior_tiles) != set(ie._COMPONENT_TILES[component])
            or any(prior_tiles[tile] != restored_tiles[tile] for tile in untouched)
        ):
            raise FlowSv9StrictComponentAdapterError(
                "shared checkpoint changed an untouched accepted tile"
            )

    def _validate_projected_untouched_tiles(
        self,
        component: str,
        request: Mapping[str, Any],
        projected: Any,
    ) -> None:
        requested = {str(row["tile_id"]) for row in request["requested_tiles"]}
        untouched = set(ie._COMPONENT_TILES[component]) - requested
        if not untouched:
            return
        prior = self._prior_projected_components.get(component)
        prior_states = {
            row.tile_id: row.estado
            for row in getattr(prior, "tile_profile", [])
        }
        projected_states = {
            row.tile_id: row.estado
            for row in getattr(projected, "tile_profile", [])
        }
        if (
            getattr(prior, "status", None) != "scored"
            or set(prior_states) != set(ie._COMPONENT_TILES[component])
            or any(
                prior_states[tile] != projected_states[tile]
                for tile in untouched
            )
        ):
            raise FlowSv9StrictComponentAdapterError(
                "source policy changed an untouched accepted tile"
            )

    def _cumulative_llm_usage(self) -> dict[str, Any]:
        from scripts.sv9_flow_sv9_shadow_eval import (
            _llm_usage_payload,
            _llm_usage_summary,
        )

        current = _llm_usage_payload(
            interpretation_llm=self._interpretation_llm,
            labeling_llm=self._labeling_llm,
            evaluator_llm=self._evaluator_llm,
            reasoning_llm=self._reasoning_llm,
        )
        if self._reasoning_llm is None:
            current["roles"]["sv9_reasoning"] = _llm_usage_summary(None)
        return _merge_cumulative_llm_usage(
            self._restored_llm_usage,
            current,
            additive=True,
        )

    def _shared_checkpoint_binding(
        self, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not is_core_shared_series_contract(request.get("current_series_contract")):
            raise FlowSv9StrictComponentAdapterError("shared series is invalid")
        return {
            "source_scan_id": self._source_run_id,
            "canonical_plan_fingerprint": request["plan_fingerprint"],
            "canonical_request_fingerprint": request[
                "canonical_request_fingerprint"
            ],
            "current_series_fingerprint": request["current_series_fingerprint"],
            "candidate_series_fingerprint": request[
                "candidate_series_fingerprint"
            ],
            "component_key": request["component_key"],
            "capture_origin": request["capture_origin"],
            "operation_origin": request["operation_origin"],
            "snapshot_fingerprint": judgment_memory.canonical_fingerprint(
                _SHARED_CHECKPOINT_SNAPSHOT_FINGERPRINT_VERSION,
                self._snapshot,
            ),
        }

    def _prepare(self) -> None:
        if self._candidate is not None:
            return
        from src.sv9_flow.orchestrator import build_flow_candidate
        from scripts.sv9_flow_sv9_shadow_eval import _visual_evidence_packet_from_snapshot

        self._initialize_llms()
        self._visual_evidence_packet = _visual_evidence_packet_from_snapshot(self._snapshot)
        self._candidate, self._debug = build_flow_candidate(
            snapshot=self._snapshot,
            llm=self._interpretation_llm,
            adjudicator_llm=self._adjudicator_llm,
            labeling_llm=self._labeling_llm,
            visual_signature_evidence=self._visual_evidence_packet,
            gate_authority=self._gate_authority,
        )
        self._hydrate_analysis_inputs()

    def _hydrate_analysis_inputs(self) -> None:
        if self._candidate is None:
            raise FlowSv9StrictComponentAdapterError(
                "shared Flow context is unavailable"
            )
        from src.sv9.service import _prepare_sv9_analysis_inputs

        (
            self._tldr,
            self._signals,
            _brand_name,
            _url,
            _source_run_id,
        ) = _prepare_sv9_analysis_inputs(
            self._snapshot,
            llm=None,
            sv9_flow_candidate=self._candidate,
        )

    def _initialize_evaluation_llms(self) -> None:
        try:
            for role in ("evaluator", "reasoning"):
                attribute = f"_{role}_llm"
                if getattr(self, attribute) is None:
                    factory = self._llm_factories[role]
                    if not callable(factory):
                        raise TypeError
                    setattr(self, attribute, factory())
        except Exception as exc:
            raise FlowSv9StrictComponentAdapterError(
                "shared analysis evaluation LLM initialization failed"
            ) from exc

    def _initialize_llms(self) -> None:
        if self._interpretation_llm is not None:
            return
        try:
            clients = {
                role: factory()
                for role, factory in self._llm_factories.items()
                if callable(factory)
            }
        except Exception as exc:
            raise FlowSv9StrictComponentAdapterError(
                "shared analysis LLM initialization failed"
            ) from exc
        if set(clients) != set(self._llm_factories):
            raise FlowSv9StrictComponentAdapterError(
                "shared analysis LLM factories are invalid"
            )
        self._interpretation_llm = clients["interpretation"]
        self._adjudicator_llm = clients["adjudicator"]
        self._labeling_llm = clients["labeling"]
        self._evaluator_llm = clients["evaluator"]
        self._reasoning_llm = clients["reasoning"]

    def _evaluate_base_component(self, component: str):
        from src.sv9.evaluator import evaluate_component
        from src.sv9.rubric import REASONING_COMPONENTS

        llm = self._reasoning_llm if component in REASONING_COMPONENTS else self._evaluator_llm
        return evaluate_component(
            component,
            tldr=self._tldr or {},
            signals=(self._signals or {}).get(component) or [],
            brand_name=str(self._candidate.evidence_pack.brand_name),
            url=str(self._candidate.evidence_pack.url),
            llm=llm,
        )

    def _evaluate_coherencia(self):
        from src.sv9.evaluator import evaluate_coherencia
        from src.sv9.rubric import PRESENTATION_ORDER

        required = set(PRESENTATION_ORDER) - {"coherencia"}
        if not required <= set(self._components):
            raise FlowSv9StrictComponentAdapterError(
                "Coherencia requires the complete accepted component context"
            )
        return evaluate_coherencia(
            components={key: self._components[key] for key in PRESENTATION_ORDER if key != "coherencia"},
            tldr=self._tldr or {},
            signals=(self._signals or {}).get("coherencia") or [],
            brand_name=str(self._candidate.evidence_pack.brand_name),
            url=str(self._candidate.evidence_pack.url),
            llm=self._reasoning_llm,
        )

    def _merge_component(self, component: str, requested: list[str], core: Any):
        from src.sv9.aggregator import score_from_tile_profile
        from src.sv9.models import ComponentResult

        full = list(ie._COMPONENT_TILES[component])
        current = {row.tile_id: row for row in core.tile_profile}
        if set(current) != set(full):
            raise FlowSv9StrictComponentAdapterError("Core tile profile is incomplete")
        if requested == full:
            return core
        prior = self._components.get(component)
        prior_rows = {
            row.tile_id: row for row in getattr(prior, "tile_profile", [])
        }
        if getattr(prior, "status", None) != "scored" or set(prior_rows) != set(full):
            raise FlowSv9StrictComponentAdapterError(
                "partial Core evaluation has no complete accepted base"
            )
        selected = set(requested)
        profile = [current[tile] if tile in selected else prior_rows[tile] for tile in full]
        return ComponentResult(
            component=component,
            status=core.status,
            score=score_from_tile_profile(profile),
            tile_profile=profile,
            veredicto=core.veredicto,
            message=core.message,
            evaluation_model=core.evaluation_model,
            detected_content=core.detected_content,
            detection_mode=core.detection_mode,
            detection_confidence=core.detection_confidence,
            detection_limitations=list(core.detection_limitations),
            evidence_source_summary=dict(core.evidence_source_summary),
            source_policy_notes=list(core.source_policy_notes),
            evidence=list(core.evidence),
            error=core.error,
        )

    @staticmethod
    def _project_source_policy_graph(components: Mapping[str, Any]) -> dict[str, Any]:
        if not components:
            return {}
        from src.sv9.source_policy import project_legacy_source_policy

        return project_legacy_source_policy(components).components

    def _project_component_for_strict(self, component: str) -> Any:
        from src.sv9.rubric import COMPONENTS

        if component == "coherencia":
            if set(self._components) != set(COMPONENTS):
                raise FlowSv9StrictComponentAdapterError(
                    "Coherencia source policy requires the complete component graph"
                )
            graph = self._components
        else:
            graph = {component: self._components[component]}
        return self._project_source_policy_graph(graph)[component]

    def _actual_evaluation_evidence(
        self,
        component: str,
    ) -> tuple[list[str], set[str]]:
        from src.sv9.evaluator import (
            _coherencia_literal_sources,
            _component_literal_sources,
        )
        from src.sv9.rubric import COMPONENTS

        signals = (self._signals or {}).get(component) or []
        signal_refs = [
            str(ref)
            for signal in signals
            for key in ("source_evidence_refs", "evidence_refs")
            for ref in signal.get(key) or []
            if str(ref or "").strip()
        ]
        if component == "coherencia":
            components = {
                key: value
                for key, value in self._components.items()
                if key != "coherencia"
            }
            literal_sources = _coherencia_literal_sources(
                components,
                self._tldr or {},
            )
            source_pairs = self._coherencia_literal_source_pairs(
                components,
                literal_sources,
            )
        else:
            block_key = COMPONENTS[component].get("tldr_key")
            raw_block = (self._tldr or {}).get(block_key) if block_key else None
            block = raw_block if isinstance(raw_block, dict) else {}
            literal_sources = _component_literal_sources(block, signals)
            source_pairs = [
                (source, ref, self._candidate)
                for source, ref in self._block_literal_source_pairs(
                    block,
                    candidate=self._candidate,
                )
            ]
        admitted_refs = self._evidence_ref_aliases(
            signal_refs,
            candidate=self._candidate,
        )
        for _source, ref, candidate in source_pairs:
            admitted_refs.update(
                self._evidence_ref_aliases([ref], candidate=candidate)
            )
        return literal_sources, admitted_refs

    def _coherencia_literal_source_pairs(
        self,
        components: Mapping[str, Any],
        literal_sources: list[str],
    ) -> list[tuple[str, str, Any]]:
        from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER

        if not literal_sources:
            return []
        ordered: list[tuple[str, str, Any]] = []
        seen: set[str] = set()
        for component_key in PRESENTATION_ORDER:
            component = components.get(component_key)
            if component is None:
                continue
            owner = self._component_provenance_candidates.get(component_key)
            block_key = COMPONENTS[component_key].get("tldr_key")
            owner_blocks = self._detection_blocks_for_candidate(owner)
            raw_block = owner_blocks.get(block_key) if block_key else None
            block = raw_block if isinstance(raw_block, dict) else {}
            pairs = self._block_literal_source_pairs(block, candidate=owner)
            expected = [str(item).strip() for item in component.evidence if str(item).strip()]
            cursor = 0
            for source in expected:
                if source in seen:
                    continue
                if len(ordered) >= len(literal_sources):
                    return ordered
                if source != literal_sources[len(ordered)]:
                    raise FlowSv9StrictComponentAdapterError(
                        "Coherencia evidence provenance does not match its prompt"
                    )
                match = next(
                    (
                        index
                        for index in range(cursor, len(pairs))
                        if pairs[index][0] == source
                    ),
                    None,
                )
                if match is None:
                    raise FlowSv9StrictComponentAdapterError(
                        "Coherencia evidence provenance is unavailable"
                    )
                ordered.append((*pairs[match], owner))
                seen.add(source)
                cursor = match + 1
        for component_key in PRESENTATION_ORDER:
            if len(ordered) >= len(literal_sources):
                return ordered
            block_key = COMPONENTS[component_key].get("tldr_key")
            raw_block = (self._tldr or {}).get(block_key) if block_key else None
            if isinstance(raw_block, dict):
                for source, ref in self._block_literal_source_pairs(
                    raw_block,
                    candidate=self._candidate,
                ):
                    if source in seen:
                        continue
                    if source != literal_sources[len(ordered)]:
                        raise FlowSv9StrictComponentAdapterError(
                            "Coherencia evidence provenance does not match its prompt"
                        )
                    ordered.append((source, ref, self._candidate))
                    seen.add(source)
                    if len(ordered) >= len(literal_sources):
                        return ordered
        if [source for source, _ref, _candidate in ordered] != literal_sources:
            raise FlowSv9StrictComponentAdapterError(
                "Coherencia evidence provenance does not match its prompt"
            )
        return ordered

    @staticmethod
    def _detection_blocks_for_candidate(candidate: Any) -> dict[str, dict[str, Any]]:
        if candidate is None:
            return {}
        from src.sv9.flow_ingress import detection_blocks_from_flow_candidate

        return detection_blocks_from_flow_candidate(candidate)

    def _block_literal_source_pairs(
        self,
        block: Mapping[str, Any],
        *,
        candidate: Any,
    ) -> list[tuple[str, str]]:
        return _block_literal_source_pairs_for_candidate(block, candidate=candidate)

    def _evidence_ref_aliases(
        self,
        refs: list[str],
        *,
        candidate: Any,
    ) -> set[str]:
        from src.sv9.flow_ingress import _canonical_signal_refs

        raw = {str(ref) for ref in refs if str(ref or "").strip()}
        if candidate is None:
            return raw
        return raw | set(_canonical_signal_refs(list(raw), candidate))

    def _strict_tile_result(
        self,
        requested: Mapping[str, Any],
        verdict: Any,
        *,
        actual_sources: list[str],
        admitted_refs: set[str],
    ) -> dict[str, Any]:
        evidence = [dict(row) for row in requested.get("evidence") or []]
        supplied = [
            row
            for row in evidence
            if str(row.get("evidence_ref") or "") in admitted_refs
        ]
        if verdict.estado == "ok":
            supporting = [
                row
                for row in supplied
                if _literal_in_content(verdict.evidencia, row.get("content"))
            ]
            if (
                not _literal_in_content(verdict.evidencia, actual_sources)
                or not supporting
            ):
                raise FlowSv9StrictComponentAdapterError(
                    "Core ok quote is not bound to requested Vault evidence"
                )
        elif verdict.estado == "no":
            supporting = supplied
            if not supporting:
                raise FlowSv9StrictComponentAdapterError(
                    "Core no result has no requested evidence supplied to its prompt"
                )
        elif verdict.estado == "sin_evidencia":
            supporting = []
        else:
            raise FlowSv9StrictComponentAdapterError("Core tile state is invalid")
        return {
            "tile_id": str(verdict.tile_id),
            "assessment_state": str(verdict.estado),
            "supporting_evidence": [
                {key: row[key] for key in ("evidence_ref", "evidence_fingerprint")}
                for row in supporting
            ],
        }


def _components_from_shared_analysis(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        from src.sv9.rubric import COMPONENTS

        if value.get("schema_version") != "evidence-vault-sv9-shared-analysis-payload-v1":
            raise ValueError
        rows = value["evaluation_components"]
        if not isinstance(rows, Mapping) or set(rows) != set(COMPONENTS):
            raise ValueError
        components = {}
        for key, raw in rows.items():
            components[key] = _component_from_shared_analysis_row(key, raw)
        return components
    except Exception as exc:
        raise FlowSv9StrictComponentAdapterError(
            "prior shared analysis is invalid"
        ) from exc


def _shared_provenance_component_keys() -> tuple[str, ...]:
    from src.sv9.rubric import PRESENTATION_ORDER

    return tuple(key for key in PRESENTATION_ORDER if key != "coherencia")


def _component_provenance_candidates_from_shared_analysis(
    value: Mapping[str, Any] | None,
    *,
    components: Mapping[str, Any],
) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        from src.history.report_parser import normalize_domain
        from src.sv9.flow_ingress import detection_blocks_from_flow_candidate
        from src.sv9.rubric import COMPONENTS

        rows = value["component_provenance"]
        expected_keys = _shared_provenance_component_keys()
        if type(rows) is not dict or set(rows) != set(expected_keys):
            raise ValueError
        current = _flow_candidate_from_shared_analysis(value)
        current_brand = str(current.evidence_pack.brand_name)
        current_domain = normalize_domain(str(current.evidence_pack.url))
        candidates: dict[str, Any] = {}
        for component_key in expected_keys:
            owner = _flow_candidate_from_shared_checkpoint(rows[component_key])
            if (
                str(owner.evidence_pack.brand_name) != current_brand
                or normalize_domain(str(owner.evidence_pack.url)) != current_domain
            ):
                raise ValueError
            component = components[component_key]
            component_evidence = [
                str(source).strip()
                for source in component.evidence
                if str(source).strip()
            ]
            if component.status == "not_detected":
                if component_evidence:
                    raise ValueError
            else:
                block_key = COMPONENTS[component_key].get("tldr_key")
                blocks = detection_blocks_from_flow_candidate(owner)
                raw_block = blocks.get(block_key) if block_key else None
                block = raw_block if isinstance(raw_block, dict) else {}
                pairs = _block_literal_source_pairs_for_candidate(
                    block,
                    candidate=owner,
                )
                if [source for source, _ref in pairs] != component_evidence:
                    raise ValueError
            candidates[component_key] = owner
        return candidates
    except FlowSv9StrictComponentAdapterError as exc:
        raise FlowSv9StrictComponentAdapterError(
            "prior shared analysis is invalid"
        ) from exc
    except Exception as exc:
        raise FlowSv9StrictComponentAdapterError(
            "prior shared analysis is invalid"
        ) from exc


def _flow_candidate_from_shared_analysis(value: Mapping[str, Any] | None) -> Any:
    if value is None:
        return None
    try:
        analysis_payload = value["analysis_payload"]
        flow = analysis_payload["flow"]
        return _flow_candidate_from_shared_checkpoint(flow["candidate"])
    except FlowSv9StrictComponentAdapterError as exc:
        raise FlowSv9StrictComponentAdapterError(
            "prior shared analysis is invalid"
        ) from exc
    except Exception as exc:
        raise FlowSv9StrictComponentAdapterError(
            "prior shared analysis is invalid"
        ) from exc


def _block_literal_source_pairs_for_candidate(
    block: Mapping[str, Any],
    *,
    candidate: Any,
) -> list[tuple[str, str]]:
    from src.sv9.flow_ingress import _evidence_snippet_pairs

    refs = [
        str(ref)
        for ref in block.get("evaluation_evidence_refs") or []
        if str(ref or "").strip()
    ]
    expected = [
        str(source).strip()
        for source in block.get("evidence") or []
        if str(source or "").strip()
    ]
    if not expected:
        return []
    evidence_by_ref = {
        record.ref: record
        for record in getattr(
            getattr(candidate, "evidence_pack", None),
            "evidence",
            [],
        )
    }
    pairs = _evidence_snippet_pairs(refs, evidence_by_ref, limit=8)
    if [source for source, _ in pairs] != expected:
        raise FlowSv9StrictComponentAdapterError(
            "component evidence provenance does not match its prompt"
        )
    return pairs


def _validate_llm_usage(value: Any) -> dict[str, Any]:
    from scripts.sv9_flow_sv9_shadow_eval import SV9_FLOW_SV9_SHADOW_EVAL_VERSION

    roles_expected = {
        "flow_labeling",
        "flow_interpretation",
        "sv9_evaluator",
        "sv9_reasoning",
    }
    summary_keys = {
        "model",
        "base_url",
        "cache_hits",
        "cache_misses",
        "cache_writes",
        "provider_calls",
        "usage_metadata_available",
        "observations",
        "call_failures",
    }
    counter_keys = ("cache_hits", "cache_misses", "cache_writes", "provider_calls")
    try:
        payload = _shared_json_copy(value)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "roles", "totals"}
            or payload["schema_version"]
            != f"{SV9_FLOW_SV9_SHADOW_EVAL_VERSION}.llm_usage"
            or not isinstance(payload["roles"], dict)
            or set(payload["roles"]) != roles_expected
            or not isinstance(payload["totals"], dict)
            or set(payload["totals"])
            != {*counter_keys, "usage_metadata_available"}
        ):
            raise ValueError
        for summary in payload["roles"].values():
            if (
                not isinstance(summary, dict)
                or set(summary) != summary_keys
                or not isinstance(summary["model"], str)
                or not isinstance(summary["base_url"], str)
                or any(
                    type(summary[key]) is not int or summary[key] < 0
                    for key in counter_keys
                )
                or type(summary["usage_metadata_available"]) is not bool
                or not isinstance(summary["observations"], list)
                or not all(isinstance(row, dict) for row in summary["observations"])
                or not isinstance(summary["call_failures"], list)
            ):
                raise ValueError
        totals = payload["totals"]
        if (
            any(
                type(totals[key]) is not int
                or totals[key] != sum(row[key] for row in payload["roles"].values())
                for key in counter_keys
            )
            or type(totals["usage_metadata_available"]) is not bool
            or totals["usage_metadata_available"]
            != any(
                row["usage_metadata_available"]
                for row in payload["roles"].values()
            )
        ):
            raise ValueError
        return payload
    except FlowSv9StrictComponentAdapterError:
        raise
    except Exception as exc:
        raise FlowSv9StrictComponentAdapterError(
            "shared checkpoint LLM usage is invalid"
        ) from exc


def _merge_cumulative_llm_usage(
    previous: Mapping[str, Any] | None,
    incoming: Mapping[str, Any],
    *,
    additive: bool,
) -> dict[str, Any]:
    current = _validate_llm_usage(incoming)
    if previous is None:
        return current
    prior = _validate_llm_usage(previous)
    counters = ("cache_hits", "cache_misses", "cache_writes", "provider_calls")
    if prior["schema_version"] != current["schema_version"]:
        raise FlowSv9StrictComponentAdapterError(
            "shared checkpoint LLM usage conflicts"
        )
    merged = _shared_json_copy(prior)
    for role, before in prior["roles"].items():
        after = current["roles"][role]
        if additive:
            if after["model"] and before["model"] != after["model"]:
                raise FlowSv9StrictComponentAdapterError(
                    "shared checkpoint LLM usage conflicts"
                )
            if after["base_url"] and before["base_url"] != after["base_url"]:
                raise FlowSv9StrictComponentAdapterError(
                    "shared checkpoint LLM usage conflicts"
                )
            target = merged["roles"][role]
            for key in counters:
                target[key] = before[key] + after[key]
            target["usage_metadata_available"] = (
                before["usage_metadata_available"]
                or after["usage_metadata_available"]
            )
            target["observations"] = before["observations"] + after["observations"]
            target["call_failures"] = before["call_failures"] + after["call_failures"]
            continue
        if (
            before["model"] != after["model"]
            or before["base_url"] != after["base_url"]
            or any(after[key] < before[key] for key in counters)
            or after["usage_metadata_available"] < before["usage_metadata_available"]
            or after["observations"][: len(before["observations"])]
            != before["observations"]
            or after["call_failures"][: len(before["call_failures"])]
            != before["call_failures"]
        ):
            raise FlowSv9StrictComponentAdapterError(
                "shared checkpoint LLM usage conflicts"
            )
        merged["roles"][role] = _shared_json_copy(after)
    merged["totals"] = {
        key: sum(row[key] for row in merged["roles"].values())
        for key in counters
    } | {
        "usage_metadata_available": any(
            row["usage_metadata_available"] for row in merged["roles"].values()
        )
    }
    return _validate_llm_usage(merged)


def _validate_shared_checkpoint_process(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    source_run_id: str,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        if not isinstance(value, Mapping):
            raise ValueError
        payload = _shared_json_copy(dict(value))
        if set(payload) != {
            "schema_version",
            "binding",
            "flow_context",
            "component_result",
            "llm_usage",
        } or payload["schema_version"] != _SHARED_CHECKPOINT_PROCESS_VERSION:
            raise ValueError
        expected_binding = {
            "source_scan_id": str(source_run_id),
            "canonical_plan_fingerprint": request["plan_fingerprint"],
            "canonical_request_fingerprint": request[
                "canonical_request_fingerprint"
            ],
            "current_series_fingerprint": request["current_series_fingerprint"],
            "candidate_series_fingerprint": request[
                "candidate_series_fingerprint"
            ],
            "component_key": request["component_key"],
            "capture_origin": request["capture_origin"],
            "operation_origin": request["operation_origin"],
            "snapshot_fingerprint": judgment_memory.canonical_fingerprint(
                _SHARED_CHECKPOINT_SNAPSHOT_FINGERPRINT_VERSION,
                snapshot,
            ),
        }
        if payload["binding"] != expected_binding:
            raise ValueError
        context = payload["flow_context"]
        if not isinstance(context, dict) or set(context) != {
            "candidate",
            "interpretation_debug",
            "visual_evidence_packet",
        }:
            raise ValueError
        _flow_candidate_from_shared_checkpoint(context["candidate"])
        if not isinstance(context["interpretation_debug"], dict):
            raise ValueError
        if context["visual_evidence_packet"] is not None and not isinstance(
            context["visual_evidence_packet"], dict
        ):
            raise ValueError
        _component_from_shared_analysis_row(
            str(request["component_key"]), payload["component_result"]
        )
        _validate_llm_usage(payload["llm_usage"])
        return payload
    except FlowSv9StrictComponentAdapterError:
        raise
    except Exception as exc:
        raise FlowSv9StrictComponentAdapterError(
            "shared checkpoint process is invalid"
        ) from exc


def _flow_candidate_from_shared_checkpoint(raw: Any) -> Any:
    from src.sv9_flow.contracts import (
        BRAND_EVIDENCE_PACK_VERSION,
        BRAND_INTERPRETATION_VERSION,
        SV9_FLOW_CANDIDATE_VERSION,
        SV9_TILE_SIGNALS_VERSION,
        BrandEvidencePack,
        BrandInterpretation,
        EvidenceRecord,
        Sv9FlowCandidate,
        TileSignal,
        interpretation_contract_violations,
    )

    try:
        if not isinstance(raw, Mapping):
            raise ValueError
        payload = _shared_json_copy(dict(raw))
        if set(payload) != {
            "schema_version",
            "evidence_pack",
            "interpretation",
            "tile_signals",
            "claim_memory_evidence",
            "evaluation_evidence_refs",
            "evaluation_evidence_version",
            "limitations",
        }:
            raise ValueError
        evidence_pack_raw = payload["evidence_pack"]
        interpretation_raw = payload["interpretation"]
        if not isinstance(evidence_pack_raw, dict) or not isinstance(
            interpretation_raw, dict
        ):
            raise ValueError
        evidence = [
            EvidenceRecord(**dict(row))
            for row in evidence_pack_raw.get("evidence") or []
        ]
        evidence_pack = BrandEvidencePack(
            **{
                key: child
                for key, child in evidence_pack_raw.items()
                if key != "evidence"
            },
            evidence=evidence,
        )
        interpretation = BrandInterpretation(**interpretation_raw)
        candidate = Sv9FlowCandidate(
            evidence_pack=evidence_pack,
            interpretation=interpretation,
            tile_signals=[TileSignal(**dict(row)) for row in payload["tile_signals"]],
            claim_memory_evidence=[
                EvidenceRecord(**dict(row))
                for row in payload["claim_memory_evidence"]
            ],
            evaluation_evidence_refs=dict(payload["evaluation_evidence_refs"]),
            evaluation_evidence_version=str(payload["evaluation_evidence_version"]),
            limitations=list(payload["limitations"]),
            schema_version=str(payload["schema_version"]),
        )
        if (
            candidate.schema_version != SV9_FLOW_CANDIDATE_VERSION
            or candidate.evidence_pack.schema_version != BRAND_EVIDENCE_PACK_VERSION
            or candidate.interpretation.schema_version
            != BRAND_INTERPRETATION_VERSION
            or any(
                row.schema_version != SV9_TILE_SIGNALS_VERSION
                for row in candidate.tile_signals
            )
            or interpretation_contract_violations(candidate.interpretation)
            or _shared_json_copy(candidate.to_dict()) != payload
        ):
            raise ValueError
        return candidate
    except Exception as exc:
        raise FlowSv9StrictComponentAdapterError(
            "shared checkpoint Flow context is invalid"
        ) from exc


def _component_from_shared_analysis_row(component_key: str, raw: Any) -> Any:
    from src.sv9.aggregator import score_from_tile_profile
    from src.sv9.models import (
        ComponentResult,
        STATUS_NOT_DETECTED,
        STATUS_SCORED,
        TileVerdict,
    )
    from src.sv9.rubric import COMPONENTS, tile_ids

    if (
        component_key not in COMPONENTS
        or not isinstance(raw, Mapping)
        or raw.get("component") != component_key
    ):
        raise FlowSv9StrictComponentAdapterError(
            "shared component identity is invalid"
        )
    try:
        component = ComponentResult(
            component=component_key,
            status=str(raw["status"]),
            score=int(raw.get("score") or 0),
            tile_profile=[
                TileVerdict.from_dict(dict(row))
                for row in raw.get("tile_profile") or []
            ],
            veredicto=str(raw.get("veredicto") or ""),
            message=str(raw.get("message") or ""),
            evaluation_model=raw.get("evaluation_model"),
            detected_content=raw.get("detected_content"),
            detection_mode=raw.get("detection_mode"),
            detection_confidence=raw.get("detection_confidence"),
            detection_limitations=list(raw.get("detection_limitations") or []),
            evidence_source_summary=dict(raw.get("evidence_source_summary") or {}),
            source_policy_notes=list(raw.get("source_policy_notes") or []),
            evidence=list(raw.get("evidence") or []),
            error=raw.get("error"),
        )
        if component.status not in {STATUS_SCORED, STATUS_NOT_DETECTED}:
            raise ValueError
        expected_tiles = (
            tile_ids(component_key) if component.status == STATUS_SCORED else []
        )
        if [row.tile_id for row in component.tile_profile] != expected_tiles:
            raise ValueError
        expected_score = (
            score_from_tile_profile(component.tile_profile)
            if component.status == STATUS_SCORED
            else 0
        )
        if component.score != expected_score or _shared_json_copy(
            component.to_dict()
        ) != _shared_json_copy(dict(raw)):
            raise ValueError
        return component
    except FlowSv9StrictComponentAdapterError:
        raise
    except Exception as exc:
        raise FlowSv9StrictComponentAdapterError(
            "shared component payload is invalid"
        ) from exc


def _content_strings(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [item for child in value.values() for item in _content_strings(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _content_strings(child)]
    if value is None:
        return []
    text = " ".join(str(value).split())
    return [text] if text else []


def _literal_in_content(quote: Any, content: Any) -> bool:
    needle = " ".join(str(quote or "").split()).casefold()
    return bool(needle) and any(
        needle in value.casefold() for value in _content_strings(content)
    )

# fmt: on
