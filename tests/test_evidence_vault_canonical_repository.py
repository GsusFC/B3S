from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import os
from threading import Barrier
from uuid import uuid4

import pytest

from src.services.evidence_vault_canonical_authority import (
    CanonicalMemoryPromotionCommand,
    EvidenceVaultCanonicalPromotionConflictError,
    promotion_request_fingerprint,
)
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_tile_contract_registry,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_postgres_canonical_promotion_is_durable_idempotent_and_serialized() -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1":
        raise RuntimeError("Set B3S_ALLOW_SCHEMA_DROP=1 only for a disposable PostgreSQL database")
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    repository = PostgresHistoryRepository(dsn)
    try:
        repository.migrate()
        _insert_brand(dsn)

        baseline = _baseline_packet()
        stored, replayed = repository.register_evidence_vault_canonical_memory_packet(
            "example.com",
            baseline,
            resolved_references=_references(baseline),
        )
        assert replayed is False
        assert stored["authority_state"] == "pending_review"
        assert stored["authority"] is False
        assert stored["packet"] == baseline
        assert repository.get_or_create_evidence_vault_canonical_score_evaluation(
            "example.com"
        ) == (None, False)

        repeated, replayed = repository.register_evidence_vault_canonical_memory_packet(
            "example.com",
            baseline,
            resolved_references=_references(baseline),
        )
        assert replayed is True
        assert repeated["packet"] == baseline

        baseline_command = _command(baseline, key_hash="1" * 64)
        baseline_event, replayed = repository.append_evidence_vault_canonical_memory_promotion(
            "example.com",
            baseline_command,
        )
        assert replayed is False
        assert baseline_event["sequence"] == 1
        assert baseline_event["authority"] is True
        assert baseline_event["authority_scope"] == "b3s-vault"
        assert baseline_event["production_runtime_effect"] is False
        assert baseline_event["scanner_runtime_effect"] is False

        baseline_evaluation, replayed = (
            repository.get_or_create_evidence_vault_canonical_score_evaluation(
                "example.com"
            )
        )
        assert replayed is False
        assert baseline_evaluation is not None
        assert baseline_evaluation["score"] == 1
        assert baseline_evaluation["authority_scope"] == "b3s-vault"
        assert baseline_evaluation["scanner_runtime_effect"] is False
        assert baseline_evaluation["production_runtime_effect"] is False
        repeated_evaluation, replayed = (
            repository.get_or_create_evidence_vault_canonical_score_evaluation(
                "example.com"
            )
        )
        assert replayed is True
        assert repeated_evaluation == baseline_evaluation
        assert (
            repository.get_evidence_vault_canonical_score_evaluation(
                "example.com"
            )
            == baseline_evaluation
        )

        _insert_brand(dsn, workspace_slug="mirror")
        mirror = PostgresHistoryRepository(dsn)
        mirror.register_evidence_vault_canonical_memory_packet(
            "example.com",
            baseline,
            resolved_references=_references(baseline),
            workspace_slug="mirror",
        )
        mirror.append_evidence_vault_canonical_memory_promotion(
            "example.com",
            baseline_command,
            workspace_slug="mirror",
        )
        mirror_evaluation, replayed = (
            mirror.get_or_create_evidence_vault_canonical_score_evaluation(
                "example.com",
                workspace_slug="mirror",
            )
        )
        assert replayed is False
        assert mirror_evaluation is not None
        assert mirror_evaluation["evaluation_identity"] == (
            baseline_evaluation["evaluation_identity"]
        )

        repeated_event, replayed = repository.append_evidence_vault_canonical_memory_promotion(
            "example.com",
            baseline_command,
        )
        assert replayed is True
        assert repeated_event == baseline_event

        with pytest.raises(
            EvidenceVaultCanonicalPromotionConflictError,
            match="fingerprint does not match",
        ):
            repository.append_evidence_vault_canonical_memory_promotion(
                "example.com",
                replace(baseline_command, reviewer_id="different-reviewer"),
            )

        current = PostgresHistoryRepository(dsn).get_evidence_vault_canonical_memory("example.com")
        assert current is not None
        assert current["promotion_sequence"] == 1
        assert current["canonical_memory_version"] == baseline_event["promoted_canonical_memory_version"]

        candidate_a = _incremental_packet(
            baseline,
            parent=current["canonical_memory_version"],
            tile_id="M1",
            delta_kind="strengthened",
            basis=[_accepted_basis(1, "supports"), _accepted_basis(2, "supports")],
            candidate_memory_version=HASH_F,
        )
        candidate_b = _incremental_packet(
            baseline,
            parent=current["canonical_memory_version"],
            tile_id="M2",
            delta_kind="candidate_update",
            basis=[_accepted_basis(3, "supports")],
            candidate_memory_version="2" * 64,
        )
        for packet in (candidate_a, candidate_b):
            repository.register_evidence_vault_canonical_memory_packet(
                "example.com",
                packet,
                resolved_references=_references(packet),
            )

        commands = [
            _command(candidate_a, key_hash="3" * 64),
            _command(candidate_b, key_hash="4" * 64),
        ]
        barrier = Barrier(2)

        def promote(command: CanonicalMemoryPromotionCommand):
            barrier.wait()
            try:
                event, was_replayed = PostgresHistoryRepository(dsn).append_evidence_vault_canonical_memory_promotion(
                    "example.com",
                    command,
                )
                return ("promoted", event, was_replayed)
            except EvidenceVaultCanonicalPromotionConflictError as exc:
                return (
                    "conflict",
                    exc.current_canonical_memory_version,
                    exc.existing_event_id,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(promote, commands))

        promoted = [outcome for outcome in outcomes if outcome[0] == "promoted"]
        conflicts = [outcome for outcome in outcomes if outcome[0] == "conflict"]
        assert len(promoted) == 1
        assert len(conflicts) == 1
        assert promoted[0][1]["sequence"] == 2
        assert promoted[0][2] is False
        assert conflicts[0][1] == promoted[0][1]["promoted_canonical_memory_version"]
        winner_index = 0 if outcomes[0][0] == "promoted" else 1

        restarted = PostgresHistoryRepository(dsn)
        evolved = restarted.get_evidence_vault_canonical_memory("example.com")
        assert evolved is not None
        assert evolved["promotion_sequence"] == 2
        assert evolved["canonical_memory_version"] == promoted[0][1]["promoted_canonical_memory_version"]

        evolved_evaluation, replayed = (
            restarted.get_or_create_evidence_vault_canonical_score_evaluation(
                "example.com"
            )
        )
        assert replayed is False
        assert evolved_evaluation is not None
        assert evolved_evaluation["evaluation_identity"] != (
            baseline_evaluation["evaluation_identity"]
        )
        if winner_index == 0:
            assert evolved_evaluation["score"] == 1
            assert evolved_evaluation["score_input_fingerprint"] == (
                baseline_evaluation["score_input_fingerprint"]
            )
            assert evolved_evaluation[
                "reused_from_evaluation_identity"
            ] == baseline_evaluation["evaluation_identity"]
        else:
            assert evolved_evaluation["score"] == 2
            assert evolved_evaluation["score_input_fingerprint"] != (
                baseline_evaluation["score_input_fingerprint"]
            )
            assert evolved_evaluation[
                "reused_from_evaluation_identity"
            ] is None

        replay, replayed = restarted.append_evidence_vault_canonical_memory_promotion(
            "example.com",
            commands[winner_index],
        )
        assert replayed is True
        assert replay == promoted[0][1]

        with pytest.raises(psycopg.Error):
            with psycopg.connect(dsn) as conn:
                conn.execute(
                    """
                    UPDATE b3s_history.evidence_vault_canonical_memory_packets
                    SET authority_state = 'pending_review'
                    """
                )
        with pytest.raises(psycopg.Error):
            with psycopg.connect(dsn) as conn:
                conn.execute(
                    """
                    DELETE FROM b3s_history.evidence_vault_canonical_memory_promotion_events
                    """
                )
        with pytest.raises(psycopg.Error):
            with psycopg.connect(dsn) as conn:
                conn.execute(
                    """
                    UPDATE b3s_history.evidence_vault_canonical_score_evaluations
                    SET score = score
                    """
                )
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")


def _insert_brand(dsn: str, *, workspace_slug: str = "b3s") -> None:
    import psycopg

    workspace_id = uuid4()
    brand_id = uuid4()
    now = datetime.now(timezone.utc)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            INSERT INTO b3s_history.workspaces (id, slug, name)
            VALUES (%s, %s, %s)
            """,
            (workspace_id, workspace_slug, workspace_slug.upper()),
        )
        conn.execute(
            """
            INSERT INTO b3s_history.brands (
                id, workspace_id, canonical_domain, display_name,
                canonical_url, first_observed_at, latest_observed_at
            ) VALUES (
                %s, %s, 'example.com', 'Example', 'https://example.com', %s, %s
            )
            """,
            (brand_id, workspace_id, now, now),
        )


def _baseline_packet() -> dict:
    tiles = [build_candidate_tile(tile_id=row["tile_id"]) for row in build_tile_contract_registry()["tiles"]]
    tiles[0] = build_candidate_tile(
        tile_id="M1",
        basis=[_accepted_basis(1, "supports")],
    )
    return _packet(tiles, parent=None, candidate_memory_version=HASH_A)


def _incremental_packet(
    baseline: dict,
    *,
    parent: str,
    tile_id: str,
    delta_kind: str,
    basis: list[dict],
    candidate_memory_version: str,
) -> dict:
    tiles = build_incremental_candidate_tiles(
        previous_candidate_tiles=baseline["candidate_tiles"],
        tile_updates=[
            {
                "tile_id": tile_id,
                "delta_kind": delta_kind,
                "basis": basis,
            }
        ],
    )
    return _packet(
        tiles,
        parent=parent,
        candidate_memory_version=candidate_memory_version,
    )


def _packet(
    tiles: list[dict],
    *,
    parent: str | None,
    candidate_memory_version: str,
) -> dict:
    return build_candidate_packet(
        brand_identity="example.com",
        parent_canonical_memory_version=parent,
        candidate_memory_version=candidate_memory_version,
        accepted_memory_candidate_version=HASH_B,
        reviewed_memory_candidate_version=HASH_C,
        review_packet_set_fingerprint=HASH_D,
        aggregation_policy_fingerprint=(
            canonical_aggregation_policy_fingerprint()
        ),
        candidate_tiles=tiles,
        coverage_summary={},
    )


def _references(packet: dict) -> dict[str, str]:
    manifest = packet["manifest"]
    return {
        field: manifest[field]
        for field in (
            "candidate_memory_version",
            "accepted_memory_candidate_version",
            "reviewed_memory_candidate_version",
            "review_packet_set_fingerprint",
            "rubric_version",
            "tile_contract_registry_fingerprint",
            "reducer_policy_fingerprint",
            "aggregation_policy_fingerprint",
        )
    }


def _command(packet: dict, *, key_hash: str) -> CanonicalMemoryPromotionCommand:
    reviewer_id = "gsus"
    reviewed_at = "2026-08-04T20:00:00+02:00"
    rationale = "Exact candidate packet reviewed for Vault-only promotion."
    return CanonicalMemoryPromotionCommand(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=packet["manifest"]["parent_canonical_memory_version"],
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        rationale=rationale,
        idempotency_key_hash=key_hash,
        request_fingerprint=promotion_request_fingerprint(
            candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
            parent_canonical_memory_version=packet["manifest"]["parent_canonical_memory_version"],
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            rationale=rationale,
        ),
    )


def _accepted_basis(seed: int, polarity: str) -> dict:
    character = format(seed, "x")[-1]
    return {
        "relation_id": character * 64,
        "evidence_id": format(seed + 8, "x")[-1] * 64,
        "source_identity_id": format(seed + 4, "x")[-1] * 64,
        "claim_id": HASH_C,
        "polarity": polarity,
        "review_status": "accepted",
        "decision_event_id": f"review-{seed}",
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }
