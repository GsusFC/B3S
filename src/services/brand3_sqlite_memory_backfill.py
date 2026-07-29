"""Read-only adapter from the Brand3 SQLite archive to B3S evidence memory."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from urllib.parse import urlparse

from src.evidence_identity import (
    canonical_evidence_digest,
    stable_artifact_digest,
)
from src.services.evidence_scoring_memory_preview import (
    apply_recovery_review_gate,
    build_evidence_scoring_memory_preview,
)
from src.services.evidence_scoring_recovery_review import (
    build_recovery_review_candidates,
    evaluate_recovery_reviews,
)
from src.sv9.rubric import RUBRIC_VERSION
from src.sv9_flow.contracts import EvidenceRecord
from src.sv9_flow.evidence_source import classify_source
from src.sv9_flow.evidence_worker import (
    build_evidence_pack_from_snapshot,
)


BRAND3_SQLITE_BACKFILL_VERSION = "brand3-sqlite-memory-backfill-v1"
BRAND3_SQLITE_BACKFILL_POLICY_VERSION = (
    "brand3-sqlite-memory-backfill-policy-v1"
)
_REQUIRED_TABLES = {
    "runs",
    "raw_inputs",
    "features",
    "evidence_items",
    "sv9_scans",
    "sv9_component_scores",
}


class Brand3SQLiteBackfillError(ValueError):
    """The archive cannot be read without guessing or mutating it."""


def load_brand3_sqlite_sv9_reports(
    database_path: str | Path,
    *,
    rubric_version: str = RUBRIC_VERSION,
) -> list[dict[str, Any]]:
    """Normalize compatible Brand3 scans into immutable B3S report payloads."""

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise Brand3SQLiteBackfillError(
            f"Brand3 SQLite archive does not exist: {path}"
        )
    with _read_only_connection(path) as conn:
        _validate_schema(conn)
        scans = conn.execute(
            """
            SELECT *
            FROM sv9_scans
            WHERE rubric_version = ?
            ORDER BY created_at, id
            """,
            (str(rubric_version),),
        ).fetchall()
        return [
            _report_from_scan(conn, scan)
            for scan in scans
        ]


def build_brand3_sqlite_memory_validation(
    database_path: str | Path,
    *,
    current_reports: Iterable[dict[str, Any]] = (),
    recovery_review_events: Iterable[dict[str, Any]] = (),
    rubric_version: str = RUBRIC_VERSION,
) -> dict[str, Any]:
    """Measure evaluator repeats, capture history, and the B3S bridge."""

    archive_reports = load_brand3_sqlite_sv9_reports(
        database_path,
        rubric_version=rubric_version,
    )
    current_rows = [
        dict(report)
        for report in current_reports
        if isinstance(report, dict)
        and _domain(str(report.get("url") or ""))
    ]
    archive_by_domain = _group_by_domain(archive_reports)
    current_by_domain = _group_by_domain(current_rows)
    domain_work: list[dict[str, Any]] = []
    for domain, all_scans in sorted(archive_by_domain.items()):
        capture_reports = _latest_report_per_capture(all_scans)
        all_scan_preview = build_evidence_scoring_memory_preview(
            all_scans,
            mode="shadow",
        )
        capture_preview = build_evidence_scoring_memory_preview(
            capture_reports,
            mode="shadow",
        )
        bridge_rows = [
            *capture_reports,
            *current_by_domain.get(domain, []),
        ]
        bridge_preview = (
            build_evidence_scoring_memory_preview(
                bridge_rows,
                mode="shadow",
            )
            if current_by_domain.get(domain)
            else None
        )
        domain_work.append(
            {
                "domain": domain,
                "archive_scan_count": len(all_scans),
                "archive_capture_count": len(capture_reports),
                "evaluator_repeat_count": (
                    len(all_scans) - len(capture_reports)
                ),
                "current_b3s_report_count": len(
                    current_by_domain.get(domain, [])
                ),
                "all_scan_preview": all_scan_preview,
                "all_scan_latest_report": _latest_report_for_preview(
                    all_scans,
                    all_scan_preview,
                ),
                "capture_preview": capture_preview,
                "capture_latest_report": _latest_report_for_preview(
                    capture_reports,
                    capture_preview,
                ),
                "bridge_preview": bridge_preview,
                "bridge_latest_report": (
                    _latest_report_for_preview(
                        bridge_rows,
                        bridge_preview,
                    )
                    if bridge_preview is not None
                    else None
                ),
            }
        )

    review_candidates = build_recovery_review_candidates(
        {
            "lane": lane,
            "preview": work[f"{lane}_preview"],
        }
        for work in domain_work
        for lane in ("all_scan", "capture", "bridge")
        if isinstance(work.get(f"{lane}_preview"), dict)
    )
    recovery_review = evaluate_recovery_reviews(
        review_candidates,
        recovery_review_events,
    )
    accepted_evidence_ids = recovery_review[
        "accepted_tile_evidence_ids"
    ]
    domains: list[dict[str, Any]] = []
    for work in domain_work:
        summarized: dict[str, Any] = {
            key: value
            for key, value in work.items()
            if not key.endswith("_preview")
            and not key.endswith("_latest_report")
        }
        for lane in ("all_scan", "capture", "bridge"):
            preview = work.get(f"{lane}_preview")
            latest_report = work.get(f"{lane}_latest_report")
            summarized[f"{lane}_preview"] = (
                _preview_summary(
                    apply_recovery_review_gate(
                        preview,
                        latest_report,
                        accepted_tile_evidence_ids=(
                            accepted_evidence_ids
                        ),
                    )
                )
                if isinstance(preview, dict)
                and isinstance(latest_report, dict)
                else None
            )
        domains.append(summarized)

    source_manifest = [
        {
            "report_id": str(report.get("id") or ""),
            "source_run_id": (
                report.get("raw") or {}
            ).get("source_run_id"),
            "created_at": str(report.get("created_at") or ""),
            "score": report.get("score"),
            "normalized_report_fingerprint": stable_artifact_digest(
                "brand3-sqlite-normalized-report-v1",
                {
                    "evidence_pack": (
                        ((report.get("raw") or {}).get("flow") or {})
                        .get("candidate", {})
                        .get("evidence_pack", {})
                    ),
                    "evaluation_result": (
                        ((report.get("raw") or {}).get("sv9") or {})
                        .get("result", {})
                    ),
                },
            ),
        }
        for report in archive_reports
    ]
    summary = {
        "archive_scan_count": len(archive_reports),
        "archive_domain_count": len(archive_by_domain),
        "archive_capture_count": sum(
            int(row["archive_capture_count"])
            for row in domains
        ),
        "evaluator_repeat_count": sum(
            int(row["evaluator_repeat_count"])
            for row in domains
        ),
        "archive_accepted_evidence_count": sum(
            int(
                row["all_scan_preview"][
                    "accepted_evidence_count"
                ]
            )
            for row in domains
        ),
        "evaluator_repeat_recovery_count": sum(
            len(
                _recovery_keys(row["all_scan_preview"])
                - _recovery_keys(row["capture_preview"])
            )
            for row in domains
        ),
        "capture_recovery_count": sum(
            int(
                row["capture_preview"][
                    "recovered_blind_spot_count"
                ]
            )
            for row in domains
        ),
        "capture_explicit_negative_conflict_count": sum(
            int(
                row["capture_preview"][
                    "explicit_negative_conflict_count"
                ]
            )
            for row in domains
        ),
        "bridge_domain_count": sum(
            1 for row in domains if row["bridge_preview"] is not None
        ),
        "bridge_recovery_count": sum(
            int(
                (row["bridge_preview"] or {}).get(
                    "recovered_blind_spot_count"
                )
                or 0
            )
            for row in domains
        ),
        "unmatched_current_domain_count": len(
            set(current_by_domain) - set(archive_by_domain)
        ),
        "recovery_review_candidate_count": int(
            recovery_review["summary"]["candidate_count"]
        ),
        "recovery_review_accepted_count": int(
            recovery_review["summary"]["accepted_count"]
        ),
    }
    promotion_blockers = [
        "runtime_scoring_wiring_disabled",
        "legacy_backfill_remains_read_only_shadow",
    ]
    if recovery_review["summary"]["pending_count"]:
        promotion_blockers.append(
            "recovered_tile_semantics_pending_review"
        )
    if (
        recovery_review["summary"]["disputed_count"]
        or recovery_review["summary"]["rejected_count"]
    ):
        promotion_blockers.append(
            "recovered_tile_semantics_not_accepted"
        )
    if (
        summary["capture_recovery_count"] == 0
        and summary["bridge_recovery_count"] == 0
    ):
        promotion_blockers.append(
            "no_cross_capture_positive_recovery_observed"
        )
    if not archive_reports:
        verdict = "insufficient_compatible_history"
    elif (
        summary["capture_recovery_count"] == 0
        and summary["bridge_recovery_count"] == 0
    ):
        verdict = "backfill_supported_positive_case_not_observed"
    else:
        verdict = "foundation_supported_activation_blocked"
    payload: dict[str, Any] = {
        "schema_version": BRAND3_SQLITE_BACKFILL_VERSION,
        "policy_version": BRAND3_SQLITE_BACKFILL_POLICY_VERSION,
        "mode": "shadow",
        "runtime_effect": False,
        "authority": False,
        "mutates_archive": False,
        "automatic_scoring_effect": False,
        "promotion_ready": False,
        "verdict": verdict,
        "rubric_version": str(rubric_version),
        "source_database": {
            "path": str(Path(database_path).expanduser().resolve()),
            "manifest_fingerprint": stable_artifact_digest(
                "brand3-sqlite-source-manifest-v1",
                {"reports": source_manifest},
            ),
        },
        "summary": summary,
        "promotion_blockers": promotion_blockers,
        "recovery_review_candidates": review_candidates,
        "recovery_review": recovery_review,
        "domains": domains,
        "warnings": [
            "legacy_five_dimension_scores_are_not_converted",
            "only_matching_sv9_rubric_scans_are_loaded",
            "same_capture_evaluator_repeats_are_measured_separately",
            "archive_is_opened_read_only",
            "preview_has_no_runtime_or_scoring_authority",
        ],
    }
    payload["state_fingerprint"] = stable_artifact_digest(
        BRAND3_SQLITE_BACKFILL_VERSION,
        {
            key: value
            for key, value in payload.items()
            if key not in {"state_fingerprint"}
        },
    )
    return payload


def _recovery_keys(
    preview: dict[str, Any],
) -> set[tuple[str, str, tuple[str, ...]]]:
    return {
        (
            str(row.get("component_key") or ""),
            str(row.get("tile_id") or ""),
            tuple(
                sorted(
                    str(evidence_id)
                    for evidence_id in row.get(
                        "tile_evidence_ids"
                    )
                    or []
                    if str(evidence_id)
                )
            ),
        )
        for row in preview.get("recoveries") or []
        if isinstance(row, dict)
    }


def _read_only_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _validate_schema(conn: sqlite3.Connection) -> None:
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = sorted(_REQUIRED_TABLES - tables)
    if missing:
        raise Brand3SQLiteBackfillError(
            "Brand3 SQLite archive is missing tables: "
            + ", ".join(missing)
        )


def _report_from_scan(
    conn: sqlite3.Connection,
    scan: sqlite3.Row,
) -> dict[str, Any]:
    scan_id = int(scan["id"])
    source_run_id = (
        int(scan["source_run_id"])
        if scan["source_run_id"] is not None
        else None
    )
    snapshot = _run_snapshot(
        conn,
        source_run_id=source_run_id,
        brand_name=str(scan["brand_name"] or ""),
        url=str(scan["url"] or ""),
    )
    pack = build_evidence_pack_from_snapshot(snapshot)
    pack.evidence = _merge_evidence_records(
        pack.evidence,
        _legacy_evidence_records(
            conn,
            source_run_id=source_run_id,
        ),
    )
    components = _components_for_scan(conn, scan_id=scan_id)
    component_list = [
        {
            "key": key,
            "status": component["status"],
            "score": component["score"],
            "scale": component["scale"],
            "points": component["points"],
            "tile_profile": component["tile_profile"],
        }
        for key, component in sorted(components.items())
    ]
    reliability = str(scan["reliability_status"] or "unknown")
    created_at = str(scan["created_at"] or "")
    return {
        "id": f"brand3-sqlite-sv9-{scan_id}",
        "brand_name": str(scan["brand_name"] or ""),
        "url": str(scan["url"] or ""),
        "created_at": created_at,
        "observed_at": created_at,
        "score": int(scan["brand3_score"] or 0),
        "base_average": scan["base_average"],
        "reliability_status": reliability,
        "not_detected": [
            key
            for key, component in components.items()
            if component["status"] == "not_detected"
        ],
        "limitations": [],
        "acquisition_gate": {"state": "legacy_persisted_capture"},
        "attempts": [],
        "acquisition_artifacts": [],
        "blocks": [],
        "components": component_list,
        "raw": {
            "schema_version": BRAND3_SQLITE_BACKFILL_VERSION,
            "source_run_id": source_run_id,
            "legacy_source": {
                "database": "brand3.sqlite3",
                "sv9_scan_id": scan_id,
                "source_run_id": source_run_id,
                "read_only": True,
            },
            "flow": {
                "candidate": {
                    "evidence_pack": pack.to_dict(),
                }
            },
            "sv9": {
                "brand3_score": int(scan["brand3_score"] or 0),
                "base_average": scan["base_average"],
                "reliability_status": reliability,
                "result": {
                    "rubric_version": str(
                        scan["rubric_version"] or ""
                    ),
                    "brand3_score": int(
                        scan["brand3_score"] or 0
                    ),
                    "base_average": scan["base_average"],
                    "magnetism_capped": bool(
                        scan["magnetism_capped"]
                    ),
                    "model": str(scan["model"] or ""),
                    "evaluator_model": str(
                        scan["evaluator_model"] or ""
                    ),
                    "components": components,
                },
            },
        },
    }


def _run_snapshot(
    conn: sqlite3.Connection,
    *,
    source_run_id: int | None,
    brand_name: str,
    url: str,
) -> dict[str, Any]:
    raw_inputs: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    if source_run_id is not None:
        for row in conn.execute(
            """
            SELECT source, payload_json, created_at
            FROM raw_inputs
            WHERE run_id = ?
            ORDER BY created_at, id
            """,
            (source_run_id,),
        ).fetchall():
            raw_inputs.append(
                {
                    "source": str(row["source"] or ""),
                    "payload": _json_value(row["payload_json"]),
                    "created_at": str(row["created_at"] or ""),
                }
            )
        features = [
            dict(row)
            for row in conn.execute(
                """
                SELECT dimension_name, feature_name, value, raw_value,
                       confidence, source
                FROM features
                WHERE run_id = ?
                ORDER BY id
                """,
                (source_run_id,),
            ).fetchall()
        ]
    return {
        "run": {
            "id": source_run_id,
            "brand_name": brand_name,
            "url": url,
        },
        "raw_inputs": raw_inputs,
        "features": features,
    }


def _legacy_evidence_records(
    conn: sqlite3.Connection,
    *,
    source_run_id: int | None,
) -> list[EvidenceRecord]:
    if source_run_id is None:
        return []
    records: list[EvidenceRecord] = []
    rows = conn.execute(
        """
        SELECT id, source, url, quote, feature_name, dimension_name,
               confidence, freshness_days, created_at
        FROM evidence_items
        WHERE run_id = ?
        ORDER BY id
        """,
        (source_run_id,),
    ).fetchall()
    for row in rows:
        quote = str(row["quote"] or "").strip()
        if not quote:
            continue
        source = str(row["source"] or "legacy_evidence")
        evidence_type = ".".join(
            item
            for item in (
                "legacy_evidence",
                str(row["dimension_name"] or "").strip(),
                str(row["feature_name"] or "").strip(),
            )
            if item
        )
        ref = f"legacy_evidence_items.{int(row['id'])}"
        source_class = classify_source(
            ref=ref,
            source=source,
            evidence_type=evidence_type,
            content=quote,
        )
        records.append(
            EvidenceRecord(
                ref=ref,
                source=source,
                evidence_type=evidence_type,
                content=quote,
                url=str(row["url"] or "") or None,
                confidence=_confidence(row["confidence"]),
                metadata={
                    "source_class": source_class,
                    "legacy_dimension": str(
                        row["dimension_name"] or ""
                    ),
                    "legacy_feature": str(
                        row["feature_name"] or ""
                    ),
                    "freshness_days": row["freshness_days"],
                    "legacy_created_at": str(
                        row["created_at"] or ""
                    ),
                },
            )
        )
    return records


def _merge_evidence_records(
    primary: list[EvidenceRecord],
    additional: list[EvidenceRecord],
) -> list[EvidenceRecord]:
    by_identity: dict[str, EvidenceRecord] = {}
    for record in [*primary, *additional]:
        source_class = str(
            record.metadata.get("source_class") or "other"
        )
        evidence_id = canonical_evidence_digest(
            source_class=source_class,
            evidence_type=record.evidence_type,
            url=record.url,
            content=record.content,
        )
        by_identity.setdefault(evidence_id, record)
    return list(by_identity.values())


def _components_for_scan(
    conn: sqlite3.Connection,
    *,
    scan_id: int,
) -> dict[str, dict[str, Any]]:
    components: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        """
        SELECT *
        FROM sv9_component_scores
        WHERE scan_id = ?
        ORDER BY id
        """,
        (scan_id,),
    ).fetchall()
    for row in rows:
        key = str(row["component"] or "")
        components[key] = {
            "component": key,
            "status": str(row["status"] or ""),
            "score": int(row["score"] or 0),
            "scale": int(row["scale"] or 0),
            "points": int(row["points"] or 0),
            "tile_profile": _json_list(row["tile_profile_json"]),
            "detected_content": str(
                row["detected_content"] or ""
            ),
            "detection_mode": str(
                row["detection_mode"] or ""
            ),
            "detection_confidence": str(
                row["detection_confidence"] or ""
            ),
            "evidence": _json_list(row["evidence_json"]),
            "message": str(row["message"] or ""),
            "error": (
                str(row["error"])
                if row["error"] is not None
                else None
            ),
            "veredicto": str(row["veredicto"] or ""),
            "evaluation_model": str(
                row["evaluation_model"] or ""
            ),
        }
    return components


def _latest_report_per_capture(
    reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for report in reports:
        source_run_id = (
            report.get("raw") or {}
        ).get("source_run_id")
        key = (
            f"source_run:{source_run_id}"
            if source_run_id is not None
            else f"report:{report.get('id')}"
        )
        current = latest.get(key)
        if current is None or _report_order(report) > _report_order(
            current
        ):
            latest[key] = report
    return sorted(latest.values(), key=_report_order)


def _latest_report_for_preview(
    reports: Iterable[dict[str, Any]],
    preview: dict[str, Any],
) -> dict[str, Any]:
    latest_report_id = str(preview.get("latest_report_id") or "")
    for report in reports:
        if str(report.get("id") or "") == latest_report_id:
            return report
    raise Brand3SQLiteBackfillError(
        "memory preview latest report is absent from its source lane"
    )


def _preview_summary(preview: dict[str, Any]) -> dict[str, Any]:
    summary = preview.get("summary") or {}
    scoring = preview.get("scoring") or {}
    reviewed_shadow = (
        preview.get("reviewed_shadow")
        if isinstance(preview.get("reviewed_shadow"), dict)
        else {}
    )
    reviewed_scoring = (
        reviewed_shadow.get("scoring")
        if isinstance(reviewed_shadow.get("scoring"), dict)
        else {}
    )
    recoveries = [
        dict(row)
        for row in preview.get("recoveries") or []
        if isinstance(row, dict)
    ]
    recovered_ids = {
        str(evidence_id)
        for recovery in recoveries
        for evidence_id in recovery.get("tile_evidence_ids") or []
        if str(evidence_id)
    }
    return {
        "report_count": int(preview.get("report_count") or 0),
        "accepted_evidence_count": int(
            summary.get("accepted_evidence_count") or 0
        ),
        "accepted_evidence_occurrence_count": int(
            summary.get("accepted_evidence_occurrence_count") or 0
        ),
        "recovered_blind_spot_count": int(
            summary.get("recovered_blind_spot_count") or 0
        ),
        "explicit_negative_conflict_count": int(
            summary.get("explicit_negative_conflict_count") or 0
        ),
        "current_score": scoring.get("current_score"),
        "preview_score": scoring.get("preview_score"),
        "score_delta": scoring.get("score_delta"),
        "scoring_status": str(scoring.get("status") or ""),
        "reviewed_preview_score": reviewed_scoring.get(
            "preview_score"
        ),
        "reviewed_score_delta": reviewed_scoring.get("score_delta"),
        "reviewed_scoring_status": str(
            reviewed_scoring.get("status") or ""
        ),
        "reviewed_accepted_recovery_count": int(
            reviewed_shadow.get("accepted_recovery_count") or 0
        ),
        "memory_version": preview.get("memory_version"),
        "state_fingerprint": preview.get("state_fingerprint"),
        "recoveries": recoveries,
        "recovery_evidence": [
            dict(row)
            for row in preview.get("accepted_evidence") or []
            if isinstance(row, dict)
            and str(row.get("tile_evidence_id") or "")
            in recovered_ids
        ],
        "conflicts": [
            dict(row)
            for row in preview.get("conflicts") or []
            if isinstance(row, dict)
        ],
    }


def _group_by_domain(
    reports: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        domain = _domain(str(report.get("url") or ""))
        if domain:
            grouped[domain].append(report)
    return {
        domain: sorted(rows, key=_report_order)
        for domain, rows in grouped.items()
    }


def _domain(value: str) -> str:
    parsed = urlparse(
        value if "://" in value else f"https://{value}"
    )
    return (
        str(parsed.hostname or "")
        .strip(".")
        .lower()
        .removeprefix("www.")
    )


def _report_order(report: dict[str, Any]) -> tuple[datetime, str]:
    text = str(report.get("created_at") or "")
    try:
        observed_at = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except ValueError:
        observed_at = datetime.min.replace(tzinfo=timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return (
        observed_at.astimezone(timezone.utc),
        str(report.get("id") or ""),
    )


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _json_list(value: Any) -> list[Any]:
    parsed = _json_value(value)
    return parsed if isinstance(parsed, list) else []


def _confidence(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.0
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"
