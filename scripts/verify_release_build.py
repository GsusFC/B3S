"""Fail a production release whose image has no immutable commit identity."""

from __future__ import annotations

from src.build_info import require_deployable_build_sha


if __name__ == "__main__":
    build_sha = require_deployable_build_sha()
    print(f"Verified production build commit: {build_sha}")
