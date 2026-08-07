from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import json
import os
from pathlib import Path

import pytest

from src.history.models import OperationalAdoptionCommand
from src.services.evidence_vault_authority_profiles import (
    REVIEWED_BASIS_PROFILE_ID,
    build_initial_authority_profile_matrix,
    evaluate_reviewed_basis_authority,
)
from src.services.evidence_vault_canonical_authority import (
    CanonicalMemoryPromotionCommand,
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
from src.services.evidence_vault_coverage_supplement import (
    build_coverage_supplement_request,
    execute_coverage_supplement,
)
from src.services.evidence_vault_exact_relation_supplement import (
    build_exact_relation_supplement_artifact,
)
from src.services.evidence_vault_operational_authority import (
    EvidenceVaultOperationalAuthorityError,
    adoption_request_fingerprint,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)


def test_operational_v2_reuses_existing_ledgers_and_survives_restart() -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        pytest.fail("B3S_TEST_ALLOW_SCHEMA_DROP=1 is required")
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    repository = PostgresHistoryRepository(dsn)
    repository.migrate()
    repository.persist_capture_observation(_capture())

    baseline_candidates = _candidates([_basis("m1-a")])
    baseline_source = _source_packet(baseline_candidates, seed="baseline")
    repository.register_evidence_vault_canonical_memory_packet(
        "example.com",
        baseline_source,
        resolved_references=_references(baseline_source),
    )
    legacy_event, legacy_replayed = (
        repository.append_evidence_vault_canonical_memory_promotion(
            "example.com",
            _canonical_command(baseline_source),
        )
    )
    assert legacy_replayed is False
    assert legacy_event["sequence"] == 1
    assert repository.get_evidence_vault_operational_memory("example.com") is None
    baseline = _packet(
        baseline_candidates,
        current=None,
        source=baseline_source,
    )
    stored, replayed = repository.register_evidence_vault_operational_memory_packet(
        "example.com",
        baseline,
    )
    assert replayed is False
    assert stored["packet"] == baseline
    assert repository.get_or_create_evidence_vault_operational_score_evaluation(
        "example.com"
    ) == (None, False)

    repeated, replayed = repository.register_evidence_vault_operational_memory_packet(
        "example.com",
        baseline,
    )
    assert replayed is True
    assert repeated["packet"] == baseline

    command = _command(baseline, parent=None, seed="baseline")
    event, replayed = repository.append_evidence_vault_operational_adoption(
        "example.com",
        command,
    )
    assert replayed is False
    assert event["sequence"] == 1
    assert event["adopted_by"] == "policy"

    restarted = PostgresHistoryRepository(dsn)
    memory = restarted.get_evidence_vault_operational_memory("example.com")
    assert memory is not None
    assert memory["canonical_memory_version"] == event[
        "promoted_canonical_memory_version"
    ]
    assert memory["scoring_projection"]["coverage"]["accepted_tile_count"] == 1

    evaluation, replayed = (
        restarted.get_or_create_evidence_vault_operational_score_evaluation(
            "example.com"
        )
    )
    assert replayed is False
    assert evaluation is not None
    assert evaluation["score"] == 1
    assert evaluation["authority_coverage"]["unresolved_tile_count"] == 79
    repeated_evaluation, replayed = (
        restarted.get_or_create_evidence_vault_operational_score_evaluation(
            "example.com"
        )
    )
    assert replayed is True
    assert repeated_evaluation == evaluation

    strengthened_candidates = build_incremental_candidate_tiles(
        previous_candidate_tiles=baseline_source["candidate_tiles"],
        tile_updates=[
            {
                "tile_id": "M1",
                "delta_kind": "strengthened",
                "basis": [_basis("m1-a"), _basis("m1-b")],
            }
        ],
    )
    strengthened_source = _source_packet(
        strengthened_candidates,
        seed="strengthened",
        parent=legacy_event["promoted_canonical_memory_version"],
    )
    restarted.register_evidence_vault_canonical_memory_packet(
        "example.com",
        strengthened_source,
        resolved_references=_references(strengthened_source),
    )
    strengthened = _packet(
        strengthened_candidates,
        current=memory,
        source=strengthened_source,
    )
    restarted.register_evidence_vault_operational_memory_packet(
        "example.com",
        strengthened,
    )
    second_event, replayed = restarted.append_evidence_vault_operational_adoption(
        "example.com",
        _command(
            strengthened,
            parent=memory["canonical_memory_version"],
            seed="strengthened",
        ),
    )
    assert replayed is False
    assert second_event["sequence"] == 2

    second, replayed = (
        PostgresHistoryRepository(dsn)
        .get_or_create_evidence_vault_operational_score_evaluation("example.com")
    )
    assert replayed is False
    assert second is not None
    assert second["canonical_memory_version"] != evaluation[
        "canonical_memory_version"
    ]
    assert second["evaluation_identity"] != evaluation["evaluation_identity"]
    assert second["score_input_fingerprint"] == evaluation[
        "score_input_fingerprint"
    ]
    assert second["reused_from_evaluation_identity"] == evaluation[
        "evaluation_identity"
    ]
    assert second["score"] == evaluation["score"] == 1

    replay, replayed = restarted.append_evidence_vault_operational_adoption(
        "example.com",
        _command(
            strengthened,
            parent=memory["canonical_memory_version"],
            seed="strengthened",
        ),
    )
    assert replayed is True
    assert replay == second_event

    with psycopg.connect(dsn) as conn:
        counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM b3s_history.evidence_vault_canonical_memory_packets
                WHERE packet_kind = 'operational_v2') AS packets,
              (SELECT count(*) FROM b3s_history.evidence_vault_canonical_memory_promotion_events
                WHERE adoption_kind = 'operational_v2') AS adoptions,
              (SELECT count(*) FROM b3s_history.evidence_vault_canonical_score_evaluations
                WHERE evaluation_kind = 'operational_v2') AS evaluations
            """
        ).fetchone()
    assert tuple(counts) == (2, 2, 2)


class _PersistedCoverageLLM:
    api_key = "persisted-test"

    def __init__(self, relations: list[dict]) -> None:
        self._relations = relations

    def _call_json(self, system, user, **kwargs):
        del system, user, kwargs
        return {"relations": self._relations}


def test_coverage_supplement_registers_reviews_and_adopts_n_plus_one() -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        pytest.fail("B3S_TEST_ALLOW_SCHEMA_DROP=1 is required")
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    repository = PostgresHistoryRepository(dsn)
    repository.migrate()
    brand = "causaprima.ai"
    repository.persist_capture_observation(
        _capture(
            domain=brand,
            source_scan_id="coverage-supplement-capture",
        )
    )
    baseline_candidates = _candidates([_basis("baseline-m1")])
    baseline_source = _source_packet(
        baseline_candidates,
        seed="coverage-baseline",
        brand=brand,
    )
    repository.register_evidence_vault_canonical_memory_packet(
        brand,
        baseline_source,
        resolved_references=_references(baseline_source),
    )
    repository.append_evidence_vault_canonical_memory_promotion(
        brand,
        _canonical_command(baseline_source),
    )
    baseline_packet = _packet(
        baseline_candidates,
        current=None,
        source=baseline_source,
        brand=brand,
    )
    repository.register_evidence_vault_operational_memory_packet(
        brand,
        baseline_packet,
    )
    baseline_event, _ = repository.append_evidence_vault_operational_adoption(
        brand,
        _command(baseline_packet, parent=None, seed="coverage-baseline"),
    )
    current = repository.get_evidence_vault_operational_memory(brand)
    assert current is not None

    root = Path(__file__).parents[1]
    frozen = json.loads(
        (
            root
            / "audits/evidence_vault_field_validation_v1"
            / "causa-prima-coverage-supplement-v2.json"
        ).read_text(encoding="utf-8")
    )
    pack = json.loads(
        (
            root
            / "fixtures/evidence_vault_field_validation_v1"
            / "causa_prima-a8ba05137817-normalized-evidence-pack.json"
        ).read_text(encoding="utf-8")
    )
    evidence_rows = frozen["result"]["evidence_snapshot"]
    request = build_coverage_supplement_request(
        brand_identity=brand,
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version=current["canonical_memory_version"],
        source_evidence_pack_sha256=frozen["request"][
            "source_evidence_pack_sha256"
        ],
        evidence_rows=evidence_rows,
        tile_shortlists={
            row["evidence_fingerprint"]: row["tile_ids"]
            for row in frozen["request"]["evidence_workset"]
        },
        target_tile_ids=frozen["request"]["target_tile_ids"],
        rationale=frozen["request"]["rationale"],
    )
    result = execute_coverage_supplement(
        request,
        evidence_rows=evidence_rows,
        llm=_PersistedCoverageLLM(
            frozen["result"]["relation_proposal"]["relations"]
        ),
    )
    artifact = {
        **frozen,
        "execution_mode": "live_provider",
        "replay_source_result_fingerprint": None,
        "replay_source_file_sha256": None,
        "replay_source_file": None,
        "request": request,
        "result": result,
        "worksheet_relation_count": len(result["basis_relations"]),
    }
    source, replayed = (
        repository.register_evidence_vault_coverage_supplement_source_packet(
            brand,
            artifact,
            evidence_pack=pack,
        )
    )
    assert replayed is False
    source_by_id = {
        row["tile_id"]: row for row in source["packet"]["candidate_tiles"]
    }
    assert len(source_by_id["MG10"]["basis"]) == 4
    assert {
        row["review_status"] for row in source_by_id["MG10"]["basis"]
    } == {"unreviewed"}
    assert source_by_id["M1"]["basis"][0]["review_status"] == "accepted"
    repeated_source, replayed = (
        repository.register_evidence_vault_coverage_supplement_source_packet(
            brand,
            artifact,
            evidence_pack=pack,
        )
    )
    assert replayed is True
    assert repeated_source == source

    decisions = [
        {
            "relation_id": row["relation_id"],
            "decision": "accept",
            "rationale": "Exact external funding relation accepted.",
        }
        for row in result["basis_relations"]
    ]
    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="resolve every pending",
    ):
        repository.review_and_adopt_evidence_vault_operational_source(
            brand,
            source_candidate_packet_fingerprint=source["packet"][
                "candidate_packet_fingerprint"
            ],
            decisions=decisions[:-1],
            reviewer_id="gsus",
            reviewed_at="2026-08-07T11:43:10+02:00",
        )
    review = repository.review_and_adopt_evidence_vault_operational_source(
        brand,
        source_candidate_packet_fingerprint=source["packet"][
            "candidate_packet_fingerprint"
        ],
        decisions=decisions,
        reviewer_id="gsus",
        reviewed_at="2026-08-07T11:43:10+02:00",
        created_at="2026-08-07T11:43:10+02:00",
    )
    assert review["adoption"] is not None
    assert review["adoption"]["sequence"] == 2
    assert review["adoption"]["previous_event_id"] == baseline_event["event_id"]
    memory = review["memory"]
    assert memory is not None
    assert {row["tile_id"] for row in memory["content"]["accepted_tiles"]} == {
        "M1",
        "MG10",
    }
    assert sum(
        len(row["basis"]) for row in memory["content"]["accepted_tiles"]
    ) == 5
    score, score_replayed = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    assert score_replayed is False
    assert score is not None
    assert score["score"] == 3
    assert score["authority_coverage"]["accepted_tile_count"] == 2
    assert score["production_runtime_effect"] is False
    assert score["scanner_runtime_effect"] is False

    replay_review = (
        repository.review_and_adopt_evidence_vault_operational_source(
            brand,
            source_candidate_packet_fingerprint=source["packet"][
                "candidate_packet_fingerprint"
            ],
            decisions=decisions,
            reviewer_id="gsus",
            reviewed_at="2026-08-07T11:43:10+02:00",
            created_at="2026-08-07T11:43:10+02:00",
        )
    )
    assert replay_review["packet_replayed"] is True
    assert replay_review["adoption_replayed"] is True
    score_replay, score_replayed = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    assert score_replayed is True
    assert score_replay == score
    replay_after_adoption, replayed = (
        repository.register_evidence_vault_coverage_supplement_source_packet(
            brand,
            artifact,
            evidence_pack=pack,
        )
    )
    assert replayed is True
    assert replay_after_adoption == source

    assessment_path = (
        root
        / "audits/evidence_vault_field_validation_v1"
        / "causa-prima-missing-tile-independent-audit.json"
    )
    assessment_worksheet_path = (
        root
        / "audits/evidence_vault_field_validation_v1"
        / "causa-prima-missing-tile-independent-review.csv"
    )
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    with assessment_worksheet_path.open(encoding="utf-8", newline="") as handle:
        assessment_rows = list(csv.DictReader(handle))
    exact_artifact = build_exact_relation_supplement_artifact(
        brand_identity=brand,
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version=memory["canonical_memory_version"],
        selected_tile_ids=["C7", "MG1", "MG3", "MG5"],
        assessment_artifact=assessment,
        assessment_review_rows=assessment_rows,
        assessment_worksheet_sha256=hashlib.sha256(
            assessment_worksheet_path.read_bytes()
        ).hexdigest(),
        evidence_pack=pack,
    )
    exact_source, exact_replayed = (
        repository.register_evidence_vault_exact_relation_supplement_source_packet(
            brand,
            exact_artifact,
            assessment_artifact=assessment,
            assessment_review_rows=assessment_rows,
            assessment_worksheet_sha256=hashlib.sha256(
                assessment_worksheet_path.read_bytes()
            ).hexdigest(),
            evidence_pack=pack,
        )
    )
    assert exact_replayed is False
    exact_pending = [
        relation
        for tile in exact_source["packet"]["candidate_tiles"]
        for relation in tile["basis"]
        if relation["review_status"] == "unreviewed"
    ]
    assert len(exact_pending) == 5
    exact_replay, exact_replayed = (
        repository.register_evidence_vault_exact_relation_supplement_source_packet(
            brand,
            exact_artifact,
            assessment_artifact=assessment,
            assessment_review_rows=assessment_rows,
            assessment_worksheet_sha256=hashlib.sha256(
                assessment_worksheet_path.read_bytes()
            ).hexdigest(),
            evidence_pack=pack,
        )
    )
    assert exact_replayed is True
    assert exact_replay == exact_source
    mixed_decisions = [
        {
            "relation_id": relation["relation_id"],
            "decision": "accept",
            "rationale": "Exact relation scope accepted.",
        }
        for relation in exact_pending
    ]
    c7 = next(
        group for group in exact_artifact["groups"] if group["tile_id"] == "C7"
    )
    c7_first = c7["relations"][0]["relation_id"]
    next(
        row for row in mixed_decisions if row["relation_id"] == c7_first
    )["decision"] = "reject"
    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="decision group is invalid",
    ):
        repository.review_and_adopt_evidence_vault_operational_source(
            brand,
            source_candidate_packet_fingerprint=exact_source["packet"][
                "candidate_packet_fingerprint"
            ],
            decisions=mixed_decisions,
            reviewer_id="gsus",
            reviewed_at="2026-08-07T12:00:00+02:00",
        )
    with psycopg.connect(dsn) as conn:
        exact_review_count = conn.execute(
            """
            SELECT count(*)
            FROM b3s_history.evidence_vault_operational_relation_reviews
            WHERE source_packet_fingerprint = %s
            """,
            (exact_source["packet"]["candidate_packet_fingerprint"],),
        ).fetchone()[0]
    assert exact_review_count == 0

    accepted_decisions = [
        {
            "relation_id": relation["relation_id"],
            "decision": "accept",
            "rationale": (
                "Integration-only exact relation and composite group accepted."
            ),
        }
        for relation in exact_pending
    ]
    exact_review = repository.review_and_adopt_evidence_vault_operational_source(
        brand,
        source_candidate_packet_fingerprint=exact_source["packet"][
            "candidate_packet_fingerprint"
        ],
        decisions=accepted_decisions,
        reviewer_id="integration-fixture-reviewer",
        reviewed_at="2026-08-07T12:05:00+02:00",
    )
    assert exact_review["adoption"] is not None
    assert exact_review["adoption"]["sequence"] == 3
    exact_memory = exact_review["memory"]
    assert exact_memory is not None
    assert {row["tile_id"] for row in exact_memory["content"]["accepted_tiles"]} == {
        "C7",
        "M1",
        "MG1",
        "MG10",
        "MG3",
        "MG5",
    }
    c7_memory = next(
        row
        for row in exact_memory["content"]["accepted_tiles"]
        if row["tile_id"] == "C7"
    )
    assert len(c7_memory["basis"]) == 2
    assert {row["claim_id"] for row in c7_memory["basis"]} == {c7["group_id"]}
    exact_score, _ = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    assert exact_score is not None
    assert exact_score["score"] == 11
    assert exact_score["authority_coverage"]["accepted_tile_count"] == 6

    exact_review_replay = (
        repository.review_and_adopt_evidence_vault_operational_source(
            brand,
            source_candidate_packet_fingerprint=exact_source["packet"][
                "candidate_packet_fingerprint"
            ],
            decisions=accepted_decisions,
            reviewer_id="integration-fixture-reviewer",
            reviewed_at="2026-08-07T12:05:00+02:00",
        )
    )
    assert exact_review_replay["packet_replayed"] is True
    assert exact_review_replay["adoption_replayed"] is True
    assert exact_review_replay["memory"] == exact_memory

    tampered = deepcopy(artifact)
    tampered["limitations"][0] += " changed"
    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="replay differs",
    ):
        repository.register_evidence_vault_coverage_supplement_source_packet(
            brand,
            tampered,
            evidence_pack=pack,
        )


def _packet(
    candidates: list[dict],
    *,
    current: dict | None,
    source: dict,
    brand: str = "example.com",
) -> dict:
    m1 = next(row for row in candidates if row["tile_id"] == "M1")
    decision = evaluate_reviewed_basis_authority(candidate_tile=m1)
    return build_operational_memory_packet(
        brand_identity=brand,
        source_candidate_packet_fingerprint=source[
            "candidate_packet_fingerprint"
        ],
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions={
            "M1": {
                "authority_state": "accepted",
                "review_state": "none",
                "authority_profile_id": REVIEWED_BASIS_PROFILE_ID,
                "authority_source": "policy",
                "policy_decision": decision,
            }
        },
        current_accepted_tiles=(
            current["content"]["accepted_tiles"] if current is not None else ()
        ),
        parent_canonical_memory_version=(
            current["canonical_memory_version"] if current is not None else None
        ),
    )


def _source_packet(
    candidates: list[dict],
    *,
    seed: str,
    parent: str | None = None,
    brand: str = "example.com",
) -> dict:
    return build_candidate_packet(
        brand_identity=brand,
        parent_canonical_memory_version=parent,
        candidate_memory_version=_digest(f"{seed}-candidate-memory"),
        accepted_memory_candidate_version=_digest(f"{seed}-accepted-memory"),
        reviewed_memory_candidate_version=_digest(f"{seed}-reviewed-memory"),
        review_packet_set_fingerprint=_digest(f"{seed}-review-packets"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
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


def _candidates(m1_basis: list[dict]) -> list[dict]:
    return [
        build_candidate_tile(
            tile_id=str(row["tile_id"]),
            basis=m1_basis if str(row["tile_id"]) == "M1" else [],
            previous_canonical_state=(
                "ok" if str(row["tile_id"]) == "M1" and len(m1_basis) > 1 else None
            ),
            delta_kind=(
                "strengthened"
                if str(row["tile_id"]) == "M1" and len(m1_basis) > 1
                else None
            ),
        )
        for row in build_tile_contract_registry()["tiles"]
    ]


def _canonical_command(packet: dict) -> CanonicalMemoryPromotionCommand:
    reviewer_id = "migration-reviewer"
    reviewed_at = "2026-08-06T13:55:00+02:00"
    rationale = "Promote v1 before explicit operational-v2 projection."
    request = promotion_request_fingerprint(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=None,
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        rationale=rationale,
    )
    return CanonicalMemoryPromotionCommand(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=None,
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        rationale=rationale,
        idempotency_key_hash=_digest("legacy-v1-promotion"),
        request_fingerprint=request,
    )


def _command(
    packet: dict,
    *,
    parent: str | None,
    seed: str,
) -> OperationalAdoptionCommand:
    policy = str(
        build_initial_authority_profile_matrix()[
            "authority_matrix_fingerprint"
        ]
    )
    request = adoption_request_fingerprint(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=parent,
        adopted_by="policy",
        actor_id="automatic-operational-policy-v1",
        policy_fingerprint=policy,
    )
    return OperationalAdoptionCommand(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=parent,
        adopted_by="policy",
        actor_id="automatic-operational-policy-v1",
        policy_fingerprint=policy,
        created_at="2026-08-06T14:00:00+02:00",
        idempotency_key_hash=_digest(f"idempotency-{seed}"),
        request_fingerprint=request,
    )


def _basis(seed: str) -> dict:
    return {
        "relation_id": _digest(f"{seed}-relation"),
        "evidence_id": _digest(f"{seed}-evidence"),
        "source_identity_id": _digest(f"{seed}-source"),
        "claim_id": None,
        "polarity": "supports",
        "review_status": "accepted",
        "decision_event_id": f"review-{seed}",
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _capture(
    *,
    domain: str = "example.com",
    source_scan_id: str = "operational-v2-capture",
) -> dict:
    return {
        "schema_version": "b3s-capture-observation-v1",
        "source_scan_id": source_scan_id,
        "brand_name": domain,
        "url": f"https://{domain}",
        "observed_at": "2026-08-06T10:00:00Z",
        "pipeline_version": "vault-incremental-v1",
        "acquisition_state": "complete",
        "capture_payload": {"raw_inputs": [{"source": "web"}]},
        "evidence_records": [],
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
