"""Deterministic Spanish presentation guard for SV9 generated prose.

The SV9 scanner is a Spanish product surface. Source evidence may remain in the
source language, but Brand3-generated explanations (`motivo`,
`contexto_requerido`, `veredicto`, editorial messages and product-facing reason
codes) must not leak English when rendering the Spanish scanner.
"""

from __future__ import annotations

import re
from typing import Any

from src.sv9.rubric import COMPONENTS, ESTADO_NO, ESTADO_SIN_EVIDENCIA

_ENGLISH_PHRASE_RE = re.compile(
    r"\b("
    r"the snapshot|snapshot does not|does not provide|doesn't provide|does not include|"
    r"access to|full product interface|error states|transactional microcopy|"
    r"social media|product usage documentation|direct competitors|competitive analysis|"
    r"the brand idea|clearly articulated|consistently executed|cohesive visual system|"
    r"the available evidence|logged-in product|customer|competitor|requires evidence"
    r")\b",
    re.IGNORECASE,
)
_ENGLISH_WORD_RE = re.compile(
    r"\b(the|this|that|to|from|with|and|by|for|does|provide|provides|include|"
    r"access|full|product|interface|brand|idea|organizations|organization|"
    r"platform|values|enabling|integrate|manage|deploy|improve|across|without|"
    r"requiring|future|intelligence|integrated|fragmented|infrastructure|"
    r"clearly|executed|consistent|customer|competitor|social|media|documentation|"
    r"available|evidence|requires|logged|dashboard|states|microcopy)\b",
    re.IGNORECASE,
)
_SPANISH_WORD_RE = re.compile(
    r"\b(el|la|los|las|una|uno|que|marca|evidencia|snapshot|contexto|producto|"
    r"competidores|redes|requiere|aporta|no|sin|porque|para|con|del|de)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+")
_ENGLISH_STOPWORDS = {
    "a",
    "all",
    "also",
    "are",
    "as",
    "at",
    "be",
    "been",
    "beyond",
    "can",
    "could",
    "does",
    "for",
    "from",
    "goal",
    "goals",
    "has",
    "have",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "more",
    "not",
    "offer",
    "offers",
    "offering",
    "or",
    "provides",
    "provide",
    "providing",
    "should",
    "that",
    "the",
    "their",
    "they",
    "this",
    "to",
    "under",
    "with",
    "without",
    "you",
    "your",
}
_SPANISH_STOPWORDS = {
    "como",
    "con",
    "de",
    "del",
    "el",
    "en",
    "es",
    "esta",
    "esto",
    "la",
    "las",
    "le",
    "lo",
    "los",
    "más",
    "mi",
    "no",
    "o",
    "para",
    "pero",
    "que",
    "qué",
    "sin",
    "sobre",
    "su",
    "una",
    "y",
}

_STATUS_LABELS_ES = {
    "reliable": "confiable",
    "usable": "usable",
    "shadow": "sombra",
    "broken": "rota",
    "canonical": "canónico",
    "non_canonical": "no canónico",
    "invalid": "inválido",
}

_REASON_LABELS_ES = {
    "scan_not_complete": "scan incompleto",
    "coherencia_needs_review": "coherencia requiere revisión",
    "components_not_detected": "componentes no detectados",
    "blind_spots_above_usable_threshold": "puntos ciegos por encima del umbral usable",
    "blind_spots_present": "puntos ciegos presentes",
    "needs_review": "requiere revisión",
    "usable_not_canonical": "usable, no canónico",
    "shadow_not_canonical": "sombra, no canónico",
    "invalid_scan_state": "estado de scan inválido",
}


def spanish_status_label(value: object) -> str:
    raw = str(value or "").strip()
    return _STATUS_LABELS_ES.get(raw, raw or "desconocido")


def spanish_reason_labels(codes: list[object] | tuple[object, ...] | None) -> list[str]:
    return [_REASON_LABELS_ES.get(str(code), str(code)) for code in (codes or [])]


def looks_like_generated_english(text: object) -> bool:
    """Detect common English LLM prose, while avoiding literal quote handling."""

    value = str(text or "").strip()
    if not value:
        return False
    if _ENGLISH_PHRASE_RE.search(value):
        return True

    english_hits = len(_ENGLISH_WORD_RE.findall(value))
    spanish_hits = len(_SPANISH_WORD_RE.findall(value))
    word_count = len(_WORD_RE.findall(value))
    if word_count >= 8 and english_hits >= 4 and spanish_hits == 0:
        return True

    if word_count >= 10:
        tokens = [token.lower() for token in _WORD_RE.findall(value)]
        en = sum(1 for token in tokens if token in _ENGLISH_STOPWORDS)
        es = sum(1 for token in tokens if token in _SPANISH_STOPWORDS)
        if en >= 6 and en >= max(1, es * 2):
            return True

    return False


def spanish_generated_text(text: object, fallback: str = "") -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if looks_like_generated_english(value):
        return fallback
    return value


def spanish_tile_motivo(text: object, *, estado: object) -> str:
    fallback = (
        "El snapshot no aporta evidencia suficiente para evaluar esta baldosa sin contexto adicional."
        if str(estado or "") == ESTADO_SIN_EVIDENCIA
        else "El snapshot no comunica evidencia suficiente para encender esta baldosa."
    )
    return spanish_generated_text(text, fallback) or fallback


def spanish_tile_contexto(text: object) -> str:
    return spanish_generated_text(
        text,
        "Aporta contexto externo verificable: comparativa, experiencia de producto, canales activos o documentación operativa.",
    )


def spanish_component_verdict(component_key: str, text: object, tile_profile: object | None = None) -> str:
    value = str(text or "").strip()
    if value and not looks_like_generated_english(value):
        return value
    if not value:
        return ""
    return fallback_component_verdict(component_key, tile_profile)


def spanish_component_summary(component_key: str, text: object, tile_profile: object | None = None) -> str:
    value = str(text or "").strip()
    if value and not looks_like_generated_english(value):
        return value
    return fallback_component_summary(component_key, tile_profile)


def _tile_summary_counts(tile_profile: object | None, *, scale_hint: object | None = None) -> tuple[int, int, int, int]:
    scale = int(scale_hint or 0)

    if isinstance(tile_profile, dict):
        lit = int(tile_profile.get("lit") or 0)
        off = int(tile_profile.get("off") or 0)
        blind = int(tile_profile.get("blind") or 0)
        if scale:
            return scale, lit, off, blind
        return lit + off + blind, lit, off, blind

    if not isinstance(tile_profile, list):
        return int(scale or 0), 0, 0, 0

    lit = sum(1 for item in tile_profile if _estado(item) == "ok")
    off = sum(1 for item in tile_profile if _estado(item) == ESTADO_NO)
    blind = sum(1 for item in tile_profile if _estado(item) == ESTADO_SIN_EVIDENCIA)
    if scale:
        return int(scale), lit, off, blind
    return lit + off + blind, lit, off, blind


def _tile_failure_names(component_key: str, tile_profile: object | None) -> list[str]:
    if not isinstance(tile_profile, list):
        return []

    spec = COMPONENTS.get(component_key, {})
    tile_names = {
        str(tile.get("id")): str(tile.get("name") or "")
        for tile in spec.get("tiles", [])
        if isinstance(tile, dict)
    }
    names: list[str] = []
    for tile in tile_profile:
        if not isinstance(tile, dict):
            continue
        if _estado(tile) != ESTADO_NO:
            continue
        tile_id = str(tile.get("id") or "")
        name = tile_names.get(tile_id)
        if name:
            names.append(name)
    return names[:3]


def fallback_component_summary(component_key: str, tile_profile: object | None = None) -> str:
    spec = COMPONENTS.get(component_key, {})
    label = spec.get("label") or component_key
    scale, lit, off, blind = _tile_summary_counts(
        tile_profile,
        scale_hint=spec.get("scale") or 0,
    )
    if scale <= 0:
        return f"Componente {label} detectado a partir de la evidencia citada."

    return (
        f"Componente {label} detectado: {lit}/{scale} baldosas encendidas, "
        f"{off} apagada{'s' if off != 1 else ''}, {blind} punto{'s' if blind != 1 else ''} ciego{'s' if blind != 1 else ''}. "
        "Revisa las fuentes para validar el matiz exacto."
    )


def fallback_component_verdict(component_key: str, tile_profile: object | None = None) -> str:
    scale, lit, off, blind = _tile_summary_counts(
        tile_profile,
        scale_hint=COMPONENTS.get(component_key, {}).get("scale") or 0,
    )
    parts = [f"{lit}/{scale} baldosas encendidas"]
    if off:
        parts.append(f"{off} apagada{'s' if off != 1 else ''}")
    if blind:
        parts.append(f"{blind} punto{'s' if blind != 1 else ''} ciego{'s' if blind != 1 else ''}")

    failing_tiles = _tile_failure_names(component_key, tile_profile)
    if failing_tiles:
        parts.append("fallos: " + ", ".join(failing_tiles))

    return "Síntesis automática: " + ", ".join(parts) + "."


def _estado(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("estado") or "")
    return str(getattr(item, "estado", "") or "")
