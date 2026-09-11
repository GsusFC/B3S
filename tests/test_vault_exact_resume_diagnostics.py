from __future__ import annotations

import copy

from web import scan_runner


def _status() -> dict:
    return {
        "id": "scan-1",
        "state": "done",
        "phase": "done",
        "report_id": "scan-1",
        "diagnostic_operation_ledger": {
            "version": "scan-diagnostic-ledger-v1",
            "events": [],
            "dropped_event_count": 0,
            "truncated_event_count": 0,
        },
    }


def test_exact_resume_no_score_reason_is_attributed_to_action(monkeypatch):
    status = _status()
    persisted: list[dict] = []
    monkeypatch.setattr(scan_runner, "_load_persisted_scan_status", lambda _scan_id: copy.deepcopy(status))
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda value: persisted.append(value))

    scan_runner._record_exact_resume_diagnostic(
        "scan-1",
        action_id="action-1",
        operation="vault_authority_evaluation",
        stage="vault_authority",
        outcome="observed",
        coverage={"status": "no_new_score", "reason_codes": ["repository_failure"]},
    )

    assert persisted
    event = scan_runner.scan_diagnostic_dossier_from_status(persisted[-1])["events"][-1]
    assert event["action_id"] == "action-1"
    assert event["stage"] == "vault_authority"
    assert event["authority_result"] == {
        "status": "no_new_score",
        "reason_codes": ["repository_failure"],
    }
    assert event["reason_codes"] == ["repository_failure"]


def test_exact_resume_events_accumulate_in_cached_scan_status(monkeypatch):
    status = _status()
    persisted: list[dict] = []
    monkeypatch.setitem(scan_runner._SCANS, "scan-1", status)
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda value: persisted.append(copy.deepcopy(value)))

    for action_id in ("action-1", "action-2"):
        scan_runner._record_exact_resume_diagnostic(
            "scan-1",
            action_id=action_id,
            operation="vault_exact_resume",
            stage="report",
            outcome="completed",
        )

    events = scan_runner.scan_diagnostic_dossier_from_status(scan_runner._SCANS["scan-1"])["events"]
    assert [event["action_id"] for event in events] == ["action-1", "action-2"]
    assert [event["action_id"] for event in persisted[-1]["diagnostic_operation_ledger"]["events"]] == [
        "action-1",
        "action-2",
    ]


def test_exact_resume_error_captures_sanitized_exception_and_does_not_mutate_report(monkeypatch):
    status = _status()
    original_report = {"id": "scan-1", "score": {"value": None, "publishable": False}}
    report = copy.deepcopy(original_report)
    persisted: list[dict] = []
    monkeypatch.setattr(scan_runner, "_load_persisted_scan_status", lambda _scan_id: copy.deepcopy(status))
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda value: persisted.append(value))

    class DatabaseFailure(RuntimeError):
        sqlstate = "23505"

    scan_runner._record_exact_resume_diagnostic(
        "scan-1",
        action_id="action-2",
        operation="vault_authority_application",
        stage="vault_authority",
        outcome="failed",
        exc=DatabaseFailure("secret evidence and token must never escape"),
        reason_codes=["repository_failure"],
    )

    event = scan_runner.scan_diagnostic_dossier_from_status(persisted[-1])["events"][-1]
    assert event["action_id"] == "action-2"
    assert event["origin"] == {"exception_type": "DatabaseFailure", "sqlstate": "23505"}
    assert "secret evidence" not in str(event)
    assert report == original_report


def test_exact_resume_success_trace_and_unsafe_action_id_are_closed(monkeypatch):
    status = _status()
    persisted: list[dict] = []
    monkeypatch.setattr(scan_runner, "_load_persisted_scan_status", lambda _scan_id: copy.deepcopy(status))
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda value: persisted.append(value))

    scan_runner._record_exact_resume_diagnostic(
        "scan-1",
        action_id="action-3",
        operation="vault_exact_resume",
        stage="report",
        outcome="completed",
    )
    scan_runner._record_exact_resume_diagnostic(
        "scan-1",
        action_id="../secret",
        operation="vault_exact_resume",
        stage="report",
        outcome="completed",
    )

    dossier = scan_runner.scan_diagnostic_dossier_from_status(persisted[-1])
    assert dossier["exact_resume"] == {
        "supported": True,
        "reason": "action_trace_recorded",
        "action_id": "action-3",
        "observed_event_count": 1,
    }
    assert "../secret" not in str(dossier)


def test_existing_diagnostic_response_model_accepts_trace_extension():
    from web.api_v1.models import ScanDiagnosticDetailResponse

    payload = {
        "object": "scan_diagnostic_detail",
        "api_version": "v1",
        "scan_id": "scan-1",
        "available": True,
        "events": [
            {
                "operation": "vault_exact_resume",
                "stage": "report",
                "outcome": "completed",
                "observed_at": "2026-09-10T10:00:00+00:00",
                "durability": "observed_best_effort",
                "action_id": "action-1",
            }
        ],
        "exact_resume": {
            "supported": True,
            "reason": "action_trace_recorded",
            "action_id": "action-1",
        },
        "links": {},
    }

    model = ScanDiagnosticDetailResponse.model_validate(payload)
    assert model.events[0]["action_id"] == "action-1"
    assert model.exact_resume["action_id"] == "action-1"
