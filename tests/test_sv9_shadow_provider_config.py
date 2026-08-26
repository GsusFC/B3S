from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

import pytest


def _resolve(environ):
    from src.sv9.shadow_provider_config import resolve_shadow_provider_config

    return resolve_shadow_provider_config(environ)


def test_outcome_normalizes_misuse_and_redacts_repr():
    from src.sv9.shadow_provider_config import ShadowProviderConfig, ShadowProviderConfigOutcome

    class Secret:
        def __repr__(self):
            return "SECRET_OBJECT"

    valid = ShadowProviderConfig("KEY_SECRET", "https://provider.example/v1/chat/completions", 35)
    invalid = ShadowProviderConfig("KEY_SECRET", "https://ENDPOINT_SECRET", 35)
    for config, reason in (
        (None, None),
        (valid, "configuration_invalid_endpoint"),
        (invalid, None),
        (Secret(), None),
        (None, "SECRET_REASON"),
    ):
        outcome = ShadowProviderConfigOutcome(config, reason)
        assert outcome.config is None and outcome.reason_code == "configuration_invalid_endpoint"
        assert "SECRET" not in repr(outcome)
    assert ShadowProviderConfigOutcome(valid).config is valid
    assert "KEY_SECRET" not in repr(valid) and "ENDPOINT_SECRET" not in repr(invalid)


@pytest.mark.parametrize(
    ("base", "expected"),
    (
        (None, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
        ("HTTP://provider.example/v1", "HTTP://provider.example/v1/chat/completions"),
        ("https://3m.example/v1", "https://3m.example/v1/chat/completions"),
        ("https://provider.example./v1", "https://provider.example./v1/chat/completions"),
        ("https://provider.example:1/v1", "https://provider.example:1/v1/chat/completions"),
        ("https://provider.example:65535/v1", "https://provider.example:65535/v1/chat/completions"),
        ("http://127.0.0.1:8080/base", "http://127.0.0.1:8080/base/chat/completions"),
        ("https://[2001:db8::1]:443/v1%2Fsub", "https://[2001:db8::1]:443/v1%2Fsub/chat/completions"),
    ),
)
def test_valid_config_is_exact_deterministic_and_immutable(base, expected):
    environ = {"BRAND3_LLM_API_KEY": "KEY_SECRET"}
    if base is not None:
        environ["BRAND3_LLM_BASE_URL"] = base
    before = deepcopy(environ)
    first, second = _resolve(environ), _resolve(environ)
    assert first == second and environ == before and first.reason_code is None
    assert (first.config.api_key, first.config.endpoint, first.config.timeout) == ("KEY_SECRET", expected, 35)
    assert "KEY_SECRET" not in repr(first) and "provider" not in repr(first)


@pytest.mark.parametrize(
    ("environ", "key", "reason"),
    (
        ({}, None, "configuration_missing_api_key"),
        ({"BRAND3_LLM_API_KEY": "", "GEMINI_API_KEY": "LOWER"}, None, "configuration_missing_api_key"),
        ({"BRAND3_LLM_API_KEY": "KEY\u200b", "GEMINI_API_KEY": "LOWER"}, None, "configuration_missing_api_key"),
        ({"GEMINI_API_KEY": "GEMINI"}, "GEMINI", None),
        ({"GOOGLE_API_KEY": "GOOGLE"}, "GOOGLE", None),
        ({"OPENROUTER_API_KEY": "ROUTER"}, "ROUTER", None),
        ({"BRAND3_LLM_API_KEY": "A", "GEMINI_API_KEY": "B"}, "A", None),
        ({"BRAND3_LLM_API_KEY": " A"}, None, "configuration_missing_api_key"),
        ({"BRAND3_LLM_API_KEY": "A B"}, None, "configuration_missing_api_key"),
        ({"BRAND3_LLM_API_KEY": "A\u0085"}, None, "configuration_missing_api_key"),
        ({"BRAND3_LLM_API_KEY": "K" * 4097}, None, "configuration_missing_api_key"),
    ),
)
def test_credential_presence_precedence_and_validation(environ, key, reason):
    outcome = _resolve(environ)
    assert outcome.reason_code == reason
    assert (outcome.config.api_key if outcome.config else None) == key


@pytest.mark.parametrize(
    ("raw", "timeout", "reason"),
    (
        ("1", 1, None),
        ("120", 120, None),
        ("01", 1, None),
        ("0", None, "configuration_invalid_timeout"),
        ("121", None, "configuration_invalid_timeout"),
        ("+1", None, "configuration_invalid_timeout"),
        ("1.0", None, "configuration_invalid_timeout"),
        (" 1", None, "configuration_invalid_timeout"),
        ("", None, "configuration_invalid_timeout"),
        ("١", None, "configuration_invalid_timeout"),
        (True, None, "configuration_invalid_timeout"),
        (1, None, "configuration_invalid_timeout"),
    ),
)
def test_timeout_contract(raw, timeout, reason):
    outcome = _resolve({"BRAND3_LLM_API_KEY": "KEY", "BRAND3_LLM_CALL_TIMEOUT_SECONDS": raw})
    assert outcome.reason_code == reason
    assert (outcome.config.timeout if outcome.config else None) == timeout


@pytest.mark.parametrize(
    "endpoint",
    (
        "ftp://provider.example/v1",
        "https:/provider.example/v1",
        "https://",
        "https:///v1",
        "https://user@provider.example/v1",
        "https://user%40provider.example/v1",
        "https://user%2540provider.example/v1",
        "https://[v1.foo]/v1",
        "https://[fe80::1%25zone]/v1",
        "https://[::1/v1",
        "https://[::1]x/v1",
        "https://::1/v1",
        "https://provider.example::80/v1",
        "https://provider.example:/v1",
        "https://provider.example:0/v1",
        "https://provider.example:65536/v1",
        "https://provider.example:+80/v1",
        "https://provider.example:٨٠/v1",
        "https://_provider.example/v1",
        "https://-provider.example/v1",
        "https://provider-.example/v1",
        "https://provider..example/v1",
        f"https://{'a' * 64}.example/v1",
        "https://999.999.999.999/v1",
        "https://0x7f.0.0.1/v1",
        "https://0x7f000001/v1",
        "https://127.0x0.0.1/v1",
        "https://provider%2eexample/v1",
        "https://provider.example/v1?query",
        "https://provider.example/v1#fragment",
        "https://provider.example/v1/%",
        "https://provider.example/v1/%2",
        "https://provider.example/v1/%zz",
        "https://provider.example/v1/%ff",
        "https://provider.example/v1/%250a",
        "https://provider.example/v1/%255c",
        "https://provider.example/v1/%253f",
        "https://provider.example/v1/%2525252525252F",
        "https://provider.example/v1/é",
        "https://provider.example/v1/\u200b",
        "https://provider.example/v1/\u0085",
        "https://provider.example/v 1",
        "https://provider.example\\v1",
    ),
)
def test_explicit_parser_rejects_unsafe_or_legacy_numeric_endpoint(endpoint):
    outcome = _resolve({"BRAND3_LLM_API_KEY": "KEY", "BRAND3_LLM_BASE_URL": endpoint})
    assert outcome.config is None and outcome.reason_code == "configuration_invalid_endpoint"
    assert "KEY" not in repr(outcome) and endpoint not in repr(outcome)


def test_hostile_mapping_fails_closed_without_leakage():
    class Hostile(Mapping):
        def __getitem__(self, key):
            raise RuntimeError("MAPPING_SECRET")

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

    outcome = _resolve(Hostile())
    assert outcome.config is None and outcome.reason_code == "configuration_invalid_endpoint"
    assert "MAPPING_SECRET" not in repr(outcome)


def test_parser_preserves_stdlib_url_cache_state():
    from urllib.parse import urlsplit
    import src.sv9.shadow_provider_config as module

    assert "urlsplit" not in module.__dict__ and "urllib" not in module.__dict__
    before = urlsplit.cache_info()
    assert (
        _resolve({"BRAND3_LLM_API_KEY": "KEY", "BRAND3_LLM_BASE_URL": "https://parser-probe.example/v1"}).reason_code
        is None
    )
    assert urlsplit.cache_info() == before
