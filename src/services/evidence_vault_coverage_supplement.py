"""Non-authoritative, explicitly scoped evidence coverage supplements.

A supplement can ask the relation model about frozen evidence/tile pairs that
an acquisition delta did not revisit. It cannot grant authority or score. A
separate, exact projection may register its relations as a pending-only source
for durable human review.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
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
    validate_candidate_packet,
)
from src.services.evidence_vault_operational_scoring import (
    EvidenceVaultOperationalScoringError,
    build_operational_score_evaluation,
)
from src.services.scanner_evidence_comparison import (
    canonical_evidence_representatives,
)
from src.sv9_flow.evidence_tile_relation_worker import (
    EVIDENCE_TILE_RELATION_LEGACY_PROPOSAL_VERSION,
    EVIDENCE_TILE_RELATION_PROPOSAL_VERSION,
    evidence_tile_relation_call_count,
    propose_evidence_tile_relations,
)


EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_REQUEST_VERSION = (
    "evidence-vault-coverage-supplement-request-v1"
)
_LEGACY_COVERAGE_SUPPLEMENT_RESULT_VERSION = "evidence-vault-coverage-supplement-result-v1"
EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION = "evidence-vault-coverage-supplement-result-v2"
EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_ARTIFACT_VERSION = (
    "evidence-vault-field-coverage-supplement-artifact-v1"
)
EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_SOURCE_RESOLUTION_VERSION = (
    "evidence-vault-coverage-supplement-source-resolution-v1"
)
_MAX_EVIDENCE_ROWS = 40
_MAX_PAIRS = 120
_RELATION_ANALYSIS_STATES = frozenset(
    {"analyzed_without_sufficient_support", "supported", "inconclusive"}
)
_DISCARD_REASONS = frozenset(
    {
        "duplicate_relation",
        "evidence_outside_plan",
        "field_type_invalid",
        "fields_mismatch",
        "polarity_invalid",
        "quote_not_literal",
        "rationale_invalid",
        "tile_outside_shortlist",
    }
)


class EvidenceVaultCoverageSupplementError(ValueError):
    """The requested non-authoritative coverage supplement is invalid."""


def build_coverage_supplement_request(
    *,
    brand_identity: str,
    subject_url: str,
    parent_canonical_memory_version: str,
    source_evidence_pack_sha256: str,
    evidence_rows: list[Mapping[str, Any]],
    tile_shortlists: Mapping[str, list[str]],
    target_tile_ids: list[str],
    rationale: str,
) -> dict[str, Any]:
    """Freeze one reviewed evidence/tile workset without granting authority."""

    if not isinstance(brand_identity, str) or not isinstance(
        subject_url, str
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement identity must be text"
        )
    brand = brand_identity.strip().lower()
    url = subject_url.strip()
    _validate_matching_identity(brand, url)
    parent = _sha256(
        parent_canonical_memory_version,
        field="parent_canonical_memory_version",
    )
    pack_sha = _sha256(
        source_evidence_pack_sha256,
        field="source_evidence_pack_sha256",
    )
    reason = _text(rationale, field="rationale", maximum=2000)
    registry_ids = {
        str(row["tile_id"]) for row in build_tile_contract_registry()["tiles"]
    }
    if not isinstance(target_tile_ids, list) or not all(
        isinstance(value, str) for value in target_tile_ids
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement target tiles are invalid"
        )
    targets = sorted(set(target_tile_ids))
    if (
        not brand
        or not url
        or not targets
        or targets != target_tile_ids
        or not set(targets).issubset(registry_ids)
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement target tiles are invalid"
        )
    evidence = _evidence_rows(evidence_rows)
    if not evidence or len(evidence) > _MAX_EVIDENCE_ROWS:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence workset is invalid"
        )
    _validate_owned_evidence_identity(evidence.values(), subject_url=url)
    if set(tile_shortlists) != set(evidence):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement shortlist workset differs from evidence"
        )
    normalized_shortlists: dict[str, list[str]] = {}
    pair_count = 0
    for fingerprint, raw_tile_ids in tile_shortlists.items():
        normalized = _sha256(
            fingerprint,
            field="shortlist evidence fingerprint",
        )
        if (
            not isinstance(raw_tile_ids, list)
            or not all(isinstance(value, str) for value in raw_tile_ids)
            or raw_tile_ids != sorted(set(raw_tile_ids))
            or not raw_tile_ids
            or not set(raw_tile_ids).issubset(targets)
        ):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement shortlist is invalid"
            )
        pair_count += len(raw_tile_ids)
        normalized_shortlists[normalized] = list(raw_tile_ids)
    if pair_count > _MAX_PAIRS:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement exceeds its pair bound"
        )
    workset = [
        {
            "evidence_fingerprint": fingerprint,
            "ref": evidence[fingerprint]["ref"],
            "source": evidence[fingerprint]["source"],
            "evidence_type": evidence[fingerprint]["evidence_type"],
            "url": evidence[fingerprint]["url"],
            "source_class": str(
                evidence[fingerprint].get("metadata", {}).get(
                    "source_class"
                )
                or ""
            ),
            "evidence_snapshot_fingerprint": _evidence_snapshot_fingerprint(
                evidence[fingerprint]
            ),
            "tile_ids": normalized_shortlists[fingerprint],
        }
        for fingerprint in sorted(evidence)
    ]
    unsigned = {
        "schema_version": EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_REQUEST_VERSION,
        "brand_identity": brand,
        "subject_url": url,
        "parent_canonical_memory_version": parent,
        "source_evidence_pack_sha256": pack_sha,
        "target_tile_ids": targets,
        "evidence_workset": workset,
        "pair_count": pair_count,
        "rationale": reason,
        "authority": False,
        "runtime_effect": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    return {
        **unsigned,
        "request_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_REQUEST_VERSION,
            unsigned,
        ),
    }


def execute_coverage_supplement(
    request: Mapping[str, Any],
    *,
    evidence_rows: list[Mapping[str, Any]],
    llm: Any,
) -> dict[str, Any]:
    """Run one bounded relation proposal and return pending-only artifacts."""

    request_value = deepcopy(dict(request))
    validate_coverage_supplement_request(request_value)
    evidence = _evidence_rows(evidence_rows)
    expected_fingerprints = {
        str(row["evidence_fingerprint"])
        for row in request_value["evidence_workset"]
    }
    if set(evidence) != expected_fingerprints:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence differs from its frozen request"
        )
    request_workset = {
        str(row["evidence_fingerprint"]): row
        for row in request_value["evidence_workset"]
    }
    if any(
        _evidence_snapshot_fingerprint(row)
        != request_workset[fingerprint]["evidence_snapshot_fingerprint"]
        for fingerprint, row in evidence.items()
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence snapshot binding changed"
        )
    workset = {
        str(row["evidence_fingerprint"]): list(row["tile_ids"])
        for row in request_value["evidence_workset"]
    }
    contracts = {
        str(row["tile_id"]): row
        for row in build_tile_contract_registry()["tiles"]
    }
    proposal_rows = [evidence[value] for value in sorted(evidence)]
    proposal_shortlists = {
        fingerprint: [contracts[tile_id] for tile_id in tile_ids]
        for fingerprint, tile_ids in sorted(workset.items())
    }
    proposal = propose_evidence_tile_relations(
        evidence_rows=proposal_rows, tile_shortlists=proposal_shortlists, llm=llm
    )
    basis = _basis_from_proposal(
        proposal,
        evidence=evidence,
        brand_identity=request_value["brand_identity"],
        request_fingerprint=request_value["request_fingerprint"],
        tile_shortlists=workset,
    )
    proposed_tiles = {row["tile_id"] for row in basis}
    result = {
        "schema_version": EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION,
        "request_fingerprint": request_value["request_fingerprint"],
        "parent_canonical_memory_version": request_value[
            "parent_canonical_memory_version"
        ],
        "target_tile_ids": request_value["target_tile_ids"],
        "uncovered_target_tile_ids": sorted(
            set(request_value["target_tile_ids"]) - proposed_tiles
        ),
        "evidence_snapshot": [
            {
                "evidence_fingerprint": fingerprint,
                **deepcopy(evidence[fingerprint]),
            }
            for fingerprint in sorted(evidence)
        ],
        "relation_proposal": proposal,
        "relation_proposal_call_count": evidence_tile_relation_call_count(
            evidence_rows=proposal_rows, tile_shortlists=proposal_shortlists
        ),
        "basis_relations": basis,
        "authority": False,
        "runtime_effect": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    result["result_fingerprint"] = canonical_fingerprint(
        EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION,
        result,
    )
    validate_coverage_supplement_result(result, request=request_value)
    return result


def validate_coverage_supplement_artifact(
    artifact: Mapping[str, Any],
    *,
    evidence_pack: Mapping[str, Any],
) -> None:
    """Bind one field artifact to its exact normalized evidence pack."""

    fields = {
        "schema_version",
        "model",
        "execution_mode",
        "replay_source_result_fingerprint",
        "replay_source_file_sha256",
        "replay_source_file",
        "request",
        "result",
        "explicit_no_candidate_tile_ids",
        "limitations",
        "worksheet_relation_count",
        "review_instructions",
        "authority",
        "runtime_effect",
        "cutover_authorized",
    }
    if (
        not isinstance(artifact, Mapping)
        or set(artifact) != fields
        or artifact.get("schema_version")
        != EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_ARTIFACT_VERSION
        or artifact.get("authority") is not False
        or artifact.get("runtime_effect") is not False
        or artifact.get("cutover_authorized") is not False
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement artifact is invalid"
        )
    model = artifact.get("model")
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement artifact model is invalid"
        )
    mode = artifact.get("execution_mode")
    if mode == "live_provider":
        if any(
            artifact.get(field) is not None
            for field in (
                "replay_source_result_fingerprint",
                "replay_source_file_sha256",
                "replay_source_file",
            )
        ):
            raise EvidenceVaultCoverageSupplementError(
                "live coverage artifact declares replay provenance"
            )
    elif mode == "persisted_relation_replay":
        _sha256(
            artifact.get("replay_source_result_fingerprint"),
            field="replay_source_result_fingerprint",
        )
        _sha256(
            artifact.get("replay_source_file_sha256"),
            field="replay_source_file_sha256",
        )
        _text(
            artifact.get("replay_source_file"),
            field="replay_source_file",
            maximum=500,
        )
    else:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement execution mode is invalid"
        )
    request = artifact.get("request")
    result = artifact.get("result")
    if not isinstance(request, Mapping) or not isinstance(result, Mapping):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement request and result are required"
        )
    validate_coverage_supplement_request(request)
    validate_coverage_supplement_result(result, request=request)
    if artifact.get("worksheet_relation_count") != len(
        result["basis_relations"]
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement worksheet count is invalid"
        )
    instructions = artifact.get("review_instructions")
    if instructions != {
        "allowed_human_decisions": ["accept", "reject"],
        "required_fields": [
            "human_decision",
            "human_rationale",
            "reviewer_id",
            "reviewed_at",
        ],
        "effect": "relation_decision_only_no_runtime_effect",
    }:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement review instructions are invalid"
        )
    no_candidate = artifact.get("explicit_no_candidate_tile_ids")
    if (
        not isinstance(no_candidate, list)
        or not all(isinstance(value, str) for value in no_candidate)
        or no_candidate != sorted(set(no_candidate))
        or not set(no_candidate).issubset(set(request["target_tile_ids"]))
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement no-candidate tiles are invalid"
        )
    limitations = artifact.get("limitations")
    if (
        not isinstance(limitations, list)
        or not limitations
        or any(
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            for value in limitations
        )
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement limitations are invalid"
        )
    if not isinstance(evidence_pack, Mapping):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence pack is invalid"
        )
    pack_sha = hashlib.sha256(
        canonical_json(dict(evidence_pack)).encode("utf-8")
    ).hexdigest()
    if pack_sha != request["source_evidence_pack_sha256"]:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence pack hash mismatch"
        )
    pack_rows = evidence_pack.get("evidence")
    if not isinstance(pack_rows, list):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence pack rows are invalid"
        )
    representatives = canonical_evidence_representatives(
        pack_rows,
        subject_url=str(request["subject_url"]),
    )
    expected_fingerprints = {
        str(row["evidence_fingerprint"])
        for row in request["evidence_workset"]
    }
    if not expected_fingerprints.issubset(representatives):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence is absent from its frozen pack"
        )
    expected_rows = _evidence_rows(
        [
            {
                "evidence_fingerprint": fingerprint,
                **dict(representatives[fingerprint]),
            }
            for fingerprint in sorted(expected_fingerprints)
        ]
    )
    expected_snapshot = [
        deepcopy(expected_rows[fingerprint])
        for fingerprint in sorted(expected_rows)
    ]
    if result["evidence_snapshot"] != expected_snapshot:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence snapshot differs from its pack"
        )


def build_coverage_supplement_source_candidate(
    artifact: Mapping[str, Any],
    *,
    evidence_pack: Mapping[str, Any],
    current_operational_memory: Mapping[str, Any],
) -> dict[str, Any]:
    """Project exact supplement relations into a pending-only v1 source."""

    validate_coverage_supplement_artifact(
        artifact,
        evidence_pack=evidence_pack,
    )
    current = deepcopy(dict(current_operational_memory))
    try:
        build_operational_score_evaluation(
            current,
            created_at="2000-01-01T00:00:00+00:00",
        )
    except EvidenceVaultOperationalScoringError as exc:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement source parent memory is invalid"
        ) from exc
    request = artifact["request"]
    result = artifact["result"]
    content = current.get("content")
    if (
        current.get("authority") is not True
        or current.get("authority_scope") != "b3s-vault"
        or current.get("lifecycle_state") != "active"
        or current.get("production_runtime_effect") is not False
        or current.get("scanner_runtime_effect") is not False
        or current.get("brand_identity") != request["brand_identity"]
        or current.get("canonical_memory_version")
        != request["parent_canonical_memory_version"]
        or not isinstance(content, Mapping)
        or not isinstance(content.get("accepted_tiles"), list)
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement source has a stale or invalid Vault parent"
        )
    aggregation = str(content.get("aggregation_policy_fingerprint") or "")
    if aggregation != canonical_aggregation_policy_fingerprint():
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement parent aggregation policy mismatch"
        )
    registry = build_tile_contract_registry()["tiles"]
    current_by_id = {
        str(row["tile_id"]): dict(row)
        for row in content["accepted_tiles"]
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
    new_by_tile: dict[str, list[dict[str, Any]]] = {}
    for relation in result["basis_relations"]:
        relation_id = str(relation["relation_id"])
        if relation_id in current_relation_ids:
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement relation already exists in its parent"
            )
        new_by_tile.setdefault(str(relation["tile_id"]), []).append(
            {
                "relation_id": relation_id,
                "evidence_id": str(relation["evidence_id"]),
                "source_identity_id": str(relation["source_identity_id"]),
                "claim_id": relation["claim_id"],
                "polarity": str(relation["polarity"]),
                "review_status": "unreviewed",
                "decision_event_id": None,
                "absence_test_contract_id": None,
                "coverage_assessment_id": None,
                "coverage_status": None,
                "tested_scope": None,
                "observed_result": None,
            }
        )
    if not new_by_tile:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement source has no pending relations"
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
                f"coverage-supplement:{result['result_fingerprint']}:{tile_id}"
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
            "details": {"source": "coverage_supplement"},
        }
        for candidate in candidates
        if candidate["candidate_state"] == TileState.CONTRADICTION.value
        for ref in candidate.get("unresolved_refs") or []
    ]
    identity = {
        "request_fingerprint": request["request_fingerprint"],
        "result_fingerprint": result["result_fingerprint"],
        "parent_canonical_memory_version": current[
            "canonical_memory_version"
        ],
        "basis_relations": result["basis_relations"],
    }
    packet = build_candidate_packet(
        brand_identity=str(request["brand_identity"]),
        parent_canonical_memory_version=str(
            current["canonical_memory_version"]
        ),
        candidate_memory_version=canonical_fingerprint(
            "evidence-vault-coverage-supplement-candidate-memory-v1",
            identity,
        ),
        accepted_memory_candidate_version=canonical_fingerprint(
            "evidence-vault-coverage-supplement-accepted-memory-v1",
            content,
        ),
        reviewed_memory_candidate_version=canonical_fingerprint(
            "evidence-vault-coverage-supplement-reviewed-memory-v1",
            {"accepted_tiles": content["accepted_tiles"]},
        ),
        review_packet_set_fingerprint=canonical_fingerprint(
            "evidence-vault-coverage-supplement-review-set-v1",
            [],
        ),
        aggregation_policy_fingerprint=aggregation,
        candidate_tiles=candidates,
        coverage_summary={
            "source": "coverage_supplement",
            "request_fingerprint": request["request_fingerprint"],
            "result_fingerprint": result["result_fingerprint"],
            "target_tile_ids": request["target_tile_ids"],
            "pending_relation_count": len(result["basis_relations"]),
        },
        unresolved_items=unresolved,
    )
    validate_candidate_packet(packet)
    return packet


def build_coverage_supplement_source_resolution(
    artifact: Mapping[str, Any],
    *,
    source_candidate_packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze the exact non-authoritative artifact behind a source packet."""

    source = deepcopy(dict(source_candidate_packet))
    validate_candidate_packet(source)
    request = artifact.get("request")
    result = artifact.get("result")
    if not isinstance(request, Mapping) or not isinstance(result, Mapping):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement resolution artifact is invalid"
        )
    if (
        source["manifest"]["brand_identity"]
        != request.get("brand_identity")
        or source["manifest"]["parent_canonical_memory_version"]
        != request.get("parent_canonical_memory_version")
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement source resolution lineage mismatch"
        )
    return {
        "schema_version": (
            EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_SOURCE_RESOLUTION_VERSION
        ),
        "source_kind": "coverage_supplement",
        "request_fingerprint": request["request_fingerprint"],
        "result_fingerprint": result["result_fingerprint"],
        "source_evidence_pack_sha256": request[
            "source_evidence_pack_sha256"
        ],
        "source_candidate_packet_fingerprint": source[
            "candidate_packet_fingerprint"
        ],
        "parent_canonical_memory_version": request[
            "parent_canonical_memory_version"
        ],
        "artifact": deepcopy(dict(artifact)),
    }


def validate_coverage_supplement_request(request: Mapping[str, Any]) -> None:
    fields = {
        "schema_version",
        "brand_identity",
        "subject_url",
        "parent_canonical_memory_version",
        "source_evidence_pack_sha256",
        "target_tile_ids",
        "evidence_workset",
        "pair_count",
        "rationale",
        "authority",
        "runtime_effect",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "request_fingerprint",
    }
    if not isinstance(request, Mapping) or set(request) != fields:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement request fields mismatch"
        )
    if (
        request.get("schema_version")
        != EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_REQUEST_VERSION
        or request.get("authority") is not False
        or request.get("runtime_effect") is not False
        or request.get("production_runtime_effect") is not False
        or request.get("scanner_runtime_effect") is not False
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement request authority is invalid"
        )
    brand = request.get("brand_identity")
    subject_url = request.get("subject_url")
    if (
        not isinstance(brand, str)
        or brand != brand.strip().lower()
        or not brand
        or not isinstance(subject_url, str)
        or subject_url != subject_url.strip()
        or not subject_url
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement request identity is invalid"
        )
    _validate_matching_identity(brand, subject_url)
    _sha256(
        request.get("parent_canonical_memory_version"),
        field="parent_canonical_memory_version",
    )
    _sha256(
        request.get("source_evidence_pack_sha256"),
        field="source_evidence_pack_sha256",
    )
    if request.get("rationale") != _text(
        request.get("rationale"), field="rationale", maximum=2000
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement rationale is not canonical"
        )
    registry_ids = {
        str(row["tile_id"]) for row in build_tile_contract_registry()["tiles"]
    }
    targets = request.get("target_tile_ids")
    if (
        not isinstance(targets, list)
        or not all(isinstance(value, str) for value in targets)
        or targets != sorted(set(targets))
        or not targets
        or not set(targets).issubset(registry_ids)
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement target tiles are invalid"
        )
    workset = request.get("evidence_workset")
    if (
        not isinstance(workset, list)
        or not workset
        or len(workset) > _MAX_EVIDENCE_ROWS
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence workset is invalid"
        )
    seen: set[str] = set()
    pair_count = 0
    for row in workset:
        if not isinstance(row, Mapping) or set(row) != {
            "evidence_fingerprint",
            "ref",
            "source",
            "evidence_type",
            "url",
            "source_class",
            "evidence_snapshot_fingerprint",
            "tile_ids",
        }:
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement workset row is invalid"
            )
        fingerprint = _sha256(
            row["evidence_fingerprint"],
            field="evidence_fingerprint",
        )
        _sha256(
            row["evidence_snapshot_fingerprint"],
            field="evidence_snapshot_fingerprint",
        )
        if fingerprint in seen:
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement repeats evidence"
            )
        seen.add(fingerprint)
        if (
            row["ref"] != _text(row["ref"], field="ref", maximum=1000)
            or row["source"]
            != _text(row["source"], field="source", maximum=200)
            or row["evidence_type"]
            != _text(
                row["evidence_type"],
                field="evidence_type",
                maximum=500,
            )
        ):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement evidence binding is not canonical"
            )
        if not isinstance(row["url"], str) or not isinstance(
            row["source_class"], str
        ):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement evidence binding is invalid"
            )
        tile_ids = row["tile_ids"]
        if (
            not isinstance(tile_ids, list)
            or not all(isinstance(value, str) for value in tile_ids)
            or tile_ids != sorted(set(tile_ids))
            or not tile_ids
            or not set(tile_ids).issubset(set(targets))
        ):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement shortlist is invalid"
            )
        pair_count += len(tile_ids)
    _validate_owned_evidence_identity(workset, subject_url=subject_url)
    if [row["evidence_fingerprint"] for row in workset] != sorted(seen):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement workset order is invalid"
        )
    if pair_count > _MAX_PAIRS or request.get("pair_count") != pair_count:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement pair count is invalid"
        )
    unsigned = {
        key: deepcopy(value)
        for key, value in request.items()
        if key != "request_fingerprint"
    }
    if request.get("request_fingerprint") != canonical_fingerprint(
        EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_REQUEST_VERSION,
        unsigned,
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement request fingerprint mismatch"
        )


def validate_coverage_supplement_result(
    result: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
) -> None:
    fields = {
        "schema_version",
        "request_fingerprint",
        "parent_canonical_memory_version",
        "target_tile_ids",
        "uncovered_target_tile_ids",
        "evidence_snapshot",
        "relation_proposal",
        "basis_relations",
        "authority",
        "runtime_effect",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "result_fingerprint",
    }
    proposal_value = result.get("relation_proposal") if isinstance(result, Mapping) else None
    proposal_version = proposal_value.get("schema_version") if isinstance(proposal_value, Mapping) else None
    if result.get("schema_version") == EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION:
        fields.add("relation_proposal_call_count")
    if (
        not isinstance(result, Mapping)
        or set(result) != fields
        or result.get("schema_version") not in {
            _LEGACY_COVERAGE_SUPPLEMENT_RESULT_VERSION,
            EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION,
        }
        or result.get("request_fingerprint")
        != request.get("request_fingerprint")
        or result.get("parent_canonical_memory_version")
        != request.get("parent_canonical_memory_version")
        or result.get("target_tile_ids") != request.get("target_tile_ids")
        or result.get("authority") is not False
        or result.get("runtime_effect") is not False
        or result.get("production_runtime_effect") is not False
        or result.get("scanner_runtime_effect") is not False
        or not isinstance(result.get("basis_relations"), list)
        or not isinstance(result.get("evidence_snapshot"), list)
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement result is invalid"
        )
    unsigned = {
        key: deepcopy(value)
        for key, value in result.items()
        if key != "result_fingerprint"
    }
    if result.get("result_fingerprint") != canonical_fingerprint(
        str(result["schema_version"]),
        unsigned,
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement result fingerprint mismatch"
        )
    evidence = _evidence_rows(list(result["evidence_snapshot"]))
    workset = {
        str(row["evidence_fingerprint"]): list(row["tile_ids"])
        for row in request["evidence_workset"]
    }
    if set(evidence) != set(workset):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement result evidence workset changed"
        )
    request_rows = {
        str(row["evidence_fingerprint"]): row
        for row in request["evidence_workset"]
    }
    for fingerprint, row in evidence.items():
        binding = request_rows[fingerprint]
        if (
            any(
                str(row[field]) != str(binding[field])
                for field in ("ref", "source", "evidence_type", "url")
            )
            or str(row.get("metadata", {}).get("source_class") or "")
            != str(binding["source_class"])
            or _evidence_snapshot_fingerprint(row)
            != binding["evidence_snapshot_fingerprint"]
        ):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement evidence binding changed"
            )
    proposal = result.get("relation_proposal")
    version_pair = (
        result.get("schema_version"),
        proposal.get("schema_version") if isinstance(proposal, Mapping) else None,
    )
    expected_proposal_fields = {
        "schema_version",
        "relations",
        "discarded_relations",
    }
    if version_pair == (
        EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION,
        EVIDENCE_TILE_RELATION_PROPOSAL_VERSION,
    ):
        expected_proposal_fields.add("analysis_states")
    if (
        not isinstance(proposal, Mapping)
        or set(proposal) != expected_proposal_fields
        or version_pair not in {
            (_LEGACY_COVERAGE_SUPPLEMENT_RESULT_VERSION, EVIDENCE_TILE_RELATION_LEGACY_PROPOSAL_VERSION),
            (EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION, EVIDENCE_TILE_RELATION_PROPOSAL_VERSION),
        }
        or not isinstance(proposal.get("relations"), list)
        or not isinstance(proposal.get("discarded_relations"), list)
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement relation proposal is invalid"
        )
    if proposal_version == EVIDENCE_TILE_RELATION_PROPOSAL_VERSION:
        analysis_states = proposal["analysis_states"]
        analyzed_fingerprints = {
            fingerprint for fingerprint, tile_ids in workset.items() if tile_ids
        }
        relation_fingerprints = {
            str(row.get("evidence_fingerprint") or "")
            for row in proposal["relations"]
            if isinstance(row, Mapping)
        }
        if (
            type(analysis_states) is not dict
            or set(analysis_states) != analyzed_fingerprints
            or any(
                type(state) is not str or state not in _RELATION_ANALYSIS_STATES
                for state in analysis_states.values()
            )
            or any(
                (fingerprint in relation_fingerprints)
                != (state == "supported")
                for fingerprint, state in analysis_states.items()
            )
        ):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement relation analysis is invalid"
            )
        contracts = {str(row["tile_id"]): row for row in build_tile_contract_registry()["tiles"]}
        calls = evidence_tile_relation_call_count(
            evidence_rows=list(result["evidence_snapshot"]),
            tile_shortlists={key: [contracts[tile] for tile in value] for key, value in workset.items()},
        )
        if type(result.get("relation_proposal_call_count")) is not int or result.get("relation_proposal_call_count") != calls or len(proposal["relations"]) + len(proposal["discarded_relations"]) > calls * 120:
            raise EvidenceVaultCoverageSupplementError("coverage supplement relation call count is invalid")
    discarded_fingerprints: set[str] = set()
    for discarded in proposal["discarded_relations"]:
        if (
            not isinstance(discarded, Mapping)
            or set(discarded)
            != {"reason", "submitted_relation_fingerprint"}
            or discarded.get("reason") not in _DISCARD_REASONS
        ):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement discarded relation audit is invalid"
            )
        submitted_fingerprint = _sha256(
            discarded["submitted_relation_fingerprint"],
            field="submitted_relation_fingerprint",
        )
        if submitted_fingerprint in discarded_fingerprints:
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement repeats discarded relation audit"
            )
        discarded_fingerprints.add(submitted_fingerprint)
    expected_basis = _basis_from_proposal(
        proposal,
        evidence=evidence,
        brand_identity=str(request["brand_identity"]),
        request_fingerprint=str(request["request_fingerprint"]),
        tile_shortlists=workset,
    )
    if result["basis_relations"] != expected_basis:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement basis is not derived from its proposal"
        )
    proposed_tiles = {row["tile_id"] for row in expected_basis}
    if result.get("uncovered_target_tile_ids") != sorted(
        set(request["target_tile_ids"]) - proposed_tiles
    ):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement uncovered tiles are invalid"
        )


def _basis_from_proposal(
    proposal: Mapping[str, Any],
    *,
    evidence: Mapping[str, Mapping[str, Any]],
    brand_identity: str,
    request_fingerprint: str,
    tile_shortlists: Mapping[str, list[str]],
) -> list[dict[str, Any]]:
    identities = {
        fingerprint: project_evidence_memory_row_identity(
            row,
            brand_domain=brand_identity,
        )
        for fingerprint, row in evidence.items()
    }
    if any(value is None for value in identities.values()):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence has no durable identity"
        )
    basis: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for relation in proposal["relations"]:
        if not isinstance(relation, Mapping) or set(relation) != {
            "evidence_fingerprint",
            "tile_id",
            "polarity",
            "literal_quote",
            "rationale",
        }:
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement relation fields mismatch"
            )
        if not all(isinstance(value, str) for value in relation.values()):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement relation values must be text"
            )
        fingerprint = _sha256(
            relation["evidence_fingerprint"],
            field="relation evidence fingerprint",
        )
        tile_id = relation["tile_id"].strip()
        polarity = relation["polarity"].strip()
        quote = relation["literal_quote"].strip()
        rationale = relation["rationale"].strip()
        if (
            fingerprint not in evidence
            or tile_id not in tile_shortlists.get(fingerprint, [])
            or polarity not in {"supports", "contradicts"}
            or not 8 <= len(quote) <= 320
            or quote not in str(evidence[fingerprint]["content"])
            or not rationale
            or len(rationale) > 1000
        ):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement relation exceeds its bounds"
            )
        key = (fingerprint, tile_id, polarity)
        if key in seen:
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement repeats relation proposals"
            )
        seen.add(key)
        identity = identities[fingerprint]
        assert identity is not None
        relation_id = canonical_fingerprint(
            "evidence-vault-coverage-supplement-relation-v1",
            {
                "request_fingerprint": request_fingerprint,
                "evidence_id": identity["evidence_id"],
                "source_identity_id": identity["document_id"],
                "tile_id": tile_id,
                "polarity": polarity,
                "literal_quote": quote,
            },
        )
        basis.append(
            {
                "tile_id": tile_id,
                "relation_id": relation_id,
                "evidence_id": identity["evidence_id"],
                "source_identity_id": identity["document_id"],
                "claim_id": None,
                "polarity": polarity,
                "literal_quote": quote,
                "rationale": rationale,
                "review_status": "unreviewed",
                "decision_event_id": None,
                "evidence_fingerprint": fingerprint,
            }
        )
    return sorted(basis, key=lambda row: (row["tile_id"], row["relation_id"]))


def _evidence_snapshot_fingerprint(row: Mapping[str, Any]) -> str:
    return canonical_fingerprint(
        "evidence-vault-coverage-supplement-evidence-snapshot-v1",
        deepcopy(dict(row)),
    )


def _identity_domain(value: str) -> str:
    candidate = value if "://" in value else f"https://{value}"
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement identity is invalid"
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
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement identity is invalid"
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
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement identity is invalid"
        )
    return domain


def _validate_matching_identity(brand_identity: str, subject_url: str) -> None:
    brand_domain = _identity_domain(brand_identity)
    if brand_identity != brand_domain or brand_domain != _identity_domain(subject_url):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement brand and subject identities mismatch"
        )


def _validate_owned_evidence_identity(
    rows: Any,
    *,
    subject_url: str,
) -> None:
    subject_domain = _identity_domain(subject_url)
    for row in rows:
        if row.get("source_class") == "owned_copy" or (
            isinstance(row.get("metadata"), Mapping)
            and row["metadata"].get("source_class") == "owned_copy"
        ):
            url = row.get("url")
            if not isinstance(url, str) or _identity_domain(url) != subject_domain:
                raise EvidenceVaultCoverageSupplementError(
                    "coverage supplement owned evidence identity mismatch"
                )


def _evidence_rows(
    rows: list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise EvidenceVaultCoverageSupplementError(
            "coverage supplement evidence rows must be a list"
        )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement evidence row must be an object"
            )
        fingerprint = _sha256(
            row.get("evidence_fingerprint"),
            field="evidence_fingerprint",
        )
        content = row.get("content")
        if not isinstance(content, str) or not content:
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement evidence content is invalid"
            )
        if fingerprint in result:
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement repeats evidence"
            )
        metadata = row.get("metadata") or {}
        source_class = metadata.get("source_class") if isinstance(
            metadata, Mapping
        ) else None
        if (
            not isinstance(metadata, Mapping)
            or source_class is not None
            and not isinstance(source_class, str)
            or not isinstance(row.get("url") or "", str)
        ):
            raise EvidenceVaultCoverageSupplementError(
                "coverage supplement evidence metadata is invalid"
            )
        result[fingerprint] = {
            "ref": _text(row.get("ref"), field="ref", maximum=1000),
            "source": _text(row.get("source"), field="source", maximum=200),
            "evidence_type": _text(
                row.get("evidence_type"),
                field="evidence_type",
                maximum=500,
            ),
            "url": str(row.get("url") or ""),
            "content": content,
            "confidence": str(row.get("confidence") or "medium"),
            "metadata": deepcopy(dict(metadata)),
            "evidence_fingerprint": fingerprint,
        }
    return result


def _sha256(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise EvidenceVaultCoverageSupplementError(
            f"{field} must be a SHA-256"
        )
    return value


def _text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise EvidenceVaultCoverageSupplementError(f"{field} must be text")
    text = value.strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise EvidenceVaultCoverageSupplementError(f"{field} is invalid")
    return text


__all__ = [
    "EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_ARTIFACT_VERSION",
    "EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_REQUEST_VERSION",
    "EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION",
    "EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_SOURCE_RESOLUTION_VERSION",
    "EvidenceVaultCoverageSupplementError",
    "build_coverage_supplement_request",
    "build_coverage_supplement_source_candidate",
    "build_coverage_supplement_source_resolution",
    "execute_coverage_supplement",
    "validate_coverage_supplement_artifact",
    "validate_coverage_supplement_request",
    "validate_coverage_supplement_result",
]
