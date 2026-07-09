import unittest

from src.sv9.aggregator import aggregate
from src.sv9.editorial import build_editorial
from src.sv9.models import (
    ComponentResult,
    ESTADO_NO,
    ESTADO_OK,
    STATUS_NOT_DETECTED,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    TileVerdict,
)
from src.sv9.editorial import SV9_EDITORIAL_SCHEMA_VERSION
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER, tile_ids


def scored(key: str, score: int) -> ComponentResult:
    ids = tile_ids(key)
    profile = [
        TileVerdict(
            tile_id=tid,
            estado=ESTADO_OK if i < score else ESTADO_NO,
            evidencia=f"quote {tid}" if i < score else "",
            motivo="" if i < score else "falta",
        )
        for i, tid in enumerate(ids)
    ]
    return ComponentResult(
        component=key,
        status=STATUS_SCORED,
        score=score,
        tile_profile=profile,
        detected_content=f"{key} detected text",
    )


def scan_dict(**overrides: ComponentResult) -> dict:
    components = {key: scored(key, 3) for key in COMPONENTS}
    components.update(overrides)
    result = aggregate(components, brand_name="Acme", url="https://acme.test", source_run_id=1)
    return result.to_dict()


class FakeEditorialLLM:
    def __init__(self, *, fail_for: set[str] | None = None):
        self.api_key = "test-key"
        self.fail_for = fail_for or set()
        self.calls: list[dict] = []

    def _call_json(self, system, user, max_tokens=8000, *, json_schema=None, schema_name=None, timeout_seconds=None, strict_schema=True):
        self.calls.append({"system": system, "user": user, "schema_name": schema_name})
        if schema_name in self.fail_for:
            return {}
        return {
            "brand_context": {
                "what": "Una plataforma de prueba.",
                "for_whom": "Equipos que necesitan claridad.",
                "how": "Ordenando señales visibles.",
                "why": "Reducir decisiones ambiguas.",
                "category": "Software de marca.",
                "proof": "El snapshot aporta claims propios.",
                "main_gap": "Falta tensión narrativa.",
                "narrative_thesis": "La marca puede convertir claridad técnica en criterio de decisión.",
            },
            "tension_map": {
                "category_enemy": "La ambigüedad operativa.",
                "core_mechanism": "Unificación de señales.",
                "earned_strength": "Claridad técnica.",
                "missing_signal": "Postura propia.",
                "brand_thesis": "La utilidad gana valor cuando expresa criterio.",
                "not_x_but_y": "No es una herramienta aislada, es una capa de decisión.",
                "decision_filter": "Priorizar señales observables sobre promesas genéricas.",
            },
            "executive_reading": "Lectura ejecutiva de prueba.",
            "components": {
                key: {
                    "tldr": f"Tesis breve para {key}.",
                    "diagnosis": f"Mensaje para {key}. La causa visible genera una tensión concreta y exige una decisión observable en la marca.",
                    "detected_basis": f"Base detectada para {key}.",
                    "next_artifact": "Mapa de decisión.",
                    "terms": ["Clara", "Técnica", "Flexible"] if key in {"attributes", "values"} else [],
                    "claim_type": "observed",
                    "mode": "editorial",
                    "confidence": 0.8,
                }
                for key in PRESENTATION_ORDER
            },
        }


class BuildEditorialTests(unittest.TestCase):
    def test_generates_message_per_component_and_reading(self):
        llm = FakeEditorialLLM()
        payload = build_editorial(scan_dict(), llm=llm)
        self.assertEqual(len(payload["component_messages"]), 10)
        self.assertEqual(payload["executive_reading"], "Lectura ejecutiva de prueba.")
        self.assertEqual(payload["structured"]["schema_version"], SV9_EDITORIAL_SCHEMA_VERSION)

    def test_prompt_carries_score_evidence_and_next_off_tile(self):
        llm = FakeEditorialLLM()
        build_editorial(scan_dict(mission=scored("mission", 3)), llm=llm)
        call = next(c for c in llm.calls if c["schema_name"] == SV9_EDITORIAL_SCHEMA_VERSION)
        self.assertIn('"score": 3', call["user"])
        self.assertIn("quote M1", call["user"])
        self.assertIn('"components_to_write"', call["user"])
        self.assertIn("Contrato V3.1", call["system"])
        self.assertNotIn("ha consolidado", call["system"])
        self.assertIn("se posiciona como", call["system"])

    def test_not_detected_gets_message_not_evaluated_does_not(self):
        llm = FakeEditorialLLM()
        payload = build_editorial(
            scan_dict(
                vision=ComponentResult(component="vision", status=STATUS_NOT_DETECTED),
                values=ComponentResult(component="values", status=STATUS_NOT_EVALUATED, error="boom"),
            ),
            llm=llm,
        )
        self.assertIn("vision", payload["component_messages"])
        self.assertNotIn("values", payload["component_messages"])

    def test_failed_generation_leaves_component_voiceless(self):
        llm = FakeEditorialLLM(fail_for={SV9_EDITORIAL_SCHEMA_VERSION})
        payload = build_editorial(scan_dict(), llm=llm)
        self.assertNotIn("mission", payload["component_messages"])
        self.assertEqual(payload, {"component_messages": {}, "executive_reading": None})

    def test_no_llm_returns_empty_payload(self):
        payload = build_editorial(scan_dict(), llm=None)
        self.assertEqual(payload, {"component_messages": {}, "executive_reading": None})

    def test_accepts_persisted_scan_shape(self):
        scan = scan_dict()
        scan["components"] = [
            {**c, "component": key} for key, c in scan["components"].items()
        ]
        payload = build_editorial(scan, llm=FakeEditorialLLM())
        self.assertEqual(len(payload["component_messages"]), 10)

    def test_can_limit_component_messages_and_skip_executive_reading(self):
        llm = FakeEditorialLLM()
        payload = build_editorial(
            scan_dict(),
            llm=llm,
            component_keys=["magnetism"],
            include_executive_reading=False,
        )

        self.assertEqual(set(payload["component_messages"]), {"magnetism"})
        self.assertIsNone(payload["executive_reading"])
        self.assertEqual([call["schema_name"] for call in llm.calls], [SV9_EDITORIAL_SCHEMA_VERSION])


class EditorialPersistenceTests(unittest.TestCase):
    def test_save_editorial_roundtrip(self):
        import tempfile
        from pathlib import Path

        from src.sv9.store import Sv9Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Sv9Store(str(Path(tmp) / "t.sqlite3"))
            try:
                components = {key: scored(key, 3) for key in COMPONENTS}
                result = aggregate(components, brand_name="Acme", url="https://acme.test")
                scan_id = store.save_scan(result)
                store.save_editorial(
                    scan_id,
                    component_messages={"mission": "La misión es concreta."},
                    executive_reading="Lectura ejecutiva.",
                )
                loaded = store.get_scan(scan_id)
                self.assertEqual(loaded["executive_reading"], "Lectura ejecutiva.")
                mission = next(c for c in loaded["components"] if c["component"] == "mission")
                self.assertEqual(mission["message"], "La misión es concreta.")
                vision = next(c for c in loaded["components"] if c["component"] == "vision")
                self.assertEqual(vision["message"], "")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
