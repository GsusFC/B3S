from __future__ import annotations

from copy import deepcopy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb


def _hash(letter: str) -> str:
    return letter * 64


def test_authority_migration_declares_append_only_lineage_and_acl_contract() -> None:
    sql = Path("src/history/migrations/032_evidence_vault_sv9_judgment_authority.sql").read_text()

    assert all(
        value in sql
        for value in (
            "evidence_vault_sv9_judgment_authority_events",
            "event_type IN ('adopt', 'reopen', 'supersede')",
            "UNIQUE (workspace_id, brand_id, sequence)",
            "UNIQUE (workspace_id, brand_id, idempotency_key_hash)",
            "candidate_adoption_key",
            "validate_vault_sv9_judgment_authority_insert",
            "evidence_vault_sv9_judgment_active_series_v1",
            "evidence_vault_sv9_judgment_active_partition_v1",
            "GRANT SELECT, INSERT ON b3s_history.evidence_vault_sv9_judgment_authority_events",
        )
    )
    assert sql.count("BEFORE UPDATE OR DELETE OR TRUNCATE") == 1
    assert "REFERENCES b3s_history.evidence_vault_sv9_judgment_candidates" in sql
    assert "accepted_candidate_payload" in sql
    assert "NEW.current_series_fingerprint IS DISTINCT FROM parent.current_series_fingerprint" in sql
    assert "NEW.current_series_fingerprint IS DISTINCT FROM parent.candidate_series_fingerprint" not in sql


# fmt: off
@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_authority_journal_replays_only_valid_active_state() -> None:
    import psycopg
    from psycopg.rows import dict_row
    from src.history.repository import PostgresHistoryRepository

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    workspace, brand, scan, capture, plan = (
        "00000000-0000-0000-0000-000000000101",
        "00000000-0000-0000-0000-000000000102",
        "00000000-0000-0000-0000-000000000103",
        "00000000-0000-0000-0000-000000000104",
        "00000000-0000-0000-0000-000000000105",
    )
    candidate_one = _candidate("00000000-0000-0000-0000-000000000106", "a", "b", "c", "d", "e", "f")
    candidate_two = _candidate("00000000-0000-0000-0000-000000000107", "0", "b", "1", "2", "3", "4")
    owner_preexisting = False
    with psycopg.connect(dsn, autocommit=True) as conn:
        owner_preexisting = bool(conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'b3s_history_vault_provenance_owner'").fetchone())
        if not owner_preexisting:
            conn.execute("CREATE ROLE b3s_history_vault_provenance_owner NOLOGIN")
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
    try:
        PostgresHistoryRepository(dsn).migrate()
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            _seed_candidate_parent(conn, workspace, brand, scan, capture, plan)
            candidate_one_payload = _insert_candidate(conn, candidate_one, workspace, brand, scan, capture, plan, Jsonb({"candidate_component_sentinels": [{"status": "not_detected"}], "candidate_tile_judgments": []}))
            _insert_candidate(conn, candidate_two, workspace, brand, scan, capture, plan, Jsonb({"candidate_component_sentinels": [{"status": "not_detected", "review": "new"}], "candidate_tile_judgments": []}))
            event_one, event_two, event_three = (
                "00000000-0000-0000-0000-000000000108",
                "00000000-0000-0000-0000-000000000109",
                "00000000-0000-0000-0000-000000000110",
            )
            with pytest.raises(psycopg.errors.RaiseException, match="first event"):
                _insert_event(conn, "00000000-0000-0000-0000-000000000111", "reopen", 1, workspace, brand, None, None, None, candidate_one["current"])
            _insert_event(conn, event_one, "adopt", 1, workspace, brand, None, None, candidate_one)
            _insert_event(conn, event_two, "reopen", 2, workspace, brand, event_one, event_one, None, candidate_one["current"])
            reopened = conn.execute("SELECT authority_event_id, latest_event_id, accepted_candidate_payload, reopen_review_overlay FROM b3s_history.evidence_vault_sv9_judgment_active_partition_v1").fetchone()
            assert reopened == {"authority_event_id": UUID(event_one), "latest_event_id": UUID(event_two), "accepted_candidate_payload": candidate_one_payload, "reopen_review_overlay": {"review_state": "pending", "signed_delta": {"kind": "review"}}}
            with pytest.raises(psycopg.errors.RaiseException, match="stale, forked, or gapped"):
                _insert_event(conn, "00000000-0000-0000-0000-000000000112", "supersede", 3, workspace, brand, event_one, event_one, candidate_two)
            _insert_event(conn, event_three, "supersede", 3, workspace, brand, event_two, event_one, candidate_two)
            with pytest.raises(psycopg.errors.RaiseException, match="active parent"):
                _insert_event(conn, "00000000-0000-0000-0000-000000000113", "reopen", 4, workspace, brand, event_three, event_one, None, candidate_two["current"])
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute("UPDATE b3s_history.evidence_vault_sv9_judgment_authority_events SET sequence = 9")
            assert conn.execute("SELECT authority_event_id, latest_event_id FROM b3s_history.evidence_vault_sv9_judgment_active_series_v1").fetchone() == {"authority_event_id": UUID(event_three), "latest_event_id": UUID(event_three)}
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
            if not owner_preexisting:
                conn.execute("DROP ROLE b3s_history_vault_provenance_owner")


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_repository_authority_adopts_replays_competes_and_reopens(monkeypatch) -> None:
    import psycopg
    from src.history.repository import EvidenceVaultSv9JudgmentCandidateConflictError, EvidenceVaultSv9JudgmentCandidateError, EvidenceVaultSv9JudgmentCandidateLegacyAuthorityError, PostgresHistoryRepository
    from src.services import evidence_vault_sv9_authority_event as authority_event
    from src.services import evidence_vault_sv9_authority_projection as authority_projection
    from src.services import evidence_vault_sv9_judgment_delta as delta
    from src.services.evidence_vault_sv9_authoritative_relations import EvidenceVaultSv9AuthoritativeRelationStaleWitnessError, EvidenceVaultSv9AuthoritativeRelationWitnessError
    from src.sv9 import judgment_memory as memory
    from tests.test_evidence_vault_sv9_judgment_candidates_postgres import _captured_candidate, _insert, _legacy, _operational
    from tests.test_sv9_judgment_memory import _series

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    def key(action, scan, candidate=None, fingerprint=None, predecessor=None):
        request = authority_event.build_evidence_vault_sv9_authority_request(action=action, candidate_id=candidate, expected_predecessor_event_fingerprint=predecessor, delta_fingerprint=fingerprint, source_scan_id=scan)
        return authority_event.authority_application_idempotency_fingerprint(request)
    with psycopg.connect(dsn, autocommit=True) as conn:
        existed = bool(conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'b3s_history_vault_provenance_owner'").fetchone())
        if not existed: conn.execute("CREATE ROLE b3s_history_vault_provenance_owner NOLOGIN")
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
    try:
        repository = PostgresHistoryRepository(dsn); repository.migrate()
        current = _operational(repository, "authority-a")
        raw, _projection, _evidence = _captured_candidate(monkeypatch, repository, "authority-a", _series()); stored = repository.append_evidence_vault_sv9_judgment_candidate("authority-a", raw)[0]
        legacy_raw, _, _ = _captured_candidate(monkeypatch, repository, "authority-a", _series(prompt_version="legacy")); legacy = repository.append_evidence_vault_sv9_judgment_candidate("authority-a", _legacy(legacy_raw))[0]
        stale_raw, _, _ = _captured_candidate(monkeypatch, repository, "authority-a", _series(prompt_version="stale")); stale = repository.append_evidence_vault_sv9_judgment_candidate("authority-a", stale_raw)[0]
        invalid, _, _ = _captured_candidate(monkeypatch, repository, "authority-a", _series(prompt_version="invalid"))
        source = repository.resolve_evidence_vault_sv9_judgment_evidence("authority-a", ["raw_inputs.0.chunk.0"])
        def head():
            with psycopg.connect(dsn) as conn: return conn.execute("SELECT count(*), (SELECT event_fingerprint FROM b3s_history.evidence_vault_sv9_judgment_authority_events ORDER BY sequence DESC LIMIT 1) FROM b3s_history.evidence_vault_sv9_judgment_authority_events").fetchone()
        winner_key = key("adopt_candidate", "authority-a", stored["id"])
        def advance(_):
            try: return repository.adopt_evidence_vault_sv9_judgment_candidate("authority-a", stored["id"], expected_predecessor_event_fingerprint=None, idempotency_key_hash=winner_key)
            except EvidenceVaultSv9JudgmentCandidateConflictError: return None
        with ThreadPoolExecutor(max_workers=2) as pool: outcomes = list(pool.map(advance, range(2)))
        winner = next(result[0] for result in outcomes if result is not None)
        assert all(result is not None for result in outcomes) and winner["event"]["event_type"] == "adopt"
        assert repository.get_evidence_vault_sv9_judgment_candidate("authority-a", canonical_plan_fingerprint=legacy["canonical_plan_fingerprint"]) == legacy
        before = head()
        with pytest.raises(EvidenceVaultSv9JudgmentCandidateLegacyAuthorityError): repository.adopt_evidence_vault_sv9_judgment_candidate("authority-a", legacy["id"], expected_predecessor_event_fingerprint=winner["current_head"]["event_fingerprint"], idempotency_key_hash=key("adopt_candidate", "authority-a", legacy["id"], predecessor=winner["current_head"]["event_fingerprint"]))
        assert head() == before
        invalid = deepcopy(invalid); invalid["authoritative_relation_witness"]["witness_fingerprint"] = "0" * 64; invalid["complete_record_fingerprint"] = memory.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v2", {key: value for key, value in invalid.items() if key != "complete_record_fingerprint"}); _insert(repository, "authority-a", invalid)
        with psycopg.connect(dsn) as conn: invalid_id = str(conn.execute("SELECT id FROM b3s_history.evidence_vault_sv9_judgment_candidates WHERE canonical_plan_fingerprint = %s", (invalid["canonical_plan_fingerprint"],)).fetchone()[0])
        next_current = _operational(repository, "authority-next", current)
        same, replayed = repository.adopt_evidence_vault_sv9_judgment_candidate("authority-a", stored["id"], expected_predecessor_event_fingerprint=None, idempotency_key_hash=winner_key)
        assert replayed and same["event"] == winner["event"]
        for error, candidate_id in ((EvidenceVaultSv9AuthoritativeRelationStaleWitnessError, stale["id"]), (EvidenceVaultSv9AuthoritativeRelationWitnessError, invalid_id)):
            before = head()
            with pytest.raises(error): repository.adopt_evidence_vault_sv9_judgment_candidate("authority-a", candidate_id, expected_predecessor_event_fingerprint=winner["current_head"]["event_fingerprint"], idempotency_key_hash=key("adopt_candidate", "authority-a", candidate_id, predecessor=winner["current_head"]["event_fingerprint"]))
            assert head() == before
        def signed(prior, source, *, evidence=None, capture=None, operation=None):
            evidence = evidence or [{key: row[key] for key in ("evidence_ref", "evidence_fingerprint")} for row in source["evidence"]]; capture, operation = capture or source["capture_origin"], operation or source["operation_origin"]
            relation = delta.build_authoritative_evidence_tile_relation(tile_id="M1", component_key="mission", disposition="contradiction", evidence_ref=evidence[0]["evidence_ref"], evidence_fingerprint=evidence[0]["evidence_fingerprint"], capture_origin=capture, operation_origin=operation)
            return delta.build_evidence_vault_sv9_judgment_delta(current_evidence=delta.build_evidence_identity_set(evidence), prior_judgments=prior, authoritative_relations=[relation], current_series_contract=_series())
        prior = [memory.build_tile_judgment(**({key: value for key, value in row.items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint", "authority_state"}} | {"authority_state": "accepted"})) for row in winner["accepted_candidate"]["candidate_tile_judgments"]]
        valid = signed(prior, source)
        next_source = repository.resolve_evidence_vault_sv9_judgment_evidence("authority-next", [row["ref"] for row in next_current])
        def reject(value, scan, predecessor):
            before = head()
            with pytest.raises(EvidenceVaultSv9JudgmentCandidateError): repository.reopen_evidence_vault_sv9_judgment_authority(scan, value, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash=key("reopen_authority", scan, fingerprint=value["canonical_delta_fingerprint"], predecessor=predecessor))
            assert head() == before
        raw = prior[0]; bad_prior = list(prior); bad_prior[0] = memory.build_tile_judgment(**({key: value for key, value in raw.items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint"}} | {"assessment_state": "no"}))
        predecessor = winner["current_head"]["event_fingerprint"]
        for value in (signed(bad_prior, source), signed(prior, source, evidence=[{"evidence_ref": "foreign", "evidence_fingerprint": "f" * 64}]), signed(prior, source, capture={"capture_id": "foreign", "capture_fingerprint": "e" * 64}), signed(prior, source, operation={"operation_id": "foreign", "operation_fingerprint": "d" * 64})): reject(value, "authority-a", predecessor)
        reopen_key = key("reopen_authority", "authority-a", fingerprint=valid["canonical_delta_fingerprint"], predecessor=predecessor)
        reopened, replayed = repository.reopen_evidence_vault_sv9_judgment_authority("authority-a", valid, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash=reopen_key)
        assert not replayed and reopened["event"]["event_type"] == "reopen" and reopened["assessment"] == winner["assessment"] and reopened["score"] == winner["score"] and reopened["reopen_review_overlay"]["review_state"] == "pending"
        same_reopen, replayed = repository.reopen_evidence_vault_sv9_judgment_authority("authority-a", valid, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash=reopen_key)
        assert replayed and same_reopen["event"] == reopened["event"]
        before = head()
        with pytest.raises(EvidenceVaultSv9JudgmentCandidateConflictError): repository.reopen_evidence_vault_sv9_judgment_authority("authority-next", valid, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash=key("reopen_authority", "authority-next", fingerprint=valid["canonical_delta_fingerprint"], predecessor=predecessor))
        assert head() == before
        next_delta = signed(prior, next_source); next_predecessor = reopened["current_head"]["event_fingerprint"]
        newest_key = key("reopen_authority", "authority-next", fingerprint=next_delta["canonical_delta_fingerprint"], predecessor=next_predecessor)
        newest, replayed = repository.reopen_evidence_vault_sv9_judgment_authority("authority-next", next_delta, expected_predecessor_event_fingerprint=next_predecessor, idempotency_key_hash=newest_key)
        assert not replayed and newest["current_head"]["predecessor_event_id"] == reopened["current_head"]["event_id"] and newest["current_head"]["predecessor_event_fingerprint"] == reopened["current_head"]["event_fingerprint"] and newest["current_head"]["request"]["expected_predecessor_event_fingerprint"] == reopened["current_head"]["event_fingerprint"] and newest["current_head"]["active_parent_event_id"] == newest["active_authority_event"]["event_id"] and newest["current_head"]["active_parent_event_fingerprint"] == newest["active_authority_event"]["event_fingerprint"] and newest["current_head"]["request"]["source_scan_id"] == "authority-next" and newest["reopen_review_overlay"]["delta_fingerprint"] == next_delta["canonical_delta_fingerprint"]
        older_replay, replayed = repository.reopen_evidence_vault_sv9_judgment_authority("authority-a", valid, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash=reopen_key)
        assert replayed and older_replay["event"] == reopened["event"] and older_replay["event"] != older_replay["current_head"]
        loaded = repository.get_evidence_vault_sv9_judgment_authority("example.com")
        assert loaded and loaded["current_head"]["event_type"] == "reopen" and loaded["active_authority_event"]["event_type"] == "adopt" and loaded["accepted_candidate"]["source_scan_id"] == "authority-a" and loaded["current_head"]["request"]["source_scan_id"] == "authority-next" and loaded["reopen_review_overlay"]["delta_fingerprint"] == next_delta["canonical_delta_fingerprint"]
        for projection in (winner, reopened, newest, older_replay, loaded):
            assert authority_projection.validate_persisted_evidence_vault_sv9_authority_projection(projection) == json.loads(json.dumps(projection))
            for name in ("active_authority_event", "current_head", "event"):
                assert authority_event.validate_evidence_vault_sv9_authority_event(projection[name]) == json.loads(json.dumps(projection[name]))
        assert winner["event"]["idempotency_key_hash"] == winner_key and reopened["event"]["idempotency_key_hash"] == reopen_key and newest["event"]["idempotency_key_hash"] == newest_key
        with psycopg.connect(dsn) as conn: assert conn.execute("SELECT event_fingerprint FROM b3s_history.evidence_vault_sv9_judgment_authority_events WHERE id = %s", (newest["event"]["event_id"],)).fetchone()[0] == loaded["current_head"]["event_fingerprint"]
        def update(sql, values):
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("ALTER TABLE b3s_history.evidence_vault_sv9_judgment_authority_events DISABLE TRIGGER ALL")
                try: conn.execute(sql, values)
                finally: conn.execute("ALTER TABLE b3s_history.evidence_vault_sv9_judgment_authority_events ENABLE TRIGGER ALL")
        identifier = reopened["event"]["event_id"]
        for column, bad, original, target in (("event_fingerprint", _hash("0"), reopened["event"]["event_fingerprint"], identifier), ("idempotency_key_hash", _hash("1"), reopen_key, identifier), ("assessment_fingerprint", _hash("2"), winner["event"]["assessment_fingerprint"], winner["event"]["event_id"])):
            sql = f"UPDATE b3s_history.evidence_vault_sv9_judgment_authority_events SET {column} = %s WHERE id = %s"
            update(sql, (bad, target))
            with pytest.raises(EvidenceVaultSv9JudgmentCandidateError): repository.get_evidence_vault_sv9_judgment_authority("example.com")
            update(sql, (original, target))
        sql = "UPDATE b3s_history.evidence_vault_sv9_judgment_authority_events SET event_payload = jsonb_set(event_payload, '{request,source_scan_id}', to_jsonb(%s::text)) WHERE id = %s"
        update(sql, ("tampered", identifier))
        with pytest.raises(EvidenceVaultSv9JudgmentCandidateError): repository.get_evidence_vault_sv9_judgment_authority("example.com")
        update(sql, ("authority-a", identifier))
        assert repository.get_evidence_vault_sv9_judgment_authority("example.com")["current_head"] == loaded["current_head"]
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
            if not existed: conn.execute("DROP ROLE b3s_history_vault_provenance_owner")


def _candidate(identifier: str, plan: str, current: str, series: str, bundle: str, assessment: str, score: str) -> dict[str, str]:
    return {"id": identifier, "plan": _hash(plan), "current": _hash(current), "series": _hash(series), "bundle": _hash(bundle), "assessment": _hash(assessment), "score": _hash(score)}


def _seed_candidate_parent(conn, workspace, brand, scan, capture, plan) -> None:
    conn.execute("INSERT INTO b3s_history.workspaces (id, slug, name) VALUES (%s, 'authority', 'Authority')", (workspace,))
    conn.execute("INSERT INTO b3s_history.brands (id, workspace_id, canonical_domain, display_name, canonical_url, first_observed_at, latest_observed_at) VALUES (%s, %s, 'authority.test', 'Authority', 'https://authority.test', now(), now())", (brand, workspace))
    conn.execute("INSERT INTO b3s_history.scan_runs (id, workspace_id, brand_id, source_scan_id, status, pipeline_version, requested_at) VALUES (%s, %s, %s, 'authority-scan', 'captured', 'test', now())", (scan, workspace, brand))
    conn.execute("INSERT INTO b3s_history.captures (id, brand_id, scan_run_id, observed_at, recorded_at, source_url, content_hash) VALUES (%s, %s, %s, now(), now(), 'https://authority.test', %s)", (capture, brand, scan, _hash("a")))
    conn.execute("INSERT INTO b3s_history.evidence_vault_operation_plans (id, workspace_id, brand_id, scan_run_id, observation_hash, operation_plan_fingerprint, mode, status, plan_payload) VALUES (%s, %s, %s, %s, %s, %s, 'baseline', 'pending', '{}'::jsonb)", (plan, workspace, brand, scan, _hash("b"), _hash("c")))


def _insert_candidate(conn, value, workspace, brand, scan, capture, plan, payload) -> dict:
    fingerprint = _hash(value["id"][-1]); payload = Jsonb({"schema_version": "evidence-vault-sv9-judgment-candidate-v1", "plan": {}, "canonical_plan_fingerprint": value["plan"], "current_series_fingerprint": value["current"], "candidate_series_fingerprint": value["series"], "component_evaluations": [], "evidence_bindings": [], "candidate_tile_judgments": [], "candidate_component_sentinels": [], "assessment": {}, "telemetry": {}, "evaluation_bundle_fingerprint": value["bundle"], "assessment_fingerprint": value["assessment"], "score_fingerprint": value["score"], "complete_record_fingerprint": fingerprint} | payload.obj)
    conn.execute("INSERT INTO b3s_history.evidence_vault_sv9_judgment_candidates (id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id, operation_plan_id, schema_version, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, evaluation_bundle_fingerprint, assessment_fingerprint, score_fingerprint, complete_record_fingerprint, candidate_payload, authority, review_state, lifecycle_state, runtime_effect) VALUES (%s, %s, %s, %s, 'authority-scan', %s, %s, 'evidence-vault-sv9-judgment-candidate-v1', %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 'none', 'active', 'shadow_only')", (value["id"], workspace, brand, scan, capture, plan, value["plan"], value["current"], value["series"], value["bundle"], value["assessment"], value["score"], fingerprint, payload)); return payload.obj


def _insert_event(conn, identifier, event_type, sequence, workspace, brand, predecessor, parent, candidate, current=None) -> None:
    candidate = candidate or {}
    conn.execute("INSERT INTO b3s_history.evidence_vault_sv9_judgment_authority_events (id, workspace_id, brand_id, event_type, sequence, predecessor_event_id, active_parent_event_id, candidate_id, candidate_scan_run_id, candidate_capture_id, candidate_operation_plan_id, request_fingerprint, event_fingerprint, evaluation_bundle_fingerprint, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, assessment_fingerprint, score_fingerprint, delta_fingerprint, idempotency_key_hash, event_payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (identifier, workspace, brand, event_type, sequence, predecessor, parent, candidate.get("id"), "00000000-0000-0000-0000-000000000103" if candidate else None, "00000000-0000-0000-0000-000000000104" if candidate else None, "00000000-0000-0000-0000-000000000105" if candidate else None, _hash("5"), _hash(identifier[-1]), candidate.get("bundle"), candidate.get("plan"), candidate.get("current", current), candidate.get("series"), candidate.get("assessment"), candidate.get("score"), None if candidate else _hash("6"), _hash(identifier[-1]), Jsonb({"review_state": "pending", "signed_delta": {"kind": "review"}}) if not candidate else Jsonb({"candidate": candidate["id"]})))
# fmt: on


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_authority_application_adopts_witnessed_candidate_and_replays() -> None:
    import psycopg
    from src.history.repository import PostgresHistoryRepository
    from src.services import evidence_vault_sv9_authority_application as application
    from src.services.evidence_vault_sv9_authoritative_relations import (
        project_evidence_vault_sv9_authoritative_relations,
    )
    from tests.test_evidence_vault_sv9_judgment_candidates_postgres import _AuthorityFlow, _operational
    from tests.test_sv9_judgment_memory import _series

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        existed = bool(
            conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'b3s_history_vault_provenance_owner'").fetchone()
        )
        if not existed:
            conn.execute("CREATE ROLE b3s_history_vault_provenance_owner NOLOGIN")
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
    try:
        repository = PostgresHistoryRepository(dsn)
        repository.migrate()

        _operational(repository, "authority-service-current")
        projection = project_evidence_vault_sv9_authoritative_relations(
            repository=repository, source_scan_id="authority-service-current"
        )
        assert projection["status"] == "available"
        applied = application.run_evidence_vault_sv9_authority_application(
            repository=repository,
            flow=_AuthorityFlow(),
            domain_or_url="example.com",
            source_scan_id="authority-service-current",
            current_series_contract=_series(),
        )
        repeated = application.run_evidence_vault_sv9_authority_application(
            repository=repository,
            flow=_AuthorityFlow(),
            domain_or_url="example.com",
            source_scan_id="authority-service-current",
            current_series_contract=_series(),
        )
        stored = repository.get_evidence_vault_sv9_judgment_candidate(
            "authority-service-current", canonical_plan_fingerprint=applied["candidate"]["canonical_plan_fingerprint"]
        )
        authority = repository.get_evidence_vault_sv9_judgment_authority("example.com")
        assert (
            applied["status"] == "authority_established"
            and repeated["status"] == "authority_retained"
            and stored
            and stored["schema_version"].endswith("v2")
            and stored["id"] == applied["candidate"]["id"]
            and authority["accepted_candidate"]["id"] == stored["id"]
            and authority["reopen_review_overlay"] is None
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
            if not existed:
                conn.execute("DROP ROLE b3s_history_vault_provenance_owner")
