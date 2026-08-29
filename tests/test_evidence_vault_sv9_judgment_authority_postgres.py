from __future__ import annotations

import os
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
