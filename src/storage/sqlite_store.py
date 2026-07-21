"""SQLite persistence for Brand3 Scoring runs."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .analysis_jobs import AnalysisJobsStoreMixin
from .annotations_reports import AnnotationsReportsStoreMixin
from .brand_runs import BrandRunsStoreMixin
from .calibration_candidates import CalibrationCandidatesStoreMixin
from .calibration_versions import CalibrationVersionsStoreMixin
from .evidence_items import EvidenceItemsStoreMixin
from .experiments import ExperimentsStoreMixin
from .features_scores import FeaturesScoresStoreMixin
from .json_payloads import MalformedJSONPayload as _MalformedJSONPayload
from .llm_cache import LLMCacheStoreMixin
from .raw_inputs import RawInputsStoreMixin
from .reviewed_scores import ReviewedScoresStoreMixin
from .run_audits import RunAuditsStoreMixin
from .run_snapshots import RunSnapshotsStoreMixin
from .scanner_api_jobs import ScannerApiJobsStoreMixin
from .schema import SchemaManagementStoreMixin


class SQLiteStore(
    SchemaManagementStoreMixin,
    AnalysisJobsStoreMixin,
    RawInputsStoreMixin,
    EvidenceItemsStoreMixin,
    LLMCacheStoreMixin,
    ReviewedScoresStoreMixin,
    CalibrationCandidatesStoreMixin,
    ExperimentsStoreMixin,
    CalibrationVersionsStoreMixin,
    FeaturesScoresStoreMixin,
    BrandRunsStoreMixin,
    RunAuditsStoreMixin,
    RunSnapshotsStoreMixin,
    AnnotationsReportsStoreMixin,
    ScannerApiJobsStoreMixin,
):
    """Persists runs, raw collector inputs, features, and scores in SQLite."""

    _schema_init_lock = threading.Lock()
    _schema_initialized: set[tuple[str, tuple[int, int], tuple[tuple[str, int, int], ...]]] = set()
    _schema_init_metrics: dict[str, int | float] = {
        "runs": 0,
        "skips": 0,
        "run_seconds": 0.0,
    }

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._ensure_schema_initialized()

    def _configure_connection(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self.conn.close()
