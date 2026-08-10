from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import shutil
from typing import Any
from uuid import uuid4

import pytest

from src.services.evidence_vault_acquisition_contract import (
    SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION,
    SafeDeterministicDocument,
    SignedAcquisitionResultEnvelope,
    TrustedAcquisitionCommand,
)
from src.services.evidence_vault_acquisition_ipc import (
    ACQUISITION_IPC_REQUEST_VERSION,
    EvidenceVaultAcquisitionTransportError,
    UnixTrustedAcquisitionClient,
    _MAX_REQUEST_BYTES,
    _receive_frame,
    _reject_trailing_bytes,
    _strict_json_object,
)
from src.services.evidence_vault_acquisition_ipc_server import (
    serve_unix_trusted_acquisition,
)
from src.services.evidence_vault_canonical_core import canonical_fingerprint


_RECEIPT_SET_VERSION = "evidence-vault-raw-acquisition-receipt-set-v1"
_FINGERPRINT = "1" * 64


def _result() -> SignedAcquisitionResultEnvelope:
    receipt_set = canonical_fingerprint(
        _RECEIPT_SET_VERSION,
        {
            "schema_version": _RECEIPT_SET_VERSION,
            "receipt_fingerprints": [_FINGERPRINT],
        },
    )
    return SignedAcquisitionResultEnvelope(
        schema_version=SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION,
        workspace_slug="b3s",
        source_scan_id="ipc-test-scan",
        brand_url="https://example.com",
        capture_id="12345678-1234-4234-8234-123456789abc",
        capture_content_hash="2" * 64,
        capture_observation_hash="3" * 64,
        receipt_set_fingerprint=receipt_set,
        receipt_rows=[
            {
                "receipt_id": "22345678-1234-4234-8234-123456789abc",
                "receipt_fingerprint": _FINGERPRINT,
                "received_at": datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
            }
        ],
        documents=[
            {
                "role": "owned_web",
                "source_url": "https://example.com",
                "extracted_document": "Durable verified acquisition document.",
                "extracted_document_sha256": hashlib.sha256(
                    b"Durable verified acquisition document."
                ).hexdigest(),
                "extractor_version": "evidence-vault-deterministic-extractor-v1",
                "receipt_fingerprint": _FINGERPRINT,
            }
        ],
    )


class _Worker:
    def capture(self, command: TrustedAcquisitionCommand) -> SignedAcquisitionResultEnvelope:
        assert command == TrustedAcquisitionCommand(
            workspace_slug="b3s",
            source_scan_id="ipc-test-scan",
            brand_url="https://example.com",
        )
        return _result()


class _SecretFailureWorker:
    def capture(self, _command: TrustedAcquisitionCommand) -> SignedAcquisitionResultEnvelope:
        raise RuntimeError("postgresql://scanner:secret@internal private-key-value")


def _serve_process(socket_path: str, stop_event: Any, failing: bool = False) -> None:
    serve_unix_trusted_acquisition(
        _SecretFailureWorker() if failing else _Worker(),
        socket_path,
        stop_event=stop_event,
        accept_timeout_seconds=0.05,
    )


def _wait_for_socket(path: Path, process: Any) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        if not process.is_alive():
            raise AssertionError(f"server exited early: {process.exitcode}")
        time.sleep(0.01)
    assert path.exists()


@pytest.fixture
def socket_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="b3s-ipc-", dir="/tmp"))
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _command() -> TrustedAcquisitionCommand:
    return TrustedAcquisitionCommand(
        workspace_slug="b3s",
        source_scan_id="ipc-test-scan",
        brand_url="https://example.com",
    )


def test_real_separate_process_round_trip_has_one_typed_capture_verb(socket_dir: Path) -> None:
    os.chmod(socket_dir, 0o700)
    path = socket_dir / "acquisition.sock"
    context = multiprocessing.get_context("spawn")
    stop_event = context.Event()
    process = context.Process(target=_serve_process, args=(str(path), stop_event))
    process.start()
    try:
        _wait_for_socket(path, process)
        result = UnixTrustedAcquisitionClient(path, timeout_seconds=2).capture(_command())
        assert result == _result()
        assert set(result.model_dump(mode="json")) == {
            "schema_version", "workspace_slug", "source_scan_id", "brand_url",
            "capture_id", "capture_content_hash", "capture_observation_hash",
            "receipt_set_fingerprint", "receipt_rows", "documents",
        }
        assert "raw_payload" not in result.model_dump_json()
        assert "signature" not in result.model_dump_json()
    finally:
        stop_event.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_worker_exception_is_allowlisted_and_redacts_internal_values(socket_dir: Path) -> None:
    os.chmod(socket_dir, 0o700)
    path = socket_dir / "acquisition.sock"
    context = multiprocessing.get_context("spawn")
    stop_event = context.Event()
    process = context.Process(target=_serve_process, args=(str(path), stop_event, True))
    process.start()
    try:
        _wait_for_socket(path, process)
        with pytest.raises(EvidenceVaultAcquisitionTransportError) as caught:
            UnixTrustedAcquisitionClient(path, timeout_seconds=2).capture(_command())
        assert str(caught.value) == "acquisition_failed"
        assert "secret" not in str(caught.value)
        assert "postgresql" not in str(caught.value)
    finally:
        stop_event.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_server_rejects_duplicate_keys_and_oversized_frames_without_worker_call(
    socket_dir: Path,
) -> None:
    os.chmod(socket_dir, 0o700)
    path = socket_dir / "acquisition.sock"
    context = multiprocessing.get_context("spawn")
    stop_event = context.Event()
    process = context.Process(target=_serve_process, args=(str(path), stop_event))
    process.start()
    try:
        _wait_for_socket(path, process)
        request_id = str(uuid4())
        duplicate = (
            "{"
            f'"schema_version":"{ACQUISITION_IPC_REQUEST_VERSION}",'
            f'"request_id":"{request_id}","operation":"capture",'
            '"operation":"capture","command":{}}'
        ).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            client.sendall(struct.pack("!I", len(duplicate)) + duplicate)
            response = _strict_json_object(_receive_frame(client, maximum=4096))
        assert response["ok"] is False
        assert response["error_code"] == "invalid_request"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            client.sendall(struct.pack("!I", _MAX_REQUEST_BYTES + 1))
            response = _strict_json_object(_receive_frame(client, maximum=4096))
        assert response["ok"] is False
        assert response["error_code"] == "invalid_request"
    finally:
        stop_event.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_server_refuses_world_writable_parent_and_non_socket_replacement(
    socket_dir: Path,
) -> None:
    path = socket_dir / "acquisition.sock"
    os.chmod(socket_dir, 0o777)
    with pytest.raises(ValueError, match="owned non-writable"):
        serve_unix_trusted_acquisition(_Worker(), path)
    os.chmod(socket_dir, 0o700)
    path.write_text("do not replace")
    with pytest.raises(ValueError, match="non-owned socket"):
        serve_unix_trusted_acquisition(_Worker(), path)


def test_client_import_is_secret_free_and_does_not_import_worker_runtime() -> None:
    code = """
import json, sys
import src.services.evidence_vault_acquisition_contract
import src.services.evidence_vault_acquisition_ipc
print(json.dumps(sorted(name for name in sys.modules if name.endswith((
    'evidence_vault_acquisition_worker',
    'evidence_vault_raw_repository',
    'evidence_vault_raw_provenance',
)))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(completed.stdout) == []


def test_public_document_projection_enforces_character_and_utf8_byte_caps() -> None:
    common = {
        "role": "owned_web",
        "receipt_fingerprint": "a" * 64,
        "source_url": "https://example.com",
        "extracted_document_sha256": hashlib.sha256(
            ("x" * 2_097_152).encode()
        ).hexdigest(),
        "extractor_version": "evidence-vault-deterministic-extractor-v1",
    }
    accepted = SafeDeterministicDocument(
        **common, extracted_document="x" * 2_097_152
    )
    assert len(accepted.extracted_document) == 2_097_152
    with pytest.raises(ValueError, match="at most 2097152 characters"):
        SafeDeterministicDocument(
            **common, extracted_document="x" * 2_097_153
        )
    with pytest.raises(ValueError, match="durable v1 bound"):
        SafeDeterministicDocument(
            **common, extracted_document="é" * 1_048_577
        )


def test_framing_rejects_trailing_bytes_after_one_message() -> None:
    reader, writer = socket.socketpair()
    try:
        payload = b'{}'
        writer.sendall(struct.pack('>I', len(payload)) + payload + b'x')
        assert _receive_frame(reader, maximum=32) == payload
        with pytest.raises(ValueError, match="trailing IPC bytes"):
            _reject_trailing_bytes(reader)
    finally:
        reader.close()
        writer.close()


def test_public_result_rejects_document_hash_source_and_external_only_corruption() -> None:
    payload = _result().model_dump(mode="python", round_trip=True)
    payload["documents"][0]["extracted_document"] += " corrupted"
    with pytest.raises(ValueError, match="hash differs"):
        SignedAcquisitionResultEnvelope.model_validate(payload, strict=True)

    payload = _result().model_dump(mode="python", round_trip=True)
    payload["documents"][0]["source_url"] = "https://other.example/path"
    with pytest.raises(ValueError, match="differs from the command brand"):
        SignedAcquisitionResultEnvelope.model_validate(payload, strict=True)

    payload = _result().model_dump(mode="python", round_trip=True)
    payload["documents"][0]["role"] = "external_social_profile"
    payload["documents"][0]["source_url"] = (
        "https://www.linkedin.com/company/example"
    )
    with pytest.raises(ValueError, match="invalid exact role set"):
        SignedAcquisitionResultEnvelope.model_validate(payload, strict=True)


def test_server_bounds_concurrent_handlers_and_rejects_excess_peer(
    socket_dir: Path,
) -> None:
    os.chmod(socket_dir, 0o700)
    path = socket_dir / "bounded.sock"
    stop = threading.Event()
    entered = threading.Event()
    release = threading.Event()

    class BlockingWorker:
        def capture(self, _command):
            entered.set()
            assert release.wait(2)
            return _result()

    server = threading.Thread(
        target=serve_unix_trusted_acquisition,
        args=(BlockingWorker(), path),
        kwargs={
            "stop_event": stop,
            "socket_mode": 0o600,
            "accept_timeout_seconds": 0.05,
            "request_frame_timeout_seconds": 0.1,
            "max_concurrent_connections": 1,
        },
        daemon=True,
    )
    first_result = []
    first = threading.Thread(
        target=lambda: first_result.append(
            UnixTrustedAcquisitionClient(path, timeout_seconds=2).capture(_command())
        )
    )
    server.start()
    try:
        _wait_for_socket(path, server)
        first.start()
        assert entered.wait(1)
        with pytest.raises(
            EvidenceVaultAcquisitionTransportError, match="worker_unavailable"
        ):
            UnixTrustedAcquisitionClient(path, timeout_seconds=0.2).capture(
                _command()
            )
        release.set()
        first.join(2)
        assert len(first_result) == 1
    finally:
        release.set()
        stop.set()
        server.join(2)


def test_public_document_rejects_noncanonical_role_urls() -> None:
    document = "Exact bytes"
    common = {
        "extracted_document": document,
        "extracted_document_sha256": hashlib.sha256(document.encode()).hexdigest(),
        "extractor_version": "evidence-vault-deterministic-extractor-v1",
        "receipt_fingerprint": "a" * 64,
    }
    with pytest.raises(ValueError, match="owned source URL"):
        SafeDeterministicDocument(
            **common, role="owned_web", source_url="https://EXAMPLE.com/path"
        )
    with pytest.raises(ValueError, match="external source URL"):
        SafeDeterministicDocument(
            **common,
            role="external_social_profile",
            source_url="https://linkedin.com/company/example",
        )
