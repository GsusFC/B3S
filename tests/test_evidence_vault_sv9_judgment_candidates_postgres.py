from copy import deepcopy
import json
import os
from pathlib import Path
import pytest
from src.history import repository as history
from src.services import evidence_vault_sv9_authority_evaluation as authority
from src.services.evidence_vault_incremental_executor import execute_vault_operation_plan
from src.services.evidence_vault_sv9_authoritative_relations import project_evidence_vault_sv9_authoritative_relations
from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan
from src.sv9 import incremental_evaluation as ie
from src.sv9 import incremental_planner as ip
from src.sv9 import judgment_memory as jm
from tests.test_sv9_incremental_evaluation import _Flow, _packets, _prior
from tests.test_sv9_judgment_memory import _series
from tests.test_evidence_vault_operation_execution_postgres import (
    ExecutorLLM,
    _persist_baseline,
    _reset_repository,
    _row,
)


# fmt: off
def _candidate(plan, result):
    value = {"schema_version": "evidence-vault-sv9-judgment-candidate-v1", "plan": plan, "canonical_plan_fingerprint": plan["canonical_plan_fingerprint"], "current_series_fingerprint": plan["current_series_fingerprint"], "candidate_series_fingerprint": plan["candidate_series_fingerprint"], "component_evaluations": [row["evaluation"] for row in result["captured_calls"]], "evidence_bindings": [], "candidate_tile_judgments": result["candidate_tile_judgments"], "candidate_component_sentinels": result["candidate_component_sentinels"], "assessment": result["assessment"], "telemetry": {key: result[key] for key in ("call_count", "calls_avoided", "reused_tile_count", "evaluated_tile_count")}}
    value["evaluation_bundle_fingerprint"] = jm.canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": plan["canonical_plan_fingerprint"], "evaluations": value["component_evaluations"]})
    value["assessment_fingerprint"], value["score_fingerprint"] = result["assessment"]["assessment_fingerprint"], result["assessment"]["score_fingerprint"]
    value["complete_record_fingerprint"] = jm.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v1", value)
    return value
def test_replay_rebuilds_requests_without_captured_request_bodies():
    plan = ip.build_incremental_plan([], [], _series()); result = ie.execute_incremental_evaluation(plan, _packets(plan), _Flow())
    assert ie.replay_incremental_evaluations(plan, _packets(plan), [deepcopy(row["evaluation"]) for row in result["captured_calls"]])["assessment"] == result["assessment"]
def test_candidate_contract_replays_without_content_and_rejects_semantic_payloads():
    plan = ip.build_incremental_plan(_prior(), [], _series()); candidate = _candidate(plan, ie.execute_incremental_evaluation(plan, [], _Flow()))
    assert history._sv9_judgment_candidate_replay(candidate, []) == candidate
    for key in "prompt request content url provider_output".split():
        tampered = deepcopy(candidate); tampered["telemetry"][key] = "forbidden"
        with pytest.raises(history.EvidenceVaultSv9JudgmentCandidateError): history._sv9_judgment_candidate_envelope(tampered)
def test_migration_has_append_only_provenance_and_runtime_contract():
    sql = Path("src/history/migrations/031_evidence_vault_sv9_judgment_candidates.sql").read_text()
    assert all(value in sql for value in ("evidence_vault_sv9_judgment_candidates", "evidence_vault_sv9_judgment_evidence_bindings", "PRIMARY KEY (candidate_id, evidence_record_id)", "evidence_records_capture_id_id_content_hash_key", "captures_brand_scan_run_id_key", "FOREIGN KEY (brand_id, scan_run_id, capture_id) REFERENCES b3s_history.captures (brand_id, scan_run_id, id)", "GRANT SELECT, INSERT ON b3s_history.evidence_vault_sv9_judgment_candidates"))
    assert sql.count("BEFORE UPDATE OR DELETE OR TRUNCATE") == 2 and "USING (true)" not in sql and "CREATE POLICY" not in sql
# fmt: on


def _v2_candidate(base, source_scan, capture, operation):
    witness = {
        "schema_version": "evidence-vault-sv9-authoritative-relation-witness-v1",
        "source_scan_id": source_scan,
        "operational_witness": {
            "canonical_memory_version": "2" * 64,
            "adoption_event_id": "00000000-0000-0000-0000-000000000002",
            "adoption_sequence": 1,
            "candidate_packet_fingerprint": "3" * 64,
            "request_fingerprint": "4" * 64,
        },
        "authoritative_relations": [
            {
                "schema_version": "evidence-vault-sv9-authoritative-tile-relation-v1",
                "tile_id": "M1",
                "component_key": "mission",
                "disposition": "relevant",
                "evidence_ref": "evidence:1",
                "evidence_fingerprint": "1" * 64,
                "capture_origin": capture,
                "operation_origin": operation,
                "relation_fingerprint": "5" * 64,
            }
        ],
        "projection_fingerprint": "6" * 64,
        "witness_fingerprint": "7" * 64,
    }
    candidate = deepcopy(base) | {
        "schema_version": "evidence-vault-sv9-judgment-candidate-v2",
        "authoritative_relation_witness": witness,
    }
    candidate["complete_record_fingerprint"] = jm.canonical_fingerprint(
        "evidence-vault-sv9-judgment-candidate-record-v2",
        {key: value for key, value in candidate.items() if key != "complete_record_fingerprint"},
    )
    return candidate


# fmt: off
class _AuthorityFlow:
    def __init__(self, source_scan_id="unused"):
        from src.services.evidence_vault_sv9_shared_process import (
            CoreFlowSv9StrictComponentAdapter,
        )
        class FlowLLM:
            api_key = "test-key"; model = "flow-fake"; last_failure_reason = None; call_failures = []
            def _call_json(self, _system, _user, **_kwargs): return {"detected": False, "content": "", "confidence": "low", "evidence_refs": [], "rationale": "The evidence is insufficient for this block.", "limitations": []}
        class LabelingLLM:
            api_key = None; model = "labeling-fake"; last_failure_reason = None; call_failures = []
        class TileLLM:
            api_key = "test-key"; model = "tile-fake"; last_failure_reason = None
            def __init__(self): self.call_failures = []
            def _call_json(self, _system, _user, **kwargs):
                from src.sv9.rubric import COMPONENTS, tile_ids
                component = next((key for key in COMPONENTS if kwargs.get("schema_name") == f"baldosas_{key}"), None)
                payload = {"baldosas": [{"id": tile, "estado": "sin_evidencia", "evidencia": "", "motivo": "The evidence is insufficient for this tile."} for tile in tile_ids(component)]}
                if component == "coherencia": payload["veredicto"] = "The brand story is not yet coherent."
                return payload
        self.calls = []
        self.delegate = CoreFlowSv9StrictComponentAdapter(snapshot={"run": {"id": source_scan_id, "brand_name": "Example", "url": "https://example.com"}, "raw_inputs": [{"source": "homepage", "payload": {"url": "https://example.com", "text": "Example helps teams work safely."}}]}, source_run_id=source_scan_id, interpretation_llm_factory=FlowLLM, adjudicator_llm_factory=FlowLLM, labeling_llm_factory=LabelingLLM, evaluator_llm_factory=TileLLM, reasoning_llm_factory=TileLLM, gate_authority="veto_only")
    def evaluate_component(self, request):
        from src.sv9.aggregator import score_from_tile_profile
        from src.sv9.models import ComponentResult, TileVerdict
        self.calls.append(request); self.delegate._prepare()
        rows = []
        profile = []
        for requested in request["requested_tiles"]:
            evidence = requested["evidence"]
            supported = requested["tile_id"] == "M1" and bool(evidence)
            rows.append({"tile_id": requested["tile_id"], "assessment_state": "ok" if supported else "sin_evidencia", "supporting_evidence": [{key: evidence[0][key] for key in ("evidence_ref", "evidence_fingerprint")}] if supported else []})
            profile.append(TileVerdict(tile_id=requested["tile_id"], estado="ok" if supported else "sin_evidencia", evidencia=evidence[0]["content"] if supported else "", motivo="Direct literal support." if supported else "The evidence is insufficient for this tile."))
        self.delegate._components[request["component_key"]] = ComponentResult(component=request["component_key"], status="scored", score=score_from_tile_profile(profile), tile_profile=profile, evaluation_model="test-evaluator")
        self.delegate._component_provenance_candidates[request["component_key"]] = self.delegate._candidate
        return ie.ComponentEvaluationOutcome.success(ie.build_component_evaluation(component_key=request["component_key"], series_fingerprint=request["current_series_fingerprint"], request_fingerprint=request["canonical_request_fingerprint"], status="evaluated", tile_results=rows))
    def get_shared_checkpoint_process(self, request): return self.delegate.get_shared_checkpoint_process(request)
    def restore_shared_checkpoint_process(self, request, accepted, value): return self.delegate.restore_shared_checkpoint_process(request, accepted, value)
    def build_shared_analysis_payload(self, assessment): return self.delegate.build_shared_analysis_payload(assessment)

class _LegacyAuthorityFlow:
    def __init__(self): self.calls = []
    def evaluate_component(self, request):
        self.calls.append(request); rows = [{"tile_id": row["tile_id"], "assessment_state": "ok" if row["evidence"] else "sin_evidencia", "supporting_evidence": [{key: evidence[key] for key in ("evidence_ref", "evidence_fingerprint")} for evidence in row["evidence"]]} for row in request["requested_tiles"]]
        return ie.ComponentEvaluationOutcome.success(ie.build_component_evaluation(component_key=request["component_key"], series_fingerprint=request["current_series_fingerprint"], request_fingerprint=request["canonical_request_fingerprint"], status="evaluated", tile_results=rows))

def _operational(repository, scan, previous=()):
    row = _row() | {"ref": f"raw_inputs.{len(previous)}.chunk.0", "content": _row()["content"] if not previous else f"Safer {scan}. {_row()['content']}"}; current = [*previous, row]; memory = repository.get_evidence_vault_operational_memory("example.com"); plan = build_vault_scan_plan(brand_identity="example.com", subject_url="https://example.com", mode="incremental_refresh" if memory else "baseline", current_evidence_records=current, previous_capture_evidence_records=list(previous) or current, known_evidence_records=current, canonical_memory_version=memory["canonical_memory_version"] if memory else None); _persist_baseline(repository, scan, current, plan)
    execute_vault_operation_plan(repository=repository, source_scan_id=scan, worker_id="candidate-worker", llm=ExecutorLLM()); operation = repository.get_capture_operation_plan(scan)["result_payload"]
    repository.review_and_adopt_evidence_vault_operational_source("example.com", source_candidate_packet_fingerprint=operation["source_candidate_packet_fingerprint"], decisions=[{"relation_id": operation["basis_relations"][0]["relation_id"], "decision": "accept", "rationale": "Direct literal support."}], reviewer_id="candidate-reviewer", reviewed_at="2026-08-07T13:00:00+02:00", created_at="2026-08-07T11:00:00Z")
    return current

def _captured_candidate(monkeypatch, repository, scan, series):
    from src.services.evidence_vault_sv9_shared_process import (
        is_core_shared_series_contract,
    )
    projection = project_evidence_vault_sv9_authoritative_relations(repository=repository, source_scan_id=scan); evidence = [{key: row[key] for key in ("evidence_ref", "evidence_fingerprint")} for row in repository.resolve_evidence_vault_sv9_judgment_evidence(scan, [row["evidence_ref"] for row in projection["authoritative_relations"]])["evidence"]]; captured = {}
    def append(_scan, candidate, **_kwargs): captured["candidate"] = deepcopy(candidate); raise RuntimeError
    flow = _AuthorityFlow(scan) if is_core_shared_series_contract(series) else _LegacyAuthorityFlow()
    with monkeypatch.context() as patch: patch.setattr(repository, "append_evidence_vault_sv9_judgment_candidate", append); authority.run_evidence_vault_sv9_authority_evaluation(repository=repository, flow=flow, domain_or_url="example.com", source_scan_id=scan, current_series_contract=series)
    return captured["candidate"], projection, evidence

def _legacy(candidate):
    value = {key: row for key, row in candidate.items() if key != "authoritative_relation_witness"}; value["schema_version"] = "evidence-vault-sv9-judgment-candidate-v1"; value["complete_record_fingerprint"] = jm.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v1", {key: row for key, row in value.items() if key != "complete_record_fingerprint"})
    return value

def _insert(repository, scan, candidate):
    import psycopg; from psycopg.types.json import Jsonb; from uuid import uuid4
    context = repository.load_evidence_vault_sv9_judgment_context(scan)
    identifier = uuid4()
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn: conn.execute(f"INSERT INTO b3s_history.evidence_vault_sv9_judgment_candidates (id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id, operation_plan_id, schema_version, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, evaluation_bundle_fingerprint, assessment_fingerprint, score_fingerprint, complete_record_fingerprint, candidate_payload, authority, review_state, lifecycle_state, runtime_effect) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 'none', 'active', 'shadow_only')", (identifier, context["workspace_id"], context["brand_id"], context["scan_run_id"], scan, context["capture_id"], context["operation_plan_id"], *[candidate[key] for key in ("schema_version", "canonical_plan_fingerprint", "current_series_fingerprint", "candidate_series_fingerprint", "evaluation_bundle_fingerprint", "assessment_fingerprint", "score_fingerprint", "complete_record_fingerprint")], Jsonb(candidate)))
    return str(identifier)
# fmt: on


def _shared_series():
    from src.services.evidence_vault_sv9_shared_process import (
        CORE_SHARED_EVALUATOR_VERSION,
        CORE_SHARED_FLOW_VERSION,
        CORE_SHARED_NORMALIZATION_VERSION,
        CORE_SHARED_PROMPT_VERSION,
    )

    return _series(
        evaluator_version=CORE_SHARED_EVALUATOR_VERSION,
        prompt_version=CORE_SHARED_PROMPT_VERSION,
        flow_version=CORE_SHARED_FLOW_VERSION,
        normalization_version=CORE_SHARED_NORMALIZATION_VERSION,
    )


def _shared_analysis(candidate, source_scan_id):
    from scripts.sv9_flow_sv9_shadow_eval import _result_summary
    from src.sv9.aggregator import aggregate, score_from_tile_profile
    from src.services.evidence_vault_sv9_shared_process import (
        _assessment_output_from_scanner_envelope,
    )
    from src.sv9.models import ComponentResult, TileVerdict
    from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER, tile_ids
    from src.sv9_flow.contracts import (
        BrandEvidencePack,
        BrandInterpretation,
        Sv9FlowCandidate,
    )

    judgments = {row["tile_id"]: row for row in candidate["candidate_tile_judgments"]}
    sentinels = {row["component_key"] for row in candidate["candidate_component_sentinels"]}
    components = {}
    for component in PRESENTATION_ORDER:
        if component in sentinels:
            components[component] = ComponentResult(
                component=component,
                status="not_detected",
            )
            continue
        profile = []
        for tile_id in tile_ids(component):
            judgment = judgments[tile_id]
            state = judgment["assessment_state"]
            profile.append(
                TileVerdict(
                    tile_id=tile_id,
                    estado=state,
                    evidencia="Shared evidence." if state == "ok" else "",
                    motivo="No supporting evidence." if state != "ok" else "",
                )
            )
        components[component] = ComponentResult(
            component=component,
            status="scored",
            score=score_from_tile_profile(profile),
            tile_profile=profile,
            evaluation_model="test-evaluator",
        )
    assert set(components) == set(COMPONENTS)
    flow_candidate = Sv9FlowCandidate(
        evidence_pack=BrandEvidencePack(
            brand_name="Example",
            url="https://example.com",
        ),
        interpretation=BrandInterpretation(
            brand_name="Example",
            url="https://example.com",
            blocks={},
            evidence_refs={},
        ),
    ).to_dict()
    result = aggregate(
        components,
        brand_name="Example",
        url="https://example.com",
        source_run_id=source_scan_id,
    ).to_dict()
    assert _assessment_output_from_scanner_envelope(result["assessment"]) == candidate["assessment"]
    return {
        "schema_version": "evidence-vault-sv9-shared-analysis-payload-v1",
        "analysis_payload": {
            "schema_version": "sv9-flow-sv9-shadow-eval-v1",
            "source_run_id": source_scan_id,
            "brand_name": "Example",
            "url": "https://example.com",
            "visual_acquisition_present": False,
            "flow": {"candidate": flow_candidate},
            "sv9": _result_summary(result) | {"result": result},
            "llm_usage": {},
        },
        "evaluation_components": {key: value.to_dict() for key, value in components.items()},
        "component_provenance": {
            key: deepcopy(flow_candidate)
            for key in PRESENTATION_ORDER
            if key != "coherencia"
        },
    }


def _shared_snapshot_values(repository, scan, candidate_id, candidate, payload):
    import hashlib

    raw = history.canonical_json_bytes(payload)
    context = repository.load_evidence_vault_sv9_judgment_context(scan)
    return {
        "candidate_id": candidate_id,
        "workspace_id": context["workspace_id"],
        "brand_id": context["brand_id"],
        "scan_run_id": context["scan_run_id"],
        "source_scan_id": scan,
        "capture_id": context["capture_id"],
        "operation_plan_id": context["operation_plan_id"],
        "schema_version": "evidence-vault-sv9-shared-analysis-snapshot-v1",
        "candidate_complete_record_fingerprint": candidate["complete_record_fingerprint"],
        "canonical_plan_fingerprint": candidate["canonical_plan_fingerprint"],
        "current_series_fingerprint": candidate["current_series_fingerprint"],
        "candidate_series_fingerprint": candidate["candidate_series_fingerprint"],
        "evaluation_bundle_fingerprint": candidate["evaluation_bundle_fingerprint"],
        "assessment_fingerprint": candidate["assessment_fingerprint"],
        "score_fingerprint": candidate["score_fingerprint"],
        "payload_sha256": hashlib.sha256(raw).hexdigest(),
        "payload": payload,
        "payload_raw": raw,
    }


def test_candidate_witness_migration_has_exact_json_types_and_row_alignment():
    sql = Path("src/history/migrations/033_evidence_vault_sv9_judgment_candidate_witness.sql").read_text()
    base_types = {
        "schema_version": "string",
        "plan": "object",
        "canonical_plan_fingerprint": "string",
        "current_series_fingerprint": "string",
        "candidate_series_fingerprint": "string",
        "component_evaluations": "array",
        "evidence_bindings": "array",
        "candidate_tile_judgments": "array",
        "candidate_component_sentinels": "array",
        "assessment": "object",
        "telemetry": "object",
        "evaluation_bundle_fingerprint": "string",
        "assessment_fingerprint": "string",
        "score_fingerprint": "string",
        "complete_record_fingerprint": "string",
    }
    assert all(f"jsonb_typeof(candidate_payload->'{field}') = '{kind}'" in sql for field, kind in base_types.items())
    assert all(
        f"candidate_payload->>'{field}' = {field}" in sql
        for field in (
            "schema_version",
            "canonical_plan_fingerprint",
            "current_series_fingerprint",
            "candidate_series_fingerprint",
            "evaluation_bundle_fingerprint",
            "assessment_fingerprint",
            "score_fingerprint",
            "complete_record_fingerprint",
        )
    )
    witness = "candidate_payload->'authoritative_relation_witness'"
    assert all(
        f"jsonb_typeof({field}) = '{kind}'" in sql
        for field, kind in {
            witness: "object",
            f"{witness}->'schema_version'": "string",
            f"{witness}->'source_scan_id'": "string",
            f"{witness}->'operational_witness'": "object",
            f"{witness}->'authoritative_relations'": "array",
            f"{witness}->'projection_fingerprint'": "string",
            f"{witness}->'witness_fingerprint'": "string",
            f"{witness}->'operational_witness'->'canonical_memory_version'": "string",
            f"{witness}->'operational_witness'->'adoption_event_id'": "string",
            f"{witness}->'operational_witness'->'adoption_sequence'": "number",
            f"{witness}->'operational_witness'->'candidate_packet_fingerprint'": "string",
            f"{witness}->'operational_witness'->'request_fingerprint'": "string",
        }.items()
    )
    assert "candidate_payload->'authoritative_relation_witness'->>'source_scan_id' = source_scan_id" in sql
    assert (
        sql.count("jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations'")
        == 13
    )
    assert "authoritative_relations', 'strict $[*] ? (@.type() != \"object\"" in sql
    assert all(
        f'exists({field} ? (@.type() != "{kind}"))' in sql
        for field, kind in {
            "@.schema_version": "string",
            "@.tile_id": "string",
            "@.component_key": "string",
            "@.disposition": "string",
            "@.evidence_ref": "string",
            "@.evidence_fingerprint": "string",
            "@.capture_origin": "object",
            "@.capture_origin.capture_id": "string",
            "@.capture_origin.capture_fingerprint": "string",
            "@.operation_origin": "object",
            "@.operation_origin.operation_id": "string",
            "@.operation_origin.operation_fingerprint": "string",
            "@.relation_fingerprint": "string",
        }.items()
    )
    assert all(
        f"!(exists({field}))" in sql
        for field in (
            "@.schema_version",
            "@.tile_id",
            "@.component_key",
            "@.disposition",
            "@.evidence_ref",
            "@.evidence_fingerprint",
            "@.capture_origin",
            "@.capture_origin.capture_id",
            "@.capture_origin.capture_fingerprint",
            "@.operation_origin",
            "@.operation_origin.operation_id",
            "@.operation_origin.operation_fingerprint",
            "@.relation_fingerprint",
        )
    )
    assert all(
        value in sql
        for value in (
            "exists(@.keyvalue()",
            "exists(@.capture_origin.keyvalue()",
            "exists(@.operation_origin.keyvalue()",
        )
    )
    uuid_pattern = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    assert all(
        f'@.{field} like_regex "{uuid_pattern}"' in sql
        for field in ("capture_origin.capture_id", "operation_origin.operation_id")
    )
    assert ") IS TRUE) NOT VALID;" in sql


def test_empty_witness_migration_preserves_enforcement_and_binds_all_plan_origins():
    sql = Path("src/history/migrations/035_evidence_vault_sv9_empty_relation_witness.sql").read_text()
    assert sql.index("ADD CONSTRAINT") < sql.index("VALIDATE CONSTRAINT") < sql.index("DROP CONSTRAINT") < sql.index("RENAME CONSTRAINT")
    assert ") IS TRUE) NOT VALID;" in sql
    assert "strict $[*] ? (@.type() != \"object\")" in sql
    assert "candidate_payload->'plan'->'delta_projections' <> '[]'::jsonb" in sql
    for key in ("capture_id", "capture_fingerprint", "operation_id", "operation_fingerprint"):
        assert f"!= ${key}" in sql
    assert "= capture_id::text" in sql and "= operation_plan_id::text" in sql


# fmt: off
@pytest.mark.skipif(not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1", reason="requires disposable PostgreSQL")
def test_candidate_witness_check_rejects_invalid_payloads_and_row_payload_divergence():
    import psycopg
    from psycopg.types.json import Jsonb
    from uuid import uuid4
    from tests.test_evidence_vault_operation_execution_postgres import _persist_baseline, _reset_repository
    dsn = os.environ["B3S_TEST_DATABASE_URL"]; repository = _reset_repository(); scan = "candidate-witness-v1"; _persist_baseline(repository, scan); source = repository.load_evidence_vault_sv9_judgment_context(scan); v2_scan = "candidate-witness-v2"; _persist_baseline(repository, v2_scan); v2_source = repository.load_evidence_vault_sv9_judgment_context(v2_scan)
    plan = ip.build_incremental_plan([], [], _series()); base = _candidate(plan, ie.execute_incremental_evaluation(plan, _packets(plan), _Flow()))
    capture, operation = {"capture_id": str(v2_source["capture_id"]), "capture_fingerprint": str(v2_source["capture_fingerprint"])}, {"operation_id": str(v2_source["operation_plan_id"]), "operation_fingerprint": str(v2_source["operation_fingerprint"])}; v2 = _v2_candidate(base, v2_scan, capture, operation); witness = v2["authoritative_relation_witness"]
    def insert(index, context, source_scan, payload, row_overrides=None):
        payload = deepcopy(payload); row_values = {"schema_version": payload["schema_version"], "canonical_plan_fingerprint": payload["canonical_plan_fingerprint"], "current_series_fingerprint": payload["current_series_fingerprint"], "candidate_series_fingerprint": payload["candidate_series_fingerprint"], "evaluation_bundle_fingerprint": payload["evaluation_bundle_fingerprint"], "assessment_fingerprint": payload["assessment_fingerprint"], "score_fingerprint": payload["score_fingerprint"], "complete_record_fingerprint": str(payload.get("complete_record_fingerprint") or f"{index:064x}")} | (row_overrides or {})
        with psycopg.connect(dsn, autocommit=True) as conn: conn.execute("INSERT INTO b3s_history.evidence_vault_sv9_judgment_candidates (id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id, operation_plan_id, schema_version, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, evaluation_bundle_fingerprint, assessment_fingerprint, score_fingerprint, complete_record_fingerprint, candidate_payload, authority, review_state, lifecycle_state, runtime_effect) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 'none', 'active', 'shadow_only')", (uuid4(), context["workspace_id"], context["brand_id"], context["scan_run_id"], source_scan, context["capture_id"], context["operation_plan_id"], row_values["schema_version"], row_values["canonical_plan_fingerprint"], row_values["current_series_fingerprint"], row_values["candidate_series_fingerprint"], row_values["evaluation_bundle_fingerprint"], row_values["assessment_fingerprint"], row_values["score_fingerprint"], row_values["complete_record_fingerprint"], Jsonb(payload)))
    def rejected(*args):
        with pytest.raises(psycopg.errors.CheckViolation) as error: insert(*args)
        assert error.value.diag.constraint_name == "evidence_vault_sv9_judgment_candidate_payload_witness_check"
    def malformed(payload, path, value):
        payload = deepcopy(payload); target = payload
        for key in path[:-1]: target = target[key]
        target[path[-1]] = value; return payload
    number = int("8" * 64)
    rejected(9, source, scan, malformed(base, ("schema_version",), []), {"schema_version": "evidence-vault-sv9-judgment-candidate-v1"})
    for index, field in enumerate(("canonical_plan_fingerprint", "current_series_fingerprint", "candidate_series_fingerprint", "evaluation_bundle_fingerprint", "assessment_fingerprint", "score_fingerprint", "complete_record_fingerprint"), 10):
        rejected(index, source, scan, malformed(base, (field,), number), {field: str(number)})
    base_containers = {"plan": [], "component_evaluations": {}, "evidence_bindings": {}, "candidate_tile_judgments": {}, "candidate_component_sentinels": {}, "assessment": [], "telemetry": []}
    for index, (field, value) in enumerate(base_containers.items(), 20):
        rejected(index, source, scan, malformed(base, (field,), value))
    numeric_scan = "9" * 64; _persist_baseline(repository, numeric_scan); numeric_source = repository.load_evidence_vault_sv9_judgment_context(numeric_scan)
    numeric_capture, numeric_operation = {"capture_id": str(numeric_source["capture_id"]), "capture_fingerprint": str(numeric_source["capture_fingerprint"])}, {"operation_id": str(numeric_source["operation_plan_id"]), "operation_fingerprint": str(numeric_source["operation_fingerprint"])}; numeric_v2 = _v2_candidate(base, numeric_scan, numeric_capture, numeric_operation)
    rejected(30, numeric_source, numeric_scan, malformed(numeric_v2, ("authoritative_relation_witness", "source_scan_id"), int(numeric_scan)))
    v2_shapes = [(("authoritative_relation_witness",), []), (("authoritative_relation_witness", "schema_version"), []), (("authoritative_relation_witness", "projection_fingerprint"), number), (("authoritative_relation_witness", "witness_fingerprint"), number), (("authoritative_relation_witness", "operational_witness"), []), (("authoritative_relation_witness", "operational_witness", "canonical_memory_version"), number), (("authoritative_relation_witness", "operational_witness", "adoption_event_id"), []), (("authoritative_relation_witness", "operational_witness", "adoption_sequence"), "1"), (("authoritative_relation_witness", "operational_witness", "candidate_packet_fingerprint"), number), (("authoritative_relation_witness", "operational_witness", "request_fingerprint"), number), (("authoritative_relation_witness", "authoritative_relations"), {}), (("authoritative_relation_witness", "authoritative_relations", 0), []), (("authoritative_relation_witness", "authoritative_relations", 0), 1), (("authoritative_relation_witness", "authoritative_relations", 0), None), (("authoritative_relation_witness", "authoritative_relations", 0, "schema_version"), []), (("authoritative_relation_witness", "authoritative_relations", 0, "tile_id"), 1), (("authoritative_relation_witness", "authoritative_relations", 0, "component_key"), 1), (("authoritative_relation_witness", "authoritative_relations", 0, "disposition"), []), (("authoritative_relation_witness", "authoritative_relations", 0, "evidence_ref"), 1), (("authoritative_relation_witness", "authoritative_relations", 0, "evidence_fingerprint"), number), (("authoritative_relation_witness", "authoritative_relations", 0, "capture_origin"), []), (("authoritative_relation_witness", "authoritative_relations", 0, "capture_origin", "capture_id"), 1), (("authoritative_relation_witness", "authoritative_relations", 0, "capture_origin", "capture_fingerprint"), number), (("authoritative_relation_witness", "authoritative_relations", 0, "operation_origin"), []), (("authoritative_relation_witness", "authoritative_relations", 0, "operation_origin", "operation_id"), 1), (("authoritative_relation_witness", "authoritative_relations", 0, "operation_origin", "operation_fingerprint"), number), (("authoritative_relation_witness", "authoritative_relations", 0, "relation_fingerprint"), number)]
    for index, (path, value) in enumerate(v2_shapes, 40):
        rejected(index, v2_source, v2_scan, malformed(v2, path, value))
    assert v2["schema_version"] != "evidence-vault-sv9-judgment-candidate-v1"
    rejected(100, v2_source, v2_scan, v2, {"schema_version": "evidence-vault-sv9-judgment-candidate-v1"})
    assert v2["authoritative_relation_witness"]["source_scan_id"] != scan
    rejected(101, source, scan, v2)
    mismatches = {"canonical_plan_fingerprint": "1" * 64, "current_series_fingerprint": "2" * 64, "candidate_series_fingerprint": "3" * 64, "evaluation_bundle_fingerprint": "4" * 64, "assessment_fingerprint": "5" * 64, "score_fingerprint": "6" * 64, "complete_record_fingerprint": "7" * 64}
    for index, (field, value) in enumerate(mismatches.items(), 102):
        assert base[field] != value
        rejected(index, source, scan, base, {field: value})
    cases = [(True, source, scan, base), (True, v2_source, v2_scan, v2), (False, source, scan, {key: value for key, value in base.items() if key != "complete_record_fingerprint"}), (False, source, scan, base | {"complete_record_fingerprint": None}), (False, source, scan, base | {"extra": True}), (False, v2_source, v2_scan, v2 | {"authoritative_relation_witness": witness | {"authoritative_relations": []}}), (False, v2_source, v2_scan, v2 | {"authoritative_relation_witness": witness | {"authoritative_relations": [{}]}})]
    for index, (valid, context, source_scan, payload) in enumerate(cases, 1):
        if valid: insert(index, context, source_scan, payload); continue
        payload = payload | {"canonical_plan_fingerprint": f"{index:064x}"}
        rejected(index, context, source_scan, payload)
    empty = deepcopy(v2); empty["canonical_plan_fingerprint"] = "a" * 64
    origins = {"capture_origin": capture, "operation_origin": operation}
    empty["authoritative_relation_witness"] |= origins | {"authoritative_relations": []}
    empty["plan"]["delta_projections"] = [deepcopy(origins), deepcopy(origins)]
    for value in (None, [], [[]], [[origins]], [origins, []], [origins, {}]):
        rejected(200, v2_source, v2_scan, malformed(empty, ("plan", "delta_projections"), value))
    for origin, fingerprint in (("capture_origin", "capture_fingerprint"), ("operation_origin", "operation_fingerprint")):
        for value in (None, {}, [], [origins[origin]], origins[origin] | {"extra": True}):
            rejected(201, v2_source, v2_scan, malformed(empty, ("authoritative_relation_witness", origin), value))
        rejected(202, v2_source, v2_scan, malformed(empty, ("authoritative_relation_witness", origin, fingerprint), "f" * 64))
        rejected(203, v2_source, v2_scan, malformed(empty, ("plan", "delta_projections", 1, origin, fingerprint), "f" * 64))
    insert(204, v2_source, v2_scan, empty)
# fmt: on


# fmt: off
@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_repository_fences_witnessed_candidate_append_and_invalid_readback(monkeypatch):
    repository, scan = _reset_repository(), "candidate-v2-base"; current = _operational(repository, scan)
    first, projection, evidence = _captured_candidate(monkeypatch, repository, scan, _series()); second, _, _ = _captured_candidate(monkeypatch, repository, scan, _series(prompt_version="v2")); stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(scan, first)
    resolved = repository.resolve_evidence_vault_sv9_judgment_evidence(scan, [row["evidence_ref"] for row in evidence])["evidence"]
    identities = {(row["evidence_ref"], row["evidence_fingerprint"]): row["evidence_record_id"] for row in resolved}
    expected_bindings = [{"tile_id": item["tile_id"], "evidence_record_id": identities[(row["evidence_ref"], row["evidence_fingerprint"])], **row} for item in first["plan"]["items"] if item["tile_id"] in first["plan"]["tile_workset"] for row in item["evidence"]]
    assert inserted and stored["evidence_bindings"] == expected_bindings
    assert stored["authoritative_relation_witness"]["authoritative_relations"] == projection["authoritative_relations"]
    assert repository.get_evidence_vault_sv9_judgment_candidate(scan, canonical_plan_fingerprint=first["canonical_plan_fingerprint"]) == stored
    current = _operational(repository, "candidate-v2-next", current)
    with pytest.raises(history.EvidenceVaultSv9AuthoritativeRelationStaleWitnessError): repository.append_evidence_vault_sv9_judgment_candidate(scan, second)
    assert repository.get_evidence_vault_sv9_judgment_candidate(scan, canonical_plan_fingerprint=second["canonical_plan_fingerprint"]) is None
    repository = _reset_repository(); legacy_scan = "candidate-v1-legacy"; _operational(repository, legacy_scan); third, _, _ = _captured_candidate(monkeypatch, repository, legacy_scan, _series(prompt_version="v3")); legacy, _ = repository.append_evidence_vault_sv9_judgment_candidate(legacy_scan, _legacy(third))
    replayed_legacy = repository.get_evidence_vault_sv9_judgment_candidate(legacy_scan, canonical_plan_fingerprint=legacy["canonical_plan_fingerprint"])
    assert replayed_legacy["schema_version"].endswith("v1") and replayed_legacy["complete_record_fingerprint"] == legacy["complete_record_fingerprint"]
    repository = _reset_repository(); invalid_scan = "candidate-v2-invalid"; _operational(repository, invalid_scan); invalid, _, _ = _captured_candidate(monkeypatch, repository, invalid_scan, _series(prompt_version="v4")); relation = invalid["authoritative_relation_witness"]["authoritative_relations"][0]; relation["relation_fingerprint"] = "0" * 64 if relation["relation_fingerprint"] != "0" * 64 else "1" * 64; invalid["complete_record_fingerprint"] = jm.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v2", {key: row for key, row in invalid.items() if key != "complete_record_fingerprint"}); _insert(repository, invalid_scan, invalid)
    flow = _AuthorityFlow(); outcome = authority.run_evidence_vault_sv9_authority_evaluation(repository=repository, flow=flow, domain_or_url="example.com", source_scan_id=invalid_scan, current_series_contract=_series(prompt_version="v4"))
    assert outcome["status"] == "review_required" and outcome["reason_codes"] == ["invalid_authoritative_relation_witness"] and not flow.calls
# fmt: on


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_shared_analysis_candidate_append_is_atomic_immutable_and_readable(monkeypatch):
    repository, scan = _reset_repository(), "candidate-shared-analysis"
    _operational(repository, scan)
    candidate, _, _ = _captured_candidate(monkeypatch, repository, scan, _shared_series())
    payload = _shared_analysis(candidate, scan)

    with pytest.raises(
        history.EvidenceVaultSv9JudgmentCandidateError,
        match="shared analysis is required",
    ):
        repository.append_evidence_vault_sv9_judgment_candidate(scan, candidate)
    assert (
        repository.get_evidence_vault_sv9_judgment_candidate(
            scan,
            canonical_plan_fingerprint=candidate["canonical_plan_fingerprint"],
        )
        is None
    )

    mismatched = deepcopy(payload)
    mismatched["analysis_payload"]["sv9"]["assessment"]["score_fingerprint"] = "0" * 64
    with pytest.raises(
        history.EvidenceVaultSv9JudgmentCandidateError,
        match="shared analysis payload is invalid",
    ):
        repository.append_evidence_vault_sv9_judgment_candidate(scan, candidate, shared_analysis_payload=mismatched)
    assert (
        repository.get_evidence_vault_sv9_judgment_candidate(
            scan,
            canonical_plan_fingerprint=candidate["canonical_plan_fingerprint"],
        )
        is None
    )

    wrong_brand = deepcopy(payload)
    wrong_brand["analysis_payload"]["url"] = "https://other.example"
    wrong_brand["analysis_payload"]["sv9"]["result"]["url"] = "https://other.example"
    with pytest.raises(
        history.EvidenceVaultSv9JudgmentCandidateError,
        match="shared analysis payload is invalid",
    ):
        repository.append_evidence_vault_sv9_judgment_candidate(scan, candidate, shared_analysis_payload=wrong_brand)

    missing_owner = deepcopy(payload)
    missing_owner["component_provenance"].pop("mission")
    with pytest.raises(
        history.EvidenceVaultSv9JudgmentCandidateError,
        match="shared analysis payload is invalid",
    ):
        repository.append_evidence_vault_sv9_judgment_candidate(
            scan,
            candidate,
            shared_analysis_payload=missing_owner,
        )

    wrong_owner = deepcopy(payload)
    wrong_owner["component_provenance"]["mission"]["evidence_pack"]["url"] = (
        "https://other.example"
    )
    wrong_owner["component_provenance"]["mission"]["interpretation"]["url"] = (
        "https://other.example"
    )
    with pytest.raises(
        history.EvidenceVaultSv9JudgmentCandidateError,
        match="shared analysis payload is invalid",
    ):
        repository.append_evidence_vault_sv9_judgment_candidate(
            scan,
            candidate,
            shared_analysis_payload=wrong_owner,
        )

    stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(
        scan, candidate, shared_analysis_payload=payload
    )
    assert inserted
    assert repository.get_evidence_vault_sv9_shared_analysis(stored["id"]) == payload

    conflicting = deepcopy(payload)
    conflicting["analysis_payload"]["llm_usage"] = {"different": True}
    with pytest.raises(
        history.EvidenceVaultSv9JudgmentCandidateConflictError,
        match="shared analysis occurrence conflicts",
    ):
        repository.append_evidence_vault_sv9_judgment_candidate(scan, candidate, shared_analysis_payload=conflicting)
    assert repository.get_evidence_vault_sv9_shared_analysis(stored["id"]) == payload

    from src.services.evidence_vault_sv9_authority_event import (
        authority_application_idempotency_fingerprint,
    )

    request = history._sv9_authority_request("adopt_candidate", stored["id"], None, None, scan)
    adopted, replayed = repository.adopt_evidence_vault_sv9_judgment_candidate(
        scan,
        stored["id"],
        expected_predecessor_event_fingerprint=None,
        idempotency_key_hash=authority_application_idempotency_fingerprint(request),
    )
    assert not replayed
    assert adopted["active_authority_event"]["candidate_id"] == stored["id"]

    repeat_flow = _AuthorityFlow()
    repeated = authority.run_evidence_vault_sv9_authority_evaluation(
        repository=repository,
        flow=repeat_flow,
        domain_or_url="example.com",
        source_scan_id=scan,
        current_series_contract=_shared_series(),
    )
    assert repeated["status"] == "no_new_score"
    assert repeated["calls_issued"] == 0
    assert repeated["calls_avoided"] == 10
    assert not repeat_flow.calls

    import hashlib

    import psycopg
    from psycopg.types.json import Jsonb

    corrupted = deepcopy(payload)
    corrupted["analysis_payload"]["url"] = "https://other.example"
    corrupted["analysis_payload"]["sv9"]["result"]["url"] = "https://other.example"
    corrupted_raw = history.canonical_json_bytes(corrupted)
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn:
        try:
            conn.execute("ALTER TABLE b3s_history.evidence_vault_sv9_shared_analysis_snapshots DISABLE TRIGGER USER")
            conn.execute(
                "UPDATE b3s_history.evidence_vault_sv9_shared_analysis_snapshots "
                "SET payload = %s, payload_raw = %s, payload_sha256 = %s "
                "WHERE candidate_id = %s",
                (
                    Jsonb(corrupted),
                    corrupted_raw,
                    hashlib.sha256(corrupted_raw).hexdigest(),
                    stored["id"],
                ),
            )
        finally:
            conn.execute("ALTER TABLE b3s_history.evidence_vault_sv9_shared_analysis_snapshots ENABLE TRIGGER USER")
    with pytest.raises(
        history.EvidenceVaultSv9JudgmentCandidateError,
        match="shared analysis payload is invalid",
    ):
        repository.get_evidence_vault_sv9_shared_analysis(stored["id"])


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_shared_analysis_database_guards_raw_digest_binding_and_adoption(monkeypatch):
    import hashlib

    import psycopg
    from psycopg.types.json import Jsonb

    repository, scan = _reset_repository(), "candidate-shared-analysis-guards"
    _operational(repository, scan)
    candidate, _, _ = _captured_candidate(monkeypatch, repository, scan, _shared_series())
    candidate_id = _insert(repository, scan, candidate)
    context = repository.load_evidence_vault_sv9_judgment_context(scan)
    bindings = {row["evidence_record_id"]: row["evidence_fingerprint"] for row in candidate["evidence_bindings"]}
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn:
        for evidence_record_id, evidence_fingerprint in bindings.items():
            conn.execute(
                "INSERT INTO b3s_history.evidence_vault_sv9_judgment_evidence_bindings "
                "(candidate_id, workspace_id, brand_id, scan_run_id, capture_id, "
                "operation_plan_id, evidence_record_id, evidence_fingerprint) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    candidate_id,
                    context["workspace_id"],
                    context["brand_id"],
                    context["scan_run_id"],
                    context["capture_id"],
                    context["operation_plan_id"],
                    evidence_record_id,
                    evidence_fingerprint,
                ),
            )
    payload = _shared_analysis(candidate, scan)
    values = _shared_snapshot_values(repository, scan, candidate_id, candidate, payload)
    columns = tuple(values)
    statement = (
        "INSERT INTO b3s_history.evidence_vault_sv9_shared_analysis_snapshots "
        f"({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
    )

    def insert_snapshot(**overrides):
        row = values | overrides
        parameters = tuple(Jsonb(row[key]) if key == "payload" else row[key] for key in columns)
        with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn:
            conn.execute(statement, parameters)

    malformed = deepcopy(payload)
    malformed.pop("evaluation_components")
    with pytest.raises(
        psycopg.errors.RaiseException,
        match="shared analysis component payload is invalid",
    ):
        insert_snapshot(payload=malformed)

    divergent_raw = b'{"not":"the payload"}'
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_snapshot(
            payload_raw=divergent_raw,
            payload_sha256=hashlib.sha256(divergent_raw).hexdigest(),
        )

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_snapshot(payload_sha256="0" * 64)

    empty_components = deepcopy(payload)
    empty_components["evaluation_components"] = {}
    empty_raw = history.canonical_json_bytes(empty_components)
    with pytest.raises(
        psycopg.errors.RaiseException,
        match="shared analysis component set is invalid",
    ):
        insert_snapshot(
            payload=empty_components,
            payload_raw=empty_raw,
            payload_sha256=hashlib.sha256(empty_raw).hexdigest(),
        )

    empty_provenance = deepcopy(payload)
    empty_provenance["component_provenance"] = {}
    empty_provenance_raw = history.canonical_json_bytes(empty_provenance)
    with pytest.raises(
        psycopg.errors.RaiseException,
        match="shared analysis component provenance set is invalid",
    ):
        insert_snapshot(
            payload=empty_provenance,
            payload_raw=empty_provenance_raw,
            payload_sha256=hashlib.sha256(empty_provenance_raw).hexdigest(),
        )

    malformed_provenance = deepcopy(payload)
    malformed_provenance["component_provenance"]["mission"] = None
    malformed_provenance_raw = history.canonical_json_bytes(malformed_provenance)
    with pytest.raises(
        psycopg.errors.RaiseException,
        match="shared analysis component provenance is invalid",
    ):
        insert_snapshot(
            payload=malformed_provenance,
            payload_raw=malformed_provenance_raw,
            payload_sha256=hashlib.sha256(malformed_provenance_raw).hexdigest(),
        )

    malformed_component = deepcopy(payload)
    malformed_component["evaluation_components"]["mission"] = {}
    malformed_raw = history.canonical_json_bytes(malformed_component)
    with pytest.raises(
        psycopg.errors.RaiseException,
        match="shared analysis component payload is invalid",
    ):
        insert_snapshot(
            payload=malformed_component,
            payload_raw=malformed_raw,
            payload_sha256=hashlib.sha256(malformed_raw).hexdigest(),
        )

    forged_score = deepcopy(payload)
    forged_score["evaluation_components"]["mission"]["score"] += 1
    forged_score_raw = history.canonical_json_bytes(forged_score)
    with pytest.raises(
        psycopg.errors.RaiseException,
        match="shared analysis component payload is invalid",
    ):
        insert_snapshot(
            payload=forged_score,
            payload_raw=forged_score_raw,
            payload_sha256=hashlib.sha256(forged_score_raw).hexdigest(),
        )

    null_component_status = deepcopy(payload)
    null_component_status["evaluation_components"]["mission"]["status"] = None
    null_status_raw = history.canonical_json_bytes(null_component_status)
    with pytest.raises(psycopg.errors.RaiseException):
        insert_snapshot(
            payload=null_component_status,
            payload_raw=null_status_raw,
            payload_sha256=hashlib.sha256(null_status_raw).hexdigest(),
        )

    null_tile_state = deepcopy(payload)
    null_tile_state["evaluation_components"]["mission"]["tile_profile"][0]["estado"] = None
    null_tile_raw = history.canonical_json_bytes(null_tile_state)
    with pytest.raises(psycopg.errors.RaiseException):
        insert_snapshot(
            payload=null_tile_state,
            payload_raw=null_tile_raw,
            payload_sha256=hashlib.sha256(null_tile_raw).hexdigest(),
        )

    extra_component = deepcopy(payload)
    extra_component["evaluation_components"]["unexpected"] = deepcopy(
        extra_component["evaluation_components"]["mission"]
    )
    extra_component_raw = history.canonical_json_bytes(extra_component)
    with pytest.raises(
        psycopg.errors.RaiseException,
        match="shared analysis component set is invalid",
    ):
        insert_snapshot(
            payload=extra_component,
            payload_raw=extra_component_raw,
            payload_sha256=hashlib.sha256(extra_component_raw).hexdigest(),
        )

    with pytest.raises(
        psycopg.errors.RaiseException,
        match="shared analysis candidate binding is invalid",
    ):
        insert_snapshot(source_scan_id=f"{scan}-other")

    candidate_for_event = candidate | {
        "id": candidate_id,
        "source_scan_id": scan,
    }
    request = history._sv9_authority_request("adopt_candidate", candidate_id, None, None, scan)
    from src.services.evidence_vault_sv9_authority_event import (
        authority_application_idempotency_fingerprint,
    )

    idempotency = authority_application_idempotency_fingerprint(request)
    event = history._sv9_authority_event(
        context,
        None,
        request,
        idempotency,
        candidate_for_event,
        context,
        None,
    )
    authority_columns = history._SV9_AUTHORITY_COLUMNS
    authority_values = tuple(Jsonb(event[key]) if key == "event_payload" else event[key] for key in authority_columns)
    with pytest.raises(
        psycopg.errors.RaiseException,
        match="shared analysis is required before authority adoption",
    ):
        with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn:
            conn.execute(
                "INSERT INTO b3s_history.evidence_vault_sv9_judgment_authority_events "
                f"({', '.join(authority_columns)}) VALUES "
                f"({', '.join(['%s'] * len(authority_columns))})",
                authority_values,
            )

    noncanonical_raw = json.dumps(payload, ensure_ascii=False, sort_keys=False, indent=2).encode("utf-8")
    insert_snapshot(
        payload_raw=noncanonical_raw,
        payload_sha256=hashlib.sha256(noncanonical_raw).hexdigest(),
    )
    assert repository.get_evidence_vault_sv9_shared_analysis(candidate_id) == payload


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_shared_checkpoint_database_rejects_null_state_binding_and_raw_tampering(
    monkeypatch,
):
    import hashlib
    from uuid import uuid4

    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    repository, scan = _reset_repository(), "candidate-shared-process-guards"
    _operational(repository, scan)
    _captured_candidate(monkeypatch, repository, scan, _shared_series())

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], row_factory=dict_row) as conn:
        source = conn.execute(
            "SELECT * FROM b3s_history.evidence_vault_sv9_evaluation_checkpoints "
            "WHERE shared_process_payload #>> '{binding,component_key}' = 'mission'"
        ).fetchone()
    assert source is not None
    columns = [key for key in source if key != "created_at"]
    statement = (
        "INSERT INTO b3s_history.evidence_vault_sv9_evaluation_checkpoints "
        f"({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
    )

    def insert_clone(mutate=None, *, divergent_raw=False):
        row = deepcopy(source)
        row["id"] = uuid4()
        fingerprint = hashlib.sha256(str(row["id"]).encode()).hexdigest()
        row["checkpoint_fingerprint"] = fingerprint
        row["checkpoint_payload"]["checkpoint_fingerprint"] = fingerprint
        process = row["shared_process_payload"]
        if mutate is not None:
            mutate(process)
        raw = b'{"schema_version":"tampered"}' if divergent_raw else history.canonical_json_bytes(process)
        row["shared_process_payload_raw"] = raw
        row["shared_process_payload_sha256"] = hashlib.sha256(raw).hexdigest()
        parameters = tuple(
            Jsonb(row[key]) if key in {"checkpoint_payload", "shared_process_payload"} else row[key] for key in columns
        )
        with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn:
            conn.execute(statement, parameters)

    mutations = [
        lambda value: value["component_result"].__setitem__("status", None),
        lambda value: value["component_result"]["tile_profile"][0].__setitem__("estado", None),
        lambda value: value["binding"].__setitem__("source_scan_id", "different-source"),
        lambda value: value["binding"].__setitem__("unexpected", True),
    ]
    for mutate in mutations:
        with pytest.raises(psycopg.errors.CheckViolation):
            insert_clone(mutate)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_clone(divergent_raw=True)
