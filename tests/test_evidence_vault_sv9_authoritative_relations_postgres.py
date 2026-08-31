from __future__ import annotations

import os

import pytest

# fmt: off

@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_projection_replays_human_accepted_basis_without_writing() -> None:
    import psycopg
    from src.services.evidence_vault_incremental_executor import execute_vault_operation_plan
    from src.services.evidence_vault_sv9_authoritative_relations import project_evidence_vault_sv9_authoritative_relations
    from tests.test_evidence_vault_operation_execution_postgres import ExecutorLLM, _persist_baseline, _reset_repository

    repository = _reset_repository(); _persist_baseline(repository, "relations-scan")
    execute_vault_operation_plan(repository=repository, source_scan_id="relations-scan", worker_id="relations-worker", llm=ExecutorLLM())
    operation = repository.get_capture_operation_plan("relations-scan")
    result = operation["result_payload"]
    repository.review_and_adopt_evidence_vault_operational_source("example.com", source_candidate_packet_fingerprint=result["source_candidate_packet_fingerprint"], decisions=[{"relation_id": result["basis_relations"][0]["relation_id"], "decision": "accept", "rationale": "Direct literal support."}], reviewer_id="relations-reviewer", reviewed_at="2026-08-07T13:00:00+02:00", created_at="2026-08-07T11:00:00Z")
    def counts():
        with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
            return conn.execute("SELECT count(*) FROM b3s_history.evidence_vault_canonical_memory_promotion_events").fetchone()[0]
    before = counts()
    first = project_evidence_vault_sv9_authoritative_relations(repository=repository, source_scan_id="relations-scan")
    second = project_evidence_vault_sv9_authoritative_relations(repository=repository, source_scan_id="relations-scan")
    assert first == second and first["status"] == "available" and first["authoritative_relations"]
    assert {row["disposition"] for row in first["authoritative_relations"]} == {"relevant"}
    assert counts() == before
# fmt: on
