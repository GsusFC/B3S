"""Live B3S scan runner: capture → envelope → evidence flow → SV9 → report.

Runs in a background thread per scan. In-flight status lives in memory (a
process restart forgets running scans); finished reports are files in the
report store.
"""

from __future__ import annotations

import dataclasses
import threading
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from web.report_store import new_scan_id, save_report

_SCANS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()

_PHASES = (
    ("capture", "Capture: owned pages, Exa, GitHub proof, SearchAPI fallback"),
    ("interpret", "Evidence pack → shortlists → gated LLM interpretation"),
    ("score", "Tile signals → SV9 components → score"),
    ("report", "Coverage + report assembly"),
)


def normalize_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("URL is required")
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if not parsed.netloc or "." not in parsed.netloc:
        raise ValueError(f"'{raw}' does not look like a brand URL")
    return value


def default_brand_name(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    label = host.split(".")[0] if host else "brand"
    return label.capitalize()


def start_scan(url: str, brand_name: str = "") -> str:
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
            "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    thread = threading.Thread(target=_run, args=(scan_id, url, brand_name), daemon=True)
    thread.start()
    return scan_id


def scan_status(scan_id: str) -> dict[str, Any] | None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        return dict(status) if status else None


def _set_phase(scan_id: str, key: str, state: str) -> None:
    with _LOCK:
        status = _SCANS.get(scan_id)
        if not status:
            return
        if state == "running":
            status["phase"] = key
        for phase in status["phases"]:
            if phase["key"] == key:
                phase["state"] = state


def _run(scan_id: str, url: str, brand_name: str) -> None:
    try:
        _set_phase(scan_id, "capture", "running")
        snapshot = _capture_snapshot(scan_id, url, brand_name)
        _set_phase(scan_id, "capture", "done")

        _set_phase(scan_id, "interpret", "running")
        from scripts.sv9_flow_sv9_shadow_eval import build_flow_sv9_shadow_eval

        envelope = {"snapshot": snapshot, "source_run_id": snapshot["run"]["id"]}
        payload = build_flow_sv9_shadow_eval(envelope, include_full=True)
        _set_phase(scan_id, "interpret", "done")
        _set_phase(scan_id, "score", "done")

        _set_phase(scan_id, "report", "running")
        report = _compose_report(scan_id, url, brand_name, payload)
        save_report(report)
        _set_phase(scan_id, "report", "done")
        with _LOCK:
            _SCANS[scan_id]["state"] = "done"
    except Exception as exc:  # surface the failure to the UI, never die silently
        traceback.print_exc()
        with _LOCK:
            status = _SCANS.get(scan_id)
            if status:
                status["state"] = "error"
                status["error"] = f"{type(exc).__name__}: {exc}"


def _capture_snapshot(scan_id: str, url: str, brand_name: str) -> dict[str, Any]:
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

    acquisition_steps: dict[str, Any] = {}
    acquisition_rows: list[dict[str, str]] = []
    for source, step in (raw.acquisition_steps or {}).items():
        payload = _to_payload(step)
        acquisition_steps[source] = payload
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        acquisition_rows.append(
            {
                "source": str(source),
                "status": str(payload.get("status") or ""),
                "detail": str(details.get("reason") or payload.get("error") or ""),
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


def _compose_report(scan_id: str, url: str, brand_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    flow = payload.get("flow") or {}
    candidate = flow.get("candidate") or {}
    interpretation = candidate.get("interpretation") or {}
    pack = candidate.get("evidence_pack") or {}
    records = {r.get("ref"): r for r in pack.get("evidence") or [] if isinstance(r, dict)}
    debug = flow.get("interpretation_debug") or {}
    coverage = debug.get("evidence_coverage") or {}
    coverage_blocks = coverage.get("blocks") or {}
    sv9 = payload.get("sv9") or {}

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
    from src.sv9.rubric import COMPONENTS as RUBRIC_COMPONENTS

    components = []
    known = tuple(dict.fromkeys(_COMPONENT_ORDER + tuple((sv9.get("components") or {}).keys())))
    for name in known:
        component = (sv9.get("components") or {}).get(name)
        if not isinstance(component, dict):
            continue
        detail = result_components.get(name) if isinstance(result_components.get(name), dict) else {}
        meta = RUBRIC_COMPONENTS.get(name) or {}
        tile_names = {tile.get("id"): tile.get("name") for tile in meta.get("tiles") or []}
        failing_tiles = []
        for tile in detail.get("tile_profile") or []:
            if not isinstance(tile, dict) or tile.get("estado") == "ok":
                continue
            failing_tiles.append(
                {
                    "id": str(tile.get("id") or ""),
                    "name": str(tile_names.get(tile.get("id")) or ""),
                    "estado": str(tile.get("estado") or ""),
                    "motivo": str(tile.get("motivo") or ""),
                    "evidencia": str(tile.get("evidencia") or ""),
                }
            )
        components.append(
            {
                "key": name,
                "label": str(meta.get("label") or name),
                "question": str(meta.get("question") or ""),
                "level_zero": str(meta.get("level_zero") or ""),
                "score": component.get("score"),
                "scale": detail.get("scale") or meta.get("scale"),
                "status": str(component.get("status") or ""),
                "confidence": str(detail.get("confidence") or ""),
                "resumen": str(detail.get("detected_content") or ""),
                "veredicto": str(detail.get("veredicto") or ""),
                "lit": len(component.get("lit_tiles") or []),
                "off": len(component.get("off_tiles") or []),
                "blind": len(component.get("blind_spot_tiles") or []),
                "tiles": failing_tiles,
                "block": blocks_by_name.get(name),
            }
        )

    gap_key = str(result.get("most_painful_gap") or "")
    detected_count = sum(1 for block in blocks if block["detected"])
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
        "detected_count": detected_count,
        "block_count": len(blocks),
        "components": components,
        "blocks": blocks,
        "coverage_acquisition": coverage.get("acquisition") or {},
        "absences": absences,
        "attempts": attempts,
        "limitations": [str(item) for item in candidate.get("limitations") or []],
        "raw": payload,
    }
