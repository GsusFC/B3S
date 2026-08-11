from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_c7_shadow_readiness import C7ShadowReadinessReason
from src.services.evidence_vault_raw_provenance import canonical_fingerprint
from evidence_vault_c7_shadow_fixture import (
    ReadyFixture,
    append_disposition_event,
    build_verified_raw_ready_fixture,
    clone_ready_snapshot,
)


Mutation = Callable[[dict[str, Any]], None]


@pytest.fixture(scope="module")
def ready_fixture() -> ReadyFixture:
    return build_verified_raw_ready_fixture()


def test_public_evaluator_accepts_two_complete_verified_raw_captures(
    ready_fixture: ReadyFixture,
) -> None:
    result = ready_fixture.evaluator.evaluate("causaprima.ai", ready_fixture.snapshot)

    assert result.model_dump(mode="json") == {
        "schema_version": "evidence-vault-c7-shadow-readiness-v1",
        "ready": True,
        "reason": "verified_raw_ready",
    }
    assert not hasattr(result, "witness")
    assert result.reason is C7ShadowReadinessReason.VERIFIED_RAW_READY


def _context_overflow(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["overflow"]["reviews"] = True


def _provenance_overflow(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"][0]["provenance"]["overflow"]["dispositions"] = True


def _acquisition_overflow(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"][0]["acquisition"]["overflow"]["evidence_records"] = True


def _forged_disposition_bound(snapshot: dict[str, Any]) -> None:
    provenance = snapshot["proofs"][0]["provenance"]
    provenance["dispositions"] = provenance["dispositions"] * 65
    provenance["disposition_count"] = 65


def _watermark_head_count_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["watermark_count"] = 999


def _final_clock_expiry(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["database_time"] = "2026-08-10T06:00:00+00:00"


def _string_count(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["brand_match_count"] = "1"


def _score_row_id_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["operational_scores"][0]["id"] = (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )


def _operational_packet_id_transplant(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["operational_packets"][-1]["id"] = (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )


def _exact_source_id_transplant(snapshot: dict[str, Any]) -> None:
    source = next(
        row
        for row in snapshot["context"]["source_packets"]
        if row["packet_kind"] == "operational_source_v2"
    )
    source["id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    for review in snapshot["context"]["relation_reviews"]:
        review["source_packet_id"] = source["id"]


def _score_future_of_database(snapshot: dict[str, Any]) -> None:
    score = snapshot["context"]["operational_scores"][0]
    score["created_at"] = "2030-01-01T00:00:00+00:00"
    score["evaluation_payload"]["created_at"] = score["created_at"]


def _score_before_adoption(snapshot: dict[str, Any]) -> None:
    score = snapshot["context"]["operational_scores"][0]
    score["created_at"] = "2020-01-01T00:00:00+00:00"
    score["evaluation_payload"]["created_at"] = score["created_at"]


def _adoption_storage_future(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["operational_adoptions"][-1]["created_at"] = (
        "2030-01-01T00:00:00+00:00"
    )


def _review_event_id_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["relation_reviews"][0]["id"] = (
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )


def _review_rationale_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["relation_reviews"][0]["rationale"] += " changed"


def _review_source_packet_kind_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["relation_reviews"][0]["source_packet_kind"] = (
        "operational_reviewed_v2"
    )


def _adoption_resolution_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["operational_adoptions"][-1][
        "reference_resolution_fingerprint"
    ] = "0" * 64


def _initial_disposition_actor_tamper(snapshot: dict[str, Any]) -> None:
    provenance = snapshot["proofs"][0]["provenance"]
    binding = provenance["binding"]
    row = provenance["dispositions"][0]
    row["actor_id"] = "forged-retention-actor"
    identity = {
        "schema_version": row["event_schema_version"],
        "binding_fingerprint": binding["binding_fingerprint"],
        "receipt_set_fingerprint": row["receipt_set_fingerprint"],
        "event_sequence": row["event_sequence"],
        "previous_event_fingerprint": row["previous_event_fingerprint"],
        "action": row["action"],
        "prior_state": row["prior_state"],
        "resulting_state": row["resulting_state"],
        "reason": row["reason"],
        "actor_id": row["actor_id"],
        "idempotency_key_hash": row["idempotency_key_hash"],
    }
    row["event_fingerprint"] = canonical_fingerprint(
        "evidence-vault-raw-provenance-disposition-event-v1",
        identity,
    )


def _duplicate_disposition_id(snapshot: dict[str, Any]) -> None:
    append_disposition_event(snapshot, proof_index=0, action="place_legal_hold")
    append_disposition_event(snapshot, proof_index=0, action="release_legal_hold")
    rows = snapshot["proofs"][0]["provenance"]["dispositions"]
    rows[-1]["id"] = rows[-2]["id"]


def _receipt_id_transplant(snapshot: dict[str, Any]) -> None:
    proof = snapshot["proofs"][0]
    old_id = proof["provenance"]["receipts"][0]["id"]
    new_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    for projection in (proof["provenance"], proof["acquisition"]):
        for row in projection["receipts"]:
            if row["id"] == old_id:
                row["id"] = new_id
        for row in projection["evidence_bindings"]:
            if row["receipt_id"] == old_id:
                row["receipt_id"] = new_id
    next(
        row for row in proof["provenance"]["members"] if row["receipt_id"] == old_id
    )["receipt_id"] = new_id


def _raw_binding_id_transplant(snapshot: dict[str, Any]) -> None:
    proof = snapshot["proofs"][0]
    old_id = proof["provenance"]["evidence_bindings"][0]["id"]
    new_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    for projection in (proof["provenance"], proof["acquisition"]):
        for row in projection["evidence_bindings"]:
            if row["id"] == old_id:
                row["id"] = new_id
    next(
        row
        for row in proof["provenance"]["members"]
        if row["raw_evidence_binding_id"] == old_id
    )["raw_evidence_binding_id"] = new_id


def _latest_watermark_id_transplant(snapshot: dict[str, Any]) -> None:
    later = max(
        snapshot["context"]["watermarks"],
        key=lambda row: row["capture_sequence"],
    )
    old_id = later["id"]
    new_id = "99999999-9999-4999-8999-999999999999"
    later["id"] = new_id
    context_binding = next(
        row
        for row in snapshot["context"]["lineage_bindings"]
        if row["watermark_event_id"] == old_id
    )
    context_binding["watermark_event_id"] = new_id
    proof = next(
        row for row in snapshot["proofs"] if row["binding_id"] == context_binding["id"]
    )
    proof["provenance"]["binding"]["watermark_event_id"] = new_id
    proof["acquisition"]["watermark_event"]["id"] = new_id
    for projection in (proof["provenance"], proof["acquisition"]):
        for receipt in projection["receipts"]:
            if receipt["watermark_event_id"] == old_id:
                receipt["watermark_event_id"] = new_id


def _operation_plan_id_transplant(snapshot: dict[str, Any]) -> None:
    proof = snapshot["proofs"][0]
    new_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    proof["acquisition"]["operation_plan"]["id"] = new_id
    binding_id = proof["binding_id"]
    next(
        row for row in snapshot["context"]["lineage_bindings"] if row["id"] == binding_id
    )["operation_plan_id"] = new_id
    proof["provenance"]["binding"]["operation_plan_id"] = new_id


def _evidence_id_transplant(snapshot: dict[str, Any]) -> None:
    proof = snapshot["proofs"][0]
    old_id = proof["acquisition"]["evidence_records"][0]["id"]
    new_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    next(
        row for row in proof["acquisition"]["evidence_records"] if row["id"] == old_id
    )["id"] = new_id
    for projection in (proof["provenance"], proof["acquisition"]):
        for row in projection["evidence_bindings"]:
            if row["evidence_record_id"] == old_id:
                row["evidence_record_id"] = new_id
    next(
        row
        for row in proof["provenance"]["members"]
        if row["evidence_record_id"] == old_id
    )["evidence_record_id"] = new_id


def _authority_event_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["operational_adoptions"][-1][
        "candidate_packet_fingerprint"
    ] = "0" * 64


def _group_review_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["relation_reviews"].pop()


def _score_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["operational_scores"][0]["score"] += 1


def _watermark_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["watermarks"][1]["previous_event_fingerprint"] = "0" * 64


def _lineage_cardinality_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"].pop()


def _signature_tamper(snapshot: dict[str, Any]) -> None:
    row = snapshot["proofs"][0]["provenance"]["receipts"][0]
    row["signature"] = ("A" if row["signature"][0] != "A" else "B") + row["signature"][1:]


def _raw_capture_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"][0]["acquisition"]["capture"]["raw_payload"]["sources"][
        "owned"
    ]["markdown_content"] += " changed"


def _capture_summary_state_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"][0]["acquisition"]["capture"]["acquisition_summary"][
        "state"
    ] = "failed"


def _passage_tamper(snapshot: dict[str, Any]) -> None:
    proof = snapshot["proofs"][0]
    for projection in (proof["provenance"], proof["acquisition"]):
        projection["evidence_bindings"][0]["passage_locator"]["evidence_start"] = 1


def _association_tamper(snapshot: dict[str, Any]) -> None:
    envelope = snapshot["proofs"][0]["acquisition"]["capture"]["raw_payload"][
        "evidence_vault_raw_provenance"
    ]
    envelope["external_identity_provenance"]["external_source_identity_id"] = "0" * 64


def _member_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"][0]["provenance"]["members"][0]["evidence_quote"] += " changed"


def _arrival_gap_tamper(snapshot: dict[str, Any]) -> None:
    first_received = datetime(2026, 8, 9, 5, 1, 1, tzinfo=timezone.utc).isoformat()
    first = snapshot["proofs"][0]
    for projection in (first["provenance"], first["acquisition"]):
        for receipt in projection["receipts"]:
            receipt["received_at"] = first_received


def _legal_hold(snapshot: dict[str, Any]) -> None:
    append_disposition_event(snapshot, proof_index=0, action="place_legal_hold")


def _hold_then_release(snapshot: dict[str, Any]) -> None:
    append_disposition_event(snapshot, proof_index=0, action="place_legal_hold")
    append_disposition_event(snapshot, proof_index=0, action="release_legal_hold")


def _runtime_revoke(snapshot: dict[str, Any]) -> None:
    append_disposition_event(snapshot, proof_index=0, action="revoke_runtime")


def _hold_then_other_revoke(snapshot: dict[str, Any]) -> None:
    append_disposition_event(snapshot, proof_index=0, action="place_legal_hold")
    append_disposition_event(snapshot, proof_index=1, action="revoke_runtime")


def _revoke_then_other_hold(snapshot: dict[str, Any]) -> None:
    append_disposition_event(snapshot, proof_index=0, action="revoke_runtime")
    append_disposition_event(snapshot, proof_index=1, action="place_legal_hold")


def _replace_exact(value: Any, old: str, new: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if child == old:
                value[key] = new
            else:
                _replace_exact(child, old, new)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if child == old:
                value[index] = new
            else:
                _replace_exact(child, old, new)


def _workspace_id_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"][0]["acquisition"]["workspace"]["id"] = (
        "ffffffff-ffff-4fff-8fff-ffffffffffff"
    )


def _brand_id_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"][0]["acquisition"]["brand"]["id"] = (
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    )


def _proof_id_tamper(
    snapshot: dict[str, Any], path: tuple[str | int, ...], new: str
) -> None:
    proof = snapshot["proofs"][0]
    row: Any = proof
    for key in path:
        row = row[key]
    _replace_exact(snapshot, row, new)


def _scan_id_tamper(snapshot: dict[str, Any]) -> None:
    _proof_id_tamper(
        snapshot,
        ("acquisition", "scan_run", "id"),
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    )


def _capture_id_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"][0]["acquisition"]["capture"]["id"] = (
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    )


def _operation_plan_id_tamper(snapshot: dict[str, Any]) -> None:
    _proof_id_tamper(
        snapshot,
        ("acquisition", "operation_plan", "id"),
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )


def _evidence_id_tamper(snapshot: dict[str, Any]) -> None:
    _proof_id_tamper(
        snapshot,
        ("acquisition", "evidence_records", 0, "id"),
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )


def _receipt_id_tamper(snapshot: dict[str, Any]) -> None:
    _proof_id_tamper(
        snapshot,
        ("acquisition", "receipts", 0, "id"),
        "99999999-9999-4999-8999-999999999999",
    )


def _raw_binding_id_tamper(snapshot: dict[str, Any]) -> None:
    _proof_id_tamper(
        snapshot,
        ("acquisition", "evidence_bindings", 0, "id"),
        "88888888-8888-4888-8888-888888888888",
    )


def _score_id_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["operational_scores"][0]["id"] = (
        "77777777-7777-4777-8777-777777777777"
    )


def _string_count_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["context"]["operational_score_count"] = "1"


def _bool_count_tamper(snapshot: dict[str, Any]) -> None:
    snapshot["proofs"][0]["acquisition"]["receipt_count"] = True


def _rehash_disposition(row: dict[str, Any], binding: dict[str, Any]) -> None:
    identity = {
        "schema_version": row["event_schema_version"],
        "binding_fingerprint": binding["binding_fingerprint"],
        "receipt_set_fingerprint": row["receipt_set_fingerprint"],
        "event_sequence": row["event_sequence"],
        "previous_event_fingerprint": row["previous_event_fingerprint"],
        "action": row["action"],
        "prior_state": row["prior_state"],
        "resulting_state": row["resulting_state"],
        "reason": row["reason"],
        "actor_id": row["actor_id"],
        "idempotency_key_hash": row["idempotency_key_hash"],
    }
    row["event_fingerprint"] = canonical_fingerprint(
        "evidence-vault-raw-provenance-disposition-event-v1",
        identity,
    )


def _initial_reason_tamper(snapshot: dict[str, Any]) -> None:
    provenance = snapshot["proofs"][0]["provenance"]
    row = provenance["dispositions"][0]
    row["reason"] = "different but internally hashed reason"
    _rehash_disposition(row, provenance["binding"])


def _initial_actor_tamper(snapshot: dict[str, Any]) -> None:
    provenance = snapshot["proofs"][0]["provenance"]
    row = provenance["dispositions"][0]
    row["actor_id"] = "different-internal-actor"
    _rehash_disposition(row, provenance["binding"])


def _initial_idempotency_tamper(snapshot: dict[str, Any]) -> None:
    provenance = snapshot["proofs"][0]["provenance"]
    row = provenance["dispositions"][0]
    row["idempotency_key_hash"] = "f" * 64
    _rehash_disposition(row, provenance["binding"])


def _initial_occurred_before_binding(snapshot: dict[str, Any]) -> None:
    provenance = snapshot["proofs"][0]["provenance"]
    binding_time = datetime.fromisoformat(provenance["binding"]["created_at"])
    provenance["dispositions"][0]["occurred_at"] = (
        binding_time - timedelta(microseconds=1)
    ).isoformat()


def _hold_release(snapshot: dict[str, Any]) -> dict[str, Any]:
    _hold_then_release(snapshot)
    return snapshot["proofs"][0]["provenance"]


def _duplicate_disposition_fingerprint(snapshot: dict[str, Any]) -> None:
    provenance = _hold_release(snapshot)
    provenance["dispositions"][2]["event_fingerprint"] = provenance["dispositions"][1][
        "event_fingerprint"
    ]


def _duplicate_disposition_idempotency(snapshot: dict[str, Any]) -> None:
    provenance = _hold_release(snapshot)
    release = provenance["dispositions"][2]
    release["idempotency_key_hash"] = provenance["dispositions"][1][
        "idempotency_key_hash"
    ]
    _rehash_disposition(release, provenance["binding"])


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        pytest.param(
            _context_overflow,
            C7ShadowReadinessReason.CONTEXT_BOUND_EXCEEDED,
            id="context-overflow",
        ),
        pytest.param(
            _provenance_overflow,
            C7ShadowReadinessReason.CONTEXT_BOUND_EXCEEDED,
            id="provenance-overflow",
        ),
        pytest.param(
            _acquisition_overflow,
            C7ShadowReadinessReason.CONTEXT_BOUND_EXCEEDED,
            id="acquisition-overflow",
        ),
        pytest.param(
            _forged_disposition_bound,
            C7ShadowReadinessReason.CONTEXT_BOUND_EXCEEDED,
            id="forged-disposition-bound",
        ),
        pytest.param(
            _watermark_head_count_tamper,
            C7ShadowReadinessReason.WATERMARK_WINDOW_INVALID,
            id="watermark-head-count-link",
        ),
        pytest.param(
            _final_clock_expiry,
            C7ShadowReadinessReason.LINEAGE_AUTHORITY_MISMATCH,
            id="final-clock-expiry",
        ),
        pytest.param(
            _string_count,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="strict-count-type",
        ),
        pytest.param(
            _score_row_id_tamper,
            C7ShadowReadinessReason.CURRENT_SCORE_INVALID,
            id="score-row-id",
        ),
        pytest.param(
            _operational_packet_id_transplant,
            C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID,
            id="operational-packet-id-transplant",
        ),
        pytest.param(
            _exact_source_id_transplant,
            C7ShadowReadinessReason.CURRENT_GROUP_INVALID,
            id="exact-source-id-transplant",
        ),
        pytest.param(
            _score_future_of_database,
            C7ShadowReadinessReason.CURRENT_SCORE_INVALID,
            id="score-future-of-final-clock",
        ),
        pytest.param(
            _score_before_adoption,
            C7ShadowReadinessReason.CURRENT_SCORE_INVALID,
            id="score-before-adoption",
        ),
        pytest.param(
            _adoption_storage_future,
            C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID,
            id="adoption-storage-future-of-final-clock",
        ),
        pytest.param(
            _review_event_id_tamper,
            C7ShadowReadinessReason.CURRENT_GROUP_INVALID,
            id="review-event-id",
        ),
        pytest.param(
            _review_rationale_tamper,
            C7ShadowReadinessReason.CURRENT_GROUP_INVALID,
            id="review-request-rationale",
        ),
        pytest.param(
            _review_source_packet_kind_tamper,
            C7ShadowReadinessReason.CURRENT_GROUP_INVALID,
            id="review-source-packet-kind",
        ),
        pytest.param(
            _adoption_resolution_tamper,
            C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID,
            id="adoption-resolution-link",
        ),
        pytest.param(
            _initial_disposition_actor_tamper,
            C7ShadowReadinessReason.DISPOSITION_INVALID,
            id="initial-retain-contract",
        ),
        pytest.param(
            _duplicate_disposition_id,
            C7ShadowReadinessReason.DISPOSITION_INVALID,
            id="disposition-duplicate-id",
        ),
        pytest.param(
            _receipt_id_transplant,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="receipt-id-transplant",
        ),
        pytest.param(
            _raw_binding_id_transplant,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="raw-binding-id-transplant",
        ),
        pytest.param(
            _latest_watermark_id_transplant,
            C7ShadowReadinessReason.WATERMARK_WINDOW_INVALID,
            id="watermark-id-transplant",
        ),
        pytest.param(
            _operation_plan_id_transplant,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="operation-plan-id-transplant",
        ),
        pytest.param(
            _evidence_id_transplant,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="evidence-id-transplant",
        ),
        pytest.param(
            _bool_count_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="strict-bool-count",
        ),
        pytest.param(
            _workspace_id_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="stable-workspace-id",
        ),
        pytest.param(
            _brand_id_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="stable-brand-id",
        ),
        pytest.param(
            _scan_id_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="stable-scan-id",
        ),
        pytest.param(
            _capture_id_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="stable-capture-id",
        ),
        pytest.param(
            _initial_reason_tamper,
            C7ShadowReadinessReason.DISPOSITION_INVALID,
            id="initial-disposition-reason",
        ),
        pytest.param(
            _initial_idempotency_tamper,
            C7ShadowReadinessReason.DISPOSITION_INVALID,
            id="initial-disposition-idempotency",
        ),
        pytest.param(
            _initial_occurred_before_binding,
            C7ShadowReadinessReason.DISPOSITION_INVALID,
            id="initial-disposition-before-binding",
        ),
        pytest.param(
            _duplicate_disposition_fingerprint,
            C7ShadowReadinessReason.DISPOSITION_INVALID,
            id="duplicate-disposition-fingerprint",
        ),
        pytest.param(
            _duplicate_disposition_idempotency,
            C7ShadowReadinessReason.DISPOSITION_INVALID,
            id="duplicate-disposition-idempotency",
        ),
        pytest.param(
            _authority_event_tamper,
            C7ShadowReadinessReason.CURRENT_AUTHORITY_INVALID,
            id="authority-event",
        ),
        pytest.param(
            _group_review_tamper,
            C7ShadowReadinessReason.CURRENT_GROUP_INVALID,
            id="exact-group-review",
        ),
        pytest.param(
            _score_tamper,
            C7ShadowReadinessReason.CURRENT_SCORE_INVALID,
            id="current-score",
        ),
        pytest.param(
            _watermark_tamper,
            C7ShadowReadinessReason.WATERMARK_WINDOW_INVALID,
            id="watermark-chain",
        ),
        pytest.param(
            _lineage_cardinality_tamper,
            C7ShadowReadinessReason.LINEAGE_CARDINALITY_INVALID,
            id="lineage-cardinality",
        ),
        pytest.param(
            _signature_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="receipt-signature",
        ),
        pytest.param(
            _raw_capture_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="raw-capture",
        ),
        pytest.param(
            _capture_summary_state_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="capture-summary-state",
        ),
        pytest.param(
            _passage_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="passage-locator",
        ),
        pytest.param(
            _association_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="external-association",
        ),
        pytest.param(
            _member_tamper,
            C7ShadowReadinessReason.RAW_PROVENANCE_INVALID,
            id="verified-member",
        ),
        pytest.param(
            _arrival_gap_tamper,
            C7ShadowReadinessReason.WATERMARK_WINDOW_INVALID,
            id="receipt-arrival-gap",
        ),
        pytest.param(
            _legal_hold,
            C7ShadowReadinessReason.LEGAL_HOLD_PRESENT,
            id="legal-hold-head",
        ),
        pytest.param(
            _hold_then_release,
            C7ShadowReadinessReason.VERIFIED_RAW_READY,
            id="released-hold-head",
        ),
        pytest.param(
            _runtime_revoke,
            C7ShadowReadinessReason.RUNTIME_REVOKED,
            id="runtime-revoked-head",
        ),
        pytest.param(
            _hold_then_other_revoke,
            C7ShadowReadinessReason.RUNTIME_REVOKED,
            id="runtime-revocation-priority-after-hold",
        ),
        pytest.param(
            _revoke_then_other_hold,
            C7ShadowReadinessReason.RUNTIME_REVOKED,
            id="runtime-revocation-priority-before-hold",
        ),
    ],
)
def test_ready_fixture_fails_closed_at_each_proof_layer(
    ready_fixture: ReadyFixture,
    mutation: Mutation,
    expected_reason: C7ShadowReadinessReason,
) -> None:
    snapshot = clone_ready_snapshot(ready_fixture)
    mutation(snapshot)

    result = ready_fixture.evaluator.evaluate("causaprima.ai", snapshot)

    assert result.reason is expected_reason
    assert result.ready is (expected_reason is C7ShadowReadinessReason.VERIFIED_RAW_READY)
