from __future__ import annotations

from src.collectors.exa_collector import ExaData, ExaResult
from src.collectors.searchapi_collector import SearchApiData, SearchApiIntent, SearchApiResult
from src.services.input_collection_external_sources import (
    _collect_searchapi_fallback_input,
    _searchapi_candidate_intents,
)
from src.services.input_collection_state import AcquisitionResult
from web.scan_runner import _build_acquisition_gate


def _exa_data(*urls: str) -> ExaData:
    return ExaData(
        brand_name="Example",
        news=[
            ExaResult(
                url=url,
                title=url,
                intent="news",
                source_class="external",
                relation="external",
            )
            for url in urls
        ],
        diagnostics={
            "failed_intents": [],
            "no_result_intents": [],
            "intent_results": {
                "external_mentions": {"status": "ok", "result_count": len(urls)},
                "news": {"status": "ok", "result_count": len(urls)},
                "ai_visibility": {"status": "ok", "result_count": len(urls)},
            },
        },
    )


def test_vault_low_external_domain_diversity_triggers_budgeted_news_fallback(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(
        "src.services.input_collection_external_sources.BRAND3_SEARCHAPI_FALLBACK_INTENTS",
        ("news",),
    )

    assert _searchapi_candidate_intents(
        _exa_data("https://press.example/story"),
        effective_brand_url="https://brand.example",
    ) == ("news",)


def test_production_does_not_trigger_fallback_for_successful_thin_exa(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "production")
    monkeypatch.setattr(
        "src.services.input_collection_external_sources.BRAND3_SEARCHAPI_FALLBACK_INTENTS",
        ("news",),
    )

    assert _searchapi_candidate_intents(
        _exa_data("https://press.example/story"),
        effective_brand_url="https://brand.example",
    ) == ()


def test_vault_fallback_records_combined_domain_coverage(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    monkeypatch.setattr(
        "src.services.input_collection_external_sources.BRAND3_SEARCHAPI_FALLBACK_INTENTS",
        ("news",),
    )
    calls: list[tuple[str, ...]] = []

    class FakeSearchApiCollector:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def collect(self, brand_name, brand_url, *, intents, queries_by_intent):
            calls.append(intents)
            return SearchApiData(
                brand_name=brand_name,
                brand_url=brand_url,
                status="ok",
                intents={
                    "news": SearchApiIntent(
                        intent="news",
                        status="ok",
                        result_count=2,
                        results=[
                            SearchApiResult(url="https://second.example/story", domain="second.example"),
                            SearchApiResult(url="https://third.example/story", domain="third.example"),
                        ],
                    )
                },
                diagnostics={"result_total": 2},
            )

    acquisition_steps = {
        "exa": AcquisitionResult(
            source="exa",
            status="ok",
            cache_status="miss",
            eligible=True,
        )
    }
    data = _collect_searchapi_fallback_input(
        store=None,
        run_id=None,
        brand_name="Example",
        effective_brand_url="https://brand.example",
        exa_data=_exa_data("https://first.example/story"),
        cache_read=lambda *args: None,
        raw_input_cache={},
        acquisition_steps=acquisition_steps,
        run_input_sources={"searchapi"},
        searchapi_collector_cls=FakeSearchApiCollector,
    )

    assert data is not None
    assert calls == [("news",)]
    assert acquisition_steps["exa"].details["external_domain_count"] == 1
    details = acquisition_steps["searchapi"].details
    assert details["trigger_reason"] == "low_external_domain_diversity"
    assert details["combined_external_domain_count"] == 3
    assert details["external_domain_coverage"] == "sufficient"


def test_vault_gate_warns_when_fallback_still_has_fewer_than_three_domains(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "vault")
    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "ok", "details": {"external_domain_count": 1}},
            "searchapi": {
                "status": "ok",
                "details": {
                    "combined_external_domain_count": 2,
                    "minimum_external_domain_count": 3,
                },
            },
        }
    )

    assert gate["state"] == "warning"
    warning = next(item for item in gate["warnings"] if item["code"] == "external_domain_diversity_low")
    assert warning["observed_external_domain_count"] == 2
    assert warning["minimum_external_domain_count"] == 3
    assert gate["limitations"] == ["acquisition_gate:external_domain_diversity_low"]


def test_production_gate_ignores_vault_external_domain_threshold(monkeypatch) -> None:
    monkeypatch.setenv("BRAND3_ENVIRONMENT", "production")
    gate = _build_acquisition_gate(
        {
            "web": {"status": "ok"},
            "exa": {"status": "ok", "details": {"external_domain_count": 1}},
            "searchapi": {
                "status": "ok",
                "details": {
                    "combined_external_domain_count": 1,
                    "minimum_external_domain_count": 3,
                },
            },
        }
    )

    assert gate["state"] == "pass"
    assert gate["warnings"] == []
