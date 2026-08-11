from __future__ import annotations

from web import scan_runner


def test_vault_environment_alone_does_not_enable_operational_pipeline(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.delenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", raising=False)

    assert scan_runner._vault_operational_pipeline_enabled() is False


def test_operational_pipeline_requires_environment_and_exact_true_flag(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "true")
    assert scan_runner._vault_operational_pipeline_enabled() is True

    monkeypatch.setenv("BRAND3_ENVIRONMENT", "production")
    assert scan_runner._vault_operational_pipeline_enabled() is False

    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setenv("BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED", "1")
    assert scan_runner._vault_operational_pipeline_enabled() is False
