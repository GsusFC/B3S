"""Worker-only live collection and receipt signing for verified raw acquisition."""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import ipaddress
import json
import socket
import ssl
from typing import Any, Callable, Literal, Mapping, Protocol

import httpcore
import httpx
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from src.services.evidence_vault_acquisition_contract import TrustedAcquisitionCommand
from src.services.evidence_vault_acquisition_worker import SignedAcquisition
from src.services.evidence_vault_canonical_core import canonical_fingerprint, canonical_json
from src.services.evidence_vault_raw_capture import (
    DETERMINISTIC_EXTRACTOR_VERSION,
    build_signed_raw_capture,
    prepare_deterministic_document_for_signing,
    validate_signed_raw_capture,
)
from src.services.evidence_vault_raw_provenance import (
    C7_LIVE_FRESHNESS_POLICY_VERSION,
    PRE_RECEIPT_SNAPSHOT_VERSION,
    RAW_ACQUISITION_RECEIPT_VERSION,
    DirectAcquisition,
    ExternalIdentityProvenance,
    PreReceiptSnapshot,
    ProviderApiAcquisition,
    PublicKeyRegistry,
    RawAcquisitionReceipt,
    RawAcquisitionReceiptClaims,
    RedirectHop,
    evidence_memory_source_identity_id,
    external_identity_provenance_fingerprint,
    pre_receipt_snapshot_sha256,
    receipt_set_fingerprint,
    sign_raw_acquisition_receipt,
)


_MAX_CAPTURE_BYTES = 2_097_152
_MAX_PROVIDER_BYTES = 4_194_304
_MAX_REDIRECTS = 10
_SAFE_HEADERS = {
    "accept-ranges",
    "age",
    "cache-control",
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
}


class EvidenceVaultAcquisitionRuntimeError(RuntimeError):
    """Worker-local live acquisition is not eligible for a signed receipt."""


class _ExternalProviderResultError(EvidenceVaultAcquisitionRuntimeError):
    """The provider responded, but its exact result cannot qualify."""


class _StrictRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OwnedHttpObservation(_StrictRuntimeModel):
    requested_url: str
    redirect_chain: list[RedirectHop] = Field(max_length=_MAX_REDIRECTS)
    final_url: str
    fetched_at: str
    status_code: int = Field(ge=200, le=299)
    selected_headers: dict[str, str]
    media_type: str
    byte_count: int = Field(gt=0, le=_MAX_CAPTURE_BYTES)
    raw_fragment: dict[str, JsonValue]


class ExternalProviderObservation(_StrictRuntimeModel):
    provider_request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_ordinal: int = Field(ge=0, le=9999)
    reported_source_url: str
    fetched_at: str
    status_code: int = Field(ge=200, le=299)
    selected_headers: dict[str, str]
    media_type: str
    byte_count: int = Field(gt=0, le=_MAX_PROVIDER_BYTES)
    raw_fragment: dict[str, JsonValue]


class CollectedAcquisition(_StrictRuntimeModel):
    owned: OwnedHttpObservation
    external: ExternalProviderObservation | None
    external_outcome: Literal[
        "not_discovered",
        "captured",
        "owned_only_downgrade",
    ]
    external_failure_reason: Literal[
        "provider_not_configured",
        "provider_unavailable",
        "provider_result_ineligible",
    ] | None


class OwnedFetcher(Protocol):
    def __call__(
        self,
        command: TrustedAcquisitionCommand,
    ) -> OwnedHttpObservation | Mapping[str, Any]: ...


class ExternalFetcher(Protocol):
    def __call__(
        self,
        source_url: str,
    ) -> ExternalProviderObservation | Mapping[str, Any]: ...


class TrustedAcquisitionRuntime:
    """Worker-local collect/sign capabilities; this is not an IPC surface."""

    __slots__ = (
        "__allow_owned_only_downgrade",
        "__external_fetch",
        "__owned_fetch",
        "__private_key",
        "__registry_json",
    )

    def __init__(
        self,
        *,
        private_key: Ed25519PrivateKey,
        public_key_registry: PublicKeyRegistry | Mapping[str, Any],
        owned_fetch: OwnedFetcher,
        external_fetch: ExternalFetcher | None = None,
        allow_owned_only_downgrade: bool = False,
    ) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise EvidenceVaultAcquisitionRuntimeError("worker_private_key_invalid")
        if not callable(owned_fetch) or (
            external_fetch is not None and not callable(external_fetch)
        ):
            raise EvidenceVaultAcquisitionRuntimeError(
                "worker_fetch_capability_invalid"
            )
        if not isinstance(allow_owned_only_downgrade, bool):
            raise EvidenceVaultAcquisitionRuntimeError(
                "worker_downgrade_policy_invalid"
            )
        try:
            registry = PublicKeyRegistry.model_validate(
                public_key_registry.model_dump(mode="json")
                if isinstance(public_key_registry, PublicKeyRegistry)
                else deepcopy(dict(public_key_registry)),
                strict=True,
            )
            current = registry.keys[registry.current_key_id]
            actual_public = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            expected_public = base64.b64decode(
                current.public_key_base64,
                validate=True,
            )
            if current.status != "current" or actual_public != expected_public:
                raise ValueError("private key does not match current registry key")
        except Exception:
            registry = None
        if registry is None:
            raise EvidenceVaultAcquisitionRuntimeError("worker_key_registry_mismatch")
        self.__allow_owned_only_downgrade = allow_owned_only_downgrade
        self.__private_key = private_key
        self.__registry_json = registry.model_dump_json()
        self.__owned_fetch = owned_fetch
        self.__external_fetch = external_fetch

    def collect(self, command: TrustedAcquisitionCommand) -> Mapping[str, Any]:
        validated = _command(command)
        try:
            owned = _model(self.__owned_fetch(validated), OwnedHttpObservation)
            _validate_owned_observation(validated, owned)
        except Exception:
            owned = None
        if owned is None:
            raise EvidenceVaultAcquisitionRuntimeError("owned_collection_failed")
        linkedin_url = _owned_linkedin_fact(owned.raw_fragment)
        independent_external = bool(
            getattr(self.__external_fetch, "supports_independent_discovery", False)
        )
        external_attempted = linkedin_url is not None or independent_external
        external: ExternalProviderObservation | None = None
        outcome: Literal[
            "not_discovered",
            "captured",
            "owned_only_downgrade",
        ] = "not_discovered"
        failure_reason: Literal[
            "provider_not_configured",
            "provider_unavailable",
            "provider_result_ineligible",
        ] | None = None
        if external_attempted:
            if self.__external_fetch is None:
                failure_reason = "provider_not_configured"
            else:
                try:
                    candidate_value = self.__external_fetch(
                        linkedin_url if linkedin_url is not None else validated.brand_url
                    )
                except _ExternalProviderResultError:
                    failure_reason = "provider_result_ineligible"
                except EvidenceVaultAcquisitionRuntimeError:
                    failure_reason = "provider_unavailable"
                else:
                    try:
                        candidate = _model(
                            candidate_value,
                            ExternalProviderObservation,
                        )
                        _validate_external_observation(
                            candidate.reported_source_url,
                            candidate,
                        )
                    except (ValidationError, ValueError):
                        failure_reason = "provider_result_ineligible"
                    else:
                        external = candidate
                        outcome = "captured"
            if external is None:
                if not self.__allow_owned_only_downgrade:
                    raise EvidenceVaultAcquisitionRuntimeError(
                        "external_collection_failed"
                    )
                outcome = "owned_only_downgrade"
        return CollectedAcquisition(
            owned=owned,
            external=external,
            external_outcome=outcome,
            external_failure_reason=failure_reason,
        ).model_dump(mode="json")

    def sign(
        self,
        command: TrustedAcquisitionCommand,
        collected: Mapping[str, Any],
    ) -> SignedAcquisition:
        validated = _command(command)
        try:
            frozen = _model(collected, CollectedAcquisition)
            _validate_owned_observation(validated, frozen.owned)
            linkedin_url = _owned_linkedin_fact(frozen.owned.raw_fragment)
            if frozen.external is not None:
                _validate_external_observation(
                    frozen.external.reported_source_url,
                    frozen.external,
                )
            _validate_external_outcome(
                frozen,
                external_attempted=(
                    linkedin_url is not None
                    or bool(
                        getattr(
                            self.__external_fetch,
                            "supports_independent_discovery",
                            False,
                        )
                    )
                ),
                allow_owned_only_downgrade=self.__allow_owned_only_downgrade,
            )
            registry = PublicKeyRegistry.model_validate_json(
                self.__registry_json,
                strict=True,
            )
            return _sign_collected(
                validated,
                frozen,
                private_key=self.__private_key,
                registry=registry,
            )
        except Exception:
            signed = None
        if signed is None:
            raise EvidenceVaultAcquisitionRuntimeError("acquisition_signing_failed")
        return signed


class PublicOnlyNetworkBackend(httpcore.NetworkBackend):
    """Reject non-global resolution and connected peers before HTTP bytes exist."""

    def __init__(
        self,
        *,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        delegate: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._delegate = delegate or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        resolved = _require_public_resolution(
            host,
            port=port,
            resolver=self._resolver,
        )
        selected_address = resolved[0]
        stream = self._delegate.connect_tcp(
            selected_address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        try:
            server_address = stream.get_extra_info("server_addr")
            address = server_address[0]
            peer = ipaddress.ip_address(address)
            resolved_addresses = {
                ipaddress.ip_address(item) for item in resolved
            }
            if not peer.is_global or peer not in resolved_addresses:
                raise ValueError("connected peer is not the pinned global address")
        except Exception:
            stream.close()
            raise EvidenceVaultAcquisitionRuntimeError(
                "network_peer_scope_invalid"
            ) from None
        return stream

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        del path, timeout, socket_options
        raise EvidenceVaultAcquisitionRuntimeError(
            "network_unix_socket_denied"
        )

    def sleep(self, seconds: float) -> None:
        self._delegate.sleep(seconds)


class PublicOnlyHTTPTransport(httpx.HTTPTransport):
    """HTTPX transport whose network backend enforces public peers pre-request."""

    def __init__(self) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=10,
            max_keepalive_connections=0,
            retries=0,
            network_backend=PublicOnlyNetworkBackend(),
        )


class HttpxOwnedFetcher:
    """Bounded direct-HTTPS fetcher with manual same-brand redirects."""

    __slots__ = ("_client", "_resolver")

    def __init__(
        self,
        client: Any,
        *,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ) -> None:
        self._client = client
        self._resolver = resolver

    def __call__(self, command: TrustedAcquisitionCommand) -> OwnedHttpObservation:
        canonical_domain = urlsplit(command.brand_url).hostname or ""
        current = command.brand_url
        redirects: list[RedirectHop] = []
        for _ in range(_MAX_REDIRECTS + 1):
            _require_public_same_brand_url(
                current,
                canonical_domain=canonical_domain,
                resolver=self._resolver,
            )
            with self._client.stream(
                "GET",
                current,
                headers={
                    "accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                    "accept-encoding": "identity",
                    "user-agent": "B3S-Evidence-Vault-Acquisition/1",
                },
                follow_redirects=False,
                timeout=20.0,
            ) as response:
                _require_public_peer(response)
                _require_identity_encoding(response.headers)
                status = int(response.status_code)
                if status in {300, 301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not isinstance(location, str) or not location:
                        raise EvidenceVaultAcquisitionRuntimeError(
                            "owned_redirect_invalid"
                        )
                    destination = urljoin(current, location)
                    _require_public_same_brand_url(
                        destination,
                        canonical_domain=canonical_domain,
                        resolver=self._resolver,
                    )
                    redirects.append(
                        RedirectHop(
                            request_url=current,
                            status_code=status,
                            location_url=destination,
                        )
                    )
                    current = destination
                    continue
                if not 200 <= status <= 299:
                    raise EvidenceVaultAcquisitionRuntimeError(
                        "owned_http_status_ineligible"
                    )
                body = _read_bounded_body(response, maximum=_MAX_CAPTURE_BYTES)
                media_type, charset = _media_type_and_charset(
                    response.headers.get("content-type")
                )
                if media_type not in {
                    "text/html",
                    "application/xhtml+xml",
                    "text/plain",
                }:
                    raise EvidenceVaultAcquisitionRuntimeError(
                        "owned_media_type_ineligible"
                    )
                try:
                    content = body.decode(charset, errors="strict")
                except (LookupError, UnicodeDecodeError):
                    raise EvidenceVaultAcquisitionRuntimeError(
                        "owned_text_decode_failed"
                    ) from None
                if not content.strip():
                    raise EvidenceVaultAcquisitionRuntimeError(
                        "owned_content_empty"
                    )
                if media_type in {"text/html", "application/xhtml+xml"}:
                    document_text, linkedin = _extract_owned_html(
                        content,
                        base_url=current,
                    )
                    if not document_text:
                        raise EvidenceVaultAcquisitionRuntimeError(
                            "owned_document_empty"
                        )
                    fragment: dict[str, JsonValue] = {
                        "url": current,
                        "raw_body": content,
                        "text": document_text,
                    }
                else:
                    fragment = {
                        "url": current,
                        "text": content,
                    }
                    linkedin = None
                if linkedin is not None:
                    fragment["linkedin"] = linkedin
                return OwnedHttpObservation(
                    requested_url=command.brand_url,
                    redirect_chain=redirects,
                    final_url=current,
                    fetched_at=_utc_now(),
                    status_code=status,
                    selected_headers=_selected_headers(response.headers),
                    media_type=media_type,
                    byte_count=len(body),
                    raw_fragment=fragment,
                )
        raise EvidenceVaultAcquisitionRuntimeError("owned_redirect_limit_exceeded")


class HttpxExaExactUrlFetcher:
    """Acquire an exact external profile, discovering it independently via Exa."""

    supports_independent_discovery = True
    __slots__ = ("_api_key", "_client")

    def __init__(self, client: Any, *, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise EvidenceVaultAcquisitionRuntimeError("exa_api_key_missing")
        self._client = client
        self._api_key = api_key.strip()

    def __call__(self, source_url: str) -> ExternalProviderObservation:
        discovery_fingerprint: str | None = None
        try:
            _strict_linkedin_company_url(source_url)
            target_url = source_url
            discovery_ordinal = None
        except ValueError:
            discovery_body = {
                "query": f"{source_url} company LinkedIn profile",
                "type": "auto",
                "numResults": 10,
            }
            discovery_fingerprint = canonical_fingerprint(
                "evidence-vault-exa-independent-discovery-request-v1",
                discovery_body,
            )
            discovery_payload, _status, _headers, _body_size = self._post_json(
                "https://api.exa.ai/search",
                discovery_body,
            )
            results = (
                discovery_payload.get("results")
                if isinstance(discovery_payload, Mapping)
                else None
            )
            if not isinstance(results, list):
                raise _ExternalProviderResultError("exa_discovery_results_invalid")
            matches: list[tuple[int, str]] = []
            for ordinal, item in enumerate(results):
                candidate = item.get("url") if isinstance(item, Mapping) else None
                if not isinstance(candidate, str):
                    continue
                try:
                    _strict_linkedin_company_url(candidate)
                except ValueError:
                    continue
                matches.append((ordinal, candidate))
            if len(matches) != 1:
                raise _ExternalProviderResultError(
                    "exa_independent_profile_not_unique"
                )
            discovery_ordinal, target_url = matches[0]

        request_body = {
            "ids": [target_url],
            "text": {"maxCharacters": 20_000},
            "highlights": {"maxCharacters": 4_000},
            "summary": {"query": "Exact company profile summary"},
            "livecrawl": "always",
        }
        request_fingerprint = canonical_fingerprint(
            "evidence-vault-exa-exact-url-request-v1",
            {
                "discovery_request_fingerprint": discovery_fingerprint,
                "contents_request": request_body,
            },
        )
        payload, status, selected_headers, body_size = self._post_json(
            "https://api.exa.ai/contents",
            request_body,
        )
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(results, list):
            raise _ExternalProviderResultError("exa_results_invalid")
        matches = [
            (ordinal, item)
            for ordinal, item in enumerate(results)
            if isinstance(item, Mapping) and item.get("url") == target_url
        ]
        if len(matches) != 1:
            raise _ExternalProviderResultError("exa_exact_result_not_unique")
        ordinal, item = matches[0]
        title = item.get("title")
        summary = item.get("summary")
        text = item.get("text")
        highlights = item.get("highlights")
        if (
            not all(isinstance(value, str) for value in (title, summary, text))
            or not isinstance(highlights, list)
            or not all(isinstance(value, str) for value in highlights)
        ):
            raise _ExternalProviderResultError("exa_result_fields_invalid")
        fragment: dict[str, JsonValue] = {
            "url": target_url,
            "title": title,
            "summary": summary,
            "highlights": list(highlights),
            "text": text,
        }
        if discovery_ordinal is not None:
            fragment["discovery_result_ordinal"] = discovery_ordinal
        return ExternalProviderObservation(
            provider_request_fingerprint=request_fingerprint,
            result_ordinal=ordinal,
            reported_source_url=target_url,
            fetched_at=_utc_now(),
            status_code=status,
            selected_headers=selected_headers,
            media_type="application/json",
            byte_count=body_size,
            raw_fragment=fragment,
        )

    def _post_json(
        self,
        endpoint: str,
        request_body: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], int, dict[str, str], int]:
        with self._client.stream(
            "POST",
            endpoint,
            headers={
                "accept": "application/json",
                "accept-encoding": "identity",
                "content-type": "application/json",
                "x-api-key": self._api_key,
            },
            content=canonical_json(dict(request_body)).encode("utf-8"),
            timeout=30.0,
        ) as response:
            _require_public_peer(response)
            _require_identity_encoding(response.headers)
            status = int(response.status_code)
            if not 200 <= status <= 299:
                raise EvidenceVaultAcquisitionRuntimeError("exa_response_ineligible")
            body = _read_bounded_body(response, maximum=_MAX_PROVIDER_BYTES)
            selected_headers = _selected_headers(response.headers)
            media_type, _charset = _media_type_and_charset(
                response.headers.get("content-type")
            )
        if media_type != "application/json":
            raise _ExternalProviderResultError("exa_media_type_ineligible")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _ExternalProviderResultError("exa_json_invalid") from None
        if not isinstance(payload, Mapping):
            raise _ExternalProviderResultError("exa_json_object_invalid")
        return payload, status, selected_headers, len(body)


def _sign_collected(
    command: TrustedAcquisitionCommand,
    collected: CollectedAcquisition,
    *,
    private_key: Ed25519PrivateKey,
    registry: PublicKeyRegistry,
) -> SignedAcquisition:
    raw_payload: dict[str, Any] = {
        "sources": {"owned": collected.owned.raw_fragment},
        "acquisition_outcome": {
            "schema_version": "evidence-vault-acquisition-outcome-v1",
            "external": collected.external_outcome,
            "failure_reason": collected.external_failure_reason,
        },
    }
    if collected.external is not None:
        raw_payload["sources"]["external"] = collected.external.raw_fragment
    snapshot = PreReceiptSnapshot(
        schema_version=PRE_RECEIPT_SNAPSHOT_VERSION,
        workspace_slug=command.workspace_slug,
        source_scan_id=command.source_scan_id,
        acquisition_session_id=str(uuid4()),
        canonical_brand_domain=urlsplit(command.brand_url).hostname or "",
        canonical_brand_url=command.brand_url,
        raw_payload=raw_payload,
    )
    snapshot_sha = pre_receipt_snapshot_sha256(snapshot)
    owned_receipt = _sign_observation(
        snapshot=snapshot,
        role="owned_web",
        observation=collected.owned,
        private_key=private_key,
        registry=registry,
        snapshot_sha=snapshot_sha,
        external_provenance_fingerprint=None,
    )
    receipts = [owned_receipt]
    association: ExternalIdentityProvenance | None = None
    if collected.external is not None:
        external_url = collected.external.reported_source_url
        owned_linkedin_url = _owned_linkedin_fact(collected.owned.raw_fragment)
        association_method = (
            "owned_raw_links_external_profile"
            if owned_linkedin_url is not None
            else "exa_independent_discovery"
        )
        association = ExternalIdentityProvenance(
            schema_version="external-identity-provenance-v1",
            policy_version="evidence-vault-external-identity-association-policy-v1",
            association_method=association_method,
            canonical_brand_domain=snapshot.canonical_brand_domain,
            owned_source_url=snapshot.canonical_brand_url,
            external_source_url=external_url,
            proof_receipt_fingerprint=owned_receipt.receipt_fingerprint,
            raw_fact_role=(
                "owned_web"
                if owned_linkedin_url is not None
                else "external_social_profile"
            ),
            raw_fact_json_pointer=(
                "/sources/owned/linkedin"
                if owned_linkedin_url is not None
                else "/sources/external/url"
            ),
            raw_fact_sha256=_json_fragment_sha256(external_url),
            source_identity_schema_version="evidence-memory-document-v2",
            owned_source_identity_id=evidence_memory_source_identity_id(
                source_url=snapshot.canonical_brand_url,
                raw_fact_role="owned_web",
            ),
            external_source_identity_id=evidence_memory_source_identity_id(
                source_url=external_url,
                raw_fact_role="external_social_profile",
            ),
        )
        receipts.append(
            _sign_observation(
                snapshot=snapshot,
                role="external_social_profile",
                observation=collected.external,
                private_key=private_key,
                registry=registry,
                snapshot_sha=snapshot_sha,
                external_provenance_fingerprint=(
                    external_identity_provenance_fingerprint(association)
                    if association is not None
                    else None
                ),
            )
        )

    receipts.sort(key=lambda receipt: receipt.receipt_fingerprint)
    signed = SignedAcquisition(
        pre_receipt_snapshot=snapshot,
        receipts=receipts,
        receipt_set_fingerprint=receipt_set_fingerprint(receipts),
        external_identity_provenance=association,
    )
    built = build_signed_raw_capture(
        snapshot,
        receipts,
        public_key_registry=registry,
        external_identity_provenance=association,
    )
    validate_signed_raw_capture(
        built.durable_raw_capture_payload,
        capture_content_hash=built.capture_content_hash,
        public_key_registry=registry,
    )
    return signed


def _sign_observation(
    *,
    snapshot: PreReceiptSnapshot,
    role: str,
    observation: OwnedHttpObservation | ExternalProviderObservation,
    private_key: Ed25519PrivateKey,
    registry: PublicKeyRegistry,
    snapshot_sha: str,
    external_provenance_fingerprint: str | None,
) -> RawAcquisitionReceipt:
    fragment = observation.raw_fragment
    extraction = prepare_deterministic_document_for_signing(
        channel_role=role,
        raw_fragment=fragment,
    )
    if role == "owned_web":
        if not isinstance(observation, OwnedHttpObservation):
            raise ValueError("owned observation union mismatch")
        provider = "direct_http"
        pointer = "/sources/owned"
        acquisition: Any = DirectAcquisition(
            acquisition_mode="direct_http",
            requested_url=observation.requested_url,
            redirect_chain=observation.redirect_chain,
            final_url=observation.final_url,
        )
    else:
        if not isinstance(observation, ExternalProviderObservation):
            raise ValueError("external observation union mismatch")
        provider = "exa"
        pointer = "/sources/external"
        acquisition = ProviderApiAcquisition(
            acquisition_mode="provider_api",
            provider_request_fingerprint=observation.provider_request_fingerprint,
            result_ordinal=observation.result_ordinal,
            reported_source_url=observation.reported_source_url,
            redirect_chain=[],
        )
    claims = RawAcquisitionReceiptClaims(
        schema_version=RAW_ACQUISITION_RECEIPT_VERSION,
        freshness_policy_version=C7_LIVE_FRESHNESS_POLICY_VERSION,
        key_id=registry.current_key_id,
        receipt_nonce=str(uuid4()),
        acquisition_session_id=snapshot.acquisition_session_id,
        workspace_slug=snapshot.workspace_slug,
        source_scan_id=snapshot.source_scan_id,
        canonical_brand_domain=snapshot.canonical_brand_domain,
        channel_role=role,
        pre_receipt_snapshot_sha256=snapshot_sha,
        provider=provider,
        acquisition=acquisition,
        fetched_at=observation.fetched_at,
        status_code=observation.status_code,
        selected_headers=observation.selected_headers,
        media_type=observation.media_type,
        byte_count=observation.byte_count,
        raw_fragment_json_pointer=pointer,
        raw_fragment_sha256=_json_fragment_sha256(fragment),
        extracted_document_sha256=extraction.sha256,
        extractor_version=DETERMINISTIC_EXTRACTOR_VERSION,
        external_identity_provenance_fingerprint=external_provenance_fingerprint,
    )
    return sign_raw_acquisition_receipt(
        claims,
        private_key=private_key,
        public_key_registry=registry,
    )


def _command(command: TrustedAcquisitionCommand) -> TrustedAcquisitionCommand:
    if not isinstance(command, TrustedAcquisitionCommand):
        raise EvidenceVaultAcquisitionRuntimeError("command_invalid")
    return TrustedAcquisitionCommand.model_validate(
        command.model_dump(mode="python", round_trip=True),
        strict=True,
    )


def _model(value: Any, model: type[BaseModel]) -> Any:
    if isinstance(value, model):
        value = value.model_dump(mode="python", round_trip=True)
    if not isinstance(value, Mapping):
        raise ValueError("runtime capability result must be an object")
    return model.model_validate(deepcopy(dict(value)), strict=True)


def _validate_external_outcome(
    collected: CollectedAcquisition,
    *,
    external_attempted: bool,
    allow_owned_only_downgrade: bool,
) -> None:
    if collected.external_outcome == "captured":
        if (
            collected.external is None
            or not external_attempted
            or collected.external_failure_reason is not None
        ):
            raise ValueError("captured external outcome is inconsistent")
        return
    if collected.external_outcome == "not_discovered":
        if (
            collected.external is not None
            or external_attempted
            or collected.external_failure_reason is not None
        ):
            raise ValueError("not-discovered external outcome is inconsistent")
        return
    if (
        collected.external_outcome != "owned_only_downgrade"
        or collected.external is not None
        or not external_attempted
        or collected.external_failure_reason is None
        or not allow_owned_only_downgrade
    ):
        raise ValueError("owned-only downgrade is inconsistent or denied")


def _validate_owned_observation(
    command: TrustedAcquisitionCommand,
    observation: OwnedHttpObservation,
) -> None:
    direct = DirectAcquisition(
        acquisition_mode="direct_http",
        requested_url=observation.requested_url,
        redirect_chain=observation.redirect_chain,
        final_url=observation.final_url,
    )
    if direct.requested_url != command.brand_url:
        raise ValueError("owned requested URL differs from command")
    canonical = urlsplit(command.brand_url).hostname or ""
    for value in [direct.requested_url, direct.final_url, *[
        url for hop in direct.redirect_chain for url in (hop.request_url, hop.location_url)
    ]]:
        if _canonical_host(value) != canonical:
            raise ValueError("owned observation leaves canonical brand")
    if observation.raw_fragment.get("url") != observation.final_url:
        raise ValueError("owned raw URL differs from final URL")
    prepare_deterministic_document_for_signing(
        channel_role="owned_web",
        raw_fragment=observation.raw_fragment,
    )
    _utc_text(observation.fetched_at)


def _validate_external_observation(
    source_url: str,
    observation: ExternalProviderObservation,
) -> None:
    _strict_linkedin_company_url(source_url)
    if observation.reported_source_url != source_url:
        raise ValueError("external result URL differs from owned fact")
    if observation.raw_fragment.get("url") != source_url:
        raise ValueError("external raw URL differs from provider result")
    prepare_deterministic_document_for_signing(
        channel_role="external_social_profile",
        raw_fragment=observation.raw_fragment,
    )
    _utc_text(observation.fetched_at)


def _owned_linkedin_fact(fragment: Mapping[str, Any]) -> str | None:
    value = fragment.get("linkedin")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("owned LinkedIn fact must be text")
    _strict_linkedin_company_url(value)
    return value


class _OwnedHtmlParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: set[str] = set()
        self.text_parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self._suppressed_depth += 1
        for name, value in attrs:
            if name.lower() != "href" or not isinstance(value, str):
                continue
            candidate = urljoin(self.base_url, value.strip())
            # Public company pages commonly publish one trailing slash, while
            # the signed external target is canonicalized without it. Only
            # remove that single safe suffix; query/fragment and other
            # non-canonical forms remain rejected by the strict validator.
            if candidate.endswith("/"):
                candidate = candidate[:-1]
            try:
                _strict_linkedin_company_url(candidate)
            except ValueError:
                continue
            self.links.add(candidate)

    def handle_endtag(self, tag: str) -> None:
        if (
            tag.lower() in {"script", "style", "noscript", "template"}
            and self._suppressed_depth
        ):
            self._suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth and data.strip():
            self.text_parts.append(data)


def _extract_owned_html(
    content: str,
    *,
    base_url: str,
) -> tuple[str, str | None]:
    parser = _OwnedHtmlParser(base_url)
    try:
        parser.feed(content)
        parser.close()
    except Exception:
        raise EvidenceVaultAcquisitionRuntimeError(
            "owned_html_parse_failed"
        ) from None
    document = " ".join(" ".join(parser.text_parts).split()).strip()
    linkedin = next(iter(parser.links)) if len(parser.links) == 1 else None
    return document, linkedin


def _strict_linkedin_company_url(value: str) -> None:
    if not isinstance(value, str) or value != value.strip() or not value.isascii():
        raise ValueError("LinkedIn company URL is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.linkedin.com"
        or parsed.hostname != "www.linkedin.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.endswith("/")
    ):
        raise ValueError("LinkedIn company URL is invalid")
    parts = parsed.path.split("/")
    slug = parts[2] if len(parts) == 3 and parts[1] == "company" else ""
    if (
        not slug
        or len(slug) > 100
        or slug != slug.lower()
        or not slug[0].isalnum()
        or not slug[-1].isalnum()
        or any(not (character.isalnum() or character == "-") for character in slug)
    ):
        raise ValueError("LinkedIn company URL is invalid")


def _require_public_peer(response: Any) -> None:
    try:
        network_stream = response.extensions["network_stream"]
        server_address = network_stream.get_extra_info("server_addr")
        address = server_address[0]
        if not isinstance(address, str) or not ipaddress.ip_address(address).is_global:
            raise ValueError("peer is not global")
    except Exception:
        raise EvidenceVaultAcquisitionRuntimeError("network_peer_scope_invalid") from None


def _require_public_resolution(
    host: str,
    *,
    port: int,
    resolver: Callable[..., Any],
) -> tuple[str, ...]:
    try:
        answers = resolver(host, port, type=socket.SOCK_STREAM)
        parsed_addresses = {
            ipaddress.ip_address(item[4][0]) for item in answers
        }
        if not parsed_addresses or any(
            not address.is_global for address in parsed_addresses
        ):
            raise ValueError("non-global address")
        return tuple(
            str(address)
            for address in sorted(
                parsed_addresses,
                key=lambda item: (item.version, int(item)),
            )
        )
    except Exception:
        raise EvidenceVaultAcquisitionRuntimeError(
            "network_dns_scope_invalid"
        ) from None


def _require_public_same_brand_url(
    value: str,
    *,
    canonical_domain: str,
    resolver: Callable[..., Any],
) -> None:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
        or _canonical_host(value) != canonical_domain
    ):
        raise EvidenceVaultAcquisitionRuntimeError("owned_url_scope_invalid")
    try:
        _require_public_resolution(host, port=443, resolver=resolver)
    except EvidenceVaultAcquisitionRuntimeError:
        raise EvidenceVaultAcquisitionRuntimeError(
            "owned_dns_scope_invalid"
        ) from None


def _canonical_host(value: str) -> str:
    return (urlsplit(value).hostname or "").lower().removeprefix("www.")


def _selected_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for name, value in headers.items():
        canonical = str(name).lower()
        if canonical in _SAFE_HEADERS:
            selected[canonical] = str(value)
    return selected


def _require_identity_encoding(headers: Mapping[str, Any]) -> None:
    value = headers.get("content-encoding")
    if value is not None and str(value).strip().lower() not in {"", "identity"}:
        raise EvidenceVaultAcquisitionRuntimeError(
            "response_content_encoding_ineligible"
        )


def _read_bounded_body(response: Any, *, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    try:
        iterator = response.iter_raw()
    except Exception:
        raise EvidenceVaultAcquisitionRuntimeError("response_stream_invalid") from None
    for chunk in iterator:
        if not isinstance(chunk, bytes):
            raise EvidenceVaultAcquisitionRuntimeError("response_stream_invalid")
        size += len(chunk)
        if size > maximum:
            raise EvidenceVaultAcquisitionRuntimeError("response_size_invalid")
        chunks.append(chunk)
    if size == 0:
        raise EvidenceVaultAcquisitionRuntimeError("response_size_invalid")
    return b"".join(chunks)


def _media_type_and_charset(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceVaultAcquisitionRuntimeError("content_type_missing")
    parts = [part.strip() for part in value.split(";")]
    media_type = parts[0].lower()
    charset = "utf-8"
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip(' "').lower()
    return media_type, charset


def _json_fragment_sha256(value: Any) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _utc_text(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp is not canonical UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError("timestamp is naive")
    return parsed


__all__ = [
    "CollectedAcquisition",
    "EvidenceVaultAcquisitionRuntimeError",
    "ExternalProviderObservation",
    "HttpxExaExactUrlFetcher",
    "HttpxOwnedFetcher",
    "OwnedHttpObservation",
    "PublicOnlyHTTPTransport",
    "PublicOnlyNetworkBackend",
    "TrustedAcquisitionRuntime",
]
