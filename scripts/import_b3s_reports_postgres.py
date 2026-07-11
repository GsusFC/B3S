#!/usr/bin/env python3
"""Validate and import file-backed B3S reports into PostgreSQL history."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import B3S_DATABASE_URL
from src.history.models import HistoricalReport
from src.history.report_parser import parse_report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", default="data/reports")
    parser.add_argument("--database-url", default=B3S_DATABASE_URL)
    parser.add_argument("--workspace-slug", default="b3s")
    parser.add_argument("--workspace-name", default="B3S")
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize without connecting")
    parser.add_argument("--migrate-only", action="store_true", help="Apply schema migrations without importing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.migrate_only and args.dry_run:
        raise SystemExit("--migrate-only and --dry-run are mutually exclusive")

    reports, failures = load_reports(Path(args.reports_dir))
    if failures:
        print(json.dumps({"status": "invalid", "failures": failures}, ensure_ascii=False, indent=2))
        return 1

    if args.dry_run:
        print(
            json.dumps(
                dry_run_summary(reports),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return 0

    from src.history.repository import PostgresHistoryRepository

    repository = PostgresHistoryRepository(args.database_url)
    applied_migrations = repository.migrate()
    if args.migrate_only:
        print(
            json.dumps(
                {"status": "ok", "applied_migrations": applied_migrations},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    outcomes = []
    import_failures = []
    for report in sorted(reports, key=lambda item: (item.observed_at, item.source_report_id)):
        try:
            outcome = repository.import_report(
                report,
                workspace_slug=args.workspace_slug,
                workspace_name=args.workspace_name,
            )
        except Exception as exc:
            import_failures.append(
                {
                    "source_report_id": report.source_report_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        outcomes.append(outcome)

    statuses = Counter(outcome.status for outcome in outcomes)
    payload = {
        "status": "partial" if import_failures else "ok",
        "reports_discovered": len(reports),
        "imported": statuses["imported"],
        "unchanged": statuses["unchanged"],
        "failed": len(import_failures),
        "failures": import_failures,
        "applied_migrations": applied_migrations,
        "storage_counts": repository.storage_counts(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 1 if import_failures else 0


def load_reports(directory: Path) -> tuple[list[HistoricalReport], list[dict[str, str]]]:
    reports: list[HistoricalReport] = []
    failures: list[dict[str, str]] = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            reports.append(parse_report(payload))
        except Exception as exc:
            failures.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return reports, failures


def dry_run_summary(reports: list[HistoricalReport]) -> dict[str, Any]:
    domains = {report.canonical_domain for report in reports}
    return {
        "status": "valid",
        "reports": len(reports),
        "brands": len(domains),
        "first_observed_at": min((report.observed_at for report in reports), default=None),
        "latest_observed_at": max((report.observed_at for report in reports), default=None),
        "evidence_records": sum(len(report.evidence_records) for report in reports),
        "block_interpretations": sum(len(report.block_interpretations) for report in reports),
        "component_evaluations": sum(len(report.components) for report in reports),
        "tile_verdicts": sum(_tile_count(report) for report in reports),
        "pipeline_versions": sorted({report.pipeline_version for report in reports}),
        "rubric_versions": sorted({report.rubric_version for report in reports}),
    }


def _tile_count(report: HistoricalReport) -> int:
    components = report.evaluation_result.get("components")
    components = components if isinstance(components, dict) else {}
    return sum(
        len(component.get("tile_profile") or [])
        for component in components.values()
        if isinstance(component, dict)
    )


if __name__ == "__main__":
    raise SystemExit(main())
