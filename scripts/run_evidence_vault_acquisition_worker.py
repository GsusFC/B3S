#!/usr/bin/env python3
"""Run the isolated verified-raw acquisition worker on one Unix socket.

Secret material is read only from owner-restricted files in this process.  The
web/scanner process needs only the public socket path and the default-off flag.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qsl, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx

from src.history.evidence_vault_raw_repository import (
    EvidenceVaultRawPlanningContext,
    EvidenceVaultRawRepository,
)
from src.services.evidence_vault_acquisition_ipc_server import (
    serve_unix_trusted_acquisition,
)
from src.services.evidence_vault_acquisition_runtime import (
    HttpxExaExactUrlFetcher,
    HttpxOwnedFetcher,
    PublicOnlyHTTPTransport,
    TrustedAcquisitionRuntime,
)
from src.services.evidence_vault_acquisition_worker import TrustedAcquisitionWorker
from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan
from src.services.evidence_vault_raw_provenance import PublicKeyRegistry


_MAX_SECRET_BYTES = 16_384
_EXPECTED_SCANNER_ROLE = "b3s_pr71_scanner_ingest"
_T = TypeVar("_T")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-path", type=Path, required=True)
    parser.add_argument("--private-key-file", type=Path, required=True)
    parser.add_argument("--registry-file", type=Path, required=True)
    parser.add_argument("--ingest-dsn-file", type=Path, required=True)
    parser.add_argument("--exa-api-key-file", type=Path)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-database-hostname", required=True)
    parser.add_argument("--expected-neon-project-id", required=True)
    parser.add_argument("--expected-neon-branch-id", required=True)
    parser.add_argument("--socket-mode", choices=("0600", "0660"), default="0660")
    parser.add_argument(
        "--allow-owned-only-downgrade",
        action="store_true",
        help=(
            "Explicitly persist a signed owned-only outcome when an owned "
            "external link cannot be acquired; disabled by default"
        ),
    )
    args = parser.parse_args()
    try:
        return _run(args)
    except Exception:
        print("verified-raw worker failed: startup_failed", file=sys.stderr)
        return 78


def _run(args: argparse.Namespace) -> int:
    private_key = _load_private_key(args.private_key_file)
    registry = _load_json_model(args.registry_file, PublicKeyRegistry)
    ingest_dsn = _load_secret_text(args.ingest_dsn_file, field="ingest_dsn")
    _validate_postgres_dsn(ingest_dsn)
    if (
        urlsplit(ingest_dsn).hostname or ""
    ).lower() != args.expected_database_hostname.lower():
        raise ValueError("ingest_dsn_target_invalid")
    exa_api_key = (
        _load_secret_text(args.exa_api_key_file, field="exa_api_key")
        if args.exa_api_key_file is not None
        else None
    )

    # Ignore ambient proxy/netrc settings so acquisition egress is deployment-owned.
    with httpx.Client(
        transport=PublicOnlyHTTPTransport(),
        trust_env=False,
    ) as owned_client, httpx.Client(
        transport=PublicOnlyHTTPTransport(),
        trust_env=False,
    ) as provider_client:
        owned_fetcher = HttpxOwnedFetcher(owned_client)
        external_fetcher = (
            HttpxExaExactUrlFetcher(provider_client, api_key=exa_api_key)
            if exa_api_key is not None
            else None
        )
        runtime = TrustedAcquisitionRuntime(
            private_key=private_key,
            public_key_registry=registry,
            owned_fetch=owned_fetcher,
            external_fetch=external_fetcher,
            allow_owned_only_downgrade=args.allow_owned_only_downgrade,
        )
        repository = EvidenceVaultRawRepository(
            ingest_dsn,
            public_key_registry=registry,
            operation_plan_builder=_operation_plan,
            expected_database=args.expected_database,
            expected_role=_EXPECTED_SCANNER_ROLE,
            expected_neon_project_id=args.expected_neon_project_id,
            expected_neon_branch_id=args.expected_neon_branch_id,
        )
        repository.verify_ingest_capability()
        worker = TrustedAcquisitionWorker(
            collect=runtime.collect,
            sign=runtime.sign,
            persist=repository.persist,
            lookup=repository.lookup,
            public_key_registry=registry,
        )
        serve_unix_trusted_acquisition(
            worker,
            args.socket_path,
            socket_mode=int(args.socket_mode, 8),
        )
    return 0


def _operation_plan(
    command: Any,
    evidence_records: Any,
    planning_context: EvidenceVaultRawPlanningContext,
) -> dict[str, Any]:
    canonical_domain = urlsplit(command.brand_url).hostname
    if not isinstance(canonical_domain, str) or not canonical_domain:
        raise ValueError("canonical brand domain is absent")
    mode = (
        "incremental_refresh"
        if planning_context.canonical_memory_version is not None
        else "baseline"
    )
    # The isolated worker receives no ambient cutover configuration.  C7 is
    # therefore excluded fail-closed; a later reviewed extension may inject an
    # explicit safe cutover decision rather than inheriting process state.
    relations = [
        dict(row)
        for row in planning_context.accepted_evidence_tile_relations
        if str(row.get("tile_id") or "") != "C7"
    ]
    return build_vault_scan_plan(
        brand_identity=canonical_domain,
        subject_url=command.brand_url,
        mode=mode,
        current_evidence_records=evidence_records,
        previous_capture_evidence_records=(
            planning_context.previous_capture_evidence_records
        ),
        known_evidence_records=planning_context.known_evidence_records,
        accepted_evidence_tile_relations=relations,
        canonical_memory_version=planning_context.canonical_memory_version,
    )


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    encoded = _load_secret_text(path, field="private_key")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != 32:
            raise ValueError("wrong Ed25519 private key size")
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception:
        raise ValueError("private_key_file_invalid") from None


def _load_secret_text(path: Path, *, field: str) -> str:
    raw = _read_regular_file(path, maximum=_MAX_SECRET_BYTES, private=True)
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError(f"{field}_file_invalid") from None
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError(f"{field}_file_invalid")
    return value


def _load_json_model(path: Path, model: type[_T]) -> _T:
    raw = _read_regular_file(path, maximum=131_072, private=False)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        return model.model_validate(value, strict=True)  # type: ignore[attr-defined]
    except Exception:
        raise ValueError("public_key_registry_file_invalid") from None


def _read_regular_file(path: Path, *, maximum: int, private: bool) -> bytes:
    candidate = Path(path)
    try:
        before = candidate.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            raise ValueError("unsafe file owner or type")
        if private and stat.S_IMODE(before.st_mode) & 0o077:
            raise ValueError("secret file permissions are not owner-only")
        if not 0 < before.st_size <= maximum:
            raise ValueError("file size invalid")
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("file changed during open")
            raw = os.read(descriptor, maximum + 1)
            trailing = os.read(descriptor, 1)
        finally:
            os.close(descriptor)
        if trailing or len(raw) != before.st_size or len(raw) > maximum:
            raise ValueError("file changed or exceeds limit")
        return raw
    except Exception:
        raise ValueError("worker_file_invalid") from None


def _validate_postgres_dsn(value: str) -> None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"postgres", "postgresql"}
            or not parsed.hostname
            or not parsed.username
            or not parsed.password
            or not parsed.path
            or parsed.path == "/"
            or parsed.fragment
        ):
            raise ValueError
        _ = parsed.port
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
        keys = [key for key, _item in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError
        if {
            "host",
            "hostaddr",
            "user",
            "password",
            "dbname",
            "port",
            "service",
            "servicefile",
            "options",
        }.intersection(keys):
            raise ValueError
        ssl_modes = [item for key, item in pairs if key == "sslmode"]
        channel_bindings = [
            item for key, item in pairs if key == "channel_binding"
        ]
        authenticated_tls = ssl_modes == ["verify-full"] or (
            ssl_modes == ["require"] and channel_bindings == ["require"]
        )
        if not authenticated_tls:
            raise ValueError
    except Exception:
        raise ValueError("ingest_dsn_file_invalid") from None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value}")


if __name__ == "__main__":
    raise SystemExit(main())
