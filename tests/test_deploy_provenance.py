from __future__ import annotations

import tomllib
from pathlib import Path


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


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


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
    assert config["env"]["B3S_SITE_BASIC_AUTH_ENABLED"] == "true"
    assert config["env"]["BRAND3_VAULT_C7_CUTOVER_ENABLED"] == "false"
    assert config["env"]["BRAND3_VAULT_C7_EMERGENCY_DENY"] == "true"
    assert config["env"]["BRAND3_VAULT_C7_ALLOWLIST"] == ""
    assert config["env"]["B3S_VAULT_WORKER_ENABLED"] == "true"
    assert config["env"]["BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED"] == "true"
    assert config["env"]["BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH"] == ""
    assert config["http_service"]["auto_start_machines"] is True
    assert config["http_service"]["min_machines_running"] == 1


def test_isolated_pr71_vault_workflow_is_manual_post_merge_only():
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")

    assert "workflow_dispatch:" in workflow
    assert "workflow_run:" not in workflow
    assert "Deploy isolated PR71 Vault after merge" in workflow
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
    assert "fly.toml" not in workflow.replace("fly.pr71-vault.toml", "")
    assert f"actions/checkout@{CHECKOUT_V7_SHA}" in workflow
    assert f"actions/setup-python@{SETUP_PYTHON_V7_SHA}" in workflow
    assert "setup-flyctl@master" not in workflow
    attest = workflow.index("Attest exact merged PR71 commit before checkout")
    checkout = workflow.index(f"actions/checkout@{CHECKOUT_V7_SHA}")
    install = workflow.index("Install release package")
    migration_secret = workflow.index("secrets.B3S_MIGRATION_DATABASE_URL")
    fly_secret = workflow.index("secrets.FLY_API_TOKEN")
    assert attest < checkout < install < migration_secret < fly_secret
    assert 'r"[0-9a-f]{40}"' in workflow
    assert 'DISPATCH_REF: ${{ github.ref }}' in workflow
    assert 'dispatch_ref == "refs/heads/main"' in workflow
    assert '"$API_URL/repos/$GH_REPOSITORY/pulls/71"' in workflow
    assert "persist-credentials: false" in workflow


def test_isolated_deploy_attests_the_complete_merged_pr71_object():
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")

    required_contract = (
        'dispatch_ref == "refs/heads/main"',
        'pr.get("number") == 71',
        'pr.get("state") == "closed"',
        'pr.get("merged") is True',
        'pr.get("draft") is False',
        'base.get("ref") == "main"',
        'head.get("ref") == "fix/vault-v2-cumulative-landing"',
        'repo.get("full_name") == repository',
        'merge_commit_sha = pr.get("merge_commit_sha")',
        'deploy_sha == merge_commit_sha',
        'trusted_base_sha = base.get("sha")',
        'main_ref.get("ref") == "refs/heads/main"',
        'main_object.get("type") == "commit"',
        'main_sha == deploy_sha',
    )
    for assertion in required_contract:
        assert assertion in workflow
    assert 'for label, side in (("base", base), ("head", head))' in workflow
    assert '"$API_URL/repos/$GH_REPOSITORY/git/ref/heads/main"' in workflow
    assert 'pr.get("state") == "open"' not in workflow
    assert 'head.get("sha") == deploy_sha' not in workflow
    assert 'base.get("sha") == main_sha' not in workflow


def test_isolated_deploy_binds_merge_success_to_the_trusted_ci_workflow():
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")

    assert "actions: read" in workflow
    assert "checks: read" not in workflow
    assert (
        '"$API_URL/repos/$GH_REPOSITORY/actions/workflows/ci.yml"' in workflow
    )
    assert 'workflow.get("path") == ".github/workflows/ci.yml"' in workflow
    assert 'workflow.get("state") == "active"' in workflow
    assert (
        'contents/.github/workflows/ci.yml?ref=$trusted_base_sha' in workflow
    )
    assert 'contents/.github/workflows/ci.yml?ref=$DEPLOY_SHA' in workflow
    assert 'if deployment["sha"] != base["sha"]:' in workflow
    assert (
        "actions/workflows/$trusted_workflow_id/runs?event=push&branch=main&"
        "head_sha=$DEPLOY_SHA&per_page=100" in workflow
    )
    assert "status=success" not in workflow
    assert '"workflow_id": int(os.environ["TRUSTED_WORKFLOW_ID"])' in workflow
    assert '"path": ".github/workflows/ci.yml"' in workflow
    assert '"event": "push"' in workflow
    assert '"head_branch": "main"' in workflow
    assert '"head_sha": os.environ["DEPLOY_SHA"]' in workflow
    assert '"status": "completed"' in workflow
    assert '"conclusion": "success"' in workflow
    assert 'for field in ("repository", "head_repository")' in workflow
    assert 'run.get("pull_requests")' not in workflow
    assert 'payload.get("total_count") != 1' in workflow
    assert "len(runs) != 1" in workflow


def test_isolated_deploy_does_not_accept_named_check_or_commit_status():
    workflow = _read(".github/workflows/fly-deploy-pr71-vault.yml")

    assert "/check-runs" not in workflow
    assert "/commits/$DEPLOY_SHA/status" not in workflow
    assert "Full tests and strategic benchmark" not in workflow
    assert 'app.get("slug")' not in workflow


def test_pr71_runbook_keeps_migrator_dsn_out_of_command_history():
    runbook = _read("docs/deployment/b3s_pr71_vault.md")

    assert "approved secret manager has" in runbook
    assert "already injected `B3S_MIGRATION_DATABASE_URL`" in runbook
    assert "B3S_MIGRATION_DATABASE_URL='" not in runbook
    assert 'test -n "${B3S_MIGRATION_DATABASE_URL:-}"' in runbook

def test_pr71_runbook_marks_workflow_post_merge_and_separately_authorized():
    runbook = _read("docs/deployment/b3s_pr71_vault.md")

    assert "**post-merge-only**" in runbook
    assert "authorizes a merge or a deployment" in runbook
    assert "deployment GO remain separate operator decisions" in runbook
    assert "`state=closed`, `merged=true`, and `draft=false`" in runbook
    assert "`merge_commit_sha`" in runbook
    assert "`refs/heads/main`" in runbook
    assert "custom deployment branch policy configured to allow only `main`" in runbook
    assert "currently has no deployment secrets" in runbook
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
