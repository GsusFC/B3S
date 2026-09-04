"""History import must preserve valid no-score reports without inventing scores."""

import pytest

from src.history.models import ReportImportError
from src.history.report_parser import canonical_json_hash, parse_report
from tests.test_evidence_vault_sv9_authority_evaluation import _Flow
from tests.test_vault_stabilization_replay import exact_replay as exact_replay
from web import report_store


@pytest.fixture
def unavailable_report(exact_replay, monkeypatch):
    scan, _repository, run, _directory = exact_replay
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: None)
    assert run(_Flow(fail=2)).action == "record_no_score"
    report = report_store.load_report(scan)
    assert report is not None and report["components"] == []
    return report


def test_import_valid_unavailable_report_without_components(unavailable_report):
    before = canonical_json_hash(unavailable_report)
    parsed = parse_report(unavailable_report)

    assert parsed.components == ()
    assert parsed.score is None and parsed.base_average is None
    assert parsed.evaluation_config["assessment_availability"] == "unavailable"
    assert parsed.evaluation_config["assessment_fingerprint"] is None
    assert parsed.evaluation_config["score_fingerprint"] is None
    assert parsed.report_payload == unavailable_report
    assert parsed.report_hash == before == canonical_json_hash(unavailable_report)
    assert parse_report(unavailable_report).report_hash == before


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("reason_codes", [], "sv9_unavailable_assessment_reasons_invalid"),
        ("sv9_score", 1, "sv9_unavailable_assessment_projection_invalid"),
    ],
)
def test_unavailable_import_still_validates_assessment(
    unavailable_report, field, value, reason
):
    unavailable_report["sv9_assessment"][field] = value
    with pytest.raises(ReportImportError, match=reason):
        parse_report(unavailable_report)


def test_unavailable_import_rejects_stripped_envelope(unavailable_report):
    unavailable_report.pop("sv9_assessment")
    with pytest.raises(ReportImportError, match="sv9_assessment_envelope_missing"):
        parse_report(unavailable_report)


def test_legacy_report_still_requires_components():
    report = {
        "id": "legacy-no-components",
        "url": "https://example.test",
        "created_at": "2026-09-05T00:00:00Z",
        "components": [],
    }
    with pytest.raises(ReportImportError, match="report must contain component evaluations"):
        parse_report(report)


def test_complete_report_keeps_canonical_score_and_identity(exact_replay, monkeypatch):
    _scan, _repository, run, _directory = exact_replay
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: None)
    publication = run(_Flow())
    assert publication.action == "publish_current"
    report = report_store.load_report(publication.report_id)
    parsed = parse_report(report)

    assert parsed.components
    assert parsed.evaluation_config["assessment_availability"] == "available"
    assert parsed.score == report["sv9_assessment"]["sv9_score"]
    assert parsed.evaluation_config["assessment_fingerprint"] == report["assessment_fingerprint"]
    assert parsed.evaluation_config["score_fingerprint"] == report["score_fingerprint"]
