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
from typing import Any
from urllib.parse import urlparse

from src.history.models import ReportConflictError
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


_LOG = logging.getLogger(__name__)
_POSTGRES_PAGE_SIZE = 200


def reports_dir() -> Path:
    return Path(os.environ.get("B3S_REPORTS_DIR", "data/reports"))


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

    return PostgresHistoryRepository(database_url)


def new_scan_id() -> str:
    return uuid.uuid4().hex[:12]


def report_path(scan_id: str) -> Path:
    return reports_dir() / f"{scan_id}.json"


def save_report(report: dict[str, Any]) -> None:
    path = report_path(str(report["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and existing != report:
            raise ReportConflictError(f"report {report['id']} already exists with different content")
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
    repository = _postgres_repository()
    if repository is not None:
        try:
            report = repository.get_report_payload(scan_id)
            if report is not None:
                return report
        except Exception:
            _LOG.exception("failed to load report from postgres", extra={"scan_id": str(scan_id)})
    path = report_path(scan_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_reports() -> list[dict[str, Any]]:
    """Return summary rows for every stored report, newest first."""

    rows_by_id: dict[str, dict[str, Any]] = {}
    repository = _postgres_repository()
    if repository is not None:
        try:
            for row in _all_postgres_pages(repository.list_report_summaries):
                report_id = str(row.get("id") or "")
                if report_id:
                    rows_by_id[report_id] = row
        except Exception:
            _LOG.exception("failed to list reports from postgres")

    directory = reports_dir()
    if directory.is_dir():
        for path in directory.glob("*.json"):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            row = _summary_row(report, fallback_id=path.stem)
            report_id = str(row.get("id") or path.stem)
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


def list_reports_for_domain(domain: str) -> list[dict[str, Any]]:
    """Return full reports matching a normalized domain, newest first."""

    target = domain_key(domain)
    if not target:
        return []

    matches_by_id: dict[str, dict[str, Any]] = {}
    repository = _postgres_repository()
    if repository is not None:
        try:
            fetch_page = lambda **page: repository.list_report_payloads_for_domain(target, **page)
            for report in _all_postgres_pages(fetch_page):
                report_id = str(report.get("id") or "")
                if report_id:
                    matches_by_id[report_id] = report
        except Exception:
            _LOG.exception("failed to list brand reports from postgres", extra={"domain": target})

    directory = reports_dir()
    if directory.is_dir():
        for path in directory.glob("*.json"):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if domain_key(str(report.get("url") or "")) == target:
                report_id = str(report.get("id") or path.stem)
                matches_by_id.setdefault(report_id, report)
    matches = list(matches_by_id.values())
    matches.sort(key=lambda report: str(report.get("created_at") or ""), reverse=True)
    return matches


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
) -> dict[str, Any]:
    """Return the current persisted mapping projection when fingerprints agree."""

    mode = evidence_claim_tile_ledger_mode()
    reports = list_reports_for_domain(domain)
    derived = build_evidence_claim_tile_ledger(reports, mode=mode)
    if mode != "shadow":
        return {
            **derived,
            "persistence": {"stored": False, "backend": "disabled"},
        }

    repository = _postgres_repository()
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
                return {
                    **stored,
                    "persistence": {
                        "stored": True,
                        "backend": "postgres",
                    },
                }
        except Exception:
            _LOG.exception(
                "failed to load evidence claim tile ledger",
                extra={"domain": domain_key(domain)},
            )
    return {
        **derived,
        "persistence": {
            "stored": False,
            "backend": "history_derived",
        },
    }


def evidence_scoring_memory_preview_for_domain(
    domain: str,
) -> dict[str, Any]:
    """Return candidate and reviewed scoring memory from durable history."""

    repository = _postgres_repository()
    if repository is not None:
        try:
            stored = repository.get_evidence_scoring_memory_preview(
                domain
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
    derived = build_reviewed_scoring_memory_shadow(
        list_reports_for_domain(domain),
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

    return selected_report_for_display(list_reports_for_domain(domain), mode=mode)


def _summary_row(report: dict[str, Any], *, fallback_id: str = "") -> dict[str, Any]:
    return {
        "id": report.get("id") or fallback_id,
        "brand_name": report.get("brand_name") or "",
        "url": report.get("url") or "",
        "created_at": report.get("created_at") or "",
        "score": report.get("score"),
        "detected_count": report.get("detected_count"),
        "block_count": report.get("block_count"),
        "not_detected": report.get("not_detected") or [],
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

    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportConflictError(f"report {report['id']} already exists but is unreadable") from exc
    if existing != report:
        raise ReportConflictError(f"report {report['id']} already exists with different content")


def _all_postgres_pages(fetch_page) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = fetch_page(limit=_POSTGRES_PAGE_SIZE, offset=offset)
        rows.extend(item for item in page if isinstance(item, dict))
        if len(page) < _POSTGRES_PAGE_SIZE:
            return rows
        offset += _POSTGRES_PAGE_SIZE
