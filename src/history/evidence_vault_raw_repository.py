"""Worker-only PostgreSQL ingest for signed Evidence Vault acquisitions.

The repository intentionally has no migration or general table-DML surface.  Its
only durable capabilities are the scanner role's two SECURITY DEFINER functions.
Every successful write is read back and reverified before its transaction commits.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.history.capture_observation import (
    CAPTURE_OBSERVATION_SCHEMA_VERSION,
    parse_capture_observation,
)
from src.services.evidence_vault_acquisition_outcome import (
    trusted_acquisition_report_metadata as _trusted_acquisition_report_metadata,
)
from src.services.evidence_vault_acquisition_worker import (
    DurableAcquisitionReadback,
    DurableReceiptReadback,
    SignedAcquisition,
    TrustedAcquisitionCommand,
)
from src.services.evidence_vault_canonical_core import canonical_fingerprint, canonical_json
from src.services.evidence_vault_incremental_refresh import validate_vault_scan_plan
from src.services.evidence_vault_raw_capture import (
    DETERMINISTIC_EXTRACTOR_VERSION,
    VerifiedRawCapture,
    build_signed_raw_capture,
    extract_deterministic_document,
    reproduce_passage_locator,
    validate_signed_raw_capture,
)
from src.services.evidence_vault_raw_provenance import (
    DirectAcquisition,
    PublicKeyRegistry,
    ProviderApiAcquisition,
    external_identity_provenance_fingerprint,
    pre_receipt_snapshot_sha256,
    validate_c7_receipt_time_policy,
    verify_raw_acquisition_receipt,
)
from src.services.scanner_evidence_comparison import canonical_evidence_rows


_SCHEMA = "b3s_history"
_CONNECT_TIMEOUT_SECONDS = 5
_ID_NAMESPACE = UUID("3ef1b80c-e7b7-4fb3-95ad-fb9e03c59d52")
_PIPELINE_VERSION = "evidence-vault-trusted-acquisition-v1"
_RAW_BINDING_VERSION = "evidence-vault-raw-evidence-binding-v1"
_WATERMARK_VERSION = "evidence-vault-capture-watermark-event-v1"
_ERROR = "raw_acquisition_repository_failed"

_APPEND_SQL = (
    "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb) "
    "AS append_result"
)
_READ_SQL = (
    "SELECT b3s_history.read_evidence_vault_raw_acquisition(%s, %s) "
    "AS acquisition, clock_timestamp() AS database_time"
)
_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"
_ROLE_PREFLIGHT_SQL = """
WITH role_row AS (
    SELECT * FROM pg_catalog.pg_roles WHERE rolname = current_user
), allowed_functions AS (
    SELECT routines.oid, routines.proowner, routines.proacl
    FROM pg_catalog.pg_proc AS routines
    WHERE routines.oid = ANY(ARRAY[
        'b3s_history.append_evidence_vault_raw_acquisition(jsonb)'::regprocedure::oid,
        'b3s_history.read_evidence_vault_raw_acquisition(text,text)'::regprocedure::oid
    ])
), allowed_function_acl_invalid AS (
    SELECT 1
    FROM allowed_functions AS routines
    CROSS JOIN role_row
    WHERE NOT EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            coalesce(
                routines.proacl,
                pg_catalog.acldefault('f', routines.proowner)
            )
        ) AS grants
        WHERE grants.grantee = role_row.oid
          AND grants.privilege_type = 'EXECUTE'
          AND NOT grants.is_grantable
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.aclexplode(
            coalesce(
                routines.proacl,
                pg_catalog.acldefault('f', routines.proowner)
            )
        ) AS grants
        WHERE grants.privilege_type = 'EXECUTE'
          AND (
              grants.grantee = 0
              OR grants.grantee NOT IN (role_row.oid, routines.proowner)
              OR (grants.grantee = role_row.oid AND grants.is_grantable)
          )
    )
), application_relations AS (
    SELECT relations.oid, relations.relkind,
           relations.relowner, relations.relacl
    FROM pg_catalog.pg_class AS relations
    JOIN pg_catalog.pg_namespace AS schemas ON schemas.oid = relations.relnamespace
    WHERE schemas.nspname !~ '^pg_'
      AND schemas.nspname <> 'information_schema'
), forbidden_table_privilege AS (
    SELECT 1
    FROM application_relations AS relations
    WHERE relations.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND (
          pg_catalog.has_table_privilege(current_user, relations.oid, 'SELECT')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'INSERT')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'UPDATE')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'DELETE')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'TRUNCATE')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'TRIGGER')
          OR pg_catalog.has_table_privilege(current_user, relations.oid, 'REFERENCES')
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(
                  coalesce(
                      relations.relacl,
                      pg_catalog.acldefault('r', relations.relowner)
                  )
              ) AS grants
              WHERE grants.grantee IN (0, (SELECT oid FROM role_row))
                AND grants.privilege_type = 'MAINTAIN'
          )
      )
), forbidden_column_privilege AS (
    SELECT 1
    FROM application_relations AS relations
    WHERE relations.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND pg_catalog.has_any_column_privilege(
          current_user,
          relations.oid,
          'SELECT,INSERT,UPDATE,REFERENCES'
      )
), forbidden_sequence_privilege AS (
    SELECT 1
    FROM application_relations AS relations
    WHERE relations.relkind = 'S'
      AND (
          pg_catalog.has_sequence_privilege(current_user, relations.oid, 'USAGE')
          OR pg_catalog.has_sequence_privilege(current_user, relations.oid, 'SELECT')
          OR pg_catalog.has_sequence_privilege(current_user, relations.oid, 'UPDATE')
      )
), forbidden_security_definer_execute AS (
    SELECT 1
    FROM pg_catalog.pg_proc AS routines
    WHERE routines.prosecdef
      AND routines.oid NOT IN (SELECT oid FROM allowed_functions)
      AND pg_catalog.has_function_privilege(
          current_user, routines.oid, 'EXECUTE'
      )
), forbidden_direct_function_grant AS (
    SELECT 1
    FROM pg_catalog.pg_proc AS routines
    JOIN pg_catalog.pg_namespace AS schemas ON schemas.oid = routines.pronamespace
    CROSS JOIN role_row
    CROSS JOIN LATERAL pg_catalog.aclexplode(
        coalesce(
            routines.proacl,
            pg_catalog.acldefault('f', routines.proowner)
        )
    ) AS grants
    WHERE grants.grantee = role_row.oid
      AND grants.privilege_type = 'EXECUTE'
      AND routines.oid NOT IN (SELECT oid FROM allowed_functions)
), forbidden_schema_create AS (
    SELECT 1
    FROM pg_catalog.pg_namespace AS schemas
    WHERE schemas.nspname !~ '^pg_'
      AND schemas.nspname <> 'information_schema'
      AND pg_catalog.has_schema_privilege(
          current_user, schemas.oid, 'CREATE'
      )
), owned_object AS (
    SELECT 1
    FROM pg_catalog.pg_shdepend AS dependencies
    CROSS JOIN role_row
    WHERE dependencies.refclassid = 'pg_catalog.pg_authid'::regclass
      AND dependencies.refobjid = role_row.oid
      AND dependencies.deptype = 'o'
), role_membership AS (
    SELECT 1
    FROM pg_catalog.pg_auth_members AS memberships
    JOIN role_row ON role_row.oid = memberships.member
)
SELECT
    session_user = current_user
        AND (%s::text IS NULL OR current_user::text = %s::text)
        AS direct_session,
    role_row.rolcanlogin AND NOT role_row.rolsuper
        AND NOT role_row.rolinherit AND NOT role_row.rolcreaterole
        AND NOT role_row.rolcreatedb AND NOT role_row.rolreplication
        AND NOT role_row.rolbypassrls AS restricted_login,
    pg_catalog.has_database_privilege(
        current_user, current_database(), 'CONNECT'
    ) AND NOT pg_catalog.has_database_privilege(
        current_user, current_database(), 'CREATE'
    ) AS database_scope,
    pg_catalog.has_schema_privilege(current_user, 'b3s_history', 'USAGE')
        AND NOT pg_catalog.has_schema_privilege(
            current_user, 'b3s_history', 'CREATE'
        )
        AND NOT EXISTS (SELECT 1 FROM forbidden_schema_create)
        AS schema_scope,
    pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.append_evidence_vault_raw_acquisition(jsonb)'::regprocedure,
        'EXECUTE'
    ) AND pg_catalog.has_function_privilege(
        current_user,
        'b3s_history.read_evidence_vault_raw_acquisition(text,text)'::regprocedure,
        'EXECUTE'
    ) AND NOT EXISTS (SELECT 1 FROM allowed_function_acl_invalid)
        AS required_execute,
    NOT EXISTS (SELECT 1 FROM forbidden_security_definer_execute)
        AND NOT EXISTS (SELECT 1 FROM forbidden_direct_function_grant)
        AS forbidden_execute_absent,
    NOT EXISTS (SELECT 1 FROM forbidden_table_privilege)
        AS table_privileges_absent,
    NOT EXISTS (SELECT 1 FROM forbidden_column_privilege)
        AS column_privileges_absent,
    NOT EXISTS (SELECT 1 FROM forbidden_sequence_privilege)
        AS sequence_privileges_absent,
    NOT EXISTS (SELECT 1 FROM owned_object)
        AS ownership_absent,
    NOT EXISTS (SELECT 1 FROM role_membership)
        AND NOT pg_catalog.pg_has_role(
            current_user, 'b3s_history_vault_provenance_owner', 'MEMBER'
        ) AS memberships_absent,
    current_database() = %s
        AND current_setting('neon.project_id', true) IS NOT DISTINCT FROM %s
        AND current_setting('neon.branch_id', true) IS NOT DISTINCT FROM %s
        AS target_identity
FROM role_row
"""

_TOP_LEVEL_FIELDS = {
    "workspace",
    "brand",
    "scan_run",
    "operation_plan",
    "capture",
    "evidence_records",
    "watermark_event",
    "receipts",
    "evidence_bindings",
}
_WORKSPACE_FIELDS = {"id", "slug", "name", "created_at"}
_BRAND_FIELDS = {
    "id",
    "workspace_id",
    "canonical_domain",
    "display_name",
    "canonical_url",
    "first_observed_at",
    "latest_observed_at",
    "created_at",
    "updated_at",
}
_SCAN_FIELDS = {
    "id",
    "workspace_id",
    "brand_id",
    "source_scan_id",
    "source_run_id",
    "status",
    "pipeline_version",
    "acquisition_state",
    "requested_at",
    "started_at",
    "completed_at",
    "recorded_at",
    "error_summary",
    "request_payload",
    "metadata",
}
_PLAN_FIELDS = {
    "id",
    "workspace_id",
    "brand_id",
    "scan_run_id",
    "observation_hash",
    "operation_plan_fingerprint",
    "canonical_memory_version",
    "mode",
    "status",
    "plan_payload",
    "attempt_count",
    "lease_owner",
    "lease_token",
    "lease_generation",
    "lease_expires_at",
    "claimed_at",
    "started_at",
    "heartbeat_at",
    "result_fingerprint",
    "result_payload",
    "result_persisted_at",
    "candidate_packet_fingerprint",
    "completed_at",
    "superseded_at",
    "last_error",
    "authority",
    "authority_scope",
    "production_runtime_effect",
    "scanner_runtime_effect",
    "created_at",
    "updated_at",
}
_CAPTURE_FIELDS = {
    "id",
    "scan_run_id",
    "brand_id",
    "observed_at",
    "recorded_at",
    "source_url",
    "content_hash",
    "acquisition_summary",
    "limitations",
    "raw_payload",
}
_EVIDENCE_FIELDS = {
    "id",
    "capture_id",
    "evidence_ref",
    "source",
    "source_class",
    "evidence_type",
    "url",
    "content",
    "content_raw",
    "content_hash",
    "confidence",
    "metadata",
}
_WATERMARK_FIELDS = {
    "id",
    "brand_id",
    "capture_id",
    "capture_sequence",
    "previous_event_id",
    "previous_event_fingerprint",
    "capture_content_hash",
    "capture_observation_hash",
    "append_origin",
    "event_schema_version",
    "event_fingerprint",
    "authority",
    "production_runtime_effect",
    "scanner_runtime_effect",
    "created_at",
}
_RECEIPT_FIELDS = {
    "id",
    "workspace_id",
    "brand_id",
    "scan_run_id",
    "source_scan_id",
    "capture_id",
    "watermark_event_id",
    "capture_sequence",
    "receipt_schema_version",
    "freshness_policy_version",
    "signature_schema_version",
    "key_id",
    "receipt_nonce",
    "acquisition_session_id",
    "workspace_slug",
    "canonical_brand",
    "channel_role",
    "provider",
    "acquisition_mode",
    "pre_receipt_snapshot_sha256",
    "source_url",
    "raw_fragment_pointer",
    "raw_fragment_sha256",
    "extracted_document_sha256",
    "extractor_version",
    "external_identity_provenance_fingerprint",
    "external_identity_provenance",
    "fetched_at",
    "received_at",
    "eligible_until",
    "claims",
    "receipt_fingerprint",
    "signed_payload",
    "signature",
    "created_at",
}
_BINDING_FIELDS = {
    "id",
    "workspace_id",
    "brand_id",
    "scan_run_id",
    "capture_id",
    "receipt_id",
    "channel_role",
    "evidence_record_id",
    "evidence_ref",
    "source_url",
    "extractor_schema_version",
    "extractor_version",
    "extracted_document",
    "extracted_document_sha256",
    "passage_locator",
    "passage_text",
    "passage_sha256",
    "evidence_record_content_hash",
    "binding_fingerprint",
    "created_at",
}


class EvidenceVaultRawRepositoryError(RuntimeError):
    """A scanner-ingest failure that never exposes database or signed raw data."""


class OperationPlanBuilder(Protocol):
    """Worker-local planner over the repository's exact deterministic evidence."""

    def __call__(
        self,
        command: TrustedAcquisitionCommand,
        evidence_records: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class _PreparedAcquisition:
    envelope: dict[str, Any]
    observation: Any
    verified: VerifiedRawCapture
    evidence_records: tuple[dict[str, Any], ...]
    receipt_ids: tuple[str, ...]
    binding_ids: tuple[str, ...]
    plan_id: str


class EvidenceVaultRawRepository:
    """Execute-only repository usable as a TrustedAcquisitionWorker capability."""

    __slots__ = (
        "__connect_fn",
        "__dsn",
        "__expected_database",
        "__expected_neon_branch_id",
        "__expected_neon_project_id",
        "__expected_role",
        "__operation_plan_builder",
        "__public_key_registry_json",
    )

    def __init__(
        self,
        dsn: str,
        *,
        public_key_registry: PublicKeyRegistry | Mapping[str, Any],
        operation_plan_builder: OperationPlanBuilder,
        expected_database: str,
        expected_neon_project_id: str | None,
        expected_neon_branch_id: str | None,
        expected_role: str | None = None,
        connect: Callable[..., Any] = psycopg.connect,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise EvidenceVaultRawRepositoryError(_ERROR)
        if not callable(connect) or not callable(operation_plan_builder):
            raise EvidenceVaultRawRepositoryError(_ERROR)
        try:
            database = _target_identity_text(expected_database, maximum=63)
            project_id = _optional_target_identity_text(
                expected_neon_project_id,
                maximum=255,
            )
            branch_id = _optional_target_identity_text(
                expected_neon_branch_id,
                maximum=255,
            )
            role = (
                None
                if expected_role is None
                else _target_identity_text(expected_role, maximum=63)
            )
            if (project_id is None) != (branch_id is None):
                raise ValueError("incomplete Neon target")
        except Exception:
            raise EvidenceVaultRawRepositoryError(_ERROR) from None
        try:
            registry_value = (
                public_key_registry.model_dump(mode="python", round_trip=True)
                if isinstance(public_key_registry, PublicKeyRegistry)
                else deepcopy(dict(public_key_registry))
            )
            registry = PublicKeyRegistry.model_validate(registry_value, strict=True)
        except Exception:
            registry = None
        if registry is None:
            raise EvidenceVaultRawRepositoryError(_ERROR)
        self.__dsn = dsn
        self.__connect_fn = connect
        self.__expected_database = database
        self.__expected_neon_project_id = project_id
        self.__expected_neon_branch_id = branch_id
        self.__expected_role = role
        self.__operation_plan_builder = operation_plan_builder
        self.__public_key_registry_json = registry.model_dump_json()

    def verify_ingest_capability(self) -> None:
        """Prove database connectivity and the execute-only scanner role."""

        try:
            with self.__connect() as connection:
                connection.execute(_READ_ONLY_SQL)
                self.__assert_scanner_session(connection)
                return
        except Exception:
            pass
        raise EvidenceVaultRawRepositoryError(_ERROR)

    def persist(
        self,
        command: TrustedAcquisitionCommand,
        signed_acquisition: SignedAcquisition,
    ) -> DurableAcquisitionReadback:
        """Append once, reread, and verify before committing the same transaction."""

        try:
            validated_command = _reparse_command(command)
            registry = self.__registry()
            verified = _verify_signed_acquisition(
                validated_command,
                signed_acquisition,
                registry=registry,
            )
            prepared = self.__prepare(validated_command, verified)
            with self.__connect() as connection:
                self.__assert_scanner_session(connection)
                append_row = connection.execute(
                    _APPEND_SQL,
                    (Jsonb(deepcopy(prepared.envelope)),),
                ).fetchone()
                append_result = _single_result(append_row, "append_result")
                readback, projection = self.__read_and_validate(
                    connection,
                    command=validated_command,
                    expected=prepared,
                )
                _validate_append_result(append_result, projection, prepared)
                if (
                    readback.capture_content_hash != verified.capture_content_hash
                    or readback.durable_raw_capture_payload != _durable_payload(verified)
                ):
                    raise ValueError("durable capture diverged from attempted acquisition")
                stored = _signed_from_verified_projection(readback, registry)
                attempted = _signed_from_verified(verified)
                if stored != attempted:
                    raise ValueError("durable signed acquisition diverged")
                return readback
        except Exception:
            pass
        raise EvidenceVaultRawRepositoryError(_ERROR)

    def lookup(
        self,
        command: TrustedAcquisitionCommand,
    ) -> DurableAcquisitionReadback | None:
        """Read one complete acquisition without recollection or write capability."""

        try:
            validated_command = _reparse_command(command)
            with self.__connect() as connection:
                connection.execute(_READ_ONLY_SQL)
                self.__assert_scanner_session(connection)
                row = connection.execute(
                    _READ_SQL,
                    (validated_command.workspace_slug, validated_command.source_scan_id),
                ).fetchone()
                if not isinstance(row, Mapping):
                    raise ValueError("raw acquisition lookup returned no row")
                projection = row.get("acquisition")
                if projection is None:
                    return None
                database_time = _database_datetime(row.get("database_time"))
                return self.__validate_projection(
                    projection,
                    command=validated_command,
                    database_time=database_time,
                )
        except Exception:
            pass
        raise EvidenceVaultRawRepositoryError(_ERROR)

    def __assert_scanner_session(self, connection: Any) -> None:
        _assert_scanner_session(
            connection,
            expected_database=self.__expected_database,
            expected_neon_project_id=self.__expected_neon_project_id,
            expected_neon_branch_id=self.__expected_neon_branch_id,
            expected_role=self.__expected_role,
        )

    def __connect(self) -> Any:
        return self.__connect_fn(
            self.__dsn,
            row_factory=dict_row,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        )

    def __registry(self) -> PublicKeyRegistry:
        return PublicKeyRegistry.model_validate_json(
            self.__public_key_registry_json,
            strict=True,
        )

    def __prepare(
        self,
        command: TrustedAcquisitionCommand,
        verified: VerifiedRawCapture,
        *,
        frozen_operation_plan: Mapping[str, Any] | None = None,
    ) -> _PreparedAcquisition:
        evidence = _deterministic_evidence_records(verified)
        plan = (
            _build_and_validate_plan(
                self.__operation_plan_builder,
                command=command,
                evidence_records=evidence,
            )
            if frozen_operation_plan is None
            else _validate_plan_for_evidence(
                frozen_operation_plan,
                command=command,
                evidence_records=evidence,
            )
        )
        observed_at = _latest_fetched_at_text(verified)
        plan_status = _pre_interpretation_status(plan)
        capture_payload = _durable_payload(verified)
        _trusted_acquisition_report_metadata(
            capture_payload,
            external_captured=any(
                receipt.claims.channel_role == "external_social_profile"
                for receipt in verified.receipts
            ),
        )
        observation_payload = {
            "schema_version": CAPTURE_OBSERVATION_SCHEMA_VERSION,
            "source_scan_id": command.source_scan_id,
            "source_run_id": "",
            "brand_name": verified.pre_receipt_snapshot.canonical_brand_domain,
            "url": command.brand_url,
            "observed_at": observed_at,
            "recorded_at": observed_at,
            "pipeline_version": _PIPELINE_VERSION,
            "acquisition_state": "completed",
            "acquisition_summary": {
                "receipt_count": len(verified.receipts),
                "receipt_set_fingerprint": verified.envelope.receipt_set_fingerprint,
            },
            "limitations": [],
            "capture_payload": capture_payload,
            "evidence_records": deepcopy(evidence),
            "acquisition_attempts": [],
            "artifacts": [],
            "metadata": {
                "acquisition_phase": "pre_interpretation",
                "operation_plan": deepcopy(plan),
                "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
                "analysis_status": plan_status,
            },
        }
        observation = parse_capture_observation(observation_payload)
        envelope, receipt_ids, binding_ids, plan_id = _build_sql_envelope(
            observation=observation,
            verified=verified,
            operation_plan=plan,
            operation_plan_status=plan_status,
        )
        return _PreparedAcquisition(
            envelope=envelope,
            observation=observation,
            verified=verified,
            evidence_records=tuple(deepcopy(evidence)),
            receipt_ids=tuple(receipt_ids),
            binding_ids=tuple(binding_ids),
            plan_id=plan_id,
        )

    def __read_and_validate(
        self,
        connection: Any,
        *,
        command: TrustedAcquisitionCommand,
        expected: _PreparedAcquisition | None = None,
    ) -> tuple[DurableAcquisitionReadback, Mapping[str, Any]]:
        row = connection.execute(
            _READ_SQL,
            (command.workspace_slug, command.source_scan_id),
        ).fetchone()
        if not isinstance(row, Mapping) or row.get("acquisition") is None:
            raise ValueError("raw acquisition readback is absent")
        database_time = _database_datetime(row.get("database_time"))
        projection = _mapping(row["acquisition"], fields=_TOP_LEVEL_FIELDS)
        readback = self.__validate_projection(
            projection,
            command=command,
            database_time=database_time,
            expected=expected,
        )
        return readback, projection

    def __validate_projection(
        self,
        value: Any,
        *,
        command: TrustedAcquisitionCommand,
        database_time: datetime,
        expected: _PreparedAcquisition | None = None,
    ) -> DurableAcquisitionReadback:
        projection = _mapping(value, fields=_TOP_LEVEL_FIELDS)
        workspace = _mapping(projection["workspace"], fields=_WORKSPACE_FIELDS)
        brand = _mapping(projection["brand"], fields=_BRAND_FIELDS)
        scan = _mapping(projection["scan_run"], fields=_SCAN_FIELDS)
        capture = _mapping(projection["capture"], fields=_CAPTURE_FIELDS)
        plan_row = _mapping(projection["operation_plan"], fields=_PLAN_FIELDS)
        watermark = _mapping(projection["watermark_event"], fields=_WATERMARK_FIELDS)
        evidence_rows = _mapping_rows(projection["evidence_records"], fields=_EVIDENCE_FIELDS)
        receipt_rows = _mapping_rows(projection["receipts"], fields=_RECEIPT_FIELDS)
        binding_rows = _mapping_rows(projection["evidence_bindings"], fields=_BINDING_FIELDS)
        if not (1 <= len(receipt_rows) <= 2) or len(evidence_rows) != len(receipt_rows):
            raise ValueError("raw acquisition row cardinality is invalid")
        if len(binding_rows) != len(receipt_rows):
            raise ValueError("raw acquisition binding cardinality is invalid")

        expected_workspace_id = str(_stable_uuid("workspace", command.workspace_slug))
        if (
            _uuid_text(workspace["id"]) != expected_workspace_id
            or workspace["slug"] != command.workspace_slug
            or workspace["name"] != _workspace_name(command.workspace_slug)
        ):
            raise ValueError("workspace projection diverged")
        _database_datetime(workspace["created_at"])

        expected_brand_id = str(
            _stable_uuid(expected_workspace_id, "brand", _command_domain(command))
        )
        if (
            _uuid_text(brand["id"]) != expected_brand_id
            or _uuid_text(brand["workspace_id"]) != expected_workspace_id
            or brand["canonical_domain"] != _command_domain(command)
            or brand["canonical_url"] != command.brand_url
        ):
            raise ValueError("brand projection diverged")
        first_observed = _database_datetime(brand["first_observed_at"])
        latest_observed = _database_datetime(brand["latest_observed_at"])
        _database_datetime(brand["created_at"])
        _database_datetime(brand["updated_at"])
        if first_observed > latest_observed:
            raise ValueError("brand observation bounds diverged")

        expected_scan_id = str(
            _stable_uuid(expected_workspace_id, "scan", command.source_scan_id)
        )
        expected_capture_id = str(_stable_uuid(expected_scan_id, "capture"))
        if (
            _uuid_text(scan["id"]) != expected_scan_id
            or _uuid_text(scan["workspace_id"]) != expected_workspace_id
            or _uuid_text(scan["brand_id"]) != expected_brand_id
            or scan["source_scan_id"] != command.source_scan_id
        ):
            raise ValueError("scan projection diverged")
        observation_payload = _canonical_detach(scan["request_payload"])
        observation = parse_capture_observation(observation_payload)
        if (
            observation.source_scan_id != command.source_scan_id
            or observation.canonical_domain != _command_domain(command)
            or observation.canonical_url != command.brand_url
            or observation.capture_payload != capture["raw_payload"]
        ):
            raise ValueError("capture observation identity diverged")
        if not (first_observed <= observation.observed_at <= latest_observed):
            raise ValueError("brand bounds omit capture observation")

        verified = validate_signed_raw_capture(
            observation.capture_payload,
            capture_content_hash=str(capture["content_hash"]),
            public_key_registry=self.__registry(),
        )
        _require_verified_command(command, verified)
        prepared = expected or self.__prepare(
            command,
            verified,
            frozen_operation_plan=_mapping(plan_row["plan_payload"]),
        )
        if prepared.observation.raw_observation != observation_payload:
            raise ValueError("stored capture observation is not reproducible")

        _validate_scan_row(scan, prepared, expected_scan_id, expected_workspace_id, expected_brand_id)
        _validate_capture_row(capture, prepared, expected_capture_id, expected_scan_id, expected_brand_id)
        _validate_plan_row(plan_row, prepared, expected_workspace_id, expected_brand_id, expected_scan_id)
        _validate_evidence_rows(evidence_rows, prepared)
        _validate_watermark(
            watermark,
            prepared=prepared,
            brand_id=expected_brand_id,
            capture_id=expected_capture_id,
        )
        arrivals = _validate_receipt_and_binding_rows(
            receipt_rows,
            binding_rows,
            evidence_rows=evidence_rows,
            prepared=prepared,
            watermark=watermark,
            database_time=database_time,
            registry=self.__registry(),
        )
        return DurableAcquisitionReadback(
            capture_id=expected_capture_id,
            capture_content_hash=verified.capture_content_hash,
            capture_observation_hash=observation.observation_hash,
            durable_raw_capture_payload=_durable_payload(verified),
            database_time=database_time,
            receipt_rows=arrivals,
        )


# A descriptive alias for deployments that prefer an explicit PostgreSQL name.
PostgresEvidenceVaultRawRepository = EvidenceVaultRawRepository


def _assert_scanner_session(
    connection: Any,
    *,
    expected_database: str,
    expected_neon_project_id: str | None,
    expected_neon_branch_id: str | None,
    expected_role: str | None,
) -> None:
    row = connection.execute(
        _ROLE_PREFLIGHT_SQL,
        (
            expected_role,
            expected_role,
            expected_database,
            expected_neon_project_id,
            expected_neon_branch_id,
        ),
    ).fetchone()
    expected = {
        "direct_session",
        "restricted_login",
        "database_scope",
        "schema_scope",
        "required_execute",
        "forbidden_execute_absent",
        "table_privileges_absent",
        "column_privileges_absent",
        "sequence_privileges_absent",
        "ownership_absent",
        "memberships_absent",
        "target_identity",
    }
    if not isinstance(row, Mapping) or set(row) != expected or not all(
        row[field] is True for field in expected
    ):
        raise ValueError("scanner ingest database role is not execute-only")


def _verify_signed_acquisition(
    command: TrustedAcquisitionCommand,
    value: SignedAcquisition,
    *,
    registry: PublicKeyRegistry,
) -> VerifiedRawCapture:
    if not isinstance(value, SignedAcquisition):
        raise ValueError("signed acquisition must be the strict worker model")
    reparsed = SignedAcquisition.model_validate(
        deepcopy(value.model_dump(mode="python", round_trip=True)),
        strict=True,
    )
    built = build_signed_raw_capture(
        reparsed.pre_receipt_snapshot,
        reparsed.receipts,
        public_key_registry=registry,
        external_identity_provenance=reparsed.external_identity_provenance,
    )
    verified = validate_signed_raw_capture(
        built.durable_raw_capture_payload,
        capture_content_hash=built.capture_content_hash,
        public_key_registry=registry,
    )
    _require_verified_command(command, verified)
    if _signed_from_verified(verified) != reparsed:
        raise ValueError("signed acquisition changed during verification")
    return verified


def _signed_from_verified(verified: VerifiedRawCapture) -> SignedAcquisition:
    return SignedAcquisition(
        pre_receipt_snapshot=verified.pre_receipt_snapshot,
        receipts=list(verified.receipts),
        receipt_set_fingerprint=verified.envelope.receipt_set_fingerprint,
        external_identity_provenance=verified.envelope.external_identity_provenance,
    )


def _signed_from_verified_projection(
    readback: DurableAcquisitionReadback,
    registry: PublicKeyRegistry,
) -> SignedAcquisition:
    verified = validate_signed_raw_capture(
        readback.durable_raw_capture_payload,
        capture_content_hash=readback.capture_content_hash,
        public_key_registry=registry,
    )
    return _signed_from_verified(verified)


def _deterministic_evidence_records(verified: VerifiedRawCapture) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for receipt in verified.receipts:
        extraction = extract_deterministic_document(
            verified,
            receipt_fingerprint=receipt.receipt_fingerprint,
        )
        content_bytes = extraction.document.encode("utf-8")
        if len(content_bytes) < 8 or len(extraction.document) < 4:
            raise ValueError("deterministic document is not a meaningful full passage")
        if len(content_bytes) > 2_097_152:
            raise ValueError("deterministic document exceeds the durable v1 bound")
        role = receipt.claims.channel_role
        source_url = _receipt_source_url(receipt.claims.acquisition, role=role)
        source_class = "owned_copy" if role == "owned_web" else "external_proof"
        records.append(
            {
                "ref": f"raw-acquisition:{role}:{receipt.receipt_fingerprint}",
                "source": role,
                "evidence_type": "text",
                "url": source_url,
                "content": extraction.document,
                "confidence": "high",
                "metadata": {
                    "source_class": source_class,
                    "provider": "verified_raw_acquisition",
                    "role": role,
                    "channel_role": role,
                    "receipt_fingerprint": receipt.receipt_fingerprint,
                    "extractor_version": receipt.claims.extractor_version,
                    "extracted_document_sha256": extraction.sha256,
                    "verified_raw": True,
                },
            }
        )
    records.sort(key=lambda row: str(row["ref"]))
    return records


def _deterministic_passage_end(document: str) -> int:
    encoded = document.encode("utf-8")
    end = min(len(encoded), 20_000)
    while end > 0 and end < len(encoded) and encoded[end] & 0b1100_0000 == 0b1000_0000:
        end -= 1
    passage = encoded[:end].decode("utf-8")
    if end < 8 or len(passage) < 4:
        raise ValueError("deterministic passage is not meaningful")
    return end


def _receipt_source_url(acquisition: Any, *, role: str) -> str:
    if role == "owned_web" and isinstance(acquisition, DirectAcquisition):
        return acquisition.final_url
    if role == "external_social_profile" and isinstance(acquisition, ProviderApiAcquisition):
        return acquisition.reported_source_url
    raise ValueError("receipt role has no safe exact source URL")


def _build_and_validate_plan(
    builder: OperationPlanBuilder,
    *,
    command: TrustedAcquisitionCommand,
    evidence_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    detached_rows = tuple(deepcopy(dict(row)) for row in evidence_records)
    value = builder(command, detached_rows)
    return _validate_plan_for_evidence(
        value,
        command=command,
        evidence_records=evidence_records,
    )


def _validate_plan_for_evidence(
    value: Mapping[str, Any],
    *,
    command: TrustedAcquisitionCommand,
    evidence_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("operation plan must be an object")
    plan = _canonical_detach(value)
    validate_vault_scan_plan(plan)
    expected_fingerprints = sorted(
        row.fingerprint
        for row in canonical_evidence_rows(
            [deepcopy(dict(row)) for row in evidence_records],
            subject_url=command.brand_url,
        )
    )
    if (
        plan["brand_identity"] != _command_domain(command)
        or plan["subject_url"] != command.brand_url
        or plan["semantic_context"]["evidence_fingerprints"]
        != expected_fingerprints
    ):
        raise ValueError("operation plan differs from its exact deterministic evidence")
    return plan


def _pre_interpretation_status(plan: Mapping[str, Any]) -> str:
    operations = plan["operations"]
    required = bool(
        operations["llm_required"]
        or operations["create_candidate_packet"]
        or operations["create_diagnostic_report"]
    )
    return "pending" if required else "not_required"


def _build_sql_envelope(
    *,
    observation: Any,
    verified: VerifiedRawCapture,
    operation_plan: Mapping[str, Any],
    operation_plan_status: str,
) -> tuple[dict[str, Any], list[str], list[str], str]:
    workspace_id = str(
        _stable_uuid("workspace", verified.pre_receipt_snapshot.workspace_slug)
    )
    brand_id = str(_stable_uuid(workspace_id, "brand", observation.canonical_domain))
    scan_id = str(_stable_uuid(workspace_id, "scan", observation.source_scan_id))
    capture_id = str(_stable_uuid(scan_id, "capture"))
    plan_id = str(
        _stable_uuid(
            scan_id,
            "evidence-vault-operation-plan",
            operation_plan["operation_plan_fingerprint"],
        )
    )
    evidence_by_fingerprint: dict[str, tuple[dict[str, Any], str]] = {}
    evidence_rows: list[dict[str, Any]] = []
    for record in observation.evidence_records:
        row = deepcopy(record)
        ref = str(row["ref"])
        evidence_id = str(_stable_uuid(capture_id, "evidence", ref))
        content = str(row["content"])
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        metadata = deepcopy(dict(row.get("metadata") or {}))
        receipt_fingerprint = str(metadata["receipt_fingerprint"])
        evidence_row = {
            "id": evidence_id,
            "capture_id": capture_id,
            "evidence_ref": ref,
            "source": str(row["source"]),
            "source_class": str(metadata["source_class"]),
            "evidence_type": str(row["evidence_type"]),
            "url": str(row["url"]),
            "content": content,
            "content_raw": None,
            "content_hash": content_hash,
            "confidence": str(row["confidence"]),
            "metadata": metadata,
        }
        evidence_rows.append(evidence_row)
        evidence_by_fingerprint[receipt_fingerprint] = (evidence_row, evidence_id)

    receipt_rows: list[dict[str, Any]] = []
    binding_rows: list[dict[str, Any]] = []
    receipt_ids: list[str] = []
    binding_ids: list[str] = []
    association = verified.envelope.external_identity_provenance
    association_dump = association.model_dump(mode="json") if association is not None else None
    for receipt in verified.receipts:
        fingerprint = receipt.receipt_fingerprint
        evidence_row, evidence_id = evidence_by_fingerprint[fingerprint]
        receipt_dump = receipt.model_dump(mode="json")
        claims = receipt_dump["claims"]
        role = str(claims["channel_role"])
        source_url = str(evidence_row["url"])
        receipt_id = str(
            _stable_uuid(capture_id, "evidence-vault-raw-acquisition-receipt-v1", fingerprint)
        )
        receipt_ids.append(receipt_id)
        external_object = association_dump if role == "external_social_profile" else None
        if role == "external_social_profile":
            if external_object is None:
                raise ValueError("external receipt lacks association")
            external_fingerprint = external_identity_provenance_fingerprint(external_object)
        else:
            external_fingerprint = None
        receipt_rows.append(
            {
                "id": receipt_id,
                "workspace_id": workspace_id,
                "brand_id": brand_id,
                "scan_run_id": scan_id,
                "source_scan_id": observation.source_scan_id,
                "capture_id": capture_id,
                "watermark_event_id": None,
                "capture_sequence": None,
                "receipt_schema_version": claims["schema_version"],
                "freshness_policy_version": claims["freshness_policy_version"],
                "signature_schema_version": receipt_dump["signature_schema"],
                "key_id": claims["key_id"],
                "receipt_nonce": claims["receipt_nonce"],
                "acquisition_session_id": claims["acquisition_session_id"],
                "workspace_slug": claims["workspace_slug"],
                "canonical_brand": claims["canonical_brand_domain"],
                "channel_role": role,
                "provider": claims["provider"],
                "acquisition_mode": claims["acquisition"]["acquisition_mode"],
                "pre_receipt_snapshot_sha256": claims["pre_receipt_snapshot_sha256"],
                "source_url": source_url,
                "raw_fragment_pointer": claims["raw_fragment_json_pointer"],
                "raw_fragment_sha256": claims["raw_fragment_sha256"],
                "extracted_document_sha256": claims["extracted_document_sha256"],
                "extractor_version": claims["extractor_version"],
                "external_identity_provenance_fingerprint": external_fingerprint,
                "external_identity_provenance": deepcopy(external_object),
                "fetched_at": claims["fetched_at"],
                "eligible_until": None,
                "claims": deepcopy(claims),
                "receipt_fingerprint": fingerprint,
                "signed_payload": {
                    key: deepcopy(receipt_dump[key])
                    for key in ("signature_schema", "claims", "receipt_fingerprint")
                },
                "signature": receipt_dump["signature"],
            }
        )
        extracted_document = str(evidence_row["content"])
        passage_end = _deterministic_passage_end(extracted_document)
        locator = {
            "kind": "utf8_byte_range",
            "extracted_start": 0,
            "extracted_end": passage_end,
            "evidence_start": 0,
            "evidence_end": passage_end,
        }
        passage = reproduce_passage_locator(
            extracted_document=extracted_document,
            passage_locator=locator,
            durable_evidence_record_content=str(evidence_row["content"]),
        )
        binding_identity = {
            "receipt_fingerprint": fingerprint,
            "evidence_record_id": evidence_id,
            "evidence_ref": evidence_row["evidence_ref"],
            "source_url": source_url,
            "channel_role": role,
            "extractor_schema_version": DETERMINISTIC_EXTRACTOR_VERSION,
            "extractor_version": claims["extractor_version"],
            "extracted_document_sha256": claims["extracted_document_sha256"],
            "passage_locator": locator,
            "passage_sha256": passage.passage_sha256,
            "evidence_record_content_hash": evidence_row["content_hash"],
        }
        binding_fingerprint = canonical_fingerprint(_RAW_BINDING_VERSION, binding_identity)
        binding_id = str(
            _stable_uuid(capture_id, _RAW_BINDING_VERSION, binding_fingerprint)
        )
        binding_ids.append(binding_id)
        binding_rows.append(
            {
                "id": binding_id,
                "workspace_id": workspace_id,
                "brand_id": brand_id,
                "scan_run_id": scan_id,
                "capture_id": capture_id,
                "receipt_id": receipt_id,
                "channel_role": role,
                "evidence_record_id": evidence_id,
                "evidence_ref": evidence_row["evidence_ref"],
                "source_url": source_url,
                "extractor_schema_version": DETERMINISTIC_EXTRACTOR_VERSION,
                "extractor_version": claims["extractor_version"],
                "extracted_document": extracted_document,
                "extracted_document_sha256": claims["extracted_document_sha256"],
                "passage_locator": locator,
                "passage_text": passage.passage_text,
                "passage_sha256": passage.passage_sha256,
                "evidence_record_content_hash": evidence_row["content_hash"],
                "binding_fingerprint": binding_fingerprint,
            }
        )

    plan_row = {
        "id": plan_id,
        "workspace_id": workspace_id,
        "brand_id": brand_id,
        "scan_run_id": scan_id,
        "observation_hash": observation.observation_hash,
        "operation_plan_fingerprint": operation_plan["operation_plan_fingerprint"],
        "canonical_memory_version": operation_plan["canonical_memory_version"],
        "mode": operation_plan["mode"],
        "status": operation_plan_status,
        "plan_payload": deepcopy(dict(operation_plan)),
        "attempt_count": 0,
        "lease_owner": None,
        "lease_token": None,
        "lease_generation": 0,
        "lease_expires_at": None,
        "claimed_at": None,
        "started_at": None,
        "heartbeat_at": None,
        "result_fingerprint": None,
        "result_payload": None,
        "result_persisted_at": None,
        "candidate_packet_fingerprint": None,
        "completed_at": None,
        "superseded_at": None,
        "last_error": "",
        "authority": False,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    scan_metadata = {
        "persisted_as": "capture_only",
        "observation_hash": observation.observation_hash,
        "operation_plan": deepcopy(dict(operation_plan)),
        "operation_plan_fingerprint": operation_plan["operation_plan_fingerprint"],
        "analysis_status": operation_plan_status,
        "observation": deepcopy(observation.metadata),
    }
    envelope = {
        "workspace": {
            "id": workspace_id,
            "slug": verified.pre_receipt_snapshot.workspace_slug,
            "name": _workspace_name(verified.pre_receipt_snapshot.workspace_slug),
        },
        "brand": {
            "id": brand_id,
            "workspace_id": workspace_id,
            "canonical_domain": observation.canonical_domain,
            "display_name": observation.brand_name,
            "canonical_url": observation.canonical_url,
            "first_observed_at": observation.observed_at.isoformat(),
            "latest_observed_at": observation.observed_at.isoformat(),
        },
        "scan_run": {
            "id": scan_id,
            "workspace_id": workspace_id,
            "brand_id": brand_id,
            "source_scan_id": observation.source_scan_id,
            "source_run_id": observation.source_run_id or "",
            "status": "completed",
            "pipeline_version": observation.pipeline_version,
            "acquisition_state": observation.acquisition_state,
            "requested_at": observation.observed_at.isoformat(),
            "started_at": observation.observed_at.isoformat(),
            "completed_at": observation.recorded_at.isoformat(),
            "recorded_at": observation.recorded_at.isoformat(),
            "error_summary": "",
            "request_payload": deepcopy(observation.raw_observation),
            "metadata": scan_metadata,
        },
        "operation_plan": plan_row,
        "capture": {
            "id": capture_id,
            "scan_run_id": scan_id,
            "brand_id": brand_id,
            "observed_at": observation.observed_at.isoformat(),
            "recorded_at": observation.recorded_at.isoformat(),
            "source_url": observation.canonical_url,
            "content_hash": observation.capture_hash,
            "acquisition_summary": {
                **deepcopy(observation.acquisition_summary),
                "state": observation.acquisition_state,
            },
            "limitations": list(observation.limitations),
            "raw_payload": deepcopy(observation.capture_payload),
        },
        "evidence_records": evidence_rows,
        "watermark_event": {
            "capture_content_hash": observation.capture_hash,
            "capture_observation_hash": observation.observation_hash,
            "append_origin": "capture_observation_commit",
        },
        "receipts": receipt_rows,
        "evidence_bindings": binding_rows,
    }
    return envelope, receipt_ids, binding_ids, plan_id


def _validate_scan_row(
    row: Mapping[str, Any],
    prepared: _PreparedAcquisition,
    scan_id: str,
    workspace_id: str,
    brand_id: str,
) -> None:
    expected = prepared.envelope["scan_run"]
    for field in (
        "id",
        "workspace_id",
        "brand_id",
        "source_scan_id",
        "source_run_id",
        "status",
        "pipeline_version",
        "acquisition_state",
        "error_summary",
        "request_payload",
        "metadata",
    ):
        if _json_comparable(row[field]) != _json_comparable(expected[field]):
            raise ValueError("scan row diverged")
    if _uuid_text(row["id"]) != scan_id or _uuid_text(row["workspace_id"]) != workspace_id:
        raise ValueError("scan stable identity diverged")
    if _uuid_text(row["brand_id"]) != brand_id:
        raise ValueError("scan brand identity diverged")
    for field in ("requested_at", "started_at", "completed_at"):
        if _database_datetime(row[field]) != _database_datetime(expected[field]):
            raise ValueError("scan timestamp diverged")
    if _database_datetime(row["recorded_at"]) != _database_datetime(
        expected["recorded_at"]
    ):
        raise ValueError("scan recorded_at diverged")


def _validate_capture_row(
    row: Mapping[str, Any],
    prepared: _PreparedAcquisition,
    capture_id: str,
    scan_id: str,
    brand_id: str,
) -> None:
    expected = prepared.envelope["capture"]
    for field in (
        "content_hash",
        "source_url",
        "acquisition_summary",
        "limitations",
        "raw_payload",
    ):
        if _json_comparable(row[field]) != _json_comparable(expected[field]):
            raise ValueError("capture row diverged")
    if (
        _uuid_text(row["id"]) != capture_id
        or _uuid_text(row["scan_run_id"]) != scan_id
        or _uuid_text(row["brand_id"]) != brand_id
    ):
        raise ValueError("capture stable identity diverged")
    for field in ("observed_at", "recorded_at"):
        if _database_datetime(row[field]) != _database_datetime(expected[field]):
            raise ValueError("capture timestamp diverged")


def _validate_plan_row(
    row: Mapping[str, Any],
    prepared: _PreparedAcquisition,
    workspace_id: str,
    brand_id: str,
    scan_id: str,
) -> None:
    expected = prepared.envelope["operation_plan"]
    immutable_fields = {
        "observation_hash",
        "operation_plan_fingerprint",
        "canonical_memory_version",
        "mode",
        "plan_payload",
        "authority",
        "authority_scope",
        "production_runtime_effect",
        "scanner_runtime_effect",
    }
    for field in immutable_fields:
        if _json_comparable(row[field]) != _json_comparable(expected[field]):
            raise ValueError("operation plan immutable content diverged")
    if (
        _uuid_text(row["id"]) != prepared.plan_id
        or _uuid_text(row["workspace_id"]) != workspace_id
        or _uuid_text(row["brand_id"]) != brand_id
        or _uuid_text(row["scan_run_id"]) != scan_id
    ):
        raise ValueError("operation plan stable identity diverged")
    validate_vault_scan_plan(_mapping(row["plan_payload"]))
    status = str(row["status"])
    if status not in {
        "pending",
        "not_required",
        "claimed",
        "running",
        "result_persisted",
        "completed",
        "superseded",
        "failed_retryable",
    }:
        raise ValueError("operation plan lifecycle status is invalid")
    _nonnegative_integer(row["attempt_count"])
    _nonnegative_integer(row["lease_generation"])
    lease_owner = row["lease_owner"]
    if lease_owner is not None and (
        not isinstance(lease_owner, str) or not 1 <= len(lease_owner) <= 200
    ):
        raise ValueError("operation plan lease owner is invalid")
    lease_token = row["lease_token"]
    if lease_token is not None:
        _uuid_text(lease_token)
    lease_expires = _optional_database_datetime(row["lease_expires_at"])
    claimed_at = _optional_database_datetime(row["claimed_at"])
    started_at = _optional_database_datetime(row["started_at"])
    _optional_database_datetime(row["heartbeat_at"])
    has_lease = lease_owner is not None and lease_token is not None and lease_expires is not None
    if (status in {"claimed", "running"}) != has_lease:
        raise ValueError("operation plan lease lifecycle diverged")
    if status in {"claimed", "running"} and claimed_at is None:
        raise ValueError("operation plan claimed time is missing")
    if status == "running" and started_at is None:
        raise ValueError("operation plan started time is missing")
    result_fingerprint = _optional_sha256(row["result_fingerprint"])
    result_payload = row["result_payload"]
    result_persisted = _optional_database_datetime(row["result_persisted_at"])
    if (result_fingerprint is None) != (result_payload is None) or (
        result_payload is None
    ) != (result_persisted is None):
        raise ValueError("operation plan result lifecycle diverged")
    if result_payload is not None:
        _mapping(result_payload)
    candidate_fingerprint = _optional_sha256(row["candidate_packet_fingerprint"])
    completed_at = _optional_database_datetime(row["completed_at"])
    superseded_at = _optional_database_datetime(row["superseded_at"])
    if status in {"result_persisted", "completed"} and result_payload is None:
        raise ValueError("operation plan result is missing")
    if status not in {"result_persisted", "completed", "superseded"} and result_payload is not None:
        raise ValueError("operation plan result is premature")
    if status == "completed" and completed_at is None:
        raise ValueError("operation plan completion is missing")
    if (status == "superseded") != (superseded_at is not None):
        raise ValueError("operation plan supersession diverged")
    if candidate_fingerprint is not None and result_payload is None:
        raise ValueError("operation plan candidate lacks a result")
    last_error = row["last_error"]
    if not isinstance(last_error, str) or len(last_error) > 4000:
        raise ValueError("operation plan error field is invalid")
    created_at = _database_datetime(row["created_at"])
    updated_at = _database_datetime(row["updated_at"])
    if updated_at < created_at:
        raise ValueError("operation plan update precedes creation")


def _validate_evidence_rows(
    rows: Sequence[Mapping[str, Any]],
    prepared: _PreparedAcquisition,
) -> None:
    expected = sorted(prepared.envelope["evidence_records"], key=lambda row: row["evidence_ref"])
    actual = sorted(rows, key=lambda row: str(row["evidence_ref"]))
    for durable, supplied in zip(actual, expected, strict=True):
        for field in _EVIDENCE_FIELDS:
            if _json_comparable(durable[field]) != _json_comparable(supplied[field]):
                raise ValueError("durable evidence row diverged")


def _validate_watermark(
    row: Mapping[str, Any],
    *,
    prepared: _PreparedAcquisition,
    brand_id: str,
    capture_id: str,
) -> None:
    sequence = _positive_integer(row["capture_sequence"])
    previous_id = _optional_uuid_text(row["previous_event_id"])
    previous_fingerprint = row["previous_event_fingerprint"]
    if (sequence == 1) != (previous_id is None and previous_fingerprint is None):
        raise ValueError("watermark predecessor shape diverged")
    identity = {
        "schema_version": _WATERMARK_VERSION,
        "brand_id": brand_id,
        "capture_id": capture_id,
        "capture_sequence": sequence,
        "previous_event_id": previous_id,
        "previous_event_fingerprint": previous_fingerprint,
        "capture_content_hash": prepared.observation.capture_hash,
        "capture_observation_hash": prepared.observation.observation_hash,
        "append_origin": "capture_observation_commit",
    }
    fingerprint = canonical_fingerprint(_WATERMARK_VERSION, identity)
    expected_id = str(UUID(fingerprint[:32]))
    if (
        _uuid_text(row["id"]) != expected_id
        or _uuid_text(row["brand_id"]) != brand_id
        or _uuid_text(row["capture_id"]) != capture_id
        or row["capture_content_hash"] != identity["capture_content_hash"]
        or row["capture_observation_hash"] != identity["capture_observation_hash"]
        or row["append_origin"] != identity["append_origin"]
        or row["event_schema_version"] != _WATERMARK_VERSION
        or row["event_fingerprint"] != fingerprint
        or row["authority"] is not False
        or row["production_runtime_effect"] is not False
        or row["scanner_runtime_effect"] is not False
    ):
        raise ValueError("watermark row diverged")
    _database_datetime(row["created_at"])


def _validate_receipt_and_binding_rows(
    receipt_rows: Sequence[Mapping[str, Any]],
    binding_rows: Sequence[Mapping[str, Any]],
    *,
    evidence_rows: Sequence[Mapping[str, Any]],
    prepared: _PreparedAcquisition,
    watermark: Mapping[str, Any],
    database_time: datetime,
    registry: PublicKeyRegistry,
) -> list[DurableReceiptReadback]:
    expected_receipts = {
        str(row["channel_role"]): row for row in prepared.envelope["receipts"]
    }
    expected_bindings = {
        str(row["channel_role"]): row
        for row in prepared.envelope["evidence_bindings"]
    }
    durable_receipts = {str(row["channel_role"]): row for row in receipt_rows}
    durable_bindings = {str(row["channel_role"]): row for row in binding_rows}
    durable_evidence = {str(row["id"]): row for row in evidence_rows}
    if len(durable_evidence) != len(evidence_rows):
        raise ValueError("durable evidence identity set is not unique")
    if (
        set(durable_receipts) != set(expected_receipts)
        or set(durable_bindings) != set(expected_bindings)
        or len(durable_receipts) != len(receipt_rows)
        or len(durable_bindings) != len(binding_rows)
    ):
        raise ValueError("durable role set diverged")

    arrivals: list[DurableReceiptReadback] = []
    verified_for_time: list[Any] = []
    received_for_time: list[datetime] = []
    for role, supplied in expected_receipts.items():
        durable = durable_receipts[role]
        for field in set(supplied) - {
            "watermark_event_id",
            "capture_sequence",
            "fetched_at",
            "eligible_until",
        }:
            if _json_comparable(durable[field]) != _json_comparable(supplied[field]):
                raise ValueError("durable receipt row diverged")
        if (
            _uuid_text(durable["watermark_event_id"]) != _uuid_text(watermark["id"])
            or _positive_integer(durable["capture_sequence"])
            != _positive_integer(watermark["capture_sequence"])
        ):
            raise ValueError("receipt watermark binding diverged")
        received_at = _database_datetime(durable["received_at"])
        fetched_at = _database_datetime(durable["fetched_at"])
        if fetched_at != _database_datetime(supplied["fetched_at"]):
            raise ValueError("receipt fetched_at diverged")
        eligible_until = _database_datetime(durable["eligible_until"])
        _database_datetime(durable["created_at"])
        expected_eligible = min(fetched_at, received_at) + timedelta(hours=24)
        if eligible_until != expected_eligible or received_at > database_time:
            raise ValueError("receipt database time derivation diverged")
        signed_receipt = next(
            receipt
            for receipt in prepared.verified.receipts
            if receipt.claims.channel_role == role
        )
        verified_receipt = verify_raw_acquisition_receipt(
            signed_receipt,
            public_key_registry=registry,
            database_received_at=received_at,
        )
        if verified_receipt != signed_receipt:
            raise ValueError("receipt changed during durable signature verification")
        if fetched_at > received_at + timedelta(minutes=5):
            raise ValueError("receipt future skew diverged")
        if received_at - fetched_at > timedelta(minutes=15):
            raise ValueError("receipt arrival delay diverged")
        verified_for_time.append(verified_receipt)
        received_for_time.append(received_at)
        arrivals.append(
            DurableReceiptReadback(
                receipt_id=_uuid_text(durable["id"]),
                receipt_fingerprint=str(durable["receipt_fingerprint"]),
                received_at=received_at,
            )
        )

        binding = durable_bindings[role]
        expected_binding = expected_bindings[role]
        for field in set(expected_binding):
            if _json_comparable(binding[field]) != _json_comparable(expected_binding[field]):
                raise ValueError("durable raw evidence binding diverged")
        _database_datetime(binding["created_at"])
        evidence_id = _uuid_text(binding["evidence_record_id"])
        evidence = durable_evidence.get(evidence_id)
        if evidence is None:
            raise ValueError("durable passage evidence row is absent")
        evidence_content = str(evidence["content"])
        if hashlib.sha256(evidence_content.encode("utf-8")).hexdigest() != str(
            binding["evidence_record_content_hash"]
        ):
            raise ValueError("durable passage evidence hash diverged")
        passage = reproduce_passage_locator(
            extracted_document=str(binding["extracted_document"]),
            passage_locator=_mapping(binding["passage_locator"]),
            durable_evidence_record_content=evidence_content,
        )
        if (
            passage.passage_text != binding["passage_text"]
            or passage.passage_sha256 != binding["passage_sha256"]
        ):
            raise ValueError("durable evidence passage diverged")

    if len(verified_for_time) == 2:
        validate_c7_receipt_time_policy(
            verified_for_time,
            database_received_at=received_for_time,
            database_time=database_time,
        )
    arrivals.sort(key=lambda row: row.receipt_fingerprint)
    return arrivals


def _validate_append_result(
    value: Any,
    projection: Mapping[str, Any],
    prepared: _PreparedAcquisition,
) -> None:
    result = _mapping(
        value,
        fields={
            "capture_id",
            "operation_plan_id",
            "operation_plan_fingerprint",
            "watermark_event_id",
            "capture_sequence",
            "previous_event_id",
            "previous_event_fingerprint",
            "watermark_event_fingerprint",
            "receipt_ids",
            "evidence_binding_ids",
        },
    )
    watermark = _mapping(projection["watermark_event"], fields=_WATERMARK_FIELDS)
    if (
        _uuid_text(result["capture_id"]) != prepared.envelope["capture"]["id"]
        or _uuid_text(result["operation_plan_id"]) != prepared.plan_id
        or result["operation_plan_fingerprint"]
        != prepared.envelope["operation_plan"]["operation_plan_fingerprint"]
        or _uuid_text(result["watermark_event_id"]) != _uuid_text(watermark["id"])
        or _positive_integer(result["capture_sequence"])
        != _positive_integer(watermark["capture_sequence"])
        or _optional_uuid_text(result["previous_event_id"])
        != _optional_uuid_text(watermark["previous_event_id"])
        or result["previous_event_fingerprint"] != watermark["previous_event_fingerprint"]
        or result["watermark_event_fingerprint"] != watermark["event_fingerprint"]
        or [_uuid_text(item) for item in result["receipt_ids"]]
        != list(prepared.receipt_ids)
        or [_uuid_text(item) for item in result["evidence_binding_ids"]]
        != list(prepared.binding_ids)
    ):
        raise ValueError("append result diverged from its full readback")


def _reparse_command(command: TrustedAcquisitionCommand) -> TrustedAcquisitionCommand:
    if not isinstance(command, TrustedAcquisitionCommand):
        raise ValueError("command must be the strict worker model")
    return TrustedAcquisitionCommand.model_validate(
        deepcopy(command.model_dump(mode="python", round_trip=True)),
        strict=True,
    )


def _require_verified_command(
    command: TrustedAcquisitionCommand,
    verified: VerifiedRawCapture,
) -> None:
    snapshot = verified.pre_receipt_snapshot
    if (
        snapshot.workspace_slug != command.workspace_slug
        or snapshot.source_scan_id != command.source_scan_id
        or snapshot.canonical_brand_domain != _command_domain(command)
        or snapshot.canonical_brand_url != command.brand_url
    ):
        raise ValueError("verified acquisition does not match its command")


def _command_domain(command: TrustedAcquisitionCommand) -> str:
    return command.brand_url.removeprefix("https://")


def _workspace_name(slug: str) -> str:
    return "B3S" if slug == "b3s" else slug


def _latest_fetched_at_text(verified: VerifiedRawCapture) -> str:
    latest = max(
        (_database_datetime(receipt.claims.fetched_at) for receipt in verified.receipts),
    )
    return latest.isoformat().replace("+00:00", "Z")


def _durable_payload(verified: VerifiedRawCapture) -> dict[str, Any]:
    payload = verified.raw_payload
    payload["evidence_vault_raw_provenance"] = verified.envelope.model_dump(mode="json")
    return payload


def _target_identity_text(value: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError("database target identity is invalid")
    return value


def _optional_target_identity_text(
    value: str | None,
    *,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _target_identity_text(value, maximum=maximum)


def _stable_uuid(*parts: Any) -> UUID:
    return uuid5(_ID_NAMESPACE, ":".join(str(part) for part in parts))


def _mapping(value: Any, *, fields: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("projection member must be an object")
    detached = dict(value)
    if fields is not None and set(detached) != fields:
        raise ValueError("projection member fields diverged")
    return detached


def _mapping_rows(value: Any, *, fields: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("projection rows must be an array")
    return [_mapping(row, fields=fields) for row in value]


def _canonical_detach(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("canonical object is required")
    rendered = canonical_json(deepcopy(dict(value)))
    parsed = json.loads(rendered)
    if not isinstance(parsed, dict):
        raise ValueError("canonical object is required")
    return parsed


def _json_comparable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_comparable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_comparable(item) for item in value]
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def _database_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value == value.strip():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("database timestamp is invalid")
    if parsed.tzinfo is None:
        raise ValueError("database timestamp is naive")
    return parsed.astimezone(timezone.utc)


def _uuid_text(value: Any) -> str:
    parsed = UUID(str(value))
    if parsed.int == 0 or str(parsed) != str(value):
        raise ValueError("database UUID is not canonical non-zero text")
    return str(parsed)


def _optional_uuid_text(value: Any) -> str | None:
    return None if value is None else _uuid_text(value)


def _nonnegative_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("nonnegative integer is invalid")
    return value


def _optional_database_datetime(value: Any) -> datetime | None:
    return None if value is None else _database_datetime(value)


def _optional_sha256(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("optional sha256 is invalid")
    return value


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("positive integer is invalid")
    parsed = int(value)
    if parsed <= 0 or str(parsed) != str(value):
        raise ValueError("positive integer is invalid")
    return parsed


def _single_result(row: Any, field: str) -> Any:
    if not isinstance(row, Mapping) or set(row) != {field} or row[field] is None:
        raise ValueError("database function result is malformed")
    return row[field]


__all__ = [
    "EvidenceVaultRawRepository",
    "EvidenceVaultRawRepositoryError",
    "OperationPlanBuilder",
    "PostgresEvidenceVaultRawRepository",
]
