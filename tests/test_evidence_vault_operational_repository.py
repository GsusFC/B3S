from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import UUID

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
from src.services.evidence_vault_c7_cutover import load_c7_runtime_projection
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_tile_contract_registry,
    canonical_fingerprint,
    canonical_json,
)
from src.services.evidence_vault_coverage_supplement import (
    build_coverage_supplement_request,
    execute_coverage_supplement,
)
from src.services.evidence_memory_identity_v2 import (
    project_evidence_memory_row_identity,
)
from src.services.evidence_vault_exact_relation_supplement import (
    _ASSESSMENT_MUTABLE_FIELDS,
    _assessment_review_immutable,
    build_exact_relation_supplement_artifact,
)
from src.services.evidence_vault_incremental_executor import (
    execute_vault_operation_plan,
)
from src.services.evidence_vault_incremental_refresh import (
    build_vault_scan_plan,
)
from src.services.evidence_vault_lineage_replay import (
    EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION,
    build_historical_report_capture_observation,
    build_lineage_seed_export_v2,
)
from src.services.evidence_vault_operational_authority import (
    EvidenceVaultOperationalAuthorityError,
    adoption_request_fingerprint,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)
from src.services.scanner_evidence_comparison import (
    canonical_evidence_representatives,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)


def test_capture_watermark_is_commit_ordered_idempotent_and_append_only() -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        pytest.fail("B3S_TEST_ALLOW_SCHEMA_DROP=1 is required")
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    repository = PostgresHistoryRepository(dsn)
    repository.migrate()
    first = _capture(source_scan_id="watermark-newer-observation")
    first["observed_at"] = "2026-08-07T10:00:00Z"
    older = _capture(source_scan_id="watermark-older-observation")
    older["observed_at"] = "2025-01-01T00:00:00Z"
    first_result = repository.persist_capture_observation(first)
    older_result = repository.persist_capture_observation(older)
    head = repository.get_evidence_vault_current_capture_watermark("example.com")
    assert head is not None
    assert head["capture_sequence"] == 2
    assert head["capture_id"] == older_result.capture_id
    assert head["capture_id"] != first_result.capture_id
    assert head["append_origin"] == "capture_observation_commit"

    repeated = repository.persist_capture_observation(older)
    assert repeated.status == "unchanged"
    assert repository.get_evidence_vault_current_capture_watermark(
        "example.com"
    ) == head

    def persist(index: int) -> str:
        observation = _capture(source_scan_id=f"watermark-concurrent-{index}")
        observation["observed_at"] = "2026-01-01T00:00:00Z"
        return PostgresHistoryRepository(dsn).persist_capture_observation(
            observation
        ).capture_id

    with ThreadPoolExecutor(max_workers=3) as pool:
        capture_ids = list(pool.map(persist, range(3)))
    concurrent_head = repository.get_evidence_vault_current_capture_watermark(
        "example.com"
    )
    assert concurrent_head is not None
    assert concurrent_head["capture_sequence"] == 5
    with psycopg.connect(dsn) as conn:
        events = conn.execute(
            """
            SELECT capture_sequence, capture_id
            FROM b3s_history.evidence_vault_capture_watermark_events
            ORDER BY capture_sequence
            """
        ).fetchall()
    assert [int(row[0]) for row in events] == [1, 2, 3, 4, 5]
    assert {str(row[1]) for row in events[-3:]} == set(capture_ids)

    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.Error, match="predecessor"):
            conn.execute(
                """
                INSERT INTO b3s_history.evidence_vault_capture_watermark_events (
                    id, brand_id, capture_id, capture_sequence,
                    previous_event_id, previous_event_fingerprint,
                    capture_content_hash, capture_observation_hash,
                    append_origin, lineage_export_identity,
                    lineage_export_fingerprint, lineage_export_ordinal,
                    event_fingerprint, authority,
                    production_runtime_effect, scanner_runtime_effect
                )
                SELECT %s, brand_id, capture_id, capture_sequence + 2,
                       id, event_fingerprint, capture_content_hash,
                       capture_observation_hash, 'capture_observation_commit',
                       NULL, NULL, NULL, %s, false, false, false
                FROM b3s_history.evidence_vault_capture_watermark_events
                ORDER BY capture_sequence DESC
                LIMIT 1
                """,
                (
                    "00000000-0000-0000-0000-000000000067",
                    "c" * 64,
                ),
            )
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(
                """
                UPDATE b3s_history.evidence_vault_capture_watermark_events
                SET event_fingerprint = %s
                WHERE brand_id = (
                    SELECT brand_id
                    FROM b3s_history.evidence_vault_capture_watermark_events
                    LIMIT 1
                )
                """,
                ("f" * 64,),
            )
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(
                "DELETE FROM b3s_history.evidence_vault_capture_watermark_events"
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


class _C7MaterialChangeLLM:
    api_key = "c7-material-change-test"

    def __init__(self, *, polarity: str = "supports") -> None:
        self._polarity = polarity

    def _call_json(self, system, user, **kwargs):
        del system
        if kwargs["schema_name"] == "sv9_flow_evidence_labeling":
            payload = json.loads(user)
            return {
                "labels": [
                    {
                        "ref": row["ref"],
                        "relevant_blocks": ["mission"],
                        "stance": "supports",
                        "identity_match": "domain",
                        "specificity": "explicit",
                    }
                    for row in payload["records"]
                ]
            }
        marker = '"evidence_fingerprint": "'
        fingerprint = user.split(marker, 1)[1].split('"', 1)[0]
        return {
            "relations": [
                {
                    "evidence_fingerprint": fingerprint,
                    "tile_id": "C7",
                    "polarity": self._polarity,
                    "literal_quote": "The agent-to-agent network  for finance teams.",
                    "rationale": "Changed owned-channel evidence requires review.",
                }
            ]
        }


def test_coverage_supplement_registers_reviews_and_adopts_n_plus_one(
    monkeypatch,
) -> None:
    import psycopg

    from src.history.repository import (
        PostgresHistoryRepository,
        _advisory_lock_key,
    )

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
    exact_attestation = (
        repository.get_evidence_vault_active_c7_group_attestation(brand)
    )
    assert exact_attestation is not None
    assert exact_attestation["group_id"] == c7["group_id"]
    assert exact_attestation[
        "exact_source_candidate_packet_fingerprint"
    ] == exact_source["packet"]["candidate_packet_fingerprint"]
    exact_score, _ = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    assert exact_score is not None
    assert exact_score["score"] == 11
    assert exact_score["authority_coverage"]["accepted_tile_count"] == 6
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_C7_CUTOVER_ENABLED", "true")
    monkeypatch.setenv("BRAND3_VAULT_C7_EMERGENCY_DENY", "false")
    monkeypatch.setenv("BRAND3_VAULT_C7_ALLOWLIST", brand)
    # Accepted group authority is independent of capture freshness.  The
    # synthetic empty capture cannot prove the exact two-member raw lineage, so
    # runtime presentation fails closed without changing memory or score.
    exact_runtime_c7 = load_c7_runtime_projection(repository, brand)
    assert exact_runtime_c7 is None

    historical_report = {
        "id": "causa-lineage-report-derived",
        "url": "https://causaprima.ai",
        "brand_name": "Causa Prima",
        "created_at": "2026-07-08T09:17:20.190398Z",
        "components": [{"key": "coherencia", "score": 1}],
        "limitations": ["report_derived_candidate_capture"],
        "attempts": [],
        "acquisition_artifacts": [],
        "acquisition_gate": {"state": "complete"},
        "raw": {
            "schema_version": "historical-report-lineage-fixture-v1",
            "flow": {"candidate": {"evidence_pack": deepcopy(pack)}},
        },
    }
    source_bytes_sha = hashlib.sha256(
        canonical_json(historical_report).encode("utf-8")
    ).hexdigest()
    report_observation = build_historical_report_capture_observation(
        historical_report=historical_report,
        source_raw_bytes_sha256=source_bytes_sha,
        source_artifact_name="causa-lineage-report-derived.json",
    )
    previous_head = repository.get_evidence_vault_current_capture_watermark(brand)
    assert previous_head is not None
    report_export = build_lineage_seed_export_v2(
        workspace_slug="b3s",
        seed_id="causa-lineage-report-derived",
        lineage_export_identity="causa-lineage-report-derived",
        lineage_export_ordinal=previous_head["capture_sequence"] + 1,
        expected_predecessor_event_fingerprint=previous_head[
            "watermark_fingerprint"
        ],
        source_historical_report=historical_report,
        source_raw_bytes_sha256=source_bytes_sha,
        source_artifact_name="causa-lineage-report-derived.json",
        capture_observation=report_observation,
        normalized_evidence_pack=pack,
        exact_relation_supplement=exact_artifact,
    )
    repository.persist_capture_observation(report_observation)
    report_head = repository.get_evidence_vault_current_capture_watermark(brand)
    assert report_head is not None
    assert report_head["append_origin"] == (
        "report_derived_candidate_capture_replay"
    )
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.Error, match="origin must equal"):
            conn.execute(
                """
                INSERT INTO b3s_history.evidence_vault_operational_source_capture_lineage_bindings (
                    id, brand_id, operational_source_packet_id,
                    watermark_event_id, capture_id, capture_sequence,
                    provenance, lineage_artifact_schema_version,
                    lineage_artifact_fingerprint, lineage_export_identity,
                    lineage_export_fingerprint, replay_origin_sequence,
                    member_set_fingerprint, binding_fingerprint,
                    authority, production_runtime_effect, scanner_runtime_effect
                )
                SELECT %s, source.brand_id, source.id, current_event.id,
                       current_event.capture_id, current_event.capture_sequence,
                       'report_derived_candidate_capture',
                       'evidence-vault-lineage-seed-export-v2', %s,
                       'direct-wrong-origin-attempt', %s, 1, %s, %s,
                       false, false, false
                FROM b3s_history.evidence_vault_canonical_memory_packets AS source
                JOIN b3s_history.evidence_vault_capture_watermark_events
                     AS current_event
                  ON current_event.brand_id = source.brand_id
                WHERE source.packet_fingerprint = %s
                ORDER BY current_event.capture_sequence DESC
                LIMIT 1
                """,
                (
                    "00000000-0000-0000-0000-000000000066",
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    exact_source["packet"]["candidate_packet_fingerprint"],
                ),
            )
    report_binding, report_binding_replayed = (
        repository.bind_evidence_vault_exact_source_capture_lineage(
            brand,
            report_export,
        )
    )
    assert report_binding_replayed is False
    assert report_binding["member_count"] == 2
    assert report_binding["provenance"] == "report_derived_candidate_capture"
    assert repository.bind_evidence_vault_exact_source_capture_lineage(
        brand,
        report_export,
    ) == (report_binding, True)
    assert (
        repository.get_evidence_vault_runtime_ready_c7_group_attestation(brand)
        is None
    )
    assert repository.get_evidence_vault_active_c7_group_attestation(
        brand
    ) == exact_attestation
    assert load_c7_runtime_projection(repository, brand) is None

    relabeled_observation = deepcopy(report_observation)
    relabeled_observation["source_scan_id"] = "causa-relabeled-c7-capture"
    relabeled_observation["metadata"]["provenance"] = (
        "live_capture_observation"
    )
    relabeled_observation["metadata"]["acquisition_classification"] = (
        "live_capture_observation"
    )
    relabeled_outcome = repository.persist_capture_observation(
        relabeled_observation
    )
    relabeled_head = repository.get_evidence_vault_current_capture_watermark(
        brand
    )
    assert relabeled_head is not None
    assert relabeled_head["capture_id"] == relabeled_outcome.capture_id
    assert relabeled_head["append_origin"] == "capture_observation_commit"

    relabeled_export = deepcopy(report_export)
    relabeled_export.update(
        {
            "seed_id": "causa-relabeled-c7-capture",
            "lineage_export_identity": "causa-relabeled-c7-capture",
            "lineage_export_ordinal": relabeled_head["capture_sequence"],
            "capture_sequence": relabeled_head["capture_sequence"],
            "replay_origin_sequence": report_binding["replay_origin_sequence"],
            "expected_predecessor_event_fingerprint": relabeled_head[
                "previous_event_fingerprint"
            ],
            "lineage_kind": "live_capture_observation",
            "acquisition_classification": "live_capture_observation",
            "capture_observation": relabeled_observation,
            "capture_observation_hash": relabeled_outcome.observation_hash,
        }
    )
    relabeled_export.pop("lineage_export_fingerprint", None)
    relabeled_export.pop("artifact_fingerprint", None)
    relabeled_export["lineage_export_fingerprint"] = canonical_fingerprint(
        f"{EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION}-manifest",
        relabeled_export,
    )
    relabeled_export["artifact_fingerprint"] = canonical_fingerprint(
        EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION,
        relabeled_export,
    )
    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="Only report-derived lineage replay is supported",
    ):
        repository.bind_evidence_vault_exact_source_capture_lineage(
            brand,
            relabeled_export,
        )
    assert (
        repository.get_evidence_vault_runtime_ready_c7_group_attestation(brand)
        is None
    )

    coverage_loss_observation = deepcopy(report_observation)
    coverage_loss_observation["source_scan_id"] = "causa-c7-linkedin-not-reacquired"
    coverage_loss_observation["evidence_records"] = [
        row
        for row in coverage_loss_observation["evidence_records"]
        if "linkedin.com" not in canonical_json(row).lower()
    ]
    assert len(coverage_loss_observation["evidence_records"]) == (
        len(report_observation["evidence_records"]) - 1
    )
    repository.persist_capture_observation(coverage_loss_observation)
    coverage_loss_head = (
        repository.get_evidence_vault_current_capture_watermark(brand)
    )
    assert coverage_loss_head is not None
    assert coverage_loss_head["append_origin"] == (
        "report_derived_candidate_capture_replay"
    )
    assert (
        repository.get_evidence_vault_runtime_ready_c7_group_attestation(brand)
        is None
    )
    assert repository.get_evidence_vault_active_c7_group_attestation(
        brand
    ) == exact_attestation
    assert load_c7_runtime_projection(repository, brand) is None

    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(
                """
                UPDATE b3s_history.evidence_vault_operational_source_capture_lineage_bindings
                SET binding_fingerprint = %s
                WHERE id = %s
                """,
                ("f" * 64, report_binding["binding_id"]),
            )
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(
                """
                DELETE FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
                WHERE binding_id = %s
                """,
                (report_binding["binding_id"],),
            )
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.Error, match="contiguous"):
            conn.execute(
                """
                INSERT INTO b3s_history.evidence_vault_operational_source_capture_lineage_bindings (
                    id, brand_id, operational_source_packet_id,
                    watermark_event_id, capture_id, capture_sequence,
                    provenance, lineage_artifact_schema_version,
                    lineage_artifact_fingerprint, lineage_export_identity,
                    lineage_export_fingerprint, replay_origin_sequence,
                    member_set_fingerprint, binding_fingerprint,
                    authority, production_runtime_effect, scanner_runtime_effect
                )
                SELECT %s, previous.brand_id,
                       previous.operational_source_packet_id,
                       current_event.id, current_event.capture_id,
                       current_event.capture_sequence, previous.provenance,
                       previous.lineage_artifact_schema_version,
                       previous.lineage_artifact_fingerprint,
                       'direct-gap-attempt', %s,
                       previous.replay_origin_sequence,
                       previous.member_set_fingerprint, %s,
                       false, false, false
                FROM b3s_history.evidence_vault_operational_source_capture_lineage_bindings
                     AS previous
                JOIN b3s_history.evidence_vault_capture_watermark_events
                     AS current_event
                  ON current_event.brand_id = previous.brand_id
                WHERE previous.id = %s
                ORDER BY current_event.capture_sequence DESC
                LIMIT 1
                """,
                (
                    "00000000-0000-0000-0000-000000000068",
                    "e" * 64,
                    "d" * 64,
                    report_binding["binding_id"],
                ),
            )
    assert repository.bind_evidence_vault_exact_source_capture_lineage(
        brand,
        report_export,
    ) == (report_binding, True)

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

    contradiction_change = _persist_c7_change_operation(
        repository,
        brand=brand,
        scan_id="c7-accepted-contradiction",
        current_memory=exact_memory,
        group=c7,
        evidence_pack=pack,
        change_suffix=" Material contradiction candidate.",
        polarity="contradicts",
    )
    contradiction_relation_id = contradiction_change["result_payload"][
        "basis_relations"
    ][0]["relation_id"]
    contradiction_review = (
        repository.review_and_adopt_evidence_vault_operational_source(
            brand,
            source_candidate_packet_fingerprint=contradiction_change[
                "result_payload"
            ]["source_candidate_packet_fingerprint"],
            decisions=[
                {
                    "relation_id": contradiction_relation_id,
                    "decision": "accept",
                    "rationale": (
                        "Accepted contradictory observation; C7 group authority "
                        "must still reopen separately."
                    ),
                }
            ],
            reviewer_id="integration-contradiction-reviewer",
            reviewed_at="2026-08-07T13:02:00Z",
            created_at="2026-08-07T13:02:00Z",
        )
    )
    assert contradiction_review["adoption"] is None
    assert contradiction_review["memory"] == exact_memory
    contradiction_review_event_id = contradiction_review["review_events"][0][
        "decision_event_id"
    ]

    first_change = _persist_c7_change_operation(
        repository,
        brand=brand,
        scan_id="c7-material-change-a",
        current_memory=exact_memory,
        group=c7,
        evidence_pack=pack,
        change_suffix=" Materially changed in capture A.",
    )
    second_change = _persist_c7_change_operation(
        repository,
        brand=brand,
        scan_id="c7-material-change-b",
        current_memory=exact_memory,
        group=c7,
        evidence_pack=pack,
        change_suffix=" Materially changed in capture B.",
    )
    assert (
        first_change["plan"]["delta"]["delta_fingerprint"]
        != second_change["plan"]["delta"]["delta_fingerprint"]
    )
    worker_repository = repository
    with ThreadPoolExecutor(max_workers=1) as pool, psycopg.connect(
        dsn
    ) as blocker:
        operation_brand_id = blocker.execute(
            """
            SELECT brand_id
            FROM b3s_history.evidence_vault_operation_plans
            WHERE id = %s
            FOR UPDATE
            """,
            (UUID(first_change["operation_plan_id"]),),
        ).fetchone()[0]
        future = pool.submit(
            worker_repository.reopen_evidence_vault_composite_group,
            brand,
            exact_source_candidate_packet_fingerprint=exact_source["packet"][
                "candidate_packet_fingerprint"
            ],
            source_scan_id="c7-material-change-a",
            created_at="2026-08-07T13:05:00Z",
        )
        for _ in range(100):
            if future.done():
                future.result()
            blocker.execute("SELECT pg_stat_clear_snapshot()")
            wait_state = blocker.execute(
                """
                SELECT wait_event_type
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND wait_event_type = 'Lock'
                ORDER BY backend_start DESC
                LIMIT 1
                """
            ).fetchone()
            if wait_state is not None and wait_state[0] == "Lock":
                break
            time.sleep(0.02)
        else:
            pytest.fail("reopen worker did not wait on the operation row")
        blocker.execute("SET LOCAL lock_timeout = '1000ms'")
        blocker.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (
                _advisory_lock_key(
                    operation_brand_id,
                    "evidence-vault-canonical-promotion",
                ),
            ),
        )
        blocker.commit()
        reopen = future.result(timeout=5)
    assert reopen["source_replayed"] is False
    assert reopen["packet_replayed"] is False
    assert reopen["adoption_replayed"] is False
    assert reopen["adoption"]["sequence"] == 4
    assert reopen["adoption"]["adopted_by"] == "policy"
    assert reopen["reopen_artifact"]["group_id"] == c7["group_id"]
    assert reopen["reopen_artifact"]["current_group_lifecycle_state"] == (
        "pending_reassessment"
    )
    assert reopen["source_packet"]["reference_resolution"]["durable_trigger"] == {
        "kind": "operation_plan",
        "id": first_change["operation_plan_id"],
        "fingerprint": first_change["plan"]["delta"]["delta_fingerprint"],
    }
    reopened_c7_source = next(
        row
        for row in reopen["source_packet"]["packet"]["candidate_tiles"]
        if row["tile_id"] == "C7"
    )
    assert len(
        [
            row
            for row in reopened_c7_source["basis"]
            if row["polarity"] == "invalidates_candidate"
        ]
    ) == 2
    assert {row["tile_id"] for row in reopen["memory"]["content"]["accepted_tiles"]} == {
        "M1",
        "MG1",
        "MG10",
        "MG3",
        "MG5",
    }
    assert reopen["memory"]["scoring_projection"]["coverage"][
        "canonical_score_status"
    ] == "pending_reassessment"
    assert reopen["memory"]["scoring_projection"]["coverage"][
        "pending_change_tile_count"
    ] == 1
    reopened_score, score_replayed = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    assert score_replayed is False
    assert reopened_score is not None
    assert reopened_score["score"] == exact_score["score"] - 2 == 9
    assert reopened_score["authority_coverage"]["accepted_tile_count"] == 5
    assert reopened_score["authority_coverage"]["pending_change_tile_count"] == 1
    assert reopened_score["authority_coverage"]["canonical_score_status"] == (
        "pending_reassessment"
    )
    assert (
        repository.get_evidence_vault_active_c7_group_attestation(brand) is None
    )
    pending_runtime_c7 = load_c7_runtime_projection(repository, brand)
    assert pending_runtime_c7 is None

    reopen_replay = repository.reopen_evidence_vault_composite_group(
        brand,
        exact_source_candidate_packet_fingerprint=exact_source["packet"][
            "candidate_packet_fingerprint"
        ],
        source_scan_id="c7-material-change-a",
        created_at="2026-08-07T13:05:00Z",
    )
    assert reopen_replay["source_replayed"] is True
    assert reopen_replay["packet_replayed"] is True
    assert reopen_replay["adoption_replayed"] is True
    assert reopen_replay["adoption"] == reopen["adoption"]
    assert reopen_replay["memory"] == reopen["memory"]

    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="stale canonical parent",
    ):
        repository.reopen_evidence_vault_composite_group(
            brand,
            exact_source_candidate_packet_fingerprint=exact_source["packet"][
                "candidate_packet_fingerprint"
            ],
            source_scan_id="c7-material-change-b",
            created_at="2026-08-07T13:06:00Z",
        )
    assert repository.get_capture_operation_plan("c7-material-change-b")[
        "status"
    ] == "completed"
    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="stale canonical parent",
    ):
        repository.reopen_evidence_vault_composite_group(
            brand,
            exact_source_candidate_packet_fingerprint=exact_source["packet"][
                "candidate_packet_fingerprint"
            ],
            accepted_contradiction_review_event_id=(
                contradiction_review_event_id
            ),
            created_at="2026-08-07T13:07:00Z",
        )

    with psycopg.connect(dsn) as conn:
        lifecycle_counts = conn.execute(
            """
            SELECT
              (SELECT count(*)
                 FROM b3s_history.evidence_vault_canonical_memory_packets
                WHERE packet_kind = 'operational_source_v2'
                  AND reference_resolution ->> 'source_kind' =
                      'composite_group_reopen') AS sources,
              (SELECT count(*)
                 FROM b3s_history.evidence_vault_canonical_memory_promotion_events
                WHERE candidate_packet_fingerprint = %s) AS adoptions
            """,
            (
                reopen["operational_packet"]["packet"][
                    "candidate_packet_fingerprint"
                ],
            ),
        ).fetchone()
    assert tuple(lifecycle_counts) == (1, 1)

    with pytest.raises(psycopg.Error, match="append-only"):
        with psycopg.connect(dsn) as conn:
            conn.execute(
                """
                UPDATE b3s_history.evidence_vault_canonical_memory_packets
                   SET authority_state = authority_state
                 WHERE id = %s
                """,
                (
                    UUID(
                        reopen["reopen_artifact"]["reopen_event_id"]
                    ),
                ),
            )
    with pytest.raises(psycopg.Error):
        with psycopg.connect(dsn) as conn:
            conn.execute(
                """
                DELETE FROM b3s_history.evidence_vault_canonical_memory_packets
                 WHERE id = %s
                """,
                (
                    UUID(
                        reopen["reopen_artifact"]["reopen_event_id"]
                    ),
                ),
            )

    (
        replacement_assessment,
        replacement_assessment_rows,
        replacement_pack,
        replacement_worksheet_sha,
    ) = _replacement_c7_review_inputs(
        assessment=assessment,
        assessment_rows=assessment_rows,
        evidence_pack=pack,
        changed_ref=c7["relations"][0]["ref"],
    )
    replacement_artifact = build_exact_relation_supplement_artifact(
        brand_identity=brand,
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version=reopen["memory"][
            "canonical_memory_version"
        ],
        selected_tile_ids=["C7"],
        assessment_artifact=replacement_assessment,
        assessment_review_rows=replacement_assessment_rows,
        assessment_worksheet_sha256=replacement_worksheet_sha,
        evidence_pack=replacement_pack,
    )
    replacement_c7 = replacement_artifact["groups"][0]
    assert replacement_c7["group_id"] != c7["group_id"]
    assert c7["relations"][0]["evidence_fingerprint"] not in {
        row["evidence_fingerprint"]
        for row in replacement_c7["relations"]
    }
    replacement_source, replacement_source_replayed = (
        repository.register_evidence_vault_exact_relation_supplement_source_packet(
            brand,
            replacement_artifact,
            assessment_artifact=replacement_assessment,
            assessment_review_rows=replacement_assessment_rows,
            assessment_worksheet_sha256=replacement_worksheet_sha,
            evidence_pack=replacement_pack,
        )
    )
    assert replacement_source_replayed is False
    replacement_relations = [
        relation
        for tile in replacement_source["packet"]["candidate_tiles"]
        for relation in tile["basis"]
        if relation["review_status"] == "unreviewed"
    ]
    assert len(replacement_relations) == 2
    replacement_decisions = [
        {
            "relation_id": relation["relation_id"],
            "decision": "accept",
            "rationale": "Newly acquired complete C7 all-of group accepted.",
        }
        for relation in replacement_relations
    ]
    replacement_review = (
        repository.review_and_adopt_evidence_vault_operational_source(
            brand,
            source_candidate_packet_fingerprint=replacement_source["packet"][
                "candidate_packet_fingerprint"
            ],
            decisions=replacement_decisions,
            reviewer_id="replacement-c7-reviewer",
            reviewed_at="2026-08-07T14:00:00Z",
            created_at="2026-08-07T14:00:00Z",
        )
    )
    assert replacement_review["adoption"] is not None
    assert replacement_review["adoption"]["sequence"] == 5
    replacement_memory = replacement_review["memory"]
    assert replacement_memory is not None
    assert replacement_memory["content"].get("pending_reassessments") in (
        None,
        [],
    )
    replacement_memory_c7 = next(
        row
        for row in replacement_memory["content"]["accepted_tiles"]
        if row["tile_id"] == "C7"
    )
    assert {row["claim_id"] for row in replacement_memory_c7["basis"]} == {
        replacement_c7["group_id"]
    }
    replacement_attestation = (
        repository.get_evidence_vault_active_c7_group_attestation(brand)
    )
    assert replacement_attestation is not None
    assert replacement_attestation["group_id"] == replacement_c7["group_id"]
    assert replacement_attestation[
        "exact_source_candidate_packet_fingerprint"
    ] == replacement_source["packet"]["candidate_packet_fingerprint"]
    replacement_score, replacement_score_replayed = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    assert replacement_score_replayed is False
    assert replacement_score is not None
    assert replacement_score["score"] == exact_score["score"] == 11
    assert replacement_score["authority_coverage"][
        "pending_change_tile_count"
    ] == 0
    assert replacement_score["authority_coverage"][
        "canonical_score_status"
    ] == "current"
    replacement_runtime_c7 = load_c7_runtime_projection(repository, brand)
    assert replacement_runtime_c7 is None
    replacement_review_replay = (
        repository.review_and_adopt_evidence_vault_operational_source(
            brand,
            source_candidate_packet_fingerprint=replacement_source["packet"][
                "candidate_packet_fingerprint"
            ],
            decisions=replacement_decisions,
            reviewer_id="replacement-c7-reviewer",
            reviewed_at="2026-08-07T14:00:00Z",
            created_at="2026-08-07T14:00:00Z",
        )
    )
    assert replacement_review_replay["packet_replayed"] is True
    assert replacement_review_replay["adoption_replayed"] is True
    assert replacement_review_replay["memory"] == replacement_memory

    replacement_contradiction = _persist_c7_change_operation(
        repository,
        brand=brand,
        scan_id="c7-replacement-accepted-contradiction",
        current_memory=replacement_memory,
        group=replacement_c7,
        evidence_pack=replacement_pack,
        change_suffix=" Accepted contradiction against replacement group.",
        polarity="contradicts",
    )
    replacement_contradiction_relation_id = replacement_contradiction[
        "result_payload"
    ]["basis_relations"][0]["relation_id"]
    replacement_contradiction_review = (
        repository.review_and_adopt_evidence_vault_operational_source(
            brand,
            source_candidate_packet_fingerprint=replacement_contradiction[
                "result_payload"
            ]["source_candidate_packet_fingerprint"],
            decisions=[
                {
                    "relation_id": replacement_contradiction_relation_id,
                    "decision": "accept",
                    "rationale": (
                        "Accepted contradiction targets a frozen replacement "
                        "C7 member."
                    ),
                }
            ],
            reviewer_id="replacement-contradiction-reviewer",
            reviewed_at="2026-08-07T14:05:00Z",
            created_at="2026-08-07T14:05:00Z",
        )
    )
    assert replacement_contradiction_review["adoption"] is None
    replacement_contradiction_event_id = (
        replacement_contradiction_review["review_events"][0][
            "decision_event_id"
        ]
    )
    contradiction_reopen = repository.reopen_evidence_vault_composite_group(
        brand,
        exact_source_candidate_packet_fingerprint=replacement_source["packet"][
            "candidate_packet_fingerprint"
        ],
        accepted_contradiction_review_event_id=(
            replacement_contradiction_event_id
        ),
        created_at="2026-08-07T14:06:00Z",
    )
    assert contradiction_reopen["source_replayed"] is False
    assert contradiction_reopen["packet_replayed"] is False
    assert contradiction_reopen["adoption_replayed"] is False
    assert contradiction_reopen["adoption"]["sequence"] == 6
    contradiction_durable_trigger = contradiction_reopen["source_packet"][
        "reference_resolution"
    ]["durable_trigger"]
    assert contradiction_durable_trigger["kind"] == "relation_review"
    assert contradiction_durable_trigger["id"] == (
        replacement_contradiction_event_id
    )
    assert len(contradiction_durable_trigger["fingerprint"]) == 64
    assert contradiction_reopen["memory"]["content"][
        "pending_reassessments"
    ][0]["superseded_member_evidence_fingerprints"] == [
        replacement_c7["relations"][0]["evidence_fingerprint"]
    ]
    contradiction_reopened_score, contradiction_score_replayed = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    assert contradiction_score_replayed is False
    assert contradiction_reopened_score is not None
    assert contradiction_reopened_score["score"] == 9
    assert contradiction_reopened_score["authority_coverage"][
        "pending_change_tile_count"
    ] == 1
    contradiction_replay = repository.reopen_evidence_vault_composite_group(
        brand,
        exact_source_candidate_packet_fingerprint=replacement_source["packet"][
            "candidate_packet_fingerprint"
        ],
        accepted_contradiction_review_event_id=(
            replacement_contradiction_event_id
        ),
        created_at="2026-08-07T14:06:00Z",
    )
    assert contradiction_replay["source_replayed"] is True
    assert contradiction_replay["packet_replayed"] is True
    assert contradiction_replay["adoption_replayed"] is True
    assert contradiction_replay["memory"] == contradiction_reopen["memory"]

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


def _replacement_c7_review_inputs(
    *,
    assessment: dict,
    assessment_rows: list[dict[str, str]],
    evidence_pack: dict,
    changed_ref: str,
) -> tuple[dict, list[dict[str, str]], dict, str]:
    replacement_pack = deepcopy(evidence_pack)
    changed_row = next(
        row
        for row in replacement_pack["evidence"]
        if row["ref"] == changed_ref
    )
    changed_row["content"] += " Newly acquired replacement evidence."
    representatives = canonical_evidence_representatives(
        replacement_pack["evidence"],
        subject_url="https://causaprima.ai",
    )
    changed_fingerprint, changed_representative = next(
        (fingerprint, row)
        for fingerprint, row in representatives.items()
        if row["ref"] == changed_ref
    )
    changed_identity = project_evidence_memory_row_identity(
        {
            **changed_representative,
            "evidence_fingerprint": changed_fingerprint,
        },
        brand_domain="causaprima.ai",
    )
    assert changed_identity is not None
    replacement_pack_sha = hashlib.sha256(
        canonical_json(replacement_pack).encode("utf-8")
    ).hexdigest()

    replacement_assessment = deepcopy(assessment)
    replacement_assessment[
        "source_evidence_pack_canonical_sha256"
    ] = replacement_pack_sha
    old_to_new_candidates: dict[str, str] = {}
    for tile in replacement_assessment["tile_assessments"]:
        old_candidate_id = tile["candidate_id"]
        tile["source_evidence_pack_canonical_sha256"] = replacement_pack_sha
        for evidence in tile.get("evidence") or []:
            if evidence.get("ref") == changed_ref:
                evidence.update(
                    {
                        "evidence_fingerprint": changed_fingerprint,
                        "evidence_id": changed_identity["evidence_id"],
                        "source_identity_id": changed_identity["document_id"],
                    }
                )
        candidate_payload = {
            key: value
            for key, value in tile.items()
            if key
            not in {
                "candidate_id",
                "review_status",
                "decision_event_id",
            }
        }
        tile["candidate_id"] = canonical_fingerprint(
            "evidence-vault-independent-contract-audit-candidate-v1",
            candidate_payload,
        )
        old_to_new_candidates[old_candidate_id] = tile["candidate_id"]
    audit_unsigned = {
        key: value
        for key, value in replacement_assessment.items()
        if key != "audit_fingerprint"
    }
    replacement_assessment["audit_fingerprint"] = canonical_fingerprint(
        "evidence-vault-independent-contract-audit-v1",
        audit_unsigned,
    )
    replacement_by_candidate = {
        tile["candidate_id"]: tile
        for tile in replacement_assessment["tile_assessments"]
    }
    replacement_rows = []
    for row in assessment_rows:
        new_candidate_id = old_to_new_candidates[row["candidate_id"]]
        mutable = {
            field: row[field]
            for field in _ASSESSMENT_MUTABLE_FIELDS
        }
        replacement_rows.append(
            {
                **_assessment_review_immutable(
                    replacement_by_candidate[new_candidate_id]
                ),
                **mutable,
            }
        )
    worksheet_sha = hashlib.sha256(
        canonical_json(replacement_rows).encode("utf-8")
    ).hexdigest()
    return (
        replacement_assessment,
        replacement_rows,
        replacement_pack,
        worksheet_sha,
    )


def _persist_c7_change_operation(
    repository,
    *,
    brand: str,
    scan_id: str,
    current_memory: dict,
    group: dict,
    evidence_pack: dict,
    member_index: int = 0,
    change_suffix: str = " Materially changed.",
    polarity: str = "supports",
) -> dict:
    by_ref = {row["ref"]: deepcopy(row) for row in evidence_pack["evidence"]}
    previous_rows = [
        deepcopy(by_ref[relation["ref"]]) for relation in group["relations"]
    ]
    current_rows = deepcopy(previous_rows)
    current_rows[member_index]["content"] += change_suffix
    plan = build_vault_scan_plan(
        brand_identity=brand,
        subject_url=f"https://{brand}",
        mode="incremental_refresh",
        current_evidence_records=current_rows,
        previous_capture_evidence_records=previous_rows,
        known_evidence_records=previous_rows,
        accepted_evidence_tile_relations=[
            {
                "tile_id": "C7",
                "evidence_fingerprint": relation["evidence_fingerprint"],
            }
            for relation in group["relations"]
        ],
        canonical_memory_version=current_memory["canonical_memory_version"],
    )
    assert plan["delta"]["superseded_evidence_fingerprints"] == [
        group["relations"][member_index]["evidence_fingerprint"]
    ]
    observation = {
        "schema_version": "b3s-capture-observation-v1",
        "source_scan_id": scan_id,
        "source_run_id": f"provider-{scan_id}",
        "brand_name": brand,
        "url": f"https://{brand}",
        "observed_at": "2026-08-07T13:00:00Z",
        "recorded_at": "2026-08-07T13:00:01Z",
        "pipeline_version": "vault-incremental-capture-v1",
        "acquisition_state": "complete",
        "acquisition_summary": {"owned_pages": 1, "external_results": 1},
        "limitations": [],
        "capture_payload": {
            "raw_inputs": [{"source": "web", "payload": {"text": "Evidence"}}]
        },
        "evidence_records": current_rows,
        "acquisition_attempts": [
            {"provider": "web", "intent": "owned", "status": "success"}
        ],
        "artifacts": [],
        "metadata": {
            "mode": "incremental_refresh",
            "analysis_status": "pending",
            "operation_plan": plan,
            "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
        },
    }
    repository.persist_capture_observation(observation)
    execute_vault_operation_plan(
        repository=repository,
        source_scan_id=scan_id,
        worker_id=f"worker-{scan_id}",
        llm=_C7MaterialChangeLLM(polarity=polarity),
    )
    stored = repository.get_capture_operation_plan(scan_id)
    assert stored["status"] == "completed"
    return stored


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
