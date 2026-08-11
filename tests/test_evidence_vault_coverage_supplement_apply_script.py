from __future__ import annotations

import csv
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_ROOT = Path(__file__).parents[1]
_AUDIT = _ROOT / "audits/evidence_vault_field_validation_v1"
_SCRIPT = _ROOT / "scripts/apply_evidence_vault_coverage_supplement_reviews.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "coverage_review_apply_script", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_completed_coverage_worksheets_validate_at_exact_scope() -> None:
    module = _module()
    relation_artifact = json.loads(
        (_AUDIT / "causa-prima-coverage-supplement-v2.json").read_text(
            encoding="utf-8"
        )
    )
    assessment_artifact = json.loads(
        (_AUDIT / "causa-prima-missing-tile-independent-audit.json").read_text(
            encoding="utf-8"
        )
    )
    relation_rows = module._validated_relation_rows(
        _AUDIT / "causa-prima-coverage-supplement-review-v2.csv",
        artifact=relation_artifact,
    )
    assessment_rows = module._validated_assessment_rows(
        _AUDIT / "causa-prima-missing-tile-independent-review.csv",
        artifact=assessment_artifact,
    )

    assert len(relation_rows) == 4
    assert {row["human_decision"] for row in relation_rows} == {"accept"}
    assert len(assessment_rows) == 8
    assert {row["human_decision"] for row in assessment_rows} == {"accept"}
    assert {
        row["tile_id"]: row["proposed_disposition"]
        for row in assessment_rows
    } == {
        "MG1": "accept",
        "MG3": "accept",
        "MG5": "accept",
        "MG10": "accept",
        "C7": "accept",
        "V3": "sin_evidencia",
        "VA4": "sin_evidencia",
        "I9": "sin_evidencia",
    }


def test_coverage_worksheet_rejects_immutable_quote_drift(tmp_path: Path) -> None:
    module = _module()
    artifact = json.loads(
        (_AUDIT / "causa-prima-coverage-supplement-v2.json").read_text(
            encoding="utf-8"
        )
    )
    source = _AUDIT / "causa-prima-coverage-supplement-review-v2.csv"
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["literal_quote"] += " changed"
    tampered = tmp_path / source.name
    with tampered.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(RuntimeError, match="immutable columns changed"):
        module._validated_relation_rows(tampered, artifact=artifact)


def test_coverage_apply_script_refuses_remote_postgres_before_io() -> None:
    environment = dict(os.environ)
    environment["B3S_FIELD_SHADOW_ALLOW_WRITE"] = "production"
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--dsn",
            "postgresql://prod.example.com/production",
            "--artifact",
            "missing",
            "--worksheet",
            "missing",
            "--evidence-pack",
            "missing",
            "--assessment-artifact",
            "missing",
            "--assessment-worksheet",
            "missing",
            "--validation-manifest",
            "missing",
            "--output",
            "missing",
        ],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "refuses a non-local PostgreSQL DSN" in result.stderr
