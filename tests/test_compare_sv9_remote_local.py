from scripts.compare_sv9_remote_local import (
    compare_remote_local,
    normalize_local_report,
    parse_remote_export,
    render_markdown,
)


REMOTE_EXPORT = """# Brand3 Scanner — Case

- URL: https://case.test
- Brand3 Score: **79/100**

## Misión

Case convierte entrenamiento en recompensa real.

- Nota: **4/5** (4/5 pts) · confianza alta

### Baldosas apagadas (plan de trabajo)
- **M5 · Ambiciosa** — Marca un camino que lidera o redefine su categoría.

### Puntos ciegos (contexto pendiente)
- (ninguno)

## Magnetism

> El mecanismo de recompensa retiene con claridad.

Case genera deseo con misiones, ranking y moneda propia.

- Nota: **9/10** ×2 (18/20 pts) · confianza alta

### Baldosas apagadas (plan de trabajo)
- **MG9 · Pertenencia o estatus** — Faltan señales de orgullo.

### Puntos ciegos (contexto pendiente)
- (ninguno)
"""


def test_parse_remote_export_extracts_component_scores_and_tiles():
    parsed = parse_remote_export(REMOTE_EXPORT, source="fixture://remote")

    assert parsed["brand_name"] == "Case"
    assert parsed["score"] == 79
    assert parsed["components"]["mission"]["score"] == 4
    assert parsed["components"]["mission"]["scale"] == 5
    assert parsed["components"]["mission"]["off_tiles"][0]["id"] == "M5"
    assert parsed["components"]["magnetism"]["summary"] == "El mecanismo de recompensa retiene con claridad."


def test_compare_remote_local_classifies_gate_false_negative():
    remote = parse_remote_export(REMOTE_EXPORT, source="fixture://remote")
    local = normalize_local_report(
        {
            "id": "abc",
            "brand_name": "Case",
            "url": "https://case.test",
            "score": 20,
            "components": [
                {
                    "key": "magnetism",
                    "label": "Magnetism",
                    "score": 0,
                    "scale": 10,
                    "status": "not_detected",
                    "tiles": [],
                    "block": {
                        "detected": False,
                        "coverage_status": "insufficient_acquisition",
                        "provenance_source": "gate_rejected",
                        "rejected_content": "Case has missions and rankings.",
                        "refs": [
                            {
                                "ref": "raw_inputs.1",
                                "url": "https://case.test",
                                "snippet": "Misiones, ranking y recompensas.",
                            }
                        ],
                    },
                }
            ],
        },
        source="fixture://local",
    )

    comparison = compare_remote_local(remote, local)
    magnetism = next(row for row in comparison["components"] if row["key"] == "magnetism")

    assert magnetism["gap_class"] == "local_false_negative_gate"
    assert comparison["summary"]["critical_gap_count"] == 1
    assert "gate determinista" in magnetism["probable_cause"]


def test_render_markdown_includes_refs_and_remote_limitation():
    remote = parse_remote_export(REMOTE_EXPORT, source="fixture://remote")
    local = normalize_local_report(
        {
            "id": "abc",
            "brand_name": "Case",
            "url": "https://case.test",
            "score": 20,
            "components": [
                {
                    "key": "mission",
                    "label": "Misión",
                    "score": 0,
                    "scale": 5,
                    "status": "not_detected",
                    "tiles": [],
                    "block": {
                        "detected": False,
                        "coverage_status": "insufficient_acquisition",
                        "provenance_source": "insufficient_evidence",
                        "refs": [],
                    },
                }
            ],
        },
        source="fixture://local",
    )

    markdown = render_markdown(compare_remote_local(remote, local))

    assert "export remoto público no expone raw refs" in markdown
    assert "`local_acquisition_missing`" in markdown
