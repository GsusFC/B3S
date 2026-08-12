from __future__ import annotations

from dataclasses import replace
import re

import pytest
from fastapi.testclient import TestClient

from web.app import app
from web.site_google_auth import (
    GoogleOidcConfig,
    GoogleSiteSession,
    google_oidc_config,
    issue_google_site_session,
    google_oidc_allowed_emails,
)


BASE_URL = "https://b3s-pr71-vault.example"
CLIENT_ID = "1234567890-example.apps.googleusercontent.com"
CLIENT_SECRET = "google-client-secret-0123456789"
SESSION_SECRET = "site-session-secret-" + ("x" * 48)
ALLOWED = (
    "jesus@wearefloc.com,sergio@wearefloc.com,"
    "javi@wearefloc.com,victor@wearefloc.com"
)


def test_google_allowlist_is_exactly_the_four_authorized_users() -> None:
    assert google_oidc_allowed_emails() == frozenset({
        "jesus@wearefloc.com",
        "sergio@wearefloc.com",
        "javi@wearefloc.com",
        "victor@wearefloc.com",
    })


def _clear_site_access(monkeypatch) -> None:
    for name in (
        "B3S_GOOGLE_OIDC_ENABLED",
        "B3S_GOOGLE_OIDC_CLIENT_ID",
        "B3S_GOOGLE_OIDC_CLIENT_SECRET",
        "B3S_GOOGLE_OIDC_SESSION_SECRET",
        "B3S_GOOGLE_OIDC_ALLOWED_EMAILS",
        "BRAND3_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _enable_site_access(monkeypatch) -> GoogleOidcConfig:
    monkeypatch.delenv("BRAND3_ENVIRONMENT", raising=False)
    monkeypatch.setenv("B3S_GOOGLE_OIDC_ENABLED", "true")
    monkeypatch.setenv("B3S_GOOGLE_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("B3S_GOOGLE_OIDC_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("B3S_GOOGLE_OIDC_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", "scanner-token-" + ("s" * 32))
    monkeypatch.setenv("B3S_EVIDENCE_ADJUDICATION_TOKEN", "reviewer-token-" + ("r" * 32))
    monkeypatch.setenv("BRAND3_BASE_URL", BASE_URL)
    config = google_oidc_config(__import__("os").environ)
    assert config is not None
    return config


def _session_cookie(config: GoogleOidcConfig, email: str = "jesus@wearefloc.com") -> dict[str, str]:
    return {
        "__Host-b3s_google_oidc_session": issue_google_site_session(
            config,
            email=email,
            subject="google-subject-123",
        )
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/scan/missing-scan"),
        ("GET", "/api/scan/missing-scan"),
        ("GET", "/dev/scan-preview"),
        ("GET", "/health/apis?run=true"),
        ("GET", "/api/health/apis?run=true"),
        ("GET", "/api/scoring/lab"),
        ("GET", "/report/missing-report"),
        ("GET", "/brand/example.com"),
        ("GET", "/artifacts/screenshots/missing.png"),
        ("GET", "/static/missing.css"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ],
)
def test_google_gate_redirects_entire_non_v1_browser_surface(
    monkeypatch,
    method: str,
    path: str,
) -> None:
    _enable_site_access(monkeypatch)

    response = TestClient(app, base_url=BASE_URL).request(method, path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/google/login?next_path=")
    assert response.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in response.headers


def test_google_access_is_opt_in_outside_pr71(monkeypatch) -> None:
    _clear_site_access(monkeypatch)
    client = TestClient(app, base_url=BASE_URL)

    protected = client.get("/dev/scan-preview", follow_redirects=False)
    health = client.get("/health")

    assert protected.status_code == 200
    assert health.status_code == 200


def test_enabled_missing_google_configuration_fails_closed(monkeypatch) -> None:
    _clear_site_access(monkeypatch)
    monkeypatch.setenv("B3S_GOOGLE_OIDC_ENABLED", "true")
    response = TestClient(app, base_url=BASE_URL).get("/", follow_redirects=False)
    assert response.status_code == 503
    assert response.text == "Service unavailable\n"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("B3S_GOOGLE_OIDC_CLIENT_ID", "not-a-google-client"),
        ("B3S_GOOGLE_OIDC_CLIENT_SECRET", "short"),
        ("B3S_GOOGLE_OIDC_SESSION_SECRET", "short"),
        ("B3S_GOOGLE_OIDC_ALLOWED_EMAILS", "jesus@wearefloc.com,jesus@wearefloc.com"),
        ("BRAND3_BASE_URL", "http://b3s-pr71-vault.example"),
    ],
)
def test_each_invalid_google_configuration_field_fails_closed(monkeypatch, name, value) -> None:
    _enable_site_access(monkeypatch)
    monkeypatch.setenv(name, value)

    response = TestClient(app, base_url=BASE_URL).get("/", follow_redirects=False)

    assert response.status_code == 503


def test_valid_signed_allowed_session_proceeds_to_existing_routes(monkeypatch) -> None:
    config = _enable_site_access(monkeypatch)
    client = TestClient(app, base_url=BASE_URL)
    client.cookies.update(_session_cookie(config))

    response = client.get("/dev/scan-preview")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"


def test_tampered_or_removed_allowlist_session_is_rejected(monkeypatch) -> None:
    config = _enable_site_access(monkeypatch)
    client = TestClient(app, base_url=BASE_URL)
    valid = _session_cookie(config)["__Host-b3s_google_oidc_session"]
    client.cookies.set("__Host-b3s_google_oidc_session", valid + "tampered")
    assert client.get("/", follow_redirects=False).status_code == 303

    restricted = replace(config, allowed_emails=frozenset({"sergio@wearefloc.com"}))
    cookie = issue_google_site_session(
        config,
        email="jesus@wearefloc.com",
        subject="google-subject-123",
    )
    assert __import__("web.site_google_auth", fromlist=["read_google_site_session"]).read_google_site_session(
        restricted, cookie
    ) is None


def test_google_login_uses_oidc_state_nonce_pkce_and_secure_flow_cookie(monkeypatch) -> None:
    _enable_site_access(monkeypatch)

    class FakeGoogle:
        async def create_authorization_url(self, redirect_uri, **kwargs):
            assert redirect_uri == f"{BASE_URL}/auth/google/callback"
            assert len(kwargs["state"]) >= 32
            assert len(kwargs["nonce"]) >= 32
            assert len(kwargs["code_verifier"]) >= 43
            assert kwargs["hd"] == "wearefloc.com"
            return {"url": "https://accounts.google.com/o/oauth2/v2/auth?state=x"}

    monkeypatch.setattr("web.site_google_auth._google_client", lambda _config: FakeGoogle())
    response = TestClient(app, base_url=BASE_URL).get(
        "/auth/google/login?next_path=/brand/example.com",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://accounts.google.com/")
    cookie = response.headers["set-cookie"].lower()
    assert "b3s_google_oidc_flow=" in cookie
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "path=/" in cookie


def test_google_callback_accepts_exact_allowed_verified_identity(monkeypatch) -> None:
    config = _enable_site_access(monkeypatch)
    captured: dict = {}

    class FakeGoogle:
        async def fetch_access_token(self, **kwargs):
            captured.update(kwargs)
            return {"id_token": "signed-google-token", "access_token": "access"}

        async def parse_id_token(self, token, **kwargs):
            assert token["id_token"] == "signed-google-token"
            assert kwargs["nonce"]
            return {
                "iss": "https://accounts.google.com",
                "sub": "google-subject-123",
                "email": "Jesus@wearefloc.com",
                "email_verified": True,
                "hd": "wearefloc.com",
            }

    monkeypatch.setattr("web.site_google_auth._google_client", lambda _config: FakeGoogle())
    from web.site_google_auth import _flow_cookie

    flow = _flow_cookie(
        config,
        {
            "state": "exact-state",
            "nonce": "exact-nonce",
            "code_verifier": "v" * 64,
            "next_path": "/brand/example.com",
        },
    )
    client = TestClient(app, base_url=BASE_URL)
    client.cookies.set("__Host-b3s_google_oidc_flow", flow)
    response = client.get(
        "/auth/google/callback?code=exact-code&state=exact-state",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/brand/example.com"
    assert captured["code_verifier"] == "v" * 64
    cookie = response.headers.get_list("set-cookie")
    assert any("b3s_google_oidc_session=" in value and "HttpOnly" in value and "Secure" in value for value in cookie)
    assert any("__Host-b3s_google_oidc_flow=" in value and "Max-Age=0" in value for value in cookie)


@pytest.mark.parametrize(
    "userinfo",
    [
        {"iss": "https://accounts.google.com", "sub": "sub", "email": "intruder@example.com", "email_verified": True, "hd": "wearefloc.com"},
        {"iss": "https://accounts.google.com", "sub": "sub", "email": "jesus@wearefloc.com", "email_verified": False, "hd": "wearefloc.com"},
        {"iss": "https://evil.example", "sub": "sub", "email": "jesus@wearefloc.com", "email_verified": True, "hd": "wearefloc.com"},
        {"iss": "https://accounts.google.com", "sub": "", "email": "jesus@wearefloc.com", "email_verified": True, "hd": "wearefloc.com"},
    ],
)
def test_google_callback_denies_unapproved_claims(monkeypatch, userinfo) -> None:
    config = _enable_site_access(monkeypatch)

    class FakeGoogle:
        async def fetch_access_token(self, **_kwargs):
            return {"id_token": "signed-google-token", "access_token": "access"}

        async def parse_id_token(self, _token, **_kwargs):
            return userinfo

    monkeypatch.setattr("web.site_google_auth._google_client", lambda _config: FakeGoogle())
    from web.site_google_auth import _flow_cookie
    flow = _flow_cookie(config, {"state": "state", "nonce": "nonce", "code_verifier": "v" * 64, "next_path": "/"})
    client = TestClient(app, base_url=BASE_URL)
    client.cookies.set("__Host-b3s_google_oidc_flow", flow)

    response = client.get("/auth/google/callback?code=code&state=state", follow_redirects=False)

    assert response.status_code == 403
    assert "b3s_google_oidc_session=" not in response.headers.get("set-cookie", "")


def test_google_callback_rejects_state_mismatch_and_duplicate_parameters(monkeypatch) -> None:
    config = _enable_site_access(monkeypatch)
    from web.site_google_auth import _flow_cookie
    flow = _flow_cookie(config, {"state": "state", "nonce": "nonce", "code_verifier": "v" * 64, "next_path": "/"})
    client = TestClient(app, base_url=BASE_URL)
    client.cookies.set("__Host-b3s_google_oidc_flow", flow)

    mismatch = client.get("/auth/google/callback?code=code&state=wrong", follow_redirects=False)
    assert mismatch.status_code == 403
    client.cookies.set("__Host-b3s_google_oidc_flow", flow)
    duplicate = client.get("/auth/google/callback?code=one&code=two&state=state", follow_redirects=False)
    assert duplicate.status_code == 403


def test_open_redirects_are_normalized_to_root(monkeypatch) -> None:
    _enable_site_access(monkeypatch)

    class FakeGoogle:
        async def create_authorization_url(self, _redirect_uri, **_kwargs):
            return {"url": "https://accounts.google.com/o/oauth2/v2/auth"}

    monkeypatch.setattr("web.site_google_auth._google_client", lambda _config: FakeGoogle())
    response = TestClient(app, base_url=BASE_URL).get(
        "/auth/google/login?next_path=https://attacker.example",
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_vault_reviewer_surface_uses_dedicated_session_without_google(monkeypatch) -> None:
    _enable_site_access(monkeypatch)
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    reviewer_token = "reviewer-token-" + ("x" * 32)
    monkeypatch.setenv("B3S_EVIDENCE_ADJUDICATION_TOKEN", reviewer_token)
    monkeypatch.setenv("B3S_EVIDENCE_REVIEWER_ID", "test-reviewer")
    client = TestClient(app, base_url=BASE_URL)

    login_page = client.get("/vault/review/login")
    assert login_page.status_code == 200
    assert "/auth/google/login" not in login_page.headers.get("location", "")

    blocked_post = client.post("/vault/review/login", data={"token": reviewer_token})
    assert blocked_post.status_code == 403

    login = client.post(
        "/vault/review/login",
        data={"token": reviewer_token, "next_path": "/vault/review/example.com"},
        headers={"Origin": BASE_URL},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert "b3s_vault_reviewer_session=" in login.headers["set-cookie"]


def test_authenticated_unsafe_legacy_requests_require_exact_same_origin(monkeypatch) -> None:
    config = _enable_site_access(monkeypatch)
    client = TestClient(app, base_url=BASE_URL)
    client.cookies.update(_session_cookie(config))

    missing = client.post("/scan")
    hostile = client.post("/scan", headers={"Origin": "https://attacker.example"})
    valid = client.post("/scan", headers={"Origin": BASE_URL})

    assert missing.status_code == 403
    assert hostile.status_code == 403
    assert valid.status_code == 422


def test_unauthenticated_unsafe_request_does_not_redirect(monkeypatch) -> None:
    _enable_site_access(monkeypatch)
    response = TestClient(app, base_url=BASE_URL).post("/scan")
    assert response.status_code == 403


def test_logout_requires_same_origin_and_clears_session(monkeypatch) -> None:
    config = _enable_site_access(monkeypatch)
    client = TestClient(app, base_url=BASE_URL)
    client.cookies.update(_session_cookie(config))

    from web.site_google_auth import read_google_site_session
    session = read_google_site_session(
        config,
        client.cookies.get("__Host-b3s_google_oidc_session"),
    )
    assert session is not None
    hostile = client.post(
        "/auth/google/logout",
        headers={"Origin": "https://attacker.example"},
        data={"csrf_token": session.csrf_token},
    )
    assert hostile.status_code == 403
    response = client.post(
        "/auth/google/logout",
        headers={"Origin": BASE_URL},
        data={"csrf_token": session.csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "__Host-b3s_google_oidc_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_health_and_versioned_api_are_exempt_and_bearer_api_is_unchanged(monkeypatch) -> None:
    _enable_site_access(monkeypatch)
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", "scanner-bearer-token")
    client = TestClient(app, base_url=BASE_URL)

    health_response = client.get("/health")
    api_health_response = client.get("/api/v1/health")
    api_unauthorized = client.get("/api/v1/capabilities")
    api_authorized = client.get(
        "/api/v1/capabilities",
        headers={"Authorization": "Bearer scanner-bearer-token"},
    )

    assert health_response.status_code == 200
    assert api_health_response.status_code == 200
    assert api_unauthorized.status_code == 401
    assert api_unauthorized.headers["www-authenticate"] == "Bearer"
    assert api_authorized.status_code == 200


@pytest.mark.parametrize("path", ["/health/", "/api/v1", "/api/v1ish/health"])
def test_exemption_boundaries_remain_google_protected(monkeypatch, path: str) -> None:
    _enable_site_access(monkeypatch)
    response = TestClient(app, base_url=BASE_URL).get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/google/login?")


@pytest.mark.parametrize(
    "path",
    [
        "/auth/google/",
        "/auth/google/callback/",
        "/auth/google/anything",
        "/auth/googleish",
    ],
)
def test_google_auth_namespace_has_no_prefix_bypass(monkeypatch, path: str) -> None:
    _enable_site_access(monkeypatch)
    response = TestClient(app, base_url=BASE_URL).get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/google/login?")


def test_legacy_basic_enabled_with_oidc_disabled_fails_closed(monkeypatch) -> None:
    _clear_site_access(monkeypatch)
    monkeypatch.setenv("B3S_SITE_BASIC_AUTH_ENABLED", "true")
    response = TestClient(app, base_url=BASE_URL).get("/", follow_redirects=False)
    assert response.status_code == 503
