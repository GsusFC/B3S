#!/usr/bin/env python3
"""Validate Brand3 SQLite history against B3S additive evidence memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.services.brand3_sqlite_memory_backfill import (
    build_brand3_sqlite_memory_validation,
)
from src.services.evidence_scoring_recovery_review import (
    build_recovery_review_template,
    load_recovery_review_events,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read a Brand3 SQLite archive without mutation and compare "
            "evaluator repeats, captures, and current B3S reports."
        )
    )
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--current-reports-dir",
        type=Path,
        help="Optional directory containing current B3S report JSON.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
    )
    parser.add_argument(
        "--reviews",
        type=Path,
        help="Optional append-only semantic recovery review events JSONL.",
    )
    parser.add_argument(
        "--write-review-template",
        type=Path,
        help="Write an unsigned review-event template and keep evaluating.",
    )
    args = parser.parse_args()
    current_reports = (
        _load_reports(args.current_reports_dir)
        if args.current_reports_dir
        else []
    )
    payload = build_brand3_sqlite_memory_validation(
        args.database,
        current_reports=current_reports,
        recovery_review_events=load_recovery_review_events(
            args.reviews
        ),
    )
    if args.write_review_template:
        if args.write_review_template.exists():
            parser.error(
                f"review template already exists: "
                f"{args.write_review_template}"
            )
        args.write_review_template.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        rows = build_recovery_review_template(
            payload["recovery_review_candidates"]
        )
        args.write_review_template.write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True)
                for row in rows
            )
            + "\n",
            encoding="utf-8",
        )
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_markdown(payload))
    return 0


def _load_reports(directory: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            reports.append(payload)
    return reports


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Brand3 SQLite memory backfill",
        "",
        f"- Archive scans: `{summary.get('archive_scan_count', 0)}`",
        f"- Archive captures: `{summary.get('archive_capture_count', 0)}`",
        f"- Domains: `{summary.get('archive_domain_count', 0)}`",
        f"- Evaluator repeats: `{summary.get('evaluator_repeat_count', 0)}`",
        (
            "- Accepted reproducible evidence: "
            f"`{summary.get('archive_accepted_evidence_count', 0)}`"
        ),
        (
            "- Evaluator-repeat recoveries: "
            f"`{summary.get('evaluator_repeat_recovery_count', 0)}`"
        ),
        (
            "- Cross-capture recoveries: "
            f"`{summary.get('capture_recovery_count', 0)}`"
        ),
        (
            "- Bridge recoveries: "
            f"`{summary.get('bridge_recovery_count', 0)}`"
        ),
        (
            "- Recovery review candidates: "
            f"`{summary.get('recovery_review_candidate_count', 0)}`"
        ),
        (
            "- Accepted recovery mappings: "
            f"`{summary.get('recovery_review_accepted_count', 0)}`"
        ),
        "- Runtime effect: `false`",
        "- Archive mutation: `false`",
        f"- Promotion ready: `{str(bool(payload.get('promotion_ready'))).lower()}`",
        "",
        (
            "| Domain | Scans | Captures | Repeats | Evidence | "
            "Candidate all Δ | Reviewed all Δ | Candidate bridge Δ | "
            "Reviewed bridge Δ |"
        ),
        (
            "| --- | ---: | ---: | ---: | ---: | ---: | "
            "---: | ---: | ---: |"
        ),
    ]
    for row in payload.get("domains") or []:
        all_scan = row.get("all_scan_preview") or {}
        bridge = row.get("bridge_preview") or {}
        lines.append(
            "| {domain} | {scans} | {captures} | {repeats} | "
            "{evidence} | {repeat_delta} | {reviewed_repeat_delta} | "
            "{bridge_delta} | {reviewed_bridge_delta} |".format(
                domain=row.get("domain") or "",
                scans=row.get("archive_scan_count") or 0,
                captures=row.get("archive_capture_count") or 0,
                repeats=row.get("evaluator_repeat_count") or 0,
                evidence=all_scan.get(
                    "accepted_evidence_count"
                )
                or 0,
                repeat_delta=_display(
                    all_scan.get("score_delta")
                ),
                reviewed_repeat_delta=_display(
                    all_scan.get("reviewed_score_delta")
                ),
                bridge_delta=_display(
                    bridge.get("score_delta")
                ),
                reviewed_bridge_delta=_display(
                    bridge.get("reviewed_score_delta")
                ),
            )
        )
    recovery_review = payload.get("recovery_review") or {}
    review_summary = recovery_review.get("summary") or {}
    lines.extend(
        [
            "",
            "## Semantic recovery review",
            "",
            f"- Reviewed: `{review_summary.get('reviewed_count', 0)}`",
            f"- Pending: `{review_summary.get('pending_count', 0)}`",
            f"- Accepted: `{review_summary.get('accepted_count', 0)}`",
            f"- Disputed: `{review_summary.get('disputed_count', 0)}`",
            f"- Rejected: `{review_summary.get('rejected_count', 0)}`",
            f"- Revoked: `{review_summary.get('revoked_count', 0)}`",
        ]
    )
    blockers = payload.get("promotion_blockers") or []
    if blockers:
        lines.extend(
            [
                "",
                "## Promotion blockers",
                "",
                *[f"- `{blocker}`" for blocker in blockers],
            ]
        )
    candidates = payload.get("recovery_review_candidates") or []
    if candidates:
        decisions = {
            str(row.get("case_id") or ""): str(
                row.get("decision") or "pending"
            )
            for row in recovery_review.get("evaluated") or []
            if isinstance(row, dict)
        }
        lines.extend(
            [
                "",
                "## Recovery review candidates",
                "",
                "| Case | Domain | Lanes | Tile | Decision |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for candidate in candidates:
            lines.append(
                "| {case_id} | {domain} | {lanes} | "
                "{component}.{tile} | "
                "{decision} |".format(
                    case_id=candidate.get("case_id") or "",
                    domain=(candidate.get("brand") or {}).get(
                        "domain"
                    )
                    or "",
                    lanes=", ".join(
                        str(context.get("lane") or "")
                        for context in candidate.get("contexts") or []
                        if isinstance(context, dict)
                    ),
                    component=(candidate.get("tile") or {}).get(
                        "component_key"
                    )
                    or "",
                    tile=(candidate.get("tile") or {}).get(
                        "tile_id"
                    )
                    or "",
                    decision=decisions.get(
                        str(candidate.get("case_id") or ""),
                        "pending",
                    ),
                )
            )
        for candidate in candidates:
            tile = (
                candidate.get("tile")
                if isinstance(candidate.get("tile"), dict)
                else {}
            )
            evidence = (
                candidate.get("evidence")
                if isinstance(candidate.get("evidence"), dict)
                else {}
            )
            contract = (
                tile.get("evidence_contract")
                if isinstance(tile.get("evidence_contract"), dict)
                else {}
            )
            lines.extend(
                [
                    "",
                    f"### {candidate.get('case_id') or ''}",
                    "",
                    f"- Condition: {tile.get('condition') or ''}",
                    f"- Contract `ok`: {contract.get('ok') or ''}",
                    (
                        "- Contract `reject`: "
                        f"{contract.get('reject') or ''}"
                    ),
                    "- Evidence:",
                    "",
                    f"> {evidence.get('quote') or ''}",
                    "",
                    (
                        "- Sources: "
                        + ", ".join(
                            str(value)
                            for value in evidence.get("source_urls")
                            or []
                            if str(value)
                        )
                    ),
                ]
            )
    return "\n".join(lines) + "\n"


def _display(value: Any) -> str:
    return "—" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
