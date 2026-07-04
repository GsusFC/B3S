"""FastAPI app exposing Brand3 Scoring as a web backend."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.services import brand_service
from src.storage.sqlite_store import SQLiteStore
from src.config import BRAND3_DB_PATH
from src.url_validator import validate_url


class AnalyzeRequest(BaseModel):
    url: str
    brand_name: str | None = None
    use_llm: bool = True
    use_social: bool = True


class GateConfigRequest(BaseModel):
    max_composite_drop: float | None = None
    dimension_drops: dict[str, float] | None = None


class PromoteBaselineRequest(BaseModel):
    label: str | None = None
    force: bool = False


class RollbackResponse(BaseModel):
    rolled_back: bool
    target_version_id: int
    rollback_source_version_id: int
    restored_version_id: int
    label: str


class AnalyzeResponse(BaseModel):
    brand: str
    brand_profile: dict[str, Any]
    url: str
    run_id: int | None
    niche_classification: dict[str, Any]
    calibration_profile: str
    profile_source: str
    data_quality: str
    data_sources: dict[str, Any]
    context_readiness: dict[str, Any] | None = None
    confidence_summary: dict[str, Any] | None = None
    dimension_confidence: dict[str, Any] | None = None
    evidence_summary: dict[str, Any] | None = None
    trust_summary: dict[str, Any] | None = None
    composite_score: float | None
    composite_reliable: bool
    partial_score: bool
    partial_dimensions: list[str]
    dimensions: dict[str, Any]
    llm_used: bool
    social_scraped: bool
    audit: dict[str, Any]
    timestamp: str


class AnalysisJobResponse(BaseModel):
    id: int
    url: str
    brand_name: str | None = None
    brand_domain: str | None = None
    brand_logo_key: str | None = None
    brand_logo_url: str | None = None
    brand_profile: dict[str, Any]
    predicted_niche: str | None = None
    predicted_subtype: str | None = None
    niche_confidence: float | None = None
    calibration_profile: str | None = None
    profile_source: str | None = None
    use_llm: int
    use_social: int
    status: str
    phase: str | None = None
    cancel_requested: int
    attempt_count: int
    requested_at: str
    started_at: str | None = None
    completed_at: str | None = None
    queue_duration_seconds: float | None = None
    run_duration_seconds: float | None = None
    total_duration_seconds: float | None = None
    run_id: int | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []


def build_app() -> FastAPI:
    app = FastAPI(
        title="Brand3 Scoring API",
        version="0.1.0",
        description="Backend API for brand scoring, history, experiments, and calibration.",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def root() -> dict[str, Any]:
        return {
            "name": "Brand3 Scoring API",
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/health",
        }

    @app.post("/api/analyze", response_model=AnalyzeResponse)
    def analyze_brand(payload: AnalyzeRequest) -> dict[str, Any]:
        valid, normalized_or_error = validate_url(payload.url)
        if not valid:
            raise HTTPException(status_code=400, detail=f"URL rejected: {normalized_or_error}")
        try:
            return brand_service.run(
                normalized_or_error,
                brand_name=payload.brand_name,
                use_llm=payload.use_llm,
                use_social=payload.use_social,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/analyze/jobs", response_model=AnalysisJobResponse, status_code=202)
    def analyze_brand_async(payload: AnalyzeRequest) -> dict[str, Any]:
        valid, normalized_or_error = validate_url(payload.url)
        if not valid:
            raise HTTPException(status_code=400, detail=f"URL rejected: {normalized_or_error}")
        try:
            return brand_service.enqueue_analysis_job(
                normalized_or_error,
                brand_name=payload.brand_name,
                use_llm=payload.use_llm,
                use_social=payload.use_social,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/analyze/jobs/{job_id}/retry", response_model=AnalysisJobResponse, status_code=202)
    def retry_analysis_job(job_id: int) -> dict[str, Any]:
        try:
            return brand_service.retry_analysis_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/analyze/jobs/{job_id}/cancel", response_model=AnalysisJobResponse)
    def cancel_analysis_job(job_id: int) -> dict[str, Any]:
        try:
            return brand_service.cancel_analysis_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/analyze/jobs", response_model=list[AnalysisJobResponse])
    def get_analysis_jobs(
        brand_name: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return brand_service.list_analysis_jobs(
            brand_name=brand_name,
            status=status,
            limit=limit,
        )

    @app.get("/api/analyze/jobs/{job_id}", response_model=AnalysisJobResponse)
    def get_analysis_job(job_id: int) -> dict[str, Any]:
        try:
            return brand_service.get_analysis_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs")
    def get_runs(
        brand_name: str | None = None,
        url: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return brand_service.list_runs(brand_name=brand_name, url=url, limit=limit)

    @app.get("/api/brands")
    def get_brands(limit: int = 50) -> list[dict[str, Any]]:
        return brand_service.list_brands(limit=limit)

    @app.get("/api/profiles")
    def get_profiles() -> list[dict[str, Any]]:
        return brand_service.list_profiles()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> dict[str, Any]:
        try:
            return brand_service.show_run(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/evidence")
    def get_run_evidence(
        run_id: int,
        dimension: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        store = SQLiteStore(BRAND3_DB_PATH)
        try:
            if not store.get_run_snapshot(run_id):
                raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
            return store.get_run_evidence(
                run_id,
                dimension_name=(dimension or "").strip() or None,
                source=(source or "").strip() or None,
            )
        finally:
            store.close()

    @app.get("/api/runs/{run_id}/evidence-summary")
    def get_run_evidence_summary(run_id: int) -> dict[str, Any]:
        try:
            return brand_service.run_evidence_summary(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/dimension-confidence")
    def get_run_dimension_confidence(run_id: int) -> dict[str, Any]:
        try:
            return brand_service.run_dimension_confidence(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/trust-summary")
    def get_run_trust_summary(run_id: int) -> dict[str, Any]:
        try:
            return brand_service.run_trust_summary(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/brands/{brand_name}/report")
    def get_brand_report(brand_name: str, limit: int = 10) -> dict[str, Any]:
        return brand_service.brand_report(brand_name, limit=limit)

    @app.get("/api/experiments")
    def get_experiments(
        brand_name: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return brand_service.list_experiments(brand_name=brand_name, limit=limit)

    @app.get("/api/gate-config")
    def get_gate_config() -> dict[str, Any]:
        return brand_service.get_gate_config()

    @app.post("/api/gate-config")
    def update_gate_config(payload: GateConfigRequest) -> dict[str, Any]:
        return brand_service.set_gate_config(
            max_composite_drop=payload.max_composite_drop,
            dimension_drops=payload.dimension_drops,
        )

    @app.get("/api/baselines")
    def get_baselines(limit: int = 20) -> dict[str, Any]:
        return brand_service.list_baselines(limit=limit)

    @app.get("/api/versions/{version_id}/compare")
    def compare_version(version_id: int, brand_name: str) -> dict[str, Any]:
        try:
            return brand_service.compare_version(version_id, brand_name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/versions/{version_id}/promote")
    def promote_baseline(version_id: int, payload: PromoteBaselineRequest) -> dict[str, Any]:
        try:
            return brand_service.promote_baseline(
                version_id,
                label=payload.label,
                force=payload.force,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/versions/{version_id}/rollback", response_model=RollbackResponse)
    def rollback_version(version_id: int) -> dict[str, Any]:
        try:
            return brand_service.rollback_version(version_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


app = build_app()
