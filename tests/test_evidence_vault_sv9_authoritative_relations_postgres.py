from __future__ import annotations

import os

import pytest

from src.history import repository as history

# fmt: off


class _SpyResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _SpyConnection:
    def __init__(self, source_rows, operation_rows):
        self.source_rows = source_rows
        self.operation_rows = operation_rows
        self.source_lookups = 0
        self.operation_lookups = 0

    def execute(self, query, params):
        if "evidence_vault_canonical_memory_packets" in query:
            self.source_lookups += 1
            return _SpyResult([self.source_rows[params[1]]])
        if "evidence_vault_operation_plans" in query:
            self.operation_lookups += 1
            return _SpyResult([self.operation_rows[params[1]]])
        raise AssertionError(f"unexpected query: {query}")


def _policy_authority_fixture(tile_count, source_count=1):
    registry_tiles = [
        {"tile_id": f"T{index}", "component_key": "component"}
        for index in range(tile_count)
    ]
    source_rows, operation_rows, source_packets = {}, {}, {}
    accepted = []
    for index in range(tile_count):
        source_index = index % source_count
        source_fingerprint = f"{source_index + 1:064x}"
        source_packet = source_packets.setdefault(
            source_fingerprint,
            {
                "manifest": {"brand_identity": "example.com"},
                "candidate_tiles": [],
                "candidate_packet_fingerprint": source_fingerprint,
            },
        )
        basis = [{"polarity": "supports"}]
        source_packet["candidate_tiles"].append(
            {
                "tile_id": f"T{index}",
                "candidate_state": "ok",
                "basis": basis,
                "coverage_refs": [],
                "unresolved_refs": [],
                "delta_kind": "baseline",
            }
        )
        operation_fingerprint = f"{source_index + 101:064x}"
        resolution = {
            "schema_version": "evidence-vault-operational-source-resolution-v1",
            "operation_plan_fingerprint": operation_fingerprint,
            "observation_hash": f"{source_index + 201:064x}",
            "result_fingerprint": f"{source_index + 301:064x}",
            "source_candidate_packet_fingerprint": source_fingerprint,
        }
        source_rows[source_fingerprint] = {
            "packet_kind": "operational_source_v2",
            "source": {
                "packet": source_packet,
                "reference_resolution": resolution,
            },
        }
        operation_rows[operation_fingerprint] = {
            "source_packet": source_packet,
        }
        accepted.append(
            {
                "tile_id": f"T{index}",
                "component_key": "component",
                "semantic_state": "ok",
                "basis": basis,
                "coverage_refs": [],
                "unresolved_refs": [],
                "source_delta_kind": "baseline",
                "authority_profile_id": history.SCANNER_SEMANTIC_PROFILE_ID,
                "authority_source": "policy",
                "decision_event_id": None,
                "authority_matrix_fingerprint": "e" * 64,
                "authority_decision_fingerprint": "f" * 64,
                "source_candidate_packet_fingerprint": source_fingerprint,
            }
        )
    memory = {
        "brand_identity": "example.com",
        "lifecycle_state": "active",
        "authority": True,
        "content": {"accepted_tiles": accepted},
    }
    context = {"brand_id": "brand-id", "canonical_domain": "example.com"}
    return registry_tiles, source_rows, operation_rows, source_packets, context, memory


def _patch_policy_authority_fixture(monkeypatch, fixture, counters):
    registry_tiles, source_rows, operation_rows, _source_packets, _context, _memory = fixture
    monkeypatch.setattr(history, "build_tile_contract_registry", lambda: {"tiles": registry_tiles})

    def source_record(row):
        counters["source_validations"] = counters.get("source_validations", 0) + 1
        return row["source"]

    def operation_record(row):
        counters["operation_validations"] = counters.get("operation_validations", 0) + 1
        return {"status": "completed", "result_payload": {"source_candidate_packet": row["source_packet"]}}

    monkeypatch.setattr(history, "_vault_operational_source_packet_record", source_record)
    monkeypatch.setattr(history, "_vault_operation_plan_record", operation_record)
    decision = {
        "authority_profile_id": history.SCANNER_SEMANTIC_PROFILE_ID,
        "authority_matrix_fingerprint": "e" * 64,
        "authority_decision_fingerprint": "f" * 64,
    }
    evaluator_calls = []
    validator_calls = []
    monkeypatch.setattr(
        history,
        "evaluate_scanner_semantic_authority",
        lambda **kwargs: evaluator_calls.append(kwargs) or decision,
    )
    monkeypatch.setattr(
        history,
        "validate_authority_decision",
        lambda *_args, **kwargs: validator_calls.append((_args, kwargs)),
    )
    return _SpyConnection(source_rows, operation_rows), evaluator_calls, validator_calls


def test_authoritative_relation_validation_deduplicates_shared_policy_source(monkeypatch) -> None:
    fixture = _policy_authority_fixture(79)
    counters = {}
    connection, evaluator_calls, validator_calls = _patch_policy_authority_fixture(monkeypatch, fixture, counters)
    _registry, _source_rows, _operation_rows, _packets, context, memory = fixture

    result = history._sv9_authoritative_relation_accepted(connection, context, memory)

    assert len(result) == 79
    assert connection.source_lookups == 1
    assert connection.operation_lookups == 1
    assert counters == {"source_validations": 1, "operation_validations": 1}
    assert len(evaluator_calls) == len(validator_calls) == 79


def test_authoritative_relation_validation_deduplicates_only_identical_sources(monkeypatch) -> None:
    fixture = _policy_authority_fixture(6, source_count=3)
    counters = {}
    connection, evaluator_calls, validator_calls = _patch_policy_authority_fixture(monkeypatch, fixture, counters)
    _registry, _source_rows, _operation_rows, _packets, context, memory = fixture

    result = history._sv9_authoritative_relation_accepted(connection, context, memory)

    assert len(result) == 6
    assert connection.source_lookups == connection.operation_lookups == 3
    assert counters == {"source_validations": 3, "operation_validations": 3}
    assert len(evaluator_calls) == len(validator_calls) == 6

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
