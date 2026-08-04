import unittest

from src.sv9.aggregator import aggregate
from src.sv9.export_md import build_scan_markdown
from src.sv9.models import (
    ComponentResult,
    ESTADO_NO,
    ESTADO_OK,
    ESTADO_SIN_EVIDENCIA,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    TileVerdict,
)
from src.sv9.rubric import COMPONENTS, tile_ids


def component(key: str, *, ok: int = 0, blind: int = 0) -> ComponentResult:
    ids = tile_ids(key)
    profile = []
    for i, tid in enumerate(ids):
        if i < ok:
            profile.append(TileVerdict(tile_id=tid, estado=ESTADO_OK, evidencia="cita"))
        elif i < ok + blind:
            profile.append(
                TileVerdict(
                    tile_id=tid,
                    estado=ESTADO_SIN_EVIDENCIA,
                    motivo="el snapshot no contiene cohorte",
                    contexto_requerido="enlaces a competidores",
                )
            )
        else:
            profile.append(TileVerdict(tile_id=tid, estado=ESTADO_NO, motivo="no comunicado"))
    return ComponentResult(
        component=key,
        status=STATUS_SCORED,
        score=ok,
        tile_profile=profile,
        detected_content=f"{key} detected",
    )


class ExportMarkdownTests(unittest.TestCase):
    def _scan(self):
        components = {key: component(key, ok=3) for key in COMPONENTS}
        components["attributes"] = component("attributes", ok=2, blind=1)
        components["coherencia"].veredicto = "La marca cuenta una historia única que se sostiene."
        result = aggregate(components, brand_name="Acme", url="https://acme.test", source_run_id=1)
        return result.to_dict()

    def test_header_carries_brand_score_and_model(self):
        scan = self._scan()
        scan["pipeline_commit_sha"] = "e" * 40
        md = build_scan_markdown(scan)
        self.assertIn("# Brand3 Scanner — Acme", md)
        self.assertIn("Brand3 Score", md)
        self.assertIn("Modelo: v3.1", md)
        self.assertIn(f"Build: `{'e' * 40}`", md)
        self.assertIn("Confiabilidad:", md)
        self.assertIn("Canonicidad:", md)

    def test_header_prefers_display_name(self):
        scan = self._scan()
        scan["brand_name"] = "https://acme.test"
        scan["display_name"] = "Acme"
        md = build_scan_markdown(scan)
        self.assertIn("# Brand3 Scanner — Acme", md)
        self.assertNotIn("# Brand3 Scanner — https://acme.test", md)

    def test_export_includes_scan_time_and_owned_page_denominator(self):
        scan = self._scan()
        scan["created_at"] = "2026-07-30T13:48:00+00:00"
        scan["coverage_acquisition"] = {
            "owned_page_coverage": {
                "selection_version": "owned-page-selection-v2",
                "known_page_count": 4,
                "captured_page_count": 3,
                "coverage_ratio": 0.75,
                "visited_pages": [
                    {
                        "url": "https://acme.test",
                        "status": "captured",
                        "navigation_status": "homepage",
                        "observed_language": "en",
                    }
                ],
                "not_visited_pages": [
                    {
                        "url": "https://acme.test/orphan",
                        "reason": "page_budget",
                        "navigation_status": "sitemap_only",
                    }
                ],
                "language_detection": {
                    "status": "mixed",
                    "mixed_language_site": True,
                    "declared_mismatch_count": 1,
                    "distribution": [
                        {"language": "en", "page_count": 2, "share": 0.6667},
                        {"language": "es", "page_count": 1, "share": 0.3333},
                    ],
                },
            },
            "content_sampling": {
                "sampling_record_count": 1,
                "affected_url_count": 1,
                "available_chunk_count": 10,
                "retained_chunk_count": 6,
                "omitted_chunk_count": 4,
                "surfaces": [],
            },
        }

        md = build_scan_markdown(scan)

        self.assertIn("Escaneado: 2026-07-30T13:48:00+00:00", md)
        self.assertIn("Páginas propias capturadas: **3 de 4 (75%)**", md)
        self.assertIn("Páginas elegibles capturadas: **3 de 4 (75%)**", md)
        self.assertIn("https://acme.test/orphan · sitemap_only", md)
        self.assertIn("Idiomas observados por página: **inglés 2 · español 1**", md)
        self.assertIn("mezcla de idiomas dentro del mismo dominio", md)
        self.assertIn("no coincide con el declarado en HTML: **1**", md)
        self.assertIn("https://acme.test · homepage · idioma inglés", md)
        self.assertIn(
            "Contenido conservado en páginas con muestreo parcial: **6 de 10 fragmentos**",
            md,
        )
        self.assertIn("se omitieron 4 fragmentos", md)

    def test_export_exposes_component_evidence_hierarchy_without_rescoring(self):
        scan = self._scan()
        scan["components"]["mission"]["surface_hierarchy"] = {
            "hierarchy_status": "mixed_with_sitemap_only",
            "counts": {"linked_from_home": 1, "sitemap_only": 2},
        }

        md = build_scan_markdown(scan)

        self.assertIn(
            "Jerarquía: **parte de la evidencia está fuera de navegación**",
            md,
        )
        self.assertIn("Superficies propias fuera de navegación: **2**", md)

    def test_export_exposes_non_canonical_evaluation_drift(self):
        scan = self._scan()
        scan["canonical_status"] = "non_canonical"
        scan["canonical_reason_codes"] = [
            "interpretation_changed_without_material_evidence_delta",
            "evaluation_changed_without_material_evidence_delta",
        ]
        scan["stability"] = {
            "classification": "evaluation_drift",
            "baseline_comparison": {
                "baseline_report_id": "baseline",
                "delta": {
                    "changed_components": [
                        {
                            "component": "core_purpose",
                            "score_before": 4,
                            "score_after": 8,
                            "changed_tiles": ["PR3", "PR4"],
                        }
                    ]
                },
            },
        }

        md = build_scan_markdown(scan)

        self.assertIn("Canonicidad: **no canónico**", md)
        self.assertIn(
            "la evaluación cambió sin cambios materiales en la evidencia",
            md,
        )
        self.assertIn("## Estabilidad de la evaluación", md)
        self.assertIn("Baseline: `baseline`", md)
        self.assertIn("Propósito: **4 → 8** · baldosas PR3, PR4", md)
        self.assertIn(
            "Brand3 Score: **retenido (evaluación no canónica)**",
            md,
        )
        self.assertIn("Nota: **retenida**", md)

    def test_export_retains_score_after_acquisition_regression(self):
        scan = self._scan()
        scan["stability"] = {"classification": "acquisition_regression"}

        md = build_scan_markdown(scan)

        self.assertIn(
            "Brand3 Score: **retenido (evaluación no canónica)**",
            md,
        )
        self.assertIn("**Regresión de adquisición:**", md)

    def test_export_declares_analysis_contract_and_newer_sitemap_content(self):
        scan = self._scan()
        scan["created_at"] = "2026-07-30T13:48:00+00:00"
        scan["analysis_contract"] = {
            "rubric_version": "baldosas-v3-1",
            "interpretation_prompt_version": "interpretation-v1.2",
            "evaluator_model": "gemini-test",
        }
        scan["coverage_acquisition"] = {
            "owned_page_coverage": {
                "known_page_count": 1,
                "captured_page_count": 1,
                "latest_lastmod": "2026-07-31",
            }
        }

        md = build_scan_markdown(scan)

        self.assertIn("Rúbrica: `baldosas-v3-1`", md)
        self.assertIn("Prompt de interpretación: `interpretation-v1.2`", md)
        self.assertIn("Evaluador: `gemini-test`", md)
        self.assertIn("contenido posterior al scan", md)

    def test_coherencia_verdict_is_the_section_header(self):
        md = build_scan_markdown(self._scan())
        coh = md.index("## Coherencia")
        self.assertIn("La marca cuenta una historia única que se sostiene.", md[coh:])

    def test_off_tiles_are_the_work_plan(self):
        md = build_scan_markdown(self._scan())
        self.assertIn("Baldosas apagadas (plan de trabajo)", md)
        # mission with 3 lit leaves M4/M5 off
        self.assertIn("M4", md)
        self.assertIn("M5", md)

    def test_component_message_is_exported(self):
        scan = self._scan()
        scan["components"]["mission"]["message"] = "Lectura V9 de misión."
        md = build_scan_markdown(scan)
        self.assertIn("Lectura V9 de misión.", md)

    def test_missing_tile_verdicts_are_exported(self):
        scan = self._scan()
        scan["components"]["mission"]["tile_profile"] = [
            {"id": "M1", "estado": "ok", "evidencia": "visible"},
            {"id": "M2", "estado": "ok", "evidencia": "visible"},
            {"id": "M3", "estado": "ok", "evidencia": "visible"},
        ]
        md = build_scan_markdown(scan)
        self.assertIn("Baldosas sin veredicto persistido", md)
        self.assertIn("M4", md)
        self.assertIn("M5", md)

    def test_blind_spots_are_separated_with_context(self):
        md = build_scan_markdown(self._scan())
        self.assertIn("Puntos ciegos (contexto pendiente)", md)
        self.assertIn("el snapshot no contiene cohorte", md)
        self.assertIn("aporta contexto: enlaces a competidores", md)

    def test_off_tiles_and_blind_spots_are_distinct_sections(self):
        md = build_scan_markdown(self._scan())
        plan_idx = md.index("Baldosas apagadas (plan de trabajo)")
        blind_idx = md.index("Puntos ciegos (contexto pendiente)")
        self.assertLess(plan_idx, blind_idx)

    def test_technical_failure_is_flagged_not_listed_as_plan(self):
        components = {key: component(key, ok=3) for key in COMPONENTS}
        components["values"] = ComponentResult(
            component="values", status=STATUS_NOT_EVALUATED, error="provider_http_error"
        )
        scan = aggregate(components, brand_name="Acme", url="https://acme.test").to_dict()
        md = build_scan_markdown(scan)
        self.assertIn("Fallo técnico", md)
        self.assertIn("provider_http_error", md)

    def test_generated_english_prose_and_reason_codes_render_in_spanish(self):
        scan = self._scan()
        scan["executive_reading"] = "The snapshot does not provide enough evidence."
        scan["reliability_status"] = "shadow"
        scan["reliability_reason_codes"] = ["components_not_detected", "blind_spots_above_usable_threshold"]
        scan["canonical_status"] = "non_canonical"
        scan["canonical_reason_codes"] = ["shadow_not_canonical"]
        scan["components"]["brand_idea"]["veredicto"] = "The brand idea is clearly articulated and consistently executed."
        scan["components"]["brand_idea"]["message"] = "The available evidence requires a stronger claim."
        scan["components"]["attributes"]["tile_profile"][2]["motivo"] = "The snapshot does not provide access to the full product interface."
        scan["components"]["attributes"]["tile_profile"][2]["contexto_requerido"] = "Access to the logged-in product dashboard and error states."

        md = build_scan_markdown(scan)

        self.assertIn("Confiabilidad: **sombra**", md)
        self.assertIn("componentes no detectados", md)
        self.assertIn("Canonicidad: **no canónico**", md)
        self.assertIn("sombra, no canónico", md)
        self.assertNotIn("components_not_detected", md)
        self.assertNotIn("non_canonical", md)
        self.assertNotIn("The snapshot", md)
        self.assertNotIn("Access to", md)
        self.assertNotIn("The brand idea", md)
        self.assertIn("Síntesis automática", md)
        self.assertIn("Aporta contexto externo verificable", md)

    def test_accepts_persisted_list_shape(self):
        scan = self._scan()
        scan["components"] = [
            {**c, "component": key} for key, c in scan["components"].items()
        ]
        md = build_scan_markdown(scan)
        self.assertIn("## Misión", md)


if __name__ == "__main__":
    unittest.main()
