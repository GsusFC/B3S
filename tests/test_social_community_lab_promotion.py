"""Fail-closed promotion proofs for the isolated social/community lab."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from src.research.scrapecreators_spike import parse_target_manifest
from src.research.social_community_lab import (
    ANALYSIS_SECTIONS,
    SocialCommunityValidationError,
    analyze_social_community,
)
from src.research.social_lab_contracts import MetricContext, SocialObservation


ROOT = Path(__file__).parents[1]
EXPECTED_GATE_IDS = [
    "role_attribution_accuracy",
    "representative_interaction_coverage",
    "citation_traceability",
    "semantic_id_stability",
    "metric_invariance",
    "community_claim_boundary",
    "strategist_incremental_value",
    "negative_control_precision",
    "cost_latency_rate_limits",
    "privacy_retention_terms",
    "canonical_invariance",
]
EXPECTED_REQUIREMENTS = {
    "role_attribution_accuracy": "Validate actor-role assignment against independent adjudication.",
    "representative_interaction_coverage": "Show representative interaction coverage across supported channels.",
    "citation_traceability": "Trace every analysis conclusion to admitted observation citations.",
    "semantic_id_stability": "Confirm equivalent observations retain stable semantic IDs.",
    "metric_invariance": "Change metrics without changing semantic identity.",
    "community_claim_boundary": "Confirm community speech is not treated as a brand-owned claim.",
    "strategist_incremental_value": "Demonstrate analyst output adds value beyond existing evidence.",
    "negative_control_precision": "Measure false-positive behavior on negative controls.",
    "cost_latency_rate_limits": "Measure bounded cost, latency, retries, and rate-limit behavior.",
    "privacy_retention_terms": "Approve privacy, retention, and provider-terms handling.",
    "canonical_invariance": "Prove canonical state and persistence remain unchanged.",
}


def _load_cli() -> Any:
    path = ROOT / "scripts/run_social_community_lab.py"
    spec = importlib.util.spec_from_file_location("social_community_lab_promotion_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(*, platform: str = "twitter") -> Any:
    target: dict[str, str] = {"target_id": "brand-x", "platform": platform}
    if platform == "linkedin":
        target["company_url"] = "https://www.linkedin.com/company/brandco"
    else:
        target["handle"] = "brandco"
    return parse_target_manifest({"schema_version": 1, "targets": [target]})


def _raw_observations() -> tuple[dict[str, Any], dict[str, Any]]:
    fixture_dir = ROOT / "fixtures/social_lab"
    return (
        json.loads((fixture_dir / "official_post.json").read_text(encoding="utf-8")),
        json.loads((fixture_dir / "community_response.json").read_text(encoding="utf-8")),
    )


def _observations() -> list[SocialObservation]:
    official, community = _raw_observations()
    return [SocialObservation.from_dict(official), SocialObservation.from_dict(community)]


def _brand_reply(observation: SocialObservation) -> SocialObservation:
    payload = observation.to_dict()
    payload.update(
        {
            "record_kind": "reply",
            "external_id": "reply-1",
            "canonical_url": "https://social.example/brandco/status/reply-1",
            "parent_external_id": observation.external_id,
            "thread_external_id": observation.external_id,
            "author_account_id": "brand-account-1",
            "author_handle": "brandco",
            "text": "We are glad this helped your team.",
            "actor_role": "brand_reply",
        }
    )
    payload.pop("content_id", None)
    payload.pop("semantic_fingerprint", None)
    payload["provenance"] = {
        **observation.provenance.to_dict(),
        "endpoint_key": "brand_replies",
        "response_sha256": "e" * 64,
    }
    return SocialObservation.from_dict(payload)


def _acquisition(observations: list[SocialObservation] | None = None) -> dict[str, Any]:
    if observations is None:
        official, community = _raw_observations()
        rows: list[Any] = [official, community]
    else:
        rows = [observation.to_dict() for observation in observations]
    return {
        "schema_version": "scrapecreators-social-spike.v1",
        "contract_version": "b3s-social-community-lab-v1",
        "observations": rows,
        "responses": [],
        "summary": {"target_count": 1, "post_count": 1, "interaction_count": max(len(rows) - 1, 0)},
    }


def _response(
    *,
    observations: list[SocialObservation],
    subject: str | None = None,
    citation_ids: list[str] | None = None,
    roles: list[str] | None = None,
    tile_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sections: dict[str, list[dict[str, Any]]] = {section: [] for section in ANALYSIS_SECTIONS}
    if subject is not None:
        sections["cross_channel_voice"] = [
            {
                "statement": "The evidence supports an advisory observation.",
                "subject": subject,
                "confidence": "medium",
                "citation_content_ids": citation_ids or [str(observations[0].content_id)],
                "derived_source_roles": roles or [observations[0].actor_role],
            }
        ]
    return {**sections, "tile_candidates": tile_candidates or []}


def _community_response(observations: list[SocialObservation]) -> dict[str, Any]:
    community = observations[1]
    return _response(
        observations=observations,
        subject="community_perception",
        citation_ids=[str(community.content_id)],
        roles=["community_response"],
    )


def _analysis_envelope(payload: object) -> dict[str, str]:
    return {
        "analysis_json": json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    }


def test_promotion_checklist_is_exactly_ordered_and_remains_insufficient() -> None:
    cli = _load_cli()
    artifact = cli.compose_lab_artifact(_manifest(), _acquisition())
    promotion = artifact["promotion_evidence"]
    assert promotion["status"] == "insufficient"
    gates = promotion["gates"]
    assert [gate["id"] for gate in gates] == EXPECTED_GATE_IDS
    assert [gate["requirement"] for gate in gates] == [EXPECTED_REQUIREMENTS[gate_id] for gate_id in EXPECTED_GATE_IDS]
    assert all(gate["status"] == "not_checked" for gate in gates)
    assert all(gate["evidence"] == [] for gate in gates)
    assert artifact["canonical_invariance"] == {
        "status": "not_checked",
        "reason": "independent_canonical_invariance_gate_required",
    }


def test_failed_analysis_state_keeps_promotion_unchecked() -> None:
    cli = _load_cli()
    observations = _observations()
    artifact = cli.compose_lab_artifact(
        _manifest(),
        _acquisition(observations),
        mode="live_analysis",
        analysis_artifact=cli._analysis_attempts_exhausted(observations),
        analysis_options={"mode": "live_analysis", "attempt_policy": "one_shot"},
    )
    artifact["analysis_failure"] = cli._analysis_failure_metadata("gemini_domain_validation_failed", attempt_count=1)

    assert artifact["community_analysis"]["status"] == "unavailable"
    assert artifact["community_analysis"]["reason"] == "analyst_attempts_exhausted"
    assert artifact["analysis_failure"]["status"] == "failed"
    assert artifact["analysis_failure"]["claims_available"] is False
    assert artifact["promotion_evidence"]["status"] == "insufficient"
    assert all(
        gate["status"] == "not_checked" and gate["evidence"] == [] for gate in artifact["promotion_evidence"]["gates"]
    )
    assert artifact["canonical_invariance"]["status"] == "not_checked"


def test_acquisition_and_caller_payloads_cannot_self_certify_promotion() -> None:
    cli = _load_cli()
    acquisition = _acquisition()
    acquisition["promotion_evidence"] = {
        "status": "allow",
        "gates": [{"id": gate_id, "status": "passed", "evidence": ["caller"]} for gate_id in EXPECTED_GATE_IDS],
    }
    acquisition["canonical_invariance"] = {"status": "verified", "evidence": ["caller"]}
    acquisition["responses"] = [
        {
            "endpoint_key": "provider_payload",
            "promotion_evidence": {"status": "allow", "gates": [{"status": "passed"}]},
        }
    ]
    artifact = cli.compose_lab_artifact(_manifest(), acquisition)
    assert artifact["promotion_evidence"]["status"] == "insufficient"
    assert all(
        gate["status"] == "not_checked" and gate["evidence"] == [] for gate in artifact["promotion_evidence"]["gates"]
    )
    assert artifact["canonical_invariance"]["status"] == "not_checked"
    assert artifact["canonical_invariance"]["reason"] == "independent_canonical_invariance_gate_required"


def test_metric_only_and_provenance_only_changes_preserve_prompt_semantics_and_candidates() -> None:
    cli = _load_cli()
    original = _observations()
    changed = [
        replace(
            original[0],
            metric_context=MetricContext(
                metrics={"likes": 99999, "followers": 500000}, observed_at="2026-09-01T00:00:00Z"
            ),
            provenance=replace(original[0].provenance, request_fingerprint="changed-request", response_sha256="c" * 64),
        ),
        replace(original[1], provenance=replace(original[1].provenance, endpoint_key="changed-endpoint")),
    ]
    candidate = {
        "tile_id": "SOCIAL-ADVISORY",
        "signal": "support",
        "rationale": "Community evidence supports the observed advisory signal.",
        "citation_content_ids": [str(original[1].content_id)],
        "source_roles": ["community_response"],
        "evidence_role": "community_corroboration",
    }
    response = _community_response(original)
    response["tile_candidates"] = [candidate]
    first = cli.compose_lab_artifact(
        _manifest(),
        _acquisition(original),
        mode="offline_analysis",
        analyst_response=_analysis_envelope(response),
        allowed_tile_ids=["SOCIAL-ADVISORY"],
    )
    second = cli.compose_lab_artifact(
        _manifest(),
        _acquisition(changed),
        mode="offline_analysis",
        analyst_response=_analysis_envelope(response),
        allowed_tile_ids=["SOCIAL-ADVISORY"],
    )
    first_analysis = first["community_analysis"]
    second_analysis = second["community_analysis"]
    assert first_analysis["prompt_packet"] == second_analysis["prompt_packet"]
    assert json.dumps(first_analysis, sort_keys=True, separators=(",", ":")) == json.dumps(
        second_analysis, sort_keys=True, separators=(",", ":")
    )
    assert first["advisory_tile_candidates"] == second["advisory_tile_candidates"]


@pytest.mark.parametrize(
    ("subject", "citation_indexes", "roles"),
    [
        ("brand_claim", [1], ["community_response"]),
        ("brand_behavior", [0], ["official_brand_post"]),
        ("tension", [0], ["official_brand_post"]),
    ],
)
def test_role_boundaries_fail_closed_without_required_evidence(
    subject: str, citation_indexes: list[int], roles: list[str]
) -> None:
    observations = _observations()
    ids = [str(observation.content_id) for observation in observations]
    response = _response(
        observations=observations,
        subject=subject,
        citation_ids=[ids[index] for index in citation_indexes],
        roles=roles,
    )
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, lambda **_: _analysis_envelope(response))


def test_brand_behavior_is_admitted_only_when_a_verified_brand_reply_exists() -> None:
    observations = _observations()
    reply = _brand_reply(observations[0])
    response = _response(
        observations=[*observations, reply],
        subject="brand_behavior",
        citation_ids=[str(reply.content_id)],
        roles=["brand_reply"],
    )
    result = analyze_social_community([*observations, reply], lambda **_: _analysis_envelope(response))
    assert result.status == "complete"
    assert result["cross_channel_voice"][0]["subject"] == "brand_behavior"


@pytest.mark.parametrize("mutation", ["unknown_citation", "duplicate_citation", "unsupported_tile"])
def test_unknown_duplicate_citations_and_unsupported_tiles_fail_closed(mutation: str) -> None:
    observations = _observations()
    response = _community_response(observations)
    community_id = str(observations[1].content_id)
    conclusion = response["cross_channel_voice"][0]
    if mutation == "unknown_citation":
        conclusion["citation_content_ids"] = ["sha256:" + "0" * 64]
    elif mutation == "duplicate_citation":
        conclusion["citation_content_ids"] = [community_id, community_id]
    else:
        response["tile_candidates"] = [
            {
                "tile_id": "not-allowed",
                "signal": "support",
                "rationale": "Community evidence supports the advisory signal.",
                "citation_content_ids": [community_id],
                "source_roles": ["community_response"],
                "evidence_role": "community_corroboration",
            }
        ]
    kwargs = {"allowed_tile_ids": {"allowed-only"}} if mutation == "unsupported_tile" else {}
    with pytest.raises(SocialCommunityValidationError):
        analyze_social_community(observations, lambda **_: _analysis_envelope(response), **kwargs)


def test_unsupported_x_interactions_are_explicitly_not_acquired() -> None:
    cli = _load_cli()
    artifact = cli.compose_lab_artifact(
        _manifest(platform="twitter"),
        {"observations": [], "responses": [], "summary": {}},
    )
    matrix = artifact["acquisition_matrix"]["brand-x"]
    for role in ("community_response", "brand_reply"):
        assert matrix[role]["status"] == "not_acquired"
        assert matrix[role]["reason"] == "no_verified_reply_tree_endpoint"


def test_replayed_x_interaction_capabilities_cannot_override_fail_closed_matrix() -> None:
    cli = _load_cli()
    acquisition = _acquisition()
    acquisition["acquisition_matrix"] = {
        "brand-x": {
            "community_response": {
                "status": "acquired",
                "reason": "verified_reply_tree",
                "provenance": [{"endpoint_key": "twitter_replies"}],
            },
            "brand_reply": {
                "status": "acquired",
                "reason": "provider_claimed_brand_reply",
                "provenance": [{"endpoint_key": "twitter_brand_replies"}],
            },
            "official_brand_post": {
                "status": "acquired",
                "reason": "verified_posts",
                "provenance": [{"endpoint_key": "twitter_posts"}],
            },
        }
    }

    artifact = cli.compose_lab_artifact(_manifest(platform="twitter"), acquisition)
    matrix = artifact["acquisition_matrix"]["brand-x"]
    for role in ("community_response", "brand_reply"):
        assert matrix[role]["status"] == "not_acquired"
        assert matrix[role]["reason"] == "no_verified_reply_tree_endpoint"
        assert matrix[role]["provenance"] == []
    assert matrix["official_brand_post"]["status"] == "acquired"


def test_external_evidence_slots_cannot_be_self_certified_by_the_runner() -> None:
    cli = _load_cli()
    artifact = cli.compose_lab_artifact(_manifest(), _acquisition())
    gates = {gate["id"]: gate for gate in artifact["promotion_evidence"]["gates"]}
    externally_proven = {
        "strategist_incremental_value",
        "representative_interaction_coverage",
        "privacy_retention_terms",
        "cost_latency_rate_limits",
        "canonical_invariance",
    }
    for gate_id in externally_proven:
        assert gates[gate_id]["status"] == "not_checked"
        assert gates[gate_id]["evidence"] == []
        assert gates[gate_id]["requirement"] == EXPECTED_REQUIREMENTS[gate_id]
    assert artifact["promotion_evidence"]["status"] == "insufficient"
    assert artifact["canonical_invariance"]["status"] == "not_checked"
