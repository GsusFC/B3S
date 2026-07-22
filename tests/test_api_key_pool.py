from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import exa_py
import firecrawl

from src.api_key_pool import ApiKeyPool, normalize_api_keys
from src.collectors.exa_collector import ExaCollector
from src.collectors.social_collector import SocialCollector
from src.collectors.web_collector import WebCollector
from src.features.visual_analyzer import VisualAnalyzer


def test_normalize_api_keys_appends_and_deduplicates_sources():
    assert normalize_api_keys(
        '"primary-key"',
        "secondary-key, primary-key\nthird-key",
        ("secondary-key", "fourth-key"),
    ) == (
        "primary-key",
        "secondary-key",
        "third-key",
        "fourth-key",
    )


def test_api_key_pool_round_robin_is_thread_safe_and_redacted():
    pool = ApiKeyPool(("secret-a", "secret-b", "secret-c"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        selected = list(executor.map(lambda _index: pool.next_key(), range(60)))

    assert selected.count("secret-a") == 20
    assert selected.count("secret-b") == 20
    assert selected.count("secret-c") == 20
    assert repr(pool) == "ApiKeyPool(size=3)"
    assert "secret" not in repr(pool)


def test_exa_retries_with_next_key(monkeypatch):
    used_keys: list[str] = []

    class FakeExa:
        def __init__(self, api_key: str):
            self.api_key = api_key

        def search(self, _query: str, **_params):
            used_keys.append(self.api_key)
            if self.api_key == "exa-pool-a":
                raise RuntimeError("429 rate limited")
            return SimpleNamespace(results=[])

    monkeypatch.setattr(exa_py, "Exa", FakeExa)
    monkeypatch.setattr("src.collectors.exa_collector.time.sleep", lambda _seconds: None)
    collector = ExaCollector(api_key=("exa-pool-a", "exa-pool-b"))

    response = collector._search_with_retry("brand", {})

    assert response.results == []
    assert used_keys == ["exa-pool-a", "exa-pool-b"]


def test_firecrawl_retries_with_next_key(monkeypatch):
    used_keys: list[str] = []

    class FakeFirecrawl:
        def __init__(self, api_key: str):
            self.api_key = api_key

        def scrape(self, _url: str, **_params):
            used_keys.append(self.api_key)
            if self.api_key == "firecrawl-pool-a":
                raise RuntimeError("429 rate limited")
            return SimpleNamespace(markdown="# Brand", html="<h1>Brand</h1>")

    monkeypatch.setattr(firecrawl, "Firecrawl", FakeFirecrawl)
    monkeypatch.setattr("src.collectors.web_collector.time.sleep", lambda _seconds: None)
    collector = WebCollector(api_key=("firecrawl-pool-a", "firecrawl-pool-b"))

    result = collector._run_firecrawl("https://example.com")

    assert result["content"] == "# Brand"
    assert used_keys == ["firecrawl-pool-a", "firecrawl-pool-b"]


def test_social_collector_rotates_firecrawl_keys(monkeypatch):
    used_keys: list[str] = []

    class FakeFirecrawl:
        def __init__(self, api_key: str):
            self.api_key = api_key

        def scrape(self, _url: str, **_params):
            used_keys.append(self.api_key)
            if self.api_key == "social-pool-a":
                raise RuntimeError("401 expired key")
            return SimpleNamespace(markdown="Social profile")

    monkeypatch.setattr(firecrawl, "Firecrawl", FakeFirecrawl)
    monkeypatch.setattr("src.collectors.social_collector.time.sleep", lambda _seconds: None)
    collector = SocialCollector(api_key=("social-pool-a", "social-pool-b"))

    result = collector._run_firecrawl("https://social.example/brand")

    assert result["content"] == "Social profile"
    assert used_keys == ["social-pool-a", "social-pool-b"]


def test_visual_analyzer_rotates_firecrawl_keys(monkeypatch):
    used_keys: list[str] = []

    class FakeFirecrawl:
        def __init__(self, api_key: str):
            self.api_key = api_key

        def scrape(self, _url: str, **_params):
            used_keys.append(self.api_key)
            if self.api_key == "visual-pool-a":
                raise RuntimeError("429 rate limited")
            return SimpleNamespace(
                screenshot="https://cdn.example/screenshot.png",
                metadata={"statusCode": 200},
            )

    monkeypatch.setattr(firecrawl, "Firecrawl", FakeFirecrawl)
    analyzer = VisualAnalyzer(api_key=("visual-pool-a", "visual-pool-b"))

    result = analyzer.take_screenshot("https://example.com")

    assert result["screenshot_url"] == "https://cdn.example/screenshot.png"
    assert used_keys == ["visual-pool-a", "visual-pool-b"]
