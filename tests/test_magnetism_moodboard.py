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

    def test_model_without_web_payload_is_unavailable(self):
        model = build_moodboard_model({"url": "https://acme.com", "tldr_brand3": {}}, None)
        self.assertFalse(model["available"])
        self.assertEqual(model["images"], [])


if __name__ == "__main__":
    unittest.main()
