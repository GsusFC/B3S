"""Public, secret-free contract for trusted acquisition clients.

This leaf module is safe to import in web/report processes.  It deliberately has
no database, network-collector, signing, private-key, or worker-runtime imports.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from src.services.evidence_vault_canonical_core import canonical_fingerprint


SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION = "evidence-vault-signed-acquisition-result-v1"
_RECEIPT_SET_FINGERPRINT_VERSION = "evidence-vault-raw-acquisition-receipt-set-v1"
_WORKSPACE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$")
_SOURCE_SCAN_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,254}[A-Za-z0-9])?$")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_UUID_128 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class _StrictPublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TrustedAcquisitionCommand(_StrictPublicModel):
    """The complete, raw-free command exposed by the worker boundary."""

    workspace_slug: str = Field(min_length=1, max_length=63)
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
        _canonical_brand_origin(value)
        return value


class SafeReceiptArrival(_StrictPublicModel):
    receipt_id: str
    receipt_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    received_at: AwareDatetime

    @field_validator("receipt_id")
    @classmethod
    def _receipt_id_is_uuid(cls, value: str) -> str:
        return _canonical_uuid(value, field="receipt_id")


class SafeDeterministicDocument(_StrictPublicModel):
    role: Literal["owned_web", "external_social_profile"]
    source_url: str = Field(min_length=8, max_length=2048)
    extracted_document: str = Field(min_length=1, max_length=2_097_152)
    extracted_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


    @field_validator("extracted_document")
    @classmethod
    def _document_utf8_bound(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 2_097_152:
            raise ValueError("extracted document exceeds the durable v1 bound")
        return value

    @model_validator(mode="after")
    def _validate_document_binding(self) -> "SafeDeterministicDocument":
        expected = hashlib.sha256(
            self.extracted_document.encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(self.extracted_document_sha256, expected):
            raise ValueError("public document hash differs from its bytes")
        _validate_role_source_url(self.role, self.source_url)
        return self


class SignedAcquisitionResultEnvelope(_StrictPublicModel):
    """Minimal verified result; no raw payload, headers, claims, DB time or secret."""

    schema_version: Literal[SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION]
    workspace_slug: str = Field(min_length=1, max_length=63)
    source_scan_id: str = Field(min_length=1, max_length=256)
    brand_url: str = Field(min_length=8, max_length=2048)
    capture_id: str
    capture_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_set_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_rows: list[SafeReceiptArrival] = Field(min_length=1, max_length=2)
    documents: list[SafeDeterministicDocument] = Field(min_length=1, max_length=2)

    @field_validator("capture_id")
    @classmethod
    def _capture_id_is_uuid(cls, value: str) -> str:
        return _canonical_uuid(value, field="capture_id")

    @model_validator(mode="after")
    def _validate_safe_public_projection(self) -> "SignedAcquisitionResultEnvelope":
        TrustedAcquisitionCommand(
            workspace_slug=self.workspace_slug,
            source_scan_id=self.source_scan_id,
            brand_url=self.brand_url,
        )
        document_fingerprints = [document.receipt_fingerprint for document in self.documents]
        row_fingerprints = [row.receipt_fingerprint for row in self.receipt_rows]
        if row_fingerprints != document_fingerprints:
            raise ValueError("public receipt rows and documents differ")
        if document_fingerprints != sorted(document_fingerprints):
            raise ValueError("public documents must be sorted by receipt fingerprint")
        expected_set = canonical_fingerprint(
            _RECEIPT_SET_FINGERPRINT_VERSION,
            {
                "schema_version": _RECEIPT_SET_FINGERPRINT_VERSION,
                "receipt_fingerprints": document_fingerprints,
            },
        )
        if self.receipt_set_fingerprint != expected_set:
            raise ValueError("public receipt_set_fingerprint differs from documents")
        roles = [document.role for document in self.documents]
        if set(roles) not in (
            {"owned_web"},
            {"owned_web", "external_social_profile"},
        ) or len(roles) != len(set(roles)):
            raise ValueError("public documents have an invalid exact role set")
        brand_host = _canonical_brand_origin(self.brand_url)
        owned = next(
            document for document in self.documents if document.role == "owned_web"
        )
        if _canonical_web_host(owned.source_url) != brand_host:
            raise ValueError("public owned source differs from the command brand")
        return self


class TrustedAcquisitionClient(Protocol):
    def capture(
        self,
        command: TrustedAcquisitionCommand,
    ) -> SignedAcquisitionResultEnvelope: ...


def _validate_role_source_url(role: str, value: str) -> None:
    if role == "owned_web":
        _canonical_web_host(value)
        return
    parsed = urlsplit(value)
    path_parts = parsed.path.split("/")
    slug = (
        path_parts[2]
        if len(path_parts) == 3 and path_parts[1] == "company"
        else ""
    )
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value.isascii()
        or parsed.scheme != "https"
        or parsed.netloc != "www.linkedin.com"
        or parsed.query
        or parsed.fragment
        or parsed.path.endswith("/")
        or not slug
        or len(slug) > 100
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?", slug)
    ):
        raise ValueError("public external source URL is invalid")


def _canonical_web_host(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
    ):
        raise ValueError("public owned source URL is invalid")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.netloc != parsed.hostname
        or parsed.fragment
    ):
        raise ValueError("public owned source URL is invalid")
    labels = host.split(".")
    if len(labels) < 2 or any(
        label.startswith("xn--") or not _DOMAIN_LABEL.fullmatch(label)
        for label in labels
    ):
        raise ValueError("public owned source URL is invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("public owned source URL is invalid")


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
        or host != host.lower()
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
    if (
        not isinstance(value, str)
        or not _UUID_128.fullmatch(value)
        or int(value.replace("-", ""), 16) == 0
    ):
        raise ValueError(f"{field} must be a canonical non-zero UUID")
    return value


__all__ = [
    "SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION",
    "SafeDeterministicDocument",
    "SafeReceiptArrival",
    "SignedAcquisitionResultEnvelope",
    "TrustedAcquisitionClient",
    "TrustedAcquisitionCommand",
]
