"""SV9 .md export (baldosas v3.1).

Per component, two clearly separated lists (prompt técnico step 6):
- Baldosas apagadas → plan de trabajo (each maps to a build deliverable).
- Puntos ciegos → contexto pendiente (the FLOC* conversation hook).

The export is presentation over a persisted scan payload (Sv9Store.get_scan
shape, components as a list). It never recomputes scores.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.services.scanner_score_publication import score_publication_from_report
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
    score_publishable = bool(
        score_publication_from_report(scan)["publishable"]
    )
    if score_publishable:
        lines.append(f"- Brand3 Score: **{scan.get('brand3_score', 0)}/100**")
    else:
        lines.append("- Brand3 Score: **retenido (evaluación no canónica)**")
    lines.append(f"- Modelo: {model}")
    analysis_contract = (
        scan.get("analysis_contract")
        if isinstance(scan.get("analysis_contract"), dict)
        else {}
    )
    if analysis_contract.get("rubric_version"):
        lines.append(
            f"- Rúbrica: `{analysis_contract.get('rubric_version')}`"
        )
    if analysis_contract.get("interpretation_prompt_version"):
        lines.append(
            "- Prompt de interpretación: "
            f"`{analysis_contract.get('interpretation_prompt_version')}`"
        )
    if analysis_contract.get("evaluator_model"):
        lines.append(
            f"- Evaluador: `{analysis_contract.get('evaluator_model')}`"
        )
    lines.append(f"- Build: `{scan.get('pipeline_commit_sha') or 'unknown'}`")
    if scan.get("created_at"):
        lines.append(f"- Escaneado: {scan.get('created_at')}")
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

    stability = (
        scan.get("stability")
        if isinstance(scan.get("stability"), dict)
        else {}
    )
    stability_classification = str(stability.get("classification") or "")
    if stability_classification in {
        "acquisition_regression",
        "candidate",
        "comparison_error",
        "evaluation_drift",
        "contract_mismatch",
        "invalid",
    }:
        lines.append("## Estabilidad de la evaluación")
        lines.append("")
        if stability_classification == "evaluation_drift":
            lines.append(
                "**Resultado no canónico:** la evidencia material es equivalente "
                "al baseline, pero la interpretación o las baldosas cambiaron."
            )
        elif stability_classification == "contract_mismatch":
            lines.append(
                "**Resultado no comparable:** el contrato de evaluación no "
                "coincide con el baseline."
            )
        elif stability_classification == "acquisition_regression":
            lines.append(
                "**Regresión de adquisición:** este run no recuperó toda la "
                "evidencia observada anteriormente; su score queda retenido."
            )
        elif stability_classification == "candidate":
            lines.append(
                "**Evidencia candidata:** el corpus material cambió y necesita "
                "confirmación antes de sustituir el baseline."
            )
        elif stability_classification == "comparison_error":
            lines.append(
                "**Comparación fallida:** no se pudo validar este run contra "
                "el baseline; su score queda retenido."
            )
        else:
            lines.append(
                "**Resultado inválido:** el run no cumple el contrato mínimo "
                "de evaluación; su score queda retenido."
            )
        comparison = (
            stability.get("baseline_comparison")
            if isinstance(stability.get("baseline_comparison"), dict)
            else {}
        )
        if comparison.get("baseline_report_id"):
            lines.append(
                f"- Baseline: `{comparison.get('baseline_report_id')}`"
            )
        delta = (
            comparison.get("delta")
            if isinstance(comparison.get("delta"), dict)
            else {}
        )
        for changed in delta.get("changed_components") or []:
            if not isinstance(changed, dict):
                continue
            key = str(changed.get("component") or "")
            label = str((COMPONENTS.get(key) or {}).get("label") or key)
            tiles = [
                str(tile)
                for tile in changed.get("changed_tiles") or []
                if str(tile)
            ]
            suffix = f" · baldosas {', '.join(tiles)}" if tiles else ""
            lines.append(
                f"- {label}: **{changed.get('score_before')} → "
                f"{changed.get('score_after')}**{suffix}"
            )
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
            lines.append(f"> **Interpretación:** {veredicto}")
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
        if score_publishable:
            lines.append(
                f"- Nota: **{score}/{spec['scale']}**{multiplier} "
                f"({component.get('points', 0)}/{component_max_points(key)} pts) · confianza {confidence}"
            )
        else:
            lines.append(
                "- Nota: **retenida** · el valor bruto permanece en el "
                "registro de auditoría"
            )
        hierarchy = (
            component.get("surface_hierarchy")
            if isinstance(component.get("surface_hierarchy"), dict)
            else {}
        )
        hierarchy_status = str(hierarchy.get("hierarchy_status") or "")
        hierarchy_label = {
            "homepage": "evidencia visible en portada",
            "linked_from_home": "evidencia en páginas enlazadas desde la portada",
            "mixed_with_sitemap_only": "parte de la evidencia está fuera de navegación",
            "sitemap_only": "evidencia propia localizable solo mediante sitemap",
            "owned_unknown": "evidencia propia sin jerarquía verificada",
            "external_only": "evidencia citada únicamente en fuentes externas",
            "unlocated": "evidencia sin superficie localizable",
        }.get(hierarchy_status)
        if hierarchy_label:
            lines.append(f"- Jerarquía: **{hierarchy_label}**")
            counts = (
                hierarchy.get("counts")
                if isinstance(hierarchy.get("counts"), dict)
                else {}
            )
            if int(counts.get("sitemap_only") or 0):
                lines.append(
                    "- Superficies propias fuera de navegación: "
                    f"**{int(counts.get('sitemap_only') or 0)}**"
                )

        verdicts = {
            str(verdict.get("id") or verdict.get("tile_id") or ""): verdict
            for verdict in component.get("tile_profile") or []
            if isinstance(verdict, dict)
        }
        off_tiles = []
        blind_spots = []
        missing_tiles = []
        literal_quotes = []
        for tile_id, tile in tiles.items():
            verdict = verdicts.get(tile_id)
            if verdict is None:
                missing_tiles.append(tile)
                continue
            estado = str(verdict.get("estado") or "")
            if estado == ESTADO_OK and str(verdict.get("evidencia") or "").strip():
                literal_quotes.append(
                    (tile, str(verdict.get("evidencia") or "").strip())
                )
            elif estado == ESTADO_NO:
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
        lines.append("### Citas literales del snapshot")
        if literal_quotes:
            for tile, quote in literal_quotes:
                lines.append(
                    f"- **{tile['id']} · {tile['name']}** — “{quote}”"
                )
        else:
            lines.append("- (ninguna cita literal positiva persistida)")

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

    coverage = (
        scan.get("coverage_acquisition")
        if isinstance(scan.get("coverage_acquisition"), dict)
        else {}
    )
    owned = (
        coverage.get("owned_page_coverage")
        if isinstance(coverage.get("owned_page_coverage"), dict)
        else {}
    )
    known_count = int(owned.get("known_page_count") or 0)
    captured_count = int(
        owned.get("captured_page_count")
        or coverage.get("owned_url_count")
        or 0
    )
    if known_count or owned.get("visited_pages") or owned.get("not_visited_pages"):
        percentage = (
            int(round((captured_count / known_count) * 100))
            if known_count
            else 0
        )
        lines.append("## Cobertura de adquisición")
        lines.append("")
        if known_count:
            lines.append(
                f"- Páginas propias capturadas: **{captured_count} de "
                f"{known_count} ({percentage}%)**"
            )
        else:
            lines.append(
                f"- Páginas propias capturadas: **{captured_count}**"
            )
        eligible_not_visited = int(
            owned.get("eligible_not_visited_count")
            or sum(
                1
                for row in owned.get("not_visited_pages") or []
                if isinstance(row, dict)
                and str(row.get("reason") or "") == "page_budget"
            )
        )
        eligible_captured = int(
            owned.get("eligible_captured_page_count") or captured_count
        )
        eligible_attempted = max(
            captured_count,
            int(
                owned.get("attempted_page_count")
                or len(owned.get("visited_pages") or [])
            ),
        )
        eligible_count = int(
            owned.get("eligible_page_count")
            or eligible_attempted + eligible_not_visited
        )
        if eligible_count:
            eligible_percentage = int(
                round((eligible_captured / eligible_count) * 100)
            )
            lines.append(
                "- Páginas elegibles capturadas: "
                f"**{eligible_captured} de {eligible_count} "
                f"({eligible_percentage}%)**"
            )
        content_sampling = (
            coverage.get("content_sampling")
            if isinstance(coverage.get("content_sampling"), dict)
            else {}
        )
        if int(content_sampling.get("sampling_record_count") or 0):
            lines.append(
                "- Contenido conservado en páginas con muestreo parcial: "
                f"**{int(content_sampling.get('retained_chunk_count') or 0)} de "
                f"{int(content_sampling.get('available_chunk_count') or 0)} fragmentos**"
            )
            lines.append(
                "- Transparencia de contenido: **capturar una URL no implica "
                "conservar todo su texto**; se omitieron "
                f"{int(content_sampling.get('omitted_chunk_count') or 0)} fragmentos"
            )
        if owned.get("selection_version"):
            lines.append(
                f"- Política de selección: `{owned.get('selection_version')}`"
            )
        discovery_status = str(owned.get("discovery_status") or "")
        discovery_sources = [
            str(source)
            for source in owned.get("discovery_sources") or []
            if str(source).strip()
        ]
        if discovery_status:
            source_suffix = (
                f" ({', '.join(discovery_sources)})"
                if discovery_sources
                else ""
            )
            lines.append(
                f"- Enumeración de páginas: **{discovery_status}**{source_suffix}"
            )
        if discovery_status == "observed_only":
            lines.append(
                "- Límite de descubrimiento: **el porcentaje cubre solo páginas "
                "conocidas; el universo del sitio no fue verificado**"
            )
        if owned.get("latest_lastmod"):
            lines.append(
                f"- Último `lastmod` observado: {owned.get('latest_lastmod')}"
            )
            if _timestamp_is_newer(
                owned.get("latest_lastmod"), scan.get("created_at")
            ):
                lines.append(
                    "- Recencia: **el sitemap declara contenido posterior al "
                    "scan; conviene reescanear**"
                )
        language_detection = (
            owned.get("language_detection")
            if isinstance(owned.get("language_detection"), dict)
            else {}
        )
        language_labels = {
            "en": "inglés",
            "es": "español",
            "mixed_en_es": "mezcla inglés/español",
            "und": "indeterminado",
        }
        language_rows = [
            row
            for row in language_detection.get("distribution") or []
            if isinstance(row, dict)
            and str(row.get("language") or "") != "und"
            and int(row.get("page_count") or 0)
        ]
        if language_rows:
            summary = " · ".join(
                f"{language_labels.get(str(row.get('language')), row.get('language'))} "
                f"{int(row.get('page_count') or 0)}"
                for row in language_rows
            )
            lines.append(f"- Idiomas observados por página: **{summary}**")
        if language_detection.get("mixed_language_site") is True:
            lines.append(
                "- Hallazgo de coherencia: **mezcla de idiomas dentro del "
                "mismo dominio**"
            )
        mismatch_count = int(
            language_detection.get("declared_mismatch_count") or 0
        )
        if mismatch_count:
            lines.append(
                "- Páginas cuyo idioma observado no coincide con el declarado "
                f"en HTML: **{mismatch_count}**"
            )

        visited_pages = [
            row
            for row in owned.get("visited_pages") or []
            if isinstance(row, dict) and str(row.get("url") or "").strip()
        ]
        if visited_pages:
            lines.append("")
            lines.append("### Páginas visitadas")
            for row in visited_pages:
                status = str(row.get("status") or "visited")
                navigation = str(row.get("navigation_status") or "")
                suffix = f" · {navigation}" if navigation else ""
                observed_language = str(
                    row.get("observed_language") or ""
                )
                if observed_language:
                    suffix += (
                        " · idioma "
                        f"{language_labels.get(observed_language, observed_language)}"
                    )
                lines.append(
                    f"- `{status}` · {row.get('url')}{suffix}"
                )

        not_visited_pages = [
            row
            for row in owned.get("not_visited_pages") or []
            if isinstance(row, dict) and str(row.get("url") or "").strip()
        ]
        if not_visited_pages:
            lines.append("")
            lines.append("### Páginas conocidas no visitadas")
            for row in not_visited_pages:
                reason = str(row.get("reason") or "not_visited")
                navigation = str(row.get("navigation_status") or "")
                suffix = f" · {navigation}" if navigation else ""
                lines.append(
                    f"- `{reason}` · {row.get('url')}{suffix}"
                )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _timestamp_is_newer(candidate: Any, baseline: Any) -> bool:
    def parse(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    candidate_dt = parse(candidate)
    baseline_dt = parse(baseline)
    return bool(
        candidate_dt is not None
        and baseline_dt is not None
        and candidate_dt > baseline_dt
    )
