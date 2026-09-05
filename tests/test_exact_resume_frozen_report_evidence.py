"""Publication carries persisted evidence, never a rebuilt acquisition fixture."""

from copy import deepcopy

import pytest

from src.history.report_parser import canonical_json_hash, parse_report
from src.services.evidence_vault_scan_orchestration import prepare_vault_scan_after_capture
from tests.test_evidence_vault_scan_orchestration import _Repository, _snapshot
from tests.test_vault_authority_scanner_orchestration import _assessment_report
from web import scan_runner


@pytest.mark.parametrize("unavailable", (False, True))
def test_authority_report_preserves_frozen_evidence_without_rebuilding(unavailable):
    repository = _Repository(memory=None, history=[])
    prepared = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=_snapshot("Exact frozen evidence"),
        scan_id="frozen-evidence",
        url="https://example.com",
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    observation = prepared["report_observation"]
    before = deepcopy(observation)
    binding = {
        "source_scan_id": observation["source_scan_id"],
        "observation_hash": canonical_json_hash(observation),
        "capture_hash": canonical_json_hash(observation["capture_payload"]),
    }
    source = _assessment_report("frozen-evidence", binding, unavailable=unavailable)
    source["raw"].pop("flow", None)
    publication = {"scanner_payload": source["raw"]}
    original_publication = deepcopy(publication)
    payload = scan_runner._authority_scanner_payload(
        publication=publication,
        canonical_snapshot={"raw_inputs": []},
        canonical_source_capture=binding,
        gate={},
        report_observation=observation,
    )
    report = scan_runner._compose_report("frozen-evidence", "https://example.com", "Example", payload)
    parsed = parse_report(report)
    assert list(parsed.evidence_records) == observation["evidence_records"]
    assert parsed.evaluation_config["assessment_availability"] == ("unavailable" if unavailable else "available")
    assert report["raw"]["source_capture"] == binding
    assert report["raw"]["source_run_id"] == "frozen-evidence"
    assert publication == original_publication and observation == before
    payload["flow"]["candidate"]["evidence_pack"]["evidence"][0]["metadata"]["test"] = "detached"
    assert observation == before
