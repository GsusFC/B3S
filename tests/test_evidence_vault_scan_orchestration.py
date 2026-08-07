from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import pytest

from src.history.report_parser import canonical_json_hash
from src.services.evidence_vault_scan_orchestration import (
    EvidenceVaultScanOrchestrationError,
    bind_vault_report_to_capture_observation,
    build_capture_observation_from_snapshot,
    prepare_vault_scan_after_capture,
)


@dataclass
class _Outcome:
    source_scan_id: str

    def to_dict(self) -> dict[str, str]:
        return {"source_scan_id": self.source_scan_id, "status": "imported"}


class _Repository:
    def __init__(self, *, memory: dict | None, history: list[dict]) -> None:
        self.memory = memory
        self.history = history
        self.persisted: list[dict] = []
        self.memory_reads = 0

    def get_evidence_vault_operational_memory(self, *_args, **_kwargs):
        self.memory_reads += 1
        return self.memory

    def list_capture_observations_for_domain(self, *_args, **_kwargs):
        return self.history

    def persist_capture_observation(self, observation, **_kwargs):
        self.persisted.append(observation)
        return _Outcome(observation["source_scan_id"])


def test_report_binding_freezes_exact_capture_hashes() -> None:
    observation = build_capture_observation_from_snapshot(
        snapshot=_snapshot("Evidence"),
        scan_id="scan-bound",
        url="https://example.com",
        brand_name="Example",
        mode="diagnostic_full",
        observed_at="2026-08-06T10:00:00Z",
    )

    report = bind_vault_report_to_capture_observation(
        {"id": "scan-bound", "raw": {"source_run_id": "42"}},
        observation,
    )

    assert report["raw"]["source_capture"] == {
        "source_scan_id": "scan-bound",
        "observation_hash": canonical_json_hash(observation),
        "capture_hash": canonical_json_hash(observation["capture_payload"]),
    }



def test_production_bypass_does_not_touch_vault_repository() -> None:
    repository = _Repository(memory=None, history=[])

    result = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Evidence"),
        scan_id="scan-production",
        url="https://example.com",
        brand_name="Example",
        environment="production",
        incremental_enabled=True,
    )

    assert result["execution_path"] == "existing_scanner"
    assert result["capture_persisted"] is False
    assert repository.memory_reads == 0
    assert repository.persisted == []


def test_identical_vault_refresh_persists_capture_and_plans_zero_llm() -> None:
    previous_observation = build_capture_observation_from_snapshot(
        snapshot=_snapshot("Evidence"),
        scan_id="scan-old",
        url="https://example.com",
        brand_name="Example",
        mode="incremental_refresh",
        observed_at="2026-08-06T10:00:00Z",
    )
    repository = _Repository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "a" * 64,
        },
        history=[_history_capture(previous_observation, analysis_status="completed")],
    )

    result = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Evidence"),
        scan_id="scan-new",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )

    plan = result["operation_plan"]
    assert result["capture_persisted"] is True
    assert len(repository.persisted) == 1
    assert repository.persisted[0]["metadata"]["operation_plan"] == plan
    assert repository.persisted[0]["metadata"]["analysis_status"] == "not_required"
    assert plan["mode"] == "incremental_refresh"
    assert plan["canonical_impact"] == "none"
    assert plan["operations"]["llm_required"] is False
    assert plan["operations"]["create_canonical_report"] is False


def test_material_delta_is_scoped_and_never_falls_through_to_full_rerun() -> None:
    previous = build_capture_observation_from_snapshot(
        snapshot=_snapshot("Old evidence"),
        scan_id="scan-old",
        url="https://example.com",
        brand_name="Example",
        mode="incremental_refresh",
        observed_at="2026-08-06T10:00:00Z",
    )
    repository = _Repository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "a" * 64,
        },
        history=[_history_capture(previous, analysis_status="completed")],
    )

    result = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("New evidence"),
        scan_id="scan-new",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )

    plan = result["operation_plan"]
    assert plan["canonical_impact"] == "review_required"
    assert plan["operations"]["llm_required"] is True
    assert len(plan["operations"]["classify_evidence_fingerprints"]) == 1
    assert len(plan["operations"]["reevaluate_tile_ids"]) < 80
    assert plan["operations"]["create_canonical_report"] is False


def test_retry_returns_the_exact_persisted_pending_plan() -> None:
    repository = _Repository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "a" * 64,
        },
        history=[],
    )
    first = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("New evidence"),
        scan_id="scan-retry",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    persisted = repository.persisted[0]
    repository.history = [
        _history_capture(persisted, analysis_status="pending")
    ]
    repository.history[0]["evidence_records"][0]["metadata"][
        "db_projection_only"
    ] = True

    retried = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("New evidence"),
        scan_id="scan-retry",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )

    assert retried["operation_plan"] == first["operation_plan"]
    assert retried["operation_plan"]["operations"]["llm_required"] is True


def test_completed_retry_returns_no_work_plan() -> None:
    repository = _Repository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "a" * 64,
        },
        history=[],
    )
    first = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Completed evidence"),
        scan_id="scan-completed",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    persisted = repository.persisted[0]
    completed = _history_capture(persisted, analysis_status="completed")
    completed["metadata"]["analysis_result_fingerprint"] = "c" * 64
    repository.history = [completed]

    retried = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Completed evidence"),
        scan_id="scan-completed",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )

    assert first["operation_plan"]["operations"]["llm_required"] is True
    assert retried["operation_plan"] is None
    assert retried["resume"] == {
        "analysis_status": "completed",
        "operation_plan_fingerprint": first["operation_plan"][
            "operation_plan_fingerprint"
        ],
        "analysis_result_fingerprint": "c" * 64,
        "work_required": False,
    }



def test_retry_rejects_a_superseded_canonical_parent() -> None:
    observation = build_capture_observation_from_snapshot(
        snapshot=_snapshot("Evidence"),
        scan_id="scan-stale",
        url="https://example.com",
        brand_name="Example",
        mode="incremental_refresh",
        observed_at="2026-08-06T10:00:00Z",
    )
    from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan

    plan = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="incremental_refresh",
        current_evidence_records=observation["evidence_records"],
        previous_capture_evidence_records=[],
        known_evidence_records=[],
        canonical_memory_version="b" * 64,
    )
    observation["metadata"] = {
        **observation["metadata"],
        "operation_plan": plan,
        "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
        "analysis_status": "pending",
    }
    repository = _Repository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "a" * 64,
        },
        history=[_history_capture(observation, analysis_status="pending")],
    )

    with pytest.raises(
        EvidenceVaultScanOrchestrationError,
        match="superseded canonical parent",
    ):
        prepare_vault_scan_after_capture(
            repository=repository,
            snapshot=_snapshot("Evidence"),
            scan_id="scan-stale",
            url="https://example.com",
            brand_name="Example",
            environment="vault",
            incremental_enabled=True,
            observed_at="2026-08-06T10:00:00Z",
        )



def test_new_scan_does_not_treat_pending_capture_as_completed_analysis() -> None:
    pending_observation = build_capture_observation_from_snapshot(
        snapshot=_snapshot("Never analysed"),
        scan_id="scan-pending",
        url="https://example.com",
        brand_name="Example",
        mode="incremental_refresh",
        observed_at="2026-08-06T10:00:00Z",
    )
    pending_observation["metadata"] = {
        **pending_observation["metadata"],
        "analysis_status": "pending",
        "operation_plan": {"mode": "incremental_refresh"},
    }
    repository = _Repository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "a" * 64,
        },
        history=[_history_capture(pending_observation, analysis_status="pending")],
    )

    result = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Never analysed"),
        scan_id="scan-next",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )

    assert result["operation_plan"]["delta"]["summary"][
        "unanalysed_unchanged_count"
    ] == 1
    assert result["operation_plan"]["operations"]["llm_required"] is True



def test_missing_memory_selects_baseline_without_granting_authority() -> None:
    repository = _Repository(memory=None, history=[])

    result = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Initial evidence"),
        scan_id="scan-baseline",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )

    plan = result["operation_plan"]
    assert plan["mode"] == "baseline"
    assert plan["authority"] is False
    assert plan["operations"]["create_candidate_packet"] is True
    assert plan["operations"]["create_canonical_report"] is False


def _history_capture(observation: dict, *, analysis_status: str) -> dict:
    raw_observation = deepcopy(observation)
    metadata = {
        "persisted_as": "capture_only",
        "analysis_status": analysis_status,
        "operation_plan": observation.get("metadata", {}).get("operation_plan"),
        "observation": observation.get("metadata", {}),
    }
    return {
        **observation,
        "source_url": observation["url"],
        "capture_hash": canonical_json_hash(observation["capture_payload"]),
        "metadata": metadata,
        "evidence_records": deepcopy(observation["evidence_records"]),
        "raw_observation": raw_observation,
    }


def _snapshot(content: str) -> dict:
    return {
        "run": {
            "id": 42,
            "brand_name": "Example",
            "url": "https://example.com",
        },
        "raw_inputs": [
            {
                "source": "web",
                "payload": {
                    "url": "https://example.com/about",
                    "summary": content,
                },
            }
        ],
        "acquisition_steps": {
            "web": {"status": "ok", "details": {"reason": "captured"}}
        },
        "features": [],
        "acquisition_gate": {
            "state": "pass",
            "issues": [],
            "warnings": [],
        },
    }


def test_retry_uses_first_class_operation_lookup_for_result_recovery() -> None:
    initial = _Repository(memory=None, history=[])
    first = prepare_vault_scan_after_capture(
        repository=initial,
        snapshot=_snapshot("Evidence"),
        scan_id="scan-first-class",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    raw = initial.persisted[0]
    plan = first["operation_plan"]

    class DirectRepository(_Repository):
        def get_capture_operation_plan(self, source_scan_id, **_kwargs):
            return {
                "status": "result_persisted",
                "raw_observation": raw,
                "plan": plan,
                "result_fingerprint": "c" * 64,
            }

        def list_capture_observations_for_domain(self, *_args, **_kwargs):
            raise AssertionError("first-class retry must not scan history")

    repository = DirectRepository(memory=None, history=[])
    resumed = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Evidence"),
        scan_id="scan-first-class",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )

    assert resumed["operation_plan"] is None
    assert resumed["resume"]["analysis_status"] == "result_persisted"
    assert resumed["resume"]["semantic_work_required"] is False
    assert resumed["resume"]["materialization_required"] is True
    assert resumed["resume"]["work_required"] is True


def test_retry_reclaims_expired_lease_but_leaves_active_lease_busy() -> None:
    initial = _Repository(memory=None, history=[])
    first = prepare_vault_scan_after_capture(
        repository=initial,
        snapshot=_snapshot("Evidence"),
        scan_id="scan-lease-retry",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    raw = initial.persisted[0]
    plan = first["operation_plan"]

    class LeaseRepository(_Repository):
        def __init__(self, *, lease_active: bool):
            super().__init__(memory=None, history=[])
            self.lease_active = lease_active

        def get_capture_operation_plan(self, source_scan_id, **_kwargs):
            return {
                "status": "running",
                "lease_active": self.lease_active,
                "raw_observation": raw,
                "plan": plan,
                "result_fingerprint": None,
            }

        def list_capture_observations_for_domain(self, *_args, **_kwargs):
            raise AssertionError("lease retry must use first-class operation lookup")

    expired = prepare_vault_scan_after_capture(
        repository=LeaseRepository(lease_active=False),
        snapshot=_snapshot("Evidence"),
        scan_id="scan-lease-retry",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    active = prepare_vault_scan_after_capture(
        repository=LeaseRepository(lease_active=True),
        snapshot=_snapshot("Evidence"),
        scan_id="scan-lease-retry",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )

    assert expired["operation_plan"] == plan
    assert expired["resume"]["execution_required"] is True
    assert expired["resume"]["work_required"] is True
    assert active["operation_plan"] is None
    assert active["resume"]["execution_required"] is False
    assert active["resume"]["work_required"] is False
