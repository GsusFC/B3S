"""Project reviewed coverage assessments into exact pending relations.

Assessment review can nominate relation candidates but cannot grant relation
or tile authority. This module freezes atomic and all-of groups for a separate
relation-scope human decision.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping
from urllib.parse import urlparse

from src.services.evidence_memory_identity_v2 import (
    project_evidence_memory_row_identity,
)
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    TileState,
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_tile_contract_registry,
    canonical_fingerprint,
    canonical_json,
    reducer_policy_fingerprint,
    tile_contract_registry_fingerprint,
    validate_candidate_packet,
)
from src.services.evidence_vault_operational_scoring import (
    EvidenceVaultOperationalScoringError,
    build_operational_score_evaluation,
)
from src.services.scanner_evidence_comparison import (
    canonical_evidence_representatives,
)


EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION = (
    "evidence-vault-exact-relation-supplement-v1"
)
EVIDENCE_VAULT_EXACT_RELATION_SOURCE_RESOLUTION_VERSION = (
    "evidence-vault-exact-relation-source-resolution-v1"
)
EXACT_RELATION_ATOMIC_REVIEW_EFFECT = (
    "exact_relation_decision_only_no_runtime_effect"
)
EXACT_RELATION_COMPOSITE_REVIEW_EFFECT = (
    "composite_all_of_decision_only_no_runtime_effect"
)
_ASSESSMENT_MUTABLE_FIELDS = frozenset(
    {"human_decision", "human_rationale", "reviewer_id", "reviewed_at"}
)


class EvidenceVaultExactRelationSupplementError(ValueError):
    """The assessment-to-relation projection is invalid."""


def build_exact_relation_supplement_artifact(
    *,
    brand_identity: str,
    subject_url: str,
    parent_canonical_memory_version: str,
    selected_tile_ids: list[str],
    assessment_artifact: Mapping[str, Any],
    assessment_review_rows: list[Mapping[str, Any]],
    assessment_worksheet_sha256: str,
    evidence_pack: Mapping[str, Any],
) -> dict[str, Any]:
    """Build exact pending relation groups from accepted assessments."""

    brand = _canonical_brand(brand_identity)
    subject = _canonical_url(subject_url)
    _validate_matching_identity(brand, subject)
    parent = _sha256(
        parent_canonical_memory_version,
        field="parent_canonical_memory_version",
    )
    worksheet_sha = _sha256(
        assessment_worksheet_sha256,
        field="assessment_worksheet_sha256",
    )
    selected = _selected_tiles(selected_tile_ids)
    assessments, reviewer_id, reviewed_at = _validated_assessments(
        assessment_artifact,
        assessment_review_rows=assessment_review_rows,
    )
    assessment_brand = _canonical_brand(assessment_artifact.get("brand_identity"))
    assessment_subject = _canonical_url(assessment_artifact.get("subject_url"))
    if assessment_brand != brand or (
        _identity_domain(assessment_subject) != _identity_domain(subject)
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation assessment identity mismatch"
        )
    if not set(selected).issubset(assessments):
        raise EvidenceVaultExactRelationSupplementError(
            "selected relation tiles are absent from the assessment"
        )
    pack = deepcopy(dict(evidence_pack))
    pack_sha = hashlib.sha256(canonical_json(pack).encode("utf-8")).hexdigest()
    if pack_sha != assessment_artifact.get(
        "source_evidence_pack_canonical_sha256"
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement evidence pack hash mismatch"
        )
    pack_subject = _canonical_url(pack.get("url"))
    if _identity_domain(pack_subject) != _identity_domain(subject):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation evidence pack identity mismatch"
        )
    rows = pack.get("evidence")
    if not isinstance(rows, list):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement evidence pack is invalid"
        )
    representatives = canonical_evidence_representatives(
        rows,
        subject_url=subject,
    )
    contracts = {
        str(row["tile_id"]): row for row in build_tile_contract_registry()["tiles"]
    }
    request_payload = {
        "brand_identity": brand,
        "subject_url": subject,
        "parent_canonical_memory_version": parent,
        "source_evidence_pack_canonical_sha256": pack_sha,
        "source_assessment_audit_fingerprint": assessment_artifact[
            "audit_fingerprint"
        ],
        "source_assessment_worksheet_sha256": worksheet_sha,
        "source_assessment_reviewer_id": reviewer_id,
        "source_assessment_reviewed_at": reviewed_at,
        "tile_contract_registry_fingerprint": (
            tile_contract_registry_fingerprint()
        ),
        "reducer_policy_fingerprint": reducer_policy_fingerprint(),
        "aggregation_policy_fingerprint": (
            canonical_aggregation_policy_fingerprint()
        ),
        "selected_assessment_candidate_ids": sorted(
            assessments[tile_id]["candidate_id"] for tile_id in selected
        ),
        "selected_tile_ids": selected,
    }
    request_fingerprint = canonical_fingerprint(
        "evidence-vault-exact-relation-supplement-request-v1",
        request_payload,
    )
    groups: list[dict[str, Any]] = []
    all_relation_ids: set[str] = set()
    for tile_id in selected:
        assessment = assessments[tile_id]
        if (
            assessment["proposed_disposition"] != "accept"
            or assessment["review_status"] != "unreviewed"
            or assessment["decision_event_id"] is not None
            or assessment["adoption_eligible"] is not False
        ):
            raise EvidenceVaultExactRelationSupplementError(
                f"assessment {tile_id} cannot nominate an exact relation"
            )
        status = str(assessment["status"])
        if status == "candidate":
            decision_rule = "atomic"
        elif status == "composite_candidate":
            decision_rule = "all_of"
        else:
            raise EvidenceVaultExactRelationSupplementError(
                f"assessment {tile_id} is not a positive candidate"
            )
        evidence = list(assessment.get("evidence") or [])
        if (
            not evidence
            or decision_rule == "atomic"
            and len(evidence) != 1
            or decision_rule == "all_of"
            and len(evidence) < 2
        ):
            raise EvidenceVaultExactRelationSupplementError(
                f"assessment {tile_id} has invalid relation cardinality"
            )
        members: list[dict[str, Any]] = []
        for evidence_row in evidence:
            fingerprint = _sha256(
                evidence_row.get("evidence_fingerprint"),
                field="evidence_fingerprint",
            )
            representative = representatives.get(fingerprint)
            if representative is None:
                raise EvidenceVaultExactRelationSupplementError(
                    f"assessment {tile_id} evidence is absent from the pack"
                )
            identity = project_evidence_memory_row_identity(
                {
                    **representative,
                    "evidence_fingerprint": fingerprint,
                },
                brand_domain=brand,
            )
            quote = _text(
                evidence_row.get("literal_quote"),
                field="literal_quote",
                maximum=4000,
            )
            if (
                identity is None
                or evidence_row.get("evidence_id") != identity["evidence_id"]
                or evidence_row.get("source_identity_id")
                != identity["document_id"]
                or evidence_row.get("ref") != representative.get("ref")
                or evidence_row.get("url")
                != (representative.get("url") or "")
                or evidence_row.get("source_class")
                != (
                    representative.get("metadata", {}).get("source_class")
                    or ""
                )
                or quote not in str(representative.get("content") or "")
            ):
                raise EvidenceVaultExactRelationSupplementError(
                    f"assessment {tile_id} evidence binding changed"
                )
            members.append(
                {
                    "evidence_fingerprint": fingerprint,
                    "evidence_id": identity["evidence_id"],
                    "source_identity_id": identity["document_id"],
                    "ref": str(representative["ref"]),
                    "url": str(representative.get("url") or ""),
                    "source_class": str(
                        representative.get("metadata", {}).get(
                            "source_class"
                        )
                        or ""
                    ),
                    "channel_role": _channel_role(
                        representative,
                        subject_url=subject,
                    ),
                    "literal_quote": quote,
                }
            )
        members.sort(
            key=lambda row: (
                row["source_identity_id"],
                row["evidence_fingerprint"],
            )
        )
        source_ids = {row["source_identity_id"] for row in members}
        channel_roles = {row["channel_role"] for row in members}
        if decision_rule == "all_of" and (
            len(source_ids) != len(members)
            or tile_id == "C7"
            and channel_roles != {"owned_web", "external_social_profile"}
        ):
            raise EvidenceVaultExactRelationSupplementError(
                f"composite assessment {tile_id} lacks distinct required channels"
            )
        group_identity = {
            "request_fingerprint": request_fingerprint,
            "tile_contract_registry_fingerprint": (
                tile_contract_registry_fingerprint()
            ),
            "assessment_candidate_id": assessment["candidate_id"],
            "tile_id": tile_id,
            "decision_rule": decision_rule,
            "members": members,
        }
        group_id = canonical_fingerprint(
            (
                "evidence-vault-exact-relation-composite-claim-v1"
                if decision_rule == "all_of"
                else "evidence-vault-exact-relation-atomic-group-v1"
            ),
            group_identity,
        )
        relations: list[dict[str, Any]] = []
        for member in members:
            relation_id = canonical_fingerprint(
                "evidence-vault-exact-relation-v1",
                {
                    "request_fingerprint": request_fingerprint,
                    "assessment_candidate_id": assessment["candidate_id"],
                    "group_id": group_id,
                    "tile_id": tile_id,
                    "evidence_id": member["evidence_id"],
                    "source_identity_id": member["source_identity_id"],
                    "claim_id": (
                        group_id if decision_rule == "all_of" else None
                    ),
                    "literal_quote": member["literal_quote"],
                    "polarity": "supports",
                },
            )
            if relation_id in all_relation_ids:
                raise EvidenceVaultExactRelationSupplementError(
                    "exact relation supplement repeats a relation id"
                )
            all_relation_ids.add(relation_id)
            relations.append(
                {
                    **member,
                    "relation_id": relation_id,
                    "tile_id": tile_id,
                    "polarity": "supports",
                    "claim_id": group_id if decision_rule == "all_of" else None,
                    "review_status": "unreviewed",
                    "decision_event_id": None,
                }
            )
        group_contract = {
            "decision_rule": decision_rule,
            "minimum_member_count": len(members),
            "required_distinct_source_identity_count": len(members),
            "required_channel_roles": (
                sorted(channel_roles) if decision_rule == "all_of" else []
            ),
            "member_decisions_must_match": decision_rule == "all_of",
            "member_change_requires_review": True,
        }
        groups.append(
            {
                "group_id": group_id,
                "assessment_candidate_id": assessment["candidate_id"],
                "tile_id": tile_id,
                "tile_key": contracts[tile_id]["tile_key"],
                "tile_condition": contracts[tile_id]["condition"],
                "evidence_contract_ok": contracts[tile_id][
                    "evidence_contract"
                ]["ok"],
                "evidence_contract_reject": contracts[tile_id][
                    "evidence_contract"
                ]["reject"],
                "decision_rule": decision_rule,
                "group_contract": group_contract,
                "relations": relations,
                "assessment_rationale": assessment["contract_reason"],
                "assessment_limitation": assessment["limitation"],
                "review_status": "unreviewed",
                "decision_event_id": None,
            }
        )
    groups.sort(key=lambda row: row["tile_id"])
    unsigned = {
        "schema_version": EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION,
        "brand_identity": brand,
        "subject_url": subject,
        "parent_canonical_memory_version": parent,
        "source_evidence_pack_canonical_sha256": pack_sha,
        "source_assessment_audit_fingerprint": assessment_artifact[
            "audit_fingerprint"
        ],
        "source_assessment_worksheet_sha256": worksheet_sha,
        "source_assessment_reviewer_id": reviewer_id,
        "source_assessment_reviewed_at": reviewed_at,
        "request_fingerprint": request_fingerprint,
        "tile_contract_registry_fingerprint": (
            tile_contract_registry_fingerprint()
        ),
        "reducer_policy_fingerprint": reducer_policy_fingerprint(),
        "aggregation_policy_fingerprint": (
            canonical_aggregation_policy_fingerprint()
        ),
        "selected_tile_ids": selected,
        "groups": groups,
        "relation_count": len(all_relation_ids),
        "adoption_eligible": False,
        "authority": False,
        "runtime_effect": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "cutover_authorized": False,
    }
    return {
        **unsigned,
        "artifact_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION,
            unsigned,
        ),
    }


def validate_exact_relation_supplement_structure(
    artifact: Mapping[str, Any],
) -> None:
    """Validate the self-contained relation/group and policy envelope."""

    fields = {
        "schema_version",
        "brand_identity",
        "subject_url",
        "parent_canonical_memory_version",
        "source_evidence_pack_canonical_sha256",
        "source_assessment_audit_fingerprint",
        "source_assessment_worksheet_sha256",
        "source_assessment_reviewer_id",
        "source_assessment_reviewed_at",
        "request_fingerprint",
        "tile_contract_registry_fingerprint",
        "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint",
        "selected_tile_ids",
        "groups",
        "relation_count",
        "adoption_eligible",
        "authority",
        "runtime_effect",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "cutover_authorized",
        "artifact_fingerprint",
    }
    if (
        not isinstance(artifact, Mapping)
        or set(artifact) != fields
        or artifact.get("schema_version")
        != EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION
        or artifact.get("adoption_eligible") is not False
        or artifact.get("authority") is not False
        or artifact.get("runtime_effect") is not False
        or artifact.get("production_runtime_effect") is not False
        or artifact.get("scanner_runtime_effect") is not False
        or artifact.get("cutover_authorized") is not False
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement structure is invalid"
        )
    brand = _canonical_brand(artifact.get("brand_identity"))
    subject = _canonical_url(artifact.get("subject_url"))
    _validate_matching_identity(brand, subject)
    parent = _sha256(
        artifact.get("parent_canonical_memory_version"),
        field="parent_canonical_memory_version",
    )
    pack_sha = _sha256(
        artifact.get("source_evidence_pack_canonical_sha256"),
        field="source_evidence_pack_canonical_sha256",
    )
    audit_fingerprint = _sha256(
        artifact.get("source_assessment_audit_fingerprint"),
        field="source_assessment_audit_fingerprint",
    )
    worksheet_sha = _sha256(
        artifact.get("source_assessment_worksheet_sha256"),
        field="source_assessment_worksheet_sha256",
    )
    reviewer = _text(
        artifact.get("source_assessment_reviewer_id"),
        field="source_assessment_reviewer_id",
        maximum=200,
    )
    reviewed_at = _timestamp(artifact.get("source_assessment_reviewed_at"))
    if (
        artifact.get("tile_contract_registry_fingerprint")
        != tile_contract_registry_fingerprint()
        or artifact.get("reducer_policy_fingerprint")
        != reducer_policy_fingerprint()
        or artifact.get("aggregation_policy_fingerprint")
        != canonical_aggregation_policy_fingerprint()
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement policy fingerprints changed"
        )
    selected = _selected_tiles(artifact.get("selected_tile_ids"))
    groups = artifact.get("groups")
    if not isinstance(groups, list) or len(groups) != len(selected):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement groups are invalid"
        )
    group_fields = {
        "group_id",
        "assessment_candidate_id",
        "tile_id",
        "tile_key",
        "tile_condition",
        "evidence_contract_ok",
        "evidence_contract_reject",
        "decision_rule",
        "group_contract",
        "relations",
        "assessment_rationale",
        "assessment_limitation",
        "review_status",
        "decision_event_id",
    }
    relation_fields = {
        "evidence_fingerprint",
        "evidence_id",
        "source_identity_id",
        "ref",
        "url",
        "source_class",
        "channel_role",
        "literal_quote",
        "relation_id",
        "tile_id",
        "polarity",
        "claim_id",
        "review_status",
        "decision_event_id",
    }
    relation_ids: set[str] = set()
    semantic_keys: set[tuple[Any, ...]] = set()
    candidate_ids: list[str] = []
    observed_tiles: list[str] = []
    for group in groups:
        if not isinstance(group, Mapping) or set(group) != group_fields:
            raise EvidenceVaultExactRelationSupplementError(
                "exact relation supplement group fields mismatch"
            )
        group_id = _sha256(group.get("group_id"), field="group_id")
        candidate_id = _sha256(
            group.get("assessment_candidate_id"),
            field="assessment_candidate_id",
        )
        tile_id = str(group.get("tile_id") or "")
        decision_rule = group.get("decision_rule")
        relations = group.get("relations")
        if (
            tile_id not in selected
            or group.get("review_status") != "unreviewed"
            or group.get("decision_event_id") is not None
            or decision_rule not in {"atomic", "all_of"}
            or not isinstance(relations, list)
            or decision_rule == "atomic"
            and len(relations) != 1
            or decision_rule == "all_of"
            and len(relations) < 2
        ):
            raise EvidenceVaultExactRelationSupplementError(
                "exact relation supplement group state is invalid"
            )
        source_ids: set[str] = set()
        channel_roles: set[str] = set()
        for relation in relations:
            if not isinstance(relation, Mapping) or set(relation) != relation_fields:
                raise EvidenceVaultExactRelationSupplementError(
                    "exact relation supplement relation fields mismatch"
                )
            relation_id = _sha256(
                relation.get("relation_id"), field="relation_id"
            )
            evidence_id = _sha256(
                relation.get("evidence_id"), field="evidence_id"
            )
            source_id = _sha256(
                relation.get("source_identity_id"),
                field="source_identity_id",
            )
            _sha256(
                relation.get("evidence_fingerprint"),
                field="evidence_fingerprint",
            )
            if (
                relation_id in relation_ids
                or relation.get("tile_id") != tile_id
                or relation.get("polarity") != "supports"
                or relation.get("review_status") != "unreviewed"
                or relation.get("decision_event_id") is not None
                or decision_rule == "all_of"
                and relation.get("claim_id") != group_id
                or decision_rule == "atomic"
                and relation.get("claim_id") is not None
                or not isinstance(relation.get("literal_quote"), str)
                or not relation["literal_quote"]
            ):
                raise EvidenceVaultExactRelationSupplementError(
                    "exact relation supplement relation state is invalid"
                )
            relation_ids.add(relation_id)
            source_ids.add(source_id)
            channel_roles.add(str(relation.get("channel_role") or ""))
            semantic_key = (
                tile_id,
                evidence_id,
                source_id,
                relation.get("claim_id"),
                relation["polarity"],
            )
            if semantic_key in semantic_keys:
                raise EvidenceVaultExactRelationSupplementError(
                    "exact relation supplement repeats a semantic relation"
                )
            semantic_keys.add(semantic_key)
        contract = group.get("group_contract")
        expected_contract = {
            "decision_rule": decision_rule,
            "minimum_member_count": len(relations),
            "required_distinct_source_identity_count": len(relations),
            "required_channel_roles": (
                sorted(channel_roles) if decision_rule == "all_of" else []
            ),
            "member_decisions_must_match": decision_rule == "all_of",
            "member_change_requires_review": True,
        }
        if (
            contract != expected_contract
            or len(source_ids) != len(relations)
            and decision_rule == "all_of"
        ):
            raise EvidenceVaultExactRelationSupplementError(
                "exact relation supplement group contract is invalid"
            )
        candidate_ids.append(candidate_id)
        observed_tiles.append(tile_id)
    if sorted(observed_tiles) != selected or len(set(observed_tiles)) != len(selected):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement group tiles mismatch"
        )
    if artifact.get("relation_count") != len(relation_ids):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement relation count mismatch"
        )
    request_payload = {
        "brand_identity": brand,
        "subject_url": subject,
        "parent_canonical_memory_version": parent,
        "source_evidence_pack_canonical_sha256": pack_sha,
        "source_assessment_audit_fingerprint": audit_fingerprint,
        "source_assessment_worksheet_sha256": worksheet_sha,
        "source_assessment_reviewer_id": reviewer,
        "source_assessment_reviewed_at": reviewed_at,
        "tile_contract_registry_fingerprint": (
            tile_contract_registry_fingerprint()
        ),
        "reducer_policy_fingerprint": reducer_policy_fingerprint(),
        "aggregation_policy_fingerprint": (
            canonical_aggregation_policy_fingerprint()
        ),
        "selected_assessment_candidate_ids": sorted(candidate_ids),
        "selected_tile_ids": selected,
    }
    if artifact.get("request_fingerprint") != canonical_fingerprint(
        "evidence-vault-exact-relation-supplement-request-v1",
        request_payload,
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement request fingerprint mismatch"
        )
    unsigned = {
        key: deepcopy(value)
        for key, value in artifact.items()
        if key != "artifact_fingerprint"
    }
    if artifact.get("artifact_fingerprint") != canonical_fingerprint(
        EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION,
        unsigned,
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement artifact fingerprint mismatch"
        )


def build_exact_relation_source_candidate(
    artifact: Mapping[str, Any],
    *,
    current_operational_memory: Mapping[str, Any],
) -> dict[str, Any]:
    """Project exact groups into one pending-only 80-tile source packet."""

    validate_exact_relation_supplement_structure(artifact)
    current = deepcopy(dict(current_operational_memory))
    try:
        build_operational_score_evaluation(
            current,
            created_at="2000-01-01T00:00:00+00:00",
        )
    except EvidenceVaultOperationalScoringError as exc:
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement parent memory is invalid"
        ) from exc
    content = current.get("content")
    if (
        current.get("authority") is not True
        or current.get("authority_scope") != "b3s-vault"
        or current.get("lifecycle_state") != "active"
        or current.get("production_runtime_effect") is not False
        or current.get("scanner_runtime_effect") is not False
        or current.get("brand_identity") != artifact["brand_identity"]
        or current.get("canonical_memory_version")
        != artifact["parent_canonical_memory_version"]
        or not isinstance(content, Mapping)
        or not isinstance(content.get("accepted_tiles"), list)
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement has a stale or invalid Vault parent"
        )
    for field in (
        "tile_contract_registry_fingerprint",
        "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint",
    ):
        if content.get(field) != artifact[field]:
            raise EvidenceVaultExactRelationSupplementError(
                f"exact relation supplement changes parent policy: {field}"
            )
    registry = build_tile_contract_registry()["tiles"]
    current_by_id = {
        str(row["tile_id"]): dict(row) for row in content["accepted_tiles"]
    }
    previous = [
        build_candidate_tile(
            tile_id=str(contract["tile_id"]),
            basis=(current_by_id.get(str(contract["tile_id"])) or {}).get(
                "basis"
            )
            or [],
            coverage_refs=(
                current_by_id.get(str(contract["tile_id"])) or {}
            ).get("coverage_refs")
            or [],
            unresolved_refs=(
                current_by_id.get(str(contract["tile_id"])) or {}
            ).get("unresolved_refs")
            or [],
        )
        for contract in registry
    ]
    previous_by_id = {str(row["tile_id"]): row for row in previous}
    current_relation_ids = {
        str(basis["relation_id"])
        for row in previous
        for basis in row.get("basis") or []
    }
    current_semantic_keys = {
        (
            row["tile_id"],
            basis["evidence_id"],
            basis["source_identity_id"],
            basis.get("claim_id"),
            basis["polarity"],
        )
        for row in previous
        for basis in row.get("basis") or []
    }
    new_by_tile: dict[str, list[dict[str, Any]]] = {}
    for group in artifact["groups"]:
        tile_id = str(group["tile_id"])
        for relation in group["relations"]:
            semantic_key = (
                tile_id,
                relation["evidence_id"],
                relation["source_identity_id"],
                relation.get("claim_id"),
                relation["polarity"],
            )
            if (
                relation["relation_id"] in current_relation_ids
                or semantic_key in current_semantic_keys
            ):
                raise EvidenceVaultExactRelationSupplementError(
                    "exact relation supplement duplicates accepted parent basis"
                )
            new_by_tile.setdefault(tile_id, []).append(
                {
                    "relation_id": relation["relation_id"],
                    "evidence_id": relation["evidence_id"],
                    "source_identity_id": relation["source_identity_id"],
                    "claim_id": relation["claim_id"],
                    "polarity": relation["polarity"],
                    "review_status": "unreviewed",
                    "decision_event_id": None,
                    "absence_test_contract_id": None,
                    "coverage_assessment_id": None,
                    "coverage_status": None,
                    "tested_scope": None,
                    "observed_result": None,
                }
            )
    updates: list[dict[str, Any]] = []
    for tile_id, relations in sorted(new_by_tile.items()):
        prior = previous_by_id[tile_id]
        combined = sorted(
            [*prior["basis"], *relations],
            key=lambda row: str(row["relation_id"]),
        )
        probe = build_candidate_tile(
            tile_id=tile_id,
            basis=combined,
            coverage_refs=prior["coverage_refs"],
            unresolved_refs=prior["unresolved_refs"],
        )
        previous_state = TileState(prior["candidate_state"])
        candidate_state = TileState(probe["candidate_state"])
        if candidate_state is TileState.CONTRADICTION:
            delta_kind = "contradiction"
            unresolved_refs = [
                f"exact-relation:{artifact['artifact_fingerprint']}:{tile_id}"
            ]
        elif candidate_state is previous_state:
            delta_kind = "strengthened"
            unresolved_refs = prior["unresolved_refs"]
        else:
            delta_kind = "candidate_update"
            unresolved_refs = prior["unresolved_refs"]
        updates.append(
            {
                "tile_id": tile_id,
                "delta_kind": delta_kind,
                "basis": combined,
                "coverage_refs": prior["coverage_refs"],
                "unresolved_refs": unresolved_refs,
            }
        )
    candidates = build_incremental_candidate_tiles(
        previous_candidate_tiles=previous,
        tile_updates=updates,
    )
    unresolved = [
        {
            "unresolved_id": ref,
            "kind": "contradiction",
            "blocking": True,
            "details": {"source": "exact_relation_supplement"},
        }
        for candidate in candidates
        if candidate["candidate_state"] == TileState.CONTRADICTION.value
        for ref in candidate.get("unresolved_refs") or []
    ]
    decision_groups = [
        {
            "group_id": group["group_id"],
            "tile_id": group["tile_id"],
            "decision_rule": group["decision_rule"],
            "member_relation_ids": sorted(
                relation["relation_id"] for relation in group["relations"]
            ),
        }
        for group in artifact["groups"]
    ]
    packet = build_candidate_packet(
        brand_identity=str(artifact["brand_identity"]),
        parent_canonical_memory_version=str(
            current["canonical_memory_version"]
        ),
        candidate_memory_version=canonical_fingerprint(
            "evidence-vault-exact-relation-candidate-memory-v1",
            {
                "artifact_fingerprint": artifact["artifact_fingerprint"],
                "parent_canonical_memory_version": current[
                    "canonical_memory_version"
                ],
            },
        ),
        accepted_memory_candidate_version=canonical_fingerprint(
            "evidence-vault-exact-relation-accepted-memory-v1",
            content,
        ),
        reviewed_memory_candidate_version=canonical_fingerprint(
            "evidence-vault-exact-relation-reviewed-memory-v1",
            {"accepted_tiles": content["accepted_tiles"]},
        ),
        review_packet_set_fingerprint=canonical_fingerprint(
            "evidence-vault-exact-relation-review-set-v1",
            decision_groups,
        ),
        aggregation_policy_fingerprint=str(
            artifact["aggregation_policy_fingerprint"]
        ),
        candidate_tiles=candidates,
        coverage_summary={
            "source": "exact_relation_supplement",
            "request_fingerprint": artifact["request_fingerprint"],
            "artifact_fingerprint": artifact["artifact_fingerprint"],
            "pending_relation_count": artifact["relation_count"],
            "decision_groups": decision_groups,
        },
        unresolved_items=unresolved,
    )
    validate_candidate_packet(packet)
    return packet


def validate_exact_relation_source_decisions(
    source_candidate_packet: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
) -> None:
    """Enforce one atomic all-of decision for every declared group."""

    source = deepcopy(dict(source_candidate_packet))
    validate_candidate_packet(source)
    coverage = source["manifest"].get("coverage_summary") or {}
    if coverage.get("source") != "exact_relation_supplement":
        return
    groups = coverage.get("decision_groups")
    if not isinstance(groups, list) or not groups:
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation source decision groups are missing"
        )
    pending_by_id = {
        str(relation["relation_id"]): (str(tile["tile_id"]), relation)
        for tile in source["candidate_tiles"]
        for relation in tile.get("basis") or []
        if relation.get("review_status") == "unreviewed"
    }
    if set(decisions) != set(pending_by_id):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation decisions must resolve the complete pending set"
        )
    grouped_ids: set[str] = set()
    for group in groups:
        if not isinstance(group, Mapping) or set(group) != {
            "group_id",
            "tile_id",
            "decision_rule",
            "member_relation_ids",
        }:
            raise EvidenceVaultExactRelationSupplementError(
                "exact relation decision group fields mismatch"
            )
        group_id = _sha256(group.get("group_id"), field="group_id")
        tile_id = str(group.get("tile_id") or "")
        rule = group.get("decision_rule")
        members = group.get("member_relation_ids")
        if (
            rule not in {"atomic", "all_of"}
            or not isinstance(members, list)
            or members != sorted(members)
            or len(set(members)) != len(members)
            or rule == "atomic"
            and len(members) != 1
            or rule == "all_of"
            and len(members) < 2
        ):
            raise EvidenceVaultExactRelationSupplementError(
                "exact relation decision group is invalid"
            )
        group_decisions: set[str] = set()
        group_rationales: set[str] = set()
        for member in members:
            relation_id = _sha256(member, field="member_relation_id")
            if relation_id in grouped_ids or relation_id not in pending_by_id:
                raise EvidenceVaultExactRelationSupplementError(
                    "exact relation decision group membership is invalid"
                )
            relation_tile, relation = pending_by_id[relation_id]
            if (
                relation_tile != tile_id
                or rule == "all_of"
                and relation.get("claim_id") != group_id
                or rule == "atomic"
                and relation.get("claim_id") is not None
            ):
                raise EvidenceVaultExactRelationSupplementError(
                    "exact relation decision group basis is invalid"
                )
            decision = decisions.get(relation_id)
            if not isinstance(decision, Mapping) or decision.get("decision") not in {
                "accept",
                "reject",
            }:
                raise EvidenceVaultExactRelationSupplementError(
                    "exact relation group decision is invalid"
                )
            group_decisions.add(str(decision["decision"]))
            if "rationale" in decision:
                group_rationales.add(str(decision["rationale"]))
            grouped_ids.add(relation_id)
        if rule == "all_of" and (
            len(group_decisions) != 1
            or len(group_rationales) > 1
        ):
            raise EvidenceVaultExactRelationSupplementError(
                "all-of exact relation members require one matching decision"
            )
    if grouped_ids != set(pending_by_id):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation groups must cover every pending relation exactly"
        )


def build_exact_relation_source_resolution(
    artifact: Mapping[str, Any],
    *,
    source_candidate_packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze the exact relation/group artifact behind its source packet."""

    validate_exact_relation_supplement_structure(artifact)
    source = deepcopy(dict(source_candidate_packet))
    validate_candidate_packet(source)
    if (
        source["manifest"]["brand_identity"] != artifact["brand_identity"]
        or source["manifest"]["parent_canonical_memory_version"]
        != artifact["parent_canonical_memory_version"]
        or source["manifest"]["coverage_summary"].get(
            "artifact_fingerprint"
        )
        != artifact["artifact_fingerprint"]
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation source resolution lineage mismatch"
        )
    return {
        "schema_version": EVIDENCE_VAULT_EXACT_RELATION_SOURCE_RESOLUTION_VERSION,
        "source_kind": "exact_relation_supplement",
        "request_fingerprint": artifact["request_fingerprint"],
        "artifact_fingerprint": artifact["artifact_fingerprint"],
        "source_evidence_pack_sha256": artifact[
            "source_evidence_pack_canonical_sha256"
        ],
        "source_candidate_packet_fingerprint": source[
            "candidate_packet_fingerprint"
        ],
        "parent_canonical_memory_version": artifact[
            "parent_canonical_memory_version"
        ],
        "decision_groups": source["manifest"]["coverage_summary"][
            "decision_groups"
        ],
        "artifact": deepcopy(dict(artifact)),
    }


def validate_exact_relation_supplement_artifact(
    artifact: Mapping[str, Any],
    *,
    assessment_artifact: Mapping[str, Any],
    assessment_review_rows: list[Mapping[str, Any]],
    assessment_worksheet_sha256: str,
    evidence_pack: Mapping[str, Any],
) -> None:
    """Rebuild the entire artifact and reject any altered relation/group."""

    if not isinstance(artifact, Mapping):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement artifact must be an object"
        )
    validate_exact_relation_supplement_structure(artifact)
    rebuilt = build_exact_relation_supplement_artifact(
        brand_identity=artifact.get("brand_identity"),
        subject_url=artifact.get("subject_url"),
        parent_canonical_memory_version=artifact.get(
            "parent_canonical_memory_version"
        ),
        selected_tile_ids=artifact.get("selected_tile_ids"),
        assessment_artifact=assessment_artifact,
        assessment_review_rows=assessment_review_rows,
        assessment_worksheet_sha256=assessment_worksheet_sha256,
        evidence_pack=evidence_pack,
    )
    if rebuilt != dict(artifact):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation supplement artifact differs from its sources"
        )


def build_exact_relation_review_worksheets(
    artifact: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return atomic relation rows and all-of group rows for human review."""

    atomic: list[dict[str, str]] = []
    composite: list[dict[str, str]] = []
    for group in artifact["groups"]:
        if group["decision_rule"] == "atomic":
            relation = group["relations"][0]
            atomic.append(
                {
                    "group_id": group["group_id"],
                    "relation_id": relation["relation_id"],
                    "assessment_candidate_id": group[
                        "assessment_candidate_id"
                    ],
                    "evidence_fingerprint": relation[
                        "evidence_fingerprint"
                    ],
                    "evidence_id": relation["evidence_id"],
                    "source_identity_id": relation["source_identity_id"],
                    "evidence_ref": relation["ref"],
                    "evidence_url": relation["url"],
                    "source_class": relation["source_class"],
                    "tile_id": group["tile_id"],
                    "tile_key": group["tile_key"],
                    "tile_condition": group["tile_condition"],
                    "evidence_contract_ok": group["evidence_contract_ok"],
                    "evidence_contract_reject": group[
                        "evidence_contract_reject"
                    ],
                    "polarity": relation["polarity"],
                    "literal_quote": relation["literal_quote"],
                    "rationale": group["assessment_rationale"],
                    "review_effect": EXACT_RELATION_ATOMIC_REVIEW_EFFECT,
                    "allowed_human_decisions": "accept|reject",
                    "human_decision": "",
                    "human_rationale": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                }
            )
            continue
        composite.append(
            {
                "group_id": group["group_id"],
                "assessment_candidate_id": group[
                    "assessment_candidate_id"
                ],
                "tile_id": group["tile_id"],
                "tile_key": group["tile_key"],
                "tile_condition": group["tile_condition"],
                "evidence_contract_ok": group["evidence_contract_ok"],
                "evidence_contract_reject": group[
                    "evidence_contract_reject"
                ],
                "decision_rule": group["decision_rule"],
                "group_contract": json.dumps(
                    group["group_contract"],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "member_relation_ids": json.dumps(
                    [row["relation_id"] for row in group["relations"]],
                    ensure_ascii=False,
                ),
                "member_evidence_fingerprints": json.dumps(
                    [
                        row["evidence_fingerprint"]
                        for row in group["relations"]
                    ],
                    ensure_ascii=False,
                ),
                "member_evidence_ids": json.dumps(
                    [row["evidence_id"] for row in group["relations"]],
                    ensure_ascii=False,
                ),
                "member_source_identity_ids": json.dumps(
                    [
                        row["source_identity_id"]
                        for row in group["relations"]
                    ],
                    ensure_ascii=False,
                ),
                "member_evidence_refs": json.dumps(
                    [row["ref"] for row in group["relations"]],
                    ensure_ascii=False,
                ),
                "member_evidence_urls": json.dumps(
                    [row["url"] for row in group["relations"]],
                    ensure_ascii=False,
                ),
                "member_channel_roles": json.dumps(
                    [row["channel_role"] for row in group["relations"]],
                    ensure_ascii=False,
                ),
                "member_literal_quotes": json.dumps(
                    [row["literal_quote"] for row in group["relations"]],
                    ensure_ascii=False,
                ),
                "rationale": group["assessment_rationale"],
                "review_effect": EXACT_RELATION_COMPOSITE_REVIEW_EFFECT,
                "allowed_human_decisions": "accept|reject",
                "human_decision": "",
                "human_rationale": "",
                "reviewer_id": "",
                "reviewed_at": "",
            }
        )
    return atomic, composite


def _validated_assessments(
    artifact: Mapping[str, Any],
    *,
    assessment_review_rows: list[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str, str]:
    if not isinstance(artifact, Mapping):
        raise EvidenceVaultExactRelationSupplementError(
            "assessment artifact is invalid"
        )
    unsigned = {
        key: value for key, value in artifact.items() if key != "audit_fingerprint"
    }
    if (
        artifact.get("schema_version")
        != "evidence-vault-independent-contract-audit-v1"
        or artifact.get("audit_fingerprint")
        != canonical_fingerprint(
            "evidence-vault-independent-contract-audit-v1",
            unsigned,
        )
        or artifact.get("authority") is not False
        or artifact.get("runtime_effect") is not False
        or artifact.get("review_effect")
        != "coverage_assessment_only_no_adoption"
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "assessment artifact authority boundary is invalid"
        )
    assessments: dict[str, dict[str, Any]] = {}
    by_candidate: dict[str, dict[str, Any]] = {}
    for raw in artifact.get("tile_assessments") or []:
        assessment = deepcopy(dict(raw))
        payload = {
            key: value
            for key, value in assessment.items()
            if key not in {
                "candidate_id",
                "review_status",
                "decision_event_id",
            }
        }
        if assessment.get("candidate_id") != canonical_fingerprint(
            "evidence-vault-independent-contract-audit-candidate-v1",
            payload,
        ):
            raise EvidenceVaultExactRelationSupplementError(
                "assessment candidate fingerprint mismatch"
            )
        tile_id = str(assessment.get("tile_id") or "")
        if tile_id in assessments:
            raise EvidenceVaultExactRelationSupplementError(
                "assessment repeats a tile"
            )
        assessments[tile_id] = assessment
        by_candidate[assessment["candidate_id"]] = assessment
    if (
        not isinstance(assessment_review_rows, list)
        or len(assessment_review_rows) != len(by_candidate)
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "assessment review row count mismatch"
        )
    reviewers: set[str] = set()
    reviewed_times: set[str] = set()
    seen: set[str] = set()
    for raw in assessment_review_rows:
        row = dict(raw)
        candidate_id = str(row.get("candidate_id") or "")
        assessment = by_candidate.get(candidate_id)
        if assessment is None or candidate_id in seen:
            raise EvidenceVaultExactRelationSupplementError(
                "assessment review identities mismatch"
            )
        seen.add(candidate_id)
        expected = _assessment_review_immutable(assessment)
        if set(row) != set(expected) | _ASSESSMENT_MUTABLE_FIELDS or any(
            str(row.get(field) or "") != value
            for field, value in expected.items()
        ):
            raise EvidenceVaultExactRelationSupplementError(
                "assessment review immutable columns changed"
            )
        if row.get("human_decision") not in {"accept", "reject"}:
            raise EvidenceVaultExactRelationSupplementError(
                "assessment review decision is invalid"
            )
        if not str(row.get("human_rationale") or "").strip():
            raise EvidenceVaultExactRelationSupplementError(
                "assessment review rationale is required"
            )
        reviewer = _text(
            row.get("reviewer_id"),
            field="reviewer_id",
            maximum=200,
        )
        reviewed_at = _timestamp(row.get("reviewed_at"))
        reviewers.add(reviewer)
        reviewed_times.add(reviewed_at)
        if row["human_decision"] != "accept":
            assessments.pop(str(assessment["tile_id"]), None)
    if set(seen) != set(by_candidate) or len(reviewers) != 1 or len(reviewed_times) != 1:
        raise EvidenceVaultExactRelationSupplementError(
            "assessment review is incomplete or has mixed attribution"
        )
    return assessments, next(iter(reviewers)), next(iter(reviewed_times))


def _assessment_review_immutable(
    assessment: Mapping[str, Any],
) -> dict[str, str]:
    return {
        "candidate_id": str(assessment["candidate_id"]),
        "tile_id": str(assessment["tile_id"]),
        "candidate_status": str(assessment["status"]),
        "proposed_disposition": str(assessment["proposed_disposition"]),
        "evidence_fingerprints": json.dumps(
            [row["evidence_fingerprint"] for row in assessment["evidence"]],
            ensure_ascii=False,
        ),
        "evidence_refs": json.dumps(
            [row["ref"] for row in assessment["evidence"]],
            ensure_ascii=False,
        ),
        "literal_quotes": json.dumps(
            [row["literal_quote"] for row in assessment["evidence"]],
            ensure_ascii=False,
        ),
        "contract_reason": str(assessment["contract_reason"]),
        "limitation": str(assessment["limitation"]),
        "review_effect": "coverage_assessment_only_no_adoption",
        "allowed_human_decisions": "accept|reject",
    }


def _channel_role(row: Mapping[str, Any], *, subject_url: str) -> str:
    url = str(row.get("url") or "")
    subject_host = (urlparse(subject_url).hostname or "").lower()
    host = (urlparse(url).hostname or "").lower()
    source_class = str(row.get("metadata", {}).get("source_class") or "")
    if source_class == "owned_copy" and host in {subject_host, f"www.{subject_host}"}:
        return "owned_web"
    if (
        source_class == "external_proof"
        and host in {"linkedin.com", "www.linkedin.com"}
        and urlparse(url).path.startswith("/company/")
    ):
        return "external_social_profile"
    return "other"


def _selected_tiles(values: Any) -> list[str]:
    registry = {
        str(row["tile_id"]) for row in build_tile_contract_registry()["tiles"]
    }
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) for value in values)
        or values != sorted(set(values))
        or not set(values).issubset(registry)
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "selected relation tiles are invalid"
        )
    return list(values)


def _identity_domain(value: str) -> str:
    candidate = value if "://" in value else f"https://{value}"
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation identity is invalid"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or ":" in parsed.netloc
        or "[" in parsed.netloc
        or "]" in parsed.netloc
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation identity is invalid"
        )
    domain = host.strip(".").lower().removeprefix("www.")
    labels = domain.split(".")
    if (
        not domain.isascii()
        or len(labels) < 2
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in label)
            for label in labels
        )
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation identity is invalid"
        )
    return domain


def _validate_matching_identity(brand_identity: str, subject_url: str) -> None:
    if _identity_domain(brand_identity) != _identity_domain(subject_url):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation brand and subject identities mismatch"
        )


def _canonical_brand(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip().lower()
        or not value
        or any(character in value for character in "/?#@:")
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation brand identity is invalid"
        )
    _identity_domain(value)
    return value


def _canonical_url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or "://" not in value
    ):
        raise EvidenceVaultExactRelationSupplementError(
            "exact relation subject URL is invalid"
        )
    _identity_domain(value)
    return value


def _sha256(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise EvidenceVaultExactRelationSupplementError(
            f"{field} must be a SHA-256"
        )
    return value


def _text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise EvidenceVaultExactRelationSupplementError(f"{field} must be text")
    text = value.strip()
    if not text or text != value or len(text) > maximum or "\x00" in text:
        raise EvidenceVaultExactRelationSupplementError(f"{field} is invalid")
    return text


def _timestamp(value: Any) -> str:
    text = _text(value, field="reviewed_at", maximum=100)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EvidenceVaultExactRelationSupplementError(
            "reviewed_at is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceVaultExactRelationSupplementError(
            "reviewed_at must include timezone"
        )
    return text


__all__ = [
    "EVIDENCE_VAULT_EXACT_RELATION_SOURCE_RESOLUTION_VERSION",
    "EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION",
    "EXACT_RELATION_ATOMIC_REVIEW_EFFECT",
    "EXACT_RELATION_COMPOSITE_REVIEW_EFFECT",
    "EvidenceVaultExactRelationSupplementError",
    "build_exact_relation_review_worksheets",
    "build_exact_relation_source_candidate",
    "build_exact_relation_source_resolution",
    "build_exact_relation_supplement_artifact",
    "validate_exact_relation_source_decisions",
    "validate_exact_relation_supplement_artifact",
    "validate_exact_relation_supplement_structure",
]
