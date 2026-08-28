#!/usr/bin/env python3
"""Fail-closed PostgreSQL target assertions for the operational B3S Vault."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

B3S_VAULT_HOST = "ep-fancy-recipe-as612c8j-pooler.c-4.eu-central-1.aws.neon.tech"
B3S_VAULT_DATABASE = "neondb"
B3S_VAULT_PROJECT_ID = "jolly-river-32467750"
B3S_VAULT_BRANCH_ID = "br-misty-sky-asfp14gb"
B3S_VAULT_MIGRATION_ROLE = "neondb_owner"
B3S_VAULT_SERVER_PORT = 5432

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


class B3SVaultTargetError(RuntimeError):
    """The connection is not the exact authorized B3S Vault target."""


@dataclass(frozen=True)
class B3SVaultTarget:
    user: str
    host: str = B3S_VAULT_HOST
    database: str = B3S_VAULT_DATABASE
    project_id: str = B3S_VAULT_PROJECT_ID
    branch_id: str = B3S_VAULT_BRANCH_ID
    server_port: int = B3S_VAULT_SERVER_PORT


def b3s_vault_migration_target() -> B3SVaultTarget:
    """Return the immutable B3S Vault migration target profile."""

    return B3SVaultTarget(user=B3S_VAULT_MIGRATION_ROLE)


def validate_b3s_vault_dsn(dsn: str, *, target: B3SVaultTarget) -> None:
    """Validate URL authority and the approved TLS shape before connecting."""

    parsed = urlsplit(str(dsn or "").strip())
    try:
        port = parsed.port
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        raise B3SVaultTargetError("invalid PostgreSQL URL") from None

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = unquote(parsed.path.removeprefix("/"))
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or parsed.hostname.lower() != target.host
        or port not in {None, target.server_port}
        or username != target.user
        or not password
        or database != target.database
        or parsed.fragment
    ):
        raise B3SVaultTargetError("PostgreSQL URL target does not match B3S Vault")

    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise B3SVaultTargetError("duplicate PostgreSQL URL parameter")
    if _DSN_AUTHORITY_OVERRIDES.intersection(keys):
        raise B3SVaultTargetError("PostgreSQL URL authority override is forbidden")
    if [value for key, value in pairs if key == "sslmode"] != ["require"] or [
        value for key, value in pairs if key == "channel_binding"
    ] != ["require"]:
        raise B3SVaultTargetError("B3S Vault requires approved PostgreSQL TLS")


def require_b3s_vault_connection(
    connection: Any,
    *,
    target: B3SVaultTarget,
) -> None:
    """Attest the DDL session target with SELECT-only assertions."""

    info = getattr(connection, "info", None)
    connected_host = str(getattr(info, "host", "") or "").strip().lower()
    if connected_host != target.host:
        raise B3SVaultTargetError("connected host does not match B3S Vault")

    pgconn = getattr(connection, "pgconn", None)
    if pgconn is None or getattr(pgconn, "ssl_in_use", None) is not True:
        raise B3SVaultTargetError("PostgreSQL connection is not using TLS")

    row = connection.execute(
        """
        SELECT current_database() AS database_name,
               current_user AS user_name,
               current_setting('neon.project_id', true) AS project_id,
               current_setting('neon.branch_id', true) AS branch_id,
               inet_server_port() AS server_port
        """
    ).fetchone()
    if isinstance(row, Mapping):
        actual = (
            row.get("database_name"),
            row.get("user_name"),
            row.get("project_id"),
            row.get("branch_id"),
            row.get("server_port"),
        )
    else:
        actual = tuple(row or ())
    expected = (
        target.database,
        target.user,
        target.project_id,
        target.branch_id,
        target.server_port,
    )
    if actual != expected:
        raise B3SVaultTargetError("connected session does not match B3S Vault")
