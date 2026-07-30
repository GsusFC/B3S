import os
import struct
import tempfile
import unittest
import zlib
from unittest.mock import patch

from src.collectors.competitor_collector import (
    ComparisonResult,
    CompetitorCollector,
    CompetitorData,
    CompetitorInfo,
)
from src.collectors.exa_collector import ExaData, ExaResult
from src.collectors.exa_collector import ExaCollector
from src.collectors.social_collector import PlatformMetrics, SocialData
from src.collectors.web_collector import WebData
from src.collectors.web_collector import WebCollector
from src.features.coherencia import CoherenciaExtractor
from src.features.diferenciacion import DiferenciacionExtractor
from src.features.llm_analyzer import LLMAnalyzer
from src.features.percepcion import PercepcionExtractor
from src.features.presencia import PresenciaExtractor
from src.features.visual_analyzer import VisualAnalyzer, VisualAnalysisResult
from src.features.vitalidad import VitalidadExtractor


def _write_test_png(path, width=12, height=8):
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            if x < width // 3:
                row.extend((232, 24, 48))
            elif x < 2 * width // 3:
                row.extend((20, 120, 220))
            else:
                row.extend((245, 245, 245 if y % 2 else 220))
        rows.append(b"\x00" + bytes(row))
    raw = zlib.compress(b"".join(rows))

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", raw)
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as f:
        f.write(png)


class VisualAnalyzerTests(unittest.TestCase):
    def test_local_file_screenshot_analysis_extracts_dominant_colors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "screenshot.png")
            _write_test_png(image_path)
            analyzer = VisualAnalyzer(vision_api_key="")
            analyzer.vision_api_key = ""

            result = analyzer.analyze_screenshot(f"file://{image_path}", brand_name="Example")

        self.assertFalse(result.error)
        self.assertEqual(result.details["method"], "local_image_analysis")
        self.assertEqual(result.details["image_dimensions"], {"width": 12, "height": 8})
        self.assertGreaterEqual(len(result.details["dominant_colors"]), 3)
        self.assertNotEqual(result.details["style"], "unknown")
        self.assertGreater(result.confidence, 0.5)

    def test_local_analysis_used_when_vision_call_returns_no_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "screenshot.png")
            _write_test_png(image_path)
            analyzer = VisualAnalyzer(vision_api_key="secret-test-key")

            with patch.object(analyzer, "_call_vision_api", return_value={}):
                result = analyzer.analyze_screenshot(f"file://{image_path}", brand_name="Example")

        self.assertFalse(result.error)
        self.assertEqual(result.details["method"], "local_image_analysis")
        self.assertTrue(result.details["vision_failed"])
        self.assertNotEqual(result.details["dominant_colors"], [])

    def test_visual_analysis_details_do_not_expose_api_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "screenshot.png")
            _write_test_png(image_path)
            analyzer = VisualAnalyzer(vision_api_key="sk-secret-visual-key")

            with patch.object(analyzer, "_call_vision_api", return_value={}):
                result = analyzer.analyze_screenshot(f"file://{image_path}", brand_name="Example")

        serialized = str(result.details)
        self.assertNotIn("sk-secret-visual-key", serialized)
        self.assertNotIn("api_key", serialized.lower())


class PercepcionExtractorTests(unittest.TestCase):
    """Covers the 4 percepcion features after the refactor with dict raw_value."""

    def _make_llm(self, sentiment_payload=None, older_payload=None, newer_payload=None, sequence=None):
        class FakeLLM:
            api_key = "sk-test"
            _calls = 0
            def analyze_brand_sentiment(self, mentions, brand_name):
                if sequence is not None:
                    idx = FakeLLM._calls
                    FakeLLM._calls += 1
                    if idx < len(sequence):
                        return sequence[idx]
                    return sequence[-1]
                # Two-call trend case
                if older_payload is not None and newer_payload is not None:
                    idx = FakeLLM._calls
                    FakeLLM._calls += 1
                    return older_payload if idx == 0 else newer_payload
                return sentiment_payload
        return FakeLLM()

    # ── brand_sentiment ────────────────────────────────────────────────

    def test_brand_sentiment_without_llm_uses_normalized_heuristic(self):
        exa = ExaData(
            brand_name="Test Brand",
            mentions=[
                ExaResult(url="https://reviews.example.com/test-brand", title="Test Brand review", text="excellent amazing outstanding"),
                ExaResult(url="https://directory.example.com/test-brand", title="Test Brand profile", text="great innovative reliable"),
            ],
        )
        feature = PercepcionExtractor()._brand_sentiment(exa)
        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.raw_value["reason"], "llm_unavailable")
        self.assertGreater(feature.raw_value["pos_count"], 0)

    def test_brand_sentiment_without_mentions_returns_neutral(self):
        feature = PercepcionExtractor()._brand_sentiment(ExaData(brand_name="X"))
        self.assertEqual(feature.value, 50.0)
        self.assertEqual(feature.raw_value["reason"], "no_mentions")

    def test_brand_sentiment_ignores_owned_and_collision_mentions(self):
        exa = ExaData(
            brand_name="www.cofisolutions.com",
            mentions=[
                ExaResult(
                    url="https://cofisolutions.com/",
                    title="COFI Solutions",
                    text="Owned brand copy",
                    source_class="owned",
                    relation="audited_surface",
                ),
                ExaResult(
                    url="https://www.coforge.com/news",
                    title="Coforge launches new platform",
                    text="Unrelated enterprise services news",
                    source_class="external",
                    relation="external",
                ),
            ],
            news=[
                ExaResult(
                    url="https://www.bcv.hn/cofisa",
                    title="COFISA enters market",
                    text="Unrelated financial institution news",
                    source_class="external",
                    relation="external",
                )
            ],
        )
        feature = PercepcionExtractor()._brand_sentiment(exa)
        self.assertEqual(feature.value, 50.0)
        self.assertEqual(feature.raw_value["reason"], "no_independent_relevant_mentions")

    def test_brand_sentiment_with_llm_uses_structured_verdict(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://x/1", title="t", text="a"),
            ExaResult(url="https://x/2", title="t", text="b"),
            ExaResult(url="https://x/3", title="t", text="c"),
        ])
        llm = self._make_llm(sentiment_payload={
            "sentiment_score": 82,
            "verdict": "positive",
            "overall_tone": "People praise reliability",
            "positive_themes": ["reliability", "speed"],
            "negative_themes": [],
            "evidence": [
                {"quote": "they ship fast", "source_url": "https://x/1", "signal": "positive"},
            ],
            "controversy_detected": False,
            "controversy_details": None,
            "reasoning": "Positive across mentions.",
        })
        feature = PercepcionExtractor(llm=llm)._brand_sentiment(exa)
        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.value, 82.0)
        self.assertEqual(feature.raw_value["verdict"], "positive")
        self.assertFalse(feature.raw_value["controversy_detected"])
        self.assertEqual(len(feature.raw_value["evidence"]), 1)

    def test_brand_sentiment_with_controversy_caps_score(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://x/1", title="t", text="a"),
            ExaResult(url="https://x/2", title="t", text="b"),
            ExaResult(url="https://x/3", title="t", text="c"),
        ])
        llm = self._make_llm(sentiment_payload={
            "sentiment_score": 80,  # LLM gave high score but flagged controversy
            "verdict": "mixed",
            "overall_tone": "Mixed with legal concerns",
            "positive_themes": [],
            "negative_themes": ["lawsuit"],
            "evidence": [
                {"quote": "filed a class action", "source_url": "https://x/1", "signal": "negative"},
            ],
            "controversy_detected": True,
            "controversy_details": "Class action lawsuit filed Q2 2026.",
            "reasoning": "Legal issue dominates.",
        })
        feature = PercepcionExtractor(llm=llm)._brand_sentiment(exa)
        self.assertLessEqual(feature.value, 35.0)
        self.assertTrue(feature.raw_value["controversy_detected"])
        self.assertTrue(feature.raw_value["controversy_cap_applied"])
        self.assertEqual(feature.raw_value["capped_from_score"], 80.0)
        self.assertEqual(feature.raw_value["controversy_details"], "Class action lawsuit filed Q2 2026.")

    def test_brand_sentiment_invalid_verdict_falls_back(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://x/1", title="t", text="a"),
        ])
        llm = self._make_llm(sentiment_payload={
            "sentiment_score": 70, "verdict": "glowing", "evidence": [],
        })
        feature = PercepcionExtractor(llm=llm)._brand_sentiment(exa)
        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.raw_value["reason"], "llm_invalid_verdict")

    def test_brand_sentiment_malformed_evidence_is_filtered(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://x/1", title="t", text="a"),
            ExaResult(url="https://x/2", title="t", text="b"),
        ])
        llm = self._make_llm(sentiment_payload={
            "sentiment_score": 72,
            "verdict": "positive",
            "positive_themes": [],
            "negative_themes": [],
            "evidence": [
                {"quote": "solid", "source_url": "https://x/1", "signal": "positive"},
                {"quote": 123, "source_url": "https://x/2", "signal": "positive"},  # malformed
                "not a dict",
            ],
            "controversy_detected": False,
            "reasoning": "ok",
        })
        feature = PercepcionExtractor(llm=llm)._brand_sentiment(exa)
        self.assertEqual(feature.source, "llm")
        self.assertEqual(len(feature.raw_value["evidence"]), 1)
        self.assertEqual(feature.confidence, 0.5)
        self.assertEqual(feature.raw_value["reason"], "llm_partial_evidence")

    def test_brand_sentiment_non_bool_controversy_is_treated_as_false_with_warning(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://x/1", title="t", text="a"),
        ])
        llm = self._make_llm(sentiment_payload={
            "sentiment_score": 72,
            "verdict": "positive",
            "evidence": [
                {"quote": "ok", "source_url": "https://x/1", "signal": "positive"},
            ],
            "controversy_detected": "yes",  # wrong type
            "reasoning": "ok",
        })
        feature = PercepcionExtractor(llm=llm)._brand_sentiment(exa)
        self.assertFalse(feature.raw_value["controversy_detected"])
        self.assertEqual(feature.raw_value["controversy_detected_type_warning"], "str")
        self.assertEqual(feature.value, 72.0)

    # ── mention_volume ─────────────────────────────────────────────────

    def test_mention_volume_returns_dict_with_tier_and_top_domains(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://techcrunch.com/a", title="t", text="..."),
            ExaResult(url="https://techcrunch.com/b", title="t", text="..."),
            ExaResult(url="https://theverge.com/a", title="t", text="..."),
        ], news=[
            ExaResult(url="https://news.example.com/a", title="t", text="..."),
        ])
        feature = PercepcionExtractor()._mention_volume(exa)
        self.assertEqual(feature.raw_value["total_mentions"], 4)
        self.assertEqual(feature.raw_value["volume_tier"], "low")
        self.assertEqual(feature.raw_value["top_domains"][0], "techcrunch.com")

    def test_mention_volume_without_exa_reports_none(self):
        feature = PercepcionExtractor()._mention_volume(exa=None)
        self.assertEqual(feature.raw_value["volume_tier"], "none")
        self.assertEqual(feature.raw_value["total_mentions"], 0)

    # ── sentiment_trend ────────────────────────────────────────────────

    def test_sentiment_trend_insufficient_dated_returns_neutral(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://x/1", title="t", text="great", published_date=""),
            ExaResult(url="https://x/2", title="t", text="great", published_date="2026-04-01"),
            ExaResult(url="https://x/3", title="t", text="bad", published_date=""),
        ])
        feature = PercepcionExtractor()._sentiment_trend(exa)
        self.assertEqual(feature.value, 50.0)
        self.assertEqual(feature.raw_value["reason"], "insufficient_dated_mentions")

    def test_sentiment_trend_with_llm_compares_halves(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://x/1", title="t", text="lawsuit trouble", published_date="2024-01-01"),
            ExaResult(url="https://x/2", title="t", text="controversy", published_date="2024-02-01"),
            ExaResult(url="https://x/3", title="t", text="great recovery", published_date="2026-03-01"),
            ExaResult(url="https://x/4", title="t", text="amazing growth", published_date="2026-04-01"),
        ])
        llm = self._make_llm(
            older_payload={"sentiment_score": 30, "verdict": "negative", "evidence": []},
            newer_payload={"sentiment_score": 80, "verdict": "positive", "evidence": []},
        )
        feature = PercepcionExtractor(llm=llm)._sentiment_trend(exa)
        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.raw_value["trend"], "improving")
        self.assertEqual(feature.raw_value["delta"], 50.0)

    def test_sentiment_trend_without_llm_uses_normalized_heuristic(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://x/1", title="t", text="lawsuit trouble", published_date="2024-01-01"),
            ExaResult(url="https://x/2", title="t", text="fraud scam", published_date="2024-02-01"),
            ExaResult(url="https://x/3", title="t", text="great innovative", published_date="2026-03-01"),
            ExaResult(url="https://x/4", title="t", text="amazing reliable", published_date="2026-04-01"),
        ])
        feature = PercepcionExtractor()._sentiment_trend(exa)
        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.raw_value["method"], "heuristic_fallback")
        self.assertEqual(feature.raw_value["trend"], "improving")

    def test_sentiment_trend_ignores_irrelevant_collision_news(self):
        exa = ExaData(
            brand_name="www.cofisolutions.com",
            mentions=[
                ExaResult(
                    url="https://cofisolutions.com/",
                    title="COFI Solutions",
                    text="Owned brand copy",
                    published_date="2026-01-01",
                    source_class="owned",
                    relation="audited_surface",
                )
            ],
            news=[
                ExaResult(
                    url="https://www.coeosolutions.com/news",
                    title="COEO Solutions investment",
                    text="Unrelated external news",
                    published_date="2026-02-01",
                    source_class="external",
                    relation="external",
                ),
                ExaResult(
                    url="https://news.coforge.com/story",
                    title="Coforge partnership",
                    text="Unrelated external news",
                    published_date="2026-03-01",
                    source_class="external",
                    relation="external",
                ),
            ],
        )
        feature = PercepcionExtractor()._sentiment_trend(exa)
        self.assertEqual(feature.value, 50.0)
        self.assertEqual(feature.raw_value["reason"], "no_independent_relevant_mentions")

    # ── review_quality ─────────────────────────────────────────────────

    def test_review_quality_without_platforms_returns_absent(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url=f"https://example.com/{i}", title="t", text="strong") for i in range(4)
        ], news=[ExaResult(url="https://news.example.com/item", title="t", text="launch")])
        feature = PercepcionExtractor()._review_quality(exa)
        self.assertEqual(feature.raw_value["review_signal"], "absent")
        self.assertEqual(feature.raw_value["total_review_results"], 0)

    def test_review_quality_rewards_professional_platforms(self):
        exa = ExaData(brand_name="X", mentions=[
            ExaResult(url="https://www.g2.com/products/example/reviews", title="G2", text="..."),
            ExaResult(url="https://www.trustpilot.com/review/example.com", title="TP", text="..."),
        ])
        feature = PercepcionExtractor()._review_quality(exa)
        self.assertTrue(feature.raw_value["has_professional_reviews"])
        self.assertFalse(feature.raw_value["has_consumer_reviews"])
        self.assertGreaterEqual(feature.value, 60.0)
        self.assertIn(feature.raw_value["review_signal"], {"moderate", "strong"})
        domains = [p["domain"] for p in feature.raw_value["platforms_with_reviews"]]
        self.assertIn("g2.com", domains)
        self.assertIn("trustpilot.com", domains)

    # ── contract ───────────────────────────────────────────────────────

    def test_extract_always_returns_four_features(self):
        features = PercepcionExtractor().extract(web=None, exa=None)
        self.assertEqual(
            set(features.keys()),
            {"brand_sentiment", "mention_volume", "sentiment_trend", "review_quality"},
        )


class CompetitorCollectorTests(unittest.TestCase):
    def test_competitor_scraping_does_not_crawl_owned_subpages(self):
        class FakeWebCollector:
            def __init__(self):
                self.calls = []

            def scrape(self, url, crawl_subpages=True):
                self.calls.append({"url": url, "crawl_subpages": crawl_subpages})
                return WebData(
                    url=url,
                    markdown_content="Competitor homepage content " * 20,
                )

        web = FakeWebCollector()
        collector = CompetitorCollector(web_collector=web)
        result = CompetitorData(
            brand_name="Brand",
            brand_url="https://brand.example",
            competitors=[
                CompetitorInfo(name="Comp A", url="https://a.example"),
                CompetitorInfo(name="Comp B", url="https://b.example"),
            ],
        )

        collector._scrape_competitors(result)

        self.assertEqual(
            web.calls,
            [
                {"url": "https://a.example", "crawl_subpages": False},
                {"url": "https://b.example", "crawl_subpages": False},
            ],
        )
        self.assertTrue(all(comp.web_data for comp in result.competitors))


class DiferenciacionExtractorTests(unittest.TestCase):
    def setUp(self):
        self.extractor = DiferenciacionExtractor()

    @staticmethod
    def _competitor_data():
        return CompetitorData(
            brand_name="Example",
            brand_url="https://example.com",
            comparisons=[
                ComparisonResult(
                    competitor_name="ClosestCo",
                    competitor_url="https://closest.example",
                    overall_distance=0.22,
                    brand_unique_terms=["deterministic", "control", "governance"],
                ),
                ComparisonResult(
                    competitor_name="FarCo",
                    competitor_url="https://far.example",
                    overall_distance=0.81,
                    brand_unique_terms=["deterministic", "control", "governance", "audit"],
                ),
            ],
        )

    @staticmethod
    def _make_llm(positioning=None, uniqueness=None):
        llm = LLMAnalyzer(api_key="test")
        llm.analyze_positioning_clarity = lambda *args, **kwargs: positioning or {}
        llm.analyze_uniqueness = lambda *args, **kwargs: uniqueness or {}
        return llm

    def test_llm_competitor_snippets_are_built_once_with_feature_specific_limits(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("Brand homepage content " * 60),
        )
        competitor_body = "A" * 450
        competitor_data = CompetitorData(
            brand_name="Example",
            brand_url="https://example.com",
            competitors=[
                CompetitorInfo(
                    name="Competitor",
                    url="https://competitor.example",
                    web_data=WebData(
                        url="https://competitor.example",
                        markdown_content=competitor_body,
                    ),
                )
            ],
        )
        calls = {}

        class FakeLLM:
            api_key = "test"

            def analyze_positioning_clarity(self, content, brand_name, competitor_snippets):
                calls["positioning_snippets"] = competitor_snippets
                return {
                    "clarity_score": 80,
                    "verdict": "clear",
                    "stated_position": "Position",
                    "target_audience": "Audience",
                    "differentiator_claimed": "Claim",
                    "evidence": [{"quote": "Position", "signal": "clear"}],
                    "reasoning": "Clear.",
                }

            def analyze_uniqueness(self, content, brand_name, competitor_snippets):
                calls["uniqueness_snippets"] = competitor_snippets
                return {
                    "uniqueness_score": 70,
                    "verdict": "moderately_unique",
                    "unique_phrases": ["Position"],
                    "generic_phrases": [],
                    "brand_vocabulary": ["Position"],
                    "competitor_overlap_signals": [],
                    "reasoning": "Some signal.",
                }

        features = DiferenciacionExtractor(llm=FakeLLM()).extract(
            web=web,
            competitor_data=competitor_data,
        )

        self.assertEqual(features["positioning_clarity"].source, "llm")
        self.assertEqual(features["uniqueness"].source, "llm")
        self.assertEqual(calls["positioning_snippets"], [f"Competitor: {competitor_body[:400]}"])
        self.assertEqual(calls["uniqueness_snippets"], [f"Competitor: {competitor_body[:300]}"])

    def test_positioning_clarity_without_llm_uses_heuristic_fallback(self):
        web = WebData(
            url="https://priorlabs.ai",
            title="One Model, Infinite Predictions",
            markdown_content=(
                "# One Model, Infinite Predictions\n\n"
                "We are building tabular foundation models for developers.\n"
                "Built for teams making predictions on structured data.\n"
            ),
        )

        feature = self.extractor.extract(web=web)["positioning_clarity"]

        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.confidence, 0.4)
        self.assertEqual(feature.raw_value["reason"], "llm_unavailable")
        self.assertIn("built for", feature.raw_value["signals_detected"])

    def test_positioning_clarity_with_llm_uses_structured_output(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("word " * 600),
        )
        extractor = DiferenciacionExtractor(
            llm=self._make_llm(
                positioning={
                    "clarity_score": 82,
                    "verdict": "clear",
                    "stated_position": "Deterministic infrastructure for enterprise AI.",
                    "target_audience": "Enterprise AI teams",
                    "differentiator_claimed": "A deterministic control layer",
                    "evidence": [
                        {"quote": "Deterministic infrastructure for enterprise AI.", "signal": "clear"}
                    ],
                    "reasoning": "The statement is concrete and repeated.",
                }
            )
        )

        feature = extractor.extract(web=web)["positioning_clarity"]

        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.value, 82)
        self.assertEqual(feature.raw_value["verdict"], "clear")
        self.assertEqual(len(feature.raw_value["evidence"]), 1)
        self.assertEqual(feature.confidence, 0.85)

    def test_llm_positioning_prompt_preserves_structured_pack_beyond_legacy_limit(self):
        llm = LLMAnalyzer(api_key="test")
        captured = {}

        def fake_call_json(system, user, *args, **kwargs):
            captured["system"] = system
            captured["user"] = user
            return {
                "clarity_score": 70,
                "verdict": "clear",
                "stated_position": "Structured pack position.",
                "target_audience": "operators",
                "differentiator_claimed": "deep marker evidence",
                "evidence": [{"quote": "DEEP_STRUCTURED_MARKER", "signal": "clear"}],
                "reasoning": "Uses deeper structured evidence.",
            }

        llm._call_json = fake_call_json
        structured_pack = (
            "Structured Brand Research Pack\n"
            "Core evidence:\n"
            + ("owned evidence " * 260)
            + "DEEP_STRUCTURED_MARKER"
        )

        result = llm.analyze_positioning_clarity(structured_pack, "Structured Brand")

        self.assertEqual(result["verdict"], "clear")
        self.assertIn("DEEP_STRUCTURED_MARKER", captured["user"])
        self.assertIn("rejected noise", captured["user"].lower())
        self.assertIn("Evidence input", captured["user"])

    def test_positioning_clarity_invalid_verdict_falls_back(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("word " * 600),
        )
        extractor = DiferenciacionExtractor(
            llm=self._make_llm(
                positioning={
                    "clarity_score": 82,
                    "verdict": "sharp",
                    "stated_position": "x",
                    "target_audience": "y",
                    "differentiator_claimed": "z",
                    "evidence": [{"quote": "x", "signal": "clear"}],
                    "reasoning": "bad verdict",
                }
            )
        )

        feature = extractor.extract(web=web)["positioning_clarity"]

        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.raw_value["reason"], "llm_invalid_verdict")

    def test_positioning_clarity_malformed_evidence_degrades_confidence(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("word " * 600),
        )
        extractor = DiferenciacionExtractor(
            llm=self._make_llm(
                positioning={
                    "clarity_score": 78,
                    "verdict": "clear",
                    "stated_position": "x",
                    "target_audience": "y",
                    "differentiator_claimed": "z",
                    "evidence": [{"quote": "x"}],
                    "reasoning": "partial evidence",
                }
            ),
        )

        feature = extractor.extract(web=web)["positioning_clarity"]

        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.confidence, 0.5)
        self.assertEqual(feature.raw_value["reason"], "llm_partial_evidence")
        self.assertEqual(feature.raw_value["evidence"], [])

    def test_positioning_clarity_uses_research_pack_instead_of_raw_truncated_copy(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("Navigation Login Sign up Pricing " * 160)
            + "Deep evidence that should be invisible to legacy 3000 char truncation.",
        )
        research_pack = {
            "version": "brand_research_pack_v0_1",
            "input_url": "https://example.com",
            "entity_type": "product",
            "resolved_entity": {
                "resolved_entity": "Example",
                "entity_type": "product",
                "canonical_url": "https://example.com",
            },
            "offer": "Example is a deterministic control layer for enterprise AI operations.",
            "audience": "enterprise AI operations teams",
            "outcome": "teams can audit and constrain autonomous workflows",
            "category": "AI governance infrastructure",
            "proof_points": [
                {
                    "text": "Example is a deterministic control layer for enterprise AI operations.",
                    "source_url": "https://example.com/product",
                    "source_type": "owned",
                    "source_label": "product_offer",
                    "confidence": "high",
                }
            ],
            "noise_rejected": [
                {
                    "text": "Login Sign up Pricing",
                    "source_url": "https://example.com",
                    "source_type": "owned",
                    "source_label": "noise",
                    "confidence": "high",
                }
            ],
        }
        calls = {}

        class FakeLLM:
            api_key = "test"

            def analyze_positioning_clarity(self, content, brand_name, competitor_snippets):
                calls["positioning_content"] = content
                return {
                    "clarity_score": 84,
                    "verdict": "clear",
                    "stated_position": "AI governance infrastructure.",
                    "target_audience": "enterprise AI operations teams",
                    "differentiator_claimed": "deterministic control layer",
                    "evidence": [
                        {
                            "quote": "Example is a deterministic control layer for enterprise AI operations.",
                            "signal": "clear",
                        }
                    ],
                    "reasoning": "The structured pack contains a concrete offer.",
                }

            def analyze_uniqueness(self, content, brand_name, competitor_snippets):
                return {}

        feature = DiferenciacionExtractor(llm=FakeLLM()).extract(
            web=web,
            research_pack=research_pack,
        )["positioning_clarity"]

        self.assertEqual(feature.source, "llm")
        self.assertIn("Structured Brand Research Pack", calls["positioning_content"])
        self.assertIn("deterministic control layer", calls["positioning_content"])
        self.assertIn("Rejected noise", calls["positioning_content"])
        self.assertNotEqual(calls["positioning_content"], web.markdown_content)

    def test_uniqueness_without_llm_uses_normalized_ratio_fallback(self):
        web = WebData(
            url="https://generic.example",
            title="Generic SaaS",
            markdown_content=(
                "# Generic SaaS\n\n"
                "We help businesses grow and improve efficiency.\n"
                "Save time. Save money. Better results. Cutting edge workflows.\n"
            ),
        )

        feature = self.extractor.extract(web=web)["uniqueness"]

        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.raw_value["reason"], "llm_unavailable")
        self.assertGreater(feature.raw_value["ratio"], 0.0)
        self.assertGreater(feature.raw_value["sentence_count"], 0)

    def test_uniqueness_llm_timeout_uses_fallback_with_timeout_reason(self):
        web = WebData(
            url="https://generic.example",
            title="Generic SaaS",
            markdown_content=(
                "# Generic SaaS\n\n"
                "We help businesses grow and improve efficiency.\n"
                "Save time. Save money. Better results. Cutting edge workflows.\n"
            ),
        )
        llm = self._make_llm(uniqueness={})
        llm.last_failure_reason = "llm_timeout"

        feature = DiferenciacionExtractor(llm=llm)._uniqueness(web)

        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.raw_value["reason"], "llm_timeout")
        self.assertGreater(feature.raw_value["sentence_count"], 0)

    def test_uniqueness_with_llm_uses_structured_output(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("word " * 600),
        )
        extractor = DiferenciacionExtractor(
            llm=self._make_llm(
                uniqueness={
                    "uniqueness_score": 76,
                    "verdict": "moderately_unique",
                    "unique_phrases": ["deterministic layer"],
                    "generic_phrases": ["cutting edge"],
                    "brand_vocabulary": ["frontier intelligence"],
                    "competitor_overlap_signals": ["shares some enterprise framing"],
                    "reasoning": "Some ownable language exists.",
                }
            )
        )

        feature = extractor.extract(web=web)["uniqueness"]

        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.value, 76)
        self.assertEqual(feature.raw_value["verdict"], "moderately_unique")
        self.assertIn("deterministic layer", feature.raw_value["unique_phrases"])
        self.assertEqual(feature.confidence, 0.85)

    def test_uniqueness_invalid_verdict_falls_back(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("word " * 600),
        )
        extractor = DiferenciacionExtractor(
            llm=self._make_llm(
                uniqueness={
                    "uniqueness_score": 90,
                    "verdict": "iconic",
                    "unique_phrases": [],
                    "generic_phrases": [],
                    "brand_vocabulary": [],
                    "competitor_overlap_signals": [],
                    "reasoning": "bad verdict",
                }
            )
        )

        feature = extractor.extract(web=web)["uniqueness"]

        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.raw_value["reason"], "llm_invalid_verdict")

    def test_uniqueness_uses_research_pack_signals_as_llm_input(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("Generic SaaS navigation " * 250),
        )
        research_pack = {
            "version": "brand_research_pack_v0_1",
            "input_url": "https://example.com",
            "entity_type": "product",
            "resolved_entity": {"resolved_entity": "Example", "entity_type": "product"},
            "offer": "Example turns agentic workflows into auditable control paths.",
            "personality_signals": ["precise", "control-oriented"],
            "visual_or_conceptual_signals": ["control paths"],
            "attributes_signals": ["auditable", "deterministic"],
            "values_signals": ["accountability"],
            "evidence_gaps": ["No third-party adoption proof was found."],
        }
        calls = {}

        class FakeLLM:
            api_key = "test"

            def analyze_positioning_clarity(self, content, brand_name, competitor_snippets):
                return {}

            def analyze_uniqueness(self, content, brand_name, competitor_snippets):
                calls["uniqueness_content"] = content
                return {
                    "uniqueness_score": 78,
                    "verdict": "moderately_unique",
                    "unique_phrases": ["auditable control paths"],
                    "generic_phrases": [],
                    "brand_vocabulary": ["agentic workflows"],
                    "competitor_overlap_signals": [],
                    "reasoning": "The pack contains ownable vocabulary.",
                }

        feature = DiferenciacionExtractor(llm=FakeLLM()).extract(
            web=web,
            research_pack=research_pack,
        )["uniqueness"]

        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.value, 78)
        self.assertIn("agentic workflows", calls["uniqueness_content"])
        self.assertIn("Evidence gaps", calls["uniqueness_content"])
        self.assertNotEqual(calls["uniqueness_content"], web.markdown_content)

    def test_competitor_distance_uses_structured_raw_value(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content="Deterministic infrastructure for AI teams.",
        )
        feature = self.extractor.extract(
            web=web,
            competitor_data=self._competitor_data(),
        )["competitor_distance"]

        self.assertEqual(feature.source, "competitor_web_comparison")
        self.assertEqual(feature.raw_value["closest_competitor"]["name"], "ClosestCo")
        self.assertEqual(feature.raw_value["most_different"]["name"], "FarCo")
        self.assertEqual(feature.raw_value["competitors_analyzed"], 2)
        self.assertIsInstance(feature.raw_value["brand_unique_terms"], list)

    def test_content_authenticity_and_brand_personality_return_structured_raw_value(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=(
                "We're building a deterministic layer for enterprise AI. "
                "We believe teams deserve control instead of generic copilots. "
                "Learn more about our platform and how we help teams move faster."
            ),
        )
        exa = ExaData(
            brand_name="Example",
            mentions=[
                ExaResult(
                    url="https://example.com/coverage",
                    title="Coverage",
                    text="Opinionated founder-led product.",
                )
            ],
        )

        features = self.extractor.extract(web=web, exa=exa)
        authenticity = features["content_authenticity"]
        personality = features["brand_personality"]

        self.assertIsInstance(authenticity.raw_value, dict)
        self.assertIn("authenticity_verdict", authenticity.raw_value)
        self.assertIsInstance(personality.raw_value, dict)
        self.assertIn("signals_detected", personality.raw_value)


class PresenciaExtractorTests(unittest.TestCase):
    def setUp(self):
        self.extractor = PresenciaExtractor()

    def _exa_mentions(self, brand_name: str = "Acme") -> ExaData:
        return ExaData(
            brand_name=brand_name,
            mentions=[
                ExaResult(
                    url="https://acme.com/blog/launch",
                    title="Acme launches new product",
                    text="Acme expands its launch motion with a new platform release.",
                    summary="Acme expands with a product release.",
                    score=0.9,
                ),
                ExaResult(
                    url="https://techcrunch.com/acme-funding",
                    title="Acme raises funding",
                    text="Acme is highlighted as a growing company.",
                    summary="Growing company profile.",
                    score=0.8,
                ),
                ExaResult(
                    url="https://random.com/roundup",
                    title="AI roundup",
                    text="Many vendors are covered in this roundup.",
                    summary="General roundup with many names.",
                    score=0.3,
                ),
            ],
            ai_visibility_results=[
                ExaResult(
                    url="https://example.com/acme-best-tools",
                    title="Acme in top AI tools",
                    text="Acme is recommended for enterprise teams.",
                    score=0.8,
                ),
                ExaResult(
                    url="https://example.com/general-roundup",
                    title="General AI roundup",
                    text="Acme appears in a broader list.",
                    score=0.5,
                ),
            ],
        )

    def test_web_presence_placeholder_page_scores_minimal(self):
        web = WebData(
            url="http://placeholder.example",
            title="Coming Soon",
            markdown_content="Coming soon. Buy this domain today.",
        )

        feature = self.extractor._web_presence(web)

        self.assertEqual(feature.value, 5.0)
        self.assertEqual(feature.raw_value["page_status"], "placeholder")
        self.assertIn("placeholder_detected", feature.raw_value["signals_detected"])

    def test_web_presence_normal_site_scores_high_with_structured_raw_value(self):
        web = WebData(
            url="https://example.com",
            title="Example Platform",
            meta_description="Example Platform helps finance teams move faster.",
            markdown_content=(
                "# Example Platform\n\n"
                "Example Platform helps finance teams move faster with approvals, docs, and automation.\n"
                "Pricing About Contact Docs Features Get started Privacy Terms.\n"
            ),
        )

        feature = self.extractor._web_presence(web)

        self.assertGreaterEqual(feature.value, 75.0)
        self.assertTrue(feature.raw_value["has_https"])
        self.assertEqual(feature.raw_value["page_status"], "live")
        self.assertIsInstance(feature.raw_value["evidence_snippet"], str)
        self.assertIn("https", feature.raw_value["signals_detected"])

    def test_web_presence_without_https_loses_signal(self):
        secure = WebData(
            url="https://example.com",
            title="Example",
            meta_description="Example is a real product site.",
            markdown_content="# Example\n\nExample is a real product site with pricing and contact.",
        )
        insecure = WebData(
            url="http://example.com",
            title="Example",
            meta_description="Example is a real product site.",
            markdown_content="# Example\n\nExample is a real product site with pricing and contact.",
        )

        secure_feature = self.extractor._web_presence(secure)
        insecure_feature = self.extractor._web_presence(insecure)

        self.assertGreater(secure_feature.value, insecure_feature.value)
        self.assertFalse(insecure_feature.raw_value["has_https"])

    def test_web_presence_without_meaningful_content_stays_low(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content="Login",
        )

        feature = self.extractor._web_presence(web)

        self.assertLessEqual(feature.value, 35.0)
        self.assertEqual(feature.raw_value["page_status"], "minimal")

    def test_social_footprint_without_social_data_degrades_gracefully(self):
        feature = self.extractor._social_footprint(social=None)

        self.assertEqual(feature.value, 15.0)
        self.assertEqual(feature.confidence, 0.3)
        self.assertEqual(feature.raw_value["reason"], "no_social_data")

    def test_social_footprint_with_multiple_platforms_is_structured(self):
        social = SocialData(
            brand_name="Example",
            platforms={
                "linkedin": PlatformMetrics(
                    platform="linkedin",
                    profile_url="https://linkedin.com/company/example",
                    followers_count=12000,
                    verified=True,
                    last_post_date="2026-04-10",
                    posts_last_30_days=6,
                ),
                "instagram": PlatformMetrics(
                    platform="instagram",
                    profile_url="https://instagram.com/example",
                    followers_count=8000,
                    verified=False,
                    last_post_date="2026-04-12",
                    posts_last_30_days=8,
                ),
            },
            total_followers=20000,
            avg_post_frequency=3,
        )

        feature = self.extractor._social_footprint(social=social)

        self.assertGreaterEqual(feature.value, 55.0)
        self.assertEqual(feature.raw_value["total_followers"], 20000)
        self.assertEqual(feature.raw_value["active_platforms_count"], 2)
        self.assertTrue(feature.raw_value["professional_presence"])
        self.assertTrue(feature.raw_value["consumer_presence"])
        self.assertEqual(len(feature.raw_value["platforms"]), 2)

    def test_social_footprint_rewards_verified_accounts(self):
        unverified = SocialData(
            brand_name="Example",
            platforms={
                "linkedin": PlatformMetrics(
                    platform="linkedin",
                    profile_url="https://linkedin.com/company/example",
                    followers_count=12000,
                    verified=False,
                    last_post_date="2026-04-10",
                    posts_last_30_days=3,
                )
            },
            total_followers=12000,
            avg_post_frequency=2,
        )
        verified = SocialData(
            brand_name="Example",
            platforms={
                "linkedin": PlatformMetrics(
                    platform="linkedin",
                    profile_url="https://linkedin.com/company/example",
                    followers_count=12000,
                    verified=True,
                    last_post_date="2026-04-10",
                    posts_last_30_days=3,
                )
            },
            total_followers=12000,
            avg_post_frequency=2,
        )

        unverified_feature = self.extractor._social_footprint(social=unverified)
        verified_feature = self.extractor._social_footprint(social=verified)

        self.assertGreater(verified_feature.value, unverified_feature.value)
        self.assertTrue(verified_feature.raw_value["platforms"][0]["verified"])

    def test_search_visibility_without_results_returns_low_neutral(self):
        feature = self.extractor._search_visibility(exa=None)

        self.assertEqual(feature.value, 15.0)
        self.assertEqual(feature.raw_value["search_results_count"], 0)
        self.assertEqual(feature.raw_value["discoverability_results_count"], 0)
        self.assertEqual(feature.raw_value["owned_results_count"], 0)
        self.assertEqual(feature.raw_value["strategic_owned_results_count"], 0)
        self.assertEqual(feature.raw_value["support_owned_results_count"], 0)
        self.assertEqual(feature.raw_value["profile_results_count"], 0)
        self.assertEqual(feature.raw_value["independent_results_count"], 0)
        self.assertEqual(feature.raw_value["credible_ai_visibility_results_count"], 0)
        self.assertEqual(feature.raw_value["evidence"], [])

    def test_search_visibility_with_few_results_stays_mid_low(self):
        exa = ExaData(
            brand_name="Acme",
            mentions=[
                ExaResult(
                    url="https://acme.com/about",
                    title="Acme",
                    text="Acme builds software for teams.",
                    score=0.7,
                ),
                ExaResult(
                    url="https://news.example.com/acme",
                    title="Acme profile",
                    text="Acme is covered in a profile.",
                    score=0.5,
                ),
            ],
        )

        feature = self.extractor._search_visibility(exa)

        self.assertGreaterEqual(feature.value, 20.0)
        self.assertLess(feature.value, 50.0)
        self.assertEqual(feature.raw_value["relevant_results_count"], 2)
        self.assertEqual(feature.raw_value["discoverability_results_count"], 2)
        self.assertEqual(feature.raw_value["owned_results_count"], 1)
        self.assertEqual(feature.raw_value["strategic_owned_results_count"], 1)
        self.assertEqual(feature.raw_value["support_owned_results_count"], 0)
        self.assertEqual(feature.raw_value["profile_results_count"], 0)
        self.assertEqual(feature.raw_value["independent_results_count"], 1)

    def test_search_visibility_rewards_many_results_and_own_url_top3(self):
        exa = self._exa_mentions()
        exa.ai_visibility_results = [
            ExaResult(
                url="https://analysis.example.com/acme-overview",
                title="Acme overview",
                text="Acme is explained in detail with category and product context.",
                score=0.9,
                source_class="external",
                relation="external",
            )
        ]
        exa.mentions.extend(
            [
                ExaResult(
                    url=f"https://coverage{i}.example.com/acme",
                    title=f"Acme mention {i}",
                    text="Acme is the main subject of this article.",
                    score=0.7,
                )
                for i in range(6)
            ]
        )

        feature = self.extractor._search_visibility(exa)

        self.assertGreaterEqual(feature.value, 70.0)
        self.assertTrue(feature.raw_value["own_url_in_top3"])
        self.assertGreaterEqual(feature.raw_value["ai_visibility_signals"], 1)
        self.assertGreaterEqual(feature.raw_value["independent_results_count"], 7)
        self.assertEqual(feature.raw_value["credible_ai_visibility_results_count"], 1)
        self.assertEqual(len(feature.raw_value["evidence"]), 3)

    def test_search_visibility_filters_low_subject_relevance(self):
        exa = ExaData(
            brand_name="Acme",
            mentions=[
                ExaResult(
                    url="https://roundup.example.com",
                    title="General software roundup",
                    text="Many brands are discussed here without focusing on one.",
                    score=0.8,
                ),
                ExaResult(
                    url="https://acme.com",
                    title="Acme",
                    text="Acme is the main subject here.",
                    score=0.8,
                ),
            ],
        )

        feature = self.extractor._search_visibility(exa)

        self.assertEqual(feature.raw_value["search_results_count"], 2)
        self.assertEqual(feature.raw_value["relevant_results_count"], 1)
        self.assertEqual(feature.raw_value["discoverability_results_count"], 1)
        self.assertEqual(feature.raw_value["owned_results_count"], 1)
        self.assertEqual(feature.raw_value["strategic_owned_results_count"], 1)
        self.assertEqual(feature.raw_value["independent_results_count"], 0)
        self.assertEqual(len(feature.raw_value["evidence"]), 1)

    def test_search_visibility_ignores_domain_collision_results(self):
        exa = ExaData(
            brand_name="www.cofisolutions.com",
            mentions=[
                ExaResult(
                    url="https://www.coforge.com/news",
                    title="Coforge launches new platform",
                    text="Unrelated enterprise systems company.",
                    score=0.9,
                ),
                ExaResult(
                    url="https://www.cofisolutions.com/",
                    title="COFI Solutions",
                    text="Official site for COFI Solutions.",
                    score=0.9,
                ),
            ],
        )

        feature = self.extractor._search_visibility(exa)

        self.assertEqual(feature.raw_value["relevant_results_count"], 1)
        self.assertEqual(feature.raw_value["discoverability_results_count"], 1)
        self.assertEqual(feature.raw_value["owned_results_count"], 1)
        self.assertEqual(feature.raw_value["strategic_owned_results_count"], 1)
        self.assertEqual(feature.raw_value["profile_results_count"], 0)
        self.assertEqual(feature.raw_value["independent_results_count"], 0)
        self.assertEqual(feature.raw_value["evidence"][0]["url"], "https://www.cofisolutions.com/")

    def test_search_visibility_ignores_owned_and_directory_ai_visibility_noise(self):
        exa = ExaData(
            brand_name="www.cofisolutions.com",
            mentions=[
                ExaResult(
                    url="https://www.cofisolutions.com/",
                    title="COFI Solutions",
                    text="Official site for COFI Solutions.",
                    score=0.9,
                    source_class="owned",
                    relation="audited_surface",
                ),
                ExaResult(
                    url="https://press.example.com/cofisolutions",
                    title="COFI Solutions profile",
                    text="Independent profile about COFI Solutions.",
                    score=0.7,
                    source_class="external",
                    relation="external",
                ),
            ],
            ai_visibility_results=[
                ExaResult(
                    url="https://www.cofisolutions.com/contacto",
                    title="COFI Solutions contacto",
                    text="Owned page",
                    score=0.8,
                    source_class="owned",
                    relation="audited_surface",
                ),
                ExaResult(
                    url="https://linkedin.com/company/cofi-solutions",
                    title="COFI Solutions LinkedIn",
                    text="Directory-like company profile.",
                    score=0.8,
                    source_class="external",
                    relation="external",
                ),
                ExaResult(
                    url="https://einforma.com/informacion-empresa/cofi-solutions",
                    title="COFI SOLUTIONS, S.L.",
                    text="Business directory listing.",
                    score=0.8,
                    source_class="external",
                    relation="external",
                ),
            ],
        )

        feature = self.extractor._search_visibility(exa)

        self.assertEqual(feature.raw_value["ai_visibility_results_count"], 3)
        self.assertEqual(feature.raw_value["credible_ai_visibility_results_count"], 0)
        self.assertEqual(feature.raw_value["ai_visibility_signals"], 0)

    def test_search_visibility_separates_profile_results_from_independent_results(self):
        exa = ExaData(
            brand_name="www.cofisolutions.com",
            mentions=[
                ExaResult(
                    url="https://www.cofisolutions.com/",
                    title="COFI Solutions",
                    text="Official site for COFI Solutions.",
                    score=0.9,
                    source_class="owned",
                    relation="audited_surface",
                ),
                ExaResult(
                    url="https://press.example.com/cofi-solutions-analysis",
                    title="COFI Solutions analysis",
                    text="Independent article explaining the company and its market focus.",
                    score=0.7,
                    source_class="external",
                    relation="external",
                ),
            ],
            profiles=[
                ExaResult(
                    url="https://www.einforma.com/informacion-empresa/cofi-solutions",
                    title="COFI SOLUTIONS, S.L.",
                    text="Business directory listing.",
                    score=0.8,
                    source_class="external",
                    relation="external",
                ),
            ],
        )

        feature = self.extractor._search_visibility(exa)

        self.assertEqual(feature.raw_value["relevant_results_count"], 2)
        self.assertEqual(feature.raw_value["discoverability_results_count"], 2)
        self.assertEqual(feature.raw_value["owned_results_count"], 1)
        self.assertEqual(feature.raw_value["strategic_owned_results_count"], 1)
        self.assertEqual(feature.raw_value["support_owned_results_count"], 0)
        self.assertEqual(feature.raw_value["profile_results_count"], 1)
        self.assertEqual(feature.raw_value["independent_results_count"], 1)

    def test_search_visibility_penalizes_support_owned_surfaces_vs_strategic_owned(self):
        strategic = ExaData(
            brand_name="www.cofisolutions.com",
            mentions=[
                ExaResult(url="https://www.cofisolutions.com/", title="COFI Solutions", text="Homepage"),
                ExaResult(url="https://www.cofisolutions.com/about", title="About COFI Solutions", text="About"),
                ExaResult(url="https://www.cofisolutions.com/solutions", title="Solutions", text="Solutions"),
                ExaResult(url="https://press.example.com/cofi-solutions-analysis", title="Analysis", text="Independent article"),
            ],
        )
        support = ExaData(
            brand_name="www.cofisolutions.com",
            mentions=[
                ExaResult(url="https://www.cofisolutions.com/", title="COFI Solutions", text="Homepage"),
                ExaResult(url="https://www.cofisolutions.com/contacto", title="COFI Solutions contacto", text="Contact"),
                ExaResult(url="https://www.cofisolutions.com/privacidad-y-datos", title="COFI Solutions privacidad", text="Privacy"),
                ExaResult(url="https://press.example.com/cofi-solutions-analysis", title="Analysis", text="Independent article"),
            ],
        )

        strategic_feature = self.extractor._search_visibility(strategic)
        support_feature = self.extractor._search_visibility(support)

        self.assertGreater(strategic_feature.value, support_feature.value)
        self.assertEqual(strategic_feature.raw_value["strategic_owned_results_count"], 3)
        self.assertEqual(support_feature.raw_value["support_owned_results_count"], 2)
        self.assertEqual(support_feature.raw_value["discoverability_results_count"], 2)

    def test_directory_presence_without_directories_is_zero(self):
        exa = ExaData(
            brand_name="Acme",
            mentions=[ExaResult(url="https://acme.com", title="Acme", text="Owned site")],
        )

        feature = self.extractor._directory_presence(exa)

        self.assertEqual(feature.value, 0.0)
        self.assertEqual(feature.raw_value["total_points"], 0)

    def test_directory_presence_with_only_tier2_is_limited(self):
        exa = ExaData(
            brand_name="Acme",
            mentions=[
                ExaResult(
                    url="https://producthunt.com/posts/acme",
                    title="Acme on Product Hunt",
                    text="Listing",
                ),
                ExaResult(
                    url="https://trustpilot.com/review/acme.com",
                    title="Acme reviews",
                    text="Review listing",
                ),
            ],
        )

        feature = self.extractor._directory_presence(exa)

        self.assertEqual(feature.value, 16.0)
        self.assertEqual(len(feature.raw_value["tier2_found"]), 2)
        self.assertEqual(feature.raw_value["tier1_found"], [])

    def test_directory_presence_with_tier1_and_tier2_mix_scores_higher(self):
        exa = ExaData(
            brand_name="Acme",
            mentions=[
                ExaResult(url="https://crunchbase.com/organization/acme", title="Crunchbase", text="Listing"),
                ExaResult(url="https://linkedin.com/company/acme", title="LinkedIn", text="Listing"),
                ExaResult(url="https://producthunt.com/posts/acme", title="Product Hunt", text="Listing"),
            ],
        )

        feature = self.extractor._directory_presence(exa)

        self.assertEqual(feature.value, 48.0)
        self.assertEqual(len(feature.raw_value["tier1_found"]), 2)
        self.assertEqual(len(feature.raw_value["tier2_found"]), 1)


class VitalidadExtractorTests(unittest.TestCase):
    """Cover the 3 features (content_recency, publication_cadence, momentum)."""

    def setUp(self):
        self.extractor = VitalidadExtractor()

    # ── content_recency ────────────────────────────────────────────────

    def _exa_with_dates(self, days_ago_list: list[int]) -> ExaData:
        from datetime import datetime, timedelta
        base = datetime.now()
        mentions = [
            ExaResult(
                url=f"https://test.example.com/test/article-{i}",
                title=f"Test article {i}",
                text="Test content",
                published_date=(base - timedelta(days=d)).strftime("%Y-%m-%d"),
            )
            for i, d in enumerate(days_ago_list)
        ]
        return ExaData(brand_name="Test", mentions=mentions)

    def test_content_recency_recent_publication_scores_high(self):
        exa = self._exa_with_dates([3])
        features = self.extractor.extract(exa=exa)
        self.assertEqual(features["content_recency"].value, 100.0)
        self.assertEqual(features["content_recency"].raw_value["evidence_snippet"], "Test content")

    def test_content_recency_30_days_is_mid_high(self):
        exa = self._exa_with_dates([25])
        features = self.extractor.extract(exa=exa)
        self.assertEqual(features["content_recency"].value, 85.0)

    def test_content_recency_6_months_drops_to_mid(self):
        exa = self._exa_with_dates([150])
        features = self.extractor.extract(exa=exa)
        self.assertEqual(features["content_recency"].value, 40.0)

    def test_content_recency_past_year_is_low(self):
        exa = self._exa_with_dates([250])
        features = self.extractor.extract(exa=exa)
        self.assertEqual(features["content_recency"].value, 20.0)

    def test_content_recency_over_365_days_is_very_low(self):
        exa = self._exa_with_dates([400])
        features = self.extractor.extract(exa=exa)
        self.assertEqual(features["content_recency"].value, 10.0)

    def test_content_recency_no_dates_returns_neutral_with_reason(self):
        import json
        features = self.extractor.extract(exa=None)
        fv = features["content_recency"]
        self.assertEqual(fv.value, 30.0)
        self.assertEqual(fv.source, "none")
        payload = fv.raw_value
        self.assertIsNone(payload["most_recent_date"])
        self.assertIsNone(payload["days_ago"])
        self.assertIsNone(payload["evidence_url"])
        self.assertEqual(payload["reason"], "no_dates_found")

    # ── publication_cadence ────────────────────────────────────────────

    def test_dated_mentions_without_content_are_not_used_as_evidence(self):
        from datetime import datetime, timedelta

        exa = ExaData(
            brand_name="Test",
            mentions=[
                ExaResult(
                    url="https://example.com/title-only",
                    title="Title alone",
                    text="",
                    summary="",
                    highlights=[],
                    published_date=(datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"),
                )
            ],
        )

        features = self.extractor.extract(exa=exa)

        self.assertEqual(features["content_recency"].source, "none")
        self.assertEqual(features["content_recency"].raw_value["reason"], "no_dates_found")

    def test_dated_mentions_with_placeholder_content_are_not_used_as_evidence(self):
        from datetime import datetime, timedelta

        exa = ExaData(
            brand_name="Test",
            mentions=[
                ExaResult(
                    url="https://example.com/placeholder",
                    title="Placeholder",
                    text="-",
                    summary="",
                    highlights=[],
                    published_date=(datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"),
                )
            ],
        )

        features = self.extractor.extract(exa=exa)

        self.assertEqual(features["content_recency"].source, "none")
        self.assertEqual(features["content_recency"].raw_value["reason"], "no_dates_found")

    def test_publication_cadence_fewer_than_2_dates_is_low(self):
        import json
        exa = self._exa_with_dates([15])
        features = self.extractor.extract(exa=exa)
        fv = features["publication_cadence"]
        self.assertEqual(fv.value, 20.0)
        payload = fv.raw_value
        self.assertEqual(payload["reason"], "insufficient_dates_12m")
        self.assertEqual(payload["evidence"][0]["snippet"], "Test content")

    def test_publication_cadence_regular_rhythm_scores_high(self):
        # 3 dates roughly ~20 days apart → mean_gap < 30 → 90
        exa = self._exa_with_dates([10, 35, 60])
        features = self.extractor.extract(exa=exa)
        self.assertEqual(features["publication_cadence"].value, 90.0)
        self.assertTrue(all(item["snippet"] == "Test content" for item in features["publication_cadence"].raw_value["evidence"]))

    def test_publication_cadence_moderate_rhythm_scores_mid(self):
        # 3 dates ~100 days apart → 90 <= mean < 180 → 50
        exa = self._exa_with_dates([5, 110, 215])
        features = self.extractor.extract(exa=exa)
        self.assertEqual(features["publication_cadence"].value, 50.0)

    def test_publication_cadence_ignores_irrelevant_collision_news(self):
        exa = ExaData(
            brand_name="www.cofisolutions.com",
            mentions=[
                ExaResult(
                    url="https://www.cofisolutions.com/blog/a",
                    title="COFI Solutions article A",
                    text="content",
                    published_date="2026-01-01",
                    source_class="owned",
                    relation="audited_surface",
                ),
                ExaResult(
                    url="https://www.cofisolutions.com/blog/b",
                    title="COFI Solutions article B",
                    text="content",
                    published_date="2026-03-01",
                    source_class="owned",
                    relation="audited_surface",
                ),
            ],
            news=[
                ExaResult(
                    url="https://news.coforge.com/story",
                    title="Coforge partnership",
                    text="Unrelated external news",
                    published_date="2026-06-01",
                    source_class="external",
                    relation="external",
                )
            ],
        )

        features = self.extractor.extract(exa=exa)
        evidence_urls = [item["url"] for item in features["publication_cadence"].raw_value["evidence"]]
        self.assertNotIn("https://news.coforge.com/story", evidence_urls)
        self.assertIn("https://www.cofisolutions.com/blog/a", evidence_urls)
        self.assertIn("https://www.cofisolutions.com/blog/b", evidence_urls)

    # ── momentum ───────────────────────────────────────────────────────

    def test_momentum_without_llm_returns_heuristic_fallback(self):
        import json
        exa = self._exa_with_dates([30, 60])
        features = self.extractor.extract(exa=exa)
        fv = features["momentum"]
        self.assertEqual(fv.value, 50.0)
        self.assertEqual(fv.source, "heuristic_fallback")
        self.assertEqual(fv.confidence, 0.3)
        payload = fv.raw_value
        self.assertEqual(payload["reason"], "llm_unavailable")

    def test_momentum_with_llm_uses_structured_verdict(self):
        import json

        class FakeLLM:
            api_key = "sk-test"

            def analyze_momentum(self, mentions, brand_name):
                return {
                    "momentum_score": 82,
                    "verdict": "building",
                    "evidence": [
                        {
                            "quote": "shipped a new inference runtime",
                            "source_url": "https://example.com/article-0",
                            "signal": "positive",
                        }
                    ],
                    "reasoning": "Fresh product launches in the last quarter.",
                }

        extractor = VitalidadExtractor(llm=FakeLLM())
        exa = self._exa_with_dates([10, 40])
        features = extractor.extract(exa=exa)

        fv = features["momentum"]
        self.assertEqual(fv.value, 82.0)
        self.assertEqual(fv.source, "llm")
        self.assertEqual(fv.confidence, 0.85)
        payload = fv.raw_value
        self.assertEqual(payload["verdict"], "building")
        self.assertEqual(len(payload["evidence"]), 1)
        self.assertIn("shipped a new inference runtime", payload["evidence"][0]["quote"])

    def test_momentum_with_unclear_verdict_has_lower_confidence(self):
        import json

        class FakeLLM:
            api_key = "sk-test"

            def analyze_momentum(self, mentions, brand_name):
                return {
                    "momentum_score": 50,
                    "verdict": "unclear",
                    "evidence": [],
                    "reasoning": "Evidence insufficient.",
                }

        extractor = VitalidadExtractor(llm=FakeLLM())
        exa = self._exa_with_dates([20])
        features = extractor.extract(exa=exa)
        fv = features["momentum"]
        self.assertEqual(fv.confidence, 0.5)
        self.assertEqual(fv.raw_value["verdict"], "unclear")

    def test_momentum_with_no_recent_mentions_returns_fallback(self):
        import json

        class FakeLLM:
            api_key = "sk-test"

            def analyze_momentum(self, mentions, brand_name):
                raise AssertionError("should not be called when no recent mentions")

        extractor = VitalidadExtractor(llm=FakeLLM())
        # Only old mentions, outside the 6-month window
        exa = self._exa_with_dates([400, 500])
        features = extractor.extract(exa=exa)
        fv = features["momentum"]
        self.assertEqual(fv.source, "heuristic_fallback")
        self.assertEqual(fv.raw_value["reason"], "no_recent_mentions_6m")

    def test_extract_always_returns_three_features(self):
        features = self.extractor.extract(web=None, exa=None)
        self.assertEqual(
            set(features.keys()),
            {"content_recency", "publication_cadence", "momentum"},
        )

    # ── momentum — LLM contract negative tests ─────────────────────────

    def _make_momentum_llm(self, payload):
        """Helper: fake LLMAnalyzer that returns a fixed payload from analyze_momentum."""

        class FakeLLM:
            api_key = "sk-test"

            def analyze_momentum(self, mentions, brand_name):
                return payload

        return FakeLLM()

    def test_momentum_with_invalid_verdict_falls_back(self):
        import json

        llm = self._make_momentum_llm({
            "momentum_score": 80,
            "verdict": "thriving",  # not in the enum
            "evidence": [
                {
                    "quote": "shipped v2",
                    "source_url": "https://example.com/article-0",
                    "signal": "positive",
                }
            ],
            "reasoning": "Good stuff.",
        })
        extractor = VitalidadExtractor(llm=llm)
        features = extractor.extract(exa=self._exa_with_dates([10]))
        fv = features["momentum"]
        self.assertEqual(fv.source, "heuristic_fallback")
        self.assertEqual(fv.value, 50.0)
        payload = fv.raw_value
        self.assertEqual(payload["reason"], "llm_invalid_verdict")
        self.assertEqual(payload["got"], "thriving")

    def test_momentum_with_non_list_evidence_degrades_confidence(self):
        import json

        llm = self._make_momentum_llm({
            "momentum_score": 72,
            "verdict": "building",
            "evidence": "they shipped a lot",  # should be list
            "reasoning": "Clearly active.",
        })
        extractor = VitalidadExtractor(llm=llm)
        features = extractor.extract(exa=self._exa_with_dates([10]))
        fv = features["momentum"]
        # Still source=llm because verdict is valid, but degraded confidence.
        self.assertEqual(fv.source, "llm")
        self.assertEqual(fv.value, 72.0)
        self.assertEqual(fv.confidence, 0.5)
        payload = fv.raw_value
        self.assertEqual(payload["reason"], "llm_partial_evidence")
        self.assertEqual(payload["evidence"], [])

    def test_momentum_with_malformed_evidence_items_are_filtered(self):
        import json

        llm = self._make_momentum_llm({
            "momentum_score": 82,
            "verdict": "building",
            "evidence": [
                # Missing signal → dropped.
                {"quote": "launched new runtime", "source_url": "https://x.com/1"},
                # Wrong signal value → dropped.
                {"quote": "grew 3x", "source_url": "https://x.com/2", "signal": "bullish"},
                # Non-str quote → dropped.
                {"quote": 123, "source_url": "https://x.com/3", "signal": "positive"},
                # Valid → kept.
                {"quote": "hired 40 engineers",
                 "source_url": "https://x.com/4",
                 "signal": "positive"},
                # Non-dict → dropped.
                "random string",
            ],
            "reasoning": "Mix.",
        })
        extractor = VitalidadExtractor(llm=llm)
        features = extractor.extract(exa=self._exa_with_dates([10]))
        fv = features["momentum"]
        self.assertEqual(fv.source, "llm")
        self.assertEqual(fv.confidence, 0.5)
        payload = fv.raw_value
        self.assertEqual(len(payload["evidence"]), 1)
        self.assertEqual(payload["evidence"][0]["quote"], "hired 40 engineers")
        self.assertEqual(payload["reason"], "llm_partial_evidence")

    def test_momentum_with_all_evidence_items_malformed_flags_partial(self):
        import json

        llm = self._make_momentum_llm({
            "momentum_score": 64,
            "verdict": "maintaining",
            "evidence": [
                {"quote": "something"},  # missing fields → dropped
                "not a dict",
            ],
            "reasoning": "Little signal.",
        })
        extractor = VitalidadExtractor(llm=llm)
        features = extractor.extract(exa=self._exa_with_dates([10]))
        fv = features["momentum"]
        self.assertEqual(fv.source, "llm")
        self.assertEqual(fv.value, 64.0)
        self.assertEqual(fv.confidence, 0.5)
        payload = fv.raw_value
        self.assertEqual(payload["reason"], "llm_partial_evidence")
        self.assertEqual(payload["evidence"], [])


class WebCollectorTests(unittest.TestCase):
    def test_clean_markdown_removes_cookie_banner_noise(self):
        collector = WebCollector()
        raw = """
![Revisit consent button](https://example.com/revisit.svg)
We value your privacy
Accept All
Reject All
Customise

# Prior Labs

Tabular foundation models for real-world data.
"""
        cleaned = collector._clean_markdown_content(raw)

        self.assertNotIn("We value your privacy", cleaned)
        self.assertNotIn("Accept All", cleaned)
        self.assertIn("# Prior Labs", cleaned)
        self.assertIn("Tabular foundation models", cleaned)

    def test_clean_markdown_trims_leading_ui_preamble(self):
        collector = WebCollector()
        raw = """
NecessaryAlways Active

Functional

Analytics

[Deploy now](https://example.com/deploy)

One Model, Infinite Predictions

Tabular foundation models for real-world data.
"""

        cleaned = collector._clean_markdown_content(raw)

        self.assertTrue(cleaned.startswith("One Model, Infinite Predictions"))
        self.assertNotIn("NecessaryAlways Active", cleaned)
        self.assertNotIn("Functional", cleaned)

    def test_clean_markdown_discards_firecrawl_auth_prompt(self):
        collector = WebCollector()
        raw = """
Turn websites into LLM-ready data

Welcome! To get started, authenticate with your Firecrawl account.

1. Login with browser
2. Enter API key manually
"""

        cleaned = collector._clean_markdown_content(raw)

        self.assertEqual(cleaned, "")

    def test_trim_to_title_drops_navigation_before_detected_title(self):
        collector = WebCollector()
        content = """
Models

Deployment

One Model, Infinite Predictions

Tabular foundation models for real-world data.
"""

        trimmed = collector._trim_to_title(content.strip(), "One Model, Infinite Predictions")

        self.assertTrue(trimmed.startswith("One Model, Infinite Predictions"))
        self.assertNotIn("Models", trimmed)

    def test_html_to_markdown_fallback_extracts_title_meta_and_body(self):
        collector = WebCollector()
        html = """
<html>
  <head>
    <title>CTGT</title>
    <meta name="description" content="Deterministic intelligence control for frontier AI." />
  </head>
  <body>
    <nav>Home Pricing Docs</nav>
    <h1>Deterministic control for frontier AI</h1>
    <p>Runtime policy enforcement, steering, and audit trails for production systems.</p>
    <script>console.log("ignore me")</script>
  </body>
</html>
"""

        content = collector._html_to_markdown_fallback(html)

        self.assertIn("# CTGT", content)
        self.assertIn("Deterministic intelligence control for frontier AI.", content)
        self.assertIn("Runtime policy enforcement, steering, and audit trails", content)
        self.assertNotIn("ignore me", content)

    def test_html_to_markdown_fallback_recovers_div_only_copy(self):
        collector = WebCollector()
        filler = "".join(
            f"<p>Structured paragraph number {i} with enough length to count.</p>" for i in range(3)
        )
        html = f"""
<html>
  <head><title>Mercury Jobs</title></head>
  <body>
    {filler}
    <div class="values-section">
      <div>Our values. Your strengths.</div>
      <div>We put our values into practice every day across the company.</div>
    </div>
    <script>window.__NEXT_DATA__ = {{"ignored": true}}</script>
  </body>
</html>
"""

        content = collector._html_to_markdown_fallback(html)

        self.assertIn("Our values. Your strengths.", content)
        self.assertIn("values into practice", content)
        self.assertNotIn("__NEXT_DATA__", content)

    def test_extract_canonical_metadata_captures_alternate_domains(self):
        collector = WebCollector()
        html = """
<html>
  <head>
    <link rel="canonical" href="https://movements.dev/en" />
    <link rel="alternate" href="https://movements.dev/es" hreflang="es" />
    <meta property="og:url" content="https://movements.dev/en" />
    <script type="application/ld+json">{"url":"https://movements.dev/en/search?q=test"}</script>
  </head>
</html>
"""

        canonical_url, alternate_domains = collector._extract_canonical_metadata(html)

        self.assertEqual(canonical_url, "https://movements.dev/en")
        self.assertIn("movements.dev", alternate_domains)

    def test_scrape_uses_html_fallback_when_firecrawl_is_empty(self):
        collector = WebCollector()
        html = """
<html>
  <head>
    <title>Poetiq</title>
    <meta name="description" content="The fastest path to safe super intelligence." />
  </head>
  <body>
    <h1>The fastest path to safe super intelligence</h1>
    <p>Better reasoning systems for aligned advanced AI. Better reasoning systems for aligned advanced AI.
    Better reasoning systems for aligned advanced AI. Better reasoning systems for aligned advanced AI.
    Better reasoning systems for aligned advanced AI.</p>
  </body>
</html>
"""

        with patch.object(WebCollector, "_run_firecrawl", return_value={"content": ""}):
            with patch.object(WebCollector, "_fetch_html_fallback", return_value=(html, "")):
                with patch.object(WebCollector, "_fetch_browser_fallback") as browser_fallback:
                    data = collector.scrape(
                        "https://poetiq.ai/",
                        crawl_subpages=False,
                    )

        self.assertEqual(data.title, "Poetiq")
        self.assertIn("safe super intelligence", data.markdown_content.lower())
        self.assertEqual(data.meta_description, "The fastest path to safe super intelligence.")
        self.assertEqual(data.error, "")
        browser_fallback.assert_not_called()

    def test_scrape_uses_browser_fallback_when_firecrawl_and_html_are_unusable(self):
        collector = WebCollector()
        browser_text = (
            "Meet Claude. Claude is an AI assistant for problem solving, coding, writing, "
            "analysis, enterprise workflows, team collaboration, and safe deployment. "
            "Choose Pro, Team, or Enterprise plans for advanced usage. "
        ) * 4
        payload = {
            "status": 200,
            "title": "Claude",
            "meta_description": "Claude is an AI assistant from Anthropic.",
            "canonical_url": "https://claude.ai/",
            "body_text": browser_text,
            "html": "<html><body>Claude</body></html>",
            "links": ["https://claude.ai/pricing", "https://claude.ai/security"],
        }

        with patch.object(WebCollector, "_run_firecrawl", return_value={"error": "blocked"}):
            with patch.object(WebCollector, "_fetch_html_fallback", return_value=("", "403")):
                with patch.object(WebCollector, "_fetch_browser_fallback", return_value=(payload, "")):
                    data = collector.scrape(
                        "https://claude.ai/",
                        crawl_subpages=False,
                    )

        self.assertEqual(data.title, "Claude")
        self.assertEqual(data.content_source, "browser_fallback")
        self.assertEqual(data.browser_status, 200)
        self.assertEqual(data.canonical_url, "https://claude.ai/")
        self.assertIn("Claude is an AI assistant", data.markdown_content)
        self.assertIn("https://claude.ai/pricing", data.links)
        self.assertEqual(data.error, "")

    def test_scrape_does_not_crash_when_browser_fallback_is_unavailable(self):
        collector = WebCollector()

        with patch.object(WebCollector, "_run_firecrawl", return_value={"error": "blocked"}):
            with patch.object(WebCollector, "_fetch_html_fallback", return_value=("", "403")):
                with patch.object(WebCollector, "_fetch_browser_fallback", return_value=({}, "playwright unavailable")):
                    data = collector.scrape("https://blocked.example/")

        self.assertEqual(data.markdown_content, "")
        self.assertEqual(data.content_source, "")
        self.assertIn(data.error, {"blocked", "playwright unavailable"})

    def test_scrape_does_not_call_browser_when_firecrawl_is_usable(self):
        collector = WebCollector()
        content = "# Claude\n\nClaude helps people solve problems with AI. " * 8
        html = "<html><body><main>Claude helps people solve problems with AI.</main></body></html>"

        with patch.object(WebCollector, "_run_firecrawl", return_value={"content": content, "html": html}):
            with patch.object(WebCollector, "_fetch_html_fallback") as html_fallback:
                with patch.object(WebCollector, "_fetch_browser_fallback") as browser_fallback:
                    data = collector.scrape(
                        "https://claude.ai/",
                        crawl_subpages=False,
                    )

        self.assertIn("Claude helps people solve problems", data.markdown_content)
        self.assertEqual(data.html, html)
        html_fallback.assert_not_called()
        browser_fallback.assert_not_called()

    def test_claude_like_browser_text_is_not_treated_as_cookie_banner(self):
        collector = WebCollector()
        content = collector._body_text_to_markdown(
            (
                "Claude\n"
                "Claude is an AI assistant for coding, writing, analysis, and business workflows.\n"
                "Explore Pro, Team, Enterprise, pricing, security, docs, and support for Claude.\n"
            ) * 5,
            title="Claude",
            meta_description="Claude is an AI assistant from Anthropic.",
        )

        self.assertGreaterEqual(len(content), 200)
        self.assertFalse(collector._looks_like_cookie_banner("Claude", content))

    def test_extract_internal_links(self):
        collector = WebCollector()
        markdown = (
            "Check our [Pricing](https://example.com/pricing), [About](https://example.com/about), "
            "and [External link](https://external.com/blog). Also, [relative links](/features) "
            "and [nested relative link](platform/solutions)."
        )
        links = collector._extract_internal_links(markdown, "https://example.com")
        self.assertIn("https://example.com/pricing", links)
        self.assertIn("https://example.com/about", links)
        self.assertIn("https://example.com/features", links)
        self.assertIn("https://example.com/platform/solutions", links)
        self.assertNotIn("https://external.com/blog", links)

    def test_extract_internal_links_encodes_spaces(self):
        collector = WebCollector()
        markdown = "See [Modelo de negocio](/Modelo de negocio/03_01_01_Modelo de Negocio_beCAUCE.html)."
        links = collector._extract_internal_links(markdown, "https://example.com")
        self.assertIn(
            "https://example.com/Modelo%20de%20negocio/03_01_01_Modelo%20de%20Negocio_beCAUCE.html",
            links,
        )

    def test_score_internal_links(self):
        collector = WebCollector()
        links = [
            "https://example.com/privacy-policy",
            "https://example.com/pricing",
            "https://example.com/about-us",
            "https://example.com/blog/2026/05/19",
            "https://example.com/features/dashboard",
        ]
        sorted_links = collector._score_internal_links(links, "https://example.com")
        self.assertEqual(sorted_links[0], "https://example.com/pricing")
        self.assertEqual(sorted_links[1], "https://example.com/features/dashboard")
        self.assertEqual(sorted_links[2], "https://example.com/about-us")
        self.assertNotIn("https://example.com/privacy-policy", sorted_links)

    def test_select_internal_links_to_crawl_balances_page_roles(self):
        collector = WebCollector()
        links = [
            "https://example.com/pricing",
            "https://example.com/features/dashboard",
            "https://example.com/features/ai-video",
            "https://example.com/about-us",
            "https://example.com/customers",
            "https://example.com/testimonials",
            "https://example.com/privacy-policy",
        ]

        selected = collector._select_internal_links_to_crawl(links, "https://example.com")

        self.assertEqual(
            selected,
            [
                "https://example.com/features/dashboard",
                "https://example.com/about-us",
                "https://example.com/customers",
                "https://example.com/testimonials",
                "https://example.com/pricing",
                "https://example.com/features/ai-video",
            ],
        )
        self.assertNotIn("https://example.com/privacy-policy", selected)

    def test_select_internal_links_to_crawl_prefers_manifesto_over_company_page(self):
        collector = WebCollector()
        links = [
            "https://example.com/use-cases",
            "https://example.com/company",
            "https://example.com/blog/manifesto",
            "https://example.com/customers",
            "https://example.com/security",
        ]

        selected = collector._select_internal_links_to_crawl(links, "https://example.com")

        self.assertIn("https://example.com/blog/manifesto", selected)
        self.assertLess(
            selected.index("https://example.com/blog/manifesto"),
            selected.index("https://example.com/company"),
        )
        self.assertLessEqual(len(selected), 6)

    def test_select_internal_links_to_crawl_recognizes_spanish_proof_pages(self):
        collector = WebCollector()
        links = [
            "https://example.com/es/precios",
            "https://example.com/es/producto",
            "https://example.com/es/nosotros",
            "https://example.com/es/testimonios",
            "https://example.com/es/resenas",
        ]

        selected = collector._select_internal_links_to_crawl(links, "https://example.com/es/")

        self.assertIn("https://example.com/es/testimonios", selected)
        self.assertIn("https://example.com/es/resenas", selected)
        self.assertLessEqual(len(selected), 6)

    def test_select_internal_links_to_crawl_guarantees_culture_page_slot(self):
        collector = WebCollector()
        links = [
            "https://example.com/features/dashboard",
            "https://example.com/features/ai-video",
            "https://example.com/solutions/enterprise",
            "https://example.com/about-us",
            "https://example.com/customers",
            "https://example.com/case-studies",
            "https://example.com/jobs",
        ]

        selected = collector._select_internal_links_to_crawl(links, "https://example.com")

        self.assertIn("https://example.com/jobs", selected)

    def test_select_internal_links_to_crawl_prefers_values_page_over_careers(self):
        collector = WebCollector()
        links = [
            "https://example.com/careers",
            "https://example.com/company/values",
            "https://example.com/features/dashboard",
        ]

        selected = collector._select_internal_links_to_crawl(links, "https://example.com")

        self.assertIn("https://example.com/company/values", selected)
        self.assertLess(
            selected.index("https://example.com/company/values"),
            selected.index("https://example.com/careers"),
        )

    def test_select_internal_links_recognizes_spanish_culture_pages(self):
        collector = WebCollector()
        links = [
            "https://example.com/es/producto",
            "https://example.com/es/cultura",
        ]

        selected = collector._select_internal_links_to_crawl(links, "https://example.com/es/")

        self.assertIn("https://example.com/es/cultura", selected)


    def test_scrape_recursive_crawling(self):
        from unittest.mock import patch
        collector = WebCollector(sitemap_discovery=False)
        
        main_content = (
            "# Main Page\n\nWelcome to our company page. "
            "Learn more at [pricing](https://example.com/pricing) "
            "or [about nosotros](https://example.com/about). " * 10
        )
        pricing_content = "# Pricing Page\n\nOur plans start at $10/mo." * 10
        about_content = "# About Us Page\n\nWe are a premium team." * 10
        
        def mock_run_firecrawl(url):
            if url == "https://example.com":
                return {"content": main_content, "html": "<html></html>"}
            elif url == "https://example.com/pricing":
                return {"content": pricing_content, "html": "<html></html>"}
            elif url == "https://example.com/about":
                return {"content": about_content, "html": "<html></html>"}
            return {"error": "not found"}
            
        with patch.object(WebCollector, "_run_firecrawl", side_effect=mock_run_firecrawl):
            data = collector.scrape("https://example.com", crawl_subpages=True)
            self.assertIn("Subpage: https://example.com/pricing", data.markdown_content)
            self.assertIn("Our plans start at $10/mo", data.markdown_content)
            self.assertIn("Subpage: https://example.com/about", data.markdown_content)
            self.assertIn("We are a premium team", data.markdown_content)
            self.assertEqual(
                data.owned_fallback_urls,
                ["https://example.com/about", "https://example.com/pricing"],
            )
            
            data_no_crawl = collector.scrape("https://example.com", crawl_subpages=False)
            self.assertNotIn("Subpage: https://example.com/pricing", data_no_crawl.markdown_content)

    def test_scrape_recursive_crawling_uses_html_links_when_markdown_has_none(self):
        from unittest.mock import patch
        collector = WebCollector(sitemap_discovery=False)

        main_content = "# Main Page\n\nPlain homepage copy without markdown links. " * 10
        main_html = """
        <html><body>
          <a href="/product">Product</a>
          <a href="/about">About</a>
          <a href="/assets/logo.svg">Logo</a>
        </body></html>
        """
        product_content = "# Product Page\n\nProduct platform evidence." * 10
        about_content = "# About Page\n\nCompany story evidence." * 10

        def mock_run_firecrawl(url):
            if url == "https://example.com":
                return {"content": main_content, "html": main_html}
            if url == "https://example.com/product":
                return {"content": product_content, "html": "<html></html>"}
            if url == "https://example.com/about":
                return {"content": about_content, "html": "<html></html>"}
            return {"error": "not found"}

        with patch.object(WebCollector, "_run_firecrawl", side_effect=mock_run_firecrawl):
            data = collector.scrape("https://example.com", crawl_subpages=True)

        self.assertIn("Subpage: https://example.com/product", data.markdown_content)
        self.assertIn("Product platform evidence", data.markdown_content)
        self.assertIn("Subpage: https://example.com/about", data.markdown_content)
        self.assertNotIn("assets/logo.svg", data.markdown_content)
        self.assertEqual(
            data.owned_fallback_urls,
            ["https://example.com/product", "https://example.com/about"],
        )

    def test_scrape_recursive_crawling_handles_internal_links_with_spaces(self):
        from unittest.mock import patch

        collector = WebCollector(sitemap_discovery=False)

        main_content = (
            "# Main Page\n\n"
            "See the [business model](/Modelo de negocio/03_01_01_Modelo de Negocio_beCAUCE.html).\n" * 6
        )
        encoded_subpage = "https://example.com/Modelo%20de%20negocio/03_01_01_Modelo%20de%20Negocio_beCAUCE.html"
        subpage_content = "# Modelo de negocio\n\nTexto de prueba." * 8

        def mock_run_firecrawl(url):
            if url == "https://example.com":
                return {"content": main_content, "html": "<html></html>"}
            if url == encoded_subpage:
                return {"content": subpage_content, "html": "<html></html>"}
            return {"error": f"unexpected url: {url}"}

        with patch.object(WebCollector, "_run_firecrawl", side_effect=mock_run_firecrawl):
            data = collector.scrape("https://example.com", crawl_subpages=True)

        self.assertIn("Subpage: https://example.com/Modelo%20de%20negocio/03_01_01_Modelo%20de%20Negocio_beCAUCE.html", data.markdown_content)
        self.assertIn("Texto de prueba", data.markdown_content)


    def test_dismiss_cookie_banners_playwright(self):
        from unittest.mock import MagicMock
        collector = WebCollector()
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_page.locator.return_value = mock_locator
        
        collector._dismiss_cookie_banners(mock_page)
        
        mock_page.locator.assert_called_once()
        mock_locator.first.click.assert_called_once_with(timeout=500)


class ExaCollectorTests(unittest.TestCase):
    def test_brand_query_includes_domain_anchor_when_available(self):
        collector = ExaCollector(api_key="test")

        query = collector._brand_query("Movements", "https://movements.mov/en", "news")

        self.assertIn('"Movements"', query)
        self.assertIn('"movements.mov"', query)
        self.assertTrue(query.endswith("news"))

    def test_brand_query_works_without_domain(self):
        collector = ExaCollector(api_key="test")

        query = collector._brand_query("CTGT", None, "brand company")

        self.assertEqual(query, '"CTGT" brand company')


class CoherenciaExtractorTests(unittest.TestCase):
    """Covers the 4 coherencia features with dict raw_value."""

    def test_visual_consistency_uses_provided_screenshot_without_recapturing(self):
        class FakeVisualAnalyzer:
            def __init__(self):
                self.screenshot_calls = []
                self.analyze_url_calls = []
                self.take_screenshot_calls = []

            def analyze_screenshot(self, screenshot_url, brand_name="", page_metadata=None):
                self.screenshot_calls.append((screenshot_url, brand_name, page_metadata))
                return VisualAnalysisResult(
                    screenshot_url=screenshot_url,
                    overall_score=88.0,
                    confidence=0.9,
                    logo_detected=True,
                    details={
                        "dominant_colors": ["#111111", "#ffffff"],
                        "style": "clean",
                        "method": "vision",
                        "typography_consistent": True,
                        "insights": ["Clear visual hierarchy"],
                    },
                )

            def analyze_url(self, url, brand_name=""):
                self.analyze_url_calls.append((url, brand_name))
                raise AssertionError("analyze_url should not be called when screenshot_url is provided")

            def take_screenshot(self, url):
                self.take_screenshot_calls.append(url)
                raise AssertionError("take_screenshot should not be called when screenshot_url is provided")

        visual = FakeVisualAnalyzer()
        web = WebData(
            url="https://example.com",
            title="Example",
            meta_description="Example description",
            markdown_content="Brand guidelines and logo usage live here.",
        )

        feature = CoherenciaExtractor(visual_analyzer=visual)._visual_consistency(
            web,
            screenshot_url="file:///tmp/example.png",
        )

        self.assertEqual(feature.source, "visual_analysis")
        self.assertEqual(feature.value, 88.0)
        self.assertTrue(feature.raw_value["has_screenshot"])
        self.assertEqual(len(visual.screenshot_calls), 1)
        screenshot_url, brand_name, metadata = visual.screenshot_calls[0]
        self.assertEqual(screenshot_url, "file:///tmp/example.png")
        self.assertEqual(brand_name, "Example")
        self.assertEqual(metadata["title"], "Example")
        self.assertEqual(metadata["description"], "Example description")
        self.assertEqual(visual.analyze_url_calls, [])
        self.assertEqual(visual.take_screenshot_calls, [])

    def test_visual_consistency_skip_flag_emits_structured_fallback(self):
        web = WebData(url="https://example.com", title="Example",
                      markdown_content="Brand guidelines and logo usage live here.")
        extractor = CoherenciaExtractor(skip_visual_analysis=True)
        feature = extractor._visual_consistency(web)
        self.assertEqual(feature.source, "web_scrape_heuristic")
        self.assertEqual(feature.raw_value["reason"], "visual_analysis_skipped")
        self.assertTrue(feature.raw_value["heuristic_score_used"])
        self.assertIn("brand_in_header", feature.raw_value["heuristic_signals"])

    def test_visual_consistency_without_screenshot_keeps_existing_analyze_url_path(self):
        class FakeVisualAnalyzer:
            def __init__(self):
                self.urls = []

            def analyze_screenshot(self, *_args, **_kwargs):
                raise AssertionError("analyze_screenshot should not be called without screenshot_url")

            def analyze_url(self, url, brand_name=""):
                self.urls.append((url, brand_name))
                return VisualAnalysisResult(error="visual unavailable")

        visual = FakeVisualAnalyzer()
        web = WebData(url="https://example.com", title="Example", markdown_content="Example logo brand content.")

        feature = CoherenciaExtractor(visual_analyzer=visual)._visual_consistency(web)

        self.assertEqual(visual.urls, [("https://example.com", "Example")])
        self.assertEqual(feature.source, "web_scrape_heuristic")
        self.assertEqual(feature.raw_value["reason"], "visual_analysis_error")

    def test_visual_consistency_without_web_returns_zero_with_reason(self):
        feature = CoherenciaExtractor(skip_visual_analysis=True)._visual_consistency(web=None)
        self.assertEqual(feature.value, 0.0)
        self.assertEqual(feature.raw_value["reason"], "no_web_data")

    def test_messaging_consistency_without_llm_uses_heuristic_category_matching(self):
        web = WebData(
            url="https://priorlabs.ai",
            title="One Model, Infinite Predictions",
            markdown_content=(
                "# One Model, Infinite Predictions\n\n"
                "Pre-trained tabular foundation models for making predictions on structured data.\n"
            ),
        )
        exa = ExaData(
            brand_name="Prior Labs",
            mentions=[ExaResult(
                url="https://example.com/post-1",
                title="Prior Labs launches tabular foundation model",
                text="The company builds pre-trained foundation models for structured data prediction.",
            )],
        )
        feature = CoherenciaExtractor()._messaging_consistency(web, exa)
        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertGreater(feature.value, 60.0)
        self.assertEqual(feature.raw_value["reason"], "llm_unavailable")

    def test_messaging_consistency_without_exa_degrades_gracefully(self):
        web = WebData(
            url="https://example.com",
            title="Deterministic AI",
            markdown_content="# Deterministic AI\n\nA deterministic policy layer for enterprise AI governance.\n",
        )
        feature = CoherenciaExtractor()._messaging_consistency(web, exa=None)
        self.assertEqual(feature.value, 55.0)
        self.assertEqual(feature.raw_value["reason"], "llm_unavailable")

    def test_exa_mentions_payload_excludes_owned_surfaces_before_limiting(self):
        exa = ExaData(brand_name="Example", mentions=[
            ExaResult(
                url="https://example.com/about",
                title="About",
                text="Owned about page.",
                source_class="owned",
                relation="audited_surface",
            ),
            ExaResult(
                url="https://docs.example.com",
                title="Docs",
                text="Owned docs.",
                source_class="owned",
                relation="same_root_surface",
            ),
            ExaResult(
                url="https://external.example/article",
                title="External article",
                text="External coverage.",
                source_class="external",
                relation="external",
            ),
        ])

        payload = CoherenciaExtractor._exa_mentions_payload(exa, limit=1)

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["url"], "https://external.example/article")
        self.assertEqual(payload[0]["source_class"], "external")
        self.assertEqual(payload[0]["relation"], "external")

    def _make_coherence_llm(self, messaging_payload=None, tone_payload=None):
        class FakeLLM:
            api_key = "sk-test"
            def analyze_messaging_consistency(self, web_content, mentions, brand_name):
                return messaging_payload
            def analyze_tone_consistency(self, web_content, snippets, brand_name):
                return tone_payload
        return FakeLLM()

    def test_messaging_consistency_with_llm_uses_structured_verdict(self):
        web = WebData(url="https://example.com", title="Example",
                      markdown_content="We are predictive infrastructure for structured data.")
        exa = ExaData(brand_name="Example", mentions=[
            ExaResult(url="https://x.com/a", title="launches", text="Example is building predictive infra."),
            ExaResult(url="https://x.com/b", title="take", text="Example, a predictive data company."),
        ])
        llm = self._make_coherence_llm(messaging_payload={
            "consistency_score": 88, "verdict": "aligned",
            "self_category": "predictive infrastructure",
            "third_party_category": "predictive data company",
            "aligned_themes": ["prediction", "structured data"],
            "gaps": [], "reasoning": "Aligned.",
        })
        feature = CoherenciaExtractor(llm=llm)._messaging_consistency(web, exa)
        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.value, 88.0)
        self.assertEqual(feature.confidence, 0.85)
        self.assertEqual(feature.raw_value["verdict"], "aligned")
        self.assertIn("prediction", feature.raw_value["aligned_themes"])

    def test_messaging_consistency_uses_research_pack_as_llm_input(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("Login Pricing Cookie banner " * 200),
        )
        exa = ExaData(brand_name="Example", mentions=[
            ExaResult(
                url="https://x.com/a",
                title="Example launch",
                text="Example is discussed as AI governance infrastructure.",
            ),
        ])
        research_pack = {
            "version": "brand_research_pack_v0_1",
            "input_url": "https://example.com",
            "entity_type": "product",
            "resolved_entity": {"resolved_entity": "Example", "entity_type": "product"},
            "category": "AI governance infrastructure",
            "offer": "Example is a deterministic control layer for enterprise AI operations.",
            "proof_points": [
                {
                    "text": "Example is a deterministic control layer for enterprise AI operations.",
                    "source_url": "https://example.com/product",
                    "source_type": "owned",
                    "source_label": "product_offer",
                    "confidence": "high",
                }
            ],
            "noise_rejected": [
                {
                    "text": "Login Pricing Cookie banner",
                    "source_url": "https://example.com",
                    "source_type": "owned",
                    "source_label": "noise",
                    "confidence": "high",
                }
            ],
        }
        calls = {}

        class FakeLLM:
            api_key = "sk-test"

            def analyze_messaging_consistency(self, web_content, mentions, brand_name):
                calls["messaging_content"] = web_content
                return {
                    "consistency_score": 86,
                    "verdict": "aligned",
                    "self_category": "AI governance infrastructure",
                    "third_party_category": "AI governance infrastructure",
                    "aligned_themes": ["governance", "control"],
                    "gaps": [],
                    "reasoning": "The pack and mentions align.",
                }

            def analyze_tone_consistency(self, web_content, snippets, brand_name):
                return {"tone_consistency_score": 70, "gap_signal": "none", "examples": []}

        feature = CoherenciaExtractor(llm=FakeLLM(), skip_visual_analysis=True).extract(
            web=web,
            exa=exa,
            research_pack=research_pack,
        )["messaging_consistency"]

        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.value, 86.0)
        self.assertIn("Structured Brand Research Pack", calls["messaging_content"])
        self.assertIn("deterministic control layer", calls["messaging_content"])
        self.assertIn("Rejected noise", calls["messaging_content"])
        self.assertNotEqual(calls["messaging_content"], web.markdown_content)

    def test_messaging_consistency_with_invalid_verdict_falls_back(self):
        web = WebData(url="https://example.com", title="Example", markdown_content="x")
        exa = ExaData(brand_name="Example", mentions=[ExaResult(url="https://x/1", title="t", text="t")])
        llm = self._make_coherence_llm(messaging_payload={
            "consistency_score": 70, "verdict": "harmonious", "gaps": [],
        })
        feature = CoherenciaExtractor(llm=llm)._messaging_consistency(web, exa)
        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.raw_value["reason"], "llm_invalid_verdict")

    def test_messaging_consistency_malformed_gaps_are_filtered(self):
        web = WebData(url="https://example.com", title="Example",
                      markdown_content="We are a data platform for teams.")
        exa = ExaData(brand_name="Example", mentions=[
            ExaResult(url="https://x/1", title="t", text="t"),
            ExaResult(url="https://x/2", title="u", text="u"),
        ])
        llm = self._make_coherence_llm(messaging_payload={
            "consistency_score": 55, "verdict": "partial_gap",
            "gaps": [
                {"self_says": "data platform", "third_party_says": "analytics tool", "source_url": "https://x/1"},
                {"self_says": "platform", "third_party_says": 123, "source_url": "https://x/2"},
                "not a dict",
            ],
            "reasoning": "Mismatch.",
        })
        feature = CoherenciaExtractor(llm=llm)._messaging_consistency(web, exa)
        self.assertEqual(feature.source, "llm")
        self.assertEqual(len(feature.raw_value["gaps"]), 1)
        self.assertNotIn("reason", feature.raw_value)

    def test_messaging_consistency_partial_gap_all_dropped_degrades_confidence(self):
        web = WebData(url="https://example.com", title="Example",
                      markdown_content="We are a data platform.")
        exa = ExaData(brand_name="Example", mentions=[
            ExaResult(
                url="https://directory.example.com/example",
                title="Example company profile",
                text="Example appears in a directory profile.",
                source_class="external",
                relation="external",
            )
        ])
        llm = self._make_coherence_llm(messaging_payload={
            "consistency_score": 50, "verdict": "partial_gap",
            "gaps": [{"self_says": 123, "third_party_says": "analytics"}],
            "reasoning": "Mismatch.",
        })
        feature = CoherenciaExtractor(llm=llm)._messaging_consistency(web, exa)
        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.confidence, 0.5)
        self.assertEqual(feature.raw_value["reason"], "llm_partial_evidence")

    def test_tone_consistency_with_llm_uses_structured_gap_signal(self):
        web = WebData(url="https://example.com", title="Example",
                      markdown_content="We build deterministic policy layers.")
        exa = ExaData(brand_name="Example", mentions=[
            ExaResult(url="https://x/1", title="t", text="Example is a rigorous enterprise platform."),
        ])
        llm = self._make_coherence_llm(tone_payload={
            "tone_consistency_score": 78,
            "self_tone": "formal technical", "third_party_tone": "formal enterprise",
            "gap_signal": "mild",
            "examples": [{"source": "web", "quote": "deterministic policy layers",
                          "tone_marker": "technical precision"}],
            "reasoning": "Both lean formal.",
        })
        feature = CoherenciaExtractor(llm=llm)._tone_consistency(web, exa)
        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.value, 78.0)
        self.assertEqual(feature.raw_value["gap_signal"], "mild")
        self.assertEqual(len(feature.raw_value["examples"]), 1)

    def test_tone_consistency_uses_research_pack_tone_signals(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content=("Generic SaaS page copy " * 220),
        )
        exa = ExaData(brand_name="Example", mentions=[
            ExaResult(
                url="https://x.com/a",
                title="Example profile",
                text="Example is described as rigorous enterprise infrastructure.",
            ),
        ])
        research_pack = {
            "version": "brand_research_pack_v0_1",
            "input_url": "https://example.com",
            "entity_type": "product",
            "resolved_entity": {"resolved_entity": "Example", "entity_type": "product"},
            "tone_of_voice": "precise, rigorous, control-oriented",
            "personality_signals": ["precise", "rigorous", "control-oriented"],
            "proof_points": [
                {
                    "text": "Example uses precise operating language around deterministic control.",
                    "source_url": "https://example.com/about",
                    "source_type": "owned",
                    "source_label": "tone",
                    "confidence": "medium",
                }
            ],
        }
        calls = {}

        class FakeLLM:
            api_key = "sk-test"

            def analyze_messaging_consistency(self, web_content, mentions, brand_name):
                return {"consistency_score": 75, "verdict": "aligned", "gaps": []}

            def analyze_tone_consistency(self, web_content, snippets, brand_name):
                calls["tone_content"] = web_content
                return {
                    "tone_consistency_score": 82,
                    "self_tone": "precise rigorous",
                    "third_party_tone": "formal enterprise",
                    "gap_signal": "none",
                    "examples": [
                        {
                            "source": "web",
                            "quote": "deterministic control",
                            "tone_marker": "technical precision",
                        },
                        {
                            "source": "mention",
                            "quote": "rigorous enterprise infrastructure",
                            "tone_marker": "formal enterprise framing",
                        }
                    ],
                    "reasoning": "The pack exposes explicit tone evidence.",
                }

        feature = CoherenciaExtractor(llm=FakeLLM(), skip_visual_analysis=True).extract(
            web=web,
            exa=exa,
            research_pack=research_pack,
        )["tone_consistency"]

        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.value, 82.0)
        self.assertIn("Structured Brand Research Pack", calls["tone_content"])
        self.assertIn("precise, rigorous, control-oriented", calls["tone_content"])
        self.assertIn("personality_signals", calls["tone_content"])
        self.assertNotEqual(calls["tone_content"], web.markdown_content)

    def test_tone_consistency_degrades_when_no_mention_examples_support_gap_none(self):
        web = WebData(
            url="https://example.com",
            title="Example",
            markdown_content="Technical and precise product copy.",
        )
        exa = ExaData(brand_name="Example", mentions=[
            ExaResult(
                url="https://external.example/article",
                title="External article",
                text="External coverage exists.",
                source_class="external",
                relation="external",
            )
        ])
        llm = self._make_coherence_llm(tone_payload={
            "tone_consistency_score": 90,
            "self_tone": "technical",
            "third_party_tone": "no relevant third-party tone",
            "gap_signal": "none",
            "examples": [
                {
                    "source": "web",
                    "quote": "Technical and precise product copy.",
                    "tone_marker": "technical precision",
                }
            ],
            "reasoning": "Only owned tone evidence was useful.",
        })

        feature = CoherenciaExtractor(llm=llm)._tone_consistency(web, exa)

        self.assertEqual(feature.source, "llm")
        self.assertEqual(feature.confidence, 0.5)
        self.assertEqual(feature.value, 60.0)
        self.assertEqual(feature.raw_value["reason"], "no_third_party_tone_evidence")

    def test_tone_consistency_ignores_domain_collision_mentions(self):
        web = WebData(
            url="https://www.cofisolutions.com",
            title="COFI Solutions",
            markdown_content="Rigor de norma internacional y lenguaje de negocio.",
        )
        exa = ExaData(
            brand_name="www.cofisolutions.com",
            mentions=[
                ExaResult(
                    url="https://cofisolutions.com/",
                    title="COFI Solutions",
                    text="Owned brand copy",
                    source_class="owned",
                    relation="audited_surface",
                ),
                ExaResult(
                    url="https://news.coforge.com/story",
                    title="Coforge partnership",
                    text="Unrelated enterprise services coverage",
                    source_class="external",
                    relation="external",
                ),
            ],
        )
        captured = {}

        class FakeLLM:
            api_key = "sk-test"

            def analyze_tone_consistency(self, web_content, snippets, brand_name):
                captured["snippets"] = snippets
                return {
                    "tone_consistency_score": 80,
                    "self_tone": "formal",
                    "third_party_tone": "",
                    "gap_signal": "none",
                    "examples": [],
                    "reasoning": "No usable third-party evidence.",
                }

        feature = CoherenciaExtractor(llm=FakeLLM())._tone_consistency(web, exa)

        self.assertEqual(captured["snippets"], [])
        self.assertEqual(feature.value, 80.0)

    def test_tone_consistency_without_llm_falls_back_to_heuristic(self):
        web = WebData(url="https://example.com", title="Example",
                      markdown_content="Hey! This is gonna be awesome. Let's go!")
        feature = CoherenciaExtractor()._tone_consistency(web, exa=None)
        self.assertEqual(feature.source, "heuristic_fallback")
        self.assertEqual(feature.raw_value["reason"], "llm_unavailable")
        self.assertGreater(feature.raw_value["heuristic_signals"]["informal_markers"], 0)

    def test_cross_channel_coherence_counts_social_platforms_explicitly(self):
        web = WebData(
            url="https://poetiq.ai/", title="Poetiq",
            markdown_content=(
                "# Poetiq\n\nFollow us at https://twitter.com/poetiq and https://linkedin.com/company/poetiq.\n"
                "Get in touch. Privacy Policy. About us.\n"
            ),
        )
        exa = ExaData(brand_name="Poetiq", mentions=[
            ExaResult(url="https://poetiq.ai/blog/launch", title="launch", text="..."),
        ])
        feature = CoherenciaExtractor()._cross_channel_coherence(web, exa)
        self.assertGreaterEqual(feature.value, 50.0)
        self.assertTrue(feature.raw_value["has_social_links"])
        self.assertIn("twitter", feature.raw_value["social_platforms_detected"])
        self.assertIn("linkedin", feature.raw_value["social_platforms_detected"])
        self.assertTrue(feature.raw_value["brand_url_mentioned_in_exa"])

    def test_cross_channel_coherence_stays_low_on_minimal_landing(self):
        web = WebData(url="https://example.com/", title="Example",
                      markdown_content="# Example\n\nMinimal landing page.\n")
        feature = CoherenciaExtractor()._cross_channel_coherence(web, exa=None)
        self.assertLessEqual(feature.value, 25.0)
        self.assertFalse(feature.raw_value["has_social_links"])
        self.assertEqual(feature.raw_value["social_platforms_detected"], [])

    def test_cross_channel_coherence_accepts_alternate_domains(self):
        web = WebData(
            url="https://movements.mov/en",
            canonical_url="https://movements.dev/en",
            alternate_domains=["movements.dev"],
            title="MOVEMENTS",
            markdown_content="# MOVEMENTS\n\nJoin the movement.\n",
        )
        exa = ExaData(brand_name="Movements", mentions=[
            ExaResult(url="https://movements.dev/en/blog/launch", title="launch", text="..."),
        ])
        feature = CoherenciaExtractor()._cross_channel_coherence(web, exa)
        self.assertTrue(feature.raw_value["brand_url_mentioned_in_exa"])
        self.assertIn("movements.dev", feature.raw_value["brand_domains"])

    def test_extract_always_returns_four_features(self):
        features = CoherenciaExtractor(skip_visual_analysis=True).extract(web=None, exa=None)
        self.assertEqual(
            set(features.keys()),
            {"visual_consistency", "messaging_consistency", "tone_consistency", "cross_channel_coherence"},
        )


if __name__ == "__main__":
    unittest.main()
