from src.collectors.web_collector import WebCollector


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
