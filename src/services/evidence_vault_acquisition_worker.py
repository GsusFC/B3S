"""Narrow process boundary for trusted Evidence Vault acquisition.

This module deliberately contains no transport, key loading, database, environment,
or subprocess code.  A future worker process may inject those capabilities here;
the web/report process should depend only on :class:`TrustedAcquisitionClient`.
"""

from __future__ import annotations

from copy import deepcopy
import ipaddress
import re
from typing import Any, Literal, Mapping, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION = "evidence-vault-signed-acquisition-result-v1"

_WORKSPACE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$")
_SOURCE_SCAN_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,254}[A-Za-z0-9])?$")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "database_url",
        "ed25519_private_key",
        "ingest_credential",
        "ingest_credentials",
        "ingest_dsn",
        "private_key",
        "private_key_bytes",
        "proxy_authorization",
        "scanner_ingest_dsn",
        "set_cookie",
        "signing_key",
    }
)


class EvidenceVaultAcquisitionWorkerError(RuntimeError):
    """The trusted acquisition boundary failed without returning public data."""


class EvidenceVaultAcquisitionReplayConflictError(EvidenceVaultAcquisitionWorkerError):
    """Persistence returned content different from the result it was asked to store."""


class TrustedAcquisitionCommand(BaseModel):
    """The complete and only command accepted across the process boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_slug: str = Field(min_length=1, max_length=128)
    source_scan_id: str = Field(min_length=1, max_length=256)
    brand_url: str = Field(min_length=1, max_length=2048)

    @field_validator("workspace_slug")
    @classmethod
    def _validate_workspace_slug(cls, value: str) -> str:
        if not _WORKSPACE_SLUG.fullmatch(value):
            raise ValueError("invalid_workspace_slug")
        return value

    @field_validator("source_scan_id")
    @classmethod
    def _validate_source_scan_id(cls, value: str) -> str:
        if not _SOURCE_SCAN_ID.fullmatch(value):
            raise ValueError("invalid_source_scan_id")
        return value

    @field_validator("brand_url")
    @classmethod
    def _validate_brand_url(cls, value: str) -> str:
        if value != value.strip() or not value.isascii():
            raise ValueError("invalid_brand_url")
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid_brand_url") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.fragment
        ):
            raise ValueError("invalid_brand_url")
        host = host.lower().removesuffix(".")
        if host.startswith("xn--") or ".xn--" in host:
            raise ValueError("invalid_brand_url")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.removeprefix("www.").split(".")
            if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
                raise ValueError("invalid_brand_url")
        else:
            raise ValueError("invalid_brand_url")
        return value


class SignedAcquisitionResultEnvelope(BaseModel):
    """Public signed acquisition data returned only after durable persistence.

    ``signed_acquisition`` remains a mapping until the receipt-core slice is
    wired.  The envelope itself is exact-field and JSON-only, and rejects common
    secret-bearing fields recursively.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION]
    workspace_slug: str = Field(min_length=1, max_length=128)
    source_scan_id: str = Field(min_length=1, max_length=256)
    brand_url: str = Field(min_length=1, max_length=2048)
    signed_acquisition: dict[str, JsonValue]

    @model_validator(mode="after")
    def _validate_public_result(self) -> "SignedAcquisitionResultEnvelope":
        TrustedAcquisitionCommand(
            workspace_slug=self.workspace_slug,
            source_scan_id=self.source_scan_id,
            brand_url=self.brand_url,
        )
        if not self.signed_acquisition:
            raise ValueError("signed_acquisition_must_not_be_empty")
        _reject_secret_fields(self.signed_acquisition)
        return self


class TrustedAcquisitionClient(Protocol):
    """The only acquisition surface that a web/report process may receive."""

    def capture(
        self,
        command: TrustedAcquisitionCommand,
    ) -> SignedAcquisitionResultEnvelope: ...


class AcquisitionReplayLookup(Protocol):
    """Worker-local durable replay lookup performed before any recollection."""

    def __call__(
        self,
        command: TrustedAcquisitionCommand,
    ) -> Mapping[str, Any] | None: ...


class AcquisitionCollector(Protocol):
    """Worker-local collection capability; its output never comes from the caller."""

    def __call__(self, command: TrustedAcquisitionCommand) -> Mapping[str, Any]: ...


class AcquisitionSigner(Protocol):
    """Worker-local signing capability; key material is captured by its implementation."""

    def __call__(
        self,
        command: TrustedAcquisitionCommand,
        collected: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class AcquisitionPersister(Protocol):
    """Worker-local atomic ingest capability; credentials are implementation-private."""

    def __call__(
        self,
        command: TrustedAcquisitionCommand,
        signed_acquisition: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class TrustedAcquisitionWorker:
    """Worker-side handler for collect -> sign -> persist.

    The injected callables may close over transport, private-key, and ingest
    state, but this handler has no parameter or return field for that state.  It
    exposes only ``capture(command)`` and never offers an arbitrary-signing verb.
    """

    __slots__ = ("__collect", "__lookup", "__persist", "__sign")

    def __init__(
        self,
        *,
        collect: AcquisitionCollector,
        sign: AcquisitionSigner,
        persist: AcquisitionPersister,
        lookup: AcquisitionReplayLookup | None = None,
    ) -> None:
        if not callable(collect) or not callable(sign) or not callable(persist):
            raise TypeError("worker capabilities must be callable")
        if lookup is not None and not callable(lookup):
            raise TypeError("worker replay lookup must be callable")
        self.__collect = collect
        self.__sign = sign
        self.__persist = persist
        self.__lookup = lookup

    def capture(
        self,
        command: TrustedAcquisitionCommand | Mapping[str, Any],
    ) -> SignedAcquisitionResultEnvelope:
        """Collect, sign, and persist one command before exposing a result."""

        validated = _parse_command(command)
        if self.__lookup is not None:
            try:
                replay = self.__lookup(validated)
            except Exception:
                raise EvidenceVaultAcquisitionWorkerError(
                    "replay_lookup_failed"
                ) from None
            if replay is not None:
                replay_mapping = _require_mapping(
                    replay,
                    failure="replay_lookup_failed",
                )
                return _build_result(validated, replay_mapping)
        try:
            collected = self.__collect(validated)
        except Exception:
            raise EvidenceVaultAcquisitionWorkerError("collection_failed") from None
        collected_mapping = _require_mapping(collected, failure="collection_failed")

        try:
            signed = self.__sign(validated, collected_mapping)
        except Exception:
            raise EvidenceVaultAcquisitionWorkerError("signing_failed") from None
        signed_mapping = _require_mapping(signed, failure="signing_failed")
        attempted = _build_result(validated, signed_mapping)

        try:
            persisted = self.__persist(validated, deepcopy(attempted.signed_acquisition))
        except Exception:
            raise EvidenceVaultAcquisitionWorkerError("persistence_failed") from None
        persisted_mapping = _require_mapping(persisted, failure="persistence_failed")
        stored = _build_result(validated, persisted_mapping)
        if stored != attempted:
            raise EvidenceVaultAcquisitionReplayConflictError("persisted_result_diverged")
        return stored



def _parse_command(
    command: TrustedAcquisitionCommand | Mapping[str, Any],
) -> TrustedAcquisitionCommand:
    if isinstance(command, TrustedAcquisitionCommand):
        command = command.model_dump(mode="python", round_trip=True, warnings="none")
    try:
        return TrustedAcquisitionCommand.model_validate(deepcopy(command), strict=True)
    except Exception:
        raise EvidenceVaultAcquisitionWorkerError("invalid_command") from None



def _require_mapping(value: Any, *, failure: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceVaultAcquisitionWorkerError(failure)
    return value



def _build_result(
    command: TrustedAcquisitionCommand,
    signed_acquisition: Mapping[str, Any],
) -> SignedAcquisitionResultEnvelope:
    try:
        public_payload = deepcopy(dict(signed_acquisition))
        return SignedAcquisitionResultEnvelope(
            schema_version=SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION,
            workspace_slug=command.workspace_slug,
            source_scan_id=command.source_scan_id,
            brand_url=command.brand_url,
            signed_acquisition=public_payload,
        )
    except Exception:
        raise EvidenceVaultAcquisitionWorkerError("invalid_signed_result") from None



def _reject_secret_fields(value: JsonValue, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            child_path = (*path, str(raw_key))
            if key in _FORBIDDEN_PUBLIC_KEYS or "private_key" in key:
                raise ValueError(f"secret field is not public: {'/'.join(child_path)}")
            if key.startswith("ingest_") and any(
                token in key for token in ("credential", "dsn", "password", "secret", "token")
            ):
                raise ValueError(f"secret field is not public: {'/'.join(child_path)}")
            _reject_secret_fields(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, path=(*path, str(index)))
