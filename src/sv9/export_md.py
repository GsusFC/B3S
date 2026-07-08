"""SV9 .md export (baldosas v3.1).

Per component, two clearly separated lists (prompt técnico step 6):
- Baldosas apagadas → plan de trabajo (each maps to a build deliverable).
- Puntos ciegos → contexto pendiente (the FLOC* conversation hook).

The export is presentation over a persisted scan payload (Sv9Store.get_scan
shape, components as a list). It never recomputes scores.
"""

from __future__ import annotations

from typing import Any

from src.sv9.language_guard import (
    spanish_component_verdict,
    spanish_generated_text,
    spanish_reason_labels,
    spanish_status_label,
    spanish_tile_contexto,
    spanish_tile_motivo,
)
from src.sv9.rubric import (
    COMPONENTS,
    ESTADO_NO,
    ESTADO_OK,
    ESTADO_SIN_EVIDENCIA,
    PRESENTATION_ORDER,
    component_max_points,
)


def _tiles_by_id(component_key: str) -> dict[str, dict[str, str]]:
    return {tile["id"]: tile for tile in COMPONENTS[component_key]["tiles"]}


def _components_by_key(scan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = scan.get("components")
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if isinstance(v, dict)}
    by_key: dict[str, dict[str, Any]] = {}
    for item in raw or []:
        if isinstance(item, dict) and item.get("component") in COMPONENTS:
            by_key[item["component"]] = item
    return by_key


def build_scan_markdown(scan: dict[str, Any], *, lang: str = "es") -> str:
    """Render the scan as a Brand3 .md report."""
    components = _components_by_key(scan)
    brand = scan.get("display_name") or scan.get("brand_name") or "(marca)"
    url = scan.get("url") or ""
    model = scan.get("model") or scan.get("rubric_version") or ""

    lines: list[str] = []
    lines.append(f"# Brand3 Scanner — {brand}")
    lines.append("")
    if url:
        lines.append(f"- URL: {url}")
    lines.append(f"- Brand3 Score: **{scan.get('brand3_score', 0)}/100**")
    lines.append(f"- Modelo: {model}")
    if scan.get("reliability_status"):
        lines.append(f"- Confiabilidad: **{spanish_status_label(scan.get('reliability_status'))}**")
        reason_codes = scan.get("reliability_reason_codes") or []
        if reason_codes:
            lines.append(f"- Razones de confiabilidad: {', '.join(spanish_reason_labels(reason_codes))}")
    if scan.get("canonical_status"):
        lines.append(f"- Canonicidad: **{spanish_status_label(scan.get('canonical_status'))}**")
        reason_codes = scan.get("canonical_reason_codes") or []
        if reason_codes:
            lines.append(f"- Razones de canonicidad: {', '.join(spanish_reason_labels(reason_codes))}")
    if scan.get("magnetism_capped"):
        lines.append("- Tope de Magnetism aplicado: sí")
    reading = spanish_generated_text(scan.get("executive_reading"))
    if reading:
        lines.append("")
        lines.append(f"> {reading}")
    lines.append("")

    for key in PRESENTATION_ORDER:
        component = components.get(key)
        if component is None:
            continue
        spec = COMPONENTS[key]
        tiles = _tiles_by_id(key)
        score = int(component.get("score") or 0)
        status = str(component.get("status") or "")
        confidence = component.get("confidence") or "alta"
        multiplier = " ×2" if spec["multiplier"] == 2 else ""

        lines.append(f"## {spec['label']}")
        veredicto = spanish_component_verdict(key, component.get("veredicto"), component.get("tile_profile") or [])
        if veredicto:
            lines.append("")
            lines.append(f"> {veredicto}")
        message = spanish_generated_text(component.get("message"))
        if message and message != veredicto:
            lines.append("")
            lines.append(message)
        if status == "not_evaluated":
            lines.append("")
            lines.append(f"_Fallo técnico ({component.get('error') or 'not_evaluated'}). Reintenta el scan._")
            lines.append("")
            continue
        if status == "not_detected":
            lines.append("")
            lines.append(f"_No detectado._ {spec['level_zero']}")
            lines.append("")
            continue

        lines.append("")
        lines.append(
            f"- Nota: **{score}/{spec['scale']}**{multiplier} "
            f"({component.get('points', 0)}/{component_max_points(key)} pts) · confianza {confidence}"
        )

        verdicts = {
            str(verdict.get("id") or verdict.get("tile_id") or ""): verdict
            for verdict in component.get("tile_profile") or []
            if isinstance(verdict, dict)
        }
        off_tiles = []
        blind_spots = []
        missing_tiles = []
        for tile_id, tile in tiles.items():
            verdict = verdicts.get(tile_id)
            if verdict is None:
                missing_tiles.append(tile)
                continue
            estado = str(verdict.get("estado") or "")
            if estado == ESTADO_NO:
                off_tiles.append((tile, verdict))
            elif estado == ESTADO_SIN_EVIDENCIA:
                blind_spots.append((tile, verdict))

        lines.append("")
        lines.append("### Baldosas apagadas (plan de trabajo)")
        if off_tiles:
            for tile, _verdict in off_tiles:
                lines.append(f"- **{tile['id']} · {tile['name']}** — {tile['condition']}")
        else:
            lines.append("- (ninguna: todas las baldosas evaluables están encendidas)")

        if missing_tiles:
            lines.append("")
            lines.append("### Baldosas sin veredicto persistido")
            for tile in missing_tiles:
                lines.append(f"- **{tile['id']} · {tile['name']}** — {tile['condition']}")

        lines.append("")
        lines.append("### Puntos ciegos (contexto pendiente)")
        if blind_spots:
            for tile, verdict in blind_spots:
                detail = f"- **{tile['id']} · {tile['name']}** — {tile['condition']}"
                motivo = spanish_tile_motivo(verdict.get("motivo"), estado=verdict.get("estado"))
                if motivo:
                    detail += f"\n  - motivo: {motivo}"
                contexto = spanish_tile_contexto(verdict.get("contexto_requerido"))
                if contexto:
                    detail += f"\n  - aporta contexto: {contexto}"
                lines.append(detail)
        else:
            lines.append("- (ninguno)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
