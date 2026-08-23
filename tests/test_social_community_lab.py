from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from src.research.social_community_lab import (
    ANALYSIS_SECTIONS,
    SocialCommunityAnalyzer,
    SocialCommunityMalformedInputError,
    SocialCommunityValidationError,
    analyze_social_community,
    analysis_response_schema,
    build_prompt_packet,
)
from src.research.social_lab_contracts import ActorRole, MetricContext, SocialLabContractError, SocialObservation


FIXTURE_DIR = Path("fixtures/social_lab")


def _observation(name: str) -> SocialObservation:
    return SocialObservation.from_dict(json.loads((FIXTURE_DIR / name).read_text()))


def _observations() -> list[SocialObservation]:
    return [_observation("official_post.json"), _observation("community_response.json")]


def _as_unclassified(observation: SocialObservation) -> SocialObservation:
    payload = observation.to_dict()
    # Make the primitive record kind ambiguous so B1's canonical constructor
    # recomputes an unclassified role instead of accepting a forged role claim.
    payload["record_kind"] = "unknown"
    payload.pop("actor_role", None)
    payload.pop("content_id", None)
    payload.pop("semantic_fingerprint", None)
    return SocialObservation.from_dict(payload)


def _response(
    observations: list[SocialObservation],
    *,
    tile_candidates: list[dict[str, Any]] | None = None,
    sections: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    ids = [str(item.content_id) for item in observations]
    defaults: dict[str, list[dict[str, Any]]] = {section: [] for section in ANALYSIS_SECTIONS}
    defaults["cross_channel_voice"] = [
        {
            "statement": "The brand communicates a useful launch update.",
            "subject": "brand_claim",
            "confidence": "high",
            "citation_content_ids": [ids[0]],
            "derived_source_roles": ["official_brand_post"],
        }
    ]
    defaults["recurring_community_themes"] = [
        {
            "statement": "Community perceives practical help.",
            "subject": "community_perception",
            "confidence": "medium",
            "citation_content_ids": [ids[1]],
            "derived_source_roles": ["community_response"],
        }
    ]
    if sections:
        defaults.update(sections)
    return {
        **defaults,
        "tile_candidates": tile_candidates if tile_candidates is not None else [],
    }


def _envelope(payload: object) -> dict[str, str]:
    return {
        "analysis_json": json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    }


def test_provider_schema_is_a_tiny_analysis_json_envelope() -> None:
    assert analysis_response_schema(allowed_tile_ids={"SOC1"}) == {
        "type": "object",
        "required": ["analysis_json"],
        "properties": {"analysis_json": {"type": "string"}},
    }


def test_analysis_json_envelope_decodes_once_and_prompt_names_inner_contract() -> None:
    observations = _observations()
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> object:
        calls.append(kwargs)
        return _envelope(_response(observations))

    result = analyze_social_community(observations, fake, allowed_tile_ids={"SOC-Z", "SOC-A"})

    assert result.status == "complete"
    assert set(calls[0]["json_schema"]["properties"]) == {"analysis_json"}
    assert 'Allowed tile IDs JSON: ["SOC-A","SOC-Z"]' in calls[0]["system"]
    assert "cross_channel_voice" in calls[0]["system"]
    assert "citation_content_ids" in calls[0]["system"]


@pytest.mark.parametrize("analysis_json", ["not-json", "[]"])
def test_analysis_json_envelope_rejects_malformed_or_non_object_inner_json(analysis_json: str) -> None:
    with pytest.raises(SocialCommunityMalformedInputError, match="analysis_json"):
        analyze_social_community(_observations(), lambda **_: {"analysis_json": analysis_json})


def test_analysis_json_envelope_rejects_duplicate_inner_object_keys() -> None:
    with pytest.raises(SocialCommunityValidationError, match="duplicate object keys"):
        analyze_social_community(
            _observations(),
            lambda **_: {"analysis_json": '{"cross_channel_voice":[],"cross_channel_voice":[]}'},
        )


def test_valid_mixed_role_analysis_is_citation_bound() -> None:
    observations = _observations()
    ids = [str(item.content_id) for item in observations]
    response = _response(
        observations,
        tile_candidates=[
            {
                "tile_id": "SOC1",
                "signal": "support",
                "rationale": "The community response corroborates the direct update.",
                "citation_content_ids": ids,
                "source_roles": ["official_brand_post", "community_response"],
                "evidence_role": "mixed_tension",
            }
        ],
    )

    result = analyze_social_community(
        observations,
        lambda **_: _envelope(response),
        allowed_tile_ids={"SOC1"},
    )

    assert result.status == "complete"
    assert result["cross_channel_voice"][0]["citation_content_ids"] == [ids[0]]
    assert result.tile_candidates[0]["evidence_role"] == "mixed_tension"
    assert "score" not in json.dumps(result.to_dict()).casefold()


def test_prompt_packet_redacts_metrics_provenance_and_direct_identity() -> None:
    observations = _observations()
    packet = build_prompt_packet(observations)
    assert list(packet) == ["observations"]
    assert set(packet["observations"][0]) == {
        "content_id",
        "actor_role",
        "platform",
        "text",
        "published_at",
        "parent_thread_linkage_token",
        "record_kind",
    }
    serialized = json.dumps(packet, sort_keys=True)
    for forbidden in (
        "metric_context",
        "provenance",
        "author_account_id",
        "author_handle",
        "canonical_url",
        "engagement",
        "followers",
    ):
        assert forbidden not in serialized


def test_system_prompt_names_community_perception_boundary() -> None:
    observations = _observations()
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> object:
        calls.append(kwargs)
        return _envelope(_response(observations))

    analyze_social_community(observations, fake)
    assert "community speech is perception evidence, not a brand-owned claim" in calls[0]["system"].casefold()


def test_system_prompt_constrains_tile_ids_to_sorted_allowlist() -> None:
    observations = _observations()
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> object:
        calls.append(kwargs)
        return _envelope(_response(observations))

    analyze_social_community(observations, fake, allowed_tile_ids={"SOC-Z", "SOC-A", "SOC-M"})
    assert 'Allowed tile IDs JSON: ["SOC-A","SOC-M","SOC-Z"]' in calls[0]["system"]


def test_provider_schema_uses_only_gemini_response_format_keywords() -> None:
    schema = analysis_response_schema(allowed_tile_ids={"SOC1"})

    def all_keys(value: Any) -> list[str]:
        if isinstance(value, dict):
            keys: list[str] = []
            for key, item in value.items():
                keys.append(key)
                keys.extend(all_keys(item))
            return keys
        if isinstance(value, list):
            return [key for item in value for key in all_keys(item)]
        return []

    keys = all_keys(schema)
    assert "minLength" not in keys
    assert "uniqueItems" not in keys
    assert "additionalProperties" not in keys
    assert schema["type"] == "object"
    assert schema["required"] == ["analysis_json"]


def test_provider_schema_is_compact_transport_guidance_not_semantic_authority() -> None:
    schema = analysis_response_schema(allowed_tile_ids={"SOC1"})
    serialized = json.dumps(schema, separators=(",", ":"), sort_keys=True)

    assert len(serialized.encode("utf-8")) <= 128
    assert "additionalProperties" not in serialized
    assert "minItems" not in serialized


def test_provider_schema_relaxation_preserves_nonempty_and_duplicate_domain_guards() -> None:
    observations = _observations()
    ids = [str(item.content_id) for item in observations]
    schema = analysis_response_schema(allowed_tile_ids={"SOC1"})

    invalid_empty = _response(observations)
    invalid_empty["cross_channel_voice"][0]["statement"] = ""
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, lambda **_: _envelope(invalid_empty), allowed_tile_ids={"SOC1"})

    invalid_duplicate = _response(observations)
    invalid_duplicate["cross_channel_voice"][0]["citation_content_ids"] = [ids[0], ids[0]]
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, lambda **_: _envelope(invalid_duplicate), allowed_tile_ids={"SOC1"})

    assert "minLength" not in json.dumps(schema)
    assert "uniqueItems" not in json.dumps(schema)


@pytest.mark.parametrize("allowlist", [None, set()])
def test_system_prompt_disallows_candidates_without_allowlist(
    allowlist: set[str] | None,
) -> None:
    observations = _observations()
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> object:
        calls.append(kwargs)
        return _envelope(_response(observations))

    analyze_social_community(observations, fake, allowed_tile_ids=allowlist)
    assert "Allowed tile IDs JSON: []." in calls[0]["system"]


def test_candidate_without_allowlist_still_fails_post_validation() -> None:
    observations = _observations()
    ids = [str(item.content_id) for item in observations]
    candidate = {
        "tile_id": "MODEL_INVENTED",
        "signal": "support",
        "rationale": "A cited advisory signal.",
        "citation_content_ids": [ids[0]],
        "source_roles": ["official_brand_post"],
        "evidence_role": "brand_direct",
    }
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> object:
        calls.append(kwargs)
        return _envelope(_response(observations, tile_candidates=[candidate]))

    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, fake)
    assert "Allowed tile IDs JSON: []." in calls[0]["system"]


def test_unclassified_observations_are_excluded_and_reported() -> None:
    observations = _observations()
    unclassified = _as_unclassified(observations[0])
    calls: list[object] = []

    def fake(**_: Any) -> object:
        calls.append(True)
        response = _response(observations)
        response["cross_channel_voice"] = []
        return _envelope(response)

    result = analyze_social_community([unclassified, observations[1]], fake)
    assert result.status == "complete"
    assert result["unclassified_observation_count"] == 1
    assert "unclassified_observations_excluded" in result["limitations"]
    assert len(calls) == 1
    prompt_ids = {row["content_id"] for row in result["prompt_packet"]["observations"]}
    assert str(unclassified.content_id) not in prompt_ids


def test_empty_and_unclassified_only_evidence_never_call_analyst() -> None:
    calls: list[object] = []

    def fake(**_: Any) -> object:
        calls.append(True)
        return {}

    empty = analyze_social_community([], fake)
    assert empty.status == "unavailable"
    assert empty["reason"] == "empty_evidence"

    observation = _observations()[0]
    only_unclassified = analyze_social_community(
        [_as_unclassified(observation)],
        fake,
    )
    assert only_unclassified.status == "limited"
    assert only_unclassified["reason"] == "no_admitted_observations"
    assert calls == []


@pytest.mark.parametrize(
    ("subject", "citations", "roles"),
    [
        ("brand_claim", [1], ["community_response"]),
        ("brand_behavior", [0], ["official_brand_post"]),
        ("community_perception", [0], ["official_brand_post"]),
        ("tension", [0], ["official_brand_post"]),
    ],
)
def test_invalid_conclusion_role_evidence_fails_closed(
    subject: str,
    citations: list[int],
    roles: list[str],
) -> None:
    observations = _observations()
    ids = [str(item.content_id) for item in observations]
    conclusion = {
        "statement": "An asserted conclusion.",
        "subject": subject,
        "confidence": "medium",
        "citation_content_ids": [ids[index] for index in citations],
        "derived_source_roles": roles,
    }
    sections = {section: [] for section in ANALYSIS_SECTIONS}
    sections["cross_channel_voice"] = [conclusion]
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, lambda **_: _envelope(_response(observations, sections=sections)))


def test_unknown_duplicate_citations_and_score_fields_fail_closed() -> None:
    observations = _observations()
    ids = [str(item.content_id) for item in observations]
    invalid = _response(observations)
    invalid["cross_channel_voice"][0]["citation_content_ids"] = [ids[0], ids[0]]
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, lambda **_: _envelope(invalid))

    invalid = _response(observations)
    invalid["cross_channel_voice"][0]["citation_content_ids"] = ["sha256:" + "0" * 64]
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, lambda **_: _envelope(invalid))

    invalid = _response(observations)
    invalid["score"] = 7
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, lambda **_: _envelope(invalid))


def test_candidate_requires_caller_tile_allowlist_and_has_no_scoring_surface() -> None:
    observations = _observations()
    ids = [str(item.content_id) for item in observations]
    candidate = {
        "tile_id": "SOC1",
        "signal": "support",
        "rationale": "A cited advisory signal.",
        "citation_content_ids": [ids[0]],
        "source_roles": ["official_brand_post"],
        "evidence_role": "brand_direct",
    }
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(
            observations, lambda **_: _envelope(_response(observations, tile_candidates=[candidate]))
        )

    candidate["score"] = 1
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(
            observations,
            lambda **_: _envelope(_response(observations, tile_candidates=[candidate])),
            allowed_tile_ids={"SOC1"},
        )

    candidate.pop("score")
    candidate["tile_id"] = "SV9_UNKNOWN"
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(
            observations,
            lambda **_: _envelope(_response(observations, tile_candidates=[candidate])),
            allowed_tile_ids={"SOC1"},
        )


def test_metric_and_provenance_changes_do_not_change_prompt_or_analysis() -> None:
    observations = _observations()
    changed = [
        replace(
            observations[0],
            metric_context=MetricContext(
                metrics={"likes": 99999, "followers": 500000}, observed_at="2026-09-01T00:00:00Z"
            ),
            provenance=replace(
                observations[0].provenance, request_fingerprint="request-fingerprint-2", response_sha256="c" * 64
            ),
        ),
        replace(
            observations[1],
            provenance=replace(observations[1].provenance, endpoint_key="different_endpoint", response_sha256="d" * 64),
        ),
    ]
    prompts: list[str] = []

    def fake(**kwargs: Any) -> object:
        prompts.append(kwargs["user"])
        # Echo only the citation-bound fixture output; this fake is intentionally
        # independent of all acquisition/metric fields.
        return _envelope(_response(observations))

    first = analyze_social_community(observations, fake)
    second = analyze_social_community(changed, fake)
    assert prompts[0] == prompts[1]
    assert first["cross_channel_voice"] == second["cross_channel_voice"]
    assert first["tile_candidates"] == second["tile_candidates"]


def test_provider_attempts_are_bounded_and_failures_are_unavailable() -> None:
    observations = _observations()
    calls = 0

    def failing(**_: Any) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider failure with secret=do-not-leak")

    analyzer = SocialCommunityAnalyzer(failing)
    result = analyzer.analyze(observations)
    assert result.status == "unavailable"
    assert result["reason"] == "analyst_attempts_exhausted"
    assert result["attempt_count"] == 1
    assert calls == 1
    assert "do-not-leak" not in json.dumps(result)


def test_analysis_has_no_public_retry_configuration() -> None:
    observations = _observations()

    def fake(**_: Any) -> object:
        return _envelope(_response(observations))

    with pytest.raises(TypeError):
        SocialCommunityAnalyzer(fake, max_attempts=2)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        analyze_social_community(observations, fake, max_attempts=2)  # type: ignore[call-arg]


def test_identity_leakage_in_semantic_prose_is_rejected() -> None:
    observations = _observations()
    invalid = _response(observations)
    invalid["cross_channel_voice"][0]["statement"] = "@brandco says this."
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, lambda **_: _envelope(invalid))


def test_mapping_equivalent_observations_are_decoded_strictly() -> None:
    observations = _observations()
    payloads = [item.to_dict() for item in observations]
    result = analyze_social_community(payloads, lambda **_: _envelope(_response(observations)))
    assert result.status == "complete"
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(
            [{**payloads[0], "unexpected": "field"}, payloads[1]],
            lambda **_: _envelope(_response(observations)),
        )


def test_direct_forged_community_observation_cannot_support_brand_claim() -> None:
    community = _observations()[1]
    with pytest.raises(SocialLabContractError, match="does not match fail-closed role resolution"):
        SocialObservation(
            platform=community.platform,
            record_kind="post",
            external_id=community.external_id,
            canonical_url=community.canonical_url,
            author_account_id=community.author_account_id,
            author_handle=community.author_handle,
            parent_external_id=None,
            thread_external_id=None,
            text=community.text,
            media_kind=community.media_kind,
            published_at=community.published_at,
            actor_role=ActorRole.OFFICIAL_BRAND_POST,
            provenance=community.provenance,
        )
