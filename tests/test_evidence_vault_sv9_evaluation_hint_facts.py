from __future__ import annotations

import hashlib
from copy import deepcopy
from uuid import uuid5

import pytest

from src.history import repository as history
from src.history.models import CaptureConflictError


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _uuid(value: str) -> str:
    return str(uuid5(history._ID_NAMESPACE, value))


def _relation(*, tile_id: str = "M1", relation_id: str | None = None, evidence_id: str | None = None, source_id: str | None = None) -> dict[str, object]:
    return {
        "tile_id": tile_id,
        "relation_id": relation_id or _sha(f"relation:{tile_id}"),
        "evidence_id": evidence_id or _sha("evidence:one"),
        "source_identity_id": source_id or _sha("source:one"),
        "claim_id": None,
        "polarity": "supports",
        "review_status": "unreviewed",
        "decision_event_id": None,
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }


def _evidence(*, label: str = "one", evidence_id: str | None = None, source_id: str | None = None) -> dict[str, str]:
    return {
        "evidence_record_id": _uuid(f"record:{label}"),
        "evidence_id": evidence_id or _sha(f"evidence:{label}"),
        "source_identity_id": source_id or _sha(f"source:{label}"),
    }


def _operation(*, relations: list[dict[str, object]] | None = None, status: str = "completed", output_kind: str = "candidate_overlay") -> dict[str, object]:
    return {
        "operation_plan_id": _uuid("operation"),
        "status": status,
        "result_payload": {"output_kind": output_kind, "basis_relations": relations if relations is not None else [_relation()]},
    }


def test_hint_seeds_have_exact_non_authoritative_shape_and_stable_uuid() -> None:
    operation, evidence, relation = _operation(), [_evidence()], _relation()

    seeds = history._sv9_evaluation_hint_seeds(operation, evidence, None)

    assert seeds == history._sv9_evaluation_hint_seeds(deepcopy(operation), list(reversed(evidence)), None)
    assert len(seeds) == 1 and set(seeds[0]) == {"hint_id", "tile_id", "component_key", "evidence_record_id", "provenance_fingerprint"}
    assert seeds[0] == {
        "hint_id": str(uuid5(history._ID_NAMESPACE, f"{operation['operation_plan_id']}:evidence-vault-sv9-evaluation-hint-seed-v1:{relation['relation_id']}")),
        "tile_id": "M1",
        "component_key": "mission",
        "evidence_record_id": evidence[0]["evidence_record_id"],
        "provenance_fingerprint": relation["relation_id"],
    }


def test_hint_seeds_are_registry_ordered_and_reorder_invariant() -> None:
    first, second = _relation(tile_id="M1"), _relation(tile_id="M2")
    second["relation_id"] = _sha("relation:M2:second")
    second["evidence_id"] = _sha("evidence:two")
    second["source_identity_id"] = _sha("source:two")
    evidence = [_evidence(), _evidence(label="two")]
    operation = _operation(relations=[second, first])

    seeds = history._sv9_evaluation_hint_seeds(operation, list(reversed(evidence)), None)

    assert [(row["tile_id"], row["provenance_fingerprint"]) for row in seeds] == [("M1", first["relation_id"]), ("M2", second["relation_id"])]
    assert seeds == history._sv9_evaluation_hint_seeds(_operation(relations=[first, second]), evidence, None)


@pytest.mark.parametrize(
    "relations,evidence",
    [
        ([_relation(tile_id="unknown")], [_evidence()]),
        ([_relation(evidence_id=_sha("missing"), source_id=_sha("missing-source"))], [_evidence()]),
        ([_relation()], [_evidence(), {**_evidence(), "evidence_record_id": _uuid("record:duplicate")}]),
        ([_relation(), _relation(relation_id=_sha("second-provenance"))], [_evidence()]),
        ([_relation(), _relation(tile_id="M2", relation_id=_sha("relation:M1"), evidence_id=_sha("evidence:two"), source_id=_sha("source:two"))], [_evidence(), _evidence(label="two")]),
    ],
)
def test_hint_seeds_fail_closed_for_unknown_or_ambiguous_or_duplicate_inputs(relations, evidence) -> None:
    with pytest.raises(CaptureConflictError):
        history._sv9_evaluation_hint_seeds(_operation(relations=relations), evidence, None)


def test_accepted_operational_relation_is_excluded_without_becoming_authority() -> None:
    relation = _relation()
    seeds = history._sv9_evaluation_hint_seeds(
        _operation(relations=[relation]), [_evidence()], {"accepted": [{"basis": [relation]}]}
    )
    assert seeds == []


def test_hint_seeds_are_empty_for_not_required_non_candidate_or_empty_basis() -> None:
    assert history._sv9_evaluation_hint_seeds(_operation(status="not_required", relations=[]), [_evidence()], None) == []
    assert history._sv9_evaluation_hint_seeds(_operation(output_kind="no_delta"), [_evidence()], None) == []
    assert history._sv9_evaluation_hint_seeds(_operation(relations=[]), [_evidence()], None) == []


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        self.calls.append(query)
        if "pg_advisory_xact_lock" in query:
            return _Rows([])
        if "evidence_records" in query:
            return _Rows(self.rows)
        raise AssertionError(f"unexpected query: {query}")


def _loader_row() -> dict[str, object]:
    return {
        "id": _uuid("record:one"), "evidence_ref": "evidence-1", "content_hash": _sha("content"),
        "source": "web", "source_class": "owned_copy", "evidence_type": "owned_copy", "url": "https://brand.test",
        "content": "Evidence text", "content_raw": None, "confidence": "high", "metadata": {"source_class": "owned_copy"},
    }


def _fake_loader(monkeypatch, operation, *, validate_result=True, reject_validation=False):
    row, connection, validations = _loader_row(), _Connection([_loader_row()]), []
    context = {
        "workspace_id": _uuid("workspace"), "brand_id": _uuid("brand"), "scan_run_id": _uuid("scan-run"),
        "source_scan_id": "scan", "canonical_domain": "brand.test", "capture_id": _uuid("capture"),
        "capture_fingerprint": _sha("capture"), "operation_plan_id": operation["operation_plan_id"], "operation_fingerprint": _sha("operation"),
    }
    repository = object.__new__(history.PostgresHistoryRepository)
    repository._ensure_migrated = lambda: None
    repository._connect = lambda: connection
    monkeypatch.setattr(history, "_verify_exact_migration_head_under_shared_lock", lambda _conn: None)
    monkeypatch.setattr(history, "_sv9_judgment_context", lambda *_args: context)
    monkeypatch.setattr(history, "_vault_operation_row", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(history, "_vault_operation_plan_record", lambda _row: deepcopy(operation))
    monkeypatch.setattr(history, "_replay_sv9_judgment_authority", lambda *_args: None)
    monkeypatch.setattr(history, "_project_vault_operational_memory_authority_chain", lambda *_args: [])
    monkeypatch.setattr(history, "project_evidence_memory_row_identity", lambda *_args, **_kwargs: {"evidence_id": _sha("evidence:one"), "document_id": _sha("source:one")})

    if validate_result:
        def validate(conn, result, *, operation, evidence_rows=None):
            validations.append((conn, result, operation, evidence_rows))
            if reject_validation:
                raise CaptureConflictError("invalid immutable result")

        monkeypatch.setattr(history, "_validate_vault_operation_result_for_plan", validate)
    return repository, connection, validations, row


def test_facts_loader_validates_before_exposing_seeds_and_keeps_query_shape(monkeypatch) -> None:
    baseline_operation = _operation(relations=[])
    baseline_repository, baseline_connection, baseline_validations, _ = _fake_loader(monkeypatch, baseline_operation)
    baseline = baseline_repository.load_evidence_vault_sv9_authoritative_relation_facts("scan")
    seeded_operation = _operation()
    seeded_repository, seeded_connection, seeded_validations, expected_row = _fake_loader(monkeypatch, seeded_operation)
    seeded = seeded_repository.load_evidence_vault_sv9_authoritative_relation_facts("scan")

    assert baseline is not None and baseline["evaluation_hint_seeds"] == []
    assert seeded is not None and len(seeded["evaluation_hint_seeds"]) == 1
    assert len(seeded_connection.calls) - len(baseline_connection.calls) == 0
    assert seeded_connection.calls == baseline_connection.calls
    assert len(baseline_validations) == len(seeded_validations) == 1
    assert seeded_validations[0][3] == [expected_row]


def test_facts_loader_never_exposes_seeds_from_a_malformed_result(monkeypatch) -> None:
    repository, _connection, validations, _row = _fake_loader(monkeypatch, _operation(), validate_result=False)
    with pytest.raises(CaptureConflictError, match="operation result contract is invalid"):
        repository.load_evidence_vault_sv9_authoritative_relation_facts("scan")
    assert validations == []
