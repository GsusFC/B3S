#!/usr/bin/env python3
"""Process bounded PR71 operational SV9 shadow work items once.

This external administrative control-plane command is dry-run by default.  It
has no scheduler, API, runtime, or scanner hook; ``--append`` is the only mode
that may persist a row.  Its dedicated database URL is read only from the
process environment and every connection is attested before repository work.
Append mode is limited to one item so failure cannot hide a partial batch.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.pr71_vault_database_target import (  # noqa: E402
    PR71VaultTarget,
    pr71_vault_sv9_shadow_writer_target,
    require_pr71_vault_connection,
    validate_pr71_vault_dsn,
    without_libpq_environment,
)
from scripts.run_evidence_vault_operational_sv9_shadow import (  # noqa: E402
    _error_payload,
    _finish,
    _prepare_output,
)
from src.history.repository import (  # noqa: E402
    EVIDENCE_VAULT_OPERATIONAL_SV9_SHADOW_WORK_ITEM_MAX_LIMIT,
    PostgresHistoryRepository,
)

ADMIN_RUN_SCHEMA_VERSION = (
    "evidence-vault-operational-sv9-shadow-work-items-admin-run-v1"
)
DATABASE_URL_ENV = "B3S_PR71_SV9_SHADOW_WRITER_DATABASE_URL"
_DEFAULT_LIMIT = 1
_POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z")
_ASSESSMENT_STATUSES = frozenset(
    {
        "available",
        "stale_candidate_parent",
        "contradiction_requires_semantic_reassessment",
    }
)


class WorkItemRunnerInputError(ValueError):
    """A work-item runner input failed closed before database access."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        default=str(_DEFAULT_LIMIT),
        help=(
            "Maximum work items (1-"
            f"{EVIDENCE_VAULT_OPERATIONAL_SV9_SHADOW_WORK_ITEM_MAX_LIMIT}); "
            "append mode requires 1."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--append", action="store_true")
    parser.add_argument(
        "--output",
        required=True,
        help="Write sanitized JSON atomically to this path.",
    )
    return parser.parse_args(argv)


def _parse_limit(value: Any, *, append: bool) -> int:
    raw = str(value or "")
    if _POSITIVE_INTEGER_RE.fullmatch(raw) is None or len(raw) > 3:
        raise WorkItemRunnerInputError("invalid work-item limit")
    limit = int(raw)
    if limit > EVIDENCE_VAULT_OPERATIONAL_SV9_SHADOW_WORK_ITEM_MAX_LIMIT:
        raise WorkItemRunnerInputError("invalid work-item limit")
    if append and limit != 1:
        raise WorkItemRunnerInputError("append mode requires limit 1")
    return limit


def _attested_connect(target: PR71VaultTarget) -> Callable[..., Any]:
    def connect(dsn: str, **kwargs: Any) -> Any:
        connection = psycopg.connect(dsn, **kwargs)
        try:
            require_pr71_vault_connection(connection, target=target)
        except Exception:
            connection.close()
            raise
        return connection

    return connect


def _safe_result(
    work_item: Mapping[str, str | None],
    *,
    receipt: Mapping[str, Any],
    replayed: bool,
    dry_run: bool,
) -> dict[str, Any]:
    status = receipt.get("assessment_status")
    return {
        "domain": work_item["domain"],
        "operational_packet_fingerprint": work_item[
            "operational_packet_fingerprint"
        ],
        "expected_parent_canonical_memory_version": work_item[
            "expected_parent_canonical_memory_version"
        ],
        "outcome": (
            "replayed" if replayed else "dry_run" if dry_run else "appended"
        ),
        "assessment_status": (
            status if status in _ASSESSMENT_STATUSES else "unavailable"
        ),
        "replayed": bool(replayed),
    }


def _success_payload(
    *,
    limit: int,
    dry_run: bool,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": ADMIN_RUN_SCHEMA_VERSION,
        "mode": "dry_run" if dry_run else "append",
        "limit": limit,
        "discovered_count": len(results),
        "processed_count": len(results),
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "work_items": results,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    try:
        target_path, temporary_path = _prepare_output(args.output)
    except OSError:
        print(json.dumps(_error_payload("invalid output path"), sort_keys=True))
        return 2

    try:
        limit = _parse_limit(args.limit, append=bool(args.append))
        database_url = str(os.environ.get(DATABASE_URL_ENV) or "")
        if not database_url:
            raise WorkItemRunnerInputError("writer database target is required")
        target = pr71_vault_sv9_shadow_writer_target()
        validate_pr71_vault_dsn(database_url, target=target)
    except Exception:
        if not _finish(
            _error_payload("invalid writer database target or limit"),
            target=target_path,
            temporary=temporary_path,
            fallback_message="could not write output",
        ):
            return 2
        return 2

    dry_run = not bool(args.append)
    try:
        with without_libpq_environment():
            repository = PostgresHistoryRepository(
                database_url,
                connect=_attested_connect(target),
                schema_policy="verify_head",
            )
            work_items = (
                repository.discover_evidence_vault_operational_sv9_shadow_work_items(
                    limit=limit,
                )
            )
            results = []
            for work_item in work_items:
                receipt, replayed = (
                    repository.append_evidence_vault_operational_sv9_shadow_assessment(
                        str(work_item["domain"]),
                        operational_packet_fingerprint=str(
                            work_item["operational_packet_fingerprint"]
                        ),
                        expected_parent_canonical_memory_version=work_item[
                            "expected_parent_canonical_memory_version"
                        ],
                        dry_run=dry_run,
                    )
                )
                results.append(
                    _safe_result(
                        work_item,
                        receipt=receipt,
                        replayed=replayed,
                        dry_run=dry_run,
                    )
                )
        payload = _success_payload(limit=limit, dry_run=dry_run, results=results)
    except Exception:
        if not _finish(
            _error_payload("SV9 shadow work-item processing failed"),
            target=target_path,
            temporary=temporary_path,
            fallback_message="could not write output",
        ):
            return 1
        return 1

    return 0 if _finish(
        payload,
        target=target_path,
        temporary=temporary_path,
        fallback_message="could not write output",
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
