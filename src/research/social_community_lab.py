"""Citation-bound, provider-neutral analysis for the social laboratory.

This module is deliberately a small trust boundary.  It accepts the frozen
``SocialObservation`` contract, gives an injected callable one redacted JSON
packet, and only returns statements whose citations can be checked against the
observations that were admitted to the run.  It does not know about providers,
databases, Scanner, Vault, or the canonical SV9 rubric.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
import json
import re
from typing import Any, Protocol, TypeAlias

from src.research.social_lab_contracts import (
    ActorRole,
    SocialLabContractError,
    SocialLabMalformedInputError,
    SocialLabValidationError,
    SocialObservation,
)


SOCIAL_COMMUNITY_ANALYSIS_VERSION = "b3s-social-community-analysis-v1"
ANALYSIS_SCHEMA_NAME = "b3s_social_community_analysis_v1"
SOCIAL_COMMUNITY_ANALYSIS_SCHEMA_NAME = ANALYSIS_SCHEMA_NAME

DEFAULT_MAX_OBSERVATIONS = 200
DEFAULT_MAX_TOTAL_TEXT_CHARS = 120_000
DEFAULT_MAX_TOKENS = 2_400
DEFAULT_TIMEOUT_SECONDS = 30
MAX_CONCLUSIONS_PER_SECTION = 32
MAX_TILE_CANDIDATES = 32
MAX_CITATIONS_PER_ITEM = 32

ANALYSIS_SECTIONS = (
    "cross_channel_voice",
    "recurring_community_themes",
    "response_behavior",
    "corroborations",
    "tensions",
)
CONCLUSION_SUBJECTS = (
    "brand_claim",
    "brand_behavior",
    "community_perception",
    "tension",
)
CONFIDENCE_LEVELS = ("low", "medium", "high")
TILE_SIGNALS = ("support", "tension", "context")
EVIDENCE_ROLES = (
    "brand_direct",
    "brand_behavior",
    "community_corroboration",
    "mixed_tension",
)
_ADMITTED_ROLES = (
    ActorRole.OFFICIAL_BRAND_POST.value,
    ActorRole.BRAND_REPLY.value,
    ActorRole.COMMUNITY_RESPONSE.value,
)
_SAFE_RECORD_KINDS = {"post", "response", "reply", "observation"}
_OBSERVATION_KEYS = frozenset(
    {
        "platform",
        "record_kind",
        "external_id",
        "canonical_url",
        "author_account_id",
        "author_handle",
        "parent_external_id",
        "thread_external_id",
        "text",
        "media_kind",
        "published_at",
        "actor_role",
        "provenance",
        "metric_context",
        "content_id",
        "semantic_fingerprint",
        "contract_version",
    }
)
_REJECTED_OUTPUT_KEYS = frozenset(
    {
        "score",
        "scores",
        "numeric_score",
        "numeric_scores",
        "component_score",
        "component_scores",
        "canonical_score",
        "sv9_score",
        "state",
        "tile_state",
        "activation",
        "activated",
        "enabled",
        "assessment_state",
        # These are direct identity/acquisition fields, never analyst output.
        "author_account_id",
        "author_handle",
        "canonical_url",
        "external_id",
        "provenance",
        "metric_context",
    }
)


class InvokeStructuredJSON(Protocol):
    """One provider attempt accepted by the analysis core."""

    def __call__(
        self,
        *,
        system: str,
        user: str,
        json_schema: Mapping[str, Any],
        schema_name: str,
        max_tokens: int,
        timeout_seconds: int,
    ) -> object: ...


StructuredJSONCallable: TypeAlias = Callable[..., object]


class SocialCommunityLabError(SocialLabContractError):
    """Base error for the social/community analysis trust boundary."""


class SocialCommunityMalformedInputError(SocialLabMalformedInputError, SocialCommunityLabError):
    """Raised for type-confused or structurally malformed analysis input."""


class SocialCommunityValidationError(SocialLabValidationError, SocialCommunityLabError):
    """Raised when an input or untrusted analyst output violates the domain."""


# Concise aliases are useful to callers that use one common social-lab error
# handler across the B1, A, and C nodes.
AnalysisError = SocialCommunityLabError
MalformedAnalysisError = SocialCommunityMalformedInputError
AnalysisValidationError = SocialCommunityValidationError


def _malformed(message: str) -> SocialCommunityMalformedInputError:
    return SocialCommunityMalformedInputError(message)


def _invalid(message: str) -> SocialCommunityValidationError:
    return SocialCommunityValidationError(message)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise _malformed(f"value is not deterministically JSON serializable: {exc}") from None


def _non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise _malformed(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise _invalid(f"{field_name} must not be empty")
    return normalized


def _strict_sequence(value: Any, field_name: str) -> list[Any]:
    # JSON arrays are lists.  Accepting tuples here would make it too easy for
    # a Python-only provider adapter to bypass the structured-JSON boundary.
    if not isinstance(value, list):
        raise _malformed(f"{field_name} must be an array")
    return value


def _strict_object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _malformed(f"{field_name} must be an object")
    for key in value:
        if not isinstance(key, str):
            raise _malformed(f"{field_name} keys must be strings")
    return value


def _reject_banned_output_keys(value: Any, path: str = "output") -> None:
    """Reject score, identity, and acquisition fields before shape handling."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in _REJECTED_OUTPUT_KEYS:
                raise _invalid(f"{path}.{key} is not permitted in laboratory analysis output")
            _reject_banned_output_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_banned_output_keys(item, f"{path}[{index}]")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # No numeric field is part of this advisory artifact.  Rejecting all
        # numeric output also catches score-like fields hidden under an
        # otherwise acceptable key.
        raise _invalid(f"{path} must not contain numeric values")


def _require_exact_keys(value: Mapping[str, Any], expected: Collection[str], path: str) -> None:
    expected_set = set(expected)
    actual_set = set(value)
    missing = expected_set - actual_set
    extra = actual_set - expected_set
    if missing:
        raise _invalid(f"{path} is missing required keys: {sorted(missing)}")
    if extra:
        raise _invalid(f"{path} contains unsupported keys: {sorted(extra)}")


def _coerce_observation(value: Any, *, index: int) -> SocialObservation:
    if isinstance(value, SocialObservation):
        return value
    if not isinstance(value, Mapping):
        raise _malformed(f"observations[{index}] must be a SocialObservation or object")
    unknown = set(value) - _OBSERVATION_KEYS
    if unknown:
        raise _invalid(f"observations[{index}] contains unsupported keys: {sorted(unknown)}")
    try:
        return SocialObservation.from_dict(value)
    except SocialLabMalformedInputError as exc:
        raise SocialCommunityMalformedInputError(str(exc)) from None
    except SocialLabValidationError as exc:
        raise SocialCommunityValidationError(str(exc)) from None
    except SocialLabContractError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise _malformed(f"invalid observations[{index}]: {exc}") from None


def _coerce_observations(observations: Iterable[SocialObservation | Mapping[str, Any]]) -> list[SocialObservation]:
    if isinstance(observations, (str, bytes, bytearray, Mapping)):
        raise _malformed("observations must be an iterable of SocialObservation values")
    try:
        values = list(observations)
    except (TypeError, ValueError) as exc:
        raise _malformed(f"observations must be iterable: {exc}") from None
    normalized = [_coerce_observation(value, index=index) for index, value in enumerate(values)]
    seen: set[str] = set()
    for index, observation in enumerate(normalized):
        assert observation.content_id is not None
        if observation.content_id in seen:
            raise _invalid(f"observations[{index}] duplicates content_id {observation.content_id!r}")
        seen.add(observation.content_id)
    return normalized


def _safe_record_kind(observation: SocialObservation) -> str:
    record_kind = str(observation.record_kind)
    return record_kind if record_kind in _SAFE_RECORD_KINDS else "observation"


def _linkage_token(observation: SocialObservation) -> str | None:
    parent = observation.parent_external_id
    thread = observation.thread_external_id
    if parent and thread and parent != thread:
        return f"parent:{parent}|thread:{thread}"
    return parent or thread


def build_prompt_packet(
    observations: Iterable[SocialObservation | Mapping[str, Any]],
    *,
    max_observations: int = DEFAULT_MAX_OBSERVATIONS,
    max_total_text_chars: int = DEFAULT_MAX_TOTAL_TEXT_CHARS,
) -> dict[str, list[dict[str, Any]]]:
    """Build the only semantic prompt payload admitted by this core.

    Unclassified observations are intentionally omitted.  The returned object
    is ordinary JSON-compatible data so callers can inspect or hash it without
    importing any provider or persistence layer.
    """

    normalized = _coerce_observations(observations)
    _validate_bounds(normalized, max_observations=max_observations, max_total_text_chars=max_total_text_chars)
    packet: list[dict[str, Any]] = []
    for observation in normalized:
        if observation.actor_role == ActorRole.UNCLASSIFIED.value:
            continue
        packet.append(
            {
                "content_id": observation.content_id,
                "actor_role": observation.actor_role,
                "platform": observation.platform,
                "text": observation.text,
                "published_at": observation.published_at,
                "parent_thread_linkage_token": _linkage_token(observation),
                "record_kind": _safe_record_kind(observation),
            }
        )
    return {"observations": packet}


# Naming aliases used by composition code and tests that prefer an explicit
# social prefix.
build_social_prompt_packet = build_prompt_packet
build_social_analysis_prompt_packet = build_prompt_packet


def build_analysis_prompt(
    observations: Iterable[SocialObservation | Mapping[str, Any]],
    *,
    max_observations: int = DEFAULT_MAX_OBSERVATIONS,
    max_total_text_chars: int = DEFAULT_MAX_TOTAL_TEXT_CHARS,
) -> str:
    """Return the canonical user prompt string for an already bounded input."""

    return _canonical_json(
        build_prompt_packet(
            observations,
            max_observations=max_observations,
            max_total_text_chars=max_total_text_chars,
        )
    )


build_social_analysis_prompt = build_analysis_prompt


def analysis_response_schema(
    *,
    allowed_tile_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Return the tiny Gemini transport envelope; domain validation is local."""

    # Preserve caller validation even though the allowlist belongs to the inner
    # domain contract rather than the provider-facing transport schema.
    _normalize_allowed_tile_ids(allowed_tile_ids)
    return {
        "type": "object",
        "required": ["analysis_json"],
        "properties": {"analysis_json": {"type": "string"}},
    }


social_analysis_response_schema = analysis_response_schema
social_community_analysis_schema = analysis_response_schema


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("analysis_json contains duplicate object keys")
        result[key] = value
    return result


def _reject_non_json_constant(_: str) -> None:
    raise _malformed("analysis_json must contain strict JSON")


def _decode_analysis_envelope(raw: Any) -> Mapping[str, Any]:
    envelope = _strict_object(raw, "analyst_envelope")
    _require_exact_keys(envelope, {"analysis_json"}, "analyst_envelope")
    encoded = _non_empty_text(envelope["analysis_json"], "analyst_envelope.analysis_json")
    try:
        decoded = json.loads(
            encoded,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except json.JSONDecodeError:
        raise _malformed("analysis_json must contain valid JSON") from None
    except RecursionError:
        raise _malformed("analysis_json exceeds the supported nesting depth") from None
    return _strict_object(decoded, "analysis_json")


def _validate_bounds(
    observations: Sequence[SocialObservation],
    *,
    max_observations: int,
    max_total_text_chars: int,
) -> None:
    if isinstance(max_observations, bool) or not isinstance(max_observations, int) or max_observations <= 0:
        raise _malformed("max_observations must be a positive integer")
    if isinstance(max_total_text_chars, bool) or not isinstance(max_total_text_chars, int) or max_total_text_chars <= 0:
        raise _malformed("max_total_text_chars must be a positive integer")
    if len(observations) > max_observations:
        raise _invalid(f"observation bound exceeded: {len(observations)} > {max_observations}")
    total_text_chars = sum(len(observation.text) for observation in observations)
    if total_text_chars > max_total_text_chars:
        raise _invalid(f"text bound exceeded: {total_text_chars} > {max_total_text_chars}")


def _validate_string_list(value: Any, field_name: str, *, max_items: int) -> list[str]:
    values = _strict_sequence(value, field_name)
    if not values:
        raise _invalid(f"{field_name} must not be empty")
    if len(values) > max_items:
        raise _invalid(f"{field_name} exceeds maximum size {max_items}")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        text = _non_empty_text(item, f"{field_name}[{index}]")
        if text in seen:
            raise _invalid(f"{field_name} contains duplicate value {text!r}")
        seen.add(text)
        normalized.append(text)
    return normalized


def _validate_roles(value: Any, field_name: str) -> list[str]:
    roles = _validate_string_list(value, field_name, max_items=len(_ADMITTED_ROLES))
    unknown = set(roles) - set(_ADMITTED_ROLES)
    if unknown:
        raise _invalid(f"{field_name} contains unsupported roles: {sorted(unknown)}")
    return roles


def _validate_citations(
    value: Any,
    *,
    field_name: str,
    observations_by_id: Mapping[str, SocialObservation],
) -> tuple[list[str], set[str]]:
    citations = _validate_string_list(value, field_name, max_items=MAX_CITATIONS_PER_ITEM)
    unknown = set(citations) - set(observations_by_id)
    if unknown:
        raise _invalid(f"{field_name} contains unknown citation IDs: {sorted(unknown)}")
    roles = {observations_by_id[content_id].actor_role for content_id in citations}
    if ActorRole.UNCLASSIFIED.value in roles:
        raise _invalid(f"{field_name} cannot cite unclassified observations")
    return citations, roles


def _validate_conclusion(
    value: Any,
    *,
    path: str,
    observations_by_id: Mapping[str, SocialObservation],
) -> dict[str, Any]:
    item = _strict_object(value, path)
    _require_exact_keys(
        item, {"statement", "subject", "confidence", "citation_content_ids", "derived_source_roles"}, path
    )
    statement = _non_empty_text(item["statement"], f"{path}.statement")
    subject = _non_empty_text(item["subject"], f"{path}.subject")
    if subject not in CONCLUSION_SUBJECTS:
        raise _invalid(f"{path}.subject must be one of {CONCLUSION_SUBJECTS}")
    confidence = _non_empty_text(item["confidence"], f"{path}.confidence")
    if confidence not in CONFIDENCE_LEVELS:
        raise _invalid(f"{path}.confidence must be one of {CONFIDENCE_LEVELS}")
    citations, citation_roles = _validate_citations(
        item["citation_content_ids"],
        field_name=f"{path}.citation_content_ids",
        observations_by_id=observations_by_id,
    )
    source_roles = _validate_roles(item["derived_source_roles"], f"{path}.derived_source_roles")
    if set(source_roles) != citation_roles:
        raise _invalid(f"{path}.derived_source_roles must match the cited observation roles")
    if subject == "brand_claim" and ActorRole.OFFICIAL_BRAND_POST.value not in citation_roles:
        raise _invalid(f"{path}.brand_claim requires an official_brand_post citation")
    if subject == "brand_behavior" and ActorRole.BRAND_REPLY.value not in citation_roles:
        raise _invalid(f"{path}.brand_behavior requires a brand_reply citation")
    if subject == "community_perception" and ActorRole.COMMUNITY_RESPONSE.value not in citation_roles:
        raise _invalid(f"{path}.community_perception requires a community_response citation")
    if subject == "tension" and len(citation_roles) < 2:
        raise _invalid(f"{path}.tension requires citations from at least two source roles")
    _reject_identity_leakage(statement, observations_by_id, path=f"{path}.statement")
    return {
        "statement": statement,
        "subject": subject,
        "confidence": confidence,
        "citation_content_ids": citations,
        "derived_source_roles": source_roles,
    }


def _validate_tile_candidate(
    value: Any,
    *,
    path: str,
    observations_by_id: Mapping[str, SocialObservation],
    allowed_tile_ids: Collection[str] | None,
) -> dict[str, Any]:
    item = _strict_object(value, path)
    _require_exact_keys(
        item,
        {"tile_id", "signal", "rationale", "citation_content_ids", "source_roles", "evidence_role"},
        path,
    )
    if allowed_tile_ids is None:
        raise _invalid("tile candidates require caller-supplied allowed_tile_ids")
    tile_id = _non_empty_text(item["tile_id"], f"{path}.tile_id")
    if tile_id not in allowed_tile_ids:
        raise _invalid(f"{path}.tile_id is not in the caller-supplied allowed_tile_ids")
    signal = _non_empty_text(item["signal"], f"{path}.signal")
    if signal not in TILE_SIGNALS:
        raise _invalid(f"{path}.signal must be one of {TILE_SIGNALS}")
    rationale = _non_empty_text(item["rationale"], f"{path}.rationale")
    citations, citation_roles = _validate_citations(
        item["citation_content_ids"],
        field_name=f"{path}.citation_content_ids",
        observations_by_id=observations_by_id,
    )
    source_roles = _validate_roles(item["source_roles"], f"{path}.source_roles")
    if set(source_roles) != citation_roles:
        raise _invalid(f"{path}.source_roles must match the cited observation roles")
    evidence_role = _non_empty_text(item["evidence_role"], f"{path}.evidence_role")
    if evidence_role not in EVIDENCE_ROLES:
        raise _invalid(f"{path}.evidence_role must be one of {EVIDENCE_ROLES}")
    required_role_by_evidence = {
        "brand_direct": ActorRole.OFFICIAL_BRAND_POST.value,
        "brand_behavior": ActorRole.BRAND_REPLY.value,
        "community_corroboration": ActorRole.COMMUNITY_RESPONSE.value,
    }
    required_role = required_role_by_evidence.get(evidence_role)
    if required_role is not None and required_role not in citation_roles:
        raise _invalid(f"{path}.{evidence_role} requires a {required_role} citation")
    if evidence_role == "mixed_tension" and len(citation_roles) < 2:
        raise _invalid(f"{path}.mixed_tension requires at least two source roles")
    _reject_identity_leakage(rationale, observations_by_id, path=f"{path}.rationale")
    return {
        "tile_id": tile_id,
        "signal": signal,
        "rationale": rationale,
        "citation_content_ids": citations,
        "source_roles": source_roles,
        "evidence_role": evidence_role,
    }


def _reject_identity_leakage(text: str, observations_by_id: Mapping[str, SocialObservation], *, path: str) -> None:
    # Opaque content IDs are explicitly allowed as citations.  Other direct
    # actor/content identifiers must never be copied into semantic prose.
    folded = text.casefold()
    generic_handles = {"brand", "community", "user", "author", "account", "customer", "team", "company", "official"}
    for observation in observations_by_id.values():
        # Handles can be ordinary words (for example ``community``), so only
        # reject their explicit ``@handle`` form here.  Opaque account IDs and
        # URLs are never meaningful semantic prose and can be rejected by an
        # exact substring check.
        identities = [observation.author_account_id, observation.canonical_url]
        if observation.author_handle:
            identities.append(f"@{observation.author_handle}")
            if observation.author_handle.casefold() not in generic_handles:
                handle_pattern = rf"(?<![a-z0-9_]){re.escape(observation.author_handle.casefold())}(?![a-z0-9_])"
                if re.search(handle_pattern, folded):
                    raise _invalid(f"{path} leaks a direct observation identity")
        for identity in identities:
            if identity and identity.casefold() in folded:
                raise _invalid(f"{path} leaks a direct observation identity")


def _validate_model_output(
    raw: Any,
    *,
    observations_by_id: Mapping[str, SocialObservation],
    allowed_tile_ids: Collection[str] | None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    _reject_banned_output_keys(raw)
    output = _strict_object(raw, "analyst_output")
    _require_exact_keys(output, {*ANALYSIS_SECTIONS, "tile_candidates"}, "analyst_output")
    sections: dict[str, list[dict[str, Any]]] = {}
    for section in ANALYSIS_SECTIONS:
        values = _strict_sequence(output[section], f"analyst_output.{section}")
        if len(values) > MAX_CONCLUSIONS_PER_SECTION:
            raise _invalid(f"analyst_output.{section} exceeds maximum size {MAX_CONCLUSIONS_PER_SECTION}")
        sections[section] = [
            _validate_conclusion(
                item,
                path=f"analyst_output.{section}[{index}]",
                observations_by_id=observations_by_id,
            )
            for index, item in enumerate(values)
        ]
    candidates_raw = _strict_sequence(output["tile_candidates"], "analyst_output.tile_candidates")
    if len(candidates_raw) > MAX_TILE_CANDIDATES:
        raise _invalid(f"analyst_output.tile_candidates exceeds maximum size {MAX_TILE_CANDIDATES}")
    candidates = [
        _validate_tile_candidate(
            item,
            path=f"analyst_output.tile_candidates[{index}]",
            observations_by_id=observations_by_id,
            allowed_tile_ids=allowed_tile_ids,
        )
        for index, item in enumerate(candidates_raw)
    ]
    tile_ids = [candidate["tile_id"] for candidate in candidates]
    if len(tile_ids) != len(set(tile_ids)):
        raise _invalid("analyst_output.tile_candidates contains duplicate tile IDs")
    return sections, candidates


class SocialCommunityAnalysisArtifact(dict[str, Any]):
    """JSON-compatible result with convenience aliases for composition code."""

    @property
    def status(self) -> str:
        return str(self["status"])

    @property
    def available(self) -> bool:
        return self.status == "complete"

    @property
    def analysis(self) -> dict[str, list[dict[str, Any]]]:
        return {section: self[section] for section in ANALYSIS_SECTIONS}

    @property
    def sections(self) -> dict[str, list[dict[str, Any]]]:
        return self.analysis

    @property
    def tile_candidates(self) -> list[dict[str, Any]]:
        return self["tile_candidates"]

    def __getitem__(self, key: str) -> Any:
        if key == "analysis":
            return self.analysis
        if key == "sections":
            return self.sections
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in {"analysis", "sections"}:
            return self[key]
        return super().get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return dict(self)


AnalysisArtifact = SocialCommunityAnalysisArtifact
SocialCommunityLabArtifact = SocialCommunityAnalysisArtifact


_ANALYSIS_ARTIFACT_METADATA_KEYS = frozenset(
    {
        "version",
        "status",
        "reason",
        "attempt_count",
        "observation_count",
        "unclassified_observation_count",
        "limitations",
        "prompt_packet",
    }
)
_ANALYSIS_ARTIFACT_STATUSES = frozenset({"complete", "limited", "unavailable", "not_requested"})


def reconstruct_social_community_analysis(
    artifact: Mapping[str, Any],
    observations: Iterable[SocialObservation | Mapping[str, Any]],
    *,
    allowed_tile_ids: Collection[str] | None,
) -> SocialCommunityAnalysisArtifact:
    """Revalidate and reconstruct an analysis artifact at the core boundary.

    A previously produced artifact is still caller-controlled once it crosses a
    composition edge.  This function therefore validates its envelope and
    metadata, re-normalizes the supplied observations, and runs the exact same
    citation/role/allowlist validator used for a callable provider response.
    No metadata or output is trusted by JSON-copying the input mapping.
    """

    value = _strict_object(artifact, "analysis_artifact")
    _require_exact_keys(
        value,
        {*_ANALYSIS_ARTIFACT_METADATA_KEYS, *ANALYSIS_SECTIONS, "tile_candidates"},
        "analysis_artifact",
    )
    normalized = _coerce_observations(observations)
    _validate_bounds(
        normalized,
        max_observations=DEFAULT_MAX_OBSERVATIONS,
        max_total_text_chars=DEFAULT_MAX_TOTAL_TEXT_CHARS,
    )
    unclassified_count = sum(observation.actor_role == ActorRole.UNCLASSIFIED.value for observation in normalized)
    admitted = [observation for observation in normalized if observation.actor_role != ActorRole.UNCLASSIFIED.value]
    observations_by_id = {str(observation.content_id): observation for observation in admitted}
    effective_allowed = _normalize_allowed_tile_ids(allowed_tile_ids)

    version = _non_empty_text(value["version"], "analysis_artifact.version")
    if version != SOCIAL_COMMUNITY_ANALYSIS_VERSION:
        raise _invalid("analysis_artifact.version is unsupported")
    status = _non_empty_text(value["status"], "analysis_artifact.status")
    if status not in _ANALYSIS_ARTIFACT_STATUSES:
        raise _invalid("analysis_artifact.status is unsupported")
    reason_value = value["reason"]
    if not isinstance(reason_value, str):
        raise _malformed("analysis_artifact.reason must be a string")
    reason = reason_value.strip()
    attempt_count = value["attempt_count"]
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in {0, 1}:
        raise _malformed("analysis_artifact.attempt_count must be 0 or 1")
    observation_count = value["observation_count"]
    if isinstance(observation_count, bool) or not isinstance(observation_count, int):
        raise _malformed("analysis_artifact.observation_count must be an integer")
    if observation_count != len(normalized):
        raise _invalid("analysis_artifact.observation_count does not match supplied observations")
    claimed_unclassified_count = value["unclassified_observation_count"]
    if isinstance(claimed_unclassified_count, bool) or not isinstance(claimed_unclassified_count, int):
        raise _malformed("analysis_artifact.unclassified_observation_count must be an integer")
    if claimed_unclassified_count != unclassified_count:
        raise _invalid("analysis_artifact.unclassified_observation_count does not match supplied observations")

    limitations = _strict_sequence(value["limitations"], "analysis_artifact.limitations")
    normalized_limitations: list[str] = []
    for index, limitation in enumerate(limitations):
        normalized_limitations.append(_non_empty_text(limitation, f"analysis_artifact.limitations[{index}]"))

    expected_prompt_packet = build_prompt_packet(
        normalized,
        max_observations=DEFAULT_MAX_OBSERVATIONS,
        max_total_text_chars=DEFAULT_MAX_TOTAL_TEXT_CHARS,
    )
    prompt_packet = value["prompt_packet"]
    if prompt_packet is not None:
        prompt_packet = _strict_object(prompt_packet, "analysis_artifact.prompt_packet")
        _reject_banned_output_keys(prompt_packet, "analysis_artifact.prompt_packet")
        if _canonical_json(prompt_packet) != _canonical_json(expected_prompt_packet):
            raise _invalid("analysis_artifact.prompt_packet does not match supplied observations")

    raw = {section: value[section] for section in ANALYSIS_SECTIONS}
    raw["tile_candidates"] = value["tile_candidates"]
    sections, candidates = _validate_model_output(
        raw,
        observations_by_id=observations_by_id,
        allowed_tile_ids=effective_allowed,
    )

    base_limitations = ["unclassified_observations_excluded"] if unclassified_count else []
    expected_limitations: list[str]
    expected_prompt: Mapping[str, Any] | None
    if status == "complete":
        if not admitted or reason or attempt_count != 1:
            raise _invalid("analysis_artifact complete state is inconsistent with supplied observations")
        expected_limitations = base_limitations
        expected_prompt = expected_prompt_packet
    elif status == "limited":
        if admitted or reason != "no_admitted_observations" or attempt_count != 0:
            raise _invalid("analysis_artifact limited state is inconsistent with supplied observations")
        expected_limitations = [*base_limitations, "no_admitted_observations"]
        expected_prompt = None
    elif status == "unavailable" and reason == "empty_evidence":
        if normalized or attempt_count != 0:
            raise _invalid("analysis_artifact empty-evidence state is inconsistent with supplied observations")
        expected_limitations = ["empty_evidence"]
        expected_prompt = None
    elif status == "unavailable" and reason == "analyst_attempts_exhausted":
        if not normalized or not admitted or attempt_count != 1:
            raise _invalid("analysis_artifact provider-failure state is inconsistent with supplied observations")
        expected_limitations = [*base_limitations, "analyst_attempts_exhausted"]
        expected_prompt = expected_prompt_packet
    elif status == "not_requested":
        if reason != "analysis_not_requested" or attempt_count != 0:
            raise _invalid("analysis_artifact not-requested state is inconsistent")
        expected_limitations = ["analysis_not_requested"]
        expected_prompt = None
    else:
        raise _invalid("analysis_artifact status/reason combination is unsupported")

    if normalized_limitations != expected_limitations:
        raise _invalid("analysis_artifact.limitations are not the canonical state limitations")
    if (prompt_packet is None) != (expected_prompt is None):
        raise _invalid("analysis_artifact.prompt_packet is inconsistent with its state")
    if status != "complete" and (any(sections.values()) or candidates):
        raise _invalid("non-complete analysis_artifact must not carry analysis claims")

    if status == "complete":
        return SocialCommunityAnalysisArtifact(
            {
                "version": SOCIAL_COMMUNITY_ANALYSIS_VERSION,
                "status": "complete",
                "reason": "",
                "attempt_count": 1,
                "observation_count": len(normalized),
                "unclassified_observation_count": unclassified_count,
                "limitations": expected_limitations,
                "prompt_packet": expected_prompt_packet,
                **sections,
                "tile_candidates": candidates,
            }
        )
    return _empty_artifact(
        status=status,
        reason=reason,
        attempt_count=attempt_count,
        observation_count=len(normalized),
        unclassified_observation_count=unclassified_count,
        limitations=expected_limitations,
        prompt_packet=expected_prompt,
    )


def _empty_artifact(
    *,
    status: str,
    reason: str,
    attempt_count: int,
    observation_count: int,
    unclassified_observation_count: int,
    limitations: Sequence[str],
    prompt_packet: Mapping[str, Any] | None = None,
) -> SocialCommunityAnalysisArtifact:
    return SocialCommunityAnalysisArtifact(
        {
            "version": SOCIAL_COMMUNITY_ANALYSIS_VERSION,
            "status": status,
            "reason": reason,
            "attempt_count": attempt_count,
            "observation_count": observation_count,
            "unclassified_observation_count": unclassified_observation_count,
            "limitations": list(limitations),
            "prompt_packet": dict(prompt_packet) if prompt_packet is not None else None,
            **{section: [] for section in ANALYSIS_SECTIONS},
            "tile_candidates": [],
        }
    )


class SocialCommunityAnalyzer:
    """Run one bounded, injected social/community analysis call."""

    def __init__(
        self,
        invoke_structured_json: StructuredJSONCallable | None = None,
        *,
        analyst: StructuredJSONCallable | None = None,
        allowed_tile_ids: Collection[str] | None = None,
        max_observations: int = DEFAULT_MAX_OBSERVATIONS,
        max_total_text_chars: int = DEFAULT_MAX_TOTAL_TEXT_CHARS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if invoke_structured_json is not None and analyst is not None and invoke_structured_json is not analyst:
            raise _invalid("provide only one of invoke_structured_json or analyst")
        callback = invoke_structured_json if invoke_structured_json is not None else analyst
        if callback is None or not callable(callback):
            raise _malformed("an injected invoke_structured_json callable is required")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise _malformed("max_tokens must be a positive integer")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise _malformed("timeout_seconds must be a positive integer")
        normalized_tile_ids = _normalize_allowed_tile_ids(allowed_tile_ids)
        # Validate bounds once at construction as well as at run time, so a
        # long-lived analyzer cannot silently hold an invalid policy.
        _validate_bounds([], max_observations=max_observations, max_total_text_chars=max_total_text_chars)
        self.invoke_structured_json = callback
        self.allowed_tile_ids = normalized_tile_ids
        self.max_observations = max_observations
        self.max_total_text_chars = max_total_text_chars
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    def analyze(
        self,
        observations: Iterable[SocialObservation | Mapping[str, Any]],
        *,
        allowed_tile_ids: Collection[str] | None = None,
    ) -> SocialCommunityAnalysisArtifact:
        normalized = _coerce_observations(observations)
        _validate_bounds(
            normalized,
            max_observations=self.max_observations,
            max_total_text_chars=self.max_total_text_chars,
        )
        unclassified_count = sum(observation.actor_role == ActorRole.UNCLASSIFIED.value for observation in normalized)
        admitted = [observation for observation in normalized if observation.actor_role != ActorRole.UNCLASSIFIED.value]
        limitations: list[str] = []
        if unclassified_count:
            limitations.append("unclassified_observations_excluded")
        if not normalized:
            return _empty_artifact(
                status="unavailable",
                reason="empty_evidence",
                attempt_count=0,
                observation_count=0,
                unclassified_observation_count=0,
                limitations=["empty_evidence"],
            )
        if not admitted:
            return _empty_artifact(
                status="limited",
                reason="no_admitted_observations",
                attempt_count=0,
                observation_count=len(normalized),
                unclassified_observation_count=unclassified_count,
                limitations=[*limitations, "no_admitted_observations"],
            )

        packet = build_prompt_packet(
            normalized,
            max_observations=self.max_observations,
            max_total_text_chars=self.max_total_text_chars,
        )
        user_prompt = _canonical_json(packet)
        observations_by_id = {str(observation.content_id): observation for observation in admitted}
        effective_allowed = (
            self.allowed_tile_ids if allowed_tile_ids is None else _normalize_allowed_tile_ids(allowed_tile_ids)
        )
        system_prompt = _system_prompt(allowed_tile_ids=effective_allowed)
        provider_schema = analysis_response_schema(allowed_tile_ids=effective_allowed)
        try:
            raw = self.invoke_structured_json(
                system=system_prompt,
                user=user_prompt,
                json_schema=provider_schema,
                schema_name=ANALYSIS_SCHEMA_NAME,
                max_tokens=self.max_tokens,
                timeout_seconds=self.timeout_seconds,
            )
        except SocialCommunityValidationError:
            raise
        except Exception:  # provider adapters own their diagnostics; keep this envelope safe
            # Provider failures are terminal for this invocation.  The analysis
            # core has no retry or second-attempt configuration surface.
            return _empty_artifact(
                status="unavailable",
                reason="analyst_attempts_exhausted",
                attempt_count=1,
                observation_count=len(normalized),
                unclassified_observation_count=unclassified_count,
                limitations=[*limitations, "analyst_attempts_exhausted"],
                prompt_packet=packet,
            )

        decoded = _decode_analysis_envelope(raw)
        sections, candidates = _validate_model_output(
            decoded,
            observations_by_id=observations_by_id,
            allowed_tile_ids=effective_allowed,
        )
        return SocialCommunityAnalysisArtifact(
            {
                "version": SOCIAL_COMMUNITY_ANALYSIS_VERSION,
                "status": "complete",
                "reason": "",
                "attempt_count": 1,
                "observation_count": len(normalized),
                "unclassified_observation_count": unclassified_count,
                "limitations": limitations,
                "prompt_packet": packet,
                **sections,
                "tile_candidates": candidates,
            }
        )

    run = analyze

    def __call__(
        self,
        observations: Iterable[SocialObservation | Mapping[str, Any]],
        *,
        allowed_tile_ids: Collection[str] | None = None,
    ) -> SocialCommunityAnalysisArtifact:
        return self.analyze(observations, allowed_tile_ids=allowed_tile_ids)


SocialCommunityLab = SocialCommunityAnalyzer
SocialCommunityAnalysisCore = SocialCommunityAnalyzer
SocialCommunityLabAnalyzer = SocialCommunityAnalyzer


def _normalize_allowed_tile_ids(value: Collection[str] | None) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise _malformed("allowed_tile_ids must be a collection of strings")
    try:
        values = list(value)
    except (TypeError, ValueError) as exc:
        raise _malformed(f"allowed_tile_ids must be a collection: {exc}") from None
    normalized: set[str] = set()
    for index, item in enumerate(values):
        normalized.add(_non_empty_text(item, f"allowed_tile_ids[{index}]"))
    return frozenset(normalized)


def analyze_social_community(
    observations: Iterable[SocialObservation | Mapping[str, Any]],
    invoke_structured_json: StructuredJSONCallable | None = None,
    *,
    analyst: StructuredJSONCallable | None = None,
    allowed_tile_ids: Collection[str] | None = None,
    max_observations: int = DEFAULT_MAX_OBSERVATIONS,
    max_total_text_chars: int = DEFAULT_MAX_TOTAL_TEXT_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> SocialCommunityAnalysisArtifact:
    """Convenience entry point for one isolated analysis run."""

    analyzer = SocialCommunityAnalyzer(
        invoke_structured_json,
        analyst=analyst,
        allowed_tile_ids=allowed_tile_ids,
        max_observations=max_observations,
        max_total_text_chars=max_total_text_chars,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
    )
    return analyzer.analyze(observations)


run_social_community_analysis = analyze_social_community
analyze_social_lab = analyze_social_community


def _system_prompt(*, allowed_tile_ids: Collection[str] | None = None) -> str:
    normalized_allowed_tile_ids = _normalize_allowed_tile_ids(allowed_tile_ids) or frozenset()
    allowed_tile_ids_json = _canonical_json(sorted(normalized_allowed_tile_ids))
    section_names_json = _canonical_json(list(ANALYSIS_SECTIONS))
    subjects_json = _canonical_json(list(CONCLUSION_SUBJECTS))
    confidence_json = _canonical_json(list(CONFIDENCE_LEVELS))
    roles_json = _canonical_json(list(_ADMITTED_ROLES))
    signals_json = _canonical_json(list(TILE_SIGNALS))
    evidence_roles_json = _canonical_json(list(EVIDENCE_ROLES))
    return (
        "You are the advisory analyst for an isolated social/community laboratory. "
        "Return exactly one outer JSON object with exactly one key, analysis_json. "
        "analysis_json must be a JSON-encoded string whose decoded value is the inner analysis object. "
        f"The inner object must have exactly these section keys plus tile_candidates: {section_names_json}. "
        f"Each section is an array of at most {MAX_CONCLUSIONS_PER_SECTION} conclusion objects. "
        "Each conclusion has exactly statement, subject, confidence, citation_content_ids, and "
        "derived_source_roles. statement is non-empty text. "
        f"subject is one of {subjects_json}; confidence is one of {confidence_json}. "
        f"citation_content_ids contains 1 to {MAX_CITATIONS_PER_ITEM} unique supplied content IDs. "
        f"derived_source_roles contains unique values from {roles_json} and must exactly match cited roles. "
        "brand_claim requires official_brand_post evidence; brand_behavior requires brand_reply evidence; "
        "community_perception requires community_response evidence; tension requires at least two source roles. "
        f"tile_candidates is an array of at most {MAX_TILE_CANDIDATES} objects with unique tile_id values. "
        "Each tile candidate has exactly tile_id, signal, rationale, citation_content_ids, source_roles, and "
        "evidence_role. rationale is non-empty text; citations and source_roles follow the same rules as conclusions. "
        f"signal is one of {signals_json}; evidence_role is one of {evidence_roles_json}. "
        "brand_direct requires official_brand_post evidence; brand_behavior requires brand_reply evidence; "
        "community_corroboration requires community_response evidence; mixed_tension requires at least two roles. "
        f"Allowed tile IDs JSON: {allowed_tile_ids_json}. "
        "When that list is empty, tile_candidates must be empty. "
        "Use only the supplied observation content and citations; do not invent evidence. "
        "Community speech is perception evidence, not a brand-owned claim. "
        "Do not emit scores, states, activation decisions, direct actor identity, metrics, or provenance."
    )


build_system_prompt = _system_prompt
system_prompt = _system_prompt


__all__ = [
    "ANALYSIS_SCHEMA_NAME",
    "SOCIAL_COMMUNITY_ANALYSIS_SCHEMA_NAME",
    "ANALYSIS_SECTIONS",
    "AnalysisArtifact",
    "AnalysisError",
    "AnalysisValidationError",
    "CONFIDENCE_LEVELS",
    "CONCLUSION_SUBJECTS",
    "DEFAULT_MAX_OBSERVATIONS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MAX_TOTAL_TEXT_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "EVIDENCE_ROLES",
    "MalformedAnalysisError",
    "SocialCommunityAnalysisArtifact",
    "SocialCommunityAnalysisCore",
    "SocialCommunityAnalyzer",
    "SocialCommunityLab",
    "SocialCommunityLabAnalyzer",
    "SocialCommunityLabError",
    "SocialCommunityMalformedInputError",
    "SocialCommunityValidationError",
    "InvokeStructuredJSON",
    "StructuredJSONCallable",
    "TILE_SIGNALS",
    "analysis_response_schema",
    "analyze_social_community",
    "analyze_social_lab",
    "build_analysis_prompt",
    "build_prompt_packet",
    "build_social_analysis_prompt",
    "build_social_analysis_prompt_packet",
    "build_social_prompt_packet",
    "build_system_prompt",
    "reconstruct_social_community_analysis",
    "run_social_community_analysis",
    "social_analysis_response_schema",
    "social_community_analysis_schema",
    "system_prompt",
]
