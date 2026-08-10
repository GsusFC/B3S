from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from deploy import vault_worker_supervisor as supervisor


def _valid_environment() -> dict[str, str]:
    raw_private_key = b"p" * 32
    public_key = (
        Ed25519PrivateKey.from_private_bytes(raw_private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    registry = {
        "schema_version": "evidence-vault-ed25519-public-key-registry-v1",
        "current_key_id": "key-1",
        "keys": {
            "key-1": {
                "version": 1,
                "status": "current",
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                "signing_not_before": "2026-01-01T00:00:00Z",
                "signing_ended_at": None,
            }
        },
    }
    return {
        supervisor.PRIVATE_KEY_ENV: base64.b64encode(raw_private_key).decode("ascii"),
        supervisor.PUBLIC_KEY_REGISTRY_ENV: json.dumps(registry, separators=(",", ":")),
        supervisor.INGEST_DSN_ENV: "postgresql://ingest:password@db.example/vault?sslmode=require&channel_binding=require",
        supervisor.EXA_API_KEY_ENV: "exa-worker-secret",
    }


def _write_secret_source(root: Path, values: dict[str, str]) -> None:
    root.mkdir(mode=0o700)
    for name, filename in supervisor.WORKER_SECRET_FILENAMES.items():
        path = root / filename
        path.write_text(values[name])
        path.chmod(0o600)


def test_secret_files_are_root_owned_and_strictly_validated(tmp_path: Path):
    values = _valid_environment()
    source = tmp_path / "worker-secrets"
    _write_secret_source(source, values)
    environment: dict[str, str] = {}

    secrets = supervisor._load_and_validate_secrets(
        environment,
        source_root=source,
    )

    assert environment == {}
    assert secrets.ingest_dsn.startswith("postgresql://")
    assert secrets.exa_api_key == "exa-worker-secret"


@pytest.mark.parametrize(
    ("name", "replacement", "error_code"),
    [
        (supervisor.PRIVATE_KEY_ENV, "not-base64", "worker_private_key_invalid"),
        (supervisor.PUBLIC_KEY_REGISTRY_ENV, "{}", "worker_public_key_registry_invalid"),
        (supervisor.INGEST_DSN_ENV, "https://db.example/vault", "worker_ingest_dsn_invalid"),
        (supervisor.EXA_API_KEY_ENV, "", "worker_exa_api_key_invalid"),
    ],
)
def test_malformed_worker_secret_values_fail_closed(name, replacement, error_code):
    environment = _valid_environment()
    environment[name] = replacement

    with pytest.raises(supervisor.SupervisorError, match=error_code):
        supervisor._validate_secret_values(environment)


@pytest.mark.parametrize("suffix", ["\n", "\r", "\t", "\x1f", "\x7f"])
def test_controls_are_rejected_without_echoing_values(suffix):
    environment = _valid_environment()
    environment[supervisor.EXA_API_KEY_ENV] += suffix

    with pytest.raises(supervisor.SupervisorError) as raised:
        supervisor._validate_secret_values(environment)

    assert "exa-worker-secret" not in str(raised.value)


def test_inline_secret_values_are_scrubbed_and_rejected_before_file_loading(
    tmp_path: Path,
) -> None:
    environment = _valid_environment()

    with pytest.raises(
        supervisor.SupervisorError,
        match="inline_worker_secret_forbidden",
    ):
        supervisor._load_and_validate_secrets(
            environment,
            source_root=tmp_path / "absent",
        )

    assert all(
        secret_name not in environment
        for secret_name in supervisor.WORKER_SECRET_ENV_NAMES
    )



def test_worker_secret_source_rejects_unsafe_modes_and_symlinks(
    tmp_path: Path,
) -> None:
    values = _valid_environment()
    source = tmp_path / "worker-secrets"
    _write_secret_source(source, values)
    private_key = source / supervisor.WORKER_SECRET_FILENAMES[
        supervisor.PRIVATE_KEY_ENV
    ]
    private_key.chmod(0o640)
    with pytest.raises(
        supervisor.SupervisorError,
        match="worker_secret_source_invalid",
    ):
        supervisor._load_and_validate_secrets({}, source_root=source)

    private_key.chmod(0o600)
    target = tmp_path / "replacement-key"
    target.write_text(values[supervisor.PRIVATE_KEY_ENV])
    target.chmod(0o600)
    private_key.unlink()
    private_key.symlink_to(target)
    with pytest.raises(
        supervisor.SupervisorError,
        match="worker_secret_source_invalid",
    ):
        supervisor._load_and_validate_secrets({}, source_root=source)


def test_worker_secret_source_rejects_same_inode_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _valid_environment()
    source = tmp_path / "worker-secrets"
    _write_secret_source(source, values)
    private_key = source / supervisor.WORKER_SECRET_FILENAMES[
        supervisor.PRIVATE_KEY_ENV
    ]
    real_fstat = supervisor.os.fstat
    regular_file_calls = 0

    def mutating_fstat(descriptor: int):
        nonlocal regular_file_calls
        info = real_fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            regular_file_calls += 1
            if regular_file_calls == 2:
                private_key.write_text("x" * info.st_size)
                info = real_fstat(descriptor)
        return info

    monkeypatch.setattr(supervisor.os, "fstat", mutating_fstat)
    with pytest.raises(
        supervisor.SupervisorError,
        match="worker_secret_source_invalid",
    ):
        supervisor._load_and_validate_secrets({}, source_root=source)

def test_private_key_must_match_current_public_registry_key():
    environment = _valid_environment()
    environment[supervisor.PRIVATE_KEY_ENV] = base64.b64encode(b"x" * 32).decode("ascii")

    with pytest.raises(supervisor.SupervisorError, match="worker_key_registry_mismatch"):
        supervisor._validate_secret_values(environment)


def test_materialized_files_and_directories_have_exact_ownership_and_modes(tmp_path):
    environment = _valid_environment()
    secrets = supervisor._validate_secret_values(environment)

    paths = supervisor._prepare_runtime(
        secrets,
        worker_uid=os.geteuid(),
        acquisition_gid=os.getegid(),
        run_root=tmp_path,
    )
    try:
        secret_directory = paths.secret_directory.stat()
        socket_directory = paths.socket_directory.stat()
        assert stat.S_IMODE(secret_directory.st_mode) == 0o700
        assert stat.S_IMODE(socket_directory.st_mode) == 0o750
        assert socket_directory.st_uid == os.geteuid()
        assert socket_directory.st_gid == os.getegid()
        assert not socket_directory.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        for secret_file in paths.secret_files:
            info = secret_file.stat()
            assert stat.S_IMODE(info.st_mode) == 0o600
            assert info.st_uid == os.geteuid()
            assert info.st_gid == os.getegid()
            assert secret_file.read_bytes()
            assert b"\n" not in secret_file.read_bytes()
    finally:
        supervisor._remove_secret_files(paths, strict=True)
        supervisor._remove_socket_directory(paths)

    assert not paths.secret_directory.exists()
    assert not paths.socket_directory.exists()


def test_worker_environment_is_allowlisted_and_web_environment_is_scrubbed(tmp_path):
    source = _valid_environment() | {
        "PATH": "/bin",
        "B3S_DATABASE_URL": "postgresql://web-secret",
        "EXA_API_KEY": "web-exa-key",
        "HTTP_PROXY": "http://proxy-secret",
    }
    socket_path = tmp_path / "worker.sock"

    worker_environment = supervisor._worker_environment(source)
    web_environment = supervisor._web_environment(source, socket_path)

    assert worker_environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "B3S_DATABASE_URL" not in worker_environment
    assert "EXA_API_KEY" not in worker_environment
    assert "HTTP_PROXY" not in worker_environment
    assert all(name not in worker_environment for name in supervisor.WORKER_SECRET_ENV_NAMES)
    assert all(name not in web_environment for name in supervisor.WORKER_SECRET_ENV_NAMES)
    assert web_environment["B3S_DATABASE_URL"] == "postgresql://web-secret"
    assert web_environment["EXA_API_KEY"] == "web-exa-key"
    assert web_environment[supervisor.WEB_SOCKET_ENV] == str(socket_path)


def test_socket_readiness_requires_worker_owned_group_socket_with_0660():
    class _RunningWorker:
        def poll(self):
            return None

    with tempfile.TemporaryDirectory(prefix="b3s-uds-", dir="/tmp") as directory:
        socket_path = Path(directory) / "acquisition.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o660)
        socket_info = socket_path.stat()
        try:
            supervisor._wait_for_socket(
                _RunningWorker(),  # type: ignore[arg-type]
                socket_path,
                worker_uid=socket_info.st_uid,
                acquisition_gid=socket_info.st_gid,
                relay=supervisor.SignalRelay(),
                timeout_seconds=0.2,
            )
            os.chmod(socket_path, 0o666)
            with pytest.raises(supervisor.SupervisorError, match="worker_socket_invalid"):
                supervisor._wait_for_socket(
                    _RunningWorker(),  # type: ignore[arg-type]
                    socket_path,
                    worker_uid=socket_info.st_uid,
                    acquisition_gid=socket_info.st_gid,
                    relay=supervisor.SignalRelay(),
                    timeout_seconds=0.2,
                )
        finally:
            server.close()


def test_supervisor_terminates_sibling_when_either_child_exits():
    exited = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(7)"],
        start_new_session=True,
    )
    living = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        assert supervisor._supervise((exited, living), supervisor.SignalRelay()) == 7
        assert living.poll() is not None
    finally:
        for child in (exited, living):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def test_deployment_contract_is_default_off_and_uses_distinct_identities():
    root = Path(__file__).resolve().parents[1]
    entrypoint = (root / "deploy" / "fly_entrypoint.sh").read_text()
    dockerfile = (root / "Dockerfile").read_text()

    assert '[ "${B3S_VAULT_WORKER_ENABLED:-}" = "true" ]' in entrypoint
    assert 'exec gosu b3s "$@"' in entrypoint
    assert "chmod 1777 /data" in entrypoint
    assert "chown -R" not in entrypoint
    assert "prepare_vault_volume.py" in entrypoint
    assert "b3s-worker" in dockerfile
    assert "b3s-acquisition" in dockerfile
    assert "usermod --append --groups b3s-acquisition b3s" in dockerfile
    assert "chown -R b3s:b3s /app /data" not in dockerfile
    assert "chown -R b3s:b3s /app/data /data /ms-playwright" in dockerfile
    assert "/usr/local/lib/b3s/prepare_vault_volume.py" in dockerfile
    assert "/usr/local/lib/b3s/vault_worker_supervisor.py" in dockerfile


@pytest.mark.parametrize(
    ("worker_flag", "release_command"),
    [
        ("", ""),
        ("false", ""),
        ("TRUE", ""),
        ("typo", ""),
        ("true", "1"),
    ],
)
def test_default_and_release_entrypoint_paths_scrub_worker_secrets(
    tmp_path: Path,
    worker_flag: str,
    release_command: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "deploy" / "fly_entrypoint.sh").read_text()
    setup_start = source.index("# The mounted encrypted volume root")
    setup_end = source.index("\n\nif [", setup_start)
    source = source[:setup_start] + ":" + source[setup_end:]
    entrypoint = tmp_path / "entrypoint"
    entrypoint.write_text(source)
    entrypoint.chmod(0o755)
    fake_gosu = tmp_path / "gosu"
    fake_gosu.write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    fake_gosu.chmod(0o755)
    environment = os.environ.copy()
    environment.update(_valid_environment())
    environment["PATH"] = f"{tmp_path}:{environment.get('PATH', '')}"
    environment["B3S_VAULT_WORKER_ENABLED"] = worker_flag
    environment["RELEASE_COMMAND"] = release_command

    completed = subprocess.run(
        [
            str(entrypoint),
            sys.executable,
            "-c",
            (
                "import json,os; "
                "print(json.dumps(sorted(k for k in os.environ "
                "if k.startswith('B3S_VAULT_WORKER_'))))"
            ),
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    remaining = json.loads(completed.stdout)
    assert all(name not in remaining for name in supervisor.WORKER_SECRET_ENV_NAMES)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://scanner:secret@db.example/vault",
        "postgresql://scanner:secret@db.example/vault?sslmode=disable",
        "postgresql://scanner:secret@db.example/vault?sslmode=verify-ca",
        "postgresql://scanner:secret@db.example/vault?sslmode=require&host=evil.example",
        "postgresql://scanner:secret@db.example/vault?sslmode=require&channel_binding=require&service=other",
        "postgresql://scanner:secret@db.example/vault?sslmode=require&channel_binding=require&options=-c%20neon.branch_id%3Dfake",
        "postgresql://db.example/vault?sslmode=require",
        "postgresql://scanner:secret@db.example/?sslmode=require&port=5432",
        "postgresql://scanner:secret@db.example:notaport/vault?sslmode=require",
    ],
)
def test_ingest_dsn_requires_explicit_secure_non_overridden_authority(dsn: str) -> None:
    environment = _valid_environment()
    environment[supervisor.INGEST_DSN_ENV] = dsn

    with pytest.raises(supervisor.SupervisorError, match="worker_ingest_dsn_invalid"):
        supervisor._validate_secret_values(environment)


def test_ingest_dsn_accepts_neon_tls_authority() -> None:
    environment = _valid_environment()
    environment[supervisor.INGEST_DSN_ENV] = (
        "postgresql://scanner:secret@db.example/vault"
        "?sslmode=require&channel_binding=require"
    )

    secrets = supervisor._validate_secret_values(environment)

    assert secrets.ingest_dsn.endswith("channel_binding=require")


def test_worker_target_is_required_and_passed_as_fixed_command_arguments(
    tmp_path: Path,
) -> None:
    environment = {
        supervisor.EXPECTED_DATABASE_ENV: "neondb",
        supervisor.EXPECTED_NEON_ENDPOINT_ENV: "db.example",
        supervisor.EXPECTED_NEON_PROJECT_ENV: "project-1",
        supervisor.EXPECTED_NEON_BRANCH_ENV: "branch-1",
    }
    target = supervisor._validated_worker_target(environment)
    paths = supervisor.RuntimePaths(
        secret_directory=tmp_path / "secrets",
        socket_directory=tmp_path / "socket",
        socket_path=tmp_path / "socket" / "acquisition.sock",
        private_key_file=tmp_path / "secrets" / "private-key.b64",
        registry_file=tmp_path / "secrets" / "registry.json",
        ingest_dsn_file=tmp_path / "secrets" / "ingest-dsn",
        exa_api_key_file=tmp_path / "secrets" / "exa-key",
    )

    command = supervisor._worker_command(paths, target)

    assert command[command.index("--expected-database") + 1] == "neondb"
    assert (
        command[command.index("--expected-database-hostname") + 1]
        == "db.example"
    )
    assert command[command.index("--expected-neon-project-id") + 1] == "project-1"
    assert command[command.index("--expected-neon-branch-id") + 1] == "branch-1"

    del environment[supervisor.EXPECTED_NEON_BRANCH_ENV]
    with pytest.raises(
        supervisor.SupervisorError,
        match="worker_target_identity_invalid",
    ):
        supervisor._validated_worker_target(environment)


def test_ingest_dsn_accepts_hostname_verified_tls_without_channel_binding() -> None:
    environment = _valid_environment()
    environment[supervisor.INGEST_DSN_ENV] = (
        "postgresql://scanner:secret@db.example/vault?sslmode=verify-full"
    )

    secrets = supervisor._validate_secret_values(environment)

    assert secrets.ingest_dsn.endswith("sslmode=verify-full")


def test_worker_ingest_dsn_must_match_pinned_endpoint_hostname() -> None:
    secrets = supervisor._validate_secret_values(_valid_environment())
    target = supervisor.WorkerTarget(
        database="vault",
        neon_endpoint_hostname="db.example",
        neon_project_id="project-1",
        neon_branch_id="branch-1",
    )
    supervisor._assert_ingest_target(secrets, target)

    wrong_target = supervisor.WorkerTarget(
        database="vault",
        neon_endpoint_hostname="other.example",
        neon_project_id="project-1",
        neon_branch_id="branch-1",
    )
    with pytest.raises(
        supervisor.SupervisorError,
        match="worker_ingest_target_mismatch",
    ):
        supervisor._assert_ingest_target(secrets, wrong_target)
