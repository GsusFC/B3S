from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os

import pytest

from src.history.models import CaptureConflictError
from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_incremental_executor import execute_vault_operation_plan
from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan
from src.scanner_evidence_comparison import canonical_evidence_rows


pytestmark = pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)


class ExecutorLLM:
    api_key = "test"

    def __init__(self):
        self.calls = []

    def _call_json(self, system, user, **kwargs):
        self.calls.append(kwargs["schema_name"])
        if kwargs["schema_name"] == "sv9_flow_evidence_labeling":
            payload = json.loads(user)
            return {"labels": [{
                "ref": row["ref"],
                "relevant_blocks": ["mission"],
                "stance": "supports",
                "identity_match": "domain",
                "specificity": "explicit",
            } for row in payload["records"]]}
        marker = '"evidence_fingerprint": "'
        fingerprint = user.split(marker, 1)[1].split('"', 1)[0]
        return {"relations": [{
            "evidence_fingerprint": fingerprint,
            "tile_id": "M1",
            "polarity": "supports",
            "literal_quote": "help teams ship better products",
            "rationale": "Explicit contribution statement.",
        }]}


class NoCallLLM:
    api_key = "test"

    def _call_json(self, *args, **kwargs):
        raise AssertionError("persisted-result recovery must not call the LLM")


class CrashOnceAfterResult:
    def __init__(self, repository):
        self.repository = repository
        self.crashed = False

    def __getattr__(self, name):
        return getattr(self.repository, name)

    def register_evidence_vault_operational_source_packet(self, *args, **kwargs):
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("crash after result persistence")
        return self.repository.register_evidence_vault_operational_source_packet(
            *args, **kwargs
        )


class CrashBeforeFinalize:
    def __init__(self, repository):
        self.repository = repository

    def __getattr__(self, name):
        return getattr(self.repository, name)

    def finalize_capture_operation_plan(self, *args, **kwargs):
        raise RuntimeError("crash before finalize")


class TamperSemanticShortlistResult:
    def __init__(self, repository):
        self.repository = repository

    def __getattr__(self, name):
        return getattr(self.repository, name)

    def persist_capture_operation_result(self, source_scan_id, **kwargs):
        payload = deepcopy(kwargs["result_payload"])
        fingerprint = payload["selected_evidence_fingerprints"][0]
        payload["tile_shortlists"][fingerprint][-1] = "V1"
        payload["tile_shortlists"][fingerprint].sort()
        payload["shortlisted_tile_ids"] = sorted(
            {
                tile_id
                for rows in payload["tile_shortlists"].values()
                for tile_id in rows
            }
        )
        kwargs["result_payload"] = payload
        return self.repository.persist_capture_operation_result(
            source_scan_id,
            **kwargs,
        )


def _row():
    return {
        "ref": "raw_inputs.0.chunk.0",
        "source": "web",
        "evidence_type": "owned_copy",
        "url": "https://example.com",
        "content": "We help teams ship better products.",
        "confidence": "high",
        "metadata": {
            "source_class": "owned_copy",
            "identity_match": "domain",
        },
    }


def _persist_baseline(repository, scan_id, rows=None, plan=None):
    rows = rows or [_row()]
    plan = plan or build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="baseline",
        current_evidence_records=rows,
    )
    observation = {
        "schema_version": "b3s-capture-observation-v1",
        "source_scan_id": scan_id,
        "source_run_id": f"provider-{scan_id}",
        "brand_name": "Example",
        "url": "https://example.com",
        "observed_at": "2026-08-07T10:00:00Z",
        "recorded_at": "2026-08-07T10:00:01Z",
        "pipeline_version": "vault-incremental-capture-v1",
        "acquisition_state": "complete",
        "acquisition_summary": {"owned_pages": 1},
        "limitations": [],
        "capture_payload": {
            "raw_inputs": [{"source": "web", "payload": {"text": "Evidence"}}]
        },
        "evidence_records": rows,
        "acquisition_attempts": [
            {"provider": "web", "intent": "owned", "status": "success"}
        ],
        "artifacts": [],
        "metadata": {
            "mode": "baseline",
            "analysis_status": (
                "pending"
                if any(
                    plan["operations"][field]
                    for field in (
                        "llm_required",
                        "create_candidate_packet",
                        "create_diagnostic_report",
                    )
                )
                else "not_required"
            ),
            "operation_plan": plan,
            "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
        },
    }
    repository.persist_capture_observation(observation)
    return plan


def _reset_repository():
    import psycopg
    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        pytest.fail("B3S_TEST_ALLOW_SCHEMA_DROP=1 is required")
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
    repository = PostgresHistoryRepository(dsn)
    repository.migrate()
    return repository


def test_claim_is_single_winner_and_expired_lease_is_fenced() -> None:
    repository = _reset_repository()
    _persist_baseline(repository, "lease-scan")

    def claim(worker):
        return repository.claim_capture_operation_plan(
            "lease-scan", worker_id=worker, lease_seconds=60
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["worker-a", "worker-b"]))
    assert sum(result["claimed"] is True for result in claims) == 1
    winner = next(result for result in claims if result["claimed"] is True)
    loser = next(result for result in claims if result["claimed"] is False)
    assert loser["claim_status"] == "busy"
    assert loser["lease_token"] is None
    duplicate_owner = repository.claim_capture_operation_plan(
        "lease-scan", worker_id=winner["lease_owner"], lease_seconds=60
    )
    assert duplicate_owner["claimed"] is False
    assert duplicate_owner["claim_status"] == "owned_active"
    assert duplicate_owner["lease_token"] is None
    repository.mark_capture_operation_running(
        "lease-scan",
        worker_id=winner["lease_owner"],
        lease_token=winner["lease_token"],
        lease_generation=winner["lease_generation"],
        lease_seconds=60,
    )

    import psycopg
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        conn.execute(
            """
            UPDATE b3s_history.evidence_vault_operation_plans
            SET lease_expires_at = now() - interval '1 second'
            WHERE scan_run_id = (
                SELECT id FROM b3s_history.scan_runs WHERE source_scan_id = %s
            )
            """,
            ("lease-scan",),
        )
    reclaimed = repository.claim_capture_operation_plan(
        "lease-scan", worker_id="worker-c", lease_seconds=60
    )
    assert reclaimed["claimed"] is True
    assert reclaimed["lease_generation"] == winner["lease_generation"] + 1
    assert reclaimed["lease_token"] != winner["lease_token"]
    with pytest.raises(CaptureConflictError, match="lease"):
        repository.heartbeat_capture_operation_plan(
            "lease-scan",
            worker_id=winner["lease_owner"],
            lease_token=winner["lease_token"],
            lease_generation=winner["lease_generation"],
        )


def test_database_rejects_direct_operation_terminal_state_forgery() -> None:
    import psycopg

    repository = _reset_repository()
    _persist_baseline(repository, "terminal-forgery-scan")
    direct_completion = """
        UPDATE b3s_history.evidence_vault_operation_plans
        SET status = 'completed',
            result_fingerprint = repeat('a', 64),
            result_payload = jsonb_build_object('output_kind', 'no_delta'),
            result_persisted_at = now(),
            completed_at = now()
        WHERE scan_run_id = (
            SELECT id
            FROM b3s_history.scan_runs
            WHERE source_scan_id = %s
        )
    """
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="status transition is invalid",
        ):
            conn.execute(direct_completion, ("terminal-forgery-scan",))

    claim = repository.claim_capture_operation_plan(
        "terminal-forgery-scan",
        worker_id="terminal-forgery-worker",
        lease_seconds=60,
    )
    repository.mark_capture_operation_running(
        "terminal-forgery-scan",
        worker_id="terminal-forgery-worker",
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        lease_seconds=60,
    )
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="status transition is invalid",
        ):
            conn.execute(direct_completion, ("terminal-forgery-scan",))


def test_repository_rederives_shortlists_from_persisted_semantic_labels() -> None:
    repository = _reset_repository()
    _persist_baseline(repository, "tampered-semantic-shortlist")

    with pytest.raises(
        CaptureConflictError,
        match="shortlists are not derived",
    ):
        execute_vault_operation_plan(
            repository=TamperSemanticShortlistResult(repository),
            source_scan_id="tampered-semantic-shortlist",
            worker_id="worker-a",
            llm=ExecutorLLM(),
        )


def test_result_survives_crash_and_retry_performs_zero_second_llm_calls() -> None:
    import psycopg

    repository = _reset_repository()
    plan = _persist_baseline(repository, "crash-scan")
    llm = ExecutorLLM()
    crashing = CrashOnceAfterResult(repository)

    with pytest.raises(RuntimeError, match="crash after result"):
        execute_vault_operation_plan(
            repository=crashing,
            source_scan_id="crash-scan",
            worker_id="worker-a",
            llm=llm,
        )
    frozen = repository.get_capture_operation_plan("crash-scan")
    assert frozen["status"] == "result_persisted"
    assert frozen["result_payload"]["operation_plan_fingerprint"] == plan[
        "operation_plan_fingerprint"
    ]
    calls_before_retry = list(llm.calls)
    repository.register_evidence_vault_operational_memory_packet(
        "example.com",
        frozen["result_payload"]["operational_candidate_packet"],
    )
    with pytest.raises(CaptureConflictError, match="source packet"):
        repository.finalize_capture_operation_plan(
            "crash-scan",
            operation_plan_fingerprint=plan["operation_plan_fingerprint"],
            result_fingerprint=frozen["result_fingerprint"],
            candidate_packet_fingerprint=frozen["result_payload"][
                "candidate_packet_fingerprint"
            ],
        )

    recovered = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="crash-scan",
        worker_id="worker-b",
        llm=NoCallLLM(),
    )

    assert recovered["execution_status"] == "completed"
    assert llm.calls == calls_before_retry
    completed = repository.get_capture_operation_plan("crash-scan")
    assert completed["status"] == "completed"
    assert completed["candidate_packet_fingerprint"] == completed[
        "result_payload"
    ]["candidate_packet_fingerprint"]

    result = completed["result_payload"]
    relation_id = result["basis_relations"][0]["relation_id"]
    review = repository.review_and_adopt_evidence_vault_operational_source(
        "example.com",
        source_candidate_packet_fingerprint=result[
            "source_candidate_packet_fingerprint"
        ],
        decisions=[{
            "relation_id": relation_id,
            "decision": "accept",
            "rationale": "The literal statement directly supports M1.",
        }],
        reviewer_id="human-reviewer",
        reviewed_at="2026-08-07T13:00:00+02:00",
        created_at="2026-08-07T11:00:00Z",
    )
    assert review["score"] is None
    assert review["adoption"] is not None
    assert {
        event["created_at"] for event in review["review_events"]
    } == {"2026-08-07T11:00:00+00:00"}
    assert review["memory"]["content"]["accepted_tiles"][0][
        "authority_source"
    ] == "human"
    score, score_replayed = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            "example.com"
        )
    )
    assert score_replayed is False
    assert score is not None
    assert score["score"] == 1
    assert score["canonical_memory_version"] == review["memory"][
        "canonical_memory_version"
    ]
    replay = repository.review_and_adopt_evidence_vault_operational_source(
        "example.com",
        source_candidate_packet_fingerprint=result[
            "source_candidate_packet_fingerprint"
        ],
        decisions=[{
            "relation_id": relation_id,
            "decision": "accept",
            "rationale": "The literal statement directly supports M1.",
        }],
        reviewer_id="human-reviewer",
        reviewed_at="2026-08-07T13:00:00+02:00",
        created_at="2026-08-07T11:00:00Z",
    )
    assert replay["packet_replayed"] is True
    assert replay["adoption_replayed"] is True
    assert replay["memory"]["canonical_memory_version"] == review[
        "memory"
    ]["canonical_memory_version"]

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="review is immutable"):
            conn.execute(
                """
                UPDATE b3s_history.evidence_vault_operational_relation_reviews
                SET rationale = 'tampered'
                """
            )

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="result is immutable",
        ):
            conn.execute(
                """
                UPDATE b3s_history.evidence_vault_operation_plans
                SET result_payload = result_payload || '{"tampered": true}'::jsonb
                WHERE scan_run_id = (
                    SELECT id FROM b3s_history.scan_runs WHERE source_scan_id = %s
                )
                """,
                ("crash-scan",),
            )

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="terminal Evidence Vault operation plans are immutable",
        ):
            conn.execute(
                """
                UPDATE b3s_history.evidence_vault_operation_plans
                SET status = 'superseded', superseded_at = now()
                WHERE scan_run_id = (
                    SELECT id FROM b3s_history.scan_runs WHERE source_scan_id = %s
                )
                """,
                ("crash-scan",),
            )

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="operation plans are append-only",
        ):
            conn.execute(
                """
                DELETE FROM b3s_history.evidence_vault_operation_plans
                WHERE scan_run_id = (
                    SELECT id FROM b3s_history.scan_runs WHERE source_scan_id = %s
                )
                """,
                ("crash-scan",),
            )

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "DELETE FROM b3s_history.scan_runs WHERE source_scan_id = %s",
                ("crash-scan",),
            )

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="append-only Evidence Vault journals cannot be truncated",
        ):
            conn.execute(
                "TRUNCATE b3s_history.evidence_vault_operation_plans CASCADE"
            )

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """
                WITH workspace AS (
                    SELECT id FROM b3s_history.workspaces WHERE slug = 'b3s'
                ), inserted_brand AS (
                    INSERT INTO b3s_history.brands (
                        id, workspace_id, canonical_domain, display_name,
                        canonical_url, first_observed_at, latest_observed_at
                    )
                    SELECT
                        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'::uuid,
                        workspace.id,
                        'other.example',
                        'Other',
                        'https://other.example',
                        now(),
                        now()
                    FROM workspace
                    RETURNING id
                )
                INSERT INTO b3s_history.evidence_vault_operational_relation_reviews (
                    id, brand_id, source_packet_id, source_packet_fingerprint,
                    relation_id, decision, reviewer_id, rationale,
                    review_request_fingerprint
                )
                SELECT
                    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'::uuid,
                    inserted_brand.id,
                    reviews.source_packet_id,
                    reviews.source_packet_fingerprint,
                    'c' || substr(reviews.relation_id, 2),
                    reviews.decision,
                    'cross-brand-reviewer',
                    'must be rejected by the composite source identity',
                    'd' || substr(reviews.review_request_fingerprint, 2)
                FROM inserted_brand
                CROSS JOIN LATERAL (
                    SELECT *
                    FROM b3s_history.evidence_vault_operational_relation_reviews
                    LIMIT 1
                ) AS reviews
                """
            )

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """
                INSERT INTO b3s_history.evidence_vault_operational_relation_reviews (
                    id, brand_id, source_packet_id, source_packet_fingerprint,
                    relation_id, decision, reviewer_id, rationale,
                    review_request_fingerprint
                )
                SELECT
                    'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'::uuid,
                    packets.brand_id,
                    packets.id,
                    packets.packet_fingerprint,
                    repeat('e', 64),
                    'accept',
                    'wrong-kind-reviewer',
                    'must be rejected because this is not a source packet',
                    repeat('f', 64)
                FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
                WHERE packets.packet_kind = 'operational_v2'
                LIMIT 1
                """
            )

    with psycopg.connect(
        os.environ["B3S_TEST_DATABASE_URL"], autocommit=True
    ) as conn:
        for packet_kind in (
            "operational_source_v2",
            "operational_reviewed_v2",
            "operational_v2",
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        """
                        ALTER TABLE
                            b3s_history.evidence_vault_canonical_memory_packets
                        DISABLE TRIGGER
                            evidence_vault_canonical_packets_append_only
                        """
                    )
                    conn.execute(
                        """
                        UPDATE b3s_history.evidence_vault_canonical_memory_packets
                        SET packet_payload = NULL
                        WHERE id = (
                            SELECT id
                            FROM b3s_history.evidence_vault_canonical_memory_packets
                            WHERE packet_kind = %s
                            LIMIT 1
                        )
                        """,
                        (packet_kind,),
                    )


def test_parent_advance_after_result_persistence_terminalizes_as_superseded() -> None:
    repository = _reset_repository()
    _persist_baseline(repository, "stale-result-scan")
    crashing = CrashOnceAfterResult(repository)
    with pytest.raises(RuntimeError, match="crash after result"):
        execute_vault_operation_plan(
            repository=crashing,
            source_scan_id="stale-result-scan",
            worker_id="worker-stale",
            llm=ExecutorLLM(),
        )
    stale = repository.get_capture_operation_plan("stale-result-scan")
    assert stale["status"] == "result_persisted"

    _persist_baseline(repository, "advancing-scan")
    execute_vault_operation_plan(
        repository=repository,
        source_scan_id="advancing-scan",
        worker_id="worker-advance",
        llm=ExecutorLLM(),
    )
    advancing = repository.get_capture_operation_plan("advancing-scan")
    relation_id = advancing["result_payload"]["basis_relations"][0][
        "relation_id"
    ]
    repository.review_and_adopt_evidence_vault_operational_source(
        "example.com",
        source_candidate_packet_fingerprint=advancing["result_payload"][
            "source_candidate_packet_fingerprint"
        ],
        decisions=[{
            "relation_id": relation_id,
            "decision": "accept",
            "rationale": "This direct statement supports the tile.",
        }],
        reviewer_id="human-reviewer",
        reviewed_at="2026-08-07T13:00:00+02:00",
        created_at="2026-08-07T11:00:00Z",
    )

    resumed = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="stale-result-scan",
        worker_id="worker-retry",
        llm=NoCallLLM(),
    )

    assert resumed["execution_status"] == "superseded"
    assert repository.get_capture_operation_plan("stale-result-scan")[
        "status"
    ] == "superseded"


def test_material_delta_only_persists_and_completes_without_packet() -> None:
    repository = _reset_repository()
    current = _row()
    current["content"] = "Changed acquisition marker"
    current["evidence_type"] = "acquisition.metadata"
    current["metadata"]["source_class"] = "acquisition_metadata"
    plan = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="baseline",
        current_evidence_records=[current],
    )
    plan["operations"]["create_candidate_packet"] = False
    delta_unsigned = {
        key: value
        for key, value in plan["delta"].items()
        if key != "delta_fingerprint"
    }
    delta_unsigned["requires_incremental_analysis"] = True
    plan["delta"] = {
        **delta_unsigned,
        "delta_fingerprint": canonical_fingerprint(
            plan["delta"]["schema_version"],
            delta_unsigned,
        ),
    }
    plan_unsigned = {
        key: value
        for key, value in plan.items()
        if key != "operation_plan_fingerprint"
    }
    plan["operation_plan_fingerprint"] = canonical_fingerprint(
        plan["schema_version"],
        plan_unsigned,
    )
    _persist_baseline(
        repository,
        "material-delta-scan",
        rows=[current],
        plan=plan,
    )

    with pytest.raises(RuntimeError, match="crash before finalize"):
        execute_vault_operation_plan(
            repository=CrashBeforeFinalize(repository),
            source_scan_id="material-delta-scan",
            worker_id="material-worker",
            llm=NoCallLLM(),
        )

    persisted = repository.get_capture_operation_plan("material-delta-scan")
    assert persisted["status"] == "result_persisted"
    assert persisted["result_payload"]["output_kind"] == "material_delta_only"
    assert persisted["candidate_packet_fingerprint"] is None

    import psycopg

    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        conn.execute(
            """
            UPDATE b3s_history.evidence_vault_operation_plans
            SET status = 'completed', completed_at = now()
            WHERE scan_run_id = (
                SELECT id
                FROM b3s_history.scan_runs
                WHERE source_scan_id = %s
            )
            """,
            ("material-delta-scan",),
        )
    completed = repository.get_capture_operation_plan("material-delta-scan")
    assert completed["status"] == "completed"
    assert completed["candidate_packet_fingerprint"] is None


def test_no_delta_result_must_equal_frozen_delta_and_output_stays_null() -> None:
    import psycopg

    repository = _reset_repository()
    _persist_baseline(repository, "seed-scan")
    execute_vault_operation_plan(
        repository=repository,
        source_scan_id="seed-scan",
        worker_id="seed-worker",
        llm=ExecutorLLM(),
    )
    seed = repository.get_capture_operation_plan("seed-scan")
    repository.review_and_adopt_evidence_vault_operational_source(
        "example.com",
        source_candidate_packet_fingerprint=seed["result_payload"][
            "source_candidate_packet_fingerprint"
        ],
        decisions=[{
            "relation_id": seed["result_payload"]["basis_relations"][0][
                "relation_id"
            ],
            "decision": "accept",
            "rationale": "The statement directly supports the condition.",
        }],
        reviewer_id="human-reviewer",
        reviewed_at="2026-08-07T13:00:00+02:00",
        created_at="2026-08-07T11:00:00Z",
    )
    memory = repository.get_evidence_vault_operational_memory("example.com")
    rows = [_row()]
    plan = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="incremental_refresh",
        current_evidence_records=rows,
        previous_capture_evidence_records=rows,
        known_evidence_records=rows,
        canonical_memory_version=memory["canonical_memory_version"],
        semantic_analysis_claimed_fingerprints=(
            row.fingerprint
            for row in canonical_evidence_rows(rows, subject_url="https://example.com")
        ),
    )
    observation = {
        "schema_version": "b3s-capture-observation-v1",
        "source_scan_id": "no-delta-scan",
        "source_run_id": "provider-no-delta",
        "brand_name": "Example",
        "url": "https://example.com",
        "observed_at": "2026-08-07T12:00:00Z",
        "recorded_at": "2026-08-07T12:00:01Z",
        "pipeline_version": "vault-incremental-capture-v1",
        "acquisition_state": "complete",
        "acquisition_summary": {"owned_pages": 1},
        "limitations": [],
        "capture_payload": {"raw_inputs": [{"source": "web", "payload": {"text": "Evidence"}}]},
        "evidence_records": rows,
        "acquisition_attempts": [{"provider": "web", "intent": "owned", "status": "success"}],
        "artifacts": [],
        "metadata": {
            "mode": "incremental_refresh",
            "analysis_status": "not_required",
            "operation_plan": plan,
            "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
        },
    }
    repository.persist_capture_observation(observation)
    claim = repository.claim_capture_operation_plan(
        "no-delta-scan", worker_id="no-delta-worker"
    )
    repository.mark_capture_operation_running(
        "no-delta-scan",
        worker_id="no-delta-worker",
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
    )
    bad_result = {
        "schema_version": "evidence-vault-operation-result-v1",
        "output_kind": "no_delta",
        "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
        "observation_hash": repository.get_capture_operation_plan(
            "no-delta-scan"
        )["observation_hash"],
        "canonical_memory_version": memory["canonical_memory_version"],
        "delta_fingerprint": plan["delta"]["delta_fingerprint"],
        "delta_summary": {"tampered": True},
        "authority": False,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    with pytest.raises(CaptureConflictError, match="frozen delta"):
        repository.persist_capture_operation_result(
            "no-delta-scan",
            worker_id="no-delta-worker",
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            result_payload=bad_result,
        )
    execution = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="no-delta-scan",
        worker_id="no-delta-worker",
        llm=NoCallLLM(),
    )
    assert execution["execution_status"] == "running"
    repository.fail_capture_operation_plan(
        "no-delta-scan",
        worker_id="no-delta-worker",
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        error="restart after rejected payload",
    )
    completed = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="no-delta-scan",
        worker_id="no-delta-worker-2",
        llm=NoCallLLM(),
    )
    assert completed["execution_status"] == "completed"
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.RaiseException)):
            conn.execute(
                """
                UPDATE b3s_history.evidence_vault_operation_plans
                SET candidate_packet_fingerprint = %s
                WHERE scan_run_id = (
                    SELECT id FROM b3s_history.scan_runs WHERE source_scan_id = %s
                )
                """,
                ("f" * 64, "no-delta-scan"),
            )


def test_concurrent_result_materializers_both_replay_to_completed() -> None:
    repository = _reset_repository()
    _persist_baseline(repository, "concurrent-materialize-scan")
    crashing = CrashOnceAfterResult(repository)
    with pytest.raises(RuntimeError, match="crash after result"):
        execute_vault_operation_plan(
            repository=crashing,
            source_scan_id="concurrent-materialize-scan",
            worker_id="semantic-worker",
            llm=ExecutorLLM(),
        )

    def recover(worker):
        return execute_vault_operation_plan(
            repository=repository,
            source_scan_id="concurrent-materialize-scan",
            worker_id=worker,
            llm=NoCallLLM(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovered = list(pool.map(recover, ["recovery-a", "recovery-b"]))

    assert [row["execution_status"] for row in recovered] == [
        "completed",
        "completed",
    ]
    assert repository.get_capture_operation_plan(
        "concurrent-materialize-scan"
    )["status"] == "completed"


def test_non_semantic_capture_rows_complete_without_llm_or_poisoned_retry() -> None:
    repository = _reset_repository()
    rows = [{
        "ref": "acquisition.0",
        "source": "web",
        "evidence_type": "acquisition.provider_status",
        "url": "https://example.com",
        "content": "Provider returned a partial response.",
        "confidence": "high",
        "metadata": {"source_class": "acquisition_metadata"},
    }]
    _persist_baseline(repository, "metadata-scan", rows=rows)

    completed = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="metadata-scan",
        worker_id="metadata-worker",
        llm=None,
    )

    assert completed["execution_status"] == "completed"
    result = repository.get_capture_operation_plan("metadata-scan")[
        "result_payload"
    ]
    assert result["selected_evidence_fingerprints"] == []
    assert result["evidence_work_dispositions"] == {}
    assert result["basis_relations"] == []


def test_identical_source_packet_can_bind_two_distinct_operations() -> None:
    import psycopg

    repository = _reset_repository()
    _persist_baseline(repository, "source-binding-a")
    _persist_baseline(repository, "source-binding-b")

    first = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="source-binding-a",
        worker_id="worker-a",
        llm=ExecutorLLM(),
    )
    second = execute_vault_operation_plan(
        repository=repository,
        source_scan_id="source-binding-b",
        worker_id="worker-b",
        llm=ExecutorLLM(),
    )

    assert first["execution_status"] == "completed"
    assert second["execution_status"] == "completed"
    first_result = repository.get_capture_operation_plan(
        "source-binding-a"
    )["result_payload"]
    second_result = repository.get_capture_operation_plan(
        "source-binding-b"
    )["result_payload"]
    assert first_result["source_candidate_packet_fingerprint"] == (
        second_result["source_candidate_packet_fingerprint"]
    )
    with psycopg.connect(os.environ["B3S_TEST_DATABASE_URL"]) as conn:
        stored = conn.execute(
            """
            SELECT count(*) AS row_count,
                   count(DISTINCT reference_resolution_fingerprint) AS binding_count
            FROM b3s_history.evidence_vault_canonical_memory_packets
            WHERE packet_kind = 'operational_source_v2'
              AND packet_fingerprint = %s
            """,
            (first_result["source_candidate_packet_fingerprint"],),
        ).fetchone()
    assert stored[0] == 2
    assert stored[1] == 2


def test_review_retry_recovers_implicit_timestamp_after_post_commit_failure() -> None:
    repository = _reset_repository()
    _persist_baseline(repository, "review-retry")
    execute_vault_operation_plan(
        repository=repository,
        source_scan_id="review-retry",
        worker_id="worker-a",
        llm=ExecutorLLM(),
    )
    result = repository.get_capture_operation_plan(
        "review-retry"
    )["result_payload"]
    decisions = [{
        "relation_id": result["basis_relations"][0]["relation_id"],
        "decision": "accept",
        "rationale": "The literal statement directly supports M1.",
    }]
    register = repository.register_evidence_vault_operational_memory_packet

    def fail_after_review_commit(*_args, **_kwargs):
        raise RuntimeError("post-review materialization failed")

    repository.register_evidence_vault_operational_memory_packet = (
        fail_after_review_commit
    )
    with pytest.raises(RuntimeError, match="post-review materialization failed"):
        repository.review_and_adopt_evidence_vault_operational_source(
            "example.com",
            source_candidate_packet_fingerprint=result[
                "source_candidate_packet_fingerprint"
            ],
            decisions=decisions,
            reviewer_id="human-reviewer",
        )
    repository.register_evidence_vault_operational_memory_packet = register

    recovered = repository.review_and_adopt_evidence_vault_operational_source(
        "example.com",
        source_candidate_packet_fingerprint=result[
            "source_candidate_packet_fingerprint"
        ],
        decisions=decisions,
        reviewer_id="human-reviewer",
    )

    assert recovered["packet_replayed"] is False
    assert recovered["adoption"] is not None
    assert recovered["memory"] is not None
    replay = repository.review_and_adopt_evidence_vault_operational_source(
        "example.com",
        source_candidate_packet_fingerprint=result[
            "source_candidate_packet_fingerprint"
        ],
        decisions=decisions,
        reviewer_id="human-reviewer",
    )
    assert replay["review_request_fingerprint"] == recovered[
        "review_request_fingerprint"
    ]
    assert replay["packet_replayed"] is True
    assert replay["adoption_replayed"] is True
