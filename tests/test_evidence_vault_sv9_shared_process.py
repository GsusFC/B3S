import os

import pytest

from src.services import evidence_vault_sv9_shared_process as shared_process
from src.sv9 import assessment_kernel
from src.sv9 import incremental_evaluation as ie
from src.sv9.models import ComponentResult, TileVerdict
from src.sv9.rubric import PRESENTATION_ORDER
from src.sv9_flow.contracts import BrandEvidencePack, BrandInterpretation, EvidenceRecord, Sv9FlowCandidate


def _candidate(label: str) -> Sv9FlowCandidate:
    # Brand and domain match the accepted owner the Postgres scaffolding seeds (_CheckpointSharedFlow).
    record = EvidenceRecord(ref=f"flow:{label}:mission", source="flow", evidence_type="owned_copy", content=f"{label} mission evidence", url="https://example.com", confidence="high")
    return Sv9FlowCandidate(
        evidence_pack=BrandEvidencePack(brand_name="Example", url="https://example.com", evidence=[record]),
        interpretation=BrandInterpretation(brand_name="Example", url="https://example.com"),
        evaluation_evidence_version="test",
    )


def _component(component: str, label: str, offset: int) -> ComponentResult:
    profile = []
    for index, tile in enumerate(ie._COMPONENT_TILES[component]):
        state = (index + offset) % 3
        profile.append(TileVerdict(tile_id=tile, estado=("ok", "no", "sin_evidencia")[state], evidencia=f"{label} {tile} evidence" if state == 0 else "", motivo=f"{label} {'no' if state == 1 else 'blind spot'}" if state else ""))
    return ComponentResult(component=component, status="scored", score=sum(row.estado == "ok" for row in profile), tile_profile=profile, evaluation_model="test", evidence=[f"{label} mission evidence"] if component == "mission" else [])


def _assessment(components):
    return assessment_kernel.build_sv9_assessment([
        {"component_key": component, "tile_id": verdict.tile_id, "tile_key": ie._BY_TILE[verdict.tile_id][2], "assessment_state": verdict.estado}
        for component in PRESENTATION_ORDER
        for verdict in components[component].tile_profile
    ])


def _source_map(source="accepted"):
    return {tile: source for component in PRESENTATION_ORDER for tile in ie._COMPONENT_TILES[component]}


def _core_adapter(source_run_id, prior=None, candidate=None):
    """The adapter a Vault re-scan builds (scan_runner._vault_core_shared_flow), loading ``prior`` as its accepted base."""
    adapter = shared_process.CoreFlowSv9StrictComponentAdapter(
        snapshot={}, source_run_id=source_run_id, interpretation_llm_factory=object, adjudicator_llm_factory=object,
        labeling_llm_factory=object, evaluator_llm_factory=object, reasoning_llm_factory=object,
        gate_authority="veto_only", prior_shared_analysis=prior,
    )
    # The re-scan's Flow context: set here so _prepare makes no Flow model calls.
    adapter._candidate = candidate
    return adapter


def _prior():
    owner = _candidate("accepted")
    seed = _core_adapter("accepted-run", candidate=owner)
    seed._components = {component: _component(component, "accepted", index) for index, component in enumerate(PRESENTATION_ORDER)}
    seed._component_provenance_candidates = dict.fromkeys(shared_process._shared_provenance_component_keys(), owner)
    return seed.build_shared_analysis_payload(_assessment(seed._components))


def test_merged_payload_keeps_accepted_rows_the_rescan_overwrote():
    prior = _prior()
    adapter = _core_adapter("rescan", prior, _candidate("current"))
    accepted = dict(adapter._components)
    # Same states as the accepted rows with other text, so only the rows tell the sources apart.
    mission, vision = _component("mission", "current", 0), _component("vision", "current", 1)
    # What evaluate_component and restore_shared_checkpoint_process leave for the components Core saw.
    adapter._components |= {"mission": adapter._merge_component("mission", ["M1", "M2"], mission), "vision": vision}
    adapter._component_provenance_candidates |= dict.fromkeys(("mission", "vision"), adapter._candidate)

    payload, _ = adapter.build_merged_shared_analysis_payload(assessment=_assessment(accepted), current_components={"mission": mission}, tile_source_map=_source_map() | {"M2": "current"})

    rows = payload["evaluation_components"]["mission"]["tile_profile"]
    assert rows[0] == prior["evaluation_components"]["mission"]["tile_profile"][0] and rows[1] == mission.tile_profile[1].to_dict()
    assert payload["evaluation_components"]["vision"] == prior["evaluation_components"]["vision"]
    assert payload["component_provenance"] == prior["component_provenance"] | {"mission": adapter._candidate.to_dict()}


def test_merged_payload_rejects_assessment_that_breaks_kernel_parity():
    adapter = _core_adapter("rescan", _prior(), _candidate("current"))
    invalid = _assessment(adapter._components)
    invalid["sv9_score"] += 1

    with pytest.raises(shared_process.FlowSv9StrictComponentAdapterError, match="Core and Vault assessments do not match"):
        adapter.build_merged_shared_analysis_payload(assessment=invalid, current_components={}, tile_source_map=_source_map())


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1",
    reason="requires disposable PostgreSQL",
)
def test_merged_payload_round_trips_through_postgres_snapshot_and_loader(monkeypatch):
    from src.history import repository as history
    from tests import test_evidence_vault_sv9_evaluation_checkpoint as checkpoint_tests
    from tests.test_evidence_vault_evidence_ledger_postgres import _CURRENT, _accepted_pair
    from tests.test_evidence_vault_scan_orchestration_postgres import _reset_repository
    from tests.test_evidence_vault_sv9_evaluation_checkpoint_postgres import _shared_series
    from tests.test_evidence_vault_sv9_judgment_candidates_postgres import _adopt, _rescan_inputs, _select, _v3

    repository = _reset_repository()
    _accepted_pair(repository)
    authority = repository.get_evidence_vault_sv9_judgment_authority("example.com")
    accepted = authority["accepted_candidate"]
    with monkeypatch.context() as patch:
        # The re-scan's checkpoint rows join the accepted Core series.
        patch.setattr(checkpoint_tests, "_series", _shared_series)
        lit, witness = _rescan_inputs(monkeypatch, repository, _CURRENT, authority)
    candidate = _v3(_select(accepted["candidate_tile_judgments"], lit["M2"]), accepted["candidate_component_sentinels"], witness, prior=accepted["id"])
    prior = repository.get_evidence_vault_sv9_shared_analysis(accepted["id"])
    prior_mission = prior["evaluation_components"]["mission"]["tile_profile"]
    # M2 is "ok" like its checkpoint row. Every accepted tile is "ok" too, so the rows, not the score, tell the sources apart.
    current = _component("mission", "current", 2)
    assert all(row != verdict.to_dict() for row, verdict in zip(prior_mission, current.tile_profile))
    adapter = _core_adapter(_CURRENT, prior, _candidate("current"))
    payload, _ = adapter.build_merged_shared_analysis_payload(assessment=candidate["assessment"], current_components={"mission": current}, tile_source_map=_source_map() | {"M2": "current"})

    with pytest.raises(history.EvidenceVaultSv9JudgmentCandidateError, match="required for the Core judgment series"):
        repository.append_evidence_vault_sv9_judgment_candidate(_CURRENT, candidate)
    stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(_CURRENT, candidate, shared_analysis_payload=payload)
    adopted = _adopt(repository, _CURRENT, stored["id"], authority["current_head"]["event_fingerprint"])
    assert inserted and adopted["current_head"]["event_type"] == "supersede" and adopted["accepted_candidate"] == stored

    # The next re-scan reads its prior as scan_runner._vault_core_shared_flow does: the accepted candidate's snapshot.
    loaded = repository.get_evidence_vault_sv9_shared_analysis(repository.get_evidence_vault_sv9_judgment_authority("example.com")["accepted_candidate"]["id"])
    following = _core_adapter("next-rescan", loaded)
    components = {key: value.to_dict() for key, value in following._components.items()}
    owners = {key: value.to_dict() for key, value in following._component_provenance_candidates.items()}
    assert loaded == payload and components["mission"]["tile_profile"] == [current.tile_profile[1].to_dict() if row["id"] == "M2" else row for row in prior_mission]
    assert {key: value for key, value in components.items() if key != "mission"} == {key: value for key, value in prior["evaluation_components"].items() if key != "mission"}
    assert owners == prior["component_provenance"] | {"mission": adapter._candidate.to_dict()}
    assert _assessment(following._components) == candidate["assessment"]
