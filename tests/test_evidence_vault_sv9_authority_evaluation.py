from copy import deepcopy
import json
import pytest

from src.services import evidence_vault_sv9_authority_event as authority_event
from src.services import evidence_vault_sv9_authority_evaluation as service
from src.services import evidence_vault_sv9_authority_projection as authority_projection
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
    current = (rows or sentinels)[0]["series_fingerprint"]
    candidate = {"schema_version": "evidence-vault-sv9-judgment-candidate-v2", "plan": {}, "canonical_plan_fingerprint": _hash(201), "current_series_fingerprint": current, "candidate_series_fingerprint": _hash(202), "component_evaluations": [], "evidence_bindings": [], **partition, "assessment": {"sv9_score": 1}, "telemetry": {"call_count": 0, "calls_avoided": 0, "reused_tile_count": 0, "evaluated_tile_count": 0}, "assessment_fingerprint": _hash(203), "score_fingerprint": _hash(204), "authoritative_relation_witness": {}}
    candidate["evaluation_bundle_fingerprint"] = memory.canonical_fingerprint("sv9-judgment-evaluation-bundle-v1", {"canonical_plan_fingerprint": candidate["canonical_plan_fingerprint"], "evaluations": []}); candidate["complete_record_fingerprint"] = authority_event.candidate_complete_record_fingerprint(candidate); candidate |= {"id": _ID, "source_scan_id": "scan", "created_at": "2026-08-30T00:00:00+00:00"}
    identity = {name: candidate[name] for name in "id complete_record_fingerprint evaluation_bundle_fingerprint canonical_plan_fingerprint current_series_fingerprint candidate_series_fingerprint assessment_fingerprint score_fingerprint source_scan_id".split()}; request = authority_event.build_evidence_vault_sv9_authority_request(action="adopt_candidate", candidate_id=_ID, expected_predecessor_event_fingerprint=None, delta_fingerprint=None, source_scan_id="scan")
    event = authority_event.build_evidence_vault_sv9_authority_event(event_id="00000000-0000-0000-0000-000000000202", event_type="adopt", sequence=1, predecessor_event_fingerprint=None, active_parent_event_fingerprint=None, candidate_identity=identity, current_series_fingerprint=current, delta_fingerprint=None, request=request, idempotency_key_hash=authority_event.authority_application_idempotency_fingerprint(request), created_at="2026-08-30T00:00:00+00:00")
    return authority_projection.build_evidence_vault_sv9_authority_projection(accepted_candidate=candidate, current_head=event, active_authority_event=event, event=event, reopen_review_overlay=None)

class _Repository:
    def __init__(self, authority=None, records=(3,), bad_reload=False, identities=None, contents=None, tile_count=None):
        self.authority, self.records, self.bad_reload = authority, tuple(records), bad_reload; self.append_calls = self.get_calls = self.authority_calls = self.context_calls = self.evidence_calls = 0; self.candidates = {}; self.mutations = []; self.checkpoints = {}; self.checkpoint_gets = []; self.checkpoint_appends = []; self.shared_analysis_payloads = []; self.fail_checkpoint_append = self.checkpoint_conflict = self.unmapped = self.hint_only = self.reopen = False
        self.identities, self.contents = dict(identities or {}), dict(contents or {})
        self.tile_count = len(self.records) if tile_count is None else tile_count
        self.shared_processes = {}
        self.context = {"capture_origin": {"capture_id": "00000000-0000-0000-0000-000000000009", "capture_fingerprint": _hash(9)}, "operation_origin": {"operation_id": "00000000-0000-0000-0000-000000000010", "operation_fingerprint": _hash(10)}}; self.projection_relations = None; self.projection_status = "available"; self.projection_calls = 0; self.witness_seed = 300
    def get_evidence_vault_sv9_judgment_authority(self, _domain, **_kwargs): self.authority_calls += 1; return deepcopy(self.authority)
    def load_evidence_vault_sv9_judgment_context(self, _scan, **_kwargs): self.context_calls += 1; return {"canonical_domain": "example.test", "capture_id": self.context["capture_origin"]["capture_id"], "capture_fingerprint": self.context["capture_origin"]["capture_fingerprint"], "operation_plan_id": self.context["operation_origin"]["operation_id"], "operation_fingerprint": self.context["operation_origin"]["operation_fingerprint"]}
    def resolve_evidence_vault_sv9_judgment_evidence(self, _scan, refs, **_kwargs):
        self.evidence_calls += 1; assert refs == sorted(refs); rows = [{"evidence_record_id": f"00000000-0000-0000-0000-{number:012d}", **self.identity(number), "content": deepcopy(self.contents.get(number, {"evidence": number}))} for number in self.records]
        assert {row["evidence_ref"] for row in rows} == set(refs); return {**self.context, "evidence": rows}
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
    def append_evidence_vault_sv9_evaluation_checkpoint(self, _scan, checkpoint, **_kwargs):
        self.checkpoint_appends.append(deepcopy(checkpoint))
        if self.fail_checkpoint_append: raise RuntimeError("checkpoint failed")
        evaluation = checkpoint["healthy_workset"]["component_evaluations"][0]; key = (checkpoint["plan_binding"]["canonical_plan_fingerprint"], evaluation["request_fingerprint"]); self.checkpoints[key] = deepcopy(evaluation)
        if _kwargs.get("shared_process_payload") is not None: self.shared_processes[key] = deepcopy(_kwargs["shared_process_payload"])
        return deepcopy(checkpoint), True
    def load_evidence_vault_sv9_authoritative_relation_facts(self, scan, *, workspace_slug="b3s"):
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
        evidence = [
            source
            | {
                "evidence_record_id": f"00000000-0000-0000-0000-{number:012d}",
                **self.identity(number),
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
        facts = _facts(count=self.tile_count); source = facts["source"]; source.update(source_scan_id=scan, workspace_slug=workspace, canonical_domain="example.test"); self.context = {"capture_origin": {key: source[key] for key in ("capture_id", "capture_fingerprint")}, "operation_origin": {"operation_id": source["operation_plan_id"], "operation_fingerprint": source["operation_fingerprint"]}}
        if getattr(self, "baseline_plan", None):
            source["operation_fingerprint"] = self.baseline_plan["operation_plan_fingerprint"]
            self.context["operation_origin"]["operation_fingerprint"] = source["operation_fingerprint"]
        rows = [dict(facts["evidence"][0], source_scan_id=scan, canonical_domain="example.test", evidence_record_id=f"00000000-0000-0000-0000-{number:012d}", **self.identity(number), evidence_id=_hash(100 + number), source_identity_id=_hash(200 + number)) for number in self.records]
        facts["evidence"] = rows
        for index, accepted in enumerate(facts["authority"]["accepted"]):
            row = rows[min(index, len(rows) - 1)]; accepted["basis"][0].update(evidence_id=row["evidence_id"], source_identity_id=row["source_identity_id"])
        facts["authority"]["witness"] = {"canonical_memory_version": _hash(self.witness_seed), "adoption_event_id": f"00000000-0000-0000-0000-{self.witness_seed:012d}", "adoption_sequence": 1, "candidate_packet_fingerprint": _hash(self.witness_seed + 1), "request_fingerprint": _hash(self.witness_seed + 2)}
        if self.reopen: facts["authority"]["accepted"][0]["basis"][0].update(evidence_id=_hash(998), source_identity_id=_hash(999))
        if self.unmapped or self.hint_only: facts["authority"]["accepted"] = facts["authority"]["accepted"][:1]
        if getattr(self, "empty_adopted_basis", False): facts["authority"]["accepted"] = []
        if self.hint_only: facts["evaluation_hint_seeds"] = [{"hint_id": "00000000-0000-0000-0000-000000000987", "tile_id": "M1", "component_key": "mission", "evidence_record_id": rows[1]["evidence_record_id"], "provenance_fingerprint": _hash(987)}]
        return project_evidence_vault_sv9_evaluation_input(repository=_FactsRepository(facts), source_scan_id=scan, workspace_slug=workspace)
    def identity(self, number): return deepcopy(self.identities.get(number, _identity(number)))

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
    def build_shared_analysis_payload(self, assessment):
        self.shared_analysis_calls.append(deepcopy(assessment))
        return deepcopy(self.shared_analysis)

def _relation(repo, tile, disposition="relevant", number=9): return delta.build_authoritative_evidence_tile_relation(tile_id=tile, component_key=dict(planner._REGISTRY)[tile], disposition=disposition, **repo.identity(number), **repo.context)
@pytest.fixture(autouse=True)
def _authoritative_projection(monkeypatch):
    monkeypatch.setattr(service, "project_evidence_vault_sv9_evaluation_input", lambda *, repository, source_scan_id, workspace_slug: repository.evaluation_input(source_scan_id, workspace_slug))

def _run(repo, flow, current=(3,), relations=None, series=None, trusted=(), projection_relations=None):
    relations = [_relation(repo, "M1", number=number) for number in current] if relations is None else list(relations); repo.records = tuple(current); repo.projection_relations = relations if projection_relations is None else list(projection_relations)
    result = service.run_evidence_vault_sv9_authority_evaluation(repository=repo, flow=flow, domain_or_url="example.test", source_scan_id="scan", current_series_contract=_series() if series is None else series, trusted_irrelevant_evidence=[repo.identity(number) for number in trusted])
    assert not repo.mutations; return result


class _CoreHarness:
    def __init__(self, *, fail_tile_call=None, tile_states=None):
        self.fail_tile_call = fail_tile_call
        self.tile_states = dict(tile_states or {})
        self.flow_factory_calls = []
        self.flow_calls = []
        self.tile_factory_calls = []
        self.tile_calls = []

    def flow_factory(self, role):
        def factory():
            self.flow_factory_calls.append(role)
            harness = self

            class Client:
                api_key = "test-key"
                model = f"{role}-fake"
                last_failure_reason = None
                call_failures = []

                def __init__(self):
                    self.usage_observations = []

                def _call_json(self, _system, _user, **_kwargs):
                    harness.flow_calls.append(role)
                    self.usage_observations.append(
                        {"event": "provider_call", "usage_metadata_available": False}
                    )
                    return {
                        "detected": True,
                        "content": "Acme helps finance teams close faster.",
                        "confidence": "high",
                        "evidence_refs": ["raw_inputs.0"],
                        "rationale": "The evidence states an audience and outcome.",
                        "limitations": [],
                    }

            return Client()

        return factory

    def tile_factory(self, role):
        def factory():
            self.tile_factory_calls.append(role)
            harness = self

            class Client:
                api_key = "test-key"
                model = f"{role}-fake"
                last_failure_reason = None

                def __init__(self):
                    self.call_failures = []
                    self.usage_observations = []

                def _call_json(self, _system, _user, **kwargs):
                    from src.sv9.rubric import COMPONENTS, tile_ids

                    component = next(
                        key
                        for key in COMPONENTS
                        if kwargs.get("schema_name") == f"baldosas_{key}"
                    )
                    harness.tile_calls.append(component)
                    self.usage_observations.append(
                        {"event": "provider_call", "usage_metadata_available": False}
                    )
                    if harness.fail_tile_call == len(harness.tile_calls):
                        raise RuntimeError("controlled component failure")
                    rows = []
                    for tile_id in tile_ids(component):
                        estado = harness.tile_states.get(
                            tile_id,
                            "ok" if tile_id == "M1" else "sin_evidencia",
                        )
                        rows.append(
                            {
                                "id": tile_id,
                                "estado": estado,
                                "evidencia": (
                                    "Acme helps finance teams close faster."
                                    if estado == "ok"
                                    else ""
                                ),
                                "motivo": (
                                    "Direct literal support."
                                    if estado == "ok"
                                    else (
                                        "The evidence contradicts this tile."
                                        if estado == "no"
                                        else "The evidence is insufficient for this tile."
                                    )
                                ),
                            }
                        )
                    payload = {"baldosas": rows}
                    if component == "coherencia":
                        payload["veredicto"] = "The brand story is not yet coherent."
                    return payload

            return Client()

        return factory


def _core_series():
    from src.services.evidence_vault_sv9_shared_process import (
        build_core_shared_series_contract,
    )

    return build_core_shared_series_contract(
        interpretation_model="interpretation-fake",
        labeling_model="labeling-fake",
        adjudicator_model="adjudicator-fake",
        evaluator_model="evaluator-fake",
        reasoning_model="reasoning-fake",
        editorial_model="editorial-fake",
        gate_authority="veto_only",
        editorial_enabled=True,
    )


def _core_adapter(harness, *, prior_shared_analysis=None, snapshot=None):
    from src.services.evidence_vault_sv9_shared_process import (
        CoreFlowSv9StrictComponentAdapter,
    )

    snapshot = snapshot or {
        "run": {
            "id": "scan",
            "brand_name": "Acme",
            "url": "https://example.test",
        },
        "raw_inputs": [
                {
                    "source": "homepage",
                "payload": {
                    "url": "https://example.test",
                    "text": "Acme helps finance teams close faster.",
                },
            }
        ],
    }
    return CoreFlowSv9StrictComponentAdapter(
        snapshot=snapshot,
        source_run_id="scan",
        interpretation_llm_factory=harness.flow_factory("interpretation"),
        adjudicator_llm_factory=harness.flow_factory("adjudicator"),
        labeling_llm_factory=harness.flow_factory("labeling"),
        evaluator_llm_factory=harness.tile_factory("evaluator"),
        reasoning_llm_factory=harness.tile_factory("reasoning"),
        gate_authority="veto_only",
        prior_shared_analysis=prior_shared_analysis,
    )


def _core_repository():
    return _Repository(
        records=(9,),
        identities={9: {"evidence_ref": "raw_inputs.0", "evidence_fingerprint": _hash(9)}},
        contents={9: "Acme helps finance teams close faster."},
        tile_count=len(planner._REGISTRY),
    )


def _partial_core_request(request, *tile_ids):
    partial = deepcopy(request)
    selected = set(tile_ids)
    partial["requested_tiles"] = [
        row for row in request["requested_tiles"] if row["tile_id"] in selected
    ]
    packet = evaluation.build_evidence_packet(
        component_key=request["component_key"],
        tiles=[
            {
                "tile_id": row["tile_id"],
                "evidence": row["evidence"],
            }
            for row in partial["requested_tiles"]
        ],
        capture_origin=partial["capture_origin"],
        operation_origin=partial["operation_origin"],
        series_fingerprint=partial["current_series_fingerprint"],
    )
    partial["evidence_packet_fingerprint"] = packet[
        "canonical_evidence_packet_fingerprint"
    ]
    partial["canonical_request_fingerprint"] = memory.canonical_fingerprint(
        evaluation._REQUEST_FINGERPRINT,
        {
            key: value
            for key, value in partial.items()
            if key != "canonical_request_fingerprint"
        },
    )
    return partial

def test_first_run_with_exact_operational_projection_persists_witnessed_candidate():
    repo, flow = _Repository(records=(9,)), _Flow(); result = _run(repo, flow, current=(9,))
    assert result["status"] == "candidate_available" and len(flow.calls) == len(repo.checkpoint_appends) == 10 and repo.append_calls == 1 and repo.get_calls == 2
    candidate = next(iter(repo.candidates.values())); assert candidate["evidence_bindings"][0]["evidence_ref"] == "evidence:9" and result["candidate"]["id"] == "00000000-0000-0000-0000-000000000203"
    assert candidate["schema_version"] == "evidence-vault-sv9-judgment-candidate-v2" and result["candidate"]["authoritative_relation_witness_fingerprint"] == candidate["authoritative_relation_witness"]["witness_fingerprint"]
    assert "workset_partition" not in result
    repeated = _run(repo, _Flow(), current=(9,)); assert repeated["status"] == "candidate_available" and not repeated["calls_issued"] and repo.append_calls == 1



def _first_baseline(repo):
    from src.services.evidence_vault_incremental_refresh import build_vault_scan_plan

    repo.unmapped = True
    repo.baseline_plan = build_vault_scan_plan(
        brand_identity="example.test", subject_url="https://example.test", mode="baseline",
        current_evidence_records=[
            {"ref": f"evidence:{number}", "source": "web", "evidence_type": "owned_content", "url": "https://example.test", "content": f"Frozen synthetic evidence {number}", "metadata": {}}
            for number in repo.records
        ],
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


def test_first_baseline_shared_process_resumes_and_finalizes_full_analysis():
    shared = {
        "schema_version": "sv9-flow-sv9-shadow-eval-v1",
        "source_run_id": "scan",
        "flow": {"candidate": {"interpretation": {"blocks": {}}}},
        "sv9": {"assessment": {"availability": "available"}},
    }
    repo = _first_baseline(_Repository(records=(3, 9)))
    interrupted = _Flow(fail=2, shared_analysis=shared)

    first = _run(repo, interrupted, current=(3, 9), series=_core_series())

    assert first["status"] == "no_new_score"
    assert len(interrupted.calls) == 2
    assert len(repo.checkpoints) == len(repo.shared_processes) == 1
    resumed = _Flow(shared_analysis=shared)

    outcome = _run(repo, resumed, current=(3, 9), series=_core_series())

    assert outcome["status"] == "candidate_available"
    assert len(resumed.calls) == 9
    assert len(resumed.shared_components) == 10
    candidate = next(iter(repo.candidates.values()))
    assert resumed.shared_analysis_calls == [candidate["assessment"]]
    assert repo.shared_analysis_payloads == [shared]


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


def test_first_baseline_exact_reuse_reports_all_avoided_work():
    from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository

    repo = _first_baseline(_ApplicationRepository(records=(3, 9)))
    assert _baseline_application(repo, _SelectiveFlow())["status"] == "authority_established"
    flow = _SelectiveFlow(fail=1)
    mutations = list(repo.mutations)

    outcome = service.run_evidence_vault_sv9_authority_evaluation(
        repository=repo,
        flow=flow,
        domain_or_url="example.test",
        source_scan_id="scan",
        current_series_contract=_series(),
    )

    assert outcome["status"] == "no_new_score"
    assert outcome["reason_codes"] == ["exact_reuse"]
    assert (
        outcome["calls_issued"],
        outcome["calls_avoided"],
        outcome["reused_tiles"],
        outcome["evaluated_tiles"],
    ) == (0, len(planner._COMPONENT_TILES), len(planner._REGISTRY), 0)
    assert not flow.calls
    assert repo.mutations == mutations


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


@pytest.mark.parametrize(("review_input", "empty_basis"), [("hint_only", False), ("reopen", False), ("hint_only", True)])
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


def test_shared_process_finalizes_full_analysis_before_candidate_append():
    shared = {
        "schema_version": "sv9-flow-sv9-shadow-eval-v1",
        "source_run_id": "scan",
        "flow": {"candidate": {"interpretation": {"blocks": {}}}},
        "sv9": {"assessment": {"availability": "available"}},
    }
    series = _series(
        evaluator_version="sv9-core-shared-evaluator-v1",
        prompt_version="sv9-flow-brand-interpretation-v1.2+baldosas-v3.1-evaluator-v2+sv9-editorial-v3.1",
        flow_version="sv9-flow-sv9-shadow-eval-v1",
        normalization_version="vault-capture-v1",
    )
    repo, flow = _Repository(records=(9,)), _Flow(shared_analysis=shared)

    result = _run(repo, flow, current=(9,), series=series)

    assert result["status"] == "candidate_available"
    assert flow.shared_analysis_calls == [next(iter(repo.candidates.values()))["assessment"]]
    assert repo.shared_analysis_payloads == [shared]


def test_real_core_adapter_resumes_frozen_flow_and_completed_component():
    repo = _core_repository()
    first_harness = _CoreHarness(fail_tile_call=2)
    first = _run(
        repo,
        _core_adapter(first_harness),
        current=(9,),
        series=_core_series(),
    )

    assert first["status"] == "no_new_score"
    assert first["reason_codes"] == ["provider_failure"]
    assert first_harness.flow_calls
    assert first_harness.tile_calls == ["mission", "attributes"]
    assert len(repo.checkpoints) == len(repo.shared_processes) == 3
    process = next(iter(repo.shared_processes.values()))
    assert process["schema_version"] == "evidence-vault-sv9-shared-checkpoint-process-v1"
    assert process["binding"]["component_key"] == "mission"
    assert process["flow_context"]["candidate"]["evidence_pack"]["evidence"]
    checkpoint_provider_calls = process["llm_usage"]["totals"]["provider_calls"]
    assert checkpoint_provider_calls > 0

    resumed_harness = _CoreHarness()
    resumed = _run(
        repo,
        _core_adapter(resumed_harness),
        current=(9,),
        series=_core_series(),
    )

    assert resumed["status"] == "candidate_available"
    assert resumed_harness.flow_factory_calls == []
    assert resumed_harness.flow_calls == []
    assert resumed_harness.tile_calls == [
        "attributes",
        "value_proposition",
        "personality",
        "brand_idea",
        "core_purpose",
        "coherencia",
    ]
    assert len(repo.checkpoints) == len(repo.shared_processes) == 10
    assert repo.shared_analysis_payloads[-1]["analysis_payload"]["flow"]["candidate"] == process["flow_context"]["candidate"]
    assert (
        repo.shared_analysis_payloads[-1]["analysis_payload"]["llm_usage"]["totals"][
            "provider_calls"
        ]
        == checkpoint_provider_calls + len(resumed_harness.tile_calls)
    )


def test_real_core_adapter_projects_source_policy_for_strict_candidate_but_keeps_raw_replay():
    repo = _core_repository()
    harness = _CoreHarness(
        tile_states={tile_id: "ok" for tile_id in ("M1", "M2", "M3", "M4", "M5")}
    )
    snapshot = {
        "run": {
            "id": "scan",
            "brand_name": "Acme",
            "url": "https://example.test",
        },
        "raw_inputs": [
            {
                "source": "external_proof",
                "payload": {
                    "url": "https://proof.test/acme",
                    "text": "Acme helps finance teams close faster.",
                },
            }
        ],
    }

    result = _run(
        repo,
        _core_adapter(harness, snapshot=snapshot),
        current=(9,),
        series=_core_series(),
    )

    assert result["status"] == "candidate_available"
    candidate = next(iter(repo.candidates.values()))
    mission_states = {
        row["tile_id"]: row["assessment_state"]
        for row in candidate["candidate_tile_judgments"]
        if row["component_key"] == "mission"
    }
    shared = repo.shared_analysis_payloads[-1]
    assert mission_states == {
        "M1": "ok",
        "M2": "ok",
        "M3": "ok",
        "M4": "sin_evidencia",
        "M5": "sin_evidencia",
    }
    assert shared["evaluation_components"]["mission"]["score"] == 5
    assert shared["analysis_payload"]["sv9"]["result"]["components"]["mission"]["score"] == 3
    assert candidate["assessment"] == {
        key: value
        for key, value in shared["analysis_payload"]["sv9"]["assessment"].items()
        if key
        not in {
            "schema_version",
            "assessment_schema_version",
            "expected_tile_count",
            "availability",
            "reason_codes",
        }
    } | {
        "schema_version": shared["analysis_payload"]["sv9"]["assessment"][
            "assessment_schema_version"
        ]
    }


def test_real_core_adapter_rejects_requested_evidence_excluded_from_actual_prompt():
    repo = _Repository(
        records=(9,),
        identities={
            9: {
                "evidence_ref": "raw_inputs.9",
                "evidence_fingerprint": _hash(9),
            }
        },
        contents={9: "© 2026 Acme. Cookie settings."},
        tile_count=len(planner._REGISTRY),
    )
    harness = _CoreHarness(tile_states={"M1": "no"})
    snapshot = {
        "run": {
            "id": "scan",
            "brand_name": "Acme",
            "url": "https://example.test",
        },
        "raw_inputs": [
            {
                "source": "homepage",
                "payload": {
                    "url": "https://example.test",
                    "text": "Acme helps finance teams close faster.",
                },
            },
            {
                "source": "external_proof",
                "payload": {
                    "url": "https://proof.test/excluded",
                    "text": "© 2026 Acme. Cookie settings.",
                },
            },
        ],
    }
    adapter = _core_adapter(harness, snapshot=snapshot)
    prepare = adapter._prepare

    def prepare_with_excluded_mission_ref():
        prepare()
        adapter._candidate.evaluation_evidence_refs["mission"] = ["raw_inputs.0"]
        adapter._hydrate_analysis_inputs()

    adapter._prepare = prepare_with_excluded_mission_ref

    result = _run(
        repo,
        adapter,
        current=(9,),
        series=_core_series(),
    )

    assert result["status"] == "no_new_score"
    assert result["reason_codes"] == ["provider_failure"]
    assert "mission" not in {
        row["healthy_workset"]["component_evaluations"][0]["component_key"]
        for row in repo.checkpoint_appends
    }
    assert repo.shared_analysis_payloads == []


def test_real_core_adapter_uses_ingress_snippet_dedup_for_provenance_pairs():
    from src.sv9.flow_ingress import detection_blocks_from_flow_candidate
    from src.sv9_flow.contracts import (
        BrandEvidencePack,
        BrandInterpretation,
        EvidenceRecord,
        Sv9FlowCandidate,
    )

    shared_prefix = "x" * 700
    candidate = Sv9FlowCandidate(
        evidence_pack=BrandEvidencePack(
            brand_name="Acme",
            url="https://example.test",
            evidence=[
                EvidenceRecord(
                    ref="raw_inputs.0",
                    source="homepage",
                    evidence_type="raw_input_text",
                    content=f"{shared_prefix} first",
                ),
                EvidenceRecord(
                    ref="raw_inputs.1",
                    source="homepage",
                    evidence_type="raw_input_text",
                    content=f"{shared_prefix} second",
                ),
            ],
        ),
        interpretation=BrandInterpretation(
            brand_name="Acme",
            url="https://example.test",
            blocks={
                "mission": {
                    "detected": True,
                    "content": "A mission.",
                    "confidence": "high",
                    "rationale": "The homepage states it.",
                }
            },
            evidence_refs={"mission": ["raw_inputs.0"]},
        ),
        evaluation_evidence_refs={
            "mission": ["raw_inputs.0", "raw_inputs.1"]
        },
    )
    block = detection_blocks_from_flow_candidate(candidate)["mission"]

    pairs = _core_adapter(_CoreHarness())._block_literal_source_pairs(
        block,
        candidate=candidate,
    )

    assert pairs == [(shared_prefix, "raw_inputs.0")]


def test_real_core_adapter_keeps_historical_provenance_for_reused_coherencia_context():
    from src.sv9.flow_ingress import _canonical_signal_refs
    from src.services.evidence_vault_sv9_shared_process import (
        _flow_candidate_from_shared_checkpoint,
    )

    repo = _core_repository()
    first = _run(
        repo,
        _core_adapter(_CoreHarness()),
        current=(9,),
        series=_core_series(),
    )
    assert first["status"] == "candidate_available"
    shared = repo.shared_analysis_payloads[-1]
    prior_candidate = _flow_candidate_from_shared_checkpoint(
        shared["analysis_payload"]["flow"]["candidate"]
    )
    historical_alias = _canonical_signal_refs(
        ["raw_inputs.0"],
        prior_candidate,
    )[0]
    resumed = _core_adapter(
        _CoreHarness(),
        prior_shared_analysis=shared,
        snapshot={
            "run": {
                "id": "scan",
                "brand_name": "Acme",
                "url": "https://example.test",
            },
            "raw_inputs": [
                {
                    "source": "homepage",
                    "payload": {
                        "url": "https://example.test",
                        "text": "Acme now serves a different market.",
                    },
                }
            ],
        },
    )
    resumed._prepare()

    literal_sources, admitted_refs = resumed._actual_evaluation_evidence(
        "coherencia"
    )

    assert "Acme helps finance teams close faster." in literal_sources
    assert historical_alias in admitted_refs


def test_real_core_adapter_persists_mixed_component_provenance_across_roundtrips():
    from src.services.evidence_vault_sv9_shared_process import (
        _flow_candidate_from_shared_checkpoint,
    )
    from src.sv9.flow_ingress import (
        _canonical_signal_refs,
        detection_blocks_from_flow_candidate,
    )

    repo = _core_repository()
    first = _run(
        repo,
        _core_adapter(_CoreHarness()),
        current=(9,),
        series=_core_series(),
    )
    assert first["status"] == "candidate_available"
    shared_a = repo.shared_analysis_payloads[-1]

    historical_component = "attributes"
    historical_owner = shared_a["analysis_payload"]["flow"]["candidate"]
    historical_alias = _canonical_signal_refs(
        ["raw_inputs.0"],
        _flow_candidate_from_shared_checkpoint(historical_owner),
    )[0]

    second_text = "Acme now serves a different market."
    second = _core_adapter(
        _CoreHarness(),
        prior_shared_analysis=shared_a,
        snapshot={
            "run": {
                "id": "scan",
                "brand_name": "Acme",
                "url": "https://example.test",
            },
            "raw_inputs": [
                {
                    "source": "homepage",
                    "payload": {
                        "url": "https://example.test",
                        "text": second_text,
                    },
                }
            ],
        },
    )
    second._prepare()
    second_owner = second._candidate
    second._components["mission"].evidence = detection_blocks_from_flow_candidate(
        second_owner
    )["mission"]["evidence"]
    second._component_provenance_candidates["mission"] = second_owner
    shared_b = second.build_shared_analysis_payload(
        next(iter(repo.candidates.values()))["assessment"]
    )
    persisted_b = json.loads(json.dumps(shared_b))

    assert persisted_b["analysis_payload"]["flow"]["candidate"] == second_owner.to_dict()
    assert persisted_b["component_provenance"]["mission"] == second_owner.to_dict()
    assert persisted_b["component_provenance"][historical_component] == historical_owner

    third = _core_adapter(
        _CoreHarness(),
        prior_shared_analysis=persisted_b,
        snapshot={
            "run": {
                "id": "scan",
                "brand_name": "Acme",
                "url": "https://example.test",
            },
            "raw_inputs": [
                {
                    "source": "homepage",
                    "payload": {
                        "url": "https://example.test",
                        "text": "Acme has changed again.",
                    },
                }
            ],
        },
    )
    third._prepare()

    literal_sources, admitted_refs = third._actual_evaluation_evidence("coherencia")

    assert second_text in literal_sources
    assert "Acme helps finance teams close faster." in literal_sources
    assert historical_alias in admitted_refs


@pytest.mark.parametrize("tamper", ["missing", "content", "domain"])
def test_real_core_adapter_rejects_invalid_persisted_component_provenance(tamper):
    from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapterError

    repo = _core_repository()
    result = _run(
        repo,
        _core_adapter(_CoreHarness()),
        current=(9,),
        series=_core_series(),
    )
    assert result["status"] == "candidate_available"
    shared = deepcopy(repo.shared_analysis_payloads[-1])

    if tamper == "missing":
        shared["component_provenance"].pop("mission")
    elif tamper == "content":
        shared["component_provenance"]["mission"]["evidence_pack"]["evidence"][
            0
        ]["content"] = "Evidence from another capture."
    else:
        owner = shared["component_provenance"]["mission"]
        owner["evidence_pack"]["url"] = "https://other.test"
        owner["interpretation"]["url"] = "https://other.test"

    with pytest.raises(
        FlowSv9StrictComponentAdapterError,
        match="prior shared analysis is invalid",
    ):
        _core_adapter(_CoreHarness(), prior_shared_analysis=shared)


def test_real_core_adapter_stops_coherencia_provenance_at_evaluator_source_limit():
    from types import SimpleNamespace

    adapter = _core_adapter(_CoreHarness())
    owner = object()
    sources = [f"source-{index}" for index in range(17)]
    adapter._component_provenance_candidates = {"mission": owner}
    adapter._detection_blocks_for_candidate = lambda candidate: {
        "mission": {"owner": candidate}
    }
    adapter._block_literal_source_pairs = lambda block, *, candidate: [
        (source, f"ref-{index}")
        for index, source in enumerate(sources[:16])
    ]

    pairs = adapter._coherencia_literal_source_pairs(
        {"mission": SimpleNamespace(evidence=sources)},
        sources[:16],
    )

    assert [(source, ref) for source, ref, _candidate in pairs] == [
        (source, f"ref-{index}")
        for index, source in enumerate(sources[:16])
    ]
    assert all(candidate is owner for _source, _ref, candidate in pairs)


def test_real_core_adapter_rejects_source_policy_spillover_outside_partial_workset():
    from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapterError

    repo = _core_repository()
    first_harness = _CoreHarness(
        tile_states={tile_id: "ok" for tile_id in ("M1", "M2", "M3", "M4", "M5")}
    )
    first_adapter = _core_adapter(
        first_harness,
        snapshot={
            "run": {
                "id": "scan",
                "brand_name": "Acme",
                "url": "https://example.test",
            },
            "raw_inputs": [
                {
                    "source": "external_proof",
                    "payload": {
                        "url": "https://proof.test/acme",
                        "text": "Acme helps finance teams close faster.",
                    },
                }
            ],
        },
    )
    requests = []
    evaluate = first_adapter.evaluate_component

    def recording_evaluate(request):
        requests.append(deepcopy(request))
        return evaluate(request)

    first_adapter.evaluate_component = recording_evaluate
    first = _run(repo, first_adapter, current=(9,), series=_core_series())
    assert first["status"] == "candidate_available"

    request = next(row for row in requests if row["component_key"] == "mission")
    partial = _partial_core_request(request, "M1")
    resumed_adapter = _core_adapter(
        _CoreHarness(tile_states={"M1": "no"}),
        prior_shared_analysis=repo.shared_analysis_payloads[-1],
        snapshot={
            "run": {
                "id": "scan",
                "brand_name": "Acme",
                "url": "https://example.test",
            },
            "raw_inputs": [
                {
                    "source": "external_proof",
                    "payload": {
                        "url": "https://proof.test/acme",
                        "text": "Acme helps finance teams close faster.",
                    },
                }
            ],
        },
    )
    resumed = resumed_adapter.evaluate_component(partial)

    assert resumed.reason_code == "provider_failure"
    assert resumed.evaluation is None
    prior_states = {
        row.tile_id: row.estado
        for row in resumed_adapter._prior_projected_components["mission"].tile_profile
    }
    projected = resumed_adapter._project_component_for_strict("mission")
    projected_states = {row.tile_id: row.estado for row in projected.tile_profile}
    assert prior_states["M4"] == "sin_evidencia"
    assert projected_states["M4"] == "ok"
    with pytest.raises(
        FlowSv9StrictComponentAdapterError,
        match="source policy changed an untouched accepted tile",
    ):
        resumed_adapter._validate_projected_untouched_tiles(
            "mission",
            partial,
            projected,
        )


def test_real_core_adapter_rejects_mixed_checkpoint_tampering_before_provider_calls():
    repo = _core_repository()
    first = _run(
        repo,
        _core_adapter(_CoreHarness(fail_tile_call=2)),
        current=(9,),
        series=_core_series(),
    )
    assert first["reason_codes"] == ["provider_failure"]
    key, original = next(iter(repo.shared_processes.items()))

    cases = {
        "source": lambda value: value["binding"].__setitem__("source_scan_id", "other-scan"),
        "capture": lambda value: value["binding"]["capture_origin"].__setitem__("capture_fingerprint", _hash(997)),
        "operation": lambda value: value["binding"]["operation_origin"].__setitem__("operation_fingerprint", _hash(997)),
        "series": lambda value: value["binding"].__setitem__("current_series_fingerprint", _hash(997)),
        "plan": lambda value: value["binding"].__setitem__("canonical_plan_fingerprint", _hash(997)),
        "request": lambda value: value["binding"].__setitem__("canonical_request_fingerprint", _hash(997)),
        "snapshot": lambda value: value["binding"].__setitem__("snapshot_fingerprint", _hash(997)),
        "component": lambda value: value["component_result"].__setitem__("component", "vision"),
        "strict-state": lambda value: value["component_result"]["tile_profile"][0].__setitem__("evidencia", "not present in requested evidence"),
        "llm-usage": lambda value: value["llm_usage"]["totals"].__setitem__(
            "provider_calls", value["llm_usage"]["totals"]["provider_calls"] + 1
        ),
    }
    for mutate in cases.values():
        tampered = deepcopy(original)
        mutate(tampered)
        repo.shared_processes[key] = tampered
        harness = _CoreHarness()
        result = _run(
            repo,
            _core_adapter(harness),
            current=(9,),
            series=_core_series(),
        )
        assert result["status"] == "no_new_score"
        assert result["reason_codes"] == ["repository_failure"]
        assert harness.flow_factory_calls == harness.tile_factory_calls == []
        assert harness.flow_calls == harness.tile_calls == []
    repo.shared_processes[key] = original


def test_real_core_adapter_rejects_forged_untouched_tile_before_provider_calls():
    from src.sv9.aggregator import score_from_tile_profile
    from src.services.evidence_vault_sv9_shared_process import (
        _component_from_shared_analysis_row,
    )
    from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapterError

    repo = _core_repository()
    first_harness = _CoreHarness()
    first_adapter = _core_adapter(first_harness)
    requests = []
    evaluate = first_adapter.evaluate_component

    def recording_evaluate(request):
        requests.append(deepcopy(request))
        return evaluate(request)

    first_adapter.evaluate_component = recording_evaluate
    first = _run(repo, first_adapter, current=(9,), series=_core_series())
    assert first["status"] == "candidate_available"

    request = next(row for row in requests if row["component_key"] == "mission")
    partial = _partial_core_request(request, "M1")

    full_evaluation = repo.checkpoints[
        (request["plan_fingerprint"], request["canonical_request_fingerprint"])
    ]
    accepted = evaluation.build_component_evaluation(
        component_key="mission",
        series_fingerprint=partial["current_series_fingerprint"],
        request_fingerprint=partial["canonical_request_fingerprint"],
        status="evaluated",
        tile_results=[
            row for row in full_evaluation["tile_results"] if row["tile_id"] == "M1"
        ],
    )

    process = deepcopy(
        repo.shared_processes[
            (request["plan_fingerprint"], request["canonical_request_fingerprint"])
        ]
    )
    process["binding"]["canonical_request_fingerprint"] = partial[
        "canonical_request_fingerprint"
    ]
    forged = _component_from_shared_analysis_row(
        "mission", process["component_result"]
    )
    untouched = next(row for row in forged.tile_profile if row.tile_id == "M2")
    untouched.estado = "no"
    untouched.evidencia = ""
    untouched.motivo = "The evidence directly contradicts this tile."
    forged.score = score_from_tile_profile(forged.tile_profile)
    process["component_result"] = forged.to_dict()

    resumed_harness = _CoreHarness()
    resumed_adapter = _core_adapter(
        resumed_harness,
        prior_shared_analysis=repo.shared_analysis_payloads[-1],
    )
    with pytest.raises(
        FlowSv9StrictComponentAdapterError,
        match="changed an untouched accepted tile",
    ):
        resumed_adapter.restore_shared_checkpoint_process(
            partial,
            accepted,
            process,
        )
    assert resumed_harness.flow_factory_calls == []
    assert resumed_harness.tile_factory_calls == []
    assert resumed_harness.flow_calls == []
    assert resumed_harness.tile_calls == []

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

def test_hint_only_healthy_workset_runs_when_canonical_component_workset_is_empty():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); repo.hint_only = True; outcome = _run(repo, flow, current=(3, 9))
    assert outcome["signed_delta"]["plan"]["component_workset"] == [] and [row["component_key"] for row in flow.calls] == ["mission", "coherencia"]
    assert outcome["status"] == "review_required" and outcome["candidate"] is None and len(repo.checkpoint_appends) == 2 and repo.get_calls == repo.append_calls == 0
    assert (outcome["calls_avoided"], outcome["reused_tiles"], outcome["review_tile_count"]) == (8, 69, 0)

def test_continuity_review_outcome_uses_partition_telemetry():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(); repo.reopen = True; outcome = _run(repo, flow, current=(3, 9))
    assert outcome["status"] == "review_required" and outcome["reason_codes"] == ["coverage_loss", "incomplete_review_partition"]
    assert (outcome["calls_avoided"], outcome["reused_tiles"], outcome["review_tile_count"]) == (9, 68, 11)

def test_provider_failure_outcome_uses_partition_telemetry():
    repo, flow = _Repository(_authority(), (3, 9)), _Flow(fail=1); repo.hint_only = True; outcome = _run(repo, flow, current=(3, 9))
    assert outcome["status"] == "review_required" and outcome["reason_codes"] == ["unmapped_evidence", "provider_failure"]
    assert (outcome["calls_avoided"], outcome["reused_tiles"], outcome["review_tile_count"]) == (8, 69, 0)

def test_resumed_partial_outcome_keeps_partition_telemetry():
    repo = _Repository(_authority(), (3, 9)); repo.hint_only = True
    first = _run(repo, _Flow(fail=2), current=(3, 9)); resumed = _run(repo, _Flow(fail=1), current=(3, 9))
    assert first["status"] == resumed["status"] == "review_required" and first["reason_codes"][-1] == resumed["reason_codes"][-1] == "provider_failure"
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
    assert outcome["candidate"] is None and not repo.candidates and not ({"assessment", "score", "adoption", "publication"} & set(outcome))

def test_bootstrap_reopen_coverage_loss_uses_partition_reasons_without_candidate_io():
    repo, flow = _Repository(records=(3, 9)), _Flow(); repo.reopen = True; outcome = _run(repo, flow, current=(3, 9))
    value = service.partitioning.build_evidence_vault_sv9_workset_partition(evaluation_input=repo.evaluation_input("scan", "b3s"), judgment_delta=outcome["signed_delta"], trusted_irrelevant_evidence=[])["review_partition"]
    assert value["evaluation_input_reopened_tile_ids"] == value["operational_authority_coverage_loss_tile_ids"] == ["M1"] and not value["judgment_delta_coverage_loss_tile_ids"]
    partition = outcome["workset_partition"]
    assert outcome["status"] == "review_required" and "coverage_loss" in outcome["reason_codes"] and "incomplete_candidate" not in outcome["reason_codes"] and flow.calls and repo.get_calls == repo.append_calls == 0
    assert partition["review_partition"] == value and partition["judgment_delta"] == outcome["signed_delta"] and outcome["candidate"] is None
    assert not ({"assessment", "score", "adoption", "publication"} & set(outcome))


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
