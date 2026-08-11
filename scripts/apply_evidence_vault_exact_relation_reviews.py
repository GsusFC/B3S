#!/usr/bin/env python3
"""Apply fresh exact relation/group reviews only in an opted-in local Vault."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from src.history.repository import PostgresHistoryRepository
from src.services.evidence_vault_exact_relation_supplement import (
    build_exact_relation_review_worksheets,
    validate_exact_relation_supplement_artifact,
)
from src.services.evidence_vault_field_replay import (
    has_libpq_connection_override_environment,
    is_local_postgres_dsn,
    postgres_database_name,
)


_MUTABLE_FIELDS = {
    "human_decision",
    "human_rationale",
    "reviewer_id",
    "reviewed_at",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--atomic-worksheet", type=Path, required=True)
    parser.add_argument("--composite-worksheet", type=Path, required=True)
    parser.add_argument("--assessment-artifact", type=Path, required=True)
    parser.add_argument("--assessment-worksheet", type=Path, required=True)
    parser.add_argument("--evidence-pack", type=Path, required=True)
    parser.add_argument("--validation-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    database_name = postgres_database_name(args.dsn)
    if os.environ.get("B3S_FIELD_SHADOW_ALLOW_WRITE") != database_name:
        raise SystemExit(
            "B3S_FIELD_SHADOW_ALLOW_WRITE must equal the disposable database name"
        )
    if not is_local_postgres_dsn(args.dsn):
        raise SystemExit("exact relation review refuses a non-local PostgreSQL DSN")
    if has_libpq_connection_override_environment(os.environ):
        raise SystemExit(
            "exact relation review refuses ambient libpq routing overrides"
        )

    artifact_path = args.artifact.resolve()
    assessment_path = args.assessment_artifact.resolve()
    validation = _json_object(args.validation_manifest.resolve())
    _verify_audit_artifact(validation, artifact_path)
    _verify_audit_artifact(validation, assessment_path)
    artifact = _json_object(artifact_path)
    assessment = _json_object(assessment_path)
    evidence_pack = _json_object(args.evidence_pack.resolve())
    assessment_worksheet = args.assessment_worksheet.resolve()
    with assessment_worksheet.open(encoding="utf-8", newline="") as handle:
        assessment_rows = list(csv.DictReader(handle))
    assessment_sha = _file_sha(assessment_worksheet)
    validate_exact_relation_supplement_artifact(
        artifact,
        assessment_artifact=assessment,
        assessment_review_rows=assessment_rows,
        assessment_worksheet_sha256=assessment_sha,
        evidence_pack=evidence_pack,
    )
    expected_atomic, expected_composite = build_exact_relation_review_worksheets(
        artifact
    )
    atomic_rows = _validated_rows(
        args.atomic_worksheet.resolve(),
        expected=expected_atomic,
        identity_field="relation_id",
    )
    composite_rows = _validated_rows(
        args.composite_worksheet.resolve(),
        expected=expected_composite,
        identity_field="group_id",
    )
    all_rows = [*atomic_rows, *composite_rows]
    reviewers = {row["reviewer_id"] for row in all_rows}
    reviewed_times = {row["reviewed_at"] for row in all_rows}
    if len(reviewers) != 1 or len(reviewed_times) != 1:
        raise RuntimeError(
            "atomic and composite exact reviews must share reviewer/time"
        )
    reviewer_id = next(iter(reviewers))
    reviewed_at = next(iter(reviewed_times))
    decisions = [
        {
            "relation_id": row["relation_id"],
            "decision": row["human_decision"],
            "rationale": row["human_rationale"],
        }
        for row in atomic_rows
    ]
    groups = {row["group_id"]: row for row in artifact["groups"]}
    for row in composite_rows:
        group = groups[row["group_id"]]
        decisions.extend(
            {
                "relation_id": relation["relation_id"],
                "decision": row["human_decision"],
                "rationale": row["human_rationale"],
            }
            for relation in group["relations"]
        )

    repository = PostgresHistoryRepository(args.dsn)
    brand = str(artifact["brand_identity"])
    before_memory = repository.get_evidence_vault_operational_memory(brand)
    before_score, _ = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    if before_memory is None or before_score is None:
        raise RuntimeError("exact relation review requires existing Vault memory")
    initial_application = before_memory["canonical_memory_version"] == artifact[
        "parent_canonical_memory_version"
    ]
    before_relations = {
        basis["relation_id"]
        for tile in before_memory["content"]["accepted_tiles"]
        for basis in tile.get("basis") or []
    }
    before_tiles = {
        tile["tile_id"] for tile in before_memory["content"]["accepted_tiles"]
    }
    source, source_replayed = (
        repository.register_evidence_vault_exact_relation_supplement_source_packet(
            brand,
            artifact,
            assessment_artifact=assessment,
            assessment_review_rows=assessment_rows,
            assessment_worksheet_sha256=assessment_sha,
            evidence_pack=evidence_pack,
        )
    )
    review = repository.review_and_adopt_evidence_vault_operational_source(
        brand,
        source_candidate_packet_fingerprint=source["packet"][
            "candidate_packet_fingerprint"
        ],
        decisions=decisions,
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        created_at=reviewed_at,
    )
    score, score_replayed = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    memory = review["memory"]
    if memory is None or score is None:
        raise RuntimeError("exact relation review produced no Vault projection")
    accepted_ids = {
        row["relation_id"] for row in decisions if row["decision"] == "accept"
    }
    rejected_ids = {
        row["relation_id"] for row in decisions if row["decision"] == "reject"
    }
    memory_relations = {
        basis["relation_id"]
        for tile in memory["content"]["accepted_tiles"]
        for basis in tile.get("basis") or []
    }
    final_tiles = {
        tile["tile_id"] for tile in memory["content"]["accepted_tiles"]
    }
    expected_new_tiles = {
        group["tile_id"]
        for group in artifact["groups"]
        if all(
            next(
                decision["decision"]
                for decision in decisions
                if decision["relation_id"] == relation["relation_id"]
            )
            == "accept"
            for relation in group["relations"]
        )
    }
    if initial_application and (
        memory_relations - before_relations != accepted_ids
        or memory_relations & rejected_ids
        or final_tiles != before_tiles | expected_new_tiles
    ):
        raise RuntimeError("exact relation accepted-memory delta differs from review")
    if not initial_application and (
        not accepted_ids.issubset(memory_relations)
        or memory_relations & rejected_ids
    ):
        raise RuntimeError("exact relation replay differs from adopted memory")
    if score["canonical_memory_version"] != memory["canonical_memory_version"]:
        raise RuntimeError("exact relation score is not bound to accepted memory")
    if bool(accepted_ids) != (review["adoption"] is not None):
        raise RuntimeError("exact relation adoption event does not match accepted delta")

    source_again, source_replayed_again = (
        repository.register_evidence_vault_exact_relation_supplement_source_packet(
            brand,
            artifact,
            assessment_artifact=assessment,
            assessment_review_rows=assessment_rows,
            assessment_worksheet_sha256=assessment_sha,
            evidence_pack=evidence_pack,
        )
    )
    replay = repository.review_and_adopt_evidence_vault_operational_source(
        brand,
        source_candidate_packet_fingerprint=source["packet"][
            "candidate_packet_fingerprint"
        ],
        decisions=decisions,
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        created_at=reviewed_at,
    )
    score_again, score_replayed_again = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    if (
        source_replayed_again is not True
        or source_again != source
        or replay["packet_replayed"] is not True
        or replay["adoption_replayed"] is not True
        or replay["memory"] != memory
        or score_replayed_again is not True
        or score_again != score
    ):
        raise RuntimeError("exact relation review replay is not idempotent")

    output = {
        "schema_version": "evidence-vault-exact-relation-human-adoption-result-v1",
        "database_classification": "disposable_local_postgresql",
        "brand_identity": brand,
        "reviewer_id": reviewer_id,
        "reviewed_at": reviewed_at,
        "exact_relation_artifact_fingerprint": artifact["artifact_fingerprint"],
        "exact_relation_artifact_sha256": _file_sha(artifact_path),
        "atomic_worksheet_sha256": _file_sha(args.atomic_worksheet.resolve()),
        "composite_worksheet_sha256": _file_sha(
            args.composite_worksheet.resolve()
        ),
        "assessment_artifact_sha256": _file_sha(assessment_path),
        "assessment_worksheet_sha256": assessment_sha,
        "source_evidence_pack_canonical_sha256": artifact[
            "source_evidence_pack_canonical_sha256"
        ],
        "source_candidate_packet_fingerprint": source["packet"][
            "candidate_packet_fingerprint"
        ],
        "source_replayed_on_first_call": source_replayed,
        "source_replayed_after_review": source_replayed_again,
        "review_request_fingerprint": review["review_request_fingerprint"],
        "review_event_count": len(review["review_events"]),
        "review_event_ids": sorted(
            event["decision_event_id"] for event in review["review_events"]
        ),
        "accepted_relation_count": len(accepted_ids),
        "rejected_relation_count": len(rejected_ids),
        "accepted_group_tile_ids": sorted(expected_new_tiles),
        "initial_application": initial_application,
        "active_memory_at_start": before_memory["canonical_memory_version"],
        "parent_canonical_memory_version": artifact[
            "parent_canonical_memory_version"
        ],
        "canonical_memory_version": memory["canonical_memory_version"],
        "adoption_event_id": (
            review["adoption"]["event_id"]
            if review["adoption"] is not None
            else None
        ),
        "adoption_sequence": (
            review["adoption"]["sequence"]
            if review["adoption"] is not None
            else None
        ),
        "previous_event_id": (
            review["adoption"]["previous_event_id"]
            if review["adoption"] is not None
            else None
        ),
        "accepted_tile_ids": sorted(final_tiles),
        "accepted_memory_relation_count": len(memory_relations),
        "score_before": before_score["score"],
        "score": score["score"],
        "score_evaluation_identity": score["evaluation_identity"],
        "authority_coverage": score["authority_coverage"],
        "score_replayed_on_creation_call": score_replayed,
        "score_replayed_on_second_call": score_replayed_again,
        "authority": True,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "cutover_authorized": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _validated_rows(
    path: Path,
    *,
    expected: list[Mapping[str, str]],
    identity_field: str,
) -> list[dict[str, str]]:
    expected_by_id = {row[identity_field]: dict(row) for row in expected}
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    actual_ids = [row.get(identity_field) or "" for row in rows]
    if (
        len(rows) != len(expected_by_id)
        or len(actual_ids) != len(set(actual_ids))
        or set(actual_ids) != set(expected_by_id)
    ):
        raise RuntimeError(f"{path.name} identities differ from its artifact")
    for row in rows:
        expected_row = expected_by_id[row[identity_field]]
        immutable = {
            key: value for key, value in expected_row.items() if key not in _MUTABLE_FIELDS
        }
        if set(row) != set(expected_row):
            raise RuntimeError(f"{path.name} columns differ from its artifact")
        if any(row[field] != value for field, value in immutable.items()):
            raise RuntimeError(f"{path.name} immutable columns changed")
        if row["human_decision"] not in {"accept", "reject"}:
            raise RuntimeError(f"{path.name} has an invalid human decision")
        if not row["human_rationale"].strip() or not row["reviewer_id"].strip():
            raise RuntimeError(f"{path.name} human review is incomplete")
        try:
            reviewed_at = datetime.fromisoformat(row["reviewed_at"])
        except ValueError as exc:
            raise RuntimeError(f"{path.name} reviewed_at is invalid") from exc
        if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
            raise RuntimeError(f"{path.name} reviewed_at lacks timezone")
    return rows


def _verify_audit_artifact(
    manifest: Mapping[str, Any],
    path: Path,
) -> None:
    expected = next(
        (
            row
            for row in manifest.get("artifacts") or []
            if row.get("file") == path.name
        ),
        None,
    )
    if (
        expected is None
        or expected.get("bytes") != path.stat().st_size
        or expected.get("sha256") != _file_sha(path)
    ):
        raise RuntimeError(f"{path.name} differs from the validation manifest")


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return value


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
