from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from pydantic import ValidationError

from src.history.evidence_vault_c7_shadow_repository import (
    EvidenceVaultC7ShadowRepository,
)
from src.services.evidence_vault_c7_shadow_readiness import (
    C7_SHADOW_READINESS_VERSION,
    C7ShadowReadinessReason,
    C7ShadowReadinessResult,
    EvidenceVaultC7ShadowReadinessEvaluator,
)
from src.services.evidence_vault_raw_provenance import (
    PUBLIC_KEY_REGISTRY_VERSION,
    PublicKeyRecord,
    PublicKeyRegistry,
    public_key_registry_fingerprint,
)


def _registry() -> PublicKeyRegistry:
    key = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return PublicKeyRegistry(
        schema_version=PUBLIC_KEY_REGISTRY_VERSION,
        current_key_id="scanner-key-v1",
        keys={
            "scanner-key-v1": PublicKeyRecord(
                version=1,
                status="current",
                public_key_base64=base64.b64encode(key).decode("ascii"),
                signing_not_before="2025-01-01T00:00:00Z",
            )
        },
    )


def _evaluator(*, expected: str | None = None) -> EvidenceVaultC7ShadowReadinessEvaluator:
    registry = _registry()
    return EvidenceVaultC7ShadowReadinessEvaluator(
        public_key_registry=registry,
        expected_public_key_registry_fingerprint=(
            public_key_registry_fingerprint(registry)
            if expected is None
            else expected
        ),
    )


def test_result_is_strict_reason_only_and_policy_is_separately_pinned() -> None:
    result = _evaluator(expected="0" * 64).evaluate(
        "example.com",
        {"untrusted": "raw witness must never escape"},
    )

    assert result.to_dict() == {
        "schema_version": C7_SHADOW_READINESS_VERSION,
        "ready": False,
        "reason": "verification_policy_invalid",
    }
    assert set(result.to_dict()) == {"schema_version", "ready", "reason"}
    assert "untrusted" not in repr(result)


def test_public_result_cannot_represent_a_contradictory_state() -> None:
    with pytest.raises(ValidationError, match="ready must match"):
        C7ShadowReadinessResult(
            ready=True,
            reason=C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
        )
    with pytest.raises(ValidationError, match="ready must match"):
        C7ShadowReadinessResult(
            ready=False,
            reason=C7ShadowReadinessReason.VERIFIED_RAW_READY,
        )


def test_brand_and_context_fail_closed_before_any_raw_proof() -> None:
    evaluator = _evaluator()

    invalid_brand = evaluator.evaluate("https://user@example.com", {})
    unavailable = evaluator.evaluate("example.com", {})

    assert invalid_brand.reason is C7ShadowReadinessReason.BRAND_IDENTITY_INVALID
    assert unavailable.reason is C7ShadowReadinessReason.BRAND_CONTEXT_UNAVAILABLE
    assert invalid_brand.ready is unavailable.ready is False


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _Connection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.exited = False

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.exited = True
        return None

    def execute(self, sql: str, params: Any = None) -> _Result:
        self.statements.append((sql, params))
        if sql.startswith("BEGIN ISOLATION") or sql.startswith("SET LOCAL"):
            return _Result(None)
        if "role_row AS" in sql:
            return _Result(
                {
                    "direct_session": True,
                    "restricted_login": True,
                    "login_owns_nothing": True,
                    "transaction_mode": True,
                    "schema_scope": True,
                    "runtime_membership_only": True,
                    "runtime_group_restricted": True,
                    "runtime_group_owns_nothing": True,
                    "runtime_group_isolated": True,
                    "required_execute": True,
                    "forbidden_execute_absent": True,
                    "relation_privileges_absent": True,
                    "unexpected_definer_execute_absent": True,
                    "owner_membership_absent": True,
                }
            )
        if sql.startswith("SELECT b3s_history.read_evidence_vault_c7_shadow_context"):
            return _Result({"context": {"lineage_bindings": []}})
        if sql == "SELECT clock_timestamp() AS database_time":
            return _Result(
                {"database_time": datetime(2026, 8, 9, 5, 1, tzinfo=timezone.utc)}
            )
        raise AssertionError(f"unexpected SQL: {sql}")


def test_repository_begins_repeatable_read_read_only_before_every_read() -> None:
    registry = _registry()
    connection = _Connection()
    repository = EvidenceVaultC7ShadowRepository(
        "postgresql://runtime-read.invalid/b3s",
        public_key_registry=registry,
        expected_public_key_registry_fingerprint=public_key_registry_fingerprint(registry),
        connect=lambda *_args, **_kwargs: connection,
    )

    result = repository.get_evidence_vault_c7_shadow_readiness("example.com")

    assert result.reason is C7ShadowReadinessReason.BRAND_CONTEXT_UNAVAILABLE
    assert connection.statements[0][0] == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert connection.statements[1][0] == "SET LOCAL statement_timeout = '5000ms'"
    assert connection.statements[2][0] == "SET LOCAL lock_timeout = '1000ms'"
    assert connection.statements[3][0] == "SET LOCAL idle_in_transaction_session_timeout = '5000ms'"
    assert "role_row AS" in connection.statements[4][0]
    assert "read_evidence_vault_c7_shadow_context" in connection.statements[5][0]
    assert all(
        token not in sql.upper()
        for sql, _params in connection.statements
        for token in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ")
    )


class _TimeoutConnection(_Connection):
    def execute(self, sql: str, params: Any = None) -> _Result:
        if sql.startswith("SELECT b3s_history.read_evidence_vault_c7_shadow_context"):
            self.statements.append((sql, params))
            raise TimeoutError("bounded shadow query timed out")
        return super().execute(sql, params)


def test_repository_timeout_is_sanitized_as_storage_unavailable() -> None:
    registry = _registry()
    connection = _TimeoutConnection()
    repository = EvidenceVaultC7ShadowRepository(
        "postgresql://runtime-read.invalid/b3s",
        public_key_registry=registry,
        expected_public_key_registry_fingerprint=public_key_registry_fingerprint(registry),
        connect=lambda *_args, **_kwargs: connection,
    )

    result = repository.get_evidence_vault_c7_shadow_readiness("example.com")

    assert result.reason is C7ShadowReadinessReason.STORAGE_UNAVAILABLE
    assert any(statement.startswith("SET LOCAL statement_timeout") for statement, _ in connection.statements)


class _ProofConnection(_Connection):
    _BINDING_ID = "11111111-1111-4111-8111-111111111111"

    def execute(self, sql: str, params: Any = None) -> _Result:
        if sql.startswith("SELECT b3s_history.read_evidence_vault_c7_shadow_context"):
            self.statements.append((sql, params))
            return _Result(
                {
                    "context": {
                        "database_time": "2026-08-09T05:00:00+00:00",
                        "lineage_bindings": [{"id": self._BINDING_ID}],
                    }
                }
            )
        if sql.startswith("SELECT b3s_history.read_evidence_vault_c7_shadow_provenance"):
            self.statements.append((sql, params))
            return _Result(
                {
                    "provenance": {
                        "receipt_count": 2,
                        "receipts": [
                            {"source_scan_id": "scan-shadow-proof"},
                            {"source_scan_id": "scan-shadow-proof"},
                        ],
                        "overflow": {"receipts": False},
                    }
                }
            )
        if sql.startswith("SELECT b3s_history.read_evidence_vault_c7_shadow_acquisition"):
            self.statements.append((sql, params))
            return _Result({"acquisition": {}})
        if sql == "SELECT clock_timestamp() AS database_time":
            self.statements.append((sql, params))
            return _Result(
                {"database_time": datetime(2026, 8, 9, 5, 1, tzinfo=timezone.utc)}
            )
        return super().execute(sql, params)


class _EvaluatorSpy:
    policy_is_valid = True

    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.snapshot: dict[str, Any] | None = None

    def evaluate(
        self,
        _brand: str,
        snapshot: dict[str, Any],
    ) -> C7ShadowReadinessResult:
        assert self.connection.exited is True
        self.snapshot = snapshot
        return C7ShadowReadinessResult(
            ready=False,
            reason=C7ShadowReadinessReason.BRAND_CONTEXT_UNAVAILABLE,
        )


def test_repository_uses_only_bounded_shadow_reads_and_a_final_database_clock() -> None:
    registry = _registry()
    connection = _ProofConnection()
    repository = EvidenceVaultC7ShadowRepository(
        "postgresql://runtime-read.invalid/b3s",
        public_key_registry=registry,
        expected_public_key_registry_fingerprint=public_key_registry_fingerprint(registry),
        connect=lambda *_args, **_kwargs: connection,
    )
    evaluator = _EvaluatorSpy(connection)
    repository._EvidenceVaultC7ShadowRepository__evaluator = evaluator

    result = repository.get_evidence_vault_c7_shadow_readiness("example.com")

    assert result.reason is C7ShadowReadinessReason.BRAND_CONTEXT_UNAVAILABLE
    assert evaluator.snapshot is not None
    assert evaluator.snapshot["context"]["database_time"] == datetime(
        2026, 8, 9, 5, 1, tzinfo=timezone.utc
    )
    sql = [statement for statement, _params in connection.statements]
    reads = [statement for statement in sql if statement.startswith("SELECT b3s_history")]
    assert any("read_evidence_vault_c7_shadow_provenance" in statement for statement in reads)
    assert any("read_evidence_vault_c7_shadow_acquisition" in statement for statement in reads)
    assert not any("read_evidence_vault_raw_provenance" in statement for statement in reads)
    assert not any("read_evidence_vault_raw_acquisition" in statement for statement in reads)
    assert sql[-1] == "SELECT clock_timestamp() AS database_time"


def test_repository_does_not_connect_when_registry_fingerprint_is_wrong() -> None:
    called = False

    def connect(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("the database must not be read under an unpinned policy")

    repository = EvidenceVaultC7ShadowRepository(
        "postgresql://runtime-read.invalid/b3s",
        public_key_registry=_registry(),
        expected_public_key_registry_fingerprint="f" * 64,
        connect=connect,
    )

    result = repository.get_evidence_vault_c7_shadow_readiness("example.com")

    assert result.reason is C7ShadowReadinessReason.VERIFICATION_POLICY_INVALID
    assert called is False


def test_migration_019_is_immutable_and_020_owns_bounded_shadow_reads() -> None:
    migration_019 = Path(
        "src/history/migrations/019_evidence_vault_verified_raw_provenance.sql"
    ).read_bytes()
    assert hashlib.sha256(migration_019).hexdigest() == (
        "f64b80d7a1b5208ae7fda3c450f49ae84f9e297d0150aa9307912e028b658209"
    )

    sql = Path(
        "src/history/migrations/020_evidence_vault_c7_shadow_readiness.sql"
    ).read_text()
    for function_name in (
        "read_evidence_vault_c7_shadow_context",
        "read_evidence_vault_c7_shadow_provenance",
        "read_evidence_vault_c7_shadow_acquisition",
    ):
        assert f"CREATE FUNCTION b3s_history.{function_name}(" in sql
    assert "SECURITY DEFINER" in sql
    assert "statement_timestamp()" in sql
    assert "transaction_timestamp()" not in sql
    assert "LIMIT 129" in sql and "LIMIT 65" in sql and "LIMIT 17" in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "TO b3s_history_vault_runtime_read" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql


def test_runtime_c7_stubs_remain_fail_closed() -> None:
    source = Path("src/history/repository.py").read_text()
    snapshot_start = source.index("    def get_evidence_vault_c7_runtime_snapshot(")
    attestation_start = source.index(
        "    def get_evidence_vault_runtime_ready_c7_group_attestation(",
        snapshot_start,
    )
    next_method = source.index(
        "    def get_or_create_evidence_vault_operational_score_evaluation(",
        attestation_start,
    )

    assert "return None" in source[snapshot_start:attestation_start]
    assert "return None" in source[attestation_start:next_method]
    assert "shadow" not in source[snapshot_start:next_method].lower()


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1",
    reason="destructive PostgreSQL integration requires B3S_TEST_DATABASE_URL and B3S_ALLOW_SCHEMA_DROP=1",
)
def test_repository_preflight_rejects_direct_privilege_escape() -> None:
    import psycopg
    from psycopg import sql as psycopg_sql
    from psycopg.conninfo import make_conninfo

    from src.history.repository import PostgresHistoryRepository

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    login_role = "b3s_c7_shadow_runtime_login_test"
    rogue_group = "b3s_c7_shadow_rogue_group_test"
    password = "shadow-runtime-login-test-password"
    PostgresHistoryRepository(dsn).migrate()

    def drop_login(admin: Any) -> None:
        if admin.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
            (login_role,),
        ).fetchone()[0]:
            admin.execute(
                psycopg_sql.SQL("DROP OWNED BY {}").format(
                    psycopg_sql.Identifier(login_role)
                )
            )
            admin.execute(
                psycopg_sql.SQL("DROP ROLE {}").format(
                    psycopg_sql.Identifier(login_role)
                )
            )

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(
            "DROP FUNCTION IF EXISTS public.b3s_c7_shadow_unsafe()"
        )
        admin.execute(
            "DROP TABLE IF EXISTS public.b3s_c7_shadow_unsafe_owned"
        )
        drop_login(admin)
        admin.execute(
            psycopg_sql.SQL("DROP ROLE IF EXISTS {}").format(
                psycopg_sql.Identifier(rogue_group)
            )
        )
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                psycopg_sql.Identifier(login_role),
                psycopg_sql.Literal(password),
            )
        )
        admin.execute(
            psycopg_sql.SQL(
                "GRANT b3s_history_vault_runtime_read TO {}"
            ).format(psycopg_sql.Identifier(login_role))
        )
    login_dsn = make_conninfo(dsn, user=login_role, password=password)
    registry = _registry()

    try:
        baseline = EvidenceVaultC7ShadowRepository(
            login_dsn,
            public_key_registry=registry,
            expected_public_key_registry_fingerprint=(
                public_key_registry_fingerprint(registry)
            ),
        ).get_evidence_vault_c7_shadow_readiness("example.com")
        assert baseline.reason is not C7ShadowReadinessReason.STORAGE_UNAVAILABLE

        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                psycopg_sql.SQL(
                    "GRANT b3s_history_vault_runtime_read TO {} WITH ADMIN OPTION"
                ).format(psycopg_sql.Identifier(login_role))
            )

        admin_option_denied = EvidenceVaultC7ShadowRepository(
            login_dsn,
            public_key_registry=registry,
            expected_public_key_registry_fingerprint=(
                public_key_registry_fingerprint(registry)
            ),
        ).get_evidence_vault_c7_shadow_readiness("example.com")
        assert (
            admin_option_denied.reason
            is C7ShadowReadinessReason.STORAGE_UNAVAILABLE
        )

        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                psycopg_sql.SQL(
                    "REVOKE ADMIN OPTION FOR "
                    "b3s_history_vault_runtime_read FROM {}"
                ).format(psycopg_sql.Identifier(login_role))
            )
            admin.execute(
                "CREATE TABLE public.b3s_c7_shadow_unsafe_owned (id integer)"
            )
            admin.execute(
                psycopg_sql.SQL(
                    "ALTER TABLE public.b3s_c7_shadow_unsafe_owned OWNER TO {}"
                ).format(psycopg_sql.Identifier(login_role))
            )

        owner_denied = EvidenceVaultC7ShadowRepository(
            login_dsn,
            public_key_registry=registry,
            expected_public_key_registry_fingerprint=(
                public_key_registry_fingerprint(registry)
            ),
        ).get_evidence_vault_c7_shadow_readiness("example.com")
        assert owner_denied.reason is C7ShadowReadinessReason.STORAGE_UNAVAILABLE

        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                "DROP TABLE public.b3s_c7_shadow_unsafe_owned"
            )
            admin.execute(
                psycopg_sql.SQL(
                    "GRANT SELECT (receipt_fingerprint) ON "
                    "b3s_history.evidence_vault_raw_acquisition_receipts TO {}"
                ).format(psycopg_sql.Identifier(login_role))
            )

        denied = EvidenceVaultC7ShadowRepository(
            login_dsn,
            public_key_registry=registry,
            expected_public_key_registry_fingerprint=(
                public_key_registry_fingerprint(registry)
            ),
        ).get_evidence_vault_c7_shadow_readiness("example.com")
        assert denied.reason is C7ShadowReadinessReason.STORAGE_UNAVAILABLE

        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                psycopg_sql.SQL(
                    "REVOKE SELECT (receipt_fingerprint) ON "
                    "b3s_history.evidence_vault_raw_acquisition_receipts FROM {}"
                ).format(psycopg_sql.Identifier(login_role))
            )
            admin.execute(
                """
                CREATE FUNCTION public.b3s_c7_shadow_unsafe()
                RETURNS integer
                LANGUAGE sql
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS 'SELECT 1'
                """
            )
            admin.execute(
                "REVOKE ALL ON FUNCTION public.b3s_c7_shadow_unsafe() FROM PUBLIC"
            )
            admin.execute(
                psycopg_sql.SQL(
                    "GRANT EXECUTE ON FUNCTION public.b3s_c7_shadow_unsafe() TO {}"
                ).format(psycopg_sql.Identifier(login_role))
            )

        function_denied = EvidenceVaultC7ShadowRepository(
            login_dsn,
            public_key_registry=registry,
            expected_public_key_registry_fingerprint=(
                public_key_registry_fingerprint(registry)
            ),
        ).get_evidence_vault_c7_shadow_readiness("example.com")
        assert function_denied.reason is C7ShadowReadinessReason.STORAGE_UNAVAILABLE

        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                "DROP FUNCTION public.b3s_c7_shadow_unsafe()"
            )
            admin.execute(
                psycopg_sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(
                    psycopg_sql.Identifier(rogue_group)
                )
            )
            admin.execute(
                psycopg_sql.SQL(
                    "GRANT {} TO b3s_history_vault_runtime_read"
                ).format(psycopg_sql.Identifier(rogue_group))
            )

        group_denied = EvidenceVaultC7ShadowRepository(
            login_dsn,
            public_key_registry=registry,
            expected_public_key_registry_fingerprint=(
                public_key_registry_fingerprint(registry)
            ),
        ).get_evidence_vault_c7_shadow_readiness("example.com")
        assert group_denied.reason is C7ShadowReadinessReason.STORAGE_UNAVAILABLE

        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                psycopg_sql.SQL("DROP ROLE {}").format(
                    psycopg_sql.Identifier(rogue_group)
                )
            )
            admin.execute(
                "ALTER ROLE b3s_history_vault_runtime_read SUPERUSER"
            )

        elevated_group_denied = EvidenceVaultC7ShadowRepository(
            login_dsn,
            public_key_registry=registry,
            expected_public_key_registry_fingerprint=(
                public_key_registry_fingerprint(registry)
            ),
        ).get_evidence_vault_c7_shadow_readiness("example.com")
        assert (
            elevated_group_denied.reason
            is C7ShadowReadinessReason.STORAGE_UNAVAILABLE
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                "DROP FUNCTION IF EXISTS public.b3s_c7_shadow_unsafe()"
            )
            admin.execute(
                "DROP TABLE IF EXISTS public.b3s_c7_shadow_unsafe_owned"
            )
            admin.execute(
                "ALTER ROLE b3s_history_vault_runtime_read "
                "NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB "
                "NOREPLICATION NOBYPASSRLS NOLOGIN"
            )
            drop_login(admin)
            admin.execute(
                psycopg_sql.SQL("DROP ROLE IF EXISTS {}").format(
                    psycopg_sql.Identifier(rogue_group)
                )
            )
