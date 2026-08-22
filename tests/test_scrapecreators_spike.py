from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest

from src.research.scrapecreators_spike import (
    MAX_ATTEMPTS,
    ManifestError,
    ProviderRequestError,
    ScrapeCreatorsClient,
    atomic_write_json,
    build_dry_run,
    parse_target_manifest,
    plan_requests,
    run_acquisition,
)

_FAKE_KEY = "fake-test-credential-never-real"


def _load_cli():
    script = Path(__file__).parents[1] / "scripts" / "scrapecreators_social_spike.py"
    spec = importlib.util.spec_from_file_location("scrapecreators_social_spike_cli", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest():
    return parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [
                {
                    "target_id": "brand-linkedin",
                    "platform": "linkedin",
                    "company_url": "https://www.linkedin.com/company/acme/",
                },
                {"target_id": "brand-twitter", "platform": "twitter", "handle": "acme"},
                {"target_id": "brand-instagram", "platform": "instagram", "handle": "acme.co"},
            ],
        }
    )


def _provider_payload(path: str) -> dict:
    if path == "/v1/linkedin/company":
        return {
            "success": True,
            "id": "li-1",
            "name": "Acme",
            "description": "Build deliberately",
            "website": "https://acme.example",
            "industry": "Software",
            "employeeCount": 42,
            "location": {"city": "Madrid", "country": "ES"},
            "x-api-key": _FAKE_KEY,
            _FAKE_KEY: "echoed as an object key",
        }
    if path == "/v1/linkedin/company/posts":
        return {
            "success": True,
            "posts": [
                {
                    "id": "li-post-1",
                    "url": "https://www.linkedin.com/posts/acme_1",
                    "datePublished": "2026-01-02T03:04:05Z",
                    "text": "A LinkedIn post",
                }
            ],
        }
    if path == "/v1/twitter/profile":
        return {
            "success": True,
            "rest_id": "tw-1",
            "legacy": {
                "screen_name": "acme",
                "name": "Acme on X",
                "description": f"safe before {_FAKE_KEY} safe after",
                "followers_count": 100,
                "friends_count": 4,
                "statuses_count": 9,
                "entities": {"url": {"urls": [{"expanded_url": "https://acme.example"}]}},
            },
        }
    if path == "/v1/twitter/user-tweets":
        return {
            "success": True,
            "tweets": [
                {
                    "rest_id": "tw-post-1",
                    "url": "https://x.com/acme/status/1",
                    "views": {"count": "77"},
                    "legacy": {
                        "created_at": "Thu Sep 12 00:08:39 +0000 2024",
                        "full_text": "An X post",
                        "favorite_count": 8,
                        "reply_count": 2,
                        "retweet_count": 3,
                        "quote_count": 1,
                        "bookmark_count": 4,
                    },
                }
            ],
        }
    if path == "/v1/instagram/profile":
        return {
            "success": True,
            "data": {
                "user": {
                    "id": "ig-1",
                    "username": "acme.co",
                    "full_name": "Acme on Instagram",
                    "biography": "Visual work",
                    "external_url": "https://acme.example",
                    "category_name": "Company",
                    "edge_followed_by": {"count": 200},
                    "edge_follow": {"count": 5},
                    "edge_owner_to_timeline_media": {"count": 12},
                }
            },
        }
    if path == "/v2/instagram/user/posts":
        return {
            "success": True,
            "items": [
                {
                    "id": "ig-post-1",
                    "code": "AbCd",
                    "taken_at": 1_700_000_000,
                    "caption": {"text": "An Instagram post"},
                    "media_type": 2,
                    "like_count": 11,
                    "comment_count": 1,
                    "play_count": 99,
                    "user": {"username": "acme.co"},
                }
            ],
        }
    raise AssertionError(path)


def test_manifest_accepts_only_exact_platform_identifiers() -> None:
    manifest = _manifest()

    assert manifest.targets[0].company_url == "https://www.linkedin.com/company/acme"
    assert manifest.targets[1].handle == "acme"
    assert len(plan_requests(manifest)) == 6

    with pytest.raises(ManifestError, match="only target_id, platform, and handle"):
        parse_target_manifest(
            {
                "schema_version": 1,
                "targets": [
                    {"target_id": "x", "platform": "twitter", "handle": "acme", "name": "Acme Inc."}
                ],
            }
        )
    with pytest.raises(ManifestError, match="invalid twitter handle"):
        parse_target_manifest(
            {"schema_version": 1, "targets": [{"target_id": "x", "platform": "twitter", "handle": "@acme"}]}
        )
    with pytest.raises(ManifestError, match="explicit LinkedIn /company/ URL"):
        parse_target_manifest(
            {
                "schema_version": 1,
                "targets": [
                    {
                        "target_id": "li",
                        "platform": "linkedin",
                        "company_url": "https://www.linkedin.com/search/results/companies/?keywords=Acme",
                    }
                ],
            }
        )


def test_mock_transport_calls_all_documented_endpoints_and_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    calls: list[httpx.Request] = []
    response_bytes: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = _provider_payload(request.url.path)
        content = json.dumps(payload, separators=(",", ":")).encode()
        response_bytes[request.url.path] = content
        return httpx.Response(200, content=content, headers={"content-type": "application/json"})

    transport = httpx.MockTransport(handler)
    with ScrapeCreatorsClient(transport=transport, sleep=lambda _delay: None) as client:
        assert _FAKE_KEY not in repr(client)
        result = run_acquisition(_manifest(), client, include_raw=True)

    assert [request.url.path for request in calls] == [
        "/v1/linkedin/company",
        "/v1/linkedin/company/posts",
        "/v1/twitter/profile",
        "/v1/twitter/user-tweets",
        "/v1/instagram/profile",
        "/v2/instagram/user/posts",
    ]
    assert all(request.headers["x-api-key"] == _FAKE_KEY for request in calls)
    assert dict(calls[0].url.params) == {"url": "https://www.linkedin.com/company/acme"}
    assert dict(calls[1].url.params) == {"url": "https://www.linkedin.com/company/acme", "page": "1"}
    assert dict(calls[3].url.params) == {"handle": "acme"}
    assert dict(calls[5].url.params) == {"handle": "acme.co"}

    assert result["summary"] == {
        "target_count": 3,
        "request_count": 6,
        "successful_request_count": 6,
        "failed_request_count": 0,
        "profile_count": 3,
        "post_count": 3,
        "credits_charged": 0,
        "raw_payloads_included": True,
    }
    assert all(row["content_id"].startswith("sha256:") for row in result["profiles"] + result["posts"])
    assert result["posts"][1]["published_at"] == "2024-09-12T00:08:39Z"
    assert result["posts"][2]["canonical_url"] == "https://www.instagram.com/p/AbCd/"
    assert result["posts"][2]["published_at"] == "2023-11-14T22:13:20Z"

    for response in result["responses"]:
        expected = hashlib.sha256(response_bytes[response["path"]]).hexdigest()
        assert response["raw_response_sha256"] == expected
    serialized = json.dumps(result)
    assert _FAKE_KEY not in serialized
    assert "[REDACTED]" in serialized
    assert result["responses"][0]["raw_payload_redacted"]["x-api-key"] == "[REDACTED]"


def test_raw_payload_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_provider_payload(request.url.path))

    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler), sleep=lambda _delay: None) as client:
        result = run_acquisition(_manifest(), client)

    assert result["summary"]["raw_payloads_included"] is False
    assert all("raw_payload_redacted" not in response for response in result["responses"])


def test_content_address_excludes_target_alias_and_raw_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    payload = _provider_payload("/v1/twitter/profile")
    compact = json.dumps(payload, separators=(",", ":")).encode()
    pretty = json.dumps(payload, indent=2).encode()
    bodies = iter((compact, pretty))
    plan = plan_requests(
        parse_target_manifest(
            {"schema_version": 1, "targets": [{"target_id": "first", "platform": "twitter", "handle": "acme"}]}
        )
    )[0]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(bodies), headers={"content-type": "application/json"})

    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler), sleep=lambda _delay: None) as client:
        first_response = client.get(plan)
        second_response = client.get(plan)

    from src.research.scrapecreators_spike import _normalize_profile

    first_target = parse_target_manifest(
        {"schema_version": 1, "targets": [{"target_id": "first", "platform": "twitter", "handle": "acme"}]}
    ).targets[0]
    second_target = parse_target_manifest(
        {"schema_version": 1, "targets": [{"target_id": "second", "platform": "twitter", "handle": "acme"}]}
    ).targets[0]
    first = _normalize_profile(first_target, "twitter_profile", first_response)
    second = _normalize_profile(second_target, "twitter_profile", second_response)

    assert first["content_id"] == second["content_id"]
    assert first["source_response_sha256"] != second["source_response_sha256"]
    assert first["target_id"] != second["target_id"]


def test_retry_is_bounded_and_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "999"})
        if attempts == 2:
            raise httpx.ConnectError("test connection failure", request=request)
        return httpx.Response(200, json=_provider_payload(request.url.path))

    plan = plan_requests(_manifest())[0]
    with ScrapeCreatorsClient(
        transport=httpx.MockTransport(handler),
        max_attempts=3,
        retry_base_seconds=0.25,
        sleep=sleeps.append,
    ) as client:
        response = client.get(plan)

    assert response.attempts == 3
    assert attempts == 3
    assert sleeps == [2.0, 0.5]


def test_provider_failure_does_not_include_body_or_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"provider echoed {_FAKE_KEY}")

    with ScrapeCreatorsClient(
        transport=httpx.MockTransport(handler), max_attempts=2, retry_base_seconds=0, sleep=lambda _delay: None
    ) as client:
        with pytest.raises(ProviderRequestError) as caught:
            client.get(plan_requests(_manifest())[0])

    assert _FAKE_KEY not in str(caught.value)
    assert "provider echoed" not in str(caught.value)
    assert "after 2 attempt(s): HTTP 500" in str(caught.value)


def test_client_configuration_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    with pytest.raises(ValueError, match="timeout_seconds"):
        ScrapeCreatorsClient(timeout_seconds=61)
    with pytest.raises(ValueError, match="max_attempts"):
        ScrapeCreatorsClient(max_attempts=MAX_ATTEMPTS + 1)


def test_dry_run_cli_needs_no_key_and_writes_mode_0600(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SCRAPECREATORS_API_KEY", raising=False)
    manifest_path = tmp_path / "targets.json"
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    cli = _load_cli()
    cli.OUTPUT_ROOT = tmp_path / "out" / "scrapecreators-social-spike"
    output_path = cli.OUTPUT_ROOT / "nested" / "result.json"

    result = cli.main(["--manifest", str(manifest_path), "--output", str(output_path), "--dry-run"])

    assert result == 0
    payload = json.loads(output_path.read_text())
    assert payload == build_dry_run(_manifest())
    assert os.stat(output_path).st_mode & 0o777 == 0o600
    assert "x-api-key" not in output_path.read_text()
    assert "Wrote secure spike output" in capsys.readouterr().out


def test_spike_cli_rejects_outside_traversal_and_symlink_escape_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SCRAPECREATORS_API_KEY", raising=False)
    cli = _load_cli()
    cli.OUTPUT_ROOT = tmp_path / "out" / "scrapecreators-social-spike"
    manifest_path = tmp_path / "targets.json"
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")

    for destination in (outside, cli.OUTPUT_ROOT.parent / "sibling.json", cli.OUTPUT_ROOT / ".." / "escape.json"):
        assert cli.main(["--manifest", str(manifest_path), "--output", str(destination), "--dry-run"]) == 2
    assert outside.read_text(encoding="utf-8") == "sentinel"

    escaped_dir = tmp_path / "escaped-dir"
    escaped_dir.mkdir()
    symlink_parent = cli.OUTPUT_ROOT / "symlink-parent"
    symlink_parent.parent.mkdir(parents=True, exist_ok=True)
    symlink_parent.symlink_to(escaped_dir, target_is_directory=True)
    escaped_destination = symlink_parent / "result.json"
    assert cli.main(["--manifest", str(manifest_path), "--output", str(escaped_destination), "--dry-run"]) == 2
    assert not (escaped_dir / "result.json").exists()


def test_atomic_writer_replaces_existing_file_and_leaves_no_temp(tmp_path: Path) -> None:
    output_path = tmp_path / "result.json"
    output_path.write_text("old", encoding="utf-8")
    os.chmod(output_path, 0o644)

    atomic_write_json(output_path, {"safe": True})

    assert json.loads(output_path.read_text()) == {"safe": True}
    assert os.stat(output_path).st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".result.json.*.tmp")) == []


def test_rejects_ambiguous_success_and_invalid_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    plan = plan_requests(_manifest())[2]

    for payload in ({"error": "rate limit"}, {"success": None}, {"success": True, "legacy": {}}):
        with ScrapeCreatorsClient(
            transport=httpx.MockTransport(lambda _request, payload=payload: httpx.Response(200, json=payload)),
            max_attempts=1,
        ) as client:
            with pytest.raises(ProviderRequestError):
                client.get(plan)


def test_live_run_isolates_endpoint_failure_and_records_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/linkedin/company/posts":
            return httpx.Response(503, json={"success": False})
        payload = _provider_payload(request.url.path)
        payload["credits_charged"] = 2
        payload["credits_remaining"] = 100
        return httpx.Response(200, json=payload)

    with ScrapeCreatorsClient(
        transport=httpx.MockTransport(handler), max_attempts=1
    ) as client:
        result = run_acquisition(_manifest(), client)

    assert result["summary"]["successful_request_count"] == 5
    assert result["summary"]["failed_request_count"] == 1
    assert result["summary"]["credits_charged"] == 10
    failed = [response for response in result["responses"] if response["outcome"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["path"] == "/v1/linkedin/company/posts"
    assert "HTTP 503" in failed[0]["failure"]


def test_handles_are_case_insensitive_duplicates() -> None:
    with pytest.raises(ManifestError, match="duplicate explicit target"):
        parse_target_manifest(
            {
                "schema_version": 1,
                "targets": [
                    {"target_id": "x-one", "platform": "twitter", "handle": "Acme"},
                    {"target_id": "x-two", "platform": "twitter", "handle": "acme"},
                ],
            }
        )


def test_instagram_zero_play_count_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _provider_payload(request.url.path)
        if request.url.path == "/v2/instagram/user/posts":
            payload["items"][0]["play_count"] = 0
        return httpx.Response(200, json=payload)

    instagram = parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "ig", "platform": "instagram", "handle": "acme.co"}],
        }
    )
    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler)) as client:
        result = run_acquisition(instagram, client)
    assert result["posts"][0]["metrics"]["plays"] == 0


def test_contract_observations_bind_provenance_and_metric_free_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    manifest = parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "brand", "platform": "twitter", "handle": "acme"}],
        }
    )

    def run_with_metrics(likes: int) -> dict:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = _provider_payload(request.url.path)
            if request.url.path == "/v1/twitter/user-tweets":
                payload["tweets"][0]["legacy"]["favorite_count"] = likes
            return httpx.Response(200, json=payload)

        with ScrapeCreatorsClient(transport=httpx.MockTransport(handler)) as client:
            return run_acquisition(manifest, client)

    first = run_with_metrics(1)
    second = run_with_metrics(999)
    first_observation = first["posts"][0]["observation"]
    second_observation = second["posts"][0]["observation"]

    assert first_observation["content_id"] == second_observation["content_id"]
    assert first_observation["semantic_fingerprint"] == second_observation["semantic_fingerprint"]
    assert first_observation["metric_context"]["fingerprint"] != second_observation["metric_context"]["fingerprint"]
    provenance = first_observation["provenance"]
    assert provenance["provider"] == "scrapecreators"
    assert provenance["endpoint_key"] == "twitter_user_tweets"
    assert provenance["target_fingerprint"]
    assert provenance["manifest_fingerprint"] == first["manifest_sha256"]
    assert provenance["request_fingerprint"]
    assert len(provenance["response_sha256"]) == 64
    assert provenance["fetched_at"]
    assert provenance["target_identity"] == {"account_id": "tw-1", "handle": "acme"}
    assert provenance["normalization_version"] == "social-lab-normalization-v1"


def test_interaction_matrix_classifies_supported_roles_and_x_not_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    manifest = _manifest()
    interaction_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in {"/v1/linkedin/post", "/v2/instagram/post/comments"}:
            interaction_calls.append(path)
            if path == "/v1/linkedin/post":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "comments": [
                            {
                                "id": "li-community",
                                "text": "Useful.",
                                "author": {"id": "person-1", "name": "Person"},
                                "linkedinUrl": "https://www.linkedin.com/in/person-1",
                            },
                            {"id": "li-ambiguous", "text": "Unknown author"},
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "comments": [
                        {
                            "id": "ig-community",
                            "text": "Love it",
                            "user": {"id": "person-2", "username": "person2"},
                        },
                        {
                            "id": "ig-brand",
                            "text": "Thanks",
                            "user": {"id": "ig-1", "username": "acme.co"},
                        },
                    ],
                },
            )
        return httpx.Response(200, json=_provider_payload(path))

    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler)) as client:
        result = run_acquisition(manifest, client, include_interactions=True)

    assert interaction_calls == ["/v1/linkedin/post", "/v2/instagram/post/comments"]
    li = result["capability_matrix"]["brand-linkedin"]
    ig = result["capability_matrix"]["brand-instagram"]
    x = result["capability_matrix"]["brand-twitter"]
    assert li["community_response"]["status"] == "acquired"
    assert ig["community_response"]["status"] == "acquired"
    assert ig["brand_reply"]["status"] == "acquired"
    assert x["community_response"] == {
        "target_id": "brand-twitter",
        "platform": "twitter",
        "role": "community_response",
        "status": "not_acquired",
        "reason": "no_verified_reply_tree_endpoint",
        "provenance": [],
    }
    ambiguous = [
        observation
        for observation in result["observations"]
        if observation["external_id"] in {"li-ambiguous"}
    ]
    assert len(ambiguous) == 1
    assert ambiguous[0]["actor_role"] == "unclassified"


def test_supported_interaction_failure_is_preserved_and_replies_require_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    manifest = parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "ig", "platform": "instagram", "handle": "acme.co"}],
        }
    )

    def failing_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/instagram/post/comments":
            return httpx.Response(503, json={"success": False})
        return httpx.Response(200, json=_provider_payload(request.url.path))

    with ScrapeCreatorsClient(transport=httpx.MockTransport(failing_handler), max_attempts=1) as client:
        result = run_acquisition(manifest, client, include_interactions=True)

    status = result["capability_matrix"]["ig"]["community_response"]
    assert status["status"] == "failed"
    assert status["reason"] == "interaction_endpoint_failed"
    assert status["provenance"][0]["endpoint_key"] == "instagram_post_comments"

    with ScrapeCreatorsClient(transport=httpx.MockTransport(failing_handler), max_attempts=1) as client:
        with pytest.raises(ValueError, match="max_interaction_credits"):
            run_acquisition(manifest, client, include_interactions=True, include_replies=True)


def test_instagram_reply_opt_in_uses_bounded_reply_endpoint_and_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    manifest = parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "ig", "platform": "instagram", "handle": "acme.co"}],
        }
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/instagram/profile":
            return httpx.Response(
                200,
                json={"success": True, "data": {"user": {"id": "ig-1", "username": "acme.co"}}},
            )
        if request.url.path == "/v2/instagram/user/posts":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "items": [{"id": "post-1", "code": "Code", "caption": "post", "taken_at": 1_700_000_000}],
                },
            )
        if request.url.path == "/v2/instagram/post/comments":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "comments": [{"id": "comment-1", "text": "hi", "user": {"id": "user-1", "username": "u"}}],
                    "cursor": "next-comments",
                }
                if "cursor" not in request.url.params
                else {"success": True, "comments": [], "cursor": None},
            )
        if request.url.path == "/v1/instagram/post/comment/replies":
            assert request.url.params["comment_id"] == "comment-1"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "replies": [{"id": "reply-1", "text": "reply", "user": {"id": "user-2", "username": "v2"}}],
                },
            )
        raise AssertionError(request.url.path)

    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler)) as client:
        result = run_acquisition(
            manifest,
            client,
            include_interactions=True,
            include_replies=True,
            max_interaction_pages=2,
            max_interaction_credits=40,
        )

    comments_calls = [request for request in calls if request.url.path == "/v2/instagram/post/comments"]
    assert len(comments_calls) == 2
    assert comments_calls[1].url.params["cursor"] == "next-comments"
    assert any(request.url.path == "/v1/instagram/post/comment/replies" for request in calls)
    assert any(observation["external_id"] == "reply-1" for observation in result["observations"])


def test_expanded_instagram_comments_reserve_fifteen_credits_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    manifest = parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "ig", "platform": "instagram", "handle": "acme.co"}],
        }
    )
    interaction_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/instagram/post/comments":
            interaction_calls.append(request.url.path)
        return httpx.Response(200, json=_provider_payload(request.url.path))

    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler)) as client:
        result = run_acquisition(
            manifest,
            client,
            include_interactions=True,
            include_replies=True,
            max_interaction_credits=1,
        )

    assert interaction_calls == []
    status = result["capability_matrix"]["ig"]["community_response"]
    assert status["status"] == "not_acquired"
    assert status["reason"] == "interaction_credit_budget_insufficient"
    assert status["provenance"][0]["credit_ceiling"] == 15
    assert status["provenance"][0]["remaining_credit_budget"] == 1


@pytest.mark.parametrize("charge_metadata", ["absent", "zero", "underreported"])
def test_interaction_pagination_reserves_each_page_before_call_even_without_full_charge_metadata(
    monkeypatch: pytest.MonkeyPatch, charge_metadata: str
) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    manifest = parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "ig", "platform": "instagram", "handle": "acme.co"}],
        }
    )
    interaction_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/instagram/post/comments":
            interaction_calls.append(request)
            payload = {
                "success": True,
                "comments": [
                    {"id": "comment-1", "text": "hi", "user": {"id": "user-1", "username": "u"}}
                ],
                "cursor": "next",
            }
            if charge_metadata == "zero":
                payload["credits_charged"] = 0
            elif charge_metadata == "underreported":
                payload["credits_charged"] = 0
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=_provider_payload(request.url.path))

    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler)) as client:
        result = run_acquisition(
            manifest,
            client,
            include_interactions=True,
            max_interaction_pages=3,
            max_interaction_credits=1,
        )

    assert len(interaction_calls) == 1
    status = result["capability_matrix"]["ig"]["community_response"]
    assert status["status"] == "partial"
    budget_blocks = [item for item in status["provenance"] if item.get("reason")]
    assert budget_blocks[-1]["reason"] == "interaction_credit_budget_exhausted"
    assert budget_blocks[-1]["remaining_credit_budget"] == 0
    assert budget_blocks[-1]["interaction_credits_reserved"] == 1


@pytest.mark.parametrize("charge_metadata", ["absent", "zero", "underreported"])
def test_reply_reservation_consumes_comments_ceiling_before_dedicated_reply_call(
    monkeypatch: pytest.MonkeyPatch, charge_metadata: str
) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    manifest = parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "ig", "platform": "instagram", "handle": "acme.co"}],
        }
    )
    reply_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/instagram/post/comments":
            payload: dict[str, object] = {
                "success": True,
                "comments": [{"id": "comment-1", "text": "hi", "user": {"id": "user-1", "username": "u"}}],
            }
            if charge_metadata == "zero":
                payload["credits_charged"] = 0
            elif charge_metadata == "underreported":
                payload["credits_charged"] = 1
            return httpx.Response(200, json=payload)
        if request.url.path == "/v1/instagram/post/comment/replies":
            reply_calls.append(request.url.path)
        return httpx.Response(200, json=_provider_payload(request.url.path))

    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler)) as client:
        result = run_acquisition(
            manifest,
            client,
            include_interactions=True,
            include_replies=True,
            max_interaction_credits=15,
        )

    assert reply_calls == []
    status = result["capability_matrix"]["ig"]["community_response"]
    budget_blocks = [item for item in status["provenance"] if item.get("reason")]
    assert budget_blocks[-1]["reason"] == "interaction_credit_budget_exhausted"
    assert budget_blocks[-1]["interaction_credits_reserved"] == 15


def test_dedicated_reply_is_skipped_when_comments_consume_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    manifest = parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "ig", "platform": "instagram", "handle": "acme.co"}],
        }
    )
    reply_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/instagram/post/comments":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "comments": [{"id": "comment-1", "text": "hi", "user": {"id": "user-1", "username": "u"}}],
                    "credits_charged": 15,
                },
            )
        if request.url.path == "/v1/instagram/post/comment/replies":
            reply_calls.append(request.url.path)
        return httpx.Response(200, json=_provider_payload(request.url.path))

    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler)) as client:
        result = run_acquisition(
            manifest,
            client,
            include_interactions=True,
            include_replies=True,
            max_interaction_credits=15,
        )

    assert reply_calls == []
    status = result["capability_matrix"]["ig"]["community_response"]
    assert any(item.get("reason") == "interaction_credit_budget_exhausted" for item in status["provenance"])


def test_provider_credit_overcharge_is_accounted_and_surfaced_as_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", _FAKE_KEY)
    manifest = parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "ig", "platform": "instagram", "handle": "acme.co"}],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/instagram/post/comments":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "comments": [{"id": "comment-1", "text": "hi", "user": {"id": "user-1", "username": "u"}}],
                    "credits_charged": 4,
                },
            )
        return httpx.Response(200, json=_provider_payload(request.url.path))

    with ScrapeCreatorsClient(transport=httpx.MockTransport(handler)) as client:
        result = run_acquisition(
            manifest,
            client,
            include_interactions=True,
            max_interaction_pages=1,
            max_interaction_credits=10,
        )

    assert result["summary"]["interaction_credits_charged"] == 4
    assert result["credit_diagnostics"][0]["declared_ceiling"] == 1
    assert result["credit_diagnostics"][0]["actual_credits_charged"] == 4
    interaction_response = next(
        response for response in result["responses"] if response["endpoint_key"] == "instagram_post_comments"
    )
    assert interaction_response["credit_diagnostic"]["actual_credits_charged"] == 4
