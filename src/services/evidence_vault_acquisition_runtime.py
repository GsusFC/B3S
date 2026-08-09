"""Worker-only live collection and receipt signing for verified raw acquisition."""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import ipaddress
import json
import socket
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, JsonValue

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
    ) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise EvidenceVaultAcquisitionRuntimeError("worker_private_key_invalid")
        if not callable(owned_fetch) or (external_fetch is not None and not callable(external_fetch)):
            raise EvidenceVaultAcquisitionRuntimeError("worker_fetch_capability_invalid")
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
        external: ExternalProviderObservation | None = None
        if linkedin_url is not None and self.__external_fetch is not None:
            try:
                candidate = _model(
                    self.__external_fetch(linkedin_url),
                    ExternalProviderObservation,
                )
                _validate_external_observation(linkedin_url, candidate)
                external = candidate
            except Exception:
                external = None
        return CollectedAcquisition(
            owned=owned,
            external=external,
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
            if frozen.external is not None:
                linkedin_url = _owned_linkedin_fact(frozen.owned.raw_fragment)
                if linkedin_url is None:
                    raise ValueError("external collection has no owned raw link")
                _validate_external_observation(linkedin_url, frozen.external)
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
            response = self._client.get(
                current,
                headers={
                    "accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                    "user-agent": "B3S-Evidence-Vault-Acquisition/1",
                },
                follow_redirects=False,
                timeout=20.0,
            )
            status = int(response.status_code)
            if status in {300, 301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not isinstance(location, str) or not location:
                    raise EvidenceVaultAcquisitionRuntimeError("owned_redirect_invalid")
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
                raise EvidenceVaultAcquisitionRuntimeError("owned_http_status_ineligible")
            body = bytes(response.content)
            if not 1 <= len(body) <= _MAX_CAPTURE_BYTES:
                raise EvidenceVaultAcquisitionRuntimeError("owned_response_size_invalid")
            media_type, charset = _media_type_and_charset(response.headers.get("content-type"))
            if media_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise EvidenceVaultAcquisitionRuntimeError("owned_media_type_ineligible")
            try:
                content = body.decode(charset, errors="strict")
            except (LookupError, UnicodeDecodeError):
                raise EvidenceVaultAcquisitionRuntimeError("owned_text_decode_failed") from None
            if not content.strip():
                raise EvidenceVaultAcquisitionRuntimeError("owned_content_empty")
            fragment: dict[str, JsonValue] = {"url": current, "content": content}
            linkedin = _discover_linkedin_company_url(content, base_url=current)
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
    """Call Exa's exact-URL contents endpoint and retain real HTTP metadata."""

    __slots__ = ("_api_key", "_client")

    def __init__(self, client: Any, *, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise EvidenceVaultAcquisitionRuntimeError("exa_api_key_missing")
        self._client = client
        self._api_key = api_key.strip()

    def __call__(self, source_url: str) -> ExternalProviderObservation:
        _strict_linkedin_company_url(source_url)
        request_body = {
            "ids": [source_url],
            "text": {"maxCharacters": 20_000},
            "highlights": {"maxCharacters": 4_000},
            "summary": {"query": "Exact company profile summary"},
            "livecrawl": "always",
        }
        request_fingerprint = canonical_fingerprint(
            "evidence-vault-exa-exact-url-request-v1",
            request_body,
        )
        response = self._client.post(
            "https://api.exa.ai/contents",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "x-api-key": self._api_key,
            },
            content=canonical_json(request_body).encode("utf-8"),
            timeout=30.0,
        )
        status = int(response.status_code)
        body = bytes(response.content)
        if not 200 <= status <= 299 or not 1 <= len(body) <= _MAX_PROVIDER_BYTES:
            raise EvidenceVaultAcquisitionRuntimeError("exa_response_ineligible")
        media_type, _charset = _media_type_and_charset(response.headers.get("content-type"))
        if media_type != "application/json":
            raise EvidenceVaultAcquisitionRuntimeError("exa_media_type_ineligible")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EvidenceVaultAcquisitionRuntimeError("exa_json_invalid") from None
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(results, list):
            raise EvidenceVaultAcquisitionRuntimeError("exa_results_invalid")
        matches: list[tuple[int, Mapping[str, Any]]] = []
        for ordinal, item in enumerate(results):
            if isinstance(item, Mapping) and item.get("url") == source_url:
                matches.append((ordinal, item))
        if len(matches) != 1:
            raise EvidenceVaultAcquisitionRuntimeError("exa_exact_result_not_unique")
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
            raise EvidenceVaultAcquisitionRuntimeError("exa_result_fields_invalid")
        fragment: dict[str, JsonValue] = {
            "url": source_url,
            "title": title,
            "summary": summary,
            "highlights": list(highlights),
            "text": text,
        }
        return ExternalProviderObservation(
            provider_request_fingerprint=request_fingerprint,
            result_ordinal=ordinal,
            reported_source_url=source_url,
            fetched_at=_utc_now(),
            status_code=status,
            selected_headers=_selected_headers(response.headers),
            media_type=media_type,
            byte_count=len(body),
            raw_fragment=fragment,
        )


def _sign_collected(
    command: TrustedAcquisitionCommand,
    collected: CollectedAcquisition,
    *,
    private_key: Ed25519PrivateKey,
    registry: PublicKeyRegistry,
) -> SignedAcquisition:
    raw_payload: dict[str, Any] = {
        "sources": {"owned": collected.owned.raw_fragment}
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
        association = ExternalIdentityProvenance(
            schema_version="external-identity-provenance-v1",
            policy_version="evidence-vault-external-identity-association-policy-v1",
            association_method="owned_raw_links_external_profile",
            canonical_brand_domain=snapshot.canonical_brand_domain,
            owned_source_url=snapshot.canonical_brand_url,
            external_source_url=external_url,
            proof_receipt_fingerprint=owned_receipt.receipt_fingerprint,
            raw_fact_role="owned_web",
            raw_fact_json_pointer="/sources/owned/linkedin",
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


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: set[str] = set()

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() != "href" or not isinstance(value, str):
                continue
            candidate = urljoin(self.base_url, value.strip())
            try:
                _strict_linkedin_company_url(candidate)
            except ValueError:
                continue
            self.links.add(candidate)


def _discover_linkedin_company_url(content: str, *, base_url: str) -> str | None:
    parser = _LinkParser(base_url)
    try:
        parser.feed(content)
        parser.close()
    except Exception:
        return None
    if len(parser.links) != 1:
        return None
    return next(iter(parser.links))


def _strict_linkedin_company_url(value: str) -> None:
    if not isinstance(value, str) or value != value.strip() or not value.isascii():
        raise ValueError("LinkedIn company URL is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower().removeprefix("www.") != "linkedin.com"
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
        answers = resolver(host, 443, type=socket.SOCK_STREAM)
        addresses = {item[4][0] for item in answers}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("non-global address")
    except Exception:
        raise EvidenceVaultAcquisitionRuntimeError("owned_dns_scope_invalid") from None


def _canonical_host(value: str) -> str:
    return (urlsplit(value).hostname or "").lower().removeprefix("www.")


def _selected_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for name, value in headers.items():
        canonical = str(name).lower()
        if canonical in _SAFE_HEADERS:
            selected[canonical] = str(value)
    return selected


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
    "TrustedAcquisitionRuntime",
]
