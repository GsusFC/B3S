from __future__ import annotations

from copy import deepcopy

import pytest

from web import scan_runner
from web.api_v1 import presenters, service


def _status(scan_id: str) -> dict:
    return {"id": scan_id, "url": "https://example.test", "brand_name": "Example", "state": "running", "phase": "capture", "phases": [{"key": key, "state": "pending"} for key in ("capture", "interpret", "score", "report")], "acquisition_gate": {}, "completed_at": None}


def _exact_binding(scan_id: str, *, source_run_id: str = "acquisition-run") -> dict[str, str]:
    return {
        "source_scan_id": scan_id,
        "source_run_id": source_run_id,
        "observation_hash": "observation-hash",
        "capture_hash": "capture-hash",
        "canonical_domain": "example.com",
    }


def _exact_report(scan_id: str, binding: dict[str, str]) -> dict:
    return {
        "id": scan_id,
        "url": "https://example.com",
        "raw": {
            "source_run_id": scan_id,
            "source_capture": {
                key: binding[key]
                for key in ("source_scan_id", "observation_hash", "capture_hash")
            },
        },
    }


def _assessment_report(
    scan_id: str,
    binding: dict[str, str],
    *,
    unavailable: bool = False,
) -> dict:
    from src.sv9.aggregator import aggregate
    from src.sv9.models import ComponentResult, STATUS_NOT_EVALUATED
    from tests.test_vault_sv9_parity import _components, _flow_payload

    components = _components()
    if unavailable:
        components["vision"] = ComponentResult(component="vision", status=STATUS_NOT_EVALUATED, error="timeout")
    report = scan_runner._compose_report(scan_id, "https://example.com", "Example", _flow_payload(aggregate(components, brand_name="Example", url="https://example.com").to_dict()))
    report["raw"].update(source_run_id=scan_id, source_capture={key: binding[key] for key in ("source_scan_id", "observation_hash", "capture_hash")})
    return report


def _exact_action(scan_id: str, action_id: str = "durable-action-1") -> dict:
    from src.history.report_parser import canonical_json_hash

    request = {"operation": "exact_resume"}
    return {
        "action_id": action_id,
        "scan_id": scan_id,
        "state": "running",
        "request_payload": request,
        "status_payload": {"state": "running", "phase": "running"},
        "request_fingerprint": canonical_json_hash(request),
    }


def _file_report_store(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from web import report_store

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(scan_runner, "save_report", report_store.save_report)
    monkeypatch.setattr(scan_runner, "load_report", report_store.load_report)
    return report_store


def test_authority_scanner_gate_requires_both_exact_flags(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault"); monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")
    monkeypatch.setenv("BRAND3_VAULT_SV9_AUTHORITY_SCANNER_ENABLED", "true"); assert scan_runner._vault_sv9_authority_scanner_enabled() is True
    monkeypatch.setenv("BRAND3_VAULT_SV9_AUTHORITY_SCANNER_ENABLED", "True"); assert scan_runner._vault_sv9_authority_scanner_enabled() is False
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "production"); assert scan_runner._vault_sv9_authority_scanner_enabled() is False


def test_authority_terminal_actions_and_completed_non_llm_resume_are_guarded(monkeypatch) -> None:
    from src import config
    from src.services import evidence_vault_incremental_executor as executor, evidence_vault_sv9_authority_application as application, evidence_vault_sv9_authority_report as publication, evidence_vault_sv9_authoritative_relations as relations
    monkeypatch.setattr(config, "SV9_FLOW_MODEL", "test-model"); monkeypatch.setattr(executor, "execute_vault_operation_plan", lambda **_k: {"execution_status": "completed"})
    monkeypatch.setattr(relations, "project_evidence_vault_sv9_authoritative_relations", lambda **_k: pytest.fail("legacy relation projector called"))
    monkeypatch.setattr(relations, "project_evidence_vault_sv9_capture_current", lambda **_k: pytest.fail("legacy capture projector called"))
    for action in ("publish_current", "retain_source", "record_no_score"):
        scan_id, saved, validated, sources, source, activations = f"scan-{action}", [], [], [], {"id": "prior"}, []; scan_runner._SCANS[scan_id] = _status(scan_id); scan_runner._SCAN_EVENTS[scan_id] = scan_runner.threading.Event()
        class Repo:
            def activate_evidence_vault_operational_scanner_result(self, *_a, **_k): activations.append(_k["operation_plan_fingerprint"]); return {"created": True}
        monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda value: assert_terminal(scan_id) if value["state"] == "done" else assert_guard(scan_id))
        monkeypatch.setattr(scan_runner, "save_report", saved.append); monkeypatch.setattr(scan_runner, "load_report", lambda value: source if value == "prior" else None)
        monkeypatch.setattr(scan_runner, "_compose_report", lambda identity, _url, _name, payload: {"id": identity, "raw": payload}); monkeypatch.setattr(scan_runner, "_validate_report_sv9_assessment", lambda *_a, **_k: validated.append(True))
        def assert_guard(identity): assert identity in scan_runner._VAULT_ACTIVATIONS
        def assert_terminal(identity): assert identity not in scan_runner._VAULT_ACTIVATIONS and scan_runner._SCANS[identity]["state"] == "done"
        def apply(**kwargs):
            assert "current_evidence" not in kwargs and "authoritative_relations" not in kwargs and kwargs["trusted_irrelevant_evidence"] == [] and activations == ["p"]
            assert scan_runner.cancel_scan(scan_id)["reason"] == "vault_activation_in_progress"
            return {"authority": {"accepted_candidate": {"source_scan_id": "prior" if action == "retain_source" else scan_id}}}
        monkeypatch.setattr(application, "run_evidence_vault_sv9_authority_application", apply)
        monkeypatch.setattr(publication, "project_vault_authority_publication", lambda _app, _current, resolved: sources.append(resolved) or {"action": action, "source_report_id": "prior" if action == "retain_source" else None, "scanner_payload": None if action == "retain_source" else {"sv9": {"brand3_score": None if action == "record_no_score" else 7}}})
        assert scan_runner._run_vault_sv9_authority_scanner(scan_id=scan_id, url="https://example.test", brand_name="Example", repository=Repo(), preparation={"operation_plan": {"operation_plan_fingerprint": "p", "operations": {"llm_required": False}}} if action != "record_no_score" else {"resume": {"analysis_status": "completed", "operation_plan_fingerprint": "p", "semantic_work_completed": False}}, canonical_snapshot={"raw_inputs": [], "acquisition_gate": {"state": "pass"}}, canonical_source_capture=None, gate={"state": "pass"}) is True
        assert scan_runner._SCANS[scan_id]["report_id"] == ("prior" if action == "retain_source" else scan_id) and (saved == [] if action == "retain_source" else len(saved) == 1) and validated == ([] if action == "retain_source" else [True]) and sources == ([source] if action == "retain_source" else [None]) and scan_id not in scan_runner._VAULT_ACTIVATIONS
        scan_runner._SCANS.pop(scan_id, None); scan_runner._SCAN_EVENTS.pop(scan_id, None)


def test_authority_scanner_delegates_capture_partition_failure_to_application(monkeypatch) -> None:
    from src.services import evidence_vault_sv9_authority_application as application
    from src.services import evidence_vault_sv9_authority_report as publication
    from src.services import evidence_vault_sv9_authoritative_relations as relations

    scan_id = "scan-capture-partition-failure"
    scan_runner._SCANS[scan_id] = _status(scan_id)
    scan_runner._SCAN_EVENTS[scan_id] = scan_runner.threading.Event()
    monkeypatch.setattr(scan_runner, "_execute_vault_operational_preparation", lambda **_kwargs: "p")
    monkeypatch.setattr(scan_runner, "_activate_vault_result_unless_cancelled", lambda *_args, **_kwargs: {"created": True})
    monkeypatch.setattr(relations, "project_evidence_vault_sv9_authoritative_relations", lambda **_kwargs: pytest.fail("legacy relation projector called"))
    monkeypatch.setattr(relations, "project_evidence_vault_sv9_capture_current", lambda **_kwargs: pytest.fail("legacy capture projector called"))
    calls = []
    monkeypatch.setattr(application, "run_evidence_vault_sv9_authority_application", lambda **kwargs: calls.append(kwargs) or {"status": "no_new_score", "reason_codes": ["invalid_capture_current"], "evaluation_status": "review_required", "candidate": None, "signed_delta": None, "authority": None})
    monkeypatch.setattr(
        publication,
        "project_vault_authority_publication",
        lambda *_args: {
            "action": "record_no_score",
            "source_report_id": None,
            "scanner_payload": {"sv9": {"brand3_score": None}},
        },
    )
    monkeypatch.setattr(scan_runner, "_compose_report", lambda identity, *_args: {"id": identity})
    monkeypatch.setattr(scan_runner, "_validate_report_sv9_assessment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scan_runner, "_publish_completed_report", lambda *_args: True)
    try:
        assert scan_runner._run_vault_sv9_authority_scanner(
            scan_id=scan_id,
            url="https://example.test",
            brand_name="Example",
            repository=object(),
            preparation={"operation_plan": {"operation_plan_fingerprint": "p", "operations": {"llm_required": False}}},
            canonical_snapshot={"raw_inputs": [], "acquisition_gate": {"state": "pass"}},
            canonical_source_capture=None,
            gate={"state": "pass"},
        ) is True
        assert calls and "current_evidence" not in calls[0] and "authoritative_relations" not in calls[0]
    finally:
        scan_runner._VAULT_ACTIVATIONS.discard(scan_id)
        scan_runner._SCANS.pop(scan_id, None)
        scan_runner._SCAN_EVENTS.pop(scan_id, None)


def test_retained_v1_result_uses_persisted_terminal_report_alias(monkeypatch) -> None:
    status, source, persisted = {"id": "current", "state": "done", "report_id": "prior"}, {"id": "prior"}, None
    monkeypatch.setattr(service, "load_report", lambda value: source if value == "prior" else None); monkeypatch.setattr(service, "scan_status", lambda _value: status); monkeypatch.setattr(service, "_load_persisted_scan_status", lambda _value: persisted)
    try: service.get_completed_report("current"); raise AssertionError("unpersisted alias resolved")
    except service.ApiError as exc: assert exc.status_code == 409
    persisted = status
    assert service.get_scan("current")["id"] == "current" and service.get_completed_report("current")["id"] == "prior" and presenters.scan_links(service.get_completed_report("current")["id"])["report"] == "/report/prior"


def test_active_resume_fails_closed_before_va4a_and_clears_the_guard(monkeypatch) -> None:
    scan_id, activation_calls = "authority-failure", []; scan_runner._SCANS[scan_id] = _status(scan_id); scan_runner._VAULT_ACTIVATIONS.add(scan_id)
    monkeypatch.setattr(scan_runner, "_activate_vault_result_unless_cancelled", lambda *_a, **_k: activation_calls.append(True))
    try:
        try: scan_runner._run_vault_sv9_authority_scanner(scan_id=scan_id, url="https://example.test", brand_name="Example", repository=object(), preparation={"resume": {"analysis_status": "running", "operation_plan_fingerprint": "p"}}, canonical_snapshot={}, canonical_source_capture=None, gate={}); raise AssertionError("authority failure was not raised")
        except RuntimeError as exc: assert str(exc) == "vault_authority_preparation_failed"
        assert scan_id not in scan_runner._VAULT_ACTIVATIONS and activation_calls == []
    finally: scan_runner._SCANS.pop(scan_id, None); scan_runner._VAULT_ACTIVATIONS.discard(scan_id)


def test_post_boundary_error_rejects_cancel_until_outer_terminalization(monkeypatch) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration
    from web import report_store
    scan_id, snapshot, entered, release = "authority-error", {"run": {"id": 1}, "acquisition_steps": {}, "raw_inputs": []}, scan_runner.threading.Event(), scan_runner.threading.Event()
    scan_runner._SCANS[scan_id] = _status(scan_id); scan_runner._SCAN_EVENTS[scan_id] = scan_runner.threading.Event(); monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault"); monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true"); monkeypatch.setenv("BRAND3_VAULT_SV9_AUTHORITY_SCANNER_ENABLED", "true")
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda *_a: None); monkeypatch.setattr(scan_runner, "_capture_snapshot", lambda *_a: snapshot); monkeypatch.setattr(scan_runner, "_build_acquisition_gate", lambda *_a, **_k: {"state": "pass"})
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: object()); monkeypatch.setattr(orchestration, "prepare_vault_scan_after_capture", lambda **_k: {"capture_persisted": True, "report_observation": {}}); monkeypatch.setattr(scan_runner, "_read_back_persisted_vault_capture", lambda **_k: {}); monkeypatch.setattr(scan_runner, "_canonical_snapshot_from_persisted_vault_capture", lambda **_k: (snapshot, None)); monkeypatch.setattr(scan_runner, "_execute_vault_operational_preparation", lambda **_k: "p")
    def fail(*_a, **_k): assert scan_id in scan_runner._VAULT_ACTIVATIONS; raise RuntimeError("post-boundary")
    monkeypatch.setattr(scan_runner, "_activate_vault_result_unless_cancelled", fail); monkeypatch.setattr(scan_runner.traceback, "print_exc", lambda: entered.set() or release.wait(2))
    thread = scan_runner.threading.Thread(target=scan_runner._run, args=(scan_id, "https://example.test", "Example", False))
    try:
        thread.start(); assert entered.wait(2); assert scan_runner.cancel_scan(scan_id)["reason"] == "vault_activation_in_progress"
        release.set(); thread.join(2); assert not thread.is_alive(); assert scan_runner._SCANS[scan_id]["state"] == "error" and scan_id not in scan_runner._VAULT_ACTIVATIONS
    finally: release.set(); thread.join(2); scan_runner._SCANS.pop(scan_id, None); scan_runner._SCAN_EVENTS.pop(scan_id, None); scan_runner._VAULT_ACTIVATIONS.discard(scan_id)


def test_authority_branch_skips_legacy_and_isolates_preparation_failure(monkeypatch) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as legacy
    from src.services import evidence_vault_scan_orchestration as orchestration
    from web import report_store
    scan_id, snapshot, called = "authority-branch", {"run": {"id": 1}, "acquisition_steps": {}, "raw_inputs": []}, []
    scan_runner._SCANS[scan_id] = _status(scan_id); monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault"); monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true"); monkeypatch.setenv("BRAND3_VAULT_SV9_AUTHORITY_SCANNER_ENABLED", "true")
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda *_a: None); monkeypatch.setattr(scan_runner, "_capture_snapshot", lambda *_a: snapshot); monkeypatch.setattr(scan_runner, "_build_acquisition_gate", lambda *_a, **_k: {"state": "pass"})
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: object()); monkeypatch.setattr(orchestration, "prepare_vault_scan_after_capture", lambda **_k: {"capture_persisted": True, "report_observation": {}})
    monkeypatch.setattr(scan_runner, "_read_back_persisted_vault_capture", lambda **_k: {}); monkeypatch.setattr(scan_runner, "_canonical_snapshot_from_persisted_vault_capture", lambda **_k: (snapshot, None))
    monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **_k: called.append("authority") or True); monkeypatch.setattr(legacy, "build_flow_sv9_shadow_eval", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("legacy called"))); monkeypatch.setattr(scan_runner, "_record_vault_sidecar_status", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("sidecar called"))); monkeypatch.setattr(scan_runner, "_run_vault_operational_sidecar", lambda **_k: (_ for _ in ()).throw(AssertionError("sidecar called"))); monkeypatch.setattr(scan_runner, "_run_vault_sv9_judgment_shadow_after_publication", lambda **_k: (_ for _ in ()).throw(AssertionError("shadow called")))
    try: scan_runner._run(scan_id, "https://example.test", "Example", False); assert called == ["authority"]
    finally: scan_runner._SCANS.pop(scan_id, None)
    scan_id, persisted = "authority-preparation", []; scan_runner._SCANS[scan_id] = _status(scan_id)
    monkeypatch.setattr(scan_runner, "_persist_scan_status", persisted.append); monkeypatch.setattr(orchestration, "prepare_vault_scan_after_capture", lambda **_k: (_ for _ in ()).throw(RuntimeError("sensitive-preparation-detail"))); monkeypatch.setattr(scan_runner.traceback, "print_exc", lambda: None)
    try:
        scan_runner._run(scan_id, "https://example.test", "Example", False)
        assert called == ["authority"] and scan_runner._SCANS[scan_id]["error"] == "RuntimeError: vault_authority_preparation_unavailable" and all("sensitive-preparation-detail" not in str(value) for value in persisted)
    finally: scan_runner._SCANS.pop(scan_id, None)


def test_preparation_stage_error_exposes_whitelisted_code_and_preserves_private_cause() -> None:
    from src.services.evidence_vault_scan_orchestration import (
        EvidenceVaultScanOrchestrationStageError,
        _run_preparation_stage,
    )

    cause = RuntimeError("postgresql://scanner:secret@internal/provider-payload")

    def fail():
        raise cause

    with pytest.raises(EvidenceVaultScanOrchestrationStageError) as caught:
        _run_preparation_stage("capture_persistence", fail)
    error = caught.value

    assert error.reason_code == "vault_authority_capture_persist_failed"
    assert str(error) == "vault_authority_capture_persist_failed"
    assert "secret" not in str(error)
    assert error.__cause__ is cause


@pytest.mark.parametrize(
    ("stage", "reason_code"),
    (
        ("operational_memory_read", "vault_authority_operational_memory_read_failed"),
        ("capture_observation_construction", "vault_authority_capture_observation_failed"),
        ("exact_operation_lookup", "vault_authority_operation_lookup_failed"),
        ("capture_history_read", "vault_authority_capture_history_read_failed"),
        ("operation_plan_construction", "vault_authority_operation_plan_failed"),
        ("capture_persistence", "vault_authority_capture_persist_failed"),
    ),
)
def test_each_preparation_stage_has_only_a_stable_public_reason(
    stage: str, reason_code: str
) -> None:
    from src.services.evidence_vault_scan_orchestration import (
        EvidenceVaultScanOrchestrationStageError,
        public_vault_authority_reason_code,
    )

    error = EvidenceVaultScanOrchestrationStageError(
        stage,
        cause=RuntimeError("private prompt payload"),
    )
    assert public_vault_authority_reason_code(error) == reason_code
    assert "private prompt payload" not in str(error)
    error.reason_code = "private prompt payload"
    assert public_vault_authority_reason_code(error) is None
    assert public_vault_authority_reason_code(RuntimeError("private")) is None


def test_authority_persists_stage_code_without_original_failure_details(monkeypatch) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration
    from web import report_store

    scan_id = "authority-stage-error"
    snapshot = {"run": {"id": 1}, "acquisition_steps": {}, "raw_inputs": []}
    persisted: list[dict] = []
    scan_runner._SCANS[scan_id] = _status(scan_id)
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")
    monkeypatch.setenv("BRAND3_VAULT_SV9_AUTHORITY_SCANNER_ENABLED", "true")
    monkeypatch.setattr(scan_runner, "_persist_scan_status", persisted.append)
    monkeypatch.setattr(scan_runner, "_capture_snapshot", lambda *_a: snapshot)
    monkeypatch.setattr(
        scan_runner,
        "_build_acquisition_gate",
        lambda *_a, **_k: {"state": "pass"},
    )
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: object())

    def fail(**_kwargs):
        raise orchestration.EvidenceVaultScanOrchestrationStageError(
            "capture_persistence",
            cause=RuntimeError("provider payload secret and postgresql://private"),
        )

    monkeypatch.setattr(orchestration, "prepare_vault_scan_after_capture", fail)
    monkeypatch.setattr(scan_runner.traceback, "print_exc", lambda: None)
    try:
        scan_runner._run(scan_id, "https://example.test", "Example", False)
        status = scan_runner._SCANS[scan_id]
        assert status["error"] == (
            "RuntimeError: vault_authority_capture_persist_failed"
        )
        assert status["error_code"] == "scan_execution_failed"
        assert all(
            "provider payload" not in str(item)
            and "postgresql://" not in str(item)
            and "secret" not in str(item)
            for item in persisted
        )
    finally:
        scan_runner._SCANS.pop(scan_id, None)


def test_scan_owner_tokens_fence_exact_reads_and_reject_stale_release(monkeypatch) -> None:
    first = scan_runner._acquire_scan_owner("owner-scan", "ordinary")
    assert first and scan_runner._release_scan_owner(first) is True
    current = scan_runner._acquire_scan_owner("owner-scan", "exact_resume")
    assert current and scan_runner._release_scan_owner(first) is False
    reads = []; monkeypatch.setattr(scan_runner, "load_report", lambda report_id: reads.append(report_id) or {"id": report_id})
    assert scan_runner._load_report_for_exact_owner("owner-scan", current.token, "prior")["id"] == "prior" and reads == ["prior"]
    assert scan_runner._release_scan_owner(current) is True
    with pytest.raises(scan_runner._ExactResumeFailure, match="busy"): scan_runner._load_report_for_exact_owner("owner-scan", current.token, "blocked")


def test_exact_current_report_binds_report_identity_to_scan_id(monkeypatch) -> None:
    scan_id = "report-identity"
    binding = _exact_binding(scan_id)
    monkeypatch.setattr(
        scan_runner,
        "_validate_report_sv9_assessment",
        lambda *_args, **_kwargs: {"availability": "available"},
    )

    projection = scan_runner._validate_exact_current_report(
        _exact_report(scan_id, binding),
        scan_id=scan_id,
        binding=binding,
    )

    assert projection["availability"] == "available"


@pytest.mark.parametrize("tampered_field", ("scan_id", "domain", "source_capture"))
def test_exact_current_report_rejects_tampered_binding(tampered_field: str) -> None:
    scan_id = "report-tamper"
    binding = _exact_binding(scan_id)
    report = _exact_report(scan_id, binding)
    if tampered_field == "scan_id":
        report["id"] = "other-scan"
    elif tampered_field == "domain":
        report["url"] = "https://other.example"
    else:
        report["raw"]["source_capture"]["capture_hash"] = "tampered"

    with pytest.raises(scan_runner._ExactResumeFailure, match="report_invalid"):
        scan_runner._validate_exact_current_report(
            report,
            scan_id=scan_id,
            binding=binding,
        )


def test_exact_resume_never_enters_ordinary_scan_state(monkeypatch) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration
    binding = {"source_scan_id": "exact-state", "source_run_id": "run", "observation_hash": "a", "capture_hash": "b", "canonical_domain": "example.com"}
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_k: {"url": "https://example.com", "brand_name": "Example", "preparation": {}, "canonical_snapshot": {}, "report_binding": binding})
    monkeypatch.setattr(scan_runner, "load_report", lambda _id: None); monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **_k: scan_runner._ExactResumePublication("record_no_score", "exact-state"))
    before = dict(scan_runner._SCANS)
    assert scan_runner._run_vault_exact_resume(scan_id="exact-state", action={}, repository=object()).action == "record_no_score" and scan_runner._SCANS == before and "exact-state" not in scan_runner._VAULT_ACTIVATIONS


def test_exact_resume_keeps_arbitrary_busy_text_retryable(monkeypatch) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration
    binding = {"source_scan_id": "exact-error", "source_run_id": "run", "observation_hash": "a", "capture_hash": "b", "canonical_domain": "example.com"}; prepared = {"url": "https://example.com", "brand_name": "Example", "preparation": {}, "canonical_snapshot": {}, "report_binding": binding}
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_k: prepared); monkeypatch.setattr(scan_runner, "load_report", lambda _id: None); monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **_k: (_ for _ in ()).throw(RuntimeError("busy")))
    with pytest.raises(orchestration.VaultExactResumeError) as caught: scan_runner._run_vault_exact_resume(scan_id="exact-error", action={}, repository=object())
    assert caught.value.reason_code == "vault_exact_resume_execution_failed" and caught.value.retryable is True

def test_exact_resume_persistent_loader_errors_are_retryable(monkeypatch) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration; scan_id = "loader-error"; binding = {"source_scan_id": scan_id, "source_run_id": "run", "observation_hash": "a", "capture_hash": "b", "canonical_domain": "example.com"}; prepared = {"url": "https://example.com", "brand_name": "Example", "preparation": {}, "canonical_snapshot": {}, "report_binding": binding}
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_k: prepared)
    def expect():
        with pytest.raises(orchestration.VaultExactResumeError) as caught: scan_runner._run_vault_exact_resume(scan_id=scan_id, action={}, repository=object())
        assert caught.value.reason_code == "vault_exact_resume_execution_failed" and caught.value.retryable is True
    def busy(*_args): raise RuntimeError("busy")
    monkeypatch.setattr(scan_runner, "_load_report_for_exact_owner", busy); monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **_k: (_ for _ in ()).throw(AssertionError("authority"))); expect()
    reads, reports = [], iter((None, RuntimeError("busy"), RuntimeError("busy")))
    def loader(*_args):
        value = next(reports)
        if isinstance(value, Exception): raise value
        return value
    monkeypatch.setattr(scan_runner, "_load_report_for_exact_owner", lambda *_a: reads.append(1) or loader()); monkeypatch.setattr(scan_runner, "save_report", lambda _r: None)
    monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **kwargs: scan_runner._publish_exact_report(scan_id, kwargs["exact_owner"], {"id": scan_id}, binding, "publish_current")); expect(); assert reads == [1] * 2


def test_exact_resume_replays_when_current_assessment_is_unavailable(monkeypatch) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration

    scan_id = "unavailable-current"
    binding = _exact_binding(scan_id)
    prepared = {
        "url": "https://example.com",
        "brand_name": "Example",
        "preparation": {},
        "canonical_snapshot": {},
        "report_binding": binding,
    }
    replayed = []
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_kwargs: prepared)
    monkeypatch.setattr(
        scan_runner,
        "_load_report_for_exact_owner",
        lambda *_args: _exact_report(scan_id, binding),
    )
    monkeypatch.setattr(
        scan_runner,
        "_validate_exact_current_report",
        lambda *_args, **_kwargs: {"availability": "unavailable"},
    )
    monkeypatch.setattr(
        scan_runner,
        "_run_vault_sv9_authority_scanner",
        lambda **kwargs: replayed.append(kwargs)
        or scan_runner._ExactResumePublication("record_no_score", scan_id),
    )

    publication = scan_runner._run_vault_exact_resume(
        scan_id=scan_id,
        action={},
        repository=object(),
    )

    assert publication.action == "record_no_score"
    assert len(replayed) == 1


def test_exact_resume_does_not_publish_unavailable_recovered_report(monkeypatch) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration

    scan_id = "unavailable-recovered"
    binding = _exact_binding(scan_id)
    prepared = {
        "url": "https://example.com",
        "brand_name": "Example",
        "preparation": {},
        "canonical_snapshot": {},
        "report_binding": binding,
    }
    reports = iter((None, _exact_report(scan_id, binding)))
    scanner_calls = []
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_kwargs: prepared)
    monkeypatch.setattr(
        scan_runner,
        "_load_report_for_exact_owner",
        lambda *_args: next(reports),
    )
    monkeypatch.setattr(
        scan_runner,
        "_validate_exact_current_report",
        lambda *_args, **_kwargs: {"availability": "unavailable"},
    )
    monkeypatch.setattr(
        scan_runner,
        "_run_vault_sv9_authority_scanner",
        lambda **_kwargs: scanner_calls.append(True)
        or (_ for _ in ()).throw(RuntimeError("execution")),
    )

    with pytest.raises(orchestration.VaultExactResumeError) as caught:
        scan_runner._run_vault_exact_resume(
            scan_id=scan_id,
            action={},
            repository=object(),
        )

    assert caught.value.reason_code == "vault_exact_resume_execution_failed"
    assert scanner_calls == [True]


@pytest.mark.parametrize("source_exists", (True, False))
def test_exact_resume_record_no_score_keeps_immutable_source(
    monkeypatch,
    tmp_path,
    source_exists,
) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration

    scan_id = "immutable-no-score"
    binding = _exact_binding(scan_id)
    action = _exact_action(scan_id)
    report_store = _file_report_store(monkeypatch, tmp_path)
    original = _assessment_report(scan_id, binding, unavailable=True)
    if source_exists:
        report_store.save_report(original)
        original_bytes = (tmp_path / f"{scan_id}.json").read_bytes()
    prepared = {
        "url": "https://example.com",
        "brand_name": "Example",
        "preparation": {},
        "canonical_snapshot": {},
        "report_binding": binding,
    }
    replayed = []
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_kwargs: prepared)

    def replay(**kwargs):
        replayed.append(True)
        changed = deepcopy(original)
        changed["created_at"] = "replacement-must-not-persist"
        return scan_runner._publish_exact_report(
            scan_id,
            kwargs["exact_owner"],
            changed,
            binding,
            "record_no_score",
            action_identity=action,
            initial_source=kwargs["exact_initial_source"],
        )

    monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", replay)

    publication = scan_runner._run_vault_exact_resume(
        scan_id=scan_id,
        action=action,
        repository=object(),
    )

    assert publication == scan_runner._ExactResumePublication(
        "record_no_score", scan_id
    )
    assert replayed == [True]
    if source_exists:
        assert (tmp_path / f"{scan_id}.json").read_bytes() == original_bytes
    else:
        assert report_store.load_report(scan_id)["sv9_assessment"]["availability"] == "unavailable"
    assert not (tmp_path / f"{scan_runner._exact_successor_report_id(action)}.json").exists()
    if not source_exists:
        monkeypatch.setattr(scan_runner, "save_report", lambda _report: pytest.fail("no-score rerun saved"))
        assert scan_runner._run_vault_exact_resume(scan_id=scan_id, action=action, repository=object()) == publication


def test_exact_resume_no_score_persistence_failure_stays_incomplete(monkeypatch, tmp_path) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration

    scan_id = "no-score-persist-failure"
    binding = _exact_binding(scan_id)
    action = _exact_action(scan_id, "durable-action-persist-failure")
    _file_report_store(monkeypatch, tmp_path)
    report = _assessment_report(scan_id, binding, unavailable=True)
    prepared = {"url": "https://example.com", "brand_name": "Example", "preparation": {}, "canonical_snapshot": {}, "report_binding": binding}
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_kwargs: prepared)
    monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **kwargs: scan_runner._publish_exact_report(scan_id, kwargs["exact_owner"], report, binding, "record_no_score", action_identity=action, initial_source=kwargs["exact_initial_source"]))
    monkeypatch.setattr(scan_runner, "save_report", lambda _report: (_ for _ in ()).throw(OSError("persistence unavailable")))

    with pytest.raises(orchestration.VaultExactResumeError) as caught:
        scan_runner._run_vault_exact_resume(scan_id=scan_id, action=action, repository=object())

    assert caught.value.reason_code == "vault_exact_resume_execution_failed"
    assert not (tmp_path / f"{scan_id}.json").exists()


@pytest.mark.parametrize("case", ("source-race", "successor-race", "source-disappears", "no-score-successor", "successor-equal", "reload-error"))
def test_exact_resume_publication_races_fail_closed(monkeypatch, case) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration
    scan_id = f"publication-{case}"
    binding = _exact_binding(scan_id)
    action = _exact_action(scan_id, f"durable-action-{case}")
    unavailable = _assessment_report(scan_id, binding, unavailable=True)
    candidate, expected_action, initial_source = unavailable, "record_no_score", None
    if case == "source-race":
        candidate = deepcopy(unavailable)
        candidate["created_at"] = "candidate"
        observed = deepcopy(unavailable)
        observed["created_at"] = "race"
        reports = iter((None, None, observed, None))
    elif case in {"successor-race", "successor-equal"}:
        candidate, expected_action = _assessment_report(scan_id, binding), "publish_current"
        observed = deepcopy(candidate)
        observed["id"] = scan_runner._exact_successor_report_id(action)
        if case == "successor-race": observed["created_at"] = "race"
        reports = iter((None, None, None, observed))
    elif case == "source-disappears":
        candidate, expected_action, initial_source = _assessment_report(scan_id, binding), "publish_current", unavailable
        reports = iter((None, unavailable, None, None))
    elif case == "no-score-successor":
        observed = _assessment_report(scan_id, binding)
        observed["id"] = scan_runner._exact_successor_report_id(action)
        reports = iter((None, None, None, observed))
    else:
        reports = iter((None, None, None, None, RuntimeError("reload failed")))
    prepared = {"url": "https://example.com", "brand_name": "Example", "preparation": {}, "canonical_snapshot": {}, "report_binding": binding}
    def load(*_args):
        value = next(reports)
        if isinstance(value, Exception): raise value
        return value
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_kwargs: prepared)
    monkeypatch.setattr(scan_runner, "_load_report_for_exact_owner", load)
    monkeypatch.setattr(scan_runner, "save_report", lambda _report: pytest.fail("exact concurrent successor was re-saved") if case == "successor-equal" else None)
    monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **kwargs: scan_runner._publish_exact_report(scan_id, kwargs["exact_owner"], candidate, binding, expected_action, action_identity=action, initial_source=initial_source))
    if case == "successor-equal":
        assert scan_runner._run_vault_exact_resume(scan_id=scan_id, action=action, repository=object()) == scan_runner._ExactResumePublication("publish_current", scan_runner._exact_successor_report_id(action))
    else:
        with pytest.raises(orchestration.VaultExactResumeError) as caught:
            scan_runner._run_vault_exact_resume(scan_id=scan_id, action=action, repository=object())
        assert caught.value.reason_code == ("vault_exact_resume_execution_failed" if case == "reload-error" else "vault_exact_resume_report_invalid")


def test_exact_resume_record_no_score_rejects_incompatible_existing_source(monkeypatch, tmp_path) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration

    scan_id = "no-score-incompatible-source"
    binding = _exact_binding(scan_id)
    action = _exact_action(scan_id, "durable-action-incompatible-source")
    report_store = _file_report_store(monkeypatch, tmp_path)
    incompatible = _assessment_report(scan_id, binding, unavailable=True)
    incompatible["raw"]["source_capture"]["capture_hash"] = "tampered"
    report_store.save_report(incompatible)
    original_bytes = (tmp_path / f"{scan_id}.json").read_bytes()
    prepared = {"url": "https://example.com", "brand_name": "Example", "preparation": {}, "canonical_snapshot": {}, "report_binding": binding}
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_kwargs: prepared)
    monkeypatch.setattr(scan_runner, "save_report", lambda _report: pytest.fail("incompatible source was overwritten"))

    with pytest.raises(orchestration.VaultExactResumeError) as caught:
        scan_runner._run_vault_exact_resume(scan_id=scan_id, action=action, repository=object())

    assert caught.value.reason_code == "vault_exact_resume_report_invalid"
    assert (tmp_path / f"{scan_id}.json").read_bytes() == original_bytes


def test_exact_resume_persists_deterministic_successor_without_mutating_source(
    monkeypatch,
    tmp_path,
) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration

    scan_id = "immutable-successor"
    binding = _exact_binding(scan_id)
    action = _exact_action(scan_id, "durable-action-successor")
    report_store = _file_report_store(monkeypatch, tmp_path)
    original = _assessment_report(scan_id, binding, unavailable=True)
    report_store.save_report(original)
    original_bytes = (tmp_path / f"{scan_id}.json").read_bytes()
    prepared = {
        "url": "https://example.com",
        "brand_name": "Example",
        "preparation": {},
        "canonical_snapshot": {},
        "report_binding": binding,
    }
    candidate = _assessment_report(scan_id, binding)
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_kwargs: prepared)
    monkeypatch.setattr(
        scan_runner,
        "_run_vault_sv9_authority_scanner",
        lambda **kwargs: scan_runner._publish_exact_report(
            scan_id,
            kwargs["exact_owner"],
            candidate,
            binding,
            "publish_current",
            action_identity=action,
            initial_source=kwargs["exact_initial_source"],
        ),
    )

    publication = scan_runner._run_vault_exact_resume(
        scan_id=scan_id,
        action=action,
        repository=object(),
    )
    successor_id = scan_runner._exact_successor_report_id(action)

    assert publication == scan_runner._ExactResumePublication(
        "publish_current", successor_id
    )
    successor = report_store.load_report(successor_id)
    assert successor is not None
    assert successor["id"] == successor_id
    assert successor["raw"]["source_run_id"] == scan_id
    assert successor["raw"]["source_capture"] == {
        key: binding[key]
        for key in ("source_scan_id", "observation_hash", "capture_hash")
    }
    assert (tmp_path / f"{scan_id}.json").read_bytes() == original_bytes

    monkeypatch.setattr(scan_runner, "save_report", lambda _report: pytest.fail("save rerun"))
    monkeypatch.setattr(
        scan_runner,
        "_run_vault_sv9_authority_scanner",
        lambda **_kwargs: pytest.fail("evaluation rerun"),
    )
    assert scan_runner._run_vault_exact_resume(
        scan_id=scan_id,
        action=action,
        repository=object(),
    ) == publication


@pytest.mark.parametrize("source_unavailable", (True, False))
def test_exact_resume_rejects_tampered_existing_successor(
    monkeypatch,
    tmp_path,
    source_unavailable,
) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration

    scan_id = "immutable-tampered"
    binding = _exact_binding(scan_id)
    action = _exact_action(scan_id, "durable-action-tampered")
    report_store = _file_report_store(monkeypatch, tmp_path)
    report_store.save_report(
        _assessment_report(scan_id, binding, unavailable=source_unavailable)
    )
    successor_id = scan_runner._exact_successor_report_id(action)
    successor = _assessment_report(scan_id, binding)
    successor["id"] = successor_id
    successor["raw"]["source_capture"]["capture_hash"] = "tampered"
    report_store.save_report(successor)
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_kwargs: {
        "url": "https://example.com",
        "brand_name": "Example",
        "preparation": {},
        "canonical_snapshot": {},
        "report_binding": binding,
    })
    monkeypatch.setattr(
        scan_runner,
        "_run_vault_sv9_authority_scanner",
        lambda **_kwargs: pytest.fail("tampered successor was ignored"),
    )

    with pytest.raises(orchestration.VaultExactResumeError) as caught:
        scan_runner._run_vault_exact_resume(
            scan_id=scan_id,
            action=action,
            repository=object(),
        )

    assert caught.value.reason_code == "vault_exact_resume_report_invalid"


def test_exact_resume_composed_report_preserves_scan_source_run_id(monkeypatch) -> None:
    scan_id = "composed-identity"
    binding = _exact_binding(scan_id)
    monkeypatch.setattr(
        scan_runner,
        "_validate_report_sv9_assessment",
        lambda *_args, **_kwargs: {"availability": "legacy"},
    )
    payload = scan_runner._authority_scanner_payload(
        publication={"scanner_payload": {"source_run_id": scan_id, "sv9": {}}},
        canonical_snapshot={},
        canonical_source_capture={
            key: binding[key]
            for key in ("source_scan_id", "observation_hash", "capture_hash")
        },
        gate={},
        exact_report_binding=binding,
    )

    report = scan_runner._compose_report(
        scan_id,
        "https://example.com",
        "Example",
        payload,
    )

    assert report["raw"]["source_run_id"] == scan_id

def test_cancelled_ordinary_owner_waits_for_worker_finally(monkeypatch) -> None:
    scan_id, entered, cancelling, unwinding, release = "owner-unwind", *(scan_runner.threading.Event() for _ in range(4)); owner, exact = scan_runner._acquire_scan_owner(scan_id, "ordinary"), None
    scan_runner._SCANS[scan_id] = _status(scan_id); scan_runner._SCAN_EVENTS[scan_id] = scan_runner.threading.Event()
    def capture(*_args): entered.set(); cancelling.wait(2); unwinding.set(); release.wait(2); return {}
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda *_a, **_k: None); monkeypatch.setattr(scan_runner, "_capture_snapshot", capture)
    thread = scan_runner.threading.Thread(target=scan_runner._run, args=(scan_id, "https://example.test", "Example", False, owner))
    try:
        thread.start(); assert entered.wait(2) and scan_runner.cancel_scan(scan_id)["cancelled"] is True
        cancelling.set(); assert unwinding.wait(2) and scan_runner._acquire_scan_owner(scan_id, "exact_resume") is None
        release.set(); thread.join(2); exact = scan_runner._acquire_scan_owner(scan_id, "exact_resume"); assert exact and scan_runner._release_scan_owner(owner) is False
    finally: release.set(); thread.join(2); [_ for _ in (scan_runner._release_scan_owner(value) for value in (owner, exact) if value)]; scan_runner._SCANS.pop(scan_id, None); scan_runner._SCAN_EVENTS.pop(scan_id, None)


def test_exact_retain_source_fences_owner_and_types_missing_source(monkeypatch) -> None:
    scan_id, owner, reads = "retain-fence", scan_runner._acquire_scan_owner("retain-fence", "exact_resume"), []
    application = {"authority": {"accepted_candidate": {"source_scan_id": "prior"}}}
    monkeypatch.setattr(scan_runner, "load_report", lambda report_id: reads.append((report_id, scan_runner._SCAN_OWNERS.get(scan_id) is owner)) or {"id": report_id})
    try:
        assert scan_runner._accepted_authority_source_report(application, scan_id, owner) == {"id": "prior"} and reads == [("prior", True)]
        assert scan_runner._release_scan_owner(owner)
        with pytest.raises(scan_runner._ExactResumeFailure, match="busy"): scan_runner._accepted_authority_source_report(application, scan_id, owner)
        assert reads == [("prior", True)]
        owner = scan_runner._acquire_scan_owner(scan_id, "exact_resume"); monkeypatch.setattr(scan_runner, "load_report", lambda _id: None)
        with pytest.raises(scan_runner._ExactResumeFailure, match="report_invalid"): scan_runner._accepted_authority_source_report(application, scan_id, owner)
    finally: scan_runner._release_scan_owner(owner)


def test_exact_current_report_reads_use_the_fenced_seam(monkeypatch) -> None:
    from src.services import evidence_vault_scan_orchestration as orchestration
    scan_id, owner = "fenced-current", None; binding = {"source_scan_id": scan_id, "source_run_id": "run", "observation_hash": "a", "capture_hash": "b", "canonical_domain": "example.com"}; prepared = {"url": "https://example.com", "brand_name": "Example", "preparation": {}, "canonical_snapshot": {}, "report_binding": binding}
    calls, loaded, reports, loader = [], [], iter(({"id": "pre"}, None, {"id": "post-save"})), scan_runner._load_report_for_exact_owner
    def fenced(source, token, report_id): calls.append((source, report_id, scan_runner._exact_owner_current(source, token))); return loader(source, token, report_id)
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_k: prepared); monkeypatch.setattr(scan_runner, "load_report", lambda report_id: loaded.append(report_id) or next(reports)); monkeypatch.setattr(scan_runner, "_load_report_for_exact_owner", fenced); monkeypatch.setattr(scan_runner, "_validate_exact_current_report", lambda *_a, **_k: {"availability": "available"})
    try:
        monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **_k: (_ for _ in ()).throw(AssertionError("authority replay")))
        assert scan_runner._run_vault_exact_resume(scan_id=scan_id, action={}, repository=object()).action == "publish_current"
        monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **_k: (_ for _ in ()).throw(RuntimeError("execution")))
        with pytest.raises(orchestration.VaultExactResumeError) as caught:
            scan_runner._run_vault_exact_resume(scan_id=scan_id, action={}, repository=object())
        assert caught.value.reason_code == "vault_exact_resume_execution_failed"
        owner = scan_runner._acquire_scan_owner("post-save", "exact_resume"); monkeypatch.setattr(scan_runner, "save_report", lambda _r: None)
        assert scan_runner._publish_exact_report("post-save", owner, {"id": "post-save"}, binding, "publish_current").action == "publish_current"
        assert scan_runner._release_scan_owner(owner); owner = scan_runner._acquire_scan_owner("post-loss", "exact_resume"); monkeypatch.setattr(scan_runner, "save_report", lambda _r: scan_runner._release_scan_owner(owner))
        with pytest.raises(scan_runner._ExactResumeFailure, match="busy"): scan_runner._publish_exact_report("post-loss", owner, {"id": "post-loss"}, binding, "publish_current")
        assert loaded == [scan_id, scan_id, "post-save"] and [(source, report) for source, report, _current in calls] == [(scan_id, scan_id), (scan_id, scan_id), ("post-save", "post-save"), ("post-loss", "post-loss")] and all(current for *_rest, current in calls[:-1])
    finally: scan_runner._release_scan_owner(owner)


@pytest.mark.parametrize("action", ("publish_current", "record_no_score"))
def test_ordinary_publication_imports_frozen_evidence_without_overwriting_reports(
    monkeypatch,
    tmp_path,
    action,
) -> None:
    from uuid import uuid4

    from src.history.capture_observation import parse_capture_observation
    from src.history.models import ReportConflictError
    from src.history.report_parser import parse_report
    from src.history.repository import _validate_capture_to_report_upgrade
    from src.services import evidence_vault_sv9_authority_application as application
    from src.services import evidence_vault_sv9_authority_report as publication
    from src.services.evidence_vault_scan_orchestration import prepare_vault_scan_after_capture
    from tests.test_evidence_vault_scan_orchestration import _Repository, _snapshot

    scan_id = f"ordinary-{action}"
    prepared = prepare_vault_scan_after_capture(
        repository=_Repository(memory=None, history=[]),
        snapshot=_snapshot("Frozen evidence must reach the report"),
        scan_id=scan_id,
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    observation = prepared["report_observation"]
    capture = parse_capture_observation(observation)
    binding = dict(source_scan_id=scan_id, observation_hash=capture.observation_hash, capture_hash=capture.capture_hash)
    stored_rows = [
        dict(
            id=uuid4(),
            evidence_ref=row["ref"],
            **{key: row[key] for key in ("source", "evidence_type", "url", "content", "confidence", "metadata")},
        )
        for row in capture.evidence_records
    ]
    assert stored_rows
    imported = []

    class CaptureConnection:
        def execute(self, query, params):
            assert "FROM b3s_history.evidence_records" in query
            return self

        def fetchall(self):
            return deepcopy(stored_rows)

    class CaptureRepository:
        def import_report(self, report):
            parsed = parse_report(report)
            ids = _validate_capture_to_report_upgrade(
                CaptureConnection(),
                {
                    "request_payload": observation,
                    "source_url": capture.canonical_url,
                    "content_hash": capture.capture_hash,
                    "capture_id": uuid4(),
                },
                parsed,
            )
            assert len(ids) == len(stored_rows)
            imported.append(parsed)

    report_store = _file_report_store(monkeypatch, tmp_path)
    original = _assessment_report("prior", binding)
    report_store.save_report(original)
    original_bytes = (tmp_path / "prior.json").read_bytes()
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: CaptureRepository())
    payload = _assessment_report(scan_id, binding, unavailable=action == "record_no_score")["raw"]
    payload.pop("flow", None)
    monkeypatch.setattr(application, "run_evidence_vault_sv9_authority_application", lambda **_kwargs: {})
    monkeypatch.setattr(
        publication,
        "project_vault_authority_publication",
        lambda *_args: {"action": action, "scanner_payload": payload},
    )
    monkeypatch.setattr(scan_runner, "_execute_vault_operational_preparation", lambda **_kwargs: "p")
    monkeypatch.setattr(
        scan_runner, "_activate_vault_result_unless_cancelled", lambda *_args, **_kwargs: {"created": True}
    )
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda *_args: None)
    scan_runner._SCANS[scan_id] = _status(scan_id)
    try:
        assert (
            scan_runner._run_vault_sv9_authority_scanner(
                scan_id=scan_id,
                url="https://example.com",
                brand_name="Example",
                repository=object(),
                preparation=prepared,
                canonical_snapshot=observation["capture_payload"],
                canonical_source_capture=binding,
                gate={},
            )
            is True
        )
        assert len(imported) == 1
        assert imported[0].evidence_records == capture.evidence_records
        assert imported[0].evaluation_config["assessment_availability"] == (
            "unavailable" if action == "record_no_score" else "available"
        )
        current_path = tmp_path / f"{scan_id}.json"
        current_bytes = current_path.read_bytes()
        changed = deepcopy(imported[0].report_payload)
        changed["created_at"] = "replacement-must-not-persist"
        with pytest.raises(ReportConflictError):
            report_store.save_report(changed)
        assert current_path.read_bytes() == current_bytes
        assert (tmp_path / "prior.json").read_bytes() == original_bytes
    finally:
        scan_runner._SCANS.pop(scan_id, None)
        scan_runner._VAULT_ACTIVATIONS.discard(scan_id)


@pytest.mark.parametrize("mode", ("ordinary", "exact"))
def test_accepted_same_source_replay_publishes_without_changing_immutable_reports(monkeypatch, tmp_path, mode):
    from src import config
    from src.services import evidence_vault_scan_orchestration as orchestration
    from src.services import evidence_vault_sv9_authority_report as publication
    from src.sv9 import incremental_flow_adapter
    from tests import test_evidence_vault_sv9_authority_application as authority_tests

    original_series = authority_tests._series
    monkeypatch.setattr(config, "SV9_FLOW_MODEL", "model-v1")
    monkeypatch.setattr(
        authority_tests,
        "_series",
        lambda: original_series(
            evaluator_version="evidence-vault-sv9-judgment-authority-v1",
            prompt_version="sv9-strict-component-v1",
            model_version="model-v1",
            flow_version="sv9-flow-strict-component-v1",
            normalization_version="vault-capture-v1",
        ),
    )
    repo = authority_tests._CheckpointReplayRepository(records=(9,))
    first = authority_tests._run(repo, authority_tests._Flow())
    store = _file_report_store(monkeypatch, tmp_path)
    original = scan_runner._compose_report(
        "scan",
        "https://example.test",
        "Example",
        publication.project_vault_authority_publication(first, "scan")["scanner_payload"],
    )
    store.save_report(original)
    assert (
        authority_tests._run(repo, authority_tests._Flow(), current=(3, 9), source="scan-2")["status"]
        == "authority_advanced"
    )
    accepted, writes = deepcopy(repo.authority), (len(repo.checkpoint_appends), repo.append_calls, len(repo.mutations))
    binding = {**_exact_binding("scan-2"), "canonical_domain": "example.test"}
    source_capture = {key: binding[key] for key in ("source_scan_id", "observation_hash", "capture_hash")}
    flow = authority_tests._Flow()
    monkeypatch.setattr(incremental_flow_adapter, "FlowSv9StrictComponentAdapter", lambda *_args, **_kwargs: flow)
    monkeypatch.setattr(scan_runner, "_execute_vault_operational_preparation", lambda **_kwargs: "operation")
    monkeypatch.setattr(
        scan_runner, "_activate_vault_result_unless_cancelled", lambda *_args, **_kwargs: {"created": False}
    )
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda *_args: None)
    if mode == "exact":
        unavailable = _assessment_report("scan-2", binding, unavailable=True)
        unavailable["url"] = "https://example.test"
        store.save_report(unavailable)
        monkeypatch.setattr(
            repo,
            "activate_evidence_vault_operational_scanner_result",
            lambda *_args, **_kwargs: {"created": False},
            raising=False,
        )
        monkeypatch.setattr(
            repo, "get_capture_operation_plan", lambda *_args, **_kwargs: {"status": "completed"}, raising=False
        )
        monkeypatch.setattr(
            orchestration,
            "prepare_vault_exact_resume",
            lambda **_kwargs: {
                "url": "https://example.test",
                "brand_name": "Example",
                "preparation": {},
                "canonical_snapshot": {},
                "report_binding": binding,
            },
        )
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    scan_runner._SCANS["scan-2"] = _status("scan-2")
    try:
        if mode == "ordinary":
            assert (
                scan_runner._run_vault_sv9_authority_scanner(
                    scan_id="scan-2",
                    url="https://example.test",
                    brand_name="Example",
                    repository=repo,
                    preparation={},
                    canonical_snapshot={},
                    canonical_source_capture=source_capture,
                    gate={},
                )
                is True
            )
            report_id = scan_runner._SCANS["scan-2"]["report_id"]
            assert report_id == "scan-2"
        else:
            scan_runner._SCANS.pop("scan-2")
            action = _exact_action("scan-2", "same-source-replay")
            result = scan_runner._run_vault_exact_resume(scan_id="scan-2", action=action, repository=repo)
            report_id = result.report_id
            assert result == scan_runner._ExactResumePublication(
                "publish_current", scan_runner._exact_successor_report_id(action)
            )
            published_bytes = (tmp_path / f"{report_id}.json").read_bytes()
            for _ in range(4):
                assert scan_runner._run_vault_exact_resume(scan_id="scan-2", action=action, repository=repo) == result
                assert (tmp_path / f"{report_id}.json").read_bytes() == published_bytes
        assert store.load_report(report_id)["score"] == accepted["score"]
        assert all((tmp_path / name).read_bytes() == value for name, value in before.items())
        assert repo.proof_reads and not flow.calls and repo.authority == accepted
        assert (len(repo.checkpoint_appends), repo.append_calls, len(repo.mutations)) == writes
    finally:
        scan_runner._SCANS.pop("scan-2", None)
        scan_runner._VAULT_ACTIVATIONS.discard("scan-2")
