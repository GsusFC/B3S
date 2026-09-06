import os
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from src.history import repository as history
from src.services import evidence_vault_sv9_authority_application as application
from src.services.evidence_vault_sv9_authoritative_relations import project_evidence_vault_sv9_evaluation_input
from src.services.evidence_vault_sv9_evaluation_checkpoint import build_evidence_vault_sv9_evaluation_checkpoint
from tests.test_evidence_vault_sv9_evaluation_checkpoint import _build, _build_not_detected, _progress, _sha
from tests.test_evidence_vault_sv9_judgment_candidates_postgres import _AuthorityFlow, _operational
from tests.test_evidence_vault_scan_orchestration_postgres import _reset_repository
from tests.test_sv9_judgment_memory import _series
def _checkpoint(repository, scan, snapshot, request="checkpoint"):
    value = project_evidence_vault_sv9_evaluation_input(repository=repository, source_scan_id=scan); assert value["status"] == "available"
    progress = _progress(value, request)
    return build_evidence_vault_sv9_evaluation_checkpoint(evaluation_input=value, prior_authority_snapshot=snapshot, current_series_fingerprint=progress["evaluated_tile_judgments"][0]["series_fingerprint"], canonical_plan_fingerprint=_sha("checkpoint-plan"), candidate_series_fingerprint=_sha("checkpoint-series"), healthy_tile_ids=["M1"], **progress)


class _Rows:
    def __init__(self, *, one=None, many=()):
        self.one, self.many = one, list(many)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.many)


class _CheckpointReadbackConnection:
    def __init__(self, *, rows=(), checkpoint_row=None, insert_row=None):
        self.rows = list(rows)
        self.checkpoint_row = checkpoint_row
        self.insert_row = insert_row
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, parameters=()):
        self.statements.append((statement, tuple(parameters)))
        if "canonical_plan_fingerprint = %s" in statement:
            return _Rows(many=self.rows)
        if "checkpoint_fingerprint = %s" in statement:
            return _Rows(one=self.checkpoint_row)
        if "INSERT INTO b3s_history.evidence_vault_sv9_evaluation_checkpoints" in statement:
            return _Rows(one=self.insert_row)
        return _Rows()


def _checkpoint_context():
    return {
        "workspace_id": "workspace",
        "brand_id": "brand",
        "scan_run_id": "scan-run",
        "source_scan_id": "scan-1",
        "capture_id": "capture",
        "operation_plan_id": "operation",
    }


def _checkpoint_readback_repository(monkeypatch, *, rows, records, checkpoint_row=None):
    conn = _CheckpointReadbackConnection(rows=rows, checkpoint_row=checkpoint_row)
    repository = object.__new__(history.PostgresHistoryRepository)
    repository._ensure_migrated = lambda: None
    repository._connect = lambda: conn
    checked = []
    monkeypatch.setattr(history, "_verify_exact_migration_head_under_shared_lock", lambda _conn: None)
    monkeypatch.setattr(history, "_sv9_judgment_context", lambda *_: _checkpoint_context())

    def stored_record(_conn, row, _context):
        checked.append(row["id"])
        return records[row["id"]]

    monkeypatch.setattr(history, "_sv9_checkpoint_stored_record", stored_record)
    return repository, conn, checked

def test_checkpoint_migration_contract_is_forward_only_and_non_authoritative():
    sql = Path("src/history/migrations/034_evidence_vault_sv9_evaluation_checkpoints.sql").read_text(); columns = sql.split("checkpoint_payload", 1)[0]
    assert history._migration_files()[-1][0] == "035_evidence_vault_sv9_empty_relation_witness.sql"
    assert all(value in sql for value in ("PRIMARY KEY (checkpoint_id, evidence_record_id)", "FOREIGN KEY (capture_id, evidence_record_id, evidence_fingerprint)", "BEFORE UPDATE OR DELETE OR TRUNCATE", "GRANT SELECT, INSERT", "authority IS FALSE", "runtime_effect = 'checkpoint_only'", "score_state = 'unavailable'", "REVOKE ALL"))
    assert all(value not in columns for value in ("assessment", "adoptable", "publishable", "complete_record")) and "$.**" in sql

@pytest.mark.parametrize("dsn", ("postgresql://test@example.com/b3s_test", "host=127.0.0.1 hostaddr=203.0.113.9 dbname=b3s_test", "host=127.0.0.1 port=5432 dbname=production"))
def test_checkpoint_disposable_dsn_guard_rejects_before_connect_or_drop(monkeypatch, dsn):
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: pytest.fail("unsafe DSN reached connect"))
    with pytest.raises(RuntimeError): _reset_repository(dsn)

def test_checkpoint_append_freshness_fences_changed_input_bootstrap_and_head(monkeypatch):
    bootstrap, head = _build(snapshot={"state": "bootstrap_absent"}), _build()
    conn = type("Conn", (), {"__enter__": lambda self: self, "__exit__": lambda *_: False, "execute": lambda self, *_: self})()
    repository = object.__new__(history.PostgresHistoryRepository); repository._ensure_migrated = lambda: None; repository._connect = lambda: conn
    monkeypatch.setattr(history, "_verify_exact_migration_head_under_shared_lock", lambda _conn: None); monkeypatch.setattr(history, "_sv9_judgment_context", lambda *_: {"brand_id": "brand"})
    monkeypatch.setattr(history, "_sv9_checkpoint_current_evaluation_input", lambda *_: {"changed": True})
    with pytest.raises(history.EvidenceVaultSv9EvaluationCheckpointStaleWitnessError): repository.append_evidence_vault_sv9_evaluation_checkpoint("scan-1", bootstrap)
    monkeypatch.setattr(history, "_sv9_checkpoint_current_evaluation_input", lambda *_: bootstrap["evaluation_input"]); monkeypatch.setattr(history, "_sv9_checkpoint_authority_snapshot", lambda *_: {"state": "accepted_authority"})
    with pytest.raises(history.EvidenceVaultSv9EvaluationCheckpointStaleWitnessError): repository.append_evidence_vault_sv9_evaluation_checkpoint("scan-1", bootstrap)
    monkeypatch.setattr(history, "_sv9_checkpoint_current_evaluation_input", lambda *_: head["evaluation_input"]); monkeypatch.setattr(history, "_sv9_checkpoint_authority_snapshot", lambda *_: ({key: value for key, value in head["prior_authority_snapshot"].items() if key != "snapshot_fingerprint"} | {"current_head_event_fingerprint": _sha("changed-head")}))
    with pytest.raises(history.EvidenceVaultSv9EvaluationCheckpointStaleWitnessError): repository.append_evidence_vault_sv9_evaluation_checkpoint("scan-1", head)


def test_checkpoint_component_evaluation_lookup_returns_none_for_other_request_and_scopes_query(monkeypatch):
    checkpoint = _build(request="stored-request")
    repository, conn, checked = _checkpoint_readback_repository(
        monkeypatch,
        rows=[{"id": "stored"}],
        records={"stored": checkpoint},
    )
    result = repository.get_evidence_vault_sv9_evaluation_checkpoint_component_evaluation(
        "scan-1",
        canonical_plan_fingerprint=checkpoint["plan_binding"]["canonical_plan_fingerprint"],
        canonical_request_fingerprint=_sha("requested-request"),
    )
    assert result is None and checked == ["stored"]
    statement, parameters = conn.statements[-1]
    assert all(name in statement for name in ("workspace_id", "brand_id", "scan_run_id", "source_scan_id", "capture_id", "operation_plan_id", "canonical_plan_fingerprint"))
    assert parameters == (*_checkpoint_context().values(), checkpoint["plan_binding"]["canonical_plan_fingerprint"])


def test_checkpoint_component_evaluation_lookup_deduplicates_and_detaches_identical_results(monkeypatch):
    checkpoint = _build(request="same-request")
    evaluation = checkpoint["healthy_workset"]["component_evaluations"][0]
    repository, _conn, checked = _checkpoint_readback_repository(
        monkeypatch,
        rows=[{"id": "first"}, {"id": "second"}],
        records={"first": checkpoint, "second": deepcopy(checkpoint)},
    )
    result = repository.get_evidence_vault_sv9_evaluation_checkpoint_component_evaluation(
        "scan-1",
        canonical_plan_fingerprint=checkpoint["plan_binding"]["canonical_plan_fingerprint"],
        canonical_request_fingerprint=evaluation["request_fingerprint"],
    )
    assert result == evaluation and result is not evaluation and checked == ["first", "second"]
    result["tile_results"].clear()
    assert evaluation["tile_results"]


def test_checkpoint_component_evaluation_lookup_fails_closed_for_distinct_canonical_results(monkeypatch):
    checkpoint = _build(request="shared-request")
    contradictory = _build_not_detected(request="shared-request")
    evaluation = checkpoint["healthy_workset"]["component_evaluations"][0]
    assert contradictory["plan_binding"]["canonical_plan_fingerprint"] == checkpoint["plan_binding"]["canonical_plan_fingerprint"]
    repository, _conn, checked = _checkpoint_readback_repository(
        monkeypatch,
        rows=[{"id": "first"}, {"id": "second"}],
        records={"first": checkpoint, "second": contradictory},
    )
    with pytest.raises(history.EvidenceVaultSv9EvaluationCheckpointConflictError):
        repository.get_evidence_vault_sv9_evaluation_checkpoint_component_evaluation(
            "scan-1",
            canonical_plan_fingerprint=checkpoint["plan_binding"]["canonical_plan_fingerprint"],
            canonical_request_fingerprint=evaluation["request_fingerprint"],
        )
    assert checked == ["first", "second"]


@pytest.mark.parametrize("plan, request_fingerprint", (("invalid", _sha("request")), (_sha("plan"), "INVALID")))
def test_checkpoint_component_evaluation_lookup_rejects_invalid_fingerprints_before_sql(plan, request_fingerprint):
    repository = object.__new__(history.PostgresHistoryRepository)
    repository._ensure_migrated = lambda: pytest.fail("invalid input reached migration SQL")
    repository._connect = lambda: pytest.fail("invalid input reached checkpoint SQL")
    with pytest.raises(history.EvidenceVaultSv9EvaluationCheckpointError):
        repository.get_evidence_vault_sv9_evaluation_checkpoint_component_evaluation(
            "scan-1",
            canonical_plan_fingerprint=plan,
            canonical_request_fingerprint=request_fingerprint,
        )


def test_checkpoint_append_rejects_distinct_logical_evaluation_before_insert(monkeypatch):
    incoming = _build(request="shared-request")
    prior = _build_not_detected(request="shared-request")
    repository, conn, checked = _checkpoint_readback_repository(
        monkeypatch,
        rows=[{"id": "prior"}],
        records={"prior": prior},
    )
    monkeypatch.setattr(history, "_sv9_checkpoint_current_evaluation_input", lambda *_: incoming["evaluation_input"])
    monkeypatch.setattr(history, "_sv9_checkpoint_authority_snapshot", lambda *_: {key: value for key, value in incoming["prior_authority_snapshot"].items() if key != "snapshot_fingerprint"})
    with pytest.raises(history.EvidenceVaultSv9EvaluationCheckpointConflictError):
        repository.append_evidence_vault_sv9_evaluation_checkpoint("scan-1", incoming)
    locks = [parameters[0] for statement, parameters in conn.statements if "pg_advisory_xact_lock" in statement]
    assert locks == [history._advisory_lock_key("brand", name) for name in ("evidence-vault-canonical-promotion", "evidence-vault-sv9-judgment-authority", "evidence-vault-sv9-evaluation-checkpoint")]
    assert checked == ["prior"] and not any("INSERT INTO b3s_history.evidence_vault_sv9_evaluation_checkpoints" in statement for statement, _ in conn.statements)


def test_checkpoint_append_keeps_identical_occurrence_idempotent(monkeypatch):
    checkpoint = _build(request="same-request")
    repository, conn, _checked = _checkpoint_readback_repository(
        monkeypatch,
        rows=[{"id": "existing"}],
        records={"existing": checkpoint},
        checkpoint_row={"id": "existing"},
    )
    monkeypatch.setattr(history, "_sv9_checkpoint_current_evaluation_input", lambda *_: checkpoint["evaluation_input"])
    monkeypatch.setattr(history, "_sv9_checkpoint_authority_snapshot", lambda *_: {key: value for key, value in checkpoint["prior_authority_snapshot"].items() if key != "snapshot_fingerprint"})
    stored, inserted = repository.append_evidence_vault_sv9_evaluation_checkpoint("scan-1", checkpoint)
    assert stored == checkpoint and not inserted
    assert sum("INSERT INTO b3s_history.evidence_vault_sv9_evaluation_checkpoints" in statement for statement, _ in conn.statements) == 1

@pytest.mark.skipif(not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1", reason="requires explicitly enabled disposable PostgreSQL")
def test_checkpoint_ledger_round_trips_fences_mutation_and_bootstrap_race():
    import psycopg
    repository = _reset_repository(); scan = "checkpoint-persistence"; _operational(repository, scan)
    bootstrap = _checkpoint(repository, scan, {"state": "bootstrap_absent"}); stored, inserted = repository.append_evidence_vault_sv9_evaluation_checkpoint(scan, bootstrap)
    assert inserted and not ({"assessment", "score", "adoptable", "publishable"} & set(stored))
    loaded = repository.get_evidence_vault_sv9_evaluation_checkpoint(scan, checkpoint_fingerprint=bootstrap["checkpoint_fingerprint"]); replay, repeated = repository.append_evidence_vault_sv9_evaluation_checkpoint(scan, bootstrap)
    assert loaded == stored == replay and not repeated
    other, other_inserted = repository.append_evidence_vault_sv9_evaluation_checkpoint(scan, _checkpoint(repository, scan, {"state": "bootstrap_absent"}, "other")); assert other_inserted and other["checkpoint_fingerprint"] != stored["checkpoint_fingerprint"]
    context = repository.load_evidence_vault_sv9_judgment_context(scan); dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings WHERE checkpoint_id = %s", (stored["id"],)).fetchone()[0] == len(bootstrap["evaluation_input"]["current_identity_bindings"])
        bad = _sha("bad-checkpoint")
        with pytest.raises(psycopg.Error): conn.execute("INSERT INTO b3s_history.evidence_vault_sv9_evaluation_checkpoints (id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id, operation_plan_id, schema_version, evaluation_input_fingerprint, relation_projection_fingerprint, prior_authority_snapshot_fingerprint, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, checkpoint_fingerprint, evaluation_state, authority, runtime_effect, score_state, checkpoint_payload) SELECT %s, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id, operation_plan_id, schema_version, evaluation_input_fingerprint, relation_projection_fingerprint, prior_authority_snapshot_fingerprint, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, %s, evaluation_state, authority, runtime_effect, score_state, jsonb_set(jsonb_set(checkpoint_payload, '{authority}', 'true'::jsonb), '{checkpoint_fingerprint}', to_jsonb(%s::text)) FROM b3s_history.evidence_vault_sv9_evaluation_checkpoints WHERE id = %s", (uuid4(), bad, bad, stored["id"]))
        for capture, record, fingerprint in ((context["capture_id"], uuid4(), _sha("wrong-evidence")), (uuid4(), bootstrap["evaluation_input"]["current_identity_bindings"][0]["evidence_record_id"], bootstrap["evaluation_input"]["current_identity_bindings"][0]["evidence_fingerprint"])):
            with pytest.raises(psycopg.Error): conn.execute("INSERT INTO b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings (checkpoint_id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id, evidence_record_id, evidence_ref, evidence_fingerprint) VALUES (%s, %s, %s, %s, %s, %s, %s, 'wrong', %s)", (stored["id"], context["workspace_id"], context["brand_id"], context["scan_run_id"], capture, context["operation_plan_id"], record, fingerprint))
        for statement in ("UPDATE b3s_history.evidence_vault_sv9_evaluation_checkpoints SET evaluation_state = 'partial'", "DELETE FROM b3s_history.evidence_vault_sv9_evaluation_checkpoints", "TRUNCATE b3s_history.evidence_vault_sv9_evaluation_checkpoints"):
            with pytest.raises(psycopg.Error): conn.execute(statement)
    applied = application.run_evidence_vault_sv9_authority_application(repository=repository, flow=_AuthorityFlow(), domain_or_url="example.com", source_scan_id=scan, current_series_contract=_series())
    assert applied["status"] == "authority_established"
    authority = repository.get_evidence_vault_sv9_judgment_authority("example.com"); candidate, active, head = authority["accepted_candidate"], authority["active_authority_event"], authority["current_head"]
    snapshot = {"state": "accepted_authority", "accepted_candidate_id": candidate["id"], "active_event_id": active["event_id"], "current_head_event_fingerprint": head["event_fingerprint"], "candidate_complete_record_fingerprint": candidate["complete_record_fingerprint"], "canonical_plan_fingerprint": candidate["canonical_plan_fingerprint"], "current_series_fingerprint": candidate["current_series_fingerprint"]}
    accepted, _ = repository.append_evidence_vault_sv9_evaluation_checkpoint(scan, _checkpoint(repository, scan, snapshot, "post-authority")); assert accepted["prior_authority_snapshot"]["state"] == "accepted_authority"
    with pytest.raises(history.EvidenceVaultSv9EvaluationCheckpointStaleWitnessError): repository.append_evidence_vault_sv9_evaluation_checkpoint(scan, bootstrap)


@pytest.mark.parametrize("shape", ("single", "duplicate", "missing", "ambiguous"))
def test_full_checkpoint_request_lookup_preserves_input_and_rejects_ambiguity(monkeypatch, shape):
    checkpoint = _build(request="accepted-request")
    other = (
        deepcopy(checkpoint)
        if shape != "ambiguous"
        else _build(request="accepted-request", snapshot={"state": "bootstrap_absent"})
    )
    repository, conn, checked = _checkpoint_readback_repository(
        monkeypatch,
        rows=[{"id": "first"}] + ([{"id": "second"}] if shape in {"duplicate", "ambiguous"} else []),
        records={"first": checkpoint, "second": other},
    )
    arguments = {
        "canonical_plan_fingerprint": checkpoint["plan_binding"]["canonical_plan_fingerprint"],
        "canonical_request_fingerprint": _sha("missing")
        if shape == "missing"
        else checkpoint["healthy_workset"]["component_evaluations"][0]["request_fingerprint"],
    }
    if shape == "ambiguous":
        with pytest.raises(history.EvidenceVaultSv9EvaluationCheckpointConflictError):
            repository.get_evidence_vault_sv9_evaluation_checkpoint_for_request("scan-1", **arguments)
    else:
        result = repository.get_evidence_vault_sv9_evaluation_checkpoint_for_request("scan-1", **arguments)
        assert result == (None if shape == "missing" else checkpoint)
        if result is not None:
            result["evaluation_input"].clear()
            assert checkpoint["evaluation_input"]
    assert checked == (["first", "second"] if shape in {"duplicate", "ambiguous"} else ["first"])
    statement, parameters = conn.statements[-1]
    assert parameters == (*_checkpoint_context().values(), arguments["canonical_plan_fingerprint"])
    assert all(f"{field} = %s" in statement for field in _checkpoint_context())


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires explicitly enabled disposable PostgreSQL",
)
def test_accepted_candidate_reuses_full_checkpoint_input_with_uncited_evidence(monkeypatch):
    from src.sv9 import incremental_evaluation as evaluator

    class Flow(_AuthorityFlow):
        def evaluate_component(self, request):
            value = deepcopy(super().evaluate_component(request).evaluation)
            for row in value["tile_results"]:
                row.update(assessment_state="sin_evidencia", supporting_evidence=[])
            return evaluator.ComponentEvaluationOutcome.success(
                evaluator.build_component_evaluation(
                    **{
                        key: item
                        for key, item in value.items()
                        if key not in {"schema_version", "canonical_component_evaluation_fingerprint"}
                    }
                )
            )

    repository = _reset_repository()
    scan = "checkpoint-accepted-reuse"
    _operational(repository, scan)
    run = lambda flow: application.run_evidence_vault_sv9_authority_application(
        repository=repository,
        flow=flow,
        domain_or_url="example.com",
        source_scan_id=scan,
        current_series_contract=_series(),
    )
    flow = Flow()
    assert run(flow)["status"] == "authority_established"
    assert any(tile["evidence"] for request in flow.calls for tile in request["requested_tiles"])
    accepted = repository.get_evidence_vault_sv9_judgment_authority("example.com")
    candidate = accepted["accepted_candidate"]
    current = project_evidence_vault_sv9_evaluation_input(repository=repository, source_scan_id=scan)
    for component in candidate["component_evaluations"]:
        proof = repository.get_evidence_vault_sv9_evaluation_checkpoint_for_request(
            scan,
            canonical_plan_fingerprint=candidate["canonical_plan_fingerprint"],
            canonical_request_fingerprint=component["request_fingerprint"],
        )
        assert proof["evaluation_input"] == current and proof["healthy_workset"]["component_evaluations"] == [component]
        assert proof["prior_authority_snapshot"]["state"] == "bootstrap_absent"
    for method in (
        "append_evidence_vault_sv9_evaluation_checkpoint",
        "append_evidence_vault_sv9_judgment_candidate",
        "adopt_evidence_vault_sv9_judgment_candidate",
    ):
        monkeypatch.setattr(
            repository, method, lambda *_args, **_kwargs: pytest.fail("accepted-input replay wrote new state")
        )
    for _ in range(4):
        flow = Flow()
        result = run(flow)
        assert result["status"] == "authority_retained" and result["reason_codes"] == ["exact_reuse"]
        assert not flow.calls and repository.get_evidence_vault_sv9_judgment_authority("example.com") == accepted
