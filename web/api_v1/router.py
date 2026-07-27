"""HTTP routes for the B3S Scanner API v1."""

from __future__ import annotations

import copy
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from src.services.scanner_evidence_comparison import annotate_report_history
from web.report_store import domain_key, list_reports_for_domain
from web.scan_runner import approve_degraded_scan, cancel_scan

from .auth import ReadPrincipal, WritePrincipal
from .errors import ApiError
from .models import (
    ApiCapabilitiesResponse,
    ApiErrorResponse,
    BrandScanHistoryResponse,
    ScanCreateRequest,
    ScanEvidenceResponse,
    ScanResultResponse,
    ScanStatusResponse,
)
from .presenters import evidence_payload, report_etag, result_payload, status_payload
from .service import create_scan_job, get_completed_report, get_scan


router = APIRouter(prefix="/api/v1", tags=["B3S Scanner API"])

_ERRORS = {
    400: {"model": ApiErrorResponse, "description": "Invalid request"},
    401: {"model": ApiErrorResponse, "description": "Invalid or missing API token"},
    403: {"model": ApiErrorResponse, "description": "Insufficient scope"},
    404: {"model": ApiErrorResponse, "description": "Resource not found"},
    409: {"model": ApiErrorResponse, "description": "Resource state conflict"},
    422: {"model": ApiErrorResponse, "description": "Request validation failed"},
    503: {"model": ApiErrorResponse, "description": "Scanner temporarily unavailable"},
}


@router.get("/health", include_in_schema=False)
def api_health() -> dict[str, str]:
    return {"status": "ok", "service": "b3s-scanner-api", "api_version": "v1"}


@router.get("/openapi.json", include_in_schema=False)
def scanner_openapi(request: Request) -> JSONResponse:
    spec = copy.deepcopy(request.app.openapi())
    spec["info"] = {
        "title": "B3S Scanner API",
        "version": "1.0.0",
        "description": "Stable asynchronous API for B3S evidence-first brand scans.",
    }
    spec["paths"] = {
        path: value
        for path, value in spec.get("paths", {}).items()
        if path.startswith("/api/v1/") and path not in {"/api/v1/openapi.json", "/api/v1/docs"}
    }
    return JSONResponse(spec)


@router.get("/docs", include_in_schema=False, response_class=HTMLResponse)
def scanner_docs() -> HTMLResponse:
    return get_swagger_ui_html(openapi_url="/api/v1/openapi.json", title="B3S Scanner API v1")


@router.get(
    "/capabilities",
    response_model=ApiCapabilitiesResponse,
    operation_id="getScannerCapabilities",
    responses=_ERRORS,
)
def capabilities(_principal: ReadPrincipal) -> dict[str, Any]:
    return {
        "object": "api_capabilities",
        "api_version": "v1",
        "result_schema_version": "b3s-scanner-result-v1",
        "authentication": "bearer",
        "idempotency": "durable",
        "job_status_durability": "durable",
        "job_execution": "process_bound",
        "restart_behavior": "marks_incomplete_as_failed",
        "supported_languages": ["es"],
    }


@router.post(
    "/scans",
    response_model=ScanStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="createScan",
    responses=_ERRORS,
)
def create_scan(
    payload: ScanCreateRequest,
    response: Response,
    principal: WritePrincipal,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    scan, replayed = create_scan_job(
        payload.model_dump(),
        client_id=principal.client_id,
        idempotency_key=idempotency_key,
    )
    response.headers["Location"] = f"/api/v1/scans/{scan['id']}"
    response.headers["Retry-After"] = "5"
    response.headers["Cache-Control"] = "no-store"
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return status_payload(scan, language=payload.language)


@router.get(
    "/scans/{scan_id}",
    response_model=ScanStatusResponse,
    operation_id="getScan",
    responses=_ERRORS,
)
def read_scan(scan_id: str, response: Response, _principal: ReadPrincipal) -> dict[str, Any]:
    scan = get_scan(scan_id)
    if scan is None:
        raise ApiError(404, "scan_not_found", f"Scan {scan_id} was not found.")
    response.headers["Cache-Control"] = "no-store"
    public = status_payload(scan)
    if public["status"] in {"running", "blocked"}:
        response.headers["Retry-After"] = "5"
    return public


@router.post(
    "/scans/{scan_id}/continue",
    response_model=ScanStatusResponse,
    operation_id="continueScan",
    responses=_ERRORS,
)
def continue_scan(scan_id: str, response: Response, _principal: WritePrincipal) -> dict[str, Any]:
    existing = get_scan(scan_id)
    if existing is None:
        raise ApiError(404, "scan_not_found", f"Scan {scan_id} was not found.")
    if str(existing.get("state") or "") != "blocked":
        raise ApiError(
            409,
            "scan_not_continuable",
            "Only a blocked scan with an approved degraded fallback can continue.",
            details={"state": str(existing.get("state") or "unknown")},
        )
    result = approve_degraded_scan(scan_id)
    if result is None or not result.get("approved"):
        raise ApiError(
            409,
            "scan_not_continuable",
            "The scan cannot continue from its current acquisition state.",
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Retry-After"] = "5"
    return status_payload(get_scan(scan_id) or existing)


@router.post(
    "/scans/{scan_id}/cancel",
    response_model=ScanStatusResponse,
    operation_id="cancelScan",
    responses=_ERRORS,
)
def cancel_scan_route(scan_id: str, response: Response, _principal: WritePrincipal) -> dict[str, Any]:
    existing = get_scan(scan_id)
    if existing is None:
        raise ApiError(404, "scan_not_found", f"Scan {scan_id} was not found.")
    public_state = status_payload(existing)["status"]
    if public_state in {"completed", "failed", "cancelled"}:
        raise ApiError(
            409,
            "scan_not_cancellable",
            "The scan is already in a terminal state.",
            details={"status": public_state},
        )
    result = cancel_scan(scan_id)
    if result is None:
        raise ApiError(404, "scan_not_found", f"Scan {scan_id} was not found.")
    response.headers["Cache-Control"] = "no-store"
    return status_payload(get_scan(scan_id) or {**existing, "state": "cancelled"})


@router.get(
    "/scans/{scan_id}/result",
    response_model=ScanResultResponse,
    operation_id="getScanResult",
    responses=_ERRORS,
)
def read_result(
    scan_id: str,
    request: Request,
    response: Response,
    _principal: ReadPrincipal,
):
    report = get_completed_report(scan_id)
    etag = report_etag(report)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Vary": "Authorization"})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=300, immutable"
    response.headers["Vary"] = "Authorization"
    return result_payload(report)


@router.get(
    "/scans/{scan_id}/evidence",
    response_model=ScanEvidenceResponse,
    operation_id="getScanEvidence",
    responses=_ERRORS,
)
def read_evidence(
    scan_id: str,
    request: Request,
    response: Response,
    _principal: ReadPrincipal,
):
    report = get_completed_report(scan_id)
    etag = report_etag(report).removesuffix('"') + "-evidence\""
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Vary": "Authorization"})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=300, immutable"
    response.headers["Vary"] = "Authorization"
    return evidence_payload(report)


@router.get(
    "/brands/{domain}/scans",
    response_model=BrandScanHistoryResponse,
    operation_id="listBrandScans",
    responses=_ERRORS,
)
def brand_scan_history(
    domain: str,
    _principal: ReadPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    normalized = domain_key(domain)
    if not normalized:
        raise ApiError(400, "invalid_domain", "A valid brand domain is required.")
    reports, history_state = annotate_report_history(list_reports_for_domain(normalized))
    page = reports[offset : offset + limit]
    items = [
        {
            "id": str(item.get("id") or ""),
            "status": "completed",
            "brand_name": str(item.get("brand_name") or normalized),
            "url": str(item.get("url") or ""),
            "score": item.get("score"),
            "created_at": item.get("created_at"),
            "reliability_status": str(item.get("reliability_status") or "unknown"),
            "canonical_status": str(item.get("canonical_status") or "unknown"),
            "stability_classification": str(
                (item.get("stability") or {}).get("classification") or "unknown"
            ),
            "stability_reason_codes": [
                str(code) for code in (item.get("stability") or {}).get("reason_codes") or []
            ],
            "result_url": f"/api/v1/scans/{item.get('id')}/result",
            "report_url": f"/report/{item.get('id')}",
        }
        for item in page
    ]
    return {
        "object": "scan_list",
        "api_version": "v1",
        "domain": normalized,
        "items": items,
        "canonical_report_id": history_state.get("canonical_report_id"),
        "provisional_report_id": history_state.get("provisional_report_id"),
        "selected_report_id": history_state.get("selected_report_id"),
        "pagination": {
            "limit": limit,
            "offset": offset,
            "count": len(items),
            "has_more": offset + len(items) < len(reports),
        },
    }
