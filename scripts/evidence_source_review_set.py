#!/usr/bin/env python3
"""Evaluate the two-axis human source-review dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from src.services.evidence_source_review_set import (
    DEFAULT_REVIEW_ROOT,
    build_review_template,
    evaluate_source_review_set,
    load_review_candidates,
    load_review_events,
    load_review_manifest,
    render_source_review_set_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate publisher independence separately from claim "
            "corroboration without runtime authority."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_REVIEW_ROOT / "manifest.json",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=DEFAULT_REVIEW_ROOT / "candidates.jsonl",
    )
    parser.add_argument(
        "--review-events",
        type=Path,
        default=DEFAULT_REVIEW_ROOT / "review_events.jsonl",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write-review-template", type=Path)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()

    manifest = load_review_manifest(args.manifest)
    candidates = load_review_candidates(args.candidates)
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
        rows = build_review_template(candidates)
        args.write_review_template.write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True)
                for row in rows
            )
            + "\n",
            encoding="utf-8",
        )

    result = evaluate_source_review_set(
        candidates,
        load_review_events(args.review_events),
        manifest=manifest,
    )
    rendered = (
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
        if args.format == "json"
        else render_source_review_set_markdown(result)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 2 if args.require_ready and not result["promotion_ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
