import os
from uuid import uuid4

import pytest

from scripts import configure_b3s_runtime_role as runtime_role
from src.history import repository as history
from src.services import evidence_vault_evidence_ledger as ledger
from tests.test_evidence_vault_evidence_ledger import _SHIPPING, _about, _about_pair, _ledger, _promise_row
from tests.test_evidence_vault_scan_orchestration_postgres import _reset_repository
from tests.test_evidence_vault_sv9_evaluation_checkpoint_postgres import _CheckpointSharedFlow, _shared_series
from tests.test_evidence_vault_sv9_judgment_candidates_postgres import _operational, _seed_accepted_sv9_authority


_MIGRATION = "039_evidence_vault_evidence_ledger.sql"
_TABLE = "b3s_history.evidence_vault_evidence_ledger_rows"
_PRIOR, _CURRENT = "ledger-prior", "ledger-current"
_COLUMNS = (
    "workspace_id", "brand_id", "scan_run_id", "capture_id", "source_scan_id",
    "prior_scan_run_id", "prior_capture_id", "prior_source_scan_id",
    "evidence_ref", "evidence_fingerprint", "evidence_id", "source_identity_id", "source_key", "evidence_class",
    "state", "reason_codes", "health", "shown_to_core", "policy_version", "runtime_effect",
)
_ROW_FIELDS = _COLUMNS[8:]
_POSTGRES = pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires explicitly enabled disposable PostgreSQL",
)


def _ledger_row():
    prior, current = _about_pair(_about(_SHIPPING))
    [row] = _ledger(prior, [_promise_row()], current)["rows"]
    return row


def _ledger_rows(facts):
    prior, current = facts["prior"], facts["current"]
    return ledger.build_evidence_ledger(
        brand_domain=facts["domain"],
        prior_snapshot=prior["snapshot"],
        prior_rows=prior["evidence_rows"],
        current_snapshot=current["snapshot"],
        current_rows=current["evidence_rows"],
        shown_index=ledger.build_shown_index(current["evaluations"]),
    )["rows"]


def _accepted_pair(repository):
    prior = _operational(repository, _PRIOR)
    _seed_accepted_sv9_authority(repository, _PRIOR, _shared_series(), flow=_CheckpointSharedFlow(_PRIOR))
    return prior, _operational(repository, _CURRENT, prior)


def test_ledger_migration_is_the_append_only_shadow_head():
    migrations = dict(history._migration_files())
    sql = migrations[_MIGRATION]

    assert list(migrations)[-1] == _MIGRATION
    assert all(
        value in sql
        for value in (
            f"CREATE TABLE {_TABLE}",
            "state text NOT NULL CHECK (state IN ('seen', 'verified_absent', 'not_verified'))",
            "CHECK (policy_version ~ '^evidence-vault-evidence-ledger-v[0-9]+$')",
            "runtime_effect boolean NOT NULL DEFAULT false CHECK (runtime_effect = false)",
            "CHECK (capture_id <> prior_capture_id)",
            "UNIQUE (capture_id, prior_capture_id, evidence_ref)",
            "FOREIGN KEY (prior_capture_id, evidence_ref) REFERENCES b3s_history.evidence_records (capture_id, evidence_ref) ON DELETE RESTRICT",
            f"REVOKE ALL ON {_TABLE} FROM PUBLIC",
            f"GRANT SELECT, INSERT ON {_TABLE} TO b3s_pr71_app_runtime",
        )
    )
    assert sql.count("BEFORE UPDATE OR DELETE OR TRUNCATE") == 1 and "FOR EACH STATEMENT" in sql
    assert "evidence_ledger_shadow" not in sql


def test_runtime_role_grants_the_ledger_append_only_at_the_new_head():
    assert runtime_role.EXPECTED_HEAD_VERSION == "039"
    assert "evidence_vault_evidence_ledger_rows" in runtime_role.APPEND_ONLY_JUDGMENT_RELATIONS


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("state", "absent"),
        ("policy_version", "evidence-vault-evidence-ledger-v2"),
        ("runtime_effect", True),
        ("evidence_ref", ""),
        ("evidence_fingerprint", "0" * 63),
        ("evidence_id", None),
        ("source_identity_id", "A" * 64),
        ("source_key", None),
        ("evidence_class", "Owned-Page"),
        ("reason_codes", "page_404"),
        ("health", []),
        ("shown_to_core", None),
    ),
)
def test_append_rejects_an_invalid_row_before_any_database_access(field, value):
    repository = history.PostgresHistoryRepository(
        "host=127.0.0.1 port=5432 dbname=b3s_test", connect=lambda *_args, **_kwargs: pytest.fail("connected")
    )

    with pytest.raises(history.EvidenceVaultEvidenceLedgerError, match=field):
        repository.append_evidence_ledger_rows(
            _CURRENT, prior_source_scan_id=_PRIOR, rows=[_ledger_row(), {**_ledger_row(), field: value}]
        )


@_POSTGRES
def test_loader_reads_the_latest_accepted_scan_other_than_the_current_one():
    repository = _reset_repository()
    prior = _operational(repository, _PRIOR)
    assert repository.load_evidence_ledger_scan_facts("example.com", source_scan_id=_PRIOR) is None
    _seed_accepted_sv9_authority(repository, _PRIOR, _shared_series(), flow=_CheckpointSharedFlow(_PRIOR))
    # The hook runs right after application, which may just have adopted this scan's own candidate.
    assert repository.load_evidence_ledger_scan_facts("example.com", source_scan_id=_PRIOR) is None
    current = _operational(repository, _CURRENT, prior)

    facts = repository.load_evidence_ledger_scan_facts("https://example.com", source_scan_id=_CURRENT)

    assert facts["domain"] == "example.com"
    assert [facts[side]["scan_id"] for side in ("prior", "current")] == [_PRIOR, _CURRENT]
    for side, rows in (("prior", prior), ("current", current)):
        scan = facts[side]
        assert [(row["ref"], row["content"]) for row in scan["evidence_rows"]] == [(row["ref"], row["content"]) for row in rows]
        assert scan["snapshot"]["raw_inputs"] and scan["snapshot"]["acquisition_gate"] == {"state": "complete"}
    assert {row["status"] for row in facts["prior"]["evaluations"]} == {"evaluated"}
    assert facts["current"]["evaluations"] == []


@_POSTGRES
def test_ledger_rows_append_once_and_the_table_keeps_the_shadow_contract():
    import psycopg

    repository = _reset_repository()
    _accepted_pair(repository)
    rows = _ledger_rows(repository.load_evidence_ledger_scan_facts("example.com", source_scan_id=_CURRENT))

    first = repository.append_evidence_ledger_rows(_CURRENT, prior_source_scan_id=_PRIOR, rows=rows)
    second = repository.append_evidence_ledger_rows(_CURRENT, prior_source_scan_id=_PRIOR, rows=rows)

    assert first == len(rows) > 0 and second == 0
    for prior_scan in (_CURRENT, "ledger-missing"):
        with pytest.raises(history.EvidenceVaultEvidenceLedgerError):
            repository.append_evidence_ledger_rows(_CURRENT, prior_source_scan_id=prior_scan, rows=rows)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        repository.append_evidence_ledger_rows(
            _CURRENT, prior_source_scan_id=_PRIOR, rows=[{**rows[0], "evidence_ref": "raw_inputs.9.chunk.0"}]
        )
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"], autocommit=True) as conn:
        stored = conn.execute(
            f"SELECT id, source_scan_id, prior_source_scan_id, {', '.join(_ROW_FIELDS)} FROM {_TABLE} ORDER BY evidence_ref"
        ).fetchall()
        assert [row[1:3] for row in stored] == [(_CURRENT, _PRIOR)] * first
        assert [dict(zip(_ROW_FIELDS, row[3:])) for row in stored] == [{name: row[name] for name in _ROW_FIELDS} for row in rows]
        for column, expression in (
            ("state", "'absent'"),
            ("runtime_effect", "true"),
            ("policy_version", "'evidence-vault-evidence-ledger-v3.1'"),
            ("capture_id", "prior_capture_id"),
        ):
            values = ", ".join(expression if name == column else name for name in _COLUMNS)
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    f"INSERT INTO {_TABLE} (id, {', '.join(_COLUMNS)}) SELECT %s, {values} FROM {_TABLE} WHERE id = %s",
                    (uuid4(), stored[0][0]),
                )
        for statement in (f"UPDATE {_TABLE} SET state = 'seen'", f"DELETE FROM {_TABLE}", f"TRUNCATE {_TABLE}"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute(statement)
        assert conn.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0] == first
