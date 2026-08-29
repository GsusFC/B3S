# fmt: off
from copy import deepcopy
import pytest

from src.history import repository as history
from src.services import evidence_vault_sv9_authority_application as application
from src.services import evidence_vault_sv9_authority_evaluation as evaluation_service
from src.services import evidence_vault_sv9_judgment_delta as delta
from tests.test_evidence_vault_sv9_authority_evaluation import _Flow, _Repository, _hash, _identity, _relation, _series
from tests.test_sv9_judgment_memory import _judgment

def _uuid(number): return f"00000000-0000-0000-0000-{number:012d}"

class _ApplicationRepository(_Repository):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.event, self.candidate, self.authority_calls, self.interleave, self.appear = 500, 400, 0, 0, None; self.adopt_failure = self.corrupt_after_adopt = self.authority_failure = self._corrupt = False
    def get_evidence_vault_sv9_judgment_authority(self, _domain, **_kwargs):
        self.authority_calls += 1
        if self.authority_failure: raise RuntimeError("authority unavailable")
        if self.interleave == self.authority_calls and self.appear: self.authority = deepcopy(self.appear)
        elif self.authority and self.interleave == self.authority_calls: self.event += 1; self.authority["current_head"] = {"event_fingerprint": _hash(self.event)}
        value = deepcopy(self.authority)
        if value and self._corrupt: value["accepted_candidate"]["score_fingerprint"] = _hash(999)
        return value
    def append_evidence_vault_sv9_judgment_candidate(self, scan, candidate, **kwargs):
        stored, inserted = super().append_evidence_vault_sv9_judgment_candidate(scan, candidate, **kwargs)
        if inserted:
            self.candidate += 1; self.candidates[stored["canonical_plan_fingerprint"]]["id"] = _uuid(self.candidate)
            stored = deepcopy(self.candidates[stored["canonical_plan_fingerprint"]])
        return stored, inserted
    def _accept(self, candidate_id, scan, kind):
        candidate = next(deepcopy(row) for row in self.candidates.values() if row["id"] == candidate_id) | {"source_scan_id": scan}
        partition = {key: candidate[key] for key in ("candidate_tile_judgments", "candidate_component_sentinels")}
        self.event += 1
        self.authority = {"authority": True, "accepted_candidate": candidate, "accepted_partition": partition, "assessment": candidate["assessment"], "score": candidate["assessment"]["sv9_score"], "active_authority_event": {"event_id": _uuid(self.event), "event_type": kind}, "current_head": {"event_fingerprint": _hash(self.event)}, "reopen_review_overlay": None}
    def adopt_evidence_vault_sv9_judgment_candidate(self, scan, candidate_id, *, expected_predecessor_event_fingerprint, **_kwargs):
        self.mutations.append("adopt")
        if self.authority and expected_predecessor_event_fingerprint != self.authority["current_head"]["event_fingerprint"]: raise RuntimeError("stale")
        if self.adopt_failure: raise RuntimeError("stale")
        self._accept(candidate_id, scan, "supersede" if self.authority else "adopt"); self._corrupt = self.corrupt_after_adopt
        return deepcopy(self.authority), False
    def reopen_evidence_vault_sv9_judgment_authority(self, scan, signed_delta, *, expected_predecessor_event_fingerprint, **_kwargs):
        self.mutations.append("reopen")
        if expected_predecessor_event_fingerprint != self.authority["current_head"]["event_fingerprint"]: raise RuntimeError("stale")
        self.event += 1; self.authority["current_head"] = {"event_fingerprint": _hash(self.event)}
        self.authority["reopen_review_overlay"] = {"review_state": "pending", "signed_delta": deepcopy(signed_delta), "delta_fingerprint": signed_delta["canonical_delta_fingerprint"]}
        return deepcopy(self.authority), False

def _run(repo, flow, *, current=(9,), relations=(), trusted=(9,), source="scan", domain="example.test"):
    return application.run_evidence_vault_sv9_authority_application(
        repository=repo, flow=flow, domain_or_url=domain, source_scan_id=source,
        current_evidence=[_identity(value) for value in current], authoritative_relations=list(relations),
        current_series_contract=_series(), trusted_irrelevant_evidence=[_identity(value) for value in trusted],
    )

def _stage(repo):
    return evaluation_service.run_evidence_vault_sv9_authority_evaluation(
        repository=repo, flow=_Flow(), domain_or_url="example.test", source_scan_id="scan",
        current_evidence=[_identity(9)], authoritative_relations=[], current_series_contract=_series(),
        trusted_irrelevant_evidence=[_identity(9)],
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

def test_source_identity_and_interleavings_reject_without_new_event():
    repo, flow = _ApplicationRepository(records=(9,)), _Flow(); mismatch = _run(repo, flow, domain="other.test")
    assert mismatch["status"] == "authority_conflict" and mismatch["reason_codes"] == ["invalid_source_identity"] and not flow.calls and not repo.get_calls and not repo.append_calls and not repo.mutations
    appeared, other = _ApplicationRepository(records=(9,)), _ApplicationRepository(records=(3,)); assert _run(other, _Flow(), current=(3,), trusted=(3,), source="other")["status"] == "authority_established"; appeared.appear, appeared.interleave = deepcopy(other.authority), 2
    conflict = _run(appeared, _Flow()); assert conflict["status"] == "authority_conflict" and not appeared.mutations
    for disposition in ("relevant", "contradiction"):
        repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"; repo.records, repo.authority_calls, repo.interleave = (3, 9), 0, 2
        result = _run(repo, _Flow(), current=(3, 9), trusted=(3,), relations=[_relation(repo, "M1", disposition, 9)], source=f"scan-{disposition}")
        assert result["evaluation_status"] == ("candidate_available" if disposition == "relevant" else "review_required") and result["status"] == "authority_conflict" and repo.mutations == ["adopt"]

def test_supersede_and_same_reopen_are_single_append_state_transitions():
    repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"; repo.records = (3, 9); prior = repo.authority["accepted_candidate"]["id"]
    advanced = _run(repo, _Flow(), current=(3, 9), trusted=(3,), relations=[_relation(repo, "M1", number=9)], source="scan-2")
    assert advanced["status"] == "authority_advanced" and repo.authority["accepted_candidate"]["id"] != prior and repo.authority["accepted_candidate"]["source_scan_id"] == "scan-2"
    review = _run(repo, _Flow(), current=(3, 9), relations=[_relation(repo, "M1", "contradiction", 9)], trusted=(3,), source="scan-3"); events = len(repo.mutations)
    assert review["status"] == "review_required" and repo.mutations[-1] == "reopen" and repo.authority["accepted_candidate"]["id"] != prior
    again = _run(repo, _Flow(), current=(3, 9), relations=[_relation(repo, "M1", "contradiction", 9)], trusted=(3,), source="scan-3")
    assert again["status"] == "review_required" and len(repo.mutations) == events

def test_first_run_review_and_no_score_or_repository_failures_fail_closed():
    first = _ApplicationRepository(records=(9,)); unresolved = _run(first, _Flow(), relations=[_relation(first, "M1", "contradiction", 9)], trusted=())
    assert unresolved["status"] == "first_run_unresolved" and not first.mutations
    failed = _ApplicationRepository(records=(9,)); assert _run(failed, _Flow(fail=1), relations=[_relation(failed, "M1", number=9)], trusted=())["status"] == "first_run_unresolved"
    repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"
    retained = _run(repo, _Flow(fail=1), relations=[_relation(repo, "M1", number=9)], trusted=())
    assert retained["status"] == "authority_retained" and repo.mutations == ["adopt"]
    repo.authority_failure = True; assert _run(repo, _Flow())["status"] == "authority_conflict"

def test_stale_predecessor_and_bad_readback_do_not_overwrite_authority():
    repo = _ApplicationRepository(records=(9,)); assert _run(repo, _Flow())["status"] == "authority_established"; repo.records = (3, 9); before = deepcopy(repo.authority); repo.authority_calls = 0
    repo.adopt_failure = True
    stale = _run(repo, _Flow(), current=(3, 9), trusted=(3,), relations=[_relation(repo, "M1", number=9)], source="scan-2")
    assert stale["status"] == "authority_conflict" and repo.authority == before and repo.mutations == ["adopt", "adopt"] and repo.authority_calls == 3
    repo.adopt_failure = False; repo.corrupt_after_adopt = True
    mismatch = _run(repo, _Flow(), current=(3, 9), trusted=(3,), relations=[_relation(repo, "A1", number=9)], source="scan-3")
    assert mismatch["status"] == "authority_conflict" and repo.mutations == ["adopt", "adopt", "adopt"]
# fmt: on
