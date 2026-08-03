"""SV9 component evaluator (baldosas v3.1): one verdict per tile, three states.

Evaluation principles:
- Chained context, independent verdicts. Evaluators receive detected texts
  (facts) from other components, never their scores (judgments).
- Every evaluation is total: each component always returns score + status.
  Nothing blocks anything. All tiles are evaluated, always.
- The LLM judges tiles and quotes evidence. The score, the confidence index, the
  x2 multipliers and the Magnetism cap are computed by code, never by the model.

The evidence/motive contract:
- `ok` requires a verbatim `evidencia` quote from the snapshot. A missing or
  non-verbatim quote is invalid and triggers a retry; if it survives the last
  attempt it is demoted to `no`.
- `no` and `sin_evidencia` require a `motivo`; a missing motivo is auto-filled
  rather than retried, to avoid burning attempts on cheap omissions.
- Out-of-catalogue states or missing tile coverage trigger a retry with the
  validation error fed back to the model.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from src.sv9.aggregator import score_from_tile_profile
from src.sv9.models import (
    ComponentResult,
    ESTADO_NO,
    ESTADO_OK,
    ESTADO_SIN_EVIDENCIA,
    STATUS_NOT_DETECTED,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    TileVerdict,
)
from src.sv9.language_guard import (
    spanish_component_verdict,
    spanish_generated_text,
    spanish_tile_contexto,
    spanish_tile_motivo,
)
from src.sv9.rubric import (
    COMPONENTS,
    PRESENTATION_ORDER,
    REASONING_COMPONENTS,
    TILE_ESTADOS,
    tile_ids,
)

SV9_EVALUATOR_PROMPT_VERSION = "baldosas-v3.1-evaluator-v2"
SV9_EVALUATOR_TIMEOUT_SECONDS = 90
SV9_EVALUATOR_MAX_WORKERS = 4
SV9_EVALUATOR_MAX_ATTEMPTS = 2
_MIN_EMBEDDED_LITERAL_CHARS = 8
_EMBEDDED_QUOTE_RE = re.compile(
    r"""["'“”‘’`]([^"'“”‘’`]+)["'“”‘’`]"""
)
# Greedy decoding: the tile evaluator must be deterministic. At temperature 0.1
# borderline components collapsed 0<->5 across identical frozen inputs (pure
# sampling noise, sd=0 at temp 0 over 32 runs). Only this role changes; other
# LLM roles keep the 0.1 default in _call_json.
SV9_EVALUATOR_TEMPERATURE = 0.0

_TILES_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "componente": {"type": "string"},
        "baldosas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "estado": {"type": "string", "enum": list(TILE_ESTADOS)},
                    "evidencia": {
                        "type": "string",
                        "description": (
                            "Solo el substring literal exacto copiado del "
                            "snapshot cuando estado=ok; sin explicación, "
                            "prefijo, paráfrasis ni comillas añadidas."
                        ),
                    },
                    "motivo": {"type": "string"},
                    "contexto_requerido": {"type": "string"},
                },
                "required": ["id", "estado"],
            },
        },
        "puntos": {"type": "integer"},
        "confianza": {"type": "string"},
        "veredicto": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["baldosas"],
}

# Anti-bias framing (briefing section 2): injected before the tiles in every
# system prompt. Literal, including rule 6 (evaluate only the snapshot).
_ANTIBIAS_PREAMBLE = """Eres el evaluador del Brand3 Scanner. Juzgas UN componente de marca contra una rúbrica de baldosas (características clave independientes). Cada baldosa se enciende o no.

MARCO DE CATEGORÍA (antes de puntuar)
Clasifica el juego de la marca a partir de la evidencia del snapshot, nunca de tu conocimiento previo: consumo, B2B SaaS, infraestructura/DeepTech, institucional, lujo/cultural. La clasificación no cambia las baldosas ni los puntos: cambia qué cuenta como evidencia válida.

REGLAS ANTI-SESGO
1. Las baldosas miden construcción, no temperatura. Ninguna exige tono "humano", "cercano" o "empático". Un arquetipo implacable y técnico ejecutado con consistencia total puntúa igual que uno cálido. Evalúa nitidez y consistencia, no simpatía.
2. El magnetismo tiene cinco mecanismos válidos: dolor, deseo, asombro, pertenencia y estatus. Pertenencia es formar parte de algo; estatus es señalar posición ante los demás (clave en lujo, premium y Web3). No los confundas ni los cuentes doble. Identifica el mecanismo dominante antes de evaluar su ejecución; exigir "gancho de dolor" a una marca cuyo motor es el asombro o el estatus es un error de evaluación, no un fallo de la marca.
3. El público puede ser inequívoco sin estar nombrado. Si producto y contexto del snapshot hacen evidente quién compra, el criterio se cumple con esa evidencia.
4. "Problema real" incluye problemas de industria, de infraestructura, de mundo y fronteras tecnológicas, no solo dolores cotidianos de usuario final.
5. Detección no es declaración. Valores y atributos perceptibles de forma consistente en tono, decisiones y producto cuentan como detectados aunque no exista una página que los liste. Anota la vía: declarado o inferido.
6. Evalúa ÚNICAMENTE el contenido presente en el snapshot. Tu conocimiento pre-entrenado sobre la marca no existe a efectos de esta evaluación. Si sabes que la marca hace X pero X no aparece en el snapshot, la marca está fallando en comunicarlo: la baldosa se apaga. No completes, no asumas, no rellenes huecos con memoria. Toda evidencia citada debe ser literal del snapshot. La fama no sube puntos, no baja puntos y no es evidencia en ningún sentido."""

# Three-state protocol (briefing sections 1 and 4): shared by all evaluators.
_PROTOCOL = """PROTOCOLO DE BALDOSAS (tres estados, dos significados de cero)
- "ok": baldosa encendida. La marca lo comunica o lo cumple. Requiere "evidencia": cita LITERAL del snapshot. El campo debe contener SOLO el substring exacto copiado: sin introducción, explicación, paráfrasis ni comillas añadidas. Sin cita literal, no es ok.
- "no": baldosa apagada. El snapshot demuestra que la marca NO lo comunica o NO lo cumple. Es un fallo de marca. Requiere "motivo".
- "sin_evidencia": punto ciego. La prueba que encendería la baldosa el snapshot estructuralmente NO puede contenerla (cohorte competitivo, experiencia real de producto, dinámica interna de comunidad). No es un fallo de marca. Requiere "motivo" y, si procede, "contexto_requerido": qué aportaría el usuario para iluminarlo.

FRONTERA ENTRE "no" Y "sin_evidencia": si la prueba PODRÍA estar en la huella pública y no está, la baldosa se APAGA ("no"): no comunicarlo es el fallo. Reserva "sin_evidencia" solo para baldosas cuya prueba el snapshot no puede contener por su naturaleza.

REGLAS DE SALIDA
- Evalúa TODAS las baldosas del componente, siempre. Una por una. Sin orden ni dependencia.
- No inventes. No promedies. No premies ambición sin prueba.
- Devuelve JSON estricto con una entrada por cada baldosa, usando su id exacto.
- Además, si puedes, devuelve "message": una lectura editorial breve en español para el fundador. Debe sintetizar la implicación estratégica del resultado sin mencionar la rúbrica, el pipeline ni las baldosas."""

_EVALUATOR_SYSTEM_PROMPT = f"""{_ANTIBIAS_PREAMBLE}

{_PROTOCOL}

La rúbrica de baldosas de este componente, con sus ids y condiciones, llega en el mensaje del usuario. Es la única rúbrica: no añadas ni quites baldosas."""

_COHERENCIA_SYSTEM_PROMPT = f"""{_ANTIBIAS_PREAMBLE}

Evalúas COHERENCIA. Coherencia no tiene detección propia: lee el conjunto en dos ejes.
- EJE INTERNO (sintonía): ¿los 9 componentes cuentan la misma historia? ¿La visión continúa la misión? ¿La personalidad ejecuta los valores? ¿El magnetismo nace de la propuesta o es un eslogan pegado?
- EJE EXTERNO (consistencia): ¿lo que la marca dice en su web coincide con lo que dice — y con lo que se dice de ella — en el resto de sus espacios digitales? Júzgalo con las señales auxiliares de consistencia.
Los componentes ausentes ("(no detectado)") son información de coherencia: los huecos significan que las piezas no se pueden conectar en ese punto.

{_PROTOCOL}

VEREDICTO (obligatorio en Coherencia): además de las baldosas, devuelve un campo "veredicto": una sola frase de síntesis que explique si la marca cuenta una historia única y dónde se rompe o se sostiene. Es copy final que verá el fundador: directa, concreta, sin jerga de pipeline ni mención a la rúbrica.

La rúbrica de baldosas de Coherencia, con sus ids y condiciones, llega en el mensaje del usuario. Es la única rúbrica."""


def evaluate_snapshot_components(
    *,
    tldr: dict[str, Any],
    signals: dict[str, list[dict[str, Any]]],
    brand_name: str,
    url: str,
    llm: Any,
    reasoning_llm: Any | None = None,
) -> dict[str, ComponentResult]:
    """Evaluate the 9 detected components in parallel, then Coherencia.

    Model routing (deploy brief section 2.6): components in REASONING_COMPONENTS
    (Magnetism, Coherencia) run on `reasoning_llm`; the rest run on `llm` (the
    Flash tier). When `reasoning_llm` is None the same client serves both, so
    callers that pass a single LLM keep the old single-tier behaviour.
    """
    reasoning_llm = reasoning_llm if reasoning_llm is not None else llm
    component_keys = [key for key in PRESENTATION_ORDER if key != "coherencia"]

    def _llm_for(key: str) -> Any:
        return reasoning_llm if key in REASONING_COMPONENTS else llm

    def _evaluate(key: str) -> ComponentResult:
        return evaluate_component(
            key,
            tldr=tldr,
            signals=signals.get(key) or [],
            brand_name=brand_name,
            url=url,
            llm=_llm_for(key),
        )

    results: dict[str, ComponentResult] = {}
    with ThreadPoolExecutor(max_workers=SV9_EVALUATOR_MAX_WORKERS) as pool:
        for key, result in zip(component_keys, pool.map(_evaluate, component_keys)):
            results[key] = result

    results["coherencia"] = evaluate_coherencia(
        components=results,
        tldr=tldr,
        signals=signals.get("coherencia") or [],
        brand_name=brand_name,
        url=url,
        llm=reasoning_llm,
    )
    return results


def evaluate_component(
    key: str,
    *,
    tldr: dict[str, Any],
    signals: list[dict[str, Any]],
    brand_name: str,
    url: str,
    llm: Any,
) -> ComponentResult:
    """Evaluate one of the 9 components against its full tile set."""
    spec = COMPONENTS[key]
    block = tldr.get(spec["tldr_key"]) if spec["tldr_key"] else None
    block = block if isinstance(block, dict) else {}

    detected = bool(block.get("detected")) and block.get("content")
    if not detected and not (spec.get("evaluate_on_signals") and signals):
        return ComponentResult(
            component=key,
            status=STATUS_NOT_DETECTED,
            score=0,
            detection_mode=str(block.get("mode") or "not_detected"),
            detection_confidence=str(block.get("confidence") or "insufficient"),
            detection_limitations=[str(item) for item in block.get("limitations") or []],
            evidence_source_summary=_int_dict(block.get("evidence_source_summary")),
        )

    if llm is None or not getattr(llm, "api_key", None):
        return ComponentResult(
            component=key,
            status=STATUS_NOT_EVALUATED,
            detected_content=_block_content_text(block),
            detection_mode=str(block.get("mode") or ""),
            detection_confidence=str(block.get("confidence") or ""),
            detection_limitations=[str(item) for item in block.get("limitations") or []],
            evidence_source_summary=_int_dict(block.get("evidence_source_summary")),
            error="llm_unavailable",
        )

    user_prompt = _build_component_prompt(
        key,
        block=block,
        signals=signals,
        tldr=tldr,
        brand_name=brand_name,
        url=url,
    )
    return _run_tile_call(
        key,
        system=_EVALUATOR_SYSTEM_PROMPT,
        user=user_prompt,
        llm=llm,
        signals=signals,
        detected_content=_block_content_text(block),
        detection_mode=str(block.get("mode") or ""),
        detection_confidence=str(block.get("confidence") or ""),
        detection_limitations=[str(item) for item in block.get("limitations") or []],
        evidence_source_summary=_int_dict(block.get("evidence_source_summary")),
        evidence=[str(e) for e in (block.get("evidence") or [])],
        literal_sources=_component_literal_sources(block, signals),
    )


def evaluate_coherencia(
    *,
    components: dict[str, ComponentResult],
    tldr: dict[str, Any],
    signals: list[dict[str, Any]],
    brand_name: str,
    url: str,
    llm: Any,
) -> ComponentResult:
    """Evaluate Coherencia over the whole: internal harmony + external consistency."""
    if llm is None or not getattr(llm, "api_key", None):
        return ComponentResult(
            component="coherencia",
            status=STATUS_NOT_EVALUATED,
            error="llm_unavailable",
        )

    literal_sources = _coherencia_literal_sources(components, tldr)
    user_prompt = _build_coherencia_prompt(
        components=components,
        tldr=tldr,
        signals=signals,
        brand_name=brand_name,
        url=url,
        literal_sources=literal_sources,
    )
    return _run_tile_call(
        "coherencia",
        system=_COHERENCIA_SYSTEM_PROMPT,
        user=user_prompt,
        llm=llm,
        literal_sources=literal_sources,
    )


def _run_tile_call(
    key: str,
    *,
    system: str,
    user: str,
    llm: Any,
    signals: list[dict[str, Any]] | None = None,
    detected_content: str | None = None,
    detection_mode: str | None = None,
    detection_confidence: str | None = None,
    detection_limitations: list[str] | None = None,
    evidence_source_summary: dict[str, int] | None = None,
    evidence: list[str] | None = None,
    literal_sources: list[str] | None = None,
) -> ComponentResult:
    ids = tile_ids(key)
    requires_veredicto = key == "coherencia"
    last_error = "llm_error"
    user_with_feedback = user
    last_valid_verdicts: list[TileVerdict] | None = None
    last_valid_message = ""

    for attempt in range(SV9_EVALUATOR_MAX_ATTEMPTS):
        is_last = attempt == SV9_EVALUATOR_MAX_ATTEMPTS - 1
        try:
            raw = llm._call_json(
                system,
                user_with_feedback,
                json_schema=_TILES_JSON_SCHEMA,
                schema_name=f"baldosas_{key}",
                timeout_seconds=SV9_EVALUATOR_TIMEOUT_SECONDS,
                temperature=SV9_EVALUATOR_TEMPERATURE,
            )
        except Exception as exc:  # Total evaluation: a crash is a status, not an abort.
            last_error = f"evaluator_exception: {exc}"
            break

        verdicts, error = _normalize_tiles(
            raw,
            ids,
            lenient=is_last,
            literal_sources=(
                evidence
                if literal_sources is None
                else literal_sources
            ),
        )
        veredicto = str((raw or {}).get("veredicto") or "").strip() if isinstance(raw, dict) else ""
        message = spanish_generated_text((raw or {}).get("message")) if isinstance(raw, dict) else ""
        if verdicts is not None:
            last_valid_verdicts = verdicts
            last_valid_message = message
        if verdicts is not None and (veredicto or not requires_veredicto or is_last):
            verdicts = _apply_sv9_flow_tile_signal_overrides(verdicts, signals or [])
            final_veredicto = spanish_component_verdict(key, veredicto, verdicts)
            if requires_veredicto and not final_veredicto:
                # Product decision (2026-06-14): the evaluator failed to produce
                # the mandatory synthesis sentence after retries. Rather than
                # nuke a 20-point component, synthesize a deterministic fallback
                # from the tile profile so the "siempre hay veredicto" contract
                # holds without inflating or zeroing the score.
                final_veredicto = _fallback_veredicto(key, verdicts)
            return ComponentResult(
                component=key,
                status=STATUS_SCORED,
                score=score_from_tile_profile(verdicts),
                tile_profile=verdicts,
                veredicto=final_veredicto,
                message=message,
                evaluation_model=getattr(llm, "model", None),
                detected_content=detected_content,
                detection_mode=detection_mode,
                detection_confidence=detection_confidence,
                detection_limitations=list(detection_limitations or []),
                evidence_source_summary=dict(evidence_source_summary or {}),
                evidence=evidence or [],
            )

        if verdicts is not None and not veredicto and requires_veredicto:
            error = "falta 'veredicto': frase de síntesis obligatoria en Coherencia"
        last_error = str(getattr(llm, "last_failure_reason", None) or error or "llm_error")
        feedback_error = _latest_schema_validation_detail(llm) or error
        user_with_feedback = (
            f"{user}\n\nTu respuesta anterior fue inválida: {feedback_error}. "
            "Corrige y devuelve JSON estricto con una baldosa por cada id, "
            "evidencia literal en cada 'ok' y motivo en cada 'no' y 'sin_evidencia'. "
            "En cada 'ok', `evidencia` debe ser SOLO el substring exacto copiado "
            "de CITAS DE EVIDENCIA: sin explicación, introducción, paráfrasis "
            "ni comillas añadidas."
            + (" Incluye el campo 'veredicto'." if requires_veredicto else "")
        )

    if last_valid_verdicts is not None:
        # A corrective retry must never destroy a structurally valid tile
        # profile from an earlier attempt. This matters most for Coherencia:
        # missing prose can be synthesized deterministically, while losing ten
        # valid tile verdicts would fabricate a 20-point score collapse.
        verdicts = _apply_sv9_flow_tile_signal_overrides(last_valid_verdicts, signals or [])
        return ComponentResult(
            component=key,
            status=STATUS_SCORED,
            score=score_from_tile_profile(verdicts),
            tile_profile=verdicts,
            veredicto=_fallback_veredicto(key, verdicts),
            message=last_valid_message,
            evaluation_model=getattr(llm, "model", None),
            detected_content=detected_content,
            detection_mode=detection_mode,
            detection_confidence=detection_confidence,
            detection_limitations=list(detection_limitations or []),
            evidence_source_summary=dict(evidence_source_summary or {}),
            evidence=evidence or [],
        )

    return ComponentResult(
        component=key,
        status=STATUS_NOT_EVALUATED,
        evaluation_model=getattr(llm, "model", None),
        detected_content=detected_content,
        detection_mode=detection_mode,
        detection_confidence=detection_confidence,
        detection_limitations=list(detection_limitations or []),
        evidence_source_summary=dict(evidence_source_summary or {}),
        evidence=evidence or [],
        error=last_error,
    )


def _latest_schema_validation_detail(llm: Any) -> str:
    """Return the provider payload's safe schema error for corrective retries."""

    if getattr(llm, "last_failure_reason", None) != "schema_validation_error":
        return ""
    failures = getattr(llm, "call_failures", None)
    if not isinstance(failures, list) or not failures:
        return ""
    latest = failures[-1]
    if not isinstance(latest, dict) or latest.get("reason") != "schema_validation_error":
        return ""
    return str(latest.get("error") or "").strip()


def _int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, item in value.items():
        try:
            out[str(key)] = int(item)
        except (TypeError, ValueError):
            continue
    return out


def _component_literal_sources(
    block: dict[str, Any],
    signals: list[dict[str, Any]],
) -> list[str]:
    sources = [
        str(item)
        for item in block.get("evidence") or []
        if str(item or "").strip()
    ]
    for signal in signals:
        for key in ("value", "detail", "rationale"):
            value = str(signal.get(key) or "").strip()
            if value and value not in sources:
                sources.append(value)
    return sources


def _coherencia_literal_sources(
    components: dict[str, ComponentResult],
    tldr: dict[str, Any],
    *,
    limit: int = 16,
) -> list[str]:
    sources: list[str] = []
    for key in PRESENTATION_ORDER:
        component = components.get(key)
        if component is None:
            continue
        for item in component.evidence:
            value = str(item or "").strip()
            if value and value not in sources:
                sources.append(value)
            if len(sources) >= limit:
                return sources
    for key in PRESENTATION_ORDER:
        spec = COMPONENTS[key]
        tldr_key = spec.get("tldr_key")
        block = tldr.get(tldr_key) if tldr_key else None
        if not isinstance(block, dict):
            continue
        for item in block.get("evidence") or []:
            value = str(item or "").strip()
            if value and value not in sources:
                sources.append(value)
            if len(sources) >= limit:
                return sources
    return sources


def _apply_sv9_flow_tile_signal_overrides(
    verdicts: list[TileVerdict],
    signals: list[dict[str, Any]],
) -> list[TileVerdict]:
    """Apply high-confidence negative SV9 Flow tile signals after LLM judging.

    `supports` remains advisory because lighting a tile requires an evaluator
    verdict with evidence. Negative tile-scoped signals are guardrails: the
    model cannot turn them into `ok` by extrapolating from block-level prose.
    """

    overrides: dict[str, tuple[str, str]] = {}
    for signal in signals:
        if signal.get("feature") != "sv9_flow_tile_signal":
            continue
        if str(signal.get("confidence") or "").lower() != "high":
            continue
        effect = str(signal.get("effect") or signal.get("value") or "").strip()
        if effect not in {"insufficient_evidence", "weakens"}:
            continue
        tile = _signal_tile_id(signal.get("tile"))
        if not tile:
            continue
        rationale = str(signal.get("rationale") or signal.get("detail") or "").strip()
        overrides[tile] = (effect, rationale)

    if not overrides:
        return verdicts

    normalized: list[TileVerdict] = []
    for verdict in verdicts:
        override = overrides.get(verdict.tile_id)
        if override is None:
            normalized.append(verdict)
            continue
        effect, rationale = override
        if effect == "weakens":
            normalized.append(
                TileVerdict(
                    tile_id=verdict.tile_id,
                    estado=ESTADO_NO,
                    motivo=_override_motivo(
                        "La señal SV9 Flow de alta confianza debilita esta baldosa.",
                        rationale,
                    ),
                )
            )
            continue
        normalized.append(
            TileVerdict(
                tile_id=verdict.tile_id,
                estado=ESTADO_SIN_EVIDENCIA,
                motivo=_override_motivo(
                    "La señal SV9 Flow de alta confianza marca esta baldosa como evidencia insuficiente.",
                    rationale,
                ),
                contexto_requerido=spanish_tile_contexto(verdict.contexto_requerido),
            )
        )
    return normalized


def _signal_tile_id(value: Any) -> str:
    raw = str(value or "").strip()
    if "." in raw:
        return raw.rsplit(".", 1)[-1]
    return raw


def _override_motivo(prefix: str, rationale: str) -> str:
    if rationale:
        return f"{prefix} {rationale}"[:700]
    return prefix


def _fallback_veredicto(key: str, verdicts: list[TileVerdict]) -> str:
    """Deterministic synthesis sentence when the evaluator omits `veredicto`.

    Founder-facing copy built from the tile counts, marked as auto-generated so
    calibration can tell it apart from a model-written verdict.
    """
    scale = COMPONENTS[key]["scale"]
    ok = sum(1 for v in verdicts if v.estado == ESTADO_OK)
    off = sum(1 for v in verdicts if v.estado == ESTADO_NO)
    blind = sum(1 for v in verdicts if v.estado == ESTADO_SIN_EVIDENCIA)
    parts = [f"{ok}/{scale} baldosas encendidas"]
    if off:
        parts.append(f"{off} apagada{'s' if off != 1 else ''}")
    if blind:
        parts.append(f"{blind} punto{'s' if blind != 1 else ''} ciego{'s' if blind != 1 else ''}")
    return "Síntesis automática: " + ", ".join(parts) + "."


def _normalize_tiles(
    raw: Any,
    ids: list[str],
    *,
    lenient: bool,
    literal_sources: list[str],
) -> tuple[list[TileVerdict] | None, str]:
    """Validate the LLM payload into one verdict per tile.

    Returns (verdicts, "") on success or (None, error) when the payload should
    be retried. On the last attempt (`lenient`), recoverable problems are fixed
    in place rather than retried: `ok` without evidence is demoted to `no`,
    missing motivos are auto-filled. `ok` evidence must be a normalized
    substring of one of the exact evidence snippets supplied to the evaluator.
    """
    if not isinstance(raw, dict):
        return None, "la respuesta no es un objeto JSON"
    items = raw.get("baldosas")
    if not isinstance(items, list):
        return None, "falta el array 'baldosas'"

    by_id: dict[str, TileVerdict] = {}
    unknown: list[str] = []
    bad_state: list[str] = []
    duplicates: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tile_id = str(item.get("id") or item.get("tile_id") or "").strip()
        if tile_id not in ids:
            if tile_id:
                unknown.append(tile_id)
            continue
        estado = str(item.get("estado") or "").strip()
        if estado not in TILE_ESTADOS:
            bad_state.append(tile_id)
            if not lenient:
                continue
            estado = ESTADO_NO
        if tile_id in by_id:
            # "One verdict per tile": a duplicate is a schema violation, not a
            # silent last-writer-wins overwrite. Keep the first seen verdict.
            duplicates.append(tile_id)
            if not lenient:
                continue
        by_id.setdefault(tile_id, TileVerdict(
            tile_id=tile_id,
            estado=estado if estado in TILE_ESTADOS else ESTADO_NO,
            evidencia=_canonical_literal_quote(
                str(item.get("evidencia") or "").strip(),
                literal_sources,
            ),
            motivo=str(item.get("motivo") or "").strip(),
            contexto_requerido=str(item.get("contexto_requerido") or "").strip(),
        ))

    missing = [tid for tid in ids if tid not in by_id]
    if not lenient:
        if duplicates:
            return None, f"ids duplicados: {', '.join(sorted(set(duplicates)))}"
        if unknown:
            return None, f"ids fuera de catálogo: {', '.join(sorted(set(unknown)))}"
        if bad_state:
            return None, f"estado fuera de catálogo en: {', '.join(sorted(set(bad_state)))}"
        if missing:
            return None, f"faltan baldosas: {', '.join(missing)}"
        no_quote = [
            tid for tid, v in by_id.items() if v.estado == ESTADO_OK and not v.evidencia
        ]
        non_literal_quote = [
            tid
            for tid, verdict in by_id.items()
            if (
                verdict.estado == ESTADO_OK
                and verdict.evidencia
                and not _literal_quote_in_sources(
                    verdict.evidencia,
                    literal_sources,
                )
            )
        ]
        invalid_quote = no_quote + non_literal_quote
        if invalid_quote:
            return None, (
                "'ok' sin evidencia literal verificable en: "
                f"{', '.join(invalid_quote)}"
            )

    # Lenient pass (last attempt): fill the gaps instead of failing.
    if missing and not lenient:
        return None, f"faltan baldosas: {', '.join(missing)}"

    normalized: list[TileVerdict] = []
    for tid in ids:
        verdict = by_id.get(tid)
        if verdict is None:
            # Only reachable when lenient: an unjudged tile is a brand failure
            # by default, never a free pass.
            normalized.append(
                TileVerdict(tile_id=tid, estado=ESTADO_NO, motivo="sin veredicto del evaluador")
            )
            continue
        if (
            verdict.estado == ESTADO_OK
            and (
                not verdict.evidencia
                or not _literal_quote_in_sources(
                    verdict.evidencia,
                    literal_sources,
                )
            )
        ):
            verdict = TileVerdict(
                tile_id=tid,
                estado=ESTADO_NO,
                motivo="degradada: 'ok' sin evidencia literal verificable",
            )
        if verdict.estado in (ESTADO_NO, ESTADO_SIN_EVIDENCIA) and not verdict.motivo:
            verdict.motivo = (
                "punto ciego sin motivo explícito"
                if verdict.estado == ESTADO_SIN_EVIDENCIA
                else "sin evidencia que la encienda"
            )
        if verdict.estado in (ESTADO_NO, ESTADO_SIN_EVIDENCIA):
            verdict.motivo = spanish_tile_motivo(verdict.motivo, estado=verdict.estado)
        if verdict.estado == ESTADO_SIN_EVIDENCIA and verdict.contexto_requerido:
            verdict.contexto_requerido = spanish_tile_contexto(verdict.contexto_requerido)
        normalized.append(verdict)
    return normalized, ""


def _literal_quote_in_sources(
    quote: str,
    sources: list[str],
) -> bool:
    normalized_quote = " ".join(str(quote or "").split()).casefold()
    if not normalized_quote:
        return False
    return any(
        normalized_quote
        in " ".join(str(source or "").split()).casefold()
        for source in sources
        if str(source or "").strip()
    )


def _canonical_literal_quote(
    value: str,
    sources: list[str],
) -> str:
    evidence = str(value or "").strip()
    if not evidence or _literal_quote_in_sources(evidence, sources):
        return evidence
    fragments = sorted(
        {
            fragment.strip()
            for fragment in _EMBEDDED_QUOTE_RE.findall(evidence)
            if len(" ".join(fragment.split())) >= _MIN_EMBEDDED_LITERAL_CHARS
        },
        key=lambda fragment: (-len(fragment), fragment.casefold()),
    )
    return next(
        (
            fragment
            for fragment in fragments
            if _literal_quote_in_sources(fragment, sources)
        ),
        evidence,
    )


def _block_content_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False) if content else ""


def _tiles_lines(key: str) -> str:
    lines = []
    for tile in COMPONENTS[key]["tiles"]:
        line = f"{tile['id']} · {tile['name']}: {tile['condition']}"
        note = tile.get("note")
        if note:
            line += f" [NOTA: {note}]"
        contract = _tile_evidence_contract_text(tile)
        if contract:
            line += f"\n  contrato_evidencia: {contract}"
        lines.append(line)
    return "\n".join(lines)


def _tile_evidence_contract_text(tile: dict[str, Any]) -> str:
    contract = tile.get("evidence_contract")
    if not isinstance(contract, dict):
        return ""
    parts = []
    labels = (
        ("ok", "ok"),
        ("no", "no"),
        ("sin_evidencia", "sin_evidencia"),
        ("strong_sources", "fuentes_fuertes"),
        ("reject", "no_aceptar"),
    )
    for key, label in labels:
        value = str(contract.get(key) or "").strip()
        if value:
            parts.append(f"{label}={value}")
    return " | ".join(parts)


def _context_texts(key: str, tldr: dict[str, Any]) -> dict[str, str]:
    """Detected texts of components referenced by relational tiles. Facts, not scores."""
    spec = COMPONENTS[key]
    needed = {dep for tile in spec["tiles"] for dep in tile.get("context_needs", [])}
    context = {}
    for dep in sorted(needed):
        dep_block = tldr.get(COMPONENTS[dep]["tldr_key"]) or {}
        if isinstance(dep_block, dict) and dep_block.get("detected") and dep_block.get("content"):
            context[dep] = _block_content_text(dep_block)
        else:
            context[dep] = "(no detectado)"
    return context


def _signals_lines(signals: list[dict[str, Any]]) -> str:
    if not signals:
        return "(none)"
    lines = []
    for signal in signals:
        if signal.get("feature") == "sv9_flow_tile_signal":
            refs = signal.get("evidence_refs") or []
            refs_text = ", ".join(str(ref) for ref in refs) if isinstance(refs, list) else str(refs)
            line = (
                f"- SV9 Flow tile signal: tile={signal.get('tile')} "
                f"effect={signal.get('effect') or signal.get('value')} "
                f"confidence={signal.get('confidence')} source={signal.get('source')} "
                f"refs={refs_text}"
            )
            rationale = signal.get("rationale") or signal.get("detail")
            if rationale:
                line += f"\n  rationale: {rationale}"
            lines.append(line)
            continue
        value = signal.get("value")
        confidence = signal.get("confidence")
        line = (
            f"- {signal.get('feature')} (legacy {signal.get('legacy_dimension')}): "
            f"value={value} confidence={confidence}"
        )
        detail = signal.get("detail")
        if detail:
            line += f"\n  observations: {detail}"
        lines.append(line)
    return "\n".join(lines)


def _json_shape_hint(key: str) -> str:
    first = COMPONENTS[key]["tiles"][0]["id"]
    veredicto = ', "veredicto": "frase de síntesis"' if key == "coherencia" else ""
    return (
        '{"componente": "%s", "message": "lectura editorial breve en español", "baldosas": ['
        '{"id": "%s", "estado": "ok|no|sin_evidencia", '
        '"evidencia": "cita literal (si ok)", '
        '"motivo": "por qué (si no/sin_evidencia)", '
        '"contexto_requerido": "opcional, solo en sin_evidencia"}, '
        "... una por cada id]%s}" % (key, first, veredicto)
    )


def _build_component_prompt(
    key: str,
    *,
    block: dict[str, Any],
    signals: list[dict[str, Any]],
    tldr: dict[str, Any],
    brand_name: str,
    url: str,
) -> str:
    spec = COMPONENTS[key]
    context = _context_texts(key, tldr)
    context_section = (
        "\n".join(f"- {COMPONENTS[dep]['label']}: {text}" for dep, text in context.items())
        if context
        else "(not needed for these tiles)"
    )
    evidence_quotes = block.get("evidence") or []
    evidence_section = (
        "\n".join(f"- {quote}" for quote in evidence_quotes) if evidence_quotes else "(none)"
    )
    limitations = block.get("limitations") or []
    limitations_section = (
        "\n".join(f"- {limitation}" for limitation in limitations) if limitations else "(none)"
    )
    return f"""Brand: {brand_name}
URL: {url}
Componente: {spec['label']} ({key})
Pregunta del componente: {spec['question']}

LECTURA DETECTADA (Pase 1, basada en evidencia):
content: {_block_content_text(block) or "(no detectado)"}
mode: {block.get('mode')}
confidence: {block.get('confidence')}
rationale: {block.get('rationale')}

CITAS DE EVIDENCIA:
{evidence_section}

LIMITACIONES DE EVIDENCIA:
{limitations_section}

SEÑALES AUXILIARES (terceros / cross-surface):
{_signals_lines(signals)}

REGLA PARA SEÑALES SV9 FLOW:
Las señales `sv9_flow_tile_signal` son específicas por baldosa. Un `supports` en una baldosa no enciende otras baldosas por arrastre. Un `insufficient_evidence` de alta confianza en una baldosa exige `sin_evidencia`. Un `weakens` de alta confianza exige `no`. Estas señales negativas se aplican como guardarraíl determinista.

CONTEXTO FACTUAL DE OTROS COMPONENTES (textos, no juicios):
{context_section}

RÚBRICA DE BALDOSAS (evalúa cada una de forma independiente):
{_tiles_lines(key)}

Devuelve JSON: {_json_shape_hint(key)}"""


def _build_coherencia_prompt(
    *,
    components: dict[str, ComponentResult],
    tldr: dict[str, Any],
    signals: list[dict[str, Any]],
    brand_name: str,
    url: str,
    literal_sources: list[str],
) -> str:
    spec = COMPONENTS["coherencia"]
    pieces = []
    for key in PRESENTATION_ORDER:
        if key == "coherencia":
            continue
        component = components.get(key)
        if component is not None and component.status == STATUS_SCORED and component.detected_content:
            pieces.append(f"- {COMPONENTS[key]['label']}: {component.detected_content}")
        else:
            block = tldr.get(COMPONENTS[key]["tldr_key"]) or {}
            text = (
                _block_content_text(block)
                if isinstance(block, dict) and block.get("detected") and block.get("content")
                else "(no detectado)"
            )
            pieces.append(f"- {COMPONENTS[key]['label']}: {text}")
    return f"""Brand: {brand_name}
URL: {url}
Componente: Coherencia
Pregunta del componente: {spec['question']}

EJE INTERNO — LAS 9 PIEZAS SOBRE LA MESA (textos detectados):
{chr(10).join(pieces)}

EJE EXTERNO — SEÑALES DE CONSISTENCIA CROSS-SURFACE:
{_signals_lines(signals)}

CITAS DE EVIDENCIA DISPONIBLES:
{chr(10).join(f"- {quote}" for quote in literal_sources) if literal_sources else "(none)"}

RÚBRICA DE BALDOSAS (evalúa cada una de forma independiente):
{_tiles_lines('coherencia')}

Devuelve JSON: {_json_shape_hint('coherencia')}"""
