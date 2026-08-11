from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

_SCRIPT = (
    Path(__file__).parents[1]
    / "scripts/check_evidence_vault_c7_shadow_readiness.py"
)
_SPEC = importlib.util.spec_from_file_location("c7_shadow_readiness_cli", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
readiness_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(readiness_cli)
from src.services.evidence_vault_c7_shadow_readiness import (
    C7ShadowReadinessReason,
    C7ShadowReadinessResult,
    C7_SHADOW_READINESS_VERSION,
)
from src.services.evidence_vault_raw_provenance import (
    PUBLIC_KEY_REGISTRY_VERSION,
    PublicKeyRecord,
    PublicKeyRegistry,
    public_key_registry_fingerprint,
)


def _registry() -> PublicKeyRegistry:
    public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return PublicKeyRegistry(
        schema_version=PUBLIC_KEY_REGISTRY_VERSION,
        current_key_id="scanner-key-v1",
        keys={
            "scanner-key-v1": PublicKeyRecord(
                version=1,
                status="current",
                public_key_base64=base64.b64encode(public_key).decode("ascii"),
                signing_not_before="2025-01-01T00:00:00Z",
            )
        },
    )


def _write_registry(tmp_path: Path) -> tuple[Path, PublicKeyRegistry]:
    registry = _registry()
    path = tmp_path / "registry.json"
    path.write_text(registry.model_dump_json(), encoding="utf-8")
    return path, registry


def _arguments(path: Path, registry: PublicKeyRegistry) -> list[str]:
    return [
        "--domain",
        "example.com",
        "--registry-file",
        str(path),
        "--expected-registry-fingerprint",
        public_key_registry_fingerprint(registry),
    ]


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert set(payload) == {"schema_version", "ready", "reason"}
    assert payload["schema_version"] == C7_SHADOW_READINESS_VERSION
    return payload


def test_cli_uses_only_the_dedicated_runtime_read_identity_and_current_api(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, registry = _write_registry(tmp_path)
    runtime_dsn = "postgresql://runtime-read:do-not-print@example.test/b3s"
    created: list[tuple[str, PublicKeyRegistry, str]] = []
    domains: list[str] = []

    class Repository:
        def __init__(
            self,
            dsn: str,
            *,
            public_key_registry: PublicKeyRegistry,
            expected_public_key_registry_fingerprint: str,
        ) -> None:
            created.append(
                (
                    dsn,
                    public_key_registry,
                    expected_public_key_registry_fingerprint,
                )
            )

        def get_evidence_vault_c7_shadow_readiness(
            self,
            domain: str,
        ) -> C7ShadowReadinessResult:
            domains.append(domain)
            return C7ShadowReadinessResult(
                ready=True,
                reason=C7ShadowReadinessReason.VERIFIED_RAW_READY,
            )

    monkeypatch.setattr(readiness_cli, "EvidenceVaultC7ShadowRepository", Repository)

    assert readiness_cli.main(
        _arguments(path, registry),
        environ={
            "B3S_C7_RUNTIME_READ_DATABASE_URL": runtime_dsn,
            "DATABASE_URL": "postgresql://wrong:secret@wrong.test/b3s",
            "B3S_DATABASE_URL": "postgresql://wrong:secret@wrong.test/b3s",
        },
    ) == 0

    payload = _payload(capsys)
    assert payload == {
        "schema_version": C7_SHADOW_READINESS_VERSION,
        "ready": True,
        "reason": "verified_raw_ready",
    }
    assert created == [
        (runtime_dsn, registry, public_key_registry_fingerprint(registry))
    ]
    assert domains == ["example.com"]
    assert "do-not-print" not in json.dumps(payload)


def test_missing_dedicated_dsn_does_not_fall_back_and_is_sanitized(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, registry = _write_registry(tmp_path)

    class Repository:
        def __init__(self, *_args, **_kwargs) -> None:
            pytest.fail("a fallback database identity must not be constructed")

    monkeypatch.setattr(readiness_cli, "EvidenceVaultC7ShadowRepository", Repository)
    fallback = "postgresql://wrong:do-not-leak@example.test/b3s"

    assert readiness_cli.main(
        _arguments(path, registry),
        environ={"DATABASE_URL": fallback, "B3S_DATABASE_URL": fallback},
    ) == 2

    payload = _payload(capsys)
    assert payload == {
        "schema_version": C7_SHADOW_READINESS_VERSION,
        "ready": False,
        "reason": "storage_unavailable",
    }
    assert "do-not-leak" not in json.dumps(payload)


def test_mismatched_registry_pin_fails_before_connecting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _registry_model = _write_registry(tmp_path)
    dsn = "postgresql://runtime-read:never-connect@example.invalid/b3s"

    assert readiness_cli.main(
        [
            "--domain",
            "example.com",
            "--registry-file",
            str(path),
            "--expected-registry-fingerprint",
            "0" * 64,
            "--require-ready",
        ],
        environ={"B3S_C7_RUNTIME_READ_DATABASE_URL": dsn},
    ) == 1

    payload = _payload(capsys)
    assert payload == {
        "schema_version": C7_SHADOW_READINESS_VERSION,
        "ready": False,
        "reason": "verification_policy_invalid",
    }
    assert "never-connect" not in json.dumps(payload)


def test_require_ready_fails_closed_on_sanitized_database_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, registry = _write_registry(tmp_path)
    dsn = "postgresql://runtime-read:never-leak@example.test/b3s"

    class Repository:
        def __init__(self, supplied_dsn: str, **_kwargs) -> None:
            assert supplied_dsn == dsn

        def get_evidence_vault_c7_shadow_readiness(
            self,
            _domain: str,
        ) -> C7ShadowReadinessResult:
            raise RuntimeError(f"database failed at {dsn}; raw witness={{'secret': 1}}")

    monkeypatch.setattr(readiness_cli, "EvidenceVaultC7ShadowRepository", Repository)

    assert readiness_cli.main(
        [*_arguments(path, registry), "--require-ready"],
        environ={"B3S_C7_RUNTIME_READ_DATABASE_URL": dsn},
    ) == 1

    payload = _payload(capsys)
    assert payload == {
        "schema_version": C7_SHADOW_READINESS_VERSION,
        "ready": False,
        "reason": "storage_unavailable",
    }
    rendered = json.dumps(payload)
    assert "never-leak" not in rendered
    assert "witness" not in rendered


def test_false_readiness_is_observable_without_gate_and_nonzero_with_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, registry = _write_registry(tmp_path)

    class Repository:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_evidence_vault_c7_shadow_readiness(
            self,
            _domain: str,
        ) -> C7ShadowReadinessResult:
            return C7ShadowReadinessResult(
                ready=False,
                reason=C7ShadowReadinessReason.BRAND_CONTEXT_UNAVAILABLE,
            )

    monkeypatch.setattr(readiness_cli, "EvidenceVaultC7ShadowRepository", Repository)
    environment = {
        "B3S_C7_RUNTIME_READ_DATABASE_URL": (
            "postgresql://runtime-read@example.test/b3s"
        )
    }

    assert readiness_cli.main(
        _arguments(path, registry),
        environ=environment,
    ) == 0
    assert _payload(capsys)["reason"] == "brand_context_unavailable"

    assert readiness_cli.main(
        [*_arguments(path, registry), "--require-ready"],
        environ=environment,
    ) == 1
    assert _payload(capsys)["reason"] == "brand_context_unavailable"


@pytest.mark.parametrize(
    "malformed",
    [
        '{"schema_version":"one","schema_version":"two"}',
        '{"schema_version":NaN}',
    ],
)
def test_registry_validation_rejects_ambiguous_json_without_disclosure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    malformed: str,
) -> None:
    path = tmp_path / "registry-do-not-print.json"
    path.write_text(malformed, encoding="utf-8")

    assert readiness_cli.main(
        [
            "--domain",
            "example.com",
            "--registry-file",
            str(path),
            "--expected-registry-fingerprint",
            "0" * 64,
        ],
        environ={
            "B3S_C7_RUNTIME_READ_DATABASE_URL": (
                "postgresql://runtime-read:do-not-print@example.test/b3s"
            )
        },
    ) == 2

    payload = _payload(capsys)
    assert payload["reason"] == "verification_policy_invalid"
    assert "do-not-print" not in json.dumps(payload)


def test_registry_validation_rejects_symlinks_and_missing_arguments_publicly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, registry = _write_registry(tmp_path)
    link = tmp_path / "registry-link-do-not-print.json"
    link.symlink_to(path)
    environment = {
        "B3S_C7_RUNTIME_READ_DATABASE_URL": (
            "postgresql://runtime-read:do-not-print@example.test/b3s"
        )
    }

    assert readiness_cli.main(
        _arguments(link, registry),
        environ=environment,
    ) == 2
    assert _payload(capsys)["reason"] == "verification_policy_invalid"

    assert readiness_cli.main([], environ=environment) == 2
    assert _payload(capsys)["reason"] == "storage_unavailable"


def test_registry_file_rejects_group_or_world_writable_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, registry = _write_registry(tmp_path)
    path.chmod(0o666)

    assert readiness_cli.main(
        _arguments(path, registry),
        environ={
            "B3S_C7_RUNTIME_READ_DATABASE_URL": (
                "postgresql://runtime-read:do-not-print@example.test/b3s"
            )
        },
    ) == 2
    assert _payload(capsys)["reason"] == "verification_policy_invalid"


def test_registry_file_rejects_same_inode_mutation_during_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, registry = _write_registry(tmp_path)
    original = path.read_text(encoding="utf-8")
    changed = original.replace("scanner-key-v1", "scanner-key-v2")
    assert len(changed) == len(original)
    real_fstat = readiness_cli.os.fstat
    calls = 0

    def mutating_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text(changed, encoding="utf-8")
        return real_fstat(descriptor)

    monkeypatch.setattr(readiness_cli.os, "fstat", mutating_fstat)
    assert readiness_cli.main(
        _arguments(path, registry),
        environ={
            "B3S_C7_RUNTIME_READ_DATABASE_URL": (
                "postgresql://runtime-read:do-not-print@example.test/b3s"
            )
        },
    ) == 2
    assert _payload(capsys)["reason"] == "verification_policy_invalid"


def test_malformed_pin_and_invalid_dsn_port_are_usage_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, registry = _write_registry(tmp_path)
    arguments = _arguments(path, registry)
    arguments[-1] = "BANANA"

    assert readiness_cli.main(
        arguments,
        environ={
            "B3S_C7_RUNTIME_READ_DATABASE_URL": (
                "postgresql://runtime-read@example.test/b3s"
            )
        },
    ) == 2
    assert _payload(capsys)["reason"] == "verification_policy_invalid"

    assert readiness_cli.main(
        _arguments(path, registry),
        environ={
            "B3S_C7_RUNTIME_READ_DATABASE_URL": (
                "postgresql://runtime-read@example.test:notaport/b3s"
            )
        },
    ) == 2
    assert _payload(capsys)["reason"] == "storage_unavailable"
