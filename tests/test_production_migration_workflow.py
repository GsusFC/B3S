from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_production_migration_workflow_is_manual_main_only_and_target_pinned() -> None:
    workflow = _read(".github/workflows/b3s-production-migration.yml")

    assert "workflow_dispatch:" in workflow
    assert 'description: "Exact current main SHA to migrate"' in workflow
    assert "workflow_run:" not in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "name: production-migration" in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert 'MIGRATION_SHA: ${{ inputs.migration_sha }}' in workflow
    assert 'DISPATCH_SHA: ${{ github.sha }}' in workflow
    assert 'DISPATCH_REF: ${{ github.ref }}' in workflow
    assert "$API_URL/repos/$GH_REPOSITORY/git/ref/heads/main" in workflow
    assert 'test "$MIGRATION_SHA" = "$live_main"' in workflow
    assert 'test "$MIGRATION_SHA" = "$DISPATCH_SHA"' in workflow
    assert 'test "$DISPATCH_SHA" = "$live_main"' in workflow
    assert "Checkout attested source without credentials" in workflow
    assert 'git fetch --depth=1 origin "$MIGRATION_SHA"' in workflow
    assert "actions/checkout@" not in workflow
    assert 'test "$(git rev-parse HEAD)" = "$MIGRATION_SHA"' in workflow
    assert 'test "$(git rev-parse HEAD)" = "$DISPATCH_SHA"' in workflow
    assert "run_b3s_production_migration.py" in workflow
    assert "--target-profile b3s-production" not in workflow
    assert "--database-url" not in workflow
    assert "requirements-b3s-production-migration.txt" in workflow
    assert "--require-hashes" in workflow
    assert "--only-binary=:all:" in workflow
    assert "--no-deps" in workflow
    assert "pip install --disable-pip-version-check ." not in workflow


def test_production_migration_workflow_exposes_no_secret_before_pinned_checkout() -> None:
    workflow = _read(".github/workflows/b3s-production-migration.yml")

    attest = workflow.index("Attest live main before checkout")
    checkout = workflow.index("Checkout attested source without credentials")
    source_check = workflow.index("Verify checked-out migration source")
    migration_secret = workflow.index("secrets.B3S_MIGRATION_DATABASE_URL")
    trust_root_secret = workflow.index("secrets.B3S_PRODUCTION_TLS_ROOT_CERT")
    controller = workflow.index("run_b3s_production_migration.py")
    assert attest < checkout < source_check < migration_secret < trust_root_secret < controller
    assert workflow.count("secrets.") == 2
    assert workflow.count("\n          B3S_MIGRATION_DATABASE_URL:") == 1
    assert "GH_TOKEN" not in workflow
    assert "github.token" not in workflow
    assert "FLY_API_TOKEN" not in workflow
    assert "flyctl" not in workflow.lower()
    assert "fly secrets" not in workflow.lower()
    assert "B3S_DATABASE_URL" not in workflow
    assert "PGSSLROOTCERT" not in workflow


def test_production_migration_workflow_requires_protected_identity_contract() -> None:
    workflow = _read(".github/workflows/b3s-production-migration.yml")

    for name in (
        "B3S_PRODUCTION_DATABASE",
        "B3S_PRODUCTION_HOST",
        "B3S_PRODUCTION_NEON_PROJECT_ID",
        "B3S_PRODUCTION_NEON_BRANCH_ID",
        "B3S_PRODUCTION_MIGRATION_ROLE",
    ):
        assert f"{name}: ${{{{ vars.{name} }}}}" in workflow
    assert "B3S_MIGRATION_DATABASE_URL: ${{ secrets.B3S_MIGRATION_DATABASE_URL }}" in workflow
    assert "B3S_PRODUCTION_TLS_ROOT_CERT: ${{ secrets.B3S_PRODUCTION_TLS_ROOT_CERT }}" in workflow


def test_existing_fly_deploy_remains_select_only_with_no_migration_ddl() -> None:
    deploy = _read(".github/workflows/fly-deploy.yml")

    assert "python scripts/migrate_b3s_history_postgres.py" not in deploy
    assert "b3s-production" not in deploy
    assert "B3S_MIGRATION_DATABASE_URL" not in deploy
    assert "CREATE SCHEMA" not in deploy
    assert "ALTER TABLE" not in deploy
    assert "DROP TABLE" not in deploy
    assert "verify_b3s_history_postgres.py" not in deploy
    assert "Database DDL is deliberately absent" in deploy


def test_production_migration_runbook_keeps_credentials_out_of_source() -> None:
    runbook = _read("docs/deployment/b3s_production_migration.md")

    assert "`production-migration`" in runbook
    assert "B3S_MIGRATION_DATABASE_URL" in runbook
    assert "B3S_PRODUCTION_TLS_ROOT_CERT" in runbook
    assert "temporary environment secret" in runbook
    assert "`--database-url`" in runbook
    assert "`fly deploy`" in runbook
    assert "not authorized" in runbook
    assert "B3S_MIGRATION_DATABASE_URL='" not in runbook
    assert "postgresql://" not in runbook
    assert "PGSSLROOTCERT" in runbook
    assert "system trust-store discovery" in runbook


def test_production_migration_dependency_lock_is_hash_pinned_and_minimal() -> None:
    source = _read("requirements-b3s-production-migration.in")
    lock = _read("requirements-b3s-production-migration.txt")

    assert source == "psycopg[binary]==3.2.10\n"
    assert "psycopg==3.2.10" in lock
    assert "psycopg-binary==3.2.10" in lock
    assert "typing-extensions==4.16.0" in lock
    assert lock.count("--hash=sha256:") >= 3
    assert ">=" not in lock
    assert "<" not in lock
