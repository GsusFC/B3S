#!/usr/bin/env python3
"""Run the administrative SV9 operational shadow writer.

The command defaults to a read-only dry-run.  ``--append`` is the explicit
opt-in for the restricted PostgreSQL writer capability.  It never migrates the
schema, exposes packet payloads, or prints database/driver errors.  The output
path is prevalidated before PostgreSQL is opened and replaced atomically from a
temporary file in the same directory; append persistence therefore never
truncates or partially writes an existing report.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.history.report_parser import normalize_domain  # noqa: E402
from src.history.repository import PostgresHistoryRepository  # noqa: E402

ADMIN_RUN_SCHEMA_VERSION = "evidence-vault-operational-sv9-shadow-admin-run-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MALFORMED_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SAFE_RECEIPT_FIELDS = (
    "schema_version",
    "assessment_id",
    "evaluation_identity",
    "operational_packet_fingerprint",
    "source_candidate_packet_fingerprint",
    "expected_parent_canonical_memory_version",
    "candidate_overlay_version",
    "assessment_status",
    "assessment_fingerprint",
    "score_fingerprint",
    "sv9_score",
    "base_average",
    "magnetism_capped",
    "legacy_operational_projection",
    "semantic_provenance_fingerprint",
    "authority",
    "production_runtime_effect",
    "scanner_runtime_effect",
    "created_at",
)


class RunnerInputError(ValueError):
    """The runner input cannot be normalized safely before opening PostgreSQL."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--operational-packet-fingerprint", required=True)
    parser.add_argument(
        "--expected-parent-canonical-memory-version",
        required=True,
        help="A lowercase SHA-256 fingerprint or the literal 'none'.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--append", action="store_true")
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL; defaults to B3S_DATABASE_URL.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Write the sanitized JSON atomically to this path.",
    )
    return parser.parse_args(argv)


def _parse_fingerprint(value: str, *, allow_none: bool = False) -> str | None:
    normalized = str(value or "").strip()
    if allow_none and normalized == "none":
        return None
    if _SHA256_RE.fullmatch(normalized) is None:
        raise RunnerInputError("invalid fingerprint arguments")
    return normalized


def _normalize_input_domain(value: str) -> str:
    raw = str(value or "")
    if (
        not raw
        or raw != raw.strip()
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            or character == "\\"
            for character in raw
        )
    ):
        raise RunnerInputError("invalid domain argument")
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlsplit(candidate)
        # Accessing .port validates malformed numeric ports without retaining
        # any user-provided authority or credentials in the error surface.
        _ = parsed.port
        normalized = normalize_domain(raw)
    except (TypeError, ValueError):
        raise RunnerInputError("invalid domain argument") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or not normalized
    ):
        raise RunnerInputError("invalid domain argument")
    return normalized


def _validate_database_url(value: str) -> str:
    dsn = str(value or "").strip()
    try:
        parsed = urlsplit(dsn)
        _ = parsed.port
        parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        raise RunnerInputError("invalid PostgreSQL URL") from None
    host = parsed.hostname
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not host
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            or character == "\\"
            for character in host
        )
        or _MALFORMED_PERCENT_ESCAPE_RE.search(parsed.query)
        or not parsed.path.removeprefix("/")
        or parsed.fragment
    ):
        raise RunnerInputError("invalid PostgreSQL URL")
    return dsn


def _safe_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the repository's bounded receipt; never serialize a row."""

    return {field: receipt.get(field) for field in _SAFE_RECEIPT_FIELDS}


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _comparison(receipt: Mapping[str, Any]) -> dict[str, Any]:
    shadow_score = _finite_number(receipt.get("sv9_score"))
    legacy_projection = receipt.get("legacy_operational_projection")
    legacy_score = (
        _finite_number(legacy_projection.get("score"))
        if isinstance(legacy_projection, Mapping)
        else None
    )
    comparison = {
        "shadow_score": shadow_score,
        "legacy_operational_projection": (
            dict(legacy_projection) if isinstance(legacy_projection, Mapping) else None
        ),
    }
    if shadow_score is not None and legacy_score is not None:
        comparison["score_delta"] = shadow_score - legacy_score
    return comparison


def _success_payload(
    *,
    domain: str,
    packet_fingerprint: str,
    expected_parent: str | None,
    dry_run: bool,
    replayed: bool,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": ADMIN_RUN_SCHEMA_VERSION,
        "input": {
            "domain": domain,
            "operational_packet_fingerprint": packet_fingerprint,
            "expected_parent_canonical_memory_version": expected_parent,
        },
        "mode": "dry_run" if dry_run else "append",
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "replayed": bool(replayed),
        "shadow_receipt": _safe_receipt(receipt),
        "comparison": _comparison(receipt),
    }


def _error_payload(message: str) -> dict[str, str]:
    return {"status": "error", "error": message}


def _prepare_output(path_value: str) -> tuple[Path, Path]:
    target = Path(str(path_value)).expanduser()
    parent = target.parent
    if not parent.exists() or not parent.is_dir() or target.exists() and target.is_dir():
        raise OSError("invalid output path")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(parent),
    )
    os.close(descriptor)
    return target, Path(temporary_name)


def _cleanup_temporary(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_json_atomic(
    payload: Mapping[str, Any],
    *,
    target: Path,
    temporary: Path,
) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        _cleanup_temporary(temporary)


def _finish(
    payload: Mapping[str, Any],
    *,
    target: Path,
    temporary: Path,
    fallback_message: str,
) -> bool:
    try:
        _write_json_atomic(payload, target=target, temporary=temporary)
        return True
    except Exception:
        _cleanup_temporary(temporary)
        print(json.dumps(_error_payload(fallback_message), sort_keys=True))
        return False


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    try:
        target, temporary = _prepare_output(args.output)
    except OSError:
        print(json.dumps(_error_payload("invalid output path"), sort_keys=True))
        return 2

    try:
        domain = _normalize_input_domain(args.domain)
        packet_fingerprint = _parse_fingerprint(args.operational_packet_fingerprint)
        expected_parent = _parse_fingerprint(
            args.expected_parent_canonical_memory_version,
            allow_none=True,
        )
        database_candidate = str(
            args.database_url or os.environ.get("B3S_DATABASE_URL") or ""
        ).strip()
        if not database_candidate:
            raise RunnerInputError("B3S_DATABASE_URL is required")
        database_url = _validate_database_url(database_candidate)
    except RunnerInputError as exc:
        if not _finish(
            _error_payload(str(exc)),
            target=target,
            temporary=temporary,
            fallback_message="could not write output",
        ):
            return 2
        return 2

    dry_run = not bool(args.append)
    try:
        # verify_head is deliberate: this administrative runner is never a
        # migration capability.  The URL must resolve to the dedicated writer
        # role provisioned by migration 027 for append mode.
        repository = PostgresHistoryRepository(
            database_url,
            schema_policy="verify_head",
        )
        receipt, replayed = repository.append_evidence_vault_operational_sv9_shadow_assessment(
            domain,
            operational_packet_fingerprint=packet_fingerprint,
            expected_parent_canonical_memory_version=expected_parent,
            dry_run=dry_run,
        )
        payload = _success_payload(
            domain=domain,
            packet_fingerprint=packet_fingerprint,
            expected_parent=expected_parent,
            dry_run=dry_run,
            replayed=replayed,
            receipt=receipt,
        )
    except Exception:
        # Driver/connection/repository diagnostics can contain DSNs, usernames,
        # hostnames, or packet contents.  Keep the CLI boundary sanitized.
        if not _finish(
            _error_payload("SV9 shadow assessment failed"),
            target=target,
            temporary=temporary,
            fallback_message="could not write output",
        ):
            return 1
        return 1

    return 0 if _finish(
        payload,
        target=target,
        temporary=temporary,
        fallback_message="could not write output",
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
