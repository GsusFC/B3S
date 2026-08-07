#!/usr/bin/env python3
"""Validate human field worksheets and adopt them only in a disposable Vault."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from src.history.repository import PostgresHistoryRepository
from src.services.evidence_vault_field_replay import (
    has_libpq_connection_override_environment,
    is_local_postgres_dsn,
    postgres_database_name,
)


_IMMUTABLE_FIELDS = (
    "relation_id",
    "evidence_fingerprint",
    "evidence_ref",
    "evidence_url",
    "tile_id",
    "tile_key",
    "tile_condition",
    "polarity",
    "literal_quote",
    "rationale",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=Path("audits/evidence_vault_field_validation_v1"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database_name = postgres_database_name(args.dsn)
    if os.environ.get("B3S_FIELD_SHADOW_ALLOW_WRITE") != database_name:
        raise SystemExit(
            "B3S_FIELD_SHADOW_ALLOW_WRITE must equal the disposable database name"
        )
    if not is_local_postgres_dsn(args.dsn):
        raise SystemExit("field review refuses a non-local PostgreSQL DSN")
    if has_libpq_connection_override_environment(os.environ):
        raise SystemExit("field review refuses ambient libpq routing overrides")

    audit_dir = args.audit_dir.resolve()
    semantic_path = audit_dir / "semantic-shadow.json"
    validation = _json_object(audit_dir / "validation-manifest.json")
    expected_semantic_sha = next(
        (
            row.get("sha256")
            for row in validation.get("artifacts") or []
            if row.get("file") == semantic_path.name
        ),
        None,
    )
    if hashlib.sha256(semantic_path.read_bytes()).hexdigest() != (
        expected_semantic_sha
    ):
        raise RuntimeError("semantic shadow differs from the validated audit")
    shadow = _json_object(semantic_path)
    cases: list[dict[str, Any]] = []
    reviewer_ids: set[str] = set()
    reviewed_times: set[str] = set()
    prepared: list[tuple[dict[str, Any], list[dict[str, str]]]] = []
    for case in shadow["cases"]:
        case_id = str(case["case_id"])
        rows = _validated_rows(
            audit_dir / f"{case_id}-human-review.csv",
            expected=case["review_worksheet"],
        )
        reviewer_ids.update(row["reviewer_id"] for row in rows)
        reviewed_times.update(row["reviewed_at"] for row in rows)
        prepared.append((case, rows))
    if len(reviewer_ids) != 1 or len(reviewed_times) != 1:
        raise RuntimeError("field worksheets must share one reviewer and review time")

    repository = PostgresHistoryRepository(args.dsn)
    for case, rows in prepared:
        case_id = str(case["case_id"])
        decisions = [
            {
                "relation_id": row["relation_id"],
                "decision": row["human_decision"],
                "rationale": row["human_rationale"],
            }
            for row in rows
        ]
        reviewer_id = rows[0]["reviewer_id"]
        reviewed_at = rows[0]["reviewed_at"]
        review = repository.review_and_adopt_evidence_vault_operational_source(
            case["brand_identity"],
            source_candidate_packet_fingerprint=case[
                "source_candidate_packet_fingerprint"
            ],
            decisions=decisions,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            created_at=reviewed_at,
        )
        score, score_replayed = (
            repository.get_or_create_evidence_vault_operational_score_evaluation(
                case["brand_identity"]
            )
        )
        replay = repository.review_and_adopt_evidence_vault_operational_source(
            case["brand_identity"],
            source_candidate_packet_fingerprint=case[
                "source_candidate_packet_fingerprint"
            ],
            decisions=decisions,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            created_at=reviewed_at,
        )
        memory = review["memory"]
        if memory is None or score is None:
            raise RuntimeError(f"{case_id} did not create accepted memory and score")
        if score["canonical_memory_version"] != memory["canonical_memory_version"]:
            raise RuntimeError(f"{case_id} score is not bound to accepted memory")
        accepted_ids = {
            row["relation_id"]
            for row in rows
            if row["human_decision"] == "accept"
        }
        rejected_ids = {
            row["relation_id"]
            for row in rows
            if row["human_decision"] == "reject"
        }
        memory_relations = {
            basis["relation_id"]
            for tile in memory["content"]["accepted_tiles"]
            for basis in tile.get("basis") or []
        }
        if memory_relations != accepted_ids or memory_relations & rejected_ids:
            raise RuntimeError(f"{case_id} accepted memory differs from worksheet")
        if replay["packet_replayed"] is not True or replay[
            "adoption_replayed"
        ] is not True:
            raise RuntimeError(f"{case_id} human review replay is not idempotent")
        cases.append(
            {
                "case_id": case_id,
                "brand_identity": case["brand_identity"],
                "review_event_count": len(review["review_events"]),
                "accepted_relation_count": len(accepted_ids),
                "rejected_relation_count": len(rejected_ids),
                "accepted_tile_count": len(memory["content"]["accepted_tiles"]),
                "accepted_tile_ids": sorted(
                    tile["tile_id"]
                    for tile in memory["content"]["accepted_tiles"]
                ),
                "canonical_memory_version": memory[
                    "canonical_memory_version"
                ],
                "adoption_event_id": review["adoption"]["event_id"],
                "adoption_replayed_on_first_call": review[
                    "adoption_replayed"
                ],
                "replay_packet_replayed": replay["packet_replayed"],
                "replay_adoption_replayed": replay["adoption_replayed"],
                "score": score["score"],
                "score_evaluation_identity": score["evaluation_identity"],
                "score_replayed_on_creation_call": score_replayed,
                "authority_coverage": score["authority_coverage"],
                "review_event_times": sorted(
                    {event["created_at"] for event in review["review_events"]}
                ),
                "production_runtime_effect": False,
                "scanner_runtime_effect": False,
            }
        )
    output = {
        "schema_version": "evidence-vault-field-human-adoption-result-v1",
        "database_classification": "disposable_local_postgresql",
        "reviewer_id": next(iter(reviewer_ids)),
        "reviewed_at": next(iter(reviewed_times)),
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "cutover_authorized": False,
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


def _validated_rows(
    path: Path,
    *,
    expected: list[dict[str, Any]],
) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    expected_by_id = {str(row["relation_id"]): row for row in expected}
    if not rows or {row.get("relation_id") for row in rows} != set(expected_by_id):
        raise RuntimeError(f"{path.name} relation set changed")
    for row in rows:
        relation_id = str(row["relation_id"])
        expected_row = expected_by_id[relation_id]
        for field in _IMMUTABLE_FIELDS:
            expected_value = expected_row.get(field)
            if row.get(field, "") != (
                "" if expected_value is None else str(expected_value)
            ):
                raise RuntimeError(
                    f"{path.name} immutable field changed: {relation_id} {field}"
                )
        if json.loads(row["semantic_labels"]) != expected_row["semantic_labels"]:
            raise RuntimeError(
                f"{path.name} semantic labels changed: {relation_id}"
            )
        if row.get("human_decision") not in {"accept", "reject"}:
            raise RuntimeError(f"{path.name} has an incomplete decision")
        for field in ("human_rationale", "reviewer_id", "reviewed_at"):
            if not str(row.get(field) or "").strip():
                raise RuntimeError(f"{path.name} has an incomplete {field}")
    if len({row["reviewer_id"] for row in rows}) != 1 or len(
        {row["reviewed_at"] for row in rows}
    ) != 1:
        raise RuntimeError(f"{path.name} has inconsistent review authority")
    return rows


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
