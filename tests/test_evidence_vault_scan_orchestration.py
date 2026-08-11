from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import pytest

from src.history.report_parser import canonical_json_hash
from src.services.evidence_vault_incremental_refresh import canonical_evidence_rows
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



def test_opaque_verified_run_id_is_not_interpreted_as_unix_time() -> None:
    snapshot = _snapshot("Evidence")
    snapshot["run"]["id"] = (1 << 63) - 1

    observation = build_capture_observation_from_snapshot(
        snapshot=snapshot,
        scan_id="scan-opaque-run-id",
        url="https://example.com",
        brand_name="Example",
        mode="incremental",
    )

    assert observation["source_run_id"] == str((1 << 63) - 1)
    assert observation["observed_at"] == observation["recorded_at"]
    assert observation["observed_at"].endswith("+00:00")


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
    snapshot = _snapshot("Evidence")
    snapshot["acquisition_steps"]["web"] = {
        "status": "error",
        "details": {"reason": "timeout"},
    }
    previous_observation = build_capture_observation_from_snapshot(
        snapshot=snapshot,
        scan_id="scan-old",
        url="https://example.com",
        brand_name="Example",
        mode="incremental_refresh",
        observed_at="2026-08-06T10:00:00Z",
    )
    assert any(
        str(row.get("evidence_type") or "").startswith("acquisition.attempt.")
        for row in previous_observation["evidence_records"]
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
        snapshot=snapshot,
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
    assert result["report_observation"] == repository.persisted[0]
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


def test_operational_c7_relations_are_excluded_while_cutover_is_disabled() -> None:
    previous = build_capture_observation_from_snapshot(
        snapshot=_snapshot("Old evidence"),
        scan_id="scan-old-c7-off",
        url="https://example.com",
        brand_name="Example",
        mode="incremental_refresh",
        observed_at="2026-08-06T10:00:00Z",
    )
    fingerprint = canonical_evidence_rows(
        previous["evidence_records"],
        subject_url="https://example.com",
    )[0].fingerprint
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
        scan_id="scan-new-c7-off",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        accepted_evidence_tile_relations=[
            {"tile_id": "C7", "evidence_fingerprint": fingerprint},
            {"tile_id": "M1", "evidence_fingerprint": fingerprint},
        ],
        observed_at="2026-08-06T11:00:00Z",
    )

    assert result["operation_plan"]["delta"]["affected_tile_ids"] == ["M1"]


def test_allowlisted_operational_c7_relation_can_enter_vault_plan(
    monkeypatch,
) -> None:
    previous = build_capture_observation_from_snapshot(
        snapshot=_snapshot("Old evidence"),
        scan_id="scan-old-c7-on",
        url="https://example.com",
        brand_name="Example",
        mode="incremental_refresh",
        observed_at="2026-08-06T10:00:00Z",
    )
    fingerprint = canonical_evidence_rows(
        previous["evidence_records"],
        subject_url="https://example.com",
    )[0].fingerprint
    repository = _Repository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "a" * 64,
        },
        history=[_history_capture(previous, analysis_status="completed")],
    )
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_C7_CUTOVER_ENABLED", "true")
    monkeypatch.setenv("BRAND3_VAULT_C7_EMERGENCY_DENY", "false")
    monkeypatch.setenv("BRAND3_VAULT_C7_ALLOWLIST", "example.com")

    result = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("New evidence"),
        scan_id="scan-new-c7-on",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        accepted_evidence_tile_relations=[
            {"tile_id": "C7", "evidence_fingerprint": fingerprint},
            {"tile_id": "M1", "evidence_fingerprint": fingerprint},
        ],
        observed_at="2026-08-06T11:00:00Z",
    )

    assert result["operation_plan"]["delta"]["affected_tile_ids"] == ["C7", "M1"]


def test_emergency_flip_before_persistence_rebuilds_plan_without_c7(
    monkeypatch,
) -> None:
    previous = build_capture_observation_from_snapshot(
        snapshot=_snapshot("Old evidence"),
        scan_id="scan-old-c7-flip",
        url="https://example.com",
        brand_name="Example",
        mode="incremental_refresh",
        observed_at="2026-08-06T10:00:00Z",
    )
    fingerprint = canonical_evidence_rows(
        previous["evidence_records"],
        subject_url="https://example.com",
    )[0].fingerprint
    repository = _Repository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "a" * 64,
        },
        history=[_history_capture(previous, analysis_status="completed")],
    )
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_C7_CUTOVER_ENABLED", "true")
    monkeypatch.setenv("BRAND3_VAULT_C7_EMERGENCY_DENY", "false")
    monkeypatch.setenv("BRAND3_VAULT_C7_ALLOWLIST", "example.com")
    from src.services import evidence_vault_scan_orchestration as orchestration

    original_builder = orchestration.build_vault_scan_plan
    calls = 0

    def build_then_kill(**kwargs):
        nonlocal calls
        calls += 1
        result = original_builder(**kwargs)
        if calls == 1:
            monkeypatch.setenv("BRAND3_VAULT_C7_EMERGENCY_DENY", "true")
        return result

    monkeypatch.setattr(orchestration, "build_vault_scan_plan", build_then_kill)

    result = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("New evidence"),
        scan_id="scan-new-c7-flip",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        accepted_evidence_tile_relations=[
            {"tile_id": "C7", "evidence_fingerprint": fingerprint},
            {"tile_id": "M1", "evidence_fingerprint": fingerprint},
        ],
        observed_at="2026-08-06T11:00:00Z",
    )

    assert calls == 2
    assert result["operation_plan"]["delta"]["affected_tile_ids"] == ["M1"]
    assert repository.persisted[0]["metadata"]["operation_plan"] == result[
        "operation_plan"
    ]


@pytest.mark.parametrize("first_class", [False, True])
def test_emergency_deny_suppresses_persisted_c7_plan_retry_without_write(
    monkeypatch,
    first_class: bool,
) -> None:
    previous = build_capture_observation_from_snapshot(
        snapshot=_snapshot("Old evidence"),
        scan_id="scan-old-c7-retry",
        url="https://example.com",
        brand_name="Example",
        mode="incremental_refresh",
        observed_at="2026-08-06T10:00:00Z",
    )
    fingerprint = canonical_evidence_rows(
        previous["evidence_records"],
        subject_url="https://example.com",
    )[0].fingerprint
    memory = {
        "brand_identity": "example.com",
        "canonical_memory_version": "a" * 64,
    }
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_C7_CUTOVER_ENABLED", "true")
    monkeypatch.setenv("BRAND3_VAULT_C7_EMERGENCY_DENY", "false")
    monkeypatch.setenv("BRAND3_VAULT_C7_ALLOWLIST", "example.com")
    initial = _Repository(
        memory=memory,
        history=[_history_capture(previous, analysis_status="completed")],
    )
    first = prepare_vault_scan_after_capture(
        repository=initial,
        snapshot=_snapshot("New evidence"),
        scan_id="scan-new-c7-retry",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        accepted_evidence_tile_relations=[
            {"tile_id": "C7", "evidence_fingerprint": fingerprint},
        ],
        observed_at="2026-08-06T11:00:00Z",
    )
    raw = initial.persisted[0]
    plan = first["operation_plan"]
    assert plan["delta"]["affected_tile_ids"] == ["C7"]

    if first_class:
        class RetryRepository(_Repository):
            def get_capture_operation_plan(self, _source_scan_id, **_kwargs):
                return {
                    "status": "pending",
                    "raw_observation": raw,
                    "plan": plan,
                    "result_fingerprint": None,
                }

        retry_repository = RetryRepository(memory=memory, history=[])
    else:
        retry_repository = _Repository(
            memory=memory,
            history=[_history_capture(raw, analysis_status="pending")],
        )
    monkeypatch.setenv("BRAND3_VAULT_C7_EMERGENCY_DENY", "true")

    retried = prepare_vault_scan_after_capture(
        repository=retry_repository,
        snapshot=_snapshot("New evidence"),
        scan_id="scan-new-c7-retry",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        accepted_evidence_tile_relations=[
            {"tile_id": "C7", "evidence_fingerprint": fingerprint},
        ],
        observed_at="2026-08-06T11:00:00Z",
    )

    assert retried["operation_plan"] is None
    assert retried["resume"]["operational_c7_cutover_blocked"] is True
    assert retried["resume"]["work_required"] is False
    assert retry_repository.persisted == []


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
    snapshot = _snapshot("Completed evidence")
    snapshot["acquisition_steps"]["web"] = {
        "status": "error",
        "details": {"reason": "timeout"},
    }
    repository = _Repository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "a" * 64,
        },
        history=[],
    )
    first = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=snapshot,
        scan_id="scan-completed",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    persisted = repository.persisted[0]
    assert any(
        str(row.get("evidence_type") or "").startswith("acquisition.attempt.")
        for row in persisted["evidence_records"]
    )
    completed = _history_capture(persisted, analysis_status="completed")
    completed["metadata"]["analysis_result_fingerprint"] = "c" * 64
    repository.history = [completed]

    retried = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=snapshot,
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
        "semantic_work_completed": True,
        "work_required": False,
    }
    assert retried["report_observation"] == completed["raw_observation"]



def test_retry_rejects_changed_capture_artifacts_for_same_scan_id() -> None:
    repository = _Repository(memory=None, history=[])
    first = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Artifact evidence"),
        scan_id="scan-artifact-conflict",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        artifacts=({"kind": "screenshot", "path": "/tmp/a.png"},),
        observed_at="2026-08-06T11:00:00Z",
    )
    persisted = repository.persisted[0]
    repository.history = [_history_capture(persisted, analysis_status="pending")]

    with pytest.raises(
        EvidenceVaultScanOrchestrationError,
        match="different exact capture",
    ):
        prepare_vault_scan_after_capture(
            repository=repository,
            snapshot=_snapshot("Artifact evidence"),
            scan_id="scan-artifact-conflict",
            url="https://example.com",
            brand_name="Example",
            environment="vault",
            incremental_enabled=True,
            artifacts=({"kind": "screenshot", "path": "/tmp/b.png"},),
            observed_at="2026-08-06T11:00:00Z",
        )
    assert first["report_observation"]["artifacts"][0]["path"] == "/tmp/a.png"


def test_v1_retry_ignores_artifacts_that_old_runner_never_persisted() -> None:
    repository = _Repository(memory=None, history=[])
    first = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Legacy artifact evidence"),
        scan_id="scan-v1-artifact-resume",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    persisted = repository.persisted[0]
    persisted["pipeline_version"] = "vault-incremental-capture-v1"
    assert persisted["artifacts"] == []
    repository.history = [_history_capture(persisted, analysis_status="pending")]

    resumed = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Legacy artifact evidence"),
        scan_id="scan-v1-artifact-resume",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        artifacts=({"kind": "screenshot", "path": "/tmp/new.png"},),
        observed_at="2026-08-06T11:00:00Z",
    )

    assert resumed["operation_plan"] == first["operation_plan"]
    assert resumed["report_observation"]["pipeline_version"] == (
        "vault-incremental-capture-v1"
    )
    assert resumed["report_observation"]["artifacts"] == []


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

    assert resumed["operation_plan"] == plan
    assert resumed["resume"]["analysis_status"] == "result_persisted"
    assert resumed["resume"]["semantic_work_required"] is False
    assert resumed["resume"]["semantic_work_completed"] is True
    assert resumed["resume"]["materialization_required"] is True
    assert resumed["resume"]["work_required"] is True


def test_completed_first_class_retry_rejects_unattributed_memory_advance() -> None:
    parent = {
        "brand_identity": "example.com",
        "canonical_memory_version": "a" * 64,
    }
    initial = _Repository(memory=parent, history=[])
    first = prepare_vault_scan_after_capture(
        repository=initial,
        snapshot=_snapshot("Completed first-class evidence"),
        scan_id="scan-first-class-completed",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    raw = initial.persisted[0]
    plan = first["operation_plan"]

    class CompletedRepository(_Repository):
        def get_capture_operation_plan(self, source_scan_id, **_kwargs):
            return {
                "status": "completed",
                "raw_observation": raw,
                "plan": plan,
                "result_fingerprint": "f" * 64,
            }

    repository = CompletedRepository(
        memory={
            "brand_identity": "example.com",
            "canonical_memory_version": "b" * 64,
        },
        history=[],
    )
    with pytest.raises(
        EvidenceVaultScanOrchestrationError,
        match="superseded canonical parent",
    ):
        prepare_vault_scan_after_capture(
            repository=repository,
            snapshot=_snapshot("Completed first-class evidence"),
            scan_id="scan-first-class-completed",
            url="https://example.com",
            brand_name="Example",
            environment="vault",
            incremental_enabled=True,
            observed_at="2026-08-06T11:00:00Z",
        )



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
