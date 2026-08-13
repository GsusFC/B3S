"""Bearer authentication and scope checks for B3S Scanner API clients."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import ApiError


READ_SCANS = "scans:read"
WRITE_SCANS = "scans:write"
READ_SCAN_OUTPUTS = "scan_outputs:read"
CREATE_SCANS = "scans:create"
ADJUDICATE_EVIDENCE = "evidence:adjudicate"
SCANNER_SCOPES = frozenset({READ_SCANS, WRITE_SCANS})
ECLIPSE_SCAN_SCOPES = frozenset({READ_SCAN_OUTPUTS, CREATE_SCANS})
EVIDENCE_REVIEWER_SCOPES = frozenset({READ_SCANS, ADJUDICATE_EVIDENCE})
ECLIPSE_SCAN_CLIENT_ID = "eclipse-scan"
ECLIPSE_SCAN_DAILY_LIMIT = 40
_REVIEWER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$")
_TOKEN_RE = re.compile(r"^[\x21-\x7E]{1,256}$")
_ECLIPSE_TOKEN_RE = re.compile(r"^[\x21-\x7E]{32,256}$")

_bearer = HTTPBearer(auto_error=False, scheme_name="B3SScannerBearer")


@dataclass(frozen=True)
class ApiPrincipal:
    client_id: str
    scopes: frozenset[str]
    reviewer_id: str | None = None
    daily_scan_limit: int | None = None
    require_idempotency: bool = False
    fail_blocked_scan: bool = False


def _configured_scanner_token() -> str:
    return (
        os.environ.get("B3S_SCANNER_API_TOKEN", "").strip()
        or os.environ.get("BRAND3_SCANNER_API_TOKEN", "").strip()
    )


def _configured_eclipse_scan_token() -> str:
    return os.environ.get("B3S_ECLIPSE_SCAN_API_TOKEN", "").strip()


def _configured_evidence_reviewer() -> tuple[str, str]:
    return (
        os.environ.get("B3S_EVIDENCE_ADJUDICATION_TOKEN", "").strip(),
        os.environ.get("B3S_EVIDENCE_REVIEWER_ID", "").strip(),
    )


def _ensure_distinct_tokens(*tokens: str) -> None:
    configured = [token for token in tokens if token]
    if any(not _TOKEN_RE.fullmatch(token) for token in configured):
        raise ApiError(
            503,
            "api_token_configuration_invalid",
            "Scanner API credentials must be 1-256 visible ASCII characters.",
        )
    for index, token in enumerate(configured):
        if any(secrets.compare_digest(token, other) for other in configured[index + 1 :]):
            raise ApiError(
                503,
                "api_token_configuration_conflict",
                "Scanner API credentials must be different.",
            )


def _validate_eclipse_token(token: str) -> None:
    if token and not _ECLIPSE_TOKEN_RE.fullmatch(token):
        raise ApiError(
            503,
            "api_token_configuration_invalid",
            "The Eclipse Scan credential must be 32-256 visible ASCII characters.",
        )


def _token_matches(supplied: str, configured: str) -> bool:
    """Compare only validated ASCII token material; never let Unicode hit compare_digest."""
    return bool(_TOKEN_RE.fullmatch(supplied)) and secrets.compare_digest(supplied, configured)


def authenticate_evidence_reviewer_token(
    supplied_token: str,
) -> ApiPrincipal:
    """Authenticate the dedicated evidence reviewer outside HTTP Bearer flows."""

    scanner_token = _configured_scanner_token()
    eclipse_token = _configured_eclipse_scan_token()
    _validate_eclipse_token(eclipse_token)
    reviewer_token, reviewer_id = _configured_evidence_reviewer()
    _ensure_distinct_tokens(scanner_token, eclipse_token, reviewer_token)
    if not reviewer_token:
        raise ApiError(
            503,
            "evidence_reviewer_not_configured",
            "B3S evidence reviewer authentication is not configured.",
        )
    supplied = str(supplied_token or "").strip()
    if not supplied or not _token_matches(supplied, reviewer_token):
        raise ApiError(
            401,
            "invalid_api_token",
            "A valid evidence reviewer credential is required.",
        )
    if not _REVIEWER_ID_RE.fullmatch(reviewer_id):
        raise ApiError(
            503,
            "evidence_reviewer_not_configured",
            "B3S evidence reviewer identity is not configured correctly.",
        )
    return ApiPrincipal(
        client_id=reviewer_id,
        reviewer_id=reviewer_id,
        scopes=EVIDENCE_REVIEWER_SCOPES,
    )


async def authenticate(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ApiPrincipal:
    scanner_token = _configured_scanner_token()
    eclipse_token = _configured_eclipse_scan_token()
    _validate_eclipse_token(eclipse_token)
    reviewer_token, reviewer_id = _configured_evidence_reviewer()
    if not scanner_token and not eclipse_token and not reviewer_token:
        raise ApiError(
            503,
            "api_auth_not_configured",
            "B3S Scanner API authentication is not configured.",
        )
    _ensure_distinct_tokens(scanner_token, eclipse_token, reviewer_token)
    supplied = (
        credentials.credentials.strip()
        if credentials and credentials.scheme.lower() == "bearer"
        else ""
    )
    if scanner_token and supplied and _token_matches(supplied, scanner_token):
        return ApiPrincipal(client_id="environment-token", scopes=SCANNER_SCOPES)
    if reviewer_token and supplied and _token_matches(supplied, reviewer_token):
        return authenticate_evidence_reviewer_token(supplied)
    if eclipse_token and supplied and _token_matches(supplied, eclipse_token):
        return ApiPrincipal(
            client_id=ECLIPSE_SCAN_CLIENT_ID,
            scopes=ECLIPSE_SCAN_SCOPES,
            daily_scan_limit=ECLIPSE_SCAN_DAILY_LIMIT,
            require_idempotency=True,
            fail_blocked_scan=True,
        )
    if not supplied:
        raise ApiError(
            401,
            "invalid_api_token",
            "A valid B3S Scanner API Bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise ApiError(
        401,
        "invalid_api_token",
        "A valid B3S Scanner API Bearer token is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_scope(scope: str):
    async def dependency(principal: Annotated[ApiPrincipal, Depends(authenticate)]) -> ApiPrincipal:
        if scope not in principal.scopes:
            raise ApiError(
                403,
                "insufficient_scope",
                "The API credential does not grant the required scope.",
                details={"required_scope": scope},
            )
        return principal

    return dependency


def require_any_scope(*scopes: str):
    required = frozenset(scopes)

    async def dependency(principal: Annotated[ApiPrincipal, Depends(authenticate)]) -> ApiPrincipal:
        if principal.scopes.isdisjoint(required):
            raise ApiError(
                403,
                "insufficient_scope",
                "The API credential does not grant the required scope.",
                details={"required_any_scope": sorted(required)},
            )
        return principal

    return dependency


ReadPrincipal = Annotated[ApiPrincipal, Depends(require_scope(READ_SCANS))]
WritePrincipal = Annotated[ApiPrincipal, Depends(require_scope(WRITE_SCANS))]
ScanOutputPrincipal = Annotated[
    ApiPrincipal,
    Depends(require_any_scope(READ_SCANS, READ_SCAN_OUTPUTS)),
]
ScanCreatePrincipal = Annotated[
    ApiPrincipal,
    Depends(require_any_scope(WRITE_SCANS, CREATE_SCANS)),
]
AdjudicationPrincipal = Annotated[
    ApiPrincipal,
    Depends(require_scope(ADJUDICATE_EVIDENCE)),
]
