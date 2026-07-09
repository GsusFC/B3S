"""Live B3S scan runner: capture → envelope → evidence flow → SV9 → report.

Runs in a background thread per scan. In-flight status lives in memory (a
process restart forgets running scans); finished reports are files in the
report store.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from src.url_validator import validate_url
from web.report_store import new_scan_id, save_report

_SCANS: dict[str, dict[str, Any]] = {}
_SCAN_EVENTS: dict[str, threading.Event] = {}
_LOCK = threading.Lock()

_PHASES = (
    ("capture", "Capture: owned pages, Exa, GitHub proof, SearchAPI fallback, visual evidence"),
    ("interpret", "Evidence pack → shortlists → gated LLM interpretation"),
    ("score", "Tile signals → SV9 components → score"),
    ("report", "Coverage + report assembly"),
)


def normalize_url(raw: str) -> str:
    valid, normalized_or_error = validate_url(raw)
    if not valid:
        raise ValueError(normalized_or_error)
    return normalized_or_error


def default_brand_name(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    label = host.split(".")[0] if host else "brand"
    return label.capitalize()


def start_scan(url: str, brand_name: str = "", *, allow_degraded_fallback: bool = False) -> str:
    url = normalize_url(url)
    brand_name = (brand_name or "").strip() or default_brand_name(url)
    scan_id = new_scan_id()
    with _LOCK:
        _SCANS[scan_id] = {
            "id": scan_id,
            "url": url,
            "brand_name": brand_name,
            "state": "running",
            "phase": "capture",
            "phases": [{"key": key, "label": label, "state": "pending"} for key, label in _PHASES],
            "acquisition": [],
            "acquisition_gate": {"state": "pending", "issues": [], "warnings": [], "fallbacks": []},
            "allow_degraded_fallback": bool(allow_degraded_fallback),
            "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        _SCAN_EVENTS[scan_id] = threading.Event()
    thread = threading.Thread(
        target=_run,
        args=(scan_id, url, brand_name, bool(allow_degraded_fallback)),
        daemon=True,
    )
    thread.start()
    return scan_id


def scan_status(scan_id: str) -> dict[str, Any] | None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        return dict(status) if status else None


def approve_degraded_scan(scan_id: str) -> dict[str, Any] | None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None:
            return None
        gate = status.get("acquisition_gate") if isinstance(status.get("acquisition_gate"), dict) else {}
        if status.get("state") != "blocked" or not gate.get("can_continue"):
            return {"state": str(status.get("state") or "unknown"), "approved": False, "reason": "not_continuable"}
        approved_gate = _approve_acquisition_gate(gate, decision_source="user")
        status["acquisition_gate"] = approved_gate
        status["state"] = "running"
        status["phase"] = "interpret"
        _set_phase_locked(status, "interpret", "pending")
        event = _SCAN_EVENTS.get(scan_id)
    if event:
        event.set()
    return {"state": "running", "approved": True, "acquisition_gate": approved_gate}


def cancel_scan(scan_id: str) -> dict[str, Any] | None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status is None:
            return None
        gate = status.get("acquisition_gate") if isinstance(status.get("acquisition_gate"), dict) else {}
        if isinstance(gate, dict):
            gate = dict(gate)
            gate["user_decision"] = "cancelled"
            gate["state"] = "cancelled"
            status["acquisition_gate"] = gate
        status["state"] = "cancelled"
        status["phase"] = "capture"
        _mark_pending_phases_locked(status, "cancelled")
        event = _SCAN_EVENTS.pop(scan_id, None)
    if event:
        event.set()
    return {"state": "cancelled", "cancelled": True, "acquisition_gate": gate}


def _set_phase(scan_id: str, key: str, state: str) -> None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if not status:
            return
        if state == "running":
            status["phase"] = key
        _set_phase_locked(status, key, state)


def _set_phase_locked(status: dict[str, Any], key: str, state: str) -> None:
    for phase in status["phases"]:
        if phase["key"] == key:
            phase["state"] = state


def _mark_pending_phases_locked(status: dict[str, Any], state: str) -> None:
    for phase in status.get("phases") or []:
        if phase.get("state") in {"pending", "running"}:
            phase["state"] = state


def _run(scan_id: str, url: str, brand_name: str, allow_degraded_fallback: bool) -> None:
    try:
        _set_phase(scan_id, "capture", "running")
        snapshot = _capture_snapshot(scan_id, url, brand_name)
        _set_phase(scan_id, "capture", "done")
        gate = _build_acquisition_gate(
            snapshot.get("acquisition_steps") if isinstance(snapshot, dict) else {},
            allow_degraded_fallback=allow_degraded_fallback,
        )
        if gate["state"] == "blocked" and allow_degraded_fallback and gate.get("can_continue"):
            gate = _approve_acquisition_gate(gate, decision_source="preapproved")
        snapshot["acquisition_gate"] = gate
        _set_acquisition_gate(scan_id, gate)
        if gate["state"] == "blocked":
            if not _wait_for_acquisition_decision(scan_id):
                return
            with _LOCK:
                status = _SCANS.get(scan_id) or {}
                current_gate = status.get("acquisition_gate") if isinstance(status.get("acquisition_gate"), dict) else gate
                if status.get("state") == "cancelled":
                    return
            snapshot["acquisition_gate"] = current_gate

        _set_phase(scan_id, "interpret", "running")
        from scripts.sv9_flow_sv9_shadow_eval import build_flow_sv9_shadow_eval

        envelope = {"snapshot": snapshot, "source_run_id": snapshot["run"]["id"]}
        payload = build_flow_sv9_shadow_eval(envelope, include_full=True)
        payload = _attach_sv9_editorial(payload)
        payload["acquisition_gate"] = snapshot.get("acquisition_gate") or gate
        payload["acquisition_artifacts"] = _acquisition_artifacts_from_snapshot(snapshot)
        _set_phase(scan_id, "interpret", "done")
        _set_phase(scan_id, "score", "done")

        _set_phase(scan_id, "report", "running")
        report = _compose_report(scan_id, url, brand_name, payload)
        save_report(report)
        _set_phase(scan_id, "report", "done")
        with _LOCK:
            _SCANS[scan_id]["state"] = "done"
            _SCAN_EVENTS.pop(scan_id, None)
    except Exception as exc:  # surface the failure to the UI, never die silently
        traceback.print_exc()
        with _LOCK:
            status = _SCANS.get(scan_id)
            if status:
                status["state"] = "error"
                status["error"] = f"{type(exc).__name__}: {exc}"
            _SCAN_EVENTS.pop(scan_id, None)


def _set_acquisition_gate(scan_id: str, gate: dict[str, Any]) -> None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if not status:
            return
        status["acquisition_gate"] = gate
        if gate.get("state") == "blocked":
            status["state"] = "blocked"
            status["phase"] = "capture"


def _wait_for_acquisition_decision(scan_id: str) -> bool:
    with _LOCK:
        event = _SCAN_EVENTS.get(scan_id)
    if event is None:
        return False
    event.wait()
    with _LOCK:
        status = _SCANS.get(scan_id)
        return bool(status and status.get("state") != "cancelled")


def _build_acquisition_gate(
    acquisition_steps: Any,
    *,
    allow_degraded_fallback: bool = False,
) -> dict[str, Any]:
    steps = acquisition_steps if isinstance(acquisition_steps, dict) else {}
    normalized = {str(source): _step_payload(step) for source, step in steps.items()}
    _ensure_configured_step_markers(normalized)

    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []

    web_step = normalized.get("web")
    if web_step is None or _is_failure_status(_step_status(web_step)):
        issues.append(
            _issue(
                source="web",
                code="web_capture_failed",
                severity="blocker",
                message="Owned web capture failed; scoring would lack the primary evidence base.",
                step=web_step,
            )
        )
    else:
        web_details = web_step.get("details") if isinstance(web_step.get("details"), dict) else {}
        if web_details.get("cookie_banner_suspected") is True:
            warnings.append(
                _issue(
                    source="web",
                    code="web_cookie_banner_suspected",
                    severity="warning",
                    message="Owned web capture may contain unresolved cookie-banner text.",
                    step=web_step,
                )
            )

    exa_step = normalized.get("exa")
    exa_status = _step_status(exa_step)
    exa_failed = exa_step is None or _is_failure_status(exa_status)
    searchapi_step = normalized.get("searchapi")
    searchapi_status = _step_status(searchapi_step)
    searchapi_available = _searchapi_available_for_fallback(searchapi_step)
    if exa_failed:
        fallback = {
            "source": "searchapi",
            "for_source": "exa",
            "available": searchapi_available,
            "approved": False,
            "status": searchapi_status or "missing",
            "reason": "vertical external-proof fallback for Exa failure",
        }
        fallbacks.append(fallback)
        issues.append(
            _issue(
                source="exa",
                code="exa_failed",
                severity="blocker",
                message="Exa failed; external proof acquisition is incomplete.",
                step=exa_step,
                fallback="searchapi" if searchapi_available else "",
                can_fallback=searchapi_available,
            )
        )
    elif exa_status in {"empty", "partial"}:
        warnings.append(
            _issue(
                source="exa",
                code=f"exa_{exa_status}",
                severity="warning",
                message="Exa returned limited external proof; score should declare reduced acquisition coverage.",
                step=exa_step,
            )
        )

    if _is_failure_status(searchapi_status):
        target = issues if exa_failed else warnings
        target.append(
            _issue(
                source="searchapi",
                code="searchapi_failed",
                severity="blocker" if exa_failed else "warning",
                message="SearchAPI fallback failed.",
                step=searchapi_step,
            )
        )

    github_status = _step_status(normalized.get("github"))
    if _is_failure_status(github_status):
        warnings.append(
            _issue(
                source="github",
                code="github_proof_failed",
                severity="warning",
                message="GitHub proof acquisition failed; continuing without repository proof.",
                step=normalized.get("github"),
            )
        )

    visual_status = _step_status(normalized.get("visual_acquisition"))
    visual_detail = _step_detail(normalized.get("visual_acquisition")).lower()
    if _is_failure_status(visual_status) or "blocked" in visual_detail or "not_interpretable" in visual_detail:
        warnings.append(
            _issue(
                source="visual_acquisition",
                code="visual_acquisition_limited",
                severity="warning",
                message="Visual acquisition was unavailable or obstructed; visual evidence remains limited.",
                step=normalized.get("visual_acquisition"),
            )
        )

    blocking = [item for item in issues if item.get("severity") == "blocker"]
    can_continue = bool(blocking) and all(bool(item.get("can_fallback")) for item in blocking)
    state = "blocked" if blocking else ("warning" if warnings else "pass")
    return {
        "version": "b3s-acquisition-gate-v1",
        "state": state,
        "can_continue": can_continue,
        "allow_degraded_fallback": bool(allow_degraded_fallback),
        "issues": issues,
        "warnings": warnings,
        "fallbacks": fallbacks,
        "limitations": _gate_limitations(issues, warnings),
        "user_decision": None,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def _approve_acquisition_gate(gate: dict[str, Any], *, decision_source: str) -> dict[str, Any]:
    approved = dict(gate)
    approved["state"] = "degraded_approved"
    approved["user_decision"] = "continue_degraded"
    approved["decision_source"] = decision_source
    approved["decided_at"] = datetime.now(timezone.utc).isoformat()
    approved["fallbacks"] = [
        {**fallback, "approved": bool(fallback.get("available"))}
        for fallback in gate.get("fallbacks") or []
        if isinstance(fallback, dict)
    ]
    return approved


def _step_payload(step: Any) -> dict[str, Any]:
    if step is None:
        return {}
    if isinstance(step, dict):
        return step
    if hasattr(step, "to_payload"):
        return dict(step.to_payload())
    if hasattr(step, "to_dict"):
        return dict(step.to_dict())
    if dataclasses.is_dataclass(step):
        return dataclasses.asdict(step)
    return dict(vars(step))


def _ensure_configured_step_markers(steps: dict[str, dict[str, Any]]) -> None:
    from src.config import EXA_API_KEY, SEARCHAPI_API_KEY

    if not EXA_API_KEY and "exa" not in steps:
        steps["exa"] = {
            "source": "exa",
            "status": "missing_key",
            "cache_status": "missing",
            "eligible": True,
            "error": "EXA_API_KEY not set",
            "details": {"reason": "EXA_API_KEY not set"},
        }
    if not SEARCHAPI_API_KEY and "searchapi" not in steps:
        steps["searchapi"] = {
            "source": "searchapi",
            "status": "missing_key",
            "cache_status": "missing",
            "eligible": False,
            "error": "SEARCHAPI_API_KEY not set",
            "details": {"reason": "SEARCHAPI_API_KEY not set"},
        }


def _step_status(step: Any) -> str:
    if not isinstance(step, dict):
        return ""
    return str(step.get("status") or "").strip().lower()


def _step_detail(step: Any) -> str:
    if not isinstance(step, dict):
        return ""
    details = step.get("details") if isinstance(step.get("details"), dict) else {}
    reason = str(details.get("reason") or step.get("error") or "").strip()
    parts = [reason] if reason else []
    for key in ("blocked_reason", "cookie_banner_snippet"):
        value = str(details.get(key) or "").strip()
        if value:
            parts.append(f"{key}: {value}")
    if parts:
        return "; ".join(parts)
    for key in ("failed_intents", "no_result_intents"):
        values = details.get(key)
        if isinstance(values, list) and values:
            return f"{key}: {', '.join(str(value) for value in values)}"
    return ""


def _is_failure_status(status: str) -> bool:
    return status in {
        "error",
        "failed",
        "failure",
        "missing",
        "missing_key",
        "blocked",
        "timeout",
        "acquisition_failed",
    }


def _searchapi_available_for_fallback(step: Any) -> bool:
    from src.config import SEARCHAPI_API_KEY

    if not SEARCHAPI_API_KEY:
        return False
    status = _step_status(step)
    if not status:
        return True
    return status not in {"disabled", "missing_key", "error", "failed", "failure", "blocked", "timeout"}


def _issue(
    *,
    source: str,
    code: str,
    severity: str,
    message: str,
    step: dict[str, Any] | None,
    fallback: str = "",
    can_fallback: bool = False,
) -> dict[str, Any]:
    payload = {
        "source": source,
        "code": code,
        "severity": severity,
        "message": message,
        "status": _step_status(step) or "missing",
        "detail": _step_detail(step),
        "can_fallback": bool(can_fallback),
    }
    if fallback:
        payload["fallback"] = fallback
    return payload


def _gate_limitations(issues: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> list[str]:
    codes = []
    for item in issues + warnings:
        code = str(item.get("code") or "").strip()
        if code and code not in codes:
            codes.append(f"acquisition_gate:{code}")
    return codes


def _capture_snapshot(scan_id: str, url: str, brand_name: str) -> dict[str, Any]:
    from src.config import BRAND3_VISUAL_SIGNATURE_SCAN_ENABLED
    from src.services import brand_service as service

    raw = service.collect_raw_inputs(
        store=None,
        run_id=None,
        brand_name=brand_name,
        url=url,
        refresh=False,
        use_social=False,
        use_competitors=False,
        effective_brand_url_builder=service._effective_brand_url,
        context_evidence_builder=service._context_evidence_items,
        run_input_sources={"context", "web", "exa", "github", "searchapi"},
        social_collector=service._collect_social_with_budget,
        context_collector_cls=service.ContextCollector,
        web_collector_cls=service.WebCollector,
        exa_collector_cls=service.ExaCollector,
    )

    raw_inputs: list[dict[str, Any]] = []
    for source, data in (
        ("context", raw.context_data),
        ("web", raw.web_data),
        ("exa", raw.exa_data),
        ("github", raw.github_data),
        ("searchapi", raw.searchapi_data),
    ):
        if data is None:
            continue
        if str(getattr(data, "status", "") or "") in {"skipped", "error"}:
            continue
        raw_inputs.append({"source": source, "payload": _to_payload(data)})

    visual_rows, visual_step = _capture_visual_evidence(
        service=service,
        enabled=BRAND3_VISUAL_SIGNATURE_SCAN_ENABLED,
        url=url,
        brand_name=brand_name,
        web_data=raw.web_data,
        content_web=raw.web_data,
    )
    raw_inputs.extend(visual_rows)

    acquisition_steps: dict[str, Any] = {}
    acquisition_rows: list[dict[str, str]] = []
    for source, step in (raw.acquisition_steps or {}).items():
        payload = _to_payload(step)
        if str(source) == "web":
            _annotate_web_cookie_banner_signal(raw.web_data, payload)
        acquisition_steps[source] = payload
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        acquisition_rows.append(
            {
                "source": str(source),
                "status": str(payload.get("status") or ""),
                "detail": str(details.get("reason") or payload.get("error") or ""),
            }
        )
    if visual_step:
        acquisition_steps["visual_acquisition"] = visual_step
        acquisition_rows.append(
            {
                "source": "visual_acquisition",
                "status": str(visual_step.get("status") or ""),
                "detail": str((visual_step.get("details") or {}).get("reason") or ""),
            }
        )
    with _LOCK:
        status = _SCANS.get(scan_id)
        if status:
            status["acquisition"] = sorted(acquisition_rows, key=lambda row: row["source"])

    return {
        "run": {"brand_name": brand_name, "id": int(datetime.now(timezone.utc).timestamp()), "url": url},
        "raw_inputs": raw_inputs,
        "acquisition_steps": acquisition_steps,
        "features": [],
    }


def _annotate_web_cookie_banner_signal(web_data: Any | None, step_payload: dict[str, Any]) -> None:
    snippet = _cookie_banner_snippet_from_web_data(web_data)
    if not snippet:
        return
    details = step_payload.get("details") if isinstance(step_payload.get("details"), dict) else {}
    details["cookie_banner_suspected"] = True
    details["cookie_banner_snippet"] = snippet
    step_payload["details"] = details


def _cookie_banner_snippet_from_web_data(web_data: Any | None) -> str:
    if web_data is None:
        return ""
    try:
        payload = _to_payload(web_data)
    except Exception:
        return ""
    candidates = [
        str(payload.get("title") or ""),
        str(payload.get("body_text") or ""),
        str(payload.get("markdown") or payload.get("markdown_content") or payload.get("content") or payload.get("text") or ""),
    ]
    text = " ".join(part for part in candidates if part).strip()
    if not text:
        return ""
    lowered = text.lower()
    exact_banner_markers = (
        "valoramos tu privacidad",
        "we value your privacy",
        "we use cookies",
        "usamos cookies",
        "utilizamos cookies",
    )
    consent_markers = ("cookie", "cookies", "consent", "privacidad", "privacy")
    action_markers = ("aceptar", "accept", "rechazar", "reject", "preferencias", "preferences", "consent")
    if any(marker in lowered for marker in exact_banner_markers) or (
        any(marker in lowered for marker in consent_markers) and any(marker in lowered for marker in action_markers)
    ):
        return " ".join(text.split())[:220]
    return ""


def _capture_visual_evidence(
    *,
    service: Any,
    enabled: bool,
    url: str,
    brand_name: str,
    web_data: Any | None,
    content_web: Any | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not enabled:
        return [], {
            "status": "skipped",
            "details": {"reason": "visual_acquisition_disabled"},
        }

    screenshot_capture = _capture_screenshot(service=service, url=url)
    try:
        result = service._run_visual_signature_shadow(
            enabled=True,
            store=None,
            run_id=None,
            brand_name=brand_name,
            url=url,
            web_data=web_data,
            content_web=content_web,
            screenshot_capture=screenshot_capture,
        )
    except Exception as exc:  # visual acquisition enriches the run; it must not own the run
        return [
            {
                "source": "screenshot_capture",
                "payload": {
                    "version": "screenshot_capture_v1",
                    "url": url,
                    "content_source": "b3s_live_scan",
                    "skip_visual_analysis": False,
                    "capture": screenshot_capture,
                },
            }
        ], {"status": "error", "details": {"reason": str(exc)}}
    visual_evidence = result.get("visual_evidence_packet")
    rows: list[dict[str, Any]] = [
        {
            "source": "screenshot_capture",
            "payload": {
                "version": "screenshot_capture_v1",
                "url": url,
                "content_source": "b3s_live_scan",
                "skip_visual_analysis": False,
                "capture": screenshot_capture,
            },
        }
    ]
    if isinstance(visual_evidence, dict):
        rows.append(
            {
                "source": "visual_acquisition",
                "payload": {
                    "schema_version": "visual-signature-persistence-1",
                    "run_id": None,
                    "brand_name": brand_name,
                    "website_url": url,
                    "run_metadata": {
                        "source": "b3s_live_scan",
                        "visual_signature_scan_status": result.get("visual_signature_scan_status"),
                        "visual_signature_score": result.get("visual_signature_score"),
                        "interpretation_status": result.get("interpretation_status"),
                        "agreement_level": result.get("agreement_level"),
                    },
                    "visual_signature_scan": result.get("visual_signature_scan"),
                    "visual_evidence_packet": visual_evidence,
                    "visual_signature_evidence": result.get("visual_signature_evidence"),
                    "raw_visual_signature_payload": result.get("payload"),
                    "vision_payload": result.get("vision"),
                },
            }
        )

    status = str(result.get("status") or "unknown")
    details = {
        "reason": _visual_acquisition_detail(result=result, screenshot_capture=screenshot_capture),
    }
    blocked_reason = _visual_obstruction_detail(result)
    if blocked_reason:
        details["blocked_reason"] = blocked_reason
    cookie_snippet = _visual_cookie_banner_snippet(result=result, screenshot_capture=screenshot_capture)
    if cookie_snippet:
        details["cookie_banner_suspected"] = True
        details["cookie_banner_snippet"] = cookie_snippet
    return rows, {"status": status, "details": details}


def _capture_screenshot(*, service: Any, url: str) -> dict[str, Any]:
    try:
        screenshot_data, limitation = service._take_screenshot_with_budget(
            url,
            timeout_seconds=int(os.environ.get("BRAND3_VISUAL_SCREENSHOT_TIMEOUT_SECONDS", "20")),
        )
        return service._screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data=screenshot_data,
            limitation=limitation,
        )
    except Exception as exc:  # visual evidence must never block text scoring
        return service._screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data={"error": str(exc)},
            limitation="error",
        )


def _visual_acquisition_detail(*, result: dict[str, Any], screenshot_capture: dict[str, Any]) -> str:
    evidence = result.get("visual_evidence_packet")
    if isinstance(evidence, dict):
        capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
        capture_status = str(capture.get("status") or "").strip()
        if capture_status:
            return f"visual_evidence_packet:{capture_status}"
        return "visual_evidence_packet"
    if not screenshot_capture.get("success"):
        return str(screenshot_capture.get("error_type") or screenshot_capture.get("error") or "screenshot_unavailable")
    return str(result.get("interpretation_status") or result.get("status") or "visual_acquisition_unavailable")


def _visual_obstruction_detail(result: dict[str, Any]) -> str:
    evidence = result.get("visual_evidence_packet")
    if not isinstance(evidence, dict):
        return ""
    capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
    obstruction = capture.get("obstruction") if isinstance(capture.get("obstruction"), dict) else {}
    if not obstruction.get("present"):
        return ""
    obstruction_type = str(obstruction.get("type") or "unknown")
    severity = str(obstruction.get("severity") or "unknown")
    signals = [str(signal) for signal in obstruction.get("signals") or [] if str(signal).strip()]
    suffix = f"; signals: {', '.join(signals[:4])}" if signals else ""
    return f"obstruction:{obstruction_type}; severity:{severity}{suffix}"


def _visual_cookie_banner_snippet(*, result: dict[str, Any], screenshot_capture: dict[str, Any]) -> str:
    evidence = result.get("visual_evidence_packet")
    if isinstance(evidence, dict):
        capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
        obstruction = capture.get("obstruction") if isinstance(capture.get("obstruction"), dict) else {}
        signals = " ".join(str(signal) for signal in obstruction.get("signals") or [])
        obstruction_type = str(obstruction.get("type") or "")
        if "cookie" in f"{obstruction_type} {signals}".lower() or "privacy" in f"{obstruction_type} {signals}".lower():
            return " ".join(f"{obstruction_type} {signals}".split())[:220]
    metadata = screenshot_capture.get("metadata") if isinstance(screenshot_capture.get("metadata"), dict) else {}
    dismissal = metadata.get("cookie_banner_dismissal") if isinstance(metadata.get("cookie_banner_dismissal"), dict) else {}
    if dismissal.get("success") is True:
        return "cookie_banner_dismissal:success"
    return ""


def _acquisition_artifacts_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for row in snapshot.get("raw_inputs") or []:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if source == "screenshot_capture":
            capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
            artifacts.append(_screenshot_artifact(capture))
        elif source == "visual_acquisition":
            evidence = payload.get("visual_evidence_packet") if isinstance(payload.get("visual_evidence_packet"), dict) else {}
            capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
            obstruction = capture.get("obstruction") if isinstance(capture.get("obstruction"), dict) else {}
            artifacts.append(
                {
                    "source": "visual_acquisition",
                    "kind": "visual_evidence_packet",
                    "status": str(capture.get("status") or ""),
                    "first_fold_evaluable": capture.get("first_fold_evaluable"),
                    "obstruction": {
                        "present": obstruction.get("present"),
                        "type": str(obstruction.get("type") or ""),
                        "severity": str(obstruction.get("severity") or ""),
                        "signals": [str(item) for item in obstruction.get("signals") or []][:8],
                    },
                }
            )
    return [artifact for artifact in artifacts if artifact]


def _screenshot_artifact(capture: dict[str, Any]) -> dict[str, Any]:
    screenshot_path = str(capture.get("screenshot_path") or "").strip()
    screenshot_url = str(capture.get("screenshot_url") or "").strip()
    metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
    artifact = {
        "source": "screenshot_capture",
        "kind": "screenshot",
        "status": str(capture.get("status") or ""),
        "success": capture.get("success") is True,
        "provider": str(capture.get("source") or ""),
        "screenshot_url": screenshot_url,
        "screenshot_path": screenshot_path,
        "public_url": _public_screenshot_url(screenshot_path=screenshot_path, screenshot_url=screenshot_url),
        "metadata": metadata,
    }
    return artifact if screenshot_path or screenshot_url or artifact["status"] else {}


def _public_screenshot_url(*, screenshot_path: str, screenshot_url: str) -> str:
    from pathlib import Path
    from urllib.parse import urlparse

    candidate = screenshot_path
    if not candidate and screenshot_url.startswith("file://"):
        candidate = urlparse(screenshot_url).path
    if not candidate:
        return ""
    try:
        path = Path(candidate).resolve()
        root = Path("data/screenshots").resolve()
        path.relative_to(root)
    except Exception:
        return ""
    return f"/artifacts/screenshots/{path.name}"


def _to_payload(data: Any) -> dict[str, Any]:
    if hasattr(data, "to_dict"):
        return data.to_dict()
    if dataclasses.is_dataclass(data):
        return dataclasses.asdict(data)
    return dict(vars(data))


_COMPONENT_ORDER = (
    "core_purpose",
    "magnetism",
    "value_proposition",
    "personality",
    "brand_idea",
    "attributes",
    "values",
    "mission",
    "vision",
    "coherencia",
)


def _resolve_scan_tile_profile(
    detail: dict[str, Any],
    *,
    lit: int,
    off: int,
    blind: int,
    scale_hint: int,
) -> Any:
    tile_profile = detail.get("tile_profile")
    if tile_profile is not None:
        if isinstance(tile_profile, list):
            if not tile_profile:
                return {
                    "lit": lit,
                    "off": off,
                    "blind": blind,
                    "scale": scale_hint,
                }
            if lit or off or blind:
                lit_tiles = sum(
                    1
                    for tile in tile_profile
                    if str((tile or {}).get("estado") or "") == "ok"
                )
                off_tiles = sum(
                    1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "no"
                )
                blind_tiles = sum(
                    1
                    for tile in tile_profile
                    if str((tile or {}).get("estado") or "") == "sin_evidencia"
                )
                if (lit_tiles, off_tiles, blind_tiles) != (lit, off, blind):
                    return {
                        "lit": lit,
                        "off": off,
                        "blind": blind,
                        "scale": scale_hint,
                    }
        return tile_profile

    fallback_tiles = detail.get("tiles")
    if isinstance(fallback_tiles, list):
        if not fallback_tiles:
            return {
                "lit": lit,
                "off": off,
                "blind": blind,
                "scale": scale_hint,
            }
        if lit or off or blind:
            lit_tiles = sum(
                1 for tile in fallback_tiles if str((tile or {}).get("estado") or "") == "ok"
            )
            off_tiles = sum(
                1 for tile in fallback_tiles if str((tile or {}).get("estado") or "") == "no"
            )
            blind_tiles = sum(
                1
                for tile in fallback_tiles
                if str((tile or {}).get("estado") or "") == "sin_evidencia"
            )
            if (lit_tiles, off_tiles, blind_tiles) != (lit, off, blind):
                return {
                    "lit": lit,
                    "off": off,
                    "blind": blind,
                    "scale": scale_hint,
                }
        return fallback_tiles

    return {
        "lit": lit,
        "off": off,
        "blind": blind,
        "scale": scale_hint,
    }


def _tile_name_map(component_key: str) -> dict[str, str]:
    from src.sv9.rubric import COMPONENTS as RUBRIC_COMPONENTS

    meta = RUBRIC_COMPONENTS.get(component_key) or {}
    return {str(tile.get("id") or ""): str(tile.get("name") or "") for tile in meta.get("tiles") or []}


def _tile_counts_from_profile(tile_profile: list[dict[str, Any]]) -> tuple[int, int, int]:
    lit = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "ok")
    off = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "no")
    blind = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "sin_evidencia")
    return lit, off, blind


def _failing_tiles_from_profile(component_key: str, tile_profile: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from src.sv9.language_guard import spanish_tile_contexto, spanish_tile_motivo

    tile_names = _tile_name_map(component_key)
    failing_tiles: list[dict[str, Any]] = []
    for tile in tile_profile:
        if not isinstance(tile, dict) or tile.get("estado") == "ok":
            continue
        failing_tiles.append(
            {
                "id": str(tile.get("id") or tile.get("tile_id") or ""),
                "name": str(tile_names.get(str(tile.get("id") or tile.get("tile_id") or "")) or ""),
                "estado": str(tile.get("estado") or ""),
                "motivo": spanish_tile_motivo(tile.get("motivo"), estado=tile.get("estado")),
                "contexto_requerido": spanish_tile_contexto(tile.get("contexto_requerido")),
                "evidencia": str(tile.get("evidencia") or ""),
            }
        )
    return failing_tiles


def _sv9_editorial_enabled() -> bool:
    return os.environ.get("B3S_SV9_EDITORIAL_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _component_needs_editorial_message(component_key: str, detail: dict[str, Any]) -> bool:
    from src.sv9.language_guard import spanish_component_verdict

    if str(detail.get("message") or "").strip():
        return False
    tile_profile = detail.get("tile_profile") if isinstance(detail.get("tile_profile"), list) else []
    verdict = spanish_component_verdict(component_key, detail.get("veredicto"), tile_profile)
    return not verdict or verdict.startswith("Síntesis automática")


def _attach_sv9_editorial(
    payload: dict[str, Any],
    *,
    llm: Any | None = None,
    build_editorial_fn: Any | None = None,
) -> dict[str, Any]:
    if not _sv9_editorial_enabled():
        return payload
    sv9 = payload.get("sv9") if isinstance(payload.get("sv9"), dict) else {}
    result = sv9.get("result") if isinstance(sv9.get("result"), dict) else {}
    components = result.get("components") if isinstance(result.get("components"), dict) else {}
    if not components:
        return payload
    structured = result.get("editorial_v3_1") if isinstance(result.get("editorial_v3_1"), dict) else {}
    structured_components = (
        structured.get("components") if isinstance(structured.get("components"), dict) else {}
    )
    ordered_keys = [
        key for key in _COMPONENT_ORDER
        if key in components and isinstance(components.get(key), dict)
    ]
    ordered_keys.extend(
        key
        for key, detail in components.items()
        if key not in ordered_keys and isinstance(detail, dict)
    )
    needed = [
        key
        for key in ordered_keys
        for detail in [components.get(key)]
        if isinstance(detail, dict) and _component_needs_editorial_message(str(key), detail)
    ]
    if not structured_components:
        needed = ordered_keys
    else:
        for key in ordered_keys:
            if key not in structured_components and key not in needed:
                needed.append(key)
    if not needed:
        return payload
    try:
        if llm is None:
            from src.config import SV9_EDITORIAL_MODEL
            from src.features.llm_analyzer import LLMAnalyzer

            llm = LLMAnalyzer(model=SV9_EDITORIAL_MODEL)
        if not getattr(llm, "api_key", None):
            return payload
        if build_editorial_fn is None:
            from src.sv9.editorial import build_editorial as build_editorial_fn

        editorial = build_editorial_fn(
            result,
            llm=llm,
            component_keys=needed,
            include_executive_reading=True,
        )
    except Exception as exc:
        sv9["editorial"] = {"status": "failed", "error": str(exc)}
        return payload

    messages = editorial.get("component_messages") if isinstance(editorial, dict) else {}
    for key, message in (messages or {}).items():
        detail = components.get(key)
        if isinstance(detail, dict) and str(message or "").strip():
            detail["message"] = str(message).strip()
    reading = editorial.get("executive_reading") if isinstance(editorial, dict) else None
    if str(reading or "").strip():
        result["executive_reading"] = str(reading).strip()
    structured = editorial.get("structured") if isinstance(editorial, dict) else None
    structured_components = {}
    if isinstance(structured, dict):
        result["editorial_v3_1"] = structured
        structured_components = (
            structured.get("components") if isinstance(structured.get("components"), dict) else {}
        )
    sv9["editorial"] = {
        "status": "attached",
        "mode": "v3_1_structured",
        "requested_components": needed,
        "message_components": sorted(str(key) for key in (messages or {}).keys()),
        "executive_reading": bool(str(reading or "").strip()),
        "structured_schema_version": str((structured or {}).get("schema_version") or ""),
        "structured_components": sorted(str(key) for key in structured_components.keys()),
    }
    return payload


def _compose_report(scan_id: str, url: str, brand_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    from src.sv9.language_guard import (
        spanish_component_summary,
        spanish_component_verdict,
        spanish_generated_text,
    )

    flow = payload.get("flow") or {}
    candidate = flow.get("candidate") or {}
    interpretation = candidate.get("interpretation") or {}
    pack = candidate.get("evidence_pack") or {}
    records = {r.get("ref"): r for r in pack.get("evidence") or [] if isinstance(r, dict)}
    debug = flow.get("interpretation_debug") or {}
    coverage = debug.get("evidence_coverage") or {}
    coverage_blocks = coverage.get("blocks") or {}
    sv9 = payload.get("sv9") or {}
    acquisition_gate = payload.get("acquisition_gate") if isinstance(payload.get("acquisition_gate"), dict) else {}
    acquisition_artifacts = payload.get("acquisition_artifacts") if isinstance(payload.get("acquisition_artifacts"), list) else []

    blocks = []
    for name, block in sorted((interpretation.get("blocks") or {}).items()):
        if not isinstance(block, dict):
            continue
        refs = [str(r) for r in (interpretation.get("evidence_refs") or {}).get(name) or []]
        provenance = block.get("detection_provenance") if isinstance(block.get("detection_provenance"), dict) else {}
        blocks.append(
            {
                "name": name,
                "detected": block.get("detected") is True,
                "confidence": str(block.get("confidence") or ""),
                "content": str(block.get("content") or ""),
                "rejected_content": str(block.get("rejected_content") or ""),
                "provenance_source": str(provenance.get("final_source") or ""),
                "coverage_status": str((coverage_blocks.get(name) or {}).get("status") or ""),
                "refs": [
                    {
                        "ref": ref,
                        "url": str((records.get(ref) or {}).get("url") or ""),
                        "snippet": " ".join(str((records.get(ref) or {}).get("content") or "").split())[:280],
                    }
                    for ref in refs
                ],
            }
        )

    absences = []
    attempts = []
    for record in pack.get("evidence") or []:
        if not isinstance(record, dict):
            continue
        evidence_type = str(record.get("evidence_type") or "")
        if evidence_type.startswith("acquisition.absence."):
            absences.append(
                {
                    "block": evidence_type.rsplit(".", 1)[-1],
                    "url": str(record.get("url") or ""),
                    "content": str(record.get("content") or ""),
                }
            )
        elif evidence_type.startswith("acquisition.attempt."):
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            attempts.append(
                {
                    "provider": str(metadata.get("provider") or record.get("source") or ""),
                    "intent": str(metadata.get("intent") or ""),
                    "status": str(metadata.get("status") or ""),
                    "detail": str(metadata.get("query") or metadata.get("error") or ""),
                }
            )

    blocks_by_name = {block["name"]: block for block in blocks}
    result = sv9.get("result") if isinstance(sv9.get("result"), dict) else {}
    result_components = result.get("components") if isinstance(result.get("components"), dict) else {}
    structured_editorial = (
        result.get("editorial_v3_1") if isinstance(result.get("editorial_v3_1"), dict) else {}
    )
    structured_editorial_components = (
        structured_editorial.get("components")
        if isinstance(structured_editorial.get("components"), dict)
        else {}
    )
    from src.sv9.rubric import COMPONENTS as RUBRIC_COMPONENTS

    components = []
    known = tuple(dict.fromkeys(_COMPONENT_ORDER + tuple((sv9.get("components") or {}).keys())))
    for name in known:
        component = (sv9.get("components") or {}).get(name)
        if not isinstance(component, dict):
            continue
        detail = result_components.get(name) if isinstance(result_components.get(name), dict) else {}
        meta = RUBRIC_COMPONENTS.get(name) or {}
        scale = int(detail.get("scale") or meta.get("scale") or 0)
        raw_tile_profile = detail.get("tile_profile")
        tile_profile = (
            [tile for tile in raw_tile_profile if isinstance(tile, dict)]
            if isinstance(raw_tile_profile, list)
            else []
        )
        if tile_profile:
            lit_count, off_count, blind_count = _tile_counts_from_profile(tile_profile)
        else:
            lit_tiles = set(component.get("lit_tiles") or detail.get("lit_tiles") or [])
            off_tiles = set(component.get("off_tiles") or detail.get("off_tiles") or [])
            blind_tiles = set(component.get("blind_spot_tiles") or detail.get("blind_spot_tiles") or [])
            lit_count = len(lit_tiles)
            off_count = len(off_tiles)
            blind_count = len(blind_tiles)
            resolved = _resolve_scan_tile_profile(
                detail,
                lit=lit_count,
                off=off_count,
                blind=blind_count,
                scale_hint=scale,
            )
            tile_profile = list(resolved) if isinstance(resolved, list) else []

        tile_states = []
        tile_names = _tile_name_map(name)
        if tile_profile:
            for tile in tile_profile:
                tile_id = str(tile.get("id") or tile.get("tile_id") or "")
                estado = str(tile.get("estado") or "")
                state = {"ok": "on", "no": "off", "sin_evidencia": "blind"}.get(estado)
                if state:
                    tile_states.append(
                        {"id": tile_id, "name": str(tile_names.get(tile_id) or ""), "state": state}
                    )
        else:
            lit_tiles = set(component.get("lit_tiles") or detail.get("lit_tiles") or [])
            off_tiles = set(component.get("off_tiles") or detail.get("off_tiles") or [])
            blind_tiles = set(component.get("blind_spot_tiles") or detail.get("blind_spot_tiles") or [])
            for tile in meta.get("tiles") or []:
                tile_id = str(tile.get("id") or "")
                if tile_id in lit_tiles:
                    state = "on"
                elif tile_id in off_tiles:
                    state = "off"
                elif tile_id in blind_tiles:
                    state = "blind"
                else:
                    continue
                tile_states.append({"id": tile_id, "name": str(tile.get("name") or ""), "state": state})

        failing_tiles = _failing_tiles_from_profile(name, tile_profile)
        detected_content = str(detail.get("detected_content") or "").strip()
        message = spanish_generated_text(detail.get("message"))
        editorial_component = (
            structured_editorial_components.get(name)
            if isinstance(structured_editorial_components.get(name), dict)
            else {}
        )
        components.append(
            {
                "key": name,
                "component": name,
                "label": str(meta.get("label") or name),
                "question": str(meta.get("question") or ""),
                "level_zero": str(meta.get("level_zero") or ""),
                "score": component.get("score"),
                "scale": scale,
                "status": str(component.get("status") or ""),
                "confidence": str(detail.get("confidence") or ""),
                "tile_profile": list(tile_profile) if isinstance(tile_profile, list) else [],
                "resumen": spanish_component_summary(
                    name,
                    detected_content,
                    tile_profile or {"lit": lit_count, "off": off_count, "blind": blind_count, "scale": scale},
                ),
                "detected_content": detected_content,
                "veredicto": spanish_component_verdict(name, detail.get("veredicto"), tile_profile),
                "message": message,
                "editorial": editorial_component,
                "points": detail.get("points"),
                "evaluation_model": detail.get("evaluation_model"),
                "evidence": list(detail.get("evidence") or []),
                "source_policy_notes": list(detail.get("source_policy_notes") or []),
                "lit": lit_count,
                "off": off_count,
                "blind": blind_count,
                "tile_states": tile_states,
                "tiles": failing_tiles,
                "block": blocks_by_name.get(name),
            }
        )

    gap_key = str(result.get("most_painful_gap") or "")
    detected_count = sum(1 for block in blocks if block["detected"])
    limitations = [str(item) for item in candidate.get("limitations") or []]
    for item in acquisition_gate.get("limitations") or []:
        value = str(item)
        if value and value not in limitations:
            limitations.append(value)

    return {
        "id": scan_id,
        "brand_name": brand_name,
        "url": url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "score": sv9.get("brand3_score"),
        "base_average": sv9.get("base_average"),
        "reliability_status": str(sv9.get("reliability_status") or "shadow"),
        "not_detected": [str(item) for item in sv9.get("not_detected") or []],
        "most_painful_gap": gap_key,
        "most_painful_gap_label": str((RUBRIC_COMPONENTS.get(gap_key) or {}).get("label") or gap_key),
        "immediate_margin": result.get("immediate_margin"),
        "total_blind_spots": result.get("total_blind_spots"),
        "executive_reading": structured_editorial.get("executive_reading") or result.get("executive_reading"),
        "editorial": structured_editorial,
        "detected_count": detected_count,
        "block_count": len(blocks),
        "components": components,
        "blocks": blocks,
        "acquisition_gate": acquisition_gate,
        "acquisition_artifacts": acquisition_artifacts,
        "coverage_acquisition": coverage.get("acquisition") or {},
        "absences": absences,
        "attempts": attempts,
        "limitations": limitations,
        "raw": payload,
    }
