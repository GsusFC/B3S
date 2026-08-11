from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest

from src.services.evidence_vault_exact_relation_supplement import (
    build_exact_relation_review_worksheets,
)


_ROOT = Path(__file__).parents[1]
_AUDIT = _ROOT / "audits/evidence_vault_field_validation_v1"


def test_exact_review_sheet_validation_rejects_blank_or_drifted_rows(
    tmp_path: Path,
) -> None:
    module = _load_apply_script()
    artifact = json.loads(
        (_AUDIT / "causa-prima-exact-relation-supplement-v1.json").read_text(
            encoding="utf-8"
        )
    )
    atomic, composite = build_exact_relation_review_worksheets(artifact)
    blank = tmp_path / "blank.csv"
    _write_rows(blank, atomic)
    with pytest.raises(RuntimeError, match="invalid human decision"):
        module._validated_rows(
            blank,
            expected=atomic,
            identity_field="relation_id",
        )

    completed = [
        {
            **row,
            "human_decision": "accept",
            "human_rationale": "Exact relation content and scope accepted.",
            "reviewer_id": "gsus",
            "reviewed_at": "2026-08-07T12:30:00+02:00",
        }
        for row in atomic
    ]
    completed_path = tmp_path / "completed.csv"
    _write_rows(completed_path, completed)
    validated = module._validated_rows(
        completed_path,
        expected=atomic,
        identity_field="relation_id",
    )
    assert len(validated) == 3

    drifted = [dict(row) for row in completed]
    drifted[0]["literal_quote"] += " changed"
    _write_rows(completed_path, drifted)
    with pytest.raises(RuntimeError, match="immutable columns changed"):
        module._validated_rows(
            completed_path,
            expected=atomic,
            identity_field="relation_id",
        )

    assert len(composite) == 1
    assert composite[0]["decision_rule"] == "all_of"
    assert len(json.loads(composite[0]["member_relation_ids"])) == 2


def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _load_apply_script():
    path = _ROOT / "scripts/apply_evidence_vault_exact_relation_reviews.py"
    spec = importlib.util.spec_from_file_location("exact_relation_apply", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load exact relation apply script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
