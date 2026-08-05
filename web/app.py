"""B3S lab web app: submit a brand URL, watch the scoring run, read the evidence."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from urllib.parse import quote
import uuid

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import select_autoescape
from pydantic import ValidationError

from src.build_info import current_build_sha
from src.services.scanner_evidence_comparison import (
    canonical_enforcement_mode,
    selected_report_for_display,
)
from src.services.scanner_content_sampling import content_sampling_from_report
from src.services.scanner_score_publication import score_publication_from_report
from web.api_v1 import install_scanner_api
from web.api_v1.errors import ApiError
from web.api_v1.models import EvidenceScoringRecoveryReviewCreateRequest
from web.api_v1.service import create_evidence_scoring_recovery_review
from web.report_store import (
    domain_key,
    evidence_claim_tile_ledger_for_domain,
    evidence_scoring_memory_preview_for_domain,
    list_reports,
    list_reports_for_domain,
    load_report,
)
from web.report_view_model import build_report_view_model
from web.scan_runner import approve_degraded_scan, cancel_scan, recover_interrupted_scans, scan_status, start_scan
from web.scoring_store import backfill_reports, dashboard as scoring_dashboard
from web.vault_reviewer_session import (
    VAULT_REVIEWER_COOKIE,
    VAULT_REVIEWER_SESSION_MAX_AGE,
    VaultReviewerSession,
    authenticate_vault_reviewer,
    csrf_matches,
    issue_vault_reviewer_session,
    read_vault_reviewer_session,
    safe_vault_review_path,
    vault_reviewer_enabled,
)
from src.sv9.language_guard import (
    spanish_component_summary,
    spanish_component_verdict,
    spanish_generated_text,
    spanish_tile_contexto,
    spanish_tile_motivo,
)
from src.sv9.rubric import COMPONENTS as SV9_COMPONENTS


_LOG = logging.getLogger(__name__)


_VAULT_IMPACT_PRESENTATION = {
    "tile_improved": ("baldosa mejorada", "ok"),
    "tile_worsened": ("baldosa empeorada", "bad"),
    "evidence_reinforced": ("evidencia reforzada", "ok"),
    "evidence_weakened": ("evidencia debilitada", "bad"),
    "evidence_changed": ("evidencia cambiada", "warn"),
    "evidence_sources_changed": ("fuentes cambiadas", "warn"),
    "acquisition_gap": ("hueco de adquisición", "warn"),
    "unvalidated_negative": ("negativo sin validar", "bad"),
}

_VAULT_REVIEW_CHANNEL_PRESENTATION = {
    "scoring_recovery_review": "recuperación semántica",
    "claim_tile_review": "claim → baldosa",
    "claim_corroboration_review": "corroboración del claim",
    "evidence_gap_review": "hueco de evidencia",
}

_VAULT_REVIEW_DECISION_REASON_CODES = {
    "accepted": "tile_contract_satisfied",
    "disputed": "tile_contract_uncertain",
    "rejected": "tile_contract_not_satisfied",
}


def _initialize_runtime() -> None:
    env_file = Path(".env")
    if env_file.is_file():
        from scripts.sv9_flow_shadow_run import _load_env_file

        _load_env_file(str(env_file))
    recover_interrupted_scans()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _initialize_runtime()
    yield


app = FastAPI(title="B3S — Brand Evidence Lab", lifespan=_lifespan)
install_scanner_api(app)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.autoescape = select_autoescape(("html", "j2"))
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


def _component_display_text(component: dict[str, Any], *, prefer_summary: bool = False) -> str:
    message = str(component.get("message") or "").strip()
    if message:
        return message
    verdict = str(component.get("veredicto") or "").strip()
    summary = str(component.get("resumen") or "").strip()
    if prefer_summary and summary and not component.get("resumen_is_fallback"):
        return summary
    if verdict:
        return verdict
    if summary and not component.get("resumen_is_fallback"):
        return summary
    return summary


def _brand_profile(domain: str) -> dict:
    raw_reports = list_reports_for_domain(domain)
    selected, classified_reports, history_state = selected_report_for_display(raw_reports)
    reports = []
    for source_report in classified_reports:
        report = _sanitize_report_language(source_report)
        report["score_publication"] = score_publication_from_report(report)
        reports.append(report)
    selected_id = str((selected or {}).get("id") or "")
    current = next((report for report in reports if str(report.get("id") or "") == selected_id), None)
    latest_attempt = reports[0] if reports else None
    normalized_domain = domain_key(domain) or domain

    components = list((current or {}).get("components") or [])
    detected = [component for component in components if component.get("status") == "scored"]
    proof_urls: list[str] = []
    for component in components:
        block = component.get("block") or {}
        for ref in block.get("refs") or []:
            url = ref.get("url")
            if url and url not in proof_urls:
                proof_urls.append(url)

    purpose = next((component for component in components if component.get("key") == "core_purpose"), {})
    value = next((component for component in components if component.get("key") == "value_proposition"), {})
    return {
        "domain": normalized_domain,
        "display_name": (current or {}).get("brand_name") or normalized_domain,
        "url": (current or {}).get("url") or f"https://{normalized_domain}",
        "current": current,
        "latest_attempt": latest_attempt,
        "reports": reports,
        "history_state": history_state,
        "enforcement_mode": canonical_enforcement_mode(),
        "summary": _component_display_text(purpose, prefer_summary=True) or _component_display_text(value, prefer_summary=True),
        "outcome": _component_display_text(value) or _component_display_text(purpose),
        "proof_urls": proof_urls[:6],
        "detected_count": len(detected),
        "component_count": len(components),
        "not_detected": (current or {}).get("not_detected") or [],
        "visual_module": _moodboard_from_report(current) if current else {"available": False, "images": []},
        "vault_memory": _vault_tile_memory_profile(normalized_domain),
    }


def _vault_tile_memory_profile(domain: str) -> dict[str, Any]:
    """Build the read-only Vault view from existing durable projections."""

    if os.environ.get("BRAND3_ENVIRONMENT", "").strip().lower() != "vault":
        return {"enabled": False}
    try:
        preview = evidence_scoring_memory_preview_for_domain(domain)
        ledger = evidence_claim_tile_ledger_for_domain(domain)
    except Exception:
        _LOG.exception(
            "failed to build vault tile memory view",
            extra={"domain": domain_key(domain)},
        )
        return {
            "enabled": True,
            "available": False,
            "status": "unavailable",
            "message": "La memoria persistente no está disponible temporalmente.",
        }

    scoring = preview.get("scoring") if isinstance(preview.get("scoring"), dict) else {}
    reviewed_shadow = preview.get("reviewed_shadow") if isinstance(preview.get("reviewed_shadow"), dict) else {}
    reviewed_scoring = reviewed_shadow.get("scoring") if isinstance(reviewed_shadow.get("scoring"), dict) else {}
    candidate_evolution = (
        preview.get("tile_evolution")
        if isinstance(preview.get("tile_evolution"), dict)
        else {}
    )
    reviewed_evolution = (
        reviewed_shadow.get("tile_evolution")
        if isinstance(reviewed_shadow.get("tile_evolution"), dict)
        else {}
    )
    evolution = (
        reviewed_evolution
        if isinstance(reviewed_evolution.get("changes"), list)
        else candidate_evolution
    )
    evolution_summary = evolution.get("summary") if isinstance(evolution.get("summary"), dict) else {}
    trajectory = evolution.get("score_trajectory") if isinstance(evolution.get("score_trajectory"), dict) else {}
    ledger_summary = ledger.get("summary") if isinstance(ledger.get("summary"), dict) else {}
    reviewed_memory = ledger.get("reviewed_memory") if isinstance(ledger.get("reviewed_memory"), dict) else {}
    reviewed_mapping_summary = (
        reviewed_memory.get("summary") if isinstance(reviewed_memory.get("summary"), dict) else {}
    )
    mapping_count = int(ledger_summary.get("mapping_count") or 0)
    reviewed_memory_available = reviewed_memory.get("available") is True
    accepted_mapping_count = int(reviewed_mapping_summary.get("accepted_mapping_count") or 0)
    pending_mapping_count = (
        int(reviewed_mapping_summary.get("pending_mapping_count") or 0)
        if reviewed_memory_available
        else mapping_count
    )

    recovery_candidates_by_tile: dict[str, list[dict[str, Any]]] = {}
    for candidate in preview.get("recovery_review_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        tile = candidate.get("tile") if isinstance(candidate.get("tile"), dict) else {}
        tile_key = str(tile.get("tile_key") or "")
        if tile_key:
            recovery_candidates_by_tile.setdefault(tile_key, []).append(candidate)

    latest_mapping_series_id = str(ledger.get("latest_mapping_series_id") or "")
    current_mappings = [
        row
        for row in ledger.get("mappings") or []
        if isinstance(row, dict)
        and (
            not latest_mapping_series_id
            or str(row.get("mapping_series_id") or "")
            == latest_mapping_series_id
        )
    ]

    recovery_review = (
        preview.get("recovery_review")
        if isinstance(preview.get("recovery_review"), dict)
        else {}
    )
    recovery_review_summary = (
        recovery_review.get("summary")
        if isinstance(recovery_review.get("summary"), dict)
        else {}
    )
    protected_candidate_count = sum(
        isinstance(candidate, dict)
        for candidate in preview.get("recovery_review_candidates") or []
    )
    pending_protected_candidate_count = int(
        recovery_review_summary.get("pending_count")
        if recovery_review_summary.get("pending_count") is not None
        else protected_candidate_count
    )

    review_items = []
    for row in evolution.get("changes") or []:
        if not isinstance(row, dict):
            continue
        impact_kind = str(row.get("impact_kind") or "unknown")
        impact_label, tone = _VAULT_IMPACT_PRESENTATION.get(
            impact_kind,
            (impact_kind.replace("_", " "), "warn"),
        )
        review_items.append(
            _vault_score_review_item(
                row,
                impact_label=impact_label,
                impact_tone=tone,
                recovery_candidates=recovery_candidates_by_tile.get(
                    str(row.get("tile_key") or ""),
                    [],
                ),
                current_mappings=current_mappings,
            )
        )

    review_summary = {
        "total_count": len(review_items),
        "score_affecting_count": sum(
            item["review_priority"] == "score_affecting"
            for item in review_items
        ),
        "evidence_quality_count": sum(
            item["review_priority"] == "evidence_quality"
            for item in review_items
        ),
        "ready_count": sum(item["queue_state"] == "ready" for item in review_items),
        "preparation_count": sum(
            item["queue_state"] == "preparation_required" for item in review_items
        ),
        "blocked_count": sum(item["queue_state"] == "blocked" for item in review_items),
        "resolved_count": sum(item["queue_state"] == "resolved" for item in review_items),
        "protected_candidate_count": protected_candidate_count,
        "pending_protected_candidate_count": (
            pending_protected_candidate_count
        ),
    }

    preview_summary = preview.get("summary") if isinstance(preview.get("summary"), dict) else {}
    return {
        "enabled": True,
        "available": bool(preview.get("report_count")),
        "status": "shadow",
        "runtime_effect": False,
        "authority": False,
        "report_count": int(preview.get("report_count") or 0),
        "memory_version": str(preview.get("memory_version") or ""),
        "tile_memory_version": str(candidate_evolution.get("tile_memory_version") or ""),
        "persistence": preview.get("persistence") or {},
        "summary": {
            "tile_count": int(evolution_summary.get("tile_count") or 0),
            "stable_tile_count": int(evolution_summary.get("stable_tile_count") or 0),
            "changed_tile_count": int(evolution_summary.get("changed_tile_count") or 0),
            "score_affecting_change_count": int(evolution_summary.get("score_affecting_change_count") or 0),
            "evidence_quality_change_count": int(evolution_summary.get("evidence_quality_change_count") or 0),
            "pending_review_count": int(evolution_summary.get("pending_review_count") or 0),
            "incompatible_report_count": int(evolution_summary.get("incompatible_report_count") or 0),
            "accepted_evidence_count": int(preview_summary.get("accepted_evidence_count") or 0),
            "candidate_recovery_count": len(preview.get("recoveries") or []),
            "explicit_negative_conflict_count": len(preview.get("conflicts") or []),
        },
        "trajectory": trajectory,
        "candidate_scoring": {
            "current_score": scoring.get("current_score"),
            "preview_score": scoring.get("preview_score"),
            "score_delta": scoring.get("score_delta"),
        },
        "reviewed_scoring": {
            "current_score": reviewed_scoring.get("current_score"),
            "preview_score": reviewed_scoring.get("preview_score"),
            "score_delta": reviewed_scoring.get("score_delta"),
        },
        "ledger": {
            "stored": bool((ledger.get("persistence") or {}).get("stored")),
            "mapping_count": mapping_count,
            "current_mapping_count": int(ledger_summary.get("current_series_mapping_count") or 0),
            "claim_variant_count": int(ledger_summary.get("claim_variant_count") or 0),
            "tile_count": int(ledger_summary.get("tile_count") or 0),
            "reviewed_memory_available": reviewed_memory_available,
            "accepted_mapping_count": accepted_mapping_count,
            "pending_mapping_count": pending_mapping_count,
        },
        "review_queue": {
            "read_only": True,
            "summary": review_summary,
            "items": review_items,
        },
    }


def _vault_score_review_item(
    change: dict[str, Any],
    *,
    impact_label: str,
    impact_tone: str,
    recovery_candidates: list[dict[str, Any]],
    current_mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify one material change without inventing a review path."""

    tile_key = str(change.get("tile_key") or "")
    validation_channel = str(change.get("validation_channel") or "unknown")
    validation_state = str(change.get("validation_state") or "pending_review")
    if validation_state != "pending_review":
        return {
            "tile_key": tile_key,
            "impact_label": impact_label,
            "impact_tone": impact_tone,
            "state_change": (
                f"{change.get('previous_state') or '—'} → "
                f"{change.get('current_state') or '—'}"
            ),
            "channel_label": _VAULT_REVIEW_CHANNEL_PRESENTATION.get(
                validation_channel,
                validation_channel.replace("_", " "),
            ),
            "queue_state": "resolved",
            "queue_label": "decisión registrada",
            "queue_tone": "ok",
            "next_action": "La decisión semántica ya está reflejada en la sombra revisada.",
            "validation_state": validation_state,
            "review_priority": str(
                change.get("review_priority") or "evidence_quality"
            ),
        }

    queue_state = "blocked"
    queue_label = "bloqueada"
    queue_tone = "bad"
    next_action = "No existe un canal de revisión seguro para este cambio."
    if validation_channel == "scoring_recovery_review":
        if recovery_candidates:
            queue_state = "ready"
            queue_label = "lista para revisar"
            queue_tone = "ok"
            next_action = (
                "Revisar el candidato protegido y persistir una decisión "
                "en el journal de recuperación."
            )
        else:
            next_action = "Falta el candidato de recuperación vinculado a esta baldosa."
    elif validation_channel == "claim_tile_review":
        relevant_mappings = _vault_relevant_claim_tile_mappings(
            change,
            current_mappings=current_mappings,
        )
        if relevant_mappings:
            queue_state = "preparation_required"
            queue_label = "preparar paquete"
            queue_tone = "warn"
            next_action = (
                "Registrar el paquete atómico actual y revisar el mapping "
                "claim → baldosa."
            )
        else:
            next_action = (
                "Falta un mapping claim → baldosa reproducible; no se puede "
                "validar el cambio todavía."
            )
    elif validation_channel == "claim_corroboration_review":
        queue_state = "preparation_required"
        queue_label = "preparar paquete"
        queue_tone = "warn"
        next_action = (
            "Preparar un paquete claim-scoped para decidir si la fuente "
            "refuerza, sustituye o debilita la evidencia existente."
        )
    elif validation_channel == "evidence_gap_review":
        next_action = (
            "Recapturar evidencia reproducible antes de aceptar el empeoramiento."
        )

    return {
        "tile_key": tile_key,
        "impact_label": impact_label,
        "impact_tone": impact_tone,
        "state_change": (
            f"{change.get('previous_state') or '—'} → "
            f"{change.get('current_state') or '—'}"
        ),
        "channel_label": _VAULT_REVIEW_CHANNEL_PRESENTATION.get(
            validation_channel,
            validation_channel.replace("_", " "),
        ),
        "queue_state": queue_state,
        "queue_label": queue_label,
        "queue_tone": queue_tone,
        "next_action": next_action,
        "validation_state": validation_state,
        "review_priority": str(
            change.get("review_priority") or "evidence_quality"
        ),
    }


def _vault_relevant_claim_tile_mappings(
    change: dict[str, Any],
    *,
    current_mappings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_polarity = {
        "ok": "supports",
        "no": "weakens",
        "sin_evidencia": "insufficient_evidence",
    }.get(str(change.get("current_state") or ""))
    relevant_sources = {
        str(source_id)
        for source_id in (
            change.get("added_source_evidence_ids")
            or change.get("current_source_evidence_ids")
            or []
        )
        if str(source_id)
    }
    if not expected_polarity or not relevant_sources:
        return []
    return [
        mapping
        for mapping in current_mappings
        if str(mapping.get("tile_key") or "")
        == str(change.get("tile_key") or "")
        and str(mapping.get("polarity") or "") == expected_polarity
        and str(mapping.get("source_evidence_id") or "")
        in relevant_sources
    ]


def _vault_reviewer_session(
    request: Request,
) -> VaultReviewerSession | None:
    return read_vault_reviewer_session(request.cookies.get(VAULT_REVIEWER_COOKIE))


def _vault_reviewer_profile(domain: str) -> dict[str, Any]:
    """Expose exact recovery candidates only inside an authenticated view."""

    normalized = domain_key(domain)
    if not normalized:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        preview = evidence_scoring_memory_preview_for_domain(normalized)
    except Exception:
        _LOG.exception(
            "failed to build protected vault reviewer view",
            extra={"domain": normalized},
        )
        return {
            "domain": normalized,
            "available": False,
            "message": "El journal protegido no está disponible temporalmente.",
            "items": [],
            "summary": {"candidate_count": 0, "pending_count": 0, "reviewed_count": 0},
        }

    persistence = preview.get("persistence") if isinstance(preview.get("persistence"), dict) else {}
    journal_ready = persistence.get("stored") is True and persistence.get("review_journal") == "postgres"
    review = preview.get("recovery_review") if isinstance(preview.get("recovery_review"), dict) else {}
    brand = preview.get("brand") if isinstance(preview.get("brand"), dict) else {}
    evaluated_by_case = {
        str(row.get("case_id") or ""): row
        for row in review.get("evaluated") or []
        if isinstance(row, dict) and str(row.get("case_id") or "")
    }
    items = []
    for candidate in preview.get("recovery_review_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        case_id = str(candidate.get("case_id") or "")
        subject_id = str(candidate.get("candidate_fingerprint") or "")
        tile = candidate.get("tile") if isinstance(candidate.get("tile"), dict) else {}
        evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
        contexts = [
            context
            for context in candidate.get("contexts") or []
            if isinstance(context, dict)
        ]
        review_scope = (
            "current_direct_relation"
            if any(
                context.get("review_scope")
                == "current_direct_relation"
                for context in contexts
            )
            else "historical_recovery"
        )
        evaluated = evaluated_by_case.get(case_id) or {}
        decision = str(evaluated.get("decision") or "pending")
        items.append(
            {
                "case_id": case_id,
                "subject_id": subject_id,
                "fingerprint_short": subject_id[:16],
                "tile_key": str(tile.get("tile_key") or ""),
                "tile_name": str(tile.get("name") or ""),
                "tile_condition": str(tile.get("condition") or ""),
                "tile_contract": (
                    tile.get("evidence_contract") if isinstance(tile.get("evidence_contract"), dict) else {}
                ),
                "latest_state": str(tile.get("latest_state") or "—"),
                "proposed_state": str(tile.get("proposed_state") or "—"),
                "review_scope": review_scope,
                "quote": str(evidence.get("quote") or ""),
                "source_urls": [
                    str(url)
                    for url in evidence.get("source_urls") or []
                    if str(url).startswith(("https://", "http://"))
                ],
                "source_classes": [str(value) for value in evidence.get("source_classes") or [] if str(value)],
                "acceptance_basis": [str(value) for value in evidence.get("acceptance_basis") or [] if str(value)],
                "first_seen_at": str(evidence.get("first_seen_at") or ""),
                "last_seen_at": str(evidence.get("last_seen_at") or ""),
                "observation_count": int(evidence.get("observation_count") or 0),
                "review_prompt": str(candidate.get("review_prompt") or ""),
                "decision": decision,
                "reviewer_id": str(evaluated.get("reviewer_id") or ""),
                "current_event_id": str(evaluated.get("event_id") or ""),
                "can_review": journal_ready and decision == "pending",
                "idempotency_key": f"vault-review-{uuid.uuid4()}",
            }
        )
    pending_count = sum(item["decision"] == "pending" for item in items)
    return {
        "domain": normalized,
        "brand_name": str(brand.get("name") or normalized),
        "available": bool(preview.get("report_count")),
        "journal_ready": journal_ready,
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "items": items,
        "summary": {
            "candidate_count": len(items),
            "pending_count": pending_count,
            "reviewed_count": len(items) - pending_count,
        },
    }


def _vault_response_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _vault_review_login_response(
    request: Request,
    *,
    next_path: str,
    error: str = "",
    status_code: int = 200,
):
    response = templates.TemplateResponse(
        request,
        "vault_review_login.html.j2",
        {
            "next_path": safe_vault_review_path(next_path),
            "error": error,
        },
        status_code=status_code,
    )
    return _vault_response_headers(response)


def _vault_review_page_response(
    request: Request,
    domain: str,
    session: VaultReviewerSession,
    *,
    error: str = "",
    saved: bool = False,
    status_code: int = 200,
):
    response = templates.TemplateResponse(
        request,
        "vault_review.html.j2",
        {
            "review": _vault_reviewer_profile(domain),
            "reviewer": session.reviewer_id,
            "csrf_token": session.csrf_token,
            "error": error,
            "saved": saved,
        },
        status_code=status_code,
    )
    return _vault_response_headers(response)


def _vault_review_error(error: ApiError) -> tuple[str, int]:
    if error.status_code == 409:
        return (
            "La decisión no se registró porque el candidato cambió desde que abriste la página. Recarga y revisa el estado actual.",
            409,
        )
    if error.status_code == 404:
        return (
            "El candidato ya no pertenece a la proyección inmutable actual.",
            404,
        )
    if error.status_code == 503:
        return (
            "El journal protegido no está disponible; no se ha persistido ninguna decisión.",
            503,
        )
    return ("La decisión no cumple el contrato de persistencia.", 400)


def _report_rows_for_index() -> list[dict[str, Any]]:
    rows = []
    for row in list_reports():
        enriched = dict(row)
        enriched["brand_domain"] = domain_key(str(row.get("url") or ""))
        enriched["score_publication"] = score_publication_from_report(enriched)
        rows.append(enriched)
    return rows


def _resolve_component_tile_profile(component: dict[str, Any]) -> dict[str, Any] | list[Any]:
    tile_profile = component.get("tile_profile")
    counts = {
        "lit": int(component.get("lit", 0) or 0),
        "off": int(component.get("off", 0) or 0),
        "blind": int(component.get("blind", 0) or 0),
        "scale": int(component.get("scale", 0) or 0),
    }
    if tile_profile is not None:
        if isinstance(tile_profile, list):
            if not tile_profile:
                return counts
            if any(counts.values()):
                lit = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "ok")
                off = sum(
                    1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "no"
                )
                blind = sum(
                    1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "sin_evidencia"
                )
                if (lit, off, blind) != (counts["lit"], counts["off"], counts["blind"]):
                    return counts
        return tile_profile

    tiles = component.get("tiles")
    if isinstance(tiles, list):
        if not tiles:
            return counts
        if any(counts.values()):
            lit = sum(1 for tile in tiles if str((tile or {}).get("estado") or "") == "ok")
            off = sum(1 for tile in tiles if str((tile or {}).get("estado") or "") == "no")
            blind = sum(1 for tile in tiles if str((tile or {}).get("estado") or "") == "sin_evidencia")
            if (lit, off, blind) != (counts["lit"], counts["off"], counts["blind"]):
                return counts
        return tiles
    return counts


def _raw_sv9_result(report: dict[str, Any]) -> dict[str, Any]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    sv9 = raw.get("sv9") if isinstance(raw.get("sv9"), dict) else {}
    result = sv9.get("result") if isinstance(sv9.get("result"), dict) else {}
    return result


def _raw_sv9_components(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = _raw_sv9_result(report)
    components = result.get("components") if isinstance(result.get("components"), dict) else {}
    return {str(key): value for key, value in components.items() if isinstance(value, dict)}


def _tile_names(component_key: str) -> dict[str, str]:
    spec = SV9_COMPONENTS.get(component_key) or {}
    return {str(tile.get("id") or ""): str(tile.get("name") or "") for tile in spec.get("tiles") or []}


def _tile_counts(tile_profile: list[dict[str, Any]]) -> tuple[int, int, int]:
    lit = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "ok")
    off = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "no")
    blind = sum(1 for tile in tile_profile if str((tile or {}).get("estado") or "") == "sin_evidencia")
    return lit, off, blind


def _failing_tiles(component_key: str, tile_profile: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = _tile_names(component_key)
    rows = []
    for tile in tile_profile:
        if not isinstance(tile, dict) or tile.get("estado") == "ok":
            continue
        tile_id = str(tile.get("id") or tile.get("tile_id") or "")
        rows.append(
            {
                "id": tile_id,
                "name": names.get(tile_id) or "",
                "estado": str(tile.get("estado") or ""),
                "motivo": spanish_tile_motivo(tile.get("motivo"), estado=tile.get("estado")),
                "contexto_requerido": spanish_tile_contexto(tile.get("contexto_requerido")),
                "evidencia": str(tile.get("evidencia") or ""),
            }
        )
    return rows


def _enrich_component_from_raw_sv9(item: dict[str, Any], raw_component: dict[str, Any]) -> dict[str, Any]:
    if not raw_component:
        return item
    enriched = dict(item)
    key = str(enriched.get("key") or enriched.get("component") or raw_component.get("component") or "")
    if key:
        enriched.setdefault("key", key)
        enriched.setdefault("component", key)
    for field in (
        "status",
        "score",
        "scale",
        "points",
        "confidence",
        "evaluation_model",
        "evidence",
        "source_policy_notes",
        "detected_content",
    ):
        if enriched.get(field) in (None, "", []):
            enriched[field] = raw_component.get(field)

    raw_tile_profile = raw_component.get("tile_profile")
    if isinstance(raw_tile_profile, list) and not enriched.get("tile_profile"):
        enriched["tile_profile"] = [tile for tile in raw_tile_profile if isinstance(tile, dict)]
    tile_profile = enriched.get("tile_profile") if isinstance(enriched.get("tile_profile"), list) else []
    if tile_profile:
        lit, off, blind = _tile_counts(tile_profile)
        enriched["lit"] = lit
        enriched["off"] = off
        enriched["blind"] = blind
        if not enriched.get("tiles"):
            enriched["tiles"] = _failing_tiles(key, tile_profile)

    resumen = str(enriched.get("resumen") or "").strip()
    if not resumen or resumen.startswith("Componente "):
        enriched["resumen"] = raw_component.get("detected_content") or resumen

    veredicto = str(enriched.get("veredicto") or "").strip()
    if not veredicto or veredicto.startswith("Síntesis automática"):
        enriched["veredicto"] = raw_component.get("message") or raw_component.get("veredicto") or veredicto
    if not enriched.get("message"):
        enriched["message"] = raw_component.get("message")
    return enriched


def _sanitize_report_language(report: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {}
    sanitized = dict(report)
    raw_result = _raw_sv9_result(report)
    if not sanitized.get("editorial") and isinstance(raw_result.get("editorial_v3_1"), dict):
        sanitized["editorial"] = raw_result.get("editorial_v3_1")
    if not sanitized.get("executive_reading") and raw_result.get("executive_reading"):
        sanitized["executive_reading"] = raw_result.get("executive_reading")
    raw_components = _raw_sv9_components(report)
    raw_editorial = raw_result.get("editorial_v3_1") if isinstance(raw_result.get("editorial_v3_1"), dict) else {}
    raw_editorial_components = (
        raw_editorial.get("components") if isinstance(raw_editorial.get("components"), dict) else {}
    )
    components = []
    for component in sanitized.get("components") or []:
        if not isinstance(component, dict):
            continue
        item = dict(component)
        key = str(item.get("key") or item.get("component") or "")
        item = _enrich_component_from_raw_sv9(item, raw_components.get(key) or {})
        if not item.get("editorial") and isinstance(raw_editorial_components.get(key), dict):
            item["editorial"] = raw_editorial_components.get(key)
        key = str(item.get("key") or item.get("component") or "")
        tile_profile = _resolve_component_tile_profile(item)
        item["resumen"] = spanish_component_summary(
            key,
            item.get("resumen"),
            tile_profile,
        )
        item["veredicto"] = spanish_component_verdict(key, item.get("veredicto"), tile_profile)
        item["message"] = spanish_generated_text(item.get("message"))
        item["resumen_is_fallback"] = str(item.get("resumen") or "").startswith("Componente ")
        tiles = []
        source_tiles = item.get("tiles") or []
        if not source_tiles and isinstance(tile_profile, list):
            source_tiles = _failing_tiles(key, tile_profile)
        for tile in source_tiles:
            if not isinstance(tile, dict):
                continue
            tile_item = dict(tile)
            tile_item["motivo"] = spanish_tile_motivo(tile_item.get("motivo"), estado=tile_item.get("estado"))
            tile_item["contexto_requerido"] = spanish_tile_contexto(tile_item.get("contexto_requerido"))
            tiles.append(tile_item)
        item["tiles"] = tiles
        components.append(item)
    sanitized["components"] = components
    return sanitized


def _scan_payload_for_markdown(report: dict[str, Any]) -> dict[str, Any]:
    from src.services.scanner_analysis_contract import (
        analysis_contract_from_report,
    )

    result = dict(_raw_sv9_result(report))
    projected_components = {
        str(component.get("key") or component.get("component") or ""): component
        for component in report.get("components") or []
        if isinstance(component, dict)
        and str(component.get("key") or component.get("component") or "")
    }
    if not result:
        components = {}
        for key, component in projected_components.items():
            components[key] = dict(component)
        result = {
            "brand3_score": report.get("score"),
            "base_average": report.get("base_average"),
            "brand_name": report.get("brand_name"),
            "url": report.get("url"),
            "reliability_status": report.get("reliability_status"),
            "components": components,
        }
    elif isinstance(result.get("components"), dict):
        components = {}
        for key, component in result["components"].items():
            if not isinstance(component, dict):
                continue
            merged = dict(component)
            projection = projected_components.get(str(key)) or {}
            if isinstance(projection.get("surface_hierarchy"), dict):
                merged["surface_hierarchy"] = dict(
                    projection["surface_hierarchy"]
                )
            components[str(key)] = merged
        result["components"] = components
    result.setdefault("display_name", report.get("brand_name"))
    result.setdefault("brand_name", report.get("brand_name"))
    result.setdefault("url", report.get("url"))
    result.setdefault("brand3_score", report.get("score"))
    result.setdefault("pipeline_commit_sha", report.get("pipeline_commit_sha") or "unknown")
    result.setdefault("created_at", report.get("created_at"))
    result.setdefault(
        "canonical_status",
        report.get("canonical_status")
        or (report.get("stability") or {}).get("canonical_status"),
    )
    result.setdefault(
        "canonical_reason_codes",
        (report.get("stability") or {}).get("reason_codes") or [],
    )
    result.setdefault(
        "stability",
        report.get("stability") or {},
    )
    result_coverage = (
        result.get("coverage_acquisition")
        if isinstance(result.get("coverage_acquisition"), dict)
        else {}
    )
    coverage_acquisition = {
        **result_coverage,
        **dict(report.get("coverage_acquisition") or {}),
    }
    coverage_acquisition.setdefault(
        "content_sampling",
        content_sampling_from_report(report),
    )
    result["coverage_acquisition"] = coverage_acquisition
    result.setdefault(
        "analysis_contract",
        analysis_contract_from_report(report),
    )
    return result


def _moodboard_from_report(report: dict[str, Any]) -> dict[str, Any]:
    from src.features.magnetism.moodboard import build_moodboard_model

    web_payload = _moodboard_web_payload(report)
    scan_payload = {
        "url": report.get("url") or "",
        "tldr_brand3": {
            str(block.get("name") or ""): {
                "detected": block.get("detected") is True,
                "content": str(block.get("content") or ""),
            }
            for block in report.get("blocks") or []
            if isinstance(block, dict) and block.get("name")
        },
    }
    model = build_moodboard_model(
        scan_payload,
        web_payload,
        brand_logo_url=str(report.get("brand_logo_url") or _visual_signature_logo_url(report) or ""),
    )
    model.update(
        {
            "report_id": report.get("id") or "",
            "brand_name": report.get("brand_name") or "",
            "score": report.get("score"),
            "url": report.get("url") or model.get("page_url") or "",
            "brand_domain": domain_key(str(report.get("url") or "")),
        }
    )
    return model


def _visual_signature_logo_url(report: dict[str, Any]) -> str:
    for payload in _visual_signature_payloads(report):
        logo_url = _logo_url_from_visual_signature_payload(payload)
        if logo_url:
            return logo_url
    return ""


def _visual_signature_payloads(report: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    for record in raw.get("raw_inputs") or []:
        if not isinstance(record, dict):
            continue
        if str(record.get("source") or "") not in {"visual_acquisition", "visual_signature"}:
            continue
        payload = record.get("payload")
        if isinstance(payload, dict):
            payloads.append(payload)

    evidence = (((raw.get("flow") or {}).get("candidate") or {}).get("evidence_pack") or {}).get("evidence") or []
    for record in evidence:
        if not isinstance(record, dict):
            continue
        if str(record.get("source") or "") not in {"visual_acquisition", "visual_signature"}:
            continue
        payload = _json_dict(record.get("content"))
        if payload:
            payloads.append(payload)
    return payloads


def _logo_url_from_visual_signature_payload(payload: dict[str, Any]) -> str:
    for key in ("visual_signature_evidence", "visual_evidence_packet"):
        evidence = payload.get(key) if isinstance(payload.get(key), dict) else {}
        identity = evidence.get("identity") if isinstance(evidence.get("identity"), dict) else {}
        for candidate in identity.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("role") != "real_logo":
                continue
            url = _http_url(candidate.get("url"))
            if url:
                return url

    raw_payload = payload.get("raw_visual_signature_payload") if isinstance(payload.get("raw_visual_signature_payload"), dict) else payload
    logo = raw_payload.get("logo") if isinstance(raw_payload.get("logo"), dict) else {}
    for candidate in logo.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        location = str(candidate.get("location") or "")
        confidence = candidate.get("confidence")
        if location not in {"header", "nav"} or not isinstance(confidence, (int, float)) or confidence < 0.55:
            continue
        url = _http_url(candidate.get("url"))
        if url:
            return url
    return ""


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _http_url(value: Any) -> str:
    candidate = str(value or "").strip()
    if candidate.startswith(("http://", "https://")):
        return candidate
    return ""


def _moodboard_web_payload(report: dict[str, Any]) -> dict[str, Any]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    for record in raw.get("raw_inputs") or []:
        if not isinstance(record, dict) or str(record.get("source") or "") != "web":
            continue
        payload = record.get("payload")
        if isinstance(payload, str):
            payload = _json_dict(payload)
        if not isinstance(payload, dict):
            continue
        if not any(payload.get(key) for key in ("html", "images", "markdown_content")):
            continue
        rich_payload = dict(payload)
        rich_payload.setdefault("url", str(report.get("url") or ""))
        rich_payload.setdefault(
            "canonical_url",
            str(rich_payload.get("url") or report.get("url") or ""),
        )
        rich_payload.setdefault("brand_name", str(report.get("brand_name") or ""))
        return rich_payload

    # Compatibility path for reports created before the immutable web payload
    # was exposed to the brand view. Evidence-pack Markdown has no DOM context
    # or dimensions, so the visual selector deliberately treats it as fallback.
    evidence = (
        (raw.get("flow") or {})
        .get("candidate", {})
        .get("evidence_pack", {})
        .get("evidence", [])
    )
    markdown_parts = []
    for record in evidence:
        if not isinstance(record, dict):
            continue
        if record.get("source") != "web" or record.get("evidence_type") != "raw_input":
            continue
        content = str(record.get("content") or "").strip()
        if content:
            markdown_parts.append(content)
    return {
        "url": str(report.get("url") or ""),
        "canonical_url": str(report.get("url") or ""),
        "brand_name": str(report.get("brand_name") or ""),
        "markdown_content": "\n\n".join(markdown_parts),
    }


def _api_health(*, run_checks: bool = False) -> dict[str, Any]:
    from src import config

    services = [
        _service_row(
            key="firecrawl_scrape",
            label="Firecrawl scrape",
            env_var="FIRECRAWL_API_KEY",
            secret=config.FIRECRAWL_API_KEY,
            enabled=True,
            run_checks=run_checks,
            check_fn=_check_firecrawl_scrape,
        ),
        _service_row(
            key="firecrawl_screenshot",
            label="Firecrawl screenshot",
            env_var="FIRECRAWL_API_KEY",
            secret=config.FIRECRAWL_API_KEY,
            enabled=True,
            run_checks=run_checks,
            check_fn=_check_firecrawl_screenshot,
        ),
        _service_row(
            key="exa",
            label="Exa search",
            env_var="EXA_API_KEY",
            secret=config.EXA_API_KEY,
            enabled=True,
            run_checks=run_checks,
            check_fn=_check_exa,
        ),
        _service_row(
            key="searchapi",
            label="SearchAPI fallback",
            env_var="SEARCHAPI_API_KEY",
            secret=config.SEARCHAPI_API_KEY,
            enabled=config.BRAND3_SEARCHAPI_VERTICAL_FALLBACK_ENABLED,
            run_checks=run_checks,
            check_fn=_check_searchapi,
        ),
        _service_row(
            key="llm",
            label="LLM provider",
            env_var="BRAND3_LLM_API_KEY",
            secret=config.BRAND3_LLM_API_KEY,
            enabled=True,
            run_checks=run_checks,
            check_fn=_check_llm,
            detail=f"configured model: {config.LLM_MODEL}",
        ),
        {
            "key": "github",
            "label": "GitHub proof",
            "env_var": "public GitHub API",
            "configured": True,
            "enabled": config.BRAND3_GITHUB_PROOF_ENABLED,
            "status": "configured" if config.BRAND3_GITHUB_PROOF_ENABLED else "disabled",
            "detail": "uses public repository metadata when owned links expose GitHub URLs",
            "elapsed_ms": None,
            "secret_suffix": "",
        },
    ]
    return {
        "status": _overall_api_status(services),
        "run_checks": run_checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "services": services,
    }


def _service_row(
    *,
    key: str,
    label: str,
    env_var: str,
    secret: str,
    enabled: bool,
    run_checks: bool,
    check_fn,
    detail: str = "",
) -> dict[str, Any]:
    configured = bool(secret)
    status = "disabled" if not enabled else "configured" if configured else "missing"
    row = {
        "key": key,
        "label": label,
        "env_var": env_var,
        "configured": configured,
        "enabled": enabled,
        "status": status,
        "detail": detail,
        "elapsed_ms": None,
        "secret_suffix": secret[-4:] if secret else "",
    }
    if not run_checks or check_fn is None:
        return row
    if not enabled:
        row["status"] = "disabled"
        row["detail"] = "disabled by feature flag"
        return row
    if not configured:
        row["status"] = "missing"
        row["detail"] = f"{env_var} not set"
        return row
    start = perf_counter()
    try:
        detail = check_fn(secret)
    except Exception as exc:  # external checks must report, not break the health page
        row["status"] = "error"
        row["detail"] = str(exc)[:220]
    else:
        row["status"] = "ok"
        row["detail"] = detail
    row["elapsed_ms"] = int((perf_counter() - start) * 1000)
    return row


def _check_firecrawl_scrape(secret: str) -> str:
    from src.collectors.web_collector import WebCollector

    result = WebCollector(api_key=secret)._run_firecrawl("https://example.com")
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return f"example.com scrape ok; markdown chars={len(result.get('content') or '')}"


def _check_firecrawl_screenshot(secret: str) -> str:
    from src.features.visual_analyzer import VisualAnalyzer

    result = VisualAnalyzer(api_key=secret).take_screenshot("https://example.com")
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return "example.com screenshot ok"


def _check_exa(secret: str) -> str:
    from src.collectors.exa_collector import ExaCollector

    results = ExaCollector(api_key=secret).search("OpenAI official website", num_results=1)
    return f"search ok; results={len(results)}"


def _check_searchapi(secret: str) -> str:
    from src.collectors.searchapi_collector import SearchApiCollector

    data = SearchApiCollector(api_key=secret, limit=1, timeout_seconds=8).collect(
        "OpenAI",
        "https://openai.com",
        intents=("news",),
        queries_by_intent={"news": "OpenAI"},
    )
    if data.status == "error":
        failed = [item.error for item in data.intents.values() if item.error]
        raise RuntimeError("; ".join(failed) or "SearchAPI returned error")
    return f"search ok; status={data.status}; results={sum(item.result_count for item in data.intents.values())}"


def _check_llm(secret: str) -> str:
    from src import config
    from src.features.llm_analyzer import LLMAnalyzer

    analyzer = LLMAnalyzer(api_key=secret, model=config.LLM_MODEL)
    analyzer.use_cache = False
    response = analyzer._call(
        "You are a health check. Reply with exactly OK.",
        "Reply exactly OK.",
        max_tokens=128,
    )
    if response.strip() != "OK":
        detail = analyzer.last_failure_reason or "empty_or_unexpected_response"
        raise RuntimeError(f"LLM check failed: {detail}")
    return f"model {analyzer.model} responded ok"


def _overall_api_status(services: list[dict[str, Any]]) -> str:
    statuses = {service["status"] for service in services}
    if "error" in statuses or "missing" in statuses:
        return "degraded"
    return "ok"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "commit_sha": current_build_sha()}


@app.get("/artifacts/screenshots/{filename}")
def screenshot_artifact(filename: str):
    from src.config import BRAND3_SCREENSHOT_DIR

    safe_name = Path(filename).name
    root = Path(BRAND3_SCREENSHOT_DIR).resolve()
    path = (root / safe_name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return JSONResponse({"error": "invalid artifact path"}, status_code=404)
    if not path.is_file():
        return JSONResponse({"error": "artifact not found"}, status_code=404)
    return FileResponse(path)


@app.get("/api/health/apis")
def api_health_api(run: bool = False):
    return JSONResponse(_api_health(run_checks=run))


@app.get("/health/apis")
def api_health_view(request: Request, run: bool = False):
    return templates.TemplateResponse(
        request,
        "api_health.html.j2",
        {"health": _api_health(run_checks=run)},
    )


@app.get("/api/scoring/lab")
def scoring_lab_api():
    backfill_reports()
    return JSONResponse(scoring_dashboard())


@app.get("/")
def index(request: Request, error: str = ""):
    return templates.TemplateResponse(
        request,
        "index.html.j2",
        {"reports": _report_rows_for_index(), "error": error},
    )


@app.get("/brand/{domain}")
def brand_view(request: Request, domain: str, lang: str = "es"):
    return templates.TemplateResponse(
        request,
        "brand.html.j2",
        {"brand": _brand_profile(domain), "lang": lang},
    )


@app.get("/vault/review/login")
def vault_review_login(
    request: Request,
    next_path: str = "/",
):
    if not vault_reviewer_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    destination = safe_vault_review_path(next_path)
    if _vault_reviewer_session(request) is not None:
        return _vault_response_headers(RedirectResponse(destination, status_code=303))
    return _vault_review_login_response(
        request,
        next_path=destination,
    )


@app.post("/vault/review/login")
def create_vault_review_session(
    request: Request,
    token: str = Form(""),
    next_path: str = Form("/"),
):
    if not vault_reviewer_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    destination = safe_vault_review_path(next_path)
    try:
        principal = authenticate_vault_reviewer(token)
        cookie_value = issue_vault_reviewer_session(principal)
    except ApiError as exc:
        if exc.status_code == 503:
            return _vault_review_login_response(
                request,
                next_path=destination,
                error="La credencial de revisión no está configurada correctamente en Vault.",
                status_code=503,
            )
        return _vault_review_login_response(
            request,
            next_path=destination,
            error="Credencial de revisión no válida.",
            status_code=401,
        )
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        key=VAULT_REVIEWER_COOKIE,
        value=cookie_value,
        max_age=VAULT_REVIEWER_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/vault/review",
    )
    return _vault_response_headers(response)


@app.post("/vault/review/logout")
def delete_vault_review_session(
    request: Request,
    csrf_token: str = Form(""),
):
    if not vault_reviewer_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    session = _vault_reviewer_session(request)
    if session is not None and not csrf_matches(session, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(
        VAULT_REVIEWER_COOKIE,
        path="/vault/review",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return _vault_response_headers(response)


@app.get("/vault/review/{domain}")
def vault_review_view(
    request: Request,
    domain: str,
    saved: str = "",
):
    if not vault_reviewer_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    normalized = domain_key(domain)
    if not normalized:
        raise HTTPException(status_code=404, detail="Brand not found")
    session = _vault_reviewer_session(request)
    if session is None:
        destination = quote(
            f"/vault/review/{normalized}",
            safe="",
        )
        return _vault_response_headers(
            RedirectResponse(
                f"/vault/review/login?next_path={destination}",
                status_code=303,
            )
        )
    return _vault_review_page_response(
        request,
        normalized,
        session,
        saved=saved == "1",
    )


@app.post("/vault/review/{domain}/decisions")
def create_vault_review_decision(
    request: Request,
    domain: str,
    csrf_token: str = Form(""),
    subject_id: str = Form(""),
    case_id: str = Form(""),
    decision: str = Form(""),
    expected_current_event_id: str = Form(""),
    rationale: str = Form(""),
    idempotency_key: str = Form(""),
):
    if not vault_reviewer_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    normalized = domain_key(domain)
    if not normalized:
        raise HTTPException(status_code=404, detail="Brand not found")
    session = _vault_reviewer_session(request)
    if session is None:
        destination = quote(
            f"/vault/review/{normalized}",
            safe="",
        )
        return _vault_response_headers(
            RedirectResponse(
                f"/vault/review/login?next_path={destination}",
                status_code=303,
            )
        )
    if not csrf_matches(session, csrf_token):
        return _vault_review_page_response(
            request,
            normalized,
            session,
            error="La sesión cambió o el formulario caducó. Recarga antes de firmar.",
            status_code=403,
        )
    if decision not in _VAULT_REVIEW_DECISION_REASON_CODES:
        return _vault_review_page_response(
            request,
            normalized,
            session,
            error="Selecciona una decisión válida.",
            status_code=422,
        )
    try:
        payload = EvidenceScoringRecoveryReviewCreateRequest.model_validate(
            {
                "subject_id": subject_id,
                "case_id": case_id,
                "decision": decision,
                "expected_current_event_id": (expected_current_event_id or None),
                "reason_code": _VAULT_REVIEW_DECISION_REASON_CODES[decision],
                "rationale": rationale,
                "evaluator_version": "vault-manual-review-v1",
            }
        )
        create_evidence_scoring_recovery_review(
            normalized,
            payload.model_dump(),
            client_id=session.reviewer_id,
            reviewer_id=session.reviewer_id,
            idempotency_key=idempotency_key,
        )
    except ValidationError:
        return _vault_review_page_response(
            request,
            normalized,
            session,
            error="Completa una justificación válida antes de registrar la decisión.",
            status_code=422,
        )
    except ApiError as exc:
        message, status_code = _vault_review_error(exc)
        return _vault_review_page_response(
            request,
            normalized,
            session,
            error=message,
            status_code=status_code,
        )
    response = RedirectResponse(
        f"/vault/review/{normalized}?saved=1",
        status_code=303,
    )
    return _vault_response_headers(response)


@app.post("/scan")
def create_scan(
    url: str = Form(...),
    brand_name: str = Form(""),
    allow_degraded_fallback: bool = Form(False),
):
    try:
        scan_id = start_scan(
            url,
            brand_name,
            allow_degraded_fallback=allow_degraded_fallback,
        )
    except ValueError as exc:
        return RedirectResponse(f"/?error={exc}", status_code=303)
    return RedirectResponse(f"/scan/{scan_id}", status_code=303)


@app.get("/dev/scan-preview")
def scan_preview_view(request: Request, variant: str = "blocked"):
    return templates.TemplateResponse(
        request,
        "scan.html.j2",
        {"scan": _scan_preview_status(variant)},
    )


def _scan_preview_status(variant: str) -> dict[str, Any]:
    normalized = variant if variant in {"running", "warning", "blocked"} else "blocked"
    phases = [
        {"key": "capture", "label": "Capture evidence", "state": "done"},
        {"key": "interpret", "label": "Interpret SV9", "state": "pending"},
        {"key": "score", "label": "Score components", "state": "pending"},
        {"key": "report", "label": "Write report", "state": "pending"},
    ]
    if normalized == "running":
        phases[1]["state"] = "running"
    if normalized == "warning":
        phases[1]["state"] = "running"
    if normalized == "blocked":
        phases[1]["state"] = "blocked"
    acquisition = [
        {"source": "web", "status": "ok", "detail": "owned homepage captured with 4 strategic surfaces"},
        {"source": "exa", "status": "fetched", "detail": "external proof available from mentions/news"},
        {"source": "searchapi", "status": "fetched", "detail": "fallback search returned 6 candidate references"},
        {"source": "github", "status": "skipped", "detail": "no repository links observed on owned capture"},
        {
            "source": "visual_acquisition",
            "status": "completed",
            "evidence_status": "blocked" if normalized == "blocked" else "usable",
            "screenshot_status": "captured",
            "first_fold_evaluable": normalized != "blocked",
            "detail": "visual evidence packet blocked by viewport obstruction" if normalized == "blocked" else "screenshot captured; visual evidence usable",
        },
    ]
    gate = {"state": "pass"} if normalized == "running" else {
        "state": "blocked" if normalized == "blocked" else "warning",
        "can_continue": normalized == "blocked",
        "issues": [
            {
                "source": "visual_acquisition",
                "code": "visual_acquisition_limited",
                "severity": "blocker" if normalized == "blocked" else "warning",
                "status": "blocked" if normalized == "blocked" else "limited",
                "evidence_status": "blocked" if normalized == "blocked" else "usable",
                "first_fold_evaluable": normalized != "blocked",
                "message": "Visual acquisition could not produce a reliable first-fold reading.",
                "detail": "visual_evidence_packet:blocked; obstruction:viewport_overlay" if normalized == "blocked" else "",
            }
        ] if normalized == "blocked" else [],
        "warnings": [
            {
                "source": "exa",
                "code": "external_profiles_empty",
                "severity": "warning",
                "status": "empty",
                "message": "External profile discovery was empty, but SearchAPI returned usable fallback references.",
                "detail": "",
            }
        ] if normalized == "warning" else [],
    }
    return {
        "id": f"preview-{normalized}",
        "brand_name": "Stabolut",
        "url": "https://stabolut.com",
        "state": "blocked" if normalized == "blocked" else "running",
        "preview": True,
        "phases": phases,
        "acquisition": acquisition,
        "acquisition_gate": gate,
    }


@app.get("/scan/{scan_id}")
def scan_view(request: Request, scan_id: str):
    status = scan_status(scan_id)
    if status is None:
        if load_report(scan_id) is not None:
            return RedirectResponse(f"/report/{scan_id}", status_code=303)
        return RedirectResponse("/?error=Unknown scan", status_code=303)
    return templates.TemplateResponse(request, "scan.html.j2", {"scan": status})


@app.get("/api/scan/{scan_id}")
def scan_api(scan_id: str):
    status = scan_status(scan_id)
    if status is None:
        if load_report(scan_id) is not None:
            return JSONResponse({"state": "done", "id": scan_id})
        return JSONResponse({"state": "unknown", "id": scan_id}, status_code=404)
    status.pop("raw", None)
    return JSONResponse(status)


@app.post("/api/scan/{scan_id}/continue")
def scan_continue_api(scan_id: str):
    result = approve_degraded_scan(scan_id)
    if result is None:
        return JSONResponse({"state": "unknown", "id": scan_id}, status_code=404)
    status_code = 200 if result.get("approved") else 409
    return JSONResponse(result, status_code=status_code)


@app.post("/api/scan/{scan_id}/cancel")
def scan_cancel_api(scan_id: str):
    result = cancel_scan(scan_id)
    if result is None:
        return JSONResponse({"state": "unknown", "id": scan_id}, status_code=404)
    return JSONResponse(result)


@app.get("/report/{scan_id}.md")
def report_markdown_view(scan_id: str):
    report = load_report(scan_id)
    if report is None:
        return PlainTextResponse("Report not found\n", status_code=404)
    from src.sv9.export_md import build_scan_markdown

    markdown = build_scan_markdown(_scan_payload_for_markdown(_sanitize_report_language(report)))
    return PlainTextResponse(markdown, media_type="text/markdown; charset=utf-8")


@app.get("/report/{scan_id}")
def report_view(request: Request, scan_id: str):
    report = load_report(scan_id)
    if report is None:
        return RedirectResponse("/?error=Report not found", status_code=303)
    report = _sanitize_report_language(report)
    report_vm = build_report_view_model(report)
    return templates.TemplateResponse(request, "report.html.j2", {"report": report_vm})
