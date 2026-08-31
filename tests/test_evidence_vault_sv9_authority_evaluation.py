from copy import deepcopy
import pytest

from src.services import evidence_vault_sv9_authority_evaluation as service
from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_sv9_authoritative_relations import (
    EvidenceVaultSv9AuthoritativeRelationStaleWitnessError,
    EvidenceVaultSv9AuthoritativeRelationWitnessError,
)
from src.services import evidence_vault_sv9_judgment_delta as delta
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory
from tests.test_sv9_judgment_memory import _hash, _judgment, _origin, _series


# fmt: off
_ID = "00000000-0000-0000-0000-000000000201"
def _identity(number): return {"evidence_ref": f"evidence:{number}", "evidence_fingerprint": _hash(number)}
def _pending(row): return memory.build_tile_judgment(**({key: value for key, value in row.items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint", "authority_state"}} | {"authority_state": "pending"}))
def _sentinel(series):
    row = _judgment(tile_id="M1", component_key="mission", series=series)
    value = {"component_key": "mission", "status": "not_detected", "supporting_evidence": [], "capture_origin": row["capture_origin"], "operation_origin": row["operation_origin"], "series_contract": series, "authority_state": "pending", "review_state": "none", "lifecycle_state": "active", "lifecycle_reason": ""}
    return planner.build_component_not_detected_sentinel(**value)
def _authority(series=None, sentinel=False):
    series = _series() if series is None else series; rows = [_pending(_judgment(tile_id=tile, component_key=component, series=series)) for tile, component in planner._REGISTRY]
    sentinels = []
    if sentinel: rows = [row for row in rows if row["component_key"] != "mission"]; sentinels = [_sentinel(series)]
    partition = {"candidate_tile_judgments": rows, "candidate_component_sentinels": sentinels}
    return {"authority": True, "accepted_candidate": {"id": _ID, **partition}, "accepted_partition": partition, "active_authority_event": {"event_id": "00000000-0000-0000-0000-000000000202"}, "current_head": {"event_fingerprint": _hash(202)}, "reopen_review_overlay": None}

class _Repository:
    def __init__(self, authority=None, records=(3,), bad_reload=False):
        self.authority, self.records, self.bad_reload = authority, tuple(records), bad_reload; self.append_calls = self.get_calls = 0; self.candidates = {}; self.mutations = []
        self.context = {"capture_origin": _origin("capture", 9), "operation_origin": _origin("operation", 10)}; self.projection_relations = None; self.projection_status = "available"; self.projection_calls = 0; self.witness_seed = 300
    def get_evidence_vault_sv9_judgment_authority(self, _domain, **_kwargs): return deepcopy(self.authority)
    def load_evidence_vault_sv9_judgment_context(self, _scan, **_kwargs): return {"canonical_domain": "example.test", "capture_id": self.context["capture_origin"]["capture_id"], "capture_fingerprint": self.context["capture_origin"]["capture_fingerprint"], "operation_plan_id": self.context["operation_origin"]["operation_id"], "operation_fingerprint": self.context["operation_origin"]["operation_fingerprint"]}
    def resolve_evidence_vault_sv9_judgment_evidence(self, _scan, refs, **_kwargs):
        assert refs == sorted(refs); rows = [{"evidence_record_id": f"00000000-0000-0000-0000-{number:012d}", **_identity(number), "content": {"evidence": number}} for number in self.records]
        assert {row["evidence_ref"] for row in rows} == set(refs); return {**self.context, "evidence": rows}
    def get_evidence_vault_sv9_judgment_candidate(self, _scan, *, canonical_plan_fingerprint, **_kwargs):
        self.get_calls += 1; row = deepcopy(self.candidates.get(canonical_plan_fingerprint))
        if row and self.bad_reload and self.get_calls > 1: row["complete_record_fingerprint"] = _hash(999)
        return row
    def append_evidence_vault_sv9_judgment_candidate(self, _scan, candidate, **_kwargs):
        self.append_calls += 1; key = candidate["canonical_plan_fingerprint"]
        if key in self.candidates: return deepcopy(self.candidates[key]), False
        self.candidates[key] = deepcopy(candidate) | {"id": "00000000-0000-0000-0000-000000000203"}; return deepcopy(self.candidates[key]), True
    def adopt_evidence_vault_sv9_judgment_candidate(self, *_args, **_kwargs): self.mutations.append("adopt"); raise AssertionError
    def reopen_evidence_vault_sv9_judgment_authority(self, *_args, **_kwargs): self.mutations.append("reopen"); raise AssertionError
    def project(self, scan, workspace):
        self.projection_calls += 1
        if self.projection_status != "available": return {"status": "review_required", "reason_codes": [self.projection_status], "authoritative_relations": [], "operational_witness": None, "projection_fingerprint": None}
        rows = deepcopy(self.projection_relations if self.projection_relations is not None else [_relation(self, "M1", number=number) for number in self.records])
        seed = self.witness_seed; operational = {"canonical_memory_version": _hash(seed), "adoption_event_id": f"00000000-0000-0000-0000-{seed:012d}", "adoption_sequence": 1, "candidate_packet_fingerprint": _hash(seed + 1), "request_fingerprint": _hash(seed + 2)}
        fingerprint = canonical_fingerprint("evidence-vault-sv9-authoritative-relation-projection-v1", {"source_scan_id": scan, "capture_origin": self.context["capture_origin"], "operation_origin": self.context["operation_origin"], "operational_witness": operational, "authoritative_relations": rows})
        return {"status": "available", "reason_codes": [], "authoritative_relations": rows, "operational_witness": operational, "projection_fingerprint": fingerprint}

class _Flow:
    def __init__(self, fail=None, sentinel=False, malformed=False): self.fail, self.sentinel, self.malformed, self.calls = fail, sentinel, malformed, []
    def evaluate_component(self, request):
        self.calls.append(request)
        if self.fail == len(self.calls): return evaluation.ComponentEvaluationOutcome.provider_failure()
        if self.malformed: return evaluation.ComponentEvaluationOutcome.success({})
        if self.sentinel and request["component_key"] == "mission": rows, status = [], "not_detected"
        else:
            rows = [{"tile_id": row["tile_id"], "assessment_state": "ok" if row["evidence"] else "sin_evidencia", "supporting_evidence": [{key: item[key] for key in ("evidence_ref", "evidence_fingerprint")} for item in row["evidence"]]} for row in request["requested_tiles"]]; status = "evaluated"
        return evaluation.ComponentEvaluationOutcome.success(evaluation.build_component_evaluation(component_key=request["component_key"], series_fingerprint=request["current_series_fingerprint"], request_fingerprint=request["canonical_request_fingerprint"], status=status, tile_results=rows))

def _relation(repo, tile, disposition="relevant", number=9): return delta.build_authoritative_evidence_tile_relation(tile_id=tile, component_key=dict(planner._REGISTRY)[tile], disposition=disposition, **_identity(number), **repo.context)
@pytest.fixture(autouse=True)
def _authoritative_projection(monkeypatch):
    monkeypatch.setattr(service, "project_evidence_vault_sv9_authoritative_relations", lambda *, repository, source_scan_id, workspace_slug: repository.project(source_scan_id, workspace_slug))

def _run(repo, flow, current=(3,), relations=None, series=None, trusted=(), projection_relations=None):
    relations = [_relation(repo, "M1", number=number) for number in current] if relations is None else list(relations); repo.projection_relations = relations if projection_relations is None else list(projection_relations)
    result = service.run_evidence_vault_sv9_authority_evaluation(repository=repo, flow=flow, domain_or_url="example.test", source_scan_id="scan", current_evidence=[_identity(number) for number in current], authoritative_relations=relations, current_series_contract=_series() if series is None else series, trusted_irrelevant_evidence=[_identity(number) for number in trusted])
    assert not repo.mutations; return result

def test_first_run_with_exact_operational_projection_persists_witnessed_candidate():
    repo, flow = _Repository(records=(9,)), _Flow(); result = _run(repo, flow, current=(9,))
    assert result["status"] == "candidate_available" and len(flow.calls) == 10 and repo.append_calls == 1 and repo.get_calls == 2
    candidate = next(iter(repo.candidates.values())); assert candidate["evidence_bindings"][0]["evidence_ref"] == "evidence:9" and result["candidate"]["id"] == "00000000-0000-0000-0000-000000000203"
    assert candidate["schema_version"] == "evidence-vault-sv9-judgment-candidate-v2" and result["candidate"]["authoritative_relation_witness_fingerprint"] == candidate["authoritative_relation_witness"]["witness_fingerprint"]
    repeated = _run(repo, _Flow(), current=(9,)); assert repeated["status"] == "candidate_available" and not repeated["calls_issued"] and repo.append_calls == 1

@pytest.mark.parametrize(("current", "trusted", "reason"), [((3,), (), "exact_reuse")])
def test_exact_reuse_and_explicit_irrelevant_evidence_skip_flow(current, trusted, reason):
    repo, flow = _Repository(_authority(), current), _Flow(); result = _run(repo, flow, current=current, trusted=trusted)
    assert (result["status"], result["reason_codes"], flow.calls, repo.append_calls) == ("no_new_score", [reason], [], 0)
    assert result["accepted_authority"]["accepted_candidate_id"] == _ID and result["trusted_irrelevant_evidence_count"] == len(trusted)

@pytest.mark.parametrize(("tiles", "components"), [(("M1",), ["mission", "coherencia"]), (("M1", "A1"), ["mission", "attributes", "coherencia"])])
def test_exact_one_or_many_component_worksets_reuse_all_other_authority(tiles, components):
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); result = _run(repo, flow, current=(3, 9), relations=[_relation(repo, "M1", number=3), *[_relation(repo, tile) for tile in tiles]])
    candidate = next(iter(repo.candidates.values())); assert result["status"] == "candidate_available" and [row["component_key"] for row in flow.calls] == components
    assert {row["tile_id"] for row in candidate["evidence_bindings"]} == set(tiles) and all(row["tile_id"] not in planner._COMPONENT_TILES["coherencia"] for row in candidate["evidence_bindings"])

@pytest.mark.parametrize("disposition", ["contradiction", "human_review_required"])
def test_advisory_relation_substitution_requires_review_without_flow_or_write(disposition):
    repo, flow = _Repository(_authority(), (3, 9)), _Flow()
    result = _run(repo, flow, current=(3, 9), relations=[_relation(repo, "M1", disposition), _relation(repo, "A1")], projection_relations=[_relation(repo, "M1", number=3), _relation(repo, "M1")])
    assert result["status"] == "review_required" and result["reason_codes"] == ["authoritative_relation_mismatch"] and not flow.calls and repo.append_calls == 0


def test_projection_review_and_stale_witness_freeze_candidate_io():
    repo, flow = _Repository(records=(9,)), _Flow(); repo.projection_status = "unmatched_current_evidence"
    blocked = _run(repo, flow, current=(9,)); assert blocked["reason_codes"] == ["unmatched_current_evidence"] and not flow.calls and repo.append_calls == 0
    repo, flow = _Repository(records=(9,)), _Flow(); assert _run(repo, flow, current=(9,))["status"] == "candidate_available"
    repo.witness_seed = 400; stale = _run(repo, _Flow(), current=(9,))
    assert stale["status"] == "review_required" and stale["reason_codes"] == ["stale_authoritative_relation_witness"] and stale["calls_issued"] == 0 and repo.append_calls == 1
    repo.witness_seed = 300; stored = next(iter(repo.candidates.values())); stored["authoritative_relation_witness"]["witness_fingerprint"] = _hash(999)
    invalid = _run(repo, _Flow(), current=(9,)); assert invalid["status"] == "review_required" and invalid["reason_codes"] == ["invalid_authoritative_relation_witness"] and invalid["calls_issued"] == 0 and repo.append_calls == 1


@pytest.mark.parametrize(("error", "reason"), [
    (EvidenceVaultSv9AuthoritativeRelationWitnessError("invalid"), "invalid_authoritative_relation_witness"),
    (EvidenceVaultSv9AuthoritativeRelationStaleWitnessError("stale"), "stale_authoritative_relation_witness"),
])
def test_typed_append_witness_errors_require_review(error, reason):
    repo, flow = _Repository(records=(9,)), _Flow()
    repo.append_evidence_vault_sv9_judgment_candidate = lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    outcome = _run(repo, flow, current=(9,))
    assert outcome["status"] == "review_required" and outcome["reason_codes"] == [reason] and flow.calls and not repo.candidates


def test_legacy_v1_candidate_replays_but_requires_review():
    repo, flow = _Repository(records=(9,)), _Flow(); assert _run(repo, flow, current=(9,))["status"] == "candidate_available"
    stored = next(iter(repo.candidates.values())); stored.pop("authoritative_relation_witness"); stored["schema_version"] = "evidence-vault-sv9-judgment-candidate-v1"; stored["complete_record_fingerprint"] = memory.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v1", {key: value for key, value in stored.items() if key not in {"id", "complete_record_fingerprint"}})
    legacy = _run(repo, _Flow(), current=(9,)); assert legacy["status"] == "review_required" and legacy["reason_codes"] == ["unwitnessed_legacy_candidate"] and legacy["calls_issued"] == 0

def test_coverage_loss_persists_only_pending_review_candidate():
    repo, flow = _Repository(_authority(), (9,)), _Flow(); result = _run(repo, flow, current=(9,))
    assert result["status"] == "review_required" and "coverage_loss" in result["reason_codes"] and flow.calls and repo.append_calls == 1

@pytest.mark.parametrize("kwargs", [{"fail": 2}, {"malformed": True}])
def test_provider_or_malformed_partial_results_are_atomic(kwargs):
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(**kwargs); result = _run(repo, flow, current=(3, 9))
    assert result["status"] == "no_new_score" and result["reason_codes"] == ["provider_failure"] and repo.append_calls == 0

def test_contract_rollover_persists_pending_candidate_but_keeps_authority_unchanged():
    authority = _authority(); before = deepcopy(authority); repo, flow = _Repository(authority, (3,)), _Flow(); result = _run(repo, flow, series=_series(prompt_version="prompt-v2"))
    assert result["status"] == "review_required" and result["reason_codes"] == ["series_rollover"] and len(flow.calls) == 10 and repo.append_calls == 1 and authority == before

def test_sentinel_reuses_then_reopens_and_not_detected_consumes_component_capacity():
    repo, flow = _Repository(_authority(sentinel=True), (3,)), _Flow(); assert _run(repo, flow)["status"] == "candidate_available" and flow.calls
    repo, flow = _Repository(_authority(sentinel=True), (3, 9)), _Flow(sentinel=True); result = _run(repo, flow, current=(3, 9), relations=[_relation(repo, "M1", number=3), _relation(repo, "M1")]); candidate = next(iter(repo.candidates.values()))
    assert result["status"] == "candidate_available" and [row["component_key"] for row in flow.calls] == ["mission", "coherencia"] and len(candidate["candidate_component_sentinels"]) == 1
    assert len(candidate["candidate_tile_judgments"]) + sum(len(planner._COMPONENT_TILES[row["component_key"]]) for row in candidate["candidate_component_sentinels"]) == 80

def test_invalid_trusted_identity_and_replay_mismatches_fail_closed(monkeypatch):
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); invalid = _run(repo, flow, current=(3, 9), trusted=(99,))
    assert invalid["status"] == "no_new_score" and invalid["reason_codes"] == ["invalid_input"] and not flow.calls and repo.append_calls == 0
    repo, flow = _Repository(None, (9,), bad_reload=True), _Flow(); mismatch = _run(repo, flow, current=(9,))
    assert mismatch["status"] == "no_new_score" and mismatch["reason_codes"] == ["invalid_replay"] and repo.append_calls == 1
    repo, flow = _Repository(None, (9,)), _Flow(); monkeypatch.setattr(service.evaluation, "replay_incremental_evaluations", lambda *_: {"status": "pending"})
    preappend = _run(repo, flow, current=(9,)); assert preappend["status"] == "no_new_score" and preappend["reason_codes"] == ["invalid_replay"] and repo.append_calls == 0
# fmt: on
