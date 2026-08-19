"""Strict, replay-safe projection of a signed trusted acquisition outcome."""

from __future__ import annotations

from typing import Any, Mapping


_OUTCOME_SCHEMA_VERSION = "evidence-vault-acquisition-outcome-v1"
_PROVENANCE_SCHEMA_VERSION = "evidence-vault-raw-provenance-envelope-v1"
_PROVENANCE_KEY = "evidence_vault_raw_provenance"
_FAILURE_REASONS = frozenset(
    {
        "provider_not_configured",
        "provider_unavailable",
        "provider_result_ineligible",
    }
)


def trusted_acquisition_report_metadata(
    capture_payload: Mapping[str, Any],
    *,
    external_captured: bool | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Validate the signed outcome and derive public acquisition metadata.

    Absence of ``acquisition_outcome`` is the only legacy-compatible case.
    A present object is strict and must agree with the durable receipt set.
    """

    if "acquisition_gate" in capture_payload:
        raise ValueError("trusted acquisition payload may not embed acquisition gate")
    if external_captured is None:
        external_captured = _external_captured_from_provenance(
            capture_payload.get(_PROVENANCE_KEY),
        )

    attempts = [
        {
            "provider": "web",
            "intent": "owned_web",
            "status": "success",
            "detail": "verified_raw_document_persisted",
        }
    ]
    if "acquisition_outcome" not in capture_payload:
        if external_captured:
            attempts.append(_external_success_attempt())
        return [], attempts

    raw_outcome = capture_payload["acquisition_outcome"]
    if not isinstance(raw_outcome, Mapping):
        raise ValueError("trusted acquisition outcome must be an object")
    outcome = dict(raw_outcome)
    if set(outcome) != {"schema_version", "external", "failure_reason"}:
        raise ValueError("trusted acquisition outcome fields are invalid")
    if outcome["schema_version"] != _OUTCOME_SCHEMA_VERSION:
        raise ValueError("trusted acquisition outcome schema is invalid")

    external_outcome = outcome["external"]
    failure_reason = outcome["failure_reason"]
    if external_outcome == "captured":
        if not external_captured or failure_reason is not None:
            raise ValueError("trusted acquisition captured outcome is invalid")
        attempts.append(_external_success_attempt())
        return [], attempts
    if external_outcome == "not_discovered":
        if external_captured or failure_reason is not None:
            raise ValueError("trusted acquisition not-discovered outcome is invalid")
        discovery = capture_payload.get("external_discovery")
        if _independent_discovery_searched(discovery):
            attempts.append(
                {
                    "provider": "exa",
                    "intent": "external_social_profile",
                    "status": "not_discovered",
                    "detail": "independent_discovery_unassociated",
                }
            )
        return [
            "verified_external_document_unavailable",
            "external_acquisition:not_discovered",
        ], attempts
    if external_outcome != "owned_only_downgrade" or external_captured:
        raise ValueError("trusted acquisition external outcome is invalid")
    if failure_reason not in _FAILURE_REASONS:
        raise ValueError("trusted acquisition external failure reason is invalid")
    attempts.append(
        {
            "provider": "exa",
            "intent": "external_social_profile",
            "status": "error",
            "detail": failure_reason,
        }
    )
    return [
        "verified_external_document_unavailable",
        f"external_acquisition:{failure_reason}",
    ], attempts


def _external_captured_from_provenance(value: Any) -> bool:
    if not isinstance(value, Mapping):
        raise ValueError("trusted acquisition provenance is unavailable")
    if value.get("schema_version") != _PROVENANCE_SCHEMA_VERSION:
        raise ValueError("trusted acquisition provenance is invalid")
    signed_receipts = value.get("signed_receipts")
    if not isinstance(signed_receipts, list) or not 1 <= len(signed_receipts) <= 2:
        raise ValueError("trusted acquisition provenance is invalid")
    roles: list[str] = []
    for receipt in signed_receipts:
        if not isinstance(receipt, Mapping):
            raise ValueError("trusted acquisition provenance is invalid")
        claims = receipt.get("claims")
        if not isinstance(claims, Mapping):
            raise ValueError("trusted acquisition provenance is invalid")
        role = claims.get("channel_role")
        if role not in {"owned_web", "external_social_profile"}:
            raise ValueError("trusted acquisition provenance is invalid")
        roles.append(str(role))
    if roles.count("owned_web") != 1 or len(roles) != len(set(roles)):
        raise ValueError("trusted acquisition provenance is invalid")
    external_captured = "external_social_profile" in roles
    if external_captured != (len(roles) == 2):
        raise ValueError("trusted acquisition provenance is invalid")
    return external_captured


def _independent_discovery_searched(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    candidates = value.get("candidate_urls")
    return (
        value.get("schema_version") == "evidence-vault-external-discovery-v1"
        and value.get("provider") == "exa"
        and value.get("status") == "searched"
        and isinstance(candidates, list)
    )


def _external_success_attempt() -> dict[str, str]:
    return {
        "provider": "exa",
        "intent": "external_social_profile",
        "status": "success",
        "detail": "verified_raw_document_persisted",
    }


__all__ = ["trusted_acquisition_report_metadata"]
