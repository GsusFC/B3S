#!/usr/bin/env python3
"""Offline reconciliation of the serving brand catalog from PostgreSQL and files."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.brand_catalog import repair_catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", default=os.environ.get("B3S_REPORTS_DIR", "data/reports"))
    arguments = sys.argv[1:] if argv is None else argv
    if any(item == "--database-url" or item.startswith("--database-url=") for item in arguments):
        parser.error("Use B3S_DATABASE_URL instead of command-line credentials")
    args = parser.parse_args(arguments)
    database_url = os.environ.get("B3S_DATABASE_URL", "").strip()
    if os.environ.get("B3S_POSTGRES_REQUIRED", "").strip().casefold() == "true" and not database_url:
        print(json.dumps({"status": "incomplete", "error": "required PostgreSQL is not configured"}))
        return 1

    try:
        repository = None
        if database_url:
            from src.history.repository import PostgresHistoryRepository

            repository = PostgresHistoryRepository(database_url, schema_policy="verify_head")
        count = repair_catalog(Path(args.catalog_root), repository=repository)
    except Exception as exc:
        print(json.dumps({"status": "incomplete", "error": type(exc).__name__}))
        return 1
    print(json.dumps({"status": "ready", "reports_indexed": count, "postgres_reconciled": repository is not None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
