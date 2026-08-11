from __future__ import annotations

from fastapi.testclient import TestClient

from web.app import app
from web.vault_reviewer_session import (
    VAULT_REVIEWER_COOKIE,
    VAULT_REVIEWER_SESSION_MAX_AGE,
    authenticate_vault_reviewer,
    issue_vault_reviewer_session,
    read_vault_reviewer_session,
)


REVIEWER_TOKEN = "vault-reviewer-token-for-tests"
SCANNER_TOKEN = "scanner-token-for-tests"
REVIEWER_ID = "gsus"
SUBJECT_ID = "b" * 64
CASE_ID = "scoring-recovery-example-com-coherencia-C6-bbbbbbbbbbbb"


def _configure_vault(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", SCANNER_TOKEN)
    monkeypatch.setenv(
        "B3S_EVIDENCE_ADJUDICATION_TOKEN",
        REVIEWER_TOKEN,
    )
    monkeypatch.setenv("B3S_EVIDENCE_REVIEWER_ID", REVIEWER_ID)
    monkeypatch.setenv("BRAND3_BASE_URL", "https://testserver")


def _preview(*, decision: str = "pending") -> dict:
    event_id = "00000000-0000-0000-0000-000000000042" if decision != "pending" else None
    return {
        "report_count": 2,
        "brand": {"name": "Example", "domain": "example.com"},
        "persistence": {
            "stored": True,
            "review_journal": "postgres",
        },
        "recovery_review_candidates": [
            {
                "case_id": CASE_ID,
                "candidate_fingerprint": SUBJECT_ID,
                "tile": {
                    "tile_key": "coherencia.C6",
                    "name": "Coherencia verbal",
                    "condition": "El discurso conserva la misma promesa.",
                    "evidence_contract": {
                        "ok": "La evidencia satisface el contrato.",
                        "reject": "No aceptar una coincidencia genérica.",
                    },
                    "latest_state": "sin_evidencia",
                    "proposed_state": "ok",
                },
                "evidence": {
                    "quote": "<script>private evidence</script>",
                    "source_urls": [
                        "https://example.com/proof",
                        "javascript:alert(1)",
                    ],
                    "source_classes": ["owned_copy"],
                    "acceptance_basis": ["literal_source_match"],
                    "first_seen_at": "2026-07-01T08:00:00+00:00",
                    "last_seen_at": "2026-07-30T08:00:00+00:00",
                    "observation_count": 2,
                },
                "contexts": [
                    {
                        "review_scope": "current_direct_relation",
                        "lane": "history",
                    }
                ],
                "review_prompt": "¿La cita satisface el contrato?",
            }
        ],
        "recovery_review": {
            "evaluated": [
                {
                    "case_id": CASE_ID,
                    "candidate_fingerprint": SUBJECT_ID,
                    "decision": decision,
                    "event_id": event_id,
                    "reviewer_id": REVIEWER_ID if event_id else None,
                }
            ]
        },
    }


def _login(client: TestClient):
    return client.post(
        "/vault/review/login",
        data={
            "token": REVIEWER_TOKEN,
            "next_path": "/vault/review/example.com",
        },
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )


def test_reviewer_routes_do_not_exist_outside_vault(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "production")
    monkeypatch.setenv("B3S_SITE_BASIC_AUTH_ENABLED", "false")
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/vault/review/example.com")

    assert response.status_code == 404


def test_protected_candidate_redirects_before_exposing_evidence(
    monkeypatch,
) -> None:
    _configure_vault(monkeypatch)
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain: _preview(),
    )
    client = TestClient(app, base_url="https://testserver")

    response = client.get(
        "/vault/review/example.com",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/vault/review/login?next_path=")
    assert "private evidence" not in response.text


def test_login_sets_short_lived_hardened_cookie(monkeypatch) -> None:
    _configure_vault(monkeypatch)
    client = TestClient(app, base_url="https://testserver")

    response = _login(client)

    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert f"{VAULT_REVIEWER_COOKIE}=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/vault/review" in cookie
    assert f"Max-Age={VAULT_REVIEWER_SESSION_MAX_AGE}" in cookie
    assert REVIEWER_TOKEN not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"


def test_invalid_login_is_generic_and_does_not_set_cookie(
    monkeypatch,
) -> None:
    _configure_vault(monkeypatch)
    client = TestClient(app, base_url="https://testserver")

    response = client.post(
        "/vault/review/login",
        data={"token": "wrong-secret", "next_path": "/"},
        headers={"Origin": "https://testserver"},
    )

    assert response.status_code == 401
    assert "Credencial de revisión no válida" in response.text
    assert "wrong-secret" not in response.text
    assert VAULT_REVIEWER_COOKIE not in response.headers.get(
        "set-cookie",
        "",
    )


def test_reviewer_session_rejects_tampering_and_expiry(monkeypatch) -> None:
    _configure_vault(monkeypatch)
    principal = authenticate_vault_reviewer(REVIEWER_TOKEN)
    cookie = issue_vault_reviewer_session(
        principal,
        issued_at=1_000,
        csrf_token="c" * 64,
    )

    assert read_vault_reviewer_session(cookie, now=1_001) is not None
    assert read_vault_reviewer_session(f"{cookie}x", now=1_001) is None
    assert (
        read_vault_reviewer_session(
            cookie,
            now=1_000 + VAULT_REVIEWER_SESSION_MAX_AGE + 1,
        )
        is None
    )


def test_authenticated_view_exposes_exact_candidate_safely(
    monkeypatch,
) -> None:
    _configure_vault(monkeypatch)
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain: _preview(),
    )
    client = TestClient(app, base_url="https://testserver")
    _login(client)

    response = client.get("/vault/review/example.com")

    assert response.status_code == 200
    assert "coherencia.C6" in response.text
    assert "private evidence" in response.text
    assert "<script>private evidence</script>" not in response.text
    assert "javascript:alert" not in response.text
    assert "relación directa observada en el escaneo actual" in response.text
    assert "no reescribe el scan ni su scoring diagnóstico" in response.text
    assert "Registrar decisión firmada" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_decision_requires_csrf_and_binds_server_reviewer(
    monkeypatch,
) -> None:
    _configure_vault(monkeypatch)
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain: _preview(),
    )
    captured = {}

    def persist(
        domain,
        payload,
        *,
        client_id,
        reviewer_id,
        idempotency_key,
    ):
        captured.update(
            domain=domain,
            payload=payload,
            client_id=client_id,
            reviewer_id=reviewer_id,
            idempotency_key=idempotency_key,
        )
        return ({"event_id": "event-1"}, False)

    monkeypatch.setattr(
        "web.app.create_evidence_scoring_recovery_review",
        persist,
    )
    client = TestClient(app, base_url="https://testserver")
    _login(client)
    session = read_vault_reviewer_session(client.cookies.get(VAULT_REVIEWER_COOKIE))
    assert session is not None
    form = {
        "csrf_token": "wrong",
        "subject_id": SUBJECT_ID,
        "case_id": CASE_ID,
        "decision": "accepted",
        "expected_current_event_id": "",
        "rationale": "La cita satisface el contrato específico.",
        "idempotency_key": "vault-review-test-1",
    }

    rejected = client.post(
        "/vault/review/example.com/decisions",
        data=form,
        headers={"Origin": "https://testserver"},
    )
    assert rejected.status_code == 403
    assert captured == {}

    accepted = client.post(
        "/vault/review/example.com/decisions",
        data={**form, "csrf_token": session.csrf_token},
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    assert accepted.headers["location"] == (
        "/vault/review/example.com?state=actionable&saved=1"
    )
    assert captured["domain"] == "example.com"
    assert captured["reviewer_id"] == REVIEWER_ID
    assert captured["client_id"] == REVIEWER_ID
    assert captured["payload"]["subject_id"] == SUBJECT_ID
    assert captured["payload"]["case_id"] == CASE_ID
    assert captured["payload"]["reason_code"] == ("tile_contract_satisfied")
    assert captured["payload"]["evaluator_version"] == ("vault-manual-review-v1")
    assert captured["idempotency_key"] == "vault-review-test-1"


def test_registered_candidate_is_read_only(monkeypatch) -> None:
    _configure_vault(monkeypatch)
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain: _preview(decision="accepted"),
    )
    client = TestClient(app, base_url="https://testserver")
    _login(client)

    response = client.get("/vault/review/example.com?state=accepted")

    assert response.status_code == 200
    assert "Decisión registrada: accepted" in response.text
    assert "Registrar decisión firmada" not in response.text


def test_disputed_candidate_is_counted_and_can_be_resolved(
    monkeypatch,
) -> None:
    _configure_vault(monkeypatch)
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain: _preview(decision="disputed"),
    )
    client = TestClient(app, base_url="https://testserver")
    _login(client)

    response = client.get("/vault/review/example.com")

    assert response.status_code == 200
    assert "disputada · requiere resolución" in response.text
    assert "Esta disputa bloquea el paquete candidato" in response.text
    assert "Resolver disputa" in response.text
    assert "disputadas</span>\n      <strong>1</strong>" in response.text
    assert "disputas bloqueantes</span>\n      <strong>1</strong>" in response.text
    assert (
        'name="expected_current_event_id" '
        'value="00000000-0000-0000-0000-000000000042"'
    ) in response.text
    assert 'name="decision" value="accepted"' in response.text
    assert 'name="decision" value="rejected"' in response.text
    assert 'name="decision" value="disputed"' not in response.text


def test_review_state_navigation_filters_candidates_server_side(
    monkeypatch,
) -> None:
    _configure_vault(monkeypatch)
    monkeypatch.setattr(
        "web.app.evidence_scoring_memory_preview_for_domain",
        lambda _domain: _preview(decision="disputed"),
    )
    client = TestClient(app, base_url="https://testserver")
    _login(client)

    actionable = client.get("/vault/review/example.com")
    pending = client.get("/vault/review/example.com?state=pending")
    all_items = client.get("/vault/review/example.com?state=all")
    unknown = client.get("/vault/review/example.com?state=unknown")

    assert actionable.status_code == 200
    assert 'href="/vault/review/example.com?state=actionable"' in actionable.text
    assert 'aria-current="page"' in actionable.text
    assert "Por gestionar" in actionable.text
    assert "coherencia.C6" in actionable.text
    assert "Disputadas" in actionable.text
    assert "Aceptadas" in actionable.text
    assert "Rechazadas" in actionable.text

    assert pending.status_code == 200
    assert "coherencia.C6" not in pending.text
    assert "private evidence" not in pending.text
    assert "No hay relaciones en este estado de revisión" in pending.text
    assert "Ver relaciones por gestionar" in pending.text

    assert all_items.status_code == 200
    assert "coherencia.C6" in all_items.text
    assert unknown.status_code == 200
    assert "coherencia.C6" in unknown.text
