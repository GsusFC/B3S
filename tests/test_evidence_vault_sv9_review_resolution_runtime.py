from __future__ import annotations

from uuid import uuid4

import pytest

import src.history.repository as repository_module
from src.history.repository import PostgresHistoryRepository


SHA = "a" * 64


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, *, existing=None):
        self.existing = existing
        self.statements: list[str] = []
        self.sequence: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=()):
        compact = " ".join(str(sql).split())
        self.statements.append(compact)
        if "resolution_key_hash = %s" in compact:
            return _Cursor(self.existing)
        if "candidate_id = %s" in compact and "review_resolutions" in compact:
            return _Cursor(self.existing)
        if "candidate_id = %s" in compact and "authority_events" in compact:
            return _Cursor(None)
        return _Cursor()


def _context():
    return {
        "workspace_id": str(uuid4()),
        "brand_id": str(uuid4()),
        "scan_run_id": str(uuid4()),
        "source_scan_id": "rescan-001",
        "capture_id": str(uuid4()),
        "operation_plan_id": str(uuid4()),
    }


def _candidate(context):
    return {
        "id": str(uuid4()),
        "source_scan_id": context["source_scan_id"],
        "schema_version": "evidence-vault-sv9-judgment-candidate-v2",
        "complete_record_fingerprint": SHA,
        "canonical_plan_fingerprint": SHA,
        "current_series_fingerprint": SHA,
        "candidate_series_fingerprint": SHA,
        "evaluation_bundle_fingerprint": SHA,
        "assessment_fingerprint": SHA,
        "score_fingerprint": SHA,
    }


def _request(context, candidate, *, decision):
    event_id = str(uuid4())
    active_id = str(uuid4())
    reopen_id = str(uuid4())
    values = {
        "schema_version": "evidence-vault-sv9-judgment-review-resolution-v1",
        "id": str(uuid4()),
        "workspace_id": context["workspace_id"],
        "brand_id": context["brand_id"],
        "candidate_id": candidate["id"],
        "scan_run_id": context["scan_run_id"],
        "capture_id": context["capture_id"],
        "operation_plan_id": context["operation_plan_id"],
        "source_scan_id": context["source_scan_id"],
        "candidate_schema_version": candidate["schema_version"],
        "candidate_complete_record_fingerprint": SHA,
        "canonical_plan_fingerprint": SHA,
        "current_series_fingerprint": SHA,
        "candidate_series_fingerprint": SHA,
        "evaluation_bundle_fingerprint": SHA,
        "assessment_fingerprint": SHA,
        "score_fingerprint": SHA,
        "decision": decision,
        "expected_authority_event_id": reopen_id,
        "expected_authority_event_fingerprint": SHA,
        "expected_authority_event_sequence": 3,
        "active_authority_event_id": active_id,
        "active_authority_event_fingerprint": SHA,
        "active_authority_event_sequence": 2,
        "evaluated_authority_event_id": active_id,
        "evaluated_authority_event_fingerprint": SHA,
        "evaluated_authority_sequence": 2,
        "reopen_authority_event_id": reopen_id,
        "reopen_authority_event_fingerprint": SHA,
        "reopen_authority_event_sequence": 3,
        "reopen_active_parent_event_id": active_id,
        "reopen_active_parent_event_fingerprint": SHA,
        "reopen_active_parent_event_sequence": 2,
        "reopen_delta_fingerprint": SHA,
        "reopen_review_overlay_fingerprint": SHA,
        "successor_authority_event_id": None,
        "successor_authority_event_fingerprint": None,
        "successor_authority_event_sequence": None,
        "resolution_key_hash": SHA,
        "created_at": "2026-09-21T00:00:00+00:00",
    }
    return values | ({
        "successor_authority_event_id": event_id,
        "successor_authority_event_fingerprint": SHA,
        "successor_authority_event_sequence": 4,
    } if decision == "approve" else {})


def _bind_request_to_state(request, state):
    head, active = state["head"], state["active"]
    return request | {
        "expected_authority_event_id": head["id"],
        "expected_authority_event_fingerprint": head["event_fingerprint"],
        "expected_authority_event_sequence": head["sequence"],
        "active_authority_event_id": active["id"],
        "active_authority_event_fingerprint": active["event_fingerprint"],
        "active_authority_event_sequence": active["sequence"],
        "evaluated_authority_event_id": active["id"],
        "evaluated_authority_event_fingerprint": active["event_fingerprint"],
        "evaluated_authority_sequence": active["sequence"],
        "reopen_authority_event_id": head["id"],
        "reopen_authority_event_fingerprint": head["event_fingerprint"],
        "reopen_authority_event_sequence": head["sequence"],
        "reopen_active_parent_event_id": active["id"],
        "reopen_active_parent_event_fingerprint": active["event_fingerprint"],
        "reopen_active_parent_event_sequence": active["sequence"],
    }


@pytest.fixture
def runtime_harness(monkeypatch):
    context = _context()
    candidate = _candidate(context)
    row = context | {
        "source_scan_id": context["source_scan_id"],
        "scan_run_id": context["scan_run_id"],
        "capture_id": context["capture_id"],
        "operation_plan_id": context["operation_plan_id"],
    }
    active = {
        "id": str(uuid4()),
        "event_type": "adopt",
        "event_fingerprint": SHA,
        "sequence": 2,
        "candidate": candidate,
    }
    head = {
        "id": str(uuid4()),
        "event_type": "reopen",
        "event_fingerprint": SHA,
        "sequence": 3,
        "active_parent_event_id": active["id"],
        "active_parent_event_fingerprint": SHA,
        "delta_fingerprint": SHA,
        "event_payload": {
            "schema_version": "evidence-vault-sv9-judgment-authority-event-v1",
            "request": {},
            "review_state": "pending",
            "signed_delta": {},
        },
    }
    state = {"head": head, "active": active, "candidate": candidate, "overlay": {}}
    connection = _Connection()
    appended: list[tuple[str, object]] = []

    monkeypatch.setattr(PostgresHistoryRepository, "_ensure_migrated", lambda _self: None)
    monkeypatch.setattr(repository_module, "_verify_exact_migration_head_under_shared_lock", lambda _conn: None)
    monkeypatch.setattr(repository_module, "_sv9_judgment_context", lambda *_args: context)
    monkeypatch.setattr(repository_module, "_advisory_lock_key", lambda *_args: 1)
    monkeypatch.setattr(repository_module, "_sv9_authority_candidate", lambda *_args: (candidate, row))
    monkeypatch.setattr(repository_module, "_sv9_review_resolution_record", lambda value: dict(value))
    monkeypatch.setattr(repository_module.review_resolution, "validate_evidence_vault_sv9_review_resolution", lambda value, **_kwargs: dict(value))

    def replay(*_args):
        return state

    monkeypatch.setattr(repository_module, "_replay_sv9_judgment_authority", replay)
    monkeypatch.setattr(repository_module.review_resolution, "review_overlay_fingerprint", lambda _value: SHA)
    monkeypatch.setattr(
        repository_module,
        "_project_sv9_judgment_authority",
        lambda _state, _event=None: {"authority": True},
    )

    def append_event(_conn, event):
        appended.append(("event", event))
        state["events"] = {event["id"]: event}

    def append_resolution(_conn, resolution):
        appended.append(("resolution", resolution))
        connection.existing = dict(resolution)
        return resolution

    monkeypatch.setattr(repository_module, "_append_sv9_judgment_authority_event", append_event)
    monkeypatch.setattr(repository_module, "_append_sv9_review_resolution", append_resolution)
    monkeypatch.setattr(repository_module, "_sv9_authority_event_public", lambda event: dict(event))
    monkeypatch.setattr(repository_module, "_sv9_authority_event", lambda *_args, **kwargs: {
        "id": kwargs.get("event_id", str(uuid4())),
        "event_type": "supersede",
        "sequence": 4,
        "event_fingerprint": SHA,
        "candidate": candidate,
        "request": {},
    })
    repo = PostgresHistoryRepository(
        "postgresql://unused", connect=lambda *_args, **_kwargs: connection
    )
    return repo, context, candidate, state, connection, appended


def test_approve_appends_successor_before_resolution(runtime_harness, monkeypatch):
    repo, context, candidate, state, connection, appended = runtime_harness
    successor_id = str(uuid4())
    request = _bind_request_to_state(
        _request(context, candidate, decision="approve"), state
    )
    request = request | {
        "successor_authority_event_id": successor_id,
        "successor_authority_event_fingerprint": SHA,
        "successor_authority_event_sequence": 4,
    }

    def event_factory(*_args, **_kwargs):
        return {
            "id": successor_id,
            "event_type": "supersede",
            "sequence": 4,
            "event_fingerprint": SHA,
            "candidate": candidate,
            "request": {},
        }

    monkeypatch.setattr(repository_module, "_sv9_authority_event", event_factory)
    result, replayed = repo.resolve_evidence_vault_sv9_judgment_review(request)

    assert replayed is False
    assert result["successor"]["id"] == successor_id
    assert [kind for kind, _value in appended] == ["event", "resolution"]
    assert connection.existing["decision"] == "approve"


def test_reject_does_not_append_authority_successor(runtime_harness):
    repo, context, candidate, _state, _connection, appended = runtime_harness
    request = _bind_request_to_state(
        _request(context, candidate, decision="reject"), _state
    )

    result, replayed = repo.resolve_evidence_vault_sv9_judgment_review(request)

    assert replayed is False
    assert result["successor"] is None
    assert [kind for kind, _value in appended] == ["resolution"]


def test_existing_resolution_is_idempotent(runtime_harness):
    repo, context, candidate, state, connection, appended = runtime_harness
    request = _bind_request_to_state(
        _request(context, candidate, decision="reject"), state
    )
    connection.existing = dict(request)

    result, replayed = repo.resolve_evidence_vault_sv9_judgment_review(request)

    assert replayed is True
    assert result["resolution"] == request
    assert appended == []


def test_candidate_must_belong_to_source_scan_context(runtime_harness, monkeypatch):
    repo, context, candidate, _state, _connection, _appended = runtime_harness
    request = _bind_request_to_state(
        _request(context, candidate, decision="reject"), _state
    )

    original = repository_module._sv9_authority_candidate

    def wrong_context(*args):
        value, row = original(*args)
        return value, row | {"source_scan_id": "another-scan"}

    # The runtime must reject a candidate whose row binding crosses the request context.
    from src.history.repository import EvidenceVaultSv9JudgmentCandidateConflictError

    monkeypatch.setattr(repository_module, "_sv9_authority_candidate", wrong_context)
    with pytest.raises(EvidenceVaultSv9JudgmentCandidateConflictError):
        repo.resolve_evidence_vault_sv9_judgment_review(request)
