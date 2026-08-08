from __future__ import annotations

from copy import deepcopy
from threading import Event, Thread
from typing import Any, Mapping

import pytest
from pydantic import ValidationError

from src.services.evidence_vault_acquisition_worker import (
    SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION,
    EvidenceVaultAcquisitionReplayConflictError,
    EvidenceVaultAcquisitionWorkerError,
    SignedAcquisitionResultEnvelope,
    TrustedAcquisitionCommand,
    TrustedAcquisitionWorker,
)


def _command_payload(**extra: Any) -> dict[str, Any]:
    return {
        "workspace_slug": "b3s",
        "source_scan_id": "scan-70",
        "brand_url": "https://example.com",
        **extra,
    }


def _signed_payload(nonce: str = "nonce-1") -> dict[str, Any]:
    return {
        "pre_receipt_snapshot": {"raw_inputs": [{"source": "web", "payload": {"body": "example"}}]},
        "receipts": [
            {
                "receipt_fingerprint": "a" * 64,
                "claims": {"receipt_nonce": nonce},
                "signature": "public-signature",
            }
        ],
        "receipt_set_fingerprint": "b" * 64,
    }


def _worker(
    *,
    events: list[str] | None = None,
    sign_payload: Mapping[str, Any] | None = None,
    persist: Any | None = None,
) -> TrustedAcquisitionWorker:
    observed = events if events is not None else []
    payload = dict(sign_payload or _signed_payload())

    def collect(command: TrustedAcquisitionCommand) -> Mapping[str, Any]:
        observed.append("collect")
        return {"snapshot": {"source_scan_id": command.source_scan_id}}

    def sign(
        command: TrustedAcquisitionCommand,
        collected: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert collected["snapshot"] == {"source_scan_id": command.source_scan_id}
        observed.append("sign")
        return deepcopy(payload)

    def default_persist(
        command: TrustedAcquisitionCommand,
        signed: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert command.source_scan_id == "scan-70"
        observed.append("persist")
        return deepcopy(dict(signed))

    return TrustedAcquisitionWorker(
        collect=collect,
        sign=sign,
        persist=persist or default_persist,
    )


@pytest.mark.parametrize(
    "untrusted_field",
    ["raw_payload", "snapshot", "claims", "signed_payload", "private_key", "ingest_dsn"],
)
def test_command_rejects_every_caller_acquisition_field_before_capabilities_run(
    untrusted_field: str,
) -> None:
    events: list[str] = []
    worker = _worker(events=events)

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^invalid_command$"):
        worker.capture(_command_payload(**{untrusted_field: {"forged": True}}))

    assert events == []


def test_command_and_result_models_are_exact_field_and_strict() -> None:
    with pytest.raises(ValidationError):
        TrustedAcquisitionCommand.model_validate({**_command_payload(), "claims": {}})
    with pytest.raises(ValidationError):
        TrustedAcquisitionCommand.model_validate(
            {**_command_payload(), "source_scan_id": 70},
            strict=True,
        )
    with pytest.raises(ValidationError):
        SignedAcquisitionResultEnvelope.model_validate(
            {
                "schema_version": SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION,
                **_command_payload(),
                "signed_acquisition": _signed_payload(),
                "ingest_dsn": "postgresql://scanner-secret",
            },
            strict=True,
        )


def test_capture_orders_collection_signing_and_persistence_before_return() -> None:
    events: list[str] = []
    worker = _worker(events=events)

    result = worker.capture(_command_payload())
    events.append("returned")

    assert events == ["collect", "sign", "persist", "returned"]
    assert result.schema_version == SIGNED_ACQUISITION_RESULT_SCHEMA_VERSION
    assert result.workspace_slug == "b3s"
    assert result.source_scan_id == "scan-70"
    assert result.brand_url == "https://example.com"
    assert result.signed_acquisition == _signed_payload()
    assert not hasattr(result, "private_key")
    assert not hasattr(result, "ingest_dsn")


@pytest.mark.parametrize("failed_stage", ["collect", "sign", "persist"])
def test_stage_failure_is_explicit_and_returns_no_partial_result(failed_stage: str) -> None:
    events: list[str] = []
    result: list[SignedAcquisitionResultEnvelope] = []

    def collect(command: TrustedAcquisitionCommand) -> Mapping[str, Any]:
        events.append("collect")
        if failed_stage == "collect":
            raise RuntimeError("provider error containing a secret")
        return {"snapshot": command.source_scan_id}

    def sign(
        command: TrustedAcquisitionCommand,
        collected: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        events.append("sign")
        if failed_stage == "sign":
            raise RuntimeError("private-key-material")
        return _signed_payload()

    def persist(
        command: TrustedAcquisitionCommand,
        signed: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        events.append("persist")
        if failed_stage == "persist":
            raise RuntimeError("postgresql://scanner-ingest-secret")
        return deepcopy(dict(signed))

    worker = TrustedAcquisitionWorker(collect=collect, sign=sign, persist=persist)
    with pytest.raises(EvidenceVaultAcquisitionWorkerError) as exc_info:
        result.append(worker.capture(_command_payload()))

    assert result == []
    expected_error = {
        "collect": "collection_failed",
        "sign": "signing_failed",
        "persist": "persistence_failed",
    }
    assert str(exc_info.value) == expected_error[failed_stage]
    assert "secret" not in str(exc_info.value)
    expected = {
        "collect": ["collect"],
        "sign": ["collect", "sign"],
        "persist": ["collect", "sign", "persist"],
    }
    assert events == expected[failed_stage]


def test_exact_replay_returns_the_stored_public_result() -> None:
    stored: dict[tuple[str, str], dict[str, Any]] = {}

    def persist(
        command: TrustedAcquisitionCommand,
        signed: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        key = (command.workspace_slug, command.source_scan_id)
        stored.setdefault(key, deepcopy(dict(signed)))
        return deepcopy(stored[key])

    worker = _worker(persist=persist)

    first = worker.capture(_command_payload())
    second = worker.capture(_command_payload())

    assert second == first
    assert len(stored) == 1


def test_divergent_replay_fails_closed_instead_of_returning_stored_or_new_data() -> None:
    stored: dict[tuple[str, str], dict[str, Any]] = {}
    signed_attempts = iter((_signed_payload("nonce-1"), _signed_payload("nonce-2")))

    def collect(command: TrustedAcquisitionCommand) -> Mapping[str, Any]:
        return {"source_scan_id": command.source_scan_id}

    def sign(
        command: TrustedAcquisitionCommand,
        collected: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return next(signed_attempts)

    def persist(
        command: TrustedAcquisitionCommand,
        signed: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        key = (command.workspace_slug, command.source_scan_id)
        stored.setdefault(key, deepcopy(dict(signed)))
        return deepcopy(stored[key])

    worker = TrustedAcquisitionWorker(collect=collect, sign=sign, persist=persist)
    first = worker.capture(_command_payload())

    with pytest.raises(
        EvidenceVaultAcquisitionReplayConflictError,
        match="^persisted_result_diverged$",
    ):
        worker.capture(_command_payload())

    assert first.signed_acquisition["receipts"][0]["claims"]["receipt_nonce"] == "nonce-1"
    assert stored[("b3s", "scan-70")]["receipts"][0]["claims"]["receipt_nonce"] == "nonce-1"


def test_capture_cannot_return_while_persistence_is_incomplete() -> None:
    persist_entered = Event()
    allow_persist_to_finish = Event()
    returned = Event()
    results: list[SignedAcquisitionResultEnvelope] = []
    failures: list[BaseException] = []

    def persist(
        command: TrustedAcquisitionCommand,
        signed: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        persist_entered.set()
        assert allow_persist_to_finish.wait(timeout=2)
        return deepcopy(dict(signed))

    worker = _worker(persist=persist)

    def invoke() -> None:
        try:
            results.append(worker.capture(_command_payload()))
        except BaseException as exc:  # keep the assertion visible to the test thread
            failures.append(exc)
        finally:
            returned.set()

    thread = Thread(target=invoke, daemon=True)
    thread.start()
    assert persist_entered.wait(timeout=2)
    assert not returned.is_set()
    assert results == []

    allow_persist_to_finish.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures == []
    assert returned.is_set()
    assert len(results) == 1


def test_public_result_rejects_secret_bearing_signed_fields() -> None:
    worker = _worker(sign_payload={**_signed_payload(), "private_key": "must-not-cross"})

    with pytest.raises(EvidenceVaultAcquisitionWorkerError, match="^invalid_signed_result$"):
        worker.capture(_command_payload())
