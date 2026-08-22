"""Provider-neutral contracts for the isolated social/community laboratory.

The laboratory deliberately keeps two kinds of information apart:

* semantic observation fields, which identify what was published and who
  published/interacted with it; and
* acquisition/metric fields, which describe how and when the observation was
  fetched or measured.

Only the first group participates in ``content_id`` and
``semantic_fingerprint``.  This is the important boundary that prevents a
changing like count, follower count, cursor, or fetch timestamp from changing
the identity of a social post.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
import unicodedata
from types import MappingProxyType
from typing import Any, TypeAlias
from urllib.parse import SplitResult, urlsplit, urlunsplit


SOCIAL_LAB_CONTRACT_VERSION = "b3s-social-community-lab-v1"
SOCIAL_LAB_NORMALIZATION_VERSION = "social-lab-normalization-v1"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class SocialLabContractError(ValueError):
    """Base domain error for malformed or invalid laboratory contracts."""


class SocialLabMalformedInputError(SocialLabContractError):
    """Raised when an input has an unsupported or type-confused shape."""


class SocialLabValidationError(SocialLabContractError):
    """Raised when a correctly shaped input violates a contract invariant."""


# Short aliases make the domain error convenient for callers without exposing
# implementation-specific ``TypeError``/``KeyError``/``JSONDecodeError``
# details.
ContractError = SocialLabContractError
MalformedInputError = SocialLabMalformedInputError
ValidationError = SocialLabValidationError


class ActorRole(str, Enum):
    """Closed actor-role vocabulary for social observations."""

    OFFICIAL_BRAND_POST = "official_brand_post"
    COMMUNITY_RESPONSE = "community_response"
    BRAND_REPLY = "brand_reply"
    UNCLASSIFIED = "unclassified"


class RecordKind(str, Enum):
    """Provider-neutral record categories used for fail-closed role mapping."""

    POST = "post"
    RESPONSE = "response"
    REPLY = "reply"
    OBSERVATION = "observation"


ACTOR_ROLES = tuple(role.value for role in ActorRole)
RECORD_KINDS = tuple(kind.value for kind in RecordKind)

_TOP_LEVEL_KINDS = frozenset(
    {
        "post",
        "status",
        "publication",
        "original_post",
        "original_content",
        "tweet",
        "video",
        "photo",
        "article",
    }
)
_INTERACTION_KINDS = frozenset(
    {
        "response",
        "reply",
        "comment",
        "community_response",
        "brand_reply",
        "conversation",
        "observation",
    }
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def _domain_error(message: str, *, malformed: bool = False) -> SocialLabContractError:
    error_type = SocialLabMalformedInputError if malformed else SocialLabValidationError
    return error_type(message)


def _require_text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _domain_error(f"{field_name} must be a string", malformed=True)
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized and not allow_empty:
        raise _domain_error(f"{field_name} must not be empty")
    return normalized


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = _require_text(value, field_name)
    return normalized or None


def _normalize_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise _domain_error(f"{field_name} must be a string", malformed=True)
    # Provider serializers commonly differ in line endings and incidental
    # whitespace.  Preserve words, not transport formatting.
    normalized = unicodedata.normalize("NFC", value)
    return " ".join(normalized.split())


def _normalize_token(value: Any, field_name: str) -> str:
    token = _require_text(value, field_name).casefold()
    return token.replace("-", "_").replace(" ", "_")


def _normalize_url(value: Any, field_name: str) -> str:
    raw = _require_text(value, field_name)
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("absolute HTTP(S) URL required")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL credentials are not allowed")
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("URL hostname is required")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid URL port") from exc
        netloc = hostname.casefold()
        if ":" in hostname and not netloc.startswith("["):
            netloc = f"[{netloc}]"
        if port is not None and not ((parsed.scheme.casefold() == "http" and port == 80) or (parsed.scheme.casefold() == "https" and port == 443)):
            netloc = f"{netloc}:{port}"
        path = parsed.path or "/"
        if len(path) > 1:
            path = path.rstrip("/")
        return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))
    except SocialLabContractError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _domain_error(f"{field_name} must be a valid absolute HTTP(S) URL: {exc}") from None


def _format_datetime(value: datetime) -> str:
    utc_value = value.astimezone(timezone.utc)
    timespec = "microseconds" if utc_value.microsecond else "seconds"
    return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _normalize_timestamp(value: Any, field_name: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            if allow_none:
                return None
            raise _domain_error(f"{field_name} must not be empty")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise _domain_error(f"{field_name} must be an ISO-8601 timestamp: {exc}") from None
    else:
        raise _domain_error(f"{field_name} must be a datetime or ISO-8601 string", malformed=True)
    if parsed.tzinfo is None:
        # A naive provider timestamp has no offset to preserve.  Treat it as
        # UTC once, making the conversion deterministic and explicit.
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return _format_datetime(parsed)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _domain_error(f"{field_name} could not be normalized: {exc}") from None


def _normalize_json_value(value: Any, field_name: str) -> JsonValue:
    """Validate JSON-compatible input without leaking built-in exceptions."""

    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            return unicodedata.normalize("NFC", value)
        if isinstance(value, bool):
            return value
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _domain_error(f"{field_name} must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        try:
            items = value.items()
        except (AttributeError, TypeError) as exc:
            raise _domain_error(f"{field_name} must be a JSON object: {exc}", malformed=True) from None
        for key, item in items:
            if not isinstance(key, str):
                raise _domain_error(f"{field_name} keys must be strings", malformed=True)
            result[unicodedata.normalize("NFC", key)] = _normalize_json_value(item, f"{field_name}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        return [_normalize_json_value(item, f"{field_name}[]") for item in value]
    raise _domain_error(f"{field_name} contains a non-JSON-compatible value", malformed=True)


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})  # type: ignore[return-value]
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)  # type: ignore[return-value]
    return value


def _thaw_json(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if isinstance(value, list):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise _domain_error(f"value is not deterministically JSON serializable: {exc}", malformed=True) from None


def _sha256(value: Any) -> str:
    encoded = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_text(value: Any, field_name: str) -> str:
    return _require_text(value, field_name)


def _normalize_identity(value: Any, field_name: str = "target_identity") -> JsonValue:
    if isinstance(value, str):
        return _require_text(value, field_name)
    normalized = _normalize_json_value(value, field_name)
    if not isinstance(normalized, dict) or not normalized:
        raise _domain_error(f"{field_name} must be a non-empty string or object")
    return normalized


def _mapping_value(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _role_bound_identity(target_identity: str | Mapping[str, Any]) -> str | Mapping[str, Any] | None:
    """Return only identity shapes that can support exact role resolution.

    Provenance may carry a richer target descriptor for acquisition purposes.
    The role boundary uses an exact account identity when one is present; an
    unresolvable descriptor must conservatively produce ``unclassified``
    rather than becoming an implicit brand identity.
    """

    if isinstance(target_identity, str):
        return target_identity
    if _mapping_value(target_identity, "account_id", "author_account_id", "id") is None:
        return None
    return target_identity


@dataclass(frozen=True, slots=True)
class MetricContext:
    """Mutable acquisition measurements kept outside semantic observation data."""

    metrics: Mapping[str, Any]
    observed_at: str | datetime | date
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.metrics, Mapping):
            raise _domain_error("metric_context.metrics must be a mapping", malformed=True)
        normalized_metrics: dict[str, JsonValue] = {}
        for name, value in self.metrics.items():
            if not isinstance(name, str) or not name.strip():
                raise _domain_error("metric names must be non-empty strings", malformed=True)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _domain_error(f"metric {name!r} must be a number", malformed=True)
            if isinstance(value, float) and not math.isfinite(value):
                raise _domain_error(f"metric {name!r} must be finite")
            if value < 0:
                raise _domain_error(f"metric {name!r} must not be negative")
            normalized_metrics[unicodedata.normalize("NFC", name).strip()] = value
        if not normalized_metrics:
            raise _domain_error("metric_context.metrics must not be empty")
        normalized_observed_at = _normalize_timestamp(self.observed_at, "metric_context.observed_at")
        assert normalized_observed_at is not None
        frozen_metrics = _freeze_json(normalized_metrics)
        object.__setattr__(self, "metrics", frozen_metrics)
        object.__setattr__(self, "observed_at", normalized_observed_at)
        object.__setattr__(self, "fingerprint", "sha256:" + _sha256({"metrics": normalized_metrics, "observed_at": normalized_observed_at}))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MetricContext:
        if not isinstance(value, Mapping):
            raise _domain_error("metric_context must be an object", malformed=True)
        try:
            return cls(metrics=value["metrics"], observed_at=value["observed_at"])
        except SocialLabContractError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise _domain_error(f"invalid metric_context: {exc}", malformed=True) from None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "metrics": _thaw_json(self.metrics),
            "observed_at": self.observed_at,
            "fingerprint": self.fingerprint,
        }

    def __hash__(self) -> int:
        return hash(self.fingerprint)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Evidence binding for one provider response item."""

    provider: str
    endpoint_key: str
    target_fingerprint: str
    manifest_fingerprint: str
    request_fingerprint: str
    response_sha256: str
    fetched_at: str | datetime | date
    page: int | None
    cursor: str | None
    result_ordinal: int
    target_identity: str | Mapping[str, Any]
    normalization_version: str = SOCIAL_LAB_NORMALIZATION_VERSION
    linkage_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        provider = _require_text(self.provider, "provenance.provider")
        endpoint_key = _require_text(self.endpoint_key, "provenance.endpoint_key")
        target_fingerprint = _hash_text(self.target_fingerprint, "provenance.target_fingerprint")
        manifest_fingerprint = _hash_text(self.manifest_fingerprint, "provenance.manifest_fingerprint")
        request_fingerprint = _hash_text(self.request_fingerprint, "provenance.request_fingerprint")
        response_sha256 = _require_text(self.response_sha256, "provenance.response_sha256").lower()
        if not _SHA256_RE.fullmatch(response_sha256):
            raise _domain_error("provenance.response_sha256 must be a 64-character SHA-256 hex digest")
        fetched_at = _normalize_timestamp(self.fetched_at, "provenance.fetched_at")
        assert fetched_at is not None
        if self.page is not None and (isinstance(self.page, bool) or not isinstance(self.page, int)):
            raise _domain_error("provenance.page must be an integer or null", malformed=True)
        if self.page is not None and self.page < 0:
            raise _domain_error("provenance.page must not be negative")
        cursor = _optional_text(self.cursor, "provenance.cursor")
        if isinstance(self.result_ordinal, bool) or not isinstance(self.result_ordinal, int):
            raise _domain_error("provenance.result_ordinal must be an integer", malformed=True)
        if self.result_ordinal < 0:
            raise _domain_error("provenance.result_ordinal must not be negative")
        target_identity = _freeze_json(_normalize_identity(self.target_identity))
        normalization_version = _require_text(self.normalization_version, "provenance.normalization_version")
        if not isinstance(self.linkage_evidence, Mapping):
            raise _domain_error("provenance.linkage_evidence must be an object", malformed=True)
        linkage_evidence = _freeze_json(_normalize_json_value(self.linkage_evidence, "provenance.linkage_evidence"))
        assert isinstance(linkage_evidence, Mapping)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "endpoint_key", endpoint_key)
        object.__setattr__(self, "target_fingerprint", target_fingerprint)
        object.__setattr__(self, "manifest_fingerprint", manifest_fingerprint)
        object.__setattr__(self, "request_fingerprint", request_fingerprint)
        object.__setattr__(self, "response_sha256", response_sha256)
        object.__setattr__(self, "fetched_at", fetched_at)
        object.__setattr__(self, "cursor", cursor)
        object.__setattr__(self, "target_identity", target_identity)
        object.__setattr__(self, "normalization_version", normalization_version)
        object.__setattr__(self, "linkage_evidence", linkage_evidence)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Provenance:
        if not isinstance(value, Mapping):
            raise _domain_error("provenance must be an object", malformed=True)
        try:
            target_fingerprint = _mapping_value(value, "target_fingerprint", "target_manifest_fingerprint")
            manifest_fingerprint = _mapping_value(value, "manifest_fingerprint", "target_manifest_fingerprint")
            return cls(
                provider=value["provider"],
                endpoint_key=_mapping_value(value, "endpoint_key", "endpoint"),
                target_fingerprint=target_fingerprint,
                manifest_fingerprint=manifest_fingerprint,
                request_fingerprint=_mapping_value(value, "request_fingerprint", "request_hash"),
                response_sha256=_mapping_value(value, "response_sha256", "response_hash"),
                fetched_at=_mapping_value(value, "fetched_at", "fetch_timestamp"),
                page=value.get("page"),
                cursor=value.get("cursor"),
                result_ordinal=value.get("result_ordinal", value.get("ordinal", 0)),
                target_identity=value["target_identity"],
                normalization_version=value.get("normalization_version", SOCIAL_LAB_NORMALIZATION_VERSION),
                linkage_evidence=_mapping_value(value, "linkage_evidence", "parent_thread_linkage_evidence") or {},
            )
        except SocialLabContractError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise _domain_error(f"invalid provenance: {exc}", malformed=True) from None

    @property
    def target_manifest_fingerprint(self) -> str:
        """Stable combined target/manifest binding for consumers that need one key."""

        return "sha256:" + _sha256(
            {"target_fingerprint": self.target_fingerprint, "manifest_fingerprint": self.manifest_fingerprint}
        )

    @property
    def fetch_timestamp(self) -> str:
        return self.fetched_at

    @property
    def parent_thread_linkage_evidence(self) -> Mapping[str, JsonValue]:
        return self.linkage_evidence

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "provider": self.provider,
            "endpoint_key": self.endpoint_key,
            "target_fingerprint": self.target_fingerprint,
            "manifest_fingerprint": self.manifest_fingerprint,
            "request_fingerprint": self.request_fingerprint,
            "response_sha256": self.response_sha256,
            "fetched_at": self.fetched_at,
            "page": self.page,
            "cursor": self.cursor,
            "result_ordinal": self.result_ordinal,
            "target_identity": _thaw_json(self.target_identity),
            "normalization_version": self.normalization_version,
            "linkage_evidence": _thaw_json(self.linkage_evidence),
        }

    def __hash__(self) -> int:
        return hash(_canonical_json(self.to_dict()))


@dataclass(frozen=True, slots=True)
class SocialObservation:
    """Frozen provider-neutral social observation contract."""

    platform: str
    record_kind: str | RecordKind
    external_id: str
    canonical_url: str
    author_account_id: str | None
    author_handle: str | None
    parent_external_id: str | None
    thread_external_id: str | None
    text: str
    media_kind: str | None
    published_at: str | datetime | date | None
    actor_role: str | ActorRole
    provenance: Provenance | Mapping[str, Any]
    metric_context: MetricContext | Mapping[str, Any] | None = None
    content_id: str | None = None
    semantic_fingerprint: str | None = None
    contract_version: str = SOCIAL_LAB_CONTRACT_VERSION

    def __post_init__(self) -> None:
        platform = _normalize_token(self.platform, "platform")
        record_kind = _normalize_token(self.record_kind.value if isinstance(self.record_kind, RecordKind) else self.record_kind, "record_kind")
        external_id = _require_text(self.external_id, "external_id")
        canonical_url = _normalize_url(self.canonical_url, "canonical_url")
        author_account_id = _optional_text(self.author_account_id, "author_account_id")
        author_handle = _optional_text(self.author_handle, "author_handle")
        if author_handle is not None:
            author_handle = author_handle.lstrip("@").casefold()
        parent_external_id = _optional_text(self.parent_external_id, "parent_external_id")
        thread_external_id = _optional_text(self.thread_external_id, "thread_external_id")
        text = _normalize_text(self.text, "text")
        media_kind = _optional_text(self.media_kind, "media_kind")
        if media_kind is not None:
            media_kind = _normalize_token(media_kind, "media_kind")
        published_at = _normalize_timestamp(self.published_at, "published_at", allow_none=True)
        actor_role = self.actor_role.value if isinstance(self.actor_role, ActorRole) else _normalize_token(self.actor_role, "actor_role")
        if actor_role not in ACTOR_ROLES:
            raise _domain_error(f"actor_role must be one of {ACTOR_ROLES}")
        contract_version = _require_text(self.contract_version, "contract_version")
        if contract_version != SOCIAL_LAB_CONTRACT_VERSION:
            raise _domain_error(f"unsupported contract_version: {contract_version!r}")
        if actor_role == ActorRole.OFFICIAL_BRAND_POST.value and (parent_external_id or thread_external_id):
            raise _domain_error("official_brand_post must be top-level content")
        if actor_role in {ActorRole.COMMUNITY_RESPONSE.value, ActorRole.BRAND_REPLY.value} and not (parent_external_id or thread_external_id):
            raise _domain_error(f"{actor_role} requires parent_external_id or thread_external_id")
        if not isinstance(self.provenance, Provenance):
            provenance = Provenance.from_dict(self.provenance)
        else:
            provenance = self.provenance
        resolved_role = resolve_actor_role(
            record_kind=record_kind,
            author_account_id=author_account_id,
            author_handle=author_handle,
            bound_brand_identity=_role_bound_identity(provenance.target_identity),
            parent_external_id=parent_external_id,
            thread_external_id=thread_external_id,
        )
        if actor_role != resolved_role.value:
            raise _domain_error(
                f"actor_role {actor_role!r} does not match fail-closed role resolution {resolved_role.value!r}"
            )
        if self.metric_context is None or isinstance(self.metric_context, MetricContext):
            metric_context = self.metric_context
        else:
            metric_context = MetricContext.from_dict(self.metric_context)
        semantic_values: dict[str, JsonValue] = {
            "contract_version": contract_version,
            "platform": platform,
            "record_kind": record_kind,
            "external_id": external_id,
            "canonical_url": canonical_url,
            "author_account_id": author_account_id,
            "author_handle": author_handle,
            "parent_external_id": parent_external_id,
            "thread_external_id": thread_external_id,
            "text": text,
            "media_kind": media_kind,
            "published_at": published_at,
            "actor_role": actor_role,
        }
        fingerprint = _sha256(semantic_values)
        derived_content_id = "sha256:" + fingerprint
        if self.content_id is not None and self.content_id != derived_content_id:
            raise _domain_error("content_id does not match the metric-free semantic payload")
        if self.semantic_fingerprint is not None and self.semantic_fingerprint != fingerprint:
            raise _domain_error("semantic_fingerprint does not match the metric-free semantic payload")
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "record_kind", record_kind)
        object.__setattr__(self, "external_id", external_id)
        object.__setattr__(self, "canonical_url", canonical_url)
        object.__setattr__(self, "author_account_id", author_account_id)
        object.__setattr__(self, "author_handle", author_handle)
        object.__setattr__(self, "parent_external_id", parent_external_id)
        object.__setattr__(self, "thread_external_id", thread_external_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "media_kind", media_kind)
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "actor_role", actor_role)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "metric_context", metric_context)
        object.__setattr__(self, "contract_version", contract_version)
        object.__setattr__(self, "semantic_fingerprint", fingerprint)
        object.__setattr__(self, "content_id", derived_content_id)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SocialObservation:
        if not isinstance(value, Mapping):
            raise _domain_error("observation must be an object", malformed=True)
        try:
            # Replay is a trust boundary: actor_role/content_id/semantic_fingerprint
            # are claims, never constructor inputs that may determine identity.  The
            # target identity persisted in provenance is normalized first, then the
            # canonical constructor resolves the role from primitive content and
            # linkage.  Supplying a stale or forged claim is rejected by the
            # constructor rather than silently relabelled.
            provenance = Provenance.from_dict(value["provenance"])
            claimed_role = value.get("actor_role")
            if "actor_role" in value and claimed_role is None:
                raise _domain_error("actor_role must be a string", malformed=True)
            for derived_name in ("content_id", "semantic_fingerprint"):
                if derived_name in value and value[derived_name] is None:
                    raise _domain_error(f"{derived_name} must be a string", malformed=True)
            return build_social_observation(
                platform=value["platform"],
                record_kind=value["record_kind"],
                external_id=value["external_id"],
                canonical_url=value["canonical_url"],
                author_account_id=value.get("author_account_id"),
                author_handle=value.get("author_handle"),
                parent_external_id=value.get("parent_external_id"),
                thread_external_id=value.get("thread_external_id"),
                text=value["text"],
                media_kind=value.get("media_kind"),
                published_at=value.get("published_at"),
                provenance=provenance,
                bound_brand_identity=_role_bound_identity(provenance.target_identity),
                metric_context=value.get("metric_context"),
                content_id=value.get("content_id"),
                semantic_fingerprint=value.get("semantic_fingerprint"),
                contract_version=value.get("contract_version", SOCIAL_LAB_CONTRACT_VERSION),
                actor_role=claimed_role,
            )
        except SocialLabContractError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise _domain_error(f"invalid observation: {exc}", malformed=True) from None

    def semantic_dict(self) -> dict[str, JsonValue]:
        return _semantic_values(self)

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            **self.semantic_dict(),
            "content_id": self.content_id,
            "semantic_fingerprint": self.semantic_fingerprint,
            "provenance": self.provenance.to_dict(),
        }
        if self.metric_context is not None:
            result["metric_context"] = self.metric_context.to_dict()
        else:
            result["metric_context"] = None
        return result

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    def __hash__(self) -> int:
        return hash(self.content_id)


# Names used by neighboring laboratory nodes can remain concise while the
# explicit Social* names make the public boundary self-documenting.
Observation = SocialObservation
SocialLabObservation = SocialObservation
SocialProvenance = Provenance
SocialLabProvenance = Provenance
SocialMetricContext = MetricContext
SocialLabMetricContext = MetricContext


def _semantic_values(observation: SocialObservation) -> dict[str, JsonValue]:
    return {
        "contract_version": observation.contract_version,
        "platform": observation.platform,
        "record_kind": observation.record_kind,
        "external_id": observation.external_id,
        "canonical_url": observation.canonical_url,
        "author_account_id": observation.author_account_id,
        "author_handle": observation.author_handle,
        "parent_external_id": observation.parent_external_id,
        "thread_external_id": observation.thread_external_id,
        "text": observation.text,
        "media_kind": observation.media_kind,
        "published_at": observation.published_at,
        "actor_role": observation.actor_role,
    }


def _coerce_identity(
    bound_brand_identity: str | Mapping[str, Any] | None,
    bound_brand_account_id: str | None,
    bound_brand_handle: str | None,
) -> tuple[str | None, str | None]:
    account_id: str | None = None
    handle: str | None = None
    if bound_brand_identity is not None:
        if isinstance(bound_brand_identity, str):
            account_id = _require_text(bound_brand_identity, "bound_brand_identity")
        elif isinstance(bound_brand_identity, Mapping):
            raw_account_id = _mapping_value(bound_brand_identity, "account_id", "author_account_id", "id")
            raw_handle = _mapping_value(bound_brand_identity, "handle", "author_handle", "username")
            if raw_account_id is not None:
                account_id = _require_text(raw_account_id, "bound_brand_identity.account_id")
            if raw_handle is not None:
                handle = _require_text(raw_handle, "bound_brand_identity.handle").lstrip("@").casefold()
            if account_id is None:
                raise _domain_error("bound_brand_identity requires an exact account_id", malformed=True)
        else:
            raise _domain_error("bound_brand_identity must be a string or object", malformed=True)
    if bound_brand_account_id is not None:
        candidate = _require_text(bound_brand_account_id, "bound_brand_account_id")
        if account_id is not None and candidate != account_id:
            raise _domain_error("bound brand account identities conflict")
        account_id = candidate
    if bound_brand_handle is not None:
        candidate_handle = _require_text(bound_brand_handle, "bound_brand_handle").lstrip("@").casefold()
        if handle is not None and candidate_handle != handle:
            raise _domain_error("bound brand handles conflict")
        handle = candidate_handle
    return account_id, handle


def _identity_matches(
    author_account_id: str | None,
    author_handle: str | None,
    brand_account_id: str | None,
    brand_handle: str | None,
) -> bool:
    if brand_account_id is None or author_account_id is None or author_account_id != brand_account_id:
        return False
    if brand_handle is not None:
        return author_handle is not None and author_handle.lstrip("@").casefold() == brand_handle
    return True


def _different_author(
    author_account_id: str | None,
    author_handle: str | None,
    brand_account_id: str | None,
    brand_handle: str | None,
) -> bool:
    if brand_account_id is not None:
        # An exact bound account is available, so a missing author account
        # cannot prove that the response came from a different actor.
        if author_account_id is None:
            return False
        return author_account_id != brand_account_id
    if brand_handle is not None and author_handle is not None:
        return author_handle.lstrip("@").casefold() != brand_handle
    # Missing identity cannot prove that this is a community actor.
    return False


def resolve_actor_role(
    *,
    record_kind: str | RecordKind | None,
    author_account_id: str | None,
    author_handle: str | None,
    bound_brand_identity: str | Mapping[str, Any] | None = None,
    bound_brand_account_id: str | None = None,
    bound_brand_handle: str | None = None,
    parent_external_id: str | None,
    thread_external_id: str | None,
    is_top_level: bool | None = None,
) -> ActorRole:
    """Resolve a role conservatively; ambiguous records become unclassified."""

    normalized_author_id = _optional_text(author_account_id, "author_account_id")
    normalized_author_handle = _optional_text(author_handle, "author_handle")
    if normalized_author_handle is not None:
        normalized_author_handle = normalized_author_handle.lstrip("@").casefold()
    normalized_parent = _optional_text(parent_external_id, "parent_external_id")
    normalized_thread = _optional_text(thread_external_id, "thread_external_id")
    if is_top_level is not None and not isinstance(is_top_level, bool):
        raise _domain_error("is_top_level must be a boolean or null", malformed=True)
    top_level = is_top_level if is_top_level is not None else not (normalized_parent or normalized_thread)
    linkage_resolved = bool(normalized_parent or normalized_thread)
    brand_account_id, brand_handle = _coerce_identity(
        bound_brand_identity,
        bound_brand_account_id,
        bound_brand_handle,
    )
    exact_brand = _identity_matches(normalized_author_id, normalized_author_handle, brand_account_id, brand_handle)
    different_author = _different_author(
        normalized_author_id,
        normalized_author_handle,
        brand_account_id,
        brand_handle,
    )
    if isinstance(record_kind, RecordKind):
        normalized_kind = record_kind.value
    elif record_kind is None:
        normalized_kind = "post" if top_level else "response"
    else:
        normalized_kind = _normalize_token(record_kind, "record_kind")
    if exact_brand and top_level and normalized_kind in _TOP_LEVEL_KINDS:
        return ActorRole.OFFICIAL_BRAND_POST
    if linkage_resolved and normalized_kind in _INTERACTION_KINDS:
        if exact_brand:
            return ActorRole.BRAND_REPLY
        if different_author:
            return ActorRole.COMMUNITY_RESPONSE
    return ActorRole.UNCLASSIFIED


classify_actor_role = resolve_actor_role
resolve_role = resolve_actor_role


def build_social_observation(
    *,
    platform: str,
    record_kind: str | RecordKind,
    external_id: str,
    canonical_url: str,
    author_account_id: str | None,
    author_handle: str | None,
    parent_external_id: str | None,
    thread_external_id: str | None,
    text: str,
    media_kind: str | None,
    published_at: str | datetime | date | None,
    provenance: Provenance | Mapping[str, Any],
    bound_brand_identity: str | Mapping[str, Any] | None = None,
    bound_brand_account_id: str | None = None,
    bound_brand_handle: str | None = None,
    metric_context: MetricContext | Mapping[str, Any] | None = None,
    actor_role: str | ActorRole | None = None,
    content_id: str | None = None,
    semantic_fingerprint: str | None = None,
    contract_version: str = SOCIAL_LAB_CONTRACT_VERSION,
) -> SocialObservation:
    """Construct an observation after resolving its role against bound identity."""

    resolved_role = resolve_actor_role(
        record_kind=record_kind,
        author_account_id=author_account_id,
        author_handle=author_handle,
        bound_brand_identity=bound_brand_identity,
        bound_brand_account_id=bound_brand_account_id,
        bound_brand_handle=bound_brand_handle,
        parent_external_id=parent_external_id,
        thread_external_id=thread_external_id,
    )
    if actor_role is not None:
        requested_role = actor_role.value if isinstance(actor_role, ActorRole) else _normalize_token(actor_role, "actor_role")
        if requested_role != resolved_role.value:
            raise _domain_error(
                f"actor_role {requested_role!r} does not match fail-closed role resolution {resolved_role.value!r}"
            )
    return SocialObservation(
        platform=platform,
        record_kind=record_kind,
        external_id=external_id,
        canonical_url=canonical_url,
        author_account_id=author_account_id,
        author_handle=author_handle,
        parent_external_id=parent_external_id,
        thread_external_id=thread_external_id,
        text=text,
        media_kind=media_kind,
        published_at=published_at,
        actor_role=resolved_role,
        provenance=provenance,
        metric_context=metric_context,
        content_id=content_id,
        semantic_fingerprint=semantic_fingerprint,
        contract_version=contract_version,
    )


make_social_observation = build_social_observation
create_observation = build_social_observation


def semantic_fingerprint(value: SocialObservation | Mapping[str, Any]) -> str:
    """Return the metric/provenance-free semantic digest for an observation."""

    if isinstance(value, SocialObservation):
        return value.semantic_fingerprint
    if not isinstance(value, Mapping):
        raise _domain_error("semantic_fingerprint expects an observation or object", malformed=True)
    try:
        if "semantic_fingerprint" in value and set(value).issubset(
            {"contract_version", "platform", "record_kind", "external_id", "canonical_url", "author_account_id", "author_handle", "parent_external_id", "thread_external_id", "text", "media_kind", "published_at", "actor_role", "content_id", "semantic_fingerprint", "provenance", "metric_context"}
        ):
            return SocialObservation.from_dict(value).semantic_fingerprint
        semantic = {key: value[key] for key in (
            "contract_version", "platform", "record_kind", "external_id", "canonical_url", "author_account_id", "author_handle", "parent_external_id", "thread_external_id", "text", "media_kind", "published_at", "actor_role"
        )}
    except SocialLabContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _domain_error(f"invalid semantic observation: {exc}", malformed=True) from None
    normalized = SocialObservation.from_dict(
        {
            **semantic,
            "provenance": value.get("provenance") or _minimal_provenance_for_hash(),
            "metric_context": None,
        }
    )
    return normalized.semantic_fingerprint


def content_id(value: SocialObservation | Mapping[str, Any]) -> str:
    """Return the stable content ID derived solely from semantic fields."""

    return "sha256:" + semantic_fingerprint(value)


def metric_fingerprint(
    metrics_or_context: MetricContext | Mapping[str, Any],
    observed_at: str | datetime | date | None = None,
) -> str:
    """Return the independent digest for an engagement/follower snapshot."""

    if isinstance(metrics_or_context, MetricContext):
        if observed_at is not None:
            raise _domain_error("observed_at must not be supplied with MetricContext", malformed=True)
        return metrics_or_context.fingerprint
    if not isinstance(metrics_or_context, Mapping):
        raise _domain_error("metrics_or_context must be a mapping or MetricContext", malformed=True)
    if observed_at is None and "metrics" in metrics_or_context and "observed_at" in metrics_or_context:
        context = MetricContext.from_dict(metrics_or_context)
    elif observed_at is not None:
        context = MetricContext(metrics=metrics_or_context, observed_at=observed_at)
    else:
        raise _domain_error("observed_at is required when passing raw metrics", malformed=True)
    return context.fingerprint


fingerprint_metrics = metric_fingerprint


def _minimal_provenance_for_hash() -> Provenance:
    """Build a valid placeholder only for mapping-based fingerprint helpers."""

    return Provenance(
        provider="unknown",
        endpoint_key="unknown",
        target_fingerprint="unknown-target",
        manifest_fingerprint="unknown-manifest",
        request_fingerprint="unknown-request",
        response_sha256="0" * 64,
        fetched_at="1970-01-01T00:00:00Z",
        page=None,
        cursor=None,
        result_ordinal=0,
        target_identity="unknown",
        linkage_evidence={},
    )


__all__ = [
    "ACTOR_ROLES",
    "RECORD_KINDS",
    "SOCIAL_LAB_CONTRACT_VERSION",
    "SOCIAL_LAB_NORMALIZATION_VERSION",
    "ActorRole",
    "ContractError",
    "MalformedInputError",
    "MetricContext",
    "Observation",
    "Provenance",
    "RecordKind",
    "SocialLabContractError",
    "SocialLabMalformedInputError",
    "SocialLabMetricContext",
    "SocialLabObservation",
    "SocialLabProvenance",
    "SocialLabValidationError",
    "SocialMetricContext",
    "SocialObservation",
    "SocialProvenance",
    "ValidationError",
    "build_social_observation",
    "classify_actor_role",
    "content_id",
    "create_observation",
    "fingerprint_metrics",
    "make_social_observation",
    "metric_fingerprint",
    "resolve_actor_role",
    "resolve_role",
    "semantic_fingerprint",
]
