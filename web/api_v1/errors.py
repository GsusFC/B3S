"""Stable API errors and request correlation for the v1 surface."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


_API_PREFIX = "/api/v1"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_MAX_VALIDATION_ERRORS = 20
_LOG = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        self.details = details or {}
        self.headers = headers or {}


def request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", "")
    if value:
        return str(value)
    supplied = request.headers.get("x-request-id", "").strip()
    value = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex
    request.state.request_id = value
    return value


def error_response(request: Request, error: ApiError) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {
            "code": error.code,
            "message": error.message,
            "request_id": request_id(request),
        }
    }
    if error.details:
        payload["error"]["details"] = error.details
    headers = {"X-Request-ID": request_id(request), **error.headers}
    return JSONResponse(payload, status_code=error.status_code, headers=headers)


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(request, exc)


async def _validation_error_handler(request: Request, exc: RequestValidationError):
    if not request.url.path.startswith(_API_PREFIX):
        return await request_validation_exception_handler(request, exc)
    fields = []
    errors = exc.errors()
    for item in errors[:_MAX_VALIDATION_ERRORS]:
        location = ".".join(str(part) for part in item.get("loc") or [] if part != "body")
        fields.append(
            {
                "field": location,
                "code": str(item.get("type") or "validation_error"),
                "message": str(item.get("msg") or "Invalid value"),
            }
        )
    return error_response(
        request,
        ApiError(
            422,
            "request_validation_failed",
            "The request does not match the API contract.",
            details={"fields": fields, "total_errors": len(errors)},
        ),
    )


def install_api_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    @app.middleware("http")
    async def api_request_context(request: Request, call_next):
        if not request.url.path.startswith(_API_PREFIX):
            return await call_next(request)
        correlation_id = request_id(request)
        try:
            response = await call_next(request)
        except Exception:
            _LOG.exception(
                "unhandled Scanner API error",
                extra={"request_id": correlation_id, "path": request.url.path},
            )
            response = error_response(
                request,
                ApiError(500, "internal_error", "The Scanner API encountered an unexpected internal error."),
            )
        response.headers.setdefault("X-Request-ID", correlation_id)
        response.headers.setdefault("X-B3S-API-Version", "v1")
        if request.url.path.endswith("/operational-c7"):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
            vary = {
                value.strip()
                for value in response.headers.get("Vary", "").split(",")
                if value.strip()
            }
            vary.add("Authorization")
            response.headers["Vary"] = ", ".join(sorted(vary))
        return response
