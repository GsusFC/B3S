"""Secret-free Unix-socket client for trusted Evidence Vault acquisition."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import struct
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.services.evidence_vault_acquisition_contract import (
    SignedAcquisitionResultEnvelope,
    TrustedAcquisitionCommand,
)


ACQUISITION_IPC_REQUEST_VERSION = "evidence-vault-acquisition-ipc-request-v1"
ACQUISITION_IPC_RESPONSE_VERSION = "evidence-vault-acquisition-ipc-response-v1"
_MAX_REQUEST_BYTES = 8 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_FRAME_HEADER_BYTES = 4
_MAX_SOCKET_PATH_BYTES = 96


class EvidenceVaultAcquisitionTransportError(RuntimeError):
    """A stable transport failure with no worker, database, or secret detail."""


class _StrictIpcModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _CaptureRequest(_StrictIpcModel):
    schema_version: Literal[ACQUISITION_IPC_REQUEST_VERSION]
    request_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    operation: Literal["capture"]
    command: TrustedAcquisitionCommand


class _CaptureResponse(_StrictIpcModel):
    schema_version: Literal[ACQUISITION_IPC_RESPONSE_VERSION]
    request_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    ok: bool
    result: SignedAcquisitionResultEnvelope | None
    error_code: Literal[
        "acquisition_failed",
        "replay_conflict",
        "invalid_request",
        "busy",
        "result_too_large",
        "worker_misconfigured",
    ] | None

    @model_validator(mode="after")
    def _exact_outcome(self) -> "_CaptureResponse":
        if self.ok:
            if self.result is None or self.error_code is not None:
                raise ValueError("successful response must contain only a result")
        elif self.result is not None or self.error_code is None:
            raise ValueError("failed response must contain only an error code")
        return self


class UnixTrustedAcquisitionClient:
    """The only acquisition capability imported by a web/report process."""

    __slots__ = ("_socket_path", "_timeout_seconds")

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._socket_path = _validated_socket_path(socket_path)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.05 <= float(timeout_seconds) <= 300.0
        ):
            raise ValueError("timeout_seconds must be between 0.05 and 300")
        self._timeout_seconds = float(timeout_seconds)

    def capture(
        self,
        command: TrustedAcquisitionCommand,
    ) -> SignedAcquisitionResultEnvelope:
        try:
            parsed_command = TrustedAcquisitionCommand.model_validate(
                command.model_dump(mode="json")
                if isinstance(command, TrustedAcquisitionCommand)
                else command,
                strict=True,
            )
        except Exception:
            raise EvidenceVaultAcquisitionTransportError("invalid_command") from None
        request = _CaptureRequest(
            schema_version=ACQUISITION_IPC_REQUEST_VERSION,
            request_id=str(uuid4()),
            operation="capture",
            command=parsed_command,
        )
        payload = request.model_dump_json().encode("utf-8")
        if len(payload) > _MAX_REQUEST_BYTES:
            raise EvidenceVaultAcquisitionTransportError("request_too_large")
        try:
            _validate_client_socket(Path(self._socket_path))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self._timeout_seconds)
                client.connect(self._socket_path)
                _send_frame(client, payload, maximum=_MAX_REQUEST_BYTES)
                raw_response = _receive_frame(client, maximum=_MAX_RESPONSE_BYTES)
                _reject_trailing_bytes(client)
        except (OSError, TimeoutError, ValueError):
            raise EvidenceVaultAcquisitionTransportError("worker_unavailable") from None
        try:
            parsed_response = _strict_json_object(raw_response)
            response = _CaptureResponse.model_validate_json(
                json.dumps(
                    parsed_response,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ),
                strict=True,
            )
        except Exception:
            raise EvidenceVaultAcquisitionTransportError(
                "invalid_worker_response"
            ) from None
        if response.request_id != request.request_id:
            raise EvidenceVaultAcquisitionTransportError("invalid_worker_response")
        if not response.ok or response.result is None:
            raise EvidenceVaultAcquisitionTransportError(
                response.error_code or "acquisition_failed"
            )
        return SignedAcquisitionResultEnvelope.model_validate_json(
            response.result.model_dump_json(),
            strict=True,
        )


def _send_frame(connection: socket.socket, payload: bytes, *, maximum: int) -> None:
    if not payload or len(payload) > maximum:
        raise ValueError("invalid frame length")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def _receive_frame(connection: socket.socket, *, maximum: int) -> bytes:
    header = _receive_exact(connection, _FRAME_HEADER_BYTES)
    length = struct.unpack("!I", header)[0]
    if length < 1 or length > maximum:
        raise ValueError("invalid frame length")
    return _receive_exact(connection, length)


def _receive_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        part = connection.recv(remaining)
        if not part:
            raise OSError("truncated frame")
        chunks.append(part)
        remaining -= len(part)
    return b"".join(chunks)


def _reject_trailing_bytes(connection: socket.socket) -> None:
    prior_timeout = connection.gettimeout()
    try:
        connection.setblocking(False)
        try:
            extra = connection.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            extra = b""
        if extra:
            raise ValueError("trailing IPC bytes")
    finally:
        connection.settimeout(prior_timeout)


def _strict_json_object(payload: bytes) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError("invalid JSON number")

    value = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=pairs,
        parse_constant=invalid_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("IPC payload must be an object")
    return value


def _validated_socket_path(value: str | os.PathLike[str]) -> str:
    path = Path(value)
    if not path.is_absolute() or "\x00" in os.fspath(path):
        raise ValueError("socket_path must be an absolute filesystem path")
    if len(os.fsencode(path)) > _MAX_SOCKET_PATH_BYTES:
        raise ValueError("socket_path is too long")
    return os.fspath(path)


def _validate_client_socket(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_mode & 0o007:
        raise ValueError("worker socket permissions are invalid")


__all__ = [
    "ACQUISITION_IPC_REQUEST_VERSION",
    "ACQUISITION_IPC_RESPONSE_VERSION",
    "EvidenceVaultAcquisitionTransportError",
    "UnixTrustedAcquisitionClient",
]
