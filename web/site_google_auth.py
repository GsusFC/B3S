"""Google OpenID Connect access boundary for the protected browser surface."""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
from hashlib import sha256
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.responses import PlainTextResponse, RedirectResponse, Response

_GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
_GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
_GOOGLE_FLOW_COOKIE = "__Host-b3s_google_oidc_flow"
_GOOGLE_SESSION_COOKIE = "__Host-b3s_google_oidc_session"
_GOOGLE_FLOW_MAX_AGE = 10 * 60
_GOOGLE_SESSION_MAX_AGE = 8 * 60 * 60
_GOOGLE_FLOW_SALT = "b3s-google-oidc-flow-v1"
_GOOGLE_SESSION_SALT = "b3s-google-oidc-session-v1"
_EXACT_ALLOWED_EMAILS = frozenset({
    "jesus@wearefloc.com",
    "sergio@wearefloc.com",
    "javi@wearefloc.com",
    "victor@wearefloc.com",
})
_EMAIL_RE = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,512}\.apps\.googleusercontent\.com")


@dataclass(frozen=True)
class GoogleOidcConfig:
    client_id: str
    client_secret: str
    session_secret: str
    allowed_emails: frozenset[str]
    base_url: str


@dataclass(frozen=True)
class GoogleSiteSession:
    email: str
    subject: str
    csrf_token: str


def google_oidc_enabled(environment: Mapping[str, str]) -> bool | None:
    raw = str(environment.get("B3S_GOOGLE_OIDC_ENABLED") or "").strip().casefold()
    legacy = str(environment.get("B3S_SITE_BASIC_AUTH_ENABLED") or "").strip().casefold()
    if legacy not in {"", "false"}:
        return None
    if raw in {"", "false"}:
        return False
    if raw == "true":
        return True
    return None


def _effective_isolated_tokens(
    environment: Mapping[str, str],
) -> tuple[str, str] | None:
    primary_scanner = str(environment.get("B3S_SCANNER_API_TOKEN") or "")
    legacy_scanner = str(environment.get("BRAND3_SCANNER_API_TOKEN") or "")
    reviewer = str(environment.get("B3S_EVIDENCE_ADJUDICATION_TOKEN") or "")
    values = (primary_scanner, legacy_scanner, reviewer)
    if any(value != value.strip() for value in values):
        return None
    if primary_scanner and legacy_scanner and primary_scanner != legacy_scanner:
        return None
    scanner = primary_scanner or legacy_scanner
    if not scanner or not reviewer or secrets.compare_digest(scanner, reviewer):
        return None
    return scanner, reviewer


def google_oidc_config(environment: Mapping[str, str]) -> GoogleOidcConfig | None:
    """Return a strict configuration, or ``None`` when any field is invalid."""

    client_id = str(environment.get("B3S_GOOGLE_OIDC_CLIENT_ID") or "").strip()
    client_secret = str(environment.get("B3S_GOOGLE_OIDC_CLIENT_SECRET") or "")
    session_secret = str(environment.get("B3S_GOOGLE_OIDC_SESSION_SECRET") or "")
    base_url = str(environment.get("BRAND3_BASE_URL") or "").strip()
    if str(environment.get("B3S_GOOGLE_OIDC_ALLOWED_EMAILS") or "").strip():
        return None
    allowed_emails = _EXACT_ALLOWED_EMAILS
    isolated_tokens = _effective_isolated_tokens(environment)

    valid_secret = (
        lambda value, minimum: minimum <= len(value) <= 512
        and value == value.strip()
        and all(33 <= ord(char) <= 126 for char in value)
    )
    try:
        parsed_base = urlsplit(base_url)
    except ValueError:
        parsed_base = None
    valid_base_url = bool(
        parsed_base
        and parsed_base.scheme == "https"
        and parsed_base.hostname
        and parsed_base.username is None
        and parsed_base.password is None
        and parsed_base.path in {"", "/"}
        and not parsed_base.query
        and not parsed_base.fragment
        and not base_url.endswith("/")
    )
    if (
        google_oidc_enabled(environment) is not True
        or _CLIENT_ID_RE.fullmatch(client_id) is None
        or not valid_secret(client_secret, 20)
        or not valid_secret(session_secret, 43)
        or not valid_base_url
        or allowed_emails != _EXACT_ALLOWED_EMAILS
        or isolated_tokens is None
        or client_secret == session_secret
        or client_secret in isolated_tokens
        or session_secret in isolated_tokens
        or not allowed_emails
        or any(_EMAIL_RE.fullmatch(email) is None for email in allowed_emails)
    ):
        return None
    return GoogleOidcConfig(
        client_id=client_id,
        client_secret=client_secret,
        session_secret=session_secret,
        allowed_emails=allowed_emails,
        base_url=base_url,
    )


def safe_site_destination(value: str, *, default: str = "/") -> str:
    """Accept only bounded local paths outside the authentication namespace."""

    candidate = str(value or "").strip()
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or candidate.startswith("/auth/")
        or "\\" in candidate
        or any(ord(char) < 32 or ord(char) == 127 for char in candidate)
        or len(candidate) > 2048
    ):
        return default
    return candidate


def google_login_path(destination: str) -> str:
    return "/auth/google/login?" + urlencode(
        {"next_path": safe_site_destination(destination)}
    )


def _serializer(secret: str, *, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret,
        salt=salt,
        signer_kwargs={"digest_method": sha256},
    )


def issue_google_site_session(
    config: GoogleOidcConfig,
    *,
    email: str,
    subject: str,
) -> str:
    normalized_email = str(email or "").strip().casefold()
    normalized_subject = str(subject or "").strip()
    if (
        normalized_email not in config.allowed_emails
        or not normalized_subject
        or len(normalized_subject) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized_subject)
    ):
        raise ValueError("unauthorized Google identity")
    return _serializer(
        config.session_secret,
        salt=_GOOGLE_SESSION_SALT,
    ).dumps({
        "v": 1,
        "email": normalized_email,
        "sub": normalized_subject,
        "csrf": secrets.token_urlsafe(32),
    })


def read_google_site_session(
    config: GoogleOidcConfig,
    raw_cookie: str | None,
) -> GoogleSiteSession | None:
    if not raw_cookie or len(raw_cookie) > 4096:
        return None
    try:
        payload = _serializer(
            config.session_secret,
            salt=_GOOGLE_SESSION_SALT,
        ).loads(raw_cookie, max_age=_GOOGLE_SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"v", "email", "sub", "csrf"}:
        return None
    if payload.get("v") != 1:
        return None
    email = str(payload.get("email") or "").strip().casefold()
    subject = str(payload.get("sub") or "").strip()
    csrf_token = str(payload.get("csrf") or "").strip()
    if (
        email not in config.allowed_emails
        or not subject
        or len(subject) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in subject)
        or len(csrf_token) < 32
        or len(csrf_token) > 256
    ):
        return None
    return GoogleSiteSession(email=email, subject=subject, csrf_token=csrf_token)


def _google_client(config: GoogleOidcConfig):
    oauth = OAuth()
    return oauth.register(
        "google",
        client_id=config.client_id,
        client_secret=config.client_secret,
        server_metadata_url=_GOOGLE_METADATA_URL,
        client_kwargs={
            "scope": "openid email",
            "code_challenge_method": "S256",
        },
    )


def _flow_cookie(config: GoogleOidcConfig, payload: dict[str, str]) -> str:
    return _serializer(config.session_secret, salt=_GOOGLE_FLOW_SALT).dumps(payload)


def _read_flow_cookie(
    config: GoogleOidcConfig,
    raw_cookie: str | None,
) -> dict[str, str] | None:
    if not raw_cookie or len(raw_cookie) > 8192:
        return None
    try:
        payload = _serializer(
            config.session_secret,
            salt=_GOOGLE_FLOW_SALT,
        ).loads(raw_cookie, max_age=_GOOGLE_FLOW_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    required = {"state", "nonce", "code_verifier", "next_path"}
    if not isinstance(payload, dict) or set(payload) != required:
        return None
    if not all(isinstance(payload[key], str) and payload[key] for key in required):
        return None
    if safe_site_destination(payload["next_path"]) != payload["next_path"]:
        return None
    return payload


def _set_flow_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        _GOOGLE_FLOW_COOKIE,
        value,
        max_age=_GOOGLE_FLOW_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def _clear_flow_cookie(response: Response) -> None:
    response.delete_cookie(
        _GOOGLE_FLOW_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def set_google_session_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        _GOOGLE_SESSION_COOKIE,
        value,
        max_age=_GOOGLE_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def clear_google_session_cookie(response: Response) -> None:
    response.delete_cookie(
        _GOOGLE_SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def google_session_cookie_name() -> str:
    return _GOOGLE_SESSION_COOKIE


def google_oidc_allowed_emails() -> frozenset[str]:
    return _EXACT_ALLOWED_EMAILS


async def begin_google_login(
    config: GoogleOidcConfig,
    *,
    next_path: str,
) -> Response:
    destination = safe_site_destination(next_path)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    redirect_uri = f"{config.base_url}/auth/google/callback"
    try:
        authorization = await _google_client(config).create_authorization_url(
            redirect_uri,
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            hd="wearefloc.com",
            prompt="select_account",
        )
        authorization_url = str(authorization["url"])
    except Exception:
        return PlainTextResponse(
            "Authentication service unavailable\n",
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    response = RedirectResponse(authorization_url, status_code=302)
    _set_flow_cookie(
        response,
        _flow_cookie(
            config,
            {
                "state": state,
                "nonce": nonce,
                "code_verifier": code_verifier,
                "next_path": destination,
            },
        ),
    )
    return response


async def complete_google_login(
    config: GoogleOidcConfig,
    *,
    query_params: Any,
    flow_cookie: str | None,
) -> Response:
    flow = _read_flow_cookie(config, flow_cookie)
    codes = query_params.getlist("code")
    states = query_params.getlist("state")
    errors = query_params.getlist("error")
    if (
        flow is None
        or errors
        or len(codes) != 1
        or len(states) != 1
        or not codes[0]
        or not secrets.compare_digest(states[0], flow["state"])
    ):
        response = PlainTextResponse(
            "Authentication failed\n",
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
        _clear_flow_cookie(response)
        return response

    redirect_uri = f"{config.base_url}/auth/google/callback"
    try:
        client = _google_client(config)
        token = await client.fetch_access_token(
            code=codes[0],
            redirect_uri=redirect_uri,
            code_verifier=flow["code_verifier"],
        )
        userinfo = await client.parse_id_token(
            token,
            nonce=flow["nonce"],
            claims_options={
                "iss": {"values": sorted(_GOOGLE_ISSUERS)},
                "email": {"essential": True},
                "email_verified": {"essential": True},
                "hd": {"essential": True, "value": "wearefloc.com"},
                "sub": {"essential": True},
            },
        )
        email = str(userinfo.get("email") or "").strip().casefold()
        subject = str(userinfo.get("sub") or "").strip()
        verified = userinfo.get("email_verified") is True
        issuer = str(userinfo.get("iss") or "")
        hosted_domain = str(userinfo.get("hd") or "").strip().casefold()
        if (
            not verified
            or hosted_domain != "wearefloc.com"
            or issuer not in _GOOGLE_ISSUERS
            or email not in config.allowed_emails
            or not subject
            or len(subject) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in subject)
        ):
            raise PermissionError("unauthorized Google identity")
        session_cookie = issue_google_site_session(
            config,
            email=email,
            subject=subject,
        )
    except (OAuthError, PermissionError, BadSignature, ValueError, KeyError, TypeError):
        response = PlainTextResponse(
            "Access denied\n",
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
        _clear_flow_cookie(response)
        return response
    except Exception:
        response = PlainTextResponse(
            "Authentication service unavailable\n",
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
        _clear_flow_cookie(response)
        return response

    response = RedirectResponse(flow["next_path"], status_code=303)
    _clear_flow_cookie(response)
    set_google_session_cookie(response, session_cookie)
    return response
