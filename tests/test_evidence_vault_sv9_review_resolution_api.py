from __future__ import annotations

from fastapi.testclient import TestClient

from web.app import app


REVIEW_AUTH = {"Authorization": "Bearer review-token"}
SCANNER_AUTH = {"Authorization": "Bearer scanner-token"}


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("B3S_SCANNER_API_TOKEN", "scanner-token")
    monkeypatch.setenv("B3S_EVIDENCE_ADJUDICATION_TOKEN", "review-token")
    monkeypatch.setenv("B3S_EVIDENCE_REVIEWER_ID", "reviewer")


def _resolution(decision: str = "approve") -> dict:
    return {"id": "00000000-0000-0000-0000-000000000001", "decision": decision}


def test_review_resolution_api_requires_adjudication_scope(monkeypatch):
    _configure(monkeypatch)

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-vault/sv9/"
        "judgment-review-resolutions",
        headers=SCANNER_AUTH,
        json={"resolution": _resolution()},
    )

    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == (
        "evidence:adjudicate"
    )


def test_review_resolution_api_returns_durable_result_and_replay_header(monkeypatch):
    _configure(monkeypatch)
    result = {
        "successor": {"id": "successor-1"},
        "resolution": _resolution(),
        "authority": {"authority": True},
    }
    monkeypatch.setattr(
        "web.api_v1.router.resolve_evidence_vault_sv9_review",
        lambda domain, payload: (result, True),
    )

    response = TestClient(app).post(
        "/api/v1/brands/example.com/evidence-vault/sv9/"
        "judgment-review-resolutions",
        headers=REVIEW_AUTH,
        json={"resolution": _resolution()},
    )

    assert response.status_code == 201
    assert response.headers["idempotent-replayed"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["domain"] == "example.com"
    assert response.json()["authority"]["authority"] is True


def test_review_resolution_service_maps_repository_conflict(monkeypatch):
    from src.history.repository import EvidenceVaultSv9JudgmentCandidateConflictError
    from web.api_v1.errors import ApiError
    from web.api_v1.service import resolve_evidence_vault_sv9_review

    monkeypatch.setattr(
        "web.api_v1.service._postgres_repository",
        lambda: type(
            "Repository",
            (),
            {
                "resolve_evidence_vault_sv9_judgment_review": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    EvidenceVaultSv9JudgmentCandidateConflictError("stale")
                )
            },
        )(),
    )

    try:
        resolve_evidence_vault_sv9_review(
            "example.com", {"resolution": _resolution()}
        )
    except ApiError as exc:
        assert exc.status_code == 409
        assert exc.code == "sv9_review_resolution_precondition_failed"
    else:
        raise AssertionError("expected ApiError")


def test_review_resolution_service_preserves_publication_error(monkeypatch):
    from web.api_v1.errors import ApiError
    from web.api_v1.service import resolve_evidence_vault_sv9_review

    result = {
        "resolution": _resolution(),
        "authority": {"accepted_candidate": {"id": "candidate-1"}},
    }
    repository = type(
        "Repository",
        (),
        {
            "resolve_evidence_vault_sv9_judgment_review": lambda *_args, **_kwargs: (
                result,
                False,
            )
        },
    )()
    monkeypatch.setattr("web.api_v1.service._postgres_repository", lambda: repository)
    monkeypatch.setattr(
        "web.vault_sv9_review_publication.publish_approved_sv9_review",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("report store unavailable")
        ),
    )

    try:
        resolve_evidence_vault_sv9_review("example.com", {"resolution": _resolution()})
    except ApiError as exc:
        assert exc.status_code == 503
        assert exc.code == "sv9_review_publication_unavailable"
        assert exc.details == {"resolution_persisted": True}
    else:
        raise AssertionError("expected ApiError")
