from __future__ import annotations

import pytest

from src.services.evidence_vault_c7_cutover import (
    ALLOWLIST_ENV,
    EMERGENCY_DENY_ENV,
    ENVIRONMENT_ENV,
    MASTER_ENABLE_ENV,
    EvidenceVaultC7CutoverConfig,
    EvidenceVaultC7CutoverError,
    canonicalize_c7_brand,
    current_c7_cutover_decision,
    decide_c7_cutover,
    load_c7_runtime_projection,
)


def _environment(**overrides: str) -> dict[str, str]:
    return {
        ENVIRONMENT_ENV: "vault",
        MASTER_ENABLE_ENV: "true",
        EMERGENCY_DENY_ENV: "false",
        ALLOWLIST_ENV: "example.com",
        **overrides,
    }


@pytest.mark.parametrize("environment", ["", "production", "Vault", " vault ", "unknown"])
def test_only_exact_vault_environment_can_enable(environment: str) -> None:
    decision = decide_c7_cutover(
        "example.com",
        environment=environment,
        config=EvidenceVaultC7CutoverConfig.from_environ(_environment()),
    )

    assert decision.enabled is False
    assert decision.reason == "environment_not_vault"


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        (MASTER_ENABLE_ENV, "false", "master_disabled"),
        (MASTER_ENABLE_ENV, "1", "master_enable_invalid"),
        (MASTER_ENABLE_ENV, "TRUE", "master_enable_invalid"),
        (EMERGENCY_DENY_ENV, "true", "emergency_deny_engaged"),
        (EMERGENCY_DENY_ENV, "0", "emergency_deny_invalid"),
        (EMERGENCY_DENY_ENV, "FALSE", "emergency_deny_invalid"),
    ],
)
def test_boolean_controls_are_exact_and_fail_closed(
    key: str,
    value: str,
    reason: str,
) -> None:
    env = _environment(**{key: value})
    decision = current_c7_cutover_decision("example.com", environ=env)

    assert decision.enabled is False
    assert decision.reason == reason


def test_missing_controls_default_to_master_off_and_emergency_deny_on() -> None:
    decision = current_c7_cutover_decision("example.com", environ={})

    assert decision.enabled is False
    assert decision.reason == "emergency_deny_engaged"


def test_emergency_deny_has_precedence_over_every_other_error() -> None:
    decision = current_c7_cutover_decision(
        "not a domain",
        environ={
            ENVIRONMENT_ENV: "production",
            MASTER_ENABLE_ENV: "invalid",
            EMERGENCY_DENY_ENV: "true",
            ALLOWLIST_ENV: "*,example.com,",
        },
    )

    assert decision.reason == "emergency_deny_engaged"
    malformed = current_c7_cutover_decision(
        "https://[example.com",
        environ={
            ENVIRONMENT_ENV: "production",
            MASTER_ENABLE_ENV: "invalid",
            EMERGENCY_DENY_ENV: "true",
            ALLOWLIST_ENV: "*,example.com,",
        },
    )
    assert malformed.reason == "emergency_deny_engaged"


@pytest.mark.parametrize(
    "allowlist",
    [
        "",
        " ",
        ",",
        "example.com,",
        "example.com,,other.com",
        "*",
        "*.example.com",
        "https://example.com",
        "user@example.com",
        "example.com/path",
        "-bad.example",
        "example.com:443",
        "127.0.0.1",
        "127.1",
        "0x7f.1",
        "localhost",
        "example.com,bad token",
        "xn--bcher-kva.example",
        "bücher.example",
    ],
)
def test_empty_or_partly_malformed_allowlist_denies_every_brand(
    allowlist: str,
) -> None:
    decision = current_c7_cutover_decision(
        "example.com",
        environ=_environment(**{ALLOWLIST_ENV: allowlist}),
    )

    assert decision.enabled is False
    assert decision.reason in {"allowlist_empty", "allowlist_invalid"}


@pytest.mark.parametrize(
    "brand",
    [
        "EXAMPLE.com.",
        "www.example.com",
        "https://EXAMPLE.com./path?q=1",
    ],
)
def test_exact_canonical_brand_can_enable_without_exposing_allowlist(
    brand: str,
) -> None:
    decision = current_c7_cutover_decision(brand, environ=_environment())

    assert decision.enabled is True
    assert decision.reason == "enabled"
    assert decision.canonical_brand == "example.com"
    assert "allowlist" not in decision.to_dict()


@pytest.mark.parametrize(
    "brand",
    [
        "sub.example.com",
        "example.com.evil",
        "https://user@example.com",
        "https://example.com:443",
        "127.0.0.1",
        "127.1",
        "0177.0.0.1",
        "0x7f.1",
        "01.02.03.04",
        "xn--bcher-kva.example",
        "bücher.example",
        "Kexample.com",
        "https://Kexample.com",
        "https://[v1.example.com]",
        "https://[example.com",
        "https://example.com]",
        "https://[]",
        "https://example.com:",
        "https://example.com:/",
        "https://example.com:?query",
        "https://example.com:#fragment",
        "https://example.com／evil",
    ],
)
def test_membership_is_exact_and_ambiguous_authorities_are_rejected(brand: str) -> None:
    decision = current_c7_cutover_decision(brand, environ=_environment())

    assert decision.enabled is False
    assert decision.reason in {"brand_not_allowlisted", "brand_identity_invalid"}


def test_duplicate_canonical_allowlist_entries_are_idempotent() -> None:
    env = _environment(
        **{ALLOWLIST_ENV: "EXAMPLE.com.,www.example.com,example.com"}
    )
    config = EvidenceVaultC7CutoverConfig.from_environ(env)

    assert config.allowlist_valid is True
    assert config.allowlisted_domains == frozenset({"example.com"})
    assert current_c7_cutover_decision("example.com", environ=env).enabled is True


@pytest.mark.parametrize(
    "value",
    [
        ".example.com",
        "example.com..",
        "www.www.example.com",
        "example..com",
        "a",
        "a_b.com",
    ],
)
def test_strict_brand_parser_rejects_grouping_grade_identifiers(value: str) -> None:
    with pytest.raises(EvidenceVaultC7CutoverError):
        canonicalize_c7_brand(value)


class _Repository:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_evidence_vault_c7_runtime_snapshot(self, *_args, **_kwargs):
        self.calls.append("snapshot")
        return None


def test_denied_runtime_projection_performs_zero_snapshot_reads(
    monkeypatch,
) -> None:
    repository = _Repository()
    for key, value in _environment(**{MASTER_ENABLE_ENV: "false"}).items():
        monkeypatch.setenv(key, value)

    result = load_c7_runtime_projection(repository, "example.com")

    assert result is None
    assert repository.calls == []


def test_enabled_runtime_projection_stays_unavailable_without_atomic_snapshot(
    monkeypatch,
) -> None:
    repository = _Repository()
    for key, value in _environment().items():
        monkeypatch.setenv(key, value)

    result = load_c7_runtime_projection(repository, "example.com")

    assert result is None
    assert repository.calls == ["snapshot"]


def test_emergency_switch_flip_during_snapshot_denies_presentation(
    monkeypatch,
) -> None:
    for key, value in _environment().items():
        monkeypatch.setenv(key, value)

    class KillDuringSnapshot(_Repository):
        def get_evidence_vault_c7_runtime_snapshot(self, *_args, **_kwargs):
            self.calls.append("snapshot")
            monkeypatch.setenv(EMERGENCY_DENY_ENV, "true")
            return {
                "canonical_memory": {},
                "score_evaluation": {},
                "active_group_attestation": {},
            }

    repository = KillDuringSnapshot()
    result = load_c7_runtime_projection(repository, "example.com")

    assert result is None
    assert repository.calls == ["snapshot"]
