"""Worker-side handler for trusted Evidence Vault acquisition.

This module defines the handler and its capability contracts. It deliberately does
not implement transport, process isolation, key loading, database access,
environment access, or subprocess management. A deployment must place the handler
behind its own isolation boundary and inject worker-local capabilities.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hmac
import ipaddress
import json
import re
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from src.services.evidence_vault_acquisition_contract import (
    SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION,
    SafeDeterministicDocument,
    SafeReceiptArrival,
    SignedAcquisitionResultEnvelope,
    TrustedAcquisitionClient,
    TrustedAcquisitionCommand,
)
from src.services.evidence_vault_raw_capture import (
    VerifiedRawCapture,
    build_signed_raw_capture,
    extract_deterministic_document,
    validate_signed_raw_capture,
)
from src.services.evidence_vault_raw_provenance import (
    DirectAcquisition,
    ExternalIdentityProvenance,
    PreReceiptSnapshot,
    ProviderApiAcquisition,
    PublicKeyRegistry,
    RawAcquisitionReceipt,
    pre_receipt_snapshot_sha256,
    receipt_set_fingerprint,
    validate_c7_receipt_time_policy,
    verify_raw_acquisition_receipt,
)


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID_128 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class EvidenceVaultAcquisitionWorkerError(RuntimeError):
    """The trusted acquisition handler failed without exposing internal data."""


class EvidenceVaultAcquisitionReplayConflictError(EvidenceVaultAcquisitionWorkerError):
    """A valid durable replay differs from the newly attempted acquisition."""


class _StrictWorkerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SignedAcquisition(_StrictWorkerModel):
    """Strict worker-internal output of the collection signing capability."""

    pre_receipt_snapshot: PreReceiptSnapshot
    receipts: list[RawAcquisitionReceipt] = Field(min_length=1, max_length=2)
    receipt_set_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_identity_provenance: ExternalIdentityProvenance | None

    @model_validator(mode="after")
    def _validate_exact_receipt_group(self) -> "SignedAcquisition":
        fingerprints = [receipt.receipt_fingerprint for receipt in self.receipts]
        if fingerprints != sorted(fingerprints) or len(fingerprints) != len(set(fingerprints)):
            raise ValueError("receipts must be a sorted unique fingerprint set")
        roles = [receipt.claims.channel_role for receipt in self.receipts]
        if len(roles) != len(set(roles)):
            raise ValueError("receipts must have unique roles")
        expected_set = receipt_set_fingerprint(self.receipts)
        if not hmac.compare_digest(self.receipt_set_fingerprint, expected_set):
            raise ValueError("receipt_set_fingerprint does not match receipts")

        snapshot = self.pre_receipt_snapshot
        expected_identity: dict[str, str] = {
            "workspace_slug": snapshot.workspace_slug,
            "source_scan_id": snapshot.source_scan_id,
            "acquisition_session_id": snapshot.acquisition_session_id,
            "canonical_brand_domain": snapshot.canonical_brand_domain,
            "pre_receipt_snapshot_sha256": pre_receipt_snapshot_sha256(snapshot),
        }
        for receipt in self.receipts:
            for field, expected in expected_identity.items():
                if getattr(receipt.claims, field) != expected:
                    raise ValueError(f"receipt {field} does not match the snapshot")
        return self


class DurableReceiptReadback(_StrictWorkerModel):
    """Exact durable identity and immutable database arrival for one receipt row."""

    receipt_id: str
    receipt_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    received_at: AwareDatetime

    @field_validator("receipt_id")
    @classmethod
    def _receipt_id_is_uuid(cls, value: str) -> str:
        return _canonical_uuid(value, field="receipt_id")


class DurableAcquisitionReadback(_StrictWorkerModel):
    """Full worker-internal database readback required after ingest or lookup."""

    capture_id: str
    capture_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    durable_raw_capture_payload: dict[str, JsonValue]
    database_time: AwareDatetime
    receipt_rows: list[DurableReceiptReadback] = Field(min_length=1, max_length=2)

    @field_validator("capture_id")
    @classmethod
    def _capture_id_is_uuid(cls, value: str) -> str:
        return _canonical_uuid(value, field="capture_id")

    @model_validator(mode="after")
    def _rows_are_exact_and_immutable(self) -> "DurableAcquisitionReadback":
        fingerprints = [row.receipt_fingerprint for row in self.receipt_rows]
        if fingerprints != sorted(fingerprints) or len(fingerprints) != len(set(fingerprints)):
            raise ValueError("receipt_rows must be a sorted unique fingerprint set")
        receipt_ids = [row.receipt_id for row in self.receipt_rows]
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("receipt_rows must have unique receipt ids")
        if any(row.received_at > self.database_time for row in self.receipt_rows):
            raise ValueError("database_time precedes a durable receipt row")
        return self


class AcquisitionReplayLookup(Protocol):
    """Worker-local durable replay lookup performed before recollection."""

    def __call__(
        self,
        command: TrustedAcquisitionCommand,
    ) -> DurableAcquisitionReadback | Mapping[str, Any] | None: ...


class AcquisitionCollector(Protocol):
    """Worker-local collection capability; its output never comes from the caller."""

    def __call__(self, command: TrustedAcquisitionCommand) -> Mapping[str, Any]: ...


class AcquisitionSigner(Protocol):
    """Worker-local signing capability; private material remains in its closure."""

    def __call__(
        self,
        command: TrustedAcquisitionCommand,
        collected: Mapping[str, Any],
    ) -> SignedAcquisition | Mapping[str, Any]: ...


class AcquisitionPersister(Protocol):
    """Worker-local atomic ingest capability returning a fresh durable readback."""

    def __call__(
        self,
        command: TrustedAcquisitionCommand,
        signed_acquisition: SignedAcquisition,
    ) -> DurableAcquisitionReadback | Mapping[str, Any]: ...


class TrustedAcquisitionWorker:
    """Worker-side collect -> sign -> persist handler, not a process boundary.

    The injected callables may close over transport, private-key, and ingest
    state. The handler has no parameter or public return field for that state,
    and it never offers an arbitrary-signing verb.
    """

    __slots__ = ("__collect", "__lookup", "__persist", "__public_key_registry_json", "__sign")

    def __init__(
        self,
        *,
        collect: AcquisitionCollector,
        sign: AcquisitionSigner,
        persist: AcquisitionPersister,
        public_key_registry: PublicKeyRegistry | Mapping[str, Any],
        lookup: AcquisitionReplayLookup | None = None,
    ) -> None:
        if not callable(collect) or not callable(sign) or not callable(persist):
            raise TypeError("worker capabilities must be callable")
        if lookup is not None and not callable(lookup):
            raise TypeError("worker replay lookup must be callable")
        try:
            registry = _reparse_model(public_key_registry, PublicKeyRegistry)
        except Exception:
            raise EvidenceVaultAcquisitionWorkerError("invalid_public_key_registry") from None
        self.__collect = collect
        self.__sign = sign
        self.__persist = persist
        self.__lookup = lookup
        # Keep an immutable serialized pin rather than a caller-owned nested dict.
        self.__public_key_registry_json = registry.model_dump_json()

    def capture(
        self,
        command: TrustedAcquisitionCommand | Mapping[str, Any],
    ) -> SignedAcquisitionResultEnvelope:
        """Collect, verify, persist, reread, and safely project one command."""

        validated = _parse_command(command)
        registry = self.__pinned_registry()

        if self.__lookup is not None:
            try:
                replay_value = self.__lookup(validated)
                if replay_value is not None:
                    replay, verified, _stored = _validate_durable_readback(
                        replay_value,
                        command=validated,
                        public_key_registry=registry,
                    )
                    return _build_public_result(validated, replay, verified)
            except Exception:
                raise EvidenceVaultAcquisitionWorkerError("replay_lookup_failed") from None

        try:
            collected = self.__collect(validated)
            collected_mapping = _require_mapping(collected, failure="collection_failed")
        except EvidenceVaultAcquisitionWorkerError:
            raise
        except Exception:
            raise EvidenceVaultAcquisitionWorkerError("collection_failed") from None

        try:
            signed_value = self.__sign(validated, collected_mapping)
            attempted = _validate_signed_acquisition(
                signed_value,
                command=validated,
                public_key_registry=registry,
            )
        except Exception:
            raise EvidenceVaultAcquisitionWorkerError("signing_failed") from None

        try:
            persist_input = _reparse_model(attempted, SignedAcquisition)
            persisted_value = self.__persist(validated, persist_input)
            persisted, verified, stored = _validate_durable_readback(
                persisted_value,
                command=validated,
                public_key_registry=registry,
            )
        except Exception:
            raise EvidenceVaultAcquisitionWorkerError("persistence_failed") from None

        if stored != attempted:
            raise EvidenceVaultAcquisitionReplayConflictError("persisted_result_diverged")
        try:
            return _build_public_result(validated, persisted, verified)
        except Exception:
            raise EvidenceVaultAcquisitionWorkerError("persistence_failed") from None

    def __pinned_registry(self) -> PublicKeyRegistry:
        try:
            return PublicKeyRegistry.model_validate(json.loads(self.__public_key_registry_json), strict=True)
        except Exception:
            raise EvidenceVaultAcquisitionWorkerError("invalid_public_key_registry") from None


def _parse_command(
    command: TrustedAcquisitionCommand | Mapping[str, Any],
) -> TrustedAcquisitionCommand:
    try:
        return _reparse_model(command, TrustedAcquisitionCommand)
    except Exception:
        raise EvidenceVaultAcquisitionWorkerError("invalid_command") from None


def _validate_signed_acquisition(
    value: SignedAcquisition | Mapping[str, Any],
    *,
    command: TrustedAcquisitionCommand,
    public_key_registry: PublicKeyRegistry,
) -> SignedAcquisition:
    signed = _reparse_model(value, SignedAcquisition)
    _require_command_identity(command, signed.pre_receipt_snapshot)
    built = build_signed_raw_capture(
        signed.pre_receipt_snapshot,
        signed.receipts,
        public_key_registry=public_key_registry,
        external_identity_provenance=signed.external_identity_provenance,
    )
    verified = validate_signed_raw_capture(
        built.durable_raw_capture_payload,
        capture_content_hash=built.capture_content_hash,
        public_key_registry=public_key_registry,
    )
    if _signed_from_verified(verified) != signed:
        raise ValueError("signed acquisition changed during verification")
    return signed


def _validate_durable_readback(
    value: DurableAcquisitionReadback | Mapping[str, Any],
    *,
    command: TrustedAcquisitionCommand,
    public_key_registry: PublicKeyRegistry,
) -> tuple[DurableAcquisitionReadback, VerifiedRawCapture, SignedAcquisition]:
    readback = _reparse_model(value, DurableAcquisitionReadback)
    verified = validate_signed_raw_capture(
        readback.durable_raw_capture_payload,
        capture_content_hash=readback.capture_content_hash,
        public_key_registry=public_key_registry,
    )
    _require_command_identity(command, verified.pre_receipt_snapshot)
    stored = _signed_from_verified(verified)

    expected_fingerprints = [receipt.receipt_fingerprint for receipt in stored.receipts]
    row_fingerprints = [row.receipt_fingerprint for row in readback.receipt_rows]
    if row_fingerprints != expected_fingerprints:
        raise ValueError("durable receipt rows do not match the verified receipt set")

    for receipt, row in zip(stored.receipts, readback.receipt_rows, strict=True):
        verified_receipt = verify_raw_acquisition_receipt(
            receipt,
            public_key_registry=public_key_registry,
            database_received_at=row.received_at,
        )
        if verified_receipt != receipt:
            raise ValueError("durable receipt changed during database-time verification")

    if len(stored.receipts) == 2:
        validate_c7_receipt_time_policy(
            stored.receipts,
            database_received_at=[row.received_at for row in readback.receipt_rows],
            database_time=readback.database_time,
        )
    return readback, verified, stored


def _signed_from_verified(verified: VerifiedRawCapture) -> SignedAcquisition:
    return SignedAcquisition(
        pre_receipt_snapshot=verified.pre_receipt_snapshot,
        receipts=list(verified.receipts),
        receipt_set_fingerprint=verified.envelope.receipt_set_fingerprint,
        external_identity_provenance=verified.envelope.external_identity_provenance,
    )


def _build_public_result(
    command: TrustedAcquisitionCommand,
    readback: DurableAcquisitionReadback,
    verified: VerifiedRawCapture,
) -> SignedAcquisitionResultEnvelope:
    documents: list[SafeDeterministicDocument] = []
    for receipt in verified.receipts:
        extraction = extract_deterministic_document(
            verified,
            receipt_fingerprint=receipt.receipt_fingerprint,
        )
        acquisition = receipt.claims.acquisition
        if receipt.claims.channel_role == "owned_web" and isinstance(acquisition, DirectAcquisition):
            source_url = acquisition.final_url
        elif receipt.claims.channel_role == "external_social_profile" and isinstance(
            acquisition,
            ProviderApiAcquisition,
        ):
            source_url = acquisition.reported_source_url
        else:
            raise ValueError("verified receipt has no safe source URL")
        documents.append(
            SafeDeterministicDocument(
                role=receipt.claims.channel_role,
                source_url=source_url,
                extracted_document=extraction.document,
                extracted_document_sha256=extraction.sha256,
                receipt_fingerprint=receipt.receipt_fingerprint,
            )
        )
    return SignedAcquisitionResultEnvelope(
        schema_version=SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION,
        workspace_slug=command.workspace_slug,
        source_scan_id=command.source_scan_id,
        brand_url=command.brand_url,
        capture_id=readback.capture_id,
        capture_content_hash=readback.capture_content_hash,
        receipt_set_fingerprint=verified.envelope.receipt_set_fingerprint,
        receipt_rows=[
            SafeReceiptArrival(
                receipt_id=row.receipt_id,
                receipt_fingerprint=row.receipt_fingerprint,
                received_at=row.received_at,
            )
            for row in readback.receipt_rows
        ],
        documents=documents,
    )


def _require_command_identity(
    command: TrustedAcquisitionCommand,
    snapshot: PreReceiptSnapshot,
) -> None:
    command_domain = _canonical_brand_origin(command.brand_url)
    if (
        snapshot.workspace_slug != command.workspace_slug
        or snapshot.source_scan_id != command.source_scan_id
        or snapshot.canonical_brand_url != command.brand_url
        or snapshot.canonical_brand_domain != command_domain
    ):
        raise ValueError("signed acquisition does not match its command")


def _reparse_model(value: Any, model_type: type[BaseModel]) -> Any:
    if isinstance(value, model_type):
        value = value.model_dump(mode="python", round_trip=True, warnings="none")
    if not isinstance(value, Mapping):
        raise ValueError(f"{model_type.__name__} must be an object")
    return model_type.model_validate(deepcopy(dict(value)), strict=True)


def _require_mapping(value: Any, *, failure: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceVaultAcquisitionWorkerError(failure)
    return value


def _canonical_brand_origin(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
    ):
        raise ValueError("invalid_brand_url")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_brand_url") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or parsed.query
        or parsed.path != ""
        or parsed.netloc != host
    ):
        raise ValueError("invalid_brand_url")
    if (
        host != host.lower()
        or host.startswith("www.")
        or host.endswith(".")
        or not host.isascii()
        or value != f"https://{host}"
    ):
        raise ValueError("invalid_brand_url")
    labels = host.split(".")
    if len(labels) < 2 or any(
        label.startswith("xn--") or not _DOMAIN_LABEL.fullmatch(label)
        for label in labels
    ):
        raise ValueError("invalid_brand_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("invalid_brand_url")


def _canonical_uuid(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _UUID_128.fullmatch(value) or int(value.replace("-", ""), 16) == 0:
        raise ValueError(f"{field} must be a canonical non-zero UUID")
    return value


__all__ = [
    "SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION",
    "AcquisitionCollector",
    "AcquisitionPersister",
    "AcquisitionReplayLookup",
    "AcquisitionSigner",
    "DurableAcquisitionReadback",
    "DurableReceiptReadback",
    "EvidenceVaultAcquisitionReplayConflictError",
    "EvidenceVaultAcquisitionWorkerError",
    "SafeDeterministicDocument",
    "SafeReceiptArrival",
    "SignedAcquisition",
    "SignedAcquisitionResultEnvelope",
    "TrustedAcquisitionClient",
    "TrustedAcquisitionCommand",
    "TrustedAcquisitionWorker",
]
