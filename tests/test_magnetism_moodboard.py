from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Ensure workspace root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features.magnetism.moodboard import (
    MAX_MOODBOARD_IMAGES,
    build_moodboard_model,
    extract_moodboard_images,
    select_moodboard_images,
)


_SAMPLE_HTML = """
<html><head>
<meta property="og:image" content="https://cdn.acme.com/og-card.png">
<meta name="twitter:image" content="https://cdn.acme.com/og-card.png">
<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
<link rel="icon" href="/favicon.ico">
</head><body>
<img src="/img/hero.jpg" alt="Team at work">
<img src="data:image/gif;base64,R0lGOD" alt="inline">
<img src="https://www.google-analytics.com/collect.gif" alt="">
<img src="https://cdn.acme.com/pixel.gif" width="1" height="1">
</body></html>
"""


class MoodboardExtractionTests(unittest.TestCase):
    def test_extracts_and_classifies_images_from_web_payload(self):
        images = extract_moodboard_images(
            {
                "url": "https://acme.com",
                "html": _SAMPLE_HTML,
                "markdown_content": "Intro ![Product screenshot](https://acme.com/img/product.png) more text",
            }
        )

        urls = {item["url"]: item for item in images}
        self.assertIn("https://cdn.acme.com/og-card.png", urls)
        self.assertEqual(urls["https://cdn.acme.com/og-card.png"]["role"], "social_card")
        self.assertIn("https://acme.com/assets/apple-touch-icon.png", urls)
        self.assertEqual(urls["https://acme.com/assets/apple-touch-icon.png"]["role"], "logo")
        self.assertIn("https://acme.com/img/hero.jpg", urls)
        self.assertEqual(urls["https://acme.com/img/hero.jpg"]["alt"], "Team at work")
        self.assertIn("https://acme.com/img/product.png", urls)

        # Duplicates collapse; .ico, data:, tracking, and 1x1 images are dropped.
        self.assertEqual(len([i for i in images if i["url"] == "https://cdn.acme.com/og-card.png"]), 1)
        self.assertNotIn("https://acme.com/favicon.ico", urls)
        for url in urls:
            self.assertNotIn("google-analytics", url)
            self.assertNotIn("pixel", url)

    def test_caps_image_count(self):
        markdown = "\n".join(
            f"![img {i}](https://acme.com/img/photo-{i}.png)" for i in range(MAX_MOODBOARD_IMAGES + 10)
        )
        images = extract_moodboard_images({"url": "https://acme.com", "markdown_content": markdown})
        self.assertEqual(len(images), MAX_MOODBOARD_IMAGES)

    def test_empty_payload_yields_empty_list(self):
        self.assertEqual(extract_moodboard_images(None), [])
        self.assertEqual(extract_moodboard_images({}), [])

    def test_filters_language_flags_partner_logos_and_invalid_urls(self):
        html = """
        <main>
          <section class="hero">
            <img src="/girl_hero_section1.webp" alt="Zeeguros insurance advice"
                 width="1440" height="900">
          </section>
          <section class="insurance-logos">
            <h2>Aseguradoras con las que trabajamos</h2>
            <img src="/allianz_black_1x.png" width="220" height="70">
            <img src="/allianz_color_1x.png" width="220" height="70">
            <img src="/mapfre_black_1x.png" width="220" height="70">
            <img src="/mapfre_color_1x.png" width="220" height="70">
          </section>
          <nav class="language-switcher">
            <img src="/flags/en.svg" alt="English" width="24" height="16">
          </nav>
          <img src="https://zeeguro" alt="">
        </main>
        """
        selection = select_moodboard_images(
            {
                "url": "https://zeeguros.com",
                "brand_name": "Zeeguros",
                "html": html,
            },
            brand_logo_url="https://zeeguros.com/zeeguros-logo.svg",
        )

        urls = {item["url"] for item in selection["images"]}
        self.assertEqual(
            urls,
            {
                "https://zeeguros.com/zeeguros-logo.svg",
                "https://zeeguros.com/girl_hero_section1.webp",
            },
        )
        reasons = {item["reason"] for item in selection["rejected_images"]}
        self.assertIn("partner_logo", reasons)
        self.assertIn("language_flag", reasons)
        self.assertIn("invalid_url", reasons)

    def test_filters_third_party_logos_placeholders_and_opaque_assets(self):
        selection = select_moodboard_images(
            {
                "url": "https://masia.vc",
                "brand_name": "Masia",
                "images": [
                    {
                        "url": "https://cdn.prod.website-files.com/masia-collective.avif",
                        "alt": "Masia collective gathering",
                        "width": 1400,
                        "height": 900,
                    },
                    {
                        "url": "https://cdn.prod.website-files.com/Autentic_logo_text.svg",
                        "width": 240,
                        "height": 60,
                    },
                    {
                        "url": "https://cdn.prod.website-files.com/bluewalker_logo_png.png",
                        "width": 240,
                        "height": 60,
                    },
                    {
                        "url": "https://cdn.prod.website-files.com/plugins/Basic/assets/placeholder.svg",
                    },
                    {
                        "url": "https://cdn.prod.website-files.com/6a1daeaf",
                    },
                    {
                        "url": "https://cdn.prod.website-files.com/6",
                    },
                ],
            },
            brand_logo_url="https://masia.vc/masia-logo.svg",
        )

        urls = {item["url"] for item in selection["images"]}
        self.assertEqual(
            urls,
            {
                "https://masia.vc/masia-logo.svg",
                "https://cdn.prod.website-files.com/masia-collective.avif",
            },
        )
        reason_counts = {
            reason: sum(item["reason"] == reason for item in selection["rejected_images"])
            for reason in {item["reason"] for item in selection["rejected_images"]}
        }
        self.assertEqual(reason_counts["third_party_logo"], 2)
        self.assertEqual(reason_counts["placeholder"], 1)
        self.assertEqual(reason_counts["opaque_asset"], 2)

    def test_dom_context_cannot_be_bypassed_by_markdown_duplicate(self):
        shared_url = "https://acme.com/assets/investor-logo.svg"
        selection = select_moodboard_images(
            {
                "url": "https://acme.com",
                "brand_name": "Acme",
                "html": f"""
                    <section class="investor-logos">
                      <img src="{shared_url}" alt="Fund logo" width="200" height="60">
                    </section>
                """,
                "markdown_content": f"![Fund logo]({shared_url})",
            }
        )

        self.assertEqual(selection["images"], [])
        self.assertEqual(selection["rejected_images"][0]["reason"], "partner_logo")

    def test_collapses_visual_variants_and_preserves_brand_cdn_hero(self):
        selection = select_moodboard_images(
            {
                "url": "https://acme.com",
                "brand_name": "Acme",
                "images": [
                    {
                        "url": "https://cdn.assets.com/acme-campaign-black.webp",
                        "alt": "Acme campaign",
                        "width": 1200,
                        "height": 800,
                        "context": "hero campaign",
                    },
                    {
                        "url": "https://cdn.assets.com/acme-campaign-color.webp",
                        "alt": "Acme campaign",
                        "width": 1200,
                        "height": 800,
                        "context": "hero campaign",
                    },
                ],
            }
        )

        self.assertEqual(len(selection["images"]), 1)
        self.assertEqual(selection["images"][0]["role"], "hero")
        self.assertIn(
            "duplicate_variant",
            {item["reason"] for item in selection["rejected_images"]},
        )

    def test_legacy_mode_is_available_as_display_only_rollback(self):
        payload = {
            "url": "https://acme.com",
            "markdown_content": "![Investor logo](https://cdn.example.com/investor-logo.svg)",
        }
        filtered = select_moodboard_images(payload, mode="filtered")
        legacy = select_moodboard_images(payload, mode="legacy")

        self.assertEqual(filtered["images"], [])
        self.assertEqual(filtered["selection_version"], "visual-assets-v2")
        self.assertEqual(len(legacy["images"]), 1)
        self.assertEqual(legacy["selection_version"], "legacy")

    def test_selection_is_deterministic(self):
        payload = {
            "url": "https://acme.com",
            "html": _SAMPLE_HTML,
            "markdown_content": "![Product](https://acme.com/img/product.png)",
        }
        first = select_moodboard_images(payload)
        second = select_moodboard_images(payload)
        self.assertEqual(first, second)


class MoodboardModelTests(unittest.TestCase):
    def test_model_includes_brand_logo_and_visual_reading(self):
        scan_payload = {
            "url": "https://acme.com",
            "tldr_brand3": {
                "personality": {"detected": True, "content": "Direct and technical."},
                "attributes": {"detected": True, "content": ["clear", "fast"]},
                "brand_idea": {"detected": False, "content": ""},
                "value_proposition": {"detected": True, "content": "Close faster."},
            },
        }
        model = build_moodboard_model(
            scan_payload,
            {"url": "https://acme.com", "html": _SAMPLE_HTML},
            brand_logo_url="https://acme.com/logo.svg",
        )

        self.assertTrue(model["available"])
        self.assertEqual(model["images"][0]["url"], "https://acme.com/logo.svg")
        self.assertEqual(model["images"][0]["role"], "logo")
        self.assertEqual(model["logo_image"]["url"], "https://acme.com/logo.svg")
        reading_keys = [item["key"] for item in model["visual_reading"]]
        self.assertEqual(reading_keys, ["personality", "attributes", "value_proposition"])
        attributes = next(item for item in model["visual_reading"] if item["key"] == "attributes")
        self.assertEqual(attributes["text"], "clear · fast")
        self.assertGreaterEqual(model["role_counts"]["logo"], 1)
        self.assertFalse(model["runtime_effect"])
        self.assertEqual(model["selection_version"], "visual-assets-v2")

    def test_model_without_web_payload_is_unavailable(self):
        model = build_moodboard_model({"url": "https://acme.com", "tldr_brand3": {}}, None)
        self.assertFalse(model["available"])
        self.assertEqual(model["images"], [])


if __name__ == "__main__":
    unittest.main()
