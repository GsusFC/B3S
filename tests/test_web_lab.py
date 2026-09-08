from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.history.models import ReportConflictError


def test_normalize_url_accepts_domains_and_rejects_bad_inputs():
    from web.scan_runner import default_brand_name, normalize_url

    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url(" HTTPS://EXAMPLE.COM/path ") == "https://example.com/path"
    assert default_brand_name("https://www.linear.app") == "Linear"

    with pytest.raises(ValueError, match="URL is required"):
        normalize_url(" ")
    with pytest.raises(ValueError, match="host must include a dot"):
        normalize_url("intranet")
    with pytest.raises(ValueError, match="private"):
        normalize_url("http://127.0.0.1")


def test_vault_judgment_shadow_is_default_off_and_post_publication(monkeypatch):
    import inspect
    import src.services.evidence_vault_sv9_judgment_shadow as shadow
    import src.sv9.shadow_component_provider as provider
    from web import scan_runner

    constructed, invoked = [], {}
    monkeypatch.setattr(provider, "FlowSv9ShadowJsonProvider", lambda: constructed.append(True) or object())
    monkeypatch.setattr(shadow, "run_evidence_vault_sv9_judgment_shadow", lambda **kwargs: invoked.update(kwargs) or {"status": "pending"})
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")
    monkeypatch.setenv("BRAND3_VAULT_SV9_JUDGMENT_SHADOW_ENABLED", "TRUE")
    assert scan_runner._vault_sv9_judgment_shadow_enabled() is False
    report = {"score": 64}; scan_runner._run_vault_sv9_judgment_shadow_after_publication(scan_id="safe", repository=object(), payload={}, report=report); assert report == {"score": 64}
    assert not constructed and not invoked
    monkeypatch.setenv("BRAND3_VAULT_SV9_JUDGMENT_SHADOW_ENABLED", "true")
    assert scan_runner._vault_sv9_judgment_shadow_enabled() is True
    scan_runner._run_vault_sv9_judgment_shadow_after_publication(scan_id="safe", repository=object(), payload={}, report=report)
    assert constructed == [True] and invoked["current_public_score"] == 64 and type(invoked["flow"]).__name__ == "FlowSv9StrictComponentAdapter"
    source = inspect.getsource(scan_runner._run)
    assert source.index("_publish_completed_report(scan_id, report)") < source.index("_run_vault_sv9_judgment_shadow_after_publication(") and 'current_public_score=report.get("score")' in inspect.getsource(scan_runner._run_vault_sv9_judgment_shadow_after_publication)


def test_report_store_saves_loads_and_fails_closed_on_corrupt_json(tmp_path, monkeypatch):
    from web import report_store

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.save_report(
        {
            "id": "older",
            "brand_name": "Older",
            "url": "https://older.test",
            "created_at": "2026-01-01T00:00:00+00:00",
            "score": 55,
            "detected_count": 2,
            "block_count": 9,
            "not_detected": ["vision"],
        }
    )
    report_store.save_report(
        {
            "id": "newer",
            "brand_name": "Newer",
            "url": "https://newer.test",
            "created_at": "2026-01-02T00:00:00+00:00",
            "score": 77,
            "detected_count": 4,
            "block_count": 9,
            "not_detected": [],
        }
    )
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    assert report_store.load_report("older")["brand_name"] == "Older"
    assert report_store.load_report("missing") is None
    with pytest.raises(ReportConflictError, match="already exists but is unreadable"):
        report_store.list_reports()


def test_report_store_mirrors_new_reports_and_falls_back_to_files(tmp_path, monkeypatch):
    from web import report_store

    report = {
        "id": "fresh",
        "brand_name": "Fresh",
        "url": "https://fresh.test",
        "created_at": "2026-07-10T10:00:00+00:00",
        "score": 81,
    }

    class Repository:
        def __init__(self):
            self.imported = []

        def import_report(self, payload):
            self.imported.append(payload)

        def get_report_payload(self, report_id):
            return next((item for item in self.imported if item["id"] == report_id), None)

        def list_report_summaries(self, *, limit, offset):
            rows = [report_store._summary_row(item) for item in self.imported]
            return rows[offset : offset + limit]

        def list_report_payloads_for_domain(self, domain, *, limit, offset):
            rows = [item for item in self.imported if report_store.domain_key(item["url"]) == domain]
            return rows[offset : offset + limit]

    repository = Repository()
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    report_store.save_report(report)

    assert repository.imported == [report]
    assert report_store.load_report("fresh") == report
    assert [row["id"] for row in report_store.list_reports()] == ["fresh"]
    assert report_store.list_reports_for_domain("fresh.test") == [report]


def test_report_store_merges_postgres_and_file_reports(tmp_path, monkeypatch):
    from web import report_store

    file_report = {
        "id": "file-only",
        "brand_name": "File",
        "url": "https://same.test",
        "created_at": "2026-07-10T11:00:00+00:00",
    }
    postgres_report = {
        "id": "postgres-only",
        "brand_name": "Postgres",
        "url": "https://same.test",
        "created_at": "2026-07-10T10:00:00+00:00",
    }
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path("file-only").write_text(json.dumps(file_report), encoding="utf-8")
    repository = SimpleNamespace(
        get_report_payload=lambda report_id: postgres_report if report_id == "postgres-only" else None,
        list_report_summaries=lambda *, limit, offset: (
            [report_store._summary_row(postgres_report)][offset : offset + limit]
        ),
        list_report_payloads_for_domain=lambda domain, *, limit, offset: (
            [postgres_report][offset : offset + limit] if domain == "same.test" else []
        ),
    )
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    assert [row["id"] for row in report_store.list_reports()] == ["file-only", "postgres-only"]
    assert [row["id"] for row in report_store.list_reports_for_domain("same.test")] == [
        "file-only",
        "postgres-only",
    ]


def test_report_store_raises_same_id_conflict_after_postgres_payload_load(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    postgres_report = {
        "id": "same-id",
        "brand_name": "Postgres",
        "url": "https://same.test",
        "created_at": "2026-07-10T10:00:00+00:00",
        "score": 70,
    }
    file_report = {**postgres_report, "brand_name": "File", "score": 71}

    repository = SimpleNamespace(
        get_report_payload=lambda report_id: (
            postgres_report if report_id == "same-id" else None
        ),
        list_report_summaries=lambda *, limit, offset: (
            [report_store._summary_row(postgres_report)][offset : offset + limit]
        ),
    )
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path("same-id").write_text(
        json.dumps(file_report),
        encoding="utf-8",
    )
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    with pytest.raises(
        ReportConflictError,
        match="postgres and file stores with different content",
    ):
        report_store.list_reports()


def test_report_store_accepts_identical_postgres_and_file_payloads(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    report = {
        "id": "same-id",
        "brand_name": "Same",
        "url": "https://same.test",
        "created_at": "2026-07-10T10:00:00+00:00",
        "score": 70,
        "raw": {"font_data": "before\x00after"},
    }
    repository = SimpleNamespace(
        get_report_payload=lambda report_id: report if report_id == "same-id" else None,
        list_report_summaries=lambda *, limit, offset: (
            [report_store._summary_row(report)][offset : offset + limit]
        ),
        list_report_payloads_for_domain=lambda domain, *, limit, offset: (
            [report][offset : offset + limit] if domain == "same.test" else []
        ),
    )
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path("same-id").write_text(
        json.dumps(report),
        encoding="utf-8",
    )
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    assert report_store.load_report("same-id") == report
    assert report_store.list_reports() == [report_store._summary_row(report)]
    assert report_store.list_reports_for_domain("same.test") == [report]


def test_report_store_does_not_fallback_after_repository_raw_integrity_conflict(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    file_report = {
        "id": "raw-integrity-conflict",
        "brand_name": "File",
        "url": "https://same.test",
    }

    class Repository:
        def get_report_payload(self, _report_id):
            raise ReportConflictError(
                "report snapshot raw payload failed integrity validation"
            )

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path(file_report["id"]).write_text(
        json.dumps(file_report),
        encoding="utf-8",
    )
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())

    with pytest.raises(
        ReportConflictError,
        match="^report snapshot raw payload failed integrity validation$",
    ):
        report_store.load_report(file_report["id"])


def test_report_store_load_fails_closed_for_malformed_same_id_file_after_postgres_load(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    postgres_report = {
        "id": "malformed-file",
        "brand_name": "Postgres",
        "url": "https://same.test",
        "created_at": "2026-07-10T10:00:00+00:00",
        "score": 70,
    }
    repository = SimpleNamespace(
        get_report_payload=lambda report_id: (
            postgres_report if report_id == "malformed-file" else None
        ),
    )
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path("malformed-file").write_text(
        "{not json",
        encoding="utf-8",
    )
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    with pytest.raises(ReportConflictError, match="already exists but is unreadable"):
        report_store.load_report("malformed-file")


def test_report_store_lists_fail_closed_for_non_object_same_id_file_after_postgres_load(
    tmp_path,
    monkeypatch,
):
    from src.services.scanner_report_assessment import ScannerReportAssessmentError
    from web import report_store

    postgres_report = {
        "id": "non-object-file",
        "brand_name": "Postgres",
        "url": "https://same.test",
        "created_at": "2026-07-10T10:00:00+00:00",
        "score": 70,
    }
    repository = SimpleNamespace(
        get_report_payload=lambda report_id: (
            postgres_report if report_id == "non-object-file" else None
        ),
        list_report_summaries=lambda *, limit, offset: (
            [report_store._summary_row(postgres_report)][offset : offset + limit]
        ),
        list_report_payloads_for_domain=lambda domain, *, limit, offset: (
            [postgres_report][offset : offset + limit]
            if domain == "same.test"
            else []
        ),
    )
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path("non-object-file").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    with pytest.raises(ScannerReportAssessmentError, match="file_report_payload_invalid"):
        report_store.list_reports()
    with pytest.raises(ScannerReportAssessmentError, match="file_report_payload_invalid"):
        report_store.list_reports_for_domain("same.test")


def test_report_store_does_not_fallback_after_malformed_postgres_payload(
    tmp_path,
    monkeypatch,
):
    from src.services.scanner_report_assessment import ScannerReportAssessmentError
    from web import report_store

    file_report = {
        "id": "malformed-postgres",
        "brand_name": "File",
        "url": "https://same.test",
    }
    malformed_postgres = {
        "id": "malformed-postgres",
        "brand_name": "Postgres",
        "url": "https://same.test",
        "sv9_assessment": {"availability": "available"},
    }
    repository = SimpleNamespace(
        get_report_payload=lambda report_id: (
            malformed_postgres if report_id == "malformed-postgres" else None
        ),
    )
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path("malformed-postgres").write_text(
        json.dumps(file_report),
        encoding="utf-8",
    )
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    with pytest.raises(ScannerReportAssessmentError, match="envelope_fields_mismatch"):
        report_store.load_report("malformed-postgres")


def test_report_store_rejects_stripped_assessment_at_file_and_postgres_boundaries(
    tmp_path,
    monkeypatch,
):
    from src.services.scanner_report_assessment import ScannerReportAssessmentError
    from tests.test_vault_sv9_parity import _available_report
    from web import report_store

    report = _available_report("stripped-store-assessment")
    report.pop("sv9_assessment")
    report.pop("assessment_fingerprint")
    report.pop("score_fingerprint")

    class TrackingRepository:
        def __init__(self):
            self.imported = []

        def import_report(self, payload):
            self.imported.append(payload)

        def get_report_payload(self, report_id):
            return report if report_id == "stripped-store-assessment" else None

    repository = TrackingRepository()
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_envelope_missing",
    ):
        report_store.save_report(report)
    assert repository.imported == []
    assert not report_store.report_path(report["id"]).exists()

    path = report_store.report_path(report["id"])
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: None)
    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_envelope_missing",
    ):
        report_store.load_report(report["id"])

    path.unlink()
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)
    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_envelope_missing",
    ):
        report_store.load_report(report["id"])


def test_report_store_rejects_boolean_component_alias_before_write(
    tmp_path,
    monkeypatch,
):
    from src.services.scanner_report_assessment import ScannerReportAssessmentError
    from tests.test_vault_sv9_parity import _available_report
    from web import report_store

    report = _available_report("boolean-store-assessment")
    report["components"][0]["lit"] = False
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_component_lit_mismatch:report",
    ):
        report_store.save_report(report)
    assert not report_store.report_path(report["id"]).exists()


def test_report_store_rejects_type_drift_in_duplicate_assessment_before_write(
    tmp_path,
    monkeypatch,
):
    from src.services.scanner_report_assessment import ScannerReportAssessmentError
    from tests.test_vault_sv9_parity import _available_report
    from web import report_store

    report = _available_report("duplicate-assessment-store")
    report["raw"]["sv9"]["assessment"]["component_breakdown"][0][
        "sin_evidencia_count"
    ] = False
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))

    with pytest.raises(
        ScannerReportAssessmentError,
        match="sv9_assessment_duplicate_mismatch",
    ):
        report_store.save_report(report)
    assert not report_store.report_path(report["id"]).exists()


def test_evidence_ledger_shadow_prefers_matching_persisted_projection(
    tmp_path,
    monkeypatch,
):
    from src.services.evidence_ledger_shadow import build_evidence_ledger_shadow
    from web import report_store

    report = {
        "id": "ledger-one",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-07-10T10:00:00+00:00",
        "reliability_status": "usable",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [
                            {
                                "ref": "web.home",
                                "source": "web",
                                "evidence_type": "owned_copy.homepage",
                                "content": "Stable owned proof.",
                                "url": "https://example.com",
                                "confidence": "high",
                                "metadata": {"source_class": "owned_copy"},
                            }
                        ]
                    }
                }
            }
        },
    }
    persisted = build_evidence_ledger_shadow([report], mode="shadow")
    repository = SimpleNamespace(
        list_report_payloads_for_domain=lambda domain, *, limit, offset: (
            [report][offset : offset + limit] if domain == "example.com" else []
        ),
        get_evidence_ledger_shadow=lambda domain: (
            persisted if domain == "example.com" else None
        ),
    )
    monkeypatch.setenv("B3S_EVIDENCE_LEDGER_MODE", "shadow")
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    result = report_store.evidence_ledger_shadow_for_domain("example.com")

    assert result["state_fingerprint"] == persisted["state_fingerprint"]
    assert result["runtime_effect"] is False
    assert result["persistence"] == {"stored": True, "backend": "postgres"}


def test_evidence_ledger_shadow_recomputes_when_persisted_projection_is_stale(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    report = {
        "id": "ledger-latest",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-07-11T10:00:00+00:00",
        "raw": {},
    }
    repository = SimpleNamespace(
        list_report_payloads_for_domain=lambda domain, *, limit, offset: (
            [report][offset : offset + limit] if domain == "example.com" else []
        ),
        get_evidence_ledger_shadow=lambda _domain: {
            "state_fingerprint": "0" * 64,
            "latest_report_id": "ledger-old",
        },
    )
    monkeypatch.setenv("B3S_EVIDENCE_LEDGER_MODE", "shadow")
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    result = report_store.evidence_ledger_shadow_for_domain("example.com")

    assert result["latest_report_id"] == "ledger-latest"
    assert result["runtime_effect"] is False
    assert result["persistence"] == {
        "stored": False,
        "backend": "history_derived",
    }


def test_claim_tile_ledger_reuses_matching_persisted_projection(
    tmp_path,
    monkeypatch,
):
    from src.services.evidence_claim_tile_ledger import (
        build_evidence_claim_tile_ledger,
    )
    from web import report_store

    claim = "Our mission is to simplify finance."
    report = {
        "id": "claim-tile-one",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-07-11T10:00:00+00:00",
        "raw": {
            "schema_version": "report-v1",
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [
                            {
                                "ref": "web.about",
                                "source": "web",
                                "evidence_type": "raw_input",
                                "url": "https://example.com/about",
                                "content": claim,
                                "metadata": {
                                    "source_class": "owned_copy",
                                    "claim_slot_key": "mission.primary",
                                },
                            }
                        ]
                    }
                }
            },
            "sv9": {
                "evaluator_model": "evaluator-a",
                "result": {
                    "rubric_version": "rubric-v1",
                    "components": {
                        "mission": {
                            "tile_profile": [
                                {
                                    "id": "M1",
                                    "estado": "ok",
                                    "evidencia": claim,
                                }
                            ]
                        }
                    },
                },
            },
        },
    }
    persisted = build_evidence_claim_tile_ledger(
        [report],
        mode="shadow",
    )
    repository = SimpleNamespace(
        list_report_payloads_for_domain=lambda domain, *, limit, offset: (
            [report][offset : offset + limit]
            if domain == "example.com"
            else []
        ),
        get_evidence_claim_tile_ledger=lambda domain: (
            persisted if domain == "example.com" else None
        ),
        get_reviewed_claim_tile_memory_shadow=lambda domain: (
            {
                "runtime_effect": False,
                "authority": False,
                "automatic_tile_effect": False,
                "automatic_scoring_effect": False,
                "reviewed_memory_candidate_version": "1" * 64,
                "accepted_mappings": [
                    {
                        "mapping_id": persisted["mappings"][0][
                            "mapping_id"
                        ]
                    }
                ],
            }
            if domain == "example.com"
            else None
        ),
    )
    monkeypatch.setenv(
        "B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE",
        "shadow",
    )
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        report_store,
        "_postgres_repository",
        lambda: repository,
    )

    result = report_store.evidence_claim_tile_ledger_for_domain(
        "example.com"
    )

    assert result["summary"]["mapping_count"] == 1
    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["persistence"] == {
        "stored": True,
        "backend": "postgres",
    }
    assert result["reviewed_memory"]["available"] is True
    assert result["reviewed_memory"]["persistence"] == {
        "stored": True,
        "backend": "postgres_ledger_and_review_journal",
    }
    assert len(result["reviewed_memory"]["accepted_mappings"]) == 1
    assert claim not in json.dumps(result)


def test_evidence_memory_identity_v2_is_derived_without_authority(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    report = {
        "id": "memory-v2-one",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-07-11T10:00:00+00:00",
        "reliability_status": "usable",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [
                            {
                                "ref": "web.home",
                                "source": "web",
                                "evidence_type": "owned_copy.homepage",
                                "content": "Stable owned proof.",
                                "url": "https://example.com",
                                "metadata": {"source_class": "owned_copy"},
                            }
                        ]
                    }
                }
            }
        },
    }
    repository = SimpleNamespace(
        list_report_payloads_for_domain=lambda domain, *, limit, offset: (
            [report][offset : offset + limit] if domain == "example.com" else []
        ),
        list_current_evidence_memory_adjudications=lambda domain: [],
    )
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    result = report_store.evidence_memory_identity_v2_for_domain("example.com")

    assert result["schema_version"] == "evidence-memory-identity-v2"
    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"]["document_count"] == 1
    assert result["summary"]["passage_count"] == 1
    assert result["persistence"] == {
        "stored": False,
        "backend": "history_derived",
        "adjudications": {
            "stored": True,
            "backend": "postgres",
            "event_count": 0,
        },
    }


def test_evidence_claim_memory_is_derived_without_authority(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    report = {
        "id": "claim-memory-one",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-07-11T10:00:00+00:00",
        "reliability_status": "usable",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [
                            {
                                "ref": "web.home",
                                "source": "web",
                                "evidence_type": "owned_copy.homepage",
                                "content": "Stable owned proof.",
                                "url": "https://example.com",
                                "metadata": {
                                    "source_class": "owned_copy",
                                    "claim_slot_key": "promise.primary",
                                    "claim_type": "promise",
                                },
                            }
                        ]
                    }
                }
            }
        },
    }
    repository = SimpleNamespace(
        list_report_payloads_for_domain=lambda domain, *, limit, offset: (
            [report][offset : offset + limit] if domain == "example.com" else []
        ),
        list_current_evidence_memory_adjudications=lambda domain: [],
        list_current_evidence_claim_reconciliations=lambda domain: [],
    )
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    result = report_store.evidence_claim_memory_for_domain("example.com")

    assert result["schema_version"] == "evidence-claim-memory-v1"
    assert result["runtime_effect"] is False
    assert result["authority"] is False
    assert result["summary"]["claim_slot_count"] == 1
    assert result["summary"]["claim_variant_count"] == 1
    assert result["summary"]["claim_occurrence_count"] == 1
    assert result["persistence"] == {
        "stored": False,
        "backend": "history_derived",
        "adjudications": {
            "stored": True,
            "backend": "postgres",
            "event_count": 0,
        },
        "claim_reconciliations": {
            "stored": True,
            "backend": "postgres",
            "event_count": 0,
        },
    }


def test_claim_reconciliation_write_resolves_relation_type_server_side(
    tmp_path,
    monkeypatch,
):
    from src.services.evidence_claim_reconciliation import (
        EvidenceClaimReconciliationCommand,
    )
    from web import report_store

    captured = {}

    class Repository:
        @staticmethod
        def list_report_payloads_for_domain(_domain, *, limit, offset):
            return []

        @staticmethod
        def append_evidence_claim_reconciliation(
            domain,
            command,
            *,
            relation_type,
        ):
            captured.update(
                domain=domain,
                command=command,
                relation_type=relation_type,
            )
            return {"id": "event-one"}, False

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())
    monkeypatch.setattr(
        report_store,
        "build_evidence_claim_memory",
        lambda *_args, **_kwargs: {
            "slots": [
                {
                    "relation_candidates": [
                        {
                            "relation_candidate_id": "a" * 64,
                            "relation": "replacement_candidate",
                        }
                    ]
                }
            ]
        },
    )
    command = EvidenceClaimReconciliationCommand(
        subject_id="a" * 64,
        decision="accepted",
        expected_current_event_id=None,
        reviewer="gsus",
        reason_code="relation_reviewed",
        rationale="The relation was manually reviewed.",
        evaluator_version="manual-review-v1",
        actor_id="gsus",
        idempotency_key_hash="b" * 64,
        request_fingerprint="c" * 64,
    )

    event, replayed = (
        report_store.append_evidence_claim_reconciliation_for_domain(
            "example.com",
            command,
        )
    )

    assert event == {"id": "event-one"}
    assert replayed is False
    assert captured["domain"] == "example.com"
    assert captured["command"] == command
    assert captured["relation_type"] == "replacement_candidate"


def test_claim_reconciliation_write_rejects_unknown_relation(
    tmp_path,
    monkeypatch,
):
    from src.services.evidence_claim_reconciliation import (
        EvidenceClaimReconciliationCommand,
        EvidenceClaimReconciliationNotFoundError,
    )
    from web import report_store

    class Repository:
        @staticmethod
        def list_report_payloads_for_domain(_domain, *, limit, offset):
            return []

        @staticmethod
        def append_evidence_claim_reconciliation(*_args, **_kwargs):
            raise AssertionError("unknown relation must not reach the journal")

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())
    command = EvidenceClaimReconciliationCommand(
        subject_id="a" * 64,
        decision="accepted",
        expected_current_event_id=None,
        reviewer="gsus",
        reason_code="relation_reviewed",
        rationale="The relation was manually reviewed.",
        evaluator_version="manual-review-v1",
        actor_id="gsus",
        idempotency_key_hash="b" * 64,
        request_fingerprint="c" * 64,
    )

    with pytest.raises(
        EvidenceClaimReconciliationNotFoundError,
        match="does not exist",
    ):
        report_store.append_evidence_claim_reconciliation_for_domain(
            "example.com",
            command,
        )


def test_evidence_adjudication_write_rejects_unknown_projected_subject(
    tmp_path,
    monkeypatch,
):
    from src.services.evidence_memory_adjudication import (
        EvidenceMemoryAdjudicationCommand,
        EvidenceMemoryAdjudicationNotFoundError,
    )
    from web import report_store

    class Repository:
        @staticmethod
        def list_report_payloads_for_domain(_domain, *, limit, offset):
            return []

        @staticmethod
        def append_evidence_memory_adjudication(*_args, **_kwargs):
            raise AssertionError("unknown evidence must not reach the journal")

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())
    command = EvidenceMemoryAdjudicationCommand(
        subject_id="a" * 64,
        decision="accepted",
        expected_current_event_id=None,
        reviewer="reviewer@example.com",
        reason_code="identity_confirmed",
        rationale="The source identifies the scanned brand.",
        evaluator_version="manual-review-v1",
        actor_id="environment-token",
        idempotency_key_hash="b" * 64,
        request_fingerprint="c" * 64,
    )

    with pytest.raises(
        EvidenceMemoryAdjudicationNotFoundError,
        match="does not exist",
    ):
        report_store.append_evidence_memory_adjudication_for_domain(
            "example.com",
            command,
        )


def test_evidence_adjudication_write_never_falls_back_to_json_files(
    tmp_path,
    monkeypatch,
):
    from src.services.evidence_memory_adjudication import (
        EvidenceMemoryAdjudicationCommand,
        EvidenceMemoryAdjudicationUnavailableError,
    )
    from web import report_store

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: None)
    command = EvidenceMemoryAdjudicationCommand(
        subject_id="a" * 64,
        decision="accepted",
        expected_current_event_id=None,
        reviewer="reviewer@example.com",
        reason_code="identity_confirmed",
        rationale="The source identifies the scanned brand.",
        evaluator_version="manual-review-v1",
        actor_id="environment-token",
        idempotency_key_hash="b" * 64,
        request_fingerprint="c" * 64,
    )

    with pytest.raises(
        EvidenceMemoryAdjudicationUnavailableError,
        match="not configured",
    ):
        report_store.append_evidence_memory_adjudication_for_domain(
            "example.com",
            command,
        )

    assert list(tmp_path.iterdir()) == []


def test_report_store_rejects_reused_file_id_with_different_content(tmp_path, monkeypatch):
    from web import report_store

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    original = {"id": "immutable", "brand_name": "Original"}
    report_store.save_report(original)

    with pytest.raises(ReportConflictError, match="different content"):
        report_store.save_report({"id": "immutable", "brand_name": "Changed"})

    assert json.loads(report_store.report_path("immutable").read_text(encoding="utf-8")) == original


def test_report_store_validates_corrupt_existing_file_before_postgres_import(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    class TrackingRepository:
        def __init__(self):
            self.imported = []

        def import_report(self, payload):
            self.imported.append(payload)

    repository = TrackingRepository()
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    path = report_store.report_path("corrupt-before-import")
    path.write_bytes(b"{not json")
    before = path.read_bytes()
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    with pytest.raises(ReportConflictError, match="already exists but is unreadable"):
        report_store.save_report(
            {
                "id": "corrupt-before-import",
                "brand_name": "Changed",
            }
        )

    assert repository.imported == []
    assert path.read_bytes() == before


def test_report_store_does_not_write_file_after_postgres_conflict(tmp_path, monkeypatch):
    from web import report_store

    class ConflictingRepository:
        def import_report(self, payload):
            raise ReportConflictError(f"report {payload['id']} already exists with different content")

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: ConflictingRepository())

    with pytest.raises(ReportConflictError, match="different content"):
        report_store.save_report({"id": "postgres-conflict", "brand_name": "Changed"})

    assert not report_store.report_path("postgres-conflict").exists()


def test_report_store_allows_concurrent_identical_file_write(tmp_path, monkeypatch):
    from web import report_store

    report = {"id": "concurrent", "brand_name": "Same"}
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(report_store.save_report, [report, report]))

    assert outcomes == [None, None]
    assert json.loads(report_store.report_path("concurrent").read_text(encoding="utf-8")) == report


def test_report_store_falls_back_when_postgres_is_unavailable(tmp_path, monkeypatch):
    from web import report_store

    report = {
        "id": "file-fallback",
        "brand_name": "Fallback",
        "url": "https://fallback.test",
        "created_at": "2026-07-10T12:00:00+00:00",
    }

    class UnavailableRepository:
        def get_report_payload(self, report_id):
            raise OSError("postgres unavailable")

        def list_report_summaries(self, *, limit, offset):
            raise OSError("postgres unavailable")

        def list_report_payloads_for_domain(self, domain, *, limit, offset):
            raise OSError("postgres unavailable")

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path(report["id"]).write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: UnavailableRepository())

    assert report_store.load_report(report["id"]) == report
    assert [row["id"] for row in report_store.list_reports()] == [report["id"]]
    assert report_store.list_reports_for_domain("fallback.test") == [report]


def test_report_store_paginates_all_postgres_summaries(tmp_path, monkeypatch):
    from web import report_store

    summaries = [
        {
            "id": f"postgres-{index:03d}",
            "brand_name": "Postgres",
            "url": "https://postgres.test",
            "created_at": f"2026-07-10T12:{index % 60:02d}:00+00:00",
            "not_detected": [],
        }
        for index in range(501)
    ]
    repository = SimpleNamespace(
        list_report_summaries=lambda *, limit, offset: summaries[offset : offset + limit]
    )
    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: repository)

    assert len(report_store.list_reports()) == 501


def test_report_store_index_payloads_bulk_hydrate_once_and_merge_file_store(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    duplicate = {
        "id": "same-id",
        "brand_name": "Same",
        "url": "https://same.test",
        "created_at": "2026-07-10T12:00:00+00:00",
        "score": 70,
    }
    postgres_only = {
        "id": "postgres-only",
        "brand_name": "Postgres",
        "url": "https://postgres.test",
        "created_at": "2026-07-10T11:00:00+00:00",
        "score": 71,
    }
    file_only = {
        "id": "file-only",
        "brand_name": "File",
        "url": "https://file.test",
        "created_at": "2026-07-10T13:00:00+00:00",
        "score": 72,
    }
    calls = {"bulk": 0, "get": []}

    class Repository:
        def list_report_payloads(self, *, limit):
            calls["bulk"] += 1
            assert limit == 1000
            return [duplicate, postgres_only]

        def list_report_summaries(self, *, limit, offset):
            summaries = [report_store._summary_row(item) for item in (duplicate, postgres_only)]
            return summaries[offset : offset + limit]

        def get_report_payload(self, report_id):
            calls["get"].append(report_id)
            raise AssertionError("bulk-covered report must not be hydrated individually")

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path(file_only["id"]).write_text(
        json.dumps(file_only),
        encoding="utf-8",
    )
    report_store.report_path(duplicate["id"]).write_text(
        json.dumps(duplicate),
        encoding="utf-8",
    )
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())

    payloads = report_store.list_report_payloads_for_index()

    assert [item["id"] for item in payloads] == [
        "file-only",
        "same-id",
        "postgres-only",
    ]
    assert calls == {"bulk": 1, "get": []}

    report_store.report_path(duplicate["id"]).write_text(
        json.dumps({**duplicate, "score": 69}),
        encoding="utf-8",
    )
    with pytest.raises(
        ReportConflictError,
        match="postgres and file stores with different content",
    ):
        report_store.list_report_payloads_for_index()


def test_report_store_index_payloads_recovers_ids_beyond_bulk_cap(tmp_path, monkeypatch):
    from web import report_store

    reports = [
        {
            "id": f"report-{index:04d}",
            "brand_name": "Bulk",
            "url": "https://bulk.test",
            "created_at": f"{index:04d}",
            "score": index,
        }
        for index in range(1001)
    ]
    by_id = {item["id"]: item for item in reports}
    calls = {"bulk": 0, "get": []}

    class Repository:
        def list_report_payloads(self, *, limit):
            calls["bulk"] += 1
            assert limit == 1000
            return reports[:limit]

        def list_report_summaries(self, *, limit, offset):
            summaries = [report_store._summary_row(item) for item in reports]
            return summaries[offset : offset + limit]

        def get_report_payload(self, report_id):
            calls["get"].append(report_id)
            return by_id.get(report_id)

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())

    payloads = report_store.list_report_payloads_for_index()

    assert len(payloads) == 1001
    assert payloads[-1]["id"] == "report-0000"
    assert calls == {"bulk": 1, "get": ["report-1000"]}


def test_report_store_index_discards_staged_postgres_rows_after_summary_failure(
    tmp_path,
    monkeypatch,
):
    from web import report_store

    file_report = {
        "id": "file-fallback",
        "brand_name": "File",
        "url": "https://file.test",
        "created_at": "2026-07-10T13:00:00+00:00",
        "score": 72,
    }
    postgres_report = {
        "id": "staged-postgres",
        "brand_name": "Postgres",
        "url": "https://postgres.test",
        "created_at": "2026-07-10T12:00:00+00:00",
        "score": 71,
    }

    class Repository:
        def list_report_payloads(self, *, limit):
            return [postgres_report]

        def list_report_summaries(self, *, limit, offset):
            if offset:
                raise OSError("summary pagination failed")
            return [
                report_store._summary_row(
                    {
                        "id": f"summary-{index:03d}",
                        "brand_name": "Postgres",
                        "url": "https://postgres.test",
                        "created_at": f"2026-07-10T12:{index:02d}:00+00:00",
                        "score": index,
                    }
                )
                for index in range(200)
            ]

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    report_store.report_path(file_report["id"]).write_text(
        json.dumps(file_report),
        encoding="utf-8",
    )
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())

    payloads = report_store.list_report_payloads_for_index()

    assert [item["id"] for item in payloads] == ["file-fallback"]


def test_report_store_index_rejects_cap_without_summary_reader(tmp_path, monkeypatch):
    from src.services.scanner_report_assessment import ScannerReportAssessmentError
    from web import report_store

    reports = [
        {
            "id": f"report-{index:04d}",
            "brand_name": "Bulk",
            "url": "https://bulk.test",
            "created_at": f"{index:04d}",
            "score": index,
        }
        for index in range(1000)
    ]

    class Repository:
        def list_report_payloads(self, *, limit):
            assert limit == 1000
            return reports

    monkeypatch.setenv("B3S_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())

    with pytest.raises(
        ScannerReportAssessmentError,
        match="postgres_report_payloads_incomplete",
    ):
        report_store.list_report_payloads_for_index()


def test_home_renders_report_list(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.list_report_payloads_for_index",
        lambda: [
            {
                "id": "abc123",
                "brand_name": "Vercel",
                "url": "https://vercel.com",
                "score": 88,
                "detected_count": 8,
                "block_count": 9,
                "not_detected": [],
                "created_at": "2026-01-02T12:00:00+00:00",
            }
        ],
    )

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "Scan a brand" in response.text
    assert 'aria-label="B3S home"' in response.text
    assert 'viewBox="0 0 193 63"' in response.text
    assert 'href="/brand/vercel.com?lang=es"' in response.text
    assert 'data-row-href="/brand/vercel.com?lang=es"' in response.text
    assert 'href="/report/abc123"' not in response.text
    assert 'href="/report/abc123/moodboard?lang=es"' not in response.text
    assert "Vercel" in response.text
    assert "88" in response.text
    assert "status-tag status-tag--ok status-tag--filled" in response.text


def test_home_groups_one_bulk_payload_collection_without_domain_loaders(monkeypatch):
    from web.app import _report_rows_for_index
    from web.app import selected_report_for_display as select_reports

    reports = [
        {
            "id": "alpha-new",
            "brand_name": "Alpha",
            "url": "https://alpha.test",
            "created_at": "2026-08-03T00:00:00+00:00",
            "score": 30,
            "components": [],
            "raw": {},
        },
        {
            "id": "alpha-old",
            "brand_name": "Alpha",
            "url": "https://alpha.test",
            "created_at": "2026-08-02T00:00:00+00:00",
            "score": 40,
            "components": [],
            "raw": {},
        },
        {
            "id": "beta-only",
            "brand_name": "Beta",
            "url": "https://www.beta.test",
            "created_at": "2026-08-01T00:00:00+00:00",
            "score": 50,
            "components": [],
            "raw": {},
        },
    ]
    collection_calls = []
    selected_calls = []

    def collect_index_payloads():
        collection_calls.append(True)
        return reports

    def select_once(items, **kwargs):
        selected_calls.append([item["id"] for item in items])
        return select_reports(items, **kwargs)

    monkeypatch.setattr("web.app.list_report_payloads_for_index", collect_index_payloads)
    monkeypatch.setattr(
        "web.app.list_reports_for_domain",
        lambda _domain: pytest.fail("index must not load reports per domain"),
    )
    monkeypatch.setattr("web.app.selected_report_for_display", select_once)

    rows = _report_rows_for_index()

    assert len(collection_calls) == 1
    assert selected_calls == [["alpha-new", "alpha-old"], ["beta-only"]]
    assert [(row["brand_domain"], row["id"]) for row in rows] == [
        ("alpha.test", "alpha-new"),
        ("beta.test", "beta-only"),
    ]


def test_vault_recapture_keeps_selected_sv9_report_id(monkeypatch):
    from web.scan_runner import _vault_published_report_id

    older = {
        "id": "sv9-baseline",
        "brand_name": "Livellup",
        "url": "https://livellup.com",
        "score": 20,
        "created_at": "2026-08-10T20:47:38+00:00",
        "reliability_status": "shadow",
        "canonical_status": "provisional",
        "components": [],
        "raw": {},
    }
    recapture = {
        **older,
        "id": "vault-recapture",
        "score": 3,
        "created_at": "2026-08-19T12:20:12+00:00",
        "canonical_status": "non_canonical",
        "stability": {
            "classification": "contract_mismatch",
            "canonical_status": "non_canonical",
        },
    }
    monkeypatch.setenv("B3S_CANONICAL_ENFORCEMENT_MODE", "repeated")
    monkeypatch.setattr(
        "web.scan_runner.list_reports_for_domain",
        lambda _url: [older],
    )

    assert _vault_published_report_id("https://livellup.com", recapture) == "sv9-baseline"
    assert _vault_published_report_id("https://livellup.com", older) == "sv9-baseline"


def test_scan_runner_has_no_prior_report_tile_lock() -> None:
    import web.scan_runner as scan_runner

    assert not hasattr(scan_runner, "_lock_prior_sv9_tiles")


def test_home_lists_one_selected_analysis_per_brand(monkeypatch):
    from web.app import app

    older = {
        "id": "baseline",
        "brand_name": "Livellup",
        "url": "https://livellup.com",
        "score": 20,
        "detected_count": 4,
        "block_count": 9,
        "not_detected": ["vision"],
        "created_at": "2026-08-10T20:47:38+00:00",
        "reliability_status": "shadow",
        "canonical_status": "provisional",
        "components": [],
        "raw": {},
    }
    newer = {
        **older,
        "id": "later",
        "score": 3,
        "detected_count": 0,
        "block_count": 0,
        "not_detected": [],
        "created_at": "2026-08-19T12:20:12+00:00",
        "canonical_status": "non_canonical",
        "stability": {
            "classification": "contract_mismatch",
            "canonical_status": "non_canonical",
        },
    }

    monkeypatch.setenv("B3S_CANONICAL_ENFORCEMENT_MODE", "repeated")
    monkeypatch.setattr("web.app.list_report_payloads_for_index", lambda: [newer, older])
    monkeypatch.setattr(
        "web.app.list_reports_for_domain",
        lambda _domain: [newer, older],
    )

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.text.count("https://livellup.com") == 1
    assert "<strong>20</strong>" in response.text
    assert "diagnóstico" not in response.text
    assert 'data-row-href="/brand/livellup.com?lang=es"' in response.text


def test_home_publishes_gemini_sv9_over_semantic_v3_baseline(monkeypatch):
    from web.app import app

    semantic = {
        "id": "semantic-v3",
        "brand_name": "Leeters",
        "url": "https://leeters.com",
        "score": 3,
        "detected_count": 3,
        "block_count": 9,
        "not_detected": [],
        "created_at": "2026-08-20T07:00:00+00:00",
        "reliability_status": "shadow",
        "canonical_status": "provisional",
        "components": [],
        "raw": {
            "sv9": {
                "result": {
                    "evaluator_model": "evidence-vault-semantic-scoring-v3",
                }
            }
        },
    }
    sv9 = {
        **semantic,
        "id": "gemini-sv9",
        "score": 38,
        "detected_count": 7,
        "created_at": "2026-08-20T07:17:22+00:00",
        "canonical_status": "non_canonical",
        "raw": {
            "sv9": {
                "result": {"evaluator_model": "gemini-3.1-flash-lite"}
            }
        },
        "stability": {
            "classification": "contract_mismatch",
            "canonical_status": "non_canonical",
        },
    }

    monkeypatch.setenv("B3S_CANONICAL_ENFORCEMENT_MODE", "repeated")
    monkeypatch.setattr("web.app.list_report_payloads_for_index", lambda: [sv9, semantic])
    monkeypatch.setattr(
        "web.app.list_reports_for_domain",
        lambda _domain: [sv9, semantic],
    )

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.text.count("https://leeters.com") == 1
    assert "<strong>38</strong>" in response.text
    assert "<strong>3</strong>" not in response.text
    assert "diagnóstico" not in response.text


def test_home_hides_a_later_drifted_scan_behind_selected_score(monkeypatch):
    from web.app import app

    baseline = {
        "id": "baseline",
        "brand_name": "SoccerSolver",
        "url": "https://soccersolver.com",
        "score": 80,
        "detected_count": 8,
        "block_count": 9,
        "not_detected": [],
        "created_at": "2026-08-01T12:00:00+00:00",
        "reliability_status": "shadow",
        "canonical_status": "provisional",
        "components": [],
        "raw": {},
    }
    drifted = {
        **baseline,
        "id": "drifted",
        "score": 64,
        "created_at": "2026-08-03T12:00:00+00:00",
        "canonical_status": "non_canonical",
        "stability": {
            "classification": "evaluation_drift",
            "canonical_status": "non_canonical",
        },
    }

    monkeypatch.setenv("B3S_CANONICAL_ENFORCEMENT_MODE", "repeated")
    monkeypatch.setattr("web.app.list_report_payloads_for_index", lambda: [drifted, baseline])
    monkeypatch.setattr(
        "web.app.list_reports_for_domain",
        lambda _domain: [drifted, baseline],
    )

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.text.count("https://soccersolver.com") == 1
    assert "<strong>80</strong>" in response.text
    assert "<strong>64</strong>" not in response.text
    assert "diagnóstico" not in response.text


def test_visual_signature_logo_url_falls_back_to_real_logo_candidate():
    from web.app import _visual_signature_logo_url

    visual_payload = {
        "schema_version": "visual-signature-persistence-1",
        "visual_signature_evidence": {
            "schema_version": "visual-signature-evidence-v1",
            "identity": {
                "candidates": [
                    {
                        "url": "https://stabolut.com/assets/brand-mark.svg",
                        "role": "real_logo",
                        "location": "header",
                        "confidence": 0.91,
                    }
                ]
            },
        },
    }
    report = {
        "id": "scan123",
        "brand_name": "Stabolut",
        "url": "https://stabolut.com",
        "score": 79,
        "blocks": [],
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [
                            {
                                "source": "visual_acquisition",
                                "evidence_type": "raw_input",
                                "content": json.dumps(visual_payload),
                            }
                        ]
                    }
                }
            }
        },
    }

    assert _visual_signature_logo_url(report) == "https://stabolut.com/assets/brand-mark.svg"


def test_brand_view_renders_profile_from_matching_reports(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.brand_navigation_history_for_domain",
        lambda domain: ([
            {
                "id": "report123",
                "brand_name": "Stabolut",
                "url": "https://stabolut.com",
                "created_at": "2026-07-03T12:00:00+00:00",
                "score": 79,
                "immediate_margin": 8,
                "most_painful_gap_label": "Propósito",
                "not_detected": ["values"],
                "components": [
                    {
                        "key": "core_purpose",
                        "status": "scored",
                        "resumen": "Stablecoin platform.",
                        "veredicto": "Purpose needs sharper proof.",
                        "block": {"refs": [{"url": "https://stabolut.com"}]},
                    }
                ],
            }
        ], []),
    )

    response = TestClient(app).get("/brand/stabolut.com?lang=es")

    assert response.status_code == 200
    assert "Stabolut" in response.text
    assert "Stablecoin platform." in response.text
    assert 'href="/report/report123"' in response.text
    assert "79" in response.text


def test_brand_view_exposes_persistent_tile_memory_only_in_vault(monkeypatch):
    from web.app import app

    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(
        "web.app.vault_accepted_report_for_domain",
        lambda _domain, *, reports: reports[0],
    )
    monkeypatch.setattr(
        "web.app.brand_navigation_history_for_domain",
        lambda _domain: ([
            {
                "id": "latest",
                "brand_name": "Example",
                "url": "https://example.com",
                "created_at": "2026-07-31T12:00:00+00:00",
                "score": 64,
                "components": [],
            }
        ], []),
    )
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain, *, reports, postgres_reports: {
            "report_count": 2,
            "memory_version": "a" * 64,
            "persistence": {"stored": True, "backend": "postgres_history_derived"},
            "summary": {
                "accepted_evidence_count": 4,
            },
            "recoveries": [{"tile_key": "mission.M1"}],
            "conflicts": [],
            "scoring": {
                "current_score": 64,
                "preview_score": 66,
                "score_delta": 2,
            },
            "reviewed_shadow": {
                "scoring": {
                    "current_score": 64,
                    "preview_score": 64,
                    "score_delta": 0,
                }
            },
            "recovery_review_candidates": [
                {
                    "case_id": "historical-recovery",
                    "tile": {"tile_key": "coherencia.C8"},
                }
            ],
            "recovery_review": {
                "summary": {
                    "candidate_count": 1,
                    "pending_count": 1,
                }
            },
            "tile_evolution": {
                "tile_memory_version": "b" * 64,
                "summary": {
                    "tile_count": 70,
                    "stable_tile_count": 68,
                    "changed_tile_count": 2,
                    "score_affecting_change_count": 1,
                    "evidence_quality_change_count": 1,
                    "pending_review_count": 2,
                    "incompatible_report_count": 0,
                },
                "score_trajectory": {
                    "previous_score": 62,
                    "latest_score": 64,
                    "score_delta_latest_minus_previous": 2,
                },
                "changes": [
                    {
                        "tile_key": "mission.M1",
                        "impact_kind": "tile_improved",
                        "previous_state": "sin_evidencia",
                        "current_state": "ok",
                        "review_priority": "score_affecting",
                        "validation_state": "pending_review",
                    },
                    {
                        "tile_key": "value_proposition.P3",
                        "impact_kind": "evidence_weakened",
                        "previous_state": "ok",
                        "current_state": "ok",
                        "review_priority": "evidence_quality",
                        "validation_state": "pending_review",
                    },
                ],
            },
        },
    )
    monkeypatch.setattr(
        "web.app.evidence_claim_tile_ledger_for_domain",
        lambda _domain, *, reports: {
            "persistence": {"stored": True, "backend": "postgres"},
            "summary": {
                "mapping_count": 3,
                "current_series_mapping_count": 2,
                "claim_variant_count": 2,
                "tile_count": 3,
            },
            "reviewed_memory": {
                "available": True,
                "summary": {
                    "accepted_mapping_count": 1,
                    "pending_mapping_count": 1,
                },
            },
        },
    )

    response = TestClient(app).get("/brand/example.com?lang=es")

    assert response.status_code == 200
    assert "memoria_de_baldosas" in response.text
    assert "baldosa mejorada" in response.text
    assert "evidencia debilitada" in response.text
    assert "mission.M1" in response.text
    assert "runtime_effect=false" in response.text
    assert "authority=false" in response.text
    assert "memoria vault" in response.text
    assert "cola_de_revision_vault" in response.text
    assert "score y calidad de evidencia" in response.text
    assert "candidatos protegidos pendientes" in response.text
    assert 'href="/vault/review/example.com"' in response.text
    assert response.text.count("value_proposition.P3") == 1
    assert "bloqueada" in response.text


def test_brand_view_shares_loaded_history_with_vault_projections(monkeypatch):
    from web.app import _brand_profile

    reports = [
        {
            "id": "latest",
            "brand_name": "Example",
            "url": "https://example.com",
            "created_at": "2026-07-31T12:00:00+00:00",
            "score": 64,
            "components": [],
        }
    ]
    calls: list[tuple[str, object]] = []
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(
        "web.app.vault_accepted_report_for_domain",
        lambda _domain, *, reports: reports[0],
    )

    def load_history(_domain):
        calls.append(("history", None))
        return reports, reports

    def preview(_domain, *, reports, postgres_reports):
        calls.append(("preview", reports))
        assert postgres_reports is reports
        return {
            "report_count": 1,
            "summary": {},
            "tile_evolution": {"summary": {}, "changes": []},
        }

    def ledger(_domain, *, reports):
        calls.append(("ledger", reports))
        return {"summary": {}, "reviewed_memory": {"available": False}}

    monkeypatch.setattr("web.app.brand_navigation_history_for_domain", load_history)
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain", preview
    )
    monkeypatch.setattr("web.app.evidence_claim_tile_ledger_for_domain", ledger)

    profile = _brand_profile("example.com")

    assert profile["current"]["id"] == "latest"
    assert profile["vault_memory"]["report_count"] == 1
    assert [name for name, _value in calls] == ["history", "preview", "ledger"]
    assert calls[1][1] is reports
    assert calls[2][1] is reports


def test_vault_navigation_uses_accepted_authority_report_not_history(monkeypatch):
    from web import report_store

    accepted_fingerprint = "a" * 64
    accepted = {
        "id": "accepted",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-09-08T12:00:00+00:00",
        "score": 58,
    }
    reports = [
        {**accepted, "id": "latest", "created_at": "2026-09-08T13:00:00+00:00", "score": 68},
        accepted,
        {**accepted, "id": "legacy", "created_at": "2026-07-21T12:00:00+00:00", "score": 53},
    ]

    class Repository:
        def get_evidence_vault_sv9_judgment_authority(self, _domain, **_kwargs):
            return {
                "accepted_candidate": {
                    "source_scan_id": "accepted",
                    "assessment_fingerprint": accepted_fingerprint,
                },
                "assessment": {
                    "assessment_fingerprint": accepted_fingerprint,
                    "sv9_score": 58,
                },
                "score": 58,
            }

    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())
    monkeypatch.setattr(
        report_store,
        "assessment_projection_from_report",
        lambda _report, required=False: {
            "availability": "available",
            "assessment_fingerprint": accepted_fingerprint,
            "sv9_score": 58,
        },
    )

    selected = report_store.vault_accepted_report_for_domain(
        "example.com",
        reports=reports,
    )

    assert selected["id"] == "accepted"


@pytest.mark.parametrize(
    "authority",
    [None, RuntimeError("database unavailable")],
)
def test_vault_navigation_fails_closed_without_authority(monkeypatch, authority):
    from web import report_store

    class Repository:
        def get_evidence_vault_sv9_judgment_authority(self, _domain, **_kwargs):
            if isinstance(authority, Exception):
                raise authority
            return authority

    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())

    assert (
        report_store.vault_accepted_report_for_domain(
            "example.com",
            reports=[{"id": "legacy", "url": "https://example.com"}],
        )
        is None
    )


def test_vault_navigation_fails_closed_on_assessment_mismatch(monkeypatch):
    from web import report_store

    fingerprint = "a" * 64

    class Repository:
        def get_evidence_vault_sv9_judgment_authority(self, _domain, **_kwargs):
            return {
                "accepted_candidate": {
                    "source_scan_id": "accepted",
                    "assessment_fingerprint": fingerprint,
                },
                "assessment": {
                    "assessment_fingerprint": fingerprint,
                    "sv9_score": 58,
                },
                "score": 58,
            }

    monkeypatch.setattr(report_store, "_postgres_repository", lambda: Repository())
    monkeypatch.setattr(
        report_store,
        "assessment_projection_from_report",
        lambda _report, required=False: {
            "availability": "available",
            "assessment_fingerprint": "b" * 64,
            "sv9_score": 58,
        },
    )

    assert (
        report_store.vault_accepted_report_for_domain(
            "example.com",
            reports=[{"id": "accepted", "url": "https://example.com"}],
        )
        is None
    )


def test_vault_profile_preserves_history_when_authority_is_unavailable(monkeypatch):
    from web.app import _brand_profile, app

    latest = {
        "id": "latest",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-09-08T13:00:00+00:00",
        "score": 68,
        "components": [],
    }
    legacy = {**latest, "id": "legacy", "created_at": "2026-07-21T12:00:00+00:00", "score": 53}
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr("web.app.brand_navigation_history_for_domain", lambda _domain: ([latest, legacy], []))
    monkeypatch.setattr("web.app.vault_accepted_report_for_domain", lambda _domain, *, reports: None)

    profile = _brand_profile("example.com")

    assert profile["current"] is None
    assert profile["latest_attempt"]["id"] == "latest"
    assert [row["id"] for row in profile["reports"]] == ["latest", "legacy"]
    assert profile["vault_current_status"] == "unavailable"
    response = TestClient(app).get("/brand/example.com?lang=es")
    assert response.status_code == 200
    assert "autoridad no disponible" in response.text
    assert "ningún informe es actual" in response.text


def test_vault_index_preserves_latest_history_row_without_calling_it_current(monkeypatch):
    from web.app import _report_rows_for_index, app

    latest = {
        "id": "latest",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-09-08T13:00:00+00:00",
        "score": 68,
    }
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr("web.app.list_report_payloads_for_index", lambda: [latest])
    monkeypatch.setattr("web.app.vault_accepted_report_for_domain", lambda _domain, *, reports: None)

    rows = _report_rows_for_index()

    assert len(rows) == 1
    assert rows[0]["id"] == "latest"
    assert rows[0]["vault_current_available"] is False
    assert rows[0]["vault_current_status"] == "unavailable"
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "Vault: autoridad no disponible; no es actual" in response.text


def test_vault_current_report_state_uses_accepted_identity(monkeypatch):
    from web import report_store

    reports = [
        {"id": "latest", "url": "https://example.com", "created_at": "2026-09-08T13:00:00+00:00", "score": 68},
        {"id": "accepted", "url": "https://example.com", "created_at": "2026-09-08T12:00:00+00:00", "score": 58},
        {"id": "legacy", "url": "https://example.com", "created_at": "2026-07-21T12:00:00+00:00", "score": 53},
    ]
    accepted = reports[1]
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(report_store, "list_reports_for_domain", lambda _domain: reports)
    monkeypatch.setattr(
        report_store,
        "vault_accepted_report_for_domain",
        lambda _domain, *, reports: accepted,
    )

    selected, _classified, state = report_store.current_report_for_domain("example.com")

    assert selected["id"] == "accepted"
    assert state["selected_report_id"] == "accepted"
    assert state["canonical_report_id"] == "accepted"
    assert state["provisional_report_id"] is None


def test_vault_profile_distinguishes_accepted_latest_and_legacy_reports(monkeypatch):
    from web.app import _brand_profile, app

    legacy = {
        "id": "legacy",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-07-21T12:00:00+00:00",
        "score": 53,
        "components": [],
    }
    accepted = {**legacy, "id": "accepted", "created_at": "2026-09-08T12:00:00+00:00", "score": 58}
    latest = {**legacy, "id": "latest", "created_at": "2026-09-08T13:00:00+00:00", "score": 68}
    reports = [latest, accepted, legacy]
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr("web.app.brand_navigation_history_for_domain", lambda _domain: (reports, []))
    monkeypatch.setattr("web.app.vault_accepted_report_for_domain", lambda _domain, *, reports: accepted)
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain, *, reports, postgres_reports: {"summary": {}, "tile_evolution": {"summary": {}, "changes": []}},
    )
    monkeypatch.setattr(
        "web.app.evidence_claim_tile_ledger_for_domain",
        lambda _domain, *, reports: {"summary": {}, "reviewed_memory": {"available": False}},
    )

    profile = _brand_profile("example.com")

    assert profile["current"]["id"] == "accepted"
    assert profile["latest_attempt"]["id"] == "latest"
    assert [row["id"] for row in profile["reports"]] == ["latest", "accepted", "legacy"]
    response = TestClient(app).get("/brand/example.com?lang=es")
    assert "aceptado por Vault" in response.text
    assert "último intento" in response.text


def test_vault_historical_report_links_to_accepted_report(monkeypatch):
    from tests.test_vault_sv9_parity import _available_report
    from web.app import app

    historical = _available_report("legacy")
    accepted = _available_report("accepted")
    historical["created_at"] = "2026-07-21T12:00:00+00:00"
    accepted["created_at"] = "2026-09-08T12:00:00+00:00"
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr("web.app.load_report", lambda _scan_id: historical)
    monkeypatch.setattr("web.app.list_reports_for_domain", lambda _domain: [accepted, historical])
    monkeypatch.setattr("web.app.vault_accepted_report_for_domain", lambda _domain, *, reports: accepted)

    response = TestClient(app).get("/report/legacy")

    assert response.status_code == 200
    assert "histórico · no actual" in response.text
    assert 'href="/report/accepted"' in response.text


def test_vault_report_labels_unavailable_authority_without_fallback(monkeypatch):
    from tests.test_vault_sv9_parity import _available_report
    from web.app import app

    historical = _available_report("legacy")
    latest = _available_report("latest")
    historical["created_at"] = "2026-07-21T12:00:00+00:00"
    latest["created_at"] = "2026-09-08T13:00:00+00:00"
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr("web.app.load_report", lambda _scan_id: historical)
    monkeypatch.setattr("web.app.list_reports_for_domain", lambda _domain: [latest, historical])
    monkeypatch.setattr("web.app.vault_accepted_report_for_domain", lambda _domain, *, reports: None)

    response = TestClient(app).get("/report/legacy")

    assert response.status_code == 200
    assert "Vault: autoridad no disponible; ningún informe es actual" in response.text
    assert "histórico · no actual" in response.text


def test_brand_view_hides_vault_memory_outside_vault(monkeypatch):
    from web.app import app

    monkeypatch.setenv("BRAND3_ENVIRONMENT", "production")
    monkeypatch.setattr(
        "web.app.brand_navigation_history_for_domain",
        lambda _domain: ([
            {
                "id": "latest",
                "brand_name": "Example",
                "url": "https://example.com",
                "created_at": "2026-07-31T12:00:00+00:00",
                "score": 64,
                "components": [],
            }
        ], []),
    )
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain, *, reports, postgres_reports: pytest.fail("production must not build the Vault preview"),
    )
    monkeypatch.setattr(
        "web.app.evidence_claim_tile_ledger_for_domain",
        lambda _domain, *, reports: pytest.fail("production must not read the Vault ledger"),
    )

    response = TestClient(app).get("/brand/example.com?lang=es")

    assert response.status_code == 200
    assert "memoria_de_baldosas" not in response.text
    assert "memoria vault" not in response.text


def test_brand_view_counts_unreviewed_vault_mappings_as_pending(monkeypatch):
    from web.app import app

    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(
        "web.app.brand_navigation_history_for_domain",
        lambda _domain: ([
            {
                "id": "latest",
                "brand_name": "Example",
                "url": "https://example.com",
                "created_at": "2026-07-31T12:00:00+00:00",
                "score": 64,
                "components": [],
            }
        ], []),
    )
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain, *, reports, postgres_reports: {
            "report_count": 1,
            "summary": {},
            "tile_evolution": {"summary": {}, "changes": []},
        },
    )
    monkeypatch.setattr(
        "web.app.evidence_claim_tile_ledger_for_domain",
        lambda _domain, *, reports: {
            "persistence": {"stored": True, "backend": "postgres"},
            "summary": {
                "mapping_count": 3,
                "current_series_mapping_count": 1,
                "claim_variant_count": 2,
                "tile_count": 3,
            },
            "reviewed_memory": {"available": False},
        },
    )

    response = TestClient(app).get("/brand/example.com?lang=es")

    assert response.status_code == 200
    assert "<dt>mappings</dt><dd>3</dd>" in response.text
    assert "<dt>aceptados</dt><dd>0</dd>" in response.text
    assert "<dt>pendientes</dt><dd>3</dd>" in response.text


def test_vault_score_review_queue_uses_existing_validation_channels():
    from web.app import _vault_score_review_item

    base = {
        "tile_key": "mission.M1",
        "previous_state": "sin_evidencia",
        "current_state": "ok",
        "review_priority": "score_affecting",
        "validation_state": "pending_review",
        "current_source_evidence_ids": ["evidence-1"],
        "added_source_evidence_ids": ["evidence-1"],
    }

    recovery = _vault_score_review_item(
        {
            **base,
            "previous_state": "ok",
            "current_state": "sin_evidencia",
            "validation_channel": "scoring_recovery_review",
        },
        impact_label="hueco de adquisición",
        impact_tone="warn",
        recovery_candidates=[{"case_id": "recovery-1"}],
        current_mappings=[],
    )
    mapped = _vault_score_review_item(
        {**base, "validation_channel": "claim_tile_review"},
        impact_label="baldosa mejorada",
        impact_tone="ok",
        recovery_candidates=[],
        current_mappings=[
            {
                "tile_key": "mission.M1",
                "polarity": "supports",
                "source_evidence_id": "evidence-1",
            }
        ],
    )
    missing_mapping = _vault_score_review_item(
        {**base, "validation_channel": "claim_tile_review"},
        impact_label="baldosa mejorada",
        impact_tone="ok",
        recovery_candidates=[],
        current_mappings=[],
    )
    evidence_gap = _vault_score_review_item(
        {
            **base,
            "previous_state": "ok",
            "current_state": "no",
            "validation_channel": "evidence_gap_review",
        },
        impact_label="negativo sin validar",
        impact_tone="bad",
        recovery_candidates=[],
        current_mappings=[],
    )
    corroboration = _vault_score_review_item(
        {
            **base,
            "review_priority": "evidence_quality",
            "validation_channel": "claim_corroboration_review",
        },
        impact_label="evidencia reforzada",
        impact_tone="ok",
        recovery_candidates=[],
        current_mappings=[],
    )

    assert recovery["queue_state"] == "ready"
    assert mapped["queue_state"] == "preparation_required"
    assert missing_mapping["queue_state"] == "blocked"
    assert evidence_gap["queue_state"] == "blocked"
    assert "Recapturar evidencia" in evidence_gap["next_action"]
    assert corroboration["queue_state"] == "preparation_required"
    assert corroboration["review_priority"] == "evidence_quality"
    assert "claim-scoped" in corroboration["next_action"]


def test_brand_view_repeated_mode_keeps_selected_baseline_visible(monkeypatch):
    from web.app import _brand_profile

    older = {
        "id": "baseline",
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": "2026-07-01T00:00:00Z",
        "score": 56,
        "reliability_status": "shadow",
        "components": [],
        "raw": {},
    }
    newer = {
        **older,
        "id": "drifted",
        "created_at": "2026-07-02T00:00:00Z",
        "score": 40,
        "components": [{"key": "value_proposition", "status": "scored", "score": 0}],
    }
    monkeypatch.setenv("B3S_CANONICAL_ENFORCEMENT_MODE", "repeated")
    monkeypatch.setattr(
        "web.app.brand_navigation_history_for_domain",
        lambda _domain: ([newer, older], []),
    )

    profile = _brand_profile("example.com")

    assert profile["current"]["id"] == "baseline"
    assert profile["current"]["score"] == 56
    assert profile["current"]["score_publication"]["publishable"] is True
    assert profile["latest_attempt"]["id"] == "drifted"
    assert profile["latest_attempt"]["score_publication"]["publishable"] is False
    assert profile["enforcement_mode"] == "repeated"


def test_brand_view_prefers_component_editorial_message(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.brand_navigation_history_for_domain",
        lambda domain: ([
            {
                "id": "report123",
                "brand_name": "Optiak",
                "url": "https://optiak.com",
                "created_at": "2026-07-07T12:00:00+00:00",
                "score": 65,
                "components": [
                    {
                        "key": "core_purpose",
                        "status": "scored",
                        "resumen": "Componente Propósito detectado: 7/10 baldosas encendidas.",
                        "resumen_is_fallback": True,
                        "veredicto": "Propósito técnico sólido.",
                        "message": "Lectura editorial de propósito.",
                        "block": None,
                    },
                    {
                        "key": "value_proposition",
                        "status": "scored",
                        "resumen": "Componente Propuesta de valor detectado.",
                        "resumen_is_fallback": True,
                        "veredicto": "Propuesta aún demasiado técnica.",
                        "message": "Lectura editorial de propuesta.",
                        "block": None,
                    },
                ],
                "blocks": [],
                "raw": {},
            }
        ], []),
    )

    response = TestClient(app).get("/brand/optiak.com?lang=es")

    assert response.status_code == 200
    assert "Lectura editorial de propósito." in response.text
    assert "Lectura editorial de propuesta." in response.text
    assert "Componente Propósito detectado" not in response.text


def test_brand_view_embeds_visual_module_from_latest_report(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.brand_navigation_history_for_domain",
        lambda domain: ([
            {
                "id": "report123",
                "brand_name": "Stabolut",
                "url": "https://stabolut.com",
                "created_at": "2026-07-03T12:00:00+00:00",
                "score": 79,
                "components": [],
                "blocks": [{"name": "brand_idea", "detected": True, "content": "Beyond digital dollars."}],
                "raw": {
                    "flow": {
                        "candidate": {
                            "evidence_pack": {
                                "evidence": [
                                    {
                                        "source": "web",
                                        "evidence_type": "raw_input",
                                        "content": "![Layered cube](https://stabolut.com/assets/card-overcollateralized.png)",
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ], []),
    )

    response = TestClient(app).get("/brand/stabolut.com?lang=es")

    assert response.status_code == 200
    assert "módulo_visual" in response.text
    assert "moodboard-stage--brand" in response.text
    assert "data-moodboard-stage" in response.text
    assert "/static/brand3_cloud.js" in response.text
    assert "/static/moodboard.js" in response.text
    assert "assets de marca seleccionados" in response.text
    assert '<div class="moodboard-strip"' not in response.text
    assert "https://stabolut.com/assets/card-overcollateralized.png" in response.text
    assert 'href="#modulo-visual"' in response.text


def test_brand_view_prefers_structured_web_capture_for_visual_module(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.brand_navigation_history_for_domain",
        lambda domain: ([
            {
                "id": "report-rich-web",
                "brand_name": "Acme",
                "url": "https://acme.com",
                "created_at": "2026-07-28T12:00:00+00:00",
                "score": 79,
                "components": [],
                "blocks": [],
                "raw": {
                    "raw_inputs": [
                        {
                            "source": "web",
                            "payload": {
                                "url": "https://acme.com",
                                "canonical_url": "https://www.acme.com",
                                "html": (
                                    '<section class="hero">'
                                    '<img src="/brand-story.jpg" alt="Acme story" width="1200" height="800">'
                                    "</section>"
                                ),
                                "markdown_content": (
                                    "![Ignored investor]"
                                    "(https://acme.com/investor-logo.svg)"
                                ),
                            },
                        }
                    ],
                    "flow": {
                        "candidate": {
                            "evidence_pack": {
                                "evidence": [
                                    {
                                        "source": "web",
                                        "evidence_type": "raw_input",
                                        "content": "![Legacy](https://acme.com/legacy-only.jpg)",
                                    }
                                ]
                            }
                        }
                    },
                },
            }
        ], []),
    )

    response = TestClient(app).get("/brand/acme.com?lang=es")

    assert response.status_code == 200
    assert "https://www.acme.com/brand-story.jpg" in response.text
    assert "https://acme.com/legacy-only.jpg" not in response.text
    assert "https://acme.com/investor-logo.svg" not in response.text
    assert "visual-assets-v2" in response.text


def test_brand_view_handles_missing_scan(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.brand_navigation_history_for_domain",
        lambda _domain: ([], []),
    )

    response = TestClient(app).get("/brand/stabolut.com?lang=es")

    assert response.status_code == 200
    assert "stabolut.com" in response.text
    assert "sin scan local" in response.text
    assert 'name="url" value="https://stabolut.com"' in response.text


def test_api_health_page_renders_config_without_secrets(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app._api_health",
        lambda run_checks=False: {
            "status": "ok",
            "run_checks": run_checks,
            "checked_at": "2026-07-04T00:00:00+00:00",
            "services": [
                {
                    "key": "firecrawl_scrape",
                    "label": "Firecrawl scrape",
                    "env_var": "FIRECRAWL_API_KEY",
                    "configured": True,
                    "enabled": True,
                    "status": "configured",
                    "detail": "",
                    "elapsed_ms": None,
                    "secret_suffix": "507d",
                }
            ],
        },
    )

    response = TestClient(app).get("/health/apis")

    assert response.status_code == 200
    assert "Estado de APIs" in response.text
    assert "Firecrawl scrape" in response.text
    assert "••••507d" in response.text
    assert "run checks" in response.text


def test_api_health_json_can_run_checks(monkeypatch):
    from web.app import app

    calls = []
    monkeypatch.setattr(
        "web.app._api_health",
        lambda run_checks=False: calls.append(run_checks)
        or {
            "status": "ok",
            "run_checks": run_checks,
            "checked_at": "2026-07-04T00:00:00+00:00",
            "services": [],
        },
    )

    response = TestClient(app).get("/api/health/apis?run=1")

    assert response.status_code == 200
    assert response.json()["run_checks"] is True
    assert calls == [True]


def test_disabled_api_health_service_does_not_degrade_without_secret():
    from web.app import _overall_api_status, _service_row

    row = _service_row(
        key="searchapi",
        label="SearchAPI fallback",
        env_var="SEARCHAPI_API_KEY",
        secret="",
        enabled=False,
        run_checks=False,
        check_fn=None,
    )

    assert row["status"] == "disabled"
    assert _overall_api_status([row]) == "ok"


def test_scan_submission_redirects_to_scan_view(monkeypatch):
    from web.app import app

    calls = []

    def fake_start_scan(url: str, brand_name: str = "", *, allow_degraded_fallback: bool = False) -> str:
        calls.append((url, brand_name, allow_degraded_fallback))
        return "scan123"

    monkeypatch.setattr("web.app.start_scan", fake_start_scan)

    response = TestClient(app).post(
        "/scan",
        data={"url": "https://vercel.com", "brand_name": "Vercel"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/scan/scan123"
    assert calls == [("https://vercel.com", "Vercel", False)]


def test_scan_submission_can_preapprove_degraded_fallback(monkeypatch):
    from web.app import app

    calls = []

    def fake_start_scan(url: str, brand_name: str = "", *, allow_degraded_fallback: bool = False) -> str:
        calls.append((url, brand_name, allow_degraded_fallback))
        return "scan123"

    monkeypatch.setattr("web.app.start_scan", fake_start_scan)

    response = TestClient(app).post(
        "/scan",
        data={
            "url": "https://vercel.com",
            "brand_name": "Vercel",
            "allow_degraded_fallback": "true",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert calls == [("https://vercel.com", "Vercel", True)]


def test_acquisition_gate_blocks_web_failure_without_safe_fallback():
    from web.scan_runner import _build_acquisition_gate

    gate = _build_acquisition_gate(
        {
            "web": {"status": "error", "error": "timeout", "details": {"reason": "timeout"}},
            "exa": {"status": "ok"},
        }
    )

    assert gate["state"] == "blocked"
    assert gate["can_continue"] is False
    assert gate["issues"][0]["code"] == "web_capture_failed"


def test_acquisition_gate_allows_exa_fallback_when_searchapi_is_available(monkeypatch):
    from web.scan_runner import _approve_acquisition_gate, _build_acquisition_gate

    monkeypatch.setattr("src.config.SEARCHAPI_API_KEY", "search-key")
    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "missing_key", "error": "EXA_API_KEY not set"},
            "searchapi": {"status": "ok", "details": {"result_total": 4}},
        }
    )

    assert gate["state"] == "blocked"
    assert gate["can_continue"] is True
    assert gate["fallbacks"][0]["source"] == "searchapi"

    approved = _approve_acquisition_gate(gate, decision_source="user")
    assert approved["state"] == "degraded_approved"
    assert approved["fallbacks"][0]["approved"] is True


def test_acquisition_gate_treats_exa_empty_as_warning():
    from web.scan_runner import _build_acquisition_gate

    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "empty", "details": {"no_result_intents": ["news"]}},
            "searchapi": {"status": "skipped"},
        }
    )

    assert gate["state"] == "warning"
    assert gate["issues"] == []
    assert gate["warnings"][0]["code"] == "exa_empty"
    assert gate["warnings"][0]["detail"] == "no_result_intents: news"


def test_acquisition_gate_does_not_warn_when_exa_has_external_proof_with_profile_gap():
    from web.scan_runner import _build_acquisition_gate

    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "ok", "details": {"no_result_intents": ["external_profiles"]}},
            "searchapi": {"status": "skipped"},
        }
    )

    assert gate["state"] == "pass"
    assert gate["warnings"] == []


def test_acquisition_gate_does_not_parse_blocked_reason_for_usable_visual_evidence():
    from web.scan_runner import _build_acquisition_gate

    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "ok"},
            "searchapi": {"status": "ok"},
            "visual_acquisition": {
                "status": "completed",
                "evidence_status": "usable",
                "screenshot_status": "captured",
                "first_fold_evaluable": True,
                "obstruction": {
                    "present": True,
                    "type": "unknown_overlay",
                    "severity": "minor",
                },
                "details": {
                    "reason": "visual_evidence_packet:usable",
                    "blocked_reason": "obstruction:unknown_overlay; severity:minor",
                },
            },
        }
    )

    assert gate["state"] == "pass"
    assert gate["warnings"] == []


def test_scan_control_endpoints_delegate_to_runner(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.approve_degraded_scan",
        lambda scan_id: {"state": "running", "approved": True, "id": scan_id},
    )
    monkeypatch.setattr(
        "web.app.cancel_scan",
        lambda scan_id: {"state": "cancelled", "cancelled": True, "id": scan_id},
    )
    client = TestClient(app)

    assert client.post("/api/scan/scan123/continue").json()["approved"] is True
    assert client.post("/api/scan/scan123/cancel").json()["cancelled"] is True


def test_visual_acquisition_rows_include_evidence_packet(monkeypatch):
    from web.scan_runner import _capture_visual_evidence

    calls = {}
    evidence = {
        "schema_version": "visual-signature-evidence-v1",
        "capture": {"status": "usable", "first_fold_evaluable": True},
        "tile_signals": [],
    }

    def fake_shadow(**kwargs):
        calls.update(kwargs)
        return {
            "status": "completed",
            "interpretation_status": "interpretable",
            "agreement_level": "high",
            "visual_signature_score": 74,
            "visual_signature_scan_status": "usable",
            "visual_signature_scan": {"schema_version": "visual-signature-scan-v1"},
            "visual_evidence_packet": evidence,
            "visual_signature_evidence": evidence,
        }

    service = SimpleNamespace(
        _take_screenshot_with_budget=lambda url, timeout_seconds=20: (
            {"screenshot_url": "file:///tmp/example.png", "screenshot_provider": "playwright"},
            None,
        ),
        _screenshot_capture_diagnostic=lambda attempted, screenshot_data=None, limitation=None: {
            "success": bool((screenshot_data or {}).get("screenshot_url")),
            "source": (screenshot_data or {}).get("screenshot_provider"),
            "screenshot_url": (screenshot_data or {}).get("screenshot_url"),
        },
        _run_visual_signature_shadow=fake_shadow,
    )

    rows, step = _capture_visual_evidence(
        service=service,
        enabled=True,
        url="https://example.com",
        brand_name="Example",
        web_data={"owned": True},
        content_web={"owned": True},
    )

    assert step == {
        "status": "completed",
        "evidence_status": "usable",
        "screenshot_status": "unknown",
        "first_fold_evaluable": True,
        "obstruction": {},
        "details": {"reason": "visual_evidence_packet:usable"},
    }
    assert [row["source"] for row in rows] == ["screenshot_capture", "visual_acquisition"]
    assert rows[1]["payload"]["visual_evidence_packet"] == evidence
    assert rows[1]["payload"]["visual_signature_evidence"] == evidence
    assert calls["store"] is None
    assert calls["run_id"] is None
    assert calls["screenshot_capture"]["screenshot_url"] == "file:///tmp/example.png"


def test_visual_acquisition_persists_blocked_cookie_obstruction_detail(monkeypatch):
    from web.scan_runner import _build_acquisition_gate, _capture_visual_evidence

    evidence = {
        "schema_version": "visual-signature-evidence-v1",
        "capture": {
            "status": "blocked",
            "first_fold_evaluable": False,
            "obstruction": {
                "present": True,
                "type": "cookie_banner",
                "severity": "blocking",
                "signals": ["dom_keyword:cookie", "dom_keyword:privacy"],
            },
        },
        "tile_signals": [],
    }

    service = SimpleNamespace(
        _take_screenshot_with_budget=lambda url, timeout_seconds=20: (
            {"screenshot_url": "file:///tmp/example.png", "screenshot_provider": "playwright"},
            None,
        ),
        _screenshot_capture_diagnostic=lambda attempted, screenshot_data=None, limitation=None: {
            "success": bool((screenshot_data or {}).get("screenshot_url")),
            "source": (screenshot_data or {}).get("screenshot_provider"),
            "screenshot_url": (screenshot_data or {}).get("screenshot_url"),
        },
        _run_visual_signature_shadow=lambda **_kwargs: {
            "status": "completed",
            "visual_evidence_packet": evidence,
            "visual_signature_evidence": evidence,
        },
    )

    _rows, step = _capture_visual_evidence(
        service=service,
        enabled=True,
        url="https://example.com",
        brand_name="Example",
        web_data=None,
        content_web=None,
    )

    assert step["details"]["reason"] == "visual_evidence_packet:blocked"
    assert "obstruction:cookie_banner" in step["details"]["blocked_reason"]
    assert step["details"]["cookie_banner_suspected"] is True

    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "ok"},
            "searchapi": {"status": "ok"},
            "visual_acquisition": step,
        }
    )
    visual_warning = next(item for item in gate["warnings"] if item["source"] == "visual_acquisition")
    assert "blocked_reason: obstruction:cookie_banner" in visual_warning["detail"]


def test_visual_acquisition_failure_does_not_drop_screenshot_row():
    from web.scan_runner import _capture_visual_evidence

    def fake_shadow(**_kwargs):
        raise RuntimeError("visual model unavailable")

    service = SimpleNamespace(
        _take_screenshot_with_budget=lambda url, timeout_seconds=20: (
            {"screenshot_url": "file:///tmp/example.png", "screenshot_provider": "playwright"},
            None,
        ),
        _screenshot_capture_diagnostic=lambda attempted, screenshot_data=None, limitation=None: {
            "success": bool((screenshot_data or {}).get("screenshot_url")),
            "source": (screenshot_data or {}).get("screenshot_provider"),
            "screenshot_url": (screenshot_data or {}).get("screenshot_url"),
        },
        _run_visual_signature_shadow=fake_shadow,
    )

    rows, step = _capture_visual_evidence(
        service=service,
        enabled=True,
        url="https://example.com",
        brand_name="Example",
        web_data=None,
        content_web=None,
    )

    assert step["status"] == "error"
    assert "visual model unavailable" in step["details"]["reason"]
    assert rows == [
        {
            "source": "screenshot_capture",
            "payload": {
                "version": "screenshot_capture_v1",
                "url": "https://example.com",
                "content_source": "b3s_live_scan",
                "skip_visual_analysis": False,
                "capture": {
                    "success": True,
                    "source": "playwright",
                    "screenshot_url": "file:///tmp/example.png",
                },
            },
        }
    ]


def test_acquisition_gate_warns_when_web_capture_looks_like_cookie_banner():
    from web.scan_runner import _build_acquisition_gate

    gate = _build_acquisition_gate(
        {
            "web": {
                "status": "ok",
                "details": {
                    "reason": "captured",
                    "cookie_banner_suspected": True,
                    "cookie_banner_snippet": "Valoramos tu privacidad. Aceptar Rechazar Configurar.",
                },
            },
            "exa": {"status": "ok"},
            "searchapi": {"status": "ok"},
        }
    )

    warning = next(item for item in gate["warnings"] if item["code"] == "web_cookie_banner_suspected")
    assert warning["severity"] == "warning"
    assert "cookie_banner_snippet: Valoramos tu privacidad" in warning["detail"]


def test_web_cookie_signal_ignores_footer_controls_and_crawled_policy_pages():
    from src.collectors.web_collector import WebData
    from web.scan_runner import _cookie_banner_snippet_from_web_data

    homepage = (
        "# The app that makes money work\n\n"
        + ("Send, save, invest, and move money globally. " * 30)
        + "\n\nDeclineAccept"
    )
    web_data = WebData(
        url="https://example.com",
        title="The app that makes money work",
        markdown_content=(
            homepage
            + "\n\n---\n## Subpage: https://example.com/cookie-policy\n"
            + "We use cookies. Accept Reject Preferences."
        ),
    )

    assert _cookie_banner_snippet_from_web_data(web_data) == ""


def test_web_cookie_signal_keeps_banner_dominated_homepage_warning():
    from src.collectors.web_collector import WebData
    from web.scan_runner import _cookie_banner_snippet_from_web_data

    web_data = WebData(
        url="https://example.com",
        title="Privacy preferences",
        markdown_content="We value your privacy. Accept Reject Preferences.",
    )

    assert "We value your privacy" in _cookie_banner_snippet_from_web_data(web_data)


def test_acquisition_artifacts_include_screenshot_and_visual_obstruction(tmp_path, monkeypatch):
    from web.scan_runner import _acquisition_artifacts_from_snapshot

    screenshot_dir = tmp_path / "data" / "screenshots"
    screenshot_dir.mkdir(parents=True)
    screenshot = screenshot_dir / "brand3-screenshot-test.png"
    screenshot.write_bytes(b"png")
    monkeypatch.setattr("src.config.BRAND3_SCREENSHOT_DIR", str(screenshot_dir))

    artifacts = _acquisition_artifacts_from_snapshot(
        {
            "raw_inputs": [
                {
                    "source": "screenshot_capture",
                    "payload": {
                        "capture": {
                            "status": "captured",
                            "success": True,
                            "source": "playwright",
                            "screenshot_url": screenshot.as_uri(),
                            "screenshot_path": str(screenshot),
                            "metadata": {"title": "Stabolut"},
                        }
                    },
                },
                {
                    "source": "visual_acquisition",
                    "payload": {
                        "visual_evidence_packet": {
                            "capture": {
                                "status": "blocked",
                                "first_fold_evaluable": False,
                                "obstruction": {
                                    "present": True,
                                    "type": "cookie_modal",
                                    "severity": "major",
                                    "signals": ["dom_keyword:privacy"],
                                },
                            }
                        }
                    },
                },
            ]
        }
    )

    assert artifacts[0]["public_url"] == "/artifacts/screenshots/brand3-screenshot-test.png"
    assert artifacts[0]["metadata"]["title"] == "Stabolut"
    assert artifacts[1]["status"] == "blocked"
    assert artifacts[1]["obstruction"]["type"] == "cookie_modal"


def test_acquisition_artifacts_include_full_page_and_section_captures(tmp_path, monkeypatch):
    from web.scan_runner import _acquisition_artifacts_from_snapshot

    screenshot_dir = tmp_path / "data" / "screenshots"
    screenshot_dir.mkdir(parents=True)
    viewport = screenshot_dir / "capture.png"
    full_page = screenshot_dir / "capture.full-page.png"
    section = screenshot_dir / "capture.section-01-hero.png"
    atlas = screenshot_dir / "capture.analysis-atlas.png"
    for path in (viewport, full_page, section, atlas):
        path.write_bytes(b"png")
    monkeypatch.setattr("src.config.BRAND3_SCREENSHOT_DIR", str(screenshot_dir))

    artifacts = _acquisition_artifacts_from_snapshot(
        {
            "raw_inputs": [
                {
                    "source": "screenshot_capture",
                    "payload": {
                        "capture": {
                            "status": "captured",
                            "success": True,
                            "source": "playwright",
                            "screenshot_path": str(viewport),
                            "screenshot_url": viewport.as_uri(),
                            "metadata": {
                                "full_page_screenshot_path": str(full_page),
                                "section_capture_status": "complete",
                                "section_manifest": {
                                    "capture_variant": "raw_viewport",
                                    "sections": [
                                        {
                                            "id": "section-01-hero",
                                            "label": "Hero",
                                            "kind": "hero",
                                            "capture_variant": "raw_viewport",
                                            "bbox": {"left": 0, "top": 0, "width": 1440, "height": 900},
                                            "capture_path": str(section),
                                        }
                                    ],
                                },
                                "analysis_atlas_path": str(atlas),
                                "analysis_atlas_status": "complete",
                                "analysis_atlas_manifest": {
                                    "panel_count": 2,
                                    "section_panel_count": 1,
                                },
                            },
                        }
                    },
                }
            ]
        }
    )

    assert [artifact["kind"] for artifact in artifacts] == [
        "screenshot",
        "full_page_screenshot",
        "visual_analysis_atlas",
        "section_screenshot",
    ]
    assert artifacts[1]["public_url"] == f"/artifacts/screenshots/{full_page.name}"
    assert artifacts[2]["public_url"] == f"/artifacts/screenshots/{atlas.name}"
    assert artifacts[2]["section_panel_count"] == 1
    assert artifacts[3]["public_url"] == f"/artifacts/screenshots/{section.name}"
    assert artifacts[3]["section_id"] == "section-01-hero"


def test_screenshot_artifact_uses_configured_persistent_root(tmp_path, monkeypatch):
    from web.app import app

    screenshot = tmp_path / "brand3-screenshot-volume.png"
    screenshot.write_bytes(b"png")
    monkeypatch.setattr("src.config.BRAND3_SCREENSHOT_DIR", str(tmp_path))

    response = TestClient(app).get(f"/artifacts/screenshots/{screenshot.name}")

    assert response.status_code == 200
    assert response.content == b"png"


def test_capture_snapshot_adds_visual_acquisition_raw_input(monkeypatch):
    from src.services import brand_service
    from web.scan_runner import _capture_snapshot

    evidence = {
        "schema_version": "visual-signature-evidence-v1",
        "capture": {"status": "usable"},
        "tile_signals": [],
    }

    monkeypatch.setattr("src.config.BRAND3_VISUAL_SIGNATURE_SCAN_ENABLED", True)
    monkeypatch.setattr(
        brand_service,
        "collect_raw_inputs",
        lambda **_kwargs: SimpleNamespace(
            context_data=SimpleNamespace(status="ok", to_dict=lambda: {"url": "https://example.com"}),
            web_data=SimpleNamespace(status="ok", to_dict=lambda: {"url": "https://example.com"}),
            exa_data=None,
            github_data=None,
            searchapi_data=None,
            acquisition_steps={
                "web": SimpleNamespace(
                    status="ok",
                    to_dict=lambda: {"status": "ok", "details": {"reason": "captured"}},
                )
            },
        ),
    )
    monkeypatch.setattr(
        brand_service,
        "_take_screenshot_with_budget",
        lambda url, timeout_seconds=20: (
            {"screenshot_url": "file:///tmp/example.png", "screenshot_provider": "playwright"},
            None,
        ),
    )
    monkeypatch.setattr(
        brand_service,
        "_screenshot_capture_diagnostic",
        lambda attempted, screenshot_data=None, limitation=None: {
            "success": True,
            "source": (screenshot_data or {}).get("screenshot_provider"),
            "screenshot_url": (screenshot_data or {}).get("screenshot_url"),
        },
    )
    monkeypatch.setattr(
        brand_service,
        "_run_visual_signature_shadow",
        lambda **_kwargs: {
            "status": "completed",
            "visual_signature_scan": {"schema_version": "visual-signature-scan-v1"},
            "visual_evidence_packet": evidence,
            "visual_signature_evidence": evidence,
        },
    )

    snapshot = _capture_snapshot("scan123", "https://example.com", "Example")

    raw_sources = [row["source"] for row in snapshot["raw_inputs"]]
    assert raw_sources == ["context", "web", "screenshot_capture", "visual_acquisition"]
    assert snapshot["raw_inputs"][-1]["payload"]["visual_evidence_packet"] == evidence
    assert snapshot["acquisition_steps"]["visual_acquisition"]["status"] == "completed"


def test_scan_and_api_fall_back_to_finished_report(monkeypatch):
    from web.app import app

    monkeypatch.setattr("web.app.scan_status", lambda scan_id: None)
    monkeypatch.setattr("web.app.load_report", lambda scan_id: {"id": scan_id})
    client = TestClient(app)

    page = client.get("/scan/done123", follow_redirects=False)
    api = client.get("/api/scan/done123")

    assert page.status_code == 303
    assert page.headers["location"] == "/report/done123"
    assert api.status_code == 200
    assert api.json() == {"state": "done", "id": "done123", "report_id": "done123"}


def test_scan_view_uses_structured_layout(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.scan_status",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Mercury",
            "url": "https://mercury.com",
            "created_at": "2026-07-30T13:48:00+00:00",
            "state": "running",
            "phases": [
                {"key": "capture", "label": "Capture evidence", "state": "running"},
                {"key": "interpret", "label": "Interpret SV9", "state": "pending"},
            ],
        },
    )

    response = TestClient(app).get("/scan/scan123")

    assert response.status_code == 200
    assert "scan-shell" in response.text
    assert "scan-card" in response.text
    assert "scan-meter" in response.text
    assert "scan-steps" in response.text
    assert "scan-gate-actions" in response.text
    assert "scan-acquisition-table" in response.text
    assert "Detalle técnico" in response.text
    assert "https://mercury.com" in response.text
    assert 'style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px"' not in response.text


def test_scan_preview_renders_without_live_scan():
    from web.app import app

    response = TestClient(app).get("/dev/scan-preview?variant=blocked")

    assert response.status_code == 200
    assert "preview-blocked" in response.text
    assert "scan-shell" in response.text
    assert "visual_acquisition" in response.text
    assert "visual_evidence_packet:blocked" in response.text
    assert "status-tag status-tag--warn status-tag--filled" in response.text
    assert ">limited</span>" in response.text
    assert "const scanPreview = true" in response.text


def test_scan_view_marks_blocked_visual_packet_as_limited_warning(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.scan_status",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "state": "running",
            "phases": [
                {"key": "capture", "label": "Capture evidence", "state": "running"},
            ],
            "acquisition": [
                {
                    "source": "visual_acquisition",
                    "status": "completed",
                    "evidence_status": "blocked",
                    "first_fold_evaluable": False,
                    "detail": "visual_evidence_packet:blocked; blocked_reason: obstruction:cookie_modal",
                }
            ],
            "acquisition_gate": {
                "state": "warning",
                "issues": [],
                "warnings": [
                    {
                        "source": "visual_acquisition",
                        "code": "visual_acquisition_limited",
                        "status": "completed",
                        "message": "Visual acquisition was obstructed.",
                    }
                ],
            },
        },
    )

    response = TestClient(app).get("/scan/scan123")

    assert response.status_code == 200
    assert "visual_evidence_packet:blocked" in response.text
    assert "status-tag status-tag--warn status-tag--filled" in response.text
    assert ">limited</span>" in response.text


def test_report_view_renders_report(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Mercury",
            "url": "https://mercury.com",
            "created_at": "2026-07-30T13:48:00+00:00",
            "score": 81,
            "base_average": 72,
            "reliability_status": "shadow",
            "canonical_status": "non_canonical",
            "stability": {
                "classification": "evaluation_drift",
                "canonical_status": "non_canonical",
                "reason_codes": [
                    "evaluation_changed_without_material_evidence_delta"
                ],
                "baseline_comparison": {
                    "baseline_report_id": "baseline123",
                    "delta": {
                        "changed_components": [
                            {
                                "component": "core_purpose",
                                "score_before": 4,
                                "score_after": 8,
                                "status_before": "scored",
                                "status_after": "scored",
                                "changed_tiles": ["PR3", "PR4"],
                            }
                        ]
                    },
                },
            },
            "detected_count": 1,
            "block_count": 1,
            "not_detected": [],
            "most_painful_gap_label": "",
            "immediate_margin": None,
            "total_blind_spots": 0,
            "coverage_acquisition": {
                "owned_url_count": 1,
                "owned_page_coverage": {
                    "selection_version": "owned-page-selection-v2",
                    "known_page_count": 2,
                    "attempted_page_count": 1,
                    "captured_page_count": 1,
                    "coverage_ratio": 0.5,
                    "visited_pages": [
                        {
                            "url": "https://mercury.com",
                            "status": "captured",
                            "navigation_status": "homepage",
                            "observed_language": "en",
                            "language_confidence": "high",
                            "declared_language": "en",
                        }
                    ],
                    "not_visited_pages": [
                        {
                            "url": "https://mercury.com/orphan",
                            "reason": "page_budget",
                            "navigation_status": "sitemap_only",
                        }
                    ],
                    "language_detection": {
                        "version": "owned-page-language-en-es-v1",
                        "status": "mixed",
                        "mixed_language_site": True,
                        "captured_page_count": 2,
                        "evaluated_page_count": 2,
                        "unknown_page_count": 0,
                        "declared_mismatch_count": 1,
                        "distribution": [
                            {"language": "en", "page_count": 1, "share": 0.5},
                            {"language": "es", "page_count": 1, "share": 0.5},
                        ],
                    },
                },
                "external_source_count": 0,
                "absence_record_count": 0,
                "attempt_record_count": 0,
                "content_sampling": {
                    "sampling_record_count": 1,
                    "affected_url_count": 1,
                    "available_chunk_count": 10,
                    "retained_chunk_count": 6,
                    "omitted_chunk_count": 4,
                    "surfaces": [
                        {
                            "ref": "raw_inputs.0.diagnostics.sampling.subpage.1",
                            "url": "https://mercury.com/orphan",
                            "available_chunk_count": 10,
                            "retained_chunk_count": 6,
                            "omitted_chunk_count": 4,
                        }
                    ],
                },
            },
            "components": [
                {
                    "key": "core_purpose",
                    "label": "Propósito",
                    "score": 4,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "Purpose text.",
                    "veredicto": "",
                    "tile_states": [{"id": "PX9", "name": "Hidden tile", "state": "off"}],
                    "tiles": [
                        {
                            "id": "P2",
                            "name": "Tensión explícita",
                            "estado": "no",
                            "motivo": "No aparece una tensión propia.",
                            "evidencia": "Texto de apoyo.",
                        }
                    ],
                    "surface_hierarchy": {
                        "schema_version": "component-surface-hierarchy-v1",
                        "presence_status": "detected",
                        "hierarchy_status": "sitemap_only",
                        "owned_surface_count": 1,
                        "counts": {
                            "homepage": 0,
                            "linked_from_home": 0,
                            "sitemap_only": 1,
                            "owned_unknown": 0,
                            "external": 0,
                        },
                        "owned_surfaces": [
                            {
                                "url": "https://mercury.com/orphan",
                                "navigation_status": "sitemap_only",
                                "captured": True,
                                "cited_refs": ["owned.orphan"],
                            }
                        ],
                    },
                    "block": None,
                },
                {
                    "key": "brand_idea",
                    "label": "Idea de marca",
                    "score": 3,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "Brand idea text.",
                    "veredicto": "",
                    "tile_states": [],
                    "tiles": [],
                    "block": None,
                },
                {
                    "key": "coherencia",
                    "label": "Coherencia",
                    "score": 7,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "",
                    "veredicto": "Coherence text.",
                    "tile_states": [],
                    "tiles": [],
                    "block": None,
                },
            ],
            "blocks": [],
            "absences": [],
            "attempts": [],
            "limitations": [],
            "raw": {"ok": True},
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert "Mercury" in response.text
    assert "https://mercury.com" in response.text
    assert "Escaneado: 2026-07-30 13:48:00 UTC" in response.text
    assert "1 de 2" in response.text
    assert "https://mercury.com/orphan" in response.text
    assert "page_budget" in response.text
    assert "contenido muestreado" in response.text
    assert "se omitieron 4 fragmentos" in response.text
    assert "6/10 fragmentos" in response.text
    assert "report-hero" in response.text
    assert "Score diagnóstico" in response.text
    assert "--score-width: 81%;" in response.text
    assert "81<span>/100</span>" in response.text
    assert 'method="post" action="/scan"' in response.text
    assert 'value="https://mercury.com"' in response.text
    assert "shadow run" not in response.text
    assert 'id="core_purpose"' in response.text
    assert "component-card--half" in response.text
    assert 'aria-label="Abrir lectura de Propósito"' in response.text
    assert 'data-dialog-target="drawer-core_purpose"' in response.text
    assert 'class="report-drawer" id="drawer-core_purpose"' in response.text
    assert 'class="tiles"' not in response.text
    assert "PX9" not in response.text
    assert "P2" in response.text
    assert "evidencia propia localizable solo mediante sitemap" in response.text
    assert "https://mercury.com/orphan" in response.text
    assert "inglés 1 · español 1" in response.text
    assert "mezcla de idiomas" in response.text
    assert "idioma inglés (high)" in response.text
    assert "Deriva de evaluación detectada" in response.text
    assert "baseline123" in response.text
    assert "Propósito</strong>: 4 → 8" in response.text
    assert 'id="brand_idea"' in response.text
    assert "component-card--third" in response.text
    assert 'id="coherencia"' in response.text
    assert "component-card--full" in response.text
    assert json.dumps({"ok": True}) not in response.text


def test_report_hides_explanatory_tags_but_keeps_diagnostic_scores_and_quote(
    monkeypatch,
):
    from web.app import app

    monkeypatch.setattr(
        "web.app.load_report",
        lambda _report_id: {
            "id": "diagnostic123",
            "brand_name": "Mercury",
            "url": "https://mercury.com",
            "score": 64,
            "stability": {
                "classification": "evaluation_drift",
                "canonical_status": "non_canonical",
            },
            "components": [
                {
                    "key": "core_purpose",
                    "label": "Propósito",
                    "score": 7,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "Una lectura estratégica verificable.",
                    "veredicto": "La evidencia sostiene la lectura.",
                    "tile_profile": [
                        {
                            "id": "P1",
                            "estado": "ok",
                            "evidencia": "La prueba literal permanece visible.",
                        }
                    ],
                    "evidence": [
                        {
                            "ref": "raw_inputs.1",
                            "url": "https://mercury.com/about",
                            "snippet": "La prueba literal permanece visible.",
                        }
                    ],
                    "block": {"coverage_status": "positive_evidence"},
                }
            ],
            "blocks": [],
            "absences": [],
            "attempts": [],
            "raw": {},
        },
    )

    response = TestClient(app).get("/report/diagnostic123")

    assert response.status_code == 200
    assert "Score diagnóstico" in response.text
    assert "64<span>/100</span>" in response.text
    assert '<span class="badge">7/10</span>' in response.text
    assert "diagnóstico no canónico" in response.text
    assert "La prueba literal permanece visible." in response.text
    assert '<span class="chip info">interpretación</span>' not in response.text
    assert '<span class="chip ok">cita literal</span>' not in response.text
    assert ">con evidencia</span>" not in response.text


def test_report_view_hides_automatic_verdict_from_card_without_tile_profile(monkeypatch):
    from web.app import app

    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Mercury",
            "url": "https://mercury.com",
            "score": 81,
            "base_average": 72,
            "reliability_status": "shadow",
            "detected_count": 1,
            "block_count": 1,
            "not_detected": [],
            "most_painful_gap_label": "",
            "immediate_margin": None,
            "total_blind_spots": 0,
            "coverage_acquisition": {
                "owned_url_count": 1,
                "external_source_count": 0,
                "absence_record_count": 0,
                "attempt_record_count": 0,
            },
            "components": [
                {
                    "key": "magnetism",
                    "label": "Magnetism",
                    "score": 3,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "The snapshot does not provide access to the full product interface.",
                    "veredicto": "The snapshot does not provide access to the full product interface.",
                    "lit": 3,
                    "off": 1,
                    "blind": 6,
                    "tile_states": [{"id": "MX9", "name": "Resilience", "state": "off"}],
                    "tiles": [],
                    "block": None,
                }
            ],
            "blocks": [],
            "absences": [],
            "attempts": [],
            "limitations": [],
            "raw": {},
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert "Síntesis automática: 3/10 baldosas encendidas, 1 apagada, 6 puntos ciegos." not in response.text
    assert "The snapshot does not provide access" not in response.text
    assert "0/10 baldosas encendidas" not in response.text


def test_compose_report_preserves_canonical_sv9_tile_profile(monkeypatch):
    from src.sv9.aggregator import aggregate
    from src.sv9.models import ESTADO_NO, ESTADO_OK, ESTADO_SIN_EVIDENCIA
    from src.sv9.rubric import tile_ids
    from tests.test_vault_sv9_parity import _component, _components, _flow_payload
    from web.scan_runner import _compose_report

    ids = tile_ids("magnetism")
    components = _components()
    states = [ESTADO_OK] * 3 + [ESTADO_NO] + [ESTADO_SIN_EVIDENCIA] * 6
    magnetism = _component("magnetism", states)
    magnetism.detected_content = "Promesa de soberanía tecnológica."
    magnetism.veredicto = (
        "La marca tiene utilidad técnica, pero necesita más tensión narrativa."
    )
    for tile in magnetism.tile_profile:
        if tile.estado == ESTADO_NO:
            tile.motivo = "No hay contraste narrativo."
        elif tile.estado == ESTADO_SIN_EVIDENCIA:
            tile.motivo = "El snapshot no aporta prueba externa."
            tile.contexto_requerido = "Aporta entrevistas o métricas de adopción."
    components["magnetism"] = magnetism
    result = aggregate(
        components,
        brand_name="Optiak",
        url="https://optiak.com",
    ).to_dict()
    payload = _flow_payload(result)
    payload["flow"]["interpretation_debug"]["evidence_coverage"] = {
        "component_hierarchy": {
            "magnetism": {
                "schema_version": "component-surface-hierarchy-v1",
                "presence_status": "detected",
                "hierarchy_status": "sitemap_only",
                "owned_surface_count": 1,
                "counts": {
                    "homepage": 0,
                    "linked_from_home": 0,
                    "sitemap_only": 1,
                    "owned_unknown": 0,
                    "external": 0,
                },
                "owned_surfaces": [
                    {
                        "url": "https://optiak.com/thesis",
                        "navigation_status": "sitemap_only",
                        "captured": True,
                        "cited_refs": ["owned.thesis"],
                    }
                ],
            }
        }
    }

    monkeypatch.setenv("B3S_BUILD_SHA", "c" * 40)
    report = _compose_report("scan123", "https://optiak.com", "Optiak", payload)
    magnetism = next(component for component in report["components"] if component["key"] == "magnetism")

    assert report["pipeline_commit_sha"] == "c" * 40
    assert len(magnetism["tile_profile"]) == 10
    assert magnetism["lit"] == 3
    assert magnetism["off"] == 1
    assert magnetism["blind"] == 6
    assert magnetism["resumen"] == "Promesa de soberanía tecnológica."
    assert "tensión narrativa" in magnetism["veredicto"]
    assert magnetism["tiles"][0]["id"] == ids[3]
    assert magnetism["tiles"][1]["contexto_requerido"] == "Aporta entrevistas o métricas de adopción."
    assert magnetism["surface_hierarchy"]["hierarchy_status"] == "sitemap_only"


def test_english_tile_prose_localizes_profile_and_alias_for_html_and_markdown(
    monkeypatch,
):
    from src.sv9.aggregator import aggregate
    from tests.test_vault_sv9_parity import _components, _flow_payload
    from web import scan_runner
    from web.app import app

    english_motivo = (
        "The snapshot does not provide access to the full product interface "
        "and customer evidence"
    )
    english_context = (
        "The available evidence requires customer interviews and product usage documentation"
    )
    components = _components()
    mission_m4 = components["mission"].tile_profile[3]
    mission_m4.estado = "sin_evidencia"
    mission_m4.motivo = english_motivo
    mission_m4.contexto_requerido = english_context
    result = aggregate(
        components,
        brand_name="Example",
        url="https://example.com",
    ).to_dict()
    report = scan_runner._compose_report(
        "english-tile-presentation",
        "https://example.com",
        "Example",
        _flow_payload(result),
    )
    raw_mission = next(row for row in report["components"] if row["key"] == "mission")
    raw_profile_m4 = next(row for row in raw_mission["tile_profile"] if row["id"] == "M4")
    raw_alias_m4 = next(row for row in raw_mission["tiles"] if row["id"] == "M4")
    assert raw_profile_m4["motivo"] == raw_alias_m4["motivo"] == english_motivo
    assert raw_profile_m4["contexto_requerido"] == raw_alias_m4["contexto_requerido"] == english_context

    monkeypatch.setattr("web.app.load_report", lambda _scan_id: report)
    monkeypatch.setattr("web.app.list_reports_for_domain", lambda _domain: [])
    client = TestClient(app)

    html = client.get("/report/english-tile-presentation")
    markdown = client.get("/report/english-tile-presentation.md")

    assert html.status_code == 200
    assert markdown.status_code == 200
    spanish_motivo = "El snapshot no aporta evidencia suficiente para evaluar esta baldosa sin contexto adicional."
    spanish_context = "Aporta contexto externo verificable: comparativa, experiencia de producto, canales activos o documentación operativa."
    for response in (html, markdown):
        assert english_motivo not in response.text
        assert english_context not in response.text
        assert spanish_motivo in response.text
        assert spanish_context in response.text


def test_attach_sv9_editorial_only_requests_components_with_unusable_prose():
    from src.sv9.rubric import tile_ids
    from web.scan_runner import _attach_sv9_editorial

    ids = tile_ids("magnetism")
    payload = {
        "sv9": {
            "result": {
                "brand_name": "Optiak",
                "url": "https://optiak.com",
                "brand3_score": 61,
                "editorial_v3_1": {
                    "schema_version": "sv9_editorial_v3_1",
                    "components": {
                        "magnetism": {"diagnosis": "Diagnóstico estructurado."},
                        "mission": {"diagnosis": "Diagnóstico estructurado."},
                    },
                },
                "components": {
                    "magnetism": {
                        "component": "magnetism",
                        "status": "scored",
                        "score": 3,
                        "scale": 10,
                        "veredicto": "The snapshot does not provide enough narrative evidence.",
                        "tile_profile": [
                            {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
                            {"id": ids[1], "estado": "no", "motivo": "falta"},
                        ],
                    },
                    "mission": {
                        "component": "mission",
                        "status": "scored",
                        "score": 5,
                        "scale": 5,
                        "veredicto": "La misión está formulada con claridad.",
                        "tile_profile": [],
                    },
                },
            }
        }
    }
    calls = []

    class FakeLLM:
        api_key = "test-key"

    def fake_build_editorial(scan, *, llm, component_keys, include_executive_reading):
        calls.append(
            {
                "component_keys": list(component_keys),
                "include_executive_reading": include_executive_reading,
            }
        )
        return {
            "component_messages": {
                "magnetism": "La marca necesita convertir utilidad técnica en tensión narrativa."
            },
            "executive_reading": "Lectura ejecutiva del scan.",
            "structured": {
                "schema_version": "sv9_editorial_v3_1",
                "executive_reading": "Lectura ejecutiva del scan.",
                "components": {
                    "magnetism": {
                        "diagnosis": "La marca necesita convertir utilidad técnica en tensión narrativa.",
                        "detected_basis": "Base detectada.",
                        "next_artifact": "Narrativa de tensión.",
                        "terms": [],
                    }
                },
            },
        }

    result = _attach_sv9_editorial(
        payload,
        llm=FakeLLM(),
        build_editorial_fn=fake_build_editorial,
    )

    assert calls == [{"component_keys": ["magnetism"], "include_executive_reading": True}]
    components = result["sv9"]["result"]["components"]
    assert components["magnetism"]["message"] == "La marca necesita convertir utilidad técnica en tensión narrativa."
    assert "message" not in components["mission"]
    assert result["sv9"]["result"]["executive_reading"] == "Lectura ejecutiva del scan."
    assert result["sv9"]["result"]["editorial_v3_1"]["schema_version"] == "sv9_editorial_v3_1"


def test_attach_sv9_editorial_requests_all_components_when_structured_contract_is_missing():
    from src.sv9.rubric import tile_ids
    from web.scan_runner import _attach_sv9_editorial

    ids = tile_ids("magnetism")
    payload = {
        "sv9": {
            "result": {
                "brand_name": "Optiak",
                "url": "https://optiak.com",
                "brand3_score": 61,
                "components": {
                    "magnetism": {
                        "component": "magnetism",
                        "status": "scored",
                        "score": 3,
                        "scale": 10,
                        "message": "La marca necesita convertir utilidad técnica en tensión narrativa.",
                        "veredicto": "Síntesis automática: 3/10 baldosas encendidas.",
                        "tile_profile": [
                            {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
                            {"id": ids[1], "estado": "no", "motivo": "falta"},
                        ],
                    },
                    "mission": {
                        "component": "mission",
                        "status": "scored",
                        "score": 5,
                        "scale": 5,
                        "message": "La misión está formulada con claridad.",
                        "veredicto": "La misión está formulada con claridad.",
                        "tile_profile": [],
                    },
                },
            }
        }
    }
    calls = []

    class FakeLLM:
        api_key = "test-key"

    def fake_build_editorial(scan, *, llm, component_keys, include_executive_reading):
        calls.append(
            {
                "component_keys": list(component_keys),
                "include_executive_reading": include_executive_reading,
            }
        )
        return {"component_messages": {}, "executive_reading": None}

    _attach_sv9_editorial(
        payload,
        llm=FakeLLM(),
        build_editorial_fn=fake_build_editorial,
    )

    assert calls == [{"component_keys": ["magnetism", "mission"], "include_executive_reading": True}]


def test_attach_sv9_editorial_skips_when_evaluator_messages_exist():
    from src.sv9.rubric import tile_ids
    from web.scan_runner import _attach_sv9_editorial

    ids = tile_ids("magnetism")
    payload = {
        "sv9": {
            "result": {
                "brand_name": "Optiak",
                "url": "https://optiak.com",
                "brand3_score": 61,
                "components": {
                    "magnetism": {
                        "component": "magnetism",
                        "status": "scored",
                        "score": 3,
                        "scale": 10,
                        "message": "La marca necesita convertir utilidad técnica en tensión narrativa.",
                        "veredicto": "Síntesis automática: 3/10 baldosas encendidas.",
                        "tile_profile": [
                            {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
                            {"id": ids[1], "estado": "no", "motivo": "falta"},
                        ],
                    },
                    "mission": {
                        "component": "mission",
                        "status": "scored",
                        "score": 5,
                        "scale": 5,
                        "message": "La misión está formulada con claridad.",
                        "veredicto": "La misión está formulada con claridad.",
                        "tile_profile": [],
                    },
                },
            }
        }
    }

    def fail_build_editorial(*_args, **_kwargs):
        raise AssertionError("editorial fallback should not run")

    result = _attach_sv9_editorial(
        payload,
        llm=object(),
        build_editorial_fn=fail_build_editorial,
    )

    assert "editorial" not in result["sv9"]
    assert result["sv9"]["result"]["components"]["magnetism"]["message"].startswith("La marca")


def test_report_view_rehydrates_reduced_projection_from_raw_sv9_result(monkeypatch):
    from src.sv9.rubric import tile_ids
    from web.app import app

    ids = tile_ids("magnetism")
    tile_profile = [
        {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
        {"id": ids[1], "estado": "ok", "evidencia": "dolor visible"},
        {"id": ids[2], "estado": "ok", "evidencia": "deseo visible"},
        {"id": ids[3], "estado": "no", "motivo": "No hay contraste narrativo."},
        *[
            {
                "id": tile_id,
                "estado": "sin_evidencia",
                "motivo": "El snapshot no aporta prueba externa.",
                "contexto_requerido": "Aporta entrevistas o métricas de adopción.",
            }
            for tile_id in ids[4:]
        ],
    ]
    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 61,
            "base_average": 61,
            "reliability_status": "shadow",
            "detected_count": 1,
            "block_count": 1,
            "not_detected": [],
            "most_painful_gap_label": "Magnetism",
            "immediate_margin": 8,
            "total_blind_spots": 6,
            "coverage_acquisition": {
                "owned_url_count": 1,
                "external_source_count": 0,
                "absence_record_count": 0,
                "attempt_record_count": 0,
            },
            "components": [
                {
                    "key": "magnetism",
                    "label": "Magnetism",
                    "score": 3,
                    "scale": 10,
                    "status": "scored",
                    "resumen": "Componente Magnetism detectado: 3/10 baldosas encendidas.",
                    "veredicto": "Síntesis automática: 3/10 baldosas encendidas, 1 apagada, 6 puntos ciegos.",
                    "lit": 3,
                    "off": 1,
                    "blind": 6,
                    "tiles": [],
                    "block": None,
                }
            ],
            "blocks": [],
            "absences": [],
            "attempts": [],
            "limitations": [],
            "raw": {
                "sv9": {
                    "result": {
                        "brand3_score": 61,
                        "model": "v3.1",
                        "components": {
                            "magnetism": {
                                "component": "magnetism",
                                "status": "scored",
                                "score": 3,
                                "scale": 10,
                                "points": 6,
                                "confidence": "baja",
                                "detected_content": "Promesa de soberanía tecnológica.",
                                "veredicto": "La marca tiene utilidad técnica, pero necesita más tensión narrativa.",
                                "tile_profile": tile_profile,
                            }
                        },
                    }
                }
            },
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert "Promesa de soberanía tecnológica." in response.text
    assert "La marca tiene utilidad técnica" in response.text
    assert "Síntesis automática" not in response.text
    assert ids[3] in response.text
    assert "aporta contexto: Aporta entrevistas o métricas de adopción." in response.text


def test_report_markdown_exports_raw_sv9_contract(monkeypatch):
    from src.sv9.rubric import tile_ids
    from web.app import app

    ids = tile_ids("magnetism")
    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 61,
            "components": [
                {
                    "key": "magnetism",
                    "surface_hierarchy": {
                        "hierarchy_status": "mixed_with_sitemap_only",
                        "counts": {
                            "linked_from_home": 1,
                            "sitemap_only": 1,
                        },
                    },
                }
            ],
            "raw": {
                "sv9": {
                    "result": {
                        "brand_name": "Optiak",
                        "url": "https://optiak.com",
                        "brand3_score": 61,
                        "model": "v3.1",
                        "components": {
                            "magnetism": {
                                "component": "magnetism",
                                "status": "scored",
                                "score": 3,
                                "scale": 10,
                                "points": 6,
                                "confidence": "baja",
                                "detected_content": "Promesa de soberanía tecnológica.",
                                "message": "Lectura editorial de Magnetism.",
                                "veredicto": "La marca necesita más tensión narrativa.",
                                "tile_profile": [
                                    {"id": ids[0], "estado": "ok", "evidencia": "promesa visible"},
                                    {"id": ids[1], "estado": "ok", "evidencia": "dolor visible"},
                                    {"id": ids[2], "estado": "ok", "evidencia": "deseo visible"},
                                    {"id": ids[3], "estado": "no", "motivo": "No hay contraste narrativo."},
                                ],
                            }
                        },
                    }
                }
            },
        },
    )

    response = TestClient(app).get("/report/report123.md")

    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]
    assert "# Brand3 Scanner — Optiak" in response.text
    assert "Brand3 Score: **61/100**" in response.text
    assert "## Magnetism" in response.text
    assert "Lectura editorial de Magnetism." in response.text
    assert "Jerarquía: **parte de la evidencia está fuera de navegación**" in response.text
    assert "Superficies propias fuera de navegación: **1**" in response.text


def test_report_view_renders_coherencia_once_and_prioritizes_editorial_message(monkeypatch):
    from web.app import app

    base_component = {
        "score": 5,
        "scale": 10,
        "status": "scored",
        "resumen": "Texto detectado.",
        "veredicto": "Veredicto estratégico.",
        "tile_states": [],
        "tiles": [],
        "block": None,
    }
    components = [
        {**base_component, "key": "core_purpose", "label": "Propósito"},
        {
            **base_component,
            "key": "coherencia",
            "label": "Coherencia",
            "score": 6,
            "resumen": "Componente Coherencia detectado: 6/10 baldosas encendidas, 0 apagadas, 4 puntos ciegos. Revisa las fuentes para validar el matiz exacto.",
            "veredicto": "Optiak construye un discurso técnico sólido.",
            "message": "Lectura editorial de coherencia.",
            "tile_profile": [
                {
                    "id": "C7",
                    "estado": "no",
                    "motivo": "El discurso cambia entre web y red social.",
                    "evidencia": "Las promesas de canal no coinciden.",
                }
            ],
            "lit": 0,
            "off": 1,
            "blind": 0,
        },
    ]
    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 65,
            "base_average": 65,
            "reliability_status": "shadow",
            "detected_count": 2,
            "block_count": 2,
            "not_detected": [],
            "most_painful_gap_label": "Magnetism",
            "immediate_margin": 8,
            "total_blind_spots": 4,
            "coverage_acquisition": {
                "owned_url_count": 1,
                "external_source_count": 0,
                "absence_record_count": 0,
                "attempt_record_count": 0,
            },
            "components": components,
            "blocks": [],
            "absences": [],
            "attempts": [],
            "limitations": [],
            "raw": {},
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert response.text.count('id="coherencia"') == 1
    assert "Lectura editorial de coherencia." in response.text
    assert "Optiak construye un discurso técnico sólido." in response.text
    assert "Componente Coherencia detectado" not in response.text
    assert "C7" in response.text
    assert "Web-redes" in response.text
    assert "El discurso cambia entre web y red social." in response.text


def test_report_view_does_not_instantiate_llm_analyzer(monkeypatch):
    from web.app import app

    def fail_llm(*_args, **_kwargs):
        raise AssertionError("report view must not instantiate LLMAnalyzer")

    monkeypatch.setattr("src.features.llm_analyzer.LLMAnalyzer", fail_llm)
    monkeypatch.setattr(
        "web.app.load_report",
        lambda scan_id: {
            "id": scan_id,
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 65,
            "components": [
                {
                    "key": "mission",
                    "label": "Misión",
                    "score": 5,
                    "scale": 5,
                    "status": "scored",
                    "message": "Lectura persistida.",
                    "detected_content": "Misión detectada.",
                }
            ],
            "raw": {},
        },
    )

    response = TestClient(app).get("/report/report123")

    assert response.status_code == 200
    assert "Lectura persistida." in response.text
    assert "Interpretación de la evidencia" in response.text
    assert "Misión detectada." in response.text
    assert '<p class="card-verdict">Misión detectada.</p>' in response.text
