from __future__ import annotations
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
    monkeypatch.setattr(relations, "project_evidence_vault_sv9_authoritative_relations", lambda **_k: {"status": "available", "authoritative_relations": [{"evidence_ref": "one", "evidence_fingerprint": "f"}, {"evidence_ref": "one", "evidence_fingerprint": "f"}]})
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
            assert kwargs["current_evidence"] == [{"evidence_ref": "one", "evidence_fingerprint": "f"}] and kwargs["trusted_irrelevant_evidence"] == [] and activations == ["p"]
            assert scan_runner.cancel_scan(scan_id)["reason"] == "vault_activation_in_progress"
            return {"authority": {"accepted_candidate": {"source_scan_id": "prior" if action == "retain_source" else scan_id}}}
        monkeypatch.setattr(application, "run_evidence_vault_sv9_authority_application", apply)
        monkeypatch.setattr(publication, "project_vault_authority_publication", lambda _app, _current, resolved: sources.append(resolved) or {"action": action, "source_report_id": "prior" if action == "retain_source" else None, "scanner_payload": None if action == "retain_source" else {"sv9": {"brand3_score": None if action == "record_no_score" else 7}}})
        assert scan_runner._run_vault_sv9_authority_scanner(scan_id=scan_id, url="https://example.test", brand_name="Example", repository=Repo(), preparation={"operation_plan": {"operation_plan_fingerprint": "p", "operations": {"llm_required": False}}} if action != "record_no_score" else {"resume": {"analysis_status": "completed", "operation_plan_fingerprint": "p", "semantic_work_completed": False}}, canonical_snapshot={"raw_inputs": [], "acquisition_gate": {"state": "pass"}}, canonical_source_capture=None, gate={"state": "pass"}) is True
        assert scan_runner._SCANS[scan_id]["report_id"] == ("prior" if action == "retain_source" else scan_id) and (saved == [] if action == "retain_source" else len(saved) == 1) and validated == ([] if action == "retain_source" else [True]) and sources == ([source] if action == "retain_source" else [None]) and scan_id not in scan_runner._VAULT_ACTIVATIONS
        scan_runner._SCANS.pop(scan_id, None); scan_runner._SCAN_EVENTS.pop(scan_id, None)


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
