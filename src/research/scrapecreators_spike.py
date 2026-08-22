"""Isolated ScrapeCreators social-acquisition spike.

This module is deliberately not wired into Scanner, Evidence Vault, workers, or
runtime configuration. It only supports exact targets supplied in a manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.research.social_lab_contracts import (
    ActorRole,
    MetricContext,
    Provenance,
    SocialLabContractError,
    SocialObservation,
    build_social_observation,
)

API_BASE_URL = "https://api.scrapecreators.com"
API_KEY_ENV = "SCRAPECREATORS_API_KEY"
OUTPUT_SCHEMA_VERSION = "scrapecreators-social-spike.v1"
CONTRACT_OUTPUT_SCHEMA_VERSION = "b3s-social-community-lab-v1"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_SECONDS = 0.25
MAX_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 4
MAX_RETRY_DELAY_SECONDS = 2.0
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SENSITIVE_KEYS = frozenset({"x-api-key", "api-key", "api_key", "apikey", "authorization"})
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TWITTER_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_INSTAGRAM_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


class ScrapeCreatorsSpikeError(RuntimeError):
    """Base error whose message is safe to print (it never contains credentials/body data)."""


class ManifestError(ScrapeCreatorsSpikeError):
    """The explicit-target manifest is invalid."""


class MissingAPIKeyError(ScrapeCreatorsSpikeError):
    """The required environment-only credential is missing."""


class ProviderRequestError(ScrapeCreatorsSpikeError):
    """A provider request failed after the bounded retry policy."""


@dataclass(frozen=True)
class SocialTarget:
    target_id: str
    platform: str
    handle: str | None = None
    company_url: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {"target_id": self.target_id, "platform": self.platform}
        if self.handle is not None:
            result["handle"] = self.handle
        if self.company_url is not None:
            result["company_url"] = self.company_url
        return result


@dataclass(frozen=True)
class TargetManifest:
    schema_version: int
    targets: tuple[SocialTarget, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "targets": [target.as_dict() for target in self.targets],
        }

    @property
    def sha256(self) -> str:
        return _sha256_bytes(_canonical_json_bytes(self.as_dict()))


@dataclass(frozen=True)
class EndpointPlan:
    target_id: str
    platform: str
    record_kind: str
    endpoint_key: str
    path: str
    params: Mapping[str, str | int | bool]
    parent_external_id: str | None = None
    thread_external_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "target_id": self.target_id,
            "platform": self.platform,
            "record_kind": self.record_kind,
            "endpoint_key": self.endpoint_key,
            "method": "GET",
            "path": self.path,
            "params": dict(self.params),
        }
        if self.parent_external_id is not None:
            result["parent_external_id"] = self.parent_external_id
        if self.thread_external_id is not None:
            result["thread_external_id"] = self.thread_external_id
        return result


@dataclass(frozen=True)
class ProviderResponse:
    payload: Mapping[str, Any]
    raw_response_sha256: str
    status_code: int
    attempts: int
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))

    @property
    def fetch_timestamp(self) -> str:
        return self.fetched_at


class ScrapeCreatorsClient:
    """Small synchronous client with an environment-only credential.

    ``transport`` and ``sleep`` exist for deterministic, network-free tests. No
    constructor argument can supply an API key; the only source is
    ``SCRAPECREATORS_API_KEY``.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be in (0, {MAX_TIMEOUT_SECONDS}]")
        if not 0 < connect_timeout_seconds <= timeout_seconds:
            raise ValueError("connect_timeout_seconds must be positive and no greater than timeout_seconds")
        if not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ValueError(f"max_attempts must be between 1 and {MAX_ATTEMPTS}")
        if not 0 <= retry_base_seconds <= MAX_RETRY_DELAY_SECONDS:
            raise ValueError(f"retry_base_seconds must be between 0 and {MAX_RETRY_DELAY_SECONDS}")

        api_key = os.environ.get(API_KEY_ENV, "")
        if not api_key.strip():
            raise MissingAPIKeyError(f"{API_KEY_ENV} is not set")

        self._secret = api_key
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=API_BASE_URL,
            headers={"accept": "application/json", "x-api-key": api_key},
            timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    def __enter__(self) -> ScrapeCreatorsClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={API_BASE_URL!r}, "
            f"max_attempts={self._max_attempts}, credential='[REDACTED]')"
        )

    def close(self) -> None:
        self._client.close()

    def get(self, plan: EndpointPlan) -> ProviderResponse:
        response: httpx.Response | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.get(plan.path, params=plan.params)
            except httpx.TransportError as exc:
                if attempt == self._max_attempts:
                    raise ProviderRequestError(
                        f"GET {plan.path} failed after {attempt} attempt(s): transport error"
                    ) from exc
                self._sleep(self._backoff_seconds(attempt, None))
                continue

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < self._max_attempts:
                self._sleep(self._backoff_seconds(attempt, response))
                continue
            if not 200 <= response.status_code < 300:
                raise ProviderRequestError(
                    f"GET {plan.path} failed after {attempt} attempt(s): HTTP {response.status_code}"
                )

            raw_hash = _sha256_bytes(response.content)
            try:
                decoded = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise ProviderRequestError(f"GET {plan.path} returned non-JSON content") from exc
            if not isinstance(decoded, Mapping):
                raise ProviderRequestError(f"GET {plan.path} returned a non-object JSON response")
            _validate_provider_payload(plan, decoded)

            # Downstream code only receives a recursively redacted copy. The exact
            # response is represented only by its SHA-256 hash.
            safe_payload = _redact_payload(decoded, self._secret)
            return ProviderResponse(
                payload=safe_payload,
                raw_response_sha256=raw_hash,
                status_code=response.status_code,
                attempts=attempt,
                fetched_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            )

        raise AssertionError("bounded request loop exhausted unexpectedly")

    def _backoff_seconds(self, attempt: int, response: httpx.Response | None) -> float:
        delay = min(self._retry_base_seconds * (2 ** (attempt - 1)), MAX_RETRY_DELAY_SECONDS)
        if response is None:
            return delay
        retry_after = response.headers.get("retry-after", "")
        try:
            server_delay = float(retry_after)
        except (TypeError, ValueError):
            return delay
        return min(max(server_delay, 0.0), MAX_RETRY_DELAY_SECONDS)


def _validate_provider_payload(plan: EndpointPlan, payload: Mapping[str, Any]) -> None:
    """Reject provider envelopes that cannot prove a successful usable response."""

    if payload.get("success") is not True:
        raise ProviderRequestError(f"GET {plan.path} did not return success=true")
    if plan.record_kind in {"response", "reply", "interaction", "interactions", "comments"}:
        # The provider has used both top-level ``comments``/``replies`` and
        # nested ``data`` envelopes.  A success response may legitimately
        # contain no interactions, but when a collection field is present it
        # must be a list; accepting an object here would silently turn schema
        # drift into an empty-success result.
        for key in ("comments", "replies", "items"):
            if key in payload and (
                not isinstance(payload[key], list)
                or any(not isinstance(item, Mapping) for item in payload[key])
            ):
                raise ProviderRequestError(f"GET {plan.path} returned an invalid {key} shape")
        data = _mapping(payload.get("data"))
        for key in ("comments", "replies", "items"):
            if key in data and (
                not isinstance(data[key], list)
                or any(not isinstance(item, Mapping) for item in data[key])
            ):
                raise ProviderRequestError(f"GET {plan.path} returned an invalid data.{key} shape")
        return
    if plan.record_kind == "posts":
        rows = payload.get("tweets" if plan.platform == "twitter" else "posts")
        if plan.platform == "instagram":
            rows = payload.get("items")
        if not isinstance(rows, list) or any(not isinstance(item, Mapping) for item in rows):
            raise ProviderRequestError(f"GET {plan.path} returned an invalid posts shape")
        return

    if plan.platform == "linkedin":
        has_identity = _string(payload.get("id")) is not None or _string(payload.get("name")) is not None
    elif plan.platform == "twitter":
        legacy = _mapping(payload.get("legacy"))
        has_identity = _string(payload.get("rest_id")) is not None and _string(legacy.get("screen_name")) is not None
    else:
        user = _mapping(_mapping(payload.get("data")).get("user"))
        has_identity = (_string(user.get("id")) is not None or _string(user.get("pk")) is not None) and _string(
            user.get("username")
        ) is not None
    if not has_identity:
        raise ProviderRequestError(f"GET {plan.path} returned an invalid profile shape")


def load_target_manifest(path: str | Path) -> TargetManifest:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError("target manifest does not exist") from exc
    except (OSError, UnicodeError) as exc:
        raise ManifestError("target manifest could not be read") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"target manifest is not valid JSON (line {exc.lineno})") from exc
    return parse_target_manifest(raw)


def parse_target_manifest(raw: Any) -> TargetManifest:
    if not isinstance(raw, Mapping):
        raise ManifestError("target manifest must be a JSON object")
    if set(raw) != {"schema_version", "targets"}:
        raise ManifestError("target manifest must contain only schema_version and targets")
    if raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(f"target manifest schema_version must be {MANIFEST_SCHEMA_VERSION}")
    rows = raw.get("targets")
    if not isinstance(rows, list) or not rows:
        raise ManifestError("targets must be a non-empty JSON list")

    targets: list[SocialTarget] = []
    target_ids: set[str] = set()
    explicit_targets: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        target = _parse_target(row, index=index)
        if target.target_id in target_ids:
            raise ManifestError(f"duplicate target_id at targets[{index}]")
        identity = (target.platform, target.company_url or target.handle or "")
        if identity in explicit_targets:
            raise ManifestError(f"duplicate explicit target at targets[{index}]")
        target_ids.add(target.target_id)
        explicit_targets.add(identity)
        targets.append(target)
    return TargetManifest(schema_version=MANIFEST_SCHEMA_VERSION, targets=tuple(targets))


def plan_requests(
    manifest: TargetManifest,
    *,
    max_post_pages: int = 1,
    include_interactions: bool = False,
    posts: Sequence[Mapping[str, Any]] = (),
    include_replies: bool = False,
) -> tuple[EndpointPlan, ...]:
    if not 1 <= max_post_pages <= 10:
        raise ValueError("max_post_pages must be between 1 and 10")
    plans: list[EndpointPlan] = []
    for target in manifest.targets:
        if target.platform == "linkedin":
            assert target.company_url is not None
            plans.append(
                EndpointPlan(
                    target.target_id,
                    target.platform,
                    "profile",
                    "linkedin_company",
                    "/v1/linkedin/company",
                    {"url": target.company_url},
                )
            )
            plans.extend(
                EndpointPlan(
                    target.target_id,
                    target.platform,
                    "posts",
                    "linkedin_company_posts",
                    "/v1/linkedin/company/posts",
                    {"url": target.company_url, "page": page},
                )
                for page in range(1, max_post_pages + 1)
            )
        elif target.platform == "twitter":
            assert target.handle is not None
            plans.extend(
                (
                    EndpointPlan(
                        target.target_id,
                        target.platform,
                        "profile",
                        "twitter_profile",
                        "/v1/twitter/profile",
                        {"handle": target.handle},
                    ),
                    EndpointPlan(
                        target.target_id,
                        target.platform,
                        "posts",
                        "twitter_user_tweets",
                        "/v1/twitter/user-tweets",
                        {"handle": target.handle},
                    ),
                )
            )
        elif target.platform == "instagram":
            assert target.handle is not None
            plans.extend(
                (
                    EndpointPlan(
                        target.target_id,
                        target.platform,
                        "profile",
                        "instagram_profile",
                        "/v1/instagram/profile",
                        {"handle": target.handle},
                    ),
                    EndpointPlan(
                        target.target_id,
                        target.platform,
                        "posts",
                        "instagram_user_posts_v2",
                        "/v2/instagram/user/posts",
                        {"handle": target.handle},
                    ),
                )
            )
        else:  # pragma: no cover - manifest parsing prevents this
            raise AssertionError(f"unsupported platform: {target.platform}")
    if include_interactions:
        plans.extend(plan_interaction_requests(manifest, posts, include_replies=include_replies))
    return tuple(plans)


def plan_interaction_requests(
    manifest: TargetManifest,
    posts: Sequence[Mapping[str, Any]],
    *,
    include_replies: bool = False,
    cursor_by_target: Mapping[str, str] | None = None,
) -> tuple[EndpointPlan, ...]:
    """Plan only the interaction endpoints supported by the provider.

    Interaction acquisition is intentionally separate from the six original
    profile/post requests: parent URLs and provider post IDs are needed before
    a comment request can be made.  X/Twitter is omitted here because no
    verified reply-tree endpoint exists; callers receive an explicit
    ``not_acquired`` capability status instead of an empty success.
    """

    target_by_id = {target.target_id: target for target in manifest.targets}
    plans: list[EndpointPlan] = []
    for post in posts:
        target_id = _string(post.get("target_id"))
        target = target_by_id.get(target_id or "")
        if target is None or target.platform not in {"linkedin", "instagram"}:
            continue
        canonical_url = _string(post.get("canonical_url"))
        external_id = _string(post.get("external_id"))
        if canonical_url is None or external_id is None:
            # An item without a stable parent cannot safely receive a thread.
            continue
        if target.platform == "linkedin":
            plans.append(
                EndpointPlan(
                    target_id=target.target_id,
                    platform=target.platform,
                    record_kind="response",
                    endpoint_key="linkedin_post_comments",
                    path="/v1/linkedin/post",
                    params={"url": canonical_url},
                    parent_external_id=external_id,
                    thread_external_id=external_id,
                )
            )
        else:
            params: dict[str, str | int | bool] = {"url": canonical_url}
            cursor = (cursor_by_target or {}).get(external_id)
            if cursor:
                params["cursor"] = cursor
            if include_replies:
                params["include_replies"] = True
            plans.append(
                EndpointPlan(
                    target_id=target.target_id,
                    platform=target.platform,
                    record_kind="response",
                    endpoint_key="instagram_post_comments",
                    path="/v2/instagram/post/comments",
                    params=params,
                    parent_external_id=external_id,
                    thread_external_id=external_id,
                )
            )
    return tuple(plans)


def plan_instagram_reply_request(
    *,
    target_id: str,
    post_url: str,
    post_external_id: str,
    comment_id: str,
    cursor: str | None = None,
) -> EndpointPlan:
    """Build one bounded Instagram nested-reply request."""

    params: dict[str, str | int | bool] = {"url": post_url, "comment_id": comment_id}
    if cursor:
        params["cursor"] = cursor
    return EndpointPlan(
        target_id=target_id,
        platform="instagram",
        record_kind="reply",
        endpoint_key="instagram_comment_replies",
        path="/v1/instagram/post/comment/replies",
        params=params,
        parent_external_id=comment_id,
        thread_external_id=post_external_id,
    )


def build_dry_run(manifest: TargetManifest, *, max_post_pages: int = 1) -> dict[str, Any]:
    plans = plan_requests(manifest, max_post_pages=max_post_pages)
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "mode": "dry_run",
        "provider": "scrapecreators",
        "api_base_url": API_BASE_URL,
        "manifest_sha256": manifest.sha256,
        "targets": [target.as_dict() for target in manifest.targets],
        "requests": [plan.as_dict() for plan in plans],
        "summary": {
            "target_count": len(manifest.targets),
            "request_count": len(plans),
            "profile_count": 0,
            "post_count": 0,
        },
    }


_CAPABILITY_STATUSES = frozenset({"acquired", "partial", "not_acquired", "failed"})
_CAPABILITY_ROLES = tuple(role.value for role in ActorRole)

# Conservative ceilings are deliberately local to the verified provider
# endpoint/mode contract.  They are reservations, not estimates derived from
# a response: a request is never launched if its entire declared ceiling does
# not fit the caller's remaining interaction budget.
_INTERACTION_CREDIT_CEILINGS = {
    "linkedin_post_comments": 1,
    "instagram_post_comments": 1,
    "instagram_comment_replies": 1,
}
_INSTAGRAM_COMMENTS_WITH_REPLIES_CREDIT_CEILING = 15


def _target_fingerprint(target: SocialTarget) -> str:
    return _sha256_bytes(_canonical_json_bytes(target.as_dict()))


def _request_fingerprint(plan: EndpointPlan) -> str:
    return _sha256_bytes(_canonical_json_bytes({"method": "GET", "path": plan.path, "params": dict(plan.params)}))


def _profile_identity_from_payload(target: SocialTarget, payload: Mapping[str, Any]) -> dict[str, str] | None:
    """Extract the exact provider account identity used to bind official posts."""

    if target.platform == "linkedin":
        account_id = _string(payload.get("id"))
        handle = _linkedin_slug(target.company_url)
    elif target.platform == "twitter":
        legacy = _mapping(payload.get("legacy"))
        account_id = _string(payload.get("rest_id")) or _string(payload.get("id"))
        handle = _string(legacy.get("screen_name")) or target.handle
    else:
        user = _mapping(_mapping(payload.get("data")).get("user"))
        account_id = _string(user.get("id")) or _string(user.get("pk"))
        handle = _string(user.get("username")) or target.handle
    if account_id is None or handle is None:
        return None
    return {"account_id": account_id, "handle": handle.lstrip("@").casefold()}


def _identity_account_id(identity: Mapping[str, Any] | None) -> str | None:
    return _string(identity.get("account_id")) if identity is not None else None


def _identity_handle(identity: Mapping[str, Any] | None) -> str | None:
    return _string(identity.get("handle")) if identity is not None else None


def _metric_context_for_row(row: Mapping[str, Any], fetched_at: str) -> MetricContext | None:
    metrics = row.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    safe_metrics: dict[str, int | float] = {}
    for key, value in metrics.items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value < 0:
            continue
        safe_metrics[key] = value
    if not safe_metrics:
        return None
    try:
        return MetricContext(metrics=safe_metrics, observed_at=fetched_at)
    except SocialLabContractError:
        return None


def _build_contract_observation(
    *,
    row: Mapping[str, Any],
    target: SocialTarget,
    plan: EndpointPlan,
    response: ProviderResponse,
    manifest: TargetManifest,
    ordinal: int,
    bound_identity: Mapping[str, Any] | None,
    record_kind: str,
    parent_external_id: str | None = None,
    thread_external_id: str | None = None,
    linkage_evidence: Mapping[str, Any] | None = None,
) -> SocialObservation | None:
    """Map one selected provider row to the frozen provider-neutral contract."""

    external_id = _string(row.get("external_id"))
    canonical_url = _string(row.get("canonical_url"))
    if external_id is None or canonical_url is None:
        return None
    author_account_id = _string(row.get("author_account_id")) or _string(row.get("_author_account_id"))
    author_handle = _string(row.get("author_handle")) or _string(row.get("_author_handle"))
    if record_kind == "observation" and bound_identity is not None:
        author_account_id = author_account_id or _identity_account_id(bound_identity)
        author_handle = author_handle or _identity_handle(bound_identity)
    if record_kind == "post" and bound_identity is not None:
        # Posts from the exact company/profile endpoint are bound to the
        # successfully acquired target account when the provider omits the
        # repeated author object on each row.  Explicit provider identities are
        # retained and can therefore fail closed when they conflict.
        author_account_id = author_account_id or _identity_account_id(bound_identity)
        author_handle = author_handle or _identity_handle(bound_identity)
    parent = parent_external_id if parent_external_id is not None else _string(row.get("parent_external_id"))
    thread = thread_external_id if thread_external_id is not None else _string(row.get("thread_external_id"))
    evidence: dict[str, Any] = {
        "target_id": target.target_id,
        "endpoint_target_binding": target.as_dict(),
    }
    if linkage_evidence:
        evidence.update(dict(linkage_evidence))
    provenance = Provenance(
        provider="scrapecreators",
        endpoint_key=plan.endpoint_key,
        target_fingerprint=_target_fingerprint(target),
        manifest_fingerprint=manifest.sha256,
        request_fingerprint=_request_fingerprint(plan),
        response_sha256=response.raw_response_sha256,
        fetched_at=response.fetched_at,
        page=_integer(plan.params.get("page")),
        cursor=_string(plan.params.get("cursor")),
        result_ordinal=ordinal,
        target_identity=dict(bound_identity) if bound_identity is not None else target.as_dict(),
        linkage_evidence=evidence,
    )
    try:
        return build_social_observation(
            platform=target.platform,
            record_kind=record_kind,
            external_id=external_id,
            canonical_url=canonical_url,
            author_account_id=author_account_id,
            author_handle=author_handle,
            parent_external_id=parent,
            thread_external_id=thread,
            text=_string(row.get("text")) or _string(row.get("description")) or "",
            media_kind=_string(row.get("media_kind")),
            published_at=row.get("published_at"),
            provenance=provenance,
            bound_brand_identity=dict(bound_identity) if bound_identity is not None else None,
            metric_context=_metric_context_for_row(row, response.fetched_at),
        )
    except SocialLabContractError:
        # A provider row that cannot satisfy the frozen contract is retained in
        # the endpoint outcome but never admitted as a semantic observation.
        return None


def _attach_contract_observation(row: dict[str, Any], observation: SocialObservation | None) -> None:
    if observation is None:
        row.pop("observation", None)
        row.pop("contract_observation", None)
        return
    row["content_id"] = observation.content_id
    row["semantic_fingerprint"] = observation.semantic_fingerprint
    row["observation"] = observation.to_dict()
    row["contract_observation"] = observation.to_dict()


def _provenance_ref(plan: EndpointPlan, response: ProviderResponse | None, *, reason: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "provider": "scrapecreators",
        "endpoint_key": plan.endpoint_key,
        "path": plan.path,
        "params": dict(plan.params),
    }
    if response is not None:
        result.update(
            {
                "response_sha256": response.raw_response_sha256,
                "fetched_at": response.fetched_at,
                "page": _integer(plan.params.get("page")),
                "cursor": _string(plan.params.get("cursor")),
            }
        )
    if reason:
        result["reason"] = reason
    return result


def _interaction_credit_ceiling(plan: EndpointPlan) -> int | None:
    """Return the verified conservative credit reservation for one request."""

    if plan.endpoint_key == "instagram_post_comments" and plan.params.get("include_replies") is True:
        return _INSTAGRAM_COMMENTS_WITH_REPLIES_CREDIT_CEILING
    return _INTERACTION_CREDIT_CEILINGS.get(plan.endpoint_key)


def _new_capability_matrix(manifest: TargetManifest) -> dict[str, dict[str, dict[str, Any]]]:
    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    for target in manifest.targets:
        matrix[target.target_id] = {
            role: {
                "target_id": target.target_id,
                "platform": target.platform,
                "role": role,
                "status": "not_acquired",
                "reason": "acquisition_not_attempted",
                "provenance": [],
            }
            for role in _CAPABILITY_ROLES
        }
        if target.platform == "twitter":
            for role in (ActorRole.COMMUNITY_RESPONSE.value, ActorRole.BRAND_REPLY.value):
                matrix[target.target_id][role].update(
                    {
                        "status": "not_acquired",
                        "reason": "no_verified_reply_tree_endpoint",
                    }
                )
    return matrix


def _set_capability(
    matrix: dict[str, dict[str, dict[str, Any]]],
    target_id: str,
    role: str,
    *,
    status: str,
    reason: str,
    provenance: Mapping[str, Any] | None = None,
) -> None:
    if status not in _CAPABILITY_STATUSES:
        raise ValueError(f"unsupported capability status: {status}")
    entry = matrix.setdefault(target_id, {}).setdefault(
        role,
        {
            "target_id": target_id,
            "platform": "unknown",
            "role": role,
            "status": status,
            "reason": reason,
            "provenance": [],
        },
    )
    entry["status"] = status
    entry["reason"] = reason
    if provenance is not None:
        entry.setdefault("provenance", []).append(dict(provenance))


def _capability_matrix_by_platform(
    manifest: TargetManifest,
    matrix_by_target: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Provide a platform-level view while retaining target-level evidence."""

    rank = {"not_acquired": 0, "acquired": 1, "partial": 2, "failed": 3}
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for target in manifest.targets:
        platform_entries = result.setdefault(target.platform, {})
        target_entries = matrix_by_target.get(target.target_id, {})
        for role in _CAPABILITY_ROLES:
            entry = target_entries.get(role)
            if entry is None:
                continue
            current = platform_entries.get(role)
            if current is None or rank.get(str(entry.get("status")), 0) > rank.get(str(current.get("status")), 0):
                platform_entries[role] = {
                    **dict(entry),
                    "target_ids": [target.target_id],
                    "provenance": list(entry.get("provenance", [])),
                }
            else:
                current.setdefault("target_ids", []).append(target.target_id)
                current.setdefault("provenance", []).extend(entry.get("provenance", []))
    return result


def _interaction_items(payload: Mapping[str, Any], *, replies: bool = False) -> list[Mapping[str, Any]]:
    keys = ("replies", "comments", "items") if replies else ("comments", "items", "replies")
    candidates: list[Any] = [payload]
    data = _mapping(payload.get("data"))
    if data:
        candidates.append(data)
        candidates.append(_mapping(data.get("post")))
    candidates.append(_mapping(payload.get("post")))
    for candidate in candidates:
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
    return []


def _next_cursor(payload: Mapping[str, Any]) -> str | None:
    for candidate in (
        payload.get("cursor"),
        payload.get("next_cursor"),
        payload.get("nextCursor"),
        _mapping(payload.get("pagination")).get("next_cursor"),
        _mapping(payload.get("pagination")).get("nextCursor"),
        _mapping(payload.get("data")).get("cursor"),
        _mapping(payload.get("data")).get("next_cursor"),
    ):
        value = _string(candidate)
        if value:
            return value
    return None


def _interaction_author(item: Mapping[str, Any], platform: str) -> tuple[str | None, str | None, str | None]:
    author = {}
    for key in ("author", "user", "owner", "from", "profile"):
        candidate = _mapping(item.get(key))
        if candidate:
            author = candidate
            break
    account_id = next(
        (
            _string(item.get(key))
            for key in ("author_account_id", "author_id", "account_id", "user_id", "userId")
            if _string(item.get(key)) is not None
        ),
        None,
    )
    if account_id is None:
        account_id = next(
            (
                _string(author.get(key))
                for key in ("account_id", "author_account_id", "id", "pk", "rest_id", "user_id")
                if _string(author.get(key)) is not None
            ),
            None,
        )
    handle = next(
        (
            _string(item.get(key))
            for key in ("author_handle", "handle", "username", "screen_name")
            if _string(item.get(key)) is not None
        ),
        None,
    )
    if handle is None:
        handle = next(
            (
                _string(author.get(key))
                for key in ("handle", "username", "screen_name")
                if _string(author.get(key)) is not None
            ),
            None,
        )
    stable_url = _string(item.get("linkedinUrl")) or _string(author.get("linkedinUrl"))
    # LinkedIn's documented comment payload may expose only linkedinUrl.  It
    # is a stable provider identity for conservative community classification,
    # but cannot accidentally equal the profile numeric id.
    if account_id is None and platform == "linkedin" and stable_url is not None:
        account_id = stable_url
    return account_id, handle, stable_url


def _normalize_interaction_row(
    item: Mapping[str, Any],
    *,
    target: SocialTarget,
    post: Mapping[str, Any],
    record_kind: str,
    parent_external_id: str,
    thread_external_id: str,
) -> dict[str, Any] | None:
    external_id = next(
        (
            _string(item.get(key))
            for key in ("id", "comment_id", "commentId", "pk", "rest_id", "reply_id")
            if _string(item.get(key)) is not None
        ),
        None,
    )
    post_url = _string(post.get("canonical_url"))
    if external_id is None or post_url is None:
        return None
    canonical_url = next(
        (
            _string(item.get(key))
            for key in ("canonical_url", "url", "permalink", "link")
            if _string(item.get(key)) is not None
        ),
        f"{post_url}#comment-{external_id}",
    )
    account_id, handle, stable_url = _interaction_author(item, target.platform)
    text = next(
        (
            _string(item.get(key))
            for key in ("text", "full_text", "body", "message", "content")
            if _string(item.get(key)) is not None
        ),
        "",
    )
    published_at = next(
        (
            item.get(key)
            for key in ("published_at", "created_at", "created_time", "timestamp", "date")
            if item.get(key) is not None
        ),
        None,
    )
    row: dict[str, Any] = {
        "schema_version": 1,
        "record_type": record_kind,
        "platform": target.platform,
        "external_id": external_id,
        "author_account_id": account_id,
        "author_handle": handle,
        "canonical_url": canonical_url,
        "parent_external_id": parent_external_id,
        "thread_external_id": thread_external_id,
        "published_at": _normalize_datetime(published_at),
        "text": text,
        "media_kind": "comment",
        "metrics": _compact_mapping(
            {
                "likes": _first_integer(item.get("like_count"), item.get("likes")),
                "replies": _first_integer(item.get("reply_count"), item.get("replies_count")),
            }
        ),
    }
    if stable_url is not None:
        row["author_profile_url"] = stable_url
    return row


def run_acquisition(
    manifest: TargetManifest,
    client: ScrapeCreatorsClient,
    *,
    include_raw: bool = False,
    include_interactions: bool = False,
    include_replies: bool = False,
    max_post_pages: int = 1,
    max_interaction_pages: int = 1,
    max_interaction_credits: int | None = None,
) -> dict[str, Any]:
    if include_replies and not include_interactions:
        raise ValueError("include_replies requires explicit include_interactions=True")
    if not 1 <= max_interaction_pages <= 10:
        raise ValueError("max_interaction_pages must be between 1 and 10")
    if not 1 <= max_post_pages <= 10:
        raise ValueError("max_post_pages must be between 1 and 10")
    if max_interaction_credits is not None and max_interaction_credits < 0:
        raise ValueError("max_interaction_credits must be non-negative")
    if include_replies and max_interaction_credits is None:
        raise ValueError("include_replies requires an explicit max_interaction_credits budget")

    target_by_id = {target.target_id: target for target in manifest.targets}
    profiles: list[dict[str, Any]] = []
    posts: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    identities: dict[str, dict[str, str] | None] = {}
    capability_matrix = _new_capability_matrix(manifest)
    credits_charged = 0
    interaction_credits_charged = 0
    interaction_credits_reserved = 0
    credit_diagnostics: list[dict[str, Any]] = []
    ordinal_by_target: dict[str, int] = {}

    def append_response(
        plan: EndpointPlan,
        response: ProviderResponse,
        normalized: Sequence[dict[str, Any]],
        *,
        outcome: str = "succeeded",
        is_interaction: bool = False,
    ) -> dict[str, Any]:
        nonlocal credits_charged, interaction_credits_charged
        charged = _integer(response.payload.get("credits_charged"))
        if charged is not None:
            credits_charged += max(charged, 0)
            if is_interaction:
                interaction_credits_charged += max(charged, 0)
        response_row: dict[str, Any] = {
            **plan.as_dict(),
            "outcome": outcome,
            "status_code": response.status_code,
            "attempts": response.attempts,
            "fetched_at": response.fetched_at,
            "raw_response_sha256": response.raw_response_sha256,
            "credits_charged": charged,
            "credits_remaining": _integer(response.payload.get("credits_remaining")),
            "normalized_content_ids": [item["content_id"] for item in normalized if item.get("content_id")],
        }
        if include_raw:
            response_row["raw_payload_redacted"] = response.payload
        if is_interaction and charged is not None:
            ceiling = _interaction_credit_ceiling(plan)
            if ceiling is not None and charged > ceiling:
                diagnostic = {
                    "kind": "provider_credit_overcharge",
                    "endpoint_key": plan.endpoint_key,
                    "path": plan.path,
                    "declared_ceiling": ceiling,
                    "actual_credits_charged": charged,
                    "response_sha256": response.raw_response_sha256,
                }
                response_row["credit_diagnostic"] = diagnostic
                credit_diagnostics.append(diagnostic)
        responses.append(response_row)
        return response_row

    def reserve_interaction_budget(plan: EndpointPlan, target_id: str) -> bool:
        """Reserve a full request ceiling before calling the provider."""

        nonlocal interaction_credits_reserved
        ceiling = _interaction_credit_ceiling(plan)
        remaining = (
            None
            if max_interaction_credits is None
            else max_interaction_credits - interaction_credits_reserved
        )
        reason: str | None = None
        if ceiling is None:
            # An interaction endpoint outside the verified table is never
            # launched, even when the caller did not configure a budget.
            reason = "interaction_credit_budget_insufficient"
        elif remaining is not None and remaining <= 0:
            reason = "interaction_credit_budget_exhausted"
        elif remaining is not None and ceiling > remaining:
            reason = "interaction_credit_budget_insufficient"
        if reason is None:
            # Consume the reservation before the provider call.  The provider's
            # optional ``credits_charged`` field is diagnostic only and cannot
            # release capacity for a later page/reply request.
            assert ceiling is not None
            interaction_credits_reserved += ceiling
            return True

        provenance = _provenance_ref(plan, None, reason=reason)
        provenance.update(
            {
                "credit_ceiling": ceiling,
                "remaining_credit_budget": remaining,
                "interaction_credits_charged": interaction_credits_charged,
                "interaction_credits_reserved": interaction_credits_reserved,
                "max_interaction_credits": max_interaction_credits,
            }
        )
        for role in (ActorRole.COMMUNITY_RESPONSE.value, ActorRole.BRAND_REPLY.value):
            prior_status = capability_matrix[target_id][role]["status"]
            _set_capability(
                capability_matrix,
                target_id,
                role,
                status="partial" if prior_status in {"acquired", "partial"} else "not_acquired",
                reason=reason,
                provenance=provenance,
            )
        return False

    # Preserve the original six endpoint acquisition contract.
    for plan in plan_requests(manifest, max_post_pages=max_post_pages):
        target = target_by_id[plan.target_id]
        try:
            response = client.get(plan)
        except ProviderRequestError as exc:
            responses.append(
                {
                    **plan.as_dict(),
                    "outcome": "failed",
                    "failure": str(exc),
                    "normalized_content_ids": [],
                }
            )
            if plan.record_kind == "profile":
                identities[target.target_id] = None
            elif plan.record_kind == "posts":
                prior_status = capability_matrix[target.target_id][ActorRole.OFFICIAL_BRAND_POST.value]["status"]
                _set_capability(
                    capability_matrix,
                    target.target_id,
                    ActorRole.OFFICIAL_BRAND_POST.value,
                    status="partial" if prior_status in {"acquired", "partial"} else "failed",
                    reason=(
                        "official_posts_page_failed_after_partial_success"
                        if prior_status in {"acquired", "partial"}
                        else "official_posts_endpoint_failed"
                    ),
                    provenance=_provenance_ref(plan, None, reason=str(exc)),
                )
            continue

        normalized: list[dict[str, Any]]
        if plan.record_kind == "profile":
            identities[target.target_id] = _profile_identity_from_payload(target, response.payload)
            normalized = [_normalize_profile(target, plan.endpoint_key, response)]
            for index, row in enumerate(normalized):
                row["target_id"] = target.target_id
                observation = _build_contract_observation(
                    row=row,
                    target=target,
                    plan=plan,
                    response=response,
                    manifest=manifest,
                    ordinal=index,
                    bound_identity=identities[target.target_id],
                    record_kind="observation",
                    linkage_evidence={"observation_scope": "target_profile"},
                )
                _attach_contract_observation(row, observation)
                if observation is not None:
                    observations.append(observation.to_dict())
            profiles.extend(normalized)
        else:
            normalized = _normalize_posts(target, plan.endpoint_key, response)
            for index, row in enumerate(normalized):
                row["target_id"] = target.target_id
                ordinal = ordinal_by_target.get(target.target_id, 0)
                ordinal_by_target[target.target_id] = ordinal + 1
                observation = _build_contract_observation(
                    row=row,
                    target=target,
                    plan=plan,
                    response=response,
                    manifest=manifest,
                    ordinal=ordinal,
                    bound_identity=identities.get(target.target_id),
                    record_kind="post",
                    linkage_evidence={"observation_scope": "official_brand_post"},
                )
                _attach_contract_observation(row, observation)
                if observation is not None:
                    observations.append(observation.to_dict())
                    role = observation.actor_role
                    _set_capability(
                        capability_matrix,
                        target.target_id,
                        role,
                        status="acquired",
                        reason="admitted_observation",
                        provenance=_provenance_ref(plan, response),
                    )
            posts.extend(normalized)
        append_response(plan, response, normalized)
        if plan.record_kind == "posts" and not normalized:
            _set_capability(
                capability_matrix,
                target.target_id,
                ActorRole.OFFICIAL_BRAND_POST.value,
                status="partial",
                reason="no_official_posts_returned",
                provenance=_provenance_ref(plan, response),
            )

    if include_interactions:
        # Build interaction requests from admitted/normalized parent posts. X
        # intentionally has no requests and remains explicit not_acquired.
        interaction_plans = plan_interaction_requests(manifest, posts, include_replies=include_replies)
        for plan in interaction_plans:
            target = target_by_id[plan.target_id]
            cursor: str | None = _string(plan.params.get("cursor"))
            page = 0
            while page < max_interaction_pages:
                params = dict(plan.params)
                if cursor:
                    params["cursor"] = cursor
                active_plan = EndpointPlan(
                    target_id=plan.target_id,
                    platform=plan.platform,
                    record_kind=plan.record_kind,
                    endpoint_key=plan.endpoint_key,
                    path=plan.path,
                    params=params,
                    parent_external_id=plan.parent_external_id,
                    thread_external_id=plan.thread_external_id,
                )
                if not reserve_interaction_budget(active_plan, target.target_id):
                    break
                try:
                    response = client.get(active_plan)
                except ProviderRequestError as exc:
                    responses.append(
                        {
                            **active_plan.as_dict(),
                            "outcome": "failed",
                            "failure": str(exc),
                            "normalized_content_ids": [],
                        }
                    )
                    for role in (ActorRole.COMMUNITY_RESPONSE.value, ActorRole.BRAND_REPLY.value):
                        prior_status = capability_matrix[target.target_id][role]["status"]
                        _set_capability(
                            capability_matrix,
                            target.target_id,
                            role,
                            status="partial" if prior_status in {"acquired", "partial"} else "failed",
                            reason=(
                                "interaction_endpoint_failed_after_partial_success"
                                if prior_status in {"acquired", "partial"}
                                else "interaction_endpoint_failed"
                            ),
                            provenance=_provenance_ref(active_plan, None, reason=str(exc)),
                        )
                    break
                page += 1
                parent_post = next(
                    (row for row in posts if row.get("target_id") == target.target_id and row.get("external_id") == plan.parent_external_id),
                    {"external_id": plan.parent_external_id, "canonical_url": active_plan.params.get("url")},
                )
                raw_items = _interaction_items(response.payload)
                normalized_interactions: list[dict[str, Any]] = []
                for item in raw_items:
                    row = _normalize_interaction_row(
                        item,
                        target=target,
                        post=parent_post,
                        record_kind="response",
                        parent_external_id=plan.parent_external_id or "",
                        thread_external_id=plan.thread_external_id or plan.parent_external_id or "",
                    )
                    if row is None:
                        continue
                    normalized_interactions.append(row)
                for index, row in enumerate(normalized_interactions):
                    ordinal = ordinal_by_target.get(target.target_id, 0)
                    ordinal_by_target[target.target_id] = ordinal + 1
                    observation = _build_contract_observation(
                        row=row,
                        target=target,
                        plan=active_plan,
                        response=response,
                        manifest=manifest,
                        ordinal=ordinal,
                        bound_identity=identities.get(target.target_id),
                        record_kind="response",
                        parent_external_id=plan.parent_external_id,
                        thread_external_id=plan.thread_external_id,
                        linkage_evidence={
                            "observation_scope": "interaction",
                            "parent_url": active_plan.params.get("url"),
                            "identity_source": "provider_author_fields",
                        },
                    )
                    _attach_contract_observation(row, observation)
                    if observation is not None:
                        observations.append(observation.to_dict())
                        _set_capability(
                            capability_matrix,
                            target.target_id,
                            observation.actor_role,
                            status="acquired" if observation.actor_role != ActorRole.UNCLASSIFIED.value else "partial",
                            reason=(
                                "admitted_interaction"
                                if observation.actor_role != ActorRole.UNCLASSIFIED.value
                                else "interaction_identity_or_linkage_unclassified"
                            ),
                            provenance=_provenance_ref(active_plan, response),
                        )
                response_row = append_response(
                    active_plan,
                    response,
                    normalized_interactions,
                    is_interaction=True,
                )
                interactions.extend(normalized_interactions)

                # Nested Instagram replies returned in the comments envelope
                # are admitted without an extra request.  If the provider does
                # not embed them, the dedicated endpoint below is used only
                # under the explicit bounded reply opt-in.
                if include_replies and target.platform == "instagram":
                    for comment in raw_items:
                        nested = _interaction_items(comment, replies=True)
                        comment_id = _string(comment.get("id")) or _string(comment.get("comment_id"))
                        if nested and comment_id:
                            nested_rows: list[dict[str, Any]] = []
                            for reply in nested:
                                nested_row = _normalize_interaction_row(
                                    reply,
                                    target=target,
                                    post=parent_post,
                                    record_kind="reply",
                                    parent_external_id=comment_id,
                                    thread_external_id=plan.parent_external_id or "",
                                )
                                if nested_row is not None:
                                    nested_rows.append(nested_row)
                            for index, nested_row in enumerate(nested_rows):
                                ordinal = ordinal_by_target.get(target.target_id, 0)
                                ordinal_by_target[target.target_id] = ordinal + 1
                                nested_observation = _build_contract_observation(
                                    row=nested_row,
                                    target=target,
                                    plan=active_plan,
                                    response=response,
                                    manifest=manifest,
                                    ordinal=ordinal,
                                    bound_identity=identities.get(target.target_id),
                                    record_kind="reply",
                                    parent_external_id=comment_id,
                                    thread_external_id=plan.parent_external_id,
                                    linkage_evidence={
                                        "observation_scope": "nested_reply",
                                        "parent_url": active_plan.params.get("url"),
                                        "parent_comment_id": comment_id,
                                        "identity_source": "provider_author_fields",
                                    },
                                )
                                _attach_contract_observation(nested_row, nested_observation)
                                if nested_observation is not None:
                                    observations.append(nested_observation.to_dict())
                                    _set_capability(
                                        capability_matrix,
                                        target.target_id,
                                        nested_observation.actor_role,
                                        status=(
                                            "acquired"
                                            if nested_observation.actor_role != ActorRole.UNCLASSIFIED.value
                                            else "partial"
                                        ),
                                        reason="admitted_nested_reply",
                                        provenance=_provenance_ref(active_plan, response),
                                    )
                            interactions.extend(nested_rows)
                        if nested or not comment_id:
                            continue
                        reply_plan = plan_instagram_reply_request(
                            target_id=target.target_id,
                            post_url=str(active_plan.params["url"]),
                            post_external_id=plan.parent_external_id or "",
                            comment_id=comment_id,
                        )
                        if not reserve_interaction_budget(reply_plan, target.target_id):
                            continue
                        try:
                            reply_response = client.get(reply_plan)
                        except ProviderRequestError as exc:
                            responses.append(
                                {
                                    **reply_plan.as_dict(),
                                    "outcome": "failed",
                                    "failure": str(exc),
                                    "normalized_content_ids": [],
                                }
                            )
                            for role in (
                                ActorRole.COMMUNITY_RESPONSE.value,
                                ActorRole.BRAND_REPLY.value,
                            ):
                                _set_capability(
                                    capability_matrix,
                                    target.target_id,
                                    role,
                                    status="partial",
                                    reason="reply_endpoint_failed",
                                    provenance=_provenance_ref(reply_plan, None, reason=str(exc)),
                                )
                            continue
                        reply_rows: list[dict[str, Any]] = []
                        for reply in _interaction_items(reply_response.payload, replies=True):
                            reply_row = _normalize_interaction_row(
                                reply,
                                target=target,
                                post=parent_post,
                                record_kind="reply",
                                parent_external_id=comment_id,
                                thread_external_id=plan.parent_external_id or "",
                            )
                            if reply_row is not None:
                                reply_rows.append(reply_row)
                        for index, reply_row in enumerate(reply_rows):
                            ordinal = ordinal_by_target.get(target.target_id, 0)
                            ordinal_by_target[target.target_id] = ordinal + 1
                            reply_observation = _build_contract_observation(
                                row=reply_row,
                                target=target,
                                plan=reply_plan,
                                response=reply_response,
                                manifest=manifest,
                                ordinal=ordinal,
                                bound_identity=identities.get(target.target_id),
                                record_kind="reply",
                                parent_external_id=comment_id,
                                thread_external_id=plan.parent_external_id,
                                linkage_evidence={
                                    "observation_scope": "nested_reply",
                                    "parent_url": active_plan.params.get("url"),
                                    "parent_comment_id": comment_id,
                                    "identity_source": "provider_author_fields",
                                },
                            )
                            _attach_contract_observation(reply_row, reply_observation)
                            if reply_observation is not None:
                                observations.append(reply_observation.to_dict())
                                _set_capability(
                                    capability_matrix,
                                    target.target_id,
                                    reply_observation.actor_role,
                                    status="acquired" if reply_observation.actor_role != ActorRole.UNCLASSIFIED.value else "partial",
                                    reason="admitted_nested_reply",
                                    provenance=_provenance_ref(reply_plan, reply_response),
                                )
                        append_response(reply_plan, reply_response, reply_rows, is_interaction=True)
                        interactions.extend(reply_rows)

                cursor = _next_cursor(response.payload)
                if not cursor:
                    break

        # A successfully fetched interaction envelope with no admitted rows is
        # partial rather than an empty-success claim.  Unsupported X remains
        # not_acquired with its explicit reason from the initial matrix.
        attempted_interaction_targets = {plan.target_id for plan in interaction_plans}
        for target in manifest.targets:
            if target.platform == "twitter":
                continue
            for role in (ActorRole.COMMUNITY_RESPONSE.value, ActorRole.BRAND_REPLY.value):
                entry = capability_matrix[target.target_id][role]
                if entry["status"] == "not_acquired" and entry["reason"] == "acquisition_not_attempted":
                    if target.target_id in attempted_interaction_targets:
                        entry["status"] = "partial"
                        entry["reason"] = "no_interactions_returned"
                    else:
                        entry["reason"] = "parent_posts_not_acquired"
    else:
        for target in manifest.targets:
            for role in (ActorRole.COMMUNITY_RESPONSE.value, ActorRole.BRAND_REPLY.value):
                if target.platform != "twitter":
                    capability_matrix[target.target_id][role]["reason"] = "interaction_acquisition_not_enabled"

    succeeded = sum(response["outcome"] == "succeeded" for response in responses)
    failed = len(responses) - succeeded
    platform_capability_matrix = _capability_matrix_by_platform(manifest, capability_matrix)
    result: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "contract_version": CONTRACT_OUTPUT_SCHEMA_VERSION,
        "mode": "live",
        "provider": "scrapecreators",
        "api_base_url": API_BASE_URL,
        "manifest_sha256": manifest.sha256,
        "targets": [target.as_dict() for target in manifest.targets],
        "responses": responses,
        "profiles": profiles,
        "posts": posts,
        "interactions": interactions,
        "observations": observations,
        "provider_observations": observations,
        "capability_matrix": capability_matrix,
        "acquisition_matrix": capability_matrix,
        "interaction_capability_matrix": capability_matrix,
        "capabilities": capability_matrix,
        "platform_capability_matrix": platform_capability_matrix,
        "capability_matrix_by_platform": platform_capability_matrix,
        "capability_status": [
            entry
            for target_entries in capability_matrix.values()
            for entry in target_entries.values()
        ],
        "credit_diagnostics": credit_diagnostics,
        "summary": {
            "target_count": len(manifest.targets),
            "request_count": len(responses),
            "successful_request_count": succeeded,
            "failed_request_count": failed,
            "profile_count": len(profiles),
            "post_count": len(posts),
            "credits_charged": credits_charged,
            "raw_payloads_included": include_raw,
        },
    }
    if include_interactions:
        result["summary"].update(
            {
                "interaction_count": len(interactions),
                "observation_count": len(observations),
                "interaction_requests_enabled": True,
                "replies_enabled": include_replies,
                "interaction_credits_charged": interaction_credits_charged,
                "interaction_credits_reserved": interaction_credits_reserved,
                "credit_diagnostic_count": len(credit_diagnostics),
            }
        )
    return result


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Atomically replace ``path`` with a JSON file whose mode is exactly 0600."""

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    fd = -1
    temporary_name: str | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
        )
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output_path)
        temporary_name = None
        os.chmod(output_path, 0o600)
        return output_path
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _parse_target(raw: Any, *, index: int) -> SocialTarget:
    if not isinstance(raw, Mapping):
        raise ManifestError(f"targets[{index}] must be a JSON object")
    target_id = raw.get("target_id")
    platform = raw.get("platform")
    if not isinstance(target_id, str) or not _TARGET_ID_RE.fullmatch(target_id):
        raise ManifestError(f"targets[{index}].target_id has an invalid format")
    if platform not in {"linkedin", "twitter", "instagram"}:
        raise ManifestError(f"targets[{index}].platform must be linkedin, twitter, or instagram")

    if platform == "linkedin":
        if set(raw) != {"target_id", "platform", "company_url"}:
            raise ManifestError(
                f"targets[{index}] LinkedIn target must contain only target_id, platform, and company_url"
            )
        company_url = _validate_linkedin_company_url(raw.get("company_url"), index=index)
        return SocialTarget(target_id=target_id, platform=platform, company_url=company_url)

    if set(raw) != {"target_id", "platform", "handle"}:
        raise ManifestError(f"targets[{index}] {platform} target must contain only target_id, platform, and handle")
    handle = raw.get("handle")
    matcher = _TWITTER_HANDLE_RE if platform == "twitter" else _INSTAGRAM_HANDLE_RE
    if not isinstance(handle, str) or not matcher.fullmatch(handle):
        raise ManifestError(f"targets[{index}].handle has an invalid {platform} handle format")
    if platform == "instagram" and (handle.startswith(".") or handle.endswith(".") or ".." in handle):
        raise ManifestError(f"targets[{index}].handle has an invalid instagram handle format")
    return SocialTarget(target_id=target_id, platform=platform, handle=handle.lower())


def _validate_linkedin_company_url(value: Any, *, index: int) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"targets[{index}].company_url must be an explicit LinkedIn company URL")
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or not (hostname == "linkedin.com" or hostname.endswith(".linkedin.com"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or len(path_parts) != 2
        or path_parts[0].lower() != "company"
        or not path_parts[1]
        or parsed.query
        or parsed.fragment
    ):
        raise ManifestError(f"targets[{index}].company_url must be an explicit LinkedIn /company/ URL")
    canonical_path = f"/company/{path_parts[1]}"
    return urlunsplit(("https", hostname, canonical_path, "", ""))


def _normalize_profile(
    target: SocialTarget,
    endpoint_key: str,
    response: ProviderResponse,
) -> dict[str, Any]:
    payload = response.payload
    if endpoint_key == "linkedin_company":
        content = {
            "schema_version": 1,
            "record_type": "profile",
            "platform": "linkedin",
            "external_id": _string(payload.get("id")),
            "handle": _linkedin_slug(target.company_url),
            "canonical_url": target.company_url,
            "display_name": _string(payload.get("name")),
            "description": _string(payload.get("description")),
            "website": _string(payload.get("website")),
            "category": _string(payload.get("industry")),
            "location": _linkedin_location(payload),
            "metrics": _compact_mapping({"employee_count": _integer(payload.get("employeeCount"))}),
        }
    elif endpoint_key == "twitter_profile":
        legacy = _mapping(payload.get("legacy"))
        handle = _string(legacy.get("screen_name")) or target.handle
        content = {
            "schema_version": 1,
            "record_type": "profile",
            "platform": "twitter",
            "external_id": _string(payload.get("rest_id")) or _string(payload.get("id")),
            "handle": handle,
            "canonical_url": f"https://x.com/{handle}" if handle else None,
            "display_name": _string(legacy.get("name")),
            "description": _string(legacy.get("description")),
            "website": _expanded_profile_url(legacy),
            "category": None,
            "location": _string(legacy.get("location")),
            "metrics": _compact_mapping(
                {
                    "followers": _integer(legacy.get("followers_count")),
                    "following": _integer(legacy.get("friends_count")),
                    "posts": _integer(legacy.get("statuses_count")),
                    "likes": _integer(legacy.get("favourites_count")),
                }
            ),
        }
    elif endpoint_key == "instagram_profile":
        user = _mapping(_mapping(payload.get("data")).get("user"))
        handle = _string(user.get("username")) or target.handle
        content = {
            "schema_version": 1,
            "record_type": "profile",
            "platform": "instagram",
            "external_id": _string(user.get("id")) or _string(user.get("pk")),
            "handle": handle,
            "canonical_url": f"https://www.instagram.com/{handle}/" if handle else None,
            "display_name": _string(user.get("full_name")),
            "description": _string(user.get("biography")),
            "website": _string(user.get("external_url")),
            "category": _string(user.get("category_name")),
            "location": _instagram_location(user),
            "metrics": _compact_mapping(
                {
                    "followers": _nested_integer(user, "edge_followed_by", "count"),
                    "following": _nested_integer(user, "edge_follow", "count"),
                    "posts": _nested_integer(user, "edge_owner_to_timeline_media", "count"),
                }
            ),
        }
    else:  # pragma: no cover - endpoint plan controls this
        raise AssertionError(f"unsupported profile endpoint: {endpoint_key}")
    return _content_address(content, response.raw_response_sha256, target.target_id)


def _normalize_posts(
    target: SocialTarget,
    endpoint_key: str,
    response: ProviderResponse,
) -> list[dict[str, Any]]:
    payload = response.payload
    if endpoint_key == "linkedin_company_posts":
        raw_posts = _sequence(payload.get("posts"))
        bodies = [
            {
                "schema_version": 1,
                "record_type": "post",
                "platform": "linkedin",
                "external_id": _string(post.get("id")),
                "author_account_id": _post_author_account_id(post, "linkedin"),
                "author_handle": _post_author_handle(post, "linkedin") or _linkedin_slug(target.company_url),
                "canonical_url": _string(post.get("url")),
                "published_at": _normalize_datetime(post.get("datePublished")),
                "text": _string(post.get("text")),
                "media_kind": None,
                "metrics": {},
            }
            for post in raw_posts
        ]
    elif endpoint_key == "twitter_user_tweets":
        raw_posts = _sequence(payload.get("tweets"))
        bodies = []
        for post in raw_posts:
            legacy = _mapping(post.get("legacy"))
            bodies.append(
                {
                    "schema_version": 1,
                    "record_type": "post",
                    "platform": "twitter",
                    "external_id": _string(post.get("rest_id")) or _string(legacy.get("id_str")),
                    "author_account_id": _post_author_account_id(post, "twitter"),
                    "author_handle": _post_author_handle(post, "twitter") or target.handle,
                    "canonical_url": _string(post.get("url")),
                    "published_at": _normalize_datetime(legacy.get("created_at")),
                    "text": _string(legacy.get("full_text")),
                    "media_kind": "tweet",
                    "metrics": _compact_mapping(
                        {
                            "likes": _integer(legacy.get("favorite_count")),
                            "replies": _integer(legacy.get("reply_count")),
                            "reposts": _integer(legacy.get("retweet_count")),
                            "quotes": _integer(legacy.get("quote_count")),
                            "bookmarks": _integer(legacy.get("bookmark_count")),
                            "views": _integer(_mapping(post.get("views")).get("count")),
                        }
                    ),
                }
            )
    elif endpoint_key == "instagram_user_posts_v2":
        raw_posts = _sequence(payload.get("items"))
        bodies = []
        for post in raw_posts:
            code = _string(post.get("code"))
            owner = _mapping(post.get("user")) or _mapping(post.get("owner"))
            bodies.append(
                {
                    "schema_version": 1,
                    "record_type": "post",
                    "platform": "instagram",
                    "external_id": _string(post.get("id")) or _string(post.get("pk")),
                    "author_account_id": _post_author_account_id(post, "instagram"),
                    "author_handle": _post_author_handle(post, "instagram") or _string(owner.get("username")) or target.handle,
                    "canonical_url": _string(post.get("url"))
                    or (f"https://www.instagram.com/p/{code}/" if code else None),
                    "published_at": _normalize_datetime(post.get("taken_at")),
                    "text": _instagram_caption(post),
                    "media_kind": _instagram_media_kind(post.get("media_type"), post.get("product_type")),
                    "metrics": _compact_mapping(
                        {
                            "likes": _integer(post.get("like_count")),
                            "comments": _integer(post.get("comment_count")),
                            "plays": _first_integer(post.get("play_count"), post.get("ig_play_count")),
                        }
                    ),
                }
            )
    else:  # pragma: no cover - endpoint plan controls this
        raise AssertionError(f"unsupported posts endpoint: {endpoint_key}")

    return [_content_address(body, response.raw_response_sha256, target.target_id) for body in bodies]


def _post_author_account_id(post: Mapping[str, Any], platform: str) -> str | None:
    """Read an explicit author id when the provider repeats it on a post."""

    for key in ("author_account_id", "author_id", "account_id", "user_id", "userId"):
        value = _string(post.get(key))
        if value is not None:
            return value
    for key in ("author", "user", "owner"):
        candidate = _mapping(post.get(key))
        for id_key in ("account_id", "author_account_id", "id", "pk", "rest_id", "user_id"):
            value = _string(candidate.get(id_key))
            if value is not None:
                return value
    if platform == "twitter":
        core = _mapping(post.get("core"))
        user_result = _mapping(_mapping(core.get("user_results")).get("result"))
        for id_key in ("rest_id", "id"):
            value = _string(user_result.get(id_key))
            if value is not None:
                return value
    return None


def _post_author_handle(post: Mapping[str, Any], platform: str) -> str | None:
    for key in ("author_handle", "handle", "username", "screen_name"):
        value = _string(post.get(key))
        if value is not None:
            return value
    for key in ("author", "user", "owner"):
        candidate = _mapping(post.get(key))
        for handle_key in ("handle", "username", "screen_name"):
            value = _string(candidate.get(handle_key))
            if value is not None:
                return value
    if platform == "twitter":
        core = _mapping(post.get("core"))
        user_result = _mapping(_mapping(core.get("user_results")).get("result"))
        for handle_key in ("screen_name", "username"):
            value = _string(user_result.get(handle_key))
            if value is not None:
                return value
    return None


def _content_address(content: Mapping[str, Any], raw_response_sha256: str, target_id: str) -> dict[str, Any]:
    # Keep the legacy provider row useful for callers of the transferred spike,
    # but never include measurements in its identity.  Laboratory consumers
    # receive the stricter B1 contract ID attached by ``run_acquisition``.
    semantic_content = {key: value for key, value in content.items() if key != "metrics"}
    digest = _sha256_bytes(_canonical_json_bytes(semantic_content))
    return {
        **content,
        "content_id": f"sha256:{digest}",
        "source_response_sha256": raw_response_sha256,
        "target_id": target_id,
    }


def _redact_payload(value: Any, secret: str) -> Any:
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, child in value.items():
            rendered_key = str(key)
            normalized_key = rendered_key.strip().lower()
            safe_key = rendered_key.replace(secret, "[REDACTED]") if secret else rendered_key
            if normalized_key in _SENSITIVE_KEYS:
                safe[safe_key] = "[REDACTED]"
            else:
                safe[safe_key] = _redact_payload(child, secret)
        return safe
    if isinstance(value, list):
        return [_redact_payload(child, secret) for child in value]
    if isinstance(value, tuple):
        return [_redact_payload(child, secret) for child in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _first_integer(*values: Any) -> int | None:
    for value in values:
        normalized = _integer(value)
        if normalized is not None:
            return normalized
    return None


def _compact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: child for key, child in value.items() if child is not None}


def _nested_integer(value: Mapping[str, Any], *path: str) -> int | None:
    cursor: Any = value
    for part in path:
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(part)
    return _integer(cursor)


def _linkedin_slug(company_url: str | None) -> str | None:
    if not company_url:
        return None
    parts = [part for part in urlsplit(company_url).path.split("/") if part]
    return parts[1] if len(parts) == 2 else None


def _linkedin_location(payload: Mapping[str, Any]) -> str | None:
    location = _mapping(payload.get("location"))
    parts = [_string(location.get(part)) for part in ("city", "state", "country")]
    compact = [part for part in parts if part]
    return ", ".join(compact) or _string(payload.get("headquarters"))


def _instagram_location(user: Mapping[str, Any]) -> str | None:
    address = user.get("business_address_json")
    if isinstance(address, str):
        try:
            address = json.loads(address)
        except json.JSONDecodeError:
            address = {}
    location = _mapping(address)
    parts = [_string(location.get(part)) for part in ("street_address", "city_name", "zip_code")]
    return ", ".join(part for part in parts if part) or None


def _expanded_profile_url(legacy: Mapping[str, Any]) -> str | None:
    urls = _sequence(_mapping(_mapping(legacy.get("entities")).get("url")).get("urls"))
    if not urls:
        return None
    return _string(urls[0].get("expanded_url")) or _string(urls[0].get("url"))


def _instagram_caption(post: Mapping[str, Any]) -> str | None:
    caption = post.get("caption")
    if isinstance(caption, Mapping):
        return _string(caption.get("text"))
    return _string(caption)


def _instagram_media_kind(media_type: Any, product_type: Any) -> str | None:
    product = _string(product_type)
    if product:
        return product
    numeric_type = _integer(media_type)
    return {1: "image", 2: "video", 8: "carousel"}.get(numeric_type)


def _normalize_datetime(value: Any) -> str | None:
    parsed: datetime | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        rendered = value.strip()
        try:
            parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(rendered)
            except (TypeError, ValueError, OverflowError):
                return rendered
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
