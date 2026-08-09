from __future__ import annotations

import base64
from copy import deepcopy
import hashlib

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from src.services.evidence_vault_canonical_core import canonical_json
from src.services.evidence_vault_raw_capture import (
    DETERMINISTIC_EXTRACTOR_VERSION,
    RAW_PROVENANCE_ENVELOPE_VERSION,
    EvidenceVaultRawCaptureError,
    RawProvenanceEnvelope,
    build_signed_raw_capture,
    extract_deterministic_document,
    parse_and_validate_signed_raw_capture,
    raw_capture_content_hash,
    reproduce_passage_locator,
)
from src.services.evidence_vault_raw_provenance import (
    C7_LIVE_FRESHNESS_POLICY_VERSION,
    PRE_RECEIPT_SNAPSHOT_VERSION,
    PUBLIC_KEY_REGISTRY_VERSION,
    RAW_ACQUISITION_RECEIPT_VERSION,
    PreReceiptSnapshot,
    RawAcquisitionReceipt,
    pre_receipt_snapshot_sha256,
    sign_raw_acquisition_receipt,
)
from src.sv9_flow.evidence_worker import _external_result_content, _summarize_payload


SESSION_ID = "12345678-1234-4234-8234-123456789abc"
OWNED_NONCE = "22345678-1234-4234-8234-123456789abc"
EXTERNAL_NONCE = "32345678-1234-4234-8234-123456789abc"
RESERVED_KEY = "evidence_vault_raw_provenance"


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def _registry(key: Ed25519PrivateKey) -> dict:
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
        "current_key_id": "acquisition-2026-01",
        "keys": {
            "acquisition-2026-01": {
                "version": 1,
                "status": "current",
                "public_key_base64": base64.b64encode(public).decode("ascii"),
            }
        },
    }


def _raw_payload() -> dict:
    return {
        "sources": {
            "owned": {
                "url": "https://example.com/",
                "markdown_content": "  # Café Example\n\nDurable owned proof.  ",
                "ignored": "caller-selected text is forbidden",
            },
            "external": {
                "url": "https://www.linkedin.com/company/example",
                "title": " Example   Company ",
                "summary": " Durable\n profile ",
                "highlights": [" First  highlight ", "", "Second\nline"],
                "text": " Final\tproof ",
            },
        }
    }


def _snapshot(raw_payload: dict | None = None) -> PreReceiptSnapshot:
    return PreReceiptSnapshot(
        schema_version=PRE_RECEIPT_SNAPSHOT_VERSION,
        workspace_slug="b3s",
        source_scan_id="scan-70",
        acquisition_session_id=SESSION_ID,
        canonical_brand_domain="example.com",
        canonical_brand_url="https://example.com/",
        raw_payload=deepcopy(raw_payload if raw_payload is not None else _raw_payload()),
    )


def _owned_document(raw_payload: dict) -> str:
    fragment = raw_payload["sources"]["owned"]
    for field in ("text", "content", "markdown", "markdown_content", "summary", "title"):
        value = fragment.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _external_document(raw_payload: dict) -> str:
    return _external_result_content(raw_payload["sources"]["external"])


def _claims(snapshot: PreReceiptSnapshot, role: str) -> dict:
    raw_payload = snapshot.raw_payload
    common = {
        "schema_version": RAW_ACQUISITION_RECEIPT_VERSION,
        "freshness_policy_version": C7_LIVE_FRESHNESS_POLICY_VERSION,
        "key_id": "acquisition-2026-01",
        "acquisition_session_id": SESSION_ID,
        "workspace_slug": "b3s",
        "source_scan_id": "scan-70",
        "canonical_brand_domain": "example.com",
        "pre_receipt_snapshot_sha256": pre_receipt_snapshot_sha256(snapshot),
        "fetched_at": "2026-06-01T12:00:00Z",
        "status_code": 200,
        "extractor_version": DETERMINISTIC_EXTRACTOR_VERSION,
    }
    if role == "owned_web":
        fragment = raw_payload["sources"]["owned"]
        return {
            **common,
            "receipt_nonce": OWNED_NONCE,
            "channel_role": role,
            "provider": "direct_http",
            "acquisition": {
                "acquisition_mode": "direct_http",
                "requested_url": "https://example.com/",
                "redirect_chain": [],
                "final_url": "https://example.com/",
            },
            "selected_headers": {"content-type": "text/html; charset=utf-8"},
            "media_type": "text/html",
            "byte_count": 123,
            "raw_fragment_json_pointer": "/sources/owned",
            "raw_fragment_sha256": _sha_json(fragment),
            "extracted_document_sha256": _sha_text(_owned_document(raw_payload)),
            "external_identity_provenance_fingerprint": None,
        }
    fragment = raw_payload["sources"]["external"]
    return {
        **common,
        "receipt_nonce": EXTERNAL_NONCE,
        "channel_role": "external_social_profile",
        "provider": "exa",
        "acquisition": {
            "acquisition_mode": "provider_api",
            "provider_request_fingerprint": "c" * 64,
            "result_ordinal": 0,
            "reported_source_url": "https://www.linkedin.com/company/example",
            "redirect_chain": [],
        },
        "selected_headers": {"content-type": "application/json"},
        "media_type": "application/json",
        "byte_count": 456,
        "raw_fragment_json_pointer": "/sources/external",
        "raw_fragment_sha256": _sha_json(fragment),
        "extracted_document_sha256": _sha_text(_external_document(raw_payload)),
        "external_identity_provenance_fingerprint": "d" * 64,
    }


def _receipt(snapshot: PreReceiptSnapshot, role: str) -> RawAcquisitionReceipt:
    key = _key()
    return sign_raw_acquisition_receipt(
        _claims(snapshot, role),
        private_key=key,
        public_key_registry=_registry(key),
    )


def _group() -> tuple[PreReceiptSnapshot, RawAcquisitionReceipt, RawAcquisitionReceipt]:
    snapshot = _snapshot()
    return snapshot, _receipt(snapshot, "owned_web"), _receipt(snapshot, "external_social_profile")


def test_partial_one_receipt_capture_is_exact_detached_and_replayable() -> None:
    snapshot = _snapshot()
    owned = _receipt(snapshot, "owned_web")
    original_snapshot = deepcopy(snapshot.model_dump(mode="json"))
    original_receipt = owned.model_dump(mode="json")

    built = build_signed_raw_capture(snapshot, [owned])

    assert snapshot.model_dump(mode="json") == original_snapshot
    assert owned.model_dump(mode="json") == original_receipt
    assert RESERVED_KEY not in snapshot.raw_payload
    envelope = built.raw_payload[RESERVED_KEY]
    assert set(envelope) == {
        "schema_version",
        "workspace_slug",
        "source_scan_id",
        "acquisition_session_id",
        "canonical_brand_domain",
        "canonical_brand_url",
        "pre_receipt_snapshot_sha256",
        "signed_receipts",
        "receipt_set_fingerprint",
    }
    assert envelope["schema_version"] == RAW_PROVENANCE_ENVELOPE_VERSION
    assert len(envelope["signed_receipts"]) == 1
    assert built.capture_content_hash == raw_capture_content_hash(built.raw_payload)

    parsed = parse_and_validate_signed_raw_capture(
        built.raw_payload,
        capture_content_hash=built.capture_content_hash,
    )
    assert parsed.pre_receipt_snapshot == snapshot
    assert parsed.raw_payload == original_snapshot["raw_payload"]
    assert RESERVED_KEY not in parsed.raw_payload
    assert parsed.receipts == (owned,)


def test_two_role_group_is_sorted_and_registry_verified() -> None:
    snapshot, owned, external = _group()
    built = build_signed_raw_capture(
        snapshot,
        [external, owned],
        public_key_registry=_registry(_key()),
    )
    fingerprints = [
        item["receipt_fingerprint"]
        for item in built.raw_payload[RESERVED_KEY]["signed_receipts"]
    ]
    assert fingerprints == sorted(fingerprints)
    parsed = parse_and_validate_signed_raw_capture(
        built.raw_payload,
        capture_content_hash=built.capture_content_hash,
        public_key_registry=_registry(_key()),
    )
    assert {receipt.claims.channel_role for receipt in parsed.receipts} == {
        "owned_web",
        "external_social_profile",
    }


def test_builder_rejects_reserved_key_duplicate_role_mixed_identity_and_fragment() -> None:
    snapshot, owned, _external = _group()
    reserved_snapshot = _snapshot({**_raw_payload(), RESERVED_KEY: {"forged": True}})
    with pytest.raises(EvidenceVaultRawCaptureError, match="reserved key"):
        build_signed_raw_capture(reserved_snapshot, [_receipt(reserved_snapshot, "owned_web")])
    with pytest.raises(EvidenceVaultRawCaptureError, match="unique channel roles"):
        build_signed_raw_capture(snapshot, [owned, owned])

    other_data = _raw_payload()
    other_data["sources"]["owned"]["markdown_content"] += " changed"
    other_snapshot = _snapshot(other_data)
    other_receipt = _receipt(other_snapshot, "owned_web")
    with pytest.raises(EvidenceVaultRawCaptureError, match="pre_receipt_snapshot_sha256"):
        build_signed_raw_capture(snapshot, [other_receipt])

    bad_fragment = owned.model_copy(
        update={
            "claims": owned.claims.model_copy(update={"raw_fragment_sha256": "f" * 64})
        }
    )
    with pytest.raises((ValidationError, EvidenceVaultRawCaptureError)):
        build_signed_raw_capture(snapshot, [bad_fragment])


@pytest.mark.parametrize("count", [0, 3])
def test_builder_requires_one_or_two_receipts(count: int) -> None:
    snapshot, owned, external = _group()
    members = [owned, external, owned][:count]
    with pytest.raises(EvidenceVaultRawCaptureError, match="one or two"):
        build_signed_raw_capture(snapshot, members)


def test_caller_and_result_mutation_do_not_cross_the_capture_boundary() -> None:
    snapshot = _snapshot()
    owned = _receipt(snapshot, "owned_web")
    built = build_signed_raw_capture(snapshot, [owned])
    durable_before = deepcopy(built.raw_payload)

    snapshot.raw_payload["sources"]["owned"]["markdown_content"] = "mutated caller"
    assert built.raw_payload == durable_before
    built.raw_payload["sources"]["owned"]["markdown_content"] = "mutated result"
    assert snapshot.raw_payload["sources"]["owned"]["markdown_content"] == "mutated caller"


def test_parser_rejects_payload_envelope_receipt_set_and_capture_hash_tampering() -> None:
    snapshot, owned, external = _group()
    built = build_signed_raw_capture(snapshot, [owned, external])

    changed_payload = deepcopy(built.raw_payload)
    changed_payload["sources"]["owned"]["markdown_content"] += "!"
    with pytest.raises(EvidenceVaultRawCaptureError, match="capture_content_hash"):
        parse_and_validate_signed_raw_capture(
            changed_payload,
            capture_content_hash=built.capture_content_hash,
        )
    with pytest.raises(EvidenceVaultRawCaptureError, match="pre_receipt_snapshot_sha256"):
        parse_and_validate_signed_raw_capture(
            changed_payload,
            capture_content_hash=raw_capture_content_hash(changed_payload),
        )

    extra_envelope_field = deepcopy(built.raw_payload)
    extra_envelope_field[RESERVED_KEY]["authority"] = True
    with pytest.raises(ValidationError):
        parse_and_validate_signed_raw_capture(
            extra_envelope_field,
            capture_content_hash=raw_capture_content_hash(extra_envelope_field),
        )

    changed_set = deepcopy(built.raw_payload)
    changed_set[RESERVED_KEY]["receipt_set_fingerprint"] = "0" * 64
    with pytest.raises(ValidationError, match="receipt_set_fingerprint"):
        parse_and_validate_signed_raw_capture(
            changed_set,
            capture_content_hash=raw_capture_content_hash(changed_set),
        )

    reversed_receipts = deepcopy(built.raw_payload)
    reversed_receipts[RESERVED_KEY]["signed_receipts"].reverse()
    with pytest.raises(ValidationError, match="sorted"):
        parse_and_validate_signed_raw_capture(
            reversed_receipts,
            capture_content_hash=raw_capture_content_hash(reversed_receipts),
        )


def test_registry_is_optional_but_when_supplied_detects_signature_tampering() -> None:
    snapshot = _snapshot()
    owned = _receipt(snapshot, "owned_web")
    built = build_signed_raw_capture(snapshot, [owned])
    tampered = deepcopy(built.raw_payload)
    signature = bytearray(base64.b64decode(tampered[RESERVED_KEY]["signed_receipts"][0]["signature"]))
    signature[0] ^= 1
    tampered[RESERVED_KEY]["signed_receipts"][0]["signature"] = base64.b64encode(signature).decode("ascii")
    tampered_hash = raw_capture_content_hash(tampered)

    # Structural replay cannot establish signature authority without a registry.
    parse_and_validate_signed_raw_capture(tampered, capture_content_hash=tampered_hash)
    with pytest.raises(EvidenceVaultRawCaptureError, match="signature is invalid"):
        parse_and_validate_signed_raw_capture(
            tampered,
            capture_content_hash=tampered_hash,
            public_key_registry=_registry(_key()),
        )


def test_owned_and_external_extractors_match_live_evidence_worker_helpers() -> None:
    snapshot, owned, external = _group()
    built = build_signed_raw_capture(snapshot, [owned, external])
    owned_extraction = extract_deterministic_document(built.raw_payload, owned)
    external_extraction = extract_deterministic_document(built.raw_payload, external)

    owned_fragment = snapshot.raw_payload["sources"]["owned"]
    external_fragment = snapshot.raw_payload["sources"]["external"]
    assert owned_extraction.document == _summarize_payload(owned_fragment, limit=None)
    assert external_extraction.document == _external_result_content(external_fragment)
    assert owned_extraction.sha256 == owned.claims.extracted_document_sha256
    assert external_extraction.sha256 == external.claims.extracted_document_sha256


def test_owned_extractor_uses_fixed_live_allowlist_order_not_caller_text() -> None:
    raw_payload = _raw_payload()
    raw_payload["sources"]["owned"] = {
        "text": "  first text  ",
        "markdown_content": "markdown must lose to text",
        "document": "arbitrary caller document",
    }
    snapshot = _snapshot(raw_payload)
    owned = _receipt(snapshot, "owned_web")
    built = build_signed_raw_capture(snapshot, [owned])
    assert extract_deterministic_document(built.raw_payload, owned).document == "first text"

    missing_payload = _raw_payload()
    missing_payload["sources"]["owned"] = {"document": "not allowlisted"}
    missing_snapshot = _snapshot(missing_payload)
    missing_claims = _claims(missing_snapshot, "owned_web")
    missing_claims["extracted_document_sha256"] = _sha_text("not allowlisted")
    missing = sign_raw_acquisition_receipt(
        missing_claims,
        private_key=_key(),
        public_key_registry=_registry(_key()),
    )
    missing_built = build_signed_raw_capture(missing_snapshot, [missing])
    with pytest.raises(EvidenceVaultRawCaptureError, match="allowlisted"):
        extract_deterministic_document(missing_built.raw_payload, missing)


def test_extractor_rejects_unsupported_version_missing_external_content_and_hash_drift() -> None:
    snapshot, owned, _external = _group()
    unsupported_claims = _claims(snapshot, "owned_web")
    unsupported_claims["extractor_version"] = "caller-extractor-v9"
    unsupported = sign_raw_acquisition_receipt(
        unsupported_claims,
        private_key=_key(),
        public_key_registry=_registry(_key()),
    )
    unsupported_built = build_signed_raw_capture(snapshot, [unsupported])
    with pytest.raises(EvidenceVaultRawCaptureError, match="unsupported extractor"):
        extract_deterministic_document(unsupported_built.raw_payload, unsupported)

    missing_payload = _raw_payload()
    missing_payload["sources"]["external"] = {"url": "https://www.linkedin.com/company/example"}
    missing_snapshot = _snapshot(missing_payload)
    missing_claims = _claims(missing_snapshot, "external_social_profile")
    missing_claims["extracted_document_sha256"] = _sha_text("invented")
    missing = sign_raw_acquisition_receipt(
        missing_claims,
        private_key=_key(),
        public_key_registry=_registry(_key()),
    )
    missing_built = build_signed_raw_capture(missing_snapshot, [missing])
    with pytest.raises(EvidenceVaultRawCaptureError, match="no reproducible content"):
        extract_deterministic_document(missing_built.raw_payload, missing)

    wrong_claims = _claims(snapshot, "owned_web")
    wrong_claims["extracted_document_sha256"] = "f" * 64
    wrong = sign_raw_acquisition_receipt(
        wrong_claims,
        private_key=_key(),
        public_key_registry=_registry(_key()),
    )
    wrong_built = build_signed_raw_capture(snapshot, [wrong])
    with pytest.raises(EvidenceVaultRawCaptureError, match="signed extracted_document_sha256"):
        extract_deterministic_document(wrong_built.raw_payload, wrong)


def test_raw_fragment_pointer_supports_rfc6901_escaping_and_arrays() -> None:
    raw_payload = _raw_payload()
    raw_payload["sources"]["owned/list"] = [
        {"~record": {"markdown_content": "Escaped pointer proof"}},
    ]
    snapshot = _snapshot(raw_payload)
    claims = _claims(snapshot, "owned_web")
    fragment = raw_payload["sources"]["owned/list"][0]["~record"]
    claims["raw_fragment_json_pointer"] = "/sources/owned~1list/0/~0record"
    claims["raw_fragment_sha256"] = _sha_json(fragment)
    claims["extracted_document_sha256"] = _sha_text("Escaped pointer proof")
    receipt = sign_raw_acquisition_receipt(
        claims,
        private_key=_key(),
        public_key_registry=_registry(_key()),
    )
    built = build_signed_raw_capture(snapshot, [receipt])
    assert extract_deterministic_document(built.raw_payload, receipt).document == "Escaped pointer proof"

    missing = deepcopy(built.raw_payload)
    del missing["sources"]["owned/list"][0]["~record"]
    with pytest.raises(EvidenceVaultRawCaptureError, match="does not resolve"):
        extract_deterministic_document(missing, receipt)


def test_byte_range_locator_uses_utf8_bytes_and_rejects_split_boundaries() -> None:
    document = "Aé🙂Z"
    encoded = document.encode("utf-8")
    start = len("A".encode("utf-8"))
    end = len("Aé🙂".encode("utf-8"))
    reproduced = reproduce_passage_locator(
        extracted_document=document,
        passage_locator={"kind": "byte_range", "start": start, "end": end},
        durable_evidence_record_content="Record contains exactly é🙂 here.",
    )
    assert reproduced.passage_text == "é🙂"
    assert reproduced.passage_sha256 == _sha_text("é🙂")
    assert end <= len(encoded)

    with pytest.raises(EvidenceVaultRawCaptureError, match="UTF-8"):
        reproduce_passage_locator(
            extracted_document=document,
            passage_locator={"kind": "byte_range", "start": 2, "end": end},
            durable_evidence_record_content="é🙂",
        )
    with pytest.raises(EvidenceVaultRawCaptureError, match="not contained"):
        reproduce_passage_locator(
            extracted_document=document,
            passage_locator={"kind": "byte_range", "start": start, "end": end},
            durable_evidence_record_content="different durable content",
        )


def test_json_pointer_passage_reproduces_string_and_canonical_fragment() -> None:
    document = canonical_json(
        {
            "proof/list": [
                {"~value": "Café proof"},
                {"nested": {"b": 2, "a": 1}},
            ]
        }
    )
    string_passage = reproduce_passage_locator(
        extracted_document=document,
        passage_locator={"kind": "json_pointer", "pointer": "/proof~1list/0/~0value"},
        durable_evidence_record_content="Durable evidence: Café proof",
    )
    assert string_passage.passage_text == "Café proof"

    object_text = canonical_json({"b": 2, "a": 1})
    object_passage = reproduce_passage_locator(
        extracted_document=document,
        passage_locator={"kind": "json_pointer", "pointer": "/proof~1list/1/nested"},
        durable_evidence_record_content=f"Durable evidence: {object_text}",
    )
    assert object_passage.passage_text == '{"a":1,"b":2}'
    with pytest.raises(EvidenceVaultRawCaptureError, match="does not resolve"):
        reproduce_passage_locator(
            extracted_document=document,
            passage_locator={"kind": "json_pointer", "pointer": "/missing"},
            durable_evidence_record_content="missing",
        )


def test_exact_models_and_locators_reject_extra_or_malformed_fields() -> None:
    snapshot, owned, _external = _group()
    built = build_signed_raw_capture(snapshot, [owned])
    envelope = deepcopy(built.raw_payload[RESERVED_KEY])
    envelope["authority"] = "accepted"
    with pytest.raises(ValidationError):
        RawProvenanceEnvelope.model_validate(envelope)
    with pytest.raises(EvidenceVaultRawCaptureError, match="non-exact fields"):
        reproduce_passage_locator(
            extracted_document="proof",
            passage_locator={"kind": "byte_range", "start": 0, "end": 5, "unit": "bytes"},
            durable_evidence_record_content="proof",
        )
    with pytest.raises(EvidenceVaultRawCaptureError, match="unsupported"):
        reproduce_passage_locator(
            extracted_document="proof",
            passage_locator={"kind": "regex", "pattern": "proof"},
            durable_evidence_record_content="proof",
        )
