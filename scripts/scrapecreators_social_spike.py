#!/usr/bin/env python3
"""Run the isolated ScrapeCreators social-acquisition spike."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.research.scrapecreators_spike import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_TIMEOUT_SECONDS,
    ManifestError,
    ScrapeCreatorsClient,
    ScrapeCreatorsSpikeError,
    atomic_write_json as _atomic_write_json,
    build_dry_run,
    load_target_manifest,
    run_acquisition,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPOSITORY_ROOT / "out" / "scrapecreators-social-spike"


def _validate_output_path(path: str | Path) -> Path:
    """Return a destination confined to the ignored spike output root."""

    candidate = Path(path)
    if any(part == ".." for part in candidate.parts):
        raise ValueError("output path traversal is not allowed")
    absolute_candidate = candidate if candidate.is_absolute() else REPOSITORY_ROOT / candidate
    expected_root = REPOSITORY_ROOT / "out" / "scrapecreators-social-spike"
    if OUTPUT_ROOT.absolute() == expected_root.absolute():
        cursor = REPOSITORY_ROOT
        for part in expected_root.relative_to(REPOSITORY_ROOT).parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("output root must not escape through a symlink")
    try:
        resolved_root = OUTPUT_ROOT.resolve(strict=False)
        resolved_candidate = absolute_candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("output path cannot be resolved safely") from exc
    try:
        relative = resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("output must remain beneath out/scrapecreators-social-spike") from exc
    if not relative.parts:
        raise ValueError("output must name a file beneath out/scrapecreators-social-spike")
    return absolute_candidate


def atomic_write_json(path: str | Path, payload: dict) -> Path:
    """Write one private spike artifact after validating its lab-only path."""

    return _atomic_write_json(_validate_output_path(path), payload)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        # Validate before reading the manifest, credentials, or provider.
        output_path = _validate_output_path(args.output)
        if args.dry_run and args.include_raw:
            print("error: --include-raw is only valid for a live run", file=sys.stderr)
            return 2
        manifest = load_target_manifest(args.manifest)
        if args.dry_run:
            payload = build_dry_run(manifest, max_post_pages=args.max_post_pages)
        else:
            with ScrapeCreatorsClient(
                timeout_seconds=args.timeout_seconds,
                max_attempts=args.max_attempts,
            ) as client:
                payload = run_acquisition(
                    manifest,
                    client,
                    include_raw=args.include_raw,
                    include_interactions=args.include_interactions,
                    include_replies=args.include_replies,
                    max_post_pages=args.max_post_pages,
                    max_interaction_pages=args.max_interaction_pages,
                    max_interaction_credits=args.max_interaction_credits,
                )
        atomic_write_json(output_path, payload)
    except (ManifestError, ScrapeCreatorsSpikeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("error: spike output could not be written", file=sys.stderr)
        return 2

    summary = payload["summary"]
    print(
        "Wrote secure spike output "
        f"(mode={payload['mode']}, targets={summary['target_count']}, "
        f"requests={summary['request_count']}, profiles={summary['profile_count']}, "
        f"posts={summary['post_count']}"
        + (f", interactions={summary['interaction_count']}" if "interaction_count" in summary else "")
        + ")"
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="JSON manifest containing exact social targets")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("out/scrapecreators-social-spike/result.json"),
        help="Destination JSON (written atomically with mode 0600)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan requests without reading a key or using the network")
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Include recursively redacted provider payloads in the mode-0600 output",
    )
    parser.add_argument(
        "--include-interactions",
        action="store_true",
        help="Acquire supported LinkedIn/Instagram comment endpoints and emit capability statuses",
    )
    parser.add_argument(
        "--include-replies",
        action="store_true",
        help="Opt in to bounded Instagram nested-reply acquisition (requires a credit budget)",
    )
    parser.add_argument(
        "--max-interaction-pages",
        type=int,
        default=1,
        help="Maximum pages/cursors per interaction endpoint (1..10)",
    )
    parser.add_argument(
        "--max-post-pages",
        type=int,
        default=1,
        help="Maximum LinkedIn company-post pages to acquire (1..10)",
    )
    parser.add_argument(
        "--max-interaction-credits",
        type=int,
        default=None,
        help="Credit ceiling for interaction/reply requests; required with --include-replies",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-request timeout, >0 and <= {MAX_TIMEOUT_SECONDS:g}",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"Total attempts per request, 1..{MAX_ATTEMPTS}",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
