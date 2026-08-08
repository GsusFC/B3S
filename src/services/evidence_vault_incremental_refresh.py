"""Pure operation planning for baseline and incremental Vault scans.

This module is deliberately outside the live scanner orchestration.  It makes
the desired Vault-only execution boundary executable and testable before any
route is allowed to use it:

* ``baseline`` may analyse the complete rubric once;
* ``incremental_refresh`` persists acquisition and analyses only material
  evidence deltas;
* ``diagnostic_full`` may rerun the complete legacy analysis, but never gains
  canonical authority.

The planner performs no LLM calls, persistence, scoring, or promotion.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Iterable, Mapping

from src.services.evidence_vault_canonical_core import (
    build_tile_contract_registry,
    canonical_fingerprint,
)
from src.services.scanner_evidence_comparison import (
    CanonicalEvidenceRecord,
    canonical_evidence_representatives,
    canonical_evidence_rows,
)
from src.sv9_flow.contracts import EvidenceRecord
from src.sv9_flow.evidence_labeling_worker import is_evidence_record_labelable


EVIDENCE_VAULT_OPERATION_PLAN_VERSION = "evidence-vault-operation-plan-v1"
EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION = "evidence-vault-incremental-delta-v1"
VAULT_INCREMENTAL_ENVIRONMENT = "vault"


class EvidenceVaultOperationPlanError(ValueError):
    """The requested Vault operation cannot be planned safely."""


class VaultScanMode(StrEnum):
    BASELINE = "baseline"
    INCREMENTAL_REFRESH = "incremental_refresh"
    DIAGNOSTIC_FULL = "diagnostic_full"


def resolve_vault_scan_mode(
    *,
    environment: str,
    incremental_enabled: bool,
    has_canonical_memory: bool,
    requested_mode: str | None = None,
) -> dict[str, Any]:
    """Resolve the Vault mode without changing the existing scanner path.

    Until the feature is explicitly enabled in ``BRAND3_ENVIRONMENT=vault``,
    callers must continue through the existing scanner.  A Vault mode request
    outside that boundary fails closed instead of silently changing production.
    """

    normalized_environment = str(environment or "").strip().lower()
    requested = str(requested_mode or "").strip().lower() or None
    if requested is not None and requested not in {mode.value for mode in VaultScanMode}:
        raise EvidenceVaultOperationPlanError(
            f"unsupported vault scan mode: {requested}"
        )
    if normalized_environment != VAULT_INCREMENTAL_ENVIRONMENT or not incremental_enabled:
        if requested is not None:
            raise EvidenceVaultOperationPlanError(
                "vault scan modes require the enabled vault environment"
            )
        return {
            "execution_path": "existing_scanner",
            "mode": None,
            "vault_only": False,
            "reason": "vault_incremental_not_enabled",
        }

    if requested == VaultScanMode.DIAGNOSTIC_FULL.value:
        mode = VaultScanMode.DIAGNOSTIC_FULL
    elif has_canonical_memory:
        if requested == VaultScanMode.BASELINE.value:
            raise EvidenceVaultOperationPlanError(
                "baseline cannot replace existing canonical memory"
            )
        mode = VaultScanMode.INCREMENTAL_REFRESH
    else:
        if requested == VaultScanMode.INCREMENTAL_REFRESH.value:
            raise EvidenceVaultOperationPlanError(
                "incremental refresh requires canonical memory"
            )
        mode = VaultScanMode.BASELINE
    return {
        "execution_path": "vault_incremental",
        "mode": mode.value,
        "vault_only": True,
        "reason": "explicit_diagnostic" if mode is VaultScanMode.DIAGNOSTIC_FULL else (
            "canonical_memory_exists"
            if mode is VaultScanMode.INCREMENTAL_REFRESH
            else "canonical_memory_missing"
        ),
    }


def build_vault_scan_plan(
    *,
    brand_identity: str,
    subject_url: str,
    mode: str,
    current_evidence_records: Iterable[Mapping[str, Any]],
    previous_capture_evidence_records: Iterable[Mapping[str, Any]] = (),
    known_evidence_records: Iterable[Mapping[str, Any]] = (),
    accepted_evidence_tile_relations: Iterable[Mapping[str, Any]] = (),
    canonical_memory_version: str | None = None,
) -> dict[str, Any]:
    """Build a frozen, non-authoritative execution plan for one Vault scan."""

    try:
        selected_mode = VaultScanMode(str(mode))
    except ValueError as exc:
        raise EvidenceVaultOperationPlanError(f"unsupported vault scan mode: {mode}") from exc
    brand = str(brand_identity or "").strip().lower()
    if not brand:
        raise EvidenceVaultOperationPlanError("brand_identity is required")
    url = str(subject_url or "").strip()
    if not url:
        raise EvidenceVaultOperationPlanError("subject_url is required")
    canonical_version = _optional_fingerprint(canonical_memory_version)

    current_rows = [
        dict(row) for row in current_evidence_records if isinstance(row, Mapping)
    ]
    previous_rows = [
        dict(row)
        for row in previous_capture_evidence_records
        if isinstance(row, Mapping)
    ]
    known_rows = [
        dict(row) for row in known_evidence_records if isinstance(row, Mapping)
    ]
    relation_rows = [
        dict(row)
        for row in accepted_evidence_tile_relations
        if isinstance(row, Mapping)
    ]
    current = canonical_evidence_rows(
        current_rows,
        subject_url=url,
    )
    previous = canonical_evidence_rows(
        previous_rows,
        subject_url=url,
    )
    known = canonical_evidence_rows(
        known_rows,
        subject_url=url,
    )
    labelable_fingerprints = _labelable_fingerprints(
        current_rows,
        subject_url=url,
    )

    if selected_mode is VaultScanMode.BASELINE:
        if canonical_version is not None:
            raise EvidenceVaultOperationPlanError(
                "baseline plan cannot target existing canonical memory"
            )
        delta = _baseline_delta(current)
        operations = {
            "persist_capture_only": True,
            "classify_evidence_fingerprints": sorted(labelable_fingerprints),
            "propose_tile_relations_for_fingerprints": sorted(
                labelable_fingerprints
            ),
            "reevaluate_tile_ids": _all_tile_ids(),
            "llm_required": bool(labelable_fingerprints),
            "recalculate_canonical_score": False,
            "create_candidate_packet": True,
            "create_canonical_report": False,
            "create_diagnostic_report": False,
        }
        canonical_impact = "baseline_candidate_required"
    elif selected_mode is VaultScanMode.DIAGNOSTIC_FULL:
        delta = _diagnostic_delta(current)
        operations = {
            "persist_capture_only": True,
            "classify_evidence_fingerprints": sorted(labelable_fingerprints),
            "propose_tile_relations_for_fingerprints": sorted(
                labelable_fingerprints
            ),
            "reevaluate_tile_ids": _all_tile_ids(),
            "llm_required": bool(labelable_fingerprints),
            "recalculate_canonical_score": False,
            "create_candidate_packet": False,
            "create_canonical_report": False,
            "create_diagnostic_report": True,
        }
        canonical_impact = "diagnostic_only"
    else:
        if canonical_version is None:
            raise EvidenceVaultOperationPlanError(
                "incremental refresh requires canonical_memory_version"
            )
        delta = build_incremental_evidence_delta(
            subject_url=url,
            current_evidence_records=current_rows,
            previous_capture_evidence_records=previous_rows,
            known_evidence_records=known_rows,
            accepted_evidence_tile_relations=relation_rows,
        )
        analysis_fingerprints = [
            *delta["added_evidence_fingerprints"],
            *delta["modified_evidence_fingerprints"],
            *delta["unanalysed_unchanged_evidence_fingerprints"],
        ]
        analysis_fingerprints = sorted(
            set(analysis_fingerprints).intersection(labelable_fingerprints)
        )
        has_analysis = bool(analysis_fingerprints)
        operations = {
            "persist_capture_only": True,
            "classify_evidence_fingerprints": analysis_fingerprints,
            "propose_tile_relations_for_fingerprints": analysis_fingerprints,
            "reevaluate_tile_ids": list(delta["affected_tile_ids"]),
            "llm_required": has_analysis,
            "recalculate_canonical_score": False,
            "create_candidate_packet": has_analysis,
            "create_canonical_report": False,
            "create_diagnostic_report": False,
        }
        canonical_impact = "review_required" if has_analysis else "none"

    semantic_context_unsigned = {
        "scope": "exact_capture_owned_identity_context",
        "evidence_fingerprints": sorted(row.fingerprint for row in current),
    }
    semantic_context = {
        **semantic_context_unsigned,
        "context_fingerprint": canonical_fingerprint(
            "evidence-vault-semantic-context-v1",
            semantic_context_unsigned,
        ),
    }
    unsigned = {
        "schema_version": EVIDENCE_VAULT_OPERATION_PLAN_VERSION,
        "authority": False,
        "runtime_effect": False,
        "authority_scope": "b3s-vault",
        "brand_identity": brand,
        "subject_url": url,
        "mode": selected_mode.value,
        "canonical_memory_version": canonical_version,
        "canonical_impact": canonical_impact,
        "semantic_context": semantic_context,
        "delta": delta,
        "operations": operations,
        "exit_contract": {
            "canonical_memory_changes": False,
            "canonical_score_changes": False,
            "promotion_required_for_canonical_change": True,
        },
    }
    plan = {
        **unsigned,
        "operation_plan_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_OPERATION_PLAN_VERSION,
            unsigned,
        ),
    }
    validate_vault_scan_plan(plan)
    return plan


def validate_vault_scan_plan(plan: Mapping[str, Any]) -> None:
    """Validate one frozen non-authoritative operation plan and its hash."""

    fields = {
        "schema_version",
        "authority",
        "runtime_effect",
        "authority_scope",
        "brand_identity",
        "subject_url",
        "mode",
        "canonical_memory_version",
        "canonical_impact",
        "semantic_context",
        "delta",
        "operations",
        "exit_contract",
        "operation_plan_fingerprint",
    }
    if not isinstance(plan, Mapping) or set(plan) != fields:
        raise EvidenceVaultOperationPlanError("operation plan fields mismatch")
    if (
        plan.get("schema_version") != EVIDENCE_VAULT_OPERATION_PLAN_VERSION
        or plan.get("authority") is not False
        or plan.get("runtime_effect") is not False
        or plan.get("authority_scope") != "b3s-vault"
    ):
        raise EvidenceVaultOperationPlanError("operation plan authority is invalid")
    try:
        VaultScanMode(str(plan.get("mode") or ""))
    except ValueError as exc:
        raise EvidenceVaultOperationPlanError("operation plan mode is invalid") from exc
    if not str(plan.get("brand_identity") or "").strip():
        raise EvidenceVaultOperationPlanError("operation plan brand is required")
    if not str(plan.get("subject_url") or "").strip():
        raise EvidenceVaultOperationPlanError("operation plan URL is required")
    semantic_context = plan.get("semantic_context")
    if (
        not isinstance(semantic_context, Mapping)
        or set(semantic_context) != {
            "scope",
            "evidence_fingerprints",
            "context_fingerprint",
        }
        or semantic_context.get("scope")
        != "exact_capture_owned_identity_context"
        or not isinstance(semantic_context.get("evidence_fingerprints"), list)
        or semantic_context["evidence_fingerprints"]
        != sorted(set(semantic_context["evidence_fingerprints"]))
        or semantic_context.get("context_fingerprint")
        != canonical_fingerprint(
            "evidence-vault-semantic-context-v1",
            {
                "scope": semantic_context.get("scope"),
                "evidence_fingerprints": semantic_context.get(
                    "evidence_fingerprints"
                ),
            },
        )
    ):
        raise EvidenceVaultOperationPlanError(
            "operation plan semantic context is invalid"
        )
    for fingerprint in semantic_context["evidence_fingerprints"]:
        _optional_fingerprint(fingerprint)
    canonical_version = plan.get("canonical_memory_version")
    if canonical_version is not None and (
        not isinstance(canonical_version, str)
        or len(canonical_version) != 64
        or any(character not in "0123456789abcdef" for character in canonical_version)
    ):
        raise EvidenceVaultOperationPlanError(
            "operation plan canonical version is invalid"
        )
    delta = plan.get("delta")
    if not isinstance(delta, Mapping):
        raise EvidenceVaultOperationPlanError("operation plan delta is required")
    operations = plan.get("operations")
    operation_fields = {
        "persist_capture_only",
        "classify_evidence_fingerprints",
        "propose_tile_relations_for_fingerprints",
        "reevaluate_tile_ids",
        "llm_required",
        "recalculate_canonical_score",
        "create_candidate_packet",
        "create_canonical_report",
        "create_diagnostic_report",
    }
    if not isinstance(operations, Mapping) or set(operations) != operation_fields:
        raise EvidenceVaultOperationPlanError("operation plan operations mismatch")
    for field in (
        "persist_capture_only",
        "llm_required",
        "recalculate_canonical_score",
        "create_candidate_packet",
        "create_canonical_report",
        "create_diagnostic_report",
    ):
        if not isinstance(operations[field], bool):
            raise EvidenceVaultOperationPlanError(
                f"operation flag {field} must be boolean"
            )
    for field in (
        "classify_evidence_fingerprints",
        "propose_tile_relations_for_fingerprints",
        "reevaluate_tile_ids",
    ):
        rows = operations[field]
        if (
            not isinstance(rows, list)
            or any(not isinstance(row, str) or not row for row in rows)
            or len(rows) != len(set(rows))
        ):
            raise EvidenceVaultOperationPlanError(
                f"operation list {field} is invalid"
            )
    if operations["persist_capture_only"] is not True:
        raise EvidenceVaultOperationPlanError("Vault plan must persist capture")
    if (
        operations["classify_evidence_fingerprints"]
        != sorted(operations["classify_evidence_fingerprints"])
        or not set(operations["classify_evidence_fingerprints"]).issubset(
            semantic_context["evidence_fingerprints"]
        )
        or operations["llm_required"]
        is not bool(operations["classify_evidence_fingerprints"])
    ):
        raise EvidenceVaultOperationPlanError(
            "operation semantic workset is inconsistent with its frozen context"
        )
    if (
        operations["classify_evidence_fingerprints"]
        != operations["propose_tile_relations_for_fingerprints"]
    ):
        raise EvidenceVaultOperationPlanError(
            "classification and relation worksets must match"
        )
    if plan.get("mode") == "incremental_refresh" and operations[
        "reevaluate_tile_ids"
    ] != sorted(delta.get("affected_tile_ids") or []):
        raise EvidenceVaultOperationPlanError(
            "incremental tile workset differs from the frozen delta"
        )
    if operations["recalculate_canonical_score"] is not False:
        raise EvidenceVaultOperationPlanError(
            "scan plan cannot recalculate canonical score"
        )
    if operations["create_canonical_report"] is not False:
        raise EvidenceVaultOperationPlanError(
            "scan plan cannot create a canonical report"
        )
    exit_contract = plan.get("exit_contract")
    if exit_contract != {
        "canonical_memory_changes": False,
        "canonical_score_changes": False,
        "promotion_required_for_canonical_change": True,
    }:
        raise EvidenceVaultOperationPlanError("operation plan exit contract mismatch")
    unsigned = {
        key: plan[key]
        for key in fields
        if key != "operation_plan_fingerprint"
    }
    expected = canonical_fingerprint(
        EVIDENCE_VAULT_OPERATION_PLAN_VERSION,
        unsigned,
    )
    if plan.get("operation_plan_fingerprint") != expected:
        raise EvidenceVaultOperationPlanError(
            "operation plan fingerprint mismatch"
        )


def build_incremental_evidence_delta(
    *,
    subject_url: str,
    current_evidence_records: Iterable[Mapping[str, Any]],
    previous_capture_evidence_records: Iterable[Mapping[str, Any]],
    known_evidence_records: Iterable[Mapping[str, Any]] = (),
    accepted_evidence_tile_relations: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Compare acquisition with the last capture and durable known evidence.

    Missing evidence is recorded as coverage loss only.  It is never treated as
    removal, refutation, or a reason to reopen accepted tiles automatically.
    """

    current = canonical_evidence_rows(
        [dict(row) for row in current_evidence_records if isinstance(row, Mapping)],
        subject_url=subject_url,
    )
    previous = canonical_evidence_rows(
        [
            dict(row)
            for row in previous_capture_evidence_records
            if isinstance(row, Mapping)
        ],
        subject_url=subject_url,
    )
    known = canonical_evidence_rows(
        [dict(row) for row in known_evidence_records if isinstance(row, Mapping)],
        subject_url=subject_url,
    )
    current_by_fingerprint = _by_fingerprint(current)
    previous_by_fingerprint = _by_fingerprint(previous)
    known_by_fingerprint = _by_fingerprint(known)
    previous_by_locator = _fingerprints_by_locator(previous)
    known_by_locator = _fingerprints_by_locator(
        tuple(known_by_fingerprint.values())
    )

    unchanged_fingerprints = (
        set(current_by_fingerprint) & set(previous_by_fingerprint)
    )
    unanalysed_unchanged = sorted(
        unchanged_fingerprints - set(known_by_fingerprint)
    )
    unchanged = sorted(
        unchanged_fingerprints & set(known_by_fingerprint)
    )
    reacquired = sorted(
        (set(current_by_fingerprint) & set(known_by_fingerprint))
        - set(previous_by_fingerprint)
    )
    unseen = (
        set(current_by_fingerprint)
        - set(known_by_fingerprint)
        - set(previous_by_fingerprint)
    )
    modified: set[str] = set()
    added: set[str] = set()
    modified_locators: set[str] = set()
    for fingerprint in unseen:
        locator = current_by_fingerprint[fingerprint].locator
        if locator in previous_by_locator or locator in known_by_locator:
            modified.add(fingerprint)
            modified_locators.add(locator)
        else:
            added.add(fingerprint)
    superseded_previous = {
        fingerprint
        for locator in modified_locators
        for fingerprint in previous_by_locator.get(locator, ())
        if fingerprint not in current_by_fingerprint
    }
    not_reacquired = sorted(
        set(previous_by_fingerprint)
        - set(current_by_fingerprint)
        - superseded_previous
    )

    relation_tiles = _relation_tiles(
        accepted_evidence_tile_relations,
        known_fingerprints=(
            set(known_by_fingerprint) | set(previous_by_fingerprint)
        ),
        known_locators=(
            set(known_by_locator) | set(previous_by_locator)
        ),
    )
    affected_tiles: set[str] = set()
    for locator in modified_locators:
        affected_tiles.update(relation_tiles["by_locator"].get(locator, ()))
        for old_fingerprint in known_by_locator.get(locator, ()):
            affected_tiles.update(
                relation_tiles["by_fingerprint"].get(old_fingerprint, ())
            )

    payload = {
        "schema_version": EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION,
        "current_evidence_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION,
            _record_payload(current),
        ),
        "previous_capture_evidence_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION,
            _record_payload(previous),
        ),
        "known_evidence_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION,
            _record_payload(tuple(known_by_fingerprint.values())),
        ),
        "unchanged_evidence_fingerprints": unchanged,
        "unanalysed_unchanged_evidence_fingerprints": unanalysed_unchanged,
        "reacquired_evidence_fingerprints": reacquired,
        "added_evidence_fingerprints": sorted(added),
        "modified_evidence_fingerprints": sorted(modified),
        "superseded_evidence_fingerprints": sorted(superseded_previous),
        "modified_locators": sorted(modified_locators),
        "not_reacquired_evidence_fingerprints": not_reacquired,
        "affected_tile_ids": sorted(affected_tiles),
        "summary": {
            "current_count": len(current),
            "previous_capture_count": len(previous),
            "known_count": len(known_by_fingerprint),
            "unchanged_count": len(unchanged),
            "unanalysed_unchanged_count": len(unanalysed_unchanged),
            "reacquired_count": len(reacquired),
            "added_count": len(added),
            "modified_count": len(modified),
            "superseded_count": len(superseded_previous),
            "not_reacquired_count": len(not_reacquired),
            "affected_tile_count": len(affected_tiles),
        },
        "coverage_loss_only": bool(not_reacquired) and not (
            added or modified or unanalysed_unchanged
        ),
        "requires_incremental_analysis": bool(
            added or modified or unanalysed_unchanged
        ),
    }
    return {
        **payload,
        "delta_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION,
            payload,
        ),
    }


def _baseline_delta(records: tuple[CanonicalEvidenceRecord, ...]) -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION,
        "classification": "baseline_input",
        "evidence_fingerprints": [row.fingerprint for row in records],
        "evidence_count": len(records),
    }


def _diagnostic_delta(records: tuple[CanonicalEvidenceRecord, ...]) -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION,
        "classification": "diagnostic_full_input",
        "evidence_fingerprints": [row.fingerprint for row in records],
        "evidence_count": len(records),
    }


def _relation_tiles(
    relations: Iterable[Mapping[str, Any]],
    *,
    known_fingerprints: set[str],
    known_locators: set[str],
) -> dict[str, dict[str, set[str]]]:
    valid_tiles = set(_all_tile_ids())
    by_fingerprint: dict[str, set[str]] = {}
    by_locator: dict[str, set[str]] = {}
    for raw in relations:
        if not isinstance(raw, Mapping):
            raise EvidenceVaultOperationPlanError("evidence relation must be an object")
        tile_id = str(raw.get("tile_id") or "").strip()
        if tile_id not in valid_tiles:
            raise EvidenceVaultOperationPlanError(
                f"unknown tile in evidence relation: {tile_id}"
            )
        fingerprint = str(raw.get("evidence_fingerprint") or "").strip()
        locator = str(raw.get("evidence_locator") or "").strip()
        if bool(fingerprint) == bool(locator):
            raise EvidenceVaultOperationPlanError(
                "evidence relation requires exactly one fingerprint or locator"
            )
        if fingerprint:
            if fingerprint not in known_fingerprints:
                raise EvidenceVaultOperationPlanError(
                    "evidence relation fingerprint is not in known evidence"
                )
            by_fingerprint.setdefault(fingerprint, set()).add(tile_id)
        else:
            if locator not in known_locators:
                raise EvidenceVaultOperationPlanError(
                    "evidence relation locator is not in known evidence"
                )
            by_locator.setdefault(locator, set()).add(tile_id)
    return {"by_fingerprint": by_fingerprint, "by_locator": by_locator}


def _record_payload(
    records: Iterable[CanonicalEvidenceRecord],
) -> list[dict[str, Any]]:
    return [
        {
            **record.public_dict(),
            "normalized_content": record.normalized_content,
        }
        for record in sorted(records, key=lambda row: (row.locator, row.fingerprint))
    ]


def _labelable_fingerprints(
    rows: Iterable[Mapping[str, Any]],
    *,
    subject_url: str,
) -> set[str]:
    representatives = canonical_evidence_representatives(
        rows,
        subject_url=subject_url,
    )
    result: set[str] = set()
    for fingerprint, row in representatives.items():
        record = EvidenceRecord(
            ref=str(row.get("ref") or ""),
            source=str(row.get("source") or "unknown"),
            evidence_type=str(row.get("evidence_type") or "unknown"),
            content=str(row.get("content") or ""),
            url=str(row.get("url") or "") or None,
            confidence=str(row.get("confidence") or "medium"),
            metadata=dict(row.get("metadata") or {}),
        )
        if is_evidence_record_labelable(record):
            result.add(fingerprint)
    return result


def _by_fingerprint(
    records: Iterable[CanonicalEvidenceRecord],
) -> dict[str, CanonicalEvidenceRecord]:
    return {record.fingerprint: record for record in records}


def _fingerprints_by_locator(
    records: Iterable[CanonicalEvidenceRecord],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for record in records:
        result.setdefault(record.locator, set()).add(record.fingerprint)
    return result


def _all_tile_ids() -> list[str]:
    return [str(row["tile_id"]) for row in build_tile_contract_registry()["tiles"]]


def _optional_fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise EvidenceVaultOperationPlanError(
            "canonical_memory_version must be a lowercase sha256 fingerprint"
        )
    return normalized


__all__ = [
    "EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION",
    "EVIDENCE_VAULT_OPERATION_PLAN_VERSION",
    "EvidenceVaultOperationPlanError",
    "VaultScanMode",
    "build_incremental_evidence_delta",
    "build_vault_scan_plan",
    "resolve_vault_scan_mode",
    "validate_vault_scan_plan",
]
