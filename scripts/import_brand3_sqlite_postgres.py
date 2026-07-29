#!/usr/bin/env python3
"""Import compatible Brand3 captures into an isolated PostgreSQL workspace."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import B3S_DATABASE_URL
from src.services.brand3_sqlite_history_import import (
    BRAND3_ARCHIVE_WORKSPACE_NAME,
    BRAND3_ARCHIVE_WORKSPACE_SLUG,
    load_brand3_archive_history_reports,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="Path to the Brand3 SQLite archive")
    parser.add_argument(
        "--database-url",
        default=B3S_DATABASE_URL,
        help="Target PostgreSQL DSN; used only with --apply",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Persist into the fixed b3s-archive workspace. Without this "
            "flag the command only validates and prints the plan."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        reports, plan = load_brand3_archive_history_reports(
            args.database
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "invalid",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "valid",
                    "action": "dry_run",
                    "applied": False,
                    **plan,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not reports:
        print(
            json.dumps(
                {
                    "status": "empty",
                    "action": "apply",
                    "applied": False,
                    **plan,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    try:
        from src.history.repository import PostgresHistoryRepository

        repository = PostgresHistoryRepository(args.database_url)
        applied_migrations = repository.migrate()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "action": "apply",
                    "applied": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    **plan,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    outcomes = []
    failures: list[dict[str, str]] = []
    for report in reports:
        try:
            outcomes.append(
                repository.import_report(
                    report,
                    workspace_slug=BRAND3_ARCHIVE_WORKSPACE_SLUG,
                    workspace_name=BRAND3_ARCHIVE_WORKSPACE_NAME,
                )
            )
        except Exception as exc:
            failures.append(
                {
                    "source_report_id": report.source_report_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    statuses = Counter(outcome.status for outcome in outcomes)
    payload: dict[str, Any] = {
        "status": "partial" if failures else "ok",
        "action": "apply",
        "applied": bool(outcomes),
        **plan,
        "import": {
            "discovered": len(reports),
            "imported": int(statuses["imported"]),
            "unchanged": int(statuses["unchanged"]),
            "failed": len(failures),
            "failures": failures,
            "applied_migrations": applied_migrations,
        },
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
