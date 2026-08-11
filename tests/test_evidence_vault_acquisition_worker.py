from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from threading import Event, Thread
from typing import Any, Mapping

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from src.services.evidence_vault_canonical_core import canonical_json
from src.services.evidence_vault_raw_capture import (
    DETERMINISTIC_EXTRACTOR_VERSION,
    build_signed_raw_capture,
)
from src.services.evidence_vault_raw_provenance import (
    C7_LIVE_FRESHNESS_POLICY_VERSION,
    PRE_RECEIPT_SNAPSHOT_VERSION,
    PUBLIC_KEY_REGISTRY_VERSION,
    RAW_ACQUISITION_RECEIPT_VERSION,
    PreReceiptSnapshot,
    PublicKeyRegistry,
    RawAcquisitionReceipt,
    evidence_memory_source_identity_id,
    external_identity_provenance_fingerprint,
    pre_receipt_snapshot_sha256,
    receipt_set_fingerprint,
    sign_raw_acquisition_receipt,
)
from src.services.evidence_vault_acquisition_worker import (
    SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION,
    DurableAcquisitionReadback,
    EvidenceVaultAcquisitionReplayConflictError,
    EvidenceVaultAcquisitionWorkerError,
    SignedAcquisition,
    SignedAcquisitionResultEnvelope,
    TrustedAcquisitionCommand,
    TrustedAcquisitionWorker,
)


SESSION_ID = "12345678-1234-4234-8234-123456789abc"
OWNED_NONCE = "22345678-1234-4234-8234-123456789abc"
EXTERNAL_NONCE = "32345678-1234-4234-8234-123456789abc"
CAPTURE_ID = "42345678-1234-4234-8234-123456789abc"
RECEIPT_IDS = (
    "52345678-1234-4234-8234-123456789abc",
    "62345678-1234-4234-8234-123456789abc",
)
KEY_ID = "acquisition-2026-01"
FETCHED_OWNED = "2026-06-01T12:00:00Z"
FETCHED_EXTERNAL = "2026-06-01T12:02:00Z"
RECEIVED_OWNED = datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc)
RECEIVED_EXTERNAL = datetime(2026, 6, 1, 12, 3, tzinfo=timezone.utc)
DATABASE_TIME = datetime(2026, 6, 1, 12, 4, tzinfo=timezone.utc)


def _command_payload(**extra: Any) -> dict[str, Any]:
    return {
        "workspace_slug": "b3s",
        "source_scan_id": "scan-70",
        "brand_url": "https://example.com",
        **extra,
    }


def _key(seed_offset: int = 0) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes((index + seed_offset) % 256 for index in range(32)))


def _public_key_base64(key: Ed25519PrivateKey) -> str:
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(public).decode("ascii")


def _registry(
    key: Ed25519PrivateKey,
    *,
    key_id: str = KEY_ID,
    signing_not_before: str = "2026-01-01T00:00:00Z",
) -> dict[str, Any]:
    return {
        "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
        "current_key_id": key_id,
        "keys": {
            key_id: {
                "version": 1,
                "status": "current",
                "public_key_base64": _public_key_base64(key),
                "signing_not_before": signing_not_before,
                "signing_ended_at": None,
            }
        },
    }


def _raw_payload() -> dict[str, Any]:
    return {
        "sources": {
            "owned": {
                "url": "https://example.com/about",
                "linkedin": "https://www.linkedin.com/company/example",
                "markdown_content": "  # Café Example\n\nDurable owned proof.  ",
                "authorization": "raw-internal-value-must-not-be-public",
            },
            "external": {
                "url": "https://www.linkedin.com/company/example",
                "title": " Example   Company ",
                "summary": " Durable\n profile ",
                "highlights": [" First  highlight ", "", "Second\nline"],
                "text": " Final\tproof ",
                "cookie": "raw-internal-value-must-not-be-public",
            },
        }
    }


def _snapshot(
    *,
    workspace_slug: str = "b3s",
    source_scan_id: str = "scan-70",
    canonical_brand_domain: str = "example.com",
    canonical_brand_url: str = "https://example.com",
    raw_payload: dict[str, Any] | None = None,
) -> PreReceiptSnapshot:
    return PreReceiptSnapshot(
        schema_version=PRE_RECEIPT_SNAPSHOT_VERSION,
        workspace_slug=workspace_slug,
        source_scan_id=source_scan_id,
        acquisition_session_id=SESSION_ID,
        canonical_brand_domain=canonical_brand_domain,
        canonical_brand_url=canonical_brand_url,
        raw_payload=deepcopy(raw_payload if raw_payload is not None else _raw_payload()),
    )


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _owned_document(snapshot: PreReceiptSnapshot) -> str:
    return str(snapshot.raw_payload["sources"]["owned"]["markdown_content"]).strip()


def _external_document(snapshot: PreReceiptSnapshot) -> str:
    fragment = snapshot.raw_payload["sources"]["external"]
    title = str(fragment.get("title") or "").strip()
    summary = str(fragment.get("summary") or "").strip()
    text = str(fragment.get("text") or "").strip()
    highlights = " ".join(
        str(item)
        for item in fragment["highlights"]
        if str(item).strip()
    )
    return " ".join(
        " ".join(part.split())
        for part in (title, summary, highlights, text)
        if part
    ).strip()


def _claims(
    snapshot: PreReceiptSnapshot,
    role: str,
    *,
    key_id: str = KEY_ID,
    nonce: str | None = None,
    external_provenance_fingerprint: str | None = None,
) -> dict[str, Any]:
    common = {
        "schema_version": RAW_ACQUISITION_RECEIPT_VERSION,
        "freshness_policy_version": C7_LIVE_FRESHNESS_POLICY_VERSION,
        "key_id": key_id,
        "acquisition_session_id": snapshot.acquisition_session_id,
        "workspace_slug": snapshot.workspace_slug,
        "source_scan_id": snapshot.source_scan_id,
        "canonical_brand_domain": snapshot.canonical_brand_domain,
        "pre_receipt_snapshot_sha256": pre_receipt_snapshot_sha256(snapshot),
        "status_code": 200,
        "extractor_version": DETERMINISTIC_EXTRACTOR_VERSION,
    }
    if role == "owned_web":
        fragment = snapshot.raw_payload["sources"]["owned"]
        return {
            **common,
            "receipt_nonce": nonce or OWNED_NONCE,
            "channel_role": "owned_web",
            "provider": "direct_http",
            "acquisition": {
                "acquisition_mode": "direct_http",
                "requested_url": "https://example.com/about",
                "redirect_chain": [],
                "final_url": "https://example.com/about",
            },
            "fetched_at": FETCHED_OWNED,
            "selected_headers": {"content-type": "text/html; charset=utf-8"},
            "media_type": "text/html",
            "byte_count": 123,
            "raw_fragment_json_pointer": "/sources/owned",
            "raw_fragment_sha256": _sha_json(fragment),
            "extracted_document_sha256": _sha_text(_owned_document(snapshot)),
            "external_identity_provenance_fingerprint": None,
        }
    fragment = snapshot.raw_payload["sources"]["external"]
    return {
        **common,
        "receipt_nonce": nonce or EXTERNAL_NONCE,
        "channel_role": "external_social_profile",
        "provider": "exa",
        "acquisition": {
            "acquisition_mode": "provider_api",
            "provider_request_fingerprint": "c" * 64,
            "result_ordinal": 0,
            "reported_source_url": "https://www.linkedin.com/company/example",
            "redirect_chain": [],
        },
        "fetched_at": FETCHED_EXTERNAL,
        "selected_headers": {"content-type": "application/json"},
        "media_type": "application/json",
        "byte_count": 456,
        "raw_fragment_json_pointer": "/sources/external",
        "raw_fragment_sha256": _sha_json(fragment),
        "extracted_document_sha256": _sha_text(_external_document(snapshot)),
        "external_identity_provenance_fingerprint": external_provenance_fingerprint,
    }


def _receipt(
    snapshot: PreReceiptSnapshot,
    role: str,
    *,
    key: Ed25519PrivateKey,
    signing_registry: Mapping[str, Any],
    key_id: str = KEY_ID,
    nonce: str | None = None,
    external_provenance_fingerprint: str | None = None,
) -> RawAcquisitionReceipt:
    return sign_raw_acquisition_receipt(
        _claims(
            snapshot,
            role,
            key_id=key_id,
            nonce=nonce,
            external_provenance_fingerprint=external_provenance_fingerprint,
        ),
        private_key=key,
        public_key_registry=signing_registry,
    )


def _association(
    snapshot: PreReceiptSnapshot,
    owned_receipt: RawAcquisitionReceipt,
) -> dict[str, Any]:
    external_url = "https://www.linkedin.com/company/example"
    owned_url = snapshot.canonical_brand_url
    return {
        "schema_version": "external-identity-provenance-v1",
        "policy_version": "evidence-vault-external-identity-association-policy-v1",
        "association_method": "owned_raw_links_external_profile",
        "canonical_brand_domain": snapshot.canonical_brand_domain,
        "owned_source_url": owned_url,
        "external_source_url": external_url,
        "proof_receipt_fingerprint": owned_receipt.receipt_fingerprint,
        "raw_fact_role": "owned_web",
        "raw_fact_json_pointer": "/sources/owned/linkedin",
        "raw_fact_sha256": _sha_json(external_url),
        "source_identity_schema_version": "evidence-memory-document-v2",
        "owned_source_identity_id": evidence_memory_source_identity_id(
            source_url=owned_url,
            raw_fact_role="owned_web",
        ),
        "external_source_identity_id": evidence_memory_source_identity_id(
            source_url=external_url,
            raw_fact_role="external_social_profile",
        ),
    }


def _bundle(
    *,
    roles: tuple[str, ...] = ("owned_web", "external_social_profile"),
    key: Ed25519PrivateKey | None = None,
    registry: Mapping[str, Any] | None = None,
    snapshot: PreReceiptSnapshot | None = None,
    owned_nonce: str | None = None,
) -> dict[str, Any]:
    signing_key = key or _key()
    signing_registry = deepcopy(dict(registry or _registry(signing_key)))
    frozen_snapshot = snapshot or _snapshot()
    receipts: list[RawAcquisitionReceipt] = []
    owned: RawAcquisitionReceipt | None = None
    if "owned_web" in roles:
        owned = _receipt(
            frozen_snapshot,
            "owned_web",
            key=signing_key,
            signing_registry=signing_registry,
            key_id=signing_registry["current_key_id"],
            nonce=owned_nonce,
        )
        receipts.append(owned)
    association = (
        _association(frozen_snapshot, owned)
        if "external_social_profile" in roles and owned is not None
        else None
    )
    if "external_social_profile" in roles:
        if association is None:
            raise ValueError("external test fixture requires owned association proof")
        receipts.append(
            _receipt(
                frozen_snapshot,
                "external_social_profile",
                key=signing_key,
                signing_registry=signing_registry,
                key_id=signing_registry["current_key_id"],
                external_provenance_fingerprint=(
                    external_identity_provenance_fingerprint(association)
                ),
            )
        )
    receipts.sort(key=lambda receipt: receipt.receipt_fingerprint)
    signed = {
        "pre_receipt_snapshot": frozen_snapshot.model_dump(mode="json"),
        "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
        "receipt_set_fingerprint": receipt_set_fingerprint(receipts),
        "external_identity_provenance": association,
    }
    return {
        "key": signing_key,
        "registry": signing_registry,
        "snapshot": frozen_snapshot,
        "receipts": receipts,
        "signed": signed,
    }


def _readback(
    bundle_or_signed: Mapping[str, Any] | SignedAcquisition,
    *,
    registry: Mapping[str, Any],
    received_at: list[datetime] | None = None,
    database_time: datetime = DATABASE_TIME,
) -> dict[str, Any]:
    if isinstance(bundle_or_signed, SignedAcquisition):
        signed = bundle_or_signed
    elif "signed" in bundle_or_signed:
        signed = SignedAcquisition.model_validate(bundle_or_signed["signed"], strict=True)
    else:
        signed = SignedAcquisition.model_validate(bundle_or_signed, strict=True)
    built = build_signed_raw_capture(
        signed.pre_receipt_snapshot,
        signed.receipts,
        public_key_registry=registry,
        external_identity_provenance=signed.external_identity_provenance,
    )
    arrivals = received_at or (
        [RECEIVED_OWNED]
        if len(signed.receipts) == 1
        else [
            RECEIVED_OWNED if receipt.claims.channel_role == "owned_web" else RECEIVED_EXTERNAL
            for receipt in signed.receipts
        ]
    )
    rows = [
        {
            "receipt_id": RECEIPT_IDS[index],
            "receipt_fingerprint": receipt.receipt_fingerprint,
            "received_at": arrivals[index],
        }
        for index, receipt in enumerate(signed.receipts)
    ]
    return {
        "capture_id": CAPTURE_ID,
        "capture_content_hash": built.capture_content_hash,
        "capture_observation_hash": "f" * 64,
        "durable_raw_capture_payload": deepcopy(built.durable_raw_capture_payload),
        "database_time": database_time,
        "receipt_rows": rows,
    }


def _worker(
    *,
    bundle: Mapping[str, Any] | None = None,
    events: list[str] | None = None,
    sign_value: Any | None = None,
    persist: Any | None = None,
    lookup: Any | None = None,
    registry: Mapping[str, Any] | PublicKeyRegistry | None = None,
) -> TrustedAcquisitionWorker:
    fixture = dict(bundle or _bundle())
    pinned_registry = registry or fixture["registry"]
    observed = events if events is not None else []
    signed_output = sign_value if sign_value is not None else fixture["signed"]
    durable = _readback(fixture, registry=pinned_registry)

    def collect(command: TrustedAcquisitionCommand) -> Mapping[str, Any]:
        observed.append("collect")
        return {"snapshot": {"source_scan_id": command.source_scan_id}}

    def sign(
        command: TrustedAcquisitionCommand,
        collected: Mapping[str, Any],
    ) -> Any:
        assert collected["snapshot"] == {"source_scan_id": command.source_scan_id}
        observed.append("sign")
        return deepcopy(signed_output)

    def default_persist(
        command: TrustedAcquisitionCommand,
        signed: SignedAcquisition,
    ) -> Mapping[str, Any]:
        assert command.source_scan_id == "scan-70"
        assert signed == SignedAcquisition.model_validate(fixture["signed"], strict=True)
        observed.append("persist")
        return deepcopy(durable)

    return TrustedAcquisitionWorker(
        collect=collect,
        sign=sign,
        persist=persist or default_persist,
        lookup=lookup,
        public_key_registry=pinned_registry,
    )


@pytest.mark.parametrize(
    "untrusted_field",
    ["raw_payload", "snapshot", "claims", "signed_payload", "private_key", "ingest_dsn"],
)
def test_command_rejects_every_caller_acquisition_field_before_capabilities_run(
    untrusted_field: str,
) -> None:
    events: list[str] = []
    worker = _worker(events=events)

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^invalid_command$"):
        worker.capture(_command_payload(**{untrusted_field: {"forged": True}}))

    assert events == []


@pytest.mark.parametrize(
    "brand_url",
    [
        "http://example.com",
        "https://www.example.com",
        "https://example.com/",
        "https://example.com/about",
        "https://example.com?query=1",
        "https://example.com?",
        "https://example.com#",
        "https://Example.com",
    ],
)
def test_command_requires_canonical_non_www_https_origin(brand_url: str) -> None:
    with pytest.raises(ValidationError):
        TrustedAcquisitionCommand.model_validate(_command_payload(brand_url=brand_url), strict=True)


def test_command_internal_and_public_models_are_strict_exact_field_models() -> None:
    bundle = _bundle()
    readback = _readback(bundle, registry=bundle["registry"])
    with pytest.raises(ValidationError):
        TrustedAcquisitionCommand.model_validate({**_command_payload(), "claims": {}}, strict=True)
    with pytest.raises(ValidationError):
        SignedAcquisition.model_validate({**bundle["signed"], "private_key": "secret"}, strict=True)
    with pytest.raises(ValidationError):
        DurableAcquisitionReadback.model_validate({**readback, "ingest_dsn": "secret"}, strict=True)
    with pytest.raises(ValidationError):
        SignedAcquisitionResultEnvelope.model_validate(
            {
                "schema_version": SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION,
                **_command_payload(),
                "capture_id": CAPTURE_ID,
                "capture_content_hash": "a" * 64,
                "receipt_set_fingerprint": "b" * 64,
                "receipt_rows": [],
                "documents": [],
                "signed_acquisition": bundle["signed"],
            },
            strict=True,
        )


def test_capture_returns_only_safe_deterministic_documents_after_durable_readback() -> None:
    events: list[str] = []
    worker = _worker(events=events)

    result = worker.capture(_command_payload())
    events.append("returned")
    public = result.model_dump(mode="python")

    assert events == ["collect", "sign", "persist", "returned"]
    assert set(public) == {
        "schema_version",
        "workspace_slug",
        "source_scan_id",
        "brand_url",
        "capture_id",
        "capture_content_hash",
        "capture_observation_hash",
        "receipt_set_fingerprint",
        "receipt_rows",
        "documents",
    }
    assert public["schema_version"] == SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION
    assert public["workspace_slug"] == "b3s"
    assert public["source_scan_id"] == "scan-70"
    assert public["brand_url"] == "https://example.com"
    assert public["capture_id"] == CAPTURE_ID
    assert len(public["receipt_rows"]) == len(public["documents"]) == 2
    assert all(
        set(row) == {"receipt_id", "receipt_fingerprint", "received_at"}
        for row in public["receipt_rows"]
    )
    assert all(
        set(document)
        == {
            "role",
            "source_url",
            "extracted_document",
            "extracted_document_sha256",
            "extractor_version",
            "receipt_fingerprint",
        }
        for document in public["documents"]
    )
    rendered = result.model_dump_json()
    for forbidden in (
        "signed_acquisition",
        "pre_receipt_snapshot",
        "durable_raw_capture_payload",
        "selected_headers",
        "authorization",
        "cookie",
        "private_key",
        "database_time",
    ):
        assert forbidden not in rendered
    documents_by_role = {document.role: document for document in result.documents}
    assert documents_by_role["owned_web"].source_url == "https://example.com/about"
    assert documents_by_role["owned_web"].extracted_document == "# Café Example\n\nDurable owned proof."
    assert (
        documents_by_role["external_social_profile"].extracted_document
        == "Example Company Durable profile First highlight Second line Final proof"
    )


@pytest.mark.parametrize("failed_stage", ["collect", "sign", "persist"])
def test_stage_failures_are_generic_and_return_no_partial_result(failed_stage: str) -> None:
    bundle = _bundle()
    events: list[str] = []
    results: list[SignedAcquisitionResultEnvelope] = []

    def collect(_command: TrustedAcquisitionCommand) -> Mapping[str, Any]:
        events.append("collect")
        if failed_stage == "collect":
            raise RuntimeError("provider error containing a secret")
        return {"snapshot": {}}

    def sign(_command: TrustedAcquisitionCommand, _collected: Mapping[str, Any]) -> Mapping[str, Any]:
        events.append("sign")
        if failed_stage == "sign":
            raise RuntimeError("private-key-material")
        return deepcopy(bundle["signed"])

    def persist(_command: TrustedAcquisitionCommand, _signed: SignedAcquisition) -> Mapping[str, Any]:
        events.append("persist")
        if failed_stage == "persist":
            raise RuntimeError("postgresql://scanner-ingest-secret")
        return _readback(bundle, registry=bundle["registry"])

    worker = TrustedAcquisitionWorker(
        collect=collect,
        sign=sign,
        persist=persist,
        public_key_registry=bundle["registry"],
    )
    with pytest.raises(EvidenceVaultAcquisitionWorkerError) as exc_info:
        results.append(worker.capture(_command_payload()))

    assert results == []
    assert str(exc_info.value) == {
        "collect": "collection_failed",
        "sign": "signing_failed",
        "persist": "persistence_failed",
    }[failed_stage]
    assert "secret" not in str(exc_info.value)
    assert events == {
        "collect": ["collect"],
        "sign": ["collect", "sign"],
        "persist": ["collect", "sign", "persist"],
    }[failed_stage]


def test_sign_output_requires_real_ed25519_verification() -> None:
    bundle = _bundle()
    forged = deepcopy(bundle["signed"])
    forged["receipts"][0]["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    events: list[str] = []
    worker = _worker(bundle=bundle, sign_value=forged, events=events)

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^signing_failed$"):
        worker.capture(_command_payload())
    assert events == ["collect", "sign"]


def test_cross_command_signed_acquisition_is_rejected_before_persistence() -> None:
    other_snapshot = _snapshot(source_scan_id="scan-71")
    other = _bundle(snapshot=other_snapshot)
    events: list[str] = []
    worker = _worker(bundle=_bundle(), sign_value=other["signed"], events=events)

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^signing_failed$"):
        worker.capture(_command_payload())
    assert events == ["collect", "sign"]


def test_signed_acquisition_rejects_unsorted_or_wrong_receipt_set_and_model_copy_bypass() -> None:
    bundle = _bundle()
    reversed_receipts = list(reversed(bundle["signed"]["receipts"]))
    with pytest.raises(ValidationError):
        SignedAcquisition.model_validate({**bundle["signed"], "receipts": reversed_receipts}, strict=True)
    with pytest.raises(ValidationError):
        SignedAcquisition.model_validate(
            {**bundle["signed"], "receipt_set_fingerprint": "0" * 64},
            strict=True,
        )

    valid = SignedAcquisition.model_validate(bundle["signed"], strict=True)
    bypassed = valid.model_copy(update={"receipt_set_fingerprint": "0" * 64})
    worker = _worker(bundle=bundle, sign_value=bypassed)
    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^signing_failed$"):
        worker.capture(_command_payload())


@pytest.mark.parametrize("tamper", ["capture_hash", "receipt_row", "database_time", "model_copy"])
def test_persisted_readback_must_be_strict_and_exact(tamper: str) -> None:
    bundle = _bundle()
    readback = _readback(bundle, registry=bundle["registry"])
    if tamper == "capture_hash":
        persisted: Any = {**readback, "capture_content_hash": "0" * 64}
    elif tamper == "receipt_row":
        persisted = deepcopy(readback)
        persisted["receipt_rows"][0]["receipt_fingerprint"] = "f" * 64
    elif tamper == "database_time":
        persisted = {**readback, "database_time": "not-a-database-timestamp"}
    else:
        valid = DurableAcquisitionReadback.model_validate(readback, strict=True)
        persisted = valid.model_copy(update={"capture_content_hash": "0" * 64})

    worker = _worker(bundle=bundle, persist=lambda _command, _signed: persisted)
    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^persistence_failed$"):
        worker.capture(_command_payload())


def test_echo_persister_cannot_satisfy_the_durable_readback_contract() -> None:
    bundle = _bundle()
    worker = _worker(
        bundle=bundle,
        persist=lambda _command, signed: signed.model_dump(mode="python"),
    )

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^persistence_failed$"):
        worker.capture(_command_payload())


def test_durable_lookup_is_fully_verified_before_skipping_collection() -> None:
    bundle = _bundle()
    replay = _readback(bundle, registry=bundle["registry"])
    events: list[str] = []

    def lookup(command: TrustedAcquisitionCommand) -> Mapping[str, Any]:
        events.append(f"lookup:{command.source_scan_id}")
        return deepcopy(replay)

    worker = _worker(bundle=bundle, events=events, lookup=lookup)
    result = worker.capture(_command_payload())

    assert result.capture_id == CAPTURE_ID
    assert events == ["lookup:scan-70"]


def test_invalid_or_cross_command_lookup_fails_before_collection() -> None:
    other_snapshot = _snapshot(source_scan_id="scan-71")
    other = _bundle(snapshot=other_snapshot)
    replay = _readback(other, registry=other["registry"])
    events: list[str] = []
    worker = _worker(
        bundle=_bundle(),
        events=events,
        lookup=lambda _command: deepcopy(replay),
    )

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^replay_lookup_failed$"):
        worker.capture(_command_payload())
    assert events == []


def test_lookup_miss_precedes_normal_collection_signing_and_persistence() -> None:
    events: list[str] = []
    worker = _worker(
        events=events,
        lookup=lambda _command: events.append("lookup") or None,
    )

    worker.capture(_command_payload())
    assert events == ["lookup", "collect", "sign", "persist"]


def test_exact_atomic_persistence_replay_succeeds() -> None:
    bundle = _bundle()
    stored: dict[tuple[str, str], dict[str, Any]] = {}

    def persist(
        command: TrustedAcquisitionCommand,
        signed: SignedAcquisition,
    ) -> Mapping[str, Any]:
        key = (command.workspace_slug, command.source_scan_id)
        candidate = _readback(signed, registry=bundle["registry"])
        stored.setdefault(key, deepcopy(candidate))
        return deepcopy(stored[key])

    worker = _worker(bundle=bundle, persist=persist)
    first = worker.capture(_command_payload())
    second = worker.capture(_command_payload())

    assert second == first
    assert len(stored) == 1


def test_divergent_atomic_replay_fails_closed() -> None:
    first_bundle = _bundle()
    second_bundle = _bundle(owned_nonce="72345678-1234-4234-8234-123456789abc")
    signed_attempts = iter((first_bundle["signed"], second_bundle["signed"]))
    stored: dict[str, Any] = {}

    def sign(_command: TrustedAcquisitionCommand, _collected: Mapping[str, Any]) -> Mapping[str, Any]:
        return deepcopy(next(signed_attempts))

    def persist(_command: TrustedAcquisitionCommand, signed: SignedAcquisition) -> Mapping[str, Any]:
        stored.setdefault("readback", _readback(signed, registry=first_bundle["registry"]))
        return deepcopy(stored["readback"])

    worker = TrustedAcquisitionWorker(
        collect=lambda _command: {},
        sign=sign,
        persist=persist,
        public_key_registry=first_bundle["registry"],
    )
    first = worker.capture(_command_payload())

    with pytest.raises(
        EvidenceVaultAcquisitionReplayConflictError,
        match="^persisted_result_diverged$",
    ):
        worker.capture(_command_payload())

    assert first.receipt_set_fingerprint == first_bundle["signed"]["receipt_set_fingerprint"]


def test_capture_cannot_return_while_durable_persistence_is_incomplete() -> None:
    bundle = _bundle()
    readback = _readback(bundle, registry=bundle["registry"])
    persist_entered = Event()
    allow_persist_to_finish = Event()
    returned = Event()
    results: list[SignedAcquisitionResultEnvelope] = []
    failures: list[BaseException] = []

    def persist(_command: TrustedAcquisitionCommand, _signed: SignedAcquisition) -> Mapping[str, Any]:
        persist_entered.set()
        assert allow_persist_to_finish.wait(timeout=2)
        return deepcopy(readback)

    worker = _worker(bundle=bundle, persist=persist)

    def invoke() -> None:
        try:
            results.append(worker.capture(_command_payload()))
        except BaseException as exc:  # retain the failure for the assertion thread
            failures.append(exc)
        finally:
            returned.set()

    thread = Thread(target=invoke, daemon=True)
    thread.start()
    assert persist_entered.wait(timeout=2)
    assert not returned.is_set()
    assert results == []

    allow_persist_to_finish.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures == []
    assert returned.is_set()
    assert len(results) == 1


def test_constructor_requires_and_detaches_a_public_only_registry() -> None:
    bundle = _bundle()
    registry = deepcopy(bundle["registry"])
    readback = _readback(bundle, registry=registry)
    worker = TrustedAcquisitionWorker(
        collect=lambda _command: {},
        sign=lambda _command, _collected: deepcopy(bundle["signed"]),
        persist=lambda _command, _signed: deepcopy(readback),
        public_key_registry=registry,
    )
    registry["keys"][KEY_ID]["public_key_base64"] = base64.b64encode(b"\x00" * 32).decode("ascii")

    assert worker.capture(_command_payload()).capture_id == CAPTURE_ID

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^invalid_public_key_registry$"):
        TrustedAcquisitionWorker(
            collect=lambda _command: {},
            sign=lambda _command, _collected: {},
            persist=lambda _command, _signed: {},
            public_key_registry={**bundle["registry"], "private_key": "forbidden"},
        )


def test_constructor_reparses_registry_models_to_block_model_copy_bypass() -> None:
    bundle = _bundle()
    registry = PublicKeyRegistry.model_validate(bundle["registry"], strict=True)
    bypassed = registry.model_copy(update={"current_key_id": "missing-key"})

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^invalid_public_key_registry$"):
        TrustedAcquisitionWorker(
            collect=lambda _command: {},
            sign=lambda _command, _collected: {},
            persist=lambda _command, _signed: {},
            public_key_registry=bypassed,
        )


def test_two_member_database_time_policy_is_mandatory() -> None:
    bundle = _bundle()
    late = [
        datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc),
        datetime(2026, 6, 1, 12, 31, tzinfo=timezone.utc),
    ]
    readback = _readback(
        bundle,
        registry=bundle["registry"],
        received_at=late,
        database_time=datetime(2026, 6, 1, 12, 32, tzinfo=timezone.utc),
    )
    worker = _worker(bundle=bundle, persist=lambda _command, _signed: readback)

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^persistence_failed$"):
        worker.capture(_command_payload())


def test_one_member_audit_capture_is_verified_and_safely_projected() -> None:
    bundle = _bundle(roles=("owned_web",))
    worker = _worker(bundle=bundle)

    result = worker.capture(_command_payload())

    assert len(result.documents) == len(result.receipt_rows) == 1
    assert result.documents[0].role == "owned_web"


def test_retired_key_receipt_is_rechecked_against_its_database_arrival() -> None:
    old_key = _key()
    new_key = _key(seed_offset=29)
    old_key_id = "acquisition-2026-old"
    new_key_id = "acquisition-2026-new"
    signing_registry = _registry(old_key, key_id=old_key_id)
    bundle = _bundle(
        roles=("owned_web",),
        key=old_key,
        registry=signing_registry,
    )
    pinned_registry = {
        "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
        "current_key_id": new_key_id,
        "keys": {
            old_key_id: {
                "version": 1,
                "status": "verification_only",
                "public_key_base64": _public_key_base64(old_key),
                "signing_not_before": "2026-01-01T00:00:00Z",
                "signing_ended_at": "2026-06-01T12:00:30Z",
            },
            new_key_id: {
                "version": 2,
                "status": "current",
                "public_key_base64": _public_key_base64(new_key),
                "signing_not_before": "2026-06-01T12:00:31Z",
                "signing_ended_at": None,
            },
        },
    }
    too_late = datetime(2026, 6, 1, 12, 15, 31, tzinfo=timezone.utc)
    readback = _readback(
        bundle,
        registry=pinned_registry,
        received_at=[too_late],
        database_time=too_late + timedelta(minutes=1),
    )
    worker = _worker(
        bundle=bundle,
        registry=pinned_registry,
        persist=lambda _command, _signed: readback,
    )

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^persistence_failed$"):
        worker.capture(_command_payload())


def test_preconstructed_command_is_detached_and_revalidated() -> None:
    command = TrustedAcquisitionCommand.model_validate(_command_payload(), strict=True)
    bypassed = command.model_copy(update={"workspace_slug": "B3S"})
    worker = _worker()

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^invalid_command$"):
        worker.capture(bypassed)
