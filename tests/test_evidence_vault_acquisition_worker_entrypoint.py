from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts/run_evidence_vault_acquisition_worker.py"


def _module():
    spec = importlib.util.spec_from_file_location("vault_acquisition_entrypoint", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_entrypoint_loads_owner_only_secret_files_without_ambient_config(
    tmp_path: Path,
) -> None:
    module = _module()
    secret = tmp_path / "private-key"
    secret.write_text(base64.b64encode(bytes(range(32))).decode())
    secret.chmod(0o600)
    key = module._load_private_key(secret)
    assert key is not None

    dsn = tmp_path / "ingest-dsn"
    dsn.write_text("postgresql://scanner_ingest@vault/raw")
    dsn.chmod(0o600)
    assert module._load_secret_text(dsn, field="ingest_dsn").startswith(
        "postgresql://"
    )

    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "private-key-file" in completed.stdout
    assert "ingest-dsn-file" in completed.stdout
    assert "PRIVATE_KEY" not in _SCRIPT.read_text()
    assert "os.environ" not in _SCRIPT.read_text()


def test_worker_entrypoint_rejects_symlink_group_readable_and_malformed_files(
    tmp_path: Path,
) -> None:
    module = _module()
    target = tmp_path / "target"
    target.write_text("secret")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="worker_file_invalid"):
        module._load_secret_text(link, field="secret")

    target.chmod(0o640)
    with pytest.raises(ValueError, match="worker_file_invalid"):
        module._load_secret_text(target, field="secret")

    target.chmod(0o600)
    target.write_text("line-one\nline-two")
    with pytest.raises(ValueError, match="secret_file_invalid"):
        module._load_secret_text(target, field="secret")


def test_registry_loader_rejects_duplicate_and_nonfinite_json(tmp_path: Path) -> None:
    module = _module()
    for raw in ('{"schema_version":"a","schema_version":"b"}', '{"x":NaN}'):
        path = tmp_path / str(abs(hash(raw)))
        path.write_text(raw)
        path.chmod(0o600)
        with pytest.raises(ValueError, match="registry_file_invalid"):
            module._load_json_model(path, module.PublicKeyRegistry)


def test_worker_dsn_validation_requires_authenticated_tls_and_fixed_authority() -> None:
    module = _module()
    module._validate_postgres_dsn(
        "postgresql://scanner:secret@db.example/vault"
        "?sslmode=require&channel_binding=require"
    )
    module._validate_postgres_dsn(
        "postgresql://scanner:secret@db.example/vault?sslmode=verify-full"
    )
    for invalid in (
        "postgresql://scanner:secret@db.example/vault?sslmode=require",
        "postgresql://scanner:secret@db.example/vault?sslmode=verify-ca",
        "postgresql://scanner:secret@db.example/vault?sslmode=disable",
        "postgresql://scanner:secret@db.example/vault"
        "?sslmode=require&channel_binding=require&host=other",
        "postgresql://scanner:secret@db.example/vault"
        "?sslmode=require&channel_binding=require&options=-c%20neon.branch_id%3Dfake",
    ):
        with pytest.raises(ValueError, match="ingest_dsn_file_invalid"):
            module._validate_postgres_dsn(invalid)
