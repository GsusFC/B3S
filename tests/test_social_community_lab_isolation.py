"""Independent boundary proofs for the file-only social/community laboratory.

These tests deliberately import the canonical SV9 kernel only as an external
oracle.  The laboratory sources are inspected separately so a future test
fixture cannot accidentally make a forbidden production dependency look safe.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from src.research.scrapecreators_spike import parse_target_manifest
from src.research.social_lab_contracts import MetricContext, SocialObservation
from src.sv9.assessment_kernel import build_sv9_assessment
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER


ROOT = Path(__file__).parents[1]
LAB_SOURCE_PATHS = (
    ROOT / "src/research/social_lab_contracts.py",
    ROOT / "src/research/scrapecreators_spike.py",
    ROOT / "src/research/social_community_lab.py",
    ROOT / "src/research/social_tiles.py",
    ROOT / "scripts/scrapecreators_social_spike.py",
    ROOT / "scripts/run_social_community_lab.py",
)
CANONICAL_OUTPUT_KEYS = frozenset(
    {
        "score",
        "scores",
        "numeric_score",
        "numeric_scores",
        "numeric_aggregate",
        "numeric_aggregates",
        "aggregate",
        "aggregates",
        "aggregate_score",
        "total_score",
        "points",
        "component_score",
        "component_scores",
        "canonical_score",
        "promotion_score",
        "canonical_assessment",
        "canonical_state",
        "assessment",
        "assessment_result",
        "sv9_score",
        "sv9",
        "sv9_assessment",
        "sv9_state",
        "vault",
        "vault_score",
        "vault_state",
        "assessment_state",
        "state",
        "tile_state",
        "activation",
        "activated",
        "enabled",
        "confidence",
        "confidence_level",
        "confidence_score",
        "detection_confidence",
        "assessment_fingerprint",
        "score_fingerprint",
    }
)


def _load_cli() -> Any:
    path = ROOT / "scripts/run_social_community_lab.py"
    spec = importlib.util.spec_from_file_location("social_community_lab_isolation_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> Any:
    return parse_target_manifest(
        {
            "schema_version": 1,
            "targets": [{"target_id": "brand-x", "platform": "twitter", "handle": "brandco"}],
        }
    )


def _observations() -> tuple[dict[str, Any], dict[str, Any]]:
    fixture_dir = ROOT / "fixtures/social_lab"
    return (
        json.loads((fixture_dir / "official_post.json").read_text(encoding="utf-8")),
        json.loads((fixture_dir / "community_response.json").read_text(encoding="utf-8")),
    )


def _acquisition() -> dict[str, Any]:
    official, community = _observations()
    return {
        "schema_version": "scrapecreators-social-spike.v1",
        "contract_version": "b3s-social-community-lab-v1",
        "observations": [official, community],
        "responses": [],
        "summary": {"target_count": 1, "post_count": 1, "interaction_count": 1},
    }


def _analyst_response() -> dict[str, Any]:
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
        "tile_candidates": [],
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


def _canonical_fingerprint() -> tuple[str, str, str]:
    rows = [
        {
            "component_key": component_key,
            "tile_id": str(tile["id"]),
            "tile_key": f"{component_key}.{tile['id']}",
            "assessment_state": "no",
        }
        for component_key in PRESENTATION_ORDER
        for tile in COMPONENTS[component_key]["tiles"]
    ]
    assessment = build_sv9_assessment(rows)
    return (
        json.dumps(assessment, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        str(assessment["assessment_fingerprint"]),
        str(assessment["score_fingerprint"]),
    )


def _hash_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _default_db_snapshot() -> dict[str, str | None]:
    paths = {
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not ({".git", ".venv"} & set(path.parts))
        and path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}
    }
    postgres_dir = ROOT / "data/postgres"
    if postgres_dir.is_dir():
        paths.update(path for path in postgres_dir.rglob("*") if path.is_file())
    return {str(path.relative_to(ROOT)): _hash_file(path) for path in sorted(paths)}


def _repo_output_snapshot() -> set[str]:
    output_dir = ROOT / "out"
    if not output_dir.is_dir():
        return set()
    return {str(path.relative_to(ROOT)) for path in output_dir.rglob("*") if path.is_file()}


def _tmp_db_artifacts(root: Path) -> set[str]:
    artifacts: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        lowered = path.name.casefold()
        if lowered.endswith((".sqlite", ".sqlite3", ".db")) or "postgres" in lowered:
            artifacts.add(str(path.relative_to(root)))
    return artifacts


def _assert_secure_file(path: Path) -> None:
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    json.loads(path.read_text(encoding="utf-8"))


_ADVISORY_SECTIONS = frozenset(
    {"cross_channel_voice", "recurring_community_themes", "response_behavior", "corroborations", "tensions"}
)


def _is_advisory_confidence_path(path: str) -> bool:
    return any(
        path.startswith(f"root.community_analysis.{section}[") and path.endswith("]") for section in _ADVISORY_SECTIONS
    )


def _is_v2_verdict_path(path: str) -> bool:
    prefix = "root.social_tiles.verdicts["
    return path.startswith(prefix) and path.endswith("]") and path[len(prefix) : -1].isdigit()


def _assert_no_canonical_fields(
    value: Any,
    *,
    path: str = "root",
    inside_raw: bool = False,
    authoritative_v2: bool = False,
) -> None:
    if path == "root" and isinstance(value, dict):
        authoritative_v2 = (
            value.get("schema_version") == "b3s-social-community-lab-v2"
            and value.get("version") == "b3s-social-community-lab-v2"
            and value.get("artifact_type") == "b3s-social-community-lab-v2"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).casefold()
            raw_child = inside_raw or key_text == "raw_payload_redacted"
            if not raw_child:
                advisory_confidence = key_text == "confidence" and _is_advisory_confidence_path(path)
                v2_verdict_state = authoritative_v2 and key_text == "state" and _is_v2_verdict_path(path)
                assert key_text not in CANONICAL_OUTPUT_KEYS or advisory_confidence or v2_verdict_state, (
                    f"{path}.{key} leaked canonical output"
                )
            _assert_no_canonical_fields(
                item,
                path=f"{path}.{key}",
                inside_raw=raw_child,
                authoritative_v2=authoritative_v2,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_canonical_fields(
                item,
                path=f"{path}[{index}]",
                inside_raw=inside_raw,
                authoritative_v2=authoritative_v2,
            )


def test_canonical_field_scanner_allows_only_authoritative_v2_verdict_state() -> None:
    payload: dict[str, Any] = {
        "schema_version": "b3s-social-community-lab-v2",
        "version": "b3s-social-community-lab-v2",
        "artifact_type": "b3s-social-community-lab-v2",
        "social_tiles": {
            "verdicts": [{"tile_id": "ST-VI-01", "state": "demonstrated", "citations": []}],
        },
    }
    _assert_no_canonical_fields(payload)

    misplaced = deepcopy(payload)
    misplaced["social_tiles"]["state"] = "demonstrated"
    with pytest.raises(AssertionError):
        _assert_no_canonical_fields(misplaced)

    legacy_marker = deepcopy(payload)
    legacy_marker["schema_version"] = "b3s-social-community-lab-v1"
    with pytest.raises(AssertionError):
        _assert_no_canonical_fields(legacy_marker)

    forbidden = deepcopy(payload)
    forbidden["social_tiles"]["verdicts"][0]["tile_state"] = "demonstrated"
    with pytest.raises(AssertionError):
        _assert_no_canonical_fields(forbidden)


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "score",
        "numeric_aggregate",
        "activation",
        "confidence",
        "promotion_score",
        "canonical_assessment",
        "sv9_score",
        "vault_state",
        "state",
        "tile_state",
    ],
)
def test_canonical_field_scanner_rejects_forbidden_fields_outside_owned_context(forbidden_key: str) -> None:
    with pytest.raises(AssertionError):
        _assert_no_canonical_fields({forbidden_key: "forged"})


def _fail_constructor(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("provider/LLM constructor must not be touched by isolated execution")


def _fail_db_connection(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("database connection must not be touched by isolated execution")


def test_lab_sources_have_no_canonical_or_persistence_imports() -> None:
    forbidden_prefixes = (
        "web",
        "src.sv9",
        "src.sv9_flow",
        "sqlite3",
        "psycopg",
        "psycopg2",
    )
    forbidden_segments = {"scanner", "vault", "store", "storage", "persistence", "database", "db"}
    violations: list[str] = []

    for path in LAB_SOURCE_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
                imported.extend(alias.name for alias in node.names)
            else:
                continue
            for name in imported:
                lowered = name.casefold()
                module_parts = set(lowered.split("."))
                if any(lowered == prefix or lowered.startswith(prefix + ".") for prefix in forbidden_prefixes):
                    violations.append(f"{path.name}:{node.lineno}:{name}")
                if module_parts & forbidden_segments or "save_report" in lowered:
                    violations.append(f"{path.name}:{node.lineno}:{name}")

    assert violations == []


def test_isolated_dry_run_and_offline_replay_are_file_only_and_side_effect_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    monkeypatch.chdir(tmp_path)
    cli.OUTPUT_ROOT = tmp_path / "out" / "social-community-lab"
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", "offline-provider-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "offline-llm-secret")
    monkeypatch.setattr(cli, "ScrapeCreatorsClient", _fail_constructor)
    monkeypatch.setattr(cli, "_build_gemini_invoker", _fail_constructor)
    monkeypatch.setattr(sqlite3, "connect", _fail_db_connection)
    try:
        import psycopg
    except ImportError:  # pragma: no cover - the project test environment has psycopg
        psycopg = None
    if psycopg is not None:
        monkeypatch.setattr(psycopg, "connect", _fail_db_connection)

    from src.features import llm_analyzer as llm_analyzer_module

    monkeypatch.setattr(llm_analyzer_module, "LLMAnalyzer", _fail_constructor)
    manifest_path = tmp_path / "targets.json"
    acquisition_path = tmp_path / "acquisition.json"
    analyst_path = tmp_path / "analyst.json"
    dry_output = cli.OUTPUT_ROOT / "dry" / "plan.json"
    replay_output = cli.OUTPUT_ROOT / "replay" / "artifact.json"
    manifest_path.write_text(json.dumps(_manifest().as_dict()), encoding="utf-8")
    acquisition_path.write_text(json.dumps(_acquisition()), encoding="utf-8")
    analyst_path.write_text(json.dumps(_analysis_envelope()), encoding="utf-8")

    db_before = _default_db_snapshot()
    repo_outputs_before = _repo_output_snapshot()
    canonical_before = _canonical_fingerprint()

    assert cli.main(["--manifest", str(manifest_path), "--dry-run", "--output", str(dry_output)]) == 0
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
                str(replay_output),
            ]
        )
        == 0
    )

    _assert_secure_file(dry_output)
    _assert_secure_file(replay_output)
    dry_artifact = json.loads(dry_output.read_text(encoding="utf-8"))
    replay_artifact = json.loads(replay_output.read_text(encoding="utf-8"))
    assert dry_artifact["mode"] == "dry_run"
    assert replay_artifact["mode"] == "offline_analysis"
    assert replay_artifact["community_analysis"]["status"] == "complete"
    _assert_no_canonical_fields(dry_artifact)
    _assert_no_canonical_fields(replay_artifact)
    assert _tmp_db_artifacts(tmp_path) == set()
    assert _repo_output_snapshot() == repo_outputs_before
    assert _default_db_snapshot() == db_before
    assert _canonical_fingerprint() == canonical_before


def test_atomic_artifacts_are_private_json_and_raw_provider_fields_are_the_only_opt_in_escape(
    tmp_path: Path,
) -> None:
    cli = _load_cli()
    acquisition = _acquisition()
    acquisition["responses"] = [
        {
            "endpoint_key": "twitter_user_tweets",
            "outcome": "succeeded",
            "raw_payload_redacted": {
                "provider_note": "raw content is opt-in",
                "score": 17,
                "token": "[REDACTED]",
            },
        }
    ]
    cli.OUTPUT_ROOT = tmp_path / "out" / "social-community-lab"
    output = cli.OUTPUT_ROOT / "private" / "artifact.json"
    path = cli.atomic_write_json(output, cli.compose_lab_artifact(_manifest(), acquisition, include_raw=True))
    _assert_secure_file(path)
    artifact = json.loads(path.read_text(encoding="utf-8"))
    _assert_no_canonical_fields(artifact)
    assert artifact["provider_responses"][0]["raw_payload_redacted"]["score"] == 17
    assert _tmp_db_artifacts(tmp_path) == set()


def test_metric_and_provenance_mutations_cannot_change_canonical_sv9_or_lab_semantics() -> None:
    official_raw, community_raw = _observations()
    original = [SocialObservation.from_dict(official_raw), SocialObservation.from_dict(community_raw)]
    changed = [
        replace(
            original[0],
            metric_context=MetricContext(
                metrics={"likes": 99_999, "followers": 500_000},
                observed_at="2026-09-01T00:00:00Z",
            ),
            provenance=replace(
                original[0].provenance,
                request_fingerprint="request-fingerprint-changed",
                response_sha256="c" * 64,
            ),
        ),
        replace(
            original[1],
            provenance=replace(
                original[1].provenance,
                endpoint_key="different_endpoint",
                response_sha256="d" * 64,
            ),
        ),
    ]
    from src.research.social_community_lab import analyze_social_community, build_analysis_prompt

    prompts: list[str] = []

    def fake(**kwargs: Any) -> object:
        prompts.append(kwargs["user"])
        return _analysis_envelope(_valid_core_response(original))

    first = analyze_social_community(original, fake)
    second = analyze_social_community(changed, fake)
    assert prompts[0] == prompts[1] == build_analysis_prompt(original) == build_analysis_prompt(changed)
    assert first.to_dict() == second.to_dict()


def _valid_core_response(observations: list[SocialObservation]) -> dict[str, Any]:
    community = str(observations[1].content_id)
    return {
        "cross_channel_voice": [],
        "recurring_community_themes": [
            {
                "statement": "Community members describe the outcome as useful.",
                "subject": "community_perception",
                "confidence": "medium",
                "citation_content_ids": [community],
                "derived_source_roles": ["community_response"],
            }
        ],
        "response_behavior": [],
        "corroborations": [],
        "tensions": [],
        "tile_candidates": [],
    }
