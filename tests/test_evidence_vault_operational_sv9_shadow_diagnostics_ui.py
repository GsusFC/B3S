from fastapi import HTTPException
from starlette.requests import Request

import web.app as web_app


def _request(*, authenticated: bool) -> Request:
    state = {"google_site_user": object()} if authenticated else {}
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/vault/diagnostics/sv9/example.com",
            "raw_path": b"/vault/diagnostics/sv9/example.com",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 443),
            "state": state,
        }
    )


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED", "true")


def _diagnostics():
    return {
        "enabled": True,
        "available": True,
        "domain": "example.com",
        "limit": 10,
        "count": 1,
        "has_more": False,
        "items": [
            {
                "evaluation_identity": "a" * 64,
                "assessment_status": "available",
                "sv9_score": 73,
                "base_average": 7.3,
                "verification_counts": {
                    "pending": 80,
                    "verified": 0,
                    "disputed": 0,
                    "stale": 0,
                    "unverifiable": 0,
                },
                "created_at": "2026-08-14T00:00:00+00:00",
            }
        ],
    }


def test_flag_off_does_not_call_repository(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED", "false")
    monkeypatch.setattr(
        web_app,
        "vault_sv9_shadow_diagnostics_for_domain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("called")),
    )
    try:
        web_app.vault_sv9_shadow_diagnostics_view(
            _request(authenticated=True),
            "example.com",
        )
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("disabled diagnostics must stay hidden")


def test_ui_requires_an_actual_google_site_user_even_if_flag_is_on(
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    try:
        web_app.vault_sv9_shadow_diagnostics_view(
            _request(authenticated=False),
            "example.com",
        )
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("diagnostics must fail closed without Google session")


def test_authenticated_ui_renders_only_short_sanitized_observations(
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        web_app,
        "vault_sv9_shadow_diagnostics_for_domain",
        lambda _domain, *, limit: {**_diagnostics(), "limit": limit},
    )
    response = web_app.vault_sv9_shadow_diagnostics_view(
        _request(authenticated=True),
        "example.com",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"
    assert "aaaaaaaaaaaa" in body
    assert "a" * 64 not in body
    assert "No es autoritativa" in body
    assert "public_scoring_effect=false" in body
    for forbidden in (
        "brand_id",
        "candidate_semantic_tiles",
        "assessment_output",
        "verification_requirements",
        "source_packet_id",
    ):
        assert forbidden not in body


def test_diagnostic_route_is_not_linked_from_product_templates() -> None:
    for name in ("brand.html.j2", "report.html.j2", "index.html.j2"):
        source = (web_app.Path(web_app.__file__).parent / "templates" / name).read_text(
            encoding="utf-8"
        )
        assert "/vault/diagnostics/sv9/" not in source
