from __future__ import annotations

from src.collectors.exa_collector import EXA_STRATEGY_VERSION, ExaData
from src.collectors.hyperbrowser_collector import HyperbrowserFetchData
from src.collectors.parallel_shadow_collector import ParallelShadowData, ParallelShadowIntent, ParallelShadowResult
from src.collectors.web_collector import WebData
from src.services.input_collection import (
    _cache_reader,
    _collect_exa_input,
    _collect_hyperbrowser_input,
    _collect_parallel_shadow_input,
    _collect_web_input,
    _set_acquisition_state,
    from_exa_payload,
)
from src.storage.sqlite_store import _MalformedJSONPayload


def test_from_exa_payload_preserves_diagnostics():
    payload = {
        "brand_name": "Brand",
        "mentions": [],
        "profiles": [],
        "competitors": [],
        "ai_visibility_results": [],
        "news": [],
        "raw_responses": {"search_events": []},
        "diagnostics": {
            "strategy": EXA_STRATEGY_VERSION,
            "status": "degraded",
            "failed_intents": ["mentions"],
        },
    }

    exa = from_exa_payload(payload)

    assert isinstance(exa, ExaData)
    assert exa is not None
    assert exa.diagnostics["status"] == "degraded"
    assert exa.diagnostics["failed_intents"] == ["mentions"]


def test_from_exa_payload_rejects_legacy_strategy_cache():
    payload = {
        "brand_name": "Brand",
        "mentions": [],
        "profiles": [],
        "competitors": [],
        "ai_visibility_results": [],
        "news": [],
        "raw_responses": {"search_events": []},
        "diagnostics": {"status": "ok", "intent_results": {"competitors": {}}},
    }

    assert from_exa_payload(payload) is None


def test_set_acquisition_state_updates_cache_and_preserves_existing_details():
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}

    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="web",
        raw_cache_status="error",
        status="cache_error",
        cache_status="error",
        eligible=True,
        details={"cache_error": "sqlite unavailable"},
    )
    _set_acquisition_state(
        raw_input_cache,
        acquisition_steps,
        source="web",
        raw_cache_status="miss",
        status="fetched",
        cache_status="miss",
        eligible=True,
        details={"chars": 128},
    )

    assert raw_input_cache["web"] == "miss"
    assert acquisition_steps["web"].status == "fetched"
    assert acquisition_steps["web"].cache_status == "miss"
    assert acquisition_steps["web"].details == {
        "cache_error": "sqlite unavailable",
        "chars": 128,
    }


class _FakeExaCollector:
    calls = 0
    legal_names: list[str | None] = []

    def __init__(self, api_key=None):
        self.api_key = api_key

    def collect_brand_data(self, brand_name: str, brand_url: str, *, legal_name: str | None = None):
        type(self).calls += 1
        type(self).legal_names.append(legal_name)
        return ExaData(
            brand_name=brand_name,
            mentions=[],
            news=[],
            diagnostics={
                "strategy": EXA_STRATEGY_VERSION,
                "status": "degraded",
                "failed_intents": ["mentions"],
                "no_result_intents": [],
            },
        )


class _MatrixExaCollector:
    diagnostics: dict[str, object] = {}
    calls = 0

    def __init__(self, api_key=None):
        self.api_key = api_key

    def collect_brand_data(self, brand_name: str, brand_url: str, *, legal_name: str | None = None):
        type(self).calls += 1
        return ExaData(
            brand_name=brand_name,
            mentions=[],
            news=[],
            diagnostics=dict(type(self).diagnostics),
        )


class _FailingCacheStore:
    def get_latest_raw_input(self, **_kwargs):
        raise RuntimeError("sqlite unavailable")


class _InvalidCacheStore:
    def get_latest_raw_input(self, **_kwargs):
        return {"unexpected": "payload"}


class _LegacyExaCacheStore:
    def get_latest_raw_input(self, **_kwargs):
        return {
            "brand_name": "Brand",
            "mentions": [],
            "competitors": [],
            "ai_visibility_results": [],
            "news": [],
            "raw_responses": {"search_events": []},
            "diagnostics": {"status": "ok"},
        }


class _MalformedCacheStore:
    def get_latest_raw_input(self, **_kwargs):
        return _MalformedJSONPayload(
            field="raw_inputs.payload_json",
            raw_json="{not-json",
            error="Expecting property name enclosed in double quotes",
        )


class _FakeWebCollector:
    def __init__(self, api_key=None):
        self.api_key = api_key

    def scrape(self, url: str):
        return WebData(url=url, markdown_content="Fresh owned homepage copy")


class _FailingStorageStore:
    def save_raw_input(self, *_args, **_kwargs):
        raise RuntimeError("disk is read-only")


class _RecordingStorageStore:
    def __init__(self):
        self.saved = []

    def save_raw_input(self, run_id, source, payload):
        self.saved.append((run_id, source, payload))


def test_collect_web_input_records_raw_payload_ref_after_successful_storage():
    acquisition_steps: dict[str, object] = {}
    raw_input_cache: dict[str, str] = {}
    store = _RecordingStorageStore()

    web_data, _collector = _collect_web_input(
        store=store,
        run_id=123,
        url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        web_collector_cls=_FakeWebCollector,
    )

    assert store.saved == [(123, "web", web_data)]
    assert acquisition_steps["web"].details["raw_payload_ref"] == {
        "store": "raw_inputs",
        "run_id": 123,
        "source": "web",
    }


def test_collect_exa_input_derives_legal_name_from_owned_web_content():
    acquisition_steps: dict[str, object] = {}
    raw_input_cache: dict[str, str] = {}
    _FakeExaCollector.calls = 0
    _FakeExaCollector.legal_names = []

    exa_data, _collector = _collect_exa_input(
        store=None,
        run_id=None,
        brand_name="www.cofisolutions.com",
        effective_brand_url="https://www.cofisolutions.com",
        web_data=WebData(
            url="https://www.cofisolutions.com",
            markdown_content="Aviso legal\nRazón social: COFI SOLUTIONS, S.L.\nCIF: B12345678",
        ),
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        exa_collector_cls=_FakeExaCollector,
    )

    assert exa_data.brand_name == "www.cofisolutions.com"
    assert _FakeExaCollector.calls == 1
    assert _FakeExaCollector.legal_names == ["COFI SOLUTIONS, S.L."]


def test_collect_web_input_preserves_cache_read_error_in_acquisition_details():
    acquisition_steps: dict[str, object] = {}
    raw_input_cache: dict[str, str] = {}
    cache_read = _cache_reader(
        store=_FailingCacheStore(),
        brand_name="Brand",
        url="https://brand.com",
        refresh=False,
        acquisition_steps=acquisition_steps,
    )

    web_data, _collector = _collect_web_input(
        store=None,
        run_id=None,
        url="https://brand.com",
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        web_collector_cls=_FakeWebCollector,
    )

    assert web_data.markdown_content == "Fresh owned homepage copy"
    assert raw_input_cache["web"] == "miss"
    assert acquisition_steps["web"].status == "fetched"
    assert acquisition_steps["web"].cache_status == "miss"
    assert acquisition_steps["web"].details["cache_error"] == "sqlite unavailable"


def test_collect_web_input_preserves_storage_error_in_acquisition_details():
    acquisition_steps: dict[str, object] = {}
    raw_input_cache: dict[str, str] = {}

    web_data, _collector = _collect_web_input(
        store=_FailingStorageStore(),
        run_id=123,
        url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        web_collector_cls=_FakeWebCollector,
    )

    assert web_data.markdown_content == "Fresh owned homepage copy"
    assert raw_input_cache["web"] == "miss"
    assert acquisition_steps["web"].status == "fetched"
    assert acquisition_steps["web"].cache_status == "miss"
    assert acquisition_steps["web"].details["storage_errors"] == [
        {"action": "web save", "error": "disk is read-only"}
    ]


def test_collect_web_input_preserves_invalid_cache_payload_in_acquisition_details():
    acquisition_steps: dict[str, object] = {}
    raw_input_cache: dict[str, str] = {}
    cache_read = _cache_reader(
        store=_InvalidCacheStore(),
        brand_name="Brand",
        url="https://brand.com",
        refresh=False,
        acquisition_steps=acquisition_steps,
    )

    web_data, _collector = _collect_web_input(
        store=None,
        run_id=None,
        url="https://brand.com",
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        web_collector_cls=_FakeWebCollector,
    )

    assert web_data.markdown_content == "Fresh owned homepage copy"
    assert raw_input_cache["web"] == "miss"
    assert acquisition_steps["web"].status == "fetched"
    assert acquisition_steps["web"].cache_status == "miss"
    assert "unexpected" in acquisition_steps["web"].details["cache_error"]


def test_collect_exa_input_marks_partial_when_failed_intents_exist():
    _FakeExaCollector.calls = 0
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}

    exa_data, _collector = _collect_exa_input(
        store=None,
        run_id=None,
        brand_name="Brand",
        effective_brand_url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        exa_collector_cls=_FakeExaCollector,
    )

    assert raw_input_cache["exa"] == "partial"
    assert exa_data.diagnostics["status"] == "degraded"
    assert acquisition_steps["exa"].status == "partial"
    assert acquisition_steps["exa"].eligible is True
    assert _FakeExaCollector.calls == 1


def test_collect_exa_input_keeps_ok_when_only_external_profiles_are_empty():
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}
    _MatrixExaCollector.calls = 0
    _MatrixExaCollector.diagnostics = {
        "strategy": EXA_STRATEGY_VERSION,
        "status": "ok",
        "failed_intents": [],
        "no_result_intents": ["external_profiles"],
        "intent_results": {
            "external_mentions": {"result_count": 9},
            "news": {"result_count": 4},
            "ai_visibility": {"result_count": 3},
            "external_profiles": {"result_count": 0},
        },
    }

    _exa_data, _collector = _collect_exa_input(
        store=None,
        run_id=None,
        brand_name="Toteemi",
        effective_brand_url="https://toteemi.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        exa_collector_cls=_MatrixExaCollector,
    )

    assert raw_input_cache["exa"] == "miss"
    assert acquisition_steps["exa"].status == "ok"
    assert acquisition_steps["exa"].details["no_result_intents"] == ["external_profiles"]
    assert _MatrixExaCollector.calls == 1


def test_collect_exa_input_marks_empty_when_all_external_proof_intents_are_empty():
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}
    _MatrixExaCollector.calls = 0
    _MatrixExaCollector.diagnostics = {
        "strategy": EXA_STRATEGY_VERSION,
        "status": "ok",
        "failed_intents": [],
        "no_result_intents": ["external_mentions", "news", "ai_visibility"],
        "intent_results": {
            "external_mentions": {"result_count": 0},
            "news": {"result_count": 0},
            "ai_visibility": {"result_count": 0},
        },
    }

    _exa_data, _collector = _collect_exa_input(
        store=None,
        run_id=None,
        brand_name="Brand",
        effective_brand_url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        exa_collector_cls=_MatrixExaCollector,
    )

    assert acquisition_steps["exa"].status == "empty"
    assert acquisition_steps["exa"].details["no_result_intents"] == [
        "external_mentions",
        "news",
        "ai_visibility",
    ]


def test_collect_exa_input_reuses_current_strategy_cache_without_collecting():
    _FakeExaCollector.calls = 0
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}
    cached_payload = {
        "brand_name": "Brand",
        "mentions": [],
        "competitors": [],
        "ai_visibility_results": [],
        "news": [],
        "raw_responses": {"search_events": []},
        "diagnostics": {"strategy": EXA_STRATEGY_VERSION, "status": "ok"},
    }

    exa_data, _collector = _collect_exa_input(
        store=None,
        run_id=None,
        brand_name="Brand",
        effective_brand_url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: from_exa_payload(cached_payload),
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        exa_collector_cls=_FakeExaCollector,
    )

    assert isinstance(exa_data, ExaData)
    assert exa_data.diagnostics["strategy"] == EXA_STRATEGY_VERSION
    assert raw_input_cache["exa"] == "hit"
    assert acquisition_steps["exa"].status == "hit"
    assert acquisition_steps["exa"].details["mentions"] == 0
    assert _FakeExaCollector.calls == 0


def test_collect_exa_input_invalidates_legacy_strategy_cache_and_collects_fresh():
    _FakeExaCollector.calls = 0
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}
    cache_read = _cache_reader(
        store=_LegacyExaCacheStore(),
        brand_name="Brand",
        url="https://brand.com",
        refresh=False,
        acquisition_steps=acquisition_steps,
    )

    exa_data, _collector = _collect_exa_input(
        store=None,
        run_id=None,
        brand_name="Brand",
        effective_brand_url="https://brand.com",
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        exa_collector_cls=_FakeExaCollector,
    )

    assert raw_input_cache["exa"] == "partial"
    assert exa_data.diagnostics["strategy"] == EXA_STRATEGY_VERSION
    assert acquisition_steps["exa"].status == "partial"
    assert acquisition_steps["exa"].details["cache_error"] == "cached payload was not accepted by decoder"
    assert _FakeExaCollector.calls == 1


def test_collect_exa_input_records_malformed_cache_payload_and_collects_fresh():
    _FakeExaCollector.calls = 0
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}
    cache_read = _cache_reader(
        store=_MalformedCacheStore(),
        brand_name="Brand",
        url="https://brand.com",
        refresh=False,
        acquisition_steps=acquisition_steps,
    )

    exa_data, _collector = _collect_exa_input(
        store=None,
        run_id=None,
        brand_name="Brand",
        effective_brand_url="https://brand.com",
        cache_read=cache_read,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        exa_collector_cls=_FakeExaCollector,
    )

    assert raw_input_cache["exa"] == "partial"
    assert exa_data.diagnostics["strategy"] == EXA_STRATEGY_VERSION
    assert acquisition_steps["exa"].status == "partial"
    assert acquisition_steps["exa"].details["payload_field"] == "raw_inputs.payload_json"
    assert acquisition_steps["exa"].details["raw_json"] == "{not-json"
    assert _FakeExaCollector.calls == 1


class _FakeParallelShadowCollector:
    def collect(self, brand_name: str, brand_url: str):
        return ParallelShadowData(
            brand_name=brand_name,
            brand_url=brand_url,
            status="ok",
            intents={
                "mentions": ParallelShadowIntent(
                    intent="mentions",
                    status="ok",
                    result_count=1,
                    unique_domains=["g2.com"],
                    results=[
                        ParallelShadowResult(
                            url="https://g2.com/products/brand/reviews",
                            title="Brand Reviews",
                            excerpt="Shadow review signal.",
                        )
                    ],
                )
            },
        )


class _FakeHyperbrowserCollector:
    def fetch(self, url: str, **_kwargs):
        return HyperbrowserFetchData(
            url=url,
            source_url=url,
            markdown="Hyperbrowser owned page content",
            links=["https://brand.com/about"],
            metadata={"confidence": 0.81, "contentHash": "hb-123"},
        )


def test_collect_parallel_shadow_input_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BRAND3_PARALLEL_SHADOW_ENABLED", raising=False)
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}

    result = _collect_parallel_shadow_input(
        store=None,
        run_id=None,
        brand_name="Brand",
        effective_brand_url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        parallel_shadow_collector_cls=_FakeParallelShadowCollector,
    )

    assert result is None
    assert raw_input_cache["parallel_shadow"] == "disabled"
    assert acquisition_steps["parallel_shadow"].status == "disabled"
    assert acquisition_steps["parallel_shadow"].eligible is False


def test_collect_parallel_shadow_input_runs_when_enabled(monkeypatch):
    monkeypatch.setenv("BRAND3_PARALLEL_SHADOW_ENABLED", "1")
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}

    result = _collect_parallel_shadow_input(
        store=None,
        run_id=None,
        brand_name="Brand",
        effective_brand_url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        parallel_shadow_collector_cls=_FakeParallelShadowCollector,
    )

    assert result is not None
    assert result.summary()["result_total"] == 1
    assert raw_input_cache["parallel_shadow"] == "ok"
    assert acquisition_steps["parallel_shadow"].status == "ok"
    assert acquisition_steps["parallel_shadow"].eligible is True


def test_collect_parallel_shadow_input_reuses_cached_payload(monkeypatch):
    monkeypatch.setenv("BRAND3_PARALLEL_SHADOW_ENABLED", "1")
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}
    cached = _FakeParallelShadowCollector().collect("Brand", "https://brand.com").to_dict()

    result = _collect_parallel_shadow_input(
        store=None,
        run_id=None,
        brand_name="Brand",
        effective_brand_url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: cached,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        parallel_shadow_collector_cls=_FakeParallelShadowCollector,
    )

    assert result is not None
    assert result.summary()["result_total"] == 1
    assert raw_input_cache["parallel_shadow"] == "hit"
    assert acquisition_steps["parallel_shadow"].status == "hit"
    assert acquisition_steps["parallel_shadow"].eligible is True


def test_collect_hyperbrowser_input_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BRAND3_HYPERBROWSER_ENABLED", raising=False)
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}

    result = _collect_hyperbrowser_input(
        store=None,
        run_id=None,
        url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        hyperbrowser_collector_cls=_FakeHyperbrowserCollector,
    )

    assert result is None
    assert raw_input_cache["hyperbrowser"] == "disabled"
    assert acquisition_steps["hyperbrowser"].status == "disabled"
    assert acquisition_steps["hyperbrowser"].details["channel"] == "web_shadow"
    assert acquisition_steps["hyperbrowser"].eligible is False


def test_collect_hyperbrowser_input_runs_when_enabled_via_run_sources():
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}

    result = _collect_hyperbrowser_input(
        store=None,
        run_id=None,
        url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        run_input_sources={"hyperbrowser"},
        hyperbrowser_collector_cls=_FakeHyperbrowserCollector,
    )

    assert result is not None
    assert result.text_chars > 0
    assert raw_input_cache["hyperbrowser"] == "miss"
    assert acquisition_steps["hyperbrowser"].status == "ok"
    assert acquisition_steps["hyperbrowser"].details["provider"] == "hyperbrowser"
    assert acquisition_steps["hyperbrowser"].details["source_url"] == "https://brand.com"


def test_collect_hyperbrowser_input_records_raw_payload_ref_after_successful_storage():
    acquisition_steps: dict[str, object] = {}
    raw_input_cache: dict[str, str] = {}
    store = _RecordingStorageStore()

    result = _collect_hyperbrowser_input(
        store=store,
        run_id=123,
        url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: None,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        run_input_sources={"hyperbrowser"},
        hyperbrowser_collector_cls=_FakeHyperbrowserCollector,
    )

    assert result is not None
    assert store.saved == [(123, "hyperbrowser", result)]
    assert acquisition_steps["hyperbrowser"].details["raw_payload_ref"] == {
        "store": "raw_inputs",
        "run_id": 123,
        "source": "hyperbrowser",
    }


def test_collect_hyperbrowser_input_reuses_cached_payload():
    raw_input_cache: dict[str, str] = {}
    acquisition_steps: dict[str, object] = {}
    cached = HyperbrowserFetchData(
        url="https://brand.com",
        source_url="https://brand.com",
        markdown="Cached Hyperbrowser content",
        metadata={"confidence": 0.72, "contentHash": "hb-cached"},
    )

    result = _collect_hyperbrowser_input(
        store=None,
        run_id=None,
        url="https://brand.com",
        cache_read=lambda *_args, **_kwargs: cached,
        raw_input_cache=raw_input_cache,
        acquisition_steps=acquisition_steps,
        run_input_sources={"hyperbrowser"},
        hyperbrowser_collector_cls=_FakeHyperbrowserCollector,
    )

    assert result is not None
    assert result.markdown == "Cached Hyperbrowser content"
    assert raw_input_cache["hyperbrowser"] == "hit"
    assert acquisition_steps["hyperbrowser"].status == "hit"
    assert acquisition_steps["hyperbrowser"].details["content_hash"] == "hb-cached"
