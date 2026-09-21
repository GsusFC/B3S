from __future__ import annotations

from web.vault_sv9_review_publication import publish_approved_sv9_review


def _resolution(decision: str) -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "decision": decision,
        "source_scan_id": "rescan-001",
        "created_at": "2026-09-21T00:00:00+00:00",
        "successor_authority_event_id": (
            "00000000-0000-0000-0000-000000000002"
            if decision == "approve"
            else None
        ),
    }


def test_rejected_resolution_never_publishes():
    called = []

    result = publish_approved_sv9_review(
        resolution_result={"resolution": _resolution("reject")},
        domain="example.com",
        repository=object(),
        publish=called.append,
    )

    assert result == {"state": "not_published", "reason": "review_rejected"}
    assert called == []


def test_approved_resolution_publishes_new_immutable_report(monkeypatch):
    candidate = {
        "id": "00000000-0000-0000-0000-000000000003",
        "source_scan_id": "rescan-001",
        "canonical_plan_fingerprint": "a" * 64,
        "complete_record_fingerprint": "b" * 64,
        "assessment_fingerprint": "c" * 64,
        "score_fingerprint": "d" * 64,
    }
    authority = {"accepted_candidate": candidate}
    published = []
    observed = {
        "canonical_url": "https://example.com",
        "brand_name": "Example",
    }
    repository = type(
        "Repository",
        (),
        {"get_capture_operation_plan": lambda _self, _scan: {"raw_observation": observed}},
    )()
    monkeypatch.setattr(
        "web.vault_sv9_review_publication.project_vault_authority_publication",
        lambda application, current: (
            assert_application(application, current),
            {"action": "publish_current", "scanner_payload": {"sv9": {}}},
        )[1],
    )
    monkeypatch.setattr(
        "web.vault_sv9_review_publication._authority_scanner_payload",
        lambda **_kwargs: {"sv9": {}},
    )
    monkeypatch.setattr(
        "web.vault_sv9_review_publication._compose_report",
        lambda report_id, url, brand, _payload: {
            "id": report_id,
            "url": url,
            "brand_name": brand,
            "score": 81,
            "score_fingerprint": "d" * 64,
        },
    )

    result = publish_approved_sv9_review(
        resolution_result={
            "resolution": _resolution("approve"),
            "authority": authority,
        },
        domain="example.com",
        repository=repository,
        publish=published.append,
    )

    assert result["state"] == "published"
    assert result["report_id"].startswith("sv9-review-")
    assert len(published) == 1
    assert published[0]["review_publication"]["authority"] is True


def assert_application(application, current):
    assert current == "rescan-001"
    assert application["status"] == "authority_advanced"
    assert application["authority"]["accepted_candidate"]["id"]
    return True
