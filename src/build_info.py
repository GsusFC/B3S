"""Build provenance exposed by production artifacts and persisted reports."""

from __future__ import annotations

import os
import re


UNKNOWN_BUILD_SHA = "unknown"
_FULL_GIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def current_build_sha() -> str:
    """Return the immutable build commit SHA, or ``unknown`` outside a release."""

    value = os.environ.get("B3S_BUILD_SHA", "").strip()
    if not _FULL_GIT_SHA.fullmatch(value):
        return UNKNOWN_BUILD_SHA
    return value.lower()


def require_deployable_build_sha() -> str:
    """Reject release images that were not built from an explicit commit."""

    build_sha = current_build_sha()
    if build_sha == UNKNOWN_BUILD_SHA:
        raise RuntimeError(
            "B3S_BUILD_SHA must be a full 40-character Git commit SHA. "
            "Build production images with --build-arg B3S_BUILD_SHA=<commit>."
        )
    return build_sha
