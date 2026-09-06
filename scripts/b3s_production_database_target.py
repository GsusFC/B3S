#!/usr/bin/env python3
"""Fail-closed PostgreSQL target assertions for the B3S production migrator."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


_TARGET_ENVIRONMENT_NAMES = (
    "B3S_PRODUCTION_DATABASE",
    "B3S_PRODUCTION_HOST",
    "B3S_PRODUCTION_NEON_PROJECT_ID",
    "B3S_PRODUCTION_NEON_BRANCH_ID",
    "B3S_PRODUCTION_MIGRATION_ROLE",
)
_LIBPQ_ENV_NAMES = (
    "PGAPPNAME",
    "PGAPPLICATIONNAME",
    "PGCHANNELBINDING",
    "PGCLIENTENCODING",
    "PGCONNECT_TIMEOUT",
    "PGDATABASE",
    "PGGSSENCMODE",
    "PGGSSLIB",
    "PGHOST",
    "PGHOSTADDR",
    "PGKRBSRVNAME",
    "PGLOADBALANCEHOSTS",
    "PGOPTIONS",
    "PGPASSFILE",
    "PGPASSWORD",
    "PGPORT",
    "PGREALM",
    "PGREQUIREAUTH",
    "PGREQUIRESSL",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGSSLCERT",
    "PGSSLCERTMODE",
    "PGSSLCRL",
    "PGSSLCRLDIR",
    "PGSSLGSSENCMODE",
    "PGSSLKEY",
    "PGSSLKEYLOGFILE",
    "PGSSLMAXPROTOCOLVERSION",
    "PGSSLMINPROTOCOLVERSION",
    "PGSSLMODE",
    "PGSSLNEGOTIATION",
    "PGSSLROOTCERT",
    "PGSSLSNI",
    "PGSYSCONFDIR",
    "PGTARGETSESSIONATTRS",
    "PGUSER",
)
_REQUIRED_TLS_PARAMETERS = {
    "sslmode": "verify-full",
    "channel_binding": "require",
}
_TRUST_ROOT_PARAMETER = "sslrootcert"


class B3SProductionTargetError(RuntimeError):
    """The connection is not the exact authorized B3S production target."""


@dataclass(frozen=True)
class B3SProductionTarget:
    user: str
    host: str
    database: str
    project_id: str
    branch_id: str


def _required_environment_value(name: str, environ: Mapping[str, str]) -> str:
    value = str(environ.get(name, "")).strip()
    if not value or len(value) > 255 or any(character in value for character in "\r\n\x00"):
        raise B3SProductionTargetError("production target contract is incomplete")
    return value


@contextmanager
def without_libpq_environment() -> Iterator[None]:
    """Prevent ambient libpq settings from influencing an attested connection."""

    saved = {
        name: os.environ.pop(name)
        for name in _LIBPQ_ENV_NAMES
        if name in os.environ
    }
    try:
        yield
    finally:
        os.environ.update(saved)


def b3s_production_migration_target(
    *,
    environ: Mapping[str, str] | None = None,
) -> B3SProductionTarget:
    """Load the production target only from the protected workflow environment."""

    environment = os.environ if environ is None else environ
    database, host, project_id, branch_id, user = (
        _required_environment_value(name, environment) for name in _TARGET_ENVIRONMENT_NAMES
    )
    return B3SProductionTarget(
        user=user,
        host=host.lower(),
        database=database,
        project_id=project_id,
        branch_id=branch_id,
    )


def _validate_trust_root_path(value: str) -> str:
    path = str(value or "").strip()
    if (
        not path
        or len(path) > 4096
        or any(character in path for character in "\r\n\x00")
        or not Path(path).is_absolute()
    ):
        raise B3SProductionTargetError("production PostgreSQL URL TLS contract is invalid")
    return path


def attach_b3s_production_trust_root(dsn: str, *, trust_root_path: str) -> str:
    """Attach the controller-created root path directly to a PostgreSQL URL."""

    root_path = _validate_trust_root_path(trust_root_path)
    parsed = urlsplit(str(dsn or "").strip())
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        raise B3SProductionTargetError("invalid PostgreSQL URL") from None
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)) or _TRUST_ROOT_PARAMETER in keys:
        raise B3SProductionTargetError("production PostgreSQL URL TLS contract is invalid")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode([*pairs, (_TRUST_ROOT_PARAMETER, root_path)]),
            parsed.fragment,
        )
    )


def validate_b3s_production_dsn(dsn: str, *, target: B3SProductionTarget) -> None:
    """Validate the exact production URL shape before any connection is opened."""

    parsed = urlsplit(str(dsn or "").strip())
    try:
        parsed.port
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        raise B3SProductionTargetError("invalid PostgreSQL URL") from None

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = unquote(parsed.path.removeprefix("/"))
    host = str(parsed.hostname or "").strip().lower()
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or host != target.host
        or username != target.user
        or not password
        or database != target.database
        or parsed.fragment
    ):
        raise B3SProductionTargetError("PostgreSQL URL target does not match production")

    keys = [key for key, _value in pairs]
    parameters = dict(pairs)
    if (
        len(keys) != len(set(keys))
        or set(parameters) != {*_REQUIRED_TLS_PARAMETERS, _TRUST_ROOT_PARAMETER}
        or any(parameters.get(key) != value for key, value in _REQUIRED_TLS_PARAMETERS.items())
    ):
        raise B3SProductionTargetError("production PostgreSQL URL TLS contract is invalid")
    _validate_trust_root_path(parameters[_TRUST_ROOT_PARAMETER])


def require_b3s_production_connection(
    connection: Any,
    *,
    target: B3SProductionTarget,
) -> None:
    """Attest the exact DDL or verification session with SELECT-only checks."""

    info = getattr(connection, "info", None)
    connected_host = str(getattr(info, "host", "") or "").strip().lower()
    if connected_host != target.host:
        raise B3SProductionTargetError("connected host does not match production")

    pgconn = getattr(connection, "pgconn", None)
    if pgconn is None or getattr(pgconn, "ssl_in_use", None) is not True:
        raise B3SProductionTargetError("PostgreSQL connection is not using TLS")

    row = connection.execute(
        """
        SELECT current_database() AS database_name,
               current_user AS user_name,
               current_setting('neon.project_id', true) AS project_id,
               current_setting('neon.branch_id', true) AS branch_id
        """
    ).fetchone()
    if isinstance(row, Mapping):
        actual = (
            row.get("database_name"),
            row.get("user_name"),
            row.get("project_id"),
            row.get("branch_id"),
        )
    else:
        actual = tuple(row or ())
    expected = (
        target.database,
        target.user,
        target.project_id,
        target.branch_id,
    )
    if actual != expected:
        raise B3SProductionTargetError("connected session does not match production")
