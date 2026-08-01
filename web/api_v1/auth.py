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
ADJUDICATE_EVIDENCE = "evidence:adjudicate"
SCANNER_SCOPES = frozenset({READ_SCANS, WRITE_SCANS})
EVIDENCE_REVIEWER_SCOPES = frozenset(
    {READ_SCANS, ADJUDICATE_EVIDENCE}
)
_REVIEWER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$")

_bearer = HTTPBearer(auto_error=False, scheme_name="B3SScannerBearer")


@dataclass(frozen=True)
class ApiPrincipal:
    client_id: str
    scopes: frozenset[str]
    reviewer_id: str | None = None


def _configured_scanner_token() -> str:
    return (
        os.environ.get("B3S_SCANNER_API_TOKEN", "").strip()
        or os.environ.get("BRAND3_SCANNER_API_TOKEN", "").strip()
    )


def _configured_evidence_reviewer() -> tuple[str, str]:
    return (
        os.environ.get("B3S_EVIDENCE_ADJUDICATION_TOKEN", "").strip(),
        os.environ.get("B3S_EVIDENCE_REVIEWER_ID", "").strip(),
    )


def authenticate_evidence_reviewer_token(
    supplied_token: str,
) -> ApiPrincipal:
    """Authenticate the dedicated evidence reviewer outside HTTP Bearer flows."""

    scanner_token = _configured_scanner_token()
    reviewer_token, reviewer_id = _configured_evidence_reviewer()
    if not reviewer_token:
        raise ApiError(
            503,
            "evidence_reviewer_not_configured",
            "B3S evidence reviewer authentication is not configured.",
        )
    if scanner_token and secrets.compare_digest(
        scanner_token,
        reviewer_token,
    ):
        raise ApiError(
            503,
            "api_token_configuration_conflict",
            "Scanner and evidence adjudication credentials must be different.",
        )
    supplied = str(supplied_token or "").strip()
    if not supplied or not secrets.compare_digest(
        supplied,
        reviewer_token,
    ):
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
    reviewer_token, reviewer_id = _configured_evidence_reviewer()
    if not scanner_token and not reviewer_token:
        raise ApiError(
            503,
            "api_auth_not_configured",
            "B3S Scanner API authentication is not configured.",
        )
    if (
        scanner_token
        and reviewer_token
        and secrets.compare_digest(scanner_token, reviewer_token)
    ):
        raise ApiError(
            503,
            "api_token_configuration_conflict",
            "Scanner and evidence adjudication credentials must be different.",
        )
    supplied = (
        credentials.credentials.strip()
        if credentials and credentials.scheme.lower() == "bearer"
        else ""
    )
    if scanner_token and supplied and secrets.compare_digest(
        supplied,
        scanner_token,
    ):
        return ApiPrincipal(
            client_id="environment-token",
            scopes=SCANNER_SCOPES,
        )
    if reviewer_token and supplied and secrets.compare_digest(
        supplied,
        reviewer_token,
    ):
        return authenticate_evidence_reviewer_token(supplied)
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


ReadPrincipal = Annotated[ApiPrincipal, Depends(require_scope(READ_SCANS))]
WritePrincipal = Annotated[ApiPrincipal, Depends(require_scope(WRITE_SCANS))]
AdjudicationPrincipal = Annotated[
    ApiPrincipal,
    Depends(require_scope(ADJUDICATE_EVIDENCE)),
]
