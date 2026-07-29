#!/usr/bin/env python3
"""Validate Brand3 SQLite history against B3S additive evidence memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.services.brand3_sqlite_memory_backfill import (
    build_brand3_sqlite_memory_validation,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read a Brand3 SQLite archive without mutation and compare "
            "evaluator repeats, captures, and current B3S reports."
        )
    )
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--current-reports-dir",
        type=Path,
        help="Optional directory containing current B3S report JSON.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
    )
    args = parser.parse_args()
    current_reports = (
        _load_reports(args.current_reports_dir)
        if args.current_reports_dir
        else []
    )
    payload = build_brand3_sqlite_memory_validation(
        args.database,
        current_reports=current_reports,
    )
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_markdown(payload))
    return 0


def _load_reports(directory: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            reports.append(payload)
    return reports


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Brand3 SQLite memory backfill",
        "",
        f"- Archive scans: `{summary.get('archive_scan_count', 0)}`",
        f"- Archive captures: `{summary.get('archive_capture_count', 0)}`",
        f"- Domains: `{summary.get('archive_domain_count', 0)}`",
        f"- Evaluator repeats: `{summary.get('evaluator_repeat_count', 0)}`",
        (
            "- Accepted reproducible evidence: "
            f"`{summary.get('archive_accepted_evidence_count', 0)}`"
        ),
        (
            "- Evaluator-repeat recoveries: "
            f"`{summary.get('evaluator_repeat_recovery_count', 0)}`"
        ),
        (
            "- Cross-capture recoveries: "
            f"`{summary.get('capture_recovery_count', 0)}`"
        ),
        (
            "- Bridge recoveries: "
            f"`{summary.get('bridge_recovery_count', 0)}`"
        ),
        "- Runtime effect: `false`",
        "- Archive mutation: `false`",
        f"- Promotion ready: `{str(bool(payload.get('promotion_ready'))).lower()}`",
        "",
        (
            "| Domain | Scans | Captures | Repeats | Evidence | "
            "All scans Δ | Capture Δ | Bridge Δ |"
        ),
        (
            "| --- | ---: | ---: | ---: | ---: | ---: | "
            "---: | ---: |"
        ),
    ]
    for row in payload.get("domains") or []:
        all_scan = row.get("all_scan_preview") or {}
        capture = row.get("capture_preview") or {}
        bridge = row.get("bridge_preview") or {}
        lines.append(
            "| {domain} | {scans} | {captures} | {repeats} | "
            "{evidence} | {repeat_delta} | {capture_delta} | "
            "{bridge_delta} |".format(
                domain=row.get("domain") or "",
                scans=row.get("archive_scan_count") or 0,
                captures=row.get("archive_capture_count") or 0,
                repeats=row.get("evaluator_repeat_count") or 0,
                evidence=all_scan.get(
                    "accepted_evidence_count"
                )
                or 0,
                repeat_delta=_display(
                    all_scan.get("score_delta")
                ),
                capture_delta=_display(
                    capture.get("score_delta")
                ),
                bridge_delta=_display(
                    bridge.get("score_delta")
                ),
            )
        )
    blockers = payload.get("promotion_blockers") or []
    if blockers:
        lines.extend(
            [
                "",
                "## Promotion blockers",
                "",
                *[f"- `{blocker}`" for blocker in blockers],
            ]
        )
    candidates = payload.get("recovery_review_candidates") or []
    if candidates:
        lines.extend(
            [
                "",
                "## Recovery review candidates",
                "",
                "| Domain | Lane | Tile | Decision |",
                "| --- | --- | --- | --- |",
            ]
        )
        for candidate in candidates:
            lines.append(
                "| {domain} | {lane} | {component}.{tile} | "
                "{decision} |".format(
                    domain=candidate.get("domain") or "",
                    lane=candidate.get("lane") or "",
                    component=candidate.get("component_key")
                    or "",
                    tile=candidate.get("tile_id") or "",
                    decision=candidate.get("decision") or "",
                )
            )
    return "\n".join(lines) + "\n"


def _display(value: Any) -> str:
    return "—" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
