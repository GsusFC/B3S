"""Plan a safe Brand3 SQLite archive import into isolated B3S history."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.evidence_identity import stable_artifact_digest
from src.history.models import HistoricalReport
from src.history.report_parser import parse_report
from src.services.brand3_sqlite_memory_backfill import (
    latest_brand3_reports_per_capture,
    load_brand3_sqlite_sv9_reports,
)
from src.sv9.rubric import RUBRIC_VERSION


BRAND3_ARCHIVE_HISTORY_IMPORT_VERSION = (
    "brand3-sqlite-history-import-v1"
)
BRAND3_ARCHIVE_HISTORY_IMPORT_POLICY_VERSION = (
    "brand3-sqlite-history-import-policy-v1"
)
BRAND3_ARCHIVE_WORKSPACE_SLUG = "b3s-archive"
BRAND3_ARCHIVE_WORKSPACE_NAME = "B3S Legacy Archive"


def load_brand3_archive_history_reports(
    database_path: str | Path,
    *,
    rubric_version: str = RUBRIC_VERSION,
) -> tuple[list[HistoricalReport], dict[str, Any]]:
    """Validate all selected reports before any PostgreSQL connection."""

    all_evaluations = load_brand3_sqlite_sv9_reports(
        database_path,
        rubric_version=rubric_version,
    )
    capture_payloads = latest_brand3_reports_per_capture(
        all_evaluations
    )
    reports = [
        parse_report(_with_stable_capture_report_id(payload))
        for payload in capture_payloads
    ]
    reports.sort(
        key=lambda report: (
            report.observed_at,
            report.source_report_id,
        )
    )
    manifest_rows = [
        {
            "source_report_id": report.source_report_id,
            "source_run_id": report.source_run_id,
            "canonical_domain": report.canonical_domain,
            "observed_at": report.observed_at.isoformat(),
            "report_hash": report.report_hash,
            "capture_hash": report.capture_hash,
        }
        for report in reports
    ]
    source_path = Path(database_path).expanduser().resolve()
    plan: dict[str, Any] = {
        "schema_version": BRAND3_ARCHIVE_HISTORY_IMPORT_VERSION,
        "policy_version": (
            BRAND3_ARCHIVE_HISTORY_IMPORT_POLICY_VERSION
        ),
        "mode": "archive_workspace",
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "mutates_source_archive": False,
        "requires_explicit_apply": True,
        "workspace": {
            "slug": BRAND3_ARCHIVE_WORKSPACE_SLUG,
            "name": BRAND3_ARCHIVE_WORKSPACE_NAME,
            "operational_workspace": False,
        },
        "source_database": {
            "path": str(source_path),
            "manifest_fingerprint": stable_artifact_digest(
                "brand3-sqlite-history-import-manifest-v1",
                {"reports": manifest_rows},
            ),
        },
        "rubric_version": str(rubric_version),
        "summary": {
            "archive_evaluation_count": len(all_evaluations),
            "capture_report_count": len(reports),
            "excluded_evaluator_revision_count": (
                len(all_evaluations) - len(reports)
            ),
            "brand_count": len(
                {report.canonical_domain for report in reports}
            ),
            "evidence_record_count": sum(
                len(report.evidence_records) for report in reports
            ),
            "component_evaluation_count": sum(
                len(report.components) for report in reports
            ),
            "tile_verdict_count": sum(
                _tile_count(report) for report in reports
            ),
            "first_observed_at": _iso_or_none(
                min(
                    (report.observed_at for report in reports),
                    default=None,
                )
            ),
            "latest_observed_at": _iso_or_none(
                max(
                    (report.observed_at for report in reports),
                    default=None,
                )
            ),
        },
        "source_report_ids": [
            report.source_report_id for report in reports
        ],
        "warnings": [
            "legacy_five_dimension_scores_are_not_imported",
            "only_matching_sv9_rubric_scans_are_imported",
            "latest_evaluation_per_capture_only",
            "archive_workspace_isolated_from_operational_b3s_reads",
            "imported_scores_remain_legacy_archive_observations",
            "source_sqlite_is_opened_read_only",
        ],
    }
    plan["state_fingerprint"] = stable_artifact_digest(
        BRAND3_ARCHIVE_HISTORY_IMPORT_VERSION,
        {
            key: value
            for key, value in plan.items()
            if key != "state_fingerprint"
        },
    )
    return reports, plan


def _with_stable_capture_report_id(
    payload: dict[str, Any],
) -> dict[str, Any]:
    source_run_id = (payload.get("raw") or {}).get("source_run_id")
    if source_run_id is None:
        return payload
    normalized = dict(payload)
    normalized["id"] = f"brand3-sqlite-capture-{source_run_id}"
    return normalized


def _tile_count(report: HistoricalReport) -> int:
    components = report.evaluation_result.get("components")
    components = components if isinstance(components, dict) else {}
    return sum(
        len(component.get("tile_profile") or [])
        for component in components.values()
        if isinstance(component, dict)
    )


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
