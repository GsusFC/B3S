from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from src.research.social_lab_contracts import (
    ACTOR_ROLES,
    SOCIAL_LAB_CONTRACT_VERSION,
    ActorRole,
    MetricContext,
    Provenance,
    SocialLabContractError,
    SocialObservation,
    build_social_observation,
    metric_fingerprint,
    resolve_actor_role,
    semantic_fingerprint,
)


BRAND_IDENTITY = {"account_id": "brand-account-1", "handle": "brandco"}


def _provenance(*, linkage: dict[str, object] | None = None) -> Provenance:
    return Provenance(
        provider="scrapecreators",
        endpoint_key="brand_posts",
        target_fingerprint="target-fingerprint-1",
        manifest_fingerprint="manifest-fingerprint-1",
        request_fingerprint="request-fingerprint-1",
        response_sha256="a" * 64,
        fetched_at="2026-08-22T08:00:00Z",
        page=1,
        cursor="cursor-1",
        result_ordinal=0,
        target_identity=BRAND_IDENTITY,
        normalization_version="social-lab-normalization-v1",
        linkage_evidence=linkage or {},
    )


def _post(
    *,
    metrics: dict[str, object] | None = None,
    fetched_at: str = "2026-08-22T08:00:00Z",
    provenance: Provenance | None = None,
) -> SocialObservation:
    selected_provenance = provenance or _provenance()
    if fetched_at != selected_provenance.fetched_at:
        selected_provenance = replace(selected_provenance, fetched_at=fetched_at)
    metric_context = (
        MetricContext(metrics=metrics, observed_at="2026-08-22T08:01:00Z") if metrics is not None else None
    )
    return build_social_observation(
        platform="x",
        record_kind="post",
        external_id="post-1",
        canonical_url="https://social.example/brandco/status/post-1",
        author_account_id="brand-account-1",
        author_handle="brandco",
        parent_external_id=None,
        thread_external_id=None,
        text="A useful launch update.",
        media_kind="text",
        published_at="2026-08-21T12:00:00Z",
        provenance=selected_provenance,
        metric_context=metric_context,
        bound_brand_identity=BRAND_IDENTITY,
    )


def test_contract_version_and_required_fields_are_json_compatible() -> None:
    observation = _post(metrics={"likes": 4, "followers": 100})

    payload = observation.to_dict()

    assert observation.contract_version == SOCIAL_LAB_CONTRACT_VERSION
    assert set(
        (
            "platform",
            "record_kind",
            "external_id",
            "canonical_url",
            "author_account_id",
            "author_handle",
            "parent_external_id",
            "thread_external_id",
            "text",
            "media_kind",
            "published_at",
            "actor_role",
            "content_id",
            "provenance",
        )
    ).issubset(payload)
    assert payload["actor_role"] == ActorRole.OFFICIAL_BRAND_POST.value
    json.dumps(payload, sort_keys=True)


def test_role_resolution_fails_closed_for_official_and_interaction_roles() -> None:
    assert (
        resolve_actor_role(
            record_kind="post",
            author_account_id="brand-account-1",
            author_handle="brandco",
            bound_brand_identity=BRAND_IDENTITY,
            parent_external_id=None,
            thread_external_id=None,
        )
        == ActorRole.OFFICIAL_BRAND_POST
    )
    assert (
        resolve_actor_role(
            record_kind="response",
            author_account_id="community-account-1",
            author_handle="community",
            bound_brand_identity=BRAND_IDENTITY,
            parent_external_id="post-1",
            thread_external_id="post-1",
        )
        == ActorRole.COMMUNITY_RESPONSE
    )
    assert (
        resolve_actor_role(
            record_kind="reply",
            author_account_id="brand-account-1",
            author_handle="brandco",
            bound_brand_identity=BRAND_IDENTITY,
            parent_external_id="response-1",
            thread_external_id="post-1",
        )
        == ActorRole.BRAND_REPLY
    )
    assert (
        resolve_actor_role(
            record_kind="unknown",
            author_account_id="brand-account-1",
            author_handle="brandco",
            bound_brand_identity=BRAND_IDENTITY,
            parent_external_id="post-1",
            thread_external_id="post-1",
        )
        == ActorRole.UNCLASSIFIED
    )
    assert (
        resolve_actor_role(
            record_kind="response",
            author_account_id=None,
            author_handle="different-looking-handle",
            bound_brand_identity=BRAND_IDENTITY,
            parent_external_id="post-1",
            thread_external_id="post-1",
        )
        == ActorRole.UNCLASSIFIED
    )


def test_role_resolution_requires_exact_identity_and_resolvable_linkage() -> None:
    assert (
        resolve_actor_role(
            record_kind="post",
            author_account_id="brand-account-1",
            author_handle="lookalike",
            bound_brand_identity=BRAND_IDENTITY,
            parent_external_id=None,
            thread_external_id=None,
        )
        == ActorRole.UNCLASSIFIED
    )
    assert (
        resolve_actor_role(
            record_kind="response",
            author_account_id="community-account-1",
            author_handle="community",
            bound_brand_identity=BRAND_IDENTITY,
            parent_external_id=None,
            thread_external_id=None,
        )
        == ActorRole.UNCLASSIFIED
    )
    assert (
        resolve_actor_role(
            record_kind="post",
            author_account_id="brand-account-1",
            author_handle="brandco",
            bound_brand_identity=None,
            parent_external_id=None,
            thread_external_id=None,
        )
        == ActorRole.UNCLASSIFIED
    )


def test_direct_observation_rejects_forged_role_from_provenance_identity() -> None:
    with pytest.raises(SocialLabContractError, match="does not match fail-closed role resolution"):
        SocialObservation(
            platform="x",
            record_kind="post",
            external_id="community-post-1",
            canonical_url="https://social.example/community/status/community-post-1",
            author_account_id="community-account-1",
            author_handle="community",
            parent_external_id=None,
            thread_external_id=None,
            text="A community post.",
            media_kind="text",
            published_at="2026-08-21T13:00:00Z",
            actor_role=ActorRole.OFFICIAL_BRAND_POST,
            provenance=_provenance(),
        )


def test_metrics_and_acquisition_metadata_cannot_change_semantic_identity() -> None:
    base = _post(metrics={"likes": 4, "followers": 100})
    changed_metrics = _post(metrics={"likes": 400, "followers": 10_000})
    changed_fetch = _post(metrics={"likes": 4, "followers": 100}, fetched_at="2026-08-23T10:00:00Z")
    changed_metric_observation = replace(
        changed_fetch,
        metric_context=MetricContext(
            metrics={"likes": 4, "followers": 100},
            observed_at="2026-08-23T10:01:00Z",
        ),
    )

    assert base.content_id == changed_metrics.content_id == changed_fetch.content_id == changed_metric_observation.content_id
    assert (
        base.semantic_fingerprint
        == changed_metrics.semantic_fingerprint
        == changed_fetch.semantic_fingerprint
        == changed_metric_observation.semantic_fingerprint
    )
    assert base.metric_context is not None
    assert changed_metrics.metric_context is not None
    assert changed_metric_observation.metric_context is not None
    assert base.metric_context.fingerprint != changed_metrics.metric_context.fingerprint
    assert base.metric_context.fingerprint != changed_metric_observation.metric_context.fingerprint


def test_metric_fingerprint_is_deterministic_and_observed_at_is_bound() -> None:
    first = MetricContext(metrics={"likes": 1, "comments": 2}, observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc))
    second = MetricContext(metrics={"comments": 2, "likes": 1}, observed_at="2026-08-22T00:00:00Z")

    assert first.fingerprint == second.fingerprint
    assert metric_fingerprint({"likes": 1, "comments": 2}, "2026-08-22T00:00:00Z") == first.fingerprint


def test_semantic_fingerprint_ignores_provenance_and_metric_context() -> None:
    first = _post(metrics={"likes": 1})
    second = _post(
        metrics={"likes": 999},
        provenance=replace(
            _provenance(),
            endpoint_key="different_endpoint",
            request_fingerprint="request-fingerprint-2",
            response_sha256="b" * 64,
            target_identity={"account_id": "brand-account-1", "handle": "brandco"},
            linkage_evidence={"note": "different page"},
        ),
    )

    assert semantic_fingerprint(first) == semantic_fingerprint(second)


def test_observation_rejects_type_confused_and_unhashable_values_with_domain_error() -> None:
    with pytest.raises(SocialLabContractError):
        build_social_observation(
            platform="x",
            record_kind="post",
            external_id=["not-hashable"],  # type: ignore[arg-type]
            canonical_url="https://social.example/post-1",
            author_account_id="brand-account-1",
            author_handle="brandco",
            parent_external_id=None,
            thread_external_id=None,
            text="body",
            media_kind="text",
            published_at="2026-08-21T12:00:00Z",
            provenance=_provenance(),
            bound_brand_identity=BRAND_IDENTITY,
        )

    with pytest.raises(SocialLabContractError):
        MetricContext(metrics={"likes": True}, observed_at="2026-08-22T00:00:00Z")


def test_provenance_requires_sha256_and_does_not_leak_raw_type_errors() -> None:
    with pytest.raises(SocialLabContractError):
        replace(_provenance(), response_sha256="not-a-sha")

    with pytest.raises(SocialLabContractError):
        Provenance(
            provider="scrapecreators",
            endpoint_key="brand_posts",
            target_fingerprint={"not": "hashable"},  # type: ignore[arg-type]
            manifest_fingerprint="manifest-fingerprint-1",
            request_fingerprint="request-fingerprint-1",
            response_sha256="a" * 64,
            fetched_at="2026-08-22T08:00:00Z",
            page=1,
            cursor=None,
            result_ordinal=0,
            target_identity=BRAND_IDENTITY,
            normalization_version="social-lab-normalization-v1",
            linkage_evidence={},
        )


def test_observation_is_frozen_and_content_id_is_derived() -> None:
    observation = _post()

    with pytest.raises((AttributeError, TypeError)):
        observation.text = "mutated"  # type: ignore[misc]

    assert observation.content_id
    assert observation.semantic_fingerprint
    assert observation.content_id.endswith(observation.semantic_fingerprint)


def test_replay_recomputes_role_and_rejects_forged_community_to_official_claim() -> None:
    payload = _post().to_dict()
    payload["actor_role"] = ActorRole.COMMUNITY_RESPONSE.value
    payload.pop("content_id")
    payload.pop("semantic_fingerprint")

    with pytest.raises(SocialLabContractError, match="actor_role"):
        SocialObservation.from_dict(payload)


def test_replay_recomputes_role_and_rejects_altered_linkage() -> None:
    payload = _post().to_dict()
    payload["parent_external_id"] = "forged-parent"
    payload["thread_external_id"] = "forged-parent"
    payload.pop("content_id")
    payload.pop("semantic_fingerprint")

    with pytest.raises(SocialLabContractError, match="actor_role"):
        SocialObservation.from_dict(payload)


def test_replay_rebuilds_omitted_derived_ids_but_rejects_forged_ids() -> None:
    observation = _post()
    omitted = observation.to_dict()
    omitted.pop("content_id")
    omitted.pop("semantic_fingerprint")
    replayed = SocialObservation.from_dict(omitted)
    assert replayed.content_id == observation.content_id
    assert replayed.semantic_fingerprint == observation.semantic_fingerprint

    forged = observation.to_dict()
    forged["content_id"] = "sha256:" + "0" * 64
    with pytest.raises(SocialLabContractError, match="content_id"):
        SocialObservation.from_dict(forged)


def test_replay_rebuilds_omitted_role_from_provenance_identity() -> None:
    payload = _post().to_dict()
    payload.pop("actor_role")
    payload.pop("content_id")
    payload.pop("semantic_fingerprint")

    replayed = SocialObservation.from_dict(payload)
    assert replayed.actor_role == ActorRole.OFFICIAL_BRAND_POST.value

    payload["actor_role"] = None
    with pytest.raises(SocialLabContractError, match="actor_role"):
        SocialObservation.from_dict(payload)


def test_actor_role_domain_contains_only_fail_closed_roles() -> None:
    assert set(ACTOR_ROLES) == {
        ActorRole.OFFICIAL_BRAND_POST.value,
        ActorRole.COMMUNITY_RESPONSE.value,
        ActorRole.BRAND_REPLY.value,
        ActorRole.UNCLASSIFIED.value,
    }
