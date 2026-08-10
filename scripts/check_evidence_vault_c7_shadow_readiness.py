#!/usr/bin/env python3
"""Report private C7 verified-raw shadow readiness without exposing its witness."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.history.evidence_vault_c7_shadow_repository import (  # noqa: E402
    EvidenceVaultC7ShadowRepository,
)
from src.services.evidence_vault_c7_shadow_readiness import (  # noqa: E402
    C7ShadowReadinessReason,
    C7ShadowReadinessResult,
    storage_unavailable_result,
)
from src.services.evidence_vault_raw_provenance import (  # noqa: E402
    PublicKeyRegistry,
)


_RUNTIME_READ_DATABASE_URL_ENV = "B3S_C7_RUNTIME_READ_DATABASE_URL"
_MAX_REGISTRY_BYTES = 131_072
_MAX_DSN_CHARS = 16_384
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_USAGE_EXIT = 2
_NOT_READY_EXIT = 1


class _ArgumentError(Exception):
    pass


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _ArgumentError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _SanitizedArgumentParser(
        description=__doc__,
        allow_abbrev=False,
    )
    parser.add_argument("--domain", required=True)
    parser.add_argument("--registry-file", type=Path, required=True)
    parser.add_argument("--expected-registry-fingerprint", required=True)
    parser.add_argument("--require-ready", action="store_true")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    try:
        args = parse_args(argv)
    except _ArgumentError:
        _emit(storage_unavailable_result())
        return _USAGE_EXIT

    if not _SHA256_PATTERN.fullmatch(args.expected_registry_fingerprint):
        _emit(_verification_policy_invalid_result())
        return _USAGE_EXIT

    environment = os.environ if environ is None else environ
    try:
        dsn = _runtime_read_dsn(environment)
    except Exception:
        _emit(storage_unavailable_result())
        return _USAGE_EXIT

    try:
        registry = _load_public_key_registry(args.registry_file)
    except Exception:
        _emit(_verification_policy_invalid_result())
        return _USAGE_EXIT

    try:
        repository = EvidenceVaultC7ShadowRepository(
            dsn,
            public_key_registry=registry,
            expected_public_key_registry_fingerprint=(
                args.expected_registry_fingerprint
            ),
        )
        result = repository.get_evidence_vault_c7_shadow_readiness(args.domain)
        if not isinstance(result, C7ShadowReadinessResult):
            raise TypeError("unexpected readiness result")
    except Exception:
        result = storage_unavailable_result()

    _emit(result)
    if args.require_ready and not result.ready:
        return _NOT_READY_EXIT
    return 0


def _runtime_read_dsn(environ: Mapping[str, str]) -> str:
    value = environ.get(_RUNTIME_READ_DATABASE_URL_ENV)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_DSN_CHARS
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("runtime read database configuration unavailable")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except Exception:
        raise ValueError("runtime read database configuration unavailable") from None
    if parsed.scheme not in {"postgres", "postgresql"} or not hostname:
        raise ValueError("runtime read database configuration unavailable")
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("runtime read database configuration unavailable") from None
    return value


def _load_public_key_registry(path: Path) -> PublicKeyRegistry:
    raw = _read_regular_file(path, maximum=_MAX_REGISTRY_BYTES)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        return PublicKeyRegistry.model_validate(value, strict=True)
    except Exception:
        raise ValueError("public key registry unavailable") from None


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    candidate = Path(path)
    try:
        before = candidate.lstat()
        _validate_registry_file_stat(before, maximum=maximum)
        descriptor = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(descriptor)
            _validate_registry_file_stat(opened, maximum=maximum)
            if _registry_file_identity(opened) != _registry_file_identity(before):
                raise ValueError("file changed during open")
            raw = os.read(descriptor, maximum + 1)
            trailing = os.read(descriptor, 1)
            after = os.fstat(descriptor)
            _validate_registry_file_stat(after, maximum=maximum)
            if _registry_file_identity(after) != _registry_file_identity(opened):
                raise ValueError("file changed during read")
        finally:
            os.close(descriptor)
        if trailing or len(raw) != opened.st_size or len(raw) > maximum:
            raise ValueError("file changed or exceeds limit")
        return raw
    except Exception:
        raise ValueError("public key registry unavailable") from None


def _validate_registry_file_stat(info: os.stat_result, *, maximum: int) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not 0 < info.st_size <= maximum
    ):
        raise ValueError("unsafe public key registry file")


def _registry_file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON constant")


def _verification_policy_invalid_result() -> C7ShadowReadinessResult:
    return C7ShadowReadinessResult(
        ready=False,
        reason=C7ShadowReadinessReason.VERIFICATION_POLICY_INVALID,
    )


def _emit(result: C7ShadowReadinessResult) -> None:
    public = result.to_dict()
    if set(public) != {"schema_version", "ready", "reason"}:
        public = storage_unavailable_result().to_dict()
    sys.stdout.write(
        json.dumps(
            public,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
