"""Pure adapters from internal scan/report payloads to the public v1 contract."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse

from src.services.scanner_analysis_contract import analysis_contract_from_report
from src.services.scanner_score_publication import score_publication_from_report


_STATE_MAP = {
    "accepted": "running",
    "running": "running",
    "blocked": "blocked",
    "done": "completed",
    "error": "failed",
    "cancelled": "cancelled",
}


def scan_links(scan_id: str) -> dict[str, str]:
    root = f"/api/v1/scans/{scan_id}"
    return {
        "self": root,
        "result": f"{root}/result",
        "evidence": f"{root}/evidence",
        "continue_scan": f"{root}/continue",
        "cancel": f"{root}/cancel",
        "report": f"/report/{scan_id}",
        "report_markdown": f"/report/{scan_id}.md",
    }


def status_payload(status: dict[str, Any], *, language: str = "es") -> dict[str, Any]:
    scan_id = str(status.get("id") or "")
    internal_state = str(status.get("state") or "error")
    public_state = _STATE_MAP.get(internal_state, "failed")
    phases = [dict(item) for item in status.get("phases") or [] if isinstance(item, dict)]
    completed = sum(1 for item in phases if item.get("state") == "done")
    active = sum(1 for item in phases if item.get("state") == "running")
    progress = (completed + (0.5 if active else 0)) / len(phases) if phases else 0.0
    if public_state == "completed":
        progress = 1.0
    failure = None
    if public_state == "failed":
        failure_code = str(status.get("error_code") or "scan_execution_failed")
        failure = {
            "code": failure_code,
            "message": _public_failure_message(failure_code),
            "retryable": failure_code == "process_restarted",
        }
    return {
        "object": "scan",
        "api_version": "v1",
        "id": scan_id,
        "status": public_state,
        "phase": str(status.get("phase") or public_state),
        "progress": round(progress, 3),
        "brand_name": str(status.get("brand_name") or ""),
        "url": str(status.get("url") or ""),
        "language": language,
        "started_at": status.get("started_at"),
        "completed_at": status.get("completed_at"),
        "phases": phases,
        "acquisition": [dict(item) for item in status.get("acquisition") or [] if isinstance(item, dict)],
        "acquisition_gate": (
            dict(status.get("acquisition_gate")) if isinstance(status.get("acquisition_gate"), dict) else {}
        ),
        "failure": failure,
        "result_available": public_state == "completed",
        "durable_status": True,
        "resumable_after_restart": False,
        "links": scan_links(scan_id),
    }


def completed_status_from_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(report.get("id") or ""),
        "state": "done",
        "phase": "done",
        "brand_name": str(report.get("brand_name") or ""),
        "url": str(report.get("url") or ""),
        "started_at": report.get("created_at"),
        "completed_at": report.get("created_at"),
        "phases": [],
        "acquisition": [],
        "acquisition_gate": report.get("acquisition_gate") or {},
        "error": None,
    }


def result_payload(report: dict[str, Any]) -> dict[str, Any]:
    scan_id = str(report.get("id") or "")
    score_publication = score_publication_from_report(report)
    score_publishable = bool(score_publication["publishable"])
    components = [
        _component_payload(item, score_publishable=score_publishable)
        for item in report.get("components") or []
        if isinstance(item, dict)
    ]
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    debug = flow.get("interpretation_debug") if isinstance(flow.get("interpretation_debug"), dict) else {}
    sv9 = raw.get("sv9") if isinstance(raw.get("sv9"), dict) else {}
    sv9_result = sv9.get("result") if isinstance(sv9.get("result"), dict) else {}
    evaluator_model = str(
        sv9_result.get("evaluator_model")
        or sv9_result.get("evaluation_model")
        or next((item.get("evaluation_model") for item in report.get("components") or [] if isinstance(item, dict) and item.get("evaluation_model")), "")
        or ""
    )
    executive_reading = report.get("executive_reading")
    if not isinstance(executive_reading, str):
        executive_reading = ""
    limitations = [_string_value(item) for item in report.get("limitations") or []]
    limitations = [item for item in limitations if item]
    not_detected = [_string_value(item) for item in report.get("not_detected") or []]
    not_detected = [item for item in not_detected if item]
    insufficient_evidence = []
    for component in report.get("components") or []:
        if not isinstance(component, dict):
            continue
        block = component.get("block") if isinstance(component.get("block"), dict) else {}
        if str(block.get("coverage_status") or "") != "insufficient_acquisition":
            continue
        key = str(component.get("key") or component.get("component") or "").strip()
        if key and key not in insufficient_evidence:
            insufficient_evidence.append(key)
    analysis_contract = analysis_contract_from_report(report)
    raw_score = _number(score_publication.get("raw_value"))
    return {
        "object": "scan_result",
        "api_version": "v1",
        "id": scan_id,
        "status": "completed",
        "brand": _brand_payload(report),
        "score": {
            "value": raw_score if score_publishable else None,
            "raw_value": raw_score,
            "publishable": score_publishable,
            "retention_reason": str(
                score_publication.get("retention_reason") or ""
            ),
            "scale": 100,
            "base_average": _number(report.get("base_average")),
            "reliability_status": str(report.get("reliability_status") or "unknown"),
        },
        "summary": executive_reading or _first_component_summary(components),
        "executive_reading": executive_reading,
        "components": components,
        "detected_count": int(report.get("detected_count") or 0),
        "component_count": len(components),
        "not_detected": not_detected,
        "insufficient_evidence": insufficient_evidence,
        "limitations": limitations,
        "acquisition_summary": (
            dict(report.get("coverage_acquisition"))
            if isinstance(report.get("coverage_acquisition"), dict)
            else {}
        ),
        "acquisition_gate": (
            dict(report.get("acquisition_gate")) if isinstance(report.get("acquisition_gate"), dict) else {}
        ),
        "stability": (
            dict(report.get("stability")) if isinstance(report.get("stability"), dict) else {}
        ),
        "metadata": {
            "schema_version": "b3s-scanner-result-v1",
            "pipeline_schema_version": str(raw.get("schema_version") or "unknown"),
            "pipeline_commit_sha": str(report.get("pipeline_commit_sha") or "unknown"),
            "rubric_version": str(sv9_result.get("rubric_version") or "unknown"),
            "prompt_version": str(debug.get("prompt_version") or "unknown"),
            "evaluator_model": evaluator_model or "unknown",
            "analysis_contract_fingerprint": str(
                analysis_contract.get("fingerprint") or ""
            ),
            "generated_at": report.get("created_at"),
        },
        "links": scan_links(scan_id),
    }


def evidence_payload(report: dict[str, Any]) -> dict[str, Any]:
    references = _evidence_references(report)
    acquisition = report.get("coverage_acquisition") if isinstance(report.get("coverage_acquisition"), dict) else {}
    absences = [dict(item) for item in report.get("absences") or [] if isinstance(item, dict)]
    attempts = [dict(item) for item in report.get("attempts") or [] if isinstance(item, dict)]
    return {
        "object": "scan_evidence",
        "api_version": "v1",
        "scan_id": str(report.get("id") or ""),
        "brand": _brand_payload(report),
        "acquisition_summary": dict(acquisition),
        "acquisition_gate": (
            dict(report.get("acquisition_gate")) if isinstance(report.get("acquisition_gate"), dict) else {}
        ),
        "references": references,
        "absences": absences,
        "attempts": attempts,
        "totals": {
            "references": len(references),
            "absences": len(absences),
            "attempts": len(attempts),
            "owned_urls": int(acquisition.get("owned_url_count") or 0),
            "external_proof": int(acquisition.get("external_proof_count") or 0),
        },
        "links": scan_links(str(report.get("id") or "")),
    }


def report_etag(report: dict[str, Any]) -> str:
    canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f'"{digest}"'


def _component_payload(
    component: dict[str, Any],
    *,
    score_publishable: bool = True,
) -> dict[str, Any]:
    block = component.get("block") if isinstance(component.get("block"), dict) else {}
    tile_profile = [dict(item) for item in component.get("tile_profile") or [] if isinstance(item, dict)]
    if not tile_profile:
        tile_profile = [dict(item) for item in component.get("tiles") or [] if isinstance(item, dict)]
    passed = sum(1 for item in tile_profile if str(item.get("estado") or item.get("state") or "") in {"ok", "on"})
    failed = sum(1 for item in tile_profile if str(item.get("estado") or item.get("state") or "") in {"no", "off"})
    blind = sum(
        1
        for item in tile_profile
        if str(item.get("estado") or item.get("state") or "") in {"sin_evidencia", "blind"}
    )
    key = str(component.get("key") or component.get("component") or block.get("name") or "")
    refs = []
    for item in block.get("refs") or []:
        if not isinstance(item, dict):
            continue
        refs.append(
            {
                "ref": str(item.get("ref") or ""),
                "component": key,
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("snippet") or ""),
            }
        )
    return {
        "key": key,
        "label": str(component.get("label") or key.replace("_", " ").title()),
        "status": str(component.get("status") or "unknown"),
        "score": (
            _number(component.get("score")) if score_publishable else None
        ),
        "raw_score": _number(component.get("score")),
        "score_publishable": score_publishable,
        "max_score": _number(component.get("scale")),
        "confidence": str(component.get("confidence") or block.get("confidence") or "unknown"),
        "summary": str(component.get("resumen") or ""),
        "verdict": str(component.get("veredicto") or ""),
        "message": str(component.get("message") or ""),
        "detected_content": str(component.get("detected_content") or block.get("content") or ""),
        "coverage_status": str(block.get("coverage_status") or "unknown"),
        "tile_summary": {
            "passed": int(component.get("lit") if component.get("lit") is not None else passed),
            "failed": int(component.get("off") if component.get("off") is not None else failed),
            "insufficient_evidence": int(component.get("blind") if component.get("blind") is not None else blind),
            "total": len(tile_profile),
        },
        "tiles": tile_profile,
        "evidence_refs": refs,
    }


def _evidence_references(report: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for component in report.get("components") or []:
        if not isinstance(component, dict):
            continue
        key = str(component.get("key") or component.get("component") or "")
        block = component.get("block") if isinstance(component.get("block"), dict) else {}
        for item in block.get("refs") or []:
            if not isinstance(item, dict):
                continue
            row = {
                "ref": str(item.get("ref") or ""),
                "component": key,
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("snippet") or ""),
            }
            identity = (row["ref"], row["component"], row["url"], row["snippet"])
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(row)
    return rows


def _brand_payload(report: dict[str, Any]) -> dict[str, str]:
    url = str(report.get("url") or "")
    domain = (urlparse(url if "://" in url else f"https://{url}").hostname or "").removeprefix("www.")
    return {
        "name": str(report.get("brand_name") or domain),
        "url": url,
        "domain": domain,
    }


def _public_failure_message(code: str) -> str:
    if code == "process_restarted":
        return "The scan was interrupted by a process restart."
    if code == "scan_start_failed":
        return "The scanner job could not be started."
    if code == "acquisition_gate_blocked_for_client":
        return "The scan could not continue because its evidence gate was blocked."
    return "The scan failed during execution."


def _number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _string_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("label") or value.get("key") or value.get("message") or "").strip()
    return str(value or "").strip()


def _first_component_summary(components: list[dict[str, Any]]) -> str:
    for item in components:
        summary = str(item.get("summary") or "").strip()
        if summary:
            return summary
    return ""
