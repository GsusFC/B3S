from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from src.collectors.exa_collector import EXA_STRATEGY_VERSION, ExaCollector
from src.collectors.web_collector import WebData
from src.services.legal_identity import derive_legal_name


def _identity_checker_unavailable(identity, candidates):
    raise RuntimeError("identity checker is not available in unit tests")


@pytest.fixture(autouse=True)
def _no_default_identity_checker(monkeypatch):
    # src.config loads .env, so the default LLM checker could reach a real provider.
    monkeypatch.setattr(
        "src.collectors.exa_collector.llm_identity_check",
        _identity_checker_unavailable,
        raising=False,
    )


class _RecordingChecker:
    """Stands in for the LLM identity check and records every batch it gets."""

    def __init__(self, decide=lambda candidates: set(range(len(candidates)))):
        self.decide = decide
        self.calls: list[tuple] = []

    def __call__(self, identity, candidates):
        self.calls.append((identity, list(candidates)))
        return self.decide(candidates)


def _accept_urls(*urls: str):
    return lambda candidates: {index for index, item in enumerate(candidates) if item.url in urls}


def _raise(error: Exception):
    def decide(candidates):
        raise error

    return decide


def _raise_identity_check_error(reason: str):
    def decide(candidates):
        from src.services.exa_brand_identity import IdentityCheckError

        raise IdentityCheckError(reason)

    return decide


def _result(url: str, title: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        url=url,
        title=title,
        text=text,
        highlights=[],
        summary="",
        score=0.5,
        published_date="2026-05-15",
    )


class _StaticClient:
    def __init__(self, results: list[SimpleNamespace]):
        self.results = results

    def search(self, query: str, **kwargs):
        return SimpleNamespace(results=list(self.results))


_PRIMARY_URL = "https://primary.studio"
_PRIMARY_NAMESAKES = [
    _result(
        "https://www.primary.com/kids-basics",
        "Primary | Kids clothing basics",
        "Primary makes soft, colorful basics for kids.",
    ),
    _result(
        "https://www.primarywave.com/news/catalog-deal",
        "Primary Wave signs a new catalog deal",
        "Primary Wave Music announced another catalog acquisition. " * 20,
    ),
]
_PRIMARY_GENUINE = _result(
    "https://www.itsnicethat.com/articles/primary-brand-sprints",
    "Primary rethinks the brand sprint",
    "The London design studio Primary (primary.studio) runs week-long brand sprints.",
)


def test_external_intents_exclude_person_profiles_and_defer_near_names_to_identity_check():
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
    ) == (True, "no_alias_match", 0.0)
    assert collector._should_accept_result(
        result=person,
        intent="external_mentions",
        brand_name="Movyn",
        brand_url="https://movyn.ai",
    )[0] is False


def test_identity_check_rejects_namesakes_and_keeps_the_genuine_result():
    checker = _RecordingChecker(decide=_accept_urls(_PRIMARY_GENUINE.url))
    collector = ExaCollector(api_key="test", identity_checker=checker)
    collector._client = _StaticClient([*_PRIMARY_NAMESAKES, _PRIMARY_GENUINE])

    results = collector.search(
        "brand query",
        intent="external_mentions",
        brand_name="Primary",
        brand_url=_PRIMARY_URL,
    )

    assert [item.url for item in results] == [_PRIMARY_GENUINE.url]
    assert results[0].metadata["entity_match_reason"] == "identity_check"
    assert len(checker.calls) == 1
    identity, candidates = checker.calls[0]
    assert (identity.name, identity.domain) == ("Primary", "primary.studio")
    assert [item.url for item in candidates] == [
        *(namesake.url for namesake in _PRIMARY_NAMESAKES),
        _PRIMARY_GENUINE.url,
    ]
    assert candidates[0].title == "Primary | Kids clothing basics"
    assert all(0 < len(item.text) <= 300 for item in candidates)
    intent_result = collector._build_diagnostics()["intent_results"]["external_mentions"]
    assert intent_result["identity_method"] == "llm"
    assert intent_result["identity_checked_count"] == 3
    assert intent_result["identity_rejected_count"] == 2
    assert intent_result["identity_error"] == ""
    assert intent_result["filtered_irrelevant_count"] == 2
    assert intent_result["result_count"] == 1


def test_owned_surfaces_are_excluded_before_the_identity_check():
    checker = _RecordingChecker()
    collector = ExaCollector(api_key="test", identity_checker=checker)
    collector._client = _StaticClient(
        [
            _result("https://primary.studio/work", "Primary work", "Our brand sprint work."),
            _result(
                "https://www.linkedin.com/posts/primary-studio_brand-sprint-activity-1",
                "Primary on LinkedIn",
                "We just wrapped another brand sprint.",
            ),
            _PRIMARY_GENUINE,
        ]
    )

    results = collector.search(
        "brand query",
        intent="news",
        brand_name="Primary",
        brand_url=_PRIMARY_URL,
    )

    assert [item.url for item in results] == [_PRIMARY_GENUINE.url]
    assert [item.url for item in checker.calls[0][1]] == [_PRIMARY_GENUINE.url]
    intent_result = collector._build_diagnostics()["intent_results"]["news"]
    assert intent_result["identity_checked_count"] == 1
    assert intent_result["identity_rejected_count"] == 0
    assert intent_result["filtered_irrelevant_count"] == 2


@pytest.mark.parametrize(
    ("decide", "identity_error"),
    [
        (_raise(RuntimeError("provider said: secret detail")), "RuntimeError"),
        (_raise_identity_check_error("llm_timeout"), "llm_timeout"),
        (lambda candidates: None, "no_verdict"),
        (lambda candidates: {len(candidates)}, "invalid_output"),
    ],
    ids=["raises", "reason_code", "none", "out_of_range"],
)
def test_identity_check_failure_falls_back_to_strong_aliases(decide, identity_error):
    strong_title = _result(
        "https://www.designweek.co.uk/news/studio-opening",
        "Primary Studio opens in Lisbon",
        "The team expands to a second city.",
    )
    collector = ExaCollector(api_key="test", identity_checker=_RecordingChecker(decide=decide))
    collector._client = _StaticClient([*_PRIMARY_NAMESAKES, _PRIMARY_GENUINE, strong_title])

    results = collector.search(
        "brand query",
        intent="external_profiles",
        brand_name="Primary",
        brand_url=_PRIMARY_URL,
    )

    assert [item.url for item in results] == [_PRIMARY_GENUINE.url, strong_title.url]
    assert {item.metadata["entity_match_reason"] for item in results} == {"strong_alias_fallback"}
    diagnostics = collector._build_diagnostics()
    intent_result = diagnostics["intent_results"]["external_profiles"]
    assert intent_result["identity_method"] == "strong_alias_fallback"
    assert intent_result["identity_error"] == identity_error
    assert intent_result["identity_checked_count"] == 4
    assert intent_result["identity_rejected_count"] == 2
    assert intent_result["filtered_irrelevant_count"] == 2
    assert "secret detail" not in str(diagnostics)


class _FakeExaClient:
    def __init__(self):
        self.calls: list[dict] = []

    def search(self, query: str, **kwargs):
        self.calls.append({"query": query, "kwargs": kwargs})
        if "competitors" in query:
            raise RuntimeError("fixture competitor failure")
        if query.startswith("News coverage and press releases about"):
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
    checker = _RecordingChecker()
    collector = ExaCollector(api_key="test", identity_checker=checker)
    fake = _FakeExaClient()
    collector._client = fake

    data = collector.collect_brand_data("Brand", "https://brand.com", legal_name="Brand Inc")
    calls = {call["query"]: call["kwargs"] for call in fake.calls}

    owned_query = '"Brand" "brand.com" official website product company about services'
    profiles_query = (
        "Company profile pages about Brand (brand.com) on LinkedIn, Crunchbase, Wikipedia and business directories"
    )
    mentions_query = "Articles, case studies and reviews on other websites that mention Brand (brand.com)"
    news_query = "News coverage and press releases about Brand (brand.com), from the last 12 months"
    ai_visibility_query = '"Brand" "brand.com" what is this company services expertise overview'
    assert set(calls) == {owned_query, profiles_query, mentions_query, news_query, ai_visibility_query}
    assert not any("Brand Inc" in query for query in calls)

    assert calls[owned_query]["include_domains"] == ["brand.com"]
    # Exa's company category returns company homepages, which the external
    # filter rejects; profile pages come back only without it.
    assert "category" not in calls[profiles_query]
    assert calls[mentions_query]["exclude_domains"] == ["brand.com"]
    assert calls[news_query]["exclude_domains"] == ["brand.com"]
    assert calls[news_query]["category"] == "news"
    assert "start_published_date" in calls[news_query]
    # Profiles and mentions are identity-checked; news came back empty, and
    # owned_confirmation and ai_visibility keep their deterministic rules.
    assert len(checker.calls) == 2
    assert len(data.mentions) == 1
    assert len(data.profiles) == 1
    assert data.competitors == []
    assert EXA_STRATEGY_VERSION == "precision_vnext_v4"
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


def test_collect_brand_data_describes_the_brand_with_its_owned_site_identity():
    from src.services.exa_brand_identity import BrandIdentity

    collector = ExaCollector(api_key="test", identity_checker=_RecordingChecker())
    fake = _FakeExaClient()
    collector._client = fake

    collector.collect_brand_data(
        "sensesbit.com",
        "https://sensesbit.com",
        identity=BrandIdentity(
            name="Sensesbit",
            domain="sensesbit.com",
            description="Sensory analysis software for food teams",
            strong_aliases=("sensesbitcom",),
        ),
    )
    queries = {call["query"] for call in fake.calls}

    assert (
        "Company profile pages about Sensesbit (sensesbit.com) on LinkedIn, Crunchbase, Wikipedia and business directories"
        in queries
    )
    assert (
        "Articles, case studies and reviews on other websites that mention Sensesbit (sensesbit.com), "
        "Sensory analysis software for food teams"
    ) in queries
    assert (
        "News coverage and press releases about Sensesbit (sensesbit.com), "
        "Sensory analysis software for food teams, from the last 12 months"
    ) in queries


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
            if query.startswith("Articles, case studies and reviews on other websites that mention"):
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

    collector = ExaCollector(api_key="test", identity_checker=_RecordingChecker())
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
            if query.startswith("Articles, case studies and reviews on other websites that mention"):
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

    collector = ExaCollector(api_key="test", identity_checker=_RecordingChecker())
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


def test_identity_checked_result_keeps_deterministic_legal_name_provenance():
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

    collector = ExaCollector(
        api_key="test",
        identity_checker=_RecordingChecker(
            decide=_accept_urls("https://www.einforma.com/informacion-empresa/cofi-solutions")
        ),
    )
    collector._client = DirectoryClient()

    results = collector.search(
        "brand query",
        intent="external_mentions",
        brand_name="COFI",
        brand_url="https://www.cofisolutions.com",
        legal_name="COFI SOLUTIONS, S.L.",
    )

    assert [item.url for item in results] == ["https://www.einforma.com/informacion-empresa/cofi-solutions"]
    assert results[0].metadata["entity_match_reason"] == "identity_check"
    # The LLM verdict never becomes provenance: downstream reproduces only the
    # deterministic alias match.
    provenance = results[0].metadata["external_identity_provenance"]
    assert provenance["subject_domain"] == "cofisolutions.com"
    assert provenance["source_domain"] == "einforma.com"
    assert provenance["matched_alias"] == "cofisolutionssl"
    assert provenance["match_method"] == "alias_in_title"
    assert provenance["candidate_strength"] == "strong"


def test_external_mentions_counts_identity_rejected_collisions_as_filtered():
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

    collector = ExaCollector(
        api_key="test",
        identity_checker=_RecordingChecker(
            decide=_accept_urls("https://www.einforma.com/informacion-empresa/cofi-solutions")
        ),
    )
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


def test_news_counts_identity_rejected_collisions_as_filtered():
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

    collector = ExaCollector(
        api_key="test",
        identity_checker=_RecordingChecker(decide=_accept_urls("https://press.example.com/cofisolutions-expands")),
    )
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

    checker = _RecordingChecker()
    collector = ExaCollector(api_key="test", identity_checker=checker)
    collector._client = EmptyContentClient()

    results = collector.search(
        "brand query",
        intent="external_mentions",
        brand_name="Brand",
        brand_url="https://brand.com",
    )

    assert [item.url for item in results] == ["https://press.example.com/body"]
    assert [item.url for item in checker.calls[0][1]] == ["https://press.example.com/body"]
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
