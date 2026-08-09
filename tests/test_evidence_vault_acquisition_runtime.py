from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from src.services.evidence_vault_acquisition_contract import TrustedAcquisitionCommand
from src.services.evidence_vault_acquisition_runtime import (
    EvidenceVaultAcquisitionRuntimeError,
    ExternalProviderObservation,
    HttpxExaExactUrlFetcher,
    HttpxOwnedFetcher,
    OwnedHttpObservation,
    TrustedAcquisitionRuntime,
)
from src.services.evidence_vault_raw_capture import (
    build_signed_raw_capture,
    validate_signed_raw_capture,
)
from src.services.evidence_vault_raw_provenance import (
    PUBLIC_KEY_REGISTRY_VERSION,
    PublicKeyRegistry,
    public_key_registry_fingerprint,
)


def _key_registry() -> tuple[Ed25519PrivateKey, PublicKeyRegistry]:
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    registry = PublicKeyRegistry.model_validate(
        {
            "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
            "current_key_id": "runtime-test-v1",
            "keys": {
                "runtime-test-v1": {
                    "version": 1,
                    "status": "current",
                    "signing_not_before": "2026-01-01T00:00:00Z",
                    "signing_ended_at": None,
                    "public_key_base64": base64.b64encode(public).decode(),
                }
            },
        }
    )
    return key, registry


def _command() -> TrustedAcquisitionCommand:
    return TrustedAcquisitionCommand(
        workspace_slug="b3s",
        source_scan_id="runtime-test-scan",
        brand_url="https://example.com",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _owned(*, linkedin: bool = True) -> OwnedHttpObservation:
    fragment: dict[str, Any] = {
        "url": "https://example.com/about",
        "content": "Durable exact owned acquisition evidence.",
    }
    if linkedin:
        fragment["linkedin"] = "https://www.linkedin.com/company/example"
    return OwnedHttpObservation(
        requested_url="https://example.com",
        redirect_chain=[
            {
                "request_url": "https://example.com",
                "status_code": 302,
                "location_url": "https://example.com/about",
            }
        ],
        final_url="https://example.com/about",
        fetched_at=_now(),
        status_code=200,
        selected_headers={"content-type": "text/html; charset=utf-8"},
        media_type="text/html",
        byte_count=44,
        raw_fragment=fragment,
    )


def _external() -> ExternalProviderObservation:
    return ExternalProviderObservation(
        provider_request_fingerprint="a" * 64,
        result_ordinal=0,
        reported_source_url="https://www.linkedin.com/company/example",
        fetched_at=_now(),
        status_code=200,
        selected_headers={"content-type": "application/json"},
        media_type="application/json",
        byte_count=512,
        raw_fragment={
            "url": "https://www.linkedin.com/company/example",
            "title": "Example Company",
            "summary": "Exact provider profile",
            "highlights": ["Owned-linked external fact"],
            "text": "Durable external acquisition evidence.",
        },
    )


def test_runtime_signs_exact_owned_external_group_and_embeds_association() -> None:
    key, registry = _key_registry()
    runtime = TrustedAcquisitionRuntime(
        private_key=key,
        public_key_registry=registry,
        owned_fetch=lambda _command: _owned(),
        external_fetch=lambda _url: _external(),
    )
    collected = runtime.collect(_command())
    signed = runtime.sign(_command(), collected)
    assert {receipt.claims.channel_role for receipt in signed.receipts} == {
        "owned_web",
        "external_social_profile",
    }
    assert signed.external_identity_provenance is not None
    assert (
        signed.external_identity_provenance.proof_receipt_fingerprint
        == next(
            receipt.receipt_fingerprint
            for receipt in signed.receipts
            if receipt.claims.channel_role == "owned_web"
        )
    )
    built = build_signed_raw_capture(
        signed.pre_receipt_snapshot,
        signed.receipts,
        public_key_registry=registry,
        external_identity_provenance=signed.external_identity_provenance,
    )
    verified = validate_signed_raw_capture(
        built.durable_raw_capture_payload,
        capture_content_hash=built.capture_content_hash,
        public_key_registry=registry,
    )
    assert verified.public_key_registry_fingerprint == public_key_registry_fingerprint(
        registry
    )


def test_runtime_persists_owned_only_when_external_provider_cannot_qualify() -> None:
    key, registry = _key_registry()

    def unavailable(_url: str):
        raise EvidenceVaultAcquisitionRuntimeError("provider unavailable")

    runtime = TrustedAcquisitionRuntime(
        private_key=key,
        public_key_registry=registry,
        owned_fetch=lambda _command: _owned(),
        external_fetch=unavailable,
    )
    signed = runtime.sign(_command(), runtime.collect(_command()))
    assert [receipt.claims.channel_role for receipt in signed.receipts] == [
        "owned_web"
    ]
    assert signed.external_identity_provenance is None


def test_runtime_rejects_mismatched_key_and_tampered_collected_external() -> None:
    _key, registry = _key_registry()
    other = Ed25519PrivateKey.generate()
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="registry_mismatch"):
        TrustedAcquisitionRuntime(
            private_key=other,
            public_key_registry=registry,
            owned_fetch=lambda _command: _owned(),
        )

    key, registry = _key_registry()
    runtime = TrustedAcquisitionRuntime(
        private_key=key,
        public_key_registry=registry,
        owned_fetch=lambda _command: _owned(),
        external_fetch=lambda _url: _external(),
    )
    collected = runtime.collect(_command())
    collected["external"]["reported_source_url"] = (
        "https://www.linkedin.com/company/other"
    )
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="signing_failed"):
        runtime.sign(_command(), collected)


class _Response:
    def __init__(
        self,
        status_code: int,
        content: bytes,
        headers: dict[str, str],
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers


class _GetClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _global_resolver(_host: str, _port: int, **_kwargs: Any):
    return [(None, None, None, None, ("93.184.216.34", 443))]


def test_owned_http_fetcher_records_real_redirect_body_headers_and_owned_link() -> None:
    body = (
        b'<html><a href="https://www.linkedin.com/company/example">LinkedIn</a>'
        b'<p>Durable body</p></html>'
    )
    client = _GetClient(
        [
            _Response(302, b"", {"location": "/about"}),
            _Response(
                200,
                body,
                {
                    "content-type": "text/html; charset=utf-8",
                    "etag": '"exact"',
                    "set-cookie": "secret=forbidden",
                },
            ),
        ]
    )
    observation = HttpxOwnedFetcher(
        client,
        resolver=_global_resolver,
    )(_command())
    assert observation.final_url == "https://example.com/about"
    assert observation.byte_count == len(body)
    assert observation.raw_fragment["content"] == body.decode()
    assert observation.raw_fragment["linkedin"] == (
        "https://www.linkedin.com/company/example"
    )
    assert observation.selected_headers == {
        "content-type": "text/html; charset=utf-8",
        "etag": '"exact"',
    }
    assert len(observation.redirect_chain) == 1


def test_owned_http_fetcher_rejects_private_dns_and_cross_brand_redirect() -> None:
    private = lambda *_args, **_kwargs: [
        (None, None, None, None, ("127.0.0.1", 443))
    ]
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="dns_scope"):
        HttpxOwnedFetcher(_GetClient([]), resolver=private)(_command())

    client = _GetClient(
        [_Response(302, b"", {"location": "https://evil.example/about"})]
    )
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="url_scope"):
        HttpxOwnedFetcher(client, resolver=_global_resolver)(_command())


class _PostClient:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, _url: str, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self.response


def test_exa_fetcher_requires_one_exact_url_result_and_real_fields() -> None:
    source_url = "https://www.linkedin.com/company/example"
    body = json.dumps(
        {
            "results": [
                {
                    "url": source_url,
                    "title": "Example",
                    "summary": "Exact summary",
                    "highlights": ["Exact highlight"],
                    "text": "Exact provider text",
                }
            ]
        }
    ).encode()
    client = _PostClient(
        _Response(200, body, {"content-type": "application/json", "etag": "v1"})
    )
    observation = HttpxExaExactUrlFetcher(client, api_key="worker-only")(
        source_url
    )
    assert observation.result_ordinal == 0
    assert observation.byte_count == len(body)
    assert observation.raw_fragment["url"] == source_url
    assert client.calls[0]["headers"]["x-api-key"] == "worker-only"
    assert b"worker-only" not in client.calls[0]["content"]

    duplicate_body = json.dumps(
        {"results": [observation.raw_fragment, observation.raw_fragment]}
    ).encode()
    duplicate = _PostClient(
        _Response(200, duplicate_body, {"content-type": "application/json"})
    )
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="not_unique"):
        HttpxExaExactUrlFetcher(duplicate, api_key="worker-only")(source_url)
