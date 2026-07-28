from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from typing import Any


DEFAULT_DEPLOY_ORIGIN = "https://b3s.fly.dev"
DEFAULT_SCANNER_API_URL = f"{DEFAULT_DEPLOY_ORIGIN}/api/v1"
# Backwards-compatible alias for callers that still expect an origin.
DEFAULT_DEPLOY_BASE = DEFAULT_DEPLOY_ORIGIN
CRITICAL_STABILITY_FIELDS = (
    "magnetism_score",
    "coherence_score",
    "quadrant",
    "earned_magnetism_score",
    "expressive_magnetism_score",
    "evidence_duty_status",
    "research_pack_hash",
    "raw_inputs_hash",
)


def read_env_value(name: str, *, env_path: str = ".env") -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        for line in open(env_path, "r", encoding="utf-8").read().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        return ""
    return ""


def normalize_scanner_api_url(value: str | None) -> str:
    """Return one canonical v1 base while accepting a legacy origin input."""

    candidate = str(value or DEFAULT_SCANNER_API_URL).strip().rstrip("/")
    if candidate.endswith("/api/v1"):
        return candidate
    return f"{candidate}/api/v1"


def configured_scanner_api_url(
    value: str | None = None,
    *,
    env_path: str = ".env",
) -> str:
    """Resolve explicit value, environment/.env configuration, then production."""

    configured = str(value or "").strip() or read_env_value(
        "B3S_SCANNER_API_URL",
        env_path=env_path,
    )
    return normalize_scanner_api_url(configured)


def scanner_api_origin(value: str | None) -> str:
    """Return the web origin for consumers that also call non-API routes."""

    api_url = normalize_scanner_api_url(value)
    return api_url[: -len("/api/v1")]


def scanner_api_endpoint(base_url: str, path: str) -> str:
    return f"{normalize_scanner_api_url(base_url)}/{path.lstrip('/')}"


def request_json(
    url: str,
    *,
    method: str = "GET",
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, headers=headers, data=data, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    if not isinstance(body, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return body


def create_scan(
    *,
    base_url: str,
    url: str,
    lang: str,
    token: str,
    timeout: int,
) -> dict[str, Any]:
    return request_json(
        scanner_api_endpoint(base_url, "scanner"),
        method="POST",
        token=token,
        payload={"url": url, "lang": lang},
        timeout=timeout,
    )


def poll_scan_ready(
    *,
    base_url: str,
    scan_id: int,
    lang: str,
    token: str,
    poll_interval: float,
    max_wait_seconds: int,
    timeout: int,
) -> dict[str, Any]:
    deadline = time.time() + max_wait_seconds
    last_status: dict[str, Any] = {}
    while time.time() < deadline:
        last_status = request_json(
            scanner_api_endpoint(base_url, f"scanner/{scan_id}?lang={lang}"),
            token=token,
            timeout=timeout,
        )
        if last_status.get("status") in {"ready", "failed"}:
            return last_status
        time.sleep(poll_interval)
    return {
        **last_status,
        "status": last_status.get("status") or "timeout",
        "error_message": f"Timed out after {max_wait_seconds}s",
    }


def fetch_scan_bundle(
    *,
    base_url: str,
    scan_id: int,
    token: str,
    timeout: int,
) -> dict[str, Any]:
    status = request_json(
        scanner_api_endpoint(base_url, f"scanner/{scan_id}"),
        token=token,
        timeout=timeout,
    )
    result = request_json(
        scanner_api_endpoint(base_url, f"scanner/{scan_id}/result?full=true"),
        token=token,
        timeout=timeout,
    )
    audit_snapshot = request_json(
        scanner_api_endpoint(base_url, f"scanner/{scan_id}/audit-snapshot?full=true"),
        token=token,
        timeout=timeout,
    )
    return {
        "status": status,
        "result": result,
        "audit_snapshot": audit_snapshot,
    }


def extract_probe_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    status = _as_dict(bundle.get("status"))
    result = _as_dict(bundle.get("result"))
    audit_snapshot = _as_dict(bundle.get("audit_snapshot"))

    normalized_payload = _as_dict(_as_dict(result.get("debug")).get("normalized_payload"))
    metrics = _as_dict(normalized_payload.get("metrics"))
    magnetism_scoring = _as_dict(metrics.get("magnetism_scoring_context"))
    audit_run = _as_dict(audit_snapshot.get("run"))
    audit_debug = _as_dict(audit_snapshot.get("debug"))
    debug_run = _as_dict(audit_debug.get("run"))
    acquisition = _as_dict(_as_dict(_as_dict(debug_run.get("audit")).get("acquisition")).get("provenance"))
    quality = _as_dict(acquisition.get("quality"))

    return {
        "scan_id": result.get("id") or status.get("id"),
        "source_run_id": _as_dict(result.get("audit")).get("source_run_id") or audit_snapshot.get("source_run_id"),
        "status": status.get("status"),
        "phase": status.get("phase"),
        "scan_mode": _as_dict(status.get("scan_mode")).get("mode") or _as_dict(result.get("scan_mode")).get("mode"),
        "magnetism_score": _as_number(_as_dict(result.get("scores")).get("magnetism")),
        "coherence_score": _as_number(_as_dict(result.get("scores")).get("coherence")),
        "quadrant": _as_dict(result.get("scores")).get("quadrant"),
        "composite_score": _as_number(audit_run.get("composite_score")),
        "earned_magnetism_score": _as_number(magnetism_scoring.get("earned_magnetism_score")),
        "expressive_magnetism_score": _as_number(magnetism_scoring.get("expressive_magnetism_score")),
        "evidence_duty_status": magnetism_scoring.get("evidence_duty_status"),
        "reasoning_preview": _reasoning_preview(magnetism_scoring.get("reasoning")),
        "quality_label": quality.get("label"),
        "quality_score": _as_number(quality.get("score")),
        "research_pack_hash": _short_hash(normalized_payload.get("research_pack")),
        "raw_inputs_hash": _short_hash(audit_debug.get("raw_inputs")),
        "analyst_tldr_hash": _short_hash(normalized_payload.get("analyst_tldr_validated")),
        "result_tldr_hash": _short_hash(result.get("tldr_brand3")),
    }


def compare_probe_summaries(
    summaries: list[dict[str, Any]],
    *,
    critical_fields: tuple[str, ...] = CRITICAL_STABILITY_FIELDS,
) -> dict[str, Any]:
    if not summaries:
        return {
            "stable": True,
            "critical_stable": True,
            "field_count": 0,
            "changing_fields": [],
            "critical_changes": [],
        }
    field_names = sorted({key for row in summaries for key in row.keys() if key not in {"scan_id", "source_run_id"}})
    changing_fields: list[dict[str, Any]] = []
    critical_changes: list[dict[str, Any]] = []
    for field in field_names:
        values = [row.get(field) for row in summaries]
        unique = _unique_values(values)
        if len(unique) <= 1:
            continue
        change = {
            "field": field,
            "values": [
                {
                    "scan_id": row.get("scan_id"),
                    "source_run_id": row.get("source_run_id"),
                    "value": row.get(field),
                }
                for row in summaries
            ],
        }
        changing_fields.append(change)
        if field in critical_fields:
            critical_changes.append(change)
    return {
        "stable": not changing_fields,
        "critical_stable": not critical_changes,
        "field_count": len(field_names),
        "changing_fields": changing_fields,
        "critical_changes": critical_changes,
    }


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        if "." in str(value):
            return float(value)
        return int(value)
    except (TypeError, ValueError):
        return None


def _short_hash(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _reasoning_preview(value: Any) -> str:
    if isinstance(value, str):
        return _compact_text(value, 220)
    if isinstance(value, dict):
        preview = value.get("preview")
        if isinstance(preview, str) and preview.strip():
            return _compact_text(preview, 220)
    return ""


def _compact_text(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _unique_values(values: list[Any]) -> list[Any]:
    unique: list[Any] = []
    for value in values:
        if any(existing == value for existing in unique):
            continue
        unique.append(value)
    return unique
