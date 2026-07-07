import unittest

from src.sv9.aggregator import (
    aggregate,
    apply_magnetism_cap,
    base_average,
    immediate_margin,
    most_painful_gap,
    score_from_tile_profile,
)
from src.sv9.models import (
    ComponentResult,
    ESTADO_NO,
    ESTADO_OK,
    ESTADO_SIN_EVIDENCIA,
    STATUS_NOT_DETECTED,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    TileVerdict,
)
from src.sv9.rubric import COMPONENTS, tile_ids


def _tile(tid: str, estado: str) -> TileVerdict:
    if estado == ESTADO_OK:
        return TileVerdict(tile_id=tid, estado=estado, evidencia="cita")
    return TileVerdict(tile_id=tid, estado=estado, motivo="motivo")


def scored(
    key: str,
    score: int,
    *,
    blind: int = 0,
    evidence_source_summary: dict[str, int] | None = None,
    detection_limitations: list[str] | None = None,
) -> ComponentResult:
    """Light the first `score` tiles; mark `blind` of the rest as sin_evidencia."""
    ids = tile_ids(key)
    profile = []
    for i, tid in enumerate(ids):
        if i < score:
            profile.append(_tile(tid, ESTADO_OK))
        elif i < score + blind:
            profile.append(_tile(tid, ESTADO_SIN_EVIDENCIA))
        else:
            profile.append(_tile(tid, ESTADO_NO))
    return ComponentResult(
        component=key,
        status=STATUS_SCORED,
        score=score,
        tile_profile=profile,
        evidence_source_summary=evidence_source_summary or {},
        detection_limitations=detection_limitations or [],
    )


def full_components(**overrides: ComponentResult) -> dict[str, ComponentResult]:
    components = {key: scored(key, COMPONENTS[key]["scale"]) for key in COMPONENTS}
    components.update(overrides)
    return components


class ScoreFromTileProfileTests(unittest.TestCase):
    def test_all_lit_reaches_ceiling(self):
        profile = [TileVerdict(tile_id=f"X{i}", estado=ESTADO_OK, evidencia="q") for i in range(10)]
        self.assertEqual(score_from_tile_profile(profile), 10)

    def test_off_and_blind_tiles_score_zero(self):
        profile = [
            TileVerdict(tile_id="A", estado=ESTADO_OK, evidencia="q"),
            TileVerdict(tile_id="B", estado=ESTADO_NO, motivo="m"),
            TileVerdict(tile_id="C", estado=ESTADO_SIN_EVIDENCIA, motivo="m"),
            TileVerdict(tile_id="D", estado=ESTADO_OK, evidencia="q"),
        ]
        self.assertEqual(score_from_tile_profile(profile), 2)

    def test_independent_no_order(self):
        # A gap below a lit tile never truncates it (the v2 ladder bug).
        profile = [
            TileVerdict(tile_id="A", estado=ESTADO_NO, motivo="m"),
            TileVerdict(tile_id="B", estado=ESTADO_OK, evidencia="q"),
            TileVerdict(tile_id="C", estado=ESTADO_OK, evidencia="q"),
        ]
        self.assertEqual(score_from_tile_profile(profile), 2)

    def test_empty_profile_scores_zero(self):
        self.assertEqual(score_from_tile_profile([]), 0)


class ConfidenceTests(unittest.TestCase):
    def test_component_confidence_from_blind_spots(self):
        self.assertEqual(scored("personality", 4, blind=0).confidence, "alta")
        self.assertEqual(scored("personality", 4, blind=2).confidence, "media")
        self.assertEqual(scored("personality", 4, blind=3).confidence, "baja")

    def test_blind_spot_count_excludes_off_tiles(self):
        comp = scored("attributes", 1, blind=2)  # 1 ok, 2 blind, 2 off
        self.assertEqual(comp.blind_spot_count, 2)
        self.assertEqual(len(comp.off_tiles), 2)
        self.assertEqual(len(comp.blind_spot_tiles), 2)

    def test_non_scored_components_have_no_confidence(self):
        nd = ComponentResult(component="mission", status=STATUS_NOT_DETECTED)
        ne = ComponentResult(component="vision", status=STATUS_NOT_EVALUATED, error="x")
        self.assertIsNone(nd.confidence)
        self.assertIsNone(ne.confidence)
        self.assertEqual(scored("mission", 3).confidence, "alta")


class BaseAverageAndCapTests(unittest.TestCase):
    def test_base_average_normalizes_five_point_scales(self):
        components = full_components(
            mission=scored("mission", 5),
            vision=scored("vision", 0),
            values=scored("values", 5),
            attributes=scored("attributes", 0),
            value_proposition=scored("value_proposition", 10),
            personality=scored("personality", 0),
            brand_idea=scored("brand_idea", 10),
            core_purpose=scored("core_purpose", 0),
        )
        self.assertAlmostEqual(base_average(components), 5.0)

    def test_zero_status_components_count_as_zero_in_base(self):
        components = full_components(
            mission=ComponentResult(component="mission", status=STATUS_NOT_DETECTED),
            vision=ComponentResult(component="vision", status=STATUS_NOT_EVALUATED, error="boom"),
        )
        expected = (0 + 0 + 10 + 10 + 10 + 10 + 10 + 10) / 8
        self.assertAlmostEqual(base_average(components), expected)

    def test_magnetism_capped_on_broken_base(self):
        components = full_components(
            **{key: scored(key, 0) for key in COMPONENTS if key not in {"magnetism", "coherencia"}},
            magnetism=scored("magnetism", 9),
        )
        avg, capped = apply_magnetism_cap(components)
        self.assertTrue(capped)
        self.assertEqual(components["magnetism"].score, 5)
        self.assertLess(avg, 4.0)

    def test_magnetism_not_capped_on_solid_base(self):
        components = full_components(magnetism=scored("magnetism", 9))
        _avg, capped = apply_magnetism_cap(components)
        self.assertFalse(capped)
        self.assertEqual(components["magnetism"].score, 9)

    def test_low_magnetism_untouched_even_on_broken_base(self):
        components = full_components(
            **{key: scored(key, 0) for key in COMPONENTS if key not in {"magnetism", "coherencia"}},
            magnetism=scored("magnetism", 3),
        )
        _avg, capped = apply_magnetism_cap(components)
        self.assertFalse(capped)
        self.assertEqual(components["magnetism"].score, 3)


class AggregateTests(unittest.TestCase):
    def test_perfect_scan_totals_100(self):
        result = aggregate(full_components(), brand_name="Acme", url="https://acme.test")
        self.assertEqual(result.brand3_score, 100)
        self.assertTrue(result.is_complete)
        self.assertIsNone(result.most_painful_gap)
        self.assertEqual(result.immediate_margin, 0)
        self.assertFalse(result.needs_review)
        self.assertEqual(result.reliability_status, "reliable")
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.reliability_reason_codes, [])

    def test_pair_halves_score_alone_and_never_average(self):
        result = aggregate(
            full_components(
                mission=scored("mission", 4),
                vision=ComponentResult(component="vision", status=STATUS_NOT_DETECTED),
            ),
            brand_name="Acme",
            url="https://acme.test",
        )
        self.assertEqual(result.brand3_score, 100 - 5 - 1)

    def test_multipliers_double_magnetism_and_coherencia(self):
        result = aggregate(
            full_components(
                magnetism=scored("magnetism", 7),
                coherencia=scored("coherencia", 6),
            ),
            brand_name="Acme",
            url="https://acme.test",
        )
        self.assertEqual(result.components["magnetism"].points, 14)
        self.assertEqual(result.components["coherencia"].points, 12)
        self.assertEqual(result.brand3_score, 60 + 14 + 12)

    def test_not_evaluated_scores_zero_and_marks_partial(self):
        result = aggregate(
            full_components(
                personality=ComponentResult(
                    component="personality", status=STATUS_NOT_EVALUATED, error="llm_timeout"
                )
            ),
            brand_name="Acme",
            url="https://acme.test",
        )
        self.assertEqual(result.brand3_score, 90)
        self.assertFalse(result.is_complete)
        self.assertEqual(result.not_evaluated, ["personality"])

    def test_needs_review_on_low_coherencia(self):
        result = aggregate(
            full_components(coherencia=scored("coherencia", 3)),
            brand_name="Acme",
            url="https://acme.test",
        )
        self.assertTrue(result.needs_review)

    def test_total_blind_spots_reported(self):
        result = aggregate(
            full_components(
                attributes=scored("attributes", 2, blind=1),
                value_proposition=scored("value_proposition", 6, blind=2),
            ),
            brand_name="Acme",
            url="https://acme.test",
        )
        self.assertEqual(result.total_blind_spots, 3)
        self.assertEqual(result.reliability_status, "shadow")
        self.assertFalse(result.is_reliable)
        self.assertIn("blind_spots_above_usable_threshold", result.reliability_reason_codes)

    def test_reliability_status_allows_small_blind_spots_as_usable(self):
        result = aggregate(
            full_components(attributes=scored("attributes", 2, blind=1)),
            brand_name="Acme",
            url="https://acme.test",
        )
        self.assertEqual(result.total_blind_spots, 1)
        self.assertEqual(result.reliability_status, "usable")
        self.assertFalse(result.is_reliable)
        self.assertIn("blind_spots_present", result.reliability_reason_codes)

    def test_aggregate_requires_every_component(self):
        components = full_components()
        components.pop("coherencia")
        with self.assertRaises(ValueError):
            aggregate(components, brand_name="Acme", url="https://acme.test")

    def test_owned_expression_is_capped_when_only_external_proof_supports_it(self):
        result = aggregate(
            full_components(
                mission=scored(
                    "mission",
                    5,
                    evidence_source_summary={
                        "owned_copy": 0,
                        "external_proof": 2,
                        "visual_signal": 0,
                        "derived_strategy": 0,
                        "acquisition_metadata": 0,
                        "other": 0,
                        "total": 2,
                    },
                )
            ),
            brand_name="Acme",
            url="https://acme.test",
        )

        mission = result.components["mission"]
        self.assertEqual(mission.score, 3)
        self.assertIn("source_policy:mission_requires_owned_expression", mission.source_policy_notes)
        self.assertEqual(len(mission.lit_tiles), 3)
        self.assertEqual(len(mission.blind_spot_tiles), 2)

    def test_implied_owned_expression_is_capped_and_coherence_follows(self):
        result = aggregate(
            full_components(
                mission=scored("mission", 5, detection_limitations=["coverage:mission_implied_not_explicit"]),
                vision=scored("vision", 5, detection_limitations=["coverage:vision_implied_not_explicit"]),
                values=scored("values", 5, detection_limitations=["coverage:values_implied_not_explicit"]),
                coherencia=scored("coherencia", 10),
            ),
            brand_name="Acme",
            url="https://acme.test",
        )

        self.assertEqual(result.components["mission"].score, 3)
        self.assertEqual(result.components["vision"].score, 3)
        self.assertEqual(result.components["values"].score, 3)
        self.assertEqual(result.components["coherencia"].score, 5)
        self.assertIn(
            "source_policy:coherencia_capped_after_component_authority_caps",
            result.components["coherencia"].source_policy_notes,
        )

    def test_external_proof_can_support_but_not_max_out_magnetism_without_owned_surface(self):
        result = aggregate(
            full_components(
                magnetism=scored(
                    "magnetism",
                    10,
                    evidence_source_summary={
                        "owned_copy": 0,
                        "external_proof": 4,
                        "visual_signal": 0,
                        "derived_strategy": 0,
                        "acquisition_metadata": 0,
                        "other": 0,
                        "total": 4,
                    },
                )
            ),
            brand_name="Acme",
            url="https://acme.test",
        )

        self.assertEqual(result.components["magnetism"].score, 7)
        self.assertIn(
            "source_policy:magnetism_external_proof_without_owned_surface",
            result.components["magnetism"].source_policy_notes,
        )


class ImmediateMarginTests(unittest.TestCase):
    def test_margin_counts_one_tile_per_component_with_multiplier(self):
        components = full_components(
            mission=scored("mission", 3),  # +1
            coherencia=scored("coherencia", 8),  # +2
        )
        self.assertEqual(immediate_margin(components, magnetism_capped=False), 3)

    def test_not_detected_components_offer_their_first_tile(self):
        components = full_components(
            values=ComponentResult(component="values", status=STATUS_NOT_DETECTED),
        )
        self.assertEqual(immediate_margin(components, magnetism_capped=False), 1)

    def test_technical_failures_promise_nothing(self):
        components = full_components(
            values=ComponentResult(component="values", status=STATUS_NOT_EVALUATED, error="x"),
        )
        self.assertEqual(immediate_margin(components, magnetism_capped=False), 0)

    def test_capped_magnetism_gains_nothing_from_one_tile(self):
        components = full_components(magnetism=scored("magnetism", 5))
        self.assertEqual(immediate_margin(components, magnetism_capped=True), 0)
        self.assertEqual(immediate_margin(components, magnetism_capped=False), 2)


class MostPainfulGapTests(unittest.TestCase):
    def test_widest_gap_wins(self):
        components = full_components(
            mission=scored("mission", 4),  # gap 1
            core_purpose=scored("core_purpose", 2),  # gap 8
        )
        self.assertEqual(most_painful_gap(components), "core_purpose")

    def test_tie_breaks_toward_heavier_component(self):
        components = full_components(
            mission=scored("mission", 0),  # gap 5
            coherencia=scored("coherencia", 7),  # gap 6
        )
        self.assertEqual(most_painful_gap(components), "coherencia")

    def test_excludes_technical_failures(self):
        components = full_components(
            personality=ComponentResult(
                component="personality", status=STATUS_NOT_EVALUATED, error="x"
            ),
            mission=scored("mission", 4),
        )
        self.assertEqual(most_painful_gap(components), "mission")


if __name__ == "__main__":
    unittest.main()
