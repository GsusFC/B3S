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
        help="Privileged PostgreSQL URL for a non-deployment controlled invocation.",
    )
    parser.add_argument(
        "--target-profile",
        choices=("pr71-vault",),
        help=(
            "Require the immutable isolated target profile. The profile only "
            "accepts B3S_MIGRATION_DATABASE_URL; a URL argument is forbidden."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.target_profile and args.database_url:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "target profiles require B3S_MIGRATION_DATABASE_URL",
                }
            )
        )
        return 2
    database_url = str(
        args.database_url or os.environ.get("B3S_MIGRATION_DATABASE_URL", "")
    ).strip()
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

    from scripts.pr71_vault_database_target import (
        pr71_vault_migration_target,
        require_pr71_vault_connection,
        validate_pr71_vault_dsn,
        without_libpq_environment,
    )
    from src.history.repository import (
        PostgresHistoryRepository,
        _migration_manifest,
    )

    try:
        manifest = _migration_manifest()
        repository = PostgresHistoryRepository(database_url)
        if args.target_profile == "pr71-vault":
            target = pr71_vault_migration_target()
            validate_pr71_vault_dsn(database_url, target=target)
            with without_libpq_environment():
                applied = repository.migrate(
                    connection_preflight=lambda connection: (
                        require_pr71_vault_connection(connection, target=target)
                    )
                )
        else:
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
