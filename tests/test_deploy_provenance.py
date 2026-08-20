from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_V7_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_V7_SHA = "5fda3b95a4ea91299a34e894583c3862153e4b97"
RELEASE_COMMAND = (
    "sh -lc 'python scripts/verify_release_build.py && "
    "python scripts/verify_b3s_history_postgres.py'"
)
ISOLATED_RELEASE_COMMAND = (
    "sh -lc 'python scripts/verify_release_build.py && "
    "python scripts/verify_b3s_history_postgres.py && "
    "python scripts/verify_pr71_vault_target.py'"
)
TRUSTED_PR_HEAD_SHA = "b476fff80cea9fb1f474bce24ebe511c06f30f7b"
TRUSTED_PR_MERGE_SHA = "0ac13c6e95ce0f413094838f2448578d25e90b2c"
TRUSTED_CI_BLOB_SHA = "c1082f6b5c53a364b8d38e43936b93722c0183d1"
TRUSTED_REPOSITORY_ID = 1288696741
TRUSTED_CI_WORKFLOW_ID = 306885838
TRUSTED_PR_CI_RUN_ID = 32006974926
TRUSTED_MERGE_CI_RUN_ID = 32007250803
EXPECTED_DEPLOY_WORKFLOW_BLOB_SHA = "d" * 40
FIXTURE_CONTROLLER_SHA = "7550c9969ea9b77dafbe016abe4c93e86e6a2535"
FIXTURE_DEPLOY_SHA = FIXTURE_CONTROLLER_SHA
HOTFIX_FILES = {
    ".github/workflows/fly-deploy-pr71-vault.yml",
    "docs/deployment/b3s_pr71_vault.md",
    "scripts/configure_b3s_runtime_role.py",
    "scripts/run_evidence_vault_acquisition_worker.py",
    "scripts/run_evidence_vault_operational_sv9_shadow_work_items.py",
    "src/history/evidence_vault_raw_repository.py",
    "src/history/migrations/029_evidence_vault_semantic_analysis_claims.sql",
    "src/history/repository.py",
    "src/services/evidence_vault_acquisition_outcome.py",
    "src/services/evidence_vault_acquisition_runtime.py",
    "src/services/evidence_vault_field_replay.py",
    "src/services/evidence_vault_incremental_executor.py",
    "src/services/evidence_vault_incremental_refresh.py",
    "src/services/evidence_vault_operational_assessment_shadow.py",
    "src/services/evidence_vault_operational_candidate.py",
    "src/services/evidence_vault_operational_memory.py",
    "src/services/evidence_vault_scan_orchestration.py",
    "src/services/evidence_vault_semantic_analysis_contract.py",
    "src/services/scanner_analysis_contract.py",
    "src/services/scanner_evidence_comparison.py",
    "src/sv9/evaluator.py",
    "src/sv9_flow/interpretation_llm_worker.py",
    "src/sv9_flow/policy_data/calibration_terms.json",
    "src/sv9_flow/semantic_passages.py",
    "tests/test_b3s_history.py",
    "tests/test_configure_b3s_runtime_role.py",
    "tests/test_deploy_provenance.py",
    "tests/test_evidence_vault_acquisition_runtime.py",
    "tests/test_evidence_vault_acquisition_worker_entrypoint.py",
    "tests/test_evidence_vault_composite_group_lifecycle.py",
    "tests/test_evidence_vault_cumulative_upgrade_postgres.py",
    "tests/test_evidence_vault_field_replay.py",
    "tests/test_evidence_vault_incremental_executor.py",
    "tests/test_evidence_vault_incremental_refresh.py",
    "tests/test_evidence_vault_operational_candidate.py",
    "tests/test_evidence_vault_operation_execution_postgres.py",
    "tests/test_evidence_vault_operational_sv9_shadow_ledger_migration.py",
    "tests/test_evidence_vault_operational_sv9_shadow_work_item_runner.py",
    "tests/test_evidence_vault_raw_provenance_postgres.py",
    "tests/test_evidence_vault_raw_repository.py",
    "tests/test_evidence_vault_scan_orchestration.py",
    "tests/test_evidence_vault_semantic_analysis_claims_migration.py",
    "tests/test_evidence_vault_semantic_analysis_contract.py",
    "tests/test_scanner_evidence_comparison.py",
    "tests/test_sv9_evaluator.py",
    "tests/test_sv9_flow_block_detection_worker.py",
    "tests/test_sv9_flow_calibration_terms.py",
    "tests/test_sv9_flow_interpretation_llm_worker.py",
    "tests/test_vault_memory_report_projection.py",
    "tests/test_web_lab.py",
    "web/app.py",
    "web/scan_runner.py",
    "web/templates/brand.html.j2",
    "web/templates/index.html.j2",
    "web/templates/report.html.j2",
    "web/templates/scan.html.j2",
}


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _attestation_source() -> str:
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")
    marker = "          python - <<'PY'\n"
    start = workflow.index(marker) + len(marker)
    end = workflow.index("\n          PY", start)
    return textwrap.dedent(workflow[start:end])


def _attestation_fixture() -> dict[str, dict]:
    repository = {"id": TRUSTED_REPOSITORY_ID, "full_name": "GsusFC/B3S"}
    common_run = {
        "workflow_id": TRUSTED_CI_WORKFLOW_ID,
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "repository": repository,
        "head_repository": repository,
    }
    ci_blob = {
        "type": "file",
        "path": ".github/workflows/ci.yml",
        "sha": TRUSTED_CI_BLOB_SHA,
    }
    fixture = {
        "PR_FILE": {
            "number": 102,
            "state": "closed",
            "merged": True,
            "draft": False,
            "merge_commit_sha": TRUSTED_PR_MERGE_SHA,
            "base": {
                "ref": "main",
                "sha": "b" * 40,
                "repo": repository,
            },
            "head": {
                "ref": "fix/vault-semantic-coverage",
                "sha": TRUSTED_PR_HEAD_SHA,
                "repo": repository,
            },
        },
        "MAIN_REF_FILE": {
            "ref": "refs/heads/main",
            "object": {"type": "commit", "sha": FIXTURE_CONTROLLER_SHA},
        },
        "WORKFLOW_FILE": {
            "id": TRUSTED_CI_WORKFLOW_ID,
            "path": ".github/workflows/ci.yml",
            "state": "active",
        },
        "HEAD_CI_FILE": dict(ci_blob),
        "MERGE_CI_FILE": dict(ci_blob),
        "DEPLOY_CI_FILE": dict(ci_blob),
        "DEPLOY_WORKFLOW_BLOB_FILE": {
            "type": "file",
            "path": ".github/workflows/fly-deploy-pr71-vault.yml",
            "sha": EXPECTED_DEPLOY_WORKFLOW_BLOB_SHA,
        },
        "HEAD_MERGE_COMPARE_FILE": {
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "total_commits": 1,
            "base_commit": {"sha": TRUSTED_PR_HEAD_SHA},
            "merge_base_commit": {"sha": TRUSTED_PR_HEAD_SHA},
            "commits": [{"sha": TRUSTED_PR_MERGE_SHA}],
            "files": [],
        },
        "MERGE_DEPLOY_COMPARE_FILE": {
            "status": "ahead",
            "ahead_by": 6,
            "behind_by": 0,
            "total_commits": 6,
            "base_commit": {"sha": TRUSTED_PR_MERGE_SHA},
            "merge_base_commit": {"sha": TRUSTED_PR_MERGE_SHA},
            "commits": [
                {"sha": "d9c2ffcab806616bf6e5d58bc80a7d654294c82c"},
                {"sha": "3795739abd64d8a0137b11c173acb822ccc33e25"},
                {"sha": "4c1df3b3bf7b055ec912cf9cbc2a651d59b17221"},
                {"sha": "3eaac76d0b5988b3041f7f8fbf5ff074a12372e1"},
                {"sha": "725d26c1799bb832cb368a1d8ca07edd3366c322"},
                {"sha": "7550c9969ea9b77dafbe016abe4c93e86e6a2535"},
            ],
            "files": [
                {"filename": filename, "status": "modified"}
                for filename in sorted(HOTFIX_FILES)
            ],
        },
        "PR_CI_RUN_FILE": {
            **common_run,
            "id": TRUSTED_PR_CI_RUN_ID,
            "event": "pull_request",
            "head_branch": "fix/vault-semantic-coverage",
            "head_sha": TRUSTED_PR_HEAD_SHA,
        },
        "MERGE_CI_RUN_FILE": {
            **common_run,
            "id": TRUSTED_MERGE_CI_RUN_ID,
            "event": "push",
            "head_branch": "main",
            "head_sha": TRUSTED_PR_MERGE_SHA,
        },
        "DEPLOYMENT_CI_RUNS_FILE": {
            "total_count": 1,
            "workflow_runs": [
                {
                    **common_run,
                    "id": 40000000000,
                    "event": "push",
                    "head_branch": "main",
                    "head_sha": FIXTURE_CONTROLLER_SHA,
                }
            ],
        },
    }
    return json.loads(json.dumps(fixture))


def _run_attestation(
    tmp_path: Path,
    fixture: dict[str, dict],
    *,
    env_updates: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DEPLOY_SHA": FIXTURE_DEPLOY_SHA,
        "DISPATCH_REF": "refs/heads/main",
        "DISPATCH_SHA": FIXTURE_CONTROLLER_SHA,
        "GH_REPOSITORY": "GsusFC/B3S",
        "TRUSTED_PR_HEAD_SHA": TRUSTED_PR_HEAD_SHA,
        "TRUSTED_PR_MERGE_SHA": TRUSTED_PR_MERGE_SHA,
        "TRUSTED_CI_BLOB_SHA": TRUSTED_CI_BLOB_SHA,
        "TRUSTED_REPOSITORY_ID": str(TRUSTED_REPOSITORY_ID),
        "TRUSTED_CI_WORKFLOW_ID": str(TRUSTED_CI_WORKFLOW_ID),
        "TRUSTED_PR_CI_RUN_ID": str(TRUSTED_PR_CI_RUN_ID),
        "TRUSTED_MERGE_CI_RUN_ID": str(TRUSTED_MERGE_CI_RUN_ID),
        "EXPECTED_DEPLOY_WORKFLOW_BLOB_SHA": EXPECTED_DEPLOY_WORKFLOW_BLOB_SHA,
    }
    for name, payload in fixture.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        env[name] = str(path)
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        [sys.executable, "-c", _attestation_source()],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def _replace_nested(payload: dict, path: tuple[str | int, ...], value: object) -> None:
    target: object = payload
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]


def test_container_persists_immutable_build_identity():
    dockerfile = _read("Dockerfile")

    assert "ARG B3S_BUILD_SHA=unknown" in dockerfile
    assert "B3S_BUILD_SHA=${B3S_BUILD_SHA}" in dockerfile
    assert 'org.opencontainers.image.revision="${B3S_BUILD_SHA}"' in dockerfile


def test_release_commands_are_select_only_and_never_migrate_with_runtime_dsn():
    for relative_path in ("fly.toml", "fly.vault.toml"):
        fly_config = tomllib.loads(_read(relative_path))

        assert fly_config["deploy"]["release_command"] == RELEASE_COMMAND
        assert "--migrate-only" not in fly_config["deploy"]["release_command"]
        assert (
            fly_config["env"]["BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED"]
            == "false"
        )


def test_deploy_workflow_builds_and_verifies_the_exact_commit():
    workflow = _read(".github/workflows/fly-deploy.yml")

    assert 'cancel-in-progress: false' in workflow
    assert '--build-arg B3S_BUILD_SHA="$DEPLOY_SHA"' in workflow
    assert 'https://b3s.fly.dev/health' in workflow
    assert 'if [ "$observed" = "$DEPLOY_SHA" ]' in workflow
    assert "Backfill evidence ledger shadow" not in workflow
    assert "--migrate-only" not in workflow
    assert "independently authorized external migration" in workflow
    assert "setup-flyctl@master" not in workflow


def test_github_actions_are_pinned_to_immutable_commits():
    workflows = _read(".github/workflows/ci.yml") + _read(".github/workflows/fly-deploy.yml")

    assert workflows.count(f"actions/checkout@{CHECKOUT_V7_SHA}") == 2
    assert workflows.count(f"actions/setup-python@{SETUP_PYTHON_V7_SHA}") == 1
    assert "actions/checkout@v4" not in workflows
    assert "actions/setup-python@v5" not in workflows
    assert "setup-flyctl@master" not in workflows


def test_isolated_pr71_vault_config_is_exact_and_fail_closed():
    config = tomllib.loads(_read("fly.pr71-vault.toml"))

    assert config["app"] == "b3s-pr71-vault"
    assert config["deploy"]["release_command"] == ISOLATED_RELEASE_COMMAND
    assert config["mounts"] == [
        {"source": "b3s_pr71_vault_data", "destination": "/data"}
    ]
    assert config["env"]["BRAND3_BASE_URL"] == "https://b3s-pr71-vault.fly.dev"
    assert config["env"]["B3S_EXPECTED_NEON_BRANCH_ID"] == "br-divine-star-aspobuer"
    assert config["env"]["B3S_EXPECTED_RUNTIME_ROLE"] == "b3s_pr71_app_runtime"
    assert config["env"]["B3S_POSTGRES_REQUIRED"] == "true"
    assert config["env"]["BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED"] == "true"
    assert config["env"]["B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED"] == "false"
    assert config["env"]["B3S_GOOGLE_OIDC_ENABLED"] == "true"
    assert "B3S_SITE_BASIC_AUTH_ENABLED" not in config["env"]
    assert "B3S_SITE_BASIC_AUTH_USERNAME" not in config["env"]
    assert "B3S_GOOGLE_OIDC_ALLOWED_EMAILS" not in config["env"]
    assert not any("C7" in key for key in config["env"])
    assert config["env"]["B3S_VAULT_WORKER_ENABLED"] == "true"
    assert config["processes"]["app"].endswith("--no-access-log")
    assert "--no-access-log" not in _read("fly.toml")
    assert "--no-access-log" not in _read("fly.vault.toml")
    assert config["env"]["BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED"] == "true"
    assert config["env"]["BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH"] == ""
    assert config["http_service"]["auto_start_machines"] is True
    assert config["http_service"]["min_machines_running"] == 1


def test_isolated_pr71_vault_workflow_is_manual_post_merge_only():
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")

    assert "workflow_dispatch:" in workflow
    assert 'description: "Exact current main SHA to deploy"' in workflow
    assert "Exact reviewed PR #102 merge SHA to deploy" not in workflow
    assert "workflow_run:" not in workflow
    assert "Deploy isolated PR71 Vault" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "name: pr71-vault" in workflow
    assert "b3s-pr71-vault" in workflow
    assert "fly.pr71-vault.toml" in workflow
    assert "python scripts/migrate_b3s_history_postgres.py" in workflow
    assert "--target-profile pr71-vault" in workflow
    assert "python scripts/configure_b3s_runtime_role.py" in workflow
    assert "--role b3s_pr71_app_runtime" in workflow
    assert "secrets.B3S_MIGRATION_DATABASE_URL" in workflow
    assert "flyctl deploy --remote-only" in workflow
    assert "-a b3s-pr71-vault" in workflow
    assert "ref: ${{ inputs.deployment_sha }}" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$DEPLOY_SHA"' in workflow
    assert '--build-arg B3S_BUILD_SHA="$DEPLOY_SHA"' in workflow
    assert "https://b3s-pr71-vault.fly.dev/health" in workflow
    assert 'test "$observed" = "$DEPLOY_SHA"' in workflow
    assert "fly.toml" not in workflow.replace("fly.pr71-vault.toml", "")
    assert f"actions/checkout@{CHECKOUT_V7_SHA}" in workflow
    assert f"actions/setup-python@{SETUP_PYTHON_V7_SHA}" in workflow
    assert "setup-flyctl@master" not in workflow
    attest = workflow.index("Attest reviewed deployment ancestry and current main before checkout")
    checkout = workflow.index(f"actions/checkout@{CHECKOUT_V7_SHA}")
    install = workflow.index("Install release package")
    migration_secret = workflow.index("secrets.B3S_MIGRATION_DATABASE_URL")
    fly_secret = workflow.index("secrets.FLY_API_TOKEN")
    assert attest < checkout < install < migration_secret < fly_secret
    assert 'r"[0-9a-f]{40}"' in workflow
    assert 'DISPATCH_REF: ${{ github.ref }}' in workflow
    assert 'os.environ["DISPATCH_REF"] == "refs/heads/main"' in workflow
    assert '"$API_URL/repos/$GH_REPOSITORY/pulls/102"' in workflow
    assert "persist-credentials: false" in workflow
    assert "EXPECTED_DEPLOY_WORKFLOW_BLOB_SHA: ${{ vars.PR71_DEPLOY_WORKFLOW_BLOB_SHA }}" in workflow
    assert "deploy_workflow_blob_file" in workflow
    assert "contents/.github/workflows/fly-deploy-pr71-vault.yml?ref=$DISPATCH_SHA" in workflow
    assert "contents/.github/workflows/fly-deploy-pr71-vault.yml?ref=$DEPLOY_SHA" not in workflow
    assert "deployment workflow blob is not the protected environment baseline" in workflow


def test_isolated_deploy_attests_exact_pr71_and_current_main_ancestry():
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")

    required_contract = (
        'DISPATCH_SHA: ${{ github.sha }}',
        'deploy_sha == dispatch_sha',
        'pr.get("number"), 102',
        'pr.get("state") == "closed"',
        'pr.get("merged") is True',
        'pr.get("draft") is False',
        'base.get("ref") == "main"',
        'head.get("ref") == "fix/vault-semantic-coverage"',
        'head.get("sha") == trusted_head',
        'pr.get("merge_commit_sha") == trusted_merge',
        'repo.get("id"), trusted_repository_id',
        'repo.get("full_name") == repository',
        'main_ref.get("ref") == "refs/heads/main"',
        'main_object.get("type") == "commit"',
        'main_object.get("sha") == dispatch_sha',
        'payload.get("merge_base_commit", {}).get("sha") == base_sha',
        'commits[-1].get("sha") == head_sha',
    )
    for assertion in required_contract:
        assert assertion in workflow
    assert TRUSTED_PR_HEAD_SHA in workflow
    assert TRUSTED_PR_MERGE_SHA in workflow
    assert (
        "compare/$TRUSTED_PR_HEAD_SHA...$TRUSTED_PR_MERGE_SHA?"
        "per_page=100&page=1"
    ) in workflow
    assert (
        "compare/$TRUSTED_PR_MERGE_SHA...$DISPATCH_SHA?per_page=100&page=1"
        in workflow
    )
    assert 'deploy_sha == trusted_merge' not in workflow


def test_isolated_deploy_pins_ci_blob_runs_and_hotfix_file_set():
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")

    assert "actions: read" in workflow
    assert "checks: read" not in workflow
    assert TRUSTED_CI_BLOB_SHA in workflow
    assert str(TRUSTED_REPOSITORY_ID) in workflow
    assert str(TRUSTED_CI_WORKFLOW_ID) in workflow
    assert str(TRUSTED_PR_CI_RUN_ID) in workflow
    assert str(TRUSTED_MERGE_CI_RUN_ID) in workflow
    assert 'workflow.get("id"), trusted_workflow_id' in workflow
    assert 'workflow.get("path") == CI_PATH' in workflow
    assert 'workflow.get("state") == "active"' in workflow
    for ref in (
        "$TRUSTED_PR_HEAD_SHA",
        "$TRUSTED_PR_MERGE_SHA",
        "$DISPATCH_SHA",
    ):
        assert f"contents/.github/workflows/ci.yml?ref={ref}" in workflow
    assert 'blob.get("sha") == trusted_blob' in workflow
    assert "HOTFIX_FILES = {" in workflow
    for filename in HOTFIX_FILES:
        assert f'"{filename}"' in workflow
    assert "filenames == expected_files" in workflow
    assert 'item.get("status") in {"added", "modified"}' in workflow
    assert '"previous_filename" not in item' in workflow
    assert (
        "actions/workflows/$TRUSTED_CI_WORKFLOW_ID/runs?event=push&"
        "branch=main&head_sha=$DISPATCH_SHA&per_page=100"
    ) in workflow
    assert '"event": "push"' in workflow
    assert '"head_branch": "main"' in workflow
    assert '"head_sha": dispatch_sha' in workflow
    assert '"status": "completed"' in workflow
    assert '"conclusion": "success"' in workflow
    assert '"run_attempt": 1' in workflow
    assert 'for field in ("repository", "head_repository")' in workflow
    assert 'deployment_runs.get("total_count"), 1' in workflow
    assert "len(runs) == 1" in workflow
    assert 'run.get("pull_requests")' not in workflow


def test_pr71_attestation_fixture_accepts_only_current_allowlisted_main(tmp_path):
    result = _run_attestation(tmp_path, _attestation_fixture())

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("target", "path", "value", "error"),
    [
        ("PR_FILE", ("head", "sha"), "f" * 40, "reviewed PR head SHA"),
        ("PR_FILE", ("merge_commit_sha",), "f" * 40, "merge commit SHA"),
        ("MAIN_REF_FILE", ("object", "sha"), "f" * 40, "not current main"),
        ("WORKFLOW_FILE", ("id",), 1, "workflow id"),
        ("HEAD_CI_FILE", ("sha",), "f" * 40, "blob SHA"),
        ("MERGE_CI_FILE", ("sha",), "f" * 40, "blob SHA"),
        ("DEPLOY_CI_FILE", ("sha",), "f" * 40, "blob SHA"),
        ("DEPLOY_WORKFLOW_BLOB_FILE", ("sha",), "f" * 40, "deployment workflow blob"),
        (
            "HEAD_MERGE_COMPARE_FILE",
            ("merge_base_commit", "sha"),
            "f" * 40,
            "merge base",
        ),
        (
            "MERGE_DEPLOY_COMPARE_FILE",
            ("behind_by",),
            1,
            "behind its base",
        ),
        (
            "MERGE_DEPLOY_COMPARE_FILE",
            ("commits", -1, "sha"),
            "f" * 40,
            "head commit",
        ),
        ("PR_CI_RUN_FILE", ("conclusion",), "failure", "unexpected conclusion"),
        ("MERGE_CI_RUN_FILE", ("head_sha",), "f" * 40, "unexpected head_sha"),
        (
            "DEPLOYMENT_CI_RUNS_FILE",
            ("workflow_runs", 0, "event"),
            "workflow_dispatch",
            "unexpected event",
        ),
        (
            "DEPLOYMENT_CI_RUNS_FILE",
            ("workflow_runs", 0, "head_sha"),
            TRUSTED_PR_MERGE_SHA,
            "unexpected head_sha",
        ),
        (
            "DEPLOYMENT_CI_RUNS_FILE",
            ("workflow_runs", 0, "repository", "full_name"),
            "attacker/fork",
            "unexpected repository name",
        ),
        (
            "DEPLOYMENT_CI_RUNS_FILE",
            ("workflow_runs", 0, "head_repository", "id"),
            1,
            "unexpected head_repository id",
        ),
        (
            "DEPLOYMENT_CI_RUNS_FILE",
            ("total_count",),
            2,
            "missing or ambiguous",
        ),
    ],
)
def test_pr71_attestation_rejects_tampered_rest_objects(
    tmp_path, target, path, value, error
):
    fixture = _attestation_fixture()
    _replace_nested(fixture[target], path, value)

    result = _run_attestation(tmp_path, fixture)

    assert result.returncode != 0
    assert error in result.stderr


@pytest.mark.parametrize(
    ("files", "error"),
    [
        (
            [
                {"filename": filename, "status": "modified"}
                for filename in sorted(HOTFIX_FILES | {"src/config.py"})
            ],
            "changed files are not exact",
        ),
        (
            [
                {"filename": filename, "status": "modified"}
                for filename in sorted(HOTFIX_FILES - {"tests/test_deploy_provenance.py"})
            ],
            "changed files are not exact",
        ),
        (
            [
                {
                    "filename": filename,
                    "status": "removed"
                    if filename == "tests/test_deploy_provenance.py"
                    else "modified",
                }
                for filename in sorted(HOTFIX_FILES)
            ],
            "non-modified allowlisted file",
        ),
        (
            [
                {
                    "filename": filename,
                    "status": "modified",
                    **(
                        {"previous_filename": "src/config.py"}
                        if filename == "tests/test_deploy_provenance.py"
                        else {}
                    ),
                }
                for filename in sorted(HOTFIX_FILES)
            ],
            "renamed file",
        ),
    ],
)
def test_pr71_attestation_rejects_non_exact_hotfix_file_sets(tmp_path, files, error):
    fixture = _attestation_fixture()
    fixture["MERGE_DEPLOY_COMPARE_FILE"]["files"] = files

    result = _run_attestation(tmp_path, fixture)

    assert result.returncode != 0
    assert error in result.stderr


def test_pr71_attestation_rejects_unreviewed_deployment_target(tmp_path):
    result = _run_attestation(
        tmp_path,
        _attestation_fixture(),
        env_updates={"DEPLOY_SHA": "f" * 40},
    )

    assert result.returncode != 0
    assert "deployment_sha is not current main" in result.stderr


def test_pr71_attestation_rejects_controller_other_than_current_main(tmp_path):
    result = _run_attestation(
        tmp_path,
        _attestation_fixture(),
        env_updates={"DEPLOY_SHA": "f" * 40, "DISPATCH_SHA": "f" * 40},
    )

    assert result.returncode != 0
    assert "dispatch SHA is not current main" in result.stderr


def test_isolated_deploy_does_not_accept_named_check_or_commit_status():
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")

    assert "/check-runs" not in workflow
    assert "/commits/$DEPLOY_SHA/status" not in workflow
    assert "Full tests and strategic benchmark" not in workflow
    assert 'app.get("slug")' not in workflow


def test_pr71_runbook_keeps_migrator_dsn_out_of_command_history():
    runbook = _read("docs/deployment/b3s_pr71_vault.md")

    assert "approved secret manager must provision `B3S_MIGRATION_DATABASE_URL`" in runbook
    assert "temporary secret" in runbook
    assert "B3S_MIGRATION_DATABASE_URL='" not in runbook
    assert "Do not invoke the migrator" in runbook
    assert "`fly deploy` from a local checkout" in runbook
    assert "fly deploy --remote-only" not in runbook

def test_pr71_runbook_marks_workflow_post_merge_and_separately_authorized():
    runbook = _read("docs/deployment/b3s_pr71_vault.md")

    assert "**post-merge-only**" in runbook
    assert "authorizes a merge or deployment" in runbook
    assert "deployment GO remain separate operator decisions" in runbook
    assert "`state=closed`, `merged=true`, and `draft=false`" in runbook
    assert "`merge_commit_sha`" in runbook
    assert "`refs/heads/main`" in runbook
    assert "custom deployment branch policy configured to allow only `main`" in runbook
    assert "`deployment_sha` input must equal the live `main` SHA" in runbook
    assert "reviewed PR\n#102 head" in runbook
    assert "reviewed PR\n#97 head" not in runbook
    assert "immutable dispatch SHA must separately equal the live REST `main` ref" in runbook
    assert "controller is checked out only as part of attested current main" in runbook
    assert "currently contains zero deployment secrets" in runbook
    assert "`B3S_MIGRATION_DATABASE_URL` and `FLY_API_TOKEN` were provisioned only" in runbook
    assert "Deployment run `31851012413`" in runbook
    assert "completed every attestation, migration/ACL" in runbook
    assert "`8e6bc79da525e854e1a96fb5cad9f3837445f125`" in runbook
    assert "Both temporary deployment secrets were then removed" in runbook
    assert "Any future deployment requires a new exact-SHA GO" in runbook
    assert "exact reviewed semantic-v3 application image" in runbook
    assert "already merged PRs #89, #91, #93" in runbook
    assert "#95, and #97" in runbook
    assert "`B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED=false`" in runbook
    assert "scheduler retain their independently authorized state" in runbook
    assert "replaces the former deployment NO-GO only" in runbook
    assert "records an exact-SHA deployment" in runbook
    assert "No new flag activation or scheduler-state change" in runbook
    assert "Post-deployment semantic-v3 validation" in runbook
    assert "metadata.pipeline_commit_sha` must equal the dispatched `deployment_sha`" in runbook
    assert "score_projected_from_evidence_vault_semantic_scoring_v3" in runbook
    assert "historical Vault report before and after" in runbook
    assert "`raw.vault.legacy_operational_v2.score_evaluation`" in runbook
    assert "`raw.vault.legacy_operational_v2.score_authority_witness`" in runbook
    assert "do not backfill, promote evidence, or roll back automatically" in runbook
    assert "remains **NO-GO** under the" not in runbook
    assert "A separate post-merge deployment reauthorization must" not in runbook
    assert "self-blob comparison is defense in depth" in runbook
    assert "mutable repository source an immutable root of trust" in runbook
    assert "Before provisioning either" in runbook
    assert "temporary secret, the operator must independently fetch" in runbook
    assert "environment still contains zero" in runbook
    assert "immutable external deployment controller" in runbook
    assert "these exact existing, modified, non-renamed paths" in runbook
    assert "without filtering" in runbook
    assert "non-successful runs" in runbook
    assert "`pull_requests` array may be empty" in runbook



def test_vault_adr_distinguishes_pr71_capability_from_dormant_b3s_vault():
    adr = _read("docs/evidence_vault_canonical_memory_adr_v2.md")

    assert "`BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED=true`" in adr
    assert "`b3s-vault` conserva explícitamente" in adr
    assert "capability en `false`" in adr
    assert "PR71 ejerce el pipeline operacional" in adr
    assert "`b3s-vault` sigue dormant" in adr
