"""Pure contracts for signed raw/live Evidence Vault acquisition receipts.

This module deliberately has no repository, scanner, runtime, environment, or I/O
integration.  It validates and content-addresses caller-supplied values only.
Nothing defined here grants authority or enables a production/runtime effect.
"""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import re
from typing import Annotated, Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from src.evidence_identity import normalize_evidence_url, stable_artifact_digest
from src.services.evidence_vault_canonical_core import (
    canonical_fingerprint,
    canonical_json,
)


RAW_ACQUISITION_RECEIPT_VERSION = "evidence-vault-raw-acquisition-receipt-v1"
PRE_RECEIPT_SNAPSHOT_VERSION = "evidence-vault-pre-receipt-snapshot-v1"
ED25519_SIGNATURE_VERSION = "evidence-vault-ed25519-signature-v1"
PUBLIC_KEY_REGISTRY_VERSION = "evidence-vault-ed25519-public-key-registry-v1"
C7_LIVE_FRESHNESS_POLICY_VERSION = "evidence-vault-c7-live-freshness-policy-v1"
RECEIPT_SET_FINGERPRINT_VERSION = "evidence-vault-raw-acquisition-receipt-set-v1"
EXTERNAL_IDENTITY_PROVENANCE_VERSION = "external-identity-provenance-v1"
EXTERNAL_IDENTITY_ASSOCIATION_POLICY_VERSION = "evidence-vault-external-identity-association-policy-v1"
SOURCE_IDENTITY_SCHEMA_VERSION = "evidence-memory-document-v2"
RAW_PROVENANCE_CAPTURE_KEY = "evidence_vault_raw_provenance"

MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_RECEIPT_DELAY = timedelta(minutes=15)
MAX_MEMBER_SKEW = timedelta(minutes=15)
LIVE_ELIGIBILITY_TTL = timedelta(hours=24)
MAX_REDIRECTS = 10
MAX_PUBLIC_KEYS = 32

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_128_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_KEY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEADER_NAME_RE = re.compile(r"^[a-z0-9!#$%&'*+.^_`|~-]+$")
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_LINKEDIN_COMPANY_PATH_RE = re.compile(r"^/company/[a-z0-9](?:[a-z0-9-]{0,99}[a-z0-9])?$")
_JSON_POINTER_TOKEN_RE = re.compile(r"(?:[^~]|~[01])*")
_CANONICAL_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "accept-ranges",
        "age",
        "cache-control",
        "content-encoding",
        "content-language",
        "content-length",
        "content-location",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
        "link",
        "location",
        "retry-after",
        "server-timing",
        "vary",
        "via",
        "x-request-id",
    }
)
_SECRET_HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
    }
)
_REDIRECT_STATUS_CODES = frozenset({300, 301, 302, 303, 307, 308})


class EvidenceVaultRawProvenanceError(ValueError):
    """A raw/live provenance value fails the v1 contract."""


class StrictContractModel(BaseModel):
    """Shared exact-field, no-coercion contract configuration."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmptyText = Annotated[str, Field(min_length=1, max_length=2048)]


class PreReceiptSnapshot(StrictContractModel):
    """Receiptless acquisition snapshot frozen before interpretation/signing."""

    schema_version: Literal[PRE_RECEIPT_SNAPSHOT_VERSION]
    workspace_slug: str = Field(min_length=1, max_length=63)
    source_scan_id: NonEmptyText
    acquisition_session_id: str
    canonical_brand_domain: str = Field(min_length=3, max_length=253)
    canonical_brand_url: str = Field(min_length=8, max_length=2048)
    raw_payload: dict[str, JsonValue]

    @field_validator("workspace_slug")
    @classmethod
    def _workspace_is_canonical(cls, value: str) -> str:
        return _canonical_workspace(value)

    @field_validator("source_scan_id")
    @classmethod
    def _source_scan_id_is_clean(cls, value: str) -> str:
        return _clean_text(value, field="source_scan_id", maximum=2048)

    @field_validator("acquisition_session_id")
    @classmethod
    def _session_is_uuid(cls, value: str) -> str:
        return _uuid_128(value, field="acquisition_session_id")

    @field_validator("canonical_brand_domain")
    @classmethod
    def _brand_domain_is_canonical(cls, value: str) -> str:
        return _canonical_brand_domain(value)

    @model_validator(mode="after")
    def _brand_url_matches_domain(self) -> "PreReceiptSnapshot":
        _strict_owned_origin_url(
            self.canonical_brand_url,
            brand_domain=self.canonical_brand_domain,
            field="canonical_brand_url",
        )
        return self


class RedirectHop(StrictContractModel):
    request_url: str = Field(min_length=8, max_length=2048)
    status_code: int
    location_url: str = Field(min_length=8, max_length=2048)

    @field_validator("status_code")
    @classmethod
    def _status_is_redirect(cls, value: int) -> int:
        if value not in _REDIRECT_STATUS_CODES:
            raise ValueError("redirect status_code is not permitted")
        return value

    @field_validator("request_url", "location_url")
    @classmethod
    def _url_is_strict(cls, value: str, info: Any) -> str:
        _strict_web_url(value, field=info.field_name)
        return value


class DirectAcquisition(StrictContractModel):
    acquisition_mode: Literal["direct_http", "browser"]
    requested_url: str = Field(min_length=8, max_length=2048)
    redirect_chain: list[RedirectHop] = Field(max_length=MAX_REDIRECTS)
    final_url: str = Field(min_length=8, max_length=2048)

    @field_validator("requested_url", "final_url")
    @classmethod
    def _url_is_strict(cls, value: str, info: Any) -> str:
        _strict_web_url(value, field=info.field_name)
        return value

    @model_validator(mode="after")
    def _chain_is_complete(self) -> "DirectAcquisition":
        current = self.requested_url
        for index, hop in enumerate(self.redirect_chain):
            if hop.request_url != current:
                raise ValueError(f"redirect_chain[{index}] does not continue the prior URL")
            current = hop.location_url
        if current != self.final_url:
            raise ValueError("redirect_chain does not terminate at final_url")
        return self


class ProviderApiAcquisition(StrictContractModel):
    acquisition_mode: Literal["provider_api"]
    provider_request_fingerprint: Sha256
    result_ordinal: int = Field(ge=0, le=9999)
    reported_source_url: str = Field(min_length=8, max_length=2048)
    redirect_chain: list[RedirectHop] = Field(max_length=0)

    @field_validator("reported_source_url")
    @classmethod
    def _url_is_strict(cls, value: str) -> str:
        _strict_web_url(value, field="reported_source_url")
        return value


AcquisitionDetails = Annotated[
    DirectAcquisition | ProviderApiAcquisition,
    Field(discriminator="acquisition_mode"),
]


class RawAcquisitionReceiptClaims(StrictContractModel):
    """The complete, exact signed claims layer for a v1 raw receipt."""

    schema_version: Literal[RAW_ACQUISITION_RECEIPT_VERSION]
    freshness_policy_version: Literal[C7_LIVE_FRESHNESS_POLICY_VERSION]
    key_id: str = Field(min_length=1, max_length=128)
    receipt_nonce: str
    acquisition_session_id: str
    workspace_slug: str = Field(min_length=1, max_length=63)
    source_scan_id: NonEmptyText
    canonical_brand_domain: str = Field(min_length=3, max_length=253)
    channel_role: Literal["owned_web", "external_social_profile"]
    pre_receipt_snapshot_sha256: Sha256
    provider: Literal["firecrawl", "direct_http", "browser_fallback", "exa"]
    acquisition: AcquisitionDetails
    fetched_at: str
    status_code: int = Field(ge=100, le=599)
    selected_headers: dict[str, str]
    media_type: str = Field(min_length=3, max_length=255)
    byte_count: int = Field(gt=0, le=100_000_000)
    raw_fragment_json_pointer: str = Field(min_length=1, max_length=2048)
    raw_fragment_sha256: Sha256
    extracted_document_sha256: Sha256
    extractor_version: str = Field(min_length=1, max_length=200)
    external_identity_provenance_fingerprint: Sha256 | None

    @field_validator("key_id")
    @classmethod
    def _key_id_is_canonical(cls, value: str) -> str:
        if not _KEY_ID_RE.fullmatch(value):
            raise ValueError("key_id is not canonical")
        return value

    @field_validator("receipt_nonce", "acquisition_session_id")
    @classmethod
    def _ids_are_uuid_128(cls, value: str, info: Any) -> str:
        return _uuid_128(value, field=info.field_name)

    @field_validator("workspace_slug")
    @classmethod
    def _workspace_is_canonical(cls, value: str) -> str:
        return _canonical_workspace(value)

    @field_validator("source_scan_id", "extractor_version")
    @classmethod
    def _text_is_canonical(cls, value: str, info: Any) -> str:
        return _clean_text(value, field=info.field_name, maximum=2048)

    @field_validator("canonical_brand_domain")
    @classmethod
    def _brand_domain_is_canonical(cls, value: str) -> str:
        return _canonical_brand_domain(value)

    @field_validator("fetched_at")
    @classmethod
    def _fetched_at_is_canonical(cls, value: str) -> str:
        _parse_canonical_utc(value, field="fetched_at")
        return value

    @field_validator("selected_headers")
    @classmethod
    def _headers_are_safe(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("selected_headers exceeds the bounded v1 set")
        for name, header_value in value.items():
            if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name) or name != name.lower():
                raise ValueError("selected_headers names must be canonical lowercase HTTP tokens")
            if name in _SECRET_HEADER_NAMES or name not in _SAFE_RESPONSE_HEADERS:
                raise ValueError(f"selected_headers contains a non-approved header: {name}")
            if (
                not isinstance(header_value, str)
                or len(header_value) > 8192
                or any(ord(character) < 32 and character != "\t" for character in header_value)
                or "\x7f" in header_value
            ):
                raise ValueError(f"selected_headers contains an invalid value for {name}")
        return value

    @field_validator("media_type")
    @classmethod
    def _media_type_is_canonical(cls, value: str) -> str:
        if not _MEDIA_TYPE_RE.fullmatch(value) or value != value.lower():
            raise ValueError("media_type must be a canonical lowercase type/subtype")
        return value

    @field_validator("raw_fragment_json_pointer")
    @classmethod
    def _pointer_is_strict(cls, value: str) -> str:
        return _strict_json_pointer(value)

    @model_validator(mode="after")
    def _role_and_acquisition_are_eligible(self) -> "RawAcquisitionReceiptClaims":
        if not 200 <= self.status_code <= 299:
            raise ValueError("a qualifying raw acquisition must have a 2xx status")
        if self.channel_role == "owned_web":
            if self.provider not in {"firecrawl", "direct_http", "browser_fallback"}:
                raise ValueError("owned_web provider is not eligible in v1")
            if not isinstance(self.acquisition, DirectAcquisition):
                raise ValueError("owned_web requires direct HTTP/browser URL provenance")
            if self.provider == "direct_http" and self.acquisition.acquisition_mode != "direct_http":
                raise ValueError("direct_http provider requires direct_http acquisition mode")
            if self.provider == "browser_fallback" and self.acquisition.acquisition_mode != "browser":
                raise ValueError("browser_fallback provider requires browser acquisition mode")
            urls = [self.acquisition.requested_url, self.acquisition.final_url]
            for hop in self.acquisition.redirect_chain:
                urls.extend((hop.request_url, hop.location_url))
            for index, value in enumerate(urls):
                parsed = _strict_web_url(value, field=f"owned_url[{index}]")
                _require_owned_host(parsed, self.canonical_brand_domain, field=f"owned_url[{index}]")
            if self.external_identity_provenance_fingerprint is not None:
                raise ValueError("owned_web must not claim external identity provenance")
        else:
            if self.provider != "exa":
                raise ValueError("external_social_profile provider must be exa in v1")
            if not isinstance(self.acquisition, ProviderApiAcquisition):
                raise ValueError("external_social_profile requires provider_api provenance")
            _strict_linkedin_company_url(self.acquisition.reported_source_url)
            if self.external_identity_provenance_fingerprint is None:
                raise ValueError("external_social_profile requires external identity provenance")
        return self


class SignedRawAcquisitionPayload(StrictContractModel):
    signature_schema: Literal[ED25519_SIGNATURE_VERSION]
    claims: RawAcquisitionReceiptClaims
    receipt_fingerprint: Sha256

    @model_validator(mode="after")
    def _fingerprint_matches_claims(self) -> "SignedRawAcquisitionPayload":
        expected = raw_acquisition_receipt_fingerprint(self.claims)
        if not hmac.compare_digest(self.receipt_fingerprint, expected):
            raise ValueError("receipt_fingerprint does not match claims")
        return self


class RawAcquisitionReceipt(SignedRawAcquisitionPayload):
    signature: str = Field(min_length=88, max_length=88)

    @field_validator("signature")
    @classmethod
    def _signature_is_canonical(cls, value: str) -> str:
        _decode_base64(value, expected_length=64, field="signature")
        return value


class PublicKeyRecord(StrictContractModel):
    version: int = Field(ge=1)
    status: Literal["current", "verification_only", "revoked"]
    public_key_base64: str = Field(min_length=44, max_length=44)
    signing_not_before: str
    signing_ended_at: str | None = None

    @field_validator("public_key_base64")
    @classmethod
    def _key_is_canonical(cls, value: str) -> str:
        _decode_base64(value, expected_length=32, field="public_key_base64")
        return value

    @field_validator("signing_not_before", "signing_ended_at")
    @classmethod
    def _signing_times_are_canonical(cls, value: str | None, info: Any) -> str | None:
        if value is not None:
            _parse_canonical_utc(value, field=info.field_name)
        return value

    @model_validator(mode="after")
    def _signing_window_matches_status(self) -> "PublicKeyRecord":
        if self.status == "current":
            if self.signing_ended_at is not None:
                raise ValueError("a current public key must not have signing_ended_at")
            return self
        if self.signing_ended_at is None:
            raise ValueError("a retired public key requires signing_ended_at")
        if _parse_canonical_utc(
            self.signing_ended_at,
            field="signing_ended_at",
        ) < _parse_canonical_utc(
            self.signing_not_before,
            field="signing_not_before",
        ):
            raise ValueError("signing_ended_at precedes signing_not_before")
        return self


class PublicKeyRegistry(StrictContractModel):
    """Bounded public-only registry supporting rotation and revocation."""

    schema_version: Literal[PUBLIC_KEY_REGISTRY_VERSION]
    current_key_id: str = Field(min_length=1, max_length=128)
    keys: dict[str, PublicKeyRecord] = Field(min_length=1, max_length=MAX_PUBLIC_KEYS)

    @model_validator(mode="after")
    def _registry_is_coherent(self) -> "PublicKeyRegistry":
        if not _KEY_ID_RE.fullmatch(self.current_key_id):
            raise ValueError("current_key_id is not canonical")
        for key_id in self.keys:
            if not _KEY_ID_RE.fullmatch(key_id):
                raise ValueError(f"public key id is not canonical: {key_id}")
        current = [key_id for key_id, record in self.keys.items() if record.status == "current"]
        if current != [self.current_key_id]:
            raise ValueError("registry must contain exactly its declared current key")
        versions = [record.version for record in self.keys.values()]
        if len(versions) != len(set(versions)):
            raise ValueError("public key versions must be unique")
        if self.keys[self.current_key_id].version != max(versions):
            raise ValueError("the current public key must have the highest version")
        materials = [record.public_key_base64 for record in self.keys.values()]
        if len(materials) != len(set(materials)):
            raise ValueError("public key material must not be reused under multiple ids")
        ordered = sorted(self.keys.values(), key=lambda record: record.version)
        prior_not_before: datetime | None = None
        prior_ended_at: datetime | None = None
        for record in ordered:
            not_before = _parse_canonical_utc(
                record.signing_not_before,
                field="signing_not_before",
            )
            if prior_not_before is not None and not_before <= prior_not_before:
                raise ValueError("public key versions must have strictly increasing signing_not_before")
            if prior_ended_at is not None and prior_ended_at > not_before:
                raise ValueError("public key signing windows must not overlap")
            prior_not_before = not_before
            prior_ended_at = (
                _parse_canonical_utc(record.signing_ended_at, field="signing_ended_at")
                if record.signing_ended_at is not None
                else None
            )
        return self


class ReceiptSetIdentity(StrictContractModel):
    schema_version: Literal[RECEIPT_SET_FINGERPRINT_VERSION]
    receipt_fingerprints: list[Sha256] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _fingerprints_are_a_canonical_set(self) -> "ReceiptSetIdentity":
        if self.receipt_fingerprints != sorted(set(self.receipt_fingerprints)):
            raise ValueError("receipt_fingerprints must be a sorted unique set")
        return self


class ExternalIdentityProvenance(StrictContractModel):
    """One exact, non-circular raw fact associating the external profile."""

    schema_version: Literal[EXTERNAL_IDENTITY_PROVENANCE_VERSION]
    policy_version: Literal[EXTERNAL_IDENTITY_ASSOCIATION_POLICY_VERSION]
    association_method: Literal[
        "owned_raw_links_external_profile",
        "external_raw_declares_owned_domain",
    ]
    canonical_brand_domain: str = Field(min_length=3, max_length=253)
    owned_source_url: str = Field(min_length=8, max_length=2048)
    external_source_url: str = Field(min_length=8, max_length=2048)
    proof_receipt_fingerprint: Sha256
    raw_fact_role: Literal["owned_web", "external_social_profile"]
    raw_fact_json_pointer: str = Field(min_length=1, max_length=2048)
    raw_fact_sha256: Sha256
    source_identity_schema_version: Literal[SOURCE_IDENTITY_SCHEMA_VERSION]
    owned_source_identity_id: Sha256
    external_source_identity_id: Sha256

    @field_validator("canonical_brand_domain")
    @classmethod
    def _brand_domain_is_canonical(cls, value: str) -> str:
        return _canonical_brand_domain(value)

    @field_validator("external_source_url")
    @classmethod
    def _external_url_is_canonical(cls, value: str) -> str:
        _strict_linkedin_company_url(value, field="external_source_url")
        return value

    @field_validator("raw_fact_json_pointer")
    @classmethod
    def _raw_fact_pointer_is_strict(cls, value: str) -> str:
        return _strict_json_pointer(value, field="raw_fact_json_pointer")

    @model_validator(mode="after")
    def _association_is_exact(self) -> "ExternalIdentityProvenance":
        _strict_owned_origin_url(
            self.owned_source_url,
            brand_domain=self.canonical_brand_domain,
            field="owned_source_url",
        )
        expected_role = (
            "owned_web" if self.association_method == "owned_raw_links_external_profile" else "external_social_profile"
        )
        if self.raw_fact_role != expected_role:
            raise ValueError("raw_fact_role does not match association_method")
        expected_owned_id = evidence_memory_source_identity_id(
            source_url=self.owned_source_url,
            raw_fact_role="owned_web",
        )
        expected_external_id = evidence_memory_source_identity_id(
            source_url=self.external_source_url,
            raw_fact_role="external_social_profile",
        )
        if not hmac.compare_digest(self.owned_source_identity_id, expected_owned_id):
            raise ValueError("owned_source_identity_id does not match the evidence identity algorithm")
        if not hmac.compare_digest(self.external_source_identity_id, expected_external_id):
            raise ValueError("external_source_identity_id does not match the evidence identity algorithm")
        return self


def pre_receipt_snapshot_sha256(snapshot: PreReceiptSnapshot | Mapping[str, Any]) -> str:
    """Return the untagged canonical SHA-256 stored in each receipt claim."""

    model = _model_from(snapshot, PreReceiptSnapshot)
    rendered = canonical_json(model.model_dump(mode="json"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def raw_acquisition_receipt_fingerprint(
    claims: RawAcquisitionReceiptClaims | Mapping[str, Any],
) -> str:
    """Build the non-circular content fingerprint for the exact claims layer."""

    model = _model_from(claims, RawAcquisitionReceiptClaims)
    return canonical_fingerprint(
        RAW_ACQUISITION_RECEIPT_VERSION,
        model.model_dump(mode="json"),
    )


def evidence_memory_source_identity_id(
    *,
    source_url: str,
    raw_fact_role: Literal["owned_web", "external_social_profile"],
) -> str:
    """Reproduce the evidence-memory-v2 URL document identity exactly."""

    if raw_fact_role == "owned_web":
        source_class = "owned_copy"
    elif raw_fact_role == "external_social_profile":
        source_class = "external_proof"
    else:
        raise EvidenceVaultRawProvenanceError("raw_fact_role is not eligible for source identity")
    if not isinstance(source_url, str) or not source_url:
        raise EvidenceVaultRawProvenanceError("source_url must be canonical non-empty text")
    return stable_artifact_digest(
        SOURCE_IDENTITY_SCHEMA_VERSION,
        {
            "kind": "url",
            "source_class": source_class,
            "url": normalize_evidence_url(source_url),
        },
    )


def external_identity_provenance_fingerprint(
    provenance: ExternalIdentityProvenance | Mapping[str, Any],
) -> str:
    """Fingerprint the exact association object, never either external receipt."""

    model = _model_from(provenance, ExternalIdentityProvenance)
    return canonical_fingerprint(
        EXTERNAL_IDENTITY_PROVENANCE_VERSION,
        model.model_dump(mode="json"),
    )


def validate_external_identity_provenance(
    provenance: ExternalIdentityProvenance | Mapping[str, Any],
    *,
    owned_receipt: RawAcquisitionReceipt | Mapping[str, Any],
    external_receipt: RawAcquisitionReceipt | Mapping[str, Any],
    durable_raw_capture_payload: Mapping[str, Any],
    public_key_registry: PublicKeyRegistry | Mapping[str, Any],
) -> ExternalIdentityProvenance:
    """Validate one strict raw association from cryptographically verified receipts."""

    model = _model_from(provenance, ExternalIdentityProvenance)
    owned = verify_raw_acquisition_receipt(
        owned_receipt,
        public_key_registry=public_key_registry,
    )
    external = verify_raw_acquisition_receipt(
        external_receipt,
        public_key_registry=public_key_registry,
    )

    if owned.claims.channel_role != "owned_web":
        raise EvidenceVaultRawProvenanceError("owned_receipt must have the owned_web role")
    if external.claims.channel_role != "external_social_profile":
        raise EvidenceVaultRawProvenanceError("external_receipt must have the external_social_profile role")
    for field in (
        "workspace_slug",
        "source_scan_id",
        "acquisition_session_id",
        "canonical_brand_domain",
        "pre_receipt_snapshot_sha256",
    ):
        if getattr(owned.claims, field) != getattr(external.claims, field):
            raise EvidenceVaultRawProvenanceError(f"external identity receipts have mixed {field}")
    if model.canonical_brand_domain != owned.claims.canonical_brand_domain:
        raise EvidenceVaultRawProvenanceError("association canonical_brand_domain does not match its receipts")
    if not isinstance(external.claims.acquisition, ProviderApiAcquisition):
        raise EvidenceVaultRawProvenanceError("external receipt does not expose provider API source identity")
    if model.external_source_url != external.claims.acquisition.reported_source_url:
        raise EvidenceVaultRawProvenanceError("external_source_url does not match the external receipt")
    if owned.claims.external_identity_provenance_fingerprint is not None:
        raise EvidenceVaultRawProvenanceError("owned receipt must not depend on external identity provenance")
    if not hmac.compare_digest(
        model.proof_receipt_fingerprint,
        owned.receipt_fingerprint,
    ):
        raise EvidenceVaultRawProvenanceError("proof_receipt_fingerprint does not name the owned receipt")
    association_fingerprint = external_identity_provenance_fingerprint(model)
    external_claimed_fingerprint = external.claims.external_identity_provenance_fingerprint
    if external_claimed_fingerprint is None or not hmac.compare_digest(
        external_claimed_fingerprint,
        association_fingerprint,
    ):
        raise EvidenceVaultRawProvenanceError("external receipt does not claim the exact association fingerprint")

    if not isinstance(durable_raw_capture_payload, Mapping):
        raise EvidenceVaultRawProvenanceError("durable_raw_capture_payload must be an object")
    payload = dict(durable_raw_capture_payload)
    payload.pop(RAW_PROVENANCE_CAPTURE_KEY, None)
    reconstructed_snapshot = PreReceiptSnapshot(
        schema_version=PRE_RECEIPT_SNAPSHOT_VERSION,
        workspace_slug=owned.claims.workspace_slug,
        source_scan_id=owned.claims.source_scan_id,
        acquisition_session_id=owned.claims.acquisition_session_id,
        canonical_brand_domain=owned.claims.canonical_brand_domain,
        canonical_brand_url=model.owned_source_url,
        raw_payload=payload,
    )
    reconstructed_snapshot_sha256 = pre_receipt_snapshot_sha256(reconstructed_snapshot)
    if not hmac.compare_digest(
        owned.claims.pre_receipt_snapshot_sha256,
        reconstructed_snapshot_sha256,
    ):
        raise EvidenceVaultRawProvenanceError(
            "pre_receipt_snapshot_sha256 does not match the durable raw capture payload"
        )
    fact_receipt = owned if model.raw_fact_role == "owned_web" else external
    fragment_pointer = fact_receipt.claims.raw_fragment_json_pointer
    if not _json_pointer_contains(fragment_pointer, model.raw_fact_json_pointer):
        raise EvidenceVaultRawProvenanceError("raw_fact_json_pointer lies outside its receipt raw fragment")
    _resolve_json_pointer(payload, fragment_pointer)
    raw_fact = _resolve_json_pointer(payload, model.raw_fact_json_pointer)
    if isinstance(raw_fact, (Mapping, list)) or raw_fact is None:
        raise EvidenceVaultRawProvenanceError("raw association fact must be a JSON scalar")
    raw_fact_sha256 = hashlib.sha256(canonical_json(raw_fact).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(model.raw_fact_sha256, raw_fact_sha256):
        raise EvidenceVaultRawProvenanceError("raw_fact_sha256 does not match the resolved scalar")

    expected_fact = (
        model.external_source_url
        if model.association_method == "owned_raw_links_external_profile"
        else model.owned_source_url
    )
    if not isinstance(raw_fact, str) or raw_fact != expected_fact:
        raise EvidenceVaultRawProvenanceError("resolved raw fact does not satisfy association_method")
    return model


def build_signed_raw_acquisition_payload(
    claims: RawAcquisitionReceiptClaims | Mapping[str, Any],
) -> SignedRawAcquisitionPayload:
    claims_model = _model_from(claims, RawAcquisitionReceiptClaims)
    return SignedRawAcquisitionPayload(
        signature_schema=ED25519_SIGNATURE_VERSION,
        claims=claims_model,
        receipt_fingerprint=raw_acquisition_receipt_fingerprint(claims_model),
    )


def sign_raw_acquisition_receipt(
    claims: RawAcquisitionReceiptClaims | Mapping[str, Any],
    *,
    private_key: bytes | str | Ed25519PrivateKey,
    public_key_registry: PublicKeyRegistry | Mapping[str, Any],
) -> RawAcquisitionReceipt:
    """Sign claims only for the registry's one current, matching key."""

    payload = build_signed_raw_acquisition_payload(claims)
    registry = _model_from(public_key_registry, PublicKeyRegistry)
    key_id = payload.claims.key_id
    if key_id != registry.current_key_id:
        raise EvidenceVaultRawProvenanceError("new receipts must use the registry current key")
    _validate_key_signing_window(
        claims=payload.claims,
        record=registry.keys[key_id],
        database_received_at=None,
    )
    signing_key = _private_key(private_key)
    derived_public = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    registered_public = _decode_base64(
        registry.keys[key_id].public_key_base64,
        expected_length=32,
        field=f"keys.{key_id}.public_key_base64",
    )
    if not hmac.compare_digest(derived_public, registered_public):
        raise EvidenceVaultRawProvenanceError("private key does not match the registered current public key")
    signature = signing_key.sign(_signed_payload_bytes(payload))
    return RawAcquisitionReceipt(
        **payload.model_dump(mode="python"),
        signature=base64.b64encode(signature).decode("ascii"),
    )


def verify_raw_acquisition_receipt(
    receipt: RawAcquisitionReceipt | Mapping[str, Any],
    *,
    public_key_registry: PublicKeyRegistry | Mapping[str, Any],
    database_received_at: datetime | None = None,
) -> RawAcquisitionReceipt:
    """Strictly parse, re-fingerprint, and verify a receipt with a public-only registry."""

    model = _model_from(receipt, RawAcquisitionReceipt)
    registry = _model_from(public_key_registry, PublicKeyRegistry)
    record = registry.keys.get(model.claims.key_id)
    if record is None:
        raise EvidenceVaultRawProvenanceError("receipt key_id is unknown")
    if record.status == "revoked":
        raise EvidenceVaultRawProvenanceError("receipt key_id is revoked")
    public_bytes = _decode_base64(
        record.public_key_base64,
        expected_length=32,
        field=f"keys.{model.claims.key_id}.public_key_base64",
    )
    signature = _decode_base64(model.signature, expected_length=64, field="signature")
    payload = SignedRawAcquisitionPayload(
        signature_schema=model.signature_schema,
        claims=model.claims,
        receipt_fingerprint=model.receipt_fingerprint,
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature,
            _signed_payload_bytes(payload),
        )
    except InvalidSignature as exc:
        raise EvidenceVaultRawProvenanceError("receipt signature is invalid") from exc
    _validate_key_signing_window(
        claims=model.claims,
        record=record,
        database_received_at=database_received_at,
    )
    return model


def _validate_key_signing_window(
    *,
    claims: RawAcquisitionReceiptClaims,
    record: PublicKeyRecord,
    database_received_at: datetime | None,
) -> None:
    fetched_at = _parse_canonical_utc(claims.fetched_at, field="fetched_at")
    not_before = _parse_canonical_utc(
        record.signing_not_before,
        field="signing_not_before",
    )
    if fetched_at < not_before:
        raise EvidenceVaultRawProvenanceError("receipt fetched_at precedes the key signing window")
    if record.signing_ended_at is None:
        return
    ended_at = _parse_canonical_utc(record.signing_ended_at, field="signing_ended_at")
    if fetched_at > ended_at:
        raise EvidenceVaultRawProvenanceError("receipt fetched_at exceeds the retired key signing cutoff")
    if database_received_at is not None:
        received_at = _aware_utc(
            database_received_at,
            field="database_received_at",
        )
        if received_at > ended_at + MAX_RECEIPT_DELAY:
            raise EvidenceVaultRawProvenanceError("receipt first arrived after the retired key transport allowance")


def public_key_registry_fingerprint(
    public_key_registry: PublicKeyRegistry | Mapping[str, Any],
) -> str:
    """Content-address the exact public verification/rotation policy."""

    registry = _model_from(public_key_registry, PublicKeyRegistry)
    return canonical_fingerprint(
        PUBLIC_KEY_REGISTRY_VERSION,
        registry.model_dump(mode="json"),
    )


def receipt_set_fingerprint(
    receipts: Sequence[RawAcquisitionReceipt | Mapping[str, Any]],
) -> str:
    """Return an order-independent identity for distinct, self-consistent receipts."""

    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence):
        raise EvidenceVaultRawProvenanceError("receipts must be a sequence")
    fingerprints: list[str] = []
    for receipt in receipts:
        model = _model_from(receipt, RawAcquisitionReceipt)
        expected = raw_acquisition_receipt_fingerprint(model.claims)
        if not hmac.compare_digest(model.receipt_fingerprint, expected):
            raise EvidenceVaultRawProvenanceError("receipt set contains a changed fingerprint")
        fingerprints.append(expected)
    identity = ReceiptSetIdentity(
        schema_version=RECEIPT_SET_FINGERPRINT_VERSION,
        receipt_fingerprints=sorted(fingerprints),
    )
    return canonical_fingerprint(
        RECEIPT_SET_FINGERPRINT_VERSION,
        identity.model_dump(mode="json"),
    )


def validate_c7_receipt_time_policy(
    receipts: Sequence[RawAcquisitionReceiptClaims | RawAcquisitionReceipt | Mapping[str, Any]],
    *,
    database_received_at: datetime | Sequence[datetime],
    database_time: datetime,
) -> datetime:
    """Validate two signed members against their DB-recorded arrival instants.

    A single arrival remains supported when both rows are inserted under one
    database timestamp.  Shadow reads pass each immutable row's own
    ``received_at`` so a later member cannot borrow an earlier arrival window.
    Capture identity is a separate durable-FK invariant.
    """

    now = _aware_utc(database_time, field="database_time")
    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence) or len(receipts) != 2:
        raise EvidenceVaultRawProvenanceError("C7 live policy requires exactly two receipts")
    if isinstance(database_received_at, datetime):
        received = [_aware_utc(database_received_at, field="database_received_at") for _ in receipts]
    else:
        if (
            isinstance(database_received_at, (str, bytes))
            or not isinstance(database_received_at, Sequence)
            or len(database_received_at) != len(receipts)
        ):
            raise EvidenceVaultRawProvenanceError("database_received_at must contain one timestamp per receipt")
        received = [_aware_utc(value, field="database_received_at") for value in database_received_at]
    if any(now < value for value in received):
        raise EvidenceVaultRawProvenanceError("database_time precedes database_received_at")
    claims = [_claims_from_receipt(value) for value in receipts]
    if {claim.channel_role for claim in claims} != {"owned_web", "external_social_profile"}:
        raise EvidenceVaultRawProvenanceError("C7 live policy requires one receipt per qualifying role")
    for field in (
        "workspace_slug",
        "source_scan_id",
        "acquisition_session_id",
        "canonical_brand_domain",
        "pre_receipt_snapshot_sha256",
    ):
        if len({getattr(claim, field) for claim in claims}) != 1:
            raise EvidenceVaultRawProvenanceError(f"C7 receipts have mixed {field}")
    fetched = [_parse_canonical_utc(claim.fetched_at, field="fetched_at") for claim in claims]
    for fetched_at, received_at in zip(fetched, received, strict=True):
        if fetched_at > received_at + MAX_FUTURE_SKEW or fetched_at > now + MAX_FUTURE_SKEW:
            raise EvidenceVaultRawProvenanceError("receipt fetched_at exceeds the five-minute future allowance")
        if abs(received_at - fetched_at) > MAX_RECEIPT_DELAY:
            raise EvidenceVaultRawProvenanceError("receipt fetched_at is not within fifteen minutes of received_at")
    if max(fetched) - min(fetched) > MAX_MEMBER_SKEW:
        raise EvidenceVaultRawProvenanceError("receipt member timestamps differ by more than fifteen minutes")
    eligible_until = (
        min(min(fetched_at, received_at) for fetched_at, received_at in zip(fetched, received, strict=True))
        + LIVE_ELIGIBILITY_TTL
    )
    if now >= eligible_until:
        raise EvidenceVaultRawProvenanceError("receipt set has expired")
    return eligible_until


def _claims_from_receipt(
    value: RawAcquisitionReceiptClaims | RawAcquisitionReceipt | Mapping[str, Any],
) -> RawAcquisitionReceiptClaims:
    if isinstance(value, RawAcquisitionReceiptClaims):
        return _model_from(value, RawAcquisitionReceiptClaims)
    if isinstance(value, RawAcquisitionReceipt):
        return _model_from(value, RawAcquisitionReceipt).claims
    if not isinstance(value, Mapping):
        raise EvidenceVaultRawProvenanceError("receipt time-policy member must be an object")
    detached = deepcopy(dict(value))
    if set(detached) == set(RawAcquisitionReceiptClaims.model_fields):
        return RawAcquisitionReceiptClaims.model_validate(detached)
    return RawAcquisitionReceipt.model_validate(detached).claims


def _signed_payload_bytes(payload: SignedRawAcquisitionPayload) -> bytes:
    return canonical_json(payload.model_dump(mode="json")).encode("utf-8")


def _model_from(value: Any, model_type: type[BaseModel]) -> Any:
    if isinstance(value, model_type):
        # Pydantic model_copy(update=...) and mutable nested mappings do not
        # re-run validators.  Reparse a detached dump at every trust boundary.
        value = value.model_dump(mode="python", round_trip=True, warnings="none")
    if not isinstance(value, Mapping):
        raise EvidenceVaultRawProvenanceError(f"{model_type.__name__} must be an object")
    return model_type.model_validate(deepcopy(dict(value)))


def _decode_base64(value: str, *, expected_length: int, field: str) -> bytes:
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise ValueError(f"{field} must be strict canonical base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field} must be strict canonical base64") from exc
    if len(decoded) != expected_length or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{field} has an invalid encoded length or non-canonical form")
    return decoded


def _private_key(value: bytes | str | Ed25519PrivateKey) -> Ed25519PrivateKey:
    if isinstance(value, Ed25519PrivateKey):
        return value
    if isinstance(value, str):
        raw = _decode_base64(value, expected_length=32, field="private_key")
    elif isinstance(value, bytes):
        raw = value
        if len(raw) != 32:
            raise EvidenceVaultRawProvenanceError("private_key must be a 32-byte Ed25519 seed")
    else:
        raise EvidenceVaultRawProvenanceError("private_key must be a 32-byte seed or Ed25519 private key")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _uuid_128(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _UUID_128_RE.fullmatch(value) or int(value.replace("-", ""), 16) == 0:
        raise ValueError(f"{field} must be a canonical non-zero 128-bit UUID")
    return value


def _canonical_workspace(value: str) -> str:
    if not isinstance(value, str) or not _WORKSPACE_RE.fullmatch(value):
        raise ValueError("workspace_slug is not canonical")
    return value


def _clean_text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field} must be canonical non-empty text")
    return value


def _canonical_brand_domain(value: str) -> str:
    if not isinstance(value, str) or value != value.lower() or not value.isascii():
        raise ValueError("canonical_brand_domain must be lowercase ASCII")
    if value.startswith("www.") or value.endswith(".") or len(value) > 253:
        raise ValueError("canonical_brand_domain must be the non-www canonical host")
    labels = value.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("canonical_brand_domain is invalid")
    if any(label.startswith("xn--") for label in labels):
        raise ValueError("punycode brand domains are ineligible in v1")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("IP literal brand domains are ineligible")


def _strict_web_url(value: str, *, field: str) -> Any:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
    ):
        raise ValueError(f"{field} is not a strict ASCII URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.scheme != parsed.scheme.lower():
        raise ValueError(f"{field} must use canonical HTTP(S)")
    if not parsed.netloc or parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError(f"{field} must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} contains an invalid port") from exc
    if port is not None:
        raise ValueError(f"{field} must not contain a port")
    host = parsed.hostname or ""
    if not host or host != host.lower() or not host.isascii() or host.endswith("."):
        raise ValueError(f"{field} host is not canonical lowercase ASCII")
    if parsed.netloc != host:
        raise ValueError(f"{field} authority is not canonical")
    labels = host.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError(f"{field} host is invalid")
    if any(label.startswith("xn--") for label in labels):
        raise ValueError(f"{field} punycode hosts are ineligible in v1")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"{field} IP literal hosts are ineligible")
    if parsed.fragment:
        raise ValueError(f"{field} must not contain a fragment")
    return parsed


def _require_owned_host(parsed: Any, brand_domain: str, *, field: str) -> None:
    host = parsed.hostname or ""
    identity = host[4:] if host.startswith("www.") else host
    if identity != brand_domain or host not in {brand_domain, f"www.{brand_domain}"}:
        raise ValueError(f"{field} does not match the exact canonical brand domain")


def _strict_owned_origin_url(value: str, *, brand_domain: str, field: str) -> None:
    parsed = _strict_web_url(value, field=field)
    if (
        parsed.scheme != "https"
        or parsed.hostname != brand_domain
        or parsed.query
        or parsed.path
        or value != f"https://{brand_domain}"
    ):
        raise ValueError(f"{field} must be the exact HTTPS canonical brand origin")


def _strict_linkedin_company_url(
    value: str,
    *,
    field: str = "reported_source_url",
) -> None:
    parsed = _strict_web_url(value, field=field)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.linkedin.com"
        or parsed.query
        or not _LINKEDIN_COMPANY_PATH_RE.fullmatch(parsed.path)
        or value != f"https://www.linkedin.com{parsed.path}"
    ):
        raise ValueError(f"{field} must be a strict canonical LinkedIn company URL")


def _strict_json_pointer(
    value: str,
    *,
    field: str = "raw_fragment_json_pointer",
) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.endswith("/"):
        raise ValueError(f"{field} must select a non-root JSON value")
    if len(value) > 2048 or "\x00" in value:
        raise ValueError(f"{field} is invalid")
    tokens = value[1:].split("/")
    if any(token == "" or not _JSON_POINTER_TOKEN_RE.fullmatch(token) for token in tokens):
        raise ValueError(f"{field} is not a strict RFC 6901 pointer")
    return value


def _json_pointer_tokens(value: str) -> tuple[str, ...]:
    pointer = _strict_json_pointer(value)
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/"))


def _json_pointer_contains(fragment_pointer: str, fact_pointer: str) -> bool:
    fragment_tokens = _json_pointer_tokens(fragment_pointer)
    fact_tokens = _json_pointer_tokens(fact_pointer)
    return fact_tokens[: len(fragment_tokens)] == fragment_tokens


def _resolve_json_pointer(value: Any, pointer: str) -> JsonValue:
    current: Any = value
    for token in _json_pointer_tokens(pointer):
        if isinstance(current, Mapping):
            if token not in current:
                raise EvidenceVaultRawProvenanceError(f"JSON pointer does not resolve at object member {token!r}")
            current = current[token]
            continue
        if isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise EvidenceVaultRawProvenanceError("JSON pointer array token is not a canonical index")
            index = int(token)
            if index >= len(current):
                raise EvidenceVaultRawProvenanceError("JSON pointer array index is out of bounds")
            current = current[index]
            continue
        raise EvidenceVaultRawProvenanceError("JSON pointer traverses through a scalar")
    return current


def _parse_canonical_utc(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or not _CANONICAL_UTC_RE.fullmatch(value):
        raise ValueError(f"{field} must be canonical RFC 3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid timestamp") from exc
    canonical = parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds").replace("+00:00", "Z")
    if canonical != value:
        raise ValueError(f"{field} must use its shortest canonical UTC representation")
    return parsed


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceVaultRawProvenanceError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


__all__ = [
    "C7_LIVE_FRESHNESS_POLICY_VERSION",
    "ED25519_SIGNATURE_VERSION",
    "EXTERNAL_IDENTITY_ASSOCIATION_POLICY_VERSION",
    "EXTERNAL_IDENTITY_PROVENANCE_VERSION",
    "EvidenceVaultRawProvenanceError",
    "LIVE_ELIGIBILITY_TTL",
    "MAX_FUTURE_SKEW",
    "MAX_MEMBER_SKEW",
    "MAX_RECEIPT_DELAY",
    "PRE_RECEIPT_SNAPSHOT_VERSION",
    "PUBLIC_KEY_REGISTRY_VERSION",
    "RAW_ACQUISITION_RECEIPT_VERSION",
    "RECEIPT_SET_FINGERPRINT_VERSION",
    "SOURCE_IDENTITY_SCHEMA_VERSION",
    "DirectAcquisition",
    "ExternalIdentityProvenance",
    "PreReceiptSnapshot",
    "ProviderApiAcquisition",
    "PublicKeyRecord",
    "PublicKeyRegistry",
    "RawAcquisitionReceipt",
    "RawAcquisitionReceiptClaims",
    "ReceiptSetIdentity",
    "RedirectHop",
    "SignedRawAcquisitionPayload",
    "build_signed_raw_acquisition_payload",
    "evidence_memory_source_identity_id",
    "external_identity_provenance_fingerprint",
    "pre_receipt_snapshot_sha256",
    "public_key_registry_fingerprint",
    "raw_acquisition_receipt_fingerprint",
    "receipt_set_fingerprint",
    "sign_raw_acquisition_receipt",
    "validate_c7_receipt_time_policy",
    "validate_external_identity_provenance",
    "verify_raw_acquisition_receipt",
]
