#!/usr/bin/env python3
"""Read-only global preview of B3S canonical evidence enforcement."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from src.services.scanner_evidence_comparison import render_history_dry_run
from web.report_store import domain_key, list_reports, list_reports_for_domain


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify every stored brand history without writing or changing canonical state."
    )
    parser.add_argument("--reports-dir", help="Optional JSON reports directory override.")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", help="Optional output path; stdout when omitted.")
    args = parser.parse_args()

    if args.reports_dir:
        os.environ["B3S_REPORTS_DIR"] = str(Path(args.reports_dir).expanduser().resolve())

    histories = _load_histories()
    result = render_history_dry_run(histories)
    rendered = (
        json.dumps(result, ensure_ascii=False, indent=2)
        if args.format == "json"
        else render_markdown(result)
    )
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


def _load_histories() -> dict[str, list[dict[str, Any]]]:
    domains = {
        domain_key(str(row.get("url") or ""))
        for row in list_reports()
        if isinstance(row, dict)
    }
    return {
        domain: list_reports_for_domain(domain)
        for domain in sorted(domains)
        if domain
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Canonical evidence dry-run",
        "",
        f"- Mutates state: `{str(result.get('mutates_state')).lower()}`",
        f"- Brands: {result.get('brand_count', 0)}",
        f"- Repeated histories: {result.get('repeated_brand_count', 0)}",
        f"- Single-scan histories: {result.get('single_scan_brand_count', 0)}",
        f"- Visible reports changed by repeated mode: {result.get('repeated_mode_changed_brand_count', 0)}",
        f"- Visible reports changed by all mode: {result.get('all_mode_changed_brand_count', 0)}",
        "",
        "## Classification counts",
        "",
    ]
    for key, count in (result.get("classification_counts") or {}).items():
        lines.append(f"- `{key}`: {count}")
    lines.extend(["", "## Brands", ""])
    for brand in result.get("brands") or []:
        lines.append(
            f"- `{brand.get('domain')}`: reports={brand.get('report_count', 0)}, "
            f"latest={brand.get('latest_report_id') or 'none'}, "
            f"canonical={brand.get('canonical_report_id') or 'none'}, "
            f"provisional={brand.get('provisional_report_id') or 'none'}, "
            f"selected_score={brand.get('selected_score')}, "
            f"latest_score={brand.get('latest_score')}, "
            f"score_delta={brand.get('score_delta_latest_minus_selected')}, "
            f"changes_visible={str(bool(brand.get('selection_changes_visible_report'))).lower()}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
