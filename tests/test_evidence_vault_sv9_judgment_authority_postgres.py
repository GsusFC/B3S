from __future__ import annotations

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
            _insert_candidate(conn, candidate_one, workspace, brand, scan, capture, plan, Jsonb({"candidate_component_sentinels": [{"status": "not_detected"}], "candidate_tile_judgments": []}))
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
            assert reopened == {"authority_event_id": UUID(event_one), "latest_event_id": UUID(event_two), "accepted_candidate_payload": {"candidate_component_sentinels": [{"status": "not_detected"}], "candidate_tile_judgments": []}, "reopen_review_overlay": {"review_state": "pending", "signed_delta": {"kind": "review"}}}
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
def test_repository_authority_adopts_replays_competes_and_reopens() -> None:
    import psycopg
    from src.history.repository import EvidenceVaultSv9JudgmentCandidateConflictError, EvidenceVaultSv9JudgmentCandidateError, PostgresHistoryRepository
    from src.services import evidence_vault_sv9_judgment_delta as delta
    from src.sv9 import incremental_evaluation as evaluation, incremental_planner as planner, judgment_memory as memory
    from tests.test_evidence_vault_operation_execution_postgres import _persist_baseline
    from tests.test_evidence_vault_sv9_judgment_candidates_postgres import _candidate as build_candidate
    from tests.test_sv9_incremental_evaluation import _Flow
    from tests.test_sv9_judgment_memory import _series

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        existed = bool(conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'b3s_history_vault_provenance_owner'").fetchone())
        if not existed: conn.execute("CREATE ROLE b3s_history_vault_provenance_owner NOLOGIN")
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
    try:
        repository = PostgresHistoryRepository(dsn); repository.migrate()
        def store(scan, sentinel=False):
            source = repository.resolve_evidence_vault_sv9_judgment_evidence(scan, ["raw_inputs.0.chunk.0"]); evidence = source["evidence"]; plan = planner.build_incremental_plan([], [], _series())
            packets = [evaluation.build_evidence_packet(component_key=component, tiles=[{"tile_id": tile, "evidence": [{key: evidence[0][key] for key in ("evidence_ref", "evidence_fingerprint", "content")}]} for tile in plan["tile_workset"] if dict(planner._REGISTRY)[tile] == component], capture_origin=source["capture_origin"], operation_origin=source["operation_origin"], series_fingerprint=plan["current_series_fingerprint"]) for component in plan["component_workset"]]
            candidate = build_candidate(plan, evaluation.execute_incremental_evaluation(plan, packets, _Flow("not_detected" if sentinel else "ok")))
            candidate["evidence_bindings"] = [{"tile_id": tile, "evidence_record_id": evidence[0]["evidence_record_id"], **{key: evidence[0][key] for key in ("evidence_ref", "evidence_fingerprint")}} for tile in plan["tile_workset"]]
            candidate["complete_record_fingerprint"] = memory.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v1", {key: value for key, value in candidate.items() if key != "complete_record_fingerprint"})
            return repository.append_evidence_vault_sv9_judgment_candidate(scan, candidate)[0], source
        stored, sources = {}, {}
        for scan in ("authority-a", "authority-b", "authority-c"):
            _persist_baseline(repository, scan); stored[scan], sources[scan] = store(scan)
        adopted, replayed = repository.adopt_evidence_vault_sv9_judgment_candidate("authority-a", stored["authority-a"]["id"], expected_predecessor_event_fingerprint=None, idempotency_key_hash="a" * 64)
        same, same_replayed = repository.adopt_evidence_vault_sv9_judgment_candidate("authority-a", stored["authority-a"]["id"], expected_predecessor_event_fingerprint=None, idempotency_key_hash="a" * 64)
        assert not replayed and same_replayed and same["event"] == adopted["event"]
        with pytest.raises(EvidenceVaultSv9JudgmentCandidateConflictError): repository.adopt_evidence_vault_sv9_judgment_candidate("authority-b", stored["authority-a"]["id"], expected_predecessor_event_fingerprint=None, idempotency_key_hash="a" * 64)
        def advance(scan):
            try: return scan, repository.adopt_evidence_vault_sv9_judgment_candidate(scan, stored[scan]["id"], expected_predecessor_event_fingerprint=adopted["current_head"]["event_fingerprint"], idempotency_key_hash=scan[-1] * 64)
            except EvidenceVaultSv9JudgmentCandidateConflictError: return scan, None
        with ThreadPoolExecutor(max_workers=2) as pool: outcomes = list(pool.map(advance, ("authority-b", "authority-c")))
        winner_scan, winner = next((scan, result[0]) for scan, result in outcomes if result is not None)
        assert sum(result is not None for _scan, result in outcomes) == 1 and winner["event"]["event_type"] == "supersede"
        def signed(prior, source, *, evidence=None, capture=None, operation=None):
            evidence = evidence or [{key: row[key] for key in ("evidence_ref", "evidence_fingerprint")} for row in source["evidence"]]; capture, operation = capture or source["capture_origin"], operation or source["operation_origin"]
            relation = delta.build_authoritative_evidence_tile_relation(tile_id="M1", component_key="mission", disposition="contradiction", evidence_ref=evidence[0]["evidence_ref"], evidence_fingerprint=evidence[0]["evidence_fingerprint"], capture_origin=capture, operation_origin=operation)
            return delta.build_evidence_vault_sv9_judgment_delta(current_evidence=delta.build_evidence_identity_set(evidence), prior_judgments=prior, authoritative_relations=[relation], current_series_contract=_series())
        prior, source = [memory.build_tile_judgment(**({key: value for key, value in row.items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint", "authority_state"}} | {"authority_state": "accepted"})) for row in winner["accepted_candidate"]["candidate_tile_judgments"]], sources[winner_scan]
        valid = signed(prior, source)
        def head():
            with psycopg.connect(dsn) as conn: return conn.execute("SELECT count(*), (SELECT event_fingerprint FROM b3s_history.evidence_vault_sv9_judgment_authority_events ORDER BY sequence DESC LIMIT 1) FROM b3s_history.evidence_vault_sv9_judgment_authority_events").fetchone()
        def reject(value, scan, predecessor, key):
            before = head()
            with pytest.raises(EvidenceVaultSv9JudgmentCandidateError): repository.reopen_evidence_vault_sv9_judgment_authority(scan, value, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash=key * 64)
            assert head() == before
        raw = prior[0]; bad_prior = list(prior); bad_prior[0] = memory.build_tile_judgment(**({key: value for key, value in raw.items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint"}} | {"assessment_state": "no"}))
        predecessor = winner["current_head"]["event_fingerprint"]
        for key, value in (("0", signed(bad_prior, source)), ("1", signed(prior, source, evidence=[{"evidence_ref": "foreign", "evidence_fingerprint": "f" * 64}])), ("2", signed(prior, source, capture={"capture_id": "foreign", "capture_fingerprint": "e" * 64})), ("3", signed(prior, source, operation={"operation_id": "foreign", "operation_fingerprint": "d" * 64}))): reject(value, winner_scan, predecessor, key)
        reopened, replayed = repository.reopen_evidence_vault_sv9_judgment_authority(winner_scan, valid, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash="4" * 64)
        assert not replayed and reopened["event"]["event_type"] == "reopen" and reopened["assessment"] == winner["assessment"] and reopened["score"] == winner["score"] and reopened["reopen_review_overlay"]["review_state"] == "pending"
        same_reopen, replayed = repository.reopen_evidence_vault_sv9_judgment_authority(winner_scan, valid, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash="4" * 64)
        assert replayed and same_reopen["event"] == reopened["event"]
        before = head()
        with pytest.raises(EvidenceVaultSv9JudgmentCandidateConflictError): repository.reopen_evidence_vault_sv9_judgment_authority(winner_scan, valid, expected_predecessor_event_fingerprint=predecessor, idempotency_key_hash="5" * 64)
        assert head() == before
        loaded = repository.get_evidence_vault_sv9_judgment_authority("example.com")
        assert loaded and loaded["current_head"]["event_type"] == "reopen" and loaded["active_authority_event"]["event_type"] == "supersede" and loaded["reopen_review_overlay"]["delta_fingerprint"] == valid["canonical_delta_fingerprint"]
        _persist_baseline(repository, "authority-s"); sentinel, sentinel_source = store("authority-s", True)
        active, _ = repository.adopt_evidence_vault_sv9_judgment_candidate("authority-s", sentinel["id"], expected_predecessor_event_fingerprint=reopened["current_head"]["event_fingerprint"], idempotency_key_hash="6" * 64)
        assert active["event"]["event_type"] == "supersede" and active["reopen_review_overlay"] is None
        reject(signed([memory.build_tile_judgment(**({key: value for key, value in row.items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint", "authority_state"}} | {"authority_state": "accepted"})) for row in active["accepted_candidate"]["candidate_tile_judgments"]], sentinel_source), "authority-s", active["current_head"]["event_fingerprint"], "7")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("ALTER TABLE b3s_history.evidence_vault_sv9_judgment_authority_events DISABLE TRIGGER ALL")
            conn.execute("UPDATE b3s_history.evidence_vault_sv9_judgment_authority_events SET event_fingerprint = %s WHERE id = %s", (_hash("0"), active["event"]["event_id"]))
            conn.execute("ALTER TABLE b3s_history.evidence_vault_sv9_judgment_authority_events ENABLE TRIGGER ALL")
        with pytest.raises(EvidenceVaultSv9JudgmentCandidateError): repository.get_evidence_vault_sv9_judgment_authority("example.com")
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


def _insert_candidate(conn, value, workspace, brand, scan, capture, plan, payload) -> None:
    conn.execute("INSERT INTO b3s_history.evidence_vault_sv9_judgment_candidates (id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id, operation_plan_id, schema_version, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, evaluation_bundle_fingerprint, assessment_fingerprint, score_fingerprint, complete_record_fingerprint, candidate_payload, authority, review_state, lifecycle_state, runtime_effect) VALUES (%s, %s, %s, %s, 'authority-scan', %s, %s, 'evidence-vault-sv9-judgment-candidate-v1', %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 'none', 'active', 'shadow_only')", (value["id"], workspace, brand, scan, capture, plan, value["plan"], value["current"], value["series"], value["bundle"], value["assessment"], value["score"], _hash(value["id"][-1]), payload))


def _insert_event(conn, identifier, event_type, sequence, workspace, brand, predecessor, parent, candidate, current=None) -> None:
    candidate = candidate or {}
    conn.execute("INSERT INTO b3s_history.evidence_vault_sv9_judgment_authority_events (id, workspace_id, brand_id, event_type, sequence, predecessor_event_id, active_parent_event_id, candidate_id, candidate_scan_run_id, candidate_capture_id, candidate_operation_plan_id, request_fingerprint, event_fingerprint, evaluation_bundle_fingerprint, canonical_plan_fingerprint, current_series_fingerprint, candidate_series_fingerprint, assessment_fingerprint, score_fingerprint, delta_fingerprint, idempotency_key_hash, event_payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (identifier, workspace, brand, event_type, sequence, predecessor, parent, candidate.get("id"), "00000000-0000-0000-0000-000000000103" if candidate else None, "00000000-0000-0000-0000-000000000104" if candidate else None, "00000000-0000-0000-0000-000000000105" if candidate else None, _hash("5"), _hash(identifier[-1]), candidate.get("bundle"), candidate.get("plan"), candidate.get("current", current), candidate.get("series"), candidate.get("assessment"), candidate.get("score"), None if candidate else _hash("6"), _hash(identifier[-1]), Jsonb({"review_state": "pending", "signed_delta": {"kind": "review"}}) if not candidate else Jsonb({"candidate": candidate["id"]})))
# fmt: on


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_authority_service_appends_replays_without_mutating_active_authority() -> None:
    import psycopg
    from src.history.repository import PostgresHistoryRepository
    from src.services import evidence_vault_sv9_authority_evaluation as service
    from src.services import evidence_vault_sv9_judgment_delta as delta
    from src.sv9 import incremental_evaluation as evaluation, incremental_planner as planner, judgment_memory as memory
    from tests.test_evidence_vault_operation_execution_postgres import _persist_baseline, _row
    from tests.test_evidence_vault_sv9_judgment_candidates_postgres import _candidate
    from tests.test_sv9_incremental_evaluation import _Flow
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

        def seed(scan, content):
            row = _row() | {"content": content}
            _persist_baseline(repository, scan, rows=[row])
            source = repository.resolve_evidence_vault_sv9_judgment_evidence(scan, ["raw_inputs.0.chunk.0"])
            evidence = source["evidence"]
            plan = planner.build_incremental_plan([], [], _series())
            packets = [
                evaluation.build_evidence_packet(
                    component_key=component,
                    tiles=[
                        {
                            "tile_id": tile,
                            "evidence": [
                                {key: evidence[0][key] for key in ("evidence_ref", "evidence_fingerprint", "content")}
                            ],
                        }
                        for tile in plan["tile_workset"]
                        if dict(planner._REGISTRY)[tile] == component
                    ],
                    capture_origin=source["capture_origin"],
                    operation_origin=source["operation_origin"],
                    series_fingerprint=plan["current_series_fingerprint"],
                )
                for component in plan["component_workset"]
            ]
            candidate = _candidate(plan, evaluation.execute_incremental_evaluation(plan, packets, _Flow()))
            candidate["evidence_bindings"] = [
                {
                    "tile_id": tile,
                    "evidence_record_id": evidence[0]["evidence_record_id"],
                    **{key: evidence[0][key] for key in ("evidence_ref", "evidence_fingerprint")},
                }
                for tile in plan["tile_workset"]
            ]
            candidate["complete_record_fingerprint"] = memory.canonical_fingerprint(
                "evidence-vault-sv9-judgment-candidate-record-v1",
                {key: value for key, value in candidate.items() if key != "complete_record_fingerprint"},
            )
            return repository.append_evidence_vault_sv9_judgment_candidate(scan, candidate)[0]

        accepted = seed("authority-service-accepted", "We help teams ship better products.")
        repository.adopt_evidence_vault_sv9_judgment_candidate(
            "authority-service-accepted",
            accepted["id"],
            expected_predecessor_event_fingerprint=None,
            idempotency_key_hash="a" * 64,
        )
        current = seed("authority-service-current", "We help teams ship safer products.")
        source = repository.resolve_evidence_vault_sv9_judgment_evidence(
            "authority-service-current", ["raw_inputs.0.chunk.0"]
        )
        identity = [{key: source["evidence"][0][key] for key in ("evidence_ref", "evidence_fingerprint")}]
        relation = delta.build_authoritative_evidence_tile_relation(
            tile_id="M1",
            component_key="mission",
            disposition="relevant",
            **identity[0],
            capture_origin=source["capture_origin"],
            operation_origin=source["operation_origin"],
        )

        class Flow:
            def evaluate_component(self, request):
                rows = [
                    {
                        "tile_id": row["tile_id"],
                        "assessment_state": "ok" if row["evidence"] else "sin_evidencia",
                        "supporting_evidence": [
                            {key: evidence[key] for key in ("evidence_ref", "evidence_fingerprint")}
                            for evidence in row["evidence"]
                        ],
                    }
                    for row in request["requested_tiles"]
                ]
                return evaluation.ComponentEvaluationOutcome.success(
                    evaluation.build_component_evaluation(
                        component_key=request["component_key"],
                        series_fingerprint=request["current_series_fingerprint"],
                        request_fingerprint=request["canonical_request_fingerprint"],
                        status="evaluated",
                        tile_results=rows,
                    )
                )

        before = repository.get_evidence_vault_sv9_judgment_authority("example.com")
        result = service.run_evidence_vault_sv9_authority_evaluation(
            repository=repository,
            flow=Flow(),
            domain_or_url="example.com",
            source_scan_id="authority-service-current",
            current_evidence=identity,
            authoritative_relations=[relation],
            current_series_contract=_series(),
        )
        stored = repository.get_evidence_vault_sv9_judgment_candidate(
            "authority-service-current", canonical_plan_fingerprint=result["candidate"]["canonical_plan_fingerprint"]
        )
        assert (
            result["status"] == "review_required"
            and "coverage_loss" in result["reason_codes"]
            and stored
            and stored["id"] == result["candidate"]["id"]
            and repository.get_evidence_vault_sv9_judgment_authority("example.com") == before
            and current["id"] != stored["id"]
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
            if not existed:
                conn.execute("DROP ROLE b3s_history_vault_provenance_owner")
