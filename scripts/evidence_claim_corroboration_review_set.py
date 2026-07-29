#!/usr/bin/env python3
"""Evaluate or prepare the claim-scoped corroboration review set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from src.services.evidence_claim_corroboration_review_set import (
    DEFAULT_REVIEW_ROOT,
    build_review_template,
    evaluate_claim_corroboration_review_set,
    load_review_candidates,
    load_review_events,
    load_review_manifest,
    render_claim_corroboration_review_set_markdown,
    verify_frozen_candidate_provenance,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate literal claim-scoped source reviews without runtime "
            "or scoring authority."
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
        "--reviews",
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
    parser.add_argument("--verify-provenance", action="store_true")
    parser.add_argument("--require-review-ready", action="store_true")
    parser.add_argument("--require-promotion-ready", action="store_true")
    args = parser.parse_args()

    manifest = load_review_manifest(args.manifest)
    candidates = load_review_candidates(args.candidates)
    if args.verify_provenance and not verify_frozen_candidate_provenance(
        candidates
    ):
        parser.error(
            "frozen candidates do not reproduce from the capture"
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
        rows = build_review_template(
            candidates,
            manifest=manifest,
        )
        args.write_review_template.write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True)
                for row in rows
            )
            + "\n",
            encoding="utf-8",
        )

    result = evaluate_claim_corroboration_review_set(
        candidates,
        load_review_events(args.reviews),
        manifest=manifest,
    )
    rendered = (
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
        if args.format == "json"
        else render_claim_corroboration_review_set_markdown(result)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    if args.require_promotion_ready and not result["promotion_ready"]:
        return 2
    if args.require_review_ready and not result["review_gate_ready"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
