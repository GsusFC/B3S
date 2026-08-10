#!/usr/bin/env python3
"""Fail closed unless this image is releasing to the isolated PR71 Vault."""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import psycopg

_EXPECTED = {
    "FLY_APP_NAME": "b3s-pr71-vault",
    "BRAND3_BASE_URL": "https://b3s-pr71-vault.fly.dev",
    "BRAND3_ENVIRONMENT": "vault",
    "B3S_POSTGRES_REQUIRED": "true",
    "B3S_SITE_BASIC_AUTH_ENABLED": "true",
    "B3S_SITE_BASIC_AUTH_USERNAME": "vault",
    "B3S_EXPECTED_NEON_PROJECT_ID": "jolly-river-32467750",
    "B3S_EXPECTED_NEON_BRANCH_ID": "br-divine-star-aspobuer",
    "B3S_EXPECTED_NEON_ENDPOINT_HOST": (
        "ep-broad-river-as71uv9y.c-4.eu-central-1.aws.neon.tech"
    ),
    "B3S_EXPECTED_DATABASE_NAME": "neondb",
    "B3S_VAULT_WORKER_ENABLED": "false",
    "BRAND3_VAULT_C7_CUTOVER_ENABLED": "false",
    "BRAND3_VAULT_C7_EMERGENCY_DENY": "true",
    "BRAND3_VAULT_C7_ALLOWLIST": "",
    "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED": "false",
    "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH": "",
}


def main() -> int:
    for name, expected in _EXPECTED.items():
        if os.environ.get(name, "") != expected:
            raise SystemExit("isolated Vault deployment target verification failed")
    dsn = os.environ.get("B3S_DATABASE_URL", "").strip()
    try:
        dsn_hostname = urlsplit(dsn).hostname
    except Exception:
        dsn_hostname = None
    if dsn_hostname != _EXPECTED["B3S_EXPECTED_NEON_ENDPOINT_HOST"]:
        raise SystemExit("isolated Vault deployment target verification failed")
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            row = connection.execute(
                """
                SELECT current_database(),
                       current_setting('neon.project_id', true),
                       current_setting('neon.branch_id', true)
                """
            ).fetchone()
    except Exception:
        raise SystemExit("isolated Vault deployment target verification failed") from None
    if row != (
        _EXPECTED["B3S_EXPECTED_DATABASE_NAME"],
        _EXPECTED["B3S_EXPECTED_NEON_PROJECT_ID"],
        _EXPECTED["B3S_EXPECTED_NEON_BRANCH_ID"],
    ):
        raise SystemExit("isolated Vault deployment target verification failed")
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
