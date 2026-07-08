import tempfile
import unittest
from pathlib import Path

from src.sv9.aggregator import aggregate
from src.sv9.models import (
    ComponentResult,
    ESTADO_NO,
    ESTADO_OK,
    ESTADO_SIN_EVIDENCIA,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    TileVerdict,
)
from src.sv9.rubric import COMPONENTS, MODEL_LABEL, RUBRIC_VERSION, tile_ids
from src.sv9.store import Sv9Store, load_tile_profile


def scored(key: str, score: int, *, blind: int = 0) -> ComponentResult:
    ids = tile_ids(key)
    profile = []
    for i, tid in enumerate(ids):
        if i < score:
            profile.append(TileVerdict(tile_id=tid, estado=ESTADO_OK, evidencia="q"))
        elif i < score + blind:
            profile.append(TileVerdict(tile_id=tid, estado=ESTADO_SIN_EVIDENCIA, motivo="m"))
        else:
            profile.append(TileVerdict(tile_id=tid, estado=ESTADO_NO, motivo="m"))
    return ComponentResult(
        component=key,
        status=STATUS_SCORED,
        score=score,
        tile_profile=profile,
        detected_content=f"{key} text",
        detection_mode="literal",
        detection_confidence="high",
        evidence=[f"{key} quote"],
    )


class Sv9StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "sv9_test.sqlite3")
        self.store = Sv9Store(self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _result(self):
        components = {key: scored(key, 3) for key in COMPONENTS}
        components["attributes"] = scored("attributes", 2, blind=2)
        components["coherencia"].veredicto = "La marca cuenta una historia única."
        components["coherencia"].evaluation_model = "reasoning-tier"
        components["mission"].evaluation_model = "flash-tier"
        components["mission"].message = "La misión traduce el producto en una promesa clara."
        components["values"] = ComponentResult(
            component="values", status=STATUS_NOT_EVALUATED, error="llm_timeout"
        )
        return aggregate(
            components, brand_name="Acme", url="https://acme.test", source_run_id=175
        )

    def test_scan_roundtrip(self):
        result = self._result()
        scan_id = self.store.save_scan(result)
        loaded = self.store.get_scan(scan_id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["brand_name"], "Acme")
        self.assertEqual(loaded["source_run_id"], 175)
        self.assertEqual(loaded["rubric_version"], RUBRIC_VERSION)
        self.assertEqual(loaded["model"], MODEL_LABEL)
        self.assertEqual(loaded["brand3_score"], result.brand3_score)
        self.assertEqual(loaded["is_complete"], 0)
        self.assertEqual(len(loaded["components"]), 10)

        by_component = {c["component"]: c for c in loaded["components"]}
        self.assertEqual(by_component["mission"]["score"], 3)
        self.assertEqual(len(by_component["mission"]["tile_profile"]), 5)
        self.assertEqual(by_component["mission"]["evaluation_model"], "flash-tier")
        self.assertEqual(by_component["mission"]["message"], "La misión traduce el producto en una promesa clara.")
        self.assertEqual(by_component["coherencia"]["veredicto"], "La marca cuenta una historia única.")
        self.assertEqual(by_component["coherencia"]["evaluation_model"], "reasoning-tier")
        self.assertEqual(by_component["attributes"]["confidence"], "media")
        self.assertEqual(by_component["values"]["status"], STATUS_NOT_EVALUATED)
        self.assertEqual(by_component["values"]["error"], "llm_timeout")
        self.assertEqual(by_component["coherencia"]["points"], 6)

    def test_load_tile_profile_rebuilds_typed_verdicts(self):
        scan_id = self.store.save_scan(self._result())
        loaded = self.store.get_scan(scan_id)
        mission = next(c for c in loaded["components"] if c["component"] == "mission")
        profile = load_tile_profile(mission)
        self.assertEqual(len(profile), 5)
        self.assertEqual(profile[0].tile_id, "M1")
        self.assertEqual(profile[0].estado, ESTADO_OK)

    def test_get_scan_for_run_returns_latest(self):
        first = self.store.save_scan(self._result())
        second = self.store.save_scan(self._result())
        loaded = self.store.get_scan_for_run(175)
        self.assertEqual(loaded["id"], second)
        self.assertNotEqual(loaded["id"], first)

    def test_tile_calibration_records_states_and_traces_rubric(self):
        scan_id = self.store.save_scan(self._result())
        self.store.save_tile_calibration(
            scan_id=scan_id,
            url="https://acme.test",
            component="mission",
            baldosa_id="M3",
            estado_ia="no",
            estado_humano="ok",
            motivo="la misión sí está anclada al problema",
            evaluador="sergio",
            rubric_version=RUBRIC_VERSION,
        )
        labels = self.store.list_tile_calibration(scan_id=scan_id)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["baldosa_id"], "M3")
        self.assertEqual(labels[0]["estado_ia"], "no")
        self.assertEqual(labels[0]["estado_humano"], "ok")
        self.assertEqual(labels[0]["rubric_version"], RUBRIC_VERSION)

    def test_tile_calibration_filter_by_baldosa(self):
        scan_id = self.store.save_scan(self._result())
        for tid, est in (("M1", "ok"), ("M2", "no")):
            self.store.save_tile_calibration(
                scan_id=scan_id, url="u", component="mission", baldosa_id=tid,
                estado_ia=est, estado_humano=est, motivo=None,
                evaluador="s", rubric_version=RUBRIC_VERSION,
            )
        self.assertEqual(len(self.store.list_tile_calibration(baldosa_id="M1")), 1)

    def test_editorial_decision_roundtrip_and_replace(self):
        scan_id = self.store.save_scan(self._result())
        self.store.save_editorial_decision(
            scan_id=scan_id,
            component="mission",
            decision="v9",
            note="más pegado al score",
            evaluator="sergio",
        )
        decisions = self.store.list_editorial_decisions(scan_id)
        self.assertEqual(decisions["mission"]["decision"], "v9")
        self.assertEqual(decisions["mission"]["note"], "más pegado al score")

        self.store.save_editorial_decision(
            scan_id=scan_id,
            component="mission",
            decision="mix",
            note="v9 diagnóstico + v2 tono",
            evaluator="ana",
        )
        decisions = self.store.list_editorial_decisions(scan_id)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions["mission"]["decision"], "mix")
        self.assertEqual(decisions["mission"]["evaluator"], "ana")

    def test_detection_cache_roundtrip_and_replace(self):
        self.assertIsNone(self.store.get_detection(175))
        self.store.save_detection(175, {"tldr_brand3": {"mission": {"content": "v1"}}})
        self.assertEqual(
            self.store.get_detection(175)["tldr_brand3"]["mission"]["content"], "v1"
        )
        self.store.save_detection(175, {"tldr_brand3": {"mission": {"content": "v2"}}})
        self.assertEqual(
            self.store.get_detection(175)["tldr_brand3"]["mission"]["content"], "v2"
        )
        runs = self.store.list_detection_runs(175, source="cache_pin")
        self.assertGreaterEqual(len(runs), 1)
        self.assertEqual(
            runs[0]["payload"]["tldr_brand3"]["mission"]["content"],
            "v2",
        )
        self.assertIn("mission", runs[0]["block_hashes"])

    def test_record_detection_run_persists_fingerprint_and_payload(self):
        payload = {
            "tldr_brand3": {
                "mission": {"content": "v1"},
                "vision": {"content": "v2"},
            }
        }
        row_id = self.store.record_detection_run(188, payload, source="stability_probe")
        self.assertGreater(row_id, 0)
        rows = self.store.list_detection_runs(188, source="stability_probe")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["tldr_brand3"]["mission"]["content"], "v1")
        self.assertIn("mission", rows[0]["block_hashes"])

    def test_migration_is_idempotent(self):
        again = Sv9Store(self.db_path)
        again.close()
        self.assertEqual(self.store.list_scans(), [])


if __name__ == "__main__":
    unittest.main()
