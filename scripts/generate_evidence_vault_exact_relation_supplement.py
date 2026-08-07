#!/usr/bin/env python3
"""Generate exact atomic/composite relation worksheets from reviewed assessments."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from src.services.evidence_vault_exact_relation_supplement import (
    build_exact_relation_review_worksheets,
    build_exact_relation_supplement_artifact,
    validate_exact_relation_supplement_artifact,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assessment-artifact", type=Path, required=True)
    parser.add_argument("--assessment-worksheet", type=Path, required=True)
    parser.add_argument("--evidence-pack", type=Path, required=True)
    parser.add_argument("--parent-adoption-result", type=Path, required=True)
    parser.add_argument("--tile", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--atomic-worksheet", type=Path, required=True)
    parser.add_argument("--composite-worksheet", type=Path, required=True)
    args = parser.parse_args()

    assessment = _json_object(args.assessment_artifact.resolve())
    assessment_rows = _csv_rows(args.assessment_worksheet.resolve())
    evidence_pack = _json_object(args.evidence_pack.resolve())
    parent_result = _json_object(args.parent_adoption_result.resolve())
    if (
        parent_result.get("schema_version")
        != "evidence-vault-coverage-human-adoption-result-v1"
        or parent_result.get("brand_identity") != "causaprima.ai"
        or parent_result.get("authority") is not True
        or parent_result.get("authority_scope") != "b3s-vault"
        or parent_result.get("production_runtime_effect") is not False
        or parent_result.get("scanner_runtime_effect") is not False
        or parent_result.get("cutover_authorized") is not False
        or parent_result.get("assessment_review", {}).get(
            "persisted_to_vault"
        )
        is not False
    ):
        raise SystemExit("parent adoption result is not the approved Vault N+1")
    selected = sorted(set(args.tile))
    if selected != args.tile:
        raise SystemExit("--tile values must be unique and sorted")
    worksheet_sha = hashlib.sha256(
        args.assessment_worksheet.resolve().read_bytes()
    ).hexdigest()
    artifact = build_exact_relation_supplement_artifact(
        brand_identity="causaprima.ai",
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version=parent_result[
            "canonical_memory_version"
        ],
        selected_tile_ids=selected,
        assessment_artifact=assessment,
        assessment_review_rows=assessment_rows,
        assessment_worksheet_sha256=worksheet_sha,
        evidence_pack=evidence_pack,
    )
    validate_exact_relation_supplement_artifact(
        artifact,
        assessment_artifact=assessment,
        assessment_review_rows=assessment_rows,
        assessment_worksheet_sha256=worksheet_sha,
        evidence_pack=evidence_pack,
    )
    atomic, composite = build_exact_relation_review_worksheets(artifact)
    if not atomic or not composite:
        raise SystemExit("exact relation supplement requires both review modes")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(args.atomic_worksheet.resolve(), atomic)
    _write_csv(args.composite_worksheet.resolve(), composite)
    print(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
