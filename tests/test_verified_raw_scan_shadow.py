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
    with pytest.raises(RuntimeError, match="verified_raw_acquisition_disabled"):
        scan_runner._capture_verified_raw_shadow(
            scan_id="scan-shadow-off",
            url="https://example.com/path",
            brand_name="Example",
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
                documents=[
                    SimpleNamespace(
                        role="owned_web",
                        source_url="https://example.com",
                        extracted_document="Exact owned document.",
                        extracted_document_sha256="3" * 64,
                        receipt_fingerprint="1" * 64,
                    ),
                    SimpleNamespace(
                        role="external_social_profile",
                        source_url="https://www.linkedin.com/company/example",
                        extracted_document="Exact external document.",
                        extracted_document_sha256="4" * 64,
                        receipt_fingerprint="2" * 64,
                    ),
                ],
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
        snapshot = scan_runner._capture_verified_raw_shadow(
            scan_id=scan_id,
            url="https://www.example.com/path?ignored=1",
            brand_name="Example",
        )
        assert snapshot["raw_inputs"] == [
            {
                "source": "verified_raw_document",
                "payload": {
                    "role": "owned_web",
                    "url": "https://example.com",
                    "content": "Exact owned document.",
                    "extracted_document_sha256": "3" * 64,
                    "receipt_fingerprint": "1" * 64,
                },
            },
            {
                "source": "verified_raw_document",
                "payload": {
                    "role": "external_social_profile",
                    "url": "https://www.linkedin.com/company/example",
                    "content": "Exact external document.",
                    "extracted_document_sha256": "4" * 64,
                    "receipt_fingerprint": "2" * 64,
                },
            },
        ]
        assert snapshot["acquisition_steps"]["searchapi"]["status"] == "disabled"
        assert snapshot["run"]["url"] == "https://example.com"
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
            brand_name="Example",
        )
    assert str(caught.value) == "verified_raw_acquisition_failed"
    assert caught.value.__context__ is None


def test_scan_runner_uses_verified_snapshot_instead_of_legacy_capture_when_enabled() -> None:
    text = __import__("inspect").getsource(scan_runner._run)
    assert "if BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED" in text
    assert text.index("_capture_verified_raw_shadow") < text.index(
        "_capture_snapshot"
    )
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


def test_enabled_run_interprets_only_the_verified_durable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import sv9_flow_sv9_shadow_eval as flow_eval

    scan_id = "scan-verified-integration"
    status = _status(scan_id)
    status["phases"] = [
        {"key": key, "state": "pending"}
        for key in ("capture", "interpret", "score", "report")
    ]
    scan_runner._SCANS[scan_id] = status
    verified_snapshot = {
        "run": {"brand_name": "Example", "id": 123, "url": "https://example.com"},
        "raw_inputs": [
            {
                "source": "web",
                "payload": {"content": "Exact durable verified document."},
            }
        ],
        "acquisition_steps": {
            "web": {"source": "web", "status": "success"},
            "exa": {"source": "exa", "status": "success"},
        },
        "features": [],
    }
    observed = []
    monkeypatch.setattr(
        config,
        "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
        True,
    )
    monkeypatch.setattr(
        scan_runner,
        "_capture_verified_raw_shadow",
        lambda **_kwargs: verified_snapshot,
    )
    monkeypatch.setattr(
        scan_runner,
        "_capture_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy unsigned capture must not run")
        ),
    )
    monkeypatch.setattr(
        flow_eval,
        "build_flow_sv9_shadow_eval",
        lambda envelope, **_kwargs: observed.append(envelope["snapshot"]) or {},
    )
    monkeypatch.setattr(scan_runner, "_attach_sv9_editorial", lambda value: value)
    monkeypatch.setattr(
        scan_runner,
        "_compose_report",
        lambda *_args, **_kwargs: {"id": scan_id},
    )
    monkeypatch.setattr(scan_runner, "_attach_evidence_stability", lambda value: value)
    monkeypatch.setattr(scan_runner, "save_report", lambda value: observed.append(value))
    monkeypatch.setattr(scan_runner, "_persist_scan_status", lambda _value: None)
    try:
        scan_runner._run(scan_id, "https://example.com", "Example", False)
        assert observed[0] is verified_snapshot
        assert status["state"] == "done"
    finally:
        scan_runner._SCANS.pop(scan_id, None)


def test_verified_external_document_survives_real_evidence_pack() -> None:
    from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot

    token = "EXACT-EXTERNAL-VERIFIED-TOKEN"
    documents = [
        SimpleNamespace(
            role="owned_web",
            source_url="https://example.com",
            extracted_document="Exact owned verified text.",
            extracted_document_sha256="3" * 64,
            receipt_fingerprint="1" * 64,
        ),
        SimpleNamespace(
            role="external_social_profile",
            source_url="https://www.linkedin.com/company/example",
            extracted_document=f"Exact external verified text {token}.",
            extracted_document_sha256="4" * 64,
            receipt_fingerprint="2" * 64,
        ),
    ]
    snapshot = scan_runner._verified_raw_pre_analysis_snapshot(
        scan_id="stable-scan-id",
        brand_name="Example",
        canonical_url="https://example.com",
        documents=documents,
    )
    pack = build_evidence_pack_from_snapshot(snapshot)
    matches = [record for record in pack.evidence if token in record.content]
    assert len(matches) == 1
    external = matches[0]
    assert external.url == "https://www.linkedin.com/company/example"
    assert external.metadata["source_class"] == "external_proof"
    assert external.metadata["channel_role"] == "external_social_profile"
    assert external.metadata["receipt_fingerprint"] == "2" * 64
    assert snapshot["run"]["id"] == scan_runner._stable_verified_source_run_id(
        "stable-scan-id"
    )


def test_verified_owned_only_never_fabricates_configured_searchapi_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SEARCHAPI_API_KEY", "configured-but-not-run")
    snapshot = scan_runner._verified_raw_pre_analysis_snapshot(
        scan_id="owned-only",
        brand_name="Example",
        canonical_url="https://example.com",
        documents=[
            SimpleNamespace(
                role="owned_web",
                source_url="https://example.com",
                extracted_document="Exact owned verified text.",
                extracted_document_sha256="3" * 64,
                receipt_fingerprint="1" * 64,
            )
        ],
    )
    assert snapshot["acquisition_steps"]["searchapi"]["status"] == "disabled"
    gate = scan_runner._build_acquisition_gate(
        snapshot["acquisition_steps"], allow_degraded_fallback=True
    )
    assert gate["state"] == "blocked"
    assert gate["can_continue"] is False
    assert gate["fallbacks"] == [
        {
            "source": "searchapi",
            "for_source": "exa",
            "available": False,
            "approved": False,
            "status": "disabled",
            "reason": "vertical external-proof fallback for Exa failure",
        }
    ]
