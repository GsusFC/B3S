"""Durable, read-optimized domain index for completed immutable reports."""

from __future__ import annotations

from contextlib import closing, contextmanager
from asyncio import current_task
import fcntl
import os
from pathlib import Path
import sqlite3
from threading import local
from typing import Any, Iterator

from src.history.models import ReportConflictError
from src.history.report_parser import canonical_json_hash
from src.services.scanner_report_assessment import assessment_projection_from_report


class CatalogUnavailable(RuntimeError):
    """The catalog cannot prove its completeness against durable history."""


_lock_owner = local()


def _actor():
    try:
        return current_task()
    except RuntimeError:
        return None


def _require_lock(reports_root: Path) -> None:
    if getattr(_lock_owner, "lease", None) != (reports_root.resolve(), _actor()):
        raise RuntimeError("brand catalog write requires catalog_lock")


def catalog_path(reports_root: Path) -> Path:
    return reports_root / ".brand-catalog.sqlite3"


@contextmanager
def catalog_lock(reports_root: Path) -> Iterator[None]:
    """Serialize writers and offline repair across processes on one volume."""

    if getattr(_lock_owner, "lease", None) is not None:
        raise RuntimeError("catalog_lock cannot be nested")
    reports_root.mkdir(parents=True, exist_ok=True)
    with (reports_root / ".brand-catalog.lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _lock_owner.lease = (reports_root.resolve(), _actor())
        try:
            yield
        finally:
            del _lock_owner.lease
            fcntl.flock(handle, fcntl.LOCK_UN)


def _validate_write(reports_root: Path, report: dict[str, Any], domain: str) -> None:
    _require_lock(reports_root)
    from web.report_store import domain_key

    if domain != domain_key(str(report.get("url") or "")):
        raise ValueError("brand catalog domain does not match report URL")


def _connect(reports_root: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = catalog_path(reports_root)
    new_index = not path.exists()
    if readonly:
        if not path.is_file():
            raise CatalogUnavailable("brand catalog has not been reconciled")
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        conn.execute("PRAGMA synchronous = FULL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalog_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
                postgres_reconciled INTEGER NOT NULL CHECK (postgres_reconciled IN (0, 1))
            );
            INSERT OR IGNORE INTO catalog_state (singleton, complete, postgres_reconciled)
                VALUES (1, 0, 0);
            CREATE TABLE IF NOT EXISTS report_domains (
                report_id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'ready', 'conflict'))
            );
            CREATE INDEX IF NOT EXISTS idx_report_domains_state_domain
                ON report_domains (state, domain);
            """
        )
        if new_index:
            directory_fd = os.open(reports_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    return conn


def begin_intent(reports_root: Path, report: dict[str, Any], domain: str) -> None:
    """Durably record intent before PostgreSQL or file report side effects."""

    _validate_write(reports_root, report, domain)
    report_id = str(report["id"])
    fingerprint = canonical_json_hash(report)
    conflict = False
    with closing(_connect(reports_root)) as conn, conn:
        row = conn.execute(
            "SELECT domain, payload_hash FROM report_domains WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        if row is not None and (row["domain"], row["payload_hash"]) != (domain, fingerprint):
            conn.execute(
                "UPDATE report_domains SET state = 'conflict' WHERE report_id = ?",
                (report_id,),
            )
            conflict = True
        else:
            conn.execute(
                """
                INSERT INTO report_domains (report_id, domain, payload_hash, state)
                VALUES (?, ?, ?, 'pending')
                ON CONFLICT (report_id) DO UPDATE SET state = 'pending'
                """,
                (report_id, domain, fingerprint),
            )
    if conflict:
        raise ReportConflictError(f"report {report_id} already exists with different content")


def mark_ready(reports_root: Path, report: dict[str, Any], domain: str) -> None:
    """Publish one report only after its immutable file is durable."""

    _validate_write(reports_root, report, domain)
    report_id = str(report["id"])
    fingerprint = canonical_json_hash(report)
    with closing(_connect(reports_root)) as conn, conn:
        updated = conn.execute(
            """
            UPDATE report_domains SET state = 'ready'
            WHERE report_id = ? AND domain = ? AND payload_hash = ? AND state = 'pending'
            """,
            (report_id, domain, fingerprint),
        )
        if updated.rowcount != 1:
            raise CatalogUnavailable("brand catalog intent was not persisted")


def mark_incomplete(reports_root: Path) -> None:
    """Block catalog reads before an offline direct import begins."""

    _require_lock(reports_root)
    with closing(_connect(reports_root)) as conn, conn:
        conn.execute("UPDATE catalog_state SET complete = 0 WHERE singleton = 1")


def list_domains(
    reports_root: Path,
    *,
    limit: int,
    cursor: str | None,
    require_postgres_reconciliation: bool = False,
) -> tuple[list[str], bool, bool]:
    """Read one deterministic page without hydrating the report archive."""

    if not 1 <= limit <= 100:
        raise ValueError("brand catalog limit is out of range")
    try:
        with closing(_connect(reports_root, readonly=True)) as conn:
            conn.execute("BEGIN")
            state = conn.execute(
                "SELECT complete, postgres_reconciled FROM catalog_state WHERE singleton = 1"
            ).fetchone()
            if state is None or state["complete"] != 1 or (
                require_postgres_reconciliation and state["postgres_reconciled"] != 1
            ):
                raise CatalogUnavailable("brand catalog reconciliation is incomplete")
            unsettled = conn.execute(
                "SELECT 1 FROM report_domains WHERE state IN ('pending', 'conflict') LIMIT 1"
            ).fetchone()
            if unsettled is not None:
                raise CatalogUnavailable("brand catalog contains pending or conflicted reports")
            rows = conn.execute(
                """
                SELECT domain FROM report_domains
                WHERE state = 'ready' AND domain != '' AND domain > ?
                GROUP BY domain ORDER BY domain LIMIT ?
                """,
                (cursor or "", limit + 1),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise CatalogUnavailable("brand catalog is unavailable") from exc
    return (
        [str(row["domain"]) for row in rows[:limit]],
        len(rows) > limit,
        bool(state["postgres_reconciled"]),
    )


def repair_catalog(
    reports_root: Path,
    *,
    repository: Any = None,
    workspace_slug: str = "b3s",
    lock_held: bool = False,
) -> int:
    """Offline reconciliation of PostgreSQL and files; publish atomically."""

    if lock_held:
        _require_lock(reports_root)
        return _repair_locked(reports_root, repository, workspace_slug)
    with catalog_lock(reports_root):
        return _repair_locked(reports_root, repository, workspace_slug)


def _repair_locked(reports_root: Path, repository: Any, workspace_slug: str) -> int:
    from web import report_store

    mark_incomplete(reports_root)
    with closing(_connect(reports_root)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM report_domains")
        count = 0

        def add_report(report_id: str, report: dict[str, Any]) -> None:
            nonlocal count
            fingerprint = canonical_json_hash(report)
            prior = conn.execute(
                "SELECT payload_hash FROM report_domains WHERE report_id = ?",
                (report_id,),
            ).fetchone()
            if prior is not None:
                if prior["payload_hash"] != fingerprint:
                    raise ReportConflictError(
                        f"report {report_id} already exists in postgres and file stores with different content"
                    )
                return
            conn.execute(
                """
                INSERT INTO report_domains (report_id, domain, payload_hash, state)
                VALUES (?, ?, ?, 'ready')
                """,
                (report_id, report_store.domain_key(str(report.get("url") or "")), fingerprint),
            )
            count += 1

        if repository is not None:
            offset = 0
            while True:
                page = repository.list_report_summaries(
                    workspace_slug=workspace_slug, limit=200, offset=offset
                )
                for row in page:
                    report_id = str(row.get("id") or "")
                    if not report_id:
                        raise CatalogUnavailable("PostgreSQL report has no identity")
                    payload = repository.get_report_payload(report_id, workspace_slug=workspace_slug)
                    if not isinstance(payload, dict):
                        raise CatalogUnavailable("PostgreSQL report payload is missing")
                    assessment_projection_from_report(payload)
                    add_report(report_id, payload)
                if len(page) < 200:
                    break
                offset += 200

        if reports_root.is_dir():
            for path in reports_root.glob("*.json"):
                report = report_store._read_report_file(path, expected_id=path.stem)
                add_report(str(report.get("id") or path.stem), report)

        conn.execute(
            "UPDATE catalog_state SET complete = 1, postgres_reconciled = ? WHERE singleton = 1",
            (1 if repository is not None else 0,),
        )
    return count
