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
from src.services import evidence_vault_sv9_workset_partition as partitioning
from src.services.evidence_vault_canonical_core import canonical_fingerprint
from tests.test_evidence_vault_sv9_authority_evaluation import _Flow, _Repository, _authority, _hash, _identity, _relation, _series
from tests.test_sv9_judgment_memory import _judgment

def _uuid(number): return f"00000000-0000-0000-0000-{number:012d}"

class _ApplicationRepository(_Repository):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.context = {"capture_origin": {"capture_id": _uuid(9), "capture_fingerprint": _hash(9)}, "operation_origin": {"operation_id": _uuid(10), "operation_fingerprint": _hash(10)}}; self.event, self.candidate, self.authority_calls, self.interleave, self.appear = 500, 400, 0, 0, None; self.adopt_failure = self.corrupt_after_adopt = self.authority_failure = self._corrupt = False; self.witness_seed = 300; self.adopt_error = None; self.authority_responses = []; self.reopen_partitions = []; self.records_by_scan = {}
    def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, *, workspace_slug="b3s"):
        records = self.records_by_scan.setdefault(scan, tuple(self.records))
        source = {"workspace_id": _uuid(1), "brand_id": _uuid(2), "scan_run_id": _uuid(3), "source_scan_id": scan, "workspace_slug": workspace_slug, "canonical_domain": "example.test", "capture_id": self.context["capture_origin"]["capture_id"], "capture_fingerprint": self.context["capture_origin"]["capture_fingerprint"], "operation_plan_id": self.context["operation_origin"]["operation_id"], "operation_fingerprint": self.context["operation_origin"]["operation_fingerprint"], "operation_status": "completed"}
        evidence, basis = [], []
        for number in records:
            evidence_id, source_identity_id = _hash(100 + number), _hash(200 + number)
            evidence.append(source | {"evidence_record_id": _uuid(number), "evidence_ref": f"evidence:{number}", "evidence_fingerprint": _hash(number), "evidence_id": evidence_id, "source_identity_id": source_identity_id})
            basis.append({"relation_id": _hash(300 + number), "evidence_id": evidence_id, "source_identity_id": source_identity_id, "polarity": "supports"})
        accepted = [{"tile_id": "M1", "component_key": "mission", "assessment_state": "ok", "authority_state": "accepted", "review_state": "resolved", "lifecycle_state": "active", "basis": basis}]
        seed = self.witness_seed; witness = {"canonical_memory_version": _hash(seed), "adoption_event_id": _uuid(seed), "adoption_sequence": 1, "candidate_packet_fingerprint": _hash(seed + 1), "request_fingerprint": _hash(seed + 2)}
        return {"source": source, "evidence": evidence, "authority": {"witness": witness, "accepted": accepted}, "evaluation_hint_seeds": []}
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
    def reopen_evidence_vault_sv9_judgment_authority(self, scan, signed_delta, *, expected_predecessor_event_fingerprint, idempotency_key_hash, workset_partition, **_kwargs):
        self.mutations.append("reopen")
        if expected_predecessor_event_fingerprint != self.authority["current_head"]["event_fingerprint"]: raise RuntimeError("stale")
        partition = partitioning.validate_evidence_vault_sv9_workset_partition(workset_partition)
        if partition["judgment_delta"] != signed_delta: raise RuntimeError("partition")
        self.reopen_partitions.append(deepcopy(partition))
        previous, active = self.authority["current_head"], self.authority["active_authority_event"]; self.event += 1
        fingerprint = signed_delta["canonical_delta_fingerprint"]
        partition_fingerprint = partition["partition_fingerprint"]
        request = authority_event.build_evidence_vault_sv9_authority_request(action="reopen_authority", candidate_id=None, expected_predecessor_event_fingerprint=previous["event_fingerprint"], delta_fingerprint=fingerprint, source_scan_id=scan, workset_partition_fingerprint=partition_fingerprint)
        event = authority_event.build_evidence_vault_sv9_authority_event(event_id=_uuid(self.event), event_type="reopen", sequence=previous["sequence"] + 1, predecessor_event_fingerprint=previous["event_fingerprint"], active_parent_event_fingerprint=active["event_fingerprint"], candidate_identity=None, current_series_fingerprint=active["current_series_fingerprint"], delta_fingerprint=fingerprint, request=request, idempotency_key_hash=idempotency_key_hash, predecessor_event_id=previous["event_id"], active_parent_event_id=active["event_id"], created_at=f"2026-01-01T00:00:{self.event % 60:02d}+00:00", workset_partition_fingerprint=partition_fingerprint)
        self.authority["current_head"] = self.authority["event"] = event
        self.authority["reopen_review_overlay"] = {"review_state": "pending", "signed_delta": deepcopy(signed_delta), "delta_fingerprint": signed_delta["canonical_delta_fingerprint"], "workset_partition": partition, "workset_partition_fingerprint": partition_fingerprint}
        return deepcopy(self.authority), False

def _run(repo, flow, *, current=(9,), relations=None, trusted=(), source="scan", domain="example.test"):
    repo.records = tuple(current)
    relations = [_relation(repo, "M1", number=value) for value in current] if relations is None else list(relations)
    repo.projection_relations = relations
    return application.run_evidence_vault_sv9_authority_application(
        repository=repo, flow=flow, domain_or_url=domain, source_scan_id=source,
        current_series_contract=_series(), trusted_irrelevant_evidence=[_identity(value) for value in trusted],
    )

def _stage(repo):
    return evaluation_service.run_evidence_vault_sv9_authority_evaluation(
        repository=repo, flow=_Flow(), domain_or_url="example.test", source_scan_id="scan",
        current_series_contract=_series(),
        trusted_irrelevant_evidence=[],
    )

def test_idempotency_binds_action_source_candidate_delta_and_predecessor():
    base = application._idempotency("adopt_candidate", "scan", _uuid(1), None, _hash(1))
    assert base == application._idempotency("adopt_candidate", "scan", _uuid(1), None, _hash(1))
    assert len({base, application._idempotency("reopen_authority", "scan", None, _hash(2), _hash(1), _hash(4)), application._idempotency("reopen_authority", "scan", None, _hash(2), _hash(1), _hash(5)), application._idempotency("reopen_authority", "scan", None, _hash(3), _hash(1), _hash(4)), application._idempotency("adopt_candidate", "scan-2", _uuid(1), None, _hash(1)), application._idempotency("adopt_candidate", "scan", _uuid(2), None, _hash(1)), application._idempotency("adopt_candidate", "scan", _uuid(1), None, _hash(2))}) == 7

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
    head = repo.authority["current_head"]
    assert next_reopen["status"] == "review_required" and repo.mutations[-2:] == ["reopen", "reopen"] and head["active_parent_event_id"] == repo.authority["active_authority_event"]["event_id"]
    assert head["schema_version"] == "evidence-vault-sv9-judgment-authority-event-v2" and head["request"]["workset_partition_fingerprint"] == repo.authority["reopen_review_overlay"]["workset_partition_fingerprint"]

def test_continuity_only_reopen_carries_the_validated_partition_through_append_and_replay(monkeypatch):
    monkeypatch.setattr(
        evaluation_service,
        "project_evidence_vault_sv9_evaluation_input",
        lambda *, repository, source_scan_id, workspace_slug: repository.evaluation_input(
            source_scan_id, workspace_slug
        ),
    )
    repo = _ApplicationRepository(records=(9,))
    assert _run(repo, _Flow(), current=(9,))["status"] == "authority_established"
    repo.reopen = True
    accepted = deepcopy(repo.authority["accepted_candidate"])
    mutations = list(repo.mutations)

    result = _run(repo, _Flow(), current=(3, 9))

    overlay = repo.authority["reopen_review_overlay"]
    partition = overlay["workset_partition"]
    assert result["status"] == result["evaluation_status"] == "review_required"
    assert not overlay["signed_delta"]["plan"]["review_set"] and not overlay["signed_delta"]["coverage_loss"]
    assert partition["review_partition"]["evaluation_input_reopened_tile_ids"] == ["M1"]
    assert partition["review_partition"]["operational_authority_coverage_loss_tile_ids"] == ["M1"]
    assert partition["review_partition"]["tile_ids"] != ["M1"]
    assert repo.mutations == mutations + ["reopen"] and repo.authority["accepted_candidate"] == accepted
    assert authority_projection.validate_persisted_evidence_vault_sv9_authority_projection(repo.authority) == repo.authority


def test_review_handoff_rejects_absent_or_tampered_partitions_without_writes(monkeypatch):
    monkeypatch.setattr(
        evaluation_service,
        "project_evidence_vault_sv9_evaluation_input",
        lambda *, repository, source_scan_id, workspace_slug: repository.evaluation_input(
            source_scan_id, workspace_slug
        ),
    )
    repo = _ApplicationRepository(records=(9,))
    assert _run(repo, _Flow(), current=(9,))["status"] == "authority_established"
    repo.reopen = True
    base = evaluation_service.run_evidence_vault_sv9_authority_evaluation(
        repository=repo,
        flow=_Flow(),
        domain_or_url="example.test",
        source_scan_id="scan",
        current_series_contract=_series(),
    )
    assert base["status"] == "review_required" and base["workset_partition"]["review_partition"]["evaluation_input_reopened_tile_ids"] == ["M1"]

    def rehash(value):
        value["partition_fingerprint"] = canonical_fingerprint(
            "evidence-vault-sv9-workset-partition-fingerprint-v1",
            {key: item for key, item in value.items() if key != "partition_fingerprint"},
        )

    alternate = delta.build_evidence_vault_sv9_judgment_delta(
        current_evidence=base["signed_delta"]["current_evidence"],
        prior_judgments=base["signed_delta"]["prior_judgments"],
        prior_component_sentinels=base["signed_delta"]["plan"]["prior_component_sentinels"],
        authoritative_relations=base["signed_delta"]["authoritative_relations"],
        current_series_contract=_series(prompt_version="partition-mismatch"),
    )
    cases = [
        lambda outcome: outcome.pop("workset_partition"),
        lambda outcome: (
            outcome["workset_partition"]["review_partition"]["evaluation_input_reopened_tile_ids"].append("M2"),
            rehash(outcome["workset_partition"]),
        ),
        lambda outcome: (
            outcome["workset_partition"]["review_partition"].__setitem__("tile_ids", ["M2"]),
            rehash(outcome["workset_partition"]),
        ),
        lambda outcome: outcome.__setitem__(
            "workset_partition",
            partitioning.build_evidence_vault_sv9_workset_partition(
                evaluation_input=base["workset_partition"]["evaluation_input"],
                judgment_delta=alternate,
                trusted_irrelevant_evidence=[],
            ),
        ),
    ]
    accepted = deepcopy(repo.authority["accepted_candidate"])
    for mutate in cases:
        outcome = deepcopy(base); mutate(outcome); before = list(repo.mutations)
        monkeypatch.setattr(evaluation_service, "run_evidence_vault_sv9_authority_evaluation", lambda **_kwargs: outcome)
        result = _run(repo, _Flow(), current=(3, 9))
        assert result["status"] == "authority_conflict" and repo.mutations == before
        assert repo.authority["accepted_candidate"] == accepted and result["candidate"] is None and result["signed_delta"] is None


def test_stale_continuity_reopen_predecessor_is_rejected_without_a_second_write(monkeypatch):
    monkeypatch.setattr(
        evaluation_service,
        "project_evidence_vault_sv9_evaluation_input",
        lambda *, repository, source_scan_id, workspace_slug: repository.evaluation_input(
            source_scan_id, workspace_slug
        ),
    )
    repo = _ApplicationRepository(records=(9,))
    assert _run(repo, _Flow(), current=(9,))["status"] == "authority_established"
    repo.reopen = True
    repo.records = (3, 9)
    repo.projection_relations = [_relation(repo, "M1", number=3), _relation(repo, "M1", number=9)]
    stale = evaluation_service.run_evidence_vault_sv9_authority_evaluation(
        repository=repo,
        flow=_Flow(),
        domain_or_url="example.test",
        source_scan_id="scan",
        current_series_contract=_series(),
    )
    assert stale["status"] == "review_required"
    repo.reopen = False
    advanced = _run(
        repo,
        _Flow(),
        current=(3, 9),
        relations=[_relation(repo, "M1", number=3), _relation(repo, "M1", number=9)],
        source="scan-2",
    )
    assert advanced["status"] == "authority_advanced"
    before, accepted = list(repo.mutations), deepcopy(repo.authority["accepted_candidate"])
    monkeypatch.setattr(
        evaluation_service,
        "run_evidence_vault_sv9_authority_evaluation",
        lambda **_kwargs: deepcopy(stale),
    )

    result = _run(repo, _Flow(), current=(3, 9), source="scan")

    assert result["status"] == "authority_conflict" and repo.mutations == before
    assert repo.authority["accepted_candidate"] == accepted
    assert result["candidate"] is result["signed_delta"] is None


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
    first = _ApplicationRepository(records=()); unresolved = _run(first, _Flow(), current=())
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


class _ValidatedCandidateRepository(_ApplicationRepository):
    def resolve_evidence_vault_sv9_judgment_evidence(self, *args, **kwargs):
        result = super().resolve_evidence_vault_sv9_judgment_evidence(*args, **kwargs)
        for row in result["evidence"]:
            row["content"] = f"Frozen {row['evidence_ref']}"
        return result

    def append_evidence_vault_sv9_judgment_candidate(self, scan, candidate, **kwargs):
        context = self.load_evidence_vault_sv9_authoritative_relation_facts(scan)["source"]
        resolved = self.resolve_evidence_vault_sv9_judgment_evidence(
            scan, sorted(f"evidence:{number}" for number in self.records)
        )["evidence"]

        class Connection:
            def execute(self, query, params):
                assert "FROM b3s_history.evidence_records" in query
                self.ids = {str(value) for value in params[1]}
                return self

            def fetchall(self):
                return [
                    {
                        "id": row["evidence_record_id"],
                        "evidence_ref": row["evidence_ref"],
                        "content_hash": row["evidence_fingerprint"],
                        "content": row["content"],
                        "content_raw": row["content"].encode(),
                    }
                    for row in resolved
                    if row["evidence_record_id"] in self.ids
                ]

        # Exercise the real persistence envelope, bindings, packet replay and witness guards.
        packets = history._sv9_judgment_packets(Connection(), candidate, context)
        assert history._sv9_judgment_candidate_replay(candidate, packets) == candidate
        history._sv9_judgment_candidate_witness(candidate, context)
        return super().append_evidence_vault_sv9_judgment_candidate(scan, candidate, **kwargs)


class _CoherenciaRepository(_ValidatedCandidateRepository):
    def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, **kwargs):
        facts = super().load_evidence_vault_sv9_authoritative_relation_facts(scan, **kwargs)
        mission = facts["authority"]["accepted"][0]
        coherencia = deepcopy(mission)
        coherencia.update(tile_id="C1", component_key="coherencia")
        coherencia["basis"] = [deepcopy(next(row for row in mission["basis"] if row["evidence_id"] == _hash(109)))]
        coherencia["basis"][0]["relation_id"] = _hash(999)
        facts["authority"]["accepted"].append(coherencia)
        return facts


@pytest.mark.parametrize("recovery", ("none", "checkpoint", "adoption"))
def test_incremental_coherencia_support_survives_persistence_and_recovery(recovery):
    repo = _CoherenciaRepository(records=(9,))
    assert _run(repo, _Flow())["status"] == "authority_established"
    initial = deepcopy(repo.authority)
    for tile_id in ("M1", "C1"):
        assert next(
            row for row in initial["accepted_partition"]["candidate_tile_judgments"] if row["tile_id"] == tile_id
        )["supporting_evidence"] == [_identity(9)]
    repo.adopt_failure = recovery == "adoption"
    flow = _Flow(fail=2 if recovery == "checkpoint" else None)
    first = _run(repo, flow, current=(3, 9), source="scan-2")
    assert [row["component_key"] for row in flow.calls] == ["mission", "coherencia"]
    c1 = next(row for row in flow.calls[1]["requested_tiles"] if row["tile_id"] == "C1")
    assert [row["evidence_ref"] for row in c1["evidence"]] == ["evidence:9"]
    if recovery != "none":
        assert repo.authority == initial
        repo.adopt_failure = False
        retry = _Flow()
        result = _run(repo, retry, current=(3, 9), source="scan-2")
        if recovery == "checkpoint":
            assert retry.calls == [flow.calls[1]]
        else:
            assert first["evaluation_status"] == "candidate_available" and not retry.calls
    else:
        result = first
    assert result["status"] == "authority_advanced", result["reason_codes"]
    candidate = repo.authority["accepted_candidate"]
    assert candidate["source_scan_id"] == "scan-2"
    assert {row["tile_id"]: row["evidence_ref"] for row in candidate["evidence_bindings"]} == {
        "M1": "evidence:3",
        "C1": "evidence:9",
    }
    expected_requests = [row["canonical_request_fingerprint"] for row in flow.calls]
    assert [row["request_fingerprint"] for row in candidate["component_evaluations"]] == expected_requests
    for fingerprint in expected_requests:
        assert (candidate["canonical_plan_fingerprint"], fingerprint) in repo.checkpoints
    assert (
        authority_projection.validate_persisted_evidence_vault_sv9_authority_projection(repo.authority)
        == repo.authority
    )


@pytest.mark.parametrize(("tile", "component"), (("C1", "coherencia"), ("M2", "mission")))
def test_cross_tile_routing_hint_cannot_become_canonical_evidence(tile, component):
    class Repository(_ValidatedCandidateRepository):
        def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, **kwargs):
            facts = super().load_evidence_vault_sv9_authoritative_relation_facts(scan, **kwargs)
            facts["evaluation_hint_seeds"] = [
                {
                    "hint_id": _uuid(987),
                    "tile_id": tile,
                    "component_key": component,
                    "evidence_record_id": _uuid(9),
                    "provenance_fingerprint": _hash(987),
                }
            ]
            return facts

    repo, flow = Repository(records=(9,)), _Flow()
    result = _run(repo, flow)
    assert repo.authority is None and not repo.mutations and not repo.candidates
    assert result["candidate"] is None and result["reason_codes"] == ["incomplete_candidate"]
    assert repo.append_calls == 0
    requested = next(row for call in flow.calls for row in call["requested_tiles"] if row["tile_id"] == tile)
    assert [row["evidence_ref"] for row in requested["evidence"]] == ["evidence:9"]
    assert len(repo.checkpoints) == 10
    progress = next(
        row
        for value in repo.checkpoint_appends
        for row in value["healthy_workset"]["evaluated_tile_judgments"]
        if row["tile_id"] == tile
    )
    assert progress["assessment_state"] == "ok" and progress["authority_state"] == "pending"
    checkpoint = next(value for value in repo.checkpoint_appends if tile in value["healthy_workset"]["tile_ids"])
    assert checkpoint["evaluation_input"]["non_authoritative_hints"][0]["authority"] is False
    cached, retry = deepcopy(repo.checkpoints), _Flow()
    repeated = _run(repo, retry)
    assert not retry.calls and repo.checkpoints == cached
    assert repeated["candidate"] is None and not repo.mutations and not repo.candidates


def test_cross_tile_hint_retains_existing_source_without_reopen(monkeypatch, tmp_path):
    from src.services import evidence_vault_sv9_authority_report as publication
    from tests.test_evidence_vault_sv9_authority_report import _report
    from tests.test_vault_authority_scanner_orchestration import _file_report_store, _status
    from web import scan_runner

    reopen_attempts = []

    class Repository(_CoherenciaRepository):
        def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, **kwargs):
            facts = super().load_evidence_vault_sv9_authoritative_relation_facts(scan, **kwargs)
            if scan == "scan-2":
                facts["evaluation_hint_seeds"] = [
                    {
                        "hint_id": _uuid(987),
                        "tile_id": "C2",
                        "component_key": "coherencia",
                        "evidence_record_id": _uuid(9),
                        "provenance_fingerprint": _hash(987),
                    }
                ]
            return facts

        def reopen_evidence_vault_sv9_judgment_authority(self, scan, signed, **kwargs):
            reopen_attempts.append(scan)
            history._sv9_authority_partition_reopen_tile_ids(kwargs["workset_partition"], signed)
            return super().reopen_evidence_vault_sv9_judgment_authority(scan, signed, **kwargs)

    repo = Repository(records=(9,))
    first = _run(repo, _Flow())
    store = _file_report_store(monkeypatch, tmp_path)
    source = _report(publication.project_vault_authority_publication(first, "scan")["scanner_payload"])
    store.save_report(source)
    source_bytes = (tmp_path / "scan.json").read_bytes()
    accepted = deepcopy(repo.authority)
    flow = _Flow()
    result = _run(repo, flow, current=(3, 9), source="scan-2")
    assert result["status"] == "authority_retained" and result["evaluation_status"] == "no_new_score"
    assert result["reason_codes"] == ["incomplete_candidate"] and not reopen_attempts
    assert repo.authority == accepted and repo.mutations == ["adopt"] and repo.append_calls == 1
    assert any(
        row["tile_id"] == "C2" and row["supporting_evidence"] == [_identity(9)]
        for value in repo.checkpoint_appends
        for row in value["healthy_workset"]["evaluated_tile_judgments"]
    )
    retry = _Flow()
    assert _run(repo, retry, current=(3, 9), source="scan-2")["status"] == "authority_retained"
    assert not retry.calls and not reopen_attempts

    monkeypatch.setattr(application, "run_evidence_vault_sv9_authority_application", lambda **_kwargs: result)
    monkeypatch.setattr(scan_runner, "_execute_vault_operational_preparation", lambda **_kwargs: "p")
    monkeypatch.setattr(
        scan_runner, "_activate_vault_result_unless_cancelled", lambda *_args, **_kwargs: {"created": True}
    )
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda *_args: None)
    scan_runner._SCANS["scan-2"] = _status("scan-2")
    try:
        assert (
            scan_runner._run_vault_sv9_authority_scanner(
                scan_id="scan-2",
                url="https://example.test",
                brand_name="Example",
                repository=repo,
                preparation={},
                canonical_snapshot={},
                canonical_source_capture=None,
                gate={},
            )
            is True
        )
        assert scan_runner._SCANS["scan-2"]["report_id"] == "scan"
        assert (tmp_path / "scan.json").read_bytes() == source_bytes
        assert not (tmp_path / "scan-2.json").exists()
    finally:
        scan_runner._SCANS.pop("scan-2", None)
        scan_runner._VAULT_ACTIVATIONS.discard("scan-2")


class _CheckpointReplayRepository(_CoherenciaRepository):
    """Application fixture using real checkpoint SQL selection/readback and adoption guard."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.adopted_ids = set()
        self.proof_reads = []
        self.proof_fault = None
        self.fact_change = None

    def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, **kwargs):
        facts = _ApplicationRepository.load_evidence_vault_sv9_authoritative_relation_facts(self, scan, **kwargs)
        coherencia = deepcopy(facts["authority"]["accepted"][0])
        coherencia.update(
            tile_id="C1",
            component_key="coherencia",
            basis=[
                {
                    "relation_id": _hash(999),
                    "evidence_id": _hash(109),
                    "source_identity_id": _hash(209),
                    "polarity": "supports",
                }
            ],
        )
        facts["authority"]["accepted"].append(coherencia)
        if self.fact_change:
            self.fact_change(facts)
        return facts

    def adopt_evidence_vault_sv9_judgment_candidate(self, scan, candidate_id, **kwargs):
        import ast
        from pathlib import Path

        tree = ast.parse(Path(history.__file__).read_text())
        guard = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Raise)
            and any(
                isinstance(child, ast.Constant) and child.value == "SV9 judgment candidate is already adopted."
                for child in ast.walk(node)
            )
        )
        owner = self

        class Connection:
            def execute(self, query, parameters):
                assert "evidence_vault_sv9_judgment_authority_events" in query
                assert parameters[-1] == candidate_id
                return self

            def fetchone(self):
                return {"exists": 1} if candidate_id in owner.adopted_ids else None

        exec(
            compile(ast.Module(body=[guard], type_ignores=[]), history.__file__, "exec"),
            vars(history),
            {
                "conn": Connection(),
                "context": self.load_evidence_vault_sv9_authoritative_relation_facts(scan)["source"],
                "candidate_id": candidate_id,
            },
        )
        result = super().adopt_evidence_vault_sv9_judgment_candidate(scan, candidate_id, **kwargs)
        self.adopted_ids.add(candidate_id)
        return result

    def get_evidence_vault_sv9_evaluation_checkpoint_for_request(self, scan, **kwargs):
        self.proof_reads.append((scan, kwargs))
        fault = self.proof_fault
        if fault == "missing":
            return None
        context = self.load_evidence_vault_sv9_authoritative_relation_facts(scan)["source"]
        checkpoints = deepcopy(self.checkpoint_appends)
        if fault in {"stale_input", "evaluation_mismatch", "snapshot_mismatch", "ambiguous", "provider_failure"}:
            from src.services import evidence_vault_sv9_evaluation_checkpoint as checkpoints_contract
            from src.sv9 import incremental_evaluation, judgment_memory

            original = checkpoints[-1]
            changed = deepcopy(original)
            if fault == "stale_input":
                self.witness_seed += 1
                try:
                    changed["evaluation_input"] = evaluation_service.project_evidence_vault_sv9_evaluation_input(
                        repository=self, source_scan_id=scan
                    )
                finally:
                    self.witness_seed -= 1
            elif fault in {"snapshot_mismatch", "ambiguous"}:
                changed["prior_authority_snapshot"] = {"state": "bootstrap_absent"}
            elif fault != "provider_failure":
                value = changed["healthy_workset"]["component_evaluations"][0]
                tile = next(row for row in value["tile_results"] if row["supporting_evidence"])
                tile["assessment_state"] = "no"
                changed["healthy_workset"]["component_evaluations"] = [
                    incremental_evaluation.build_component_evaluation(
                        **{
                            key: item
                            for key, item in value.items()
                            if key not in {"schema_version", "canonical_component_evaluation_fingerprint"}
                        }
                    )
                ]
                for index, row in enumerate(changed["healthy_workset"]["evaluated_tile_judgments"]):
                    if row["tile_id"] == tile["tile_id"]:
                        changed["healthy_workset"]["evaluated_tile_judgments"][index] = (
                            judgment_memory.build_tile_judgment(
                                **{
                                    key: ("no" if key == "assessment_state" else item)
                                    for key, item in row.items()
                                    if key
                                    not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint"}
                                }
                            )
                        )
            changed = checkpoints_contract.build_evidence_vault_sv9_evaluation_checkpoint(
                evaluation_state="provider_failure" if fault == "provider_failure" else "partial",
                evaluation_input=changed["evaluation_input"],
                prior_authority_snapshot=changed["prior_authority_snapshot"],
                **changed["plan_binding"],
                healthy_tile_ids=changed["healthy_workset"]["tile_ids"],
                **{
                    key: changed["healthy_workset"][key]
                    for key in ("component_evaluations", "evaluated_tile_judgments", "evaluated_component_sentinels")
                },
            )
            if fault == "ambiguous":
                checkpoints.append(changed)
            else:
                checkpoints[-1] = changed
        if fault == "last_missing":
            checkpoints.pop()
        rows = []
        for index, value in enumerate(checkpoints):
            if value["evaluation_input"]["source_identity"] != context:
                continue
            plan, snapshot, input_ = value["plan_binding"], value["prior_authority_snapshot"], value["evaluation_input"]
            rows.append(
                {
                    **context,
                    **plan,
                    "id": _uuid(700 + index),
                    "created_at": "2026-09-05T00:00:00+00:00",
                    "schema_version": value["schema_version"],
                    "checkpoint_payload": value,
                    "evaluation_input_fingerprint": input_["evaluation_input_fingerprint"],
                    "relation_projection_fingerprint": input_["relation_projection_fingerprint"],
                    "prior_authority_snapshot_fingerprint": snapshot["snapshot_fingerprint"],
                    "checkpoint_fingerprint": value["checkpoint_fingerprint"],
                    "evaluation_state": value["evaluation_state"],
                    "authority": False,
                    "runtime_effect": "checkpoint_only",
                    "score_state": "unavailable",
                }
            )
        if fault == "malformed" and rows:
            rows[-1]["checkpoint_payload"]["evaluation_input_fingerprint"] = _hash(999)
        owner = self

        class Connection:
            def execute(self, query, parameters):
                if "canonical_plan_fingerprint = %s" in query:
                    assert parameters[:-1] == tuple(
                        context[key]
                        for key in (
                            "workspace_id",
                            "brand_id",
                            "scan_run_id",
                            "source_scan_id",
                            "capture_id",
                            "operation_plan_id",
                        )
                    )
                    self.result = [row for row in rows if row["canonical_plan_fingerprint"] == parameters[-1]]
                else:
                    assert "evidence_vault_sv9_evaluation_checkpoint_evidence_bindings" in query
                    row = next(row for row in rows if row["id"] == parameters[0])
                    self.result = [
                        {**context, **binding, "durable_evidence_ref": binding["evidence_ref"]}
                        for binding in row["checkpoint_payload"]["evaluation_input"]["current_identity_bindings"]
                        if binding["evidence_record_id"] in {_uuid(number) for number in owner.records}
                    ]
                return self

            def fetchall(self):
                return self.result

        kwargs.pop("workspace_slug", None)
        return history._sv9_checkpoint_for_request(Connection(), context, **kwargs)


class _SubsetSupportFlow(_Flow):
    def evaluate_component(self, request):
        from src.sv9 import incremental_evaluation as evaluator

        outcome = super().evaluate_component(request)
        if outcome.evaluation is None:
            return outcome
        value = deepcopy(outcome.evaluation)
        for row in value["tile_results"]:
            if row["tile_id"] == "M1":
                row["supporting_evidence"] = row["supporting_evidence"][:1]
        return evaluator.ComponentEvaluationOutcome.success(
            evaluator.build_component_evaluation(
                **{
                    key: item
                    for key, item in value.items()
                    if key not in {"schema_version", "canonical_component_evaluation_fingerprint"}
                }
            )
        )


@pytest.mark.parametrize("recovery", ("none", "checkpoint", "adoption"))
def test_accepted_full_input_replay_stays_stable_after_addition(recovery):
    repo = _CheckpointReplayRepository(records=(9,))
    assert _run(repo, _Flow())["status"] == "authority_established"
    repo.adopt_failure = recovery == "adoption"
    flow = _Flow(fail=2 if recovery == "checkpoint" else None)
    result = _run(repo, flow, current=(3, 9), source="scan-2")
    if recovery != "none":
        assert repo.authority["current_head"]["sequence"] == 1
        repo.adopt_failure = False
        result = _run(repo, _Flow(), current=(3, 9), source="scan-2")
    assert result["status"] == "authority_advanced"
    accepted = deepcopy(repo.authority)
    writes = (len(repo.checkpoint_appends), repo.append_calls, len(repo.mutations))
    assert next(row for row in accepted["accepted_partition"]["candidate_tile_judgments"] if row["tile_id"] == "M1")[
        "supporting_evidence"
    ] == [_identity(3)]
    for _ in range(4):
        flow = _Flow()
        result = _run(repo, flow, current=(3, 9), source="scan-2")
        assert result["status"] == "authority_retained" and result["reason_codes"] == ["exact_reuse"]
        assert not flow.calls and repo.authority == accepted
        assert (len(repo.checkpoint_appends), repo.append_calls, len(repo.mutations)) == writes
    assert repo.proof_reads


def test_accepted_full_input_replay_handles_true_subset_citations():
    repo = _CheckpointReplayRepository(records=(3, 9))
    flow = _SubsetSupportFlow()
    assert _run(repo, flow, current=(3, 9))["status"] == "authority_established"
    assert next(row for row in flow.calls[0]["requested_tiles"] if row["tile_id"] == "M1")["evidence"] == [
        {**_identity(number), "content": f"Frozen evidence:{number}"} for number in (3, 9)
    ]
    accepted = deepcopy(repo.authority)
    for _ in range(4):
        flow = _SubsetSupportFlow()
        assert _run(repo, flow, current=(3, 9))["status"] == "authority_retained"
        assert not flow.calls and repo.authority == accepted


@pytest.mark.parametrize(
    "fault",
    (
        "missing",
        "last_missing",
        "malformed",
        "ambiguous",
        "stale_input",
        "evaluation_mismatch",
        "snapshot_mismatch",
        "provider_failure",
    ),
)
def test_accepted_input_reuse_requires_every_original_checkpoint_proof(fault):
    repo = _CheckpointReplayRepository(records=(9,))
    _run(repo, _Flow())
    assert _run(repo, _Flow(), current=(3, 9), source="scan-2")["status"] == "authority_advanced"
    repo.proof_fault = fault
    flow = _Flow()
    result = _run(repo, flow, current=(3, 9), source="scan-2")
    assert repo.proof_reads and result["reason_codes"] != ["exact_reuse"]
    assert [row["component_key"] for row in flow.calls] == ["mission", "coherencia"]


@pytest.mark.parametrize(
    "change",
    (
        "new_mapped",
        "new_unmapped",
        "removed",
        "changed_hash",
        "relation",
        "provenance",
        "status",
        "series",
        "source",
        "hint",
        "overlay",
        "trusted",
    ),
)
def test_accepted_input_reuse_does_not_hide_changed_or_nonauthoritative_work(monkeypatch, change):
    repo = _CheckpointReplayRepository(records=(9,))
    _run(repo, _Flow())
    _run(repo, _Flow(), current=(3, 9), source="scan-2")
    current, source, trusted = (3, 9), "scan-2", ()
    if change in {"new_mapped", "new_unmapped", "trusted"}:
        current = (3, 7, 9)
        if change == "new_mapped":
            source = "scan-3"
        if change != "new_mapped":
            repo.fact_change = lambda facts: facts["authority"]["accepted"][0].__setitem__(
                "basis", [row for row in facts["authority"]["accepted"][0]["basis"] if row["evidence_id"] != _hash(107)]
            )
        if change == "trusted":
            trusted = (7,)
    elif change == "removed":
        current = (9,)
    elif change == "changed_hash":
        repo.fact_change = lambda facts: facts["evidence"][0].__setitem__("evidence_fingerprint", _hash(333))
        resolve = repo.resolve_evidence_vault_sv9_judgment_evidence

        def changed_resolve(*args, **kwargs):
            value = resolve(*args, **kwargs)
            value["evidence"][0]["evidence_fingerprint"] = _hash(333)
            return value

        monkeypatch.setattr(repo, "resolve_evidence_vault_sv9_judgment_evidence", changed_resolve)
    elif change == "relation":
        repo.fact_change = lambda facts: facts["authority"]["accepted"].append(
            {**deepcopy(facts["authority"]["accepted"][0]), "tile_id": "M2"}
        )
    elif change == "provenance":
        repo.witness_seed += 1
    elif change == "status":
        repo.fact_change = lambda facts: facts["source"].__setitem__("operation_status", "superseded")
    elif change == "series":
        series = _series()
        series["model_version"] = "different-model"
        from src.sv9 import judgment_memory

        series = judgment_memory.build_judgment_series_contract(
            **{key: value for key, value in series.items() if key not in {"schema_version"}}
        )
        monkeypatch.setattr(__import__(__name__, fromlist=["_series"]), "_series", lambda: series)
    elif change == "source":
        source = "scan-3"
    elif change == "hint":
        repo.fact_change = lambda facts: facts.__setitem__(
            "evaluation_hint_seeds",
            [
                {
                    "hint_id": _uuid(987),
                    "tile_id": "C2",
                    "component_key": "coherencia",
                    "evidence_record_id": _uuid(9),
                    "provenance_fingerprint": _hash(987),
                }
            ],
        )
    elif change == "overlay":
        assert _run(repo, _Flow(), current=(9,), source="scan-3")["status"] == "review_required"
        source = "scan-4"
    flow = _Flow()
    result = _run(repo, flow, current=current, source=source, trusted=trusted)
    assert result["reason_codes"] != ["exact_reuse"] and not repo.proof_reads
    if change == "new_mapped":
        assert result["status"] == "authority_advanced" and flow.calls
    if change == "hint":
        assert result["status"] == "authority_retained" and result["reason_codes"] == ["incomplete_candidate"]
        assert any(row["tile_id"] == "C2" and row["evidence"] for call in flow.calls for row in call["requested_tiles"])
