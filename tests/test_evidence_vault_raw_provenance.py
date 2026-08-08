from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from src.services.evidence_vault_canonical_core import canonical_fingerprint, canonical_json
from src.services.evidence_vault_raw_provenance import (
    C7_LIVE_FRESHNESS_POLICY_VERSION,
    ED25519_SIGNATURE_VERSION,
    PRE_RECEIPT_SNAPSHOT_VERSION,
    PUBLIC_KEY_REGISTRY_VERSION,
    RAW_ACQUISITION_RECEIPT_VERSION,
    RECEIPT_SET_FINGERPRINT_VERSION,
    EvidenceVaultRawProvenanceError,
    PreReceiptSnapshot,
    PublicKeyRegistry,
    RawAcquisitionReceipt,
    RawAcquisitionReceiptClaims,
    build_signed_raw_acquisition_payload,
    pre_receipt_snapshot_sha256,
    raw_acquisition_receipt_fingerprint,
    receipt_set_fingerprint,
    sign_raw_acquisition_receipt,
    validate_c7_receipt_time_policy,
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
        "canonical_brand_url": "https://example.com/",
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
                "reported_source_url": "https://www.linkedin.com/company/example/",
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


def test_pre_receipt_snapshot_is_exact_and_uses_existing_canonical_json() -> None:
    model = PreReceiptSnapshot.model_validate(_snapshot())
    expected = __import__("hashlib").sha256(
        canonical_json(model.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    assert pre_receipt_snapshot_sha256(model) == expected
    assert len(expected) == 64
    changed = deepcopy(_snapshot())
    changed["raw_payload"]["owned"]["html"] += "!"
    assert pre_receipt_snapshot_sha256(changed) != expected
    with pytest.raises(ValidationError):
        PreReceiptSnapshot.model_validate({**_snapshot(), "authority": True})
    with pytest.raises(ValidationError):
        PreReceiptSnapshot.model_validate({**_snapshot(), "canonical_brand_url": "https://other.test/"})


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


def test_valid_signature_and_matching_private_key_are_required(key_one: Ed25519PrivateKey, key_two: Ed25519PrivateKey) -> None:
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
            },
            "acquisition-2026-02": {
                "version": 2,
                "status": "current",
                "public_key_base64": _public_b64(key_two),
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
        lambda c: c["acquisition"].update(redirect_chain=[{
            "request_url": "https://www.linkedin.com/company/example/",
            "status_code": 301,
            "location_url": "https://www.linkedin.com/company/example/",
        }]),
    ):
        claims = _external_claims()
        mutate(claims)
        with pytest.raises(ValidationError):
            RawAcquisitionReceiptClaims.model_validate(claims)


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
