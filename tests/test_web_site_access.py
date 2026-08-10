from __future__ import annotations

import base64
import secrets

import pytest
from fastapi.testclient import TestClient

from web.app import app


USERNAME = "vault.operator"
PASSWORD = "correct-horse-battery-staple-0123456789"


def _clear_site_access(monkeypatch) -> None:
    monkeypatch.delenv("B3S_SITE_BASIC_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("B3S_SITE_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("B3S_SITE_BASIC_AUTH_PASSWORD", raising=False)


def _enable_site_access(monkeypatch) -> None:
    monkeypatch.delenv("BRAND3_ENVIRONMENT", raising=False)
    monkeypatch.setenv("B3S_SITE_BASIC_AUTH_ENABLED", "true")
    monkeypatch.setenv("B3S_SITE_BASIC_AUTH_USERNAME", USERNAME)
    monkeypatch.setenv("B3S_SITE_BASIC_AUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("BRAND3_BASE_URL", "https://b3s-pr71-vault.example")


def _basic_header(username: str = USERNAME, password: str = PASSWORD) -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


@pytest.mark.parametrize("enabled", [None, "", "  ", "false", " FaLsE "])
def test_site_access_gate_is_opt_in_and_preserves_legacy_access(monkeypatch, enabled) -> None:
    _clear_site_access(monkeypatch)
    if enabled is not None:
        monkeypatch.setenv("B3S_SITE_BASIC_AUTH_ENABLED", enabled)

    response = TestClient(app).get("/dev/scan-preview")

    assert response.status_code == 200
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("POST", "/scan"),
        ("GET", "/scan/missing-scan"),
        ("GET", "/api/scan/missing-scan"),
        ("POST", "/api/scan/missing-scan/continue"),
        ("POST", "/api/scan/missing-scan/cancel"),
        ("GET", "/dev/scan-preview"),
        ("GET", "/health/apis?run=true"),
        ("GET", "/api/health/apis?run=true"),
        ("GET", "/api/scoring/lab"),
        ("GET", "/report/missing-report"),
        ("GET", "/report/missing-report.md"),
        ("GET", "/brand/example.com"),
        ("GET", "/artifacts/screenshots/missing.png"),
        ("GET", "/static/missing.css"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ],
)
def test_enabled_gate_protects_entire_non_v1_site_surface(
    monkeypatch,
    method: str,
    path: str,
) -> None:
    _enable_site_access(monkeypatch)

    response = TestClient(app).request(method, path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="B3S", charset="UTF-8"'
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer not-basic",
        "Basic !!!",
        "Basic " + base64.b64encode(b"missing-colon").decode("ascii"),
        "Basic " + base64.b64encode(f":{PASSWORD}".encode()).decode("ascii"),
        "Basic " + base64.b64encode(f"{USERNAME}:short".encode()).decode("ascii"),
        "Basic " + base64.b64encode(f"wrong-user:{PASSWORD}".encode()).decode("ascii"),
        "Basic " + base64.b64encode(f"{USERNAME}:{'x' * 32}".encode()).decode("ascii"),
    ],
)
def test_enabled_gate_rejects_malformed_and_wrong_credentials(
    monkeypatch,
    authorization: str,
) -> None:
    _enable_site_access(monkeypatch)

    response = TestClient(app).get(
        "/dev/scan-preview",
        headers={"Authorization": authorization},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert USERNAME not in response.text
    assert PASSWORD not in response.text


def test_enabled_gate_compares_both_credential_fields(monkeypatch) -> None:
    _enable_site_access(monkeypatch)
    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = secrets.compare_digest

    def compare_digest(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real_compare_digest(left, right)

    monkeypatch.setattr("web.app.secrets.compare_digest", compare_digest)

    response = TestClient(app).get(
        "/dev/scan-preview",
        headers=_basic_header("wrong-user", "x" * 32),
    )

    assert response.status_code == 401
    assert len(calls) == 2


def test_vault_reviewer_surface_uses_dedicated_session_without_site_basic(
    monkeypatch,
) -> None:
    _enable_site_access(monkeypatch)
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    reviewer_token = "reviewer-token-" + ("x" * 32)
    monkeypatch.setenv("B3S_EVIDENCE_ADJUDICATION_TOKEN", reviewer_token)
    monkeypatch.setenv("B3S_EVIDENCE_REVIEWER_ID", "test-reviewer")
    client = TestClient(app)

    login_page = client.get("/vault/review/login")
    assert login_page.status_code == 200
    assert "Unauthorized" not in login_page.text

    blocked_post = client.post(
        "/vault/review/login",
        data={"token": reviewer_token},
    )
    assert blocked_post.status_code == 403

    login = client.post(
        "/vault/review/login",
        data={"token": reviewer_token, "next_path": "/vault/review/example.com"},
        headers={"Origin": "https://b3s-pr71-vault.example"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert "b3s_vault_reviewer_session=" in login.headers["set-cookie"]


def test_correct_basic_credentials_proceed_to_existing_route_controls(monkeypatch) -> None:
    _enable_site_access(monkeypatch)
    client = TestClient(app)

    browser_response = client.get("/dev/scan-preview", headers=_basic_header())
    same_origin_headers = {
        **_basic_header(),
        "Origin": "https://b3s-pr71-vault.example",
    }
    invalid_form_response = client.post("/scan", headers=same_origin_headers)
    missing_scan_response = client.post(
        "/api/scan/missing-scan/cancel",
        headers=same_origin_headers,
    )
    reviewer_response = client.get("/vault/review/login", headers=_basic_header())

    assert browser_response.status_code == 200
    assert invalid_form_response.status_code == 422
    assert missing_scan_response.status_code == 404
    assert reviewer_response.status_code == 404


@pytest.mark.parametrize("origin", [None, "https://attacker.example", "null"])
def test_unsafe_legacy_requests_require_exact_same_origin(
    monkeypatch,
    origin: str | None,
) -> None:
    _enable_site_access(monkeypatch)
    headers = _basic_header()
    if origin is not None:
        headers["Origin"] = origin

    response = TestClient(app).post("/scan", headers=headers)

    assert response.status_code == 403
    assert response.text == "Forbidden\n"
    assert response.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in response.headers


def test_unsafe_legacy_request_accepts_normalized_configured_origin(monkeypatch) -> None:
    _enable_site_access(monkeypatch)
    monkeypatch.setenv(
        "BRAND3_BASE_URL",
        "https://B3S-PR71-VAULT.EXAMPLE:443/app/path",
    )
    headers = {
        **_basic_header(),
        "Origin": "https://b3s-pr71-vault.example",
    }

    response = TestClient(app).post("/scan", headers=headers)

    assert response.status_code == 422


def test_health_and_versioned_api_are_exempt_and_bearer_api_is_unchanged(monkeypatch) -> None:
    _enable_site_access(monkeypatch)
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", "scanner-bearer-token")
    client = TestClient(app)

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
    assert api_authorized.json()["authentication"] == "bearer"


@pytest.mark.parametrize("path", ["/health/", "/api/v1", "/api/v1ish/health"])
def test_exemption_boundaries_remain_protected(monkeypatch, path: str) -> None:
    _enable_site_access(monkeypatch)

    response = TestClient(app).get(path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")


@pytest.mark.parametrize(
    ("enabled", "username", "password"),
    [
        ("yes", USERNAME, PASSWORD),
        ("1", USERNAME, PASSWORD),
        ("true", "", PASSWORD),
        ("true", USERNAME, ""),
        ("true", "bad user", PASSWORD),
        ("true", "bad:user", PASSWORD),
        ("true", "a" * 65, PASSWORD),
        ("true", USERNAME, "x" * 31),
        ("true", USERNAME, " x" * 16),
        ("true", USERNAME, ("x" * 16) + "\n" + ("x" * 16)),
        ("true", USERNAME, ("x" * 16) + "\u200b" + ("x" * 16)),
        ("true", USERNAME, ("x" * 16) + "\ue000" + ("x" * 16)),
    ],
)
def test_invalid_enabled_configuration_fails_closed_without_detail(
    monkeypatch,
    enabled: str,
    username: str,
    password: str,
) -> None:
    monkeypatch.setenv("B3S_SITE_BASIC_AUTH_ENABLED", enabled)
    monkeypatch.setenv("B3S_SITE_BASIC_AUTH_USERNAME", username)
    monkeypatch.setenv("B3S_SITE_BASIC_AUTH_PASSWORD", password)
    client = TestClient(app)

    protected = client.get("/dev/scan-preview", headers=_basic_header())
    health = client.get("/health")

    assert protected.status_code == 503
    assert protected.text == "Service unavailable\n"
    assert "www-authenticate" not in protected.headers
    if username:
        assert username not in protected.text
    if password:
        assert password not in protected.text
    assert health.status_code == 200
