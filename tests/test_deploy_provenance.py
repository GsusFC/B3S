from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_container_persists_immutable_build_identity():
    dockerfile = _read("Dockerfile")

    assert "ARG B3S_BUILD_SHA=unknown" in dockerfile
    assert "B3S_BUILD_SHA=${B3S_BUILD_SHA}" in dockerfile
    assert 'org.opencontainers.image.revision="${B3S_BUILD_SHA}"' in dockerfile


def test_release_command_rejects_unidentified_images():
    fly_config = _read("fly.toml")

    assert "python scripts/verify_release_build.py &&" in fly_config


def test_deploy_workflow_builds_and_verifies_the_exact_commit():
    workflow = _read(".github/workflows/fly-deploy.yml")

    assert 'cancel-in-progress: false' in workflow
    assert '--build-arg B3S_BUILD_SHA="$DEPLOY_SHA"' in workflow
    assert 'https://b3s.fly.dev/health' in workflow
    assert 'if [ "$observed" = "$DEPLOY_SHA" ]' in workflow
    assert "setup-flyctl@master" not in workflow


def test_github_actions_are_pinned_to_immutable_commits():
    workflows = _read(".github/workflows/ci.yml") + _read(".github/workflows/fly-deploy.yml")

    assert "actions/checkout@v4" not in workflows
    assert "actions/setup-python@v5" not in workflows
    assert "setup-flyctl@master" not in workflows
