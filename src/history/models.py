"""Typed import contracts for the B3S historical store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


class ReportImportError(ValueError):
    """A report cannot satisfy the historical import contract."""


class ReportConflictError(ReportImportError):
    """A source report id already exists with different immutable content."""


@dataclass(frozen=True, slots=True)
class HistoricalReport:
    source_report_id: str
    source_run_id: str
    brand_name: str
    canonical_domain: str
    canonical_url: str
    observed_at: datetime
    recorded_at: datetime
    evaluated_at: datetime
    pipeline_version: str
    rubric_version: str
    prompt_version: str
    evaluator_model: str
    gate_authority: str
    score: float | None
    base_average: float | None
    reliability_status: str
    acquisition_state: str
    report_hash: str
    capture_hash: str
    limitations: tuple[str, ...]
    not_detected: tuple[str, ...]
    evidence_records: tuple[dict[str, Any], ...]
    block_interpretations: tuple[dict[str, Any], ...]
    components: tuple[dict[str, Any], ...]
    acquisition_attempts: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]
    capture_payload: dict[str, Any]
    evaluation_result: dict[str, Any]
    evaluation_config: dict[str, Any]
    report_payload: dict[str, Any]


ImportStatus = Literal["imported", "unchanged"]


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    source_report_id: str
    status: ImportStatus
    brand_id: str
    capture_id: str
    evaluation_run_id: str
    report_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_report_id": self.source_report_id,
            "status": self.status,
            "brand_id": self.brand_id,
            "capture_id": self.capture_id,
            "evaluation_run_id": self.evaluation_run_id,
            "report_hash": self.report_hash,
        }
