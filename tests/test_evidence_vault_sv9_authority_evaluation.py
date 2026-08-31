from copy import deepcopy
import pytest

from src.services import evidence_vault_sv9_authority_evaluation as service
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
        self.context = {"capture_origin": _origin("capture", 9), "operation_origin": _origin("operation", 10)}
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
def _run(repo, flow, current=(3,), relations=(), series=None, trusted=()):
    result = service.run_evidence_vault_sv9_authority_evaluation(repository=repo, flow=flow, domain_or_url="example.test", source_scan_id="scan", current_evidence=[_identity(number) for number in current], authoritative_relations=list(relations), current_series_contract=_series() if series is None else series, trusted_irrelevant_evidence=[_identity(number) for number in trusted])
    assert not repo.mutations; return result

def test_first_run_requires_trusted_unmapped_evidence_before_ten_calls_and_persistence():
    repo, flow = _Repository(records=(9,)), _Flow(); blocked = _run(repo, flow, current=(9,))
    assert blocked["status"] == "review_required" and not flow.calls and repo.append_calls == 0
    flow = _Flow(); result = _run(repo, flow, current=(9,), trusted=(9,))
    assert result["status"] == "candidate_available" and len(flow.calls) == 10 and repo.append_calls == 1 and repo.get_calls == 2
    candidate = next(iter(repo.candidates.values())); assert candidate["evidence_bindings"] == [] and result["candidate"]["id"] == "00000000-0000-0000-0000-000000000203"
    repeated = _run(repo, _Flow(), current=(9,), trusted=(9,)); assert repeated["status"] == "candidate_available" and not repeated["calls_issued"] and repo.append_calls == 1

@pytest.mark.parametrize(("current", "trusted", "reason"), [((3,), (), "exact_reuse"), ((3, 9), (9,), "exact_reuse")])
def test_exact_reuse_and_explicit_irrelevant_evidence_skip_flow(current, trusted, reason):
    repo, flow = _Repository(_authority(), current), _Flow(); result = _run(repo, flow, current=current, trusted=trusted)
    assert (result["status"], result["reason_codes"], flow.calls, repo.append_calls) == ("no_new_score", [reason], [], 0)
    assert result["accepted_authority"]["accepted_candidate_id"] == _ID and result["trusted_irrelevant_evidence_count"] == len(trusted)

@pytest.mark.parametrize(("tiles", "components"), [(("M1",), ["mission", "coherencia"]), (("M1", "A1"), ["mission", "attributes", "coherencia"])])
def test_exact_one_or_many_component_worksets_reuse_all_other_authority(tiles, components):
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); result = _run(repo, flow, current=(3, 9), relations=[_relation(repo, tile) for tile in tiles])
    candidate = next(iter(repo.candidates.values())); assert result["status"] == "candidate_available" and [row["component_key"] for row in flow.calls] == components
    assert {row["tile_id"] for row in candidate["evidence_bindings"]} == set(tiles) and all(row["tile_id"] not in planner._COMPONENT_TILES["coherencia"] for row in candidate["evidence_bindings"])

@pytest.mark.parametrize("disposition", ["contradiction", "human_review_required"])
def test_review_dispositions_freeze_authority_but_safe_work_persists_review_candidate(disposition):
    authority = _authority(); before = deepcopy(authority); repo, flow = _Repository(authority, (3, 9)), _Flow()
    result = _run(repo, flow, current=(3, 9), relations=[_relation(repo, "M1", disposition), _relation(repo, "A1")]); candidate = next(iter(repo.candidates.values()))
    assert result["status"] == "review_required" and result["reason_codes"] == ["review_set"] and [row["component_key"] for row in flow.calls] == ["attributes", "coherencia"]
    assert next(row for row in candidate["candidate_tile_judgments"] if row["tile_id"] == "M1")["authority_state"] == "accepted" and authority == before

@pytest.mark.parametrize(("current", "tiles", "reason"), [((9,), (), "coverage_loss"), ((3, 9, 10), ("M1",), "unmapped_evidence")])
def test_coverage_loss_and_untrusted_unmapped_evidence_require_review_without_calls(current, tiles, reason):
    repo, flow = _Repository(_authority(), current), _Flow(); result = _run(repo, flow, current=current, relations=[_relation(repo, tile) for tile in tiles])
    assert result["status"] == "review_required" and reason in result["reason_codes"] and not flow.calls and repo.get_calls == repo.append_calls == 0

@pytest.mark.parametrize("kwargs", [{"fail": 2}, {"malformed": True}])
def test_provider_or_malformed_partial_results_are_atomic(kwargs):
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(**kwargs); result = _run(repo, flow, current=(3, 9), relations=[_relation(repo, "M1")])
    assert result["status"] == "no_new_score" and result["reason_codes"] == ["provider_failure"] and repo.append_calls == 0

def test_contract_rollover_persists_pending_candidate_but_keeps_authority_unchanged():
    authority = _authority(); before = deepcopy(authority); repo, flow = _Repository(authority, (3,)), _Flow(); result = _run(repo, flow, series=_series(prompt_version="prompt-v2"))
    assert result["status"] == "review_required" and result["reason_codes"] == ["series_rollover"] and len(flow.calls) == 10 and repo.append_calls == 1 and authority == before

def test_sentinel_reuses_then_reopens_and_not_detected_consumes_component_capacity():
    repo, flow = _Repository(_authority(sentinel=True), (3,)), _Flow(); assert _run(repo, flow)["status"] == "no_new_score" and not flow.calls
    repo, flow = _Repository(_authority(sentinel=True), (3, 9)), _Flow(sentinel=True); result = _run(repo, flow, current=(3, 9), relations=[_relation(repo, "M1")]); candidate = next(iter(repo.candidates.values()))
    assert result["status"] == "candidate_available" and [row["component_key"] for row in flow.calls] == ["mission", "coherencia"] and len(candidate["candidate_component_sentinels"]) == 1
    assert len(candidate["candidate_tile_judgments"]) + sum(len(planner._COMPONENT_TILES[row["component_key"]]) for row in candidate["candidate_component_sentinels"]) == 80

def test_invalid_trusted_identity_and_replay_mismatches_fail_closed(monkeypatch):
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); invalid = _run(repo, flow, current=(3, 9), relations=[_relation(repo, "M1")], trusted=(9,))
    assert invalid["status"] == "no_new_score" and invalid["reason_codes"] == ["invalid_input"] and not flow.calls and repo.append_calls == 0
    repo, flow = _Repository(None, (9,), bad_reload=True), _Flow(); mismatch = _run(repo, flow, current=(9,), trusted=(9,))
    assert mismatch["status"] == "no_new_score" and mismatch["reason_codes"] == ["invalid_replay"] and repo.append_calls == 1
    repo, flow = _Repository(None, (9,)), _Flow(); monkeypatch.setattr(service.evaluation, "replay_incremental_evaluations", lambda *_: {"status": "pending"})
    preappend = _run(repo, flow, current=(9,), trusted=(9,)); assert preappend["status"] == "no_new_score" and preappend["reason_codes"] == ["invalid_replay"] and repo.append_calls == 0
# fmt: on
