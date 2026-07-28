#!/usr/bin/env python3
"""Compare Brand3 local and deployed Scanner/Audit outputs for the same URLs.

The script intentionally exercises the public Scanner API because a URL-based
Scanner run also creates an attached Brand Audit snapshot. That gives us one
common entry point and four comparable payloads per environment:
status, result, evidence, methodology, and audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.scanner_api_stability import (
    DEFAULT_DEPLOY_ORIGIN,
    configured_scanner_api_url,
    scanner_api_origin,
)


DEFAULT_LOCAL_BASE = "http://127.0.0.1:8000"
DEFAULT_DEPLOY_BASE = DEFAULT_DEPLOY_ORIGIN
DEFAULT_OUT_DIR = Path("scratch/local_vs_deploy_pipeline_compare")
TLDR_KEYS = (
    "value_proposition",
    "mission",
    "vision",
    "personality",
    "positioning",
    "proof",
    "audience",
    "offer",
    "magnetism",
)


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: str

    def json(self) -> dict[str, Any]:
        try:
            data = json.loads(self.body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Expected JSON response, got invalid JSON: {self.body[:300]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Expected JSON object response.")
        return data


def main() -> int:
    deploy_base = scanner_api_origin(
        os.environ.get("BRAND3_COMPARE_DEPLOY_BASE")
        or configured_scanner_api_url()
    )
    parser = argparse.ArgumentParser(
        description="Run the same Brand3 Scanner/Audit case in local and deploy, then compare normalized outputs."
    )
    parser.add_argument("--url", action="append", required=True, help="Brand URL to compare. Repeatable.")
    parser.add_argument("--local-base", default=os.environ.get("BRAND3_COMPARE_LOCAL_BASE", DEFAULT_LOCAL_BASE))
    parser.add_argument("--deploy-base", default=deploy_base)
    parser.add_argument("--scanner-token", default=os.environ.get("BRAND3_SCANNER_API_TOKEN") or read_env_value("BRAND3_SCANNER_API_TOKEN"))
    parser.add_argument("--mode", choices=("auto", "api", "web"), default="auto")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-wait-seconds", type=int, default=900)
    parser.add_argument("--request-timeout", type=int, default=45)
    parser.add_argument("--lang", choices=("es", "en"), default="es")
    args = parser.parse_args()

    if args.mode == "api" and not args.scanner_token:
        print("Missing scanner token. Set BRAND3_SCANNER_API_TOKEN or pass --scanner-token.", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cases: list[dict[str, Any]] = []
    for url in args.url:
        case = {"url": url, "local": None, "deploy": None, "comparison": None}
        log_progress(f"Starting local {args.mode} comparison case: {url}")
        local = run_case(
            label="local",
            base_url=args.local_base,
            url=url,
            scanner_token=args.scanner_token,
            mode=args.mode,
            lang=args.lang,
            poll_interval=args.poll_interval,
            max_wait_seconds=args.max_wait_seconds,
            request_timeout=args.request_timeout,
        )
        log_progress(case_progress_line("local", local))
        log_progress(f"Starting deploy {args.mode} comparison case: {url}")
        deploy = run_case(
            label="deploy",
            base_url=args.deploy_base,
            url=url,
            scanner_token=args.scanner_token,
            mode=args.mode,
            lang=args.lang,
            poll_interval=args.poll_interval,
            max_wait_seconds=args.max_wait_seconds,
            request_timeout=args.request_timeout,
        )
        log_progress(case_progress_line("deploy", deploy))
        case["local"] = local
        case["deploy"] = deploy
        case["comparison"] = compare_cases(local, deploy)
        cases.append(case)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "local_base": args.local_base,
        "deploy_base": args.deploy_base,
        "case_count": len(cases),
        "summary": summarize_cases(cases),
        "cases": cases,
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"comparison-{timestamp}.json"
    md_path = out_dir / f"comparison-{timestamp}.md"
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    md_text = render_markdown(payload)
    json_path.write_text(json_text, encoding="utf-8")
    md_path.write_text(md_text, encoding="utf-8")
    latest_json.write_text(json_text, encoding="utf-8")
    latest_md.write_text(md_text, encoding="utf-8")

    print(f"Wrote {len(cases)} comparison case(s)")
    print(f"Markdown: {md_path}")
    print(f"JSON: {json_path}")
    return 1 if payload["summary"]["critical_count"] else 0


def run_case(
    *,
    label: str,
    base_url: str,
    url: str,
    scanner_token: str | None,
    mode: str,
    lang: str,
    poll_interval: float,
    max_wait_seconds: int,
    request_timeout: int,
) -> dict[str, Any]:
    if mode == "web" or (mode == "auto" and not scanner_token):
        return run_web_case(
            label=label,
            base_url=base_url,
            url=url,
            lang=lang,
            poll_interval=poll_interval,
            max_wait_seconds=max_wait_seconds,
            request_timeout=request_timeout,
        )
    if not scanner_token:
        return {
            "label": label,
            "base_url": base_url,
            "url": url,
            "ok": False,
            "error": "missing_scanner_token",
        }
    return run_api_case(
        label=label,
        base_url=base_url,
        url=url,
        scanner_token=scanner_token,
        lang=lang,
        poll_interval=poll_interval,
        max_wait_seconds=max_wait_seconds,
        request_timeout=request_timeout,
    )


def run_api_case(
    *,
    label: str,
    base_url: str,
    url: str,
    scanner_token: str,
    lang: str,
    poll_interval: float,
    max_wait_seconds: int,
    request_timeout: int,
) -> dict[str, Any]:
    started_at = time.time()
    try:
        create = post_json(
            join_url(base_url, "/api/v1/scanner"),
            {"url": url, "lang": lang},
            scanner_token=scanner_token,
            timeout=request_timeout,
        ).json()
        scan_id = int(create["id"])
        status = poll_status(
            base_url=base_url,
            scan_id=scan_id,
            scanner_token=scanner_token,
            lang=lang,
            poll_interval=poll_interval,
            max_wait_seconds=max_wait_seconds,
            timeout=request_timeout,
        )
        if status.get("status") != "ready":
            return {
                "label": label,
                "base_url": base_url,
                "url": url,
                "scan_id": scan_id,
                "mode": "api",
                "ok": False,
                "error": status.get("error_message") or f"scan_status:{status.get('status')}",
                "status": status,
                "elapsed_seconds": round(time.time() - started_at, 2),
            }

        result = get_json(join_url(base_url, f"/api/v1/scanner/{scan_id}/result?lang={lang}"), scanner_token, request_timeout)
        evidence = get_json(join_url(base_url, f"/api/v1/scanner/{scan_id}/evidence"), scanner_token, request_timeout)
        methodology = get_json(join_url(base_url, f"/api/v1/scanner/{scan_id}/methodology"), scanner_token, request_timeout)
        audit = get_json(join_url(base_url, f"/api/v1/scanner/{scan_id}/audit"), scanner_token, request_timeout)
        return {
            "label": label,
            "base_url": base_url,
            "url": url,
            "scan_id": scan_id,
            "mode": "api",
            "ok": True,
            "elapsed_seconds": round(time.time() - started_at, 2),
            "status": status,
            "normalized": normalize_payloads(
                status=status,
                result=result,
                evidence=evidence,
                methodology=methodology,
                audit=audit,
            ),
            "raw_refs": {
                "status_url": join_url(base_url, f"/api/v1/scanner/{scan_id}"),
                "result_url": join_url(base_url, f"/api/v1/scanner/{scan_id}/result"),
                "evidence_url": join_url(base_url, f"/api/v1/scanner/{scan_id}/evidence"),
                "methodology_url": join_url(base_url, f"/api/v1/scanner/{scan_id}/methodology"),
                "audit_url": join_url(base_url, f"/api/v1/scanner/{scan_id}/audit"),
            },
        }
    except Exception as exc:
        return {
            "label": label,
            "base_url": base_url,
            "url": url,
            "ok": False,
            "mode": "api",
            "error": str(exc),
            "elapsed_seconds": round(time.time() - started_at, 2),
        }


def run_web_case(
    *,
    label: str,
    base_url: str,
    url: str,
    lang: str,
    poll_interval: float,
    max_wait_seconds: int,
    request_timeout: int,
) -> dict[str, Any]:
    started_at = time.time()
    try:
        create = post_form(
            join_url(base_url, "/magnetism-scanner/analyze"),
            {"url": url, "lang": lang},
            timeout=request_timeout,
        )
        location = create.headers.get("Location") or create.headers.get("location") or ""
        if create.status not in {302, 303} or not location:
            return {
                "label": label,
                "base_url": base_url,
                "url": url,
                "ok": False,
                "mode": "web",
                "error": f"unexpected_create_response:{create.status}",
                "body_preview": compact_text(strip_html(create.body), 300),
            }
        status_path = location
        scan_path = poll_web_status(
            base_url=base_url,
            status_path=status_path,
            poll_interval=poll_interval,
            max_wait_seconds=max_wait_seconds,
            timeout=request_timeout,
        )
        scan_id = parse_scan_id(scan_path)
        pages = {
            "detail": get_text(join_url(base_url, scan_path), request_timeout),
            "research": get_text(join_url(base_url, f"/magnetism-scanner/scan/{scan_id}/research?lang={lang}"), request_timeout),
            "audit": get_text(join_url(base_url, f"/magnetism-scanner/scan/{scan_id}/audit?lang={lang}"), request_timeout),
            "methodology": get_text(join_url(base_url, f"/magnetism-scanner/scan/{scan_id}/methodology?lang={lang}"), request_timeout),
        }
        return {
            "label": label,
            "base_url": base_url,
            "url": url,
            "scan_id": scan_id,
            "mode": "web",
            "ok": True,
            "elapsed_seconds": round(time.time() - started_at, 2),
            "normalized": normalize_web_pages(pages),
            "raw_refs": {
                key: join_url(base_url, path)
                for key, path in {
                    "detail": scan_path,
                    "research": f"/magnetism-scanner/scan/{scan_id}/research?lang={lang}",
                    "audit": f"/magnetism-scanner/scan/{scan_id}/audit?lang={lang}",
                    "methodology": f"/magnetism-scanner/scan/{scan_id}/methodology?lang={lang}",
                }.items()
            },
        }
    except Exception as exc:
        return {
            "label": label,
            "base_url": base_url,
            "url": url,
            "ok": False,
            "mode": "web",
            "error": str(exc),
            "elapsed_seconds": round(time.time() - started_at, 2),
        }


def poll_web_status(
    *,
    base_url: str,
    status_path: str,
    poll_interval: float,
    max_wait_seconds: int,
    timeout: int,
) -> str:
    deadline = time.time() + max_wait_seconds
    last_preview = ""
    while time.time() < deadline:
        response = request("GET", join_url(base_url, status_path), timeout=timeout, follow_redirects=False)
        location = response.headers.get("Location") or response.headers.get("location") or ""
        if response.status in {302, 303} and "/magnetism-scanner/scan/" in location:
            return location
        last_preview = compact_text(strip_html(response.body), 240)
        if "failed" in last_preview.lower() or "fall" in last_preview.lower():
            raise RuntimeError(f"web scan failed or reported failure: {last_preview}")
        time.sleep(poll_interval)
    raise RuntimeError(f"Timed out after {max_wait_seconds}s waiting for web scan. Last page: {last_preview}")


def poll_status(
    *,
    base_url: str,
    scan_id: int,
    scanner_token: str,
    lang: str,
    poll_interval: float,
    max_wait_seconds: int,
    timeout: int,
) -> dict[str, Any]:
    deadline = time.time() + max_wait_seconds
    last_status: dict[str, Any] = {}
    while time.time() < deadline:
        last_status = get_json(join_url(base_url, f"/api/v1/scanner/{scan_id}?lang={lang}"), scanner_token, timeout)
        if last_status.get("status") in {"ready", "failed"}:
            return last_status
        time.sleep(poll_interval)
    return {
        **last_status,
        "status": last_status.get("status") or "timeout",
        "error_message": f"Timed out after {max_wait_seconds}s",
    }


def normalize_payloads(
    *,
    status: dict[str, Any],
    result: dict[str, Any],
    evidence: dict[str, Any],
    methodology: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    result_metadata = result.get("result_metadata") if isinstance(result.get("result_metadata"), dict) else {}
    audit_run = audit.get("run") if isinstance(audit.get("run"), dict) else {}
    audit_payload = audit.get("audit") if isinstance(audit.get("audit"), dict) else {}
    report_readiness = audit_payload.get("report_readiness") if isinstance(audit_payload.get("report_readiness"), dict) else {}
    audit_publication = audit_payload.get("publication_decision") if isinstance(audit_payload.get("publication_decision"), dict) else {}
    scanner_readiness = result_metadata.get("scanner_readiness") if isinstance(result_metadata.get("scanner_readiness"), dict) else {}
    scanner_publication = (
        result_metadata.get("publication_decision")
        if isinstance(result_metadata.get("publication_decision"), dict)
        else {}
    )

    evidence_model = evidence.get("evidence") if isinstance(evidence.get("evidence"), dict) else {}
    methodology_model = methodology.get("methodology") if isinstance(methodology.get("methodology"), dict) else {}
    score_breakdown = methodology_model.get("score_breakdown") if isinstance(methodology_model.get("score_breakdown"), dict) else {}
    coherence_breakdown = (
        score_breakdown.get("coherence")
        if isinstance(score_breakdown.get("coherence"), dict)
        else {}
    )
    magnetism_breakdown = (
        score_breakdown.get("magnetism")
        if isinstance(score_breakdown.get("magnetism"), dict)
        else {}
    )
    tldr = result.get("tldr_brand3") if isinstance(result.get("tldr_brand3"), dict) else {}

    return {
        "brand_name": result.get("brand_name") or status.get("brand_name") or audit_run.get("brand_name"),
        "url": result.get("url") or status.get("url") or audit_run.get("url"),
        "scanner": {
            "status": status.get("status"),
            "phase": status.get("phase"),
            "readiness_status": scanner_readiness.get("status"),
            "publication_status": scanner_publication.get("status"),
            "publication_publishable": scanner_publication.get("publishable"),
            "scan_mode": scanner_scan_mode_summary(status.get("scan_mode") if isinstance(status.get("scan_mode"), dict) else {}),
            "score_magnetism": _number((result.get("scores") or {}).get("magnetism")),
            "score_coherence": _number((result.get("scores") or {}).get("coherence")),
            "score_breakdown": score_breakdown,
            "score_magnetism_breakdown": magnetism_breakdown,
            "score_coherence_breakdown": coherence_breakdown,
            "quadrant": (result.get("scores") or {}).get("quadrant"),
            "pipeline_version": result_metadata.get("pipeline_version"),
            "stale_against_current_pipeline": result_metadata.get("stale_against_current_pipeline"),
            "generated_with": result_metadata.get("generated_with") if isinstance(result_metadata.get("generated_with"), dict) else {},
            "failure_reason_code": ((status.get("failure_diagnostics") or {}).get("reason_code") if isinstance(status.get("failure_diagnostics"), dict) else None),
            "tldr": normalize_tldr(tldr),
        },
        "audit": {
            "available": audit.get("available"),
            "source_run_id": audit.get("source_run_id"),
            "brand_name": audit_run.get("brand_name"),
            "url": audit_run.get("url"),
            "composite_score": _number(audit_run.get("composite_score")),
            "report_mode": report_readiness.get("report_mode"),
            "readiness_version": report_readiness.get("version"),
            "publication_status": audit_publication.get("status"),
            "publication_publishable": audit_publication.get("publishable"),
            "audit_fingerprint": fingerprint_json(audit_payload),
        },
        "evidence": {
            "source_rows": count_key_lengths(evidence_model, "source_rows"),
            "sources": count_key_lengths(evidence_model, "sources"),
            "evidence_items": count_key_lengths(evidence_model, "evidence_items"),
            "external_candidates": count_key_lengths(evidence_model, "external_candidates"),
            "research_pack_source": first_value_for_key(evidence_model, "research_pack_source"),
            "fingerprint": fingerprint_json(evidence_model),
        },
        "methodology": {
            "result_metadata": methodology_model.get("result_metadata") if isinstance(methodology_model, dict) else {},
            "tldr_generation_mode": methodology_model.get("tldr_generation_mode"),
            "research_pack_source": methodology_model.get("research_pack_source"),
            "analysis_error": methodology_model.get("analysis_error"),
            "fingerprint": fingerprint_json(methodology_model),
        },
    }


def normalize_web_pages(pages: dict[str, str]) -> dict[str, Any]:
    detail_text = strip_html(pages.get("detail", ""))
    research_text = strip_html(pages.get("research", ""))
    audit_text = strip_html(pages.get("audit", ""))
    methodology_text = strip_html(pages.get("methodology", ""))
    return {
        "mode": "web",
        "brand_name": extract_title(pages.get("detail", "")),
        "url": None,
        "scanner": {
            "status": "ready",
            "phase": "ready",
            "readiness_status": None,
            "publication_status": None,
            "publication_publishable": None,
            "score_magnetism": first_number_after(detail_text, "score"),
            "score_coherence": first_number_after(detail_text, "coherencia") or first_number_after(detail_text, "coherence"),
            "quadrant": None,
            "pipeline_version": first_pipeline_version(methodology_text),
            "stale_against_current_pipeline": None,
            "generated_with": {},
            "scan_mode": {},
            "failure_reason_code": None,
            "tldr": normalize_web_tldr(detail_text),
            "page_fingerprint": short_hash(sanitize_web_text(detail_text)),
        },
        "audit": {
            "available": "Audit" in audit_text or "Auditor" in audit_text,
            "source_run_id": None,
            "brand_name": None,
            "url": None,
            "composite_score": first_number_after(audit_text, "score audit") or first_number_after(audit_text, "audit score"),
            "report_mode": web_status_marker(audit_text, ("publishable_brand_report", "insufficient_evidence")),
            "publication_status": None,
            "publication_publishable": None,
            "audit_fingerprint": short_hash(sanitize_web_text(audit_text)),
        },
        "evidence": {
            "source_rows": count_occurrences(research_text, ("Abrir fuente", "Open source", "https://", "http://")),
            "sources": count_occurrences(research_text, ("Abrir fuente", "Open source")),
            "evidence_items": count_occurrences(research_text, ("Evidencia", "Evidence")),
            "external_candidates": count_occurrences(research_text, ("Candidatos externos", "External candidates")),
            "research_pack_source": web_status_marker(research_text, ("evidence_graph", "legacy", "research_pack")),
            "fingerprint": short_hash(sanitize_web_text(research_text)),
        },
        "methodology": {
            "result_metadata": {},
            "tldr_generation_mode": web_status_marker(methodology_text, ("analyst_pass_validated", "legacy", "unknown")),
            "research_pack_source": web_status_marker(methodology_text, ("evidence_graph", "legacy_snapshot", "legacy")),
            "analysis_error": web_status_marker(methodology_text, ("llm_timeout", "llm_error", "analysis_error")),
            "fingerprint": short_hash(sanitize_web_text(methodology_text)),
        },
    }


def normalize_tldr(tldr: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in TLDR_KEYS:
        block = tldr.get(key)
        if not isinstance(block, dict):
            normalized[key] = {"detected": False, "confidence": None, "content_hash": ""}
            continue
        content = block.get("content") or block.get("answer") or block.get("text") or ""
        normalized[key] = {
            "detected": bool(block.get("detected")) or bool(str(content).strip()),
            "confidence": block.get("confidence"),
            "human_review_recommended": bool(block.get("human_review_recommended")),
            "content_hash": short_hash(str(content)),
            "content_preview": compact_text(str(content), 180),
        }
    return normalized


def compare_cases(local: dict[str, Any], deploy: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if not local.get("ok"):
        findings.append({"severity": "critical", "field": "local", "message": local.get("error") or "local run failed"})
    if not deploy.get("ok"):
        findings.append({"severity": "critical", "field": "deploy", "message": deploy.get("error") or "deploy run failed"})
    if findings:
        return {"findings": findings, "numeric_deltas": {}, "status": "blocked"}

    left = local["normalized"]
    right = deploy["normalized"]
    if left.get("mode") == "web" or right.get("mode") == "web":
        return compare_web_cases(left, right)
    numeric_deltas = {
        "scanner.score_magnetism": delta(left["scanner"]["score_magnetism"], right["scanner"]["score_magnetism"]),
        "scanner.score_coherence": delta(left["scanner"]["score_coherence"], right["scanner"]["score_coherence"]),
        "audit.composite_score": delta(left["audit"]["composite_score"], right["audit"]["composite_score"]),
        "evidence.source_rows": delta(left["evidence"]["source_rows"], right["evidence"]["source_rows"]),
        "evidence.evidence_items": delta(left["evidence"]["evidence_items"], right["evidence"]["evidence_items"]),
    }
    compare_equal(findings, left, right, "scanner.readiness_status", severity="critical")
    compare_equal(findings, left, right, "scanner.publication_status", severity="critical")
    compare_equal(findings, left, right, "scanner.publication_publishable", severity="critical")
    compare_equal(findings, left, right, "audit.report_mode", severity="critical")
    compare_equal(findings, left, right, "audit.publication_status", severity="critical")
    compare_equal(findings, left, right, "audit.publication_publishable", severity="critical")
    compare_equal(findings, left, right, "scanner.quadrant", severity="warning")
    compare_equal(findings, left, right, "scanner.pipeline_version", severity="warning")
    compare_equal(findings, left, right, "scanner.stale_against_current_pipeline", severity="warning")
    compare_equal(findings, left, right, "methodology.research_pack_source", severity="warning")
    compare_equal(findings, left, right, "methodology.tldr_generation_mode", severity="warning")
    compare_equal(findings, left, right, "scanner.scan_mode.mode", severity="warning")
    compare_equal(findings, left, right, "scanner.scan_mode.comparable", severity="critical")
    for generated_key in ("audit_snapshot", "research_pack", "evidence_graph", "analyst_pass", "research_pack_quality"):
        compare_equal(findings, left, right, f"scanner.generated_with.{generated_key}", severity="warning")
    if left["methodology"].get("analysis_error") or right["methodology"].get("analysis_error"):
        compare_equal(findings, left, right, "methodology.analysis_error", severity="warning")

    for field, item in numeric_deltas.items():
        if item["local"] is None or item["deploy"] is None:
            continue
        threshold = 5.0 if "score" in field else max(3.0, abs(float(item["deploy"])) * 0.3)
        if abs(float(item["delta"])) > threshold:
            findings.append(
                {
                    "severity": "warning",
                    "field": field,
                    "message": f"delta {item['delta']} exceeds threshold {round(threshold, 2)}",
                    "local": item["local"],
                    "deploy": item["deploy"],
                }
            )

    local_tldr = left["scanner"]["tldr"]
    deploy_tldr = right["scanner"]["tldr"]
    local_detected = detected_tldr_count(local_tldr)
    deploy_detected = detected_tldr_count(deploy_tldr)
    if abs(local_detected - deploy_detected) >= 2:
        findings.append(
            {
                "severity": "warning",
                "field": "scanner.tldr.detected_count",
                "message": "detected TLDR block count changed by 2 or more",
                "local": local_detected,
                "deploy": deploy_detected,
            }
        )
    for key in TLDR_KEYS:
        if local_tldr[key]["detected"] != deploy_tldr[key]["detected"]:
            findings.append(
                {
                    "severity": "warning",
                    "field": f"scanner.tldr.{key}.detected",
                    "message": "TLDR block detection changed",
                    "local": local_tldr[key]["detected"],
                    "deploy": deploy_tldr[key]["detected"],
                }
            )

    status = "regression_risk" if any(f["severity"] == "critical" for f in findings) else "review"
    if not findings:
        status = "no_material_diff"
    return {
        "status": status,
        "findings": findings,
        "numeric_deltas": numeric_deltas,
        "contract_signals": contract_signals(left, right),
        "tldr_detected": {"local": local_detected, "deploy": deploy_detected},
    }


def compare_web_cases(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    numeric_deltas = {
        "scanner.score_magnetism": delta(left["scanner"]["score_magnetism"], right["scanner"]["score_magnetism"]),
        "scanner.score_coherence": delta(left["scanner"]["score_coherence"], right["scanner"]["score_coherence"]),
        "audit.composite_score": delta(left["audit"]["composite_score"], right["audit"]["composite_score"]),
        "evidence.source_rows": delta(left["evidence"]["source_rows"], right["evidence"]["source_rows"]),
        "evidence.evidence_items": delta(left["evidence"]["evidence_items"], right["evidence"]["evidence_items"]),
    }
    compare_equal(findings, left, right, "scanner.pipeline_version", severity="warning")
    if left["audit"]["available"] != right["audit"]["available"]:
        findings.append(
            {
                "severity": "critical",
                "field": "audit.available",
                "message": "audit page availability differs",
                "local": left["audit"]["available"],
                "deploy": right["audit"]["available"],
            }
        )
    for field, item in numeric_deltas.items():
        if item["local"] is None or item["deploy"] is None:
            continue
        threshold = 5.0 if "score" in field else max(3.0, abs(float(item["deploy"])) * 0.3)
        if abs(float(item["delta"])) > threshold:
            findings.append(
                {
                    "severity": "warning",
                    "field": field,
                    "message": f"delta {item['delta']} exceeds threshold {round(threshold, 2)}",
                    "local": item["local"],
                    "deploy": item["deploy"],
                }
            )
    local_detected = detected_tldr_count(left["scanner"]["tldr"])
    deploy_detected = detected_tldr_count(right["scanner"]["tldr"])
    if abs(local_detected - deploy_detected) >= 2:
        findings.append(
            {
                "severity": "warning",
                "field": "scanner.tldr.detected_count",
                "message": "visible TLDR label count changed by 2 or more",
                "local": local_detected,
                "deploy": deploy_detected,
            }
        )
    if left["scanner"]["page_fingerprint"] != right["scanner"]["page_fingerprint"]:
        findings.append(
            {
                "severity": "info",
                "field": "scanner.visible_page_fingerprint",
                "message": "visible scanner page text differs; review Markdown/HTML refs",
                "local": left["scanner"]["page_fingerprint"],
                "deploy": right["scanner"]["page_fingerprint"],
            }
        )
    status = "regression_risk" if any(f["severity"] == "critical" for f in findings) else "review"
    if not findings:
        status = "no_material_diff"
    return {
        "status": status,
        "findings": findings,
        "numeric_deltas": numeric_deltas,
        "contract_signals": contract_signals(left, right),
        "tldr_detected": {"local": local_detected, "deploy": deploy_detected},
    }


def scanner_scan_mode_summary(scan_mode: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": scan_mode.get("mode") or scan_mode.get("scan_mode"),
        "comparable": scan_mode.get("comparable"),
        "canonical": scan_mode.get("canonical"),
        "source": scan_mode.get("source"),
    }


def contract_signals(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "scanner.readiness_status",
        "scanner.publication_status",
        "scanner.publication_publishable",
        "scanner.pipeline_version",
        "scanner.stale_against_current_pipeline",
        "scanner.scan_mode.mode",
        "scanner.scan_mode.comparable",
        "scanner.generated_with.evidence_graph",
        "scanner.generated_with.analyst_pass",
        "scanner.generated_with.research_pack_quality",
        "audit.report_mode",
        "audit.publication_status",
        "audit.publication_publishable",
        "methodology.research_pack_source",
        "methodology.tldr_generation_mode",
        "methodology.analysis_error",
    )
    return {
        field: {
            "local": get_path(left, field),
            "deploy": get_path(right, field),
        }
        for field in fields
    }


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    critical = 0
    warning = 0
    for case in cases:
        for finding in ((case.get("comparison") or {}).get("findings") or []):
            if finding.get("severity") == "critical":
                critical += 1
            elif finding.get("severity") == "warning":
                warning += 1
    return {
        "critical_count": critical,
        "warning_count": warning,
        "case_statuses": {
            case["url"]: (case.get("comparison") or {}).get("status")
            for case in cases
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Brand3 Local vs Deploy Pipeline Comparison",
        "",
        f"- Generated: `{payload['generated_at']}`",
        f"- Local: `{payload['local_base']}`",
        f"- Deploy: `{payload['deploy_base']}`",
        f"- Cases: `{payload['case_count']}`",
        f"- Critical findings: `{payload['summary']['critical_count']}`",
        f"- Warnings: `{payload['summary']['warning_count']}`",
        "",
    ]
    for case in payload["cases"]:
        comparison = case.get("comparison") or {}
        lines.extend([
            f"## {case['url']}",
            "",
            f"- Status: `{comparison.get('status')}`",
            f"- Local scan: `{((case.get('local') or {}).get('scan_id'))}`",
            f"- Deploy scan: `{((case.get('deploy') or {}).get('scan_id'))}`",
            "",
            "### Findings",
            "",
        ])
        findings = comparison.get("findings") or []
        if not findings:
            lines.append("- No material differences detected by configured thresholds.")
        else:
            for finding in findings:
                lines.append(
                    f"- `{finding.get('severity')}` `{finding.get('field')}`: {finding.get('message')}"
                )
        lines.extend(["", "### Numeric Deltas", ""])
        for field, item in (comparison.get("numeric_deltas") or {}).items():
            lines.append(
                f"- `{field}`: local `{item.get('local')}`, deploy `{item.get('deploy')}`, delta `{item.get('delta')}`"
            )
        score_diagnostics = score_diagnostics_lines(case)
        if score_diagnostics:
            lines.extend(["", "### Score Diagnostics", ""])
            lines.extend(score_diagnostics)
        contract = comparison.get("contract_signals") or {}
        if contract:
            lines.extend(["", "### Contract Signals", ""])
            for field, item in contract.items():
                lines.append(
                    f"- `{field}`: local `{item.get('local')}`, deploy `{item.get('deploy')}`"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def score_diagnostics_lines(case: dict[str, Any]) -> list[str]:
    local_scanner = (((case.get("local") or {}).get("normalized") or {}).get("scanner") or {})
    deploy_scanner = (((case.get("deploy") or {}).get("normalized") or {}).get("scanner") or {})
    rows: list[str] = []
    for label, key in (
        ("scanner.score_coherence_breakdown", "score_coherence_breakdown"),
        ("scanner.score_magnetism_breakdown", "score_magnetism_breakdown"),
    ):
        local_value = local_scanner.get(key)
        deploy_value = deploy_scanner.get(key)
        if not local_value and not deploy_value:
            continue
        rows.append(f"- `{label}`: local `{compact_json(local_value)}`, deploy `{compact_json(deploy_value)}`")
    return rows


def log_progress(message: str) -> None:
    print(f"[compare] {message}", flush=True)


def case_progress_line(label: str, case: dict[str, Any]) -> str:
    scan_id = case.get("scan_id")
    elapsed = case.get("elapsed_seconds")
    if case.get("ok"):
        return f"{label} ready scan={scan_id} elapsed={elapsed}s"
    return f"{label} failed scan={scan_id} elapsed={elapsed}s error={case.get('error')}"


def post_json(url: str, payload: dict[str, Any], *, scanner_token: str, timeout: int) -> HttpResult:
    return request(
        "POST",
        url,
        scanner_token=scanner_token,
        timeout=timeout,
        body=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
    )


def get_json(url: str, scanner_token: str, timeout: int) -> dict[str, Any]:
    return request("GET", url, scanner_token=scanner_token, timeout=timeout).json()


def post_form(url: str, payload: dict[str, Any], *, timeout: int) -> HttpResult:
    body = urllib.parse.urlencode(payload).encode("utf-8")
    return request(
        "POST",
        url,
        timeout=timeout,
        body=body,
        content_type="application/x-www-form-urlencoded",
        follow_redirects=False,
    )


def get_text(url: str, timeout: int) -> str:
    return request("GET", url, timeout=timeout).body


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(
    method: str,
    url: str,
    *,
    scanner_token: str | None = None,
    timeout: int,
    body: bytes | None = None,
    content_type: str | None = None,
    follow_redirects: bool = True,
) -> HttpResult:
    headers = {"Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    if scanner_token:
        headers["Authorization"] = f"Bearer {scanner_token}"
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return HttpResult(status=response.status, headers=dict(response.headers.items()), body=raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        if 300 <= exc.code < 400:
            return HttpResult(status=exc.code, headers=dict(exc.headers.items()), body=raw)
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {raw[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc


def join_url(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def read_env_value(key: str, path: str = ".env") -> str:
    env_path = Path(path)
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip().removeprefix("export ").strip()
        if name != key:
            continue
        return value.strip().strip("'").strip('"')
    return ""


def parse_scan_id(path: str) -> int:
    match = re.search(r"/magnetism-scanner/scan/(\d+)", path)
    if not match:
        raise RuntimeError(f"Could not parse scan id from redirect: {path}")
    return int(match.group(1))


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return compact_text(text, 100000)


def sanitize_web_text(text: str) -> str:
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?\b", " TIMESTAMP ", text)
    text = re.sub(r"/magnetism-scanner/scan/\d+", "/magnetism-scanner/scan/ID", text)
    text = re.sub(r"#\d+", "#ID", text)
    return compact_text(text, 100000)


def extract_title(html: str) -> str | None:
    match = re.search(r"(?is)<title>(.*?)</title>", html)
    if not match:
        return None
    return compact_text(strip_html(match.group(1)), 160)


def first_number_after(text: str, marker: str) -> float | None:
    pattern = re.compile(re.escape(marker) + r".{0,80}?(\d+(?:\.\d+)?)", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    return _number(match.group(1))


def first_pipeline_version(text: str) -> str | None:
    match = re.search(r"brand3_scanner_pipeline_[0-9_]+", text)
    return match.group(0) if match else None


def web_status_marker(text: str, markers: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for marker in markers:
        if marker.lower() in lowered:
            return marker
    return None


def normalize_web_tldr(text: str) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    lowered = text.lower()
    label_map = {
        "value_proposition": ("value proposition", "propuesta de valor"),
        "mission": ("mission", "misión"),
        "vision": ("vision", "visión"),
        "personality": ("personality", "personalidad"),
        "positioning": ("positioning", "posicionamiento"),
        "proof": ("proof", "prueba"),
        "audience": ("audience", "audiencia"),
        "offer": ("offer", "oferta"),
        "magnetism": ("magnetism", "magnetismo"),
    }
    for key in TLDR_KEYS:
        labels = label_map.get(key, (key,))
        normalized[key] = {
            "detected": any(label in lowered for label in labels),
            "confidence": None,
            "human_review_recommended": False,
            "content_hash": "",
            "content_preview": "",
        }
    return normalized


def count_occurrences(text: str, markers: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(lowered.count(marker.lower()) for marker in markers)


def compare_equal(findings: list[dict[str, Any]], left: dict[str, Any], right: dict[str, Any], path: str, *, severity: str) -> None:
    local = get_path(left, path)
    deploy = get_path(right, path)
    if local != deploy:
        findings.append(
            {
                "severity": severity,
                "field": path,
                "message": "local and deploy values differ",
                "local": local,
                "deploy": deploy,
            }
        )


def get_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def delta(local: Any, deploy: Any) -> dict[str, Any]:
    left = _number(local)
    right = _number(deploy)
    return {
        "local": left,
        "deploy": right,
        "delta": None if left is None or right is None else round(float(left) - float(right), 4),
    }


def compact_json(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def detected_tldr_count(tldr: dict[str, Any]) -> int:
    return sum(1 for key in TLDR_KEYS if (tldr.get(key) or {}).get("detected"))


def count_key_lengths(payload: Any, target_key: str) -> int:
    count = 0
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == target_key and isinstance(value, list):
                count += len(value)
            count += count_key_lengths(value, target_key)
    elif isinstance(payload, list):
        for item in payload:
            count += count_key_lengths(item, target_key)
    return count


def first_value_for_key(payload: Any, target_key: str) -> Any:
    if isinstance(payload, dict):
        if target_key in payload:
            return payload[target_key]
        for value in payload.values():
            found = first_value_for_key(value, target_key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = first_value_for_key(item, target_key)
            if found is not None:
                return found
    return None


def fingerprint_json(payload: Any) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        text = str(payload)
    return short_hash(text)


def short_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]


def compact_text(text: str, max_len: int) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1].rstrip() + "…"


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
