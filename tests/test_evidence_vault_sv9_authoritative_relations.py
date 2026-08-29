from copy import deepcopy
import hashlib
import inspect
from uuid import NAMESPACE_URL, uuid5

import pytest

from src.services.evidence_vault_canonical_core import build_tile_contract_registry
from src.services.evidence_vault_sv9_authoritative_relations import (
    project_evidence_vault_sv9_authoritative_relations,
)
from src.services.evidence_vault_sv9_judgment_delta import (
    EvidenceVaultSV9JudgmentDeltaError,
    build_authoritative_evidence_tile_relation,
)

# fmt: off

def _sha(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _id(value):
    return str(uuid5(NAMESPACE_URL, str(value)))


class _Repository:
    def __init__(self, facts): self.facts, self.calls = facts, []
    def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, *, workspace_slug="b3s"):
        self.calls.append((scan, workspace_slug)); return deepcopy(self.facts)


def _facts(*, polarity="supports", count=1):
    workspace, brand, scan, capture, operation = (_id(name) for name in ("workspace", "brand", "scan", "capture", "operation"))
    source = {"workspace_id": workspace, "brand_id": brand, "scan_run_id": _id("run"), "source_scan_id": "scan-1", "workspace_slug": "b3s", "canonical_domain": "brand.test", "capture_id": capture, "capture_fingerprint": _sha("capture"), "operation_plan_id": operation, "operation_fingerprint": _sha("operation"), "operation_status": "completed"}
    evidence_id, document_id = _sha("evidence"), _sha("document")
    evidence = [{"workspace_id": workspace, "brand_id": brand, "source_scan_id": "scan-1", "canonical_domain": "brand.test", "capture_id": capture, "evidence_record_id": _id("record"), "evidence_ref": "evidence-1", "evidence_fingerprint": _sha("content"), "evidence_id": evidence_id, "source_identity_id": document_id}]
    registry = build_tile_contract_registry()["tiles"][:count]
    accepted = [{"tile_id": tile["tile_id"], "component_key": tile["component_key"], "authority_state": "accepted", "review_state": "resolved", "lifecycle_state": "active", "basis": [{"relation_id": _sha(f"relation-{tile['tile_id']}"), "evidence_id": evidence_id, "source_identity_id": document_id, "polarity": polarity}]} for tile in registry]
    witness = {"canonical_memory_version": _sha("memory"), "adoption_event_id": _id("event"), "adoption_sequence": 1, "candidate_packet_fingerprint": _sha("packet"), "request_fingerprint": _sha("request")}
    return {"source": source, "evidence": evidence, "authority": {"witness": witness, "accepted": accepted}}


def _project(facts):
    repo = _Repository(facts)
    return project_evidence_vault_sv9_authoritative_relations(repository=repo, source_scan_id="scan-1"), repo


def test_projects_exact_one_to_many_without_advisory_inputs_or_cartesian_expansion():
    result, repo = _project(_facts(count=2))
    assert repo.calls == [("scan-1", "b3s")]
    assert result["status"] == "available"
    assert len(result["authoritative_relations"]) == 2
    assert {row["disposition"] for row in result["authoritative_relations"]} == {"relevant"}
    assert tuple(inspect.signature(project_evidence_vault_sv9_authoritative_relations).parameters) == ("repository", "source_scan_id", "workspace_slug")


@pytest.mark.parametrize("polarity", ["supports", "contradicts", "demonstrates_absence"])
def test_all_accepted_operational_polarities_project_as_relevant(polarity):
    result, _ = _project(_facts(polarity=polarity))
    assert result["status"] == "available"
    assert result["authoritative_relations"][0]["disposition"] == "relevant"


@pytest.mark.parametrize("mutate", [
    lambda f: f.__setitem__("authority", None),
    lambda f: f["authority"]["accepted"][0].__setitem__("authority_state", "pending"),
    lambda f: f["authority"]["accepted"][0].__setitem__("tile_id", "unknown"),
    lambda f: f["authority"]["accepted"][0].__setitem__("component_key", "wrong"),
    lambda f: f["authority"]["witness"].pop("adoption_event_id"),
    lambda f: f["evidence"].append(deepcopy(f["evidence"][0])),
    lambda f: f["evidence"][0].__setitem__("source_identity_id", _sha("other")),
    lambda f: f["source"].__setitem__("source_scan_id", "other-scan"),
    lambda f: f["source"].__setitem__("workspace_slug", "other"),
    lambda f: f["evidence"][0].__setitem__("source_scan_id", "other-scan"),
    lambda f: f["evidence"][0].__setitem__("capture_id", _id("other-capture")),
    lambda f: f["evidence"][0].__setitem__("canonical_domain", "other.test"),
    lambda f: f["source"].__setitem__("operation_fingerprint", "not-a-fingerprint"),
])
def test_unverifiable_or_nonactive_facts_fail_closed(mutate):
    facts = _facts(); mutate(facts)
    result, _ = _project(facts)
    assert result["status"] == "review_required"
    assert result["authoritative_relations"] == []


def test_unmapped_current_evidence_and_invalidating_basis_require_review():
    facts = _facts(); extra = deepcopy(facts["evidence"][0]); extra["evidence_record_id"] = _id("extra"); extra["evidence_ref"] = "evidence-2"; extra["evidence_id"] = _sha("extra"); facts["evidence"].append(extra)
    assert _project(facts)[0]["reason_codes"] == ["unmatched_current_evidence"]
    facts = _facts(); facts["authority"]["accepted"][0]["basis"][0]["polarity"] = "invalidates_candidate"
    assert _project(facts)[0]["status"] == "review_required"


def test_projection_is_deterministic_head_bound_and_signed_relations_replay():
    facts = _facts(count=2); first, _ = _project(facts); second, _ = _project(facts)
    assert first == second
    relation = first["authoritative_relations"][0]
    assert build_authoritative_evidence_tile_relation(_raw=relation, _signed=True) == relation
    tampered = dict(relation); tampered["disposition"] = "contradiction"
    with pytest.raises(EvidenceVaultSV9JudgmentDeltaError): build_authoritative_evidence_tile_relation(_raw=tampered, _signed=True)
    facts["authority"]["witness"]["adoption_event_id"] = _id("new-event")
    changed, _ = _project(facts)
    assert changed["projection_fingerprint"] != first["projection_fingerprint"]
# fmt: on
