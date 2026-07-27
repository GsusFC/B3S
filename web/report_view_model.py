"""Deterministic view-model for the B3S report UI.

This module is presentation-only. It reads the persisted report projection
after language sanitization and never recomputes scores, mutates raw SV9 data,
or calls an LLM.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from web.report_store import domain_key

from src.sv9.rubric import COMPONENTS as SV9_COMPONENTS

SCHEMA_VERSION = "b3s_report_view_model_v0_2"

COMPONENT_ROWS = (
    ("component-card--half", ("core_purpose", "magnetism")),
    ("component-card--third", ("value_proposition", "personality", "brand_idea")),
    ("component-card--half", ("attributes", "values")),
    ("component-card--half", ("mission", "vision")),
)

_TECHNICAL_TILE_COUNT_RE = re.compile(r"^\d+\s*/\s*\d+\s+baldosas\s+encendidas\b", re.IGNORECASE)

_ATTRIBUTE_TERMS = (
    ("agnóstica", ("agnóstic", "agnostic")),
    ("modular", ("modular",)),
    ("segura", ("segur", "security")),
    ("gobernable", ("gobern", "policies", "políticas")),
    ("soberana", ("soberan",)),
    ("flexible", ("flexib",)),
    ("adaptable", ("adapt", "adapts to change")),
    ("preparada para el futuro", ("future-proof", "preparad", "futuro")),
    ("intercambiable", ("intercambi", "swap", "change it all")),
    ("integrable", ("integr",)),
    ("controlada", ("control",)),
    ("observable", ("observab",)),
    ("optimizada", ("optim",)),
    ("escalable", ("escal",)),
    ("privada", ("privad",)),
)

_VALUE_TERMS = (
    ("IA como infraestructura", ("foundational", "fundamental", "pilar fundamental")),
    ("integración operativa", ("integraci", "operativa", "operational")),
    ("rigor operativo", ("experimental", "experimento", "superficial")),
    ("utilidad estructural", ("utilidad estructural", "structural")),
    ("seguridad", ("segur", "security")),
    ("soberanía tecnológica", ("soberan",)),
    ("control", ("control",)),
    ("transparencia", ("transparen",)),
    ("privacidad", ("privad",)),
    ("confianza", ("confian", "trust")),
    ("responsabilidad", ("responsab",)),
    ("adaptabilidad", ("adapt",)),
    ("excelencia técnica", ("excel",)),
    ("innovación pragmática", ("innov",)),
    ("eficiencia", ("eficien",)),
)


def build_report_view_model(report: dict[str, Any]) -> dict[str, Any]:
    """Build the stable UI contract consumed by `report.html.j2`."""

    components = [
        _component_view_model(component)
        for component in report.get("components") or []
        if isinstance(component, dict)
    ]
    components_by_key = {str(component.get("key") or ""): component for component in components}
    ordered_components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _row_class, keys in COMPONENT_ROWS:
        for key in keys:
            component = components_by_key.get(key)
            if component:
                component["span_class"] = _row_class
                ordered_components.append(component)
                seen.add(key)
    coherencia = components_by_key.get("coherencia")
    if coherencia:
        coherencia["span_class"] = "component-card--full"
        ordered_components.append(coherencia)
        seen.add("coherencia")
    for component in components:
        key = str(component.get("key") or "")
        if key not in seen:
            component["span_class"] = "component-card--full"
            ordered_components.append(component)

    acquisition = _acquisition_view_model(report)
    score = report.get("score")
    editorial = report.get("editorial") if isinstance(report.get("editorial"), dict) else {}
    insufficient_evidence = _coverage_limited_keys(report)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": str(report.get("id") or ""),
        "brand_name": str(report.get("brand_name") or ""),
        "url": str(report.get("url") or ""),
        "lang": "es",
        "score": score,
        "score_scale": 100,
        "score_width": score if score is not None else 0,
        "base_average": report.get("base_average"),
        "reliability": {
            "status": str(report.get("reliability_status") or "unknown"),
            "canonical_status": str(report.get("canonical_status") or "unknown"),
            "reason_codes": list(report.get("reliability_reason_codes") or []),
        },
        "hero": {
            "score_label": "Brand3 Score",
            "most_painful_gap": {
                "key": str(report.get("most_painful_gap") or ""),
                "label": str(report.get("most_painful_gap_label") or ""),
            },
            "immediate_margin": report.get("immediate_margin"),
            "not_detected": [str(item) for item in report.get("not_detected") or []],
            "insufficient_evidence": insufficient_evidence,
            "executive_reading": _clean_text(editorial.get("executive_reading") or report.get("executive_reading")),
        },
        "components": ordered_components,
        "component_rows": [
            {"class": row_class, "keys": list(keys)}
            for row_class, keys in COMPONENT_ROWS
        ],
        "acquisition": acquisition,
        "absences": list(report.get("absences") or []),
        "attempts": list(report.get("attempts") or []),
        "export": {
            "markdown_url": f"/report/{report.get('id')}.md",
            "brand_visual_url": f"/brand/{domain_key(str(report.get('url') or ''))}?lang=es#modulo-visual",
        },
        "raw_refs": {
            "has_raw": isinstance(report.get("raw"), dict) and bool(report.get("raw")),
        },
    }


def _coverage_limited_keys(report: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for component in report.get("components") or []:
        if not isinstance(component, dict):
            continue
        block = component.get("block") if isinstance(component.get("block"), dict) else {}
        if str(block.get("coverage_status") or "") != "insufficient_acquisition":
            continue
        key = str(component.get("key") or component.get("component") or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _component_view_model(component: dict[str, Any]) -> dict[str, Any]:
    key = str(component.get("key") or component.get("component") or "")
    meta = SV9_COMPONENTS.get(key) or {}
    label = str(component.get("label") or meta.get("label") or key)
    scale = int(component.get("scale") or meta.get("scale") or 0)
    tile_profile = component.get("tile_profile") if isinstance(component.get("tile_profile"), list) else []
    lit, off, blind = _tile_counts(component, tile_profile)
    primary = _primary_text(component)
    support = _support_text(component, primary_text=primary.get("text") or "")
    off_tiles, blind_spots = _split_tiles(component, tile_profile)
    evidence = _evidence_items(component)
    brand_quote = _lit_evidence_quote(tile_profile, evidence)
    block = component.get("block") if isinstance(component.get("block"), dict) else {}
    drawer = {
        "summary": {
            "primary_text": primary.get("text") or "",
            "confidence": str(component.get("confidence") or "unknown"),
            "coverage_status": str(block.get("coverage_status") or "unknown"),
        },
        "detected_basis": support,
        "next_artifact": _editorial_next_artifact(component),
        "evaluator_verdict": _secondary_verdict(component, primary_text=primary.get("text") or ""),
        "tile_summary": {"lit": lit, "off": off, "blind": blind, "scale": scale},
        "off_tiles": off_tiles,
        "blind_spots": blind_spots,
        "evidence": evidence,
        "coverage": {
            "status": str(block.get("coverage_status") or ""),
            "source_layers": list(component.get("source_layers") or []),
        },
        "debug": {
            "raw_component_key": key,
            "source_fields_used": _source_fields_used(primary, support),
            "editorial_schema_version": _clean_text(
                (component.get("editorial") or {}).get("schema_version")
                if isinstance(component.get("editorial"), dict)
                else ""
            ),
            "fallback_verdict": _technical_fallback_text(component.get("veredicto")),
            "fallback_summary": _technical_fallback_text(component.get("resumen")),
        },
    }
    has_drawer = any(
        (
            support.get("text"),
            support.get("items"),
            off_tiles,
            blind_spots,
            evidence,
            drawer["evaluator_verdict"],
            block.get("rejected_content"),
            block.get("coverage_status"),
        )
    )
    return {
        "key": key,
        "component": key,
        "label": label,
        "question": str(component.get("question") or meta.get("question") or ""),
        "score": component.get("score"),
        "scale": scale,
        "status": str(component.get("status") or ""),
        "confidence": str(component.get("confidence") or ""),
        "card": {
            "primary": primary,
            "support": support,
            "brand_quote": brand_quote,
            "meta": {
                "lit": lit,
                "off": off,
                "blind": blind,
                "scale": scale,
                "has_drawer": has_drawer,
            },
        },
        "drawer": drawer,
        "export": {},
    }


def _primary_text(component: dict[str, Any]) -> dict[str, Any]:
    editorial = component.get("editorial") if isinstance(component.get("editorial"), dict) else {}
    editorial_diagnosis = _clean_text(editorial.get("diagnosis"))
    editorial_terms = _editorial_terms(component)
    if editorial_diagnosis or editorial_terms:
        return {
            "role": "diagnosis",
            "text": editorial_diagnosis,
            "items": editorial_terms,
            "source": "editorial.diagnosis",
            "is_fallback": False,
        }

    for source in ("message", "veredicto", "detected_content", "resumen"):
        value = _clean_text(component.get(source))
        if not value:
            continue
        if source == "veredicto" and _is_technical_fallback(value):
            continue
        if source in {"detected_content", "resumen"} and _is_technical_fallback(value):
            continue
        return {
            "role": "diagnosis" if source in {"message", "veredicto"} else "detected_basis",
            "text": value,
            "items": _term_items(component, source),
            "source": source,
            "is_fallback": False,
        }

    level_zero = _clean_text(component.get("level_zero"))
    if level_zero:
        return {
            "role": "fallback",
            "text": level_zero,
            "items": [],
            "source": "fallback",
            "is_fallback": True,
        }
    return {"role": "none", "text": "", "items": [], "source": "none", "is_fallback": True}


def _support_text(component: dict[str, Any], *, primary_text: str) -> dict[str, Any]:
    editorial = component.get("editorial") if isinstance(component.get("editorial"), dict) else {}
    editorial_basis = _clean_text(editorial.get("detected_basis"))
    if editorial_basis and editorial_basis != primary_text:
        return {
            "role": "detected_basis",
            "text": editorial_basis,
            "items": [],
            "source": "editorial.detected_basis",
            "is_fallback": False,
            "show_on_card": True,
        }

    for source in ("detected_content", "resumen"):
        value = _clean_text(component.get(source))
        if not value or value == primary_text or _is_technical_fallback(value):
            continue
        return {
            "role": "detected_terms" if component.get("key") in {"attributes", "values"} else "detected_basis",
            "text": value,
            "items": _term_items(component, source),
            "source": source,
            "is_fallback": False,
            "show_on_card": True,
        }
    return {
        "role": "none",
        "text": "",
        "items": [],
        "source": "none",
        "is_fallback": False,
        "show_on_card": False,
    }


def _secondary_verdict(component: dict[str, Any], *, primary_text: str) -> str:
    value = _clean_text(component.get("veredicto"))
    if not value or value == primary_text or _is_technical_fallback(value):
        return ""
    return value


def _term_items(component: dict[str, Any], source: str) -> list[str]:
    key = str(component.get("key") or component.get("component") or "")
    editorial_terms = _editorial_terms(component)
    if editorial_terms:
        return editorial_terms
    raw = component.get(source)
    if isinstance(raw, list):
        return [_clean_text(item) for item in raw if _clean_text(item)][:5]
    explicit = component.get("items")
    if key in {"attributes", "values"} and isinstance(explicit, list):
        return [_clean_text(item) for item in explicit if _clean_text(item)][:5]
    if key in {"attributes", "values"}:
        return _semantic_terms(component, key=key, source_text=_clean_text(raw))
    return []


def _editorial_terms(component: dict[str, Any]) -> list[str]:
    key = str(component.get("key") or component.get("component") or "")
    if key not in {"attributes", "values"}:
        return []
    editorial = component.get("editorial") if isinstance(component.get("editorial"), dict) else {}
    terms = editorial.get("terms")
    if not isinstance(terms, list):
        return []
    return [_clean_text(item) for item in terms if _clean_text(item)][:5]


def _editorial_next_artifact(component: dict[str, Any]) -> str:
    editorial = component.get("editorial") if isinstance(component.get("editorial"), dict) else {}
    return _clean_text(editorial.get("next_artifact"))


def _semantic_terms(component: dict[str, Any], *, key: str, source_text: str) -> list[str]:
    candidates = _ATTRIBUTE_TERMS if key == "attributes" else _VALUE_TERMS
    text_parts = [source_text]
    text_parts.append(_clean_text(component.get("detected_content")))
    text_parts.append(_clean_text(component.get("resumen")))
    block = component.get("block") if isinstance(component.get("block"), dict) else {}
    text_parts.append(_clean_text(block.get("content")))
    for tile in component.get("tile_profile") or []:
        if isinstance(tile, dict) and str(tile.get("estado") or "") == "ok":
            text_parts.append(_clean_text(tile.get("evidencia")))
    text = _strip_accents(" ".join(part for part in text_parts if part)).lower()
    terms: list[str] = []
    for label, markers in candidates:
        if label in terms:
            continue
        if any(_strip_accents(marker).lower() in text for marker in markers):
            terms.append(label)
        if len(terms) >= 5:
            return terms

    if len(terms) < 3:
        for phrase in _compact_phrase_terms(source_text):
            if phrase not in terms:
                terms.append(phrase)
            if len(terms) >= 3:
                break
    return terms[:5]


def _compact_phrase_terms(text: str) -> list[str]:
    cleaned = _clean_text(text)
    if not cleaned:
        return []
    parts = re.split(r"[.;:,\u2022]|\s+y\s+|\s+e\s+", cleaned)
    terms: list[str] = []
    for part in parts:
        candidate = _compact_term_candidate(part)
        if candidate and candidate not in terms:
            terms.append(candidate)
    return terms


def _compact_term_candidate(value: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    text = re.sub(r"^(la|el|los|las|una|un|su|sus|tu|tus)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^(marca|plataforma|producto|equipo|optiak)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    words = text.split()
    if not 1 <= len(words) <= 4:
        return ""
    if len(text) > 48:
        return ""
    return text[0].upper() + text[1:] if text.islower() else text


def _split_tiles(component: dict[str, Any], tile_profile: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_tiles = component.get("tiles") if isinstance(component.get("tiles"), list) else []
    if not source_tiles and tile_profile:
        source_tiles = tile_profile
    off_tiles: list[dict[str, Any]] = []
    blind_spots: list[dict[str, Any]] = []
    for tile in source_tiles:
        if not isinstance(tile, dict):
            continue
        estado = str(tile.get("estado") or "")
        if estado not in {"no", "sin_evidencia"}:
            continue
        item = {
            "id": str(tile.get("id") or tile.get("tile_id") or ""),
            "name": str(tile.get("name") or _tile_name(str(component.get("key") or ""), tile) or ""),
            "estado": estado,
            "motivo": _clean_text(tile.get("motivo")),
            "evidencia": _clean_text(tile.get("evidencia")),
            "contexto_requerido": _clean_text(tile.get("contexto_requerido")),
        }
        if estado == "no":
            off_tiles.append(item)
        else:
            blind_spots.append(item)
    return off_tiles, blind_spots


def _ref_is_brand(ref: str) -> bool:
    """True when the ref points at the brand's own web (raw_inputs.1*) — its own voice."""
    r = str(ref or "").strip().lower()
    return r == "raw_inputs.1" or r.startswith("raw_inputs.1.")


def _lit_evidence_quote(tile_profile: list, evidence_items: list) -> dict[str, str]:
    """Literal quote that lit the first ON tile — this component's own brand-voice
    evidence (differs per component, unlike the shared block-level owned copy)."""
    snippet = ""
    for tile in tile_profile:
        if isinstance(tile, dict) and str(tile.get("estado")) == "ok":
            candidate = _strip_markdown(tile.get("evidencia"))
            if candidate:
                snippet = candidate
                break
    if not snippet:
        return {}
    url = next(
        (item.get("url") for item in evidence_items if item.get("is_brand") and item.get("url")),
        "",
    )
    return {"snippet": snippet, "url": url}


def _evidence_items(component: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for evidence in component.get("evidence") or []:
        if isinstance(evidence, dict):
            snippet = _strip_markdown(
                evidence.get("snippet")
                or evidence.get("content")
                or evidence.get("text")
                or evidence.get("evidencia")
            )
            url = _clean_text(evidence.get("url"))
            ref = _clean_text(evidence.get("ref") or evidence.get("id") or evidence.get("source"))
            source_class = _clean_text(evidence.get("source_class") or evidence.get("source"))
        else:
            snippet = _strip_markdown(evidence)
            url = ""
            ref = ""
            source_class = ""
        if snippet or url or ref:
            items.append(
                {
                    "ref": ref,
                    "url": url,
                    "snippet": snippet,
                    "source_class": source_class or "unknown",
                    "is_brand": _ref_is_brand(ref),
                }
            )

    block = component.get("block") if isinstance(component.get("block"), dict) else {}
    for ref_item in block.get("refs") or []:
        if not isinstance(ref_item, dict):
            continue
        ref = _clean_text(ref_item.get("ref"))
        items.append(
            {
                "ref": ref,
                "url": _clean_text(ref_item.get("url")),
                "snippet": _strip_markdown(ref_item.get("snippet")),
                "source_class": _clean_text(ref_item.get("source_class")) or "unknown",
                "is_brand": _ref_is_brand(ref),
            }
        )
    return items


def _acquisition_view_model(report: dict[str, Any]) -> dict[str, Any]:
    gate = report.get("acquisition_gate") if isinstance(report.get("acquisition_gate"), dict) else {}
    artifacts = []
    for artifact in report.get("acquisition_artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        href = _clean_text(
            artifact.get("public_url")
            or artifact.get("screenshot_url")
            or artifact.get("screenshot_path")
        )
        if not href:
            continue
        source = _clean_text(artifact.get("source"))
        kind = _clean_text(artifact.get("kind"))
        status = _clean_text(artifact.get("status"))
        provider = _clean_text(artifact.get("provider"))
        label = _clean_text(artifact.get("label"))
        artifacts.append(
            {
                "source": source,
                "kind": kind,
                "status": status,
                "provider": provider,
                "label": label,
                "href": href,
                "display_label": _artifact_label(
                    source=source,
                    kind=kind,
                    status=status,
                    provider=provider,
                    label=label,
                ),
                "chip_class": _artifact_chip_class(artifact),
            }
        )
    coverage = report.get("coverage_acquisition") if isinstance(report.get("coverage_acquisition"), dict) else {}
    return {
        "state": _clean_text(gate.get("state")) or "unknown",
        "warnings": list(gate.get("warnings") or []),
        "issues": list(gate.get("issues") or []),
        "fallbacks": list(gate.get("fallbacks") or []),
        "user_decision": _clean_text(gate.get("user_decision")),
        "decision_source": _clean_text(gate.get("decision_source")),
        "artifacts": artifacts,
        "metrics": {
            "owned_url_count": int(coverage.get("owned_url_count") or 0),
            "external_proof_count": int(
                coverage.get("external_proof_count")
                or coverage.get("external_source_count")
                or 0
            ),
            "external_attempt_count": int(
                coverage.get("external_attempt_count")
                or coverage.get("attempt_record_count")
                or 0
            ),
            "absence_ref_count": int(
                coverage.get("absence_ref_count")
                or coverage.get("absence_record_count")
                or 0
            ),
            "evidence_record_count": int(coverage.get("evidence_record_count") or 0),
        },
    }


def _tile_counts(component: dict[str, Any], tile_profile: list[Any]) -> tuple[int, int, int]:
    if tile_profile:
        return (
            sum(
                1
                for tile in tile_profile
                if isinstance(tile, dict) and str(tile.get("estado") or "") == "ok"
            ),
            sum(
                1
                for tile in tile_profile
                if isinstance(tile, dict) and str(tile.get("estado") or "") == "no"
            ),
            sum(
                1
                for tile in tile_profile
                if isinstance(tile, dict) and str(tile.get("estado") or "") == "sin_evidencia"
            ),
        )
    return (
        int(component.get("lit") or 0),
        int(component.get("off") or 0),
        int(component.get("blind") or 0),
    )


def _tile_name(component_key: str, tile: dict[str, Any]) -> str:
    tile_id = str(tile.get("id") or tile.get("tile_id") or "")
    spec = SV9_COMPONENTS.get(component_key) or {}
    for candidate in spec.get("tiles") or []:
        if isinstance(candidate, dict) and str(candidate.get("id") or "") == tile_id:
            return str(candidate.get("name") or "")
    return ""


def _source_fields_used(primary: dict[str, Any], support: dict[str, Any]) -> list[str]:
    fields = []
    for item in (primary, support):
        source = str(item.get("source") or "")
        if source and source != "none" and source not in fields:
            fields.append(source)
    return fields


def _artifact_label(
    *,
    source: str,
    kind: str,
    status: str,
    provider: str,
    label: str = "",
) -> str:
    parts = [part for part in (label, kind, status) if part]
    if not parts:
        parts = [source] if source else []
    label = " · ".join(parts) or "artefacto"
    if provider:
        label = f"{label} · {provider}"
    return label


def _artifact_chip_class(artifact: dict[str, Any]) -> str:
    status = str(artifact.get("status") or "")
    if artifact.get("success") or status in {"usable", "captured"}:
        return "ok"
    if status in {"blocked", "limited", "warning"}:
        return "warn"
    return "bad"


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _strip_markdown(value: object) -> str:
    """Drop leftover markdown header markers (#, ##, …) from an inline snippet."""
    return " ".join(re.sub(r"#{1,6}\s*", " ", str(value or "")).split())


def _is_technical_fallback(value: object) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    normalized = _strip_accents(text).lower()
    return (
        normalized.startswith("componente ")
        or normalized.startswith("sintesis automatica")
        or bool(_TECHNICAL_TILE_COUNT_RE.match(normalized))
    )


def _technical_fallback_text(value: object) -> str:
    return _clean_text(value) if _is_technical_fallback(value) else ""


def _strip_accents(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )
