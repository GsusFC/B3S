from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.storage.sqlite_store import SQLiteStore
from src.visual_signature.persistence import build_visual_signature_persistence_bundle


class VisualSignaturePersistenceTests(unittest.TestCase):
    def test_build_visual_signature_persistence_bundle_serializes_evidence_only(self):
        bundle = build_visual_signature_persistence_bundle(
            raw_visual_signature_payload={
                "brand_name": "Example",
                "website_url": "https://example.com",
                "interpretation_status": "interpretable",
                "acquisition": {"status_code": 200, "warnings": [], "errors": []},
                "vision": {
                    "screenshot": {
                        "available": True,
                        "quality": "usable",
                        "capture_type": "viewport",
                        "width": 1440,
                        "height": 900,
                        "viewport_width": 1440,
                        "viewport_height": 900,
                    },
                    "agreement": {
                        "agreement_level": "medium",
                        "disagreement_flags": ["dom_density_higher_than_viewport"],
                        "summary_notes": ["DOM suggests a denser page than the viewport first impression."],
                    },
                },
            },
            vision_payload={
                "screenshot": {
                    "available": True,
                    "quality": "usable",
                    "capture_type": "viewport",
                    "width": 1440,
                    "height": 900,
                    "viewport_width": 1440,
                    "viewport_height": 900,
                },
                "viewport_composition": {
                    "visual_density": "balanced",
                    "whitespace_ratio": 0.42,
                    "composition_classification": "balanced_blocks",
                },
                "agreement": {
                    "agreement_level": "medium",
                    "disagreement_flags": ["dom_density_higher_than_viewport"],
                    "summary_notes": ["DOM suggests a denser page than the viewport first impression."],
                },
            },
            agreement_payload={
                "agreement_level": "medium",
                "disagreement_flags": ["dom_density_higher_than_viewport"],
                "summary_notes": ["DOM suggests a denser page than the viewport first impression."],
            },
            run_id=17,
            brand_name="Example",
            website_url="https://example.com",
            screenshot_path=Path("/tmp/example-screenshot.png"),
            secondary_screenshot_path=Path("/tmp/example-screenshot.full-page.png"),
            full_page_screenshot_path=Path("/tmp/example-screenshot.full-page.png"),
            section_screenshot_paths=[
                "/tmp/example-screenshot.section-01-hero.png",
                "/tmp/example-screenshot.section-02-proof.png",
            ],
            section_manifest={
                "schema_version": "rendered-page-section-manifest-v1",
                "section_count": 2,
            },
            analysis_atlas_path=Path("/tmp/example-screenshot.analysis-atlas.png"),
            analysis_atlas_manifest={
                "schema_version": "visual-analysis-atlas-v1",
                "panel_count": 3,
            },
            manifest_path=Path("/tmp/capture_manifest.json"),
            capture_type="viewport",
            secondary_capture_type="full_page",
            visual_signature_scan={
                "schema_version": "visual-signature-scan-v1",
                "status": "ready",
                "score": 74.5,
                "dimensions": {},
            },
        )

        payload = bundle.to_dict()
        json.dumps(payload)

        self.assertEqual(payload["schema_version"], "visual-signature-persistence-1")
        self.assertEqual(payload["run_id"], 17)
        self.assertEqual(payload["brand_name"], "Example")
        self.assertEqual(payload["website_url"], "https://example.com")
        self.assertEqual(payload["run_metadata"]["acquisition_status"], "ok")
        self.assertTrue(payload["run_metadata"]["screenshot_available"])
        self.assertTrue(payload["run_metadata"]["viewport_available"])
        self.assertTrue(payload["run_metadata"]["full_page_available"])
        self.assertEqual(payload["run_metadata"]["interpretation_status"], "interpretable")
        self.assertEqual(payload["run_metadata"]["agreement_level"], "medium")
        self.assertEqual(payload["run_metadata"]["visual_signature_scan_status"], "ready")
        self.assertEqual(payload["run_metadata"]["visual_signature_score"], 74.5)
        self.assertEqual(payload["artifact_refs"]["capture_type"], "viewport")
        self.assertEqual(payload["artifact_refs"]["secondary_capture_type"], "full_page")
        self.assertEqual(payload["artifact_refs"]["screenshot_path"], "/tmp/example-screenshot.png")
        self.assertEqual(
            payload["artifact_refs"]["secondary_screenshot_path"],
            "/tmp/example-screenshot.full-page.png",
        )
        self.assertEqual(payload["artifact_refs"]["manifest_path"], "/tmp/capture_manifest.json")
        self.assertEqual(
            payload["artifact_refs"]["full_page_screenshot_path"],
            "/tmp/example-screenshot.full-page.png",
        )
        self.assertEqual(payload["run_metadata"]["section_capture_count"], 2)
        self.assertEqual(payload["run_metadata"]["semantic_analysis_scope"], "labeled_section_atlas")
        self.assertEqual(
            payload["artifact_refs"]["section_manifest"]["schema_version"],
            "rendered-page-section-manifest-v1",
        )
        self.assertEqual(
            payload["artifact_refs"]["analysis_atlas_path"],
            "/tmp/example-screenshot.analysis-atlas.png",
        )
        self.assertEqual(payload["agreement_payload"]["agreement_level"], "medium")
        self.assertEqual(payload["vision_payload"]["viewport_composition"]["visual_density"], "balanced")
        self.assertEqual(payload["visual_signature_scan"]["schema_version"], "visual-signature-scan-v1")
        self.assertEqual(payload["raw_visual_signature_payload"]["brand_name"], "Example")

    def test_sqlite_store_round_trips_visual_signature_raw_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            store = SQLiteStore(str(db_path))
            brand_id = store.upsert_brand("Example", "https://example.com")
            run_id = store.create_run(brand_id, "Example", "https://example.com", True, False)

            bundle = build_visual_signature_persistence_bundle(
                raw_visual_signature_payload={
                    "brand_name": "Example",
                    "website_url": "https://example.com",
                    "interpretation_status": "not_interpretable",
                    "acquisition": {
                        "status_code": None,
                        "warnings": [],
                        "errors": ["fixture acquisition failure"],
                    },
                },
                run_id=run_id,
                brand_name="Example",
                website_url="https://example.com",
                manifest_path=Path(tmpdir) / "capture_manifest.json",
                capture_type="viewport",
            )

            store.save_visual_signature_evidence(run_id, bundle.to_dict())
            latest = store.get_latest_visual_signature_evidence("Example", "https://example.com")
            snapshot = store.get_run_snapshot(run_id)
            store.close()

            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest["schema_version"], "visual-signature-persistence-1")
            self.assertEqual(latest["run_metadata"]["acquisition_status"], "error")
            self.assertEqual(latest["run_metadata"]["interpretation_status"], "not_interpretable")
            self.assertEqual(latest["artifact_refs"]["capture_type"], "viewport")
            self.assertEqual(latest["brand_name"], "Example")
            self.assertEqual(latest["website_url"], "https://example.com")
            self.assertEqual(snapshot["raw_inputs"][-1]["source"], "visual_acquisition")
            self.assertEqual(snapshot["raw_inputs"][-1]["payload"]["run_metadata"]["acquisition_status"], "error")
            self.assertIn("visual_evidence_packet", snapshot["raw_inputs"][-1]["payload"])
