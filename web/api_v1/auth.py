"""Bearer authentication and scope checks for B3S Scanner API clients."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import ApiError


READ_SCANS = "scans:read"
WRITE_SCANS = "scans:write"
ALL_SCOPES = frozenset({READ_SCANS, WRITE_SCANS})

_bearer = HTTPBearer(auto_error=False, scheme_name="B3SScannerBearer")


@dataclass(frozen=True)
class ApiPrincipal:
    client_id: str
    scopes: frozenset[str]


def _configured_token() -> str:
    return (
        os.environ.get("B3S_SCANNER_API_TOKEN", "").strip()
        or os.environ.get("BRAND3_SCANNER_API_TOKEN", "").strip()
    )


async def authenticate(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ApiPrincipal:
    configured = _configured_token()
    if not configured:
        raise ApiError(
            503,
            "api_auth_not_configured",
            "B3S Scanner API authentication is not configured.",
        )
    supplied = credentials.credentials.strip() if credentials and credentials.scheme.lower() == "bearer" else ""
    if not supplied or not secrets.compare_digest(supplied, configured):
        raise ApiError(
            401,
            "invalid_api_token",
            "A valid B3S Scanner API Bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return ApiPrincipal(client_id="environment-token", scopes=ALL_SCOPES)


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


ReadPrincipal = Annotated[ApiPrincipal, Depends(require_scope(READ_SCANS))]
WritePrincipal = Annotated[ApiPrincipal, Depends(require_scope(WRITE_SCANS))]
