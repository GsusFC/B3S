"""SV9 editorial layer: founder-facing prose conditioned on the verdict.

The inviolable order is number first, prose after: generation receives the
score, the first off tile (baldosa apagada), and the brand's own evidence as
hard constraints — it can explain the score, never move it. Same rubric
underneath, personalized voice on top.

Messages are presentation, not scoring: a generation failure leaves the
component without a message and never touches scores or statuses.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from src.sv9.language_guard import spanish_generated_text
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER

SV9_EDITORIAL_PROMPT_VERSION = "sv9-editorial-v0.2"
SV9_EDITORIAL_TIMEOUT_SECONDS = 60
SV9_EDITORIAL_MAX_WORKERS = 4

_MESSAGE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
}

_READING_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reading": {"type": "string"}},
    "required": ["reading"],
}

_MESSAGE_SYSTEM_PROMPT = """Escribes la línea de diagnóstico que ve un fundador en el Brand3 Scanner.

Voz FLOC*: directa, concreta, en castellano, sin lenguaje de consultor, sin
adulación y sin alarmismo. Hablas de la marca en tercera persona.

Reglas duras:
- El veredicto es definitivo. Nunca contradigas el score ni insinúes que
  debería ser otro.
- No escribas un resumen descriptivo. Escribe un diagnóstico: tesis concreta,
  tensión estratégica y coste de no resolverla.
- Máximo dos frases. Primera: por qué la marca está donde está, usando su
  propia evidencia (cita o parafrasea lo detectado). Segunda: qué tensión
  limita el siguiente salto y qué ajuste observable exigiría.
- Si el componente no fue detectado, di qué significa ese vacío para esta
  marca concreta y qué requeriría el primer peldaño.
- No inventes datos, menciones ni logros que no estén en el material.
- Evita fórmulas blandas y repetidas: "se posiciona como", "ha consolidado",
  "su fuerza reside", "para avanzar debe", "comunica con claridad",
  "mantener este nivel exige".
- No uses la palabra "peldaño" ni menciones la rúbrica o el motor: el
  fundador ve un diagnóstico, no la maquinaria.
- Devuelve JSON estricto: {"message": "..."}
"""

_READING_SYSTEM_PROMPT = """Escribes la lectura ejecutiva de un scan del Brand3 Scanner.

Voz FLOC*: directa, concreta, en castellano, sin lenguaje de consultor.

Reglas duras:
- El scorecard es definitivo: no contradigas ningún score.
- Tres frases como máximo. La historia, no la lista: tesis de marca, tensión
  principal y coste inmediato de no resolver el hueco más doloroso.
- Usa la evidencia detectada de la marca, no generalidades.
- No abras con "se posiciona como" salvo que sea la forma más precisa de
  describir la evidencia. Evita frases comodín como "su fuerza reside" o
  "para avanzar debe".
- No menciones el motor, la rúbrica, dimensiones internas ni jerga de
  pipeline.
- Devuelve JSON estricto: {"reading": "..."}
"""


def build_editorial(
    scan: dict[str, Any],
    *,
    llm: Any,
    component_keys: list[str] | tuple[str, ...] | set[str] | None = None,
    include_executive_reading: bool = True,
) -> dict[str, Any]:
    """Generate per-component messages and the executive reading for a scan.

    `scan` accepts both shapes: Sv9ScanResult.to_dict() (components as dict)
    and the persisted Sv9Store.get_scan payload (components as list).
    Returns {"component_messages": {key: str}, "executive_reading": str|None};
    components that fail generation are simply absent.
    """
    if llm is None or not getattr(llm, "api_key", None):
        return {"component_messages": {}, "executive_reading": None}

    components = _normalize_components(scan)
    requested = {str(key) for key in component_keys} if component_keys is not None else None
    keys = [key for key in PRESENTATION_ORDER if key in components and (requested is None or key in requested)]

    def _one(key: str) -> tuple[str, str | None]:
        return key, _component_message(scan, key, components[key], llm)

    messages: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=SV9_EDITORIAL_MAX_WORKERS) as pool:
        for key, message in pool.map(_one, keys):
            if message:
                messages[key] = message

    return {
        "component_messages": messages,
        "executive_reading": _executive_reading(scan, components, llm) if include_executive_reading else None,
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


def _component_message(
    scan: dict[str, Any],
    key: str,
    component: dict[str, Any],
    llm: Any,
) -> str | None:
    status = str(component.get("status") or "")
    if status == "not_evaluated":
        return None  # technical failure: nothing to narrate, retry the scan

    spec = COMPONENTS[key]
    score = int(component.get("score") or 0)
    detected = str(component.get("detected_content") or "").strip()
    tiles_by_id = {tile["id"]: tile for tile in spec["tiles"]}

    lit_evidence = []
    failed_criterion = None
    for verdict in component.get("tile_profile") or []:
        verdict = verdict if isinstance(verdict, dict) else verdict.to_dict()
        estado = str(verdict.get("estado") or "")
        tile = tiles_by_id.get(str(verdict.get("id") or ""))
        if estado == "ok" and verdict.get("evidencia"):
            lit_evidence.append(str(verdict["evidencia"]))
        elif failed_criterion is None and estado == "no" and tile:
            # The work plan is the off tiles; blind spots are not failures.
            failed_criterion = tile["condition"]

    evidence_section = "\n".join(f"- {quote}" for quote in lit_evidence[:6]) or "(ninguna)"
    user = f"""Marca: {scan.get('brand_name')} ({scan.get('url')})
Componente: {spec['label']}
Pregunta del componente: {spec['question']}
Score (definitivo): {score}/{spec['scale']}
Estado: {"no detectado" if status == "not_detected" else "evaluado"}

LO DETECTADO:
{detected or "(no detectado)"}

EVIDENCIA QUE SOSTIENE EL SCORE:
{evidence_section}

LO QUE EXIGIRÍA SUBIR (criterio interno, no lo cites literalmente):
{failed_criterion or "(está en el techo: reconoce lo conseguido sin inflarlo)"}

Escribe el mensaje. JSON estricto: {{"message": "..."}}"""

    try:
        raw = llm._call_json(
            _MESSAGE_SYSTEM_PROMPT,
            user,
            json_schema=_MESSAGE_JSON_SCHEMA,
            schema_name=f"sv9_editorial_{key}",
            timeout_seconds=SV9_EDITORIAL_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    message = str((raw or {}).get("message") or "").strip()
    return spanish_generated_text(message) or None


def _executive_reading(
    scan: dict[str, Any],
    components: dict[str, dict[str, Any]],
    llm: Any,
) -> str | None:
    lines = []
    for key in PRESENTATION_ORDER:
        component = components.get(key)
        if component is None:
            continue
        spec = COMPONENTS[key]
        detected = str(component.get("detected_content") or "").strip()
        lines.append(
            f"- {spec['label']}: {component.get('score', 0)}/{spec['scale']}"
            f" · {component.get('status')}"
            + (f" · «{detected[:140]}»" if detected else "")
        )

    gap = scan.get("most_painful_gap")
    gap_label = COMPONENTS[gap]["label"] if gap in COMPONENTS else "(ninguno)"
    user = f"""Marca: {scan.get('brand_name')} ({scan.get('url')})
Brand3 Score (definitivo): {scan.get('brand3_score')}/100
Tope de Magnetism aplicado: {"sí" if scan.get('magnetism_capped') else "no"}
Hueco más doloroso: {gap_label}
Margen inmediato: +{scan.get('immediate_margin')} puntos

SCORECARD:
{chr(10).join(lines)}

Escribe la lectura ejecutiva. JSON estricto: {{"reading": "..."}}"""

    try:
        raw = llm._call_json(
            _READING_SYSTEM_PROMPT,
            user,
            json_schema=_READING_JSON_SCHEMA,
            schema_name="sv9_editorial_reading",
            timeout_seconds=SV9_EDITORIAL_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    reading = str((raw or {}).get("reading") or "").strip()
    return spanish_generated_text(reading) or None
