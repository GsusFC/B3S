"""PostgreSQL-first report reads with a file compatibility fallback.

New scans still write one JSON file per scan under data/reports/ for rollback.
When configured, PostgreSQL mirrors those writes and serves the historical read
model, including imported reports that no longer exist on the mounted volume.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping
from urllib.parse import urlparse

from src.history.models import ReportConflictError
from src.history.report_parser import canonical_json_hash
from src.services.evidence_claim_memory import build_evidence_claim_memory
from src.services.evidence_claim_reconciliation import (
    EvidenceClaimReconciliationCommand,
    EvidenceClaimReconciliationError,
    EvidenceClaimReconciliationNotFoundError,
    EvidenceClaimReconciliationUnavailableError,
    claim_relation_subject,
)
from src.services.evidence_claim_tile_ledger import (
    build_evidence_claim_tile_ledger,
    evidence_claim_tile_ledger_mode,
)
from src.services.evidence_claim_tile_review import (
    EvidenceClaimTileReviewCommand,
    EvidenceClaimTileReviewJournalError,
    EvidenceClaimTileReviewPacketNotFoundError,
    EvidenceClaimTileReviewUnavailableError,
)
from src.services.evidence_memory_adjudication import (
    EvidenceMemoryAdjudicationCommand,
    EvidenceMemoryAdjudicationError,
    EvidenceMemoryAdjudicationNotFoundError,
    EvidenceMemoryAdjudicationUnavailableError,
    evidence_subject_exists,
)
from src.services.evidence_ledger_shadow import (
    build_evidence_ledger_shadow,
    evidence_ledger_mode,
)
from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)
from src.services.evidence_scoring_recovery_review import (
    EvidenceScoringRecoveryJournalError,
    EvidenceScoringRecoveryReviewCommand,
    EvidenceScoringRecoveryReviewNotFoundError,
    EvidenceScoringRecoveryReviewUnavailableError,
    build_reviewed_scoring_memory_shadow,
    recovery_review_subject,
)
from src.services.scanner_evidence_comparison import (
    annotate_report_history,
    selected_report_for_display,
)
from src.services.scanner_report_assessment import (
    ScannerReportAssessmentError,
    assessment_projection_from_report,
)


_LOG = logging.getLogger(__name__)
_POSTGRES_PAGE_SIZE = 200
_POSTGRES_INDEX_PAYLOAD_LIMIT = 1000


def reports_dir() -> Path:
    return Path(os.environ.get("B3S_REPORTS_DIR", "data/reports"))


def verify_postgres_runtime_ready() -> None:
    """Fail startup when an explicitly required PostgreSQL head is unavailable."""

    required = os.environ.get("B3S_POSTGRES_REQUIRED", "").strip().lower()
    if required in {"", "false"}:
        return
    if required != "true":
        raise RuntimeError("B3S_POSTGRES_REQUIRED must be true or false")
    database_url = os.environ.get("B3S_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("required PostgreSQL history is not configured")
    try:
        repository = _postgres_repository_for_url(database_url)
        repository.verify_migration_head()
    except Exception:
        raise RuntimeError(
            "required PostgreSQL history failed schema verification"
        ) from None


def _postgres_repository():
    # Tests and local tooling may point the file store at a temporary directory;
    # keep that explicit override authoritative even when a .env has a DSN.
    configured_reports_dir = os.environ.get("B3S_REPORTS_DIR")
    if configured_reports_dir and configured_reports_dir != "/data/reports":
        return None
    database_url = os.environ.get("B3S_DATABASE_URL", "").strip()
    if not database_url:
        return None
    try:
        return _postgres_repository_for_url(database_url)
    except Exception:
        _LOG.exception("failed to configure postgres history repository")
        return None


@lru_cache(maxsize=4)
def _postgres_repository_for_url(database_url: str):
    from src.history.repository import PostgresHistoryRepository

    return PostgresHistoryRepository(database_url, schema_policy="verify_head")


def vault_sv9_shadow_diagnostics_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Enable diagnostic reads only in the isolated Vault and only explicitly."""

    values = os.environ if environment is None else environment
    return (
        str(values.get("BRAND3_ENVIRONMENT") or "").strip().casefold() == "vault"
        and str(
            values.get("B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED") or ""
        ).strip().casefold()
        == "true"
    )


def vault_sv9_shadow_diagnostics_for_domain(
    domain: str,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Return a bounded diagnostic history without falling back to raw reports."""

    if not vault_sv9_shadow_diagnostics_enabled():
        return {"enabled": False, "available": False, "items": []}
    normalized = domain_key(domain)
    repository = _postgres_repository()
    if not normalized or repository is None:
        return {
            "enabled": True,
            "available": False,
            "domain": normalized,
            "items": [],
            "message": "SV9 shadow diagnostics are temporarily unavailable.",
        }
    try:
        result = repository.list_evidence_vault_operational_sv9_shadow_diagnostics(
            normalized,
            limit=limit,
        )
    except Exception:
        _LOG.error(
            "failed to read protected SV9 shadow diagnostics",
            extra={"domain": normalized},
        )
        return {
            "enabled": True,
            "available": False,
            "domain": normalized,
            "items": [],
            "message": "SV9 shadow diagnostics are temporarily unavailable.",
        }
    return {
        "enabled": True,
        "available": True,
        **result,
    }


def new_scan_id() -> str:
    return uuid.uuid4().hex[:12]


def report_path(scan_id: str) -> Path:
    return reports_dir() / f"{scan_id}.json"


def save_report(report: dict[str, Any]) -> None:
    # Validate the immutable SV9 identity before either persistence backend is
    # touched.  Legacy reports remain compatible; assessment-bearing reports
    # fail closed on any duplicate or projection drift.
    assessment_projection_from_report(report)
    path = report_path(str(report["id"]))
    if path.exists() or path.is_symlink():
        existing = _read_report_file(
            path,
            expected_id=str(report["id"]),
            require_id=True,
        )
        if isinstance(existing, dict) and existing != report:
            raise ReportConflictError(f"report {report['id']} already exists with different content")
    path.parent.mkdir(parents=True, exist_ok=True)
    repository = _postgres_repository()
    if repository is not None:
        try:
            repository.import_report(report)
        except ReportConflictError:
            raise
        except Exception:
            # The mounted file store remains the recovery source when history
            # cannot persist an otherwise valid web report.
            _LOG.exception(
                "failed to mirror report to postgres",
                extra={"scan_id": str(report.get("id") or "")},
            )
    _write_immutable_report_file(path, report)
    try:
        from web.scoring_store import record_report

        record_report(report)
    except Exception:
        pass


def load_report(scan_id: str) -> dict[str, Any] | None:
    postgres_report: dict[str, Any] | None = None
    read_error: Exception | None = None
    repository = _postgres_repository()
    if repository is not None:
        try:
            report = repository.get_report_payload(scan_id)
            if report is not None:
                if not isinstance(report, dict):
                    raise ScannerReportAssessmentError(
                        "postgres_report_payload_invalid"
                    )
                assessment_projection_from_report(report)
                postgres_report = report
        except (ReportConflictError, ScannerReportAssessmentError):
            raise
        except Exception as exc:
            read_error = exc
            _LOG.exception("failed to load report from postgres", extra={"scan_id": str(scan_id)})
    path = report_path(scan_id)
    file_report: dict[str, Any] | None = None
    if path.exists() or path.is_symlink():
        file_report = _read_report_file(path, expected_id=str(scan_id))
    if postgres_report is not None and file_report is not None:
        _assert_duplicate_report_identity(
            postgres_report,
            file_report,
            report_id=str(scan_id),
        )
    if postgres_report is None and file_report is None:
        if read_error is not None:
            raise read_error
        # A configured backend that could not initialize is also unknown,
        # not confirmed absence. Keep explicit local-only overrides unchanged.
        if repository is None and os.environ.get("B3S_DATABASE_URL", "").strip() and os.environ.get("B3S_REPORTS_DIR", "/data/reports") in {"", "/data/reports"}:
            raise RuntimeError("report history unavailable without a local fallback")
    return postgres_report or file_report


def list_report_payloads_for_index() -> list[dict[str, Any]]:
    """Return every validated report payload for one home-page request."""

    reports_by_id: dict[str, dict[str, Any]] = {}
    repository = _postgres_repository()
    if repository is not None:
        postgres_reports_by_id: dict[str, dict[str, Any]] = {}
        try:
            bulk_reader = getattr(repository, "list_report_payloads", None)
            summary_ids: list[str] = []
            bulk_count: int | None = None
            if callable(bulk_reader):
                bulk_rows = list(bulk_reader(limit=_POSTGRES_INDEX_PAYLOAD_LIMIT))
                bulk_count = len(bulk_rows)
                for report in bulk_rows:
                    if not isinstance(report, dict):
                        raise ScannerReportAssessmentError("postgres_report_payload_invalid")
                    report_id = str(report.get("id") or "")
                    if not report_id:
                        continue
                    if _is_report_summary(report):
                        summary_ids.append(report_id)
                        continue
                    assessment_projection_from_report(report)
                    postgres_reports_by_id[report_id] = report

            summary_reader = getattr(repository, "list_report_summaries", None)
            if callable(summary_reader):
                summaries = _all_postgres_pages(summary_reader)
                summary_ids.extend(str(row.get("id") or "") for row in summaries if str(row.get("id") or ""))
            elif bulk_count is not None and bulk_count >= _POSTGRES_INDEX_PAYLOAD_LIMIT:
                raise ScannerReportAssessmentError("postgres_report_payloads_incomplete")

            for report_id in dict.fromkeys(summary_ids):
                if report_id in postgres_reports_by_id:
                    continue
                payload = _postgres_report_payload_for_identity(
                    repository,
                    report_id,
                )
                if payload is None:
                    raise ScannerReportAssessmentError("postgres_report_payload_missing")
                postgres_reports_by_id[report_id] = payload
            reports_by_id.update(postgres_reports_by_id)
        except (ReportConflictError, ScannerReportAssessmentError):
            raise
        except Exception:
            _LOG.exception("failed to list report payloads for index from postgres")

    directory = reports_dir()
    if directory.is_dir():
        for path in directory.glob("*.json"):
            report = _read_report_file(path, expected_id=path.stem)
            report_id = str(report.get("id") or path.stem)
            existing = reports_by_id.get(report_id)
            if existing is not None:
                _assert_duplicate_report_identity(
                    existing,
                    report,
                    report_id=report_id,
                )
                continue
            reports_by_id[report_id] = report

    reports = list(reports_by_id.values())
    reports.sort(
        key=lambda report: (
            str(report.get("created_at") or ""),
            str(report.get("id") or ""),
        ),
        reverse=True,
    )
    return reports


def list_reports() -> list[dict[str, Any]]:
    """Return summary rows for every stored report, newest first."""

    rows_by_id: dict[str, dict[str, Any]] = {}
    repository = _postgres_repository()
    if repository is not None:
        try:
            for row in _all_postgres_pages(repository.list_report_summaries):
                report_id = str(row.get("id") or "")
                if report_id:
                    payload = _postgres_report_payload_for_identity(
                        repository,
                        report_id,
                    )
                    rows_by_id[report_id] = (
                        _summary_row(payload, fallback_id=report_id)
                        if payload is not None
                        else row
                    )
        except (ReportConflictError, ScannerReportAssessmentError):
            raise
        except Exception:
            _LOG.exception("failed to list reports from postgres")

    directory = reports_dir()
    if directory.is_dir():
        for path in directory.glob("*.json"):
            report = _read_report_file(path, expected_id=path.stem)
            row = _summary_row(report, fallback_id=path.stem)
            report_id = str(row.get("id") or path.stem)
            if report_id in rows_by_id:
                postgres_report = _postgres_report_payload_for_identity(
                    repository,
                    report_id,
                )
                if postgres_report is None:
                    candidate = rows_by_id[report_id]
                    if _looks_like_full_report(candidate):
                        postgres_report = candidate
                if postgres_report is not None:
                    _assert_duplicate_report_identity(
                        postgres_report,
                        report,
                        report_id=report_id,
                    )
            rows_by_id.setdefault(report_id, row)
    rows = list(rows_by_id.values())
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows


def domain_key(value: str) -> str:
    """Normalize a URL/domain for brand-level grouping."""

    candidate = (value or "").strip().lower()
    if not candidate:
        return ""
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    host = (parsed.hostname or candidate).strip(".")
    return host.removeprefix("www.")


def _report_history_for_domain(
    domain: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load the merged history and its PostgreSQL source once."""

    target = domain_key(domain)
    if not target:
        return [], []

    matches_by_id: dict[str, dict[str, Any]] = {}
    postgres_reports: list[dict[str, Any]] = []
    repository = _postgres_repository()
    if repository is not None:
        try:
            fetch_page = lambda **page: repository.list_report_payloads_for_domain(target, **page)
            for report in _all_postgres_pages(fetch_page):
                report_id = str(report.get("id") or "")
                if report_id:
                    matches_by_id[report_id] = report
                    postgres_reports.append(report)
        except (ReportConflictError, ScannerReportAssessmentError):
            raise
        except Exception:
            _LOG.exception("failed to list brand reports from postgres", extra={"domain": target})

    directory = reports_dir()
    if directory.is_dir():
        for path in directory.glob("*.json"):
            report = _read_report_file(path, expected_id=path.stem)
            if domain_key(str(report.get("url") or "")) == target:
                report_id = str(report.get("id") or path.stem)
                existing = matches_by_id.get(report_id)
                if existing is not None:
                    _assert_duplicate_report_identity(
                        existing,
                        report,
                        report_id=report_id,
                    )
                matches_by_id.setdefault(report_id, report)
    matches = list(matches_by_id.values())
    matches.sort(key=lambda report: str(report.get("created_at") or ""), reverse=True)
    postgres_reports.sort(
        key=lambda report: str(report.get("created_at") or ""),
        reverse=True,
    )
    return matches, postgres_reports


def list_reports_for_domain(domain: str) -> list[dict[str, Any]]:
    """Return full reports matching a normalized domain, newest first."""

    return _report_history_for_domain(domain)[0]


def brand_navigation_history_for_domain(
    domain: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return merged history plus its PostgreSQL source for one brand view."""

    return _report_history_for_domain(domain)


def vault_accepted_report_for_domain(
    domain: str,
    *,
    reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Resolve the report bound to Vault's accepted SV9 authority.

    Vault must not infer its current report from temporal history.  The
    authority projection is the source of truth; a report is usable only when
    its immutable scan id, domain, and validated assessment identity all agree
    with the accepted candidate.  Any unavailable or inconsistent input is a
    deliberate fail-closed result.
    """

    target = domain_key(domain)
    if not target:
        return None
    repository = _postgres_repository()
    if repository is None:
        return None
    try:
        authority = repository.get_evidence_vault_sv9_judgment_authority(
            target,
            workspace_slug="b3s",
        )
    except Exception:
        _LOG.exception(
            "failed to load Vault SV9 authority for report navigation",
            extra={"domain": target},
        )
        return None
    if not isinstance(authority, Mapping):
        return None
    candidate = authority.get("accepted_candidate")
    authority_assessment = authority.get("assessment")
    if not isinstance(candidate, Mapping) or not isinstance(
        authority_assessment, Mapping
    ):
        return None
    source_scan_id = str(candidate.get("source_scan_id") or "")
    candidate_assessment_fingerprint = str(
        candidate.get("assessment_fingerprint") or ""
    )
    authority_assessment_fingerprint = str(
        authority_assessment.get("assessment_fingerprint") or ""
    )
    if not source_scan_id or not candidate_assessment_fingerprint:
        return None
    if candidate_assessment_fingerprint != authority_assessment_fingerprint:
        return None

    history = reports if reports is not None else list_reports_for_domain(target)
    report = next(
        (
            item
            for item in history
            if isinstance(item, Mapping)
            and str(item.get("id") or "") == source_scan_id
        ),
        None,
    )
    if report is None or domain_key(str(report.get("url") or "")) != target:
        return None
    try:
        assessment = assessment_projection_from_report(report, required=True)
    except Exception:
        _LOG.exception(
            "accepted Vault report failed assessment validation",
            extra={"domain": target, "source_scan_id": source_scan_id},
        )
        return None
    if (
        assessment.get("availability") != "available"
        or assessment.get("assessment_fingerprint")
        != candidate_assessment_fingerprint
        or authority.get("score") != assessment.get("sv9_score")
        or authority_assessment.get("sv9_score") != assessment.get("sv9_score")
    ):
        return None
    return dict(report)


def classified_reports_for_domain(domain: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return immutable reports with a derived temporal-stability projection."""

    return annotate_report_history(list_reports_for_domain(domain))


def evidence_ledger_shadow_for_domain(domain: str) -> dict[str, Any]:
    """Return persisted shadow memory when current, otherwise derive it safely."""

    mode = evidence_ledger_mode()
    reports = list_reports_for_domain(domain)
    derived = build_evidence_ledger_shadow(reports, mode=mode)
    if mode != "shadow":
        return {
            **derived,
            "persistence": {"stored": False, "backend": "disabled"},
        }

    repository = _postgres_repository()
    if repository is not None:
        try:
            stored = repository.get_evidence_ledger_shadow(domain)
            if (
                isinstance(stored, dict)
                and stored.get("state_fingerprint") == derived.get("state_fingerprint")
                and stored.get("latest_report_id") == derived.get("latest_report_id")
            ):
                return {
                    **stored,
                    "persistence": {"stored": True, "backend": "postgres"},
                }
        except Exception:
            _LOG.exception(
                "failed to load evidence ledger shadow",
                extra={"domain": domain_key(domain)},
            )
    return {
        **derived,
        "persistence": {
            "stored": False,
            "backend": "history_derived",
        },
    }


def evidence_memory_identity_v2_for_domain(domain: str) -> dict[str, Any]:
    """Derive identity v2 and overlay durable, non-authoritative decisions."""

    events: list[dict[str, Any]] = []
    adjudication_persistence: dict[str, Any] = {
        "stored": False,
        "backend": "not_configured",
        "event_count": 0,
    }
    repository = _postgres_repository()
    if repository is not None:
        try:
            events = repository.list_current_evidence_memory_adjudications(
                domain
            )
            adjudication_persistence = {
                "stored": True,
                "backend": "postgres",
                "event_count": len(events),
            }
        except Exception:
            _LOG.exception(
                "failed to load evidence memory adjudications",
                extra={"domain": domain_key(domain)},
            )
            adjudication_persistence["backend"] = "unavailable"
    derived = build_evidence_memory_identity_v2(
        list_reports_for_domain(domain),
        mode="shadow",
        adjudications=events,
    )
    return {
        **derived,
        "persistence": {
            "stored": False,
            "backend": "history_derived",
            "adjudications": adjudication_persistence,
        },
    }


def evidence_claim_memory_for_domain(domain: str) -> dict[str, Any]:
    """Derive non-authoritative claim memory over immutable report history."""

    events: list[dict[str, Any]] = []
    adjudication_persistence: dict[str, Any] = {
        "stored": False,
        "backend": "not_configured",
        "event_count": 0,
    }
    reconciliation_events: list[dict[str, Any]] = []
    reconciliation_persistence: dict[str, Any] = {
        "stored": False,
        "backend": "not_configured",
        "event_count": 0,
    }
    repository = _postgres_repository()
    if repository is not None:
        try:
            events = repository.list_current_evidence_memory_adjudications(
                domain
            )
            adjudication_persistence = {
                "stored": True,
                "backend": "postgres",
                "event_count": len(events),
            }
        except Exception:
            _LOG.exception(
                "failed to load evidence memory adjudications for claim memory",
                extra={"domain": domain_key(domain)},
            )
            adjudication_persistence["backend"] = "unavailable"
        try:
            reconciliation_events = (
                repository.list_current_evidence_claim_reconciliations(
                    domain
                )
            )
            reconciliation_persistence = {
                "stored": True,
                "backend": "postgres",
                "event_count": len(reconciliation_events),
            }
        except Exception:
            _LOG.exception(
                "failed to load evidence claim reconciliations",
                extra={"domain": domain_key(domain)},
            )
            reconciliation_persistence["backend"] = "unavailable"
    derived = build_evidence_claim_memory(
        list_reports_for_domain(domain),
        mode="shadow",
        evidence_adjudications=events,
        claim_reconciliations=reconciliation_events,
    )
    return {
        **derived,
        "persistence": {
            "stored": False,
            "backend": "history_derived",
            "adjudications": adjudication_persistence,
            "claim_reconciliations": reconciliation_persistence,
        },
    }


def evidence_claim_tile_ledger_for_domain(
    domain: str,
    *,
    reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the current persisted mapping projection when fingerprints agree."""

    mode = evidence_claim_tile_ledger_mode()
    if reports is None:
        reports = list_reports_for_domain(domain)
    derived = build_evidence_claim_tile_ledger(reports, mode=mode)
    if mode != "shadow":
        return {
            **derived,
            "persistence": {"stored": False, "backend": "disabled"},
            "reviewed_memory": _unavailable_reviewed_claim_tile_memory(
                "claim_tile_ledger_disabled"
            ),
        }

    repository = _postgres_repository()
    selected = derived
    persistence = {
        "stored": False,
        "backend": "history_derived",
    }
    reviewed_memory = _unavailable_reviewed_claim_tile_memory(
        "durable_review_journal_unavailable"
    )
    if repository is not None:
        try:
            stored = repository.get_evidence_claim_tile_ledger(domain)
            if (
                isinstance(stored, dict)
                and stored.get("state_fingerprint")
                == derived.get("state_fingerprint")
                and stored.get("latest_report_id")
                == derived.get("latest_report_id")
            ):
                selected = stored
                persistence = {
                    "stored": True,
                    "backend": "postgres",
                }
        except Exception:
            _LOG.exception(
                "failed to load evidence claim tile ledger",
                extra={"domain": domain_key(domain)},
            )
        if persistence["stored"]:
            try:
                reviewed = (
                    repository.get_reviewed_claim_tile_memory_shadow(
                        domain
                    )
                )
                if isinstance(reviewed, dict):
                    reviewed_memory = {
                        **reviewed,
                        "available": True,
                        "persistence": {
                            "stored": True,
                            "backend": (
                                "postgres_ledger_and_review_journal"
                            ),
                        },
                    }
                else:
                    reviewed_memory = (
                        _unavailable_reviewed_claim_tile_memory(
                            "no_current_packet_bound_reviews"
                        )
                    )
            except Exception:
                _LOG.exception(
                    "failed to rebuild reviewed claim tile memory",
                    extra={"domain": domain_key(domain)},
                )
                reviewed_memory = (
                    _unavailable_reviewed_claim_tile_memory(
                        "reviewed_memory_validation_failed",
                        status="invalid",
                    )
                )
        else:
            reviewed_memory = _unavailable_reviewed_claim_tile_memory(
                "claim_tile_ledger_not_current"
            )
    return {
        **selected,
        "persistence": persistence,
        "reviewed_memory": reviewed_memory,
    }


def _unavailable_reviewed_claim_tile_memory(
    reason: str,
    *,
    status: str = "unavailable",
) -> dict[str, Any]:
    return {
        "available": False,
        "status": status,
        "reason": reason,
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
        "accepted_mappings": [],
    }


def evidence_scoring_memory_preview_for_domain(
    domain: str,
    *,
    reports: list[dict[str, Any]] | None = None,
    postgres_reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return candidate and reviewed scoring memory from durable history."""

    repository = _postgres_repository()
    if repository is not None:
        try:
            if postgres_reports is None:
                stored = repository.get_evidence_scoring_memory_preview(domain)
            else:
                stored = repository.get_evidence_scoring_memory_preview(
                    domain,
                    reports=postgres_reports,
                )
            if isinstance(stored, dict):
                return {
                    **stored,
                    "persistence": {
                        "stored": True,
                        "backend": "postgres_history_derived",
                        "review_journal": "postgres",
                    },
                }
        except Exception:
            _LOG.exception(
                "failed to load evidence scoring memory preview",
                extra={"domain": domain_key(domain)},
            )
    if reports is None:
        reports = list_reports_for_domain(domain)
    derived = build_reviewed_scoring_memory_shadow(
        reports,
        claim_tile_ledger=build_evidence_claim_tile_ledger(
            reports,
            mode="shadow",
        ),
    )
    return {
        **derived,
        "persistence": {
            "stored": False,
            "backend": "history_derived",
            "review_journal": "unavailable",
        },
    }


def append_evidence_scoring_recovery_review_for_domain(
    domain: str,
    command: EvidenceScoringRecoveryReviewCommand,
) -> tuple[dict[str, Any], bool]:
    """Validate a projected recovery and append only to PostgreSQL."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceScoringRecoveryReviewUnavailableError(
            "The durable scoring recovery review journal is not configured."
        )
    try:
        projection = repository.get_evidence_scoring_memory_preview(domain)
        if not isinstance(projection, dict):
            raise EvidenceScoringRecoveryReviewNotFoundError(
                "The brand has no scoring-memory history."
            )
        subject = recovery_review_subject(
            projection.get("recovery_review_candidates") or [],
            command.subject_id,
        )
        if (
            subject is None
            or str(subject.get("case_id") or "") != command.case_id
        ):
            raise EvidenceScoringRecoveryReviewNotFoundError(
                "The scoring recovery does not exist in this brand's "
                "immutable history."
            )
        return repository.append_evidence_scoring_recovery_review(
            domain,
            command,
        )
    except EvidenceScoringRecoveryJournalError:
        raise
    except Exception as exc:
        raise EvidenceScoringRecoveryReviewUnavailableError(
            "The durable scoring recovery review journal is unavailable."
        ) from exc


def append_evidence_claim_tile_review_for_domain(
    domain: str,
    command: EvidenceClaimTileReviewCommand,
) -> tuple[dict[str, Any], bool]:
    """Append a semantic mapping decision only to durable PostgreSQL."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceClaimTileReviewUnavailableError(
            "The durable claim-to-tile review journal is not configured."
        )
    try:
        return repository.append_evidence_claim_tile_review(
            domain,
            command,
        )
    except EvidenceClaimTileReviewJournalError:
        raise
    except Exception as exc:
        raise EvidenceClaimTileReviewUnavailableError(
            "The durable claim-to-tile review journal is unavailable."
        ) from exc


def register_evidence_claim_tile_review_packet_for_domain(
    domain: str,
) -> tuple[dict[str, Any], bool]:
    """Build and append the exact private reviewer packet to PostgreSQL."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceClaimTileReviewUnavailableError(
            "The durable claim-to-tile review packet registry is not "
            "configured."
        )
    try:
        return repository.register_evidence_claim_tile_review_packet(
            domain
        )
    except EvidenceClaimTileReviewJournalError:
        raise
    except Exception as exc:
        raise EvidenceClaimTileReviewUnavailableError(
            "The durable claim-to-tile review packet registry is "
            "unavailable."
        ) from exc


def get_evidence_claim_tile_review_packet_for_domain(
    domain: str,
    packet_fingerprint: str,
) -> dict[str, Any]:
    """Read one exact registered private reviewer packet."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceClaimTileReviewUnavailableError(
            "The durable claim-to-tile review packet registry is not "
            "configured."
        )
    try:
        return repository.get_evidence_claim_tile_review_packet(
            domain,
            packet_fingerprint,
        )
    except (
        EvidenceClaimTileReviewPacketNotFoundError,
        EvidenceClaimTileReviewUnavailableError,
    ):
        raise
    except Exception as exc:
        raise EvidenceClaimTileReviewUnavailableError(
            "The durable claim-to-tile review packet registry is "
            "unavailable."
        ) from exc


def get_evidence_claim_tile_review_queue_for_domain(
    domain: str,
    packet_fingerprint: str,
) -> dict[str, Any]:
    """Read the derived private review queue for one exact packet."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceClaimTileReviewUnavailableError(
            "The durable claim-to-tile review packet registry is not "
            "configured."
        )
    try:
        return repository.get_evidence_claim_tile_review_queue(
            domain,
            packet_fingerprint,
        )
    except (
        EvidenceClaimTileReviewPacketNotFoundError,
        EvidenceClaimTileReviewUnavailableError,
    ):
        raise
    except Exception as exc:
        raise EvidenceClaimTileReviewUnavailableError(
            "The claim-to-tile review queue is unavailable."
        ) from exc


def list_evidence_claim_tile_reviews_for_domain(
    domain: str,
    *,
    subject_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Read the durable semantic claim-to-tile review journal."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceClaimTileReviewUnavailableError(
            "The durable claim-to-tile review journal is not configured."
        )
    try:
        return repository.list_evidence_claim_tile_reviews(
            domain,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    except EvidenceClaimTileReviewJournalError:
        raise
    except Exception as exc:
        raise EvidenceClaimTileReviewUnavailableError(
            "The durable claim-to-tile review journal is unavailable."
        ) from exc


def list_evidence_scoring_recovery_reviews_for_domain(
    domain: str,
    *,
    subject_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Read the durable semantic-recovery review journal."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceScoringRecoveryReviewUnavailableError(
            "The durable scoring recovery review journal is not configured."
        )
    try:
        return repository.list_evidence_scoring_recovery_reviews(
            domain,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    except EvidenceScoringRecoveryJournalError:
        raise
    except Exception as exc:
        raise EvidenceScoringRecoveryReviewUnavailableError(
            "The durable scoring recovery review journal is unavailable."
        ) from exc


def append_evidence_claim_reconciliation_for_domain(
    domain: str,
    command: EvidenceClaimReconciliationCommand,
) -> tuple[dict[str, Any], bool]:
    """Validate a projected relation and append only to its durable journal."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceClaimReconciliationUnavailableError(
            "The durable claim reconciliation journal is not configured."
        )
    projection = build_evidence_claim_memory(
        list_reports_for_domain(domain),
        mode="shadow",
    )
    subject = claim_relation_subject(projection, command.subject_id)
    if subject is None:
        raise EvidenceClaimReconciliationNotFoundError(
            "The claim relation does not exist in this brand's immutable history."
        )
    try:
        return repository.append_evidence_claim_reconciliation(
            domain,
            command,
            relation_type=str(subject.get("relation") or ""),
        )
    except EvidenceClaimReconciliationError:
        raise
    except Exception as exc:
        raise EvidenceClaimReconciliationUnavailableError(
            "The durable claim reconciliation journal is unavailable."
        ) from exc


def list_evidence_claim_reconciliations_for_domain(
    domain: str,
    *,
    subject_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Read the durable, reviewable claim-reconciliation journal."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceClaimReconciliationUnavailableError(
            "The durable claim reconciliation journal is not configured."
        )
    try:
        return repository.list_evidence_claim_reconciliations(
            domain,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    except EvidenceClaimReconciliationError:
        raise
    except Exception as exc:
        raise EvidenceClaimReconciliationUnavailableError(
            "The durable claim reconciliation journal is unavailable."
        ) from exc


def append_evidence_memory_adjudication_for_domain(
    domain: str,
    command: EvidenceMemoryAdjudicationCommand,
) -> tuple[dict[str, Any], bool]:
    """Validate a projected subject and append only to the durable journal."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceMemoryAdjudicationUnavailableError(
            "The durable evidence adjudication journal is not configured."
        )
    projection = build_evidence_memory_identity_v2(
        list_reports_for_domain(domain),
        mode="shadow",
    )
    if not evidence_subject_exists(projection, command.subject_id):
        raise EvidenceMemoryAdjudicationNotFoundError(
            "The evidence subject does not exist in this brand's immutable history."
        )
    try:
        return repository.append_evidence_memory_adjudication(
            domain,
            command,
        )
    except EvidenceMemoryAdjudicationError:
        raise
    except Exception as exc:
        raise EvidenceMemoryAdjudicationUnavailableError(
            "The durable evidence adjudication journal is unavailable."
        ) from exc


def list_evidence_memory_adjudications_for_domain(
    domain: str,
    *,
    subject_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Read the durable, reviewable adjudication journal."""

    repository = _postgres_repository()
    if repository is None:
        raise EvidenceMemoryAdjudicationUnavailableError(
            "The durable evidence adjudication journal is not configured."
        )
    try:
        return repository.list_evidence_memory_adjudications(
            domain,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    except EvidenceMemoryAdjudicationError:
        raise
    except Exception as exc:
        raise EvidenceMemoryAdjudicationUnavailableError(
            "The durable evidence adjudication journal is unavailable."
        ) from exc


def current_report_for_domain(
    domain: str,
    *,
    mode: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    """Select the visible canonical/provisional report for one brand."""

    reports = list_reports_for_domain(domain)
    selected, classified, state = selected_report_for_display(reports, mode=mode)
    if os.environ.get("BRAND3_ENVIRONMENT", "").strip().lower() == "vault":
        selected = vault_accepted_report_for_domain(domain, reports=reports)
        state = dict(state)
        selected_id = str((selected or {}).get("id") or "")
        # Keep the historical state shape, but make its selected identity
        # describe the accepted Vault report rather than the temporal choice.
        state["selected_report_id"] = selected_id or None
        state["canonical_report_id"] = selected_id or None
        state["provisional_report_id"] = None
    return selected, classified, state


def _summary_row(report: dict[str, Any], *, fallback_id: str = "") -> dict[str, Any]:
    assessment = assessment_projection_from_report(report)
    availability = str(assessment.get("availability") or "legacy")
    score = (
        assessment.get("sv9_score")
        if availability == "available"
        else (
            assessment.get("raw_score")
            if availability == "legacy"
            else None
        )
    )
    return {
        "id": report.get("id") or fallback_id,
        "brand_name": report.get("brand_name") or "",
        "url": report.get("url") or "",
        "created_at": report.get("created_at") or "",
        "score": score,
        "base_average": (
            assessment.get("base_average")
            if availability == "available"
            else assessment.get("raw_base_average")
        ),
        "magnetism_capped": (
            assessment.get("magnetism_capped")
            if availability == "available"
            else assessment.get("raw_magnetism_capped")
        ),
        "assessment_availability": availability,
        "assessment_fingerprint": assessment.get("assessment_fingerprint"),
        "score_fingerprint": assessment.get("score_fingerprint"),
        "detected_count": report.get("detected_count"),
        "block_count": report.get("block_count"),
        "not_detected": report.get("not_detected") or [],
        "canonical_status": report.get("canonical_status") or "",
        "stability": (
            dict(report.get("stability"))
            if isinstance(report.get("stability"), dict)
            else {}
        ),
    }


def _write_immutable_report_file(path: Path, report: dict[str, Any]) -> None:
    serialized = json.dumps(report, ensure_ascii=False, indent=1)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write(serialized)
            temporary_path = Path(handle.name)
        try:
            os.link(temporary_path, path)
            return
        except FileExistsError:
            pass
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    existing = _read_report_file(
        path,
        expected_id=str(report["id"]),
        require_id=True,
    )
    if existing != report:
        raise ReportConflictError(f"report {report['id']} already exists with different content")


def _read_report_file(
    path: Path,
    *,
    expected_id: str | None = None,
    require_id: bool = False,
) -> dict[str, Any]:
    """Read and validate an existing report file without treating failures as absence."""

    if not path.is_file():
        report_id = expected_id or path.stem
        raise ReportConflictError(
            f"report {report_id} already exists but is unreadable"
        )
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report_id = expected_id or path.stem
        raise ReportConflictError(
            f"report {report_id} already exists but is unreadable"
        ) from exc
    if not isinstance(loaded, dict):
        raise ScannerReportAssessmentError("file_report_payload_invalid")

    actual_id = str(loaded.get("id") or "")
    if expected_id is not None:
        if actual_id and actual_id != expected_id:
            raise ReportConflictError(
                f"report {expected_id} file store payload has unexpected identity"
            )
        if require_id and actual_id != expected_id:
            raise ReportConflictError(
                f"report {expected_id} file store payload has unexpected identity"
            )
    assessment_projection_from_report(loaded)
    return loaded


def _all_postgres_pages(fetch_page) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = fetch_page(limit=_POSTGRES_PAGE_SIZE, offset=offset)
        for item in page:
            if not isinstance(item, dict):
                raise ScannerReportAssessmentError("postgres_report_payload_invalid")
            if not _is_report_summary(item):
                assessment_projection_from_report(item)
            rows.append(item)
        if len(page) < _POSTGRES_PAGE_SIZE:
            return rows
        offset += _POSTGRES_PAGE_SIZE


def _is_report_summary(value: Mapping[str, Any]) -> bool:
    """Recognize the flattened PostgreSQL summary projection, not a payload."""

    return (
        "assessment_availability" in value
        and "sv9_assessment" not in value
        and "raw" not in value
        and not isinstance(value.get("components"), list)
    )


def _postgres_report_payload_for_identity(
    repository: Any,
    report_id: str,
) -> dict[str, Any] | None:
    """Load one full PostgreSQL payload for a same-ID file comparison."""

    getter = getattr(repository, "get_report_payload", None)
    if not callable(getter):
        return None
    try:
        payload = getter(report_id)
    except AttributeError:
        # Small compatibility/test repositories may expose summaries only.
        return None
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ScannerReportAssessmentError("postgres_report_payload_invalid")
    assessment_projection_from_report(payload)
    return payload


def _looks_like_full_report(value: Mapping[str, Any]) -> bool:
    """Recognize a full payload returned by a test/compatibility repository."""

    return isinstance(value.get("raw"), Mapping) or "sv9_assessment" in value


def _assert_duplicate_report_identity(
    postgres_report: Mapping[str, Any],
    file_report: Mapping[str, Any],
    *,
    report_id: str,
) -> None:
    """Reject two stores claiming one id while carrying different JSON."""

    assessment_projection_from_report(postgres_report)
    assessment_projection_from_report(file_report)
    if canonical_json_hash(dict(postgres_report)) != canonical_json_hash(
        dict(file_report)
    ):
        raise ReportConflictError(
            f"report {report_id} already exists in postgres and file stores with different content"
        )
