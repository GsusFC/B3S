#!/usr/bin/env python3
"""Run the one-time B3S production migration with a bounded safe receipt."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import migrate_b3s_history_postgres as migration_cli
from scripts import verify_b3s_history_postgres as verification_cli
from scripts.b3s_production_database_target import attach_b3s_production_trust_root


_HEAD_PATTERN = re.compile(r"[0-9]{3}_[a-z0-9_]+\.sql\Z")
_MAX_RECEIPT_BYTES = 8192
_MAX_MANIFEST_COUNT = 10000
_TRUST_ROOT_ENVIRONMENT = "B3S_PRODUCTION_TLS_ROOT_CERT"
_MIGRATION_DSN_ENVIRONMENT = "B3S_MIGRATION_DATABASE_URL"
_MAX_TRUST_ROOT_BYTES = 131072


class _TrustRootError(RuntimeError):
    """The protected production trust root could not be provisioned safely."""


def _protected_trust_root_certificate() -> str:
    certificate = str(os.environ.get(_TRUST_ROOT_ENVIRONMENT, "")).strip()
    if (
        not certificate
        or len(certificate.encode("utf-8")) > _MAX_TRUST_ROOT_BYTES
        or "\x00" in certificate
        or not certificate.startswith("-----BEGIN CERTIFICATE-----")
        or not certificate.endswith("-----END CERTIFICATE-----")
    ):
        raise _TrustRootError
    return f"{certificate}\n"


@contextmanager
def _temporary_profile_dsn():
    original_dsn = os.environ.get(_MIGRATION_DSN_ENVIRONMENT)
    if not str(original_dsn or "").strip():
        yield None
        return

    root_path: str | None = None
    descriptor: int | None = None
    try:
        descriptor, root_path = tempfile.mkstemp(
            prefix="b3s-production-trust-",
            suffix=".pem",
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(_protected_trust_root_certificate())
            handle.flush()
            os.fsync(handle.fileno())
        os.environ[_MIGRATION_DSN_ENVIRONMENT] = attach_b3s_production_trust_root(
            original_dsn,
            trust_root_path=root_path,
        )
        yield root_path
    except _TrustRootError:
        raise
    except Exception:
        raise _TrustRootError from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if original_dsn is None:
            os.environ.pop(_MIGRATION_DSN_ENVIRONMENT, None)
        else:
            os.environ[_MIGRATION_DSN_ENVIRONMENT] = original_dsn
        if root_path is not None:
            try:
                Path(root_path).unlink(missing_ok=True)
            except OSError:
                pass


def _success_payload(output: str, *, includes_applied: bool) -> dict[str, object] | None:
    if len(output) > _MAX_RECEIPT_BYTES:
        return None
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None

    head = payload.get("head")
    count = payload.get("count")
    if (
        not isinstance(head, str)
        or _HEAD_PATTERN.fullmatch(head) is None
        or not isinstance(count, int)
        or isinstance(count, bool)
        or not 0 <= count <= _MAX_MANIFEST_COUNT
    ):
        return None

    result: dict[str, object] = {"head": head, "count": count}
    if includes_applied:
        applied = payload.get("applied")
        if (
            not isinstance(applied, list)
            or len(applied) > count
            or any(
                not isinstance(filename, str)
                or _HEAD_PATTERN.fullmatch(filename) is None
                for filename in applied
            )
        ):
            return None
        result["applied_count"] = len(applied)
    return result


def _run_profiled_cli(
    cli: Callable[[list[str]], int],
    *,
    includes_applied: bool,
) -> dict[str, object] | None:
    output = StringIO()
    try:
        with redirect_stdout(output):
            exit_code = cli(["--target-profile", "b3s-production"])
    except Exception:
        return None
    if exit_code != 0:
        return None
    return _success_payload(output.getvalue(), includes_applied=includes_applied)


def main(argv: list[str] | None = None) -> int:
    """Run migration then exact postflight without accepting a DSN argument."""

    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print(json.dumps({"status": "error", "error": "production migration controller accepts no arguments"}))
        return 2

    try:
        with _temporary_profile_dsn() as profile_dsn:
            if profile_dsn is None:
                print(json.dumps({"status": "error", "error": "production migration failed"}))
                return 1
            migration = _run_profiled_cli(migration_cli.main, includes_applied=True)
            if migration is None:
                print(json.dumps({"status": "error", "error": "production migration failed"}))
                return 1

            postflight = _run_profiled_cli(verification_cli.main, includes_applied=False)
            if postflight is None:
                print(json.dumps({"status": "error", "error": "production migration postflight failed"}))
                return 1
            if (
                migration["head"] != postflight["head"]
                or migration["count"] != postflight["count"]
            ):
                print(json.dumps({"status": "error", "error": "production migration receipt mismatch"}))
                return 1
    except _TrustRootError:
        print(json.dumps({"status": "error", "error": "production migration trust root unavailable"}))
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "head": migration["head"],
                "count": migration["count"],
                "applied_count": migration["applied_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
