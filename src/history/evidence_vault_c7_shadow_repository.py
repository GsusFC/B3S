"""Execute-only PostgreSQL adapter for C7 verified-raw shadow readiness.

The adapter owns no write SQL and exposes no raw read methods.  It collects the
entire witness in one repeatable-read/read-only transaction, hands it to the pure
evaluator, and returns only the evaluator's three-field result.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Callable, Mapping
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from src.services.evidence_vault_c7_cutover import (
    EvidenceVaultC7CutoverError,
    canonicalize_c7_brand,
)
from src.services.evidence_vault_c7_shadow_readiness import (
    C7ShadowReadinessResult,
    EvidenceVaultC7ShadowReadinessEvaluator,
    storage_unavailable_result,
)
from src.services.evidence_vault_raw_provenance import PublicKeyRegistry


_CONNECT_TIMEOUT_SECONDS = 5
_BEGIN_SQL = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '5000ms'"
_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '1000ms'"
_IDLE_TIMEOUT_SQL = "SET LOCAL idle_in_transaction_session_timeout = '5000ms'"
_CONTEXT_SQL = (
    "SELECT b3s_history.read_evidence_vault_c7_shadow_context(%s, %s) "
    "AS context"
)
_PROVENANCE_SQL = (
    "SELECT b3s_history.read_evidence_vault_c7_shadow_provenance(%s::uuid) "
    "AS provenance"
)
_ACQUISITION_SQL = (
    "SELECT b3s_history.read_evidence_vault_c7_shadow_acquisition(%s, %s) "
    "AS acquisition"
)
_CLOCK_SQL = "SELECT clock_timestamp() AS database_time"
_WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

_ROLE_PREFLIGHT_SQL = """
WITH role_row AS (
    SELECT * FROM pg_catalog.pg_roles WHERE rolname = current_user
), runtime_read_role AS (
    SELECT *
    FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_runtime_read'
), memberships AS (
    SELECT granted.rolname, member_rows.admin_option
    FROM pg_catalog.pg_auth_members AS member_rows
    JOIN pg_catalog.pg_roles AS granted ON granted.oid = member_rows.roleid
    JOIN role_row ON role_row.oid = member_rows.member
), login_owned_object AS (
    SELECT 1
    FROM pg_catalog.pg_shdepend AS dependencies
    JOIN role_row ON role_row.oid = dependencies.refobjid
    WHERE dependencies.refclassid =
            'pg_catalog.pg_authid'::pg_catalog.regclass
      AND dependencies.deptype = 'o'
), runtime_group_owned_object AS (
    SELECT 1
    FROM pg_catalog.pg_shdepend AS dependencies
    JOIN runtime_read_role
      ON runtime_read_role.oid = dependencies.refobjid
    WHERE dependencies.refclassid =
            'pg_catalog.pg_authid'::pg_catalog.regclass
      AND dependencies.deptype = 'o'
), forbidden_table_privilege AS (
    SELECT 1
    FROM pg_catalog.pg_class AS relations
    JOIN pg_catalog.pg_namespace AS schemas ON schemas.oid = relations.relnamespace
    WHERE schemas.nspname = 'b3s_history'
      AND relations.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND (
          pg_catalog.has_table_privilege(current_user, relations.oid, 'SELECT')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'INSERT')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'UPDATE')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'DELETE')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'TRUNCATE')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'REFERENCES')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'TRIGGER')
      )
), forbidden_column_privilege AS (
    SELECT 1
    FROM pg_catalog.pg_attribute AS attributes
    JOIN pg_catalog.pg_class AS relations ON relations.oid = attributes.attrelid
    JOIN pg_catalog.pg_namespace AS schemas ON schemas.oid = relations.relnamespace
    WHERE schemas.nspname = 'b3s_history'
      AND relations.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND attributes.attnum > 0
      AND NOT attributes.attisdropped
      AND (
          pg_catalog.has_column_privilege(
              current_user, relations.oid, attributes.attnum, 'SELECT'
          ) OR pg_catalog.has_column_privilege(
              current_user, relations.oid, attributes.attnum, 'INSERT'
          ) OR pg_catalog.has_column_privilege(
              current_user, relations.oid, attributes.attnum, 'UPDATE'
          ) OR pg_catalog.has_column_privilege(
              current_user, relations.oid, attributes.attnum, 'REFERENCES'
          )
      )
), forbidden_sequence_privilege AS (
    SELECT 1
    FROM pg_catalog.pg_class AS sequences
    JOIN pg_catalog.pg_namespace AS schemas ON schemas.oid = sequences.relnamespace
    WHERE schemas.nspname = 'b3s_history'
      AND sequences.relkind = 'S'
      AND (
          pg_catalog.has_sequence_privilege(current_user, sequences.oid, 'USAGE')
          OR pg_catalog.has_sequence_privilege(current_user, sequences.oid, 'SELECT')
          OR pg_catalog.has_sequence_privilege(current_user, sequences.oid, 'UPDATE')
      )
), unexpected_security_definer_execute AS (
    SELECT 1
    FROM pg_catalog.pg_proc AS functions
    WHERE functions.prosecdef
      AND pg_catalog.has_function_privilege(current_user, functions.oid, 'EXECUTE')
      AND functions.oid <> ALL(ARRAY[
          'b3s_history.read_evidence_vault_c7_shadow_context(text,text)'::regprocedure::oid,
          'b3s_history.read_evidence_vault_c7_shadow_provenance(uuid)'::regprocedure::oid,
          'b3s_history.read_evidence_vault_c7_shadow_acquisition(text,text)'::regprocedure::oid
      ])
)
SELECT
    session_user = current_user AS direct_session,
    role_row.rolcanlogin AND role_row.rolinherit AND NOT role_row.rolsuper
        AND NOT role_row.rolcreaterole AND NOT role_row.rolcreatedb
        AND NOT role_row.rolreplication AND NOT role_row.rolbypassrls
        AS restricted_login,
    NOT EXISTS (SELECT 1 FROM login_owned_object) AS login_owns_nothing,
    current_setting('transaction_isolation') = 'repeatable read'
        AND current_setting('transaction_read_only') = 'on'
        AS transaction_mode,
    pg_catalog.has_schema_privilege(current_user, 'b3s_history', 'USAGE')
        AND NOT pg_catalog.has_schema_privilege(current_user, 'b3s_history', 'CREATE')
        AS schema_scope,
    pg_catalog.pg_has_role(
        current_user, 'b3s_history_vault_runtime_read', 'MEMBER'
    ) AND NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE rolname <> 'b3s_history_vault_runtime_read' OR admin_option
    ) AS runtime_membership_only,
    EXISTS (
        SELECT 1
        FROM runtime_read_role
        WHERE NOT rolcanlogin AND NOT rolinherit AND NOT rolsuper
          AND NOT rolcreaterole AND NOT rolcreatedb
          AND NOT rolreplication AND NOT rolbypassrls
    ) AS runtime_group_restricted,
    NOT EXISTS (SELECT 1 FROM runtime_group_owned_object)
        AS runtime_group_owns_nothing,
    NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS group_memberships
        JOIN runtime_read_role
          ON runtime_read_role.oid = group_memberships.member
    ) AS runtime_group_isolated,
    pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.read_evidence_vault_c7_shadow_context(text,text)'::regprocedure,
        'EXECUTE'
    ) AND pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.read_evidence_vault_c7_shadow_provenance(uuid)'::regprocedure,
        'EXECUTE'
    ) AND pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.read_evidence_vault_c7_shadow_acquisition(text,text)'::regprocedure,
        'EXECUTE'
    ) AS required_execute,
    NOT pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.append_evidence_vault_raw_acquisition(jsonb)'::regprocedure,
        'EXECUTE'
    ) AND NOT pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.bind_evidence_vault_verified_c7_lineage(jsonb)'::regprocedure,
        'EXECUTE'
    ) AND NOT pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.append_evidence_vault_raw_provenance_disposition(jsonb)'::regprocedure,
        'EXECUTE'
    ) AND NOT pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.read_evidence_vault_raw_provenance(uuid)'::regprocedure,
        'EXECUTE'
    ) AND NOT pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.read_evidence_vault_raw_acquisition(text,text)'::regprocedure,
        'EXECUTE'
    ) AS forbidden_execute_absent,
    NOT EXISTS (SELECT 1 FROM forbidden_table_privilege)
        AND NOT EXISTS (SELECT 1 FROM forbidden_column_privilege)
        AND NOT EXISTS (SELECT 1 FROM forbidden_sequence_privilege)
        AS relation_privileges_absent,
    NOT EXISTS (SELECT 1 FROM unexpected_security_definer_execute)
        AS unexpected_definer_execute_absent,
    NOT pg_catalog.pg_has_role(
        current_user, 'b3s_history_vault_provenance_owner', 'MEMBER'
    ) AS owner_membership_absent
FROM role_row
"""

_ROLE_FIELDS = {
    "direct_session",
    "restricted_login",
    "login_owns_nothing",
    "transaction_mode",
    "schema_scope",
    "runtime_membership_only",
    "runtime_group_restricted",
    "runtime_group_owns_nothing",
    "runtime_group_isolated",
    "required_execute",
    "forbidden_execute_absent",
    "relation_privileges_absent",
    "unexpected_definer_execute_absent",
    "owner_membership_absent",
}


class EvidenceVaultC7ShadowRepository:
    """A single sanitized readiness capability with no write adapter surface."""

    __slots__ = ("__connect_fn", "__dsn", "__evaluator")

    def __init__(
        self,
        dsn: str,
        *,
        public_key_registry: PublicKeyRegistry | Mapping[str, Any],
        expected_public_key_registry_fingerprint: str,
        connect: Callable[..., Any] = psycopg.connect,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip() or not callable(connect):
            raise ValueError("invalid C7 shadow repository configuration")
        self.__dsn = dsn
        self.__connect_fn = connect
        self.__evaluator = EvidenceVaultC7ShadowReadinessEvaluator(
            public_key_registry=public_key_registry,
            expected_public_key_registry_fingerprint=(
                expected_public_key_registry_fingerprint
            ),
        )

    def get_evidence_vault_c7_shadow_readiness(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> C7ShadowReadinessResult:
        """Collect and verify one snapshot, returning no part of its witness."""

        try:
            brand = canonicalize_c7_brand(domain_or_url)
        except EvidenceVaultC7CutoverError:
            return self.__evaluator.evaluate(str(domain_or_url or ""), {})
        if not isinstance(workspace_slug, str) or not _WORKSPACE_RE.fullmatch(workspace_slug):
            return storage_unavailable_result()
        if not self.__evaluator.policy_is_valid:
            return self.__evaluator.evaluate(brand, {})
        try:
            with self.__connect() as connection:
                connection.execute(_BEGIN_SQL)
                connection.execute(_STATEMENT_TIMEOUT_SQL)
                connection.execute(_LOCK_TIMEOUT_SQL)
                connection.execute(_IDLE_TIMEOUT_SQL)
                _assert_runtime_read_session(connection)
                context_row = connection.execute(
                    _CONTEXT_SQL,
                    (workspace_slug, brand),
                ).fetchone()
                context = _one_field(context_row, "context")
                if not isinstance(context, Mapping):
                    return self.__evaluator.evaluate(
                        brand,
                        {"context": context, "proofs": []},
                    )
                binding_rows = context.get("lineage_bindings")
                if not isinstance(binding_rows, list):
                    return self.__evaluator.evaluate(
                        brand,
                        {"context": context, "proofs": []},
                    )
                proofs: list[dict[str, Any]] = []
                for binding_row in binding_rows:
                    if not isinstance(binding_row, Mapping):
                        raise ValueError("shadow binding selector is malformed")
                    binding_id = _uuid_text(binding_row.get("id"))
                    provenance_row = connection.execute(
                        _PROVENANCE_SQL,
                        (binding_id,),
                    ).fetchone()
                    provenance = _one_field(provenance_row, "provenance")
                    source_scan_id = _one_source_scan_id(provenance)
                    acquisition_row = connection.execute(
                        _ACQUISITION_SQL,
                        (workspace_slug, source_scan_id),
                    ).fetchone()
                    acquisition = _one_field(acquisition_row, "acquisition")
                    proofs.append(
                        {
                            "binding_id": binding_id,
                            "provenance": deepcopy(provenance),
                            "acquisition": deepcopy(acquisition),
                        }
                    )
                clock_row = connection.execute(_CLOCK_SQL).fetchone()
                database_time = _one_field(clock_row, "database_time")
                final_context = deepcopy(dict(context))
                final_context["database_time"] = database_time
                private_snapshot = {"context": final_context, "proofs": proofs}
            return self.__evaluator.evaluate(brand, private_snapshot)
        except Exception:
            return storage_unavailable_result()

    def __connect(self) -> Any:
        return self.__connect_fn(
            self.__dsn,
            row_factory=dict_row,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        )


PostgresEvidenceVaultC7ShadowRepository = EvidenceVaultC7ShadowRepository


def _assert_runtime_read_session(connection: Any) -> None:
    row = connection.execute(_ROLE_PREFLIGHT_SQL).fetchone()
    if not isinstance(row, Mapping) or set(row) != _ROLE_FIELDS or any(
        row[field] is not True for field in _ROLE_FIELDS
    ):
        raise ValueError("C7 shadow role is not execute-only")


def _one_field(row: Any, field: str) -> Any:
    if not isinstance(row, Mapping) or set(row) != {field}:
        raise ValueError("C7 shadow read returned an invalid row")
    return row[field]


def _one_source_scan_id(provenance: Any) -> str:
    if not isinstance(provenance, Mapping):
        raise ValueError("C7 shadow provenance is unavailable")
    receipts = provenance.get("receipts")
    overflow = provenance.get("overflow")
    if (
        provenance.get("receipt_count") != 2
        or not isinstance(receipts, list)
        or len(receipts) != 2
        or not isinstance(overflow, Mapping)
        or overflow.get("receipts") is not False
    ):
        raise ValueError("C7 shadow receipt selectors are invalid")
    values = {
        str(row.get("source_scan_id") or "")
        for row in receipts
        if isinstance(row, Mapping)
    }
    if len(values) != 1:
        raise ValueError("C7 shadow receipt selectors are mixed")
    value = values.pop()
    if not value or len(value) > 300 or value != value.strip() or "\x00" in value:
        raise ValueError("C7 shadow source scan selector is invalid")
    return value


def _uuid_text(value: Any) -> str:
    parsed = UUID(str(value))
    if parsed.int == 0 or str(parsed) != str(value):
        raise ValueError("C7 shadow binding selector is invalid")
    return str(parsed)


__all__ = [
    "EvidenceVaultC7ShadowRepository",
    "PostgresEvidenceVaultC7ShadowRepository",
]
