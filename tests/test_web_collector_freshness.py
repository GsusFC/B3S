from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import firecrawl

from src.collectors.web_collector import (
    WEB_CAPTURE_PROVENANCE_VERSION,
    WebCollector,
)
from src.services.input_collection_payloads import from_web_payload


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "soccersolver"
    / "about_us_2026_07_29.html"
)


def test_firecrawl_forces_fresh_soccersolver_capture_and_records_provenance(
    monkeypatch,
) -> None:
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    collector = WebCollector(api_key="firecrawl-test-key")
    markdown = collector._html_to_markdown_fallback(html)
    calls: list[dict[str, object]] = []

    class FakeFirecrawl:
        def __init__(self, api_key: str):
            assert api_key == "firecrawl-test-key"

        def scrape(self, url: str, **kwargs):
            calls.append({"url": url, **kwargs})
            return SimpleNamespace(
                markdown=markdown,
                html=html,
                metadata={"sourceURL": "https://soccersolver.com/about-us"},
            )

    monkeypatch.setattr(firecrawl, "Firecrawl", FakeFirecrawl)

    data = collector.scrape(
        "https://soccersolver.com/about-us",
        crawl_subpages=False,
    )

    assert calls[0]["max_age"] == 0
    assert calls[0]["formats"] == ["markdown", "html"]
    assert "To redefine football as a discipline of science." in data.markdown_content
    assert "We apply the scientific method to football." in data.markdown_content

    provenance = data.capture_provenance
    assert provenance["version"] == WEB_CAPTURE_PROVENANCE_VERSION
    assert provenance["provider"] == "firecrawl"
    assert provenance["firecrawl_max_age_ms"] == 0
    assert provenance["requested_url"] == "https://soccersolver.com/about-us"
    assert provenance["final_url"] == "https://soccersolver.com/about-us"
    assert provenance["content_sha256"] == sha256(
        data.markdown_content.encode("utf-8")
    ).hexdigest()
    assert datetime.fromisoformat(
        str(provenance["fetched_at"]).replace("Z", "+00:00")
    ).tzinfo is not None

    restored = from_web_payload(asdict(data))
    assert restored is not None
    assert restored.capture_provenance == provenance
