import copy
import unittest

from src.sv9.models import (
    ComponentResult,
    ESTADO_NO,
    ESTADO_OK,
    ESTADO_SIN_EVIDENCIA,
    STATUS_NOT_DETECTED,
    STATUS_SCORED,
    TileVerdict,
)
from src.sv9.rubric import COMPONENTS, tile_ids
from src.sv9.source_policy import (
    ASSESSMENT_TILE_REASSESSMENT_REQUIRED,
    LEGACY_COMPATIBILITY_ONLY,
    LEGACY_SOURCE_POLICY_VERSION,
    SOURCE_POLICY_AXIS_CLASSIFICATION_VERSION,
    SOURCE_POLICY_REASON_CLASSIFICATIONS,
    SOURCE_POLICY_VERSION,
    apply_source_policy,
    build_source_policy_plan,
)


def _scored(
    key: str,
    score: int | None = None,
    *,
    summary: dict[str, int] | None = None,
    limitations: list[str] | None = None,
) -> ComponentResult:
    score = int(COMPONENTS[key]["scale"]) if score is None else score
    profile = [
        TileVerdict(tile_id=tile_id, estado=ESTADO_OK, evidencia="cita")
        if index < score
        else TileVerdict(tile_id=tile_id, estado=ESTADO_NO, motivo="motivo")
        for index, tile_id in enumerate(tile_ids(key))
    ]
    return ComponentResult(
        component=key,
        status=STATUS_SCORED,
        score=score,
        tile_profile=profile,
        evidence_source_summary=summary or {},
        detection_limitations=limitations or [],
    )


def _components(**overrides: ComponentResult) -> dict[str, ComponentResult]:
    components = {key: _scored(key) for key in COMPONENTS}
    components.update(overrides)
    return components


def _rule_matrix() -> dict[str, ComponentResult]:
    return _components(
        mission=_scored("mission", limitations=["coverage:mission_verified_absent"]),
        vision=_scored("vision", limitations=["coverage:vision_implied_not_explicit"]),
        values=_scored("values", summary={"external_proof": 2, "total": 2}),
        core_purpose=_scored("core_purpose", summary={"owned_copy": 1, "external_proof": 3, "total": 4}),
        personality=_scored("personality", summary={"derived_strategy": 1, "total": 0}),
        magnetism=_scored("magnetism", summary={"external_proof": 4, "total": 4}),
    )


class SourcePolicyAxisClassificationContractTests(unittest.TestCase):
    def test_plan_is_pure_and_classifies_every_legacy_rule(self):
        components = _rule_matrix()
        before = copy.deepcopy(components)

        plan = build_source_policy_plan(components)

        self.assertEqual(components, before)
        self.assertEqual(plan, build_source_policy_plan(copy.deepcopy(components)))
        self.assertEqual(plan.axis_classification_version, SOURCE_POLICY_AXIS_CLASSIFICATION_VERSION)
        self.assertEqual(plan.legacy_policy_version, LEGACY_SOURCE_POLICY_VERSION)
        self.assertEqual(SOURCE_POLICY_VERSION, "sv9-source-authority-v1")
        self.assertEqual(
            [action.reason for action in plan.actions],
            [
                "source_policy:mission_owned_expression_verified_absent",
                "source_policy:vision_implied_not_explicit",
                "source_policy:values_requires_owned_expression",
                "source_policy:personality_derived_strategy_is_not_primary_evidence",
                "source_policy:core_purpose_external_proof_dominates_owned_expression",
                "source_policy:magnetism_external_proof_without_owned_surface",
            ],
        )
        self.assertEqual(
            [action.axis_classification for action in plan.actions],
            [ASSESSMENT_TILE_REASSESSMENT_REQUIRED] * 6,
        )
        self.assertEqual(
            set(SOURCE_POLICY_REASON_CLASSIFICATIONS),
            {
                "source_policy:{component}_owned_expression_verified_absent",
                "source_policy:{component}_implied_not_explicit",
                "source_policy:{component}_requires_owned_expression",
                "source_policy:{component}_external_proof_dominates_owned_expression",
                "source_policy:{component}_derived_strategy_is_not_primary_evidence",
                "source_policy:{component}_external_proof_without_owned_surface",
                "source_policy:coherencia_capped_after_component_authority_caps",
            },
        )
        self.assertNotIn("verification", set(SOURCE_POLICY_REASON_CLASSIFICATIONS.values()))
        self.assertNotIn("authority_transition", set(SOURCE_POLICY_REASON_CLASSIFICATIONS.values()))

    def test_boundary_and_non_scored_component_do_not_change_legacy_output(self):
        components = _components(
            mission=_scored("mission", 3, summary={"external_proof": 2, "total": 2}),
            vision=ComponentResult(
                component="vision",
                status=STATUS_NOT_DETECTED,
                detection_limitations=["coverage:vision_verified_absent"],
            ),
        )
        before = copy.deepcopy(components)

        plan = build_source_policy_plan(components)

        self.assertEqual([action.component for action in plan.actions], ["mission"])
        self.assertFalse(apply_source_policy(components))
        self.assertEqual(components, before)

    def test_coherence_cap_counts_actual_sequential_mutations(self):
        for capped_count, expected_coherence in ((1, 6), (2, 6), (3, 5)):
            with self.subTest(capped_count=capped_count):
                components = _components()
                for key in ("mission", "vision", "values")[:capped_count]:
                    components[key] = _scored(key, summary={"external_proof": 2, "total": 2})

                self.assertTrue(apply_source_policy(components))

                self.assertEqual(components["coherencia"].score, expected_coherence)

    def test_shared_component_alias_counts_only_the_first_actual_cap(self):
        shared = _scored(
            "mission",
            summary={"external_proof": 2, "total": 2},
        )
        components = _components(mission=shared, vision=shared, values=shared)

        self.assertTrue(apply_source_policy(components))

        self.assertEqual(shared.score, 3)
        self.assertEqual(components["coherencia"].score, 6)

    def test_non_finite_score_keeps_legacy_actual_change_semantics(self):
        mission = _scored(
            "mission",
            summary={"external_proof": 2, "total": 2},
        )
        mission.score = float("nan")
        components = _components(mission=mission)

        self.assertTrue(apply_source_policy(components))

        self.assertEqual(mission.score, 5)
        self.assertEqual(components["coherencia"].score, 6)

    def test_late_invalid_identity_preserves_legacy_partial_mutation_order(self):
        mission = _scored("mission", summary={"external_proof": 2, "total": 2})
        vision = _scored("vision", summary={"external_proof": 2, "total": 2})
        invalid = _scored("values", summary={"external_proof": 2, "total": 2})
        invalid.component = "unknown"
        components = _components(mission=mission, vision=vision, values=invalid)

        with self.assertRaises(KeyError):
            apply_source_policy(components)

        self.assertEqual(mission.score, 3)
        self.assertEqual(vision.score, 3)
        self.assertEqual(components["coherencia"].score, 10)

    def test_late_classification_failure_preserves_legacy_partial_mutation(self):
        mission = _scored("mission", summary={"external_proof": 2, "total": 2})
        invalid = _scored("values")
        invalid.detection_limitations = [None]
        components = _components(mission=mission, values=invalid)

        with self.assertRaises(AttributeError):
            build_source_policy_plan(components)
        self.assertEqual(mission.score, 5)
        self.assertEqual(mission.source_policy_notes, [])

        with self.assertRaises(AttributeError):
            apply_source_policy(components)
        self.assertEqual(mission.score, 3)
        self.assertEqual(
            mission.source_policy_notes,
            ["source_policy:mission_requires_owned_expression"],
        )
        self.assertEqual(components["coherencia"].score, 10)

    def test_apply_matches_frozen_legacy_scores_tiles_notes_and_dicts(self):
        components = _rule_matrix()
        before = copy.deepcopy(components)
        expected_caps = [
            ("mission", 3, "source_policy:mission_owned_expression_verified_absent"),
            ("vision", 3, "source_policy:vision_implied_not_explicit"),
            ("values", 3, "source_policy:values_requires_owned_expression"),
            ("personality", 4, "source_policy:personality_derived_strategy_is_not_primary_evidence"),
            ("core_purpose", 6, "source_policy:core_purpose_external_proof_dominates_owned_expression"),
            ("magnetism", 7, "source_policy:magnetism_external_proof_without_owned_surface"),
            ("coherencia", 5, "source_policy:coherencia_capped_after_component_authority_caps"),
        ]

        self.assertTrue(apply_source_policy(components))

        for key, cap, reason in expected_caps:
            component = components[key]
            self.assertEqual(component.score, cap)
            self.assertEqual(component.lit_tiles, tile_ids(key)[:cap])
            self.assertEqual(component.blind_spot_tiles, tile_ids(key)[cap:])
            self.assertEqual(component.source_policy_notes, [reason])
            expected_motive = (
                f"sv9-source-authority-v1: la fuente citada no tiene autoridad suficiente para esta baldosa ({reason})."
            )
            self.assertEqual(
                [verdict.motivo for verdict in component.tile_profile[cap:]],
                [expected_motive] * (len(component.tile_profile) - cap),
            )
        changed_keys = {key for key, _, _ in expected_caps}
        self.assertEqual(
            {key: component.to_dict() for key, component in components.items() if key not in changed_keys},
            {key: component.to_dict() for key, component in before.items() if key not in changed_keys},
        )


if __name__ == "__main__":
    unittest.main()
