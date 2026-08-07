"""Fail-closed lifecycle for accepted Evidence Vault ``all_of`` groups.

The module is pure and Vault-scoped.  It converts a durable material evidence
change (or an accepted contradictory relation) into an immutable group-reopen
artifact and a pending candidate packet.  It never treats acquisition absence
as a semantic change and never grants authority to a replacement group.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping
from uuid import UUID

from src.services.evidence_vault_canonical_core import (
    EvidenceVaultCanonicalCoreError,
    TileState,
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_tile_contract_registry,
    canonical_fingerprint,
    validate_candidate_packet,
)
from src.services.evidence_vault_exact_relation_supplement import (
    EvidenceVaultExactRelationSupplementError,
    validate_exact_relation_supplement_structure,
)
from src.services.evidence_vault_incremental_refresh import (
    EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION,
)
from src.services.evidence_vault_operational_memory import (
    EvidenceVaultOperationalMemoryError,
    build_operational_memory_packet,
)
from src.services.evidence_vault_operational_scoring import (
    EvidenceVaultOperationalScoringError,
    build_operational_score_evaluation,
)


EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_VERSION = (
    "evidence-vault-composite-group-reopen-v1"
)
EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_SOURCE_RESOLUTION_VERSION = (
    "evidence-vault-composite-group-reopen-source-resolution-v1"
)
EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_SOURCE_VERSION = (
    "evidence-vault-composite-group-reopen-source-v1"
)
COMPOSITE_GROUP_REOPEN_POLICY_ACTOR = "composite-group-reopen-policy-v1"


class EvidenceVaultCompositeGroupLifecycleError(ValueError):
    """An ``all_of`` group cannot be reopened from the supplied durable state."""


def build_composite_group_reopen_artifact(
    *,
    current_operational_memory: Mapping[str, Any],
    exact_source_record: Mapping[str, Any],
    reopen_event_id: str,
    evidence_delta: Mapping[str, Any] | None = None,
    accepted_contradiction: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind one whole-group reopen to its exact accepted group and trigger.

    Exactly one trigger is required.  A material delta must supersede at least
    one frozen member fingerprint.  ``not_reacquired`` never qualifies.
    """

    current, source, resolution, group, accepted_members = _resolved_active_group(
        current_operational_memory=current_operational_memory,
        exact_source_record=exact_source_record,
    )
    event_id = _uuid(reopen_event_id, field="reopen_event_id")
    if (evidence_delta is None) == (accepted_contradiction is None):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "exactly one composite-group reopen trigger is required"
        )
    if evidence_delta is not None:
        trigger = _material_change_trigger(evidence_delta, group=group)
    else:
        trigger = _accepted_contradiction_trigger(
            accepted_contradiction,
            tile_id=str(group["tile_id"]),
            member_relations=group["relations"],
        )

    member_bindings = []
    accepted_by_id = {
        str(row["relation_id"]): row for row in accepted_members
    }
    for relation in group["relations"]:
        accepted = accepted_by_id[str(relation["relation_id"])]
        member_bindings.append(
            {
                "relation_id": str(relation["relation_id"]),
                "evidence_fingerprint": str(relation["evidence_fingerprint"]),
                "evidence_id": str(relation["evidence_id"]),
                "source_identity_id": str(relation["source_identity_id"]),
                "claim_id": str(relation["claim_id"]),
                "channel_role": str(relation["channel_role"]),
                "ref": str(relation["ref"]),
                "url": str(relation["url"]),
                "accepted_decision_event_id": str(
                    accepted["decision_event_id"]
                ),
            }
        )
    member_bindings.sort(key=lambda row: row["relation_id"])
    unsigned = {
        "schema_version": EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_VERSION,
        "brand_identity": str(current["brand_identity"]),
        "parent_canonical_memory_version": str(
            current["canonical_memory_version"]
        ),
        "source_candidate_packet_fingerprint": str(
            source["candidate_packet_fingerprint"]
        ),
        "source_artifact_fingerprint": str(
            resolution["artifact_fingerprint"]
        ),
        "group_id": str(group["group_id"]),
        "tile_id": str(group["tile_id"]),
        "decision_rule": "all_of",
        "group_contract": deepcopy(dict(group["group_contract"])),
        "member_bindings": member_bindings,
        "trigger": trigger,
        "reopen_event_id": event_id,
        "lifecycle_transition": "group_reopened",
        "prior_group_lifecycle_state": "accepted",
        "current_group_lifecycle_state": "pending_reassessment",
        "score_eligible": False,
        "replacement_authority_granted": False,
        "authority": False,
        "authority_scope": "b3s-vault",
        "runtime_effect": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    artifact = {
        **unsigned,
        "artifact_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_VERSION,
            unsigned,
        ),
    }
    validate_composite_group_reopen_artifact(
        artifact,
        current_operational_memory=current,
        exact_source_record=exact_source_record,
    )
    return artifact


def validate_composite_group_reopen_artifact(
    artifact: Mapping[str, Any],
    *,
    current_operational_memory: Mapping[str, Any],
    exact_source_record: Mapping[str, Any],
) -> None:
    """Re-derive the immutable transition against its exact parent and source."""

    if not isinstance(artifact, Mapping):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen artifact must be an object"
        )
    unsigned = {key: deepcopy(value) for key, value in artifact.items() if key != "artifact_fingerprint"}
    if artifact.get("schema_version") != EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_VERSION:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen schema mismatch"
        )
    if artifact.get("artifact_fingerprint") != canonical_fingerprint(
        EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_VERSION,
        unsigned,
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen fingerprint mismatch"
        )
    current, source, resolution, group, accepted_members = _resolved_active_group(
        current_operational_memory=current_operational_memory,
        exact_source_record=exact_source_record,
    )
    if (
        artifact.get("brand_identity") != current["brand_identity"]
        or artifact.get("parent_canonical_memory_version")
        != current["canonical_memory_version"]
        or artifact.get("source_candidate_packet_fingerprint")
        != source["candidate_packet_fingerprint"]
        or artifact.get("source_artifact_fingerprint")
        != resolution["artifact_fingerprint"]
        or artifact.get("group_id") != group["group_id"]
        or artifact.get("tile_id") != group["tile_id"]
        or artifact.get("decision_rule") != "all_of"
        or artifact.get("group_contract") != group["group_contract"]
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen lineage mismatch"
        )
    if (
        artifact.get("lifecycle_transition") != "group_reopened"
        or artifact.get("prior_group_lifecycle_state") != "accepted"
        or artifact.get("current_group_lifecycle_state")
        != "pending_reassessment"
        or artifact.get("score_eligible") is not False
        or artifact.get("replacement_authority_granted") is not False
        or artifact.get("authority") is not False
        or artifact.get("authority_scope") != "b3s-vault"
        or artifact.get("runtime_effect") is not False
        or artifact.get("production_runtime_effect") is not False
        or artifact.get("scanner_runtime_effect") is not False
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen authority boundary mismatch"
        )
    _uuid(artifact.get("reopen_event_id"), field="reopen_event_id")
    expected_members = []
    accepted_by_id = {
        str(row["relation_id"]): row for row in accepted_members
    }
    for relation in group["relations"]:
        accepted = accepted_by_id[str(relation["relation_id"])]
        expected_members.append(
            {
                "relation_id": str(relation["relation_id"]),
                "evidence_fingerprint": str(relation["evidence_fingerprint"]),
                "evidence_id": str(relation["evidence_id"]),
                "source_identity_id": str(relation["source_identity_id"]),
                "claim_id": str(relation["claim_id"]),
                "channel_role": str(relation["channel_role"]),
                "ref": str(relation["ref"]),
                "url": str(relation["url"]),
                "accepted_decision_event_id": str(
                    accepted["decision_event_id"]
                ),
            }
        )
    expected_members.sort(key=lambda row: row["relation_id"])
    if artifact.get("member_bindings") != expected_members:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen member bindings mismatch"
        )
    trigger = artifact.get("trigger")
    if not isinstance(trigger, Mapping):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen trigger is missing"
        )
    kind = trigger.get("kind")
    if kind == "material_member_change":
        _validate_material_trigger(trigger, group=group)
    elif kind == "accepted_contradiction":
        _validate_contradiction_trigger(trigger, tile_id=str(group["tile_id"]))
        matching_members = [
            row
            for row in group["relations"]
            if row.get("source_identity_id")
            == trigger.get("source_identity_id")
        ]
        if (
            len(matching_members) != 1
            or trigger.get("affected_member_evidence_fingerprints")
            != [matching_members[0]["evidence_fingerprint"]]
        ):
            raise EvidenceVaultCompositeGroupLifecycleError(
                "accepted contradiction member binding drifted"
            )
    else:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "unsupported composite-group reopen trigger"
        )
    affected = set(
        trigger.get("affected_member_evidence_fingerprints") or []
    )
    member_fingerprints = {
        str(row["evidence_fingerprint"]) for row in expected_members
    }
    if not affected or not affected.issubset(member_fingerprints):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group trigger is not bound to an accepted member"
        )


def build_composite_group_reopen_source_candidate(
    artifact: Mapping[str, Any],
    *,
    current_operational_memory: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a pending 80-tile source that supersedes the whole old group."""

    current = deepcopy(dict(current_operational_memory))
    _validate_current_memory(current)
    _validate_artifact_self(artifact, current=current)
    content = current["content"]
    registry = build_tile_contract_registry()["tiles"]
    current_by_id = {
        str(row["tile_id"]): dict(row) for row in content["accepted_tiles"]
    }
    previous = [
        build_candidate_tile(
            tile_id=str(contract["tile_id"]),
            basis=(current_by_id.get(str(contract["tile_id"])) or {}).get("basis")
            or [],
            coverage_refs=(current_by_id.get(str(contract["tile_id"])) or {}).get(
                "coverage_refs"
            )
            or [],
            unresolved_refs=(current_by_id.get(str(contract["tile_id"])) or {}).get(
                "unresolved_refs"
            )
            or [],
        )
        for contract in registry
    ]
    previous_by_id = {str(row["tile_id"]): row for row in previous}
    tile_id = str(artifact["tile_id"])
    prior = previous_by_id[tile_id]
    member_ids = {
        str(row["relation_id"]) for row in artifact["member_bindings"]
    }
    prior_ids = {str(row["relation_id"]) for row in prior["basis"]}
    if not member_ids.issubset(prior_ids):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "active composite group no longer matches the reopen parent"
        )
    retained = [
        dict(row) for row in prior["basis"] if str(row["relation_id"]) not in member_ids
    ]
    invalidations = [
        _invalidation_relation(artifact, member)
        for member in artifact["member_bindings"]
    ]
    unresolved_ref = f"composite-group-reopen:{artifact['artifact_fingerprint']}"
    updates = [
        {
            "tile_id": tile_id,
            "delta_kind": "verified_deprecation",
            "basis": sorted(
                [*retained, *invalidations],
                key=lambda row: str(row["relation_id"]),
            ),
            "coverage_refs": prior["coverage_refs"],
            "unresolved_refs": sorted(
                set(prior["unresolved_refs"]) | {unresolved_ref}
            ),
        }
    ]
    try:
        candidates = build_incremental_candidate_tiles(
            previous_candidate_tiles=previous,
            tile_updates=updates,
        )
    except EvidenceVaultCanonicalCoreError as exc:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen cannot evolve its exact parent"
        ) from exc
    reopened = next(row for row in candidates if row["tile_id"] == tile_id)
    if any(str(row.get("relation_id")) in member_ids for row in reopened["basis"]):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen left an old member active"
        )
    lifecycle_summary = {
        "source": "composite_group_reopen",
        "artifact_fingerprint": artifact["artifact_fingerprint"],
        "trigger_fingerprint": artifact["trigger"]["trigger_fingerprint"],
        "reopened_tile_ids": [tile_id],
        "reopened_groups": [
            {
                "group_id": artifact["group_id"],
                "tile_id": tile_id,
                "decision_rule": "all_of",
                "member_relation_ids": sorted(member_ids),
                "lifecycle_transition": "group_reopened",
                "current_group_lifecycle_state": "pending_reassessment",
            }
        ],
    }
    packet = build_candidate_packet(
        brand_identity=str(artifact["brand_identity"]),
        parent_canonical_memory_version=str(
            artifact["parent_canonical_memory_version"]
        ),
        candidate_memory_version=canonical_fingerprint(
            EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_SOURCE_VERSION,
            {
                "artifact_fingerprint": artifact["artifact_fingerprint"],
                "parent_canonical_memory_version": artifact[
                    "parent_canonical_memory_version"
                ],
            },
        ),
        accepted_memory_candidate_version=canonical_fingerprint(
            "evidence-vault-composite-group-reopen-accepted-parent-v1",
            content,
        ),
        reviewed_memory_candidate_version=canonical_fingerprint(
            "evidence-vault-composite-group-reopen-reviewed-memory-v1",
            lifecycle_summary,
        ),
        review_packet_set_fingerprint=canonical_fingerprint(
            "evidence-vault-composite-group-reopen-review-set-v1",
            {
                "group_id": artifact["group_id"],
                "reopen_event_id": artifact["reopen_event_id"],
            },
        ),
        aggregation_policy_fingerprint=str(
            content["aggregation_policy_fingerprint"]
        ),
        candidate_tiles=candidates,
        coverage_summary=lifecycle_summary,
        unresolved_items=[
            {
                "unresolved_id": unresolved_ref,
                "kind": "composite_group_reassessment",
                "blocking": True,
                "details": {
                    "group_id": artifact["group_id"],
                    "tile_id": tile_id,
                },
            }
        ],
    )
    validate_candidate_packet(packet)
    return packet


def build_composite_group_reopen_operational_packet(
    artifact: Mapping[str, Any],
    *,
    source_candidate_packet: Mapping[str, Any],
    current_operational_memory: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove current tile authority while keeping reassessment pending."""

    source = deepcopy(dict(source_candidate_packet))
    validate_candidate_packet(source)
    current = deepcopy(dict(current_operational_memory))
    _validate_current_memory(current)
    _validate_artifact_self(artifact, current=current)
    coverage = source["manifest"].get("coverage_summary") or {}
    if (
        coverage.get("source") != "composite_group_reopen"
        or coverage.get("artifact_fingerprint")
        != artifact.get("artifact_fingerprint")
        or source["manifest"].get("parent_canonical_memory_version")
        != current.get("canonical_memory_version")
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen operational source mismatch"
        )
    tile_id = str(artifact["tile_id"])
    try:
        return build_operational_memory_packet(
            brand_identity=str(artifact["brand_identity"]),
            source_candidate_packet_fingerprint=source[
                "candidate_packet_fingerprint"
            ],
            aggregation_policy_fingerprint=str(
                current["content"]["aggregation_policy_fingerprint"]
            ),
            candidate_tiles=source["candidate_tiles"],
            dispositions={
                tile_id: {
                    "authority_state": "pending",
                    "review_state": "required",
                    "authority_profile_id": "composite-group-reassessment-v1",
                    "authority_source": "none",
                    "decision_event_id": None,
                }
            },
            current_accepted_tiles=current["content"]["accepted_tiles"],
            parent_canonical_memory_version=current[
                "canonical_memory_version"
            ],
            reopened_tile_ids=[tile_id],
            reopen_policy_fingerprint=artifact["artifact_fingerprint"],
            reopened_group_contexts=[
                {
                    "tile_id": tile_id,
                    "prior_group_id": artifact["group_id"],
                    "trigger_fingerprint": artifact["trigger"][
                        "trigger_fingerprint"
                    ],
                    "superseded_member_evidence_fingerprints": (
                        artifact["trigger"][
                            "affected_member_evidence_fingerprints"
                        ]
                    ),
                }
            ],
            current_pending_reassessments=current["content"].get(
                "pending_reassessments"
            )
            or [],
        )
    except EvidenceVaultOperationalMemoryError as exc:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen operational projection is invalid"
        ) from exc


def build_composite_group_reopen_source_resolution(
    artifact: Mapping[str, Any],
    *,
    source_candidate_packet: Mapping[str, Any],
    durable_trigger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze the transition artifact behind its pending source packet."""

    source = deepcopy(dict(source_candidate_packet))
    validate_candidate_packet(source)
    coverage = source["manifest"].get("coverage_summary") or {}
    if (
        coverage.get("source") != "composite_group_reopen"
        or coverage.get("artifact_fingerprint")
        != artifact.get("artifact_fingerprint")
        or source["manifest"].get("brand_identity")
        != artifact.get("brand_identity")
        or source["manifest"].get("parent_canonical_memory_version")
        != artifact.get("parent_canonical_memory_version")
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen source lineage mismatch"
        )
    normalized_trigger = _durable_trigger(durable_trigger)
    return {
        "schema_version": (
            EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_SOURCE_RESOLUTION_VERSION
        ),
        "source_kind": "composite_group_reopen",
        "artifact_fingerprint": artifact["artifact_fingerprint"],
        "source_candidate_packet_fingerprint": source[
            "candidate_packet_fingerprint"
        ],
        "parent_canonical_memory_version": artifact[
            "parent_canonical_memory_version"
        ],
        "prior_group_source_candidate_packet_fingerprint": artifact[
            "source_candidate_packet_fingerprint"
        ],
        "group_id": artifact["group_id"],
        "tile_id": artifact["tile_id"],
        "trigger_fingerprint": artifact["trigger"]["trigger_fingerprint"],
        "reopen_event_id": artifact["reopen_event_id"],
        "durable_trigger": normalized_trigger,
        "artifact": deepcopy(dict(artifact)),
    }


def validate_composite_group_reopen_source_resolution(
    resolution: Mapping[str, Any],
    *,
    source_candidate_packet: Mapping[str, Any],
) -> None:
    if (
        not isinstance(resolution, Mapping)
        or resolution.get("schema_version")
        != EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_SOURCE_RESOLUTION_VERSION
        or resolution.get("source_kind") != "composite_group_reopen"
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen source resolution is invalid"
        )
    artifact = resolution.get("artifact")
    if not isinstance(artifact, Mapping):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen resolution artifact is missing"
        )
    _validate_artifact_fingerprint(artifact)
    source = deepcopy(dict(source_candidate_packet))
    validate_candidate_packet(source)
    coverage = source["manifest"].get("coverage_summary") or {}
    expected = {
        "artifact_fingerprint": artifact["artifact_fingerprint"],
        "source_candidate_packet_fingerprint": source[
            "candidate_packet_fingerprint"
        ],
        "parent_canonical_memory_version": artifact[
            "parent_canonical_memory_version"
        ],
        "prior_group_source_candidate_packet_fingerprint": artifact[
            "source_candidate_packet_fingerprint"
        ],
        "group_id": artifact["group_id"],
        "tile_id": artifact["tile_id"],
        "trigger_fingerprint": artifact["trigger"]["trigger_fingerprint"],
        "reopen_event_id": artifact["reopen_event_id"],
    }
    if any(resolution.get(field) != value for field, value in expected.items()):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen resolution lineage mismatch"
        )
    _durable_trigger(resolution.get("durable_trigger"))
    if (
        coverage.get("source") != "composite_group_reopen"
        or coverage.get("artifact_fingerprint")
        != artifact["artifact_fingerprint"]
        or coverage.get("reopened_tile_ids") != [artifact["tile_id"]]
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen source coverage mismatch"
        )


def _resolved_active_group(
    *,
    current_operational_memory: Mapping[str, Any],
    exact_source_record: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    current = deepcopy(dict(current_operational_memory))
    _validate_current_memory(current)
    if not isinstance(exact_source_record, Mapping):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "exact group source record is required"
        )
    source = deepcopy(dict(exact_source_record.get("packet") or {}))
    resolution = deepcopy(
        dict(exact_source_record.get("reference_resolution") or {})
    )
    try:
        validate_candidate_packet(source)
    except EvidenceVaultCanonicalCoreError as exc:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "exact group source packet is invalid"
        ) from exc
    exact_artifact = resolution.get("artifact")
    try:
        if (
            resolution.get("source_kind") != "exact_relation_supplement"
            or resolution.get("source_candidate_packet_fingerprint")
            != source["candidate_packet_fingerprint"]
            or not isinstance(exact_artifact, Mapping)
        ):
            raise EvidenceVaultExactRelationSupplementError(
                "exact source resolution mismatch"
            )
        validate_exact_relation_supplement_structure(exact_artifact)
    except EvidenceVaultExactRelationSupplementError as exc:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "exact group source resolution is invalid"
        ) from exc
    if exact_artifact.get("brand_identity") != current["brand_identity"]:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "exact group source belongs to another brand"
        )
    groups = [
        dict(row)
        for row in exact_artifact.get("groups") or []
        if isinstance(row, Mapping)
        and row.get("decision_rule") == "all_of"
        and row.get("tile_id") == "C7"
    ]
    if len(groups) != 1:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "exact source must resolve one C7 all-of group"
        )
    group = groups[0]
    contract = group.get("group_contract")
    if (
        not isinstance(contract, Mapping)
        or contract.get("member_change_requires_review") is not True
        or contract.get("member_decisions_must_match") is not True
        or contract.get("required_channel_roles")
        != ["external_social_profile", "owned_web"]
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "C7 all-of lifecycle contract is incomplete"
        )
    content = current["content"]
    accepted_tile = next(
        (
            dict(row)
            for row in content["accepted_tiles"]
            if row.get("tile_id") == group["tile_id"]
        ),
        None,
    )
    if accepted_tile is None or accepted_tile.get("semantic_state") != "ok":
        raise EvidenceVaultCompositeGroupLifecycleError(
            "C7 all-of group is not active in accepted memory"
        )
    group_member_ids = {
        str(row["relation_id"]) for row in group["relations"]
    }
    accepted_tile_rows = [
        dict(row) for row in accepted_tile.get("basis") or []
    ]
    accepted_group_rows = [
        row
        for row in accepted_tile_rows
        if row.get("claim_id") == group["group_id"]
    ]
    if (
        {
            str(row.get("relation_id") or "")
            for row in accepted_tile_rows
        }
        != group_member_ids
        or {
            str(row.get("relation_id") or "")
            for row in accepted_group_rows
        }
        != group_member_ids
        or len(accepted_group_rows) != len(group_member_ids)
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "accepted C7 group is partial or has drifted membership"
        )
    artifact_by_relation = {
        str(row["relation_id"]): row for row in group["relations"]
    }
    for row in accepted_group_rows:
        frozen = artifact_by_relation[str(row["relation_id"])]
        if (
            row.get("evidence_id") != frozen["evidence_id"]
            or row.get("source_identity_id") != frozen["source_identity_id"]
            or row.get("claim_id") != group["group_id"]
            or row.get("polarity") != "supports"
            or row.get("review_status") != "accepted"
            or not str(row.get("decision_event_id") or "").strip()
        ):
            raise EvidenceVaultCompositeGroupLifecycleError(
                "accepted C7 group authority cannot be rederived"
            )
    return current, source, resolution, group, accepted_group_rows


def _validate_current_memory(current: Mapping[str, Any]) -> None:
    try:
        build_operational_score_evaluation(
            current,
            created_at="2000-01-01T00:00:00+00:00",
        )
    except EvidenceVaultOperationalScoringError as exc:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen parent memory is invalid"
        ) from exc
    if (
        current.get("authority") is not True
        or current.get("authority_scope") != "b3s-vault"
        or current.get("lifecycle_state") != "active"
        or current.get("production_runtime_effect") is not False
        or current.get("scanner_runtime_effect") is not False
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen parent is not active Vault memory"
        )


def _material_change_trigger(
    evidence_delta: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
) -> dict[str, Any]:
    delta = deepcopy(dict(evidence_delta))
    _validate_delta_fingerprint(delta)
    member_fingerprints = {
        str(row["evidence_fingerprint"]) for row in group["relations"]
    }
    superseded = {
        _sha256(value, field="superseded_evidence_fingerprint")
        for value in delta.get("superseded_evidence_fingerprints") or []
    }
    affected = sorted(member_fingerprints & superseded)
    if not affected:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "delta does not materially supersede an accepted group member"
        )
    trigger = {
        "kind": "material_member_change",
        "delta_fingerprint": delta["delta_fingerprint"],
        "current_evidence_fingerprint": delta["current_evidence_fingerprint"],
        "previous_capture_evidence_fingerprint": delta[
            "previous_capture_evidence_fingerprint"
        ],
        "known_evidence_fingerprint": delta["known_evidence_fingerprint"],
        "affected_member_evidence_fingerprints": affected,
        "modified_evidence_fingerprints": list(
            delta.get("modified_evidence_fingerprints") or []
        ),
        "superseded_evidence_fingerprints": list(
            delta.get("superseded_evidence_fingerprints") or []
        ),
        "modified_locators": list(delta.get("modified_locators") or []),
    }
    return {
        **trigger,
        "trigger_fingerprint": canonical_fingerprint(
            "evidence-vault-composite-group-material-change-trigger-v1",
            trigger,
        ),
    }


def _validate_material_trigger(
    trigger: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
) -> None:
    required = {
        "kind",
        "delta_fingerprint",
        "current_evidence_fingerprint",
        "previous_capture_evidence_fingerprint",
        "known_evidence_fingerprint",
        "affected_member_evidence_fingerprints",
        "modified_evidence_fingerprints",
        "superseded_evidence_fingerprints",
        "modified_locators",
        "trigger_fingerprint",
    }
    if set(trigger) != required:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "material group trigger fields mismatch"
        )
    unsigned = {key: deepcopy(value) for key, value in trigger.items() if key != "trigger_fingerprint"}
    if trigger.get("trigger_fingerprint") != canonical_fingerprint(
        "evidence-vault-composite-group-material-change-trigger-v1",
        unsigned,
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "material group trigger fingerprint mismatch"
        )
    member_fingerprints = {
        str(row["evidence_fingerprint"]) for row in group["relations"]
    }
    affected = trigger.get("affected_member_evidence_fingerprints")
    superseded = trigger.get("superseded_evidence_fingerprints")
    if (
        not isinstance(affected, list)
        or affected != sorted(set(affected))
        or not isinstance(superseded, list)
        or set(affected) != member_fingerprints & set(superseded)
        or not affected
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "material group trigger does not identify changed members"
        )
    for field in (
        "delta_fingerprint",
        "current_evidence_fingerprint",
        "previous_capture_evidence_fingerprint",
        "known_evidence_fingerprint",
    ):
        _sha256(trigger.get(field), field=field)


def _accepted_contradiction_trigger(
    value: Mapping[str, Any] | None,
    *,
    tile_id: str,
    member_relations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    trigger = deepcopy(dict(value or {}))
    _validate_contradiction_trigger(
        trigger,
        tile_id=tile_id,
        require_fingerprint=False,
    )
    member_matches = [
        row
        for row in member_relations
        if row.get("source_identity_id") == trigger.get("source_identity_id")
    ]
    if len(member_matches) != 1:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "accepted contradiction does not identify exactly one group member"
        )
    unsigned = {
        key: deepcopy(item)
        for key, item in trigger.items()
        if key != "trigger_fingerprint"
    }
    unsigned["kind"] = "accepted_contradiction"
    unsigned["affected_member_evidence_fingerprints"] = [
        str(member_matches[0]["evidence_fingerprint"])
    ]
    return {
        **unsigned,
        "trigger_fingerprint": canonical_fingerprint(
            "evidence-vault-composite-group-accepted-contradiction-trigger-v1",
            unsigned,
        ),
    }


def _validate_contradiction_trigger(
    trigger: Mapping[str, Any],
    *,
    tile_id: str,
    require_fingerprint: bool = True,
) -> None:
    fields = {
        "kind",
        "source_candidate_packet_fingerprint",
        "relation_id",
        "tile_id",
        "evidence_id",
        "source_identity_id",
        "decision_event_id",
        "review_request_fingerprint",
        "decision",
        "polarity",
    }
    expected_fields = fields | (
        {
            "trigger_fingerprint",
            "affected_member_evidence_fingerprints",
        }
        if require_fingerprint
        else set()
    )
    if set(trigger) != expected_fields:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "accepted contradiction trigger fields mismatch"
        )
    if (
        trigger.get("kind") != "accepted_contradiction"
        or trigger.get("tile_id") != tile_id
        or trigger.get("decision") != "accept"
        or trigger.get("polarity") != "contradicts"
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "accepted contradiction cannot reopen this group"
        )
    for field in (
        "source_candidate_packet_fingerprint",
        "relation_id",
        "evidence_id",
        "source_identity_id",
        "review_request_fingerprint",
    ):
        _sha256(trigger.get(field), field=field)
    if not str(trigger.get("decision_event_id") or "").strip():
        raise EvidenceVaultCompositeGroupLifecycleError(
            "accepted contradiction requires a decision event"
        )
    if require_fingerprint:
        affected = trigger.get("affected_member_evidence_fingerprints")
        if not isinstance(affected, list) or len(affected) != 1:
            raise EvidenceVaultCompositeGroupLifecycleError(
                "accepted contradiction member binding is invalid"
            )
        _sha256(
            affected[0],
            field="affected_member_evidence_fingerprint",
        )
        unsigned = {
            key: deepcopy(value)
            for key, value in trigger.items()
            if key != "trigger_fingerprint"
        }
        if trigger.get("trigger_fingerprint") != canonical_fingerprint(
            "evidence-vault-composite-group-accepted-contradiction-trigger-v1",
            unsigned,
        ):
            raise EvidenceVaultCompositeGroupLifecycleError(
                "accepted contradiction trigger fingerprint mismatch"
            )


def _validate_delta_fingerprint(delta: Mapping[str, Any]) -> None:
    if delta.get("schema_version") != EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "incremental delta schema mismatch"
        )
    unsigned = {key: deepcopy(value) for key, value in delta.items() if key != "delta_fingerprint"}
    if delta.get("delta_fingerprint") != canonical_fingerprint(
        EVIDENCE_VAULT_INCREMENTAL_DELTA_VERSION,
        unsigned,
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "incremental delta fingerprint mismatch"
        )
    if delta.get("coverage_loss_only") is True:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "coverage loss cannot reopen an accepted composite group"
        )


def _validate_artifact_self(
    artifact: Mapping[str, Any],
    *,
    current: Mapping[str, Any],
) -> None:
    _validate_artifact_fingerprint(artifact)
    if (
        artifact.get("brand_identity") != current.get("brand_identity")
        or artifact.get("parent_canonical_memory_version")
        != current.get("canonical_memory_version")
        or artifact.get("decision_rule") != "all_of"
        or artifact.get("lifecycle_transition") != "group_reopened"
        or artifact.get("score_eligible") is not False
        or artifact.get("replacement_authority_granted") is not False
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen artifact does not match its current parent"
        )
    _uuid(artifact.get("reopen_event_id"), field="reopen_event_id")


def _validate_artifact_fingerprint(artifact: Mapping[str, Any]) -> None:
    if artifact.get("schema_version") != EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_VERSION:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen schema mismatch"
        )
    unsigned = {key: deepcopy(value) for key, value in artifact.items() if key != "artifact_fingerprint"}
    if artifact.get("artifact_fingerprint") != canonical_fingerprint(
        EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_VERSION,
        unsigned,
    ):
        raise EvidenceVaultCompositeGroupLifecycleError(
            "composite-group reopen fingerprint mismatch"
        )


def _invalidation_relation(
    artifact: Mapping[str, Any],
    member: Mapping[str, Any],
) -> dict[str, Any]:
    relation_id = canonical_fingerprint(
        "evidence-vault-composite-group-member-invalidation-v1",
        {
            "artifact_fingerprint": artifact["artifact_fingerprint"],
            "group_id": artifact["group_id"],
            "superseded_relation_id": member["relation_id"],
            "reopen_event_id": artifact["reopen_event_id"],
        },
    )
    return {
        "relation_id": relation_id,
        "evidence_id": member["evidence_id"],
        "source_identity_id": member["source_identity_id"],
        "claim_id": artifact["group_id"],
        "polarity": "invalidates_candidate",
        "review_status": "accepted",
        "decision_event_id": artifact["reopen_event_id"],
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _durable_trigger(value: Mapping[str, Any] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"kind", "id", "fingerprint"}:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "durable reopen trigger fields mismatch"
        )
    kind = str(value.get("kind") or "").strip()
    identifier = str(value.get("id") or "").strip()
    if kind not in {"operation_plan", "relation_review"} or not identifier:
        raise EvidenceVaultCompositeGroupLifecycleError(
            "durable reopen trigger is invalid"
        )
    return {
        "kind": kind,
        "id": identifier,
        "fingerprint": _sha256(
            value.get("fingerprint"),
            field="durable_trigger.fingerprint",
        ),
    }


def _uuid(value: Any, *, field: str) -> str:
    try:
        return str(UUID(str(value or "")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceVaultCompositeGroupLifecycleError(
            f"{field} must be a UUID"
        ) from exc


def _sha256(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise EvidenceVaultCompositeGroupLifecycleError(
            f"{field} must be sha256"
        )
    return text


__all__ = [
    "COMPOSITE_GROUP_REOPEN_POLICY_ACTOR",
    "EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_SOURCE_RESOLUTION_VERSION",
    "EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_SOURCE_VERSION",
    "EVIDENCE_VAULT_COMPOSITE_GROUP_REOPEN_VERSION",
    "EvidenceVaultCompositeGroupLifecycleError",
    "build_composite_group_reopen_artifact",
    "build_composite_group_reopen_operational_packet",
    "build_composite_group_reopen_source_candidate",
    "build_composite_group_reopen_source_resolution",
    "validate_composite_group_reopen_artifact",
    "validate_composite_group_reopen_source_resolution",
]
