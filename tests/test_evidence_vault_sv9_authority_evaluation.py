from copy import deepcopy
import hashlib
import pytest

from src.history import repository as history
from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_sv9_authority_evaluation as service
from src.services import evidence_vault_sv9_authority_projection as authority_projection
from src.services import evidence_vault_sv9_shared_process as shared_process
from src.services.evidence_memory_identity_v2 import project_evidence_memory_row_identity
from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_sv9_authoritative_relations import (
    EvidenceVaultSv9AuthoritativeRelationStaleWitnessError,
    EvidenceVaultSv9AuthoritativeRelationWitnessError,
    EVIDENCE_VAULT_SV9_EVALUATION_INPUT_VERSION,
    project_evidence_vault_sv9_evaluation_input,
    )
from src.services import evidence_vault_sv9_judgment_delta as delta
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory
from tests.test_sv9_judgment_memory import _hash, _judgment, _origin, _series
from tests.test_evidence_vault_sv9_authoritative_relations import _facts, _Repository as _FactsRepository


# fmt: off
_ID = "00000000-0000-0000-0000-000000000201"
def _identity(number): return {"evidence_ref": f"evidence:{number}", "evidence_fingerprint": _hash(number)}
def _uuid(number): return f"00000000-0000-0000-0000-{number:012d}"
def _pending(row): return memory.build_tile_judgment(**({key: value for key, value in row.items() if key not in {"schema_version", "series_fingerprint", "canonical_judgment_fingerprint", "authority_state"}} | {"authority_state": "pending"}))
def _sentinel(series):
    row = _judgment(tile_id="M1", component_key="mission", series=series)
    value = {"component_key": "mission", "status": "not_detected", "supporting_evidence": [], "capture_origin": row["capture_origin"], "operation_origin": row["operation_origin"], "series_contract": series, "authority_state": "pending", "review_state": "none", "lifecycle_state": "active", "lifecycle_reason": ""}
    return planner.build_component_not_detected_sentinel(**value)
def _authority(series=None, sentinel=False, support=None):
    series = _series() if series is None else series; support = {} if support is None else support; rows = [_pending(_judgment(tile_id=tile, component_key=component, series=series, **({"evidence": support[tile]} if tile in support else {}))) for tile, component in planner._REGISTRY]
    sentinels = []
    if sentinel: rows = [row for row in rows if row["component_key"] != "mission"]; sentinels = [_sentinel(series)]
    partition = {"candidate_tile_judgments": rows, "candidate_component_sentinels": sentinels}
    current = (rows or sentinels)[0]["series_fingerprint"]
    candidate = {"schema_version": "evidence-vault-sv9-judgment-candidate-v2", "plan": {}, "canonical_plan_fingerprint": _hash(201), "current_series_fingerprint": current, "candidate_series_fingerprint": _hash(202), "component_evaluations": [], "evidence_bindings": [], **partition, "assessment": {"sv9_score": 1, "assessment_fingerprint": _hash(203), "score_fingerprint": _hash(204)}, "telemetry": {"call_count": 0, "calls_avoided": 0, "reused_tile_count": 0, "evaluated_tile_count": 0}, "assessment_fingerprint": _hash(203), "score_fingerprint": _hash(204), "authoritative_relation_witness": {}}
    candidate["evaluation_bundle_fingerprint"] = memory.canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": candidate["canonical_plan_fingerprint"], "evaluations": []}); candidate["complete_record_fingerprint"] = authority_event.candidate_complete_record_fingerprint(candidate); candidate |= {"id": _ID, "source_scan_id": "scan", "created_at": "2026-08-30T00:00:00+00:00"}
    identity = {name: candidate[name] for name in "id complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint source_scan_id".split()}; request = authority_event.build_evidence_vault_sv9_authority_request(action="adopt_candidate", candidate_id=_ID, expected_predecessor_event_fingerprint=None, delta_fingerprint=None, source_scan_id="scan")
    event = authority_event.build_evidence_vault_sv9_authority_event(event_id="00000000-0000-0000-0000-000000000202", event_type="adopt", sequence=1, predecessor_event_fingerprint=None, active_parent_event_fingerprint=None, candidate_identity=identity, current_series_fingerprint=current, delta_fingerprint=None, request=request, idempotency_key_hash=authority_event.authority_application_idempotency_fingerprint(request), created_at="2026-08-30T00:00:00+00:00")
    return authority_projection.build_evidence_vault_sv9_authority_projection(accepted_candidate=candidate, current_head=event, active_authority_event=event, event=event, reopen_review_overlay=None)

class _Repository:
    def __init__(self, authority=None, records=(3,), bad_reload=False):
        self.authority, self.records, self.bad_reload = authority, tuple(records), bad_reload; self.append_calls = self.get_calls = self.authority_calls = self.context_calls = self.evidence_calls = 0; self.candidates = {}; self.mutations = []; self.checkpoints = {}; self.checkpoint_gets = []; self.checkpoint_appends = []; self.shared_analysis_payloads = []; self.shared_processes = {}; self.fail_checkpoint_append = self.checkpoint_conflict = self.unmapped = self.hint_only = self.bootstrap_hint_only = self.reopen = False; self.processing_complete = ()
        self.context = {"capture_origin": {"capture_id": "00000000-0000-0000-0000-000000000009", "capture_fingerprint": _hash(9)}, "operation_origin": {"operation_id": "00000000-0000-0000-0000-000000000010", "operation_fingerprint": _hash(10)}}; self.projection_relations = None; self.projection_status = "available"; self.projection_calls = 0; self.witness_seed = 300
        # Per-scan capture rows let the historical and current captures differ; see _capture_row.
        self.captures = {}; self.operational_basis = {}; self.records_by_scan = {}; self.facts_loads = []
    def get_evidence_vault_sv9_judgment_authority(self, _domain, **_kwargs): self.authority_calls += 1; return deepcopy(self.authority)
    def load_evidence_vault_sv9_judgment_context(self, _scan, **_kwargs): self.context_calls += 1; return {"canonical_domain": "example.test", "capture_id": self.context["capture_origin"]["capture_id"], "capture_fingerprint": self.context["capture_origin"]["capture_fingerprint"], "operation_plan_id": self.context["operation_origin"]["operation_id"], "operation_fingerprint": self.context["operation_origin"]["operation_fingerprint"]}
    def resolve_evidence_vault_sv9_judgment_evidence(self, scan, refs, **_kwargs):
        self.evidence_calls += 1; assert refs == sorted(refs)
        if scan in self.captures:
            by_ref = {row["ref"]: row for row in self.captures[scan]}; assert set(refs) <= set(by_ref)
            rows = [{"evidence_record_id": _uuid(by_ref[ref]["record"]), "evidence_ref": ref, "evidence_fingerprint": by_ref[ref]["evidence_fingerprint"], "content": by_ref[ref]["content"]} for ref in refs]
        else:
            numbers = self.records
            if {_identity(number)["evidence_ref"] for number in numbers} != set(refs):
                # A historical scan resolves the records it was captured with, not the current ones.
                numbers = self.records_by_scan.get(scan, self.records)
            rows = [{"evidence_record_id": _uuid(number), **_identity(number), "content": {"evidence": number}} for number in numbers]
            assert {row["evidence_ref"] for row in rows} == set(refs)
        return {**self.context, "evidence": rows}
    def get_evidence_vault_sv9_judgment_candidate(self, _scan, *, canonical_plan_fingerprint, **_kwargs):
        self.get_calls += 1; row = deepcopy(self.candidates.get(canonical_plan_fingerprint))
        if row and self.bad_reload and self.get_calls > 1: row["complete_record_fingerprint"] = _hash(999)
        return row
    def append_evidence_vault_sv9_judgment_candidate(self, _scan, candidate, *, shared_analysis_payload=None, **_kwargs):
        self.append_calls += 1; key = candidate["canonical_plan_fingerprint"]
        self.shared_analysis_payloads.append(deepcopy(shared_analysis_payload))
        if key in self.candidates: return deepcopy(self.candidates[key]), False
        self.candidates[key] = deepcopy(candidate) | {"id": "00000000-0000-0000-0000-000000000203"}; return deepcopy(self.candidates[key]), True
    def get_evidence_vault_sv9_evaluation_checkpoint_component_evaluation(self, _scan, *, canonical_plan_fingerprint, canonical_request_fingerprint, **_kwargs):
        self.checkpoint_gets.append((canonical_plan_fingerprint, canonical_request_fingerprint))
        if self.checkpoint_conflict: raise ValueError("contradictory checkpoint")
        return deepcopy(self.checkpoints.get((canonical_plan_fingerprint, canonical_request_fingerprint)))
    def get_evidence_vault_sv9_evaluation_checkpoint_shared_process(self, _scan, *, canonical_plan_fingerprint, canonical_request_fingerprint, **_kwargs):
        return deepcopy(self.shared_processes.get((canonical_plan_fingerprint, canonical_request_fingerprint)))
    def get_evidence_vault_sv9_evaluation_checkpoint_for_request(self, _scan, *, canonical_plan_fingerprint, canonical_request_fingerprint, **_kwargs):
        for value in reversed(self.checkpoint_appends):
            if value["plan_binding"]["canonical_plan_fingerprint"] != canonical_plan_fingerprint:
                continue
            evaluations = value["healthy_workset"]["component_evaluations"]
            if evaluations and evaluations[0]["request_fingerprint"] == canonical_request_fingerprint:
                return deepcopy(value)
        return None
    def append_evidence_vault_sv9_evaluation_checkpoint(self, _scan, checkpoint, **_kwargs):
        self.checkpoint_appends.append(deepcopy(checkpoint))
        if self.fail_checkpoint_append: raise RuntimeError("checkpoint failed")
        evaluation = checkpoint["healthy_workset"]["component_evaluations"][0]; key = (checkpoint["plan_binding"]["canonical_plan_fingerprint"], evaluation["request_fingerprint"]); self.checkpoints[key] = deepcopy(evaluation)
        if _kwargs.get("shared_process_payload") is not None: self.shared_processes[key] = deepcopy(_kwargs["shared_process_payload"])
        return deepcopy(checkpoint), True
    def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, *, workspace_slug="b3s"):
        self.facts_loads.append(scan)
        source = {
            "workspace_id": "00000000-0000-0000-0000-000000000001",
            "brand_id": "00000000-0000-0000-0000-000000000002",
            "scan_run_id": "00000000-0000-0000-0000-000000000003",
            "source_scan_id": scan,
            "workspace_slug": workspace_slug,
            "canonical_domain": "example.test",
            "capture_id": "00000000-0000-0000-0000-000000000009",
            "capture_fingerprint": _hash(9),
            "operation_plan_id": "00000000-0000-0000-0000-000000000010",
            "operation_fingerprint": _hash(10),
            "operation_status": "completed",
        }
        if scan in self.captures:
            evidence = [
                source
                | {
                    "evidence_record_id": _uuid(row["record"]),
                    "evidence_ref": row["ref"],
                    "evidence_fingerprint": row["evidence_fingerprint"],
                    "evidence_id": row["evidence_id"],
                    "source_identity_id": row["source_identity_id"],
                    "source_class": row["source_class"],
                    "source": row["source"],
                    "evidence_type": row["evidence_type"],
                    "url": row["url"],
                }
                for row in self.captures[scan]
            ]
            return {"source": source, "evidence": evidence, "authority": None}
        evidence = [
            source
            | {
                "evidence_record_id": _uuid(number),
                **_identity(number),
                "evidence_id": _hash(100 + number),
                "source_identity_id": _hash(200 + number),
            }
            for number in self.records
        ]
        return {"source": source, "evidence": evidence, "authority": None}
    def adopt_evidence_vault_sv9_judgment_candidate(self, *_args, **_kwargs): self.mutations.append("adopt"); raise AssertionError
    def reopen_evidence_vault_sv9_judgment_authority(self, *_args, **_kwargs): self.mutations.append("reopen"); raise AssertionError
    def project(self, scan, workspace):
        self.projection_calls += 1
        if self.projection_status != "available": return {"status": "review_required", "reason_codes": [self.projection_status], "authoritative_relations": [], "operational_witness": None, "projection_fingerprint": None}
        rows = deepcopy(self.projection_relations if self.projection_relations is not None else [_relation(self, "M1", number=number) for number in self.records])
        seed = self.witness_seed; operational = {"canonical_memory_version": _hash(seed), "adoption_event_id": f"00000000-0000-0000-0000-{seed:012d}", "adoption_sequence": 1, "candidate_packet_fingerprint": _hash(seed + 1), "request_fingerprint": _hash(seed + 2)}
        fingerprint = canonical_fingerprint("evidence-vault-sv9-authoritative-relation-projection-v1", {"source_scan_id": scan, "capture_origin": self.context["capture_origin"], "operation_origin": self.context["operation_origin"], "operational_witness": operational, "authoritative_relations": rows})
        return {"status": "available", "reason_codes": [], "authoritative_relations": rows, "operational_witness": operational, "projection_fingerprint": fingerprint}

    def evaluation_input(self, scan, workspace):
        return project_evidence_vault_sv9_evaluation_input(repository=_FactsRepository(self.evaluation_facts(scan, workspace)), source_scan_id=scan, workspace_slug=workspace)

    def evaluation_facts(self, scan, workspace):
        facts = _facts(count=len(self.records)); source = facts["source"]; source.update(source_scan_id=scan, workspace_slug=workspace, canonical_domain="example.test"); self.context = {"capture_origin": {key: source[key] for key in ("capture_id", "capture_fingerprint")}, "operation_origin": {"operation_id": source["operation_plan_id"], "operation_fingerprint": source["operation_fingerprint"]}}
        if getattr(self, "baseline_plan", None):
            source["operation_fingerprint"] = self.baseline_plan["operation_plan_fingerprint"]
            self.context["operation_origin"]["operation_fingerprint"] = source["operation_fingerprint"]
        if scan in self.captures: return self._capture_facts(scan, facts)
        rows = [dict(facts["evidence"][0], source_scan_id=scan, canonical_domain="example.test", evidence_record_id=_uuid(number), **_identity(number), evidence_id=_hash(100 + number), source_identity_id=_hash(200 + number)) for number in self.records]
        facts["evidence"] = rows
        for index, row in enumerate(rows): facts["authority"]["accepted"][index]["basis"][0].update(evidence_id=row["evidence_id"], source_identity_id=row["source_identity_id"])
        facts["authority"]["witness"] = {"canonical_memory_version": _hash(self.witness_seed), "adoption_event_id": f"00000000-0000-0000-0000-{self.witness_seed:012d}", "adoption_sequence": 1, "candidate_packet_fingerprint": _hash(self.witness_seed + 1), "request_fingerprint": _hash(self.witness_seed + 2)}
        if self.reopen: facts["authority"]["accepted"][0]["basis"][0].update(evidence_id=_hash(998), source_identity_id=_hash(999))
        if self.unmapped or self.hint_only: facts["authority"]["accepted"] = facts["authority"]["accepted"][:1]
        if getattr(self, "empty_adopted_basis", False): facts["authority"]["accepted"] = []
        if self.hint_only:
            hint_count = getattr(self, "hint_count", 1)
            facts["evaluation_hint_seeds"] = [
                {
                    "hint_id": f"00000000-0000-0000-0000-{987 + index:012d}",
                    "tile_id": planner._REGISTRY[index % len(planner._REGISTRY)][0],
                    "component_key": planner._REGISTRY[index % len(planner._REGISTRY)][1],
                    "evidence_record_id": rows[(1 if hint_count == 1 else index // len(planner._REGISTRY))]["evidence_record_id"],
                    "provenance_fingerprint": _hash(987 + index),
                }
                for index in range(hint_count)
            ]
        if self.bootstrap_hint_only:
            facts["authority"]["accepted"] = []
            facts["evaluation_hint_seeds"] = [{"hint_id": "00000000-0000-0000-0000-000000000987", "tile_id": "M1", "component_key": "mission", "evidence_record_id": rows[0]["evidence_record_id"], "provenance_fingerprint": _hash(987)}]
        facts["processing_complete_evidence"] = [
            {key: row[key] for key in ("evidence_ref", "evidence_fingerprint")}
            for number, row in zip(self.records, rows, strict=True)
            if number in self.processing_complete
        ]
        return facts

    def _capture_facts(self, scan, facts):
        capture, components = self.captures[scan], dict(planner._REGISTRY); by_ref = {row["ref"]: row for row in capture}
        facts["evidence"] = [dict(facts["evidence"][0], source_scan_id=scan, canonical_domain="example.test", evidence_record_id=_uuid(row["record"]), evidence_ref=row["ref"], evidence_fingerprint=row["evidence_fingerprint"], evidence_id=row["evidence_id"], source_identity_id=row["source_identity_id"]) for row in capture]
        # The operational authority witnesses only the identities the test names; everything else is SV9-only support.
        facts["authority"]["accepted"] = [{"tile_id": tile, "component_key": components[tile], "assessment_state": "ok", "authority_state": "accepted", "review_state": "resolved", "lifecycle_state": "active", "basis": [{"relation_id": _hash(700 + 10 * index + offset), "evidence_id": by_ref[ref]["evidence_id"], "source_identity_id": by_ref[ref]["source_identity_id"], "polarity": "supports"} for offset, ref in enumerate(refs)]} for index, (tile, refs) in enumerate(self.operational_basis.items())]
        facts["authority"]["witness"] = {"canonical_memory_version": _hash(self.witness_seed), "adoption_event_id": _uuid(self.witness_seed), "adoption_sequence": 1, "candidate_packet_fingerprint": _hash(self.witness_seed + 1), "request_fingerprint": _hash(self.witness_seed + 2)}
        facts["evaluation_hint_seeds"] = []; facts["processing_complete_evidence"] = []
        return facts

class _Flow:
    def __init__(self, fail=None, sentinel=False, malformed=False, shared_analysis=None): self.fail, self.sentinel, self.malformed, self.calls = fail, sentinel, malformed, []; self.shared_analysis = shared_analysis; self.shared_analysis_calls = []; self.shared_components = {}
    def evaluate_component(self, request):
        self.calls.append(request)
        if self.fail == len(self.calls): return evaluation.ComponentEvaluationOutcome.provider_failure()
        if self.malformed: return evaluation.ComponentEvaluationOutcome.success({})
        if self.sentinel and request["component_key"] == "mission": rows, status = [], "not_detected"
        else:
            rows = [{"tile_id": row["tile_id"], "assessment_state": "ok" if row["evidence"] else "sin_evidencia", "supporting_evidence": [{key: item[key] for key in ("evidence_ref", "evidence_fingerprint")} for item in row["evidence"]]} for row in request["requested_tiles"]]; status = "evaluated"
        if self.shared_analysis is not None:
            from src.sv9.aggregator import score_from_tile_profile
            from src.sv9.models import ComponentResult, TileVerdict
            profile = [TileVerdict(tile_id=row["tile_id"], estado=row["assessment_state"], evidencia="Shared evidence." if row["assessment_state"] == "ok" else "", motivo="No supporting evidence." if row["assessment_state"] != "ok" else "") for row in rows]
            self.shared_components[request["component_key"]] = ComponentResult(component=request["component_key"], status="not_detected" if status == "not_detected" else "scored", score=score_from_tile_profile(profile), tile_profile=profile, evaluation_model="test-evaluator").to_dict()
        return evaluation.ComponentEvaluationOutcome.success(evaluation.build_component_evaluation(component_key=request["component_key"], series_fingerprint=request["current_series_fingerprint"], request_fingerprint=request["canonical_request_fingerprint"], status=status, tile_results=rows))
    def get_shared_checkpoint_process(self, request): return {"component_result": deepcopy(self.shared_components[request["component_key"]])}
    def restore_shared_checkpoint_process(self, request, _accepted, value): self.shared_components[request["component_key"]] = deepcopy(value["component_result"])
    def build_shared_analysis_payload(self, assessment): self.shared_analysis_calls.append(deepcopy(assessment)); return deepcopy(self.shared_analysis)

def _relation(repo, tile, disposition="relevant", number=9): return delta.build_authoritative_evidence_tile_relation(tile_id=tile, component_key=dict(planner._REGISTRY)[tile], disposition=disposition, **_identity(number), **repo.context)
@pytest.fixture(autouse=True)
def _authoritative_projection(monkeypatch):
    monkeypatch.setattr(service, "project_evidence_vault_sv9_evaluation_input", lambda *, repository, source_scan_id, workspace_slug: repository.evaluation_input(source_scan_id, workspace_slug))

def _run(repo, flow, current=(3,), relations=None, series=None, trusted=(), projection_relations=None, scan="scan"):
    relations = [_relation(repo, "M1", number=number) for number in current] if relations is None else list(relations); repo.records = tuple(current); repo.projection_relations = relations if projection_relations is None else list(projection_relations)
    result = service.run_evidence_vault_sv9_authority_evaluation(repository=repo, flow=flow, domain_or_url="example.test", source_scan_id=scan, current_series_contract=_series() if series is None else series, trusted_irrelevant_evidence=[_identity(number) for number in trusted])
    assert not repo.mutations; return result

def test_accepted_sin_evidencia_with_authoritative_first_light_routes_to_evaluation():
    repo = _Repository(records=(9,))
    prior = _judgment(assessment_state="sin_evidencia", evidence=[])
    relation = _relation(repo, "M1", number=9)
    signed = delta.build_evidence_vault_sv9_judgment_delta(
        current_evidence=delta.build_evidence_identity_set([_identity(9)]),
        prior_judgments=[prior],
        authoritative_relations=[relation],
        current_series_contract=_series(),
    )

    item = next(row for row in signed["plan"]["items"] if row["tile_id"] == "M1")
    assert item["action"] == "evaluate_delta"
    assert "M1" in signed["plan"]["tile_workset"]
    assert "M1" not in signed["plan"]["review_set"]
    assert signed["unmapped_evidence"] == []

def test_shadow_hint_alone_cannot_publish_a_candidate():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow()
    repo.hint_only = True

    outcome = _run(repo, flow, current=(3, 9))

    assert outcome["status"] == "review_required"
    assert outcome["candidate"] is None
    assert "unmapped_evidence" in outcome["reason_codes"]
    assert repo.append_calls == 0

@pytest.mark.parametrize("prior_state", ("no", "ok"))
def test_established_verdict_change_stays_in_human_review(prior_state):
    repo = _Repository(records=(9,))
    prior = _judgment(
        assessment_state=prior_state,
        evidence=[{"evidence_ref": "evidence:3", "evidence_fingerprint": _hash(3)}],
    )
    relation = _relation(repo, "M1", disposition="contradiction", number=9)
    signed = delta.build_evidence_vault_sv9_judgment_delta(
        current_evidence=delta.build_evidence_identity_set([_identity(9)]),
        prior_judgments=[prior],
        authoritative_relations=[relation],
        current_series_contract=_series(),
    )

    item = next(row for row in signed["plan"]["items"] if row["tile_id"] == "M1")
    assert item["action"] == "reopen_contradiction"
    assert "M1" in signed["plan"]["review_set"]
    assert "M1" not in signed["plan"]["tile_workset"]

def test_first_run_with_exact_operational_projection_persists_witnessed_candidate():
    repo, flow = _Repository(records=(9,)), _Flow(); result = _run(repo, flow, current=(9,))
    assert result["status"] == "candidate_available" and len(flow.calls) == len(repo.checkpoint_appends) == 10 and repo.append_calls == 1 and repo.get_calls == 2
    candidate = next(iter(repo.candidates.values())); assert candidate["evidence_bindings"][0]["evidence_ref"] == "evidence:9" and result["candidate"]["id"] == "00000000-0000-0000-0000-000000000203"
    assert candidate["schema_version"] == "evidence-vault-sv9-judgment-candidate-v2" and result["candidate"]["authoritative_relation_witness_fingerprint"] == candidate["authoritative_relation_witness"]["witness_fingerprint"]
    assert "workset_partition" not in result
    repeated = _run(repo, _Flow(), current=(9,)); assert repeated["status"] == "candidate_available" and not repeated["calls_issued"] and repo.append_calls == 1


def _first_baseline(repo, *, operation_mode="baseline", canonical_memory_version=None):
    from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan

    repo.unmapped = True
    current_records = [
        {"ref": f"evidence:{number}", "source": "web", "evidence_type": "owned_content", "url": "https://example.test", "content": f"Frozen synthetic evidence {number}", "metadata": {}}
        for number in repo.records
    ]
    repo.baseline_plan = build_vault_scan_plan(
        brand_identity="example.test", subject_url="https://example.test", mode=operation_mode,
        current_evidence_records=current_records,
        previous_capture_evidence_records=current_records if operation_mode == "incremental_refresh" else (),
        known_evidence_records=current_records if operation_mode == "incremental_refresh" else (),
        canonical_memory_version=canonical_memory_version,
    )

    def operation(scan, **_kwargs):
        return {
            "source_scan_id": scan, "status": "completed", "plan": deepcopy(repo.baseline_plan),
            "operation_plan_id": repo.context["operation_origin"]["operation_id"],
            "operation_plan_fingerprint": repo.baseline_plan["operation_plan_fingerprint"],
            "capture_id": repo.context["capture_origin"]["capture_id"],
            "capture_hash": repo.context["capture_origin"]["capture_fingerprint"],
        }

    repo.get_capture_operation_plan = operation
    return repo


class _SelectiveFlow(_Flow):
    def evaluate_component(self, request):
        self.calls.append(deepcopy(request))
        if self.fail == len(self.calls):
            return evaluation.ComponentEvaluationOutcome.provider_failure()
        # Frozen synthetic outcomes explicitly cover all requested criteria;
        # the extra input record need not be cited or forced into support.
        rows = [
            {
                "tile_id": row["tile_id"],
                "assessment_state": "ok" if row["tile_id"] == "M1" else "sin_evidencia",
                "supporting_evidence": [_identity(3)] if row["tile_id"] == "M1" else [],
            }
            for row in request["requested_tiles"]
        ]
        return evaluation.ComponentEvaluationOutcome.success(
            evaluation.build_component_evaluation(
                component_key=request["component_key"],
                series_fingerprint=request["current_series_fingerprint"],
                request_fingerprint=request["canonical_request_fingerprint"],
                status="evaluated",
                tile_results=rows[:-1] if self.malformed else rows,
            )
        )


def test_first_sv9_evaluation_uses_full_capture_with_legacy_operational_memory_and_hints_then_replays_exactly():
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository

    repo = _first_baseline(
        _ApplicationRepository(records=(3, 9)),
        operation_mode="incremental_refresh",
        canonical_memory_version=_hash(500),
    )
    repo.hint_only = True
    repo.hint_count = 114
    input_ = repo.evaluation_input("scan", "b3s")
    assert repo.baseline_plan["mode"] == "incremental_refresh"
    assert repo.baseline_plan["canonical_memory_version"] == _hash(500)
    assert len(input_["non_authoritative_hints"]) == 113
    assert {row["evidence_ref"] for row in input_["current_identity_bindings"]} == {"evidence:3", "evidence:9"}
    assert {row["evidence_ref"] for row in input_["authoritative_relations"]} == {"evidence:3"}

    flow = _SelectiveFlow()
    outcome = _baseline_application(repo, flow)

    assert outcome["status"] == "authority_established"
    assert len(flow.calls) == len(repo.checkpoint_appends) == 10
    candidate = repo.authority["accepted_candidate"]
    assert len(candidate["candidate_tile_judgments"]) == 80
    assert len(repo.candidates) == repo.append_calls == 1
    assert {
        row["evidence_ref"]
        for row in candidate["authoritative_relation_witness"]["authoritative_relations"]
    } == {"evidence:3"}
    assert {
        evidence["evidence_ref"]
        for row in candidate["candidate_tile_judgments"]
        for evidence in row["supporting_evidence"]
    } == {"evidence:3"}

    accepted = deepcopy(repo.authority)
    retry_flow = _SelectiveFlow(fail=1)
    retry = _baseline_application(repo, retry_flow)
    assert retry["status"] == "authority_retained"
    assert not retry_flow.calls and repo.append_calls == 1 and repo.authority == accepted

    repo.hint_count = 115
    changed_flow = _SelectiveFlow(fail=1)
    changed = _baseline_application(repo, changed_flow)
    assert changed["status"] != "authority_retained"
    assert not changed_flow.calls and repo.append_calls == 1 and repo.authority == accepted


@pytest.mark.parametrize("proof_fault", ["intact", "missing", "corrupt"])
def test_cleared_hints_do_not_infer_historical_hint_free_authority_replay(proof_fault):
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository

    repo = _first_baseline(
        _ApplicationRepository(records=(3, 9)),
        operation_mode="incremental_refresh",
        canonical_memory_version=_hash(500),
    )
    repo.hint_only = True
    repo.hint_count = 114
    assert _baseline_application(repo, _SelectiveFlow())["status"] == "authority_established"
    accepted = deepcopy(repo.authority)

    original_get = repo.get_evidence_vault_sv9_evaluation_checkpoint_for_request
    proof_reads = []

    def counted_get(*args, **kwargs):
        proof_reads.append((args, kwargs))
        if proof_fault == "missing":
            return None
        value = original_get(*args, **kwargs)
        if proof_fault == "corrupt" and value is not None:
            value["checkpoint_fingerprint"] = _hash(999)
        return value

    repo.get_evidence_vault_sv9_evaluation_checkpoint_for_request = counted_get
    repo.hint_only = False
    retry = _baseline_application(repo, _SelectiveFlow(fail=1))

    assert proof_reads, "cleared hints must validate persisted input provenance"
    assert repo.authority == accepted
    assert retry["status"] != "authority_retained"


@pytest.mark.parametrize("proof_fault", ["missing", "corrupt"])
def test_true_baseline_without_checkpoint_proof_requires_review(proof_fault):
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository

    repo = _first_baseline(_ApplicationRepository(records=(3, 9)))
    assert _baseline_application(repo, _SelectiveFlow())["status"] == "authority_established"
    accepted = deepcopy(repo.authority)
    if proof_fault == "missing":
        repo.checkpoint_appends.clear()
    else:
        repo.checkpoint_appends[-1]["checkpoint_fingerprint"] = _hash(999)

    retry_flow = _SelectiveFlow(fail=1)
    retry = _baseline_application(repo, retry_flow)
    assert retry["status"] != "authority_retained"
    assert not retry_flow.calls and repo.authority == accepted


def test_accepted_sv9_authority_does_not_rebootstrap_later_capture():
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository

    repo = _first_baseline(
        _ApplicationRepository(records=(3, 9)),
        operation_mode="incremental_refresh",
        canonical_memory_version=_hash(500),
    )
    assert _baseline_application(repo, _SelectiveFlow())["status"] == "authority_established"

    repo.hint_only = True
    flow = _SelectiveFlow()
    outcome = _baseline_application(repo, flow, source="scan-2")

    assert outcome["status"] == "review_required"
    assert outcome["evaluation_status"] == "review_required"
    assert "unmapped_evidence" in outcome["reason_codes"]
    assert len(flow.calls) < 10


@pytest.mark.parametrize("empty_basis", [False, True])
def test_first_baseline_assesses_unmapped_input_without_forcing_support_relations(empty_basis):
    repo = _first_baseline(_Repository(records=(3, 9)))
    repo.empty_adopted_basis = empty_basis
    flow = _SelectiveFlow()
    outcome = _run(repo, flow, current=(3, 9))

    assert outcome["status"] == "candidate_available"
    assert len(flow.calls) == 10
    assert all(
        [item["evidence_ref"] for item in row["evidence"]] == ["evidence:3", "evidence:9"]
        for request in flow.calls
        for row in request["requested_tiles"]
    )
    assert flow.calls[-1]["component_key"] == "coherencia"
    assert flow.calls[-1]["upstream_candidate_state"]
    candidate = next(iter(repo.candidates.values()))
    assert candidate["assessment"]["tile_count"] == 80
    assert candidate["assessment"]["sv9_score"] > 0
    assert len(candidate["candidate_tile_judgments"]) == 80
    assert all(
        row["supporting_evidence"] == ([_identity(3)] if row["tile_id"] == "M1" else [])
        for row in candidate["candidate_tile_judgments"]
    )
    assert {row["evidence_ref"] for row in candidate["evidence_bindings"]} == {"evidence:3", "evidence:9"}
    assert {row["evidence_ref"] for row in candidate["authoritative_relation_witness"]["authoritative_relations"]} == (set() if empty_basis else {"evidence:3"})
    assert outcome["trusted_irrelevant_evidence_count"] == 0


def _baseline_application(repo, flow, source="scan"):
    from src.services.evidence_vault_sv9_authority_application import run_evidence_vault_sv9_authority_application

    return run_evidence_vault_sv9_authority_application(
        repository=repo, flow=flow, domain_or_url="example.test", source_scan_id=source,
        current_series_contract=_series(),
    )


@pytest.mark.parametrize("empty_basis", [False, True])
def test_first_baseline_adoption_retry_publishes_and_recapture_retains_accepted_state(empty_basis):
    from src.services.evidence_vault_sv9_authority_report import project_vault_authority_publication
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository
    from web import scan_runner

    repo = _first_baseline(_ApplicationRepository(records=(3, 9)))
    repo.empty_adopted_basis = empty_basis
    first = _baseline_application(repo, _SelectiveFlow())
    assert first["status"] == "authority_established"
    accepted = deepcopy(repo.authority)
    writes = (repo.append_calls, list(repo.mutations))
    flow = _SelectiveFlow(fail=1)
    # Adoption survived, but the report was not written before the retry.
    retry = _baseline_application(repo, flow)
    publication = project_vault_authority_publication(retry, "scan")
    assert publication["action"] == "publish_current"
    assert not flow.calls and repo.authority == accepted
    assert (repo.append_calls, repo.mutations) == writes
    report = scan_runner._compose_report("scan", "https://example.test", "Example", publication["scanner_payload"])
    assert scan_runner._validate_report_sv9_assessment(report, required=True)["availability"] == "available"
    assert report["score"] == accepted["score"] > 0
    before = deepcopy(report)

    recapture = _baseline_application(repo, _SelectiveFlow(), source="scan-2")
    assert recapture["status"] == "review_required"
    assert project_vault_authority_publication(recapture, "scan-2", report)["action"] == "retain_source"
    assert repo.authority["accepted_candidate"] == accepted["accepted_candidate"]
    assert repo.authority["score"] == accepted["score"] and report == before


def test_first_baseline_replay_ignores_another_scans_review_overlay():
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository

    repo = _first_baseline(_ApplicationRepository(records=(3, 9)))
    assert _baseline_application(repo, _SelectiveFlow())["status"] == "authority_established"
    assert _baseline_application(repo, _SelectiveFlow(), source="scan-2")["status"] == "review_required"
    assert repo.authority["current_head"]["request"]["source_scan_id"] == "scan-2"
    assert repo.authority["reopen_review_overlay"]["review_state"] == "pending"
    accepted, flow = deepcopy(repo.authority), _SelectiveFlow(fail=1)

    retry = _baseline_application(repo, flow)

    assert (retry["status"], retry["reason_codes"]) == ("authority_retained", ["exact_reuse"])
    assert not flow.calls and repo.authority == accepted


@pytest.mark.parametrize("failure", ("provider", "omitted_tile", "coherencia", "interrupted", "invalid_witness", "stale_witness"))
@pytest.mark.parametrize("empty_basis", [False, True])
def test_first_baseline_failed_complete_analysis_never_adopts_or_publishes_score(failure, empty_basis):
    from src.services.evidence_vault_sv9_authority_report import project_vault_authority_publication
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository

    repo = _first_baseline(_ApplicationRepository(records=(3, 9)))
    repo.empty_adopted_basis = empty_basis
    if failure.endswith("witness"): repo.get_evidence_vault_sv9_judgment_candidate = lambda *_args, **_kwargs: (_ for _ in ()).throw((EvidenceVaultSv9AuthoritativeRelationStaleWitnessError if failure == "stale_witness" else EvidenceVaultSv9AuthoritativeRelationWitnessError)("invalid"))
    flow = _SelectiveFlow(fail=10 if failure == "coherencia" else 2 if failure == "interrupted" else 1 if failure == "provider" else None, malformed=failure == "omitted_tile")
    result = _baseline_application(repo, flow)
    assert result["status"] == "first_run_unresolved"
    if failure.endswith("witness"): assert result["evaluation_status"] == "review_required" and result["reason_codes"] == [failure.replace("_witness", "_authoritative_relation_witness")] and not flow.calls
    assert not repo.candidates and not repo.mutations and repo.authority is None
    assert project_vault_authority_publication(result, "scan")["action"] == "record_no_score"
    if failure == "interrupted":
        assert len(repo.checkpoint_appends) == 1
        resumed = _SelectiveFlow(); assert _baseline_application(repo, resumed)["status"] == "authority_established"
        assert len(resumed.calls) == 9 and len(repo.checkpoint_appends) == 10


@pytest.mark.parametrize(("review_input", "empty_basis"), [("reopen", False)])
def test_first_baseline_with_unresolved_review_input_cannot_establish_authority(review_input, empty_basis):
    from src.services.evidence_vault_sv9_authority_report import project_vault_authority_publication
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository

    repo = _first_baseline(_ApplicationRepository(records=(3, 9)))
    repo.empty_adopted_basis = empty_basis
    setattr(repo, review_input, True)
    for _ in range(2):
        result = _baseline_application(repo, _SelectiveFlow())
        assert result["status"] == "first_run_unresolved"
        assert project_vault_authority_publication(result, "scan")["action"] == "record_no_score"
        assert repo.authority is None and not repo.candidates and not repo.mutations


@pytest.mark.parametrize("invalid", ("witness", "bindings", "evaluation", "source"))
@pytest.mark.parametrize("empty_basis", [False, True])
def test_first_baseline_exact_replay_rejects_unproven_inputs(invalid, empty_basis):
    from src.services.evidence_vault_sv9_authority_report import project_vault_authority_publication
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository

    repo = _first_baseline(_ApplicationRepository(records=(3, 9)))
    repo.empty_adopted_basis = empty_basis
    assert _baseline_application(repo, _SelectiveFlow())["status"] == "authority_established"
    writes = (repo.append_calls, list(repo.mutations))
    if invalid == "witness":
        repo.witness_seed += 1
    elif invalid == "bindings":
        repo.authority["accepted_candidate"]["evidence_bindings"].pop()
    elif invalid == "evaluation":
        repo.authority["accepted_candidate"]["component_evaluations"].pop()
    else:
        getter = repo.get_capture_operation_plan
        repo.get_capture_operation_plan = lambda *args, **kwargs: getter(*args, **kwargs) | {"capture_hash": _hash(999)}
    flow = _SelectiveFlow(fail=1)
    result = _baseline_application(repo, flow)
    assert result["status"] == "authority_conflict"
    assert project_vault_authority_publication(result, "scan")["action"] == "record_no_score"
    assert not flow.calls and (repo.append_calls, repo.mutations) == writes

@pytest.mark.parametrize(("kind", "status", "reason"), [("v2", "candidate_available", "candidate_already_present"), ("legacy", "review_required", "unwitnessed_legacy_candidate"), ("stale", "review_required", "stale_authoritative_relation_witness")])
def test_complete_existing_candidate_precedes_checkpoints_and_flow(kind, status, reason):
    repo = _Repository(records=(9,)); assert _run(repo, _Flow(), current=(9,))["status"] == "candidate_available"
    stored = next(iter(repo.candidates.values()))
    if kind == "legacy":
        stored.pop("authoritative_relation_witness"); stored["schema_version"] = "evidence-vault-sv9-judgment-candidate-v1"; stored["complete_record_fingerprint"] = memory.canonical_fingerprint("evidence-vault-sv9-judgment-candidate-record-v1", {key: value for key, value in stored.items() if key not in {"id", "complete_record_fingerprint"}})
    if kind == "stale": repo.witness_seed += 1
    checkpoint_gets, candidate_gets = len(repo.checkpoint_gets), repo.get_calls; repo.checkpoints.clear(); flow = _Flow(fail=1); outcome = _run(repo, flow, current=(9,))
    assert (outcome["status"], outcome["reason_codes"], flow.calls, len(repo.checkpoint_gets), repo.get_calls, repo.append_calls) == (status, [reason], [], checkpoint_gets, candidate_gets + 1, 1)

@pytest.mark.parametrize(("current", "trusted", "reason"), [((3,), (), "exact_reuse")])
def test_exact_reuse_and_explicit_irrelevant_evidence_skip_flow(current, trusted, reason):
    repo, flow = _Repository(_authority(), current), _Flow(); result = _run(repo, flow, current=current, trusted=trusted)
    assert (result["status"], result["reason_codes"], flow.calls, repo.append_calls) == ("no_new_score", [reason], [], 0)
    assert result["accepted_authority"]["accepted_candidate_id"] == _ID and result["trusted_irrelevant_evidence_count"] == len(trusted)

def test_hint_routing_keeps_signed_unmapped_evidence_pending_review():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); repo.hint_only = True; outcome = _run(repo, flow, current=(3, 9))
    assert outcome["signed_delta"]["plan"]["component_workset"] == [] and [row["component_key"] for row in flow.calls] == ["mission", "coherencia"]
    assert outcome["status"] == "review_required" and outcome["reason_codes"] == ["unmapped_evidence"] and outcome["candidate"] is None and len(repo.checkpoint_appends) == 2 and repo.get_calls == repo.append_calls == 0
    assert outcome["unmapped_evidence_count"] == outcome["trusted_irrelevant_evidence_count"] == 0
    assert (outcome["calls_avoided"], outcome["reused_tiles"], outcome["review_tile_count"]) == (8, 69, 0)


def test_bootstrap_hint_routing_keeps_unmapped_evidence_pending_review():
    repo, flow = _Repository(records=(9,)), _Flow()
    repo.bootstrap_hint_only = True
    series = shared_process.build_core_shared_series_contract(
        interpretation_model="interpretation-test",
        labeling_model="labeling-test",
        adjudicator_model="adjudicator-test",
        evaluator_model="evaluator-test",
        reasoning_model="reasoning-test",
        editorial_model="editorial-test",
        gate_authority="veto_only",
        editorial_enabled=True,
    )
    flow.shared_analysis = {"status": "test"}

    outcome = _run(repo, flow, current=(9,), series=series)

    assert outcome["status"] == "review_required"
    assert outcome["reason_codes"] == ["unmapped_evidence"]
    assert not repo.candidates

def test_continuity_review_outcome_uses_partition_telemetry():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); repo.reopen = True; outcome = _run(repo, flow, current=(3, 9))
    assert outcome["status"] == "review_required" and outcome["reason_codes"] == ["coverage_loss", "incomplete_review_partition"]
    assert (outcome["calls_avoided"], outcome["reused_tiles"], outcome["review_tile_count"]) == (9, 68, 11)

def test_provider_failure_outcome_uses_partition_telemetry():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(fail=1); repo.hint_only = True; outcome = _run(repo, flow, current=(3, 9))
    assert outcome["status"] == "review_required" and outcome["reason_codes"] == ["unmapped_evidence", "provider_failure"]
    assert outcome["unmapped_evidence_count"] == 0
    assert (outcome["calls_avoided"], outcome["reused_tiles"], outcome["review_tile_count"]) == (8, 69, 0)

@pytest.mark.parametrize("enabled", [False, True])
def test_continue_past_failed_component_flag_keeps_outcome_and_checkpoints_healthy_component(monkeypatch, enabled):
    monkeypatch.setattr(service, "BRAND3_VAULT_SV9_CONTINUE_PAST_FAILED_COMPONENT_ENABLED", enabled)
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(fail=1); repo.hint_only, repo.hint_count = True, 6; outcome = _run(repo, flow, current=(3, 9))
    assert (outcome["status"], outcome["reason_codes"]) == ("review_required", ["unmapped_evidence", "incomplete_review_partition", "provider_failure"])
    assert [row["component_key"] for row in flow.calls] == (["mission", "vision"] if enabled else ["mission"]) and len(repo.checkpoint_appends) == int(enabled)
    assert outcome.get("failed_components") == ([{"component_key": "mission", "reason_code": "provider_failure"}] if enabled else None)

def test_resumed_partial_outcome_keeps_partition_telemetry():
    repo = _Repository(_authority(), (3, 9)); repo.hint_only = True
    first = _run(repo, _Flow(fail=2), current=(3, 9)); resumed = _run(repo, _Flow(fail=1), current=(3, 9))
    assert first["status"] == resumed["status"] == "review_required" and first["reason_codes"] == resumed["reason_codes"] == ["unmapped_evidence", "provider_failure"]
    assert first["unmapped_evidence_count"] == resumed["unmapped_evidence_count"] == 0
    assert tuple(first[key] for key in ("calls_avoided", "reused_tiles", "review_tile_count")) == (8, 69, 0)
    assert tuple(resumed[key] for key in ("calls_avoided", "reused_tiles", "review_tile_count")) == (8, 69, 0)

def test_failed_second_call_persists_first_and_resume_starts_at_second():
    repo, first = _Repository(records=(9,)), _Flow(fail=2); outcome = _run(repo, first, current=(9,))
    assert (outcome["status"], outcome["candidate"], repo.get_calls, repo.append_calls, len(repo.checkpoint_appends)) == ("no_new_score", None, 1, 0, 1)
    resumed = _Flow(fail=1); rerun = _run(repo, resumed, current=(9,))
    assert rerun["reason_codes"] == ["provider_failure"] and len(resumed.calls) == 1 and resumed.calls[0]["canonical_request_fingerprint"] == first.calls[1]["canonical_request_fingerprint"]

def test_tampered_evaluation_input_stops_before_flow_or_write():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow()
    original = repo.evaluation_input("scan", "b3s")
    original["authoritative_relations"][0]["evidence_ref"] = "tampered"
    repo.evaluation_input = lambda *_args: original
    result = _run(repo, flow, current=(3, 9))
    assert result["status"] == "review_required"
    assert result["reason_codes"] == ["invalid_evaluation_input"]
    assert not flow.calls and repo.append_calls == 0


def test_checkpoint_persistence_failure_stops_before_second_flow_call():
    repo, flow = _Repository(records=(9,)), _Flow(); repo.fail_checkpoint_append = True; outcome = _run(repo, flow, current=(9,))
    assert outcome["reason_codes"] == ["repository_failure"] and len(flow.calls) == len(repo.checkpoint_appends) == 1 and repo.get_calls == 1 and repo.append_calls == 0


def test_coherencia_request_matches_uninterrupted_cached_upstream_state():
    clean = _Flow(); _run(_Repository(records=(9,)), clean, current=(9,)); uninterrupted = next(row for row in clean.calls if row["component_key"] == "coherencia")
    repo, failed = _Repository(records=(9,)), _Flow(fail=10); _run(repo, failed, current=(9,)); resumed = _Flow(); _run(repo, resumed, current=(9,))
    assert len(repo.checkpoint_appends) == 10 and [row["component_key"] for row in resumed.calls] == ["coherencia"]
    assert resumed.calls[0]["canonical_request_fingerprint"] == uninterrupted["canonical_request_fingerprint"] and resumed.calls[0]["upstream_candidate_state"] == uninterrupted["upstream_candidate_state"]


def test_review_pending_allows_healthy_flow_but_no_candidate_side_effects():
    repo, flow = _Repository(records=(3, 9)), _Flow(); repo.unmapped = True; outcome = _run(repo, flow, current=(3, 9))
    assert outcome["status"] == "review_required" and outcome["reason_codes"] == ["unmapped_evidence", "incomplete_review_partition"] and flow.calls and repo.get_calls == repo.append_calls == 0
    assert outcome["unmapped_evidence_count"] == len(outcome["workset_partition"]["pending_evidence"]) == 1
    assert outcome["candidate"] is None and not repo.candidates and not ({"assessment", "score", "adoption", "publication"} & set(outcome))


def test_processing_complete_unmapped_evidence_can_produce_candidate_without_inflating_trusted_count():
    repo, flow = _Repository(records=(3, 9)), _Flow()
    repo.unmapped = True
    repo.processing_complete = (9,)

    assert (
        repo.evaluation_input("scan", "b3s")["schema_version"]
        == "evidence-vault-sv9-evaluation-input-v4"
    )
    outcome = _run(repo, flow, current=(3, 9))

    assert outcome["status"] == "candidate_available"
    assert outcome["reason_codes"] == []
    assert outcome["unmapped_evidence_count"] == 0
    assert outcome["trusted_irrelevant_evidence_count"] == 0


def test_partition_reasons_keeps_routing_only_unmapped_evidence_pending_review():
    evidence = _identity(9)
    partition = {
        "review_partition": {
            "operational_authority_coverage_loss_tile_ids": [],
            "judgment_delta_coverage_loss_tile_ids": [],
            "evaluation_input_reopened_tile_ids": [],
            "planner_review_tile_ids": [],
            "coherencia_blocked_tile_ids": [],
        },
        "judgment_delta": {"unmapped_evidence": [evidence]},
        "trusted_irrelevant_evidence": [],
        "processing_complete_evidence": [],
        # A routing-only hint can remove a row from the execution workset; it
        # cannot certify the signed unmapped evidence as complete.
        "pending_evidence": [],
    }

    assert service._partition_reasons(partition, ["unmapped_evidence"]) == [
        "unmapped_evidence"
    ]


def test_partition_reasons_allows_explicit_processing_complete_unmapped_evidence():
    evidence = _identity(9)
    partition = {
        "review_partition": {
            "operational_authority_coverage_loss_tile_ids": [],
            "judgment_delta_coverage_loss_tile_ids": [],
            "evaluation_input_reopened_tile_ids": [],
            "planner_review_tile_ids": [],
            "coherencia_blocked_tile_ids": [],
        },
        "judgment_delta": {"unmapped_evidence": [evidence]},
        "trusted_irrelevant_evidence": [],
        "processing_complete_evidence": [evidence],
        "pending_evidence": [],
    }

    assert service._partition_reasons(partition, ["unmapped_evidence"]) == []


def test_bootstrap_reopen_coverage_loss_uses_partition_reasons_without_candidate_io():
    repo, flow = _Repository(records=(3, 9)), _Flow(); repo.reopen = True; outcome = _run(repo, flow, current=(3, 9))
    value = service.partitioning.build_evidence_vault_sv9_workset_partition(evaluation_input=repo.evaluation_input("scan", "b3s"), judgment_delta=outcome["signed_delta"], trusted_irrelevant_evidence=[])["review_partition"]
    assert value["evaluation_input_reopened_tile_ids"] == value["operational_authority_coverage_loss_tile_ids"] == ["M1"] and not value["judgment_delta_coverage_loss_tile_ids"]
    partition = outcome["workset_partition"]
    assert outcome["status"] == "review_required" and "coverage_loss" in outcome["reason_codes"] and "incomplete_candidate" not in outcome["reason_codes"] and not flow.calls and repo.get_calls == repo.append_calls == 0
    assert partition["review_partition"] == value and partition["judgment_delta"] == outcome["signed_delta"] and outcome["candidate"] is None
    assert not ({"assessment", "score", "adoption", "publication"} & set(outcome))


@pytest.mark.parametrize(
    ("overlay_scan", "status"),
    (("scan-3", "candidate_available"), ("scan-4", "review_required")),
)
def test_pending_review_overlay_holds_only_its_own_scan(monkeypatch, overlay_scan, status):
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository
    from tests.test_evidence_vault_sv9_authority_application import _run as _apply

    # These flows read the fake repository's own relation facts.
    monkeypatch.setattr(
        service,
        "project_evidence_vault_sv9_evaluation_input",
        project_evidence_vault_sv9_evaluation_input,
    )
    repo = _ApplicationRepository(records=(9,))
    assert _apply(repo, _Flow())["status"] == "authority_established"
    assert _apply(repo, _Flow(), current=(3, 9), source="scan-2")["status"] == "authority_advanced"

    def evaluate(scan, current):
        repo.records = current
        repo.projection_relations = [_relation(repo, "M1", number=number) for number in current]
        return service.run_evidence_vault_sv9_authority_evaluation(
            repository=repo,
            flow=_Flow(),
            domain_or_url="example.test",
            source_scan_id=scan,
            current_series_contract=_series(),
        )

    review = evaluate("scan-3", (9,))
    assert review["status"] == "review_required"
    predecessor = repo.authority["current_head"]["event_fingerprint"]
    request = authority_event.build_evidence_vault_sv9_authority_request(
        action="reopen_authority",
        candidate_id=None,
        expected_predecessor_event_fingerprint=predecessor,
        delta_fingerprint=review["signed_delta"]["canonical_delta_fingerprint"],
        source_scan_id=overlay_scan,
        workset_partition_fingerprint=review["workset_partition"]["partition_fingerprint"],
    )
    # The cases differ only in which scan owns the pending overlay.
    repo.reopen_evidence_vault_sv9_judgment_authority(
        overlay_scan,
        review["signed_delta"],
        expected_predecessor_event_fingerprint=predecessor,
        idempotency_key_hash=authority_event.authority_application_idempotency_fingerprint(request),
        workset_partition=review["workset_partition"],
    )
    assert repo.authority["reopen_review_overlay"]["review_state"] == "pending"

    outcome = evaluate("scan-4", (3, 7, 9))

    own_overlay = overlay_scan == "scan-4"
    assert outcome["status"] == status
    assert ("active_review_overlay" in outcome["reason_codes"]) is own_overlay
    assert (outcome["candidate"] is None) is own_overlay


@pytest.mark.parametrize(
    ("historical", "current", "expected"),
    [
        ([(109, 209)], [], set()),
        ([(109, 209)], [(109, 209)], set()),
        ([(109, 209)], [(998, 999)], {"M1"}),
        ([], [], {"M1"}),
        ([(109, 209), (109, 209)], [], {"M1"}),
        ([(109, 209)], [(109, 209), (109, 209)], {"M1"}),
    ],
)
def test_accepted_support_identity_check_distinguishes_absence_from_mismatch(
    historical, current, expected
):
    pair = _identity(9)

    class IdentityRepository:
        def load_evidence_vault_sv9_authoritative_relation_facts(
            self, _scan, *, workspace_slug
        ):
            assert workspace_slug == "b3s"
            return {
                "evidence": [
                    {
                        **pair,
                        "evidence_id": _hash(evidence_id),
                        "source_identity_id": _hash(source_id),
                    }
                    for evidence_id, source_id in historical
                ]
            }

    bindings = [
        {
            **pair,
            "evidence_id": _hash(evidence_id),
            "source_identity_id": _hash(source_id),
        }
        for evidence_id, source_id in current
    ]
    result = service._accepted_support_identity_mismatches(
        IdentityRepository(),
        {"accepted_candidate": {"source_scan_id": "accepted-scan"}},
        [{"tile_id": "M1", "supporting_evidence": [pair]}],
        bindings,
        records={},
        source_scan_id="current-scan",
        workspace_slug="b3s",
    )

    assert result == expected

def test_identity_mismatch_stays_fail_closed_before_hint_routing():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow()
    repo.hint_only = True
    load_historical = repo.load_evidence_vault_sv9_authoritative_relation_facts

    def mismatched_historical(*args, **kwargs):
        facts = load_historical(*args, **kwargs)
        facts["evidence"][0]["evidence_id"] = _hash(998)
        return facts

    repo.load_evidence_vault_sv9_authoritative_relation_facts = mismatched_historical
    outcome = _run(repo, flow, current=(3, 9))

    # Neither the delta nor the partition names a reopen cause, and an unmapped hint cannot mint one.
    assert outcome["status"] == "no_new_score"
    assert outcome["reason_codes"] == ["unmapped_evidence", "coverage_loss"]
    assert not flow.calls and repo.get_calls == repo.append_calls == 0


def test_contradictory_checkpoint_fails_closed_before_flow():
    repo, flow = _Repository(records=(9,)), _Flow(); repo.checkpoint_conflict = True; outcome = _run(repo, flow, current=(9,))
    assert outcome["reason_codes"] == ["repository_failure"] and not flow.calls and repo.get_calls == 1 and repo.append_calls == 0


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

def test_partial_outcome_never_exposes_or_persists_candidate_state():
    repo, flow = _Repository(records=(9,)), _Flow(fail=2); outcome = _run(repo, flow, current=(9,))
    assert outcome["candidate"] is None and not repo.candidates and repo.get_calls == 1 and repo.append_calls == 0
    assert not ({"assessment", "score", "adoption", "publication"} & set(outcome)) and outcome["reason_codes"] == ["provider_failure"]

@pytest.mark.parametrize("kwargs", [{"fail": 2}, {"malformed": True}])
def test_provider_or_malformed_partial_results_are_atomic(kwargs):
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(**kwargs); result = _run(repo, flow, current=(3, 9))
    assert result["status"] == "no_new_score" and result["reason_codes"] == ["provider_failure"] and repo.append_calls == 0

def test_contract_rollover_persists_pending_candidate_but_keeps_authority_unchanged():
    authority = _authority(); before = deepcopy(authority); repo, flow = _Repository(authority, (3,)), _Flow(); result = _run(repo, flow, series=_series(prompt_version="prompt-v2"))
    assert result["status"] == "review_required" and result["reason_codes"] == ["series_rollover"] and len(flow.calls) == len(repo.checkpoint_appends) == 10 and repo.append_calls == 0 and authority == before

def test_tampered_recovered_evaluation_is_rejected_without_candidate_publication():
    repo, first = _Repository(records=(9,)), _Flow(fail=2); _run(repo, first, current=(9,)); repo.checkpoints[next(iter(repo.checkpoints))]["request_fingerprint"] = _hash(999); candidate_gets = repo.get_calls
    flow = _Flow(); outcome = _run(repo, flow, current=(9,))
    assert outcome["reason_codes"] == ["provider_failure"] and not flow.calls and repo.get_calls == candidate_gets + 1 and repo.append_calls == 0 and outcome["candidate"] is None

def test_invalid_trusted_identity_and_replay_mismatches_fail_closed(monkeypatch):
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); invalid = _run(repo, flow, current=(3, 9), trusted=(99,))
    assert invalid["status"] == "no_new_score" and invalid["reason_codes"] == ["invalid_input"] and not flow.calls and repo.append_calls == 0
    repo, flow = _Repository(None, (9,), bad_reload=True), _Flow(); mismatch = _run(repo, flow, current=(9,))
    assert mismatch["status"] == "no_new_score" and mismatch["reason_codes"] == ["invalid_replay"] and repo.append_calls == 1
    repo, flow = _Repository(None, (9,)), _Flow(); monkeypatch.setattr(service.evaluation, "replay_incremental_evaluations", lambda *_: {"status": "pending"})
    preappend = _run(repo, flow, current=(9,)); assert preappend["status"] == "no_new_score" and preappend["reason_codes"] == ["incomplete_candidate"] and repo.append_calls == 0

@pytest.mark.parametrize(("name", "missing"), [(name, missing) for name in ("active_authority_event", "current_head", "event") for missing in (False, True)])
def test_invalid_persisted_event_audit_metadata_stops_all_evaluation_effects(name, missing):
    repo, flow = _Repository(_authority(), (9,)), _Flow()
    repo.authority[name].pop("created_at") if missing else repo.authority[name].__setitem__("created_at", None)
    result = _run(repo, flow, current=(9,))
    assert (result["status"], result["reason_codes"], result["accepted_authority"], result["candidate"], result["signed_delta"]) == ("no_new_score", ["invalid_input"], None, None, None)
    assert (repo.authority_calls, repo.projection_calls, repo.context_calls, repo.evidence_calls, repo.get_calls, repo.append_calls, flow.calls) == (1, 0, 0, 0, 0, 0, [])
# fmt: on


_HOME, _ABOUT, _WORK, _WWW_ABOUT = "https://example.test", "https://example.test/about", "https://example.test/work", "https://www.example.test/about"
_EXA = "https://press.example/2026/top-design-studios"
_EXA_ROW = {"url": _EXA, "source": "exa", "evidence_type": "external_proof.external_mentions", "source_class": "external_proof"}
# (accepted, current) about-chunk overrides: the brand host lost its ``www.``, or the scheme drifted.
_WWW_ALIAS, _SCHEME_DRIFT = ({"url": _WWW_ABOUT}, {}), ({"url": "http://example.test/about"}, {})
_ABOUT_TEXT = "Primary is an independent design studio " + " ".join(f"a{index}" for index in range(30))
_MENTION_TEXT = "Primary was named a top design studio by the trade press this spring."


def _capture_row(record, ref, content, *, url, source="web", evidence_type="raw_input", source_class="owned_copy"):
    """One stored capture row whose canonical ids come from the production projection, never hand-built."""
    stored = {"evidence_ref": ref, "source": source, "evidence_type": evidence_type, "url": url, "content": content, "content_raw": None, "confidence": "high", "metadata": {}, "source_class": source_class}
    identity = project_evidence_memory_row_identity(history._capture_evidence_rows([stored])[0], brand_domain="example.test")
    return {
        "record": record,
        "ref": ref,
        "content": content,
        # The stored content hash, not the identity's casefolded content digest.
        "evidence_fingerprint": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "evidence_id": identity["evidence_id"],
        "source_identity_id": identity["document_id"],
        **{key: identity[key] for key in ("source_class", "source", "evidence_type", "url")},
    }


def _pair(row):
    return {"evidence_ref": row["ref"], "evidence_fingerprint": row["evidence_fingerprint"]}


def _primary_repository(*, truncated, repository=_Repository, about=({}, {})):
    """Primary's re-scan shape: a moved Exa ref, a re-chunked /work page and a truncated homepage.

    ``about`` overrides the stored fields of the unchanged about chunk on the accepted and the
    current capture; its ref and fingerprint survive whatever identities those fields derive.
    """
    home = " ".join(f"h{index}" for index in range(340))
    work = [f"k{index}" for index in range(120)]
    historical_about, current_about = ({"url": _ABOUT} | overrides for overrides in about)
    historical = [
        _capture_row(1, "raw_inputs.0.chunk.0", home, url=_HOME),
        _capture_row(2, "raw_inputs.1.chunk.0", _ABOUT_TEXT, **historical_about),
        _capture_row(3, "raw_inputs.3.subpage.2.chunk.0", " ".join(work[:40]), url=_WORK),
        _capture_row(4, "raw_inputs.3.subpage.2.chunk.1", " ".join(work[40:80]), url=_WORK),
        _capture_row(5, "raw_inputs.3.subpage.2.chunk.2", " ".join(work[80:]), url=_WORK),
        _capture_row(6, "raw_inputs.5.exa.mentions.7", _MENTION_TEXT, **_EXA_ROW),
    ]
    current = [
        _capture_row(11, "raw_inputs.0.chunk.0", " ".join(home.split()[:67]) if truncated else home, url=_HOME),
        _capture_row(12, "raw_inputs.1.chunk.0", _ABOUT_TEXT, **current_about),
        _capture_row(13, "raw_inputs.4.subpage.1.chunk.0", " ".join(work[:70]), url=_WORK),
        _capture_row(14, "raw_inputs.4.subpage.1.chunk.1", " ".join(work[70:]) + " new closing line", url=_WORK),
        _capture_row(16, "raw_inputs.6.exa.mentions.2", _MENTION_TEXT, **_EXA_ROW),
    ]
    support = {"M1": [_pair(historical[0])], "M2": [_pair(historical[5])], "A1": [_pair(historical[3])]}
    support |= {tile: [_pair(historical[1])] for tile, _component in planner._REGISTRY if tile not in support}
    repo = repository(_authority(support=support))
    repo.captures = {"scan": historical, "scan-2": current}
    return repo


class _DroppingFlow(_Flow):
    def __init__(self, *, tile, drop):
        super().__init__()
        self.tile, self.drop = tile, drop

    def evaluate_component(self, request):
        self.calls.append(request)
        rows = [
            {
                "tile_id": row["tile_id"],
                "assessment_state": "ok" if row["evidence"] else "sin_evidencia",
                "supporting_evidence": [
                    {key: item[key] for key in ("evidence_ref", "evidence_fingerprint")}
                    for item in row["evidence"]
                    if not (row["tile_id"] == self.tile and item["evidence_ref"] == self.drop)
                ],
            }
            for row in request["requested_tiles"]
        ]
        return evaluation.ComponentEvaluationOutcome.success(
            evaluation.build_component_evaluation(
                component_key=request["component_key"],
                series_fingerprint=request["current_series_fingerprint"],
                request_fingerprint=request["canonical_request_fingerprint"],
                status="evaluated",
                tile_results=rows,
            )
        )


@pytest.mark.parametrize("www", [False, True])
def test_next_scan_carries_moved_and_rechunked_support_and_reviews_only_the_truncated_page(www):
    repo, flow = _primary_repository(truncated=True, about=_WWW_ALIAS if www else ({}, {})), _Flow()

    outcome = _run(repo, flow, scan="scan-2")

    signed = outcome["signed_delta"]
    assert outcome["status"] == "review_required" and "coverage_loss" in outcome["reason_codes"]
    assert [row["tile_id"] for row in signed["coverage_loss"]] == ["M1"]
    carried = {row["tile_id"]: row for row in signed["support_continuity"]["carried"]}
    assert {tile: row["tier"] for tile, row in carried.items()} == {"M2": "canonical", "A1": "owned_page_similarity"}
    current = {row["ref"]: row for row in repo.captures["scan-2"]}
    assert carried["M2"]["targets"] == [_pair(current["raw_inputs.6.exa.mentions.2"])]
    assert carried["A1"]["targets"] == [_pair(current["raw_inputs.4.subpage.1.chunk.0"]), _pair(current["raw_inputs.4.subpage.1.chunk.1"])]
    items = {row["tile_id"]: row for row in signed["plan"]["items"]}
    assert {"M2", "A1"} <= set(signed["plan"]["tile_workset"])
    assert items["M2"]["evidence"] == carried["M2"]["targets"] and items["A1"]["evidence"] == carried["A1"]["targets"]
    targets = {(pair["evidence_ref"], pair["evidence_fingerprint"]) for row in carried.values() for pair in row["targets"]}
    unmapped = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in signed["unmapped_evidence"]}
    assert not targets & unmapped and [row["evidence_ref"] for row in signed["unmapped_evidence"]] == ["raw_inputs.0.chunk.0"]
    partition = outcome["workset_partition"]
    healthy = {row["tile_id"] for row in partition["healthy_workset"]["tiles"]}
    assert {"M2", "A1"} <= healthy and partition["review_partition"]["judgment_delta_coverage_loss_tile_ids"] == ["M1"]
    requested = {(row["tile_id"], item["evidence_ref"]) for call in flow.calls for row in call["requested_tiles"] for item in row["evidence"]}
    assert {("M2", "raw_inputs.6.exa.mentions.2"), ("A1", "raw_inputs.4.subpage.1.chunk.0"), ("A1", "raw_inputs.4.subpage.1.chunk.1")} <= requested
    assert not any(ref.startswith(("raw_inputs.3.", "raw_inputs.5.")) for _tile, ref in requested)
    assert outcome["candidate"] is None and repo.append_calls == 0


@pytest.mark.parametrize("www", [False, True])
def test_next_scan_without_truncation_produces_a_candidate_citing_only_current_records(www):
    repo, flow = _primary_repository(truncated=False, about=_WWW_ALIAS if www else ({}, {})), _Flow()

    outcome = _run(repo, flow, scan="scan-2")

    assert outcome["status"] == "candidate_available" and outcome["reason_codes"] == []
    # Only an alias needs the current facts join, and it loads them once.
    assert repo.facts_loads.count("scan-2") == int(www)
    candidate = next(iter(repo.candidates.values()))
    current_pairs = {(row["ref"], row["evidence_fingerprint"]) for row in repo.captures["scan-2"]}
    cited = {(item["evidence_ref"], item["evidence_fingerprint"]) for row in candidate["candidate_tile_judgments"] for item in row["supporting_evidence"]}
    assert cited and cited <= current_pairs
    assert {(row["evidence_ref"], row["evidence_fingerprint"]) for row in candidate["evidence_bindings"]} <= current_pairs
    judgments = {row["tile_id"]: row for row in candidate["candidate_tile_judgments"]}
    assert [item["evidence_ref"] for item in judgments["A1"]["supporting_evidence"]] == ["raw_inputs.4.subpage.1.chunk.0", "raw_inputs.4.subpage.1.chunk.1"]
    assert [item["evidence_ref"] for item in judgments["M2"]["supporting_evidence"]] == ["raw_inputs.6.exa.mentions.2"]
    assert len(candidate["candidate_tile_judgments"]) == 80


def test_carried_tile_dropping_a_witnessed_pair_reviews_instead_of_publishing_a_candidate():
    kept = _primary_repository(truncated=False)
    kept.operational_basis = {"A1": ["raw_inputs.1.chunk.0"]}
    assert _run(kept, _Flow(), scan="scan-2")["status"] == "candidate_available"
    repo = _primary_repository(truncated=False)
    repo.operational_basis = {"A1": ["raw_inputs.1.chunk.0"]}
    flow = _DroppingFlow(tile="A1", drop="raw_inputs.1.chunk.0")

    outcome = _run(repo, flow, scan="scan-2")

    assert outcome["status"] == "review_required" and "coverage_loss" in outcome["reason_codes"]
    assert flow.calls and outcome["candidate"] is None and repo.append_calls == 0 and not repo.candidates
    signed, partition = outcome["signed_delta"], outcome["workset_partition"]
    historical = {row["ref"]: row for row in repo.captures["scan"]}
    # The dropped tile is no longer carried, so its accepted pair is real coverage loss the reopen can cite.
    assert [(row["tile_id"], row["missing_evidence"]) for row in signed["coverage_loss"]] == [("A1", [_pair(historical["raw_inputs.3.subpage.2.chunk.1"])])]
    assert [row["tile_id"] for row in signed["support_continuity"]["carried"]] == ["M2"]
    assert partition["review_partition"]["judgment_delta_coverage_loss_tile_ids"] == ["A1"] and partition["judgment_delta"] == signed
    assert history._sv9_authority_partition_reopen_tile_ids(partition, signed) == set()


class _UnavailableHistoryRepository(_Repository):
    def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, **kwargs):
        if scan == "scan":
            raise RuntimeError("history unavailable")
        return super().load_evidence_vault_sv9_authoritative_relation_facts(scan, **kwargs)


def test_history_load_failure_during_carry_is_a_repository_failure_not_a_review():
    repo, flow = _primary_repository(truncated=False, repository=_UnavailableHistoryRepository), _Flow()

    outcome = _run(repo, flow, scan="scan-2")

    assert (outcome["status"], outcome["reason_codes"], outcome["signed_delta"]) == ("no_new_score", ["repository_failure"], None)
    assert not flow.calls and repo.append_calls == 0 and not repo.candidates


def test_malformed_historical_row_carries_nothing_and_stays_coverage_loss():
    repo, flow = _primary_repository(truncated=False), _Flow()
    next(row for row in repo.captures["scan"] if row["ref"] == "raw_inputs.5.exa.mentions.7")["source_class"] = 123

    outcome = _run(repo, flow, scan="scan-2")

    signed = outcome["signed_delta"]
    assert outcome["status"] == "review_required" and "coverage_loss" in outcome["reason_codes"]
    assert signed["schema_version"] == delta.JUDGMENT_DELTA_VERSION and "support_continuity" not in signed
    assert {row["tile_id"] for row in signed["coverage_loss"]} == {"M2", "A1"} and repo.append_calls == 0


def test_no_carry_delta_stays_v2_while_a_carried_delta_is_v4():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); repo.reopen = True
    plain = _run(repo, flow, current=(3, 9))["signed_delta"]
    assert plain["schema_version"] == delta.JUDGMENT_DELTA_VERSION == "evidence-vault-sv9-judgment-delta-v2" and "support_continuity" not in plain
    carried = _run(_primary_repository(truncated=True), _Flow(), scan="scan-2")["signed_delta"]
    assert carried["schema_version"] == delta.SUPPORT_CONTINUITY_JUDGMENT_DELTA_VERSION == "evidence-vault-sv9-judgment-delta-v4" and carried["support_continuity"]["carried"]


def test_identity_mismatch_with_a_signed_cause_returns_a_persistable_review():
    repo, flow = _primary_repository(truncated=True, about=_SCHEME_DRIFT), _Flow()

    outcome = _run(repo, flow, scan="scan-2")

    signed, partition = outcome["signed_delta"], outcome["workset_partition"]
    assert outcome["status"] == "review_required"
    assert outcome["reason_codes"] == ["coverage_loss", "unmapped_evidence", "incomplete_review_partition"]
    assert outcome["unmapped_evidence_count"] == len(partition["pending_evidence"])
    assert partition["judgment_delta"] == signed and [row["tile_id"] for row in signed["coverage_loss"]] == ["M1"]
    # The truncated homepage is the signed cause the repository accepts for the reopen.
    assert history._sv9_authority_partition_reopen_tile_ids(partition, signed) == set()
    assert not flow.calls and repo.get_calls == repo.append_calls == 0
    assert repo.checkpoint_gets == [] and repo.checkpoint_appends == []


def test_identity_mismatch_without_a_signed_cause_keeps_the_accepted_authority(monkeypatch):
    repo, flow = _primary_repository(truncated=False, about=_SCHEME_DRIFT), _Flow()
    built, validated_partition = [], service._validated_partition

    def spy(*args, **kwargs):
        built.append(validated_partition(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(service, "_validated_partition", spy)

    outcome = _run(repo, flow, scan="scan-2")

    assert (outcome["status"], outcome["reason_codes"]) == ("no_new_score", ["coverage_loss"])
    assert "workset_partition" not in outcome
    assert not flow.calls and repo.get_calls == repo.append_calls == 0
    assert repo.checkpoint_gets == [] and repo.checkpoint_appends == []
    # The service declines exactly the reopen the repository would refuse for the same delta and partition.
    assert len(built) == 1
    with pytest.raises(history.EvidenceVaultSv9JudgmentCandidateError):
        history._sv9_authority_partition_reopen_tile_ids(built[0], built[0]["judgment_delta"])


def test_identity_mismatch_partition_failure_is_invalid_input_before_any_provider_call(monkeypatch):
    repo, flow = _primary_repository(truncated=True, about=_SCHEME_DRIFT), _Flow()
    monkeypatch.setattr(service, "_validated_partition", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("partition")))

    outcome = _run(repo, flow, scan="scan-2")

    assert (outcome["status"], outcome["reason_codes"]) == ("no_new_score", ["invalid_input"])
    assert not flow.calls and repo.get_calls == repo.append_calls == 0


@pytest.mark.parametrize(
    ("about", "tampered"),
    [
        pytest.param(_SCHEME_DRIFT, None, id="scheme-drift"),
        pytest.param(({"url": "https://www.example.org/about"}, {"url": "https://example.org/about"}), None, id="foreign-www-host"),
        pytest.param(({"url": _WWW_ABOUT, "evidence_type": "owned_content"}, {}), None, id="evidence-type-change"),
        pytest.param(({"url": _WWW_ABOUT, "source_class": "external_proof"}, {"source_class": "external_proof"}), None, id="external-proof"),
        pytest.param(_WWW_ALIAS, "evidence_id", id="tampered-evidence-id"),
        pytest.param(_WWW_ALIAS, "source_identity_id", id="tampered-source-identity-id"),
    ],
)
def test_non_alias_identity_drift_stays_a_mismatch_without_reading_current_facts(about, tampered):
    repo, flow = _primary_repository(truncated=False, about=about), _Flow()
    if tampered:
        repo.captures["scan"][1][tampered] = _hash(998)

    outcome = _run(repo, flow, scan="scan-2")

    assert (outcome["status"], outcome["reason_codes"]) == ("no_new_score", ["coverage_loss"])
    assert not flow.calls and repo.append_calls == 0
    assert "scan-2" not in repo.facts_loads


@pytest.mark.parametrize(
    ("about", "current_drift"),
    [
        pytest.param(({"url": _WWW_ABOUT, "source": "context"}, {}), {}, id="source-change"),
        pytest.param(_WWW_ALIAS, {"evidence_id": _hash(998)}, id="current-evidence-id"),
        pytest.param(_WWW_ALIAS, {"evidence_record_id": _uuid(998)}, id="current-record-id"),
        pytest.param(_WWW_ALIAS, {"canonical_domain": "other.test"}, id="current-domain"),
    ],
)
def test_www_alias_without_a_matching_current_facts_row_stays_a_mismatch(about, current_drift):
    repo, flow = _primary_repository(truncated=False, about=about), _Flow()
    load = repo.load_evidence_vault_sv9_authoritative_relation_facts

    def drifted(scan, **kwargs):
        facts = load(scan, **kwargs)
        if scan == "scan-2":
            next(row for row in facts["evidence"] if row["evidence_ref"] == "raw_inputs.1.chunk.0").update(current_drift)
        return facts

    repo.load_evidence_vault_sv9_authoritative_relation_facts = drifted
    outcome = _run(repo, flow, scan="scan-2")

    assert (outcome["status"], outcome["reason_codes"]) == ("no_new_score", ["coverage_loss"])
    assert repo.facts_loads.count("scan-2") == 1
    assert not flow.calls and repo.append_calls == 0


def test_www_alias_current_facts_load_failure_is_a_repository_failure():
    repo, flow = _primary_repository(truncated=False, about=_WWW_ALIAS), _Flow()
    load = repo.load_evidence_vault_sv9_authoritative_relation_facts

    def current_unavailable(scan, **kwargs):
        if scan == "scan-2":
            repo.facts_loads.append(scan)
            raise RuntimeError("current facts unavailable")
        return load(scan, **kwargs)

    repo.load_evidence_vault_sv9_authoritative_relation_facts = current_unavailable
    outcome = _run(repo, flow, scan="scan-2")

    assert (outcome["status"], outcome["reason_codes"]) == ("no_new_score", ["repository_failure"])
    assert repo.facts_loads.count("scan-2") == 1 and outcome["signed_delta"] is not None
    assert not flow.calls and repo.append_calls == 0 and not repo.candidates


def test_historical_identity_load_failure_is_a_repository_failure():
    repo, flow = _primary_repository(truncated=False), _Flow()
    load = repo.load_evidence_vault_sv9_authoritative_relation_facts

    def second_history_load_fails(scan, **kwargs):
        # The support-continuity carry reads history first; the identity gate reads it again.
        if scan == "scan" and "scan" in repo.facts_loads:
            raise RuntimeError("history unavailable")
        return load(scan, **kwargs)

    repo.load_evidence_vault_sv9_authoritative_relation_facts = second_history_load_fails
    outcome = _run(repo, flow, scan="scan-2")

    assert (outcome["status"], outcome["reason_codes"]) == ("no_new_score", ["repository_failure"])
    # Unlike a carry-path failure, the signed delta already exists.
    assert repo.facts_loads == ["scan"] and outcome["signed_delta"] is not None
    assert not flow.calls and repo.append_calls == 0 and not repo.candidates


@pytest.mark.parametrize("error", [KeyError("evidence"), ValueError("current facts are invalid")])
def test_current_facts_contract_gap_keeps_only_the_alias_pair_a_mismatch(error):
    repo = _primary_repository(truncated=False, about=_WWW_ALIAS)
    prior = repo.authority["accepted_partition"]["candidate_tile_judgments"]
    bindings = repo.evaluation_input("scan-2", "b3s")["current_identity_bindings"]
    records = {(row["ref"], row["evidence_fingerprint"]): {"content": row["content"]} for row in repo.captures["scan-2"]}

    def mismatches():
        return service._accepted_support_identity_mismatches(repo, repo.authority, prior, bindings, records=records, source_scan_id="scan-2", workspace_slug="b3s")

    assert mismatches() == set()
    load = repo.load_evidence_vault_sv9_authoritative_relation_facts

    def current_gap(scan, **kwargs):
        if scan == "scan-2":
            raise error
        return load(scan, **kwargs)

    repo.load_evidence_vault_sv9_authoritative_relation_facts = current_gap
    # Only the tiles resting on the about chunk stay unverified; a catch-all would also flag M1, M2 and A1.
    assert mismatches() == {tile for tile, _component in planner._REGISTRY} - {"M1", "M2", "A1"}


def test_facts_row_provenance_keys_leave_evaluation_input_fingerprints_unchanged():
    plain = _facts()
    extended = deepcopy(plain)
    for row in extended["evidence"]:
        row.update(source="web", evidence_type="raw_input", url="https://brand.test/")

    before, after = (project_evidence_vault_sv9_evaluation_input(repository=_FactsRepository(facts), source_scan_id="scan-1") for facts in (plain, extended))

    assert before["status"] == "available" and before["authoritative_relations"]
    assert (after["relation_projection_fingerprint"], after["evaluation_input_fingerprint"]) == (before["relation_projection_fingerprint"], before["evaluation_input_fingerprint"])
    assert after == before


def _scoped_record(ref, content):
    from src.sv9_flow.contracts import EvidenceRecord

    return EvidenceRecord(ref=ref, source="web", evidence_type="raw_input", content=content, url="https://brand.test", metadata={"source_class": "owned_copy"})


# A re-scan's shape: Flow shows Core 8 mission refs, while M1 is routed 9 other rows.
_FLOW_ROWS = [_scoped_record(f"raw_inputs.0.chunk.{index}", f"Flow chunk {index} restates the mission for teams.") for index in range(1, 9)]
_ROUTED_ROWS = [_scoped_record(f"raw_inputs.1.subpage.{index}.chunk.1", f"Routed proof {index}: we help finance team {index} close the books faster.") for index in range(1, 10)]


def _scoped_candidate():
    from src.sv9_flow.contracts import BrandEvidencePack, BrandInterpretation, Sv9FlowCandidate

    refs = [row.ref for row in _FLOW_ROWS]
    return Sv9FlowCandidate(
        evidence_pack=BrandEvidencePack(brand_name="Brand", url="https://brand.test", evidence=[*_FLOW_ROWS, *_ROUTED_ROWS]),
        interpretation=BrandInterpretation(brand_name="Brand", url="https://brand.test", blocks={"mission": {"detected": True, "content": "Help finance teams close faster.", "confidence": "high", "rationale": "The site says so."}}, evidence_refs={"mission": refs}),
        evaluation_evidence_refs={"mission": refs},
        evaluation_evidence_version="sv9-flow-evaluation-evidence-refs-v1",
    )


class _QuotingLLM:
    """Fake evaluator: M1 is lit by the first quote its prompt shows, M2 is "no"."""

    api_key, model, base_url = "fake", "fake-evaluator", ""

    def _call_json(self, _system, user, **_kwargs):
        quote = next(line[2:] for line in user.split("CITAS DE EVIDENCIA:\n", 1)[1].splitlines() if line.startswith("- "))
        return {"message": "Lectura breve.", "baldosas": [{"id": tile, "estado": "ok", "evidencia": quote} if tile == "M1" else {"id": tile, "estado": "no" if tile == "M2" else "sin_evidencia", "motivo": "Sin prueba."} for tile in evaluation._COMPONENT_TILES["mission"]]}


def _scoped_adapter(monkeypatch, enabled, rescan=True):
    from src.sv9.aggregator import score_from_tile_profile
    from src.sv9.models import ComponentResult, TileVerdict
    from src.sv9_flow import orchestrator

    monkeypatch.setattr(shared_process, "BRAND3_VAULT_SV9_REQUEST_SCOPED_PROMPT_ENABLED", enabled)
    monkeypatch.setattr(orchestrator, "build_flow_candidate", lambda **_kwargs: (_scoped_candidate(), {}))
    adapter = shared_process.CoreFlowSv9StrictComponentAdapter(snapshot={}, source_run_id="scan", interpretation_llm_factory=_QuotingLLM, adjudicator_llm_factory=_QuotingLLM, labeling_llm_factory=_QuotingLLM, evaluator_llm_factory=_QuotingLLM, reasoning_llm_factory=_QuotingLLM, gate_authority="veto_only")
    if not rescan:
        return adapter
    # The re-scan re-evaluates M1 on top of the accepted mission result.
    profile = [TileVerdict(tile_id=tile, estado="sin_evidencia", motivo="Sin prueba.") for tile in evaluation._COMPONENT_TILES["mission"]]
    adapter._components = {"mission": ComponentResult(component="mission", status="scored", score=score_from_tile_profile(profile), tile_profile=profile)}
    adapter._prior_projected_components = adapter._project_source_policy_graph(adapter._components)
    return adapter


def _scoped_series():
    return shared_process.build_core_shared_series_contract(interpretation_model="interpretation-fake", labeling_model="labeling-fake", adjudicator_model="adjudicator-fake", evaluator_model="evaluator-fake", reasoning_model="reasoning-fake", editorial_model="editorial-fake", gate_authority="veto_only", editorial_enabled=False)


def _scoped_request():
    rows = [{"evidence_ref": row.ref, "evidence_fingerprint": hashlib.sha256(row.content.encode()).hexdigest(), "content": row.content} for row in _ROUTED_ROWS]
    return {"plan_fingerprint": _hash(301), "candidate_series_fingerprint": _hash(302), "component_key": "mission", "current_series_contract": _scoped_series(), "current_series_fingerprint": _hash(303), "capture_origin": _origin("capture", 304), "operation_origin": _origin("operation", 305), "requested_tiles": [{"tile_id": "M1", "evidence": rows}], "canonical_request_fingerprint": _hash(306)}


@pytest.mark.parametrize("enabled", [False, True])
def test_request_scoped_prompt_binds_rows_the_flow_block_never_showed(monkeypatch, enabled):
    adapter, request, observed = _scoped_adapter(monkeypatch, enabled), _scoped_request(), []
    with service.observe_evidence_vault_sv9_authority_evaluation_diagnostics(lambda event, _exc: observed.append(event)):
        outcome = adapter.evaluate_component(request)

    routed = [row.ref for row in _ROUTED_ROWS]
    if not enabled:
        # Main's failure: Core quotes its Flow block, and no requested row is admitted.
        binding = observed[0]["evidence_binding"]
        assert outcome.reason_code == "provider_failure" and [event["reason_codes"] for event in observed] == [["evidence_binding_failure"]]
        assert (binding["requested_evidence_refs"], binding["supplied_evidence_refs"], binding["evaluation_evidence_refs"]) == (routed, [], [row.ref for row in _FLOW_ROWS])
        return
    assert observed == [] and outcome.evaluation["tile_results"] == [{"tile_id": "M1", "assessment_state": "ok", "supporting_evidence": [{key: request["requested_tiles"][0]["evidence"][0][key] for key in ("evidence_ref", "evidence_fingerprint")}]}]
    # Shown is admitted: Core saw all 9 requested rows (no 8-row cap) and nothing else.
    assert adapter.get_shared_checkpoint_process(request)["component_result"]["evidence"] == [row.content for row in _ROUTED_ROWS]


@pytest.mark.parametrize("corrected", [True, False], ids=["retry_corrects", "keeps_sibling_quote"])
def test_request_scoped_prompt_binds_each_tile_to_its_own_rows(monkeypatch, corrected):
    # Primary's A1/P2 failure: one component prompt shows every requested tile's rows, and Core lit M1 with M2's row.
    class SiblingQuotingLLM(_QuotingLLM):
        calls = []

        def _call_json(self, _system, user, **_kwargs):
            self.calls.append(user)
            quote = _ROUTED_ROWS[0 if corrected and len(self.calls) > 1 else 5].content
            return {"message": "Lectura breve.", "baldosas": [{"id": tile, "estado": "ok", "evidencia": quote} if tile == "M1" else {"id": tile, "estado": "no" if tile == "M2" else "sin_evidencia", "motivo": "Sin prueba."} for tile in evaluation._COMPONENT_TILES["mission"]]}

    adapter, request, observed = _scoped_adapter(monkeypatch, True), _scoped_request(), []
    adapter._llm_factories = dict.fromkeys(adapter._llm_factories, SiblingQuotingLLM)
    m1, m2 = request["requested_tiles"][0]["evidence"][:5], request["requested_tiles"][0]["evidence"][5:]
    request["requested_tiles"] = [{"tile_id": "M1", "evidence": m1}, {"tile_id": "M2", "evidence": m2}]
    with service.observe_evidence_vault_sv9_authority_evaluation_diagnostics(lambda event, _exc: observed.append(event)):
        outcome = adapter.evaluate_component(request)

    assert len(SiblingQuotingLLM.calls) == 2 and "otra baldosa en: M1" in SiblingQuotingLLM.calls[1]
    if not corrected:
        # The last attempt only checks the component's quotes: Vault's per-tile binding still fails closed.
        assert outcome.reason_code == "provider_failure" and [event["reason_codes"] for event in observed] == [["evidence_binding_failure"]]
        return
    assert observed == [] and outcome.evaluation["tile_results"][0] == {"tile_id": "M1", "assessment_state": "ok", "supporting_evidence": [{key: m1[0][key] for key in ("evidence_ref", "evidence_fingerprint")}]}


def test_request_scoped_prompt_leaves_a_first_evaluation_on_the_flow_block(monkeypatch):
    # No prior shared analysis and the complete capture on every tile: a first Core-shared evaluation.
    capture = sorted(({"evidence_ref": row.ref, "evidence_fingerprint": hashlib.sha256(row.content.encode()).hexdigest(), "content": row.content} for row in [*_FLOW_ROWS, *_ROUTED_ROWS]), key=lambda row: row["evidence_ref"])
    request, runs = _scoped_request() | {"requested_tiles": [{"tile_id": tile, "evidence": capture} for tile in evaluation._COMPONENT_TILES["mission"]]}, []
    for enabled in (False, True):
        first, resumed = (_scoped_adapter(monkeypatch, enabled, rescan=False) for _ in range(2))
        outcome = first.evaluate_component(request)
        process = first.get_shared_checkpoint_process(request)
        resumed.restore_shared_checkpoint_process(request, outcome.evaluation, process)
        runs.append((outcome, process["component_result"], resumed.get_shared_checkpoint_process(request)["component_result"]))

    assert runs[0][0].evaluation is not None and runs[1] == runs[0]


def test_request_scoped_core_evidence_is_what_the_ledger_marks_shown(monkeypatch):
    from src.services import evidence_vault_evidence_ledger as ledger

    adapter, request = _scoped_adapter(monkeypatch, True), _scoped_request()
    assert adapter.evaluate_component(request).evaluation is not None
    process = adapter.get_shared_checkpoint_process(request)
    index = ledger.build_shown_index([{"component_key": "mission", "status": "evaluated", "candidate": process["flow_context"]["candidate"], "component_result": process["component_result"]}])

    assert [ledger.shown_to_core(row.content, index)["mission"] for row in _ROUTED_ROWS] == [{"status": "shown", "reason_codes": [], "evidence_refs": [row.ref]} for row in _ROUTED_ROWS]
    assert ledger.shown_to_core(_FLOW_ROWS[0].content, index)["mission"] == {"status": "not_shown", "reason_codes": ["not_in_evaluation_evidence"], "evidence_refs": []}


@pytest.mark.parametrize("enabled", [True, False])
def test_restore_rebuilds_the_prompt_scope_its_checkpoint_was_evaluated_with(monkeypatch, enabled):
    adapter, request = _scoped_adapter(monkeypatch, True), _scoped_request()
    accepted = adapter.evaluate_component(request).evaluation
    process, resumed = adapter.get_shared_checkpoint_process(request), _scoped_adapter(monkeypatch, enabled)
    assert accepted is not None
    if enabled:
        resumed.restore_shared_checkpoint_process(request, accepted, process)
        assert resumed.get_shared_checkpoint_process(request)["component_result"] == process["component_result"]
        return
    # Flipping the flag off mid-scan fails closed instead of re-binding another prompt's quotes.
    with pytest.raises(shared_process.FlowSv9StrictComponentAdapterError):
        resumed.restore_shared_checkpoint_process(request, accepted, process)


def _shared_analysis_owners(adapter, mission_evidence):
    from src.sv9.models import ComponentResult
    from src.sv9.rubric import PRESENTATION_ORDER

    candidate = _scoped_candidate().to_dict()
    adapter._components = {key: ComponentResult(component=key, status="not_detected") for key in PRESENTATION_ORDER} | {"mission": ComponentResult(component="mission", status="scored", evidence=mission_evidence)}
    value = {"analysis_payload": {"flow": {"candidate": candidate}}, "component_provenance": {key: candidate for key in PRESENTATION_ORDER if key != "coherencia"}}
    return shared_process._component_provenance_candidates_from_shared_analysis(value, components=adapter._components)


# Requests sort refs as text and group them by tile, so scoped evidence need not follow pack order.
@pytest.mark.parametrize("rows", [_FLOW_ROWS, _ROUTED_ROWS[::-1]], ids=["flow_block", "request_scoped"])
def test_shared_analysis_validators_accept_both_prompt_shapes_with_the_flag_off(monkeypatch, rows):
    # Flow-block evidence is main's shape; request-scoped evidence must survive a rollback.
    adapter = _scoped_adapter(monkeypatch, False)
    adapter._component_provenance_candidates = _shared_analysis_owners(adapter, [row.content for row in rows])
    adapter._prepare()

    sources, admitted = adapter._actual_evaluation_evidence("coherencia")

    assert sources[: len(rows)] == [row.content for row in rows] and {row.ref for row in rows} <= admitted


def test_shared_analysis_validator_rejects_a_prefix_of_a_pack_snippet(monkeypatch):
    with pytest.raises(shared_process.FlowSv9StrictComponentAdapterError):
        _shared_analysis_owners(_scoped_adapter(monkeypatch, False), [_ROUTED_ROWS[0].content[:14]])


def test_request_scoped_prompt_flag_keeps_the_series_fingerprint(monkeypatch):
    fingerprints = set()
    for enabled in (False, True):
        monkeypatch.setattr(shared_process, "BRAND3_VAULT_SV9_REQUEST_SCOPED_PROMPT_ENABLED", enabled)
        fingerprints.add(memory.canonical_fingerprint("sv9-judgment-series-fingerprint-v1", memory.validate_judgment_series_contract(_scoped_series())))
    assert len(fingerprints) == 1


def test_core_strict_quote_binding_failure_exposes_safe_binding_diagnostic(
    monkeypatch,
):
    from src.sv9.models import ComponentResult, TileVerdict

    quote = "At Primary, we believe exceptional branding is possible without the wait."
    component = "mission"
    target_tile = "M3"
    evaluation_refs = [
        "raw_inputs.1.subpage.3.chunk.4",
        "raw_inputs.1.subpage.3.chunk.3",
        "raw_inputs.1.subpage.3.chunk.5",
        "raw_inputs.1.chunk.8",
        "raw_inputs.1.subpage.15.chunk.8",
        "raw_inputs.1.subpage.14.chunk.1",
        "raw_inputs.2.exa.mentions.10",
    ]
    tiles = list(evaluation._COMPONENT_TILES[component])
    core = ComponentResult(
        component=component,
        status="scored",
        tile_profile=[
            TileVerdict(
                tile_id=tile,
                estado="ok" if tile == target_tile else "sin_evidencia",
                evidencia=quote if tile == target_tile else "",
            )
            for index, tile in enumerate(tiles)
        ],
    )
    adapter = object.__new__(shared_process.CoreFlowSv9StrictComponentAdapter)
    adapter._candidate = None
    adapter._components = {}
    adapter._component_provenance_candidates = {}
    adapter._prior_projected_components = {}
    adapter._tldr = {component: {"evaluation_evidence_refs": evaluation_refs}}
    adapter._prepare = lambda: None
    adapter._initialize_evaluation_llms = lambda: None
    adapter._evaluate_base_component = lambda _component: core
    adapter._merge_component = lambda _component, _requested, value: value
    adapter._project_component_for_strict = lambda _component: core
    adapter._actual_evaluation_evidence = lambda _component: (
        [quote],
        set(evaluation_refs),
    )
    monkeypatch.setattr(
        shared_process,
        "is_core_shared_series_contract",
        lambda _value: True,
    )
    request = {
        "component_key": component,
        "current_series_contract": {},
        "current_series_fingerprint": "a" * 64,
        "canonical_request_fingerprint": "b" * 64,
        "requested_tiles": [
            {
                "tile_id": tile,
                "evidence": (
                    [
                        {
                            "evidence_ref": "raw_inputs.1.chunk.17",
                            "evidence_fingerprint": "c" * 64,
                            "content": quote,
                        },
                        {
                            "evidence_ref": "raw_inputs.1.subpage.1.chunk.1",
                            "evidence_fingerprint": "d" * 64,
                            "content": quote,
                        },
                    ]
                    if tile == target_tile
                    else []
                ),
            }
            for index, tile in enumerate(tiles)
        ],
    }

    observed = []
    with service.observe_evidence_vault_sv9_authority_evaluation_diagnostics(
        lambda event, exc: observed.append((event, exc))
    ):
        outcome = adapter.evaluate_component(request)

    assert outcome.reason_code == "provider_failure"
    assert len(observed) == 1
    event, exception = observed[0]
    assert type(exception).__name__ == "FlowSv9StrictComponentAdapterError"
    assert event["reason_codes"] == ["evidence_binding_failure"]
    assert event["evidence_binding"] == {
        "tile_id": "M3",
        "requested_evidence_refs": [
            "raw_inputs.1.chunk.17",
            "raw_inputs.1.subpage.1.chunk.1",
        ],
        "supplied_evidence_refs": [],
        "evaluation_evidence_refs": evaluation_refs,
        "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
    }
    assert quote not in str(event)


@pytest.mark.parametrize("failing", ["flow", "hydration"])
def test_core_shared_adapter_does_not_repeat_or_continue_failed_flow_preparation(monkeypatch, failing):
    from src.sv9_flow import orchestrator
    attempts, evaluated = [], []
    monkeypatch.setattr(orchestrator, "build_flow_candidate", lambda **_kwargs: attempts.append(1) or ((_ for _ in ()).throw(RuntimeError("flow")) if failing == "flow" else (object(), {})))
    monkeypatch.setattr(shared_process, "is_core_shared_series_contract", lambda _value: True)
    adapter = shared_process.CoreFlowSv9StrictComponentAdapter(snapshot={}, source_run_id="run", interpretation_llm_factory=object, adjudicator_llm_factory=object, labeling_llm_factory=object, evaluator_llm_factory=object, reasoning_llm_factory=object, gate_authority="test")
    adapter._hydrate_analysis_inputs = lambda: (_ for _ in ()).throw(RuntimeError("hydration"))
    adapter._evaluate_base_component = evaluated.append
    outcomes = [adapter.evaluate_component({"component_key": component, "current_series_contract": {}, "requested_tiles": []}) for component in ("mission", "vision", "values")]
    assert [outcome.reason_code for outcome in outcomes] == ["provider_failure"] * 3 and (len(attempts), evaluated) == (1, [])
