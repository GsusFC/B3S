import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.collectors.context_collector import ContextData
from src.collectors.exa_collector import EXA_STRATEGY_VERSION, ExaData, ExaResult
from src.collectors.social_collector import PlatformMetrics, SocialData
from src.collectors.web_collector import WebData
from src.services import brand_service
from src.config import (
    AUDIT_ANALYST_MODEL,
    DEFAULT_LLM_CHEAP_MODEL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_PREMIUM_MODEL,
    DEFAULT_VISION_MODEL,
    LLM_CHEAP_MODEL,
    SCREENSHOT_PROVIDER,
)
from src.services.brand_service import (
    _aggregate_exa_content,
    _acquisition_provenance_summary,
    _audit_analyst_llm,
    _build_content_web,
    _build_research_pack_for_feature_prompts,
    _compute_data_quality,
    _context_effective_readiness,
    _context_enrichment_summary,
    _context_confidence_summary,
    _context_evidence_items,
    _cost_policy_summary,
    _dimension_confidence_summary,
    _infer_llm_provider,
    _llm_cache_summary,
    _llm_model_roles_payload,
    _llm_provider_payload,
    _public_presence_inventory_summary,
    _persist_report_readiness,
    ensure_visual_signature_for_existing_run,
    run_visual_signature_for_existing_run,
    _recover_owned_web_content,
    _run_visual_signature_shadow,
    _screenshot_capture_diagnostic,
    _take_screenshot_with_budget,
    _should_skip_llm_for_low_context,
    _trust_summary_payload,
)
from src.discovery.summary import format_discovery_summary
from src.models.brand import FeatureValue
from src.quality.evidence_summary import summarize_evidence_from_features
from src.storage.sqlite_store import SQLiteStore


class _VisualSignatureStore:
    def __init__(self, *, should_fail: bool = False):
        self.should_fail = should_fail
        self.saved = []

    def save_visual_signature_evidence(self, run_id, payload):
        if self.should_fail:
            raise RuntimeError("fixture persistence failure")
        self.saved.append((run_id, payload))


class _SnapshotStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_run_snapshot(self, run_id):
        return self.snapshot


class BrandAuditReadinessPersistenceTests(unittest.TestCase):
    def test_research_pack_for_feature_prompts_uses_legacy_when_graph_is_not_recommended(self):
        snapshot = {"run": {"id": 1, "brand_name": "Example", "url": "https://example.com"}}

        class _Recommended:
            pack = "legacy-pack"

        with patch("src.services.brand_service.build_recommended_research_pack", return_value=_Recommended()) as build_mock:
            pack = _build_research_pack_for_feature_prompts(store=_SnapshotStore(snapshot), run_id=1)

        self.assertEqual(pack, "legacy-pack")
        build_mock.assert_called_once_with(snapshot)

    def test_acquisition_provenance_summary_exposes_plan_trace_and_quality(self):
        context = ContextData(
            url="https://example.com",
            robots_found=True,
            sitemap_found=True,
            sitemap_url_count=3,
            coverage=0.8,
            confidence=0.8,
        )
        web = WebData(
            url="https://example.com",
            markdown_content="Example brand builds reliable software. " * 12,
        )
        exa = ExaData(
            brand_name="Example",
            mentions=[],
        )

        payload = _acquisition_provenance_summary(
            brand_name="Example",
            url="https://example.com",
            web_data=web,
            exa_data=exa,
            context_data=context,
            discovery_enrichment_payload={"applied": False, "urls_used": [], "queries_used": []},
            raw_input_cache={"context": "hit", "web": "miss", "exa": "partial"},
            content_source="firecrawl",
            data_quality="good",
        )

        assert payload["plan"]["resolved_entity"] == "Example"
        assert payload["trace"]["provider_summary"]["web_cache"] == "miss"
        assert payload["quality"]["metrics"]["planned_sources"] >= 1
        assert payload["quality"]["version"] == "brand_research_acquisition_quality_v0_1"

    def test_persist_report_readiness_updates_run_audit(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            store = SQLiteStore(str(db_path))
            try:
                brand_id = store.upsert_brand("Readiness Brand", "https://readiness.test")
                run_id = store.create_run(
                    brand_id,
                    "Readiness Brand",
                    "https://readiness.test",
                    use_llm=True,
                    use_social=False,
                )
                store.save_run_audit(
                    run_id,
                    {
                        "scoring_state_fingerprint": "readiness123",
                        "executive_analysis_v2": {
                            "executive_summary": "Readable executive summary.",
                        },
                    },
                )
                store.finalize_run(
                    run_id=run_id,
                    composite_score=72.0,
                    llm_used=False,
                    social_scraped=False,
                    result_path="/tmp/readiness.json",
                    summary="summary",
                )

                readiness = _persist_report_readiness(
                    store,
                    run_id,
                    {
                        "scoring_state_fingerprint": "readiness123",
                        "executive_analysis_v2": {
                            "executive_summary": "Readable executive summary.",
                        },
                    },
                )
                snapshot = store.get_run_snapshot(run_id)
            finally:
                store.close()

        self.assertIsNotNone(readiness)
        self.assertEqual(readiness["report_mode"], "insufficient_evidence")
        self.assertEqual(
            snapshot["run"]["audit"]["report_readiness"]["report_mode"],
            "insufficient_evidence",
        )
        self.assertEqual(
            snapshot["run"]["audit"]["publication_decision"]["status"],
            "non_public",
        )
        self.assertEqual(
            snapshot["run"]["audit"]["publication_decision"]["source_status"],
            "insufficient_evidence",
        )


class VisualSignatureShadowRunTests(unittest.TestCase):
    def test_shadow_run_disabled_skips_without_extraction_or_persistence(self):
        calls = {"extractor": 0}

        def extractor(**_kwargs):
            calls["extractor"] += 1
            return {}

        store = _VisualSignatureStore()

        result = _run_visual_signature_shadow(
            enabled=False,
            store=store,
            run_id=1,
            brand_name="Example",
            url="https://example.com",
            web_data=None,
            content_web=None,
            screenshot_capture=None,
            extractor=extractor,
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["events"], ["skipped"])
        self.assertEqual(calls["extractor"], 0)
        self.assertEqual(store.saved, [])

    def test_shadow_run_extracts_enriches_and_persists_evidence(self):
        store = _VisualSignatureStore()

        def extractor(**kwargs):
            self.assertEqual(kwargs["brand_name"], "Example")
            self.assertEqual(kwargs["website_url"], "https://example.com")
            self.assertIsNotNone(kwargs["screenshot_payload"])
            return {
                "brand_name": "Example",
                "website_url": "https://example.com",
                "interpretation_status": "interpretable",
                "acquisition": {"adapter": "existing_web_data", "status_code": 200, "warnings": [], "errors": []},
                "version": "visual-signature-mvp-1",
            }

        def enricher(**kwargs):
            payload = dict(kwargs["visual_signature_payload"])
            payload["vision"] = {
                "screenshot": {
                    "available": True,
                    "path": kwargs["screenshot_path"],
                    "capture_type": "full_page",
                    "quality": "usable",
                },
                "viewport_composition": {"visual_density": "balanced"},
                "agreement": {"agreement_level": "high", "disagreement_flags": [], "summary_notes": []},
            }
            return payload

        result = _run_visual_signature_shadow(
            enabled=True,
            store=store,
            run_id=7,
            brand_name="Example",
            url="https://example.com",
            web_data=None,
            content_web=None,
            screenshot_capture={
                "success": True,
                "source": "playwright",
                "screenshot_url": "file:///tmp/example.png",
            },
            extractor=extractor,
            vision_enricher=enricher,
        )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["persisted"])
        self.assertIn("persisted", result["events"])
        self.assertEqual(result["agreement_level"], "high")
        self.assertEqual(len(store.saved), 1)
        run_id, payload = store.saved[0]
        self.assertEqual(run_id, 7)
        self.assertEqual(payload["run_metadata"]["acquisition_status"], "ok")
        self.assertTrue(payload["run_metadata"]["screenshot_available"])
        self.assertTrue(payload["run_metadata"]["full_page_available"])
        self.assertEqual(payload["visual_signature_scan"]["schema_version"], "visual-signature-scan-v1")
        self.assertGreaterEqual(payload["visual_signature_scan"]["score"], 0)
        self.assertEqual(payload["visual_signature_evidence"]["schema_version"], "visual-signature-evidence-v1")
        self.assertEqual(payload["visual_evidence_packet"]["schema_version"], "visual-signature-evidence-v1")
        self.assertEqual(payload["visual_signature_evidence"]["capture"]["status"], "usable")
        self.assertTrue(payload["visual_signature_evidence"]["tile_signals"])
        self.assertIn("visual_signature_score", result)
        self.assertIn("visual_signature_scan_status", result)
        self.assertEqual(result["visual_evidence_packet"]["schema_version"], "visual-signature-evidence-v1")
        self.assertEqual(result["visual_signature_evidence"]["schema_version"], "visual-signature-evidence-v1")

    def test_run_visual_signature_for_existing_run_reuses_persisted_inputs(self):
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "brand3.sqlite3")
            store = SQLiteStore(db_path)
            try:
                brand_id = store.upsert_brand("Existing", "https://existing.com")
                run_id = store.create_run(brand_id, "Existing", "https://existing.com", True, True)
                store.save_raw_input(
                    run_id,
                    "web",
                    {
                        "url": "https://existing.com",
                        "title": "Existing",
                        "markdown_content": "Existing brand homepage evidence.",
                        "html": "<header><a>Existing</a></header>",
                        "images": [],
                    },
                )
                store.save_raw_input(
                    run_id,
                    "screenshot_capture",
                    {
                        "capture": {
                            "success": True,
                            "source": "playwright",
                            "screenshot_url": "file:///tmp/existing.png",
                            "width": 1440,
                            "height": 900,
                        }
                    },
                )

                def extractor(**kwargs):
                    self.assertEqual(kwargs["web_data"].title, "Existing")
                    self.assertEqual(kwargs["screenshot_payload"]["path"], "/tmp/existing.png")
                    return {
                        "brand_name": kwargs["brand_name"],
                        "website_url": kwargs["website_url"],
                        "analyzed_url": kwargs["website_url"],
                        "interpretation_status": "interpretable",
                        "acquisition": {"status": "ok"},
                        "extraction_confidence": {"score": 0.8, "limitations": []},
                        "logo": {"logo_detected": True, "confidence": 0.8, "primary_location": "header"},
                        "consistency": {"overall_consistency": 0.7},
                        "assets": {},
                    }

                def enricher(**kwargs):
                    payload = dict(kwargs["visual_signature_payload"])
                    payload["vision"] = {
                        "screenshot": {
                            "available": True,
                            "path": kwargs["screenshot_path"],
                            "capture_type": "viewport",
                            "quality": "usable",
                        },
                        "viewport_confidence": {"score": 0.8},
                    }
                    return payload

                result = run_visual_signature_for_existing_run(
                    store=store,
                    run_id=run_id,
                    extractor=extractor,
                    vision_enricher=enricher,
                )

                self.assertEqual(result["status"], "completed")
                snapshot = store.get_run_snapshot(run_id)
                visual_inputs = [
                    item for item in snapshot["raw_inputs"] if item["source"] == "visual_acquisition"
                ]
                self.assertEqual(len(visual_inputs), 1)
                self.assertEqual(
                    visual_inputs[0]["payload"]["visual_signature_scan"]["schema_version"],
                    "visual-signature-scan-v1",
                )
                self.assertEqual(
                    visual_inputs[0]["payload"]["visual_evidence_packet"]["schema_version"],
                    "visual-signature-evidence-v1",
                )
                self.assertEqual(
                    visual_inputs[0]["payload"]["visual_signature_evidence"]["schema_version"],
                    "visual-signature-evidence-v1",
                )
            finally:
                store.close()

    def test_ensure_visual_signature_for_existing_run_skips_existing_scan_and_evidence(self):
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "brand3.sqlite3")
            store = SQLiteStore(db_path)
            try:
                brand_id = store.upsert_brand("Existing", "https://existing.com")
                run_id = store.create_run(brand_id, "Existing", "https://existing.com", True, True)
                store.save_raw_input(
                    run_id,
                    "visual_signature",
                    {
                        "visual_signature_scan": {
                            "schema_version": "visual-signature-scan-v1",
                            "score": 70,
                        },
                        "visual_signature_evidence": {
                            "schema_version": "visual-signature-evidence-v1",
                            "capture": {"status": "usable"},
                        },
                    },
                )

                result = ensure_visual_signature_for_existing_run(
                    store=store,
                    run_id=run_id,
                    extractor=lambda **_kwargs: self.fail("extractor should not run"),
                )

                self.assertEqual(result["status"], "already_available")
                snapshot = store.get_run_snapshot(run_id)
                visual_inputs = [
                    item
                    for item in snapshot["raw_inputs"]
                    if item["source"] in {"visual_signature", "visual_acquisition"}
                ]
                self.assertEqual(len(visual_inputs), 1)
            finally:
                store.close()

    def test_ensure_visual_signature_for_existing_run_refreshes_legacy_scan_without_evidence_contract(self):
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "brand3.sqlite3")
            store = SQLiteStore(db_path)
            try:
                brand_id = store.upsert_brand("Existing", "https://existing.com")
                run_id = store.create_run(brand_id, "Existing", "https://existing.com", True, True)
                store.save_raw_input(
                    run_id,
                    "visual_signature",
                    {
                        "visual_signature_scan": {
                            "schema_version": "visual-signature-scan-v1",
                            "score": 70,
                        }
                    },
                )
                store.save_raw_input(
                    run_id,
                    "web",
                    {
                        "url": "https://existing.com",
                        "title": "Existing",
                        "markdown_content": "Existing homepage.",
                        "html": "<header>Existing</header>",
                        "images": [],
                    },
                )

                def extractor(**kwargs):
                    return {
                        "brand_name": kwargs["brand_name"],
                        "website_url": kwargs["website_url"],
                        "analyzed_url": kwargs["website_url"],
                        "interpretation_status": "interpretable",
                        "acquisition": {"status": "ok"},
                        "extraction_confidence": {"score": 0.8, "limitations": []},
                        "logo": {"logo_detected": True, "confidence": 0.8, "primary_location": "header"},
                        "consistency": {"overall_consistency": 0.7},
                        "assets": {},
                    }

                result = ensure_visual_signature_for_existing_run(
                    store=store,
                    run_id=run_id,
                    extractor=extractor,
                )

                self.assertEqual(result["status"], "completed")
                self.assertTrue(result["persisted"])
                snapshot = store.get_run_snapshot(run_id)
                visual_inputs = [
                    item
                    for item in snapshot["raw_inputs"]
                    if item["source"] in {"visual_signature", "visual_acquisition"}
                ]
                self.assertEqual(len(visual_inputs), 2)
                self.assertEqual(visual_inputs[-1]["source"], "visual_acquisition")
                self.assertEqual(
                    visual_inputs[-1]["payload"]["visual_evidence_packet"]["schema_version"],
                    "visual-signature-evidence-v1",
                )
                self.assertEqual(
                    visual_inputs[-1]["payload"]["visual_signature_evidence"]["schema_version"],
                    "visual-signature-evidence-v1",
                )
            finally:
                store.close()

    def test_run_forwards_run_input_sources_to_collect_raw_inputs(self):
        captured = {}

        class _FakeStorage:
            run_id = 123
            store = SimpleNamespace(close=lambda: None)

        def fake_start_analysis_run(*_args, **_kwargs):
            return _FakeStorage()

        def fake_collect_raw_inputs(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                context_data=SimpleNamespace(url="https://example.com"),
                web_data=SimpleNamespace(url="https://example.com"),
                effective_brand_url="https://example.com",
                exa_data=SimpleNamespace(mentions=[]),
                hyperbrowser_data=None,
                parallel_shadow_data=None,
                social_data=None,
                social_limitation=None,
                competitor_data=None,
                raw_input_cache={},
                acquisition_steps={},
                web_collector=SimpleNamespace(),
                exa_collector=SimpleNamespace(),
            )

        def fake_select_niche_profile(*_args, **_kwargs):
            raise RuntimeError("run_input_source_capture")

        with patch("src.services.brand_service.start_analysis_run", fake_start_analysis_run), \
            patch("src.services.brand_service.collect_raw_inputs", fake_collect_raw_inputs), \
            patch("src.services.brand_service.select_niche_profile", fake_select_niche_profile):
            with self.assertRaises(RuntimeError) as exc:
                brand_service.run(
                    "https://example.com",
                    brand_name="Example",
                    use_llm=False,
                    use_social=False,
                    run_input_sources={"hyperbrowser"},
                )

        self.assertIn("run_input_source_capture", str(exc.exception))
        self.assertEqual(captured["run_input_sources"], {"hyperbrowser"})

    def test_shadow_run_persists_not_interpretable_payload_on_extraction_failure(self):
        store = _VisualSignatureStore()

        def extractor(**_kwargs):
            raise RuntimeError("fixture acquisition failure")

        result = _run_visual_signature_shadow(
            enabled=True,
            store=store,
            run_id=11,
            brand_name="Failed",
            url="https://failed.example",
            web_data=None,
            content_web=None,
            screenshot_capture=None,
            extractor=extractor,
        )

        self.assertEqual(result["status"], "acquisition_failed")
        self.assertEqual(result["interpretation_status"], "not_interpretable")
        self.assertIn("acquisition_failed", result["events"])
        self.assertEqual(len(store.saved), 1)
        payload = store.saved[0][1]
        self.assertEqual(payload["run_metadata"]["acquisition_status"], "error")
        self.assertEqual(payload["run_metadata"]["interpretation_status"], "not_interpretable")
        self.assertEqual(payload["raw_visual_signature_payload"]["acquisition"]["errors"][0], "fixture acquisition failure")

    def test_shadow_run_degrades_gracefully_when_persistence_fails(self):
        store = _VisualSignatureStore(should_fail=True)

        def extractor(**_kwargs):
            return {
                "brand_name": "Example",
                "website_url": "https://example.com",
                "interpretation_status": "interpretable",
                "acquisition": {"adapter": "existing_web_data", "status_code": 200, "warnings": [], "errors": []},
                "version": "visual-signature-mvp-1",
            }

        result = _run_visual_signature_shadow(
            enabled=True,
            store=store,
            run_id=13,
            brand_name="Example",
            url="https://example.com",
            web_data=None,
            content_web=None,
            screenshot_capture=None,
            extractor=extractor,
        )

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["persisted"])
        self.assertIn("persistence_skipped", result["events"])


class BrandServiceContentFallbackTests(unittest.TestCase):
    def _run_with_mocked_inputs(self, *, brand_name: str, url: str) -> dict:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(
                url=url,
                robots_found=True,
                sitemap_found=True,
                sitemap_url_count=3,
                coverage=0.8,
                confidence=0.8,
            )
            web = WebData(
                url=url,
                title=brand_name,
                markdown_content=f"{brand_name} builds useful brand intelligence. " * 12,
            )
            exa = ExaData(
                brand_name=brand_name,
                mentions=[
                    ExaResult(url=f"https://source{i}.com", title=f"{brand_name} mention", text=f"{brand_name} mention.")
                    for i in range(5)
                ],
            )

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context):
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web):
                        with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa):
                            return brand_service.run(
                                url,
                                brand_name,
                                use_llm=False,
                                use_social=False,
                                use_competitors=False,
                                skip_visual_analysis=True,
                                refresh=True,
                            )

    def _run_with_discovery_evidence(self, *, brand_name: str, url: str, exa: ExaData) -> dict:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(url=url, robots_found=True, sitemap_found=True, sitemap_url_count=3, coverage=0.8, confidence=0.8)
            web = WebData(url=url, title=brand_name, markdown_content=f"{brand_name} owned product evidence. " * 12)
            pages = [WebData(url=url, title=brand_name, markdown_content=f"{brand_name} discovery owned page. " * 12)]

            def fake_search(query, num_results=5, **kwargs):
                suffix = sum(ord(ch) for ch in query) % 10000
                return [ExaResult(url=f"https://extra.example.com/{suffix}", title=query)]

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context):
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web):
                        with patch("src.services.brand_service.WebCollector.scrape_multiple", return_value=pages):
                            with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa):
                                with patch("src.services.brand_service.ExaCollector.search", side_effect=fake_search):
                                    return brand_service.run(
                                        url,
                                        brand_name,
                                        use_llm=False,
                                        use_social=False,
                                        use_competitors=False,
                                        skip_visual_analysis=True,
                                        refresh=True,
                                    )

    def test_run_json_includes_entity_discovery(self):
        result = self._run_with_mocked_inputs(brand_name="Example", url="https://example.com")

        self.assertIn("entity_discovery", result)
        self.assertEqual(result["entity_discovery"]["entity_type"], "company")
        self.assertEqual(result["entity_discovery"]["analysis_scope"], "company_brand")

    def test_run_json_includes_discovery_search_plan(self):
        result = self._run_with_mocked_inputs(brand_name="ChatGPT", url="https://chatgpt.com")

        self.assertIn("discovery_search_plan", result)
        self.assertEqual(result["discovery_search_plan"]["primary_entity"], "OpenAI")
        self.assertEqual(result["discovery_search_plan"]["requested_entity"], "ChatGPT")
        self.assertEqual(result["discovery_search_plan"]["analysis_mode"], "product_with_parent")
        self.assertEqual(
            result["discovery_search_plan"]["queries"],
            [
                "OpenAI ChatGPT brand positioning",
                "OpenAI ChatGPT product updates",
                "OpenAI ChatGPT reviews",
                "OpenAI ChatGPT competitors",
            ],
        )

    def test_run_json_includes_discovery_evidence_preview(self):
        result = self._run_with_mocked_inputs(brand_name="ChatGPT", url="https://chatgpt.com")

        self.assertIn("discovery_evidence_preview", result)
        self.assertTrue(result["discovery_evidence_preview"]["attempted"])
        self.assertEqual(result["discovery_evidence_preview"]["queries_used"], result["discovery_search_plan"]["queries"])
        self.assertIn("recommended_to_use_for_scoring", result["discovery_evidence_preview"])

    def test_run_json_includes_discovery_calibration_hint(self):
        result = self._run_with_mocked_inputs(brand_name="ChatGPT", url="https://chatgpt.com")

        self.assertIn("discovery_calibration_hint", result)
        self.assertEqual(result["discovery_calibration_hint"]["recommended_profile"], "product_with_parent")
        self.assertFalse(result["discovery_calibration_hint"]["applied"])

    def test_discovery_enrichment_chatgpt_uses_openai_and_chatgpt(self):
        exa = ExaData(brand_name="ChatGPT", mentions=[
            ExaResult(url="https://openai.com/blog/chatgpt", title="OpenAI ChatGPT updates"),
            ExaResult(url="https://chatgpt.com", title="ChatGPT by OpenAI"),
            ExaResult(url="https://techcrunch.com/chatgpt", title="OpenAI ChatGPT reviews"),
            ExaResult(url="https://theverge.com/chatgpt", title="ChatGPT competitors"),
            ExaResult(url="https://wired.com/chatgpt", title="OpenAI ChatGPT positioning"),
        ])
        result = self._run_with_discovery_evidence(brand_name="ChatGPT", url="https://chatgpt.com", exa=exa)

        self.assertTrue(result["discovery_enrichment"]["applied"])
        self.assertEqual(result["discovery_enrichment"]["urls_used"], ["https://openai.com", "https://chatgpt.com"])
        self.assertEqual(result["discovery_enrichment"]["queries_used"][0], "OpenAI ChatGPT brand positioning")
        self.assertGreater(result["discovery_enrichment"]["added_third_party_evidence"], 0)
        self.assertEqual(result["discovery_trust_basis"]["basis"], "product_with_parent_enriched")
        self.assertEqual(result["discovery_trust_basis"]["primary_entity"], "OpenAI")
        self.assertIn("not only https://chatgpt.com", result["discovery_trust_basis"]["user_message"])
        self.assertEqual(result["trust_summary"]["evidence_basis_summary"], result["discovery_trust_basis"]["user_message"])
        self.assertTrue(result["discovery_calibration_decision"]["applied"])
        self.assertEqual(result["calibration_profile"], "product_with_parent")
        self.assertEqual(result["profile_source"], "discovery")
        self.assertEqual(result["audit"]["calibration_profile_config"]["label"], "Product with Parent")

    def test_discovery_enrichment_claude_uses_anthropic_and_claude(self):
        exa = ExaData(brand_name="Claude", mentions=[
            ExaResult(url="https://anthropic.com/news/claude", title="Anthropic Claude updates"),
            ExaResult(url="https://claude.ai", title="Claude by Anthropic"),
            ExaResult(url="https://techcrunch.com/claude", title="Anthropic Claude reviews"),
            ExaResult(url="https://theverge.com/claude", title="Claude competitors"),
            ExaResult(url="https://wired.com/claude", title="Anthropic Claude positioning"),
        ])
        result = self._run_with_discovery_evidence(brand_name="Claude", url="https://claude.ai", exa=exa)

        self.assertTrue(result["discovery_enrichment"]["applied"])
        self.assertEqual(result["discovery_search_plan"]["primary_entity"], "Anthropic")
        self.assertEqual(result["discovery_enrichment"]["queries_used"][0], "Anthropic Claude brand positioning")
        self.assertEqual(result["discovery_trust_basis"]["basis"], "product_with_parent_enriched")
        self.assertEqual(result["discovery_trust_basis"]["primary_entity"], "Anthropic")
        self.assertIn("not only https://claude.ai", result["discovery_trust_basis"]["user_message"])
        self.assertTrue(result["discovery_calibration_decision"]["applied"])
        self.assertEqual(result["calibration_profile"], "product_with_parent")
        self.assertEqual(result["profile_source"], "discovery")
        self.assertIn("previous_calibration_profile", result["discovery_calibration_decision"])
        self.assertEqual(result["audit"]["calibration_profile_config"]["label"], "Product with Parent")

    def test_discovery_enrichment_base_uses_ecosystem_protocol_queries(self):
        exa = ExaData(brand_name="Base", mentions=[
            ExaResult(url="https://base.org", title="Base protocol updates"),
            ExaResult(url="https://cointelegraph.com/base", title="Base ecosystem positioning"),
            ExaResult(url="https://decrypt.co/base", title="Base competitors alternatives"),
            ExaResult(url="https://blockworks.co/base", title="Base developer community"),
            ExaResult(url="https://example.com/base", title="Base protocol community"),
        ])
        result = self._run_with_discovery_evidence(brand_name="Base", url="https://base.org", exa=exa)

        self.assertTrue(result["discovery_enrichment"]["applied"])
        self.assertEqual(result["discovery_search_plan"]["analysis_mode"], "ecosystem_or_protocol")
        self.assertEqual(result["discovery_enrichment"]["queries_used"][0], "Base ecosystem positioning")
        self.assertEqual(result["discovery_trust_basis"]["basis"], "ecosystem_or_protocol_enriched")
        self.assertTrue(result["discovery_calibration_decision"]["applied"])
        self.assertEqual(result["calibration_profile"], "ecosystem_or_protocol")
        self.assertEqual(result["profile_source"], "discovery")
        self.assertEqual(result["audit"]["calibration_profile_config"]["label"], "Ecosystem / Protocol")

    def test_discovery_enrichment_not_applied_when_preview_insufficient(self):
        result = self._run_with_mocked_inputs(brand_name="Unknown", url="https://example.com")

        self.assertFalse(result["discovery_enrichment"]["applied"])
        self.assertEqual(result["discovery_enrichment"]["urls_used"], [])
        self.assertEqual(result["discovery_enrichment"]["queries_used"], [])
        self.assertEqual(result["discovery_trust_basis"]["basis"], "url_only")
        self.assertFalse(result["discovery_trust_basis"]["uses_enriched_evidence"])

    def test_discovery_trust_basis_openai_company_brand(self):
        result = self._run_with_mocked_inputs(brand_name="OpenAI", url="https://openai.com")

        self.assertIn(result["discovery_trust_basis"]["basis"], {"company_brand", "company_brand_enriched"})
        self.assertEqual(result["discovery_trust_basis"]["primary_entity"], "OpenAI")

    def test_discovery_calibration_gate_applies_openai_frontier_ai(self):
        exa = ExaData(brand_name="OpenAI", mentions=[
            ExaResult(url="https://openai.com/news", title="OpenAI frontier AI updates"),
            ExaResult(url="https://techcrunch.com/openai", title="OpenAI frontier AI reviews"),
            ExaResult(url="https://theverge.com/openai", title="OpenAI competitors"),
            ExaResult(url="https://wired.com/openai", title="OpenAI brand positioning"),
            ExaResult(url="https://example.com/openai", title="OpenAI latest product updates"),
        ])
        niche = {"predicted_niche": "frontier_ai", "confidence": 0.91, "alternatives": [], "evidence": ["test"]}
        with patch("src.services.brand_service.classify_brand_niche", return_value=niche):
            result = self._run_with_discovery_evidence(brand_name="OpenAI", url="https://openai.com", exa=exa)

        self.assertEqual(result["discovery_calibration_decision"]["calibration_profile"], "frontier_ai")
        self.assertEqual(result["discovery_calibration_decision"]["profile_source"], "discovery")
        self.assertTrue(result["discovery_calibration_decision"]["applied"])
        self.assertEqual(result["discovery_calibration_decision"]["reason"], "discovery_calibration_gate_passed")
        self.assertIn("previous_calibration_profile", result["discovery_calibration_decision"])
        self.assertEqual(result["profile_source"], "discovery")
        self.assertEqual(result["audit"]["discovery_calibration_decision"], result["discovery_calibration_decision"])

    def test_format_discovery_summary_product_with_parent(self):
        lines = format_discovery_summary({
            "entity_discovery": {"entity_name": "ChatGPT"},
            "discovery_search_plan": {"primary_entity": "OpenAI", "analysis_mode": "product_with_parent"},
            "discovery_evidence_preview": {
                "recommended_to_use_for_scoring": True,
                "owned_results_count": 5,
                "third_party_results_count": 7,
                "top_domains": ["openai.com", "help.openai.com", "techcrunch.com"],
            },
            "discovery_trust_basis": {
                "basis": "product_with_parent_enriched",
                "user_message": "Audit basis covers ChatGPT as a product of OpenAI; it is not only https://chatgpt.com.",
            },
        })

        self.assertEqual(lines[0], "--- Discovery ---")
        self.assertIn("Entity: ChatGPT", lines)
        self.assertIn("Primary entity: OpenAI", lines)
        self.assertIn("Mode: product_with_parent", lines)
        self.assertIn("Search scope: product + parent company", lines)
        self.assertIn("Evidence preview: recommended", lines)
        self.assertIn("Owned evidence: 5", lines)
        self.assertIn("Third-party evidence: 7", lines)
        self.assertIn("Top domains: openai.com, help.openai.com, techcrunch.com", lines)
        self.assertIn("Evidence basis: product_with_parent_enriched", lines)

    def test_format_discovery_summary_company_brand(self):
        lines = format_discovery_summary({
            "entity_discovery": {"entity_name": "OpenAI"},
            "discovery_search_plan": {"primary_entity": "OpenAI", "analysis_mode": "company_brand"},
            "discovery_evidence_preview": {
                "recommended_to_use_for_scoring": True,
                "owned_results_count": 2,
                "third_party_results_count": 4,
            },
        })

        self.assertIn("Entity: OpenAI", lines)
        self.assertIn("Primary entity: OpenAI", lines)
        self.assertIn("Mode: company_brand", lines)
        self.assertIn("Search scope: company brand", lines)
        self.assertIn("Evidence preview: recommended", lines)

    def test_format_discovery_summary_insufficient_limitations(self):
        lines = format_discovery_summary({
            "entity_discovery": {"entity_name": "Obscure Thing"},
            "discovery_search_plan": {"primary_entity": "Obscure Thing", "analysis_mode": "url_only"},
            "discovery_evidence_preview": {
                "recommended_to_use_for_scoring": False,
                "owned_results_count": 0,
                "third_party_results_count": 1,
                "limitations": ["insufficient_results", "owned_evidence_missing"],
            },
        })

        self.assertIn("Evidence preview: insufficient", lines)
        self.assertIn("Limitations: insufficient_results, owned_evidence_missing", lines)

    def test_run_entity_discovery_chatgpt_parent(self):
        result = self._run_with_mocked_inputs(brand_name="ChatGPT", url="https://chatgpt.com")

        self.assertEqual(result["entity_discovery"]["parent_brand_name"], "OpenAI")
        self.assertEqual(result["entity_discovery"]["analysis_scope"], "product_with_parent")

    def test_run_entity_discovery_claude_parent(self):
        result = self._run_with_mocked_inputs(brand_name="Claude", url="https://claude.ai")

        self.assertEqual(result["entity_discovery"]["parent_brand_name"], "Anthropic")
        self.assertEqual(result["entity_discovery"]["analysis_scope"], "product_with_parent")

    def test_run_entity_discovery_openai_company(self):
        result = self._run_with_mocked_inputs(brand_name="OpenAI", url="https://openai.com")

        self.assertEqual(result["entity_discovery"]["entity_type"], "company")
        self.assertEqual(result["entity_discovery"]["analysis_scope"], "company_brand")

    def test_entity_discovery_does_not_change_scoring_outputs(self):
        result = self._run_with_mocked_inputs(brand_name="ChatGPT", url="https://chatgpt.com")
        result_without_discovery = dict(result)
        result_without_discovery.pop("entity_discovery")
        result_without_discovery.pop("discovery_search_plan")
        result_without_discovery.pop("discovery_evidence_preview")
        result_without_discovery.pop("discovery_enrichment")
        result_without_discovery.pop("discovery_trust_basis")
        result_without_discovery.pop("discovery_calibration_hint")
        result_without_discovery.pop("discovery_calibration_decision")

        self.assertIsNotNone(result["composite_score"])
        self.assertEqual(
            set(result["dimensions"]),
            {"coherencia", "presencia", "percepcion", "diferenciacion", "vitalidad"},
        )
        self.assertEqual(result["composite_score"], result_without_discovery["composite_score"])
        self.assertEqual(result["dimensions"], result_without_discovery["dimensions"])

    def test_discovery_calibration_hint_does_not_change_selected_profile(self):
        result = self._run_with_mocked_inputs(brand_name="ChatGPT", url="https://chatgpt.com")

        self.assertEqual(result["discovery_calibration_hint"]["recommended_profile"], "product_with_parent")
        self.assertNotEqual(result["calibration_profile"], "product_with_parent")
        self.assertEqual(result["calibration_profile"], "base")
        self.assertEqual(result["profile_source"], "fallback")

    def test_entity_discovery_failure_does_not_fail_run(self):
        with patch("src.services.brand_service.discover_entity", side_effect=RuntimeError("boom")):
            result = self._run_with_mocked_inputs(brand_name="Example", url="https://example.com")

        self.assertEqual(result["entity_discovery"]["entity_type"], "unknown")
        self.assertEqual(result["entity_discovery"]["analysis_scope"], "url_only")
        self.assertEqual(result["entity_discovery"]["confidence"], 0.0)
        self.assertEqual(result["entity_discovery"]["warnings"], ["entity_discovery_failed"])

    def test_owned_fallback_recovers_same_domain_pages_before_exa_fallback(self):
        initial = WebData(url="https://example.com", title="Example", markdown_content="", error="blocked")
        about = WebData(
            url="https://example.com/about",
            title="About Example",
            markdown_content="About Example builds reliable brand intelligence. " * 8,
        )
        pricing = WebData(
            url="https://example.com/pricing",
            title="Pricing",
            markdown_content="Pricing information for teams and enterprise customers. " * 8,
        )
        exa = ExaData(
            brand_name="Example",
            mentions=[
                ExaResult(url=f"https://external{i}.com", title=f"Mention {i}", text="External mention. " * 30)
                for i in range(4)
            ],
        )

        class FakeCollector:
            def __init__(self):
                self.urls = []

            def scrape_multiple(self, urls):
                self.urls = urls
                return [about, WebData(url="https://example.com/docs", error="404"), pricing]

        collector = FakeCollector()
        context = ContextData(
            url="https://example.com",
            key_pages={"about": True, "products": True},
        )
        recovered = _recover_owned_web_content("https://example.com", initial, collector, context)
        content_web, content_source, data_sources = _build_content_web(
            "https://example.com",
            "Example",
            recovered or initial,
            exa,
        )

        self.assertEqual(
            collector.urls,
            [
                "https://example.com/about",
                "https://example.com/products",
                "https://example.com/product",
                "https://example.com/collections",
                "https://example.com/shop",
                "https://example.com/solutions",
                "https://example.com/pricing",
                "https://example.com/docs",
                "https://example.com/blog",
                "https://example.com/news",
                "https://example.com/reviews",
                "https://example.com/testimonials",
                "https://example.com/customers",
                "https://example.com/case-studies",
                "https://example.com/help",
                "https://example.com/support",
                "https://example.com/trust",
                "https://example.com/security",
            ],
        )
        self.assertIsNotNone(content_web)
        self.assertEqual(content_source, "owned_fallback")
        self.assertEqual(data_sources["web_scrape"], "owned_fallback")
        self.assertEqual(data_sources["content_source"], "owned_fallback")
        self.assertEqual(data_sources["exa_fallback_mentions_used"], 0)
        self.assertEqual(
            data_sources["owned_fallback_urls"],
            ["https://example.com/about", "https://example.com/pricing"],
        )
        self.assertIn("About Example builds reliable brand intelligence", content_web.markdown_content)
        self.assertIn("Pricing information for teams", content_web.markdown_content)

    def test_owned_fallback_failure_keeps_exa_fallback_behavior(self):
        initial = WebData(url="https://uber.com", title="", markdown_content="", error="")
        exa = ExaData(
            brand_name="Uber",
            mentions=[
                ExaResult(url=f"https://example{i}.com", title=f"Mention {i}", text="Uber is a mobility platform. " * 30)
                for i in range(4)
            ],
        )

        class FakeCollector:
            def scrape_multiple(self, urls):
                return [WebData(url=url, markdown_content="", error="404") for url in urls]

        recovered = _recover_owned_web_content("https://uber.com", initial, FakeCollector())
        content_web, content_source, data_sources = _build_content_web(
            "https://uber.com",
            "Uber",
            recovered or initial,
            exa,
        )

        self.assertIsNone(recovered)
        self.assertIsNotNone(content_web)
        self.assertEqual(content_source, "exa_fallback")
        self.assertEqual(data_sources["content_source"], "exa_fallback")
        self.assertEqual(data_sources["exa_fallback_mentions_used"], 4)

    def test_owned_fallback_is_not_attempted_when_initial_web_data_has_strategic_coverage(self):
        initial = WebData(
            url="https://example.com",
            title="Example",
            markdown_content="Strategic homepage content with product, audience, proof, and company context. " * 40,
        )

        class FakeCollector:
            def scrape_multiple(self, urls):
                raise AssertionError("fallback URLs should not be scraped")

        recovered = _recover_owned_web_content("https://example.com", initial, FakeCollector())

        self.assertIsNone(recovered)

    def test_owned_fallback_enriches_short_but_usable_homepage(self):
        initial = WebData(
            url="https://example.com",
            title="Example",
            markdown_content="Short homepage with a real headline. " * 10,
        )
        product = WebData(
            url="https://example.com/products",
            title="Products",
            markdown_content="Product page explains the offer, audience, and outcome. " * 12,
        )
        about = WebData(
            url="https://example.com/about",
            title="About",
            markdown_content="About page explains the company mission and proof. " * 12,
        )

        class FakeCollector:
            def __init__(self):
                self.urls = []

            def scrape_multiple(self, urls):
                self.urls = urls
                return [about, product]

        collector = FakeCollector()
        context = ContextData(
            url="https://example.com",
            key_pages={"about": True, "products": True},
        )
        recovered = _recover_owned_web_content("https://example.com", initial, collector, context)

        self.assertIsNotNone(recovered)
        self.assertIn("https://example.com/products", collector.urls)
        self.assertIn("Short homepage with a real headline", recovered.markdown_content)
        self.assertIn("Product page explains the offer", recovered.markdown_content)
        self.assertIn("About page explains the company mission", recovered.markdown_content)
        self.assertEqual(
            recovered.owned_fallback_urls,
            ["https://example.com/about", "https://example.com/products"],
        )
        self.assertEqual(recovered.content_source, "owned_fallback")

    def test_run_persists_effective_owned_fallback_web_input(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(
                url="https://example.com",
                robots_found=True,
                sitemap_found=True,
                key_pages={"about": True, "products": True},
                coverage=1.0,
                confidence=1.0,
            )
            homepage = WebData(
                url="https://example.com",
                title="Example",
                markdown_content="Short homepage with a real headline. " * 10,
            )
            about = WebData(
                url="https://example.com/about",
                title="About",
                markdown_content="About page explains the company mission and proof. " * 12,
            )
            product = WebData(
                url="https://example.com/products",
                title="Products",
                markdown_content="Product page explains the offer, audience, and outcome. " * 12,
            )
            exa = ExaData(brand_name="Example")

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context):
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=homepage):
                        with patch("src.services.brand_service.WebCollector.scrape_multiple", return_value=[about, product]):
                            with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa):
                                result = brand_service.run(
                                    "https://example.com",
                                    "Example",
                                    use_llm=False,
                                    use_social=False,
                                    use_competitors=False,
                                    skip_visual_analysis=True,
                                    refresh=True,
                                )

            store = SQLiteStore(str(db_path))
            try:
                snapshot = store.get_run_snapshot(result["run_id"])
            finally:
                store.close()

        web_inputs = [item["payload"] for item in snapshot["raw_inputs"] if item["source"] == "web"]
        self.assertEqual(web_inputs[-1]["content_source"], "owned_fallback")
        self.assertEqual(
            web_inputs[-1]["owned_fallback_urls"],
            ["https://example.com/about", "https://example.com/products"],
        )
        self.assertIn("Short homepage with a real headline", web_inputs[-1]["markdown_content"])
        self.assertIn("Product page explains the offer", web_inputs[-1]["markdown_content"])

    def test_owned_fallback_uses_same_scheme_and_host_only(self):
        initial = WebData(url="https://www.example.com/start", markdown_content="", error="blocked")

        class FakeCollector:
            def __init__(self):
                self.urls = []

            def scrape_multiple(self, urls):
                self.urls = urls
                return []

        collector = FakeCollector()
        recovered = _recover_owned_web_content("https://www.example.com/start?x=1", initial, collector)

        self.assertIsNone(recovered)
        self.assertTrue(all(url.startswith("https://www.example.com/") for url in collector.urls))
        self.assertNotIn("https://example.com/about", collector.urls)

    def test_owned_fallback_data_quality_follows_owned_content_path(self):
        exa = ExaData(
            brand_name="Example",
            mentions=[ExaResult(url=f"https://e{i}.com", title="x") for i in range(5)],
        )

        self.assertEqual(_compute_data_quality(exa, "owned_fallback"), "good")

    def test_build_content_web_reports_browser_fallback_as_owned_content_source(self):
        web = WebData(
            url="https://claude.ai",
            title="Claude",
            markdown_content="Claude browser-rendered product and pricing content. " * 8,
            content_source="browser_fallback",
            browser_status=200,
        )
        exa = ExaData(
            brand_name="Claude",
            mentions=[ExaResult(url=f"https://e{i}.com", title="x") for i in range(5)],
        )

        content_web, content_source, data_sources = _build_content_web(
            "https://claude.ai",
            "Claude",
            web,
            exa,
        )

        self.assertIs(content_web, web)
        self.assertEqual(content_source, "browser_fallback")
        self.assertEqual(data_sources["web_scrape"], "browser_fallback")
        self.assertEqual(data_sources["content_source"], "browser_fallback")
        self.assertEqual(data_sources["exa_fallback_mentions_used"], 0)
        self.assertEqual(_compute_data_quality(exa, content_source), "good")

    def test_low_context_does_not_skip_llm_when_browser_fallback_has_usable_owned_content(self):
        context = ContextData(url="https://claude.ai", coverage=0.0)
        content_web = WebData(
            url="https://claude.ai",
            markdown_content="Claude browser-rendered product content. " * 8,
            content_source="browser_fallback",
        )

        self.assertFalse(
            _should_skip_llm_for_low_context(context, content_web, "browser_fallback")
        )

    def test_low_context_still_skips_llm_when_only_exa_fallback_exists(self):
        context = ContextData(url="https://claude.ai", coverage=0.0)
        content_web = WebData(
            url="https://claude.ai",
            markdown_content="External Exa aggregate content. " * 12,
        )

        self.assertTrue(
            _should_skip_llm_for_low_context(context, content_web, "exa_fallback")
        )

    def test_good_context_keeps_llm_allowed_without_owned_content(self):
        context = ContextData(url="https://example.com", coverage=0.8)

        self.assertFalse(
            _should_skip_llm_for_low_context(context, None, "none")
        )

    def test_public_presence_inventory_summarizes_read_only_official_pages(self):
        web = WebData(
            url="https://claude.ai",
            title="Claude",
            markdown_content="Claude public page content. " * 90,
            content_source="browser_fallback",
            links=[
                "https://docs.anthropic.com/en/docs/claude-code",
                "https://example-news.com/claude-review",
            ],
        )
        exa = ExaData(
            brand_name="Claude",
            mentions=[
                ExaResult(
                    url="https://www.anthropic.com/news/claude",
                    title="Anthropic news about Claude",
                    text="Claude announcement",
                ),
                ExaResult(
                    url="https://example-news.com/claude-review",
                    title="Review of Claude",
                    text="Third-party coverage",
                ),
            ],
        )
        context = ContextData(
            url="https://claude.ai",
            key_pages={"pricing": True, "docs": False},
            coverage=0.0,
            confidence=0.0,
            error="homepage_unavailable",
        )

        summary = _public_presence_inventory_summary(
            brand_name="Claude",
            url="https://claude.ai",
            web_data=web,
            content_web=web,
            content_source="browser_fallback",
            exa_data=exa,
            context_data=context,
        )

        self.assertEqual(summary["mode"], "read_only_public_pages")
        self.assertGreaterEqual(summary["official_pages_found"], 3)
        self.assertEqual(summary["primary_page"]["collection_method"], "browser_fallback")
        self.assertGreaterEqual(summary["primary_page"]["text_chars"], 1500)
        self.assertTrue(summary["primary_page"]["usable_for_brand_evidence"])
        self.assertGreaterEqual(summary["news_or_blog_candidates"], 1)
        self.assertEqual(summary["third_party_candidates"], 1)
        self.assertTrue(summary["recommended_evidence_base"])

    def test_public_presence_inventory_does_not_treat_exa_snippets_as_owned_evidence(self):
        web = WebData(url="https://claude.ai", title="Claude", markdown_content="", error="blocked")
        exa = ExaData(
            brand_name="Claude",
            mentions=[
                ExaResult(
                    url="https://www.anthropic.com/claude",
                    title="Claude by Anthropic",
                    text="Official metadata only",
                )
            ],
        )

        summary = _public_presence_inventory_summary(
            brand_name="Claude",
            url="https://claude.ai",
            web_data=web,
            content_web=None,
            content_source="none",
            exa_data=exa,
            context_data=ContextData(url="https://claude.ai", error="homepage_unavailable"),
        )

        self.assertEqual(summary["official_pages_found"], 2)
        self.assertEqual(summary["usable_brand_evidence_pages"], 0)
        self.assertFalse(summary["recommended_evidence_base"])

    def test_context_enrichment_applies_for_limited_context_with_public_inventory_base(self):
        inventory = {
            "recommended_evidence_base": True,
            "official_pages_found": 4,
            "usable_brand_evidence_pages": 2,
            "usable_public_perception_pages": 1,
            "docs_candidates": 1,
            "support_candidates": 1,
            "news_or_blog_candidates": 1,
            "trust_or_safety_candidates": 1,
        }
        context = {"status": "insufficient_data", "coverage": 0.0}

        enrichment = _context_enrichment_summary(
            public_presence_inventory=inventory,
            context_summary=context,
        )

        self.assertTrue(enrichment["applied"])
        self.assertEqual(enrichment["source"], "public_presence_inventory")
        self.assertEqual(enrichment["reason"], "official_public_pages_available")
        self.assertEqual(enrichment["official_pages_found"], 4)
        self.assertTrue(enrichment["recommended_evidence_base"])
        self.assertIn("homepage_pre_scan_unavailable", enrichment["limitations"])
        self.assertIn("raw_context_readiness_unchanged", enrichment["limitations"])

    def test_context_enrichment_does_not_apply_without_recommended_inventory_base(self):
        enrichment = _context_enrichment_summary(
            public_presence_inventory={"recommended_evidence_base": False, "official_pages_found": 4},
            context_summary={"status": "insufficient_data", "coverage": 0.0},
        )
        missing = _context_enrichment_summary(
            public_presence_inventory=None,
            context_summary={"status": "insufficient_data", "coverage": 0.0},
        )

        self.assertFalse(enrichment["applied"])
        self.assertEqual(enrichment["reason"], "not_applicable")
        self.assertFalse(missing["applied"])

    def test_context_effective_readiness_compensates_with_usable_public_inventory(self):
        effective = _context_effective_readiness(
            public_presence_inventory={
                "recommended_evidence_base": True,
                "official_pages_found": 3,
                "usable_brand_evidence_pages": 2,
                "usable_public_perception_pages": 1,
            },
            context_summary={"status": "insufficient_data", "coverage": 0.0},
        )

        self.assertTrue(effective["applied"])
        self.assertEqual(effective["status"], "degraded")
        self.assertGreater(effective["coverage"], 0.0)
        self.assertEqual(effective["reason"], "homepage_unavailable_but_public_inventory_available")
        self.assertIn("homepage_pre_scan_unavailable", effective["limitations"])
        self.assertIn("raw_context_readiness_unchanged", effective["limitations"])

    def test_context_effective_readiness_does_not_apply_without_usable_recommended_inventory(self):
        not_recommended = _context_effective_readiness(
            public_presence_inventory={"recommended_evidence_base": False, "usable_brand_evidence_pages": 2},
            context_summary={"status": "insufficient_data", "coverage": 0.0},
        )
        no_usable_owned = _context_effective_readiness(
            public_presence_inventory={"recommended_evidence_base": True, "usable_brand_evidence_pages": 0},
            context_summary={"status": "insufficient_data", "coverage": 0.0},
        )
        missing = _context_effective_readiness(
            public_presence_inventory=None,
            context_summary={"status": "insufficient_data", "coverage": 0.0},
        )

        self.assertFalse(not_recommended["applied"])
        self.assertFalse(no_usable_owned["applied"])
        self.assertFalse(missing["applied"])

    def test_trust_summary_uses_effective_context_without_changing_raw_context_status(self):
        context_summary = {
            "status": "insufficient_data",
            "coverage": 0.0,
            "confidence_reason": ["homepage_unavailable", "low_coverage"],
        }
        enrichment = _context_enrichment_summary(
            public_presence_inventory={
                "recommended_evidence_base": True,
                "official_pages_found": 3,
                "usable_brand_evidence_pages": 2,
            },
            context_summary=context_summary,
        )
        effective = _context_effective_readiness(
            public_presence_inventory={
                "recommended_evidence_base": True,
                "official_pages_found": 3,
                "usable_brand_evidence_pages": 2,
            },
            context_summary=context_summary,
        )

        summary = _trust_summary_payload(
            data_quality="good",
            context_summary=context_summary,
            evidence_summary={"total": 4},
            dimension_confidence={"presencia": {"status": "good"}},
            context_enrichment_summary=enrichment,
            context_effective_readiness=effective,
        )

        self.assertEqual(summary["context"]["status"], "insufficient_data")
        self.assertEqual(summary["overall_status"], "degraded")
        self.assertEqual(summary["effective_context"]["status"], "degraded")
        self.assertTrue(summary["interpretation"]["audit_usable"])
        self.assertEqual(summary["interpretation"]["primary_limitation"], "homepage_pre_scan_unavailable")
        self.assertEqual(summary["interpretation"]["evidence_base"], "sufficient_with_context_limitation")
        self.assertIn("pre-scan técnico de contexto", summary["user_facing_summary"])
        self.assertIn("Todas las dimensiones cuentan con alguna evidencia.", summary["user_facing_summary"])
        for alarming_word in ("unreliable", "invalid", "failed audit"):
            self.assertNotIn(alarming_word, summary["user_facing_summary"].lower())
        self.assertEqual(
            summary["context_enrichment"]["status"],
            "raw_context_limited_but_public_inventory_available",
        )
        self.assertEqual(summary["context_enrichment"]["official_pages_found"], 3)
        self.assertTrue(summary["context_enrichment"]["recommended_evidence_base"])

    def test_trust_summary_stays_insufficient_without_effective_context(self):
        summary = _trust_summary_payload(
            data_quality="good",
            context_summary={"status": "insufficient_data", "coverage": 0.0},
            evidence_summary={"total": 4},
            dimension_confidence={"presencia": {"status": "good"}},
            context_effective_readiness={"applied": False, "reason": "not_applicable"},
        )

        self.assertEqual(summary["overall_status"], "insufficient_data")
        self.assertNotIn("effective_context", summary)
        self.assertNotIn("interpretation", summary)

    def test_build_content_web_prefers_usable_firecrawl_content(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content="A" * 250,
        )
        exa = ExaData(brand_name="Example")

        content_web, content_source, data_sources = _build_content_web(
            "https://example.com",
            "Example",
            web,
            exa,
        )

        self.assertIs(content_web, web)
        self.assertEqual(content_source, "firecrawl")
        self.assertEqual(data_sources["content_source"], "firecrawl")

    def test_aggregate_exa_content_requires_enough_mentions_and_text(self):
        exa = ExaData(
            brand_name="Example",
            mentions=[
                ExaResult(url="https://a.com", title="One", text="short"),
                ExaResult(url="https://b.com", title="Two", text="short"),
            ],
        )

        aggregate, used = _aggregate_exa_content(exa)

        self.assertEqual(aggregate, "")
        self.assertEqual(used, 0)

    def test_build_content_web_falls_back_to_exa_mentions(self):
        exa = ExaData(
            brand_name="Uber",
            mentions=[
                ExaResult(url=f"https://example{i}.com", title=f"Mention {i}", text="Uber is a mobility platform. " * 30)
                for i in range(4)
            ],
        )
        web = WebData(url="https://uber.com", title="", markdown_content="", error="")

        content_web, content_source, data_sources = _build_content_web(
            "https://uber.com",
            "Uber",
            web,
            exa,
        )

        self.assertIsNotNone(content_web)
        self.assertEqual(content_source, "exa_fallback")
        self.assertEqual(data_sources["content_source"], "exa_fallback")
        self.assertEqual(data_sources["exa_fallback_mentions_used"], 4)
        self.assertGreaterEqual(len(content_web.markdown_content), 300)
        self.assertEqual(content_web.title, "Mention 0")

    def test_build_content_web_returns_none_when_no_usable_sources_exist(self):
        web = WebData(url="https://example.com", markdown_content="", title="")
        exa = ExaData(brand_name="Example", mentions=[])

        content_web, content_source, data_sources = _build_content_web(
            "https://example.com",
            "Example",
            web,
            exa,
        )

        self.assertIsNone(content_web)
        self.assertEqual(content_source, "none")
        self.assertEqual(data_sources["content_source"], "none")

    def test_compute_data_quality_distinguishes_good_degraded_and_insufficient(self):
        rich_exa = ExaData(brand_name="Example", mentions=[ExaResult(url=f"https://e{i}.com", title="x") for i in range(5)])
        degraded_exa = ExaData(brand_name="Example", mentions=[ExaResult(url=f"https://e{i}.com", title="x") for i in range(3)])
        empty_exa = ExaData(brand_name="Example", mentions=[])

        self.assertEqual(_compute_data_quality(rich_exa, "firecrawl"), "good")
        self.assertEqual(_compute_data_quality(degraded_exa, "exa_fallback"), "degraded")
        self.assertEqual(_compute_data_quality(empty_exa, "none"), "insufficient")

    def test_context_confidence_summary_marks_low_coverage_insufficient(self):
        context = ContextData(
            url="https://example.com",
            coverage=0.2,
            confidence=0.5,
            confidence_reason=["low_coverage"],
        )

        summary = _context_confidence_summary(context)

        self.assertEqual(summary["status"], "insufficient_data")
        self.assertEqual(summary["coverage"], 0.2)
        self.assertIn("low_coverage", summary["confidence_reason"])

    def test_context_evidence_items_are_generated_from_context_signals(self):
        context = ContextData(
            url="https://example.com",
            robots_found=True,
            sitemap_found=True,
            sitemap_url_count=12,
            llms_txt_found=True,
            schema_types=["Organization", "WebSite"],
            key_pages={"about": True, "blog": True},
            coverage=0.8,
            confidence=0.85,
        )

        items = _context_evidence_items(context)

        self.assertGreaterEqual(len(items), 4)
        self.assertIn("sitemap.xml found with 12 URLs", [item["quote"] for item in items])
        self.assertIn("coherencia", {item["dimension_name"] for item in items})

    def test_llm_cache_summary_reports_hits_and_skip_reason(self):
        class FakeLLM:
            cache_hits = 2
            cache_misses = 1
            cache_writes = 1

        summary = _llm_cache_summary(FakeLLM())
        skipped = _llm_cache_summary(None, "insufficient_context_coverage")

        self.assertEqual(summary["cache_hits"], 2)
        self.assertEqual(summary["estimated_cost_saved_units"], 2)
        self.assertEqual(skipped["skipped_reason"], "insufficient_context_coverage")

    def test_infer_llm_provider_from_base_url(self):
        self.assertEqual(
            _infer_llm_provider("https://generativelanguage.googleapis.com/v1beta/openai"),
            "Google AI Studio / Gemini",
        )
        self.assertEqual(_infer_llm_provider("https://openrouter.ai/api/v1"), "OpenRouter")
        self.assertEqual(_infer_llm_provider("https://inference.nousresearch.com/v1"), "Nous")
        self.assertEqual(_infer_llm_provider("https://llm.example.com/v1"), "OpenAI-compatible")

    def test_llm_provider_payload_exposes_provider_without_api_key(self):
        class FakeLLM:
            api_key = "secret-key"
            model = "gemini-3.1-pro-preview"
            base_url = "https://generativelanguage.googleapis.com/v1beta/openai"

        payload = _llm_provider_payload(FakeLLM())

        self.assertEqual(payload["provider"], "Google AI Studio / Gemini")
        self.assertEqual(payload["model"], "gemini-3.1-pro-preview")
        self.assertEqual(payload["base_url"], "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertTrue(payload["openai_compatible"])
        self.assertNotIn("api_key", payload)

    def test_llm_model_role_defaults_are_documented(self):
        self.assertEqual(DEFAULT_LLM_MODEL, "gemini-3.1-pro-preview")
        self.assertEqual(DEFAULT_LLM_CHEAP_MODEL, "gemini-3.1-flash-lite")
        self.assertEqual(DEFAULT_LLM_PREMIUM_MODEL, "gemini-3.1-pro-preview")
        self.assertEqual(DEFAULT_VISION_MODEL, "gemini-2.5-flash")

    def test_llm_model_roles_payload_exposes_models_without_secrets(self):
        roles = _llm_model_roles_payload()

        self.assertEqual(set(roles), {"default", "cheap", "premium", "audit_analyst", "vision"})
        self.assertTrue(all(isinstance(value, str) and value for value in roles.values()))
        self.assertNotIn("api_key", roles)
        self.assertNotIn("key", roles)

    def test_audit_analyst_llm_can_route_to_separate_model(self):
        class FeatureLLM:
            api_key = "key"
            model = "feature-tier"

        created_models = []

        class FakeAnalyzer:
            api_key = "key"

            def __init__(self, *, model=None):
                self.model = model
                created_models.append(model)

        with patch.object(brand_service, "AUDIT_ANALYST_MODEL", "analyst-tier"):
            with patch("src.services.brand_service.LLMAnalyzer", FakeAnalyzer):
                analyst_llm = _audit_analyst_llm(FeatureLLM())

        self.assertEqual(created_models, ["analyst-tier"])
        self.assertEqual(analyst_llm.model, "analyst-tier")

    def test_audit_analyst_llm_reuses_feature_llm_when_model_matches(self):
        class FeatureLLM:
            api_key = "key"
            model = AUDIT_ANALYST_MODEL

        feature_llm = FeatureLLM()

        self.assertIs(_audit_analyst_llm(feature_llm), feature_llm)

    def test_screenshot_capture_diagnostic_reports_success_error_and_skip(self):
        captured = _screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data={"screenshot_url": "https://cdn.example/screenshot.png"},
        )
        playwright_captured = _screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data={
                "screenshot_url": "file:///tmp/brand3-shot.png",
                "screenshot_path": "/tmp/brand3-shot.png",
                "screenshot_provider": "playwright",
                "metadata": {"title": "Brand", "cookie_banner_dismissal": {"success": True}},
            },
        )
        payment_error = _screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data={"error": "Screenshot failed: Payment Required: Insufficient credits"},
        )
        playwright_error = _screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data={
                "error": "browser launch failed",
                "error_type": "browser_error",
                "screenshot_provider": "playwright",
            },
        )
        skipped = _screenshot_capture_diagnostic(
            attempted=False,
            skipped_reason="benchmark_mode",
        )

        self.assertEqual(captured["status"], "captured")
        self.assertTrue(captured["success"])
        self.assertEqual(playwright_captured["source"], "playwright")
        self.assertEqual(playwright_captured["screenshot_path"], "/tmp/brand3-shot.png")
        self.assertEqual(playwright_captured["metadata"]["title"], "Brand")
        self.assertIsNone(playwright_captured["error_type"])
        self.assertEqual(payment_error["status"], "error")
        self.assertEqual(payment_error["error_type"], "payment_required")
        self.assertIn("Payment Required", payment_error["error_message"])
        self.assertEqual(playwright_error["source"], "playwright")
        self.assertEqual(playwright_error["error_type"], "browser_error")
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["reason"], "benchmark_mode")

    def test_default_screenshot_provider_is_playwright(self):
        self.assertEqual(SCREENSHOT_PROVIDER, "playwright")

    def test_playwright_provider_routes_to_playwright_capture(self):
        with patch(
            "src.services.brand_service._take_playwright_screenshot",
            return_value={
                "screenshot_url": "file:///tmp/brand3-shot.png",
                "screenshot_provider": "playwright",
            },
        ) as capture:
            data, limitation = _take_screenshot_with_budget(
                "https://example.com",
                timeout_seconds=0,
                provider="playwright",
            )

        capture.assert_called_once_with("https://example.com")
        self.assertIsNone(limitation)
        self.assertEqual(data["screenshot_provider"], "playwright")

    def test_playwright_provider_failure_falls_back_to_firecrawl(self):
        with patch(
            "src.services.brand_service._take_playwright_screenshot",
            return_value={
                "error": "Playwright not available",
                "error_type": "missing_dependency",
                "screenshot_provider": "playwright",
            },
        ), patch(
            "src.services.brand_service._take_firecrawl_screenshot",
            return_value={
                "screenshot_url": "https://cdn.example.com/fallback.png",
                "screenshot_provider": "firecrawl_screenshot",
            },
        ) as fallback:
            data, limitation = _take_screenshot_with_budget(
                "https://example.com",
                timeout_seconds=0,
                provider="playwright",
            )
        diagnostic = _screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data=data,
            limitation=limitation,
        )

        fallback.assert_called_once_with("https://example.com")
        self.assertIsNone(limitation)
        self.assertEqual(diagnostic["source"], "firecrawl_screenshot")
        self.assertTrue(diagnostic["success"])
        self.assertEqual(data["fallback_from_provider"], "playwright")
        self.assertEqual(data["fallback_reason"], "missing_dependency")
        self.assertEqual(data["primary_error"], "Playwright not available")
        self.assertEqual(diagnostic["fallback_from_provider"], "playwright")
        self.assertEqual(diagnostic["fallback_reason"], "missing_dependency")
        self.assertEqual(diagnostic["primary_error_type"], "missing_dependency")
        self.assertEqual(diagnostic["primary_error"], "Playwright not available")

    def test_playwright_provider_failure_is_structured_when_fallback_fails(self):
        with patch(
            "src.services.brand_service._take_playwright_screenshot",
            return_value={
                "error": "Playwright not available",
                "error_type": "missing_dependency",
                "screenshot_provider": "playwright",
            },
        ), patch(
            "src.services.brand_service._take_firecrawl_screenshot",
            side_effect=RuntimeError("Firecrawl missing API key"),
        ):
            data, limitation = _take_screenshot_with_budget(
                "https://example.com",
                timeout_seconds=0,
                provider="playwright",
            )
        diagnostic = _screenshot_capture_diagnostic(
            attempted=True,
            screenshot_data=data,
            limitation=limitation,
        )

        self.assertIsNone(limitation)
        self.assertEqual(diagnostic["source"], "playwright")
        self.assertFalse(diagnostic["success"])
        self.assertEqual(diagnostic["error_type"], "missing_dependency")
        self.assertTrue(data["fallback_attempted"])
        self.assertEqual(data["fallback_provider"], "firecrawl_screenshot")
        self.assertIn("Firecrawl missing API key", data["fallback_error"])

    def test_cost_policy_summary_exposes_skips_and_cache_savings(self):
        summary = _cost_policy_summary(
            raw_input_cache={"context": "hit", "web": "miss", "social": "skipped"},
            llm_cache={"cache_hits": 2, "cache_misses": 1, "skipped_reason": "missing_api_key"},
            use_llm=True,
            use_social=False,
            use_competitors=False,
            skip_visual_analysis=True,
            context_data=ContextData(url="https://example.com", coverage=0.2, confidence=0.4),
            data_quality="insufficient",
        )

        self.assertEqual(summary["cache_hits"], 1)
        self.assertEqual(summary["cache_misses"], 1)
        self.assertEqual(summary["llm_cache_hits"], 2)
        self.assertEqual(summary["skipped"]["llm"], "missing_api_key")
        self.assertEqual(summary["skipped"]["social"], "disabled_by_request")
        self.assertEqual(summary["skipped"]["deep_llm_narrative"], "insufficient_context_coverage")
        self.assertGreaterEqual(summary["estimated_saved_operations"], 3)

    def test_dimension_confidence_marks_sparse_dimension_insufficient(self):
        summary = _dimension_confidence_summary(
            {
                "presencia": {
                    "web_presence": FeatureValue(
                        "web_presence",
                        80.0,
                        raw_value={"evidence_snippet": "homepage reachable"},
                        confidence=0.9,
                        source="web_scrape",
                    )
                }
            },
            data_quality="good",
            context_data=ContextData(url="https://example.com", coverage=0.8, confidence=0.8),
        )

        self.assertEqual(summary["presencia"]["status"], "insufficient_data")
        self.assertIn("social_footprint", summary["presencia"]["missing_signals"])
        self.assertIn("Conectar senales sociales relevantes", " ".join(summary["presencia"]["recommended_next_steps"]))
        self.assertLess(summary["presencia"]["coverage"], 0.3)

    def test_evidence_summary_counts_feature_and_persisted_evidence(self):
        summary = summarize_evidence_from_features(
            {
                "presencia": {
                    "web_presence": FeatureValue(
                        "web_presence",
                        80.0,
                        raw_value={"evidence_snippet": "homepage reachable"},
                        confidence=0.9,
                        source="web_scrape",
                    )
                }
            },
            evidence_items=[
                {
                    "source": "context",
                    "quote": "robots.txt found",
                    "dimension_name": "presencia",
                }
            ],
        )

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_dimension"]["presencia"], 2)
        self.assertEqual(summary["by_quality"]["indirect"], 1)
        self.assertEqual(summary["by_quality"]["weak"], 1)
        self.assertIn("coherencia", summary["dimensions_without_evidence"])

    def test_trust_summary_payload_includes_aggregate_status_and_labels(self):
        summary = _trust_summary_payload(
            data_quality="good",
            context_summary={"status": "good", "coverage": 0.8},
            evidence_summary={"total": 2},
            dimension_confidence={
                "presencia": {"status": "insufficient_data", "missing_signals": ["schema"]},
                "coherencia": {"status": "insufficient_data"},
                "percepcion": {"status": "insufficient_data"},
            },
        )

        self.assertEqual(summary["overall_status"], "insufficient_data")
        self.assertEqual(summary["overall_status_label"], "datos insuficientes")
        self.assertEqual(summary["overall_reason"], "multiple_dimensions_insufficient")
        self.assertEqual(summary["evidence"]["total"], 2)
        self.assertEqual(summary["limited_dimensions"][0]["name"], "presencia")
        self.assertEqual(summary["limited_dimensions"][0]["missing_signals"], ["schema"])

    def test_run_reuses_raw_input_cache_and_copies_payloads_to_current_run(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(
                url="https://example.com",
                robots_found=True,
                sitemap_found=True,
                sitemap_url_count=3,
                coverage=0.8,
                confidence=0.8,
            )
            web = WebData(
                url="https://example.com",
                title="Example",
                markdown_content="Example brand builds reliable software. " * 12,
            )
            exa = ExaData(
                brand_name="Example",
                mentions=[
                    ExaResult(url=f"https://source{i}.com", title="Example mention", text="Example brand mention.")
                    for i in range(5)
                ],
                diagnostics={"strategy": EXA_STRATEGY_VERSION},
            )

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context) as context_scan:
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web) as web_scrape:
                        with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa) as exa_collect:
                            brand_service.run(
                                "https://example.com",
                                "Example",
                                use_llm=False,
                                use_social=False,
                                use_competitors=False,
                                skip_visual_analysis=True,
                            )
                            second = brand_service.run(
                                "https://example.com",
                                "Example",
                                use_llm=False,
                                use_social=False,
                                use_competitors=False,
                                skip_visual_analysis=True,
                            )

            self.assertEqual(context_scan.call_count, 1)
            self.assertEqual(web_scrape.call_count, 1)
            self.assertEqual(exa_collect.call_count, 1)
            self.assertEqual(second["data_sources"]["raw_input_cache"]["context"], "hit")
            self.assertEqual(second["data_sources"]["raw_input_cache"]["web"], "hit")
            self.assertEqual(second["data_sources"]["raw_input_cache"]["exa"], "hit")
            self.assertEqual(second["data_sources"]["raw_input_cache"]["social"], "skipped")
            self.assertIn("acquisition_provenance", second["data_sources"])
            self.assertIn("acquisition_steps", second["data_sources"])
            self.assertEqual(second["data_sources"]["acquisition_steps"]["web"]["status"], "hit")
            self.assertEqual(second["data_sources"]["acquisition_steps"]["web"]["cache_status"], "hit")
            self.assertEqual(
                second["data_sources"]["acquisition_provenance"]["trace"]["provider_summary"]["web_cache"],
                "hit",
            )
            self.assertEqual(second["data_sources"]["cost_policy"]["cache_hits"], 3)
            self.assertEqual(second["data_sources"]["cost_policy"]["skipped"]["llm"], "disabled_by_request")
            self.assertEqual(second["data_sources"]["cost_policy"]["skipped"]["social"], "disabled_by_request")

            store = SQLiteStore(str(db_path))
            try:
                snapshot = store.get_run_snapshot(second["run_id"])
            finally:
                store.close()
            raw_sources = {item["source"] for item in snapshot["raw_inputs"]}
            self.assertTrue({"context", "web", "exa"}.issubset(raw_sources))

    def test_run_refresh_bypasses_raw_input_cache_reads_but_writes_fresh_inputs(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(
                url="https://example.com",
                robots_found=True,
                sitemap_found=True,
                sitemap_url_count=3,
                coverage=0.8,
                confidence=0.8,
            )
            web = WebData(
                url="https://example.com",
                title="Example",
                markdown_content="Example brand builds reliable software. " * 12,
            )
            exa = ExaData(
                brand_name="Example",
                mentions=[
                    ExaResult(url=f"https://source{i}.com", title="Example mention", text="Example brand mention.")
                    for i in range(5)
                ],
            )

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context) as context_scan:
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web) as web_scrape:
                        with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa) as exa_collect:
                            brand_service.run(
                                "https://example.com",
                                "Example",
                                use_llm=False,
                                use_social=False,
                                use_competitors=False,
                                skip_visual_analysis=True,
                            )
                            refreshed = brand_service.run(
                                "https://example.com",
                                "Example",
                                use_llm=False,
                                use_social=False,
                                use_competitors=False,
                                skip_visual_analysis=True,
                                refresh=True,
                            )

            self.assertEqual(context_scan.call_count, 2)
            self.assertEqual(web_scrape.call_count, 2)
            self.assertEqual(exa_collect.call_count, 2)
            self.assertEqual(refreshed["data_sources"]["raw_input_cache"]["context"], "miss")
            self.assertEqual(refreshed["data_sources"]["raw_input_cache"]["web"], "miss")
            self.assertEqual(refreshed["data_sources"]["raw_input_cache"]["exa"], "miss")
            self.assertEqual(refreshed["data_sources"]["raw_input_cache"]["social"], "skipped")
            self.assertIn("public_presence_inventory", refreshed["data_sources"])
            self.assertEqual(refreshed["data_sources"]["acquisition_steps"]["web"]["status"], "fetched")
            self.assertEqual(refreshed["data_sources"]["acquisition_steps"]["web"]["cache_status"], "miss")
            self.assertEqual(
                refreshed["data_sources"]["public_presence_inventory"]["primary_page"]["url"],
                "https://example.com",
            )
            self.assertEqual(refreshed["context_readiness"]["coverage"], 0.8)

            store = SQLiteStore(str(db_path))
            try:
                snapshot = store.get_run_snapshot(refreshed["run_id"])
            finally:
                store.close()
            raw_sources = {item["source"] for item in snapshot["raw_inputs"]}
            self.assertTrue({"context", "web", "exa"}.issubset(raw_sources))

    def test_run_adds_context_enrichment_for_limited_context_with_public_inventory_base(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(
                url="https://claude.ai",
                homepage_status=403,
                coverage=0.0,
                confidence=0.0,
                confidence_reason=["homepage_unavailable", "low_coverage"],
                error="homepage_unavailable",
            )
            web = WebData(
                url="https://claude.ai",
                title="Claude",
                markdown_content="Claude public product and pricing content. " * 90,
                content_source="browser_fallback",
                browser_status=200,
                links=["https://docs.anthropic.com/en/docs/claude-code"],
            )
            exa = ExaData(
                brand_name="Claude",
                mentions=[
                    ExaResult(url=f"https://source{i}.com", title="Claude mention", text="Claude mention.")
                    for i in range(5)
                ],
                news=[
                    ExaResult(
                        url="https://www.anthropic.com/news/claude",
                        title="Anthropic news about Claude",
                        text="Claude public news",
                    )
                ],
            )

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context):
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web):
                        with patch("src.services.brand_service.WebCollector.scrape_multiple", return_value=[]):
                            with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa):
                                with patch("src.services.brand_service.ExaCollector.search", return_value=[]):
                                    result = brand_service.run(
                                        "https://claude.ai",
                                        "Claude",
                                        use_llm=False,
                                        use_social=False,
                                        use_competitors=False,
                                        skip_visual_analysis=True,
                                        refresh=True,
                                    )

        self.assertEqual(result["context_readiness"]["coverage"], 0.0)
        self.assertEqual(result["context_readiness"]["homepage_status"], 403)
        self.assertEqual(result["confidence_summary"]["status"], "insufficient_data")
        self.assertTrue(result["data_sources"]["public_presence_inventory"]["recommended_evidence_base"])
        self.assertTrue(result["context_enrichment_summary"]["applied"])
        self.assertEqual(result["context_enrichment_summary"]["official_pages_found"], 3)
        self.assertTrue(result["context_effective_readiness"]["applied"])
        self.assertEqual(result["context_effective_readiness"]["status"], "degraded")
        self.assertGreater(result["context_effective_readiness"]["coverage"], 0.0)
        self.assertEqual(
            result["context_effective_readiness"]["reason"],
            "homepage_unavailable_but_public_inventory_available",
        )
        self.assertEqual(result["trust_summary"]["context"]["status"], "insufficient_data")
        self.assertEqual(result["context_readiness"]["coverage"], 0.0)
        self.assertEqual(result["context_readiness"]["homepage_status"], 403)
        self.assertEqual(result["trust_summary"]["overall_status"], "degraded")
        self.assertEqual(result["trust_summary"]["effective_context"]["status"], "degraded")
        interpretation = result["trust_summary"]["interpretation"]
        self.assertTrue(interpretation["audit_usable"])
        self.assertEqual(interpretation["primary_limitation"], "homepage_pre_scan_unavailable")
        self.assertTrue(interpretation["compensated_by_public_inventory"])
        self.assertEqual(interpretation["evidence_base"], "partial_with_context_limitation")
        self.assertIn("homepage no pudo analizarse directamente", interpretation["user_message"])
        self.assertIn("Algunas dimensiones aún tienen evidencia limitada:", interpretation["user_message"])
        self.assertIn("limitación", interpretation["user_message"])
        for alarming_word in ("unreliable", "invalid", "failed audit"):
            self.assertNotIn(alarming_word, interpretation["user_message"].lower())
        self.assertEqual(
            result["trust_summary"]["context_enrichment"]["status"],
            "raw_context_limited_but_public_inventory_available",
        )
        self.assertIsNotNone(result["composite_score"])
        self.assertEqual(set(result["dimensions"]), {"coherencia", "presencia", "percepcion", "diferenciacion", "vitalidad"})

    def test_run_passes_captured_screenshot_to_coherencia_extraction(self):
        captured_screenshot_urls = []

        def fake_coherencia_extract(self, web=None, exa=None, context=None, screenshot_url=None, research_pack=None):
            captured_screenshot_urls.append(screenshot_url)
            return {
                "visual_consistency": FeatureValue("visual_consistency", 80.0, confidence=0.8, source="visual_analysis"),
                "messaging_consistency": FeatureValue("messaging_consistency", 80.0, confidence=0.8, source="heuristic"),
                "tone_consistency": FeatureValue("tone_consistency", 80.0, confidence=0.8, source="heuristic"),
                "cross_channel_coherence": FeatureValue("cross_channel_coherence", 80.0, confidence=0.8, source="heuristic"),
                "structured_identity": FeatureValue("structured_identity", 50.0, confidence=0.5, source="context"),
            }

        def fake_diferenciacion_extract(self, web=None, exa=None, competitor_data=None, screenshot_url=None, context=None, research_pack=None):
            return {
                "positioning_clarity": FeatureValue("positioning_clarity", 70.0, confidence=0.8, source="heuristic"),
                "uniqueness": FeatureValue("uniqueness", 70.0, confidence=0.8, source="heuristic"),
                "competitor_distance": FeatureValue("competitor_distance", 70.0, confidence=0.8, source="heuristic"),
                "content_authenticity": FeatureValue("content_authenticity", 70.0, confidence=0.8, source="heuristic"),
                "brand_personality": FeatureValue("brand_personality", 70.0, confidence=0.8, source="heuristic"),
                "content_depth_signal": FeatureValue("content_depth_signal", 50.0, confidence=0.5, source="context"),
            }

        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(url="https://example.com", coverage=0.8, confidence=0.8)
            web = WebData(
                url="https://example.com",
                title="Example",
                markdown_content="Example brand builds reliable software. " * 12,
            )
            exa = ExaData(
                brand_name="Example",
                mentions=[
                    ExaResult(url=f"https://source{i}.com", title="Example mention", text="Example brand mention.")
                    for i in range(5)
                ],
            )

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context):
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web):
                        with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa):
                            with patch(
                                "src.services.brand_service._take_screenshot_with_budget",
                                return_value=(
                                    {
                                        "screenshot_url": "file:///tmp/brand3-shot.png",
                                        "screenshot_provider": "playwright",
                                    },
                                    None,
                                ),
                            ):
                                with patch.object(
                                    brand_service.CoherenciaExtractor,
                                    "extract",
                                    autospec=True,
                                    side_effect=fake_coherencia_extract,
                                ):
                                    with patch.object(
                                        brand_service.DiferenciacionExtractor,
                                        "extract",
                                        autospec=True,
                                        side_effect=fake_diferenciacion_extract,
                                    ):
                                        result = brand_service.run(
                                            "https://example.com",
                                            "Example",
                                            use_llm=False,
                                            use_social=False,
                                            use_competitors=False,
                                            skip_visual_analysis=False,
                                            refresh=True,
                                        )
            store = SQLiteStore(str(db_path))
            try:
                snapshot = store.get_run_snapshot(result["run_id"])
            finally:
                store.close()

        self.assertEqual(captured_screenshot_urls, ["file:///tmp/brand3-shot.png"])
        self.assertTrue(result["data_sources"]["screenshot_capture"]["success"])
        self.assertEqual(result["data_sources"]["screenshot_capture"]["source"], "playwright")
        screenshot_inputs = [
            item["payload"]
            for item in snapshot["raw_inputs"]
            if item["source"] == "screenshot_capture"
        ]
        self.assertEqual(len(screenshot_inputs), 1)
        self.assertEqual(screenshot_inputs[0]["version"], "screenshot_capture_v1")
        self.assertEqual(screenshot_inputs[0]["capture"]["source"], "playwright")
        self.assertTrue(screenshot_inputs[0]["capture"]["success"])

    def test_run_social_timeout_continues_and_records_limitation(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(
                url="https://example.com",
                robots_found=True,
                sitemap_found=True,
                sitemap_url_count=3,
                coverage=0.8,
                confidence=0.8,
            )
            web = WebData(
                url="https://example.com",
                title="Example",
                markdown_content="Example brand builds reliable software. " * 12,
            )
            exa = ExaData(
                brand_name="Example",
                mentions=[
                    ExaResult(url=f"https://source{i}.com", title="Example mention", text="Example brand mention.")
                    for i in range(5)
                ],
            )
            social = SocialData(brand_name="Example", error="social_collection_timeout_after_1s")

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context):
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web):
                        with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa):
                            with patch(
                                "src.services.brand_service._collect_social_with_budget",
                                return_value=(social, "timeout"),
                            ):
                                result = brand_service.run(
                                    "https://example.com",
                                    "Example",
                                    use_llm=False,
                                    use_social=True,
                                    use_competitors=False,
                                    skip_visual_analysis=True,
                                    refresh=True,
                                )

            self.assertEqual(result["brand"], "Example")
            self.assertFalse(result["social_scraped"])
            self.assertEqual(result["data_sources"]["social_limitation"], "timeout")
            self.assertEqual(result["data_sources"]["raw_input_cache"]["social"], "timeout")
            self.assertEqual(result["data_sources"]["cost_policy"]["skipped"]["social"], "collection_timeout")

            store = SQLiteStore(str(db_path))
            try:
                snapshot = store.get_run_snapshot(result["run_id"])
            finally:
                store.close()
            social_inputs = [item for item in snapshot["raw_inputs"] if item["source"] == "social"]
            self.assertEqual(len(social_inputs), 1)
            self.assertEqual(social_inputs[0]["payload"]["error"], "social_collection_timeout_after_1s")

    def test_run_social_error_continues_and_records_limitation(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(url="https://example.com", coverage=0.8, confidence=0.8)
            web = WebData(
                url="https://example.com",
                title="Example",
                markdown_content="Example brand builds reliable software. " * 12,
            )
            exa = ExaData(
                brand_name="Example",
                mentions=[
                    ExaResult(url=f"https://source{i}.com", title="Example mention", text="Example brand mention.")
                    for i in range(5)
                ],
            )

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context):
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web):
                        with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa):
                            with patch(
                                "src.services.brand_service._collect_social_with_budget",
                                side_effect=RuntimeError("firebase/firecrawl unavailable"),
                            ):
                                result = brand_service.run(
                                    "https://example.com",
                                    "Example",
                                    use_llm=False,
                                    use_social=True,
                                    use_competitors=False,
                                    skip_visual_analysis=True,
                                    refresh=True,
                                )

            self.assertEqual(result["brand"], "Example")
            self.assertFalse(result["social_scraped"])
            self.assertEqual(result["data_sources"]["social_limitation"], "error")
            self.assertEqual(result["data_sources"]["raw_input_cache"]["social"], "error")
            self.assertEqual(result["data_sources"]["cost_policy"]["skipped"]["social"], "collection_error")

    def test_run_social_success_preserves_normal_behavior(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(url="https://example.com", coverage=0.8, confidence=0.8)
            web = WebData(
                url="https://example.com",
                title="Example",
                markdown_content="Example brand builds reliable software. " * 12,
            )
            exa = ExaData(
                brand_name="Example",
                mentions=[
                    ExaResult(url=f"https://source{i}.com", title="Example mention", text="Example brand mention.")
                    for i in range(5)
                ],
            )
            social = SocialData(
                brand_name="Example",
                platforms={
                    "linkedin": PlatformMetrics(
                        platform="linkedin",
                        profile_url="https://linkedin.com/company/example",
                        followers_count=1200,
                    )
                },
                profiles_found=["https://linkedin.com/company/example"],
                total_followers=1200,
                most_active_platform="linkedin",
            )

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context):
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web):
                        with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa):
                            with patch(
                                "src.services.brand_service._collect_social_with_budget",
                                return_value=(social, None),
                            ):
                                result = brand_service.run(
                                    "https://example.com",
                                    "Example",
                                    use_llm=False,
                                    use_social=True,
                                    use_competitors=False,
                                    skip_visual_analysis=True,
                                    refresh=True,
                                )

            self.assertTrue(result["social_scraped"])
            self.assertIsNone(result["data_sources"]["social_limitation"])
            self.assertEqual(result["data_sources"]["raw_input_cache"]["social"], "miss")
            self.assertNotIn("social", result["data_sources"]["cost_policy"]["skipped"])

    def test_run_llm_feature_timeout_continues_and_records_limitation(self):
        class FakeLLM:
            api_key = "key"
            model = "fake-model"
            base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
            cache_hits = 0
            cache_misses = 1
            cache_writes = 0

            def __init__(self, *, model=None):
                self.model = model or self.model
                self.last_failure_reason = None
                self.call_failures = []

            def analyze_momentum(self, mentions, brand_name):
                return {"momentum_score": 60, "verdict": "maintaining", "signals": [], "evidence": []}

            def analyze_messaging_consistency(self, web_content, mentions, brand_name):
                self.last_failure_reason = None
                return {
                    "consistency_score": 80,
                    "verdict": "aligned",
                    "self_category": "software",
                    "third_party_category": "software",
                    "aligned_themes": ["software"],
                    "gaps": [],
                }

            def analyze_tone_consistency(self, web_content, snippets, brand_name):
                self.last_failure_reason = None
                return {
                    "tone_consistency_score": 75,
                    "gap_signal": "none",
                    "self_tone": "clear",
                    "external_tone": "clear",
                    "examples": [],
                }

            def analyze_positioning_clarity(self, content, brand_name, competitor_snippets):
                self.last_failure_reason = None
                return {
                    "clarity_score": 82,
                    "verdict": "clear",
                    "stated_position": "Reliable software for teams.",
                    "target_audience": "Teams",
                    "differentiator_claimed": "Reliability",
                    "evidence": [{"quote": "Reliable software for teams.", "signal": "clear"}],
                }

            def analyze_uniqueness(self, content, brand_name, competitor_snippets):
                self.last_failure_reason = "llm_timeout"
                self.call_failures.append({"reason": "llm_timeout", "error": "llm_call_timeout_after_1s"})
                return {}

            def analyze_brand_sentiment(self, mentions, brand_name):
                self.last_failure_reason = None
                return {
                    "sentiment_score": 72,
                    "verdict": "positive",
                    "controversy": False,
                    "signals": [],
                    "evidence": [],
                }

        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            context = ContextData(url="https://example.com", coverage=0.8, confidence=0.8)
            web = WebData(
                url="https://example.com",
                title="Example",
                markdown_content="Reliable software for teams. " * 40,
            )
            exa = ExaData(
                brand_name="Example",
                mentions=[
                    ExaResult(url=f"https://source{i}.com", title="Positive Example mention", text="Example is reliable.")
                    for i in range(5)
                ],
            )

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch("src.services.brand_service.ContextCollector.scan", return_value=context):
                    with patch("src.services.brand_service.WebCollector.scrape", return_value=web):
                        with patch("src.services.brand_service.ExaCollector.collect_brand_data", return_value=exa):
                            with patch("src.services.brand_service.LLMAnalyzer", FakeLLM):
                                with patch("src.services.brand_service._take_screenshot_with_budget", return_value=({}, "timeout")):
                                    result = brand_service.run(
                                        "https://example.com",
                                        "Example",
                                        use_llm=True,
                                        use_social=False,
                                        use_competitors=False,
                                        skip_visual_analysis=False,
                                        refresh=True,
                                    )

        self.assertEqual(result["brand"], "Example")
        self.assertIn("diferenciacion", result["dimensions"])
        self.assertIn("percepcion", result["dimensions"])
        self.assertEqual(
            result["data_sources"]["llm_cache"]["call_failures"][0]["reason"],
            "llm_timeout",
        )
        self.assertEqual(
            result["data_sources"]["cost_policy"]["skipped"]["llm_feature_calls"],
            "partial_timeout_or_error",
        )
        self.assertEqual(
            result["data_sources"]["llm_provider"],
            {
                "provider": "Google AI Studio / Gemini",
                "model": LLM_CHEAP_MODEL,
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "openai_compatible": True,
            },
        )
        self.assertEqual(
            set(result["data_sources"]["llm_model_roles"]),
            {"default", "cheap", "premium", "audit_analyst", "vision"},
        )
        self.assertNotIn("api_key", result["data_sources"]["llm_model_roles"])
        self.assertEqual(
            result["data_sources"]["screenshot_capture"],
            {
                "attempted": True,
                "success": False,
                "status": "timeout",
                "source": "firecrawl_screenshot",
                "error_type": "timeout",
                "error_message": "timeout",
            },
        )
        visual_raw = result["dimensions"]  # Dimension scores remain produced by the existing scoring path.
        self.assertIn("coherencia", visual_raw)


if __name__ == "__main__":
    unittest.main()
