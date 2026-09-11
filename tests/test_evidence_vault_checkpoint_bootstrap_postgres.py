import hashlib
import json
import os

import psycopg
import pytest

from src.services.evidence_vault_incremental_executor import execute_vault_operation_plan
from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan
from src.services.evidence_vault_sv9_authoritative_relations import project_evidence_vault_sv9_evaluation_input
from src.services.evidence_vault_sv9_evaluation_checkpoint import build_evidence_vault_sv9_evaluation_checkpoint
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import judgment_memory as memory
from tests.test_evidence_vault_operation_execution_postgres import _persist_baseline, _reset_repository, _row
from tests.test_sv9_judgment_memory import _series


pytestmark = pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)


class ExplicitCompletionLLM:
    api_key = "test"

    def _call_json(self, system, user, **kwargs):
        if kwargs["schema_name"] == "sv9_flow_evidence_labeling":
            payload = json.loads(user)
            return {
                "labels": [
                    {
                        "ref": row["ref"],
                        "relevant_blocks": ["mission"],
                        "stance": "supports",
                        "identity_match": "domain",
                        "specificity": "explicit",
                    }
                    for row in payload["records"]
                ]
            }
        payload = json.loads(user.split(":\n", 1)[1])
        first = next(
            row["evidence_fingerprint"]
            for row in payload
            if row["ref"] == "raw_inputs.0.chunk.0"
        )
        return {
            "relations": [
                {
                    "evidence_fingerprint": first,
                    "tile_id": "M1",
                    "polarity": "supports",
                    "literal_quote": "We help teams ship better products.",
                    "rationale": "Direct literal support.",
                }
            ],
            "analysis": [
                {
                    "evidence_fingerprint": row["evidence_fingerprint"],
                    "decision": (
                        "supported"
                        if row["ref"] == "raw_inputs.0.chunk.0"
                        else "analyzed_without_sufficient_support"
                        if row["ref"] == "raw_inputs.1.chunk.0"
                        else "inconclusive"
                    ),
                }
                for row in payload
            ],
        }


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _prepare(repository, scan):
    rows = [
        _row(),
        {**_row(), "ref": "raw_inputs.1.chunk.0", "url": "https://example.com/about", "content": "General company information without mission support."},
    ]
    plan = build_vault_scan_plan(brand_identity="example.com", subject_url="https://example.com", mode="baseline", current_evidence_records=rows)
    _persist_baseline(repository, scan, rows, plan)
    execute_vault_operation_plan(repository=repository, source_scan_id=scan, worker_id="checkpoint-v4-worker", llm=ExplicitCompletionLLM())
    result = repository.get_capture_operation_plan(scan)["result_payload"]
    repository.review_and_adopt_evidence_vault_operational_source(
        "example.com",
        source_candidate_packet_fingerprint=result["source_candidate_packet_fingerprint"],
        decisions=[{"relation_id": result["basis_relations"][0]["relation_id"], "decision": "accept", "rationale": "Direct literal support."}],
        reviewer_id="checkpoint-v4-reviewer",
        reviewed_at="2026-08-07T13:00:00+02:00",
        created_at="2026-08-07T11:00:00Z",
    )


def _build_checkpoint(value):
    source = value["source_identity"]
    binding = next(row for row in value["current_identity_bindings"] if row["evidence_ref"] == "raw_inputs.0.chunk.0")
    evidence = [{key: binding[key] for key in ("evidence_ref", "evidence_fingerprint")}]
    origins = {
        "capture_origin": {key: source[key] for key in ("capture_id", "capture_fingerprint")},
        "operation_origin": {"operation_id": source["operation_plan_id"], "operation_fingerprint": source["operation_fingerprint"]},
    }
    judgment = memory.build_tile_judgment(
        tile_id="M1", component_key="mission", assessment_state="ok", supporting_evidence=evidence,
        series_contract=_series(), authority_state="pending", review_state="none", lifecycle_state="active", lifecycle_reason="", **origins,
    )
    component = evaluation.build_component_evaluation(
        component_key="mission", series_fingerprint=judgment["series_fingerprint"], request_fingerprint=_sha("checkpoint-request"),
        status="evaluated", tile_results=[{"tile_id": "M1", "assessment_state": "ok", "supporting_evidence": evidence}],
    )
    return build_evidence_vault_sv9_evaluation_checkpoint(
        evaluation_input=value, prior_authority_snapshot={"state": "bootstrap_absent"},
        current_series_fingerprint=judgment["series_fingerprint"], canonical_plan_fingerprint=_sha("checkpoint-plan"),
        candidate_series_fingerprint=_sha("candidate-series"), healthy_tile_ids=["M1"],
        component_evaluations=[component], evaluated_tile_judgments=[judgment],
    )


def test_bootstrap_v4_checkpoint_round_trip_and_append_only_guard():
    repository = _reset_repository()
    scan = "checkpoint-v4-real-postgres"
    _prepare(repository, scan)
    value = project_evidence_vault_sv9_evaluation_input(repository=repository, source_scan_id=scan)
    assert value["schema_version"] == "evidence-vault-sv9-evaluation-input-v4"
    assert value["processing_complete_evidence"]
    stored, inserted = repository.append_evidence_vault_sv9_evaluation_checkpoint(scan, _build_checkpoint(value))
    assert inserted
    loaded = repository.get_evidence_vault_sv9_evaluation_checkpoint(scan, checkpoint_fingerprint=stored["checkpoint_fingerprint"])
    assert loaded == stored
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute("UPDATE b3s_history.evidence_vault_sv9_evaluation_checkpoints SET checkpoint_payload = checkpoint_payload WHERE id = %s", (stored["id"],))
