#!/usr/bin/env python3
"""Run the isolated social/community laboratory.

The runner is intentionally a composition edge.  Manifest validation and
provider acquisition stay in the accepted ScrapeCreators spike, while
semantic analysis stays in the citation-bound social laboratory core.  This
module only joins those contracts and writes a private, file-only artifact.

It never imports Scanner, Evidence Vault, SV9, or a persistence service.
"""

from __future__ import annotations

import argparse
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

from src.research.scrapecreators_spike import (
    API_KEY_ENV,
    CONTRACT_OUTPUT_SCHEMA_VERSION,
    ScrapeCreatorsClient,
    ScrapeCreatorsSpikeError,
    TargetManifest,
    atomic_write_json as _atomic_write_json,
    load_target_manifest,
    plan_requests,
    run_acquisition,
)
from src.research.social_community_lab import (
    ANALYSIS_SECTIONS,
    SOCIAL_COMMUNITY_ANALYSIS_VERSION,
    SocialCommunityAnalyzer,
    SocialCommunityLabError,
    build_prompt_packet,
    reconstruct_social_community_analysis,
)
from src.research.social_lab_contracts import (
    ACTOR_ROLES,
    SOCIAL_LAB_CONTRACT_VERSION,
    SOCIAL_LAB_NORMALIZATION_VERSION,
    SocialLabContractError,
    SocialObservation,
)


ARTIFACT_SCHEMA_VERSION = CONTRACT_OUTPUT_SCHEMA_VERSION
RUNNER_VERSION = "social-community-lab-runner-v1"
DEFAULT_OUTPUT_PATH = Path("out/social-community-lab/run.json")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPOSITORY_ROOT / "out" / "social-community-lab"
_ROLE_ORDER = (
    "official_brand_post",
    "community_response",
    "brand_reply",
    "unclassified",
)
_CAPABILITY_STATUSES = frozenset({"acquired", "partial", "not_acquired", "failed"})
_UNSUPPORTED_X_INTERACTION_ROLES = frozenset({"community_response", "brand_reply"})
_X_PLATFORMS = frozenset({"twitter", "x"})
_PROMOTION_GATE_DEFINITIONS = (
    (
        "role_attribution_accuracy",
        "Validate actor-role assignment against independent adjudication.",
    ),
    (
        "representative_interaction_coverage",
        "Show representative interaction coverage across supported channels.",
    ),
    (
        "citation_traceability",
        "Trace every analysis conclusion to admitted observation citations.",
    ),
    (
        "semantic_id_stability",
        "Confirm equivalent observations retain stable semantic IDs.",
    ),
    (
        "metric_invariance",
        "Change metrics without changing semantic identity.",
    ),
    (
        "community_claim_boundary",
        "Confirm community speech is not treated as a brand-owned claim.",
    ),
    (
        "strategist_incremental_value",
        "Demonstrate analyst output adds value beyond existing evidence.",
    ),
    (
        "negative_control_precision",
        "Measure false-positive behavior on negative controls.",
    ),
    (
        "cost_latency_rate_limits",
        "Measure bounded cost, latency, retries, and rate-limit behavior.",
    ),
    (
        "privacy_retention_terms",
        "Approve privacy, retention, and provider-terms handling.",
    ),
    (
        "canonical_invariance",
        "Prove canonical state and persistence remain unchanged.",
    ),
)
_SECRET_ENV_NAMES = (
    API_KEY_ENV,
    "BRAND3_LLM_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
)
_SENSITIVE_KEYS = frozenset(
    {
        "x-api-key",
        "api-key",
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        # Replay inputs can come from a previous environment, so redact
        # generic credential-bearing keys even when their values are unknown
        # to the current process.
        "token",
        "secret",
        "client_secret",
        "password",
        "bearer",
        "cookie",
        "set-cookie",
    }
)


class SocialCommunityLabCLIError(RuntimeError):
    """Safe, user-facing runner error."""


class LiveAnalysisUnavailable(SocialCommunityLabCLIError):
    """The requested one-call Gemini-native path cannot be proven."""


def _validate_output_path(path: str | Path) -> Path:
    """Return a destination confined to the ignored social-lab output root."""

    candidate = Path(path)
    if any(part == ".." for part in candidate.parts):
        raise SocialCommunityLabCLIError("output path traversal is not allowed")
    absolute_candidate = candidate if candidate.is_absolute() else REPOSITORY_ROOT / candidate
    expected_root = REPOSITORY_ROOT / "out" / "social-community-lab"
    if OUTPUT_ROOT.absolute() == expected_root.absolute():
        cursor = REPOSITORY_ROOT
        for part in expected_root.relative_to(REPOSITORY_ROOT).parts:
            cursor /= part
            if cursor.is_symlink():
                raise SocialCommunityLabCLIError("output root must not escape through a symlink")
    try:
        resolved_root = OUTPUT_ROOT.resolve(strict=False)
        resolved_candidate = absolute_candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SocialCommunityLabCLIError("output path cannot be resolved safely") from exc
    try:
        relative = resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise SocialCommunityLabCLIError("output must remain beneath out/social-community-lab") from exc
    if not relative.parts:
        raise SocialCommunityLabCLIError("output must name a file beneath out/social-community-lab")
    return absolute_candidate


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Write one private artifact after validating the lab-only destination."""

    return _atomic_write_json(_validate_output_path(path), payload)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _json_copy(value: Any) -> Any:
    """Return JSON-shaped data without retaining caller-owned mutable objects."""

    try:
        return json.loads(_canonical_json(value))
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise SocialCommunityLabCLIError("value is not JSON-compatible") from exc


def _secret_values() -> tuple[str, ...]:
    values = {value.strip() for name in _SECRET_ENV_NAMES if (value := os.environ.get(name, "")).strip()}
    return tuple(sorted(values, key=len, reverse=True))


def _redact_text(value: str, secrets: Collection[str] | None = None) -> str:
    result = str(value)
    for secret in secrets if secrets is not None else _secret_values():
        result = result.replace(secret, "[REDACTED]")
    return result


def _redact_value(value: Any, *, secrets: Collection[str] | None = None, key: str | None = None) -> Any:
    """Redact known credentials in replayed/provider data before serialization."""

    normalized_key = key.casefold() if isinstance(key, str) else ""
    if normalized_key in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(item, secrets=secrets, key=str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


def _normalize_tile_ids(value: Collection[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise SocialCommunityLabCLIError("allowed tile IDs must be a JSON array of strings")
    try:
        values = list(value)
    except (TypeError, ValueError) as exc:
        raise SocialCommunityLabCLIError("allowed tile IDs must be a JSON array of strings") from exc
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise SocialCommunityLabCLIError("allowed tile IDs must be a JSON array of non-empty strings")
        item = item.strip()
        if item in seen:
            raise SocialCommunityLabCLIError(f"allowed tile IDs contain duplicate value {item!r}")
        seen.add(item)
        normalized.append(item)
    return sorted(normalized)


def _load_json_argument(value: str, *, label: str) -> tuple[Any, str]:
    """Load inline JSON or a local file and return value plus exact input hash."""

    source = value[1:] if value.startswith("@") else value
    path: Path | None = None
    try:
        candidate = Path(source)
        if candidate.is_file():
            path = candidate
    except (OSError, ValueError):
        path = None
    if path is not None:
        try:
            raw_bytes = path.read_bytes()
        except (OSError, UnicodeError) as exc:
            raise SocialCommunityLabCLIError(f"{label} could not be read") from exc
    else:
        raw_bytes = source.encode("utf-8")
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SocialCommunityLabCLIError(f"{label} is not valid JSON") from exc
    return parsed, _sha256_bytes(raw_bytes)


def _load_json_array_argument(value: str, *, label: str) -> list[str]:
    parsed, _ = _load_json_argument(value, label=label)
    return _normalize_tile_ids(parsed) or []


def _read_acquisition_result(path: str) -> Mapping[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SocialCommunityLabCLIError("acquisition result does not exist") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SocialCommunityLabCLIError("acquisition result is not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise SocialCommunityLabCLIError("acquisition result must be a JSON object")
    return raw


def _empty_capability_matrix(manifest: TargetManifest, *, reason: str) -> dict[str, dict[str, dict[str, Any]]]:
    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    for target in manifest.targets:
        matrix[target.target_id] = {
            role: {
                "target_id": target.target_id,
                "platform": target.platform,
                "role": role,
                "status": "not_acquired",
                "reason": reason,
                "provenance": [],
            }
            for role in _ROLE_ORDER
        }
        if target.platform.casefold() in _X_PLATFORMS:
            for role in _UNSUPPORTED_X_INTERACTION_ROLES:
                matrix[target.target_id][role]["reason"] = "no_verified_reply_tree_endpoint"
    return matrix


def _coerce_observations(acquisition: Mapping[str, Any]) -> list[SocialObservation]:
    rows = acquisition.get("observations")
    candidates: list[Any] = list(rows) if isinstance(rows, list) else []
    if not candidates:
        for collection_name in ("profiles", "posts", "interactions"):
            collection = acquisition.get(collection_name)
            if not isinstance(collection, list):
                continue
            for row in collection:
                if isinstance(row, Mapping) and isinstance(row.get("observation"), Mapping):
                    candidates.append(row["observation"])

    observations: list[SocialObservation] = []
    seen: set[str] = set()
    for index, value in enumerate(candidates):
        if isinstance(value, Mapping) and isinstance(value.get("observation"), Mapping):
            value = value["observation"]
        try:
            observation = value if isinstance(value, SocialObservation) else SocialObservation.from_dict(value)
        except (SocialLabContractError, TypeError, ValueError, KeyError) as exc:
            raise SocialCommunityLabCLIError(f"acquisition observation {index} is invalid") from exc
        if observation.content_id in seen:
            raise SocialCommunityLabCLIError("acquisition contains duplicate observation content IDs")
        seen.add(str(observation.content_id))
        observations.append(observation)
    return observations


def _observation_target_map(
    manifest: TargetManifest,
    acquisition: Mapping[str, Any],
    observations: Sequence[SocialObservation],
) -> dict[str, str]:
    """Recover A-layer target bindings without adding them to B1 semantics."""

    by_content_id: dict[str, str] = {}
    for collection_name in ("observations", "profiles", "posts", "interactions"):
        collection = acquisition.get(collection_name)
        if not isinstance(collection, list):
            continue
        for row in collection:
            if not isinstance(row, Mapping):
                continue
            target_id = row.get("target_id")
            nested = row.get("observation") if isinstance(row.get("observation"), Mapping) else row
            content_id = nested.get("content_id") if isinstance(nested, Mapping) else None
            if isinstance(target_id, str) and isinstance(content_id, str):
                by_content_id[content_id] = target_id
    if len(manifest.targets) == 1:
        only_target = manifest.targets[0].target_id
        for observation in observations:
            by_content_id.setdefault(str(observation.content_id), only_target)
    return by_content_id


def _matrix_from_acquisition(
    manifest: TargetManifest,
    acquisition: Mapping[str, Any],
    observations: Sequence[SocialObservation],
    target_map: Mapping[str, str] | None = None,
    secrets: Collection[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    raw_matrix = acquisition.get("acquisition_matrix") or acquisition.get("capability_matrix")
    if isinstance(raw_matrix, Mapping):
        matrix = _redact_value(raw_matrix, secrets=secrets)
    else:
        matrix = _empty_capability_matrix(manifest, reason="acquisition_not_attempted")

    by_target = {target.target_id: target for target in manifest.targets}
    counts: dict[str, dict[str, int]] = {
        target.target_id: {role: 0 for role in _ROLE_ORDER} for target in manifest.targets
    }
    for observation in observations:
        target_id = (target_map or {}).get(str(observation.content_id))
        linkage = observation.provenance.linkage_evidence
        if isinstance(linkage, Mapping):
            candidate = linkage.get("target_id")
            if isinstance(candidate, str):
                target_id = candidate
        # Provider rows carry target_id outside the B1 contract.  A matrix
        # entry remains the authoritative target binding when it is present.
        if target_id not in by_target:
            target_id = next(
                (
                    candidate_id
                    for candidate_id, entries in matrix.items()
                    if isinstance(entries, Mapping)
                    and any(
                        isinstance(entry, Mapping)
                        and entry.get("provenance")
                        and any(
                            isinstance(proof, Mapping) and proof.get("target_id") == candidate_id
                            for proof in entry.get("provenance", [])
                        )
                        for entry in entries.values()
                    )
                ),
                None,
            )
        # Most accepted A observations are associated with a target in their
        # provenance linkage.  If the replay omitted that non-semantic hint,
        # use the only target when unambiguous.
        if target_id not in by_target and len(by_target) == 1:
            target_id = next(iter(by_target))
        if target_id in by_target and observation.actor_role in _ROLE_ORDER:
            counts[target_id][observation.actor_role] += 1

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for target in manifest.targets:
        entries = matrix.get(target.target_id)
        if not isinstance(entries, Mapping):
            entries = {}
        result[target.target_id] = {}
        for role in _ROLE_ORDER:
            if target.platform.casefold() in _X_PLATFORMS and role in _UNSUPPORTED_X_INTERACTION_ROLES:
                # Replayed/provider capability matrices are untrusted.  X has
                # no verified reply-tree endpoint, so reconstruct these
                # interaction capabilities instead of preserving claimed
                # status, reason, or provenance.
                result[target.target_id][role] = {
                    "target_id": target.target_id,
                    "platform": target.platform,
                    "role": role,
                    "status": "not_acquired",
                    "reason": "no_verified_reply_tree_endpoint",
                    "provenance": [],
                }
                continue
            entry = entries.get(role)
            if isinstance(entry, Mapping):
                status = str(entry.get("status", "not_acquired"))
                if status not in _CAPABILITY_STATUSES:
                    status = "partial"
                result[target.target_id][role] = {
                    "target_id": target.target_id,
                    "platform": target.platform,
                    "role": role,
                    "status": status,
                    "reason": _redact_text(str(entry.get("reason", "")), secrets),
                    "provenance": _redact_value(entry.get("provenance", []), secrets=secrets),
                }
            else:
                result[target.target_id][role] = {
                    "target_id": target.target_id,
                    "platform": target.platform,
                    "role": role,
                    "status": "acquired" if counts[target.target_id][role] else "not_acquired",
                    "reason": "admitted_observation" if counts[target.target_id][role] else "acquisition_not_attempted",
                    "provenance": [],
                }
    return result


def _aggregate_status(statuses: Sequence[str]) -> str:
    if not statuses or all(status == "not_acquired" for status in statuses):
        return "not_acquired"
    if all(status == "acquired" for status in statuses):
        return "acquired"
    if any(status in {"acquired", "partial"} for status in statuses):
        return "partial"
    if any(status == "failed" for status in statuses):
        return "failed"
    return "partial"


def _role_coverage(
    matrix: Mapping[str, Mapping[str, Mapping[str, Any]]],
    observations: Sequence[SocialObservation],
    target_map: Mapping[str, str] | None = None,
    secrets: Collection[str] | None = None,
) -> dict[str, dict[str, Any]]:
    counts = {role: 0 for role in _ROLE_ORDER}
    for observation in observations:
        role = observation.actor_role
        if role in counts:
            counts[role] += 1
    result: dict[str, dict[str, Any]] = {}
    for role in _ROLE_ORDER:
        by_target: dict[str, dict[str, Any]] = {}
        statuses: list[str] = []
        for target_id, entries in matrix.items():
            entry = entries.get(role, {}) if isinstance(entries, Mapping) else {}
            status = str(entry.get("status", "not_acquired"))
            if status not in _CAPABILITY_STATUSES:
                status = "partial"
            statuses.append(status)
            by_target[str(target_id)] = {
                "count": sum(
                    1
                    for observation in observations
                    if observation.actor_role == role
                    and (target_map or {}).get(str(observation.content_id)) == target_id
                ),
                "status": status,
                "reason": _redact_text(str(entry.get("reason", "")), secrets),
            }
        status = _aggregate_status(statuses)
        result[role] = {
            "count": counts[role],
            "observation_count": counts[role],
            "status": status,
            "acquisition_status": status,
            "target_count": sum(1 for item in by_target.values() if item["count"]),
            "by_target": by_target,
        }
    return result


def _observations_by_role(observations: Sequence[SocialObservation]) -> dict[str, list[dict[str, Any]]]:
    result = {role: [] for role in _ROLE_ORDER}
    for observation in observations:
        row = observation.to_dict()
        # Metrics are deliberately kept in the separate metric_context
        # section.  Provenance remains here so every citation can be audited.
        row.pop("metric_context", None)
        result.setdefault(observation.actor_role, []).append(row)
    return result


def _metric_context(observations: Sequence[SocialObservation]) -> dict[str, Any]:
    return {
        str(observation.content_id): observation.metric_context.to_dict()
        for observation in observations
        if observation.metric_context is not None
    }


def _provider_responses(
    acquisition: Mapping[str, Any], *, include_raw: bool, secrets: Collection[str] | None = None
) -> list[dict[str, Any]]:
    raw_rows = acquisition.get("responses")
    if not isinstance(raw_rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, Mapping):
            continue
        clean = dict(row)
        if not include_raw:
            clean.pop("raw_payload_redacted", None)
        result.append(_redact_value(clean, secrets=secrets))
    return result


def _analysis_not_requested(observation_count: int, unclassified_count: int) -> dict[str, Any]:
    return {
        "version": SOCIAL_COMMUNITY_ANALYSIS_VERSION,
        "status": "not_requested",
        "reason": "analysis_not_requested",
        "attempt_count": 0,
        "observation_count": observation_count,
        "unclassified_observation_count": unclassified_count,
        "limitations": ["analysis_not_requested"],
        "prompt_packet": None,
        **{section: [] for section in ANALYSIS_SECTIONS},
        "tile_candidates": [],
    }


def _analysis_attempts_exhausted(observations: Sequence[SocialObservation]) -> dict[str, Any]:
    """Build the canonical fail-closed analysis state after one attempted call."""

    unclassified_count = sum(observation.actor_role == "unclassified" for observation in observations)
    limitations = ["unclassified_observations_excluded"] if unclassified_count else []
    limitations.append("analyst_attempts_exhausted")
    return {
        "version": SOCIAL_COMMUNITY_ANALYSIS_VERSION,
        "status": "unavailable",
        "reason": "analyst_attempts_exhausted",
        "attempt_count": 1,
        "observation_count": len(observations),
        "unclassified_observation_count": unclassified_count,
        "limitations": limitations,
        "prompt_packet": build_prompt_packet(observations),
        **{section: [] for section in ANALYSIS_SECTIONS},
        "tile_candidates": [],
    }


def _analysis_preflight_failed(observations: Sequence[SocialObservation]) -> dict[str, Any]:
    """Build a fail-closed state for rejection before the provider boundary."""

    unclassified_count = sum(observation.actor_role == "unclassified" for observation in observations)
    limitations = ["unclassified_observations_excluded"] if unclassified_count else []
    limitations.append("analysis_preflight_failed")
    return {
        "version": SOCIAL_COMMUNITY_ANALYSIS_VERSION,
        "status": "unavailable",
        "reason": "analysis_preflight_failed",
        "attempt_count": 0,
        "observation_count": len(observations),
        "unclassified_observation_count": unclassified_count,
        "limitations": limitations,
        "prompt_packet": None,
        **{section: [] for section in ANALYSIS_SECTIONS},
        "tile_candidates": [],
    }


def _analysis_failure_metadata(reason: str, *, attempt_count: int) -> dict[str, Any]:
    """Return the runner-owned, non-claiming failure envelope."""

    allowed_reasons = {
        "gemini_domain_validation_failed",
        "gemini_provider_failure",
        "gemini_unavailable",
    }
    if reason not in allowed_reasons:
        raise SocialCommunityLabCLIError("unsupported live analysis failure reason")
    if attempt_count not in {0, 1}:
        raise SocialCommunityLabCLIError("unsupported live analysis attempt count")
    return {
        "status": "failed",
        "reason": reason,
        "attempt_count": attempt_count,
        "claims_available": False,
    }


def _promotion_evidence_checklist() -> list[dict[str, Any]]:
    """Return the immutable evidence slots that the independent E gate owns."""

    return [
        {
            "id": gate_id,
            "status": "not_checked",
            "evidence": [],
            "requirement": requirement,
        }
        for gate_id, requirement in _PROMOTION_GATE_DEFINITIONS
    ]


def _make_run_identity(
    manifest: TargetManifest,
    *,
    mode: str,
    acquisition_options: Mapping[str, Any],
    analysis_options: Mapping[str, Any],
    analyst_response_hash: str | None,
    acquisition_contract_version: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "manifest_fingerprint": manifest.sha256,
        "acquisition_options": _json_copy(acquisition_options),
        "analysis_options": _json_copy(analysis_options),
        "contract_versions": {
            "artifact": ARTIFACT_SCHEMA_VERSION,
            "observation": SOCIAL_LAB_CONTRACT_VERSION,
            "observation_normalization": SOCIAL_LAB_NORMALIZATION_VERSION,
            "analysis": SOCIAL_COMMUNITY_ANALYSIS_VERSION,
            "acquisition": acquisition_contract_version or ARTIFACT_SCHEMA_VERSION,
        },
        "mode": mode,
    }
    if analyst_response_hash is not None:
        payload["analyst_response_sha256"] = analyst_response_hash
    payload["run_id"] = _sha256_json(payload)
    return payload


def compose_lab_artifact(
    manifest: TargetManifest,
    acquisition: Mapping[str, Any],
    *,
    mode: str = "acquisition_only",
    allowed_tile_ids: Collection[str] | None = None,
    analyst_response: Any | None = None,
    analyst_response_hash: str | None = None,
    analysis_artifact: Mapping[str, Any] | None = None,
    acquisition_options: Mapping[str, Any] | None = None,
    analysis_options: Mapping[str, Any] | None = None,
    include_raw: bool | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compose one private artifact from accepted acquisition and analysis data.

    ``output_path`` is accepted for callers that build an artifact before
    writing, but is intentionally ignored by the run identity.
    """

    del output_path
    normalized_tile_ids = _normalize_tile_ids(allowed_tile_ids)
    # Dry-run must not read credential environment variables at all.  The
    # provider path is never instantiated and no secret-bearing data exists in
    # this plan, so redaction is unnecessary there.
    secrets = () if mode == "dry_run" else _secret_values()
    observations = _coerce_observations(acquisition)
    target_map = _observation_target_map(manifest, acquisition, observations)
    matrix = _matrix_from_acquisition(manifest, acquisition, observations, target_map, secrets)
    coverage = _role_coverage(matrix, observations, target_map, secrets)
    unclassified_count = coverage["unclassified"]["count"]
    if include_raw is None:
        response_rows = acquisition.get("responses")
        include_raw = any(
            isinstance(row, Mapping) and "raw_payload_redacted" in row
            for row in (response_rows if isinstance(response_rows, list) else [])
        )

    acquisition_options = dict(acquisition_options or {})
    analysis_options = dict(analysis_options or {})
    if "max_attempts" in analysis_options:
        raise SocialCommunityLabCLIError("max_attempts is not supported; social analysis is one-shot")
    analysis_options.setdefault("allowed_tile_ids", normalized_tile_ids)
    attempt_policy = analysis_options.setdefault("attempt_policy", "one_shot")
    if attempt_policy != "one_shot":
        raise SocialCommunityLabCLIError("social analysis attempt_policy must be one_shot")
    run_identity = _make_run_identity(
        manifest,
        mode=mode,
        acquisition_options=acquisition_options,
        analysis_options=analysis_options,
        analyst_response_hash=analyst_response_hash,
        acquisition_contract_version=(
            str(acquisition.get("contract_version")) if acquisition.get("contract_version") else None
        ),
    )

    if analysis_artifact is not None and analyst_response is not None:
        raise SocialCommunityLabCLIError("provide one of analyst_response or analysis_artifact")
    if analysis_artifact is not None:
        try:
            community_analysis = reconstruct_social_community_analysis(
                analysis_artifact,
                observations,
                allowed_tile_ids=normalized_tile_ids,
            )
        except SocialCommunityLabError as exc:
            raise SocialCommunityLabCLIError("prebuilt analyst artifact failed domain validation") from exc
        community_analysis = dict(community_analysis)
    elif analyst_response is None:
        community_analysis = _analysis_not_requested(len(observations), unclassified_count)
    else:
        try:
            analyzer = SocialCommunityAnalyzer(
                lambda **_kwargs: analyst_response,
                allowed_tile_ids=normalized_tile_ids,
            )
            community_analysis = analyzer.analyze(observations)
        except SocialCommunityLabError as exc:
            raise SocialCommunityLabCLIError("recorded analyst response failed domain validation") from exc
        community_analysis = dict(community_analysis)

    candidates = community_analysis.get("tile_candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    if normalized_tile_ids is not None:
        candidates = [candidate for candidate in candidates if candidate.get("tile_id") in normalized_tile_ids]
    else:
        candidates = []

    limitations: list[str] = ["laboratory_only_no_canonical_scoring_or_persistence"]
    if mode == "dry_run":
        limitations.append("dry_run_no_network_or_provider_calls")
    if analyst_response is None and analysis_artifact is None and mode != "dry_run":
        limitations.append("analysis_not_requested")
    if unclassified_count:
        limitations.append("unclassified_observations_excluded_from_analysis")
    for target_id, entries in matrix.items():
        for role, entry in entries.items():
            status = str(entry.get("status"))
            if status in {"not_acquired", "partial", "failed"}:
                reason = str(entry.get("reason") or "unspecified")
                limitation = f"{target_id}:{role}:{status}:{reason}"
                if limitation not in limitations:
                    limitations.append(limitation)

    artifact: dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "version": ARTIFACT_SCHEMA_VERSION,
        "artifact_type": ARTIFACT_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "mode": mode,
        "privacy": "private",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "run_identity": _redact_value(run_identity, secrets=secrets),
        "acquisition_matrix": _redact_value(matrix, secrets=secrets),
        "role_coverage": _redact_value(coverage, secrets=secrets),
        "observations_by_role": _redact_value(_observations_by_role(observations), secrets=secrets),
        "community_analysis": _redact_value(community_analysis, secrets=secrets),
        "advisory_tile_candidates": _redact_value(candidates, secrets=secrets),
        "metric_context": _redact_value(_metric_context(observations), secrets=secrets),
        "promotion_evidence": {
            "status": "insufficient",
            "reason": "runner_cannot_promote_social_evidence",
            "gates": _promotion_evidence_checklist(),
        },
        "limitations": _redact_value(limitations, secrets=secrets),
        "canonical_invariance": {
            "status": "not_checked",
            "reason": "independent_canonical_invariance_gate_required",
        },
        # Response metadata is safe to retain; raw payloads are included only
        # when the explicit A-layer opt-in was supplied and remain redacted.
        "acquisition_summary": _redact_value(acquisition.get("summary", {}), secrets=secrets),
        "provider_responses": _provider_responses(acquisition, include_raw=bool(include_raw), secrets=secrets),
    }
    return artifact


# Common composition aliases make the boundary easy to discover without
# exposing any persistence or provider implementation details.
build_lab_artifact = compose_lab_artifact
compose_social_community_lab = compose_lab_artifact


def _build_dry_run_artifact(
    manifest: TargetManifest,
    *,
    max_post_pages: int,
    include_interactions: bool,
    include_replies: bool,
    max_interaction_pages: int,
    max_interaction_credits: int | None,
) -> dict[str, Any]:
    plans = plan_requests(
        manifest,
        max_post_pages=max_post_pages,
        include_interactions=include_interactions,
        posts=(),
        include_replies=include_replies,
    )
    matrix = _empty_capability_matrix(manifest, reason="dry_run_only")
    acquisition_options = {
        "max_post_pages": max_post_pages,
        "include_interactions": include_interactions,
        "include_replies": include_replies,
        "max_interaction_pages": max_interaction_pages,
        "max_interaction_credits": max_interaction_credits,
        "include_raw": False,
    }
    analysis_options = {"mode": "dry_run", "attempt_policy": "one_shot", "allowed_tile_ids": None}
    artifact = compose_lab_artifact(
        manifest,
        {
            "contract_version": ARTIFACT_SCHEMA_VERSION,
            "acquisition_matrix": matrix,
            "observations": [],
            "responses": [],
            "summary": {"target_count": len(manifest.targets), "request_count": len(plans)},
        },
        mode="dry_run",
        acquisition_options=acquisition_options,
        analysis_options=analysis_options,
    )
    artifact["request_plan"] = [plan.as_dict() for plan in plans]
    artifact["capability_plan"] = _redact_value(matrix, secrets=())
    artifact["dry_run"] = {
        "network": False,
        "api_key_read": False,
        "request_count": len(plans),
        "interaction_requests_deferred_until_posts_are_acquired": bool(include_interactions),
    }
    return artifact


def _build_gemini_invoker():
    """Build exactly one Gemini-native callable or fail closed.

    The generic ``_call_json`` method is intentionally not a fallback here: it
    may negotiate a different provider schema mode and may perform hidden
    retry/cache work.  The core owns the single attempt boundary.
    """

    key_available = any(os.environ.get(name, "").strip() for name in _SECRET_ENV_NAMES[1:])
    if not key_available:
        raise LiveAnalysisUnavailable("Gemini live analysis requires a configured environment key")
    try:
        from src.features.llm_analyzer import LLMAnalyzer

        analyzer = LLMAnalyzer()
    except Exception as exc:
        raise LiveAnalysisUnavailable("Gemini native analyzer is unavailable") from exc

    # This assignment must happen before any analyzer call, including the
    # native structured call.  It prevents SQLite cache reads/writes.
    analyzer.use_cache = False
    native_method = getattr(analyzer, "_call_json_gemini_native", None)
    base_url = str(getattr(analyzer, "base_url", ""))
    hostname = (urlsplit(base_url).hostname or "").casefold()
    if not callable(native_method) or hostname != "generativelanguage.googleapis.com":
        raise LiveAnalysisUnavailable("Gemini native structured output path is not available for this provider")

    def invoke(
        *,
        system: str,
        user: str,
        json_schema: Mapping[str, Any],
        schema_name: str,
        max_tokens: int,
        timeout_seconds: int,
    ) -> object:
        # Exactly one call.  Do not call _call_json or wrap this in a retry.
        result = native_method(
            system=system,
            user=user,
            json_schema=dict(json_schema),
            schema_name=schema_name,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
        # The native analyzer returns an empty mapping for transport/provider
        # failures and records the safe reason on the analyzer instance.  Do
        # not let that sentinel reach the domain validator as forged JSON.
        if isinstance(result, Mapping) and not result and getattr(analyzer, "last_failure_reason", None):
            raise RuntimeError("gemini native provider failure")
        return result

    return invoke


def _options_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "max_post_pages": args.max_post_pages,
        "include_interactions": bool(args.include_interactions),
        "include_replies": bool(args.include_replies),
        "max_interaction_pages": args.max_interaction_pages,
        "max_interaction_credits": args.max_interaction_credits,
        "include_raw": bool(args.include_raw),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated social/community laboratory.")
    parser.add_argument("--manifest", required=True, help="Strict social target manifest JSON path.")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help=f"Private atomic artifact path (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate and write a request/capability plan only.")
    parser.add_argument(
        "--acquisition-result",
        help="Replay a local accepted acquisition JSON result; no provider key or network is used.",
    )
    parser.add_argument(
        "--analyst-response",
        help="Inline JSON or local JSON path containing one recorded structured analyst response.",
    )
    parser.add_argument("--live-analysis", action="store_true", help="Use one explicit Gemini-native analysis call.")
    parser.add_argument(
        "--allowed-tile-ids",
        help="JSON array of advisory tile IDs; no rubric is imported by the runner.",
    )
    parser.add_argument("--max-post-pages", type=int, default=1)
    parser.add_argument("--include-interactions", action="store_true")
    parser.add_argument("--include-replies", action="store_true")
    parser.add_argument("--max-interaction-pages", type=int, default=1)
    parser.add_argument("--max-interaction-credits", type=int)
    parser.add_argument("--include-raw", "--include-raw-response", action="store_true")
    return parser


def _safe_exception_message(exc: Exception) -> str:
    message = _redact_text(str(exc))
    if not message or len(message) > 240:
        return "social community lab run failed"
    return message


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        # Validate before reading manifests, credentials, or constructing a
        # provider so rejected destinations cannot cause side effects.
        output_path = _validate_output_path(args.output)
        if args.dry_run and any((args.acquisition_result, args.analyst_response, args.live_analysis)):
            raise SocialCommunityLabCLIError("dry-run cannot be combined with acquisition or analysis inputs")
        if args.live_analysis and args.analyst_response:
            raise SocialCommunityLabCLIError("--live-analysis cannot be combined with --analyst-response")
        if args.include_replies and not args.include_interactions:
            raise SocialCommunityLabCLIError("--include-replies requires --include-interactions")
        if args.include_replies and args.max_interaction_credits is None:
            raise SocialCommunityLabCLIError("--include-replies requires --max-interaction-credits")
        if args.max_interaction_credits is not None and args.max_interaction_credits < 0:
            raise SocialCommunityLabCLIError("--max-interaction-credits must be non-negative")

        manifest = load_target_manifest(args.manifest)
        allowed_tile_ids = (
            _load_json_array_argument(args.allowed_tile_ids, label="allowed tile IDs")
            if args.allowed_tile_ids is not None
            else None
        )
        acquisition_options = _options_from_args(args)

        if args.dry_run:
            artifact = _build_dry_run_artifact(
                manifest,
                max_post_pages=args.max_post_pages,
                include_interactions=args.include_interactions,
                include_replies=args.include_replies,
                max_interaction_pages=args.max_interaction_pages,
                max_interaction_credits=args.max_interaction_credits,
            )
            output_path = atomic_write_json(output_path, artifact)
            print(f"Dry-run plan written ({len(artifact['request_plan'])} requests).")
            return 0

        if args.acquisition_result:
            acquisition = _read_acquisition_result(args.acquisition_result)
        else:
            # ScrapeCreatorsClient is the only path that reads the provider
            # credential.  It is never instantiated for dry-run or replay.
            with ScrapeCreatorsClient() as client:
                acquisition = run_acquisition(
                    manifest,
                    client,
                    include_raw=args.include_raw,
                    include_interactions=args.include_interactions,
                    include_replies=args.include_replies,
                    max_post_pages=args.max_post_pages,
                    max_interaction_pages=args.max_interaction_pages,
                    max_interaction_credits=args.max_interaction_credits,
                )

        analyst_response = None
        analyst_response_hash = None
        analysis_artifact = None
        mode = "acquisition_only"
        if args.analyst_response:
            analyst_response, analyst_response_hash = _load_json_argument(
                args.analyst_response,
                label="analyst response",
            )
            if not isinstance(analyst_response, Mapping):
                raise SocialCommunityLabCLIError("analyst response must be a JSON object")
            mode = "offline_analysis"
        elif args.live_analysis:
            # Persist the acquisition before crossing into the paid, one-shot
            # analysis call.  If the process is interrupted during analysis,
            # this checkpoint still contains all admitted evidence and
            # provider accounting.
            mode = "live_analysis"
            analysis_options = {
                "mode": mode,
                "allowed_tile_ids": allowed_tile_ids,
                "attempt_policy": "one_shot",
            }
            checkpoint = compose_lab_artifact(
                manifest,
                acquisition,
                mode=mode,
                allowed_tile_ids=allowed_tile_ids,
                acquisition_options=acquisition_options,
                analysis_options=analysis_options,
                include_raw=args.include_raw,
                output_path=output_path,
            )
            atomic_write_json(output_path, checkpoint)

            # The analyzer core owns the only invocation made by this
            # composition edge; there is no retry configuration surface.
            analysis_failure_reason: str | None = None
            analysis_attempt_count = 0
            try:
                invoker = _build_gemini_invoker()
            except LiveAnalysisUnavailable:
                analysis_failure_reason = "gemini_unavailable"
            except Exception:
                analysis_failure_reason = "gemini_provider_failure"
            else:
                try:
                    observations = _coerce_observations(acquisition)

                    def invoke_once(**kwargs: Any) -> object:
                        nonlocal analysis_attempt_count
                        if analysis_attempt_count:
                            raise RuntimeError("Gemini analysis retry is not allowed")
                        # Count only when the validated core crosses the
                        # injected provider boundary, immediately before the
                        # sole native invocation.
                        analysis_attempt_count = 1
                        return invoker(**kwargs)

                    analyzer = SocialCommunityAnalyzer(
                        invoke_once,
                        allowed_tile_ids=allowed_tile_ids,
                    )
                    analysis_artifact = analyzer.analyze(observations)
                    if (
                        analysis_artifact.get("status") == "unavailable"
                        and analysis_artifact.get("reason") == "analyst_attempts_exhausted"
                    ):
                        analysis_failure_reason = "gemini_provider_failure"
                except SocialCommunityLabError:
                    analysis_failure_reason = "gemini_domain_validation_failed"
                except Exception:
                    # Provider adapters own their diagnostics.  Persist only a
                    # fixed safe reason and never copy exception text into the
                    # artifact.
                    analysis_failure_reason = "gemini_provider_failure"

            if analysis_failure_reason is not None:
                observations = _coerce_observations(acquisition)
                if analysis_artifact is None:
                    if analysis_attempt_count:
                        try:
                            analysis_artifact = _analysis_attempts_exhausted(observations)
                        except SocialCommunityLabError:
                            # Keep the acquisition checkpoint and explicit
                            # top-level failure envelope even when the evidence is
                            # outside the C analyzer's bounded reconstruction
                            # envelope and cannot carry a canonical analysis body.
                            analysis_artifact = None
                    elif analysis_failure_reason == "gemini_domain_validation_failed":
                        analysis_artifact = _analysis_preflight_failed(observations)
                failed_artifact = compose_lab_artifact(
                    manifest,
                    acquisition,
                    mode=mode,
                    allowed_tile_ids=allowed_tile_ids,
                    analysis_artifact=analysis_artifact if analysis_attempt_count else None,
                    acquisition_options=acquisition_options,
                    analysis_options=analysis_options,
                    include_raw=args.include_raw,
                    output_path=output_path,
                )
                if analysis_artifact is not None and not analysis_attempt_count:
                    # A bounds failure cannot be replayed through the core's
                    # reconstruction path because it is intentionally outside
                    # those same bounds. Replace the checkpoint's
                    # not-requested state with the fixed, claim-free preflight
                    # state constructed above.
                    failed_artifact["community_analysis"] = analysis_artifact
                    failed_artifact["limitations"] = [
                        limitation
                        for limitation in failed_artifact["limitations"]
                        if limitation != "analysis_not_requested"
                    ]
                    failed_artifact["limitations"].append("analysis_preflight_failed")
                failed_artifact["analysis_failure"] = _analysis_failure_metadata(
                    analysis_failure_reason,
                    attempt_count=analysis_attempt_count,
                )
                atomic_write_json(output_path, failed_artifact)
                if analysis_failure_reason == "gemini_domain_validation_failed":
                    raise SocialCommunityLabCLIError("live Gemini analysis failed domain validation")
                if analysis_failure_reason == "gemini_unavailable":
                    raise LiveAnalysisUnavailable("Gemini live analysis is unavailable")
                raise SocialCommunityLabCLIError("live Gemini analysis failed")

            # Compose takes a recorded structured object so live output uses
            # exactly the same post-validation and artifact path.
            analyst_response = None

        artifact = compose_lab_artifact(
            manifest,
            acquisition,
            mode=mode,
            allowed_tile_ids=allowed_tile_ids,
            analyst_response=analyst_response,
            analysis_artifact=analysis_artifact if args.live_analysis else None,
            analyst_response_hash=analyst_response_hash,
            acquisition_options=acquisition_options,
            analysis_options={
                "mode": mode,
                "allowed_tile_ids": allowed_tile_ids,
                "attempt_policy": "one_shot",
            },
            include_raw=args.include_raw,
            output_path=output_path,
        )
        output_path = atomic_write_json(output_path, artifact)
        status = artifact["community_analysis"]["status"]
        print(
            f"Social community lab artifact written (status={status}, observations={len(_coerce_observations(acquisition))})."
        )
        return 0
    except (
        ScrapeCreatorsSpikeError,
        SocialLabContractError,
        SocialCommunityLabError,
        SocialCommunityLabCLIError,
        ValueError,
    ) as exc:
        print(f"error: {_safe_exception_message(exc)}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - fail-safe CLI envelope
        print(f"error: {_safe_exception_message(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "DEFAULT_OUTPUT_PATH",
    "LiveAnalysisUnavailable",
    "RUNNER_VERSION",
    "SocialCommunityLabCLIError",
    "build_lab_artifact",
    "compose_lab_artifact",
    "compose_social_community_lab",
    "main",
]
