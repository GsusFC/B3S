"""Durable, API-independent controller for frozen Vault exact resumes."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable

from src.services.evidence_vault_scan_orchestration import VaultExactResumeError
from src.storage.scanner_api_jobs import ScannerResumeActionCorruptionError, ScannerResumeActionReservation
from src.storage.sqlite_store import SQLiteStore
from web.scan_runner import _run_vault_exact_resume


_EXECUTION_FAILURE = "vault_exact_resume_execution_failed"


@dataclass(frozen=True)
class VaultExactResumeLaunch:
    action_id: str
    scan_id: str
    outcome: str


def launch_vault_exact_resume_action(
    *,
    reservation: ScannerResumeActionReservation,
    repository: Any,
    database_path: str | None = None,
    thread_factory: Callable[..., Any] | None = None,
) -> VaultExactResumeLaunch:
    """Claim a newly reserved action once, then run only the frozen resume seam."""

    action_id, scan_id = reservation.get("action_id"), reservation.get("scan_id")
    if reservation.get("outcome") != "created" or not isinstance(action_id, str) or not isinstance(scan_id, str):
        return VaultExactResumeLaunch(str(action_id or ""), str(scan_id or ""), "ignored")
    try:
        action = _load_action(action_id, database_path)
        if action is None or action["scan_id"] != scan_id:
            return VaultExactResumeLaunch(action_id, scan_id, "invalid")
        if action["state"] != "accepted":
            return VaultExactResumeLaunch(action_id, scan_id, "stale")
        store = _open_store(database_path)
        try:
            transition = store.start_scanner_resume_action(action_id=action_id)
        finally:
            store.close()
    except (ScannerResumeActionCorruptionError, ValueError):
        return VaultExactResumeLaunch(action_id, scan_id, "invalid")
    if transition["outcome"] != "started":
        return VaultExactResumeLaunch(action_id, scan_id, transition["outcome"])
    try:
        (thread_factory or threading.Thread)(
            target=lambda: _run_reserved_action(action_id, scan_id, repository, database_path), daemon=True
        ).start()
    except Exception:
        _finish(action_id, database_path, _execution_failure())
        return VaultExactResumeLaunch(action_id, scan_id, "thread_start_failed")
    return VaultExactResumeLaunch(action_id, scan_id, "started")


def recover_interrupted_vault_exact_resume_actions(*, database_path: str | None = None) -> int:
    """Fence orphaned accepted/running actions before this process serves work."""

    store = _open_store(database_path)
    try:
        return store.interrupt_incomplete_scanner_resume_actions()
    finally:
        store.close()


def _run_reserved_action(action_id: str, scan_id: str, repository: Any, database_path: str | None) -> None:
    try:
        action = _load_action(action_id, database_path)
        if action is None or action["scan_id"] != scan_id or action["state"] != "running":
            return
        publication = _run_vault_exact_resume(scan_id=scan_id, action=action, repository=repository)
        payload = {
            "state": "completed",
            "publication_action": getattr(publication, "action", None),
            "report_id": getattr(publication, "report_id", None),
        }
    except VaultExactResumeError as exc:
        payload = {"state": "failed", "reason_code": exc.reason_code, "retryable": exc.retryable}
    except Exception:
        payload = _execution_failure()
    _finish(action_id, database_path, payload)


def _finish(action_id: str, database_path: str | None, payload: dict[str, Any]) -> None:
    try:
        _finalize(action_id, database_path, payload)
    except Exception:
        if payload == _execution_failure():
            return
        try:
            _finalize(action_id, database_path, _execution_failure())
        except Exception:
            return


def _finalize(action_id: str, database_path: str | None, payload: dict[str, Any]) -> None:
    store = _open_store(database_path)
    try:
        store.finalize_scanner_resume_action(action_id=action_id, state=payload["state"], status_payload=payload)
    finally:
        store.close()


def _load_action(action_id: str, database_path: str | None):
    store = _open_store(database_path)
    try:
        return store.get_scanner_resume_action(action_id=action_id)
    finally:
        store.close()


def _open_store(database_path: str | None) -> SQLiteStore:
    if database_path is None:
        from src.config import BRAND3_DB_PATH

        database_path = BRAND3_DB_PATH
    return SQLiteStore(database_path)


def _execution_failure() -> dict[str, Any]:
    return {"state": "failed", "reason_code": _EXECUTION_FAILURE, "retryable": True}
