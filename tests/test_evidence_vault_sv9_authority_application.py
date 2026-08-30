# fmt: off
from copy import deepcopy
import json
import pytest

from src.history import repository as history
from src.services import evidence_vault_sv9_authority_application as application
from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_sv9_authority_evaluation as evaluation_service
from src.services import evidence_vault_sv9_authority_projection as authority_projection
from src.services import evidence_vault_sv9_judgment_delta as delta
from tests.test_evidence_vault_sv9_authority_evaluation import _Flow, _Repository, _authority, _hash, _identity, _relation, _series
from tests.test_sv9_judgment_memory import _judgment

def _uuid(number): return f"00000000-0000-0000-0000-{number:012d}"

class _ApplicationRepository(_Repository):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.context = {"capture_origin": {"capture_id": _uuid(9), "capture_fingerprint": _hash(9)}, "operation_origin": {"operation_id": _uuid(10), "operation_fingerprint": _hash(10)}}; self.event, self.candidate, self.authority_calls, self.interleave, self.appear = 500, 400, 0, 0, None; self.adopt_failure = self.corrupt_after_adopt = self.authority_failure = self._corrupt = False; self.witness_seed = 300; self.adopt_error = None; self.authority_responses = []
    def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, *, workspace_slug="b3s"):
        source = {"workspace_id": _uuid(1), "brand_id": _uuid(2), "scan_run_id": _uuid(3), "source_scan_id": scan, "workspace_slug": workspace_slug, "canonical_domain": "example.test", "capture_id": self.context["capture_origin"]["capture_id"], "capture_fingerprint": self.context["capture_origin"]["capture_fingerprint"], "operation_plan_id": self.context["operation_origin"]["operation_id"], "operation_fingerprint": self.context["operation_origin"]["operation_fingerprint"], "operation_status": "completed"}
        evidence, basis = [], []
        for number in self.records:
            evidence_id, source_identity_id = _hash(100 + number), _hash(200 + number)
            evidence.append(source | {"evidence_record_id": _uuid(number), "evidence_ref": f"evidence:{number}", "evidence_fingerprint": _hash(number), "evidence_id": evidence_id, "source_identity_id": source_identity_id})
            basis.append({"relation_id": _hash(300 + number), "evidence_id": evidence_id, "source_identity_id": source_identity_id, "polarity": "supports"})
        accepted = [{"tile_id": "M1", "component_key": "mission", "authority_state": "accepted", "review_state": "resolved", "lifecycle_state": "active", "basis": basis}]
        seed = self.witness_seed; witness = {"canonical_memory_version": _hash(seed), "adoption_event_id": _uuid(seed), "adoption_sequence": 1, "candidate_packet_fingerprint": _hash(seed + 1), "request_fingerprint": _hash(seed + 2)}
        return {"source": source, "evidence": evidence, "authority": {"witness": witness, "accepted": accepted}}
    def get_evidence_vault_sv9_judgment_authority(self, _domain, **_kwargs):
        self.authority_calls += 1
        if self.authority_failure: raise RuntimeError("authority unavailable")
        if self.authority_responses: return deepcopy(self.authority_responses.pop(0))
        if self.interleave == self.authority_calls and self.appear: self.authority = deepcopy(self.appear)
        elif self.authority and self.interleave == self.authority_calls: self.event += 1; self.authority["current_head"]["event_fingerprint"] = _hash(self.event)
        value = deepcopy(self.authority)
        if value and self._corrupt: value["accepted_candidate"]["score_fingerprint"] = _hash(999)
        return value
    def append_evidence_vault_sv9_judgment_candidate(self, scan, candidate, **kwargs):
        stored, inserted = super().append_evidence_vault_sv9_judgment_candidate(scan, candidate, **kwargs)
        if inserted:
            self.candidate += 1; self.candidates[stored["canonical_plan_fingerprint"]].update({"id": _uuid(self.candidate), "source_scan_id": scan, "created_at": f"2026-01-01T00:00:{self.candidate % 60:02d}+00:00"})
            stored = deepcopy(self.candidates[stored["canonical_plan_fingerprint"]])
        return stored, inserted
    def _accept(self, candidate_id, scan, kind, key):
        candidate = next(deepcopy(row) for row in self.candidates.values() if row["id"] == candidate_id)
        partition = {key: candidate[key] for key in ("candidate_tile_judgments", "candidate_component_sentinels")}
        previous, active = (self.authority["current_head"], self.authority["active_authority_event"]) if self.authority else (None, None); self.event += 1
        identity = {name: candidate[name] for name in "id complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint source_scan_id".split()}
        request = authority_event.build_evidence_vault_sv9_authority_request(action="adopt_candidate", candidate_id=candidate_id, expected_predecessor_event_fingerprint=None if previous is None else previous["event_fingerprint"], delta_fingerprint=None, source_scan_id=scan)
        event = authority_event.build_evidence_vault_sv9_authority_event(event_id=_uuid(self.event), event_type=kind, sequence=1 if previous is None else previous["sequence"] + 1, predecessor_event_fingerprint=None if previous is None else previous["event_fingerprint"], active_parent_event_fingerprint=None if active is None else active["event_fingerprint"], candidate_identity=identity, current_series_fingerprint=candidate["current_series_fingerprint"], delta_fingerprint=None, request=request, idempotency_key_hash=key, predecessor_event_id=None if previous is None else previous["event_id"], active_parent_event_id=None if active is None else active["event_id"], created_at=f"2026-01-01T00:00:{self.event % 60:02d}+00:00")
        self.authority = authority_projection.build_evidence_vault_sv9_authority_projection(accepted_candidate=candidate, current_head=event, active_authority_event=event, event=event, reopen_review_overlay=None)
    def adopt_evidence_vault_sv9_judgment_candidate(self, scan, candidate_id, *, expected_predecessor_event_fingerprint, idempotency_key_hash, **_kwargs):
        if self.adopt_error: raise self.adopt_error
        self.mutations.append("adopt")
        if self.authority and expected_predecessor_event_fingerprint != self.authority["current_head"]["event_fingerprint"]: raise RuntimeError("stale")
        if self.adopt_failure: raise RuntimeError("stale")
        self._accept(candidate_id, scan, "supersede" if self.authority else "adopt", idempotency_key_hash); self._corrupt = self.corrupt_after_adopt
        return deepcopy(self.authority), False
    def reopen_evidence_vault_sv9_judgment_authority(self, scan, signed_delta, *, expected_predecessor_event_fingerprint, idempotency_key_hash, **_kwargs):
        self.mutations.append("reopen")
        if expected_predecessor_event_fingerprint != self.authority["current_head"]["event_fingerprint"]: raise RuntimeError("stale")
        previous, active = self.authority["current_head"], self.authority["active_authority_event"]; self.event += 1
        fingerprint = signed_delta["canonical_delta_fingerprint"]
        request = authority_event.build_evidence_vault_sv9_authority_request(action="reopen_authority", candidate_id=None, expected_predecessor_event_fingerprint=previous["event_fingerprint"], delta_fingerprint=fingerprint, source_scan_id=scan)
        event = authority_event.build_evidence_vault_sv9_authority_event(event_id=_uuid(self.event), event_type="reopen", sequence=previous["sequence"] + 1, predecessor_event_fingerprint=previous["event_fingerprint"], active_parent_event_fingerprint=active["event_fingerprint"], candidate_identity=None, current_series_fingerprint=active["current_series_fingerprint"], delta_fingerprint=fingerprint, request=request, idempotency_key_hash=idempotency_key_hash, predecessor_event_id=previous["event_id"], active_parent_event_id=active["event_id"], created_at=f"2026-01-01T00:00:{self.event % 60:02d}+00:00")
        self.authority["current_head"] = self.authority["event"] = event
        self.authority["reopen_review_overlay"] = {"review_state": "pending", "signed_delta": deepcopy(signed_delta), "delta_fingerprint": signed_delta["canonical_delta_fingerprint"]}
        return deepcopy(self.authority), False

def _run(repo, flow, *, current=(9,), relations=None, trusted=(), source="scan", domain="example.test"):
    relations = [_relation(repo, "M1", number=value) for value in current] if relations is None else list(relations)
    return application.run_evidence_vault_sv9_authority_application(
        repository=repo, flow=flow, domain_or_url=domain, source_scan_id=source,
        current_evidence=[_identity(value) for value in current], authoritative_relations=list(relations),
        current_series_contract=_series(), trusted_irrelevant_evidence=[_identity(value) for value in trusted],
    )

def _stage(repo):
    return evaluation_service.run_evidence_vault_sv9_authority_evaluation(
        repository=repo, flow=_Flow(), domain_or_url="example.test", source_scan_id="scan",
        current_evidence=[_identity(9)], authoritative_relations=[_relation(repo, "M1", number=9)], current_series_contract=_series(),
        trusted_irrelevant_evidence=[],
    )

def test_idempotency_binds_action_source_candidate_delta_and_predecessor():
    base = application._idempotency("adopt_candidate", "scan", _uuid(1), None, _hash(1))
    assert base == application._idempotency("adopt_candidate", "scan", _uuid(1), None, _hash(1))
    assert len({base, application._idempotency("reopen_authority", "scan", None, _hash(2), _hash(1)), application._idempotency("reopen_authority", "scan", None, _hash(3), _hash(1)), application._idempotency("adopt_candidate", "scan-2", _uuid(1), None, _hash(1)), application._idempotency("adopt_candidate", "scan", _uuid(2), None, _hash(1)), application._idempotency("adopt_candidate", "scan", _uuid(1), None, _hash(2))}) == 6

def test_coverage_loss_is_reopen_eligible_but_empty_delta_is_not():
    evidence = delta.build_evidence_identity_set([])
    covered = delta.build_evidence_vault_sv9_judgment_delta(current_evidence=evidence, prior_judgments=[_judgment()], authoritative_relations=[], current_series_contract=_series())
    empty = delta.build_evidence_vault_sv9_judgment_delta(current_evidence=evidence, prior_judgments=[], authoritative_relations=[], current_series_contract=_series())
    assert not covered["plan"]["review_set"] and covered["coverage_loss"] and history._sv9_authority_delta(covered) == covered
    with pytest.raises(history.EvidenceVaultSv9JudgmentCandidateError): history._sv9_authority_delta(empty)

def test_first_adopt_crash_recovery_and_idempotent_retry_are_append_safe():
    fresh = _ApplicationRepository(records=(9,)); first = _run(fresh, _Flow())
    assert first["status"] == "authority_established" and fresh.mutations == ["adopt"] and fresh.append_calls == 1
    repo = _ApplicationRepository(records=(9,)); staged = _stage(repo); flow = _Flow(); recovered = _run(repo, flow)
    assert staged["status"] == recovered["evaluation_status"] == "candidate_available" and recovered["status"] == "authority_established" and not flow.calls and repo.append_calls == 1 and repo.mutations == ["adopt"]
    repeated = _run(repo, _Flow()); assert repeated["status"] == "authority_retained" and repo.mutations == ["adopt"]

@pytest.mark.parametrize(("error", "reason"), [
    (history.EvidenceVaultSv9AuthoritativeRelationStaleWitnessError("stale"), "stale_authoritative_relation_witness"),
    (history.EvidenceVaultSv9AuthoritativeRelationWitnessError("invalid"), "invalid_authoritative_relation_witness"),
    (history.EvidenceVaultSv9JudgmentCandidateLegacyAuthorityError("legacy"), "unwitnessed_legacy_candidate"),
])
def test_typed_adoption_witness_errors_require_review_without_authority_write(error, reason):
    assert history.EvidenceVaultSv9JudgmentCandidateLegacyAuthorityError is application.EvidenceVaultSv9JudgmentCandidateLegacyAuthorityError
    repo = _ApplicationRepository(records=(9,)); repo.adopt_error = error
    result = _run(repo, _Flow())
    assert result["status"] == "review_required" and result["reason_codes"] == [reason] and result["evaluation_status"] == "candidate_available" and not repo.mutations and repo.authority is None

def test_source_identity_and_interleavings_reject_without_new_event():
    repo, flow = _ApplicationRepository(records=(9,)), _Flow(); mismatch = _run(repo, flow, domain="other.test")
    assert mismatch["status"] == "authority_conflict" and mismatch["reason_codes"] == ["invalid_source_identity"] and not flow.calls and not repo.get_calls and not repo.append_calls and not repo.mutations
    appeared, other = _ApplicationRepository(records=(9,)), _ApplicationRepository(records=(3,)); assert _run(other, _Flow(), current=(3,), source="other")["status"] == "authority_established"; appeared.appear, appeared.interleave = deepcopy(other.authority), 2
    conflict = _run(appeared, _Flow()); assert conflict["status"] == "authority_conflict" and not appeared.mutations
    for review in (False, True):
        repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"; current = (3,) if review else (3, 9); repo.records, repo.authority_calls, repo.interleave = current, 0, 2
        relations = [_relation(repo, "M1", number=value) for value in current]
        result = _run(repo, _Flow(), current=current, relations=relations, source=f"scan-{review}")
        assert result["evaluation_status"] == ("review_required" if review else "candidate_available") and result["status"] == "authority_conflict" and repo.mutations == ["adopt"]

@pytest.mark.parametrize("missing", (False, True), ids=("null_timestamp", "missing_timestamp"))
@pytest.mark.parametrize("next_authority", (lambda: _authority(), lambda: None), ids=("valid", "absent"))
def test_invalid_persisted_wrapper_stops_before_a_second_authority_read(missing, next_authority):
    invalid = _authority(); invalid["event"].pop("created_at") if missing else invalid["event"].__setitem__("created_at", None)
    repo, flow = _ApplicationRepository(records=(9,)), _Flow(); repo.authority_responses = [invalid, next_authority()]
    result = _run(repo, flow)
    assert result == {"status": "authority_conflict", "reason_codes": ["invalid_input"], "evaluation_status": "no_new_score", "candidate": None, "signed_delta": None, "authority": None}
    assert (repo.authority_calls, repo.projection_calls, repo.context_calls, repo.evidence_calls, repo.get_calls, repo.append_calls, repo.mutations, flow.calls, len(repo.authority_responses)) == (1, 0, 0, 0, 0, 0, [], [], 1)

def test_supersede_and_consecutive_reopens_are_single_append_state_transitions():
    repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"; repo.records = (3, 9); prior = repo.authority["accepted_candidate"]["id"]
    advanced = _run(repo, _Flow(), current=(3, 9), relations=[_relation(repo, "M1", number=3), _relation(repo, "M1", number=9)], source="scan-2")
    assert advanced["status"] == "authority_advanced" and repo.authority["accepted_candidate"]["id"] != prior and repo.authority["accepted_candidate"]["source_scan_id"] == "scan-2"
    repo.records = (9,); review = _run(repo, _Flow(), current=(9,), relations=[_relation(repo, "M1", number=9)], source="scan-3"); events = len(repo.mutations)
    assert review["status"] == "review_required" and repo.mutations[-1] == "reopen" and repo.authority["accepted_candidate"]["id"] != prior
    again = _run(repo, _Flow(), current=(9,), relations=[_relation(repo, "M1", number=9)], source="scan-3")
    assert again["status"] == "review_required" and len(repo.mutations) == events
    repo.records = (3,); next_reopen = _run(repo, _Flow(), current=(3,), relations=[_relation(repo, "M1", number=3)], source="scan-4")
    assert next_reopen["status"] == "review_required" and repo.mutations[-2:] == ["reopen", "reopen"] and repo.authority["current_head"]["active_parent_event_id"] == repo.authority["active_authority_event"]["event_id"]

def test_selected_older_event_uses_current_head_for_stable_authority():
    repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"; older = deepcopy(repo.authority["event"])
    repo.records = (3, 9); relations = [_relation(repo, "M1", number=3), _relation(repo, "M1", number=9)]
    assert _run(repo, _Flow(), current=(3, 9), relations=relations, source="scan-2")["status"] == "authority_advanced"
    repo.authority["event"] = older
    assert application._authority(repo.authority)["head"] == evaluation_service._authority(repo.authority)[2]["current_head_event_fingerprint"] == repo.authority["current_head"]["event_fingerprint"]

def test_pending_overlay_blocks_adoption_stable_success_and_retention(monkeypatch):
    repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"; repo.records = (3, 9)
    relations = [_relation(repo, "M1", number=3), _relation(repo, "M1", number=9)]; assert _run(repo, _Flow(), current=(3, 9), relations=relations, source="scan-2")["status"] == "authority_advanced"
    repo.records = (9,); assert _run(repo, _Flow(), current=(9,), relations=[_relation(repo, "M1", number=9)], source="scan-3")["status"] == "review_required"; accepted = deepcopy(repo.authority["accepted_candidate"]); older = next(deepcopy(row) for row in repo.candidates.values() if row["id"] != accepted["id"]); before = list(repo.mutations)
    def candidate_outcome(candidate): return {"status": "candidate_available", "reason_codes": [], "candidate": candidate, "accepted_authority": {"current_head_event_fingerprint": repo.authority["current_head"]["event_fingerprint"]}}
    for candidate in (older, accepted):
        monkeypatch.setattr(evaluation_service, "run_evidence_vault_sv9_authority_evaluation", lambda **_kwargs: candidate_outcome(candidate))
        assert _run(repo, _Flow(), source=candidate["source_scan_id"])["status"] == "authority_conflict" and repo.mutations == before
    monkeypatch.setattr(evaluation_service, "run_evidence_vault_sv9_authority_evaluation", lambda **_kwargs: {"status": "no_new_score", "reason_codes": ["exact_reuse"]})
    assert _run(repo, _Flow())["status"] == "authority_conflict" and repo.mutations == before

def test_first_run_review_and_no_score_or_repository_failures_fail_closed():
    first = _ApplicationRepository(records=()); unresolved = _run(first, _Flow())
    assert unresolved["status"] == "first_run_unresolved" and not first.mutations
    failed = _ApplicationRepository(records=(9,)); assert _run(failed, _Flow(fail=1), relations=[_relation(failed, "M1", number=9)], trusted=())["status"] == "first_run_unresolved"
    repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"
    retained = _run(repo, _Flow(fail=1), relations=[_relation(repo, "M1", number=9)], trusted=())
    assert retained["status"] == "authority_retained" and repo.mutations == ["adopt"]
    repo.authority_failure = True; assert _run(repo, _Flow())["status"] == "authority_conflict"

def test_stale_predecessor_and_bad_readback_do_not_overwrite_authority():
    repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"; repo.records = (3, 9); before = deepcopy(repo.authority); repo.authority_calls = 0
    repo.adopt_failure = True
    stale = _run(repo, _Flow(), current=(3, 9), relations=[_relation(repo, "M1", number=3), _relation(repo, "M1", number=9)], source="scan-2")
    assert stale["status"] == "authority_conflict" and repo.authority == before and repo.mutations == ["adopt", "adopt"] and repo.authority_calls == 3
    repo.adopt_failure = False; repo.corrupt_after_adopt = True
    mismatch = _run(repo, _Flow(), current=(3, 9), relations=[_relation(repo, "M1", number=3), _relation(repo, "M1", number=9)], source="scan-2")
    assert mismatch["status"] == "authority_conflict" and repo.mutations == ["adopt", "adopt", "adopt"]

def test_authority_event_readback_is_canonical_and_fails_closed_for_tampering():
    repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"
    value, event = deepcopy(repo.authority), repo.authority["event"]
    assert authority_event.validate_evidence_vault_sv9_authority_event(event) == json.loads(json.dumps(event)) and event["created_at"] and event["idempotency_key_hash"] == application._idempotency("adopt_candidate", "scan", value["accepted_candidate"]["id"], None, None)
    def invalid(change):
        broken = deepcopy(value); change(broken)
        with pytest.raises(ValueError): application._authority(broken)
    invalid(lambda row: row["event"].pop("created_at"))
    invalid(lambda row: row["event"].__setitem__("unknown", "field"))
    invalid(lambda row: row["active_authority_event"]["request"].__setitem__("source_scan_id", "tampered"))
    def stale_candidate(row):
        candidate = row["accepted_candidate"]; candidate["assessment_fingerprint"] = _hash(777); candidate["assessment"]["assessment_fingerprint"] = candidate["assessment_fingerprint"]; row["assessment"]["assessment_fingerprint"] = candidate["assessment_fingerprint"]; candidate.pop("complete_record_fingerprint"); candidate["complete_record_fingerprint"] = authority_event.candidate_complete_record_fingerprint(candidate)
    invalid(stale_candidate)
    def mismatched_head(row):
        candidate, active = row["accepted_candidate"], row["active_authority_event"]
        identity = {name: candidate[name] for name in "id complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint source_scan_id".split()}
        request = authority_event.build_evidence_vault_sv9_authority_request(action="adopt_candidate", candidate_id=candidate["id"], expected_predecessor_event_fingerprint=active["event_fingerprint"], delta_fingerprint=None, source_scan_id=candidate["source_scan_id"])
        row["current_head"] = row["event"] = authority_event.build_evidence_vault_sv9_authority_event(event_id=_uuid(999), event_type="supersede", sequence=2, predecessor_event_fingerprint=active["event_fingerprint"], active_parent_event_fingerprint=active["event_fingerprint"], candidate_identity=identity, current_series_fingerprint=candidate["current_series_fingerprint"], delta_fingerprint=None, request=request, idempotency_key_hash=authority_event.authority_application_idempotency_fingerprint(request), predecessor_event_id=active["event_id"], active_parent_event_id=active["event_id"], created_at=active["created_at"])
    invalid(mismatched_head)
    repo.records = (3, 9); assert _run(repo, _Flow(), current=(3, 9), relations=[_relation(repo, "M1", number=3), _relation(repo, "M1", number=9)], source="scan-2")["status"] == "authority_advanced"
    repo.records = (9,); assert _run(repo, _Flow(), current=(9,), relations=[_relation(repo, "M1", number=9)], source="scan-3")["status"] == "review_required"
    reopen = deepcopy(repo.authority)
    for change in (lambda row: row["reopen_review_overlay"].__setitem__("delta_fingerprint", _hash(888)), lambda row: row["current_head"].__setitem__("active_parent_event_id", _uuid(998))):
        with pytest.raises(ValueError):
            broken = deepcopy(reopen); change(broken); application._authority(broken)
# fmt: on
