#!/usr/bin/env python3
"""Compare a public SV9 export against a local B3S report JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COMPONENT_ALIASES = {
    "mision": "mission",
    "mission": "mission",
    "vision": "vision",
    "valores": "values",
    "values": "values",
    "atributos": "attributes",
    "attributes": "attributes",
    "propuesta de valor": "value_proposition",
    "value proposition": "value_proposition",
    "personalidad / arquetipo": "personality",
    "personalidad": "personality",
    "personality": "personality",
    "idea de marca": "brand_idea",
    "brand idea": "brand_idea",
    "proposito": "core_purpose",
    "core purpose": "core_purpose",
    "magnetism": "magnetism",
    "magnetismo": "magnetism",
    "coherencia": "coherencia",
    "coherence": "coherencia",
}

COMPONENT_ORDER = (
    "mission",
    "vision",
    "values",
    "attributes",
    "value_proposition",
    "personality",
    "brand_idea",
    "core_purpose",
    "magnetism",
    "coherencia",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-url", required=True, help="Public SV9 export.md URL or local markdown file.")
    parser.add_argument("--local-report", required=True, help="Local B3S report JSON path.")
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    remote_text = load_text(args.remote_url, timeout=args.timeout)
    local_report = json.loads(Path(args.local_report).read_text(encoding="utf-8"))
    payload = compare_remote_local(
        parse_remote_export(remote_text, source=args.remote_url),
        normalize_local_report(local_report, source=args.local_report),
    )
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    md_text = render_markdown(payload)

    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(json_text + "\n", encoding="utf-8")
    if args.output_md:
        Path(args.output_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_md).write_text(md_text + "\n", encoding="utf-8")

    if not args.output_json and not args.output_md:
        print(md_text)
    else:
        if args.output_json:
            print(f"JSON: {args.output_json}")
        if args.output_md:
            print(f"Markdown: {args.output_md}")
    return 0


def load_text(source: str, *, timeout: int = 30) -> str:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "B3S-SV9-compare/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    return Path(source).read_text(encoding="utf-8")


def parse_remote_export(text: str, *, source: str = "") -> dict[str, Any]:
    lines = text.splitlines()
    brand_name = ""
    url = ""
    score: float | None = None
    if lines and lines[0].startswith("# "):
        brand_name = lines[0].split("—", 1)[-1].strip() if "—" in lines[0] else lines[0].lstrip("# ").strip()
    for line in lines[:20]:
        if line.startswith("- URL:"):
            url = line.split(":", 1)[1].strip()
        match = re.search(r"Brand3 Score:\s+\*\*([0-9.]+)/100\*\*", line)
        if match:
            score = float(match.group(1))

    sections: list[tuple[str, int, int]] = []
    heading_rows = [(idx, line[3:].strip()) for idx, line in enumerate(lines) if line.startswith("## ")]
    for pos, (start, heading) in enumerate(heading_rows):
        end = heading_rows[pos + 1][0] if pos + 1 < len(heading_rows) else len(lines)
        key = component_key(heading)
        if key:
            sections.append((key, start, end))

    components: dict[str, Any] = {}
    for key, start, end in sections:
        section = lines[start:end]
        components[key] = parse_remote_component(key, section)

    return {
        "source": source,
        "brand_name": brand_name,
        "url": url,
        "score": score,
        "components": components,
        "raw_contract": "public_export_markdown",
        "limitations": [
            "Remote public export exposes verdicts, scores, off tiles, and blind spots; it does not expose raw acquisition refs."
        ],
    }


def parse_remote_component(key: str, section: list[str]) -> dict[str, Any]:
    title = section[0][3:].strip() if section else key
    body = "\n".join(section)
    score = None
    scale = None
    confidence = ""
    score_match = re.search(r"- Nota:\s+\*\*([0-9.]+)/([0-9.]+)\*\*(?:\s+×([0-9.]+))?", body)
    points_match = re.search(r"\(([0-9.]+)/([0-9.]+) pts\)", body)
    if score_match:
        score = float(score_match.group(1))
        scale = float(score_match.group(2))
    if points_match:
        score_points = float(points_match.group(1))
        scale_points = float(points_match.group(2))
    else:
        score_points = score
        scale_points = scale
    confidence_match = re.search(r"- Nota:[^\n]*·\s*confianza\s+([^\n]+)", body, re.IGNORECASE)
    if confidence_match:
        confidence = confidence_match.group(1).strip()

    lead_quote = ""
    verdict_lines: list[str] = []
    for line in section[1:]:
        if line.startswith("- Nota:") or line.startswith("### "):
            break
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(">"):
            lead_quote = stripped.lstrip("> ").strip()
        else:
            verdict_lines.append(stripped)

    return {
        "key": key,
        "label": title,
        "score": score,
        "scale": scale,
        "score_points": score_points,
        "scale_points": scale_points,
        "detected": bool(score and score > 0),
        "confidence": confidence,
        "summary": lead_quote,
        "verdict": " ".join(verdict_lines),
        "off_tiles": parse_tile_list(section, "Baldosas apagadas"),
        "blind_spots": parse_tile_list(section, "Puntos ciegos"),
    }


def parse_tile_list(section: list[str], heading: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    active = False
    for line in section:
        if line.startswith("### "):
            active = normalize(line[4:].strip()).startswith(normalize(heading))
            continue
        if not active or not line.startswith("- "):
            continue
        if "(ningun" in normalize(line) or "(ninguna" in normalize(line):
            continue
        match = re.match(r"- \*\*([^*]+)\*\*\s+[—-]\s+(.+)", line)
        if match:
            tile_label = match.group(1).strip()
            tile_id, _, name = tile_label.partition("·")
            out.append({"id": tile_id.strip(), "name": name.strip() or tile_label, "reason": match.group(2).strip()})
    return out


def normalize_local_report(report: dict[str, Any], *, source: str = "") -> dict[str, Any]:
    components = {}
    for component in report.get("components") or []:
        key = str(component.get("key") or "")
        if not key:
            continue
        block = component.get("block") or {}
        refs = block.get("refs") or []
        components[key] = {
            "key": key,
            "label": component.get("label") or key,
            "score": number_or_none(component.get("score")),
            "scale": number_or_none(component.get("scale")),
            "status": component.get("status") or "",
            "detected": component.get("status") != "not_detected",
            "confidence": component.get("confidence") or block.get("confidence") or "",
            "summary": component.get("resumen") or "",
            "verdict": component.get("veredicto") or "",
            "lit": component.get("lit"),
            "off": component.get("off"),
            "blind": component.get("blind"),
            "off_tiles": local_tiles(component, state="no"),
            "blind_spots": local_tiles(component, state="sin_evidencia"),
            "block": {
                "detected": block.get("detected"),
                "coverage_status": block.get("coverage_status") or "",
                "provenance_source": block.get("provenance_source") or "",
                "content": block.get("content") or "",
                "rejected_content": block.get("rejected_content") or "",
                "ref_count": len(refs),
                "refs": [
                    {
                        "ref": ref.get("ref") or "",
                        "url": ref.get("url") or "",
                        "snippet": compact(ref.get("snippet") or "", 240),
                    }
                    for ref in refs[:5]
                ],
            },
        }

    return {
        "source": source,
        "id": report.get("id") or "",
        "brand_name": report.get("brand_name") or "",
        "url": report.get("url") or "",
        "score": report.get("score"),
        "created_at": report.get("created_at") or "",
        "not_detected": report.get("not_detected") or [],
        "acquisition_gate": report.get("acquisition_gate") or {},
        "components": components,
    }


def compare_remote_local(remote: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for key in COMPONENT_ORDER:
        remote_component = remote["components"].get(key)
        local_component = local["components"].get(key)
        if not remote_component and not local_component:
            continue
        rows.append(compare_component(key, remote_component, local_component))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "remote": {
            "source": remote["source"],
            "brand_name": remote["brand_name"],
            "url": remote["url"],
            "score": remote["score"],
            "limitations": remote["limitations"],
        },
        "local": {
            "source": local["source"],
            "id": local["id"],
            "brand_name": local["brand_name"],
            "url": local["url"],
            "score": local["score"],
            "created_at": local["created_at"],
            "not_detected": local["not_detected"],
            "acquisition_gate": local["acquisition_gate"],
        },
        "summary": summarize(rows, remote, local),
        "components": rows,
    }


def compare_component(
    key: str,
    remote_component: dict[str, Any] | None,
    local_component: dict[str, Any] | None,
) -> dict[str, Any]:
    remote_score = number_or_none((remote_component or {}).get("score"))
    local_score = number_or_none((local_component or {}).get("score"))
    remote_scale = number_or_none((remote_component or {}).get("scale"))
    local_scale = number_or_none((local_component or {}).get("scale"))
    remote_norm = normalized_score(remote_score, remote_scale)
    local_norm = normalized_score(local_score, local_scale)
    delta = None if remote_norm is None or local_norm is None else round(local_norm - remote_norm, 2)
    cause = classify_gap(remote_component, local_component, delta)
    return {
        "key": key,
        "label": (local_component or remote_component or {}).get("label") or key,
        "remote": remote_component,
        "local": local_component,
        "delta_normalized_points": delta,
        "gap_class": cause["gap_class"],
        "probable_cause": cause["probable_cause"],
        "recommended_next_step": cause["recommended_next_step"],
    }


def classify_gap(
    remote_component: dict[str, Any] | None,
    local_component: dict[str, Any] | None,
    delta: float | None,
) -> dict[str, str]:
    if not remote_component:
        return gap("remote_missing_component", "El export remoto no contiene este componente.", "No usar este caso como contrato para este componente.")
    if not local_component:
        return gap("local_missing_component", "El JSON local no contiene este componente.", "Revisar composición del reporte local.")

    block = local_component.get("block") or {}
    remote_detected = bool(remote_component.get("detected"))
    local_detected = bool(local_component.get("detected")) and local_component.get("status") != "not_detected"
    coverage = block.get("coverage_status") or ""
    provenance = block.get("provenance_source") or ""
    ref_count = int(block.get("ref_count") or 0)

    if remote_detected and not local_detected:
        if provenance == "gate_rejected":
            return gap(
                "local_false_negative_gate",
                "Remoto detecta el bloque, pero local lo rechaza en gate determinista.",
                "Reproducir las refs locales con adjudicador LLM y convertir el caso remoto en contrato de calibración si la cita existe.",
            )
        if coverage == "insufficient_acquisition" and ref_count == 0:
            return gap(
                "local_acquisition_missing",
                "Remoto detecta el bloque y local no conserva refs útiles.",
                "Comparar acquisition trace y añadir fallback/captura antes de tocar scoring.",
            )
        return gap(
            "local_interpretation_miss",
            "Remoto detecta el bloque y local no, con alguna evidencia local disponible o rechazo no trazado.",
            "Guardar el pack de evidencia y evaluar el bloque con LLM adjudicador antes de añadir reglas.",
        )

    if delta is not None and delta <= -30:
        if int(local_component.get("blind") or 0) >= 3:
            return gap(
                "local_tile_blind_spot_gap",
                "Local detecta el bloque, pero apaga demasiadas baldosas como sin evidencia.",
                "Comparar baldosas remotas/locales y mover el caso a Scoring Lab como candidato de gate/tile false negative.",
            )
        return gap(
            "local_under_scores",
            "Local detecta el bloque pero puntúa mucho más bajo.",
            "Auditar el prompt de scoring del componente contra el veredicto remoto.",
        )

    if delta is not None and delta >= 30:
        return gap(
            "local_over_scores",
            "Local puntúa claramente por encima del remoto.",
            "Revisar si local está aceptando evidencia débil o visual contaminada.",
        )

    if provenance == "gate_rejected":
        return gap(
            "local_gate_pressure",
            "Aunque la diferencia no sea extrema, el bloque local pasó por rechazo de gate.",
            "Mantener trazado y revisar con adjudicador si aparece en casos repetidos.",
        )

    return gap("aligned_or_minor_delta", "No hay divergencia material en este componente.", "No priorizar.")


def summarize(rows: list[dict[str, Any]], remote: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    critical = [row for row in rows if row["gap_class"] in {"local_false_negative_gate", "local_acquisition_missing", "local_interpretation_miss"}]
    large = [row for row in rows if row["gap_class"] in {"local_tile_blind_spot_gap", "local_under_scores", "local_over_scores"}]
    remote_score = number_or_none(remote.get("score"))
    local_score = number_or_none(local.get("score"))
    return {
        "score_delta_local_minus_remote": None if remote_score is None or local_score is None else round(local_score - remote_score, 2),
        "critical_gap_count": len(critical),
        "large_score_gap_count": len(large),
        "priority_components": [row["key"] for row in critical + large],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# SV9 remoto vs local",
        "",
        f"- Remoto: {payload['remote']['brand_name']} · {payload['remote']['score']}/100 · {payload['remote']['source']}",
        f"- Local: {payload['local']['brand_name']} · {payload['local']['score']}/100 · {payload['local']['source']}",
        f"- Delta local-remoto: {payload['summary']['score_delta_local_minus_remote']}",
        "- Limitación: el export remoto público no expone raw refs; se compara decisión/veredicto remoto contra refs locales.",
        "",
        "## Gaps",
        "",
        "| Componente | Remoto | Local | Delta norm. | Clase | Causa probable |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in payload["components"]:
        remote = row.get("remote") or {}
        local = row.get("local") or {}
        lines.append(
            "| {label} | {remote_score} | {local_score} | {delta} | `{gap}` | {cause} |".format(
                label=row["label"],
                remote_score=score_label(remote),
                local_score=score_label(local),
                delta=row["delta_normalized_points"],
                gap=row["gap_class"],
                cause=row["probable_cause"],
            )
        )

    for row in payload["components"]:
        if row["gap_class"] == "aligned_or_minor_delta":
            continue
        remote = row.get("remote") or {}
        local = row.get("local") or {}
        block = local.get("block") or {}
        lines.extend(
            [
                "",
                f"## {row['label']}",
                "",
                f"- Siguiente paso: {row['recommended_next_step']}",
                f"- Remoto: {score_label(remote)} · confianza {remote.get('confidence') or 'n/a'}",
                f"- Local: {score_label(local)} · {local.get('status') or 'n/a'} · coverage `{block.get('coverage_status') or 'n/a'}` · provenance `{block.get('provenance_source') or 'n/a'}` · refs {block.get('ref_count') or 0}",
            ]
        )
        if remote.get("verdict"):
            lines.append(f"- Veredicto remoto: {compact(remote['verdict'], 360)}")
        if local.get("verdict") or local.get("summary"):
            lines.append(f"- Veredicto local: {compact(local.get('verdict') or local.get('summary') or '', 360)}")
        if block.get("rejected_content"):
            lines.append(f"- Rechazo local: {compact(block['rejected_content'], 360)}")
        refs = block.get("refs") or []
        if refs:
            lines.append("- Refs locales:")
            for ref in refs[:3]:
                lines.append(f"  - `{ref['ref']}` {ref['url']}: {compact(ref['snippet'], 220)}")
        remote_off = remote.get("off_tiles") or []
        remote_blind = remote.get("blind_spots") or []
        if remote_off or remote_blind:
            lines.append(f"- Baldosas remotas apagadas: {tile_ids(remote_off) or 'ninguna'}; puntos ciegos: {tile_ids(remote_blind) or 'ninguno'}")
        local_off = local.get("off_tiles") or []
        local_blind = local.get("blind_spots") or []
        if local_off or local_blind:
            lines.append(f"- Baldosas locales apagadas: {tile_ids(local_off) or 'ninguna'}; puntos ciegos: {tile_ids(local_blind) or 'ninguno'}")
    return "\n".join(lines)


def component_key(label: str) -> str:
    return COMPONENT_ALIASES.get(normalize(label), "")


def normalize(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_value.lower().strip())


def local_tiles(component: dict[str, Any], *, state: str) -> list[dict[str, str]]:
    out = []
    for tile in component.get("tiles") or []:
        if tile.get("estado") == state:
            out.append(
                {
                    "id": str(tile.get("id") or ""),
                    "name": str(tile.get("name") or ""),
                    "reason": str(tile.get("motivo") or ""),
                    "evidence": str(tile.get("evidencia") or ""),
                }
            )
    return out


def score_label(component: dict[str, Any]) -> str:
    score = component.get("score")
    scale = component.get("scale")
    if score is None or scale is None:
        return "n/a"
    return f"{score:g}/{scale:g}"


def normalized_score(score: float | None, scale: float | None) -> float | None:
    if score is None or not scale:
        return None
    return round((score / scale) * 100, 2)


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compact(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def tile_ids(tiles: list[dict[str, str]]) -> str:
    return ", ".join(tile.get("id") or "?" for tile in tiles)


def gap(gap_class: str, probable_cause: str, recommended_next_step: str) -> dict[str, str]:
    return {
        "gap_class": gap_class,
        "probable_cause": probable_cause,
        "recommended_next_step": recommended_next_step,
    }


if __name__ == "__main__":
    raise SystemExit(main())
