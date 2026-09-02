from __future__ import annotations

import pytest

from web import scan_runner
from web.api_v1 import presenters, service


def _status(scan_id: str) -> dict:
    return {"id": scan_id, "url": "https://example.test", "brand_name": "Example", "state": "running", "phase": "capture", "phases": [{"key": key, "state": "pending"} for key in ("capture", "interpret", "score", "report")], "acquisition_gate": {}, "completed_at": None}


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
    monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **kwargs: scan_runner._publish_exact_report(scan_id, kwargs["exact_owner"], {"id": scan_id}, binding, "publish_current")); expect(); assert reads == [1] * 3

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
    calls, loaded, reports, loader = [], [], iter(({"id": "pre"}, None, {"id": "recovered"}, {"id": "post"})), scan_runner._load_report_for_exact_owner
    def fenced(source, token, report_id): calls.append((source, report_id, scan_runner._exact_owner_current(source, token))); return loader(source, token, report_id)
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_k: prepared); monkeypatch.setattr(scan_runner, "load_report", lambda report_id: loaded.append(report_id) or next(reports)); monkeypatch.setattr(scan_runner, "_load_report_for_exact_owner", fenced); monkeypatch.setattr(scan_runner, "_validate_exact_current_report", lambda *_a, **_k: {})
    try:
        monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **_k: (_ for _ in ()).throw(AssertionError("authority replay")))
        assert scan_runner._run_vault_exact_resume(scan_id=scan_id, action={}, repository=object()).action == "publish_current"
        monkeypatch.setattr(scan_runner, "_run_vault_sv9_authority_scanner", lambda **_k: (_ for _ in ()).throw(RuntimeError("execution")))
        assert scan_runner._run_vault_exact_resume(scan_id=scan_id, action={}, repository=object()).action == "publish_current"
        owner = scan_runner._acquire_scan_owner("post-save", "exact_resume"); monkeypatch.setattr(scan_runner, "save_report", lambda _r: None)
        assert scan_runner._publish_exact_report("post-save", owner, {"id": "post-save"}, binding, "publish_current").action == "publish_current"
        assert scan_runner._release_scan_owner(owner); owner = scan_runner._acquire_scan_owner("post-loss", "exact_resume"); monkeypatch.setattr(scan_runner, "save_report", lambda _r: scan_runner._release_scan_owner(owner))
        with pytest.raises(scan_runner._ExactResumeFailure, match="busy"): scan_runner._publish_exact_report("post-loss", owner, {"id": "post-loss"}, binding, "publish_current")
        assert loaded == [scan_id, scan_id, scan_id, "post-save"] and [(source, report) for source, report, _current in calls] == [(scan_id, scan_id), (scan_id, scan_id), (scan_id, scan_id), ("post-save", "post-save"), ("post-loss", "post-loss")] and all(current for *_rest, current in calls[:-1])
    finally: scan_runner._release_scan_owner(owner)
