from copy import deepcopy
import os
from pathlib import Path
import pytest
from src.history import repository as history
from src.services import evidence_vault_sv9_authority_evaluation as authority
from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_sv9_authoritative_relations as authoritative_relations
from src.services.evidence_vault_incremental_executor import execute_vault_operation_plan
from src.services.evidence_vault_sv9_authoritative_relations import (
    project_evidence_vault_sv9_authoritative_relations,
    project_evidence_vault_sv9_evaluation_input,
)
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
_V3 = "evidence-vault-sv9-judgment-candidate-v3"
_PRIOR_ID = "00000000-0000-0000-0000-000000000009"


def _seal(candidate):
    candidate["complete_record_fingerprint"] = jm.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v3", {key: value for key, value in candidate.items() if key != "complete_record_fingerprint"})
    return candidate


def _v3(rows, sentinels, witness, *, prior=_PRIOR_ID, kind="apply"):
    series = rows[0]["series_fingerprint"]
    plan = {"schema_version": "evidence-vault-sv9-tile-rescan-plan-v1", "kind": kind, "prior_candidate_id": prior, "current_series_contract": rows[0]["series_contract"]}
    assessment = ie._assessment({"current_series_fingerprint": series, "prior_judgments": [], "prior_component_sentinels": []}, set(), {row["tile_id"]: row for row in rows}, {row["component_key"]: row for row in sentinels})[0]
    candidate = {"schema_version": _V3, "plan": plan, "canonical_plan_fingerprint": jm.canonical_fingerprint("evidence-vault-sv9-tile-rescan-plan-fingerprint-v1", plan), "current_series_fingerprint": series, "candidate_series_fingerprint": ip._candidate(series, [series]), "component_evaluations": [], "evidence_bindings": [], "candidate_tile_judgments": rows, "candidate_component_sentinels": sentinels, "assessment": assessment, "telemetry": dict.fromkeys(("call_count", "calls_avoided", "reused_tile_count", "evaluated_tile_count"), 0), "assessment_fingerprint": assessment["assessment_fingerprint"], "score_fingerprint": assessment["score_fingerprint"], "authoritative_relation_witness": witness}
    candidate["evaluation_bundle_fingerprint"] = jm.canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": candidate["canonical_plan_fingerprint"], "evaluations": []})
    candidate["tile_rescan"] = {"kind": kind, "rule_version": "evidence-vault-tile-rescan-rule-v1", "ledger_policy_version": "evidence-vault-evidence-ledger-v1", "prior_candidate_id": prior, "ledger_rows_fingerprint": "e" * 64, "judgments_fingerprint": jm.canonical_fingerprint("evidence-vault-sv9-tile-rescan-judgments-fingerprint-v1", {"candidate_tile_judgments": rows, "candidate_component_sentinels": sentinels}), "decisions": [{"tile_id": tile, "decision": "keep_lit", "reason_codes": ["same_quote"]} for tile, _component in ip._REGISTRY], "change_signal": {"lit_tiles": len(rows)}, "guard": {"within_tolerance": True}}
    return _seal(candidate)


def test_tile_rescan_candidate_envelope_is_exact_and_bound_to_its_selected_vector():
    from tests.test_evidence_vault_sv9_authoritative_relations import _facts, _project
    plan = ip.build_incremental_plan([], [], _series()); result = ie.execute_incremental_evaluation(plan, _packets(plan), _Flow())
    rows, sentinels = result["candidate_tile_judgments"], result["candidate_component_sentinels"]
    witness = authoritative_relations.build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id="scan-1", projection=_project(_facts())[0])
    candidate = _v3(rows, sentinels, witness); rescan = candidate["tile_rescan"]; decisions = rescan["decisions"]
    assert history._sv9_judgment_candidate_envelope(candidate) == candidate
    held = _seal(candidate | {"tile_rescan": rescan | {"decisions": [decisions[0] | {"decision": "turn_off_proven", "reason_codes": ["held_without_core_verdict"]}, *decisions[1:]]}})
    assert history._sv9_judgment_candidate_envelope(held) == held
    tampered = [
        *(candidate | {"tile_rescan": rescan | {"decisions": [decisions[0] | {"decision": decision}, *decisions[1:]]}} for decision in ("new_quote", "same_quote", "turn_off")),
        {key: value for key, value in candidate.items() if key != "tile_rescan"}, candidate | {"schema_version": "evidence-vault-sv9-judgment-candidate-v2"}, candidate | {"extra": True}, candidate | {"plan": candidate["plan"] | {"tile_workset": []}},
        candidate | {"tile_rescan": {key: value for key, value in rescan.items() if key != "guard"}}, candidate | {"tile_rescan": rescan | {"note": ""}}, candidate | {"tile_rescan": rescan | {"kind": "revert"}}, _v3(rows, sentinels, witness, kind="undo"), _v3(rows, sentinels, witness, prior="prior"),
        candidate | {"tile_rescan": rescan | {"decisions": decisions[1:]}}, candidate | {"tile_rescan": rescan | {"decisions": [decisions[1], decisions[0], *decisions[2:]]}}, candidate | {"tile_rescan": rescan | {"decisions": [decisions[0] | {"note": ""}, *decisions[1:]]}},
        candidate | {"tile_rescan": rescan | {"guard": {"url": "https://example.com"}}}, candidate | {"tile_rescan": rescan | {"change_signal": {"text": "quoted proof"}}},
        candidate | {"tile_rescan": rescan | {"judgments_fingerprint": "0" * 64}}, candidate | {"candidate_series_fingerprint": "0" * 64}, candidate | {"telemetry": candidate["telemetry"] | {"call_count": 1}},
        candidate | {"assessment": candidate["assessment"] | {"sv9_score": candidate["assessment"]["sv9_score"] + 1}},
    ]
    for value in tampered:
        with pytest.raises(history.EvidenceVaultSv9JudgmentCandidateError): history._sv9_judgment_candidate_envelope(_seal(deepcopy(value)))


class _AuthorityFlow:
    def __init__(self): self.calls = []
    def evaluate_component(self, request):
        self.calls.append(request); rows = [{"tile_id": row["tile_id"], "assessment_state": "ok" if row["evidence"] else "sin_evidencia", "supporting_evidence": [{key: evidence[key] for key in ("evidence_ref", "evidence_fingerprint")} for evidence in row["evidence"]]} for row in request["requested_tiles"]]
        return ie.ComponentEvaluationOutcome.success(ie.build_component_evaluation(component_key=request["component_key"], series_fingerprint=request["current_series_fingerprint"], request_fingerprint=request["canonical_request_fingerprint"], status="evaluated", tile_results=rows))

def _operational(repository, scan, previous=()):
    row = _row() | {"ref": f"raw_inputs.{len(previous)}.chunk.0", "content": _row()["content"] if not previous else f"Safer {scan}. {_row()['content']}"}; current = [*previous, row]; memory = repository.get_evidence_vault_operational_memory("example.com"); plan = build_vault_scan_plan(brand_identity="example.com", subject_url="https://example.com", mode="incremental_refresh" if memory else "baseline", current_evidence_records=current, previous_capture_evidence_records=list(previous) or current, known_evidence_records=current, canonical_memory_version=memory["canonical_memory_version"] if memory else None); _persist_baseline(repository, scan, current, plan)
    execute_vault_operation_plan(repository=repository, source_scan_id=scan, worker_id="candidate-worker", llm=ExecutorLLM()); operation = repository.get_capture_operation_plan(scan)["result_payload"]
    repository.review_and_adopt_evidence_vault_operational_source("example.com", source_candidate_packet_fingerprint=operation["source_candidate_packet_fingerprint"], decisions=[{"relation_id": relation["relation_id"], "decision": "accept", "rationale": "Direct literal support."} for relation in operation["basis_relations"]], reviewer_id="candidate-reviewer", reviewed_at="2026-08-07T13:00:00+02:00", created_at="2026-08-07T11:00:00Z")
    return current

def _captured_candidate(monkeypatch, repository, scan, series):
    evaluation_input = project_evidence_vault_sv9_evaluation_input(repository=repository, source_scan_id=scan)
    context, records = authority._source(repository, scan, evaluation_input, "b3s", "example.com")
    projection = project_evidence_vault_sv9_authoritative_relations(repository=repository, source_scan_id=scan)
    evidence = [
        {key: row[key] for key in ("evidence_ref", "evidence_fingerprint")}
        for row in evaluation_input["current_evidence"]
    ]
    captured = {}
    def append(_scan, candidate, **_kwargs): captured["candidate"] = deepcopy(candidate); raise RuntimeError
    # This is a historical full-capture candidate fixture.  Operational review
    # remains a non-authoritative transport witness; the test must not resolve
    # its advisory refs as SV9-authoritative evidence.
    with monkeypatch.context() as patch:
        patch.setattr(repository, "append_evidence_vault_sv9_judgment_candidate", append)
        with pytest.raises(RuntimeError):
            authority._evaluate_first_baseline(
                repository,
                _AuthorityFlow(),
                scan,
                "b3s",
                evaluation_input,
                context,
                records,
                series,
                None,
                None,
            )
    return captured["candidate"], projection, evidence


def _seed_accepted_sv9_authority(repository, scan, series, flow=None):
    """Seed a replay-valid historical accepted SV9 authority for continuity tests."""
    if repository.get_evidence_vault_sv9_judgment_authority("example.com") is not None:
        raise AssertionError("historical authority fixture must start absent")
    evaluation_input = project_evidence_vault_sv9_evaluation_input(
        repository=repository,
        source_scan_id=scan,
    )
    context, records = authority._source(repository, scan, evaluation_input, "b3s", "example.com")
    captured = {}

    class _CapturedCandidate(Exception):
        pass

    def append(_scan, candidate, **kwargs):
        captured["candidate"] = deepcopy(candidate)
        captured["shared_analysis_payload"] = deepcopy(kwargs.get("shared_analysis_payload"))
        raise _CapturedCandidate

    original_append = repository.append_evidence_vault_sv9_judgment_candidate
    repository.append_evidence_vault_sv9_judgment_candidate = append
    # Historical authority is seeded as an accepted record.  Legacy callers
    # intentionally omit checkpoints because this helper represents a
    # proofless historical record, not current runtime production.
    original_append_checkpoint = repository.append_evidence_vault_sv9_evaluation_checkpoint
    repository.append_evidence_vault_sv9_evaluation_checkpoint = lambda _scan, checkpoint, **_kwargs: (checkpoint, False)
    try:
        authority._evaluate_first_baseline(
            repository,
            flow or _AuthorityFlow(),
            scan,
            "b3s",
            evaluation_input,
            context,
            records,
            series,
            None,
            None,
        )
    except _CapturedCandidate:
        pass
    finally:
        repository.append_evidence_vault_sv9_judgment_candidate = original_append
        repository.append_evidence_vault_sv9_evaluation_checkpoint = original_append_checkpoint
    candidate = captured["candidate"]
    facts = repository.load_evidence_vault_sv9_authoritative_relation_facts(scan)
    operation = repository.get_capture_operation_plan(scan)["result_payload"]
    basis = operation["basis_relations"][0]
    projection = authoritative_relations._project(
        facts["source"],
        facts["evidence"],
        {
            "witness": evaluation_input["operational_witness"],
            "accepted": [
                {
                    "tile_id": basis["tile_id"],
                    "component_key": "mission",
                    "assessment_state": "ok",
                    "authority_state": "accepted",
                    "review_state": "resolved",
                    "lifecycle_state": "active",
                    "basis": [
                        {
                            key: basis[key]
                            for key in (
                                "relation_id",
                                "evidence_id",
                                "source_identity_id",
                                "polarity",
                            )
                        }
                    ],
                }
            ],
        },
    )
    candidate["authoritative_relation_witness"] = (
        authoritative_relations.build_evidence_vault_sv9_authoritative_relation_witness(
            source_scan_id=scan,
            projection=projection,
        )
    )
    candidate["complete_record_fingerprint"] = jm.canonical_fingerprint(
        "evidence-vault-sv9-judgment-candidate-record-v2",
        {
            key: value
            for key, value in candidate.items()
            if key != "complete_record_fingerprint"
        },
    )
    candidate_id = _insert(
        repository,
        scan,
        candidate,
        shared_analysis_payload=captured["shared_analysis_payload"],
    )
    stored = repository.get_evidence_vault_sv9_judgment_candidate(
        scan,
        canonical_plan_fingerprint=candidate["canonical_plan_fingerprint"],
    )
    assert stored["id"] == candidate_id
    request = authority_event.build_evidence_vault_sv9_authority_request(
        action="adopt_candidate",
        candidate_id=candidate_id,
        expected_predecessor_event_fingerprint=None,
        delta_fingerprint=None,
        source_scan_id=scan,
    )
    with repository._connect() as connection:
        source_context = history._sv9_judgment_context(connection, scan, "b3s", True)
        candidate_row = connection.execute(
            "SELECT * FROM b3s_history.evidence_vault_sv9_judgment_candidates WHERE id = %s",
            (candidate_id,),
        ).fetchone()
        event = history._sv9_authority_event(
            source_context,
            None,
            request,
            authority_event.authority_application_idempotency_fingerprint(request),
            stored,
            candidate_row,
            None,
        )
        history._append_sv9_judgment_authority_event(connection, event)
    accepted = repository.get_evidence_vault_sv9_judgment_authority("example.com")
    assert accepted["accepted_candidate"]["id"] == candidate_id
    return accepted

def _legacy(candidate):
    value = {key: row for key, row in candidate.items() if key != "authoritative_relation_witness"}; value["schema_version"] = "evidence-vault-sv9-judgment-candidate-v1"; value["complete_record_fingerprint"] = jm.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v1", {key: row for key, row in value.items() if key != "complete_record_fingerprint"})
    return value

def _insert(repository, scan, candidate, *, shared_analysis_payload=None):
    import psycopg; from psycopg.types.json import Jsonb; from uuid import uuid4
    context = repository.load_evidence_vault_sv9_judgment_context(scan)
    candidate_id = uuid4()
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(f"INSERT INTO b3s_history.evidence_vault_sv9_judgment_candidates (id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id, operation_plan_id, schema_version, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, evaluation_bundle_fingerprint, assessment_fingerprint, score_fingerprint, complete_record_fingerprint, candidate_payload, authority, review_state, lifecycle_state, runtime_effect) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 'none', 'active', 'shadow_only')", (candidate_id, context["workspace_id"], context["brand_id"], context["scan_run_id"], scan, context["capture_id"], context["operation_plan_id"], *[candidate[key] for key in ("schema_version", "canonical_plan_fingerprint", "current_series_fingerprint", "candidate_series_fingerprint", "evaluation_bundle_fingerprint", "assessment_fingerprint", "score_fingerprint", "complete_record_fingerprint")], Jsonb(candidate)))
        for evidence_record_id, evidence_fingerprint in {
            row["evidence_record_id"]: row["evidence_fingerprint"]
            for row in candidate["evidence_bindings"]
        }.items():
            conn.execute(
                "INSERT INTO b3s_history.evidence_vault_sv9_judgment_evidence_bindings (candidate_id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id, evidence_record_id, evidence_fingerprint) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (candidate_id, context["workspace_id"], context["brand_id"], context["scan_run_id"], context["capture_id"], context["operation_plan_id"], evidence_record_id, evidence_fingerprint),
            )
        shared_analysis = history._prepare_sv9_shared_analysis_snapshot(
            shared_analysis_payload,
            candidate=candidate,
            context=context,
        )
        if shared_analysis is not None:
            conn.execute(
                "INSERT INTO b3s_history.evidence_vault_sv9_shared_analysis_snapshots (candidate_id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id, operation_plan_id, schema_version, candidate_complete_record_fingerprint, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, evaluation_bundle_fingerprint, assessment_fingerprint, score_fingerprint, payload_sha256, payload, payload_raw) VALUES (%s, %s, %s, %s, %s, %s, %s, 'evidence-vault-sv9-shared-analysis-snapshot-v1', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (candidate_id, context["workspace_id"], context["brand_id"], context["scan_run_id"], scan, context["capture_id"], context["operation_plan_id"], candidate["complete_record_fingerprint"], candidate["canonical_plan_fingerprint"], candidate["current_series_fingerprint"], candidate["candidate_series_fingerprint"], candidate["evaluation_bundle_fingerprint"], candidate["assessment_fingerprint"], candidate["score_fingerprint"], shared_analysis["payload_sha256"], Jsonb(shared_analysis["payload"]), shared_analysis["payload_raw"]),
            )
    return str(candidate_id)
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


def test_empty_witness_migration_preserves_enforcement_and_binds_all_plan_origins():
    sql = Path("src/history/migrations/035_evidence_vault_sv9_empty_relation_witness.sql").read_text()
    assert sql.index("ADD CONSTRAINT") < sql.index("VALIDATE CONSTRAINT") < sql.index("DROP CONSTRAINT") < sql.index("RENAME CONSTRAINT")
    assert ") IS TRUE) NOT VALID;" in sql
    assert "strict $[*] ? (@.type() != \"object\")" in sql
    assert "candidate_payload->'plan'->'delta_projections' <> '[]'::jsonb" in sql
    for key in ("capture_id", "capture_fingerprint", "operation_id", "operation_fingerprint"):
        assert f"!= ${key}" in sql
    assert "= capture_id::text" in sql and "= operation_plan_id::text" in sql


def test_tile_rescan_migration_admits_v3_candidates_without_new_relations_or_grants():
    sql = Path("src/history/migrations/040_evidence_vault_sv9_judgment_candidate_tile_rescan.sql").read_text()
    assert sql.index("ADD CONSTRAINT evidence_vault_sv9_judgment_candidate_tile_rescan_check") < sql.index("VALIDATE CONSTRAINT evidence_vault_sv9_judgment_candidate_tile_rescan_check") < sql.index("DROP CONSTRAINT evidence_vault_sv9_judgment_candidate_payload_witness_check") < sql.index("RENAME CONSTRAINT")
    assert sql.count(") NOT VALID;") == 2 and "(candidate_payload ? 'tile_rescan') = (schema_version = 'evidence-vault-sv9-judgment-candidate-v3')" in sql
    assert all(value not in sql for value in ("review_resolution", "GRANT", "CREATE TABLE", "CREATE TRIGGER"))


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
    for value in (None, [[]], [[origins]], [origins, []], [origins, {}]):
        rejected(200, v2_source, v2_scan, malformed(empty, ("plan", "delta_projections"), value))
    for origin, fingerprint in (("capture_origin", "capture_fingerprint"), ("operation_origin", "operation_fingerprint")):
        for value in (None, {}, [], [origins[origin]], origins[origin] | {"extra": True}):
            rejected(201, v2_source, v2_scan, malformed(empty, ("authoritative_relation_witness", origin), value))
        rejected(202, v2_source, v2_scan, malformed(empty, ("authoritative_relation_witness", origin, fingerprint), "f" * 64))
        rejected(203, v2_source, v2_scan, malformed(empty, ("plan", "delta_projections", 1, origin, fingerprint), "f" * 64))
    insert(204, v2_source, v2_scan, empty)
    empty_projection = deepcopy(empty)
    empty_projection["canonical_plan_fingerprint"] = "b" * 64
    empty_projection["plan"]["delta_projections"] = []
    insert(205, v2_source, v2_scan, empty_projection)
    rescan = {"kind": "apply", "rule_version": "rule-v1", "ledger_policy_version": "evidence-vault-evidence-ledger-v1", "prior_candidate_id": str(uuid4()), "ledger_rows_fingerprint": "e" * 64, "judgments_fingerprint": "f" * 64, "decisions": [{"tile_id": tile, "decision": "keep_lit", "reason_codes": []} for tile, _component in ip._REGISTRY], "change_signal": {}, "guard": {}}
    v3 = _seal(deepcopy(v2) | {"schema_version": _V3, "canonical_plan_fingerprint": "c" * 64, "plan": {"kind": "apply"}, "tile_rescan": rescan})
    insert(206, v2_source, v2_scan, v3)
    insert(207, v2_source, v2_scan, _seal(deepcopy(empty) | {"schema_version": _V3, "canonical_plan_fingerprint": "d" * 64, "plan": {}, "tile_rescan": rescan}))
    v3_shapes = [(("kind",), "undo"), (("prior_candidate_id",), "prior"), (("judgments_fingerprint",), number), (("decisions",), rescan["decisions"][1:]), (("decisions", 0), rescan["decisions"][0] | {"note": ""}), (("decisions", 0, "reason_codes"), "same_quote"), (("guard",), []), (("note",), {})]
    for index, payload in enumerate([v2 | {"tile_rescan": rescan}, {key: value for key, value in v3.items() if key != "tile_rescan"}, *(malformed(v3, ("tile_rescan", *path), value) for path, value in v3_shapes)], 300):
        rejected(index, v2_source, v2_scan, payload | {"canonical_plan_fingerprint": f"{index:064x}"})
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
    repository = _reset_repository(); invalid_scan = "candidate-v2-invalid"; _operational(repository, invalid_scan); invalid, _, _ = _captured_candidate(monkeypatch, repository, invalid_scan, _series(prompt_version="v4")); witness = invalid["authoritative_relation_witness"]; witness["witness_fingerprint"] = "0" * 64 if witness["witness_fingerprint"] != "0" * 64 else "1" * 64; invalid["complete_record_fingerprint"] = jm.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v2", {key: row for key, row in invalid.items() if key != "complete_record_fingerprint"}); _insert(repository, invalid_scan, invalid)
    with pytest.raises(history.EvidenceVaultSv9AuthoritativeRelationWitnessError):
        repository.get_evidence_vault_sv9_judgment_candidate(
            invalid_scan,
            canonical_plan_fingerprint=invalid["canonical_plan_fingerprint"],
        )


def _rescan_inputs(monkeypatch, repository, scan, authority):
    from src.services.evidence_vault_sv9_evaluation_checkpoint import build_evidence_vault_sv9_evaluation_checkpoint
    from tests.test_evidence_vault_sv9_evaluation_checkpoint import _progress, _sha
    accepted, head = authority["accepted_candidate"], authority["current_head"]
    snapshot = {"state": "accepted_authority", "accepted_candidate_id": accepted["id"], "active_event_id": head["event_id"], "current_head_event_fingerprint": head["event_fingerprint"], "candidate_complete_record_fingerprint": accepted["complete_record_fingerprint"], **{key: accepted[key] for key in ("canonical_plan_fingerprint", "current_series_fingerprint")}}
    value, lit = project_evidence_vault_sv9_evaluation_input(repository=repository, source_scan_id=scan), {}; assert value["status"] == "available"
    for tile in ("M1", "M2"):
        progress = _progress(value, f"{scan}-{tile}", tile_id=tile); [lit[tile]] = progress["evaluated_tile_judgments"]
        repository.append_evidence_vault_sv9_evaluation_checkpoint(scan, build_evidence_vault_sv9_evaluation_checkpoint(evaluation_input=value, prior_authority_snapshot=snapshot, current_series_fingerprint=lit[tile]["series_fingerprint"], canonical_plan_fingerprint=_sha("checkpoint-plan"), candidate_series_fingerprint=_sha("checkpoint-series"), healthy_tile_ids=[tile], **progress))
    with monkeypatch.context() as patch:
        patch.setattr(repository, "append_evidence_vault_sv9_evaluation_checkpoint", lambda _scan, value, **_kwargs: (value, False))
        witness = _captured_candidate(monkeypatch, repository, scan, _series())[0]["authoritative_relation_witness"]
    return lit, witness


def _select(rows, selected):
    return [selected if row["tile_id"] == selected["tile_id"] else row for row in rows]


def _adopt(repository, scan, candidate_id, predecessor):
    request = authority_event.build_evidence_vault_sv9_authority_request(action="adopt_candidate", candidate_id=candidate_id, expected_predecessor_event_fingerprint=predecessor, delta_fingerprint=None, source_scan_id=scan)
    return repository.adopt_evidence_vault_sv9_judgment_candidate(scan, candidate_id, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash=authority_event.authority_application_idempotency_fingerprint(request))[0]


@pytest.mark.skipif(not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1", reason="requires disposable PostgreSQL")
def test_tile_rescan_candidate_selects_prior_or_checkpoint_rows_and_serves_as_authority(monkeypatch):
    from uuid import uuid4
    from tests.test_evidence_vault_evidence_ledger_postgres import _CURRENT, _PRIOR, _adopt_captured_candidate
    from tests.test_evidence_vault_sv9_review_resolution_runtime import _request
    repository = _reset_repository(); prior = _operational(repository, _PRIOR); authority = _seed_accepted_sv9_authority(repository, _PRIOR, _series()); accepted = authority["accepted_candidate"]; current = _operational(repository, _CURRENT, prior)
    lit, witness = _rescan_inputs(monkeypatch, repository, _CURRENT, authority); sentinels, rows = accepted["candidate_component_sentinels"], _select(accepted["candidate_tile_judgments"], lit["M2"])
    fabricated = _select(rows, jm.build_tile_judgment(**{key: value for key, value in lit["M2"].items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint"}} | {"assessment_state": "no"}))
    candidate = _v3(rows, sentinels, witness, prior=accepted["id"])
    wrong_score = _seal(deepcopy(candidate) | {"assessment": candidate["assessment"] | {"sv9_score": candidate["assessment"]["sv9_score"] - 1}})
    [relation] = witness["authoritative_relations"]
    assert relation["tile_id"] == "M1" and {key: relation[key] for key in ("evidence_ref", "evidence_fingerprint")} not in lit["M1"]["supporting_evidence"]
    for invalid in (_v3(fabricated, sentinels, witness, prior=accepted["id"]), wrong_score, _v3(rows, sentinels, witness, prior=str(uuid4())), _v3(_select(rows, lit["M1"]), sentinels, witness, prior=accepted["id"])):
        with pytest.raises(history.EvidenceVaultSv9JudgmentCandidateError): repository.append_evidence_vault_sv9_judgment_candidate(_CURRENT, invalid)
    stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(_CURRENT, candidate)
    assert inserted and {key: stored[key] for key in candidate} == candidate
    assert repository.get_evidence_vault_sv9_judgment_candidate(_CURRENT, canonical_plan_fingerprint=candidate["canonical_plan_fingerprint"]) == stored
    with pytest.raises(history.EvidenceVaultSv9JudgmentCandidateError) as refused:
        repository.resolve_evidence_vault_sv9_judgment_review(_request(repository.load_evidence_vault_sv9_judgment_context(_CURRENT), stored, decision="reject"))
    assert "candidate_schema_version" in str(refused.value.__cause__)
    adopted = _adopt(repository, _CURRENT, stored["id"], authority["current_head"]["event_fingerprint"])
    assert adopted["current_head"]["event_type"] == "supersede" and adopted["accepted_candidate"] == stored
    assert repository.get_evidence_vault_sv9_judgment_authority("example.com")["accepted_candidate"] == stored
    later = _operational(repository, "ledger-later", current); lit, witness = _rescan_inputs(monkeypatch, repository, "ledger-later", adopted)
    chained = repository.append_evidence_vault_sv9_judgment_candidate("ledger-later", _v3(_select(rows, lit["M2"]), sentinels, witness, prior=stored["id"]))[0]
    chained_authority = _adopt(repository, "ledger-later", chained["id"], adopted["current_head"]["event_fingerprint"])
    _operational(repository, "ledger-last", later); last, final = _adopt_captured_candidate(monkeypatch, repository, "ledger-last", chained_authority["current_head"]["event_fingerprint"])
    assert chained_authority["accepted_candidate"] == chained and final["accepted_candidate"]["id"] == last["id"] and final["current_head"]["sequence"] == 4
    assert [row["tile_id"] for row in last["authoritative_relation_witness"]["authoritative_relations"]] == [row["tile_id"] for row in accepted["authoritative_relation_witness"]["authoritative_relations"]] != []


@pytest.mark.skipif(not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1", reason="requires disposable PostgreSQL")
def test_tile_rescan_apply_builds_a_v3_the_repository_appends_and_adopts(monkeypatch):
    from src.services.evidence_vault_tile_rescan_apply import build_tile_rescan_candidate
    from tests.test_evidence_vault_evidence_ledger_postgres import _CURRENT, _PRIOR
    from tests.test_evidence_vault_tile_rescan_apply import _ledger
    repository = _reset_repository(); prior = _operational(repository, _PRIOR); authority = _seed_accepted_sv9_authority(repository, _PRIOR, _series()); accepted = authority["accepted_candidate"]; _operational(repository, _CURRENT, prior)
    lit, witness = _rescan_inputs(monkeypatch, repository, _CURRENT, authority)
    outcome = {"status": "review_required", "reason_codes": ["provider_failure"], "accepted_authority": {"accepted_candidate_id": accepted["id"]}}
    result = build_tile_rescan_candidate(outcome=outcome, accepted_candidate=accepted, ledger_rows=_ledger(accepted), current_judgments=list(lit.values()), witness=witness)
    rows = {row["tile_id"]: row for row in result["candidate"]["candidate_tile_judgments"]}
    # Both tiles quote anew; M1's new quote drops its authoritative relation pair, so M1 keeps the accepted row.
    assert rows["M1"] == next(row for row in accepted["candidate_tile_judgments"] if row["tile_id"] == "M1") and rows["M2"] == lit["M2"]
    stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(_CURRENT, result["candidate"])
    assert inserted and {key: stored[key] for key in result["candidate"]} == result["candidate"]
    assert repository.get_evidence_vault_sv9_judgment_candidate(_CURRENT, canonical_plan_fingerprint=stored["canonical_plan_fingerprint"]) == stored
    assert _adopt(repository, _CURRENT, stored["id"], authority["current_head"]["event_fingerprint"])["accepted_candidate"] == stored


@pytest.mark.skipif(not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1", reason="requires disposable PostgreSQL")
def test_tile_rescan_migration_validates_and_reads_back_existing_v1_and_v2_candidates(monkeypatch):
    import psycopg
    files, scan = history._migration_files(), "candidate-before-040"
    assert files[-1][0] == "040_evidence_vault_sv9_judgment_candidate_tile_rescan.sql"
    with monkeypatch.context() as patch:
        patch.setattr(history, "_migration_files", lambda: files[:-1])
        repository = _reset_repository(); _operational(repository, scan)
        witnessed, legacy = (_captured_candidate(monkeypatch, repository, scan, _series(prompt_version=version))[0] for version in ("v1", "v2"))
        stored = [repository.append_evidence_vault_sv9_judgment_candidate(scan, value)[0] for value in (witnessed, _legacy(legacy))]
    assert [row["schema_version"][-2:] for row in stored] == ["v2", "v1"] and repository.migrate() == [files[-1][0]]
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        assert conn.execute("SELECT bool_and(convalidated), count(*) FILTER (WHERE conname IN ('evidence_vault_sv9_judgment_candidates_schema_version_check', 'evidence_vault_sv9_judgment_candidate_payload_witness_check')) FROM pg_constraint WHERE conrelid = 'b3s_history.evidence_vault_sv9_judgment_candidates'::regclass AND contype = 'c'").fetchone() == (True, 2)
    assert [repository.get_evidence_vault_sv9_judgment_candidate(scan, canonical_plan_fingerprint=row["canonical_plan_fingerprint"]) for row in stored] == stored
# fmt: on
