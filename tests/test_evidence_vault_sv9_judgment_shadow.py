from __future__ import annotations

import json
from copy import deepcopy

from src.sv9 import judgment_memory as jm
from tests.test_sv9_judgment_memory import _hash, _origin, _series

# fmt: off

class _Repository:
    def __init__(self, *, fail_append: bool = False) -> None:
        self.candidate = None
        self.calls: list[str] = []
        self.fail_append = fail_append

    def resolve_evidence_vault_sv9_judgment_evidence(self, scan_id, refs, **_kwargs):
        self.calls.append("resolve")
        assert refs == ["evidence:one"]
        return {
            "capture_origin": _origin("capture", 11),
            "operation_origin": _origin("operation", 12),
            "evidence": [{
                "evidence_record_id": "00000000-0000-0000-0000-000000000001",
                "evidence_ref": "evidence:one", "evidence_fingerprint": _hash(13),
                "content": "Persisted proof only.",
            }],
        }

    def get_evidence_vault_sv9_judgment_candidate(self, _scan_id, *, canonical_plan_fingerprint, **_kwargs):
        self.calls.append("get")
        return deepcopy(self.candidate) if self.candidate and self.candidate["canonical_plan_fingerprint"] == canonical_plan_fingerprint else None

    def append_evidence_vault_sv9_judgment_candidate(self, _scan_id, candidate, **_kwargs):
        self.calls.append("append")
        if self.fail_append:
            raise RuntimeError("secret provider body")
        self.candidate = deepcopy(candidate)
        return deepcopy(candidate), True


class _Provider:
    def __init__(self, mode: str = "ok") -> None:
        self.calls: list[dict] = []
        self.mode = mode

    def evaluate_component_json(self, request, **_kwargs):
        from src.sv9.shadow_component_provider import ShadowProviderOutcome
        self.calls.append({"request": request})
        if self.mode == "raise":
            raise RuntimeError("SECRET_PROVIDER_BODY")
        if self.mode == "malformed":
            return ShadowProviderOutcome.success({"SECRET_PROVIDER_RESPONSE": "SECRET_PROVIDER_BODY"})
        rows = [{
            "tile_id": row["tile_id"], "assessment_state": "ok",
            "supporting_evidence": [{key: row["evidence"][0][key] for key in ("evidence_ref", "evidence_fingerprint")}],
        } for row in request["requested_tiles"]]
        if self.mode == "invented":
            rows[0]["supporting_evidence"][0]["evidence_fingerprint"] = _hash(99)
        if self.mode == "missing":
            rows.pop()
        return ShadowProviderOutcome.success({"component_key": request["component_key"], "series_fingerprint": _hash(99) if self.mode == "identity" else request["current_series_fingerprint"], "request_fingerprint": request["canonical_request_fingerprint"], "status": "evaluated", "tile_results": rows})


def _run(repository, provider, current_public_score=73, **adapter_kwargs):
    from src.services.evidence_vault_sv9_judgment_shadow import run_evidence_vault_sv9_judgment_shadow
    from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapter

    return run_evidence_vault_sv9_judgment_shadow(
        repository=repository, flow=FlowSv9StrictComponentAdapter(provider, **adapter_kwargs),
        source_scan_id="scan-1", current_series_contract=_series(),
        advisory_evidence_refs=["evidence:one"], current_public_score=current_public_score,
    )


def test_shadow_evaluates_exact_workset_then_replays_without_provider_calls():
    repository, provider = _Repository(), _Provider()
    first = _run(repository, provider)
    assert (first["status"], first["calls_issued"], first["reopened_tiles"], first["evaluated_tiles"], first["current_score"], first["candidate_score"]) == ("available", 10, 0, 80, 73.0, 100)
    assert len(provider.calls) == 10 and repository.calls == ["resolve", "get", "append"]
    assert all(set(call) == {"request"} for call in provider.calls)
    assert first["tile_diffs"] == [{"tile_id": row["tile_id"], "action": row["action"]} for row in repository.candidate["plan"]["items"] if row["action"] != "unaffected"] and len(first["tile_diffs"]) <= 80
    assert "Persisted proof only." not in json.dumps(repository.candidate)
    second = _run(repository, provider)
    assert second["status"] == "replayed" and second["calls_issued"] == 0 and second["calls_avoided"] == 10 and second["tile_diffs"] == first["tile_diffs"] and len(provider.calls) == 10
    assert repository.calls[-2:] == ["resolve", "get"]


def test_shadow_keeps_provider_and_repository_failures_pending_and_sanitized():
    for repository, provider, reason in ((_Repository(), _Provider("raise"), "provider_failure"), (_Repository(), _Provider("malformed"), "provider_failure"), (_Repository(), _Provider("invented"), "provider_failure"), (_Repository(), _Provider("missing"), "provider_failure"), (_Repository(fail_append=True), _Provider(), "repository_failure")):
        result = _run(repository, provider)
        assert result["status"] == "pending" and result["reason_code"] == reason and len(result["tile_diffs"]) == 80
        assert "candidate_score" not in result and "secret" not in json.dumps(result) and "SECRET_" not in json.dumps(result)

def test_adapter_returns_closed_failure_for_secret_request_and_provider_errors():
    from src.sv9 import incremental_evaluation as ie
    from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapter

    source = _Provider(); _run(_Repository(), source); request = source.calls[0]["request"]
    for mode in ("raise", "malformed", "identity", "invented", "missing"):
        outcome = FlowSv9StrictComponentAdapter(_Provider(mode)).evaluate_component(request)
        assert type(outcome) is ie.ComponentEvaluationOutcome and outcome.evaluation is None and outcome.reason_code == "provider_failure" and "SECRET_" not in repr(outcome)
    invalid = deepcopy(request); invalid["requested_tiles"][0]["evidence"][0]["content"] = {"secret": "SECRET_EVIDENCE"}; provider = _Provider()
    outcome = FlowSv9StrictComponentAdapter(provider).evaluate_component(invalid)
    assert type(outcome) is ie.ComponentEvaluationOutcome and outcome.evaluation is None and not provider.calls and "SECRET_" not in repr(outcome)


def test_shadow_never_promotes_cross_scan_memory_or_persists_semantic_content():
    source = __import__("src.services.evidence_vault_sv9_judgment_shadow", fromlist=["*"])
    assert "prior_judgments = []" in source.__loader__.get_source(source.__name__)
    assert jm.validate_judgment_series_contract(_series())["rubric_version"] and all(_run(_Repository(), _Provider(), score)["current_score"] is None for score in ("invalid", -1, 101))

# fmt: on


def test_core_adapter_matches_real_flow_pipeline_and_reuses_without_llms():
    from scripts.sv9_flow_sv9_shadow_eval import build_flow_sv9_shadow_eval
    from src.sv9 import incremental_evaluation as ie
    from src.sv9 import incremental_planner as ip
    from src.services.evidence_vault_sv9_shared_process import (
        CoreFlowSv9StrictComponentAdapter,
        build_core_shared_series_contract,
    )
    from src.sv9.rubric import COMPONENTS, tile_ids

    class FlowLLM:
        api_key = "test-key"
        model = "flow-fake"
        last_failure_reason = None
        call_failures = []

        def _call_json(self, _system, _user, **_kwargs):
            return {
                "detected": True,
                "content": "Acme helps finance teams close faster.",
                "confidence": "high",
                "evidence_refs": ["raw_inputs.0"],
                "rationale": "The evidence states the audience and outcome.",
                "limitations": [],
            }

    class LabelingLLM:
        api_key = None
        model = "labeling-fake"
        last_failure_reason = None
        call_failures = []

    class TileLLM:
        api_key = "test-key"
        model = "tile-fake"
        last_failure_reason = None

        def __init__(self):
            self.call_failures = []

        def _call_json(self, _system, _user, **kwargs):
            schema_name = kwargs.get("schema_name")
            component = next(
                (
                    key
                    for key in COMPONENTS
                    if schema_name == f"baldosas_{key}"
                ),
                None,
            )
            rows = [
                {
                    "id": tile_id,
                    "estado": "no",
                    "evidencia": "",
                    "motivo": "The evidence does not satisfy this tile.",
                }
                for tile_id in tile_ids(component)
            ]
            payload = {"baldosas": rows}
            if component == "coherencia":
                payload["veredicto"] = "The brand story is not yet coherent."
            return payload

    snapshot = {
        "run": {
            "id": 42,
            "brand_name": "Acme",
            "url": "https://acme.example",
        },
        "raw_inputs": [
            {
                "source": "homepage",
                "payload": {
                    "url": "https://acme.example",
                    "text": "Acme helps finance teams close faster.",
                },
            }
        ],
    }
    direct = build_flow_sv9_shadow_eval(
        {"source_run_id": 42, "debug": snapshot},
        include_full=True,
        interpretation_llm=FlowLLM(),
        adjudicator_llm=FlowLLM(),
        labeling_llm=LabelingLLM(),
        evaluator_llm=TileLLM(),
        reasoning_llm=TileLLM(),
        gate_authority="veto_only",
        visual_evidence_fn=lambda _snapshot: None,
    )
    series = build_core_shared_series_contract(
        interpretation_model="flow-fake",
        labeling_model="labeling-fake",
        adjudicator_model="flow-fake",
        evaluator_model="tile-fake",
        reasoning_model="tile-fake",
        editorial_model="editorial-fake",
        gate_authority="veto_only",
        editorial_enabled=True,
    )
    adapter = CoreFlowSv9StrictComponentAdapter(
        snapshot=snapshot,
        source_run_id="42",
        interpretation_llm_factory=FlowLLM,
        adjudicator_llm_factory=FlowLLM,
        labeling_llm_factory=LabelingLLM,
        evaluator_llm_factory=TileLLM,
        reasoning_llm_factory=TileLLM,
        gate_authority="veto_only",
    )
    plan = ip.build_incremental_plan([], [], series)
    evidence = {
        "evidence_ref": "raw_inputs.0",
        "evidence_fingerprint": _hash(13),
        "content": "Acme helps finance teams close faster.",
    }
    packets = [
        ie.build_evidence_packet(
            component_key=component,
            tiles=[
                {"tile_id": tile_id, "evidence": [evidence]}
                for tile_id in plan["tile_workset"]
                if ie._BY_TILE[tile_id][1] == component
            ],
            capture_origin=_origin("capture", 11),
            operation_origin=_origin("operation", 12),
            series_fingerprint=plan["current_series_fingerprint"],
        )
        for component in plan["component_workset"]
    ]
    incremental = ie.execute_incremental_evaluation(plan, packets, adapter)
    assert incremental["status"] == "available"
    shared = adapter.build_shared_analysis_payload(incremental["assessment"])
    actual = shared["analysis_payload"]

    for payload in (direct, actual):
        payload["source_run_id"] = str(payload["source_run_id"])
        payload["sv9"]["result"]["source_run_id"] = str(
            payload["sv9"]["result"]["source_run_id"]
        )
    assert actual == direct
    assert shared["evaluation_components"] == direct["sv9"]["result"][
        "components"
    ]
    assert actual["flow"]["interpretation_debug"]["evidence_coverage"]

    def forbidden_factory():
        raise AssertionError("Exact Resume must not initialize an LLM")

    accepted_judgments = [
        jm.build_tile_judgment(
            **{
                key: value
                for key, value in row.items()
                if key
                not in {
                    "schema_version",
                    "series_fingerprint",
                    "canonical_judgment_fingerprint",
                    "authority_state",
                }
            },
            authority_state="accepted",
        )
        for row in incremental["candidate_tile_judgments"]
    ]
    accepted_sentinels = [
        ip.build_component_not_detected_sentinel(
            **{
                key: value
                for key, value in row.items()
                if key
                not in {
                    "schema_version",
                    "series_fingerprint",
                    "canonical_component_sentinel_fingerprint",
                    "authority_state",
                }
            },
            authority_state="accepted",
        )
        for row in incremental["candidate_component_sentinels"]
    ]
    resume = CoreFlowSv9StrictComponentAdapter(
        snapshot=snapshot,
        source_run_id="42",
        interpretation_llm_factory=forbidden_factory,
        adjudicator_llm_factory=forbidden_factory,
        labeling_llm_factory=forbidden_factory,
        evaluator_llm_factory=forbidden_factory,
        reasoning_llm_factory=forbidden_factory,
        gate_authority="veto_only",
        prior_shared_analysis=shared,
    )
    repeat_plan = ip.build_incremental_plan(
        accepted_judgments,
        [],
        series,
        prior_component_sentinels=accepted_sentinels,
    )
    repeated = ie.execute_incremental_evaluation(repeat_plan, [], resume)
    assert repeated["status"] == "available"
    assert repeated["call_count"] == 0
    assert repeated["calls_avoided"] == 10
    assert repeated["assessment"] == incremental["assessment"]
