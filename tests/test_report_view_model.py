from web.report_view_model import build_report_view_model


def test_component_primary_prefers_message_and_keeps_detected_content_for_drawer():
    vm = build_report_view_model(
        {
            "id": "r1",
            "brand_name": "Optiak",
            "url": "https://optiak.com",
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
