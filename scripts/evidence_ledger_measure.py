#!/usr/bin/env python3
"""Measure shadow evidence-ledger states and tile re-scan decisions across consecutive Vault scans, read-only.

Usage, on the Vault machine:

    B3S_DATABASE_URL=... python -B scripts/evidence_ledger_measure.py --domain brand.com SCAN_1 SCAN_2 [SCAN_3 ...]

Every statement runs in one REPEATABLE READ, READ ONLY transaction. Before any fact
is read, that same transaction proves ``transaction_read_only`` is on and
``transaction_isolation`` is repeatable read. The session default is not required,
so a pooled URL works. Only single SELECT or WITH statements pass the guard; anything
else aborts the run. The JSON report goes to stdout and carries fingerprints,
never evidence text.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, TextIO

if __name__ == "__main__":
    # Stdout is the only output: never write __pycache__ files next to the app on the Vault machine.
    sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.history.report_parser import normalize_domain  # noqa: E402
from src.services import evidence_vault_evidence_ledger as ledger  # noqa: E402
from src.services import evidence_vault_tile_rescan_rule as tile_rule  # noqa: E402


MEASURE_SCHEMA_VERSION = "evidence-vault-evidence-ledger-measure-v1"
_DATABASE_URL_ENV = "B3S_DATABASE_URL"
_ABORT_EXIT = 2
_REQUIRED_TRANSACTION_SETTINGS = {"transaction_read_only": "on", "transaction_isolation": "repeatable read"}
# "Most likely did not change": at least 9 of every 10 owned sources keep Jaccard >= 0.80.
_UNCHANGED_NUMERATOR, _UNCHANGED_DENOMINATOR = 9, 10
_LEADING_COMMENTS = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.S)
# A WITH statement can still carry a data-modifying CTE, so its body is checked too.
_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|TRUNCATE|COPY|CREATE|ALTER|DROP|GRANT|REVOKE|CALL|LOCK|VACUUM|"
    r"REINDEX|CLUSTER|REFRESH|NOTIFY|LISTEN|DISCARD|RESET|PREPARE|EXECUTE|SET)\b",
    re.I,
)
_ROW_LOCK = re.compile(r"\bFOR\s+(NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b", re.I)
_SIDE_EFFECT_FUNCTIONS = re.compile(
    r"\b(pg_(try_)?advisory_\w+|nextval|setval|set_config|pg_notify|txid_current|pg_current_xact_id|"
    r"lo_import|lo_export|lo_unlink|dblink\w*|pg_terminate_backend|pg_cancel_backend|pg_reload_conf)\s*\(",
    re.I,
)

_BRAND_SQL = """
SELECT brands.id, brands.workspace_id, brands.canonical_domain
FROM b3s_history.brands AS brands
JOIN b3s_history.workspaces AS workspaces ON workspaces.id = brands.workspace_id
WHERE workspaces.slug = %s AND brands.canonical_domain = %s
"""
_SCANS_SQL = """
SELECT scans.source_scan_id, scans.acquisition_state, captures.id AS capture_id,
       COALESCE(NULLIF(captures.raw_payload, '{}'::jsonb), scans.request_payload -> 'capture_payload') AS snapshot
FROM b3s_history.scan_runs AS scans
JOIN b3s_history.captures AS captures ON captures.scan_run_id = scans.id
WHERE scans.workspace_id = %s AND scans.brand_id = %s AND scans.source_scan_id = ANY(%s)
"""
_EVIDENCE_SQL = """
SELECT capture_id, evidence_ref, source, source_class, evidence_type, url, content, content_raw, confidence, metadata
FROM b3s_history.evidence_records
WHERE capture_id = ANY(%s)
ORDER BY capture_id, evidence_ref
"""
_CHECKPOINTS_SQL = """
SELECT source_scan_id,
       checkpoint_payload #> '{healthy_workset,component_evaluations}' AS component_evaluations,
       checkpoint_payload #> '{healthy_workset,evaluated_tile_judgments}' AS evaluated_tile_judgments,
       shared_process_payload #> '{flow_context,candidate}' AS candidate,
       shared_process_payload -> 'component_result' AS component_result
FROM b3s_history.evidence_vault_sv9_evaluation_checkpoints
WHERE workspace_id = %s AND brand_id = %s AND source_scan_id = ANY(%s)
ORDER BY created_at, id
"""
_SNAPSHOTS_SQL = """
SELECT candidates.source_scan_id,
       candidates.candidate_payload -> 'component_evaluations' AS component_evaluations,
       snapshots.payload -> 'component_provenance' AS component_provenance,
       snapshots.payload -> 'evaluation_components' AS component_results,
       snapshots.payload #> '{analysis_payload,flow,candidate}' AS flow_candidate
FROM b3s_history.evidence_vault_sv9_judgment_candidates AS candidates
JOIN b3s_history.evidence_vault_sv9_shared_analysis_snapshots AS snapshots ON snapshots.candidate_id = candidates.id
WHERE candidates.workspace_id = %s AND candidates.brand_id = %s AND candidates.source_scan_id = ANY(%s)
ORDER BY candidates.created_at, candidates.id
"""
_EVENTS_SQL = """
SELECT event_type, candidate_id
FROM b3s_history.evidence_vault_sv9_judgment_authority_events
WHERE workspace_id = %s AND brand_id = %s
ORDER BY sequence
"""
_CANDIDATE_SQL = """
SELECT id, source_scan_id, candidate_payload -> 'candidate_tile_judgments' AS tile_judgments,
       candidate_payload -> 'candidate_component_sentinels' AS component_sentinels
FROM b3s_history.evidence_vault_sv9_judgment_candidates
WHERE workspace_id = %s AND brand_id = %s AND id = %s
"""


class ReadOnlyViolation(RuntimeError):
    """A statement or connection could not be proven read-only."""


def statement_allowed(sql: str) -> bool:
    """Only one SELECT or WITH statement with no writes, row locks or side-effect functions."""

    body = " ".join(_LEADING_COMMENTS.sub("", str(sql)).split()).rstrip(";").strip()
    first = body.split(" ", 1)[0].upper() if body else ""
    return first in {"SELECT", "WITH"} and not (
        ";" in body or _WRITE_KEYWORDS.search(body) or _ROW_LOCK.search(body) or _SIDE_EFFECT_FUNCTIONS.search(body)
    )


class ReadOnlyGuard:
    """Counts every statement decision; a blocked statement aborts the run."""

    def __init__(self) -> None:
        self.allowed = 0
        self.blocked: list[str] = []

    def check(self, sql: str) -> None:
        if statement_allowed(sql):
            self.allowed += 1
            return
        label = " ".join(_LEADING_COMMENTS.sub("", str(sql)).split())[:96]
        self.blocked.append(label)
        raise ReadOnlyViolation(f"blocked non-read statement: {label}")

    def summary(self) -> dict[str, Any]:
        return {"statements_allowed": self.allowed, "statements_blocked": len(self.blocked), "blocked_statements": list(self.blocked)}


class ReadOnlyReader:
    """The only path to the database: every statement passes the guard before it is sent."""

    def __init__(self, connection: Any, guard: ReadOnlyGuard) -> None:
        self._connection = connection
        self.guard = guard
        self.settings: dict[str, str] = {}

    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        self.guard.check(sql)
        return [dict(row) for row in self._connection.execute(sql, params).fetchall()]

    def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None


@contextmanager
def open_read_only_reader(
    dsn: str, *, connect: Callable[..., Any], guard: ReadOnlyGuard | None = None
) -> Iterator[ReadOnlyReader]:
    """Open one REPEATABLE READ, READ ONLY transaction and prove it before any fact read."""

    from psycopg import IsolationLevel
    from psycopg.rows import dict_row

    connection = connect(dsn, row_factory=dict_row, connect_timeout=5)
    try:
        connection.read_only = True
        connection.isolation_level = IsolationLevel.REPEATABLE_READ
        reader = ReadOnlyReader(connection, guard or ReadOnlyGuard())
        reader.settings = _verified_settings(reader)
        yield reader
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def load_scan_facts(
    reader: ReadOnlyReader, *, domain: str, workspace_slug: str, scan_ids: Sequence[str]
) -> dict[str, Any]:
    """Read one brand's scan series in the given order."""

    brand = reader.fetch_one(_BRAND_SQL, (workspace_slug, domain))
    if brand is None:
        raise LookupError(f"brand {domain!r} is not in workspace {workspace_slug!r}; check --domain and --workspace")
    scope = (brand["workspace_id"], brand["id"])
    scans = {row["source_scan_id"]: row for row in reader.fetch_all(_SCANS_SQL, (*scope, list(scan_ids)))}
    missing = [scan_id for scan_id in scan_ids if scan_id not in scans]
    if missing:
        raise LookupError(f"scans {missing} have no capture for {domain!r}; check the scan ids")
    evidence = _evidence_by_capture(reader.fetch_all(_EVIDENCE_SQL, ([scans[scan_id]["capture_id"] for scan_id in scan_ids],)))
    checkpoints = reader.fetch_all(_CHECKPOINTS_SQL, (*scope, list(scan_ids)))
    evaluations = _scan_evaluations(checkpoints, reader.fetch_all(_SNAPSHOTS_SQL, (*scope, list(scan_ids))))
    judgments = _scan_judgments(checkpoints)
    return {
        "domain": str(brand["canonical_domain"]),
        "scans": [
            {**_scan_facts(scans[scan_id], evidence, evaluations), "judgments": judgments.get(scan_id, [])}
            for scan_id in scan_ids
        ],
        "accepted": _accepted_candidate(reader, scope),
    }


def measure_scan_series(
    *, domain: str, scans: Sequence[Mapping[str, Any]], accepted: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Ledger every consecutive pair of an ordered scan series; no database access.

    Each scan is ``{"scan_id", "snapshot", "evidence_rows", "evaluations", "judgments"}``,
    where ``judgments`` are the tile judgments Core gave in that scan;
    ``accepted`` is ``{"candidate_id", "source_scan_id", "tile_judgments", "component_sentinels"}`` or None.
    """

    scan_ids = [str(scan["scan_id"]) for scan in scans]
    judgments = [row for row in (accepted or {}).get("tile_judgments") or [] if row.get("supporting_evidence")]
    pairs = [_measure_pair(domain, prior, current, judgments, accepted) for prior, current in zip(scans, scans[1:])]
    accepted_summary = None
    warnings = ["no_accepted_candidate"]
    if accepted is not None:
        in_series = str(accepted["source_scan_id"]) in scan_ids
        accepted_summary = {
            "candidate_id": str(accepted["candidate_id"]),
            "source_scan_id": str(accepted["source_scan_id"]),
            "source_scan_in_series": in_series,
            "tile_count": len(judgments),
        }
        # Supporting pairs only match rows whose prior capture is the accepted candidate's scan.
        warnings = [] if in_series else ["accepted_candidate_source_scan_not_in_series"]
    return {
        "schema_version": MEASURE_SCHEMA_VERSION,
        "policy_version": ledger.EVIDENCE_LEDGER_POLICY_VERSION,
        "runtime_effect": False,
        "domain": domain,
        "scan_ids": scan_ids,
        "accepted_candidate": accepted_summary,
        "warnings": warnings,
        "pairs": pairs,
        "summary": _series_summary(pairs),
    }


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    connect: Callable[..., Any] | None = None,
    stdout: TextIO | None = None,
) -> int:
    args = _parse_args(argv)
    out = sys.stdout if stdout is None else stdout
    dsn = (os.environ if environ is None else environ).get(_DATABASE_URL_ENV, "").strip()
    if not dsn:
        return _emit(out, {"status": "aborted", "error": f"{_DATABASE_URL_ENV} is not set; export the Vault URL and retry"})
    from psycopg import Error as DatabaseError

    guard = ReadOnlyGuard()
    settings: dict[str, str] = {}
    try:
        with open_read_only_reader(dsn, connect=connect or _psycopg_connect(), guard=guard) as reader:
            settings = reader.settings
            facts = load_scan_facts(
                reader, domain=normalize_domain(args.domain), workspace_slug=args.workspace, scan_ids=args.scan_ids
            )
    except (ReadOnlyViolation, LookupError) as exc:
        return _emit(out, {"status": "aborted", "error": str(exc), "read_only": {**settings, **guard.summary()}})
    except (DatabaseError, UnicodeDecodeError) as exc:
        return _emit(out, {"status": "aborted", "error": _read_error(exc), "read_only": {**settings, **guard.summary()}})
    report = measure_scan_series(domain=facts["domain"], scans=facts["scans"], accepted=facts["accepted"])
    return _emit(out, {"status": "ok", **report, "read_only": {**settings, **guard.summary()}}, code=0)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", required=True, help="brand domain, for example brand.com")
    parser.add_argument("--workspace", default="b3s", help="Vault workspace slug")
    parser.add_argument("scan_ids", nargs="+", help="source scan ids, oldest first")
    args = parser.parse_args(argv)
    if len(args.scan_ids) < 2 or len(set(args.scan_ids)) != len(args.scan_ids):
        parser.error("pass at least two distinct scan ids, oldest first")
    return args


def _read_error(exc: Exception) -> str:
    """Name the failure without the driver's message, which can carry the database host and user."""

    if isinstance(exc, UnicodeDecodeError):
        return "an evidence record's content_raw is not valid UTF-8, so nothing was measured; inspect that capture"
    sqlstate = getattr(exc, "sqlstate", None) or "none"
    return (
        f"database read failed ({type(exc).__name__}, sqlstate {sqlstate}), so nothing was measured; "
        f"check {_DATABASE_URL_ENV}, the network and the b3s_history schema, then retry"
    )


def _psycopg_connect() -> Callable[..., Any]:
    import psycopg

    return psycopg.connect


def _verified_settings(reader: ReadOnlyReader) -> dict[str, str]:
    settings = {}
    for name, expected in _REQUIRED_TRANSACTION_SETTINGS.items():
        row = reader.fetch_one(f"SELECT current_setting('{name}') AS value")
        value = str((row or {}).get("value") or "unknown")
        if value != expected:
            raise ReadOnlyViolation(
                f"{name}={value}, expected {expected}: the run's transaction is not provably READ ONLY, "
                "REPEATABLE READ, so nothing was read. Check that B3S_DATABASE_URL reaches Postgres "
                "directly or through a session- or transaction-mode pooler, then retry."
            )
        settings[name] = value
    return settings


def _evidence_by_capture(rows: Iterable[Mapping[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        metadata = dict(row["metadata"] or {})
        metadata.setdefault("source_class", str(row["source_class"]))
        raw = row["content_raw"]
        grouped.setdefault(row["capture_id"], []).append(
            {
                "ref": str(row["evidence_ref"]),
                "source": str(row["source"]),
                "evidence_type": str(row["evidence_type"]),
                "url": str(row["url"]),
                "content": bytes(raw).decode("utf-8") if raw is not None else str(row["content"]),
                "confidence": str(row["confidence"]),
                "metadata": metadata,
            }
        )
    return grouped


def _scan_evaluations(
    checkpoints: Iterable[Mapping[str, Any]], snapshots: Iterable[Mapping[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Component evaluations per scan; a complete shared-analysis snapshot overrides checkpoints.

    A component Core evaluated more than once in a scan keeps only the evidence all of
    those prompts showed: a resumed scan can re-evaluate it on another request scope,
    and each tile keeps the verdict of whichever run judged it last.
    """

    by_scan: dict[str, dict[str, dict[str, Any]]] = {}
    for row in checkpoints:
        for evaluation in _mappings(row["component_evaluations"]):
            _put_evaluation(by_scan, row["source_scan_id"], evaluation, row["candidate"], row["component_result"])
    for row in snapshots:
        provenance = row["component_provenance"] if isinstance(row["component_provenance"], Mapping) else {}
        results = row["component_results"] if isinstance(row["component_results"], Mapping) else {}
        for evaluation in _mappings(row["component_evaluations"]):
            key = str(evaluation.get("component_key") or "")
            candidate = provenance.get(key) or row["flow_candidate"]
            _put_evaluation(by_scan, row["source_scan_id"], evaluation, candidate, results.get(key))
    return {scan_id: list(components.values()) for scan_id, components in by_scan.items()}


def _put_evaluation(
    by_scan: dict[str, dict[str, dict[str, Any]]],
    scan_id: Any,
    evaluation: Mapping[str, Any],
    candidate: Any,
    component_result: Any,
) -> None:
    key = str(evaluation.get("component_key") or "")
    previous = by_scan.get(str(scan_id), {}).get(key, {}).get("component_result")
    if isinstance(previous, Mapping) and isinstance(component_result, Mapping):
        shown = previous.get("evidence") or []
        component_result = {
            **component_result,
            "evidence": [item for item in component_result.get("evidence") or [] if item in shown],
        }
    by_scan.setdefault(str(scan_id), {})[key] = {
        "component_key": key,
        "status": str(evaluation.get("status") or ""),
        "candidate": candidate,
        "component_result": component_result,
    }


def _scan_judgments(checkpoints: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Tile judgments Core gave per scan; a later checkpoint for a tile replaces an earlier one.

    Only checkpoints carry them: a candidate's tile judgments also hold reused accepted ones.
    """

    by_scan: dict[str, dict[str, dict[str, Any]]] = {}
    for row in checkpoints:
        tiles = by_scan.setdefault(str(row["source_scan_id"]), {})
        for judgment in _mappings(row["evaluated_tile_judgments"]):
            tiles[str(judgment.get("tile_id") or "")] = dict(judgment)
    return {scan_id: list(tiles.values()) for scan_id, tiles in by_scan.items()}


def _scan_facts(
    row: Mapping[str, Any], evidence: Mapping[Any, list[dict[str, Any]]], evaluations: Mapping[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    snapshot = dict(row["snapshot"] or {})
    if not isinstance(snapshot.get("acquisition_gate"), Mapping):
        snapshot["acquisition_gate"] = {"state": row["acquisition_state"]}
    return {
        "scan_id": str(row["source_scan_id"]),
        "snapshot": snapshot,
        "evidence_rows": evidence.get(row["capture_id"], []),
        "evaluations": evaluations.get(str(row["source_scan_id"]), []),
    }


def _accepted_candidate(reader: ReadOnlyReader, scope: tuple[Any, Any]) -> dict[str, Any] | None:
    """The candidate of the latest adopt or supersede event; reopen events do not replace it."""

    events = reader.fetch_all(_EVENTS_SQL, scope)
    adopted = [row["candidate_id"] for row in events if row["event_type"] in {"adopt", "supersede"} and row["candidate_id"]]
    if not adopted:
        return None
    row = reader.fetch_one(_CANDIDATE_SQL, (*scope, adopted[-1]))
    if row is None:
        return None
    return {
        "candidate_id": str(row["id"]),
        "source_scan_id": str(row["source_scan_id"]),
        "tile_judgments": _mappings(row["tile_judgments"]),
        "component_sentinels": _mappings(row["component_sentinels"]),
    }


def _measure_pair(
    domain: str,
    prior: Mapping[str, Any],
    current: Mapping[str, Any],
    judgments: list[Mapping[str, Any]],
    accepted: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = ledger.build_evidence_ledger(
        brand_domain=domain,
        prior_snapshot=prior["snapshot"],
        prior_rows=prior["evidence_rows"],
        current_snapshot=current["snapshot"],
        current_rows=current["evidence_rows"],
        shown_index=ledger.build_shown_index(current["evaluations"]),
    )
    rows = result["rows"]
    basis = _unchanged_basis(ledger.compare_owned_pages(prior["snapshot"], current["snapshot"]))
    unchanged = _likely_unchanged(basis)
    candidates = [
        {key: row[key] for key in ("evidence_ref", "evidence_fingerprint", "source_key")}
        for row in rows
        if unchanged and row["state"] == ledger.VERIFIED_ABSENT
    ]
    tiles = ledger.summarize_tile_support(rows, judgments)
    # Diagnostic only: the projection never reaches the accepted authority or its score.
    projection = tile_rule.project_rescan(
        (accepted or {}).get("tile_judgments") or [],
        (accepted or {}).get("component_sentinels") or [],
        rows,
        current.get("judgments") or [],
    )
    return {
        "prior_scan_id": str(prior["scan_id"]),
        "current_scan_id": str(current["scan_id"]),
        "brand_likely_unchanged": unchanged,
        "unchanged_basis": basis,
        "rows": rows,
        "skipped_rows": result["skipped_rows"],
        "tiles": tiles,
        "false_verified_absent_candidates": candidates,
        **projection,
        "summary": _pair_summary(rows, result["skipped_rows"], tiles, candidates),
    }


def _unchanged_basis(pages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Owned sources are prior pages with text; a lastmod change needs both values present and different."""

    owned = [page for page in pages if page["in_prior"]]
    return {
        "owned_source_count": len(owned),
        "unchanged_source_count": sum(page["change"] == "unchanged" for page in owned),
        "lastmod_changed_sources": sorted(
            page["source_key"]
            for page in pages
            if page["prior_lastmod"] and page["current_lastmod"] and page["prior_lastmod"] != page["current_lastmod"]
        ),
    }


def _likely_unchanged(basis: Mapping[str, Any]) -> bool:
    owned = basis["owned_source_count"]
    return (
        owned > 0
        and _UNCHANGED_DENOMINATOR * basis["unchanged_source_count"] >= _UNCHANGED_NUMERATOR * owned
        and not basis["lastmod_changed_sources"]
    )


def _pair_summary(
    rows: Sequence[Mapping[str, Any]],
    skipped: Mapping[str, int],
    tiles: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    classes = sorted({row["evidence_class"] for row in rows})
    return {
        "row_count": len(rows),
        "skipped_row_count": sum(skipped.values()),
        "state_counts": _counts(row["state"] for row in rows),
        "class_state_counts": {name: _counts(row["state"] for row in rows if row["evidence_class"] == name) for name in classes},
        "reason_counts": _counts(code for row in rows for code in row["reason_codes"]),
        "tile_count": len(tiles),
        "tiles_with_verified_absent": sum(ledger.VERIFIED_ABSENT in tile["state_counts"] for tile in tiles),
        "tiles_with_not_verified": sum(ledger.NOT_VERIFIED in tile["state_counts"] for tile in tiles),
        "false_verified_absent_candidate_count": len(candidates),
    }


def _series_summary(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    states, reasons = Counter(), Counter()
    for pair in pairs:
        states.update(pair["summary"]["state_counts"])
        reasons.update(pair["summary"]["reason_counts"])
    return {
        "pair_count": len(pairs),
        "pairs_likely_unchanged": sum(pair["brand_likely_unchanged"] for pair in pairs),
        "row_count": sum(pair["summary"]["row_count"] for pair in pairs),
        "state_counts": dict(sorted(states.items())),
        "reason_counts": dict(sorted(reasons.items())),
        "false_verified_absent_candidate_count": sum(
            pair["summary"]["false_verified_absent_candidate_count"] for pair in pairs
        ),
    }


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _emit(out: TextIO, payload: Mapping[str, Any], *, code: int = _ABORT_EXIT) -> int:
    out.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
