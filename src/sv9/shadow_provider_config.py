"""Pure, explicit configuration contract for the SV9 shadow provider."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Literal, Mapping

_REASONS = frozenset(
    {
        "configuration_missing_api_key",
        "configuration_invalid_endpoint",
        "configuration_invalid_timeout",
    }
)
_KEYS = ("BRAND3_LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY")
_DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai"
_HEX = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True, slots=True, repr=False)
class ShadowProviderConfig:
    api_key: str
    endpoint: str
    timeout: int

    def __repr__(self) -> str:
        return "ShadowProviderConfig(api_key=<redacted>, endpoint=<redacted>, timeout=<redacted>)"


@dataclass(frozen=True, slots=True)
class ShadowProviderConfigOutcome:
    config: ShadowProviderConfig | None = None
    reason_code: (
        Literal[
            "configuration_missing_api_key",
            "configuration_invalid_endpoint",
            "configuration_invalid_timeout",
        ]
        | None
    ) = None

    def __post_init__(self) -> None:
        valid = _valid_config(self.config) and self.reason_code is None
        valid = valid or (self.config is None and type(self.reason_code) is str and self.reason_code in _REASONS)
        if not valid:
            object.__setattr__(self, "config", None)
            object.__setattr__(self, "reason_code", "configuration_invalid_endpoint")

    @classmethod
    def success(cls, config: ShadowProviderConfig) -> ShadowProviderConfigOutcome:
        return cls(config=config)

    @classmethod
    def failure(cls, reason: str) -> ShadowProviderConfigOutcome:
        return cls(
            reason_code=reason if type(reason) is str and reason in _REASONS else "configuration_invalid_endpoint"
        )


def _key(value: object) -> str | None:
    if type(value) is not str or not 0 < len(value) <= 4096:
        return None
    return value if all(char.isprintable() and not char.isspace() for char in value) else None


def _safe(value: str) -> bool:
    return all(char.isprintable() and not char.isspace() and char not in "\\?#" for char in value)


def _raw(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 2048 and value.isascii() and _safe(value)


def _decode(value: str) -> str | None:
    for _ in range(4):
        data, changed, index = bytearray(), False, 0
        while index < len(value):
            char = value[index]
            if char == "%":
                if index + 2 >= len(value) or value[index + 1] not in _HEX or value[index + 2] not in _HEX:
                    return None
                data.append(int(value[index + 1 : index + 3], 16))
                changed, index = True, index + 3
            else:
                data.extend(char.encode("utf-8"))
                index += 1
        try:
            decoded = bytes(data).decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None
        if not _safe(decoded):
            return None
        if not changed:
            return value
        value = decoded
    return None


def _port(value: str) -> bool:
    return 0 < len(value) <= 5 and value.isascii() and value.isdecimal() and 0 < int(value) <= 65535


def _host(value: str) -> bool:
    if not value:
        return False
    name = value[:-1] if value.endswith(".") else value
    if not name or name.endswith(".") or len(name) > 253:
        return False
    try:
        ipaddress.IPv4Address(name)
        return True
    except ValueError:
        if all(
            label.isdecimal()
            or (label[:2].lower() == "0x" and len(label) > 2 and all(char in _HEX for char in label[2:]))
            for label in name.split(".")
        ):
            return False
        return all(
            0 < len(label) <= 63
            and label[0] != "-"
            and label[-1] != "-"
            and all(char.isalnum() or char == "-" for char in label)
            for label in name.split(".")
        )


def _authority(raw: str) -> bool:
    if not raw or _decode(raw) != raw or "@" in raw:
        return False
    if raw.startswith("["):
        if raw.count("[") != 1 or raw.count("]") != 1:
            return False
        closing = raw.find("]")
        address, tail = raw[1:closing], raw[closing + 1 :]
        if not address or "%" in address or tail and (not tail.startswith(":") or not _port(tail[1:])):
            return False
        try:
            ipaddress.IPv6Address(address)
            return True
        except ValueError:
            return False
    if "[" in raw or "]" in raw or raw.count(":") > 1:
        return False
    host, separator, port = raw.partition(":")
    return _host(host) and (not separator or _port(port))


def _base(value: object) -> bool:
    if not _raw(value):
        return False
    scheme, separator, rest = value.partition("://")
    if not separator or scheme.lower() not in {"http", "https"}:
        return False
    authority, slash, path = rest.partition("/")
    return _authority(authority) and _decode(path) is not None


def _endpoint(value: object) -> str | None:
    if not _base(value):
        return None
    endpoint = value.rstrip("/") + "/chat/completions"
    return endpoint if _base(endpoint) else None


def _credential(environ: Mapping[str, object]) -> str | None:
    for name in _KEYS:
        if name in environ:
            return _key(environ[name])
    return None


def _timeout(value: object) -> int | None:
    if type(value) is not str or not 0 < len(value) <= 3 or not value.isascii() or not value.isdecimal():
        return None
    timeout = int(value)
    return timeout if 0 < timeout <= 120 else None


def _valid_config(config: object) -> bool:
    return (
        type(config) is ShadowProviderConfig
        and _key(config.api_key) is not None
        and type(config.endpoint) is str
        and config.endpoint.endswith("/chat/completions")
        and _base(config.endpoint)
        and type(config.timeout) is int
        and 0 < config.timeout <= 120
    )


def resolve_shadow_provider_config(environ: Mapping[str, object]) -> ShadowProviderConfigOutcome:
    """Resolve a detached environment snapshot without I/O or diagnostics."""
    try:
        key = _credential(environ)
        if key is None:
            return ShadowProviderConfigOutcome.failure("configuration_missing_api_key")
        endpoint = _endpoint(environ["BRAND3_LLM_BASE_URL"] if "BRAND3_LLM_BASE_URL" in environ else _DEFAULT_ENDPOINT)
        if endpoint is None:
            return ShadowProviderConfigOutcome.failure("configuration_invalid_endpoint")
        timeout = _timeout(
            environ["BRAND3_LLM_CALL_TIMEOUT_SECONDS"] if "BRAND3_LLM_CALL_TIMEOUT_SECONDS" in environ else "35"
        )
        if timeout is None:
            return ShadowProviderConfigOutcome.failure("configuration_invalid_timeout")
        return ShadowProviderConfigOutcome.success(ShadowProviderConfig(key, endpoint, timeout))
    except Exception:
        return ShadowProviderConfigOutcome.failure("configuration_invalid_endpoint")
