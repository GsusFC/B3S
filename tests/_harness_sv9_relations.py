from copy import deepcopy
import hashlib
import inspect
from uuid import NAMESPACE_URL, uuid5

import pytest

from src.history import repository as history
from src.services.evidence_vault_canonical_core import build_tile_contract_registry, canonical_fingerprint
from src.services.evidence_vault_sv9_authoritative_relations import (
    EVIDENCE_VAULT_SV9_EVALUATION_INPUT_VERSION,
    EvidenceVaultSv9AuthoritativeRelationWitnessError,
    build_evidence_vault_sv9_authoritative_relation_witness,
    project_evidence_vault_sv9_evaluation_input,
    project_evidence_vault_sv9_capture_current,
    project_evidence_vault_sv9_authoritative_relations,
    validate_evidence_vault_sv9_evaluation_input,
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
    return {"source": source, "evidence": evidence, "authority": {"witness": witness, "accepted": accepted}, "evaluation_hint_seeds": []}


def _project(facts):
    repo = _Repository(facts)
    return project_evidence_vault_sv9_authoritative_relations(repository=repo, source_scan_id="scan-1"), repo


def _distinct_facts():
    facts = _facts(count=2)
    second = deepcopy(facts["evidence"][0]); second.update(evidence_record_id=_id("second"), evidence_ref="evidence-2", evidence_fingerprint=_sha("content-2"), evidence_id=_sha("evidence-2"), source_identity_id=_sha("source-2"))
    facts["evidence"].append(second)
    facts["authority"]["accepted"][1]["basis"][0].update(evidence_id=second["evidence_id"], source_identity_id=second["source_identity_id"])
    return facts


def _hint_seed(*, label="hint", tile_id="M1", evidence_record_id=None, provenance=None, component_key="mission"):
    return {
        "hint_id": _id(label),
        "tile_id": tile_id,
        "component_key": component_key,
        "evidence_record_id": evidence_record_id or _id("record"),
        "provenance_fingerprint": provenance or _sha(f"{label}-provenance"),
    }


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


@pytest.mark.parametrize("mutate, status, reason", [
    (lambda facts: facts["authority"]["accepted"][0]["basis"][0].update(evidence_id=_sha("missing-evidence"), source_identity_id=_sha("missing-source")), "available", None),
    (lambda facts: facts["evidence"].append(dict(facts["evidence"][0], evidence_record_id=_id("duplicate"))), "review_required", "ambiguous_current_evidence"),
])
def test_referenced_basis_identity_distinguishes_missing_from_duplicate(mutate, status, reason):
    facts = _facts(); mutate(facts); result, _ = _project(facts)
    assert result["status"] == status and result["authoritative_relations"] == []
    if reason: assert result["reason_codes"] == [reason]
    else: assert result["reopen_tile_ids"] == ["M1"]


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
    projection, _ = _project(_facts(count=2)); legacy = {key: projection[key] for key in ("status", "reason_codes", "authoritative_relations", "operational_witness", "projection_fingerprint")}
    legacy["projection_fingerprint"] = canonical_fingerprint("evidence-vault-sv9-authoritative-relation-projection-v1", {"source_scan_id": "scan-1", "capture_origin": projection["authoritative_relations"][0]["capture_origin"], "operation_origin": projection["authoritative_relations"][0]["operation_origin"], "operational_witness": projection["operational_witness"], "authoritative_relations": projection["authoritative_relations"]})
    witness = build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id="scan-1", projection=projection); legacy_witness = build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id="scan-1", projection=legacy)
    assert witness == legacy_witness == build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id="scan-1", projection=projection, **{key: projection["authoritative_relations"][0][key] for key in ("capture_origin", "operation_origin")})
    assert validate_evidence_vault_sv9_authoritative_relation_witness(witness) == witness and validate_evidence_vault_sv9_authoritative_relation_witness(legacy_witness) == legacy_witness
    assert set(witness) == {"schema_version", "source_scan_id", "operational_witness", "authoritative_relations", "projection_fingerprint", "witness_fingerprint"}; invalid = deepcopy(projection); invalid["authority_continuity"][0]["continuity_state"] = "changed"; assert pytest.raises(EvidenceVaultSv9AuthoritativeRelationWitnessError, lambda: build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id="scan-1", projection=invalid)); partial = _distinct_facts(); partial["evidence"].pop(); partial_projection, _ = _project(partial); assert pytest.raises(EvidenceVaultSv9AuthoritativeRelationWitnessError, lambda: build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id="scan-1", projection=partial_projection))
    for path in (("authoritative_relations", 0, "evidence_ref"), ("operational_witness", "canonical_memory_version")):
        tampered = deepcopy(witness); target = tampered
        for key in path[:-1]: target = target[key]
        target[path[-1]] = "other"
        with pytest.raises(EvidenceVaultSv9AuthoritativeRelationWitnessError): validate_evidence_vault_sv9_authoritative_relation_witness(tampered)


def test_empty_adopted_witness_preserves_standalone_provenance_and_context_binding():
    facts = _facts(); facts["authority"]["accepted"] = []
    projection, _ = _project(facts)
    source = facts["source"]
    origins = {"capture_origin": {key: source[key] for key in ("capture_id", "capture_fingerprint")}, "operation_origin": {"operation_id": source["operation_plan_id"], "operation_fingerprint": source["operation_fingerprint"]}}
    witness = build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id="scan-1", projection=projection, **origins)
    assert witness["authoritative_relations"] == []
    assert all(witness[key] == value for key, value in origins.items())
    assert validate_evidence_vault_sv9_authoritative_relation_witness(witness) == witness
    candidate = {"schema_version": "evidence-vault-sv9-judgment-candidate-v2", "authoritative_relation_witness": witness}
    history._sv9_judgment_candidate_witness(candidate, source)
    with pytest.raises(EvidenceVaultSv9AuthoritativeRelationWitnessError):
        history._sv9_judgment_candidate_witness(candidate, source | {"capture_id": _id("other")})
    for path, value in ((["capture_origin", "capture_id"], _id("other")), (["operation_origin", "operation_fingerprint"], _sha("other")), (["capture_origin", "extra"], "extra"), (["operational_witness", "adoption_event_id"], _id("other"))):
        tampered = deepcopy(witness); tampered[path[0]][path[1]] = value
        with pytest.raises(EvidenceVaultSv9AuthoritativeRelationWitnessError):
            validate_evidence_vault_sv9_authoritative_relation_witness(tampered)
    with pytest.raises(EvidenceVaultSv9AuthoritativeRelationWitnessError):
        build_evidence_vault_sv9_authoritative_relation_witness(source_scan_id="scan-1", projection=projection)
    facts["authority"] = None
    unavailable, _ = _project(facts)
    assert unavailable["reason_codes"] == ["no_operational_authority"]


def test_evaluation_input_loads_one_snapshot_and_binds_full_partition():
    facts = _facts(count=2); repo = _Repository(facts)
    result = project_evidence_vault_sv9_evaluation_input(repository=repo, source_scan_id="scan-1")

    assert repo.calls == [("scan-1", "b3s")]
    assert result["status"] == "available"
    assert result["schema_version"] == EVIDENCE_VAULT_SV9_EVALUATION_INPUT_VERSION
    assert set(result) == {
        "status", "reason_codes", "schema_version", "source_identity", "current_evidence", "current_identity_bindings",
        "authoritative_relations", "authority_continuity", "authority_coverage_loss", "reopen_tile_ids", "operational_witness", "relation_projection_fingerprint", "projection_version", "evaluation_input_fingerprint",
        "non_authoritative_hints",
    }
    payload = {key: result[key] for key in (
        "source_identity", "current_evidence", "current_identity_bindings", "authoritative_relations", "authority_continuity", "authority_coverage_loss", "reopen_tile_ids", "operational_witness", "relation_projection_fingerprint", "projection_version", "non_authoritative_hints",
    )}
    assert result["evaluation_input_fingerprint"] == canonical_fingerprint(
        EVIDENCE_VAULT_SV9_EVALUATION_INPUT_VERSION, payload
    )
    assert result["current_evidence"] == project_evidence_vault_sv9_capture_current(
        repository=_Repository(facts), source_scan_id="scan-1"
    )["current_evidence"]
    assert result["authoritative_relations"] == project_evidence_vault_sv9_authoritative_relations(
        repository=_Repository(facts), source_scan_id="scan-1"
    )["authoritative_relations"]


def test_evaluation_input_is_deterministic_and_rejects_unavailable_or_invalid_facts():
    facts = _facts(count=2); repo = _Repository(facts)
    first = project_evidence_vault_sv9_evaluation_input(repository=repo, source_scan_id="scan-1")
    second = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    assert first == second

    unavailable = project_evidence_vault_sv9_evaluation_input(repository=_Repository(None), source_scan_id="scan-1")
    assert unavailable["status"] == "review_required" and unavailable["reason_codes"] == ["source_unavailable"]
    assert unavailable["evaluation_input_fingerprint"] is None and unavailable["authoritative_relations"] == []

    invalid = deepcopy(facts); invalid["source"]["operation_status"] = "pending"
    rejected = project_evidence_vault_sv9_evaluation_input(repository=_Repository(invalid), source_scan_id="scan-1")
    assert rejected["status"] == "review_required" and rejected["reason_codes"] == ["operation_not_immutable"]
    assert rejected["source_identity"] is None and rejected["current_evidence"] == []
    assert unavailable["non_authoritative_hints"] == rejected["non_authoritative_hints"] == []


def test_evaluation_input_projects_capture_bound_non_authoritative_hints_without_authority_fingerprint_drift():
    facts = _facts()
    facts["evaluation_hint_seeds"] = [_hint_seed()]
    hinted = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    baseline_facts = _facts()
    baseline = project_evidence_vault_sv9_evaluation_input(repository=_Repository(baseline_facts), source_scan_id="scan-1")

    assert hinted["status"] == baseline["status"] == "available"
    assert hinted["relation_projection_fingerprint"] == baseline["relation_projection_fingerprint"]
    hint = hinted["non_authoritative_hints"][0]
    assert set(hint) == {
        "hint_id", "tile_id", "component_key", "evidence_record_id", "provenance_fingerprint",
        "capture_id", "capture_fingerprint", "authority", "runtime_effect", "hint_fingerprint",
    }
    assert hint["tile_id"] == "M1" and hint["component_key"] == "mission"
    assert hint["evidence_record_id"] == _id("record")
    assert hint["capture_id"] == hinted["source_identity"]["capture_id"]
    assert hint["capture_fingerprint"] == hinted["source_identity"]["capture_fingerprint"]
    assert hint["authority"] is False and hint["runtime_effect"] == "evaluation_routing_only"
    unsigned = {key: value for key, value in hint.items() if key != "hint_fingerprint"}
    assert hint["hint_fingerprint"] == canonical_fingerprint(
        "evidence-vault-sv9-evaluation-hint-fingerprint-v1", unsigned
    )
    assert hinted["evaluation_input_fingerprint"] != baseline["evaluation_input_fingerprint"]


def test_evaluation_hint_seed_reordering_is_semantically_invariant_and_changes_are_signed():
    facts = _distinct_facts()
    first_record, second_record = facts["evidence"][0]["evidence_record_id"], facts["evidence"][1]["evidence_record_id"]
    facts["evaluation_hint_seeds"] = [
        _hint_seed(label="second", tile_id="M2", evidence_record_id=second_record),
        _hint_seed(label="first", tile_id="M1", evidence_record_id=first_record),
    ]
    first = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    reordered = deepcopy(facts)
    reordered["evaluation_hint_seeds"].reverse()
    second = project_evidence_vault_sv9_evaluation_input(repository=_Repository(reordered), source_scan_id="scan-1")
    assert first == second
    changed = deepcopy(facts)
    changed["evaluation_hint_seeds"][0]["provenance_fingerprint"] = _sha("changed-provenance")
    changed["evaluation_hint_seeds"][0]["hint_id"] = _id("changed-hint")
    changed_result = project_evidence_vault_sv9_evaluation_input(repository=_Repository(changed), source_scan_id="scan-1")
    assert changed_result["status"] == "available"
    assert changed_result["evaluation_input_fingerprint"] != first["evaluation_input_fingerprint"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda hint: hint.__setitem__("hint_id", _id("other-hint")),
        lambda hint: hint.__setitem__("tile_id", "unknown"),
        lambda hint: hint.__setitem__("component_key", "wrong"),
        lambda hint: hint.__setitem__("evidence_record_id", _id("outside-record")),
        lambda hint: hint.__setitem__("provenance_fingerprint", _sha("other-provenance")),
        lambda hint: hint.__setitem__("capture_id", _id("other-capture")),
        lambda hint: hint.__setitem__("capture_fingerprint", _sha("other-capture")),
        lambda hint: hint.__setitem__("authority", True),
        lambda hint: hint.__setitem__("runtime_effect", "authority"),
        lambda hint: hint.__setitem__("hint_fingerprint", _sha("tampered")),
    ],
)
def test_evaluation_input_validator_rejects_hint_tampering(mutate):
    facts = _facts()
    facts["evaluation_hint_seeds"] = [_hint_seed()]
    value = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    tampered = deepcopy(value)
    mutate(tampered["non_authoritative_hints"][0])
    with pytest.raises(ValueError):
        validate_evidence_vault_sv9_evaluation_input(tampered, source_scan_id="scan-1")


@pytest.mark.parametrize(
    "seeds",
    [
        [_hint_seed(label="one"), _hint_seed(label="two", provenance=_sha("other"))],
        [_hint_seed(label="one"), _hint_seed(label="two", provenance=_hint_seed(label="one")["provenance_fingerprint"])],
        [_hint_seed(label="one"), _hint_seed(label="two", evidence_record_id=_id("record"), provenance=_sha("other"))],
        [_hint_seed(evidence_record_id=_id("outside-record"))],
        [_hint_seed(tile_id="unknown")],
        None,
        {"hint_id": _id("one")},
    ],
)
def test_evaluation_hint_seeds_fail_closed_for_missing_malformed_or_duplicate_bindings(seeds):
    facts = _facts()
    facts["evaluation_hint_seeds"] = seeds
    result = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    assert result["status"] == "review_required"
    assert result["non_authoritative_hints"] == []
    assert result["evaluation_input_fingerprint"] is None


@pytest.mark.parametrize("field", ["workspace_id", "brand_id", "scan_run_id"])
def test_evaluation_input_rejects_malformed_nonempty_uuid_source_identity(field):
    facts = _facts()
    facts["source"][field] = "not-a-uuid"

    result = project_evidence_vault_sv9_evaluation_input(
        repository=_Repository(facts), source_scan_id="scan-1"
    )

    assert result["status"] == "review_required"
    assert result["reason_codes"] == ["invalid_source_identity"]
    assert result["source_identity"] is None
    assert result["evaluation_input_fingerprint"] is None


@pytest.mark.parametrize("field", ["workspace_id", "brand_id", "scan_run_id"])
def test_evaluation_input_canonicalizes_uuid_source_identity_before_fingerprint(field):
    canonical_facts = _facts()
    canonical = project_evidence_vault_sv9_evaluation_input(
        repository=_Repository(canonical_facts), source_scan_id="scan-1"
    )
    variant_facts = deepcopy(canonical_facts)
    variant_facts["source"][field] = variant_facts["source"][field].upper()
    if field in {"workspace_id", "brand_id"}:
        for row in variant_facts["evidence"]:
            row[field] = variant_facts["source"][field]

    variant = project_evidence_vault_sv9_evaluation_input(
        repository=_Repository(variant_facts), source_scan_id="scan-1"
    )

    assert variant["status"] == "available"
    assert variant["source_identity"][field] == canonical["source_identity"][field]
    assert variant["evaluation_input_fingerprint"] == canonical["evaluation_input_fingerprint"]


@pytest.mark.parametrize("field", ["workspace_id", "brand_id"])
@pytest.mark.parametrize("side", ["source", "evidence"])
def test_evaluation_input_accepts_mixed_uuid_ownership_representations(field, side):
    canonical_facts = _facts(count=2)
    canonical = project_evidence_vault_sv9_evaluation_input(
        repository=_Repository(canonical_facts), source_scan_id="scan-1"
    )
    variant_facts = deepcopy(canonical_facts)
    representation = variant_facts["source"][field].upper()
    if side == "source":
        variant_facts["source"][field] = representation
    else:
        for row in variant_facts["evidence"]:
            row[field] = representation
    repo = _Repository(variant_facts)

    variant = project_evidence_vault_sv9_evaluation_input(
        repository=repo, source_scan_id="scan-1"
    )

    assert repo.calls == [("scan-1", "b3s")]
    assert variant["status"] == "available"
    assert variant["source_identity"] == canonical["source_identity"]
    assert variant["evaluation_input_fingerprint"] == canonical["evaluation_input_fingerprint"]
    assert variant["current_evidence"] == canonical["current_evidence"]
    assert variant["authoritative_relations"] == canonical["authoritative_relations"]


def test_boolean_adoption_sequence_fails_closed_for_evaluation_input():
    facts = _facts(); facts["authority"]["witness"]["adoption_sequence"] = True
    result = project_evidence_vault_sv9_evaluation_input(
        repository=_Repository(facts), source_scan_id="scan-1"
    )
    assert result["status"] == "review_required"
    assert result["reason_codes"] == ["invalid_authoritative_facts"]
    assert result["evaluation_input_fingerprint"] is None


def test_evaluation_input_validator_replays_nested_contract_and_rejects_tampering():
    facts = _facts(count=2)
    value = project_evidence_vault_sv9_evaluation_input(
        repository=_Repository(facts), source_scan_id="scan-1"
    )
    assert validate_evidence_vault_sv9_evaluation_input(
        value, source_scan_id="scan-1"
    ) == value
    for mutate in (
        lambda row: row.__setitem__("schema_version", "unknown"),
        lambda row: row["source_identity"].__setitem__("brand_id", "not-a-uuid"),
        lambda row: row["authoritative_relations"][0].__setitem__(
            "relation_fingerprint", _sha("tampered")
        ),
        lambda row: row.__setitem__("evaluation_input_fingerprint", _sha("tampered")),
    ):
        tampered = deepcopy(value)
        mutate(tampered)
        with pytest.raises(ValueError):
            validate_evidence_vault_sv9_evaluation_input(
                tampered, source_scan_id="scan-1"
            )


def test_v2_continuity_reopens_only_changed_or_missing_tiles():
    facts = _distinct_facts(); facts["evidence"].pop()
    result, _ = _project(facts)
    assert result["status"] == "available" and [row["tile_id"] for row in result["authoritative_relations"]] == ["M1"]
    assert result["reopen_tile_ids"] == ["M2"] and result["authority_coverage_loss"][0]["reason"] == "historical_basis_missing"
    facts = _distinct_facts(); facts["evidence"][0]["evidence_id"] = _sha("changed")
    result, _ = _project(facts)
    assert result["status"] == "available" and [row["tile_id"] for row in result["authoritative_relations"]] == ["M2"]
    assert result["reopen_tile_ids"] == ["M1"] and result["authority_coverage_loss"][0]["reason"] == "historical_basis_changed"
def test_v2_duplicate_current_pair_is_global_unsigned_diagnostic():
    facts = _facts(); facts["evidence"].append(dict(facts["evidence"][0], evidence_record_id=_id("duplicate")))
    result, _ = _project(facts)
    assert result["status"] == "review_required" and result["reason_codes"] == ["ambiguous_current_evidence"]
    assert result["authority_continuity"][0]["continuity_state"] == "ambiguous_duplicate" and result["authoritative_relations"] == []
def test_v2_all_relations_lost_remains_signed_available_and_replays():
    facts = _distinct_facts(); facts["evidence"] = []
    result = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    assert result["status"] == "available" and result["authoritative_relations"] == [] and result["reopen_tile_ids"] == ["M1", "M2"]
    assert validate_evidence_vault_sv9_evaluation_input(result, source_scan_id="scan-1") == result
def test_v2_fingerprint_is_raw_order_invariant_but_continuity_sensitive():
    facts = _distinct_facts(); first = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    facts["evidence"].reverse(); facts["authority"]["accepted"].reverse()
    second = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    assert second == first
    facts = _distinct_facts(); facts["evidence"][0]["evidence_id"] = _sha("changed")
    changed = project_evidence_vault_sv9_evaluation_input(repository=_Repository(facts), source_scan_id="scan-1")
    assert changed["evaluation_input_fingerprint"] != first["evaluation_input_fingerprint"]
# fmt: on
