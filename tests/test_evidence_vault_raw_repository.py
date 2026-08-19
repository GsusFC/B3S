from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import os
import runpy
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

import src.history.evidence_vault_raw_repository as raw_repository
from src.history.evidence_vault_raw_repository import (
    EvidenceVaultRawRepository,
    EvidenceVaultRawRepositoryError,
)
from src.services.evidence_vault_acquisition_worker import (
    DurableAcquisitionReadback,
    DurableReceiptReadback,
    SignedAcquisition,
    TrustedAcquisitionCommand,
)
from src.services.evidence_vault_canonical_core import canonical_json
from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan
from src.services.evidence_vault_raw_capture import (
    DETERMINISTIC_EXTRACTOR_VERSION,
    build_signed_raw_capture,
    validate_signed_raw_capture,
)
from src.services.evidence_vault_raw_provenance import (
    C7_LIVE_FRESHNESS_POLICY_VERSION,
    PRE_RECEIPT_SNAPSHOT_VERSION,
    PUBLIC_KEY_REGISTRY_VERSION,
    RAW_ACQUISITION_RECEIPT_VERSION,
    DirectAcquisition,
    PreReceiptSnapshot,
    PublicKeyRegistry,
    RawAcquisitionReceiptClaims,
    pre_receipt_snapshot_sha256,
    receipt_set_fingerprint,
    sign_raw_acquisition_receipt,
)


_UNIT_TARGET = {
    "expected_database": "unit_test",
    "expected_neon_project_id": None,
    "expected_neon_branch_id": None,
}


class _Cursor:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _Connection:
    def __init__(
        self,
        *,
        lookup: Any = None,
        planning_context: Any = None,
        fail_append: bool = False,
    ) -> None:
        self.lookup = lookup
        self.planning_context = planning_context
        self.fail_append = fail_append
        self.calls: list[tuple[str, Any]] = []
        self.exit_exception: type[BaseException] | None = None

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        self.exit_exception = exc_type

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        self.calls.append((sql, params))
        if "restricted_login" in sql:
            return _Cursor(
                {
                    "direct_session": True,
                    "restricted_login": True,
                    "database_scope": True,
                    "schema_scope": True,
                    "required_execute": True,
                    "forbidden_execute_absent": True,
                    "table_privileges_absent": True,
                    "column_privileges_absent": True,
                    "sequence_privileges_absent": True,
                    "ownership_absent": True,
                    "memberships_absent": True,
                    "target_identity": True,
                }
            )
        if "read_evidence_vault_raw_planning_context" in sql:
            (
                workspace_slug,
                source_scan_id,
                canonical_domain,
                *_ids,
                semantic_contract_fingerprint,
            ) = params
            value = self.planning_context or {
                "schema_version": "evidence-vault-raw-planning-context-v2",
                "workspace_slug": workspace_slug,
                "source_scan_id": source_scan_id,
                "canonical_domain": canonical_domain,
                "canonical_memory_version": None,
                "previous_capture_evidence_records": [],
                "known_evidence_records": [],
                "semantic_analysis_contract_fingerprint": (
                    semantic_contract_fingerprint
                ),
                "semantic_analysis_claimed_fingerprints": [],
                "accepted_evidence_tile_relations": [],
                "operational_authority_context": {
                    "database_time": "2026-08-09T02:00:00+00:00",
                    "operational_adoption_count": 0,
                    "operational_adoptions": [],
                    "operational_packet_count": 0,
                    "operational_packets": [],
                },
                "existing_operation_plan": None,
            }
            return _Cursor({"planning_context": value})
        if "append_evidence_vault_raw_acquisition" in sql:
            if self.fail_append:
                raise RuntimeError("postgresql://secret@internal")
            return _Cursor({"append_result": {"capture_id": "ignored"}})
        if "read_evidence_vault_raw_acquisition" in sql:
            return _Cursor(
                {
                    "acquisition": self.lookup,
                    "database_time": datetime.now(timezone.utc),
                }
            )
        return _Cursor(None)


class _Connector:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, dsn: str, **kwargs: Any) -> _Connection:
        self.calls.append((dsn, kwargs))
        return self.connection


def _fixture(
    document: str = "Durable raw owned evidence document.",
) -> tuple[
    TrustedAcquisitionCommand,
    SignedAcquisition,
    PublicKeyRegistry,
]:
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    registry = PublicKeyRegistry.model_validate(
        {
            "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
            "current_key_id": "repository-test-v1",
            "keys": {
                "repository-test-v1": {
                    "version": 1,
                    "status": "current",
                    "signing_not_before": "2026-01-01T00:00:00Z",
                    "signing_ended_at": None,
                    "public_key_base64": public_key,
                }
            },
        }
    )
    command = TrustedAcquisitionCommand(
        workspace_slug="b3s",
        source_scan_id="raw-repository-test-scan",
        brand_url="https://example.com",
    )
    raw_payload = {
        "sources": {
            "owned": {
                "url": "https://example.com",
                "content": document,
            }
        },
        "acquisition_outcome": {
            "schema_version": "evidence-vault-acquisition-outcome-v1",
            "external": "owned_only_downgrade",
            "failure_reason": "provider_result_ineligible",
        },
    }
    snapshot = PreReceiptSnapshot(
        schema_version=PRE_RECEIPT_SNAPSHOT_VERSION,
        workspace_slug=command.workspace_slug,
        source_scan_id=command.source_scan_id,
        acquisition_session_id=str(uuid4()),
        canonical_brand_domain="example.com",
        canonical_brand_url=command.brand_url,
        raw_payload=raw_payload,
    )
    fragment = raw_payload["sources"]["owned"]
    claims = RawAcquisitionReceiptClaims(
        schema_version=RAW_ACQUISITION_RECEIPT_VERSION,
        freshness_policy_version=C7_LIVE_FRESHNESS_POLICY_VERSION,
        key_id="repository-test-v1",
        receipt_nonce=str(uuid4()),
        acquisition_session_id=snapshot.acquisition_session_id,
        workspace_slug=command.workspace_slug,
        source_scan_id=command.source_scan_id,
        canonical_brand_domain="example.com",
        channel_role="owned_web",
        pre_receipt_snapshot_sha256=pre_receipt_snapshot_sha256(snapshot),
        provider="direct_http",
        acquisition=DirectAcquisition(
            acquisition_mode="direct_http",
            requested_url=command.brand_url,
            redirect_chain=[],
            final_url=command.brand_url,
        ),
        fetched_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
        status_code=200,
        selected_headers={"content-type": "text/html"},
        media_type="text/html",
        byte_count=128,
        raw_fragment_json_pointer="/sources/owned",
        raw_fragment_sha256=hashlib.sha256(canonical_json(fragment).encode()).hexdigest(),
        extracted_document_sha256=hashlib.sha256(document.encode()).hexdigest(),
        extractor_version=DETERMINISTIC_EXTRACTOR_VERSION,
        external_identity_provenance_fingerprint=None,
    )
    receipt = sign_raw_acquisition_receipt(
        claims,
        private_key=private_key,
        public_key_registry=registry,
    )
    signed = SignedAcquisition(
        pre_receipt_snapshot=snapshot,
        receipts=[receipt],
        receipt_set_fingerprint=receipt_set_fingerprint([receipt]),
        external_identity_provenance=None,
    )
    return command, signed, registry


def _plan(
    command: TrustedAcquisitionCommand,
    evidence: Any,
    _planning_context: raw_repository.EvidenceVaultRawPlanningContext,
) -> dict[str, Any]:
    return build_vault_scan_plan(
        brand_identity="example.com",
        subject_url=command.brand_url,
        mode="baseline",
        current_evidence_records=evidence,
    )



def test_planning_context_is_strict_and_never_accepts_operational_evidence_id_as_fingerprint() -> None:
    command, _signed, _registry = _fixture()
    base = {
        "schema_version": "evidence-vault-raw-planning-context-v2",
        "workspace_slug": command.workspace_slug,
        "source_scan_id": command.source_scan_id,
        "canonical_domain": "example.com",
        "canonical_memory_version": None,
        "previous_capture_evidence_records": [],
        "known_evidence_records": [],
        "semantic_analysis_contract_fingerprint": (
            raw_repository.current_semantic_analysis_contract()[
                "semantic_analysis_contract_fingerprint"
            ]
        ),
        "semantic_analysis_claimed_fingerprints": [],
        "accepted_evidence_tile_relations": [],
        "operational_authority_context": {
            "database_time": "2026-08-09T02:00:00+00:00",
            "operational_adoption_count": 0,
            "operational_adoptions": [],
            "operational_packet_count": 0,
            "operational_packets": [],
        },
        "existing_operation_plan": None,
    }
    parsed = raw_repository._validate_planning_context(base, command=command)
    assert parsed.canonical_memory_version is None

    contaminated = dict(base)
    contaminated["accepted_evidence_tile_relations"] = [
        {"tile_id": "M1", "evidence_id": "b" * 64}
    ]
    with pytest.raises(ValueError, match="fields diverged"):
        raw_repository._validate_planning_context(contaminated, command=command)


def test_contextual_plan_is_bound_to_the_frozen_parent_and_exact_history() -> None:
    command, signed, registry = _fixture("New mission evidence")
    previous = {
        "ref": "previous",
        "source": "owned_web",
        "evidence_type": "text",
        "url": command.brand_url,
        "content": "Old mission evidence",
        "confidence": "high",
        "metadata": {"source_class": "owned_copy"},
    }
    previous_fingerprint = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url=command.brand_url,
        mode="baseline",
        current_evidence_records=[previous],
    )["semantic_context"]["evidence_fingerprints"][0]
    context = raw_repository.EvidenceVaultRawPlanningContext(
        canonical_memory_version="a" * 64,
        previous_capture_evidence_records=(previous,),
        known_evidence_records=(previous,),
        accepted_evidence_tile_relations=(
            {
                "tile_id": "C7",
                "evidence_fingerprint": previous_fingerprint,
                "evidence_locator": None,
            },
        ),
    )
    seen: list[raw_repository.EvidenceVaultRawPlanningContext] = []

    def contextual_plan(plan_command, evidence, planning_context):
        seen.append(planning_context)
        return build_vault_scan_plan(
            brand_identity="example.com",
            subject_url=plan_command.brand_url,
            mode="incremental_refresh",
            current_evidence_records=evidence,
            previous_capture_evidence_records=(
                planning_context.previous_capture_evidence_records
            ),
            known_evidence_records=planning_context.known_evidence_records,
            accepted_evidence_tile_relations=(
                planning_context.accepted_evidence_tile_relations
            ),
            canonical_memory_version=planning_context.canonical_memory_version,
        )

    repository = EvidenceVaultRawRepository(
        "postgresql://scanner-ingest",
        public_key_registry=registry,
        operation_plan_builder=contextual_plan,
        **_UNIT_TARGET,
        connect=_Connector(_Connection()),
    )
    built = build_signed_raw_capture(
        signed.pre_receipt_snapshot,
        signed.receipts,
        public_key_registry=registry,
        external_identity_provenance=None,
    )
    verified = validate_signed_raw_capture(
        built.durable_raw_capture_payload,
        capture_content_hash=built.capture_content_hash,
        public_key_registry=registry,
    )
    prepared = repository._EvidenceVaultRawRepository__prepare(
        command,
        verified,
        planning_context=context,
    )

    assert seen == [context]
    assert prepared.envelope["operation_plan"]["mode"] == "incremental_refresh"
    assert prepared.envelope["operation_plan"]["canonical_memory_version"] == "a" * 64
    assert prepared.envelope["operation_plan"]["plan_payload"]["delta"][
        "modified_evidence_fingerprints"
    ]
    assert prepared.envelope["operation_plan"]["plan_payload"]["delta"][
        "affected_tile_ids"
    ] == ["C7"]


def test_contextual_plan_rejects_a_self_consistent_forged_delta() -> None:
    command, signed, registry = _fixture("New mission evidence")
    previous = {
        "ref": "previous",
        "source": "owned_web",
        "evidence_type": "text",
        "url": command.brand_url,
        "content": "Old mission evidence",
        "confidence": "high",
        "metadata": {"source_class": "owned_copy"},
    }
    context = raw_repository.EvidenceVaultRawPlanningContext(
        canonical_memory_version="a" * 64,
        previous_capture_evidence_records=(previous,),
        known_evidence_records=(previous,),
        accepted_evidence_tile_relations=(),
    )
    built = build_signed_raw_capture(
        signed.pre_receipt_snapshot,
        signed.receipts,
        public_key_registry=registry,
        external_identity_provenance=None,
    )
    verified = validate_signed_raw_capture(
        built.durable_raw_capture_payload,
        capture_content_hash=built.capture_content_hash,
        public_key_registry=registry,
    )
    evidence = raw_repository._deterministic_evidence_records(verified)

    def forged_plan(plan_command, current, planning_context):
        del planning_context
        # This plan is internally valid and bound to the exact current evidence,
        # but falsely calls the changed capture already known and unchanged.
        return build_vault_scan_plan(
            brand_identity="example.com",
            subject_url=plan_command.brand_url,
            mode="incremental_refresh",
            current_evidence_records=current,
            previous_capture_evidence_records=current,
            known_evidence_records=current,
            canonical_memory_version="a" * 64,
        )

    with pytest.raises(ValueError, match="frozen planning context"):
        raw_repository._build_and_validate_plan(
            forged_plan,
            command=command,
            evidence_records=evidence,
            planning_context=context,
        )

def test_repository_prepares_exact_atomic_pre_interpretation_envelope() -> None:
    command, signed, registry = _fixture()
    connection = _Connection()
    repository = EvidenceVaultRawRepository(
        "postgresql://scanner-ingest",
        public_key_registry=registry,
        operation_plan_builder=_plan,
        **_UNIT_TARGET,
        connect=_Connector(connection),
    )
    built = build_signed_raw_capture(
        signed.pre_receipt_snapshot,
        signed.receipts,
        public_key_registry=registry,
        external_identity_provenance=None,
    )
    verified = validate_signed_raw_capture(
        built.durable_raw_capture_payload,
        capture_content_hash=built.capture_content_hash,
        public_key_registry=registry,
    )
    prepared = repository._EvidenceVaultRawRepository__prepare(command, verified)
    assert set(prepared.envelope) == {
        "workspace", "brand", "scan_run", "operation_plan", "capture",
        "evidence_records", "watermark_event", "receipts", "evidence_bindings",
    }
    assert prepared.envelope["capture"]["raw_payload"] == built.durable_raw_capture_payload
    assert prepared.envelope["operation_plan"]["status"] == "pending"
    assert prepared.envelope["scan_run"]["metadata"]["analysis_status"] == "pending"
    assert len(prepared.envelope["receipts"]) == 1
    assert len(prepared.envelope["evidence_records"]) == 1
    assert len(prepared.envelope["evidence_bindings"]) == 1
    assert prepared.observation.limitations == ()
    assert prepared.observation.acquisition_attempts == ()
    assert set(prepared.observation.acquisition_summary) == {
        "receipt_count",
        "receipt_set_fingerprint",
    }
    limitations, attempts = raw_repository._trusted_acquisition_report_metadata(
        prepared.observation.capture_payload,
    )
    assert limitations == [
        "verified_external_document_unavailable",
        "external_acquisition:provider_result_ineligible",
    ]
    assert [attempt["provider"] for attempt in attempts] == ["web", "exa"]
    binding = prepared.envelope["evidence_bindings"][0]
    assert binding["passage_text"] == "Durable raw owned evidence document."
    assert binding["passage_locator"] == {
        "kind": "utf8_byte_range",
        "extracted_start": 0,
        "extracted_end": 36,
        "evidence_start": 0,
        "evidence_end": 36,
    }


def test_trusted_acquisition_report_metadata_marks_external_not_discovered() -> None:
    limitations, attempts = raw_repository._trusted_acquisition_report_metadata(
        {
            "acquisition_outcome": {
                "schema_version": "evidence-vault-acquisition-outcome-v1",
                "external": "not_discovered",
                "failure_reason": None,
            }
        },
        external_captured=False,
    )

    assert limitations == [
        "verified_external_document_unavailable",
        "external_acquisition:not_discovered",
    ]
    assert [attempt["provider"] for attempt in attempts] == ["web"]


def test_trusted_acquisition_report_metadata_accepts_exact_external_capture() -> None:
    limitations, attempts = raw_repository._trusted_acquisition_report_metadata(
        {
            "acquisition_outcome": {
                "schema_version": "evidence-vault-acquisition-outcome-v1",
                "external": "captured",
                "failure_reason": None,
            }
        },
        external_captured=True,
    )

    assert limitations == []
    assert [attempt["provider"] for attempt in attempts] == ["web", "exa"]
    assert all(attempt["status"] == "success" for attempt in attempts)


def test_trusted_acquisition_outcome_absence_has_explicit_legacy_behavior() -> None:
    limitations, attempts = raw_repository._trusted_acquisition_report_metadata(
        {},
        external_captured=False,
    )

    assert limitations == []
    assert [attempt["provider"] for attempt in attempts] == ["web"]
    with pytest.raises(ValueError, match="outcome must be an object"):
        raw_repository._trusted_acquisition_report_metadata(
            {"acquisition_outcome": []},
            external_captured=False,
        )


def test_trusted_acquisition_rejects_embedded_report_gate() -> None:
    with pytest.raises(ValueError, match="may not embed acquisition gate"):
        raw_repository._trusted_acquisition_report_metadata(
            {
                "acquisition_gate": {"state": "pass"},
                "acquisition_outcome": {
                    "schema_version": "evidence-vault-acquisition-outcome-v1",
                    "external": "owned_only_downgrade",
                    "failure_reason": "provider_result_ineligible",
                },
            },
            external_captured=False,
        )


@pytest.mark.parametrize(
    "outcome, error",
    [
        (
            {
                "schema_version": "evidence-vault-acquisition-outcome-v1",
                "external": "captured",
                "failure_reason": None,
            },
            "captured outcome is invalid",
        ),
        (
            {
                "schema_version": "evidence-vault-acquisition-outcome-v1",
                "external": "owned_only_downgrade",
                "failure_reason": "untrusted_reason",
            },
            "failure reason is invalid",
        ),
        (
            {
                "schema_version": "evidence-vault-acquisition-outcome-v1",
                "external": "owned_only_downgrade",
                "failure_reason": None,
            },
            "failure reason is invalid",
        ),
        (
            {
                "schema_version": "evidence-vault-acquisition-outcome-v1",
                "external": "not_discovered",
                "failure_reason": "provider_unavailable",
            },
            "not-discovered outcome is invalid",
        ),
        (
            {
                "schema_version": "wrong",
                "external": "not_discovered",
                "failure_reason": None,
            },
            "outcome schema is invalid",
        ),
        (
            {
                "schema_version": "evidence-vault-acquisition-outcome-v1",
                "external": "not_discovered",
                "failure_reason": None,
                "extra": True,
            },
            "outcome fields are invalid",
        ),
    ],
)
def test_trusted_acquisition_report_metadata_fails_closed(
    outcome: dict[str, Any],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        raw_repository._trusted_acquisition_report_metadata(
            {"acquisition_outcome": outcome},
            external_captured=False,
        )


def test_repository_keeps_large_unicode_document_and_binds_utf8_safe_passage() -> None:
    document = "a" * 19_999 + "é" + "z" * 100
    command, signed, registry = _fixture(document)
    repository = EvidenceVaultRawRepository(
        "postgresql://scanner-ingest",
        public_key_registry=registry,
        operation_plan_builder=_plan,
        **_UNIT_TARGET,
        connect=_Connector(_Connection()),
    )
    built = build_signed_raw_capture(
        signed.pre_receipt_snapshot,
        signed.receipts,
        public_key_registry=registry,
        external_identity_provenance=None,
    )
    verified = validate_signed_raw_capture(
        built.durable_raw_capture_payload,
        capture_content_hash=built.capture_content_hash,
        public_key_registry=registry,
    )
    prepared = repository._EvidenceVaultRawRepository__prepare(command, verified)
    binding = prepared.envelope["evidence_bindings"][0]
    evidence = prepared.envelope["evidence_records"][0]
    assert evidence["content"] == document
    assert binding["extracted_document"] == document
    assert binding["passage_locator"]["extracted_end"] == 19_999
    assert binding["passage_locator"]["evidence_end"] == 19_999
    assert binding["passage_text"] == "a" * 19_999
    assert len(binding["passage_text"].encode("utf-8")) <= 20_000


def test_lookup_is_read_only_uses_only_definer_read_and_null_is_none() -> None:
    command, _signed, registry = _fixture()
    connection = _Connection(lookup=None)
    connector = _Connector(connection)
    repository = EvidenceVaultRawRepository(
        "postgresql://scanner-ingest",
        public_key_registry=registry,
        operation_plan_builder=_plan,
        **_UNIT_TARGET,
        connect=connector,
    )
    assert repository.lookup(command) is None
    assert connection.calls[0] == ("SET TRANSACTION READ ONLY", None)
    assert "restricted_login" in connection.calls[1][0]
    assert "read_evidence_vault_raw_acquisition" in connection.calls[2][0]
    all_sql = "\n".join(sql for sql, _params in connection.calls).lower()
    assert "migration" not in all_sql
    assert "insert " not in all_sql
    assert "update " not in all_sql
    assert "delete " not in all_sql
    assert connector.calls[0][1]["connect_timeout"] == 5





def test_scanner_preflight_allows_exactly_the_three_worker_functions() -> None:
    sql = raw_repository._ROLE_PREFLIGHT_SQL
    signature = (
        "b3s_history.read_evidence_vault_raw_planning_context("
        "text,text,text,uuid,uuid,uuid,text)"
    )
    assert signature in sql
    assert sql.count(signature) == 2
    assert "forbidden_security_definer_execute" in sql
    assert "forbidden_direct_function_grant" in sql

def test_verify_ingest_capability_connects_read_only_and_checks_exact_role() -> None:
    _command, _signed, registry = _fixture()
    connection = _Connection()
    connector = _Connector(connection)
    repository = EvidenceVaultRawRepository(
        "postgresql://scanner-ingest",
        public_key_registry=registry,
        operation_plan_builder=_plan,
        **_UNIT_TARGET,
        connect=connector,
    )

    repository.verify_ingest_capability()

    assert connection.calls[0] == ("SET TRANSACTION READ ONLY", None)
    assert "restricted_login" in connection.calls[1][0]
    assert connector.calls[0][1]["connect_timeout"] == 5


def test_verify_ingest_capability_redacts_connection_or_role_failure() -> None:
    _command, _signed, registry = _fixture()

    def failed_connect(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("postgresql://scanner:do-not-leak@internal/vault")

    repository = EvidenceVaultRawRepository(
        "postgresql://scanner:do-not-leak@internal/vault",
        public_key_registry=registry,
        operation_plan_builder=_plan,
        **_UNIT_TARGET,
        connect=failed_connect,
    )
    with pytest.raises(EvidenceVaultRawRepositoryError) as caught:
        repository.verify_ingest_capability()
    assert str(caught.value) == "raw_acquisition_repository_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "do-not-leak" not in str(caught.value)


def test_persist_calls_append_once_and_returns_only_verified_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    command, signed, registry = _fixture()
    connection = _Connection()
    repository = EvidenceVaultRawRepository(
        "postgresql://scanner-ingest",
        public_key_registry=registry,
        operation_plan_builder=_plan,
        **_UNIT_TARGET,
        connect=_Connector(connection),
    )
    built = build_signed_raw_capture(
        signed.pre_receipt_snapshot,
        signed.receipts,
        public_key_registry=registry,
        external_identity_provenance=None,
    )
    receipt = signed.receipts[0]
    readback = DurableAcquisitionReadback(
        capture_id=str(raw_repository._stable_uuid(
            str(raw_repository._stable_uuid(
                str(raw_repository._stable_uuid("workspace", "b3s")),
                "scan",
                command.source_scan_id,
            )),
            "capture",
        )),
        capture_content_hash=built.capture_content_hash,
        capture_observation_hash="3" * 64,
        durable_raw_capture_payload=built.durable_raw_capture_payload,
        database_time=datetime(2026, 8, 9, 2, 1, tzinfo=timezone.utc),
        receipt_rows=[
            DurableReceiptReadback(
                receipt_id=str(uuid4()),
                receipt_fingerprint=receipt.receipt_fingerprint,
                received_at=datetime(2026, 8, 9, 2, 0, 30, tzinfo=timezone.utc),
            )
        ],
    )
    monkeypatch.setattr(
        EvidenceVaultRawRepository,
        "_EvidenceVaultRawRepository__read_and_validate",
        lambda _self, _connection, *, command, expected=None: (readback, {}),
    )
    monkeypatch.setattr(raw_repository, "_validate_append_result", lambda *_args: None)
    assert repository.persist(command, signed) == readback
    append_calls = [
        sql
        for sql, _params in connection.calls
        if sql.startswith("SELECT b3s_history.append_evidence")
    ]
    assert len(append_calls) == 1
    planning_index = next(
        index
        for index, (sql, _params) in enumerate(connection.calls)
        if "read_evidence_vault_raw_planning_context" in sql
    )
    append_index = next(
        index
        for index, (sql, _params) in enumerate(connection.calls)
        if sql.startswith("SELECT b3s_history.append_evidence")
    )
    assert planning_index < append_index
    assert connection.exit_exception is None


def test_database_failure_is_redacted_and_transaction_receives_exception() -> None:
    command, signed, registry = _fixture()
    connection = _Connection(fail_append=True)
    repository = EvidenceVaultRawRepository(
        "postgresql://scanner-ingest:secret@internal",
        public_key_registry=registry,
        operation_plan_builder=_plan,
        **_UNIT_TARGET,
        connect=_Connector(connection),
    )
    with pytest.raises(EvidenceVaultRawRepositoryError) as caught:
        repository.persist(command, signed)
    assert str(caught.value) == "raw_acquisition_repository_failed"
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert connection.exit_exception is RuntimeError


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_postgres_migration_sanitizes_poisoned_default_scanner_function_acls() -> None:
    import psycopg
    from psycopg import sql

    from src.history.repository import PostgresHistoryRepository

    admin_dsn = os.environ["B3S_TEST_DATABASE_URL"]
    attacker = "b3s_raw_default_acl_attacker_test"
    public_schema_create_was_granted = False
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        public_schema_create_was_granted = bool(
            connection.execute(
                "SELECT has_schema_privilege('public', 'public', 'CREATE')"
            ).fetchone()[0]
        )
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        if connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (attacker,)
        ).fetchone():
            connection.execute(
                sql.SQL("DROP OWNED BY {}").format(sql.Identifier(attacker))
            )
            connection.execute(
                sql.SQL("DROP ROLE {}").format(sql.Identifier(attacker))
            )
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(
            sql.Identifier(attacker)
        ))
        connection.execute(sql.SQL(
            "ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO {}"
        ).format(sql.Identifier(attacker)))
        connection.execute(
            """DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles
                    WHERE rolname = 'b3s_history_vault_provenance_owner'
                ) THEN
                    CREATE ROLE b3s_history_vault_provenance_owner NOLOGIN;
                END IF;
            END $$"""
        )
    try:
        PostgresHistoryRepository(admin_dsn).migrate()
        signatures = (
            "b3s_history.append_evidence_vault_raw_acquisition(jsonb)",
            "b3s_history.read_evidence_vault_raw_acquisition(text,text)",
            "b3s_history.read_evidence_vault_raw_planning_context("
            "text,text,text,uuid,uuid,uuid,text)",
        )
        with psycopg.connect(admin_dsn) as connection:
            assert [
                connection.execute(
                    "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                    (attacker, signature),
                ).fetchone()[0]
                for signature in signatures
            ] == [False, False, False]
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL(
                "ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM {}"
            ).format(sql.Identifier(attacker)))
            connection.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
            if connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (attacker,)
            ).fetchone():
                connection.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(attacker))
                )
                connection.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(attacker))
                )
            if public_schema_create_was_granted:
                connection.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_postgres_scanner_execute_role_persists_looks_up_and_replays() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from src.history.repository import PostgresHistoryRepository

    admin_dsn = os.environ["B3S_TEST_DATABASE_URL"]
    role = "b3s_raw_repository_scanner_test"
    password = "repository-test-password"
    public_schema_create_was_granted = False
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        public_schema_create_was_granted = bool(
            connection.execute(
                "SELECT has_schema_privilege('public', 'public', 'CREATE')"
            ).fetchone()[0]
        )
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        connection.execute(
            """DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles
                    WHERE rolname = 'b3s_history_vault_provenance_owner'
                ) THEN
                    CREATE ROLE b3s_history_vault_provenance_owner NOLOGIN;
                END IF;
            END $$"""
        )
        if connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone():
            connection.execute(
                sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role))
            )
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    PostgresHistoryRepository(admin_dsn).migrate()
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(password),
            )
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA b3s_history TO {}").format(
                sql.Identifier(role)
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION "
                "b3s_history.append_evidence_vault_raw_acquisition(jsonb), "
                "b3s_history.read_evidence_vault_raw_acquisition(text, text), "
                "b3s_history.read_evidence_vault_raw_planning_context("
                "text, text, text, uuid, uuid, uuid, text) TO {}"
            ).format(sql.Identifier(role))
        )
    connection_values = conninfo_to_dict(admin_dsn)
    expected_database = str(connection_values["dbname"])
    connection_values.update(user=role, password=password)
    scanner_dsn = make_conninfo(**connection_values)
    target = {
        "expected_database": expected_database,
        "expected_neon_project_id": None,
        "expected_neon_branch_id": None,
        "expected_role": role,
    }
    command, signed, registry = _fixture()
    planner_calls = 0

    def counted_plan(
        plan_command: TrustedAcquisitionCommand,
        evidence: Any,
        planning_context: raw_repository.EvidenceVaultRawPlanningContext,
    ) -> dict[str, Any]:
        nonlocal planner_calls
        planner_calls += 1
        return _plan(plan_command, evidence, planning_context)

    try:
        repository = EvidenceVaultRawRepository(
            scanner_dsn,
            public_key_registry=registry,
            operation_plan_builder=counted_plan,
            **target,
        )
        wrong_role_repository = EvidenceVaultRawRepository(
            scanner_dsn,
            public_key_registry=registry,
            operation_plan_builder=counted_plan,
            **{**target, "expected_role": "wrong_scanner_role"},
        )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            wrong_role_repository.verify_ingest_capability()
        first = repository.persist(command, signed)
        assert planner_calls == 1
        with psycopg.connect(admin_dsn) as admin:
            admin.execute(
                """UPDATE b3s_history.evidence_vault_operation_plans
                   SET status = 'failed_retryable', last_error = 'test retry',
                       updated_at = clock_timestamp()
                   WHERE scan_run_id = (
                       SELECT id FROM b3s_history.scan_runs
                       WHERE source_scan_id = %s
                   )""",
                (command.source_scan_id,),
            )
        looked_up = repository.lookup(command)
        assert planner_calls == 1
        replayed = repository.persist(command, signed)
        assert planner_calls == 1
        assert looked_up is not None
        assert first.capture_id == looked_up.capture_id == replayed.capture_id
        assert first.capture_content_hash == looked_up.capture_content_hash
        assert first.receipt_rows == looked_up.receipt_rows == replayed.receipt_rows
        assert first.durable_raw_capture_payload == looked_up.durable_raw_capture_payload

        with psycopg.connect(scanner_dsn) as scanner:
            frozen_before = scanner.execute(
                "SELECT b3s_history.read_evidence_vault_raw_acquisition(%s, %s)",
                (command.workspace_slug, command.source_scan_id),
            ).fetchone()[0]
        with psycopg.connect(admin_dsn) as admin:
            admin.execute(
                """UPDATE b3s_history.scan_runs
                   SET pipeline_version = 'b3s-vault-memory-report-v1',
                       acquisition_state = 'warning',
                       completed_at = completed_at + interval '1 second',
                       recorded_at = completed_at + interval '1 second',
                       metadata = metadata || jsonb_build_object(
                           'analysis_status', 'completed',
                           'analysis_result_fingerprint', repeat('a', 64),
                           'candidate_packet_fingerprint', NULL,
                           'persisted_as', 'capture_with_report',
                           'report_hash', repeat('b', 64)
                       )
                   WHERE source_scan_id = %s""",
                (command.source_scan_id,),
            )
        with psycopg.connect(scanner_dsn) as scanner:
            frozen_after = scanner.execute(
                "SELECT b3s_history.read_evidence_vault_raw_acquisition(%s, %s)",
                (command.workspace_slug, command.source_scan_id),
            ).fetchone()[0]
        assert frozen_after["scan_run"] == frozen_before["scan_run"]
        assert frozen_after["scan_run"]["metadata"]["persisted_as"] == "capture_only"
        assert (
            frozen_after["scan_run"]["metadata"]["operation_plan_fingerprint"]
            == frozen_after["operation_plan"]["operation_plan_fingerprint"]
        )
        completed_lookup = repository.lookup(command)
        assert completed_lookup is not None
        assert completed_lookup.capture_id == first.capture_id
        assert completed_lookup.capture_content_hash == first.capture_content_hash
        assert completed_lookup.receipt_rows == first.receipt_rows
        assert planner_calls == 1

        scan_poison_cases = (
            ("status = 'forged'", "status = 'completed'"),
            ("error_summary = 'forged'", "error_summary = ''"),
            (
                "requested_at = requested_at + interval '1 second'",
                "requested_at = requested_at - interval '1 second'",
            ),
            (
                "started_at = started_at + interval '1 second'",
                "started_at = started_at - interval '1 second'",
            ),
            ("source_run_id = 'forged'", "source_run_id = ''"),
            (
                "metadata = metadata || '{\"unexpected\":true}'::jsonb",
                "metadata = metadata - 'unexpected'",
            ),
            (
                "metadata = jsonb_set(metadata, "
                "'{operation_plan_fingerprint}', to_jsonb(repeat('0', 64)))",
                "metadata = jsonb_set(metadata, "
                "'{operation_plan_fingerprint}', "
                "metadata #> '{operation_plan,operation_plan_fingerprint}')",
            ),
        )
        for poison, restore in scan_poison_cases:
            with psycopg.connect(admin_dsn) as admin:
                admin.execute(
                    sql.SQL(
                        "UPDATE b3s_history.scan_runs SET {} "
                        "WHERE source_scan_id = %s"
                    ).format(sql.SQL(poison)),
                    (command.source_scan_id,),
                )
            with pytest.raises(EvidenceVaultRawRepositoryError):
                repository.lookup(command)
            with psycopg.connect(admin_dsn) as admin:
                admin.execute(
                    sql.SQL(
                        "UPDATE b3s_history.scan_runs SET {} "
                        "WHERE source_scan_id = %s"
                    ).format(sql.SQL(restore)),
                    (command.source_scan_id,),
                )
            assert repository.lookup(command) is not None

        scan_trigger = "evidence_vault_scan_parent_immutable"
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("ALTER TABLE b3s_history.scan_runs DISABLE TRIGGER {}")
                .format(sql.Identifier(scan_trigger))
            )
            admin.execute(
                """UPDATE b3s_history.scan_runs
                   SET metadata = jsonb_set(
                       metadata,
                       '{observation_hash}',
                       to_jsonb(repeat('0', 64))
                   )
                   WHERE source_scan_id = %s""",
                (command.source_scan_id,),
            )
            admin.execute(
                sql.SQL("ALTER TABLE b3s_history.scan_runs ENABLE TRIGGER {}")
                .format(sql.Identifier(scan_trigger))
            )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            repository.lookup(command)
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("ALTER TABLE b3s_history.scan_runs DISABLE TRIGGER {}")
                .format(sql.Identifier(scan_trigger))
            )
            admin.execute(
                """UPDATE b3s_history.scan_runs
                   SET metadata = jsonb_set(
                       metadata,
                       '{observation_hash}',
                       to_jsonb(%s::text)
                   )
                   WHERE source_scan_id = %s""",
                (
                    frozen_before["scan_run"]["metadata"]["observation_hash"],
                    command.source_scan_id,
                ),
            )
            admin.execute(
                sql.SQL("ALTER TABLE b3s_history.scan_runs ENABLE TRIGGER {}")
                .format(sql.Identifier(scan_trigger))
            )
        assert repository.lookup(command) is not None

        with pytest.raises(psycopg.Error):
            with psycopg.connect(admin_dsn) as admin:
                admin.execute(
                    """UPDATE b3s_history.scan_runs
                       SET request_payload = jsonb_set(
                           request_payload,
                           '{pipeline_version}',
                           '"forged"'::jsonb
                       )
                       WHERE source_scan_id = %s""",
                    (command.source_scan_id,),
                )
        with pytest.raises(psycopg.Error):
            with psycopg.connect(admin_dsn) as admin:
                admin.execute(
                    """UPDATE b3s_history.evidence_vault_operation_plans
                       SET plan_payload = jsonb_set(
                           plan_payload,
                           '{mode}',
                           '"incremental_refresh"'::jsonb
                       )
                       WHERE scan_run_id = (
                           SELECT id FROM b3s_history.scan_runs
                           WHERE source_scan_id = %s
                       )""",
                    (command.source_scan_id,),
                )
        with pytest.raises(psycopg.Error):
            with psycopg.connect(admin_dsn) as admin:
                admin.execute(
                    """UPDATE b3s_history.captures
                       SET raw_payload = raw_payload || '{"forged":true}'::jsonb
                       WHERE scan_run_id = (
                           SELECT id FROM b3s_history.scan_runs
                           WHERE source_scan_id = %s
                       )""",
                    (command.source_scan_id,),
                )

        worker_fixtures = runpy.run_path(
            "tests/test_evidence_vault_acquisition_worker.py"
        )
        fetched_now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        worker_fixtures["_bundle"].__globals__["FETCHED_OWNED"] = fetched_now
        worker_fixtures["_bundle"].__globals__["FETCHED_EXTERNAL"] = fetched_now
        external_bundle = worker_fixtures["_bundle"]()
        external_command = TrustedAcquisitionCommand(
            workspace_slug="b3s",
            source_scan_id="scan-70",
            brand_url="https://example.com",
        )
        external_signed = SignedAcquisition.model_validate(
            external_bundle["signed"],
            strict=True,
        )
        external_repository = EvidenceVaultRawRepository(
            scanner_dsn,
            public_key_registry=external_bundle["registry"],
            operation_plan_builder=_plan,
            **target,
        )
        external_result = external_repository.persist(
            external_command,
            external_signed,
        )
        external_lookup = external_repository.lookup(external_command)
        assert external_lookup is not None
        assert len(external_result.receipt_rows) == 2
        assert external_result.receipt_rows == external_lookup.receipt_rows
        assert (
            external_result.durable_raw_capture_payload[
                "evidence_vault_raw_provenance"
            ]["external_identity_provenance"]
            == external_signed.external_identity_provenance.model_dump(mode="json")
        )
        repository.verify_ingest_capability()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL(
                    "GRANT SELECT(raw_payload) ON "
                    "b3s_history.captures TO {}"
                ).format(sql.Identifier(role))
            )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            repository.verify_ingest_capability()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL(
                    "REVOKE SELECT(raw_payload) ON "
                    "b3s_history.captures FROM {}"
                ).format(sql.Identifier(role))
            )
            admin.execute(
                "CREATE SEQUENCE b3s_history.scanner_acl_probe"
            )
            admin.execute(
                sql.SQL(
                    "GRANT USAGE ON SEQUENCE "
                    "b3s_history.scanner_acl_probe TO {}"
                ).format(sql.Identifier(role))
            )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            repository.verify_ingest_capability()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("DROP SEQUENCE b3s_history.scanner_acl_probe")
            admin.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION "
                    "b3s_history.read_evidence_vault_c7_shadow_context(text,text) "
                    "TO {}"
                ).format(sql.Identifier(role))
            )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            repository.verify_ingest_capability()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL(
                    "REVOKE EXECUTE ON FUNCTION "
                    "b3s_history.read_evidence_vault_c7_shadow_context(text,text) "
                    "FROM {}"
                ).format(sql.Identifier(role))
            )
            admin.execute(
                "GRANT EXECUTE ON FUNCTION "
                "b3s_history.append_evidence_vault_raw_acquisition(jsonb) "
                "TO PUBLIC"
            )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            repository.verify_ingest_capability()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "REVOKE EXECUTE ON FUNCTION "
                "b3s_history.append_evidence_vault_raw_acquisition(jsonb) "
                "FROM PUBLIC"
            )
            admin.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION "
                    "b3s_history.append_evidence_vault_raw_acquisition(jsonb) "
                    "TO {} WITH GRANT OPTION"
                ).format(sql.Identifier(role))
            )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            repository.verify_ingest_capability()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL(
                    "REVOKE GRANT OPTION FOR EXECUTE ON FUNCTION "
                    "b3s_history.append_evidence_vault_raw_acquisition(jsonb) "
                    "FROM {}"
                ).format(sql.Identifier(role))
            )
            admin.execute(
                sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(expected_database),
                    sql.Identifier(role),
                )
            )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            repository.verify_ingest_capability()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
                    sql.Identifier(expected_database),
                    sql.Identifier(role),
                )
            )
            admin.execute("CREATE TABLE b3s_history.scanner_owned_probe(id integer)")
            admin.execute(
                sql.SQL(
                    "ALTER TABLE b3s_history.scanner_owned_probe OWNER TO {}"
                ).format(sql.Identifier(role))
            )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            repository.verify_ingest_capability()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("DROP TABLE b3s_history.scanner_owned_probe")
        repository.verify_ingest_capability()
        wrong_target_repository = EvidenceVaultRawRepository(
            scanner_dsn,
            public_key_registry=registry,
            operation_plan_builder=_plan,
            expected_database=f"{expected_database}_wrong",
            expected_neon_project_id=None,
            expected_neon_branch_id=None,
        )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            wrong_target_repository.verify_ingest_capability()

        privileged_repository = EvidenceVaultRawRepository(
            admin_dsn,
            public_key_registry=registry,
            operation_plan_builder=_plan,
            **target,
        )
        with pytest.raises(EvidenceVaultRawRepositoryError):
            privileged_repository.lookup(command)
        with psycopg.connect(scanner_dsn) as scanner:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                scanner.execute(
                    "SELECT * FROM b3s_history.evidence_vault_raw_acquisition_receipts"
                )
            scanner.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                scanner.execute("CREATE TABLE b3s_history.forbidden(id integer)")
            scanner.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                scanner.execute("SET ROLE b3s_history_vault_provenance_owner")
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            if connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone():
                connection.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role))
                )
                connection.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                )
            if public_schema_create_was_granted:
                connection.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")
