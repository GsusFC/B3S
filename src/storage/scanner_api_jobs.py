"""Durable job envelopes and idempotency reservations for the Scanner API."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any, Literal, TypedDict
from uuid import uuid4

from .json_payloads import json_dumps, safe_json_loads


class IdempotencyReservation(TypedDict):
    outcome: Literal["created", "replay", "conflict"]
    scan_id: str
    request_fingerprint: str


ResumeActionState = Literal["accepted", "running", "completed", "failed", "interrupted"]
ResumeActionTerminalState = Literal["completed", "failed", "interrupted"]


class ScannerResumeActionCorruptionError(ValueError):
    """Stored resume-action envelopes are invalid and cannot be trusted."""


class ScannerResumeAction(TypedDict):
    action_id: str
    scan_id: str
    state: ResumeActionState
    request_fingerprint: str
    request_payload: dict[str, str]
    status_payload: dict[str, str]
    created_at: str
    updated_at: str
    completed_at: str | None


class ScannerResumeActionReservation(TypedDict):
    outcome: Literal["created", "replay", "conflict"]
    scan_id: str
    action_id: str | None
    state: ResumeActionState | None


class ScannerResumeActionTransition(TypedDict):
    outcome: Literal["started", "finalized", "stale", "not_found"]
    action: ScannerResumeAction | None


_TERMINAL_STATES = {"done", "error", "cancelled"}
_INTERRUPTIBLE_STATES = {"accepted", "running", "blocked"}
_RESUME_TERMINAL = {"completed", "failed", "interrupted"}
_RESUME_STATES = _RESUME_TERMINAL | {"accepted", "running"}
_RESUME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_RESUME_FORBIDDEN = {"idempotencykey", "url", "provider", "prompt", "token", "secret", "credential", "authorization", "body", "payload", "evidence"}


class ScannerApiJobsStoreMixin:
    """Persistence methods mixed into :class:`SQLiteStore`.

    Only the control-plane envelope is stored here. Completed report payloads
    continue to live in the immutable B3S report store.
    """

    def reserve_scanner_api_job(
        self,
        *,
        scan_id: str,
        request_payload: dict[str, Any],
        status_payload: dict[str, Any],
        client_id: str,
        idempotency_key_hash: str | None,
        request_fingerprint: str,
    ) -> IdempotencyReservation:
        """Atomically reserve an idempotency key and scan id.

        A repeated key with the same normalized request replays the original
        scan. Reusing a key for a different request is a conflict.
        """

        now = _utc_now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key_hash:
                existing = self.conn.execute(
                    """
                    SELECT scan_id, request_fingerprint
                    FROM b3s_scanner_jobs
                    WHERE idempotency_key_hash = ?
                    """,
                    (idempotency_key_hash,),
                ).fetchone()
                if existing:
                    self.conn.commit()
                    existing_scan_id = str(existing["scan_id"])
                    existing_fingerprint = str(existing["request_fingerprint"] or "")
                    return {
                        "outcome": "replay" if existing_fingerprint == request_fingerprint else "conflict",
                        "scan_id": existing_scan_id,
                        "request_fingerprint": existing_fingerprint,
                    }

            self.conn.execute(
                """
                INSERT INTO b3s_scanner_jobs (
                    scan_id, state, phase, request_json, status_json, client_id,
                    idempotency_key_hash, request_fingerprint, created_at,
                    updated_at, completed_at
                ) VALUES (?, 'accepted', 'accepted', ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    str(scan_id),
                    json_dumps(request_payload),
                    json_dumps(status_payload),
                    str(client_id or ""),
                    idempotency_key_hash,
                    str(request_fingerprint or ""),
                    now,
                    now,
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {
            "outcome": "created",
            "scan_id": str(scan_id),
            "request_fingerprint": str(request_fingerprint or ""),
        }

    def save_scanner_job_status(
        self,
        status_payload: dict[str, Any],
        *,
        request_payload: dict[str, Any] | None = None,
        client_id: str = "",
    ) -> None:
        scan_id = str(status_payload.get("id") or "").strip()
        if not scan_id:
            raise ValueError("scanner job status requires an id")
        state = str(status_payload.get("state") or "unknown")
        phase = str(status_payload.get("phase") or "")
        now = _utc_now()
        completed_at = status_payload.get("completed_at")
        if state in _TERMINAL_STATES and not completed_at:
            completed_at = now

        self.conn.execute(
            """
            INSERT INTO b3s_scanner_jobs (
                scan_id, state, phase, request_json, status_json, client_id,
                idempotency_key_hash, request_fingerprint, created_at,
                updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, '', ?, ?, ?)
            ON CONFLICT(scan_id) DO UPDATE SET
                state = excluded.state,
                phase = excluded.phase,
                status_json = excluded.status_json,
                request_json = CASE
                    WHEN excluded.request_json = '{}' THEN b3s_scanner_jobs.request_json
                    ELSE excluded.request_json
                END,
                client_id = CASE
                    WHEN excluded.client_id = '' THEN b3s_scanner_jobs.client_id
                    ELSE excluded.client_id
                END,
                updated_at = excluded.updated_at,
                completed_at = excluded.completed_at
            WHERE b3s_scanner_jobs.state NOT IN ('done', 'error', 'cancelled')
                OR b3s_scanner_jobs.state = excluded.state
            """,
            (
                scan_id,
                state,
                phase,
                json_dumps(request_payload or {}),
                json_dumps(status_payload),
                str(client_id or ""),
                str(status_payload.get("started_at") or now),
                now,
                completed_at,
            ),
        )
        self.conn.commit()

    def get_scanner_job_status(self, scan_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT status_json
            FROM b3s_scanner_jobs
            WHERE scan_id = ?
            """,
            (str(scan_id),),
        ).fetchone()
        if not row:
            return None
        payload, error = safe_json_loads(
            row["status_json"],
            field="b3s_scanner_jobs.status_json",
            fallback=None,
        )
        if error or not isinstance(payload, dict):
            return None
        return payload

    def interrupt_incomplete_scanner_jobs(self) -> int:
        """Convert orphaned process-bound jobs into an explicit failure."""

        rows = self.conn.execute(
            """
            SELECT scan_id, status_json
            FROM b3s_scanner_jobs
            WHERE state IN ('accepted', 'running', 'blocked')
            """
        ).fetchall()
        if not rows:
            return 0
        now = _utc_now()
        interrupted_count = 0
        for row in rows:
            payload, _error = safe_json_loads(
                row["status_json"],
                field="b3s_scanner_jobs.status_json",
                fallback={},
            )
            status = payload if isinstance(payload, dict) else {}
            status.update(
                {
                    "id": str(row["scan_id"]),
                    "state": "error",
                    "phase": "interrupted",
                    "error": "Scan interrupted by process restart",
                    "error_code": "process_restarted",
                    "completed_at": now,
                }
            )
            for phase in status.get("phases") or []:
                if isinstance(phase, dict) and phase.get("state") in {
                    "pending",
                    "running",
                    "blocked",
                }:
                    phase["state"] = "interrupted"
            cursor = self.conn.execute(
                """
                UPDATE b3s_scanner_jobs
                SET state='error', phase='interrupted', status_json=?,
                    updated_at=?, completed_at=?
                WHERE scan_id=?
                    AND state IN ('accepted', 'running', 'blocked')
                """,
                (json_dumps(status), now, now, str(row["scan_id"])),
            )
            interrupted_count += cursor.rowcount
        self.conn.commit()
        return interrupted_count

    def reserve_scanner_resume_action(
        self,
        *,
        scan_id: str,
        request_payload: dict[str, Any],
        status_payload: dict[str, Any],
        client_id: str,
        idempotency_key_hash: str,
        request_fingerprint: str,
    ) -> ScannerResumeActionReservation:
        """Atomically reserve one strict exact-resume action."""
        scan_id, client_id = _resume_id(scan_id), _resume_id(client_id)
        key_hash = _resume_digest(idempotency_key_hash)
        fingerprint = _resume_digest(request_fingerprint)
        request_json, status_json = _resume_envelope(request_payload), _resume_envelope(status_payload, "accepted")
        now = _utc_now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM b3s_scanner_resume_actions WHERE client_id=? AND idempotency_key_hash=?", (client_id, key_hash)).fetchone()
            if row:
                action = _resume_action(row)
                self.conn.commit()
                if action["scan_id"] == scan_id and action["request_fingerprint"] == fingerprint:
                    return {"outcome": "replay", "scan_id": scan_id, "action_id": action["action_id"], "state": action["state"]}
                return _resume_conflict(scan_id)
            active = self.conn.execute("SELECT * FROM b3s_scanner_resume_actions WHERE scan_id=? AND state IN ('accepted', 'running')", (scan_id,)).fetchone()
            if active:
                _resume_action(active)
                self.conn.commit()
                return _resume_conflict(scan_id)
            action_id = uuid4().hex
            self.conn.execute(
                """INSERT INTO b3s_scanner_resume_actions (action_id, scan_id, client_id, idempotency_key_hash, request_fingerprint, state, request_json, status_json, created_at, updated_at, completed_at) VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, NULL)""",
                (action_id, scan_id, client_id, key_hash, fingerprint, request_json, status_json, now, now),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"outcome": "created", "scan_id": scan_id, "action_id": action_id, "state": "accepted"}

    def start_scanner_resume_action(self, *, action_id: str) -> ScannerResumeActionTransition:
        return self._transition(action_id, "running", _resume_envelope(_resume_status("running"), "running"))

    def finalize_scanner_resume_action(
        self, *, action_id: str, state: ResumeActionTerminalState, status_payload: dict[str, Any]
    ) -> ScannerResumeActionTransition:
        if state not in _RESUME_TERMINAL:
            raise ValueError("scanner resume action state must be terminal")
        return self._transition(action_id, state, _resume_envelope(status_payload, state))

    def _transition(self, action_id: str, state: str, status_json: str) -> ScannerResumeActionTransition:
        action_id, now = _resume_id(action_id), _utc_now()
        completed_at = now if state in _RESUME_TERMINAL else None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            action = _resume_action(self.conn.execute("SELECT * FROM b3s_scanner_resume_actions WHERE action_id=?", (action_id,)).fetchone())
            if action is None:
                self.conn.commit()
                return {"outcome": "not_found", "action": None}
            cursor = self.conn.execute(
                """UPDATE b3s_scanner_resume_actions SET state=?, status_json=?, updated_at=?, completed_at=? WHERE action_id=? AND (state='accepted' OR (? AND state IN ('accepted', 'running')))""",
                (state, status_json, now, completed_at, action_id, int(state in _RESUME_TERMINAL)),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if cursor.rowcount:
            action = {**action, "state": state, "status_payload": _resume_status(state), "updated_at": now, "completed_at": completed_at}
            return {"outcome": "started" if state == "running" else "finalized", "action": action}
        return {"outcome": "stale", "action": action}

    def get_scanner_resume_action(
        self, *, action_id: str | None = None, scan_id: str | None = None, client_id: str | None = None, idempotency_key_hash: str | None = None
    ) -> ScannerResumeAction | None:
        if action_id is not None:
            row = self.conn.execute("SELECT * FROM b3s_scanner_resume_actions WHERE action_id=?", (_resume_id(action_id),)).fetchone()
        elif None not in (scan_id, client_id, idempotency_key_hash):
            row = self.conn.execute("SELECT * FROM b3s_scanner_resume_actions WHERE scan_id=? AND client_id=? AND idempotency_key_hash=?", (_resume_id(scan_id), _resume_id(client_id), _resume_digest(idempotency_key_hash))).fetchone()
        else:
            raise ValueError("scanner resume action lookup requires action_id or scan/client/key")
        return _resume_action(row)

    def interrupt_incomplete_scanner_resume_actions(self) -> int:
        """CAS orphaned accepted/running actions to interrupted after restart."""
        rows = self.conn.execute("SELECT * FROM b3s_scanner_resume_actions WHERE state IN ('accepted', 'running')").fetchall()
        try:
            for row in rows:
                _resume_action(row)
            now, status_json = _utc_now(), _resume_envelope(_resume_status("interrupted"), "interrupted")
            count = sum(
                self.conn.execute("UPDATE b3s_scanner_resume_actions SET state='interrupted', status_json=?, updated_at=?, completed_at=? WHERE action_id=? AND state IN ('accepted', 'running')", (status_json, now, now, str(row["action_id"]))).rowcount
                for row in rows
            )
            self.conn.commit()
            return count
        except Exception:
            self.conn.rollback()
            raise


def _resume_conflict(scan_id: str) -> ScannerResumeActionReservation:
    return {"outcome": "conflict", "scan_id": scan_id, "action_id": None, "state": None}


def _resume_id(value: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _RESUME_ID.fullmatch(text):
        raise ValueError("scanner resume action identifier is invalid")
    return text


def _resume_digest(value: str) -> str:
    text = value.lower().strip() if isinstance(value, str) else ""
    if not _SHA256_HEX.fullmatch(text):
        raise ValueError("scanner resume action digest is invalid")
    return text


def _resume_status(state: str) -> dict[str, str]:
    return {"state": state, "phase": state}


def _resume_envelope(value: Any, state: str | None = None) -> str:
    _resume_json(value)
    expected = {"operation": "exact_resume"} if state is None else _resume_status(state)
    if value != expected:
        raise ValueError("scanner resume action envelope is invalid")
    return json_dumps(value)


def _resume_json(value: Any) -> None:
    if value is None or isinstance(value, (str, int, bool)) or isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _resume_json(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or re.sub(r"[^a-z0-9]", "", key.lower()) in _RESUME_FORBIDDEN:
                raise ValueError("scanner resume action envelope is invalid")
            _resume_json(item)
        return
    raise ValueError("scanner resume action envelope is invalid")


def _resume_action(row: Any) -> ScannerResumeAction | None:
    if row is None:
        return None
    request, request_error = safe_json_loads(row["request_json"], field="b3s_scanner_resume_actions.request_json", fallback=None)
    status, status_error = safe_json_loads(row["status_json"], field="b3s_scanner_resume_actions.status_json", fallback=None)
    state = str(row["state"])
    if request_error or status_error or state not in _RESUME_STATES:
        raise ScannerResumeActionCorruptionError("scanner resume action record is corrupt")
    try:
        _resume_envelope(request)
        _resume_envelope(status, state)
    except ValueError as exc:
        raise ScannerResumeActionCorruptionError("scanner resume action record is corrupt") from exc
    return {"action_id": str(row["action_id"]), "scan_id": str(row["scan_id"]), "state": state, "request_fingerprint": str(row["request_fingerprint"]), "request_payload": request, "status_payload": status, "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]), "completed_at": str(row["completed_at"]) if row["completed_at"] else None}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
