from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from src.research.scrapecreators_spike import parse_target_manifest
from src.features import llm_analyzer as llm_analyzer_module


def _load_cli():
    path = Path(__file__).parents[1] / "scripts" / "run_social_community_lab.py"
    spec = importlib.util.spec_from_file_location("social_community_lab_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _test_output_path(cli: object, tmp_path: Path, name: str) -> Path:
    root = tmp_path / "out" / "social-community-lab"
    setattr(cli, "OUTPUT_ROOT", root)
    return root / name


def _manifest():
    return parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [
                {"target_id": "brand-x", "platform": "twitter", "handle": "brandco"},
            ],
        }
    )


def _observations() -> tuple[dict, dict]:
    root = Path(__file__).parents[1] / "fixtures" / "social_lab"
    official = json.loads((root / "official_post.json").read_text(encoding="utf-8"))
    community = json.loads((root / "community_response.json").read_text(encoding="utf-8"))
    return official, community


def _acquisition() -> dict:
    official, community = _observations()
    matrix = {
        "brand-x": {
            role: {
                "target_id": "brand-x",
                "platform": "twitter",
                "role": role,
                "status": "not_acquired",
                "reason": "interaction_acquisition_not_enabled",
                "provenance": [],
            }
            for role in (
                "official_brand_post",
                "community_response",
                "brand_reply",
                "unclassified",
            )
        }
    }
    matrix["brand-x"]["official_brand_post"]["status"] = "acquired"
    return {
        "schema_version": "scrapecreators-social-spike.v1",
        "contract_version": "b3s-social-community-lab-v1",
        "manifest_sha256": "sha256:" + "1" * 64,
        "acquisition_matrix": matrix,
        "observations": [official, community],
        "responses": [
            {
                "endpoint_key": "twitter_user_tweets",
                "outcome": "succeeded",
                "raw_response_sha256": "2" * 64,
                "raw_payload_redacted": {"x-api-key": "[REDACTED]"},
            }
        ],
        "summary": {"target_count": 1, "post_count": 1, "interaction_count": 1},
    }


def _analyst_response() -> dict:
    _official, community = _observations()
    return {
        "cross_channel_voice": [],
        "recurring_community_themes": [
            {
                "statement": "Community members describe the outcome as useful.",
                "subject": "community_perception",
                "confidence": "medium",
                "citation_content_ids": [community["content_id"]],
                "derived_source_roles": ["community_response"],
            }
        ],
        "response_behavior": [],
        "corroborations": [],
        "tensions": [],
        "tile_candidates": [
            {
                "tile_id": "tile-community",
                "signal": "support",
                "rationale": "Community evidence supports the observed claim.",
                "citation_content_ids": [community["content_id"]],
                "source_roles": ["community_response"],
                "evidence_role": "community_corroboration",
            }
        ],
    }


def _analysis_envelope(payload: object | None = None) -> dict[str, str]:
    return {
        "analysis_json": json.dumps(
            _analyst_response() if payload is None else payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    }


def test_compose_artifact_keeps_roles_metrics_and_analysis_separate() -> None:
    cli = _load_cli()
    response = _analyst_response()
    acquisition = _acquisition()
    # Caller/provider payloads cannot pre-authorize promotion gates.
    acquisition["promotion_evidence"] = {
        "status": "allow",
        "gates": [{"id": "role_attribution_accuracy", "status": "passed"}],
    }
    artifact = cli.compose_lab_artifact(
        _manifest(),
        acquisition,
        mode="offline_analysis",
        allowed_tile_ids=["tile-community"],
        analyst_response=_analysis_envelope(response),
        analyst_response_hash="sha256:" + hashlib.sha256(b"recorded").hexdigest(),
    )

    assert artifact["schema_version"] == "b3s-social-community-lab-v1"
    assert set(
        (
            "run_identity",
            "acquisition_matrix",
            "role_coverage",
            "observations_by_role",
            "community_analysis",
            "advisory_tile_candidates",
            "metric_context",
            "promotion_evidence",
            "limitations",
            "canonical_invariance",
        )
    ) <= set(artifact)
    assert artifact["role_coverage"]["official_brand_post"]["count"] == 1
    assert artifact["role_coverage"]["community_response"]["count"] == 1
    assert artifact["community_analysis"]["status"] == "complete"
    assert artifact["advisory_tile_candidates"][0]["tile_id"] == "tile-community"
    assert artifact["metric_context"]
    assert all("metric_context" not in row for rows in artifact["observations_by_role"].values() for row in rows)
    assert artifact["promotion_evidence"]["status"] == "insufficient"
    expected_gate_ids = [
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
    gates = artifact["promotion_evidence"]["gates"]
    assert [gate["id"] for gate in gates] == expected_gate_ids
    assert all(gate["status"] == "not_checked" and gate["evidence"] == [] and gate["requirement"] for gate in gates)
    assert artifact["canonical_invariance"]["status"] == "not_checked"
    serialized = json.dumps(artifact)
    assert "score" not in serialized.casefold()
    assert "activation" not in serialized.casefold()


def test_compose_artifact_identity_excludes_output_path_and_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli()
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", "secret-social-key")
    acquisition = _acquisition()
    acquisition["responses"][0]["raw_payload_redacted"] = {
        "token": "old-token-not-in-environment",
        "SECRET": "old-secret-not-in-environment",
        "client_secret": "old-client-secret-not-in-environment",
        "password": "old-password-not-in-environment",
        "bearer": "old-bearer-not-in-environment",
        "cookie": "old-cookie-not-in-environment",
        "set-cookie": "old-set-cookie-not-in-environment",
    }
    first = cli.compose_lab_artifact(
        _manifest(),
        acquisition,
        mode="acquisition_only",
        output_path="/tmp/one.json",
    )
    second = cli.compose_lab_artifact(
        _manifest(),
        acquisition,
        mode="acquisition_only",
        output_path="/tmp/two.json",
    )
    assert first["run_identity"] == second["run_identity"]
    serialized = json.dumps(first)
    for secret in (
        "old-token-not-in-environment",
        "old-secret-not-in-environment",
        "old-client-secret-not-in-environment",
        "old-password-not-in-environment",
        "old-bearer-not-in-environment",
        "old-cookie-not-in-environment",
        "old-set-cookie-not-in-environment",
    ):
        assert secret not in serialized
    raw = first["provider_responses"][0]["raw_payload_redacted"]
    assert set(raw.values()) == {"[REDACTED]"}


def test_compose_artifact_rejects_removed_retry_configuration() -> None:
    cli = _load_cli()
    with pytest.raises(cli.SocialCommunityLabCLIError, match="one-shot"):
        cli.compose_lab_artifact(_manifest(), _acquisition(), analysis_options={"max_attempts": 2})


def test_compose_artifact_revalidates_prebuilt_analysis_against_observations_and_allowlist() -> None:
    cli = _load_cli()
    acquisition = _acquisition()
    valid = cli.compose_lab_artifact(
        _manifest(),
        acquisition,
        mode="offline_analysis",
        allowed_tile_ids=["tile-community"],
        analyst_response=_analysis_envelope(),
    )["community_analysis"]

    replayed = cli.compose_lab_artifact(
        _manifest(),
        acquisition,
        mode="offline_analysis",
        allowed_tile_ids=["tile-community"],
        analysis_artifact=valid,
    )
    assert replayed["community_analysis"]["status"] == "complete"
    assert replayed["community_analysis"]["tile_candidates"] == valid["tile_candidates"]

    bad_cases = []
    bad = json.loads(json.dumps(valid))
    bad["score"] = 9
    bad_cases.append(bad)

    bad = json.loads(json.dumps(valid))
    bad["recurring_community_themes"][0]["citation_content_ids"] = ["sha256:" + "0" * 64]
    bad_cases.append(bad)

    bad = json.loads(json.dumps(valid))
    bad["recurring_community_themes"][0]["derived_source_roles"] = ["official_brand_post"]
    bad_cases.append(bad)

    bad = json.loads(json.dumps(valid))
    bad["state"] = "active"
    bad_cases.append(bad)

    bad = json.loads(json.dumps(valid))
    bad["status"] = "partial"
    bad_cases.append(bad)

    bad = json.loads(json.dumps(valid))
    bad["tensions"] = {"malformed": True}
    bad_cases.append(bad)

    bad = json.loads(json.dumps(valid))
    bad["tile_candidates"][0]["tile_id"] = "not-allowed"
    bad_cases.append(bad)

    for forged in bad_cases:
        with pytest.raises(cli.SocialCommunityLabCLIError):
            cli.compose_lab_artifact(
                _manifest(),
                acquisition,
                mode="offline_analysis",
                allowed_tile_ids=["tile-community"],
                analysis_artifact=forged,
            )


def test_cli_dry_run_never_requires_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli()
    monkeypatch.delenv("SCRAPECREATORS_API_KEY", raising=False)

    def fail_secret_reader() -> tuple[str, ...]:
        raise AssertionError("dry-run must not read credential environment variables")

    def fail_provider_constructor(*_args, **_kwargs):
        raise AssertionError("dry-run must not construct a provider client")

    monkeypatch.setattr(cli, "_secret_values", fail_secret_reader)
    monkeypatch.setattr(cli, "ScrapeCreatorsClient", fail_provider_constructor)
    manifest_path = tmp_path / "targets.json"
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    output_path = _test_output_path(cli, tmp_path, "nested/plan.json")

    assert cli.main(["--manifest", str(manifest_path), "--dry-run", "--output", str(output_path)]) == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry_run"
    assert payload["request_plan"]
    assert os.stat(output_path).st_mode & 0o777 == 0o600


def test_live_analysis_uses_one_uncached_gemini_native_call_and_secure_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    calls: list[dict] = []
    generic_calls: list[dict] = []

    class FakeLLMAnalyzer:
        def __init__(self) -> None:
            self.use_cache = True
            self.base_url = "https://generativelanguage.googleapis.com/v1beta/openai"

        def _call_json(self, **kwargs):
            generic_calls.append(kwargs)
            raise AssertionError("live analysis must not use the generic schema fallback")

        def _call_json_gemini_native(self, **kwargs):
            assert self.use_cache is False
            calls.append(kwargs)
            return _analysis_envelope()

    monkeypatch.setenv("GEMINI_API_KEY", "live-gemini-key")
    monkeypatch.setattr(llm_analyzer_module, "LLMAnalyzer", FakeLLMAnalyzer)
    monkeypatch.chdir(tmp_path)
    manifest_path = tmp_path / "targets.json"
    acquisition_path = tmp_path / "acquisition.json"
    output_path = _test_output_path(cli, tmp_path, "live.json")
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    acquisition_path.write_text(json.dumps(_acquisition()), encoding="utf-8")

    assert (
        cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--acquisition-result",
                str(acquisition_path),
                "--live-analysis",
                "--allowed-tile-ids",
                '["tile-community"]',
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "live_analysis"
    assert payload["community_analysis"]["status"] == "complete"
    assert len(calls) == 1
    assert generic_calls == []
    assert os.stat(output_path).st_mode & 0o777 == 0o600
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_live_analysis_domain_failure_preserves_acquisition_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed live analysis must not discard already-paid acquisition evidence."""

    cli = _load_cli()
    acquisition = _acquisition()
    acquisition["summary"].update(
        {
            "successful_request_count": 1,
            "failed_request_count": 0,
            "credits_charged": 7,
        }
    )
    acquisition["responses"][0].update({"status_code": 200, "attempts": 1, "credits_charged": 7})

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_run_acquisition(*_args, **_kwargs):
        return acquisition

    calls: list[dict] = []

    class FailingGeminiAnalyzer:
        def __init__(self) -> None:
            self.use_cache = True
            self.base_url = "https://generativelanguage.googleapis.com/v1beta/openai"

        def _call_json_gemini_native(self, **_kwargs):
            calls.append({})
            invalid = _analyst_response()
            invalid["tile_candidates"][0]["tile_id"] = "forged-tile"
            return invalid

    monkeypatch.setattr(cli, "ScrapeCreatorsClient", FakeClient)
    monkeypatch.setattr(cli, "run_acquisition", fake_run_acquisition)
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", "paid-acquisition-key")
    monkeypatch.setenv("GEMINI_API_KEY", "live-gemini-key")
    monkeypatch.setattr(llm_analyzer_module, "LLMAnalyzer", FailingGeminiAnalyzer)

    monkeypatch.chdir(tmp_path)
    manifest_path = tmp_path / "targets.json"
    output_path = _test_output_path(cli, tmp_path, "failed-live.json")
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")

    assert (
        cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--live-analysis",
                "--allowed-tile-ids",
                '["tile-community"]',
                "--output",
                str(output_path),
            ]
        )
        == 2
    )

    assert output_path.exists()
    # The failure checkpoint must not be bought by an implicit retry.
    assert len(calls) == 1
    assert os.stat(output_path).st_mode & 0o777 == 0o600
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["observations_by_role"]["official_brand_post"]
    observation = payload["observations_by_role"]["official_brand_post"][0]
    assert observation["provenance"]["response_sha256"]
    assert payload["acquisition_summary"]["credits_charged"] == 7
    assert payload["provider_responses"][0]["credits_charged"] == 7
    assert payload["analysis_failure"] == {
        "status": "failed",
        "reason": "gemini_domain_validation_failed",
        "attempt_count": 1,
        "claims_available": False,
    }
    assert payload["community_analysis"]["status"] == "unavailable"
    assert payload["community_analysis"]["reason"] == "analyst_attempts_exhausted"
    assert payload["promotion_evidence"]["status"] == "insufficient"
    assert all(gate["status"] == "not_checked" for gate in payload["promotion_evidence"]["gates"])
    serialized = json.dumps(payload)
    assert "paid-acquisition-key" not in serialized
    assert "live-gemini-key" not in serialized
    assert "forged-tile" not in serialized


def test_live_analysis_preflight_bounds_failure_records_zero_provider_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core preflight rejection must not be recorded as a Gemini attempt."""

    cli = _load_cli()
    official, _community = _observations()
    oversized_observations = []
    for index in range(201):
        row = {
            **official,
            "external_id": f"post-{index}",
            "canonical_url": f"https://social.example/brandco/status/post-{index}",
        }
        row.pop("content_id", None)
        row.pop("semantic_fingerprint", None)
        oversized_observations.append(cli.SocialObservation.from_dict(row).to_dict())

    acquisition = _acquisition()
    acquisition["observations"] = oversized_observations
    native_calls: list[dict] = []

    class NeverCalledGeminiAnalyzer:
        def __init__(self) -> None:
            self.use_cache = True
            self.base_url = "https://generativelanguage.googleapis.com/v1beta/openai"

        def _call_json_gemini_native(self, **kwargs):
            native_calls.append(kwargs)
            return _analysis_envelope()

    monkeypatch.setenv("GEMINI_API_KEY", "live-gemini-key")
    monkeypatch.setattr(llm_analyzer_module, "LLMAnalyzer", NeverCalledGeminiAnalyzer)
    manifest_path = tmp_path / "targets.json"
    acquisition_path = tmp_path / "acquisition.json"
    output_path = _test_output_path(cli, tmp_path, "preflight-failure.json")
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    acquisition_path.write_text(json.dumps(acquisition), encoding="utf-8")

    assert (
        cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--acquisition-result",
                str(acquisition_path),
                "--live-analysis",
                "--output",
                str(output_path),
            ]
        )
        == 2
    )

    assert native_calls == []
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["analysis_failure"] == {
        "status": "failed",
        "reason": "gemini_domain_validation_failed",
        "attempt_count": 0,
        "claims_available": False,
    }
    assert payload["community_analysis"]["status"] == "unavailable"
    assert payload["community_analysis"]["reason"] == "analysis_preflight_failed"
    assert payload["community_analysis"]["attempt_count"] == 0
    assert payload["community_analysis"]["prompt_packet"] is None


def test_live_analysis_provider_raise_preserves_checkpoint_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()

    class RaisingGeminiAnalyzer:
        def __init__(self) -> None:
            self.use_cache = True
            self.base_url = "https://generativelanguage.googleapis.com/v1beta/openai"

        def _call_json_gemini_native(self, **_kwargs):
            raise RuntimeError("provider transport secret=must-not-persist")

    monkeypatch.setenv("GEMINI_API_KEY", "live-gemini-key")
    monkeypatch.setattr(llm_analyzer_module, "LLMAnalyzer", RaisingGeminiAnalyzer)
    manifest_path = tmp_path / "targets.json"
    acquisition_path = tmp_path / "acquisition.json"
    output_path = _test_output_path(cli, tmp_path, "provider-failure.json")
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    acquisition_path.write_text(json.dumps(_acquisition()), encoding="utf-8")

    assert (
        cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--acquisition-result",
                str(acquisition_path),
                "--live-analysis",
                "--output",
                str(output_path),
            ]
        )
        == 2
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["analysis_failure"]["status"] == "failed"
    assert payload["analysis_failure"]["reason"] == "gemini_provider_failure"
    assert payload["analysis_failure"]["attempt_count"] == 1
    assert payload["community_analysis"]["status"] == "unavailable"
    assert payload["community_analysis"]["reason"] == "analyst_attempts_exhausted"
    assert "provider transport" not in json.dumps(payload)
    assert "must-not-persist" not in json.dumps(payload)


def test_native_gemini_empty_failure_result_remains_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()

    class EmptyFailureGeminiAnalyzer:
        def __init__(self) -> None:
            self.use_cache = True
            self.base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
            self.last_failure_reason = "transport_error"

        def _call_json_gemini_native(self, **_kwargs):
            return {}

    monkeypatch.setenv("GEMINI_API_KEY", "live-gemini-key")
    monkeypatch.setattr(llm_analyzer_module, "LLMAnalyzer", EmptyFailureGeminiAnalyzer)
    manifest_path = tmp_path / "targets.json"
    acquisition_path = tmp_path / "acquisition.json"
    output_path = _test_output_path(cli, tmp_path, "empty-provider-failure.json")
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    acquisition_path.write_text(json.dumps(_acquisition()), encoding="utf-8")

    assert (
        cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--acquisition-result",
                str(acquisition_path),
                "--live-analysis",
                "--output",
                str(output_path),
            ]
        )
        == 2
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["analysis_failure"] == {
        "status": "failed",
        "reason": "gemini_provider_failure",
        "attempt_count": 1,
        "claims_available": False,
    }
    assert payload["community_analysis"]["status"] == "unavailable"
    assert payload["community_analysis"]["reason"] == "analyst_attempts_exhausted"
    assert "transport_error" not in json.dumps(payload)


def test_live_analysis_rejects_non_gemini_provider_before_native_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    native_calls: list[dict] = []

    class NonGeminiLLMAnalyzer:
        def __init__(self) -> None:
            self.use_cache = True
            self.base_url = "https://openrouter.example/v1"

        def _call_json_gemini_native(self, **kwargs):
            native_calls.append(kwargs)
            raise AssertionError("non-Gemini path must fail before invocation")

    monkeypatch.setenv("GEMINI_API_KEY", "live-gemini-key")
    monkeypatch.setattr(llm_analyzer_module, "LLMAnalyzer", NonGeminiLLMAnalyzer)
    manifest_path = tmp_path / "targets.json"
    acquisition_path = tmp_path / "acquisition.json"
    output_path = _test_output_path(cli, tmp_path, "non-gemini.json")
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    acquisition_path.write_text(json.dumps(_acquisition()), encoding="utf-8")

    assert (
        cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--acquisition-result",
                str(acquisition_path),
                "--live-analysis",
                "--output",
                str(output_path),
            ]
        )
        == 2
    )
    assert native_calls == []
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["analysis_failure"] == {
        "status": "failed",
        "reason": "gemini_unavailable",
        "attempt_count": 0,
        "claims_available": False,
    }
    assert payload["community_analysis"]["status"] == "not_requested"


def test_cli_replay_supports_offline_end_to_end_and_secure_write(tmp_path: Path) -> None:
    cli = _load_cli()
    manifest_path = tmp_path / "targets.json"
    acquisition_path = tmp_path / "acquisition.json"
    analyst_path = tmp_path / "analyst.json"
    output_path = _test_output_path(cli, tmp_path, "result.json")
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    acquisition_path.write_text(json.dumps(_acquisition()), encoding="utf-8")
    analyst_path.write_text(json.dumps(_analysis_envelope()), encoding="utf-8")

    assert (
        cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--acquisition-result",
                str(acquisition_path),
                "--analyst-response",
                str(analyst_path),
                "--allowed-tile-ids",
                '["tile-community"]',
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["community_analysis"]["status"] == "complete"
    assert payload["advisory_tile_candidates"]
    assert os.stat(output_path).st_mode & 0o777 == 0o600


def test_cli_rejects_untrusted_analysis_without_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli = _load_cli()
    manifest_path = tmp_path / "targets.json"
    acquisition_path = tmp_path / "acquisition.json"
    response_path = tmp_path / "bad.json"
    output_path = _test_output_path(cli, tmp_path, "result.json")
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    acquisition_path.write_text(json.dumps(_acquisition()), encoding="utf-8")
    bad = _analyst_response()
    bad["tile_candidates"][0]["tile_id"] = "not-allowed"
    response_path.write_text(json.dumps(bad), encoding="utf-8")

    assert (
        cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--acquisition-result",
                str(acquisition_path),
                "--analyst-response",
                str(response_path),
                "--allowed-tile-ids",
                '["tile-community"]',
                "--output",
                str(output_path),
            ]
        )
        == 2
    )
    assert not output_path.exists()
    assert "not-allowed" not in capsys.readouterr().err


def test_cli_replay_rejects_forged_observation_role_before_analysis(tmp_path: Path) -> None:
    cli = _load_cli()
    manifest_path = tmp_path / "targets.json"
    acquisition_path = tmp_path / "acquisition.json"
    analyst_path = tmp_path / "analyst.json"
    output_path = _test_output_path(cli, tmp_path, "forged.json")
    acquisition = _acquisition()
    acquisition["observations"][1]["actor_role"] = "official_brand_post"
    acquisition["observations"][1].pop("content_id")
    acquisition["observations"][1].pop("semantic_fingerprint")
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    acquisition_path.write_text(json.dumps(acquisition), encoding="utf-8")
    analyst_path.write_text(json.dumps(_analysis_envelope()), encoding="utf-8")

    assert (
        cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--acquisition-result",
                str(acquisition_path),
                "--analyst-response",
                str(analyst_path),
                "--output",
                str(output_path),
            ]
        )
        == 2
    )
    assert not output_path.exists()


def test_cli_rejects_outside_traversal_and_symlink_escape_without_writing(tmp_path: Path) -> None:
    cli = _load_cli()
    manifest_path = tmp_path / "targets.json"
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    output_root = tmp_path / "out" / "social-community-lab"
    cli.OUTPUT_ROOT = output_root
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")

    for destination in (outside, output_root.parent / "sibling.json", output_root / ".." / "escape.json"):
        assert cli.main(["--manifest", str(manifest_path), "--dry-run", "--output", str(destination)]) == 2
    assert outside.read_text(encoding="utf-8") == "sentinel"

    escaped_dir = tmp_path / "escaped-dir"
    escaped_dir.mkdir()
    symlink_parent = output_root / "symlink-parent"
    symlink_parent.parent.mkdir(parents=True, exist_ok=True)
    symlink_parent.symlink_to(escaped_dir, target_is_directory=True)
    escaped_destination = symlink_parent / "result.json"
    assert cli.main(["--manifest", str(manifest_path), "--dry-run", "--output", str(escaped_destination)]) == 2
    assert not (escaped_dir / "result.json").exists()
