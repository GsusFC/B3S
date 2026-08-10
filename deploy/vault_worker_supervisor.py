#!/usr/bin/env python3
"""Root-only supervisor for the optional same-Machine Vault worker.

The normal container entrypoint does not invoke this module.  When explicitly
selected, it copies four fixed root-owned volume secrets into short-lived
worker-owned files, starts the worker under its own uid, and starts the web child
only after the worker has created the expected Unix socket.
"""

from __future__ import annotations

import base64
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.services.evidence_vault_raw_provenance import PublicKeyRegistry


_APP_ROOT = Path("/app")
_RUN_ROOT = Path("/run")
_SECRET_SOURCE_ROOT = Path("/data/b3s-vault-worker")
_WORKER_USER = "b3s-worker"
_WEB_USER = "b3s"
_WEB_HOME = "/home/b3s"
_LIBPQ_ENV_NAMES = (
    "PGOPTIONS",
    "PGSERVICE",
    "PGHOST",
    "PGPORT",
    "PGHOSTADDR",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSSLMODE",
    "PGCHANNELBINDING",
    "PGSERVICEFILE",
    "PGSYSCONFDIR",
    "PGREQUIRESSL",
    "PGTARGETSESSIONATTRS",
    "PGAPPNAME",
)
_ACQUISITION_GROUP = "b3s-acquisition"
_SOCKET_FILENAME = "acquisition.sock"
_SOCKET_MODE = 0o660
_SOCKET_DIRECTORY_MODE = 0o750
_SECRET_DIRECTORY_MODE = 0o700
_SECRET_FILE_MODE = 0o600
_STARTUP_TIMEOUT_SECONDS = 30.0
_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_MAX_SECRET_BYTES = 16_384
_MAX_REGISTRY_BYTES = 131_072

PRIVATE_KEY_ENV = "B3S_VAULT_WORKER_PRIVATE_KEY_B64"
PUBLIC_KEY_REGISTRY_ENV = "B3S_VAULT_WORKER_PUBLIC_KEY_REGISTRY_JSON"
INGEST_DSN_ENV = "B3S_VAULT_WORKER_INGEST_DSN"
EXA_API_KEY_ENV = "B3S_VAULT_WORKER_EXA_API_KEY"
WORKER_SECRET_ENV_NAMES = (
    PRIVATE_KEY_ENV,
    PUBLIC_KEY_REGISTRY_ENV,
    INGEST_DSN_ENV,
    EXA_API_KEY_ENV,
)
WORKER_SECRET_FILENAMES = {
    PRIVATE_KEY_ENV: "private-key.b64",
    PUBLIC_KEY_REGISTRY_ENV: "public-key-registry.json",
    INGEST_DSN_ENV: "ingest-dsn",
    EXA_API_KEY_ENV: "exa-api-key",
}
WEB_SOCKET_ENV = "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH"
EXPECTED_DATABASE_ENV = "B3S_EXPECTED_DATABASE_NAME"
EXPECTED_NEON_ENDPOINT_ENV = "B3S_EXPECTED_NEON_ENDPOINT_HOST"
EXPECTED_NEON_PROJECT_ENV = "B3S_EXPECTED_NEON_PROJECT_ID"
EXPECTED_NEON_BRANCH_ENV = "B3S_EXPECTED_NEON_BRANCH_ID"


class SupervisorError(RuntimeError):
    """An expected startup failure with a value-free diagnostic code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class WorkerSecrets:
    private_key_b64: str
    public_key_registry_json: str
    ingest_dsn: str
    exa_api_key: str


@dataclass(frozen=True)
class WorkerTarget:
    database: str
    neon_endpoint_hostname: str
    neon_project_id: str
    neon_branch_id: str


@dataclass(frozen=True)
class RuntimePaths:
    secret_directory: Path
    socket_directory: Path
    socket_path: Path
    private_key_file: Path
    registry_file: Path
    ingest_dsn_file: Path
    exa_api_key_file: Path

    @property
    def secret_files(self) -> tuple[Path, ...]:
        return (
            self.private_key_file,
            self.registry_file,
            self.ingest_dsn_file,
            self.exa_api_key_file,
        )


class SignalRelay:
    """Record and forward container signals to isolated child process groups."""

    def __init__(self) -> None:
        self.children: list[subprocess.Popen[Any]] = []
        self.received: int | None = None
        self._prior_handlers: dict[int, Any] = {}

    def install(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            self._prior_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)

    def restore(self) -> None:
        for signum, handler in self._prior_handlers.items():
            signal.signal(signum, handler)
        self._prior_handlers.clear()

    def add(self, child: subprocess.Popen[Any]) -> None:
        self.children.append(child)
        if self.received is not None:
            _signal_child(child, self.received)

    def _handle(self, signum: int, _frame: Any) -> None:
        if self.received is None:
            self.received = signum
        for child in tuple(self.children):
            _signal_child(child, signum)


def _load_and_validate_secrets(
    environment: MutableMapping[str, str],
    *,
    source_root: Path = _SECRET_SOURCE_ROOT,
) -> WorkerSecrets:
    """Load fixed root-owned files and reject every inline secret value."""

    inline_present = False
    for name in WORKER_SECRET_ENV_NAMES:
        if environment.pop(name, None) is not None:
            inline_present = True
    if inline_present:
        raise SupervisorError("inline_worker_secret_forbidden")
    values = _read_worker_secret_sources(source_root)
    return _validate_secret_values(values)


def _read_worker_secret_sources(source_root: Path) -> dict[str, str]:
    maximums = {
        PRIVATE_KEY_ENV: _MAX_SECRET_BYTES,
        PUBLIC_KEY_REGISTRY_ENV: _MAX_REGISTRY_BYTES,
        INGEST_DSN_ENV: _MAX_SECRET_BYTES,
        EXA_API_KEY_ENV: _MAX_SECRET_BYTES,
    }
    try:
        before_directory = source_root.lstat()
        _validate_secret_source_directory(before_directory)
        directory_fd = os.open(
            source_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened_directory = os.fstat(directory_fd)
            _validate_secret_source_directory(opened_directory)
            if _file_identity(opened_directory) != _file_identity(before_directory):
                raise ValueError("secret source directory changed")
            values: dict[str, str] = {}
            for name, filename in WORKER_SECRET_FILENAMES.items():
                descriptor = os.open(
                    filename,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(descriptor)
                    _validate_secret_source_file(
                        opened,
                        maximum=maximums[name],
                    )
                    raw = os.read(descriptor, maximums[name] + 1)
                    trailing = os.read(descriptor, 1)
                    after = os.fstat(descriptor)
                    _validate_secret_source_file(
                        after,
                        maximum=maximums[name],
                    )
                    if _file_identity(after) != _file_identity(opened):
                        raise ValueError("secret source file changed")
                finally:
                    os.close(descriptor)
                if trailing or len(raw) != opened.st_size:
                    raise ValueError("secret source file changed")
                values[name] = raw.decode("utf-8", errors="strict")
            return values
        finally:
            os.close(directory_fd)
    except Exception:
        raise SupervisorError("worker_secret_source_invalid") from None


def _validate_secret_source_directory(info: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != _SECRET_DIRECTORY_MODE
    ):
        raise ValueError("unsafe worker secret source directory")


def _validate_secret_source_file(info: os.stat_result, *, maximum: int) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != _SECRET_FILE_MODE
        or not 0 < info.st_size <= maximum
    ):
        raise ValueError("unsafe worker secret source file")


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _validate_secret_values(values: Mapping[str, str]) -> WorkerSecrets:
    private_key = _validated_text(
        values.get(PRIVATE_KEY_ENV),
        field="private_key",
        maximum=_MAX_SECRET_BYTES,
    )
    registry_text = _validated_text(
        values.get(PUBLIC_KEY_REGISTRY_ENV),
        field="public_key_registry",
        maximum=_MAX_REGISTRY_BYTES,
    )
    ingest_dsn = _validated_text(
        values.get(INGEST_DSN_ENV),
        field="ingest_dsn",
        maximum=_MAX_SECRET_BYTES,
    )
    exa_api_key = _validated_text(
        values.get(EXA_API_KEY_ENV),
        field="exa_api_key",
        maximum=_MAX_SECRET_BYTES,
    )

    try:
        decoded_private_key = base64.b64decode(private_key, validate=True)
        if len(decoded_private_key) != 32:
            raise ValueError("wrong key length")
        if base64.b64encode(decoded_private_key).decode("ascii") != private_key:
            raise ValueError("non-canonical base64")
    except Exception:
        raise SupervisorError("worker_private_key_invalid") from None

    try:
        registry_value = json.loads(
            registry_text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        _reject_decoded_controls(registry_value)
        registry = PublicKeyRegistry.model_validate(registry_value, strict=True)
    except Exception:
        raise SupervisorError("worker_public_key_registry_invalid") from None

    try:
        actual_public_key = Ed25519PrivateKey.from_private_bytes(
            decoded_private_key
        ).public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        expected_public_key = base64.b64decode(
            registry.keys[registry.current_key_id].public_key_base64,
            validate=True,
        )
        if actual_public_key != expected_public_key:
            raise ValueError("key mismatch")
    except Exception:
        raise SupervisorError("worker_key_registry_mismatch") from None

    try:
        _validate_ingest_dsn(ingest_dsn)
    except Exception:
        raise SupervisorError("worker_ingest_dsn_invalid") from None

    return WorkerSecrets(
        private_key_b64=private_key,
        public_key_registry_json=registry_text,
        ingest_dsn=ingest_dsn,
        exa_api_key=exa_api_key,
    )


def _validated_text(value: str | None, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SupervisorError(f"worker_{field}_invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise SupervisorError(f"worker_{field}_invalid") from None
    if len(encoded) > maximum or any(not character.isprintable() for character in value):
        raise SupervisorError(f"worker_{field}_invalid")
    return value


def _validate_ingest_dsn(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or not parsed.username
        or not parsed.password
        or not parsed.path
        or parsed.path == "/"
        or parsed.fragment
    ):
        raise ValueError("invalid PostgreSQL DSN")
    _ = parsed.port
    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    keys = [key for key, _item in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate PostgreSQL DSN parameter")
    forbidden_overrides = {
        "host",
        "hostaddr",
        "user",
        "password",
        "dbname",
        "port",
        "service",
        "servicefile",
        "options",
    }
    if forbidden_overrides.intersection(keys):
        raise ValueError("PostgreSQL DSN authority override is forbidden")
    ssl_modes = [item for key, item in pairs if key == "sslmode"]
    channel_bindings = [item for key, item in pairs if key == "channel_binding"]
    authenticated_tls = ssl_modes == ["verify-full"] or (
        ssl_modes == ["require"] and channel_bindings == ["require"]
    )
    if not authenticated_tls:
        raise ValueError("PostgreSQL DSN must authenticate TLS")


def _validated_worker_target(environment: MutableMapping[str, str]) -> WorkerTarget:
    values: list[str] = []
    for name in (
        EXPECTED_DATABASE_ENV,
        EXPECTED_NEON_ENDPOINT_ENV,
        EXPECTED_NEON_PROJECT_ENV,
        EXPECTED_NEON_BRANCH_ENV,
    ):
        value = _validated_text(
            environment.get(name),
            field="target_identity",
            maximum=255,
        )
        if re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None:
            raise SupervisorError("worker_target_identity_invalid")
        values.append(value)
    return WorkerTarget(
        database=values[0],
        neon_endpoint_hostname=values[1].lower(),
        neon_project_id=values[2],
        neon_branch_id=values[3],
    )


def _assert_ingest_target(secrets: WorkerSecrets, target: WorkerTarget) -> None:
    try:
        hostname = urlsplit(secrets.ingest_dsn).hostname
    except Exception:
        hostname = None
    if not hostname or hostname.lower() != target.neon_endpoint_hostname:
        raise SupervisorError("worker_ingest_target_mismatch")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON constant")


def _reject_decoded_controls(value: Any) -> None:
    if isinstance(value, str):
        if any(not character.isprintable() for character in value):
            raise ValueError("non-printable registry value")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_decoded_controls(key)
            _reject_decoded_controls(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_decoded_controls(item)


def _prepare_runtime(
    secrets: WorkerSecrets,
    *,
    worker_uid: int,
    acquisition_gid: int,
    run_root: Path = _RUN_ROOT,
) -> RuntimePaths:
    """Create private secret storage and the group-readable socket directory."""

    _validate_run_root(run_root)
    secret_directory: Path | None = None
    socket_directory: Path | None = None
    try:
        secret_directory = Path(tempfile.mkdtemp(prefix="b3s-vault-worker-secrets-", dir=run_root))
        socket_directory = Path(tempfile.mkdtemp(prefix="b3s-vault-acquisition-", dir=run_root))
        os.chown(secret_directory, worker_uid, acquisition_gid)
        os.chmod(secret_directory, _SECRET_DIRECTORY_MODE)
        os.chown(socket_directory, worker_uid, acquisition_gid)
        os.chmod(socket_directory, _SOCKET_DIRECTORY_MODE)

        paths = RuntimePaths(
            secret_directory=secret_directory,
            socket_directory=socket_directory,
            socket_path=socket_directory / _SOCKET_FILENAME,
            private_key_file=secret_directory / "private-key.b64",
            registry_file=secret_directory / "public-key-registry.json",
            ingest_dsn_file=secret_directory / "ingest-dsn",
            exa_api_key_file=secret_directory / "exa-api-key",
        )
        payloads = (
            (paths.private_key_file.name, secrets.private_key_b64),
            (paths.registry_file.name, secrets.public_key_registry_json),
            (paths.ingest_dsn_file.name, secrets.ingest_dsn),
            (paths.exa_api_key_file.name, secrets.exa_api_key),
        )
        _materialize_files(
            secret_directory,
            payloads,
            owner_uid=worker_uid,
            owner_gid=acquisition_gid,
        )
        return paths
    except Exception:
        if secret_directory is not None:
            _remove_runtime_directory(secret_directory)
        if socket_directory is not None:
            _remove_runtime_directory(socket_directory)
        raise


def _validate_run_root(run_root: Path) -> None:
    try:
        info = run_root.lstat()
    except OSError:
        raise SupervisorError("run_root_invalid") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise SupervisorError("run_root_invalid")


def _materialize_files(
    directory: Path,
    payloads: Sequence[tuple[str, str]],
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(directory, directory_flags)
    try:
        for filename, value in payloads:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(filename, flags, _SECRET_FILE_MODE, dir_fd=directory_fd)
            try:
                os.fchown(descriptor, owner_uid, owner_gid)
                os.fchmod(descriptor, _SECRET_FILE_MODE)
                _write_all(descriptor, value.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("short secret file write")
        written += count


def _worker_command(paths: RuntimePaths, target: WorkerTarget) -> list[str]:
    return [
        sys.executable,
        str(_APP_ROOT / "scripts" / "run_evidence_vault_acquisition_worker.py"),
        "--socket-path",
        str(paths.socket_path),
        "--private-key-file",
        str(paths.private_key_file),
        "--registry-file",
        str(paths.registry_file),
        "--ingest-dsn-file",
        str(paths.ingest_dsn_file),
        "--exa-api-key-file",
        str(paths.exa_api_key_file),
        "--expected-database",
        target.database,
        "--expected-database-hostname",
        target.neon_endpoint_hostname,
        "--expected-neon-project-id",
        target.neon_project_id,
        "--expected-neon-branch-id",
        target.neon_branch_id,
        "--socket-mode",
        "0660",
    ]


def _worker_environment(_source: MutableMapping[str, str]) -> dict[str, str]:
    """Give the worker no ambient application or provider secret environment."""

    return {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(_APP_ROOT),
    }


def _web_environment(source: MutableMapping[str, str], socket_path: Path) -> dict[str, str]:
    environment = dict(source)
    for name in WORKER_SECRET_ENV_NAMES + _LIBPQ_ENV_NAMES:
        environment.pop(name, None)
    # The supervisor runs as root. Do not let libpq inherit root's HOME and
    # attempt to read root-owned client-certificate files after dropping uid.
    environment["HOME"] = _WEB_HOME
    environment[WEB_SOCKET_ENV] = str(socket_path)
    return environment


def _spawn_child(
    command: Sequence[str],
    *,
    environment: dict[str, str],
    uid: int,
    gid: int,
    extra_groups: Sequence[int],
    umask: int,
) -> subprocess.Popen[Any]:
    if not command or not command[0]:
        raise SupervisorError("child_command_invalid")
    return subprocess.Popen(
        list(command),
        cwd=_APP_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        restore_signals=True,
        user=uid,
        group=gid,
        extra_groups=tuple(extra_groups),
        umask=umask,
    )


def _wait_for_socket(
    worker: subprocess.Popen[Any],
    socket_path: Path,
    *,
    worker_uid: int,
    acquisition_gid: int,
    relay: SignalRelay,
    timeout_seconds: float = _STARTUP_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        returncode = worker.poll()
        if returncode is not None:
            raise SupervisorError("worker_exited_before_socket")
        if relay.received is not None:
            raise SupervisorError("startup_interrupted")
        try:
            info = socket_path.lstat()
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        except OSError:
            raise SupervisorError("worker_socket_invalid") from None
        if (
            not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != _SOCKET_MODE
            or info.st_uid != worker_uid
            or info.st_gid != acquisition_gid
        ):
            raise SupervisorError("worker_socket_invalid")
        return
    raise SupervisorError("worker_socket_timeout")


def _supervise(children: Sequence[subprocess.Popen[Any]], relay: SignalRelay) -> int:
    while True:
        for child in children:
            returncode = child.poll()
            if returncode is not None:
                _terminate_children([other for other in children if other is not child])
                if relay.received is not None:
                    return 128 + relay.received
                return _portable_returncode(returncode)
        time.sleep(0.1)


def _portable_returncode(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode


def _signal_child(child: subprocess.Popen[Any], signum: int) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _terminate_children(
    children: Sequence[subprocess.Popen[Any]],
    *,
    timeout_seconds: float = _SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    living = [child for child in children if child.poll() is None]
    for child in living:
        _signal_child(child, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while living and time.monotonic() < deadline:
        living = [child for child in living if child.poll() is None]
        if living:
            time.sleep(0.05)
    for child in living:
        _signal_child(child, signal.SIGKILL)
    for child in children:
        try:
            child.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, ChildProcessError):
            pass


def _remove_secret_files(paths: RuntimePaths, *, strict: bool = False) -> None:
    failed = False
    for path in paths.secret_files:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            failed = True
    try:
        paths.secret_directory.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        failed = True
    if strict and (failed or paths.secret_directory.exists()):
        raise SupervisorError("worker_secret_cleanup_failed")


def _remove_runtime_directory(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _remove_socket_directory(paths: RuntimePaths) -> None:
    try:
        paths.socket_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    try:
        paths.socket_directory.rmdir()
    except OSError:
        pass


def _lookup_identity(user_name: str, group_name: str) -> tuple[int, int]:
    try:
        return pwd.getpwnam(user_name).pw_uid, grp.getgrnam(group_name).gr_gid
    except KeyError:
        raise SupervisorError("container_identity_missing") from None


def main(argv: Sequence[str] | None = None) -> int:
    web_command = list(sys.argv[1:] if argv is None else argv)
    if os.geteuid() != 0:
        print("vault worker supervisor failed: root_required", file=sys.stderr)
        return 78
    if not web_command:
        print("vault worker supervisor failed: web_command_missing", file=sys.stderr)
        return 78

    relay = SignalRelay()
    children: list[subprocess.Popen[Any]] = []
    paths: RuntimePaths | None = None
    secrets_materialized = False
    relay.install()
    try:
        secrets = _load_and_validate_secrets(os.environ)
        target = _validated_worker_target(os.environ)
        _assert_ingest_target(secrets, target)
        worker_uid, acquisition_gid = _lookup_identity(_WORKER_USER, _ACQUISITION_GROUP)
        web_uid, web_gid = _lookup_identity(_WEB_USER, _WEB_USER)
        paths = _prepare_runtime(
            secrets,
            worker_uid=worker_uid,
            acquisition_gid=acquisition_gid,
        )
        secrets_materialized = True
        secrets = None

        worker = _spawn_child(
            _worker_command(paths, target),
            environment=_worker_environment(os.environ),
            uid=worker_uid,
            gid=acquisition_gid,
            extra_groups=(),
            umask=0o027,
        )
        children.append(worker)
        relay.add(worker)
        _wait_for_socket(
            worker,
            paths.socket_path,
            worker_uid=worker_uid,
            acquisition_gid=acquisition_gid,
            relay=relay,
        )
        _remove_secret_files(paths, strict=True)
        secrets_materialized = False

        web = _spawn_child(
            web_command,
            environment=_web_environment(os.environ, paths.socket_path),
            uid=web_uid,
            gid=web_gid,
            extra_groups=(acquisition_gid,),
            umask=0o022,
        )
        children.append(web)
        relay.add(web)
        return _supervise((worker, web), relay)
    except SupervisorError as exc:
        print(f"vault worker supervisor failed: {exc.code}", file=sys.stderr)
        if relay.received is not None:
            return 128 + relay.received
        return 78
    except Exception:
        print("vault worker supervisor failed: internal_error", file=sys.stderr)
        if relay.received is not None:
            return 128 + relay.received
        return 70
    finally:
        _terminate_children(children)
        if paths is not None:
            if secrets_materialized:
                _remove_secret_files(paths)
            _remove_socket_directory(paths)
        relay.restore()


if __name__ == "__main__":
    raise SystemExit(main())
