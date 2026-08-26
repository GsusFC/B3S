from copy import deepcopy
from pathlib import Path
import pytest
from src.history import repository as history
from src.sv9 import incremental_evaluation as ie
from src.sv9 import incremental_planner as ip
from src.sv9 import judgment_memory as jm
from tests.test_sv9_incremental_evaluation import _Flow, _packets, _prior
from tests.test_sv9_judgment_memory import _series


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
