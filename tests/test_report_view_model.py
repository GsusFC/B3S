from web.report_view_model import build_report_view_model


def test_component_primary_prefers_message_and_keeps_detected_content_for_drawer():
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "pipeline_commit_sha": "d" * 40,
            "score": 64,
            "components": [
                {
                    "key": "value_proposition",
                    "label": "Propuesta de valor",
                    "score": 7,
                    "scale": 10,
                    "status": "scored",
                    "message": "Diagnóstico editorial.",
                    "veredicto": "Veredicto SV9.",
                    "detected_content": "Oferta detectada.",
                    "resumen": "Componente Propuesta de valor detectado: 7/10 baldosas encendidas.",
                    "tile_profile": [],
                }
            ],
        }
    )

    component = vm["components"][0]
    assert vm["build"] == {"commit_sha": "d" * 40, "commit_short": "d" * 12}
    assert component["card"]["primary"]["text"] == "Diagnóstico editorial."
    assert component["card"]["primary"]["source"] == "message"
    assert component["card"]["support"]["text"] == "Oferta detectada."
    assert component["card"]["support"]["source"] == "detected_content"
    assert component["card"]["support"]["show_on_card"] is True
    assert component["drawer"]["detected_basis"]["text"] == "Oferta detectada."


def test_component_primary_falls_back_to_verdict_without_showing_technical_support():
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 64,
            "components": [
                {
                    "key": "coherencia",
                    "label": "Coherencia",
                    "score": 7,
                    "scale": 10,
                    "status": "scored",
                    "message": "",
                    "veredicto": "La marca mantiene una alineación técnica clara.",
                    "resumen": "Componente Coherencia detectado: 7/10 baldosas encendidas, 0 apagadas, 3 puntos ciegos.",
                    "tile_profile": [],
                }
            ],
        }
    )

    component = vm["components"][0]
    assert component["card"]["primary"]["text"] == "La marca mantiene una alineación técnica clara."
    assert component["card"]["support"]["text"] == ""
    assert component["drawer"]["debug"]["fallback_summary"].startswith("Componente Coherencia detectado")


def test_auto_synthesis_is_not_used_as_card_primary_or_support():
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 64,
            "components": [
                {
                    "key": "magnetism",
                    "label": "Magnetism",
                    "score": 3,
                    "scale": 10,
                    "status": "scored",
                    "message": "",
                    "veredicto": "Síntesis automática: 3/10 baldosas encendidas, 1 apagada, 6 puntos ciegos.",
                    "resumen": "Componente Magnetism detectado: 3/10 baldosas encendidas.",
                    "tile_profile": [],
                }
            ],
        }
    )

    component = vm["components"][0]
    assert component["card"]["primary"]["text"] == ""
    assert component["card"]["support"]["text"] == ""
    assert "Síntesis automática" in component["drawer"]["debug"]["fallback_verdict"]


def test_drawer_separates_off_tiles_and_blind_spots():
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 64,
            "components": [
                {
                    "key": "magnetism",
                    "label": "Magnetism",
                    "score": 4,
                    "scale": 10,
                    "status": "scored",
                    "veredicto": "Veredicto.",
                    "tile_profile": [
                        {"id": "MG1", "estado": "ok", "evidencia": "visible"},
                        {"id": "MG2", "estado": "no", "motivo": "falta tensión"},
                        {
                            "id": "MG3",
                            "estado": "sin_evidencia",
                            "motivo": "sin prueba",
                            "contexto_requerido": "aporta preferencia",
                        },
                    ],
                }
            ],
        }
    )

    drawer = vm["components"][0]["drawer"]
    assert [tile["id"] for tile in drawer["off_tiles"]] == ["MG2"]
    assert [tile["id"] for tile in drawer["blind_spots"]] == ["MG3"]
    assert drawer["tile_summary"] == {"lit": 1, "off": 1, "blind": 1, "scale": 10}


def test_acquisition_artifact_uses_safe_visible_label_instead_of_raw_url():
    long_url = "https://storage.example.test/screenshot.png?signature=" + ("x" * 200)
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 64,
            "components": [],
            "acquisition_artifacts": [
                {
                    "source": "screenshot_capture",
                    "kind": "screenshot",
                    "status": "captured",
                    "success": True,
                    "provider": "firecrawl_screenshot",
                    "screenshot_url": long_url,
                }
            ],
        }
    )

    artifact = vm["acquisition"]["artifacts"][0]
    assert artifact["href"] == long_url
    assert long_url not in artifact["display_label"]
    assert artifact["chip_class"] == "ok"


def test_acquisition_view_model_exposes_owned_page_denominator_and_reasons():
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "created_at": "2026-07-30T13:48:00+00:00",
            "score": 64,
            "components": [],
            "coverage_acquisition": {
                "owned_url_count": 5,
                "owned_page_coverage": {
                    "selection_version": "owned-page-selection-v2",
                    "known_page_count": 4,
                    "attempted_page_count": 3,
                    "captured_page_count": 3,
                    "coverage_ratio": 0.75,
                    "visited_pages": [
                        {
                            "url": "https://optiak.com",
                            "status": "captured",
                            "navigation_status": "homepage",
                        }
                    ],
                    "not_visited_pages": [
                        {
                            "url": "https://optiak.com/orphan",
                            "reason": "page_budget",
                            "navigation_status": "sitemap_only",
                        }
                    ],
                    "latest_lastmod": "2026-07-29",
                    "language_detection": {
                        "version": "owned-page-language-en-es-v1",
                        "status": "mixed",
                        "mixed_language_site": True,
                        "captured_page_count": 3,
                        "evaluated_page_count": 3,
                        "unknown_page_count": 0,
                        "declared_mismatch_count": 1,
                        "distribution": [
                            {
                                "language": "en",
                                "page_count": 2,
                                "share": 0.6667,
                            },
                            {
                                "language": "es",
                                "page_count": 1,
                                "share": 0.3333,
                            },
                        ],
                    },
                },
            },
        }
    )

    assert vm["created_at_display"] == "2026-07-30 13:48:00 UTC"
    assert vm["acquisition"]["metrics"]["owned_url_count"] == 3
    assert vm["acquisition"]["owned_pages"]["coverage_label"] == "3 de 4"
    assert vm["acquisition"]["owned_pages"]["coverage_percent"] == 75
    assert vm["acquisition"]["owned_pages"]["eligible_coverage_label"] == "3 de 4"
    assert vm["acquisition"]["owned_pages"]["eligible_coverage_percent"] == 75
    assert vm["acquisition"]["owned_pages"]["latest_lastmod_newer_than_scan"] is False
    assert vm["acquisition"]["owned_pages"]["not_visited_pages"] == [
        {
            "url": "https://optiak.com/orphan",
            "role": "",
            "source": "",
            "navigation_status": "sitemap_only",
            "status": "",
            "reason": "page_budget",
            "lastmod": "",
            "observed_language": "",
            "observed_language_label": "",
            "language_confidence": "",
            "declared_language": "",
            "declared_language_mismatch": False,
        }
    ]
    language = vm["acquisition"]["owned_pages"]["language_detection"]
    assert language["summary"] == "inglés 2 · español 1"
    assert language["mixed_language_site"] is True


def test_acquisition_view_model_distinguishes_page_capture_from_content_retention():
    vm = build_report_view_model(
        {
            "id": "soccersolver",
            "brand_name": "SoccerSolver",
            "url": "https://soccersolver.com",
            "score": 64,
            "components": [],
            "coverage_acquisition": {
                "owned_page_coverage": {
                    "known_page_count": 22,
                    "captured_page_count": 18,
                }
            },
            "raw": {
                "flow": {
                    "candidate": {
                        "evidence_pack": {
                            "evidence": [
                                {
                                    "ref": "raw_inputs.0.diagnostics.sampling.subpage.1",
                                    "url": "https://soccersolver.com/luka-vuskovic",
                                    "evidence_type": "acquisition.evidence_sampling",
                                    "metadata": {
                                        "total_chunks": 10,
                                        "selected_chunks": 6,
                                        "strategic_prioritization": True,
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        }
    )

    sampling = vm["acquisition"]["owned_pages"]["content_sampling"]
    assert sampling["affected_url_count"] == 1
    assert sampling["retained_chunk_count"] == 6
    assert sampling["omitted_chunk_count"] == 4


def test_eligible_coverage_counts_failed_attempts_in_denominator():
    vm = build_report_view_model(
        {
            "id": "partial",
            "brand_name": "SoccerSolver",
            "url": "https://soccersolver.com",
            "score": 64,
            "components": [],
            "coverage_acquisition": {
                "owned_page_coverage": {
                    "known_page_count": 4,
                    "attempted_page_count": 3,
                    "captured_page_count": 2,
                    "not_visited_pages": [
                        {
                            "url": "https://soccersolver.com/editorial",
                            "reason": "page_budget",
                        }
                    ],
                }
            },
        }
    )

    owned = vm["acquisition"]["owned_pages"]
    assert owned["eligible_coverage_label"] == "2 de 4"
    assert owned["eligible_coverage_percent"] == 50


def test_component_view_model_exposes_hidden_evidence_hierarchy():
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 64,
            "components": [
                {
                    "key": "mission",
                    "label": "Misión",
                    "score": 4,
                    "scale": 5,
                    "status": "scored",
                    "detected_content": "Misión detectada.",
                    "surface_hierarchy": {
                        "schema_version": "component-surface-hierarchy-v1",
                        "presence_status": "detected",
                        "hierarchy_status": "mixed_with_sitemap_only",
                        "owned_surface_count": 2,
                        "counts": {
                            "linked_from_home": 1,
                            "sitemap_only": 1,
                        },
                        "owned_surfaces": [
                            {
                                "url": "https://optiak.com/about",
                                "navigation_status": "linked_from_home",
                                "captured": True,
                                "cited_refs": ["owned.about"],
                            },
                            {
                                "url": "https://optiak.com/thesis",
                                "navigation_status": "sitemap_only",
                                "captured": False,
                                "cited_refs": ["owned.thesis"],
                            },
                        ],
                    },
                }
            ],
        }
    )

    hierarchy = vm["components"][0]["card"]["surface_hierarchy"]
    assert hierarchy["label"] == "parte de la evidencia está fuera de navegación"
    assert hierarchy["warns_hidden_content"] is True
    assert hierarchy["counts"]["sitemap_only"] == 1
    assert hierarchy["owned_surfaces"][1] == {
        "url": "https://optiak.com/thesis",
        "navigation_status": "sitemap_only",
        "captured": False,
        "cited_ref_count": 1,
    }


def test_report_view_model_exposes_evaluation_drift_against_baseline():
    vm = build_report_view_model(
        {
            "id": "candidate",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 66,
            "canonical_status": "non_canonical",
            "components": [],
            "stability": {
                "classification": "evaluation_drift",
                "canonical_status": "non_canonical",
                "reason_codes": [
                    "evaluation_changed_without_material_evidence_delta"
                ],
                "baseline_comparison": {
                    "baseline_report_id": "baseline",
                    "delta": {
                        "changed_components": [
                            {
                                "component": "core_purpose",
                                "score_before": 4,
                                "score_after": 8,
                                "status_before": "scored",
                                "status_after": "scored",
                                "changed_tiles": ["PR3", "PR4"],
                            }
                        ]
                    },
                },
            },
        }
    )

    assert vm["stability"]["show"] is True
    assert vm["stability"]["title"] == "Deriva de evaluación detectada"
    assert vm["stability"]["baseline_report_id"] == "baseline"
    assert vm["stability"]["changed_components"] == [
        {
            "key": "core_purpose",
            "label": "Propósito",
            "score_before": 4,
            "score_after": 8,
            "status_before": "scored",
            "status_after": "scored",
            "changed_tiles": ["PR3", "PR4"],
        }
    ]
    assert vm["score_publication"]["publishable"] is False
    assert vm["score_publication"]["value"] is None
    assert vm["score_publication"]["raw_value"] == 66
    assert vm["score_publication"]["retention_reason"] == "evaluation_drift"
    assert vm["score_publication"]["label"] == "Score diagnóstico"
    assert vm["score_publication"]["reason"] == (
        "Resultado no canónico: permanece visible para diagnóstico y no "
        "sustituye al score autorizado."
    )
    assert vm["score_width"] == 66


def test_report_view_model_retains_score_after_acquisition_regression():
    vm = build_report_view_model(
        {
            "id": "candidate",
            "brand_name": "SoccerSolver",
            "url": "https://soccersolver.com",
            "score": 64,
            "components": [],
            "stability": {"classification": "acquisition_regression"},
        }
    )

    assert vm["score_publication"]["publishable"] is False
    assert vm["score_width"] == 64
    assert vm["stability"]["title"] == "Regresión de adquisición detectada"


def test_report_view_model_fails_closed_when_stability_comparison_errors():
    vm = build_report_view_model(
        {
            "id": "candidate",
            "brand_name": "SoccerSolver",
            "url": "https://soccersolver.com",
            "score": 64,
            "canonical_status": "non_canonical",
            "components": [],
            "stability": {"classification": "comparison_error"},
        }
    )

    assert vm["score_publication"]["publishable"] is False
    assert vm["score_width"] == 64


def test_acquisition_flags_sitemap_content_newer_than_scan():
    vm = build_report_view_model(
        {
            "id": "r-newer",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "created_at": "2026-07-30T13:48:00+00:00",
            "score": 64,
            "components": [],
            "coverage_acquisition": {
                "owned_page_coverage": {
                    "known_page_count": 1,
                    "captured_page_count": 1,
                    "latest_lastmod": "2026-07-31",
                }
            },
        }
    )

    assert (
        vm["acquisition"]["owned_pages"][
            "latest_lastmod_newer_than_scan"
        ]
        is True
    )


def test_attributes_and_values_cards_expose_compact_terms():
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 64,
            "components": [
                {
                    "key": "attributes",
                    "label": "Atributos",
                    "score": 5,
                    "scale": 5,
                    "status": "scored",
                    "message": "Diagnóstico editorial largo.",
                    "detected_content": (
                        "Plataforma de orquestación de IA agnóstica y modular, con seguridad, "
                        "gobernanza y capacidad de intercambio de componentes."
                    ),
                    "tile_profile": [
                        {
                            "id": "A1",
                            "estado": "ok",
                            "evidencia": "Arquitectura agnóstica, modular y segura.",
                        }
                    ],
                },
                {
                    "key": "values",
                    "label": "Valores",
                    "score": 3,
                    "scale": 5,
                    "status": "scored",
                    "message": "Diagnóstico editorial largo.",
                    "detected_content": (
                        "La IA debe ser fundamental para las empresas, no solo experimental, "
                        "y debe integrarse plenamente en la operativa."
                    ),
                    "tile_profile": [
                        {
                            "id": "VA1",
                            "estado": "ok",
                            "evidencia": "AI should be foundational to businesses, not just experimental.",
                        }
                    ],
                },
            ],
        }
    )

    attributes = next(component for component in vm["components"] if component["key"] == "attributes")
    values = next(component for component in vm["components"] if component["key"] == "values")

    assert attributes["card"]["primary"]["text"] == "Diagnóstico editorial largo."
    assert attributes["card"]["primary"]["items"][:4] == ["agnóstica", "modular", "segura", "gobernable"]
    assert values["card"]["primary"]["items"][:3] == [
        "IA como infraestructura",
        "integración operativa",
        "rigor operativo",
    ]


def test_component_primary_prefers_structured_editorial_v31():
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
            "score": 64,
            "editorial": {
                "schema_version": "sv9_editorial_v3_1",
                "executive_reading": "Lectura ejecutiva estructurada.",
            },
            "executive_reading": "Lectura legacy.",
            "components": [
                {
                    "key": "attributes",
                    "label": "Atributos",
                    "score": 5,
                    "scale": 5,
                    "status": "scored",
                    "message": "Diagnóstico legacy.",
                    "detected_content": "Oferta detectada legacy.",
                    "editorial": {
                        "diagnosis": "Diagnóstico V3.1.",
                        "detected_basis": "Base factual V3.1.",
                        "next_artifact": "Mapa de atributos.",
                        "terms": ["Agnóstica", "Modular", "Segura"],
                        "claim_type": "observed",
                        "mode": "evidence_based",
                    },
                    "tile_profile": [],
                },
                {
                    "key": "values",
                    "label": "Valores",
                    "score": 0,
                    "scale": 5,
                    "status": "not_detected",
                    "message": "Valores legacy.",
                    "editorial": {
                        "diagnosis": "No hay principios observables.",
                        "detected_basis": "No se detectan valores declarados.",
                        "next_artifact": "Principios operativos.",
                        "terms": [],
                        "claim_type": "missing",
                        "mode": "gap",
                    },
                    "tile_profile": [],
                },
            ],
        }
    )

    assert vm["hero"]["executive_reading"] == "Lectura ejecutiva estructurada."
    attributes = next(component for component in vm["components"] if component["key"] == "attributes")
    values = next(component for component in vm["components"] if component["key"] == "values")
    assert attributes["card"]["primary"]["text"] == "Diagnóstico V3.1."
    assert attributes["card"]["primary"]["items"] == ["Agnóstica", "Modular", "Segura"]
    assert attributes["drawer"]["detected_basis"]["text"] == "Base factual V3.1."
    assert attributes["drawer"]["next_artifact"] == "Mapa de atributos."
    assert values["card"]["primary"]["text"] == "No hay principios observables."
    assert values["card"]["primary"]["items"] == []


def test_report_hero_separates_insufficient_acquisition_from_absence():
    vm = build_report_view_model(
        {
            "id": "r2",
            "brand_name": "Movyn",
            "url": "https://movyn.ai",
            "not_detected": ["values"],
            "components": [
                {
                    "key": "values",
                    "label": "Valores",
                    "score": 0,
                    "scale": 5,
                    "status": "not_detected",
                    "block": {"coverage_status": "insufficient_acquisition"},
                    "tile_profile": [],
                }
            ],
        }
    )

    assert vm["hero"]["insufficient_evidence"] == ["values"]
