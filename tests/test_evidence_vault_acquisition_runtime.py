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
    ExternalDiscovery,
    ExternalProviderObservation,
    HttpxExaExactUrlFetcher,
    HttpxOwnedFetcher,
    OwnedHttpObservation,
    PublicOnlyNetworkBackend,
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
        signed.external_identity_provenance.association_method
        == "owned_raw_links_external_profile"
    )
    assert "exa_independent_discovery" not in signed.external_identity_provenance.model_dump_json()
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
        allow_owned_only_downgrade=True,
    )
    collected = runtime.collect(_command())
    assert collected["external_outcome"] == "owned_only_downgrade"
    assert collected["external_failure_reason"] == "provider_unavailable"
    signed = runtime.sign(_command(), collected)
    assert [receipt.claims.channel_role for receipt in signed.receipts] == [
        "owned_web"
    ]
    assert signed.external_identity_provenance is None


def test_runtime_rejects_external_url_different_from_owned_exact_link() -> None:
    key, registry = _key_registry()
    different = _external().model_copy(
        update={
            "reported_source_url": "https://www.linkedin.com/company/different",
            "raw_fragment": {
                **_external().raw_fragment,
                "url": "https://www.linkedin.com/company/different",
            },
        }
    )
    runtime = TrustedAcquisitionRuntime(
        private_key=key,
        public_key_registry=registry,
        owned_fetch=lambda _command: _owned(),
        external_fetch=lambda _url: different,
        allow_owned_only_downgrade=True,
    )

    collected = runtime.collect(_command())

    assert collected["external_outcome"] == "owned_only_downgrade"
    assert collected["external_failure_reason"] == "provider_result_ineligible"
    assert runtime.sign(_command(), collected).external_identity_provenance is None


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


class _NetworkStream:
    def __init__(self, address: str = "93.184.216.34") -> None:
        self.address = address

    def get_extra_info(self, name: str):
        assert name == "server_addr"
        return (self.address, 443)


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
        self.extensions = {"network_stream": _NetworkStream()}

    def iter_raw(self):
        midpoint = len(self.content) // 2
        yield self.content[:midpoint]
        yield self.content[midpoint:]

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return None


class _GetClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> _Response:
        assert method == "GET"
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
    assert observation.raw_fragment["raw_body"] == body.decode()
    assert observation.raw_fragment["text"] == "LinkedIn Durable body"
    assert observation.raw_fragment["linkedin"] == (
        "https://www.linkedin.com/company/example"
    )
    assert observation.selected_headers == {
        "content-type": "text/html; charset=utf-8",
        "etag": '"exact"',
    }
    assert len(observation.redirect_chain) == 1
    assert "## Subpage:" not in str(observation.raw_fragment["text"])


def test_owned_http_fetcher_appends_same_brand_subpages_like_fly() -> None:
    home = (
        b'<html><a href="/sofia">SofIA</a>'
        b"<p>Encuentra talento real en horas.</p></html>"
    )
    sofia = (
        b"<html><h1>SofIA</h1>"
        b"<p>Ecosistema propietario para que el reclutamiento vuelva a ser "
        b"estrategico.</p></html>"
    )
    client = _GetClient(
        [
            _Response(200, home, {"content-type": "text/html; charset=utf-8"}),
            _Response(200, sofia, {"content-type": "text/html; charset=utf-8"}),
        ]
    )
    observation = HttpxOwnedFetcher(
        client,
        resolver=_global_resolver,
    )(_command())
    text = str(observation.raw_fragment["text"])
    assert "talento real" in text
    assert "## Subpage: https://example.com/sofia" in text
    assert "reclutamiento vuelva a ser estrategico" in text
    assert [url for url, _kwargs in client.calls] == [
        "https://example.com",
        "https://example.com/sofia",
    ]


def test_verified_raw_owned_subpages_become_separate_evidence_records() -> None:
    from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot

    fingerprint = "b" * 64
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {
                "id": 1,
                "brand_name": "Leeters",
                "url": "https://leeters.com",
            },
            "raw_inputs": [
                {
                    "source": "verified_raw_document",
                    "payload": {
                        "role": "owned_web",
                        "url": "https://www.leeters.com/",
                        "content": (
                            "Encuentra talento real en horas.\n\n---\n"
                            "## Subpage: https://www.leeters.com/sofia\n"
                            "Ecosistema propietario para que el reclutamiento "
                            "vuelva a ser estrategico."
                        ),
                        "extracted_document_sha256": "a" * 64,
                        "extractor_version": (
                            "evidence-vault-deterministic-extractor-v1"
                        ),
                        "receipt_fingerprint": fingerprint,
                    },
                }
            ],
        },
        include_acquisition_steps=False,
    )
    urls = [record.url for record in pack.evidence]
    assert "https://www.leeters.com/" in urls
    assert "https://www.leeters.com/sofia" in urls
    sofia = next(
        record
        for record in pack.evidence
        if record.url == "https://www.leeters.com/sofia"
    )
    assert "estrategico" in sofia.content
    assert sofia.metadata.get("verified_raw") is True


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

    def stream(self, method: str, url: str, **kwargs: Any) -> _Response:
        assert method == "POST"
        self.calls.append({"url": url, **kwargs})
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


def test_exa_fetcher_rejects_brand_url_without_owned_linkedin_fact() -> None:
    class NoPostClient:
        def stream(self, *_args: Any, **_kwargs: Any) -> _Response:
            raise AssertionError("unassociated discovery must not reach Exa")

    fetcher = HttpxExaExactUrlFetcher(NoPostClient(), api_key="worker-only")
    assert fetcher.supports_independent_discovery is True
    with pytest.raises(
        EvidenceVaultAcquisitionRuntimeError,
        match="external_source_url_ineligible",
    ):
        fetcher("https://example.com")


def test_http_fetchers_stream_and_reject_oversized_bodies() -> None:
    huge_owned = _Response(
        200,
        b"x" * 2_097_153,
        {"content-type": "text/plain"},
    )
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="response_size"):
        HttpxOwnedFetcher(
            _GetClient([huge_owned]), resolver=_global_resolver
        )(_command())

    huge_provider = _Response(
        200,
        b"x" * 4_194_305,
        {"content-type": "application/json"},
    )
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="response_size"):
        HttpxExaExactUrlFetcher(
            _PostClient(huge_provider), api_key="worker-only"
        )("https://www.linkedin.com/company/example")


def test_http_fetcher_rejects_private_connected_peer_even_after_public_dns() -> None:
    response = _Response(200, b"durable", {"content-type": "text/plain"})
    response.extensions = {"network_stream": _NetworkStream("127.0.0.1")}
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="peer_scope"):
        HttpxOwnedFetcher(
            _GetClient([response]), resolver=_global_resolver
        )(_command())


def test_independent_exa_search_does_not_sign_c7_without_owned_link() -> None:
    key, registry = _key_registry()

    class IndependentFetcher:
        supports_independent_discovery = True

        def __init__(self) -> None:
            self.fetch_calls: list[str] = []
            self.discover_calls: list[str] = []

        def discover(self, command: Any) -> dict[str, Any]:
            self.discover_calls.append(command.brand_url)
            return {
                "schema_version": "evidence-vault-external-discovery-v1",
                "provider": "exa",
                "status": "searched",
                "request_fingerprint": "b" * 64,
                "candidate_urls": ["https://www.linkedin.com/company/example"],
            }

        def __call__(self, source_url: str):
            self.fetch_calls.append(source_url)
            return _external()

    fetcher = IndependentFetcher()
    runtime = TrustedAcquisitionRuntime(
        private_key=key,
        public_key_registry=registry,
        owned_fetch=lambda _command: _owned(linkedin=False),
        external_fetch=fetcher,
        allow_owned_only_downgrade=False,
    )

    collected = runtime.collect(_command())
    signed = runtime.sign(_command(), collected)

    assert fetcher.discover_calls == ["https://example.com"]
    assert fetcher.fetch_calls == []
    assert collected["external_outcome"] == "not_discovered"
    assert collected["external"] is None
    assert collected["external_discovery"]["candidate_urls"] == [
        "https://www.linkedin.com/company/example"
    ]
    assert signed.external_identity_provenance is None
    assert signed.pre_receipt_snapshot.raw_payload["external_discovery"][
        "status"
    ] == "searched"
    assert [receipt.claims.channel_role for receipt in signed.receipts] == [
        "owned_web"
    ]


def test_exa_discover_keeps_linkedin_company_candidates() -> None:
    body = json.dumps(
        {
            "results": [
                {"url": "https://linkedin.com/company/example/"},
                {"url": "https://www.linkedin.com/company/example"},
                {"url": "https://news.example/post"},
            ]
        }
    ).encode()
    client = _PostClient(
        _Response(200, body, {"content-type": "application/json"})
    )
    discovery = HttpxExaExactUrlFetcher(client, api_key="worker-only").discover(
        _command()
    )
    assert discovery == ExternalDiscovery(
        schema_version="evidence-vault-external-discovery-v1",
        provider="exa",
        status="searched",
        request_fingerprint=discovery.request_fingerprint,
        candidate_urls=["https://www.linkedin.com/company/example"],
    )
    assert "https://api.exa.ai/search" in client.calls[0]["url"]


def test_external_failure_is_terminal_by_default_and_not_discovered_is_explicit() -> None:
    key, registry = _key_registry()

    def unavailable(_url: str):
        raise EvidenceVaultAcquisitionRuntimeError("provider unavailable")

    strict_runtime = TrustedAcquisitionRuntime(
        private_key=key,
        public_key_registry=registry,
        owned_fetch=lambda _command: _owned(),
        external_fetch=unavailable,
    )
    with pytest.raises(
        EvidenceVaultAcquisitionRuntimeError, match="external_collection_failed"
    ):
        strict_runtime.collect(_command())

    no_link_runtime = TrustedAcquisitionRuntime(
        private_key=key,
        public_key_registry=registry,
        owned_fetch=lambda _command: _owned(linkedin=False),
    )
    collected = no_link_runtime.collect(_command())
    assert collected["external_outcome"] == "not_discovered"
    assert collected["external_failure_reason"] is None
    signed = no_link_runtime.sign(_command(), collected)
    assert signed.pre_receipt_snapshot.raw_payload["acquisition_outcome"] == {
        "schema_version": "evidence-vault-acquisition-outcome-v1",
        "external": "not_discovered",
        "failure_reason": None,
    }


class _BackendStream:
    def __init__(self, address: str) -> None:
        self.address = address
        self.closed = False

    def get_extra_info(self, name: str):
        assert name == "server_addr"
        return (self.address, 443)

    def close(self) -> None:
        self.closed = True


class _BackendDelegate:
    def __init__(self, address: str) -> None:
        self.stream = _BackendStream(address)
        self.connect_calls = 0

    def connect_tcp(self, *_args, **_kwargs):
        self.connect_calls += 1
        return self.stream

    def sleep(self, _seconds: float) -> None:
        return None


def test_connect_backend_blocks_private_resolution_and_rebound_peer_pre_http() -> None:
    private_delegate = _BackendDelegate("93.184.216.34")
    private_dns = lambda *_args, **_kwargs: [
        (None, None, None, None, ("127.0.0.1", 443))
    ]
    backend = PublicOnlyNetworkBackend(
        resolver=private_dns, delegate=private_delegate
    )
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="dns_scope"):
        backend.connect_tcp("example.com", 443)
    assert private_delegate.connect_calls == 0

    rebound_delegate = _BackendDelegate("127.0.0.1")
    backend = PublicOnlyNetworkBackend(
        resolver=_global_resolver, delegate=rebound_delegate
    )
    with pytest.raises(EvidenceVaultAcquisitionRuntimeError, match="peer_scope"):
        backend.connect_tcp("example.com", 443)
    assert rebound_delegate.connect_calls == 1
    assert rebound_delegate.stream.closed is True


def test_http_fetcher_requires_identity_encoding_and_exact_www_linkedin() -> None:
    encoded = _Response(
        200, b"compressed",
        {"content-type": "text/plain", "content-encoding": "gzip"},
    )
    with pytest.raises(
        EvidenceVaultAcquisitionRuntimeError, match="content_encoding"
    ):
        HttpxOwnedFetcher(
            _GetClient([encoded]), resolver=_global_resolver
        )(_command())

    body = b'<a href="https://linkedin.com/company/example">not canonical</a>'
    observation = HttpxOwnedFetcher(
        _GetClient([_Response(200, body, {"content-type": "text/html"})]),
        resolver=_global_resolver,
    )(_command())
    assert "linkedin" not in observation.raw_fragment

    trailing = b'<a href="https://www.linkedin.com/company/example/">canonical company</a>'
    observation = HttpxOwnedFetcher(
        _GetClient([_Response(200, trailing, {"content-type": "text/html"})]),
        resolver=_global_resolver,
    )(_command())
    assert observation.raw_fragment["linkedin"] == (
        "https://www.linkedin.com/company/example"
    )


def test_explicit_downgrade_distinguishes_ineligible_provider_result() -> None:
    key, registry = _key_registry()
    runtime = TrustedAcquisitionRuntime(
        private_key=key,
        public_key_registry=registry,
        owned_fetch=lambda _command: _owned(),
        external_fetch=lambda _url: {"malformed": True},
        allow_owned_only_downgrade=True,
    )
    collected = runtime.collect(_command())
    assert collected["external_outcome"] == "owned_only_downgrade"
    assert collected["external_failure_reason"] == "provider_result_ineligible"
