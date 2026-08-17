from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_incremental_executor import (
    execute_vault_operation_plan,
    validate_vault_operation_result,
)
from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan


class ExecutorLLM:
    api_key = "test"

    def __init__(self):
        self.calls = []

    def _call_json(self, system, user, **kwargs):
        self.calls.append(kwargs["schema_name"])
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
        assert kwargs["schema_name"] == "evidence_tile_relation_proposals"
        marker = '"evidence_fingerprint": "'
        fingerprint = user.split(marker, 1)[1].split('"', 1)[0]
        return {
            "relations": [
                {
                    "evidence_fingerprint": fingerprint,
                    "tile_id": "M1",
                    "polarity": "supports",
                    "literal_quote": "help teams ship better products",
                    "rationale": "This is an explicit organizational contribution.",
                }
            ]
        }



class NoCallLLM:
    api_key = "test"

    def _call_json(self, *args, **kwargs):
        raise AssertionError("LLM must not be called")


class MemoryRepository:
    def __init__(self, *, plan, rows, status="pending", result=None):
        self.context = {
            "source_scan_id": "scan-1",
            "brand_identity": "example.com",
            "observation_hash": "b" * 64,
            "plan": plan,
            "evidence_records": rows,
            "raw_observation": {"evidence": rows},
        }
        self.operation = {
            "status": status,
            "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
            "lease_token": None,
            "lease_generation": 0,
            "result_payload": result,
            "result_fingerprint": (
                canonical_fingerprint("evidence-vault-operation-result-v1", result)
                if result is not None
                else None
            ),
        }
        self.source_packets = []
        self.operational_packets = []
        self.failures = []

    def claim_capture_operation_plan(self, source_scan_id, **kwargs):
        if self.operation["status"] == "result_persisted":
            return dict(self.operation, claimed=False, claim_status="result_persisted")
        if self.operation["status"] == "completed":
            return dict(self.operation, claimed=False, claim_status="completed")
        self.operation.update(
            status="claimed",
            lease_token="token",
            lease_generation=self.operation["lease_generation"] + 1,
        )
        return dict(self.operation, claimed=True, claim_status="acquired")

    def mark_capture_operation_running(self, source_scan_id, **kwargs):
        self.operation["status"] = "running"
        return dict(self.operation)

    def heartbeat_capture_operation_plan(self, source_scan_id, **kwargs):
        return dict(self.operation)

    def get_capture_operation_plan(self, source_scan_id, **kwargs):
        return {**self.context, **self.operation}

    def get_evidence_vault_operational_memory(self, *args, **kwargs):
        return None

    def persist_capture_operation_result(self, source_scan_id, **kwargs):
        result = kwargs["result_payload"]
        self.operation.update(
            status="result_persisted",
            result_payload=result,
            result_fingerprint=canonical_fingerprint(
                "evidence-vault-operation-result-v1", result
            ),
            lease_token=None,
        )
        return dict(self.operation)

    def register_evidence_vault_operational_source_packet(
        self, domain, packet, **kwargs
    ):
        self.source_packets.append(packet)
        return {"packet": packet}, False

    def register_evidence_vault_operational_memory_packet(
        self, domain, packet, **kwargs
    ):
        self.operational_packets.append(packet)
        return {"packet": packet}, False

    def finalize_capture_operation_plan(self, source_scan_id, **kwargs):
        assert kwargs["result_fingerprint"] == self.operation["result_fingerprint"]
        self.operation["status"] = "completed"
        return dict(self.operation)

    def fail_capture_operation_plan(self, source_scan_id, **kwargs):
        self.failures.append(kwargs["error"])
        self.operation["status"] = "failed_retryable"
        return dict(self.operation)


def _claims(rows: list[dict[str, object]]) -> list[str]:
    baseline = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="baseline",
        current_evidence_records=rows,
    )
    return list(baseline["semantic_context"]["evidence_fingerprints"])


def _row(content="We help teams ship better products."):
    return {
        "ref": "web.home",
        "source": "web",
        "evidence_type": "owned_copy",
        "url": "https://example.com/",
        "content": content,
        "confidence": "high",
        "metadata": {
            "source_class": "owned_copy",
            "identity_match": "domain",
        },
    }


def _baseline_plan(rows):
    return build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="baseline",
        current_evidence_records=rows,
    )



def test_executor_builds_pending_overlay_without_authority_or_score() -> None:
    rows = [_row()]
    repository = MemoryRepository(plan=_baseline_plan(rows), rows=rows)
    llm = ExecutorLLM()

    execution = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="scan-1",
        worker_id="worker-a",
        llm=llm,
    )

    assert execution["execution_status"] == "completed"
    assert llm.calls == ["sv9_flow_evidence_labeling", "evidence_tile_relation_proposals"]
    result = repository.operation["result_payload"]
    validate_vault_operation_result(result)
    assert result["authority"] is False
    assert "score" not in result
    assert len(result["source_candidate_packet"]["candidate_tiles"]) == 80
    relation = result["basis_relations"][0]
    assert relation["tile_id"] == "M1"
    assert relation["review_status"] == "unreviewed"
    assert relation["decision_event_id"] is None
    assert relation["evidence_id"] == result["selected_evidence_ids"][0]
    assert relation["source_identity_id"] == result["selected_document_ids"][0]
    operational = result["operational_candidate_packet"]
    assert operational["has_accepted_change"] is False
    assert operational["accepted_memory"]["accepted_tiles"] == []
    assert repository.source_packets and repository.operational_packets



def test_executor_processes_c7_plan_without_special_runtime_gate() -> None:
    rows = [_row("Stable evidence")]
    plan = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="incremental_refresh",
        current_evidence_records=rows,
        previous_capture_evidence_records=rows,
        known_evidence_records=rows,
        semantic_analysis_claimed_fingerprints=_claims(rows),
        canonical_memory_version="a" * 64,
    )
    plan = deepcopy(plan)
    delta_unsigned = {
        key: value for key, value in plan["delta"].items()
        if key != "delta_fingerprint"
    }
    delta_unsigned["affected_tile_ids"] = ["C7"]
    delta_unsigned["summary"]["affected_tile_count"] = 1
    plan["delta"] = {
        **delta_unsigned,
        "delta_fingerprint": canonical_fingerprint(
            plan["delta"]["schema_version"],
            delta_unsigned,
        ),
    }
    plan["operations"]["reevaluate_tile_ids"] = ["C7"]
    plan_unsigned = {
        key: value for key, value in plan.items()
        if key != "operation_plan_fingerprint"
    }
    plan["operation_plan_fingerprint"] = canonical_fingerprint(
        plan["schema_version"],
        plan_unsigned,
    )
    repository = MemoryRepository(plan=plan, rows=rows)
    execution = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="scan-1",
        worker_id="worker-a",
        llm=NoCallLLM(),
    )

    assert execution["execution_status"] == "completed"
    assert repository.operation["status"] == "completed"
    assert repository.failures == []



def test_executor_rejects_unavailable_frozen_semantic_contract() -> None:
    rows = [_row("Future contract evidence")]
    plan = deepcopy(build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="baseline",
        current_evidence_records=rows,
    ))
    contract = plan["semantic_context"]["semantic_analysis_contract"]
    contract["relation_policy_version"] = "evidence-tile-relation-policy-v4"
    contract_unsigned = {
        key: value
        for key, value in contract.items()
        if key != "semantic_analysis_contract_fingerprint"
    }
    contract["semantic_analysis_contract_fingerprint"] = canonical_fingerprint(
        contract["schema_version"], contract_unsigned
    )
    context_unsigned = {
        key: value
        for key, value in plan["semantic_context"].items()
        if key != "context_fingerprint"
    }
    plan["semantic_context"]["context_fingerprint"] = canonical_fingerprint(
        plan["semantic_context"]["schema_version"], context_unsigned
    )
    plan_unsigned = {
        key: value
        for key, value in plan.items()
        if key != "operation_plan_fingerprint"
    }
    plan["operation_plan_fingerprint"] = canonical_fingerprint(
        plan["schema_version"], plan_unsigned
    )
    repository = MemoryRepository(plan=plan, rows=rows)

    with pytest.raises(
        RuntimeError,
        match="semantic analyzer is unavailable",
    ):
        execute_vault_operation_plan(
            repository=repository,
            source_scan_id="scan-future-contract",
            worker_id="worker-a",
            llm=ExecutorLLM(),
        )

    assert repository.failures

def test_no_delta_executor_performs_zero_llm_and_creates_no_packet() -> None:
    rows = [_row("Stable evidence")]
    plan = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="incremental_refresh",
        current_evidence_records=rows,
        previous_capture_evidence_records=rows,
        known_evidence_records=rows,
        semantic_analysis_claimed_fingerprints=_claims(rows),
        canonical_memory_version="a" * 64,
    )
    repository = MemoryRepository(plan=plan, rows=rows, status="not_required")

    execution = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="scan-1",
        worker_id="worker-a",
        llm=NoCallLLM(),
    )

    assert execution["execution_status"] == "completed"
    assert repository.operation["result_payload"]["output_kind"] == "no_delta"
    assert repository.source_packets == []
    assert repository.operational_packets == []


def test_material_unlabelable_delta_is_not_reported_as_no_delta() -> None:
    previous = _row("Old acquisition marker")
    current = _row("Changed acquisition marker")
    for row in (previous, current):
        row["evidence_type"] = "acquisition.metadata"
        row["metadata"]["source_class"] = "acquisition_metadata"
    plan = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="incremental_refresh",
        current_evidence_records=[current],
        previous_capture_evidence_records=[previous],
        known_evidence_records=[previous],
        canonical_memory_version="a" * 64,
    )
    assert plan["delta"]["requires_incremental_analysis"] is True
    assert plan["operations"]["llm_required"] is False
    repository = MemoryRepository(plan=plan, rows=[current], status="not_required")

    execution = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="scan-1",
        worker_id="worker-a",
        llm=NoCallLLM(),
    )

    assert execution["execution_status"] == "completed"
    assert repository.operation["result_payload"]["output_kind"] == (
        "material_delta_only"
    )
    assert repository.source_packets == []
    assert repository.operational_packets == []


def test_result_persisted_retry_uses_zero_llm_calls() -> None:
    rows = [_row()]
    first = MemoryRepository(plan=_baseline_plan(rows), rows=rows)
    execute_vault_operation_plan(
        repository=first,
        source_scan_id="scan-1",
        worker_id="worker-a",
        llm=ExecutorLLM(),
    )
    frozen = first.operation["result_payload"]
    retry = MemoryRepository(
        plan=_baseline_plan(rows),
        rows=rows,
        status="result_persisted",
        result=frozen,
    )

    execution = execute_vault_operation_plan(
        repository=retry,
        source_scan_id="scan-1",
        worker_id="worker-b",
        llm=NoCallLLM(),
    )

    assert execution["execution_status"] == "completed"
    assert retry.source_packets and retry.operational_packets


def test_deterministic_identity_mismatch_cannot_be_overridden_by_llm() -> None:
    row = _row("External same-name company evidence.")
    row["source"] = "exa"
    row["url"] = "https://other.example"
    row["metadata"] = {
        "source_class": "external_proof",
        "identity_match": "none",
    }
    repository = MemoryRepository(plan=_baseline_plan([row]), rows=[row])
    llm = ExecutorLLM()

    execution = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="scan-1",
        worker_id="worker-a",
        llm=llm,
    )

    assert execution["execution_status"] == "completed"
    result = repository.operation["result_payload"]
    fingerprint = result["selected_evidence_fingerprints"][0]
    assert result["evidence_work_dispositions"][fingerprint] == (
        "deterministic_identity_mismatch"
    )
    assert result["tile_shortlists"] == {}
    assert result["basis_relations"] == []
    assert llm.calls == ["sv9_flow_evidence_labeling"]


def test_acquisition_metadata_is_persisted_as_deterministically_ineligible() -> None:
    rows = [{
        "ref": "acquisition.0",
        "source": "web",
        "evidence_type": "acquisition.provider_status",
        "url": "https://example.com",
        "content": "Provider returned a partial response.",
        "confidence": "high",
        "metadata": {"source_class": "acquisition_metadata"},
    }]
    repository = MemoryRepository(plan=_baseline_plan(rows), rows=rows)

    execution = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="scan-1",
        worker_id="worker-a",
        llm=None,
    )

    assert execution["execution_status"] == "completed"
    result = repository.operation["result_payload"]
    assert result["selected_evidence_fingerprints"] == []
    assert result["evidence_work_dispositions"] == {}
    assert result["tile_shortlists"] == {}
    assert result["basis_relations"] == []
    assert result["labeling_debug"]["status"] == "not_required"


def test_baseline_relation_work_is_deterministically_chunked() -> None:
    rows = [
        {
            **_row(f"We help teams ship better products. Evidence {index}."),
            "ref": f"web.{index}",
            "url": f"https://example.com/{index}",
        }
        for index in range(25)
    ]
    repository = MemoryRepository(plan=_baseline_plan(rows), rows=rows)
    llm = ExecutorLLM()

    execution = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="scan-1",
        worker_id="worker-a",
        llm=llm,
    )

    assert execution["execution_status"] == "completed"
    result = repository.operation["result_payload"]
    assert result["relation_proposal_call_count"] == 2
    assert llm.calls.count("sv9_flow_evidence_labeling") == 25
    assert llm.calls[-2:] == ["evidence_tile_relation_proposals"] * 2
    assert sum(len(rows) for rows in result["tile_shortlists"].values()) == 125


class BroadLabelExecutorLLM(ExecutorLLM):
    def _call_json(self, system, user, **kwargs):
        if kwargs["schema_name"] == "sv9_flow_evidence_labeling":
            self.calls.append(kwargs["schema_name"])
            payload = json.loads(user)
            return {
                "labels": [
                    {
                        "ref": row["ref"],
                        "relevant_blocks": [
                            "attributes",
                            "brand_idea",
                            "coherencia",
                            "core_purpose",
                            "magnetism",
                            "mission",
                            "personality",
                            "value_proposition",
                            "values",
                            "vision",
                        ],
                        "stance": "supports",
                        "identity_match": "domain",
                        "specificity": "explicit",
                    }
                    for row in payload["records"]
                ]
            }
        self.calls.append(kwargs["schema_name"])
        return {"relations": []}


def test_baseline_caps_broad_semantic_shortlists_with_audit() -> None:
    rows = [_row("We help teams ship better products.")]
    repository = MemoryRepository(plan=_baseline_plan(rows), rows=rows)

    execution = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="scan-1",
        worker_id="worker-a",
        llm=BroadLabelExecutorLLM(),
    )

    assert execution["execution_status"] == "completed"
    result = repository.operation["result_payload"]
    fingerprint = result["selected_evidence_fingerprints"][0]
    assert len(result["tile_shortlists"][fingerprint]) == 24
    assert result["shortlist_truncations"][fingerprint]["eligible_count"] == 70
    assert len(
        result["shortlist_truncations"][fingerprint]["omitted_tile_ids"]
    ) == 46
    validate_vault_operation_result(result)
