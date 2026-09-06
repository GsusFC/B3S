"""Composed replays of existing Vault paths; no live providers or new stores."""
from copy import deepcopy

import pytest

from src.services import evidence_vault_scan_orchestration as orchestration
from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan
from src.sv9 import incremental_flow_adapter
from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository
from tests.test_evidence_vault_sv9_authority_evaluation import _Flow
from web import report_store, scan_runner


@pytest.fixture
def exact_replay(monkeypatch, tmp_path):
    """Start at an existing completed operation, using the existing test store."""
    scan = "stabilization-replay"
    repo = _ApplicationRepository(records=(9,))
    operation_plan = build_vault_scan_plan(
        brand_identity="example.test", subject_url="https://example.test",
        mode="incremental_refresh", canonical_memory_version=f"{repo.witness_seed:064x}",
        current_evidence_records=[{
            "ref": "evidence:9", "source": "web", "evidence_type": "owned_content",
            "url": "https://example.test", "content": "Frozen checkpoint evidence", "metadata": {},
        }],
    )
    repo.context["operation_origin"]["operation_fingerprint"] = operation_plan["operation_plan_fingerprint"]
    load_facts = repo.load_evidence_vault_sv9_authoritative_relation_facts

    def operation_facts(*args, **kwargs):
        facts = load_facts(*args, **kwargs)
        facts["source"]["operation_fingerprint"] = operation_plan["operation_plan_fingerprint"]
        return facts

    monkeypatch.setattr(repo, "load_evidence_vault_sv9_authoritative_relation_facts", operation_facts)
    binding = {
        "source_scan_id": scan,
        "source_run_id": "acquisition-run",
        "observation_hash": "observation-hash",
        "capture_hash": "capture-hash",
        "canonical_domain": "example.test",
    }
    prepared = {
        "url": "https://example.test", "brand_name": "Example",
        "report_binding": binding, "canonical_snapshot": {},
        "preparation": {"resume": {
            "analysis_status": "completed",
            "operation_plan_fingerprint": repo.context["operation_origin"]["operation_fingerprint"],
        }},
    }
    monkeypatch.setattr(orchestration, "prepare_vault_exact_resume", lambda **_: deepcopy(prepared))
    # Reuse the application fixture; these are the only additional completed-
    # operation methods required by the real scanner orchestration.
    monkeypatch.setattr(repo, "activate_evidence_vault_operational_scanner_result", lambda *a, **k: {"created": False}, raising=False)
    monkeypatch.setattr(repo, "get_capture_operation_plan", lambda source, **kwargs: {
        "source_scan_id": source, "status": "completed", "plan": deepcopy(operation_plan),
        "capture_id": repo.context["capture_origin"]["capture_id"],
        "capture_hash": repo.context["capture_origin"]["capture_fingerprint"],
        "operation_plan_id": repo.context["operation_origin"]["operation_id"],
        "operation_plan_fingerprint": operation_plan["operation_plan_fingerprint"],
    }, raising=False)
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(scan_runner, "save_report", report_store.save_report)
    monkeypatch.setattr(scan_runner, "load_report", report_store.load_report)
    action = {"action_id": "stabilization-action", "scan_id": scan, "state": "running"}

    def run(flow):
        monkeypatch.setattr(incremental_flow_adapter, "FlowSv9StrictComponentAdapter", lambda *a, **k: flow)
        return scan_runner._run_vault_exact_resume(scan_id=scan, action=action, repository=repo)

    return scan, repo, run, tmp_path


@pytest.mark.parametrize("complete", (False, True))
def test_composed_resume_preserves_source_and_finishes(exact_replay, complete):
    scan, repo, run, directory = exact_replay
    interrupted_flow = _Flow(fail=2)
    first = run(interrupted_flow)
    assert first == scan_runner._ExactResumePublication("record_no_score", scan)
    assert len(interrupted_flow.calls) == 2 and len(repo.checkpoint_appends) == 1
    before = (directory / f"{scan}.json").read_bytes()
    resumed_flow = _Flow(fail=None if complete else 1)
    resumed = run(resumed_flow)
    assert (directory / f"{scan}.json").read_bytes() == before
    assert resumed.action == ("publish_current" if complete else "record_no_score")
    if complete:
        assert resumed.report_id != scan
        successor = report_store.load_report(resumed.report_id)
        assert successor["raw"]["source_run_id"] == scan
        assert successor["sv9_assessment"]["availability"] == "available"
        assert len(resumed_flow.calls) == 9 and len(repo.checkpoint_appends) == 10
        no_call = _Flow(fail=1)
        assert run(no_call) == resumed and not no_call.calls
    else:
        assert resumed.report_id == scan
        assert len(resumed_flow.calls) == 1 and len(repo.checkpoint_appends) == 1


def test_resume_recovers_adopted_authority_after_report_write_failure(exact_replay, monkeypatch):
    scan, repo, run, directory = exact_replay
    run(_Flow(fail=2))
    before = (directory / f"{scan}.json").read_bytes()
    with monkeypatch.context() as patch:
        def unavailable(_report):
            raise OSError("final report storage unavailable")
        patch.setattr(scan_runner, "save_report", unavailable)
        with pytest.raises(orchestration.VaultExactResumeError):
            run(_Flow())
    assert repo.authority is not None and len(repo.checkpoints) == 10
    assert (directory / f"{scan}.json").read_bytes() == before
    no_call = _Flow(fail=1)
    recovered = run(no_call)
    assert not no_call.calls
    assert recovered.action == "publish_current" and recovered.report_id != scan
    assert report_store.load_report(recovered.report_id)["sv9_assessment"]["availability"] == "available"


def test_later_scan_retains_published_successor(exact_replay):
    from src.services import evidence_vault_sv9_authority_application as application
    from src.services.evidence_vault_sv9_authority_report import project_vault_authority_publication

    scan, repo, run, _directory = exact_replay
    run(_Flow(fail=2))
    successor = run(_Flow())
    no_call = _Flow(fail=1)
    retained = application.run_evidence_vault_sv9_authority_application(
        repository=repo, flow=no_call, domain_or_url="example.test",
        source_scan_id="next-scan",
        current_series_contract=repo.authority["accepted_candidate"]["plan"]["current_series_contract"],
    )
    assert retained["status"] == "authority_retained" and not no_call.calls
    source = scan_runner._accepted_authority_source_report(retained, "next-scan", domain_or_url="example.test")
    publication = project_vault_authority_publication(retained, "next-scan", source)
    assert publication == {"action": "retain_source", "source_report_id": successor.report_id, "scanner_payload": None}
    assert report_store.load_report(scan)["sv9_assessment"]["availability"] == "unavailable"


def test_report_read_failure_is_not_confirmed_absence(monkeypatch, tmp_path):
    class UnavailableRepository:
        def get_report_payload(self, _report_id):
            raise OSError("history read failed")

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: UnavailableRepository())
    with pytest.raises(OSError, match="history read failed"):
        report_store.load_report("no-local-fallback")


@pytest.mark.parametrize("after_persist", (False, True))
def test_process_interruption_at_checkpoint_boundary(exact_replay, monkeypatch, after_persist):
    scan, repo, run, directory = exact_replay
    persist = repo.append_evidence_vault_sv9_evaluation_checkpoint
    flow = _Flow()

    def interrupted(*args, **kwargs):
        if after_persist:
            persist(*args, **kwargs)
        raise KeyboardInterrupt("process interrupted at durable boundary")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "append_evidence_vault_sv9_evaluation_checkpoint", interrupted)
        with pytest.raises(KeyboardInterrupt):
            run(flow)
    assert len(flow.calls) == 1 and len(repo.checkpoints) == int(after_persist)
    assert not (directory / f"{scan}.json").exists()
    assert scan not in scan_runner._SCAN_OWNERS
    resumed_flow = _Flow()
    publication = run(resumed_flow)
    assert publication.action == "publish_current"
    assert len(resumed_flow.calls) == 10 - int(after_persist)
    assert len(repo.checkpoints) == 10


def test_checkpoint_persistence_failure_does_not_advance(exact_replay):
    _scan, repo, run, _directory = exact_replay
    repo.fail_checkpoint_append = True
    flow = _Flow()
    assert run(flow).action == "record_no_score"
    assert len(flow.calls) == 1 and not repo.checkpoints and not repo.candidates
    assert repo.authority is None
    repo.fail_checkpoint_append = False
    resumed = _Flow()
    assert run(resumed).action == "publish_current"
    assert len(resumed.calls) == 10 and len(repo.checkpoints) == 10


def test_accept_rejection_never_creates_checkpoint(exact_replay):
    _scan, repo, run, _directory = exact_replay
    malformed = _Flow(malformed=True)
    assert run(malformed).action == "record_no_score"
    assert len(malformed.calls) == 1
    assert not repo.checkpoints and not repo.candidates and repo.authority is None


def test_persisted_checkpoint_is_reaccepted_not_trusted_blindly(exact_replay):
    _scan, repo, run, _directory = exact_replay
    assert run(_Flow(fail=2)).action == "record_no_score"
    checkpoint = next(iter(repo.checkpoints.values()))
    checkpoint["request_fingerprint"] = "0" * 64
    no_call = _Flow()
    assert run(no_call).action == "record_no_score"
    assert not no_call.calls and not repo.candidates and repo.authority is None


def test_report_read_error_can_use_validated_durable_file(monkeypatch, tmp_path):
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: None)
    report = {"id": "local-durable-fallback", "url": "https://example.test"}
    report_store.save_report(report)

    class UnavailableRepository:
        def get_report_payload(self, _report_id):
            raise OSError("history read failed")

    monkeypatch.setattr(report_store, "_postgres_repository", lambda: UnavailableRepository())
    assert report_store.load_report(report["id"]) == report


@pytest.mark.parametrize("database_configured", (False, True))
def test_explicit_local_store_can_confirm_absence(monkeypatch, tmp_path, database_configured):
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("B3S_DATABASE_URL", "postgresql://unavailable" if database_configured else "")
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: None)
    assert report_store.load_report("absent") is None


@pytest.mark.parametrize("field", ("id", "source_run_id", "capture_hash", "score", "assessment_fingerprint"))
def test_later_scan_does_not_retain_an_unbound_successor(exact_replay, monkeypatch, field):
    from src.services import evidence_vault_sv9_authority_application as application
    from src.services.evidence_vault_sv9_authority_report import project_vault_authority_publication

    scan, repo, run, _directory = exact_replay
    run(_Flow(fail=2))
    successor = run(_Flow())
    retained = application.run_evidence_vault_sv9_authority_application(
        repository=repo, flow=_Flow(fail=1), domain_or_url="example.test",
        source_scan_id="next-scan",
        current_series_contract=repo.authority["accepted_candidate"]["plan"]["current_series_contract"],
    )
    report = deepcopy(report_store.load_report(successor.report_id))
    if field == "source_run_id":
        report["raw"][field] = "another-capture"
    elif field == "capture_hash":
        report["raw"]["source_capture"][field] = "tampered"
    elif field == "assessment_fingerprint":
        report["sv9_assessment"][field] = "0" * 64
    else:
        report[field] = "exact-resume-invalid" if field == "id" else 99
    monkeypatch.setattr(scan_runner, "list_reports_for_domain", lambda _domain: [report])
    source = scan_runner._accepted_authority_source_report(retained, "next-scan", domain_or_url="example.test")
    assert source["id"] == scan
    assert project_vault_authority_publication(retained, "next-scan", source)["action"] == "record_no_score"


def test_uninitialized_required_history_cannot_confirm_absence(monkeypatch, tmp_path):
    monkeypatch.setenv("B3S_DATABASE_URL", "postgresql://unavailable")
    monkeypatch.delenv("B3S_REPORTS_DIR", raising=False)
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: None)
    monkeypatch.setattr(report_store, "report_path", lambda _id: tmp_path / "absent.json")
    with pytest.raises(RuntimeError, match="history unavailable"):
        report_store.load_report("absent")


def test_successful_history_read_can_confirm_absence(monkeypatch, tmp_path):
    class AvailableRepository:
        def get_report_payload(self, _report_id):
            return None

    monkeypatch.setenv("B3S_DATABASE_URL", "postgresql://configured")
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: AvailableRepository())
    assert report_store.load_report("absent") is None


def test_resume_successor_is_readable_through_api_and_public_report(exact_replay, monkeypatch, tmp_path):
    """Follow the public resume API through real SQLite and report readers."""
    from fastapi.testclient import TestClient

    from src.storage.sqlite_store import SQLiteStore
    from tests.test_scanner_api_v1 import AUTH, _InlineThread, _enable_resume_api
    from web import exact_resume_controller
    from web.api_v1 import service
    from web.app import app

    scan, repository, run, directory = exact_replay
    assert run(_Flow(fail=2)).action == "record_no_score"
    original_bytes = (directory / f"{scan}.json").read_bytes()
    database_path = tmp_path / "resume-api.sqlite3"
    _enable_resume_api(monkeypatch, database_path, repository)
    monkeypatch.setattr(
        service, "launch_vault_exact_resume_action",
        lambda **kwargs: exact_resume_controller.launch_vault_exact_resume_action(
            **kwargs, thread_factory=_InlineThread,
        ),
    )
    flow = _Flow()
    monkeypatch.setattr(incremental_flow_adapter, "FlowSv9StrictComponentAdapter", lambda *a, **k: flow)
    client = TestClient(app)
    original = client.get(f"/api/v1/scans/{scan}/result", headers=AUTH)
    assert original.status_code == 200
    assert original.json()["score"]["publishable"] is False
    assert original.json()["score"]["value"] is None
    headers = {**AUTH, "Idempotency-Key": "composed-api-resume"}
    response = client.post(f"/api/v1/scans/{scan}/resume", headers=headers)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["state"] == "completed", payload
    successor_id = payload["result"]["report_id"]
    assert successor_id != scan and payload["result"]["publication_action"] == "publish_current"

    store = SQLiteStore(str(database_path))
    try:
        action = store.get_scanner_resume_action(action_id=payload["action_id"])
    finally:
        store.close()
    assert action["state"] == "completed" and action["status_payload"]["report_id"] == successor_id
    # Fresh clients and storage connections, not the API's original response.
    reader = TestClient(app)
    status = reader.get(response.headers["location"], headers=AUTH)
    assert status.status_code == 200 and status.json()["result"] == payload["result"]
    successor = reader.get(f"/api/v1/scans/{successor_id}/result", headers=AUTH)
    assert successor.status_code == 200, successor.text
    assert successor.json()["id"] == successor_id
    assert successor.json()["score"]["publishable"] is True
    assert successor.json()["score"]["value"] == report_store.load_report(successor_id)["score"]
    assert successor.headers["etag"] != original.headers["etag"]
    assert reader.get(f"/api/v1/scans/{successor_id}/result",
                      headers={**AUTH, "If-None-Match": successor.headers["etag"]}).status_code == 304
    assert reader.get(f"/api/v1/scans/{scan}/result",
                      headers={**AUTH, "If-None-Match": original.headers["etag"]}).status_code == 304
    assert reader.get(f"/report/{successor_id}", follow_redirects=False).status_code == 200
    assert reader.get(f"/report/{successor_id}.md").status_code == 200
    assert report_store.load_report(successor_id)["raw"]["source_run_id"] == scan
    assert (directory / f"{scan}.json").read_bytes() == original_bytes
    assert len(flow.calls) == 9 and len(repository.checkpoints) == 10
    no_call = _Flow(fail=1)
    monkeypatch.setattr(incremental_flow_adapter, "FlowSv9StrictComponentAdapter", lambda *a, **k: no_call)
    repeated = reader.post(f"/api/v1/scans/{scan}/resume", headers=headers)
    assert repeated.status_code == 202 and repeated.headers["idempotent-replayed"] == "true"
    assert repeated.json()["action_id"] == payload["action_id"]
    assert repeated.json()["result"] == payload["result"] and not no_call.calls


@pytest.mark.parametrize("all_finalization_writes_fail", (False, True))
def test_api_report_survives_action_finalization_failure(exact_replay, monkeypatch, tmp_path, all_finalization_writes_fail):
    """A saved report is not a completed action until SQLite records completion."""
    from fastapi.testclient import TestClient

    from src.storage.sqlite_store import SQLiteStore
    from tests.test_scanner_api_v1 import AUTH, _InlineThread, _enable_resume_api
    from web import exact_resume_controller
    from web.api_v1 import service
    from web.app import app

    scan, repository, run, directory = exact_replay
    assert run(_Flow(fail=2)).action == "record_no_score"
    original_bytes = (directory / f"{scan}.json").read_bytes()
    database_path = tmp_path / "finalization.sqlite3"
    _enable_resume_api(monkeypatch, database_path, repository)
    monkeypatch.setattr(
        service, "launch_vault_exact_resume_action",
        lambda **kwargs: exact_resume_controller.launch_vault_exact_resume_action(
            **kwargs, thread_factory=_InlineThread,
        ),
    )
    flow = _Flow()
    monkeypatch.setattr(incremental_flow_adapter, "FlowSv9StrictComponentAdapter", lambda *a, **k: flow)
    finalize = SQLiteStore.finalize_scanner_resume_action

    def unavailable(store, **kwargs):
        if all_finalization_writes_fail or kwargs["state"] == "completed":
            raise OSError("injected action-finalization storage failure")
        return finalize(store, **kwargs)

    client = TestClient(app)
    headers = {**AUTH, "Idempotency-Key": "finalization-first"}
    with monkeypatch.context() as fault:
        fault.setattr(SQLiteStore, "finalize_scanner_resume_action", unavailable)
        response = client.post(f"/api/v1/scans/{scan}/resume", headers=headers)
    assert response.status_code == 202, response.text
    first = response.json()
    assert first["state"] == ("running" if all_finalization_writes_fail else "failed")
    assert first["result"] is None
    successor_id = scan_runner._exact_successor_report_id(first)
    persisted = report_store.load_report(successor_id)
    assert persisted["sv9_assessment"]["availability"] == "available"
    assert len(flow.calls) == 9 and len(repository.checkpoints) == 10

    if all_finalization_writes_fail:
        assert exact_resume_controller.recover_interrupted_vault_exact_resume_actions(
            database_path=str(database_path),
        ) == 1
    status = TestClient(app).get(response.headers["location"], headers=AUTH).json()
    assert status["state"] == ("interrupted" if all_finalization_writes_fail else "failed")
    assert status["result"] is None and status["failure"]["retryable"] is True
    no_call = _Flow(fail=1)
    monkeypatch.setattr(incremental_flow_adapter, "FlowSv9StrictComponentAdapter", lambda *a, **k: no_call)
    same_request = client.post(f"/api/v1/scans/{scan}/resume", headers=headers)
    assert same_request.headers["idempotent-replayed"] == "true"
    assert same_request.json()["state"] == status["state"] and not no_call.calls
    # The existing contract uses a new key for a new action after a failed one.
    retried = client.post(f"/api/v1/scans/{scan}/resume",
                          headers={**AUTH, "Idempotency-Key": "finalization-retry"})
    assert retried.status_code == 202 and retried.json()["state"] == "completed", retried.text
    retry_report = report_store.load_report(retried.json()["result"]["report_id"])
    assert retry_report["sv9_assessment"] == persisted["sv9_assessment"]
    assert retry_report["raw"]["source_run_id"] == scan
    assert report_store.load_report(successor_id) == persisted
    assert (directory / f"{scan}.json").read_bytes() == original_bytes
    assert not no_call.calls and len(repository.checkpoints) == 10
