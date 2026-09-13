from copy import deepcopy
import hashlib

import pytest

from src.history import repository as history
from src.services.evidence_memory_identity_v2 import project_evidence_memory_row_identity
from src.services.evidence_vault_canonical_core import (
    build_candidate_tile,
    build_tile_contract_registry,
    canonical_fingerprint,
)
from src.services.evidence_vault_operational_memory import build_operational_memory_packet
from src.services.evidence_vault_sv9_authoritative_relations import (
    build_evidence_vault_sv9_authoritative_relation_witness,
)
from src.services.evidence_vault_sv9_judgment_delta import (
    build_authoritative_evidence_tile_relation,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, packet_row, evidence_rows):
        self.packet_row = packet_row
        self.evidence_rows = evidence_rows
        self.calls = []

    def execute(self, query, args):
        self.calls.append((query, args))
        if "canonical_memory_packets" in query:
            return _Rows([self.packet_row])
        return _Rows(self.evidence_rows)


def _evidence(seed: str) -> tuple[dict, dict]:
    value = {
        "ref": f"evidence-{seed}",
        "source": "https://example.com",
        "evidence_type": "web",
        "url": f"https://example.com/{seed}",
        "content": f"Verified evidence content for {seed} with enough words.",
        "confidence": "1",
        "metadata": {"source_class": "owned_copy"},
    }
    identity = project_evidence_memory_row_identity(value, brand_domain="example.com")
    row = {
        "id": f"00000000-0000-0000-0000-{int(seed):012d}",
        "evidence_ref": value["ref"],
        "content_hash": identity["content_hash"],
        "source": value["source"],
        "source_class": "owned_copy",
        "evidence_type": value["evidence_type"],
        "url": value["url"],
        "content": value["content"],
        "content_raw": None,
        "confidence": value["confidence"],
        "metadata": value["metadata"],
    }
    return value, row


def _fixture():
    values = [_evidence("1"), _evidence("2")]
    by_tile = {"M1": values[0][0], "M2": values[1][0]}

    def basis(tile_id: str, value: dict) -> dict:
        identity = project_evidence_memory_row_identity(value, brand_domain="example.com")
        return {
            "relation_id": _sha(f"{tile_id}-original-relation"),
            "evidence_id": identity["evidence_id"],
            "source_identity_id": identity["document_id"],
            "claim_id": None,
            "polarity": "contradicts" if tile_id == "M1" else "supports",
            "review_status": "accepted",
            "decision_event_id": f"review-{tile_id}",
            "absence_test_contract_id": None,
            "coverage_assessment_id": None,
            "coverage_status": None,
            "tested_scope": None,
            "observed_result": None,
        }

    candidates = [
        build_candidate_tile(
            tile_id=str(tile["tile_id"]),
            basis=[basis(str(tile["tile_id"]), by_tile[str(tile["tile_id"])])]
            if str(tile["tile_id"]) in by_tile
            else [],
        )
        for tile in build_tile_contract_registry()["tiles"]
    ]
    source_packet_fingerprint = _sha("source-packet")
    packet = build_operational_memory_packet(
        brand_identity="example.com",
        source_candidate_packet_fingerprint=source_packet_fingerprint,
        aggregation_policy_fingerprint=_sha("aggregation"),
        candidate_tiles=candidates,
        dispositions={
            tile_id: {
                "authority_state": "accepted",
                "review_state": "none",
                "authority_profile_id": "human-reviewed-test-v1",
                "authority_source": "human",
                "decision_event_id": f"review-{tile_id}",
            }
            for tile_id in ("M1", "M2")
        },
    )
    resolution = history._operational_storage_resolution(packet)
    packet_row = {
        "packet_kind": "operational_v2",
        "packet_payload": packet,
        "reference_resolution": resolution,
        "packet_fingerprint": packet["candidate_packet_fingerprint"],
        "schema_version": packet["schema_version"],
        "brand_identity": packet["brand_identity"],
        "parent_canonical_memory_version": packet["current_canonical_memory_version"],
        "accepted_memory_candidate_version": packet["accepted_memory_candidate_version"],
        "candidate_overlay_version": packet["candidate_overlay_version"],
        "reference_resolution_fingerprint": resolution["reference_resolution_fingerprint"],
        "candidate_tiles": packet["scoring_projection"]["tiles"],
        "authority_state": "pending_review",
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "created_at": "2026-09-10T00:00:00+00:00",
    }
    capture = {
        "capture_id": "00000000-0000-0000-0000-000000000009",
        "capture_fingerprint": _sha("capture"),
    }
    operation = {
        "operation_id": "00000000-0000-0000-0000-000000000010",
        "operation_fingerprint": _sha("operation"),
    }
    operational_witness = {
        "canonical_memory_version": _sha("memory"),
        "adoption_event_id": "00000000-0000-0000-0000-000000000011",
        "adoption_sequence": 1,
        "candidate_packet_fingerprint": packet["candidate_packet_fingerprint"],
        "request_fingerprint": _sha("request"),
    }
    first_value, first_row = values[0]
    relation = build_authoritative_evidence_tile_relation(
        tile_id="M1",
        component_key="mission",
        disposition="relevant",
        evidence_ref=first_value["ref"],
        evidence_fingerprint=first_row["content_hash"],
        capture_origin=capture,
        operation_origin=operation,
    )
    legacy_payload = {
        "operational_witness": operational_witness,
        "authoritative_relations": [relation],
    }
    legacy_payload["projection_fingerprint"] = canonical_fingerprint(
        "evidence-vault-sv9-authoritative-relation-projection-v1",
        {
            "source_scan_id": "historical-scan",
            "capture_origin": capture,
            "operation_origin": operation,
            **legacy_payload,
        },
    )
    witness = build_evidence_vault_sv9_authoritative_relation_witness(
        source_scan_id="historical-scan",
        projection={"status": "available", "reason_codes": [], **legacy_payload},
        capture_origin=capture,
        operation_origin=operation,
    )
    candidate = {
        "schema_version": "evidence-vault-sv9-judgment-candidate-v2",
        "source_scan_id": "historical-scan",
        "authoritative_relation_witness": witness,
        "candidate_tile_judgments": [
            {
                "tile_id": "M1",
                "component_key": "mission",
                "assessment_state": "no",
                "supporting_evidence": [
                    {
                        "evidence_ref": first_value["ref"],
                        "evidence_fingerprint": first_row["content_hash"],
                    }
                ],
            }
        ],
    }
    context = {
        "workspace_id": "workspace",
        "brand_id": "brand",
        "scan_run_id": "run",
        "source_scan_id": "current-scan",
        "canonical_domain": "example.com",
        "capture_id": capture["capture_id"],
        "capture_fingerprint": capture["capture_fingerprint"],
        "operation_plan_id": operation["operation_id"],
        "operation_fingerprint": operation["operation_fingerprint"],
    }
    candidate_context = dict(context, source_scan_id="historical-scan")
    return packet_row, [first_row, values[1][1]], candidate, context, candidate_context, operational_witness


def test_repository_facts_uses_only_candidate_witness_basis_and_preserves_original_fields(monkeypatch):
    packet_row, evidence_rows, candidate, context, candidate_context, _ = _fixture()
    connection = _Connection(packet_row, evidence_rows)
    monkeypatch.setattr(history, "_vault_operation_row", lambda *args, **kwargs: {"row": True})
    monkeypatch.setattr(history, "_vault_operation_plan_record", lambda row: {"status": "not_required", "result_payload": None})
    monkeypatch.setattr(history, "_sv9_judgment_context", lambda *args, **kwargs: candidate_context)
    monkeypatch.setattr(history, "_replay_sv9_judgment_authority", lambda *args, **kwargs: {"candidate": candidate})
    facts = history._sv9_judgment_authoritative_relation_facts(connection, context, "b3s")
    assert [row["tile_id"] for row in facts["authority"]["accepted"]] == ["M1"]
    basis = facts["authority"]["accepted"][0]["basis"][0]
    packet_basis = packet_row["packet_payload"]["accepted_memory"]["accepted_tiles"][0]["basis"][0]
    assert basis == packet_basis
    assert basis["polarity"] == "contradicts"


def test_repository_facts_rejects_packet_fingerprint_and_basis_tampering(monkeypatch):
    packet_row, evidence_rows, candidate, context, candidate_context, _ = _fixture()
    monkeypatch.setattr(history, "_vault_operation_row", lambda *args, **kwargs: {"row": True})
    monkeypatch.setattr(history, "_vault_operation_plan_record", lambda row: {"status": "not_required", "result_payload": None})
    monkeypatch.setattr(history, "_sv9_judgment_context", lambda *args, **kwargs: candidate_context)
    monkeypatch.setattr(history, "_replay_sv9_judgment_authority", lambda *args, **kwargs: {"candidate": candidate})
    packet_row["packet_fingerprint"] = _sha("tampered")
    with pytest.raises(history.EvidenceVaultSv9JudgmentCandidateError):
        history._sv9_judgment_authoritative_relation_facts(_Connection(packet_row, evidence_rows), context, "b3s")


def test_repository_facts_does_not_reuse_operational_only_basis_without_result(monkeypatch):
    packet_row, evidence_rows, _, context, candidate_context, _ = _fixture()
    connection = _Connection(packet_row, evidence_rows)
    monkeypatch.setattr(history, "_vault_operation_row", lambda *args, **kwargs: {"row": True})
    monkeypatch.setattr(history, "_vault_operation_plan_record", lambda row: {"status": "not_required", "result_payload": None})
    monkeypatch.setattr(history, "_replay_sv9_judgment_authority", lambda *args, **kwargs: None)
    monkeypatch.setattr(history, "_project_vault_operational_memory_authority_chain", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("operational basis was consulted")))
    facts = history._sv9_judgment_authoritative_relation_facts(connection, context, "b3s")
    assert facts["authority"] is None
    assert not any("canonical_memory_packets" in query for query, _ in connection.calls)
