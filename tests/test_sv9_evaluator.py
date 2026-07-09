import unittest

from src.sv9.evaluator import (
    evaluate_component,
    evaluate_coherencia,
    evaluate_snapshot_components,
)
from src.sv9.models import (
    STATUS_NOT_DETECTED,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
)
from src.sv9.rubric import COMPONENTS, tile_ids


def tldr_block(content: str, *, detected: bool = True, evidence: list[str] | None = None) -> dict:
    return {
        "content": content if detected else None,
        "detected": detected,
        "mode": "literal" if detected else "not_detected",
        "confidence": "high" if detected else "insufficient",
        "evidence": evidence if detected else [],
        "rationale": "test block",
    }


def full_tldr() -> dict:
    return {
        key: tldr_block(f"{key} detected text", evidence=[f"{key} quote"])
        for key in [
            "core_purpose",
            "magnetism",
            "value_proposition",
            "personality",
            "brand_idea",
            "attributes",
            "values",
            "mission",
            "vision",
        ]
    }


class FakeLLM:
    """Lights the first `ok_up_to` tiles (with evidence), the rest `no`."""

    def __init__(self, ok_up_to: int = 3, *, omit_tiles: bool = False, fail_call: bool = False, model: str = "fake-flash"):
        self.api_key = "test-key"
        self.model = model
        self.ok_up_to = ok_up_to
        self.omit_tiles = omit_tiles
        self.fail_call = fail_call
        self.calls: list[dict] = []
        self.last_failure_reason = "llm_error"

    def _tiles_for(self, schema_name):
        for key in COMPONENTS:
            if schema_name == f"baldosas_{key}":
                return tile_ids(key)
        return []

    def _call_json(self, system, user, max_tokens=8000, *, json_schema=None, schema_name=None, timeout_seconds=None, strict_schema=True, temperature=None):
        self.calls.append({"system": system, "user": user, "schema_name": schema_name})
        if self.fail_call:
            return {}
        ids = self._tiles_for(schema_name)
        if self.omit_tiles:
            ids = ids[:-1]
        baldosas = []
        for i, tid in enumerate(ids):
            if i < self.ok_up_to:
                baldosas.append({"id": tid, "estado": "ok", "evidencia": f"quote {tid}"})
            else:
                baldosas.append({"id": tid, "estado": "no", "motivo": "falta"})
        payload = {"componente": schema_name, "baldosas": baldosas}
        if schema_name == "baldosas_coherencia":
            payload["veredicto"] = "La marca cuenta una historia única que se sostiene."
        return payload


class EvaluateComponentTests(unittest.TestCase):
    def test_not_detected_scores_zero_without_llm_call(self):
        llm = FakeLLM()
        tldr = full_tldr()
        tldr["mission"] = tldr_block("", detected=False)
        result = evaluate_component("mission", tldr=tldr, signals=[], brand_name="Acme", url="u", llm=llm)
        self.assertEqual(result.status, STATUS_NOT_DETECTED)
        self.assertEqual(result.score, 0)
        self.assertEqual(llm.calls, [])

    def test_detected_component_counts_lit_tiles(self):
        llm = FakeLLM(ok_up_to=4)
        result = evaluate_component(
            "personality", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.score, 4)
        self.assertEqual(len(result.tile_profile), 10)

    def test_component_message_is_captured_without_changing_score(self):
        class MessageLLM(FakeLLM):
            def _call_json(self, system, user, max_tokens=8000, *, json_schema=None, schema_name=None, timeout_seconds=None, strict_schema=True, temperature=None):
                payload = super()._call_json(
                    system,
                    user,
                    max_tokens=max_tokens,
                    json_schema=json_schema,
                    schema_name=schema_name,
                    timeout_seconds=timeout_seconds,
                    strict_schema=strict_schema,
                )
                payload["message"] = "La marca convierte precisión técnica en una promesa clara para el comprador."
                return payload

        result = evaluate_component(
            "personality",
            tldr=full_tldr(),
            signals=[],
            brand_name="Acme",
            url="u",
            llm=MessageLLM(ok_up_to=4),
        )

        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.score, 4)
        self.assertEqual(
            result.message,
            "La marca convierte precisión técnica en una promesa clara para el comprador.",
        )
        self.assertEqual(result.to_dict()["message"], result.message)

    def test_component_message_is_sanitized_to_spanish(self):
        class EnglishMessageLLM(FakeLLM):
            def _call_json(self, system, user, max_tokens=8000, *, json_schema=None, schema_name=None, timeout_seconds=None, strict_schema=True, temperature=None):
                payload = super()._call_json(
                    system,
                    user,
                    max_tokens=max_tokens,
                    json_schema=json_schema,
                    schema_name=schema_name,
                    timeout_seconds=timeout_seconds,
                    strict_schema=strict_schema,
                )
                payload["message"] = "The available evidence requires a stronger claim."
                return payload

        result = evaluate_component(
            "personality",
            tldr=full_tldr(),
            signals=[],
            brand_name="Acme",
            url="u",
            llm=EnglishMessageLLM(),
        )

        self.assertNotIn("The available evidence", result.message)
        self.assertEqual(result.message, "")

    def test_generated_tile_explanations_are_sanitized_to_spanish(self):
        class EnglishLLM(FakeLLM):
            def _call_json(self, system, user, max_tokens=8000, *, json_schema=None, schema_name=None, timeout_seconds=None, strict_schema=True, temperature=None):
                ids = self._tiles_for(schema_name)
                return {
                    "componente": schema_name,
                    "baldosas": [
                        {"id": ids[0], "estado": "ok", "evidencia": "quote"},
                        {
                            "id": ids[1],
                            "estado": "sin_evidencia",
                            "motivo": "The snapshot does not provide access to the full product interface.",
                            "contexto_requerido": "Access to the logged-in product dashboard and error states.",
                        },
                        *[{"id": tid, "estado": "no", "motivo": "falta"} for tid in ids[2:]],
                    ],
                }

        result = evaluate_component(
            "personality",
            tldr=full_tldr(),
            signals=[],
            brand_name="Acme",
            url="u",
            llm=EnglishLLM(),
        )

        blind = result.tile_profile[1]
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertNotIn("The snapshot", blind.motivo)
        self.assertNotIn("Access to", blind.contexto_requerido)
        self.assertIn("snapshot no aporta evidencia suficiente", blind.motivo)
        self.assertIn("Aporta contexto externo verificable", blind.contexto_requerido)

    def test_sv9_flow_tile_signals_are_rendered_as_tile_scoped_context(self):
        llm = FakeLLM(ok_up_to=2)
        evaluate_component(
            "magnetism",
            tldr=full_tldr(),
            signals=[
                {
                    "feature": "sv9_flow_tile_signal",
                    "legacy_dimension": "sv9-tile-signals-v1",
                    "value": "insufficient_evidence",
                    "confidence": "high",
                    "source": "brand_interpretation",
                    "tile": "magnetism.MG10",
                    "effect": "insufficient_evidence",
                    "evidence_refs": ["features.7"],
                    "rationale": "No direct gravity evidence.",
                }
            ],
            brand_name="Acme",
            url="u",
            llm=llm,
        )

        user_prompt = llm.calls[0]["user"]
        self.assertIn("SV9 Flow tile signal: tile=magnetism.MG10 effect=insufficient_evidence", user_prompt)
        self.assertIn("Un `supports` en una baldosa no enciende otras baldosas por arrastre", user_prompt)
        self.assertIn("Un `insufficient_evidence` de alta confianza en una baldosa exige `sin_evidencia`", user_prompt)
        self.assertIn("Un `weakens` de alta confianza exige `no`", user_prompt)

    def test_high_confidence_negative_sv9_flow_tile_signals_override_llm_tile_states(self):
        llm = FakeLLM(ok_up_to=10)
        result = evaluate_component(
            "magnetism",
            tldr=full_tldr(),
            signals=[
                {
                    "feature": "sv9_flow_tile_signal",
                    "value": "insufficient_evidence",
                    "confidence": "high",
                    "tile": "magnetism.MG10",
                    "effect": "insufficient_evidence",
                    "rationale": "No direct gravity evidence.",
                },
                {
                    "feature": "sv9_flow_tile_signal",
                    "value": "weakens",
                    "confidence": "high",
                    "tile": "magnetism.MG3",
                    "effect": "weakens",
                    "rationale": "No hook evidence.",
                },
                {
                    "feature": "sv9_flow_tile_signal",
                    "value": "insufficient_evidence",
                    "confidence": "medium",
                    "tile": "magnetism.MG9",
                    "effect": "insufficient_evidence",
                    "rationale": "Medium confidence remains advisory.",
                },
            ],
            brand_name="Acme",
            url="u",
            llm=llm,
        )

        by_tile = {verdict.tile_id: verdict for verdict in result.tile_profile}
        self.assertEqual(result.score, 8)
        self.assertEqual(by_tile["MG10"].estado, "sin_evidencia")
        self.assertEqual(by_tile["MG3"].estado, "no")
        self.assertEqual(by_tile["MG9"].estado, "ok")
        self.assertIn("No direct gravity evidence", by_tile["MG10"].motivo)
        self.assertIn("No hook evidence", by_tile["MG3"].motivo)

    def test_missing_llm_marks_not_evaluated(self):
        result = evaluate_component(
            "mission", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=None
        )
        self.assertEqual(result.status, STATUS_NOT_EVALUATED)
        self.assertEqual(result.error, "llm_unavailable")

    def test_failed_llm_call_marks_not_evaluated_and_keeps_detection(self):
        llm = FakeLLM(fail_call=True)
        result = evaluate_component(
            "mission", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(result.status, STATUS_NOT_EVALUATED)
        self.assertEqual(result.detected_content, "mission detected text")
        self.assertTrue(result.error)

    def test_incomplete_tile_coverage_retries_then_recovers_leniently(self):
        # omit_tiles drops one tile every call. After retries, the lenient pass
        # fills the missing tile as `no` rather than discarding the component.
        llm = FakeLLM(omit_tiles=True, ok_up_to=2)
        result = evaluate_component(
            "mission", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertGreaterEqual(len(llm.calls), 2)  # retried
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(len(result.tile_profile), 5)  # the missing tile was filled
        feedback_call = llm.calls[-1]["user"]
        self.assertIn("inválida", feedback_call)

    def test_blind_spot_state_is_recorded_and_scores_zero(self):
        class BlindLLM(FakeLLM):
            def _call_json(self, system, user, max_tokens=8000, **kwargs):
                self.calls.append({"user": user})
                ids = self._tiles_for(kwargs.get("schema_name"))
                baldosas = [
                    {"id": ids[0], "estado": "ok", "evidencia": "q"},
                    {"id": ids[1], "estado": "sin_evidencia", "motivo": "no hay cohorte", "contexto_requerido": "competidores"},
                    {"id": ids[2], "estado": "sin_evidencia", "motivo": "no hay comunidad"},
                ] + [{"id": t, "estado": "no", "motivo": "x"} for t in ids[3:]]
                return {"baldosas": baldosas}

        result = evaluate_component(
            "attributes", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=BlindLLM()
        )
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.score, 1)
        self.assertEqual(result.blind_spot_count, 2)
        self.assertEqual(result.confidence, "media")
        blind = next(v for v in result.tile_profile if v.estado == "sin_evidencia")
        self.assertTrue(blind.motivo)

    def test_ok_without_evidence_retries_then_demotes(self):
        class NoQuoteLLM(FakeLLM):
            def _call_json(self, system, user, max_tokens=8000, **kwargs):
                self.calls.append({"user": user})
                ids = self._tiles_for(kwargs.get("schema_name"))
                return {"baldosas": [{"id": t, "estado": "ok", "evidencia": ""} for t in ids]}

        llm = NoQuoteLLM()
        result = evaluate_component(
            "mission", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(len(llm.calls), 2)  # one retry before demotion
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.score, 0)  # all demoted to `no`
        self.assertTrue(all(v.estado == "no" for v in result.tile_profile))

    def test_duplicate_tile_ids_are_rejected_and_retried(self):
        class DuplicateLLM(FakeLLM):
            def __init__(self):
                super().__init__(ok_up_to=2)
                self.attempt = 0

            def _call_json(self, system, user, max_tokens=8000, **kwargs):
                self.calls.append({"user": user})
                ids = self._tiles_for(kwargs.get("schema_name"))
                self.attempt += 1
                if self.attempt == 1:
                    # M1 appears twice with conflicting states.
                    baldosas = [
                        {"id": ids[0], "estado": "ok", "evidencia": "first"},
                        {"id": ids[0], "estado": "no", "motivo": "second"},
                    ] + [{"id": t, "estado": "no", "motivo": "m"} for t in ids[1:]]
                    return {"baldosas": baldosas}
                return {"baldosas": [
                    {"id": t, "estado": "ok" if i < 2 else "no",
                     "evidencia": "q" if i < 2 else "", "motivo": "" if i < 2 else "m"}
                    for i, t in enumerate(ids)
                ]}

        llm = DuplicateLLM()
        result = evaluate_component(
            "mission", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(llm.attempt, 2)  # the duplicate triggered a retry
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.score, 2)

    def test_out_of_catalogue_state_retries(self):
        class BadStateLLM(FakeLLM):
            def __init__(self):
                super().__init__()
                self.attempt = 0

            def _call_json(self, system, user, max_tokens=8000, **kwargs):
                self.calls.append({"user": user})
                ids = self._tiles_for(kwargs.get("schema_name"))
                self.attempt += 1
                if self.attempt == 1:
                    return {"baldosas": [{"id": t, "estado": "maybe"} for t in ids]}
                return {"baldosas": [{"id": t, "estado": "no", "motivo": "x"} for t in ids]}

        llm = BadStateLLM()
        result = evaluate_component(
            "mission", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.score, 0)

    def test_relational_context_carries_facts_not_judgments(self):
        llm = FakeLLM()
        tldr = full_tldr()
        evaluate_component("vision", tldr=tldr, signals=[], brand_name="Acme", url="u", llm=llm)
        prompt = llm.calls[0]["user"]
        self.assertIn("mission detected text", prompt)
        self.assertNotIn("score", prompt.split("RÚBRICA")[0].lower())

    def test_missing_upstream_component_shows_as_not_detected_in_context(self):
        llm = FakeLLM()
        tldr = full_tldr()
        tldr["mission"] = tldr_block("", detected=False)
        evaluate_component("vision", tldr=tldr, signals=[], brand_name="Acme", url="u", llm=llm)
        self.assertIn("(no detectado)", llm.calls[0]["user"])

    def test_prompt_carries_antibias_rules_and_tile_ids(self):
        llm = FakeLLM()
        evaluate_component("mission", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm)
        system = llm.calls[0]["system"]
        self.assertIn("ÚNICAMENTE el contenido presente en el snapshot", system)
        self.assertIn("dolor, deseo, asombro, pertenencia", system)
        self.assertIn("M1", llm.calls[0]["user"])

    def test_prompt_carries_tile_evidence_contracts_when_defined(self):
        llm = FakeLLM()
        evaluate_component("magnetism", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm)
        user_prompt = llm.calls[0]["user"]

        self.assertIn("contrato_evidencia", user_prompt)
        self.assertIn("MG2 · Mecanismo identificable", user_prompt)
        self.assertIn("fuentes_fuertes=owned_copy, product mechanism, community/product proof", user_prompt)
        self.assertIn("no_aceptar=No aceptar 'tenemos usuarios' como pertenencia/estatus", user_prompt)

    def test_value_proposition_prompt_carries_mechanism_contract(self):
        llm = FakeLLM()
        evaluate_component("value_proposition", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm)
        user_prompt = llm.calls[0]["user"]

        self.assertIn("P8 · Mecanismo propio", user_prompt)
        self.assertIn("ok=El cómo de la propuesta está nombrado", user_prompt)
        self.assertIn("no_aceptar=No aceptar 'IA', 'plataforma' o 'ecosistema'", user_prompt)

    def test_mission_prompt_allows_product_embodied_mission_contract(self):
        llm = FakeLLM()
        evaluate_component("mission", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm)
        user_prompt = llm.calls[0]["user"]

        self.assertIn("M1 · Detectada", user_prompt)
        self.assertIn("misión encarnada en un mecanismo de producto repetible", user_prompt)
        self.assertIn("No exigir la palabra misión", user_prompt)

    def test_brand_idea_prompt_carries_visual_contract(self):
        llm = FakeLLM()
        evaluate_component("brand_idea", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm)
        user_prompt = llm.calls[0]["user"]

        self.assertIn("I7 · Traduce la estrategia", user_prompt)
        self.assertIn("visual traduce propósito, personalidad o propuesta", user_prompt)
        self.assertIn("No inferir estrategia visual desde colores aislados", user_prompt)

    def test_records_evaluation_model(self):
        llm = FakeLLM(model="gemini-flash-test")
        result = evaluate_component(
            "mission", tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(result.evaluation_model, "gemini-flash-test")


class BrandIdeaSignalEvaluationTests(unittest.TestCase):
    def test_brand_idea_evaluates_from_visual_signals_without_detection(self):
        llm = FakeLLM(ok_up_to=3)
        tldr = full_tldr()
        tldr["brand_idea"] = tldr_block("", detected=False)
        signals = [
            {
                "feature": "vision_observations",
                "legacy_dimension": "sv9_vision_pass",
                "value": "professional-generic",
                "confidence": "high",
                "source": "vision_llm",
                "detail": '{"logo_detected": true, "dominant_colors": ["#101010"]}',
            }
        ]
        result = evaluate_component(
            "brand_idea", tldr=tldr, signals=signals, brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.score, 3)
        prompt = llm.calls[0]["user"]
        self.assertIn("(no detectado)", prompt)
        self.assertIn("logo_detected", prompt)

    def test_brand_idea_without_detection_or_signals_stays_not_detected(self):
        llm = FakeLLM()
        tldr = full_tldr()
        tldr["brand_idea"] = tldr_block("", detected=False)
        result = evaluate_component(
            "brand_idea", tldr=tldr, signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(result.status, STATUS_NOT_DETECTED)
        self.assertEqual(llm.calls, [])


class EvaluateCoherenciaTests(unittest.TestCase):
    def test_coherencia_runs_with_holes_and_sees_both_axes(self):
        llm = FakeLLM(ok_up_to=2)
        tldr = full_tldr()
        tldr["values"] = tldr_block("", detected=False)
        components = {
            key: evaluate_component(key, tldr=tldr, signals=[], brand_name="Acme", url="u", llm=llm)
            for key in COMPONENTS
            if key != "coherencia"
        }
        signals = [
            {"feature": "messaging_consistency", "legacy_dimension": "coherencia", "value": 62.0, "confidence": 0.8, "source": "llm_analysis"}
        ]
        result = evaluate_coherencia(
            components=components, tldr=tldr, signals=signals, brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.score, 2)
        prompt = llm.calls[-1]["user"]
        self.assertIn("(no detectado)", prompt)
        self.assertIn("messaging_consistency", prompt)
        self.assertIn("C8 · Marca-producto", prompt)
        self.assertIn("Por defecto si el scanner no puede probar producto/uso real", prompt)

    def test_coherencia_without_llm_is_not_evaluated(self):
        result = evaluate_coherencia(
            components={}, tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=None
        )
        self.assertEqual(result.status, STATUS_NOT_EVALUATED)

    def test_coherencia_captures_veredicto(self):
        llm = FakeLLM(ok_up_to=2)
        components = {
            key: evaluate_component(key, tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm)
            for key in COMPONENTS if key != "coherencia"
        }
        result = evaluate_coherencia(
            components=components, tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertTrue(result.veredicto)
        self.assertIn("historia única", result.veredicto)

    def test_coherencia_missing_veredicto_retries_then_accepts(self):
        class NoVeredictoLLM(FakeLLM):
            def __init__(self):
                super().__init__(ok_up_to=2)
                self.attempt = 0

            def _call_json(self, system, user, max_tokens=8000, **kwargs):
                self.calls.append({"user": user, "schema_name": kwargs.get("schema_name")})
                ids = self._tiles_for(kwargs.get("schema_name"))
                baldosas = [
                    {"id": t, "estado": "ok" if i < 2 else "no",
                     "evidencia": "q" if i < 2 else "", "motivo": "" if i < 2 else "m"}
                    for i, t in enumerate(ids)
                ]
                self.attempt += 1
                payload = {"baldosas": baldosas}
                if self.attempt >= 2:
                    payload["veredicto"] = "Síntesis al segundo intento."
                return payload

        llm = NoVeredictoLLM()
        result = evaluate_coherencia(
            components={}, tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(llm.attempt, 2)  # retried because veredicto was missing
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.veredicto, "Síntesis al segundo intento.")

    def test_coherencia_synthesizes_fallback_veredicto_after_retries(self):
        class NeverVeredictoLLM(FakeLLM):
            def _call_json(self, system, user, max_tokens=8000, **kwargs):
                self.calls.append({"user": user, "schema_name": kwargs.get("schema_name")})
                ids = self._tiles_for(kwargs.get("schema_name"))
                # Tiles always valid, veredicto never provided.
                return {"baldosas": [
                    {"id": t, "estado": "ok" if i < 2 else "no",
                     "evidencia": "q" if i < 2 else "", "motivo": "" if i < 2 else "m"}
                    for i, t in enumerate(ids)
                ]}

        llm = NeverVeredictoLLM(ok_up_to=2)
        result = evaluate_coherencia(
            components={}, tldr=full_tldr(), signals=[], brand_name="Acme", url="u", llm=llm
        )
        # Component stays scored (20-pt weight preserved); a deterministic
        # fallback veredicto is synthesized rather than left blank.
        self.assertEqual(result.status, STATUS_SCORED)
        self.assertEqual(result.score, 2)
        self.assertTrue(result.veredicto)
        self.assertIn("Síntesis automática", result.veredicto)
        self.assertIn("2/10 baldosas encendidas", result.veredicto)


class EvaluateSnapshotComponentsTests(unittest.TestCase):
    def test_full_pass_yields_ten_components(self):
        llm = FakeLLM(ok_up_to=3)
        results = evaluate_snapshot_components(
            tldr=full_tldr(), signals={}, brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(set(results), set(COMPONENTS))
        for key, component in results.items():
            self.assertEqual(component.status, STATUS_SCORED, key)
        self.assertEqual(results["mission"].score, 3)
        self.assertEqual(results["coherencia"].score, 3)

    def test_one_component_failure_does_not_compromise_the_rest(self):
        class FlakyLLM(FakeLLM):
            def _call_json(self, system, user, max_tokens=8000, *, schema_name=None, **kwargs):
                if schema_name == "baldosas_personality":
                    self.calls.append({"schema_name": schema_name})
                    return {}
                return super()._call_json(system, user, max_tokens, schema_name=schema_name, **kwargs)

        results = evaluate_snapshot_components(
            tldr=full_tldr(), signals={}, brand_name="Acme", url="u", llm=FlakyLLM(ok_up_to=5)
        )
        self.assertEqual(results["personality"].status, STATUS_NOT_EVALUATED)
        others = [k for k in COMPONENTS if k != "personality"]
        for key in others:
            self.assertEqual(results[key].status, STATUS_SCORED, key)

    def test_model_routing_sends_magnetism_and_coherencia_to_reasoning(self):
        base = FakeLLM(ok_up_to=3, model="flash-tier")
        reasoning = FakeLLM(ok_up_to=3, model="reasoning-tier")
        results = evaluate_snapshot_components(
            tldr=full_tldr(), signals={}, brand_name="Acme", url="u",
            llm=base, reasoning_llm=reasoning,
        )
        self.assertEqual(results["magnetism"].evaluation_model, "reasoning-tier")
        self.assertEqual(results["coherencia"].evaluation_model, "reasoning-tier")
        for key in ("mission", "vision", "values", "attributes",
                    "value_proposition", "personality", "brand_idea", "core_purpose"):
            self.assertEqual(results[key].evaluation_model, "flash-tier", key)

    def test_single_llm_serves_both_tiers(self):
        llm = FakeLLM(ok_up_to=3, model="only-tier")
        results = evaluate_snapshot_components(
            tldr=full_tldr(), signals={}, brand_name="Acme", url="u", llm=llm
        )
        self.assertEqual(results["magnetism"].evaluation_model, "only-tier")
        self.assertEqual(results["mission"].evaluation_model, "only-tier")


if __name__ == "__main__":
    unittest.main()
