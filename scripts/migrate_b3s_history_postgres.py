#!/usr/bin/env python3
"""Apply B3S history migrations with a privileged database identity."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="Privileged PostgreSQL URL for this controlled invocation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    database_url = str(args.database_url or os.environ.get("B3S_MIGRATION_DATABASE_URL", "")).strip()
    if not database_url:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "B3S_MIGRATION_DATABASE_URL is required",
                }
            )
        )
        return 2

    from src.history.repository import (
        PostgresHistoryRepository,
        _migration_manifest,
    )

    try:
        manifest = _migration_manifest()
        repository = PostgresHistoryRepository(database_url)
        applied = repository.migrate()
    except Exception:
        print(json.dumps({"status": "error", "error": "history migration failed"}))
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "head": manifest[-1][1] if manifest else None,
                "count": len(manifest),
                "applied": applied,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
