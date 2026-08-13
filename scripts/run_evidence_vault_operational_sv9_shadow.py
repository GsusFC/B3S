#!/usr/bin/env python3
"""Run the administrative SV9 operational shadow writer.

The command defaults to a read-only dry-run.  ``--append`` is the explicit
opt-in for the restricted PostgreSQL writer capability.  It never migrates the
schema, exposes packet payloads, or prints database/driver errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.history.repository import PostgresHistoryRepository  # noqa: E402

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
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
    parser.add_argument("--output", default=None, help="Write the sanitized JSON to this path.")
    return parser.parse_args(argv)


def _parse_fingerprint(value: str, *, allow_none: bool = False) -> str | None:
    normalized = str(value or "").strip()
    if allow_none and normalized == "none":
        return None
    if _SHA256_RE.fullmatch(normalized) is None:
        raise ValueError("fingerprint must be a lowercase SHA-256 value")
    return normalized


def _safe_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the repository's bounded receipt; never serialize a row."""

    return {field: receipt.get(field) for field in _SAFE_RECEIPT_FIELDS}


def _write_json(payload: Mapping[str, Any], output: str | None) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if output:
        Path(output).write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)


def _error_payload(message: str) -> dict[str, str]:
    return {"status": "error", "error": message}


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    output = args.output
    try:
        packet_fingerprint = _parse_fingerprint(
            args.operational_packet_fingerprint
        )
        expected_parent = _parse_fingerprint(
            args.expected_parent_canonical_memory_version,
            allow_none=True,
        )
    except ValueError:
        payload = _error_payload("invalid fingerprint arguments")
        try:
            _write_json(payload, output)
        except OSError:
            print(json.dumps(payload, sort_keys=True))
        return 2

    database_url = str(args.database_url or os.environ.get("B3S_DATABASE_URL") or "").strip()
    if not database_url:
        payload = _error_payload("B3S_DATABASE_URL is required")
        try:
            _write_json(payload, output)
        except OSError:
            print(json.dumps(payload, sort_keys=True))
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
            args.domain,
            operational_packet_fingerprint=packet_fingerprint,
            expected_parent_canonical_memory_version=expected_parent,
            dry_run=dry_run,
        )
        payload = {
            "status": "ok",
            "dry_run": dry_run,
            "replayed": bool(replayed),
            "receipt": _safe_receipt(receipt),
        }
    except Exception:
        # Driver/connection/repository diagnostics can contain DSNs, usernames,
        # hostnames, or packet contents.  Keep the CLI boundary sanitized.
        payload = _error_payload("SV9 shadow assessment failed")
        try:
            _write_json(payload, output)
        except OSError:
            print(json.dumps(payload, sort_keys=True))
        return 1

    try:
        _write_json(payload, output)
    except OSError:
        print(json.dumps(_error_payload("could not write output"), sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
