from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from src.services.evidence_vault_canonical_core import canonical_fingerprint, canonical_json
from src.services.evidence_vault_raw_provenance import (
    C7_LIVE_FRESHNESS_POLICY_VERSION,
    ED25519_SIGNATURE_VERSION,
    EXTERNAL_IDENTITY_ASSOCIATION_POLICY_VERSION,
    EXTERNAL_IDENTITY_PROVENANCE_VERSION,
    PRE_RECEIPT_SNAPSHOT_VERSION,
    PUBLIC_KEY_REGISTRY_VERSION,
    RAW_ACQUISITION_RECEIPT_VERSION,
    RECEIPT_SET_FINGERPRINT_VERSION,
    SOURCE_IDENTITY_SCHEMA_VERSION,
    EvidenceVaultRawProvenanceError,
    ExternalIdentityProvenance,
    PreReceiptSnapshot,
    PublicKeyRegistry,
    RawAcquisitionReceipt,
    RawAcquisitionReceiptClaims,
    build_signed_raw_acquisition_payload,
    evidence_memory_source_identity_id,
    external_identity_provenance_fingerprint,
    pre_receipt_snapshot_sha256,
    public_key_registry_fingerprint,
    raw_acquisition_receipt_fingerprint,
    receipt_set_fingerprint,
    sign_raw_acquisition_receipt,
    validate_c7_receipt_time_policy,
    validate_external_identity_provenance,
    verify_raw_acquisition_receipt,
)


UTC = timezone.utc
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
SESSION_ID = "12345678-1234-4234-8234-123456789abc"
OWNED_NONCE = "22345678-1234-4234-8234-123456789abc"
EXTERNAL_NONCE = "32345678-1234-4234-8234-123456789abc"


def _public_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture
def key_one() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


@pytest.fixture
def key_two() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


def _registry(
    key: Ed25519PrivateKey,
    *,
    key_id: str = "acquisition-2026-01",
    version: int = 1,
    status: str = "current",
) -> dict:
    return {
        "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
        "current_key_id": key_id,
        "keys": {
            key_id: {
                "version": version,
                "status": status,
                "public_key_base64": _public_b64(key),
                "signing_not_before": "2026-01-01T00:00:00Z",
                "signing_ended_at": (None if status == "current" else "2026-06-01T12:30:00Z"),
            }
        },
    }


def _snapshot() -> dict:
    return {
        "schema_version": PRE_RECEIPT_SNAPSHOT_VERSION,
        "workspace_slug": "b3s",
        "source_scan_id": "scan-70",
        "acquisition_session_id": SESSION_ID,
        "canonical_brand_domain": "example.com",
        "canonical_brand_url": "https://example.com",
        "raw_payload": {
            "owned": {"html": "<main>Example</main>"},
            "external": {"text": "Example company"},
        },
    }


def _owned_claims(*, fetched_at: str = "2026-06-01T12:00:00Z") -> dict:
    return {
        "schema_version": RAW_ACQUISITION_RECEIPT_VERSION,
        "freshness_policy_version": C7_LIVE_FRESHNESS_POLICY_VERSION,
        "key_id": "acquisition-2026-01",
        "receipt_nonce": OWNED_NONCE,
        "acquisition_session_id": SESSION_ID,
        "workspace_slug": "b3s",
        "source_scan_id": "scan-70",
        "canonical_brand_domain": "example.com",
        "channel_role": "owned_web",
        "pre_receipt_snapshot_sha256": pre_receipt_snapshot_sha256(_snapshot()),
        "provider": "direct_http",
        "acquisition": {
            "acquisition_mode": "direct_http",
            "requested_url": "http://example.com/",
            "redirect_chain": [
                {
                    "request_url": "http://example.com/",
                    "status_code": 301,
                    "location_url": "https://www.example.com/",
                }
            ],
            "final_url": "https://www.example.com/",
        },
        "fetched_at": fetched_at,
        "status_code": 200,
        "selected_headers": {
            "content-type": "text/html; charset=utf-8",
            "etag": '"owned-v1"',
        },
        "media_type": "text/html",
        "byte_count": 4096,
        "raw_fragment_json_pointer": "/raw_payload/owned/html",
        "raw_fragment_sha256": HASH_A,
        "extracted_document_sha256": HASH_B,
        "extractor_version": "html-to-text-v1",
        "external_identity_provenance_fingerprint": None,
    }


def _external_claims(*, fetched_at: str = "2026-06-01T12:02:00Z") -> dict:
    claims = _owned_claims(fetched_at=fetched_at)
    claims.update(
        {
            "receipt_nonce": EXTERNAL_NONCE,
            "channel_role": "external_social_profile",
            "provider": "exa",
            "acquisition": {
                "acquisition_mode": "provider_api",
                "provider_request_fingerprint": HASH_C,
                "result_ordinal": 0,
                "reported_source_url": "https://www.linkedin.com/company/example",
                "redirect_chain": [],
            },
            "selected_headers": {"content-type": "application/json"},
            "media_type": "application/json",
            "byte_count": 2048,
            "raw_fragment_json_pointer": "/raw_payload/external/text",
            "raw_fragment_sha256": HASH_C,
            "extracted_document_sha256": HASH_D,
            "extractor_version": "exa-result-to-text-v1",
            "external_identity_provenance_fingerprint": HASH_A,
        }
    )
    return claims


def _sign(claims: dict, key: Ed25519PrivateKey) -> RawAcquisitionReceipt:
    return sign_raw_acquisition_receipt(
        claims,
        private_key=key,
        public_key_registry=_registry(key),
    )


def _raw_sha(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _raw_payload_snapshot_sha(raw_payload: dict) -> str:
    snapshot = _snapshot()
    snapshot["canonical_brand_url"] = "https://example.com"
    snapshot["raw_payload"] = raw_payload
    return pre_receipt_snapshot_sha256(snapshot)


def _association_bundle(
    key: Ed25519PrivateKey,
    *,
    method: str = "owned_raw_links_external_profile",
) -> tuple[dict, RawAcquisitionReceipt, RawAcquisitionReceipt, dict]:
    owned_url = "https://example.com"
    external_url = "https://www.linkedin.com/company/example"
    raw_payload = {
        "owned": {
            "html": "<main>Example</main>",
            "identity": {"linkedin_company_url": external_url},
        },
        "external": {
            "profile": {
                "company_name": "Example",
                "website": owned_url,
            }
        },
    }
    snapshot = _snapshot()
    snapshot["canonical_brand_url"] = owned_url
    snapshot["raw_payload"] = raw_payload
    snapshot_sha256 = pre_receipt_snapshot_sha256(snapshot)

    owned_claims = _owned_claims()
    owned_claims["pre_receipt_snapshot_sha256"] = snapshot_sha256
    owned_claims["raw_fragment_json_pointer"] = "/owned"
    owned_claims["raw_fragment_sha256"] = _raw_sha(raw_payload["owned"])
    owned_receipt = _sign(owned_claims, key)

    if method == "owned_raw_links_external_profile":
        role = "owned_web"
        fact_pointer = "/owned/identity/linkedin_company_url"
        fact = external_url
    else:
        role = "external_social_profile"
        fact_pointer = "/external/profile/website"
        fact = owned_url
    provenance = {
        "schema_version": EXTERNAL_IDENTITY_PROVENANCE_VERSION,
        "policy_version": EXTERNAL_IDENTITY_ASSOCIATION_POLICY_VERSION,
        "association_method": method,
        "canonical_brand_domain": "example.com",
        "owned_source_url": owned_url,
        "external_source_url": external_url,
        "proof_receipt_fingerprint": owned_receipt.receipt_fingerprint,
        "raw_fact_role": role,
        "raw_fact_json_pointer": fact_pointer,
        "raw_fact_sha256": _raw_sha(fact),
        "source_identity_schema_version": SOURCE_IDENTITY_SCHEMA_VERSION,
        "owned_source_identity_id": evidence_memory_source_identity_id(
            source_url=owned_url,
            raw_fact_role="owned_web",
        ),
        "external_source_identity_id": evidence_memory_source_identity_id(
            source_url=external_url,
            raw_fact_role="external_social_profile",
        ),
    }

    external_claims = _external_claims()
    external_claims["pre_receipt_snapshot_sha256"] = snapshot_sha256
    external_claims["raw_fragment_json_pointer"] = "/external"
    external_claims["raw_fragment_sha256"] = _raw_sha(raw_payload["external"])
    external_claims["external_identity_provenance_fingerprint"] = external_identity_provenance_fingerprint(provenance)
    external_receipt = _sign(external_claims, key)
    return provenance, owned_receipt, external_receipt, raw_payload


def test_pre_receipt_snapshot_is_exact_and_uses_existing_canonical_json() -> None:
    model = PreReceiptSnapshot.model_validate(_snapshot())
    expected = __import__("hashlib").sha256(canonical_json(model.model_dump(mode="json")).encode("utf-8")).hexdigest()
    assert pre_receipt_snapshot_sha256(model) == expected
    assert len(expected) == 64
    changed = deepcopy(_snapshot())
    changed["raw_payload"]["owned"]["html"] += "!"
    assert pre_receipt_snapshot_sha256(changed) != expected
    with pytest.raises(ValidationError):
        PreReceiptSnapshot.model_validate({**_snapshot(), "authority": True})
    with pytest.raises(ValidationError):
        PreReceiptSnapshot.model_validate({**_snapshot(), "canonical_brand_url": "https://other.test/"})


@pytest.mark.parametrize(
    "canonical_brand_url",
    [
        "https://example.com/",
        "https://www.example.com",
        "https://www.example.com/",
        "https://example.com?",
        "https://example.com?ref=x",
    ],
)
def test_pre_receipt_snapshot_requires_exact_canonical_brand_origin(
    canonical_brand_url: str,
) -> None:
    changed = _snapshot()
    changed["canonical_brand_url"] = canonical_brand_url
    with pytest.raises(ValidationError, match="exact HTTPS canonical brand origin"):
        PreReceiptSnapshot.model_validate(changed)


def test_claims_payload_and_receipt_have_only_the_exact_non_circular_layers(key_one: Ed25519PrivateKey) -> None:
    claims = RawAcquisitionReceiptClaims.model_validate(_owned_claims())
    fingerprint = raw_acquisition_receipt_fingerprint(claims)
    assert fingerprint == canonical_fingerprint(RAW_ACQUISITION_RECEIPT_VERSION, claims.model_dump(mode="json"))
    payload = build_signed_raw_acquisition_payload(claims)
    assert set(payload.model_dump()) == {"signature_schema", "claims", "receipt_fingerprint"}
    assert payload.signature_schema == ED25519_SIGNATURE_VERSION
    assert payload.receipt_fingerprint == fingerprint
    receipt = _sign(_owned_claims(), key_one)
    assert set(receipt.model_dump()) == {"signature_schema", "claims", "receipt_fingerprint", "signature"}
    assert '"signature":' not in canonical_json(payload.model_dump(mode="json"))
    assert not any(name in receipt.model_dump() for name in ("authority", "runtime_effect", "production_effect"))


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), "evidence-vault-raw-acquisition-receipt-v0"),
        (("freshness_policy_version",), "evidence-vault-c7-live-freshness-policy-v0"),
        (("key_id",), "acquisition-2026-02"),
        (("receipt_nonce",), "42345678-1234-4234-8234-123456789abc"),
        (("acquisition_session_id",), "52345678-1234-4234-8234-123456789abc"),
        (("workspace_slug",), "other"),
        (("source_scan_id",), "scan-71"),
        (("canonical_brand_domain",), "other.test"),
        (("channel_role",), "external_social_profile"),
        (("pre_receipt_snapshot_sha256",), HASH_D),
        (("provider",), "firecrawl"),
        (("acquisition", "requested_url"), "http://example.com/?changed=1"),
        (("acquisition", "redirect_chain", 0, "status_code"), 302),
        (("acquisition", "final_url"), "https://example.com/?changed=1"),
        (("fetched_at",), "2026-06-01T12:00:01Z"),
        (("status_code",), 201),
        (("selected_headers",), {"content-type": "text/plain"}),
        (("media_type",), "text/plain"),
        (("byte_count",), 4097),
        (("raw_fragment_json_pointer",), "/raw_payload/owned"),
        (("raw_fragment_sha256",), HASH_C),
        (("extracted_document_sha256",), HASH_D),
        (("extractor_version",), "html-to-text-v2"),
        (("external_identity_provenance_fingerprint",), HASH_A),
    ],
)
def test_tampering_every_signed_claim_is_rejected(
    key_one: Ed25519PrivateKey,
    path: tuple,
    replacement: object,
) -> None:
    receipt = _sign(_owned_claims(), key_one).model_dump(mode="json")
    target = receipt["claims"]
    for item in path[:-1]:
        target = target[item]
    target[path[-1]] = replacement
    with pytest.raises((ValidationError, EvidenceVaultRawProvenanceError, ValueError)):
        verify_raw_acquisition_receipt(receipt, public_key_registry=_registry(key_one))


def test_fingerprint_signature_and_signed_schema_tampering_are_rejected(key_one: Ed25519PrivateKey) -> None:
    receipt = _sign(_owned_claims(), key_one).model_dump(mode="json")
    changed_fingerprint = deepcopy(receipt)
    changed_fingerprint["receipt_fingerprint"] = HASH_D
    with pytest.raises(ValidationError, match="receipt_fingerprint"):
        verify_raw_acquisition_receipt(changed_fingerprint, public_key_registry=_registry(key_one))

    changed_schema = deepcopy(receipt)
    changed_schema["signature_schema"] = "evidence-vault-ed25519-signature-v0"
    with pytest.raises(ValidationError):
        verify_raw_acquisition_receipt(changed_schema, public_key_registry=_registry(key_one))

    changed_signature = deepcopy(receipt)
    raw = bytearray(base64.b64decode(changed_signature["signature"]))
    raw[0] ^= 1
    changed_signature["signature"] = base64.b64encode(raw).decode("ascii")
    with pytest.raises(EvidenceVaultRawProvenanceError, match="signature is invalid"):
        verify_raw_acquisition_receipt(changed_signature, public_key_registry=_registry(key_one))


@pytest.mark.parametrize("signature", ["!" * 88, "AA==", " A" + "A" * 86, base64.b64encode(b"x" * 63).decode()])
def test_signature_base64_is_strict(key_one: Ed25519PrivateKey, signature: str) -> None:
    receipt = _sign(_owned_claims(), key_one).model_dump(mode="json")
    receipt["signature"] = signature
    with pytest.raises(ValidationError):
        RawAcquisitionReceipt.model_validate(receipt)


def test_valid_signature_and_matching_private_key_are_required(
    key_one: Ed25519PrivateKey, key_two: Ed25519PrivateKey
) -> None:
    receipt = _sign(_owned_claims(), key_one)
    assert verify_raw_acquisition_receipt(receipt, public_key_registry=_registry(key_one)) == receipt
    with pytest.raises(EvidenceVaultRawProvenanceError, match="does not match"):
        sign_raw_acquisition_receipt(
            _owned_claims(),
            private_key=key_two,
            public_key_registry=_registry(key_one),
        )


def test_unknown_revoked_and_rotated_keys_fail_closed(key_one: Ed25519PrivateKey, key_two: Ed25519PrivateKey) -> None:
    old_receipt = _sign(_owned_claims(), key_one)
    rotated = {
        "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
        "current_key_id": "acquisition-2026-02",
        "keys": {
            "acquisition-2026-01": {
                "version": 1,
                "status": "verification_only",
                "public_key_base64": _public_b64(key_one),
                "signing_not_before": "2026-01-01T00:00:00Z",
                "signing_ended_at": "2026-06-01T12:30:00Z",
            },
            "acquisition-2026-02": {
                "version": 2,
                "status": "current",
                "public_key_base64": _public_b64(key_two),
                "signing_not_before": "2026-06-01T12:30:00Z",
                "signing_ended_at": None,
            },
        },
    }
    assert verify_raw_acquisition_receipt(old_receipt, public_key_registry=rotated) == old_receipt
    with pytest.raises(EvidenceVaultRawProvenanceError, match="current key"):
        sign_raw_acquisition_receipt(
            _owned_claims(),
            private_key=key_one,
            public_key_registry=rotated,
        )

    revoked = deepcopy(rotated)
    revoked["keys"]["acquisition-2026-01"]["status"] = "revoked"
    with pytest.raises(EvidenceVaultRawProvenanceError, match="revoked"):
        verify_raw_acquisition_receipt(old_receipt, public_key_registry=revoked)

    unknown = deepcopy(rotated)
    del unknown["keys"]["acquisition-2026-01"]
    with pytest.raises(EvidenceVaultRawProvenanceError, match="unknown"):
        verify_raw_acquisition_receipt(old_receipt, public_key_registry=unknown)


def test_retired_key_accepts_historical_receipt_but_rejects_fresh_old_key_attack(
    key_one: Ed25519PrivateKey,
    key_two: Ed25519PrivateKey,
) -> None:
    historical = _sign(_owned_claims(fetched_at="2026-06-01T12:00:00Z"), key_one)
    registry = {
        "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
        "current_key_id": "acquisition-2026-02",
        "keys": {
            "acquisition-2026-01": {
                "version": 1,
                "status": "verification_only",
                "public_key_base64": _public_b64(key_one),
                "signing_not_before": "2026-01-01T00:00:00Z",
                "signing_ended_at": "2026-06-01T12:30:00Z",
            },
            "acquisition-2026-02": {
                "version": 2,
                "status": "current",
                "public_key_base64": _public_b64(key_two),
                "signing_not_before": "2026-06-01T12:30:00Z",
                "signing_ended_at": None,
            },
        },
    }
    assert (
        verify_raw_acquisition_receipt(
            historical,
            public_key_registry=registry,
            database_received_at=datetime(2026, 6, 1, 12, 10, tzinfo=UTC),
        )
        == historical
    )
    with pytest.raises(EvidenceVaultRawProvenanceError, match="first arrived"):
        verify_raw_acquisition_receipt(
            historical,
            public_key_registry=registry,
            database_received_at=datetime(2026, 6, 1, 12, 45, 0, 1, tzinfo=UTC),
        )

    fresh_claims = RawAcquisitionReceiptClaims.model_validate(_owned_claims(fetched_at="2026-06-01T12:31:00Z"))
    payload = build_signed_raw_acquisition_payload(fresh_claims)
    fresh = RawAcquisitionReceipt(
        **payload.model_dump(mode="python"),
        signature=base64.b64encode(
            key_one.sign(canonical_json(payload.model_dump(mode="json")).encode("utf-8"))
        ).decode("ascii"),
    )
    with pytest.raises(EvidenceVaultRawProvenanceError, match="signing cutoff"):
        verify_raw_acquisition_receipt(fresh, public_key_registry=registry)


def test_public_key_signing_windows_and_version_time_order_are_strict(
    key_one: Ed25519PrivateKey,
    key_two: Ed25519PrivateKey,
) -> None:
    with pytest.raises(EvidenceVaultRawProvenanceError, match="precedes the key signing window"):
        sign_raw_acquisition_receipt(
            _owned_claims(fetched_at="2025-12-31T23:59:59Z"),
            private_key=key_one,
            public_key_registry=_registry(key_one),
        )

    rotated = {
        "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
        "current_key_id": "acquisition-2026-02",
        "keys": {
            "acquisition-2026-01": {
                "version": 1,
                "status": "verification_only",
                "public_key_base64": _public_b64(key_one),
                "signing_not_before": "2026-06-02T00:00:00Z",
                "signing_ended_at": "2026-06-03T00:00:00Z",
            },
            "acquisition-2026-02": {
                "version": 2,
                "status": "current",
                "public_key_base64": _public_b64(key_two),
                "signing_not_before": "2026-06-01T00:00:00Z",
                "signing_ended_at": None,
            },
        },
    }
    with pytest.raises(ValidationError, match="strictly increasing"):
        PublicKeyRegistry.model_validate(rotated)

    rotated["keys"]["acquisition-2026-02"]["signing_not_before"] = "2026-06-02T12:00:00Z"
    with pytest.raises(ValidationError, match="must not overlap"):
        PublicKeyRegistry.model_validate(rotated)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(current_key_id="missing"),
        lambda r: r["keys"]["acquisition-2026-01"].update(public_key_base64="A" * 44),
        lambda r: r["keys"]["acquisition-2026-01"].update(status="verification_only"),
        lambda r: r["keys"]["acquisition-2026-01"].update(version=0),
        lambda r: r.update(extra=True),
    ],
)
def test_malformed_public_key_registry_is_rejected(key_one: Ed25519PrivateKey, mutation) -> None:
    registry = _registry(key_one)
    mutation(registry)
    with pytest.raises(ValidationError):
        PublicKeyRegistry.model_validate(registry)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_url", "https://user:pass@example.com/"),
        ("requested_url", "https://example.com:443/"),
        ("requested_url", "https://127.0.0.1/"),
        ("requested_url", "ftp://example.com/"),
        ("requested_url", "https://xn--exmple-cua.com/"),
        ("requested_url", "https://éxample.com/"),
        ("requested_url", "https://sub.example.com/"),
        ("requested_url", "https://other.test/"),
        ("requested_url", "https://EXAMPLE.com/"),
        ("requested_url", "https://example.com/#fragment"),
    ],
)
def test_owned_url_rejections_are_fail_closed(field: str, value: str) -> None:
    claims = _owned_claims()
    claims["acquisition"][field] = value
    with pytest.raises(ValidationError):
        RawAcquisitionReceiptClaims.model_validate(claims)


def test_redirect_chain_must_be_complete_bounded_and_brand_exact() -> None:
    claims = _owned_claims()
    claims["acquisition"]["redirect_chain"][0]["location_url"] = "https://example.com/other"
    with pytest.raises(ValidationError, match="terminate"):
        RawAcquisitionReceiptClaims.model_validate(claims)
    claims = _owned_claims()
    claims["acquisition"]["redirect_chain"] = claims["acquisition"]["redirect_chain"] * 11
    with pytest.raises(ValidationError):
        RawAcquisitionReceiptClaims.model_validate(claims)
    claims = _owned_claims()
    claims["acquisition"]["redirect_chain"][0]["location_url"] = "https://other.test/"
    claims["acquisition"]["final_url"] = "https://other.test/"
    with pytest.raises(ValidationError, match="canonical brand"):
        RawAcquisitionReceiptClaims.model_validate(claims)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.linkedin.com/company/example/",
        "https://linkedin.com/company/example/",
        "https://www.linkedin.com:443/company/example/",
        "https://user@www.linkedin.com/company/example/",
        "https://www.linkedin.com/in/example/",
        "https://www.linkedin.com/company/example/",
        "https://www.linkedin.com/company/",
        "https://www.linkedin.com/company/example/jobs",
        "https://www.linkedin.com/company/Example/",
        "https://www.linkedin.com/company/example/?trk=search",
        "https://www.linkedin.com/company/example/#about",
    ],
)
def test_external_role_requires_strict_exa_linkedin_company_url(url: str) -> None:
    claims = _external_claims()
    claims["acquisition"]["reported_source_url"] = url
    with pytest.raises(ValidationError):
        RawAcquisitionReceiptClaims.model_validate(claims)


def test_role_provider_mode_and_external_identity_are_exact() -> None:
    assert RawAcquisitionReceiptClaims.model_validate(_external_claims()).provider == "exa"
    for mutate in (
        lambda c: c.update(provider="firecrawl"),
        lambda c: c.update(external_identity_provenance_fingerprint=None),
        lambda c: c["acquisition"].update(
            redirect_chain=[
                {
                    "request_url": "https://www.linkedin.com/company/example/",
                    "status_code": 301,
                    "location_url": "https://www.linkedin.com/company/example/",
                }
            ]
        ),
    ):
        claims = _external_claims()
        mutate(claims)
        with pytest.raises(ValidationError):
            RawAcquisitionReceiptClaims.model_validate(claims)


@pytest.mark.parametrize(
    ("method", "role"),
    [
        ("owned_raw_links_external_profile", "owned_web"),
        ("external_raw_declares_owned_domain", "external_social_profile"),
    ],
)
def test_external_identity_association_is_exact_raw_and_reproducible(
    key_one: Ed25519PrivateKey,
    method: str,
    role: str,
) -> None:
    provenance, owned, external, snapshot = _association_bundle(
        key_one,
        method=method,
    )

    validated = validate_external_identity_provenance(
        provenance,
        owned_receipt=owned,
        external_receipt=external,
        durable_raw_capture_payload=snapshot,
        public_key_registry=_registry(key_one),
    )

    assert validated.raw_fact_role == role
    exact = validated.model_dump(mode="json")
    assert set(exact) == {
        "schema_version",
        "policy_version",
        "association_method",
        "canonical_brand_domain",
        "owned_source_url",
        "external_source_url",
        "proof_receipt_fingerprint",
        "raw_fact_role",
        "raw_fact_json_pointer",
        "raw_fact_sha256",
        "source_identity_schema_version",
        "owned_source_identity_id",
        "external_source_identity_id",
    }
    assert external_identity_provenance_fingerprint(validated) == canonical_fingerprint(
        EXTERNAL_IDENTITY_PROVENANCE_VERSION,
        exact,
    )
    assert not any(
        field in exact
        for field in (
            "fingerprint",
            "provenance_fingerprint",
            "external_receipt_fingerprint",
            "matched_alias",
            "brand_name",
        )
    )
    durable_with_reserved_provenance = deepcopy(snapshot)
    durable_with_reserved_provenance["evidence_vault_raw_provenance"] = {
        "association": exact,
        "association_fingerprint": external_identity_provenance_fingerprint(validated),
        "receipt_fingerprints": [
            owned.receipt_fingerprint,
            external.receipt_fingerprint,
        ],
    }
    assert (
        validate_external_identity_provenance(
            provenance,
            owned_receipt=owned,
            external_receipt=external,
            durable_raw_capture_payload=durable_with_reserved_provenance,
            public_key_registry=_registry(key_one),
        )
        == validated
    )


def test_external_identity_requires_registry_and_rejects_changed_signature(
    key_one: Ed25519PrivateKey,
) -> None:
    provenance, owned, external, snapshot = _association_bundle(key_one)
    changed = external.model_dump(mode="json")
    signature = bytearray(base64.b64decode(changed["signature"]))
    signature[0] ^= 1
    changed["signature"] = base64.b64encode(signature).decode("ascii")

    with pytest.raises(TypeError, match="public_key_registry"):
        validate_external_identity_provenance(
            provenance,
            owned_receipt=owned,
            external_receipt=external,
            durable_raw_capture_payload=snapshot,
        )
    with pytest.raises(EvidenceVaultRawProvenanceError, match="signature is invalid"):
        validate_external_identity_provenance(
            provenance,
            owned_receipt=owned,
            external_receipt=changed,
            durable_raw_capture_payload=snapshot,
            public_key_registry=_registry(key_one),
        )


def test_external_identity_source_ids_have_evidence_memory_v2_parity() -> None:
    from src.services.evidence_memory_identity_v2 import (
        project_evidence_memory_row_identity,
    )

    cases = [
        (
            "owned_web",
            "owned_copy",
            "web",
            "raw_input",
            "https://example.com",
        ),
        (
            "external_social_profile",
            "external_proof",
            "exa",
            "external_proof.company_profile",
            "https://www.linkedin.com/company/example",
        ),
    ]
    for role, source_class, source, evidence_type, url in cases:
        projection = project_evidence_memory_row_identity(
            {
                "source": source,
                "evidence_type": evidence_type,
                "url": url,
                "content": "identity parity material",
                "metadata": {"source_class": source_class},
            },
            brand_domain="example.com",
        )
        assert projection is not None
        assert (
            evidence_memory_source_identity_id(
                source_url=url,
                raw_fact_role=role,
            )
            == projection["document_id"]
        )


def test_external_identity_dag_has_no_external_receipt_or_self_hash_cycle(
    key_one: Ed25519PrivateKey,
) -> None:
    provenance, owned, external, snapshot = _association_bundle(key_one)
    association_fingerprint = external_identity_provenance_fingerprint(provenance)
    assert provenance["proof_receipt_fingerprint"] == owned.receipt_fingerprint
    assert external.claims.external_identity_provenance_fingerprint == association_fingerprint
    assert association_fingerprint not in canonical_json(provenance)
    assert external.receipt_fingerprint not in canonical_json(provenance)

    changed_claims = external.claims.model_dump(mode="json")
    changed_claims["fetched_at"] = "2026-06-01T12:03:00Z"
    changed_external = _sign(changed_claims, key_one)
    assert changed_external.receipt_fingerprint != external.receipt_fingerprint
    assert validate_external_identity_provenance(
        provenance,
        owned_receipt=owned,
        external_receipt=changed_external,
        durable_raw_capture_payload=snapshot,
        public_key_registry=_registry(key_one),
    ) == ExternalIdentityProvenance.model_validate(provenance)


def test_external_identity_rejects_legacy_alias_and_name_only_shapes() -> None:
    legacy = {
        "schema_version": EXTERNAL_IDENTITY_PROVENANCE_VERSION,
        "policy_version": "external-identity-policy-v1",
        "provider": "exa",
        "subject_domain": "example.com",
        "source_domain": "linkedin.com",
        "matched_alias": "Example",
        "match_method": "alias_in_title",
        "match_score": 1.0,
        "collector_source_class": "external",
        "collector_relation": "external",
        "requires_human_review": False,
        "candidate_strength": "strong",
    }
    with pytest.raises(ValidationError):
        ExternalIdentityProvenance.model_validate(legacy)
    with pytest.raises(ValidationError):
        external_identity_provenance_fingerprint(legacy)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canonical_brand_domain", "Example.com"),
        ("owned_source_url", "http://example.com"),
        ("owned_source_url", "https://www.example.com"),
        ("owned_source_url", "https://example.com/"),
        ("owned_source_url", "https://example.com/about"),
        ("owned_source_url", "https://example.com?ref=profile"),
        ("external_source_url", "https://www.linkedin.com/company/example/"),
        ("external_source_url", "https://www.linkedin.com/company/Example"),
        ("external_source_url", "https://linkedin.com/company/example"),
        ("external_source_url", "https://www.linkedin.com/company/example?trk=x"),
        ("source_identity_schema_version", "evidence-memory-document-v1"),
        ("owned_source_identity_id", HASH_A),
        ("external_source_identity_id", HASH_B),
    ],
)
def test_external_identity_urls_domains_and_source_ids_are_strict(
    key_one: Ed25519PrivateKey,
    field: str,
    value: object,
) -> None:
    provenance, _, _, _ = _association_bundle(key_one)
    provenance[field] = value
    with pytest.raises(ValidationError):
        ExternalIdentityProvenance.model_validate(provenance)


def test_external_identity_rejects_pointer_hash_predicate_and_receipt_tampering(
    key_one: Ed25519PrivateKey,
) -> None:
    provenance, owned, external, snapshot = _association_bundle(key_one)

    outside = deepcopy(provenance)
    outside["raw_fact_json_pointer"] = "/external/profile/website"
    outside["raw_fact_sha256"] = _raw_sha("https://example.com")
    outside_claims = external.claims.model_dump(mode="json")
    outside_claims["external_identity_provenance_fingerprint"] = external_identity_provenance_fingerprint(outside)
    outside_external = _sign(outside_claims, key_one)
    with pytest.raises(EvidenceVaultRawProvenanceError, match="outside"):
        validate_external_identity_provenance(
            outside,
            owned_receipt=owned,
            external_receipt=outside_external,
            durable_raw_capture_payload=snapshot,
            public_key_registry=_registry(key_one),
        )

    wrong_fact_snapshot = deepcopy(snapshot)
    wrong_fact = "https://www.linkedin.com/company/different"
    wrong_fact_snapshot["owned"]["identity"]["linkedin_company_url"] = wrong_fact
    wrong_fact_snapshot_sha256 = _raw_payload_snapshot_sha(wrong_fact_snapshot)
    wrong_fact_owned_claims = owned.claims.model_dump(mode="json")
    wrong_fact_owned_claims["pre_receipt_snapshot_sha256"] = wrong_fact_snapshot_sha256
    wrong_fact_owned_claims["raw_fragment_sha256"] = _raw_sha(wrong_fact_snapshot["owned"])
    wrong_fact_owned = _sign(wrong_fact_owned_claims, key_one)
    wrong_fact_provenance = deepcopy(provenance)
    wrong_fact_provenance["proof_receipt_fingerprint"] = wrong_fact_owned.receipt_fingerprint
    wrong_fact_provenance["raw_fact_sha256"] = _raw_sha(wrong_fact)
    wrong_fact_external_claims = external.claims.model_dump(mode="json")
    wrong_fact_external_claims["pre_receipt_snapshot_sha256"] = wrong_fact_snapshot_sha256
    wrong_fact_external_claims["external_identity_provenance_fingerprint"] = external_identity_provenance_fingerprint(
        wrong_fact_provenance
    )
    wrong_fact_external = _sign(wrong_fact_external_claims, key_one)
    with pytest.raises(EvidenceVaultRawProvenanceError, match="association_method"):
        validate_external_identity_provenance(
            wrong_fact_provenance,
            owned_receipt=wrong_fact_owned,
            external_receipt=wrong_fact_external,
            durable_raw_capture_payload=wrong_fact_snapshot,
            public_key_registry=_registry(key_one),
        )

    wrong_hash = deepcopy(provenance)
    wrong_hash["raw_fact_sha256"] = HASH_A
    wrong_hash_claims = external.claims.model_dump(mode="json")
    wrong_hash_claims["external_identity_provenance_fingerprint"] = external_identity_provenance_fingerprint(wrong_hash)
    wrong_hash_external = _sign(wrong_hash_claims, key_one)
    with pytest.raises(EvidenceVaultRawProvenanceError, match="raw_fact_sha256"):
        validate_external_identity_provenance(
            wrong_hash,
            owned_receipt=owned,
            external_receipt=wrong_hash_external,
            durable_raw_capture_payload=snapshot,
            public_key_registry=_registry(key_one),
        )

    wrong_proof = deepcopy(provenance)
    wrong_proof["proof_receipt_fingerprint"] = HASH_D
    wrong_proof_claims = external.claims.model_dump(mode="json")
    wrong_proof_claims["external_identity_provenance_fingerprint"] = external_identity_provenance_fingerprint(
        wrong_proof
    )
    wrong_proof_external = _sign(wrong_proof_claims, key_one)
    with pytest.raises(EvidenceVaultRawProvenanceError, match="owned receipt"):
        validate_external_identity_provenance(
            wrong_proof,
            owned_receipt=owned,
            external_receipt=wrong_proof_external,
            durable_raw_capture_payload=snapshot,
            public_key_registry=_registry(key_one),
        )

    wrong_association_claims = external.claims.model_dump(mode="json")
    wrong_association_claims["external_identity_provenance_fingerprint"] = HASH_C
    wrong_association_external = _sign(wrong_association_claims, key_one)
    with pytest.raises(EvidenceVaultRawProvenanceError, match="exact association"):
        validate_external_identity_provenance(
            provenance,
            owned_receipt=owned,
            external_receipt=wrong_association_external,
            durable_raw_capture_payload=snapshot,
            public_key_registry=_registry(key_one),
        )

    arbitrary_snapshot_owned_claims = owned.claims.model_dump(mode="json")
    arbitrary_snapshot_owned_claims["pre_receipt_snapshot_sha256"] = HASH_D
    arbitrary_snapshot_owned = _sign(arbitrary_snapshot_owned_claims, key_one)
    arbitrary_snapshot_provenance = deepcopy(provenance)
    arbitrary_snapshot_provenance["proof_receipt_fingerprint"] = arbitrary_snapshot_owned.receipt_fingerprint
    arbitrary_snapshot_external_claims = external.claims.model_dump(mode="json")
    arbitrary_snapshot_external_claims["pre_receipt_snapshot_sha256"] = HASH_D
    arbitrary_snapshot_external_claims["external_identity_provenance_fingerprint"] = (
        external_identity_provenance_fingerprint(arbitrary_snapshot_provenance)
    )
    arbitrary_snapshot_external = _sign(
        arbitrary_snapshot_external_claims,
        key_one,
    )
    with pytest.raises(EvidenceVaultRawProvenanceError, match="pre_receipt_snapshot_sha256"):
        validate_external_identity_provenance(
            arbitrary_snapshot_provenance,
            owned_receipt=arbitrary_snapshot_owned,
            external_receipt=arbitrary_snapshot_external,
            durable_raw_capture_payload=snapshot,
            public_key_registry=_registry(key_one),
        )

    changed_payload = deepcopy(snapshot)
    changed_payload["owned"]["html"] = "<main>Transplanted</main>"
    with pytest.raises(EvidenceVaultRawProvenanceError, match="pre_receipt_snapshot_sha256"):
        validate_external_identity_provenance(
            provenance,
            owned_receipt=owned,
            external_receipt=external,
            durable_raw_capture_payload=changed_payload,
            public_key_registry=_registry(key_one),
        )


def test_external_identity_rejects_method_role_and_cross_receipt_identity_tampering(
    key_one: Ed25519PrivateKey,
) -> None:
    provenance, owned, external, snapshot = _association_bundle(key_one)
    wrong_role = deepcopy(provenance)
    wrong_role["raw_fact_role"] = "external_social_profile"
    with pytest.raises(ValidationError, match="raw_fact_role"):
        ExternalIdentityProvenance.model_validate(wrong_role)

    for field, value in (
        ("workspace_slug", "other"),
        ("source_scan_id", "scan-71"),
        ("acquisition_session_id", "62345678-1234-4234-8234-123456789abc"),
        ("canonical_brand_domain", "other.test"),
        ("pre_receipt_snapshot_sha256", HASH_D),
    ):
        external_claims = external.claims.model_dump(mode="json")
        external_claims[field] = value
        mixed_external = _sign(external_claims, key_one)
        with pytest.raises(EvidenceVaultRawProvenanceError, match=f"mixed {field}"):
            validate_external_identity_provenance(
                provenance,
                owned_receipt=owned,
                external_receipt=mixed_external,
                durable_raw_capture_payload=snapshot,
                public_key_registry=_registry(key_one),
            )


def test_json_pointer_headers_timestamps_and_ids_are_strict() -> None:
    mutations = [
        ("raw_fragment_json_pointer", ""),
        ("raw_fragment_json_pointer", "/bad~2escape"),
        ("raw_fragment_json_pointer", "/trailing/"),
        ("selected_headers", {"authorization": "secret"}),
        ("selected_headers", {"X-Request-Id": "id"}),
        ("selected_headers", {"x-custom": "value"}),
        ("fetched_at", "2026-06-01T12:00:00+00:00"),
        ("fetched_at", "2026-06-01T12:00:00.000000Z"),
        ("receipt_nonce", "22345678123442348234123456789abc"),
        ("workspace_slug", "B3S"),
        ("key_id", "KEY 1"),
    ]
    for field, value in mutations:
        claims = _owned_claims()
        claims[field] = value
        with pytest.raises(ValidationError):
            RawAcquisitionReceiptClaims.model_validate(claims)


def test_receipt_set_fingerprint_is_order_independent_and_rejects_duplicates(key_one: Ed25519PrivateKey) -> None:
    owned = _sign(_owned_claims(), key_one)
    external = _sign(_external_claims(), key_one)
    first = receipt_set_fingerprint([owned, external])
    assert first == receipt_set_fingerprint([external, owned])
    expected = canonical_fingerprint(
        RECEIPT_SET_FINGERPRINT_VERSION,
        {
            "schema_version": RECEIPT_SET_FINGERPRINT_VERSION,
            "receipt_fingerprints": sorted([owned.receipt_fingerprint, external.receipt_fingerprint]),
        },
    )
    assert first == expected
    with pytest.raises(ValidationError, match="sorted unique"):
        receipt_set_fingerprint([owned, owned])
    with pytest.raises(ValidationError):
        receipt_set_fingerprint([])


def test_time_policy_accepts_exact_two_member_same_scan_session_and_uses_db_time() -> None:
    received = datetime(2026, 6, 1, 12, 5, tzinfo=UTC)
    now = datetime(2026, 6, 1, 12, 10, tzinfo=UTC)
    expires = validate_c7_receipt_time_policy(
        [_owned_claims(), _external_claims()],
        database_received_at=received,
        database_time=now,
    )
    assert expires == datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("owned_time", "external_time", "received", "now", "match"),
    [
        ("2026-06-01T12:11:00Z", "2026-06-01T12:02:00Z", "2026-06-01T12:05:00Z", "2026-06-01T12:05:00Z", "future"),
        ("2026-06-01T11:49:00Z", "2026-06-01T12:02:00Z", "2026-06-01T12:05:00Z", "2026-06-01T12:05:00Z", "fifteen"),
        ("2026-06-01T11:50:00Z", "2026-06-01T12:10:00Z", "2026-06-01T12:05:00Z", "2026-06-01T12:05:00Z", "timestamps"),
        ("2026-06-01T12:00:00Z", "2026-06-01T12:02:00Z", "2026-06-01T12:05:00Z", "2026-06-02T12:00:00Z", "expired"),
    ],
)
def test_time_policy_rejects_future_delayed_skewed_and_expired(
    owned_time: str,
    external_time: str,
    received: str,
    now: str,
    match: str,
) -> None:
    with pytest.raises(EvidenceVaultRawProvenanceError, match=match):
        validate_c7_receipt_time_policy(
            [_owned_claims(fetched_at=owned_time), _external_claims(fetched_at=external_time)],
            database_received_at=datetime.fromisoformat(received.replace("Z", "+00:00")),
            database_time=datetime.fromisoformat(now.replace("Z", "+00:00")),
        )


def test_time_policy_rejects_mixed_identity_roles_cardinality_and_naive_db_time() -> None:
    received = datetime(2026, 6, 1, 12, 5, tzinfo=UTC)
    now = datetime(2026, 6, 1, 12, 10, tzinfo=UTC)
    for field, value in (
        ("workspace_slug", "other"),
        ("source_scan_id", "scan-71"),
        ("acquisition_session_id", "62345678-1234-4234-8234-123456789abc"),
        ("canonical_brand_domain", "other.test"),
        ("pre_receipt_snapshot_sha256", HASH_D),
    ):
        external = _external_claims()
        external[field] = value
        with pytest.raises(EvidenceVaultRawProvenanceError, match="mixed"):
            validate_c7_receipt_time_policy(
                [_owned_claims(), external],
                database_received_at=received,
                database_time=now,
            )
    with pytest.raises(EvidenceVaultRawProvenanceError, match="one receipt"):
        validate_c7_receipt_time_policy(
            [_owned_claims(), _owned_claims()],
            database_received_at=received,
            database_time=now,
        )
    with pytest.raises(EvidenceVaultRawProvenanceError, match="exactly two"):
        validate_c7_receipt_time_policy(
            [_owned_claims()],
            database_received_at=received,
            database_time=now,
        )
    with pytest.raises(EvidenceVaultRawProvenanceError, match="timezone-aware"):
        validate_c7_receipt_time_policy(
            [_owned_claims(), _external_claims()],
            database_received_at=received.replace(tzinfo=None),
            database_time=now,
        )
    with pytest.raises(EvidenceVaultRawProvenanceError, match="precedes"):
        validate_c7_receipt_time_policy(
            [_owned_claims(), _external_claims()],
            database_received_at=received,
            database_time=received - timedelta(seconds=1),
        )


def test_extra_fields_and_lax_type_coercion_are_rejected_at_every_layer(key_one: Ed25519PrivateKey) -> None:
    claims = _owned_claims()
    claims["runtime_effect"] = False
    with pytest.raises(ValidationError):
        RawAcquisitionReceiptClaims.model_validate(claims)
    claims = _owned_claims()
    claims["byte_count"] = "4096"
    with pytest.raises(ValidationError):
        RawAcquisitionReceiptClaims.model_validate(claims)
    receipt = _sign(_owned_claims(), key_one).model_dump(mode="json")
    receipt["authority"] = False
    with pytest.raises(ValidationError):
        RawAcquisitionReceipt.model_validate(receipt)


def test_public_key_registry_fingerprint_binds_rotation_and_status(
    key_one: Ed25519PrivateKey,
    key_two: Ed25519PrivateKey,
) -> None:
    initial = _registry(key_one)
    initial_fp = public_key_registry_fingerprint(initial)
    assert initial_fp == canonical_fingerprint(PUBLIC_KEY_REGISTRY_VERSION, initial)

    rotated = deepcopy(initial)
    rotated["keys"]["acquisition-2026-01"].update(
        status="verification_only",
        signing_ended_at="2026-06-01T12:30:00Z",
    )
    rotated["keys"]["acquisition-2026-02"] = {
        "version": 2,
        "status": "current",
        "public_key_base64": _public_b64(key_two),
        "signing_not_before": "2026-06-01T12:30:00Z",
        "signing_ended_at": None,
    }
    rotated["current_key_id"] = "acquisition-2026-02"
    assert public_key_registry_fingerprint(rotated) != initial_fp

    revoked = deepcopy(rotated)
    revoked["keys"]["acquisition-2026-01"]["status"] = "revoked"
    assert public_key_registry_fingerprint(revoked) not in {
        initial_fp,
        public_key_registry_fingerprint(rotated),
    }


def test_preconstructed_models_are_revalidated_at_every_trust_boundary(
    key_one: Ed25519PrivateKey,
    key_two: Ed25519PrivateKey,
) -> None:
    receipt = _sign(_owned_claims(), key_one)
    valid_registry = PublicKeyRegistry.model_validate(_registry(key_one))
    bypassed_registry = valid_registry.model_copy(
        update={
            "keys": {
                **valid_registry.keys,
                "acquisition-2026-02": {
                    "version": 1,
                    "status": "verification_only",
                    "public_key_base64": _public_b64(key_two),
                    "signing_not_before": "2026-01-01T00:00:00Z",
                    "signing_ended_at": "2026-06-01T12:30:00Z",
                },
            }
        }
    )
    with pytest.raises(ValidationError, match="public key versions must be unique"):
        verify_raw_acquisition_receipt(
            receipt,
            public_key_registry=bypassed_registry,
        )

    bypassed_claims = receipt.claims.model_copy(update={"workspace_slug": "B3S"})
    with pytest.raises(ValidationError, match="workspace_slug"):
        raw_acquisition_receipt_fingerprint(bypassed_claims)
    with pytest.raises(ValidationError, match="workspace_slug"):
        validate_c7_receipt_time_policy(
            [
                bypassed_claims,
                RawAcquisitionReceiptClaims.model_validate(_external_claims()),
            ],
            database_received_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            database_time=datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc),
        )


def test_time_policy_uses_each_durable_receipt_arrival() -> None:
    owned = RawAcquisitionReceiptClaims.model_validate(_owned_claims(fetched_at="2026-06-01T12:00:00Z"))
    external = RawAcquisitionReceiptClaims.model_validate(_external_claims(fetched_at="2026-06-01T12:10:00Z"))
    eligible = validate_c7_receipt_time_policy(
        [owned, external],
        database_received_at=[
            datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 12, 11, tzinfo=timezone.utc),
        ],
        database_time=datetime(2026, 6, 1, 12, 12, tzinfo=timezone.utc),
    )
    assert eligible == datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(EvidenceVaultRawProvenanceError, match="fifteen minutes"):
        validate_c7_receipt_time_policy(
            [owned, external],
            database_received_at=[
                datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc),
                datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc),
            ],
            database_time=datetime(2026, 6, 1, 12, 31, tzinfo=timezone.utc),
        )
