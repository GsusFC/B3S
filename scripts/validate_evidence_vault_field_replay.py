#!/usr/bin/env python3
"""Run the offline normalized real-brand Vault planning replay gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.services.evidence_vault_field_replay import (
    validate_evidence_vault_field_replay,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(
            "fixtures/evidence_vault_field_validation_v1/manifest.json"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_evidence_vault_field_replay(args.manifest)
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
