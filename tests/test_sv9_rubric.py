import unittest

from src.sv9.rubric import (
    BASE_COMPONENTS,
    COMPONENTS,
    PRESENTATION_ORDER,
    REASONING_COMPONENTS,
    RUBRIC_VERSION,
    TILE_EVIDENCE_CONTRACT_VERSION,
    component_max_points,
    component_points,
    confidence_from_blind_spots,
    tile_ids,
    tile_index,
)


class Sv9RubricTests(unittest.TestCase):
    def test_brand3_score_totals_100(self):
        self.assertEqual(sum(component_max_points(k) for k in COMPONENTS), 100)

    def test_nine_components_plus_coherencia(self):
        self.assertEqual(len(COMPONENTS), 10)
        self.assertIn("coherencia", COMPONENTS)
        self.assertEqual(len(BASE_COMPONENTS), 8)
        self.assertNotIn("magnetism", BASE_COMPONENTS)
        self.assertNotIn("coherencia", BASE_COMPONENTS)

    def test_exactly_80_tiles(self):
        self.assertEqual(sum(len(spec["tiles"]) for spec in COMPONENTS.values()), 80)

    def test_presentation_order_covers_everything_and_closes_with_coherencia(self):
        self.assertEqual(set(PRESENTATION_ORDER), set(COMPONENTS))
        self.assertEqual(PRESENTATION_ORDER[-1], "coherencia")
        self.assertEqual(PRESENTATION_ORDER[-2], "magnetism")

    def test_tiles_match_scales(self):
        for key, spec in COMPONENTS.items():
            self.assertEqual(len(spec["tiles"]), spec["scale"], key)

    def test_tile_ids_are_unique_and_non_empty(self):
        seen = set()
        for key in COMPONENTS:
            for tid in tile_ids(key):
                self.assertTrue(tid.strip(), key)
                self.assertNotIn(tid, seen, f"duplicate {tid}")
                seen.add(tid)
        self.assertEqual(len(seen), 80)

    def test_expected_tile_prefixes(self):
        self.assertEqual(tile_ids("mission"), ["M1", "M2", "M3", "M4", "M5"])
        self.assertEqual(tile_ids("values"), ["VA1", "VA2", "VA3", "VA4", "VA5"])
        self.assertEqual(tile_ids("magnetism")[0], "MG1")
        self.assertEqual(tile_ids("coherencia")[-1], "C10")

    def test_tile_index_maps_id_to_component(self):
        index = tile_index()
        self.assertEqual(index["M1"]["component"], "mission")
        self.assertEqual(index["C8"]["component"], "coherencia")
        self.assertEqual(len(index), 80)

    def test_c8_carries_evaluator_note(self):
        c8 = next(t for t in COMPONENTS["coherencia"]["tiles"] if t["id"] == "C8")
        self.assertIn("sin_evidencia", c8["note"])
        self.assertIn("reviews", c8["note"])

    def test_pairs_are_five_point_scales(self):
        pairs = {}
        for key, spec in COMPONENTS.items():
            if spec["pair"]:
                pairs.setdefault(spec["pair"], []).append(key)
        self.assertEqual(set(pairs), {"mission_vision", "values_attributes"})
        for members in pairs.values():
            self.assertEqual(len(members), 2)
            for member in members:
                self.assertEqual(COMPONENTS[member]["scale"], 5)

    def test_multipliers_only_on_magnetism_and_coherencia(self):
        for key, spec in COMPONENTS.items():
            expected = 2 if key in {"magnetism", "coherencia"} else 1
            self.assertEqual(spec["multiplier"], expected, key)

    def test_component_points_applies_multiplier(self):
        self.assertEqual(component_points("magnetism", 7), 14)
        self.assertEqual(component_points("coherencia", 10), 20)
        self.assertEqual(component_points("mission", 5), 5)

    def test_relational_context_needs_reference_real_components(self):
        for key, spec in COMPONENTS.items():
            for tile in spec["tiles"]:
                for dep in tile.get("context_needs", []):
                    self.assertIn(dep, COMPONENTS, f"{key} tile {tile['id']}")

    def test_coherencia_has_no_detection(self):
        self.assertIsNone(COMPONENTS["coherencia"]["tldr_key"])

    def test_confidence_thresholds(self):
        self.assertEqual(confidence_from_blind_spots(0), "alta")
        self.assertEqual(confidence_from_blind_spots(1), "alta")
        self.assertEqual(confidence_from_blind_spots(2), "media")
        self.assertEqual(confidence_from_blind_spots(3), "baja")
        self.assertEqual(confidence_from_blind_spots(5), "baja")

    def test_rubric_version_is_pinned(self):
        self.assertEqual(RUBRIC_VERSION, "baldosas-v3-1")

    def test_tile_evidence_contract_version_is_pinned(self):
        self.assertEqual(TILE_EVIDENCE_CONTRACT_VERSION, "tile-evidence-contract-v0-1")

    def test_all_tiles_have_evidence_contracts(self):
        for key in COMPONENTS:
            for tile in COMPONENTS[key]["tiles"]:
                contract = tile.get("evidence_contract")
                self.assertIsInstance(contract, dict, tile["id"])
                for field in ("ok", "no", "sin_evidencia", "strong_sources", "reject"):
                    self.assertTrue(str(contract.get(field) or "").strip(), f"{tile['id']} {field}")

    def test_reasoning_components_are_the_two_heavy_judgments(self):
        self.assertEqual(set(REASONING_COMPONENTS), {"magnetism", "coherencia"})
        for key in REASONING_COMPONENTS:
            self.assertEqual(COMPONENTS[key]["multiplier"], 2)


if __name__ == "__main__":
    unittest.main()
