from copy import deepcopy
import hashlib
import inspect
from uuid import NAMESPACE_URL, uuid5

import pytest

from src.history import repository as history
from src.services.evidence_vault_canonical_core import build_tile_contract_registry
from src.services.evidence_vault_sv9_authoritative_relations import (
    EvidenceVaultSv9AuthoritativeRelationWitnessError,
    build_evidence_vault_sv9_authoritative_relation_witness,
    project_evidence_vault_sv9_capture_current,
    project_evidence_vault_sv9_authoritative_relations,
    validate_evidence_vault_sv9_authoritative_relation_witness,
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
    accepted = [{"tile_id": tile["tile_id"], "component_key": tile["component_key"], "assessment_state": "ok", "authority_state": "accepted", "review_state": "resolved", "lifecycle_state": "active", "basis": [{"relation_id": _sha(f"relation-{tile['tile_id']}"), "evidence_id": evidence_id, "source_identity_id": document_id, "polarity": polarity}]} for tile in registry]
    witness = {"canonical_memory_version": _sha("memory"), "adoption_event_id": _id("event"), "adoption_sequence": 1, "candidate_packet_fingerprint": _sha("packet"), "request_fingerprint": _sha("request")}
    return {"source": source, "evidence": evidence, "authority": {"witness": witness, "accepted": accepted}}


def _project(facts):
    repo = _Repository(facts)
    return project_evidence_vault_sv9_authoritative_relations(repository=repo, source_scan_id="scan-1"), repo


class _Rows:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows


class _Connection:
    def __init__(self, rows): self.rows, self.calls = rows, []
    def execute(self, *args): self.calls.append(args); return _Rows(self.rows)


def _repository_projection_inputs(assessment_state):
    source_fingerprint = _sha("source-packet")
    source_tile = {"tile_id": "M1", "candidate_state": assessment_state, "basis": [], "coverage_refs": [], "unresolved_refs": [], "delta_kind": "baseline"}
    source = {"packet": {"manifest": {"brand_identity": "brand.test"}, "candidate_tiles": [source_tile]}, "reference_resolution": {}}
    accepted = {"tile_id": "M1", "component_key": "mission", "semantic_state": assessment_state, "basis": [], "coverage_refs": [], "unresolved_refs": [], "source_delta_kind": "baseline", "source_candidate_packet_fingerprint": source_fingerprint, "authority_profile_id": "scanner-semantic-v1", "authority_source": "policy", "decision_event_id": None, "authority_matrix_fingerprint": _sha("authority-matrix"), "authority_decision_fingerprint": _sha("authority-decision")}
    memory = {"brand_identity": "brand.test", "lifecycle_state": "active", "authority": True, "content": {"accepted_tiles": [accepted]}}
    context = {"brand_id": _id("repository-brand"), "canonical_domain": "brand.test"}
    connection = _Connection([{"packet_kind": "operational_source_v2"}])
    return connection, context, memory, source


def _patch_repository_projection_seams(monkeypatch, source):
    monkeypatch.setattr(history, "_vault_operational_source_packet_record", lambda _row: source)
    monkeypatch.setattr(history, "_sv9_authoritative_relation_source_operation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(history, "evaluate_scanner_semantic_authority", lambda **_kwargs: {"authority_profile_id": "scanner-semantic-v1", "authority_matrix_fingerprint": _sha("authority-matrix"), "authority_decision_fingerprint": _sha("authority-decision")})
    monkeypatch.setattr(history, "validate_authority_decision", lambda *_args, **_kwargs: None)


def test_repository_projection_carries_empty_sin_evidencia_state_without_basis(monkeypatch):
    connection, context, memory, source = _repository_projection_inputs("sin_evidencia")
    _patch_repository_projection_seams(monkeypatch, source)

    projected = history._sv9_authoritative_relation_accepted(connection, context, memory)

    assert len(projected) == 1
    assert projected[0]["assessment_state"] == "sin_evidencia"
    assert projected[0]["basis"] == []


@pytest.mark.parametrize("assessment_state", ["ok", "no"])
def test_repository_projection_rejects_empty_basis_for_evidence_state(monkeypatch, assessment_state):
    connection, context, memory, source = _repository_projection_inputs(assessment_state)
    _patch_repository_projection_seams(monkeypatch, source)

    with pytest.raises(history.EvidenceVaultOperationalAuthorityError, match="projectable"):
        history._sv9_authoritative_relation_accepted(connection, context, memory)


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


def test_sin_evidencia_with_empty_basis_does_not_poison_projection():
    facts = _facts(count=2)
    facts["authority"]["accepted"][0]["assessment_state"] = "ok"
    facts["authority"]["accepted"][1]["assessment_state"] = "sin_evidencia"
    facts["authority"]["accepted"][1]["basis"] = []

    result, _ = _project(facts)

    assert result["status"] == "available"
    assert len(result["authoritative_relations"]) == 1
    assert result["authoritative_relations"][0]["tile_id"] == facts["authority"]["accepted"][0]["tile_id"]


@pytest.mark.parametrize("assessment_state", ["ok", "no"])
def test_empty_basis_for_evidence_bearing_assessment_state_fails_closed(assessment_state):
    facts = _facts()
    facts["authority"]["accepted"][0]["assessment_state"] = assessment_state
    facts["authority"]["accepted"][0]["basis"] = []

    result, _ = _project(facts)

    assert result["status"] == "review_required"
    assert result["authoritative_relations"] == []


def test_nonempty_basis_for_sin_evidencia_fails_closed():
    facts = _facts()
    facts["authority"]["accepted"][0]["assessment_state"] = "sin_evidencia"

    result, _ = _project(facts)

    assert result["status"] == "review_required"
    assert result["authoritative_relations"] == []


def test_unknown_assessment_state_fails_closed():
    facts = _facts()
    facts["authority"]["accepted"][0]["assessment_state"] = "unknown"

    result, _ = _project(facts)

    assert result["status"] == "review_required"
    assert result["authoritative_relations"] == []


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
    lambda f: f["source"].__setitem__("workspace_id", ""),
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
    assert _project(facts)[0]["status"] == "available"
    facts = _facts(); facts["authority"]["accepted"][0]["basis"][0]["polarity"] = "invalidates_candidate"
    assert _project(facts)[0]["status"] == "review_required"


def test_relation_projection_partitions_capture_rows_from_authoritative_basis():
    facts = _facts(); identity_less = deepcopy(facts["evidence"][0]); identity_less.update(evidence_record_id=_id("identity-less"), evidence_ref="evidence-identity-less", evidence_fingerprint=_sha("identity-less"), evidence_id=None, source_identity_id=None); unreferenced = deepcopy(facts["evidence"][0]); unreferenced.update(evidence_record_id=_id("unreferenced"), evidence_ref="evidence-unreferenced", evidence_fingerprint=_sha("unreferenced"), evidence_id=_sha("unreferenced-evidence"), source_identity_id=_sha("unreferenced-source")); facts["evidence"].extend((identity_less, unreferenced))
    result, _ = _project(facts)
    assert result["status"] == "available" and [(row["evidence_ref"], row["evidence_fingerprint"]) for row in result["authoritative_relations"]] == [("evidence-1", _sha("content"))]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda facts: facts["authority"]["accepted"][0]["basis"][0].update(
            evidence_id=_sha("missing-evidence"),
            source_identity_id=_sha("missing-source"),
        ),
        lambda facts: facts["evidence"].append(deepcopy(facts["evidence"][0])),
    ],
)
def test_referenced_basis_identity_must_resolve_exactly_once(mutate):
    facts = _facts(); mutate(facts); result, _ = _project(facts)
    assert result["status"] == "review_required" and result["authoritative_relations"] == []


def test_one_sided_canonical_identity_fails_closed_but_absent_identity_is_allowed():
    facts = _facts(); facts["evidence"][0]["source_identity_id"] = None; result, _ = _project(facts); assert result["status"] == "review_required"
    facts = _facts(); facts["evidence"][0].update(evidence_id=None, source_identity_id=None); facts["authority"]["accepted"][0].update(assessment_state="sin_evidencia", basis=[]); result, _ = _project(facts); assert result["status"] == "available" and result["authoritative_relations"] == []; facts["evidence"] = []; result, _ = _project(facts); current = project_evidence_vault_sv9_capture_current(repository=_Repository(facts), source_scan_id="scan-1"); assert result["status"] == "available" and result["authoritative_relations"] == [] and current["status"] == "available" and current["current_evidence"] == []


def test_full_capture_current_projection_includes_identityless_rows_and_rejects_duplicate_pairs():
    facts = _facts(); identity_less = deepcopy(facts["evidence"][0]); identity_less.update(evidence_record_id=_id("identity-less"), evidence_ref="evidence-identity-less", evidence_fingerprint=_sha("identity-less"), evidence_id=None, source_identity_id=None); facts["evidence"].append(identity_less); result, _ = _project(facts)
    current = project_evidence_vault_sv9_capture_current(repository=_Repository(facts), source_scan_id="scan-1")
    assert current["status"] == "available" and current["current_evidence"] == [{"evidence_ref": "evidence-1", "evidence_fingerprint": _sha("content")}, {"evidence_ref": "evidence-identity-less", "evidence_fingerprint": _sha("identity-less")} ] and result["status"] == "available"
    duplicate = deepcopy(facts); duplicate["evidence"].append(deepcopy(duplicate["evidence"][0])); current = project_evidence_vault_sv9_capture_current(repository=_Repository(duplicate), source_scan_id="scan-1")
    assert current["status"] == "review_required" and current["current_evidence"] == []
    drifted = deepcopy(facts); drifted["source"]["source_scan_id"] = "other-scan"; current = project_evidence_vault_sv9_capture_current(repository=_Repository(drifted), source_scan_id="scan-1")
    assert current["status"] == "review_required" and current["reason_codes"] == ["source_identity_mismatch"]


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


def test_witness_has_exact_v1_shape_and_rejects_relation_or_origin_tampering():
    projection, _ = _project(_facts(count=2))
    witness = build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id="scan-1", projection=projection)
    assert validate_evidence_vault_sv9_authoritative_relation_witness(witness) == witness
    assert set(witness) == {"schema_version", "source_scan_id", "operational_witness", "authoritative_relations", "projection_fingerprint", "witness_fingerprint"}
    for path in (("authoritative_relations", 0, "evidence_ref"), ("operational_witness", "canonical_memory_version")):
        tampered = deepcopy(witness); target = tampered
        for key in path[:-1]: target = target[key]
        target[path[-1]] = "other"
        with pytest.raises(EvidenceVaultSv9AuthoritativeRelationWitnessError): validate_evidence_vault_sv9_authoritative_relation_witness(tampered)
# fmt: on
