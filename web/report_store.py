"""File-backed report store for B3S lab scans.

One JSON file per scan under data/reports/. Deliberately boring: the lab's
artifacts are files first; Postgres replaces this seam when the store port
lands (see README, Database).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def reports_dir() -> Path:
    return Path(os.environ.get("B3S_REPORTS_DIR", "data/reports"))


def new_scan_id() -> str:
    return uuid.uuid4().hex[:12]


def report_path(scan_id: str) -> Path:
    return reports_dir() / f"{scan_id}.json"


def save_report(report: dict[str, Any]) -> None:
    path = report_path(str(report["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


def load_report(scan_id: str) -> dict[str, Any] | None:
    path = report_path(scan_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_reports() -> list[dict[str, Any]]:
    """Return summary rows for every stored report, newest first."""

    rows: list[dict[str, Any]] = []
    directory = reports_dir()
    if not directory.is_dir():
        return rows
    for path in directory.glob("*.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append(
            {
                "id": report.get("id") or path.stem,
                "brand_name": report.get("brand_name") or "",
                "url": report.get("url") or "",
                "created_at": report.get("created_at") or "",
                "score": report.get("score"),
                "detected_count": report.get("detected_count"),
                "block_count": report.get("block_count"),
                "not_detected": report.get("not_detected") or [],
            }
        )
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows
