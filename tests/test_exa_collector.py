from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from src.collectors.exa_collector import EXA_STRATEGY_VERSION, ExaCollector
from src.collectors.web_collector import WebData
from src.services.legal_identity import derive_legal_name


def test_exa_identity_matching_rejects_near_name_and_person_collisions():
    collector = ExaCollector(api_key="test")

    near_name = SimpleNamespace(
        url="https://linkedin.com/company/movyng",
        title="Movyng",
        text="Automotive rental company.",
        summary="",
    )
    person = SimpleNamespace(
        url="https://www.linkedin.com/in/movyn",
        title="Movyn John",
        text="VP Expert Services at Fluent Commerce.",
        summary="",
    )

    assert collector._should_accept_result(
        result=near_name,
        intent="external_profiles",
        brand_name="Movyn",
        brand_url="https://movyn.ai",
    )[0] is False
    assert collector._should_accept_result(
        result=person,
        intent="external_mentions",
        brand_name="Movyn",
        brand_url="https://movyn.ai",
    )[0] is False


class _FakeExaClient:
    def __init__(self):
        self.calls: list[dict] = []

    def search(self, query: str, **kwargs):
        self.calls.append({"query": query, "kwargs": kwargs})
        if "competitors" in query:
            raise RuntimeError("fixture competitor failure")
        if "press release media coverage announcement featured in" in query:
            return SimpleNamespace(results=[])
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    url="https://brand.io/post",
                    title="Brand mention",
                    text="Brand appears in an external source",
                    highlights=[],
                    summary="",
                    score=None,
                    published_date="2026-05-15",
                )
            ]
        )


def test_search_uses_news_profile_with_freshness_window():
    collector = ExaCollector(api_key="test")
    fake = _FakeExaClient()
    collector._client = fake

    collector.search(
        '"Brand" "brand.com" news',
        intent="news",
        brand_name="Brand",
        brand_url="https://brand.com",
    )

    assert fake.calls
    call = fake.calls[0]
    assert call["kwargs"]["type"] == "fast"
    assert call["kwargs"]["category"] == "news"
    assert call["kwargs"]["num_results"] == 10
    assert "start_published_date" in call["kwargs"]


def test_collect_brand_data_emits_structured_diagnostics_for_failed_and_empty_intents(monkeypatch):
    monkeypatch.setenv("BRAND3_EXA_INCLUDE_COMPETITOR_INTENT", "1")
    collector = ExaCollector(api_key="test")
    fake = _FakeExaClient()
    collector._client = fake

    data = collector.collect_brand_data("Brand", "https://brand.com")
    diagnostics = data.diagnostics

    assert diagnostics["status"] == "degraded"
    assert diagnostics["strategy"] == EXA_STRATEGY_VERSION
    assert diagnostics["competitor_intent_enabled"] is True
    assert "competitors" in diagnostics["failed_intents"]
    assert "news" in diagnostics["no_result_intents"]
    assert diagnostics["intent_results"]["competitors"]["status"] == "search_failed"
    assert diagnostics["intent_results"]["news"]["status"] == "no_results"
    assert diagnostics["latency_buckets_by_intent"]
    assert "search_events" in data.raw_responses
    competitor_call = next(call for call in fake.calls if "competitors" in call["query"])
    assert "exclude_domains" not in competitor_call["kwargs"]
    stripped = diagnostics["intent_results"]["competitors"]["stripped_filters"]
    assert any(item.get("param") == "exclude_domains" for item in stripped)


def test_collect_brand_data_uses_precision_exa_queries_in_production():
    collector = ExaCollector(api_key="test")
    fake = _FakeExaClient()
    collector._client = fake

    data = collector.collect_brand_data("Brand", "https://brand.com")
    queries = [call["query"] for call in fake.calls]

    assert any("official website product company about services" in query for query in queries)
    assert any("company profile linkedin crunchbase business directory services" in query for query in queries)
    assert any("press mention media coverage client testimonial case study review" in query for query in queries)
    assert any("press release media coverage announcement featured in" in query for query in queries)
    assert any("what is this company services expertise overview" in query for query in queries)
    assert not any("alternatives competitors similar to Brand brand.com category" in query for query in queries)

    owned_call = next(call for call in fake.calls if "official website product company about services" in call["query"])
    profile_call = next(call for call in fake.calls if "company profile linkedin crunchbase business directory services" in call["query"])
    external_call = next(call for call in fake.calls if "press mention media coverage client testimonial case study review" in call["query"])
    news_call = next(call for call in fake.calls if "press release media coverage announcement featured in" in call["query"])
    assert owned_call["kwargs"]["include_domains"] == ["brand.com"]
    assert profile_call["kwargs"]["category"] == "company"
    assert external_call["kwargs"]["exclude_domains"] == ["brand.com"]
    assert news_call["kwargs"]["exclude_domains"] == ["brand.com"]
    assert len(data.mentions) == 1
    assert len(data.profiles) == 1
    assert data.competitors == []
    assert data.diagnostics["strategy"] == EXA_STRATEGY_VERSION
    assert data.diagnostics["competitor_intent_enabled"] is False
    assert data.diagnostics["planned_intents"] == [
        "owned_confirmation",
        "external_profiles",
        "external_mentions",
        "news",
        "ai_visibility",
    ]
    assert "owned_confirmation" in data.diagnostics["intent_results"]
    assert "external_profiles" in data.diagnostics["intent_results"]
    assert "external_mentions" in data.diagnostics["intent_results"]
    assert "competitors" not in data.diagnostics["intent_results"]


def test_collect_brand_data_promotes_profile_like_external_mentions_into_profiles():
    class MixedClient:
        def __init__(self):
            self.calls = []

        def search(self, query: str, **kwargs):
            self.calls.append({"query": query, "kwargs": kwargs})
            if "official website product company about services" in query:
                return SimpleNamespace(
                    results=[
                        SimpleNamespace(
                            url="https://brand.com/",
                            title="Brand",
                            text="Owned site",
                            highlights=[],
                            summary="",
                            score=0.8,
                            published_date="2026-05-15",
                        )
                    ]
                )
            if "press mention media coverage client testimonial case study review" in query:
                return SimpleNamespace(
                    results=[
                        SimpleNamespace(
                            url="https://startupshub.catalonia.com/startup/brand",
                            title="Brand startup profile",
                            text="Directory listing.",
                            highlights=[],
                            summary="",
                            score=0.7,
                            published_date="2026-05-15",
                        ),
                        SimpleNamespace(
                            url="https://press.example.com/brand-analysis",
                            title="Brand analysis",
                            text="Independent article.",
                            highlights=[],
                            summary="",
                            score=0.7,
                            published_date="2026-05-15",
                        ),
                    ]
                )
            return SimpleNamespace(results=[])

    collector = ExaCollector(api_key="test")
    collector._client = MixedClient()

    data = collector.collect_brand_data("Brand", "https://brand.com")

    assert [item.url for item in data.mentions] == [
        "https://brand.com/",
        "https://press.example.com/brand-analysis",
    ]
    assert [item.url for item in data.profiles] == [
        "https://startupshub.catalonia.com/startup/brand",
    ]


def test_collect_brand_data_promotes_insurtechcommunityhub_mentions_into_profiles():
    class HubClient:
        def search(self, query: str, **kwargs):
            if "press mention media coverage client testimonial case study review" in query:
                return SimpleNamespace(
                    results=[
                        SimpleNamespace(
                            url="https://insurtechcommunityhub.com/blog/socio/brand",
                            title="Brand partner profile",
                            text="Partner directory profile.",
                            highlights=[],
                            summary="",
                            score=0.7,
                            published_date="2026-05-15",
                        )
                    ]
                )
            return SimpleNamespace(results=[])

    collector = ExaCollector(api_key="test")
    collector._client = HubClient()

    data = collector.collect_brand_data("Brand", "https://brand.com")

    assert data.mentions == []
    assert [item.url for item in data.profiles] == [
        "https://insurtechcommunityhub.com/blog/socio/brand",
    ]


def test_derive_legal_name_prefers_explicit_legal_notice_signal():
    legal_name = derive_legal_name(
        brand_name="www.cofisolutions.com",
        web_data=WebData(
            url="https://www.cofisolutions.com",
            markdown_content="Aviso legal\nRazón social: COFI SOLUTIONS, S.L.\nCIF B12345678",
        ),
    )

    assert legal_name == "COFI SOLUTIONS, S.L."


def test_external_mentions_accept_legal_name_exact_match():
    class DirectoryClient:
        def search(self, query: str, **kwargs):
            return SimpleNamespace(
                results=[
                    SimpleNamespace(
                        url="https://www.einforma.com/informacion-empresa/cofi-solutions",
                        title="COFI SOLUTIONS, S.L. - Consulte CIF y dirección",
                        text="Directorio mercantil de COFI SOLUTIONS, S.L.",
                        highlights=[],
                        summary="",
                        score=0.6,
                        published_date="2026-05-15",
                    ),
                    SimpleNamespace(
                        url="https://www.einforma.com/informacion-empresa/cfo-solutions",
                        title="CFO Solutions - Consulte CIF y dirección",
                        text="Directorio mercantil de CFO Solutions.",
                        highlights=[],
                        summary="",
                        score=0.5,
                        published_date="2026-05-15",
                    ),
                ]
            )

    collector = ExaCollector(api_key="test")
    collector._client = DirectoryClient()

    results = collector.search(
        "brand query",
        intent="external_mentions",
        brand_name="COFI",
        brand_url="https://www.cofisolutions.com",
        legal_name="COFI SOLUTIONS, S.L.",
    )

    assert [item.url for item in results] == ["https://www.einforma.com/informacion-empresa/cofi-solutions"]
    assert results[0].metadata["entity_match_reason"] in {"alias_in_title", "alias_in_text", "alias_in_host"}
    provenance = results[0].metadata["external_identity_provenance"]
    assert provenance["subject_domain"] == "cofisolutions.com"
    assert provenance["source_domain"] == "einforma.com"
    assert provenance["matched_alias"] == "cofisolutionssl"
    assert provenance["candidate_strength"] == "strong"


def test_external_mentions_filters_collision_results_without_exact_entity_match():
    class CollisionClient:
        def search(self, query: str, **kwargs):
            return SimpleNamespace(
                results=[
                    SimpleNamespace(
                        url="https://cfosolutions.com/case-study",
                        title="CFO Solutions case study",
                        text="Consulting work for finance teams.",
                        highlights=[],
                        summary="",
                        score=0.5,
                        published_date="2026-05-15",
                    ),
                    SimpleNamespace(
                        url="https://www.einforma.com/informacion-empresa/cofi-solutions",
                        title="Cofi Solutions SL: consulte teléfono, CIF y dirección",
                        text="Business directory profile for Cofi Solutions SL.",
                        highlights=[],
                        summary="",
                        score=0.6,
                        published_date="2026-05-15",
                    ),
                ]
            )

    collector = ExaCollector(api_key="test")
    collector._client = CollisionClient()

    results = collector.search(
        "brand query",
        intent="external_mentions",
        brand_name="www.cofisolutions.com",
        brand_url="https://www.cofisolutions.com",
    )

    assert [item.url for item in results] == ["https://www.einforma.com/informacion-empresa/cofi-solutions"]
    diagnostics = collector._build_diagnostics()
    assert diagnostics["intent_results"]["external_mentions"]["filtered_irrelevant_count"] == 1


def test_news_filters_results_without_exact_brand_alias():
    class NewsClient:
        def search(self, query: str, **kwargs):
            return SimpleNamespace(
                results=[
                    SimpleNamespace(
                        url="https://www.businesswire.com/coherent-solutions",
                        title="Coherent Solutions closes strategic investment",
                        text="Unrelated software engineering company.",
                        highlights=[],
                        summary="",
                        score=0.4,
                        published_date="2026-05-15",
                    ),
                    SimpleNamespace(
                        url="https://press.example.com/cofisolutions-expands",
                        title="Cofisolutions expands advisory footprint",
                        text="Cofisolutions announced a new advisory initiative.",
                        highlights=[],
                        summary="",
                        score=0.7,
                        published_date="2026-05-15",
                    ),
                ]
            )

    collector = ExaCollector(api_key="test")
    collector._client = NewsClient()

    results = collector.search(
        "brand query",
        intent="news",
        brand_name="www.cofisolutions.com",
        brand_url="https://www.cofisolutions.com",
    )

    assert [item.url for item in results] == ["https://press.example.com/cofisolutions-expands"]
    diagnostics = collector._build_diagnostics()
    assert diagnostics["intent_results"]["news"]["filtered_irrelevant_count"] == 1


def test_ai_visibility_accepts_only_exact_alias_matches():
    class VisibilityClient:
        def search(self, query: str, **kwargs):
            return SimpleNamespace(
                results=[
                    SimpleNamespace(
                        url="https://startupshub.catalonia.com/company",
                        title="COFI SOLUTIONS SL at Barcelona & Catalonia Startup Hub",
                        text="Directory listing for Cofi Solutions.",
                        highlights=[],
                        summary="",
                        score=0.8,
                        published_date="2026-05-15",
                    ),
                    SimpleNamespace(
                        url="https://www.coforge.com/overview",
                        title="Coforge overview",
                        text="Enterprise modernization company.",
                        highlights=[],
                        summary="",
                        score=0.7,
                        published_date="2026-05-15",
                    ),
                ]
            )

    collector = ExaCollector(api_key="test")
    collector._client = VisibilityClient()

    results = collector.search(
        "brand query",
        intent="ai_visibility",
        brand_name="www.cofisolutions.com",
        brand_url="https://www.cofisolutions.com",
    )

    assert [item.url for item in results] == ["https://startupshub.catalonia.com/company"]
    diagnostics = collector._build_diagnostics()
    assert diagnostics["intent_results"]["ai_visibility"]["filtered_irrelevant_count"] == 1


def test_collect_brand_data_can_opt_into_exa_competitor_intent(monkeypatch):
    monkeypatch.setenv("BRAND3_EXA_INCLUDE_COMPETITOR_INTENT", "1")
    collector = ExaCollector(api_key="test")
    fake = _FakeExaClient()
    collector._client = fake

    data = collector.collect_brand_data("Brand", "https://brand.com")
    queries = [call["query"] for call in fake.calls]

    assert any("alternatives competitors similar to Brand brand.com category" in query for query in queries)
    assert data.diagnostics["competitor_intent_enabled"] is True
    assert "competitors" in data.diagnostics["planned_intents"]
    assert data.diagnostics["intent_results"]["competitors"]["status"] == "search_failed"


def test_collect_brand_data_runs_independent_intents_concurrently():
    class SlowCollector(ExaCollector):
        def __init__(self):
            super().__init__(api_key="test")
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def search(self, query: str, num_results: int | None = None, *, intent: str = "default", **kwargs):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.03)
                self._record_event(
                    {
                        "intent": intent,
                        "query": query,
                        "status": "no_results",
                        "result_count": 0,
                        "elapsed_ms": 30,
                        "latency_bucket": "sub_1s",
                    }
                )
                return []
            finally:
                with self.lock:
                    self.active -= 1

    collector = SlowCollector()

    data = collector.collect_brand_data("Brand", "https://brand.com")

    assert collector.max_active > 1
    assert data.mentions == []
    assert data.competitors == []
    assert data.news == []
    assert data.ai_visibility_results == []


def test_same_name_different_root_is_related_unresolved():
    collector = ExaCollector(api_key="test")
    fake = _FakeExaClient()
    collector._client = fake

    results = collector.search(
        "brand query",
        intent="mentions",
        brand_name="Brand",
        brand_url="https://brand.com",
    )

    assert len(results) == 1
    item = results[0]
    assert item.source_class == "related_unresolved"
    assert item.relation == "unresolved"
    assert item.requires_human_review is True
    assert item.classification_reason == "same_name_different_root_domain"


def test_search_filters_url_results_without_content_body():
    class EmptyContentClient:
        def search(self, query: str, **kwargs):
            return SimpleNamespace(
                results=[
                    SimpleNamespace(
                        url="https://press.example.com/title-only",
                        title="Title is not enough",
                        text="",
                        highlights=[],
                        summary="",
                        score=0.1,
                        published_date="2026-05-15",
                    ),
                    SimpleNamespace(
                        url="https://press.example.com/body",
                        title="Brand body exists",
                        text="Independent body content about Brand.",
                        highlights=[],
                        summary="",
                        score=0.2,
                        published_date="2026-05-15",
                    ),
                ]
            )

    collector = ExaCollector(api_key="test")
    collector._client = EmptyContentClient()

    results = collector.search(
        "brand query",
        intent="external_mentions",
        brand_name="Brand",
        brand_url="https://brand.com",
    )

    assert [item.url for item in results] == ["https://press.example.com/body"]
    diagnostics = collector._build_diagnostics()
    assert diagnostics["intent_results"]["external_mentions"]["filtered_empty_content_count"] == 1


def test_company_category_strips_unsupported_date_filters():
    collector = ExaCollector(api_key="test")
    fake = _FakeExaClient()
    collector._client = fake

    collector.search(
        "competitors similar to brand brand.com",
        intent="competitors",
        brand_name="Brand",
        brand_url="https://brand.com",
        start_crawl_date="2026-01-01",
        start_published_date="2026-01-01",
        exclude_domains=["brand.com"],
    )

    call = fake.calls[0]
    assert call["kwargs"]["category"] == "company"
    assert "start_crawl_date" not in call["kwargs"]
    assert "start_published_date" not in call["kwargs"]
    assert "exclude_domains" not in call["kwargs"]


def test_deep_reasoning_experiment_is_opt_in(monkeypatch):
    monkeypatch.setenv("BRAND3_EXA_ENABLE_DEEP_REASONING", "1")
    monkeypatch.setenv("BRAND3_EXA_DEEP_REASONING_INTENTS", "competitors,ai_visibility")
    collector = ExaCollector(api_key="test")
    fake = _FakeExaClient()
    collector._client = fake

    collector.search(
        "competitors similar to brand brand.com",
        intent="competitors",
        brand_name="Brand",
        brand_url="https://brand.com",
    )
    collector.search(
        '"Brand" "brand.com" news',
        intent="news",
        brand_name="Brand",
        brand_url="https://brand.com",
    )

    competitor_call = next(call for call in fake.calls if "competitors" in call["query"])
    news_call = next(call for call in fake.calls if "news" in call["query"])
    assert competitor_call["kwargs"]["type"] == "deep-reasoning"
    assert news_call["kwargs"]["type"] == "fast"
