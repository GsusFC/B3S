#!/usr/bin/env python3
"""Fail-closed PostgreSQL target assertions for the isolated PR71 Vault."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import os
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

PR71_VAULT_HOST = "ep-broad-river-as71uv9y.c-4.eu-central-1.aws.neon.tech"
PR71_VAULT_DATABASE = "neondb"
PR71_VAULT_PROJECT_ID = "jolly-river-32467750"
PR71_VAULT_BRANCH_ID = "br-divine-star-aspobuer"
PR71_VAULT_RUNTIME_ROLE = "b3s_pr71_app_runtime"
PR71_VAULT_MIGRATION_ROLE = "neondb_owner"

_LIBPQ_ENV_NAMES = (
    "PGOPTIONS",
    "PGSERVICE",
    "PGHOST",
    "PGPORT",
    "PGHOSTADDR",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSSLMODE",
    "PGCHANNELBINDING",
    "PGSERVICEFILE",
    "PGSYSCONFDIR",
    "PGREQUIRESSL",
    "PGTARGETSESSIONATTRS",
    "PGAPPNAME",
    "PGPASSFILE",
    "PGCONNECT_TIMEOUT",
    "PGCLIENTENCODING",
    "PGSSLCERT",
    "PGSSLKEY",
    "PGSSLROOTCERT",
    "PGSSLCRL",
    "PGSSLCRLDIR",
    "PGSSLCERTMODE",
    "PGSSLNEGOTIATION",
    "PGGSSENCMODE",
    "PGKRBSRVNAME",
    "PGGSSLIB",
    "PGREQUIREAUTH",
    "PGREALM",
)
_DSN_AUTHORITY_OVERRIDES = frozenset(
    {
        "host",
        "hostaddr",
        "service",
        "servicefile",
        "user",
        "password",
        "dbname",
        "port",
        "options",
    }
)


class PR71VaultTargetError(RuntimeError):
    """The connection is not the exact authorized PR71 Vault target."""


@dataclass(frozen=True)
class PR71VaultTarget:
    user: str
    host: str = PR71_VAULT_HOST
    database: str = PR71_VAULT_DATABASE
    project_id: str = PR71_VAULT_PROJECT_ID
    branch_id: str = PR71_VAULT_BRANCH_ID


@contextmanager
def without_libpq_environment() -> Iterator[None]:
    """Prevent ambient libpq variables from overriding an attested URL."""

    saved = {
        name: os.environ.pop(name)
        for name in _LIBPQ_ENV_NAMES
        if name in os.environ
    }
    try:
        yield
    finally:
        os.environ.update(saved)


def pr71_vault_migration_target() -> PR71VaultTarget:
    """Return the immutable isolated migration target profile."""

    return PR71VaultTarget(user=PR71_VAULT_MIGRATION_ROLE)


def pr71_vault_runtime_target() -> PR71VaultTarget:
    """Return the immutable isolated application-runtime target profile."""

    return PR71VaultTarget(user=PR71_VAULT_RUNTIME_ROLE)


def validate_pr71_vault_dsn(dsn: str, *, target: PR71VaultTarget) -> None:
    """Validate the URL authority before any database connection is attempted."""

    parsed = urlsplit(str(dsn or "").strip())
    try:
        port = parsed.port
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        raise PR71VaultTargetError("invalid PostgreSQL URL") from None
    del port

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = unquote(parsed.path.removeprefix("/"))
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or parsed.hostname.lower() != target.host
        or username != target.user
        or not password
        or database != target.database
        or parsed.fragment
    ):
        raise PR71VaultTargetError("PostgreSQL URL target does not match PR71 Vault")

    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise PR71VaultTargetError("duplicate PostgreSQL URL parameter")
    if _DSN_AUTHORITY_OVERRIDES.intersection(keys):
        raise PR71VaultTargetError("PostgreSQL URL authority override is forbidden")
    ssl_modes = [value for key, value in pairs if key == "sslmode"]
    channel_bindings = [value for key, value in pairs if key == "channel_binding"]
    authenticated_tls = ssl_modes == ["verify-full"] or (
        ssl_modes == ["require"] and channel_bindings == ["require"]
    )
    if not authenticated_tls:
        raise PR71VaultTargetError(
            "PR71 Vault requires authenticated PostgreSQL TLS"
        )


def require_pr71_vault_connection(
    connection: Any,
    *,
    target: PR71VaultTarget,
) -> None:
    """Assert the live session identity using SELECT only on the same connection."""

    info = getattr(connection, "info", None)
    connected_host = str(getattr(info, "host", "") or "").strip().lower()
    if connected_host != target.host:
        raise PR71VaultTargetError("connected host does not match PR71 Vault")

    pgconn = getattr(connection, "pgconn", None)
    if pgconn is None or getattr(pgconn, "ssl_in_use", None) is not True:
        raise PR71VaultTargetError("PostgreSQL connection is not using TLS")

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
        raise PR71VaultTargetError("connected session does not match PR71 Vault")
