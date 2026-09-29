import hashlib
import io
import json
from uuid import UUID

import psycopg
import pytest

from scripts import evidence_ledger_measure as measure
from src.sv9_flow.contracts import BrandEvidencePack, BrandInterpretation, EvidenceRecord, Sv9FlowCandidate
from tests.test_evidence_vault_evidence_ledger import (
    ABOUT,
    BRAND,
    HOME,
    _PROMISE,
    _SHIPPING,
    _about,
    _row,
    _snapshot,
    _web,
    _words,
)


_ON = {"transaction_read_only": "on", "transaction_isolation": "repeatable read"}


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Connection:
    def __init__(self, *, settings=None, tables=None):
        self.settings = dict(_ON, **(settings or {}))
        self.tables = tables or {}
        self.executed = []
        self.params = []
        self.read_only = None
        self.isolation_level = None
        self.closed = False
        self.calls = []

    def execute(self, sql, params=()):
        self.executed.append(sql)
        self.params.append(params)
        for name, value in self.settings.items():
            if f"current_setting('{name}')" in sql:
                return _Cursor([{"value": value}])
        for marker, rows in self.tables.items():
            if marker in sql:
                return _Cursor(rows)
        return _Cursor([])

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")
        self.closed = True


def _scan(scan_id, snapshot, rows, evaluations=()):
    return {"scan_id": scan_id, "snapshot": snapshot, "evidence_rows": list(rows), "evaluations": list(evaluations)}


def _promise_row():
    return _row("raw_inputs.0.subpage.1.chunk.1", _PROMISE, url=ABOUT)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  -- scans for one brand\nSELECT id FROM b3s_history.scan_runs WHERE brand_id = %s",
        "WITH recent AS (SELECT id FROM b3s_history.captures) SELECT * FROM recent",
        "select current_setting('default_transaction_read_only') AS value",
    ],
)
def test_read_statements_are_allowed(sql):
    assert measure.statement_allowed(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE b3s_history.brands SET display_name = 'x'",
        "DELETE FROM b3s_history.scan_runs",
        "INSERT INTO b3s_history.brands VALUES (1)",
        "SET default_transaction_read_only = off",
        "SHOW transaction_read_only",
        "BEGIN",
        "WITH gone AS (DELETE FROM b3s_history.captures RETURNING id) SELECT * FROM gone",
        "SELECT 1; DROP TABLE b3s_history.brands",
        "SELECT id FROM b3s_history.scan_runs FOR UPDATE",
        "SELECT pg_advisory_lock(1)",
        "SELECT set_config('default_transaction_read_only', 'off', false)",
    ],
)
def test_non_read_statements_are_blocked(sql):
    assert not measure.statement_allowed(sql)


def test_reader_counts_statements_and_aborts_before_a_write_reaches_the_database():
    connection = _Connection()
    guard = measure.ReadOnlyGuard()
    reader = measure.ReadOnlyReader(connection, guard)

    reader.fetch_all("SELECT 1")
    with pytest.raises(measure.ReadOnlyViolation):
        reader.fetch_all("UPDATE b3s_history.brands SET display_name = 'x'")

    assert connection.executed == ["SELECT 1"]
    assert guard.summary() == {
        "statements_allowed": 1,
        "statements_blocked": 1,
        "blocked_statements": ["UPDATE b3s_history.brands SET display_name = 'x'"],
    }


@pytest.mark.parametrize(
    ("setting", "value"),
    [("transaction_read_only", "off"), ("transaction_isolation", "read committed")],
)
def test_connection_must_prove_a_read_only_repeatable_read_transaction(setting, value):
    connection = _Connection(settings={setting: value})

    with pytest.raises(measure.ReadOnlyViolation, match=setting):
        with measure.open_read_only_reader("postgresql://vault.example/db", connect=lambda *args, **kwargs: connection):
            pytest.fail("an unproven connection must not be used")

    assert connection.read_only is True and connection.closed


def test_a_pooled_session_default_does_not_block_a_proven_read_only_transaction():
    connection = _Connection(settings={"default_transaction_read_only": "off"})

    with measure.open_read_only_reader("postgresql://vault.example/db", connect=lambda *args, **kwargs: connection) as reader:
        assert reader.settings == {"transaction_read_only": "on", "transaction_isolation": "repeatable read"}

    assert connection.closed


def test_reader_rolls_back_its_read_only_transaction_before_closing():
    connection = _Connection()

    with measure.open_read_only_reader("postgresql://vault.example/db", connect=lambda *args, **kwargs: connection):
        pass

    assert connection.calls == ["rollback", "close"]


class _RollbackFails(_Connection):
    def rollback(self):
        raise psycopg.OperationalError("server closed the connection unexpectedly")


def test_reader_still_closes_when_the_rollback_fails():
    connection = _RollbackFails()

    with pytest.raises(psycopg.OperationalError):
        with measure.open_read_only_reader("postgresql://vault.example/db", connect=lambda *args, **kwargs: connection):
            pass

    assert connection.closed


def test_pairs_are_measured_from_fixture_dicts():
    home = _words(120, "h")
    home_row = _row("raw_inputs.0", home[:200])
    first = _snapshot(_web(home, (ABOUT, _about())))
    second = _snapshot(_web(home))
    accepted = {
        "candidate_id": "candidate-1",
        "source_scan_id": "s1",
        "tile_judgments": [
            {
                "tile_id": "M1",
                "component_key": "mission",
                "assessment_state": "ok",
                "supporting_evidence": [
                    {
                        "evidence_ref": _promise_row()["ref"],
                        "evidence_fingerprint": hashlib.sha256(_PROMISE.encode()).hexdigest(),
                    }
                ],
            }
        ],
    }

    report = measure.measure_scan_series(
        domain=BRAND,
        scans=[_scan("s1", first, [home_row, _promise_row()]), _scan("s2", second, [home_row])],
        accepted=accepted,
    )

    [pair] = report["pairs"]
    assert (pair["prior_scan_id"], pair["current_scan_id"]) == ("s1", "s2")
    assert [(row["evidence_ref"], row["state"], row["reason_codes"]) for row in pair["rows"]] == [
        ("raw_inputs.0", "seen", []),
        ("raw_inputs.0.subpage.1.chunk.1", "not_verified", ["page_not_visited"]),
    ]
    assert [(tile["tile_id"], tile["state_counts"]) for tile in pair["tiles"]] == [("M1", {"not_verified": 1})]
    assert pair["summary"]["state_counts"] == {"not_verified": 1, "seen": 1}
    assert pair["summary"]["reason_counts"] == {"page_not_visited": 1}
    assert report["summary"]["state_counts"] == {"not_verified": 1, "seen": 1}
    assert report["accepted_candidate"] == {
        "candidate_id": "candidate-1",
        "source_scan_id": "s1",
        "source_scan_in_series": True,
        "tile_count": 1,
    }
    assert report["runtime_effect"] is False


@pytest.mark.parametrize(
    ("lastmods", "flagged"),
    [((None, None), True), (("2026-01-01", "2026-02-01"), False)],
)
def test_verified_absent_in_a_likely_unchanged_pair_is_a_false_absence_candidate(lastmods, flagged):
    home = _words(160, "h")
    prior_lastmod, current_lastmod = lastmods
    prior = _snapshot(_web(home, (ABOUT, _about()), lastmods={ABOUT: prior_lastmod} if prior_lastmod else None))
    current = _snapshot(
        _web(home, (ABOUT, _about(_SHIPPING)), lastmods={ABOUT: current_lastmod} if current_lastmod else None)
    )

    report = measure.measure_scan_series(
        domain=BRAND, scans=[_scan("s1", prior, [_promise_row()]), _scan("s2", current, [])], accepted=None
    )

    [pair] = report["pairs"]
    assert [row["state"] for row in pair["rows"]] == ["verified_absent"]
    assert pair["brand_likely_unchanged"] is flagged
    expected = [_promise_row()["ref"]] if flagged else []
    assert [row["evidence_ref"] for row in pair["false_verified_absent_candidates"]] == expected
    assert report["summary"]["false_verified_absent_candidate_count"] == len(expected)


_BRAND_ID, _WORKSPACE_ID, _CANDIDATE_ID = UUID(int=1), UUID(int=2), UUID(int=21)


def _mission_candidate(content):
    record = EvidenceRecord(
        ref="raw_inputs.0",
        source="web",
        evidence_type="raw_input",
        content=content,
        url=HOME,
        metadata={"source_class": "owned_copy"},
    )
    return Sv9FlowCandidate(
        evidence_pack=BrandEvidencePack(brand_name="Acme", url=HOME, evidence=[record]),
        interpretation=BrandInterpretation(
            brand_name="Acme",
            url=HOME,
            blocks={"mission": {"detected": True, "content": "Close faster.", "confidence": "high", "rationale": "Home."}},
            evidence_refs={"mission": ["raw_inputs.0"]},
        ),
    ).to_dict()


def _tables(prior, current, home_row):
    captures = {"s1": UUID(int=11), "s2": UUID(int=12)}
    supporting = {"evidence_ref": _promise_row()["ref"], "evidence_fingerprint": hashlib.sha256(_PROMISE.encode()).hexdigest()}

    def evidence(scan_id, row):
        return {
            "capture_id": captures[scan_id],
            "evidence_ref": row["ref"],
            "source": row["source"],
            "source_class": row["metadata"]["source_class"],
            "evidence_type": row["evidence_type"],
            "url": row["url"],
            "content": row["content"],
            "content_raw": None,
            "confidence": row["confidence"],
            "metadata": {},
        }

    return {
        "b3s_history.brands": [{"id": _BRAND_ID, "workspace_id": _WORKSPACE_ID, "canonical_domain": BRAND}],
        "b3s_history.scan_runs": [
            {"source_scan_id": scan_id, "acquisition_state": "pass", "capture_id": captures[scan_id], "snapshot": snapshot}
            for scan_id, snapshot in (("s2", current), ("s1", prior))
        ],
        "b3s_history.evidence_records": [evidence("s1", home_row), evidence("s1", _promise_row())],
        "evidence_vault_sv9_evaluation_checkpoints": [
            {
                "source_scan_id": "s2",
                "component_evaluations": [{"component_key": "mission", "status": "evaluated"}],
                "candidate": _mission_candidate(home_row["content"]),
            }
        ],
        "evidence_vault_sv9_judgment_authority_events": [{"event_type": "adopt", "candidate_id": _CANDIDATE_ID}],
        "candidate_tile_judgments": [
            {
                "id": _CANDIDATE_ID,
                "source_scan_id": "s1",
                "tile_judgments": [
                    {"tile_id": "M1", "component_key": "mission", "assessment_state": "ok", "supporting_evidence": [supporting]}
                ],
            }
        ],
    }


def test_main_reads_through_the_guard_and_prints_one_json_report():
    home = _words(160, "h")
    home_row = _row("raw_inputs.0", home[:200])
    prior, current = _snapshot(_web(home, (ABOUT, _about()))), _snapshot(_web(home, (ABOUT, _about(_SHIPPING))))
    connection = _Connection(tables=_tables(prior, current, home_row))
    stdout = io.StringIO()

    code = measure.main(
        ["--domain", "https://www.acme.example/", "s1", "s2"],
        environ={"B3S_DATABASE_URL": "postgresql://vault.example/db"},
        connect=lambda *args, **kwargs: connection,
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    [pair] = report["pairs"]
    assert code == 0 and report["status"] == "ok"
    assert {row["evidence_ref"]: row["state"] for row in pair["rows"]} == {
        "raw_inputs.0": "seen",
        "raw_inputs.0.subpage.1.chunk.1": "verified_absent",
    }
    assert pair["rows"][0]["shown_to_core"]["mission"]["status"] == "shown"
    assert [(tile["tile_id"], tile["state_counts"]) for tile in pair["tiles"]] == [("M1", {"verified_absent": 1})]
    assert report["accepted_candidate"]["candidate_id"] == str(_CANDIDATE_ID)
    candidate_query = next(index for index, sql in enumerate(connection.executed) if "candidate_tile_judgments" in sql)
    assert connection.params[candidate_query] == (_WORKSPACE_ID, _BRAND_ID, _CANDIDATE_ID)
    assert report["read_only"]["statements_blocked"] == 0
    assert report["read_only"]["transaction_read_only"] == "on"
    assert report["read_only"]["transaction_isolation"] == "repeatable read"
    assert all(measure.statement_allowed(sql) for sql in connection.executed)
    assert connection.closed


def test_main_aborts_without_reading_facts_when_the_transaction_is_not_read_only():
    connection = _Connection(settings={"transaction_read_only": "off"})
    stdout = io.StringIO()

    code = measure.main(
        ["--domain", BRAND, "s1", "s2"],
        environ={"B3S_DATABASE_URL": "postgresql://vault.example/db"},
        connect=lambda *args, **kwargs: connection,
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code != 0 and report["status"] == "aborted"
    assert all("current_setting" in sql for sql in connection.executed)


class _UnknownColumn(_Connection):
    def execute(self, sql, params=()):
        if "b3s_history.brands" in sql:
            raise psycopg.errors.UndefinedColumn('column brands.canonical_domain does not exist at "vault.internal"')
        return super().execute(sql, params)


def _refused(*args, **kwargs):
    raise psycopg.OperationalError('connection to server at "vault.internal" (10.0.0.9), port 5432 failed: timeout expired')


@pytest.mark.parametrize(
    ("connect", "expected"),
    [(_refused, "OperationalError, sqlstate none"), (lambda *args, **kwargs: _UnknownColumn(), "UndefinedColumn, sqlstate 42703")],
)
def test_main_reports_a_database_error_as_aborted_json_without_the_raw_message(connect, expected):
    stdout = io.StringIO()

    code = measure.main(
        ["--domain", BRAND, "s1", "s2"],
        environ={"B3S_DATABASE_URL": "postgresql://vault.example/db"},
        connect=connect,
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code != 0 and report["status"] == "aborted"
    assert expected in report["error"] and "vault.internal" not in report["error"]


def test_main_reports_undecodable_evidence_bytes_as_aborted_json():
    home = _words(160, "h")
    home_row = _row("raw_inputs.0", home[:200])
    prior, current = _snapshot(_web(home, (ABOUT, _about()))), _snapshot(_web(home, (ABOUT, _about(_SHIPPING))))
    tables = _tables(prior, current, home_row)
    tables["b3s_history.evidence_records"][0]["content_raw"] = b"\xff\xfe"
    stdout = io.StringIO()

    code = measure.main(
        ["--domain", BRAND, "s1", "s2"],
        environ={"B3S_DATABASE_URL": "postgresql://vault.example/db"},
        connect=lambda *args, **kwargs: _Connection(tables=tables),
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code != 0 and report["status"] == "aborted"
    assert "not valid UTF-8" in report["error"]
