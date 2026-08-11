#!/usr/bin/env python3
"""Fail closed unless this image is releasing to the isolated PR71 Vault."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pr71_vault_database_target import (  # noqa: E402
    PR71_VAULT_BRANCH_ID,
    PR71_VAULT_DATABASE,
    PR71_VAULT_HOST,
    PR71_VAULT_PROJECT_ID,
    PR71_VAULT_RUNTIME_ROLE,
    pr71_vault_runtime_target,
    require_pr71_vault_connection,
    validate_pr71_vault_dsn,
    without_libpq_environment,
)

_EXPECTED = {
    "FLY_APP_NAME": "b3s-pr71-vault",
    "BRAND3_BASE_URL": "https://b3s-pr71-vault.fly.dev",
    "BRAND3_ENVIRONMENT": "vault",
    "BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED": "true",
    "B3S_POSTGRES_REQUIRED": "true",
    "B3S_SITE_BASIC_AUTH_ENABLED": "true",
    "B3S_SITE_BASIC_AUTH_USERNAME": "vault",
    "B3S_EXPECTED_NEON_PROJECT_ID": PR71_VAULT_PROJECT_ID,
    "B3S_EXPECTED_NEON_BRANCH_ID": PR71_VAULT_BRANCH_ID,
    "B3S_EXPECTED_NEON_ENDPOINT_HOST": PR71_VAULT_HOST,
    "B3S_EXPECTED_DATABASE_NAME": PR71_VAULT_DATABASE,
    "B3S_EXPECTED_RUNTIME_ROLE": PR71_VAULT_RUNTIME_ROLE,
    "B3S_VAULT_WORKER_ENABLED": "true",
    "BRAND3_VAULT_C7_CUTOVER_ENABLED": "false",
    "BRAND3_VAULT_C7_EMERGENCY_DENY": "true",
    "BRAND3_VAULT_C7_ALLOWLIST": "",
    "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED": "true",
    "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH": "",
}


def _validate_target_dsn(value: str, *, expected_host: str) -> None:
    """Backward-compatible test boundary around the immutable runtime profile."""

    if expected_host != PR71_VAULT_HOST:
        raise ValueError("unexpected PostgreSQL host")
    validate_pr71_vault_dsn(value, target=pr71_vault_runtime_target())


def main() -> int:
    for name, expected in _EXPECTED.items():
        if os.environ.get(name, "") != expected:
            raise SystemExit("isolated Vault deployment target verification failed")
    dsn = os.environ.get("B3S_DATABASE_URL", "").strip()
    target = pr71_vault_runtime_target()
    try:
        validate_pr71_vault_dsn(dsn, target=target)
        with without_libpq_environment():
            with psycopg.connect(dsn, connect_timeout=5) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                require_pr71_vault_connection(connection, target=target)
    except Exception:
        raise SystemExit("isolated Vault deployment target verification failed") from None
    print(
        json.dumps(
            {
                "status": "ok",
                "app": _EXPECTED["FLY_APP_NAME"],
                "database": _EXPECTED["B3S_EXPECTED_DATABASE_NAME"],
                "neon_project_id": _EXPECTED["B3S_EXPECTED_NEON_PROJECT_ID"],
                "neon_branch_id": _EXPECTED["B3S_EXPECTED_NEON_BRANCH_ID"],
                "c7_cutover": "disabled",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
