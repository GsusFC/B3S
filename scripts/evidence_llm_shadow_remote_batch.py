#!/usr/bin/env python3
"""Run authenticated production evidence LLM shadow diagnostics in batch.

This script does not mutate production data. It calls read-only diagnostic
endpoints, stores local artifacts, and groups LLM-vs-heuristic disagreements
into review buckets so repeated patterns can be promoted into deterministic
Python rules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from src.services.scanner_api_stability import (
    DEFAULT_DEPLOY_ORIGIN,
    configured_scanner_api_url,
    scanner_api_origin,
)


DEFAULT_BASE_URL = DEFAULT_DEPLOY_ORIGIN


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    base_url = str(args.base_url).rstrip("/")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    token = _scanner_token()

    run_ids = list(args.run_ids)
    if args.latest_from_index:
        run_ids.extend(_latest_run_ids_from_index(base_url, limit=args.latest_from_index, timeout=args.timeout))
    run_ids = _unique_ints(run_ids)
    if not run_ids:
        raise SystemExit("Provide run IDs or --latest-from-index.")

    rows: list[dict[str, Any]] = []
    for run_id in run_ids:
        started = time.monotonic()
        vnext = _fetch_json(
            f"{base_url}/magnetism-scanner/run/{run_id}/evidence-vnext",
            timeout=args.timeout,
        )
        vnext_path = output_dir / f"vnext_{run_id}.json"
        _write_json(vnext_path, vnext)
        vnext_row = _vnext_row(vnext)
        totals = ((vnext.get("report") or {}).get("totals") or {}) if isinstance(vnext, dict) else {}
        accepted = int(totals.get("accepted") or 0)
        if args.only_with_accepted and accepted <= 0:
            rows.append(
                {
                    "run_id": run_id,
                    "brand_name": vnext_row.get("brand_name") or "",
                    "status": "skipped",
                    "reason": "no_accepted_evidence",
                    "accepted": accepted,
                    "elapsed_seconds": _elapsed(started),
                }
            )
            print(f"run={run_id} skipped accepted=0")
            continue

        shadow_url = f"{base_url}/api/v1/scanner/run/{run_id}/evidence-vnext/llm-shadow"
        if args.no_cache:
            shadow_url += "?no_cache=true"
        shadow = _fetch_json(shadow_url, token=token, timeout=args.timeout)
        shadow_path = output_dir / f"llm_shadow_{run_id}.json"
        _write_json(shadow_path, shadow)
        row = _run_row(run_id, vnext=vnext, shadow=shadow)
        row["elapsed_seconds"] = _elapsed(started)
        rows.append(row)
        print(
            "run={run_id} brand={brand_name} status={llm_status} batches={batches} "
            "attempts={attempts} retries={retries} class_delta={class_delta} "
            "materiality_delta={materiality_delta} elapsed={elapsed_seconds}s".format(**row)
        )

    payload = {
        "version": "evidence_llm_shadow_remote_batch_v0_1",
        "runtime_effect": False,
        "prompt_effect": False,
        "persistence_effect": False,
        "base_url": base_url,
        "run_ids": run_ids,
        "rows": rows,
        "summary": _summary(rows, output_dir=output_dir),
    }
    _write_json(output_dir / "batch_summary.json", payload)
    (output_dir / "batch_summary.md").write_text(_markdown(payload), encoding="utf-8")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_ids", nargs="*", type=int, help="Brand Audit run IDs to inspect.")
    parser.add_argument(
        "--base-url",
        default=scanner_api_origin(configured_scanner_api_url()),
        help="B3S web origin; defaults to the origin derived from B3S_SCANNER_API_URL.",
    )
    parser.add_argument(
        "--latest-from-index",
        type=int,
        default=0,
        help="Append the latest run IDs linked from /magnetism-scanner.",
    )
    parser.add_argument(
        "--only-with-accepted",
        action="store_true",
        help="Skip LLM calls for runs whose vNext report has accepted=0.",
    )
    parser.add_argument("--no-cache", action="store_true", help="Bypass provider cache for live timing.")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("out/evidence_vnext/remote_llm_shadow_batch"),
        help="Directory for JSON and Markdown artifacts.",
    )
    return parser.parse_args(argv)


def _scanner_token() -> str:
    token = os.environ.get("BRAND3_SCANNER_API_TOKEN", "").strip()
    if token:
        return token
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "BRAND3_SCANNER_API_TOKEN":
                token = value.strip()
                break
    if not token:
        raise SystemExit("BRAND3_SCANNER_API_TOKEN is required.")
    return token


def _latest_run_ids_from_index(base_url: str, *, limit: int, timeout: int) -> list[int]:
    body = _fetch_text(f"{base_url}/magnetism-scanner?lang=es", timeout=timeout)
    run_ids: list[int] = []
    for match in re.finditer(r"/magnetism-scanner/run/(\d+)/evidence-vnext", body):
        run_id = int(match.group(1))
        if run_id not in run_ids:
            run_ids.append(run_id)
        if len(run_ids) >= limit:
            break
    return run_ids


def _fetch_json(url: str, *, token: str = "", timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc


def _fetch_text(url: str, *, timeout: int) -> str:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc


def _headers(token: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _run_row(run_id: int, *, vnext: dict[str, Any], shadow: dict[str, Any]) -> dict[str, Any]:
    summary = shadow.get("summary") or {}
    disagreements = list(shadow.get("disagreements") or [])
    bucket_counts = Counter(_bucket(item) for item in disagreements)
    return {
        "run_id": run_id,
        "brand_name": shadow.get("brand_name") or _vnext_row(vnext).get("brand_name") or "",
        "url": shadow.get("url") or _vnext_row(vnext).get("url") or "",
        "status": "ok",
        "vnext_projected_status": _vnext_row(vnext).get("projected_promotion_status") or "",
        "vnext_readiness_status": _vnext_row(vnext).get("readiness_status") or "",
        "vnext_human_required": bool(_vnext_row(vnext).get("human_required")),
        "llm_status": summary.get("llm_status") or "",
        "batches": summary.get("llm_batch_count") or 0,
        "attempts": summary.get("llm_attempt_count") or 0,
        "retries": summary.get("llm_retry_count") or 0,
        "class_delta": summary.get("semantic_class_disagreement_count") or 0,
        "materiality_delta": summary.get("materiality_disagreement_count") or 0,
        "bucket_counts": dict(sorted(bucket_counts.items())),
        "examples": _examples(disagreements),
    }


def _vnext_row(vnext: dict[str, Any]) -> dict[str, Any]:
    rows = (((vnext.get("report") or {}).get("readiness_matrix") or {}).get("rows") or [])
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def _bucket(item: dict[str, Any]) -> str:
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    provider = str(context.get("provider") or "")
    feature = str(context.get("feature_name") or "")
    source_class = str(context.get("source_class") or "")
    text = str(context.get("text_preview") or "").lower()
    heuristic_class = str(item.get("heuristic_class") or "")
    llm_class = str(item.get("llm_class") or "")
    heuristic_materiality = str(item.get("heuristic_materiality") or "")
    llm_materiality = str(item.get("llm_materiality") or "")

    if provider == "social_scrape" and feature == "social_footprint" and "profile candidate" in text:
        return "social_placeholder_boundary"
    if provider == "competitor_web_comparison" and source_class == "competitor_comparison":
        if heuristic_class != "competitor_comparison" and llm_class == "competitor_comparison":
            return "absorbable_rule_synthetic_competitor_comparison"
        return "competitor_comparison_materiality_only"
    if heuristic_class == llm_class and heuristic_materiality != llm_materiality:
        return "materiality_granularity_no_class_change"
    if source_class in {"audited_surface", "owned_surface"} and llm_class == "wrong_entity":
        return "possible_model_noise_owned_surface"
    if heuristic_class != llm_class:
        return "semantic_boundary_review"
    return "other_review"


def _examples(disagreements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in disagreements:
        bucket = _bucket(item)
        if bucket in seen:
            continue
        seen.add(bucket)
        context = item.get("context") if isinstance(item.get("context"), dict) else {}
        out.append(
            {
                "bucket": bucket,
                "change": (
                    f"{item.get('heuristic_class')}/{item.get('heuristic_materiality')} -> "
                    f"{item.get('llm_class')}/{item.get('llm_materiality')}"
                ),
                "provider": context.get("provider") or "",
                "source_class": context.get("source_class") or "",
                "url": context.get("url") or "",
                "text_preview": str(context.get("text_preview") or "")[:260],
            }
        )
    return out


def _summary(rows: list[dict[str, Any]], *, output_dir: Path) -> dict[str, Any]:
    bucket_counts: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for row in rows:
        statuses[str(row.get("status") or "unknown")] += 1
        bucket_counts.update(row.get("bucket_counts") or {})
    return {
        "run_count": len(rows),
        "status_counts": dict(sorted(statuses.items())),
        "bucket_counts": dict(sorted(bucket_counts.items())),
        "total_class_delta": sum(int(row.get("class_delta") or 0) for row in rows),
        "total_materiality_delta": sum(int(row.get("materiality_delta") or 0) for row in rows),
        "total_retries": sum(int(row.get("retries") or 0) for row in rows),
        "artifact_dir": str(output_dir),
    }


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Evidence LLM Shadow Remote Batch",
        "",
        f"- Base URL: `{payload.get('base_url')}`",
        f"- Runtime effect: `{str(bool(payload.get('runtime_effect'))).lower()}`",
        f"- Prompt effect: `{str(bool(payload.get('prompt_effect'))).lower()}`",
        f"- Persistence effect: `{str(bool(payload.get('persistence_effect'))).lower()}`",
        "",
        "## Summary",
        "",
    ]
    summary = payload.get("summary") or {}
    for key, value in summary.items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Runs", ""])
    for row in payload.get("rows") or []:
        lines.append(
            "- `{run_id}` {brand_name}: status `{status}`, llm `{llm_status}`, projected `{vnext_projected_status}`, "
            "class_delta `{class_delta}`, materiality_delta `{materiality_delta}`, retries `{retries}`".format(
                run_id=row.get("run_id"),
                brand_name=row.get("brand_name") or "",
                status=row.get("status") or "",
                llm_status=row.get("llm_status") or "",
                vnext_projected_status=row.get("vnext_projected_status") or "",
                class_delta=row.get("class_delta") or 0,
                materiality_delta=row.get("materiality_delta") or 0,
                retries=row.get("retries") or 0,
            )
        )
        for bucket, count in (row.get("bucket_counts") or {}).items():
            lines.append(f"  - `{bucket}`: `{count}`")
    lines.extend(["", "## Representative Examples", ""])
    for row in payload.get("rows") or []:
        for example in row.get("examples") or []:
            lines.extend(
                [
                    f"### {row.get('run_id')} {row.get('brand_name')} · {example.get('bucket')}",
                    "",
                    f"- Change: `{example.get('change')}`",
                    f"- Provider/source: `{example.get('provider')}` / `{example.get('source_class')}`",
                    f"- URL: `{example.get('url')}`",
                    f"- Text: {example.get('text_preview')}",
                    "",
                ]
            )
    return "\n".join(lines)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _unique_ints(values: list[int]) -> list[int]:
    out: list[int] = []
    for value in values:
        item = int(value)
        if item not in out:
            out.append(item)
    return out


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 2)


if __name__ == "__main__":
    raise SystemExit(main())
