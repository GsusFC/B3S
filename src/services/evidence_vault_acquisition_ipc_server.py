"""Worker-only server for the trusted acquisition Unix socket."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import threading
from typing import Any, Protocol
from uuid import uuid4

from src.services.evidence_vault_acquisition_contract import (
    SignedAcquisitionResultEnvelope,
    TrustedAcquisitionCommand,
)
from src.services.evidence_vault_acquisition_ipc import (
    ACQUISITION_IPC_RESPONSE_VERSION,
    _CaptureRequest,
    _CaptureResponse,
    _MAX_REQUEST_BYTES,
    _MAX_RESPONSE_BYTES,
    _receive_frame,
    _send_frame,
    _strict_json_object,
    _validated_socket_path,
)
from src.services.evidence_vault_acquisition_worker import (
    EvidenceVaultAcquisitionReplayConflictError,
)


class _CaptureCapability(Protocol):
    def capture(
        self,
        command: TrustedAcquisitionCommand,
    ) -> SignedAcquisitionResultEnvelope: ...


def serve_unix_trusted_acquisition(
    worker: _CaptureCapability,
    socket_path: str | os.PathLike[str],
    *,
    stop_event: threading.Event | None = None,
    socket_mode: int = 0o660,
    accept_timeout_seconds: float = 0.25,
) -> None:
    """Serve the single capture verb until ``stop_event`` is set.

    The dedicated socket directory and socket group are the authorization
    boundary.  No TCP listener, raw-payload verb, or signing verb exists.
    """

    if not callable(getattr(worker, "capture", None)):
        raise TypeError("worker must expose capture")
    path = _validated_socket_path(socket_path)
    _validate_server_directory(Path(path).parent)
    if socket_mode not in {0o600, 0o660}:
        raise ValueError("socket_mode must be 0600 or 0660")
    if (
        isinstance(accept_timeout_seconds, bool)
        or not isinstance(accept_timeout_seconds, (int, float))
        or not 0.05 <= float(accept_timeout_seconds) <= 5.0
    ):
        raise ValueError("accept timeout is invalid")
    event = stop_event or threading.Event()
    _remove_stale_owned_socket(Path(path))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        prior_umask = os.umask(0o117 if socket_mode == 0o660 else 0o177)
        try:
            server.bind(path)
        finally:
            os.umask(prior_umask)
        os.chmod(path, socket_mode)
        server.listen(16)
        server.settimeout(float(accept_timeout_seconds))
        while not event.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(300.0)
                _serve_connection(worker, connection)
    finally:
        server.close()
        _remove_stale_owned_socket(Path(path))


def _serve_connection(
    worker: _CaptureCapability,
    connection: socket.socket,
) -> None:
    request_id = str(uuid4())
    try:
        payload = _receive_frame(connection, maximum=_MAX_REQUEST_BYTES)
        request = _CaptureRequest.model_validate(
            _strict_json_object(payload),
            strict=True,
        )
        request_id = request.request_id
    except Exception:
        response = _failure(request_id, "invalid_request")
    else:
        try:
            result_value = worker.capture(request.command)
            result = (
                SignedAcquisitionResultEnvelope.model_validate_json(
                    result_value.model_dump_json(),
                    strict=True,
                )
                if isinstance(result_value, SignedAcquisitionResultEnvelope)
                else SignedAcquisitionResultEnvelope.model_validate(
                    result_value,
                    strict=True,
                )
            )
            response = _CaptureResponse(
                schema_version=ACQUISITION_IPC_RESPONSE_VERSION,
                request_id=request_id,
                ok=True,
                result=result,
                error_code=None,
            )
        except EvidenceVaultAcquisitionReplayConflictError:
            response = _failure(request_id, "replay_conflict")
        except Exception:
            response = _failure(request_id, "acquisition_failed")
    try:
        encoded = response.model_dump_json().encode("utf-8")
        if len(encoded) > _MAX_RESPONSE_BYTES:
            encoded = _failure(request_id, "result_too_large").model_dump_json().encode(
                "utf-8"
            )
        _send_frame(connection, encoded, maximum=_MAX_RESPONSE_BYTES)
    except (OSError, ValueError):
        return


def _failure(request_id: str, code: Any) -> _CaptureResponse:
    return _CaptureResponse(
        schema_version=ACQUISITION_IPC_RESPONSE_VERSION,
        request_id=request_id,
        ok=False,
        result=None,
        error_code=code,
    )


def _validate_server_directory(parent: Path) -> None:
    info = parent.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValueError("socket parent must be an owned non-writable directory")


def _remove_stale_owned_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("refusing to replace a non-owned socket path")
    path.unlink()


__all__ = ["serve_unix_trusted_acquisition"]
