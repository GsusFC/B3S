from __future__ import annotations

from copy import deepcopy

import pytest

from src.history.capture_observation import parse_capture_observation
from src.history.models import ReportImportError


def test_capture_observation_requires_no_component_evaluation() -> None:
    parsed = parse_capture_observation(_observation())

    assert parsed.source_scan_id == "capture-only-1"
    assert parsed.canonical_domain == "example.com"
    assert parsed.observed_at.isoformat() == "2026-08-06T10:00:00+00:00"
    assert parsed.acquisition_state == "complete"
    assert len(parsed.evidence_records) == 1
    assert len(parsed.observation_hash) == 64
    assert len(parsed.capture_hash) == 64


def test_capture_observation_hash_is_stable_across_mapping_order() -> None:
    first = _observation()
    second = {key: first[key] for key in reversed(first)}

    assert parse_capture_observation(first).observation_hash == (
        parse_capture_observation(second).observation_hash
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_capture_observation_rejects_non_finite_numbers(value: float) -> None:
    payload = _observation()
    payload["capture_payload"]["invalid_number"] = value

    with pytest.raises(ReportImportError, match="non-finite"):
        parse_capture_observation(payload)



def test_capture_observation_rejects_duplicate_evidence_refs() -> None:
    payload = _observation()
    payload["evidence_records"].append(deepcopy(payload["evidence_records"][0]))

    with pytest.raises(ReportImportError, match="duplicate evidence ref"):
        parse_capture_observation(payload)


def test_capture_observation_rejects_malformed_collection_members() -> None:
    payload = _observation()
    payload["evidence_records"].append("not-an-object")

    with pytest.raises(ReportImportError, match=r"evidence_records\[1\]"):
        parse_capture_observation(payload)


def test_capture_observation_requires_complete_evidence_rows() -> None:
    payload = _observation()
    del payload["evidence_records"][0]["content"]

    with pytest.raises(ReportImportError, match="content"):
        parse_capture_observation(payload)


def test_capture_observation_rejects_jsonb_nul_and_unknown_fields() -> None:
    payload = _observation()
    payload["capture_payload"]["raw_inputs"][0]["payload"]["text"] = "bad\x00text"
    with pytest.raises(ReportImportError, match="NUL"):
        parse_capture_observation(payload)

    payload = _observation()
    payload["unpersisted_extra"] = {"value": 1}
    with pytest.raises(ReportImportError, match="unsupported fields"):
        parse_capture_observation(payload)


def test_capture_observation_retains_a_detached_exact_raw_payload() -> None:
    payload = _observation()
    parsed = parse_capture_observation(payload)
    payload["capture_payload"]["raw_inputs"][0]["payload"]["text"] = "mutated"

    assert parsed.raw_observation["capture_payload"]["raw_inputs"][0]["payload"]["text"] == "Evidence"
    assert parsed.capture_payload["raw_inputs"][0]["payload"]["text"] == "Evidence"


def _observation() -> dict:
    return {
        "schema_version": "b3s-capture-observation-v1",
        "source_scan_id": "capture-only-1",
        "source_run_id": "provider-run-1",
        "brand_name": "Example",
        "url": "https://www.example.com/path",
        "observed_at": "2026-08-06T12:00:00+02:00",
        "recorded_at": "2026-08-06T12:00:01+02:00",
        "pipeline_version": "vault-incremental-capture-v1",
        "acquisition_state": "complete",
        "acquisition_summary": {"owned_pages": 8, "external_results": 2},
        "limitations": [],
        "capture_payload": {
            "raw_inputs": [{"source": "web", "payload": {"text": "Evidence"}}]
        },
        "evidence_records": [
            {
                "ref": "raw_inputs.0.chunk.0",
                "source": "web",
                "evidence_type": "owned_copy",
                "url": "https://example.com/",
                "content": "Evidence",
                "confidence": "high",
                "metadata": {"source_class": "owned"},
            }
        ],
        "acquisition_attempts": [
            {"provider": "web", "status": "success", "intent": "owned"}
        ],
        "artifacts": [],
        "metadata": {"mode": "incremental_refresh"},
    }
