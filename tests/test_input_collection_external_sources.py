from types import SimpleNamespace

from src.services.input_collection_external_sources import _searchapi_candidate_intents


def test_searchapi_candidate_intents_include_explicit_exa_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.services.input_collection_external_sources.BRAND3_SEARCHAPI_FALLBACK_INTENTS",
        {"news", "external_mentions"},
    )
    exa_data = SimpleNamespace(
        diagnostics={
            "failed_intents": ["news"],
            "no_result_intents": ["external_mentions"],
            "intent_results": {},
        }
    )

    assert _searchapi_candidate_intents(exa_data) == ("news", "external_mentions")


def test_searchapi_candidate_intents_trigger_when_external_exa_results_are_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.services.input_collection_external_sources.BRAND3_SEARCHAPI_FALLBACK_INTENTS",
        {"news", "external_mentions", "ai_visibility"},
    )
    exa_data = SimpleNamespace(
        diagnostics={
            "failed_intents": [],
            "no_result_intents": [],
            "intent_results": {
                "owned_confirmation": {"status": "ok", "result_count": 5},
                "external_mentions": {"status": "ok", "result_count": 0},
                "news": {"status": "ok", "result_count": 0},
                "ai_visibility": {"status": "ok", "result_count": 0},
            },
        }
    )

    assert _searchapi_candidate_intents(exa_data) == ("news", "external_mentions", "ai_visibility")
