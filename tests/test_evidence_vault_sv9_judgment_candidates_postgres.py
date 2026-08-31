from copy import deepcopy
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
    projection = project_evidence_vault_sv9_authoritative_relations(repository=repository, source_scan_id=scan); evidence = [{key: row[key] for key in ("evidence_ref", "evidence_fingerprint")} for row in repository.resolve_evidence_vault_sv9_judgment_evidence(scan, [row["evidence_ref"] for row in projection["authoritative_relations"]])["evidence"]]; captured = {}
    def append(_scan, candidate, **_kwargs): captured["candidate"] = deepcopy(candidate); raise RuntimeError
    with monkeypatch.context() as patch: patch.setattr(repository, "append_evidence_vault_sv9_judgment_candidate", append); authority.run_evidence_vault_sv9_authority_evaluation(repository=repository, flow=_AuthorityFlow(), domain_or_url="example.com", source_scan_id=scan, current_evidence=evidence, authoritative_relations=projection["authoritative_relations"], current_series_contract=series)
    return captured["candidate"], projection, evidence

def _legacy(candidate):
    value = {key: row for key, row in candidate.items() if key != "authoritative_relation_witness"}; value["schema_version"] = "evidence-vault-sv9-judgment-candidate-v1"; value["complete_record_fingerprint"] = jm.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v1", {key: row for key, row in value.items() if key != "complete_record_fingerprint"})
    return value

def _insert(repository, scan, candidate):
    import psycopg; from psycopg.types.json import Jsonb; from uuid import uuid4
    context = repository.load_evidence_vault_sv9_judgment_context(scan)
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn: conn.execute(f"INSERT INTO b3s_history.evidence_vault_sv9_judgment_candidates (id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id, operation_plan_id, schema_version, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, evaluation_bundle_fingerprint, assessment_fingerprint, score_fingerprint, complete_record_fingerprint, candidate_payload, authority, review_state, lifecycle_state, runtime_effect) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 'none', 'active', 'shadow_only')", (uuid4(), context["workspace_id"], context["brand_id"], context["scan_run_id"], scan, context["capture_id"], context["operation_plan_id"], *[candidate[key] for key in ("schema_version", "canonical_plan_fingerprint", "current_series_fingerprint", "candidate_series_fingerprint", "evaluation_bundle_fingerprint", "assessment_fingerprint", "score_fingerprint", "complete_record_fingerprint")], Jsonb(candidate)))
# fmt: on


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
# fmt: on


# fmt: off
@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_repository_fences_witnessed_candidate_append_and_invalid_readback(monkeypatch):
    repository, scan = _reset_repository(), "candidate-v2-base"; current = _operational(repository, scan)
    first, projection, evidence = _captured_candidate(monkeypatch, repository, scan, _series()); second, _, _ = _captured_candidate(monkeypatch, repository, scan, _series(prompt_version="v2")); stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(scan, first)
    assert inserted and stored["authoritative_relation_witness"]["authoritative_relations"] == projection["authoritative_relations"] and len(stored["evidence_bindings"]) == 1 and repository.get_evidence_vault_sv9_judgment_candidate(scan, canonical_plan_fingerprint=first["canonical_plan_fingerprint"]) == stored
    current = _operational(repository, "candidate-v2-next", current)
    with pytest.raises(history.EvidenceVaultSv9AuthoritativeRelationStaleWitnessError): repository.append_evidence_vault_sv9_judgment_candidate(scan, second)
    assert repository.get_evidence_vault_sv9_judgment_candidate(scan, canonical_plan_fingerprint=second["canonical_plan_fingerprint"]) is None
    repository = _reset_repository(); legacy_scan = "candidate-v1-legacy"; _operational(repository, legacy_scan); third, _, _ = _captured_candidate(monkeypatch, repository, legacy_scan, _series(prompt_version="v3")); legacy, _ = repository.append_evidence_vault_sv9_judgment_candidate(legacy_scan, _legacy(third))
    replayed_legacy = repository.get_evidence_vault_sv9_judgment_candidate(legacy_scan, canonical_plan_fingerprint=legacy["canonical_plan_fingerprint"])
    assert replayed_legacy["schema_version"].endswith("v1") and replayed_legacy["complete_record_fingerprint"] == legacy["complete_record_fingerprint"]
    repository = _reset_repository(); invalid_scan = "candidate-v2-invalid"; _operational(repository, invalid_scan); invalid, relations, evidence = _captured_candidate(monkeypatch, repository, invalid_scan, _series(prompt_version="v4")); relation = invalid["authoritative_relation_witness"]["authoritative_relations"][0]; relation["relation_fingerprint"] = "0" * 64 if relation["relation_fingerprint"] != "0" * 64 else "1" * 64; invalid["complete_record_fingerprint"] = jm.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v2", {key: row for key, row in invalid.items() if key != "complete_record_fingerprint"}); _insert(repository, invalid_scan, invalid)
    flow = _AuthorityFlow(); outcome = authority.run_evidence_vault_sv9_authority_evaluation(repository=repository, flow=flow, domain_or_url="example.com", source_scan_id=invalid_scan, current_evidence=evidence, authoritative_relations=relations["authoritative_relations"], current_series_contract=_series(prompt_version="v4"))
    assert outcome["status"] == "review_required" and outcome["reason_codes"] == ["invalid_authoritative_relation_witness"] and not flow.calls
# fmt: on
