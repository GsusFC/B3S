from __future__ import annotations

import base64
import csv
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid5

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.history.capture_observation import parse_capture_observation
from src.services.evidence_vault_c7_shadow_readiness import (
    C7_SHADOW_READINESS_VERSION,
    EvidenceVaultC7ShadowReadinessEvaluator,
)
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_tile,
    build_tile_contract_registry,
    canonical_fingerprint,
    canonical_json,
)
from src.services.evidence_vault_composite_group_lifecycle import (
    attest_active_composite_group,
)
from src.services.evidence_vault_exact_relation_supplement import (
    build_exact_relation_source_candidate,
    build_exact_relation_source_resolution,
    build_exact_relation_supplement_artifact,
)
from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan
from src.services.evidence_vault_operational_authority import (
    build_operational_adoption_event,
    project_adopted_operational_memory,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)
from src.services.evidence_vault_operational_review import (
    build_reviewed_operational_source,
)
from src.services.evidence_vault_operational_scoring import (
    build_operational_score_evaluation,
)
from src.services.evidence_vault_raw_capture import (
    DETERMINISTIC_EXTRACTOR_VERSION,
    build_signed_raw_capture,
    extract_deterministic_document,
    prepare_deterministic_document_for_signing,
    validate_signed_raw_capture,
)
from src.services.evidence_vault_raw_provenance import (
    C7_LIVE_FRESHNESS_POLICY_VERSION,
    PRE_RECEIPT_SNAPSHOT_VERSION,
    PUBLIC_KEY_REGISTRY_VERSION,
    RAW_ACQUISITION_RECEIPT_VERSION,
    DirectAcquisition,
    ExternalIdentityProvenance,
    PreReceiptSnapshot,
    ProviderApiAcquisition,
    PublicKeyRegistry,
    RawAcquisitionReceiptClaims,
    evidence_memory_source_identity_id,
    external_identity_provenance_fingerprint,
    pre_receipt_snapshot_sha256,
    public_key_registry_fingerprint,
    receipt_set_fingerprint,
    sign_raw_acquisition_receipt,
)


_ROOT = Path(__file__).parents[1]
_AUDIT = _ROOT / "audits/evidence_vault_field_validation_v1"
_FIXTURE = _ROOT / "fixtures/evidence_vault_field_validation_v1"
_ID_NAMESPACE = UUID("029f3475-73df-4cc4-95bc-6bc550c9e8d2")
_REPOSITORY_ID_NAMESPACE = UUID("3ef1b80c-e7b7-4fb3-95ad-fb9e03c59d52")
_BRAND = "causaprima.ai"
_BRAND_URL = "https://causaprima.ai"
_EXTERNAL_URL = "https://www.linkedin.com/company/causa-prima"
_WORKSPACE_SLUG = "b3s"
_KEY_ID = "shadow-ready-ed25519-v1"
_T0 = datetime(2026, 8, 9, 5, 0, tzinfo=timezone.utc)

_WATERMARK_VERSION = "evidence-vault-capture-watermark-event-v1"
_RAW_BINDING_VERSION = "evidence-vault-raw-evidence-binding-v1"
_MEMBER_VERSION = "evidence-vault-verified-c7-lineage-member-v1"
_MEMBER_SET_VERSION = "evidence-vault-verified-c7-lineage-member-set-v1"
_VERIFIED_BINDING_VERSION = "evidence-vault-verified-c7-lineage-binding-v1"
_DISPOSITION_VERSION = "evidence-vault-raw-provenance-disposition-event-v1"
_STORAGE_RESOLUTION_VERSION = "evidence-vault-operational-storage-resolution-v1"
_SOURCE_RESOLUTION_VERSION = "evidence-vault-operational-source-resolution-v1"
_REVIEWED_RESOLUTION_VERSION = "evidence-vault-operational-reviewed-resolution-v1"
_REVIEW_REQUEST_VERSION = "evidence-vault-operational-relation-review-request-v1"

_WORKSPACE_FIELDS = {"id", "slug", "name", "created_at"}
_BRAND_FIELDS = {
    "id", "workspace_id", "canonical_domain", "display_name", "canonical_url",
    "first_observed_at", "latest_observed_at", "created_at", "updated_at",
}
_SCAN_FIELDS = {
    "id", "workspace_id", "brand_id", "source_scan_id", "source_run_id", "status",
    "pipeline_version", "acquisition_state", "requested_at", "started_at", "completed_at",
    "recorded_at", "error_summary", "request_payload", "metadata",
}
_PLAN_FIELDS = {
    "id", "workspace_id", "brand_id", "scan_run_id", "observation_hash",
    "operation_plan_fingerprint", "canonical_memory_version", "mode", "status", "plan_payload",
    "attempt_count", "lease_owner", "lease_token", "lease_generation", "lease_expires_at",
    "claimed_at", "started_at", "heartbeat_at", "result_fingerprint", "result_payload",
    "result_persisted_at", "candidate_packet_fingerprint", "completed_at", "superseded_at",
    "last_error", "authority", "authority_scope", "production_runtime_effect",
    "scanner_runtime_effect", "created_at", "updated_at",
}
_CAPTURE_FIELDS = {
    "id", "scan_run_id", "brand_id", "observed_at", "recorded_at", "source_url",
    "content_hash", "acquisition_summary", "limitations", "raw_payload",
}
_EVIDENCE_FIELDS = {
    "id", "capture_id", "evidence_ref", "source", "source_class", "evidence_type", "url",
    "content", "content_raw", "content_hash", "confidence", "metadata",
}
_WATERMARK_FIELDS = {
    "id", "brand_id", "capture_id", "capture_sequence", "previous_event_id",
    "previous_event_fingerprint", "capture_content_hash", "capture_observation_hash",
    "append_origin", "event_schema_version", "event_fingerprint", "authority",
    "production_runtime_effect", "scanner_runtime_effect", "created_at",
}
_RECEIPT_FIELDS = {
    "id", "workspace_id", "brand_id", "scan_run_id", "source_scan_id", "capture_id",
    "watermark_event_id", "capture_sequence", "receipt_schema_version",
    "freshness_policy_version", "signature_schema_version", "key_id", "receipt_nonce",
    "acquisition_session_id", "workspace_slug", "canonical_brand", "channel_role", "provider",
    "acquisition_mode", "pre_receipt_snapshot_sha256", "source_url", "raw_fragment_pointer",
    "raw_fragment_sha256", "extracted_document_sha256", "extractor_version",
    "external_identity_provenance_fingerprint", "external_identity_provenance", "fetched_at",
    "received_at", "eligible_until", "claims", "receipt_fingerprint", "signed_payload",
    "signature", "created_at",
}
_RAW_BINDING_FIELDS = {
    "id", "workspace_id", "brand_id", "scan_run_id", "capture_id", "receipt_id",
    "channel_role", "evidence_record_id", "evidence_ref", "source_url",
    "extractor_schema_version", "extractor_version", "extracted_document",
    "extracted_document_sha256", "passage_locator", "passage_text", "passage_sha256",
    "evidence_record_content_hash", "binding_fingerprint", "created_at",
}
_VERIFIED_BINDING_FIELDS = {
    "id", "workspace_id", "brand_id", "scan_run_id", "capture_id", "watermark_event_id",
    "capture_sequence", "operational_source_packet_id", "operation_plan_id", "composite_group_id",
    "canonical_brand", "source_packet_fingerprint", "operation_plan_fingerprint",
    "exact_artifact_fingerprint", "freshness_policy_version", "public_key_registry_fingerprint",
    "provenance", "receipt_set_fingerprint", "member_set_fingerprint", "eligible_until",
    "binding_schema_version", "binding_fingerprint", "authority", "production_runtime_effect",
    "scanner_runtime_effect", "created_at",
}
_MEMBER_FIELDS = {
    "id", "binding_id", "workspace_id", "brand_id", "scan_run_id", "capture_id",
    "operational_source_packet_id", "composite_group_id", "raw_evidence_binding_id",
    "receipt_id", "evidence_record_id", "channel_role", "relation_id", "evidence_id",
    "source_identity_id", "evidence_fingerprint", "evidence_ref", "source_ref",
    "evidence_quote", "member_fingerprint", "created_at",
}
_DISPOSITION_FIELDS = {
    "id", "binding_id", "workspace_id", "brand_id", "scan_run_id", "capture_id",
    "operational_source_packet_id", "composite_group_id", "receipt_set_fingerprint",
    "event_sequence", "previous_event_id", "previous_event_fingerprint", "action", "prior_state",
    "resulting_state", "reason", "actor_id", "idempotency_key_hash", "event_schema_version",
    "event_fingerprint", "occurred_at",
}
_PACKET_FIELDS = {
    "id", "brand_id", "packet_fingerprint", "schema_version", "brand_identity",
    "parent_canonical_memory_version", "reference_resolution_fingerprint", "reference_resolution",
    "manifest", "candidate_tiles", "authority_state", "authority", "production_runtime_effect",
    "scanner_runtime_effect", "created_at", "packet_kind", "packet_payload",
    "accepted_memory_candidate_version", "candidate_overlay_version",
}
_ADOPTION_FIELDS = {
    "id", "brand_id", "event_type", "sequence", "previous_event_id", "brand_identity",
    "candidate_packet_fingerprint", "reference_resolution_fingerprint",
    "promotion_policy_fingerprint", "parent_canonical_memory_version",
    "promoted_canonical_memory_version", "decision", "reviewer_id", "reviewed_at", "rationale",
    "schema_version", "idempotency_key_hash", "request_fingerprint", "authority",
    "authority_scope", "production_runtime_effect", "scanner_runtime_effect", "created_at",
    "adoption_kind", "adopted_by", "actor_id", "event_payload",
}
_REVIEW_FIELDS = {
    "id", "brand_id", "source_packet_id", "source_packet_fingerprint", "relation_id", "decision",
    "reviewer_id", "rationale", "review_request_fingerprint", "authority", "authority_scope",
    "production_runtime_effect", "scanner_runtime_effect", "created_at",
}
_SCORE_FIELDS = {
    "id", "brand_id", "promotion_event_id", "canonical_memory_version", "evaluation_identity",
    "score_input_fingerprint", "derived_tile_state_fingerprint", "rubric_version",
    "tile_contract_registry_fingerprint", "reducer_policy_fingerprint",
    "aggregation_policy_fingerprint", "schema_version", "score", "component_breakdown",
    "base_average", "magnetism_capped", "reused_from_evaluation_identity", "authority",
    "authority_scope", "production_runtime_effect", "scanner_runtime_effect", "created_at",
    "evaluation_kind", "authority_coverage", "evaluation_payload",
}


@dataclass(frozen=True)
class ReadyFixture:
    evaluator: EvidenceVaultC7ShadowReadinessEvaluator
    snapshot: dict[str, Any]
    registry: dict[str, Any]


def build_verified_raw_ready_fixture() -> ReadyFixture:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    registry = _registry(private_key)
    registry_fingerprint = public_key_registry_fingerprint(registry)
    authority = _authority_rows()

    workspace_id = _repository_uuid("workspace", _WORKSPACE_SLUG)
    brand_id = _repository_uuid(workspace_id, "brand", _BRAND)
    workspace = _strict_row(_WORKSPACE_FIELDS, {
        "id": workspace_id,
        "slug": _WORKSPACE_SLUG,
        "name": "B3S",
        "created_at": _iso(_T0 - timedelta(days=1)),
    })
    brand = _strict_row(_BRAND_FIELDS, {
        "id": brand_id,
        "workspace_id": workspace_id,
        "canonical_domain": _BRAND,
        "display_name": "Causa Prima",
        "canonical_url": _BRAND_URL,
        "first_observed_at": _iso(_T0),
        "latest_observed_at": _iso(_T0 + timedelta(minutes=6)),
        "created_at": _iso(_T0),
        "updated_at": _iso(_T0 + timedelta(minutes=6)),
    })

    captures: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for sequence, acquired_at in ((1, _T0), (2, _T0 + timedelta(minutes=6))):
        capture = _capture_proof(
            sequence=sequence,
            acquired_at=acquired_at,
            previous_watermark=previous,
            private_key=private_key,
            registry=registry,
            registry_fingerprint=registry_fingerprint,
            authority=authority,
            workspace=workspace,
            brand=brand,
        )
        captures.append(capture)
        previous = capture["watermark"]

    context = {
        "schema_version": C7_SHADOW_READINESS_VERSION,
        "database_time": _iso(_T0 + timedelta(minutes=7)),
        "workspace_slug": _WORKSPACE_SLUG,
        "brand_match_count": 1,
        "brand": brand,
        "watermark_count": 2,
        "watermarks": [row["watermark"] for row in captures],
        "lineage_binding_count": 2,
        "lineage_bindings": [row["binding"] for row in captures],
        "operational_adoption_count": len(authority["adoptions"]),
        "operational_adoptions": authority["adoptions"],
        "operational_packet_count": len(authority["operational_packets"]),
        "operational_packets": authority["operational_packets"],
        "source_packet_count": len(authority["source_packets"]),
        "source_packets": authority["source_packets"],
        "relation_review_count": len(authority["reviews"]),
        "relation_reviews": authority["reviews"],
        "operational_score_count": 1,
        "operational_scores": [authority["score"]],
        "overflow": {
            "adoptions": False,
            "operational_packets": False,
            "source_packets": False,
            "reviews": False,
            "scores": False,
            "bindings": False,
        },
    }
    snapshot = {
        "context": context,
        "proofs": [row["proof"] for row in captures],
    }
    evaluator = EvidenceVaultC7ShadowReadinessEvaluator(
        public_key_registry=registry,
        expected_public_key_registry_fingerprint=registry_fingerprint,
    )
    return ReadyFixture(evaluator=evaluator, snapshot=snapshot, registry=registry)


def clone_ready_snapshot(fixture: ReadyFixture) -> dict[str, Any]:
    return deepcopy(fixture.snapshot)


def append_disposition_event(
    snapshot: dict[str, Any],
    *,
    proof_index: int,
    action: str,
) -> None:
    provenance = snapshot["proofs"][proof_index]["provenance"]
    binding = provenance["binding"]
    previous = provenance["dispositions"][-1]
    transitions = {
        ("retained", "place_legal_hold"): "legal_hold",
        ("legal_hold", "release_legal_hold"): "retained",
        ("retained", "revoke_runtime"): "runtime_revoked",
        ("legal_hold", "revoke_runtime"): "runtime_revoked",
    }
    prior_state = previous["resulting_state"]
    resulting_state = transitions[(prior_state, action)]
    sequence = previous["event_sequence"] + 1
    event_id = _uuid(f"disposition-{proof_index}-{sequence}-{action}")
    occurred_at = _T0 + timedelta(minutes=proof_index * 6, seconds=30 * (sequence - 1))
    identity = {
        "schema_version": _DISPOSITION_VERSION,
        "binding_fingerprint": binding["binding_fingerprint"],
        "receipt_set_fingerprint": binding["receipt_set_fingerprint"],
        "event_sequence": sequence,
        "previous_event_fingerprint": previous["event_fingerprint"],
        "action": action,
        "prior_state": prior_state,
        "resulting_state": resulting_state,
        "reason": f"fixture_{action}",
        "actor_id": "shadow-fixture-governance",
        "idempotency_key_hash": _digest(f"{binding['id']}:{sequence}:{action}"),
    }
    provenance["dispositions"].append(_strict_row(_DISPOSITION_FIELDS, {
        "id": event_id,
        "binding_id": binding["id"],
        "workspace_id": binding["workspace_id"],
        "brand_id": binding["brand_id"],
        "scan_run_id": binding["scan_run_id"],
        "capture_id": binding["capture_id"],
        "operational_source_packet_id": binding["operational_source_packet_id"],
        "composite_group_id": binding["composite_group_id"],
        "receipt_set_fingerprint": binding["receipt_set_fingerprint"],
        "event_sequence": sequence,
        "previous_event_id": previous["id"],
        "previous_event_fingerprint": previous["event_fingerprint"],
        "action": action,
        "prior_state": prior_state,
        "resulting_state": resulting_state,
        "reason": identity["reason"],
        "actor_id": identity["actor_id"],
        "idempotency_key_hash": identity["idempotency_key_hash"],
        "event_schema_version": _DISPOSITION_VERSION,
        "event_fingerprint": canonical_fingerprint(_DISPOSITION_VERSION, identity),
        "occurred_at": _iso(occurred_at),
    }))
    provenance["disposition_count"] = len(provenance["dispositions"])


def _authority_rows() -> dict[str, Any]:
    workspace_id = _repository_uuid("workspace", _WORKSPACE_SLUG)
    brand_id = _repository_uuid(workspace_id, "brand", _BRAND)
    baseline_basis = {
        "relation_id": _digest("baseline-p1-relation"),
        "evidence_id": _digest("baseline-p1-evidence"),
        "source_identity_id": _digest("baseline-p1-source"),
        "claim_id": None,
        "polarity": "supports",
        "review_status": "accepted",
        "decision_event_id": _uuid("baseline-p1-review"),
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }
    candidates = [
        build_candidate_tile(
            tile_id=contract["tile_id"],
            basis=[baseline_basis] if contract["tile_id"] == "P1" else [],
        )
        for contract in build_tile_contract_registry()["tiles"]
    ]
    baseline_packet = build_operational_memory_packet(
        brand_identity=_BRAND,
        source_candidate_packet_fingerprint=_digest("baseline-source"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions={
            "P1": {
                "authority_state": "accepted",
                "review_state": "resolved",
                "authority_profile_id": "human-reviewed-relation-v1",
                "authority_source": "human",
                "decision_event_id": baseline_basis["decision_event_id"],
            }
        },
    )
    baseline_event = build_operational_adoption_event(
        baseline_packet,
        event_id=_repository_uuid(
            brand_id,
            "evidence-vault-operational-adoption",
            _digest("baseline-adoption-idempotency"),
        ),
        sequence=1,
        previous_event_id=None,
        adopted_by="human",
        actor_id="fixture-reviewer",
        policy_fingerprint=_digest("baseline-policy"),
        created_at=_iso(_T0 - timedelta(days=2)),
        idempotency_key_hash=_digest("baseline-adoption-idempotency"),
        expected_current_canonical_memory_version=None,
    )
    baseline = project_adopted_operational_memory(baseline_packet, baseline_event)

    assessment = json.loads(
        (_AUDIT / "causa-prima-missing-tile-independent-audit.json").read_text(encoding="utf-8")
    )
    worksheet = _AUDIT / "causa-prima-missing-tile-independent-review.csv"
    with worksheet.open(encoding="utf-8", newline="") as handle:
        review_rows = list(csv.DictReader(handle))
    pack = json.loads(
        (_FIXTURE / "causa_prima-a8ba05137817-normalized-evidence-pack.json").read_text(encoding="utf-8")
    )
    artifact = build_exact_relation_supplement_artifact(
        brand_identity=_BRAND,
        subject_url=_BRAND_URL,
        parent_canonical_memory_version=baseline["canonical_memory_version"],
        selected_tile_ids=["C7"],
        assessment_artifact=assessment,
        assessment_review_rows=review_rows,
        assessment_worksheet_sha256=hashlib.sha256(worksheet.read_bytes()).hexdigest(),
        evidence_pack=pack,
    )
    exact_source = build_exact_relation_source_candidate(
        artifact,
        current_operational_memory=baseline,
    )
    c7_source = next(row for row in exact_source["candidate_tiles"] if row["tile_id"] == "C7")
    relation_ids = sorted(row["relation_id"] for row in c7_source["basis"])
    reviewer_id = "fixture-reviewer"
    reviewed_at = _iso(_T0 - timedelta(days=1, minutes=5))
    normalized_request_decisions = {
        relation_id: {
            "decision": "accept",
            "rationale": "Exact C7 all-of relation accepted for the shadow fixture.",
        }
        for relation_id in relation_ids
    }
    request_fingerprint = canonical_fingerprint(
        _REVIEW_REQUEST_VERSION,
        {
            "source_candidate_packet_fingerprint": exact_source["candidate_packet_fingerprint"],
            "reviewer_id": reviewer_id,
            "reviewed_at": reviewed_at,
            "decisions": normalized_request_decisions,
        },
    )
    decision_ids = {
        relation_id: _repository_uuid(
            brand_id,
            "evidence-vault-operational-relation-review",
            exact_source["candidate_packet_fingerprint"],
            relation_id,
            request_fingerprint,
        )
        for relation_id in relation_ids
    }
    decisions = {
        relation_id: {"decision": "accept", "decision_event_id": decision_ids[relation_id]}
        for relation_id in relation_ids
    }
    reviewed_source, accepted_packet = build_reviewed_operational_source(
        exact_source,
        decisions=decisions,
        current_operational_memory=baseline,
    )
    accepted_event = build_operational_adoption_event(
        accepted_packet,
        event_id=_repository_uuid(
            brand_id,
            "evidence-vault-operational-adoption",
            _digest("accepted-adoption-idempotency"),
        ),
        sequence=2,
        previous_event_id=baseline_event["event_id"],
        adopted_by="human",
        actor_id="fixture-reviewer",
        policy_fingerprint=_digest("accepted-policy"),
        created_at=_iso(_T0 - timedelta(days=1)),
        idempotency_key_hash=_digest("accepted-adoption-idempotency"),
        expected_current_canonical_memory_version=baseline["canonical_memory_version"],
    )
    current = project_adopted_operational_memory(accepted_packet, accepted_event)

    exact_resolution = build_exact_relation_source_resolution(
        artifact,
        source_candidate_packet=exact_source,
    )
    exact_source_id = _repository_uuid(
        brand_id,
        "evidence-vault-exact-relation-supplement-source-packet",
        exact_source["candidate_packet_fingerprint"],
        canonical_fingerprint(
            "evidence-vault-operational-source-resolution-v1",
            exact_resolution,
        ),
    )
    reviewed_source_id = _repository_uuid(
        brand_id,
        "evidence-vault-operational-reviewed-packet",
        reviewed_source["candidate_packet_fingerprint"],
    )
    reviewed_resolution = {
        "schema_version": _REVIEWED_RESOLUTION_VERSION,
        "source_candidate_packet_fingerprint": exact_source["candidate_packet_fingerprint"],
        "review_request_fingerprint": request_fingerprint,
        "decision_event_ids": sorted(decision_ids.values()),
    }
    reviews = [
        _strict_row(_REVIEW_FIELDS, {
            "id": decision_ids[relation_id],
            "brand_id": brand_id,
            "source_packet_id": exact_source_id,
            "source_packet_fingerprint": exact_source["candidate_packet_fingerprint"],
            "relation_id": relation_id,
            "decision": "accept",
            "reviewer_id": reviewer_id,
            "rationale": normalized_request_decisions[relation_id]["rationale"],
            "review_request_fingerprint": request_fingerprint,
            "authority": True,
            "authority_scope": "relation_review",
            "production_runtime_effect": False,
            "scanner_runtime_effect": False,
            "created_at": reviewed_at,
        })
        for relation_id in relation_ids
    ]
    exact_source_row = _source_packet_row(
        packet_id=exact_source_id,
        brand_id=brand_id,
        packet=exact_source,
        packet_kind="operational_source_v2",
        resolution=exact_resolution,
        created_at=_iso(_T0 - timedelta(days=1, minutes=10)),
    )
    reviewed_source_row = _source_packet_row(
        packet_id=reviewed_source_id,
        brand_id=brand_id,
        packet=reviewed_source,
        packet_kind="operational_reviewed_v2",
        resolution=reviewed_resolution,
        created_at=reviewed_at,
    )
    baseline_packet_row = _operational_packet_row(
        packet_id=_repository_uuid(
            brand_id,
            "evidence-vault-operational-memory-packet",
            baseline_packet["candidate_packet_fingerprint"],
        ),
        brand_id=brand_id,
        packet=baseline_packet,
        created_at=baseline_event["created_at"],
    )
    accepted_packet_row = _operational_packet_row(
        packet_id=_repository_uuid(
            brand_id,
            "evidence-vault-operational-memory-packet",
            accepted_packet["candidate_packet_fingerprint"],
        ),
        brand_id=brand_id,
        packet=accepted_packet,
        created_at=accepted_event["created_at"],
    )
    baseline_event_row = _adoption_row(
        event=baseline_event,
        brand_id=brand_id,
        reference_resolution_fingerprint=baseline_packet_row["reference_resolution_fingerprint"],
    )
    accepted_event_row = _adoption_row(
        event=accepted_event,
        brand_id=brand_id,
        reference_resolution_fingerprint=accepted_packet_row["reference_resolution_fingerprint"],
    )
    score = build_operational_score_evaluation(
        current,
        created_at=_iso(_T0 - timedelta(hours=1)),
    )
    score_row = _strict_row(_SCORE_FIELDS, {
        "id": _repository_uuid(
            brand_id,
            "evidence-vault-operational-score",
            score["evaluation_identity"],
        ),
        "brand_id": brand_id,
        "promotion_event_id": current["adoption_event_id"],
        "canonical_memory_version": score["canonical_memory_version"],
        "evaluation_identity": score["evaluation_identity"],
        "score_input_fingerprint": score["score_input_fingerprint"],
        "derived_tile_state_fingerprint": score["derived_tile_state_fingerprint"],
        "rubric_version": score["rubric_version"],
        "tile_contract_registry_fingerprint": score["tile_contract_registry_fingerprint"],
        "reducer_policy_fingerprint": score["reducer_policy_fingerprint"],
        "aggregation_policy_fingerprint": score["aggregation_policy_fingerprint"],
        "schema_version": score["schema_version"],
        "score": score["score"],
        "component_breakdown": score["component_breakdown"],
        "base_average": score["base_average"],
        "magnetism_capped": score["magnetism_capped"],
        "reused_from_evaluation_identity": score["reused_from_evaluation_identity"],
        "authority": True,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": score["created_at"],
        "evaluation_kind": "operational_v2",
        "authority_coverage": score["authority_coverage"],
        "evaluation_payload": score,
    })
    attestation = attest_active_composite_group(
        current_operational_memory=current,
        exact_source_record={
            "packet": exact_source,
            "reference_resolution": exact_resolution,
            "authority": False,
            "authority_scope": "b3s-vault",
            "production_runtime_effect": False,
            "scanner_runtime_effect": False,
        },
    )
    return {
        "current": current,
        "attestation": attestation,
        "artifact": artifact,
        "exact_source": exact_source,
        "exact_source_id": exact_source_id,
        "exact_resolution": exact_resolution,
        "adoptions": [baseline_event_row, accepted_event_row],
        "operational_packets": [baseline_packet_row, accepted_packet_row],
        "source_packets": [exact_source_row, reviewed_source_row],
        "reviews": reviews,
        "score": score_row,
    }


def _capture_proof(
    *,
    sequence: int,
    acquired_at: datetime,
    previous_watermark: Mapping[str, Any] | None,
    private_key: Ed25519PrivateKey,
    registry: Mapping[str, Any],
    registry_fingerprint: str,
    authority: Mapping[str, Any],
    workspace: Mapping[str, Any],
    brand: Mapping[str, Any],
) -> dict[str, Any]:
    workspace_id = str(workspace["id"])
    brand_id = str(brand["id"])
    source_scan_id = f"shadow-ready-scan-{sequence}"
    scan_id = _repository_uuid(workspace_id, "scan", source_scan_id)
    capture_id = _repository_uuid(scan_id, "capture")
    binding_id = _uuid(f"verified-binding-{sequence}")
    session_id = _uuid(f"acquisition-session-{sequence}")
    c7_group = next(row for row in authority["artifact"]["groups"] if row["tile_id"] == "C7")
    relations = {row["channel_role"]: row for row in c7_group["relations"]}

    raw_without_provenance = {
        "sources": {
            "owned": {
                "url": _BRAND_URL,
                "linkedin": _EXTERNAL_URL,
                "markdown_content": relations["owned_web"]["literal_quote"],
            },
            "external": {
                "url": _EXTERNAL_URL,
                "title": "",
                "summary": "",
                "highlights": [],
                "text": relations["external_social_profile"]["literal_quote"],
            },
        }
    }
    snapshot = PreReceiptSnapshot(
        schema_version=PRE_RECEIPT_SNAPSHOT_VERSION,
        workspace_slug=_WORKSPACE_SLUG,
        source_scan_id=source_scan_id,
        acquisition_session_id=session_id,
        canonical_brand_domain=_BRAND,
        canonical_brand_url=_BRAND_URL,
        raw_payload=raw_without_provenance,
    )
    owned_fragment = raw_without_provenance["sources"]["owned"]
    external_fragment = raw_without_provenance["sources"]["external"]
    owned_extraction = prepare_deterministic_document_for_signing(
        channel_role="owned_web",
        raw_fragment=owned_fragment,
    )
    external_extraction = prepare_deterministic_document_for_signing(
        channel_role="external_social_profile",
        raw_fragment=external_fragment,
    )
    fetched_at = _iso_z(acquired_at)
    pre_snapshot_hash = pre_receipt_snapshot_sha256(snapshot)
    owned_claims = RawAcquisitionReceiptClaims(
        schema_version=RAW_ACQUISITION_RECEIPT_VERSION,
        freshness_policy_version=C7_LIVE_FRESHNESS_POLICY_VERSION,
        key_id=_KEY_ID,
        receipt_nonce=_uuid(f"owned-nonce-{sequence}"),
        acquisition_session_id=session_id,
        workspace_slug=_WORKSPACE_SLUG,
        source_scan_id=source_scan_id,
        canonical_brand_domain=_BRAND,
        channel_role="owned_web",
        pre_receipt_snapshot_sha256=pre_snapshot_hash,
        provider="direct_http",
        acquisition=DirectAcquisition(
            acquisition_mode="direct_http",
            requested_url=_BRAND_URL,
            redirect_chain=[],
            final_url=_BRAND_URL,
        ),
        fetched_at=fetched_at,
        status_code=200,
        selected_headers={"content-type": "text/html; charset=utf-8"},
        media_type="text/html",
        byte_count=len(canonical_json(owned_fragment).encode("utf-8")),
        raw_fragment_json_pointer="/sources/owned",
        raw_fragment_sha256=_sha_json(owned_fragment),
        extracted_document_sha256=owned_extraction.sha256,
        extractor_version=DETERMINISTIC_EXTRACTOR_VERSION,
        external_identity_provenance_fingerprint=None,
    )
    owned_receipt = sign_raw_acquisition_receipt(
        owned_claims,
        private_key=private_key,
        public_key_registry=registry,
    )
    association_dict = {
        "schema_version": "external-identity-provenance-v1",
        "policy_version": "evidence-vault-external-identity-association-policy-v1",
        "association_method": "owned_raw_links_external_profile",
        "canonical_brand_domain": _BRAND,
        "owned_source_url": _BRAND_URL,
        "external_source_url": _EXTERNAL_URL,
        "proof_receipt_fingerprint": owned_receipt.receipt_fingerprint,
        "raw_fact_role": "owned_web",
        "raw_fact_json_pointer": "/sources/owned/linkedin",
        "raw_fact_sha256": _sha_json(_EXTERNAL_URL),
        "source_identity_schema_version": "evidence-memory-document-v2",
        "owned_source_identity_id": evidence_memory_source_identity_id(
            source_url=_BRAND_URL,
            raw_fact_role="owned_web",
        ),
        "external_source_identity_id": evidence_memory_source_identity_id(
            source_url=_EXTERNAL_URL,
            raw_fact_role="external_social_profile",
        ),
    }
    association = ExternalIdentityProvenance.model_validate(association_dict, strict=True)
    external_claims = RawAcquisitionReceiptClaims(
        schema_version=RAW_ACQUISITION_RECEIPT_VERSION,
        freshness_policy_version=C7_LIVE_FRESHNESS_POLICY_VERSION,
        key_id=_KEY_ID,
        receipt_nonce=_uuid(f"external-nonce-{sequence}"),
        acquisition_session_id=session_id,
        workspace_slug=_WORKSPACE_SLUG,
        source_scan_id=source_scan_id,
        canonical_brand_domain=_BRAND,
        channel_role="external_social_profile",
        pre_receipt_snapshot_sha256=pre_snapshot_hash,
        provider="exa",
        acquisition=ProviderApiAcquisition(
            acquisition_mode="provider_api",
            provider_request_fingerprint=_digest(f"provider-request-{sequence}"),
            result_ordinal=0,
            reported_source_url=_EXTERNAL_URL,
            redirect_chain=[],
        ),
        fetched_at=fetched_at,
        status_code=200,
        selected_headers={"content-type": "application/json"},
        media_type="application/json",
        byte_count=len(canonical_json(external_fragment).encode("utf-8")),
        raw_fragment_json_pointer="/sources/external",
        raw_fragment_sha256=_sha_json(external_fragment),
        extracted_document_sha256=external_extraction.sha256,
        extractor_version=DETERMINISTIC_EXTRACTOR_VERSION,
        external_identity_provenance_fingerprint=external_identity_provenance_fingerprint(association),
    )
    external_receipt = sign_raw_acquisition_receipt(
        external_claims,
        private_key=private_key,
        public_key_registry=registry,
    )
    signed_capture = build_signed_raw_capture(
        snapshot,
        [owned_receipt, external_receipt],
        public_key_registry=registry,
        external_identity_provenance=association,
    )
    verified_capture = validate_signed_raw_capture(
        signed_capture.raw_payload,
        capture_content_hash=signed_capture.capture_content_hash,
        public_key_registry=registry,
    )
    receipts = {
        receipt.claims.channel_role: receipt
        for receipt in (owned_receipt, external_receipt)
    }

    evidence_rows: list[dict[str, Any]] = []
    receipt_rows: list[dict[str, Any]] = []
    raw_binding_rows: list[dict[str, Any]] = []
    for role in ("owned_web", "external_social_profile"):
        receipt = receipts[role]
        extraction = extract_deterministic_document(
            verified_capture,
            receipt_fingerprint=receipt.receipt_fingerprint,
        )
        evidence_ref = f"raw-acquisition:{role}:{receipt.receipt_fingerprint}"
        evidence_id = _repository_uuid(capture_id, "evidence", evidence_ref)
        receipt_id = _repository_uuid(
            capture_id,
            "evidence-vault-raw-acquisition-receipt-v1",
            receipt.receipt_fingerprint,
        )
        source_url = _BRAND_URL if role == "owned_web" else _EXTERNAL_URL
        content_hash = hashlib.sha256(extraction.document.encode("utf-8")).hexdigest()
        evidence_row = _strict_row(_EVIDENCE_FIELDS, {
            "id": evidence_id,
            "capture_id": capture_id,
            "evidence_ref": evidence_ref,
            "source": role,
            "source_class": "owned_copy" if role == "owned_web" else "external_proof",
            "evidence_type": "text",
            "url": source_url,
            "content": extraction.document,
            "content_raw": None,
            "content_hash": content_hash,
            "confidence": "high",
            "metadata": {
                "source_class": "owned_copy" if role == "owned_web" else "external_proof",
                "receipt_fingerprint": receipt.receipt_fingerprint,
            },
        })
        evidence_rows.append(evidence_row)
        locator = {
            "kind": "utf8_byte_range",
            "extracted_start": 0,
            "extracted_end": len(extraction.document.encode("utf-8")),
            "evidence_start": 0,
            "evidence_end": len(extraction.document.encode("utf-8")),
        }
        raw_identity = {
            "receipt_fingerprint": receipt.receipt_fingerprint,
            "evidence_record_id": evidence_id,
            "evidence_ref": evidence_ref,
            "source_url": source_url,
            "channel_role": role,
            "extractor_schema_version": DETERMINISTIC_EXTRACTOR_VERSION,
            "extractor_version": DETERMINISTIC_EXTRACTOR_VERSION,
            "extracted_document_sha256": extraction.sha256,
            "passage_locator": locator,
            "passage_sha256": content_hash,
            "evidence_record_content_hash": content_hash,
        }
        raw_binding_fingerprint = canonical_fingerprint(_RAW_BINDING_VERSION, raw_identity)
        raw_binding_id = _repository_uuid(
            capture_id,
            _RAW_BINDING_VERSION,
            raw_binding_fingerprint,
        )
        raw_binding_rows.append(_strict_row(_RAW_BINDING_FIELDS, {
            "id": raw_binding_id,
            "workspace_id": workspace_id,
            "brand_id": brand_id,
            "scan_run_id": scan_id,
            "capture_id": capture_id,
            "receipt_id": receipt_id,
            "channel_role": role,
            "evidence_record_id": evidence_id,
            "evidence_ref": evidence_ref,
            "source_url": source_url,
            "extractor_schema_version": DETERMINISTIC_EXTRACTOR_VERSION,
            "extractor_version": DETERMINISTIC_EXTRACTOR_VERSION,
            "extracted_document": extraction.document,
            "extracted_document_sha256": extraction.sha256,
            "passage_locator": locator,
            "passage_text": extraction.document,
            "passage_sha256": content_hash,
            "evidence_record_content_hash": content_hash,
            "binding_fingerprint": raw_binding_fingerprint,
            "created_at": _iso(acquired_at),
        }))

    observation_evidence = [
        {
            "ref": row["evidence_ref"],
            "source": row["source"],
            "evidence_type": row["evidence_type"],
            "url": row["url"],
            "content": row["content"],
            "confidence": row["confidence"],
            "metadata": row["metadata"],
        }
        for row in evidence_rows
    ]
    raw_observation = {
        "schema_version": "b3s-capture-observation-v1",
        "source_scan_id": source_scan_id,
        "source_run_id": "",
        "brand_name": "Causa Prima",
        "url": _BRAND_URL,
        "observed_at": _iso_z(acquired_at),
        "recorded_at": _iso_z(acquired_at),
        "pipeline_version": "evidence-vault-trusted-acquisition-v1",
        "acquisition_state": "completed",
        "acquisition_summary": {},
        "limitations": [],
        "capture_payload": signed_capture.raw_payload,
        "evidence_records": observation_evidence,
        "acquisition_attempts": [],
        "artifacts": [],
        "metadata": {},
    }
    observation = parse_capture_observation(raw_observation)
    operation_plan = build_vault_scan_plan(
        brand_identity=_BRAND,
        subject_url=_BRAND_URL,
        mode="incremental_refresh",
        current_evidence_records=observation_evidence,
        previous_capture_evidence_records=observation_evidence if sequence == 2 else (),
        known_evidence_records=observation_evidence if sequence == 2 else (),
        canonical_memory_version=authority["current"]["canonical_memory_version"],
    )
    plan_id = _repository_uuid(
        scan_id,
        "evidence-vault-operation-plan",
        operation_plan["operation_plan_fingerprint"],
    )
    plan_row = _strict_row(_PLAN_FIELDS, {
        "id": plan_id,
        "workspace_id": workspace_id,
        "brand_id": brand_id,
        "scan_run_id": scan_id,
        "observation_hash": observation.observation_hash,
        "operation_plan_fingerprint": operation_plan["operation_plan_fingerprint"],
        "canonical_memory_version": operation_plan["canonical_memory_version"],
        "mode": operation_plan["mode"],
        "status": "pending",
        "plan_payload": operation_plan,
        "attempt_count": 0,
        "lease_owner": None,
        "lease_token": None,
        "lease_generation": 0,
        "lease_expires_at": None,
        "claimed_at": None,
        "started_at": None,
        "heartbeat_at": None,
        "result_fingerprint": None,
        "result_payload": None,
        "result_persisted_at": None,
        "candidate_packet_fingerprint": None,
        "completed_at": None,
        "superseded_at": None,
        "last_error": "",
        "authority": False,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": _iso(acquired_at),
        "updated_at": _iso(acquired_at),
    })
    scan_row = _strict_row(_SCAN_FIELDS, {
        "id": scan_id,
        "workspace_id": workspace_id,
        "brand_id": brand_id,
        "source_scan_id": source_scan_id,
        "source_run_id": "",
        "status": "completed",
        "pipeline_version": observation.pipeline_version,
        "acquisition_state": observation.acquisition_state,
        "requested_at": _iso(acquired_at),
        "started_at": _iso(acquired_at),
        "completed_at": _iso(acquired_at),
        "recorded_at": _iso(acquired_at),
        "error_summary": "",
        "request_payload": observation.raw_observation,
        "metadata": {
            "observation_hash": observation.observation_hash,
            "operation_plan_fingerprint": operation_plan["operation_plan_fingerprint"],
            "operation_plan": operation_plan,
        },
    })
    capture_row = _strict_row(_CAPTURE_FIELDS, {
        "id": capture_id,
        "scan_run_id": scan_id,
        "brand_id": brand_id,
        "observed_at": _iso(acquired_at),
        "recorded_at": _iso(acquired_at),
        "source_url": _BRAND_URL,
        "content_hash": signed_capture.capture_content_hash,
        "acquisition_summary": observation.acquisition_summary,
        "limitations": list(observation.limitations),
        "raw_payload": signed_capture.raw_payload,
    })
    watermark_identity = {
        "schema_version": _WATERMARK_VERSION,
        "brand_id": brand_id,
        "capture_id": capture_id,
        "capture_sequence": sequence,
        "previous_event_id": previous_watermark["id"] if previous_watermark else None,
        "previous_event_fingerprint": previous_watermark["event_fingerprint"] if previous_watermark else None,
        "capture_content_hash": signed_capture.capture_content_hash,
        "capture_observation_hash": observation.observation_hash,
        "append_origin": "capture_observation_commit",
    }
    watermark_fingerprint = canonical_fingerprint(_WATERMARK_VERSION, watermark_identity)
    watermark_row = _strict_row(_WATERMARK_FIELDS, {
        "id": str(UUID(watermark_fingerprint[:32])),
        "brand_id": brand_id,
        "capture_id": capture_id,
        "capture_sequence": sequence,
        "previous_event_id": watermark_identity["previous_event_id"],
        "previous_event_fingerprint": watermark_identity["previous_event_fingerprint"],
        "capture_content_hash": signed_capture.capture_content_hash,
        "capture_observation_hash": observation.observation_hash,
        "append_origin": "capture_observation_commit",
        "event_schema_version": _WATERMARK_VERSION,
        "event_fingerprint": watermark_fingerprint,
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": _iso(acquired_at),
    })

    signed_dumps = {
        role: receipts[role].model_dump(mode="json")
        for role in ("owned_web", "external_social_profile")
    }
    eligible_until = acquired_at + timedelta(hours=24)
    for role in ("owned_web", "external_social_profile"):
        receipt = receipts[role]
        dumped = signed_dumps[role]
        claims = dumped["claims"]
        source_url = (
            claims["acquisition"]["final_url"]
            if role == "owned_web"
            else claims["acquisition"]["reported_source_url"]
        )
        receipt_rows.append(_strict_row(_RECEIPT_FIELDS, {
            "id": _repository_uuid(
                capture_id,
                "evidence-vault-raw-acquisition-receipt-v1",
                receipt.receipt_fingerprint,
            ),
            "workspace_id": workspace_id,
            "brand_id": brand_id,
            "scan_run_id": scan_id,
            "source_scan_id": source_scan_id,
            "capture_id": capture_id,
            "watermark_event_id": watermark_row["id"],
            "capture_sequence": sequence,
            "receipt_schema_version": claims["schema_version"],
            "freshness_policy_version": claims["freshness_policy_version"],
            "signature_schema_version": dumped["signature_schema"],
            "key_id": claims["key_id"],
            "receipt_nonce": claims["receipt_nonce"],
            "acquisition_session_id": claims["acquisition_session_id"],
            "workspace_slug": claims["workspace_slug"],
            "canonical_brand": claims["canonical_brand_domain"],
            "channel_role": role,
            "provider": claims["provider"],
            "acquisition_mode": claims["acquisition"]["acquisition_mode"],
            "pre_receipt_snapshot_sha256": claims["pre_receipt_snapshot_sha256"],
            "source_url": source_url,
            "raw_fragment_pointer": claims["raw_fragment_json_pointer"],
            "raw_fragment_sha256": claims["raw_fragment_sha256"],
            "extracted_document_sha256": claims["extracted_document_sha256"],
            "extractor_version": claims["extractor_version"],
            "external_identity_provenance_fingerprint": claims["external_identity_provenance_fingerprint"],
            "external_identity_provenance": association.model_dump(mode="json") if role == "external_social_profile" else None,
            "fetched_at": claims["fetched_at"],
            "received_at": _iso(acquired_at),
            "eligible_until": _iso(eligible_until),
            "claims": claims,
            "receipt_fingerprint": dumped["receipt_fingerprint"],
            "signed_payload": {
                key: dumped[key]
                for key in ("signature_schema", "claims", "receipt_fingerprint")
            },
            "signature": dumped["signature"],
            "created_at": _iso(acquired_at),
        }))

    raw_by_role = {row["channel_role"]: row for row in raw_binding_rows}
    receipt_by_role = {row["channel_role"]: row for row in receipt_rows}
    evidence_by_role = {
        row["source"]: row
        for row in evidence_rows
    }
    members: list[dict[str, Any]] = []
    member_set_payload: list[dict[str, Any]] = []
    for role in ("owned_web", "external_social_profile"):
        relation = relations[role]
        raw_binding = raw_by_role[role]
        receipt_row = receipt_by_role[role]
        evidence_row = evidence_by_role[role]
        member_identity = {
            "group_id": c7_group["group_id"],
            "relation_id": relation["relation_id"],
            "evidence_id": relation["evidence_id"],
            "source_identity_id": relation["source_identity_id"],
            "evidence_fingerprint": relation["evidence_fingerprint"],
            "channel_role": role,
            "evidence_ref": relation["ref"],
            "source_ref": relation["ref"],
            "evidence_quote": relation["literal_quote"],
            "receipt_fingerprint": receipt_row["receipt_fingerprint"],
            "raw_evidence_binding_fingerprint": raw_binding["binding_fingerprint"],
        }
        member_set_payload.append({key: value for key, value in member_identity.items() if key != "group_id"})
        members.append(_strict_row(_MEMBER_FIELDS, {
            "id": _uuid(f"member-{sequence}-{role}"),
            "binding_id": binding_id,
            "workspace_id": workspace_id,
            "brand_id": brand_id,
            "scan_run_id": scan_id,
            "capture_id": capture_id,
            "operational_source_packet_id": authority["exact_source_id"],
            "composite_group_id": c7_group["group_id"],
            "raw_evidence_binding_id": raw_binding["id"],
            "receipt_id": receipt_row["id"],
            "evidence_record_id": evidence_row["id"],
            "channel_role": role,
            "relation_id": relation["relation_id"],
            "evidence_id": relation["evidence_id"],
            "source_identity_id": relation["source_identity_id"],
            "evidence_fingerprint": relation["evidence_fingerprint"],
            "evidence_ref": relation["ref"],
            "source_ref": relation["ref"],
            "evidence_quote": relation["literal_quote"],
            "member_fingerprint": canonical_fingerprint(_MEMBER_VERSION, member_identity),
            "created_at": _iso(acquired_at),
        }))
    member_set_payload.sort(key=lambda row: row["relation_id"])
    member_set_fingerprint = canonical_fingerprint(_MEMBER_SET_VERSION, member_set_payload)
    receipt_set = receipt_set_fingerprint(list(receipts.values()))
    binding_identity = {
        "schema_version": _VERIFIED_BINDING_VERSION,
        "canonical_brand": _BRAND,
        "source_packet_fingerprint": authority["exact_source"]["candidate_packet_fingerprint"],
        "operation_plan_fingerprint": operation_plan["operation_plan_fingerprint"],
        "exact_artifact_fingerprint": authority["exact_resolution"]["artifact_fingerprint"],
        "watermark_event_fingerprint": watermark_fingerprint,
        "capture_id": capture_id,
        "capture_sequence": sequence,
        "composite_group_id": c7_group["group_id"],
        "freshness_policy_version": C7_LIVE_FRESHNESS_POLICY_VERSION,
        "public_key_registry_fingerprint": registry_fingerprint,
        "provenance": "verified_raw_acquisition_receipt",
        "receipt_set_fingerprint": receipt_set,
        "member_set_fingerprint": member_set_fingerprint,
        "eligible_until": _iso(eligible_until),
    }
    binding_fingerprint = canonical_fingerprint(_VERIFIED_BINDING_VERSION, binding_identity)
    binding_row = _strict_row(_VERIFIED_BINDING_FIELDS, {
        "id": binding_id,
        "workspace_id": workspace_id,
        "brand_id": brand_id,
        "scan_run_id": scan_id,
        "capture_id": capture_id,
        "watermark_event_id": watermark_row["id"],
        "capture_sequence": sequence,
        "operational_source_packet_id": authority["exact_source_id"],
        "operation_plan_id": plan_id,
        "composite_group_id": c7_group["group_id"],
        "canonical_brand": _BRAND,
        "source_packet_fingerprint": authority["exact_source"]["candidate_packet_fingerprint"],
        "operation_plan_fingerprint": operation_plan["operation_plan_fingerprint"],
        "exact_artifact_fingerprint": authority["exact_resolution"]["artifact_fingerprint"],
        "freshness_policy_version": C7_LIVE_FRESHNESS_POLICY_VERSION,
        "public_key_registry_fingerprint": registry_fingerprint,
        "provenance": "verified_raw_acquisition_receipt",
        "receipt_set_fingerprint": receipt_set,
        "member_set_fingerprint": member_set_fingerprint,
        "eligible_until": _iso(eligible_until),
        "binding_schema_version": _VERIFIED_BINDING_VERSION,
        "binding_fingerprint": binding_fingerprint,
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": _iso(acquired_at),
    })
    disposition_identity = {
        "schema_version": _DISPOSITION_VERSION,
        "binding_fingerprint": binding_fingerprint,
        "receipt_set_fingerprint": receipt_set,
        "event_sequence": 1,
        "previous_event_fingerprint": None,
        "action": "retain",
        "prior_state": "none",
        "resulting_state": "retained",
        "reason": "initial verified raw provenance retention",
        "actor_id": "trusted-acquisition-ingest",
        "idempotency_key_hash": canonical_fingerprint(
            "evidence-vault-raw-provenance-initial-retain-idempotency-v1",
            {"binding_fingerprint": binding_fingerprint},
        ),
    }
    disposition = _strict_row(_DISPOSITION_FIELDS, {
        "id": str(UUID(binding_fingerprint[:32])),
        "binding_id": binding_id,
        "workspace_id": workspace_id,
        "brand_id": brand_id,
        "scan_run_id": scan_id,
        "capture_id": capture_id,
        "operational_source_packet_id": authority["exact_source_id"],
        "composite_group_id": c7_group["group_id"],
        "receipt_set_fingerprint": receipt_set,
        "event_sequence": 1,
        "previous_event_id": None,
        "previous_event_fingerprint": None,
        "action": "retain",
        "prior_state": "none",
        "resulting_state": "retained",
        "reason": disposition_identity["reason"],
        "actor_id": disposition_identity["actor_id"],
        "idempotency_key_hash": disposition_identity["idempotency_key_hash"],
        "event_schema_version": _DISPOSITION_VERSION,
        "event_fingerprint": canonical_fingerprint(_DISPOSITION_VERSION, disposition_identity),
        "occurred_at": _iso(acquired_at),
    })
    acquisition = {
        "parent_match_count": 1,
        "workspace": dict(workspace),
        "brand": dict(brand),
        "scan_run": scan_row,
        "operation_plan_count": 1,
        "operation_plan": plan_row,
        "capture": capture_row,
        "evidence_record_count": 2,
        "evidence_records": evidence_rows,
        "watermark_count": 1,
        "watermark_event": watermark_row,
        "receipt_count": 2,
        "receipts": receipt_rows,
        "evidence_binding_count": 2,
        "evidence_bindings": raw_binding_rows,
        "overflow": {
            "parents": False,
            "operation_plans": False,
            "evidence_records": False,
            "watermarks": False,
            "receipts": False,
            "evidence_bindings": False,
        },
    }
    provenance = {
        "binding_count": 1,
        "binding": binding_row,
        "member_count": 2,
        "members": members,
        "receipt_count": 2,
        "receipts": receipt_rows,
        "evidence_binding_count": 2,
        "evidence_bindings": raw_binding_rows,
        "disposition_count": 1,
        "dispositions": [disposition],
        "overflow": {
            "bindings": False,
            "members": False,
            "receipts": False,
            "evidence_bindings": False,
            "dispositions": False,
        },
    }
    return {
        "watermark": watermark_row,
        "binding": binding_row,
        "proof": {
            "binding_id": binding_id,
            "provenance": provenance,
            "acquisition": acquisition,
        },
    }


def _source_packet_row(
    *,
    packet_id: str,
    brand_id: str,
    packet: Mapping[str, Any],
    packet_kind: str,
    resolution: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    version = _REVIEWED_RESOLUTION_VERSION if packet_kind == "operational_reviewed_v2" else _SOURCE_RESOLUTION_VERSION
    return _strict_row(_PACKET_FIELDS, {
        "id": packet_id,
        "brand_id": brand_id,
        "packet_fingerprint": packet["candidate_packet_fingerprint"],
        "schema_version": packet["manifest"]["schema_version"],
        "brand_identity": packet["manifest"]["brand_identity"],
        "parent_canonical_memory_version": packet["manifest"]["parent_canonical_memory_version"],
        "reference_resolution_fingerprint": canonical_fingerprint(version, resolution),
        "reference_resolution": dict(resolution),
        "manifest": packet["manifest"],
        "candidate_tiles": packet["candidate_tiles"],
        "authority_state": "pending_review",
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": created_at,
        "packet_kind": packet_kind,
        "packet_payload": dict(packet),
        "accepted_memory_candidate_version": None,
        "candidate_overlay_version": None,
    })


def _operational_packet_row(
    *,
    packet_id: str,
    brand_id: str,
    packet: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    resolution_body = {
        "schema_version": _STORAGE_RESOLUTION_VERSION,
        "candidate_packet_fingerprint": packet["candidate_packet_fingerprint"],
        "references": {},
    }
    resolution = {
        **resolution_body,
        "reference_resolution_fingerprint": canonical_fingerprint(
            _STORAGE_RESOLUTION_VERSION,
            resolution_body,
        ),
    }
    return _strict_row(_PACKET_FIELDS, {
        "id": packet_id,
        "brand_id": brand_id,
        "packet_fingerprint": packet["candidate_packet_fingerprint"],
        "schema_version": packet["schema_version"],
        "brand_identity": packet["brand_identity"],
        "parent_canonical_memory_version": packet["current_canonical_memory_version"],
        "reference_resolution_fingerprint": resolution["reference_resolution_fingerprint"],
        "reference_resolution": resolution,
        "manifest": {
            "schema_version": packet["schema_version"],
            "packet_kind": "operational_v2",
            "brand_identity": packet["brand_identity"],
            "current_canonical_memory_version": packet["current_canonical_memory_version"],
            "proposed_canonical_memory_version": packet["proposed_canonical_memory_version"],
            "accepted_memory_candidate_version": packet["accepted_memory_candidate_version"],
            "candidate_overlay_version": packet["candidate_overlay_version"],
            "authority": False,
            "runtime_effect": False,
        },
        "candidate_tiles": packet["scoring_projection"]["tiles"],
        "authority_state": "pending_review",
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": created_at,
        "packet_kind": "operational_v2",
        "packet_payload": dict(packet),
        "accepted_memory_candidate_version": packet["accepted_memory_candidate_version"],
        "candidate_overlay_version": packet["candidate_overlay_version"],
    })


def _adoption_row(
    *,
    event: Mapping[str, Any],
    brand_id: str,
    reference_resolution_fingerprint: str,
) -> dict[str, Any]:
    return _strict_row(_ADOPTION_FIELDS, {
        "id": event["event_id"],
        "brand_id": brand_id,
        "event_type": event["event_type"],
        "sequence": event["sequence"],
        "previous_event_id": event["previous_event_id"],
        "brand_identity": event["brand_identity"],
        "candidate_packet_fingerprint": event["candidate_packet_fingerprint"],
        "reference_resolution_fingerprint": reference_resolution_fingerprint,
        "promotion_policy_fingerprint": event["policy_fingerprint"],
        "parent_canonical_memory_version": event["parent_canonical_memory_version"],
        "promoted_canonical_memory_version": event["promoted_canonical_memory_version"],
        "decision": "promote",
        "reviewer_id": event["actor_id"],
        "reviewed_at": event["created_at"],
        "rationale": "Verified shadow fixture operational adoption.",
        "schema_version": event["schema_version"],
        "idempotency_key_hash": event["idempotency_key_hash"],
        "request_fingerprint": event["request_fingerprint"],
        "authority": True,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": event["created_at"],
        "adoption_kind": "operational_v2",
        "adopted_by": event["adopted_by"],
        "actor_id": event["actor_id"],
        "event_payload": dict(event),
    })


def _registry(private_key: Ed25519PrivateKey) -> dict[str, Any]:
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    registry = {
        "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
        "current_key_id": _KEY_ID,
        "keys": {
            _KEY_ID: {
                "version": 1,
                "status": "current",
                "public_key_base64": base64.b64encode(public).decode("ascii"),
                "signing_not_before": "2026-01-01T00:00:00Z",
                "signing_ended_at": None,
            }
        },
    }
    PublicKeyRegistry.model_validate(registry, strict=True)
    return registry


def _strict_row(fields: set[str], values: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(values)
    if set(row) != fields:
        missing = sorted(fields - set(row))
        extra = sorted(set(row) - fields)
        raise AssertionError(f"strict row fields differ; missing={missing}, extra={extra}")
    return row


def _uuid(name: str) -> str:
    return str(uuid5(_ID_NAMESPACE, name))


def _repository_uuid(*parts: Any) -> str:
    return str(uuid5(_REPOSITORY_ID_NAMESPACE, ":".join(str(part) for part in parts)))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
