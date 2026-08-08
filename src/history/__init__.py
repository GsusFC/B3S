"""PostgreSQL-backed history for observed brand state and evaluations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.history.models import HistoricalReport, ImportOutcome, ReportConflictError, ReportImportError
from src.history.report_parser import parse_report

if TYPE_CHECKING:
    from src.history.repository import PostgresHistoryRepository

__all__ = [
    "HistoricalReport",
    "ImportOutcome",
    "PostgresHistoryRepository",
    "ReportConflictError",
    "ReportImportError",
    "parse_report",
]


def __getattr__(name: str) -> type[PostgresHistoryRepository]:
    """Load the repository lazily so history leaf modules remain importable."""
    if name != "PostgresHistoryRepository":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from src.history.repository import PostgresHistoryRepository

    globals()[name] = PostgresHistoryRepository
    return PostgresHistoryRepository
