#!/usr/bin/env python3
"""Collect read-only production evidence vNext diagnostics in batch.

This script does not mutate production data. It calls the public vNext
diagnostic endpoint, stores local artifacts, and summarizes repeated decision
queue and work-order patterns so they can be promoted into deterministic
contracts or routed to LLM shadow review separately.
"""

from __future__ import annotations

import argparse
import json
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

    run_ids = list(args.run_ids)
    if args.latest_from_index:
        run_ids.extend(_latest_run_ids_from_index(base_url, limit=args.latest_from_index, timeout=args.timeout))
    run_ids = _unique_ints(run_ids)
    if not run_ids:
        raise SystemExit("Provide run IDs or --latest-from-index.")

    rows: list[dict[str, Any]] = []
    for run_id in run_ids:
        started = time.monotonic()
        payload = _fetch_json(
            f"{base_url}/magnetism-scanner/run/{run_id}/evidence-vnext",
            timeout=args.timeout,
        )
        _write_json(output_dir / f"vnext_{run_id}.json", payload)
        row = _run_row(run_id, payload=payload)
        row["elapsed_seconds"] = _elapsed(started)
        rows.append(row)
        print(
            "run={run_id} brand={brand_name} promotion={promotion_status} "
            "readiness={readiness_status} accepted={accepted} review={review_required} "
            "rejected={rejected} decisions={decision_count} work_orders={work_order_count} "
            "elapsed={elapsed_seconds}s".format(**row)
        )

    batch = {
        "version": "evidence_vnext_remote_batch_v0_1",
        "runtime_effect": False,
        "prompt_effect": False,
        "persistence_effect": False,
        "base_url": base_url,
        "run_ids": run_ids,
        "rows": rows,
        "summary": _summary(rows),
    }
    _write_json(output_dir / "batch_summary.json", batch)
    (output_dir / "batch_summary.md").write_text(_markdown(batch), encoding="utf-8")
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
        help="Append latest run IDs linked from /magnetism-scanner.",
    )
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("out/evidence_vnext/remote_batch"),
        help="Directory for JSON and Markdown artifacts.",
    )
    return parser.parse_args(argv)


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


def _fetch_json(url: str, *, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
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


def _run_row(run_id: int, *, payload: dict[str, Any]) -> dict[str, Any]:
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    report_row = rows[0] if rows and isinstance(rows[0], dict) else {}
    readiness_rows = ((report.get("readiness_matrix") or {}).get("rows") or [])
    readiness_row = readiness_rows[0] if readiness_rows and isinstance(readiness_rows[0], dict) else {}
    adjudication = report.get("adjudication_intake") if isinstance(report.get("adjudication_intake"), dict) else {}
    decision_queue = report.get("decision_queue") if isinstance(report.get("decision_queue"), list) else []
    work_orders = report.get("work_orders") if isinstance(report.get("work_orders"), list) else []
    top_review = report.get("top_review_reasons") if isinstance(report.get("top_review_reasons"), dict) else {}
    top_rejected = report.get("top_rejected_reasons") if isinstance(report.get("top_rejected_reasons"), dict) else {}
    work_order_triage = [_work_order_triage(item) for item in work_orders if isinstance(item, dict)]

    return {
        "run_id": run_id,
        "brand_name": payload.get("brand_name") or report_row.get("brand_name") or "",
        "url": payload.get("url") or report_row.get("url") or "",
        "status": report_row.get("status") or "",
        "promotion_status": report_row.get("promotion_status") or "",
        "readiness_status": readiness_row.get("readiness_status") or "",
        "next_action": readiness_row.get("next_action") or "",
        "automation_lane": readiness_row.get("automation_lane") or "",
        "human_required": bool(readiness_row.get("human_required")),
        "accepted": int(report_row.get("accepted") or 0),
        "review_required": int(report_row.get("review_required") or 0),
        "rejected": int(report_row.get("rejected") or 0),
        "changed_fields": int(report_row.get("changed_fields") or 0),
        "lost_fields": int(report_row.get("lost_fields") or 0),
        "material_lost_fields": int(report_row.get("material_lost_fields") or 0),
        "decision_count": len(decision_queue),
        "work_order_count": len(work_orders),
        "pending_adjudications": int(adjudication.get("pending_count") or 0),
        "decision_actions": [str(item.get("action") or "") for item in decision_queue if isinstance(item, dict)],
        "work_order_tasks": [str(item.get("task") or "") for item in work_orders if isinstance(item, dict)],
        "work_order_triage": work_order_triage,
        "work_order_triage_lanes": [str(item.get("lane") or "") for item in work_order_triage if item.get("lane")],
        "work_order_triage_recommendations": [
            str(item.get("recommendation") or "") for item in work_order_triage if item.get("recommendation")
        ],
        "review_reasons": dict(sorted((str(key), int(value or 0)) for key, value in top_review.items())),
        "rejected_reasons": dict(sorted((str(key), int(value or 0)) for key, value in top_rejected.items())),
        "remaining_reason_codes": list(readiness_row.get("remaining_reason_codes") or []),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(str(row.get("status") or "") for row in rows)
    promotion_counts = Counter(str(row.get("promotion_status") or "") for row in rows)
    readiness_counts = Counter(str(row.get("readiness_status") or "") for row in rows)
    next_action_counts = Counter(str(row.get("next_action") or "") for row in rows)
    decision_counts: Counter[str] = Counter()
    work_order_counts: Counter[str] = Counter()
    work_order_triage_lanes: Counter[str] = Counter()
    work_order_triage_recommendations: Counter[str] = Counter()
    review_reasons: Counter[str] = Counter()
    rejected_reasons: Counter[str] = Counter()
    remaining_reasons: Counter[str] = Counter()

    for row in rows:
        decision_counts.update(action for action in row.get("decision_actions") or [] if action)
        work_order_counts.update(task for task in row.get("work_order_tasks") or [] if task)
        work_order_triage_lanes.update(lane for lane in row.get("work_order_triage_lanes") or [] if lane)
        work_order_triage_recommendations.update(
            recommendation for recommendation in row.get("work_order_triage_recommendations") or [] if recommendation
        )
        review_reasons.update(row.get("review_reasons") or {})
        rejected_reasons.update(row.get("rejected_reasons") or {})
        remaining_reasons.update(reason for reason in row.get("remaining_reason_codes") or [] if reason)

    return {
        "run_count": len(rows),
        "accepted_total": sum(int(row.get("accepted") or 0) for row in rows),
        "review_required_total": sum(int(row.get("review_required") or 0) for row in rows),
        "rejected_total": sum(int(row.get("rejected") or 0) for row in rows),
        "human_required_count": sum(1 for row in rows if row.get("human_required")),
        "pending_adjudication_total": sum(int(row.get("pending_adjudications") or 0) for row in rows),
        "work_order_total": sum(int(row.get("work_order_count") or 0) for row in rows),
        "status_counts": _count_dict(status_counts),
        "promotion_counts": _count_dict(promotion_counts),
        "readiness_counts": _count_dict(readiness_counts),
        "next_action_counts": _count_dict(next_action_counts),
        "decision_action_counts": _count_dict(decision_counts),
        "work_order_task_counts": _count_dict(work_order_counts),
        "work_order_triage_lane_counts": _count_dict(work_order_triage_lanes),
        "work_order_triage_recommendation_counts": _count_dict(work_order_triage_recommendations),
        "top_review_reasons": _count_dict(review_reasons.most_common(10)),
        "top_rejected_reasons": _count_dict(rejected_reasons.most_common(10)),
        "remaining_reason_counts": _count_dict(remaining_reasons.most_common(10)),
    }


def _markdown(batch: dict[str, Any]) -> str:
    summary = batch.get("summary") or {}
    lines = [
        "# Evidence vNext Remote Batch",
        "",
        "## Summary",
        "",
        f"- Runs: `{summary.get('run_count', 0)}`",
        f"- Accepted evidence: `{summary.get('accepted_total', 0)}`",
        f"- Review evidence: `{summary.get('review_required_total', 0)}`",
        f"- Rejected evidence: `{summary.get('rejected_total', 0)}`",
        f"- Human required: `{summary.get('human_required_count', 0)}`",
        f"- Pending adjudications: `{summary.get('pending_adjudication_total', 0)}`",
        f"- Work orders: `{summary.get('work_order_total', 0)}`",
        "",
        "## Status Counts",
        "",
    ]
    _append_counts(lines, summary.get("status_counts") or {})
    lines.extend(["", "## Promotion Counts", ""])
    _append_counts(lines, summary.get("promotion_counts") or {})
    lines.extend(["", "## Readiness Counts", ""])
    _append_counts(lines, summary.get("readiness_counts") or {})
    lines.extend(["", "## Decision Actions", ""])
    _append_counts(lines, summary.get("decision_action_counts") or {})
    lines.extend(["", "## Work Order Tasks", ""])
    _append_counts(lines, summary.get("work_order_task_counts") or {})
    lines.extend(["", "## Work Order Triage", ""])
    _append_counts(lines, summary.get("work_order_triage_lane_counts") or {})
    lines.extend(["", "## Work Order Recommendations", ""])
    _append_counts(lines, summary.get("work_order_triage_recommendation_counts") or {})
    lines.extend(["", "## Top Review Reasons", ""])
    _append_counts(lines, summary.get("top_review_reasons") or {})
    lines.extend(["", "## Top Rejected Reasons", ""])
    _append_counts(lines, summary.get("top_rejected_reasons") or {})
    lines.extend(["", "## Runs", ""])
    lines.append("| Run | Brand | Promotion | Readiness | Accepted | Review | Rejected | Next | Work orders |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: |")
    for row in batch.get("rows") or []:
        lines.append(
            "| {run_id} | {brand_name} | {promotion_status} | {readiness_status} | {accepted} | {review_required} | {rejected} | {next_action} | {work_order_count} |".format(
                run_id=row.get("run_id"),
                brand_name=_md_cell(row.get("brand_name") or ""),
                promotion_status=_md_cell(row.get("promotion_status") or ""),
                readiness_status=_md_cell(row.get("readiness_status") or ""),
                accepted=row.get("accepted") or 0,
                review_required=row.get("review_required") or 0,
                rejected=row.get("rejected") or 0,
                next_action=_md_cell(row.get("next_action") or ""),
                work_order_count=row.get("work_order_count") or 0,
            )
        )
    work_order_rows = [
        (row, triage)
        for row in batch.get("rows") or []
        for triage in row.get("work_order_triage") or []
        if isinstance(triage, dict)
    ]
    if work_order_rows:
        lines.extend(["", "## Work Order Routing", ""])
        lines.append("| Run | Brand | Task | Lane | Recommendation | Model role |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for row, triage in work_order_rows:
            lines.append(
                "| {run_id} | {brand_name} | {task} | {lane} | {recommendation} | {model_role} |".format(
                    run_id=row.get("run_id"),
                    brand_name=_md_cell(row.get("brand_name") or ""),
                    task=_md_cell(triage.get("task") or ""),
                    lane=_md_cell(triage.get("lane") or ""),
                    recommendation=_md_cell(triage.get("recommendation") or ""),
                    model_role=_md_cell(triage.get("model_role") or ""),
                )
            )
    lines.append("")
    return "\n".join(lines)


def _append_counts(lines: list[str], counts: dict[str, int]) -> None:
    if not counts:
        lines.append("- None")
        return
    for key, value in counts.items():
        lines.append(f"- `{key}`: `{value}`")


def _work_order_triage(work_order: dict[str, Any]) -> dict[str, Any]:
    next_action = str(work_order.get("next_action") or "")
    task = str(work_order.get("task") or "")
    context = work_order.get("context") if isinstance(work_order.get("context"), dict) else {}
    remaining = context.get("remaining_review_examples") if isinstance(context.get("remaining_review_examples"), list) else []
    overlaps = context.get("projected_material_overlaps") if isinstance(context.get("projected_material_overlaps"), list) else []
    changed_fields = context.get("changed_material_fields") if isinstance(context.get("changed_material_fields"), list) else []
    reason_codes = sorted(
        {
            str(item.get("classification_reason") or "")
            for item in (*remaining, *overlaps)
            if isinstance(item, dict) and item.get("classification_reason")
        }
    )
    source_urls = sorted(
        {
            str(item.get("url") or "")
            for item in (*remaining, *overlaps)
            if isinstance(item, dict) and str(item.get("url") or "")
        }
    )
    material_fields = sorted(
        {
            str(item.get("field") or "")
            for item in (*overlaps, *changed_fields)
            if isinstance(item, dict) and item.get("field")
        }
    )

    if next_action == "add_source_url_or_remove_material_quote":
        lane = "deterministic_source_contract"
        recommendation = "fix_provenance_or_exclude_unsourced_material"
        model_role = "none"
        rationale = "A material quote without a source URL is a provenance contract failure; do not ask a model to infer the source."
    elif next_action == "confirm_entity_alias_before_promotion":
        lane = "llm_assisted_adjudication"
        recommendation = "structured_entity_alias_shadow"
        model_role = "suggest_alias_confidence_only"
        rationale = "Entity/profile aliasing is semantic, but promotion still requires adjudication and recompute."
    elif next_action == "manual_audit_projected_material_changes":
        lane = "llm_structured_candidate"
        recommendation = "structured_material_diff_review"
        model_role = "classify_materiality_entity_fit_and_source_trust"
        rationale = "The deterministic pipeline found material field changes; a structured model can reduce manual review by judging materiality and semantic risk."
    else:
        lane = "manual_review_only"
        recommendation = "keep_human_review"
        model_role = "none"
        rationale = "No supported automation lane is defined for this work order action."

    return {
        "task": task,
        "next_action": next_action,
        "lane": lane,
        "recommendation": recommendation,
        "model_role": model_role,
        "rationale": rationale,
        "reason_codes": reason_codes,
        "source_urls": source_urls,
        "material_fields": material_fields,
    }


def _count_dict(counter: Counter[str] | list[tuple[str, int]]) -> dict[str, int]:
    items = counter.items() if isinstance(counter, Counter) else counter
    return {str(key): int(value) for key, value in sorted(items, key=lambda item: (-int(item[1]), str(item[0]))) if key}


def _unique_ints(values: list[int]) -> list[int]:
    out: list[int] = []
    for value in values:
        int_value = int(value)
        if int_value not in out:
            out.append(int_value)
    return out


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 2)


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
