# fmt: off
from copy import deepcopy
from pathlib import Path
import json

import pytest

from src.services import evidence_vault_sv9_review_resolution as resolution
from src.services import evidence_vault_sv9_authority_event as authority_event


_H = lambda number: f"{number:064x}"
_U = lambda number: f"00000000-0000-0000-0000-{number:012d}"
_MISSING = object()


def _overlay():
    return {
        "schema_version": "evidence-vault-sv9-judgment-authority-event-v1",
        "request": {
            "schema_version": "evidence-vault-sv9-judgment-authority-request-v1",
            "action": "reopen_authority",
            "candidate_id": None,
            "source_scan_id": "review-scan",
            "expected_predecessor_event_fingerprint": _H(30),
            "delta_fingerprint": _H(29),
        },
        "review_state": "pending",
        "signed_delta": {"canonical_delta_fingerprint": _H(29)},
    }


def _candidate_payload(
    *,
    version="v2",
    adoption_event_id=_U(25),
    adoption_sequence=6,
):
    value = {
        "schema_version": f"evidence-vault-sv9-judgment-candidate-{version}",
        "plan": {},
        "canonical_plan_fingerprint": _H(17),
        "current_series_fingerprint": _H(18),
        "candidate_series_fingerprint": _H(19),
        "component_evaluations": [],
        "evidence_bindings": [],
        "candidate_tile_judgments": [],
        "candidate_component_sentinels": [],
        "assessment": {},
        "telemetry": {
            "call_count": 0,
            "calls_avoided": 0,
            "reused_tile_count": 0,
            "evaluated_tile_count": 0,
        },
        "evaluation_bundle_fingerprint": resolution.memory.canonical_fingerprint(
            "sv9-judgment-evaluation-bundle-v1",
            {"canonical_plan_fingerprint": _H(17), "evaluations": []},
        ),
        "assessment_fingerprint": _H(21),
        "score_fingerprint": _H(22),
    }
    if version == "v2":
        value["authoritative_relation_witness"] = {
            "schema_version": "evidence-vault-sv9-authoritative-relation-witness-v1",
            "source_scan_id": "review-scan",
            "operational_witness": {
                "canonical_memory_version": _H(30),
                "adoption_event_id": adoption_event_id,
                "adoption_sequence": adoption_sequence,
                "candidate_packet_fingerprint": _H(31),
                "request_fingerprint": _H(32),
            },
            "authoritative_relations": [],
            "projection_fingerprint": _H(33),
            "witness_fingerprint": _H(34),
        }
    value["complete_record_fingerprint"] = authority_event.candidate_complete_record_fingerprint(value)
    return value


def _record(
    decision="approve",
    *,
    candidate_schema_version="evidence-vault-sv9-judgment-candidate-v2",
    candidate_payload=_MISSING,
    active_authority_event_id=_U(25),
    active_authority_event_fingerprint=_H(26),
    active_authority_event_sequence=6,
):
    if candidate_payload is _MISSING:
        candidate_payload = (
            _candidate_payload(
                version="v2",
                adoption_event_id=active_authority_event_id,
                adoption_sequence=active_authority_event_sequence,
            )
            if candidate_schema_version.endswith("v2")
            else None
        )
    return resolution.build_evidence_vault_sv9_review_resolution(
        id=_U(1),
        workspace_id=_U(10),
        brand_id=_U(11),
        candidate_id=_U(12),
        scan_run_id=_U(13),
        capture_id=_U(14),
        operation_plan_id=_U(15),
        source_scan_id="review-scan",
        candidate_schema_version=candidate_schema_version,
        candidate_complete_record_fingerprint=(
            candidate_payload["complete_record_fingerprint"]
            if candidate_payload is not None
            else _H(16)
        ),
        canonical_plan_fingerprint=_H(17),
        current_series_fingerprint=_H(18),
        candidate_series_fingerprint=_H(19),
        evaluation_bundle_fingerprint=(
            candidate_payload["evaluation_bundle_fingerprint"]
            if candidate_payload is not None
            else _H(20)
        ),
        assessment_fingerprint=_H(21),
        score_fingerprint=_H(22),
        decision=decision,
        expected_authority_event_id=_U(23),
        expected_authority_event_fingerprint=_H(24),
        expected_authority_event_sequence=7,
        active_authority_event_id=active_authority_event_id,
        active_authority_event_fingerprint=active_authority_event_fingerprint,
        active_authority_event_sequence=active_authority_event_sequence,
        evaluated_authority_event_id=active_authority_event_id,
        evaluated_authority_event_fingerprint=active_authority_event_fingerprint,
        evaluated_authority_sequence=active_authority_event_sequence,
        reopen_authority_event_id=_U(23),
        reopen_authority_event_fingerprint=_H(24),
        reopen_authority_event_sequence=7,
        reopen_active_parent_event_id=active_authority_event_id,
        reopen_active_parent_event_fingerprint=active_authority_event_fingerprint,
        reopen_active_parent_event_sequence=active_authority_event_sequence,
        reopen_delta_fingerprint=_H(29),
        reopen_review_overlay=_overlay(),
        candidate_payload=candidate_payload,
        successor_authority_event_id=None if decision == "reject" else _U(27),
        successor_authority_event_fingerprint=None if decision == "reject" else _H(28),
        successor_authority_event_sequence=None if decision == "reject" else 8,
        created_at="2026-09-20T00:00:00+00:00",
    )


def test_approve_round_trip_is_canonical_and_does_not_publish():
    value = _record()
    replayed = resolution.validate_evidence_vault_sv9_review_resolution(
        json.loads(json.dumps(value)),
        candidate_payload=_candidate_payload(),
    )
    assert replayed == value
    assert value["decision"] == "approve"
    assert value["successor_authority_event_sequence"] == value["expected_authority_event_sequence"] + 1
    assert not hasattr(resolution, "publish_evidence_vault_sv9_review_resolution")
    assert not hasattr(resolution, "create_evidence_vault_sv9_authority_event")


def test_reject_has_no_successor_and_preserves_the_expected_authority_binding():
    value = _record("reject")
    assert resolution.validate_evidence_vault_sv9_review_resolution(
        value, candidate_payload=_candidate_payload()
    ) == value
    assert value["successor_authority_event_id"] is None
    assert value["successor_authority_event_fingerprint"] is None
    assert value["successor_authority_event_sequence"] is None
    assert value["expected_authority_event_id"] == _U(23)
    assert value["expected_authority_event_fingerprint"] == _H(24)
    assert value["expected_authority_event_sequence"] == 7


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("reopen_authority_event_id", _U(28)),
        ("reopen_authority_event_fingerprint", _H(31)),
        ("reopen_authority_event_sequence", 8),
        ("reopen_active_parent_event_id", _U(24)),
        ("reopen_active_parent_event_fingerprint", _H(32)),
        ("reopen_active_parent_event_sequence", 5),
        ("evaluated_authority_event_id", _U(24)),
        ("evaluated_authority_event_fingerprint", _H(32)),
        ("evaluated_authority_sequence", 5),
        ("reopen_delta_fingerprint", _H(33)),
        ("reopen_review_overlay_fingerprint", _H(34)),
    ],
)
def test_resolution_is_bound_to_the_concrete_reopen_and_overlay(field, replacement):
    value = _record()
    value[field] = replacement
    with pytest.raises(resolution.EvidenceVaultSv9ReviewResolutionError):
        resolution.validate_evidence_vault_sv9_review_resolution(
            value, candidate_payload=_candidate_payload()
        )


def test_review_overlay_requires_the_reopen_request_and_delta_binding():
    bad = _overlay()
    bad["signed_delta"]["canonical_delta_fingerprint"] = _H(31)
    with pytest.raises(resolution.EvidenceVaultSv9ReviewResolutionError):
        resolution.review_overlay_fingerprint(bad)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.pop("candidate_id"),
        lambda value: value.__setitem__("unexpected", 1),
        lambda value: value.__setitem__("expected_authority_event_sequence", True),
        lambda value: value.__setitem__("decision", 1),
        lambda value: value.__setitem__("candidate_complete_record_fingerprint", "A" * 64),
    ],
)
def test_missing_extra_and_wrong_type_records_fail_closed(mutator):
    value = _record()
    mutator(value)
    with pytest.raises(resolution.EvidenceVaultSv9ReviewResolutionError):
        resolution.validate_evidence_vault_sv9_review_resolution(
            value, candidate_payload=_candidate_payload()
        )


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("expected_authority_event_sequence", 8),
        ("active_authority_event_sequence", 8),
        ("successor_authority_event_sequence", 7),
        ("successor_authority_event_sequence", 9),
    ],
)
def test_stale_or_cas_mismatched_authority_bindings_fail_closed(field, replacement):
    value = _record()
    value[field] = replacement
    with pytest.raises(resolution.EvidenceVaultSv9ReviewResolutionError):
        resolution.validate_evidence_vault_sv9_review_resolution(
            value, candidate_payload=_candidate_payload()
        )


@pytest.mark.parametrize(
    "decision, successor_fields",
    [
        ("reject", {"successor_authority_event_id": _U(27), "successor_authority_event_fingerprint": _H(28), "successor_authority_event_sequence": 8}),
        ("approve", {"successor_authority_event_id": None, "successor_authority_event_fingerprint": None, "successor_authority_event_sequence": None}),
        ("approve", {"successor_authority_event_id": _U(27), "successor_authority_event_fingerprint": None, "successor_authority_event_sequence": 8}),
    ],
)
def test_decision_and_successor_shapes_are_inseparable(decision, successor_fields):
    value = _record(decision)
    value.update(successor_fields)
    with pytest.raises(resolution.EvidenceVaultSv9ReviewResolutionError):
        resolution.validate_evidence_vault_sv9_review_resolution(
            value, candidate_payload=_candidate_payload()
        )


def test_resolution_key_hash_changes_with_semantics_and_rejects_stale_hash():
    approve = _record("approve")
    reject = _record("reject")
    assert approve["resolution_key_hash"] != reject["resolution_key_hash"]
    tampered = deepcopy(approve)
    tampered["decision"] = "reject"
    tampered["successor_authority_event_id"] = None
    tampered["successor_authority_event_fingerprint"] = None
    tampered["successor_authority_event_sequence"] = None
    with pytest.raises(resolution.EvidenceVaultSv9ReviewResolutionError):
        resolution.validate_evidence_vault_sv9_review_resolution(
            tampered, candidate_payload=_candidate_payload()
        )


def test_resolution_idempotency_key_includes_review_binding_facts():
    value = _record()
    changed = deepcopy(value)
    changed["reopen_delta_fingerprint"] = _H(35)
    changed["reopen_review_overlay_fingerprint"] = _H(36)
    assert resolution.review_resolution_idempotency_fingerprint(
        value, candidate_payload=_candidate_payload()
    ) != resolution.review_resolution_idempotency_fingerprint(
        changed, candidate_payload=_candidate_payload()
    )


def test_evaluated_authority_head_changes_the_idempotency_identity():
    first = _record()
    second = _record(
        active_authority_event_id=_U(31),
        active_authority_event_fingerprint=_H(32),
    )
    assert resolution.review_resolution_idempotency_fingerprint(
        first, candidate_payload=_candidate_payload()
    ) != resolution.review_resolution_idempotency_fingerprint(
        second,
        candidate_payload=_candidate_payload(
            adoption_event_id=_U(31),
        ),
    )


def test_v2_candidate_witness_cannot_point_at_another_head_with_the_same_series():
    value = _record()
    other_head = _candidate_payload(adoption_event_id=_U(99))
    tampered = deepcopy(value)
    tampered["candidate_complete_record_fingerprint"] = other_head[
        "complete_record_fingerprint"
    ]
    tampered["resolution_key_hash"] = resolution._key_hash(tampered)
    with pytest.raises(resolution.EvidenceVaultSv9ReviewResolutionError):
        resolution.validate_evidence_vault_sv9_review_resolution(
            tampered, candidate_payload=other_head
        )


def test_v2_candidate_requires_its_existing_authority_witness():
    value = _record()
    with pytest.raises(resolution.EvidenceVaultSv9ReviewResolutionError):
        resolution.validate_evidence_vault_sv9_review_resolution(value)


def test_v1_candidate_without_a_witness_keeps_legacy_compatibility():
    value = _record(
        candidate_schema_version="evidence-vault-sv9-judgment-candidate-v1",
        candidate_payload=None,
    )
    assert resolution.validate_evidence_vault_sv9_review_resolution(value) == value


def test_sql_contract_038_is_append_only_and_trigger_bound():
    sql = Path("src/history/migrations/038_evidence_vault_sv9_judgment_review_resolutions.sql").read_text()
    required = (
        "CREATE TABLE b3s_history.evidence_vault_sv9_judgment_review_resolutions",
        "resolution_key_hash",
        "candidate_complete_record_fingerprint",
        "scan_run_id",
        "capture_id",
        "operation_plan_id",
        "source_scan_id",
        "UNIQUE (workspace_id, brand_id, resolution_key_hash)",
        "UNIQUE (workspace_id, brand_id, candidate_id)",
        "expected_authority_event_id",
        "expected_authority_event_fingerprint",
        "expected_authority_event_sequence",
        "active_authority_event_id",
        "evaluated_authority_event_id",
        "evaluated_authority_event_fingerprint",
        "evaluated_authority_sequence",
        "reopen_authority_event_id",
        "reopen_authority_event_fingerprint",
        "reopen_authority_event_sequence",
        "reopen_active_parent_event_id",
        "reopen_active_parent_event_fingerprint",
        "reopen_active_parent_event_sequence",
        "reopen_delta_fingerprint",
        "reopen_review_overlay_fingerprint",
        "successor_authority_event_id",
        "CHECK (decision IN ('approve', 'reject'))",
        "validate_vault_sv9_judgment_review_resolution_insert",
        "reject_vault_sv9_judgment_review_resolution_mutation",
        "BEFORE UPDATE OR DELETE OR TRUNCATE",
        "pg_advisory_xact_lock",
        "b3s_history_vault_provenance_owner",
        "b3s_pr71_app_runtime",
        "GRANT SELECT, INSERT",
        "event_type IN ('adopt', 'supersede')",
        "reject_vault_sv9_judgment_review_resolution_candidate_authority_conflict",
        "BEFORE INSERT ON b3s_history.evidence_vault_sv9_judgment_authority_events",
        "resolutions.decision = 'reject'",
        "resolutions.candidate_id = NEW.candidate_id",
        "cannot reject an adopted candidate",
        "evidence_vault_sv9_judgment_review_fingerprint",
        "evidence_vault_canonical_json",
        "candidate_row.candidate_payload",
        "candidate_complete_record_fingerprint(candidate)",
        "candidate witness authority binding is invalid",
        "complete_record_fingerprint",
        "request_fingerprint",
        "event_fingerprint",
        "resolution_key_hash IS DISTINCT FROM",
        "expected_event.active_parent_event_id IS DISTINCT FROM active_event.id",
        "NEW.evaluated_authority_event_id IS DISTINCT FROM active_event.id",
        "event_payload -> 'signed_delta'",
        "evidence_vault_json_has_exact_keys",
        "CREATE CONSTRAINT TRIGGER validate_vault_sv9_judgment_review_resolution_authority_pair",
        "DEFERRABLE INITIALLY DEFERRED",
        "sv9_review_resolution",
        "resolutions.successor_authority_event_id = NEW.id",
        "requires its linked resolution",
    )
    for fragment in required:
        assert fragment in sql, fragment
    candidate_guard_start = sql.index(
        "CREATE FUNCTION b3s_history.reject_vault_sv9_judgment_review_resolution_candidate_authority_conflict"
    )
    candidate_guard_end = sql.index(
        "\nCREATE FUNCTION b3s_history.validate_vault_sv9_judgment_review_resolution_authority_pair",
        candidate_guard_start,
    )
    candidate_guard = sql[candidate_guard_start:candidate_guard_end]
    pair_guard_start = candidate_guard_end + 1
    pair_guard_end = sql.index("\nCREATE TRIGGER validate_vault_sv9_judgment_review_resolution_insert", pair_guard_start)
    pair_guard = sql[pair_guard_start:pair_guard_end]
    shared_lock_key = "hashtextextended(NEW.workspace_id::text || ':' || NEW.brand_id::text, 0)"
    assert "NEW.event_type = 'supersede' AND NOT (NEW.event_payload ? 'sv9_review_resolution')" in candidate_guard
    assert candidate_guard.index("RETURN NEW") < candidate_guard.index("pg_advisory_xact_lock")
    assert candidate_guard.count("pg_advisory_xact_lock") == 1
    assert shared_lock_key in candidate_guard
    assert candidate_guard.index("pg_advisory_xact_lock") < candidate_guard.index("EXISTS")
    assert pair_guard.count("pg_advisory_xact_lock") == 1
    assert shared_lock_key in pair_guard
    assert sql.count(shared_lock_key) == 3
    assert pair_guard.index("RETURN NULL") < pair_guard.index("pg_advisory_xact_lock")
    assert pair_guard.index("pg_advisory_xact_lock") < pair_guard.index("EXISTS")
    assert "UPDATE b3s_history.evidence_vault_sv9_judgment_review_resolutions" not in sql
    assert "DELETE FROM b3s_history.evidence_vault_sv9_judgment_review_resolutions" not in sql
    assert "UPDATE b3s_history.evidence_vault_sv9_judgment_authority_events" not in sql
    assert "event_row.predecessor_event_fingerprint" not in sql
    assert "event_row.candidate_complete_record_fingerprint" not in sql
    # Static fragment checks do not execute PostgreSQL triggers, privileges, or
    # deferred constraints; disposable PostgreSQL verification remains separate.
# fmt: on
