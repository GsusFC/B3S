"""Fail-closed, read-only C7 verified-raw shadow readiness.

This module never enables C7 and never returns its verification witness.  It only
answers whether one database snapshot satisfies the complete v1 shadow proof.
Callers receive the fixed three-field result; raw material, identifiers and
fingerprints stay inside the evaluator and are discarded.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import hmac
import json
import re
from typing import Any, Literal, Mapping, Sequence
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from src.history.capture_observation import parse_capture_observation

from src.services.evidence_vault_c7_cutover import (
    EvidenceVaultC7CutoverError,
    canonicalize_c7_brand,
)
from src.services.evidence_vault_canonical_core import (
    canonical_fingerprint,
    canonical_json,
    validate_candidate_packet,
)
from src.services.evidence_vault_composite_group_lifecycle import (
    attest_active_composite_group,
)
from src.services.evidence_vault_exact_relation_supplement import (
    validate_exact_relation_supplement_structure,
)
from src.services.evidence_vault_incremental_refresh import validate_vault_scan_plan
from src.services.evidence_vault_operational_authority import (
    project_adopted_operational_memory,
    validate_operational_adoption_event,
    validate_operational_memory_packet,
)
from src.services.evidence_vault_operational_scoring import (
    build_operational_score_evaluation,
    validate_operational_score_evaluation,
)
from src.services.evidence_vault_raw_capture import (
    DETERMINISTIC_EXTRACTOR_VERSION,
    extract_deterministic_document,
    reproduce_passage_locator,
    validate_signed_raw_capture,
)
from src.services.evidence_vault_raw_provenance import (
    C7_LIVE_FRESHNESS_POLICY_VERSION,
    PUBLIC_KEY_REGISTRY_VERSION,
    RAW_ACQUISITION_RECEIPT_VERSION,
    ExternalIdentityProvenance,
    PublicKeyRegistry,
    RawAcquisitionReceipt,
    evidence_memory_source_identity_id,
    public_key_registry_fingerprint,
    receipt_set_fingerprint,
    validate_c7_receipt_time_policy,
    validate_external_identity_provenance,
    verify_raw_acquisition_receipt,
)


C7_SHADOW_READINESS_VERSION = "evidence-vault-c7-shadow-readiness-v1"
_WATERMARK_VERSION = "evidence-vault-capture-watermark-event-v1"
_RAW_BINDING_VERSION = "evidence-vault-raw-evidence-binding-v1"
_MEMBER_VERSION = "evidence-vault-verified-c7-lineage-member-v1"
_MEMBER_SET_VERSION = "evidence-vault-verified-c7-lineage-member-set-v1"
_VERIFIED_BINDING_VERSION = "evidence-vault-verified-c7-lineage-binding-v1"
_DISPOSITION_VERSION = "evidence-vault-raw-provenance-disposition-event-v1"
_INITIAL_RETAIN_IDEMPOTENCY_VERSION = (
    "evidence-vault-raw-provenance-initial-retain-idempotency-v1"
)
_SOURCE_RESOLUTION_VERSION = "evidence-vault-operational-source-resolution-v1"
_EXACT_SOURCE_RESOLUTION_SCHEMA_VERSION = (
    "evidence-vault-exact-relation-source-resolution-v1"
)
_REVIEWED_RESOLUTION_VERSION = "evidence-vault-operational-reviewed-resolution-v1"
_STORAGE_RESOLUTION_VERSION = "evidence-vault-operational-storage-resolution-v1"
_REVIEW_REQUEST_VERSION = "evidence-vault-operational-relation-review-request-v1"
_OPERATIONAL_REVIEW_VERSION = "evidence-vault-operational-relation-review-v1"
_REVIEW_EVENT_SET_VERSION = "evidence-vault-operational-review-event-set-v1"
_PRIVATE_WITNESS_VERSION = "evidence-vault-c7-shadow-private-witness-v1"
_ID_NAMESPACE = UUID("3ef1b80c-e7b7-4fb3-95ad-fb9e03c59d52")
_MIN_CONSECUTIVE_AGE = timedelta(minutes=5)
_MAX_CONSECUTIVE_AGE = timedelta(hours=24)
_MAX_DISPOSITION_EVENTS = 64
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class C7ShadowReadinessReason(StrEnum):
    VERIFIED_RAW_READY = "verified_raw_ready"
    VERIFICATION_POLICY_INVALID = "verification_policy_invalid"
    BRAND_IDENTITY_INVALID = "brand_identity_invalid"
    BRAND_CONTEXT_UNAVAILABLE = "brand_context_unavailable"
    CONTEXT_BOUND_EXCEEDED = "context_bound_exceeded"
    CURRENT_AUTHORITY_INVALID = "current_authority_invalid"
    CURRENT_C7_UNRESOLVED = "current_c7_unresolved"
    CURRENT_C7_REASSESSMENT_PENDING = "current_c7_reassessment_pending"
    CURRENT_GROUP_INVALID = "current_group_invalid"
    CURRENT_SCORE_INVALID = "current_score_invalid"
    WATERMARK_WINDOW_INVALID = "watermark_window_invalid"
    LINEAGE_CARDINALITY_INVALID = "lineage_cardinality_invalid"
    LINEAGE_AUTHORITY_MISMATCH = "lineage_authority_mismatch"
    RAW_PROVENANCE_INVALID = "raw_provenance_invalid"
    LEGAL_HOLD_PRESENT = "legal_hold_present"
    RUNTIME_REVOKED = "runtime_revoked"
    DISPOSITION_INVALID = "disposition_invalid"
    STORAGE_UNAVAILABLE = "storage_unavailable"


class C7ShadowReadinessResult(BaseModel):
    """The only public result shape.  It intentionally contains no witness data."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[C7_SHADOW_READINESS_VERSION] = C7_SHADOW_READINESS_VERSION
    ready: bool
    reason: C7ShadowReadinessReason

    @model_validator(mode="after")
    def _ready_matches_reason(self) -> "C7ShadowReadinessResult":
        if self.ready is not (self.reason is C7ShadowReadinessReason.VERIFIED_RAW_READY):
            raise ValueError("ready must match the verified-raw reason")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class _Failure(Exception):
    def __init__(self, reason: C7ShadowReadinessReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class EvidenceVaultC7ShadowReadinessEvaluator:
    """Evaluate one already-atomic database snapshot against one pinned registry."""

    __slots__ = ("__expected_registry_fingerprint", "__registry_json")

    def __init__(
        self,
        *,
        public_key_registry: PublicKeyRegistry | Mapping[str, Any],
        expected_public_key_registry_fingerprint: str,
    ) -> None:
        self.__registry_json: str | None = None
        self.__expected_registry_fingerprint = (
            expected_public_key_registry_fingerprint
            if isinstance(expected_public_key_registry_fingerprint, str)
            and _sha256(expected_public_key_registry_fingerprint)
            else ""
        )
        try:
            supplied = (
                public_key_registry.model_dump(mode="python", round_trip=True)
                if isinstance(public_key_registry, PublicKeyRegistry)
                else deepcopy(dict(public_key_registry))
            )
            registry = PublicKeyRegistry.model_validate(supplied, strict=True)
            rendered = registry.model_dump_json()
            actual = public_key_registry_fingerprint(registry)
            if (
                registry.schema_version == PUBLIC_KEY_REGISTRY_VERSION
                and _sha256(self.__expected_registry_fingerprint)
                and hmac.compare_digest(actual, self.__expected_registry_fingerprint)
            ):
                self.__registry_json = rendered
        except Exception:
            self.__registry_json = None

    @property
    def policy_is_valid(self) -> bool:
        return self.__registry_json is not None

    def evaluate(
        self,
        canonical_brand: str,
        snapshot: Mapping[str, Any],
    ) -> C7ShadowReadinessResult:
        """Return a sanitized deterministic result for one private snapshot."""

        if self.__registry_json is None:
            return _result(C7ShadowReadinessReason.VERIFICATION_POLICY_INVALID)
        try:
            brand = canonicalize_c7_brand(canonical_brand, allow_url=False)
        except EvidenceVaultC7CutoverError:
            return _result(C7ShadowReadinessReason.BRAND_IDENTITY_INVALID)
        try:
            registry = PublicKeyRegistry.model_validate_json(
                self.__registry_json,
                strict=True,
            )
            private_witness_fingerprint = _evaluate_snapshot(
                brand=brand,
                snapshot=deepcopy(dict(snapshot)),
                registry=registry,
                expected_registry_fingerprint=self.__expected_registry_fingerprint,
            )
            _required_sha256(private_witness_fingerprint)
        except _Failure as exc:
            return _result(exc.reason)
        except Exception:
            return _result(C7ShadowReadinessReason.RAW_PROVENANCE_INVALID)
        return _result(C7ShadowReadinessReason.VERIFIED_RAW_READY)


def storage_unavailable_result() -> C7ShadowReadinessResult:
    return _result(C7ShadowReadinessReason.STORAGE_UNAVAILABLE)


def _result(reason: C7ShadowReadinessReason) -> C7ShadowReadinessResult:
    return C7ShadowReadinessResult(
        ready=reason is C7ShadowReadinessReason.VERIFIED_RAW_READY,
        reason=reason,
    )


def _fail(reason: C7ShadowReadinessReason) -> None:
    raise _Failure(reason)


def _evaluate_snapshot(
    *,
    brand: str,
    snapshot: Mapping[str, Any],
    registry: PublicKeyRegistry,
    expected_registry_fingerprint: str,
) -> str:
    try:
        top = _mapping(snapshot, {"context", "proofs"})
        context = _mapping(
            top["context"],
            {
                "schema_version",
                "database_time",
                "workspace_slug",
                "brand_match_count",
                "brand",
                "watermark_count",
                "watermarks",
                "lineage_binding_count",
                "lineage_bindings",
                "operational_adoption_count",
                "operational_adoptions",
                "operational_packet_count",
                "operational_packets",
                "source_packet_count",
                "source_packets",
                "relation_review_count",
                "relation_reviews",
                "operational_score_count",
                "operational_scores",
                "overflow",
            },
        )
    except Exception:
        _fail(C7ShadowReadinessReason.BRAND_CONTEXT_UNAVAILABLE)
    if context["schema_version"] != C7_SHADOW_READINESS_VERSION:
        _fail(C7ShadowReadinessReason.BRAND_CONTEXT_UNAVAILABLE)
    database_time = _datetime(context["database_time"])
    overflow = _mapping(
        context["overflow"],
        {"adoptions", "operational_packets", "source_packets", "reviews", "scores", "bindings"},
    )
    if any(value is not False for value in overflow.values()):
        _fail(C7ShadowReadinessReason.CONTEXT_BOUND_EXCEEDED)
    context_bounds = {
        "watermarks": 2,
        "lineage_bindings": 8,
        "operational_adoptions": 128,
        "operational_packets": 128,
        "source_packets": 32,
        "relation_reviews": 64,
        "operational_scores": 16,
    }
    if any(
        not isinstance(context[field], list) or len(context[field]) > maximum
        for field, maximum in context_bounds.items()
    ):
        _fail(C7ShadowReadinessReason.CONTEXT_BOUND_EXCEEDED)

    brand_row = context["brand"]
    if _integer(context["brand_match_count"]) != 1 or not isinstance(brand_row, Mapping):
        _fail(C7ShadowReadinessReason.BRAND_CONTEXT_UNAVAILABLE)
    brand_record = _mapping(brand_row, _BRAND_FIELDS)
    if brand_record["canonical_domain"] != brand:
        _fail(C7ShadowReadinessReason.BRAND_CONTEXT_UNAVAILABLE)
    brand_id = _uuid_text(brand_record["id"])

    current, current_event, authority_identity = _current_memory(
        context,
        brand=brand,
        brand_id=brand_id,
        database_time=database_time,
    )
    attestation, exact_source, review_identity = _current_group(
        context,
        current=current,
        current_event=current_event,
        brand_id=brand_id,
        database_time=database_time,
    )
    score_identity = _current_score(
        context,
        current=current,
        current_event=current_event,
        brand_id=brand_id,
        database_time=database_time,
    )
    watermarks = _watermark_window(
        context,
        brand_id=brand_id,
        database_time=database_time,
    )
    bindings = _lineage_bindings(
        context,
        brand=brand,
        brand_id=brand_id,
        watermarks=watermarks,
        attestation=attestation,
        exact_source=exact_source,
        expected_registry_fingerprint=expected_registry_fingerprint,
        database_time=database_time,
    )
    proofs = _rows(top["proofs"])
    if len(proofs) != 2:
        _fail(C7ShadowReadinessReason.LINEAGE_CARDINALITY_INVALID)
    proofs_by_binding: dict[str, Mapping[str, Any]] = {}
    for proof in proofs:
        parsed = _mapping(proof, {"binding_id", "provenance", "acquisition"})
        binding_key = _uuid_text(parsed["binding_id"])
        if binding_key in proofs_by_binding:
            _fail(C7ShadowReadinessReason.LINEAGE_CARDINALITY_INVALID)
        proofs_by_binding[binding_key] = parsed
    if set(proofs_by_binding) != set(bindings):
        _fail(C7ShadowReadinessReason.LINEAGE_CARDINALITY_INVALID)

    receipt_arrivals: list[tuple[datetime, datetime]] = []
    proof_identities: list[dict[str, Any]] = []
    for watermark in watermarks:
        binding = next(
            row
            for row in bindings.values()
            if _integer(row["capture_sequence"])
            == _integer(watermark["capture_sequence"])
        )
        try:
            earliest, latest, proof_identity = _validate_capture_proof(
                proof=proofs_by_binding[_uuid_text(binding["id"])],
                context_binding=binding,
                watermark=watermark,
                workspace_slug=_text(context["workspace_slug"]),
                brand=brand,
                brand_record=brand_record,
                brand_id=brand_id,
                registry=registry,
                database_time=database_time,
                exact_source=exact_source,
            )
        except _Failure:
            raise
        except Exception:
            _fail(C7ShadowReadinessReason.RAW_PROVENANCE_INVALID)
        receipt_arrivals.append((earliest, latest))
        proof_identities.append(proof_identity)

    disposition_states = {
        identity["disposition"]["head_state"] for identity in proof_identities
    }
    if "runtime_revoked" in disposition_states:
        _fail(C7ShadowReadinessReason.RUNTIME_REVOKED)
    if "legal_hold" in disposition_states:
        _fail(C7ShadowReadinessReason.LEGAL_HOLD_PRESENT)

    earlier_latest = receipt_arrivals[0][1]
    later_earliest = receipt_arrivals[1][0]
    arrival_gap = later_earliest - earlier_latest
    if not _MIN_CONSECUTIVE_AGE <= arrival_gap <= _MAX_CONSECUTIVE_AGE:
        _fail(C7ShadowReadinessReason.WATERMARK_WINDOW_INVALID)

    private_witness = {
        "schema_version": _PRIVATE_WITNESS_VERSION,
        "database_time": database_time.isoformat(),
        "workspace_slug": _text(context["workspace_slug"]),
        "canonical_brand": brand,
        "public_key_registry_fingerprint": expected_registry_fingerprint,
        "current_memory": {
            "canonical_memory_version": current["canonical_memory_version"],
            "adoption_event_id": current["adoption_event_id"],
            "authority_identity": authority_identity,
        },
        "review_identity": review_identity,
        "score_identity": score_identity,
        "watermark_identities": [
            {
                "event_id": _uuid_text(row["id"]),
                "event_fingerprint": row["event_fingerprint"],
                "capture_id": _uuid_text(row["capture_id"]),
                "capture_sequence": _integer(row["capture_sequence"]),
                "capture_content_hash": row["capture_content_hash"],
                "capture_observation_hash": row["capture_observation_hash"],
            }
            for row in watermarks
        ],
        "capture_proof_identities": proof_identities,
        "attestation_fingerprint": attestation["attestation_fingerprint"],
    }
    return canonical_fingerprint(_PRIVATE_WITNESS_VERSION, private_witness)


def _current_memory(
    context: Mapping[str, Any],
    *,
    brand: str,
    brand_id: str,
    database_time: datetime,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    adoptions = _rows(context["operational_adoptions"], fields=_ADOPTION_FIELDS)
    packets = _rows(context["operational_packets"], fields=_PACKET_FIELDS)
    if (
        _integer(context["operational_adoption_count"]) != len(adoptions)
        or _integer(context["operational_packet_count"]) != len(packets)
        or not adoptions
    ):
        _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
    packet_by_fingerprint: dict[str, dict[str, Any]] = {}
    for row in packets:
        if row["packet_kind"] != "operational_v2" or _uuid_text(row["brand_id"]) != brand_id:
            _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
        record = _operational_packet(row, brand=brand, brand_id=brand_id)
        if _datetime(record["created_at"]) > database_time:
            _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
        fingerprint = _required_sha256(row["packet_fingerprint"])
        if fingerprint in packet_by_fingerprint:
            _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
        packet_by_fingerprint[fingerprint] = record

    current: dict[str, Any] | None = None
    previous_event_id: str | None = None
    current_event: dict[str, Any] | None = None
    previous_event_time: datetime | None = None
    chain_identities: list[dict[str, Any]] = []
    ordered = sorted(adoptions, key=lambda row: _integer(row["sequence"]))
    for expected_sequence, row in enumerate(ordered, start=1):
        event = _operational_event(row, brand=brand, brand_id=brand_id)
        if event["sequence"] != expected_sequence or event["previous_event_id"] != previous_event_id:
            _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
        packet_record = packet_by_fingerprint.get(event["candidate_packet_fingerprint"])
        event_time = _datetime(event["created_at"])
        event_storage_time = _datetime(row["created_at"])
        if (
            packet_record is None
            or row["reference_resolution_fingerprint"]
            != packet_record["reference_resolution_fingerprint"]
            or event_time > event_storage_time
            or event_storage_time > database_time
            or (previous_event_time is not None and event_time < previous_event_time)
            or _datetime(packet_record["created_at"]) > event_storage_time
        ):
            _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
        packet = packet_record["packet"]
        expected_parent = current["canonical_memory_version"] if current else None
        if packet["current_canonical_memory_version"] != expected_parent:
            _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
        current = project_adopted_operational_memory(packet, event)
        previous_event_id = event["event_id"]
        current_event = {
            **event,
            "_storage_created_at": event_storage_time.isoformat(),
        }
        previous_event_time = event_time
        chain_identities.append(
            {
                "event_id": event["event_id"],
                "sequence": event["sequence"],
                "previous_event_id": event["previous_event_id"],
                "candidate_packet_fingerprint": event["candidate_packet_fingerprint"],
                "reference_resolution_fingerprint": packet_record[
                    "reference_resolution_fingerprint"
                ],
                "promoted_canonical_memory_version": event[
                    "promoted_canonical_memory_version"
                ],
                "policy_fingerprint": event["policy_fingerprint"],
                "request_fingerprint": event["request_fingerprint"],
            }
        )
    if current is None or current_event is None or current.get("brand_identity") != brand:
        _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
    authority_identity = {
        "current_canonical_memory_version": current["canonical_memory_version"],
        "current_adoption_event_id": current_event["event_id"],
        "adoption_chain": chain_identities,
        "packet_identities": [
            {
                "id": record["id"],
                "packet_fingerprint": fingerprint,
                "reference_resolution_fingerprint": record[
                    "reference_resolution_fingerprint"
                ],
            }
            for fingerprint, record in sorted(packet_by_fingerprint.items())
        ],
    }
    return current, current_event, authority_identity


def _operational_packet(
    row: Mapping[str, Any],
    *,
    brand: str,
    brand_id: str,
) -> dict[str, Any]:
    packet = _mapping(row["packet_payload"])
    validate_operational_memory_packet(packet)
    expected_resolution_body = {
        "schema_version": _STORAGE_RESOLUTION_VERSION,
        "candidate_packet_fingerprint": packet["candidate_packet_fingerprint"],
        "references": {},
    }
    expected_resolution = {
        **expected_resolution_body,
        "reference_resolution_fingerprint": canonical_fingerprint(
            _STORAGE_RESOLUTION_VERSION,
            expected_resolution_body,
        ),
    }
    resolution = _mapping(
        row["reference_resolution"],
        {
            "schema_version",
            "candidate_packet_fingerprint",
            "references",
            "reference_resolution_fingerprint",
        },
    )
    expected_manifest = {
        "schema_version": packet["schema_version"],
        "packet_kind": "operational_v2",
        "brand_identity": brand,
        "current_canonical_memory_version": packet["current_canonical_memory_version"],
        "proposed_canonical_memory_version": packet["proposed_canonical_memory_version"],
        "accepted_memory_candidate_version": packet["accepted_memory_candidate_version"],
        "candidate_overlay_version": packet["candidate_overlay_version"],
        "authority": False,
        "runtime_effect": False,
    }
    if (
        _uuid_text(row["id"])
        != _stable_uuid_text(
            brand_id,
            "evidence-vault-operational-memory-packet",
            packet["candidate_packet_fingerprint"],
        )
        or packet["brand_identity"] != brand
        or row["packet_fingerprint"] != packet["candidate_packet_fingerprint"]
        or row["schema_version"] != packet["schema_version"]
        or row["brand_identity"] != brand
        or _optional_text(row["parent_canonical_memory_version"])
        != packet["current_canonical_memory_version"]
        or row["accepted_memory_candidate_version"] != packet["accepted_memory_candidate_version"]
        or row["candidate_overlay_version"] != packet["candidate_overlay_version"]
        or resolution != expected_resolution
        or row["reference_resolution_fingerprint"]
        != expected_resolution["reference_resolution_fingerprint"]
        or _mapping(row["manifest"]) != expected_manifest
        or row["candidate_tiles"] != packet["scoring_projection"]["tiles"]
        or row["authority_state"] != "pending_review"
        or row["authority"] is not False
        or row["production_runtime_effect"] is not False
        or row["scanner_runtime_effect"] is not False
    ):
        _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
    created_at = _datetime(row["created_at"])
    return {
        "id": _uuid_text(row["id"]),
        "packet": packet,
        "packet_fingerprint": packet["candidate_packet_fingerprint"],
        "reference_resolution": resolution,
        "reference_resolution_fingerprint": resolution["reference_resolution_fingerprint"],
        "created_at": created_at.isoformat(),
    }


def _operational_event(
    row: Mapping[str, Any],
    *,
    brand: str,
    brand_id: str,
) -> dict[str, Any]:
    event = _mapping(row["event_payload"])
    validate_operational_adoption_event(event)
    if (
        row["adoption_kind"] != "operational_v2"
        or _uuid_text(row["brand_id"]) != brand_id
        or event["brand_identity"] != brand
        or _uuid_text(row["id"]) != event["event_id"]
        or _uuid_text(row["id"])
        != _stable_uuid_text(
            brand_id,
            "evidence-vault-operational-adoption",
            event["idempotency_key_hash"],
        )
        or row["event_type"] != event["event_type"]
        or _integer(row["sequence"]) != event["sequence"]
        or _optional_uuid_text(row["previous_event_id"]) != event["previous_event_id"]
        or row["brand_identity"] != brand
        or row["candidate_packet_fingerprint"] != event["candidate_packet_fingerprint"]
        or row["promotion_policy_fingerprint"] != event["policy_fingerprint"]
        or _optional_text(row["parent_canonical_memory_version"])
        != event["parent_canonical_memory_version"]
        or row["promoted_canonical_memory_version"] != event["promoted_canonical_memory_version"]
        or row["adopted_by"] != event["adopted_by"]
        or row["actor_id"] != event["actor_id"]
        or row["idempotency_key_hash"] != event["idempotency_key_hash"]
        or row["request_fingerprint"] != event["request_fingerprint"]
        or row["authority"] is not True
        or row["authority_scope"] != "b3s-vault"
        or row["production_runtime_effect"] is not False
        or row["scanner_runtime_effect"] is not False
        or _datetime(row["reviewed_at"]) != _datetime(event["created_at"])
    ):
        _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
    return event


def _current_group(
    context: Mapping[str, Any],
    *,
    current: Mapping[str, Any],
    current_event: Mapping[str, Any],
    brand_id: str,
    database_time: datetime,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    accepted_rows = [
        _mapping(row)
        for row in current["content"].get("accepted_tiles") or []
        if isinstance(row, Mapping) and row.get("tile_id") == "C7"
    ]
    pending_rows = [
        row
        for row in current["content"].get("pending_reassessments") or []
        if isinstance(row, Mapping) and row.get("tile_id") == "C7"
    ]
    if pending_rows:
        _fail(C7ShadowReadinessReason.CURRENT_C7_REASSESSMENT_PENDING)
    if not accepted_rows:
        _fail(C7ShadowReadinessReason.CURRENT_C7_UNRESOLVED)
    if len(accepted_rows) != 1:
        _fail(C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID)
    accepted = accepted_rows[0]

    source_rows = _rows(context["source_packets"], fields=_PACKET_FIELDS)
    if _integer(context["source_packet_count"]) != len(source_rows):
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    reviewed_fingerprint = _required_sha256(
        accepted.get("source_candidate_packet_fingerprint")
    )
    reviewed_matches = [
        row
        for row in source_rows
        if row["packet_kind"] == "operational_reviewed_v2"
        and row["packet_fingerprint"] == reviewed_fingerprint
    ]
    if len(reviewed_matches) != 1:
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    reviewed_record = _source_packet(reviewed_matches[0], brand_id=brand_id)
    reviewed_resolution = reviewed_record["reference_resolution"]
    current_basis = _basis_by_relation(accepted)
    reviewed_tiles = [
        _mapping(row)
        for row in reviewed_record["packet"].get("candidate_tiles") or []
        if isinstance(row, Mapping) and row.get("tile_id") == "C7"
    ]
    if len(reviewed_tiles) != 1:
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    reviewed_basis = _basis_by_relation(reviewed_tiles[0])
    decision_ids = sorted(
        _uuid_text(row.get("decision_event_id")) for row in current_basis.values()
    )
    resolution_ids = reviewed_resolution["decision_event_ids"]
    if (
        set(current_basis) != set(reviewed_basis)
        or any(reviewed_basis[key] != value for key, value in current_basis.items())
        or resolution_ids != sorted(set(resolution_ids))
        or not set(decision_ids).issubset(set(resolution_ids))
    ):
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)

    exact_fingerprint = _required_sha256(
        reviewed_resolution["source_candidate_packet_fingerprint"]
    )
    exact_matches = [
        row
        for row in source_rows
        if row["packet_kind"] == "operational_source_v2"
        and row["packet_fingerprint"] == exact_fingerprint
        and _mapping(row["reference_resolution"]).get("source_kind")
        == "exact_relation_supplement"
    ]
    if len(exact_matches) != 1:
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    exact_record = _source_packet(exact_matches[0], brand_id=brand_id)
    exact_created_at = _datetime(exact_record["created_at"])
    reviewed_created_at = _datetime(reviewed_record["created_at"])
    adoption_created_at = _datetime(current_event["_storage_created_at"])
    if (
        exact_created_at > reviewed_created_at
        or reviewed_created_at > adoption_created_at
        or adoption_created_at > database_time
    ):
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)

    reviews = _rows(context["relation_reviews"], fields=_REVIEW_FIELDS)
    if _integer(context["relation_review_count"]) != len(reviews):
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    exact_source_id = exact_record["id"]
    exact_reviews = [
        row
        for row in reviews
        if _uuid_text(row["source_packet_id"]) == exact_source_id
    ]
    selected_reviews: dict[str, dict[str, Any]] = {}
    for row in exact_reviews:
        event_id = _uuid_text(row["id"])
        if event_id in selected_reviews:
            _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
        selected_reviews[event_id] = row
    if set(selected_reviews) != set(resolution_ids):
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)

    request_fingerprint = _required_sha256(
        reviewed_resolution["review_request_fingerprint"]
    )
    reviewers: set[str] = set()
    reviewed_times: set[str] = set()
    normalized_decisions: dict[str, dict[str, str]] = {}
    review_identities: list[dict[str, Any]] = []
    for event_id in resolution_ids:
        row = selected_reviews[event_id]
        relation_id = _required_sha256(row["relation_id"])
        reviewer_id = _bounded_text(row["reviewer_id"], maximum=200)
        rationale = _bounded_text(row["rationale"], maximum=2000)
        decision = _text(row["decision"])
        reviewed_at_value = _datetime(row["created_at"])
        reviewed_at = reviewed_at_value.isoformat()
        if (
            reviewed_at_value < exact_created_at
            or reviewed_at_value > reviewed_created_at
            or reviewed_at_value > adoption_created_at
            or reviewed_at_value > database_time
            or _uuid_text(row["brand_id"]) != brand_id
            or row["source_packet_fingerprint"] != exact_fingerprint
            or row["source_packet_kind"] != "operational_source_v2"
            or decision not in {"accept", "reject"}
            or row["review_request_fingerprint"] != request_fingerprint
            or row["authority"] is not True
            or row["authority_scope"] != "relation_review"
            or row["production_runtime_effect"] is not False
            or row["scanner_runtime_effect"] is not False
            or event_id
            != _stable_uuid_text(
                brand_id,
                "evidence-vault-operational-relation-review",
                exact_fingerprint,
                relation_id,
                request_fingerprint,
            )
            or relation_id in normalized_decisions
        ):
            _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
        reviewers.add(reviewer_id)
        reviewed_times.add(reviewed_at)
        normalized_decisions[relation_id] = {
            "decision": decision,
            "rationale": rationale,
        }
        review_identities.append(
            {
                "event_id": event_id,
                "relation_id": relation_id,
                "decision": decision,
            }
        )
    if len(reviewers) != 1 or len(reviewed_times) != 1:
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    expected_request = canonical_fingerprint(
        _REVIEW_REQUEST_VERSION,
        {
            "source_candidate_packet_fingerprint": exact_fingerprint,
            "reviewer_id": next(iter(reviewers)),
            "reviewed_at": next(iter(reviewed_times)),
            "decisions": normalized_decisions,
        },
    )
    if expected_request != request_fingerprint:
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    review_projection = {
        relation_id: {
            "decision": decision["decision"],
            "decision_event_id": _stable_uuid_text(
                brand_id,
                "evidence-vault-operational-relation-review",
                exact_fingerprint,
                relation_id,
                request_fingerprint,
            ),
        }
        for relation_id, decision in sorted(normalized_decisions.items())
    }
    reviewed_manifest = reviewed_record["packet"]["manifest"]
    if (
        reviewed_manifest["reviewed_memory_candidate_version"]
        != canonical_fingerprint(
            _OPERATIONAL_REVIEW_VERSION,
            {"source": exact_fingerprint, "reviews": review_projection},
        )
        or reviewed_manifest["review_packet_set_fingerprint"]
        != canonical_fingerprint(_REVIEW_EVENT_SET_VERSION, review_projection)
    ):
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)

    for relation_id, basis in current_basis.items():
        decision_id = _uuid_text(basis.get("decision_event_id"))
        row = selected_reviews[decision_id]
        if row["relation_id"] != relation_id or row["decision"] != "accept":
            _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    try:
        attestation = attest_active_composite_group(
            current_operational_memory=current,
            exact_source_record=exact_record,
        )
    except Exception:
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    if (
        attestation.get("decision_rule") != "all_of"
        or attestation.get("member_channel_roles")
        != ["external_social_profile", "owned_web"]
        or len(attestation.get("member_relation_ids") or []) != 2
        or len(attestation.get("member_evidence_fingerprints") or []) != 2
    ):
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    review_identity = {
        "reviewed_packet_id": reviewed_record["id"],
        "reviewed_packet_fingerprint": reviewed_record["packet_fingerprint"],
        "reviewed_resolution_fingerprint": reviewed_record[
            "reference_resolution_fingerprint"
        ],
        "exact_packet_id": exact_record["id"],
        "exact_packet_fingerprint": exact_record["packet_fingerprint"],
        "exact_resolution_fingerprint": exact_record[
            "reference_resolution_fingerprint"
        ],
        "exact_artifact_fingerprint": exact_record["reference_resolution"][
            "artifact_fingerprint"
        ],
        "review_request_fingerprint": request_fingerprint,
        "review_events": review_identities,
    }
    return attestation, exact_record, review_identity


def _source_packet(row: Mapping[str, Any], *, brand_id: str) -> dict[str, Any]:
    packet = _mapping(row["packet_payload"])
    validate_candidate_packet(packet)
    kind = _text(row["packet_kind"])
    if kind == "operational_reviewed_v2":
        version = _REVIEWED_RESOLUTION_VERSION
        resolution = _mapping(
            row["reference_resolution"],
            {
                "schema_version",
                "source_candidate_packet_fingerprint",
                "review_request_fingerprint",
                "decision_event_ids",
            },
        )
        decision_ids = resolution["decision_event_ids"]
        if (
            resolution["schema_version"] != _REVIEWED_RESOLUTION_VERSION
            or not _sha256(resolution["source_candidate_packet_fingerprint"])
            or not _sha256(resolution["review_request_fingerprint"])
            or not isinstance(decision_ids, list)
            or decision_ids != sorted(set(_uuid_text(value) for value in decision_ids))
        ):
            _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    elif kind == "operational_source_v2":
        version = _SOURCE_RESOLUTION_VERSION
        resolution = _mapping(
            row["reference_resolution"],
            {
                "schema_version",
                "source_kind",
                "request_fingerprint",
                "artifact_fingerprint",
                "source_evidence_pack_sha256",
                "source_candidate_packet_fingerprint",
                "parent_canonical_memory_version",
                "decision_groups",
                "artifact",
            },
        )
        artifact = _mapping(resolution["artifact"])
        validate_exact_relation_supplement_structure(artifact)
        coverage = _mapping(packet["manifest"]["coverage_summary"])
        if (
            resolution["schema_version"]
            != _EXACT_SOURCE_RESOLUTION_SCHEMA_VERSION
            or resolution["source_kind"] != "exact_relation_supplement"
            or resolution["source_candidate_packet_fingerprint"]
            != packet["candidate_packet_fingerprint"]
            or resolution["request_fingerprint"] != artifact.get("request_fingerprint")
            or resolution["artifact_fingerprint"] != artifact.get("artifact_fingerprint")
            or resolution["artifact_fingerprint"] != coverage.get("artifact_fingerprint")
            or resolution["source_evidence_pack_sha256"]
            != artifact.get("source_evidence_pack_canonical_sha256")
            or resolution["parent_canonical_memory_version"]
            != artifact.get("parent_canonical_memory_version")
            or resolution["parent_canonical_memory_version"]
            != packet["manifest"]["parent_canonical_memory_version"]
            or resolution["decision_groups"] != coverage.get("decision_groups")
            or not _sha256(resolution["request_fingerprint"])
            or not _sha256(resolution["artifact_fingerprint"])
            or not _sha256(resolution["source_evidence_pack_sha256"])
        ):
            _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    else:
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)

    reference_fingerprint = canonical_fingerprint(version, resolution)
    expected_packet_id = (
        _stable_uuid_text(
            brand_id,
            "evidence-vault-operational-reviewed-packet",
            packet["candidate_packet_fingerprint"],
        )
        if kind == "operational_reviewed_v2"
        else _stable_uuid_text(
            brand_id,
            "evidence-vault-exact-relation-supplement-source-packet",
            packet["candidate_packet_fingerprint"],
            reference_fingerprint,
        )
    )
    if (
        _uuid_text(row["id"]) != expected_packet_id
        or _uuid_text(row["brand_id"]) != brand_id
        or row["packet_fingerprint"] != packet["candidate_packet_fingerprint"]
        or row["schema_version"] != packet["manifest"]["schema_version"]
        or row["brand_identity"] != packet["manifest"]["brand_identity"]
        or _optional_text(row["parent_canonical_memory_version"])
        != packet["manifest"]["parent_canonical_memory_version"]
        or not _json_equal(row["manifest"], packet["manifest"])
        or not _json_equal(row["candidate_tiles"], packet["candidate_tiles"])
        or row["reference_resolution_fingerprint"] != reference_fingerprint
        or row["authority_state"] != "pending_review"
        or row["authority"] is not False
        or row["production_runtime_effect"] is not False
        or row["scanner_runtime_effect"] is not False
    ):
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    created_at = _datetime(row["created_at"])
    return {
        "id": _uuid_text(row["id"]),
        "packet_kind": kind,
        "packet": packet,
        "packet_fingerprint": packet["candidate_packet_fingerprint"],
        "reference_resolution": resolution,
        "reference_resolution_fingerprint": reference_fingerprint,
        "created_at": created_at.isoformat(),
        "authority": False,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


def _current_score(
    context: Mapping[str, Any],
    *,
    current: Mapping[str, Any],
    current_event: Mapping[str, Any],
    brand_id: str,
    database_time: datetime,
) -> dict[str, Any]:
    scores = _rows(context["operational_scores"], fields=_SCORE_FIELDS)
    if _integer(context["operational_score_count"]) != len(scores):
        _fail(C7ShadowReadinessReason.CURRENT_SCORE_INVALID)
    matches = [
        row
        for row in scores
        if row["evaluation_kind"] == "operational_v2"
        and row["canonical_memory_version"] == current["canonical_memory_version"]
    ]
    if len(matches) != 1:
        _fail(C7ShadowReadinessReason.CURRENT_SCORE_INVALID)
    row = matches[0]
    evaluation = _mapping(row["evaluation_payload"])
    try:
        validate_operational_score_evaluation(evaluation)
        reusable = None
        reused_identity = evaluation.get("reused_from_evaluation_identity")
        if reused_identity is not None:
            reusable_matches = [
                _mapping(value["evaluation_payload"])
                for value in scores
                if value["evaluation_identity"] == reused_identity
            ]
            if len(reusable_matches) != 1:
                _fail(C7ShadowReadinessReason.CURRENT_SCORE_INVALID)
            reusable = reusable_matches[0]
            validate_operational_score_evaluation(reusable)
        rebuilt = build_operational_score_evaluation(
            current,
            created_at=evaluation["created_at"],
            reusable_evaluation=reusable,
        )
    except _Failure:
        raise
    except Exception:
        _fail(C7ShadowReadinessReason.CURRENT_SCORE_INVALID)
    row_matches = (
        _uuid_text(row["brand_id"]) == brand_id
        and _uuid_text(row["promotion_event_id"]) == current["adoption_event_id"]
        and _uuid_text(row["id"])
        == _stable_uuid_text(
            brand_id,
            "evidence-vault-operational-score",
            evaluation["evaluation_identity"],
        )
        and current_event["event_id"] == current["adoption_event_id"]
        and rebuilt == evaluation
        and row["schema_version"] == evaluation["schema_version"]
        and row["evaluation_identity"] == evaluation["evaluation_identity"]
        and row["canonical_memory_version"] == evaluation["canonical_memory_version"]
        and row["score_input_fingerprint"] == evaluation["score_input_fingerprint"]
        and row["derived_tile_state_fingerprint"] == evaluation["derived_tile_state_fingerprint"]
        and row["rubric_version"] == evaluation["rubric_version"]
        and row["tile_contract_registry_fingerprint"] == evaluation["tile_contract_registry_fingerprint"]
        and row["reducer_policy_fingerprint"] == evaluation["reducer_policy_fingerprint"]
        and row["aggregation_policy_fingerprint"] == evaluation["aggregation_policy_fingerprint"]
        and _integer(row["score"]) == evaluation["score"]
        and row["component_breakdown"] == evaluation["component_breakdown"]
        and Decimal(str(row["base_average"])) == Decimal(str(evaluation["base_average"]))
        and row["magnetism_capped"] is evaluation["magnetism_capped"]
        and _optional_text(row["reused_from_evaluation_identity"])
        == evaluation["reused_from_evaluation_identity"]
        and row["authority_coverage"] == evaluation["authority_coverage"]
        and row["authority"] is True
        and row["authority_scope"] == "b3s-vault"
        and row["production_runtime_effect"] is False
        and row["scanner_runtime_effect"] is False
        and _datetime(row["created_at"]) == _datetime(evaluation["created_at"])
        and _datetime(evaluation["created_at"])
        >= _datetime(current_event["_storage_created_at"])
        and _datetime(evaluation["created_at"]) <= database_time
    )
    if not row_matches:
        _fail(C7ShadowReadinessReason.CURRENT_SCORE_INVALID)
    return {
        "row_id": _uuid_text(row["id"]),
        "promotion_event_id": _uuid_text(row["promotion_event_id"]),
        "schema_version": evaluation["schema_version"],
        "evaluation_identity": evaluation["evaluation_identity"],
        "canonical_memory_version": evaluation["canonical_memory_version"],
        "adoption_event_id": evaluation["adoption_event_id"],
        "score_input_fingerprint": evaluation["score_input_fingerprint"],
        "derived_tile_state_fingerprint": evaluation[
            "derived_tile_state_fingerprint"
        ],
        "rubric_version": evaluation["rubric_version"],
        "tile_contract_registry_fingerprint": evaluation[
            "tile_contract_registry_fingerprint"
        ],
        "reducer_policy_fingerprint": evaluation["reducer_policy_fingerprint"],
        "aggregation_policy_fingerprint": evaluation[
            "aggregation_policy_fingerprint"
        ],
        "score": evaluation["score"],
        "component_breakdown": evaluation["component_breakdown"],
        "base_average": str(evaluation["base_average"]),
        "magnetism_capped": evaluation["magnetism_capped"],
        "reused_from_evaluation_identity": evaluation[
            "reused_from_evaluation_identity"
        ],
        "authority_coverage": evaluation["authority_coverage"],
        "created_at": _datetime(evaluation["created_at"]).isoformat(),
    }


def _watermark_window(
    context: Mapping[str, Any],
    *,
    brand_id: str,
    database_time: datetime,
) -> list[dict[str, Any]]:
    rows = _rows(context["watermarks"], fields=_WATERMARK_FIELDS)
    if _integer(context["watermark_count"]) < 2 or len(rows) != 2:
        _fail(C7ShadowReadinessReason.WATERMARK_WINDOW_INVALID)
    ordered = sorted(rows, key=lambda row: _integer(row["capture_sequence"]))
    earlier, later = ordered
    watermark_count = _integer(context["watermark_count"])
    if (
        _integer(later["capture_sequence"]) != watermark_count
        or _integer(earlier["capture_sequence"]) != watermark_count - 1
        or _integer(later["capture_sequence"])
        != _integer(earlier["capture_sequence"]) + 1
        or _optional_uuid_text(later["previous_event_id"]) != _uuid_text(earlier["id"])
        or later["previous_event_fingerprint"] != earlier["event_fingerprint"]
        or _uuid_text(earlier["capture_id"]) == _uuid_text(later["capture_id"])
    ):
        _fail(C7ShadowReadinessReason.WATERMARK_WINDOW_INVALID)
    for row in ordered:
        _validate_watermark(row, brand_id=brand_id)
    gap = _datetime(later["created_at"]) - _datetime(earlier["created_at"])
    if (
        not _MIN_CONSECUTIVE_AGE <= gap <= _MAX_CONSECUTIVE_AGE
        or _datetime(later["created_at"]) > database_time
    ):
        _fail(C7ShadowReadinessReason.WATERMARK_WINDOW_INVALID)
    return ordered


def _validate_watermark(row: Mapping[str, Any], *, brand_id: str) -> None:
    sequence = _integer(row["capture_sequence"])
    previous_id = _optional_uuid_text(row["previous_event_id"])
    previous_fingerprint = _optional_text(row["previous_event_fingerprint"])
    identity = {
        "schema_version": _WATERMARK_VERSION,
        "brand_id": brand_id,
        "capture_id": _uuid_text(row["capture_id"]),
        "capture_sequence": sequence,
        "previous_event_id": previous_id,
        "previous_event_fingerprint": previous_fingerprint,
        "capture_content_hash": _required_sha256(row["capture_content_hash"]),
        "capture_observation_hash": _required_sha256(row["capture_observation_hash"]),
        "append_origin": "capture_observation_commit",
    }
    event_fingerprint = canonical_fingerprint(_WATERMARK_VERSION, identity)
    if (
        _uuid_text(row["brand_id"]) != brand_id
        or _uuid_text(row["id"])
        != str(UUID(event_fingerprint[:32]))
        or row["append_origin"] != "capture_observation_commit"
        or row["event_schema_version"] != _WATERMARK_VERSION
        or row["event_fingerprint"] != event_fingerprint
        or row["authority"] is not False
        or row["production_runtime_effect"] is not False
        or row["scanner_runtime_effect"] is not False
        or (sequence == 1) != (previous_id is None and previous_fingerprint is None)
    ):
        _fail(C7ShadowReadinessReason.WATERMARK_WINDOW_INVALID)


def _lineage_bindings(
    context: Mapping[str, Any],
    *,
    brand: str,
    brand_id: str,
    watermarks: Sequence[Mapping[str, Any]],
    attestation: Mapping[str, Any],
    exact_source: Mapping[str, Any],
    expected_registry_fingerprint: str,
    database_time: datetime,
) -> dict[str, dict[str, Any]]:
    rows = _rows(context["lineage_bindings"], fields=_VERIFIED_BINDING_FIELDS)
    if _integer(context["lineage_binding_count"]) != 2 or len(rows) != 2:
        _fail(C7ShadowReadinessReason.LINEAGE_CARDINALITY_INVALID)
    by_sequence: dict[int, dict[str, Any]] = {}
    source_fingerprint = exact_source["packet"]["candidate_packet_fingerprint"]
    for row in rows:
        sequence = _integer(row["capture_sequence"])
        if sequence in by_sequence:
            _fail(C7ShadowReadinessReason.LINEAGE_CARDINALITY_INVALID)
        by_sequence[sequence] = row
        watermark = next(
            (value for value in watermarks if _integer(value["capture_sequence"]) == sequence),
            None,
        )
        if watermark is None:
            _fail(C7ShadowReadinessReason.LINEAGE_CARDINALITY_INVALID)
        if (
            _uuid_text(row["brand_id"]) != brand_id
            or _uuid_text(row["capture_id"]) != _uuid_text(watermark["capture_id"])
            or _uuid_text(row["watermark_event_id"]) != _uuid_text(watermark["id"])
            or row["canonical_brand"] != brand
            or row["composite_group_id"] != attestation["group_id"]
            or row["source_packet_fingerprint"] != source_fingerprint
            or _uuid_text(row["operational_source_packet_id"]) != exact_source["id"]
            or row["exact_artifact_fingerprint"]
            != _mapping(exact_source["reference_resolution"]).get("artifact_fingerprint")
            or row["freshness_policy_version"] != C7_LIVE_FRESHNESS_POLICY_VERSION
            or row["public_key_registry_fingerprint"] != expected_registry_fingerprint
            or row["provenance"] != "verified_raw_acquisition_receipt"
            or row["binding_schema_version"] != _VERIFIED_BINDING_VERSION
            or row["authority"] is not False
            or row["production_runtime_effect"] is not False
            or row["scanner_runtime_effect"] is not False
            or _datetime(row["eligible_until"]) <= database_time
        ):
            _fail(C7ShadowReadinessReason.LINEAGE_AUTHORITY_MISMATCH)
    if set(by_sequence) != {_integer(value["capture_sequence"]) for value in watermarks}:
        _fail(C7ShadowReadinessReason.LINEAGE_CARDINALITY_INVALID)
    return {_uuid_text(row["id"]): row for row in rows}


def _validate_capture_proof(
    *,
    proof: Mapping[str, Any],
    context_binding: Mapping[str, Any],
    watermark: Mapping[str, Any],
    workspace_slug: str,
    brand: str,
    brand_record: Mapping[str, Any],
    brand_id: str,
    registry: PublicKeyRegistry,
    database_time: datetime,
    exact_source: Mapping[str, Any],
) -> tuple[datetime, datetime, dict[str, Any]]:
    provenance = _mapping(
        proof["provenance"],
        {
            "binding_count",
            "binding",
            "member_count",
            "members",
            "receipt_count",
            "receipts",
            "evidence_binding_count",
            "evidence_bindings",
            "disposition_count",
            "dispositions",
            "overflow",
        },
    )
    provenance_overflow = _mapping(
        provenance["overflow"],
        {"bindings", "members", "receipts", "evidence_bindings", "dispositions"},
    )
    if any(value is not False for value in provenance_overflow.values()):
        _fail(C7ShadowReadinessReason.CONTEXT_BOUND_EXCEEDED)
    disposition_rows = _rows(provenance["dispositions"])
    if len(disposition_rows) > _MAX_DISPOSITION_EVENTS:
        _fail(C7ShadowReadinessReason.CONTEXT_BOUND_EXCEEDED)
    if (
        _integer(provenance["binding_count"]) != 1
        or _integer(provenance["member_count"]) != 2
        or _integer(provenance["receipt_count"]) != 2
        or _integer(provenance["evidence_binding_count"]) != 2
        or _integer(provenance["disposition_count"]) != len(disposition_rows)
    ):
        raise ValueError("bounded provenance cardinality diverged")

    acquisition = _mapping(
        proof["acquisition"],
        {
            "parent_match_count",
            "workspace",
            "brand",
            "scan_run",
            "operation_plan_count",
            "operation_plan",
            "capture",
            "evidence_record_count",
            "evidence_records",
            "watermark_count",
            "watermark_event",
            "receipt_count",
            "receipts",
            "evidence_binding_count",
            "evidence_bindings",
            "overflow",
        },
    )
    acquisition_overflow = _mapping(
        acquisition["overflow"],
        {
            "parents",
            "operation_plans",
            "evidence_records",
            "watermarks",
            "receipts",
            "evidence_bindings",
        },
    )
    if any(value is not False for value in acquisition_overflow.values()):
        _fail(C7ShadowReadinessReason.CONTEXT_BOUND_EXCEEDED)
    if (
        _integer(acquisition["parent_match_count"]) != 1
        or _integer(acquisition["operation_plan_count"]) != 1
        or _integer(acquisition["evidence_record_count"]) != 2
        or _integer(acquisition["watermark_count"]) != 1
        or _integer(acquisition["receipt_count"]) != 2
        or _integer(acquisition["evidence_binding_count"]) != 2
    ):
        raise ValueError("bounded acquisition cardinality diverged")
    binding = _mapping(provenance["binding"], _VERIFIED_BINDING_FIELDS)
    if not _json_equal(binding, context_binding):
        raise ValueError("binding projection changed")
    workspace = _mapping(acquisition["workspace"], _WORKSPACE_FIELDS)
    acquisition_brand = _mapping(acquisition["brand"], _BRAND_FIELDS)
    scan = _mapping(acquisition["scan_run"], _SCAN_FIELDS)
    plan = _mapping(acquisition["operation_plan"], _PLAN_FIELDS)
    capture = _mapping(acquisition["capture"], _CAPTURE_FIELDS)
    acquisition_watermark = _mapping(
        acquisition["watermark_event"], _WATERMARK_FIELDS
    )
    workspace_id = _uuid_text(workspace["id"])
    observation = parse_capture_observation(_mapping(scan["request_payload"]))
    expected_workspace_id = _stable_uuid_text("workspace", workspace_slug)
    expected_brand_id = _stable_uuid_text(
        expected_workspace_id, "brand", brand
    )
    expected_scan_id = _stable_uuid_text(
        expected_workspace_id, "scan", observation.source_scan_id
    )
    expected_capture_id = _stable_uuid_text(expected_scan_id, "capture")
    scan_metadata = _mapping(scan["metadata"])
    plan_payload = _mapping(plan["plan_payload"])
    validate_vault_scan_plan(plan_payload)
    if (
        workspace["slug"] != workspace_slug
        or workspace_id != expected_workspace_id
        or brand_id != expected_brand_id
        or _uuid_text(scan["id"]) != expected_scan_id
        or _uuid_text(capture["id"]) != expected_capture_id
        or _uuid_text(acquisition_brand["id"]) != brand_id
        or not _json_equal(acquisition_brand, brand_record)
        or _uuid_text(acquisition_brand["workspace_id"]) != workspace_id
        or acquisition_brand["canonical_domain"] != brand
        or _uuid_text(scan["id"]) != _uuid_text(binding["scan_run_id"])
        or _uuid_text(scan["workspace_id"]) != workspace_id
        or _uuid_text(scan["brand_id"]) != brand_id
        or _uuid_text(capture["id"]) != _uuid_text(binding["capture_id"])
        or _uuid_text(capture["scan_run_id"]) != _uuid_text(scan["id"])
        or _uuid_text(capture["brand_id"]) != brand_id
        or not _json_equal(acquisition_watermark, watermark)
        or observation.source_scan_id != scan["source_scan_id"]
        or (observation.source_run_id or "") != scan["source_run_id"]
        or observation.canonical_domain != brand
        or observation.canonical_url != capture["source_url"]
        or observation.canonical_url != acquisition_brand["canonical_url"]
        or observation.pipeline_version != scan["pipeline_version"]
        or observation.acquisition_state != scan["acquisition_state"]
        or scan["status"] != "completed"
        or scan["error_summary"] != ""
        or _datetime(scan["requested_at"]) != observation.observed_at
        or _datetime(scan["started_at"]) != observation.observed_at
        or _datetime(scan["completed_at"]) != observation.recorded_at
        or _datetime(scan["recorded_at"]) != observation.recorded_at
        or _datetime(capture["observed_at"]) != observation.observed_at
        or _datetime(capture["recorded_at"]) != observation.recorded_at
        or not _json_equal(capture["raw_payload"], observation.capture_payload)
        or not _json_equal(
            capture["acquisition_summary"],
            {
                **observation.acquisition_summary,
                "state": observation.acquisition_state,
            },
        )
        or not _json_equal(capture["limitations"], list(observation.limitations))
        or capture["content_hash"] != observation.capture_hash
        or scan_metadata.get("observation_hash") != observation.observation_hash
        or scan_metadata.get("operation_plan_fingerprint")
        != plan_payload["operation_plan_fingerprint"]
        or not _json_equal(scan_metadata.get("operation_plan"), plan_payload)
    ):
        raise ValueError("acquisition parent context diverged")
    if (
        _uuid_text(plan["id"]) != _uuid_text(binding["operation_plan_id"])
        or _uuid_text(plan["id"])
        != _stable_uuid_text(
            expected_scan_id,
            "evidence-vault-operation-plan",
            plan_payload["operation_plan_fingerprint"],
        )
        or _uuid_text(plan["workspace_id"]) != workspace_id
        or _uuid_text(plan["workspace_id"]) != _uuid_text(binding["workspace_id"])
        or _uuid_text(plan["brand_id"]) != brand_id
        or _uuid_text(plan["scan_run_id"]) != _uuid_text(scan["id"])
        or _uuid_text(plan["scan_run_id"]) != _uuid_text(binding["scan_run_id"])
        or plan["observation_hash"] != observation.observation_hash
        or plan["operation_plan_fingerprint"] != binding["operation_plan_fingerprint"]
        or plan["operation_plan_fingerprint"]
        != plan_payload["operation_plan_fingerprint"]
        or plan["canonical_memory_version"]
        != plan_payload["canonical_memory_version"]
        or plan["mode"] != plan_payload["mode"]
        or plan_payload["brand_identity"] != brand
        or plan_payload["subject_url"] != observation.canonical_url
        or plan["authority"] is not False
        or plan["authority_scope"] != "b3s-vault"
        or plan["production_runtime_effect"] is not False
        or plan["scanner_runtime_effect"] is not False
        or watermark["capture_content_hash"] != capture["content_hash"]
        or watermark["capture_observation_hash"] != observation.observation_hash
    ):
        raise ValueError("operation plan or watermark diverged")
    verified = validate_signed_raw_capture(
        _mapping(capture["raw_payload"]),
        capture_content_hash=str(capture["content_hash"]),
        public_key_registry=registry,
    )
    if (
        verified.pre_receipt_snapshot.workspace_slug != workspace["slug"]
        or verified.pre_receipt_snapshot.source_scan_id != scan["source_scan_id"]
        or verified.pre_receipt_snapshot.canonical_brand_domain != brand
        or verified.pre_receipt_snapshot.canonical_brand_url
        != acquisition_brand["canonical_url"]
        or verified.capture_content_hash != capture["content_hash"]
        or verified.public_key_registry_fingerprint
        != binding["public_key_registry_fingerprint"]
    ):
        raise ValueError("verified capture identity diverged")

    receipts = _rows(provenance["receipts"], fields=_RECEIPT_FIELDS)
    raw_bindings = _rows(provenance["evidence_bindings"], fields=_RAW_BINDING_FIELDS)
    members = _rows(provenance["members"], fields=_MEMBER_FIELDS)
    evidence = _rows(acquisition["evidence_records"], fields=_EVIDENCE_FIELDS)
    acquisition_receipts = _rows(acquisition["receipts"], fields=_RECEIPT_FIELDS)
    acquisition_raw_bindings = _rows(acquisition["evidence_bindings"], fields=_RAW_BINDING_FIELDS)
    if not all(
        len(rows) == 2
        for rows in (
            receipts,
            raw_bindings,
            members,
            evidence,
            acquisition_receipts,
            acquisition_raw_bindings,
        )
    ):
        raise ValueError("capture proof cardinality diverged")
    if (
        not _json_equal(receipts, acquisition_receipts)
        or not _json_equal(raw_bindings, acquisition_raw_bindings)
        or len({_uuid_text(row["id"]) for row in receipts}) != 2
        or len({_uuid_text(row["id"]) for row in raw_bindings}) != 2
        or len({_uuid_text(row["id"]) for row in members}) != 2
    ):
        raise ValueError("raw projections diverged")

    observation_evidence: dict[str, Mapping[str, Any]] = {}
    for row in observation.evidence_records:
        parsed = _mapping(row)
        evidence_ref = _text(parsed.get("ref"))
        if evidence_ref in observation_evidence:
            raise ValueError("observation evidence reference repeated")
        observation_evidence[evidence_ref] = parsed
    if len(observation_evidence) != 2:
        raise ValueError("observation evidence cardinality diverged")
    for evidence_row in evidence:
        expected = observation_evidence.get(_text(evidence_row["evidence_ref"]))
        expected_metadata = (
            _mapping(expected.get("metadata"))
            if expected is not None and expected.get("metadata") is not None
            else {}
        )
        if (
            expected is None
            or _uuid_text(evidence_row["capture_id"]) != _uuid_text(capture["id"])
            or evidence_row["source"] != expected.get("source")
            or evidence_row["source_class"] != expected_metadata.get("source_class")
            or evidence_row["evidence_type"] != expected.get("evidence_type")
            or evidence_row["url"] != expected.get("url")
            or evidence_row["content"] != expected.get("content")
            or evidence_row["content_raw"] is not None
            or evidence_row["content_hash"]
            != hashlib.sha256(_text(expected.get("content")).encode("utf-8")).hexdigest()
            or evidence_row["confidence"] != expected.get("confidence")
            or not _json_equal(evidence_row["metadata"], expected_metadata)
        ):
            raise ValueError("durable evidence differs from capture observation")

    receipts_by_role = _unique_by(receipts, "channel_role")
    raw_by_role = _unique_by(raw_bindings, "channel_role")
    members_by_role = _unique_by(members, "channel_role")
    if set(receipts_by_role) != {"owned_web", "external_social_profile"}:
        raise ValueError("receipt roles diverged")
    evidence_by_id = {_uuid_text(row["id"]): row for row in evidence}
    if len(evidence_by_id) != 2:
        raise ValueError("evidence identities diverged")

    verified_receipts: list[RawAcquisitionReceipt] = []
    received_times: list[datetime] = []
    member_set_payload: list[dict[str, Any]] = []
    exact_group = _exact_c7_group(exact_source)
    association = verified.envelope.external_identity_provenance
    if not isinstance(association, ExternalIdentityProvenance):
        raise ValueError("external association missing")
    relations = {
        _text(row["channel_role"]): _mapping(row)
        for row in exact_group["relations"]
    }
    if set(relations) != {"owned_web", "external_social_profile"}:
        raise ValueError("exact group roles diverged")

    for role in ("owned_web", "external_social_profile"):
        receipt_row = receipts_by_role[role]
        raw_binding = raw_by_role[role]
        member = members_by_role[role]
        signed_payload = _mapping(receipt_row["signed_payload"], {"signature_schema", "claims", "receipt_fingerprint"})
        receipt = RawAcquisitionReceipt.model_validate(
            {**signed_payload, "signature": receipt_row["signature"]},
            strict=True,
        )
        received_at = _datetime(receipt_row["received_at"])
        verified_receipt = verify_raw_acquisition_receipt(
            receipt,
            public_key_registry=registry,
            database_received_at=received_at,
        )
        capture_receipt = next(
            (value for value in verified.receipts if value.claims.channel_role == role),
            None,
        )
        if capture_receipt != verified_receipt or receipt_row["claims"] != receipt.claims.model_dump(mode="json"):
            raise ValueError("durable receipt changed")
        claims = receipt.claims
        acquisition_details = claims.acquisition
        expected_source_url = (
            acquisition_details.final_url
            if role == "owned_web"
            else acquisition_details.reported_source_url
        )
        association_source_url = (
            association.owned_source_url
            if role == "owned_web"
            else association.external_source_url
        )
        expected_source_identity = evidence_memory_source_identity_id(
            source_url=expected_source_url,
            raw_fact_role=role,
        )
        expected_external = (
            verified.envelope.external_identity_provenance.model_dump(mode="json")
            if role == "external_social_profile"
            and verified.envelope.external_identity_provenance is not None
            else None
        )
        if (
            _uuid_text(receipt_row["workspace_id"]) != _uuid_text(binding["workspace_id"])
            or _uuid_text(receipt_row["brand_id"]) != brand_id
            or _uuid_text(receipt_row["scan_run_id"]) != _uuid_text(binding["scan_run_id"])
            or _uuid_text(receipt_row["capture_id"]) != _uuid_text(binding["capture_id"])
            or _uuid_text(receipt_row["id"])
            != _stable_uuid_text(
                _uuid_text(binding["capture_id"]),
                RAW_ACQUISITION_RECEIPT_VERSION,
                receipt.receipt_fingerprint,
            )
            or _uuid_text(receipt_row["watermark_event_id"]) != _uuid_text(watermark["id"])
            or _integer(receipt_row["capture_sequence"]) != _integer(binding["capture_sequence"])
            or receipt_row["receipt_fingerprint"] != receipt.receipt_fingerprint
            or receipt_row["canonical_brand"] != brand
            or receipt_row["source_scan_id"] != scan["source_scan_id"]
            or receipt_row["workspace_slug"] != workspace["slug"]
            or receipt_row["freshness_policy_version"] != C7_LIVE_FRESHNESS_POLICY_VERSION
            or receipt_row["receipt_schema_version"] != RAW_ACQUISITION_RECEIPT_VERSION
            or receipt_row["signature_schema_version"] != receipt.signature_schema
            or receipt_row["key_id"] != claims.key_id
            or _uuid_text(receipt_row["receipt_nonce"]) != claims.receipt_nonce
            or _uuid_text(receipt_row["acquisition_session_id"]) != claims.acquisition_session_id
            or receipt_row["channel_role"] != claims.channel_role
            or receipt_row["provider"] != claims.provider
            or receipt_row["acquisition_mode"] != acquisition_details.acquisition_mode
            or receipt_row["pre_receipt_snapshot_sha256"] != claims.pre_receipt_snapshot_sha256
            or receipt_row["source_url"] != expected_source_url
            or receipt_row["source_url"] != association_source_url
            or receipt_row["raw_fragment_pointer"] != claims.raw_fragment_json_pointer
            or receipt_row["raw_fragment_sha256"] != claims.raw_fragment_sha256
            or receipt_row["extracted_document_sha256"] != claims.extracted_document_sha256
            or receipt_row["extractor_version"] != DETERMINISTIC_EXTRACTOR_VERSION
            or receipt_row["external_identity_provenance_fingerprint"]
            != claims.external_identity_provenance_fingerprint
            or receipt_row["external_identity_provenance"] != expected_external
            or _datetime(receipt_row["fetched_at"]) != _datetime(claims.fetched_at)
            or signed_payload != receipt.model_dump(mode="json", exclude={"signature"})
        ):
            raise ValueError("receipt row identity diverged")
        expected_eligible = min(_datetime(receipt_row["fetched_at"]), received_at) + timedelta(hours=24)
        if _datetime(receipt_row["eligible_until"]) != expected_eligible or database_time >= expected_eligible:
            raise ValueError("receipt freshness diverged")

        extraction = extract_deterministic_document(
            verified,
            receipt_fingerprint=receipt.receipt_fingerprint,
        )
        evidence_row = evidence_by_id.get(_uuid_text(raw_binding["evidence_record_id"]))
        if evidence_row is None:
            raise ValueError("evidence row missing")
        expected_ref = f"raw-acquisition:{role}:{receipt.receipt_fingerprint}"
        expected_source_class = "owned_copy" if role == "owned_web" else "external_proof"
        if (
            _uuid_text(raw_binding["workspace_id"]) != _uuid_text(binding["workspace_id"])
            or _uuid_text(raw_binding["brand_id"]) != brand_id
            or _uuid_text(raw_binding["scan_run_id"]) != _uuid_text(binding["scan_run_id"])
            or _uuid_text(raw_binding["capture_id"]) != _uuid_text(binding["capture_id"])
            or _uuid_text(raw_binding["receipt_id"]) != _uuid_text(receipt_row["id"])
            or raw_binding["channel_role"] != role
            or raw_binding["extracted_document"] != extraction.document
            or raw_binding["extracted_document_sha256"] != extraction.sha256
            or evidence_row["content"] != extraction.document
            or evidence_row["content_hash"] != hashlib.sha256(extraction.document.encode("utf-8")).hexdigest()
            or evidence_row["evidence_ref"] != expected_ref
            or evidence_row["source"] != role
            or evidence_row["source_class"] != expected_source_class
            or evidence_row["evidence_type"] != "text"
            or evidence_row["confidence"] != "high"
            or raw_binding["evidence_ref"] != expected_ref
            or raw_binding["source_url"] != evidence_row["url"]
            or raw_binding["source_url"] != receipt_row["source_url"]
            or _mapping(evidence_row["metadata"]).get("receipt_fingerprint")
            != receipt.receipt_fingerprint
            or _mapping(evidence_row["metadata"]).get("source_class")
            != expected_source_class
            or raw_binding["evidence_record_content_hash"] != evidence_row["content_hash"]
            or raw_binding["extractor_schema_version"] != DETERMINISTIC_EXTRACTOR_VERSION
            or raw_binding["extractor_version"] != DETERMINISTIC_EXTRACTOR_VERSION
        ):
            raise ValueError("deterministic extraction binding diverged")
        passage = reproduce_passage_locator(
            extracted_document=extraction.document,
            passage_locator=_mapping(raw_binding["passage_locator"]),
            durable_evidence_record_content=_text(evidence_row["content"]),
        )
        if passage.passage_text != raw_binding["passage_text"] or passage.passage_sha256 != raw_binding["passage_sha256"]:
            raise ValueError("passage replay diverged")
        raw_identity = {
            "receipt_fingerprint": receipt.receipt_fingerprint,
            "evidence_record_id": _uuid_text(raw_binding["evidence_record_id"]),
            "evidence_ref": raw_binding["evidence_ref"],
            "source_url": raw_binding["source_url"],
            "channel_role": role,
            "extractor_schema_version": raw_binding["extractor_schema_version"],
            "extractor_version": raw_binding["extractor_version"],
            "extracted_document_sha256": raw_binding["extracted_document_sha256"],
            "passage_locator": raw_binding["passage_locator"],
            "passage_sha256": raw_binding["passage_sha256"],
            "evidence_record_content_hash": raw_binding["evidence_record_content_hash"],
        }
        raw_binding_fingerprint = canonical_fingerprint(
            _RAW_BINDING_VERSION, raw_identity
        )
        if (
            raw_binding["binding_fingerprint"] != raw_binding_fingerprint
            or _uuid_text(raw_binding["id"])
            != _stable_uuid_text(
                _uuid_text(binding["capture_id"]),
                _RAW_BINDING_VERSION,
                raw_binding_fingerprint,
            )
            or _uuid_text(evidence_row["id"])
            != _stable_uuid_text(
                _uuid_text(binding["capture_id"]),
                "evidence",
                expected_ref,
            )
        ):
            raise ValueError("raw binding fingerprint diverged")

        relation = relations[role]
        if (
            _uuid_text(member["binding_id"]) != _uuid_text(binding["id"])
            or _uuid_text(member["workspace_id"]) != _uuid_text(binding["workspace_id"])
            or _uuid_text(member["brand_id"]) != brand_id
            or _uuid_text(member["scan_run_id"]) != _uuid_text(binding["scan_run_id"])
            or _uuid_text(member["capture_id"]) != _uuid_text(binding["capture_id"])
            or _uuid_text(member["operational_source_packet_id"])
            != _uuid_text(binding["operational_source_packet_id"])
            or _uuid_text(member["raw_evidence_binding_id"]) != _uuid_text(raw_binding["id"])
            or _uuid_text(member["receipt_id"]) != _uuid_text(receipt_row["id"])
            or _uuid_text(member["evidence_record_id"]) != _uuid_text(evidence_row["id"])
            or member["composite_group_id"] != binding["composite_group_id"]
            or member["relation_id"] != relation["relation_id"]
            or member["evidence_id"] != relation["evidence_id"]
            or member["source_identity_id"] != relation["source_identity_id"]
            or member["source_identity_id"] != expected_source_identity
            or relation["url"] != receipt_row["source_url"]
            or relation["channel_role"] != role
            or relation["source_class"] != expected_source_class
            or member["evidence_fingerprint"] != relation["evidence_fingerprint"]
            or member["evidence_ref"] != relation["ref"]
            or member["source_ref"] != relation["ref"]
            or member["evidence_quote"] != relation["literal_quote"]
            or member["evidence_quote"] != passage.passage_text
        ):
            raise ValueError("verified member diverged")
        member_identity = {
            "group_id": member["composite_group_id"],
            "relation_id": member["relation_id"],
            "evidence_id": member["evidence_id"],
            "source_identity_id": member["source_identity_id"],
            "evidence_fingerprint": member["evidence_fingerprint"],
            "channel_role": role,
            "evidence_ref": member["evidence_ref"],
            "source_ref": member["source_ref"],
            "evidence_quote": member["evidence_quote"],
            "receipt_fingerprint": receipt.receipt_fingerprint,
            "raw_evidence_binding_fingerprint": raw_binding["binding_fingerprint"],
        }
        if member["member_fingerprint"] != canonical_fingerprint(_MEMBER_VERSION, member_identity):
            raise ValueError("member fingerprint diverged")
        member_set_payload.append({key: value for key, value in member_identity.items() if key != "group_id"})
        verified_receipts.append(verified_receipt)
        received_times.append(received_at)

    validate_external_identity_provenance(
        association,
        owned_receipt=receipts_by_role["owned_web"]["signed_payload"] | {
            "signature": receipts_by_role["owned_web"]["signature"]
        },
        external_receipt=receipts_by_role["external_social_profile"]["signed_payload"] | {
            "signature": receipts_by_role["external_social_profile"]["signature"]
        },
        durable_raw_capture_payload=_mapping(capture["raw_payload"]),
        public_key_registry=registry,
    )
    if (
        association.owned_source_identity_id != members_by_role["owned_web"]["source_identity_id"]
        or association.external_source_identity_id
        != members_by_role["external_social_profile"]["source_identity_id"]
        or association.owned_source_identity_id
        != evidence_memory_source_identity_id(
            source_url=association.owned_source_url,
            raw_fact_role="owned_web",
        )
        or association.external_source_identity_id
        != evidence_memory_source_identity_id(
            source_url=association.external_source_url,
            raw_fact_role="external_social_profile",
        )
    ):
        raise ValueError("external association identity diverged")

    validate_c7_receipt_time_policy(
        verified_receipts,
        database_received_at=received_times,
        database_time=database_time,
    )
    expected_receipt_set = receipt_set_fingerprint(verified_receipts)
    member_set_payload.sort(key=lambda row: _text(row["relation_id"]))
    expected_member_set = canonical_fingerprint(_MEMBER_SET_VERSION, member_set_payload)
    if (
        binding["receipt_set_fingerprint"] != expected_receipt_set
        or binding["member_set_fingerprint"] != expected_member_set
        or _datetime(binding["eligible_until"])
        != min(_datetime(row["eligible_until"]) for row in receipts)
    ):
        raise ValueError("verified binding sets diverged")
    binding_identity = {
        "schema_version": binding["binding_schema_version"],
        "canonical_brand": binding["canonical_brand"],
        "source_packet_fingerprint": binding["source_packet_fingerprint"],
        "operation_plan_fingerprint": binding["operation_plan_fingerprint"],
        "exact_artifact_fingerprint": binding["exact_artifact_fingerprint"],
        "watermark_event_fingerprint": watermark["event_fingerprint"],
        "capture_id": _uuid_text(binding["capture_id"]),
        "capture_sequence": _integer(binding["capture_sequence"]),
        "composite_group_id": binding["composite_group_id"],
        "freshness_policy_version": binding["freshness_policy_version"],
        "public_key_registry_fingerprint": binding["public_key_registry_fingerprint"],
        "provenance": binding["provenance"],
        "receipt_set_fingerprint": binding["receipt_set_fingerprint"],
        "member_set_fingerprint": binding["member_set_fingerprint"],
        "eligible_until": _timestamp_fingerprint_value(binding["eligible_until"]),
    }
    if binding["binding_fingerprint"] != canonical_fingerprint(_VERIFIED_BINDING_VERSION, binding_identity):
        raise ValueError("verified binding fingerprint diverged")

    disposition_identity = _validate_disposition(
        provenance["dispositions"],
        binding=binding,
        database_time=database_time,
    )
    proof_identity = {
        "binding_id": _uuid_text(binding["id"]),
        "binding_fingerprint": binding["binding_fingerprint"],
        "capture_id": _uuid_text(binding["capture_id"]),
        "capture_sequence": _integer(binding["capture_sequence"]),
        "capture_content_hash": capture["content_hash"],
        "capture_observation_hash": observation.observation_hash,
        "operation_plan_fingerprint": binding["operation_plan_fingerprint"],
        "receipt_set_fingerprint": binding["receipt_set_fingerprint"],
        "receipt_fingerprints": sorted(
            receipt.receipt_fingerprint for receipt in verified_receipts
        ),
        "raw_binding_fingerprints": sorted(
            _required_sha256(row["binding_fingerprint"]) for row in raw_bindings
        ),
        "member_set_fingerprint": binding["member_set_fingerprint"],
        "member_fingerprints": sorted(
            _required_sha256(row["member_fingerprint"]) for row in members
        ),
        "disposition": disposition_identity,
    }
    return min(received_times), max(received_times), proof_identity


def _validate_disposition(
    value: Any,
    *,
    binding: Mapping[str, Any],
    database_time: datetime,
) -> dict[str, Any]:
    rows = _rows(value, fields=_DISPOSITION_FIELDS)
    if not rows:
        _fail(C7ShadowReadinessReason.DISPOSITION_INVALID)
    ordered = sorted(rows, key=lambda row: _integer(row["event_sequence"]))
    previous_id: str | None = None
    previous_fingerprint: str | None = None
    previous_state = "none"
    previous_occurred_at: datetime | None = None
    binding_created_at = _datetime(binding["created_at"])
    event_ids: set[str] = set()
    event_fingerprints: set[str] = set()
    idempotency_hashes: set[str] = set()
    chain: list[dict[str, Any]] = []
    for expected_sequence, row in enumerate(ordered, start=1):
        sequence = _integer(row["event_sequence"])
        event_id = _uuid_text(row["id"])
        occurred_at = _datetime(row["occurred_at"])
        action = _text(row["action"])
        prior_state = _text(row["prior_state"])
        resulting_state = _text(row["resulting_state"])
        if expected_sequence == 1:
            expected_resulting_state = "retained" if action == "retain" else ""
        elif previous_state == "retained" and action == "place_legal_hold":
            expected_resulting_state = "legal_hold"
        elif previous_state == "legal_hold" and action == "release_legal_hold":
            expected_resulting_state = "retained"
        elif previous_state in {"retained", "legal_hold"} and action == "revoke_runtime":
            expected_resulting_state = "runtime_revoked"
        else:
            expected_resulting_state = ""
        identity = {
            "schema_version": row["event_schema_version"],
            "binding_fingerprint": binding["binding_fingerprint"],
            "receipt_set_fingerprint": row["receipt_set_fingerprint"],
            "event_sequence": sequence,
            "previous_event_fingerprint": previous_fingerprint,
            "action": action,
            "prior_state": prior_state,
            "resulting_state": resulting_state,
            "reason": _bounded_text(row["reason"], maximum=2000),
            "actor_id": _bounded_text(row["actor_id"], maximum=200),
            "idempotency_key_hash": _required_sha256(row["idempotency_key_hash"]),
        }
        event_fingerprint = _required_sha256(row["event_fingerprint"])
        duplicate_identity = (
            event_id in event_ids
            or event_fingerprint in event_fingerprints
            or identity["idempotency_key_hash"] in idempotency_hashes
        )
        expected_initial_idempotency = canonical_fingerprint(
            _INITIAL_RETAIN_IDEMPOTENCY_VERSION,
            {"binding_fingerprint": binding["binding_fingerprint"]},
        )
        if (
            _uuid_text(row["binding_id"]) != _uuid_text(binding["id"])
            or _uuid_text(row["workspace_id"]) != _uuid_text(binding["workspace_id"])
            or _uuid_text(row["brand_id"]) != _uuid_text(binding["brand_id"])
            or _uuid_text(row["scan_run_id"]) != _uuid_text(binding["scan_run_id"])
            or _uuid_text(row["capture_id"]) != _uuid_text(binding["capture_id"])
            or _uuid_text(row["operational_source_packet_id"])
            != _uuid_text(binding["operational_source_packet_id"])
            or row["composite_group_id"] != binding["composite_group_id"]
            or row["receipt_set_fingerprint"] != binding["receipt_set_fingerprint"]
            or row["event_schema_version"] != _DISPOSITION_VERSION
            or sequence != expected_sequence
            or _optional_uuid_text(row["previous_event_id"]) != previous_id
            or _optional_text(row["previous_event_fingerprint"])
            != previous_fingerprint
            or prior_state != previous_state
            or resulting_state != expected_resulting_state
            or event_fingerprint
            != canonical_fingerprint(_DISPOSITION_VERSION, identity)
            or duplicate_identity
            or occurred_at < binding_created_at
            or occurred_at > database_time
            or previous_occurred_at is not None
            and occurred_at < previous_occurred_at
            or expected_sequence == 1
            and (
                event_id != str(UUID(str(binding["binding_fingerprint"])[:32]))
                or identity["reason"]
                != "initial verified raw provenance retention"
                or identity["actor_id"] != "trusted-acquisition-ingest"
                or identity["idempotency_key_hash"]
                != expected_initial_idempotency
            )
        ):
            _fail(C7ShadowReadinessReason.DISPOSITION_INVALID)
        chain.append(
            {
                "event_id": event_id,
                "binding_id": _uuid_text(row["binding_id"]),
                "workspace_id": _uuid_text(row["workspace_id"]),
                "brand_id": _uuid_text(row["brand_id"]),
                "scan_run_id": _uuid_text(row["scan_run_id"]),
                "capture_id": _uuid_text(row["capture_id"]),
                "operational_source_packet_id": _uuid_text(
                    row["operational_source_packet_id"]
                ),
                "composite_group_id": row["composite_group_id"],
                **identity,
                "previous_event_id": _optional_uuid_text(
                    row["previous_event_id"]
                ),
                "event_fingerprint": event_fingerprint,
                "occurred_at": occurred_at.isoformat(),
            }
        )
        event_ids.add(event_id)
        event_fingerprints.add(event_fingerprint)
        idempotency_hashes.add(identity["idempotency_key_hash"])
        previous_id = event_id
        previous_fingerprint = row["event_fingerprint"]
        previous_state = resulting_state
        previous_occurred_at = occurred_at
    if previous_state not in {"retained", "legal_hold", "runtime_revoked"}:
        _fail(C7ShadowReadinessReason.DISPOSITION_INVALID)
    return {
        "head_event_id": previous_id,
        "head_event_fingerprint": previous_fingerprint,
        "head_state": previous_state,
        "head_event": chain[-1],
        "event_chain": chain,
    }


def _exact_c7_group(exact_source: Mapping[str, Any]) -> dict[str, Any]:
    resolution = _mapping(exact_source["reference_resolution"])
    artifact = _mapping(resolution.get("artifact"))
    matches = [
        _mapping(row)
        for row in artifact.get("groups") or []
        if isinstance(row, Mapping) and row.get("tile_id") == "C7"
    ]
    if len(matches) != 1 or matches[0].get("decision_rule") != "all_of":
        raise ValueError("exact C7 group unavailable")
    return matches[0]


def _basis_by_relation(tile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = [_mapping(row) for row in tile.get("basis") or [] if isinstance(row, Mapping)]
    result = {_text(row.get("relation_id")): row for row in rows}
    if len(result) != len(rows):
        _fail(C7ShadowReadinessReason.CURRENT_GROUP_INVALID)
    return result


def _mapping(value: Any, fields: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object required")
    result = dict(value)
    if fields is not None and set(result) != fields:
        raise ValueError("object fields differ")
    return result


def _rows(value: Any, fields: set[str] | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("array required")
    return [_mapping(row, fields) for row in value]


def _unique_by(rows: Sequence[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    result = {_text(row[field]): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("row identity is not unique")
    return result


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value == value.strip():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is naive")
    return parsed.astimezone(timezone.utc)


def _timestamp_fingerprint_value(value: Any) -> str:
    if isinstance(value, str):
        _datetime(value)
        return value
    return _datetime(value).isoformat()


def _integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("integer invalid")
    return value


def _uuid_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("UUID invalid")
    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError("UUID invalid")
    return value


def _optional_uuid_text(value: Any) -> str | None:
    return None if value is None else _uuid_text(value)


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError("text invalid")
    return value


def _bounded_text(value: Any, *, maximum: int) -> str:
    text = _text(value)
    if len(text) > maximum:
        raise ValueError("text bound exceeded")
    return text


def _stable_uuid_text(*parts: Any) -> str:
    return str(uuid5(_ID_NAMESPACE, ":".join(str(part) for part in parts)))


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _required_sha256(value: Any) -> str:
    if not _sha256(value):
        raise ValueError("SHA-256 invalid")
    return str(value)


def _json_equal(left: Any, right: Any) -> bool:
    def normalize(value: Any) -> Any:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    return normalize(left) == normalize(right)


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
    "id", "brand_id", "source_packet_id", "source_packet_fingerprint", "source_packet_kind",
    "relation_id", "decision", "reviewer_id", "rationale", "review_request_fingerprint",
    "authority", "authority_scope", "production_runtime_effect", "scanner_runtime_effect", "created_at",
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


__all__ = [
    "C7_SHADOW_READINESS_VERSION",
    "C7ShadowReadinessReason",
    "C7ShadowReadinessResult",
    "EvidenceVaultC7ShadowReadinessEvaluator",
    "storage_unavailable_result",
]
