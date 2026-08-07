#!/usr/bin/env python3
"""Adopt reviewed coverage relations only in an opted-in disposable Vault."""

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
from src.services.evidence_vault_canonical_core import (
    build_tile_contract_registry,
    canonical_fingerprint,
)
from src.services.evidence_vault_coverage_supplement import (
    validate_coverage_supplement_artifact,
)
from src.services.evidence_vault_field_replay import (
    has_libpq_connection_override_environment,
    is_local_postgres_dsn,
    postgres_database_name,
)


_RELATION_MUTABLE_FIELDS = {
    "human_decision",
    "human_rationale",
    "reviewer_id",
    "reviewed_at",
}
_ASSESSMENT_MUTABLE_FIELDS = _RELATION_MUTABLE_FIELDS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--worksheet", type=Path, required=True)
    parser.add_argument("--evidence-pack", type=Path, required=True)
    parser.add_argument("--assessment-artifact", type=Path, required=True)
    parser.add_argument("--assessment-worksheet", type=Path, required=True)
    parser.add_argument("--validation-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database_name = postgres_database_name(args.dsn)
    if os.environ.get("B3S_FIELD_SHADOW_ALLOW_WRITE") != database_name:
        raise SystemExit(
            "B3S_FIELD_SHADOW_ALLOW_WRITE must equal the disposable database name"
        )
    if not is_local_postgres_dsn(args.dsn):
        raise SystemExit("coverage review refuses a non-local PostgreSQL DSN")
    if has_libpq_connection_override_environment(os.environ):
        raise SystemExit(
            "coverage review refuses ambient libpq routing overrides"
        )

    artifact_path = args.artifact.resolve()
    assessment_artifact_path = args.assessment_artifact.resolve()
    validation = _json_object(args.validation_manifest.resolve())
    _verify_audit_artifact(validation, artifact_path)
    _verify_audit_artifact(validation, assessment_artifact_path)
    artifact = _json_object(artifact_path)
    evidence_pack = _json_object(args.evidence_pack.resolve())
    validate_coverage_supplement_artifact(
        artifact,
        evidence_pack=evidence_pack,
    )
    relation_rows = _validated_relation_rows(
        args.worksheet.resolve(),
        artifact=artifact,
    )
    assessment_artifact = _json_object(assessment_artifact_path)
    assessment_rows = _validated_assessment_rows(
        args.assessment_worksheet.resolve(),
        artifact=assessment_artifact,
    )
    reviewers = {
        row["reviewer_id"] for row in [*relation_rows, *assessment_rows]
    }
    reviewed_times = {
        row["reviewed_at"] for row in [*relation_rows, *assessment_rows]
    }
    if len(reviewers) != 1 or len(reviewed_times) != 1:
        raise RuntimeError(
            "coverage relation and assessment reviews must share reviewer/time"
        )
    reviewer_id = next(iter(reviewers))
    reviewed_at = next(iter(reviewed_times))

    repository = PostgresHistoryRepository(args.dsn)
    brand = str(artifact["request"]["brand_identity"])
    before_memory = repository.get_evidence_vault_operational_memory(brand)
    before_score, _ = (
        repository.get_or_create_evidence_vault_operational_score_evaluation(
            brand
        )
    )
    if before_memory is None or before_score is None:
        raise RuntimeError("coverage adoption requires existing accepted memory")
    initial_application = before_memory["canonical_memory_version"] == artifact[
        "request"
    ]["parent_canonical_memory_version"]
    before_relations = {
        basis["relation_id"]
        for tile in before_memory["content"]["accepted_tiles"]
        for basis in tile.get("basis") or []
    }
    before_tile_ids = {
        tile["tile_id"]
        for tile in before_memory["content"]["accepted_tiles"]
    }

    source, source_replayed = (
        repository.register_evidence_vault_coverage_supplement_source_packet(
            brand,
            artifact,
            evidence_pack=evidence_pack,
        )
    )
    decisions = [
        {
            "relation_id": row["relation_id"],
            "decision": row["human_decision"],
            "rationale": row["human_rationale"],
        }
        for row in relation_rows
    ]
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
    if review["memory"] is None or review["adoption"] is None or score is None:
        raise RuntimeError("coverage review did not create memory and score")
    memory = review["memory"]
    accepted_ids = {
        row["relation_id"]
        for row in relation_rows
        if row["human_decision"] == "accept"
    }
    rejected_ids = {
        row["relation_id"]
        for row in relation_rows
        if row["human_decision"] == "reject"
    }
    memory_relations = {
        basis["relation_id"]
        for tile in memory["content"]["accepted_tiles"]
        for basis in tile.get("basis") or []
    }
    final_tile_ids = {
        tile["tile_id"] for tile in memory["content"]["accepted_tiles"]
    }
    if initial_application:
        if (
            memory_relations - before_relations != accepted_ids
            or memory_relations & rejected_ids
            or before_tile_ids | {"MG10"} != final_tile_ids
        ):
            raise RuntimeError(
                "coverage accepted-memory delta differs from worksheet"
            )
    elif (
        not accepted_ids.issubset(memory_relations)
        or memory_relations & rejected_ids
        or final_tile_ids != {"P1", "P3", "P5", "PR3", "MG10"}
        or len(memory_relations) != 11
    ):
        raise RuntimeError("coverage replay differs from the adopted field result")
    if score["canonical_memory_version"] != memory["canonical_memory_version"]:
        raise RuntimeError("coverage score is not bound to accepted memory")
    if (
        before_score["score"] != (4 if initial_application else 6)
        or score["score"] != 6
        or score["authority_coverage"]["accepted_tile_count"] != 5
        or score["authority_coverage"]["unresolved_tile_count"] != 75
        or score["authority_coverage"][
            "tile_authority_coverage_ratio"
        ]
        != 0.0625
        or score["authority_coverage"][
            "score_weight_authority_coverage_ratio"
        ]
        != 0.06
    ):
        raise RuntimeError("coverage adoption produced an unexpected sparse score")
    magnetism = next(
        row
        for row in score["component_breakdown"]
        if row["component_key"] == "magnetism"
    )
    if (
        magnetism["ok_count"] != 1
        or magnetism["raw_score"] != 1
        or magnetism["multiplier"] != 2
        or magnetism["points"] != 2
        or magnetism["effective_score"] != 1
    ):
        raise RuntimeError("MG10 did not produce the expected magnetism weight")

    source_replay, source_replayed_again = (
        repository.register_evidence_vault_coverage_supplement_source_packet(
            brand,
            artifact,
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
        or source_replay != source
        or replay["packet_replayed"] is not True
        or replay["adoption_replayed"] is not True
        or replay["memory"] != memory
        or score_replayed_again is not True
        or score_again != score
    ):
        raise RuntimeError("coverage adoption replay is not idempotent")

    output = {
        "schema_version": "evidence-vault-coverage-human-adoption-result-v1",
        "database_classification": "disposable_local_postgresql",
        "brand_identity": brand,
        "reviewer_id": reviewer_id,
        "reviewed_at": reviewed_at,
        "coverage_artifact_sha256": _file_sha(artifact_path),
        "coverage_worksheet_sha256": _file_sha(args.worksheet.resolve()),
        "assessment_artifact_sha256": _file_sha(assessment_artifact_path),
        "assessment_worksheet_sha256": _file_sha(
            args.assessment_worksheet.resolve()
        ),
        "source_evidence_pack_canonical_sha256": artifact["request"][
            "source_evidence_pack_sha256"
        ],
        "source_candidate_packet_fingerprint": source["packet"][
            "candidate_packet_fingerprint"
        ],
        "source_replayed_on_first_call": source_replayed,
        "source_replayed_after_adoption": source_replayed_again,
        "review_event_count": len(review["review_events"]),
        "review_event_ids": sorted(
            event["decision_event_id"] for event in review["review_events"]
        ),
        "accepted_relation_count": len(accepted_ids),
        "rejected_relation_count": len(rejected_ids),
        "publisher_source_identity_count": len(
            {
                row["source_identity_id"]
                for row in artifact["result"]["basis_relations"]
            }
        ),
        "economic_event_count": 1,
        "economic_event_note": (
            "Four publishers cover one pre-seed financing event; relation "
            "multiplicity does not create additional tile points."
        ),
        "assessment_review": {
            "accepted_assessment_count": sum(
                row["human_decision"] == "accept"
                for row in assessment_rows
            ),
            "adoption_eligible_count": 0,
            "persisted_to_vault": False,
            "proposed_dispositions": {
                row["tile_id"]: row["proposed_disposition"]
                for row in assessment_rows
            },
        },
        "initial_application": initial_application,
        "active_memory_at_start": before_memory["canonical_memory_version"],
        "parent_canonical_memory_version": artifact["request"][
            "parent_canonical_memory_version"
        ],
        "canonical_memory_version": memory["canonical_memory_version"],
        "adoption_event_id": review["adoption"]["event_id"],
        "adoption_sequence": review["adoption"]["sequence"],
        "previous_event_id": review["adoption"]["previous_event_id"],
        "accepted_tile_ids": sorted(
            tile["tile_id"] for tile in memory["content"]["accepted_tiles"]
        ),
        "accepted_memory_relation_count": len(memory_relations),
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


def _validated_relation_rows(
    path: Path,
    *,
    artifact: Mapping[str, Any],
) -> list[dict[str, str]]:
    evidence = {
        row["evidence_fingerprint"]: row
        for row in artifact["result"]["evidence_snapshot"]
    }
    contracts = {
        row["tile_id"]: row for row in build_tile_contract_registry()["tiles"]
    }
    expected: dict[str, dict[str, str]] = {}
    for relation in artifact["result"]["basis_relations"]:
        row = evidence[relation["evidence_fingerprint"]]
        contract = contracts[relation["tile_id"]]
        expected[relation["relation_id"]] = {
            "relation_id": relation["relation_id"],
            "evidence_fingerprint": relation["evidence_fingerprint"],
            "evidence_ref": row["ref"],
            "evidence_url": row.get("url") or "",
            "source_class": row.get("metadata", {}).get("source_class") or "",
            "tile_id": relation["tile_id"],
            "tile_key": contract["tile_key"],
            "tile_condition": contract["condition"],
            "evidence_contract_ok": contract["evidence_contract"]["ok"],
            "evidence_contract_reject": contract["evidence_contract"]["reject"],
            "polarity": relation["polarity"],
            "literal_quote": relation["literal_quote"],
            "rationale": relation["rationale"],
            "review_effect": "relation_decision_only_no_runtime_effect",
            "allowed_human_decisions": "accept|reject",
        }
    return _validated_rows(
        path,
        expected=expected,
        identity_field="relation_id",
        mutable_fields=_RELATION_MUTABLE_FIELDS,
    )


def _validated_assessment_rows(
    path: Path,
    *,
    artifact: Mapping[str, Any],
) -> list[dict[str, str]]:
    unsigned = {
        key: value for key, value in artifact.items() if key != "audit_fingerprint"
    }
    if (
        artifact.get("schema_version")
        != "evidence-vault-independent-contract-audit-v1"
        or artifact.get("audit_fingerprint")
        != canonical_fingerprint(
            "evidence-vault-independent-contract-audit-v1",
            unsigned,
        )
        or artifact.get("authority") is not False
        or artifact.get("runtime_effect") is not False
        or artifact.get("cutover_authorized") is not False
        or artifact.get("review_effect")
        != "coverage_assessment_only_no_adoption"
    ):
        raise RuntimeError("independent coverage assessment artifact is invalid")
    expected: dict[str, dict[str, str]] = {}
    for assessment in artifact["tile_assessments"]:
        payload = {
            key: value
            for key, value in assessment.items()
            if key not in {
                "candidate_id",
                "review_status",
                "decision_event_id",
            }
        }
        if (
            assessment.get("candidate_id")
            != canonical_fingerprint(
                "evidence-vault-independent-contract-audit-candidate-v1",
                payload,
            )
            or assessment.get("adoption_eligible") is not False
            or assessment.get("review_status") != "unreviewed"
            or assessment.get("decision_event_id") is not None
        ):
            raise RuntimeError("independent coverage assessment row is invalid")
        expected[assessment["candidate_id"]] = {
            "candidate_id": assessment["candidate_id"],
            "tile_id": assessment["tile_id"],
            "candidate_status": assessment["status"],
            "proposed_disposition": assessment["proposed_disposition"],
            "evidence_fingerprints": json.dumps(
                [row["evidence_fingerprint"] for row in assessment["evidence"]],
                ensure_ascii=False,
            ),
            "evidence_refs": json.dumps(
                [row["ref"] for row in assessment["evidence"]],
                ensure_ascii=False,
            ),
            "literal_quotes": json.dumps(
                [row["literal_quote"] for row in assessment["evidence"]],
                ensure_ascii=False,
            ),
            "contract_reason": assessment["contract_reason"],
            "limitation": assessment["limitation"],
            "review_effect": "coverage_assessment_only_no_adoption",
            "allowed_human_decisions": "accept|reject",
        }
    return _validated_rows(
        path,
        expected=expected,
        identity_field="candidate_id",
        mutable_fields=_ASSESSMENT_MUTABLE_FIELDS,
    )


def _validated_rows(
    path: Path,
    *,
    expected: Mapping[str, Mapping[str, str]],
    identity_field: str,
    mutable_fields: set[str],
) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != len(expected):
        raise RuntimeError(f"{path.name} row count differs from its artifact")
    actual_ids = [row.get(identity_field) or "" for row in rows]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
        raise RuntimeError(f"{path.name} identities differ from its artifact")
    for row in rows:
        expected_row = expected[row[identity_field]]
        if set(row) != set(expected_row) | mutable_fields:
            raise RuntimeError(f"{path.name} columns differ from its artifact")
        if any(row[field] != value for field, value in expected_row.items()):
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
