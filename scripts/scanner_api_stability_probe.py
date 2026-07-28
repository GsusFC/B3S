#!/usr/bin/env python3
"""Run repeated deploy scanner probes and diff persisted API outputs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src.services.scanner_api_stability import (
    compare_probe_summaries,
    configured_scanner_api_url,
    create_scan,
    extract_probe_summary,
    fetch_scan_bundle,
    poll_scan_ready,
    read_env_value,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--base-url",
        default=configured_scanner_api_url(),
        help=(
            "Scanner API v1 base. Accepts https://host/api/v1 or a legacy "
            "https://host origin; defaults to B3S_SCANNER_API_URL."
        ),
    )
    parser.add_argument("--lang", default="es", choices=("es", "en"))
    parser.add_argument("--scanner-token", default=read_env_value("BRAND3_SCANNER_API_TOKEN"))
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-wait-seconds", type=int, default=900)
    parser.add_argument("--request-timeout", type=int, default=60)
    parser.add_argument("--output", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.scanner_token:
        raise SystemExit("Missing BRAND3_SCANNER_API_TOKEN.")

    samples: list[dict] = []
    for index in range(args.repeats):
        created = create_scan(
            base_url=args.base_url,
            url=args.url,
            lang=args.lang,
            token=args.scanner_token,
            timeout=args.request_timeout,
        )
        scan_id = int(created["id"])
        status = poll_scan_ready(
            base_url=args.base_url,
            scan_id=scan_id,
            lang=args.lang,
            token=args.scanner_token,
            poll_interval=args.poll_interval,
            max_wait_seconds=args.max_wait_seconds,
            timeout=args.request_timeout,
        )
        if status.get("status") != "ready" or not status.get("result_available"):
            samples.append(
                {
                    "iteration": index + 1,
                    "scan_id": scan_id,
                    "status": status.get("status"),
                    "phase": status.get("phase"),
                    "error_message": status.get("error_message"),
                }
            )
            continue
        bundle = fetch_scan_bundle(
            base_url=args.base_url,
            scan_id=scan_id,
            token=args.scanner_token,
            timeout=args.request_timeout,
        )
        summary = extract_probe_summary(bundle)
        samples.append({"iteration": index + 1, **summary})
        print(
            json.dumps(
                {
                    "iteration": index + 1,
                    "scan_id": summary["scan_id"],
                    "source_run_id": summary["source_run_id"],
                    "magnetism_score": summary["magnetism_score"],
                    "coherence_score": summary["coherence_score"],
                    "earned_magnetism_score": summary["earned_magnetism_score"],
                    "evidence_duty_status": summary["evidence_duty_status"],
                },
                ensure_ascii=False,
            )
        )

    comparable = [sample for sample in samples if sample.get("source_run_id")]
    comparison = compare_probe_summaries(comparable)
    payload = {
        "schema_version": "scanner-api-stability-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "url": args.url,
        "repeats": args.repeats,
        "samples": samples,
        "comparison": comparison,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if comparison.get("critical_stable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
