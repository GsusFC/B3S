from __future__ import annotations

from types import SimpleNamespace
import json
import os
import subprocess
import sys

import pytest

from src import config
from web import scan_runner


def _status(scan_id: str) -> dict:
    return {
        "id": scan_id,
        "url": "https://example.com",
        "brand_name": "Example",
        "state": "running",
        "phase": "capture",
        "phases": [],
        "acquisition": [],
        "acquisition_gate": {},
        "allow_degraded_fallback": False,
        "error": None,
        "error_code": None,
        "started_at": "2026-08-09T00:00:00Z",
        "completed_at": None,
    }


def test_verified_raw_shadow_is_default_off_and_performs_no_ipc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        False,
    )
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH",
        "/not/contacted.sock",
    )
    monkeypatch.delenv("BRAND3_ENVIRONMENT", raising=False)
    scan_runner._capture_verified_raw_shadow(
        scan_id="scan-shadow-off",
        url="https://example.com/path",
    )


def test_verified_raw_shadow_calls_only_public_ipc_and_records_safe_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import evidence_vault_acquisition_ipc as ipc

    observed = []

    class Client:
        def __init__(self, path: str) -> None:
            observed.append(("socket", path))

        def capture(self, command):
            observed.append(("command", command))
            return SimpleNamespace(
                capture_id="12345678-1234-4234-8234-123456789abc",
                capture_content_hash="1" * 64,
                receipt_set_fingerprint="2" * 64,
                receipt_rows=[object(), object()],
            )

    scan_id = "scan-shadow-on"
    scan_runner._SCANS[scan_id] = _status(scan_id)
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        True,
    )
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH",
        "/tmp/b3s-vault/acquisition.sock",
    )
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(ipc, "UnixTrustedAcquisitionClient", Client)
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda value: observed.append(("persist", value)))
    try:
        scan_runner._capture_verified_raw_shadow(
            scan_id=scan_id,
            url="https://www.example.com/path?ignored=1",
        )
        command = observed[1][1]
        assert command.workspace_slug == "b3s"
        assert command.source_scan_id == scan_id
        assert command.brand_url == "https://example.com"
        assert scan_runner._SCANS[scan_id]["verified_raw_acquisition"] == {
            "state": "persisted_shadow",
            "capture_id": "12345678-1234-4234-8234-123456789abc",
            "capture_content_hash": "1" * 64,
            "receipt_set_fingerprint": "2" * 64,
            "receipt_count": 2,
        }
        assert "documents" not in scan_runner._SCANS[scan_id]["verified_raw_acquisition"]
    finally:
        scan_runner._SCANS.pop(scan_id, None)


def test_enabled_shadow_fails_closed_without_fallback_or_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import evidence_vault_acquisition_ipc as ipc

    class FailingClient:
        def __init__(self, _path: str) -> None:
            pass

        def capture(self, _command):
            raise RuntimeError("private-key and postgresql://secret")

    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        True,
    )
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH",
        "/tmp/b3s-vault/acquisition.sock",
    )
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(ipc, "UnixTrustedAcquisitionClient", FailingClient)
    with pytest.raises(RuntimeError) as caught:
        scan_runner._capture_verified_raw_shadow(
            scan_id="scan-shadow-failure",
            url="https://example.com",
        )
    assert str(caught.value) == "verified_raw_acquisition_failed"
    assert caught.value.__context__ is None


def test_scan_runner_places_verified_raw_persistence_before_interpretation() -> None:
    text = __import__("inspect").getsource(scan_runner._run)
    assert text.index("_capture_verified_raw_shadow") < text.index(
        "build_flow_sv9_shadow_eval"
    )


def test_web_import_does_not_load_worker_repository_signing_or_secret_config() -> None:
    environment = dict(os.environ)
    environment["B3S_VAULT_SCANNER_INGEST_DATABASE_URL"] = "postgresql://secret"
    environment["B3S_VAULT_ACQUISITION_PRIVATE_KEY"] = "private-key-secret"
    code = """
import json, sys
from src import config
import web.scan_runner
forbidden = sorted(name for name in sys.modules if name.endswith((
    'evidence_vault_acquisition_worker',
    'evidence_vault_acquisition_ipc_server',
    'evidence_vault_raw_repository',
    'evidence_vault_raw_provenance',
)))
print(json.dumps({
    'forbidden': forbidden,
    'has_ingest_config': hasattr(config, 'B3S_VAULT_SCANNER_INGEST_DATABASE_URL'),
    'has_private_config': hasattr(config, 'B3S_VAULT_ACQUISITION_PRIVATE_KEY'),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        text=True,
        capture_output=True,
        env=environment,
    )
    assert json.loads(completed.stdout) == {
        "forbidden": [],
        "has_ingest_config": False,
        "has_private_config": False,
    }
