#!/usr/bin/env python3
"""Render the non-authoritative two-axis source/claim registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from src.services.evidence_source_claim_registry import (
    build_evidence_source_claim_registry,
    render_evidence_source_claim_registry_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Project publisher independence and claim corroboration on "
            "separate shadow-only axes."
        )
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-shadow-contract", action="store_true")
    parser.add_argument("--require-operational-adoption", action="store_true")
    args = parser.parse_args()

    registry = build_evidence_source_claim_registry()
    rendered = (
        json.dumps(
            registry,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
        if args.format == "json"
        else render_evidence_source_claim_registry_markdown(registry)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    if (
        args.require_operational_adoption
        and not registry["operational_adoption_ready"]
    ):
        return 2
    if (
        args.require_shadow_contract
        and not registry["shadow_contract_ready"]
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
