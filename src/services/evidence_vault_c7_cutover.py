"""Vault-only control plane for operational C7 authority.

The legacy SV9 C7 tile remains unchanged.  This module controls only whether an
accepted Evidence Vault v2 C7 ``all_of`` authority may affect a live Vault view.
Persisted packets and evaluations remain immutable, non-runtime artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import re
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_composite_group_lifecycle import (
    EVIDENCE_VAULT_COMPOSITE_GROUP_ATTESTATION_VERSION,
)
from src.services.evidence_vault_operational_scoring import (
    EvidenceVaultOperationalScoringError,
    build_operational_score_evaluation,
    validate_operational_score_evaluation,
)
from src.sv9.rubric import COMPONENTS


C7_CUTOVER_CONFIG_VERSION = "evidence-vault-c7-cutover-config-v1"
C7_CUTOVER_DECISION_VERSION = "evidence-vault-c7-cutover-decision-v1"
C7_RUNTIME_PROJECTION_VERSION = "evidence-vault-c7-runtime-projection-v1"
C7_GROUP_ATTESTATION_VERSION = EVIDENCE_VAULT_COMPOSITE_GROUP_ATTESTATION_VERSION
MASTER_ENABLE_ENV = "BRAND3_VAULT_C7_CUTOVER_ENABLED"
EMERGENCY_DENY_ENV = "BRAND3_VAULT_C7_EMERGENCY_DENY"
ALLOWLIST_ENV = "BRAND3_VAULT_C7_ALLOWLIST"
ENVIRONMENT_ENV = "BRAND3_ENVIRONMENT"
_C7_GROUP_CONTRACT = {
    "decision_rule": "all_of",
    "minimum_member_count": 2,
    "required_distinct_source_identity_count": 2,
    "required_channel_roles": ["external_social_profile", "owned_web"],
    "member_decisions_must_match": True,
    "member_change_requires_review": True,
}
_C7_GROUP_CONTRACT_FINGERPRINT = canonical_fingerprint(
    "evidence-vault-c7-group-contract-v1",
    _C7_GROUP_CONTRACT,
)

_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class EvidenceVaultC7CutoverError(ValueError):
    """Operational C7 cannot cross the runtime boundary safely."""


@dataclass(frozen=True, slots=True)
class EvidenceVaultC7CutoverConfig:
    master_enabled: bool
    emergency_deny: bool
    allowlisted_domains: frozenset[str]
    master_valid: bool
    emergency_deny_valid: bool
    allowlist_valid: bool
    config_fingerprint: str

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "EvidenceVaultC7CutoverConfig":
        source = os.environ if environ is None else environ
        master, master_valid = _strict_boolean(
            source.get(MASTER_ENABLE_ENV),
            missing=False,
        )
        # A missing or malformed emergency switch is engaged, never inferred off.
        emergency, emergency_valid = _strict_boolean(
            source.get(EMERGENCY_DENY_ENV),
            missing=True,
        )
        allowlist, allowlist_valid = _strict_allowlist(source.get(ALLOWLIST_ENV))
        material = {
            "master_enabled": master,
            "emergency_deny": emergency,
            "allowlisted_domains": sorted(allowlist),
            "master_valid": master_valid,
            "emergency_deny_valid": emergency_valid,
            "allowlist_valid": allowlist_valid,
        }
        return cls(
            master_enabled=master,
            emergency_deny=emergency,
            allowlisted_domains=allowlist,
            master_valid=master_valid,
            emergency_deny_valid=emergency_valid,
            allowlist_valid=allowlist_valid,
            config_fingerprint=canonical_fingerprint(
                C7_CUTOVER_CONFIG_VERSION,
                material,
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceVaultC7CutoverDecision:
    enabled: bool
    reason: str
    canonical_brand: str | None
    config_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": C7_CUTOVER_DECISION_VERSION,
            "enabled": self.enabled,
            "reason": self.reason,
            "canonical_brand": self.canonical_brand,
            "config_fingerprint": self.config_fingerprint,
            "authority_scope": "b3s-vault",
            "production_runtime_effect": False,
            "operational_c7_effect": self.enabled,
        }


class C7RuntimeRepository(Protocol):
    def get_evidence_vault_c7_runtime_snapshot(
        self,
        domain_or_url: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None: ...


def canonicalize_c7_brand(value: str, *, allow_url: bool = True) -> str:
    """Return one strict ASCII DNS authority or raise.

    This validator is deliberately narrower than history grouping helpers.  It
    rejects credentials, ports, IPs, single-label hosts, wildcards, Unicode and
    ambiguous punycode identities.
    """

    raw = str(value or "").strip()
    if (
        not raw
        or not raw.isascii()
        or any(character.isspace() for character in raw)
    ):
        raise EvidenceVaultC7CutoverError("invalid_brand_identity")
    if "://" in raw:
        if not allow_url:
            raise EvidenceVaultC7CutoverError("allowlist_token_is_not_a_domain")
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname
            username = parsed.username
            password = parsed.password
        except ValueError as exc:
            raise EvidenceVaultC7CutoverError("invalid_brand_url") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or username is not None
            or password is not None
            or ":" in parsed.netloc
            or "[" in parsed.netloc
            or "]" in parsed.netloc
        ):
            raise EvidenceVaultC7CutoverError("invalid_brand_url")
    else:
        if any(character in raw for character in "/?#@:"):
            raise EvidenceVaultC7CutoverError("invalid_brand_domain")
        host = raw
    host = str(host).lower()
    if host.endswith(".."):
        raise EvidenceVaultC7CutoverError("invalid_brand_domain")
    host = host.removesuffix(".")
    if host.startswith("www."):
        host = host[4:]
    if (
        not host
        or len(host) > 253
        or host.startswith("www.")
        or "*" in host
        or not host.isascii()
        or any(label.startswith("xn--") for label in host.split("."))
    ):
        raise EvidenceVaultC7CutoverError("invalid_brand_domain")
    if all(
        label.isdigit() or re.fullmatch(r"0x[0-9a-f]+", label)
        for label in host.split(".")
    ):
        raise EvidenceVaultC7CutoverError("ip_brand_identity_is_not_allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise EvidenceVaultC7CutoverError("ip_brand_identity_is_not_allowed")
    labels = host.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise EvidenceVaultC7CutoverError("invalid_brand_domain")
    return host


def decide_c7_cutover(
    brand_or_url: str,
    *,
    environment: str,
    config: EvidenceVaultC7CutoverConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> EvidenceVaultC7CutoverDecision:
    """Evaluate the complete control conjunction with emergency-deny priority."""

    controls = config or EvidenceVaultC7CutoverConfig.from_environ(environ)
    try:
        brand = canonicalize_c7_brand(brand_or_url)
    except EvidenceVaultC7CutoverError:
        brand = None
    if not controls.emergency_deny_valid:
        reason = "emergency_deny_invalid"
    elif controls.emergency_deny:
        reason = "emergency_deny_engaged"
    elif not controls.master_valid:
        reason = "master_enable_invalid"
    elif not controls.allowlist_valid:
        reason = "allowlist_invalid"
    elif environment != "vault":
        reason = "environment_not_vault"
    elif not controls.master_enabled:
        reason = "master_disabled"
    elif not controls.allowlisted_domains:
        reason = "allowlist_empty"
    elif brand is None:
        reason = "brand_identity_invalid"
    elif brand not in controls.allowlisted_domains:
        reason = "brand_not_allowlisted"
    else:
        reason = "enabled"
    return EvidenceVaultC7CutoverDecision(
        enabled=reason == "enabled",
        reason=reason,
        canonical_brand=brand,
        config_fingerprint=controls.config_fingerprint,
    )


def current_c7_cutover_decision(
    brand_or_url: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> EvidenceVaultC7CutoverDecision:
    source = os.environ if environ is None else environ
    return decide_c7_cutover(
        brand_or_url,
        environment=str(source.get(ENVIRONMENT_ENV) or ""),
        environ=source,
    )


def operation_plan_affects_operational_c7(plan: Mapping[str, Any]) -> bool:
    """Return whether one validated incremental plan can affect C7 authority."""

    delta = plan.get("delta")
    return bool(
        plan.get("mode") == "incremental_refresh"
        and isinstance(delta, Mapping)
        and "C7" in (delta.get("affected_tile_ids") or [])
    )


def build_c7_runtime_projection(
    *,
    decision: EvidenceVaultC7CutoverDecision,
    canonical_memory: Mapping[str, Any],
    score_evaluation: Mapping[str, Any],
    active_group_attestation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Wrap immutable Vault authority in a present-time, Vault-only envelope."""

    if not decision.enabled or decision.canonical_brand is None:
        raise EvidenceVaultC7CutoverError("operational_c7_cutover_is_disabled")
    try:
        validate_operational_score_evaluation(score_evaluation)
        rebuilt = build_operational_score_evaluation(
            canonical_memory,
            created_at=str(score_evaluation.get("created_at") or ""),
        )
    except (EvidenceVaultOperationalScoringError, TypeError) as exc:
        raise EvidenceVaultC7CutoverError("operational_c7_memory_or_score_is_invalid") from exc
    brand = str(canonical_memory.get("brand_identity") or "")
    if (
        brand != decision.canonical_brand
        or rebuilt["canonical_memory_version"]
        != score_evaluation.get("canonical_memory_version")
        or rebuilt["evaluation_identity"]
        != score_evaluation.get("evaluation_identity")
        or rebuilt["score_input_fingerprint"]
        != score_evaluation.get("score_input_fingerprint")
    ):
        raise EvidenceVaultC7CutoverError("operational_c7_identity_mismatch")

    content = canonical_memory["content"]
    accepted = [
        dict(row)
        for row in content.get("accepted_tiles") or []
        if isinstance(row, Mapping) and row.get("tile_id") == "C7"
    ]
    pending = [
        dict(row)
        for row in content.get("pending_reassessments") or []
        if isinstance(row, Mapping) and row.get("tile_id") == "C7"
    ]
    if len(accepted) > 1 or len(pending) > 1 or (accepted and pending):
        raise EvidenceVaultC7CutoverError("operational_c7_authority_is_ambiguous")

    group_identity: dict[str, Any] | None = None
    if accepted:
        attestation = _validate_group_attestation(
            active_group_attestation,
            canonical_memory=canonical_memory,
            accepted_tile=accepted[0],
        )
        group_identity = {
            "group_id": attestation["group_id"],
            "group_contract_fingerprint": attestation[
                "group_contract_fingerprint"
            ],
            "member_relation_ids": attestation["member_relation_ids"],
            "member_channel_roles": attestation["member_channel_roles"],
            "attestation_fingerprint": attestation["attestation_fingerprint"],
        }
        state = str(accepted[0]["semantic_state"])
        status = "accepted"
        authority = True
    elif pending:
        state = "sin_evidencia"
        status = "pending_reassessment"
        authority = False
        group_identity = {
            "group_id": pending[0]["prior_group_id"],
            "reopen_policy_fingerprint": pending[0][
                "reopen_policy_fingerprint"
            ],
            "trigger_fingerprint": pending[0]["trigger_fingerprint"],
        }
    else:
        state = "sin_evidencia"
        status = "unresolved"
        authority = False

    multiplier = int(COMPONENTS["coherencia"]["multiplier"])
    points = multiplier if state == "ok" and authority else 0
    body = {
        "schema_version": C7_RUNTIME_PROJECTION_VERSION,
        "brand_identity": brand,
        "canonical_memory_version": canonical_memory["canonical_memory_version"],
        "adoption_event_id": canonical_memory["adoption_event_id"],
        "evaluation_identity": score_evaluation["evaluation_identity"],
        "score_input_fingerprint": score_evaluation["score_input_fingerprint"],
        "tile_id": "C7",
        "component_key": "coherencia",
        "status": status,
        "semantic_state": state,
        "effective_points": points,
        "score_eligible": authority and state == "ok",
        "group_identity": group_identity,
        "authority": authority,
        "authority_scope": "b3s-vault",
        "vault_runtime_effect": True,
        "operational_c7_effect": True,
        "legacy_c7_unchanged": True,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    return {
        **body,
        "projection_fingerprint": canonical_fingerprint(
            C7_RUNTIME_PROJECTION_VERSION,
            body,
        ),
    }


def load_c7_runtime_projection(
    repository: C7RuntimeRepository,
    brand_or_url: str,
    *,
    workspace_slug: str = "b3s",
) -> dict[str, Any] | None:
    """Load one read-only transactional snapshot or remain unavailable.

    The production repository intentionally returns no snapshot until verified
    raw lineage and an atomic read contract exist. This path never creates score
    rows while serving a request.
    """

    def decision_now() -> EvidenceVaultC7CutoverDecision:
        return current_c7_cutover_decision(brand_or_url)

    first = decision_now()
    if not first.enabled or first.canonical_brand is None:
        return None
    snapshot = repository.get_evidence_vault_c7_runtime_snapshot(
        first.canonical_brand,
        workspace_slug=workspace_slug,
    )
    if not isinstance(snapshot, Mapping):
        return None
    final = decision_now()
    if not final.enabled or final != first:
        return None
    memory = snapshot.get("canonical_memory")
    evaluation = snapshot.get("score_evaluation")
    attestation = snapshot.get("active_group_attestation")
    if not all(
        isinstance(value, Mapping)
        for value in (memory, evaluation, attestation)
    ):
        return None
    return build_c7_runtime_projection(
        decision=final,
        canonical_memory=memory,
        score_evaluation=evaluation,
        active_group_attestation=attestation,
    )


def _validate_group_attestation(
    value: Mapping[str, Any] | None,
    *,
    canonical_memory: Mapping[str, Any],
    accepted_tile: Mapping[str, Any],
) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "brand_identity",
        "canonical_memory_version",
        "exact_source_candidate_packet_fingerprint",
        "tile_id",
        "group_id",
        "decision_rule",
        "group_contract_fingerprint",
        "member_relation_ids",
        "member_evidence_fingerprints",
        "member_channel_roles",
        "authority",
        "authority_scope",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "attestation_fingerprint",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise EvidenceVaultC7CutoverError("active_c7_group_is_not_attested")
    attestation = dict(value)
    fingerprint = str(attestation.pop("attestation_fingerprint", ""))
    basis = [
        dict(row)
        for row in accepted_tile.get("basis") or []
        if isinstance(row, Mapping)
    ]
    relation_ids = sorted(str(row.get("relation_id") or "") for row in basis)
    evidence_fingerprints = list(value.get("member_evidence_fingerprints") or [])
    claim_ids = {str(row.get("claim_id") or "") for row in basis}
    decision_ids = [str(row.get("decision_event_id") or "") for row in basis]
    sha_fields = [
        fingerprint,
        str(value.get("exact_source_candidate_packet_fingerprint") or ""),
        str(value.get("group_id") or ""),
        str(value.get("group_contract_fingerprint") or ""),
        *relation_ids,
        *evidence_fingerprints,
    ]
    if (
        value.get("schema_version") != C7_GROUP_ATTESTATION_VERSION
        or value.get("brand_identity") != canonical_memory.get("brand_identity")
        or value.get("canonical_memory_version")
        != canonical_memory.get("canonical_memory_version")
        or value.get("tile_id") != "C7"
        or value.get("decision_rule") != "all_of"
        or value.get("group_contract_fingerprint")
        != _C7_GROUP_CONTRACT_FINGERPRINT
        or value.get("member_channel_roles")
        != ["external_social_profile", "owned_web"]
        or value.get("member_relation_ids") != relation_ids
        or evidence_fingerprints != sorted(set(evidence_fingerprints))
        or len(evidence_fingerprints) != 2
        or len(basis) != 2
        or claim_ids != {str(value.get("group_id") or "")}
        or any(row.get("polarity") != "supports" for row in basis)
        or any(row.get("review_status") != "accepted" for row in basis)
        or any(not decision_id for decision_id in decision_ids)
        or len({str(row.get("source_identity_id") or "") for row in basis}) != 2
        or value.get("authority") is not True
        or value.get("authority_scope") != "b3s-vault"
        or value.get("production_runtime_effect") is not False
        or value.get("scanner_runtime_effect") is not False
        or any(
            len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
            for item in sha_fields
        )
        or fingerprint
        != canonical_fingerprint(C7_GROUP_ATTESTATION_VERSION, attestation)
    ):
        raise EvidenceVaultC7CutoverError("active_c7_group_attestation_mismatch")
    return dict(value)


def _strict_boolean(value: str | None, *, missing: bool) -> tuple[bool, bool]:
    if value is None:
        return missing, True
    if value == "true":
        return True, True
    if value == "false":
        return False, True
    return missing, False


def _strict_allowlist(value: str | None) -> tuple[frozenset[str], bool]:
    if value is None or value == "":
        return frozenset(), True
    tokens = value.split(",")
    if any(not token.strip() for token in tokens):
        return frozenset(), False
    canonical: set[str] = set()
    try:
        for token in tokens:
            canonical.add(canonicalize_c7_brand(token.strip(), allow_url=False))
    except EvidenceVaultC7CutoverError:
        return frozenset(), False
    return frozenset(canonical), True


__all__ = [
    "ALLOWLIST_ENV",
    "C7_CUTOVER_CONFIG_VERSION",
    "C7_CUTOVER_DECISION_VERSION",
    "C7_GROUP_ATTESTATION_VERSION",
    "C7_RUNTIME_PROJECTION_VERSION",
    "EMERGENCY_DENY_ENV",
    "ENVIRONMENT_ENV",
    "MASTER_ENABLE_ENV",
    "EvidenceVaultC7CutoverConfig",
    "EvidenceVaultC7CutoverDecision",
    "EvidenceVaultC7CutoverError",
    "build_c7_runtime_projection",
    "canonicalize_c7_brand",
    "current_c7_cutover_decision",
    "decide_c7_cutover",
    "load_c7_runtime_projection",
]
