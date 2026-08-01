"""Short-lived signed browser sessions for the isolated Vault reviewer UI."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import secrets
import time

from web.api_v1.auth import (
    ApiPrincipal,
    authenticate_evidence_reviewer_token,
)


VAULT_REVIEWER_COOKIE = "b3s_vault_reviewer_session"
VAULT_REVIEWER_SESSION_MAX_AGE = 8 * 60 * 60
_MAX_CLOCK_SKEW_SECONDS = 60
_MAX_COOKIE_LENGTH = 2048


@dataclass(frozen=True, slots=True)
class VaultReviewerSession:
    reviewer_id: str
    csrf_token: str
    issued_at: int


def vault_reviewer_enabled() -> bool:
    return os.environ.get("BRAND3_ENVIRONMENT", "").strip().lower() == "vault"


def authenticate_vault_reviewer(token: str) -> ApiPrincipal:
    """Authenticate the dedicated reviewer credential in Vault only."""

    if not vault_reviewer_enabled():
        raise PermissionError("Vault reviewer sessions are disabled.")
    return authenticate_evidence_reviewer_token(token)


def issue_vault_reviewer_session(
    principal: ApiPrincipal,
    *,
    issued_at: int | None = None,
    csrf_token: str | None = None,
) -> str:
    """Return an authenticated, time-limited cookie value without the token."""

    reviewer_id = str(principal.reviewer_id or "").strip()
    if not reviewer_id or principal.client_id != reviewer_id:
        raise ValueError("A bound evidence reviewer principal is required.")
    secret = _reviewer_secret()
    configured = authenticate_evidence_reviewer_token(secret)
    if configured.reviewer_id != reviewer_id:
        raise ValueError("The reviewer principal no longer matches configuration.")
    payload = {
        "csrf": csrf_token or secrets.token_hex(32),
        "iat": int(time.time()) if issued_at is None else int(issued_at),
        "reviewer_id": reviewer_id,
        "v": 1,
    }
    encoded = _encode_payload(payload)
    signature = hmac.new(
        secret.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def read_vault_reviewer_session(
    cookie_value: str | None,
    *,
    now: int | None = None,
) -> VaultReviewerSession | None:
    """Verify and decode one session, failing closed on any drift."""

    raw = str(cookie_value or "").strip()
    if not vault_reviewer_enabled() or not raw or len(raw) > _MAX_COOKIE_LENGTH or raw.count(".") != 1:
        return None
    try:
        secret = _reviewer_secret()
        principal = authenticate_evidence_reviewer_token(secret)
        encoded, supplied_signature = raw.split(".", 1)
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not secrets.compare_digest(
            supplied_signature,
            expected_signature,
        ):
            return None
        payload = _decode_payload(encoded)
    except (TypeError, ValueError, UnicodeError):
        return None
    except Exception:
        return None

    reviewer_id = payload.get("reviewer_id")
    csrf_token = payload.get("csrf")
    issued_at = payload.get("iat")
    if (
        payload.get("v") != 1
        or not isinstance(reviewer_id, str)
        or reviewer_id != principal.reviewer_id
        or not isinstance(csrf_token, str)
        or len(csrf_token) != 64
        or any(character not in "0123456789abcdef" for character in csrf_token)
        or type(issued_at) is not int
    ):
        return None
    current = int(time.time()) if now is None else int(now)
    age = current - issued_at
    if age < -_MAX_CLOCK_SKEW_SECONDS or age > VAULT_REVIEWER_SESSION_MAX_AGE:
        return None
    return VaultReviewerSession(
        reviewer_id=reviewer_id,
        csrf_token=csrf_token,
        issued_at=issued_at,
    )


def safe_vault_review_path(value: str | None) -> str:
    """Allow only local reviewer destinations after authentication."""

    path = str(value or "").strip()
    if (
        path.startswith("/vault/review/")
        and not path.startswith("//")
        and "\\" not in path
        and not any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        return path
    return "/"


def csrf_matches(session: VaultReviewerSession, supplied: str) -> bool:
    candidate = str(supplied or "").strip()
    return bool(candidate) and secrets.compare_digest(
        candidate,
        session.csrf_token,
    )


def _reviewer_secret() -> str:
    secret = os.environ.get(
        "B3S_EVIDENCE_ADJUDICATION_TOKEN",
        "",
    ).strip()
    if not secret:
        raise ValueError("Evidence reviewer authentication is not configured.")
    return secret


def _encode_payload(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")


def _decode_payload(encoded: str) -> dict[str, object]:
    padding = "=" * (-len(encoded) % 4)
    decoded = base64.b64decode(
        encoded + padding,
        altchars=b"-_",
        validate=True,
    )
    payload = json.loads(decoded.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Reviewer session payload must be an object.")
    return payload
