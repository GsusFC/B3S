"""SV9 editorial layer: founder-facing prose conditioned on the verdict.

The inviolable order is number first, prose after: generation receives the
score, the first off tile (baldosa apagada), and the brand's own evidence as
hard constraints — it can explain the score, never move it. Same rubric
underneath, personalized voice on top.

Messages are presentation, not scoring: a generation failure leaves the
component without a message and never touches scores or statuses.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.sv9.language_guard import spanish_generated_text
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER

SV9_EDITORIAL_PROMPT_VERSION = "sv9-editorial-v3.1"
SV9_EDITORIAL_SCHEMA_VERSION = "sv9_editorial_v3_1"
SV9_EDITORIAL_TIMEOUT_SECONDS = 90
SV9_EDITORIAL_MAX_ATTEMPTS = 2

_TERMS_COMPONENTS = {"attributes", "values"}
_BANNED_VISIBLE_RE = re.compile(
    r"\b("
    r"objeto de deseo|mercado saturado|identidad emocional|"
    r"se posiciona como|debe mejorar|necesita mejorar|"
    r"m[aá]s all[aá] de|herramienta [úu]til|propuesta s[oó]lida|"
    r"para escalar|comunica con claridad|"
    r"score|puntuaci[oó]n|sv9|brand3|baldosas?|r[úu]brica"
    r")\b",
    re.IGNORECASE,
)
_BANNED_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bobjeto de deseo\b", re.IGNORECASE), "atracción distintiva"),
    (re.compile(r"\bmercado saturado\b", re.IGNORECASE), "categoría competida"),
    (re.compile(r"\bidentidad emocional\b", re.IGNORECASE), "capa de significado"),
    (re.compile(r"\bse posiciona como\b", re.IGNORECASE), "actúa como"),
    (re.compile(r"\bdebe mejorar\b", re.IGNORECASE), "requiere ajustar"),
    (re.compile(r"\bnecesita mejorar\b", re.IGNORECASE), "requiere ajustar"),
    (re.compile(r"\bm[aá]s all[aá] de\b", re.IGNORECASE), "fuera de"),
    (re.compile(r"\bherramienta [úu]til\b", re.IGNORECASE), "utilidad funcional"),
    (re.compile(r"\bpropuesta s[oó]lida\b", re.IGNORECASE), "base consistente"),
    (re.compile(r"\bpara escalar\b", re.IGNORECASE), "en el siguiente ciclo"),
    (re.compile(r"\bcomunica con claridad\b", re.IGNORECASE), "formula con precisión"),
    (re.compile(r"\bBrand3\b", re.IGNORECASE), "el diagnóstico"),
    (re.compile(r"\bSV9\b", re.IGNORECASE), "el scanner"),
    (re.compile(r"\bbaldosas?\b", re.IGNORECASE), "señales"),
    (re.compile(r"\br[úu]brica\b", re.IGNORECASE), "criterio"),
    (re.compile(r"\bscore\b", re.IGNORECASE), "resultado"),
    (re.compile(r"\bpuntuaci[oó]n\b", re.IGNORECASE), "nota"),
)

_SYSTEM_PROMPT = """Eres estratega senior de marca. Escribes en español para un estudio que entrega resúmenes estratégicos tipo TLDR/FLOC.

Tu tarea: convertir un resultado persistido del scanner en un view-model editorial. No reevalúes puntuaciones. No inventes evidencia.

Estilo obligatorio:
- Concreto, diagnóstico y útil para decidir.
- Cada frase debe nombrar una causa, una tensión o una consecuencia.
- Evita lenguaje de consultoría y frases comodín.
- No uses estas expresiones: "objeto de deseo", "mercado saturado", "identidad emocional", "debe mejorar", "necesita mejorar", "más allá de", "herramienta útil", "propuesta sólida", "para escalar", "se posiciona como".
- No menciones "Brand3", "SV9", "baldosas", "score", "puntuación", "rúbrica" ni maquinaria interna en la prosa visible.
- No uses el nombre del componente como muletilla.
- Mantén todo en español salvo nombres propios, claims literales o tecnología.
- Si falta información, descríbelo como una ausencia estratégica observable, no como error técnico.

Contrato V3.1:
1. brand_context resume qué es la marca antes de los componentes.
2. tension_map define la tesis transversal del diagnóstico.
3. Cada componente entrega solo lo necesario para una card editorial.

Reglas de componentes:
- tldr: titular estratégico, máximo 24 palabras.
- diagnosis: párrafo interpretativo, 45-65 palabras. Debe explicar causa + consecuencia + siguiente tensión.
- detected_basis: base factual detectada, 16-30 palabras. Sin interpretación excesiva.
- next_artifact: artefacto o decisión concreta que crear, máximo 14 palabras.
- terms: SOLO attributes y values. Para attributes: 3-5 rasgos percibidos. Para values: 3-5 principios observados. Si values está missing/no detectado, terms debe ser [].
- claim_type: "observed", "inferred" o "missing".
- mode: "editorial", "evidence_based" o "gap".
- confidence: número de 0 a 1.

Diferencia attributes/values:
- attributes = cualidades percibidas, rasgos de comportamiento o adjetivos compuestos.
- values = principios de decisión o creencias operativas. No mezcles valores con atributos.

Devuelve SOLO JSON válido.
"""


def build_editorial(
    scan: dict[str, Any],
    *,
    llm: Any,
    component_keys: list[str] | tuple[str, ...] | set[str] | None = None,
    include_executive_reading: bool = True,
) -> dict[str, Any]:
    """Generate the report-level editorial contract for a scan.

    `scan` accepts both shapes: Sv9ScanResult.to_dict() (components as dict)
    and the persisted Sv9Store.get_scan payload (components as list).
    Returns the legacy projection (`component_messages`, `executive_reading`)
    plus `structured`, the V3.1 view-model-safe editorial payload.
    """
    if llm is None or not getattr(llm, "api_key", None):
        return {"component_messages": {}, "executive_reading": None}

    components = _normalize_components(scan)
    requested = {str(key) for key in component_keys} if component_keys is not None else None
    keys = [
        key for key in PRESENTATION_ORDER
        if key in components
        and (requested is None or key in requested)
        and str((components.get(key) or {}).get("status") or "") != "not_evaluated"
    ]
    if not keys:
        return {"component_messages": {}, "executive_reading": None}

    raw = _call_structured_editorial(scan, components, keys, llm)
    structured = _envelope_structured(raw, requested_components=keys) if raw else None
    if structured is None:
        return {"component_messages": {}, "executive_reading": None}

    structured_components = structured.get("components") if isinstance(structured.get("components"), dict) else {}
    messages = {
        key: message
        for key, component in structured_components.items()
        if key in keys
        for message in [spanish_generated_text((component or {}).get("diagnosis"))]
        if message
    }
    reading = spanish_generated_text(structured.get("executive_reading"))
    return {
        "component_messages": messages,
        "executive_reading": reading if include_executive_reading else None,
        "structured": structured if include_executive_reading else {**structured, "executive_reading": None},
    }


def _normalize_components(scan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = scan.get("components")
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if isinstance(v, dict)}
    normalized = {}
    for item in raw or []:
        if isinstance(item, dict) and item.get("component") in COMPONENTS:
            normalized[item["component"]] = item
    return normalized


def _call_structured_editorial(
    scan: dict[str, Any],
    components: dict[str, dict[str, Any]],
    keys: list[str],
    llm: Any,
) -> dict[str, Any] | None:
    payload = _prompt_payload(scan, components, keys)
    schema = _json_schema(keys)
    user = "Genera el contrato editorial V3.1 para este reporte compacto.\n\n" + json.dumps(
        payload,
        ensure_ascii=False,
    )
    last_issues: list[str] = []
    for attempt in range(SV9_EDITORIAL_MAX_ATTEMPTS):
        prompt = user
        if last_issues:
            prompt = (
                user
                + "\n\nLa salida anterior fue rechazada por estas razones: "
                + "; ".join(last_issues[:12])
                + ". Regenera todo el JSON corrigiendo solo esos incumplimientos."
            )
        try:
            raw = llm._call_json(
                _SYSTEM_PROMPT,
                prompt,
                json_schema=schema,
                schema_name=SV9_EDITORIAL_SCHEMA_VERSION,
                timeout_seconds=SV9_EDITORIAL_TIMEOUT_SECONDS,
            )
        except Exception:
            return None
        if not isinstance(raw, dict) or not raw:
            last_issues = [str(getattr(llm, "last_failure_reason", None) or "empty_output")]
            continue
        issues = _validation_issues(raw, scan=scan, components=components, keys=keys)
        if not issues:
            return raw
        sanitized = _sanitize_structured_texts(raw)
        sanitized_issues = _validation_issues(sanitized, scan=scan, components=components, keys=keys)
        if not sanitized_issues:
            return sanitized
        last_issues = sanitized_issues
    return None


def _json_schema(keys: list[str]) -> dict[str, Any]:
    component_properties = {key: _component_schema(key) for key in keys}
    return {
        "type": "object",
        "required": ["brand_context", "tension_map", "executive_reading", "components"],
        "properties": {
            "brand_context": {
                "type": "object",
                "required": [
                    "what",
                    "for_whom",
                    "how",
                    "why",
                    "category",
                    "proof",
                    "main_gap",
                    "narrative_thesis",
                ],
                "properties": {
                    key: {"type": "string"}
                    for key in (
                        "what",
                        "for_whom",
                        "how",
                        "why",
                        "category",
                        "proof",
                        "main_gap",
                        "narrative_thesis",
                    )
                },
                "additionalProperties": False,
            },
            "tension_map": {
                "type": "object",
                "required": [
                    "category_enemy",
                    "core_mechanism",
                    "earned_strength",
                    "missing_signal",
                    "brand_thesis",
                    "not_x_but_y",
                    "decision_filter",
                ],
                "properties": {
                    key: {"type": "string"}
                    for key in (
                        "category_enemy",
                        "core_mechanism",
                        "earned_strength",
                        "missing_signal",
                        "brand_thesis",
                        "not_x_but_y",
                        "decision_filter",
                    )
                },
                "additionalProperties": False,
            },
            "executive_reading": {"type": "string"},
            "components": {
                "type": "object",
                "required": keys,
                "properties": component_properties,
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }


def _component_schema(key: str) -> dict[str, Any]:
    terms_schema: dict[str, Any]
    if key in _TERMS_COMPONENTS:
        terms_schema = {"type": "array", "items": {"type": "string"}, "maxItems": 5}
    else:
        terms_schema = {"type": "array", "items": {"type": "string"}, "maxItems": 0}
    return {
        "type": "object",
        "required": [
            "tldr",
            "diagnosis",
            "detected_basis",
            "next_artifact",
            "terms",
            "claim_type",
            "mode",
            "confidence",
        ],
        "properties": {
            "tldr": {"type": "string"},
            "diagnosis": {"type": "string"},
            "detected_basis": {"type": "string"},
            "next_artifact": {"type": "string"},
            "terms": terms_schema,
            "claim_type": {"type": "string", "enum": ["observed", "inferred", "missing"]},
            "mode": {"type": "string", "enum": ["editorial", "evidence_based", "gap"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "additionalProperties": False,
    }


def _prompt_payload(
    scan: dict[str, Any],
    components: dict[str, dict[str, Any]],
    keys: list[str],
) -> dict[str, Any]:
    return {
        "brand": {
            "name": scan.get("brand_name") or scan.get("display_name"),
            "url": scan.get("url"),
            "brand_score": scan.get("brand3_score"),
            "most_painful_gap": scan.get("most_painful_gap"),
            "immediate_margin": scan.get("immediate_margin"),
            "not_detected": _not_detected_components(scan, components),
        },
        "components_to_write": keys,
        "components": {
            key: _compact_component_for_prompt(key, components[key])
            for key in PRESENTATION_ORDER
            if key in components
        },
    }


def _compact_component_for_prompt(key: str, component: dict[str, Any]) -> dict[str, Any]:
    spec = COMPONENTS.get(key) or {}
    tile_profile = [
        tile if isinstance(tile, dict) else tile.to_dict()
        for tile in component.get("tile_profile") or []
    ]
    return {
        "label": spec.get("label") or key,
        "question": spec.get("question") or "",
        "score": component.get("score"),
        "scale": component.get("scale") or spec.get("scale"),
        "status": component.get("status"),
        "confidence": component.get("confidence"),
        "detected_content": _clean_text(component.get("detected_content"))[:500],
        "veredicto": _clean_text(component.get("veredicto"))[:650],
        "message": _clean_text(component.get("message"))[:650],
        "lit_evidence": [
            _clean_text(tile.get("evidencia"))[:220]
            for tile in tile_profile
            if str(tile.get("estado") or "") == "ok" and _clean_text(tile.get("evidencia"))
        ][:6],
        "off_reasons": [
            {
                "tile": str(tile.get("id") or tile.get("tile_id") or ""),
                "reason": _clean_text(tile.get("motivo"))[:220],
            }
            for tile in tile_profile
            if str(tile.get("estado") or "") == "no"
        ][:6],
        "blind_spots": [
            {
                "tile": str(tile.get("id") or tile.get("tile_id") or ""),
                "required_context": _clean_text(tile.get("contexto_requerido"))[:220],
            }
            for tile in tile_profile
            if str(tile.get("estado") or "") == "sin_evidencia"
        ][:6],
    }


def _validation_issues(
    raw: dict[str, Any],
    *,
    scan: dict[str, Any],
    components: dict[str, dict[str, Any]],
    keys: list[str],
) -> list[str]:
    issues: list[str] = []
    for location, text in _visible_texts(raw):
        if _BANNED_VISIBLE_RE.search(text):
            issues.append(f"{location}:banned_phrase")
        if text and not spanish_generated_text(text):
            issues.append(f"{location}:not_spanish")

    output_components = raw.get("components")
    if not isinstance(output_components, dict):
        return issues + ["components:not_object"]

    missing_values = "values" in _not_detected_components(scan, components) or str(
        (components.get("values") or {}).get("status") or ""
    ) == "not_detected"
    for key in keys:
        component = output_components.get(key)
        if not isinstance(component, dict):
            issues.append(f"{key}:missing_component")
            continue
        terms = component.get("terms")
        if key not in _TERMS_COMPONENTS and terms:
            issues.append(f"{key}:unexpected_terms")
        if key in _TERMS_COMPONENTS and not isinstance(terms, list):
            issues.append(f"{key}:terms_not_list")
        if key == "values" and missing_values and terms:
            issues.append("values:terms_must_be_empty_when_missing")
        if key in _TERMS_COMPONENTS and not (key == "values" and missing_values):
            status = str((components.get(key) or {}).get("status") or "")
            claim_type = str(component.get("claim_type") or "")
            if status != "not_detected" and claim_type != "missing" and not (3 <= len(terms or []) <= 5):
                issues.append(f"{key}:terms_count")
    return issues


def _visible_texts(raw: dict[str, Any]) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    if not isinstance(raw, dict):
        return texts
    if isinstance(raw.get("executive_reading"), str):
        texts.append(("executive_reading", raw["executive_reading"]))
    for section_key in ("brand_context", "tension_map"):
        section = raw.get(section_key)
        if isinstance(section, dict):
            for key, value in section.items():
                if isinstance(value, str):
                    texts.append((f"{section_key}.{key}", value))
    components = raw.get("components")
    if isinstance(components, dict):
        for key, component in components.items():
            if not isinstance(component, dict):
                continue
            for field in ("tldr", "diagnosis", "detected_basis", "next_artifact"):
                value = component.get(field)
                if isinstance(value, str):
                    texts.append((f"components.{key}.{field}", value))
            for index, term in enumerate(component.get("terms") or []):
                if isinstance(term, str):
                    texts.append((f"components.{key}.terms[{index}]", term))
    return texts


def _envelope_structured(raw: dict[str, Any], *, requested_components: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SV9_EDITORIAL_SCHEMA_VERSION,
        "prompt_version": SV9_EDITORIAL_PROMPT_VERSION,
        "status": "generated",
        "requested_components": list(requested_components),
        "brand_context": raw.get("brand_context") or {},
        "tension_map": raw.get("tension_map") or {},
        "executive_reading": spanish_generated_text(raw.get("executive_reading")),
        "components": {
            key: _clean_structured_component(component)
            for key, component in (raw.get("components") or {}).items()
            if isinstance(component, dict)
        },
    }


def _clean_structured_component(component: dict[str, Any]) -> dict[str, Any]:
    terms = component.get("terms") if isinstance(component.get("terms"), list) else []
    return {
        "tldr": spanish_generated_text(component.get("tldr")),
        "diagnosis": spanish_generated_text(component.get("diagnosis")),
        "detected_basis": spanish_generated_text(component.get("detected_basis")),
        "next_artifact": spanish_generated_text(component.get("next_artifact")),
        "terms": [_clean_text(term) for term in terms if _clean_text(term)][:5],
        "claim_type": str(component.get("claim_type") or ""),
        "mode": str(component.get("mode") or ""),
        "confidence": component.get("confidence"),
    }


def _sanitize_structured_texts(value: Any) -> Any:
    if isinstance(value, str):
        text = value
        for pattern, replacement in _BANNED_REPLACEMENTS:
            text = pattern.sub(replacement, text)
        return text
    if isinstance(value, list):
        return [_sanitize_structured_texts(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_structured_texts(item) for key, item in value.items()}
    return value


def _not_detected_components(scan: dict[str, Any], components: dict[str, dict[str, Any]]) -> list[str]:
    explicit = scan.get("not_detected")
    if isinstance(explicit, list):
        return [str(item) for item in explicit]
    return [
        key
        for key, component in components.items()
        if str(component.get("status") or "") == "not_detected"
    ]


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())
