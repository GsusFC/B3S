from __future__ import annotations

import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_V7_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_V7_SHA = "5fda3b95a4ea91299a34e894583c3862153e4b97"
RELEASE_COMMAND = (
    "sh -lc 'python scripts/verify_release_build.py && "
    "python scripts/import_b3s_reports_postgres.py --migrate-only'"
)
ISOLATED_RELEASE_COMMAND = (
    "sh -lc 'python scripts/verify_release_build.py && "
    "python scripts/verify_b3s_history_postgres.py && "
    "python scripts/verify_pr71_vault_target.py'"
)


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_container_persists_immutable_build_identity():
    dockerfile = _read("Dockerfile")

    assert "ARG B3S_BUILD_SHA=unknown" in dockerfile
    assert "B3S_BUILD_SHA=${B3S_BUILD_SHA}" in dockerfile
    assert 'org.opencontainers.image.revision="${B3S_BUILD_SHA}"' in dockerfile


def test_release_commands_reject_unidentified_images_before_migrating():
    for relative_path in ("fly.toml", "fly.vault.toml"):
        fly_config = tomllib.loads(_read(relative_path))

        assert fly_config["deploy"]["release_command"] == RELEASE_COMMAND


def test_deploy_workflow_builds_and_verifies_the_exact_commit():
    workflow = _read(".github/workflows/fly-deploy.yml")

    assert 'cancel-in-progress: false' in workflow
    assert '--build-arg B3S_BUILD_SHA="$DEPLOY_SHA"' in workflow
    assert 'https://b3s.fly.dev/health' in workflow
    assert 'if [ "$observed" = "$DEPLOY_SHA" ]' in workflow
    assert "Backfill evidence ledger shadow" in workflow
    assert "--migrate-only --rebuild-evidence-ledger-shadow" in workflow
    assert "continue-on-error: true" in workflow
    assert "timeout-minutes: 5" in workflow
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
    assert config["env"]["B3S_SITE_BASIC_AUTH_ENABLED"] == "true"
    assert config["env"]["BRAND3_VAULT_C7_CUTOVER_ENABLED"] == "false"
    assert config["env"]["BRAND3_VAULT_C7_EMERGENCY_DENY"] == "true"
    assert config["env"]["BRAND3_VAULT_C7_ALLOWLIST"] == ""
    assert config["env"]["B3S_VAULT_WORKER_ENABLED"] == "true"
    assert config["env"]["BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED"] == "true"
    assert config["env"]["BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH"] == ""
    assert config["http_service"]["auto_start_machines"] is True
    assert config["http_service"]["min_machines_running"] == 1


def test_isolated_pr71_vault_workflow_is_manual_exact_target_only():
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")

    assert "workflow_dispatch:" in workflow
    assert "workflow_run:" not in workflow
    assert "b3s-pr71-vault" in workflow
    assert "fly.pr71-vault.toml" in workflow
    assert "python scripts/migrate_b3s_history_postgres.py" in workflow
    assert "secrets.B3S_MIGRATION_DATABASE_URL" in workflow
    assert "flyctl deploy --remote-only" in workflow
    assert "-a b3s-pr71-vault" in workflow
    assert "fly.toml" not in workflow.replace("fly.pr71-vault.toml", "")
    assert f"actions/checkout@{CHECKOUT_V7_SHA}" in workflow
    assert f"actions/setup-python@{SETUP_PYTHON_V7_SHA}" in workflow
    assert "setup-flyctl@master" not in workflow
