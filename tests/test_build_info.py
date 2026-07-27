from __future__ import annotations

import pytest

from src.build_info import UNKNOWN_BUILD_SHA, current_build_sha, require_deployable_build_sha


def test_current_build_sha_normalizes_a_full_commit(monkeypatch):
    monkeypatch.setenv("B3S_BUILD_SHA", "A" * 40)

    assert current_build_sha() == "a" * 40


@pytest.mark.parametrize("value", ["", "unknown", "abc123", "g" * 40])
def test_current_build_sha_rejects_non_commit_values(monkeypatch, value):
    monkeypatch.setenv("B3S_BUILD_SHA", value)

    assert current_build_sha() == UNKNOWN_BUILD_SHA


def test_release_requires_an_explicit_full_commit(monkeypatch):
    monkeypatch.delenv("B3S_BUILD_SHA", raising=False)

    with pytest.raises(RuntimeError, match="full 40-character Git commit SHA"):
        require_deployable_build_sha()
