"""Durable job envelopes and idempotency reservations for the Scanner API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

from .json_payloads import json_dumps, safe_json_loads


class IdempotencyReservation(TypedDict):
    outcome: Literal["created", "replay", "conflict"]
    scan_id: str
    request_fingerprint: str


_TERMINAL_STATES = {"done", "error", "cancelled"}
_INTERRUPTIBLE_STATES = {"accepted", "running", "blocked"}


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
