from contextlib import contextmanager
from copy import deepcopy

import pytest

from src.history import repository as history
from src.services import evidence_vault_sv9_authority_evaluation as service
from src.services.evidence_vault_sv9_authoritative_relations import project_evidence_vault_sv9_evaluation_input
from src.sv9 import incremental_evaluation as evaluation
from tests.test_evidence_vault_accepted_authority_reconstruction import _fixture, _Connection
from tests._harness_sv9_evaluation import _Flow, _series


def _repo(monkeypatch, *, accepted=True, mutate=None, chain=None):
    packet_row, evidence_rows, candidate, context, candidate_context, _ = _fixture()
    valid = {
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "brand_id": "00000000-0000-0000-0000-000000000002",
        "scan_run_id": "00000000-0000-0000-0000-000000000003",
    }
    context = {**context, **valid, "source_scan_id": "current-scan", "canonical_domain": "example.com", "workspace_slug": "b3s"}
    candidate_context = {**candidate_context, **valid, "source_scan_id": "historical-scan", "canonical_domain": "example.com", "workspace_slug": "b3s"}
    if mutate:
        mutate(evidence_rows)
    connection = _Connection(packet_row, evidence_rows)

    @contextmanager
    def connect():
        yield connection

    repo = history.PostgresHistoryRepository("fake", schema_policy="verify_head")
    repo._migrated = True
    repo._connect = connect
    monkeypatch.setattr(history, "_verify_exact_migration_head_under_shared_lock", lambda conn: None)
    monkeypatch.setattr(history, "_vault_operation_row", lambda *args, **kwargs: {"row": True})
    monkeypatch.setattr(history, "_vault_operation_plan_record", lambda row: {"status": "not_required", "result_payload": None})

    def context_for_scan(conn, source_scan_id, workspace_slug, for_update):
        return {**(candidate_context if source_scan_id == "historical-scan" else context), "source_scan_id": str(source_scan_id), "workspace_slug": workspace_slug}

    monkeypatch.setattr(history, "_sv9_judgment_context", context_for_scan)
    monkeypatch.setattr(history, "_replay_sv9_judgment_authority", lambda *args, **kwargs: {"candidate": candidate} if accepted else None)
    if chain is not None:
        monkeypatch.setattr(history, "_project_vault_operational_memory_authority_chain", lambda *args, **kwargs: chain)
    else:
        monkeypatch.setattr(history, "_project_vault_operational_memory_authority_chain", lambda *args, **kwargs: [])

    # The evaluator uses the same concrete repository object; only persistence
    # operations after input projection are bounded test doubles.
    monkeypatch.setattr(repo, "get_evidence_vault_sv9_judgment_authority", lambda *args, **kwargs: None)
    monkeypatch.setattr(repo, "get_capture_operation_plan", lambda *args, **kwargs: None)
    def resolve(_scan, refs, *, workspace_slug="b3s"):
        rows = []
        for raw in evidence_rows:
            if raw["evidence_ref"] in refs:
                rows.append({"evidence_record_id": raw["id"], "evidence_ref": raw["evidence_ref"], "evidence_fingerprint": raw["content_hash"], "content": {"text": raw["content"]}})
        return {"capture_origin": {"capture_id": context["capture_id"], "capture_fingerprint": context["capture_fingerprint"]}, "operation_origin": {"operation_id": context["operation_plan_id"], "operation_fingerprint": context["operation_fingerprint"]}, "evidence": rows}
    monkeypatch.setattr(repo, "resolve_evidence_vault_sv9_judgment_evidence", resolve)
    stored = {}
    def get_candidate(_scan, *, canonical_plan_fingerprint, **_kwargs):
        return deepcopy(stored.get(canonical_plan_fingerprint))
    def append_candidate(_scan, value, **_kwargs):
        value = deepcopy(value) | {"id": "00000000-0000-0000-0000-000000000203"}
        stored[value["canonical_plan_fingerprint"]] = value
        return deepcopy(value), True
    monkeypatch.setattr(repo, "get_evidence_vault_sv9_judgment_candidate", get_candidate)
    monkeypatch.setattr(repo, "append_evidence_vault_sv9_judgment_candidate", append_candidate)
    monkeypatch.setattr(repo, "get_evidence_vault_sv9_evaluation_checkpoint_component_evaluation", lambda *args, **kwargs: None)
    monkeypatch.setattr(repo, "append_evidence_vault_sv9_evaluation_checkpoint", lambda _scan, value, **_kwargs: (deepcopy(value), True))
    return repo, connection, candidate, context


def test_concrete_repo_accepted_v2_projects_and_evaluates(monkeypatch):
    repo, connection, _candidate, context = _repo(monkeypatch)
    facts = repo.load_evidence_vault_sv9_authoritative_relation_facts("current-scan", workspace_slug="b3s")
    assert facts["authority"]["accepted"][0]["basis"][0]["polarity"] == "contradicts"
    evaluation_input = project_evidence_vault_sv9_evaluation_input(repository=repo, source_scan_id="current-scan", workspace_slug="b3s")
    assert evaluation_input["status"] == "available"
    assert history._sv9_checkpoint_current_evaluation_input(
        connection,
        context,
        "b3s",
    ) == evaluation_input
    flow = _Flow()
    outcome = service.run_evidence_vault_sv9_authority_evaluation(repository=repo, flow=flow, domain_or_url="example.com", source_scan_id="current-scan", current_series_contract=_series(), workspace_slug="b3s")
    assert outcome["status"] == "review_required"
    assert "unmapped_evidence" in outcome["reason_codes"]
    assert flow.calls
    assert facts.get("processing_complete_evidence") == []


def test_concrete_repo_operational_only_witness_has_no_continuity(monkeypatch):
    memory = {"canonical_memory_version": "a" * 64}
    event = {"event_id": "00000000-0000-0000-0000-000000000011", "sequence": 1, "candidate_packet_fingerprint": "b" * 64, "request_fingerprint": "c" * 64}
    repo, connection, _candidate, _context = _repo(monkeypatch, accepted=False, chain=[(memory, event)])
    facts = repo.load_evidence_vault_sv9_authoritative_relation_facts("current-scan", workspace_slug="b3s")
    assert facts["authority"]["accepted"] == []
    evaluation_input = project_evidence_vault_sv9_evaluation_input(repository=repo, source_scan_id="current-scan", workspace_slug="b3s")
    assert evaluation_input["status"] == "available"
    assert evaluation_input["authoritative_relations"] == []
    flow = _Flow()
    outcome = service.run_evidence_vault_sv9_authority_evaluation(repository=repo, flow=flow, domain_or_url="example.com", source_scan_id="current-scan", current_series_contract=_series(), workspace_slug="b3s")
    assert flow.calls
    assert outcome["status"] == "review_required"
    assert "unmapped_evidence" in outcome["reason_codes"]


def test_checkpoint_revalidation_reuses_bootstrap_completion_evaluation_input(monkeypatch):
    memory = {"canonical_memory_version": "a" * 64}
    event = {
        "event_id": "00000000-0000-0000-0000-000000000011",
        "sequence": 1,
        "candidate_packet_fingerprint": "b" * 64,
        "request_fingerprint": "c" * 64,
    }
    repo, connection, _candidate, context = _repo(
        monkeypatch,
        accepted=False,
        chain=[(memory, event)],
    )
    completed = {
        "evidence_ref": connection.evidence_rows[0]["evidence_ref"],
        "evidence_fingerprint": connection.evidence_rows[0]["content_hash"],
    }
    monkeypatch.setattr(
        history,
        "_sv9_processing_complete_evidence",
        lambda *_args, **_kwargs: [completed],
    )

    projected = project_evidence_vault_sv9_evaluation_input(
        repository=repo,
        source_scan_id="current-scan",
        workspace_slug="b3s",
    )
    assert projected["status"] == "available"
    assert projected["schema_version"] == "evidence-vault-sv9-evaluation-input-v4"
    assert projected["processing_complete_evidence"] == [completed]

    current = history._sv9_checkpoint_current_evaluation_input(
        connection,
        context,
        "b3s",
    )
    assert current == projected

    tampered = deepcopy(current)
    tampered["processing_complete_evidence"][0]["evidence_fingerprint"] = "f" * 64
    with pytest.raises(ValueError):
        history.validate_evidence_vault_sv9_evaluation_input(
            tampered,
            source_scan_id="current-scan",
            workspace_slug="b3s",
        )
    with monkeypatch.context() as isolated:
        isolated.setattr(
            history,
            "_project_evidence_vault_sv9_evaluation_input_from_facts",
            lambda *_args, **_kwargs: tampered,
        )
        with pytest.raises(
            history.EvidenceVaultSv9EvaluationCheckpointStaleWitnessError
        ):
            history._sv9_checkpoint_current_evaluation_input(
                connection,
                context,
                "b3s",
            )


def test_concrete_repo_changed_support_reviews_without_flow(monkeypatch):
    repo, _connection, _candidate, _context = _repo(monkeypatch, mutate=lambda rows: rows.__setitem__(0, {**rows[0], "content_hash": "f" * 64}))
    evaluation_input = project_evidence_vault_sv9_evaluation_input(repository=repo, source_scan_id="current-scan", workspace_slug="b3s")
    assert evaluation_input["status"] == "review_required"
    flow = _Flow()
    outcome = service.run_evidence_vault_sv9_authority_evaluation(repository=repo, flow=flow, domain_or_url="example.com", source_scan_id="current-scan", current_series_contract=_series(), workspace_slug="b3s")
    assert flow.calls == []
    assert outcome["status"] == "review_required"


def test_concrete_repo_missing_support_reviews_without_flow(monkeypatch):
    repo, _connection, _candidate, _context = _repo(monkeypatch, mutate=lambda rows: rows.pop(0))
    evaluation_input = project_evidence_vault_sv9_evaluation_input(repository=repo, source_scan_id="current-scan", workspace_slug="b3s")
    assert evaluation_input["status"] == "review_required"
    flow = _Flow()
    outcome = service.run_evidence_vault_sv9_authority_evaluation(repository=repo, flow=flow, domain_or_url="example.com", source_scan_id="current-scan", current_series_contract=_series(), workspace_slug="b3s")
    assert flow.calls == []
    assert outcome["status"] == "review_required"
