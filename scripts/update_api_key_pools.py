#!/usr/bin/env python3
"""Append Exa/Firecrawl keys to a git-ignored env file via stdin."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from src.api_key_pool import normalize_api_keys


POOL_TO_PRIMARY = {
    "FIRECRAWL_API_KEYS": "FIRECRAWL_API_KEY",
    "EXA_API_KEYS": "EXA_API_KEY",
}


def parse_assignments(raw: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or name not in POOL_TO_PRIMARY or not value.strip():
            raise ValueError("stdin must contain non-empty EXA_API_KEYS/FIRECRAWL_API_KEYS assignments")
        assignments[name] = value.strip()
    if not assignments:
        raise ValueError("no API-key pool assignments received")
    return assignments


def update_env_file(path: Path, additions: dict[str, str]) -> dict[str, int]:
    lines = path.read_text().splitlines() if path.exists() else []
    current: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name, value = stripped.split("=", 1)
            current[name.strip()] = value.strip()

    replacements: dict[str, str] = {}
    counts: dict[str, int] = {}
    for pool_name, new_value in additions.items():
        primary_keys = normalize_api_keys(current.get(POOL_TO_PRIMARY[pool_name], ""))
        all_keys = normalize_api_keys(primary_keys, current.get(pool_name, ""), new_value)
        additional_keys = tuple(key for key in all_keys if key not in primary_keys)
        replacements[pool_name] = ",".join(additional_keys)
        counts[pool_name] = len(all_keys)

    updated: list[str] = []
    replaced: set[str] = set()
    for line in lines:
        name = line.split("=", 1)[0].strip() if "=" in line else ""
        if name in replacements:
            updated.append(f"{name}={replacements[name]}")
            replaced.add(name)
        else:
            updated.append(line)
    for name, value in replacements.items():
        if name not in replaced:
            updated.append(f"{name}={value}")

    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.write_text("\n".join(updated) + "\n")
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        additions = parse_assignments(sys.stdin.read())
        counts = update_env_file(args.env_file, additions)
    except (OSError, ValueError) as exc:
        print(f"API key pool update failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"configured_key_counts": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
