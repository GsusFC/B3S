from copy import deepcopy

from src.services import evidence_vault_sv9_authority_evaluation as service
from src.services import evidence_vault_sv9_authoritative_relations as relations
from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.sv9 import judgment_memory as memory
from _harness_sv9_evaluation import _Repository, _Flow, _authority, _run


def test_no_accepted_result_does_not_reuse_operational_basis(monkeypatch):
    monkeypatch.setattr(
        service,
        "project_evidence_vault_sv9_evaluation_input",
        lambda *, repository, source_scan_id, workspace_slug: repository.evaluation_input(
            source_scan_id, workspace_slug
        ),
    )
    repo, flow = _Repository(None, (9,)), _Flow()
    outcome = _run(repo, flow, current=(9,), scan="first-scan")
    assert outcome["status"] == "candidate_available"
    assert flow.calls  # operational basis alone did not establish continuity


def test_accepted_v2_witness_reuses_exact_current_support_without_flow(monkeypatch):
    monkeypatch.setattr(
        service,
        "project_evidence_vault_sv9_evaluation_input",
        lambda *, repository, source_scan_id, workspace_slug: repository.evaluation_input(
            source_scan_id, workspace_slug
        ),
    )
    repo, flow = _Repository(_authority(), (3,)), _Flow()
    outcome = _run(repo, flow, current=(3,), scan="next-scan")
    candidate = repo.authority["accepted_candidate"]
    assert candidate["schema_version"] == "evidence-vault-sv9-judgment-candidate-v2"
    assert outcome["status"] == "no_new_score"
    assert outcome["reason_codes"] == ["exact_reuse"]


def test_missing_or_ambiguous_support_fails_closed_before_provider(monkeypatch):
    monkeypatch.setattr(
        service,
        "project_evidence_vault_sv9_authority_evaluation.project_evidence_vault_sv9_evaluation_input"
        if False else "project_evidence_vault_sv9_evaluation_input",
        lambda *, repository, source_scan_id, workspace_slug: repository.evaluation_input(
            source_scan_id, workspace_slug
        ),
    )
    repo, flow = _Repository(_authority(), (3,)), _Flow()
    missing = _run(repo, flow, current=(9,), scan="next-scan")
    assert missing["status"] == "review_required"
    assert "coverage_loss" in missing["reason_codes"]
    assert flow.calls == []

    repo, flow = _Repository(_authority(), (3,)), _Flow()
    original = repo.load_evidence_vault_sv9_authoritative_relation_facts

    def ambiguous(scan, *, workspace_slug="b3s"):
        facts = original(scan, workspace_slug=workspace_slug)
        duplicate = deepcopy(facts["evidence"][0])
        duplicate["evidence_record_id"] = "00000000-0000-0000-0000-000000000999"
        facts["evidence"].append(duplicate)
        return facts

    repo.load_evidence_vault_sv9_authoritative_relation_facts = ambiguous
    result = _run(repo, flow, current=(3,), scan="next-scan")
    assert result["status"] == "review_required"
    assert result["reason_codes"] == ["coverage_loss"]
    assert flow.calls == []


def test_same_ref_hash_with_changed_canonical_identity_fails_closed(monkeypatch):
    monkeypatch.setattr(
        service,
        "project_evidence_vault_sv9_evaluation_input",
        lambda *, repository, source_scan_id, workspace_slug: repository.evaluation_input(
            source_scan_id, workspace_slug
        ),
    )
    repo, flow = _Repository(_authority(), (3,)), _Flow()
    original = repo.load_evidence_vault_sv9_authoritative_relation_facts

    def tampered(scan, *, workspace_slug="b3s"):
        facts = original(scan, workspace_slug=workspace_slug)
        facts["evidence"][0]["evidence_id"] = "f" * 64
        facts["evidence"][0]["source_identity_id"] = "e" * 64
        return facts

    repo.load_evidence_vault_sv9_authoritative_relation_facts = tampered
    result = _run(repo, flow, current=(3,), scan="next-scan")
    assert result["status"] == "review_required"
    assert result["reason_codes"] == ["coverage_loss"]
    assert result["signed_delta"]["coverage_loss"] == []
    assert flow.calls == []
