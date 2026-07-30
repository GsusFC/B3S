from dataclasses import asdict

from src.collectors.web_collector import WebCollector
from src.services.input_collection_payloads import from_web_payload


def test_internal_link_extraction_ignores_framework_assets_and_keeps_navigation() -> None:
    collector = WebCollector(api_key=())
    html = """
    <a href="/about">About</a>
    <link rel="preload" as="image" href="/_next/image?url=%2F_next%2Fstatic%2Fmedia%2Fillo-values.png&w=1920&q=75">
    <link rel="preload" as="font" href="/_next/static/media/font.woff2">
    """

    links = collector._extract_internal_links(
        "[Valores](/values)",
        "https://movyn.ai",
        html=html,
        links=["https://movyn.ai/_next/image?url=%2Fassets%2Fillo-values.png", "/culture"],
    )

    assert links == ["https://movyn.ai/values", "https://movyn.ai/about", "https://movyn.ai/culture"]


def test_sitemap_discovery_reads_robots_index_and_filters_disallowed_urls(
    monkeypatch,
) -> None:
    collector = WebCollector(api_key=())
    resources = {
        "https://example.com/robots.txt": (
            "User-agent: *\n"
            "Disallow: /private\n"
            "Sitemap: https://example.com/sitemap-index.xml\n"
        ),
        "https://example.com/sitemap-index.xml": (
            '<?xml version="1.0"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://example.com/pages.xml</loc></sitemap>"
            "<sitemap><loc>https://external.example/foreign.xml</loc></sitemap>"
            "</sitemapindex>"
        ),
        "https://example.com/pages.xml": (
            '<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://example.com/</loc><lastmod>2026-07-01</lastmod></url>"
            "<url><loc>https://example.com/about-us</loc><lastmod>2026-07-03</lastmod></url>"
            "<url><loc>https://example.com/private/roadmap</loc></url>"
            "<url><loc>https://example.com/report.pdf</loc></url>"
            "<url><loc>https://external.example/about</loc></url>"
            "</urlset>"
        ),
    }
    requested: list[str] = []

    def fake_fetch(url: str) -> tuple[str, str]:
        requested.append(url)
        if url in resources:
            return resources[url], ""
        return "", "not found"

    monkeypatch.setattr(collector, "_fetch_text_resource", fake_fetch)

    links, diagnostics = collector._discover_sitemap_links(
        "https://example.com",
    )

    assert links == ["https://example.com/about-us"]
    assert diagnostics["status"] == "discovered"
    assert diagnostics["robots_status"] == "read"
    assert diagnostics["disallowed_count"] == 1
    assert diagnostics["known_page_count"] == 2
    assert diagnostics["latest_lastmod"] == "2026-07-03"
    assert diagnostics["known_pages"] == [
        {"url": "https://example.com", "lastmod": "2026-07-01"},
        {"url": "https://example.com/about-us", "lastmod": "2026-07-03"},
    ]
    assert {row["reason"] for row in diagnostics["excluded_pages"]} == {
        "resource_filtered",
        "robots_disallowed",
    }
    assert diagnostics["sitemaps_read"] == [
        "https://example.com/sitemap-index.xml",
        "https://example.com/pages.xml",
    ]
    assert "https://external.example/foreign.xml" not in requested


def test_owned_page_selection_covers_soccersolver_strategic_roles() -> None:
    collector = WebCollector(api_key=())
    links = [
        "https://soccersolver.com/contact-us",
        "https://soccersolver.com/validation-meetings",
        "https://soccersolver.com/laprovincia",
        "https://soccersolver.com/about-us",
        "https://soccersolver.com/new-way-of-signings",
        "https://soccersolver.com/the-algorithms",
        "https://soccersolver.com/the-software",
        "https://soccersolver.com/media-and-press",
        "https://soccersolver.com/research",
        "https://soccersolver.com/detected-players",
        "https://soccersolver.com/club-simulations",
        "https://soccersolver.com/collaborate",
    ]

    selected = collector._select_internal_links_to_crawl(
        links,
        "https://soccersolver.com",
    )

    assert selected == [
        "https://soccersolver.com/the-software",
        "https://soccersolver.com/club-simulations",
        "https://soccersolver.com/about-us",
        "https://soccersolver.com/the-algorithms",
        "https://soccersolver.com/research",
        "https://soccersolver.com/validation-meetings",
        "https://soccersolver.com/detected-players",
        "https://soccersolver.com/new-way-of-signings",
    ]
    assert "https://soccersolver.com/laprovincia" not in selected


def test_owned_page_selection_reserves_two_sitemap_only_exploration_slots() -> None:
    collector = WebCollector(api_key=())
    strategic = [
        "https://example.com/product",
        "https://example.com/solutions",
        "https://example.com/about",
        "https://example.com/the-algorithm",
        "https://example.com/research",
        "https://example.com/validation",
    ]
    orphaned = [
        "https://example.com/founder-letter-july",
        "https://example.com/category-thesis",
        "https://example.com/another-orphan",
    ]
    overflow = ["https://example.com/validation-results"]

    selected = collector._select_internal_links_to_crawl(
        [*strategic, *orphaned, *overflow],
        "https://example.com",
        sitemap_only_links=orphaned,
    )

    assert selected[:5] == strategic[:5]
    assert selected[5] == overflow[0]
    assert selected[-2:] == orphaned[:2]
    assert len(selected) == 8


def test_owned_page_selection_backfills_unused_budget_with_recent_sitemap_pages() -> None:
    collector = WebCollector(api_key=())
    root = "https://altitude.example"
    recent_articles = [
        f"{root}/blog/cross-border-payments",
        f"{root}/blog/close-books-faster",
        f"{root}/blog/automate-expenses",
        f"{root}/blog/corporate-card",
        f"{root}/blog/stablecoin-transfers",
    ]
    links = [
        f"{root}/about-us",
        f"{root}/card",
        f"{root}/law",
        *recent_articles,
        f"{root}/legal/terms-of-service",
        f"{root}/privacy",
    ]
    lastmod = {
        url: f"2026-07-{31 - index:02d}T00:00:00.000Z"
        for index, url in enumerate(recent_articles)
    }

    selected = collector._select_internal_links_to_crawl(
        links,
        root,
        sitemap_only_links=[
            link for link in links if link != f"{root}/card"
        ],
        sitemap_lastmod=lastmod,
    )

    assert len(selected) == 8
    assert set(recent_articles).issubset(selected)
    assert f"{root}/legal/terms-of-service" not in selected
    assert f"{root}/privacy" not in selected
    assert selected.index(recent_articles[0]) < selected.index(
        recent_articles[-1]
    )


def test_scrape_persists_page_selection_sources_and_capture_status(
    monkeypatch,
) -> None:
    collector = WebCollector(api_key=())
    root = "https://example.com"
    sitemap_links = [
        f"{root}/the-software",
        f"{root}/club-simulations",
        f"{root}/about-us",
        f"{root}/the-algorithms",
        f"{root}/research",
        f"{root}/validation-meetings",
    ]

    def fake_firecrawl(url: str) -> dict:
        if url == root:
            return {
                "content": (
                    "# Example\n\n"
                    "Evidence-rich homepage copy for an analytical platform. "
                    * 8
                ),
                "html": '<a href="/about-us">About</a>',
                "final_url": root,
            }
        return {
            "content": f"# Page\n\nStrategic evidence from {url}. " * 8,
            "html": "",
            "final_url": url,
        }

    monkeypatch.setattr(collector, "_run_firecrawl", fake_firecrawl)
    monkeypatch.setattr(
        collector,
        "_discover_sitemap_links",
        lambda _url: (
            sitemap_links,
            {
                "status": "discovered",
                "robots_status": "read",
                "robots_url": f"{root}/robots.txt",
                "sitemaps_read": [f"{root}/sitemap.xml"],
                "candidate_count": len(sitemap_links),
                "known_page_count": len(sitemap_links) + 1,
                "known_pages": [
                    {"url": root, "lastmod": "2026-07-01"},
                    *[
                        {"url": page_url, "lastmod": ""}
                        for page_url in sitemap_links
                    ],
                ],
                "excluded_pages": [],
                "latest_lastmod": "2026-07-01",
                "disallowed_count": 0,
                "errors": [],
            },
        ),
    )

    data = collector.scrape(root, crawl_subpages=True)

    assert data.owned_fallback_urls == sitemap_links
    assert data.page_selection["version"] == "owned-page-selection-v2"
    assert data.page_selection["maximum_budget"] == 8
    assert data.page_selection["observed_candidate_count"] == 1
    assert data.page_selection["sitemap_candidate_count"] == 6
    assert data.page_selection["captured_count"] == 6
    assert data.page_selection["captured_page_count"] == 7
    assert data.page_selection["known_page_count"] == 7
    assert data.page_selection["coverage_ratio"] == 1.0
    assert data.page_selection["not_visited_pages"] == []
    assert len(data.page_selection["visited_pages"]) == 7
    assert data.page_selection["language_detection"]["status"] == "single_language"
    assert data.page_selection["language_detection"]["distribution"] == [
        {"language": "en", "page_count": 7, "share": 1.0}
    ]
    assert {
        row["observed_language"]
        for row in data.page_selection["visited_pages"]
    } == {"en"}
    selected = data.page_selection["selected"]
    assert selected[2]["source"] == "observed+sitemap"
    assert {row["status"] for row in selected} == {"captured"}

    restored = from_web_payload(asdict(data))
    assert restored is not None
    assert restored.page_selection == data.page_selection
