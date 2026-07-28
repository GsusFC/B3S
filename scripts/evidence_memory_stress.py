#!/usr/bin/env python3
"""Run the read-only adversarial evidence-memory validation harness."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from src.services.evidence_memory_stress import (
    render_evidence_memory_stress_markdown,
    run_evidence_memory_stress,
)
from web.report_store import domain_key, list_reports, list_reports_for_domain


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stress evidence memory against acquisition loss, evaluator drift, repetition, "
            "poisoning, syndication, and unresolved real change without mutating state."
        )
    )
    parser.add_argument("--reports-dir", help="Optional JSON reports directory override.")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", help="Optional output path; stdout when omitted.")
    parser.add_argument(
        "--require-promotion-ready",
        action="store_true",
        help="Also fail while architectural promotion blockers remain.",
    )
    args = parser.parse_args()

    if args.reports_dir:
        os.environ["B3S_REPORTS_DIR"] = str(Path(args.reports_dir).expanduser().resolve())

    report = run_evidence_memory_stress(_load_histories())
    rendered = (
        render_evidence_memory_stress_markdown(report)
        if args.format == "markdown"
        else json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    )
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered.rstrip() + "\n", encoding="utf-8")
    else:
        print(rendered.rstrip())

    if report["executable_failures"]:
        return 1
    if args.require_promotion_ready and not report["promotion_ready"]:
        return 2
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


if __name__ == "__main__":
    raise SystemExit(main())
