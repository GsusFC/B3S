#!/usr/bin/env python3
"""Preview additive evidence memory over saved B3S scanner reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.history.report_parser import normalize_domain
from src.services.evidence_scoring_memory_preview import (
    build_evidence_scoring_memory_preview,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a non-authoritative scoring-memory preview without "
            "mutating reports or production scores."
        )
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("data/reports"),
    )
    parser.add_argument(
        "--domain",
        help="Optional canonical domain filter.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
    )
    args = parser.parse_args()

    reports = _load_reports(args.reports_dir)
    requested_domain = normalize_domain(args.domain or "")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for report in reports:
        domain = normalize_domain(str(report.get("url") or ""))
        if not domain or (
            requested_domain and domain != requested_domain
        ):
            continue
        grouped.setdefault(domain, []).append(report)

    previews = [
        build_evidence_scoring_memory_preview(rows, mode="shadow")
        for _domain, rows in sorted(grouped.items())
    ]
    payload = {
        "schema_version": "evidence-scoring-memory-preview-batch-v1",
        "runtime_effect": False,
        "authority": False,
        "domain_count": len(previews),
        "report_count": sum(
            int(preview.get("report_count") or 0)
            for preview in previews
        ),
        "accepted_evidence_count": sum(
            int(
                (preview.get("summary") or {}).get(
                    "accepted_evidence_count"
                )
                or 0
            )
            for preview in previews
        ),
        "recovered_blind_spot_count": sum(
            int(
                (preview.get("summary") or {}).get(
                    "recovered_blind_spot_count"
                )
                or 0
            )
            for preview in previews
        ),
        "previews": previews,
    }
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
    lines = [
        "# Evidence scoring memory preview",
        "",
        f"- Domains: `{payload['domain_count']}`",
        f"- Reports: `{payload['report_count']}`",
        (
            "- Accepted reproducible evidence: "
            f"`{payload['accepted_evidence_count']}`"
        ),
        (
            "- Recovered blind spots: "
            f"`{payload['recovered_blind_spot_count']}`"
        ),
        "- Runtime effect: `false`",
        "",
        "| Domain | Reports | Evidence | Recoveries | Current | Preview | Delta |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for preview in payload.get("previews") or []:
        brand = preview.get("brand") or {}
        summary = preview.get("summary") or {}
        scoring = preview.get("scoring") or {}
        lines.append(
            "| {domain} | {reports} | {evidence} | {recoveries} | "
            "{current} | {preview_score} | {delta} |".format(
                domain=brand.get("domain") or "",
                reports=preview.get("report_count") or 0,
                evidence=summary.get("accepted_evidence_count") or 0,
                recoveries=summary.get("recovered_blind_spot_count")
                or 0,
                current=_display(scoring.get("current_score")),
                preview_score=_display(scoring.get("preview_score")),
                delta=_display(scoring.get("score_delta")),
            )
        )
    return "\n".join(lines) + "\n"


def _display(value: Any) -> str:
    return "—" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
